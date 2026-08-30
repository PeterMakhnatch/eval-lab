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
        return f"https://{PINNED_HTTPS_HOST}:{PINNED_HTTPS_PORT}{ALLOWED_PATH}"
    if parsed.scheme == "http":
        host = parsed.hostname
        if not host or host not in ALLOWED_HTTP_HOSTS:
            raise RuntimeError("http upstream is not pinned")
        port = parsed.port
        if port is None:
            raise RuntimeError("http upstream port is not pinned")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise RuntimeError("upstream path is not pinned")
        return f"http://{host}:{port}{ALLOWED_PATH}"
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

    canonical = json.dumps(_scrub(payload), ensure_ascii=True, separators=(",", ":")).encode("ascii")
    sanitized = _redact_key(canonical, key)
    for needle in _key_needles(key):
        if needle in sanitized:
            raise ValueError("reflected secret remains")
    return sanitized


def _response_encoding_ok(headers: http.client.HTTPMessage) -> bool:
    encoding = (headers.get("Content-Encoding") or "identity").strip().casefold()
    transfer = (headers.get("Transfer-Encoding") or "identity").strip().casefold()
    if encoding not in {"identity", ""}:
        return False
    if transfer not in {"identity", "chunked", ""}:
        return False
    content_type = headers.get("Content-Type") or ""
    media, _, params = content_type.partition(";")
    if media.strip().casefold() not in {"application/json"}:
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
        return False
    try:
        deadline = float(raw)
    except ValueError:
        return True
    return time.time() >= deadline


def _validate_model(model: Any) -> str | None:
    """Validate model selector against zai-coding-plan/ prefix.

    Returns the valid model name string or None if disallowed.
    Disallowed model/provider paths fail closed.
    """
    if not isinstance(model, str) or not model:
        return None
    if "/" not in model:
        return None
    provider, _, suffix = model.partition("/")
    if f"{provider}/" != REQUIRED_MODEL_PREFIX:
        return None
    if not suffix:
        return None
    return model


class ProxyServer(ThreadingHTTPServer):
    """Threading HTTPServer with bounded concurrent worker semaphore acquired before thread spawn."""

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

        try:
            key = provider_key()
        except RuntimeError:
            self._reject(500, b"provider secret unavailable\n")
            return

        headers = {
            name: value
            for name, value in self.headers.items()
            if name.casefold() not in HOP_BY_HOP
            and name.casefold() not in STRIP_INBOUND_HEADERS
        }

        # Forward safe fields verbatim without fallback or model substitution
        forwarded: dict[str, Any] = {
            name: payload[name]
            for name in ("model", "messages", "tools", "tool_choice", "temperature")
            if name in payload
        }
        forwarded["model"] = model
        if "max_tokens" in payload and payload["max_tokens"] is not None:
            forwarded["max_tokens"] = payload["max_tokens"]
        forwarded["n"] = 1
        forwarded["stream"] = False

        forwarded_body = json.dumps(forwarded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Authorization"] = f"Bearer {key}"
        headers["Content-Length"] = str(len(forwarded_body))
        headers["Accept-Encoding"] = "identity"
        headers["TE"] = "identity"

        try:
            target = _pinned_upstream_url()
        except RuntimeError:
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
                self._reject(502, b"redirects disabled\n")
                return
            response = exc
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            self._reject(502, b"provider unavailable\n")
            return

        with response:
            raw_status = getattr(response, "status", 502)
            status = raw_status if isinstance(raw_status, int) else 502
            if 300 <= status < 400:
                self._reject(502, b"redirects disabled\n")
                return
            if not _response_encoding_ok(response.headers):  # ty: ignore[invalid-argument-type]
                self._reject(502, b"unsupported upstream encoding\n")
                return

            upstream_body = self._read_upstream_body(response)
            if upstream_body is None:
                self._reject(502, b"unsupported upstream body\n")
                return
            if b"\x00" in upstream_body:
                self._reject(502, b"unsupported upstream body\n")
                return

            try:
                sanitized_body = _canonicalize_and_redact_json(upstream_body, key)
            except ValueError:
                self._reject(502, b"unsupported upstream body\n")
                return

            with contextlib.suppress(OSError):
                self.send_response(status)
                content_type = response.headers.get("Content-Type", "application/json")
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
    return ProxyServer((host, bound_port), Handler, max_workers=max_workers)


if __name__ == "__main__":
    serve().serve_forever()
