from __future__ import annotations

import json
import math
import os
import re
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from campaign import evidence, ledger
from campaign.plan import MAX_BALANCE_USD, Lane, Plan, arm_command, base_arm, sha256_file
from campaign.system import Row, System

NUMBER = re.compile(r"-?[0-9]+(\.[0-9]+)?")
MAX_LANE_ITEMS = 4096
TERMINAL = ("STOP", "DONE")
UNSTOPPED = ("harbor-alive", "orphan", "lane-held")
GLOBAL = ("stop-file", "cap", "cap-root", "streak", "interrupted", "crash", "tick-limit")
WAIT_NOTE_SECONDS = 60.0
LOCK_RETRY_SECONDS = 0.5
LOCK_NOTE_SECONDS = 5.0
MUSTER_FORM = "muster"
HARBOR_FORM = "harbor"

Emit = Callable[[dict[str, str]], None]
Match = tuple[int, int, str, str]
Listing = list[Match]


class Stream(Protocol):
    def write(self, text: str, /) -> int: ...

    def flush(self) -> None: ...


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
    start_time: str | None
    termed: bool = False
    stop: Stop | None = None
    phase: int = 0
    stop_at: float = 0.0
    deadline: float = 0.0
    noted_at: float = 0.0
    harbor_termed: set[int] = field(default_factory=set)


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


@dataclass(frozen=True)
class Funds:
    credits: float
    key_left: float | None

    def usable(self) -> float:
        return self.credits if self.key_left is None else min(self.credits, self.key_left)

    def binding(self) -> str:
        return "key limit left" if self.key_left is not None and self.key_left < self.credits else "account credits"

    def text(self) -> str:
        key = "" if self.key_left is None else f", key limit left ${self.key_left:.2f}"
        return f"account credits ${self.credits:.2f}{key}"


def _read_probe(system: System, argv: tuple[str, ...], label: str, timeout: float, owner: int,
                none_ok: bool) -> tuple[float | None, str]:
    assert argv and timeout > 0
    code, text = system.capture(list(argv), timeout, owner)
    value = text.strip()
    if code is None:
        return None, f"the {label} timed out after {timeout:.0f} s or did not start"
    if code != 0:
        return None, f"the {label} exited with {code}"
    if none_ok and value == "none":
        return None, ""
    if not NUMBER.fullmatch(value):
        shape = ", starting with '{'" if value.startswith("{") else ""
        return None, f"the {label} printed {len(value)} characters{shape}, not a number (output not shown)"
    number = float(value)
    if not math.isfinite(number) or number > MAX_BALANCE_USD:
        return None, f"the {label} printed a number above the sane bound ${MAX_BALANCE_USD:.0f}"
    return number, ""


def read_funds(system: System, plan: Plan, owner: int) -> tuple[Funds | None, str]:
    timeout = plan.probe_timeout_seconds
    credits, problem = _read_probe(system, plan.probe, "balance probe", timeout, owner, none_ok=False)
    if credits is None:
        assert problem
        return None, problem
    if not plan.key_limit_probe:
        return Funds(credits, None), ""
    key_left, problem = _read_probe(system, plan.key_limit_probe, "key limit probe", timeout, owner, none_ok=True)
    if problem:
        return None, problem
    return Funds(credits, key_left), ""


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    staged.write_text(text, encoding="utf-8")
    os.replace(staged, path)


def _ends_token(command: str, text: str) -> bool:
    assert text
    start = command.find(text)
    for _ in range(len(command) + 1):
        if start < 0:
            return False
        end = start + len(text)
        if end == len(command) or command[end].isspace():
            return True
        start = command.find(text, start + 1)
    return False


def command_form(command: str, folder: str) -> str:
    assert folder.startswith("/")
    if _ends_token(command, " run -c " + folder + ".job.json"):
        return HARBOR_FORM
    if _ends_token(command, " --out " + folder) or _ends_token(command, " --out=" + folder):
        return MUSTER_FORM
    return ""


def protected_ids(rows: list[Row], me: int) -> set[int]:
    parents = {pid: (ppid, pgid) for pid, ppid, pgid, _ in rows}
    protected = {0, 1, me}
    pid = me
    for _ in range(len(parents) + 1):
        if pid not in parents:
            break
        ppid, pgid = parents[pid]
        protected |= {pid, pgid}
        if ppid <= 1 or ppid in protected:
            break
        pid = ppid
    return protected


def arm_matches(rows: list[Row], plan: Plan, outs: list[str], protected: set[int]) -> Listing:
    folders = [(out, str(plan.output_root / out)) for out in sorted(set(outs))]
    found: Listing = []
    for pid, _, pgid, command in rows:
        if pid in protected:
            continue
        for out, folder in folders:
            kind = command_form(command, folder)
            if kind:
                found.append((pid, pgid, out, kind))
                break
    return found


class EventLog:
    def __init__(self, stream: Stream | None, path: Path, clock: Callable[[], str]) -> None:
        self.stream = stream
        self.path = path
        self.clock = clock
        self.stream_failed = False
        self.lines = 0

    def write(self, line: str) -> None:
        if self.stream is not None and not self.stream_failed:
            try:
                self.stream.write(line + "\n")
                self.stream.flush()
            except (OSError, ValueError):
                self.stream_failed = True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as log:
                log.write(line + "\n")
            self.lines += 1
        except OSError:
            pass

    def __call__(self, event: dict[str, str]) -> None:
        self.write(f"{self.clock()} {event['kind']} lane {event['lane']}: {event['text']}")


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
        self.protected: set[int] = {0, 1, owner}
        since = evidence.parse_utc(system.utc())
        assert since is not None
        self.since: datetime = since

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
                self._raised(run, stop)
        return any(not run.ended for run in self.lanes)

    def _raised(self, run: LaneRun, stop: Stop) -> None:
        if stop.reason in GLOBAL:
            self._stop_all(stop)
        if run.job is None:
            self._end(run, stop)

    def _stop_all(self, stop: Stop) -> None:
        for run in self.lanes:
            if not run.ended and run.job is not None and run.job.stop is None:
                self._begin_stop(run, stop)

    def interrupt(self, reason: str, detail: str) -> None:
        stop = Stop(reason, detail)
        for run in self.lanes:
            if not run.ended and run.job is not None and run.job.stop is None:
                self._guarded(run, lambda each: self._begin_stop(each, stop))
        rounds = math.ceil(2 * self.plan.stop_grace_seconds) + 4
        for _ in range(rounds):
            busy = [run for run in self.lanes if not run.ended and run.job is not None]
            if not busy:
                break
            for run in busy:
                self._guarded(run, self._step_stop)
            if any(not run.ended and run.job is not None for run in self.lanes):
                self.system.sleep(1)
        for run in self.lanes:
            if not run.ended and run.job is not None:
                job, run.job = run.job, None
                self._end(run, self._alive_stop(job, self._listing([job.step.out]), "the stop wait ran out"))
            elif not run.ended:
                self._end(run, stop)

    def _guarded(self, run: LaneRun, action: Callable[[LaneRun], None]) -> None:
        try:
            action(run)
        except Stop as stop:
            if run.job is None:
                self._end(run, stop)
        except Exception as error:
            job, run.job = run.job, None
            self._end(run, self._failed_stop(job, f"stopping the arm failed: {error!r}"))

    def _failed_stop(self, job: Job | None, detail: str) -> Stop:
        if job is None:
            return Stop("crash", detail)
        listing = self._listing([job.step.out])
        if listing != [] or self.system.poll(job.pid) is None:
            return self._alive_stop(job, listing, detail)
        reason = job.stop.reason if job.stop is not None else "crash"
        return Stop(reason, f"{detail}; {job.step.out} no longer runs; its reserve stays open until the next run")

    def exit_code(self) -> int:
        assert all(run.ended for run in self.lanes)
        return 1 if any(run.stopped for run in self.lanes) else 0

    def max_ticks(self) -> int:
        attempts = max(arm.attempts for arm in self.plan.arms)
        steps = sum(len(run.items) for run in self.lanes) * (attempts + 1)
        per_step = math.ceil(self.plan.max_arm_minutes * 60 / self.plan.poll_seconds)
        per_step += math.ceil(2 * self.plan.stop_grace_seconds / self.plan.poll_seconds) + 3
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

    def _lane_info(self, run: LaneRun, arm: str, child: int | None, child_start: str | None) -> None:
        _, info = self._lane_files(run.lane.name)
        record = {"owner": self.owner, "host": self.system.host(), "arm": arm, "child": child,
                  "child_start": child_start, "since": self.system.utc(), "plan_sha256": self.sha}
        write_atomic(info, json.dumps(record, indent=1) + "\n")

    def _listing(self, outs: list[str]) -> Listing | None:
        if not outs:
            return []
        try:
            rows = self.system.processes()
        except (OSError, ValueError):
            return None
        self.protected = protected_ids(rows, self.owner)
        return arm_matches(rows, self.plan, outs, self.protected)

    def _signal(self, pgid: int, number: int) -> None:
        if pgid <= 1 or pgid in self.protected:
            return
        try:
            self.system.signal_group(pgid, number)
        except OSError:
            return

    def _signal_process(self, pid: int, number: int) -> None:
        if pid <= 1 or pid in self.protected:
            return
        try:
            self.system.signal_process(pid, number)
        except OSError:
            return

    def _state(self) -> dict[str, ledger.ArmLedger]:
        try:
            return ledger.state(self.plan.ledger, self.plan.id)
        except ledger.LedgerError as error:
            raise Stop("ledger", str(error)) from error

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
        state = self._state()
        outs = [*run.lane.arms, *(out for out in state if base_arm(out) in run.lane.arms)]
        if isinstance(info.get("arm"), str) and info["arm"]:
            outs.append(info["arm"])
        self._refuse_orphans(info, outs)
        self._settle_interrupted(run, state)
        self._lane_info(run, "", None, None)

    def _refuse_orphans(self, info: dict[str, Any], outs: list[str]) -> None:
        listing = self._listing(outs)
        if listing is None:
            raise Stop("orphan", "cannot list processes with ps; check for a running arm by hand")
        if listing:
            shown = ", ".join(f"pid {pid} ({kind} of {out})" for pid, _, out, kind in listing[:8])
            raise Stop("orphan", f"processes of an earlier runner still run: {shown}; a paid job may "
                                 "still spend; run again after they end")
        child, child_start = info.get("child"), info.get("child_start")
        if not isinstance(child, int) or child <= 1:
            return
        if info.get("host") != self.system.host():
            raise Stop("orphan", f"arm {info.get('arm')} was started on host {info.get('host')}")
        if isinstance(child_start, str) and self.system.start_time(child) == child_start:
            raise Stop("orphan", f"arm {info.get('arm')} of an earlier runner still runs as process "
                                 f"group {child}; run again after it ends")

    def _settle_interrupted(self, run: LaneRun, state: dict[str, ledger.ArmLedger]) -> None:
        for out, arm in sorted(state.items()):
            if not arm.open or base_arm(out) not in run.lane.arms:
                continue
            for _ in range(arm.reserves - arm.settles):
                self._settle(run.lane.name, out, "interrupted", None)
            self.say("SETTLE", run.lane.name, f"{out}: an earlier runner ended without a settle line; "
                                              "no process of it runs; settled now as interrupted")

    def _settle(self, lane: str, out: str, status: str, code: int | None) -> dict[str, Any]:
        folder = self.plan.output_root / out
        try:
            actual = evidence.scan_cost([folder] if folder.exists() else [], self.system.walk).record()
        except OSError as error:
            actual = {"error": f"cannot read {folder}: {error}"}
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
        item = self._state().get(step.out)
        if item is not None and (item.open or any(status != "spawn-failed" for status in item.statuses)):
            return "the ledger has a reserve line for it"
        return ""

    def _open_items(self) -> dict[tuple[str, str], float]:
        items: dict[tuple[str, str], float] = {}
        for name, arm in self._state().items():
            if arm.open and arm.reserves:
                items[("own", name)] = arm.reserved_usd * (arm.reserves - arm.settles) / arm.reserves
        now = evidence.parse_utc(self.system.utc())
        assert now is not None
        try:
            foreign = ledger.foreign_open(self.plan.ledger, self.plan.id, self.plan.provider, now,
                                          self.plan.foreign_reserve_window_hours, self.plan.largest_reserve_usd())
        except ledger.LedgerError as error:
            raise Stop("ledger", str(error)) from error
        for item in foreign:
            key = ("foreign", str(item.line_number))
            assert key not in items, key
            items[key] = item.reserved_usd
        return items

    def _charged(self) -> float:
        missing = [root for root in self.plan.cap_roots if root != self.plan.output_root and not root.is_dir()]
        if missing:
            raise Stop("cap-root", f"cap root {missing[0]} is not a folder; its cost would count as $0")
        try:
            cost = evidence.scan_cost([root for root in self.plan.cap_roots if root.is_dir()], self.system.walk)
        except OSError as error:
            raise Stop("cap-root", f"cannot read the cap roots: {error}") from error
        return cost.charged(self.plan.unknown_seat_usd)

    def _gate_cost(self) -> None:
        charged = self._charged()
        if charged >= self.plan.cap_usd:
            raise Stop("cap", f"recorded cost ${charged:.4f} is at or above the cap ${self.plan.cap_usd:.2f}")
        hit, finished = evidence.streak(self.plan.output_root, self.plan.no_reward_streak, self.since)
        if hit:
            raise Stop("streak", f"the newest {self.plan.no_reward_streak} of {finished} trials that ended "
                                 "since this runner started have no reward")

    def _gate_balance(self, step: Step) -> float:
        before = self._open_items()
        funds, problem = read_funds(self.system, self.plan, self.owner)
        if funds is None:
            raise Stop("balance-unreadable", problem)
        after = self._open_items()
        either = {key: max(before.get(key, 0.0), after.get(key, 0.0)) for key in before.keys() | after.keys()}
        own = sum(amount for key, amount in either.items() if key[0] == "own")
        foreign = sum(amount for key, amount in either.items() if key[0] == "foreign")
        balance = funds.usable()
        need = round(self.plan.floor_usd + step.reserve_usd + own + foreign, 6)
        if balance < need:
            raise Stop("balance-low", f"{funds.text()}: {funds.binding()} ${balance:.2f} is below floor "
                                      f"${self.plan.floor_usd:.2f} + reserve ${step.reserve_usd:.2f} + open reserves "
                                      f"${own:.2f} + open reserves of other writers in the ledger ${foreign:.2f}")
        return balance

    def _advance(self, run: LaneRun) -> None:
        for _ in range(MAX_LANE_ITEMS):
            if not run.items:
                self._end(run, None)
                return
            self._gate_common()
            item = run.items[0]
            if isinstance(item, str):
                self._gate_cost()
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
            if not evidence.task_instances(item.tasks):
                raise Stop("tasks-missing", f"{item.tasks} holds no task")
            self._gate_cost()
            if self._start_locked(run, item):
                run.items.pop(0)
            return
        raise AssertionError("a lane holds more items than MAX_LANE_ITEMS")

    def money_lock(self) -> Path:
        return self.plan.ledger.with_name(self.plan.ledger.name + ".lock")

    def _take_money_lock(self, run: LaneRun, lock: Path) -> bool:
        started = self.system.now()
        rounds = math.ceil(self.plan.probe_timeout_seconds / LOCK_RETRY_SECONDS)
        noted = False
        for index in range(rounds + 1):
            try:
                if self.system.try_lock(lock, self.owner):
                    return True
            except OSError as error:
                raise Stop("money-lock", f"cannot lock {lock}: {error.strerror}") from error
            waited = self.system.now() - started
            if waited >= LOCK_NOTE_SECONDS and not noted:
                noted = True
                self.say("WAIT", run.lane.name, f"money lock {lock} is busy for {waited:.0f} s; another runner "
                                                f"checks the balance; waiting up to "
                                                f"{self.plan.probe_timeout_seconds:.0f} s", "money-lock")
            if index < rounds:
                self.system.sleep(LOCK_RETRY_SECONDS)
        self.say("WAIT", run.lane.name, f"money lock {lock} stayed busy for {self.plan.probe_timeout_seconds:.0f} s; "
                                        "trying again at the next poll", "money-lock")
        return False

    def _start_locked(self, run: LaneRun, step: Step) -> bool:
        lock = self.money_lock()
        if not self._take_money_lock(run, lock):
            return False
        try:
            balance = self._gate_balance(step)
            self._gate_common()
            self._gate_cost()
            self._start(run, step, balance)
        finally:
            self.system.unlock(lock, self.owner)
        return True

    def _start(self, run: LaneRun, step: Step, balance: float) -> None:
        concurrency = run.lane.concurrency
        tasks = len(evidence.task_instances(step.tasks))
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
            raise Stop("spawn", f"cannot start {step.out}: {error}; its reserve is settled") from error
        run.job = Job(step, pid, self.system.now(), None)
        run.started += 1
        run.job.start_time = self.system.start_time(pid)
        self._lane_info(run, step.out, pid, run.job.start_time)
        self.say("START", run.lane.name, f"{step.out}: {tasks} tasks x {step.attempts} attempts, "
                                         f"{concurrency} at once, balance ${balance:.2f}, "
                                         f"reserve ${step.reserve_usd:.2f}")

    def _watch_reason(self, job: Job) -> Stop | None:
        if self.plan.stop_file.exists():
            return Stop("stop-file", f"{self.plan.stop_file} is present")
        try:
            self._gate_cost()
        except Stop as stop:
            return stop
        limit = self.plan.max_arm_minutes * 60
        if self.system.now() - job.started >= limit:
            return Stop("timeout", f"{job.step.out} ran {limit:.0f} s, the max_arm_minutes limit")
        return None

    def _watch(self, run: LaneRun) -> None:
        job = run.job
        assert job is not None
        if job.stop is not None:
            self._step_stop(run)
            return
        code = self.system.poll(job.pid)
        if code is not None and self._listing([job.step.out]) == []:
            self._finish(run, "ended" if code == 0 else "exit", code)
            if code != 0:
                raise Stop("arm-exit", f"{job.step.out} ended with exit code {code}")
            gap = self._pilot_gap(job.step)
            if gap:
                raise Stop("pilot", gap)
            return
        stop = self._watch_reason(job)
        if stop is None:
            return
        if stop.reason == "streak":
            try:
                write_atomic(self.plan.stop_file, f"streak {self.system.utc()} {stop.detail}\n")
            except OSError as error:
                stop = Stop("streak", f"{stop.detail}; cannot write the STOP file: {error.strerror}")
        if stop.reason in GLOBAL:
            self._stop_all(stop)
        else:
            self._begin_stop(run, stop)

    def _term(self, job: Job) -> None:
        if not job.termed:
            job.termed = True
            self._signal_process(job.pid, signal.SIGTERM)

    def _term_harbor(self, job: Job, listing: Listing) -> None:
        for pid, pgid, _, kind in listing:
            if kind == HARBOR_FORM and pid == pgid and pgid != job.pid and pgid not in job.harbor_termed:
                job.harbor_termed.add(pgid)
                self._signal(pgid, signal.SIGTERM)

    def _begin_stop(self, run: LaneRun, stop: Stop) -> None:
        job = run.job
        assert job is not None and job.stop is None
        now = self.system.now()
        job.stop, job.phase, job.stop_at, job.noted_at = stop, 1, now, now
        job.deadline = now + self.plan.stop_grace_seconds
        self._term(job)
        if self.system.poll(job.pid) is not None:
            self._term_harbor(job, self._listing([job.step.out]) or [])
        limit = 2 * self.plan.stop_grace_seconds
        self.say("STOPPING", run.lane.name, f"{job.step.out}: {stop.reason}: sent TERM; waiting up to "
                                            f"{limit:.0f} s for {job.step.out} to stop; do not kill this runner",
                 stop.reason, job.step.out)

    def _step_stop(self, run: LaneRun) -> None:
        job = run.job
        assert job is not None and job.stop is not None
        listing = self._listing([job.step.out])
        code = self.system.poll(job.pid)
        if code is not None and listing == []:
            self._finish(run, job.stop.reason, code)
            raise job.stop
        now = self.system.now()
        if now >= job.deadline and job.phase == 1:
            job.phase, job.deadline = 2, now + self.plan.stop_grace_seconds
            self._signal(job.pid, signal.SIGKILL)
            self._term_harbor(job, listing or [])
        elif now >= job.deadline:
            run.job = None
            raise self._alive_stop(job, listing, "")
        if now - job.noted_at >= WAIT_NOTE_SECONDS:
            job.noted_at = now
            left = "unknown (ps failed)" if listing is None else str(len(listing))
            self.say("WAIT", run.lane.name, f"{job.step.out}: still stopping after {now - job.stop_at:.0f} s "
                                            f"({job.stop.reason}); processes left: {left}; do not kill this runner")

    def _alive_stop(self, job: Job, listing: Listing | None, extra: str) -> Stop:
        cause = job.stop or Stop("crash", "the runner stopped")
        folder = self.plan.output_root / job.step.out
        groups = sorted({pgid for _, pgid, _, _ in listing or []} | {job.pid})
        pids = "unknown (ps failed)" if listing is None else ", ".join(str(pid) for pid, _, _, _ in listing[:16])
        term = "; ".join(f"kill -TERM -{group}" for group in groups)
        kill = "; ".join(f"kill -KILL -{group}" for group in groups)
        detail = f"{cause.reason}: {cause.detail}"
        if extra:
            detail += f"; {extra}"
        return Stop("harbor-alive", f"{detail}; a paid job still runs: {job.step.out} (job folder {folder}), "
                                    f"pids {pids or 'none listed'}; its reserve stays open. Run: {term}; wait "
                                    f"120 s; if they still run: {kill}. A KILL can leave Daytona sandboxes "
                                    "that still cost money: check `daytona sandbox list` and delete the "
                                    f"sandboxes of {job.step.out}. Then run again")

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
            self._lane_info(run, "", None, None)
        except OSError as error:
            run.notes.append(f"cannot clear the lane file: {error.strerror}")
        actual = self._settle(run.lane.name, job.step.out, status, code)
        recorded = actual.get("recorded_usd")
        shown = f"${recorded:.4f}" if isinstance(recorded, float | int) else "unknown"
        self.say("SETTLE", run.lane.name, f"{job.step.out}: status {status}, exit {code}, recorded {shown}")

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
            run.held = False
            try:
                self.system.unlock(self._lane_files(run.lane.name)[0], self.owner)
            except (OSError, KeyError) as error:
                run.notes.append(f"cannot release the lane lock: {error}")
        run.ended = True


def drive(runner: Runner) -> int:
    runner.open()
    for _ in range(runner.max_ticks()):
        if not runner.tick():
            return runner.exit_code()
        runner.system.sleep(runner.plan.poll_seconds)
    runner.interrupt("tick-limit", "the runner reached its bounded number of polls")
    return runner.exit_code()
