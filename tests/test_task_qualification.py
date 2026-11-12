"""Behavior tests for cost estimates, trial facts, and source package resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.evidence.facts import extract_trial_fact, trial_environment_type
from evallab.results import load_job
from evallab.task_qualification import estimate_cost_usd, task_dir_for_trial

TASK_TOML = """\
schema_version = "1.4"

[task]
name = "mimo-v2.6-rl/qual-task"
description = "qualification fixture"

[metadata]
domain = "terminal"
source_id = "qual-task"

[agent]
timeout_sec = 100.0

[verifier]
timeout_sec = 60.0

[environment]
cpus = 2
memory_mb = 2048
storage_mb = 10240
"""


def _write_staged_task(root: Path, *, toml: str = TASK_TOML) -> Path:
    staged = root / "staged-task"
    staged.mkdir(parents=True, exist_ok=True)
    (staged / "task.toml").write_text(toml)
    return staged


def _timing(start: str, end: str | None) -> dict[str, str | None]:
    return {"started_at": start, "finished_at": end}


def _write_trial(
    job: Path,
    name: str,
    *,
    agent: str = "nop",
    env_type: str = "docker",
    import_path: str | None = None,
    reward: float | None = 0.0,
    exception: dict | None = None,
    agent_execution: bool = True,
    stability_runs: list | None = None,
    staged: Path | None = None,
    trial_id: str = "11111111-1111-1111-1111-111111111111",
) -> Path:
    trial = job / name
    trial.mkdir(parents=True, exist_ok=True)
    (trial / "trial.log").write_text("log")
    (trial / "config.json").write_text(json.dumps({
        "environment": {"type": env_type, "import_path": import_path},
        "agent": {"name": agent},
        "trial_name": name,
    }))
    lock_task: dict = {"name": "qual-task"}
    if staged is not None:
        lock_task["path"] = str(staged)
    (trial / "lock.json").write_text(json.dumps({
        "task": lock_task,
        "environment": {"type": env_type, "import_path": import_path},
        "agent": {"name": agent},
    }))
    verifier_rewards = {} if reward is None else {"reward": reward}
    (trial / "result.json").write_text(json.dumps({
        "id": trial_id,
        "task_name": "mimo-v2.6-rl/qual-task",
        "trial_name": name,
        "config": {
            "environment": {"type": env_type, "import_path": import_path},
            "agent": {"name": agent},
        },
        "agent_info": {"name": agent, "version": "1.0.0"},
        "agent_result": {},
        "verifier_result": {"rewards": verifier_rewards},
        "exception_info": exception,
        "started_at": "2026-09-28T00:00:00Z",
        "finished_at": "2026-09-28T00:01:00Z",
        "environment_setup": _timing("2026-09-28T00:00:00Z", "2026-09-28T00:00:05Z"),
        "agent_setup": _timing("2026-09-28T00:00:05Z", "2026-09-28T00:00:06Z"),
        "agent_execution": _timing("2026-09-28T00:00:06Z", "2026-09-28T00:00:50Z")
        if agent_execution
        else None,
        "verifier": _timing("2026-09-28T00:00:50Z", "2026-09-28T00:01:00Z"),
    }))
    if reward is not None:
        verifier_dir = trial / "verifier"
        verifier_dir.mkdir(exist_ok=True)
        (verifier_dir / "reward.txt").write_text(f"{reward}\n")
    if stability_runs is not None:
        verifier_dir = trial / "verifier"
        verifier_dir.mkdir(exist_ok=True)
        (verifier_dir / "stability.json").write_text(
            json.dumps({"runs": [{"reward": value} for value in stability_runs]})
        )
    return trial


def _write_job(
    root: Path,
    name: str,
    trials: list[dict],
    *,
    version_digest: str = "sha256:" + "a" * 64,
    harbor_digest: str = "sha256:" + "b" * 64,
    staged: Path | None = None,
) -> Path:
    job = root / name
    job.mkdir(parents=True, exist_ok=True)
    (job / "result.json").write_text(json.dumps({
        "id": "22222222-2222-2222-2222-222222222222",
        "n_total_trials": len(trials),
        "stats": {},
        "finished_at": "2026-09-28T00:02:00Z",
    }))
    (job / "config.json").write_text("{}")
    (job / "lock.json").write_text("{}")
    if staged is None:
        staged = _write_staged_task(root / f"{name}-staged")
    for index, kwargs in enumerate(trials):
        trial_id = f"11111111-1111-1111-1111-{index:012d}"
        _write_trial(job, kwargs.pop("name"), staged=staged, trial_id=trial_id, **kwargs)
    (job / "lab-metadata.json").write_text(json.dumps({
        "task_staging": {
            "source_package_digest": version_digest,
            "source_harbor_digest": harbor_digest,
            "source_task_basename": "qual-task",
        }
    }))
    return job


def test_environment_type_from_config_and_wrappers() -> None:
    assert trial_environment_type({"environment": {"type": "docker"}}) == "docker"
    assert trial_environment_type({"environment": {"type": "daytona"}}) == "daytona"
    assert (
        trial_environment_type(
            {"environment": {
                "type": "docker",
                "import_path": "evallab.harbor_daytona:BoundedDaytonaEnvironment",
            }}
        )
        == "daytona"
    )
    assert (
        trial_environment_type(
            {"environment": {
                "type": "docker",
                "import_path": "evallab.harbor_daytona:SecretSafeDaytonaEnvironment",
            }}
        )
        == "daytona"
    )
    # Config wins; lock is the fallback.
    assert (
        trial_environment_type(
            {"environment": {"type": "docker"}}, {"environment": {"type": "daytona"}}
        )
        == "docker"
    )
    assert (
        trial_environment_type({}, {"environment": {"type": "daytona"}}) == "daytona"
    )
    assert trial_environment_type({}, {}) is None
    assert trial_environment_type(None) is None


def test_extract_trial_fact_records_environment_type(tmp_path: Path) -> None:
    staged = _write_staged_task(tmp_path)
    job = _write_job(
        tmp_path, "job1", [{"name": "t1__a", "env_type": "docker"}], staged=staged
    )
    record = load_job(job)
    assert len(record.trials) == 1
    fact = extract_trial_fact(record, record.trials[0])
    assert fact.environment_type == "docker"


def test_extract_trial_fact_maps_daytona_wrapper(tmp_path: Path) -> None:
    staged = _write_staged_task(tmp_path)
    job = _write_job(
        tmp_path,
        "job2",
        [{
            "name": "t1__a",
            "env_type": "docker",
            "import_path": "evallab.harbor_daytona:BoundedDaytonaEnvironment",
        }],
        staged=staged,
    )
    fact = extract_trial_fact(load_job(job), load_job(job).trials[0])
    assert fact.environment_type == "daytona"


def test_cost_formula_on_known_resources() -> None:
    cost = estimate_cost_usd(
        backend="daytona",
        sandbox_seconds=3600.0,
        cpus=4,
        memory_mb=8192,
        storage_mb=30720,
        rate_backend="daytona",
    )
    assert cost == pytest.approx(4 * 0.0504 + 8 * 0.0162 + 25 * 0.000108)
    assert cost == pytest.approx(0.3339)


def test_cost_null_off_rate_card_and_missing_inputs() -> None:
    assert estimate_cost_usd(
        backend="docker", sandbox_seconds=3600.0, cpus=4,
        memory_mb=8192, storage_mb=30720,
    ) is None
    assert estimate_cost_usd(
        backend="daytona", sandbox_seconds=None, cpus=4,
        memory_mb=8192, storage_mb=30720,
    ) is None
    # Storage under the 5 GiB free tier bills nothing for disk.
    small = estimate_cost_usd(
        backend="daytona", sandbox_seconds=3600.0, cpus=1,
        memory_mb=1024, storage_mb=1024,
    )
    assert small == pytest.approx(1 * 0.0504 + 1 * 0.0162)
    # Unset resources count as 0, never invented.
    assert estimate_cost_usd(
        backend="daytona", sandbox_seconds=3600.0, cpus=None,
        memory_mb=None, storage_mb=None,
    ) == pytest.approx(0.0)


def test_task_dir_for_trial_resolves_source_after_queue_stage_removed(tmp_path: Path) -> None:
    """Queue runs delete the staged copy; the source task path still resolves."""
    checkout = tmp_path / "checkout"
    source = checkout / "derived" / "task-store" / "qual-task"
    source.mkdir(parents=True)
    (source / "task.toml").write_text(TASK_TOML)
    gone = checkout / "runs" / ".exec-stage" / "jobq"
    job = _write_job(checkout / "runs", "jobq", [
        {"name": "t__nop", "agent": "nop", "reward": 0.0, "env_type": "daytona"},
    ], staged=gone)
    (job / "experiment-spec.json").write_text(
        json.dumps({"task": "derived/task-store/qual-task"})
    )
    assert task_dir_for_trial(job / "t__nop") == source
