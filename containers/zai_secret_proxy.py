"""Least-privilege Z.ai Coding Plan reverse proxy for Harbor OpenCode agent.

Only this sidecar mounts the Z.ai provider key. Untrusted task/agent processes
see an internal endpoint and a per-trial capability, never the credential.
Enforces the ``zai-coding-plan/`` model prefix, strips inbound credential
headers, rejects Highspeed/access errors without fallback, injects provider
auth only in the proxy process, and exposes no secret in config/log/error surfaces.

Hardening features:
- Worker bounding before thread creation: rejects excess connections with nonblocking 503 without stalling accept loop.
- Inbound request deadline: wall-clock timer covers headers+body acquisition and cancels before upstream wait.
- Separate upstream timeout (120s) allowing long model generation without client socket cancellation.
- Pre-body capability authentication: rejects unauthenticated requests before reading body.
- Strict upstream response reading: requires bytes_read == declared Content-Length; EOF-short payloads return 502.
- Size-bounded upstream response reading (limit+1) with sanitized 502 classification.
- Multi-encoding secret redaction (raw, JSON, Base64, URL-encoded, Unicode, UTF-16, Bearer).
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

DEFAULT_SECRET_PATH = Path("/run/secrets/evallab_zai_api_key")
DEFAULT_UPSTREAM = "https://api.z.ai"
ALLOWED_PATH = "/api/paas/v4/chat/completions"
UPSTREAM_PATH = "/api/coding/paas/v4/chat/completions"
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
REQUIRED_MODEL_PREFIX = "zai-coding-plan/"
ALLOWED_MODEL_IDS = frozenset({"glm-5.3", "glm-5.3-flash", "glm-5.3-highspeed"})

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
    raw = os.environ.get("EVALLAB_ZAI_SECRET_PATH")
    return Path(raw) if raw else DEFAULT_SECRET_PATH


def upstream_base() -> str:
    return os.environ.get("EVALLAB_ZAI_UPSTREAM", DEFAULT_UPSTREAM).rstrip("/")


def provider_key() -> str:
    path = secret_path()
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError("Z.ai secret file is unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("Z.ai secret file is unavailable")
        if info.st_uid not in {0, os.geteuid()}:
            raise RuntimeError("Z.ai secret file is unavailable")
        if (info.st_mode & 0o777) not in {0o400, 0o600}:
            raise RuntimeError("Z.ai secret file is unavailable")
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
        raise RuntimeError("Z.ai secret file is empty")
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


def _canonicalize_and_redact_json(data: bytes, key: str) -> bytes:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("unsupported upstream body") from exc

    def _scrub(value: object) -> object:
        if isinstance(value, str):
            return _redact_key(value.encode("utf-8"), key).decode("utf-8")
        if isinstance(value, list):
            return [_scrub(item) for item in value]
        if isinstance(value, dict):
            return {
                _redact_key(str(name).encode("utf-8"), key).decode("utf-8"): _scrub(item)
                for name, item in value.items()
            }
        return value

    canonical = json.dumps(_scrub(payload), ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    sanitized = _redact_key(canonical, key)
    for needle in _key_needles(key):
        if needle in sanitized:
            raise ValueError("reflected secret remains")
    return sanitized


def _canonicalize_sse_and_usage(
    data: bytes,
    key: str,
) -> tuple[bytes, dict[str, Any]]:
    """Buffer, sanitize, and extract metering from an OpenAI-compatible SSE response."""
    output: list[bytes] = []
    usage: dict[str, Any] | None = None
    saw_done = False
    for raw_line in data.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(b":"):
            output.append(b": keep-alive\n\n")
            continue
        if not line.startswith(b"data:"):
            raise ValueError("unsupported upstream SSE event")
        event_data = line[len(b"data:") :].lstrip()
        if event_data == b"[DONE]":
            output.append(b"data: [DONE]\n\n")
            saw_done = True
            continue
        sanitized = _canonicalize_and_redact_json(event_data, key)
        event = json.loads(sanitized.decode("ascii"))
        if not isinstance(event, dict):
            raise ValueError("upstream SSE payload is not an object")
        event_usage = event.get("usage")
        if isinstance(event_usage, dict):
            usage = event_usage
        output.append(b"data: " + sanitized + b"\n\n")
    if not saw_done or usage is None:
        raise ValueError("upstream SSE usage is missing")
    return b"".join(output), usage


def _response_encoding_ok(headers: http.client.HTTPMessage) -> bool:
    encoding = (headers.get("Content-Encoding") or "identity").strip().casefold()
    transfer = (headers.get("Transfer-Encoding") or "identity").strip().casefold()
    if encoding not in {"identity", ""}:
        return False
    if transfer not in {"identity", "chunked", ""}:
        return False
    content_type = headers.get("Content-Type") or ""
    media, _, params = content_type.partition(";")
    if media.strip().casefold() not in {"application/json", "text/event-stream"}:
        return False
    charset = "utf-8"
    for part in params.split(";"):
        name, _, value = part.strip().partition("=")
        if name.casefold() == "charset" and value:
            charset = value.strip().strip('"').casefold()
    return charset in {"utf-8", "us-ascii", ""}


def _capability_ok(presented: str) -> bool:
    expected = os.environ.get("EVALLAB_ZAI_PROXY_CAPABILITY", "")
    if not expected or not presented:
        return False
    left = presented.encode("utf-8")
    right = expected.encode("utf-8")
    if len(left) != len(right):
        hmac.compare_digest(right, right)
        return False
    return hmac.compare_digest(left, right)


def _expired() -> bool:
    raw = os.environ.get("EVALLAB_ZAI_CAPABILITY_EXPIRES_AT")
    if raw is None or raw == "":
        return True
    try:
        deadline = float(raw)
    except ValueError:
        return True
    return time.time() >= deadline


def _validate_model(model: Any) -> str | None:
    """Validate and normalize OpenCode's Z.ai model selector.

    OpenCode sends the provider-native bare model ID on the wire even when its
    CLI selector is ``zai-coding-plan/<model>``. Accept either representation,
    constrain it to the explicit allowlist, and forward only the provider-native
    ID. Disallowed model/provider paths fail closed.
    """
    if not isinstance(model, str) or not model:
        return None
    native_model = (
        model.removeprefix(REQUIRED_MODEL_PREFIX)
        if model.startswith(REQUIRED_MODEL_PREFIX)
        else model
    )
    if native_model not in ALLOWED_MODEL_IDS:
        return None
    return native_model


def _positive_int_env(name: str) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        raise ValueError(name)
    value = int(raw)
    if value <= 0:
        raise ValueError(name)
    return value


def _estimate_input_tokens(payload: dict[str, Any]) -> int:
    """Reserve a conservative token upper bound from the exact billed fields."""
    billed = {
        "messages": payload.get("messages"),
        "tools": payload.get("tools"),
        "tool_choice": payload.get("tool_choice"),
    }
    encoded = json.dumps(billed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return max(1, len(encoded))


def _usage_integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(name)
    return value


class BudgetExceeded(Exception):
    """Typed refusal for one provider-meter ceiling."""

    def __init__(self, ceiling: str) -> None:
        self.ceiling = ceiling
        super().__init__(ceiling)


class TrialBudget:
    """Concurrency-safe, durable accounting bound to one trial capability."""

    def __init__(self) -> None:
        capability = os.environ.get("EVALLAB_ZAI_PROXY_CAPABILITY", "")
        attempt_id = os.environ.get("EVALLAB_ZAI_ATTEMPT_ID", "")
        usage_path = os.environ.get("EVALLAB_ZAI_USAGE_FILE", "")
        if not capability or not attempt_id or not usage_path:
            raise ValueError("proxy capability accounting is not configured")
        self._lock = threading.Lock()
        self._requests = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._cost_micros = 0
        self._calls: list[dict[str, int | str]] = []
        self._sequence = 0
        self._path = Path(usage_path)
        self._attempt_id = attempt_id
        self._capability_id = "sha256:" + hashlib.sha256(capability.encode()).hexdigest()
        self._limits = {
            "max_requests": _positive_int_env("EVALLAB_ZAI_MAX_REQUESTS"),
            "max_input_tokens": _positive_int_env("EVALLAB_ZAI_MAX_INPUT_TOKENS"),
            "max_output_tokens": _positive_int_env("EVALLAB_ZAI_MAX_OUTPUT_TOKENS"),
            "max_total_tokens": _positive_int_env("EVALLAB_ZAI_MAX_TOTAL_TOKENS"),
            "max_cost_micros": _positive_int_env("EVALLAB_ZAI_MAX_COST_MICROS"),
        }
        self._pricing = {
            "input_cost_micros_per_million": _positive_int_env(
                "EVALLAB_ZAI_INPUT_COST_MICROS_PER_MILLION"
            ),
            "output_cost_micros_per_million": _positive_int_env(
                "EVALLAB_ZAI_OUTPUT_COST_MICROS_PER_MILLION"
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
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        os.close(descriptor)
        os.replace(temporary, self._path)

    def _ceiling_reason(
        self,
        *,
        requests: int,
        input_tokens: int,
        output_tokens: int,
        cost_micros: int,
    ) -> str | None:
        if requests > self._limits["max_requests"]:
            return "request_count"
        if input_tokens > self._limits["max_input_tokens"]:
            return "input_tokens"
        if output_tokens > self._limits["max_output_tokens"]:
            return "output_tokens"
        if input_tokens + output_tokens > self._limits["max_total_tokens"]:
            return "total_tokens"
        if cost_micros > self._limits["max_cost_micros"]:
            return "cost"
        return None

    def remaining_output(self) -> int:
        with self._lock:
            return max(
                0,
                self._limits["max_output_tokens"] - self._output_tokens,
            )

    def cost_micros(self, input_tokens: int, output_tokens: int) -> int:
        numerator = (
            input_tokens * self._pricing["input_cost_micros_per_million"]
            + output_tokens * self._pricing["output_cost_micros_per_million"]
        )
        return (numerator + 999_999) // 1_000_000

    def reserve(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_micros: int,
    ) -> int:
        with self._lock:
            reason = self._ceiling_reason(
                requests=self._requests + 1,
                input_tokens=self._input_tokens + input_tokens,
                output_tokens=self._output_tokens + output_tokens,
                cost_micros=self._cost_micros + cost_micros,
            )
            if reason is not None:
                raise BudgetExceeded(reason)
            call_id = self._requests + 1
            self._requests += 1
            self._input_tokens += input_tokens
            self._output_tokens += output_tokens
            self._cost_micros += cost_micros
            self._calls.append(
                {
                    "call_id": call_id,
                    "state": "reserved",
                    "reserved_input_tokens": input_tokens,
                    "reserved_output_tokens": output_tokens,
                    "reserved_cost_micros": cost_micros,
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
    ) -> str | None:
        if min(used_input, used_output, used_cost) < 0:
            raise ValueError("negative provider usage")
        with self._lock:
            call = self._calls[call_id - 1]
            if call["call_id"] != call_id or call["state"] != "reserved":
                raise ValueError("provider call accounting state is invalid")
            reserved_input = int(call["reserved_input_tokens"])
            reserved_output = int(call["reserved_output_tokens"])
            reserved_cost = int(call["reserved_cost_micros"])
            next_input = self._input_tokens - reserved_input + used_input
            next_output = self._output_tokens - reserved_output + used_output
            next_cost = self._cost_micros - reserved_cost + used_cost
            reason = self._ceiling_reason(
                requests=self._requests,
                input_tokens=next_input,
                output_tokens=next_output,
                cost_micros=next_cost,
            )
            self._input_tokens = next_input
            self._output_tokens = next_output
            self._cost_micros = next_cost
            call.update(
                {
                    "state": "exceeded" if reason is not None else "reconciled",
                    "status": status,
                    "input_tokens": used_input,
                    "output_tokens": used_output,
                    "cost_micros": used_cost,
                }
            )
            if reason is not None:
                call["reason"] = reason
            self._sequence += 1
            self._persist_locked()
            return reason

    def mark_unresolved(self, *, call_id: int, reason: str) -> None:
        with self._lock:
            call = self._calls[call_id - 1]
            if call["call_id"] != call_id or call["state"] != "reserved":
                raise ValueError("provider call accounting state is invalid")
            call.update({"state": "unresolved", "reason": reason})
            self._sequence += 1
            self._persist_locked()


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

    def _budget(self) -> TrialBudget:
        server = self.server
        assert isinstance(server, ProxyServer)
        return server.budget

    def _mark_unresolved(self, call_id: int, reason: str) -> bool:
        try:
            self._budget().mark_unresolved(call_id=call_id, reason=reason)
        except (OSError, ValueError):
            self._reject(503, b"budget accounting unavailable\n")
            return False
        return True

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
        # Wall-clock timer covering only inbound request headers + body acquisition
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
        if path != ALLOWED_PATH and self.path != ALLOWED_PATH:
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

        # Incremental body read with deadline
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

        # INBOUND BODY ACQUISITION COMPLETE: CANCEL INBOUND DEADLINE TIMER BEFORE UPSTREAM WAIT
        self._cancel_inbound_timer()

        # Relax socket timeout for legitimate long upstream generations
        with contextlib.suppress(AttributeError, OSError):
            self.connection.settimeout(UPSTREAM_TIMEOUT_SECONDS + 30.0)

        self._proxy(body)

    def _read_upstream_body(self, response: Any) -> bytes | None:
        """Read upstream response incrementally with strict size limit (limit+1) and exact Content-Length verification."""
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
                # Read chunks up to MAX_RESPONSE_BYTES + 1
                chunk = response.read(65536)
                if not chunk:
                    break
                total_read += len(chunk)
                if total_read > limit:
                    return None
                chunks.append(chunk)
        except (OSError, http.client.HTTPException):
            return None

        # Fail closed if upstream closed before delivering declared Content-Length
        if declared_len is not None and total_read != declared_len:
            return None

        return b"".join(chunks)

    def _proxy(self, body: bytes) -> None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reject(400, b"invalid json\n")
            return
        if not isinstance(payload, dict):
            self._reject(400, b"invalid json\n")
            return

        model = _validate_model(payload.get("model"))
        if model is None:
            self._reject(403, b"model not allowed\n")
            return

        requested_output = payload.get("max_tokens")
        requested_stream = payload.get("stream", False)
        if not isinstance(requested_stream, bool):
            self._reject(400, b"invalid stream field\n")
            return
        if requested_output is not None and (
            not isinstance(requested_output, int)
            or isinstance(requested_output, bool)
            or requested_output <= 0
        ):
            self._reject(400, b"invalid budget fields\n")
            return
        try:
            remaining_output = self._budget().remaining_output()
            output_tokens = remaining_output if requested_output is None else requested_output
            if output_tokens <= 0 or output_tokens > remaining_output:
                raise BudgetExceeded("output_tokens")
            input_tokens = _estimate_input_tokens(payload)
            cost_micros = self._budget().cost_micros(input_tokens, output_tokens)
            call_id = self._budget().reserve(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_micros=cost_micros,
            )
        except BudgetExceeded as exc:
            self._reject(
                429,
                f"{exc.ceiling} ceiling exceeded\n".encode("ascii"),
            )
            return
        except (OSError, ValueError):
            self._reject(503, b"budget accounting unavailable\n")
            return

        try:
            key = provider_key()
        except RuntimeError:
            try:
                self._budget().reconcile(
                    call_id=call_id,
                    used_input=0,
                    used_output=0,
                    used_cost=0,
                    status=0,
                )
            except (OSError, ValueError):
                self._reject(503, b"budget accounting unavailable\n")
                return
            self._reject(500, b"provider secret unavailable\n")
            return

        headers = {
            name: value
            for name, value in self.headers.items()
            if name.casefold() not in HOP_BY_HOP and name.casefold() not in STRIP_INBOUND_HEADERS
        }
        forwarded: dict[str, Any] = {
            name: payload[name]
            for name in (
                "model",
                "messages",
                "tools",
                "tool_choice",
                "temperature",
            )
            if name in payload
        }
        forwarded["model"] = model
        forwarded["max_tokens"] = output_tokens
        forwarded["n"] = 1
        forwarded["stream"] = requested_stream
        if requested_stream:
            forwarded["stream_options"] = {"include_usage": True}

        forwarded_body = json.dumps(
            forwarded,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers["Authorization"] = f"Bearer {key}"
        headers["Content-Length"] = str(len(forwarded_body))
        headers["Accept-Encoding"] = "identity"
        headers["TE"] = "identity"

        try:
            target = _pinned_upstream_url()
        except RuntimeError:
            try:
                self._budget().reconcile(
                    call_id=call_id,
                    used_input=0,
                    used_output=0,
                    used_cost=0,
                    status=0,
                )
            except (OSError, ValueError):
                self._reject(503, b"budget accounting unavailable\n")
                return
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
                if not self._mark_unresolved(call_id, "upstream_redirect"):
                    return
                self._reject(502, b"redirects disabled\n")
                return
            response = exc
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            if not self._mark_unresolved(call_id, "upstream_transport_error"):
                return
            self._reject(502, b"provider unavailable\n")
            return

        with response:
            raw_status = getattr(response, "status", 502)
            status = raw_status if isinstance(raw_status, int) else 502
            if 300 <= status < 400:
                if not self._mark_unresolved(call_id, "upstream_redirect"):
                    return
                self._reject(502, b"redirects disabled\n")
                return
            if not _response_encoding_ok(response.headers):  # ty: ignore[invalid-argument-type]
                if not self._mark_unresolved(
                    call_id,
                    "unsupported_upstream_encoding",
                ):
                    return
                self._reject(502, b"unsupported upstream encoding\n")
                return

            upstream_body = self._read_upstream_body(response)
            if upstream_body is None or b"\x00" in upstream_body:
                if not self._mark_unresolved(
                    call_id,
                    "unsupported_upstream_body",
                ):
                    return
                self._reject(502, b"unsupported upstream body\n")
                return

            try:
                media_type = (response.headers.get("Content-Type") or "").partition(";")[0]
                if media_type.strip().casefold() == "text/event-stream":
                    sanitized_body, usage = _canonicalize_sse_and_usage(
                        upstream_body,
                        key,
                    )
                else:
                    sanitized_body = _canonicalize_and_redact_json(upstream_body, key)
                    upstream_payload = json.loads(sanitized_body.decode("ascii"))
                    if not isinstance(upstream_payload, dict):
                        raise ValueError("upstream payload is not an object")
                    usage = upstream_payload.get("usage")
                    if not isinstance(usage, dict):
                        raise ValueError("upstream usage is missing")
                used_input = _usage_integer(
                    usage.get("prompt_tokens"),
                    "prompt_tokens",
                )
                used_output = _usage_integer(
                    usage.get("completion_tokens"),
                    "completion_tokens",
                )
                if "total_tokens" in usage:
                    used_total = _usage_integer(
                        usage.get("total_tokens"),
                        "total_tokens",
                    )
                    if used_total != used_input + used_output:
                        raise ValueError("upstream total usage does not reconcile")
                used_cost = self._budget().cost_micros(
                    used_input,
                    used_output,
                )
                exceeded = self._budget().reconcile(
                    call_id=call_id,
                    used_input=used_input,
                    used_output=used_output,
                    used_cost=used_cost,
                    status=status,
                )
            except (KeyError, TypeError, ValueError):
                if not self._mark_unresolved(
                    call_id,
                    "unreconciled_upstream_usage",
                ):
                    return
                self._reject(502, b"provider usage unavailable\n")
                return
            except OSError:
                self._reject(503, b"budget accounting unavailable\n")
                return

            if exceeded is not None:
                self._reject(
                    429,
                    f"{exceeded} ceiling exceeded\n".encode("ascii"),
                )
                return

            with contextlib.suppress(OSError):
                self.send_response(status)
                content_type = response.headers.get(
                    "Content-Type",
                    "application/json",
                )
                sanitized_type = _redact_key(
                    content_type.encode("utf-8"),
                    key,
                ).decode("utf-8")
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
    budget = TrialBudget()
    server = ProxyServer((host, bound_port), Handler, max_workers=max_workers)
    server.budget = budget
    return server


if __name__ == "__main__":
    serve().serve_forever()
