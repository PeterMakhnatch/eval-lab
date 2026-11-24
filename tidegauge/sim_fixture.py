from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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


BASE = datetime(2026, 11, 23, 20, 0, tzinfo=UTC)
MAX_TURNS = 30
STALL_CHOICES = (120, 300, 450)
NOT_STARTED = {
    "pull-limit": "docker compose build failed: ERROR: unexpected status from HEAD request: 429 Too Many Requests",
    "build-failed": "docker compose build failed: exit status 1",
}
HEALTH_CODES = {
    "stalled": "stalled-seats",
    "lost": "lost-seats",
    "zero-calls": "zero-call-seats",
    "turn-cap": "turn-cap-seats",
    "early-end": "early-seat-end",
}
HEALTH_KINDS = ("clean", "near-stall", *HEALTH_CODES, *NOT_STARTED)
UNMEASURED_KINDS = frozenset({"stalled", "lost", "zero-calls", *NOT_STARTED})


def stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def at(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


@dataclass(frozen=True)
class HealthTrial:
    name: str
    kind: str
    faulty: tuple[str, ...]
    offset: int
    duration: int
    silent: int
    calls: tuple[int, ...]
    zero_form: str = "metadata"

    def expected(self) -> set[tuple[str, str | None]]:
        if self.kind in NOT_STARTED:
            return {("not-started", None)}
        code = HEALTH_CODES.get(self.kind)
        return {(code, seat) for seat in self.faulty} if code else set()


def health_plan(rng: Random, stall: int) -> list[HealthTrial]:
    assert stall in STALL_CHOICES
    kinds = [*HEALTH_KINDS, *(rng.choice(HEALTH_KINDS) for _ in range(rng.randint(0, 4)))]
    rng.shuffle(kinds)
    plan = []
    for index, kind in enumerate(kinds):
        names = [f"p{i}" for i in range(1, rng.randint(2, 5) + 1)]
        count = 1 if kind == "early-end" else rng.randint(1, len(names))
        faulty = () if kind in ("clean", *NOT_STARTED) else tuple(sorted(rng.sample(names, count)))
        duration = rng.randint(stall + 120, stall + 900)
        silent = rng.randint(stall, duration - 60) if kind == "stalled" else rng.randint(1, stall - 1)
        calls = tuple(rng.randint(1, MAX_TURNS - 2) for _ in names)
        form = rng.choice(("metadata", "tokens"))
        plan.append(HealthTrial(f"h{index:02d}__{kind}", kind, faulty, index * 37, duration, silent, calls, form))
    return plan


def _steps(start: datetime, last: datetime, calls: int) -> list[dict]:
    steps: list[dict] = [{"step_id": 1, "source": "user", "timestamp": stamp(start), "message": "play"}]
    span = (last - start).total_seconds()
    for i in range(calls):
        moment = last - timedelta(seconds=span * (calls - 1 - i) / max(calls, 1) * 0.9)
        steps.append({
            "step_id": i + 2,
            "source": "agent",
            "timestamp": stamp(moment),
            "metrics": {"prompt_tokens": 100 + i, "completion_tokens": 10, "cached_tokens": 0},
        })
    return steps


def _health_seat(path: Path, spec: HealthTrial, index: int, name: str, t0: datetime) -> dict:
    fault = name in spec.faulty
    calls, end, exception = spec.calls[index], t0 + timedelta(seconds=spec.duration), None
    if fault and spec.kind in ("lost", "early-end"):
        end = t0 + timedelta(seconds=round(spec.duration * 0.2))
    calls = 0 if fault and spec.kind == "zero-calls" else MAX_TURNS if fault and spec.kind == "turn-cap" else calls
    if fault and spec.kind in ("stalled", "near-stall"):
        exception = "AgentTimeoutError"
    last = end - timedelta(seconds=spec.silent if exception else 1)
    agent = path / "seats" / name / "agent"
    agent.mkdir(parents=True)
    write_json(agent / "trajectory.json", {"schema_version": "ATIF-v1.6", "steps": _steps(t0, last, calls)})
    if fault and spec.kind == "lost":
        (agent / "exit-code.txt").write_text("1\n", encoding="utf-8")
    elif index == 0:
        (agent / "exit-code.txt").write_text("0\n", encoding="utf-8")
    usage: dict[str, object] = {"n_input_tokens": 100 * calls, "n_cache_tokens": 0, "n_output_tokens": 10 * calls}
    if not (fault and spec.kind == "zero-calls" and spec.zero_form == "tokens"):
        turns = calls + 1 if exception else calls
        usage["metadata"] = {"n_episodes": turns, "api_request_times_msec": [900.0] * calls}
    return {
        "seat": name,
        "under_test": True,
        "agent_info": {"name": "sim-agent", "version": "1", "model_info": {"name": "model", "provider": "sim"}},
        "agent_result": usage,
        "agent_execution": {"started_at": stamp(t0), "finished_at": stamp(end)},
        "exception_info": {"exception_type": exception, "exception_message": "timed out"} if exception else None,
    }


def write_health_trial(path: Path, spec: HealthTrial) -> None:
    path.mkdir()
    names = [f"p{i}" for i in range(1, len(spec.calls) + 1)]
    start, t0 = at(spec.offset), at(spec.offset + 30)
    end = t0 + timedelta(seconds=spec.duration)
    participants = [{**AGENT, "seat": name, "kwargs": {"max_turns": MAX_TURNS}} for name in names]
    result: dict[str, object] = {
        "trial_name": spec.name, "task_name": spec.name.split("__")[0], "source": "sim",
        "config": {"task": {"path": "t"}, "agent": {"name": "oracle"}, "participants": participants,
                   "environment": {"type": "docker"}},
        "agent_info": {"name": "oracle"}, "started_at": stamp(start), "finished_at": stamp(end + timedelta(seconds=20)),
    }
    if spec.kind in NOT_STARTED:
        info = {"name": "sim-agent", "version": "1", "model_info": {"name": "model", "provider": "sim"}}
        result["seat_results"] = [
            {"seat": name, "under_test": True, "agent_info": info, "agent_result": None, "agent_execution": None,
             "exception_info": None}
            for name in names
        ]
        result["exception_info"] = {"exception_type": "RuntimeError", "exception_message": NOT_STARTED[spec.kind]}
        result["environment_setup"] = {"started_at": stamp(start), "finished_at": stamp(t0)}
        write_json(path / "result.json", result)
        return
    result["seat_results"] = [_health_seat(path, spec, i, name, t0) for i, name in enumerate(names)]
    result["agent_execution"] = {"started_at": stamp(t0), "finished_at": stamp(end)}
    result["verifier_result"] = {"rewards": {"reward": 0.5}}
    if spec.kind in ("stalled", "near-stall"):
        result["exception_info"] = {"exception_type": "AgentTimeoutError", "exception_message": "timed out",
                                    "policy": {"should_grade": True, "has_valid_reward": True}}
    write_json(path / "result.json", result)


def write_health_job(path: Path, plan: list[HealthTrial]) -> None:
    write_job(path, len(plan))
    for spec in plan:
        write_health_trial(path / spec.name, spec)


@dataclass(frozen=True)
class TimedTrial:
    job: str
    name: str
    start: datetime
    end: datetime
    own: bool


@dataclass
class Timeline:
    trials: list[TimedTrial] = field(default_factory=list)
    lanes: list[list[str]] = field(default_factory=list)
    reruns: dict[str, tuple[str, int]] = field(default_factory=dict)


def _pool(rng: Random, job: str, start: datetime, own: bool) -> list[TimedTrial]:
    slots = [start] * rng.randint(1, 4)
    out = []
    for i in range(rng.randint(2, 8)):
        slots.sort()
        begin = slots[0] + timedelta(seconds=rng.randint(0, 3))
        slots[0] = begin + timedelta(seconds=rng.randint(30, 600))
        out.append(TimedTrial(job, f"{job}-t{i}__{rng.randint(0, 10**6):06d}", begin, slots[0], own))
    return out


def timeline_plan(rng: Random) -> Timeline:
    plan = Timeline()
    for lane in range(rng.randint(1, 3)):
        clock, names = at(rng.randint(0, 900)), []
        for index in range(rng.randint(1, 3)):
            names.append(f"arm{lane}{index}-{rng.choice(('solo', 'team', 'scout'))}")
            if rng.random() < 0.3:
                round_ = rng.randint(1, 3)
                names.append(f"{names[-1]}.r{round_}-m{rng.randint(1, 4)}")
                plan.reruns[names[-1]] = (names[-2], round_)
        for name in names:
            trials = _pool(rng, name, clock, True)
            plan.trials += trials
            clock = max(t.end for t in trials) + timedelta(seconds=rng.randint(1, 300))
        plan.lanes.append(names)
    for index in range(rng.randint(0, 2)):
        plan.trials += _pool(rng, f"other{index}", at(rng.randint(0, 3000)), False)
    return plan


def write_timeline(root: Path, plan: Timeline) -> tuple[Path, Path]:
    own, also = root / "own", root / "also"
    own.mkdir()
    also.mkdir()
    for job in dict.fromkeys((t.job, t.own) for t in plan.trials):
        members = [t for t in plan.trials if (t.job, t.own) == job]
        directory = (own if job[1] else also) / job[0]
        write_job(directory, len(members))
        for trial in members:
            (directory / trial.name).mkdir()
            write_json(directory / trial.name / "result.json", {
                "trial_name": trial.name, "task_name": trial.name.split("__")[0],
                "started_at": stamp(trial.start), "finished_at": stamp(trial.end),
                "verifier_result": {"rewards": {"reward": 1.0}},
            })
    return own, also


@dataclass
class LiveCase:
    now: datetime
    planned: int | None
    counts: dict[str, int]
    stalled: set[tuple[str, str]]
    job_unreadable: bool


def _running_trial(path: Path, rng: Random, now: datetime, stall: int) -> set[tuple[str, str]]:
    path.mkdir()
    names = [f"p{i}" for i in range(1, rng.randint(1, 4) + 1)]
    write_json(path / "config.json", {"trial_name": path.name, "task": {"path": "t"},
                                      "participants": [{**AGENT, "seat": name} for name in names]})
    age = rng.randint(stall + 60, stall + 1200)
    flagged = set()
    for name in names:
        state = rng.choice(("recent", "silent", "missing", "truncated", "user-only", "done", "exited"))
        agent = path / "seats" / name / "agent"
        if state == "missing":
            continue
        agent.mkdir(parents=True)
        if state == "truncated":
            (agent / "trajectory.json").write_bytes(b'{"schema_version": "ATIF-v1.6", "steps": [{"timest')
            continue
        quiet = state in ("silent", "done", "exited")
        silent = rng.randint(stall + 1, age - 5) if quiet else rng.randint(0, stall - 1)
        last = now - timedelta(seconds=silent)
        steps = _steps(now - timedelta(seconds=age - 2), last, 0 if state == "user-only" else rng.randint(1, 5))
        if state == "done":
            steps[-1]["tool_calls"] = [{"tool_call_id": "c", "function_name": "mark_task_complete", "arguments": {}}]
        if state == "exited":
            (agent / "exit-code.txt").write_text("0\n", encoding="utf-8")
        write_json(agent / "trajectory.json", {"schema_version": "ATIF-v1.6", "steps": steps})
        if state in ("silent", "user-only"):
            flagged.add((path.name, name))
    started = (now - timedelta(seconds=age)).timestamp()
    os.utime(path / "config.json", (started, started))
    return flagged


def write_live_job(path: Path, rng: Random, stall: int) -> LiveCase:
    now = at(7200)
    sizes = {key: rng.randint(low, high) for key, low, high in
             (("finished", 0, 4), ("failed", 0, 2), ("running", 1, 4), ("unreadable", 0, 1), ("hidden", 0, 1))}
    planned = sum(sizes.values()) + rng.randint(0, 3)
    unreadable_job = rng.random() < 0.25
    path.mkdir()
    if unreadable_job:
        (path / "result.json").write_bytes(b'{"n_total_trials": ')
    else:
        write_json(path / "result.json", {"id": path.name, "n_total_trials": planned, "stats": {"n_retries": 0}})
    for i in range(sizes["finished"] + sizes["failed"]):
        kind = "clean" if i < sizes["finished"] else "pull-limit"
        write_health_trial(path / f"done{i}__{kind}", HealthTrial(f"done{i}__{kind}", kind, (), i * 11, 400, 1, (3, 3)))
    stalled: set[tuple[str, str]] = set()
    for i in range(sizes["running"]):
        stalled |= _running_trial(path / f"run{i}__live", rng, now, stall)
    for i in range(sizes["unreadable"]):
        (path / f"half{i}__live").mkdir()
        write_json(path / f"half{i}__live" / "config.json", {"trial_name": f"half{i}", "task": {"path": "t"}})
        (path / f"half{i}__live" / "result.json").write_bytes(b'{"trial_name": "half", ')
    for i in range(sizes["hidden"]):
        (path / f"hidden{i}__live").mkdir()
        (path / f"hidden{i}__live" / "config.json").write_bytes(b'{"trial_na')
    counts = {key: sizes[key] for key in ("finished", "failed", "running", "unreadable")}
    return LiveCase(now, None if unreadable_job else planned, counts, stalled, unreadable_job)
