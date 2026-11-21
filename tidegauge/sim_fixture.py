from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from random import Random

Outcome = dict[str, list[float]]
START = "2026-02-05T16:00:00Z"
AGENT = {"name": "sim-agent", "model_name": "sim/model"}
USAGE = {"n_input_tokens": 280, "n_cache_tokens": 130, "n_output_tokens": 30, "cost_usd": 0.03}
FAULTS = (
    "missing-reward-file",
    "malformed-result",
    "truncated-result",
    "null-reward",
    "nan-reward",
    "infinite-reward",
    "out-of-range-reward",
    "exception",
    "empty-directory",
    "unfinished-directory",
    "duplicate-trial",
    "non-utf8-result",
    "missing-trajectory",
)


@dataclass(frozen=True)
class TrialSpec:
    task: str
    index: int
    reward: float | None
    seats: int
    fault: str = ""

    @property
    def name(self) -> str:
        return f"{self.task}__{self.index:03d}"


@dataclass(frozen=True)
class Scenario:
    arms: list[Outcome]
    seats: list[int]
    attempts: list[int]
    kind: str
    fault: str


def draw(rng: Random, probabilities: list[float], attempts: list[int]) -> Outcome:
    assert 1 <= len(probabilities) == len(attempts) <= 48
    assert all(0 <= p <= 1 for p in probabilities)
    assert all(1 <= n <= 4 for n in attempts)
    return {
        f"t{i:03d}": [float(rng.random() < p) for _ in range(n)]
        for i, (p, n) in enumerate(zip(probabilities, attempts, strict=True))
    }


def scenario(seed: int) -> Scenario:
    assert 0 <= seed < 2**64
    rng = Random(seed)
    kind = ("null", "extremes", "power", "mixed")[seed % 4]
    n_tasks = rng.randint(16, 20) if kind == "power" else rng.randint(3, 9)
    n_arms = rng.randint(2, 4) if kind != "mixed" else rng.randint(1, 4)
    attempts = [rng.randint(3, 4) if kind == "power" else rng.randint(1, 4) for _ in range(n_tasks)]
    if kind == "mixed" and seed % 8 == 3:
        attempts = [1] * n_tasks
    common = [rng.choice((0.0, 0.25, 0.5, 0.75, 1.0)) for _ in range(n_tasks)]
    profiles = [common, common]
    if kind == "extremes":
        profiles = [[0.0] * n_tasks, [1.0] * n_tasks]
    if kind == "power":
        profiles = [[rng.uniform(0.08, 0.16) for _ in range(n_tasks)],
                    [rng.uniform(0.84, 0.92) for _ in range(n_tasks)]]
    for _ in range(n_arms - 2):
        profiles.append([rng.random() for _ in range(n_tasks)])
    arms = [draw(rng, profile, attempts) for profile in profiles[:n_arms]]
    seats = [0 if i == 0 else rng.choice((0, 2, 3)) for i in range(n_arms)]
    if n_arms >= 3:
        seats[1] = seats[2] = rng.choice((2, 3))
    fault = FAULTS[seed] if seed < len(FAULTS) else rng.choice(FAULTS)
    return Scenario(arms, seats, attempts, kind, fault)


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def write_job(path: Path, planned: int) -> None:
    assert 1 <= planned <= 200
    path.mkdir()
    write_json(path / "result.json", {"id": path.name, "n_total_trials": planned, "stats": {"n_retries": 0}})
    write_json(path / "lock.json", {"harbor": {"version": "0.26.0"}})


def trajectory(directory: Path) -> None:
    agent = directory / "agent"
    agent.mkdir(parents=True)
    steps = [{"source": "user", "step_id": 1, "message": "run the task"}]
    for step, (prompt, cache, output) in enumerate(((100, 30, 10), (180, 100, 20)), start=2):
        steps.append({
            "source": "agent",
            "step_id": step,
            "timestamp": f"2026-02-05T16:00:{step:02d}Z",
            "metrics": {"prompt_tokens": prompt, "cached_tokens": cache, "completion_tokens": output},
        })
    write_json(agent / "trajectory.json", {"schema_version": "ATIF-v1.6", "steps": steps})


def write_trial(path: Path, spec: TrialSpec) -> None:
    assert 0 <= spec.index <= 200 and spec.seats in (0, 2, 3)
    assert spec.fault in ("", *FAULTS)
    path.mkdir()
    result_path = path / "result.json"
    broken = {"malformed-result": b"{not json}", "truncated-result": b'{"trial_name":', "non-utf8-result": b"\xff"}
    if spec.fault in broken:
        result_path.write_bytes(broken[spec.fault])
        return
    if spec.fault == "unfinished-directory":
        write_json(path / "config.json", {"trial_name": spec.name, "task": {"path": spec.task}})
        return
    if spec.fault == "empty-directory":
        return
    reward = {"null-reward": None, "nan-reward": float("nan"), "infinite-reward": float("inf"),
              "out-of-range-reward": 1.25, "missing-reward-file": None}.get(spec.fault, spec.reward)
    error = {"missing-reward-file": "RewardFileNotFoundError", "exception": "ApiRateLimitError"}.get(spec.fault)
    end = f"2026-02-05T16:{10 + spec.index // 60:02d}:{spec.index % 60:02d}Z"
    config: dict[str, object] = {"task": {"path": spec.task}, "agent": AGENT, "environment": {"type": "docker"}}
    result: dict[str, object] = {
        "trial_name": spec.name, "task_name": spec.task, "task_id": {"path": spec.task},
        "source": "sim", "task_checksum": f"sum-{spec.task}", "config": config,
        "agent_info": {"name": "sim-agent", "version": "1", "model_info": {"name": "model", "provider": "sim"}},
        "agent_result": USAGE,
        "verifier_result": None if spec.fault == "missing-reward-file" else {"rewards": {"reward": reward}},
        "exception_info": {"exception_type": error, "exception_message": "injected fault"} if error else None,
        "started_at": START, "finished_at": end,
        "agent_execution": {"started_at": START, "finished_at": end},
    }
    if spec.fault == "out-of-range-reward":
        result["reward_valid"] = False
    if spec.seats:
        add_seats(path, config, result, spec)
    elif spec.fault != "missing-trajectory":
        trajectory(path)
    if spec.fault != "missing-reward-file":
        verifier = path / "verifier"
        verifier.mkdir()
        write_json(verifier / "reward.json", {"reward": reward})
    write_json(result_path, result)


def add_seats(path: Path, config: dict[str, object], result: dict[str, object], spec: TrialSpec) -> None:
    info = result["agent_info"]
    result["agent_result"] = None
    config["agent"] = {"name": "oracle"}
    names = [f"p{i}" for i in range(1, spec.seats + 1)]
    config["participants"] = [{**AGENT, "seat": name, "kwargs": {}} for name in names]
    result["seat_results"] = [
        {"seat": name, "under_test": True, "agent_info": info, "agent_result": USAGE, "exception_info": None}
        for name in names
    ]
    for i, name in enumerate(names):
        if i != 0 or spec.fault != "missing-trajectory":
            trajectory(path / "seats" / name)


def write_arm(path: Path, outcomes: Outcome, seats: int) -> None:
    total = sum(len(rewards) for rewards in outcomes.values())
    write_job(path, total)
    for task, rewards in outcomes.items():
        for i, reward in enumerate(rewards):
            spec = TrialSpec(task, i, reward, seats)
            write_trial(path / spec.name, spec)


def write_fault_arm(path: Path, fault: str, seats: int) -> None:
    write_job(path, 1 if fault == "empty-directory" else 2)
    control = TrialSpec("t000", 0, 1.0, seats)
    write_trial(path / control.name, control)
    index = 0 if fault == "duplicate-trial" else 1
    write_trial(path / "fault", TrialSpec("t000", index, 1.0, seats, fault))
