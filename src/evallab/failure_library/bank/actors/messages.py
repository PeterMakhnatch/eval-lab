"""Source-native actor projection for HackTrace and OLMo message transcripts.

Both collections keep the agent's interaction as an OpenAI-style ``messages``
sequence. Roles are taken only from the native role field; an unknown role is
refused rather than defaulted to assistant. Assistant tool calls and tool
replies are bound by the native ``tool_call_id``. Evaluator labels, ACT
flags and reward fields live outside the messages list and are never read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from evallab.failure_library.bank.actors import ActorProjection, ActorUnavailable
from evallab.failure_library.records import RunRecord

COLLECTIONS = frozenset({"hacktrace", "olmo-reward-hacking"})
SOURCE_BY_ROLE = {"system": "system", "user": "user", "assistant": "agent"}
LIMITATIONS = (
    "messages are projected from the native sequence; no environment state is reconstructed",
    "evaluator labels, ACT flags and reward fields are excluded from actor input",
)


def to_atif(raw: object, run: RunRecord) -> ActorProjection:
    if run.collection not in COLLECTIONS:
        raise ActorUnavailable(f"{run.run_id}: not a supported message collection")
    messages = _messages(raw, run.run_id)

    native_ids = [m.get("step_id") for m in messages if "step_id" in m]
    use_native = (
        len(native_ids) == len(messages)
        and all(type(v) is int and v >= 1 for v in native_ids)
        and len(set(native_ids)) == len(native_ids)
    )

    steps: list[dict[str, Any]] = []
    origins: dict[int, str | int] = {}
    call_ids: set[str] = set()
    for ordinal, (index, message) in enumerate(enumerate(messages), start=1):
        step_id = cast(int, native_ids[index]) if use_native else ordinal
        role = _role(message, run.run_id, index)
        if role == "tool":
            step = _tool_reply(message, run.run_id, index, call_ids, step_id)
        else:
            step = _actor_step(message, role, run.run_id, index, step_id)
            if role == "assistant":
                for call in step["tool_calls"]:
                    call_ids.add(call["tool_call_id"])
        steps.append(step)
        origins[step_id] = step_id if use_native else index

    scope = "source_native_run"
    doc = {"schema_version": "ATIF-v1.6", "session_id": run.run_id, "steps": steps}
    return ActorProjection(doc=doc, scope=scope, origins=origins, limitations=LIMITATIONS)


def _messages(raw: object, run_id: str) -> list[Mapping[str, Any]]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ActorUnavailable(f"{run_id}: messages must be a sequence")
    if not raw:
        raise ActorUnavailable(f"{run_id}: messages sequence is empty")
    messages: list[Mapping[str, Any]] = []
    for index, message in enumerate(raw):
        if not isinstance(message, Mapping):
            raise ActorUnavailable(f"{run_id}: message {index} is not a mapping")
        messages.append(message)
    if not any(_role_raw(m) == "assistant" for m in messages):
        raise ActorUnavailable(f"{run_id}: no assistant-attributed message")
    return messages


def _role_raw(message: Mapping[str, Any]) -> Any:
    return message.get("role")


def _role(message: Mapping[str, Any], run_id: str, index: int) -> str:
    role = _role_raw(message)
    if role == "tool":
        return "tool"
    if role in SOURCE_BY_ROLE:
        return role
    raise ActorUnavailable(f"{run_id}: message {index} has unsupported role {role!r}")


def _content(message: Mapping[str, Any], run_id: str, index: int) -> str:
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            else:
                raise ActorUnavailable(f"{run_id}: message {index} has unsupported content part")
        return "".join(parts)
    raise ActorUnavailable(f"{run_id}: message {index} has unsupported content")


def _reasoning(message: Mapping[str, Any], run_id: str, index: int) -> str | None:
    for key in ("reasoning_content", "reasoning"):
        value = message.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            return value
        raise ActorUnavailable(f"{run_id}: message {index} has unsupported {key}")
    return None


def _tool_calls(message: Mapping[str, Any], run_id: str, index: int) -> list[dict[str, Any]]:
    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, Sequence) or isinstance(raw_calls, (str, bytes)):
        raise ActorUnavailable(f"{run_id}: message {index} tool_calls is not a sequence")
    calls: list[dict[str, Any]] = []
    for call in raw_calls:
        function = call.get("function") if isinstance(call, Mapping) else None
        call_id = call.get("id") if isinstance(call, Mapping) else None
        if (
            not isinstance(call_id, str)
            or not call_id
            or not isinstance(function, Mapping)
            or not isinstance(function.get("name"), str)
        ):
            raise ActorUnavailable(f"{run_id}: message {index} has an unattributable tool call")
        arguments = function.get("arguments", "")
        if not isinstance(arguments, str):
            raise ActorUnavailable(f"{run_id}: message {index} tool arguments are not text")
        calls.append(
            {
                "tool_call_id": call_id,
                "function_name": function["name"],
                "arguments": arguments,
            }
        )
    return calls


def _actor_step(
    message: Mapping[str, Any], role: str, run_id: str, index: int, step_id: int
) -> dict[str, Any]:
    if role == "assistant":
        calls = _tool_calls(message, run_id, index)
    else:
        if message.get("tool_calls"):
            raise ActorUnavailable(f"{run_id}: message {index} has tool calls outside assistant")
        calls = []
    step: dict[str, Any] = {
        "step_id": step_id,
        "source": SOURCE_BY_ROLE[role],
        "message": _content(message, run_id, index),
        "tool_calls": calls,
    }
    reasoning = _reasoning(message, run_id, index) if role == "assistant" else None
    if reasoning is not None:
        step["reasoning_content"] = reasoning
    return step


def _tool_reply(
    message: Mapping[str, Any], run_id: str, index: int, call_ids: set[str], step_id: int
) -> dict[str, Any]:
    call_id = message.get("tool_call_id")
    if not isinstance(call_id, str) or call_id not in call_ids:
        raise ActorUnavailable(f"{run_id}: tool reply {index} has no matching assistant call")
    return {
        "step_id": step_id,
        "source": "system",
        "message": "",
        "observation": {
            "results": [
                {
                    "source_call_id": call_id,
                    "content": _content(message, run_id, index),
                }
            ]
        },
    }
