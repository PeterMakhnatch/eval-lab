from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evallab.queue import (
    DirectoryQueue,
    Executor,
    PaidRunAuthorization,
    PolicyGate,
    authorization_required_message,
)
from evallab.quota import Headroom
from evallab.runner import RunRequest, TrialTimeoutFailure
from evallab.schemas import AutoRunRule, ExperimentSpec, StandingApprovalsPolicy

NOW = datetime(2026, 8, 19, tzinfo=UTC)


def policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[AutoRunRule(name="local-controls", agents=["oracle", "nop"])],
    )


def control(name: str, *, timeout_seconds: int = 1) -> ExperimentSpec:
    return ExperimentSpec(
        name=name,
        hypothesis="verify queue progress",
        purpose="practice",
        task="library/tasks/event-summary",
        agent="oracle",
        submitted_by="test",
        timeout_seconds=timeout_seconds,
    )


def paid(name: str, agent: str) -> ExperimentSpec:
    return ExperimentSpec(
        name=name,
        hypothesis="verify provider attribution",
        purpose="practice",
        task="canary/event-summary",
        agent=agent,
        model="provider/model",
        submitted_by="test",
        submitted_at=NOW,
        est_cost_usd=1,
    )


def test_tick_reports_each_spec_and_drains_sequentially(tmp_path: Path) -> None:
    progress: list[str] = []
    dispatched: list[str] = []

    def runner(request: RunRequest) -> Path:
        dispatched.append(request.name)
        destination = request.jobs_dir / request.name
        destination.mkdir(parents=True)
        return destination

    service = Executor(
        repo_root=tmp_path,
        queue=DirectoryQueue(tmp_path / "queue"),
        policy=policy(),
        runner=runner,
        ingester=lambda _path: None,
        credential_probe=lambda: frozenset(),
        spent_today=lambda: 0.0,
        consecutive_harness_failures=lambda: 0,
        progress=progress.append,
        sleeper=lambda _seconds: None,
    )
    for index in range(6):
        service.submit(control(f"sequential-{index}"))

    assert service.tick(parallel=1) == 6
    assert dispatched == [f"sequential-{index}" for index in range(6)]
    dispatch_lines = [line for line in progress if line.startswith("dispatching ")]
    assert len(dispatch_lines) == 6
    assert all(f"sequential-{index}" in line for index, line in enumerate(dispatch_lines))
    child_lines = [line for line in progress if line.startswith("child started ")]
    assert len(child_lines) == 6
    assert all("state: done" in line for line in progress if line.startswith("completed "))


def test_timeout_moves_spec_to_inspectable_failed_state(tmp_path: Path) -> None:
    def runner(_request: RunRequest) -> Path:
        raise TrialTimeoutFailure("fake stuck child exceeded spec timeout")

    service = Executor(
        repo_root=tmp_path,
        queue=DirectoryQueue(tmp_path / "queue"),
        policy=policy(),
        runner=runner,
        ingester=lambda _path: None,
        credential_probe=lambda: frozenset(),
        spent_today=lambda: 0.0,
        consecutive_harness_failures=lambda: 0,
        sleeper=lambda _seconds: None,
    )
    submitted, _ = service.submit(control("stuck-child"))
    spec_id = str(service.queue.load(submitted).spec_id)

    assert service.tick() == 1
    failed = service.queue.locate(spec_id, ("failed",))
    assert failed.is_file()
    assert not service.queue.list_specs("running")
    reasons = list(service.queue.reasons_dir.glob(f"{spec_id}-*.json"))
    assert any('"code": "trial_wall_clock_timeout"' in path.read_text() for path in reasons)


def test_restart_recovers_expired_running_state(tmp_path: Path) -> None:
    service = Executor(
        repo_root=tmp_path,
        queue=DirectoryQueue(tmp_path / "queue"),
        policy=policy(),
        runner=lambda request: request.jobs_dir / request.name,
        ingester=lambda _path: None,
        credential_probe=lambda: frozenset(),
        spent_today=lambda: 0.0,
        consecutive_harness_failures=lambda: 0,
        sleeper=lambda _seconds: None,
    )
    approved, _ = service.submit(control("expired-child"))
    queued = service.queue.load(approved)
    running = service.queue.transition(
        approved,
        "running",
        actor="executor",
        event="dispatch_started",
    )
    assert running.is_file()
    state = tmp_path / queued.jobs_dir / ".executor" / f"{queued.name}.state.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "status": "running",
                "started_at": (datetime.now(UTC) - timedelta(seconds=10)).isoformat(),
                "job_timeout_seconds": 1,
            }
        )
    )

    service.reconcile_running()

    failed = service.queue.locate(str(queued.spec_id), ("failed",))
    assert failed.is_file()
    assert any(
        '"code": "trial_wall_clock_timeout"' in path.read_text()
        for path in service.queue.reasons_dir.glob(f"{queued.spec_id}-*.json")
    )


def test_provider_gate_never_reuses_codex_snapshot_for_antigravity(tmp_path: Path) -> None:
    readings = {
        "codex": Headroom(
            availability="observed",
            used_percent=100,
            remaining_percent=0,
            rate_limit_reached_type="primary",
        ),
        "antigravity-cli": Headroom(
            availability="unavailable",
            reason="Antigravity exposes no measured quota snapshot",
        ),
    }
    seen: list[str] = []

    def read(agent: str) -> Headroom:
        seen.append(agent)
        return readings[agent]

    gate = PolicyGate(policy(), headroom_by_agent=read)
    codex = paid("codex-quota", "codex").model_copy(update={"spec_id": "codex-id"})
    agy = paid("agy-quota", "antigravity-cli").model_copy(update={"spec_id": "agy-id"})
    def authorization(spec: ExperimentSpec) -> PaidRunAuthorization:
        return PaidRunAuthorization(
            spec_id=str(spec.spec_id), actor="peter", authorized_at=NOW
        )

    codex_decision = gate.decide(codex, spent_today_usd=0, authorization=authorization(codex))
    agy_decision = gate.decide(agy, spent_today_usd=0, authorization=authorization(agy))

    assert codex_decision.reason_code == "subscription_quota_exhausted"
    assert agy_decision.admitted is True
    assert seen == ["codex", "antigravity-cli"]


def test_provider_messages_name_their_own_billing_state() -> None:
    antigravity = authorization_required_message(paid("agy-message", "antigravity-cli"))
    cursor = authorization_required_message(paid("cursor-message", "cursor-cli"))

    assert "Google subscription (Antigravity OAuth)" in antigravity
    assert "ChatGPT" not in antigravity
    assert "Cursor subscription/API-key policy state" in cursor
    assert "ChatGPT" not in cursor
