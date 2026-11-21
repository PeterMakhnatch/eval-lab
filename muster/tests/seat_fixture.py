"""Write synthetic seat-trial directories, in the shape of the Harbor fork's seat trials.

    trial = write_trial(tmp_path, "t__001", {"p1": "oracle", "p2": "nop"}, rewards={"reward": 0.0})
    write_matrix(tmp_path / "m", {"p1=oracle,p2=oracle": [1.0], "p1=oracle,p2=nop": [0.0]})

A policy is `oracle`, `nop`, a script policy name (ScriptAgent with <SCRIPTS>/<name>.sh), or a dict
of participant settings for a real agent, for example {"name": "terminus-2", "model_name": "x/y"}.

    task = write_task(tmp_path, "duo", {"p1": True, "p2": False}, policies=("spoof",))
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCRIPT_AGENT = "muster.agents.script:ScriptAgent"
SCRIPTS = "/policies"
TIMEOUT = "AgentTimeoutError"


def participant(seat: str, policy: str | dict[str, Any]) -> dict[str, Any]:
    base: dict[str, Any] = {"seat": seat, "name": None, "import_path": None, "model_name": None, "kwargs": {}}
    if isinstance(policy, dict):
        return {**base, **policy}
    if policy in ("oracle", "nop"):
        return {**base, "name": policy}
    return {**base, "import_path": SCRIPT_AGENT, "kwargs": {"script": f"{SCRIPTS}/{policy}.sh"}}


def exception_info(kind: str | None) -> dict[str, Any] | None:
    if kind is None:
        return None
    return {
        "exception_type": kind,
        "exception_message": f"{kind} in a synthetic trial",
        "exception_traceback": "",
        "occurred_at": "2026-11-19T08:38:37.720023Z",
    }


def write_trial(
    root: Path,
    name: str,
    seats: dict[str, str | dict[str, Any]],
    *,
    rewards: dict[str, Any] | None = None,
    task: str = "harbor/hello-seats",
    not_tested: tuple[str, ...] = ("p2",),
    exception: str | None = None,
    seat_exceptions: dict[str, str] | None = None,
    reward_valid: bool | None = None,
    usage: dict[str, dict[str, Any]] | None = None,
    trajectories: tuple[str, ...] = (),
    checksum: str = "f8ca14185fadd92a",
) -> Path:
    """Write <root>/<name>/ with result.json, config.json and the seat, artifact and verifier folders.

    `rewards=None` writes a trial without a verifier result. `reward_valid=None` derives the flag
    as Harbor does for these cases: a reward exists and no exception other than a timeout occurred.
    """
    trial = Path(root) / name
    seat_exceptions = seat_exceptions or {}
    listed = [participant(seat, policy) for seat, policy in seats.items()]
    seat_results = []
    for entry in listed:
        seat = entry["seat"]
        agent = entry["name"] or "script"
        provider, _, model = (entry["model_name"] or "").rpartition("/")
        (trial / "seats" / seat / "agent").mkdir(parents=True, exist_ok=True)
        if seat in trajectories:
            (trial / "seats" / seat / "agent" / "trajectory.json").write_text("{}\n")
        tokens = {"n_input_tokens": None, "n_cache_tokens": None, "n_output_tokens": None, "cost_usd": None}
        seat_results.append(
            {
                "seat": seat,
                "under_test": seat not in not_tested,
                "agent_info": {
                    "name": agent,
                    "version": "1.0.0",
                    "model_info": {"name": model, "provider": provider or None} if model else None,
                },
                "agent_result": {**tokens, **(usage or {}).get(seat, {}), "metadata": None},
                "agent_setup": {
                    "started_at": "2026-11-19T08:38:37.285709Z",
                    "finished_at": "2026-11-19T08:38:37.437964Z",
                },
                "agent_execution": {
                    "started_at": "2026-11-19T08:38:37.438255Z",
                    "finished_at": "2026-11-19T08:38:37.720023Z",
                },
                "exception_info": exception_info(seat_exceptions.get(seat)),
            }
        )
    kinds = {exception, *seat_exceptions.values()} - {None}
    if reward_valid is None:
        reward_valid = bool(rewards) and kinds <= {TIMEOUT}
    config = {
        "task": {"path": f"/tasks/{task.rsplit('/', 1)[-1]}"},
        "trial_name": name,
        "trials_dir": str(Path(root)),
        "participants": listed,
        "environment": {"type": "docker", "delete": True},
    }
    result = {
        "schema_version": 1,
        "task_name": task,
        "trial_name": name,
        "task_checksum": checksum,
        "config": config,
        "agent_info": seat_results[0]["agent_info"],
        "verifier_result": None if rewards is None else {"rewards": rewards},
        "exception_info": exception_info(exception),
        "started_at": "2026-11-19T08:38:21.471157Z",
        "finished_at": "2026-11-19T08:38:42.720805Z",
        "seat_results": seat_results,
        "reward_valid": reward_valid,
    }
    for folder in ("artifacts", "verifier"):
        (trial / folder).mkdir(parents=True, exist_ok=True)
    (trial / "config.json").write_text(json.dumps(config, indent=4))
    (trial / "result.json").write_text(json.dumps(result, indent=4))
    return trial


def parse_roster(key: str) -> dict[str, str]:
    """ "p1=oracle,p2=nop" -> {"p1": "oracle", "p2": "nop"}."""
    return dict(part.split("=", 1) for part in key.split(","))


def write_job(
    job_dir: Path, seats: dict[str, str | dict[str, Any]], attempts: list[Any], **settings: Any
) -> list[Path]:
    """A Harbor job directory with one trial for each attempt.

    An attempt is the value of `reward`, a dict of rewards, or a dict of write_trial settings
    (when it has the key "rewards"). The job's own result.json and config.json are written too.
    """
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "config.json").write_text(
        json.dumps({"job_name": job_dir.name, "jobs_dir": str(job_dir.parent)})
    )
    (job_dir / "result.json").write_text(json.dumps({"n_total_trials": len(attempts), "stats": {}}))
    trials = []
    for index, attempt in enumerate(attempts, start=1):
        if isinstance(attempt, dict) and "rewards" in attempt:
            extra = attempt
        elif isinstance(attempt, dict):
            extra = {"rewards": attempt}
        else:
            extra = {"rewards": {"reward": attempt}}
        trials.append(write_trial(job_dir, f"task__{index:03d}", seats, **{**settings, **extra}))
    return trials


def write_matrix(
    out: Path, table: dict[str, list[Any]], *, manifest: dict[str, Any] | None = None, **settings: Any
) -> Path:
    """The --out layout of `muster matrix`: <out>/<roster>/jobs/<roster>/<trial>/ for each roster.

    `manifest` adds a matrix.json with the rosters of `table` and the given keys.
    """
    out = Path(out)
    for key, attempts in table.items():
        write_job(out / key / "jobs" / key, parse_roster(key), attempts, **settings)
    if manifest is not None:
        document = {"rosters": [{"roster": key} for key in table], **manifest}
        (out / "matrix.json").write_text(json.dumps(document, indent=2))
    return out


def write_task(
    root: Path,
    name: str,
    seats: dict[str, bool],
    *,
    metadata: str = "",
    policies: tuple[str, ...] = (),
    agent_timeout: float | None = 600.0,
) -> Path:
    """A seat task directory with task.toml and policies/<name>.sh. `seats` maps a seat to under_test."""
    task = Path(root) / name
    (task / "policies").mkdir(parents=True, exist_ok=True)
    lines = [
        'schema_version = "1.4"',
        "",
        "[task]",
        f'name = "muster/{name}"',
        "",
        "[verifier]",
        "timeout_sec = 120.0",
    ]
    lines += ["", "[agent]"] + ([f"timeout_sec = {agent_timeout}"] if agent_timeout is not None else [])
    if metadata:
        lines += ["", "[metadata]", metadata]
    for seat, under_test in seats.items():
        lines += ["", f"[agents.{seat}]"] + ([] if under_test else ["under_test = false"])
    (task / "task.toml").write_text("\n".join(lines) + "\n")
    for policy in policies:
        (task / "policies" / f"{policy}.sh").write_text("#!/bin/bash\nexit 0\n")
    return task
