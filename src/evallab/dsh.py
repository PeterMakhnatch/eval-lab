"""Read DeepSeek Harness sessions as ATIF.

DSH writes one append-only event stream per session under
``$DSH_HOME/sessions/--<normalized-cwd>--/<session-id>/session.v3.jsonl``
(Zstandard-compressed by default, plain when persistence is configured with
``compression: none``). A headless run — ``dsh --profile headless "<task>"`` —
writes the same stream, so a benchmark trial can be reconstructed from the
session rather than scraped from stdout.

That matters for this lab specifically: ``trajectory_quality`` quarantines a
billable trial whose ``agent/trajectory.json`` is missing, and ``traj.py``
reads that same file to build the analysis corpus. An adapter that only
captured DSH's final answer would land in the corpus as
``missing_trajectory_file``.

The event vocabulary this module reads is the one observed in real sessions:

``turn/start`` ``turn/end`` ``step/start`` ``step/end`` ``user/message``
``system/message`` ``assistant/message`` ``tool/call`` ``tool/result``

plus the ``session`` header. Each event carries ``{type, seq, time, data}``
except the header, which carries the identity fields directly. Unknown types
are ignored rather than failing the conversion, so a newer DSH that adds
events still yields a usable trajectory instead of an unreadable one.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import OrderedDict
from pathlib import Path
from typing import Any

#: Agent name the lab registers this transport under.
DSH_AGENT_NAME = "deepseek-harness"

#: Newest version ``evidence/atif.SUPPORTED_SCHEMA_VERSIONS`` accepts.
DEFAULT_SCHEMA_VERSION = "ATIF-v1.7"

#: Session files are ``session.v3.jsonl`` with or without ``.zstd``.
SESSION_GLOB = "session.v3.jsonl*"

#: Zstandard frame magic. A file without it is already plain JSONL.
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

_SECRET_KEY = re.compile(
    r"(token|secret|password|credential|authorization|api[_-]?key)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_REDACTED = "<redacted>"

JsonObject = dict[str, Any]


class SessionUnreadable(RuntimeError):
    """The session bytes could not be decompressed with anything available."""


def looks_compressed(raw: bytes) -> bool:
    """Return whether these bytes are a Zstandard frame rather than plain JSONL."""
    return raw.startswith(_ZSTD_MAGIC)


def decompress_session(raw: bytes) -> str:
    """Return the session text, decompressing only when the bytes are Zstandard.

    Three decompressors are tried in order because the lab runs on Python 3.12
    (no stdlib Zstandard) and 3.14 (stdlib ``compression.zstd``), and we would
    rather not add a dependency for one column of one artifact. Callers that
    can choose should prefer configuring DSH for plain JSONL; this exists so an
    already-written session is never unreadable.
    """
    if not looks_compressed(raw):
        return raw.decode("utf-8", errors="replace")

    try:  # Python 3.14+
        from compression import zstd  # type: ignore[attr-defined]

        return zstd.decompress(raw).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - any failure means "try the next one"
        pass

    try:
        import zstandard  # type: ignore[import-not-found]

        return zstandard.ZstdDecompressor().decompress(raw).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    executable = shutil.which("zstd")
    if executable:
        completed = subprocess.run(
            [executable, "-dc"],
            input=raw,
            capture_output=True,
            check=False,
        )
        if completed.returncode == 0:
            return completed.stdout.decode("utf-8", errors="replace")

    raise SessionUnreadable(
        "cannot decompress DSH session: no Zstandard decoder available. "
        "Use Python 3.14's compression.zstd, install the `zstandard` package, "
        "put `zstd` on PATH, or configure DSH session persistence with "
        "`compression: none`."
    )


def read_session_file(path: Path) -> str:
    """Read one session file, transparently decompressing it."""
    return decompress_session(path.read_bytes())


def newest_session_file(sessions_root: Path) -> Path | None:
    """Return the most recently written session file under a ``sessions/`` root.

    A trial runs one task in one container, so "newest" is enough to identify
    it. The directory level is a slug of the run cwd
    (``--<normalized-cwd>--``), which the caller should not have to predict.
    """
    if not sessions_root.is_dir():
        return None
    candidates = [path for path in sessions_root.rglob(SESSION_GLOB) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _redact_nested_json(value: str) -> str | None:
    """Redact inside a JSON-encoded string payload, or return None.

    DSH carries tool arguments as a JSON *string* (``"arguments": "{\"apiKey\":
    ...}"``), so a credential that appears as a field of those arguments is
    invisible to a walk over the event object alone. Without this, a trial that
    read an environment file would write the key into durable evidence — the
    exact leak the DeepSeek proxy exists to prevent.
    """
    if not value.lstrip().startswith(("{", "[")):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, (dict, list)):
        return None
    return json.dumps(_redact(parsed), ensure_ascii=False, sort_keys=True)


def _redact(value: Any, *, key: str | None = None) -> Any:
    # A credential is a string. Matching the key alone would also redact
    # `inputTokens`, `outputTokens` and `cacheReadTokens` — the token *counts*
    # this pipeline exists to report — and silently zero every metric. So the
    # name rule only fires on string values; containers are still walked.
    if key is not None and isinstance(value, str) and _SECRET_KEY.search(key):
        return _REDACTED
    if isinstance(value, dict):
        return {str(name): _redact(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        scrubbed = _BEARER.sub("Bearer <redacted>", value)
        nested = _redact_nested_json(scrubbed)
        return nested if nested is not None else scrubbed
    return value


def sanitize_session(text: str) -> str:
    """Redact credential-shaped material from a session stream, line by line.

    A DSH session records the arguments of every tool call, so a trial that
    read an environment file can put a provider key into the trajectory. The
    lab's rule is that the raw key never reaches durable evidence, and the
    projection is rebuilt from these bytes.
    """
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            lines.append(_BEARER.sub("Bearer <redacted>", line))
            continue
        lines.append(json.dumps(_redact(parsed), ensure_ascii=False, sort_keys=True))
    return "\n".join(lines) + ("\n" if lines else "")


def _text(value: Any) -> str:
    """Flatten DSH message content into text.

    Content is a list of parts (``{"type": "text", "text": "..."}`` and
    siblings); anything unrecognized degrades to its JSON form rather than
    being dropped, because a silent omission would make a trajectory look
    cleaner than it was.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
            elif item is not None:
                parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
        return "\n".join(part for part in parts if part)
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _message_text(message: Any) -> str:
    if isinstance(message, dict):
        return _text(message.get("content"))
    return _text(message)


def _arguments(raw: Any) -> JsonObject:
    """Tool arguments arrive as a JSON string; keep them structured when possible."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    if raw is None:
        return {}
    return {"value": raw}


def _usage_metrics(usage: Any) -> JsonObject | None:
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("inputTokens")
    output_tokens = usage.get("outputTokens")
    cache_tokens = usage.get("cacheReadTokens")
    if input_tokens is None and output_tokens is None and cache_tokens is None:
        return None
    metrics: JsonObject = {}
    if isinstance(input_tokens, int) and not isinstance(input_tokens, bool):
        metrics["prompt_tokens"] = input_tokens
    if isinstance(output_tokens, int) and not isinstance(output_tokens, bool):
        metrics["completion_tokens"] = output_tokens
    if isinstance(cache_tokens, int) and not isinstance(cache_tokens, bool):
        metrics["cached_tokens"] = cache_tokens
    return metrics or None


def _events(text: str) -> list[JsonObject]:
    events: list[JsonObject] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def parse_session_to_atif(
    text: str,
    *,
    session_id: str | None = None,
    agent_name: str = DSH_AGENT_NAME,
    agent_version: str = "unknown",
    model_name: str | None = None,
    raw_source: str = "session.v3.jsonl",
    job_id: str | None = None,
    trial_id: str | None = None,
    schema_version: str = DEFAULT_SCHEMA_VERSION,
) -> JsonObject | None:
    """Convert a DSH session stream into an ATIF trajectory.

    Events are folded onto their ``(turn, step)`` coordinate so a step that
    calls a tool and observes its result stays one ATIF step with one
    ``tool_calls`` entry and one matching observation, rather than three
    unrelated steps. Steps are emitted in ``(turn, step)`` order; the session's
    own ``seq`` only breaks ties.
    """
    events = _events(text)
    if not events:
        return None

    detected_session = session_id
    detected_model = model_name
    turn_end_reason: dict[int, Any] = {}
    steps_by_coordinate: OrderedDict[tuple[int, int], JsonObject] = OrderedDict()
    first_seq: dict[tuple[int, int], int] = {}
    total_input = total_output = total_cached = 0

    def coordinate(data: JsonObject, fallback_seq: int) -> tuple[int, int]:
        turn = data.get("turn")
        step = data.get("step")
        turn = turn if isinstance(turn, int) and not isinstance(turn, bool) else 0
        step = step if isinstance(step, int) and not isinstance(step, bool) else 0
        return turn, step

    def step_at(key: tuple[int, int], seq: int) -> JsonObject:
        if key not in steps_by_coordinate:
            steps_by_coordinate[key] = {"step_id": 0, "source": "agent", "message": ""}
            first_seq[key] = seq
        return steps_by_coordinate[key]

    for position, event in enumerate(events):
        kind = event.get("type")
        data = event.get("data")
        seq = event.get("seq")
        seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else position

        if kind == "session":
            detected_session = detected_session or (
                event.get("id") if isinstance(event.get("id"), str) else None
            )
            continue
        if not isinstance(data, dict):
            continue

        if kind == "turn/end":
            turn = data.get("turn")
            if isinstance(turn, int) and not isinstance(turn, bool):
                turn_end_reason[turn] = data.get("reason")
            continue
        if kind not in {
            "user/message",
            "system/message",
            "assistant/message",
            "tool/call",
            "tool/result",
            "assistant/attempt",
        }:
            continue

        key = coordinate(data, seq)
        step = step_at(key, seq)

        if kind == "user/message":
            step["source"] = "user"
            step["message"] = _message_text(data.get("content")) or step.get("message", "")
        elif kind == "system/message":
            if step.get("source") != "user":
                step["source"] = "system"
            step["message"] = _message_text(data.get("message")) or step.get("message", "")
        elif kind == "assistant/message":
            message = data.get("message")
            if isinstance(message, dict):
                source = message.get("source")
                if isinstance(source, dict) and isinstance(source.get("model"), str):
                    detected_model = detected_model or source["model"]
            step["source"] = "agent" if step.get("source") != "user" else "user"
            step["message"] = _message_text(message) or step.get("message", "")
            step["llm_call_count"] = int(step.get("llm_call_count") or 0) + 1
            metrics = _usage_metrics(data.get("usage"))
            if metrics:
                step["metrics"] = metrics
            usage = data.get("usage")
            if isinstance(usage, dict):
                for field, bucket in (
                    ("inputTokens", "input"),
                    ("outputTokens", "output"),
                    ("cacheReadTokens", "cached"),
                ):
                    value = usage.get(field)
                    if isinstance(value, int) and not isinstance(value, bool):
                        if bucket == "input":
                            total_input += value
                        elif bucket == "output":
                            total_output += value
                        else:
                            total_cached += value
        elif kind == "tool/call":
            call_id = data.get("callId")
            call_id = call_id if isinstance(call_id, str) and call_id else f"call_{seq}"
            calls = step.setdefault("tool_calls", [])
            calls.append(
                {
                    "tool_call_id": call_id,
                    "function_name": str(data.get("name") or "unknown_tool"),
                    "arguments": _arguments(data.get("arguments")),
                }
            )
            if step.get("source") == "user":
                step["source"] = "agent"
        elif kind == "tool/result":
            message = data.get("message")
            call_id = None
            if isinstance(message, dict):
                source = message.get("source")
                if isinstance(source, dict) and isinstance(source.get("callId"), str):
                    call_id = source["callId"]
            call_id = call_id or f"call_{seq}"
            observation: JsonObject = {
                "source_call_id": call_id,
                "content": _message_text(message),
            }
            error = data.get("error")
            if isinstance(error, dict):
                observation["content"] = str(error.get("code") or error.get("name") or "error")
                observation["extra"] = {
                    "error": True,
                    "error_type": str(error.get("name") or "unknown"),
                }
            results = step.setdefault("observation", {}).setdefault("results", [])
            results.append(observation)
            if step.get("source") == "user":
                step["source"] = "agent"

    ordered = sorted(steps_by_coordinate.items(), key=lambda item: (item[0], first_seq[item[0]]))
    steps: list[JsonObject] = []
    for _key, step in ordered:
        if not step.get("message") and not step.get("tool_calls") and not step.get("observation"):
            continue
        step["step_id"] = len(steps) + 1
        if step.get("source") == "agent":
            step.setdefault("model_name", detected_model)
        steps.append(step)
    if not steps:
        return None

    agent: JsonObject = {"name": agent_name, "version": agent_version}
    if detected_model:
        agent["model_name"] = detected_model
    payload: JsonObject = {
        "schema_version": schema_version,
        "session_id": detected_session or "unknown",
        "agent": agent,
        "steps": steps,
        "extra": {
            "identity": {
                "job_id": job_id or "unknown",
                "trial_id": trial_id or "unknown",
                "agent": agent_name,
                "model": detected_model or "unknown",
            },
            "raw_source": raw_source,
            "transport": "session.v3.jsonl",
        },
    }
    if turn_end_reason:
        payload["extra"]["turn_end_reason"] = {
            str(turn): reason for turn, reason in sorted(turn_end_reason.items())
        }
    payload["final_metrics"] = {
        "total_prompt_tokens": total_input,
        "total_completion_tokens": total_output,
        "total_cached_tokens": total_cached,
        "total_steps": len(steps),
    }
    return payload


def create_fallback_atif_from_final_message(
    final_message: str,
    *,
    session_id: str | None = None,
    agent_name: str = DSH_AGENT_NAME,
    agent_version: str = "unknown",
    model_name: str | None = None,
    raw_source: str = "final-message.txt",
    job_id: str | None = None,
    trial_id: str | None = None,
    schema_version: str = DEFAULT_SCHEMA_VERSION,
) -> JsonObject | None:
    """Build a one-step trajectory from stdout alone.

    The headless profile prints the final assistant message and nothing else,
    so this is the floor, not the goal: it keeps a trial countable when the
    session file is missing, at the cost of every tool call. Prefer
    :func:`parse_session_to_atif` whenever a session survived.
    """
    text = final_message.strip()
    if not text:
        return None
    agent: JsonObject = {"name": agent_name, "version": agent_version}
    if model_name:
        agent["model_name"] = model_name
    return {
        "schema_version": schema_version,
        "session_id": session_id or "unknown",
        "agent": agent,
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "message": text,
                "model_name": model_name,
                "llm_call_count": 1,
            }
        ],
        "extra": {
            "identity": {
                "job_id": job_id or "unknown",
                "trial_id": trial_id or "unknown",
                "agent": agent_name,
                "model": model_name or "unknown",
            },
            "raw_source": raw_source,
            "transport": "final-message",
            "degraded": True,
        },
        "final_metrics": {
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_cached_tokens": 0,
            "total_steps": 1,
        },
    }
