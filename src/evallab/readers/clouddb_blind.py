"""Lossless, label-free conversation projections for the admitted DEV corpus.

Actor content is projected from native conversation columns and retained actor
record types. Source/control provenance selects rows; actor text is never censored.

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
_RETAINED_ACTOR_KINDS = frozenset({
    "retained_headless_prompt", "retained_complete_headless_stdout", "retained_model_message",
})
# These are source codec record types, not patterns searched inside actor text.
# production.run_catalog._has_trace likewise excludes seeker phase telemetry.
_RETAINED_EVIDENCE_KINDS = frozenset({
    "seeker_attempt_event_transcript", "seeker_event_journal",
    "historical_rejected_task_quality_journal", "selected_task_quality_event_journal",
    "native_execution_receipt_record", "retained_differential_receipt",
    "retained_native_artifact_observation",
})


class ActorHistoryUnavailable(ValueError):
    """The source does not retain a supported actor chronology for this run."""

    category = "actor_history_unavailable"

    def __init__(
        self, trace_id: str, *, step_index: int = -1, reason: str = "actor_history_not_retained",
    ) -> None:
        self.trace_id = trace_id
        self.step_index = step_index
        self.reason = reason
        super().__init__(f"Actor history unavailable: trace {trace_id}, step {step_index}: {reason}")


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
    """Project source-native actor rows, excluding unbound retained evidence.

    System/user inputs and tool observations are actor history even though their
    producer is not a model. Explicit retained source kinds distinguish actual
    messages from event journals, receipts and artifact sidecars. No content,
    reasoning or prior feedback is filtered by its words or JSON key names.
    """
    header = con.execute(
        "SELECT model_id FROM agent_traces WHERE trace_id = ?", (trace_id,),
    ).fetchone()
    if header is None:
        raise ActorHistoryUnavailable(trace_id, reason="source_trace_missing")
    rows = con.execute(
        "SELECT step_index, role, tool_name, command_or_action, content_text, "
        "observation_preview, json_extract(flags_json, '$.reasoning_content'), "
        "json_extract(flags_json, '$.tool_call_id'), "
        "json_extract(flags_json, '$.native_node_residual.message.tool_calls'), "
        "json_extract(flags_json, '$.trace_kind'), "
        "json_extract(flags_json, '$.is_model_message') "
        "FROM trace_steps WHERE trace_id = ? ORDER BY step_index",
        (trace_id,),
    )
    messages = []
    actor_kinds = set()
    for (
        index, role, tool_name, command, content, observation, reasoning, call_id, native,
        kind, model_message,
    ) in rows:
        if kind in _RETAINED_EVIDENCE_KINDS:
            continue
        if kind is not None and kind not in _RETAINED_ACTOR_KINDS:
            raise ActorHistoryUnavailable(
                trace_id, step_index=index, reason="unsupported_source_record_type",
            )
        if kind is None and header[0] == "retained-event-journal/no-model" and model_message != 1:
            raise ActorHistoryUnavailable(
                trace_id, step_index=index, reason="unbound_event_journal_record",
            )
        if role not in {"system", "user", "assistant", "tool", "agent"}:
            raise ActorHistoryUnavailable(
                trace_id, step_index=index, reason="unsupported_actor_role",
            )
        actor_kinds.add(kind)
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
    if not messages:
        raise ActorHistoryUnavailable(trace_id)
    if (
        ("retained_headless_prompt" in actor_kinds)
        != ("retained_complete_headless_stdout" in actor_kinds)
    ):
        raise ActorHistoryUnavailable(trace_id, reason="incomplete_headless_actor_capture")
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
