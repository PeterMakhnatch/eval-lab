"""Focused security and behavior tests for Z.ai credential proxy and adapter.

Covers:
1. Proxy in-process HTTP server:
   - Capability verification and expiry (fail-closed 401).
   - Pre-body capability authentication (unauthenticated requests rejected before body read).
   - Worker bounding before thread creation: rejects excess connections with exact 503 response.
   - Inbound request deadline: cancels timer before upstream wait, allowing legitimate >15s model responses.
   - Nonblocking accept loop: overloaded client keeping socket open does not block subsequent connections.
   - Model allowlist enforcement: allows ``zai-coding-plan/glm-5.3`` and
     ``zai-coding-plan/glm-5.3-flash``; rejects disallowed providers/models (403).
   - Highspeed handling: forwards ``zai-coding-plan/glm-5.3-highspeed`` verbatim
     without fallback/substitution; upstream 429 access failure is forwarded
     without fallback so it surfaces as non-scored execution failure.
   - Credential isolation: strips inbound headers, injects provider auth only in
     proxy, redacts secret from responses (raw, JSON, Base64, URL-encoded, Unicode, Bearer).
   - Transport security: rejects redirects, gzip, binary, non-JSON upstream (502).
   - Upstream size limits & exact Content-Length verification: oversized responses (limit+1),
     stream disconnects, and truncated-valid-JSON payloads return sanitized 502.
   - Secret file validation: rejects symlinks, wrong mode/owner (500).
   - Pinned upstream URL enforcement.
2. Adapter integration (``SecretSafeZaiOpenCodeAgent``):
   - Rewrites model_connection to internal proxy with placeholder token.
   - Scrubs real secrets from environment and command execution.
   - Collects host secret file and path environment variables.
   - Sanitizes trajectory files.
3. Compose asset validation (``zai-secret.compose.yaml``).
"""

from __future__ import annotations

import asyncio
import base64
import errno
import gzip
import importlib
import importlib.util
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SECRET_SENTINEL = "zai-secret-token-must-not-leak-12345"
ZAI_PLACEHOLDER = "evallab-proxy-placeholder"


@dataclass(frozen=True)
class _Connection:
    provider: str | None = None
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    configured_base_url: str | None = None
    env: dict[str, str] = field(default_factory=dict, repr=False)


class _FakeOpenCode:
    def __init__(
        self,
        *args: Any,
        version: str | None = None,
        opencode_config: dict[str, Any] | None = None,
        model_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.received_version = version
        self.opencode_config = opencode_config or {}
        self.model_name = model_name or "zai-coding-plan/glm-5.3-flash"
        self.connection = _Connection(
            provider="zai-coding-plan",
            api_key=SECRET_SENTINEL,
            env={"ZAI_CODING_PLAN_API_KEY": SECRET_SENTINEL, "SAFE_KEY": "ok"},
        )
        self.exec_calls: list[tuple[str, dict[str, str] | None]] = []
        self.logs_dir = Path(".")

    @property
    def model_connection(self) -> _Connection:
        return self.connection

    async def exec_as_agent(
        self,
        environment: Any,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> str:
        del environment, cwd, timeout_sec
        self.exec_calls.append((command, env))
        return "ok"

    def populate_context_post_run(self, context: Any) -> None:
        del context
        traj_path = self.logs_dir / "trajectory.json"
        if traj_path.is_file():
            try:
                data = json.loads(traj_path.read_text())
            except Exception:
                data = {}
        else:
            data = {}
        data.setdefault("authorization", f"Bearer {SECRET_SENTINEL}")
        data.setdefault("apiKey", SECRET_SENTINEL)
        data.setdefault("ok", True)
        traj_path.write_text(json.dumps(data) + "\n")


def _module(name: str, **attributes: Any) -> ModuleType:
    mod = ModuleType(name)
    for key, value in attributes.items():
        setattr(mod, key, value)
    return mod


def _package(name: str) -> ModuleType:
    mod = ModuleType(name)
    mod.__path__ = []  # type: ignore[attr-defined]
    return mod


@pytest.fixture
def zai_adapter_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    for name in ("harbor", "harbor.agents", "harbor.agents.installed", "harbor.environments"):
        monkeypatch.setitem(sys.modules, name, _package(name))
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.installed.opencode",
        _module("harbor.agents.installed.opencode", OpenCode=_FakeOpenCode),
    )
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.model_connection",
        _module("harbor.agents.model_connection", ResolvedModelConnection=_Connection),
    )
    monkeypatch.setitem(
        sys.modules,
        "harbor.environments.base",
        _module("harbor.environments.base", BaseEnvironment=object),
    )
    sys.modules.pop("evallab.harbor_zai_opencode", None)
    try:
        yield importlib.import_module("evallab.harbor_zai_opencode")
    finally:
        sys.modules.pop("evallab.harbor_zai_opencode", None)


def _load_proxy_module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "containers" / "zai_secret_proxy.py"
    spec = importlib.util.spec_from_file_location("zai_secret_proxy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


class _MockZaiUpstream(BaseHTTPRequestHandler):
    seen: list[tuple[str, str, bytes]] = []

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        auth = self.headers.get("Authorization", "")
        type(self).seen.append((self.path, auth, body))

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            payload = {}

        model = payload.get("model", "")

        # Highspeed subscription access error (HTTP 429) simulation
        if "highspeed" in model:
            err_body = (
                b'{"error":{"message":"current subscription plan does not yet include '
                b'access to GLM-5.3-Highspeed","type":"subscription_error","code":429}}'
            )
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
            return

        # Success response for allowed models
        resp = json.dumps(
            {
                "id": "chatcmpl-mock-001",
                "object": "chat.completion",
                "created": 1710000000,
                "model": model or "glm-5.3-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


def _setup_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream_handler: type[BaseHTTPRequestHandler] = _MockZaiUpstream,
    capability: str = "test-zai-capability-token-32b",
    max_workers: int = 32,
    *,
    secret_path: Path | None = None,
    attempt_id: str = "test-attempt-001",
    usage_file: Path | None = None,
    max_requests: int = 100,
    max_input_tokens: int = 100_000,
    max_output_tokens: int = 100_000,
    max_total_tokens: int = 200_000,
    max_cost_micros: int = 10_000_000,
    input_cost_micros_per_million: int = 1_000_000,
    output_cost_micros_per_million: int = 2_000_000,
    request_timeout: float | None = None,
) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    if secret_path is None:
        secret_file = tmp_path / "zai_key"
        secret_file.write_text(SECRET_SENTINEL + "\n")
        secret_file.chmod(0o600)
    else:
        secret_file = secret_path

    _MockZaiUpstream.seen = []
    upstream, upstream_url = _serve(upstream_handler)

    actual_usage_file = usage_file or (tmp_path / "zai-proxy-usage.json")

    monkeypatch.setenv("EVALLAB_ZAI_SECRET_PATH", str(secret_file))
    monkeypatch.setenv("EVALLAB_ZAI_UPSTREAM", upstream_url)
    monkeypatch.setenv("EVALLAB_ZAI_PROXY_CAPABILITY", capability)
    monkeypatch.setenv("EVALLAB_ZAI_CAPABILITY_EXPIRES_AT", str(time.time() + 300))
    monkeypatch.setenv("EVALLAB_ZAI_ATTEMPT_ID", attempt_id)
    monkeypatch.setenv("EVALLAB_ZAI_USAGE_FILE", str(actual_usage_file))
    monkeypatch.setenv("EVALLAB_ZAI_MAX_REQUESTS", str(max_requests))
    monkeypatch.setenv("EVALLAB_ZAI_MAX_INPUT_TOKENS", str(max_input_tokens))
    monkeypatch.setenv("EVALLAB_ZAI_MAX_OUTPUT_TOKENS", str(max_output_tokens))
    monkeypatch.setenv("EVALLAB_ZAI_MAX_TOTAL_TOKENS", str(max_total_tokens))
    monkeypatch.setenv("EVALLAB_ZAI_MAX_COST_MICROS", str(max_cost_micros))
    monkeypatch.setenv(
        "EVALLAB_ZAI_INPUT_COST_MICROS_PER_MILLION", str(input_cost_micros_per_million)
    )
    monkeypatch.setenv(
        "EVALLAB_ZAI_OUTPUT_COST_MICROS_PER_MILLION", str(output_cost_micros_per_million)
    )

    proxy_module = _load_proxy_module()
    if request_timeout is not None:
        proxy_module.REQUEST_TIMEOUT_SECONDS = request_timeout
        proxy_module.Handler.timeout = request_timeout
    proxy = proxy_module.serve(host="127.0.0.1", port=0, max_workers=max_workers)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{proxy.server_address[1]}"
    return proxy, upstream, base_url


# ==========================================================================
# 1. Proxy Unit & Integration Tests
# ==========================================================================


def test_proxy_healthz(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proxy, upstream, base_url = _setup_proxy(tmp_path, monkeypatch)
    try:
        resp = urllib.request.urlopen(f"{base_url}/healthz", timeout=5).read()
        assert resp == b"ok\n"
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_rejects_disallowed_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proxy, upstream, base_url = _setup_proxy(tmp_path, monkeypatch)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base_url}/v1/models", timeout=5)
        assert exc.value.code == 404
        assert SECRET_SENTINEL.encode() not in exc.value.read()
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_requires_valid_capability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proxy, upstream, base_url = _setup_proxy(tmp_path, monkeypatch)
    try:
        req = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
            headers={
                "Authorization": "Bearer wrong-capability",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 401
        assert SECRET_SENTINEL.encode() not in exc.value.read()
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_rejects_expired_capability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    capability = "expired-token"
    proxy, upstream, base_url = _setup_proxy(tmp_path, monkeypatch, capability=capability)
    monkeypatch.setenv("EVALLAB_ZAI_CAPABILITY_EXPIRES_AT", str(time.time() - 10))
    try:
        req = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 401
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_unauthenticated_request_rejected_before_reading_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-body auth ensures unauthenticated slow-body attacks are rejected immediately."""
    proxy, upstream, base_url = _setup_proxy(tmp_path, monkeypatch)
    try:
        host, port = proxy.server_address[:2]
        s = socket.create_connection((host, port), timeout=5)
        request_data = (
            b"POST /api/paas/v4/chat/completions HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer invalid-capability\r\n"
            b"Content-Length: 1048576\r\n"
            b"Content-Type: application/json\r\n"
            b"\r\n"
        )
        s.sendall(request_data)
        response_data = b""
        s.settimeout(5)
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            response_data += chunk
            if b"\r\n\r\n" in response_data:
                break
        s.close()
        assert b"401" in response_data.split(b"\r\n")[0]
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_incomplete_body_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Incomplete body (declared 1000 bytes, sends 10 then closes) is rejected."""
    capability = "valid-cap"
    proxy, upstream, base_url = _setup_proxy(tmp_path, monkeypatch, capability=capability)
    try:
        host, port = proxy.server_address[:2]
        s = socket.create_connection((host, port), timeout=5)
        request_data = (
            b"POST /api/paas/v4/chat/completions HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            + f"Authorization: Bearer {capability}\r\n".encode()
            + b"Content-Length: 1000\r\n"
            b"Content-Type: application/json\r\n"
            b"\r\n"
            b'{"model":"'
        )
        s.sendall(request_data)
        s.shutdown(socket.SHUT_WR)
        response_data = s.recv(4096)
        s.close()
        assert b"400" in response_data.split(b"\r\n")[0]
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_upstream_delayed_beyond_inbound_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upstream response slower than the inbound deadline is not aborted."""

    class DelayedUpstream(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            # Exceed the shortened inbound deadline without a slow wall-clock test.
            time.sleep(1.0)
            resp = json.dumps(
                {
                    "id": "chatcmpl-delayed-001",
                    "object": "chat.completion",
                    "created": 1710000000,
                    "model": "glm-5.3-flash",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

    capability = "valid-cap"
    proxy, upstream, base_url = _setup_proxy(
        tmp_path,
        monkeypatch,
        upstream_handler=DelayedUpstream,
        capability=capability,
        request_timeout=0.5,
    )
    try:
        req = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read()
        assert resp.status == 200
        assert b'"ok"' in body
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_forwards_allowed_flash_and_full_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = "valid-cap"
    proxy, upstream, base_url = _setup_proxy(tmp_path, monkeypatch, capability=capability)
    try:
        for model in (
            "glm-5.3-flash",
            "glm-5.3",
            "zai-coding-plan/glm-5.3-flash",
            "zai-coding-plan/glm-5.3",
        ):
            req = urllib.request.Request(
                f"{base_url}/api/paas/v4/chat/completions",
                data=json.dumps(
                    {"model": model, "messages": [{"role": "user", "content": "hi"}]}
                ).encode(),
                headers={
                    "Authorization": f"Bearer {capability}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read()
            assert resp.status == 200
            assert b'"ok"' in body

        proxy_module = _load_proxy_module()
        assert len(_MockZaiUpstream.seen) == 4
        for path, auth, fwd_body in _MockZaiUpstream.seen:
            assert path == proxy_module.UPSTREAM_PATH
            assert auth == f"Bearer {SECRET_SENTINEL}"
            payload = json.loads(fwd_body.decode("utf-8"))
            assert payload["model"] in ("glm-5.3-flash", "glm-5.3")
            assert payload["stream"] is False
            assert payload["n"] == 1
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_rejects_disallowed_models_and_providers_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = "valid-cap"
    proxy, upstream, base_url = _setup_proxy(tmp_path, monkeypatch, capability=capability)
    disallowed_models = [
        "openai/gpt-5.2",
        "zai/glm-5.3",
        "deepseek/deepseek-v4-flash",
        "glm-unknown",
        "zai-coding-plan/unknown",
        "zai-coding-plan/",
        "",
    ]
    try:
        for model in disallowed_models:
            req = urllib.request.Request(
                f"{base_url}/api/paas/v4/chat/completions",
                data=json.dumps({"model": model, "messages": []}).encode(),
                headers={
                    "Authorization": f"Bearer {capability}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req, timeout=5)
            assert exc.value.code == 403
            assert SECRET_SENTINEL.encode() not in exc.value.read()

        # Upstream must never have received any requests for disallowed models
        assert len(_MockZaiUpstream.seen) == 0
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_forwards_highspeed_verbatim_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Highspeed passes prefix guard, is forwarded verbatim without fallback,

    and provider 429 surfaces as an execution access error, not reward 0.
    """
    capability = "valid-cap"
    proxy, upstream, base_url = _setup_proxy(tmp_path, monkeypatch, capability=capability)
    try:
        req = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=json.dumps(
                {
                    "model": "zai-coding-plan/glm-5.3-highspeed",
                    "messages": [{"role": "user", "content": "hi"}],
                }
            ).encode(),
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        # Upstream 429 is forwarded verbatim
        assert exc.value.code == 429
        err_body = exc.value.read()
        assert b"does not yet include access to GLM-5.3-Highspeed" in err_body
        assert SECRET_SENTINEL.encode() not in err_body

        # Verify proxy did NOT substitute or fall back to flash/full
        proxy_module = _load_proxy_module()
        assert len(_MockZaiUpstream.seen) == 1
        path, auth, fwd_body = _MockZaiUpstream.seen[0]
        assert path == proxy_module.UPSTREAM_PATH
        assert auth == f"Bearer {SECRET_SENTINEL}"
        payload = json.loads(fwd_body.decode("utf-8"))
        assert payload["model"] == "glm-5.3-highspeed"
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_strips_inbound_credential_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = "valid-cap"
    proxy, upstream, base_url = _setup_proxy(tmp_path, monkeypatch, capability=capability)
    try:
        req = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
            headers={
                "Authorization": f"Bearer {capability}",
                "X-Api-Key": "attacker-provided-key",
                "Proxy-Authorization": "Basic dXNlcjpwYXNz",
                "X-Evallab-Proxy-Capability": capability,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200

        assert len(_MockZaiUpstream.seen) == 1
        _path, auth, _body = _MockZaiUpstream.seen[0]
        # Only the real secret injected by proxy reaches upstream
        assert auth == f"Bearer {SECRET_SENTINEL}"
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_redacts_secret_reflection_from_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ReflectingUpstream(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            payload = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": f"reflected {SECRET_SENTINEL}",
                                "escaped": json.dumps(SECRET_SENTINEL),
                                "b64": base64.b64encode(SECRET_SENTINEL.encode()).decode(),
                                "url_enc": urllib.parse.quote(SECRET_SENTINEL),
                                "bearer_hdr": f"Bearer {SECRET_SENTINEL}",
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    capability = "valid-cap"
    proxy, upstream, base_url = _setup_proxy(
        tmp_path, monkeypatch, upstream_handler=ReflectingUpstream, capability=capability
    )
    try:
        req = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read()
        assert resp.status == 200
        assert SECRET_SENTINEL.encode() not in body
        assert base64.b64encode(SECRET_SENTINEL.encode()) not in body
        assert urllib.parse.quote(SECRET_SENTINEL).encode() not in body
        assert b"<redacted>" in body
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_upstream_oversized_response_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upstream response exceeding MAX_RESPONSE_BYTES is rejected with sanitized 502."""

    class HugeResponseUpstream(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            # Send Content-Length larger than 16MB
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(20 * 1024 * 1024))
            self.end_headers()
            self.wfile.write(b" " * 1024)

    capability = "valid-cap"
    proxy, upstream, base_url = _setup_proxy(
        tmp_path, monkeypatch, upstream_handler=HugeResponseUpstream, capability=capability
    )
    try:
        req = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 502
        assert SECRET_SENTINEL.encode() not in exc.value.read()
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_upstream_truncated_valid_json_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upstream delivering fewer bytes than declared Content-Length is rejected (sanitized 502) even if valid JSON."""

    class TruncatedValidJsonUpstream(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            body = b'{"choices":[{"message":{"content":"ok"}}]}'
            # Declare 200 bytes, but send only 43 bytes and close socket
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "200")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

    capability = "valid-cap"
    proxy, upstream, base_url = _setup_proxy(
        tmp_path, monkeypatch, upstream_handler=TruncatedValidJsonUpstream, capability=capability
    )
    try:
        req = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 502
        assert SECRET_SENTINEL.encode() not in exc.value.read()
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_upstream_read_error_sanitized_to_502(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upstream transport disconnect during body read is sanitized to 502."""

    class DroppingUpstream(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b'{"choices":')
            # Abruptly close connection before full body
            self.close_connection = True

    capability = "valid-cap"
    proxy, upstream, base_url = _setup_proxy(
        tmp_path, monkeypatch, upstream_handler=DroppingUpstream, capability=capability
    )
    try:
        req = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 502
        assert SECRET_SENTINEL.encode() not in exc.value.read()
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_worker_pool_503_content_length_and_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker capacity limits connections before spawning threads, returning exact 503 without stalling accept loop."""

    class SlowUpstream(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            time.sleep(1.0)
            resp = json.dumps(
                {
                    "id": "chatcmpl-slow-001",
                    "object": "chat.completion",
                    "created": 1710000000,
                    "model": "glm-5.3-flash",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

    capability = "valid-cap"
    proxy, upstream, base_url = _setup_proxy(
        tmp_path, monkeypatch, upstream_handler=SlowUpstream, capability=capability, max_workers=1
    )
    try:

        def _slow_request() -> int:
            req = urllib.request.Request(
                f"{base_url}/api/paas/v4/chat/completions",
                data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
                headers={
                    "Authorization": f"Bearer {capability}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return int(resp.status)
            except urllib.error.HTTPError as exc:
                return int(exc.code)

        # Start 1st request to occupy the single worker
        t1 = threading.Thread(target=_slow_request)
        t1.start()
        time.sleep(0.1)

        # 2nd request connects without half-closing write side: verify 503 received without blocking server
        host, port = proxy.server_address[:2]
        s2 = socket.create_connection((host, port), timeout=5)
        raw_req = (
            b"POST /api/paas/v4/chat/completions HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            + f"Authorization: Bearer {capability}\r\n".encode()
            + b"Content-Length: 50\r\n"
            b"Content-Type: application/json\r\n"
            b"\r\n"
            b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}'
        )
        s2.sendall(raw_req)
        # Note: Do NOT call s2.shutdown(socket.SHUT_WR); test that server returns 503 and closes
        raw_resp = b""
        s2.settimeout(5)
        while True:
            try:
                chunk = s2.recv(4096)
                if not chunk:
                    break
                raw_resp += chunk
            except OSError:
                break
        s2.close()

        header_bytes, _, body_bytes = raw_resp.partition(b"\r\n\r\n")
        assert b"503 Service Unavailable" in header_bytes
        assert b"Content-Length: 31" in header_bytes
        assert body_bytes == b"proxy worker capacity exceeded\n"
        assert len(body_bytes) == 31

        # Wait for worker 1 client thread to finish
        t1.join()

        # Subsequent connection 3 should be accepted after capacity frees:
        # poll with bounded monotonic deadline tolerating transient 503 or transient
        # connection reset / broken pipe during worker release window
        deadline = time.monotonic() + 5.0
        accepted = False
        while time.monotonic() < deadline:
            req3 = urllib.request.Request(
                f"{base_url}/api/paas/v4/chat/completions",
                data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
                headers={
                    "Authorization": f"Bearer {capability}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req3, timeout=5) as resp3:
                    if resp3.status == 200:
                        accepted = True
                        break
            except urllib.error.HTTPError as exc3:
                if exc3.code == 503:
                    time.sleep(0.02)
                    continue
                raise
            except (urllib.error.URLError, OSError) as exc3:
                # Treat only transient socket disconnects/resets during release window as retryable
                reason = getattr(exc3, "reason", exc3)
                if isinstance(
                    reason, (BrokenPipeError, ConnectionResetError, ConnectionRefusedError)
                ):
                    time.sleep(0.02)
                    continue
                if isinstance(reason, OSError) and reason.errno in (
                    errno.EPIPE,
                    errno.ECONNRESET,
                    errno.ECONNREFUSED,
                    errno.ETIMEDOUT,
                ):
                    time.sleep(0.02)
                    continue
                raise
        assert accepted, "connection 3 was not accepted within deadline after worker finished"
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_rejects_upstream_redirects_and_gzip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RedirectUpstream(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/steal")
            self.send_header("Content-Length", "0")
            self.end_headers()

    class GzipUpstream(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            compressed = gzip.compress(b'{"choices":[]}')
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(compressed)))
            self.end_headers()
            self.wfile.write(compressed)

    capability = "valid-cap"

    # Test redirect refusal (502)
    proxy, upstream, base_url = _setup_proxy(
        tmp_path / "redir", monkeypatch, upstream_handler=RedirectUpstream, capability=capability
    )
    try:
        req = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 502
    finally:
        proxy.shutdown()
        upstream.shutdown()

    # Test gzip refusal (502)
    proxy, upstream, base_url = _setup_proxy(
        tmp_path / "gzip", monkeypatch, upstream_handler=GzipUpstream, capability=capability
    )
    try:
        req = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 502
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_refuses_symlink_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    real = tmp_path / "real_key"
    real.write_text(SECRET_SENTINEL + "\n")
    real.chmod(0o400)
    link = tmp_path / "symlink_key"
    link.symlink_to(real)

    capability = "valid-cap"
    proxy, upstream, base_url = _setup_proxy(
        tmp_path, monkeypatch, secret_path=link, capability=capability
    )

    try:
        req = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 500
        assert SECRET_SENTINEL.encode() not in exc.value.read()
        assert len(_MockZaiUpstream.seen) == 0
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_pinned_upstream_url_enforces_whitelist(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy_module = _load_proxy_module()
    monkeypatch.setenv("EVALLAB_ZAI_UPSTREAM", "https://api.z.ai")
    assert (
        proxy_module._pinned_upstream_url() == f"https://api.z.ai:443{proxy_module.UPSTREAM_PATH}"
    )
    monkeypatch.setenv("EVALLAB_ZAI_UPSTREAM", "http://evallab-smoke-upstream:8099")
    assert (
        proxy_module._pinned_upstream_url()
        == f"http://evallab-smoke-upstream:8099{proxy_module.UPSTREAM_PATH}"
    )
    monkeypatch.setenv("EVALLAB_ZAI_UPSTREAM", "http://127.0.0.1:9000")
    assert (
        proxy_module._pinned_upstream_url() == f"http://127.0.0.1:9000{proxy_module.UPSTREAM_PATH}"
    )
    monkeypatch.setenv("EVALLAB_ZAI_UPSTREAM", "https://evil.com")
    with pytest.raises(RuntimeError, match="upstream host is not pinned"):
        proxy_module._pinned_upstream_url()
    monkeypatch.setenv("EVALLAB_ZAI_UPSTREAM", "http://untrusted-remote.com:8080")
    with pytest.raises(RuntimeError, match="http upstream is not pinned"):
        proxy_module._pinned_upstream_url()


# ==========================================================================
# 2. Adapter Tests (SecretSafeZaiOpenCodeAgent)
# ==========================================================================


def test_adapter_rewrites_connection_to_internal_proxy(
    zai_adapter_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZAI_CODING_PLAN_API_KEY", SECRET_SENTINEL)
    module = zai_adapter_module
    agent = module.SecretSafeZaiOpenCodeAgent(
        model_name="zai-coding-plan/glm-5.3-flash",
    )
    conn = agent.model_connection
    assert conn.api_key == module.ZAI_PROXY_TOKEN
    assert conn.base_url == module.ZAI_PROXY_URL
    assert conn.configured_base_url == module.ZAI_PROXY_URL
    assert conn.env["ZAI_CODING_PLAN_API_KEY"] == module.ZAI_PROXY_TOKEN
    assert conn.env["ZAI_API_KEY"] == module.ZAI_PROXY_TOKEN
    assert conn.env["ZAI_BASE_URL"] == module.ZAI_PROXY_URL
    assert conn.env["OPENAI_BASE_URL"] == module.ZAI_PROXY_URL
    assert conn.env["SAFE_KEY"] == "ok"
    assert SECRET_SENTINEL not in conn.env.values()

    # Verify provider baseURL is configured in OpenCode config
    opencode_cfg = agent.opencode_config
    assert opencode_cfg["provider"]["zai-coding-plan"]["options"]["baseURL"] == module.ZAI_PROXY_URL


def test_adapter_rejects_non_zai_models(zai_adapter_module: ModuleType) -> None:
    module = zai_adapter_module
    # Missing provider prefix
    with pytest.raises(ValueError, match="requires a provider/model selector"):
        module.SecretSafeZaiOpenCodeAgent(model_name="glm-5.3-flash")
    with pytest.raises(ValueError, match="requires a provider/model selector"):
        module.SecretSafeZaiOpenCodeAgent(model_name="")

    # Wrong provider prefix
    for bad_model in ("openai/gpt-5.2", "anthropic/claude-3-5-sonnet", "zai/glm-5.3"):
        with pytest.raises(ValueError, match="only accepts models under 'zai-coding-plan/'"):
            module.SecretSafeZaiOpenCodeAgent(model_name=bad_model)

    # Empty model under prefix
    with pytest.raises(ValueError, match="requires a non-empty model"):
        module.SecretSafeZaiOpenCodeAgent(model_name="zai-coding-plan/")


def test_adapter_refuses_provider_key_in_exec_environment(
    zai_adapter_module: ModuleType,
) -> None:
    module = zai_adapter_module
    agent = module.SecretSafeZaiOpenCodeAgent(
        model_name="zai-coding-plan/glm-5.3-flash",
    )
    with pytest.raises(ValueError, match="cannot enter the task exec environment"):
        asyncio.run(
            agent.exec_as_agent(
                object(),
                "echo hello",
                env={"ZAI_CODING_PLAN_API_KEY": SECRET_SENTINEL},
            )
        )


def test_adapter_refuses_secret_in_exec_command(
    zai_adapter_module: ModuleType,
) -> None:
    module = zai_adapter_module
    agent = module.SecretSafeZaiOpenCodeAgent(
        model_name="zai-coding-plan/glm-5.3-flash",
    )
    with pytest.raises(ValueError, match="cannot enter the task exec command"):
        asyncio.run(
            agent.exec_as_agent(
                object(),
                'export ZAI_CODING_PLAN_API_KEY="$(cat /run/secrets/key)"',
            )
        )


def test_collected_zai_secret_values_checks_both_file_and_path_envs(
    zai_adapter_module: ModuleType, tmp_path: Path
) -> None:
    module = zai_adapter_module
    file_secret = tmp_path / "host_secret"
    file_secret.write_text("host-file-secret-value\n")
    path_secret = tmp_path / "container_secret"
    path_secret.write_text("container-path-secret-value\n")

    env = {
        module.ZAI_SECRET_FILE_ENV: str(file_secret),
        module.ZAI_SECRET_PATH_ENV: str(path_secret),
        "ZAI_CODING_PLAN_API_KEY": "env-secret-value",
    }
    collected = module.collected_zai_secret_values(env)
    assert "host-file-secret-value" in collected
    assert "container-path-secret-value" in collected
    assert "env-secret-value" in collected


def test_adapter_sanitizes_trajectories(
    zai_adapter_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZAI_CODING_PLAN_API_KEY", SECRET_SENTINEL)
    module = zai_adapter_module
    agent = module.SecretSafeZaiOpenCodeAgent(
        model_name="zai-coding-plan/glm-5.3-flash",
    )
    agent.logs_dir = tmp_path
    traj = tmp_path / "trajectory.json"
    traj.write_text(
        json.dumps(
            {
                "authorization": f"Bearer {SECRET_SENTINEL}",
                "apiKey": SECRET_SENTINEL,
                "steps": [{"content": SECRET_SENTINEL}],
            }
        )
    )
    agent.populate_context_post_run(object())
    sanitized = json.loads(traj.read_text())
    assert SECRET_SENTINEL not in traj.read_text()
    assert sanitized["authorization"] == "<redacted>"
    assert sanitized["apiKey"] == "<redacted>"
    assert sanitized["steps"][0]["content"] == "<redacted>"


# ==========================================================================
# 3. Live Local Fake-Upstream SSE Regressions
# ==========================================================================


class _FakeSSEUpstream(BaseHTTPRequestHandler):
    mode: str = "normal"
    seen: list[tuple[str, str, bytes]] = []

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        auth = self.headers.get("Authorization", "")
        type(self).seen.append((self.path, auth, body))

        if type(self).mode == "requires_stream":
            payload = json.loads(body)
            if payload.get("stream") is not True or payload.get("stream_options") != {
                "include_usage": True
            }:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"streaming with usage is required"}')
                return

        if type(self).mode in {"normal", "requires_stream"}:
            events = (
                b'data: {"id":"chatcmpl-sse-1","object":"chat.completion.chunk","created":1710000000,'
                b'"model":"glm-5.3-flash","choices":[{"index":0,"delta":{"role":"assistant","content":"hello"}}]}\n\n'
                b'data: {"id":"chatcmpl-sse-1","object":"chat.completion.chunk","created":1710000000,'
                b'"model":"glm-5.3-flash","choices":[{"index":0,"delta":{"content":" world"}}],'
                b'"usage":{"prompt_tokens":8,"completion_tokens":4,"total_tokens":12}}\n\n'
                b"data: [DONE]\n\n"
            )
        elif type(self).mode == "missing_model":
            events = (
                b'data: {"id":"chatcmpl-sse-2","object":"chat.completion.chunk","created":1710000000,'
                b'"choices":[{"index":0,"delta":{"role":"assistant","content":"hello"}}]}\n\n'
                b'data: {"id":"chatcmpl-sse-2","object":"chat.completion.chunk","created":1710000000,'
                b'"choices":[{"index":0,"delta":{"content":" world"}}],'
                b'"usage":{"prompt_tokens":8,"completion_tokens":4,"total_tokens":12}}\n\n'
                b"data: [DONE]\n\n"
            )
        elif type(self).mode == "conflicting_model":
            events = (
                b'data: {"id":"chatcmpl-sse-3","object":"chat.completion.chunk","created":1710000000,'
                b'"model":"glm-5.3-flash","choices":[{"index":0,"delta":{"role":"assistant","content":"hello"}}]}\n\n'
                b'data: {"id":"chatcmpl-sse-3","object":"chat.completion.chunk","created":1710000000,'
                b'"model":"glm-5.3","choices":[{"index":0,"delta":{"content":" world"}}],'
                b'"usage":{"prompt_tokens":8,"completion_tokens":4,"total_tokens":12}}\n\n'
                b"data: [DONE]\n\n"
            )
        elif type(self).mode == "reflect_secret":
            events = (
                b'data: {"id":"chatcmpl-sse-4","object":"chat.completion.chunk","created":1710000000,'
                b'"model":"glm-5.3-flash","choices":[{"index":0,"delta":{"content":"reflected '
                + SECRET_SENTINEL.encode()
                + b'"}}],'
                b'"usage":{"prompt_tokens":8,"completion_tokens":4,"total_tokens":12}}\n\n'
                b"data: [DONE]\n\n"
            )
        else:
            events = b"data: [DONE]\n\n"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Content-Length", str(len(events)))
        self.end_headers()
        self.wfile.write(events)


def test_proxy_accepts_opencode_native_streaming_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenCode sends a bare model ID and expects SSE with reconciled usage."""
    _FakeSSEUpstream.mode = "requires_stream"
    _FakeSSEUpstream.seen = []
    usage_file = tmp_path / "zai-proxy-usage.json"
    proxy, upstream, base_url = _setup_proxy(
        tmp_path,
        monkeypatch,
        upstream_handler=_FakeSSEUpstream,
        capability="valid-cap",
        usage_file=usage_file,
    )
    try:
        request = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=b'{"model":"glm-5.3-flash","messages":[],"stream":true}',
            headers={"Authorization": "Bearer valid-cap", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.headers.get_content_type() == "text/event-stream"
            assert b"data: [DONE]" in response.read()
        call = json.loads(usage_file.read_text())["calls"][0]
        assert call["state"] == "reconciled"
        assert call["returned_model"] == "glm-5.3-flash"
        assert (call["input_tokens"], call["output_tokens"]) == (8, 4)
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_sse_provider_identity_and_missing_stays_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = "valid-cap"
    usage_file = tmp_path / "zai-proxy-usage.json"
    _FakeSSEUpstream.mode = "normal"
    _FakeSSEUpstream.seen = []
    proxy, upstream, base_url = _setup_proxy(
        tmp_path,
        monkeypatch,
        upstream_handler=_FakeSSEUpstream,
        capability=capability,
        usage_file=usage_file,
    )
    try:
        # 1. Normal SSE: response identity comes from provider ("glm-5.3-flash")
        req = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read()
            assert resp.status == 200
            assert "text/event-stream" in (resp.headers.get("Content-Type") or "")
            assert b"hello" in body and b"world" in body

        usage_data = json.loads(usage_file.read_text(encoding="utf-8"))
        assert len(usage_data["calls"]) == 1
        call1 = usage_data["calls"][0]
        assert call1["state"] == "reconciled"
        assert call1["returned_model"] == "glm-5.3-flash"
        assert call1["returned_model_reason"] is None
        assert call1["input_tokens"] == 8
        assert call1["output_tokens"] == 4

        # 2. Missing identity in SSE: missing stays unknown (None / "model_absent_or_null")
        _FakeSSEUpstream.mode = "missing_model"
        req2 = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req2, timeout=5) as resp2:
            assert resp2.status == 200

        usage_data2 = json.loads(usage_file.read_text(encoding="utf-8"))
        assert len(usage_data2["calls"]) == 2
        call2 = usage_data2["calls"][1]
        assert call2["state"] == "reconciled"
        assert call2["returned_model"] is None
        assert call2["returned_model_reason"] == "model_absent_or_null"
        assert call2["input_tokens"] == 8
        assert call2["output_tokens"] == 4
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_sse_conflicting_identity_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = "valid-cap"
    usage_file = tmp_path / "zai-proxy-usage.json"
    _FakeSSEUpstream.mode = "conflicting_model"
    _FakeSSEUpstream.seen = []
    proxy, upstream, base_url = _setup_proxy(
        tmp_path,
        monkeypatch,
        upstream_handler=_FakeSSEUpstream,
        capability=capability,
        usage_file=usage_file,
    )
    try:
        req = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 502
        assert exc.value.read() == b"unsupported upstream body\n"

        usage_data = json.loads(usage_file.read_text(encoding="utf-8"))
        call = usage_data["calls"][0]
        assert call["state"] == "unresolved"
        assert call["reason"] == "unreconciled_upstream_usage"
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_sse_limits_halt_further_upstream_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = "valid-cap"
    _FakeSSEUpstream.mode = "normal"
    _FakeSSEUpstream.seen = []
    proxy, upstream, base_url = _setup_proxy(
        tmp_path,
        monkeypatch,
        upstream_handler=_FakeSSEUpstream,
        capability=capability,
        max_requests=1,
    )
    try:
        req1 = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req1, timeout=5) as resp:
            assert resp.status == 200
        assert len(_FakeSSEUpstream.seen) == 1

        req2 = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req2, timeout=5)
        assert exc.value.code == 429
        assert exc.value.read() == b"trial budget exhausted\n"
        assert len(_FakeSSEUpstream.seen) == 1
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_sse_reflected_credentials_stay_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = "valid-cap"
    _FakeSSEUpstream.mode = "reflect_secret"
    _FakeSSEUpstream.seen = []
    proxy, upstream, base_url = _setup_proxy(
        tmp_path, monkeypatch, upstream_handler=_FakeSSEUpstream, capability=capability
    )
    try:
        req = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read()
            assert resp.status == 200
            assert SECRET_SENTINEL.encode() not in body
            assert b"<redacted>" in body
    finally:
        proxy.shutdown()
        upstream.shutdown()


@pytest.mark.parametrize(
    ("headers", "status", "reason", "fact", "category"),
    [
        (
            {"Content-Encoding": "gzip"},
            200,
            "unsupported_content_encoding",
            "content_encoding",
            "gzip",
        ),
        (
            {"Transfer-Encoding": "compress"},
            200,
            "unsupported_transfer_encoding",
            "transfer_encoding",
            "other",
        ),
        (
            {"Content-Type": "application/json; charset=iso-8859-1"},
            200,
            "unsupported_charset",
            "charset",
            "iso-8859-1",
        ),
        (
            {"Content-Type": f"text/html; charset={SECRET_SENTINEL}"},
            503,
            "unsupported_content_type",
            "media_type",
            "text/html",
        ),
    ],
)
def test_rejected_response_retains_safe_diagnosis_without_refunding(
    tmp_path, monkeypatch, headers, status, reason, fact, category
):
    requests = []

    class DiagnosticUpstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):  # noqa: N802
            requests.append(self.rfile.read(int(self.headers["Content-Length"])))
            body = SECRET_SENTINEL.encode()
            self.send_response(status)
            for name, value in {"Content-Type": "application/json", **headers}.items():
                self.send_header(name, value)
            self.send_header("X-Private", SECRET_SENTINEL)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    usage_file = tmp_path / "usage.json"
    proxy, upstream, base = _setup_proxy(
        tmp_path / "proxy",
        monkeypatch,
        upstream_handler=DiagnosticUpstream,
        capability="valid-cap",
        usage_file=usage_file,
        max_requests=1,
        max_output_tokens=1000,
    )
    try:
        request = urllib.request.Request(
            f"{base}/api/paas/v4/chat/completions",
            data=b'{"model":"glm-5.3-flash","messages":[]}',
            headers={"Authorization": "Bearer valid-cap", "Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as failure:
            urllib.request.urlopen(request, timeout=5)
        assert failure.value.code == 502
        assert SECRET_SENTINEL.encode() not in failure.value.read()
        retained = usage_file.read_text()
        assert SECRET_SENTINEL not in retained
        usage = json.loads(retained)
        call = usage["calls"][0]
        assert call["state"] == "unresolved"
        assert call["reason"] == reason
        assert call["status"] == status
        assert call["response_facts"][fact] == category
        assert usage["totals"]["output_tokens"] == 1000
        assert usage["unresolved_requests"] == 1
        with pytest.raises(urllib.error.HTTPError) as retry:
            urllib.request.urlopen(request, timeout=5)
        assert retry.value.code == 429
        assert len(requests) == 1
        assert usage_file.read_text() == retained
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_can_bind_only_loopback_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("EVALLAB_ZAI_PROXY_BIND_HOST", "127.0.0.1")
    proxy, upstream, _ = _setup_proxy(tmp_path, monkeypatch)
    try:
        module = _load_proxy_module()
        isolated = module.serve(port=0)
        try:
            assert isolated.server_address[0] == "127.0.0.1"
        finally:
            isolated.server_close()
        monkeypatch.setenv("EVALLAB_ZAI_PROXY_BIND_HOST", "192.0.2.1")
        with pytest.raises(ValueError):
            module.serve(port=0)
    finally:
        proxy.shutdown()
        upstream.shutdown()
