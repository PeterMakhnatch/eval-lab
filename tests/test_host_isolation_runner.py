"""Docker host-isolation dispatch contracts with no Docker, sockets, or credentials."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from evallab import execution_contracts as contracts
from evallab import runner

GATEWAY_MODEL = "claude-gateway/claude-haiku-5-5"
CLAUDE_MODEL = "anthropic/claude-haiku-5-5"
CODEX_MODEL = "openai/gpt-5-codex"


@pytest.fixture()
def run_request(tmp_path: Path) -> contracts.RunRequest:
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.toml").write_text('[task]\nname = "event-summary"\n')
    return contracts.RunRequest(
        task=task,
        agent="nop",
        name="host-isolation-check",
        jobs_dir=tmp_path / "jobs",
        timeout_seconds=90,
    )


def option_values(command: list[str], option: str) -> list[str]:
    return [command[index + 1] for index, value in enumerate(command) if value == option]


@pytest.mark.parametrize(
    ("agent", "model"),
    [
        ("nop", None),
        ("oracle", None),
        ("codex", CODEX_MODEL),
        ("claude-code", GATEWAY_MODEL),
        ("claude-code", CLAUDE_MODEL),
    ],
)
@pytest.mark.parametrize("egress_lock", [None, False])
def test_ordinary_docker_always_selects_host_isolation(run_request, agent, model, egress_lock):
    command = contracts.build_command(
        replace(run_request, agent=agent, model=model, egress_lock=egress_lock),
        setup_fingerprint="fixed-test-fingerprint",
    )

    assert option_values(command, "--env") == [
        contracts.HOST_ISOLATED_DOCKER_ENVIRONMENT_IMPORT_PATH
    ]
    assert option_values(command, "--environment-kwarg") == [f"isolation_lane={agent}"]
    assert all(
        not value.startswith(contracts.CLAUDE_GATEWAY_ISOLATION_PORT_ENV + "=")
        for value in option_values(command, "--agent-env")
    )


@pytest.mark.parametrize("agent", ["nop", "oracle"])
def test_explicit_docker_egress_lock_preserves_network_none_backend(run_request, agent):
    command = contracts.build_command(
        replace(run_request, agent=agent, egress_lock=True),
        setup_fingerprint="fixed-test-fingerprint",
    )

    assert option_values(command, "--env") == [contracts.LOCKED_DOCKER_ENVIRONMENT_IMPORT_PATH]
    assert option_values(command, "--environment-kwarg") == ["egress_lock=true"]


@pytest.mark.parametrize(
    ("agent", "model", "expected_environment"),
    [
        ("nop", None, "evallab.harbor_daytona:BoundedDaytonaEnvironment"),
        ("oracle", None, "evallab.harbor_daytona:BoundedDaytonaEnvironment"),
        ("codex", CODEX_MODEL, "evallab.harbor_daytona:BoundedDaytonaEnvironment"),
        ("claude-code", CLAUDE_MODEL, "evallab.harbor_daytona:BoundedDaytonaEnvironment"),
        (
            "mini-swe-agent",
            "zai/glm-5.3-flash",
            "evallab.harbor_daytona:SecretSafeDaytonaEnvironment",
        ),
    ],
)
def test_daytona_retains_its_existing_environment_and_ttl(
    run_request, agent, model, expected_environment
):
    command = contracts.build_command(
        replace(run_request, environment="daytona", agent=agent, model=model),
        setup_fingerprint="fixed-test-fingerprint",
    )

    assert option_values(command, "--env") == [expected_environment]
    assert option_values(command, "--environment-kwarg") == ["ttl_minutes=12"]


def test_bridge_port_is_a_controller_environment_value_only(run_request):
    environment = runner._claude_gateway_runtime_env(port=41234, capability="fixture-capability")

    assert environment == {
        "ANTHROPIC_BASE_URL": "http://host.docker.internal:41234",
        "ANTHROPIC_AUTH_TOKEN": "fixture-capability",
        "CLAUDE_FORCE_OAUTH": "0",
        contracts.CLAUDE_GATEWAY_ISOLATION_PORT_ENV: "41234",
    }
    command = contracts.build_command(
        replace(run_request, agent="claude-code", model=GATEWAY_MODEL),
        setup_fingerprint="fixed-test-fingerprint",
    )
    assert not any("41234" in value or "fixture-capability" in value for value in command)
    assert contracts.CLAUDE_GATEWAY_ISOLATION_PORT_ENV not in contracts.subscription_environment(
        environment
    )


class FinishedProcess:
    returncode = 0

    def poll(self):
        return self.returncode


@pytest.fixture()
def child_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime_root = tmp_path / "runtime"
    (runtime_root / "src").mkdir(parents=True)
    # Replace the complete source mapping: these tests never consult the
    # operator's credentials, home, Docker context, or inherited environment.
    monkeypatch.setattr(
        runner.os,
        "environ",
        {
            "HOME": str(tmp_path / "fixture-home"),
            "TMPDIR": str(tmp_path),
            "PYTHONPATH": "/fixture/inherited-source",
            contracts.CLAUDE_GATEWAY_ISOLATION_PORT_ENV: "54321",
        },
    )
    monkeypatch.setattr(runner, "_RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(runner.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(runner, "_lease_cancelled_or_lost", lambda *args: False)
    captured: list[dict[str, str]] = []

    def launch(command, **kwargs):
        captured.append(dict(kwargs["env"]))
        return FinishedProcess()

    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    return captured, runtime_root


@pytest.mark.parametrize("agent", ["nop", "oracle"])
@pytest.mark.parametrize(
    "environment_import",
    [
        "evallab.harbor_host_isolation:HostIsolatedDockerEnvironment",
        "evallab.harbor_docker:LockedDockerEnvironment",
    ],
)
def test_control_children_can_import_custom_docker_environment_without_agent_or_plugins(
    tmp_path: Path, child_capture, agent, environment_import
):
    captured, runtime_root = child_capture

    result = runner.run_harbor_process(
        ["harbor", "run", "--agent", agent, "--env", environment_import],
        cwd=tmp_path,
        timeout_seconds=5,
        log_path=tmp_path / "harbor.log",
    )

    assert result.returncode == 0
    assert captured[0]["PYTHONPATH"] == os.pathsep.join(
        [str(runtime_root / "src"), "/fixture/inherited-source"]
    )
    assert contracts.CLAUDE_GATEWAY_ISOLATION_PORT_ENV not in captured[0]


def test_only_gateway_launch_adds_its_assigned_bridge_port_to_child_environment(
    tmp_path: Path, child_capture, monkeypatch: pytest.MonkeyPatch
):
    captured, _ = child_capture
    usage_dir = tmp_path / "gateway-usage"
    usage_dir.mkdir()
    monkeypatch.setattr(runner.tempfile, "mkdtemp", lambda **kwargs: str(usage_dir))
    monkeypatch.setattr(runner.secrets, "token_urlsafe", lambda length: "fixture-capability")
    bridge_starts = []

    def start_bridge(**kwargs):
        bridge_starts.append(kwargs)
        return FinishedProcess(), 41234

    monkeypatch.setattr(runner, "_start_claude_gateway_bridge", start_bridge)

    result = runner.run_harbor_process(
        [
            "harbor",
            "run",
            "--agent",
            "claude-code",
            "--env",
            "evallab.harbor_host_isolation:HostIsolatedDockerEnvironment",
        ],
        cwd=tmp_path,
        timeout_seconds=5,
        log_path=tmp_path / "harbor.log",
        proxy_attempt_id="fixture-attempt",
        claude_gateway_model=GATEWAY_MODEL,
    )

    assert result.returncode == 0
    assert len(bridge_starts) == 1
    assert bridge_starts[0]["allowed_model"] == CLAUDE_MODEL
    assert captured[0][contracts.CLAUDE_GATEWAY_ISOLATION_PORT_ENV] == "41234"
    assert captured[0]["ANTHROPIC_BASE_URL"] == "http://host.docker.internal:41234"
    assert captured[0]["CLAUDE_FORCE_OAUTH"] == "0"
