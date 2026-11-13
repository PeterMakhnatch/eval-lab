"""Historical OMP event parsing and a permanently sealed live transport route.

A separate user-derived reader executed a built-in mutating tool despite frozen
no-tools/no-skills/no-extensions flags; this does not establish native METR tool
use. Read-only isolation is unproven, so live calls and Inspect registration deny
unconditionally, without an override or SDK fallback. Historical stream parsing,
prompt rendering and environment filtering remain available as pure utilities.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

KIMI_MODEL = "kimi-code/k3.1-pro-highspeed"
INSPECT_MODEL = f"omp/{KIMI_MODEL}"
_KEY_ENV = re.compile(r"(?:^|_)(?:API_KEY|AUTH_TOKEN|OAUTH_TOKEN|ACCESS_TOKEN)$")
_REFUSAL = re.compile(
    r"^\s*(?:sorry[,\s]+(?:but\s+)?|i(?:['’]m| am) sorry[,\s]+(?:but\s+)?)?"
    r"(?:i (?:cannot|can['’]t|will not|won['’]t) (?:help|assist|comply|provide)|"
    r"i(?:['’]m| am) unable to (?:help|assist|comply)|cyber_policy\b)",
    re.IGNORECASE,
)


class OmpRouteError(RuntimeError):
    """An unattested, refused, incomplete or failed subscription response."""


@dataclass(frozen=True)
class OmpReply:
    """Attested final text and native OMP usage (no reasoning text)."""

    text: str
    served_model: str
    response_id: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.output_tokens
        )


def subscription_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Keep stored OAuth discoverable, but remove inherited provider credentials."""
    return {key: value for key, value in environ.items() if not _KEY_ENV.search(key)}


def _tokens(usage: Mapping[str, Any], name: str, *, required: bool = False) -> int:
    value = usage.get(name, None if required else 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OmpRouteError(f"invalid or missing usage.{name}")
    return value


def parse_omp_stream(stdout: str, requested_model: str = KIMI_MODEL) -> OmpReply:
    """Fail closed on fallback, identity mismatch, refusal and truncated streams."""
    end = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue  # OMP startup diagnostics are not JSON events.
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type", ""))
        if (
            "fallback" in kind.lower()
            or kind == "model_changed"
            or event.get("resolvedModelIsFallback") is True
        ):
            raise OmpRouteError("OMP model fallback or change")
        if kind == "agent_end":
            if end is not None:
                raise OmpRouteError("multiple agent_end events")
            end = event
    if end is None or not isinstance(end.get("messages"), list):
        raise OmpRouteError("truncated stream: no agent_end messages")
    replies = [
        message
        for message in end["messages"]
        if isinstance(message, dict) and message.get("role") == "assistant"
    ]
    if len(replies) != 1:
        raise OmpRouteError("expected exactly one assistant turn")
    reply = replies[0]
    served = f"{reply.get('provider')}/{reply.get('model')}"
    if served != requested_model:
        raise OmpRouteError(f"served-model mismatch: requested {requested_model}, served {served}")
    if reply.get("stopReason") != "stop" or reply.get("errorMessage") or reply.get("refusal"):
        raise OmpRouteError(f"refused or incomplete response: stopReason={reply.get('stopReason')!r}")
    content = reply.get("content")
    if not isinstance(content, list) or any(
        not isinstance(block, dict) or block.get("type") not in {"text", "thinking"}
        for block in content
    ):
        raise OmpRouteError("assistant response contains non-text content or refusal")
    parts = [block.get("text") for block in content if block["type"] == "text"]
    if not parts or any(not isinstance(part, str) for part in parts):
        raise OmpRouteError("missing assistant text")
    text = "".join(parts)
    if not text.strip() or _REFUSAL.search(text):
        raise OmpRouteError("empty completion or policy refusal")
    usage = reply.get("usage")
    if not isinstance(usage, dict):
        raise OmpRouteError("missing response usage")
    response_id = reply.get("responseId")
    return OmpReply(
        text=text,
        served_model=served,
        response_id=response_id if isinstance(response_id, str) else None,
        input_tokens=_tokens(usage, "input", required=True),
        output_tokens=_tokens(usage, "output", required=True),
        cache_read_tokens=_tokens(usage, "cacheRead"),
        cache_write_tokens=_tokens(usage, "cacheWrite"),
        reasoning_tokens=_tokens(usage, "reasoningTokens"),
    )
