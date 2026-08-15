import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from evallab.cli import load_local_env
from evallab.database import _exception_type, count_consecutive_harness_failures
from evallab.runner import (
    HARBOR_COMPOSE_CONFIG_LABEL,
    HARBOR_COMPOSE_PROJECT_LABEL,
    HARBOR_COMPOSE_WORKDIR_LABEL,
    RunRequest,
    build_command,
    cleanup_new_harbor_containers,
    load_matrix,
    run_harbor_process,
    subscription_environment,
    transient_provider_reason,
    validate_request,
)


def task(tmp_path: Path) -> Path:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text('schema_version = "1.4"\n')
    return task_dir


def test_control_command_is_explicit_and_free(tmp_path: Path) -> None:
    request = RunRequest(
        task=task(tmp_path),
        agent="oracle",
        name="sample-oracle-control",
        jobs_dir=tmp_path / "runs",
    )

    validate_request(request)
    command = build_command(request)

    assert command[:2] == ["harbor", "run"]
    assert command[command.index("--agent") + 1] == "oracle"
    assert command[command.index("--n-concurrent") + 1] == "1"
    assert "--model" not in command


def test_non_control_agent_requires_billable_acknowledgement(tmp_path: Path) -> None:
    request = RunRequest(
        task=task(tmp_path),
        agent="codex",
        model="openai/example",
        name="sample-model-run",
        jobs_dir=tmp_path / "runs",
    )

    with pytest.raises(ValueError, match="allow-billable"):
        validate_request(request)


def test_existing_control_matrix_is_valid() -> None:
    root = Path(__file__).resolve().parents[1]

    matrix = load_matrix(root / "research/experiments/local-controls.json")

    assert {run.agent for run in matrix.runs} == {"oracle", "nop"}
    assert [run.expect_reward for run in matrix.runs] == [1.0, 0.0]


def test_executor_process_enforces_wall_clock_timeout(tmp_path: Path) -> None:
    started = time.monotonic()

    result = run_harbor_process(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        timeout_seconds=0.05,
        log_path=tmp_path / "executor.log",
    )

    assert result.timed_out is True
    assert time.monotonic() - started < 2
    assert result.log_path.is_file()


def test_subscription_environment_never_forwards_api_keys() -> None:
    source = {
        "HOME": "/safe/home",
        "PATH": "/safe/bin",
        "OPENAI_API_KEY": "must-not-be-read-or-forwarded",
        "ANTHROPIC_API_KEY": "must-not-be-read-or-forwarded",
    }

    assert subscription_environment(source) == {
        "HOME": "/safe/home",
        "PATH": "/safe/bin",
    }


def test_local_env_loader_ignores_model_api_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_env = tmp_path / ".env"
    local_env.write_text(
        "DATABASE_URL=postgresql://local\n"
        "EVALLAB_DERIVED_ROOT=shared/parquet\n"
        "OPENAI_API_KEY=must-not-load\n"
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("EVALLAB_DERIVED_ROOT", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    load_local_env(local_env)

    assert os.environ["DATABASE_URL"] == "postgresql://local"
    assert os.environ["EVALLAB_DERIVED_ROOT"] == "shared/parquet"
    assert "OPENAI_API_KEY" not in os.environ
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("EVALLAB_DERIVED_ROOT", None)


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (
            "provider request failed with status code: 429 (Too Many Requests)",
            "transient_harness:provider_http_429",
        ),
        (
            "upstream API returned HTTP/1.1 503 Service Unavailable",
            "transient_harness:provider_http_5xx",
        ),
        ("assertion failed: expected 500 rows", None),
    ],
)
def test_provider_status_classification(message: str, reason: str | None) -> None:
    assert transient_provider_reason(message) == reason


def test_quiet_failure_count_excludes_transient_provider_capacity() -> None:
    normalized = _exception_type(
        {
            "exception_info": {
                "exception_type": "AgentRunError",
                "message": "provider returned status code 429: Too Many Requests",
            }
        }
    )

    assert normalized == "transient_harness"
    assert count_consecutive_harness_failures(
        ["AgentRunError", "transient_harness", "VerifierError", None, "OldError"]
    ) == 2


def test_orphan_cleanup_removes_only_new_harbor_tagged_task_containers(
    tmp_path: Path,
) -> None:
    task_root = tmp_path / "task"
    task_root.mkdir()
    labels = {
        "old-harbor": {
            HARBOR_COMPOSE_PROJECT_LABEL: "task__old__trial",
            HARBOR_COMPOSE_CONFIG_LABEL: "/tools/harbor/environments/docker/base.yaml",
            HARBOR_COMPOSE_WORKDIR_LABEL: str(task_root / "tests"),
        },
        "new-harbor": {
            HARBOR_COMPOSE_PROJECT_LABEL: "task__new__trial",
            HARBOR_COMPOSE_CONFIG_LABEL: "/tools/harbor/environments/docker/base.yaml",
            HARBOR_COMPOSE_WORKDIR_LABEL: str(task_root),
        },
        "concurrent-harbor": {
            HARBOR_COMPOSE_PROJECT_LABEL: "task__other__trial",
            HARBOR_COMPOSE_CONFIG_LABEL: "/tools/harbor/environments/docker/base.yaml",
            HARBOR_COMPOSE_WORKDIR_LABEL: str(task_root),
        },
        "lab-postgres": {
            HARBOR_COMPOSE_PROJECT_LABEL: "eval-lab",
            HARBOR_COMPOSE_CONFIG_LABEL: str(tmp_path / "compose.yaml"),
            HARBOR_COMPOSE_WORKDIR_LABEL: str(tmp_path),
        },
        "other-task": {
            HARBOR_COMPOSE_PROJECT_LABEL: "other__trial",
            HARBOR_COMPOSE_CONFIG_LABEL: "/tools/harbor/environments/docker/base.yaml",
            HARBOR_COMPOSE_WORKDIR_LABEL: str(tmp_path / "other-task"),
        },
    }
    calls: list[list[str]] = []

    def command_runner(command: list[str], **_kwargs):
        calls.append(command)
        if command[:3] == ["docker", "ps", "-aq"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="\n".join(labels) + "\n",
                stderr="",
            )
        if command[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(labels[command[-1]]),
                stderr="",
            )
        if command[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(command)

    removed = cleanup_new_harbor_containers(
        task_root,
        frozenset({"old-harbor"}),
        project_prefixes=frozenset({"task__new"}),
        command_runner=command_runner,
    )

    assert removed == ("new-harbor",)
    assert ["docker", "rm", "-f", "--", "new-harbor"] in calls
    assert all("prune" not in command for command in calls)
