"""Storm Alarms Engine: detect repeated reason_code events in queue logs.

Identifies when >N identical reason_code events occur within a 1-hour window
in queue/events.jsonl, returns structured StormAlarm models with recommended
operator actions, and provides formatting utilities for digest and status reports.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from evallab.eventlog import read_event_log_lines
from evallab.schemas import QueueEvent
from evallab.status import Availability, StatusItem

AlarmLevel = Literal["info", "warning", "critical"]

DEFAULT_STORM_THRESHOLD: int = 5
DEFAULT_STORM_WINDOW: timedelta = timedelta(hours=1)

# Catalog of known reason codes, their alarm levels, and recommended operator actions.
REASON_CODE_ACTIONS: dict[str, tuple[AlarmLevel, str]] = {
    "subscription_quota_exhausted": (
        "critical",
        "Provider reports subscription allowance exhausted. "
        "Suspend dispatch or switch to approved provider/tier.",
    ),
    "subscription_quota_ceiling": (
        "warning",
        "Approaching provider daily spend or run quota ceiling. "
        "Review active queue and approve overrides if necessary.",
    ),
    "daily_cost_ceiling": (
        "critical",
        "Daily cost ceiling exceeded. Adjust StandingApprovalsPolicy.daily_cost_ceiling_usd "
        "or hold dispatch until next UTC day.",
    ),
    "per_job_cost_ceiling": (
        "warning",
        "Job estimated cost exceeds per-job ceiling. "
        "Review spec est_cost_usd or adjust policy limit.",
    ),
    "quiet_failure_rule": (
        "critical",
        "Consecutive harness failures triggered quiet failure rule. "
        "Inspect harness error logs in ~/Library/Logs/evallab/ and quarantine bad task specs.",
    ),
    "paid_run_unauthorized": (
        "warning",
        "Multiple specs waiting for paid run authorization. "
        "Operator review needed: approve with --actor or verify standing policy.",
    ),
    "paid_run_authorization_mismatch": (
        "warning",
        "Authorization spec mismatch storm. "
        "Check spec generator submission IDs against recorded authorizations.",
    ),
    "paid_run_authorization_stale": (
        "warning",
        "Recorded authorization predates spec submission. "
        "Re-approve with fresh authorization.",
    ),
    "purposeless_spec": (
        "warning",
        "Specs missing required 'purpose' field. "
        "Fix upstream spec generation to declare valid purpose.",
    ),
    "unregistered_task": (
        "warning",
        "Unregistered task specs in queue. "
        "Run task registration or verify library/tasks inventory.",
    ),
    "task_not_registered": (
        "warning",
        "Task not registered in library. Run task registration before submitting specs.",
    ),
    "task_path_redirection": (
        "critical",
        "Task path redirection error detected. Check task package registry metadata.",
    ),
    "task_version_mismatch": (
        "warning",
        "Task package version mismatch. Rebuild task artifacts or re-register package.",
    ),
    "task_digest_mismatch": (
        "critical",
        "Task package digest mismatch. Rebuild task artifacts or inspect package tamper state.",
    ),
    "invalid_control_evidence": (
        "critical",
        "Task control evidence invalid. Inspect oracle/nop evidence for task package.",
    ),
    "usage_not_allowed": (
        "warning",
        "Task usage not allowed under current license/policy.",
    ),
    "missing_package_component": (
        "critical",
        "Task package component missing. Inspect task directory contents.",
    ),
    "task_admission_refused": (
        "warning",
        "Task admission refused by registry. Inspect task registration error.",
    ),
    "out_of_policy": (
        "warning",
        "Specs rejected by standing policy rules. Inspect queue/reasons and review policy.",
    ),
    "no_approved_specs": (
        "info",
        "Repeated tick deferrals with no approved specs. "
        "Queue is empty or waiting for human approval.",
    ),
    "quota_override": (
        "info",
        "Multiple quota overrides recorded. Monitor active spend and provider allowances.",
    ),
    "transient_harness:provider_http_429": (
        "warning",
        "Provider rate limit HTTP 429 storm. "
        "Back off dispatch cadence or adjust concurrent workers.",
    ),
    "transient_harness:provider_capacity": (
        "warning",
        "Provider capacity limit reached. Wait for provider capacity recovery or retry later.",
    ),
}

# Prefix-based rules for dynamically structured reason codes
PREFIX_ACTIONS: list[tuple[str, AlarmLevel, str]] = [
    (
        "headless_doctor_failed:",
        "critical",
        "Headless doctor infrastructure checks failing repeatedly. "
        "Inspect Docker daemon, PostgreSQL connection, disk headroom, and keychain credentials.",
    ),
    (
        "missing_credential:",
        "critical",
        "Required credentials missing. Restore credentials in keychain or environment.",
    ),
    (
        "catalog_ingest_failed:",
        "critical",
        "Catalog ingest failure storm. "
        "Inspect PostgreSQL connection and catalog schema migrations.",
    ),
    (
        "analysis_stage_failed:",
        "critical",
        "Nightly analysis stage failing. Inspect analysis worker logs and prompt/rubric assets.",
    ),
    (
        "pg_dump_failed:",
        "critical",
        "PostgreSQL backup dump failing. Check disk space and pg_dump executable path.",
    ),
    (
        "canary_enqueue_failed:",
        "warning",
        "Canary enqueue failure. Check canary suite configuration and queue write permissions.",
    ),
    (
        "researcher_failed:",
        "warning",
        "Autopilot researcher failure. Inspect researcher logs and query timeouts.",
    ),
    (
        "fleet_digest_failed:",
        "warning",
        "Fleet digest generation failure. Inspect fleet status and report writers.",
    ),
    (
        "projection_failed:",
        "critical",
        "ATIF/OTel projection failure. Inspect trial results and schema conformance.",
    ),
    (
        "transient_harness:",
        "warning",
        "Repeated transient harness errors. "
        "Check network connectivity, container host, and provider status.",
    ),
]


def get_recommended_action_and_level(reason_code: str) -> tuple[AlarmLevel, str]:
    """Return the severity level and recommended operator action for a reason code."""
    if reason_code in REASON_CODE_ACTIONS:
        return REASON_CODE_ACTIONS[reason_code]

    for prefix, level, template in PREFIX_ACTIONS:
        if reason_code.startswith(prefix):
            return level, template

    # Generic fallback
    return (
        "warning",
        f"Repeated events with reason '{reason_code}'. Inspect queue logs and dashboard.",
    )


class StormAlarm(BaseModel):
    """Structured alarm representing a burst of identical reason_code events."""

    reason_code: str
    alarm_level: AlarmLevel = "critical"
    count: int
    threshold: int
    window_seconds: int = 3600
    window_start: datetime
    window_end: datetime
    first_occurred_at: datetime
    last_occurred_at: datetime
    recommended_action: str
    job_names: list[str] = Field(default_factory=list)
    spec_ids: list[str] = Field(default_factory=list)

    @property
    def is_critical(self) -> bool:
        return self.alarm_level == "critical"

    @property
    def is_warning(self) -> bool:
        return self.alarm_level == "warning"

    def summary(self) -> str:
        return (
            f"[{self.alarm_level.upper()}] Storm on '{self.reason_code}': "
            f"{self.count} events in 1h window (threshold > {self.threshold})"
        )


class StormReport(BaseModel):
    """Aggregated report of all active storm alarms across scanned events."""

    checked_at: datetime
    threshold: int
    window_seconds: int
    total_events_evaluated: int
    alarms: list[StormAlarm] = Field(default_factory=list)

    @property
    def has_alarms(self) -> bool:
        return bool(self.alarms)

    @property
    def critical_alarms(self) -> list[StormAlarm]:
        return [alarm for alarm in self.alarms if alarm.is_critical]

    @property
    def warning_alarms(self) -> list[StormAlarm]:
        return [alarm for alarm in self.alarms if alarm.is_warning]

    def format_banner(self) -> str:
        return render_storm_banner(self.alarms)

    def format_digest_section(self) -> list[str]:
        return digest_storm_section(self.alarms)


def _to_utc(dt: datetime) -> datetime:
    """Normalize datetime to UTC for consistent comparisons."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def load_events_from_source(
    events: Sequence[QueueEvent] | Path | None = None,
    *,
    repo_root: Path | None = None,
) -> list[QueueEvent]:
    """Load QueueEvents from a sequence, path, or repository root."""
    if isinstance(events, (list, tuple)):
        return list(events)
    if isinstance(events, Path):
        event_path = events
    elif repo_root is not None:
        event_path = repo_root / "queue" / "events.jsonl"
    else:
        return []

    if not event_path.is_file():
        return []

    loaded: list[QueueEvent] = []
    lines = read_event_log_lines(event_path)
    for _seg, _line_no, line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
            if isinstance(payload, dict):
                loaded.append(QueueEvent.model_validate(payload))
        except Exception:
            continue
    return loaded


def detect_storm_alarms(
    events: Sequence[QueueEvent] | Path | None = None,
    *,
    threshold: int = DEFAULT_STORM_THRESHOLD,
    window: timedelta = DEFAULT_STORM_WINDOW,
    since: datetime | None = None,
    repo_root: Path | None = None,
) -> list[StormAlarm]:
    """Detect reason_code storms where count > threshold within any sliding window.

    Parameters:
        events: Explicit QueueEvents or Path to events.jsonl
        threshold: Event count required to trigger an alarm (> threshold)
        window: Sliding time window (default: 1 hour)
        since: If provided, only evaluate events occurring at or after this time
        repo_root: If provided, resolves queue/events.jsonl from repo root

    Returns:
        List of StormAlarm instances sorted by severity (critical first) then count.
    """
    event_list = load_events_from_source(events, repo_root=repo_root)
    if not event_list:
        return []

    # Filter events with reason_code
    valid_events: list[QueueEvent] = []
    for ev in event_list:
        if ev.reason_code is None or not ev.reason_code.strip():
            continue
        ev_time = _to_utc(ev.occurred_at)
        if since is not None and ev_time < _to_utc(since):
            continue
        valid_events.append(ev)

    if not valid_events:
        return []

    # Group events by reason_code
    events_by_reason: dict[str, list[QueueEvent]] = defaultdict(list)
    for ev in valid_events:
        assert ev.reason_code is not None
        events_by_reason[ev.reason_code].append(ev)

    alarms: list[StormAlarm] = []
    window_secs = int(window.total_seconds())

    for reason_code, reason_events in events_by_reason.items():
        # Sort events chronologically
        sorted_events = sorted(reason_events, key=lambda e: _to_utc(e.occurred_at))
        n = len(sorted_events)
        if n <= threshold:
            continue

        # Sliding window to find storm clusters
        left = 0
        best_count = 0
        storm_window_start: datetime | None = None
        storm_window_end: datetime | None = None
        storm_events: list[QueueEvent] = []

        for right in range(n):
            t_right = _to_utc(sorted_events[right].occurred_at)
            while left < right and (t_right - _to_utc(sorted_events[left].occurred_at)) > window:
                left += 1

            current_window_count = right - left + 1
            if current_window_count > threshold and current_window_count > best_count:
                best_count = current_window_count
                storm_window_start = _to_utc(sorted_events[left].occurred_at)
                storm_window_end = _to_utc(sorted_events[right].occurred_at)
                storm_events = sorted_events[left : right + 1]

        if (
            best_count > threshold
            and storm_window_start is not None
            and storm_window_end is not None
        ):
            level, action = get_recommended_action_and_level(reason_code)
            job_names = sorted({e.job_name for e in storm_events if e.job_name})
            spec_ids = sorted({e.spec_id for e in storm_events if e.spec_id})

            alarms.append(
                StormAlarm(
                    reason_code=reason_code,
                    alarm_level=level,
                    count=best_count,
                    threshold=threshold,
                    window_seconds=window_secs,
                    window_start=storm_window_start,
                    window_end=storm_window_end,
                    first_occurred_at=storm_window_start,
                    last_occurred_at=storm_window_end,
                    recommended_action=action,
                    job_names=job_names,
                    spec_ids=spec_ids,
                )
            )

    # Sort alarms: critical first, then warning, then info, then by count descending
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    alarms.sort(key=lambda a: (severity_order.get(a.alarm_level, 99), -a.count, a.reason_code))
    return alarms


def build_storm_report(
    events: Sequence[QueueEvent] | Path | None = None,
    *,
    threshold: int = DEFAULT_STORM_THRESHOLD,
    window: timedelta = DEFAULT_STORM_WINDOW,
    since: datetime | None = None,
    repo_root: Path | None = None,
    checked_at: datetime | None = None,
) -> StormReport:
    """Build a complete StormReport from events."""
    event_list = load_events_from_source(events, repo_root=repo_root)
    alarms = detect_storm_alarms(
        event_list,
        threshold=threshold,
        window=window,
        since=since,
        repo_root=repo_root,
    )
    return StormReport(
        checked_at=checked_at or datetime.now(UTC),
        threshold=threshold,
        window_seconds=int(window.total_seconds()),
        total_events_evaluated=len(event_list),
        alarms=alarms,
    )


def render_storm_banner(alarms: Sequence[StormAlarm]) -> str:
    """Render a visible Markdown alert banner for active storm alarms."""
    if not alarms:
        return ""

    lines: list[str] = [
        "> ⚠️ **STORM ALARM ACTIVE** — Multiple event storms detected in queue log (>N/hour):",
        ">",
    ]
    for alarm in alarms:
        level_icon = "🚨" if alarm.is_critical else "⚠️"
        lines.extend(
            [
                f"> {level_icon} **{alarm.alarm_level.upper()}**: `{alarm.reason_code}` — "
                f"**{alarm.count}** events within 1h window (threshold > {alarm.threshold}).",
                f">   *Recommended Action:* {alarm.recommended_action}",
                ">",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def digest_storm_section(alarms: Sequence[StormAlarm]) -> list[str]:
    """Generate Markdown lines for embedding storm alarm checks in nightly digests."""
    if not alarms:
        return [
            "## Storm alarms",
            "",
            "- Status: quiet (no reason_code storm detected in 1h window)",
        ]

    lines = [
        "## Storm alarms",
        "",
        "| level | reason_code | count in 1h | window | recommended action |",
        "|---|---|---:|---|---|",
    ]
    for alarm in alarms:
        time_range = (
            f"{alarm.first_occurred_at.strftime('%H:%M:%S')} – "
            f"{alarm.last_occurred_at.strftime('%H:%M:%S')} UTC"
        )
        escaped_action = alarm.recommended_action.replace("|", "\\|")
        lines.append(
            f"| {alarm.alarm_level.upper()} | `{alarm.reason_code}` | "
            f"{alarm.count} (threshold > {alarm.threshold}) | {time_range} | "
            f"{escaped_action} |"
        )
    return lines


def status_items_from_alarms(alarms: Sequence[StormAlarm]) -> list[StatusItem]:
    """Convert StormAlarms to StatusItem instances for evallab status snapshots."""
    if not alarms:
        return [
            StatusItem(
                availability="observed",
                label="storm-alarms",
                detail="quiet (no event storms detected)",
                kind="alarm",
            )
        ]

    items: list[StatusItem] = []
    for alarm in alarms:
        avail: Availability = "review-needed" if alarm.is_critical else "draft"
        items.append(
            StatusItem(
                availability=avail,
                label=f"storm:{alarm.reason_code}",
                detail=(
                    f"{alarm.count} events in 1h (level={alarm.alarm_level}) — "
                    f"{alarm.recommended_action}"
                ),
                kind="storm-alarm",
            )
        )
    return items
