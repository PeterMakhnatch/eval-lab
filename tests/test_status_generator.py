"""Tests for STATUS.md Generator (evallab.status_generator)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from evallab.automation import NightlyCycle
from evallab.digest import DigestRenderer
from evallab.queue import DirectoryQueue, Executor, load_events
from evallab.schemas import (
    AutoRunRule,
    ExperimentPurpose,
    ExperimentSpec,
    HeadlessDoctorChecks,
    HeadlessDoctorReport,
    QueueEvent,
    StandingApprovalsPolicy,
)
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


def test_status_update_file_default_path(tmp_path: Path) -> None:
    repo = _setup_mock_repo(tmp_path)
    expected_path = repo / "docs/STATUS.md"

    updated = update_status_file(repo, target_date=TARGET_DATE)
    assert updated == expected_path
    assert expected_path.is_file()

    content = expected_path.read_text()
    assert "---" in content
    assert "status: living" in content
    assert f"# Research status — {TARGET_DATE.isoformat()}" in content


def test_status_generator_sha256_byte_identity(tmp_path: Path) -> None:
    repo = _setup_mock_repo(tmp_path)
    _write_spec(repo / "queue/running", "spec-run-1", "running-test", "event-summary", "codex")

    out1 = generate_status_markdown(repo, target_date=TARGET_DATE)
    out2 = generate_status_markdown(repo, target_date=TARGET_DATE)

    hash1 = hashlib.sha256(out1.encode("utf-8")).hexdigest()
    hash2 = hashlib.sha256(out2.encode("utf-8")).hexdigest()

    assert hash1 == hash2
    assert out1 == out2


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


def _healthy_doctor_report() -> HeadlessDoctorReport:
    return HeadlessDoctorReport(
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


def test_nightly_cycle_invokes_status_generator_idempotently(tmp_path: Path) -> None:
    repo = _setup_mock_repo(tmp_path)
    queue = DirectoryQueue(repo / "queue")
    policy = StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20.0,
        per_job_cost_ceiling_usd=2.0,
        quiet_failure_rule=3,
        auto_run=[AutoRunRule(name="local-controls", agents=["oracle", "nop"])],
    )
    executor = Executor(repo_root=repo, queue=queue, policy=policy)
    renderer = DigestRenderer(
        repo_root=repo,
        queue=queue,
        policy=policy,
        trial_loader=lambda _day: [],
        drift_loader=lambda _day: [],
        preflight_loader=lambda: None,  # type: ignore[arg-type]
        storm_loader=lambda _day: [],
    )

    doctor = type("Doctor", (), {"run": lambda self: _healthy_doctor_report()})()

    cycle = NightlyCycle(
        doctor=doctor,  # type: ignore[arg-type]
        executor=executor,
        renderer=renderer,
        committer=lambda _path: True,
    )

    result1 = cycle.run(report_date=TARGET_DATE)
    assert result1.status_path is not None
    assert result1.status_path.is_file()
    assert result1.status_path == repo / "docs/STATUS.md"
    content1 = result1.status_path.read_text()
    assert "# Research status" in content1
    # Second run produces identical output
    result2 = cycle.run(report_date=TARGET_DATE)
    assert result2.status_path == result1.status_path
    content2 = result2.status_path.read_text()
    assert content1 == content2


def test_nightly_cycle_handles_status_updater_failure_cleanly(tmp_path: Path) -> None:
    repo = _setup_mock_repo(tmp_path)
    queue = DirectoryQueue(repo / "queue")
    policy = StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20.0,
        per_job_cost_ceiling_usd=2.0,
        quiet_failure_rule=3,
        auto_run=[AutoRunRule(name="local-controls", agents=["oracle", "nop"])],
    )
    executor = Executor(repo_root=repo, queue=queue, policy=policy)
    renderer = DigestRenderer(
        repo_root=repo,
        queue=queue,
        policy=policy,
        trial_loader=lambda _day: [],
        drift_loader=lambda _day: [],
        preflight_loader=lambda: None,  # type: ignore[arg-type]
        storm_loader=lambda _day: [],
    )

    doctor = type("Doctor", (), {"run": lambda self: _healthy_doctor_report()})()

    def failing_status_updater(_day: date) -> Path:
        raise RuntimeError("simulated status generator crash")

    cycle = NightlyCycle(
        doctor=doctor,  # type: ignore[arg-type]
        executor=executor,
        renderer=renderer,
        committer=lambda _path: True,
        status_updater=failing_status_updater,
    )

    result = cycle.run(report_date=TARGET_DATE)
    assert result.status_path is None
    events = load_events(queue.events_path)
    assert any(
        e.event == "status_generation_failed"
        and "RuntimeError" in (e.reason_code or "")
        for e in events
    )
def test_status_rendering_zero_trials_renders_nothing_ran_and_no_trial_ids(tmp_path: Path) -> None:
    repo = _setup_mock_repo(tmp_path)
    rendered = generate_status_markdown(
        repo,
        target_date=TARGET_DATE,
        trial_loader=lambda _day: [],
    )
    assert "## RECENT (Yesterday: 2026-08-15)" in rendered
    assert "No completed trials observed in the reporting window." in rendered
    recent_section = rendered.split("## RECENT")[1].split("## RUNNING NOW")[0]
    assert "- **" not in recent_section
    assert "via " not in recent_section


def test_status_rendering_three_states_distinguishable(tmp_path: Path) -> None:
    t1 = TrialSummary(
        job_name="job-1",
        task_name="canary/event-summary",
        agent_name="codex",
        model_name="gpt-5.6-terra",
        reward=1.0,
        exception_type=None,
        cost_usd=0.05,
        finished_at="2026-08-15T10:00:00Z",
    )
    data_present = StatusReportData(
        target_date=TARGET_DATE,
        reporting_date=REPORTING_DATE,
        recent_trials=[t1],
        catalog_accessible=True,
        trials_source="catalog",
    )
    out_present = render_status_markdown(data_present)
    assert "- **canary/event-summary** — 1/1 `reward==1.0` via codex (gpt-5.6-terra)" in out_present
    assert "No completed trials observed in the reporting window." not in out_present
    assert "Source unavailable" not in out_present

    data_none = StatusReportData(
        target_date=TARGET_DATE,
        reporting_date=REPORTING_DATE,
        recent_trials=[],
        catalog_accessible=True,
        trials_source="catalog",
    )
    out_none = render_status_markdown(data_none)
    assert "No completed trials observed in the reporting window." in out_none
    assert "- **" not in out_none.split("## RECENT")[1].split("## RUNNING NOW")[0]
    assert "Source unavailable" not in out_none

    data_unavail = StatusReportData(
        target_date=TARGET_DATE,
        reporting_date=REPORTING_DATE,
        recent_trials=[],
        catalog_accessible=False,
        trials_source="filesystem",
        catalog_error="Postgres connection timeout",
    )
    out_unavail = render_status_markdown(data_unavail)
    assert "Source unavailable: catalog inaccessible (Postgres connection timeout)." in out_unavail
    assert "No completed trials observed in the reporting window." not in out_unavail
    assert "- **" not in out_unavail.split("## RECENT")[1].split("## RUNNING NOW")[0]

    assert out_present != out_none
    assert out_none != out_unavail
    assert out_present != out_unavail


def test_status_rendering_unreadable_jobs_surfaced_as_count(tmp_path: Path) -> None:
    repo = _setup_mock_repo(tmp_path)
    corrupt_job = repo / "research" / "evidence" / "runs" / "corrupt-job"
    corrupt_job.mkdir(parents=True)
    (corrupt_job / "result.json").write_text("invalid json content {{{")

    data = collect_status_data(repo, target_date=TARGET_DATE, database_url="")
    assert data.unreadable_jobs_count == 1

    rendered = render_status_markdown(data)
    assert "- *Warning:* 1 job directory unreadable." in rendered
    assert "- Unreadable job directories: 1" in rendered


def test_status_filesystem_fallback_honors_date_filter_and_label(tmp_path: Path) -> None:
    repo = _setup_mock_repo(tmp_path)

    # Old job: 2026-08-12 (3 days before REPORTING_DATE 2026-08-15)
    old_job = repo / "runs" / "old-job"
    old_job.mkdir(parents=True)
    (old_job / "result.json").write_text(json.dumps({
        "id": "old-001",
        "finished_at": "2026-08-12T15:00:00Z",
        "n_total_trials": 1,
        "stats": {"n_completed_trials": 1},
    }))
    (old_job / "config.json").write_text(
        json.dumps({"agent": {"name": "oracle"}, "task": {"name": "task-old"}})
    )
    old_trial = old_job / "trial-1"
    old_trial.mkdir(parents=True)
    (old_trial / "result.json").write_text(json.dumps({
        "task_name": "task-old",
        "trial_name": "trial-1",
        "agent_info": {"name": "oracle"},
        "verifier_result": {"rewards": {"reward": 1.0}},
    }))

    # Yesterday job: 2026-08-15
    yest_job = repo / "runs" / "yest-job"
    yest_job.mkdir(parents=True)
    (yest_job / "result.json").write_text(json.dumps({
        "id": "yest-001",
        "finished_at": "2026-08-15T15:00:00Z",
        "n_total_trials": 1,
        "stats": {"n_completed_trials": 1},
    }))
    (yest_job / "config.json").write_text(
        json.dumps({"agent": {"name": "codex"}, "task": {"name": "task-yesterday"}})
    )
    yest_trial = yest_job / "trial-1"
    yest_trial.mkdir(parents=True)
    (yest_trial / "result.json").write_text(json.dumps({
        "task_name": "task-yesterday",
        "trial_name": "trial-1",
        "agent_info": {"name": "codex", "model_info": {"name": "gpt-5.6-terra"}},
        "verifier_result": {"rewards": {"reward": 1.0}},
    }))

    data = collect_status_data(repo, target_date=TARGET_DATE, database_url="")
    assert data.trials_source == "filesystem"
    assert len(data.recent_trials) == 1
    assert data.recent_trials[0].task_name == "task-yesterday"

    rendered = render_status_markdown(data)
    assert "*(Source: filesystem fallback — catalog unavailable)*" in rendered
    assert "**task-yesterday** — 1/1 `reward==1.0` via codex (gpt-5.6-terra)" in rendered
    assert "task-old" not in rendered
