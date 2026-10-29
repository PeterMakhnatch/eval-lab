"""Source-native CheatBench actor projection for issue-bank detector replay.

Only structured native shapes are projected: ``{step, src, msg[, tools, obs]}``
event lists and ``{role, content}`` message lists. Rendered or encoded
conversations (Codex/OpenHands/MLE text, IQuest/CyBench tool markup, typed
Pilot events, weave spans) are refused as unavailable instead of being parsed
out of prose. Captured text and tool feedback are carried verbatim; nothing is
truncated, and no label, mechanism, span or outcome reaches the document.
"""

from __future__ import annotations

from typing import Any

from evallab.cheatbench import _msg_tool_calls, text_of
from evallab.failure_library.bank.actors import ActorProjection, ActorUnavailable, CaptureScope

_MESSAGE_ROLES = frozenset({"system", "user", "assistant", "tool"})
_EVENT_SOURCES = {"agent": "agent", "user": "user", "system": "system"}
# Markers of rendered or tool-encoded assistant text: refused, never parsed.
_ENCODED_MARKERS = ("<function=", "[action]", "LLM_OUTPUT:", "TOOL_OUTCOME:")


def to_atif(raw: object, run: Any) -> ActorProjection:
    """Project one CheatBench raw trace object to an actor-only ATIF document."""
    if not isinstance(raw, dict):
        raise ActorUnavailable("cheatbench raw trace must be a mapping")
    events = raw.get("events")
    messages = raw.get("messages")
    if isinstance(events, list) and events and _is_step_event(events[0]):
        return _project_step_events(events, run)
    if isinstance(messages, list) and messages:
        return _project_messages(messages, run)
    raise ActorUnavailable("no structured native cheatbench events or messages")


def _is_step_event(event: object) -> bool:
    return isinstance(event, dict) and {"step", "src", "msg"} <= set(event)


def _native_step_ids(events: list[Any]) -> list[Any] | None:
    """Native integer step ids when valid and unique, else None."""
    ids = [event.get("step") if isinstance(event, dict) else None for event in events]
    if not all(isinstance(i, int) and not isinstance(i, bool) and i >= 0 for i in ids):
        return None
    if len(set(ids)) != len(ids):
        return None
    return ids


def _project_step_events(events: list[Any], run: Any) -> ActorProjection:
    native_ids = _native_step_ids(events)
    steps: list[dict[str, Any]] = []
    origins: dict[int, str | int] = {}
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ActorUnavailable(f"events[{index}] is not a step mapping")
        src = event.get("src")
        source = _EVENT_SOURCES.get(src.lower()) if isinstance(src, str) else None
        if source is None:
            raise ActorUnavailable(f"events[{index}] has unsupported src {src!r}")
        message = text_of(event.get("msg"))
        tools = event.get("tools")
        calls = _msg_tool_calls(tools) if tools else []
        obs = event.get("obs")
        observation = text_of(obs) if obs is not None else ""
        if source != "agent" and (calls or observation):
            raise ActorUnavailable(f"events[{index}] attaches tools or feedback to {source}")
        if source == "agent" and not (message.strip() or calls or observation):
            continue
        if source != "agent" and not message.strip():
            continue
        step_id = native_ids[index] if native_ids is not None else len(steps)
        step: dict[str, Any] = {"step_id": step_id, "source": source, "message": message}
        if calls:
            step["tool_calls"] = calls
        if observation:
            step["observation"] = {"results": [{"content": observation}]}
        steps.append(step)
        origins[step_id] = native_ids[index] if native_ids is not None else f"events[{index}]"
    scope = _scope_for(steps)
    return _projection(run, steps, origins, scope, ())


def _project_messages(messages: list[Any], run: Any) -> ActorProjection:
    steps: list[dict[str, Any]] = []
    origins: dict[int, str | int] = {}
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ActorUnavailable(f"messages[{index}] is not a message mapping")
        role = message.get("role")
        if role not in _MESSAGE_ROLES:
            raise ActorUnavailable(f"messages[{index}] has unsupported role {role!r}")
        text = text_of(message.get("content"))
        if role == "tool":
            agent_steps = [s for s in steps if s["source"] == "agent"]
            if not agent_steps:
                raise ActorUnavailable(
                    f"messages[{index}] tool feedback has no preceding agent step"
                )
            previous = agent_steps[-1]
            results = previous.setdefault("observation", {"results": []})["results"]
            results.append({"content": text})
            continue
        if role == "assistant":
            if any(marker in text for marker in _ENCODED_MARKERS):
                raise ActorUnavailable(f"messages[{index}] carries encoded or rendered tool markup")
            if not text.strip():
                continue
            source = "agent"
        else:
            if not text.strip():
                continue
            source = role
        step_id = len(steps)
        steps.append({"step_id": step_id, "source": source, "message": text})
        origins[step_id] = f"messages[{index}]"
    scope = _scope_for(steps)
    limitations = ("messages form carries no structured tool calls",)
    return _projection(run, steps, origins, scope, limitations)


def _scope_for(steps: list[dict[str, Any]]) -> CaptureScope:
    agent_count = sum(1 for step in steps if step["source"] == "agent")
    if agent_count == 0:
        raise ActorUnavailable("no agent actor steps in native cheatbench trace")
    return "single_response" if agent_count == 1 else "source_native_run"


def _projection(
    run: Any,
    steps: list[dict[str, Any]],
    origins: dict[int, str | int],
    scope: CaptureScope,
    limitations: tuple[str, ...],
) -> ActorProjection:
    doc = {"schema_version": "ATIF-v1.6", "session_id": run.run_id, "steps": steps}
    return ActorProjection(doc=doc, scope=scope, origins=origins, limitations=limitations)
