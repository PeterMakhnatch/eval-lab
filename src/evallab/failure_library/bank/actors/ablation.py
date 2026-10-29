"""Source-native actor projection for reward-hacking-ablation rollouts.

The collector (``runs/ingest.py::_ablation``) addresses every native JSONL line with
``reference(path, index, jsonl=True)`` and no ``&field=``, so ``raw`` is the whole row:

- ``trajectories.jsonl`` rows (harness = ``agent_type``) carry ``messages``: role-keyed
  chat messages. Assistant messages hold ``content``, ``reasoning_content``/``reasoning``
  and ``tool_calls``; ``role: tool`` messages are feedback for an earlier call.
- ``records.jsonl.gz`` rhenvs envelopes (harness = ``rhenvs/<env>``) carry ``items``:
  role-keyed messages, or typed ``agent_log`` (``text``) / ``agent_episode``
  (``response``) events, in native order.

Only those documented, actor-bearing harnesses are read. Token-id rows, grading
summaries, final-text-only episodes and any entry whose role, call binding or feedback
binding is missing or ambiguous are refused with :class:`ActorUnavailable`; nothing is
inferred from labels, rewards or text.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from evallab.failure_library.bank.actors import ActorProjection, ActorUnavailable
from evallab.failure_library.bank.sources.library_runs import _ABLATION_ACTOR_HARNESSES
from evallab.failure_library.records import RunRecord

COLLECTION = "reward-hacking-ablation"
SCHEMA_VERSION = "ATIF-v1.6"
#: Harness whose export is a single model response rather than a multi-step run.
_SINGLE_RESPONSE_HARNESS = "single_step"
#: Top-level or per-entry keys that only hold token material, never readable actor text.
_TOKEN_KEYS = frozenset(
    {
        "token_ids",
        "prompt_token_ids",
        "completion_token_ids",
        "output_token_ids",
        "input_ids",
        "logprobs",
    }
)
_ROLES = {
    "system": "system",
    "developer": "system",
    "user": "user",
    "assistant": "agent",
    "agent": "agent",
}
#: Typed rhenvs events -> the native key holding their agent text.
_EVENT_TEXT_KEYS = {"agent_log": "text", "agent_episode": "response"}
_TEXT_PART_TYPES = frozenset({"text", "input_text", "output_text"})


def to_atif(raw: object, run: RunRecord) -> ActorProjection:
    """Project one addressed ablation row into ATIF-shaped source-native actor steps."""
    if run.collection != COLLECTION:
        raise ActorUnavailable(f"{run.collection!r} is not a reward-hacking-ablation run")
    if run.harness not in _ABLATION_ACTOR_HARNESSES:
        raise ActorUnavailable(f"harness {run.harness!r} keeps no attributable actor history")
    key = _sequence_key(run)
    entries = _entries(raw, key)
    steps, origins = _project(entries, key)
    if not any(_has_actor_content(step) for step in steps if step["source"] == "agent"):
        raise ActorUnavailable("no agent message, reasoning or tool call in the actor sequence")
    agent_steps = [step for step in steps if step["source"] == "agent"]
    single = run.harness == _SINGLE_RESPONSE_HARNESS or (
        len(agent_steps) == 1 and not agent_steps[0].get("tool_calls")
    )
    limitations: tuple[str, ...] = ()
    if single:
        limitations = (
            "single model response: no environment or tool history is captured for this run",
        )
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "session_id": run.run_id,
        "steps": steps,
    }
    return ActorProjection(
        doc=doc,
        scope="single_response" if single else "source_native_run",
        origins=origins,
        limitations=limitations,
    )


def _sequence_key(run: RunRecord) -> str:
    return "items" if str(run.harness).startswith("rhenvs/") else "messages"


def _entries(raw: object, key: str) -> list[Mapping[str, Any]]:
    if not isinstance(raw, Mapping):
        raise ActorUnavailable(f"unsupported raw type {type(raw).__name__}; expected a row")
    if _TOKEN_KEYS & set(raw):
        raise ActorUnavailable("token-id material is not actor text")
    value = raw.get(key)
    if value is None:
        raise ActorUnavailable(
            f"row has no {key!r} actor sequence (grading summary, final text or episode)"
        )
    if not isinstance(value, list) or not value:
        raise ActorUnavailable(f"{key!r} actor sequence is empty or not a list")
    if not all(isinstance(entry, Mapping) for entry in value):
        raise ActorUnavailable(f"{key!r} actor sequence contains a non-object entry")
    return value


def _project(
    entries: list[Mapping[str, Any]], key: str
) -> tuple[list[dict[str, Any]], dict[int, str]]:
    native_ids = _native_step_ids(entries)
    steps: list[dict[str, Any]] = []
    origins: dict[int, str] = {}
    call_owner: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        where = f"{key}[{index}]"
        if _TOKEN_KEYS & set(entry):
            raise ActorUnavailable(f"{where} carries token-id material")
        role = entry.get("role")
        if role == "tool":
            _attach_feedback(entry, where, call_owner)
            continue
        if role is None and entry.get("type") in _EVENT_TEXT_KEYS:
            step = _event_step(entry, where)
            source = "agent"
        elif isinstance(role, str) and role in _ROLES:
            step = _message_step(entry, where, _ROLES[role], call_owner)
            source = _ROLES[role]
        else:
            raise ActorUnavailable(
                f"{where} has unsupported role/type {role or entry.get('type')!r}"
            )
        step["step_id"] = native_ids[index] if native_ids is not None else len(steps) + 1
        step["source"] = source
        steps.append(step)
        origins[step["step_id"]] = where
    return steps, origins


def _native_step_ids(entries: list[Mapping[str, Any]]) -> dict[int, int] | None:
    """Native integer step ids keyed by entry index, when every entry carries a unique one."""
    values = [entry.get("step_id") for entry in entries if entry.get("role") != "tool"]
    if not values or not all(_is_index(value) for value in values):
        return None
    if len(set(values)) != len(values):
        return None
    ids: dict[int, int] = {}
    for index, entry in enumerate(entries):
        if entry.get("role") != "tool":
            ids[index] = entry["step_id"]
    return ids


def _message_step(
    entry: Mapping[str, Any],
    where: str,
    source: str,
    call_owner: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    step: dict[str, Any] = {"message": _text(entry.get("content"), where)}
    reasoning = _reasoning(entry, where)
    if reasoning is not None:
        step["reasoning_content"] = reasoning
    calls = _calls(entry, where)
    if calls:
        step["tool_calls"] = calls
        for call in calls:
            call_id = call["tool_call_id"]
            if call_id in call_owner:
                raise ActorUnavailable(
                    f"tool call id {call_id!r} is repeated; feedback binding is ambiguous"
                )
            call_owner[call_id] = step
    return step


def _event_step(entry: Mapping[str, Any], where: str) -> dict[str, Any]:
    native_key = _EVENT_TEXT_KEYS[str(entry["type"])]
    value = entry.get(native_key)
    if not isinstance(value, str):
        raise ActorUnavailable(f"{where} {entry['type']} has no string {native_key!r}")
    return {"message": value}


def _attach_feedback(
    entry: Mapping[str, Any],
    where: str,
    call_owner: dict[str, dict[str, Any]],
) -> None:
    call_id = entry.get("tool_call_id")
    if not isinstance(call_id, str) or not call_id:
        raise ActorUnavailable(f"{where} tool message has no tool_call_id to bind it to")
    owner = call_owner.get(call_id)
    if owner is None:
        raise ActorUnavailable(f"{where} tool message answers no earlier tool call")
    observation = owner.setdefault("observation", {"results": []})
    observation["results"].append(
        {
            "source_call_id": call_id,
            "content": _text(entry.get("content"), where),
        }
    )


def _text(value: object, where: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if isinstance(part, str):
                parts.append(part)
            elif (
                isinstance(part, Mapping)
                and part.get("type") in _TEXT_PART_TYPES
                and isinstance(part.get("text"), str)
            ):
                parts.append(part["text"])
            else:
                raise ActorUnavailable(f"{where} has a non-text content part")
        return "\n".join(parts)
    raise ActorUnavailable(f"{where} content is not text")


def _reasoning(entry: Mapping[str, Any], where: str) -> str | None:
    found = [
        _text(entry[key], f"{where} {key}")
        for key in ("reasoning_content", "reasoning")
        if entry.get(key) not in (None, "")
    ]
    if not found:
        return None
    if len(set(found)) != 1:
        raise ActorUnavailable(f"{where} carries two different reasoning fields")
    return found[0]


def _calls(entry: Mapping[str, Any], where: str) -> list[dict[str, Any]]:
    raw_calls = entry.get("tool_calls")
    if raw_calls in (None, []):
        return []
    if not isinstance(raw_calls, list):
        raise ActorUnavailable(f"{where} tool_calls is not a list")
    calls: list[dict[str, Any]] = []
    for position, call in enumerate(raw_calls):
        if not isinstance(call, Mapping):
            raise ActorUnavailable(f"{where} tool call {position} is not an object")
        function = call.get("function")
        if not isinstance(function, Mapping):
            raise ActorUnavailable(f"{where} tool call {position} has no function")
        call_id = call.get("id", call.get("tool_call_id"))
        name = function.get("name")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str):
            raise ActorUnavailable(f"{where} tool call {position} lacks id or name")
        calls.append(
            {
                "tool_call_id": call_id,
                "function_name": name,
                "arguments": _arguments(function.get("arguments")),
            }
        )
    return calls


def _arguments(value: object) -> dict[str, Any]:
    """Keep call arguments verbatim: the parsed object when JSON, else as received."""
    if isinstance(value, Mapping):
        return dict(value)
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"unparsed_arguments": value}
        if isinstance(parsed, dict):
            return parsed
        return {"unparsed_arguments": value}
    return {"value": value}


def _is_index(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _has_actor_content(step: Mapping[str, Any]) -> bool:
    return bool(step.get("message") or step.get("reasoning_content") or step.get("tool_calls"))
