"""Least-privilege DeepSeek reverse proxy for Harbor mini-swe-agent.

Only this sidecar mounts the provider key. Untrusted task/agent processes see
an internal endpoint and a non-provider placeholder, never the credential.
"""

from __future__ import annotations

import http.client
import os
import ssl
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_SECRET_PATH = Path("/run/secrets/evallab_deepseek_api_key")
DEFAULT_UPSTREAM = "https://api.deepseek.com"
ALLOWED_PATHS = frozenset(
    {"/chat/completions", "/v1/chat/completions", "/models", "/v1/models"}
)
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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        # Log method/path/status only. Never headers, bodies, or secrets.
        super().log_message(format, *args)

    def _reject(self, status: int, message: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(message)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(message)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.partition("?")[0] == "/healthz":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._proxy(None)

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
        self._proxy(self.rfile.read(length))

    def _proxy(self, body: bytes | None) -> None:
        path = self.path.partition("?")[0]
        if path not in ALLOWED_PATHS:
            self._reject(404, b"endpoint not allowed\n")
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
            and name.casefold() not in {"authorization", "host", "content-length"}
        }
        headers["Authorization"] = f"Bearer {key}"
        if body is not None:
            headers["Content-Length"] = str(len(body))
        request = urllib.request.Request(
            f"{upstream_base()}{self.path}",
            data=body,
            headers=headers,
            method=self.command,
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
            self.send_response(status)
            for name, value in response.headers.items():
                if name.casefold() not in HOP_BY_HOP and name.casefold() != "content-length":
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            while chunk := response.read(64 * 1024):
                self.wfile.write(chunk)


def serve(host: str = "0.0.0.0", port: int | None = None) -> ThreadingHTTPServer:
    bound_port = int(os.environ.get("PORT", "8080") if port is None else port)
    server = ThreadingHTTPServer((host, bound_port), Handler)
    return server


if __name__ == "__main__":
    serve().serve_forever()
