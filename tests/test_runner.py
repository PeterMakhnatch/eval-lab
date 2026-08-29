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
from evallab.harbor_network import HarborNetworkPolicy
from evallab.runner import (
    HARBOR_COMPOSE_CONFIG_LABEL,
    HARBOR_COMPOSE_PROJECT_LABEL,
    HARBOR_COMPOSE_WORKDIR_LABEL,
    LOCAL_TO_HARBOR_MODEL,
    ExecutionFailure,
    HarborProcessResult,
    RunRequest,
    TransientHarnessFailure,
    build_command,
    cleanup_new_harbor_containers,
    load_matrix,
    resolve_harbor_agent,
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


def no_network_task(tmp_path: Path) -> Path:
    """A package that requires network adaptation on a non-Linux host."""
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text(
        'schema_version = "1.4"\n'
        "\n"
        "[environment]\n"
        'network_mode = "no-network"\n'
        "\n"
        "[verifier.environment]\n"
        'network_mode = "no-network"\n'
    )
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


def test_antigravity_routes_through_repo_owned_capture_and_keeps_harbor_models(
    tmp_path: Path,
) -> None:
    request = RunRequest(
        task=task(tmp_path),
        agent="antigravity-cli",
        model="gemini-3.7-flash-high",
        name="agy-capture-test",
        jobs_dir=tmp_path / "runs",
        allow_billable=True,
    )
    command = build_command(request)
    assert resolve_harbor_agent("antigravity-cli") == (
        "evallab.harbor_antigravity:AntigravityCliCapture"
    )
    assert command[command.index("--agent") + 1] == (
        "evallab.harbor_antigravity:AntigravityCliCapture"
    )
    assert command[command.index("--model") + 1] == "google/gemini-3.7-flash-high"
    assert {
        resolve_harbor_model("antigravity-cli", model)
        for model in (
            "gemini-3.7-flash-low",
            "gemini-3.7-flash-medium",
            "gemini-3.7-flash-high",
        )
    } == {
        "google/gemini-3.7-flash-low",
        "google/gemini-3.7-flash-medium",
        "google/gemini-3.7-flash-high",
    }


def test_codex_routes_through_repo_owned_pinned_adapter(tmp_path: Path) -> None:
    request = RunRequest(
        task=task(tmp_path),
        agent="codex",
        model="gpt-5.6-luna",
        name="codex-pinned-test",
        jobs_dir=tmp_path / "runs",
        allow_billable=True,
    )

    command = build_command(request)

    assert resolve_harbor_agent("codex") == "evallab.harbor_codex:PinnedCodex"
    assert command[command.index("--agent") + 1] == "evallab.harbor_codex:PinnedCodex"
    assert command[command.index("--model") + 1] == "gpt-5.6-luna"


def test_deepseek_routes_through_pinned_bounded_mini_swe_adapter(
    tmp_path: Path,
) -> None:
    request = RunRequest(
        task=task(tmp_path),
        agent="mini-swe-agent",
        model="deepseek/deepseek-v4-flash",
        name="deepseek-pinned-test",
        jobs_dir=tmp_path / "runs",
        allow_billable=True,
    )

    command = build_command(request)

    assert resolve_harbor_agent("mini-swe-agent") == (
        "evallab.harbor_deepseek:SecretSafeDeepSeekMiniSweAgent"
    )
    assert command[command.index("--agent") + 1] == resolve_harbor_agent("mini-swe-agent")
    assert command[command.index("--model") + 1] == "deepseek/deepseek-v4-flash"
    assert command[command.index("--n-concurrent-agents") + 1] == "1"
    assert command[command.index("--n-tasks") + 1] == "1"
    assert command[command.index("--max-retries") + 1] == "0"
    assert "cost_limit=2.5" in command
    assert "max_tokens=8192" in command


def test_deepseek_campaign_overrides_agent_cost_and_output_ceilings(
    tmp_path: Path,
) -> None:
    command = build_command(
        RunRequest(
            task=task(tmp_path),
            agent="mini-swe-agent",
            model="deepseek/deepseek-v4-flash",
            name="deepseek-campaign-bounds",
            jobs_dir=tmp_path / "runs",
            allow_billable=True,
            max_output_tokens=1234,
            cost_limit_usd=0.75,
        )
    )

    assert "cost_limit=0.75" in command
    assert "max_tokens=1234" in command


def test_repo_owned_agent_adds_src_to_harbor_host_pythonpath(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    source_root.mkdir()
    log_path = tmp_path / "harbor.log"
    import_path = resolve_harbor_agent("antigravity-cli")
    result = run_harbor_process(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ.get('PYTHONPATH', ''))",
            import_path,
        ],
        cwd=tmp_path,
        timeout_seconds=5,
        log_path=log_path,
    )

    assert result.returncode == 0
    pythonpath = log_path.read_text().strip().split(os.pathsep)
    assert str(source_root) in pythonpath


def test_deepseek_credentials_reach_only_the_repo_owned_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "must-never-enter-log"
    monkeypatch.setenv("MSWEA_API_KEY", secret)
    script = (
        "import os; "
        "print('deepseek=' + ('set' if os.getenv('DEEPSEEK_API_KEY') else 'unset')); "
        "print('mswea=' + ('set' if os.getenv('MSWEA_API_KEY') else 'unset'))"
    )

    deepseek_log = tmp_path / "deepseek.log"
    deepseek = run_harbor_process(
        [
            sys.executable,
            "-c",
            script,
            resolve_harbor_agent("mini-swe-agent"),
        ],
        cwd=tmp_path,
        timeout_seconds=5,
        log_path=deepseek_log,
    )
    control_log = tmp_path / "control.log"
    control = run_harbor_process(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        timeout_seconds=5,
        log_path=control_log,
    )

    assert deepseek.returncode == 0
    assert deepseek_log.read_text().splitlines() == ["deepseek=set", "mswea=set"]
    assert secret not in deepseek_log.read_text()
    assert control.returncode == 0
    assert control_log.read_text().splitlines() == ["deepseek=unset", "mswea=unset"]

def test_harbor_log_redacts_deepseek_secret_across_stream_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "stream-boundary-secret"
    monkeypatch.setenv("MSWEA_API_KEY", secret)
    script = (
        "import os,sys,time; "
        "value=os.environ['MSWEA_API_KEY']; "
        "sys.stdout.write(value[:7]); sys.stdout.flush(); "
        "time.sleep(0.05); "
        "sys.stdout.write(value[7:] + '\\n'); sys.stdout.flush()"
    )
    log_path = tmp_path / "redacted.log"

    result = run_harbor_process(
        [
            sys.executable,
            "-c",
            script,
            resolve_harbor_agent("mini-swe-agent"),
        ],
        cwd=tmp_path,
        timeout_seconds=5,
        log_path=log_path,
    )

    assert result.returncode == 0
    assert log_path.read_text(encoding="utf-8") == "<redacted>\n"
    assert secret not in log_path.read_text(encoding="utf-8")


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
        "AGY_FORCE_AUTH_JSON": "1",
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
        "AGY_FORCE_AUTH_JSON": "1",
        "CLAUDE_FORCE_OAUTH": "1",
        "CODEX_FORCE_AUTH_JSON": "1",
        "REWARDKIT_FORCE_OAUTH": "1",
    }


def test_subscription_command_routes_credential_transports(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wrapper = repo / "scripts/with-claude-auth"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\n")
    overlay = repo / "containers/deepseek-v4-flash-secret.compose.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("services: {}\n")
    proxy_script = repo / "containers/deepseek_secret_proxy.py"
    proxy_script.write_text("#!/usr/bin/env python3\n")
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
    deepseek = RunRequest(
        task=claude.task,
        agent="mini-swe-agent",
        model="deepseek/deepseek-v4-flash",
        name="deepseek-api",
        jobs_dir=tmp_path / "runs",
        allow_billable=True,
    )

    assert subscription_command(claude, harbor_command, repo_root=repo) == [
        str(wrapper.resolve()),
        *harbor_command,
    ]
    assert subscription_command(codex, harbor_command, repo_root=repo) == harbor_command
    assert subscription_command(deepseek, harbor_command, repo_root=repo) == [
        *harbor_command,
        "--extra-docker-compose",
        str(overlay.resolve()),
    ]


def test_subscription_command_refuses_when_deepseek_proxy_is_missing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    overlay = repo / "containers/deepseek-v4-flash-secret.compose.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("services: {}\n")
    deepseek = RunRequest(
        task=task(tmp_path),
        agent="mini-swe-agent",
        model="deepseek/deepseek-v4-flash",
        name="deepseek-api",
        jobs_dir=tmp_path / "runs",
        allow_billable=True,
    )
    with pytest.raises(RuntimeError, match="DeepSeek secret proxy is missing"):
        subscription_command(deepseek, ["harbor", "run"], repo_root=repo)


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

    assert transient_provider_exception(provider_failure) == ("transient_harness:provider_http_5xx")
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

    assert transient_provider_exception(result) == ("transient_harness:provider_http_5xx")


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
    state = json.loads(runner_module.executor_state_path(request).read_text())
    assert state["status"] == "failed"


@pytest.mark.parametrize("note_write_fails", [False, True])
def test_completed_run_survives_evidence_archive_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    note_write_fails: bool,
) -> None:
    request = RunRequest(
        task=task(tmp_path),
        agent="oracle",
        name="completed-with-archive-failure",
        jobs_dir=tmp_path / "runs",
    )

    def completed(*_args, **kwargs) -> HarborProcessResult:
        kwargs["job_dir"].mkdir(parents=True)
        return HarborProcessResult(
            returncode=0,
            timed_out=False,
            log_path=kwargs["log_path"],
        )

    def archive_fails(*_args, **_kwargs) -> None:
        raise OSError("evidence store unavailable")

    original_write_text = Path.write_text

    def write_text(path: Path, data: str, *args, **kwargs) -> int:
        if note_write_fails and path.name == "evidence-archive-error.txt":
            raise OSError("job directory became read-only")
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setenv("EVALLAB_EVIDENCE_STORE_ROOT", str(tmp_path / "evidence"))
    monkeypatch.setattr(runner_module.shutil, "which", lambda _command: "/bin/tool")
    monkeypatch.setattr(runner_module, "harbor_container_ids", lambda _task: frozenset())
    monkeypatch.setattr(runner_module, "run_harbor_process", completed)
    monkeypatch.setattr(runner_module, "_write_run_metadata", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner_module,
        "load_job",
        lambda _job_dir: type("CompletedJob", (), {"id": "job-123"})(),
    )
    monkeypatch.setattr("evallab.evidence_store.archive_evidence", archive_fails)
    monkeypatch.setattr(Path, "write_text", write_text)

    job_dir = run_experiment(request, repo_root=tmp_path)

    assert job_dir == request.jobs_dir / request.name
    state = json.loads(runner_module.executor_state_path(request).read_text())
    assert state["status"] == "completed"
    note = job_dir / "evidence-archive-error.txt"
    if note_write_fails:
        assert not note.exists()
    else:
        assert note.read_text() == "OSError: evidence store unavailable\n"


def test_secret_scan_precedes_generic_evidence_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "provider-key-must-not-reach-cas"
    request = RunRequest(
        task=task(tmp_path),
        agent="oracle",
        name="secret-before-archive",
        jobs_dir=tmp_path / "runs",
    )
    archived: list[Path] = []

    def completed(*_args, **kwargs) -> HarborProcessResult:
        job_dir = kwargs["job_dir"]
        job_dir.mkdir(parents=True)
        (job_dir / "leak.txt").write_text(secret, encoding="utf-8")
        return HarborProcessResult(
            returncode=0,
            timed_out=False,
            log_path=kwargs["log_path"],
        )

    monkeypatch.setenv("MSWEA_API_KEY", secret)
    monkeypatch.setenv("EVALLAB_EVIDENCE_STORE_ROOT", str(tmp_path / "evidence"))
    monkeypatch.setattr(runner_module.shutil, "which", lambda _command: "/bin/tool")
    monkeypatch.setattr(runner_module, "harbor_container_ids", lambda _task: frozenset())
    monkeypatch.setattr(runner_module, "run_harbor_process", completed)
    monkeypatch.setattr(runner_module, "_write_run_metadata", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner_module,
        "load_job",
        lambda _job_dir: type("CompletedJob", (), {"id": "job-123"})(),
    )
    monkeypatch.setattr(
        "evallab.evidence_store.archive_evidence",
        lambda job_dir, *_args, **_kwargs: archived.append(job_dir),
    )

    with pytest.raises(ExecutionFailure, match="credential material reached"):
        run_experiment(request, repo_root=tmp_path)

    assert archived == []
    assert not (request.jobs_dir / request.name / "leak.txt").exists()


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
    assert (
        count_consecutive_harness_failures(
            ["AgentRunError", "transient_harness", "VerifierError", None, "OldError"]
        )
        == 2
    )


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

    assert (
        runner_module._cleanup_failure(request, frozenset(), request.jobs_dir / request.name)
        == "cleanup_failed:TimeoutError"
    )


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
    assert command[command.index("--model") + 1] == "google/gemini-3.7-flash-high"


def test_resolve_harbor_model_distinguishes_local_and_harbor_namespaces() -> None:
    """The local CLI namespace and the Harbor namespace must remain distinct."""
    from evallab.credentials import DEFAULT_AGENT_MODELS

    local_model = DEFAULT_AGENT_MODELS["antigravity-cli"]
    assert local_model == "gemini-3.7-flash-high"

    assert LOCAL_TO_HARBOR_MODEL[("antigravity-cli", local_model)] == (
        "google/gemini-3.7-flash-high"
    )
    harbor_model = resolve_harbor_model("antigravity-cli", local_model)
    assert harbor_model == "google/gemini-3.7-flash-high"
    assert harbor_model != local_model
    assert harbor_model.startswith("google/")
    assert not local_model.startswith("google/")


def test_antigravity_model_keeps_the_thinking_level_in_the_id(
    tmp_path: Path,
) -> None:
    """`agy` names its models with the level baked in, and rejects a bare id.

    A live containerised trial on 2026-08-19 settled this: `google/gemini-3.7-flash`
    made `agy` exit non-zero ("requires --effort"), while
    `google/gemini-3.7-flash-high` completed with primary reward 1.0.
    """
    command = build_command(
        RunRequest(
            task=task(tmp_path),
            agent="antigravity-cli",
            model="gemini-3.7-flash-high",
            name="sample-agy-effort",
            jobs_dir=tmp_path / "runs",
            allow_billable=True,
        )
    )

    assert command[command.index("--model") + 1] == "google/gemini-3.7-flash-high"


def test_antigravity_never_sends_reasoning_effort_as_an_agent_kwarg(
    tmp_path: Path,
) -> None:
    """Harbor's antigravity adapter declares no `--effort` flag, so the kwarg never
    reaches the Go CLI - it only writes a settings file the legacy CLI reads.
    Sending it produced an empty `--effort` and a failed trial."""
    command = build_command(
        RunRequest(
            task=task(tmp_path),
            agent="antigravity-cli",
            model="gemini-3.7-flash-high",
            name="sample-agy-nokwarg",
            jobs_dir=tmp_path / "runs",
            allow_billable=True,
        )
    )

    assert "--agent-kwarg" not in command
    assert not any(arg.startswith("reasoning_effort") for arg in command)


def test_resolve_harbor_model_variants_and_passthrough() -> None:
    """All Antigravity variants translate to Harbor format; unmapped models pass through."""
    assert (
        resolve_harbor_model("antigravity-cli", "gemini-3.7-flash-medium")
        == "google/gemini-3.7-flash-medium"
    )
    assert (
        resolve_harbor_model("antigravity-cli", "gemini-3.7-flash-low")
        == "google/gemini-3.7-flash-low"
    )
    assert (
        resolve_harbor_model("antigravity-cli", "gemini-3.1-pro-high")
        == "google/gemini-3.1-pro-high"
    )
    assert (
        resolve_harbor_model("antigravity-cli", "claude-sonnet-4-6") == "google/claude-sonnet-4-6"
    )
    # Unmapped models pass through unchanged
    assert resolve_harbor_model("codex", "gpt-5.6-terra") == "gpt-5.6-terra"
    assert (
        resolve_harbor_model("claude-code", "anthropic/claude-fable-5")
        == "anthropic/claude-fable-5"
    )
    assert resolve_harbor_model("oracle", None) is None


def _darwin_public_policy() -> HarborNetworkPolicy:
    """Return the policy used to force staging on any test host."""
    return HarborNetworkPolicy(
        network_mode="public",
        network_isolation_enforced=False,
        network_isolation_reason="darwin-docker-cannot-enforce-no-network",
    )


def test_staging_cleaned_up_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful run removes the temporary copy and keeps the metadata."""
    request = RunRequest(
        task=no_network_task(tmp_path),
        agent="oracle",
        name="staging-success",
        jobs_dir=tmp_path / "runs",
    )

    def completed(*_args, **kwargs) -> HarborProcessResult:
        kwargs["job_dir"].mkdir(parents=True)
        return HarborProcessResult(
            returncode=0,
            timed_out=False,
            log_path=kwargs["log_path"],
        )

    monkeypatch.setattr(
        "evallab.harbor_network.host_harbor_network_policy",
        _darwin_public_policy,
    )
    monkeypatch.setattr(
        runner_module,
        "load_job",
        lambda _job_dir: type("CompletedJob", (), {"id": "job-123"})(),
    )
    monkeypatch.setattr(runner_module.shutil, "which", lambda _command: "/bin/tool")
    monkeypatch.setattr(runner_module, "harbor_container_ids", lambda _task: frozenset())
    monkeypatch.setattr(runner_module, "run_harbor_process", completed)
    monkeypatch.setattr(runner_module, "tool_version", lambda _command: "0.0")
    monkeypatch.setattr(
        runner_module,
        "git_state",
        lambda _root: {"commit": None, "dirty": None},
    )

    job_dir = run_experiment(request, repo_root=tmp_path)

    staging_dir = request.jobs_dir / ".exec-stage" / request.name
    assert not staging_dir.exists()
    network_adaptation_path = runner_module._network_adaptation_path(request)
    assert network_adaptation_path.is_file()
    manifest = json.loads(network_adaptation_path.read_text())
    assert manifest["schema_version"] == "1.0"
    assert manifest["network_adaptation"]["effective_verifier_network"] == "public"
    metadata = json.loads((job_dir / "lab-metadata.json").read_text())
    assert metadata["network_adaptation"]["effective_verifier_network"] == "public"


def test_staging_cleaned_up_after_harbor_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero exit removes the copy after container cleanup and keeps metadata."""
    request = RunRequest(
        task=no_network_task(tmp_path),
        agent="oracle",
        name="staging-failure",
        jobs_dir=tmp_path / "runs",
    )
    cleaned: list[Path] = []

    def failed(*_args, **kwargs) -> HarborProcessResult:
        kwargs["job_dir"].mkdir(parents=True)
        return HarborProcessResult(
            returncode=1,
            timed_out=False,
            log_path=kwargs["log_path"],
        )

    monkeypatch.setattr(
        "evallab.harbor_network.host_harbor_network_policy",
        _darwin_public_policy,
    )
    monkeypatch.setattr(runner_module.shutil, "which", lambda _command: "/bin/tool")
    monkeypatch.setattr(runner_module, "harbor_container_ids", lambda _task: frozenset())
    monkeypatch.setattr(runner_module, "run_harbor_process", failed)
    monkeypatch.setattr(
        runner_module,
        "_cleanup_failure",
        lambda _request, _before, job_dir: cleaned.append(job_dir) or None,
    )
    monkeypatch.setattr(runner_module, "tool_version", lambda _command: "0.0")
    monkeypatch.setattr(
        runner_module,
        "git_state",
        lambda _root: {"commit": None, "dirty": None},
    )

    with pytest.raises(ExecutionFailure, match="Harbor exited with 1"):
        run_experiment(request, repo_root=tmp_path)

    assert cleaned == [request.jobs_dir / request.name]
    staging_dir = request.jobs_dir / ".exec-stage" / request.name
    assert not staging_dir.exists()
    network_adaptation_path = runner_module._network_adaptation_path(request)
    assert network_adaptation_path.is_file()
    manifest = json.loads(network_adaptation_path.read_text())
    assert manifest["network_adaptation"]["network_isolation_enforced"] is False
    assert manifest["network_adaptation"]["network_isolation_reason"] is not None


def test_staging_cleaned_up_after_harbor_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An early runtime exception still removes the copy and keeps metadata."""
    request = RunRequest(
        task=no_network_task(tmp_path),
        agent="oracle",
        name="staging-exception",
        jobs_dir=tmp_path / "runs",
    )

    def boom(*_args, **_kwargs) -> None:
        raise RuntimeError("harbor build failed")

    monkeypatch.setattr(
        "evallab.harbor_network.host_harbor_network_policy",
        _darwin_public_policy,
    )
    monkeypatch.setattr(runner_module.shutil, "which", lambda _command: "/bin/tool")
    monkeypatch.setattr(runner_module, "harbor_container_ids", lambda _task: frozenset())
    monkeypatch.setattr(runner_module, "run_harbor_process", boom)

    with pytest.raises(RuntimeError, match="harbor build failed"):
        run_experiment(request, repo_root=tmp_path)

    staging_dir = request.jobs_dir / ".exec-stage" / request.name
    assert not staging_dir.exists()
    network_adaptation_path = runner_module._network_adaptation_path(request)
    assert network_adaptation_path.is_file()
    assert (
        json.loads(network_adaptation_path.read_text())["network_adaptation"][
            "effective_verifier_network"
        ]
        == "public"
    )


def test_staging_cleaned_up_after_task_toml_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure writing the adapted task.toml into the copy still cleans the copy."""
    request = RunRequest(
        task=no_network_task(tmp_path),
        agent="oracle",
        name="staging-toml-fail",
        jobs_dir=tmp_path / "runs",
    )

    original_write_text = Path.write_text

    def write_text(self, data, *args, **kwargs):
        if ".exec-stage" in str(self.parent) and self.name == "task.toml":
            raise OSError("disk full")
        return original_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(
        "evallab.harbor_network.host_harbor_network_policy",
        _darwin_public_policy,
    )
    monkeypatch.setattr(runner_module.shutil, "which", lambda _command: "/bin/tool")
    monkeypatch.setattr(Path, "write_text", write_text)

    with pytest.raises(OSError, match="disk full"):
        run_experiment(request, repo_root=tmp_path)

    staging_dir = request.jobs_dir / ".exec-stage" / request.name
    assert not staging_dir.exists()
    source_toml = (request.task / "task.toml").read_text()
    assert 'network_mode = "no-network"' in source_toml
    assert 'network_mode = "public"' not in source_toml


def test_staging_cleaned_up_after_network_adaptation_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure writing durable network-adaptation metadata still cleans the copy."""
    request = RunRequest(
        task=no_network_task(tmp_path),
        agent="oracle",
        name="staging-meta-fail",
        jobs_dir=tmp_path / "runs",
    )

    original_write_text = Path.write_text

    def write_text(self, data, *args, **kwargs):
        if ".executor" in str(self.parent) and "network-adaptation" in self.name:
            raise OSError("disk full")
        return original_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(
        "evallab.harbor_network.host_harbor_network_policy",
        _darwin_public_policy,
    )
    monkeypatch.setattr(runner_module.shutil, "which", lambda _command: "/bin/tool")
    monkeypatch.setattr(Path, "write_text", write_text)

    with pytest.raises(OSError, match="disk full"):
        run_experiment(request, repo_root=tmp_path)

    staging_dir = request.jobs_dir / ".exec-stage" / request.name
    assert not staging_dir.exists()
    network_adaptation_path = runner_module._network_adaptation_path(request)
    assert not network_adaptation_path.exists()
    tmp_path_file = network_adaptation_path.with_name(f".{network_adaptation_path.name}.tmp")
    assert not tmp_path_file.exists()
    source_toml = (request.task / "task.toml").read_text()
    assert 'network_mode = "no-network"' in source_toml
    assert 'network_mode = "public"' not in source_toml
