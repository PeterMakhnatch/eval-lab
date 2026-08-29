"""Disabled-by-default continuous-loop operator validation (Ops/Runner adapters).

Not the Platform control-plane daemon. Never starts launchd, systemd, Docker,
Harbor, or billable runs. Writes only to an explicit operator state directory.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml

CONTROL_AGENTS = frozenset({"oracle", "nop"})
MODES = frozenset({"DISABLED", "PAUSED", "RUNNING", "DRAINING", "MAINTENANCE", "KILLED"})
KILL_DISPOSITION = "FAILED_OPERATOR_KILL"
DEFAULT_MODE = "DISABLED"

REASON_MISSING_ENABLE_TOKEN = "missing_enable_token"
REASON_MISSING_STANDING_APPROVAL = "missing_standing_approval"
REASON_MISSING_BUDGET = "missing_budget"
REASON_MISSING_SECRET = "missing_secret"
REASON_STALE_HEARTBEAT = "stale_heartbeat"
REASON_DRAIN_INCOMPLETE = "drain_incomplete"
REASON_DEFAULT_DISABLED = "default_disabled"
REASON_SAME_IDENTITY = "same_enable_and_approval_identity"
REASON_BILLABLE_REFUSED = "billable_refused"

CLOSED_REASONS = frozenset(
    {
        REASON_MISSING_ENABLE_TOKEN,
        REASON_MISSING_STANDING_APPROVAL,
        REASON_MISSING_BUDGET,
        REASON_MISSING_SECRET,
        REASON_STALE_HEARTBEAT,
        REASON_DRAIN_INCOMPLETE,
        REASON_DEFAULT_DISABLED,
        REASON_SAME_IDENTITY,
        REASON_BILLABLE_REFUSED,
    }
)

REQUIRED_POLICY_FIELDS = (
    "policy_schema_version",
    "approval_signature_ref",
    "approval_digest",
    "slo_freshness",
    "operational_limits",
    "quality_and_quarantine",
)


@dataclass(frozen=True)
class OperatorVerdict:
    ok: bool
    reason: str | None
    mode: str
    detail: str
    payload: dict[str, Any]

    def exit_code(self) -> int:
        return 0 if self.ok else 2


@dataclass
class OperatorContext:
    state_dir: Path
    now: datetime
    enable_token: str
    standing_approval: str
    enable_identity: str
    approval_identity: str
    budget_present: bool
    secret_ref: str
    secret_present: bool
    policy: dict[str, Any]
    agent: str
    drain_timeout_seconds: float | None


def _utc_now(raw: str | None) -> datetime:
    if raw:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return datetime.now(UTC)


def load_loop_policy(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        return {}
    body = raw.get("continuous_loop_policy", raw)
    return body if isinstance(body, dict) else {}


def policy_complete(policy: Mapping[str, Any]) -> bool:
    return all(field in policy and policy[field] not in (None, "", {}) for field in REQUIRED_POLICY_FIELDS)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def _append_event(state_dir: Path, event: Mapping[str, Any]) -> None:
    path = state_dir / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(event), sort_keys=True) + "\n")


def read_mode(state_dir: Path) -> str:
    value = _read_text(state_dir / "mode")
    return value if value in MODES else DEFAULT_MODE


def write_mode(state_dir: Path, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode}")
    _write_text(state_dir / "mode", mode)


def context_from_env(
    *,
    state_dir: Path,
    policy_path: Path | None,
    now: datetime,
    agent: str,
    drain_timeout_seconds: float | None,
    environ: Mapping[str, str],
) -> OperatorContext:
    approval_file = state_dir / "standing_approval"
    budget_file = state_dir / "budget"
    secret_probe = state_dir / "secret_present"
    policy = load_loop_policy(policy_path)
    budget_env = environ.get("EVAL_LAB_BUDGET_PRESENT", "")
    budget_present = budget_file.is_file() or budget_env in {"1", "true", "yes"}
    secret_ref = environ.get("EVAL_LAB_SECRET_REF", "")
    secret_present = secret_probe.is_file() or environ.get("EVAL_LAB_SECRET_PRESENT", "") in {
        "1",
        "true",
        "yes",
    }
    return OperatorContext(
        state_dir=state_dir,
        now=now,
        enable_token=environ.get("EVAL_LAB_ENABLE_TOKEN", ""),
        standing_approval=environ.get("EVAL_LAB_STANDING_APPROVAL", "") or _read_text(approval_file),
        enable_identity=environ.get("EVAL_LAB_ENABLE_IDENTITY", ""),
        approval_identity=environ.get("EVAL_LAB_APPROVAL_IDENTITY", ""),
        budget_present=budget_present,
        secret_ref=secret_ref,
        secret_present=secret_present,
        policy=policy,
        agent=agent,
        drain_timeout_seconds=drain_timeout_seconds,
    )


def _verdict(
    ctx: OperatorContext,
    *,
    ok: bool,
    reason: str | None,
    detail: str,
    extra: dict[str, Any] | None = None,
) -> OperatorVerdict:
    mode = read_mode(ctx.state_dir)
    payload = {
        "ok": ok,
        "reason": reason,
        "mode": mode,
        "detail": detail,
        "running": False,
        "authorized": False,
    }
    if extra:
        payload.update(extra)
    _append_event(
        ctx.state_dir,
        {
            "at": ctx.now.isoformat(),
            "ok": ok,
            "reason": reason,
            "mode": mode,
            "detail": detail,
        },
    )
    return OperatorVerdict(ok=ok, reason=reason, mode=mode, detail=detail, payload=payload)


def _heartbeat_stale(ctx: OperatorContext) -> bool:
    path = ctx.state_dir / "heartbeat"
    if not path.is_file():
        return False
    raw = _read_text(path)
    try:
        stamped = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    limits = ctx.policy.get("operational_limits") or {}
    stale_after = limits.get("scheduler_stale_after_seconds")
    if stale_after is None:
        return False
    age = ctx.now - stamped.astimezone(UTC)
    return age > timedelta(seconds=float(stale_after))


def admission_reason(ctx: OperatorContext) -> str | None:
    has_side_inputs = bool(
        ctx.standing_approval
        or ctx.enable_identity
        or ctx.approval_identity
        or ctx.budget_present
        or ctx.secret_ref
        or ctx.secret_present
    )
    if not ctx.enable_token:
        return REASON_MISSING_ENABLE_TOKEN if has_side_inputs else REASON_DEFAULT_DISABLED
    if not ctx.standing_approval:
        return REASON_MISSING_STANDING_APPROVAL
    enable_id = ctx.enable_identity or ctx.enable_token
    approval_id = ctx.approval_identity or ctx.standing_approval
    if enable_id == approval_id:
        return REASON_SAME_IDENTITY
    if not ctx.budget_present:
        return REASON_MISSING_BUDGET
    if not ctx.secret_ref or not ctx.secret_present:
        return REASON_MISSING_SECRET
    if not policy_complete(ctx.policy):
        return REASON_DEFAULT_DISABLED
    return None


def cmd_validate(ctx: OperatorContext) -> OperatorVerdict:
    write_mode(ctx.state_dir, DEFAULT_MODE)
    reason = admission_reason(ctx)
    if reason:
        return _verdict(ctx, ok=False, reason=reason, detail="control plane remains DISABLED")
    return _verdict(
        ctx,
        ok=True,
        reason=None,
        detail="gates passed; unit remains disabled; no service started",
        extra={"gates": "passed"},
    )


def cmd_dry_run(ctx: OperatorContext) -> OperatorVerdict:
    agent = ctx.agent or "oracle"
    if agent not in CONTROL_AGENTS:
        return _verdict(ctx, ok=False, reason=REASON_BILLABLE_REFUSED, detail=f"agent {agent} is billable")
    plan = {
        "would_run": agent,
        "harbor": False,
        "dispatch": False,
        "note": "oracle/nop smoke plan only",
    }
    _write_text(ctx.state_dir / "dry-run-plan.json", json.dumps(plan, indent=2, sort_keys=True))
    return _verdict(ctx, ok=True, reason=None, detail=f"recorded {agent} smoke plan", extra=plan)


def cmd_status(ctx: OperatorContext) -> OperatorVerdict:
    heartbeat = ctx.state_dir / "heartbeat"
    health_path = ctx.state_dir / "health.json"
    if _heartbeat_stale(ctx):
        return _verdict(ctx, ok=False, reason=REASON_STALE_HEARTBEAT, detail="heartbeat older than policy")
    health: dict[str, Any] = {"docker": "unknown", "catalog": "unknown", "disk": "unknown"}
    if health_path.is_file():
        loaded = json.loads(health_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            health.update(loaded)
    extra = {
        "heartbeat": _read_text(heartbeat) or "UNKNOWN",
        "health": health,
        "readiness": "UNKNOWN" if not (ctx.state_dir / "readiness.json").is_file() else "present",
    }
    return _verdict(ctx, ok=True, reason=None, detail="status snapshot", extra=extra)


def cmd_quota(ctx: OperatorContext) -> OperatorVerdict:
    reason = admission_reason(ctx)
    if reason in {
        REASON_DEFAULT_DISABLED,
        REASON_MISSING_ENABLE_TOKEN,
        REASON_MISSING_STANDING_APPROVAL,
        REASON_MISSING_BUDGET,
        REASON_MISSING_SECRET,
        REASON_SAME_IDENTITY,
    }:
        return _verdict(ctx, ok=False, reason=reason, detail="quota refused")
    return _verdict(ctx, ok=True, reason=None, detail="quota gates present; no dispatch")


def cmd_pause(ctx: OperatorContext) -> OperatorVerdict:
    write_mode(ctx.state_dir, "PAUSED")
    return _verdict(ctx, ok=True, reason=None, detail="recorded pause; no process signalled")


def cmd_maintenance(ctx: OperatorContext) -> OperatorVerdict:
    write_mode(ctx.state_dir, "MAINTENANCE")
    return _verdict(ctx, ok=True, reason=None, detail="recorded maintenance; no process signalled")


def cmd_restart(ctx: OperatorContext) -> OperatorVerdict:
    _write_text(ctx.state_dir / "restart.json", json.dumps({"intended": "restart", "executed": False}))
    write_mode(ctx.state_dir, DEFAULT_MODE)
    return _verdict(ctx, ok=True, reason=None, detail="recorded restart intent; unit stays disabled")


def cmd_upgrade(ctx: OperatorContext) -> OperatorVerdict:
    _write_text(ctx.state_dir / "upgrade.json", json.dumps({"intended": "upgrade", "executed": False}))
    return _verdict(ctx, ok=True, reason=None, detail="recorded upgrade intent; no image pull")


def cmd_rollback(ctx: OperatorContext) -> OperatorVerdict:
    _write_text(ctx.state_dir / "rollback.json", json.dumps({"intended": "rollback", "executed": False}))
    return _verdict(ctx, ok=True, reason=None, detail="recorded rollback intent; no unit swapped")


def cmd_drain(ctx: OperatorContext) -> OperatorVerdict:
    write_mode(ctx.state_dir, "DRAINING")
    leases_path = ctx.state_dir / "inflight.json"
    inflight = []
    if leases_path.is_file():
        loaded = json.loads(leases_path.read_text(encoding="utf-8"))
        inflight = list(loaded) if isinstance(loaded, list) else []
    started_raw = _read_text(ctx.state_dir / "drain_started")
    if not started_raw:
        _write_text(ctx.state_dir / "drain_started", ctx.now.isoformat())
        started = ctx.now
    else:
        started = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
    timeout = ctx.drain_timeout_seconds
    limits = ctx.policy.get("operational_limits") or {}
    if timeout is None:
        raw_timeout = limits.get("maintenance_drain_timeout_seconds")
        timeout = float(raw_timeout) if raw_timeout is not None else None
    if inflight and timeout is not None and (ctx.now - started) > timedelta(seconds=timeout):
        drain = {"inflight": inflight, "complete": False}
        _write_text(ctx.state_dir / "drain.json", json.dumps(drain, indent=2))
        return _verdict(ctx, ok=False, reason=REASON_DRAIN_INCOMPLETE, detail="in-flight leases remain")
    if inflight:
        drain = {"inflight": inflight, "complete": False, "waiting": True}
        _write_text(ctx.state_dir / "drain.json", json.dumps(drain, indent=2))
        return _verdict(ctx, ok=True, reason=None, detail="drain waiting on in-flight leases")
    write_mode(ctx.state_dir, DEFAULT_MODE)
    drain = {"inflight": [], "complete": True}
    _write_text(ctx.state_dir / "drain.json", json.dumps(drain, indent=2))
    return _verdict(ctx, ok=True, reason=None, detail="drain complete; mode DISABLED")


def cmd_kill(ctx: OperatorContext) -> OperatorVerdict:
    write_mode(ctx.state_dir, "KILLED")
    record = {
        "disposition": KILL_DISPOSITION,
        "at": ctx.now.isoformat(),
        "executed": False,
        "note": "emergency kill recorded; no process signalled",
    }
    _write_text(ctx.state_dir / "kill.json", json.dumps(record, indent=2, sort_keys=True))
    leases_path = ctx.state_dir / "inflight.json"
    if leases_path.is_file():
        _write_text(ctx.state_dir / "inflight.json", "[]\n")
    return _verdict(ctx, ok=True, reason=None, detail=KILL_DISPOSITION, extra=record)


def cmd_rotate(ctx: OperatorContext, kind: Literal["logs", "cas"]) -> OperatorVerdict:
    record = {"kind": kind, "intended": True, "deleted": False, "root": "state-dir-only"}
    _write_text(ctx.state_dir / f"rotate-{kind}.json", json.dumps(record, indent=2))
    return _verdict(ctx, ok=True, reason=None, detail=f"recorded {kind} rotation; no production delete")


COMMANDS = {
    "validate": cmd_validate,
    "dry-run": cmd_dry_run,
    "status": cmd_status,
    "quota": cmd_quota,
    "pause": cmd_pause,
    "drain": cmd_drain,
    "restart": cmd_restart,
    "upgrade": cmd_upgrade,
    "rollback": cmd_rollback,
    "maintenance": cmd_maintenance,
    "kill": cmd_kill,
    "rotate-logs": lambda ctx: cmd_rotate(ctx, "logs"),
    "rotate-cas": lambda ctx: cmd_rotate(ctx, "cas"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="continuous-operator")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=None)
    parser.add_argument("--agent", default="oracle")
    parser.add_argument("--now", default=None)
    parser.add_argument("--drain-timeout-seconds", type=float, default=None)
    return parser


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ if environ is None else environ
    state_dir = args.state_dir or Path(env.get("EVAL_LAB_OPERATOR_STATE", "operator-state"))
    state_dir.mkdir(parents=True, exist_ok=True)
    if not (state_dir / "mode").exists():
        write_mode(state_dir, DEFAULT_MODE)
    ctx = context_from_env(
        state_dir=state_dir,
        policy_path=args.policy,
        now=_utc_now(args.now or env.get("EVAL_LAB_OPERATOR_NOW")),
        agent=args.agent,
        drain_timeout_seconds=args.drain_timeout_seconds,
        environ=env,
    )
    verdict = COMMANDS[args.command](ctx)
    sys.stdout.write(json.dumps(verdict.payload, indent=2, sort_keys=True) + "\n")
    return verdict.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
