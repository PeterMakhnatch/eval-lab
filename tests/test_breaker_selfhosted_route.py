"""Consumer-visible safety boundaries of the matched base/L1 transport.

Real loopback proxy subprocess, scripted HTTP peer, no vendor/model/Docker calls.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from evallab import runner
from evallab.credentials import BREAKER_SELFHOSTED_API_CREDENTIAL, missing_credential_for
from evallab.dispatch_guards import resolve_selfhosted_endpoint
from evallab.execution_contracts import (
    BREAKER_SELFHOSTED_BASE_MODEL,
    BREAKER_SELFHOSTED_KEY_ENV,
    BREAKER_SELFHOSTED_L1_MODEL,
    BREAKER_SELFHOSTED_MODEL_PREFIX,
    BREAKER_SELFHOSTED_UPSTREAM_ENV,
    ProxyTrialLimits,
    RunRequest,
    collected_secret_values,
    redact_environment,
    subscription_environment,
    validate_request,
)
from evallab.ledger import build_cost_block

SECRET = "unit-breaker-provider-credential"
CAPABILITY = "unit-breaker-trial-capability"
L1 = BREAKER_SELFHOSTED_MODEL_PREFIX + BREAKER_SELFHOSTED_L1_MODEL
BASE = BREAKER_SELFHOSTED_MODEL_PREFIX + BREAKER_SELFHOSTED_BASE_MODEL


def _post(url: str, payload: dict[str, Any], capability: str) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + capability},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_proxy_prevents_arm_switch_and_protocol_drift_then_enforces_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(payload)
            assert self.headers["Authorization"] == "Bearer " + SECRET
            body = json.dumps(
                {
                    "id": "loopback-only",
                    "object": "chat.completion",
                    "model": BREAKER_SELFHOSTED_L1_MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "redact this upstream echo: " + SECRET,
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    secret = tmp_path / "provider-key"
    secret.write_text(SECRET)
    secret.chmod(0o400)
    usage = tmp_path / "usage.json"
    monkeypatch.setenv(BREAKER_SELFHOSTED_UPSTREAM_ENV, f"http://127.0.0.1:{upstream.server_port}")
    process = None
    try:
        process, url = runner._start_terminus_proxy(
            provider="breaker_selfhosted",
            secret_path=secret,
            capability=CAPABILITY,
            attempt_id="breaker-route-regression",
            usage_path=usage,
            limits=ProxyTrialLimits(
                max_requests=1,
                max_input_tokens=100_000,
                max_output_tokens=20_000,
                max_total_tokens=120_000,
                max_cost_micros=1_000_000,
            ),
            timeout_seconds=30,
            work_dir=tmp_path,
            breaker_native=BREAKER_SELFHOSTED_L1_MODEL,
        )
        payload = {"model": L1, "messages": [{"role": "user", "content": "audit"}]}
        assert _post(url, payload, "not-the-capability")[0] in {401, 403}
        assert _post(url, {**payload, "model": BASE}, CAPABILITY)[0] == 403
        assert seen == []  # neither unauthorized request reached the serving peer
        status, body = _post(
            url,
            {
                **payload,
                "max_tokens": 100_000,
                "temperature": 0.0,
                "top_p": 0.2,
                "top_k": 5,
                "reasoning_effort": "high",
                "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "high"},
            },
            CAPABILITY,
        )
        assert status == 200
        sent = seen[0]
        assert sent["model"] == BREAKER_SELFHOSTED_L1_MODEL
        assert sent["chat_template_kwargs"] == {"enable_thinking": False}
        assert (sent["temperature"], sent["top_p"], sent["top_k"]) == (1.0, 1.0, -1)
        assert sent["max_tokens"] == 16_384
        assert "reasoning_effort" not in sent
        assert SECRET.encode() not in body
        assert _post(url, payload, CAPABILITY)[0] == 429
        recorded = json.loads(usage.read_text())
        assert recorded["totals"]["requests"] == 1
        assert SECRET not in usage.read_text()
        # The token ledger must not advertise free GPU inference.
        assert build_cost_block(recorded)["cost_usd"] is None
        assert BREAKER_SELFHOSTED_BASE_MODEL not in runner._accepted_returned_models(L1)
    finally:
        if process is not None:
            process.terminate()
            process.wait(timeout=10)
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://attacker.example",
        "https://safe.modal.run.attacker.example",
        "https://a.evil.modal.direct",
        "https://user:secret@safe.modal.run",
        "https://safe.modal.run/v1",
        "https://safe.modal.run?redirect=attacker.example",
        "https://safe.modal.run:444",
        "http://safe.modal.run:80",
    ],
)
def test_readiness_refuses_origins_that_could_receive_the_provider_key(endpoint: str) -> None:
    assert (
        resolve_selfhosted_endpoint({BREAKER_SELFHOSTED_UPSTREAM_ENV: endpoint}, model=L1) is None
    )


def test_breaker_credential_cannot_leak_into_task_environment_or_use_another_provider() -> None:
    env = {BREAKER_SELFHOSTED_KEY_ENV: SECRET, "PATH": "/usr/bin"}
    assert SECRET in collected_secret_values(env)
    assert SECRET not in json.dumps(redact_environment(env))
    assert BREAKER_SELFHOSTED_KEY_ENV not in subscription_environment(env)
    assert (
        missing_credential_for("terminus-2", frozenset({"tinker_api_environment"}), L1)
        == BREAKER_SELFHOSTED_API_CREDENTIAL
    )
    assert (
        missing_credential_for("terminus-2", frozenset({BREAKER_SELFHOSTED_API_CREDENTIAL}), L1)
        is None
    )


def test_unsupported_breaker_weights_refuse_before_any_provider_launch(tmp_path: Path) -> None:
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.toml").write_text("[agent]\ntimeout_sec = 60\n")
    request = RunRequest(
        task=task,
        agent="terminus-2",
        model=BREAKER_SELFHOSTED_MODEL_PREFIX + "unreviewed/model",
        name="refuse-breaker",
        jobs_dir=tmp_path / "runs",
        allow_billable=True,
        timeout_seconds=60,
        max_requests=1,
        max_input_tokens=100,
        max_output_tokens=100,
        max_total_tokens=200,
        cost_limit_usd=1.0,
    )
    with pytest.raises(ValueError, match="unsupported breaker-selfhosted"):
        validate_request(request)
