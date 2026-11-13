"""Source coordinates for the existing public loader; no labels or membership."""
from __future__ import annotations

import hashlib
import json
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
