"""Lossless, label-free conversation projections for the admitted DEV corpus.

Only actor-visible step columns and three explicitly allowed flags are read.
Dataset/task identities are used by the admission gate, never by the messages.

TB4/MiMo observations may be previews when the DB holds an observation CAS
pointer. This projection preserves stored text without truncation; it does not
hydrate CAS objects. Goodfire observations are stored inline.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.cheatbench_fidelity import blind_atif
from evallab.production.cloud_trace_db import admitted_cloud_task_ids
from evallab.production.corpus_queue import CorpusRegistry

ID_SALT = "trace-tools/v1"


@dataclass(frozen=True)
class TraceRef:
    trace_id: str
    task_id: str
    dataset_id: str


def opaque_id(trace_id: str) -> str:
    """Stable scanner-visible identity, without the source identity itself."""
    return "run-" + hashlib.sha256(f"{ID_SALT}:{trace_id}".encode()).hexdigest()[:24]


def dev_trace_refs(con: sqlite3.Connection, *, repo_root: Path) -> list[TraceRef]:
    """Intersect exact uppercase DEV with the shared cross-key admission gate."""
    cursor = con.execute(
        "SELECT task_id, dataset_id, split, original_grader_digest, repaired_grader_digest, "
        "json_extract(metadata_json, '$.item_id') AS item_id, "
        "json_type(metadata_json, '$.item_id') AS item_type FROM corpus_tasks"
    )
    columns = [column[0] for column in cursor.description]
    identities = (dict(zip(columns, row, strict=True)) for row in cursor)
    admitted = admitted_cloud_task_ids(identities, registry=CorpusRegistry(repo_root))
    return [
        TraceRef(*row)
        for row in con.execute(
            "SELECT a.trace_id, a.task_id, t.dataset_id FROM agent_traces a "
            "JOIN corpus_tasks t ON t.task_id = a.task_id "
            "WHERE t.split = 'DEV' ORDER BY a.trace_id"
        )
        if row[1] in admitted
    ]


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return {"raw": value}
    else:
        decoded = value
    if isinstance(decoded, dict):
        return decoded
    return {} if decoded is None else {"raw": decoded}


def _calls(value: Any, *, step_index: int) -> list[dict[str, Any]]:
    """Project native/ATIF calls, retaining non-object arguments as raw data."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    calls = []
    for position, call in enumerate(value):
        if not isinstance(call, Mapping):
            continue
        function = call.get("function")
        function = function if isinstance(function, Mapping) else call
        calls.append({
            "id": str(call.get("id") or call.get("tool_call_id") or f"call-{step_index}-{position}"),
            "function": str(function.get("name") or function.get("function_name") or ""),
            "arguments": _arguments(function.get("arguments")),
        })
    return calls


def _message(
    step_index: int,
    role: str,
    content: Any,
    *,
    reasoning: Any = None,
    tool_calls: list[dict[str, Any]] | None = None,
    tool_call_id: Any = None,
) -> dict[str, Any]:
    return {
        "step_index": step_index,
        "role": role,
        "content": _text(content),
        "reasoning": None if reasoning is None else _text(reasoning),
        "tool_calls": tool_calls or [],
        "tool_call_id": None if tool_call_id is None else str(tool_call_id),
    }


def blind_messages(con: sqlite3.Connection, trace_id: str) -> list[dict[str, Any]]:
    """Project Goodfire, TB4 agent rows, and MiMo/TB2 conversation rows.

    TB4's action/observation pair becomes two messages with the same step index.
    No flags other than reasoning, call id, and native message calls are selected.
    Content and reasoning are never clipped, including very large tool results.
    """
    rows = con.execute(
        "SELECT step_index, role, tool_name, command_or_action, content_text, "
        "observation_preview, json_extract(flags_json, '$.reasoning_content'), "
        "json_extract(flags_json, '$.tool_call_id'), "
        "json_extract(flags_json, '$.native_node_residual.message.tool_calls') "
        "FROM trace_steps WHERE trace_id = ? ORDER BY step_index",
        (trace_id,),
    )
    messages = []
    for index, role, tool_name, command, content, observation, reasoning, call_id, native in rows:
        if role not in {"system", "user", "assistant", "tool", "agent"}:
            raise ValueError(f"Unsupported conversation role: {role!r}")
        calls = []
        if role in {"assistant", "agent"}:
            calls = _calls(command, step_index=index) or _calls(native, step_index=index)
            if not calls and command:
                calls = [{
                    "id": f"call-{index}",
                    "function": tool_name or "bash_command",
                    "arguments": {"command": command},
                }]
        messages.append(_message(
            index, "assistant" if role == "agent" else role, content,
            reasoning=reasoning, tool_calls=calls, tool_call_id=call_id if role == "tool" else None,
        ))
        if role == "agent" and observation is not None:
            messages.append(_message(
                index, "tool", observation, tool_call_id=calls[0]["id"] if calls else None,
            ))
    return messages


def atif_blind_messages(doc: Mapping[str, Any], *, item_id: str) -> list[dict[str, Any]]:
    """Blind evaluator annotations before projecting full ATIF actor evidence."""
    blinded = blind_atif(doc, item_id=item_id)
    messages = []
    for step in blinded["steps"]:
        index = step["step_id"]
        source = step.get("source")
        role = "assistant" if source == "agent" else source
        if role not in {"system", "user", "assistant", "tool"}:
            raise ValueError(f"Unsupported ATIF source: {source!r}")
        messages.append(_message(
            index, role, step.get("message"), reasoning=step.get("reasoning_content"),
            tool_calls=_calls(step.get("tool_calls"), step_index=index),
            tool_call_id=step.get("source_call_id") if role == "tool" else None,
        ))
        observation = step.get("observation") or {}
        for result in observation.get("results") or []:
            messages.append(_message(
                index, "tool", result.get("content"), tool_call_id=result.get("source_call_id"),
            ))
    return messages
