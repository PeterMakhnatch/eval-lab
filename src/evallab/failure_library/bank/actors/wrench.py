"""Source-native actor projection for Terminal Wrench ATIF trajectories.

Wrench's ``transcript_ref`` selects the trajectory's ``steps`` field, so ``raw``
is the step list; a whole trajectory mapping carrying ``steps`` is accepted too.
Agent steps carry the actor's message, reasoning, tool calls and tool feedback
(observation results). User and system steps hold the task prompt and are not
actor output. Verifier rewards, labels, judge annotations and any other
top-level or step-level metadata are not copied. Logs without agent steps are
refused rather than treated as actor history.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

from evallab.failure_library.bank.actors import ActorProjection, ActorUnavailable
from evallab.failure_library.records import RunRecord

COLLECTION = "terminal-wrench"
ACTOR_SOURCE = "agent"
STEP_SOURCES = frozenset({"agent", "user", "system"})
TEXT_KEYS = ("reasoning", "reasoning_content")
CALL_KEYS = ("tool_call_id", "function_name", "arguments")
RESULT_KEYS = ("source_call_id", "content")
LIMITATIONS = (
    "user and system steps (task prompt and environment setup) are not actor output and are omitted",
    "tool feedback is limited to the observation results attached to agent steps; none is inferred",
    "verifier rewards, labels, judge annotations, agent metadata and other non-ATIF fields are excluded",
)


def to_atif(raw: object, run: RunRecord) -> ActorProjection:
    if run.collection != COLLECTION:
        raise ActorUnavailable(f"{run.run_id}: not a {COLLECTION} run")
    steps = _source_steps(raw, run.run_id)
    actor = [(index, step) for index, step in enumerate(steps) if step["source"] == ACTOR_SOURCE]
    if not actor:
        raise ActorUnavailable(f"{run.run_id}: no agent steps; log-only rows are refused")
    if not any(_has_actor_content(step) for _, step in actor):
        raise ActorUnavailable(f"{run.run_id}: agent steps carry no message, call or feedback")

    native_ids = [step.get("step_id") for _, step in actor]
    use_native = all(type(v) is int and v >= 1 for v in native_ids) and len(set(native_ids)) == len(
        native_ids
    )

    projected: list[dict[str, Any]] = []
    origins: dict[int, str | int] = {}
    for ordinal, (index, step) in enumerate(actor, start=1):
        step_id = cast(int, native_ids[ordinal - 1]) if use_native else ordinal
        projected.append(_agent_step(step, step_id, run.run_id, index))
        origins[step_id] = step_id if use_native else index

    doc = {"schema_version": "ATIF-v1.6", "session_id": run.run_id, "steps": projected}
    return ActorProjection(
        doc=doc, scope="source_native_run", origins=origins, limitations=LIMITATIONS
    )


def _source_steps(raw: object, run_id: str) -> list[Mapping[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ActorUnavailable(f"{run_id}: unparseable trajectory text") from exc
    if isinstance(raw, Mapping):
        if "steps" not in raw:
            raise ActorUnavailable(f"{run_id}: trajectory has no steps field")
        raw = raw["steps"]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        raise ActorUnavailable(f"{run_id}: steps must be a non-empty list")
    steps: list[Mapping[str, Any]] = []
    for index, step in enumerate(raw):
        if not isinstance(step, Mapping) or step.get("source") not in {"agent", "user", "system"}:
            raise ActorUnavailable(f"{run_id}: step {index} has no supported source attribution")
        steps.append(step)
    return steps


def _has_actor_content(step: Mapping[str, Any]) -> bool:
    return (
        bool(step.get("message"))
        or bool(step.get("tool_calls"))
        or step.get("observation") is not None
    )


def _agent_step(
    step: Mapping[str, Any], step_id: int | str, run_id: str, index: int
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "step_id": step_id,
        "source": ACTOR_SOURCE,
        "message": _text(step.get("message"), run_id, index, "message"),
    }
    for key in TEXT_KEYS:
        if step.get(key) is not None:
            out[key] = _text(step[key], run_id, index, key)
    if step.get("tool_calls") is not None:
        out["tool_calls"] = [
            _call(call, run_id, index, position)
            for position, call in enumerate(
                _sequence(step["tool_calls"], run_id, index, "tool_calls")
            )
        ]
    if step.get("observation") is not None:
        out["observation"] = _observation(step["observation"], run_id, index)
    return out


def _text(value: object, run_id: str, index: int, key: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        parts: list[str] = []
        for block in value:
            if (
                not isinstance(block, Mapping)
                or block.get("type") != "text"
                or not isinstance(block.get("text"), str)
            ):
                raise ActorUnavailable(f"{run_id}: step {index} has unsupported {key} block")
            parts.append(block["text"])
        return "\n".join(parts)
    raise ActorUnavailable(f"{run_id}: step {index} has unsupported {key}")


def _call(call: object, run_id: str, index: int, position: int) -> dict[str, Any]:
    if not isinstance(call, Mapping):
        raise ActorUnavailable(f"{run_id}: step {index} call {position} is not an object")
    if not isinstance(call.get("tool_call_id"), str) or not isinstance(
        call.get("function_name"), str
    ):
        raise ActorUnavailable(
            f"{run_id}: step {index} call {position} lacks a tool_call_id or function_name"
        )
    if not isinstance(call.get("arguments"), Mapping):
        raise ActorUnavailable(f"{run_id}: step {index} call {position} has no argument object")
    return {key: copy.deepcopy(call[key]) for key in CALL_KEYS if key in call}


def _observation(observation: object, run_id: str, index: int) -> dict[str, Any]:
    if not isinstance(observation, Mapping) or "results" not in observation:
        raise ActorUnavailable(f"{run_id}: step {index} observation has no results list")
    results: list[dict[str, Any]] = []
    for position, result in enumerate(
        _sequence(observation["results"], run_id, index, "observation")
    ):
        if not isinstance(result, Mapping) or not isinstance(result.get("content"), (str, list)):
            raise ActorUnavailable(
                f"{run_id}: step {index} result {position} has no feedback content"
            )
        results.append({key: copy.deepcopy(result[key]) for key in RESULT_KEYS if key in result})
    return {"results": results}


def _sequence(value: object, run_id: str, index: int, key: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ActorUnavailable(f"{run_id}: step {index} {key} is not a list")
    return value
