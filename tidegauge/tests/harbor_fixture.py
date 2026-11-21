"""Write small Harbor job directories for tests.

The files have the shape that Harbor writes: a job directory with ``result.json``
and ``lock.json``, and one directory per trial with its own ``result.json``.
``modern=True`` adds the fields that Harbor 0.26 stores (``reward_valid``, the
exception ``policy``). Without it the result looks like one from early 2026.
``seat_trial`` writes a trial of the olamlabs/harbor-multiagent fork, with 1 agent per seat.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tidegauge.load import INVALID_REWARD_EXCEPTIONS

T0 = "2026-02-05T16:00:00.000000Z"
T1 = "2026-02-05T16:10:00.000000Z"


def trial(
    task: str,
    reward: float | None,
    *,
    exception: str | None = None,
    agent: str = "terminus-2",
    version: str = "2.0.0",
    model: str = "anthropic/claude-opus-4-6",
    environment: str = "docker",
    source: str = "terminal-bench",
    checksum: str | None = None,
    tokens: tuple[int, int, int] | None = (1000, 400, 200),
    cost: float | None = 0.05,
    modern: bool = False,
    reward_valid: bool | None = None,
    config: dict[str, Any] | None = None,
    started_at: str = T0,
    finished_at: str = T1,
    rewards: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One trial ``result.json`` as a dictionary."""
    if rewards is None and reward is not None:
        rewards = {"reward": reward}
    trial_config: dict[str, Any] = {
        "task": {"path": task, "source": source},
        "timeout_multiplier": 1.0,
        "agent": {"name": agent, "model_name": model, "kwargs": {}},
        "environment": {"type": environment},
        "verifier": {"disable": False},
    }
    for dotted, value in (config or {}).items():
        target = trial_config
        *parents, leaf = dotted.split(".")
        for part in parents:
            target = target.setdefault(part, {})
        target[leaf] = value
    result: dict[str, Any] = {
        "task_name": task,
        "trial_name": "",  # set by write_job
        "task_id": {"path": task},
        "source": source,
        "task_checksum": checksum or f"sum-{task.rsplit('/', 1)[-1]}",
        "config": trial_config,
        "agent_info": {"name": agent, "version": version, "model_info": {"name": model.split("/")[-1]}},
        "agent_result": None,
        "verifier_result": {"rewards": rewards} if rewards is not None else None,
        "exception_info": None,
        "started_at": started_at,
        "finished_at": finished_at,
        "environment_setup": {"started_at": started_at, "finished_at": started_at},
        "agent_execution": {"started_at": started_at, "finished_at": finished_at},
    }
    if tokens is not None:
        result["agent_result"] = {
            "n_input_tokens": tokens[0],
            "n_cache_tokens": tokens[1],
            "n_output_tokens": tokens[2],
            "cost_usd": cost,
        }
    if exception is not None:
        result["exception_info"] = {"exception_type": exception, "exception_message": "boom"}
    if modern:
        result["schema_version"] = 1
        if reward_valid is None:
            reward_valid = rewards is not None and exception not in INVALID_REWARD_EXCEPTIONS
        result["reward_valid"] = reward_valid
    elif reward_valid is not None:
        result["reward_valid"] = reward_valid
    return result


def atif(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """An ATIF trajectory. Each call is the ``metrics`` of 1 model call. ``at`` is its time in seconds after 16:00."""
    steps: list[dict[str, Any]] = [{"source": "user", "message": "do the task"}]
    for metrics in calls:
        at = metrics.get("at")
        step = {
            "source": "agent",
            "message": "ok",
            "metrics": {k: v for k, v in metrics.items() if k != "at"},
        }
        if at is not None:
            step["timestamp"] = f"2026-02-05T16:{at // 60:02d}:{at % 60:02d}Z"
        steps.append(step)
    return {
        "schema_version": "ATIF-v1.6",
        "steps": [{"step_id": i, **s} for i, s in enumerate(steps, start=1)],
    }


def seat(
    name: str,
    *,
    agent: str = "terminus-2",
    version: str = "2.0.0",
    model: str | None = "deepseek/deepseek-flash",
    under_test: bool = True,
    tokens: tuple[int | None, int | None, int | None] = (1000, 400, 200),
    cost: float | None = 0.05,
    exception: str | None = None,
    kwargs: dict[str, Any] | None = None,
    calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One seat of a seat trial: its ``seat_results`` record, its participant and its model calls.

    ``model=None`` with ``tokens=(None, None, None)`` and ``cost=None`` is a seat without a model,
    such as Harbor's oracle. ``calls`` becomes ``seats/<name>/agent/trajectory.json``.
    """
    provider, _, model_name = (model or "").rpartition("/")
    record = {
        "seat": name,
        "under_test": under_test,
        "agent_info": {
            "name": agent,
            "version": version,
            "model_info": {"name": model_name, "provider": provider or None} if model else None,
        },
        "agent_result": {
            "n_input_tokens": tokens[0],
            "n_cache_tokens": tokens[1],
            "n_output_tokens": tokens[2],
            "cost_usd": cost,
            "model_usage": None,
            "rollout_details": None,
            "metadata": None,
        },
        "agent_setup": {"started_at": T0, "finished_at": T0},
        "agent_execution": {"started_at": T0, "finished_at": T1},
        "exception_info": None,
    }
    if exception is not None:
        record["exception_info"] = {"exception_type": exception, "exception_message": "boom"}
    participant = {
        "name": agent,
        "import_path": None,
        "model_name": model,
        "kwargs": kwargs or {},
        "seat": name,
    }
    return {"record": record, "participant": participant, "calls": calls}


def seat_trial(
    task: str, reward: float | None, seats: list[dict[str, Any]], *, modern: bool = True, **kwargs: Any
) -> dict[str, Any]:
    """One trial ``result.json`` of the fork, from a list of ``seat(...)``.

    The shape follows a real seat trial. Usage and exceptions are in ``seat_results``. The
    top-level ``agent_result`` is null, ``agent_info`` names the first seat under test, and
    ``config.agent`` is Harbor's default agent, which did not run. Harbor copies the first seat
    exception to the trial, and ``reward_valid`` looks at every seat exception.
    """
    errors = [e["exception_type"] for s in seats if (e := s["record"]["exception_info"])]
    kwargs.setdefault("exception", errors[0] if errors else None)
    result = trial(task, reward, tokens=None, modern=modern, **kwargs)
    if modern and kwargs.get("reward_valid") is None:
        result["reward_valid"] = result["reward_valid"] and not INVALID_REWARD_EXCEPTIONS.intersection(errors)
    result["agent_info"] = next(s["record"]["agent_info"] for s in seats if s["record"]["under_test"])
    result["config"]["agent"] = {"name": "oracle", "import_path": None, "model_name": None, "kwargs": {}}
    result["config"]["participants"] = [s["participant"] for s in seats]
    result["step_results"] = None
    result["seat_results"] = [s["record"] for s in seats]
    result["files"] = {
        f"seats/{s['record']['seat']}/agent/trajectory.json": atif(s["calls"])
        for s in seats
        if s["calls"] is not None
    }
    return result


def write_job(
    root: Path,
    name: str,
    trials: list[dict[str, Any]],
    *,
    harbor_version: str | None = None,
    planned: int | None = None,
    retries: int = 0,
    job_files: bool = True,
) -> Path:
    """Write one job directory under ``root`` and return its path."""
    job = root / name
    job.mkdir(parents=True, exist_ok=True)
    if job_files:
        stats = {"n_retries": retries}
        total = len(trials) if planned is None else planned
        (job / "result.json").write_text(json.dumps({"id": name, "n_total_trials": total, "stats": stats}))
        (job / "config.json").write_text(json.dumps({"job_name": name}))
        if harbor_version is not None:
            (job / "lock.json").write_text(json.dumps({"harbor": {"version": harbor_version}}))
    for index, result in enumerate(trials):
        result = dict(result)
        files = result.pop("files", {})  # more files of the trial directory: relative path -> JSON content
        short = result["task_name"].rsplit("/", 1)[-1][:32]
        result["trial_name"] = f"{short}__{index:04d}"
        directory = job / result["trial_name"]
        directory.mkdir()
        (directory / "result.json").write_text(json.dumps(result))
        for relative, content in files.items():
            (directory / relative).parent.mkdir(parents=True)
            (directory / relative).write_text(json.dumps(content))
    return job


def arm(
    root: Path,
    name: str,
    outcomes: dict[str, list[float | None]],
    *,
    seats: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> Path:
    """Write a job from ``task -> rewards`` and return its path. Every trial gets its own end time.

    With ``seats``, every trial is a seat trial with these seats.
    """
    trials = []
    for task, rewards in outcomes.items():
        for reward in rewards:
            minute, second = divmod(len(trials), 60)
            stamp = f"2026-02-05T17:{minute % 60:02d}:{second:02d}Z"
            if seats is None:
                trials.append(trial(task, reward, finished_at=stamp, **kwargs))
            else:
                trials.append(seat_trial(task, reward, seats, finished_at=stamp, **kwargs))
    return write_job(root, name, trials)
