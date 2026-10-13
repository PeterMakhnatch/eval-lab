"""Deterministic bridge tests with injected HTTP streams and a pipe-only child.

No sockets, real credentials, provider calls, or subscription quota are used.
"""

from __future__ import annotations

import importlib.util
import io
import json
import shlex
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BRIDGE = REPO_ROOT / "containers" / "claude_gateway_bridge.py"

CAPABILITY = "test-capability-never-a-real-secret"  # noqa: S105 (fixture only)
ATTEMPT = "test-attempt-1"
MODEL = "anthropic/claude-haiku-5-5"

SSE_OK = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"id":"m1","type":"message","role":"assistant",'
    '"model":"anthropic/claude-haiku-5-5","content":[],"stop_reason":null,'
    '"usage":{"input_tokens":34,"output_tokens":2,"cache_read_input_tokens":0,'
    '"cache_creation_input_tokens":0}}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n'
    'event: message_delta\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    '"usage":{"output_tokens":4}}\n\n'
    'event: message_stop\n'
    'data: {"type":"message_stop"}\n\n'
)

JSON_OK = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "anthropic/claude-haiku-5-5",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {
        "input_tokens": 34,
        "output_tokens": 4,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    },
}

STUB = """\
import json, sys
def emit(value):
    sys.stdout.write(json.dumps(value) + "\\n")
    sys.stdout.flush()
emit({"ready": True, "version": "stub"})
for line in sys.stdin:
    try:
        request = json.loads(line)
    except json.JSONDecodeError:
        continue
    body = request.get("body") or {}
    mode = (body.get("messages") or [{}])[0].get("content", "")
    ident = request.get("id")
    if mode == "STUB_JSON":
        emit({"id": ident, "status": 200, "body": __JSON__})
    elif mode == "STUB_STREAM":
        emit({"id": ident, "status": 200, "body": __SSE__})
    elif mode == "STUB_WRONG_MODEL":
        wrong = dict(__JSON__)
        wrong["model"] = "anthropic/claude-opus-9-9"
        emit({"id": ident, "status": 200, "body": wrong})
    elif mode == "STUB_NO_MODEL":
        emit({"id": ident, "status": 200, "body": {"type": "message"}})
    elif mode == "STUB_429":
        emit({"id": ident, "status": 429, "body": {"error": {"type": "rate_limit_error",
              "message": "Request was throttled. Please try again later."}}})
    elif mode == "STUB_DIE":
        sys.exit(0)
    else:
        emit({"id": ident, "status": 400, "body": {"error": {"type": "invalid_request"}}})
"""


def load_bridge() -> ModuleType:
    spec = importlib.util.spec_from_file_location("claude_gateway_bridge", BRIDGE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def stub_argv(tmp_path: Path) -> str:
    stub = tmp_path / "stub_stdio.py"
    stub.write_text(STUB.replace("__JSON__", json.dumps(JSON_OK)).replace("__SSE__", json.dumps(SSE_OK)))
    return f"{sys.executable} {stub}"


@pytest.fixture()
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bridge = load_bridge()
    fixed = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)

    class FixedDatetime:
        @staticmethod
        def now(tz=None):
            assert tz == UTC
            return fixed

    monkeypatch.setattr(bridge, "datetime", FixedDatetime)
    logs = []
    monkeypatch.setattr(bridge, "_log", logs.append)
    usage = tmp_path / "usage.jsonl"
    gateway = StubGateway(bridge)
    # Bypass ThreadingHTTPServer.__init__: exercising the real handler and
    # record implementation does not require opening a listening socket.
    bridge_server = bridge.BridgeServer.__new__(bridge.BridgeServer)
    bridge_server.capability = CAPABILITY
    bridge_server.attempt_id = ATTEMPT
    bridge_server.usage_path = usage
    bridge_server.allowed_models = frozenset({MODEL})
    bridge_server.gateway = gateway
    bridge_server._usage_lock = threading.Lock()
    return BridgeHarness(bridge, bridge_server, gateway, logs), usage


class StubGateway:
    def __init__(self, bridge: ModuleType):
        self.bridge = bridge
        self.calls: list[tuple[str, dict]] = []

    def request(self, path: str, body: dict):
        self.calls.append((path, body))
        mode = (body.get("messages") or [{}])[0].get("content", "")
        if mode == "STUB_JSON":
            return 200, dict(JSON_OK)
        if mode == "STUB_STREAM":
            return 200, SSE_OK
        if mode == "STUB_WRONG_MODEL":
            return 200, dict(JSON_OK, model="anthropic/claude-opus-9-9")
        if mode == "STUB_NO_MODEL":
            return 200, {"type": "message"}
        if mode == "STUB_429":
            return 429, {"error": {"type": "rate_limit_error", "message": "throttled"}}
        if mode == "STUB_DIE":
            raise self.bridge.GatewayError("stub child died")
        return 400, {"error": {"type": "invalid_request"}}


class ObservedRequestStream(io.BytesIO):
    def __init__(self, request: bytes):
        super().__init__(request)
        self.body_reads: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.body_reads.append(size)
        return super().read(size)


class BridgeHarness:
    def __init__(self, bridge: ModuleType, bridge_server, gateway: StubGateway, logs: list):
        self.bridge = bridge
        self.bridge_server = bridge_server
        self.gateway = gateway
        self.logs = logs
        self.last_handler = None

    def request(
        self,
        method: str,
        target: str,
        body: bytes = b"",
        *,
        auth: str | None = "Bearer " + CAPABILITY,
        headers: dict[str, str] | None = None,
    ):
        request_headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        if auth is not None:
            request_headers["Authorization"] = auth
        request_headers.update(headers or {})
        wire = f"{method} {target} HTTP/1.1\r\n".encode()
        wire += b"".join(f"{key}: {value}\r\n".encode() for key, value in request_headers.items())
        wire += b"\r\n" + body
        handler = self.bridge.Handler.__new__(self.bridge.Handler)
        handler.server = self.bridge_server
        handler.rfile = ObservedRequestStream(wire)
        handler.wfile = io.BytesIO()
        handler.close_connection = True
        handler.date_time_string = lambda timestamp=None: "Tue, 13 Oct 2026 12:00:00 GMT"
        handler.handle_one_request()
        self.last_handler = handler
        response = handler.wfile.getvalue()
        if response.startswith(b"HTTP/1.1 100 Continue\r\n"):
            _, response = response.split(b"\r\n\r\n", 1)
        response_head, response_body = response.split(b"\r\n\r\n", 1)
        lines = response_head.decode().split("\r\n")
        status = int(lines[0].split()[1])
        response_headers = dict(line.split(": ", 1) for line in lines[1:])
        return status, response_headers.get("Content-Type"), response_body


def post(base: BridgeHarness, body: dict, auth: str | None = "Bearer " + CAPABILITY):
    return base.request("POST", "/v1/messages", json.dumps(body).encode(), auth=auth)


def make_body(content: str, **overrides):
    body = {"model": MODEL, "max_tokens": 8, "messages": [{"role": "user", "content": content}]}
    body.update(overrides)
    return body


def usage_records(usage: Path):
    if not usage.is_file():
        return []
    return [json.loads(line) for line in usage.read_text().splitlines() if line.strip()]


def test_json_passthrough_records_usage(server) -> None:
    base, usage = server
    status, content_type, payload = post(base, make_body("STUB_JSON"))

    assert status == 200
    assert content_type == "application/json"
    assert json.loads(payload)["model"] == MODEL
    (record,) = usage_records(usage)
    assert record["outcome"] == "ok"
    assert record["model_requested"] == MODEL
    assert record["model_served"] == MODEL
    assert record["input_tokens"] == 34
    assert record["output_tokens"] == 4
    assert record["rate_limited"] is False
    assert record["attempt_id"] == ATTEMPT


def test_sse_passthrough_merges_usage(server) -> None:
    base, usage = server
    status, content_type, payload = post(base, make_body("STUB_STREAM", stream=True))

    assert status == 200
    assert content_type == "text/event-stream"
    text = payload.decode()
    assert "event: message_start" in text
    assert "event: message_stop" in text
    (record,) = usage_records(usage)
    assert record["stream"] is True
    assert record["input_tokens"] == 34
    assert record["output_tokens"] == 4


def test_capability_is_checked_before_anything_else(server) -> None:
    base, usage = server
    for auth in (None, "Bearer wrong-capability", "Bearer "):
        status, _, _ = post(base, make_body("STUB_JSON"), auth=auth)
        assert status == 401
        assert base.last_handler.rfile.body_reads == []
        assert base.last_handler.close_connection is True
    assert base.gateway.calls == []
    # x-api-key (what CLIs send to custom endpoints) is accepted too.
    status, _, _ = base.request(
        "POST",
        "/v1/messages",
        json.dumps(make_body("STUB_JSON")).encode(),
        auth=None,
        headers={"x-api-key": CAPABILITY},
    )
    assert status == 200
    assert usage.is_file()


def test_disallowed_model_is_refused(server) -> None:
    base, _ = server
    status, _, payload = post(base, make_body("STUB_JSON", model="anthropic/claude-opus-9-9"))
    assert status == 400


def test_model_mismatch_fails_closed(server) -> None:
    base, usage = server
    status, _, _ = post(base, make_body("STUB_WRONG_MODEL"))
    assert status == 502
    (record,) = usage_records(usage)
    assert record["outcome"] == "model_mismatch"
    assert record["model_served"] == "anthropic/claude-opus-9-9"


def test_success_without_a_model_fails_closed(server) -> None:
    base, _ = server
    status, _, _ = post(base, make_body("STUB_NO_MODEL"))
    assert status == 502


def test_rate_limit_passes_through_and_is_recorded(server) -> None:
    base, usage = server
    status, _, payload = post(base, make_body("STUB_429"))
    assert status == 429
    assert b"rate_limit_error" in payload
    (record,) = usage_records(usage)
    assert record["rate_limited"] is True
    assert record["status"] == 429


def test_malformed_bodies_and_paths_are_refused(server) -> None:
    base, _ = server
    status, _, _ = post(base, {"model": MODEL})
    assert status in (400, 502)
    assert base.request("POST", "/nope", b"{}")[0] == 404
    # Readiness is a controller-owned ready file, never an HTTP exception
    # to the model-only sandbox contract.
    assert base.request("GET", "/healthz")[0] == 404


def test_capability_never_reaches_the_usage_file_or_logs(server) -> None:
    base, usage = server
    post(base, make_body("STUB_JSON"))
    post(base, make_body("STUB_JSON"), auth="Bearer wrong")
    text = usage.read_text() if usage.is_file() else ""
    assert CAPABILITY not in text
    assert "wrong" not in text
    assert CAPABILITY not in json.dumps(base.logs)
    assert "wrong" not in json.dumps(base.logs)


def test_dead_child_fails_requests(server) -> None:
    base, _ = server
    status, _, _ = post(base, make_body("STUB_DIE"))
    assert status == 502


def test_argv_validation_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = load_bridge()
    usage = tmp_path / "usage.jsonl"
    monkeypatch.delenv(bridge.CAPABILITY_ENV, raising=False)
    monkeypatch.setenv(bridge.ATTEMPT_ENV, ATTEMPT)
    monkeypatch.setenv(bridge.USAGE_FILE_ENV, str(usage))

    def unexpected_gateway(*args, **kwargs):
        raise AssertionError("invalid argv must not start a child")

    monkeypatch.setattr(bridge, "StdioGateway", unexpected_gateway)
    assert bridge.main(["--allowed-model", MODEL]) == 2
    monkeypatch.setenv(bridge.CAPABILITY_ENV, CAPABILITY)
    assert bridge.main(["--host", "0.0.0.0", "--allowed-model", MODEL]) == 2
    assert bridge.main(["--attempt-id", ATTEMPT, "--usage-file", str(usage)]) == 2


def test_models_match_accepts_only_the_anthropic_prefix() -> None:
    bridge = load_bridge()
    assert bridge._models_match(MODEL, MODEL)
    assert bridge._models_match("claude-haiku-5-5", "anthropic/claude-haiku-5-5")
    assert bridge._models_match(MODEL, "claude-haiku-5-5")
    assert not bridge._models_match(MODEL, "anthropic/claude-opus-9-9")
    assert not bridge._models_match(MODEL, "other/claude-haiku-5-5")
    assert not bridge._models_match(MODEL, "")


def test_served_model_parses_both_shapes() -> None:
    bridge = load_bridge()
    assert bridge._served_model(MODEL, 200, dict(JSON_OK)) == MODEL
    assert bridge._served_model(MODEL, 200, SSE_OK) == MODEL
    assert bridge._served_model(MODEL, 200, {"type": "message"}) is None
    assert bridge._served_model(MODEL, 200, "not sse") is None


@pytest.mark.parametrize("target", ["/v1/messages", "/v1/messages?beta=true"])
def test_exact_messages_targets_forward_only_canonical_model_path(server, target: str) -> None:
    base, usage = server
    body = make_body("STUB_JSON")

    status, _, payload = base.request("POST", target, json.dumps(body).encode())

    assert status == 200
    assert json.loads(payload)["model"] == MODEL
    assert base.gateway.calls == [("/v1/messages", body)]
    assert base.last_handler.close_connection is True
    assert usage_records(usage)[0]["ts"] == "2026-10-13T12:00:00+00:00"


@pytest.mark.parametrize(
    "target",
    [
        "/healthz",
        "/v1/models",
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/messages/count_tokens",
        "/v1/messages/count_tokens?beta=true",
        "/auth",
        "/credentials",
        "/v1/messages/",
        "//v1/messages",
        "///v1/messages?beta=true",
        "/v1//messages",
        "/v1/./messages",
        "/v1/other/../messages",
        "/v1/%6dessages",
        "/v1/messages%3fbeta=true",
        "/v1/messages;beta=true",
        "/v1/messages#fragment",
        "http://localhost/v1/messages",
        "https://host.docker.internal/v1/messages?beta=true",
        "*",
        "/v1/messages?",
        "/v1/messages?beta=false",
        "/v1/messages?beta=TRUE",
        "/v1/messages?beta=true&",
        "/v1/messages?beta=true&beta=true",
        "/v1/messages?beta=true&path=/v1/models",
        "/v1/messages?%62eta=true",
        "/v1/messages?beta=%74rue",
        "/v1/messages?other=true",
        "/v1/messages?beta=true#fragment",
    ],
)
def test_other_targets_fail_before_body_read_or_gateway_call(server, target: str) -> None:
    base, usage = server

    status, _, payload = base.request("POST", target, b"untrusted-body")

    assert status == 404
    assert json.loads(payload)["error"]["type"] == "not_found"
    assert base.last_handler.rfile.body_reads == []
    assert base.last_handler.close_connection is True
    assert base.gateway.calls == []
    assert base.logs == []
    assert not usage.exists()


@pytest.mark.parametrize(
    "method", ["GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "post", "X"]
)
@pytest.mark.parametrize("target", ["/v1/messages", "/v1/messages?beta=true", "/healthz"])
def test_every_other_method_is_refused_before_body_or_gateway(server, method: str, target: str):
    base, usage = server

    assert base.request(method, target, b"untrusted-body")[0] == 404

    assert base.last_handler.rfile.body_reads == []
    assert base.last_handler.close_connection is True
    assert base.gateway.calls == []
    assert not usage.exists()


def test_beta_messages_require_capability_before_body_read(server) -> None:
    base, usage = server

    assert base.request("POST", "/v1/messages?beta=true", b"untrusted-body", auth=None)[0] == 401

    assert base.last_handler.rfile.body_reads == []
    assert base.last_handler.close_connection is True
    assert base.gateway.calls == []
    assert not usage.exists()


@pytest.mark.parametrize(
    ("method", "target", "auth", "status"),
    [
        ("POST", "/v1/models", "Bearer " + CAPABILITY, 404),
        ("GET", "/v1/messages", "Bearer " + CAPABILITY, 404),
        ("POST", "/v1/messages", None, 401),
    ],
)
def test_expect_continue_does_not_invite_rejected_bodies(server, method, target, auth, status):
    base, usage = server

    assert base.request(
        method, target, b"untrusted-body", auth=auth, headers={"Expect": "100-continue"}
    )[0] == status

    assert b"100 Continue" not in base.last_handler.wfile.getvalue()
    assert base.last_handler.rfile.body_reads == []
    assert base.last_handler.close_connection is True
    assert base.gateway.calls == []
    assert not usage.exists()


def test_expect_continue_is_supported_for_authenticated_messages(server) -> None:
    base, _ = server

    assert base.request(
        "POST",
        "/v1/messages?beta=true",
        json.dumps(make_body("STUB_JSON")).encode(),
        headers={"Expect": "100-continue"},
    )[0] == 200

    assert base.last_handler.wfile.getvalue().startswith(b"HTTP/1.1 100 Continue\r\n")
    assert len(base.gateway.calls) == 1


@pytest.fixture()
def stdio_gateway(stub_argv: str):
    bridge = load_bridge()
    gateway = bridge.StdioGateway(
        shlex.split(stub_argv), max_concurrency=1, upstream_timeout=5.0
    )
    try:
        yield bridge, gateway
    finally:
        gateway.stop()
        try:
            gateway._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            gateway._process.kill()
            gateway._process.wait(timeout=5)
        gateway._reader.join(timeout=5)
        gateway._stderr_reader.join(timeout=5)
        for stream in (gateway._process.stdin, gateway._process.stdout, gateway._process.stderr):
            stream.close()


def test_stdio_child_exchanges_json_and_sse_over_pipes(stdio_gateway) -> None:
    _, gateway = stdio_gateway

    assert gateway.request("/v1/messages", make_body("STUB_JSON")) == (200, JSON_OK)
    assert gateway.request("/v1/messages", make_body("STUB_STREAM")) == (200, SSE_OK)


def test_stdio_child_death_fails_current_and_future_calls(stdio_gateway) -> None:
    bridge, gateway = stdio_gateway

    with pytest.raises(bridge.GatewayError):
        gateway.request("/v1/messages", make_body("STUB_DIE"))
    with pytest.raises(bridge.GatewayError):
        gateway.request("/v1/messages", make_body("STUB_JSON"))
