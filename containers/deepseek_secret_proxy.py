"""Least-privilege DeepSeek reverse proxy for Harbor mini-swe-agent.

Only this sidecar mounts the provider key. Untrusted task/agent processes see
an internal endpoint and a per-trial capability, never the credential. Budget
ceilings fail closed even if a tool reads and replays its own capability.
"""

from __future__ import annotations

import hmac
import http.client
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_SECRET_PATH = Path("/run/secrets/evallab_deepseek_api_key")
DEFAULT_UPSTREAM = "https://api.deepseek.com"
ALLOWED_PATH = "/v1/chat/completions"
HEALTHZ_PATH = "/healthz"
MAX_REQUEST_BYTES = 4 * 1024 * 1024
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
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("DeepSeek secret file is unavailable")
    value = path.read_text(encoding="utf-8").rstrip("\r\n")
    if not value:
        raise RuntimeError("DeepSeek secret file is empty")
    return value


def _int_env(name: str) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        raise ValueError(name)
    value = int(raw)
    if value < 0:
        raise ValueError(name)
    return value


def _estimate_tokens(payload: dict[str, Any]) -> int:
    chunks: list[str] = []

    def _walk(value: object) -> None:
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                _walk(item)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(payload.get("messages"))
    _walk(payload.get("tools"))
    _walk(payload.get("tool_choice"))
    encoded = "".join(chunks).encode("utf-8")
    return max(1, (len(encoded) + 3) // 4)


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
        super().log_message(format, *args)

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
            requested_output = payload.get("max_tokens")
            if requested_output is None:
                output_tokens = _int_env("EVALLAB_DEEPSEEK_MAX_OUTPUT_TOKENS")
            else:
                output_tokens = int(requested_output)
            if output_tokens < 0:
                raise ValueError("max_tokens")
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
                "x-evallab-proxy-capability",
                "x-evallab-proxy-nonce",
            }
        }
        headers["Authorization"] = f"Bearer {key}"
        headers["Content-Length"] = str(len(body))
        request = urllib.request.Request(
            f"{upstream_base()}{ALLOWED_PATH}",
            data=body,
            headers=headers,
            method="POST",
        )
        context = None if upstream_base().startswith("http://") else ssl.create_default_context()
        try:
            response = urllib.request.urlopen(  # noqa: S310 - explicit upstream
                request,
                timeout=120,
                context=context,
            )
        except urllib.error.HTTPError as exc:
            response = exc
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            self._reject(502, b"provider unavailable\n")
            return
        with response:
            raw_status = getattr(response, "status", 502)
            status = raw_status if isinstance(raw_status, int) else 502
            upstream_body = response.read()
            used_input, used_output = input_tokens, output_tokens
            try:
                upstream_payload = json.loads(upstream_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                upstream_payload = None
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
            for name, value in response.headers.items():
                if name.casefold() not in HOP_BY_HOP and name.casefold() != "content-length":
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(upstream_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(upstream_body)


def serve(host: str = "0.0.0.0", port: int | None = None) -> ThreadingHTTPServer:
    bound_port = int(os.environ.get("PORT", "8080") if port is None else port)
    server = ProxyServer((host, bound_port), Handler)
    server.budget = TrialBudget()
    return server


if __name__ == "__main__":
    serve().serve_forever()
