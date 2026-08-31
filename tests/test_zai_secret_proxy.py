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

    async def run(self, instruction: Any, environment: Any, context: Any) -> None:
        del instruction, environment, context
        self.exec_calls.append(("super.run", None))

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

        if payload.get("stream") is True:
            resp = (
                b'data: {"choices":[{"delta":{"content":"ok"}}],"usage":null}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":10,'
                b'"completion_tokens":5,"total_tokens":15}}\n\n'
                b"data: [DONE]\n\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
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


def _set_budget_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str] | None = None,
) -> None:
    values = {
        "EVALLAB_ZAI_ATTEMPT_ID": "test-attempt",
        "EVALLAB_ZAI_USAGE_FILE": str(tmp_path / "zai-proxy-usage.json"),
        "EVALLAB_ZAI_MAX_REQUESTS": "16",
        "EVALLAB_ZAI_MAX_INPUT_TOKENS": "1000000",
        "EVALLAB_ZAI_MAX_OUTPUT_TOKENS": "1000",
        "EVALLAB_ZAI_MAX_TOTAL_TOKENS": "1001000",
        "EVALLAB_ZAI_MAX_COST_MICROS": "1000000",
        "EVALLAB_ZAI_INPUT_COST_MICROS_PER_MILLION": "1000000",
        "EVALLAB_ZAI_OUTPUT_COST_MICROS_PER_MILLION": "1000000",
    }
    values.update(overrides or {})
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _setup_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream_handler: type[BaseHTTPRequestHandler] = _MockZaiUpstream,
    capability: str = "test-zai-capability-token-32b",
    max_workers: int = 32,
    budget_overrides: dict[str, str] | None = None,
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
    _set_budget_environment(tmp_path, monkeypatch, budget_overrides)

    proxy_module = _load_proxy_module()
    proxy = proxy_module.serve(host="127.0.0.1", port=0, max_workers=max_workers)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{proxy.server_address[1]}"
    return proxy, upstream, base_url


def _post_json(
    base_url: str,
    capability: str,
    payload: dict[str, Any],
) -> tuple[int, bytes]:
    request = urllib.request.Request(
        f"{base_url}/api/paas/v4/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {capability}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _default_payload(*, max_tokens: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": "zai-coding-plan/glm-5.3",
        "messages": [{"role": "user", "content": "hello"}],
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return payload


@pytest.mark.parametrize(
    ("overrides", "payload", "expected"),
    [
        (
            {"EVALLAB_ZAI_MAX_INPUT_TOKENS": "1"},
            _default_payload(max_tokens=1),
            b"input_tokens ceiling exceeded\n",
        ),
        (
            {"EVALLAB_ZAI_MAX_OUTPUT_TOKENS": "1"},
            _default_payload(max_tokens=2),
            b"output_tokens ceiling exceeded\n",
        ),
        (
            {"EVALLAB_ZAI_MAX_TOTAL_TOKENS": "1"},
            _default_payload(max_tokens=1),
            b"total_tokens ceiling exceeded\n",
        ),
        (
            {"EVALLAB_ZAI_MAX_COST_MICROS": "1"},
            _default_payload(max_tokens=1),
            b"cost ceiling exceeded\n",
        ),
    ],
    ids=["input", "output", "total", "cost"],
)
def test_proxy_refuses_each_provider_budget_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
    payload: dict[str, Any],
    expected: bytes,
) -> None:
    capability = "metered-capability"
    proxy, upstream, base_url = _setup_proxy(
        tmp_path,
        monkeypatch,
        capability=capability,
        budget_overrides=overrides,
    )
    try:
        status, body = _post_json(base_url, capability, payload)
        assert status == 429
        assert body == expected
        assert _MockZaiUpstream.seen == []
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_refuses_request_count_after_reconciled_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = "request-count-capability"
    proxy, upstream, base_url = _setup_proxy(
        tmp_path,
        monkeypatch,
        capability=capability,
        budget_overrides={"EVALLAB_ZAI_MAX_REQUESTS": "1"},
    )
    try:
        first_status, _ = _post_json(
            base_url,
            capability,
            _default_payload(max_tokens=10),
        )
        second_status, second_body = _post_json(
            base_url,
            capability,
            _default_payload(max_tokens=10),
        )
        assert first_status == 200
        assert second_status == 429
        assert second_body == b"request_count ceiling exceeded\n"
        assert len(_MockZaiUpstream.seen) == 1
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_fails_closed_when_provider_usage_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingUsageUpstream(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            body = b'{"choices":[{"message":{"content":"ok"}}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    capability = "missing-usage-capability"
    proxy, upstream, base_url = _setup_proxy(
        tmp_path,
        monkeypatch,
        upstream_handler=MissingUsageUpstream,
        capability=capability,
    )
    try:
        status, body = _post_json(
            base_url,
            capability,
            _default_payload(max_tokens=10),
        )
        assert status == 502
        assert body == b"provider usage unavailable\n"
        usage = json.loads((tmp_path / "zai-proxy-usage.json").read_text())
        assert usage["unresolved_requests"] == 1
        assert usage["calls"][0]["state"] == "unresolved"
        assert usage["calls"][0]["reason"] == "unreconciled_upstream_usage"
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_rejects_observed_usage_above_reserved_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExcessUsageUpstream(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            body = (
                b'{"choices":[{"message":{"content":"ok"}}],'
                b'"usage":{"prompt_tokens":1001,"completion_tokens":1,'
                b'"total_tokens":1002}}'
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    capability = "excess-usage-capability"
    proxy, upstream, base_url = _setup_proxy(
        tmp_path,
        monkeypatch,
        upstream_handler=ExcessUsageUpstream,
        capability=capability,
        budget_overrides={
            "EVALLAB_ZAI_MAX_INPUT_TOKENS": "1000",
            "EVALLAB_ZAI_MAX_TOTAL_TOKENS": "2000",
        },
    )
    try:
        status, body = _post_json(
            base_url,
            capability,
            _default_payload(max_tokens=10),
        )
        assert status == 429
        assert body == b"input_tokens ceiling exceeded\n"
        usage = json.loads((tmp_path / "zai-proxy-usage.json").read_text())
        assert usage["unresolved_requests"] == 1
        assert usage["calls"][0]["state"] == "exceeded"
        assert usage["calls"][0]["input_tokens"] == 1001
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_refuses_startup_when_accounting_configuration_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVALLAB_ZAI_PROXY_CAPABILITY", "startup-capability")
    monkeypatch.setenv(
        "EVALLAB_ZAI_CAPABILITY_EXPIRES_AT",
        str(time.time() + 300),
    )
    _set_budget_environment(tmp_path, monkeypatch)
    monkeypatch.delenv("EVALLAB_ZAI_MAX_COST_MICROS")
    proxy_module = _load_proxy_module()

    with pytest.raises(ValueError, match="EVALLAB_ZAI_MAX_COST_MICROS"):
        proxy_module.serve(host="127.0.0.1", port=0)


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
    """Legitimate upstream response taking >15s is not aborted by the inbound deadline timer."""

    class DelayedUpstream(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            # Sleep 16s (longer than 15s inbound timer, shorter than 120s upstream timeout)
            time.sleep(16.0)
            resp = b'{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":1,"completion_tokens":1}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

    capability = "valid-cap"
    proxy, upstream, base_url = _setup_proxy(
        tmp_path, monkeypatch, upstream_handler=DelayedUpstream, capability=capability
    )
    try:
        req = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=b'{"model":"zai-coding-plan/glm-5.3-flash","messages":[]}',
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        # Timeout 25s for the 16s wait
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read()
        assert resp.status == 200
        assert b'"ok"' in body
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_forwards_allowed_bare_and_prefixed_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = "valid-cap"
    proxy, upstream, base_url = _setup_proxy(tmp_path, monkeypatch, capability=capability)
    models = (
        "glm-5.3-flash",
        "glm-5.3",
        "zai-coding-plan/glm-5.3-flash",
        "zai-coding-plan/glm-5.3",
    )
    try:
        for model in models:
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

        assert len(_MockZaiUpstream.seen) == len(models)
        forwarded_models: list[str] = []
        for path, auth, fwd_body in _MockZaiUpstream.seen:
            assert path == "/api/coding/paas/v4/chat/completions"
            assert auth == f"Bearer {SECRET_SENTINEL}"
            payload = json.loads(fwd_body.decode("utf-8"))
            forwarded_models.append(payload["model"])
            assert payload["model"] in ("glm-5.3-flash", "glm-5.3")
            assert payload["stream"] is False
            assert payload["n"] == 1
        assert forwarded_models.count("glm-5.3-flash") == 2
        assert forwarded_models.count("glm-5.3") == 2
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_preserves_streaming_and_reconciles_final_sse_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = "valid-cap"
    proxy, upstream, base_url = _setup_proxy(tmp_path, monkeypatch, capability=capability)
    try:
        req = urllib.request.Request(
            f"{base_url}/api/paas/v4/chat/completions",
            data=json.dumps(
                {
                    "model": "glm-5.3",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                }
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
        assert b'data: {"choices":[{"delta":{"content":"ok"}}],"usage":null}' in body
        assert body.endswith(b"data: [DONE]\n\n")
        forwarded = json.loads(_MockZaiUpstream.seen[0][2].decode("utf-8"))
        assert forwarded["stream"] is True
        assert forwarded["stream_options"] == {"include_usage": True}
        usage = json.loads((tmp_path / "zai-proxy-usage.json").read_text())
        assert usage["unresolved_requests"] == 0
        assert usage["totals"]["input_tokens"] == 10
        assert usage["totals"]["output_tokens"] == 5
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


def test_proxy_normalizes_highspeed_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Highspeed passes prefix guard without fallback.

    A provider error without usage accounting fails closed rather than escaping
    the proxy as an unmetered response.
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
        assert exc.value.code == 502
        err_body = exc.value.read()
        assert err_body == b"provider usage unavailable\n"
        assert SECRET_SENTINEL.encode() not in err_body
        usage = json.loads((tmp_path / "zai-proxy-usage.json").read_text())
        assert usage["unresolved_requests"] == 1

        # Verify proxy did NOT substitute or fall back to flash/full
        assert len(_MockZaiUpstream.seen) == 1
        path, auth, fwd_body = _MockZaiUpstream.seen[0]
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
            resp = b'{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":1,"completion_tokens":1}}'
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
    _MockZaiUpstream.seen = []
    upstream, upstream_url = _serve(_MockZaiUpstream)

    monkeypatch.setenv("EVALLAB_ZAI_SECRET_PATH", str(link))
    monkeypatch.setenv("EVALLAB_ZAI_UPSTREAM", upstream_url)
    monkeypatch.setenv("EVALLAB_ZAI_PROXY_CAPABILITY", capability)
    monkeypatch.setenv("EVALLAB_ZAI_CAPABILITY_EXPIRES_AT", str(time.time() + 300))
    _set_budget_environment(tmp_path, monkeypatch)

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
    assert (
        proxy_module._pinned_upstream_url()
        == "https://api.z.ai:443/api/coding/paas/v4/chat/completions"
    )
    monkeypatch.setenv("EVALLAB_ZAI_UPSTREAM", "http://evallab-smoke-upstream:8099")
    assert (
        proxy_module._pinned_upstream_url()
        == "http://evallab-smoke-upstream:8099/api/coding/paas/v4/chat/completions"
    )
    monkeypatch.setenv("EVALLAB_ZAI_UPSTREAM", "http://127.0.0.1:9000")
    assert (
        proxy_module._pinned_upstream_url()
        == "http://127.0.0.1:9000/api/coding/paas/v4/chat/completions"
    )
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


def test_adapter_materializes_and_removes_proxy_capability_auth(
    zai_adapter_module: ModuleType,
) -> None:
    module = zai_adapter_module
    agent = module.SecretSafeZaiOpenCodeAgent(
        model_name="zai-coding-plan/glm-5.3-flash",
    )

    asyncio.run(agent.run("instruction", object(), object()))

    assert [call[0] for call in agent.exec_calls] == [
        module.CREATE_PROXY_AUTH_COMMAND,
        "super.run",
        module.REMOVE_PROXY_AUTH_COMMAND,
    ]
    create_env = agent.exec_calls[0][1]
    assert create_env is not None
    assert create_env["ZAI_CODING_PLAN_API_KEY"] == module.ZAI_PROXY_TOKEN
    assert SECRET_SENTINEL not in create_env.values()


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
    assert "ZAI_API_KEY: ${EVALLAB_ZAI_PROXY_CAPABILITY:?proxy capability required}" in text
    assert "http://zai-secret-proxy:8080" in text
    assert "EVALLAB_ZAI_MAX_REQUESTS" in text
    assert "EVALLAB_ZAI_MAX_INPUT_TOKENS" in text
    assert "EVALLAB_ZAI_MAX_OUTPUT_TOKENS" in text
    assert "EVALLAB_ZAI_MAX_TOTAL_TOKENS" in text
    assert "EVALLAB_ZAI_MAX_COST_MICROS" in text
    assert "EVALLAB_ZAI_INPUT_COST_MICROS_PER_MILLION" in text
    assert "EVALLAB_ZAI_OUTPUT_COST_MICROS_PER_MILLION" in text
    assert "EVALLAB_ZAI_USAGE_DIR" in text
    # Compose overlay mounts secret via volume, not top-level secrets block
    assert "secrets:\n" not in text
