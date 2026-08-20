"""Convert Antigravity CLI machine-readable output into ATIF.

The CLI's ``stream-json`` transport is the structured process source. Print mode
without that transport is deliberately represented as a final-response-only
trajectory; it is not presented as process capture.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from typing import Any

JsonObject = dict[str, Any]

UNAVAILABLE_PRINT_MODE_REASON = (
    "Upstream transport (print mode without transcript capture) exposed no process stream; "
    "final response only."
)

_SECRET_KEY_RE = re.compile(r"(?:token|secret|password|credential|authorization|api[_-]?key)", re.I)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


def _redact(value: Any, *, key: str | None = None) -> Any:
    """Copy JSON data while removing credential-shaped values."""
    if key is not None and _SECRET_KEY_RE.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _BEARER_RE.sub("Bearer <redacted>", value)
    return value


def sanitize_stream_json(text: str) -> str:
    """Return only valid, credential-redacted NDJSON records.

    Antigravity writes diagnostics to stderr, but accepting only JSON objects here
    also makes the parser safe when a wrapper accidentally interleaves a diagnostic.
    """
    records: list[str] = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            records.append(json.dumps(_redact(record), ensure_ascii=False, sort_keys=True))
    return "\n".join(records) + ("\n" if records else "")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _metrics(usage: Any) -> JsonObject | None:
    if not isinstance(usage, dict):
        return None
    values: JsonObject = {}
    for source, target in (
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
        ("cache_read_tokens", "cached_tokens"),
    ):
        value = usage.get(source)
        if isinstance(value, int) and not isinstance(value, bool):
            values[target] = value
    return values or None


def _merge_usage(total: JsonObject, usage: Any) -> None:
    if not isinstance(usage, dict):
        return
    for key in ("input_tokens", "output_tokens", "cache_read_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            total[key] = int(total.get(key, 0)) + value


def _stable_call_id(step_index: int) -> str:
    return f"call_{step_index}"


def _step_from_update(step: JsonObject, *, model_name: str | None) -> JsonObject | None:
    step_type = str(step.get("step_type") or "")
    step_index = step.get("step_index")
    if not isinstance(step_index, int) or isinstance(step_index, bool):
        return None
    source = (
        "user"
        if step_type == "user_input"
        else "system"
        if step_type in {"checkpoint", "error"}
        else "agent"
    )
    message = _text(step.get("text_delta"))
    result: JsonObject = {
        "step_id": step_index + 1,
        "source": source,
        "message": message,
    }
    if step.get("timestamp") is not None:
        result["timestamp"] = step["timestamp"]
    if model_name and source == "agent":
        result["model_name"] = model_name
    metrics = _metrics(step.get("usage"))
    if metrics:
        result["metrics"] = metrics
    if source == "agent":
        result["llm_call_count"] = 1 if step_type == "agent_response" else 0

    tool_info = step.get("tool_info")
    if step_type == "tool" and isinstance(tool_info, dict):
        call_id = _stable_call_id(step_index)
        parameters = tool_info.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {"value": parameters} if parameters is not None else {}
        result["tool_calls"] = [
            {
                "tool_call_id": call_id,
                "function_name": str(
                    tool_info.get("name") or step.get("tool_name") or "unknown_tool"
                ),
                "arguments": parameters,
            }
        ]
        output = tool_info.get("output")
        error = tool_info.get("error")
        observation: JsonObject = {"source_call_id": call_id, "content": _text(output)}
        if isinstance(error, dict):
            error_message = _text(error.get("message") or error)
            observation["content"] = error_message
            observation["extra"] = {
                "error": True,
                "error_type": str(error.get("type") or "unknown"),
            }
        elif error is not None:
            observation["content"] = _text(error)
            observation["extra"] = {"error": True}
        result["observation"] = {"results": [observation]}
    elif step_type == "error":
        result["message"] = _text(
            step.get("error") or step.get("message") or step.get("text_delta")
        )
    return result


def parse_stream_json_to_atif(
    stream_text: str,
    *,
    session_id: str | None = None,
    agent_name: str = "antigravity-cli",
    agent_version: str = "unknown",
    model_name: str | None = None,
    raw_source: str = "antigravity-cli.stream.jsonl",
    job_id: str | None = None,
    trial_id: str | None = None,
    schema_version: str = "ATIF-v1.6",
) -> JsonObject | None:
    """Convert Antigravity's documented ``stream-json`` NDJSON to ATIF.

    ACTIVE deltas are accumulated by zero-based ``step_index``. The resulting
    steps are emitted in source order and tool errors become ATIF observations.
    """
    updates: OrderedDict[int, JsonObject] = OrderedDict()
    result_payload: JsonObject | None = None
    total_usage: JsonObject = {}
    result_usage: Any = None
    detected_session = session_id
    detected_model = model_name
    for line in stream_text.splitlines():
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("event") == "init":
            init = event.get("init")
            if isinstance(init, dict):
                detected_session = detected_session or _as_str(init.get("conversation_id"))
                detected_model = detected_model or _as_str(init.get("model"))
        elif event.get("event") == "step_update":
            update = event.get("step_update")
            if not isinstance(update, dict):
                continue
            index = update.get("step_index")
            if not isinstance(index, int) or isinstance(index, bool):
                continue
            current = updates.setdefault(
                index, {"step_index": index, "step_type": update.get("step_type")}
            )
            for key, value in update.items():
                if key == "text_delta":
                    current["text_delta"] = str(current.get("text_delta") or "") + _text(value)
                elif key == "usage":
                    current["usage"] = value
                elif key == "state":
                    current["state"] = value
                else:
                    current[key] = value
                if key == "conversation_id" and detected_session is None:
                    detected_session = _as_str(value)
        elif event.get("event") == "result" and isinstance(event.get("result"), dict):
            result_payload = event["result"]
            result_usage = result_payload.get("usage")
            detected_session = detected_session or _as_str(result_payload.get("conversation_id"))
    if isinstance(result_usage, dict):
        _merge_usage(total_usage, result_usage)
    else:
        for update in updates.values():
            if update.get("state") == "DONE":
                _merge_usage(total_usage, update.get("usage"))

    steps: list[JsonObject] = []
    for index in sorted(updates):
        step = _step_from_update(updates[index], model_name=detected_model)
        if step is not None:
            step["step_id"] = len(steps) + 1
            steps.append(step)
    final_response = _text(result_payload.get("response")) if result_payload else ""
    if final_response and not any(
        step.get("source") == "agent" and step.get("message") == final_response for step in steps
    ):
        next_id = max((int(step["step_id"]) for step in steps), default=0) + 1
        steps.append(
            {
                "step_id": next_id,
                "source": "agent",
                "model_name": detected_model,
                "message": final_response,
                "llm_call_count": 1,
            }
        )
    if result_payload and str(result_payload.get("status") or "SUCCESS") != "SUCCESS":
        next_id = max((int(step["step_id"]) for step in steps), default=0) + 1
        steps.append(
            {
                "step_id": next_id,
                "source": "system",
                "message": _text(result_payload.get("error") or result_payload.get("status")),
            }
        )
    if not steps:
        return None

    agent: JsonObject = {"name": agent_name, "version": agent_version}
    if detected_model:
        agent["model_name"] = detected_model
    identity = {
        "job_id": job_id or "unknown",
        "trial_id": trial_id or "unknown",
        "agent": agent_name,
        "model": detected_model or "unknown",
    }
    payload: JsonObject = {
        "schema_version": schema_version,
        "session_id": detected_session or "unknown",
        "agent": agent,
        "steps": steps,
        "extra": {
            "identity": identity,
            "raw_source": raw_source,
            "transport": "stream-json",
        },
    }
    if total_usage:
        payload["final_metrics"] = {
            "total_prompt_tokens": int(total_usage.get("input_tokens", 0)),
            "total_completion_tokens": int(total_usage.get("output_tokens", 0)),
            "total_cached_tokens": int(total_usage.get("cache_read_tokens", 0)),
            "total_steps": len(steps),
        }
    return payload


def create_fallback_atif_for_print_mode(
    final_response: str,
    *,
    session_id: str | None = None,
    agent_name: str = "antigravity-cli",
    agent_version: str = "unknown",
    model_name: str | None = None,
    user_prompt: str | None = None,
    unavailable_reason: str = UNAVAILABLE_PRINT_MODE_REASON,
    raw_source: str = "antigravity-cli.txt",
    job_id: str | None = None,
    trial_id: str | None = None,
    schema_version: str = "ATIF-v1.6",
) -> JsonObject:
    """Represent text print mode without falsely claiming process capture."""
    steps: list[JsonObject] = []
    if user_prompt:
        steps.append({"step_id": 1, "source": "user", "message": user_prompt})
    agent_step: JsonObject = {
        "step_id": len(steps) + 1,
        "source": "agent",
        "message": final_response,
    }
    if model_name:
        agent_step["model_name"] = model_name
    steps.append(agent_step)
    agent: JsonObject = {"name": agent_name, "version": agent_version}
    if model_name:
        agent["model_name"] = model_name
    return {
        "schema_version": schema_version,
        "session_id": session_id or "unknown",
        "agent": agent,
        "notes": unavailable_reason,
        "extra": {
            "identity": {
                "job_id": job_id or "unknown",
                "trial_id": trial_id or "unknown",
                "agent": agent_name,
                "model": model_name or "unknown",
            },
            "raw_source": raw_source,
            "transport": "print",
            "capture": "final-response-only",
        },
        "steps": steps,
    }


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def redact_digest(value: str) -> str:
    """Stable digest for diagnostics without retaining credential-shaped input."""
    return hashlib.sha256(value.encode()).hexdigest()
