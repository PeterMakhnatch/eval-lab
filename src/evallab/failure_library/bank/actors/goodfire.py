"""Source-native actor projection for Goodfire reward-hack traces.

The raw object is ``traces[0]`` of a retained Goodfire row: a mapping with a
native ``nodes`` array. Each node envelope carries the source message under
``message``. Only that message is read; judge labels, flags and other
annotations on the row are never copied into the projection.

Nodes form a tree through their native integer ``parent``. As in the cloud
loader (``on_final_parent_path``), the run is the final parent path from the
last node to its root; resampled alternatives off that path are excluded and
counted in ``limitations``, never merged into the chosen attempt.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from evallab.failure_library.bank.actors import ActorProjection, ActorUnavailable
from evallab.failure_library.records import RunRecord

_ROLE_SOURCE = {"assistant": "agent", "agent": "agent", "user": "user", "system": "system"}
_TOOL_ROLES = frozenset({"tool", "function", "observation"})


def final_parent_path(nodes: list[Any]) -> set[int]:
    """Indices on the chain from the last node through native ``parent`` links."""
    path: set[int] = set()
    index: int | None = len(nodes) - 1
    while index is not None and 0 <= index < len(nodes) and index not in path:
        path.add(index)
        node = nodes[index]
        parent = node.get("parent") if isinstance(node, Mapping) else None
        index = parent if isinstance(parent, int) and not isinstance(parent, bool) else None
    return path


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
            else:
                raise ActorUnavailable("unsupported Goodfire content block")
        return "\n".join(parts)
    raise ActorUnavailable("unsupported Goodfire content type")


def _calls(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ActorUnavailable("Goodfire tool_calls must be a list")
    calls = []
    for call in value:
        if not isinstance(call, Mapping) or not isinstance(call.get("id"), str):
            raise ActorUnavailable("Goodfire tool call lacks a native id")
        function = call.get("function", call.get("name"))
        if not isinstance(function, str):
            raise ActorUnavailable("Goodfire tool call lacks a native function name")
        calls.append(
            {
                "tool_call_id": call["id"],
                "function_name": function,
                "arguments": call.get("arguments", {}),
            }
        )
    return calls


def _node_message(node: Any, index: int) -> Mapping[str, Any]:
    if not isinstance(node, Mapping) or not isinstance(node.get("message"), Mapping):
        raise ActorUnavailable(f"Goodfire node {index} has no native message envelope")
    return node["message"]


def to_atif(raw: object, run: RunRecord) -> ActorProjection:
    if not isinstance(raw, Mapping):
        raise ActorUnavailable("Goodfire raw trace must be a mapping")
    nodes = raw.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ActorUnavailable("Goodfire trace has no native nodes")

    linked = any(isinstance(node, Mapping) and "parent" in node for node in nodes)
    on_path = final_parent_path(nodes) if linked else set(range(len(nodes)))

    steps: list[dict[str, Any]] = []
    origins: dict[int, str | int] = {}
    call_owner: dict[str, int] = {}
    for index, node in enumerate(nodes):
        if index not in on_path:
            continue
        message = _node_message(node, index)
        role = message.get("role")
        step_id = len(steps) + 1
        native = f"nodes[{index}]"
        if role in _TOOL_ROLES:
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in call_owner:
                raise ActorUnavailable(f"Goodfire tool feedback at {native} has no owning call")
            steps.append(
                {
                    "step_id": step_id,
                    "source": "system",
                    "message": "",
                    "observation": {
                        "results": [
                            {
                                "source_call_id": call_id,
                                "content": _text(message.get("content")),
                            }
                        ],
                    },
                }
            )
            origins[step_id] = native
            continue
        if role not in _ROLE_SOURCE:
            raise ActorUnavailable(f"Goodfire node {native} has unsupported role {role!r}")
        content = _text(message.get("content"))
        reasoning = _text(message.get("reasoning", message.get("reasoning_content")))
        calls = _calls(message.get("tool_calls"))
        step: dict[str, Any] = {
            "step_id": step_id,
            "source": _ROLE_SOURCE[role],
            "message": content or "",
        }
        if reasoning is not None:
            step["reasoning_content"] = reasoning
        if calls:
            step["tool_calls"] = calls
            for call in calls:
                if call["tool_call_id"] in call_owner:
                    raise ActorUnavailable(f"Goodfire tool call id repeats at {native}")
                call_owner[call["tool_call_id"]] = step_id
        steps.append(step)
        origins[step_id] = native

    if not any(step["source"] == "agent" for step in steps):
        raise ActorUnavailable("Goodfire trace contains no actor (agent) message")

    limitations = ["Goodfire tool feedback is recorded as system observations bound to call ids."]
    if len(on_path) < len(nodes):
        limitations.append(
            f"final parent path only; {len(nodes) - len(on_path)} alternative nodes excluded"
        )
    return ActorProjection(
        doc={"schema_version": "ATIF-v1.6", "session_id": run.run_id, "steps": steps},
        scope="source_native_run",
        origins=origins,
        limitations=tuple(limitations),
    )
