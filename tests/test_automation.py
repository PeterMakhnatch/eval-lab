from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from evallab.automation import (
    DEFAULT_NIGHTLY_STEPS,
    NightlyContext,
    NightlyCycle,
    NightlyStep,
    StepOutcome,
)
from evallab.digest import DigestRenderer
from evallab.queue import DirectoryQueue, Executor
from evallab.schemas import (
    AutoRunRule,
    HeadlessDoctorChecks,
    HeadlessDoctorReport,
    StandingApprovalsPolicy,
)


def _policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[AutoRunRule(name="local-controls", agents=["oracle", "nop"])],
    )


class StaticDoctor:
    def __init__(self, report: HeadlessDoctorReport | None = None) -> None:
        self.report = report or HeadlessDoctorReport(
            checked_at=datetime.now(UTC),
            healthy=True,
            checks=HeadlessDoctorChecks(
                keychain_readable=True,
                codex_auth_present=True,
                docker_reachable=True,
                postgres_reachable=True,
                disk_headroom=True,
            ),
        )

    def run(self) -> HeadlessDoctorReport:
        return self.report


def _setup_cycle(
    tmp_path: Path,
    doctor: Any = None,
    **kwargs: Any,
) -> tuple[NightlyCycle, DirectoryQueue, Path]:
    queue_dir = tmp_path / "queue"
    queue = DirectoryQueue(queue_dir)
    service = Executor(
        repo_root=tmp_path,
        queue=queue,
        policy=_policy(),
        runner=lambda req: None,
        ingester=lambda path: None,
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
    )
    renderer = DigestRenderer(
        repo_root=tmp_path,
        queue=queue,
        policy=_policy(),
        trial_loader=lambda day: [],
        drift_loader=lambda day: [],
        preflight_loader=lambda day: None,
        storm_loader=lambda day: [],
    )
    doc = doctor or StaticDoctor()
    cycle_kwargs: dict[str, Any] = {
        "doctor": doc,
        "executor": service,
        "renderer": renderer,
        "committer": lambda p: True,
        "status_updater": lambda day: tmp_path / "STATUS.md",
        "compactor": lambda day: {"compacted": True, "dt": day.isoformat()},
        "lessons_generator": lambda day: tmp_path / "research/lessons.md",
    }
    cycle_kwargs.update(kwargs)
    cycle = NightlyCycle(**cycle_kwargs)
    return cycle, queue, tmp_path


def test_registered_step_order_matches_canonical_sequence() -> None:
    """The registered step order matches the previous hardcoded order exactly."""
    expected_order = (
        "doctor",
        "catalog_ingest",
        "analysis_staging",
        "parquet_compaction",
        "postgres_backup",
        "canary_enqueue",
        "dispatch",
        "researcher_pass",
        "lessons",
        "digest",
        "status_update",
    )
    actual_order = tuple(step.name for step in DEFAULT_NIGHTLY_STEPS)
    assert actual_order == expected_order

    # Compaction and lessons appear as registered steps in expected positions
    assert "parquet_compaction" in actual_order
    assert actual_order.index("parquet_compaction") == 3  # After analysis_staging / ingest
    assert "lessons" in actual_order
    assert actual_order.index("lessons") == 8  # After researcher_pass / facts


def test_registered_step_policies_and_metadata() -> None:
    """Each step declares its stable name, timeout, on_fail policy, and idempotence."""
    steps_by_name = {step.name: step for step in DEFAULT_NIGHTLY_STEPS}

    abort_steps = (
        "doctor",
        "catalog_ingest",
        "postgres_backup",
        "canary_enqueue",
        "dispatch",
        "digest",
    )
    for name in abort_steps:
        step = steps_by_name[name]
        assert step.on_fail == "abort", f"Expected step {name} to abort on failure"
        assert step.timeout > 0

    continue_steps = (
        "analysis_staging",
        "parquet_compaction",
        "researcher_pass",
        "lessons",
        "status_update",
    )
    for name in continue_steps:
        step = steps_by_name[name]
        assert step.on_fail == "continue", f"Expected step {name} to continue on failure"
        assert step.timeout > 0

    # Idempotence declarations
    assert steps_by_name["doctor"].idempotent is True
    assert steps_by_name["catalog_ingest"].idempotent is True
    assert steps_by_name["analysis_staging"].idempotent is True
    assert steps_by_name["parquet_compaction"].idempotent is True
    assert steps_by_name["postgres_backup"].idempotent is True
    assert steps_by_name["canary_enqueue"].idempotent is False
    assert steps_by_name["dispatch"].idempotent is False
    assert steps_by_name["researcher_pass"].idempotent is False
    assert steps_by_name["lessons"].idempotent is True
    assert steps_by_name["digest"].idempotent is True
    assert steps_by_name["status_update"].idempotent is True


def test_on_fail_continue_policy_allows_later_steps_to_complete(tmp_path: Path) -> None:
    """A step marked continue-on-failure fails, and the cycle still completes later steps."""
    executed_steps: list[str] = []

    def failing_compactor(target_date: date) -> object:
        executed_steps.append("compactor")
        raise RuntimeError("disk full on compaction partition")

    def successful_backup(target_date: date) -> Path:
        executed_steps.append("backup")
        return tmp_path / "backups/postgres.dump"

    def successful_lessons(target_date: date) -> Path:
        executed_steps.append("lessons")
        return tmp_path / "research/lessons.md"

    cycle, queue, _ = _setup_cycle(
        tmp_path,
        compactor=failing_compactor,
        database_backup=successful_backup,
        lessons_generator=successful_lessons,
    )

    result = cycle.run(report_date=date(2026, 8, 17))

    # Compactor failed, but subsequent continue/abort steps still ran
    assert "compactor" in executed_steps
    assert "backup" in executed_steps
    assert "lessons" in executed_steps

    compaction_outcome = result.step_by_name("parquet_compaction")
    assert compaction_outcome is not None
    assert compaction_outcome.status == "failed"
    assert "RuntimeError: disk full on compaction partition" in (compaction_outcome.error or "")

    # Backup and lessons succeeded
    assert result.step_by_name("postgres_backup").status == "ran"
    assert result.step_by_name("lessons").status == "ran"
    assert result.step_by_name("digest").status == "ran"
    assert result.step_by_name("status_update").status == "ran"

    # The cycle was not quarantined by compaction failure
    assert result.quarantined is False


def test_on_fail_abort_policy_stops_subsequent_steps(tmp_path: Path) -> None:
    """A step marked abort fails, and the cycle stops without running later non-surface steps."""
    executed_steps: list[str] = []

    def failing_backup(target_date: date) -> Path:
        executed_steps.append("backup")
        raise RuntimeError("database connection refused")

    def mock_canaries(target_date: date) -> int:
        executed_steps.append("canaries")
        return 1

    def mock_lessons(target_date: date) -> Path:
        executed_steps.append("lessons")
        return tmp_path / "research/lessons.md"

    cycle, queue, _ = _setup_cycle(
        tmp_path,
        database_backup=failing_backup,
        canary_enqueuer=mock_canaries,
        lessons_generator=mock_lessons,
    )

    result = cycle.run(report_date=date(2026, 8, 17))

    assert "backup" in executed_steps
    assert "canaries" not in executed_steps
    assert "lessons" not in executed_steps

    # Backup step is recorded as failed
    backup_outcome = result.step_by_name("postgres_backup")
    assert backup_outcome is not None
    assert backup_outcome.status == "failed"
    assert "RuntimeError: database connection refused" in (backup_outcome.error or "")

    # Subsequent non-surface steps are skipped with reason
    canary_outcome = result.step_by_name("canary_enqueue")
    assert canary_outcome is not None
    assert canary_outcome.status == "skipped"
    assert canary_outcome.reason in ("quarantined_by_prior_step", "aborted_by_prior_step")

    dispatch_outcome = result.step_by_name("dispatch")
    assert dispatch_outcome is not None
    assert dispatch_outcome.status == "skipped"

    lessons_outcome = result.step_by_name("lessons")
    assert lessons_outcome is not None
    assert lessons_outcome.status == "skipped"

    # Terminal reporting surfaces still ran to record the quarantine in human digest
    assert result.step_by_name("digest").status == "ran"
    assert result.step_by_name("status_update").status == "ran"
    assert result.quarantined is True


def test_custom_injected_steps_honour_on_fail_policies(tmp_path: Path) -> None:
    """Injectable steps allow substituting arbitrary steps and verifying continue vs abort."""
    log: list[str] = []

    def step_1(context: NightlyContext) -> None:
        log.append("step_1")

    def step_2_continue_fail(context: NightlyContext) -> None:
        log.append("step_2")
        raise ValueError("non-fatal warning")

    def step_3_ran(context: NightlyContext) -> None:
        log.append("step_3")

    def step_4_abort_fail(context: NightlyContext) -> None:
        log.append("step_4")
        raise RuntimeError("fatal crash")

    def step_5_skipped(context: NightlyContext) -> None:
        log.append("step_5")

    custom_steps = (
        NightlyStep(name="custom_1", fn=step_1, on_fail="continue"),
        NightlyStep(name="custom_2", fn=step_2_continue_fail, on_fail="continue"),
        NightlyStep(name="custom_3", fn=step_3_ran, on_fail="continue"),
        NightlyStep(name="custom_4", fn=step_4_abort_fail, on_fail="abort"),
        NightlyStep(name="custom_5", fn=step_5_skipped, on_fail="continue"),
        NightlyStep(name="digest", fn=lambda ctx: None, on_fail="abort"),
    )

    cycle, _, _ = _setup_cycle(tmp_path, steps=custom_steps)
    result = cycle.run(report_date=date(2026, 8, 17))

    assert log == ["step_1", "step_2", "step_3", "step_4"]

    assert result.step_by_name("custom_1").status == "ran"
    assert result.step_by_name("custom_2").status == "failed"
    assert result.step_by_name("custom_3").status == "ran"
    assert result.step_by_name("custom_4").status == "failed"
    assert result.step_by_name("custom_5").status == "skipped"
    expected_reasons = ("quarantined_by_prior_step", "aborted_by_prior_step")
    assert result.step_by_name("custom_5").reason in expected_reasons


def test_step_outcomes_and_durations_and_skip_reasons(tmp_path: Path) -> None:
    """Each step outcome and duration appear in the report, with skipped reasons."""
    cycle, _, _ = _setup_cycle(
        tmp_path,
        canary_enqueuer=None,
        researcher_pass=None,
        database_backup=None,
    )

    result = cycle.run(report_date=date(2026, 8, 17))

    # All steps are represented in result.steps
    assert len(result.steps) == len(DEFAULT_NIGHTLY_STEPS)

    for outcome in result.steps:
        assert isinstance(outcome, StepOutcome)
        assert outcome.name in cycle.step_names
        assert outcome.status in ("ran", "skipped", "failed")
        assert outcome.duration_s >= 0.0

    # Unconfigured steps are marked skipped with exact reasons
    assert result.step_by_name("canary_enqueue").status == "skipped"
    assert result.step_by_name("canary_enqueue").reason == "no_canary_enqueuer_configured"

    assert result.step_by_name("researcher_pass").status == "skipped"
    assert result.step_by_name("researcher_pass").reason == "no_researcher_configured"

    assert result.step_by_name("postgres_backup").status == "skipped"
    assert result.step_by_name("postgres_backup").reason == "no_backup_configured"

    # Formatted report contains all step details
    report = result.format_step_report()
    assert "Nightly Step Report:" in report
    assert "doctor: ran" in report
    assert "canary_enqueue: skipped [no_canary_enqueuer_configured]" in report
    assert "parquet_compaction: ran" in report
    assert "lessons: ran" in report


def test_two_consecutive_cycles_over_unchanged_fixture_are_idempotent(tmp_path: Path) -> None:
    """Two consecutive cycles over unchanged fixture state produce identical artifacts."""
    def mock_status(day: date) -> Path:
        p = tmp_path / "STATUS.md"
        p.write_text(f"# Status for {day.isoformat()}\nDeterministic content\n", encoding="utf-8")
        return p

    cycle, _, _ = _setup_cycle(
        tmp_path,
        status_updater=mock_status,
        compactor=lambda day: {"ok": True},
        lessons_generator=lambda day: tmp_path / "research/lessons.md",
    )

    day = date(2026, 8, 17)
    result_1 = cycle.run(report_date=day)
    digest_content_1 = result_1.digest_path.read_text(encoding="utf-8")
    status_content_1 = (tmp_path / "STATUS.md").read_text(encoding="utf-8")

    result_2 = cycle.run(report_date=day)
    digest_content_2 = result_2.digest_path.read_text(encoding="utf-8")
    status_content_2 = (tmp_path / "STATUS.md").read_text(encoding="utf-8")

    assert digest_content_1 == digest_content_2
    assert status_content_1 == status_content_2
    assert result_1.quarantined == result_2.quarantined
    assert result_1.enqueued == result_2.enqueued
    assert result_1.dispatched == result_2.dispatched
    assert [s.name for s in result_1.steps] == [s.name for s in result_2.steps]
    assert [s.status for s in result_1.steps] == [s.status for s in result_2.steps]
