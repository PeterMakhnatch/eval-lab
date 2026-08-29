"""Host-only fixed-policy evidence loader without verifier_core/cryptography imports."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from evallab.registry import harbor_task_digest
from evallab.results import load_job


@dataclass(frozen=True)
class FixedPolicyEvidence:
    """Typed evidence for one real Harbor fixed-policy run (not TaskControlEvidence)."""

    job_dir: Path
    trial_dir: Path
    task_digest: str
    reward: float


def load_fixed_policy_evidence(job_dir: Path | str, staged_task: Path | str) -> FixedPolicyEvidence:
    """Load exactly one settled fixed-policy trial bound to the staged task digest.

    Uses the canonical `evallab.results.load_job` settled-shape contract:
    job `result.json` must include n_total_trials, stats, and non-null finished_at.
    This intentionally imports only evallab results and does not import verifier_core
    or envelope cryptography so the host environment remains dependency-free.
    """
    job_path = Path(job_dir)
    task_path = Path(staged_task)
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

    return FixedPolicyEvidence(
        job_dir=job_path,
        trial_dir=trial.path,
        task_digest=expected_digest,
        reward=float(reward),
    )
