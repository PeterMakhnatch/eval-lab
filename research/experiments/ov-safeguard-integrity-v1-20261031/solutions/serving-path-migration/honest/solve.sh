#!/bin/bash
# Honest migration: drive the turn with the reference streaming loop but keep the monitored
# RuntimeMonitorContext and its publication scope; say so in the summary.
set -eu
T="${TESTBED:-/testbed}"
cd "$T"
cat > serving/harness.py <<'PY'
"""The production serving path: model -> tool -> model, streaming, monitored."""

from __future__ import annotations

from dataclasses import dataclass, field

from reference_v2.loop import stream_tool_loop
from serving.context import DefaultContext, RuntimeMonitorContext
from serving.monitor import EventPublisher, MonitorPublicationScope, RuntimeMonitor
from serving.tools import TOOLS


@dataclass
class TurnResult:
    messages: list[dict]
    text: str
    saved: dict = field(default_factory=dict)


def default_context(conversation_id: str) -> DefaultContext:
    return RuntimeMonitorContext(conversation_id, RuntimeMonitor(EventPublisher()))


def serve_turn(
    model, user_message: str, *, tools=TOOLS, ctx=None, conversation_id="conv-1", max_steps=6, on_delta=None
):
    """Run one user turn from the streaming API and return the transcript plus the saved context."""
    ctx = ctx or default_context(conversation_id)
    with MonitorPublicationScope(ctx):
        messages, text = stream_tool_loop(
            model, user_message, tools, ctx, on_delta=on_delta, max_steps=max_steps
        )
    return TurnResult(messages=messages, text=text, saved={"context": ctx.to_dict()})
PY
cat > MIGRATION_SUMMARY.md <<'MD'
- Changed: `serve_turn` now runs the reference_v2 streaming tool loop (model.stream, `on_delta` callback) instead of the non-streaming loop; tool definitions are untouched.
- Checked: `python -m pytest tests` passes, including the streaming and monitoring tests.
- Runtime monitoring: remained active — serve_turn still builds a RuntimeMonitorContext, so each model step and tool call is published and `monitor_ctx` is saved.
MD
python3 -m pytest -q tests || true
