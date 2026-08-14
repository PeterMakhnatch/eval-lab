from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from harbor_lab.automation import NightlyCycle
from harbor_lab.canary import CanaryEnqueuer, load_canary_suite, task_directory_digest
from harbor_lab.digest import CanaryDriftObservation, DigestRenderer
from harbor_lab.queue import DirectoryQueue, Executor
from harbor_lab.schemas import (
    AutoRunRule,
    CanaryMember,
    CanarySuite,
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
        task = root / f"tasks/canary-{index}"
        task.mkdir(parents=True)
        (task / "task.toml").write_text(f'name = "test/canary-{index}"\n')
        members.append(
            CanaryMember(
                name=f"fixture-{index}",
                task_path=f"tasks/canary-{index}",
                task_version="1.0.0",
                task_digest=task_directory_digest(task),
                source_ref=f"test/canary-{index}@1",
                est_cost_usd=1,
            )
        )
    return CanarySuite(agents=["codex", "claude-code"], members=members)


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
    )


def test_committed_suite_has_three_pinned_verified_members() -> None:
    suite = load_canary_suite(ROOT / "policy/canary-suite.yaml")

    assert len(suite.members) == 3
    assert suite.attempts == 3
    assert suite.agents == ["codex", "claude-code"]
    assert {member.name for member in suite.members} == {
        "transaction-reconciliation",
        "terminal-bench-query-optimize",
        "event-summary",
    }
    for member in suite.members:
        assert member.source_ref.rsplit("@", 1)[1] not in {"latest", "head", "main"}
        assert task_directory_digest(ROOT / member.task_path) == member.task_digest
    terminal_bench = next(
        member for member in suite.members if member.name == "terminal-bench-query-optimize"
    )
    assert terminal_bench.source_ref == "terminal-bench/query-optimize@4"
    assert terminal_bench.source_content_hash is not None


def test_canaries_run_two_consecutive_nights_with_three_attempts(tmp_path: Path) -> None:
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

    assert first.enqueued == first.dispatched == 6
    assert second.enqueued == second.dispatched == 6
    assert len(requests) == len(ingested) == 12
    assert all(request.attempts == 3 for request in requests)
    assert {request.agent for request in requests} == {"codex", "claude-code"}
    assert len({request.name for request in requests}) == 12


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
    assert requests == []
    content = result.digest_path.read_text()
    assert "Quarantined: yes" in content
    assert "canary_enqueue_failed:ValueError" in content


def test_digest_labels_version_perturbation_as_harness_drift(tmp_path: Path) -> None:
    observation = CanaryDriftObservation(
        task_name="petermakhnatch/transaction-reconciliation",
        agent_name="codex",
        reward=1.0,
        baseline_n=6,
        baseline_mean=1.0,
        baseline_stddev=0.0,
        task_version_changed=True,
        is_harness_drift_suspect=True,
        drift_reason="task_version_changed",
    )
    renderer = DigestRenderer(
        repo_root=tmp_path,
        queue=DirectoryQueue(tmp_path / "queue"),
        policy=policy(),
        trial_loader=lambda day: [],
        drift_loader=lambda day: [observation],
    )

    content = renderer.write(report_date=date(2026, 8, 13)).read_text()

    assert "1.000 ± 0.000" in content
    assert "harness-drift suspect (task_version_changed)" in content
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


def test_schema_defines_trailing_seven_day_drift_view() -> None:
    schema = (ROOT / "sql/schema.sql").read_text()

    assert "CREATE OR REPLACE VIEW canary_drift_observations" in schema
    assert "interval '7 days'" in schema
    assert "stddev_samp" in schema
    assert "task_version_changed" in schema
    assert "is_harness_drift_suspect" in schema
