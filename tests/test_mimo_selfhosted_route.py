"""Focused behavioral tests for the self-hosted MiMo Terminus route.

Covers the consumer-visible boundaries of HAR-90's Terminus addition:

- ``selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`` selector grammar: the
  exact selector is accepted, anything else under ``selfhosted/`` refused.
- Upstream pinning: ``*.modal.run`` and routing-region ``*.modal.direct``
  https hosts admitted; evil hosts, userinfo, ports, paths and an unset
  upstream refused.
- The generic metered proxy under the ``mimo_selfhosted`` provider profile,
  run as a real subprocess against a loopback stub: the forwarded body
  carries the enforced generation_config (``enable_thinking: true``,
  temperature/top_p/top_k) with ``reasoning_effort`` stripped in both
  places, the stub sees the provider key as a Bearer credential, and the
  key never appears in the response or the usage ledger.
- Adapter binding: 64K context with zero per-token prices, ``temperature``
  passed to Terminus 2, capability confined to the controller environment.
- Credential gating and the time-based trial-cost helper.

Everything runs against scripted loopback fakes: no paid provider calls.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import pytest

from evallab import runner as runner_module
from evallab.credentials import (
    MIMO_SELFHOSTED_API_CREDENTIAL,
    missing_credential_for,
)
from evallab.execution_contracts import (
    MIMO_SELFHOSTED_CONTEXT_TOKENS,
    MIMO_SELFHOSTED_MODEL_SELECTOR,
    MIMO_SELFHOSTED_NATIVE_MODEL,
    MIMO_SELFHOSTED_PROXY_CAPABILITY_ENV,
    TERMINUS_PROXY_URL_ENV,
    ProxyTrialLimits,
    RunRequest,
    mimo_selfhosted_trial_cost_usd,
    parse_mimo_selfhosted_model,
    validate_request,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

SECRET_SENTINEL = "test-mimo-provider-key-24680"
CAPABILITY_SENTINEL = "test-mimo-capability-token-xyz"
UPSTREAM_USAGE = {"prompt_tokens": 10, "completion_tokens": 5}


def _proxy_module() -> Any:
    source = REPO_ROOT / "containers" / "zai_openapi_secret_proxy.py"
    spec = importlib.util.spec_from_file_location("mimo_secret_proxy", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1. Selector grammar
# ---------------------------------------------------------------------------


def test_parse_mimo_accepts_exact_selector() -> None:
    assert parse_mimo_selfhosted_model(MIMO_SELFHOSTED_MODEL_SELECTOR) == (
        MIMO_SELFHOSTED_NATIVE_MODEL
    )


@pytest.mark.parametrize(
    "selector",
    [
        "selfhosted/other-model",
        "selfhosted/",
        "selfhosted",
        "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B ",
        " selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B",
        "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B@extra",
        # Only the admitted adapter names: no other, empty, padded or chained suffix.
        "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:har130",
        "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:",
        "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:har129 ",
        "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:har129:x",
        "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:HAR129",
        "SELFHOSTED/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B",
        "tinker/Qwen/Qwen3.6-35B-A3B",
        "zai/glm-5.3-flash",
        "openai/gpt-4",
        None,
        "",
    ],
)
def test_parse_mimo_rejects_anything_else(selector: Any) -> None:
    with pytest.raises(ValueError, match="must be exactly"):
        parse_mimo_selfhosted_model(selector)


def test_parse_mimo_accepts_the_admitted_adapter() -> None:
    # SGLang's ``base:adapter`` name selects the LoRA adapter on the
    # LoRA-enabled server; it is the native id sent upstream.
    assert parse_mimo_selfhosted_model(f"{MIMO_SELFHOSTED_MODEL_SELECTOR}:har129") == (
        f"{MIMO_SELFHOSTED_NATIVE_MODEL}:har129"
    )


def test_validate_request_accepts_the_adapter_selector(tmp_path: Path) -> None:
    validate_request(_terminus_request(tmp_path, f"{MIMO_SELFHOSTED_MODEL_SELECTOR}:har129"))


def _terminus_request(tmp_path: Path, model: str, **overrides: Any) -> RunRequest:
    task_dir = tmp_path / "task"
    task_dir.mkdir(exist_ok=True)
    (task_dir / "task.toml").write_text("[agent]\ntimeout_sec = 60\n")
    values: dict[str, Any] = {
        "task": task_dir,
        "agent": "terminus-2",
        "model": model,
        "name": "mimo-run",
        "jobs_dir": tmp_path / "jobs",
        "attempts": 1,
        "allow_billable": True,
        "timeout_seconds": 60,
        "max_requests": 10,
        "max_input_tokens": 1000,
        "max_output_tokens": 1000,
        "max_total_tokens": 2000,
        "cost_limit_usd": 1.0,
    }
    values.update(overrides)
    return RunRequest(**values)


def test_validate_request_accepts_mimo_selector(tmp_path: Path) -> None:
    validate_request(_terminus_request(tmp_path, MIMO_SELFHOSTED_MODEL_SELECTOR))


def test_validate_request_rejects_other_selfhosted(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be exactly"):
        validate_request(_terminus_request(tmp_path, "selfhosted/other-model"))


def test_validate_request_rejects_mimo_without_ceilings(tmp_path: Path) -> None:
    request = _terminus_request(
        tmp_path,
        MIMO_SELFHOSTED_MODEL_SELECTOR,
        max_requests=None,
        max_input_tokens=None,
        max_output_tokens=None,
        max_total_tokens=None,
    )
    with pytest.raises(ValueError, match="requires every provider ceiling"):
        validate_request(request)


# ---------------------------------------------------------------------------
# 2. Upstream pinning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "upstream",
    [
        "https://abc123.modal.run",
        "https://abc123.modal.run:443",
        "https://p-makhnatch--evallab-mimo-v26-9b-mimoserver.us-east.modal.direct",
        "https://abc123.us-west.modal.direct",
        "https://abc123.ca-central.modal.direct",
        "https://abc123.eu-west.modal.direct",
        "https://abc123.ap-south.modal.direct",
        "https://abc123.ap-southeast-2.modal.direct",
        "http://127.0.0.1:8471",
    ],
)
def test_mimo_pinning_accepts_modal_and_loopback(
    monkeypatch: pytest.MonkeyPatch, upstream: str
) -> None:
    module = _proxy_module()
    monkeypatch.setenv("EVALLAB_PROXY_PROVIDER", "mimo_selfhosted")
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", upstream)
    pinned = module._pinned_upstream_url()
    assert pinned.endswith("/v1/chat/completions")


@pytest.mark.parametrize(
    "upstream",
    [
        "https://evil.example.com",
        "https://modal.run.evil.com",
        "https://x.evil.modal.direct",
        "https://modal.direct.evil.com",
        "https://a.b.us-east.modal.direct",
        "https://x.us-central.modal.direct",
        "http://10.0.0.1:8471",
        "http://127.0.0.1",
        "https://abc123.modal.run:8080",
        "https://abc123.modal.run/v1/extra",
        "https://user@abc123.modal.run",
        "https://user:pass@x.us-east.modal.direct",
        "https://abc123.modal.run?x=1",
        "ftp://abc123.modal.run",
    ],
)
def test_mimo_pinning_refuses_non_modal(monkeypatch: pytest.MonkeyPatch, upstream: str) -> None:
    module = _proxy_module()
    monkeypatch.setenv("EVALLAB_PROXY_PROVIDER", "mimo_selfhosted")
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", upstream)
    with pytest.raises(RuntimeError, match="not pinned"):
        module._pinned_upstream_url()


def test_mimo_pinning_fails_closed_without_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _proxy_module()
    monkeypatch.setenv("EVALLAB_PROXY_PROVIDER", "mimo_selfhosted")
    monkeypatch.delenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", raising=False)
    with pytest.raises(RuntimeError, match="EVALLAB_MIMO_SELFHOSTED_UPSTREAM"):
        module._pinned_upstream_url()


# ---------------------------------------------------------------------------
# 3. Real-subprocess proxy: shaping, auth, secrecy
# ---------------------------------------------------------------------------


class _MimoUpstream(BaseHTTPRequestHandler):
    """Local stand-in for the pinned SGLang chat-completions endpoint."""

    protocol_version = "HTTP/1.1"
    seen: list[dict[str, Any]] = []
    #: Scripted replies served before any success: (status, JSON body) or
    #: (status, raw bytes, content type) for non-JSON upstream errors.
    errors: list[Any] = []

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/chat/completions":
            self._reply(404, b"nope")
            return
        if self.headers.get("Authorization") != f"Bearer {SECRET_SENTINEL}":
            self._reply(401, b"nope")
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        type(self).seen.append(body)
        if type(self).errors:
            scripted = type(self).errors.pop(0)
            if len(scripted) == 3:
                status, raw, content_type = scripted
                self._reply(status, raw, content_type=content_type)
            else:
                status, error = scripted
                self._reply(status, json.dumps(error).encode(), content_type="application/json")
            return
        response = {
            "id": "cmpl-local",
            "object": "chat.completion",
            "model": body.get("model"),
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": dict(UPSTREAM_USAGE),
        }
        self._reply(
            200, json.dumps(response).encode(), content_type="application/json; charset=utf-8"
        )

    def _reply(self, status: int, body: bytes, content_type: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@pytest.fixture
def mimo_upstream() -> Any:
    _MimoUpstream.seen = []
    _MimoUpstream.errors = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MimoUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()


def _proxy_limits(**overrides: Any) -> ProxyTrialLimits:
    values: dict[str, Any] = {
        "max_requests": 50,
        "max_input_tokens": 100000,
        "max_output_tokens": 10000,
        "max_total_tokens": 110000,
        "max_cost_micros": 10000000,
    }
    values.update(overrides)
    return ProxyTrialLimits(**values)


def _launch_mimo_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mimo_upstream: ThreadingHTTPServer,
    capability: str,
    limits: ProxyTrialLimits,
) -> tuple[subprocess.Popen[bytes], str, Path]:
    secret_file = tmp_path / "provider-key"
    secret_file.write_text(f"{SECRET_SENTINEL}\n")
    secret_file.chmod(0o600)
    usage_path = tmp_path / "usage.json"
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    host, port = mimo_upstream.server_address
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", f"http://{host}:{port}")
    process, url = runner_module._start_terminus_proxy(
        provider="mimo_selfhosted",
        secret_path=secret_file,
        capability=capability,
        attempt_id="mimo-test-attempt",
        usage_path=usage_path,
        limits=limits,
        timeout_seconds=60.0,
        work_dir=work_dir,
        mimo_native=MIMO_SELFHOSTED_NATIVE_MODEL,
    )
    return process, url, usage_path


def _post(
    url: str,
    payload: dict[str, Any],
    capability: str | None = None,
) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {capability}"} if capability else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_mimo_proxy_enforces_generation_config_and_strips_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mimo_upstream: Any
) -> None:
    process, url, usage_path = _launch_mimo_proxy(
        tmp_path, monkeypatch, mimo_upstream, CAPABILITY_SENTINEL, _proxy_limits()
    )
    try:
        status, body = _post(
            f"{url}/v1/chat/completions",
            {
                "model": MIMO_SELFHOSTED_MODEL_SELECTOR,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 100,
                "temperature": 1.0,
                "top_p": 0.1,
                "top_k": 5,
                "reasoning_effort": "none",
                "chat_template_kwargs": {
                    "reasoning_effort": "none",
                    "other": "kept",
                },
            },
            capability=CAPABILITY_SENTINEL,
        )
        assert status == 200, body
        assert len(_MimoUpstream.seen) == 1
        forwarded = _MimoUpstream.seen[0]
        # The proxy rewrote the selector to the served-model-name id and
        # forced the model's generation_config over caller values.
        assert forwarded["model"] == MIMO_SELFHOSTED_NATIVE_MODEL
        assert forwarded["temperature"] == 0.6
        assert forwarded["top_p"] == 0.95
        assert forwarded["top_k"] == 20
        # reasoning_effort is stripped in both places; the caller's other
        # template keys survive next to the forced flag.
        assert "reasoning_effort" not in forwarded
        assert forwarded["chat_template_kwargs"] == {
            "other": "kept",
            "enable_thinking": True,
        }

        response = json.loads(body)
        assert response["model"] == MIMO_SELFHOSTED_NATIVE_MODEL
        usage = json.loads(usage_path.read_text())
        assert usage["calls"][0]["requested_model"] == MIMO_SELFHOSTED_MODEL_SELECTOR
        assert usage["calls"][0]["shaping_applied"] is True
        # Zero per-token rates: the ledger prices and costs nothing.
        assert usage["pricing"] == {
            "input_cost_micros_per_million": 0,
            "output_cost_micros_per_million": 0,
        }
        assert usage["totals"]["cost_micros"] == 0
        # The provider key never appears in the response or the ledger.
        assert SECRET_SENTINEL not in body.decode()
        assert SECRET_SENTINEL not in usage_path.read_text()
    finally:
        process.terminate()
        process.wait(10)


def test_mimo_proxy_enforces_request_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mimo_upstream: Any
) -> None:
    # Zero per-token rates can never trip the cost ceiling; the request
    # ceiling still bounds the run.
    limits = _proxy_limits(max_requests=1)
    secret_file = tmp_path / "provider-key"
    secret_file.write_text(f"{SECRET_SENTINEL}\n")
    secret_file.chmod(0o600)
    usage_path = tmp_path / "usage.json"
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    host, port = mimo_upstream.server_address
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", f"http://{host}:{port}")
    process, url = runner_module._start_terminus_proxy(
        provider="mimo_selfhosted",
        secret_path=secret_file,
        capability=CAPABILITY_SENTINEL,
        attempt_id="mimo-test-attempt",
        usage_path=usage_path,
        limits=limits,
        timeout_seconds=60.0,
        work_dir=work_dir,
        mimo_native=MIMO_SELFHOSTED_NATIVE_MODEL,
    )
    try:
        payload = {
            "model": MIMO_SELFHOSTED_MODEL_SELECTOR,
            "messages": [{"role": "user", "content": "hi"}],
        }
        status, _ = _post(f"{url}/v1/chat/completions", payload, capability=CAPABILITY_SENTINEL)
        assert status == 200
        status, body = _post(f"{url}/v1/chat/completions", payload, capability=CAPABILITY_SENTINEL)
        assert status == 429
        assert b"trial budget exhausted" in body
    finally:
        process.terminate()
        process.wait(10)


# SGLang's reply when prompt + max_tokens exceed the served context (HAR-90
# trial 0758-b): an error body that carries no usage.
SGLANG_CONTEXT_OVERFLOW = {
    "object": "error",
    "message": (
        "Requested token count exceeds the model's maximum context length of 65536 tokens. "
        "You requested a total of 65574 tokens: 57382 tokens from the input messages and "
        "8192 tokens for the completion. Please reduce the number of tokens in the input "
        "messages or the completion to fit within the limit."
    ),
    "type": "BadRequestError",
    "param": None,
    "code": 400,
}


def test_mimo_proxy_settles_a_usage_less_400_as_a_zero_usage_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mimo_upstream: Any
) -> None:
    _MimoUpstream.errors = [
        (400, SGLANG_CONTEXT_OVERFLOW),
        (503, {"object": "error", "message": "server overloaded", "code": 503}),
    ]
    process, url, usage_path = _launch_mimo_proxy(
        tmp_path, monkeypatch, mimo_upstream, CAPABILITY_SENTINEL, _proxy_limits()
    )
    payload = {
        "model": MIMO_SELFHOSTED_MODEL_SELECTOR,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
    }
    endpoint = f"{url}/v1/chat/completions"
    try:
        status, body = _post(endpoint, payload, capability=CAPABILITY_SENTINEL)
        # The caller still sees the provider's rejection, so Terminus's
        # context-length handling runs unchanged.
        assert status == 400
        assert b"maximum context length" in body
        ledger = json.loads(usage_path.read_text())
        assert ledger["unresolved_requests"] == 0
        assert ledger["totals"]["input_tokens"] == 0
        assert ledger["calls"][0]["state"] == "reconciled"
        assert ledger["calls"][0]["error"] == "provider_http_400_no_usage"
        assert ledger["calls"][0]["input_tokens"] == 0
        assert ledger["calls"][0]["output_tokens"] == 0

        status, body = _post(endpoint, payload, capability=CAPABILITY_SENTINEL)
        assert status == 503
        assert b"server overloaded" in body
    finally:
        process.terminate()
        process.wait(10)

    ledger = json.loads(usage_path.read_text())
    # HAR-114: every usage-less error settles — the 400 keeps its ledger
    # string, other statuses record upstream_error_<status>.
    assert [call["state"] for call in ledger["calls"]] == ["reconciled", "reconciled"]
    assert ledger["unresolved_requests"] == 0
    assert ledger["calls"][1]["error"] == "upstream_error_503"
    assert ledger["calls"][1]["input_tokens"] == 0
    assert ledger["calls"][1]["output_tokens"] == 0
    # Settled calls release their reservations: nothing sits in attempted.
    assert ledger["totals"]["requests"] == 2
    assert ledger["totals"]["input_tokens"] == 0
    assert ledger["totals"]["output_tokens"] == 0
    assert ledger["attempted"]["requests"] == 0


def test_mimo_proxy_settles_non_json_upstream_errors_with_zero_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mimo_upstream: Any
) -> None:
    # HAR-114 (002256 shape): the Modal gateway answers with text/plain and
    # text/html error pages mid-run. Those settle with zero usage instead of
    # failing the run's reconciliation, and the agent still sees the status.
    _MimoUpstream.errors = [
        (502, b"Bad Gateway\n", "text/plain"),
        (503, b"<html><body>no healthy upstream</body></html>", "text/html"),
    ]
    process, url, usage_path = _launch_mimo_proxy(
        tmp_path, monkeypatch, mimo_upstream, CAPABILITY_SENTINEL, _proxy_limits()
    )
    payload = {
        "model": MIMO_SELFHOSTED_MODEL_SELECTOR,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
    }
    endpoint = f"{url}/v1/chat/completions"
    try:
        status, body = _post(endpoint, payload, capability=CAPABILITY_SENTINEL)
        assert status == 502
        assert json.loads(body)["code"] == 502
        assert b"Bad Gateway" not in body
        status, body = _post(endpoint, payload, capability=CAPABILITY_SENTINEL)
        assert status == 503
        assert json.loads(body)["code"] == 503
        assert b"no healthy upstream" not in body
        status, _ = _post(endpoint, payload, capability=CAPABILITY_SENTINEL)
        assert status == 200
    finally:
        process.terminate()
        process.wait(10)

    ledger = json.loads(usage_path.read_text())
    assert [call["state"] for call in ledger["calls"]] == [
        "reconciled",
        "reconciled",
        "reconciled",
    ]
    assert [call["error"] for call in ledger["calls"][:2]] == [
        "upstream_error_502",
        "upstream_error_503",
    ]
    assert ledger["unresolved_requests"] == 0
    assert ledger["attempted"]["requests"] == 0
    assert ledger["totals"]["requests"] == 3
    assert ledger["totals"]["input_tokens"] == UPSTREAM_USAGE["prompt_tokens"]
    # The provider key never appears in a fixed error body or the ledger.
    assert SECRET_SENTINEL not in usage_path.read_text()

    usage = runner_module._read_proxy_usage(
        usage_path,
        capability_id="sha256:" + hashlib.sha256(CAPABILITY_SENTINEL.encode()).hexdigest(),
        attempt_id="mimo-test-attempt",
        limits=_proxy_limits(),
        provider_label="MiMo self-hosted",
        expected_pricing={
            "input_cost_micros_per_million": 0,
            "output_cost_micros_per_million": 0,
        },
    )
    assert usage["unresolved_requests"] == 0


def test_mimo_proxy_non_json_400_settles_and_releases_its_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mimo_upstream: Any
) -> None:
    # HAR-114 (002391 shape): a single failing call at the end of a run must
    # not hold its reservation open — the held reservation is what starved
    # every later call into 429 "trial budget exhausted".
    _MimoUpstream.errors = [(400, b"request rejected\n", "text/plain")]
    process, url, usage_path = _launch_mimo_proxy(
        tmp_path, monkeypatch, mimo_upstream, CAPABILITY_SENTINEL, _proxy_limits()
    )
    payload = {
        "model": MIMO_SELFHOSTED_MODEL_SELECTOR,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
    }
    endpoint = f"{url}/v1/chat/completions"
    try:
        status, body = _post(endpoint, payload, capability=CAPABILITY_SENTINEL)
        assert status == 400
        assert json.loads(body)["code"] == 400
        # The next call still runs: the failed call released its reservation.
        status, _ = _post(endpoint, payload, capability=CAPABILITY_SENTINEL)
        assert status == 200
    finally:
        process.terminate()
        process.wait(10)

    ledger = json.loads(usage_path.read_text())
    assert ledger["calls"][0]["state"] == "reconciled"
    assert ledger["calls"][0]["error"] == "upstream_error_400"
    assert ledger["calls"][0]["status"] == 400
    assert ledger["calls"][0]["input_tokens"] == 0
    assert ledger["unresolved_requests"] == 0
    assert ledger["attempted"]["requests"] == 0


def test_mimo_proxy_leaves_an_unparseable_200_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mimo_upstream: Any
) -> None:
    # A 2xx with an unparseable body may still have been billed, so it stays
    # unresolved and keeps failing reconciliation.
    _MimoUpstream.errors = [(200, b"<html>wut</html>", "text/html")]
    process, url, usage_path = _launch_mimo_proxy(
        tmp_path, monkeypatch, mimo_upstream, CAPABILITY_SENTINEL, _proxy_limits()
    )
    payload = {
        "model": MIMO_SELFHOSTED_MODEL_SELECTOR,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
    }
    try:
        status, body = _post(f"{url}/v1/chat/completions", payload, capability=CAPABILITY_SENTINEL)
        assert status == 502
        assert b"unsupported upstream encoding" in body
    finally:
        process.terminate()
        process.wait(10)

    ledger = json.loads(usage_path.read_text())
    assert ledger["calls"][0]["state"] == "unresolved"
    assert ledger["calls"][0]["reason"] == "unsupported_upstream_encoding"
    assert ledger["unresolved_requests"] == 1
    usage = runner_module._read_proxy_usage(
        usage_path,
        capability_id="sha256:" + hashlib.sha256(CAPABILITY_SENTINEL.encode()).hexdigest(),
        attempt_id="mimo-test-attempt",
        limits=_proxy_limits(),
        provider_label="MiMo self-hosted",
        expected_pricing={
            "input_cost_micros_per_million": 0,
            "output_cost_micros_per_million": 0,
        },
    )
    assert usage["unresolved_requests"] == 1


def test_runner_accepts_a_ledger_whose_only_failure_was_a_usage_less_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mimo_upstream: Any
) -> None:
    _MimoUpstream.errors = [(400, SGLANG_CONTEXT_OVERFLOW)]
    process, url, usage_path = _launch_mimo_proxy(
        tmp_path, monkeypatch, mimo_upstream, CAPABILITY_SENTINEL, _proxy_limits()
    )
    payload = {
        "model": MIMO_SELFHOSTED_MODEL_SELECTOR,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
    }
    try:
        for expected in (400, 200):
            status, _ = _post(f"{url}/v1/chat/completions", payload, capability=CAPABILITY_SENTINEL)
            assert status == expected
    finally:
        process.terminate()
        process.wait(10)

    usage = runner_module._read_proxy_usage(
        usage_path,
        capability_id="sha256:" + hashlib.sha256(CAPABILITY_SENTINEL.encode()).hexdigest(),
        attempt_id="mimo-test-attempt",
        limits=_proxy_limits(),
        provider_label="MiMo self-hosted",
        expected_pricing={
            "input_cost_micros_per_million": 0,
            "output_cost_micros_per_million": 0,
        },
    )
    assert usage["unresolved_requests"] == 0
    assert usage["totals"]["requests"] == 2
    assert usage["totals"]["input_tokens"] == UPSTREAM_USAGE["prompt_tokens"]
    assert usage["totals"]["output_tokens"] == UPSTREAM_USAGE["completion_tokens"]


def test_a_call_its_client_abandoned_settles_before_the_proxy_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mimo_upstream: Any
) -> None:
    # HAR-90 0036-f: Harbor cancelled the agent at its 900 s timeout with a
    # call in flight. Stopping the proxy killed that call's handler, so the
    # call stayed reserved and the runner failed a trial the verifier scored.
    admitted = threading.Event()
    release_response = threading.Event()
    original_reply = _MimoUpstream._reply

    def held_reply(self, status, body, content_type="text/plain"):
        admitted.set()
        assert release_response.wait(30), "test did not release the upstream response"
        original_reply(self, status, body, content_type)

    monkeypatch.setattr(_MimoUpstream, "_reply", held_reply)
    process, url, usage_path = _launch_mimo_proxy(
        tmp_path, monkeypatch, mimo_upstream, CAPABILITY_SENTINEL, _proxy_limits()
    )
    endpoint = urlsplit(url)
    connection = HTTPConnection(endpoint.hostname, endpoint.port, timeout=10)
    payload = json.dumps(
        {
            "model": MIMO_SELFHOSTED_MODEL_SELECTOR,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 100,
        }
    ).encode()
    try:
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {CAPABILITY_SENTINEL}",
            },
        )
        assert admitted.wait(10), "proxy did not forward the admitted request"
        assert json.loads(usage_path.read_text())["calls"][0]["state"] == "reserved"
        connection.close()
        process.terminate()
        # Admission must close while the abandoned call is still held upstream.
        deadline = time.monotonic() + 10
        while True:
            assert process.poll() is None, "proxy exited with a call still in flight"
            try:
                with socket.create_connection((endpoint.hostname, endpoint.port), timeout=0.1):
                    pass
            except ConnectionRefusedError:
                break
            if time.monotonic() >= deadline:
                pytest.fail("proxy did not close its listener during shutdown")
            release_response.wait(0.05)
    finally:
        connection.close()
        release_response.set()
        runner_module._stop_terminus_proxy(process)

    usage = runner_module._read_proxy_usage(
        usage_path,
        capability_id="sha256:" + hashlib.sha256(CAPABILITY_SENTINEL.encode()).hexdigest(),
        attempt_id="mimo-test-attempt",
        limits=_proxy_limits(),
        provider_label="MiMo self-hosted",
        expected_pricing={
            "input_cost_micros_per_million": 0,
            "output_cost_micros_per_million": 0,
        },
    )
    assert usage["unresolved_requests"] == 0
    assert usage["calls"][0]["state"] == "reconciled"
    assert usage["totals"]["input_tokens"] == UPSTREAM_USAGE["prompt_tokens"]
    assert usage["totals"]["output_tokens"] == UPSTREAM_USAGE["completion_tokens"]


def test_mimo_proxy_rejects_unknown_models_with_null_pricing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mimo_upstream: Any
) -> None:
    process, url, usage_path = _launch_mimo_proxy(
        tmp_path, monkeypatch, mimo_upstream, CAPABILITY_SENTINEL, _proxy_limits()
    )
    try:
        endpoint = f"{url}/v1/chat/completions"
        status, _ = _post(
            endpoint,
            {"model": "selfhosted/other-model", "messages": []},
            capability=CAPABILITY_SENTINEL,
        )
        assert status == 403
        status, _ = _post(
            endpoint,
            {"model": "tinker/Qwen/Qwen3.6-35B-A3B", "messages": []},
            capability=CAPABILITY_SENTINEL,
        )
        assert status == 403
        assert _MimoUpstream.seen == []
        usage = json.loads(usage_path.read_text())
        assert usage["calls"] == []
        assert usage["pricing"] is None
    finally:
        process.terminate()
        process.wait(10)


def test_mimo_proxy_forwards_the_adapter_name_at_zero_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mimo_upstream: Any
) -> None:
    process, url, usage_path = _launch_mimo_proxy(
        tmp_path, monkeypatch, mimo_upstream, CAPABILITY_SENTINEL, _proxy_limits()
    )
    try:
        endpoint = f"{url}/v1/chat/completions"
        status, body = _post(
            endpoint,
            {
                "model": f"{MIMO_SELFHOSTED_MODEL_SELECTOR}:har129",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 100,
                "temperature": 1.0,
            },
            capability=CAPABILITY_SENTINEL,
        )
        assert status == 200, body
        forwarded = _MimoUpstream.seen[0]
        # The adapter arm gets the same enforced generation_config as the base.
        assert forwarded["model"] == f"{MIMO_SELFHOSTED_NATIVE_MODEL}:har129"
        assert forwarded["temperature"] == 0.6
        assert forwarded["chat_template_kwargs"] == {"enable_thinking": True}
        status, _ = _post(
            endpoint,
            {"model": f"{MIMO_SELFHOSTED_MODEL_SELECTOR}:har130", "messages": []},
            capability=CAPABILITY_SENTINEL,
        )
        assert status == 403
        assert len(_MimoUpstream.seen) == 1
        usage = json.loads(usage_path.read_text())
        assert [call["requested_model"] for call in usage["calls"]] == [
            f"{MIMO_SELFHOSTED_MODEL_SELECTOR}:har129"
        ]
        assert usage["totals"]["cost_micros"] == 0
    finally:
        process.terminate()
        process.wait(10)


# ---------------------------------------------------------------------------
# 4. Adapter binding (Harbor surface stubbed; adapter logic is real)
# ---------------------------------------------------------------------------


class _FakeTerminus2:
    """Upstream stand-in recording transport and running a no-op episode."""

    def __init__(
        self,
        logs_dir: Path | str | None = None,
        model_name: str | None = None,
        *args: Any,
        api_base: str | None = None,
        llm_kwargs: dict[str, Any] | None = None,
        llm_call_kwargs: dict[str, Any] | None = None,
        extra_env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        self.logs_dir = Path(logs_dir) if logs_dir is not None else Path(".")
        self.model_name = model_name
        self.api_base = api_base
        self._llm_kwargs = llm_kwargs
        self._llm_call_kwargs = dict(llm_call_kwargs) if llm_call_kwargs else {}
        self._extra_env = dict(extra_env) if extra_env else {}
        self.extra_kwargs = kwargs
        self.ran = False
        self.logger = __import__("logging").getLogger("fake-terminus2")


def _module(name: str, **attributes: Any) -> ModuleType:
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _package(name: str) -> ModuleType:
    module = ModuleType(name)
    module.__path__ = []  # type: ignore[attr-defined]
    return module


@pytest.fixture
def terminus_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    for name in (
        "harbor",
        "harbor.agents",
        "harbor.agents.installed",
        "harbor.agents.terminus_2",
        "harbor.llms",
    ):
        monkeypatch.setitem(sys.modules, name, _package(name))
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.installed.base",
        _module(
            "harbor.agents.installed.base",
            NonZeroAgentExitCodeError=type("NonZeroAgentExitCodeError", (RuntimeError,), {}),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "harbor.llms.lite_llm",
        _module("harbor.llms.lite_llm", LiteLLM=type("LiteLLM", (), {})),
    )
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.terminus_2.terminus_2",
        _module("harbor.agents.terminus_2.terminus_2", Terminus2=_FakeTerminus2),
    )
    sys.modules.pop("evallab.harbor_terminus", None)
    try:
        return importlib.import_module("evallab.harbor_terminus")
    finally:
        sys.modules.pop("evallab.harbor_terminus", None)


@pytest.fixture
def mimo_transport(monkeypatch: pytest.MonkeyPatch, terminus_module: Any) -> Any:
    monkeypatch.setenv(TERMINUS_PROXY_URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setenv(MIMO_SELFHOSTED_PROXY_CAPABILITY_ENV, CAPABILITY_SENTINEL)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-original-openai-key")
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    return terminus_module


def test_adapter_binds_mimo_context_and_capability(mimo_transport: Any, tmp_path: Path) -> None:
    agent = mimo_transport.SecretSafeTerminus2(
        logs_dir=tmp_path, model_name=MIMO_SELFHOSTED_MODEL_SELECTOR
    )
    assert agent.model_name == MIMO_SELFHOSTED_MODEL_SELECTOR
    assert agent.api_base == "http://127.0.0.1:9"
    model_info = agent.extra_kwargs["model_info"]
    assert model_info["max_input_tokens"] == MIMO_SELFHOSTED_CONTEXT_TOKENS == 65_536
    assert model_info["max_output_tokens"] == 65_536
    assert model_info["input_cost_per_token"] == 0.0
    assert model_info["output_cost_per_token"] == 0.0
    # The trajectory records the real sampling the proxy enforces.
    assert agent.extra_kwargs["temperature"] == 0.6
    # The capability reaches litellm's OpenAI-compatible lookup only in the
    # controller process environment, never in persisted kwargs.
    assert os.environ["OPENAI_API_KEY"] == CAPABILITY_SENTINEL
    assert CAPABILITY_SENTINEL not in json.dumps(agent._llm_kwargs or {})
    assert CAPABILITY_SENTINEL not in json.dumps(agent._llm_call_kwargs)
    assert CAPABILITY_SENTINEL not in json.dumps(agent._extra_env)


def test_adapter_rejects_mimo_model_info_override(mimo_transport: Any, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="runtime-bound"):
        mimo_transport.SecretSafeTerminus2(
            logs_dir=tmp_path,
            model_name=MIMO_SELFHOSTED_MODEL_SELECTOR,
            model_info={"max_input_tokens": 1},
        )


def test_adapter_rejects_transport_overrides_on_mimo(mimo_transport: Any, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="api_base"):
        mimo_transport.SecretSafeTerminus2(
            logs_dir=tmp_path,
            model_name=MIMO_SELFHOSTED_MODEL_SELECTOR,
            api_base="http://x:1",
        )
    with pytest.raises(ValueError, match="transport override"):
        mimo_transport.SecretSafeTerminus2(
            logs_dir=tmp_path,
            model_name=MIMO_SELFHOSTED_MODEL_SELECTOR,
            llm_call_kwargs={"OPENAI_API_KEY": "nope"},
        )


@pytest.mark.parametrize(
    "bad",
    [None, "selfhosted/other-model", "selfhosted/"],
)
def test_adapter_rejects_non_exact_mimo_models(
    mimo_transport: Any, tmp_path: Path, bad: Any
) -> None:
    with pytest.raises(ValueError, match="exact metered model|must be exactly"):
        mimo_transport.SecretSafeTerminus2(logs_dir=tmp_path, model_name=bad)


# ---------------------------------------------------------------------------
# 5. Credential gating and time-based cost
# ---------------------------------------------------------------------------


def test_mimo_route_requires_mimo_credential() -> None:
    empty = frozenset[str]()
    assert missing_credential_for("terminus-2", empty, MIMO_SELFHOSTED_MODEL_SELECTOR) == (
        MIMO_SELFHOSTED_API_CREDENTIAL
    )
    present = frozenset({MIMO_SELFHOSTED_API_CREDENTIAL})
    assert missing_credential_for("terminus-2", present, MIMO_SELFHOSTED_MODEL_SELECTOR) is None


def test_mimo_probe_requires_key_and_upstream() -> None:
    from evallab.credentials import probe_mimo_selfhosted_api_result

    assert probe_mimo_selfhosted_api_result({}).ok is False
    assert probe_mimo_selfhosted_api_result({"MIMO_SELFHOSTED_API_KEY": "k"}).ok is False
    assert (
        probe_mimo_selfhosted_api_result(
            {"EVALLAB_MIMO_SELFHOSTED_UPSTREAM": "https://x.us-east.modal.direct"}
        ).ok
        is False
    )
    assert (
        probe_mimo_selfhosted_api_result(
            {
                "MIMO_SELFHOSTED_API_KEY": "k",
                "EVALLAB_MIMO_SELFHOSTED_UPSTREAM": "https://x.us-east.modal.direct",
            }
        ).ok
        is True
    )


def test_mimo_trial_cost_arithmetic() -> None:
    # Server $/h (GPU + CPU + memory) x hours / concurrency + sandbox.
    assert mimo_selfhosted_trial_cost_usd(2.0, 1, 0.5) == pytest.approx(6.129824)
    assert mimo_selfhosted_trial_cost_usd(1.0, 2, 0.0) == pytest.approx(1.407456)
    assert mimo_selfhosted_trial_cost_usd(0.0, 1, 0.0) == 0.0


@pytest.mark.parametrize(
    "hours,concurrency,sandbox",
    [
        (1.0, 0, 0.0),
        (1.0, -1, 0.0),
        (-1.0, 1, 0.0),
        (1.0, 1, -0.5),
        (math.nan, 1, 0.0),
        (1.0, 1, math.inf),
        (1.0, True, 0.0),
    ],
)
def test_mimo_trial_cost_rejects_bad_inputs(hours: float, concurrency: Any, sandbox: float) -> None:
    with pytest.raises(ValueError):
        mimo_selfhosted_trial_cost_usd(hours, concurrency, sandbox)


@pytest.mark.parametrize(
    "returned,accepted",
    [
        # SGLang echoes --served-model-name; the first live trial failed
        # model_identity_mismatch on exactly this before the fix.
        (MIMO_SELFHOSTED_NATIVE_MODEL, True),
        (MIMO_SELFHOSTED_MODEL_SELECTOR, True),
        ("Qwen/Qwen3.5-9B", False),
        ("XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B-evil", False),
        ("other/MiMo-V2.6-Distill-Qwen-9B-x", False),
    ],
)
def test_mimo_returned_model_identity(returned: str, accepted: bool) -> None:
    allowed = runner_module._accepted_returned_models(MIMO_SELFHOSTED_MODEL_SELECTOR)
    assert (returned in allowed) is accepted


@pytest.mark.parametrize(
    "returned,accepted",
    [
        (f"{MIMO_SELFHOSTED_NATIVE_MODEL}:har129", True),
        # An adapter-arm call answered by the base model is an arm mix-up.
        (MIMO_SELFHOSTED_NATIVE_MODEL, False),
        (f"{MIMO_SELFHOSTED_NATIVE_MODEL}:har130", False),
    ],
)
def test_adapter_returned_model_identity(returned: str, accepted: bool) -> None:
    allowed = runner_module._accepted_returned_models(f"{MIMO_SELFHOSTED_MODEL_SELECTOR}:har129")
    assert (returned in allowed) is accepted


# ---------------------------------------------------------------------------
# 6. Input-token reservation (HAR-114 follow-up)
# ---------------------------------------------------------------------------
#
# Fixture pairs are (reserved byte-length, real prompt tokens) from settled
# ledger calls in research/experiments/har114-tokenflow/
# reservation-calibration.json: (171301, 47486) is the 002391 held call,
# (34903, 13351) the tightest observed ratio (2.614).


def _payload_with_billed_bytes(n: int) -> dict[str, Any]:
    """Build a request whose billed JSON is exactly ``n`` bytes."""
    probe: dict[str, Any] = {
        "messages": [{"role": "user", "content": ""}],
        "tools": None,
        "tool_choice": None,
    }
    overhead = len(json.dumps(probe, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert n > overhead
    return {
        "model": MIMO_SELFHOSTED_MODEL_SELECTOR,
        "messages": [{"role": "user", "content": "x" * (n - overhead)}],
        "tools": None,
        "tool_choice": None,
        "max_tokens": 8,
    }


def _billed_bytes(payload: dict[str, Any]) -> int:
    billed = {
        "messages": payload.get("messages"),
        "tools": payload.get("tools"),
        "tool_choice": payload.get("tool_choice"),
    }
    return len(json.dumps(billed, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def test_estimate_tokens_halves_byte_length_rounded_up() -> None:
    module = _proxy_module()
    payload = {
        "model": MIMO_SELFHOSTED_MODEL_SELECTOR,
        "messages": [
            {"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "Fix the parser."},
        ],
        "max_tokens": 64,
    }
    n_bytes = _billed_bytes(payload)
    assert module._estimate_tokens(payload) == -(-n_bytes // 2)


@pytest.mark.parametrize(
    "reserved,actual",
    [(171301, 47486), (34903, 13351)],
)
def test_estimate_tokens_covers_real_calls_and_beats_old_bound(reserved: int, actual: int) -> None:
    module = _proxy_module()
    payload = _payload_with_billed_bytes(reserved)
    assert _billed_bytes(payload) == reserved
    estimate = module._estimate_tokens(payload)
    assert estimate >= actual
    assert estimate < reserved


def test_estimate_tokens_never_below_one() -> None:
    module = _proxy_module()
    assert module._estimate_tokens({}) >= 1
