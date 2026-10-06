"""Native Xiaomi messages to Harbor ATIF, without changing the agent's history."""

from __future__ import annotations

import ast
import json
from pathlib import PurePosixPath
from typing import Any

from evallab.mimoagent_worker import NATIVE_REVISION, SAMPLING, SWE_SHA256


def _totals(calls: list[dict]) -> dict:
    known = [call["usage"] for call in calls if isinstance(call.get("usage"), dict)]
    result: dict[str, Any] = {"extra": {
        "model_requests": len(calls),
        "requests_without_usage": len(calls) - len(known),
        "api_pricing": "no per-token charge; Modal GPU time is accounted separately",
    }}
    for key, field in (("total_prompt_tokens", "prompt_tokens"),
                       ("total_completion_tokens", "completion_tokens")):
        values = [usage[field] for usage in known if isinstance(usage.get(field), int)]
        if values and len(values) == len(calls):
            result[key] = sum(values)
    cached = [(usage.get("prompt_tokens_details") or {}).get("cached_tokens") for usage in known]
    if cached and len(cached) == len(calls) and all(isinstance(value, int) for value in cached):
        result["total_cached_tokens"] = sum(value for value in cached if isinstance(value, int))
    return result


def _child_observation(content: str) -> tuple[str, str | None]:
    """Replace only the native agent tool's controller log locator, not its output."""
    output, separator, metadata = content.rpartition("\n\nTool metadata: ")
    if not separator:
        return content, None
    try:
        details = ast.literal_eval(metadata)
    except (ValueError, SyntaxError):
        return content, None
    if not isinstance(details, dict) or not isinstance(details.get("log_file"), str):
        return content, None
    child_name = PurePosixPath(details["log_file"]).stem
    details["log_file"] = f"childlog://{child_name}"
    return f"{output}{separator}{details!r}", child_name


def native_to_atif(native: dict, calls: list[dict], *, trajectory_id: str, model_name: str) -> dict:
    """Keep original tool IDs, raw arguments, observations, reasoning and usage.

    Missing usage remains missing, rather than becoming an invented zero. A
    rejected malformed-arguments call remains in evidence even though ATIF
    requires arguments to be an object. Root totals include all child calls.
    Failed requests without an assistant answer remain structured evidence, not
    invented agent steps. The worker records infrastructure stops as safe native
    info, never as a terminal diagnostic user message. Explicit per-agent stops
    describe failed children without turning a recovered root run into a failure.
    Task and sandbox content are not classified or path-sanitized.
    """
    trajectories: list[dict] = []
    agent_stops = native.get("info", {}).get("agent_stops", {})
    for name, conversation in native["trajs"].items():
        if not conversation["messages"]:
            continue
        own_calls = [call for call in calls if call["name"] == name]
        steps: list[dict] = []
        tool_steps: dict[str, dict] = {}
        assistant_index = 0
        for message in conversation["messages"]:
            role = message["role"]
            if role == "tool":
                call_id = message["tool_call_id"]
                owner = tool_steps.get(call_id)
                if owner is None:
                    raise ValueError(f"native observation has no tool call: {call_id}")
                result: dict[str, Any] = {"source_call_id": call_id, "content": message.get("content")}
                if message.get("name") == "agent" and isinstance(message.get("content"), str):
                    result["content"], child_name = _child_observation(message["content"])
                    if child_name in native["trajs"]:
                        result["subagent_trajectory_ref"] = [
                            {"trajectory_id": f"{trajectory_id}:{child_name}"}
                        ]
                owner.setdefault("observation", {"results": []})["results"].append(result)
                continue
            if role not in {"system", "user", "assistant"}:
                raise ValueError(f"unsupported native role: {role}")
            step: dict = {
                "step_id": len(steps) + 1,
                "source": "agent" if role == "assistant" else role,
                "message": message.get("content") or "",
            }
            if role == "assistant":
                if message.get("reasoning_content"):
                    step["reasoning_content"] = message["reasoning_content"]
                if message.get("reasoning_signature"):
                    step.setdefault("extra", {})["reasoning_signature"] = message["reasoning_signature"]
                group = [call for call in own_calls if call["assistant_index"] == assistant_index]
                assistant_index += 1
                if group:
                    step["llm_call_count"] = len(group)
                step["model_name"] = model_name
                step["metrics"] = {"extra": {"calls": group, "sampling": SAMPLING}}
                totals = _totals(group)
                for aggregate, individual in (
                    ("total_prompt_tokens", "prompt_tokens"),
                    ("total_completion_tokens", "completion_tokens"),
                    ("total_cached_tokens", "cached_tokens"),
                ):
                    if aggregate in totals:
                        step["metrics"][individual] = totals[aggregate]
                tool_calls = []
                for tool in message.get("tool_calls") or []:
                    function = tool["function"]
                    raw = function["arguments"]
                    extra = {"native_arguments": raw}
                    try:
                        arguments = json.loads(raw)
                    except (TypeError, ValueError):
                        arguments = None
                    if not isinstance(arguments, dict):
                        arguments = {"native_raw_arguments": raw}
                        extra["arguments_not_an_object"] = True
                    call_id = tool["id"]
                    if not isinstance(call_id, str) or not call_id or call_id in tool_steps:
                        raise ValueError("native tool call IDs must be nonempty and unique")
                    tool_calls.append({"tool_call_id": call_id, "function_name": function["name"],
                                       "arguments": arguments, "extra": extra})
                    tool_steps[call_id] = step
                if tool_calls:
                    step["tool_calls"] = tool_calls
            steps.append(step)
        stop = agent_stops.get(name, {})
        extra = {
            "native_exit_status": stop.get("exit_status", native.get("info", {}).get("exit_status"))
        }
        for key in ("stop_reason", "infra_error"):
            if key in stop:
                extra[key] = stop[key]
        unanswered = [
            call for call in own_calls
            if call.get("assistant_index") not in range(assistant_index)
        ]
        if unanswered:
            extra["unanswered_model_calls"] = unanswered
        trajectories.append({
            "schema_version": "ATIF-v1.8",
            "trajectory_id": trajectory_id if name == "main" else f"{trajectory_id}:{name}",
            "agent": {"name": "mimoagent", "version": NATIVE_REVISION,
                      "model_name": model_name, "tool_definitions": conversation.get("tools"),
                      "extra": {"native_name": name, "swe_sha256": SWE_SHA256, "step_limit": 500,
                                "antihack": False, "sampling": SAMPLING}},
            "steps": steps,
            "final_metrics": _totals(own_calls),
            "extra": extra,
        })
    main = next((trajectory for trajectory in trajectories if trajectory["agent"]["extra"]["native_name"] == "main"), None)
    if main is None:
        raise ValueError("native trajectory contains no main-agent messages")
    main["subagent_trajectories"] = [trajectory for trajectory in trajectories if trajectory is not main]
    main["final_metrics"] = _totals(calls)
    main["extra"]["native_model_stats"] = native.get("info", {}).get("model_stats")
    for key in ("stop_reason", "infra_error"):
        if key in native.get("info", {}):
            main["extra"][key] = native["info"][key]
    emitted_names = {trajectory["agent"]["extra"]["native_name"] for trajectory in trajectories}
    unassigned = [call for call in calls if call["name"] not in emitted_names]
    if unassigned:
        main["extra"]["unassigned_model_calls"] = unassigned
    return main
