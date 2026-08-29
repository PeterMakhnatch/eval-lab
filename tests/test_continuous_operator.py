"""Focused contracts for the disabled continuous-loop operator adapters."""

from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
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
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ops/continuous-operator"
PLIST = ROOT / "scripts/ops/launchd/com.petermakhnatch.evallab.continuous-operator.plist"
SERVICE = ROOT / "scripts/ops/systemd/evallab-continuous-operator.service"
TIMER = ROOT / "scripts/ops/systemd/evallab-continuous-operator.timer"
COMPOSE = ROOT / "containers/continuous-operator/compose.yaml"
EXAMPLE_POLICY = ROOT / "policy/continuous-loop-policy.example.yaml"
SECRET_SCAN_ROOTS = (
    ROOT / "scripts/ops",
    ROOT / "containers/continuous-operator",
    ROOT / "policy/continuous-loop-policy.example.yaml",
    ROOT / "src/evallab/ops_continuous.py",
    ROOT / "docs/continuous-loop-operator.md",
)


def _policy(path: Path, *, stale_after: float = 60.0, drain_timeout: float = 5.0) -> Path:
    body = {
        "continuous_loop_policy": {
            "policy_schema_version": "1",
            "approval_signature_ref": "approval-key-b",
            "approval_digest": "a" * 64,
            "slo_freshness": {"status_snapshot_max_age_seconds": 30},
            "operational_limits": {
                "scheduler_stale_after_seconds": stale_after,
                "maintenance_drain_timeout_seconds": drain_timeout,
            },
            "quality_and_quarantine": {"auto_acceptance_enabled": False},
        }
    }
    path.write_text(yaml.safe_dump(body))
    return path


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
    result = _run(tmp_path, "validate", env={"EVAL_LAB_STANDING_APPROVAL": "stand-1"})
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_MISSING_ENABLE_TOKEN


def test_missing_standing_approval(tmp_path: Path) -> None:
    result = _run(tmp_path, "validate", env={"EVAL_LAB_ENABLE_TOKEN": "enable-1"})
    assert _payload(result)["reason"] == REASON_MISSING_STANDING_APPROVAL


def test_missing_budget(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "validate",
        env={
            "EVAL_LAB_ENABLE_TOKEN": "enable-1",
            "EVAL_LAB_STANDING_APPROVAL": "stand-1",
            "EVAL_LAB_ENABLE_IDENTITY": "enable-key",
            "EVAL_LAB_APPROVAL_IDENTITY": "approval-key",
        },
    )
    assert _payload(result)["reason"] == REASON_MISSING_BUDGET


def test_missing_secret(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "quota",
        env={
            "EVAL_LAB_ENABLE_TOKEN": "enable-1",
            "EVAL_LAB_STANDING_APPROVAL": "stand-1",
            "EVAL_LAB_ENABLE_IDENTITY": "enable-key",
            "EVAL_LAB_APPROVAL_IDENTITY": "approval-key",
            "EVAL_LAB_BUDGET_PRESENT": "1",
            "EVAL_LAB_SECRET_REF": "keychain:lab/operator",
        },
    )
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_MISSING_SECRET


def test_same_enable_and_approval_identity(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "validate",
        env={
            "EVAL_LAB_ENABLE_TOKEN": "tok",
            "EVAL_LAB_STANDING_APPROVAL": "stand-1",
            "EVAL_LAB_ENABLE_IDENTITY": "same-key",
            "EVAL_LAB_APPROVAL_IDENTITY": "same-key",
            "EVAL_LAB_BUDGET_PRESENT": "1",
            "EVAL_LAB_SECRET_REF": "keychain:lab/operator",
            "EVAL_LAB_SECRET_PRESENT": "1",
        },
    )
    assert _payload(result)["reason"] == REASON_SAME_IDENTITY


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


def test_stale_heartbeat(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml", stale_after=30)
    state = tmp_path / "state"
    state.mkdir()
    (state / "heartbeat").write_text("2026-08-28T00:00:00+00:00\n")
    result = _run(
        tmp_path,
        "status",
        policy=policy,
        now="2026-08-28T00:05:00+00:00",
    )
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_STALE_HEARTBEAT


def test_fresh_heartbeat_status_unknown_health(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml", stale_after=300)
    state = tmp_path / "state"
    state.mkdir()
    (state / "heartbeat").write_text("2026-08-28T00:04:00+00:00\n")
    result = _run(
        tmp_path,
        "status",
        policy=policy,
        now="2026-08-28T00:05:00+00:00",
    )
    assert result.returncode == 0
    body = _payload(result)
    assert body["health"]["docker"] == "unknown"
    assert body["running"] is False


def test_graceful_drain_then_timeout(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml", drain_timeout=10)
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps(["lease-1"]))
    waiting = _run(
        tmp_path,
        "drain",
        policy=policy,
        now="2026-08-28T00:00:00+00:00",
    )
    assert waiting.returncode == 0
    assert json.loads((state / "drain.json").read_text())["complete"] is False
    timed = _run(
        tmp_path,
        "drain",
        policy=policy,
        now="2026-08-28T00:00:11+00:00",
    )
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
    assert (state / "mode").read_text().strip() == "KILLED"


def test_empty_inflight_drain_disables(tmp_path: Path) -> None:
    result = _run(tmp_path, "drain")
    assert result.returncode == 0
    assert _payload(result)["mode"] == "DISABLED"


def test_full_gates_still_non_running(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    result = _run(
        tmp_path,
        "validate",
        policy=policy,
        env={
            "EVAL_LAB_ENABLE_TOKEN": "enable-1",
            "EVAL_LAB_STANDING_APPROVAL": "stand-1",
            "EVAL_LAB_ENABLE_IDENTITY": "enable-key",
            "EVAL_LAB_APPROVAL_IDENTITY": "approval-key",
            "EVAL_LAB_BUDGET_PRESENT": "1",
            "EVAL_LAB_SECRET_REF": "keychain:lab/operator",
            "EVAL_LAB_SECRET_PRESENT": "1",
        },
    )
    body = _payload(result)
    assert result.returncode == 0
    assert body["mode"] == "DISABLED"
    assert body["running"] is False
    assert body["authorized"] is False


def test_launchd_plist_disabled() -> None:
    loaded = plistlib.loads(PLIST.read_bytes())
    assert loaded["Disabled"] is True
    assert loaded["RunAtLoad"] is False
    assert "KeepAlive" not in loaded
    assert loaded["Label"] == "com.petermakhnatch.evallab.continuous-operator"


def test_systemd_units_not_wanted() -> None:
    service = SERVICE.read_text()
    timer = TIMER.read_text()
    assert "WantedBy=" not in service
    assert "WantedBy=" not in timer
    assert "Restart=no" in service
    assert "Persistent=false" in timer


def test_compose_restart_no() -> None:
    assert 'restart: "no"' in COMPOSE.read_text()
    assert "profiles:" in COMPOSE.read_text()


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
