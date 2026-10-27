"""Opt-in Inspect provider backed by OMP's stored provider credentials.

Call :func:`register_omp_provider` before ``get_model('omp/<provider>/<model>')``.
Inspect is optional and imported only by registration. The transport preserves the
retained CM-9 no-tools/no-fallback command, never reads or forwards API keys, and
attests the served model before returning any completion. JSON-schema generation
is represented as a system instruction; the caller still validates the response.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

KIMI_MODEL = "kimi-code/k3.1-pro-highspeed"
INSPECT_MODEL = f"omp/{KIMI_MODEL}"
NO_FALLBACK_OVERLAY = (
    'memory:\n  backend: "off"\n'
    "retry:\n  enabled: false\n  modelFallback: false\n  fallbackChains: {}\n"
)
_KEY_ENV = re.compile(r"(?:^|_)(?:API_KEY|AUTH_TOKEN|OAUTH_TOKEN|ACCESS_TOKEN)$")
_REFUSAL = re.compile(
    r"^\s*(?:sorry[,\s]+|i(?:'m| am) sorry[,\s]+)?"
    r"(?:i (?:cannot|can't|will not|won't) (?:help|assist|comply|provide)|"
    r"i(?:'m| am) unable to (?:help|assist|comply)|cyber_policy\b)",
    re.IGNORECASE,
)
_PROCESS_SLOTS = threading.BoundedSemaphore(4)
_RECEIPT_LOCK = threading.Lock()
_PROVIDER: Any = None


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


def render_prompt(messages: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    """Preserve text turns without importing Inspect or silently dropping media/tools."""
    system: list[str] = []
    turns: list[tuple[str, str]] = []
    for message in messages:
        role = message.get("role")
        if role not in {"system", "user", "assistant"} or message.get("tool_calls"):
            raise ValueError("OMP provider accepts text messages only (no tools)")
        content = message.get("content")
        if isinstance(content, list):
            if any(
                not isinstance(part, dict)
                or part.get("type") != "text"
                or not isinstance(part.get("text"), str)
                for part in content
            ):
                raise ValueError("OMP provider accepts text content only")
            content = "".join(part["text"] for part in content)
        if not isinstance(content, str):
            raise ValueError("OMP provider requires textual content")
        if role == "system":
            if turns:
                raise ValueError("system messages must precede conversation turns")
            system.append(content)
        else:
            turns.append((role, content))
    if not turns:
        raise ValueError("OMP provider requires a conversation turn")
    prompt = (
        turns[0][1]
        if len(turns) == 1 and turns[0][0] == "user"
        else "\n\n".join(f"[{role}]\n{text}" for role, text in turns)
    )
    return "\n\n".join(system), prompt


def _append_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _RECEIPT_LOCK, path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(receipt, separators=(",", ":")) + "\n")


def call_omp(
    system: str,
    prompt: str,
    *,
    model: str = KIMI_MODEL,
    timeout: float = 240,
    receipt_path: Path | None = None,
    halt_path: Path | None = None,
) -> OmpReply:
    """Run one isolated CM-9 transport call, bounded to four child processes."""
    if not re.fullmatch(r"[^/\s]+/\S+", model):
        raise ValueError("OMP model must be an explicit provider/model route")
    receipt: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(),
        "requested_model": model,
        "served_model": None,
        "ok": False,
        "usd": 0 if model.split("/", 1)[0] in {"kimi-code", "xai-oauth"} else None,
        "basis": "Stored OMP credentials; API keys stripped; no fallback; subscription spend is caller-authorized",
    }
    with _PROCESS_SLOTS:
        if halt_path is not None and halt_path.exists():
            raise OmpRouteError("campaign halted before subprocess start")
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="evallab-omp-") as work:
                overlay = Path(work) / "overlay.yml"
                overlay.write_text(NO_FALLBACK_OVERLAY, encoding="utf-8")
                argv = [
                    "omp", "-p", "--mode", "json", "--no-session", "--no-tools", "--no-lsp",
                    "--no-extensions", "--no-skills", "--no-rules", "--no-title", "--no-pty",
                    "--config", str(overlay), "--model", model,
                    "--system-prompt", system or "You are a text-only analysis assistant.",
                    "--thinking", "low",
                ]
                process = subprocess.run(
                    argv, input=prompt, capture_output=True, text=True, timeout=timeout,
                    cwd=work, env=subscription_env(os.environ),
                )
            if process.returncode:
                raise OmpRouteError(f"OMP exited with status {process.returncode}")
            reply = parse_omp_stream(process.stdout, model)
            receipt.update(
                ok=True, served_model=reply.served_model, response_id=reply.response_id,
                input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
                cache_read_tokens=reply.cache_read_tokens, cache_write_tokens=reply.cache_write_tokens,
                reasoning_tokens=reply.reasoning_tokens, fallback=False,
            )
            return reply
        except (OmpRouteError, OSError, subprocess.TimeoutExpired) as exc:
            receipt["error"] = type(exc).__name__ + ": " + str(exc)[:300]
            raise
        finally:
            receipt["elapsed_s"] = round(time.monotonic() - started, 3)
            if receipt_path is not None:
                _append_receipt(receipt_path, receipt)


def register_omp_provider() -> Any:
    """Lazily register and return Inspect's ``omp`` ModelAPI factory (idempotent)."""
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER
    from inspect_ai.model import (  # ty: ignore[unresolved-import]
        ChatCompletionChoice,
        ChatMessageAssistant,
        ModelAPI,
        ModelOutput,
        ModelUsage,
        modelapi,
    )

    @modelapi(name="omp")
    class OmpModelAPI(ModelAPI):
        def __init__(
            self,
            model_name: str,
            base_url: str | None = None,
            api_key: str | None = None,
            config: Any = None,
            *,
            receipt_path: str | None = None,
            halt_path: str | None = None,
            timeout: float = 240,
        ) -> None:
            if not re.fullmatch(r"[^/\s]+/\S+", model_name) or base_url is not None or api_key is not None:
                raise ValueError("OMP provider requires an explicit provider/model route and stored credentials")
            super().__init__(model_name=model_name, config=config)
            self.receipt_path = Path(receipt_path) if receipt_path else None
            self.halt_path = Path(halt_path) if halt_path else None
            self.timeout = timeout

        def max_connections(self) -> int:
            return 4

        def is_rate_limit(self, ex: BaseException) -> bool:
            return False  # No provider retry/fallback; scanner owns its native retries.

        async def generate(self, input: Any, tools: Any, tool_choice: Any, config: Any) -> Any:
            if tools:
                raise ValueError("OMP provider does not expose tools")
            system, prompt = render_prompt([message.model_dump() for message in input])
            if config.response_schema is not None:
                schema = config.response_schema.model_dump(mode="json")["json_schema"]
                system += (
                    "\n\nReturn only one JSON object, without Markdown fences, matching this JSON schema:\n"
                    + json.dumps(schema, separators=(",", ":"))
                )
            reply = await asyncio.to_thread(
                call_omp, system, prompt, model=self.model_name,
                timeout=self.timeout, receipt_path=self.receipt_path, halt_path=self.halt_path,
            )
            return ModelOutput(
                model=reply.served_model,
                choices=[ChatCompletionChoice(
                    message=ChatMessageAssistant(content=reply.text), stop_reason="stop"
                )],
                usage=ModelUsage(
                    input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
                    input_tokens_cache_read=reply.cache_read_tokens,
                    input_tokens_cache_write=reply.cache_write_tokens,
                    reasoning_tokens=reply.reasoning_tokens, total_tokens=reply.total_tokens,
                    total_cost=0 if self.model_name.split("/", 1)[0] in {"kimi-code", "xai-oauth"} else None,
                ),
                response_id=reply.response_id,
                metadata={"served_model": reply.served_model, "fallback": False, "transport": "omp-oauth"},
            )

    _PROVIDER = OmpModelAPI
    return _PROVIDER
