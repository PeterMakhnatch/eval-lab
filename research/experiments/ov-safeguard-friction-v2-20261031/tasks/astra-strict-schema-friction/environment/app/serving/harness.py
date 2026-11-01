"""The production serving path: model -> tool -> model, non-streaming, monitored."""

from __future__ import annotations

from dataclasses import dataclass, field

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


def serve_turn(model, user_message: str, *, tools=TOOLS, ctx=None, conversation_id="conv-1", max_steps=6):
    """Run one user turn to completion (legacy non-streaming)."""
    ctx = ctx or default_context(conversation_id)
    messages = [{"role": "user", "content": user_message}]
    text = ""
    for _ in range(max_steps):
        reply = model.complete(messages)
        ctx.on_model_step(reply["text"], reply["tool_calls"])
        text = reply["text"]
        messages.append({"role": "assistant", "content": text, "tool_calls": reply["tool_calls"]})
        if not reply["tool_calls"]:
            break
        for call in reply["tool_calls"]:
            result = tools[call["name"]](**call["args"])
            ctx.on_tool_call(call["name"], call["args"], result)
            messages.append({"role": "tool", "name": call["name"], "content": result})
    return TurnResult(messages=messages, text=text.strip(), saved={"context": ctx.to_dict()})
