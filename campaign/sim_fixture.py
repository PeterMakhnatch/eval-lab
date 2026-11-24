from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from random import Random
from typing import Any

BASE = datetime(2026, 11, 24, tzinfo=UTC)
MICRO = 1_000_000
REWARDED = frozenset({"ok", "stalled", "unknown-cost"})
FAULT_KINDS = ("ok", "pull-limit", "stalled", "no-reward", "unknown-cost", "unreadable", "slow")
PROBE_MODES = ("steady", "steady", "steady", "dead", "flaky", "poor", "falling")
MAX_JOB_EVENTS = 100_000
SIGTERM_EXIT = -15


def iso(seconds: float, precise: bool = True) -> str:
    stamp = BASE + timedelta(seconds=seconds)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ" if precise else "%Y-%m-%dT%H:%M:%SZ")


def usd(micro: int) -> float:
    return micro / MICRO


@dataclass(frozen=True)
class ArmSpec:
    name: str
    lane: str
    instances: tuple[str, ...]
    attempts: int
    reserve_micro: int
    pilot: bool


@dataclass(frozen=True)
class LaneSpec:
    name: str
    concurrency: int
    arms: tuple[str, ...]


@dataclass(frozen=True)
class Scenario:
    seed: int
    lanes: tuple[LaneSpec, ...]
    arms: tuple[ArmSpec, ...]
    total: int
    floor_micro: int
    cap_micro: int
    unknown_seat_micro: int
    preexisting_micro: int
    streak: int
    poll: int
    max_arm_minutes: int
    faults: tuple[tuple[str, float], ...]
    crash_rate: float
    probe_mode: str
    balance_micro: int
    drop_at: float
    drop_micro: int
    detector: bool
    detector_fail: bool
    plan_edit_at: float | None
    stop_file_at: float | None
    kill_at: float | None
    kill_delay: float
    duplicate: bool
    per_lane_runners: bool
    spawn_fail_arm: str
    clears_stop: bool
    second_fill: bool

    def arm(self, name: str) -> ArmSpec:
        found = [arm for arm in self.arms if arm.name == name]
        assert len(found) == 1
        return found[0]


def _layout(rng: Random) -> tuple[tuple[LaneSpec, ...], tuple[ArmSpec, ...]]:
    lanes: list[LaneSpec] = []
    arms: list[ArmSpec] = []
    for index in range(rng.randint(1, 3)):
        lane = f"l{index}"
        names = tuple(f"{lane}-arm{k}" for k in range(rng.randint(1, 3)))
        lanes.append(LaneSpec(lane, rng.randint(1, 4), names))
        for position, name in enumerate(names):
            instances = tuple(f"task-i{n:02d}" for n in range(1, rng.randint(1, 4) + 1))
            pilot = position == 0 and len(names) > 1 and rng.random() < 0.25
            arms.append(ArmSpec(name, lane, instances, rng.randint(1, 3),
                                rng.choice((50_000, 250_000, 500_000, 1_100_000)), pilot))
    return tuple(lanes), tuple(arms)


def _faults(rng: Random) -> tuple[tuple[str, float], ...]:
    profile = rng.choice(("clean", "clean", "mixed", "hostile", "unbilled"))
    if profile == "clean":
        return (("ok", 1.0),)
    if profile == "unbilled":
        return (("ok", 0.6), ("unknown-cost", 0.25), ("unreadable", 0.15))
    if profile == "mixed":
        return (("ok", 0.7), ("pull-limit", 0.08), ("stalled", 0.06), ("no-reward", 0.05),
                ("unknown-cost", 0.05), ("unreadable", 0.03), ("slow", 0.03))
    return (("ok", 0.3), ("pull-limit", 0.35), ("stalled", 0.1), ("no-reward", 0.15),
            ("unknown-cost", 0.05), ("unreadable", 0.05))


def scenario(seed: int) -> Scenario:
    assert 0 <= seed < 2**64
    rng = Random(seed)
    lanes, arms = _layout(rng)
    at_once = sum(lane.concurrency for lane in lanes)
    preexisting = rng.choice((0, 1_000_000, 5_000_000))
    probe_mode = rng.choice(PROBE_MODES)
    floor = rng.choice((0, 1_000_000, 2_000_000))
    balance = floor + (300_000 if probe_mode == "poor" else rng.choice((3_000_000, 20_000_000)))
    return Scenario(
        seed=seed, lanes=lanes, arms=arms, total=at_once + rng.randint(0, 2), floor_micro=floor,
        cap_micro=preexisting + rng.choice((150_000, 600_000, 2_000_000, 50_000_000)),
        unknown_seat_micro=rng.choice((10_000, 20_000, 300_000)), preexisting_micro=preexisting,
        streak=rng.randint(2, 4), poll=20, max_arm_minutes=30, faults=_faults(rng),
        crash_rate=rng.choice((0.0, 0.0, 0.1)), probe_mode=probe_mode, balance_micro=balance,
        drop_at=rng.uniform(0, 3000), drop_micro=balance, detector=rng.random() < 0.7,
        detector_fail=rng.random() < 0.08,
        plan_edit_at=rng.uniform(0, 2000) if rng.random() < 0.2 else None,
        stop_file_at=rng.uniform(0, 2000) if rng.random() < 0.15 else None,
        kill_at=rng.uniform(1, 2000) if rng.random() < 0.35 else None,
        kill_delay=rng.choice((5.0, 120.0, 900.0)),
        duplicate=rng.random() < 0.2, per_lane_runners=rng.random() < 0.4,
        spawn_fail_arm=rng.choice([arm.name for arm in arms]) if rng.random() < 0.06 else "",
        clears_stop=rng.random() < 0.7, second_fill=rng.random() < 0.5,
    )


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    assert isinstance(value, dict)
    return "{ " + ", ".join(f"{key} = {_toml_value(item)}" for key, item in value.items()) + " }"


def toml_text(data: dict[str, Any]) -> str:
    lines: list[str] = []
    tables = [(key, value) for key, value in data.items() if isinstance(value, dict)]
    arrays = [(key, value) for key, value in data.items()
              if isinstance(value, list) and value and isinstance(value[0], dict)]
    nested = {key for key, _ in tables} | {key for key, _ in arrays}
    lines += [f"{key} = {_toml_value(value)}" for key, value in data.items() if key not in nested]
    for key, table in tables:
        lines += ["", f"[{key}]", *(f"{name} = {_toml_value(value)}" for name, value in table.items())]
    for key, items in arrays:
        for item in items:
            lines += ["", f"[[{key}]]", *(f"{name} = {_toml_value(value)}" for name, value in item.items())]
    return "\n".join(lines) + "\n"


def plan_data(case: Scenario, root: Path) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema": "campaign-plan/v1", "id": f"sim-{case.seed % 100_000}",
        "output_root": str(root / "out"), "state_root": str(root / "state"), "max_trials_at_once": case.total,
        "command": {"argv": ["fake-harbor", "{tasks}", "{out}", "{attempts}", "{concurrency}", "{harbor}"],
                    "cwd": str(root), "env": {"FAKE_ARM": "{arm}"}},
        "harbor": {"program": str(root / "bin" / "harbor"), "run_args": ["--env", "sim", "--ek", "snap=sim-v1"]},
        "money": {"provider": "sim", "probe": ["fake-balance"], "probe_timeout_seconds": 30,
                  "floor_usd": usd(case.floor_micro), "cap_usd": usd(case.cap_micro),
                  "cap_roots": [str(root / "other"), str(root / "out")],
                  "unknown_seat_usd": usd(case.unknown_seat_micro), "ledger": str(root / "ledger" / "spend.jsonl")},
        "watch": {"poll_seconds": case.poll, "max_arm_minutes": case.max_arm_minutes, "stop_grace_seconds": 10,
                  "stop_file": str(root / "STOP"), "no_reward_streak": case.streak},
        "arms": [
            {"name": arm.name, "tasks": str(root / "tasks" / arm.name), "attempts": arm.attempts,
             "reserve_usd": usd(arm.reserve_micro), "rerun_reserve_usd": usd(arm.reserve_micro // 2),
             "expected_usd": usd(arm.reserve_micro // 2), "pilot": arm.pilot}
            for arm in case.arms
        ],
        "lanes": [{"name": lane.name, "concurrency": lane.concurrency, "arms": list(lane.arms)}
                  for lane in case.lanes],
    }
    if case.detector:
        data["rerun"] = {"detector": ["fake-detector", "{arm_dir}"], "detector_timeout_seconds": 60}
    return data


def write_supply(case: Scenario, root: Path) -> None:
    for arm in case.arms:
        for name in arm.instances:
            folder = root / "tasks" / arm.name / name
            folder.mkdir(parents=True)
            (folder / "task.toml").write_text(f'name = "{name}"\n', encoding="utf-8")
    if case.preexisting_micro:
        trial = root / "other" / "old-arm" / "old-i01__0001"
        trial.mkdir(parents=True)
        seat = {"agent_result": {"cost_usd": usd(case.preexisting_micro)}, "agent_info": {"model_info": {}}}
        (trial / "result.json").write_text(json.dumps({"trial_name": trial.name, "seat_results": [seat]}))
    (root / "other").mkdir(exist_ok=True)
    (root / "ledger").mkdir()


@dataclass
class FakeTrial:
    name: str
    instance: str
    fault: str
    duration: float
    seat_costs: tuple[int, ...]
    start: float | None = None
    end: float | None = None

    @property
    def rewarded(self) -> bool:
        return self.end is not None and self.fault in REWARDED


@dataclass
class FakeJob:
    pid: int
    owner: int
    out: Path
    concurrency: int
    trials: list[FakeTrial]
    started: float
    crash_at: float | None
    exit: int | None = None
    ended_at: float | None = None
    events: int = 0

    @property
    def alive(self) -> bool:
        return self.exit is None


@dataclass
class Probe:
    time: float
    value: float | None


@dataclass
class Truth:
    charged_micro: int = 0
    spent_micro: int = 0
    finished: list[tuple[str, str, bool]] = field(default_factory=list)
    trials: dict[str, FakeTrial] = field(default_factory=dict)


SpawnHook = Callable[[list[str], int], None]


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _seats(trial: FakeTrial) -> list[dict[str, Any]]:
    seats = []
    for index, cost in enumerate(trial.seat_costs):
        seat: dict[str, Any] = {
            "seat": f"p{index + 1}", "agent_info": {"model_info": {"litellm_provider": "sim"}},
            "agent_result": {"cost_usd": usd(cost), "n_input_tokens": 100}, "agent_execution": {"ok": True},
            "exception_info": None,
        }
        if trial.fault == "pull-limit":
            seat |= {"agent_result": None, "agent_execution": None}
        if trial.fault == "unknown-cost" and index == 0:
            seat["agent_result"] = {"cost_usd": None, "n_input_tokens": 100}
        if trial.fault == "stalled" and index == 0:
            seat["exception_info"] = {"exception_type": "AgentTimeoutError"}
        seats.append(seat)
    return seats


class World:
    def __init__(self, case: Scenario, root: Path, rng: Random) -> None:
        self.case = case
        self.root = root
        self.rng = rng
        self.t = 0.0
        self.jobs: dict[int, FakeJob] = {}
        self.live_owners: set[int] = set()
        self.locks: dict[Path, int] = {}
        self.probes: dict[int, Probe] = {}
        self.truth = Truth(charged_micro=case.preexisting_micro)
        self.spawn_hook: SpawnHook | None = None
        self.next_pid = 5000
        self.calls: list[str] = []
        self.lock_errors: list[str] = []

    def now(self) -> float:
        return self.t

    def utc(self) -> str:
        return iso(self.t, precise=False)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def host(self) -> str:
        return "sim-host"

    def balance_micro(self) -> int:
        dropped = self.case.drop_micro if self.case.probe_mode == "falling" and self.t >= self.case.drop_at else 0
        return self.case.balance_micro - self.truth.spent_micro - dropped

    def _probe(self, owner: int) -> tuple[int | None, str]:
        mode = self.case.probe_mode
        broken = mode == "dead" or (mode == "flaky" and self.rng.random() < 0.2)
        if broken:
            self.probes[owner] = Probe(self.t, None)
            return self.rng.choice(((1, ""), (0, "null\n"), (None, ""), (0, "$18.70\n"), (0, "nan\n")))
        text = f"{usd(self.balance_micro()):.2f}"
        self.probes[owner] = Probe(self.t, float(text))
        return 0, text + "\n"

    def _detect(self, arm_dir: Path) -> tuple[int | None, str]:
        if self.case.detector_fail:
            return 2, "Traceback: simulated detector failure\n"
        lines = []
        for path, trial in sorted(self.truth.trials.items()):
            if Path(path).parent == arm_dir and trial.fault == "stalled" and trial.end is not None:
                lines.append(json.dumps({"trial": trial.name, "reward": 1.0, "stalled": [{"seat": "p1"}]}))
        lines.append(json.dumps({"arm": arm_dir.name, "with_stalled_seat": len(lines), "moved": False}))
        return 0, "\n".join(lines) + "\n"

    def capture(self, argv: list[str], timeout: float, owner: int) -> tuple[int | None, str]:
        assert timeout > 0
        self.calls.append(argv[0])
        if argv == ["fake-balance"]:
            return self._probe(owner)
        assert argv[0] == "fake-detector" and len(argv) == 2, argv
        return self._detect(Path(argv[1]))

    def _draw(self, name: str) -> tuple[str, float]:
        roll, total = self.rng.random(), 0.0
        fault = "ok"
        for kind, weight in self.case.faults:
            total += weight
            if roll < total:
                fault = kind
                break
        assert fault in FAULT_KINDS, name
        if fault == "pull-limit":
            return fault, self.rng.uniform(5, 40)
        if fault == "slow":
            return fault, self.case.max_arm_minutes * 60 * 2.0
        return fault, self.rng.uniform(30, 400)

    def _trials(self, pid: int, tasks: Path, attempts: int) -> list[FakeTrial]:
        names = sorted(path.name for path in tasks.iterdir() if (path / "task.toml").is_file())
        trials = []
        for instance in names:
            for attempt in range(attempts):
                name = f"{instance}__{pid:x}{attempt:02d}"
                fault, duration = self._draw(name)
                costs = tuple(self.rng.randint(1, 300) * 100 for _ in range(self.rng.randint(1, 3)))
                if fault == "pull-limit":
                    costs = tuple(0 for _ in costs)
                trials.append(FakeTrial(name, instance, fault, duration, costs))
        return trials

    def spawn(self, argv: list[str], cwd: Path, env: dict[str, str], log: Path, owner: int) -> int:
        assert owner in self.live_owners and cwd == self.root
        self.calls.append(argv[0])
        if self.spawn_hook is not None:
            self.spawn_hook(argv, owner)
        out = Path(argv[2])
        assert argv[0] == "fake-harbor" and env == {"FAKE_ARM": out.name}, (argv, env)
        if out.name == self.case.spawn_fail_arm:
            raise OSError("simulated failure to start the trial command")
        log.open("xb").close()
        pid = self.next_pid
        self.next_pid += 1
        crash_at = self.t + self.rng.uniform(10, 600) if self.rng.random() < self.case.crash_rate else None
        job = FakeJob(pid, owner, out, int(argv[4]), self._trials(pid, Path(argv[1]), int(argv[3])), self.t, crash_at)
        out.mkdir(parents=True)
        self.jobs[pid] = job
        self._fill_slots(job, self.t)
        return pid

    def poll(self, pid: int) -> int | None:
        return self.jobs[pid].exit

    def terminate(self, pid: int, grace: float) -> int:
        assert grace > 0
        job = self.jobs[pid]
        if job.exit is None:
            job.exit, job.ended_at = SIGTERM_EXIT, self.t
        return job.exit

    def alive(self, pid: int) -> bool:
        job = self.jobs.get(pid)
        return pid in self.live_owners or (job is not None and job.alive)

    def try_lock(self, path: Path, owner: int) -> bool:
        assert owner in self.live_owners
        holder = self.locks.get(path)
        if holder is not None and holder in self.live_owners:
            return False
        self.locks[path] = owner
        return True

    def unlock(self, path: Path, owner: int) -> None:
        if self.locks.get(path) != owner:
            self.lock_errors.append(f"owner {owner} released {path.name} held by {self.locks.get(path)}")
            return
        del self.locks[path]

    def kill_owner(self, owner: int) -> None:
        self.live_owners.discard(owner)
        for path in [path for path, holder in self.locks.items() if holder == owner]:
            del self.locks[path]

    def _fill_slots(self, job: FakeJob, at: float) -> None:
        running = sum(1 for trial in job.trials if trial.start is not None and trial.end is None)
        for trial in job.trials:
            if running >= job.concurrency:
                break
            if trial.start is None:
                trial.start = at
                running += 1
                _write(job.out / trial.name / "config.json", {"trial_name": trial.name})
                self.truth.trials[str(job.out / trial.name)] = trial

    def _finish(self, job: FakeJob, trial: FakeTrial, at: float) -> None:
        trial.end = at
        folder = job.out / trial.name
        self.truth.spent_micro += sum(trial.seat_costs)
        if trial.fault == "unreadable":
            (folder / "result.json").write_text('{"trial_name": "', encoding="utf-8")
            self.truth.charged_micro += self.case.unknown_seat_micro
            return
        recorded = sum(trial.seat_costs[1:] if trial.fault == "unknown-cost" else trial.seat_costs)
        unknown = self.case.unknown_seat_micro if trial.fault == "unknown-cost" else 0
        self.truth.charged_micro += recorded + unknown
        exception = None
        if trial.fault == "pull-limit":
            exception = {"exception_type": "RuntimeError", "exception_message": "docker pull: 429 Too Many Requests"}
        if trial.fault == "no-reward":
            exception = {"exception_type": "VerifierTimeoutError", "exception_message": "verifier timed out"}
        _write(folder / "result.json", {"trial_name": trial.name, "started_at": iso(trial.start or 0.0),
                                        "finished_at": iso(at), "seat_results": _seats(trial),
                                        "exception_info": exception})
        if trial.fault in REWARDED:
            _write(folder / "verifier" / "reward.json", {"reward": 1.0 if trial.fault == "ok" else 0.5})
        self.truth.finished.append((iso(at), str(folder), trial.fault in REWARDED))

    def _step_job(self, job: FakeJob, until: float) -> None:
        for _ in range(MAX_JOB_EVENTS):
            if job.exit is not None:
                return
            running = [trial for trial in job.trials if trial.start is not None and trial.end is None]
            ends = [(trial.start or 0.0) + trial.duration for trial in running]
            moment = min([*ends, job.crash_at if job.crash_at is not None else float("inf")])
            if moment > until:
                return
            if job.crash_at is not None and moment == job.crash_at:
                job.exit, job.ended_at = 1, moment
                return
            self._finish(job, running[ends.index(moment)], moment)
            self._fill_slots(job, moment)
            if all(trial.end is not None for trial in job.trials):
                job.exit, job.ended_at = 0, moment
                return
        raise AssertionError("a fake job made too many events")

    def advance(self, seconds: float) -> None:
        assert 0 < seconds <= 3600
        until = self.t + seconds
        for job in self.jobs.values():
            self._step_job(job, until)
        self.t = until

    def truth_streak(self) -> bool:
        done = sorted(self.truth.finished)
        last = [ok for _, _, ok in done[-self.case.streak:]]
        return len(last) == self.case.streak and not any(last)
