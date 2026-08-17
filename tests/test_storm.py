"""Tests for Storm Alarms Engine (evallab.storm)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from evallab.schemas import QueueEvent
from evallab.storm import (
    StormAlarm,
    build_storm_report,
    detect_storm_alarms,
    digest_storm_section,
    get_recommended_action_and_level,
    load_events_from_source,
    render_storm_banner,
    status_items_from_alarms,
)

BASE_TIME = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)


def _make_event(
    *,
    event_id: str,
    occurred_at: datetime,
    reason_code: str | None = None,
    job_name: str | None = "test-job",
    spec_id: str = "spec-1",
    event: str = "dispatch_deferred",
) -> QueueEvent:
    return QueueEvent(
        event_id=event_id,
        spec_id=spec_id,
        occurred_at=occurred_at,
        event=event,
        actor="scheduled-tick",
        reason_code=reason_code,
        job_name=job_name,
    )


def test_empty_events_produce_no_alarms() -> None:
    alarms = detect_storm_alarms([])
    assert alarms == []
    report = build_storm_report([])
    assert not report.has_alarms
    assert report.alarms == []
    assert report.critical_alarms == []
    assert report.warning_alarms == []


def test_events_without_reason_codes_produce_no_alarms() -> None:
    events = [
        _make_event(
            event_id=f"ev-{i}",
            occurred_at=BASE_TIME + timedelta(minutes=i),
            reason_code=None,
        )
        for i in range(10)
    ]
    alarms = detect_storm_alarms(events, threshold=5)
    assert alarms == []


def test_events_at_or_below_threshold_are_quiet() -> None:
    # Threshold = 5. Exactly 5 events -> quiet (must be > 5)
    events = [
        _make_event(
            event_id=f"ev-{i}",
            occurred_at=BASE_TIME + timedelta(minutes=i * 5),
            reason_code="subscription_quota_exhausted",
        )
        for i in range(5)
    ]
    alarms = detect_storm_alarms(events, threshold=5)
    assert alarms == []


def test_storm_triggers_when_exceeding_threshold_in_one_hour() -> None:
    # Threshold = 5. 6 events within 30 minutes -> triggers alarm
    events = [
        _make_event(
            event_id=f"ev-{i}",
            occurred_at=BASE_TIME + timedelta(minutes=i * 5),
            reason_code="subscription_quota_exhausted",
            job_name=f"job-{i}",
            spec_id=f"spec-{i}",
        )
        for i in range(6)
    ]
    alarms = detect_storm_alarms(events, threshold=5)
    assert len(alarms) == 1
    alarm = alarms[0]
    assert alarm.reason_code == "subscription_quota_exhausted"
    assert alarm.count == 6
    assert alarm.threshold == 5
    assert alarm.alarm_level == "critical"
    assert "subscription allowance exhausted" in alarm.recommended_action
    assert len(alarm.job_names) == 6
    assert len(alarm.spec_ids) == 6


def test_sliding_window_boundary_behavior() -> None:
    # 5 events at t=0..20m, 6th event at t=1h05m (outside 1h window) -> quiet
    events = [
        _make_event(
            event_id=f"ev-{i}",
            occurred_at=BASE_TIME + timedelta(minutes=i * 5),
            reason_code="daily_cost_ceiling",
        )
        for i in range(5)
    ]
    events.append(
        _make_event(
            event_id="ev-late",
            occurred_at=BASE_TIME + timedelta(minutes=65),
            reason_code="daily_cost_ceiling",
        )
    )
    alarms = detect_storm_alarms(events, threshold=5, window=timedelta(hours=1))
    assert alarms == []

    # If the 6th event is at t=55m (inside 1h window) -> triggers alarm
    events[-1] = _make_event(
        event_id="ev-in-window",
        occurred_at=BASE_TIME + timedelta(minutes=55),
        reason_code="daily_cost_ceiling",
    )
    alarms = detect_storm_alarms(events, threshold=5, window=timedelta(hours=1))
    assert len(alarms) == 1
    assert alarms[0].count == 6
    assert alarms[0].reason_code == "daily_cost_ceiling"
    assert alarms[0].alarm_level == "critical"


def test_multiple_reason_codes_isolated_and_sorted_by_severity() -> None:
    events: list[QueueEvent] = []
    # 7 critical events (subscription_quota_exhausted)
    for i in range(7):
        events.append(
            _make_event(
                event_id=f"quota-{i}",
                occurred_at=BASE_TIME + timedelta(minutes=i),
                reason_code="subscription_quota_exhausted",
            )
        )
    # 4 warning events (paid_run_unauthorized) -> below threshold of 5 -> no alarm
    for i in range(4):
        events.append(
            _make_event(
                event_id=f"unauth-{i}",
                occurred_at=BASE_TIME + timedelta(minutes=i),
                reason_code="paid_run_unauthorized",
            )
        )
    # 8 warning events (purposeless_spec) -> above threshold of 5 -> warning alarm
    for i in range(8):
        events.append(
            _make_event(
                event_id=f"purpose-{i}",
                occurred_at=BASE_TIME + timedelta(minutes=i),
                reason_code="purposeless_spec",
            )
        )

    alarms = detect_storm_alarms(events, threshold=5)
    assert len(alarms) == 2
    # Critical should be sorted before warning
    assert alarms[0].reason_code == "subscription_quota_exhausted"
    assert alarms[0].alarm_level == "critical"
    assert alarms[0].count == 7

    assert alarms[1].reason_code == "purposeless_spec"
    assert alarms[1].alarm_level == "warning"
    assert alarms[1].count == 8


def test_prefix_matching_for_dynamic_reason_codes() -> None:
    level, action = get_recommended_action_and_level(
        "headless_doctor_failed:postgres_reachable,docker_reachable"
    )
    assert level == "critical"
    assert "Headless doctor infrastructure checks failing" in action

    level, action = get_recommended_action_and_level("missing_credential:codex_auth")
    assert level == "critical"
    assert "Required credentials missing" in action

    level, action = get_recommended_action_and_level("transient_harness:custom_err")
    assert level == "warning"
    assert "Repeated transient harness errors" in action

    level, action = get_recommended_action_and_level("custom_unrecognized_error_code")
    assert level == "warning"
    assert "Repeated events with reason 'custom_unrecognized_error_code'" in action


def test_loading_events_from_file_and_resilience(tmp_path: Path) -> None:
    events_file = tmp_path / "queue" / "events.jsonl"
    events_file.parent.mkdir(parents=True)

    lines = []
    for i in range(8):
        ev = _make_event(
            event_id=f"ev-{i}",
            occurred_at=BASE_TIME + timedelta(minutes=i),
            reason_code="quiet_failure_rule",
        )
        lines.append(ev.model_dump_json())
    # Add corrupted line
    lines.append("{invalid json string")
    lines.append("")
    events_file.write_text("\n".join(lines))

    alarms = detect_storm_alarms(repo_root=tmp_path, threshold=5)
    assert len(alarms) == 1
    assert alarms[0].reason_code == "quiet_failure_rule"
    assert alarms[0].count == 8
    assert alarms[0].alarm_level == "critical"


def test_render_storm_banner_and_digest_section() -> None:
    # When empty
    assert render_storm_banner([]) == ""
    quiet_digest = digest_storm_section([])
    assert "quiet" in "".join(quiet_digest)

    # When alarms present
    alarm = StormAlarm(
        reason_code="subscription_quota_exhausted",
        alarm_level="critical",
        count=8,
        threshold=5,
        window_seconds=3600,
        window_start=BASE_TIME,
        window_end=BASE_TIME + timedelta(minutes=40),
        first_occurred_at=BASE_TIME,
        last_occurred_at=BASE_TIME + timedelta(minutes=40),
        recommended_action="Provider reports subscription allowance exhausted.",
        job_names=["job-1"],
        spec_ids=["spec-1"],
    )
    banner = render_storm_banner([alarm])
    assert "STORM ALARM ACTIVE" in banner
    assert "CRITICAL" in banner
    assert "subscription_quota_exhausted" in banner
    assert "8" in banner

    digest_lines = digest_storm_section([alarm])
    digest_text = "\n".join(digest_lines)
    assert "## Storm alarms" in digest_text
    assert "CRITICAL" in digest_text
    assert "`subscription_quota_exhausted`" in digest_text


def test_status_items_from_alarms_integration() -> None:
    items = status_items_from_alarms([])
    assert len(items) == 1
    assert items[0].availability == "observed"
    assert items[0].label == "storm-alarms"
    assert "quiet" in (items[0].detail or "")

    alarm = StormAlarm(
        reason_code="headless_doctor_failed:docker_reachable",
        alarm_level="critical",
        count=6,
        threshold=5,
        window_seconds=3600,
        window_start=BASE_TIME,
        window_end=BASE_TIME + timedelta(minutes=20),
        first_occurred_at=BASE_TIME,
        last_occurred_at=BASE_TIME + timedelta(minutes=20),
        recommended_action="Inspect Docker daemon.",
    )
    items = status_items_from_alarms([alarm])
    assert len(items) == 1
    assert items[0].availability == "review-needed"
    assert items[0].label == "storm:headless_doctor_failed:docker_reachable"
    assert "6 events in 1h" in (items[0].detail or "")


def test_load_events_from_source_types(tmp_path: Path) -> None:
    ev = _make_event(event_id="e-1", occurred_at=BASE_TIME, reason_code="reason_a")
    assert load_events_from_source([ev]) == [ev]
    assert load_events_from_source((ev,)) == [ev]
    assert load_events_from_source(None) == []

    path = tmp_path / "queue" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ev.model_dump_json() + "\n")
    loaded = load_events_from_source(path)
    assert len(loaded) == 1
    assert loaded[0].reason_code == "reason_a"

    loaded_repo = load_events_from_source(repo_root=tmp_path)
    assert len(loaded_repo) == 1
    assert loaded_repo[0].reason_code == "reason_a"
