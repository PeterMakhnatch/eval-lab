"""Passive Goodfire native-node projection; publisher annotations are never actor input.

Only the admitted string/null content schema and the established sampled-original /
unsampled-carrier sibling topology are supported. A failure is unavailable evidence,
not an empty or clean trace. Object-valued command requests with an unrecognized
schema remain actor input with explicit command-schema unavailability; they are never
rewritten into guessed commands. This module has no storage or execution dependencies.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

NORMALIZATION_VERSION = "goodfire-sampled-originals/2"
_NATIVE_SCHEMA = "lb1.goodfire.native_node_step/1"
_FLAG_FIELDS = (
    "schema", "native_node_index", "native_parent", "native_parent_key_present",
    "on_final_parent_path", "native_child_count", "sampled", "source_role",
    "has_reasoning_content", "reasoning_content", "reasoning_content_key_present",
    "content_encoding", "content_key_present", "tool_calls", "tool_call_id",
)
_BOOLEAN_FIELDS = (
    "native_parent_key_present", "on_final_parent_path", "has_reasoning_content",
    "reasoning_content_key_present", "content_key_present",
)
_ARGUMENT_KEYS = {"bash": "command", "python": "code"}


class TraceAvailabilityError(ValueError):
    """The native evidence cannot be projected without guessing or losing actor input."""


def _fail(index: int, reason: str) -> None:
    # Error messages contain positions and fixed reasons, never source prose or IDs.
    raise TraceAvailabilityError(f"step {index}: {reason}")


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError("nonfinite JSON number")


def _load_json(raw: str, index: int) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_unique_keys, parse_constant=_invalid_constant)
    except (TypeError, ValueError, RecursionError):
        _fail(index, "invalid or ambiguous JSON")


def _digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise TraceAvailabilityError("evidence is not canonical JSON") from None
    return hashlib.sha256(encoded).hexdigest()


def _same_json(left: Any, right: Any) -> bool:
    """JSON semantic equality: object order/number spelling do not equate bools to numbers."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_json(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_json(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def _parse_calls(raw_calls: Any, index: int) -> list[dict]:
    if not isinstance(raw_calls, list):
        _fail(index, "tool calls are not a list")
    calls = []
    ids: set[str] = set()
    for raw in raw_calls:
        if not isinstance(raw, dict) or not {"id", "name", "arguments"} <= raw.keys():
            _fail(index, "incomplete native tool call")
        if raw.keys() - {"id", "name", "arguments", "type"}:
            _fail(index, "unsupported native tool call fields")
        if "type" in raw and raw["type"] != "function":
            _fail(index, "unsupported native tool call type")
        call_id, name = raw["id"], raw["name"]
        if not isinstance(call_id, str) or not call_id.strip():
            _fail(index, "missing tool call ID")
        if call_id in ids:
            _fail(index, "ambiguous repeated tool call ID")
        if not isinstance(name, str) or not name.strip():
            _fail(index, "missing tool function name")
        ids.add(call_id)
        arguments = raw["arguments"]
        if isinstance(arguments, str):
            arguments = _load_json(arguments, index)
        if not isinstance(arguments, dict):
            _fail(index, "tool arguments are not a JSON object")
        calls.append({
            "tool_call_id": call_id, "function_name": name, "arguments": arguments,
        })
    return calls


def _native_record(row: dict) -> tuple[dict, list[dict]]:
    if not isinstance(row, dict) or not {
        "step_index", "role", "content_text", "flags_json"
    } <= row.keys():
        raise TraceAvailabilityError("incomplete native row")
    index = row["step_index"]
    if type(index) is not int or index < 0:
        raise TraceAvailabilityError("invalid native step index")
    if not isinstance(row["flags_json"], str):
        _fail(index, "flags are not raw JSON")
    flags = _load_json(row["flags_json"], index)
    if not isinstance(flags, dict) or any(key not in flags for key in _FLAG_FIELDS):
        _fail(index, "incomplete native flags")
    if flags["schema"] != _NATIVE_SCHEMA:
        _fail(index, "unsupported native schema")
    for key in _BOOLEAN_FIELDS:
        if type(flags[key]) is not bool:
            _fail(index, "nonboolean native availability or topology flag")
    if type(flags["native_node_index"]) is not int or flags["native_node_index"] != index:
        _fail(index, "native node index mismatch")
    parent = flags["native_parent"]
    if parent is not None and (type(parent) is not int or not 0 <= parent < index):
        _fail(index, "parent is not a prior native node")
    if not flags["native_parent_key_present"] and parent is not None:
        _fail(index, "absent native parent has a value")
    if type(flags["native_child_count"]) is not int or flags["native_child_count"] < 0:
        _fail(index, "invalid native child count")
    if flags["sampled"] is not None and type(flags["sampled"]) is not bool:
        _fail(index, "invalid native sampled flag")
    role = row["role"]
    if role not in ("system", "user", "assistant", "tool") or flags["source_role"] != role:
        _fail(index, "unsupported or inconsistent source role")
    if role == "assistant" and type(flags["sampled"]) is not bool:
        _fail(index, "assistant sampling availability is unknown")
    content = row["content_text"]
    encoding = flags["content_encoding"]
    if encoding == "string":
        if not isinstance(content, str) or not flags["content_key_present"]:
            _fail(index, "inconsistent string content")
    elif encoding == "null_or_absent":
        if content is not None:
            _fail(index, "inconsistent null content")
    else:
        _fail(index, "unsupported content encoding")
    reasoning = flags["reasoning_content"]
    if reasoning is not None and not isinstance(reasoning, str):
        _fail(index, "unsupported reasoning content")
    if not flags["reasoning_content_key_present"] and reasoning is not None:
        _fail(index, "absent reasoning has a value")
    if flags["has_reasoning_content"] != (isinstance(reasoning, str) and bool(reasoning)):
        _fail(index, "inconsistent reasoning availability")
    calls = _parse_calls(flags["tool_calls"], index)
    if role != "assistant" and calls:
        _fail(index, "nonassistant node contains tool calls")
    response_id = flags["tool_call_id"]
    if role == "tool":
        if not isinstance(response_id, str) or not response_id.strip():
            _fail(index, "tool observation has no call ID")
    elif response_id is not None:
        _fail(index, "nontool node contains a response ID")
    # In particular, never copy native_node_residual, judge flags, passages, labels,
    # or their arbitrary prose into either hash input or scanner steps.
    record = {key: flags[key] for key in _FLAG_FIELDS}
    record.update(step_index=index, role=role, content_text=content)
    return record, calls


def _same_calls(left: list[dict], right: list[dict]) -> bool:
    return len(left) == len(right) and all(
        a["tool_call_id"] == b["tool_call_id"]
        and a["function_name"] == b["function_name"]
        and _same_json(a["arguments"], b["arguments"])
        for a, b in zip(left, right, strict=True)
    )


def _carrier_aliases(records: list[dict], calls: list[list[dict]]) -> dict[int, int]:
    children: list[list[int]] = [[] for _ in records]
    for node in records:
        parent = node["native_parent"]
        if parent is not None:
            children[parent].append(node["step_index"])
    if [node["step_index"] for node in records if node["native_parent"] is None] != [0]:
        raise TraceAvailabilityError("native graph must have exactly one root")
    final_path: set[int] = set()
    current: int | None = len(records) - 1
    while current is not None:
        final_path.add(current)
        current = records[current]["native_parent"]
    for node in records:
        index = node["step_index"]
        if node["native_child_count"] != len(children[index]):
            _fail(index, "native child count mismatch")
        if node["on_final_parent_path"] != (index in final_path):
            _fail(index, "native final path mismatch")
    aliases: dict[int, int] = {}
    for original in records:
        index = original["step_index"]
        if index in final_path:
            continue
        parent = original["native_parent"]
        if (original["role"] != "assistant" or original["sampled"] is not True
                or children[index] or parent is None or len(children[parent]) != 2):
            _fail(index, "unknown branched topology")
        siblings = [sibling for sibling in children[parent] if sibling != index]
        carrier_index = siblings[0]
        carrier = records[carrier_index]
        if (carrier_index <= index or carrier_index not in final_path
                or carrier["role"] != "assistant" or carrier["sampled"] is not False
                or carrier["reasoning_content"] is not None or carrier_index in aliases):
            _fail(index, "no unique validated publisher carrier")
        if not _same_calls(calls[index], calls[carrier_index]):
            _fail(index, "publisher carrier calls differ from sampled original")
        for field in ("content_text", "reasoning_content"):
            text = original[field]
            if text and (not isinstance(carrier["content_text"], str)
                         or text not in carrier["content_text"]):
                _fail(index, "publisher carrier does not retain original text")
        aliases[carrier_index] = index
    for node in records:
        index = node["step_index"]
        if node["role"] == "assistant" and node["sampled"] is False and index not in aliases:
            _fail(index, "unsampled assistant is not a validated carrier")
    previous: int | None = None
    for node in records:
        index = node["step_index"]
        if index in aliases:
            continue
        parent = node["native_parent"]
        if aliases.get(parent, parent) != previous:
            _fail(index, "alias-normalized topology is not one ancestral chain")
        previous = index
    return aliases


def _project_call(call: dict, raw: dict, index: int) -> tuple[dict, bool]:
    arguments = copy.deepcopy(call["arguments"])
    canonical_key = _ARGUMENT_KEYS.get(call["function_name"])
    projected = canonical_key is not None and "cmd" in arguments
    if canonical_key is not None and projected:
        command = arguments.pop("cmd")
        if not isinstance(command, str):
            _fail(index, "command alias is not a string")
        if canonical_key in arguments and arguments[canonical_key] != command:
            _fail(index, "conflicting canonical command and alias")
        arguments[canonical_key] = command
    extra = {"native_arguments": copy.deepcopy(raw["arguments"])}
    if canonical_key is not None:
        if isinstance(arguments.get(canonical_key), str):
            extra["command_schema_availability"] = "available"
        else:
            extra["command_schema_availability"] = "unavailable"
            extra["command_schema_reason"] = "missing_string_command"
    if "type" in raw:
        extra["native_type"] = raw["type"]
    return {**call, "arguments": arguments, "extra": extra}, projected


def adapt_trace(rows: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """Return original agent steps, every noncarrier native node, and hash/count metadata.

    Native indices remain step IDs. Tool nodes stay nonagent (``source='tool'``) in
    all_steps, and their full output is also bound to the originating original's
    ATIF observation.results using source_call_id. After validating one normalized
    ancestral chain, a response must match exactly one *unanswered* ancestor call.
    IDs may repeat only after the previous use was answered; no nearest-turn or
    global last-call fallback exists. Unanswered calls remain attempts, never
    fabricated observations. A valid argument object without a recognized string
    command is retained with command_schema_availability='unavailable'; callers
    must not classify affected traces as fully scanned negatives. Inputs are not
    mutated and content is never executed.
    """
    if not isinstance(rows, list) or not rows:
        raise TraceAvailabilityError("native trace is unavailable or empty")
    parsed = [_native_record(row) for row in rows]
    parsed.sort(key=lambda item: item[0]["step_index"])
    records = [item[0] for item in parsed]
    calls = [item[1] for item in parsed]
    if [node["step_index"] for node in records] != list(range(len(records))):
        raise TraceAvailabilityError("native indices are duplicated or incomplete")
    aliases = _carrier_aliases(records, calls)
    all_steps: list[dict] = []
    agent_steps: list[dict] = []
    by_step: dict[int, dict] = {}
    unanswered: dict[str, int] = {}
    projections: list[list[int]] = []
    unavailable_commands: list[list[int]] = []
    observations = 0
    for node in records:
        index = node["step_index"]
        if index in aliases:
            continue
        parent = node["native_parent"]
        step: dict = {
            "step_id": index,
            "source": "agent" if node["role"] == "assistant" else node["role"],
            "message": node["content_text"],
            "tool_calls": [],
            "extra": {
                "parent_step_id": aliases.get(parent, parent),
                "content_key_present": node["content_key_present"],
                "reasoning_content_key_present": node["reasoning_content_key_present"],
            },
        }
        if node["reasoning_content_key_present"]:
            step["reasoning_content"] = node["reasoning_content"]
        if node["role"] == "assistant":
            for offset, (call, raw) in enumerate(zip(calls[index], node["tool_calls"], strict=True)):
                projected, changed = _project_call(call, raw, index)
                step["tool_calls"].append(projected)
                if changed:
                    projections.append([index, offset])
                if projected["extra"].get("command_schema_availability") == "unavailable":
                    unavailable_commands.append([index, offset])
                call_id = call["tool_call_id"]
                if call_id in unanswered:
                    _fail(index, "ambiguous unanswered ancestral call ID")
                unanswered[call_id] = index
            agent_steps.append(step)
        elif node["role"] == "tool":
            call_id = node["tool_call_id"]
            if call_id not in unanswered:
                _fail(index, "tool ID has no unique unanswered ancestral call")
            owner = unanswered.pop(call_id)
            step["tool_call_id"] = call_id
            step["extra"]["source_step_id"] = owner
            result = {
                "source_call_id": call_id, "content": node["content_text"],
                "extra": {"native_step_id": index},
            }
            by_step[owner].setdefault("observation", {"results": []})["results"].append(result)
            observations += 1
        all_steps.append(step)
        by_step[index] = step
    if not agent_steps:
        raise TraceAvailabilityError("sampled assistant evidence is unavailable")
    collapse_ledger = [[carrier, original] for carrier, original in sorted(aliases.items())]
    serialization_differences = sum(
        records[original]["tool_calls"] != records[carrier]["tool_calls"]
        for carrier, original in aliases.items()
    )
    source = {
        "normalization_version": NORMALIZATION_VERSION,
        "native_records": records,
        "carrier_aliases": collapse_ledger,
        "argument_projections": projections,
        "unavailable_commands": unavailable_commands,
        "declared_argument_aliases": _ARGUMENT_KEYS,
    }
    metadata = {
        "normalization_version": NORMALIZATION_VERSION,
        "source_sha256": _digest(source),
        "normalized_sha256": _digest(all_steps),
        "native_nodes": len(records),
        "normalized_nodes": len(all_steps),
        "sampled_agent_nodes": len(agent_steps),
        "carrier_aliases": len(aliases),
        "tool_observations": observations,
        "carrier_serialization_differences": serialization_differences,
        "argument_projections": len(projections),
        "unanswered_calls": len(unanswered),
        "unavailable_command_calls": len(unavailable_commands),
        "unavailable_command_steps": sorted({index for index, _offset in unavailable_commands}),
        "unavailable_commands_sha256": _digest(unavailable_commands),
        "carrier_aliases_sha256": _digest(collapse_ledger),
        "argument_projections_sha256": _digest(projections),
    }
    return agent_steps, all_steps, metadata
