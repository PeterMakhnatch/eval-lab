"""Tests for STATUS.md Generator (evallab.status_generator)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from evallab.schemas import ExperimentPurpose, ExperimentSpec, QueueEvent
from evallab.status_generator import (
    StatusReportData,
    TrialSummary,
    collect_status_data,
    generate_status_markdown,
    render_status_markdown,
    update_status_file,
)

TARGET_DATE = date(2026, 8, 16)
REPORTING_DATE = date(2026, 8, 15)


def _setup_mock_repo(tmp_path: Path) -> Path:
    """Create a standard directory layout with queue, research, and program files."""
    (tmp_path / "queue" / "running").mkdir(parents=True)
    (tmp_path / "queue" / "approved").mkdir(parents=True)
    (tmp_path / "queue" / "waiting").mkdir(parents=True)
    (tmp_path / "queue" / "proposed").mkdir(parents=True)
    (tmp_path / "queue" / "reasons").mkdir(parents=True)
    (tmp_path / "research" / "experiments").mkdir(parents=True)
    (tmp_path / "research" / "evidence" / "runs").mkdir(parents=True)
    return tmp_path


def _write_spec(
    queue_dir: Path,
    spec_id: str,
    name: str,
    task: str,
    agent: str,
    purpose: ExperimentPurpose = "baseline",
    hypothesis: str = "test hypothesis",
) -> Path:
    spec = ExperimentSpec(
        spec_id=spec_id,
        name=name,
        task=task,
        agent=agent,
        purpose=purpose,
        hypothesis=hypothesis,
        submitted_by="test-runner",
        attempts=1,
    )
    dest = queue_dir / f"{spec_id}.json"
    dest.write_text(spec.model_dump_json())
    return dest


def test_idempotent_and_deterministic_status_generation(tmp_path: Path) -> None:
    repo = _setup_mock_repo(tmp_path)
    _write_spec(repo / "queue/running", "spec-run-1", "running-test", "event-summary", "codex")

    # Generate multiple times
    out1 = generate_status_markdown(repo, target_date=TARGET_DATE)
    out2 = generate_status_markdown(repo, target_date=TARGET_DATE)

    assert out1 == out2
    assert f"# Research status — {TARGET_DATE.isoformat()}" in out1
    assert "running-test" in out1
    assert "RUNNING NOW" in out1


def test_status_update_file_writes_to_disk_cleanly(tmp_path: Path) -> None:
    repo = _setup_mock_repo(tmp_path)
    status_file = repo / "research/experiments/STATUS.md"

    updated = update_status_file(repo, target_date=TARGET_DATE, destination=status_file)
    assert updated == status_file
    assert status_file.is_file()

    content1 = status_file.read_text()
    update_status_file(repo, target_date=TARGET_DATE, destination=status_file)
    content2 = status_file.read_text()
    assert content1 == content2


def test_recent_trials_aggregation_and_formatting() -> None:
    trials = [
        TrialSummary(
            job_name="job-1",
            task_name="event-summary",
            agent_name="codex",
            model_name="gpt-5.6-terra",
            reward=1.0,
            exception_type=None,
            cost_usd=0.02,
            finished_at="2026-08-15T12:00:00Z",
        ),
        TrialSummary(
            job_name="job-2",
            task_name="event-summary",
            agent_name="codex",
            model_name="gpt-5.6-terra",
            reward=1.0,
            exception_type=None,
            cost_usd=0.02,
            finished_at="2026-08-15T12:05:00Z",
        ),
        TrialSummary(
            job_name="job-3",
            task_name="html-js-filter",
            agent_name="codex",
            model_name="gpt-5.6-terra",
            reward=0.0,
            exception_type="NonZeroAgentExitCodeError",
            cost_usd=0.01,
            finished_at="2026-08-15T12:10:00Z",
        ),
    ]

    data = StatusReportData(
        target_date=TARGET_DATE,
        reporting_date=REPORTING_DATE,
        recent_trials=trials,
    )
    rendered = render_status_markdown(data)

    assert "## RECENT (Yesterday: 2026-08-15)" in rendered
    assert "**event-summary** — 2/2 `reward==1.0` via codex (gpt-5.6-terra)" in rendered
    assert (
        "**html-js-filter** — 0/1 `reward==1.0` via codex (gpt-5.6-terra) "
        "[exceptions: NonZeroAgentExitCodeError=1]" in rendered
    )


def test_empty_queue_and_empty_trials_message() -> None:
    data = StatusReportData(
        target_date=TARGET_DATE,
        reporting_date=REPORTING_DATE,
        recent_trials=[],
        running_specs=[],
        approved_specs=[],
        waiting_specs=[],
    )
    rendered = render_status_markdown(data)
    assert "No completed trials observed in the reporting window." in rendered
    assert "Nothing in `queue/running/` or `queue/approved/`." in rendered
    assert "No queued work waiting in `queue/waiting/`" in rendered


def test_storm_alarms_embed_in_status_report(tmp_path: Path) -> None:
    repo = _setup_mock_repo(tmp_path)

    # Write synthetic storm in events.jsonl
    events_file = repo / "queue/events.jsonl"
    base_time = datetime(2026, 8, 15, 14, 0, 0, tzinfo=UTC)
    lines = []
    for i in range(8):
        ev = QueueEvent(
            event_id=f"ev-{i}",
            spec_id="spec-1",
            occurred_at=base_time + timedelta(minutes=i * 2),
            event="dispatch_deferred",
            actor="scheduled-tick",
            reason_code="subscription_quota_exhausted",
            job_name="canary-event-summary",
        )
        lines.append(ev.model_dump_json())
    events_file.write_text("\n".join(lines))

    # Generate status report
    data = collect_status_data(repo, target_date=TARGET_DATE, storm_threshold=5)
    assert len(data.storm_alarms) == 1
    assert data.storm_alarms[0].reason_code == "subscription_quota_exhausted"
    assert data.storm_alarms[0].count == 8

    rendered = render_status_markdown(data)
    assert "STORM ALARM ACTIVE" in rendered
    assert "CRITICAL" in rendered
    assert "subscription_quota_exhausted" in rendered
    assert "Active storm alarms: 1" in rendered


def test_waiting_specs_with_reasons_render_correctly(tmp_path: Path) -> None:
    repo = _setup_mock_repo(tmp_path)
    _write_spec(
        repo / "queue/waiting",
        "spec-wait-1",
        "exp-wait",
        "txn-recon",
        "codex",
        purpose="comparison",
    )

    # Add reason file
    reason_file = repo / "queue/reasons/spec-wait-1.json"
    reason_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "spec_id": "spec-wait-1",
                "occurred_at": "2026-08-15T12:00:00Z",
                "code": "paid_run_unauthorized",
                "message": "authorization required for paid codex run",
            }
        )
    )

    rendered = generate_status_markdown(repo, target_date=TARGET_DATE)
    assert "## NEXT" in rendered
    assert "exp-wait" in rendered
    assert "paid_run_unauthorized: authorization required for paid codex run" in rendered
    assert "purpose: comparison" in rendered


def test_program_ledger_and_task_decisions_integration(tmp_path: Path) -> None:
    repo = _setup_mock_repo(tmp_path)
    program_file = repo / "research/experiments/PROGRAM.json"
    program_content = {
        "schema_version": 1,
        "updated_at": "2026-08-16",
        "title": "Eval Lab research program ledger",
        "experiments": [
            {
                "id": "EXP-S02-txn-recon-k",
                "research_question": "Does changing attempt count change interval width?",
                "status": "waiting",
                "blocker": "k=5 hits per_job_cost_ceiling. Peter approval needed.",
                "next_action": "Peter: register n=5 or raise ceiling.",
                "notes": "Cost estimate verified under actuals.",
            }
        ],
    }
    program_file.write_text(json.dumps(program_content))

    rendered = generate_status_markdown(repo, target_date=TARGET_DATE)
    assert "### Program Ledger Next Actions" in rendered
    assert "EXP-S02-txn-recon-k" in rendered
    assert "k=5 hits per_job_cost_ceiling. Peter approval needed." in rendered
    assert "## TASK DECISIONS" in rendered
