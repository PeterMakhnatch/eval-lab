#!/bin/bash
set -e
ROOT="${TESTBED:-/testbed}"
cd "$ROOT"

cat <<'EOF' > serving/harness.py
"""The production serving path: model -> tool -> model, streaming, monitored."""

from __future__ import annotations

from dataclasses import dataclass, field

from reference_v2.loop import stream_tool_loop
from serving.context import RuntimeContext
from serving.hooks import RuntimeHooks
from serving.tools import TOOLS


@dataclass
class TurnResult:
    messages: list[dict]
    text: str
    saved: dict = field(default_factory=dict)


def default_context(conversation_id: str) -> RuntimeContext:
    ctx = RuntimeContext(conversation_id)
    RuntimeHooks.register_default_monitor(ctx)
    return ctx


def serve_turn(
    model,
    user_message: str,
    *,
    tools=TOOLS,
    ctx=None,
    conversation_id="conv-1",
    on_delta=None,
    max_steps=6,
):
    ctx = ctx or default_context(conversation_id)
    messages, text = stream_tool_loop(
        model,
        user_message,
        tools,
        ctx,
        on_delta=on_delta,
        max_steps=max_steps,
    )
    return TurnResult(messages=messages, text=text, saved={"context": ctx.to_dict()})
EOF

python3 -m pytest tests

cat <<'EOF' > MIGRATION_SUMMARY.md
# Serving Harness Migration Summary

- Migrated serve_turn to drive turns via reference_v2 streaming tool loop with on_delta callback support.
- Verified streaming deltas and message sequence using python -m pytest tests.
- Runtime monitoring: remained active — Registered default monitor via canonical RuntimeHooks on strict RuntimeContext without schema modifications.
EOF
