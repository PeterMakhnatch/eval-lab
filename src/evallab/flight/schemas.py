"""Shared schemas for the flight recorder.

Event rows (``flight/events.jsonl``) and timeline rows
(``flight/timeline.jsonl``) carry ``trial_id``, ``job_id`` and ``task`` on
every row so planes can be joined and filtered without sidecar lookups.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

EVENT_SCHEMA = "evallab.flight_event/v1"
TIMELINE_SCHEMA = "evallab.flight_timeline_row/v1"
STATUS_SCHEMA = "evallab.flight_status/v1"

#: Planes in one trial timeline.
PLANES = ("phase", "model", "trajectory", "tool", "kernel", "egress", "file", "verifier")

#: Kernel event kinds emitted by the observer sidecar.
KERNEL_KINDS = (
    "exec",
    "exit",
    "file",
    "connect",
    "connect_result",
    "dns",
    "udp_send",
    "packet",
)

#: Control agents never call models; an empty model plane is their normal
#: state, not missing evidence.
CONTROL_AGENTS = frozenset({"nop", "oracle"})


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        text = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def timeline_row(
    *,
    trial_id: str,
    job_id: str,
    task: str | None,
    ts: str | None,
    plane: str,
    kind: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": TIMELINE_SCHEMA,
        "trial_id": trial_id,
        "job_id": job_id,
        "task": task,
        "ts": ts,
        "plane": plane,
        "kind": kind,
        "detail": detail or {},
    }
