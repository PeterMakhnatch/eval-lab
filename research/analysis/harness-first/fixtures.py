"""Explicit CPU fixtures only; never submitted to an executor or ingester."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5


def digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def trajectory(label: str, input_tokens: int | None, *, worker: bool = False) -> dict[str, Any]:
    metrics = {
        "prompt_tokens": input_tokens,
        "completion_tokens": 50,
        "cached_tokens": 10,
        "cost_usd": 0.005,
    }
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": label,
        "trajectory_id": label,
        "agent": {
            "name": "fixture-worker" if worker else "fixture-root",
            "version": "fixture-v1",
            "model_name": "fixture/open-root",
        },
        "steps": [
            {"step_id": 1, "source": "user", "message": "CPU fixture input, not a live task"},
            {
                "step_id": 2,
                "source": "agent",
                "message": "CPU fixture output",
                "llm_call_count": 1,
                "metrics": metrics,
            },
        ],
        # Deliberately inclusive/ambiguous summary: analyzer must use disjoint
        # per-generation metrics, not count this plus embedded workers twice.
        "final_metrics": {
            "total_steps": 2,
            "total_prompt_tokens": 999,
            "total_completion_tokens": 999,
            "total_cost_usd": 9.99,
        },
    }


def write_trial(
    job: Path,
    task: str,
    arm: str,
    reward: float | None,
    *,
    attempt: int = 1,
    task_digest: str | None = "default",
    verifier_digest: str | None = "default",
    model: str | None = "fixture/open-root",
    revision: str | None = None,
    exception: str | None = None,
    finished: bool = True,
    workers: bool = False,
    partial_worker: bool = False,
    with_trajectory: bool = True,
    native_tokens: int | None = 100,
) -> Path:
    name = f"{task}__{attempt}"
    directory = job / name
    identity = str(uuid5(NAMESPACE_URL, f"har13-explicit-cpu-fixture:{arm}:{name}"))
    agent = "mini-swe-agent" if arm == "baseline" else "fixture-authors-rlm"
    version = "2.4.6" if arm == "baseline" else "fixture-v1"
    task_digest = digest(task) if task_digest == "default" else task_digest
    verifier_digest = digest("verifier") if verifier_digest == "default" else verifier_digest
    start = datetime(2026, 9, 9, 0, 0, tzinfo=UTC) + timedelta(minutes=attempt)
    result = {
        "id": identity,
        "trial_name": name,
        "task_name": task,
        "task_checksum": task_digest,
        "agent_info": {
            "name": agent,
            "version": version,
            "model_info": {"name": model, "revision": revision},
        },
        "agent_result": {
            "n_input_tokens": native_tokens,
            "n_output_tokens": 50,
            "n_cache_tokens": 10,
            "cost_usd": 0.005 if native_tokens is not None else None,
        },
        "verifier_result": {"rewards": {} if reward is None else {"reward": reward}},
        "exception_info": None
        if exception is None
        else {"exception_type": exception, "exception_message": "Explicit CPU failure fixture"},
        "started_at": start.isoformat(),
        "finished_at": (start + timedelta(seconds=20)).isoformat() if finished else None,
        "agent_execution": {
            "started_at": start.isoformat(),
            "finished_at": (start + timedelta(seconds=15)).isoformat() if finished else None,
        },
    }
    write_json(directory / "result.json", result)
    write_json(directory / "config.json", {"trial_name": name, "agent": {"name": agent}})
    write_json(
        directory / "lock.json",
        {
            "task": {} if task_digest is None else {"digest": task_digest},
            "verifier": {} if verifier_digest is None else {"digest": verifier_digest},
            "environment": {"type": "fixture", "image": digest("fixture-environment")},
            "agent": {
                "name": agent,
                "version": version,
                "model_name": model,
                "model_revision": revision,
            },
        },
    )
    if with_trajectory:
        payload = trajectory(identity, 100 if arm == "baseline" else 200)
        if workers:
            payload["subagent_trajectories"] = [
                trajectory(identity + "-worker", None if partial_worker else 300, worker=True)
            ]
        write_json(directory / "agent/trajectory.json", payload)
    return directory


def generate_cpu_fixtures(output_dir: Path) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError("Fixture destination must be empty; never overwrite native evidence")
    output_dir.mkdir(parents=True, exist_ok=True)
    jobs = {arm: output_dir / f"{arm}-job" for arm in ("baseline", "candidate")}
    for task, before, after in (
        ("task-pos", 0, 1),
        ("task-neu", 1, 1),
        ("task-neg", 1, 0),
        ("task-frac", 0.25, 0.75),
    ):
        write_trial(jobs["baseline"], task, "baseline", before)
        write_trial(jobs["candidate"], task, "candidate", after)
    cases: dict[str, dict[str, Any]] = {
        "task-mismatch-verif": {"verifier_digest": digest("other-verifier")},
        "task-mismatch-task": {"task_digest": digest("other-task")},
        "task-mismatch-model": {"model": "fixture/different-root"},
        "task-mismatch-revision": {"revision": "revision-b"},
        "task-stale-infra": {"exception": "DockerDaemonError"},
        "task-stale-verifier": {"exception": "RewardFileNotFoundError"},
        "task-stale-agent": {"exception": "AgentTimeoutError"},
        "task-partial": {"finished": False},
        "task-subagent": {"workers": True},
        "task-partial-worker": {"workers": True, "partial_worker": True},
        "task-native-only": {"with_trajectory": False, "native_tokens": 600},
        "task-missing-usage": {"with_trajectory": False, "native_tokens": None},
    }
    for task, options in cases.items():
        baseline_options = {"revision": "revision-a"} if task == "task-mismatch-revision" else {}
        write_trial(jobs["baseline"], task, "baseline", 0, **baseline_options)
        write_trial(jobs["candidate"], task, "candidate", 1, **options)
    write_trial(jobs["baseline"], "task-missing-cand", "baseline", 0)
    write_trial(jobs["candidate"], "task-missing-base", "candidate", 1)
    write_trial(jobs["baseline"], "task-ambig", "baseline", 0)
    write_trial(jobs["baseline"], "task-ambig", "baseline", 1, attempt=2)
    write_trial(jobs["candidate"], "task-ambig", "candidate", 1)
    for arm in jobs:
        write_trial(
            jobs[arm], "task-empty-fallback", arm, 0, task_digest=None, verifier_digest=None
        )
    counts = {}
    for arm, job in jobs.items():
        counts[arm] = sum(1 for p in job.iterdir() if p.is_dir())
        write_json(
            job / "result.json",
            {
                "id": str(uuid5(NAMESPACE_URL, "har13-fixture-job:" + arm)),
                "n_total_trials": counts[arm],
                "stats": {"n_completed_trials": counts[arm]},
                "started_at": "2026-09-09T00:00:00Z",
                "finished_at": "2026-09-09T00:10:00Z",
            },
        )
        write_json(job / "config.json", {"job_name": job.name})
        write_json(job / "lock.json", {"harbor": {"version": "fixture-native-record"}})
        write_json(
            job / "lab-metadata.json",
            {"evidence_kind": "fixture", "notice": "Generated CPU data; never a model result"},
        )
    spec = {
        "comparison_id": "harness-first-cpu-fixtures",
        "experiment_id": "har13-cpu-fixtures",
        "declared_variable": "agent_name",
        "mode": "exploratory",
        "pairing_key": "task_name",
        "cohorts": [{"label": arm, "paths": [job.name]} for arm, job in jobs.items()],
    }
    write_json(output_dir / "spec.json", spec)
    return {
        "spec_path": str(output_dir / "spec.json"),
        "counts": counts,
        "evidence_kind": "fixture",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    print(json.dumps(generate_cpu_fixtures(parser.parse_args().output_dir), indent=2))


if __name__ == "__main__":
    main()
