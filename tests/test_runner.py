import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import evallab.evidence_store as evidence_store_module
import evallab.runner as runner_module
from evallab.cli import load_local_env
from evallab.database import _exception_type, count_consecutive_harness_failures
from evallab.execution_contracts import (
    ZAI_PROXY_CAPABILITY_ENV,
    ZAI_SECRET_FILE_ENV,
    ProxyTrialLimits,
    materialize_zai_secret_file,
    read_owner_secret_file,
)
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
    provider_failover_exception,
    provider_failover_reason,
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


@pytest.fixture(autouse=True)
def _configured_settlement_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep process mocks on the real mandatory CAS and typed identity contract."""

    monkeypatch.setenv("EVALLAB_EVIDENCE_STORE_ROOT", str(tmp_path / "evidence-cas"))
    executable = tmp_path / "harbor"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    device, inode, size, mtime_ns, digest = runner_module._executable_snapshot(executable)
    monkeypatch.setattr(
        runner_module,
        "resolve_harbor_runtime_identity",
        lambda _repo_root: runner_module.HarborRuntimeIdentity(
            declared_version="0.22.0",
            actual_version="0.22.0",
            executable_path=executable,
            executable_digest=digest,
            executable_device=device,
            executable_inode=inode,
            executable_size=size,
            executable_mtime_ns=mtime_ns,
        ),
    )


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
        proxy_attempt_id="credential-routing-trial",
        proxy_limits=ProxyTrialLimits(
            max_requests=2,
            max_input_tokens=256,
            max_output_tokens=16,
            max_total_tokens=272,
            max_cost_micros=1_000_000,
        ),
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
        proxy_attempt_id="stream-redaction-trial",
        proxy_limits=ProxyTrialLimits(
            max_requests=2,
            max_input_tokens=256,
            max_output_tokens=16,
            max_total_tokens=272,
            max_cost_micros=1_000_000,
        ),
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


def test_executor_process_honors_campaign_cancel_marker(tmp_path: Path) -> None:
    generation = "a" * 32
    lease = tmp_path / "campaign-spec.lease"
    lease.write_text(
        json.dumps({"lease_generation": generation}) + "\n",
        encoding="utf-8",
    )
    marker = lease.with_name(f"{lease.name}.cancel.{generation}")
    timer = threading.Timer(
        0.05,
        lambda: marker.write_text(
            json.dumps({"lease_generation": generation}) + "\n",
            encoding="utf-8",
        ),
    )
    timer.start()
    started = time.monotonic()

    result = run_harbor_process(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        timeout_seconds=5,
        log_path=tmp_path / "cancelled.log",
        lease_path=lease,
        lease_generation=generation,
        heartbeat_interval_seconds=0.01,
    )
    timer.join()

    assert result.timed_out is False
    assert result.returncode != 0
    assert time.monotonic() - started < 2


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


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("account_balance_exhausted", "provider_failover:balance_exhausted"),
        ("credit_balance_exhausted", "provider_failover:credits_exhausted"),
        ("insufficient_quota", "provider_failover:account_quota_exhausted"),
        ("subscription_quota_exhausted", "provider_failover:account_quota_exhausted"),
        ("auth_quota_exhausted", "provider_failover:auth_quota_exhausted"),
        ("rate_limit_exceeded", None),
        ("HTTP 401 Unauthorized", None),
        ("account balance is too low; please top up", None),
    ],
)
def test_provider_failover_requires_exact_structured_capacity_code(
    code: str,
    reason: str | None,
) -> None:
    assert provider_failover_reason(code) == reason


def test_429_rate_limit_retries_same_provider_but_insufficient_quota_can_fail_over() -> None:
    rate_limit = {
        "exception_info": {
            "exception_type": "ApiRateLimitError",
            "code": "rate_limit_exceeded",
            "message": "provider returned status code 429",
        }
    }
    insufficient_quota = {
        "exception_info": {
            "exception_type": "ApiRateLimitError",
            "code": "insufficient_quota",
            "message": "provider returned status code 429",
        }
    }
    message_only = {
        "exception_info": {
            "exception_type": "ApiRateLimitError",
            "message": "429 insufficient_quota",
        }
    }

    assert provider_failover_exception(rate_limit) is None
    assert transient_provider_exception(rate_limit) == "transient_harness:provider_http_429"
    assert provider_failover_exception(insufficient_quota) == (
        "provider_failover:account_quota_exhausted"
    )
    assert transient_provider_exception(insufficient_quota) == (
        "transient_harness:provider_http_429"
    )
    assert provider_failover_exception(message_only) is None
    assert transient_provider_exception(message_only) == "transient_harness:provider_http_429"


def test_provider_failover_rejects_task_structured_capacity_code() -> None:
    task_failure = {
        "exception_info": {
            "exception_type": "TaskValidationError",
            "code": "insufficient_quota",
        }
    }

    assert provider_failover_exception(task_failure) is None




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


def test_completed_run_refuses_unsettled_evidence_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    monkeypatch.setattr(runner_module, "harbor_container_ids", lambda _task: frozenset())
    monkeypatch.setattr(runner_module, "run_harbor_process", completed)
    monkeypatch.setattr(runner_module, "_write_run_metadata", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner_module,
        "load_job",
        lambda _job_dir: type("CompletedJob", (), {"id": "job-123"})(),
    )
    monkeypatch.setattr(runner_module, "archive_evidence", archive_fails)

    with pytest.raises(ExecutionFailure, match="could not be archived and reopened"):
        run_experiment(request, repo_root=tmp_path)

    state = json.loads(runner_module.executor_state_path(request).read_text())
    assert state["status"] == "failed"
    assert not (request.jobs_dir / request.name / "evidence-archive-error.txt").exists()


def test_missing_cas_configuration_refuses_before_harbor_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = RunRequest(
        task=task(tmp_path),
        agent="oracle",
        name="missing-cas-config",
        jobs_dir=tmp_path / "runs",
    )
    launched = False

    def must_not_launch(*_args, **_kwargs) -> HarborProcessResult:
        nonlocal launched
        launched = True
        raise AssertionError("Harbor must not launch without configured CAS")

    monkeypatch.delenv("EVALLAB_EVIDENCE_STORE_ROOT")
    monkeypatch.setattr(runner_module, "run_harbor_process", must_not_launch)

    with pytest.raises(ExecutionFailure) as exc_info:
        run_experiment(request, repo_root=tmp_path)

    assert exc_info.value.reason_code == "evidence_cas_unconfigured"
    assert launched is False
    assert not (request.jobs_dir / request.name).exists()


def test_harbor_version_mismatch_refuses_before_harbor_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = RunRequest(
        task=task(tmp_path),
        agent="oracle",
        name="harbor-version-mismatch",
        jobs_dir=tmp_path / "runs",
    )
    launched = False

    def mismatch(_repo_root: Path) -> runner_module.HarborRuntimeIdentity:
        raise ExecutionFailure("harbor_version_mismatch", "0.21.0 does not match 0.22.0")

    def must_not_launch(*_args, **_kwargs) -> HarborProcessResult:
        nonlocal launched
        launched = True
        raise AssertionError("Harbor must not launch when identity drifts")

    monkeypatch.setattr(runner_module, "resolve_harbor_runtime_identity", mismatch)
    monkeypatch.setattr(runner_module, "run_harbor_process", must_not_launch)

    with pytest.raises(ExecutionFailure) as exc_info:
        run_experiment(request, repo_root=tmp_path)

    assert exc_info.value.reason_code == "harbor_version_mismatch"
    assert launched is False
    assert not (request.jobs_dir / request.name).exists()


@pytest.mark.parametrize(
    ("reported_version", "reason_code"),
    [
        ("0.21.0", "harbor_version_mismatch"),
        ("harbor 0.22.0", "harbor_identity_unavailable"),
        ("not harbor 0.22.0 python 3.12.0", "harbor_identity_unavailable"),
        ("0.22.0 3.12.0", "harbor_identity_unavailable"),
        ("harbor unknown", "harbor_identity_unavailable"),
    ],
)
def test_runtime_identity_refuses_version_drift_and_unparseable_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reported_version: str,
    reason_code: str,
) -> None:
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "harbor"\nversion = "0.22.0"\n',
        encoding="utf-8",
    )
    monkeypatch.undo()
    executable = tmp_path / "harbor"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr(runner_module.shutil, "which", lambda _command: str(executable))
    monkeypatch.setattr(
        runner_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["harbor", "--version"],
            returncode=0,
            stdout=reported_version,
            stderr="",
        ),
    )

    with pytest.raises(ExecutionFailure) as exc_info:
        runner_module.resolve_harbor_runtime_identity(tmp_path)

    assert exc_info.value.reason_code == reason_code


def test_settlement_freezes_source_and_returns_only_cas_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "result.json").write_text('{"finished": true}\n', encoding="utf-8")
    store_root = tmp_path / "evidence-cas"

    locator, settled = runner_module._settle_completed_job(
        job_dir,
        store_root=store_root,
        record_id="job-123",
    )
    record_bytes = evidence_store_module.read_record(
        store_root,
        kind=locator.kind,
        record_id=locator.record_id,
    )
    record = json.loads(record_bytes)
    assert not job_dir.exists()
    assert record["schema_version"] == 2
    assert set(record).isdisjoint({"source_path", "blob_path"})
    assert record["record_id"] == "job-123"
    assert record["kind"] == "job"
    assert record["content_digest"] == settled.content_digest
    assert record["archive_digest"] == settled.archive_digest
    assert record["uri"] == settled.uri
    assert locator.expected_record_digest == settled.record_digest
    assert locator.expected_content_digest == settled.content_digest
    assert not hasattr(settled, "manifest_path")
    assert not hasattr(settled, "blob_path")

    original_restore = evidence_store_module.restore_evidence

    def restore_wrong_content(*args, **kwargs) -> Path:
        restored = original_restore(*args, **kwargs)
        (restored / "result.json").write_text('{"finished": false}\n', encoding="utf-8")
        return restored

    second_job = tmp_path / "second-job"
    second_job.mkdir()
    (second_job / "result.json").write_text('{"finished": true}\n', encoding="utf-8")
    monkeypatch.setattr(evidence_store_module, "restore_evidence", restore_wrong_content)
    with pytest.raises(ExecutionFailure) as exc_info:
        runner_module._settle_completed_job(
            second_job,
            store_root=store_root,
            record_id="job-456",
        )

    assert exc_info.value.reason_code == "evidence_cas_unsettled"


def _completed_run_request(tmp_path: Path, name: str) -> RunRequest:
    return RunRequest(
        task=task(tmp_path),
        agent="oracle",
        name=name,
        jobs_dir=tmp_path / "runs",
    )


def _install_completed_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def completed(*_args, **kwargs) -> HarborProcessResult:
        kwargs["job_dir"].mkdir(parents=True)
        return HarborProcessResult(
            returncode=0,
            timed_out=False,
            log_path=kwargs["log_path"],
        )

    monkeypatch.setattr(runner_module, "harbor_container_ids", lambda _task: frozenset())
    monkeypatch.setattr(runner_module, "run_harbor_process", completed)
    monkeypatch.setattr(runner_module, "_write_run_metadata", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner_module,
        "load_job",
        lambda _job_dir: type("CompletedJob", (), {"id": "job-123"})(),
    )


@pytest.mark.parametrize("record_bytes", [b"[]", b"\xff"])
def test_unreadable_reopened_record_fails_terminally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_bytes: bytes,
) -> None:
    request = _completed_run_request(tmp_path, "malformed-reopened-record")
    _install_completed_run(monkeypatch)
    original_archive = runner_module.archive_evidence

    def archive_with_bad_record(*args, **kwargs):
        archive = original_archive(*args, **kwargs)
        store_root = Path(args[1])
        record_path = store_root / "records" / archive.kind / f"{archive.record_id}.json"
        record_path.write_bytes(record_bytes)
        return archive

    monkeypatch.setattr(runner_module, "archive_evidence", archive_with_bad_record)

    with pytest.raises(ExecutionFailure) as exc_info:
        run_experiment(request, repo_root=tmp_path)

    assert exc_info.value.reason_code == "evidence_cas_unsettled"
    state = json.loads(runner_module.executor_state_path(request).read_text())
    assert state["status"] == "failed"


def test_unexpected_settlement_exception_fails_terminally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _completed_run_request(tmp_path, "unexpected-settlement-error")
    _install_completed_run(monkeypatch)

    def unexpected(*_args, **_kwargs):
        raise RuntimeError("injected settlement programmer error")

    monkeypatch.setattr(runner_module, "_settle_completed_job", unexpected)

    with pytest.raises(RuntimeError, match="injected settlement programmer error"):
        run_experiment(request, repo_root=tmp_path)

    state = json.loads(runner_module.executor_state_path(request).read_text())
    assert state["status"] == "failed"


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


def _cas_record_path(store: Path, archive) -> Path:
    return store / "records" / archive.kind / f"{archive.record_id}.json"


def _cas_blob_path(store: Path, archive) -> Path:
    digest = archive.content_digest.removeprefix("sha256:")
    return store / "blobs" / "sha256" / digest[:2] / f"{digest}.tar.gz"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 1),
        ("schema_version", True),
        ("blob_path", "blobs/sha256/00/not-canonical.tar.gz"),
        ("file_count", 999),
        ("uncompressed_bytes", 999),
        ("source_path", "relative/job"),
    ],
)
def test_canonical_reopen_refuses_complete_record_tampering(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    source = tmp_path / "job"
    source.mkdir()
    (source / "result.json").write_text('{"finished": true}\n', encoding="utf-8")
    store = tmp_path / "cas"
    archive = evidence_store_module.archive_evidence(
        source,
        store,
        record_id="job-123",
        kind="job",
    )
    record_path = _cas_record_path(store, archive)
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    payload[field] = value
    reopened, reopened_bytes = evidence_store_module.reopen_evidence_archive(
        store,
        kind="job",
        record_id="job-123",
        expected_record_digest=archive.record_digest,
        expected_content_digest=archive.content_digest,
    )
    assert reopened == archive
    assert reopened_bytes == record_path.read_bytes()
    assert not hasattr(reopened, "manifest_path")
    assert not hasattr(reopened, "blob_path")
    record_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        evidence_store_module.reopen_evidence_archive(
            store,
            kind="job",
            record_id="job-123",
            expected_record_digest=archive.record_digest,
            expected_content_digest=archive.content_digest,
        )


@pytest.mark.parametrize("tampered_bytes", [b" ", b'{"kind":"job"}\n'])
def test_canonical_reopen_refuses_noncanonical_record_bytes(
    tmp_path: Path,
    tampered_bytes: bytes,
) -> None:
    source = tmp_path / "job"
    source.mkdir()
    (source / "result.json").write_text('{"finished": true}\n', encoding="utf-8")
    store = tmp_path / "cas"
    archive = evidence_store_module.archive_evidence(
        source,
        store,
        record_id="job-123",
        kind="job",
    )
    record_path = _cas_record_path(store, archive)
    record_path.write_bytes(tampered_bytes + record_path.read_bytes())

    with pytest.raises(ValueError):
        evidence_store_module.reopen_evidence_archive(
            store,
            kind="job",
            record_id="job-123",
            expected_record_digest=archive.record_digest,
            expected_content_digest=archive.content_digest,
        )


def test_reopen_requires_independent_content_identity(tmp_path: Path) -> None:
    source = tmp_path / "job"
    source.mkdir()
    (source / "result.json").write_text('{"finished": true}\n', encoding="utf-8")
    store = tmp_path / "cas"
    archive = evidence_store_module.archive_evidence(
        source,
        store,
        record_id="job-123",
        kind="job",
    )

    with pytest.raises(ValueError, match="content digest mismatch"):
        evidence_store_module.reopen_evidence_archive(
            store,
            kind="job",
            record_id="job-123",
            expected_record_digest=archive.record_digest,
            expected_content_digest="sha256:" + "0" * 64,
        )


@pytest.mark.parametrize("target", ["record", "archive"])
def test_reopen_returns_captured_identity_when_paths_change_during_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    source = tmp_path / "job"
    source.mkdir()
    (source / "result.json").write_text('{"finished": true}\n', encoding="utf-8")
    store = tmp_path / "cas"
    produced = evidence_store_module.archive_evidence(
        source,
        store,
        record_id="job-123",
        kind="job",
    )
    record_path = _cas_record_path(store, produced)
    blob_path = _cas_blob_path(store, produced)
    original_restore = evidence_store_module.restore_evidence

    def replace_during_restore(*args: object, **kwargs: object) -> Path:
        if target == "record":
            record_path.write_bytes(b"[]")
        else:
            replacement = bytearray(blob_path.read_bytes())
            replacement[4] ^= 1
            blob_path.write_bytes(replacement)
        return original_restore(*args, **kwargs)

    monkeypatch.setattr(evidence_store_module, "restore_evidence", replace_during_restore)
    reopened, _record_bytes = evidence_store_module.reopen_evidence_archive(
        store,
        kind="job",
        record_id="job-123",
        expected_record_digest=produced.record_digest,
        expected_content_digest=produced.content_digest,
    )
    assert reopened == produced
    monkeypatch.setattr(evidence_store_module, "restore_evidence", original_restore)
    with pytest.raises(ValueError):
        evidence_store_module.reopen_evidence_archive(
            store,
            kind="job",
            record_id="job-123",
            expected_record_digest=produced.record_digest,
            expected_content_digest=produced.content_digest,
        )


@pytest.mark.parametrize("target", ["record", "archive"])
def test_reopen_returns_captured_identity_after_last_byte_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    source = tmp_path / "job"
    source.mkdir()
    (source / "result.json").write_text('{"finished": true}\n', encoding="utf-8")
    store = tmp_path / "cas"
    produced = evidence_store_module.archive_evidence(
        source,
        store,
        record_id="job-123",
        kind="job",
    )
    record_path = _cas_record_path(store, produced)
    blob_path = _cas_blob_path(store, produced)
    if target == "record":
        original_read = evidence_store_module.read_record

        def snapshot_then_replace(*args, **kwargs) -> bytes:
            captured = original_read(*args, **kwargs)
            record_path.write_bytes(b"[]")
            return captured

        monkeypatch.setattr(evidence_store_module, "read_record", snapshot_then_replace)
    else:
        original_read = evidence_store_module.read_archive

        def snapshot_then_replace(*args, **kwargs) -> bytes:
            captured = original_read(*args, **kwargs)
            replacement = bytearray(blob_path.read_bytes())
            replacement[4] ^= 1
            blob_path.write_bytes(replacement)
            return captured

        monkeypatch.setattr(evidence_store_module, "read_archive", snapshot_then_replace)

    reopened, _record_bytes = evidence_store_module.reopen_evidence_archive(
        store,
        kind="job",
        record_id="job-123",
        expected_record_digest=produced.record_digest,
        expected_content_digest=produced.content_digest,
    )
    assert reopened == produced
    if target == "record":
        monkeypatch.setattr(evidence_store_module, "read_record", original_read)
    else:
        monkeypatch.setattr(evidence_store_module, "read_archive", original_read)
    with pytest.raises(ValueError):
        evidence_store_module.reopen_evidence_archive(
            store,
            kind="job",
            record_id="job-123",
            expected_record_digest=produced.record_digest,
            expected_content_digest=produced.content_digest,
        )


def test_former_producer_path_mutation_is_irrelevant_after_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer_path = tmp_path / "runs" / "job"
    producer_path.mkdir(parents=True)
    (producer_path / "result.json").write_text(
        '{"finished": true}\n',
        encoding="utf-8",
    )
    store = tmp_path / "cas"
    original_archive = runner_module.archive_evidence

    def mutate_old_namespace(frozen_source: Path, *args, **kwargs):
        assert frozen_source != producer_path
        assert not producer_path.exists()
        producer_path.mkdir()
        (producer_path / "result.json").write_text(
            '{"finished":false}\n',
            encoding="utf-8",
        )
        return original_archive(frozen_source, *args, **kwargs)

    monkeypatch.setattr(runner_module, "archive_evidence", mutate_old_namespace)
    locator, archive = runner_module._settle_completed_job(
        producer_path,
        store_root=store,
        record_id="job-123",
    )

    assert archive.content_digest == locator.expected_content_digest
    with evidence_store_module.materialize_evidence(locator) as restored:
        assert json.loads((restored / "result.json").read_text())["finished"] is True
    assert json.loads((producer_path / "result.json").read_text())["finished"] is False


def test_executable_identity_drift_refuses_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()
    executable = tmp_path / "harbor"
    executable.write_text("0.22.0\n", encoding="utf-8")
    snapshot = runner_module._executable_snapshot(executable)
    identity = runner_module.HarborRuntimeIdentity(
        declared_version="0.22.0",
        actual_version="0.22.0",
        executable_path=executable,
        executable_digest=snapshot[4],
        executable_device=snapshot[0],
        executable_inode=snapshot[1],
        executable_size=snapshot[2],
        executable_mtime_ns=snapshot[3],
    )
    replacement = tmp_path / "replacement"
    replacement.write_text("0.21.0 replacement bytes\n", encoding="utf-8")
    replacement.replace(executable)
    with pytest.raises(ExecutionFailure, match="changed before launch") as exc_info:
        runner_module._verify_harbor_runtime_identity(identity)
    assert exc_info.value.reason_code == "harbor_identity_drift"


def test_launch_refuses_executable_replacement_after_final_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()
    executable = tmp_path / "harbor"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    device, inode, size, mtime_ns, digest = runner_module._executable_snapshot(executable)
    identity = runner_module.HarborRuntimeIdentity(
        declared_version="0.22.0",
        actual_version="0.22.0",
        executable_path=executable,
        executable_digest=digest,
        executable_device=device,
        executable_inode=inode,
        executable_size=size,
        executable_mtime_ns=mtime_ns,
    )
    request = RunRequest(
        task=task(tmp_path), agent="oracle", name="launch-race", jobs_dir=tmp_path / "runs"
    )
    launched: list[list[str]] = []
    original_stage = runner_module._stage_verified_harbor_executable

    def replace_after_verification(
        staged_identity: runner_module.HarborRuntimeIdentity,
        staging_dir: Path,
    ) -> Path:
        replacement = tmp_path / "replacement"
        replacement.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        replacement.chmod(0o700)
        replacement.replace(executable)
        return original_stage(staged_identity, staging_dir)

    monkeypatch.setenv("EVALLAB_EVIDENCE_STORE_ROOT", str(tmp_path / "cas"))
    monkeypatch.setattr(runner_module, "resolve_harbor_runtime_identity", lambda _root: identity)
    monkeypatch.setattr(
        runner_module, "_stage_verified_harbor_executable", replace_after_verification
    )
    monkeypatch.setattr(runner_module, "harbor_container_ids", lambda _task: frozenset())
    monkeypatch.setattr(
        runner_module, "run_harbor_process", lambda command, **_kwargs: launched.append(command)
    )

    with pytest.raises(ExecutionFailure) as exc_info:
        run_experiment(request, repo_root=tmp_path)

    assert exc_info.value.reason_code == "harbor_identity_drift"
    assert launched == []
    assert json.loads(runner_module.executor_state_path(request).read_text())["status"] == "failed"


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

    settled_run = run_experiment(request, repo_root=tmp_path)

    staging_dir = request.jobs_dir / ".exec-stage" / request.name
    assert not staging_dir.exists()
    assert not (request.jobs_dir / request.name).exists()
    network_adaptation_path = runner_module._network_adaptation_path(request)
    assert network_adaptation_path.is_file()
    manifest = json.loads(network_adaptation_path.read_text())
    assert manifest["schema_version"] == "1.0"
    assert manifest["network_adaptation"]["effective_verifier_network"] == "public"
    with evidence_store_module.materialize_evidence(settled_run.cas_locator) as job_dir:
        metadata = json.loads((job_dir / "lab-metadata.json").read_text())
        assert metadata["network_adaptation"]["effective_verifier_network"] == "public"
        assert metadata["harbor_runtime"] == {
            "declared_version": "0.22.0",
            "actual_version": "0.22.0",
            "executable_path": str(tmp_path / "harbor"),
            "executable_digest": runner_module._executable_snapshot(tmp_path / "harbor")[4],
        }
    assert settled_run.cas_locator.expected_record_digest == (settled_run.cas_record.record_digest)


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


def test_zai_opencode_host_process_receives_only_proxy_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "zai-secret-never-enters-agent-environment"
    auth = tmp_path / ".local/share/opencode/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"zai-coding-plan": {"type": "api", "key": secret}}))
    auth.chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    script = (
        "import os; "
        "cap=os.environ['ZAI_API_KEY']; "
        "print('capability=' + str(bool(cap) and cap != " + repr(secret) + ")); "
        "print('secret-file=' + str(bool(os.environ.get('" + ZAI_SECRET_FILE_ENV + "')))); "
        "print('proxy-capability=' + str(cap == os.environ['" + ZAI_PROXY_CAPABILITY_ENV + "']))"
    )
    log_path = tmp_path / "zai-host.log"

    result = run_harbor_process(
        [
            sys.executable,
            "-c",
            script,
            resolve_harbor_agent("zai-opencode"),
        ],
        cwd=tmp_path,
        timeout_seconds=5,
        log_path=log_path,
        proxy_attempt_id="test-attempt",
        proxy_limits=ProxyTrialLimits(
            max_requests=16,
            max_input_tokens=200_000,
            max_output_tokens=64_000,
            max_total_tokens=264_000,
            max_cost_micros=1_000_000,
        ),
    )

    assert result.returncode == 0
    assert log_path.read_text().splitlines() == [
        "capability=True",
        "secret-file=True",
        "proxy-capability=True",
    ]
    assert secret not in log_path.read_text()
    assert not list(tmp_path.glob("evallab-zai-secret.*"))
    assert not list(tmp_path.glob("evallab-zai-usage.*"))


def test_zai_opencode_routes_through_proxy_isolated_pinned_adapter(
    tmp_path: Path,
) -> None:
    request = RunRequest(
        task=task(tmp_path),
        agent="zai-opencode",
        model="zai-coding-plan/glm-5.3",
        name="zai-opencode-pinned-test",
        jobs_dir=tmp_path / "runs",
        max_requests=16,
        max_input_tokens=200_000,
        max_output_tokens=64_000,
        max_total_tokens=264_000,
        cost_limit_usd=1.0,
        allow_billable=True,
    )

    command = build_command(request)

    assert resolve_harbor_agent("zai-opencode") == (
        "evallab.harbor_zai_opencode:SecretSafeZaiOpenCodeAgent"
    )
    assert command[command.index("--agent") + 1] == resolve_harbor_agent("zai-opencode")
    assert command[command.index("--model") + 1] == "zai-coding-plan/glm-5.3"
    assert command[command.index("--n-concurrent-agents") + 1] == "1"
    assert command[command.index("--n-tasks") + 1] == "1"
    assert command[command.index("--max-retries") + 1] == "0"

    wrapped = subscription_command(
        request,
        command,
        repo_root=Path(__file__).resolve().parents[1],
    )
    assert "--extra-docker-compose" in wrapped
    assert wrapped[-1].endswith("containers/zai-secret.compose.yaml")


def test_zai_secret_materializes_only_opencode_provider_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    zai_secret = "zai-provider-secret"
    foreign_secret = "foreign-provider-secret"
    auth = tmp_path / ".local/share/opencode/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps(
            {
                "xai": {"type": "api", "key": foreign_secret},
                "zai-coding-plan": {"type": "api", "key": zai_secret},
            }
        )
    )
    auth.chmod(0o600)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    destination = tmp_path / "staged/key"
    materialize_zai_secret_file(destination, home=tmp_path)

    assert read_owner_secret_file(destination) == zai_secret
    assert foreign_secret not in destination.read_text()
