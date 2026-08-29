"""Least-privilege DeepSeek reverse proxy for Harbor mini-swe-agent.

Only this sidecar mounts the provider key. Untrusted task/agent processes see
an internal endpoint and a per-trial capability, never the credential. Budget
ceilings fail closed even if a tool reads and replays its own capability.
"""

from __future__ import annotations

import base64
import hmac
import http.client
import json
import os
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

DEFAULT_SECRET_PATH = Path("/run/secrets/evallab_deepseek_api_key")
DEFAULT_UPSTREAM = "https://api.deepseek.com"
ALLOWED_PATH = "/v1/chat/completions"
HEALTHZ_PATH = "/healthz"
MAX_REQUEST_BYTES = 4 * 1024 * 1024
PINNED_HTTPS_HOST = "api.deepseek.com"
PINNED_HTTPS_PORT = 443
ALLOWED_HTTP_HOSTS = frozenset(
    {"127.0.0.1", "localhost", "evallab-smoke-upstream", "host.docker.internal"}
)
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


def secret_path() -> Path:
    raw = os.environ.get("EVALLAB_DEEPSEEK_SECRET_PATH")
    return Path(raw) if raw else DEFAULT_SECRET_PATH


def upstream_base() -> str:
    return os.environ.get("EVALLAB_DEEPSEEK_UPSTREAM", DEFAULT_UPSTREAM).rstrip("/")


def provider_key() -> str:
    path = secret_path()
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError("DeepSeek secret file is unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("DeepSeek secret file is unavailable")
        if info.st_uid not in {0, os.geteuid()}:
            raise RuntimeError("DeepSeek secret file is unavailable")
        if (info.st_mode & 0o777) not in {0o400, 0o600}:
            raise RuntimeError("DeepSeek secret file is unavailable")
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
        raise RuntimeError("DeepSeek secret file is empty")
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
    needles = {
        utf8,
        escaped,
        raw_escaped,
        key.encode("unicode_escape"),
        key.encode("utf-16le"),
        key.encode("utf-16be"),
        b64,
        b64.rstrip(b"="),
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


def _int_env(name: str) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
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


class TrialBudget:
    """Concurrency-safe reserved ceilings for one trial's proxy capability."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._cost_micros = 0
        self._nonces: set[bytes] = set()

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
    ) -> bool:
        max_requests = _int_env("EVALLAB_DEEPSEEK_MAX_REQUESTS")
        max_input = _int_env("EVALLAB_DEEPSEEK_MAX_INPUT_TOKENS")
        max_output = _int_env("EVALLAB_DEEPSEEK_MAX_OUTPUT_TOKENS")
        max_cost = _int_env("EVALLAB_DEEPSEEK_MAX_COST_MICROS")
        with self._lock:
            if self._requests + 1 > max_requests:
                return False
            if self._input_tokens + input_tokens > max_input:
                return False
            if self._output_tokens + output_tokens > max_output:
                return False
            if self._cost_micros + cost_micros > max_cost:
                return False
            self._requests += 1
            self._input_tokens += input_tokens
            self._output_tokens += output_tokens
            self._cost_micros += cost_micros
            return True

    def reconcile(
        self,
        *,
        reserved_input: int,
        reserved_output: int,
        reserved_cost: int,
        used_input: int,
        used_output: int,
        used_cost: int,
    ) -> None:
        extra_input = max(0, used_input - reserved_input)
        extra_output = max(0, used_output - reserved_output)
        extra_cost = max(0, used_cost - reserved_cost)
        released_input = max(0, reserved_input - used_input)
        released_output = max(0, reserved_output - used_output)
        released_cost = max(0, reserved_cost - used_cost)
        with self._lock:
            self._input_tokens = max(0, self._input_tokens + extra_input - released_input)
            self._output_tokens = max(0, self._output_tokens + extra_output - released_output)
            self._cost_micros = max(0, self._cost_micros + extra_cost - released_cost)

    def remaining_output(self) -> int:
        max_output = _int_env("EVALLAB_DEEPSEEK_MAX_OUTPUT_TOKENS")
        with self._lock:
            return max(0, max_output - self._output_tokens)


class ProxyServer(ThreadingHTTPServer):
    budget: TrialBudget


def _capability_ok(presented: str) -> bool:
    expected = os.environ.get("EVALLAB_DEEPSEEK_PROXY_CAPABILITY", "")
    if not expected or not presented:
        return False
    left = presented.encode("utf-8")
    right = expected.encode("utf-8")
    if len(left) != len(right):
        hmac.compare_digest(right, right)
        return False
    return hmac.compare_digest(left, right)


def _expired() -> bool:
    raw = os.environ.get("EVALLAB_DEEPSEEK_CAPABILITY_EXPIRES_AT")
    if raw is None or raw == "":
        return True
    try:
        deadline = float(raw)
    except ValueError:
        return True
    return time.time() >= deadline


def _cost_micros(input_tokens: int, output_tokens: int) -> int:
    input_rate = _int_env("EVALLAB_DEEPSEEK_INPUT_COST_MICROS_PER_MILLION")
    output_rate = _int_env("EVALLAB_DEEPSEEK_OUTPUT_COST_MICROS_PER_MILLION")
    return (input_tokens * input_rate + output_tokens * output_rate) // 1_000_000


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _budget(self) -> TrialBudget:
        server = self.server
        assert isinstance(server, ProxyServer)
        return server.budget

    def _reject(self, status: int, message: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(message)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(message)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.partition("?")[0]
        if path == HEALTHZ_PATH:
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._reject(404, b"endpoint not allowed\n")

    def do_POST(self) -> None:  # noqa: N802
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "-1")
        except ValueError:
            self._reject(400, b"invalid content length\n")
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._reject(413, b"request body rejected\n")
            return
        body = self.rfile.read(length)
        self._proxy(body)

    def _presented_capability(self) -> str:
        header = self.headers.get("Authorization", "")
        prefix = "bearer "
        if header.casefold().startswith(prefix):
            return header[len(prefix) :].strip()
        return self.headers.get("X-Evallab-Proxy-Capability", "").strip()

    def _proxy(self, body: bytes) -> None:
        path = self.path.partition("?")[0]
        if path != ALLOWED_PATH or self.path != ALLOWED_PATH:
            self._reject(404, b"endpoint not allowed\n")
            return
        if _expired() or not _capability_ok(self._presented_capability()):
            self._reject(401, b"capability rejected\n")
            return
        nonce = (self.headers.get("X-Evallab-Proxy-Nonce") or "").encode("utf-8")
        if nonce and not self._budget().consume_nonce(nonce):
            self._reject(409, b"replay rejected\n")
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reject(400, b"invalid json\n")
            return
        if not isinstance(payload, dict):
            self._reject(400, b"invalid json\n")
            return
        allowed_model = os.environ.get("EVALLAB_DEEPSEEK_ALLOWED_MODEL", "")
        if not allowed_model or payload.get("model") != allowed_model:
            self._reject(403, b"model not allowed\n")
            return
        try:
            input_tokens = _estimate_tokens(payload)
            max_output = _int_env("EVALLAB_DEEPSEEK_MAX_OUTPUT_TOKENS")
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
            reserved = self._budget().reserve(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_micros=cost,
            )
        except ValueError:
            self._reject(503, b"budget misconfigured\n")
            return
        if not reserved:
            self._reject(429, b"trial budget exhausted\n")
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
            and name.casefold()
            not in {
                "authorization",
                "host",
                "content-length",
                "accept-encoding",
                "te",
                "x-evallab-proxy-capability",
                "x-evallab-proxy-nonce",
            }
        }
        forwarded = {
            name: payload[name]
            for name in ("model", "messages", "tools", "tool_choice", "temperature")
            if name in payload
        }
        forwarded["model"] = allowed_model
        forwarded["max_tokens"] = output_tokens
        forwarded["n"] = 1
        forwarded["stream"] = False
        body = json.dumps(forwarded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Authorization"] = f"Bearer {key}"
        headers["Content-Length"] = str(len(body))
        headers["Accept-Encoding"] = "identity"
        headers["TE"] = "identity"
        try:
            target = _pinned_upstream_url()
        except RuntimeError:
            self._reject(502, b"provider unavailable\n")
            return
        request = urllib.request.Request(
            target,
            data=body,
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
            response = opener.open(request, timeout=120)
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
            upstream_body = response.read()
            if b"\x00" in upstream_body:
                self._reject(502, b"unsupported upstream body\n")
                return
            try:
                sanitized_body = _canonicalize_and_redact_json(upstream_body, key)
            except ValueError:
                self._reject(502, b"unsupported upstream body\n")
                return
            used_input, used_output = input_tokens, output_tokens
            try:
                upstream_payload = json.loads(sanitized_body.decode("ascii"))
            except json.JSONDecodeError:
                self._reject(502, b"unsupported upstream body\n")
                return
            if isinstance(upstream_payload, dict):
                usage = upstream_payload.get("usage") or {}
                if isinstance(usage, dict):
                    used_input = int(usage.get("prompt_tokens") or used_input)
                    used_output = int(usage.get("completion_tokens") or used_output)
            used_cost = _cost_micros(used_input, used_output)
            self._budget().reconcile(
                reserved_input=input_tokens,
                reserved_output=output_tokens,
                reserved_cost=cost,
                used_input=used_input,
                used_output=used_output,
                used_cost=used_cost,
            )
            self.send_response(status)
            content_type = response.headers.get("Content-Type", "application/json")
            sanitized_type = _redact_key(content_type.encode("utf-8"), key).decode("utf-8")
            self.send_header("Content-Type", sanitized_type)
            self.send_header("Content-Encoding", "identity")
            self.send_header("Content-Length", str(len(sanitized_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(sanitized_body)


def serve(host: str = "0.0.0.0", port: int | None = None) -> ThreadingHTTPServer:
    bound_port = int(os.environ.get("PORT", "8080") if port is None else port)
    server = ProxyServer((host, bound_port), Handler)
    server.budget = TrialBudget()
    return server


if __name__ == "__main__":
    serve().serve_forever()
