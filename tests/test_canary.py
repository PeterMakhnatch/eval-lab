from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab import cli
from evallab.automation import NightlyCycle
from evallab.canary import CanaryEnqueuer, load_canary_suite, task_directory_digest
from evallab.digest import DigestRenderer
from evallab.queue import DirectoryQueue, Executor
from evallab.schemas import (
    AutoRunRule,
    CanaryDriftObservation,
    CanaryMember,
    CanarySuite,
    ExperimentSpec,
    HeadlessDoctorChecks,
    HeadlessDoctorReport,
    StandingApprovalsPolicy,
)

ROOT = Path(__file__).resolve().parents[1]


def policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[
            AutoRunRule(
                name="canary",
                tasks=["canary/*"],
                agents=["codex", "claude-code"],
                max_attempts=3,
            )
        ],
    )


class StaticDoctor:
    def run(self) -> HeadlessDoctorReport:
        checks = HeadlessDoctorChecks(
            keychain_readable=True,
            codex_auth_present=True,
            docker_reachable=True,
            postgres_reachable=True,
            disk_headroom=True,
        )
        return HeadlessDoctorReport(
            checked_at=datetime.now(UTC),
            healthy=True,
            checks=checks,
        )


def make_suite(root: Path) -> CanarySuite:
    members = []
    for index in range(3):
        task = root / f"library/tasks/canary-{index}"
        task.mkdir(parents=True)
        (task / "task.toml").write_text(f'name = "test/canary-{index}"\n')
        members.append(
            CanaryMember(
                name=f"fixture-{index}",
                task_path=f"library/tasks/canary-{index}",
                task_version="1.0.0",
                task_digest=task_directory_digest(task),
                source_ref=f"test/canary-{index}@1",
                est_cost_usd=1,
            )
        )
    return CanarySuite(agents=["codex"], members=members)


def make_executor(root: Path, requests: list, ingested: list[Path]) -> Executor:
    def runner(request):
        requests.append(request)
        destination = request.jobs_dir / request.name
        destination.mkdir(parents=True)
        return destination

    return Executor(
        repo_root=root,
        queue=DirectoryQueue(root / "queue"),
        policy=policy(),
        runner=runner,
        ingester=ingested.append,
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
        credential_probe=lambda: frozenset({"codex_auth"}),
    )


def test_committed_suite_has_three_pinned_verified_members() -> None:
    suite = load_canary_suite(ROOT / "policy/canary-suite.yaml")

    assert len(suite.members) == 3
    assert suite.attempts == 3
    assert suite.agents == ["codex"]
    assert {member.name for member in suite.members} == {
        "transaction-reconciliation",
        "terminal-bench-html-js-filter",
        "event-summary",
    }
    for member in suite.members:
        assert member.source_ref.rsplit("@", 1)[1] not in {"latest", "head", "main"}
        assert task_directory_digest(ROOT / member.task_path) == member.task_digest
    terminal_bench = next(
        member for member in suite.members if member.name == "terminal-bench-html-js-filter"
    )
    assert terminal_bench.source_ref == "terminal-bench/terminal-bench@1"
    assert terminal_bench.source_task_name == "html-js-filter"


def test_canaries_are_staged_two_consecutive_nights_and_never_self_dispatch(
    tmp_path: Path,
) -> None:
    """Paid canaries are queued for Peter nightly; nothing runs without him.

    Before the authorization gate this asserted six unattended Codex
    dispatches across two nights — the defect itself.
    """
    requests: list = []
    ingested: list[Path] = []
    service = make_executor(tmp_path, requests, ingested)
    enqueuer = CanaryEnqueuer(repo_root=tmp_path, executor=service, suite=make_suite(tmp_path))
    renderer = DigestRenderer(
        repo_root=tmp_path,
        queue=service.queue,
        policy=policy(),
        trial_loader=lambda day: [],
        drift_loader=lambda day: [],
    )
    cycle = NightlyCycle(
        doctor=StaticDoctor(),  # type: ignore[arg-type]
        executor=service,
        renderer=renderer,
        committer=lambda path: True,
        canary_enqueuer=enqueuer.enqueue,
    )
    first_date = date(2026, 8, 12)

    first = cycle.run(report_date=first_date)
    second = cycle.run(report_date=first_date + timedelta(days=1))

    assert first.enqueued == second.enqueued == 3
    assert first.dispatched == second.dispatched == 0
    assert not first.quarantined and not second.quarantined
    assert requests == ingested == []
    staged = [item for _path, item in service.queue.list_specs("waiting")]
    assert len(staged) == 6
    assert {item.agent for item in staged} == {"codex"}
    assert all(item.attempts == 3 for item in staged)
    assert len({item.name for item in staged}) == 6


def test_authorizing_one_staged_canary_dispatches_exactly_that_one(tmp_path: Path) -> None:
    requests: list = []
    ingested: list[Path] = []
    service = make_executor(tmp_path, requests, ingested)
    enqueuer = CanaryEnqueuer(repo_root=tmp_path, executor=service, suite=make_suite(tmp_path))
    enqueuer.enqueue(date(2026, 8, 12))
    chosen = service.queue.list_specs("waiting")[0][1]
    service.queue.approve(str(chosen.spec_id), actor="peter")

    assert service.tick() == 1
    assert [request.name for request in requests] == [chosen.name]
    assert len(service.queue.list_specs("waiting")) == 2


def test_mutated_pinned_task_quarantines_nightly_before_dispatch(tmp_path: Path) -> None:
    requests: list = []
    ingested: list[Path] = []
    service = make_executor(tmp_path, requests, ingested)
    suite = make_suite(tmp_path)
    (tmp_path / suite.members[0].task_path / "task.toml").write_text('name = "bumped"\n')
    enqueuer = CanaryEnqueuer(repo_root=tmp_path, executor=service, suite=suite)
    cycle = NightlyCycle(
        doctor=StaticDoctor(),  # type: ignore[arg-type]
        executor=service,
        renderer=DigestRenderer(
            repo_root=tmp_path,
            queue=service.queue,
            policy=policy(),
            trial_loader=lambda day: [],
            drift_loader=lambda day: [],
        ),
        committer=lambda path: True,
        canary_enqueuer=enqueuer.enqueue,
    )

    result = cycle.run(report_date=date.today())

    assert result.enqueued == result.dispatched == 0
    assert result.quarantined is True
    assert requests == []
    content = result.digest_path.read_text()
    assert "Quarantined: yes" in content
    assert "canary_enqueue_failed:ValueError" in content


def test_nightly_cli_exits_nonzero_for_canary_enqueue_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = SimpleNamespace(
        report=StaticDoctor().run(),
        quarantined=True,
        enqueued=0,
        dispatched=0,
        digest_path=tmp_path / "digests/2026-08-13.md",
    )

    class FakeNightlyCycle:
        def __init__(self, **kwargs) -> None:
            pass

        def run(self, *, report_date: date | None = None):
            assert report_date == date(2026, 8, 13)
            return result

    executor = SimpleNamespace()
    monkeypatch.setattr(cli, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "Executor", SimpleNamespace(from_repo=lambda root: executor))
    monkeypatch.setattr(cli, "HeadlessDoctor", lambda root, executor: SimpleNamespace())
    monkeypatch.setattr(
        cli,
        "CanaryEnqueuer",
        SimpleNamespace(
            from_repo=lambda root, executor: SimpleNamespace(enqueue=lambda run_date: 0)
        ),
    )
    monkeypatch.setattr(cli, "_digest_renderer", lambda root: SimpleNamespace())
    monkeypatch.setattr(cli, "NightlyCycle", FakeNightlyCycle)

    assert cli.run_cli(["nightly", "--date", "2026-08-13"]) == 1
    assert "quarantined: yes" in capsys.readouterr().out


def test_digest_labels_version_perturbation_as_harness_drift(tmp_path: Path) -> None:
    observation = CanaryDriftObservation(
        task_name="canary/transaction-reconciliation",
        task_version="1.1.0",
        agent_name="codex",
        reward=1.0,
        attempt_count=3,
        exception_count=0,
        baseline_n=6,
        baseline_mean=1.0,
        baseline_stddev=0.0,
        previous_task_version="1.0.0",
        task_version_changed=True,
        is_harness_drift_suspect=True,
        drift_reason="task_version_changed",
    )
    renderer = DigestRenderer(
        repo_root=tmp_path,
        queue=DirectoryQueue(tmp_path / "queue"),
        policy=policy(),
        trial_loader=lambda day: [],
        drift_loader=lambda day: [observation] if day == date(2026, 8, 13) else [],
    )

    content = renderer.write(report_date=date(2026, 8, 13)).read_text()

    assert "1.000 ± 0.000" in content
    assert "harness-drift suspect (task_version_changed)" in content
    assert "harness_failure=1" in content
    assert "not capability news" in content


def test_suite_rejects_floating_source_reference(tmp_path: Path) -> None:
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.toml").write_text("task\n")

    with pytest.raises(ValueError, match="immutable revision"):
        CanaryMember(
            name="floating-task",
            task_path="task",
            task_version="1",
            task_digest=task_directory_digest(task),
            source_ref="terminal-bench/task@latest",
            est_cost_usd=1,
        )


def test_executor_rejects_mutable_dataset_download_before_harbor(
    tmp_path: Path,
) -> None:
    service = make_executor(tmp_path, [], [])

    with pytest.raises(ValueError, match="mutable"):
        service.download_dataset(
            "terminal-bench/terminal-bench@latest",
            tmp_path / "download",
        )


def test_suite_rejects_unreachable_daily_cycle_cap(tmp_path: Path) -> None:
    suite_data = make_suite(tmp_path).model_dump()
    with pytest.raises(ValueError, match="exceeds available slots"):
        CanarySuite.model_validate(suite_data | {"max_cycles_per_day": 2})


def test_billable_suite_cannot_reuse_zero_cost_control_estimates(tmp_path: Path) -> None:
    suite_data = make_suite(tmp_path).model_dump()
    suite_data["agents"] = ["oracle", "codex"]
    suite_data["members"][0]["est_cost_usd"] = 0
    with pytest.raises(ValueError, match="positive cost estimate"):
        CanarySuite.model_validate(suite_data)


def test_enqueue_due_slot_replenishment_and_cap(tmp_path: Path) -> None:
    requests: list = []
    ingested: list[Path] = []
    service = make_executor(tmp_path, requests, ingested)
    service.gate.policy = StandingApprovalsPolicy(
        daily_cost_ceiling_usd=100.0,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[
            AutoRunRule(
                name="local-controls",
                tasks=["canary/*"],
                agents=["oracle", "codex"],
                max_attempts=3,
            )
        ],
    )

    members = []
    for index in range(3):
        task = tmp_path / f"library/tasks/canary-free-{index}"
        task.mkdir(parents=True)
        (task / "task.toml").write_text(f'name = "free-{index}"\n')
        members.append(
            CanaryMember(
                name=f"free-{index}",
                task_path=f"library/tasks/canary-free-{index}",
                task_version="1.0.0",
                task_digest=task_directory_digest(task),
                source_ref=f"test/free-{index}@1",
                est_cost_usd=0,
            )
        )
    suite = CanarySuite(
        agents=["oracle"],
        members=members,
        interval_seconds=3600,
        max_cycles_per_day=2,
    )
    enqueuer = CanaryEnqueuer(repo_root=tmp_path, executor=service, suite=suite)

    base_time = datetime(2026, 9, 6, 14, 15, tzinfo=UTC)

    enqueued = enqueuer.enqueue_due(base_time)
    assert enqueued == 3
    assert len(service.queue.list_specs("approved")) == 3

    dispatched = service.tick()
    assert dispatched == 3
    assert len(requests) == 3

    enqueued_again = enqueuer.enqueue_due(base_time + timedelta(minutes=15))
    assert enqueued_again == 0
    assert service.tick() == 0

    # A late start spends the cap on actual enrollments, not missed midnight slots.
    assert enqueuer.enqueue_due(base_time + timedelta(hours=1)) == 3
    assert service.tick() == 3
    assert enqueuer.enqueue_due(base_time + timedelta(hours=2)) == 0
    assert enqueuer.enqueue_due(base_time + timedelta(days=1)) == 3


def test_enqueue_due_stop_and_unresolved_running_prevent_backlog(tmp_path: Path) -> None:
    requests: list = []
    ingested: list[Path] = []
    service = make_executor(tmp_path, requests, ingested)
    suite = make_suite(tmp_path)
    enqueuer = CanaryEnqueuer(repo_root=tmp_path, executor=service, suite=suite)

    base_time = datetime(2026, 9, 6, 0, 30, tzinfo=UTC)

    service.queue.stop_path.touch()
    assert enqueuer.enqueue_due(base_time) == 0

    service.queue.stop_path.unlink()
    running_dir = service.queue.state_dir("running")
    running_dir.mkdir(parents=True, exist_ok=True)
    fake_spec = ExperimentSpec(
        name="running-placeholder",
        hypothesis="test",
        purpose="drift",
        task="canary/placeholder",
        agent="oracle",
        submitted_by="test",
    )
    service.queue._create_exclusive(running_dir / "test-spec.json", fake_spec)
    assert enqueuer.enqueue_due(base_time) == 0

    (running_dir / "test-spec.json").unlink()
    later_time = datetime(2026, 9, 6, 0, 45, tzinfo=UTC)
    enqueued = enqueuer.enqueue_due(later_time)
    assert enqueued == 3


def test_enqueue_due_paid_canaries_stage_in_waiting_without_authorization(tmp_path: Path) -> None:
    requests: list = []
    ingested: list[Path] = []
    service = make_executor(tmp_path, requests, ingested)
    suite = make_suite(tmp_path)
    enqueuer = CanaryEnqueuer(repo_root=tmp_path, executor=service, suite=suite)

    base_time = datetime(2026, 9, 6, 0, 30, tzinfo=UTC)
    enqueued = enqueuer.enqueue_due(base_time)
    assert enqueued == 3

    waiting = service.queue.list_specs("waiting")
    assert len(waiting) == 3
    assert len(service.queue.list_specs("approved")) == 0

    assert service.tick() == 0
    assert requests == []


def test_enqueue_due_concurrent_producer_lock_serialization(tmp_path: Path) -> None:
    requests: list = []
    ingested: list[Path] = []
    service = make_executor(tmp_path, requests, ingested)
    suite = make_suite(tmp_path)
    enqueuer = CanaryEnqueuer(repo_root=tmp_path, executor=service, suite=suite)

    base_time = datetime(2026, 9, 6, 0, 30, tzinfo=UTC)

    with service.queue.tick_lock() as acquired:
        assert acquired is True
        assert enqueuer.enqueue_due(base_time) == 0


def test_enqueue_due_skips_task_hashing_when_already_enrolled(tmp_path: Path) -> None:
    requests: list = []
    ingested: list[Path] = []
    service = make_executor(tmp_path, requests, ingested)
    suite = make_suite(tmp_path)
    enqueuer = CanaryEnqueuer(repo_root=tmp_path, executor=service, suite=suite)

    base_time = datetime(2026, 9, 6, 0, 30, tzinfo=UTC)
    assert enqueuer.enqueue_due(base_time) == 3

    (tmp_path / suite.members[0].task_path / "task.toml").unlink()
    assert enqueuer.enqueue_due(base_time) == 0
