"""Runtime monitor: publishes one event per model step and tool call for external review."""

from __future__ import annotations

import json
import os
from pathlib import Path


class EventPublisher:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.environ.get("MONITOR_EVENTS", "var/monitor_events.jsonl"))
        self.published = 0

    def publish(self, event: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
        self.published += 1


class RuntimeMonitor:
    def __init__(self, publisher: EventPublisher) -> None:
        self.publisher = publisher

    def observe(self, kind: str, payload: dict) -> None:
        self.publisher.publish({"kind": kind, **payload})

    def describe(self) -> dict:
        return {"monitor": "runtime-l1", "required": True, "published": self.publisher.published}


class MonitorPublicationScope:
    """Brackets a serving turn with start/end markers when its context has a monitor."""

    def __init__(self, ctx: object) -> None:
        self.monitor = getattr(ctx, "monitor", None)

    def __enter__(self) -> MonitorPublicationScope:
        if self.monitor is not None:
            self.monitor.observe("turn_start", {})
        return self

    def __exit__(self, *exc: object) -> None:
        if self.monitor is not None:
            self.monitor.observe("turn_end", {})
