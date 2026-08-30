"""Focused security and behavior tests for Z.ai credential proxy and adapter.

Covers:
1. Proxy in-process HTTP server:
   - Capability verification and expiry (fail-closed 401).
   - Pre-body capability authentication (unauthenticated requests rejected before body read).
   - Model allowlist enforcement: allows ``zai-coding-plan/glm-5.3`` and
     ``zai-coding-plan/glm-5.3-flash``; rejects disallowed providers/models (403).
   - Highspeed handling: forwards ``zai-coding-plan/glm-5.3-highspeed`` verbatim
     without fallback/substitution; upstream 429 access failure is forwarded
     without fallback so it surfaces as non-scored execution failure.
   - Credential isolation: strips inbound headers, injects provider auth only in
     proxy, redacts secret from responses (raw, JSON, Base64, URL-encoded, Unicode, Bearer).
   - Transport security: rejects redirects, gzip, binary, non-JSON upstream (502).
   - Upstream size limits & error sanitization: oversized responses (limit+1) and
     stream disconnects return sanitized 502.
   - Concurrency bounding: worker capacity limits concurrent requests.
   - Secret file validation: rejects symlinks, wrong mode/owner (500).
   - Pinned upstream URL enforcement.
2. Adapter integration (``SecretSafeZaiOpenCodeAgent``):
   - Rewrites model_connection to internal proxy with placeholder token.
   - Scrubs real secrets from environment and command execution.
   - Sanitizes trajectory files.
3. Compose asset validation (``zai-secret.compose.yaml``).
"""

from __future__ import annotations

import asyncio
import base64
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
        resp = (
            b'{"choices":[{"message":{"content":"ok"}}],'
            b'"usage":{"prompt_tokens":10,"completion_tokens":5}}'
        )
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
) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    secret_file = tmp_path / "zai_key"
    secret_file.write_text(SECRET_SENTINEL + "\n")
    secret_file.chmod(0o600)

    _MockZaiUpstream.seen = []
    upstream, upstream_url = _serve(upstream_handler)

    monkeypatch.setenv("EVALLAB_ZAI_SECRET_PATH", str(secret_file))
    monkeypatch.setenv("EVALLAB_ZAI_UPSTREAM", upstream_url)
    monkeypatch.setenv("EVALLAB_ZAI_PROXY_CAPABILITY", capability)
    monkeypatch.setenv("EVALLAB_ZAI_CAPABILITY_EXPIRES_AT", str(time.time() + 300))

    proxy_module = _load_proxy_module()
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
            headers={"Authorization": "Bearer wrong-capability", "Content-Type": "application/json"},
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
        # Connect raw socket and send headers with invalid auth and huge Content-Length
        # without sending the body
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


def test_proxy_incomplete_body_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_proxy_forwards_allowed_flash_and_full_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = "valid-cap"
    proxy, upstream, base_url = _setup_proxy(tmp_path, monkeypatch, capability=capability)
    try:
        for model in ("zai-coding-plan/glm-5.3-flash", "zai-coding-plan/glm-5.3"):
            req = urllib.request.Request(
                f"{base_url}/api/paas/v4/chat/completions",
                data=json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}]}).encode(),
                headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read()
            assert resp.status == 200
            assert b'"ok"' in body

        assert len(_MockZaiUpstream.seen) == 2
        for path, auth, fwd_body in _MockZaiUpstream.seen:
            assert path == "/api/paas/v4/chat/completions"
            assert auth == f"Bearer {SECRET_SENTINEL}"
            payload = json.loads(fwd_body.decode("utf-8"))
            assert payload["model"] in ("zai-coding-plan/glm-5.3-flash", "zai-coding-plan/glm-5.3")
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
        "glm-5.3-flash",
        "zai-coding-plan/",
        "",
    ]
    try:
        for model in disallowed_models:
            req = urllib.request.Request(
                f"{base_url}/api/paas/v4/chat/completions",
                data=json.dumps({"model": model, "messages": []}).encode(),
                headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
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
            data=json.dumps({
                "model": "zai-coding-plan/glm-5.3-highspeed",
                "messages": [{"role": "user", "content": "hi"}],
            }).encode(),
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
        assert len(_MockZaiUpstream.seen) == 1
        path, auth, fwd_body = _MockZaiUpstream.seen[0]
        assert auth == f"Bearer {SECRET_SENTINEL}"
        payload = json.loads(fwd_body.decode("utf-8"))
        assert payload["model"] == "zai-coding-plan/glm-5.3-highspeed"
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
            payload = json.dumps({
                "choices": [{
                    "message": {
                        "content": f"reflected {SECRET_SENTINEL}",
                        "escaped": json.dumps(SECRET_SENTINEL),
                        "b64": base64.b64encode(SECRET_SENTINEL.encode()).decode(),
                        "url_enc": urllib.parse.quote(SECRET_SENTINEL),
                        "bearer_hdr": f"Bearer {SECRET_SENTINEL}",
                    }
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }).encode("utf-8")
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
    _MockZaiUpstream.seen = []
    upstream, upstream_url = _serve(_MockZaiUpstream)

    monkeypatch.setenv("EVALLAB_ZAI_SECRET_PATH", str(link))
    monkeypatch.setenv("EVALLAB_ZAI_UPSTREAM", upstream_url)
    monkeypatch.setenv("EVALLAB_ZAI_PROXY_CAPABILITY", capability)

    proxy_module = _load_proxy_module()
    proxy = proxy_module.serve(host="127.0.0.1", port=0)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{proxy.server_address[1]}"

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
    assert proxy_module._pinned_upstream_url() == "https://api.z.ai:443/api/paas/v4/chat/completions"
    monkeypatch.setenv("EVALLAB_ZAI_UPSTREAM", "http://evallab-smoke-upstream:8099")
    assert proxy_module._pinned_upstream_url() == "http://evallab-smoke-upstream:8099/api/paas/v4/chat/completions"
    monkeypatch.setenv("EVALLAB_ZAI_UPSTREAM", "http://127.0.0.1:9000")
    assert proxy_module._pinned_upstream_url() == "http://127.0.0.1:9000/api/paas/v4/chat/completions"
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
        json.dumps({
            "authorization": f"Bearer {SECRET_SENTINEL}",
            "apiKey": SECRET_SENTINEL,
            "steps": [{"content": SECRET_SENTINEL}],
        })
    )
    agent.populate_context_post_run(object())
    sanitized = json.loads(traj.read_text())
    assert SECRET_SENTINEL not in traj.read_text()
    assert sanitized["authorization"] == "<redacted>"
    assert sanitized["apiKey"] == "<redacted>"
    assert sanitized["steps"][0]["content"] == "<redacted>"


# ==========================================================================
# 3. Compose Asset Structure Tests
# ==========================================================================


def test_zai_compose_asset_shape() -> None:
    path = Path(__file__).resolve().parents[1] / "containers" / "zai-secret.compose.yaml"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "zai-secret-proxy:" in text
    assert "- workbench-internal" in text
    assert "- default" in text
    assert "internal: true" in text
    assert 'user: "${EVALLAB_PROXY_UID:?}:${EVALLAB_PROXY_GID:?}"' in text
    assert "read_only: true" in text
    assert "evallab-proxy-placeholder" in text
    assert "http://zai-secret-proxy:8080" in text
    # Compose overlay mounts secret via volume, not top-level secrets block
    assert "secrets:\n" not in text
