"""Focused contract tests for mini-SWE ingestion/inspection fidelity.

Tests for all five failure classes:
(a) Trial timeout: TrialTimeoutFailure recorded with timed_out_trial identity, survives Z.ai lane.
(b) Nonzero agent exit without trajectory: failure with exit code, no fabricated reward.
(c) Trajectory JSON present but malformed: parse-failure recorded, raw bytes retained, no silent drop.
(d) Absent/zero usage from the proxy: usage recorded as missing (nulls), never zero-cost.
(e) Provider-returned model identity mismatch vs requested: visible flag, distinct requested vs returned.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import evallab.runner as runner_module
from evallab.execution_contracts import (
    DEEPSEEK_PROXY_HOST,
    ZAI_OPENAPI_MODEL_SELECTOR,
    ZAI_OPENAPI_PROXY_HOST,
    ExecutionFailure,
    HarborProcessResult,
    RunRequest,
    TrialTimeoutFailure,
)
from evallab.queue import DirectoryQueue, Executor
from evallab.runner import (
    _write_run_metadata,
    run_experiment,
)
from evallab.schemas import AutoRunRule, ExperimentSpec, StandingApprovalsPolicy


def _make_task(tmp_path: Path) -> Path:
    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.toml").write_text(
        'schema_version = "1.4"\n[task]\nname = "test-task"\n\n[agent]\n'
    )
    return task_dir


def _make_spec(tmp_path: Path, **kwargs: Any) -> ExperimentSpec:
    _make_task(tmp_path)
    defaults = {
        "name": "test-spec",
        "hypothesis": "test hypothesis",
        "purpose": "practice",
        "task": "task",
        "agent": "mini-swe-agent",
        "model": ZAI_OPENAPI_MODEL_SELECTOR,
        "jobs_dir": "runs",
        "submitted_by": "test",
        "est_cost_usd": 0.0,
        "attempts": 1,
        "concurrency": 1,
    }
    defaults.update(kwargs)
    return ExperimentSpec(**defaults)


def _setup_mock_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runner_module.shutil, "which", lambda _cmd: "/bin/harbor")
    monkeypatch.setattr(runner_module, "harbor_container_ids", lambda _task: frozenset())
    monkeypatch.setattr(runner_module, "tool_version", lambda _cmd: "1.0")
    monkeypatch.setattr(
        runner_module,
        "git_state",
        lambda _root: {"commit": "abc1234", "dirty": False},
    )
    monkeypatch.setattr(
        runner_module,
        "preflight_request",
        lambda _req: type("Decision", (), {"proceed": True, "reason": None})(),
    )


# ---------------------------------------------------------------------------
# Class (a): trial timeout
# ---------------------------------------------------------------------------

def test_trial_timeout_records_timed_out_trial_identity_and_survives_zai_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trial timeout in Z.ai OpenAPI lane must raise TrialTimeoutFailure with timed_out_trial."""
    _setup_mock_run(monkeypatch, tmp_path)

    jobs_dir = tmp_path / "runs"
    task_dir = _make_task(tmp_path)
    request = RunRequest(
        task=task_dir,
        agent="mini-swe-agent",
        model=ZAI_OPENAPI_MODEL_SELECTOR,
        name="timeout-trial-zai",
        jobs_dir=jobs_dir,
        timeout_seconds=30,
        allow_billable=True,
        max_requests=10,
        max_input_tokens=1000,
        max_output_tokens=500,
        max_total_tokens=1500,
        cost_limit_usd=1.0,
    )

    def mock_run_harbor(*_args: Any, **kwargs: Any) -> HarborProcessResult:
        job_dir = kwargs["job_dir"]
        job_dir.mkdir(parents=True, exist_ok=True)
        return HarborProcessResult(
            returncode=-1,
            timed_out=True,
            log_path=kwargs["log_path"],
            timed_out_trial="test-task__hung_trial",
        )

    monkeypatch.setattr(runner_module, "run_harbor_process", mock_run_harbor)

    with pytest.raises(TrialTimeoutFailure) as exc_info:
        run_experiment(request, repo_root=tmp_path)

    # 1. Exception must carry the timed_out_trial attribute
    assert hasattr(exc_info.value, "timed_out_trial"), "TrialTimeoutFailure must carry timed_out_trial attribute"
    assert exc_info.value.timed_out_trial == "test-task__hung_trial"
    assert "test-task__hung_trial" in str(exc_info.value)

    # 2. Executor state must record the timed_out_trial identity
    state_path = jobs_dir / ".executor" / f"{request.name}.state.json"
    assert state_path.is_file()
    state = json.loads(state_path.read_text())
    assert state.get("timed_out") is True
    assert state.get("timed_out_trial") == "test-task__hung_trial"
    assert state.get("status") == "failed"

    # 3. Lab metadata must record the timed_out_trial identity
    metadata_path = jobs_dir / request.name / "lab-metadata.json"
    assert metadata_path.is_file()
    metadata = json.loads(metadata_path.read_text())
    assert metadata.get("timed_out") is True
    assert metadata.get("timed_out_trial") == "test-task__hung_trial"


def test_run_experiment_stages_zai_proxy_host_for_zai_miniswe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_experiment for mini-swe-agent with Z.ai OpenAPI model must stage ZAI_OPENAPI_PROXY_HOST."""
    _setup_mock_run(monkeypatch, tmp_path)
    jobs_dir = tmp_path / "runs"
    task_dir = _make_task(tmp_path)
    request = RunRequest(
        task=task_dir,
        agent="mini-swe-agent",
        model=ZAI_OPENAPI_MODEL_SELECTOR,
        name="staged-proxy-host-test",
        jobs_dir=jobs_dir,
        allow_billable=True,
        max_requests=10,
        max_input_tokens=1000,
        max_output_tokens=500,
        max_total_tokens=1500,
        cost_limit_usd=1.0,
    )

    staged_hosts: list[tuple[str, ...]] = []
    orig_stage = runner_module._stage_task_for_host

    def spy_stage(*args: Any, **kwargs: Any) -> Any:
        staged_hosts.append(kwargs.get("agent_allowed_hosts", ()))
        return orig_stage(*args, **kwargs)

    monkeypatch.setattr(runner_module, "_stage_task_for_host", spy_stage)

    def mock_run_harbor(*_args: Any, **kwargs: Any) -> HarborProcessResult:
        job_dir = kwargs["job_dir"]
        job_dir.mkdir(parents=True, exist_ok=True)
        return HarborProcessResult(
            returncode=-1,
            timed_out=True,
            log_path=kwargs["log_path"],
            timed_out_trial="trial1",
        )

    monkeypatch.setattr(runner_module, "run_harbor_process", mock_run_harbor)

    with pytest.raises(TrialTimeoutFailure):
        run_experiment(request, repo_root=tmp_path)

    assert len(staged_hosts) == 1
    assert ZAI_OPENAPI_PROXY_HOST in staged_hosts[0]
    assert DEEPSEEK_PROXY_HOST not in staged_hosts[0]

# ---------------------------------------------------------------------------
# Class (b): nonzero agent exit without trajectory
# ---------------------------------------------------------------------------

def test_nonzero_agent_exit_without_trajectory_fails_with_exit_code_no_reward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent nonzero exit with no trajectory must be treated as failure; verifier reward cannot be accepted."""
    _setup_mock_run(monkeypatch, tmp_path)

    jobs_dir = tmp_path / "runs"
    task_dir = _make_task(tmp_path)
    request = RunRequest(
        task=task_dir,
        agent="mini-swe-agent",
        model=ZAI_OPENAPI_MODEL_SELECTOR,
        name="nonzero-exit-notraj",
        jobs_dir=jobs_dir,
        allow_billable=True,
        max_requests=10,
        max_input_tokens=1000,
        max_output_tokens=500,
        max_total_tokens=1500,
        cost_limit_usd=1.0,
    )

    # Harbor exited 0, but trial result has agent exit_code 1, no trajectory.json,
    # and verifier_result has reward 1.0 (fabricated / accidental).
    def mock_run_harbor(*_args: Any, **kwargs: Any) -> HarborProcessResult:
        job_dir = kwargs["job_dir"]
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "result.json").write_text(
            json.dumps({"n_total_trials": 1, "stats": {}, "finished_at": "2026-09-18T10:00:00Z"})
        )
        (job_dir / "config.json").write_text("{}")
        (job_dir / "lock.json").write_text("{}")

        trial_dir = job_dir / "test-task__trial1"
        trial_dir.mkdir(parents=True, exist_ok=True)
        trial_result = {
            "id": "00000000-0000-0000-0000-000000000001",
            "task_name": "test-task",
            "trial_name": "test-task__trial1",
            "agent_result": {"exit_code": 1, "n_input_tokens": 100, "n_output_tokens": 10},
            "verifier_result": {"rewards": {"reward": 1.0}},  # fabricated!
            "started_at": "2026-09-18T10:00:00Z",
            "finished_at": "2026-09-18T10:01:00Z",
        }
        (trial_dir / "result.json").write_text(json.dumps(trial_result))
        (trial_dir / "config.json").write_text("{}")
        (trial_dir / "lock.json").write_text("{}")
        # NOTE: agent/trajectory.json is ABSENT

        proxy_usage = {
            "schema_version": 1,
            "capability_id": "sha256:" + "0" * 64,
            "attempt_id": "test-attempt",
            "limits": {
                "max_requests": 10,
                "max_input_tokens": 1000,
                "max_output_tokens": 500,
                "max_total_tokens": 1500,
                "max_cost_micros": 1000000,
            },
            "pricing": {"input_cost_micros_per_million": 100000, "output_cost_micros_per_million": 200000},
            "calls": [
                {
                    "call_id": 1,
                    "state": "reconciled",
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cost_micros": 12,
                    "requested_model": "glm-5.3-flash",
                    "returned_model": "glm-5.3-flash",
                }
            ],
            "totals": {
                "requests": 1,
                "input_tokens": 100,
                "output_tokens": 10,
                "total_tokens": 110,
                "cost_micros": 12,
            },
            "unresolved_requests": 0,
            "sequence": 2,
        }
        return HarborProcessResult(
            returncode=0,
            timed_out=False,
            log_path=kwargs["log_path"],
            proxy_usage=proxy_usage,
        )

    monkeypatch.setattr(runner_module, "run_harbor_process", mock_run_harbor)

    with pytest.raises(ExecutionFailure) as exc_info:
        run_experiment(request, repo_root=tmp_path)

    # Must fail with exit code visible, reason_code indicating agent exit, not a success
    assert "exit" in str(exc_info.value).lower() or "1" in str(exc_info.value)
    assert exc_info.value.reason_code in {"agent_exit_nonzero", "agent_nonzero_exit", "agent_exit_1"}


# ---------------------------------------------------------------------------
# Class (c): trajectory JSON present but malformed
# ---------------------------------------------------------------------------

def test_malformed_trajectory_json_records_parse_failure_and_retains_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trajectory JSON present but invalid must record parse failure and keep raw bytes."""
    _setup_mock_run(monkeypatch, tmp_path)

    jobs_dir = tmp_path / "runs"
    task_dir = _make_task(tmp_path)
    request = RunRequest(
        task=task_dir,
        agent="mini-swe-agent",
        model=ZAI_OPENAPI_MODEL_SELECTOR,
        name="malformed-traj",
        jobs_dir=jobs_dir,
        allow_billable=True,
        max_requests=10,
        max_input_tokens=1000,
        max_output_tokens=500,
        max_total_tokens=1500,
        cost_limit_usd=1.0,
    )

    malformed_bytes = b'{"steps": [{"action": "bash", "command": "echo hello", incomplete json'

    def mock_run_harbor(*_args: Any, **kwargs: Any) -> HarborProcessResult:
        job_dir = kwargs["job_dir"]
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "result.json").write_text(
            json.dumps({"n_total_trials": 1, "stats": {}, "finished_at": "2026-09-18T10:00:00Z"})
        )
        (job_dir / "config.json").write_text("{}")
        (job_dir / "lock.json").write_text("{}")

        trial_dir = job_dir / "test-task__trial1"
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "result.json").write_text(
            json.dumps({
                "id": "00000000-0000-0000-0000-000000000001",
                "task_name": "test-task",
                "trial_name": "test-task__trial1",
                "agent_result": {"exit_code": 0},
                "verifier_result": {"rewards": {"reward": 1.0}},
                "started_at": "2026-09-18T10:00:00Z",
                "finished_at": "2026-09-18T10:01:00Z",
            })
        )
        (trial_dir / "config.json").write_text("{}")
        (trial_dir / "lock.json").write_text("{}")

        # Write malformed trajectory
        agent_dir = trial_dir / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "trajectory.json").write_bytes(malformed_bytes)

        proxy_usage = {
            "schema_version": 1,
            "capability_id": "sha256:" + "0" * 64,
            "attempt_id": "test-attempt",
            "limits": {
                "max_requests": 10,
                "max_input_tokens": 1000,
                "max_output_tokens": 500,
                "max_total_tokens": 1500,
                "max_cost_micros": 1000000,
            },
            "pricing": {"input_cost_micros_per_million": 100000, "output_cost_micros_per_million": 200000},
            "calls": [
                {
                    "call_id": 1,
                    "state": "reconciled",
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cost_micros": 12,
                    "requested_model": "glm-5.3-flash",
                    "returned_model": "glm-5.3-flash",
                }
            ],
            "totals": {
                "requests": 1,
                "input_tokens": 100,
                "output_tokens": 10,
                "total_tokens": 110,
                "cost_micros": 12,
            },
            "unresolved_requests": 0,
            "sequence": 2,
        }
        return HarborProcessResult(
            returncode=0,
            timed_out=False,
            log_path=kwargs["log_path"],
            proxy_usage=proxy_usage,
        )

    monkeypatch.setattr(runner_module, "run_harbor_process", mock_run_harbor)

    with pytest.raises(ExecutionFailure) as exc_info:
        run_experiment(request, repo_root=tmp_path)

    # 1. Parse failure must be indicated
    assert "trajectory" in str(exc_info.value).lower()
    assert exc_info.value.reason_code in {"trajectory_parse_failure", "corrupt_evidence"}

    # 2. Raw bytes must be retained on disk — NOT deleted, NOT replaced with empty/dummy dict
    traj_path = jobs_dir / request.name / "test-task__trial1" / "agent" / "trajectory.json"
    assert traj_path.is_file(), "Trajectory file must not be silently dropped"
    assert traj_path.read_bytes() == malformed_bytes, "Raw bytes must be preserved exactly"


# ---------------------------------------------------------------------------
# Class (d): absent/zero usage from the proxy
# ---------------------------------------------------------------------------

def test_absent_proxy_usage_recorded_as_missing_nulls_never_zero_cost(
    tmp_path: Path,
) -> None:
    """When proxy usage is absent, metadata must record missing (nulls), never zero-cost."""
    jobs_dir = tmp_path / "runs"
    task_dir = _make_task(tmp_path)
    job_dir = jobs_dir / "absent-usage-job"
    job_dir.mkdir(parents=True, exist_ok=True)

    request = RunRequest(
        task=task_dir,
        agent="mini-swe-agent",
        model=ZAI_OPENAPI_MODEL_SELECTOR,
        name="absent-usage-job",
        jobs_dir=jobs_dir,
    )

    process = HarborProcessResult(
        returncode=0,
        timed_out=False,
        log_path=jobs_dir / ".executor" / "absent.log",
        proxy_usage=None,  # ABSENT usage
    )
    (jobs_dir / ".executor").mkdir(parents=True, exist_ok=True)
    process.log_path.touch()

    _write_run_metadata(
        request,
        repo_root=tmp_path,
        command=["harbor", "run"],
        started=runner_module.datetime.now(runner_module.UTC),
        finished=runner_module.datetime.now(runner_module.UTC),
        process=process,
    )

    metadata = json.loads((job_dir / "lab-metadata.json").read_text())
    assert "provider_usage" in metadata, "provider_usage must be present in metadata"
    usage = metadata["provider_usage"]

    # Must be recorded as missing with nulls, NEVER zero-cost (not 0 or 0.0)
    assert usage.get("cost_usd") is None, "cost_usd must be null, not 0.0"
    assert usage.get("cost_micros") is None, "cost_micros must be null, not 0"
    assert usage.get("input_tokens") is None, "input_tokens must be null, not 0"
    assert usage.get("output_tokens") is None, "output_tokens must be null, not 0"
    assert usage.get("total_tokens") is None, "total_tokens must be null, not 0"
    assert usage.get("usage_status") == "missing"


def test_zero_call_proxy_usage_recorded_as_nulls_never_zero_cost(
    tmp_path: Path,
) -> None:
    """When proxy usage has 0 calls/tokens, usage fields must be nulls, not zero."""
    jobs_dir = tmp_path / "runs"
    task_dir = _make_task(tmp_path)
    job_dir = jobs_dir / "zero-calls-job"
    job_dir.mkdir(parents=True, exist_ok=True)

    request = RunRequest(
        task=task_dir,
        agent="mini-swe-agent",
        model=ZAI_OPENAPI_MODEL_SELECTOR,
        name="zero-calls-job",
        jobs_dir=jobs_dir,
    )

    # Proxy returned usage payload, but with 0 calls (no actual calls took place)
    proxy_usage = {
        "schema_version": 1,
        "capability_id": "sha256:" + "0" * 64,
        "attempt_id": "test-attempt",
        "limits": {
            "max_requests": 10,
            "max_input_tokens": 1000,
            "max_output_tokens": 500,
            "max_total_tokens": 1500,
            "max_cost_micros": 1000000,
        },
        "calls": [],
        "totals": {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_micros": 0,
        },
        "unresolved_requests": 0,
        "sequence": 0,
    }

    process = HarborProcessResult(
        returncode=0,
        timed_out=False,
        log_path=jobs_dir / ".executor" / "zero.log",
        proxy_usage=proxy_usage,
    )
    (jobs_dir / ".executor").mkdir(parents=True, exist_ok=True)
    process.log_path.touch()

    _write_run_metadata(
        request,
        repo_root=tmp_path,
        command=["harbor", "run"],
        started=runner_module.datetime.now(runner_module.UTC),
        finished=runner_module.datetime.now(runner_module.UTC),
        process=process,
    )

    metadata = json.loads((job_dir / "lab-metadata.json").read_text())
    usage = metadata["provider_usage"]
    totals = usage.get("totals", {})

    assert totals.get("cost_usd") is None, "cost_usd for 0 calls must be null, not 0.0"
    assert totals.get("cost_micros") is None, "cost_micros for 0 calls must be null, not 0"
    assert totals.get("input_tokens") is None, "input_tokens for 0 calls must be null, not 0"
    assert totals.get("output_tokens") is None, "output_tokens for 0 calls must be null, not 0"


# ---------------------------------------------------------------------------
# Class (e): provider-returned model identity mismatch vs requested
# ---------------------------------------------------------------------------

def test_model_identity_mismatch_surfaces_flag_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When returned model disagrees with requested, metadata surfaces mismatch flag and execution fails."""
    _setup_mock_run(monkeypatch, tmp_path)

    jobs_dir = tmp_path / "runs"
    task_dir = _make_task(tmp_path)
    request = RunRequest(
        task=task_dir,
        agent="mini-swe-agent",
        model="zai/glm-5.3-flash",
        name="model-mismatch",
        jobs_dir=jobs_dir,
        allow_billable=True,
        max_requests=10,
        max_input_tokens=1000,
        max_output_tokens=500,
        max_total_tokens=1500,
        cost_limit_usd=1.0,
    )

    # Proxy call returned "glm-4" instead of "glm-5.3-flash"
    def mock_run_harbor(*_args: Any, **kwargs: Any) -> HarborProcessResult:
        job_dir = kwargs["job_dir"]
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "result.json").write_text(
            json.dumps({"n_total_trials": 1, "stats": {}, "finished_at": "2026-09-18T10:00:00Z"})
        )
        (job_dir / "config.json").write_text("{}")
        (job_dir / "lock.json").write_text("{}")

        trial_dir = job_dir / "test-task__trial1"
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "result.json").write_text(
            json.dumps({
                "id": "00000000-0000-0000-0000-000000000001",
                "task_name": "test-task",
                "trial_name": "test-task__trial1",
                "agent_result": {"exit_code": 0},
                "verifier_result": {"rewards": {"reward": 1.0}},
                "started_at": "2026-09-18T10:00:00Z",
                "finished_at": "2026-09-18T10:01:00Z",
            })
        )
        (trial_dir / "config.json").write_text("{}")
        (trial_dir / "lock.json").write_text("{}")
        agent_dir = trial_dir / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "trajectory.json").write_text(json.dumps({"steps": []}))

        proxy_usage = {
            "schema_version": 1,
            "capability_id": "sha256:" + "0" * 64,
            "attempt_id": "test-attempt",
            "limits": {
                "max_requests": 10,
                "max_input_tokens": 1000,
                "max_output_tokens": 500,
                "max_total_tokens": 1500,
                "max_cost_micros": 1000000,
            },
            "pricing": {"input_cost_micros_per_million": 100000, "output_cost_micros_per_million": 200000},
            "calls": [
                {
                    "call_id": 1,
                    "state": "reconciled",
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cost_micros": 12,
                    "requested_model": "glm-5.3-flash",
                    "returned_model": "glm-4",  # MISMATCH!
                }
            ],
            "totals": {
                "requests": 1,
                "input_tokens": 100,
                "output_tokens": 10,
                "total_tokens": 110,
                "cost_micros": 12,
            },
            "unresolved_requests": 0,
            "sequence": 2,
        }
        return HarborProcessResult(
            returncode=0,
            timed_out=False,
            log_path=kwargs["log_path"],
            proxy_usage=proxy_usage,
        )

    monkeypatch.setattr(runner_module, "run_harbor_process", mock_run_harbor)

    with pytest.raises(ExecutionFailure) as exc_info:
        run_experiment(request, repo_root=tmp_path)

    # 1. Must fail with model_identity_mismatch
    assert exc_info.value.reason_code == "model_identity_mismatch"
    assert "glm-4" in str(exc_info.value) or "mismatch" in str(exc_info.value)

    # 2. Metadata must distinctly surface requested vs returned and mismatch flag
    metadata_path = jobs_dir / request.name / "lab-metadata.json"
    assert metadata_path.is_file()
    metadata = json.loads(metadata_path.read_text())
    assert "model_identity" in metadata
    identity = metadata["model_identity"]
    assert identity["requested"] == "zai/glm-5.3-flash"
    assert identity["returned"] == "glm-4"
    assert identity["matched"] is False
    assert identity["mismatch"] is True
    assert metadata.get("model_identity_mismatch") is True

# ---------------------------------------------------------------------------
def _create_test_executor(root: Path) -> Executor:
    from evallab.quota import Headroom

    return Executor(
        repo_root=root,
        queue=DirectoryQueue(root / "queue"),
        policy=StandingApprovalsPolicy(
            daily_cost_ceiling_usd=20,
            per_job_cost_ceiling_usd=3,
            quiet_failure_rule=3,
            auto_run=[AutoRunRule(name="controls", agents=["oracle", "mini-swe-agent"])],
            escalate_to_human=[],
        ),
        runner=lambda req: req.jobs_dir / req.name,
        ingester=lambda _job: None,
        spent_today=lambda: 0.0,
        credential_probe=lambda: frozenset(),
        headroom=lambda _agent=None: Headroom(
            agent="mini-swe-agent",
            plan="team",
            window="weekly",
            availability="observed",
            hard_stop=False,
            window_resets_at=None,
            used_percent=10.0,
            as_of=runner_module.datetime.now(runner_module.UTC),
            source_path=Path("dummy"),
        ),
    )


def test_queue_settle_post_run_refuses_nonzero_agent_exit_without_trajectory(
    tmp_path: Path,
) -> None:
    """queue _settle_post_run must refuse completion if an agent exited nonzero without trajectory."""
    exec_inst = _create_test_executor(tmp_path)
    exec_inst.queue.ensure_directories()

    spec = _make_spec(tmp_path, name="queue-nonzero-exit", jobs_dir="runs")
    job_dir = tmp_path / "runs" / spec.name
    job_dir.mkdir(parents=True, exist_ok=True)

    (job_dir / "result.json").write_text(
        json.dumps({"n_total_trials": 1, "stats": {}, "finished_at": "2026-09-18T10:00:00Z"})
    )
    (job_dir / "config.json").write_text("{}")
    (job_dir / "lock.json").write_text("{}")

    trial_dir = job_dir / "test-task__trial1"
    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / "result.json").write_text(
        json.dumps({
            "id": "00000000-0000-0000-0000-000000000001",
            "task_name": "test-task",
            "trial_name": "test-task__trial1",
            "agent_result": {"exit_code": 137},
            "verifier_result": {"rewards": {"reward": 1.0}},  # fabricated reward
            "started_at": "2026-09-18T10:00:00Z",
            "finished_at": "2026-09-18T10:01:00Z",
        })
    )
    (trial_dir / "config.json").write_text("{}")
    (trial_dir / "lock.json").write_text("{}")
    # NOTE: NO trajectory.json

    decision = exec_inst._settle_post_run(job_dir, spec, actor="executor")
    assert decision is not None, "_settle_post_run must return a refusal PolicyDecision"
    assert not decision.admitted
    assert decision.reason_code in {"agent_nonzero_exit", "agent_exit_137", "agent_exit_nonzero"}


def test_queue_reconcile_running_fails_nonzero_agent_exit_without_trajectory(
    tmp_path: Path,
) -> None:
    """reconcile_running must transition running spec to failed if agent exited nonzero without trajectory."""
    exec_inst = _create_test_executor(tmp_path)
    exec_inst.queue.ensure_directories()

    spec = _make_spec(tmp_path, name="reconcile-nonzero-exit", jobs_dir="runs")
    submitted, _ = exec_inst.submit(spec)
    exec_inst.queue.transition(submitted, "running", actor="test", event="dispatch_started")

    job_dir = tmp_path / "runs" / spec.name
    job_dir.mkdir(parents=True, exist_ok=True)

    (job_dir / "result.json").write_text(
        json.dumps({"n_total_trials": 1, "stats": {}, "finished_at": "2026-09-18T10:00:00Z"})
    )
    (job_dir / "config.json").write_text("{}")
    (job_dir / "lock.json").write_text("{}")

    trial_dir = job_dir / "test-task__trial1"
    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / "result.json").write_text(
        json.dumps({
            "id": "00000000-0000-0000-0000-000000000001",
            "task_name": "test-task",
            "trial_name": "test-task__trial1",
            "agent_result": {"exit_code": 1},
            "verifier_result": {"rewards": {"reward": 1.0}},
            "started_at": "2026-09-18T10:00:00Z",
            "finished_at": "2026-09-18T10:01:00Z",
        })
    )
    (trial_dir / "config.json").write_text("{}")
    (trial_dir / "lock.json").write_text("{}")

    exec_inst.reconcile_running()

    # Must have transitioned from running to failed, not done!
    running_specs = exec_inst.queue.list_specs("running")
    assert len(running_specs) == 0
    failed_specs = exec_inst.queue.list_specs("failed")
    assert len(failed_specs) == 1
    done_specs = exec_inst.queue.list_specs("done")
    assert len(done_specs) == 0


def test_queue_settle_post_run_refuses_malformed_trajectory(
    tmp_path: Path,
) -> None:
    """queue _settle_post_run must refuse completion if trajectory is present but malformed JSON."""
    exec_inst = _create_test_executor(tmp_path)
    exec_inst.queue.ensure_directories()

    spec = _make_spec(tmp_path, name="queue-malformed-traj", jobs_dir="runs")
    job_dir = tmp_path / "runs" / spec.name
    job_dir.mkdir(parents=True, exist_ok=True)

    (job_dir / "result.json").write_text(
        json.dumps({"n_total_trials": 1, "stats": {}, "finished_at": "2026-09-18T10:00:00Z"})
    )
    (job_dir / "config.json").write_text("{}")
    (job_dir / "lock.json").write_text("{}")

    trial_dir = job_dir / "test-task__trial1"
    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / "result.json").write_text(
        json.dumps({
            "id": "00000000-0000-0000-0000-000000000001",
            "task_name": "test-task",
            "trial_name": "test-task__trial1",
            "agent_result": {"exit_code": 0},
            "verifier_result": {"rewards": {"reward": 1.0}},
            "started_at": "2026-09-18T10:00:00Z",
            "finished_at": "2026-09-18T10:01:00Z",
        })
    )
    (trial_dir / "config.json").write_text("{}")
    (trial_dir / "lock.json").write_text("{}")

    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "trajectory.json").write_bytes(b'{"incomplete": ')

    decision = exec_inst._settle_post_run(job_dir, spec, actor="executor")
    assert decision is not None, "_settle_post_run must return a refusal PolicyDecision"
    assert not decision.admitted
    assert decision.reason_code in {"trajectory_parse_failure", "corrupt_evidence"}


def test_queue_settle_post_run_refuses_model_identity_mismatch(
    tmp_path: Path,
) -> None:
    """queue _settle_post_run must refuse completion if model identity mismatched."""
    exec_inst = _create_test_executor(tmp_path)
    exec_inst.queue.ensure_directories()

    spec = _make_spec(tmp_path, name="queue-mismatch", jobs_dir="runs", model="zai/glm-5.3-flash")
    job_dir = tmp_path / "runs" / spec.name
    job_dir.mkdir(parents=True, exist_ok=True)

    (job_dir / "result.json").write_text(
        json.dumps({"n_total_trials": 1, "stats": {}, "finished_at": "2026-09-18T10:00:00Z"})
    )
    (job_dir / "config.json").write_text("{}")
    (job_dir / "lock.json").write_text("{}")
    (job_dir / "lab-metadata.json").write_text(
        json.dumps({
            "model_identity": {
                "requested": "zai/glm-5.3-flash",
                "returned": "glm-4",
                "matched": False,
                "mismatch": True,
            },
            "model_identity_mismatch": True,
        })
    )

    trial_dir = job_dir / "test-task__trial1"
    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / "result.json").write_text(
        json.dumps({
            "id": "00000000-0000-0000-0000-000000000001",
            "task_name": "test-task",
            "trial_name": "test-task__trial1",
            "agent_result": {"exit_code": 0},
            "verifier_result": {"rewards": {"reward": 1.0}},
            "started_at": "2026-09-18T10:00:00Z",
            "finished_at": "2026-09-18T10:01:00Z",
        })
    )
    (trial_dir / "config.json").write_text("{}")
    (trial_dir / "lock.json").write_text("{}")
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "trajectory.json").write_text(json.dumps({"steps": []}))

    decision = exec_inst._settle_post_run(job_dir, spec, actor="executor")
    assert decision is not None, "_settle_post_run must refuse completion on model identity mismatch"
    assert not decision.admitted
    assert decision.reason_code == "model_identity_mismatch"
