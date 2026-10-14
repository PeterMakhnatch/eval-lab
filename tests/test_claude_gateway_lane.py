"""Lane tests for claude-code via OMP's auth-gateway (no network, no quota).

The bridge subprocess tests run the real ``containers/claude_gateway_bridge.py``
against a stub stdio child, so no subscription allowance is consumed. Live
gateway behavior (probe readiness, trial) is proven outside this file.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab import runner
from evallab.credentials import CLAUDE_GATEWAY, CLAUDE_OAUTH, missing_credential_for
from evallab.execution_contracts import (
    RunRequest,
    collected_secret_values,
    is_claude_gateway_model,
    persist_private_bytes,
    resolve_harbor_model,
    subscription_command,
)
from evallab.profiles import (
    AgentProfile,
    GatewayStdioProbe,
    builtin_profiles,
    default_probe_for,
    preflight,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY_MODEL = "claude-gateway/claude-haiku-5-5"
HARBOR_MODEL = "anthropic/claude-haiku-5-5"
PROFILE_ID = "claude-code-gateway-haiku-5-5"

STUB = """\
import json, sys
sys.stdout.write(json.dumps({"ready": True, "version": "stub"}) + "\\n")
sys.stdout.flush()
for line in sys.stdin:
    try:
        request = json.loads(line)
    except json.JSONDecodeError:
        continue
    body = request.get("body") or {}
    mode = (body.get("messages") or [{}])[0].get("content", "")
    if mode == "STUB_JSON":
        payload = {"id": "m1", "type": "message", "role": "assistant",
                   "model": "MODEL_PLACEHOLDER",
                   "content": [{"type": "text", "text": "ok"}],
                   "stop_reason": "end_turn",
                   "usage": {"input_tokens": 10, "output_tokens": 2,
                             "cache_read_input_tokens": 0,
                             "cache_creation_input_tokens": 0}}
        sys.stdout.write(json.dumps({"id": request.get("id"), "status": 200,
                                     "body": payload}) + "\\n")
        sys.stdout.flush()
    else:
        sys.stdout.write(json.dumps({"id": request.get("id"), "status": 400,
                                     "body": {"error": {"type": "invalid_request"}}}) + "\\n")
        sys.stdout.flush()
"""


def test_gateway_pin_selects_gateway_credential() -> None:
    empty = frozenset[str]()
    assert missing_credential_for("claude-code", empty, GATEWAY_MODEL) == CLAUDE_GATEWAY
    present = frozenset({CLAUDE_GATEWAY})
    assert missing_credential_for("claude-code", present, GATEWAY_MODEL) is None
    # The Keychain lane is untouched by the new pin.
    assert missing_credential_for("claude-code", empty, "anthropic/claude-haiku-5-5") == CLAUDE_OAUTH


def test_gateway_profile_pins_model_and_transport() -> None:
    profile = builtin_profiles()[PROFILE_ID]
    assert profile.adapter == "claude-code"
    assert profile.model == GATEWAY_MODEL
    assert profile.auth_mode == "subscription-gateway"
    assert profile.secret_source == "gateway:omp auth-gateway stdio"
    assert "credential-transport:host-loopback-proxy" in profile.capabilities


def test_gateway_profile_uses_gateway_probe(tmp_path: Path) -> None:
    profile = builtin_profiles()[PROFILE_ID]
    probe = default_probe_for(
        profile,
        home=tmp_path,
        security_runner=lambda argv: 1,
        keychain_account="nobody",
    )
    assert isinstance(probe, GatewayStdioProbe)
    assert probe.argv == ("omp", "auth-gateway", "stdio")
    ready = GatewayStdioProbe(runner=lambda argv, timeout: '{"ready": true}')
    assert preflight(profile, ready).proceed


def test_gateway_probe_reads_only_the_ready_line() -> None:
    profile = builtin_profiles()[PROFILE_ID]
    assert GatewayStdioProbe(runner=lambda argv, timeout: '{"ready": true}')(profile).ok
    assert not GatewayStdioProbe(runner=lambda argv, timeout: '{"ready": false}')(profile).ok
    assert not GatewayStdioProbe(runner=lambda argv, timeout: "not json")(profile).ok
    assert not GatewayStdioProbe(runner=lambda argv, timeout: None)(profile).ok

    def _boom(argv: object, timeout: float) -> str | None:
        raise OSError("no omp here")

    assert not GatewayStdioProbe(runner=_boom)(profile).ok


def test_gateway_probe_starts_a_real_ready_child() -> None:
    profile = builtin_profiles()[PROFILE_ID]
    argv = (
        sys.executable,
        "-c",
        "import json,sys; sys.stdout.write(json.dumps({'ready': True}) + chr(10))",
    )
    assert GatewayStdioProbe(argv=argv, timeout_seconds=15.0)(profile).ok


def test_gateway_coherence_rejects_other_sources() -> None:
    with pytest.raises(ValidationError, match="gateway auth requires"):
        AgentProfile(
            profile_id="bad-gateway",
            adapter="claude-code",
            model=GATEWAY_MODEL,
            auth_mode="subscription-gateway",
            secret_source="keychain:something",
        )


def test_gateway_model_predicate_and_harbor_translation() -> None:
    assert is_claude_gateway_model(GATEWAY_MODEL)
    assert not is_claude_gateway_model("anthropic/claude-haiku-5-5")
    assert not is_claude_gateway_model(None)
    assert resolve_harbor_model("claude-code", GATEWAY_MODEL) == HARBOR_MODEL
    assert resolve_harbor_model("claude-code", "anthropic/claude-haiku-5-5") == (
        "anthropic/claude-haiku-5-5"
    )


def _request(model: str | None) -> RunRequest:
    return RunRequest(
        task=REPO_ROOT / "library/tasks/event-summary",
        agent="claude-code",
        model=model,
        name="gateway-command-check",
        jobs_dir=REPO_ROOT / "runs",
        allow_billable=True,
    )


def test_gateway_command_skips_the_keychain_wrapper() -> None:
    harbor_command = ["harbor", "run", "--agent", "claude-code"]
    assert subscription_command(_request(GATEWAY_MODEL), harbor_command, repo_root=REPO_ROOT) == (
        harbor_command
    )

    wrapped = subscription_command(
        _request("anthropic/claude-haiku-5-5"), harbor_command, repo_root=REPO_ROOT
    )
    assert wrapped[0].endswith("scripts/with-claude-auth")
    assert wrapped[1:] == harbor_command


def test_capability_bearer_redacts_from_logs(tmp_path: Path) -> None:
    capability = "gateway-capability-must-redact"
    assert capability in collected_secret_values({"ANTHROPIC_AUTH_TOKEN": capability})
    destination = tmp_path / "harbor.log"
    persist_private_bytes(
        destination,
        f"agent env ANTHROPIC_AUTH_TOKEN={capability} failed".encode(),
        secrets=tuple(
            value.encode() for value in collected_secret_values({"ANTHROPIC_AUTH_TOKEN": capability})
        ),
    )
    assert destination.read_text() == "agent env ANTHROPIC_AUTH_TOKEN=<redacted> failed"


def test_gateway_runtime_env_points_at_the_sandbox_host() -> None:
    env = runner._claude_gateway_runtime_env(port=41234, capability="cap")
    assert env == {
        "ANTHROPIC_BASE_URL": "http://host.docker.internal:41234",
        "ANTHROPIC_AUTH_TOKEN": "cap",
        "CLAUDE_FORCE_OAUTH": "0",
        "EVALLAB_CLAUDE_GATEWAY_ISOLATION_PORT": "41234",
    }


def test_bridge_start_stop_and_one_call(tmp_path: Path) -> None:
    stub = tmp_path / "stub_stdio.py"
    stub.write_text(STUB.replace("MODEL_PLACEHOLDER", HARBOR_MODEL))
    usage = tmp_path / "usage.jsonl"
    work = tmp_path / "work"
    work.mkdir()
    process, port = runner._start_claude_gateway_bridge(
        capability="lane-test-capability",
        attempt_id="lane-test-1",
        usage_path=usage,
        allowed_model=HARBOR_MODEL,
        work_dir=work,
        stdio_command=f"{sys.executable} {stub}",
    )
    try:
        ready = json.loads((work / "claude-gateway-ready.json").read_text())
        assert ready["host"] == "127.0.0.1"
        assert port == ready["port"]
        body = {
            "model": HARBOR_MODEL,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "STUB_JSON"}],
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/messages",
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer lane-test-capability"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            assert response.status == 200
            assert json.loads(response.read())["model"] == HARBOR_MODEL
        (record,) = [
            json.loads(line) for line in usage.read_text().splitlines() if line.strip()
        ]
        assert record["outcome"] == "ok"
        assert record["input_tokens"] == 10
        assert "lane-test-capability" not in usage.read_text()
    finally:
        runner._stop_claude_gateway_bridge(process)
    assert process.poll() is not None


def test_bridge_start_fails_closed_without_its_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "CLAUDE_GATEWAY_BRIDGE_SCRIPT", Path("containers/missing-bridge.py"))
    with pytest.raises(RuntimeError, match="bridge is missing"):
        runner._start_claude_gateway_bridge(
            capability="cap",
            attempt_id="attempt",
            usage_path=tmp_path / "usage.jsonl",
            allowed_model=HARBOR_MODEL,
            work_dir=tmp_path,
        )

