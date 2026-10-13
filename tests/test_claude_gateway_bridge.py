"""Deterministic tests for the Claude gateway bridge (no network, no quota).

The bridge is exercised as a real subprocess over loopback HTTP against a
stub stdio child (argv seam) -- never the real ``omp auth-gateway stdio``,
so nothing here spends subscription quota or touches the provider.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
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
def server(tmp_path: Path, stub_argv: str):
    usage = tmp_path / "usage.jsonl"
    ready = tmp_path / "ready.json"
    env = dict(
        os.environ,
        EVALLAB_CLAUDE_GATEWAY_CAPABILITY=CAPABILITY,
        EVALLAB_CLAUDE_GATEWAY_ATTEMPT_ID=ATTEMPT,
        EVALLAB_CLAUDE_GATEWAY_USAGE_FILE=str(usage),
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(BRIDGE),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--ready-file",
            str(ready),
            "--allowed-model",
            MODEL,
            "--attempt-id",
            ATTEMPT,
            "--usage-file",
            str(usage),
            "--stdio-command",
            stub_argv,
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 30.0
    while not ready.is_file():
        assert process.poll() is None, "bridge exited before becoming ready"
        assert time.monotonic() < deadline, "bridge did not become ready"
        time.sleep(0.05)
    base = f"http://127.0.0.1:{json.loads(ready.read_text())['port']}"
    yield base, usage
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()


def post(base: str, body: dict, auth: str | None = "Bearer " + CAPABILITY):
    headers = {"Content-Type": "application/json"}
    if auth is not None:
        headers["Authorization"] = auth
    request = urllib.request.Request(
        base + "/v1/messages", data=json.dumps(body).encode(), headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.headers.get("Content-Type"), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type"), exc.read()


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
    # x-api-key (what CLIs send to custom endpoints) is accepted too.
    status, _, payload = post(base, make_body("STUB_JSON"), auth=None)
    assert status == 401
    import urllib.request as urlrequest

    authed = urlrequest.Request(
        base + "/v1/messages",
        data=json.dumps(make_body("STUB_JSON")).encode(),
        headers={"Content-Type": "application/json", "x-api-key": CAPABILITY},
    )
    with urlrequest.urlopen(authed, timeout=60) as response:
        assert response.status == 200
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
    request = urllib.request.Request(
        base + "/nope",
        data=b"{}",
        headers={"Authorization": "Bearer " + CAPABILITY, "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request, timeout=60)
        raise AssertionError("expected HTTPError")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    health = urllib.request.urlopen(base + "/healthz", timeout=60)
    assert health.status == 200


def test_capability_never_reaches_the_usage_file_or_logs(server) -> None:
    base, usage = server
    post(base, make_body("STUB_JSON"))
    post(base, make_body("STUB_JSON"), auth="Bearer wrong")
    text = usage.read_text() if usage.is_file() else ""
    assert CAPABILITY not in text
    assert "wrong" not in text


def test_dead_child_fails_requests(server) -> None:
    base, _ = server
    status, _, _ = post(base, make_body("STUB_DIE"))
    assert status == 502


def test_argv_validation_fails_closed(tmp_path: Path) -> None:
    usage = tmp_path / "usage.jsonl"
    env = dict(
        os.environ,
        EVALLAB_CLAUDE_GATEWAY_ATTEMPT_ID=ATTEMPT,
        EVALLAB_CLAUDE_GATEWAY_USAGE_FILE=str(usage),
    )
    no_cap = subprocess.run(
        [sys.executable, str(BRIDGE), "--allowed-model", MODEL],
        env={k: v for k, v in env.items() if k != "EVALLAB_CLAUDE_GATEWAY_CAPABILITY"},
        capture_output=True,
        timeout=30,
    )
    assert no_cap.returncode == 2
    external = subprocess.run(
        [
            sys.executable,
            str(BRIDGE),
            "--host",
            "0.0.0.0",
            "--allowed-model",
            MODEL,
        ],
        env=dict(env, EVALLAB_CLAUDE_GATEWAY_CAPABILITY=CAPABILITY),
        capture_output=True,
        timeout=30,
    )
    assert external.returncode == 2
    unpinned = subprocess.run(
        [sys.executable, str(BRIDGE), "--attempt-id", ATTEMPT, "--usage-file", str(usage)],
        env=dict(env, EVALLAB_CLAUDE_GATEWAY_CAPABILITY=CAPABILITY),
        capture_output=True,
        timeout=30,
    )
    assert unpinned.returncode == 2


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
