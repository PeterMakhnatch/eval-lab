from __future__ import annotations

import json
import math
import os
import shutil
import signal
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from random import Random
from typing import Any

from campaign.system import Row, WalkError

BASE = datetime(2026, 11, 24, tzinfo=UTC)
MICRO = 1_000_000
REWARDED = frozenset({"ok", "stalled", "unknown-cost"})
FAULT_KINDS = ("ok", "pull-limit", "stalled", "no-reward", "unknown-cost", "unreadable", "slow")
PROBE_MODES = ("steady", "steady", "steady", "dead", "flaky", "poor", "falling", "tight", "tight")
KEY_MODES = ("", "", "", "none", "high", "low", "low", "falling", "falling", "flaky")
KEY_LEFT_AFTER_DROP_MICRO = 140_000
MAX_JOB_EVENTS = 100_000
MUSTER_GRACE = 120.0
STOP_GRACE = 180
OTHER_PID = 4242
OTHER_START = "Mon Nov 23 09:00:00 2026"
SHELL_PID = 700
RANK = {"top-death": 0, "harbor": 1, "trial": 2}
FOREIGN_WINDOW_SECONDS = 6 * 3600
FOREIGN_YOUNG_AGE = 3601
FOREIGN_YOUNG_MICRO = 300_000
FOREIGN_ODD_MICRO = 450_000
FOREIGN_SLOW_MICRO = 2_000_000
TWIN_SUFFIX = "-twin"
PROBE_SECRET = "sk-sim-v1-0e6a5ec7e7"
SECRET_TEXT = '{"data":{"label":"' + PROBE_SECRET + '","limit":100,"limit_remaining":null}}\n'
PROBE_DELAYS = (0.0, 0.0, 0.0, 0.0, 0.0, 5.0, 29.0, 29.0, 31.0)


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
    cleanups: tuple[float, ...]
    ignore_term_rate: float
    hangs: tuple[float, ...]
    top_death_rate: float
    unstoppable_rate: float
    concurrent_probe: bool
    closed_output_after: int
    interrupt_at: float | None
    ledger_breaks: bool
    pid_reuse: str
    cap_root_missing_at: float | None
    operator_top_only: bool
    strays: bool
    ancestor_mentions: bool
    churn: bool
    ps_fail_rate: float
    operator_second_term: bool
    twin: bool
    foreign_lines: str
    uv_top: bool
    lock_hold: tuple[float, float, float] | None
    stop_in_lock_wait: bool
    key_mode: str
    key_micro: int
    key_drop_at: float
    probe_seconds: float
    foreign_settle: bool

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


def _lock_hold(rng: Random) -> tuple[float, float, float] | None:
    roll = rng.random()
    if roll < 0.3:
        return -0.25, rng.choice((2.0, 8.0, 12.0)), 20.0
    if roll < 0.4:
        return rng.uniform(0, 1500), 45.0, 0.0
    return None


def _process_faults(rng: Random) -> dict[str, Any]:
    harsh = rng.random() < 0.5
    twin = rng.random() < 0.3
    return {
        "cleanups": (0.0, 8.0, 60.0, 400.0) if harsh else (0.0, 2.0),
        "ignore_term_rate": 0.25 if harsh else 0.0,
        "hangs": (0.0, 0.0, 90.0, math.inf) if harsh else (0.0,),
        "top_death_rate": 0.2 if harsh else 0.0,
        "unstoppable_rate": 0.08 if harsh else 0.0,
        "concurrent_probe": twin or rng.random() < 0.5,
        "closed_output_after": rng.choice((0, 0, 0, 2, 5)),
        "interrupt_at": rng.uniform(1, 2000) if rng.random() < 0.2 else None,
        "ledger_breaks": rng.random() < 0.3,
        "pid_reuse": rng.choice(("", "", "", "start", "nostart")),
        "cap_root_missing_at": rng.uniform(0, 1500) if rng.random() < 0.08 else None,
        "operator_top_only": rng.random() < 0.6,
        "strays": rng.random() < 0.35,
        "ancestor_mentions": rng.random() < 0.3,
        "churn": rng.random() < 0.3,
        "ps_fail_rate": rng.choice((0.0, 0.0, 0.0, 0.05)),
        "operator_second_term": rng.random() < 0.15,
        "twin": twin,
        "foreign_lines": rng.choice(("", "", "basic", "odd")),
        "uv_top": rng.random() < 0.5,
        "lock_hold": _lock_hold(rng),
        "stop_in_lock_wait": rng.random() < 0.3,
    }


def _key_limit(rng: Random, balance: int, floor: int) -> dict[str, Any]:
    mode = rng.choice(KEY_MODES)
    low = floor + rng.choice((KEY_LEFT_AFTER_DROP_MICRO, 1_200_000, 3_000_000))
    key = {"high": balance + 20_000_000, "flaky": balance + 20_000_000, "falling": balance + 5_000_000,
           "low": low}.get(mode, 0)
    return {"key_mode": mode, "key_micro": key, "key_drop_at": rng.uniform(0, 3000),
            "probe_seconds": rng.choice(PROBE_DELAYS), "foreign_settle": rng.random() < 0.5}


def scenario(seed: int) -> Scenario:
    assert 0 <= seed < 2**64
    rng = Random(seed)
    lanes, arms = _layout(rng)
    at_once = sum(lane.concurrency for lane in lanes)
    preexisting = rng.choice((0, 1_000_000, 5_000_000))
    probe_mode = rng.choice(PROBE_MODES)
    floor = rng.choice((0, 1_000_000, 2_000_000))
    gap = {"poor": 300_000, "tight": 1_200_000}.get(probe_mode)
    balance = floor + (gap if gap is not None else rng.choice((3_000_000, 20_000_000)))
    return Scenario(
        seed=seed, lanes=lanes, arms=arms, total=at_once + rng.randint(0, 2), floor_micro=floor,
        cap_micro=max(max(arm.reserve_micro for arm in arms),
                      preexisting + rng.choice((150_000, 600_000, 2_000_000, 50_000_000))),
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
        **_process_faults(rng),
        **_key_limit(rng, balance, floor),
    )


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    assert isinstance(value, str)
    return json.dumps(value)


def _toml_value(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{key} = {_toml_scalar(item)}" for key, item in value.items()) + " }"
    return _toml_scalar(value)


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


def plan_data(case: Scenario, root: Path, twin: bool = False) -> dict[str, Any]:
    suffix = TWIN_SUFFIX if twin else ""
    roots = [str(root / "other"), str(root / "out")] + ([str(root / f"out{TWIN_SUFFIX}")] if case.twin else [])
    data: dict[str, Any] = {
        "schema": "campaign-plan/v1", "id": f"sim-{case.seed % 100_000}{suffix}",
        "output_root": str(root / f"out{suffix}"), "state_root": str(root / f"state{suffix}"),
        "max_trials_at_once": case.total,
        "command": {"argv": ["muster", "{tasks}", "--out", "{out}", "{attempts}", "{concurrency}", "{harbor}"],
                    "cwd": str(root), "env": {"FAKE_ARM": "{arm}"}},
        "harbor": {"program": str(root / "bin" / "harbor"), "run_args": ["--env", "sim", "--ek", "snap=sim-v1"]},
        "money": {"provider": "sim", "probe": ["fake-balance"], "probe_timeout_seconds": 30,
                  **({"key_limit_probe": ["fake-key-limit"]} if case.key_mode else {}),
                  "floor_usd": usd(case.floor_micro), "cap_usd": usd(case.cap_micro), "cap_roots": roots,
                  "unknown_seat_usd": usd(case.unknown_seat_micro), "ledger": str(root / "ledger" / "spend.jsonl")},
        "watch": {"poll_seconds": case.poll, "max_arm_minutes": case.max_arm_minutes,
                  "stop_grace_seconds": STOP_GRACE, "stop_file": str(root / "STOP"), "no_reward_streak": case.streak},
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


def _odd_foreign_lines(young: str) -> list[dict[str, Any]]:
    return [
        {"ts_utc": young, "event": "reserve", "arm": "run-arm-1", "provider": "sim", "reserved_usd": 0.2},
        {"ts_utc": young, "event": "reserve", "arm": "run-arm-2", "provider": "sim", "reserved_usd": 3.0},
        {"ts_utc": young, "event": "settle", "arm": "run-arm-2", "status": "ended"},
        {"ts_utc": young, "event": "reserve", "experiment": "night", "arm": "np-1", "reserved_usd": 0.1},
        {"ts_utc": young, "event": "reserve", "experiment": "night", "arm": "case-1", "provider": "SIM",
         "reserved_usd": 0.15},
        {"ts_utc": young, "event": "reserve", "experiment": "night", "arm": "str-1", "provider": "sim",
         "reserved_usd": "1.5"},
    ]


def _foreign_ledger(path: Path, odd: bool, slow: bool) -> None:
    young, old = iso(-FOREIGN_YOUNG_AGE, precise=False), iso(-8 * 3600, precise=False)
    lines = [
        {"ts_utc": young, "event": "reserve", "experiment": "night", "arm": "team-8", "provider": "sim",
         "reserved_usd": usd(FOREIGN_YOUNG_MICRO)},
        {"ts_utc": old, "event": "reserve", "experiment": "e7", "arm": "old-1", "provider": "sim",
         "reserved_usd": 5.0},
        {"ts_utc": young, "event": "reserve", "experiment": "night", "arm": "done-1", "provider": "sim",
         "reserved_usd": 4.0},
        {"ts_utc": young, "event": "settle", "experiment": "night", "arm": "done-1", "status": "ended"},
        {"ts_utc": young, "event": "reserve", "experiment": "night", "arm": "elsewhere", "provider": "openrouter",
         "reserved_usd": 3.0},
        {"ts_utc": young, "event": "reserve", "experiment": "night", "arm": "void-1", "provider": "sim",
         "reserved_usd": 2.0},
        {"ts_utc": young, "event": "void", "experiment": "night", "arms": ["void-1"], "reason": "test"},
    ] + (_odd_foreign_lines(young) if odd else [])
    if slow:
        lines.append({"ts_utc": young, "event": "reserve", "experiment": "night", "arm": "slow-1", "provider": "sim",
                      "reserved_usd": usd(FOREIGN_SLOW_MICRO)})
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


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
    if case.churn:
        (root / "other" / "churn-0" / "tmp").mkdir(parents=True)
    if case.twin:
        (root / "out").mkdir()
        (root / f"out{TWIN_SUFFIX}").mkdir()
    if case.foreign_lines:
        _foreign_ledger(root / "ledger" / "spend.jsonl", case.foreign_lines == "odd", case.foreign_settle)


class BrokenStream:
    def __init__(self, after: int) -> None:
        assert after >= 1
        self.after = after
        self.writes = 0

    def write(self, text: str) -> int:
        self.writes += 1
        if self.writes > self.after:
            raise BrokenPipeError(32, "Broken pipe")
        return len(text)

    def flush(self) -> None:
        return None


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
    harbor_pid: int
    owner: int
    out: Path
    concurrency: int
    trials: list[FakeTrial]
    started: float
    crash_at: float | None
    top_death_at: float | None
    cleanup: float
    ignore_term: bool
    hang: float
    unstoppable: bool
    top_exit: int | None = None
    harbor_exit: int | None = None
    top_end_at: float | None = None
    harbor_end_at: float | None = None
    harbor_term_at: float | None = None
    muster_term_at: float | None = None
    done_at: float | None = None
    abandoned: bool = False
    runner_terms: int = 0
    first_term_at: float | None = None
    top_alive_at_term: bool = False
    operator_termed: bool = False
    operator_killed: bool = False

    @property
    def top_alive(self) -> bool:
        return self.top_exit is None

    @property
    def harbor_alive(self) -> bool:
        return self.harbor_exit is None

    @property
    def alive(self) -> bool:
        return self.top_alive or self.harbor_alive

    @property
    def gone_at(self) -> float | None:
        if self.top_end_at is None or self.harbor_end_at is None:
            return None
        return max(self.top_end_at, self.harbor_end_at)

    def harbor_end(self) -> tuple[float, int] | None:
        ends: list[tuple[float, int]] = []
        if self.crash_at is not None:
            ends.append((self.crash_at, 1))
        if self.harbor_term_at is not None and not self.ignore_term and not self.unstoppable:
            ends.append((self.harbor_term_at + self.cleanup, 143))
        if self.muster_term_at is not None and self.top_alive and not self.unstoppable:
            ends.append((self.muster_term_at + MUSTER_GRACE, -9))
        if self.done_at is not None and math.isfinite(self.hang):
            ends.append((self.done_at + self.hang, 0))
        return min(ends) if ends else None


@dataclass
class Probe:
    time: float
    value: float | None


@dataclass
class Truth:
    charged_micro: int = 0
    spent_micro: int = 0
    charged_by_arm: Counter[Path] = field(default_factory=Counter)
    finished: list[tuple[str, str, bool]] = field(default_factory=list)
    trials: dict[str, FakeTrial] = field(default_factory=dict)


SpawnHook = Callable[[list[str], int], None]
ProbeHook = Callable[[int], None]
SleepHook = Callable[[], None]


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
        self.key_failed: dict[int, float] = {}
        self.truth = Truth(charged_micro=case.preexisting_micro)
        self.spawn_hook: SpawnHook | None = None
        self.probe_hook: ProbeHook | None = None
        self.probe_start_hook: ProbeHook | None = None
        self.in_probe_hook = False
        self.sleep_hook: SleepHook | None = None
        self.observe_hook: SleepHook | None = None
        self.in_sleep_hook = False
        self.next_pid = 5000
        self.calls: list[str] = []
        self.lock_errors: list[str] = []
        self.failed_spawns: set[str] = set()
        self.others: dict[int, str] = {}
        self.strays: dict[int, tuple[int, str]] = {}
        self.bad_signals: list[str] = []
        self.ps_failed_at: float | None = None
        self.churned = 0
        self.lock_attempts: list[tuple[int, float, bool, bool]] = []

    def now(self) -> float:
        return self.t

    def utc(self) -> str:
        return iso(self.t, precise=False)

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        if self.in_probe_hook:
            return
        self.advance(seconds)
        if self.sleep_hook is not None and not self.in_sleep_hook:
            self.in_sleep_hook = True
            try:
                self.sleep_hook()
            finally:
                self.in_sleep_hook = False

    def host(self) -> str:
        return "sim-host"

    def balance_micro(self) -> int:
        dropped = self.case.drop_micro if self.case.probe_mode == "falling" and self.t >= self.case.drop_at else 0
        return self.case.balance_micro - self.truth.spent_micro - dropped

    def _slow(self, timeout: float) -> bool:
        delay = self.case.probe_seconds
        if delay > 0:
            self.advance(min(delay, timeout))
            if self.observe_hook is not None:
                self.observe_hook()
        return delay >= timeout

    def _probe(self, owner: int, timeout: float) -> tuple[int | None, str]:
        if self.probe_start_hook is not None:
            self.probe_start_hook(owner)
        if self.probe_hook is not None and not self.in_probe_hook:
            self.in_probe_hook = True
            try:
                self.probe_hook(owner)
            finally:
                self.in_probe_hook = False
        mode = self.case.probe_mode
        broken = mode == "dead" or (mode == "flaky" and self.rng.random() < 0.2)
        if self._slow(timeout):
            self.probes[owner] = Probe(self.t, None)
            return None, ""
        if broken:
            self.probes[owner] = Probe(self.t, None)
            return self.rng.choice(((1, ""), (0, "null\n"), (None, ""), (0, "$18.70\n"), (0, "nan\n"),
                                    (0, "99999999999\n"), (0, SECRET_TEXT)))
        text = f"{usd(self.balance_micro()):.2f}"
        self.probes[owner] = Probe(self.t, float(text))
        return 0, text + "\n"

    def key_left_micro(self) -> int | None:
        mode = self.case.key_mode
        if mode in ("", "none"):
            return None
        dropped = 0
        if mode == "falling" and self.t >= self.case.key_drop_at:
            dropped = self.case.key_micro - KEY_LEFT_AFTER_DROP_MICRO
        return self.case.key_micro - self.truth.spent_micro - dropped

    def key_truth(self) -> float | None:
        left = self.key_left_micro()
        return None if left is None else float(f"{usd(max(0, left)):.4f}")

    def _key_probe(self, owner: int, timeout: float) -> tuple[int | None, str]:
        assert self.case.key_mode
        if self._slow(timeout):
            self.key_failed[owner] = self.t
            return None, ""
        if self.case.key_mode == "flaky" and self.rng.random() < 0.2:
            self.key_failed[owner] = self.t
            return self.rng.choice(((1, ""), (0, "unlimited\n"), (None, ""), (0, "inf\n"), (0, SECRET_TEXT)))
        self.key_failed.pop(owner, None)
        credits = self.probes.get(owner)
        if credits is not None and credits.value is not None:
            self.probes[owner] = Probe(self.t, credits.value)
        left = self.key_truth()
        return 0, ("none" if left is None else f"{left:.4f}") + "\n"

    def _detect(self, arm_dir: Path) -> tuple[int | None, str]:
        if self.case.detector_fail:
            return 2, "Traceback: simulated detector failure\n"
        lines = []
        for path, trial in sorted(self.truth.trials.items()):
            if Path(path).parent == arm_dir and trial.fault == "stalled" and trial.end is not None:
                lines.append(json.dumps({"trial": trial.name, "reward": 1.0, "stalled": [{"seat": "p1"}]}))
        lines.append(json.dumps({"arm": arm_dir.name, "with_stalled_seat": len(lines), "moved": False}))
        return 0, "\n".join(lines) + "\n"

    def capture(self, argv: list[str], timeout: float, owner: int, *,
                scrub_keys: bool = False) -> tuple[int | None, str]:
        assert timeout > 0
        assert not scrub_keys
        self.calls.append(argv[0])
        if argv == ["fake-balance"]:
            return self._probe(owner, timeout)
        if argv == ["fake-key-limit"]:
            return self._key_probe(owner, timeout)
        assert argv[0] == "fake-detector" and len(argv) == 2, argv
        return self._detect(Path(argv[1]))

    def _draw(self) -> tuple[str, float]:
        roll, total = self.rng.random(), 0.0
        fault = "ok"
        for kind, weight in self.case.faults:
            total += weight
            if roll < total:
                fault = kind
                break
        assert fault in FAULT_KINDS
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
                fault, duration = self._draw()
                costs = tuple(self.rng.randint(1, 300) * 100 for _ in range(self.rng.randint(1, 3)))
                if fault == "pull-limit":
                    costs = tuple(0 for _ in costs)
                trials.append(FakeTrial(f"{instance}__{pid:x}{attempt:02d}", instance, fault, duration, costs))
        return trials

    def _job(self, pid: int, owner: int, argv: list[str]) -> FakeJob:
        case, rng = self.case, self.rng
        crash_at = self.t + rng.uniform(10, 600) if rng.random() < case.crash_rate else None
        death = self.t + rng.uniform(10, 400) if rng.random() < case.top_death_rate else None
        return FakeJob(
            pid=pid, harbor_pid=pid + 1, owner=owner, out=Path(argv[3]), concurrency=int(argv[5]),
            trials=self._trials(pid, Path(argv[1]), int(argv[4])), started=self.t, crash_at=crash_at,
            top_death_at=death, cleanup=rng.choice(case.cleanups),
            ignore_term=rng.random() < case.ignore_term_rate, hang=rng.choice(case.hangs),
            unstoppable=rng.random() < case.unstoppable_rate,
        )

    def spawn(self, argv: list[str], cwd: Path, env: dict[str, str], log: Path, owner: int, *,
              scrub_keys: bool = False) -> int:
        assert owner in self.live_owners and cwd == self.root
        assert not scrub_keys
        self.calls.append(argv[0])
        out = Path(argv[3])
        assert argv[0] == "muster" and argv[2] == "--out" and env == {"FAKE_ARM": out.name}, (argv, env)
        if out.name == self.case.spawn_fail_arm and out.name not in self.failed_spawns:
            self.failed_spawns.add(out.name)
            raise OSError("simulated failure to start the trial command")
        if self.spawn_hook is not None:
            self.spawn_hook(argv, owner)
        log.open("xb").close()
        pid = self.next_pid
        self.next_pid += 6
        job = self._job(pid, owner, argv)
        out.mkdir(parents=True)
        self.jobs[pid] = job
        if self.case.strays:
            self.strays[pid + 2] = (pid + 2, f"python3 /sim/tidegauge live {out}")
            self.strays[pid + 3] = (pid + 2, f"vim {out}.job.json")
        self._fill_slots(job, self.t)
        return pid

    def _job_of(self, pid: int) -> FakeJob | None:
        return self.jobs.get(pid) or self.jobs.get(pid - 1)

    def poll(self, pid: int) -> int | None:
        return self.jobs[pid].top_exit

    def _end_top(self, job: FakeJob, code: int, at: float) -> None:
        job.top_exit, job.top_end_at = code, at

    def _end_harbor(self, job: FakeJob, code: int, at: float) -> None:
        job.harbor_exit, job.harbor_end_at = code, at
        if job.top_alive:
            self._end_top(job, 130 if job.muster_term_at is not None else code, at)

    def _signal_top(self, job: FakeJob, number: int) -> None:
        if not job.top_alive:
            return
        if number == signal.SIGKILL or job.muster_term_at is not None:
            self._end_top(job, -9 if number == signal.SIGKILL else 130, self.t)
            return
        job.muster_term_at = self.t
        if job.harbor_alive and job.harbor_term_at is None:
            job.harbor_term_at = self.t

    def _protected_group(self, pgid: int) -> bool:
        return pgid in (SHELL_PID, OTHER_PID) or pgid in self.live_owners or pgid in self.strays

    def _deliver_term(self, job: FakeJob, deliveries: int) -> None:
        for _ in range(deliveries):
            job.runner_terms += 1
            if job.first_term_at is None:
                job.first_term_at, job.top_alive_at_term = self.t, job.top_alive
            self._signal_top(job, signal.SIGTERM)

    def signal_process(self, pid: int, number: int) -> None:
        assert pid > 1 and number in (signal.SIGTERM, signal.SIGKILL)
        if self._protected_group(pid):
            self.bad_signals.append(f"signal {number} to process {pid} at {self.t:.0f}")
            return
        job = self.jobs.get(pid)
        if job is None:
            return
        if number == signal.SIGTERM:
            self._deliver_term(job, 1)
        else:
            self._signal_top(job, number)

    def signal_group(self, pgid: int, number: int) -> None:
        assert pgid > 1 and number in (signal.SIGTERM, signal.SIGKILL)
        if self._protected_group(pgid):
            self.bad_signals.append(f"signal {number} to group {pgid} at {self.t:.0f}")
            return
        job = self._job_of(pgid)
        if job is None:
            return
        if pgid == job.pid:
            if number == signal.SIGTERM:
                self._deliver_term(job, 2 if self.case.uv_top else 1)
            else:
                self._signal_top(job, number)
            return
        if job.unstoppable:
            raise PermissionError(1, "Operation not permitted")
        if not job.harbor_alive:
            return
        if number == signal.SIGKILL:
            self._end_harbor(job, -9, self.t)
        elif job.harbor_term_at is None:
            job.harbor_term_at = self.t

    def operator_kill(self, job: FakeJob, top_only: bool) -> None:
        job.operator_killed = True
        if job.top_alive:
            self._end_top(job, -9, self.t)
        if job.harbor_alive and not top_only:
            job.harbor_exit, job.harbor_end_at = -9, self.t

    def operator_term(self, job: FakeJob) -> None:
        job.operator_termed = True
        self._signal_top(job, signal.SIGTERM)

    def alive(self, pid: int) -> bool:
        job = self._job_of(pid)
        if job is not None:
            return job.top_alive if pid == job.pid else job.harbor_alive
        return pid in self.live_owners or pid in self.others or pid in self.strays

    def start_time(self, pid: int) -> str | None:
        job = self._job_of(pid)
        if job is not None and self.alive(pid):
            return f"sim {job.started:.0f} {pid}"
        return self.others.get(pid)

    def _shell_row(self) -> Row:
        command = "sh -c campaign-loop"
        first = self.case.lanes[0].arms[0]
        if self.case.ancestor_mentions:
            command = f"sh -c campaign run plan.toml --out {self.root / 'out' / first}"
        return SHELL_PID, 1, SHELL_PID, command

    def processes(self) -> list[Row]:
        if self.case.ps_fail_rate and self.rng.random() < self.case.ps_fail_rate:
            self.ps_failed_at = self.t
            raise OSError("ps failed: simulated timeout")
        rows: list[Row] = [self._shell_row()]
        rows += [(owner, SHELL_PID, SHELL_PID, "python3 -m campaign run plan.toml") for owner in sorted(self.live_owners)]
        rows += [(pid, 1, pgid, command) for pid, (pgid, command) in sorted(self.strays.items())]
        rows += [(pid, 1, pid, "sleep 1000") for pid in sorted(self.others)]
        for job in self.jobs.values():
            if job.top_alive:
                muster = f"python3 fake-muster {job.out.parent} --attempts 2 --concurrency 2 --out {job.out}"
                if self.case.uv_top:
                    rows.append((job.pid, job.owner, job.pid, f"uv run --no-sync {muster}"))
                    rows.append((job.pid + 4, job.pid, job.pid, muster))
                else:
                    rows.append((job.pid, job.owner, job.pid, muster))
            if job.harbor_alive:
                parent = job.pid if job.top_alive else 1
                rows.append((job.harbor_pid, parent, job.harbor_pid,
                             f"/sim/python3 /sim/harbor run -c {job.out}.job.json --quiet --yes --env sim"))
        return rows

    def walk(self, root: Path, onerror: WalkError) -> Iterator[tuple[str, list[str], list[str]]]:
        churn_at = os.path.realpath(self.root / "other")
        for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
            yield dirpath, dirnames, filenames
            if self.case.churn and dirpath == churn_at:
                for name in [name for name in dirnames if name.startswith("churn-")]:
                    shutil.rmtree(Path(dirpath) / name)
                self.churned += 1
                (Path(dirpath) / f"churn-{self.churned}" / "tmp").mkdir(parents=True)

    def hold_end(self, at: float) -> float | None:
        hold = self.case.lock_hold
        if hold is None or at < hold[0]:
            return None
        start, duration, period = hold
        since = (at - start) % period if period else at - start
        return at - since + duration if since < duration else None

    def _external_hold(self, path: Path) -> bool:
        return path.name.endswith(".jsonl.lock") and self.hold_end(self.t) is not None

    def try_lock(self, path: Path, owner: int) -> bool:
        assert owner in self.live_owners
        holder = self.locks.get(path)
        external = self._external_hold(path)
        busy = external or (holder is not None and holder in self.live_owners)
        if path.name.endswith(".jsonl.lock"):
            only_external = external and (holder is None or holder not in self.live_owners)
            self.lock_attempts.append((owner, self.t, not busy, only_external))
        if busy:
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
        if not job.harbor_alive or job.harbor_term_at is not None:
            return
        running = sum(1 for trial in job.trials if trial.start is not None and trial.end is None)
        for trial in job.trials:
            if running >= job.concurrency:
                break
            if trial.start is None:
                trial.start = at
                running += 1
                participants = [{"name": "terminus-2", "model_name": "sim/model"} for _ in trial.seat_costs]
                _write(job.out / trial.name / "config.json",
                       {"trial_name": trial.name, "task": {"path": trial.instance},
                        "participants": participants, "agent": {"name": "script"}})
                self._charge(job, len(participants) * self.case.unknown_seat_micro)
                self.truth.trials[str(job.out / trial.name)] = trial

    def _charge(self, job: FakeJob, delta: int) -> None:
        self.truth.charged_micro += delta
        self.truth.charged_by_arm[job.out] += delta
        assert self.truth.charged_by_arm[job.out] >= 0

    def _finish(self, job: FakeJob, trial: FakeTrial, at: float) -> None:
        trial.end = at
        folder = job.out / trial.name
        self.truth.spent_micro += sum(trial.seat_costs)
        pending = len(trial.seat_costs) * self.case.unknown_seat_micro
        if trial.fault == "unreadable":
            (folder / "result.json").write_text('{"trial_name": "', encoding="utf-8")
            self._charge(job, self.case.unknown_seat_micro - pending)
            return
        recorded = sum(trial.seat_costs[1:] if trial.fault == "unknown-cost" else trial.seat_costs)
        unknown = self.case.unknown_seat_micro if trial.fault == "unknown-cost" else 0
        self._charge(job, recorded + unknown - pending)
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

    def _next_event(self, job: FakeJob) -> tuple[float, str, FakeTrial | None, int]:
        events: list[tuple[float, str, FakeTrial | None, int]] = []
        if job.harbor_alive:
            for trial in job.trials:
                if trial.start is not None and trial.end is None:
                    events.append((trial.start + trial.duration, "trial", trial, 0))
            end = job.harbor_end()
            if end is not None:
                events.append((end[0], "harbor", None, end[1]))
        if job.top_alive and job.top_death_at is not None:
            events.append((job.top_death_at, "top-death", None, -9))
        if not events:
            return math.inf, "", None, 0
        return min(events, key=lambda event: (event[0], RANK[event[1]]))

    def _step_job(self, job: FakeJob, until: float) -> None:
        for _ in range(MAX_JOB_EVENTS):
            moment, kind, trial, code = self._next_event(job)
            if moment > until:
                return
            if kind == "top-death":
                self._end_top(job, code, moment)
            elif kind == "harbor":
                self._end_harbor(job, code, moment)
            else:
                assert trial is not None
                self._finish(job, trial, moment)
                self._fill_slots(job, moment)
                if job.done_at is None and all(item.end is not None for item in job.trials):
                    job.done_at = moment
        raise AssertionError("a fake job made too many events")

    def advance(self, seconds: float) -> None:
        assert 0 < seconds <= 3600
        until = self.t + seconds
        for job in self.jobs.values():
            self._step_job(job, until)
        self.t = until

    def truth_streak(self, output_root: Path, since: datetime) -> bool:
        done = sorted((datetime.fromisoformat(end), path, ok) for end, path, ok in self.truth.finished
                      if Path(path).parent.parent == output_root)
        last = [ok for end, _, ok in done if end >= since][-self.case.streak:]
        return len(last) == self.case.streak and not any(last)
