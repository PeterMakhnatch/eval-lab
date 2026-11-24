from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from random import Random
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from campaign import evidence, ledger
from campaign.plan import PlanError, load, parse
from campaign.runner import Runner, Step, expand_first_run
from campaign.sim_fixture import (
    ArmSpec,
    FakeJob,
    FakeTrial,
    LaneSpec,
    World,
    iso,
    plan_data,
    scenario,
    toml_text,
    usd,
)

if TYPE_CHECKING:
    from campaign.sim import Run

MUTATIONS = {
    "cap-omit-next-reserve": "cap-admission-exposure",
    "ignore-resultless-trials": "resultless-lane-respects-cap",
}
UNKNOWN_SEAT = 75_000


class AdmissionWorld(World):
    shape: str

    def _trials(self, pid: int, tasks: Path, attempts: int) -> list[FakeTrial]:
        costs = {"script": (), "overlap": (25_000,), "missing": (0, 0)}[self.shape]
        return [FakeTrial(f"task__{pid}-{attempt}", "task", "ok", 1.0 if attempt == 0 else 10.0, costs)
                for attempt in range(attempts)]

    def _finish(self, job: FakeJob, trial: FakeTrial, at: float) -> None:
        if self.shape == "overlap":
            super()._finish(job, trial, at)
        else:
            trial.end = at


class Admission:
    def __init__(self, run: Run, root: Path, shape: str, counts: tuple[int, ...], reserve: int, cap: int,
                 *, attempts: int = 1, prior: int = 0) -> None:
        lanes = tuple(LaneSpec(f"l{i}", 1, tuple(f"l{i}-arm{j}" for j in range(count)))
                      for i, count in enumerate(counts))
        arms = tuple(ArmSpec(name, lane.name, ("task",), attempts, reserve, False)
                     for lane in lanes for name in lane.arms)
        case = replace(scenario(run.seed), lanes=lanes, arms=arms, total=len(lanes), cap_micro=cap,
                       unknown_seat_micro=UNKNOWN_SEAT, preexisting_micro=prior, floor_micro=0,
                       probe_mode="steady", balance_micro=100_000_000, probe_seconds=0, key_mode="", lock_hold=None,
                       spawn_fail_arm="", crash_rate=0, top_death_rate=0, hangs=(0,), ps_fail_rate=0,
                       strays=False, churn=False, detector=False, twin=False, foreign_lines="", ancestor_mentions=False)
        self.run = run
        self.events: list[dict[str, str]] = []
        self.world = AdmissionWorld(case, root, Random(run.seed))
        self.world.shape = shape
        self.world.spawn_hook = self._spawn
        self.data = plan_data(case, root)
        path = root / "plan.toml"
        path.parent.mkdir(parents=True)
        path.write_text(toml_text(self.data))
        self.plan = load(path)
        for arm in self.plan.arms:
            arm.tasks.mkdir(parents=True)
            (arm.tasks / "task.toml").write_text('name = "task"\n')
        other = root / "other"
        other.mkdir()
        if prior:
            _json(other / "prior__1" / "result.json", {"trial_name": "prior__1", "agent_result": {"cost_usd": usd(prior)}})

    def runner(self, index: int) -> Runner:
        owner = 2000 + index
        self.world.live_owners.add(owner)
        runner = Runner(self.plan, self.world, owner, self.events.append, [self.plan.lanes[index]], expand_first_run)
        runner.open()
        return runner

    def _exposure(self) -> tuple[int, int, int]:
        own = ledger.state(self.plan.ledger, self.plan.id)
        charged = self.world.truth.charged_micro
        held, overlap = 0, 0
        for name, arm in own.items():
            if arm.open:
                reserve = round(arm.reserved_usd * 1_000_000)
                assert arm.reserves == 1
                held += reserve
                overlap += min(reserve, self.world.truth.charged_by_arm[self.plan.output_root / name])
        return charged, held, overlap

    def _spawn(self, argv: list[str], owner: int) -> None:
        charged, held, overlap = self._exposure()
        self.run.check(charged + held - overlap <= self.world.case.cap_micro, "cap-admission-exposure", argv[3])
        lock = self.plan.ledger.with_name(self.plan.ledger.name + ".lock")
        self.run.check(self.world.locks.get(lock) == owner, "cap-admission-holds-lock", argv[3])

    def observe(self) -> None:
        charged, held, overlap = self._exposure()
        scan = evidence.scan_cost([root for root in self.plan.cap_roots if root.is_dir()])
        actual = round(scan.charged(self.plan.unknown_seat_usd) * 1_000_000)
        self.run.check(actual == charged, "cap-scan-matches-trial-evidence", f"{actual} != {charged}")
        self.run.check(actual + held <= self.world.case.cap_micro + overlap, "cap-exposure-overlap-bound",
                       f"{actual} + {held}; cap {self.world.case.cap_micro}; overlap {overlap}")

    def tick(self, runner: Runner) -> bool:
        active = runner.tick()
        self.observe()
        return active

    def advance(self, seconds: float) -> None:
        self.world.advance(seconds)
        self.observe()


def _json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _equality(run: Run, root: Path, reserve: int) -> None:
    run.step = "admission/reserve-equality"
    sim = Admission(run, root, "script", (1, 1, 1), reserve, 2 * reserve)
    plan = sim.plan
    ledger.append(plan.ledger, {"experiment": "other", "event": "reserve", "arm": "foreign-arm",
                                "reserved_usd": usd(reserve), "ts_utc": iso(0)})
    a, b, c = (sim.runner(i) for i in range(3))
    sim.tick(a)

    def overlap(owner: int) -> None:
        if owner == b.owner:
            sim.tick(c)

    sim.world.probe_hook = overlap
    sim.tick(b)
    sim.world.probe_hook = None
    run.check(sim._exposure() == (0, 2 * reserve, 0) and len(sim.world.jobs) == 2,
              "cap-reserve-only-equality-admits", "2 starts reach the cap; foreign reserve applies to balance only")
    run.check(any(event["reason"] == "money-lock" for event in sim.events), "cap-lanes-contend-for-lock", "third lane")
    _refusal(run, sim, c)
    sim.advance(2)
    run.check(not sim.tick(a) and not sim.tick(b) and a.exit_code() == b.exit_code() == 0,
              "cap-equality-completes", "both admitted scripted arms complete")
    scan = evidence.scan_cost([plan.output_root])
    run.check(scan.trials == 2 and scan.seats_without_cost == 0 and len(scan.problems) == 2,
              "resultless-scripted-free", "2 scripted trial configs without results")


def _refusal(run: Run, sim: Admission, runner: Runner) -> None:
    before_ledger = sim.plan.ledger.read_bytes() if sim.plan.ledger.exists() else b""
    before_jobs = len(sim.world.jobs)
    active = sim.tick(runner)
    stop = sim.events[-1]
    amounts = ("recorded charged cost $", "own open reserves $", "next reserve $")
    run.check(not active and runner.exit_code() == 1 and stop["reason"] == "cap"
              and all(amount in stop["text"] for amount in amounts), "cap-refusal-reports-amounts", stop["text"])
    after_ledger = sim.plan.ledger.read_bytes() if sim.plan.ledger.exists() else b""
    run.check(len(sim.world.jobs) == before_jobs and before_ledger == after_ledger,
              "cap-refusal-has-no-side-effects", stop["before"])


def _overlap(run: Run, root: Path, reserve: int) -> None:
    run.step = "admission/open-arm-with-evidence"
    sim = Admission(run, root, "overlap", (1, 1), reserve, 2 * reserve, attempts=2)
    a, b = (sim.runner(i) for i in range(2))
    sim.tick(a)
    sim.advance(2)
    sim.tick(b)
    charged, held, overlap = sim._exposure()
    run.check(charged > 0 and charged + held > 2 * reserve and charged + held - overlap == 2 * reserve,
              "cap-credits-cost-of-open-arm", f"charged {charged}; open {held}; overlap {overlap}")
    sim.advance(12)
    run.check(not sim.tick(a) and not sim.tick(b) and a.exit_code() == b.exit_code() == 0,
              "cap-fitting-plan-completes", "both arms finish, including the arm admitted during partial results")


def _missing(run: Run, root: Path) -> None:
    run.step = "admission/resultless-lane"
    reserve = 2 * UNKNOWN_SEAT
    sim = Admission(run, root, "missing", (4,), reserve, 2 * reserve + UNKNOWN_SEAT)
    plan = sim.plan
    folder = plan.output_root / "job__not-a-trial"
    _json(folder / "config.json", {"job_name": folder.name, "tasks": [],
                                    "participants": [{"model_name": "sim/model"}]})
    runner = sim.runner(0)
    for _ in range(5):
        if not sim.tick(runner):
            break
        sim.advance(2)
    scan = evidence.scan_cost([plan.output_root])
    started = len(sim.world.jobs)
    run.check(started == 2 and runner.exit_code() == 1 and sim.events[-1]["reason"] == "cap",
              "resultless-lane-respects-cap", f"{started} arms started; cap admits 2")
    run.check(scan.trials == 2 and scan.unknown_trials == 2 and scan.seats_without_cost == 4
              and len(scan.problems) == 2 and all(p.endswith(": no result file") for p in scan.problems),
              "resultless-config-counts-models-not-jobs", repr(scan))


def _largest_reserve(run: Run, sim: Admission, reserve: int) -> None:
    run.step = "admission/plan-cap"
    for field in ("reserve_usd", "rerun_reserve_usd"):
        arm = sim.data["arms"][0]
        sim.data["arms"][0] = arm | {field: usd(reserve + 1)}
        try:
            parse(toml_text(sim.data).encode(), sim.plan.path)
        except PlanError as error:
            run.check("exceeds money.cap_usd" in str(error), "plan-refuses-oversized-reserve", str(error))
        else:
            run.check(False, "plan-refuses-oversized-reserve", field)
        sim.data["arms"][0] = arm


def _near_cap(run: Run, root: Path, reserve: int) -> None:
    run.step = "admission/near-cap"
    sim = Admission(run, root, "script", (1,), reserve, reserve, prior=reserve - 10_000)
    _refusal(run, sim, sim.runner(0))
    _largest_reserve(run, sim, reserve)


def _config_shapes(run: Run, root: Path) -> None:
    run.step = "admission/resultless-config-shapes"
    scripts = [{"name": "oracle"}, {"name": "nop"}, {"name": "script"}, {"import_path": "agents:ScriptAgent"}]
    modeled = {"name": "terminus-2", "model_name": "sim/model"}
    shapes = (
        ("scripted", {"participants": scripts}, 0, False),
        ("mapped", {"participants": {"a": modeled, "b": modeled, "c": scripts[0]}}, 2, False),
        ("solo", {"agent": modeled}, 1, False),
        ("no-agent-info", {}, 3, False),
        ("invalid-result", {"participants": [modeled]}, 3, True),
    )
    for label, config, seats, unreadable in shapes:
        folder = root / f"trial__{label}"
        _json(folder / "config.json", {"trial_name": folder.name, "task": {"path": "task"}, **config})
        result = folder / "result.json"
        if unreadable:
            result.write_text("{")
        cost = evidence.scan_cost([folder], expected_seats=3)
        run.check(cost.seats_without_cost == seats and cost.unreadable_results == int(unreadable)
                  and len(cost.problems) == int(not unreadable), "resultless-config-shape", f"{label}: {cost}")


def replay(run: Run, seed: int, root: Path, mutation: str | None = None) -> None:
    reserve = Random(seed).randint(1, 3) * 1_000_000
    if mutation == "cap-omit-next-reserve":
        original = Runner._gate_commitment

        def drop(runner: Runner, step: Step) -> None:
            original(runner, replace(step, reserve_usd=0))

        with patch.object(Runner, "_gate_commitment", drop):
            _equality(run, root / "equal", reserve)
        return
    if mutation == "ignore-resultless-trials":
        with patch.object(evidence, "_missing_result", return_value=False):
            _missing(run, root / "missing")
        return
    assert mutation is None
    _equality(run, root / "equal", reserve)
    _near_cap(run, root / "near", reserve)
    _overlap(run, root / "overlap", reserve)
    _missing(run, root / "missing")
    _config_shapes(run, root / "shapes")
