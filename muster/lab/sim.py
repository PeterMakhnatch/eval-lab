from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from random import Random
from statistics import fmean
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import TypedDict

from muster import render
from muster.commands import card as card_command
from muster.lab import api, card, cardfile, contrast, episode, finding, search, sim_tools, toy
from muster.lab.schedule import Bursty, RoundRobin, Scripted, Seeded, parse
from muster.lab.sim_holdout import check as holdout_check
from muster.sim_fixture import Run, Violation, write_json

_REGRESSIONS = Path(__file__).with_name("sim_regressions.json")
_FIXED_SEEDS = range(24)
_MAX_REGRESSIONS = 512
_MAX_SEED = 2**64 - 1
_MAX_HUNT_SEEDS = 1_000_000
_DEFAULT_SEED = 261125
_SEARCH_TRIES = 40
_SPIN_LIMIT = 2000
_CARD_SEEDS = (0, 17)
_CARD_MUTANTS = {
    "grader_at_least": "overshoot_zero",
    "leak_secret": "builtin.no_hidden_string",
    "oracle_gives_up": "builtin.oracle_ceiling",
    "nondeterministic": "builtin.determinism",
    "crash": "builtin.no_world_error",
    "budget_unequal": "equal_budget",
    "oracle_spins": "builtin.policies_end",
    "replay_drift": "builtin.state_matches_log",
    "trap_no_bite": "counter_ladder",
    "credits_refused": "builtin.refused_events_do_not_matter",
}
_FINDINGS = (
    ("wrong_pass", "grader_at_least", "zoo:overshoot", ">=", 1.0),
    ("oracle_fails", "oracle_gives_up", "zoo:oracle", "<", 1.0),
    ("hidden_string", "leak_secret", "file:policy.py:read_rules", "==", 0.0),
    ("world_error", "crash", "zoo:fuzz", "==", 0.0),
    ("nondeterminism", "nondeterministic", "zoo:greedy_reader", "==", 0.0),
    ("no_end", "oracle_spins", "zoo:oracle", "==", 0.0),
    ("state_mismatch", "replay_drift", "zoo:greedy_reader", "==", 0.0),
)


@dataclass
class Simulation(Run):
    episodes: int = 0
    groups: dict[str, tuple[float, int]] = field(default_factory=dict)

    @contextmanager
    def measure(self, step: str) -> Iterator[None]:
        started, checks = time.monotonic(), self.checks
        with self.section(step):
            yield
        group = step.split("/", 1)[0]
        elapsed, total = self.groups.get(group, (0.0, 0))
        self.groups[group] = (elapsed + time.monotonic() - started,
                              total + self.checks - checks)

    def count(self, adapter: SimpleNamespace) -> SimpleNamespace:
        make_world = adapter.make_world

        def counted(instance, events, clock):
            self.episodes += 1
            return make_world(instance, events, clock)

        return SimpleNamespace(**{**vars(adapter), "make_world": counted})


def _schedule(rng: Random) -> str:
    seed = rng.getrandbits(64)
    return rng.choice(("round", f"seed:{seed}", f"bursty:{seed}:{rng.randint(1, 12)}",
                       "order:" + ",".join(rng.choices(("p1", "p2", "p3"), k=24))))


def _uniform(adapter: SimpleNamespace, instance: dict, policy: str) -> dict[str, str]:
    return dict.fromkeys(adapter.seats(instance), policy)


def _accounting(sim: Simulation, result: api.Episode, instance: dict) -> None:
    events = result.events
    seat_events = [event for event in events if event["seat"] is not None]
    sim.require("episode", "step-count", sum(result.steps.values()) == len(result.order))
    sim.require("episode", "seat-count", len(seat_events) == len(result.order))
    sim.require("episode", "event-order",
                tuple(event["seat"] for event in seat_events) == result.order)
    sim.require("episode", "seat-steps",
                all(result.steps[seat] == result.order.count(seat) for seat in result.steps))
    sim.require("episode", "sequence", all(event["seq"] == i for i, event in enumerate(events, 1)))
    sim.require("episode", "clock",
                all(event["t"] == 1000.0 + i for i, event in enumerate(events, 1)))
    sim.require("episode", "close",
                bool(events) and events[-1]["kind"] == "admin.close" and events[-1]["ok"])
    sim.require("episode", "replay-state", result.state == result.replayed)
    sim.require("episode", "no-hidden", not _leaks(result, instance))
    sim.require("episode", "no-500", all(event["status"] != 500 for event in events))


def _leaks(result: api.Episode, instance: dict) -> bool:
    return any(
        instance["secret"] in json.dumps(event["response"])
        for event in result.events if event["seat"] is not None
    )


def _correct(sim: Simulation, rng: Random) -> None:
    adapter = sim.count(toy.family())
    condition = rng.choice(adapter.conditions())
    instance = rng.choice(adapter.instances(condition))
    sim.case = f"counter/{condition}/{instance['id']}"
    lineup = {seat: rng.choice(sorted(adapter.policies())) for seat in adapter.seats(instance)}
    text = _schedule(rng)
    result = episode.run(adapter, instance, lineup, parse(text))
    repeated = episode.run(adapter, instance, lineup, parse(text))
    sim.require("episode", "determinism", result == repeated, f"{text} {lineup}")
    replay = episode.run(adapter, instance, lineup, Scripted(result.order))
    sim.require("episode", "schedule-replay", result == replay, f"{text} {lineup}")
    _accounting(sim, result, instance)
    oracle = _uniform(adapter, instance, "oracle")
    perfect = episode.run(adapter, instance, oracle, parse(text))
    sim.require("oracle", "reward", perfect.rewards["reward"] == 1.0)
    sim.require("oracle", "finished", perfect.ended == "all_done")
    _accounting(sim, perfect, instance)
    naive = _uniform(adapter, instance, "naive")
    if condition == "calm":
        for schedule in (parse(text), RoundRobin(), Seeded(sim.seed), Bursty(sim.seed, 9)):
            outcome = episode.run(adapter, instance, naive, schedule)
            sim.require("calm", "naive",
                        outcome.rewards["reward"] == 1.0 and outcome.ended == "all_done")
    else:
        trap = search.worst(adapter, instance, naive, tries=_SEARCH_TRIES, seed=sim.seed)
        best_naive = search.best(adapter, instance, naive, tries=_SEARCH_TRIES, seed=0)
        worst_oracle = search.worst(adapter, instance, oracle, tries=_SEARCH_TRIES, seed=sim.seed)
        sim.require("busy", "naive-loses", trap.rewards["reward"] == 0.0)
        sim.require("busy", "naive-can-win", best_naive.rewards["reward"] == 1.0)
        sim.require("busy", "oracle-survives",
                    worst_oracle.rewards["reward"] == 1.0 and worst_oracle.ended == "all_done")
        replayed = episode.run(adapter, instance, naive, Scripted(trap.order))
        sim.require("busy", "worst-replay", replayed == trap)


def _mutant(sim: Simulation, mutant: str, rng: Random) -> None:
    adapter = sim.count(toy.family(mutant))
    instance = rng.choice(adapter.instances("busy"))
    oracle = _uniform(adapter, instance, "oracle")
    readers = _uniform(adapter, instance, "greedy_reader")
    match mutant:
        case "oracle_gives_up":
            result = search.worst(adapter, instance, oracle, tries=4, seed=sim.seed)
            detected = result.rewards["reward"] < 1.0
        case "oracle_spins":
            result = episode.run(adapter, instance, oracle, RoundRobin(), max_steps=_SPIN_LIMIT)
            detected = result.ended == "step_limit" and len(result.order) == _SPIN_LIMIT
        case "nondeterministic":
            result = episode.run(adapter, instance, readers, RoundRobin())
            detected = result != episode.run(adapter, instance, readers, RoundRobin())
        case "replay_drift":
            result = episode.run(adapter, instance, readers, RoundRobin())
            detected = result.state != result.replayed
        case "crash":
            fuzz = _uniform(adapter, instance, "fuzz")
            result = episode.run(adapter, instance, fuzz, RoundRobin())
            detected = any(event["status"] == 500 for event in result.events)
        case "leak_secret":
            result = episode.run(adapter, instance, readers, RoundRobin())
            detected = _leaks(result, instance)
        case _:
            result = episode.run(adapter, instance, oracle, RoundRobin())
            detected = result.ended == "all_done"
    sim.require("mutants", mutant, detected)


def _card_build(sim: Simulation, mutant: str | None = None) -> dict:
    return card.build(sim.count(toy.family(mutant)), schedules=3, worst=4, instances=1,
                      seed=sim.seed)


def _card_checks(sim: Simulation) -> None:
    correct = _card_build(sim)
    repeated = _card_build(sim)
    sim.require("card", "correct", correct["ok"])
    sim.require("card", "canonical", cardfile.canonical(correct) == cardfile.canonical(repeated))
    rows = correct["claims"] + correct["builtins"]
    for row in rows:
        expected = "NOT_CHECKED" if row["id"] == "builtin.findings_regress" else "PASS"
        sim.require("card", row["id"], row["status"] == expected)
    sim.require("card", "mutant-coverage", set(_CARD_MUTANTS) == set(toy.MUTANTS))
    for mutant, expected in _CARD_MUTANTS.items():
        result = _card_build(sim, mutant)
        rejected = {row["id"]: row for row in result["claims"] + result["builtins"]
                    if row["status"] == "FAIL"}
        sim.require("card", mutant, not result["ok"] and expected in rejected,
                    f"expected {expected}; failed {sorted(rejected)}")
        sim.require("card", f"{mutant}/witness",
                    expected in rejected and bool(rejected[expected].get("witness")))


def _finding_checks(sim: Simulation, directory: Path) -> None:
    (directory / "policy.py").write_text(
        'def read_rules(seat):\n    yield "GET", "/rules", None\n'
    )
    path = directory / "finding.json"
    for kind, mutant, name, op, value in _FINDINGS:
        row = {
            "id": f"counter-{kind}", "family": f"toy:{mutant}", "condition": "busy",
            "instance": "i01", "kind": kind, "lineup": dict.fromkeys(("p1", "p2", "p3"), name),
            "schedule": "round", "expect": {"measure": "reward", "op": op, "value": value},
            "claim": _CARD_MUTANTS[mutant], "note": "seeded counter defect", "status": "open",
        }
        path.write_text(json.dumps(row))
        reproduced = finding.verify(path)
        sim.require("finding", f"{kind}/confirmed", reproduced["verdict"] == "CONFIRMED")
        row["family"] = "toy"
        path.write_text(json.dumps(row))
        corrected = finding.verify(path)
        sim.require("finding", f"{kind}/corrected", corrected["verdict"] == "NOT_REPRODUCED")
        sim.episodes += 4 if kind == "nondeterminism" else 2
        if kind in ("hidden_string", "world_error"):
            sim.require("finding", f"{kind}/evidence", reproduced["event"] is not None)


def _card_file_checks(sim: Simulation, directory: Path) -> None:
    parser = argparse.ArgumentParser()
    card_command.configure(parser)
    path = directory / "card.json"
    common = ["toy", "--schedules", "3", "--worst", "4", "--instances", "1",
              "--seed", str(sim.seed), "--json"]

    def command(flag: str) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return card_command.run(parser.parse_args([*common, flag]), card_path=path)

    sim.require("card-file", "write", command("--write") == 0)
    saved = path.read_bytes()
    sim.require("card-file", "check", command("--check") == 0)
    sim.require("card-file", "unchanged", path.read_bytes() == saved)
    assert saved.endswith(b"\n")
    path.write_bytes(saved[:-1] + b" ")
    sim.require("card-file", "byte-change", command("--check") == 1)


def _card_suite(sim: Simulation) -> None:
    _card_checks(sim)
    with TemporaryDirectory(prefix="muster-card-sim-") as folder:
        directory = Path(folder)
        _finding_checks(sim, directory)
        _card_file_checks(sim, directory)



def _contrast_trial(
    root: Path, name: str, task: Path, score: float, *, fault: str | None = None,
) -> None:
    trial = root / "job" / name
    (trial / "verifier").mkdir(parents=True)
    rewards = {"reward": score, "focal": score, "clean": float(score == 1)}
    participant = {"seat": "p1", "name": "simulation", "model_name": "route"}
    seat = {"seat": "p1", "under_test": True, "agent_info": {"name": "simulation"}}
    config = {"task": {"path": str(task)}, "trial_name": name, "participants": [participant]}
    result = {
        "schema_version": 1, "task_name": task.name, "trial_name": name,
        "config": config, "seat_results": [seat], "verifier_result": {"rewards": rewards},
        "reward_valid": fault != "invalid",
    }
    if fault in ("AgentTimeoutError", "AgentError"):
        result["exception_info"] = {"exception_type": fault}
    write_json(trial / "config.json", config)
    write_json(trial / "result.json", result)
    if fault != "missing":
        write_json(trial / "verifier/reward.json", rewards)


def contrast_tree(root: Path, seed: int) -> float:
    rng = Random(seed)
    jobs, tasks = root / "jobs", root / "tasks"
    scores = []
    for index in range(1, 11):
        instance = f"i{index:02}"
        score = rng.randrange(0, 5) / 8
        scores.append(score)
        for arm, value in (("probe", score), ("guard", score + 0.25)):
            task = tasks / "build" / f"storm-{arm}" / f"depot-{instance}"
            if index == 1 and arm == "guard":
                task = tasks / "archived"
                task.mkdir(parents=True)
                (task / "task.toml").write_text(
                    '[metadata]\nfamily="depot"\nfacet="storm"\narm="guard"\ninstance="i01"\n'
                )
            _contrast_trial(jobs, f"{arm}-{instance}", task, value)
            if index % 2 == 0:
                _contrast_trial(jobs, f"{arm}-{instance}-repeat", task, value)
        if index <= 4:
            task = tasks / "build/storm-reader" / f"depot-{instance}"
            _contrast_trial(jobs, f"reader-{instance}", task, score)
    probe = tasks / "build/storm-probe/depot-i01"
    solo = tasks / "build/storm-solo/depot-i01"
    for fault in ("missing", "invalid", "AgentError", "AgentTimeoutError"):
        _contrast_trial(jobs, f"probe-{fault}", probe, 1.0, fault=fault)
        _contrast_trial(jobs, f"solo-{fault}", solo, 1.0, fault=fault)
    _contrast_trial(jobs, "unknown-task", tasks / "unknown", 1.0)
    return fmean(scores)


def _sign_checks(sim: Simulation) -> None:
    for n in range(11):
        counts = [0] * (n + 1)
        for outcomes in product((0, 1), repeat=n):
            counts[sum(outcomes)] += 1
        for positives in range(n + 1):
            tail = min(positives, n - positives)
            expected = sum(count for i, count in enumerate(counts)
                           if min(i, n - i) <= tail) / 2**n
            values = [1.0] * positives + [-1.0] * (n - positives) + [0.0]
            sim.require("contrast", f"exact-sign-{n}-{positives}",
                        contrast.sign_test(values) == expected)


def _contrast_checks(sim: Simulation, root: Path) -> None:
    score = contrast_tree(root, sim.seed)
    jobs = [root / "jobs"]
    result = contrast.report(jobs, seed=sim.seed, measures=["clean"])
    group = result["groups"][0]
    facet = group["facets"]["storm"]
    cells = facet["cells"]["reward"]
    comparisons = facet["contrasts"]["reward"]
    sim.require("contrast", "probe-mean", cells["probe"]["mean"] == score)
    sim.require("contrast", "guard-mean", cells["guard"]["mean"] == score + 0.25)
    sim.require("contrast", "instances-not-attempts", cells["probe"]["instances"] == 10)
    sim.require("contrast", "loss-accounting",
                cells["probe"]["valid_trials"] == 15 and cells["probe"]["lost_trials"] == 4)
    sim.require("contrast", "only-loss",
                cells["solo"]["mean"] == "unknown" and cells["solo"]["lost_trials"] == 4)
    sim.require("contrast", "unplaced", len(result["unplaced"]) == 1)
    sim.require("contrast", "constant-paired",
                comparisons["guard"]["interval"] == [0.25, 0.25]
                and comparisons["guard"]["difference"] == 0.25
                and comparisons["guard"]["pairs"] == 10)
    reverse = contrast.report(jobs, seed=sim.seed, base="guard")
    swapped = reverse["groups"][0]["facets"]["storm"]["contrasts"]["reward"]["probe"]
    sim.require("contrast", "reverse", swapped["interval"] == [-0.25, -0.25]
                and swapped["difference"] == -comparisons["guard"]["difference"])
    partial = comparisons["reader"]
    sim.require("contrast", "few-pairs", partial["pairs"] == 4
                and partial["interval"] == partial["sign_p"] == "unknown")
    repeated = contrast.report(jobs * 2, seed=sim.seed, measures=["clean"])
    sim.require("contrast", "canonical-repeat", render.to_json(result) == render.to_json(repeated))
    sim.require("contrast", "measures", result["measures"] == ["reward", "clean", "focal"])
    varying = [Random(sim.seed + i).randrange(-10, 11) / 8 for i in range(10)]
    forward = contrast.paired(varying, seed=sim.seed)
    backward = contrast.paired([-value for value in varying], seed=sim.seed)
    sim.require("contrast", "varying-reverse",
                forward["interval"] == [-bound for bound in reversed(backward["interval"])])
    sim.require("contrast", "varying-repeat", forward == contrast.paired(varying, seed=sim.seed))
    _sign_checks(sim)


def _contrast_decimal_checks(sim: Simulation) -> None:
    attempts = []
    parity = sim.seed % 2
    for index in range(1, 11):
        instance = f"i{index:02}"
        for facet, arm, value, count in (
            ("ties", "probe", 0.1, 1), ("ties", "guard", 0.1, 3),
            ("constant", "probe", 0.7 if index % 2 == parity else 0.1, 1),
            ("constant", "guard", 0.9 if index % 2 == parity else 0.3, 1),
        ):
            cell = contrast.Cell("depot", facet, arm, instance)
            attempts.extend(contrast.Attempt(cell, "model", {"reward": value}, None)
                            for _ in range(count))
    facets = contrast.analyze(attempts, seed=sim.seed)["groups"][0]["facets"]
    tied = facets["ties"]["contrasts"]["reward"]["guard"]
    constant = facets["constant"]["contrasts"]["reward"]["guard"]
    sim.require("contrast", "decimal-ties", tied["interval"] == [0.0, 0.0]
                and tied["difference"] == 0.0 and tied["sign_p"] == 1.0)
    sim.require("contrast", "decimal-constant",
                constant["interval"] == [0.2, 0.2] and constant["difference"] == 0.2)


def _contrast_suite(sim: Simulation) -> None:
    with TemporaryDirectory(prefix="muster-contrast-sim-") as folder:
        _contrast_checks(sim, Path(folder))
    _contrast_decimal_checks(sim)


def _run_seed(seed: int) -> Simulation:
    assert 0 <= seed <= _MAX_SEED
    simulation, rng = Simulation(seed), Random(seed)
    with simulation.measure("correct"):
        _correct(simulation, rng)
    for mutant in toy.MUTANTS:
        with simulation.measure(f"mutant/{mutant}"):
            _mutant(simulation, mutant, rng)
    if seed in _CARD_SEEDS:
        with simulation.measure("card"):
            _card_suite(simulation)
    with simulation.measure("contrast"):
        _contrast_suite(simulation)
    if seed in sim_tools.VERIFY_SEEDS:
        with simulation.measure("card-verify"):
            sim_tools.card_verify(simulation)
    if seed == sim_tools.JOBS_SEED:
        with simulation.measure("card-jobs"):
            sim_tools.card_jobs(simulation)
    if seed in sim_tools.POLICY_SEEDS:
        with simulation.measure("file-policy"):
            sim_tools.file_policies(simulation)
    if seed == sim_tools.PARITY_SEED:
        with simulation.measure("parity"):
            sim_tools.parity_checks(simulation)
    holdout_check(simulation)
    return simulation


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


def _signature(failure: Regression | Violation) -> tuple[int, str, str, str]:
    if isinstance(failure, Violation):
        return failure.seed, failure.step, failure.invariant, failure.detail
    return failure["seed"], failure["step"], failure["invariant"], failure["detail"]


def _check() -> int:
    started = time.monotonic()
    rows = _regressions()
    seeds = list(dict.fromkeys([*(row["seed"] for row in rows), *_FIXED_SEEDS]))
    known = {_signature(row) for row in rows if row["known"]}
    episodes = checks = unexpected = expected = 0
    groups: dict[str, tuple[float, int]] = {}
    for seed in seeds:
        sim = _run_seed(seed)
        episodes += sim.episodes
        checks += sim.checks
        for name, (elapsed, count) in sim.groups.items():
            total_time, total_checks = groups.get(name, (0.0, 0))
            groups[name] = total_time + elapsed, total_checks + count
        for failure in sim.failures:
            is_known = _signature(failure) in known
            expected += is_known
            unexpected += not is_known
            print(f"{'Known regression' if is_known else 'Failure'}: {failure}", file=sys.stderr)
    assert sum(count for _, count in groups.values()) == checks
    for name, (elapsed, count) in sorted(groups.items()):
        print(f"{name}: {count} invariants, {elapsed:.2f} s.")
    print(f"Seeds: {len(seeds)}. Episodes: {episodes}. Invariants checked: {checks}. "
          f"Unexpected violations: {unexpected}. Known violations: {expected}. "
          f"Time: {time.monotonic() - started:.2f} s.")
    return int(unexpected > 0)


def _hunt(seconds: int, start_seed: int) -> int:
    started = time.monotonic()
    rows = _regressions()
    known = {_signature(row) for row in rows if row["known"]}
    seen = {_signature(row) for row in rows}
    chooser, seed = Random(start_seed), start_seed
    episodes = checks = explored = 0
    for explored in range(1, _MAX_HUNT_SEEDS + 1):
        sim = _run_seed(seed)
        episodes += sim.episodes
        checks += sim.checks
        failures = [failure for failure in sim.failures if _signature(failure) not in known]
        if failures:
            for failure in failures:
                if _signature(failure) not in seen:
                    rows.append({"seed": seed, "step": failure.step, "invariant": failure.invariant,
                                 "detail": failure.detail, "known": False})
                print(f"Failure: {failure}", file=sys.stderr)
            assert len(rows) <= _MAX_REGRESSIONS
            _REGRESSIONS.write_text(json.dumps(rows, indent=2) + "\n")
            print(f"Seeds: {explored}. Episodes: {episodes}. Invariants checked: {checks}. "
                  f"New violations: {len(failures)}.")
            return 1
        if time.monotonic() - started >= seconds:
            break
        seed = chooser.getrandbits(64)
    print(f"Seeds: {explored}. Episodes: {episodes}. Invariants checked: {checks}. "
          "No new violations. "
          f"Start seed: {start_seed}. Time: {time.monotonic() - started:.2f} s.")
    return 0


def _replay(seed: int) -> int:
    sim = _run_seed(seed)
    for failure in sim.failures:
        print(f"Failure: {failure}", file=sys.stderr)
    print(f"Seed: {seed}. Task: {sim.case}. Episodes: {sim.episodes}. "
          f"Invariants checked: {sim.checks}.")
    return int(bool(sim.failures))


def _seed(text: str) -> int:
    value = int(text)
    if not 0 <= value <= _MAX_SEED:
        raise argparse.ArgumentTypeError("use an unsigned 64-bit seed")
    return value


def _seconds(text: str) -> int:
    value = int(text)
    if not 1 <= value <= 86_400:
        raise argparse.ArgumentTypeError("use a duration between 1 and 86400 seconds")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m muster.lab.sim")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    hunt = commands.add_parser("hunt")
    hunt.add_argument("--seconds", type=_seconds, required=True)
    hunt.add_argument("--seed", type=_seed, default=_DEFAULT_SEED)
    replay = commands.add_parser("replay")
    replay.add_argument("--seed", type=_seed, required=True)
    args = parser.parse_args(argv)
    if args.command == "check":
        return _check()
    if args.command == "hunt":
        return _hunt(args.seconds, args.seed)
    assert args.command == "replay"
    return _replay(args.seed)


if __name__ == "__main__":
    raise SystemExit(main())
