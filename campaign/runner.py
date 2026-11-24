from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from campaign import evidence, ledger
from campaign.plan import Lane, Plan, arm_command, base_arm, sha256_file
from campaign.system import System

NUMBER = re.compile(r"-?[0-9]+(\.[0-9]+)?")
MAX_LANE_ITEMS = 4096
TERMINAL = ("STOP", "DONE")

Emit = Callable[[dict[str, str]], None]


class Stop(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Step:
    out: str
    arm: str
    tasks: Path
    attempts: int
    reserve_usd: float
    pilot: bool


@dataclass
class Job:
    step: Step
    pid: int
    started: float


@dataclass
class LaneRun:
    lane: Lane
    items: list[str | Step]
    job: Job | None = None
    held: bool = False
    ended: bool = False
    stopped: bool = False
    started: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)


Expand = Callable[["Runner", LaneRun, str], list[Step]]


def first_run_step(plan: Plan, arm_name: str) -> Step:
    arm = plan.arm(arm_name)
    return Step(arm.name, arm.name, arm.tasks, arm.attempts, arm.reserve_usd, arm.pilot)


def expand_first_run(runner: Runner, run: LaneRun, arm_name: str) -> list[Step]:
    assert arm_name in run.lane.arms
    return [first_run_step(runner.plan, arm_name)]


def read_balance(system: System, plan: Plan, owner: int) -> float | None:
    code, text = system.capture(list(plan.probe), plan.probe_timeout_seconds, owner)
    value = text.strip()
    if code != 0 or not NUMBER.fullmatch(value):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    staged.write_text(text, encoding="utf-8")
    os.replace(staged, path)


class Runner:
    def __init__(
        self, plan: Plan, system: System, owner: int, emit: Emit, lanes: list[Lane], expand: Expand
    ) -> None:
        assert owner > 0 and lanes
        self.plan = plan
        self.system = system
        self.owner = owner
        self.emit = emit
        self.expand = expand
        self.sha = plan.sha256
        self.wrapper = plan.wrapper_text()
        self.lanes = [LaneRun(lane, list(lane.arms)) for lane in lanes]

    def say(self, kind: str, lane: str, text: str, reason: str = "", before: str = "") -> None:
        self.emit({"kind": kind, "lane": lane, "reason": reason, "before": before, "text": text})

    def open(self) -> None:
        wrapper_problem = self._write_wrapper()
        for run in self.lanes:
            try:
                if wrapper_problem:
                    raise Stop("wrapper-changed", wrapper_problem)
                self._open_lane(run)
            except Stop as stop:
                self._end(run, stop)

    def tick(self) -> bool:
        for run in self.lanes:
            if run.ended:
                continue
            try:
                if run.job is not None:
                    self._watch(run)
                if run.job is None:
                    self._advance(run)
            except Stop as stop:
                self._end(run, stop)
        return any(not run.ended for run in self.lanes)

    def interrupt(self, reason: str, detail: str) -> None:
        for run in self.lanes:
            if run.ended:
                continue
            stop = Stop(reason, detail)
            if run.job is not None:
                code = self.system.terminate(run.job.pid, self.plan.stop_grace_seconds)
                try:
                    self._finish(run, reason, code)
                except Stop as failed:
                    stop = Stop(reason, f"{detail}; {failed.detail}")
            self._end(run, stop)

    def exit_code(self) -> int:
        assert all(run.ended for run in self.lanes)
        return 1 if any(run.stopped for run in self.lanes) else 0

    def max_ticks(self) -> int:
        attempts = max(arm.attempts for arm in self.plan.arms)
        steps = sum(len(run.items) for run in self.lanes) * (attempts + 1)
        per_step = math.ceil(self.plan.max_arm_minutes * 60 / self.plan.poll_seconds) + 3
        return steps * per_step + 16

    def _write_wrapper(self) -> str:
        path = self.plan.wrapper_path()
        try:
            if not path.exists():
                write_atomic(path, self.wrapper)
                path.chmod(0o755)
            return "" if path.read_text(encoding="utf-8") == self.wrapper else f"{path} differs from the plan"
        except OSError as error:
            return f"cannot write the Harbor wrapper {path}: {error.strerror}"

    def _lane_files(self, lane: str) -> tuple[Path, Path]:
        folder = self.plan.lane_dir()
        return folder / f"{lane}.lock", folder / f"{lane}.json"

    def _lane_info(self, run: LaneRun, arm: str, child: int | None) -> None:
        _, info = self._lane_files(run.lane.name)
        record = {"owner": self.owner, "host": self.system.host(), "arm": arm, "child": child,
                  "since": self.system.utc(), "plan_sha256": self.sha}
        write_atomic(info, json.dumps(record, indent=1) + "\n")

    def _open_lane(self, run: LaneRun) -> None:
        lock, info_path = self._lane_files(run.lane.name)
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            held = self.system.try_lock(lock, self.owner)
        except OSError as error:
            raise Stop("lane-held", f"cannot lock {lock}: {error.strerror}") from error
        if not held:
            raise Stop("lane-held", f"another runner holds {lock}")
        run.held = True
        info = evidence.read_json(info_path) if info_path.exists() else {}
        if not isinstance(info, dict):
            raise Stop("orphan", f"{info_path} is unreadable; check for a running arm by hand")
        child = info.get("child")
        if isinstance(child, int) and child > 0:
            if info.get("host") != self.system.host():
                raise Stop("orphan", f"arm {info.get('arm')} was started on host {info.get('host')}")
            if self.system.alive(child):
                raise Stop("orphan", f"arm {info.get('arm')} of an earlier runner still runs as "
                                     f"process group {child}; run again after it ends")
        self._settle_interrupted(run)
        self._lane_info(run, "", None)

    def _settle_interrupted(self, run: LaneRun) -> None:
        try:
            state = ledger.state(self.plan.ledger, self.plan.id)
        except ledger.LedgerError as error:
            raise Stop("ledger", str(error)) from error
        for out, arm in sorted(state.items()):
            if not arm.open or base_arm(out) not in run.lane.arms:
                continue
            for _ in range(arm.reserves - arm.settles):
                self._settle(run.lane.name, out, "interrupted", None)
            self.say("SETTLE", run.lane.name, f"{out}: an earlier runner ended without a settle line; "
                                              "settled now as interrupted")

    def _settle(self, lane: str, out: str, status: str, code: int | None) -> dict[str, Any]:
        actual = evidence.scan_cost([self.plan.output_root / out]).record()
        record = {
            "ts_utc": self.system.utc(), "event": "settle", "experiment": self.plan.id,
            "campaign": self.plan.id, "plan_sha256": self.sha, "lane": lane, "arm": out,
            "exit": code, "status": status, "actual": actual,
        }
        try:
            ledger.append(self.plan.ledger, record)
        except OSError as error:
            raise Stop("ledger", f"cannot write the settle line of {out}: {error.strerror}") from error
        return actual

    def _gate_common(self) -> None:
        stop_file = self.plan.stop_file
        if stop_file.exists():
            raise Stop("stop-file", f"{stop_file} is present")
        try:
            sha = sha256_file(self.plan.path)
        except OSError as error:
            raise Stop("plan-changed", f"cannot read {self.plan.path}: {error.strerror}") from error
        if sha != self.sha:
            raise Stop("plan-changed", f"{self.plan.path} sha256 is {sha[:12]}, the runner started with "
                                       f"{self.sha[:12]}")
        try:
            same = self.plan.wrapper_path().read_text(encoding="utf-8") == self.wrapper
        except OSError:
            same = False
        if not same:
            raise Stop("wrapper-changed", f"{self.plan.wrapper_path()} differs from the plan")

    def _already_started(self, step: Step) -> str:
        out = self.plan.output_root / step.out
        if out.exists():
            return f"{out} exists"
        if out.with_name(f"{step.out}.out").exists():
            return f"{out}.out exists"
        try:
            state = ledger.state(self.plan.ledger, self.plan.id)
        except ledger.LedgerError as error:
            raise Stop("ledger", str(error)) from error
        if step.out in state:
            return "the ledger has a reserve line for it"
        return ""

    def _open_reserved(self) -> float:
        try:
            state = ledger.state(self.plan.ledger, self.plan.id)
        except ledger.LedgerError as error:
            raise Stop("ledger", str(error)) from error
        total = 0.0
        for arm in state.values():
            if arm.open and arm.reserves:
                total += arm.reserved_usd * (arm.reserves - arm.settles) / arm.reserves
        return total

    def _gate_money(self, step: Step) -> tuple[int, float]:
        instances = evidence.task_instances(step.tasks)
        if not instances:
            raise Stop("tasks-missing", f"{step.tasks} holds no task")
        cost = evidence.scan_cost(self.plan.cap_roots)
        charged = cost.charged(self.plan.unknown_seat_usd)
        if charged >= self.plan.cap_usd:
            raise Stop("cap", f"recorded cost ${charged:.4f} is at or above the cap ${self.plan.cap_usd:.2f}")
        hit, finished = evidence.streak(self.plan.output_root, self.plan.no_reward_streak)
        if hit:
            raise Stop("streak", f"the newest {self.plan.no_reward_streak} of {finished} finished trials "
                                 "have no reward")
        held = self._open_reserved()
        balance = read_balance(self.system, self.plan, self.owner)
        if balance is None:
            raise Stop("balance-unreadable", "the balance probe gave no number")
        need = self.plan.floor_usd + step.reserve_usd + held
        if balance < need:
            raise Stop("balance-low", f"balance ${balance:.2f} is below floor ${self.plan.floor_usd:.2f} + "
                                      f"reserve ${step.reserve_usd:.2f} + open reserves ${held:.2f}")
        return len(instances), balance

    def _advance(self, run: LaneRun) -> None:
        for _ in range(MAX_LANE_ITEMS):
            if not run.items:
                self._end(run, None)
                return
            self._gate_common()
            item = run.items[0]
            if isinstance(item, str):
                run.items[0:1] = self.expand(self, run, item)
                continue
            reason = self._already_started(item)
            if reason:
                run.items.pop(0)
                run.skipped += 1
                self.say("SKIP", run.lane.name, f"{item.out}: {reason}; an arm folder is never reused")
                gap = self._pilot_gap(item)
                if gap:
                    raise Stop("pilot", gap)
                continue
            tasks, balance = self._gate_money(item)
            self._start(run, item, tasks, balance)
            run.items.pop(0)
            return
        raise AssertionError("a lane holds more items than MAX_LANE_ITEMS")

    def _start(self, run: LaneRun, step: Step, tasks: int, balance: float) -> None:
        concurrency = run.lane.concurrency
        argv, env = arm_command(self.plan, step.out, step.tasks, step.attempts, concurrency)
        record = {
            "ts_utc": self.system.utc(), "event": "reserve", "experiment": self.plan.id,
            "campaign": self.plan.id, "plan_sha256": self.sha, "lane": run.lane.name, "arm": step.out,
            "provider": self.plan.provider, "tasks": tasks, "attempts": step.attempts,
            "concurrency": concurrency, "reserved_usd": step.reserve_usd, "balance_usd": balance,
        }
        try:
            ledger.append(self.plan.ledger, record)
        except OSError as error:
            raise Stop("ledger", f"cannot write the reserve line of {step.out}: {error.strerror}") from error
        log = self.plan.output_root / f"{step.out}.out"
        try:
            self.plan.output_root.mkdir(parents=True, exist_ok=True)
            pid = self.system.spawn(argv, self.plan.cwd, env, log, self.owner)
        except OSError as error:
            self._settle(run.lane.name, step.out, "spawn-failed", None)
            raise Stop("spawn", f"cannot start {step.out}: {error}") from error
        run.job = Job(step, pid, self.system.now())
        run.started += 1
        self._lane_info(run, step.out, pid)
        self.say("START", run.lane.name, f"{step.out}: {tasks} tasks x {step.attempts} attempts, "
                                         f"{concurrency} at once, balance ${balance:.2f}, "
                                         f"reserve ${step.reserve_usd:.2f}")

    def _watch_reason(self, job: Job) -> Stop | None:
        if self.plan.stop_file.exists():
            return Stop("stop-file", f"{self.plan.stop_file} is present")
        charged = evidence.scan_cost(self.plan.cap_roots).charged(self.plan.unknown_seat_usd)
        if charged >= self.plan.cap_usd:
            return Stop("cap", f"recorded cost ${charged:.4f} reached the cap ${self.plan.cap_usd:.2f}")
        hit, finished = evidence.streak(self.plan.output_root, self.plan.no_reward_streak)
        if hit:
            return Stop("streak", f"the newest {self.plan.no_reward_streak} of {finished} finished "
                                  "trials have no reward")
        limit = self.plan.max_arm_minutes * 60
        if self.system.now() - job.started >= limit:
            return Stop("timeout", f"{job.step.out} ran {limit:.0f} s, the max_arm_minutes limit")
        return None

    def _watch(self, run: LaneRun) -> None:
        job = run.job
        assert job is not None
        code = self.system.poll(job.pid)
        if code is None:
            stop = self._watch_reason(job)
            if stop is None:
                return
            if stop.reason == "streak":
                try:
                    write_atomic(self.plan.stop_file, f"streak {self.system.utc()} {stop.detail}\n")
                except OSError as error:
                    stop = Stop("streak", f"{stop.detail}; cannot write the STOP file: {error.strerror}")
            code = self.system.terminate(job.pid, self.plan.stop_grace_seconds)
            self._finish(run, stop.reason, code)
            raise stop
        self._finish(run, "ended" if code == 0 else "exit", code)
        if code != 0:
            raise Stop("arm-exit", f"{job.step.out} ended with exit code {code}")
        gap = self._pilot_gap(job.step)
        if gap:
            raise Stop("pilot", gap)

    def _pilot_gap(self, step: Step) -> str:
        if not step.pilot:
            return ""
        counts = evidence.folder_counts(self.plan.output_root / step.out, frozenset())
        planned = len(evidence.task_instances(step.tasks)) * step.attempts
        if counts["with_reward"] >= planned:
            return ""
        return f"pilot {step.out} has {counts['with_reward']} of {planned} trials with a reward"

    def _finish(self, run: LaneRun, status: str, code: int | None) -> None:
        job = run.job
        assert job is not None
        run.job = None
        try:
            self._lane_info(run, "", None)
        except OSError as error:
            run.notes.append(f"cannot clear the lane file: {error.strerror}")
        actual = self._settle(run.lane.name, job.step.out, status, code)
        self.say("SETTLE", run.lane.name, f"{job.step.out}: status {status}, exit {code}, "
                                          f"recorded ${actual['recorded_usd']:.4f}")

    def _end(self, run: LaneRun, stop: Stop | None) -> None:
        assert not run.ended and run.job is None
        nxt = run.items[0] if run.items else None
        before = nxt.out if isinstance(nxt, Step) else (nxt or "-")
        if stop is None:
            self.say("DONE", run.lane.name, f"{run.started} arms started, {run.skipped} skipped")
        else:
            run.stopped = True
            self.say("STOP", run.lane.name, f"{stop.reason} before {before}: {stop.detail}", stop.reason, before)
        if run.held:
            self.system.unlock(self._lane_files(run.lane.name)[0], self.owner)
            run.held = False
        run.ended = True


def drive(runner: Runner) -> int:
    runner.open()
    for _ in range(runner.max_ticks()):
        if not runner.tick():
            return runner.exit_code()
        runner.system.sleep(runner.plan.poll_seconds)
    runner.interrupt("tick-limit", "the runner reached its bounded number of polls")
    return runner.exit_code()
