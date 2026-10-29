"""NP45-TW-1: adapter steps -> the ATIF form the gate reads. No label is touched here.

Two adapter shapes exist in the registry: ATIF-like (``source`` + ``tool_calls[function_name, arguments]`` + ``observation``)
and role-based (``role`` + ``tool_calls[name|function, args|arguments]``, tool replies as separate ``tool`` steps).
Message and argument text is never changed.
"""

from __future__ import annotations

from typing import Any


def _call(call: dict[str, Any], index: int, number: int) -> dict[str, Any]:
    name = call.get("function_name") or call.get("function") or call.get("name")
    args = call.get("arguments") if "arguments" in call else call.get("args")
    return {
        "tool_call_id": str(call.get("tool_call_id") or f"s{index}c{number}"),
        "function_name": name,
        "arguments": args,
    }


def to_atif(steps: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Return ATIF steps, or ``None`` when the rows carry no recognisable step list."""
    if not steps:
        return None
    if "source" in steps[0]:
        out = []
        for index, raw in enumerate(steps):
            step = dict(raw)
            step.setdefault("step_id", index)
            if step.get("tool_calls"):
                step["tool_calls"] = [
                    _call(c, index, n)
                    for n, c in enumerate(step["tool_calls"])
                    if isinstance(c, dict)
                ]
            out.append(step)
        return out
    if "role" not in steps[0]:
        return None
    out = []
    last: dict[str, Any] | None = None
    for raw in steps:
        role = raw.get("role")
        text = raw.get("text") if raw.get("text") is not None else raw.get("content")
        text = text if isinstance(text, str) else ""
        if role == "assistant":
            step: dict[str, Any] = {"source": "agent", "message": text}
            calls = [
                _call(c, len(out), n)
                for n, c in enumerate(raw.get("tool_calls") or [])
                if isinstance(c, dict)
            ]
            if calls:
                step["tool_calls"] = calls
            last = step
        elif role in ("tool", "tool_result"):
            if last is not None:
                last.setdefault("observation", {"results": []})["results"].append({"content": text})
                continue
            step = {"source": "tool", "message": text}
        else:
            step = {"source": str(role), "message": text}
            last = None
        step["step_id"] = len(out)
        out.append(step)
    return out
