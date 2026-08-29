"""Focused contracts for the disabled continuous-loop operator adapters."""

from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

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
    main,
    make_signed_manifest,
    policy_complete,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ops/continuous-operator"
PLIST = ROOT / "scripts/ops/launchd/com.petermakhnatch.evallab.continuous-operator.plist"
SERVICE = ROOT / "scripts/ops/systemd/evallab-continuous-operator.service"
TIMER = ROOT / "scripts/ops/systemd/evallab-continuous-operator.timer"
COMPOSE = ROOT / "containers/continuous-operator/compose.yaml"
DOCKERFILE = ROOT / "containers/continuous-operator/Dockerfile"
EXAMPLE_POLICY = ROOT / "policy/continuous-loop-policy.example.yaml"
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


def _complete_policy_body(*, stale_after: float = 60.0, drain_timeout: float = 5.0) -> dict:
    payload = {"scope": ["continuous-loop"]}
    approval = make_signed_manifest(
        authority_kind="campaign_approval",
        signer_identity=APPROVAL,
        payload=payload,
        expires_at=NOW + timedelta(hours=1),
    )
    return {
        "continuous_loop_policy": {
            "policy_schema_version": "1",
            "approval_signature_ref": APPROVAL,
            "approval_digest": approval["payload_digest"],
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
    }


def _policy(path: Path, *, stale_after: float = 60.0, drain_timeout: float = 5.0) -> Path:
    path.write_text(yaml.safe_dump(_complete_policy_body(stale_after=stale_after, drain_timeout=drain_timeout)))
    return path


def _write_manifests(state: Path, *, approval_signer: str = APPROVAL, budget_signer: str = BUDGET) -> None:
    state.mkdir(exist_ok=True)
    (state / "approval.json").write_text(
        json.dumps(
            make_signed_manifest(
                authority_kind="campaign_approval",
                signer_identity=approval_signer,
                payload={"scope": ["continuous-loop"]},
                expires_at=NOW + timedelta(hours=1),
            )
        )
    )
    (state / "budget.json").write_text(
        json.dumps(
            make_signed_manifest(
                authority_kind="budget",
                signer_identity=budget_signer,
                payload={"scope": ["continuous-loop"], "ceiling_usd": 0},
                expires_at=NOW + timedelta(hours=1),
            )
        )
    )


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
    now: str | None = None,
) -> subprocess.CompletedProcess[str]:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    argv = ["uv", "run", "python", "-m", "evallab.ops_continuous", command, "--state-dir", str(state)]
    if policy is not None:
        argv.extend(["--policy", str(policy)])
    if agent is not None:
        argv.extend(["--agent", agent])
    if now is not None:
        argv.extend(["--now", now])
    if extra:
        argv.extend(extra)
    merged = {**os.environ, **(env or {})}
    return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, env=merged)


def _payload(result: subprocess.CompletedProcess[str]) -> dict:
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


def test_unsigned_approval_digest_mismatch(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_manifests(state)
    tampered = json.loads((state / "approval.json").read_text())
    tampered["payload_digest"] = "0" * 64
    (state / "approval.json").write_text(json.dumps(tampered))
    result = _run(tmp_path, "validate", env=_gate_env(), now=NOW.isoformat())
    assert _payload(result)["reason"] == REASON_MISSING_STANDING_APPROVAL


def test_expired_approval_refused(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "approval.json").write_text(
        json.dumps(
            make_signed_manifest(
                authority_kind="campaign_approval",
                signer_identity=APPROVAL,
                payload={"scope": ["continuous-loop"]},
                expires_at=NOW - timedelta(seconds=1),
            )
        )
    )
    result = _run(tmp_path, "validate", env=_gate_env(), now=NOW.isoformat())
    assert _payload(result)["reason"] == REASON_MISSING_STANDING_APPROVAL


def test_missing_budget(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "approval.json").write_text(
        json.dumps(
            make_signed_manifest(
                authority_kind="campaign_approval",
                signer_identity=APPROVAL,
                payload={"scope": ["continuous-loop"]},
                expires_at=NOW + timedelta(hours=1),
            )
        )
    )
    result = _run(tmp_path, "validate", env=_gate_env(), now=NOW.isoformat())
    assert _payload(result)["reason"] == REASON_MISSING_BUDGET


def test_env_budget_flag_is_not_authority(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "approval.json").write_text(
        json.dumps(
            make_signed_manifest(
                authority_kind="campaign_approval",
                signer_identity=APPROVAL,
                payload={"scope": ["continuous-loop"]},
                expires_at=NOW + timedelta(hours=1),
            )
        )
    )
    env = {**_gate_env(), "EVAL_LAB_BUDGET_PRESENT": "1"}
    result = _run(tmp_path, "quota", env=env, now=NOW.isoformat())
    assert _payload(result)["reason"] == REASON_MISSING_BUDGET


def test_missing_secret(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_manifests(state)
    env = {
        "EVAL_LAB_ENABLE_TOKEN": "enable-1",
        "EVAL_LAB_ENABLE_IDENTITY": ENABLE,
        "EVAL_LAB_SECRET_REF": "keychain:lab/operator",
    }
    result = _run(tmp_path, "quota", env=env, now=NOW.isoformat())
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_MISSING_SECRET


def test_same_enable_and_approval_identity(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_manifests(state, approval_signer=ENABLE)
    result = _run(tmp_path, "validate", env=_gate_env(), now=NOW.isoformat())
    assert _payload(result)["reason"] == REASON_SAME_IDENTITY


def test_budget_signer_must_be_distinct(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_manifests(state, budget_signer=APPROVAL)
    result = _run(tmp_path, "validate", env=_gate_env(), now=NOW.isoformat())
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
    assert body["would_run"] == "oracle"
    assert body["harbor"] is False
    plan = json.loads((tmp_path / "state/dry-run-plan.json").read_text())
    assert plan["dispatch"] is False


def test_stale_heartbeat_on_status(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml", stale_after=30)
    state = tmp_path / "state"
    state.mkdir()
    (state / "heartbeat").write_text("2026-08-28T00:00:00+00:00\n")
    result = _run(tmp_path, "status", policy=policy, now="2026-08-28T00:05:00+00:00")
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_STALE_HEARTBEAT


def test_stale_heartbeat_on_validate_and_quota(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml", stale_after=30)
    state = tmp_path / "state"
    _write_manifests(state)
    (state / "heartbeat").write_text("2026-08-28T00:00:00+00:00\n")
    env = _gate_env()
    validate = _run(tmp_path, "validate", policy=policy, env=env, now="2026-08-28T00:05:00+00:00")
    quota = _run(tmp_path, "quota", policy=policy, env=env, now="2026-08-28T00:05:00+00:00")
    start = _run(tmp_path, "start", policy=policy, env=env, now="2026-08-28T00:05:00+00:00")
    assert _payload(validate)["reason"] == REASON_STALE_HEARTBEAT
    assert _payload(quota)["reason"] == REASON_STALE_HEARTBEAT
    assert _payload(start)["reason"] == REASON_STALE_HEARTBEAT


def test_fresh_heartbeat_status_unknown_health(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml", stale_after=300)
    state = tmp_path / "state"
    state.mkdir()
    (state / "heartbeat").write_text("2026-08-28T00:04:00+00:00\n")
    result = _run(tmp_path, "status", policy=policy, now="2026-08-28T00:05:00+00:00")
    assert result.returncode == 0
    body = _payload(result)
    assert body["health"]["docker"] == "unknown"
    assert body["running"] is False


def test_graceful_drain_then_timeout(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml", drain_timeout=10)
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps(["lease-1"]))
    waiting = _run(tmp_path, "drain", policy=policy, now="2026-08-28T00:00:00+00:00")
    assert waiting.returncode == 2
    assert _payload(waiting)["reason"] == REASON_DRAIN_INCOMPLETE
    assert json.loads((state / "drain.json").read_text())["complete"] is False
    timed = _run(tmp_path, "drain", policy=policy, now="2026-08-28T00:00:11+00:00")
    assert timed.returncode == 2
    assert _payload(timed)["reason"] == REASON_DRAIN_INCOMPLETE


def test_kill_records_operator_kill(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps(["lease-1"]))
    result = _run(tmp_path, "kill", now="2026-08-28T00:00:00+00:00")
    assert result.returncode == 0
    record = json.loads((state / "kill.json").read_text())
    assert record["disposition"] == KILL_DISPOSITION
    assert record["executed"] is False
    assert record["signalled"] is False
    assert json.loads((state / "inflight.json").read_text()) == ["lease-1"]
    assert (state / "mode").read_text().strip() == "KILLED"
    drain = _run(tmp_path, "drain", now="2026-08-28T00:00:01+00:00")
    assert drain.returncode == 2
    assert _payload(drain)["reason"] == REASON_DRAIN_INCOMPLETE
    assert json.loads((state / "inflight.json").read_text()) == ["lease-1"]


def test_validate_does_not_clear_killed_or_draining(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps(["lease-1"]))
    kill = _run(tmp_path, "kill", now=NOW.isoformat())
    assert _payload(kill)["mode"] == "KILLED"
    validated = _run(tmp_path, "validate", now=NOW.isoformat())
    assert (state / "mode").read_text().strip() == "KILLED"
    assert _payload(validated)["mode"] == "KILLED"
    (state / "mode").write_text("DRAINING\n")
    again = _run(tmp_path, "validate", now=NOW.isoformat())
    assert (state / "mode").read_text().strip() == "DRAINING"
    assert _payload(again)["mode"] == "DRAINING"


def test_malformed_inflight_fails_closed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps({"lease": 1}))
    result = _run(tmp_path, "drain", now=NOW.isoformat())
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_DRAIN_INCOMPLETE
    assert (state / "mode").read_text().strip() == "DRAINING"


def test_drain_without_timeout_still_incomplete(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps(["lease-1"]))
    result = _run(tmp_path, "drain", now=NOW.isoformat())
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_DRAIN_INCOMPLETE


def test_empty_inflight_drain_disables(tmp_path: Path) -> None:
    result = _run(tmp_path, "drain")
    assert result.returncode == 0
    assert _payload(result)["mode"] == "DISABLED"


def test_full_gates_still_non_running(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_manifests(state)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    result = _run(tmp_path, "validate", policy=policy, env=_gate_env(), now=NOW.isoformat())
    body = _payload(result)
    assert result.returncode == 0
    assert body["mode"] == "DISABLED"
    assert body["running"] is False
    assert body["authorized"] is False
    start = _run(tmp_path, "start", policy=policy, env=_gate_env(), now=NOW.isoformat())
    assert start.returncode == 2
    assert _payload(start)["reason"] == REASON_DEFAULT_DISABLED


def test_launchd_plist_disabled() -> None:
    loaded = plistlib.loads(PLIST.read_bytes())
    assert loaded["Disabled"] is True
    assert loaded["RunAtLoad"] is False
    assert "KeepAlive" not in loaded
    assert loaded["Label"] == "com.petermakhnatch.evallab.continuous-operator"
    assert loaded["ProgramArguments"][0] == "/opt/evallab/.venv/bin/python"
    assert "/usr/bin/env" not in loaded["ProgramArguments"]
    assert loaded["LimitLoadToSessionType"] == "Aqua"
    assert loaded["StandardOutPath"].startswith("~/Library/Logs/evallab/")
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
    assert "ExecStart=/opt/evallab/.venv/bin/python" in service
    assert "/usr/bin/env" not in service
    assert "StateDirectoryMode=0700" in service


def test_compose_restart_no() -> None:
    compose = COMPOSE.read_text()
    dockerfile = DOCKERFILE.read_text()
    assert 'restart: "no"' in compose
    assert "profiles:" in compose
    assert "command:" not in compose
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
