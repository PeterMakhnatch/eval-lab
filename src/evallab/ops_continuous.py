"""Disabled-by-default continuous-loop operator validation (Ops/Runner adapters).

Not the Platform control-plane daemon. Never starts launchd, systemd, Docker,
Harbor, or billable runs. Writes only to an explicit operator state directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError, field_validator, model_validator

from evallab.schemas import ContractModel

CONTROL_AGENTS = frozenset({"oracle", "nop"})
MODES = frozenset({"DISABLED", "PAUSED", "RUNNING", "DRAINING", "MAINTENANCE", "KILLED"})
KILL_DISPOSITION = "FAILED_OPERATOR_KILL"
DEFAULT_MODE = "DISABLED"
REQUIRED_SCOPE = "continuous-loop"
SHA256_HEX = 64
STATE_DIR_MODE = 0o700
STATE_FILE_MODE = 0o600

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

AuthorityKind = Literal["campaign_approval", "budget"]


class SloFreshnessPolicy(ContractModel):
    max_queue_admission_lag_seconds: float
    max_dispatch_latency_seconds: float
    max_oldest_postrun_lag_seconds: float
    max_oldest_catalog_settle_lag_seconds: float
    max_oldest_quality_lag_seconds: float
    max_oldest_projection_lag_seconds: float
    max_oldest_analysis_lag_seconds: float
    status_snapshot_max_age_seconds: float

    @field_validator(
        "max_queue_admission_lag_seconds",
        "max_dispatch_latency_seconds",
        "max_oldest_postrun_lag_seconds",
        "max_oldest_catalog_settle_lag_seconds",
        "max_oldest_quality_lag_seconds",
        "max_oldest_projection_lag_seconds",
        "max_oldest_analysis_lag_seconds",
        "status_snapshot_max_age_seconds",
    )
    @classmethod
    def _positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("SLO fields must be positive")
        return value


class OperationalLimitsPolicy(ContractModel):
    scheduler_heartbeat_interval_seconds: float
    scheduler_stale_after_seconds: float
    worker_heartbeat_interval_seconds: float
    lease_ttl_seconds: float
    fencing_grace_seconds: float
    max_concurrent_workers: int
    postrun_hook_timeout_seconds: float
    maintenance_drain_timeout_seconds: float
    maintenance_disk_threshold_bytes: int

    @field_validator(
        "scheduler_heartbeat_interval_seconds",
        "scheduler_stale_after_seconds",
        "worker_heartbeat_interval_seconds",
        "lease_ttl_seconds",
        "fencing_grace_seconds",
        "postrun_hook_timeout_seconds",
        "maintenance_drain_timeout_seconds",
        "maintenance_disk_threshold_bytes",
    )
    @classmethod
    def _positive_float(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("operational limit must be positive")
        return value

    @field_validator("max_concurrent_workers")
    @classmethod
    def _workers(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_concurrent_workers must be >= 1")
        return value


class QualityAndQuarantinePolicy(ContractModel):
    quarantine_rolling_window_size: int
    min_window_attempts_for_calculation: int
    max_quarantine_fraction: float
    max_warn_fraction: float
    catalog_ingestion_warn_after_seconds: float
    catalog_ingestion_pause_after_seconds: float
    max_consecutive_quiet_failures: int
    auto_acceptance_enabled: bool

    @model_validator(mode="after")
    def _safe(self) -> QualityAndQuarantinePolicy:
        if self.auto_acceptance_enabled:
            raise ValueError("auto_acceptance_enabled must be false")
        if not 0 <= self.max_quarantine_fraction <= 1:
            raise ValueError("max_quarantine_fraction must be in [0, 1]")
        if not 0 <= self.max_warn_fraction <= 1:
            raise ValueError("max_warn_fraction must be in [0, 1]")
        if self.quarantine_rolling_window_size < 1:
            raise ValueError("quarantine_rolling_window_size must be >= 1")
        if self.min_window_attempts_for_calculation < 1:
            raise ValueError("min_window_attempts_for_calculation must be >= 1")
        if self.max_consecutive_quiet_failures < 1:
            raise ValueError("max_consecutive_quiet_failures must be >= 1")
        if self.catalog_ingestion_warn_after_seconds <= 0:
            raise ValueError("catalog_ingestion_warn_after_seconds must be positive")
        if self.catalog_ingestion_pause_after_seconds <= 0:
            raise ValueError("catalog_ingestion_pause_after_seconds must be positive")
        return self


class ContinuousLoopPolicy(ContractModel):
    policy_schema_version: str
    approval_signature_ref: str
    approval_digest: str
    slo_freshness: SloFreshnessPolicy
    operational_limits: OperationalLimitsPolicy
    quality_and_quarantine: QualityAndQuarantinePolicy

    @field_validator("policy_schema_version", "approval_signature_ref")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("required string empty")
        return value

    @field_validator("approval_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if len(value) != SHA256_HEX or any(ch not in "0123456789abcdef" for ch in value.lower()):
            raise ValueError("approval_digest must be sha256 hex")
        return value.lower()


class SignedManifest(ContractModel):
    schema_version: str
    authority_kind: AuthorityKind
    signer_identity: str
    payload: dict[str, Any]
    payload_digest: str
    signature: str
    expires_at: datetime
    scope: list[str]

    @field_validator("signer_identity")
    @classmethod
    def _signer(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("signer_identity required")
        return value

    @field_validator("payload_digest", "signature")
    @classmethod
    def _hex(cls, value: str) -> str:
        if len(value) != SHA256_HEX or any(ch not in "0123456789abcdef" for ch in value.lower()):
            raise ValueError("digest/signature must be sha256 hex")
        return value.lower()

    @field_validator("scope")
    @classmethod
    def _scope(cls, value: list[str]) -> list[str]:
        if REQUIRED_SCOPE not in value:
            raise ValueError(f"scope must include {REQUIRED_SCOPE}")
        return value


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
    enable_identity: str
    approval: SignedManifest | None
    budget: SignedManifest | None
    secret_ref: str
    secret_present: bool
    policy: ContinuousLoopPolicy | None
    agent: str
    drain_timeout_seconds: float | None
    env_self_asserted_approval: bool
    env_self_asserted_budget: bool


def _utc_now(raw: str | None) -> datetime:
    if raw:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return datetime.now(UTC)


def canonical_payload(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str)


def payload_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_payload(payload).encode("utf-8")).hexdigest()


def bound_signature(*, signer_identity: str, digest: str, authority_kind: str) -> str:
    material = f"{authority_kind}\n{signer_identity}\n{digest}".encode()
    return hashlib.sha256(material).hexdigest()


def make_signed_manifest(
    *,
    authority_kind: AuthorityKind,
    signer_identity: str,
    payload: Mapping[str, Any],
    expires_at: datetime,
    scope: list[str] | None = None,
) -> dict[str, Any]:
    digest = payload_digest(payload)
    return {
        "schema_version": "1",
        "authority_kind": authority_kind,
        "signer_identity": signer_identity,
        "payload": dict(payload),
        "payload_digest": digest,
        "signature": bound_signature(
            signer_identity=signer_identity, digest=digest, authority_kind=authority_kind
        ),
        "expires_at": expires_at.isoformat(),
        "scope": scope or [REQUIRED_SCOPE],
    }


def verify_signed_manifest(
    raw: Mapping[str, Any] | None,
    *,
    expected_kind: AuthorityKind,
    now: datetime,
) -> SignedManifest | None:
    if not isinstance(raw, dict):
        return None
    try:
        manifest = SignedManifest.model_validate(raw)
    except ValidationError:
        return None
    if manifest.authority_kind != expected_kind:
        return None
    expires = manifest.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires <= now:
        return None
    digest = payload_digest(manifest.payload)
    if digest != manifest.payload_digest:
        return None
    expected = bound_signature(
        signer_identity=manifest.signer_identity,
        digest=digest,
        authority_kind=manifest.authority_kind,
    )
    if expected != manifest.signature:
        return None
    return manifest


def load_loop_policy(path: Path | None) -> ContinuousLoopPolicy | None:
    if path is None or not path.is_file():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return None
    body = raw.get("continuous_loop_policy", raw)
    if not isinstance(body, dict):
        return None
    try:
        return ContinuousLoopPolicy.model_validate(body)
    except ValidationError:
        return None


def policy_complete(policy: Mapping[str, Any] | ContinuousLoopPolicy | None) -> bool:
    if isinstance(policy, ContinuousLoopPolicy):
        return True
    if not isinstance(policy, dict):
        return False
    try:
        ContinuousLoopPolicy.model_validate(policy)
    except ValidationError:
        return False
    return True


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _load_json_mapping(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _secure_state_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, STATE_DIR_MODE)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    os.chmod(path, STATE_FILE_MODE)


def _append_event(state_dir: Path, event: Mapping[str, Any]) -> None:
    path = state_dir / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(event), sort_keys=True) + "\n")
    os.chmod(path, STATE_FILE_MODE)


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
    approval_path = (
        Path(environ["EVAL_LAB_APPROVAL_MANIFEST"])
        if environ.get("EVAL_LAB_APPROVAL_MANIFEST")
        else state_dir / "approval.json"
    )
    budget_path = (
        Path(environ["EVAL_LAB_BUDGET_MANIFEST"])
        if environ.get("EVAL_LAB_BUDGET_MANIFEST")
        else state_dir / "budget.json"
    )
    secret_probe = state_dir / "secret_present"
    policy = load_loop_policy(policy_path)
    env_self_asserted_approval = bool(environ.get("EVAL_LAB_STANDING_APPROVAL"))
    env_self_asserted_budget = environ.get("EVAL_LAB_BUDGET_PRESENT", "") in {"1", "true", "yes"}
    approval = verify_signed_manifest(
        _load_json_mapping(approval_path), expected_kind="campaign_approval", now=now
    )
    budget = verify_signed_manifest(_load_json_mapping(budget_path), expected_kind="budget", now=now)
    secret_ref = environ.get("EVAL_LAB_SECRET_REF", "")
    secret_present = secret_probe.is_file() or environ.get("EVAL_LAB_SECRET_PRESENT", "") in {
        "1",
        "true",
        "yes",
    }
    enable_token = environ.get("EVAL_LAB_ENABLE_TOKEN", "")
    enable_identity = environ.get("EVAL_LAB_ENABLE_IDENTITY", "") or enable_token
    return OperatorContext(
        state_dir=state_dir,
        now=now,
        enable_token=enable_token,
        enable_identity=enable_identity,
        approval=approval,
        budget=budget,
        secret_ref=secret_ref,
        secret_present=secret_present,
        policy=policy,
        agent=agent,
        drain_timeout_seconds=drain_timeout_seconds,
        env_self_asserted_approval=env_self_asserted_approval,
        env_self_asserted_budget=env_self_asserted_budget,
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
    limits = ctx.policy.operational_limits if ctx.policy is not None else None
    stale_after = None if limits is None else limits.scheduler_stale_after_seconds
    path = ctx.state_dir / "heartbeat"
    if stale_after is None:
        return False
    if not path.is_file():
        return True
    raw = _read_text(path)
    try:
        stamped = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    age = ctx.now - stamped.astimezone(UTC)
    return age > timedelta(seconds=float(stale_after))


def admission_reason(ctx: OperatorContext) -> str | None:
    has_side_inputs = bool(
        ctx.approval
        or ctx.budget
        or ctx.enable_identity
        or ctx.secret_ref
        or ctx.secret_present
        or ctx.env_self_asserted_approval
        or ctx.env_self_asserted_budget
        or ctx.policy is not None
    )
    if not ctx.enable_token:
        return REASON_MISSING_ENABLE_TOKEN if has_side_inputs else REASON_DEFAULT_DISABLED
    if ctx.env_self_asserted_approval and ctx.approval is None:
        return REASON_MISSING_STANDING_APPROVAL
    if ctx.approval is None:
        return REASON_MISSING_STANDING_APPROVAL
    if ctx.enable_identity == ctx.approval.signer_identity:
        return REASON_SAME_IDENTITY
    if ctx.env_self_asserted_budget and ctx.budget is None:
        return REASON_MISSING_BUDGET
    if ctx.budget is None:
        return REASON_MISSING_BUDGET
    if ctx.budget.signer_identity in {ctx.enable_identity, ctx.approval.signer_identity}:
        return REASON_SAME_IDENTITY
    if not ctx.secret_ref or not ctx.secret_present:
        return REASON_MISSING_SECRET
    if ctx.policy is None:
        return REASON_DEFAULT_DISABLED
    if ctx.policy.approval_signature_ref != ctx.approval.signer_identity:
        return REASON_MISSING_STANDING_APPROVAL
    if ctx.policy.approval_digest != ctx.approval.payload_digest:
        return REASON_MISSING_STANDING_APPROVAL
    if _heartbeat_stale(ctx):
        return REASON_STALE_HEARTBEAT
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


def cmd_start(ctx: OperatorContext) -> OperatorVerdict:
    write_mode(ctx.state_dir, DEFAULT_MODE)
    reason = admission_reason(ctx)
    if reason:
        return _verdict(ctx, ok=False, reason=reason, detail="start refused; remains DISABLED")
    return _verdict(
        ctx,
        ok=False,
        reason=REASON_DEFAULT_DISABLED,
        detail="start refused; units stay disabled; no service started",
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
    if reason:
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
    if timeout is None and ctx.policy is not None:
        timeout = float(ctx.policy.operational_limits.maintenance_drain_timeout_seconds)
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
    "start": cmd_start,
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
    _secure_state_dir(state_dir)
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
