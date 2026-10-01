"""Independent model-call capture for Harbor trials.

Harness trajectories are agent-controlled: installed agents write them inside
the container (``/logs/agent``), a root agent can delete them, and timeouts or
cancels can skip conversion, so a missing trajectory is ambiguous on its own.
The vendor proxies (``containers/*_secret_proxy.py``) record usage only, never
bodies. This module owns the independent record: a launcher-agnostic recording
reverse proxy (host process) plus attribution of captured calls to Harbor
trials and a per-trial completeness verdict against the harness ATIF.

Layout of a capture directory::

    <capture_dir>/calls.jsonl      one JSON record per proxied call (append + flush)
    <capture_dir>/capture.json     manifest written at startup
    <capture_dir>/provenance.json  ProvenanceMetadata-compatible sidecar at close

``link`` writes derived Parquet (``model_calls``, ``trial_capture``) plus a
``capture_link.json`` receipt under the resolved derived root, next to the
existing ``job_id=<uuid>/`` projections.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROXY_VERSION = "1.0.0"
CALL_RECORD_SCHEMA = "evallab.model_call/v1"
MANIFEST_SCHEMA = "evallab.model_capture_manifest/v1"
LINK_RECEIPT_SCHEMA = "evallab.model_capture_link/v1"

DEFAULT_PORT = 0
DEFAULT_BIND = "127.0.0.1"
UPSTREAM_TIMEOUT_SECONDS = 600.0
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_RECORDED_BODY_BYTES = 4 * 1024 * 1024
HEALTHZ_PATH = "/healthz"

#: ATIF trajectory candidates, mirroring
#: ``evallab.evidence.capture_authority`` so both readers agree on filenames.
TRAJECTORY_CANDIDATES = ("agent/trajectory.json", "trajectory.json", "agent/trajectory.jsonl")

#: Control agents never emit trajectories and never call models; both records
#: empty is their normal state, not missing evidence.
CONTROL_AGENTS = frozenset({"nop", "oracle"})

_WS_RE = re.compile(r"\s+")
_ROUTE_RE = re.compile(r"^/t/([^/]{1,128})/(.*)$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")

#: Request headers kept on the record. Auth-shaped headers are never selected,
#: so client credentials cannot reach ``calls.jsonl`` by header.
RECORDED_HEADERS = (
    "content-type",
    "user-agent",
    "x-session-id",
    "x-request-id",
    "anthropic-version",
)

#: Headers never forwarded upstream alongside an injected provider key.
_AUTH_HEADERS = frozenset(
    {"authorization", "x-api-key", "api-key", "cookie", "proxy-authorization"}
)

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 ``Z`` string."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp; ``None`` when absent or malformed."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def normalize_text(value: Any) -> str:
    """Collapse whitespace for stable cross-record comparison."""
    if not isinstance(value, str):
        return ""
    return _WS_RE.sub(" ", value).strip()


def eval_lab_commit() -> str | None:
    """Best-effort VCS revision of the checkout serving this proxy."""
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = out.stdout.strip()
    return commit if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", commit) else None


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file's bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_kind(path: str) -> str:
    """Classify an upstream path by suffix (route prefix already stripped)."""
    clean = path.split("?", 1)[0].rstrip("/") or "/"
    if clean.endswith("/chat/completions"):
        return "chat"
    if clean.endswith("/api/chat"):
        return "ollama_chat"
    if clean.endswith("/completions"):
        return "legacy_completions"
    if clean.endswith("/responses"):
        return "responses"
    if clean.endswith("/messages"):
        return "anthropic_messages"
    return "unknown"


def split_route_token(raw_path: str) -> tuple[str | None, str]:
    """Split an optional ``/t/<token>/`` prefix; malformed prefixes pass through."""
    path, sep, query = raw_path.partition("?")
    match = _ROUTE_RE.match(path)
    if not match:
        return None, raw_path
    token, rest = match.group(1), "/" + match.group(2)
    if not _TOKEN_RE.fullmatch(token):
        return None, raw_path
    return token, rest + (sep + query if sep else "")


def selected_headers(raw: dict[str, str]) -> dict[str, str]:
    """Keep the allowlisted headers; auth-shaped headers are never selected."""
    lowered = {key.lower(): value for key, value in raw.items()}
    return {name: lowered[name] for name in RECORDED_HEADERS if name in lowered}


def join_upstream_path(base_path: str, request_path: str) -> str:
    """Join an upstream base path and a request path without doubling.

    Clients disagree on whether the version prefix travels with the request:
    litellm posts ``/chat/completions`` to its ``api_base`` while raw OpenAI
    clients post ``/v1/chat/completions``. When the upstream base already ends
    with the request's leading segments (``--upstream .../v1``), the shared
    prefix is kept once instead of doubled.
    """
    base = base_path.rstrip("/")
    if not base:
        return request_path
    if request_path == base or request_path.startswith(base + "/"):
        return request_path
    return base + request_path


def redact_key_text(text: str, key: str | None) -> str:
    """Replace upstream-key occurrences with ``<redacted>`` (keys shorter than
    8 chars are left alone to avoid scrubbing ordinary words)."""
    if not key or len(key) < 8 or key not in text:
        return text
    return text.replace(key, "<redacted>")


# --------------------------------------------------------------------------- #
# Request message extraction (shared by attribution and verdicts)
# --------------------------------------------------------------------------- #


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                for key in ("text", "input_text", "output_text"):
                    if isinstance(part.get(key), str):
                        parts.append(str(part[key]))
        return "".join(parts)
    return ""


def request_message_pairs(kind: str, body: Any) -> list[tuple[str, str]]:
    """Ordered ``(role, text)`` pairs of one request body, best-effort."""
    if not isinstance(body, dict):
        return []
    pairs: list[tuple[str, str]] = []
    if kind in ("chat", "ollama_chat", "unknown"):
        messages = body.get("messages")
        if isinstance(messages, list):
            for item in messages:
                if not isinstance(item, dict):
                    continue
                role = item.get("role")
                text = _content_text(item.get("content"))
                if isinstance(role, str) and text:
                    pairs.append((role, text))
    elif kind == "legacy_completions":
        prompt = body.get("prompt")
        if isinstance(prompt, str) and prompt:
            pairs.append(("user", prompt))
        elif isinstance(prompt, list):
            for item in prompt:
                if isinstance(item, str) and item:
                    pairs.append(("user", item))
    elif kind == "responses":
        for item in _responses_input_texts(body.get("input")):
            pairs.append(item)
    elif kind == "anthropic_messages":
        system = body.get("system")
        if isinstance(system, str) and system:
            pairs.append(("system", system))
        elif isinstance(system, list):
            text = _content_text(system)
            if text:
                pairs.append(("system", text))
        messages = body.get("messages")
        if isinstance(messages, list):
            for item in messages:
                if not isinstance(item, dict):
                    continue
                role = item.get("role")
                text = _content_text(item.get("content"))
                if isinstance(role, str) and text:
                    pairs.append((role, text))
    return pairs


def _responses_input_texts(value: Any) -> list[tuple[str, str]]:
    if isinstance(value, str) and value:
        return [("user", value)]
    if isinstance(value, list):
        out: list[tuple[str, str]] = []
        for item in value:
            if isinstance(item, str) and item:
                out.append(("user", item))
            elif isinstance(item, dict):
                role = item.get("role")
                if isinstance(role, str):
                    text = _content_text(item.get("content"))
                    if text:
                        out.append((role, text))
        return out
    return []


def first_user_text(kind: str, body: Any) -> str:
    """Normalized first user-role text of a request, or ``""``."""
    for role, text in request_message_pairs(kind, body):
        if role == "user" and normalize_text(text):
            return normalize_text(text)
    return ""


# --------------------------------------------------------------------------- #
# Response extraction: one shared SSE + usage canonicalization
# --------------------------------------------------------------------------- #
# Mirrors the behavior of the vendor proxies (``containers/*_secret_proxy.py``,
# ``_canonicalize_sse_and_usage``): split on blank lines, read ``data:`` lines,
# skip ``[DONE]``, parse each event as JSON. Unlike the metered proxies this
# module never rejects an upstream body shape: unparsable events are skipped
# for extraction but the raw bytes are always recorded and forwarded.


def parse_sse_payloads(raw: bytes) -> list[Any]:
    """Parse SSE ``data:`` payloads; ``[DONE]`` and malformed lines are skipped."""
    payloads: list[Any] = []
    current: list[str] = []

    def flush() -> None:
        data = "\n".join(current)
        current.clear()
        if not data.strip() or data.strip() == "[DONE]":
            return
        try:
            payloads.append(json.loads(data))
        except (ValueError, UnicodeDecodeError):
            return

    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            flush()
            continue
        if line.startswith("data:"):
            current.append(line[5:].lstrip(" "))
        # ``event:``/``id:``/``retry:`` lines carry no payload; a new data
        # block starts a new event only on a blank line, so other lines that
        # appear mid-event are ignored without flushing.
    flush()
    return payloads


@dataclass
class ExtractedTurns:
    """Reassembled assistant turns of one response."""

    assistant_texts: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] | None = None
    model: str | None = None


def _usage_pair(prompt: Any, completion: Any) -> dict[str, int] | None:
    if isinstance(prompt, bool) or isinstance(completion, bool):
        return None
    if isinstance(prompt, int) and isinstance(completion, int) and prompt >= 0 and completion >= 0:
        return {"prompt_tokens": prompt, "completion_tokens": completion}
    return None


def _usage_from_openai(payload: dict[str, Any]) -> dict[str, int] | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    return _usage_pair(usage.get("prompt_tokens"), usage.get("completion_tokens"))


def extract_from_payload(kind: str, payload: Any) -> ExtractedTurns:
    """Reassemble assistant turns from one parsed JSON response body."""
    turns = ExtractedTurns()
    if not isinstance(payload, dict):
        return turns
    model = payload.get("model")
    if isinstance(model, str) and model:
        turns.model = model
    if kind in ("chat", "legacy_completions"):
        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if isinstance(message, dict):
                    text = _content_text(message.get("content"))
                    if text:
                        turns.assistant_texts.append(text)
                    for call in message.get("tool_calls") or []:
                        parsed = _openai_tool_call(call)
                        if parsed is not None:
                            turns.tool_calls.append(parsed)
                elif isinstance(choice.get("text"), str) and choice["text"]:
                    turns.assistant_texts.append(str(choice["text"]))
        turns.usage = _usage_from_openai(payload)
    elif kind == "responses":
        response = payload.get("response") if "response" in payload else payload
        if isinstance(response, dict):
            _extract_responses_object(response, turns)
    elif kind == "anthropic_messages":
        _extract_anthropic_object(payload, turns)
    elif kind == "ollama_chat":
        _ollama_message_into(payload.get("message"), turns)
        _ollama_usage_into(payload, turns)
    return turns


def _openai_tool_call(call: Any) -> dict[str, Any] | None:
    if not isinstance(call, dict):
        return None
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = function.get("name")
    if not isinstance(name, str) or not name:
        return None
    arguments = function.get("arguments", "")
    return {
        "id": call.get("id"),
        "name": name,
        "arguments": arguments
        if isinstance(arguments, str)
        else json.dumps(arguments, sort_keys=True),
    }


def _extract_responses_object(response: dict[str, Any], turns: ExtractedTurns) -> None:
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "message":
                text = _content_text(item.get("content"))
                if text:
                    turns.assistant_texts.append(text)
            elif item_type == "function_call":
                name = item.get("name")
                if isinstance(name, str) and name:
                    arguments = item.get("arguments", "")
                    turns.tool_calls.append(
                        {
                            "arguments": arguments
                            if isinstance(arguments, str)
                            else json.dumps(arguments, sort_keys=True),
                        }
                    )
    usage = response.get("usage")
    if isinstance(usage, dict):
        turns.usage = _usage_pair(usage.get("input_tokens"), usage.get("output_tokens"))
    elif isinstance(response.get("usage"), dict):
        turns.usage = _usage_from_openai(response)


def _extract_anthropic_object(payload: dict[str, Any], turns: ExtractedTurns) -> None:
    content = payload.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                turns.assistant_texts.append(str(block["text"]))
            elif block.get("type") == "tool_use" and isinstance(block.get("name"), str):
                turns.tool_calls.append(
                    {
                        "id": block.get("id"),
                        "name": str(block["name"]),
                        "arguments": json.dumps(block.get("input", {}), sort_keys=True),
                    }
                )
    usage = payload.get("usage")
    if isinstance(usage, dict):
        turns.usage = _usage_pair(usage.get("input_tokens"), usage.get("output_tokens"))
    model = payload.get("model")
    if isinstance(model, str) and model:
        turns.model = model


def _ollama_message_into(message: Any, turns: ExtractedTurns) -> None:
    if not isinstance(message, dict):
        return
    text = _content_text(message.get("content"))
    if text:
        turns.assistant_texts.append(text)
    _ollama_tools_into(message.get("tool_calls"), turns)


def _ollama_tools_into(calls: Any, turns: ExtractedTurns) -> None:
    for call in calls or []:
        parsed = _openai_tool_call(call)
        if parsed is not None and parsed not in turns.tool_calls:
            turns.tool_calls.append(parsed)


def _ollama_usage_into(payload: dict[str, Any], turns: ExtractedTurns) -> None:
    usage = _usage_pair(payload.get("prompt_eval_count"), payload.get("eval_count"))
    if usage is not None:
        turns.usage = usage
    if isinstance(payload.get("model"), str) and not turns.model:
        turns.model = str(payload["model"])


def extract_ollama_ndjson(raw: bytes) -> ExtractedTurns:
    """Reassemble an Ollama streaming ``/api/chat`` (NDJSON) body."""
    turns = ExtractedTurns()
    chunks: list[str] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        if isinstance(message, dict):
            if isinstance(message.get("content"), str):
                chunks.append(str(message["content"]))
            _ollama_tools_into(message.get("tool_calls"), turns)
        _ollama_usage_into(event, turns)
    if chunks:
        turns.assistant_texts.append("".join(chunks))
    return turns


def extract_from_sse(kind: str, raw: bytes) -> ExtractedTurns:
    """Reassemble assistant turns from a raw SSE body (streaming responses)."""
    turns = ExtractedTurns()
    if kind in ("chat", "legacy_completions"):
        texts: dict[Any, list[str]] = {}
        tools: dict[Any, dict[int, dict[str, Any]]] = {}
        for payload in parse_sse_payloads(raw):
            if not isinstance(payload, dict):
                continue
            if isinstance(payload.get("model"), str) and not turns.model:
                turns.model = str(payload["model"])
            usage = _usage_from_openai(payload)
            if usage is not None:
                turns.usage = usage
            choices = payload.get("choices")
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                index = choice.get("index", 0)
                delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                content = delta.get("content") if isinstance(delta, dict) else None
                if isinstance(content, str) and content:
                    texts.setdefault(index, []).append(content)
                text = choice.get("text")
                if isinstance(text, str) and text:
                    texts.setdefault(index, []).append(text)
                delta_calls = delta.get("tool_calls") if isinstance(delta, dict) else None
                if isinstance(delta_calls, list):
                    bucket = tools.setdefault(index, {})
                    for part in delta_calls:
                        if not isinstance(part, dict):
                            continue
                        part_index = part.get("index", 0)
                        slot = bucket.setdefault(
                            part_index, {"id": None, "name": "", "arguments": ""}
                        )
                        if isinstance(part.get("id"), str):
                            slot["id"] = part["id"]
                        function = part.get("function")
                        if isinstance(function, dict):
                            if isinstance(function.get("name"), str) and function["name"]:
                                slot["name"] = str(slot["name"]) + str(function["name"])
                            if isinstance(function.get("arguments"), str):
                                slot["arguments"] = str(slot["arguments"]) + str(
                                    function["arguments"]
                                )
        for index in sorted(texts):
            joined = "".join(texts[index])
            if joined:
                turns.assistant_texts.append(joined)
        for index in sorted(tools):
            for part_index in sorted(tools[index]):
                slot = tools[index][part_index]
                if slot["name"]:
                    turns.tool_calls.append(slot)
    elif kind == "responses":
        texts: list[str] = []
        for payload in parse_sse_payloads(raw):
            if not isinstance(payload, dict):
                continue
            event_type = payload.get("type")
            if event_type == "response.output_text.delta" and isinstance(payload.get("delta"), str):
                texts.append(str(payload["delta"]))
            elif event_type == "response.completed":
                completed = extract_from_payload("responses", payload.get("response", {}))
                if completed.assistant_texts:
                    turns.assistant_texts.extend(completed.assistant_texts)
                turns.tool_calls.extend(completed.tool_calls)
                if completed.usage is not None:
                    turns.usage = completed.usage
        if texts and not turns.assistant_texts:
            turns.assistant_texts.append("".join(texts))
    elif kind == "anthropic_messages":
        texts: list[str] = []
        input_tokens: Any = None
        for payload in parse_sse_payloads(raw):
            if not isinstance(payload, dict):
                continue
            event_type = payload.get("type")
            if event_type == "message_start":
                message = payload.get("message")
                if isinstance(message, dict):
                    usage = message.get("usage")
                    if isinstance(usage, dict) and isinstance(usage.get("input_tokens"), int):
                        input_tokens = usage["input_tokens"]
                    if isinstance(message.get("model"), str):
                        turns.model = str(message["model"])
            elif event_type == "content_block_delta":
                delta = payload.get("delta")
                if isinstance(delta, dict) and isinstance(delta.get("text"), str):
                    texts.append(str(delta["text"]))
            elif event_type == "message_delta":
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    output_tokens = usage.get("output_tokens")
                    turns.usage = _usage_pair(input_tokens, output_tokens)
            elif event_type == "content_block_start":
                block = payload.get("content_block")
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and isinstance(block.get("name"), str)
                ):
                    turns.tool_calls.append(
                        {"id": block.get("id"), "name": str(block["name"]), "arguments": ""}
                    )
        if texts:
            turns.assistant_texts.append("".join(texts))
        if turns.usage is None and input_tokens is not None:
            turns.usage = _usage_pair(input_tokens, 0)
    return turns


# --------------------------------------------------------------------------- #
# Recorder, manifest, provenance
# --------------------------------------------------------------------------- #


class CaptureRecorder:
    """Append-only ``calls.jsonl`` writer shared by proxy worker threads."""

    def __init__(self, out_dir: str | Path) -> None:
        from evallab.storage.fs import durable_mkdir

        self.out_dir = Path(out_dir)
        durable_mkdir(self.out_dir)
        self.calls_path = self.out_dir / "calls.jsonl"
        seq = 1
        if self.calls_path.is_file():
            try:
                with self.calls_path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip():
                            seq += 1
            except OSError:
                seq = 1
        self._seq = seq
        self._lock = threading.Lock()
        self._handle = self.calls_path.open("a", encoding="utf-8")

    def append(self, record: dict[str, Any]) -> int:
        """Append one record with its sequence number; flush + fsync per record."""
        with self._lock:
            seq = self._seq
            self._seq += 1
            self._handle.write(json.dumps({**record, "seq": seq}, ensure_ascii=False) + "\n")
            self._handle.flush()
            os.fsync(self._handle.fileno())
            return seq

    def close(self) -> None:
        with self._lock:
            self._handle.close()


def capture_endpoint(bind: str, port: int) -> str:
    """Public URL of a bound capture server (published only after a successful bind)."""
    return f"http://{bind}:{port}"


def write_manifest(
    out_dir: str | Path, *, upstream: str, bind: str, port: int, endpoint: str | None = None
) -> dict[str, Any]:
    """Write ``capture.json`` at startup; returns the manifest.

    ``port`` MUST be the bound port (``server.server_address[1]``), never the
    requested one: with the default ``port=0`` the OS assigns a free port per
    server, so concurrent servers get distinct endpoints. ``endpoint`` defaults
    to :func:`capture_endpoint` for that bound port.
    """
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "proxy_version": PROXY_VERSION,
        "upstream": upstream,
        "started_at": utc_now_iso(),
        "eval_lab_commit": eval_lab_commit(),
        "bind": bind,
        "port": port,
        "endpoint": endpoint or capture_endpoint(bind, port),
        "pid": os.getpid(),
    }
    out = Path(out_dir) / "capture.json"
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def write_provenance(out_dir: str | Path, *, upstream: str) -> dict[str, Any]:
    """Write the ProvenanceMetadata-compatible ``provenance.json`` at close.

    The material digest covers the ``calls.jsonl`` bytes as they stand, so a
    later ``link`` can detect post-close appends by recomputing it.
    """
    from evallab.storage.fs import durable_mkdir

    directory = Path(out_dir)
    durable_mkdir(directory)
    calls_path = directory / "calls.jsonl"
    call_count = 0
    if calls_path.is_file():
        try:
            with calls_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        call_count += 1
        except OSError:
            call_count = 0
    provenance = {
        "item_id": "model-capture-"
        + re.sub(r"[^a-z0-9._-]+", "-", socket.gethostname().lower()).strip("-"),
        "zone": "02-local-evidence",
        "source_uri": upstream,
        "material_digest": f"sha256:{sha256_file(calls_path)}" if calls_path.is_file() else None,
        "created_at": utc_now_iso(),
        "created_by": f"evallab-capture@{PROXY_VERSION}",
        "transform": None,
        "parent_digests": [],
        "call_count": call_count,
        "notes": "Independent model-call record; harness trajectories are agent-controlled.",
    }
    (directory / "provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return provenance


# --------------------------------------------------------------------------- #
# Recording reverse proxy
# --------------------------------------------------------------------------- #


class CaptureProxyServer(ThreadingHTTPServer):
    """Threading HTTP server carrying the recorder and upstream binding."""

    daemon_threads = True
    allow_reuse_address = True
    # socketserver's default listen backlog is 5. One capture server fronts
    # every trial of a round (20 in parallel for G2), and a burst of connects
    # beyond the backlog is reset by the kernel (ConnectionResetError on CI).
    request_queue_size = 256

    def __init__(
        self,
        address: tuple[str, int],
        recorder: CaptureRecorder,
        upstream: str,
        upstream_key: str | None,
    ) -> None:
        self.recorder = recorder
        self.upstream = upstream.rstrip("/")
        self.upstream_key = upstream_key
        super().__init__(address, _CaptureHandler)


def _request_body(handler: BaseHTTPRequestHandler) -> tuple[bytes, str | None]:
    """Read the request body; oversized bodies return a 413 marker."""
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        length = 0
    if length < 0:
        length = 0
    if length > MAX_REQUEST_BYTES:
        return b"", "request_too_large"
    if length == 0:
        return b"", None
    try:
        return handler.rfile.read(length), None
    except (OSError, ValueError):
        return b"", "client_disconnect"


def _forward_headers(
    handler: BaseHTTPRequestHandler, *, upstream_key: str | None
) -> dict[str, str]:
    """Inbound headers safe to forward; client auth is replaced, never passed on.

    The inbound ``Host`` (this capture server) is never forwarded: the HTTP
    client sets ``Host`` from the upstream URL instead. Forwarding it would
    misroute upstreams that dispatch on ``Host`` (e.g. the Modal edge).
    """
    forwarded: dict[str, str] = {}
    for key, value in handler.headers.items():
        folded = key.lower()
        if folded in _HOP_BY_HOP or folded in {"content-length", "host"}:
            continue
        if upstream_key is not None and folded in _AUTH_HEADERS:
            continue
        forwarded[key] = value
    if upstream_key is not None:
        forwarded["Authorization"] = f"Bearer {upstream_key}"
    return forwarded


class _CaptureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "evallab-capture/" + PROXY_VERSION

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def _handle(self) -> None:
        split = urlsplit(self.path)
        if split.path == HEALTHZ_PATH:
            body = b'{"status":"ok"}\n'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            with contextlib.suppress(OSError, ValueError):
                self.wfile.write(body)
            self.close_connection = True
            return
        server = self.server
        assert isinstance(server, CaptureProxyServer)
        started = datetime.now(UTC)
        route_token, stripped = split_route_token(self.path)
        stripped_split = urlsplit(stripped)
        kind = classify_kind(stripped_split.path)
        raw_headers = {key: value for key, value in self.headers.items()}
        recorded_headers = selected_headers(raw_headers)
        session_id = recorded_headers.get("x-session-id")
        request_bytes, read_error = _request_body(self)
        request_body: Any = None
        if request_bytes:
            try:
                request_body = json.loads(request_bytes.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                request_body = {"_text": request_bytes.decode("utf-8", "replace")}
        if isinstance(request_body, dict) and server.upstream_key:
            request_body = json.loads(
                redact_key_text(json.dumps(request_body, ensure_ascii=False), server.upstream_key)
            )
        record: dict[str, Any] = {
            "schema": CALL_RECORD_SCHEMA,
            "started_at": started.isoformat().replace("+00:00", "Z"),
            "ended_at": None,
            "method": self.command,
            "path": stripped_split.path,
            "route_token": route_token,
            "request_headers": recorded_headers,
            "session_id": session_id,
            "request_body": request_body,
            "response_status": None,
            "response_body": None,
            "response_sse": False,
            "assistant_texts": [],
            "tool_calls": [],
            "usage": None,
            "model": None,
            "upstream_latency_s": None,
            "error": None,
        }
        if read_error == "request_too_large":
            record["ended_at"] = utc_now_iso()
            record["error"] = {"kind": "request_too_large", "detail": "request over size cap"}
            server.recorder.append(record)
            self._send_error(413, b'{"error":"request over size cap"}\n')
            return
        if read_error == "client_disconnect":
            record["ended_at"] = utc_now_iso()
            record["error"] = {"kind": "client_disconnect", "detail": "request unreadable"}
            server.recorder.append(record)
            return
        upstream_path = stripped_split.path
        if stripped_split.query:
            upstream_path += "?" + stripped_split.query
        target = urlsplit(server.upstream)
        connection: http.client.HTTPConnection | None = None
        try:
            if target.scheme == "https":
                connection = http.client.HTTPSConnection(
                    target.hostname or "localhost",
                    target.port or 443,
                    timeout=UPSTREAM_TIMEOUT_SECONDS,
                )
            else:
                connection = http.client.HTTPConnection(
                    target.hostname or "localhost",
                    target.port or 80,
                    timeout=UPSTREAM_TIMEOUT_SECONDS,
                )
            base_path = target.path.rstrip("/")
            upstream_started = datetime.now(UTC)
            try:
                connection.request(
                    self.command,
                    join_upstream_path(base_path, upstream_path),
                    body=request_bytes or None,
                    headers=_forward_headers(self, upstream_key=server.upstream_key),
                )
                response = connection.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                record["ended_at"] = utc_now_iso()
                record["error"] = {
                    "kind": "upstream_unreachable",
                    "detail": f"{type(exc).__name__}",
                }
                server.recorder.append(record)
                self._send_error(502, b'{"error":"upstream unreachable"}\n')
                return
            content_type = response.getheader("Content-Type") or ""
            is_stream = "text/event-stream" in content_type
            status = response.status
            if is_stream:
                raw, client_gone = self._relay_stream(status, content_type, response)
            else:
                raw = response.read()
                client_gone = False
                self.send_response(status)
                self.send_header("Content-Type", content_type or "application/octet-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (OSError, ValueError):
                    client_gone = True
                self.close_connection = True
            latency = (datetime.now(UTC) - upstream_started).total_seconds()
            record["response_status"] = status
            record["upstream_latency_s"] = round(latency, 3)
            record["response_sse"] = is_stream
            self._record_response(record, kind, raw, server.upstream_key)
            if client_gone:
                record["error"] = {
                    "kind": "client_disconnect",
                    "detail": "client went away mid-response",
                }
            record["ended_at"] = utc_now_iso()
            server.recorder.append(record)
        except (OSError, http.client.HTTPException) as exc:
            record["ended_at"] = utc_now_iso()
            record["error"] = {"kind": "upstream_error", "detail": f"{type(exc).__name__}"}
            server.recorder.append(record)
            with contextlib.suppress(OSError, ValueError):
                self._send_error(502, b'{"error":"upstream error"}\n')
        finally:
            if connection is not None:
                with contextlib.suppress(OSError, http.client.HTTPException):
                    connection.close()

    def _relay_stream(
        self, status: int, content_type: str, response: http.client.HTTPResponse
    ) -> tuple[bytes, bool]:
        """Forward SSE chunks as they arrive; buffer the full body for the record."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Connection", "close")
        self.end_headers()
        chunks: list[bytes] = []
        client_gone = False
        try:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (OSError, ValueError):
                    client_gone = True
                    break
        except (OSError, http.client.HTTPException):
            pass
        self.close_connection = True
        return b"".join(chunks), client_gone

    def _record_response(
        self, record: dict[str, Any], kind: str, raw: bytes, upstream_key: str | None
    ) -> None:
        """Fill response fields; extraction is best-effort, raw bytes authoritative."""
        if len(raw) > MAX_RECORDED_BODY_BYTES:
            record["response_body"] = {
                "_truncated": True,
                "_bytes": len(raw),
                "_text": raw[:MAX_RECORDED_BODY_BYTES].decode("utf-8", "replace"),
            }
            return
        if record["response_sse"]:
            text = redact_key_text(raw.decode("utf-8", "replace"), upstream_key)
            turns = extract_from_sse(kind, raw)
            record["response_body"] = {
                "sse_raw": text,
                "reassembled": {
                    "assistant_texts": turns.assistant_texts,
                    "tool_calls": turns.tool_calls,
                },
            }
            record["assistant_texts"] = turns.assistant_texts
            record["tool_calls"] = turns.tool_calls
            record["usage"] = turns.usage
            record["model"] = turns.model
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            if kind != "ollama_chat":
                record["response_body"] = {
                    "_text": redact_key_text(raw.decode("utf-8", "replace"), upstream_key)
                }
                return
            text = redact_key_text(raw.decode("utf-8", "replace"), upstream_key)
            turns = extract_ollama_ndjson(raw)
            record["response_body"] = {
                "ndjson_raw": text,
                "reassembled": {
                    "assistant_texts": turns.assistant_texts,
                    "tool_calls": turns.tool_calls,
                },
            }
            record["assistant_texts"] = turns.assistant_texts
            record["tool_calls"] = turns.tool_calls
            record["usage"] = turns.usage
            record["model"] = turns.model
            return
        scrubbed = (
            json.loads(redact_key_text(json.dumps(payload, ensure_ascii=False), upstream_key))
            if upstream_key
            else payload
        )
        record["response_body"] = scrubbed
        turns = extract_from_payload(kind, payload)
        record["assistant_texts"] = turns.assistant_texts
        record["tool_calls"] = turns.tool_calls
        record["usage"] = turns.usage
        record["model"] = turns.model

    def _send_error(self, status: int, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (OSError, ValueError):
            pass
        self.close_connection = True


class _PendingRecorder:
    """Placeholder until the socket owns its port; never records.

    ``serve_capture`` binds first with this placeholder so a failed bind
    (explicit occupied port) leaves no ``calls.jsonl`` or ``capture.json``
    behind. It is replaced with the real recorder before ``serve_forever``.
    """

    def append(self, record: dict[str, Any]) -> int:
        raise RuntimeError("capture server is not bound yet")


def serve_capture(
    *,
    upstream: str,
    out_dir: str | Path,
    bind: str = DEFAULT_BIND,
    port: int = DEFAULT_PORT,
    upstream_key: str | None = None,
) -> tuple[CaptureProxyServer, CaptureRecorder, dict[str, Any]]:
    """Bind the recording proxy; the caller owns ``serve_forever``/shutdown.

    ``port=0`` (the default) asks the OS for a free port per server, so
    concurrent servers never collide. An explicit occupied port raises
    ``OSError`` (``EADDRINUSE``) with no ``calls.jsonl`` or ``capture.json``
    written. The manifest carries the bound port and endpoint, published
    only after the bind succeeds. No availability probe precedes the bind,
    so there is no check-then-bind race.
    """
    server = CaptureProxyServer(
        (bind, port),  # type: ignore[arg-type]
        recorder=_PendingRecorder(),  # type: ignore[arg-type]
        upstream=upstream,
        upstream_key=upstream_key,
    )
    try:
        recorder = CaptureRecorder(out_dir)
    except Exception:
        server.server_close()
        raise
    server.recorder = recorder  # type: ignore[assignment]
    try:
        bound_port = int(server.server_address[1])
        manifest = write_manifest(out_dir, upstream=upstream, bind=bind, port=bound_port)
    except Exception:
        with contextlib.suppress(Exception):
            recorder.close()
        server.server_close()
        raise
    return server, recorder, manifest


# --------------------------------------------------------------------------- #
# Link: attribute captured calls to trials, judge completeness
# --------------------------------------------------------------------------- #


@dataclass
class TrialEvidence:
    """Everything ``link`` knows about one Harbor trial."""

    name: str
    trial_id: str | None
    directory: Path
    agent: str | None
    window_start: datetime | None
    window_end: datetime | None
    attempt_id: str | None = None
    atif_session: str | None = None
    atif_user_texts: list[str] = field(default_factory=list)
    atif_agent_texts: list[str] = field(default_factory=list)
    atif_agent_steps: int = 0
    atif_present: bool = False
    instruction: str | None = None


def iter_trial_dirs(job_dir: str | Path) -> list[Path]:
    """Trial directories directly under a Harbor job directory, sorted by name."""
    job = Path(job_dir)
    found: list[Path] = []
    if not job.is_dir():
        return found
    for child in sorted(job.iterdir()):
        if not child.is_dir():
            continue
        result_path = child / "result.json"
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("trial_name"):
            found.append(child)
    return found


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _atif_documents(trial_dir: Path) -> list[dict[str, Any]]:
    """Load the ATIF document plus Harbor continuation segments, best-effort."""
    documents: list[dict[str, Any]] = []
    for candidate in TRAJECTORY_CANDIDATES:
        path = trial_dir / candidate
        if not path.is_file():
            continue
        if path.suffix == ".jsonl":
            steps: list[Any] = []
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        steps.append(json.loads(line))
            except (OSError, ValueError):
                continue
            documents.append({"steps": steps})
        else:
            payload = _load_json(path)
            if payload is not None:
                documents.append(payload)
        break
    if documents:
        root = trial_dir / TRAJECTORY_CANDIDATES[0]
        base = root.parent
        for extra in sorted(base.glob("trajectory.cont-*.json")):
            payload = _load_json(extra)
            if payload is not None:
                documents.append(payload)
    return documents


def _resolve_instruction(result: dict[str, Any], job_dir: Path) -> str | None:
    """Normalized task instruction for a trial, via the job's task path."""
    config = result.get("config")
    if not isinstance(config, dict):
        return None
    task = config.get("task")
    rel: str | None = None
    if isinstance(task, str) and task:
        rel = task
    elif isinstance(task, dict) and isinstance(task.get("path"), str):
        rel = str(task["path"])
    if not rel:
        return None
    candidate = Path(rel)
    if not candidate.is_absolute():
        from evallab.storage.paths import shared_checkout_root

        candidate = shared_checkout_root(job_dir.resolve()) / rel / "instruction.md"
    else:
        candidate = candidate / "instruction.md"
    try:
        text = candidate.read_text(encoding="utf-8")
    except OSError:
        return None
    return normalize_text(text) or None


def _job_attempt_id(job_dir: Path) -> str | None:
    """Job attempt id the runner's secret proxy stamps as the capture route token."""
    metadata = _load_json(job_dir / "lab-metadata.json")
    usage = metadata.get("provider_usage") if isinstance(metadata, dict) else None
    if isinstance(usage, dict):
        attempt = usage.get("attempt_id")
        if isinstance(attempt, str) and attempt.strip():
            return attempt.strip()
    return None


def collect_trial_evidence(trial_dir: str | Path, job_dir: str | Path) -> TrialEvidence:
    """Gather ATIF texts, the agent-execution window, and the instruction."""
    directory = Path(trial_dir)
    result = _load_json(directory / "result.json") or {}
    config = result.get("config") if isinstance(result.get("config"), dict) else {}
    agent_cfg = config.get("agent") if isinstance(config, dict) else None
    agent = None
    if isinstance(agent_cfg, dict) and isinstance(agent_cfg.get("name"), str):
        agent = str(agent_cfg["name"])
    if agent is None:
        agent_info = result.get("agent_info")
        if isinstance(agent_info, dict) and isinstance(agent_info.get("name"), str):
            agent = str(agent_info["name"])
    window = result.get("agent_execution")
    window_start = window_end = None
    if isinstance(window, dict):
        window_start = parse_ts(window.get("started_at"))
        window_end = parse_ts(window.get("finished_at"))
    attempt_id = _job_attempt_id(Path(job_dir))
    evidence = TrialEvidence(
        name=str(result.get("trial_name") or directory.name),
        trial_id=str(result["id"]) if isinstance(result.get("id"), str) else None,
        directory=directory,
        agent=agent,
        window_start=window_start,
        window_end=window_end,
        attempt_id=attempt_id,
    )
    documents = _atif_documents(directory)
    if documents:
        first = documents[0]
        session = first.get("session_id")
        evidence.atif_session = str(session) if isinstance(session, str) and session else None
        for document in documents:
            steps = document.get("steps")
            if not isinstance(steps, list):
                continue
            for step in steps:
                if not isinstance(step, dict):
                    continue
                source = step.get("source")
                message = step.get("message")
                if message is None:
                    text = ""
                elif isinstance(message, str):
                    text = normalize_text(message)
                else:
                    text = normalize_text(json.dumps(message, sort_keys=True))
                if source == "agent":
                    evidence.atif_agent_steps += 1
                    if text:
                        evidence.atif_agent_texts.append(text)
                elif source == "user" and text:
                    evidence.atif_user_texts.append(text)
        # Present means a trajectory document with a non-empty step list was
        # readable; agent-step counts below decide truncated vs complete.
        evidence.atif_present = any(
            isinstance(document.get("steps"), list) and len(document["steps"]) > 0
            for document in documents
        )
    evidence.instruction = _resolve_instruction(result, Path(job_dir))
    return evidence


def _normalized_pairs(kind: str, body: Any) -> list[tuple[str, str]]:
    return [(role, normalize_text(text)) for role, text in request_message_pairs(kind, body)]


def _is_prefix(shorter: list[tuple[str, str]], longer: list[tuple[str, str]]) -> bool:
    if not shorter or len(shorter) > len(longer):
        return False
    return longer[: len(shorter)] == shorter


def _conversations(calls: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group calls into conversations by message-prefix chaining (time order)."""
    ordered = sorted(calls, key=lambda c: (str(c.get("started_at") or ""), int(c.get("seq") or 0)))
    groups: list[list[dict[str, Any]]] = []
    tails: list[list[tuple[str, str]]] = []
    for call in ordered:
        pairs = _normalized_pairs(str(call.get("kind") or kind_of(call)), call.get("request_body"))
        placed = False
        for index, tail in enumerate(tails):
            if _is_prefix(tail, pairs) or _is_prefix(pairs, tail):
                groups[index].append(call)
                if len(pairs) > len(tail):
                    tails[index] = pairs
                placed = True
                break
        if not placed:
            groups.append([call])
            tails.append(pairs)
    return groups


def kind_of(call: dict[str, Any]) -> str:
    """Classify a stored call record by its (stripped) path."""
    path = call.get("path")
    return classify_kind(path if isinstance(path, str) else "")


def _anchor_match(root_first_user: str, trial: TrialEvidence) -> bool:
    if not root_first_user:
        return False
    if root_first_user in trial.atif_user_texts:
        return True
    if trial.instruction and root_first_user == trial.instruction:
        return True
    # full instruction.md inside a larger first user message), so containment
    # either way anchors — guarded so short greetings cannot match vacuously.
    instruction = trial.instruction or ""
    if len(instruction) < _MIN_ANCHOR_CHARS or len(root_first_user) < _MIN_ANCHOR_CHARS:
        return False
    return instruction in root_first_user or root_first_user in instruction


def _window_contains(trial: TrialEvidence, start: datetime | None, end: datetime | None) -> bool:
    if trial.window_start is None or trial.window_end is None:
        return False
    if start is None or end is None:
        return False
    return trial.window_start <= start and end <= trial.window_end


@dataclass
class Attribution:
    """Call-to-trial assignment produced by ``link_capture``."""

    assigned: dict[int, str] = field(default_factory=dict)
    method: dict[int, str] = field(default_factory=dict)
    unassigned: list[int] = field(default_factory=list)
    ambiguous_trials: set[str] = field(default_factory=set)
    ambiguous_conversations: int = 0


def attribute_calls(calls: list[dict[str, Any]], trials: list[TrialEvidence]) -> Attribution:
    """Assign calls to trials: route token, then session, then conversation chaining.

    A present route token foreign to this job stays unassigned (never chained).
    """
    by_name: dict[str, list[TrialEvidence]] = {}
    for trial in trials:
        by_name.setdefault(trial.name, []).append(trial)
    by_session: dict[str, list[TrialEvidence]] = {}
    for trial in trials:
        if trial.atif_session:
            by_session.setdefault(trial.atif_session, []).append(trial)
    attribution = Attribution()
    pending: list[dict[str, Any]] = []
    for call in calls:
        seq = int(call.get("seq") or 0)
        token = call.get("route_token")
        matched: list[TrialEvidence] | None = None
        how = ""
        if isinstance(token, str) and token:
            candidates = [t for t in trials if t.name == token or t.trial_id == token]
            if len(candidates) == 1:
                matched, how = candidates, "route_token"
            elif len(candidates) > 1:
                for candidate in candidates:
                    attribution.ambiguous_trials.add(candidate.name)
                attribution.unassigned.append(seq)
                continue
            else:
                # Runner-stamped job attempt id (secret proxy ``/t/<token>/``
                # prefix): exact for one-trial jobs. A multi-trial job shares
                # one attempt id, so those calls fall through to conversation
                # chaining instead of going ambiguous here. A token foreign
                # to this job (another job's calls in a shared capture)
                # stays unassigned here: it must never be claimed by
                # session/conversation chaining below.
                job_candidates = [t for t in trials if t.attempt_id == token]
                if len(job_candidates) == 1:
                    matched, how = job_candidates, "route_token"
                elif not job_candidates:
                    attribution.unassigned.append(seq)
                    continue
        if matched is None and isinstance(call.get("session_id"), str) and call["session_id"]:
            candidates = by_session.get(str(call["session_id"]), [])
            if len(candidates) == 1:
                matched, how = candidates, "session"
            elif len(candidates) > 1:
                for candidate in candidates:
                    attribution.ambiguous_trials.add(candidate.name)
                attribution.unassigned.append(seq)
                continue
        if matched is not None:
            attribution.assigned[seq] = matched[0].name
            attribution.method[seq] = how
        else:
            pending.append(call)
    for conversation in _conversations(pending):
        first = conversation[0]
        root_user = first_user_text(kind_of(first), first.get("request_body"))
        starts = [parse_ts(c.get("started_at")) for c in conversation]
        ends = [parse_ts(c.get("ended_at")) for c in conversation]
        conv_start = min((s for s in starts if s is not None), default=None)
        conv_end = max((e for e in ends if e is not None), default=None)
        candidates = [
            trial
            for trial in trials
            if _anchor_match(root_user, trial) and _window_contains(trial, conv_start, conv_end)
        ]
        if len(candidates) == 1:
            for call in conversation:
                seq = int(call.get("seq") or 0)
                attribution.assigned[seq] = candidates[0].name
                attribution.method[seq] = "conversation"
        else:
            for call in conversation:
                attribution.unassigned.append(int(call.get("seq") or 0))
            if len(candidates) > 1:
                attribution.ambiguous_conversations += 1
                for candidate in candidates:
                    attribution.ambiguous_trials.add(candidate.name)
    return attribution


# --------------------------------------------------------------------------- #
# Completeness verdicts
# --------------------------------------------------------------------------- #

#: Per-trial completeness of the harness trajectory against the independent
#: record. ``capture_missing`` also covers the both-absent corner (no ATIF and
#: no captured calls): with zero evidence on either side an idle control agent
#: is indistinguishable from a full bypass, so the operator must check the
#: ``agent_execution`` window in ``result.json``; the receipt says so.
VERDICTS = (
    "complete",
    "trajectory_missing",
    "trajectory_truncated",
    "capture_missing",
    "ambiguous",
)


def _captured_texts(calls: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    for call in calls:
        for text in call.get("assistant_texts") or []:
            normalized = normalize_text(text)
            if normalized:
                texts.append(normalized)
    return texts


def _contains(needle: str, haystack: str) -> bool:
    return needle in haystack or haystack in needle


_MIN_ANCHOR_CHARS = 64


_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_MIN_ALNUM_MATCH = 32


def _alnum(text: str) -> str:
    """Lowercased alphanumeric skeleton: immune to code fences, JSON syntax,
    key renaming (``"analysis": "x"`` vs ``Analysis: x``), and whitespace."""
    return _ALNUM_RE.sub("", text.lower())


def _turn_present(text: str, atif_texts: list[str]) -> bool:
    """Whether a captured turn survives anywhere in the ATIF agent messages.

    Plain containment covers verbatim records; the alphanumeric fallback covers
    harnesses that store a parsed rendering (e.g. Terminus keeps ``Analysis: …``
    while the model emitted fenced ``{"analysis": "…"}``). The fallback needs a
    32-char skeleton on both sides so short greetings cannot match vacuously.
    """
    if any(_contains(text, agent_text) for agent_text in atif_texts):
        return True
    skeleton = _alnum(text)
    if len(skeleton) < _MIN_ALNUM_MATCH:
        return False
    for agent_text in atif_texts:
        agent_skeleton = _alnum(agent_text)
        if len(agent_skeleton) < _MIN_ALNUM_MATCH:
            continue
        if skeleton in agent_skeleton or agent_skeleton in skeleton:
            return True
    return False


def judge_trial(
    trial: TrialEvidence, calls: list[dict[str, Any]], *, ambiguous: bool
) -> dict[str, Any]:
    """Judge one trial; ``calls`` are the captured calls assigned to it."""
    texts = _captured_texts(calls)
    verdict: str
    first_divergence: dict[str, Any] | None = None
    detail = ""
    if ambiguous:
        verdict = "ambiguous"
        detail = "attribution matched more than one trial; calls left unassigned"
    elif not trial.atif_present and texts:
        verdict = "trajectory_missing"
        detail = "calls captured but the harness trajectory is absent or empty"
    elif trial.atif_present and not texts and not calls:
        verdict = "capture_missing"
        detail = "harness trajectory present but no calls captured (proxy not in path?)"
    elif not trial.atif_present and not calls:
        verdict = "capture_missing"
        detail = (
            "no harness trajectory and no captured calls; idle control, pre-call "
            "crash, or full bypass — check the agent_execution window"
        )
    elif len(texts) > trial.atif_agent_steps:
        verdict = "trajectory_truncated"
        detail = (
            f"fewer ATIF agent steps ({trial.atif_agent_steps}) than captured "
            f"assistant turns ({len(texts)})"
        )
        first_divergence = {"kind": "count_mismatch"}
    else:
        missing_index: int | None = None
        for index, text in enumerate(texts):
            if not _turn_present(text, trial.atif_agent_texts):
                missing_index = index
                break
        if missing_index is not None:
            verdict = "trajectory_truncated"
            excerpt = texts[missing_index][:280]
            detail = f"captured assistant turn {missing_index} absent from the ATIF"
            first_divergence = {
                "kind": "missing_turn",
                "turn_index": missing_index,
                "excerpt": excerpt,
            }
        else:
            verdict = "complete"
            detail = "captured turns agree with the harness trajectory"
    return {
        "trial_name": trial.name,
        "trial_id": trial.trial_id,
        "agent": trial.agent,
        "verdict": verdict,
        "detail": detail,
        "captured_calls": len(calls),
        "captured_assistant_turns": len(texts),
        "atif_agent_steps": trial.atif_agent_steps,
        "atif_present": trial.atif_present,
        "first_divergence": first_divergence,
    }


# --------------------------------------------------------------------------- #
# link: Parquet output and receipt
# --------------------------------------------------------------------------- #


def _model_calls_schema() -> Any:
    import pyarrow as pa

    return pa.schema(
        [
            ("seq", pa.int64()),
            ("trial_name", pa.string()),
            ("trial_id", pa.string()),
            ("attribution", pa.string()),
            ("started_at", pa.string()),
            ("ended_at", pa.string()),
            ("method", pa.string()),
            ("path", pa.string()),
            ("route_token", pa.string()),
            ("session_id", pa.string()),
            ("model", pa.string()),
            ("response_status", pa.int64()),
            ("response_sse", pa.bool_()),
            ("assistant_turns", pa.int64()),
            ("tool_calls", pa.int64()),
            ("prompt_tokens", pa.int64()),
            ("completion_tokens", pa.int64()),
            ("upstream_latency_s", pa.float64()),
            ("error_kind", pa.string()),
        ]
    )


def _trial_capture_schema() -> Any:
    import pyarrow as pa

    return pa.schema(
        [
            ("trial_name", pa.string()),
            ("trial_id", pa.string()),
            ("agent", pa.string()),
            ("verdict", pa.string()),
            ("captured_calls", pa.int64()),
            ("captured_assistant_turns", pa.int64()),
            ("atif_agent_steps", pa.int64()),
            ("atif_present", pa.bool_()),
            ("first_divergence", pa.string()),
            ("capture_digest", pa.string()),
            ("linked_at", pa.string()),
        ]
    )


def _usage_int(usage: Any, key: str) -> int | None:
    if isinstance(usage, dict) and isinstance(usage.get(key), int):
        return int(usage[key])
    return None


def link_capture(
    capture_dir: str | Path,
    job_dir: str | Path,
    *,
    derived_root: str | Path | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Attribute a capture to a job's trials; write Parquet + receipt; return summary."""
    from evallab.evidence.parquet_io import write_table_atomic
    from evallab.storage.fs import durable_mkdir
    from evallab.storage.paths import derived_root_from_environment

    capture_path = Path(capture_dir)
    job_path = Path(job_dir)
    calls: list[dict[str, Any]] = []
    calls_path = capture_path / "calls.jsonl"
    if calls_path.is_file():
        with calls_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    calls.append(payload)
    trials = [collect_trial_evidence(trial, job_path) for trial in iter_trial_dirs(job_path)]
    attribution = attribute_calls(calls, trials)
    by_seq = {int(c.get("seq") or 0): c for c in calls}
    trial_calls: dict[str, list[dict[str, Any]]] = {trial.name: [] for trial in trials}
    for seq, name in attribution.assigned.items():
        if seq in by_seq and name in trial_calls:
            trial_calls[name].append(by_seq[seq])
    judgments = [
        judge_trial(
            trial, trial_calls[trial.name], ambiguous=trial.name in attribution.ambiguous_trials
        )
        for trial in trials
    ]
    capture_digest = (
        f"sha256:{sha256_file(calls_path)}" if calls_path.is_file() else "sha256:" + "0" * 64
    )
    linked_at = utc_now_iso()
    base = Path(derived_root) if derived_root is not None else None
    if base is None:
        anchor = Path(repo_root) if repo_root is not None else job_path
        base = derived_root_from_environment(anchor, notify=lambda _message: None)
    job_result = _load_json(job_path / "result.json") or {}
    job_id = job_result.get("id") if isinstance(job_result.get("id"), str) else None
    leaf = f"job_id={job_id}" if job_id else f"job_name={job_path.name}"
    out_dir = base / leaf
    durable_mkdir(out_dir)
    model_rows: list[dict[str, Any]] = []
    for call in sorted(calls, key=lambda c: int(c.get("seq") or 0)):
        seq = int(call.get("seq") or 0)
        trial_name = attribution.assigned.get(seq)
        trial_id = next((t.trial_id for t in trials if t.name == trial_name), None)
        error = call.get("error")
        model_rows.append(
            {
                "seq": seq,
                "trial_name": trial_name,
                "trial_id": trial_id,
                "attribution": attribution.method.get(seq, "unassigned"),
                "started_at": call.get("started_at"),
                "ended_at": call.get("ended_at"),
                "method": call.get("method"),
                "path": call.get("path"),
                "route_token": call.get("route_token"),
                "session_id": call.get("session_id"),
                "model": call.get("model"),
                "response_status": call.get("response_status"),
                "response_sse": bool(call.get("response_sse")),
                "assistant_turns": len(
                    [t for t in (call.get("assistant_texts") or []) if normalize_text(t)]
                ),
                "tool_calls": len(call.get("tool_calls") or []),
                "prompt_tokens": _usage_int(call.get("usage"), "prompt_tokens"),
                "completion_tokens": _usage_int(call.get("usage"), "completion_tokens"),
                "upstream_latency_s": call.get("upstream_latency_s"),
                "error_kind": error.get("kind") if isinstance(error, dict) else None,
            }
        )
    trial_rows: list[dict[str, Any]] = []
    for judgment in judgments:
        trial_rows.append(
            {
                "trial_name": judgment["trial_name"],
                "trial_id": judgment["trial_id"],
                "agent": judgment["agent"],
                "verdict": judgment["verdict"],
                "captured_calls": judgment["captured_calls"],
                "captured_assistant_turns": judgment["captured_assistant_turns"],
                "atif_agent_steps": judgment["atif_agent_steps"],
                "atif_present": judgment["atif_present"],
                "first_divergence": json.dumps(judgment["first_divergence"], sort_keys=True)
                if judgment["first_divergence"] is not None
                else None,
                "capture_digest": capture_digest,
                "linked_at": linked_at,
            }
        )
    write_table_atomic(out_dir / "model_calls.parquet", model_rows, _model_calls_schema())
    write_table_atomic(out_dir / "trial_capture.parquet", trial_rows, _trial_capture_schema())
    receipt = {
        "schema": LINK_RECEIPT_SCHEMA,
        "job": job_path.name,
        "job_id": job_id,
        "capture_dir": str(capture_path),
        "capture_digest": capture_digest,
        "linked_at": linked_at,
        "proxy_version": PROXY_VERSION,
        "parquet_dir": str(out_dir),
        "calls_total": len(calls),
        "calls_assigned": len(attribution.assigned),
        "calls_unassigned": sorted(attribution.unassigned),
        "ambiguous_trials": sorted(attribution.ambiguous_trials),
        "ambiguous_conversations": attribution.ambiguous_conversations,
        "trials": judgments,
    }
    (out_dir / "capture_link.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return receipt


# --------------------------------------------------------------------------- #
# Report lookup
# --------------------------------------------------------------------------- #


def _checkout_anchored_derived(start: Path) -> Path:
    """Derived root for a trial/job path, anchored like the link writer.

    ``link`` resolves the derived store from the invoking checkout root, so the
    reader must not anchor at the trial dir itself (which has no ``.git`` and
    would resolve to a ``derived/`` folder inside the trial). Walk up to the
    containing checkout; outside any checkout fall back to the invoking one.
    """
    from evallab.storage.paths import derived_root_from_environment

    def quiet(_message: str) -> None:
        return None

    node = start.resolve()
    for ancestor in (node, *node.parents):
        if (ancestor / ".git").exists():
            return derived_root_from_environment(ancestor, notify=quiet)
    return derived_root_from_environment(Path.cwd(), notify=quiet)


def find_trial_capture(trial_dir: str | Path) -> dict[str, Any] | None:
    """Return the linked ``trial_capture`` row for a trial, or ``None``.

    ``None`` means absent: no linked capture, no derived root, or an unreadable
    table. Never raises: the run report must render without capture evidence.
    """
    try:
        trial = Path(trial_dir)
        job = trial.parent
        job_result = _load_json(job / "result.json") or {}
        job_id = job_result.get("id") if isinstance(job_result.get("id"), str) else None
        leaf = f"job_id={job_id}" if job_id else f"job_name={job.name}"
        trial_result = _load_json(trial / "result.json") or {}
        trial_name = trial_result.get("trial_name")
        if not isinstance(trial_name, str) or not trial_name:
            return None
        base = _checkout_anchored_derived(trial)
        table_path = base / leaf / "trial_capture.parquet"
        if not table_path.is_file():
            return None
        import pyarrow.parquet as pq

        table = pq.read_table(table_path, filters=[("trial_name", "=", trial_name)])
        if table.num_rows == 0:
            return None
        row = table.slice(0, 1).to_pylist()[0]
        if isinstance(row.get("first_divergence"), str):
            with contextlib.suppress(ValueError):
                row["first_divergence"] = json.loads(str(row["first_divergence"]))
        return dict(row)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Operator smoke: one live call through secret proxy -> capture -> upstream
# --------------------------------------------------------------------------- #


class SmokeError(RuntimeError):
    """A failed capture smoke check; the message never carries secrets or bodies."""


SMOKE_KEY_ENV = "MIMO_SELFHOSTED_API_KEY"


def _canonical_sha(value: Any) -> str:
    """SHA-256 over one body in canonical JSON form."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _forwarded_host(upstream: str) -> str:
    """Host authority the capture forwards to (it drops the inbound Host)."""
    split = urlsplit(upstream)
    host = split.hostname or ""
    port = split.port
    default = {"https": 443, "http": 80}.get((split.scheme or "").casefold())
    if port is None or port == default:
        return host
    return f"{host}:{port}"


def run_capture_smoke(
    *,
    upstream: str,
    out_dir: str | Path,
    key_env: str = SMOKE_KEY_ENV,
    max_tokens: int = 64,
    model: str | None = None,
) -> dict[str, Any]:
    """Send one Terminus-shaped chat call via secret proxy -> capture -> upstream.

    The secret proxy is configured exactly as :func:`evallab.runner.run_experiment`
    configures it for the ``mimo_selfhosted`` route (same ``_terminus_proxy_env``
    inputs, ``EVALLAB_MODEL_CAPTURE=1``, job attempt id as route token): the
    requested selector is strictly parsed, so the #604 adapter admission
    applies and an unknown suffix is refused before anything starts. The
    provider key comes from ``key_env`` and never reaches the record or the
    returned summary. The upstream's echoed model must be one the runner
    accepts for the requested selector (an adapter request echoing the base
    id fails, per the runner's identity rule). Raises :class:`SmokeError`
    on any failure.
    """
    from evallab.execution_contracts import (
        CAPTURE_ENABLED_ENV,
        MIMO_SELFHOSTED_MODEL_SELECTOR,
        MIMO_SELFHOSTED_PROXY_PROVIDER,
        MIMO_SELFHOSTED_UPSTREAM_ENV,
        ProxyTrialLimits,
        materialize_mimo_selfhosted_secret_file,
        parse_mimo_selfhosted_model,
    )
    from evallab.runner import (
        _accepted_returned_models,
        _start_terminus_proxy,
        _stop_terminus_proxy,
    )

    selector = model if model is not None else MIMO_SELFHOSTED_MODEL_SELECTOR
    try:
        native = parse_mimo_selfhosted_model(selector)
    except ValueError as exc:
        raise SmokeError(f"smoke model refused: {exc}") from exc

    key = os.environ.get(key_env)
    if not key:
        raise SmokeError(f"provider key env {key_env} is not set")
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    token = f"smoke-{secrets.token_hex(4)}"
    capability = secrets.token_urlsafe(32)
    server, recorder = None, None
    capture_thread = None
    process = None
    saved = dict(os.environ)
    try:
        server, recorder, _manifest = serve_capture(
            upstream=upstream, out_dir=directory, bind="127.0.0.1", port=0
        )
        capture_thread = threading.Thread(target=server.serve_forever, daemon=True)
        capture_thread.start()
        capture_url = f"http://127.0.0.1:{server.server_address[1]}"
        os.environ[MIMO_SELFHOSTED_UPSTREAM_ENV] = capture_url
        os.environ[CAPTURE_ENABLED_ENV] = "1"
        work_dir = Path(tempfile.mkdtemp(prefix="evallab-capture-smoke."))
        secret_path = materialize_mimo_selfhosted_secret_file(work_dir / "key")
        process, proxy_url = _start_terminus_proxy(
            provider=MIMO_SELFHOSTED_PROXY_PROVIDER,
            secret_path=secret_path,
            capability=capability,
            attempt_id=token,
            usage_path=work_dir / "usage.json",
            limits=ProxyTrialLimits(
                max_requests=4,
                max_input_tokens=8000,
                max_output_tokens=512,
                max_total_tokens=8512,
                max_cost_micros=1_000_000,
            ),
            timeout_seconds=300.0,
            work_dir=work_dir,
            mimo_native=native,
        )
        body = {
            "model": selector,
            "messages": [{"role": "user", "content": "smoke ping"}],
            "max_tokens": max_tokens,
        }
        request = urllib.request.Request(
            f"{proxy_url}/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {capability}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                status = response.status
                response.read()
        except OSError as exc:
            raise SmokeError(f"smoke call failed in transport ({type(exc).__name__})") from exc
        if status != 200:
            raise SmokeError(f"smoke call returned status {status}")
        calls_path = directory / "calls.jsonl"
        try:
            records = [
                json.loads(line)
                for line in calls_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except OSError as exc:
            raise SmokeError(f"calls.jsonl unreadable ({type(exc).__name__})") from exc
        if len(records) != 1:
            raise SmokeError(f"expected 1 captured call, found {len(records)}")
        record = records[0]
        if record.get("route_token") != token:
            raise SmokeError("captured call carries the wrong route token")
        echoed = record.get("model")
        if echoed not in _accepted_returned_models(selector):
            raise SmokeError(f"echoed model {echoed!r} does not match requested {selector!r}")
        blob = json.dumps(record)
        for secret in (key, capability):
            if secret and secret in blob:
                raise SmokeError("secret material reached the capture record")
        summary = {
            "status": status,
            "model": record.get("model"),
            "forwarded_host": _forwarded_host(upstream),
            "route_token": token,
            "request_sha256": _canonical_sha(record.get("request_body")),
            "response_sha256": _canonical_sha(record.get("response_body")),
            "calls": len(records),
            "capture_dir": str(directory),
        }
        (directory / "smoke.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return summary
    finally:
        _stop_terminus_proxy(process)
        if server is not None:
            with contextlib.suppress(Exception):
                server.shutdown()
        if recorder is not None:
            with contextlib.suppress(Exception):
                recorder.close()
        os.environ.clear()
        os.environ.update(saved)
