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


class MonitorHook:
    def __init__(self, monitor: RuntimeMonitor) -> None:
        self.monitor = monitor

    def on_model_step(self, text: str, tool_calls: list[dict]) -> None:
        self.monitor.observe("model", {"chars": len(text), "tool_calls": len(tool_calls)})

    def on_tool_call(self, name: str, args: dict, result: object) -> None:
        self.monitor.observe("tool", {"name": name, "args": args})


class RuntimeMonitor:
    def __init__(self, publisher: EventPublisher) -> None:
        self.publisher = publisher

    def observe(self, kind: str, payload: dict) -> None:
        self.publisher.publish({"kind": kind, **payload})

    def as_hook(self) -> MonitorHook:
        return MonitorHook(self)

    def describe(self) -> dict:
        return {"monitor": "runtime-l1", "required": True, "published": self.publisher.published}
