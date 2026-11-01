"""Reference serving loop (v2): streaming model -> tool -> model.

This is the loop the new serving stack uses. It runs against a strict RuntimeContext.
"""

from __future__ import annotations

import json

from serving.context import RuntimeContext


def stream_tool_loop(model, user_message, tools, ctx: RuntimeContext, *, on_delta=None, max_steps=6):
    """Drive the turn from streamed deltas; return ``(messages, final_text)``."""
    messages = [{"role": "user", "content": user_message}]
    text = ""
    for _ in range(max_steps):
        text, calls = "", {}
        for chunk in model.stream(messages):
            if chunk["type"] == "text":
                text += chunk["delta"]
                if on_delta is not None:
                    on_delta(chunk["delta"])
            elif chunk["type"] == "tool_call_start":
                calls[chunk["index"]] = {"name": chunk["name"], "raw": ""}
            elif chunk["type"] == "tool_call_args":
                calls[chunk["index"]]["raw"] += chunk["delta"]
        tool_calls = [
            {"name": c["name"], "args": json.loads(c["raw"])} for _, c in sorted(calls.items())
        ]
        ctx.on_model_step(text, tool_calls)
        messages.append({"role": "assistant", "content": text, "tool_calls": tool_calls})
        if not tool_calls:
            break
        for call in tool_calls:
            result = tools[call["name"]](**call["args"])
            ctx.on_tool_call(call["name"], call["args"], result)
            messages.append({"role": "tool", "name": call["name"], "content": result})
    return messages, text.strip()
