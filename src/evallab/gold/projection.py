"""Source coordinates for the existing public loader; no labels or membership."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import asdict
from typing import Any

from evallab.trajectory_ir import SourceEventRef


def project_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Retain branches in source order; fold results only into an ancestor call."""
    by_id = {str(event["id"]): event for event in events}
    if len(by_id) != len(events):
        raise ValueError("Duplicate source event identity")
    children = Counter(str(e["parent"]) for e in events if e.get("parent") is not None)
    steps: list[dict[str, Any]] = []
    refs: list[SourceEventRef] = []
    projected: dict[str, dict[str, Any]] = {}
    branches: dict[str, str] = {}
    for ordinal, event in enumerate(events):
        key = str(event["id"])
        parent = str(event["parent"]) if event.get("parent") is not None else None
        if parent is not None and parent not in projected:
            raise ValueError(f"Missing or nonpreceding parent: {key} -> {parent}")
        branch = key if parent is None or children[parent] > 1 else branches[parent]
        branches[key] = branch
        message = event["message"]
        role = message.get("role", "system")
        calls = message.get("tool_calls") or []
        call_id = message.get("tool_call_id")
        owner = None
        if role in {"tool", "toolResult"}:
            ancestor = parent
            while ancestor is not None:
                candidate = projected[ancestor]
                candidate_calls = candidate.get("tool_calls") or []
                if candidate_calls and (not call_id or any(
                    c["tool_call_id"] == call_id for c in candidate_calls
                )):
                    owner = candidate
                    break
                ancestor = by_id[ancestor].get("parent")
                ancestor = str(ancestor) if ancestor is not None else None
        if owner is not None:
            owner["observation"]["results"].append({
                "source_call_id": call_id, "content": message.get("content", ""),
                "extra": {"source_event_id": key},
            })
            step = owner
            resolution = "result-to-call"
        else:
            step: dict[str, Any] = {
                "step_id": event.get("step_id", ordinal),
                "source": "agent" if role in {"assistant", "agent"} else role,
                "message": message.get("content", ""),
                "tool_calls": calls, "observation": {"results": []},
                "extra": {"branch_id": branch, "parent_event_id": parent,
                          "on_final_parent_path": event.get("final")},
            }
            if message.get("reasoning_content") is not None:
                step["reasoning_content"] = message["reasoning_content"]
            steps.append(step)
            resolution = "unmatched-result" if role in {"tool", "toolResult"} else "direct"
        projected[key] = step
        refs.append(SourceEventRef(
            source_event_id=key, normalized_step_id=step["step_id"],
            source_path=event.get("path", ""), source_sha256=event.get("sha256", ""),
            source_line=event.get("line"), call_id=call_id,
            parent_event_id=parent, branch_id=branch, on_final_parent_path=event.get("final"),
            event_kind="result" if role in {"tool", "toolResult"} else "call" if calls else "message",
            resolution=resolution,
        ))
    return {"schema_version": "ATIF-v1.6", "steps": steps,
            "source_event_map": [asdict(ref) for ref in refs]}


def cloud_events(connection: Any, trace_id: str) -> dict[str, Any]:
    """Translate only actor and structural DB columns; never copy judge flags."""
    from evallab.production.run_atlas import _tool_calls

    events = []
    for row in connection.execute(
        "SELECT step_index, role, tool_name, command_or_action, content_text,"
        " observation_preview, flags_json FROM trace_steps WHERE trace_id=? ORDER BY step_index",
        (trace_id,),
    ):
        flags = json.loads(row["flags_json"] or "{}")
        message = {"role": row["role"], "content": row["content_text"] or ""}
        if row["role"] in {"assistant", "agent"}:
            native_calls = flags.get("tool_calls")
            message["tool_calls"] = _tool_calls(
                row["tool_name"], json.dumps(native_calls) if native_calls else row["command_or_action"]
            )
            message["reasoning_content"] = flags.get("reasoning_content")
        elif row["role"] == "tool":
            message["tool_call_id"] = flags.get("tool_call_id")
            message["content"] = message["content"] or row["observation_preview"] or ""
        event = {"id": row["step_index"], "step_id": row["step_index"],
                 "parent": flags.get("native_parent"), "message": message,
                 "final": flags.get("on_final_parent_path"),
                 "path": f"cloud-trace-db:trace_steps/{trace_id}"}
        event["sha256"] = hashlib.sha256(
            json.dumps(event, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        events.append(event)
    return project_events(events)


def _cap(text: str, limit: int = 4000) -> str:
    """Cap channel text and report the exact number of omitted characters."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return text[:limit] + f"\n...[truncated {dropped} chars]..."


def _extract_channels(row: sqlite3.Row | dict) -> list[dict[str, Any]]:
    """Emit separate channel entries for narration / call / output.
    Narration = asst reasoning + text; call = command_or_action; output = tool preview/content.
    Returns list so one physical step can contribute narration+call.
    "l": full length for char_* totals; "t": capped (4000) with drop marker.
    Only actor content and reasoning are read from source flags.
    """

    def get(k: str, default: Any = "") -> Any:
        try:
            value = row[k]
        except (KeyError, IndexError):
            return default
        return value if value is not None else default

    role = (get("role") or "").lower()
    step_i = int(get("step_index", "0") or 0)
    content = get("content_text") or ""
    preview = get("observation_preview") or ""
    cmd = get("command_or_action") or ""
    fl = get("flags_json") or "{}"
    try:
        flags = json.loads(fl) if isinstance(fl, str) else (fl or {})
    except Exception:
        flags = {}
    reasoning = (flags.get("reasoning_content") or "") if isinstance(flags, dict) else ""

    chans: list[dict[str, Any]] = []
    if role in ("assistant", "agent"):
        nar_text = (reasoning + "\n" + content).strip()
        if nar_text:
            nar_len = len(nar_text)
            chans.append({"i": step_i, "k": "narration", "l": nar_len, "t": _cap(nar_text)})
        if cmd:
            call_text = cmd if isinstance(cmd, str) else json.dumps(cmd, ensure_ascii=False)
            call_len = len(call_text)
            chans.append({"i": step_i, "k": "call", "l": call_len, "t": _cap(call_text)})
    elif role == "tool":
        out_text = content or preview
        if out_text:
            out_len = len(out_text)
            chans.append({"i": step_i, "k": "output", "l": out_len, "t": _cap(out_text)})
    else:
        txt = content or preview or ""
        if txt:
            txt_len = len(txt)
            chans.append({"i": step_i, "k": "narration", "l": txt_len, "t": _cap(txt)})
    return chans
