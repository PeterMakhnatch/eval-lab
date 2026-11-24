from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import re
import shutil
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from typing import Any, TypedDict

from campaign import evidence, fill, ledger, report
from campaign.plan import RERUN_TAG, Plan, PlanError, base_arm, load, parse, sha256_file
from campaign.runner import TERMINAL, UNSTOPPED, EventLog, Runner, Stop, expand_first_run
from campaign.sim_fixture import (
    OTHER_PID,
    OTHER_START,
    STOP_GRACE,
    BrokenStream,
    FakeJob,
    Scenario,
    World,
    plan_data,
    scenario,
    toml_text,
    write_supply,
)

_REGRESSIONS = Path(__file__).with_name("sim_regressions.json")
_PLANS = Path(__file__).with_name("plans")
_FIXED_SEEDS = range(128)
_MAX_REGRESSIONS = 512
_MAX_SEED = 2**64 - 1
_DEFAULT_HUNT_SEED = 1_124_007
_MAX_HUNT_SEEDS = 1_000_000
_MAX_STEPS = 4000
_MAX_RESUMES = 12


@dataclass(frozen=True)
class Violation:
    seed: int
    step: str
    invariant: str
    detail: str

    def __str__(self) -> str:
        return f"seed {self.seed} at {self.step}: {self.invariant}: {self.detail}"


class Run:
    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.failures: list[Violation] = []
        self.checks = 0
        self.case = ""
        self.step = "start"

    def check(self, ok: bool, invariant: str, detail: str) -> None:
        self.checks += 1
        if not ok and len(self.failures) < 64:
            self.failures.append(Violation(self.seed, self.step, invariant, detail))


@dataclass
class Proc:
    owner: int
    runner: Runner
    start_sha: str
    mode: str
    lanes: tuple[str, ...]
    killed: bool = False
    done: bool = False
    terminal: Counter[str] = field(default_factory=Counter)
    reasons: dict[str, str] = field(default_factory=dict)


Mutation = Callable[[dict[str, Any]], None]


def _mutations() -> dict[str, Mutation]:
    def at_once(d: dict[str, Any]) -> None:
        d["lanes"][0]["concurrency"] = d["max_trials_at_once"] + 1

    def no_harbor(d: dict[str, Any]) -> None:
        d["command"]["argv"] = [arg for arg in d["command"]["argv"] if arg != "{harbor}"]

    return {
        "unknown-top-key": lambda d: d.update(extra=1),
        "unknown-arm-key": lambda d: d["arms"][0].update(color="red"),
        "missing-money-key": lambda d: d["money"].pop("floor_usd"),
        "bool-attempts": lambda d: d["arms"][0].update(attempts=True),
        "float-attempts": lambda d: d["arms"][0].update(attempts=2.0),
        "relative-path": lambda d: d.update(output_root="out"),
        "duplicate-arm": lambda d: d["arms"].append(dict(d["arms"][0])),
        "arm-in-two-lanes": lambda d: d["lanes"][0]["arms"].append(d["lanes"][-1]["arms"][0]),
        "arm-in-no-lane": lambda d: d["arms"].append(dict(d["arms"][0], name="lonely")),
        "lane-unknown-arm": lambda d: d["lanes"][0]["arms"].append("ghost"),
        "too-many-at-once": at_once,
        "argv-without-out": lambda d: d["command"].update(argv=[a for a in d["command"]["argv"] if a != "{out}"]),
        "unknown-field": lambda d: d["command"]["argv"].append("{model}"),
        "no-harbor": no_harbor,
        "expected-above-reserve": lambda d: d["arms"][0].update(expected_usd=d["arms"][0]["reserve_usd"] + 1),
        "ledger-in-output": lambda d: d["money"].update(ledger=d["output_root"] + "/ledger.jsonl"),
        "cap-misses-output": lambda d: d["money"].update(cap_roots=d["money"]["cap_roots"][:1]),
        "detector-without-field": lambda d: d.update(rerun={"detector": ["x"], "detector_timeout_seconds": 5}),
        "bad-schema": lambda d: d.update(schema="campaign-plan/v0"),
        "nan-floor": lambda d: d["money"].update(floor_usd=float("nan")),
        "state-inside-output": lambda d: d.update(state_root=d["output_root"] + "/state"),
    }


def _schema(run: Run, case: Scenario, root: Path, rng: Random) -> None:
    run.step = "schema"
    data = plan_data(case, root)
    try:
        parse(toml_text(data).encode(), root / "plan.toml")
        run.check(True, "plan-schema-accepts", "the generated plan")
    except PlanError as error:
        run.check(False, "plan-schema-accepts", str(error))
    mutations = _mutations()
    for name in rng.sample(sorted(mutations), 5):
        changed = copy.deepcopy(data)
        mutations[name](changed)
        try:
            parse(toml_text(changed).encode(), root / "plan.toml")
            run.check(False, "plan-schema-refuses", name)
        except PlanError:
            run.check(True, "plan-schema-refuses", name)


def _examples(run: Run) -> None:
    run.step = "examples"
    for path in sorted(_PLANS.glob("*.toml")):
        try:
            plan = load(path)
        except PlanError as error:
            run.check(False, "example-plan-valid", f"{path.name}: {error}")
            continue
        run.check(True, "example-plan-valid", path.name)
        if path.name == "e7-crowdrules.toml":
            order = [(lane.name, lane.concurrency, lane.arms) for lane in plan.lanes]
            want = [("a", 4, ("solo-24", "zones-8", "chief-8", "zones-4", "chief-4")),
                    ("b", 4, ("team-8", "share-8", "team-4", "share-4"))]
            run.check(order == want, "e7-order", repr(order))
            run.check(plan.harbor_run_args[-1] == "dind_snapshot=muster-dind-mirror-v1", "e7-sandbox-image",
                      repr(plan.harbor_run_args))
            reserves = {arm.name: arm.reserve_usd for arm in plan.arms}
            run.check(reserves["solo-24"] == 0.05 and reserves["zones-4"] == 0.5 and reserves["team-8"] == 1.1,
                      "e7-reserves", repr(reserves))


class Sim:
    def __init__(self, run: Run, case: Scenario, root: Path) -> None:
        self.run = run
        self.case = case
        self.root = root
        self.world = World(case, root, Random(case.seed ^ 0x5EED5EED))
        self.world.spawn_hook = self._on_spawn
        self.world.probe_hook = self._probe_hook if case.concurrent_probe else None
        self.plan_path = root / "plan.toml"
        self.procs: dict[int, Proc] = {}
        self.next_owner = 1000
        self.phase = "run"
        self.spawned: dict[str, int] = {}
        self.last_job: dict[tuple[int, str], int] = {}
        self.pending: list[tuple[float, tuple[str, ...]]] = []
        self.operator: list[tuple[float, str, bool]] = []
        self.tried: set[str] = set()
        self.resumes = 0
        self.fired: set[str] = set()
        self.events = 0
        self.interrupting = 0
        self.step_started = 0.0

    def plan(self) -> Plan:
        return load(self.plan_path)

    def start(self, lanes: tuple[str, ...], mode: str, broken_after: int = 0) -> Proc:
        plan = self.plan()
        owner = self.next_owner
        self.next_owner += 1
        self.world.live_owners.add(owner)
        stream = BrokenStream(broken_after) if broken_after else io.StringIO()
        log = EventLog(stream, plan.state_root / "campaign.log", self.world.utc)

        def emit(event: dict[str, str]) -> None:
            log(event)
            self._on_event(owner, event)

        expand = expand_first_run if mode == "run" else fill.expand_rerun
        runner = Runner(plan, self.world, owner, emit, [plan.lane(name) for name in lanes], expand)
        proc = Proc(owner, runner, plan.sha256, mode, lanes)
        self.procs[owner] = proc
        runner.open()
        return proc

    def lane_of(self, out: str) -> str:
        return self.plan().lane_of(base_arm(out)).name

    def alive_jobs(self) -> list[FakeJob]:
        return [job for job in self.world.jobs.values() if job.alive]

    def lane_jobs(self, lane: str) -> list[FakeJob]:
        return [job for job in self.alive_jobs() if self.lane_of(job.out.name) == lane]

    def rewarded(self, out: Path, skip: frozenset[str]) -> set[str]:
        return {trial.name for path, trial in self.world.truth.trials.items()
                if Path(path).parent == out and trial.rewarded and trial.name not in skip}

    def stalled(self, out: Path) -> frozenset[str]:
        if not self.case.detector:
            return frozenset()
        return frozenset(trial.name for path, trial in self.world.truth.trials.items()
                         if Path(path).parent == out and trial.fault == "stalled" and trial.end is not None)

    def pilot_short(self, plan: Plan, name: str) -> bool:
        arm = plan.arm(name)
        out = plan.output_root / name
        planned = len(self.case.arm(name).instances) * arm.attempts
        return not out.is_dir() or len(self.rewarded(out, frozenset())) < planned

    def finish_proc(self, proc: Proc) -> None:
        proc.done = True
        held = [path.name for path, owner in self.world.locks.items() if owner == proc.owner]
        saved, self.run.step = self.run.step, f"{self.phase}/exit"
        self.run.check(not held, "locks-released", f"owner {proc.owner} still holds {held}")
        self.run.step = saved
        self.world.kill_owner(proc.owner)

    def _on_event(self, owner: int, event: dict[str, str]) -> None:
        self.events += 1
        proc, lane, reason = self.procs[owner], event["lane"], event["reason"]
        if event["kind"] not in TERMINAL:
            return
        proc.terminal[lane] += 1
        proc.reasons[lane] = reason or "done"
        saved, self.run.step = self.run.step, f"{self.phase}/{event['kind'].lower()}/{reason or 'done'}"
        alive = [job.out.name for job in self.lane_jobs(lane)]
        if reason not in UNSTOPPED:
            self.run.check(not alive, "stopped-means-gone", f"lane {lane} ends while {alive} run")
        if event["kind"] == "STOP":
            self.run.check(self._stop_is_true(proc, event), "stop-reason-is-true",
                           f"{reason} in lane {lane} before {event['before']}")
        if reason == "harbor-alive":
            unsignalled = [job.out.name for job in self.lane_jobs(lane)
                           if job.harbor_alive and job.harbor_term_at is None and not job.unstoppable]
            self.run.check(not unsignalled, "harbor-signalled", f"Harbor of {unsignalled} never got a TERM")
        self.run.step = saved
        if reason == "orphan":
            self.operator.append((self.world.t + 60, lane, not self.case.operator_top_only or lane in self.tried))
            self.tried.add(lane)
        if reason == "harbor-alive":
            for job in self.lane_jobs(lane):
                job.abandoned = True
            self.operator.append((self.world.t + 600, lane, True))
        if event["kind"] == "DONE" and proc.mode == "run":
            plan = proc.runner.plan
            missing = [arm for arm in plan.lane(lane).arms
                       if arm not in self.spawned and not (plan.output_root / arm).exists()]
            self.run.check(not missing, "done-means-all-started", f"lane {lane} missed {missing}")

    def _job_reason(self, proc: Proc, lane: str, reason: str) -> bool:
        plan = proc.runner.plan
        if reason == "pilot":
            pilots = [arm for arm in plan.lane(lane).arms if plan.arm(arm).pilot]
            return any(self.pilot_short(plan, arm) for arm in pilots)
        pid = self.last_job.get((proc.owner, lane))
        if pid is None:
            return False
        job = self.world.jobs[pid]
        if reason == "arm-exit":
            return job.top_exit not in (None, 0)
        assert reason == "timeout"
        return self.world.t - job.started >= plan.max_arm_minutes * 60

    def _held(self, plan: Plan, skip: str) -> float:
        held = 0.0
        for name, item in ledger.state(plan.ledger, plan.id).items():
            if name != skip and item.open and item.reserves:
                held += item.reserved_usd * (item.reserves - item.settles) / item.reserves
        return held

    def _balance_low(self, proc: Proc, before: str) -> bool:
        plan = proc.runner.plan
        probe = self.world.probes.get(proc.owner)
        if probe is None or probe.time != self.world.t or probe.value is None:
            return False
        arm = plan.arm(base_arm(before))
        reserve = arm.rerun_reserve_usd if RERUN_TAG in before else arm.reserve_usd
        return probe.value < round(plan.floor_usd + reserve + self._held(plan, ""), 6)

    def _orphan_is_true(self, proc: Proc, lane: str) -> bool:
        if self.lane_jobs(lane):
            return True
        info = evidence.read_json(proc.runner.plan.lane_dir() / f"{lane}.json")
        if not isinstance(info, dict) or not isinstance(info.get("child"), int):
            return False
        start = info.get("child_start")
        return isinstance(start, str) and self.world.start_time(info["child"]) == start

    def _streak_in_window(self, proc: Proc) -> bool:
        moments = [self.step_started, *self.world.end_times(self.step_started), self.world.t]
        return any(self.world.truth_streak(proc.runner.since, moment) for moment in moments)

    def _stop_is_true(self, proc: Proc, event: dict[str, str]) -> bool:
        reason, lane, before = event["reason"], event["lane"], event["before"]
        plan, world = proc.runner.plan, self.world
        truths: dict[str, Callable[[], bool]] = {
            "stop-file": lambda: plan.stop_file.exists(),
            "plan-changed": lambda: sha256_file(self.plan_path) != proc.start_sha,
            "cap": lambda: world.truth.charged_micro >= self.case.cap_micro,
            "cap-root": lambda: any(not root.is_dir() for root in plan.cap_roots if root != plan.output_root),
            "streak": lambda: self._streak_in_window(proc),
            "balance-low": lambda: self._balance_low(proc, before),
            "orphan": lambda: self._orphan_is_true(proc, lane),
            "harbor-alive": lambda: bool(self.lane_jobs(lane)),
            "spawn": lambda: before == self.case.spawn_fail_arm,
            "detector": lambda: self.case.detector_fail,
            "interrupted": lambda: self.interrupting == proc.owner,
        }
        if reason == "balance-unreadable":
            probe = world.probes.get(proc.owner)
            return probe is not None and probe.time == world.t and probe.value is None
        if reason == "lane-held":
            holder = world.locks.get(plan.lane_dir() / f"{lane}.lock")
            return holder is not None and holder != proc.owner and holder in world.live_owners
        if reason in ("arm-exit", "timeout", "pilot"):
            return self._job_reason(proc, lane, reason)
        return reason in truths and truths[reason]()

    def _on_spawn(self, argv: list[str], owner: int) -> None:
        proc = self.procs[owner]
        plan, world, check = proc.runner.plan, self.world, self.run.check
        out = Path(argv[2])
        name, base = out.name, base_arm(out.name)
        lane = plan.lane_of(base)
        saved, self.run.step = self.run.step, f"{self.phase}/spawn"
        check(name not in self.spawned and not out.exists(), "single-start", name)
        check(int(argv[4]) == lane.concurrency, "lane-concurrency", f"{name} got {argv[4]}")
        busy = [job.out.name for job in self.lane_jobs(lane.name)]
        check(not busy, "one-arm-per-lane", f"{name} starts while {busy} runs")
        load = sum(job.concurrency for job in self.alive_jobs()) + lane.concurrency
        check(load <= plan.max_trials_at_once, "trials-at-once", f"{load} > {plan.max_trials_at_once}")
        lines = [item for item in ledger.entries(plan.ledger, plan.id) if item["arm"] == name]
        check(bool(lines) and lines[-1]["event"] == "reserve", "reserve-before-start", f"{name}: {lines}")
        self._spawn_money(proc, name)
        check(not plan.stop_file.exists(), "stop-file-before-start", name)
        check(not world.truth_streak(proc.runner.since), "streak-before-start", name)
        check(sha256_file(self.plan_path) == proc.start_sha, "plan-unchanged-at-start", name)
        wrapper = Path(argv[5])
        same = wrapper == plan.wrapper_path() and wrapper.read_text() == plan.wrapper_text()
        check(same and "snap=sim-v1" in wrapper.read_text(), "wrapper-from-plan", str(wrapper))
        if RERUN_TAG in name:
            self._rerun_truth(plan, name, argv)
        else:
            earlier = lane.arms[: lane.arms.index(base)]
            short = [arm for arm in earlier if plan.arm(arm).pilot and self.pilot_short(plan, arm)]
            check(not short, "pilot-gate", f"{name} starts after short pilot {short}")
        self.spawned[name] = owner
        self.last_job[(owner, lane.name)] = world.next_pid
        self.run.step = saved

    def _spawn_money(self, proc: Proc, name: str) -> None:
        plan, world, check = proc.runner.plan, self.world, self.run.check
        arm = plan.arm(base_arm(name))
        reserve = arm.rerun_reserve_usd if RERUN_TAG in name else arm.reserve_usd
        need = round(plan.floor_usd + reserve + self._held(plan, name), 6)
        probe = world.probes.get(proc.owner)
        value = probe.value if probe is not None and probe.time == world.t else None
        check(value is not None and value >= need - 1e-9, "balance-before-start",
              f"{name}: balance {value} below floor + reserve + open reserves of other arms {need}")
        check(world.truth.charged_micro < self.case.cap_micro, "cap-before-start", name)
        missing = [str(root) for root in plan.cap_roots if root != plan.output_root and not root.is_dir()]
        check(not missing, "cap-roots-present", f"{name} starts while {missing} is missing")

    def _rerun_truth(self, plan: Plan, name: str, argv: list[str]) -> None:
        match = re.fullmatch(rf"([a-z0-9-]+){re.escape(RERUN_TAG)}([0-9]+)", name)
        self.run.check(match is not None and int(match.group(2)) == int(argv[3]), "one-rerun-round", name)
        if match is None:
            return
        base = match.group(1)
        arm = plan.arm(base)
        try:
            record = fill.load_record(plan, base)
        except Stop as stop:
            self.run.check(False, "one-rerun-round", stop.detail)
            return
        outs = [group["out"] for group in record["groups"]] if record else []
        self.run.check(name in outs, "one-rerun-round", f"{name} not in the record {outs}")
        stalled = self.stalled(plan.output_root / base)
        listed = frozenset(record["detected"]) if record else frozenset()
        self.run.check(listed == stalled, "detector-recorded", f"{sorted(listed)} != {sorted(stalled)}")
        measured = Counter(trial.split("__")[0] for trial in self.rewarded(plan.output_root / base, stalled))
        missing = int(match.group(2))
        want = [inst for inst in self.case.arm(base).instances if arm.attempts - measured[inst] == missing]
        staged = sorted(path.name for path in Path(argv[1]).iterdir())
        self.run.check(staged == want, "rerun-covers-unmeasured", f"{name}: {staged} != {want}")

    def _conditions(self) -> tuple[dict[str, bool], set[int]]:
        plan = self.plan()
        flags = {
            "stop-file": plan.stop_file.exists(),
            "cap": self.world.truth.charged_micro >= self.case.cap_micro,
            "cap-root": any(not root.is_dir() for root in plan.cap_roots if root != plan.output_root),
        }
        streak = {proc.owner for proc in self.procs.values()
                  if not proc.done and not proc.killed and self.world.truth_streak(proc.runner.since)}
        return flags, streak

    def _step_checks(self, flags: dict[str, bool], streak: set[int]) -> None:
        self.run.step = f"{self.phase}/watch"
        plan = self.plan()
        alive = self.alive_jobs()
        per_lane = Counter(self.lane_of(job.out.name) for job in alive)
        self.run.check(all(count <= 1 for count in per_lane.values()), "one-arm-per-lane", repr(per_lane))
        load = sum(job.concurrency for job in alive)
        self.run.check(load <= plan.max_trials_at_once, "trials-at-once", f"{load}")
        watched = [job for job in alive if job.owner in self.world.live_owners and not job.abandoned]
        for condition, active in flags.items():
            if active:
                self.run.check(not watched, f"{condition}-stops-running", repr([j.out.name for j in watched]))
        late = [job.out.name for job in watched if job.owner in streak]
        self.run.check(not late, "streak-stops-running", repr(late))
        limit = plan.max_arm_minutes * 60 + 2 * STOP_GRACE + 2 * self.case.poll
        for job in watched:
            self.run.check(self.world.t - job.started < limit, "watchdog", f"{job.out.name}")

    def _faults(self) -> None:
        t, case = self.world.t, self.case
        if case.plan_edit_at is not None and t >= case.plan_edit_at and "edit" not in self.fired:
            self.fired.add("edit")
            with self.plan_path.open("a", encoding="utf-8") as stream:
                stream.write("\n")
        if case.stop_file_at is not None and t >= case.stop_file_at and "stop" not in self.fired:
            self.fired.add("stop")
            self.plan().stop_file.write_text("operator\n", encoding="utf-8")
        if case.cap_root_missing_at is not None and t >= case.cap_root_missing_at and "root" not in self.fired:
            self.fired.add("root")
            shutil.rmtree(self.root / "other")
        live = [proc for proc in self.procs.values() if not proc.done and not proc.killed]
        if case.kill_at is not None and t >= case.kill_at and "kill" not in self.fired and live:
            self.fired.add("kill")
            live[0].killed = True
            self.world.kill_owner(live[0].owner)
            self.pending.append((t + case.kill_delay, live[0].lanes))
        elif case.interrupt_at is not None and t >= case.interrupt_at and "hup" not in self.fired and live:
            self.fired.add("hup")
            self._interrupt(live[-1])

    def _interrupt(self, proc: Proc) -> None:
        ledger_file = proc.runner.plan.ledger
        broken = self.case.ledger_breaks and ledger_file.exists()
        if broken:
            ledger_file.chmod(0o444)
        self.interrupting = proc.owner
        try:
            proc.runner.interrupt("interrupted", "simulated HUP")
        finally:
            self.interrupting = 0
            if broken:
                ledger_file.chmod(0o644)
        self.finish_proc(proc)
        self.pending.append((self.world.t + 30, proc.lanes))

    def _operator(self) -> None:
        due = [item for item in self.operator if item[0] <= self.world.t]
        self.operator = [item for item in self.operator if item[0] > self.world.t]
        for _, lane, full in due:
            for job in self.lane_jobs(lane):
                self.world.operator_kill(job, top_only=not full)
            self.pending.append((self.world.t + 5, (lane,)))

    def _resume(self) -> None:
        due = [lanes for when, lanes in self.pending if when <= self.world.t]
        self.pending = [item for item in self.pending if item[0] > self.world.t]
        for lanes in due:
            if self.resumes < _MAX_RESUMES:
                self.resumes += 1
                self.start(lanes, "run")

    def _probe_hook(self, owner: int) -> None:
        for proc in list(self.procs.values()):
            if proc.owner != owner and not proc.done and not proc.killed and not proc.runner.tick():
                self.finish_proc(proc)

    def _quiet(self) -> bool:
        procs = all(proc.done or proc.killed for proc in self.procs.values())
        return procs and not self.pending and not self.operator and not self.alive_jobs()

    def loop(self) -> None:
        for _ in range(_MAX_STEPS):
            self.step_started = self.world.t
            if self.phase == "run":
                self._faults()
            flags, streak = self._conditions()
            for proc in list(self.procs.values()):
                if not proc.done and not proc.killed and not proc.runner.tick():
                    self.finish_proc(proc)
            self._step_checks(flags, streak)
            self._operator()
            self._resume()
            if self._quiet():
                return
            self.world.advance(self.case.poll)
        self.run.check(False, "bounded-run", f"{self.phase} did not settle in {_MAX_STEPS} polls")

    def stop_lines(self) -> None:
        self.run.step = f"{self.phase}/end"
        for proc in self.procs.values():
            for lane in proc.lanes:
                count = proc.terminal[lane]
                ok = count <= 1 if proc.killed else count == 1
                self.run.check(ok, "one-stop-line", f"owner {proc.owner} lane {lane}: {count} lines")
        self.run.check(not self.world.lock_errors, "lane-lock", repr(self.world.lock_errors[:3]))
        log = self.plan().state_root / "campaign.log"
        lines = len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0
        self.run.check(lines == self.events, "event-log-complete", f"{lines} log lines for {self.events} events")


def _purity(sim: Sim) -> None:
    sim.run.step = "check"
    plan = sim.plan()
    lines, _ = report.check(plan)
    for lane in plan.lanes:
        want = f"lane {lane.name}: {lane.concurrency} trials at once: {' -> '.join(lane.arms)}"
        sim.run.check(any(line.strip() == want for line in lines), "check-shows-order", want)
    untouched = not plan.state_root.exists() and not plan.ledger.exists() and not sim.world.calls
    sim.run.check(untouched, "check-writes-nothing", repr(sim.world.calls))


def _files(folder: Path) -> list[str]:
    return sorted(str(path) for path in folder.rglob("*")) if folder.exists() else []


def _dry_run(sim: Sim) -> None:
    sim.run.step = "fill/dry-run"
    plan = sim.plan()
    before, spawns = _files(plan.state_root / "fill-tasks") + _files(plan.fill_dir()), len(sim.spawned)
    with contextlib.suppress(Stop):
        fill.dry_run(plan, sim.world, 999, list(plan.lanes))
    after = _files(plan.state_root / "fill-tasks") + _files(plan.fill_dir())
    sim.run.check(before == after and len(sim.spawned) == spawns, "dry-run-writes-nothing", repr(after))


def _ledger_end(sim: Sim) -> None:
    sim.run.step = "end/ledger"
    plan = sim.plan()
    lines = ledger.entries(plan.ledger, plan.id)
    events = Counter((item["arm"], item["event"]) for item in lines)
    for arm in sorted({item["arm"] for item in lines}):
        reserves, settles = events[(arm, "reserve")], events[(arm, "settle")]
        failed = sum(1 for item in lines if item["arm"] == arm and item.get("status") == "spawn-failed")
        sim.run.check(reserves <= 1 + failed, "single-start", f"{arm} has {reserves} reserves, {failed} failed")
        sim.run.check(settles == reserves, "settle-after-end", f"{arm}: {reserves} reserves, {settles} settles")
        order = [item["event"] for item in lines if item["arm"] == arm]
        sim.run.check(order[0] == "reserve", "reserve-before-start", f"{arm}: {order}")
    for name in sim.spawned:
        sim.run.check(events[(name, "reserve")] >= 1, "reserve-before-start", name)


def _detected(plan: Plan, name: str) -> frozenset[str]:
    record = fill.load_record(plan, name)
    return frozenset(record["detected"]) if record else frozenset()


def _status_end(sim: Sim) -> None:
    sim.run.step = "end/status"
    plan = sim.plan()
    try:
        result = report.status(plan)
    except PlanError as error:
        sim.run.check(False, "status-readable", str(error))
        return
    for row in result["arms"]:
        name, out = row["arm"], plan.output_root / row["arm"]
        found = _detected(plan, name)
        started = sum(1 for path in sim.world.truth.trials if Path(path).parent == out)
        planned = len(sim.case.arm(name).instances) * plan.arm(name).attempts
        unmeasured = sum(count for kind, count in row["unmeasured"].items() if kind != "not-started")
        sim.run.check(row["started"] == started and row["planned"] == planned, "counts-match-truth",
                      f"{name}: {row['started']}/{row['planned']} != {started}/{planned}")
        sim.run.check(row["with_reward"] == len(sim.rewarded(out, found)), "unmeasured-not-zero",
                      f"{name}: {row['with_reward']} rewards")
        sim.run.check(row["with_reward"] + unmeasured == row["started"], "unmeasured-not-zero",
                      f"{name}: {row['with_reward']} + {unmeasured} != {row['started']}")
        sim.run.check(row["unmeasured"].get("not-started", 0) == max(planned - started, 0), "counts-match-truth",
                      f"{name}: not-started {row['unmeasured']}")
        for rerun, counts in row["reruns"].items():
            truth = len(sim.rewarded(plan.output_root / rerun, found))
            sim.run.check(counts["with_reward"] == truth, "unmeasured-not-zero", f"{rerun}: {counts}")


def _merge_end(sim: Sim) -> None:
    sim.run.step = "end/merge"
    plan = sim.plan()
    dest = sim.root / "merged"
    try:
        report.merge(plan, dest)
    except PlanError as error:
        sim.run.check(False, "merge-measured-only", str(error))
        return
    for arm in plan.arms:
        if arm.pilot:
            sim.run.check(not (dest / arm.name).exists(), "merge-measured-only", f"pilot {arm.name} merged")
            continue
        found = _detected(plan, arm.name)
        want: set[str] = set()
        for folder in [plan.output_root / arm.name, *sorted(plan.output_root.glob(f"{arm.name}{RERUN_TAG}*"))]:
            want |= sim.rewarded(folder, found)
        got = {path.name for path in (dest / arm.name).iterdir()}
        sim.run.check(got == want, "merge-measured-only", f"{arm.name}: {sorted(got ^ want)}")
    folders = [path.name for path in plan.output_root.iterdir() if path.is_dir()] if plan.output_root.exists() else []
    for name in folders:
        ok = ".r" not in name or re.fullmatch(r"[a-z0-9-]+\.r1-m[0-9]+", name) is not None
        sim.run.check(ok, "one-rerun-round", name)


def _stale_lane_file(sim: Sim) -> None:
    case = sim.case
    if not case.pid_reuse:
        return
    plan = sim.plan()
    sim.world.others[OTHER_PID] = "sim other process"
    record: dict[str, Any] = {"owner": 1, "host": "sim-host", "arm": case.lanes[0].arms[0], "child": OTHER_PID}
    if case.pid_reuse == "start":
        record["child_start"] = OTHER_START
    plan.lane_dir().mkdir(parents=True, exist_ok=True)
    (plan.lane_dir() / f"{case.lanes[0].name}.json").write_text(json.dumps(record), encoding="utf-8")


def _run_phases(sim: Sim) -> None:
    case = sim.case
    every = tuple(lane.name for lane in case.lanes)
    groups = [(name,) for name in every] if case.per_lane_runners else [every]
    _stale_lane_file(sim)
    for index, lanes in enumerate(groups):
        sim.start(lanes, "run", case.closed_output_after if index == 0 else 0)
    if case.duplicate:
        twin = sim.start((every[0],), "run")
        sim.run.step = "run/duplicate"
        sim.run.check(twin.reasons.get(every[0]) == "lane-held", "lane-lock", repr(twin.reasons))
    sim.loop()
    sim.phase = "fill"
    stop_file = sim.plan().stop_file
    if stop_file.exists() and case.clears_stop:
        stop_file.unlink()
    _dry_run(sim)
    sim.start(every, "fill")
    sim.loop()
    if case.second_fill:
        sim.start(every, "fill")
        sim.loop()
    sim.stop_lines()


def _run_seed(seed: int) -> Run:
    assert 0 <= seed <= _MAX_SEED
    run, rng, case = Run(seed), Random(seed), scenario(seed)
    run.case = f"lanes {len(case.lanes)} arms {len(case.arms)} probe {case.probe_mode}"
    with TemporaryDirectory(prefix="campaign-sim-") as directory:
        root = Path(directory)
        _schema(run, case, root, rng)
        _examples(run)
        write_supply(case, root)
        (root / "plan.toml").write_text(toml_text(plan_data(case, root)), encoding="utf-8")
        sim = Sim(run, case, root)
        _purity(sim)
        _run_phases(sim)
        _ledger_end(sim)
        _status_end(sim)
        _merge_end(sim)
    return run


class Regression(TypedDict):
    seed: int
    step: str
    invariant: str
    detail: str
    known: bool


def _regressions() -> list[Regression]:
    rows = json.loads(_REGRESSIONS.read_bytes())
    assert isinstance(rows, list) and len(rows) <= _MAX_REGRESSIONS
    for row in rows:
        assert type(row["seed"]) is int and 0 <= row["seed"] <= _MAX_SEED
        assert isinstance(row["step"], str) and row["step"]
        assert isinstance(row["invariant"], str) and row["invariant"]
        assert isinstance(row["detail"], str) and type(row["known"]) is bool
    return rows


def _signature(failure: Violation | Regression) -> tuple[str, str, str]:
    if isinstance(failure, Violation):
        return failure.step, failure.invariant, failure.detail
    return failure["step"], failure["invariant"], failure["detail"]


def _check() -> int:
    started = time.monotonic()
    rows = _regressions()
    seeds = list(dict.fromkeys((*(row["seed"] for row in rows), *_FIXED_SEEDS)))
    known = {_signature(row) for row in rows if row["known"]}
    shown: set[tuple[str, str, str]] = set()
    checks = unexpected = expected = 0
    for seed in seeds:
        result = _run_seed(seed)
        checks += result.checks
        for failure in result.failures:
            key = _signature(failure)
            is_known = key in known
            expected += is_known
            unexpected += not is_known
            if key not in shown:
                shown.add(key)
                print(f"{'Known regression' if is_known else 'Failure'}: {failure}",
                      file=sys.stdout if is_known else sys.stderr)
    print(f"Seeds: {len(seeds)}. Invariants checked: {checks}. Unexpected violations: {unexpected}. "
          f"Known violations: {expected}. Time: {time.monotonic() - started:.2f} s.")
    return int(unexpected > 0)


def _record(rows: list[Regression], seed: int, new: list[Violation]) -> None:
    seen = {(row["seed"], _signature(row)) for row in rows}
    for failure in new:
        if (seed, _signature(failure)) not in seen:
            rows.append({"seed": seed, "step": failure.step, "invariant": failure.invariant,
                         "detail": failure.detail, "known": False})
        print(f"Failure: {failure}", file=sys.stderr)
    assert len(rows) <= _MAX_REGRESSIONS
    with TemporaryDirectory(prefix=".campaign-sim-", dir=_REGRESSIONS.parent) as directory:
        staged = Path(directory) / _REGRESSIONS.name
        staged.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        staged.replace(_REGRESSIONS)


def _hunt(seconds: int, start_seed: int) -> int:
    started = time.monotonic()
    chooser = Random(start_seed)
    rows = _regressions()
    known = {_signature(row) for row in rows if row["known"]}
    seed = start_seed
    explored = 0
    for explored in range(1, _MAX_HUNT_SEEDS + 1):
        result = _run_seed(seed)
        new = [failure for failure in result.failures if _signature(failure) not in known]
        if new:
            _record(rows, seed, new)
            print(f"Seeds explored: {explored}. New violations: {len(new)}. Start seed: {start_seed}.")
            return 1
        if time.monotonic() - started >= seconds:
            break
        seed = chooser.getrandbits(64)
    print(f"Seeds explored: {explored}. No new violations. Start seed: {start_seed}. "
          f"Time: {time.monotonic() - started:.2f} s.")
    return 0


def _replay(seed: int) -> int:
    result = _run_seed(seed)
    for failure in result.failures:
        print(f"Failure: {failure}", file=sys.stderr)
    print(f"Seed: {seed}. Case: {result.case}. Invariants checked: {result.checks}.")
    return int(bool(result.failures))


def _seed(value: str) -> int:
    seed = int(value)
    if not 0 <= seed <= _MAX_SEED:
        raise argparse.ArgumentTypeError("use an unsigned 64-bit seed")
    return seed


def _seconds(value: str) -> int:
    seconds = int(value)
    if not 1 <= seconds <= 86_400:
        raise argparse.ArgumentTypeError("use a duration between 1 and 86400 seconds")
    return seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m campaign.sim")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="replay fixed and recorded seeds")
    hunt = commands.add_parser("hunt", help="search for and record new violations")
    hunt.add_argument("--seconds", type=_seconds, required=True)
    hunt.add_argument("--start-seed", type=_seed, default=_DEFAULT_HUNT_SEED)
    replay = commands.add_parser("replay", help="replay a single seed")
    replay.add_argument("seed", type=_seed)
    args = parser.parse_args(argv)
    if args.command == "check":
        return _check()
    if args.command == "hunt":
        return _hunt(args.seconds, args.start_seed)
    assert args.command == "replay"
    return _replay(args.seed)


if __name__ == "__main__":
    raise SystemExit(main())
