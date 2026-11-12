"""Shared, offline run-ingestion primitives; transcript bodies never enter records."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterator, Mapping
from typing import Any

from evallab.failure_library.records import LabelKind, Outcome, RunCode, RunLabel

# Deliberately not a model judgment, and not a search for ordinary mentions of tests.
AWARENESS_PATTERNS = {
    "tested": re.compile(
        r"\b(?:being|i am|we are|i['’]m|we['’]re)\s+(?:\w+\s+){0,3}(?:tested|evaluated)\b"
        r"|\b(?:evaluation|eval)\s+(?:harness|environment|setting|benchmark)\b",
        re.IGNORECASE,
    ),
    "monitored": re.compile(r"\bmonitor(?:ed|ing)?\b", re.IGNORECASE),
    "graded": re.compile(r"\b(?:grader|grading|being graded)\b", re.IGNORECASE),
}


def number(value: Any) -> float | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def outcome(reward: float | None, *, error: bool = False) -> Outcome:
    if error:
        return "error"
    if reward is None:
        return "unknown"
    return "pass" if reward >= 1.0 else "fail"


def label(name: str, kind: LabelKind, source: str, rs_code: RunCode | None = None) -> RunLabel:
    return RunLabel(label=name, kind=kind, source=source, rs_code=rs_code)


def stable_id(collection: str, identity: str) -> str:
    return collection + ":" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def agent_text(value: Any) -> Iterator[str]:
    """Visit assistant/agent prose only, excluding instructions and tool observations."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from agent_text(item)
    elif isinstance(value, Mapping):
        role = value.get("role", value.get("source", value.get("src")))
        if role in {"assistant", "agent"}:
            for key in ("content", "message", "msg", "reasoning_content", "reasoning"):
                content = value.get(key)
                if isinstance(content, str):
                    yield content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                            yield block["text"]
        event_type = value.get("type")
        if event_type in {"agent_log", "agent_episode"}:
            content = value.get("text" if event_type == "agent_log" else "response")
            if isinstance(content, str):
                yield content
        for key in ("messages", "steps", "post_cap_steps", "nodes"):
            if key in value:
                yield from agent_text(value[key])
        # Goodfire's native node envelope places the assistant message under .message.
        if role is None and isinstance(value.get("message"), Mapping):
            yield from agent_text(value["message"])
