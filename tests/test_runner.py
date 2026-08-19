import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import evallab.runner as runner_module
from evallab.cli import load_local_env
from evallab.database import _exception_type, count_consecutive_harness_failures
from evallab.runner import (
    HARBOR_COMPOSE_CONFIG_LABEL,
    HARBOR_COMPOSE_PROJECT_LABEL,
    HARBOR_COMPOSE_WORKDIR_LABEL,
    LOCAL_TO_HARBOR_MODEL,
    HarborProcessResult,
    RunRequest,
    TransientHarnessFailure,
    build_command,
    cleanup_new_harbor_containers,
    load_matrix,
    resolve_harbor_model,
    run_experiment,
    run_harbor_process,
    subscription_command,
    subscription_environment,
    transient_provider_exception,
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


def test_executor_watchdog_enforces_each_trial_in_multi_attempt_job(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "jobs/multi-attempt"
    script = (
        "import json, pathlib, sys, time; "
        "job=pathlib.Path(sys.argv[1]); job.mkdir(parents=True); "
        "done=job/'task__done'; done.mkdir(); "
        "(done/'result.json').write_text(json.dumps({'finished_at':'done'})); "
        "hung=job/'task__hung'; hung.mkdir(); "
        "(hung/'result.json').write_text(json.dumps({'finished_at':None})); "
        "time.sleep(5)"
    )

    result = run_harbor_process(
        [sys.executable, "-c", script, str(job_dir)],
        cwd=tmp_path,
        timeout_seconds=5,
        trial_timeout_seconds=0.05,
        job_dir=job_dir,
        log_path=tmp_path / "multi-attempt.log",
    )

    assert result.timed_out is True
    assert result.timed_out_trial == "task__hung"


def test_executor_watchdog_ignores_completed_trial_directories(tmp_path: Path) -> None:
    job_dir = tmp_path / "jobs/completed"
    script = (
        "import json, pathlib, sys, time; "
        "job=pathlib.Path(sys.argv[1]); job.mkdir(parents=True); "
        "done=job/'task__done'; done.mkdir(); "
        "(done/'result.json').write_text(json.dumps({'finished_at':'done'})); "
        "time.sleep(0.2)"
    )

    result = run_harbor_process(
        [sys.executable, "-c", script, str(job_dir)],
        cwd=tmp_path,
        timeout_seconds=2,
        trial_timeout_seconds=0.05,
        job_dir=job_dir,
        log_path=tmp_path / "completed.log",
    )

    assert result.timed_out is False
    assert result.timed_out_trial is None


def test_subscription_environment_never_forwards_api_keys() -> None:
    source = {
        "HOME": "/safe/home",
        "HARBOR_CLAUDE_KEYCHAIN_ACCOUNT": "operator",
        "HARBOR_CLAUDE_KEYCHAIN_SERVICE": "eval-lab-claude",
        "PATH": "/safe/bin",
        "OPENAI_API_KEY": "must-not-be-read-or-forwarded",
        "ANTHROPIC_API_KEY": "must-not-be-read-or-forwarded",
    }

    assert subscription_environment(source) == {
        "CLAUDE_FORCE_OAUTH": "1",
        "CODEX_FORCE_AUTH_JSON": "1",
        "HOME": "/safe/home",
        "HARBOR_CLAUDE_KEYCHAIN_ACCOUNT": "operator",
        "HARBOR_CLAUDE_KEYCHAIN_SERVICE": "eval-lab-claude",
        "PATH": "/safe/bin",
        "REWARDKIT_FORCE_OAUTH": "1",
    }


def test_subscription_environment_does_not_forward_ambient_oauth_token() -> None:
    source = {
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token",
        "OPENAI_API_KEY": "must-not-forward",
        "ANTHROPIC_API_KEY": "must-not-forward",
    }

    assert subscription_environment(source) == {
        "CLAUDE_FORCE_OAUTH": "1",
        "CODEX_FORCE_AUTH_JSON": "1",
        "REWARDKIT_FORCE_OAUTH": "1",
    }


def test_subscription_command_routes_only_claude_through_keychain_wrapper(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    wrapper = repo / "scripts/with-claude-auth"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\n")
    harbor_command = ["harbor", "run"]
    claude = RunRequest(
        task=task(tmp_path),
        agent="claude-code",
        model="anthropic/example",
        name="claude-subscription",
        jobs_dir=tmp_path / "runs",
        allow_billable=True,
    )
    codex = RunRequest(
        task=claude.task,
        agent="codex",
        model="openai/example",
        name="codex-subscription",
        jobs_dir=tmp_path / "runs",
        allow_billable=True,
    )

    assert subscription_command(claude, harbor_command, repo_root=repo) == [
        str(wrapper.resolve()),
        *harbor_command,
    ]
    assert subscription_command(codex, harbor_command, repo_root=repo) == harbor_command


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


def test_provider_retry_requires_structured_agent_exception() -> None:
    provider_failure = {
        "exception_info": {
            "exception_type": "AgentRunError",
            "message": "provider returned status code 503",
        }
    }
    task_server_failure = {
        "exception_info": {
            "exception_type": "VerifierError",
            "message": "task server returned status code 503",
        }
    }
    successful_result_with_log_text = {
        "finished_at": "2026-08-15T00:00:00Z",
        "exception_info": None,
        "agent_log": "provider returned status code 503 and recovered",
    }

    assert transient_provider_exception(provider_failure) == (
        "transient_harness:provider_http_5xx"
    )
    assert transient_provider_exception(task_server_failure) is None
    assert transient_provider_exception(successful_result_with_log_text) is None


@pytest.mark.parametrize(
    ("exception_type", "message", "reason"),
    [
        (
            "ApiRateLimitError",
            "model provider rate limited the request",
            "transient_harness:provider_http_429",
        ),
        (
            "ApiInternalServerError",
            "API Error: Internal server error",
            "transient_harness:provider_http_5xx",
        ),
        (
            "ApiOverloadedError",
            "API Error: Overloaded",
            "transient_harness:provider_http_5xx",
        ),
        (
            "NonZeroAgentExitCodeError",
            "upstream API returned status code 503",
            None,
        ),
        (
            "UnknownApiError",
            "upstream API returned status code 503",
            "transient_harness:provider_http_5xx",
        ),
        (
            "NonZeroAgentExitCodeError",
            "unexpected status 401 Unauthorized",
            None,
        ),
    ],
)
def test_harbor_021_provider_exception_classification(
    exception_type: str,
    message: str,
    reason: str | None,
) -> None:
    result = {
        "exception_info": {
            "exception_type": exception_type,
            "exception_message": message,
        }
    }

    assert transient_provider_exception(result) == reason


def test_generic_agent_failure_does_not_classify_status_from_task_prompt() -> None:
    result = {
        "exception_info": {
            "exception_type": "NonZeroAgentExitCodeError",
            "exception_message": (
                "Command failed: repair a provider status 503 example\n"
                "stdout: unexpected status 401 Unauthorized"
            ),
        }
    }

    assert transient_provider_exception(result) is None


def test_generic_agent_failure_classifies_only_stripped_adapter_output() -> None:
    result = {
        "exception_info": {
            "exception_type": "NonZeroAgentExitCodeError",
            "exception_message": (
                "Command failed: evaluate this task\n"
                "stdout: unexpected status 500 Internal Server Error\n"
                "stderr: None"
            ),
        }
    }

    assert transient_provider_exception(result) == (
        "transient_harness:provider_http_5xx"
    )


def test_successful_harbor_process_with_transient_trial_is_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = RunRequest(
        task=task(tmp_path),
        agent="oracle",
        name="provider-overloaded",
        jobs_dir=tmp_path / "runs",
    )
    cleaned: list[Path] = []

    def completed_with_transient_trial(*_args, **kwargs) -> HarborProcessResult:
        job_dir = kwargs["job_dir"]
        job_dir.mkdir(parents=True)
        (job_dir / "result.json").write_text(
            json.dumps(
                {
                    "n_total_trials": 1,
                    "stats": {},
                    "finished_at": "2026-08-15T00:00:00Z",
                }
            )
        )
        trial_dir = job_dir / "task__trial"
        trial_dir.mkdir()
        (trial_dir / "result.json").write_text(
            json.dumps(
                {
                    "task_name": "task",
                    "trial_name": "task__trial",
                    "exception_info": {
                        "exception_type": "ApiOverloadedError",
                        "exception_message": "API Error: Overloaded",
                    },
                }
            )
        )
        return HarborProcessResult(
            returncode=0,
            timed_out=False,
            log_path=kwargs["log_path"],
        )

    monkeypatch.setattr(runner_module.shutil, "which", lambda _command: "/bin/tool")
    monkeypatch.setattr(runner_module, "harbor_container_ids", lambda _task: frozenset())
    monkeypatch.setattr(runner_module, "run_harbor_process", completed_with_transient_trial)
    monkeypatch.setattr(runner_module, "_write_run_metadata", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner_module,
        "_cleanup_failure",
        lambda _request, _before, job_dir: cleaned.append(job_dir) or None,
    )

    with pytest.raises(
        TransientHarnessFailure,
        match="transient_harness:provider_http_5xx",
    ):
        run_experiment(request, repo_root=tmp_path)

    assert cleaned == [request.jobs_dir / request.name]


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


def test_hanging_docker_inspection_is_bounded(tmp_path: Path) -> None:
    timeouts: list[float] = []

    def hangs(command: list[str], **kwargs):
        timeouts.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(RuntimeError, match="cannot inspect Docker"):
        runner_module.harbor_container_ids(tmp_path, command_runner=hangs)

    assert timeouts == [runner_module.SUPPORT_COMMAND_TIMEOUT_SECONDS]


def test_cleanup_failure_is_secondary_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = RunRequest(
        task=task(tmp_path),
        agent="oracle",
        name="cleanup-secondary",
        jobs_dir=tmp_path / "runs",
    )
    monkeypatch.setattr(
        runner_module,
        "cleanup_new_harbor_containers",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
    )

    assert runner_module._cleanup_failure(
        request, frozenset(), request.jobs_dir / request.name
    ) == "cleanup_failed:TimeoutError"


def test_extra_instruction_path_is_forwarded_to_harbor(tmp_path: Path) -> None:
    """EXP-S03: the elicitation preamble must reach the harbor argv.

    `harbor run --extra-instruction-path <path>` appends an extra instruction file
    to the task instruction. A spec field that never reaches the runner is the
    defect class this repo keeps finding, so this asserts the argv, not the field.
    """
    preamble = tmp_path / "preamble.md"
    preamble.write_text("Think step by step.\n")
    request = RunRequest(
        task=task(tmp_path),
        agent="oracle",
        name="sample-with-preamble",
        jobs_dir=tmp_path / "runs",
        extra_instruction_path=preamble,
    )

    command = build_command(request)

    assert "--extra-instruction-path" in command
    assert command[command.index("--extra-instruction-path") + 1] == str(preamble)


def test_extra_instruction_flag_is_absent_when_unset(tmp_path: Path) -> None:
    """No preamble means no flag at all — never an empty-string argument."""
    command = build_command(
        RunRequest(
            task=task(tmp_path),
            agent="oracle",
            name="sample-no-preamble",
            jobs_dir=tmp_path / "runs",
        )
    )

    assert "--extra-instruction-path" not in command
    assert "" not in command


def test_existing_argv_order_is_unchanged_by_the_new_flag(tmp_path: Path) -> None:
    """Goldens and callers depend on the fixed prefix order; the flag only appends."""
    preamble = tmp_path / "preamble.md"
    preamble.write_text("x\n")
    task_dir = task(tmp_path)
    base = build_command(
        RunRequest(
            task=task_dir,
            agent="oracle",
            name="order-check",
            jobs_dir=tmp_path / "runs",
        )
    )
    with_preamble = build_command(
        RunRequest(
            task=task_dir,
            agent="oracle",
            name="order-check",
            jobs_dir=tmp_path / "runs",
            extra_instruction_path=preamble,
        )
    )

    assert with_preamble[: len(base)] == base


def test_antigravity_model_translation_for_harbor(tmp_path: Path) -> None:
    """Harbor requires provider/model format (e.g. google/gemini-3.7-flash).

    The lab tracks local CLI models as gemini-3.7-flash-high (matching `agy models`).
    build_command must translate this to the Harbor namespace when generating argv.
    """
    request = RunRequest(
        task=task(tmp_path),
        agent="antigravity-cli",
        model="gemini-3.7-flash-high",
        name="sample-agy-run",
        jobs_dir=tmp_path / "runs",
        allow_billable=True,
    )

    command = build_command(request)

    assert "--model" in command
    assert command[command.index("--model") + 1] == "google/gemini-3.7-flash"


def test_resolve_harbor_model_distinguishes_local_and_harbor_namespaces() -> None:
    """The local CLI namespace and the Harbor namespace must remain distinct."""
    from evallab.credentials import DEFAULT_AGENT_MODELS

    local_model = DEFAULT_AGENT_MODELS["antigravity-cli"]
    assert local_model == "gemini-3.7-flash-high"

    assert LOCAL_TO_HARBOR_MODEL[("antigravity-cli", local_model)] == "google/gemini-3.7-flash"
    harbor_model = resolve_harbor_model("antigravity-cli", local_model)
    assert harbor_model == "google/gemini-3.7-flash"
    assert harbor_model != local_model
    assert harbor_model.startswith("google/")
    assert not local_model.startswith("google/")

def test_resolve_harbor_model_variants_and_passthrough() -> None:
    """All Antigravity variants translate to Harbor format; unmapped models pass through."""
    assert (
        resolve_harbor_model("antigravity-cli", "gemini-3.7-flash-medium")
        == "google/gemini-3.7-flash"
    )
    assert (
        resolve_harbor_model("antigravity-cli", "gemini-3.7-flash-low")
        == "google/gemini-3.7-flash"
    )
    assert (
        resolve_harbor_model("antigravity-cli", "gemini-3.1-pro-high")
        == "google/gemini-3.1-pro"
    )
    assert (
        resolve_harbor_model("antigravity-cli", "claude-sonnet-4-6")
        == "google/claude-sonnet-4-6"
    )
    # Unmapped models pass through unchanged
    assert resolve_harbor_model("codex", "gpt-5.6-terra") == "gpt-5.6-terra"
    assert (
        resolve_harbor_model("claude-code", "anthropic/claude-fable-5")
        == "anthropic/claude-fable-5"
    )
    assert resolve_harbor_model("oracle", None) is None
