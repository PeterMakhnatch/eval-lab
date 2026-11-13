from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.cohort import pass_at_k_probability, wilson_interval

from .test_atif import _make_job


def _synthetic_job(
    root: Path,
    *,
    suffix: int,
    agent: str,
    reward: float,
    environment: str = "docker",
    task_digest: str = "sha256:task",
    exception: str | None = None,
    started_at: str | None = None,
) -> Path:
    job = _make_job(root, with_trajectory=False)
    job_result_path = job / "result.json"
    job_result = json.loads(job_result_path.read_text())
    job_result["id"] = f"00000000-0000-0000-0000-{suffix:012d}"
    job_result_path.write_text(json.dumps(job_result))
    trial = next(path for path in job.iterdir() if path.is_dir())
    trial_result_path = trial / "result.json"
    trial_result = json.loads(trial_result_path.read_text())
    trial_result["id"] = f"10000000-0000-0000-0000-{suffix:012d}"
    trial_result["agent_info"]["name"] = agent
    trial_result["verifier_result"]["rewards"]["reward"] = reward
    trial_result["exception_info"] = (
        {"exception_type": exception, "exception_message": "fixture"} if exception else None
    )
    if started_at is None:
        trial_result["started_at"] = f"2026-08-14T00:00:{suffix:02d}.000000Z"
    elif started_at == "":
        trial_result.pop("started_at", None)
    else:
        trial_result["started_at"] = started_at
    trial_result_path.write_text(json.dumps(trial_result))
    trial_lock_path = trial / "lock.json"
    trial_lock = json.loads(trial_lock_path.read_text())
    trial_lock["task"]["digest"] = task_digest
    trial_lock["agent"]["name"] = agent
    trial_lock["environment"]["type"] = environment
    trial_lock_path.write_text(json.dumps(trial_lock))
    return job


def test_wilson_interval_known_bounds() -> None:
    assert wilson_interval(0, 0) is None
    lower, upper = wilson_interval(1, 1) or (0.0, 0.0)
    assert lower == pytest.approx(0.20654931437723745)
    assert upper == pytest.approx(1.0)


def test_pass_at_k_probability_independent_attempts() -> None:
    assert pass_at_k_probability(3 / 5, 2) == pytest.approx(0.84)
