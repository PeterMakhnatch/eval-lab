"""Source-native actor projection for redistributed METR MALT responses.

A redistributed MALT row keeps one captured response as a role/content sequence
(RH-Bench). Only assistant-attributed content is projected. Evaluator labels and
verification fields live outside the response object and are never read here.
Plain, unattributed text is refused rather than treated as actor output.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

from evallab.failure_library.bank.actors import ActorProjection, ActorUnavailable
from evallab.failure_library.records import RunRecord

COLLECTION = "metr-malt"
ACTOR_ROLES = frozenset({"assistant", "agent"})
LIMITATIONS = (
    "single captured response, not a whole interactive run",
    "no tool calls, tool results or environment history are present in the source; none are inferred",
    "non-actor messages and native evaluator labels are excluded",
)


def to_atif(raw: object, run: RunRecord) -> ActorProjection:
    if run.collection != COLLECTION:
        raise ActorUnavailable(f"{run.run_id}: not a {COLLECTION} run")
    messages = _messages(raw, run.run_id)
    actor = [
        (index, message) for index, message in enumerate(messages) if message["role"] in ACTOR_ROLES
    ]
    if not actor:
        raise ActorUnavailable(f"{run.run_id}: no assistant-attributed response")

    native_ids = [message.get("step_id") for _, message in actor]
    use_native = all(type(v) is int and v >= 1 for v in native_ids) and len(set(native_ids)) == len(
        native_ids
    )

    steps: list[dict[str, Any]] = []
    origins: dict[int, str | int] = {}
    for ordinal, (index, message) in enumerate(actor, start=1):
        step_id = cast(int, native_ids[ordinal - 1]) if use_native else ordinal
        step: dict[str, Any] = {
            "step_id": step_id,
            "source": "agent",
            "message": _content(message, run.run_id, index),
        }
        reasoning = _reasoning(message, run.run_id, index)
        if reasoning is not None:
            step["reasoning_content"] = reasoning
        steps.append(step)
        origins[step_id] = step_id if use_native else index

    doc = {"schema_version": "ATIF-v1.6", "session_id": run.run_id, "steps": steps}
    return ActorProjection(
        doc=doc, scope="single_response", origins=origins, limitations=LIMITATIONS
    )


def _messages(raw: object, run_id: str) -> list[Mapping[str, Any]]:
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except ValueError as exc:
            raise ActorUnavailable(f"{run_id}: unattributed plain text response") from exc
        raw = decoded
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ActorUnavailable(f"{run_id}: response is not a role/content sequence")
    messages: list[Mapping[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping) or not isinstance(item.get("role"), str):
            raise ActorUnavailable(f"{run_id}: message {index} lacks a role attribution")
        messages.append(item)
    return messages


def _content(message: Mapping[str, Any], run_id: str, index: int) -> str:
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for block in content:
            if (
                not isinstance(block, Mapping)
                or block.get("type", "text") != "text"
                or not isinstance(block.get("text"), str)
            ):
                raise ActorUnavailable(f"{run_id}: message {index} has unsupported content block")
            parts.append(block["text"])
        return "\n".join(parts)
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
