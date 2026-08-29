"""Host-only fixed-policy evidence loader; pure recovery scoring lives in verifier_core."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from verifier_core import *  # noqa: F401,F403


@dataclass(frozen=True)
class FixedPolicyEvidence:
    job_dir: Path
    trial_dir: Path
    task_digest: str
    reward: float


def load_fixed_policy_evidence(job_dir: Path | str, staged_task: Path | str) -> FixedPolicyEvidence:
    from evallab.registry import harbor_task_digest
    from evallab.results import load_job

    job_path, task_path = Path(job_dir), Path(staged_task)
    if not job_path.is_dir() or not task_path.is_dir():
        raise ValueError("fixed-policy job or staged task is missing")
    expected_digest = harbor_task_digest(task_path)
    job = load_job(job_path)
    if len(job.trials) != 1:
        raise ValueError(f"fixed-policy run requires exactly one settled trial, got {len(job.trials)}")
    trial = job.trials[0]
    task_lock = trial.lock.get("task") if isinstance(trial.lock, dict) else None
    if not isinstance(task_lock, dict) or task_lock.get("digest") != expected_digest:
        raise ValueError("fixed-policy trial does not bind staged task digest")
    raw_verifier = trial.result.get("verifier_result") if isinstance(trial.result, dict) else None
    raw_rewards = raw_verifier.get("rewards") if isinstance(raw_verifier, dict) else None
    reward = raw_rewards.get("reward") if isinstance(raw_rewards, dict) else None
    if isinstance(reward, bool) or not isinstance(reward, (int, float)):
        raise ValueError("fixed-policy trial has no non-boolean numeric verifier reward")
    return FixedPolicyEvidence(job_path, trial.path, expected_digest, float(reward))
