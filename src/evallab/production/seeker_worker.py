"""Stored witness route validation, split guards, and evidence custody."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

MECHANISM_CLASSES = frozenset(
    {"weak_check", "grader_tamperable", "answer_reachable", "exploit_grader_gap"}
)



def _omp_stream_messages(stream_text: str) -> list[Mapping[str, Any]]:
    """Messages carried by the ``agent_end`` events of an OMP JSON event stream."""
    messages: list[Mapping[str, Any]] = []
    for line in stream_text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, Mapping) and event.get("type") == "agent_end":
            messages.extend(m for m in event.get("messages") or () if isinstance(m, Mapping))
    return messages


def _route_matches(requested: str, provider: str, model: str) -> bool:
    """Whether ``provider``/``model`` is the requested ``provider/model`` route.

    A ``:thinking`` selector on either side is not part of the route, and a
    vendor-prefixed route served through ``openrouter`` (e.g.
    ``meta/muse-spark-open`` served as ``provider='openrouter'``,
    ``model='meta/muse-spark-open'``) matches that exact vendor/model pair.
    """
    expected_provider, _, expected_model = requested.partition("/")
    want_provider = expected_provider.strip()
    want_model = expected_model.split(":", 1)[0].strip()
    got_provider = provider.strip()
    got_model = model.split(":", 1)[0].strip()
    if got_provider == want_provider and got_model == want_model:
        return True
    return got_provider == "openrouter" and got_model == f"{want_provider}/{want_model}"


def _stream_served_route(
    messages: Sequence[Mapping[str, Any]], requested: str | None
) -> tuple[str | None, str | None]:
    """Provider/model that answered an OMP session, counting every assistant message.

    A session answered by more than one route reports the first route that is not
    the requested one, so a mid-session fallback reads as a mismatch instead of
    inheriting the requested route's name.
    """
    routes = [
        (str(message["provider"]), str(message["model"]))
        for message in messages
        if message.get("role") == "assistant" and message.get("provider") and message.get("model")
    ]
    if requested and "/" in requested:
        for provider, model in routes:
            if not _route_matches(requested, provider, model):
                return provider, model
    return routes[0] if routes else (None, None)




def _parse_omp_json_stdout(raw_text: str) -> tuple[str, str | None, str | None]:
    """Return final text and actual provider/model from OMP's JSON event stream."""
    text = ""
    served_provider: str | None = None
    served_model: str | None = None
    for message in _omp_stream_messages(raw_text):
        if message.get("role") != "assistant":
            continue
        parts = [
            part.get("text", "")
            for part in message.get("content") or []
            if part.get("type") == "text"
        ]
        if any(parts):
            text = "".join(parts)
            served_provider = str(message["provider"]) if message.get("provider") else None
            served_model = str(message["model"]) if message.get("model") else None
    return text, served_provider, served_model
