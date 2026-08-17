"""Tests for digest rendering, section ordering, and storm alarm integration."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from evallab.digest import DigestRenderer, DigestTrial
from evallab.preflight import build_preflight_report
from evallab.queue import DirectoryQueue, QueueEvent
from evallab.schemas import AutoRunRule, StandingApprovalsPolicy
from evallab.storm import StormAlarm

NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
REPORT_DATE = NOW.date()
PERIOD_DATE = date(2026, 8, 15)

def _policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20.0,
        per_job_cost_ceiling_usd=2.0,
        quiet_failure_rule=3,
        auto_run=[AutoRunRule(name="local-controls", agents=["oracle", "nop"])],
    )

def _make_renderer(
    tmp_path: Path,
    *,
    storm_loader=None,
    trial_loader=None,
    drift_loader=None,
    preflight_loader=None,
) -> DigestRenderer:
    queue = DirectoryQueue(tmp_path / "queue")
    report = build_preflight_report(tmp_path, now=NOW)
    return DigestRenderer(
        repo_root=tmp_path,
        queue=queue,
        policy=_policy(),
        trial_loader=trial_loader or (lambda _day: []),
        drift_loader=drift_loader or (lambda _day: []),
        preflight_loader=preflight_loader or (lambda: report),
        storm_loader=storm_loader,
    )


def test_digest_renders_quiet_storm_alarms_when_no_storms(tmp_path: Path) -> None:
    renderer = _make_renderer(tmp_path, storm_loader=lambda _day: [])
    path = renderer.write(report_date=REPORT_DATE)
    content = path.read_text()

    assert "## Storm alarms" in content
    assert "- Status: quiet (no reason_code storm detected in 1h window)" in content


def test_digest_renders_storm_alarms_table_when_alarms_injected(tmp_path: Path) -> None:
    alarm = StormAlarm(
        reason_code="subscription_quota_exhausted",
        alarm_level="critical",
        count=8,
        threshold=5,
        window_seconds=3600,
        window_start=NOW - timedelta(minutes=45),
        window_end=NOW,
        first_occurred_at=NOW - timedelta(minutes=45),
        last_occurred_at=NOW,
        recommended_action="Provider reports subscription allowance exhausted. Suspend dispatch.",
        job_names=["canary-job-1"],
        spec_ids=["spec-1", "spec-2"],
    )
    renderer = _make_renderer(tmp_path, storm_loader=lambda _day: [alarm])
    path = renderer.write(report_date=REPORT_DATE)
    content = path.read_text()

    assert "## Storm alarms" in content
    assert "| CRITICAL | `subscription_quota_exhausted` | 8 (threshold > 5) |" in content
    assert "Provider reports subscription allowance exhausted." in content


def test_digest_renders_unavailable_storm_alarms_when_loader_raises(tmp_path: Path) -> None:
    def failing_loader(_day: date) -> list[StormAlarm]:
        raise RuntimeError("simulated storage failure")

    renderer = _make_renderer(tmp_path, storm_loader=failing_loader)
    path = renderer.write(report_date=REPORT_DATE)
    content = path.read_text()

    assert "## Storm alarms" in content
    assert (
        "- Unavailable: storm alarms could not be evaluated "
        "(RuntimeError: simulated storage failure). "
        "That is not a statement that no event storm occurred."
    ) in content


def test_digest_section_order(tmp_path: Path) -> None:
    alarm = StormAlarm(
        reason_code="quiet_failure_rule",
        alarm_level="critical",
        count=6,
        threshold=5,
        window_seconds=3600,
        window_start=NOW - timedelta(minutes=30),
        window_end=NOW,
        first_occurred_at=NOW - timedelta(minutes=30),
        last_occurred_at=NOW,
        recommended_action="Inspect harness error logs.",
    )
    trial = DigestTrial(
        job_name="trial-job",
        task_name="t/task",
        agent_name="oracle",
        model_name=None,
        reward=1.0,
        exception_type=None,
        cost_usd=0.0,
        finished_at="2026-08-15T12:00:00Z",
    )
    renderer = _make_renderer(
        tmp_path,
        storm_loader=lambda _day: [alarm],
        trial_loader=lambda day: [trial] if day == PERIOD_DATE else [],
    )
    path = renderer.write(report_date=REPORT_DATE)
    content = path.read_text()

    headings = [
        line
        for line in content.splitlines()
        if line.startswith("## ")
    ]
    expected_headings = [
        "## Automation status",
        "## Preflight",
        "## Completed trials",
        "## Canary drift",
        "## Cost and failures",
        "## Queue",
        "## Evidence and calibration",
        "## Queue events",
        "## Storm alarms",
    ]
    assert headings == expected_headings


def test_digest_default_storm_loader_reads_queue_events(tmp_path: Path) -> None:
    queue = DirectoryQueue(tmp_path / "queue")
    for i in range(7):
        queue.append_event(
            QueueEvent(
                event_id=f"evt-{i}",
                spec_id=f"spec-{i}",
                occurred_at=NOW - timedelta(minutes=30 - i * 2),
                event="tick_deferral",
                actor="executor",
                reason_code="subscription_quota_exhausted",
                report_date=REPORT_DATE.isoformat(),
            )
        )

    renderer = _make_renderer(tmp_path)
    path = renderer.write(report_date=REPORT_DATE)
    content = path.read_text()

    assert "## Storm alarms" in content
    assert "`subscription_quota_exhausted`" in content
    assert "7 (threshold > 5)" in content
