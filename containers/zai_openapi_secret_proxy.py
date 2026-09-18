"""Least-privilege Z.ai Open Platform standard API reverse proxy for Harbor mini-swe-agent.

Only this sidecar mounts the Z.ai Open Platform provider key. Untrusted task/agent
processes see an internal endpoint and a per-trial capability, never the credential.
Enforces the ``zai/`` model prefix, strips inbound credential headers, injects provider
auth only in the proxy process, and exposes no secret in config/log/error surfaces.
Connects strictly to the Open Platform Standard API (https://api.z.ai/api/paas/v4),
completely decoupled from the Coding Plan endpoint.

Hardening features:
- Worker bounding before thread creation: rejects excess connections with nonblocking 503.
- Inbound request deadline: wall-clock timer covers headers+body acquisition.
- Separate upstream timeout (120s) allowing long model generation without client socket cancellation.
- Pre-body capability authentication: rejects unauthenticated requests before reading body.
- Strict upstream response reading: requires bytes_read == declared Content-Length; EOF-short payloads return 502.
- Size-bounded upstream response reading (limit+1) with sanitized 502 classification.
- Multi-encoding secret redaction (raw, JSON, Base64, URL-encoded, Unicode, UTF-16, Bearer).
- Trial budget accounting with durable usage reports and SSE response handling.
- Provider-returned model identity verification.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import http.client
import json
import os
import socket
import ssl
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_SECRET_PATH = Path("/run/secrets/evallab_zai_openapi_api_key")
DEFAULT_UPSTREAM = "https://api.z.ai"
ALLOWED_PATH = "/api/paas/v4/chat/completions"
ALLOWED_PATHS = frozenset({ALLOWED_PATH, "/chat/completions", "/v1/chat/completions"})
UPSTREAM_PATH = "/api/paas/v4/chat/completions"
HEALTHZ_PATH = "/healthz"
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 15.0
UPSTREAM_TIMEOUT_SECONDS = 120.0
MAX_CONCURRENT_WORKERS = 32

PINNED_HTTPS_HOST = "api.z.ai"
PINNED_HTTPS_PORT = 443
ALLOWED_HTTP_HOSTS = frozenset(
    {"127.0.0.1", "localhost", "evallab-smoke-upstream", "host.docker.internal"}
)
REQUIRED_MODEL_PREFIX = "zai/"
ALLOWED_MODEL_IDS = frozenset({"glm-5.3-flash"})

# Z.ai Open Platform published rates for GLM-5.3-Flash (USD per million tokens):
# Source: https://docs.z.ai/guides/overview/pricing.md (verified 2026-09)
# Input:  $0.15 / 1M tokens = 150,000 micros / 1M tokens
# Output: $0.50 / 1M tokens = 500,000 micros / 1M tokens
DEFAULT_INPUT_COST_MICROS_PER_MILLION = 150_000
DEFAULT_OUTPUT_COST_MICROS_PER_MILLION = 500_000

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

STRIP_INBOUND_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "x-evallab-proxy-capability",
        "x-evallab-proxy-nonce",
        "host",
        "content-length",
        "accept-encoding",
        "te",
    }
)


def secret_path() -> Path:
    raw = os.environ.get("EVALLAB_ZAI_OPENAPI_SECRET_PATH") or os.environ.get(
        "EVALLAB_ZAI_SECRET_PATH"
    )
    return Path(raw) if raw else DEFAULT_SECRET_PATH


def upstream_base() -> str:
    return os.environ.get("EVALLAB_ZAI_OPENAPI_UPSTREAM", DEFAULT_UPSTREAM).rstrip("/")


def provider_key() -> str:
    path = secret_path()
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError("Z.ai OpenAPI secret file is unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("Z.ai OpenAPI secret file is unavailable")
        if info.st_uid not in {0, os.geteuid()}:
            raise RuntimeError("Z.ai OpenAPI secret file is unavailable")
        if (info.st_mode & 0o777) not in {0o400, 0o600}:
            raise RuntimeError("Z.ai OpenAPI secret file is unavailable")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    value = b"".join(chunks).decode("utf-8").rstrip("\r\n")
    if not value:
        raise RuntimeError("Z.ai OpenAPI secret file is empty")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        del req, fp, msg
        raise urllib.error.HTTPError(newurl, code, "redirects disabled", headers, None)


def _pinned_upstream_url() -> str:
    parsed = urllib.parse.urlsplit(upstream_base())
    if parsed.scheme == "https":
        if parsed.hostname != PINNED_HTTPS_HOST:
            raise RuntimeError("upstream host is not pinned")
        port = parsed.port or PINNED_HTTPS_PORT
        if port != PINNED_HTTPS_PORT:
            raise RuntimeError("upstream port is not pinned")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise RuntimeError("upstream path is not pinned")
        return f"https://{PINNED_HTTPS_HOST}:{PINNED_HTTPS_PORT}{UPSTREAM_PATH}"
    if parsed.scheme == "http":
        host = parsed.hostname
        if not host or host not in ALLOWED_HTTP_HOSTS:
            raise RuntimeError("http upstream is not pinned")
        port = parsed.port
        if port is None:
            raise RuntimeError("http upstream port is not pinned")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise RuntimeError("upstream path is not pinned")
        return f"http://{host}:{port}{UPSTREAM_PATH}"
    raise RuntimeError("upstream scheme is not pinned")


def _key_needles(key: str) -> tuple[bytes, ...]:
    utf8 = key.encode("utf-8")
    if not utf8:
        return ()
    escaped = json.dumps(key, ensure_ascii=True)[1:-1].encode("ascii")
    raw_escaped = json.dumps(key, ensure_ascii=False)[1:-1].encode("utf-8")
    b64 = base64.b64encode(utf8)
    url_quoted = urllib.parse.quote(key).encode("ascii")
    needles = {
        utf8,
        escaped,
        raw_escaped,
        url_quoted,
        key.encode("unicode_escape"),
        key.encode("utf-16le"),
        key.encode("utf-16be"),
        b64,
        b64.rstrip(b"="),
        b"Bearer " + utf8,
        b"bearer " + utf8,
        b"BEARER " + utf8,
    }
    return tuple(needle for needle in needles if needle)


def _redact_key(data: bytes, key: str) -> bytes:
    redacted = data
    for needle in _key_needles(key):
        redacted = redacted.replace(needle, b"<redacted>")
    return redacted


def _scrub_json(value: Any, key: str) -> Any:
    if isinstance(value, str):
        return _redact_key(value.encode("utf-8"), key).decode("utf-8")
    if isinstance(value, list):
        return [_scrub_json(item, key) for item in value]
    if isinstance(value, dict):
        return {
            _redact_key(str(name).encode("utf-8"), key).decode("utf-8"): _scrub_json(item, key)
            for name, item in value.items()
        }
    return value


def _canonicalize_and_redact_json(data: bytes, key: str) -> bytes:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("unsupported upstream body") from exc

    canonical = json.dumps(
        _scrub_json(payload, key), ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")
    sanitized = _redact_key(canonical, key)
    for needle in _key_needles(key):
        if needle in sanitized:
            raise ValueError("reflected secret remains")
    return sanitized


def _canonicalize_sse_and_usage(
    data: bytes, key: str
) -> tuple[bytes, dict[str, int] | None, str | None]:
    """Redact a text/event-stream and extract usage from the final data event.

    Returns the sanitized SSE body, a usage dict with ``prompt_tokens`` and
    ``completion_tokens``, and the observed model identity string.
    """
    usage: dict[str, int] | None = None
    returned_models: set[str] = set()
    events: list[list[bytes]] = []
    current: list[bytes] = []
    for line in data.splitlines(keepends=True):
        if line in (b"\n", b"\r\n"):
            if current:
                events.append(current)
                current = []
        else:
            current.append(line.rstrip(b"\r\n"))
    if current:
        events.append(current)

    sanitized_events: list[bytes] = []
    for event in events:
        data_lines: list[bytes] = []
        other_lines: list[bytes] = []
        for line in event:
            if line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip(b" "))
            else:
                other_lines.append(line)

        if not data_lines:
            sanitized_other = [_redact_key(line, key) for line in other_lines]
            for line in sanitized_other:
                for needle in _key_needles(key):
                    if needle in line:
                        raise ValueError("reflected secret remains")
            sanitized_events.append(b"\n".join(sanitized_other))
            continue

        raw_data = b"\n".join(data_lines)
        if raw_data.strip() == b"[DONE]":
            sanitized_lines = [_redact_key(line, key) for line in other_lines]
            sanitized_lines.append(b"data: [DONE]")
            sanitized_events.append(b"\n".join(sanitized_lines))
            continue

        try:
            payload = json.loads(raw_data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("unsupported upstream body") from exc

        event_model = payload.get("model")
        if event_model is not None:
            if "<redacted>" in json.dumps(event_model):
                event_model = None
            if event_model is not None:
                returned_models.add(str(event_model))
        event_usage = payload.get("usage")
        if isinstance(event_usage, dict):
            usage = event_usage

        canonical = json.dumps(
            _scrub_json(payload, key), ensure_ascii=True, separators=(",", ":")
        ).encode("ascii")
        canonical = _redact_key(canonical, key)
        for needle in _key_needles(key):
            if needle in canonical:
                raise ValueError("reflected secret remains")

        sanitized_lines = [_redact_key(line, key) for line in other_lines]
        sanitized_lines.append(b"data: " + canonical)
        sanitized_events.append(b"\n".join(sanitized_lines))

    body = b"\n\n".join(sanitized_events)
    if body:
        body += b"\n\n"
    if len(returned_models) > 1:
        raise ValueError("upstream SSE events disagree on model identity")
    return _redact_key(body, key), usage, next(iter(returned_models), None)


def _response_encoding_ok(headers: http.client.HTTPMessage, *, stream: bool = False) -> bool:
    encoding = (headers.get("Content-Encoding") or "identity").strip().casefold()
    transfer = (headers.get("Transfer-Encoding") or "identity").strip().casefold()
    if encoding not in {"identity", ""}:
        return False
    if transfer not in {"identity", "chunked", ""}:
        return False
    content_type = headers.get("Content-Type") or ""
    media, _, params = content_type.partition(";")
    if stream:
        if media.strip().casefold() not in {"application/json", "text/event-stream"}:
            return False
    else:
        if media.strip().casefold() not in {"application/json"}:
            return False
    charset = "utf-8"
    for part in params.split(";"):
        name, _, value = part.strip().partition("=")
        if name.casefold() == "charset" and value:
            charset = value.strip().strip('"').casefold()
    return charset in {"utf-8", "us-ascii", ""}


def _capability_ok(presented: str) -> bool:
    expected = os.environ.get("EVALLAB_ZAI_OPENAPI_PROXY_CAPABILITY", "")
    if not expected or not presented:
        return False
    left = presented.encode("utf-8")
    right = expected.encode("utf-8")
    if len(left) != len(right):
        hmac.compare_digest(right, right)
        return False
    return hmac.compare_digest(left, right)


def _expired() -> bool:
    raw = os.environ.get("EVALLAB_ZAI_OPENAPI_CAPABILITY_EXPIRES_AT")
    if raw is None or raw == "":
        return False
    try:
        deadline = float(raw)
    except ValueError:
        return True
    return time.time() >= deadline


def _int_env(name: str, default: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        if default is not None:
            return default
        raise ValueError(name)
    value = int(raw)
    if value < 0:
        raise ValueError(name)
    return value


def _estimate_tokens(payload: dict[str, Any]) -> int:
    """Reserve a conservative upper bound. Never trust characters/4."""
    billed = {
        "messages": payload.get("messages"),
        "tools": payload.get("tools"),
        "tool_choice": payload.get("tool_choice"),
    }
    encoded = json.dumps(billed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return max(1, len(encoded))


def _cost_micros(input_tokens: int, output_tokens: int) -> int:
    input_rate = _int_env(
        "EVALLAB_ZAI_OPENAPI_INPUT_COST_MICROS_PER_MILLION",
        DEFAULT_INPUT_COST_MICROS_PER_MILLION,
    )
    output_rate = _int_env(
        "EVALLAB_ZAI_OPENAPI_OUTPUT_COST_MICROS_PER_MILLION",
        DEFAULT_OUTPUT_COST_MICROS_PER_MILLION,
    )
    numerator = input_tokens * input_rate + output_tokens * output_rate
    return (numerator + 999_999) // 1_000_000


def _validate_model(model: Any) -> str | None:
    """Normalize model selector to native model ID."""
    if not isinstance(model, str) or not model:
        return None
    native_model = model.removeprefix(REQUIRED_MODEL_PREFIX)
    if native_model not in ALLOWED_MODEL_IDS:
        return None
    return native_model


class TrialBudget:
    """Concurrency-safe, durable accounting for one trial capability."""

    def __init__(self) -> None:
        capability = os.environ.get("EVALLAB_ZAI_OPENAPI_PROXY_CAPABILITY", "")
        attempt_id = os.environ.get("EVALLAB_ZAI_OPENAPI_ATTEMPT_ID", "")
        usage_path = os.environ.get("EVALLAB_ZAI_OPENAPI_USAGE_FILE", "")
        if not capability or not attempt_id or not usage_path:
            raise ValueError("proxy capability accounting is not configured")
        self._lock = threading.Lock()
        self._requests = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._cost_micros = 0
        self._nonces: set[bytes] = set()
        self._calls: list[dict[str, Any]] = []
        self._sequence = 0
        self._path = Path(usage_path)
        self._attempt_id = attempt_id
        self._capability_id = "sha256:" + hashlib.sha256(capability.encode()).hexdigest()
        self._limits = {
            "max_requests": _int_env("EVALLAB_ZAI_OPENAPI_MAX_REQUESTS"),
            "max_input_tokens": _int_env("EVALLAB_ZAI_OPENAPI_MAX_INPUT_TOKENS"),
            "max_output_tokens": _int_env("EVALLAB_ZAI_OPENAPI_MAX_OUTPUT_TOKENS"),
            "max_total_tokens": _int_env("EVALLAB_ZAI_OPENAPI_MAX_TOTAL_TOKENS"),
            "max_cost_micros": _int_env("EVALLAB_ZAI_OPENAPI_MAX_COST_MICROS"),
        }
        self._pricing = {
            "input_cost_micros_per_million": _int_env(
                "EVALLAB_ZAI_OPENAPI_INPUT_COST_MICROS_PER_MILLION",
                DEFAULT_INPUT_COST_MICROS_PER_MILLION,
            ),
            "output_cost_micros_per_million": _int_env(
                "EVALLAB_ZAI_OPENAPI_OUTPUT_COST_MICROS_PER_MILLION",
                DEFAULT_OUTPUT_COST_MICROS_PER_MILLION,
            ),
        }
        self._persist_locked()

    def _persist_locked(self) -> None:
        unresolved = sum(1 for call in self._calls if call["state"] != "reconciled")
        payload = {
            "schema_version": 1,
            "capability_id": self._capability_id,
            "attempt_id": self._attempt_id,
            "sequence": self._sequence,
            "limits": self._limits,
            "pricing": self._pricing,
            "totals": {
                "requests": self._requests,
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "total_tokens": self._input_tokens + self._output_tokens,
                "cost_micros": self._cost_micros,
            },
            "unresolved_requests": unresolved,
            "calls": self._calls,
        }
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(
            f".{self._path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            with open(descriptor, "wb", closefd=True) as destination:
                destination.write(encoded)
                destination.flush()
                os.fsync(destination.fileno())
        except Exception:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise
        os.replace(temporary, self._path)

    def consume_nonce(self, nonce: bytes) -> bool:
        with self._lock:
            if nonce in self._nonces:
                return False
            self._nonces.add(nonce)
            return True

    def reserve(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_micros: int,
        requested_model: str,
    ) -> int | None:
        with self._lock:
            next_requests = self._requests + 1
            next_input = self._input_tokens + input_tokens
            next_output = self._output_tokens + output_tokens
            next_total = next_input + next_output
            next_cost = self._cost_micros + cost_micros
            if (
                next_requests > self._limits["max_requests"]
                or next_input > self._limits["max_input_tokens"]
                or next_output > self._limits["max_output_tokens"]
                or next_total > self._limits["max_total_tokens"]
                or next_cost > self._limits["max_cost_micros"]
            ):
                return None
            self._requests = next_requests
            self._input_tokens = next_input
            self._output_tokens = next_output
            self._cost_micros = next_cost
            call_id = len(self._calls) + 1
            self._calls.append(
                {
                    "call_id": call_id,
                    "state": "reserved",
                    "reserved_input_tokens": input_tokens,
                    "reserved_output_tokens": output_tokens,
                    "reserved_cost_micros": cost_micros,
                    "requested_model": requested_model,
                }
            )
            self._sequence += 1
            self._persist_locked()
            return call_id

    def reconcile(
        self,
        *,
        call_id: int,
        used_input: int,
        used_output: int,
        used_cost: int,
        status: int,
        returned_model: str | None = None,
        returned_model_reason: str | None = None,
    ) -> None:
        with self._lock:
            call = self._calls[call_id - 1]
            delta_input = used_input - call["reserved_input_tokens"]
            delta_output = used_output - call["reserved_output_tokens"]
            delta_cost = used_cost - call["reserved_cost_micros"]
            self._input_tokens += delta_input
            self._output_tokens += delta_output
            self._cost_micros += delta_cost
            call.update(
                {
                    "state": "reconciled",
                    "status": status,
                    "input_tokens": used_input,
                    "output_tokens": used_output,
                    "cost_micros": used_cost,
                    "returned_model": returned_model,
                    "returned_model_reason": returned_model_reason,
                }
            )
            self._sequence += 1
            self._persist_locked()

    def mark_unresolved(
        self,
        *,
        call_id: int,
        reason: str,
        returned_model: str | None = None,
        returned_model_reason: str | None = None,
    ) -> None:
        with self._lock:
            call = self._calls[call_id - 1]
            call.update(
                {
                    "state": "unresolved",
                    "reason": reason,
                    "returned_model": returned_model,
                    "returned_model_reason": returned_model_reason,
                }
            )
            self._sequence += 1
            self._persist_locked()

    def mark_exceeded(
        self,
        *,
        call_id: int,
        reason: str,
        input_tokens: int,
        output_tokens: int,
        cost_micros: int,
    ) -> None:
        with self._lock:
            call = self._calls[call_id - 1]
            delta_input = input_tokens - call["reserved_input_tokens"]
            delta_output = output_tokens - call["reserved_output_tokens"]
            delta_cost = cost_micros - call["reserved_cost_micros"]
            self._input_tokens += delta_input
            self._output_tokens += delta_output
            self._cost_micros += delta_cost
            call.update(
                {
                    "state": "exceeded",
                    "reason": reason,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost_micros": cost_micros,
                }
            )
            self._sequence += 1
            self._persist_locked()

    def remaining_output(self) -> int:
        with self._lock:
            return max(0, self._limits["max_output_tokens"] - self._output_tokens)


class ProxyServer(ThreadingHTTPServer):
    """Threading HTTPServer with bounded concurrent worker semaphore acquired before thread spawn."""

    budget: TrialBudget

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler],  # noqa: N803
        max_workers: int = MAX_CONCURRENT_WORKERS,
    ) -> None:
        super().__init__(server_address, RequestHandlerClass)
        self.semaphore = threading.BoundedSemaphore(max_workers)

    def process_request(self, request: Any, client_address: Any) -> None:
        """Acquire worker permit before spawning thread; reject 503 if capacity exceeded."""
        if not self.semaphore.acquire(blocking=False):
            body = b"proxy worker capacity exceeded\n"
            response = (
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + body
            )
            with contextlib.suppress(OSError):
                request.settimeout(1.0)
                request.sendall(response)
            self.close_request(request)
            return

        thread = threading.Thread(
            target=self._bounded_process_request,
            args=(request, client_address),
            daemon=True,
        )
        thread.start()

    def _bounded_process_request(self, request: Any, client_address: Any) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.close_request(request)
            self.semaphore.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_TIMEOUT_SECONDS

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _force_close_socket(self) -> None:
        with contextlib.suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.connection.close()

    def _cancel_inbound_timer(self) -> None:
        if hasattr(self, "_inbound_timer") and self._inbound_timer is not None:
            self._inbound_timer.cancel()
            self._inbound_timer = None

    def setup(self) -> None:
        super().setup()
        with contextlib.suppress(AttributeError, OSError):
            self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)
        self._inbound_timer: threading.Timer | None = threading.Timer(
            REQUEST_TIMEOUT_SECONDS, self._force_close_socket
        )
        self._inbound_timer.daemon = True
        self._inbound_timer.start()

    def finish(self) -> None:
        self._cancel_inbound_timer()
        super().finish()

    def _reject(self, status: int, message: bytes) -> None:
        self._cancel_inbound_timer()
        with contextlib.suppress(OSError):
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(message)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(message)

    def do_GET(self) -> None:  # noqa: N802
        self._cancel_inbound_timer()
        path = self.path.partition("?")[0]
        if path == HEALTHZ_PATH:
            body = b"ok\n"
            with contextlib.suppress(OSError):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return
        self._reject(404, b"endpoint not allowed\n")

    def _presented_capability(self) -> str:
        header = self.headers.get("Authorization", "")
        prefix = "bearer "
        if header.casefold().startswith(prefix):
            return header[len(prefix) :].strip()
        return self.headers.get("X-Evallab-Proxy-Capability", "").strip()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.partition("?")[0]
        if path not in ALLOWED_PATHS:
            self._reject(404, b"endpoint not allowed\n")
            return

        # AUTHENTICATE HEADERS BEFORE READING BODY (slow-body DoS protection)
        if _expired() or not _capability_ok(self._presented_capability()):
            self._reject(401, b"capability rejected\n")
            return

        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "-1")
        except ValueError:
            self._reject(400, b"invalid content length\n")
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._reject(413, b"request body rejected\n")
            return

        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            chunk_size = min(remaining, 65536)
            try:
                chunk = self.rfile.read(chunk_size)
            except (TimeoutError, OSError):
                self._reject(408, b"request body timeout\n")
                return
            if not chunk:
                self._reject(400, b"incomplete request body\n")
                return
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)

        self._cancel_inbound_timer()
        with contextlib.suppress(AttributeError, OSError):
            self.connection.settimeout(UPSTREAM_TIMEOUT_SECONDS + 30.0)

        self._proxy(body)

    def _read_upstream_body(self, response: Any) -> bytes | None:
        content_length_hdr = response.headers.get("Content-Length")
        declared_len: int | None = None
        if content_length_hdr is not None:
            try:
                declared_len = int(content_length_hdr)
                if declared_len < 0 or declared_len > MAX_RESPONSE_BYTES:
                    return None
            except ValueError:
                return None

        chunks: list[bytes] = []
        total_read = 0
        limit = MAX_RESPONSE_BYTES
        try:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                total_read += len(chunk)
                if total_read > limit:
                    return None
                chunks.append(chunk)
        except (OSError, http.client.HTTPException):
            return None

        if declared_len is not None and total_read != declared_len:
            return None

        return b"".join(chunks)

    def _budget(self) -> TrialBudget:
        server = self.server
        assert isinstance(server, ProxyServer)
        return server.budget

    def _proxy(self, body: bytes) -> None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reject(400, b"invalid json\n")
            return
        if not isinstance(payload, dict):
            self._reject(400, b"invalid json\n")
            return

        nonce = (self.headers.get("X-Evallab-Proxy-Nonce") or "").encode("utf-8")
        if nonce and not self._budget().consume_nonce(nonce):
            self._reject(409, b"replay rejected\n")
            return

        model = _validate_model(payload.get("model"))
        if model is None:
            self._reject(403, b"model not allowed\n")
            return
        full_model = f"{REQUIRED_MODEL_PREFIX}{model}"
        requested_stream = payload.get("stream", False)
        if not isinstance(requested_stream, bool):
            self._reject(400, b"invalid stream field\n")
            return

        try:
            input_tokens = _estimate_tokens(payload)
            max_output = _int_env("EVALLAB_ZAI_OPENAPI_MAX_OUTPUT_TOKENS")
            requested_output = payload.get("max_tokens")
            if requested_output is None or int(requested_output) <= 0:
                output_tokens = max_output
            else:
                output_tokens = min(int(requested_output), max_output)
            remaining_output = self._budget().remaining_output()
            output_tokens = min(output_tokens, remaining_output)
            if output_tokens <= 0:
                self._reject(429, b"trial budget exhausted\n")
                return
            cost = _cost_micros(input_tokens, output_tokens)
        except (TypeError, ValueError):
            self._reject(400, b"invalid budget fields\n")
            return

        try:
            call_id = self._budget().reserve(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_micros=cost,
                requested_model=full_model,
            )
        except (OSError, ValueError):
            self._reject(503, b"budget accounting unavailable\n")
            return
        if call_id is None:
            self._reject(429, b"trial budget exhausted\n")
            return

        try:
            key = provider_key()
        except RuntimeError:
            self._budget().reconcile(
                call_id=call_id,
                used_input=0,
                used_output=0,
                used_cost=0,
                status=0,
            )
            self._reject(500, b"provider secret unavailable\n")
            return

        headers = {
            name: value
            for name, value in self.headers.items()
            if name.casefold() not in HOP_BY_HOP and name.casefold() not in STRIP_INBOUND_HEADERS
        }

        forwarded = {
            name: payload[name]
            for name in ("model", "messages", "tools", "tool_choice", "temperature")
            if name in payload
        }
        forwarded["model"] = model
        if "max_tokens" in payload and payload["max_tokens"] is not None:
            forwarded["max_tokens"] = min(int(payload["max_tokens"]), output_tokens)
        else:
            forwarded["max_tokens"] = output_tokens
        forwarded["n"] = 1
        forwarded["stream"] = requested_stream
        if requested_stream:
            forwarded["stream_options"] = {"include_usage": True}

        forwarded_body = json.dumps(forwarded, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        headers["Authorization"] = f"Bearer {key}"
        headers["Content-Length"] = str(len(forwarded_body))
        headers["Accept-Encoding"] = "identity"
        headers["TE"] = "identity"

        try:
            target = _pinned_upstream_url()
        except RuntimeError:
            self._budget().reconcile(
                call_id=call_id,
                used_input=0,
                used_output=0,
                used_cost=0,
                status=0,
            )
            self._reject(502, b"provider unavailable\n")
            return

        request = urllib.request.Request(
            target,
            data=forwarded_body,
            headers=headers,
            method="POST",
        )
        context = ssl.create_default_context() if target.startswith("https://") else None
        handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
        if context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=context))
        else:
            handlers.append(urllib.request.HTTPHandler())
        opener = urllib.request.build_opener(*handlers)

        try:
            response = opener.open(request, timeout=UPSTREAM_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as exc:
            if 300 <= int(exc.code) < 400:
                self._budget().mark_unresolved(
                    call_id=call_id,
                    reason="upstream_redirect",
                )
                self._reject(502, b"redirects disabled\n")
                return
            response = exc
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            self._budget().mark_unresolved(
                call_id=call_id,
                reason="upstream_transport_error",
            )
            self._reject(502, b"provider unavailable\n")
            return

        with response:
            raw_status = getattr(response, "status", 502)
            status = raw_status if isinstance(raw_status, int) else 502
            if 300 <= status < 400:
                self._budget().mark_unresolved(
                    call_id=call_id,
                    reason="upstream_redirect",
                )
                self._reject(502, b"redirects disabled\n")
                return

            content_type = response.headers.get("Content-Type") or ""
            is_stream = "text/event-stream" in content_type.casefold()
            if not _response_encoding_ok(response.headers, stream=is_stream):
                self._budget().mark_unresolved(
                    call_id=call_id,
                    reason="unsupported_upstream_encoding",
                )
                self._reject(502, b"unsupported upstream encoding\n")
                return

            upstream_body = self._read_upstream_body(response)
            if upstream_body is None:
                self._budget().mark_unresolved(
                    call_id=call_id,
                    reason="unsupported_upstream_body",
                )
                self._reject(502, b"unsupported upstream body\n")
                return
            if b"\x00" in upstream_body:
                self._budget().mark_unresolved(
                    call_id=call_id,
                    reason="unsupported_upstream_body",
                )
                self._reject(502, b"unsupported upstream body\n")
                return

            returned_model = None
            returned_model_reason = "response_not_observed"
            usage: dict[str, int] | None = None
            try:
                if is_stream:
                    sanitized_body, usage, returned_model = _canonicalize_sse_and_usage(
                        upstream_body, key
                    )
                    returned_model_reason = (
                        "model_absent_or_null" if returned_model is None else None
                    )
                else:
                    sanitized_body = _canonicalize_and_redact_json(upstream_body, key)
                    upstream_payload = json.loads(sanitized_body.decode("ascii"))
                    if not isinstance(upstream_payload, dict):
                        raise ValueError("upstream payload is not an object")
                    returned_model = upstream_payload.get("model")
                    returned_model_reason = (
                        "model_absent_or_null" if returned_model is None else None
                    )
                    if "<redacted>" in json.dumps(returned_model):
                        returned_model = None
                        returned_model_reason = "model_redacted"
                    usage = upstream_payload.get("usage")
            except (KeyError, TypeError, ValueError):
                self._budget().mark_unresolved(
                    call_id=call_id,
                    reason="unreconciled_upstream_usage",
                    returned_model=returned_model,
                    returned_model_reason=returned_model_reason,
                )
                self._reject(502, b"unsupported upstream body\n")
                return

            if status >= 400 and not isinstance(usage, dict):
                self._budget().mark_unresolved(
                    call_id=call_id,
                    reason=f"provider_http_{status}_usage_unknown",
                    returned_model=returned_model,
                    returned_model_reason=returned_model_reason,
                )
                self._reject(status, sanitized_body)
                return

            if not isinstance(usage, dict) or not all(
                k in usage for k in ("prompt_tokens", "completion_tokens")
            ):
                self._budget().mark_unresolved(
                    call_id=call_id,
                    reason="unreconciled_upstream_usage",
                    returned_model=returned_model,
                    returned_model_reason=returned_model_reason,
                )
                self._reject(502, b"unsupported upstream body\n")
                return

            used_input = usage["prompt_tokens"]
            used_output = usage["completion_tokens"]
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (used_input, used_output)
            ):
                self._budget().mark_unresolved(
                    call_id=call_id,
                    reason="negative_upstream_usage",
                    returned_model=returned_model,
                    returned_model_reason=returned_model_reason,
                )
                self._reject(502, b"unsupported upstream body\n")
                return

            used_cost = _cost_micros(used_input, used_output)
            if used_input > input_tokens or used_output > output_tokens or used_cost > cost:
                self._budget().mark_exceeded(
                    call_id=call_id,
                    reason="provider_usage_exceeded_reservation",
                    input_tokens=used_input,
                    output_tokens=used_output,
                    cost_micros=used_cost,
                )
                self._reject(429, b"trial budget exhausted\n")
                return

            self._budget().reconcile(
                call_id=call_id,
                used_input=used_input,
                used_output=used_output,
                used_cost=used_cost,
                status=status,
                returned_model=returned_model,
                returned_model_reason=returned_model_reason,
            )

            self.send_response(status)
            sanitized_type = _redact_key(content_type.encode("utf-8"), key).decode("utf-8")
            self.send_header("Content-Type", sanitized_type)
            self.send_header("Content-Encoding", "identity")
            self.send_header("Content-Length", str(len(sanitized_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(sanitized_body)


def serve(
    host: str = "0.0.0.0",
    port: int | None = None,
    max_workers: int = MAX_CONCURRENT_WORKERS,
) -> ThreadingHTTPServer:
    bound_port = int(os.environ.get("PORT", "8080") if port is None else port)
    server = ProxyServer((host, bound_port), Handler, max_workers=max_workers)
    server.budget = TrialBudget()
    return server


if __name__ == "__main__":
    serve().serve_forever()
