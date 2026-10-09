"""Metered/subscription GLM model client for VerifierCheck audits.

Every other vcheck slice funnels model access through this module so spend is
capped, recorded, and attributable:

- :class:`Budget` caps USD spend per run and appends every charge to
  ``<run_dir>/spend.jsonl``.
- :func:`chat_completion` POSTs to an OpenAI-compatible ``/chat/completions``
  endpoint over stdlib ``urllib``. It tries the subscription endpoint
  (``https://api.z.ai/api/coding/paas/v4`` with ``ZAI_API_KEY``) first and
  falls back to the metered endpoint (``https://api.z.ai/api/paas/v4`` with
  ``ZAI_OPENAPI_API_KEY``) on 401/403 or expiry-signalling 429. API keys come
  from the environment only and are never written to records or disk.
- :func:`chain_append` owns the hash-chained trajectory record format
  ``{sequence, kind, data, previous, sha256}`` reused by all vcheck slices;
  :func:`verify_chain` detects tampering.
- :func:`run_tool_loop` drives a bounded OpenAI-style tool-call loop.

All agents use GLM 5.3 (red may use Flash) at temperature 0.1, 4096 max
tokens, and low reasoning effort unless the caller overrides them.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Subscription (coding-plan) endpoint, tried first when a key is present.
SUBSCRIPTION_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
#: Metered pay-per-token endpoint, used on fallback or when no subscription key exists.
METERED_BASE_URL = "https://api.z.ai/api/paas/v4"

SUBSCRIPTION_KEY_ENV = "ZAI_API_KEY"
METERED_KEY_ENV = "ZAI_OPENAPI_API_KEY"

#: Per-model (input, output) USD price per million tokens.
MODEL_PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "glm-5.3": (1.40, 4.40),
    "glm-5.3-flash": (0.15, 0.50),
}

DEFAULT_MODEL = "glm-5.3"
FLASH_MODEL = "glm-5.3-flash"
DEFAULT_TEMPERATURE = 0.1
DEFAULT_MAX_TOKENS = 4096
DEFAULT_REASONING_EFFORT = "low"

#: Which trajectory record kinds this module emits itself.
REQUEST_KIND = "request"
RESPONSE_KIND = "response"
TOOL_NOTE_KIND = "note"

__all__ = [
    "Budget",
    "BudgetExceeded",
    "BudgetApprovalError",
    "ChainTamperError",
    "ModelCallError",
    "chain_append",
    "verify_chain",
    "cost_usd",
    "chat_completion",
    "run_tool_loop",
    "DEFAULT_MODEL",
    "FLASH_MODEL",
    "METERED_BASE_URL",
    "SUBSCRIPTION_BASE_URL",
]


class BudgetExceeded(Exception):
    """Raised when a charge would push spend past the budget cap."""


class BudgetApprovalError(Exception):
    """Raised when spend-gated work runs without a recorded approval string."""


class ChainTamperError(ValueError):
    """Raised when a hash-chained trajectory fails verification."""


class ModelCallError(Exception):
    """Raised when a model call fails on every usable endpoint."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Budget:
    """USD spend cap with per-charge ledger output.

    ``approval`` holds the spend-authorization string for spend-gated paths;
    :meth:`require_approval` enforces its presence before any model call.
    """

    cap_usd: float
    spent_usd: float = 0.0
    approval: str | None = None

    def require_approval(self) -> None:
        """Raise unless a non-blank approval string is recorded."""
        if self.approval is None or not self.approval.strip():
            raise BudgetApprovalError(
                "Model spend requires a recorded approval string "
                "(pass approval='<who> <what> <cap>' to Budget)."
            )

    def charge(
        self,
        usd: float,
        *,
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        run_dir: Path | str | None = None,
        endpoint: str = "",
    ) -> float:
        """Add ``usd`` to spend, enforcing the cap; ledger the charge.

        Raises :class:`BudgetExceeded` without mutating spend when the charge
        would exceed ``cap_usd``. When ``run_dir`` is given, appends
        ``{model, endpoint, input_tokens, output_tokens, usd, at}`` to
        ``<run_dir>/spend.jsonl``. Returns the new total.
        """
        if usd < 0:
            raise ValueError(f"charge must be non-negative, got {usd}")
        total = self.spent_usd + usd
        if total > self.cap_usd + 1e-9:
            raise BudgetExceeded(
                f"Charge ${usd:.6f} would exceed budget cap ${self.cap_usd:.2f} "
                f"(already spent ${self.spent_usd:.6f})."
            )
        self.spent_usd = total
        if run_dir is not None:
            line = {
                "model": model,
                "endpoint": endpoint,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "usd": usd,
                "at": datetime.now(UTC).isoformat(),
            }
            spend_path = Path(run_dir) / "spend.jsonl"
            spend_path.parent.mkdir(parents=True, exist_ok=True)
            with spend_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")
        return self.spent_usd

    @property
    def remaining_usd(self) -> float:
        """Unspent budget."""
        return self.cap_usd - self.spent_usd


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return the USD cost of a call from the per-model price table."""
    try:
        in_price, out_price = MODEL_PRICES_USD_PER_MTOK[model]
    except KeyError:
        raise ValueError(f"Unknown model for pricing: {model!r}") from None
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000


def _canonical_hash(sequence: int, kind: str, data: Mapping[str, Any], previous: str) -> str:
    body = json.dumps(
        {"sequence": sequence, "kind": kind, "data": data, "previous": previous},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def chain_append(
    records: list[dict[str, Any]], kind: str, data: Mapping[str, Any]
) -> dict[str, Any]:
    """Append a hash-chained trajectory record and return it.

    Record shape: ``{sequence (1-based), kind, data, previous, sha256}`` where
    ``previous`` is the prior record's ``sha256`` (or ``'genesis'``) and
    ``sha256`` covers the canonical JSON of the other four fields.
    """
    sequence = len(records) + 1
    previous = records[-1]["sha256"] if records else "genesis"
    record = {
        "sequence": sequence,
        "kind": kind,
        "data": dict(data),
        "previous": previous,
        "sha256": _canonical_hash(sequence, kind, data, previous),
    }
    records.append(record)
    return record


def verify_chain(records: list[dict[str, Any]]) -> None:
    """Verify sequence order, linkage, and hashes; raise :class:`ChainTamperError`."""
    previous = "genesis"
    for index, record in enumerate(records, start=1):
        if record.get("sequence") != index:
            raise ChainTamperError(
                f"Record {index}: expected sequence {index}, got {record.get('sequence')!r}."
            )
        if record.get("previous") != previous:
            raise ChainTamperError(f"Record {index}: previous-link mismatch (chain broken).")
        expected = _canonical_hash(index, record["kind"], record["data"], previous)
        if record.get("sha256") != expected:
            raise ChainTamperError(f"Record {index}: sha256 mismatch (tamper detected).")
        previous = record["sha256"]


KeyProvider = Callable[[str], str | None]


def _lookup_key(name: str, key_provider: KeyProvider | None) -> str | None:
    provider = key_provider if key_provider is not None else os.environ.get
    value = provider(name)
    return value if value else None


class _HttpStatus(Exception):
    """Internal carrier for non-2xx responses (status code + body text)."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


def _post_chat_completions(
    base_url: str, api_key: str, payload: dict[str, Any], timeout_s: float
) -> dict[str, Any]:
    """POST one request; return the decoded JSON body or raise ``_HttpStatus``."""
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise _HttpStatus(exc.code, body) from None


def _should_fall_back(status: int, body: str) -> bool:
    """True for subscription-exhausted signals: 401/403, or 429 naming expiry."""
    if status in (401, 403):
        return True
    return status == 429 and "expir" in body.lower()


def chat_completion(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    budget: Budget | None = None,
    run_dir: Path | str | None = None,
    key_provider: KeyProvider | None = None,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    records: list[dict[str, Any]] | None = None,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    """Call ``/chat/completions`` with subscription-first, metered-fallback routing.

    Returns ``{content, tool_calls, usage, model, endpoint, records}`` where
    ``tool_calls`` is a list of ``{id, name, arguments}`` (``arguments`` is a
    dict; unparsable JSON arrives as ``{"_raw": <string>}``), ``usage`` holds
    ``{input_tokens, output_tokens}``, ``endpoint`` is ``"subscription"`` or
    ``"metered"``, and ``records`` holds the appended request/response
    trajectory records. When ``records`` is given it is extended in place via
    :func:`chain_append` (request first, then response).

    Usage cost is charged to ``budget`` (which also ledgers
    ``<run_dir>/spend.jsonl``) before returning. Keys are read from the
    environment via ``key_provider`` and never appear in records or errors.
    """
    transcript = records if records is not None else []
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning_effort": reasoning_effort,
    }
    if tools:
        payload["tools"] = tools
    request_record = chain_append(
        transcript,
        REQUEST_KIND,
        {
            "model": model,
            "messages": messages,
            "tools": tools or [],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "reasoning_effort": reasoning_effort,
        },
    )

    sub_key = _lookup_key(SUBSCRIPTION_KEY_ENV, key_provider)
    metered_key = _lookup_key(METERED_KEY_ENV, key_provider)
    attempts: list[tuple[str, str, str | None]] = []
    if sub_key:
        attempts.append(("subscription", SUBSCRIPTION_BASE_URL, sub_key))
    if metered_key:
        attempts.append(("metered", METERED_BASE_URL, metered_key))
    if not attempts:
        raise ModelCallError(
            f"No API key: set {SUBSCRIPTION_KEY_ENV} and/or {METERED_KEY_ENV} in the environment."
        )

    body: dict[str, Any] | None = None
    endpoint = attempts[0][0]
    for index, (name, base_url, api_key) in enumerate(attempts):
        try:
            assert api_key is not None
            body = _post_chat_completions(base_url, api_key, payload, timeout_s)
            endpoint = name
            break
        except _HttpStatus as exc:
            if index == 0 and len(attempts) > 1 and _should_fall_back(exc.status, exc.body):
                continue
            raise ModelCallError(
                f"Model call failed on {name} endpoint (HTTP {exc.status})."
            ) from None
    assert body is not None  # guaranteed: attempts non-empty, loop sets body or raises

    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise ModelCallError("Model response had no choices[0].message.") from None
    content = message.get("content")
    tool_calls: list[dict[str, Any]] = []
    for raw in message.get("tool_calls") or []:
        function = raw.get("function", {}) if isinstance(raw, dict) else {}
        raw_args = function.get("arguments", "{}") if isinstance(function, dict) else "{}"
        if isinstance(raw_args, dict):
            arguments = raw_args
        else:
            try:
                parsed = json.loads(raw_args or "{}")
                arguments = parsed if isinstance(parsed, dict) else {"_raw": raw_args}
            except (json.JSONDecodeError, TypeError):
                arguments = {"_raw": raw_args}
        tool_calls.append(
            {
                "id": raw.get("id") if isinstance(raw, dict) else None,
                "name": function.get("name") if isinstance(function, dict) else None,
                "arguments": arguments,
            }
        )
    usage_raw = body.get("usage") or {}
    usage = {
        "input_tokens": int(usage_raw.get("prompt_tokens", 0)),
        "output_tokens": int(usage_raw.get("completion_tokens", 0)),
    }
    response_record = chain_append(
        transcript,
        RESPONSE_KIND,
        {
            "model": model,
            "endpoint": endpoint,
            "content": content,
            "tool_calls": tool_calls,
            "usage": usage,
        },
    )
    if budget is not None:
        budget.charge(
            cost_usd(model, usage["input_tokens"], usage["output_tokens"]),
            model=model,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            run_dir=run_dir,
            endpoint=endpoint,
        )
    return {
        "content": content,
        "tool_calls": tool_calls,
        "usage": usage,
        "model": model,
        "endpoint": endpoint,
        "records": [request_record, response_record],
    }


#: A tool entry: (OpenAI-compatible schema, implementation ``fn(args) -> result``).
ToolEntry = tuple[dict[str, Any], Callable[[dict[str, Any]], Any]]


def _tool_payload_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    if schema.get("type") == "function" and isinstance(schema.get("function"), dict):
        return dict(schema)
    return {"type": "function", "function": dict(schema)}


def _safe_result(result: Any) -> Any:
    if result is None or isinstance(result, (str, int, float, bool, list, dict)):
        return result
    return str(result)


def run_tool_loop(
    model: str,
    messages: list[dict[str, Any]],
    *,
    tools: dict[str, ToolEntry],
    budget: Budget | None = None,
    run_dir: Path | str | None = None,
    records: list[dict[str, Any]] | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_steps: int = 30,
    key_provider: KeyProvider | None = None,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    timeout_s: float = 120.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run a bounded tool-call loop; return ``(transcript_records, tool_results)``.

    Each step calls :func:`chat_completion` (appending request/response records
    to ``records`` when given, else to a fresh transcript). When the assistant
    emits tool calls, each is dispatched to its ``fn`` and the result is fed
    back as a ``role: tool`` message plus a ``note`` trajectory record. The
    loop stops at the first assistant message with no tool calls, or after
    ``max_steps`` model calls. ``tool_results`` holds
    ``{name, arguments, result}`` per executed call (unknown tools and handler
    exceptions become ``{..., "error": <message>}`` results, never crashes).
    """
    transcript = records if records is not None else []
    working_messages = list(messages)
    tool_results: list[dict[str, Any]] = []
    payload_tools = [_tool_payload_schema(schema) for schema, _ in tools.values()]
    for _ in range(max(0, max_steps)):
        response = chat_completion(
            model,
            working_messages,
            tools=payload_tools or None,
            temperature=temperature,
            max_tokens=max_tokens,
            budget=budget,
            run_dir=run_dir,
            key_provider=key_provider,
            reasoning_effort=reasoning_effort,
            records=transcript,
            timeout_s=timeout_s,
        )
        calls = response["tool_calls"]
        if not calls:
            break
        for call in calls:
            name = call.get("name")
            arguments = call.get("arguments") or {}
            entry = tools.get(name) if isinstance(name, str) else None
            if entry is None:
                result: Any = {"error": f"Unknown tool: {name!r}."}
            else:
                try:
                    result = _safe_result(entry[1](dict(arguments)))
                except Exception as exc:
                    result = {"error": f"{type(exc).__name__}: {exc}"}
            tool_results.append({"name": name, "arguments": arguments, "result": result})
            content = result if isinstance(result, str) else json.dumps(result, default=str)
            chain_append(
                transcript,
                TOOL_NOTE_KIND,
                {"tool_call_id": call.get("id"), "name": name, "result": result},
            )
            working_messages = working_messages + [
                {
                    "role": "assistant",
                    "content": response["content"],
                    "tool_calls": [
                        {
                            "id": call.get("id"),
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments, default=str),
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": call.get("id"), "content": content},
            ]
    return transcript, tool_results
