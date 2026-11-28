from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from fractions import Fraction
from itertools import product
from random import Random
from types import SimpleNamespace
from typing import Any, Protocol

from muster.commands import holdout as holdout_command
from muster.lab import api, holdout, toy

_CONDITIONS = ("calm", "busy")
_COUNT = 10
_CUED = 8
_VALUES = tuple(range(20, 84, 4))
_GUESS = 48
_CHANCE = Fraction(1, len(_VALUES))
_TAIL_MAX_N = 10
_SKILLS = {"id": "skills", "type": "skills", "measure": "reward",
           "policies": {"oracle": ["sum"], "cue": [], "guess": []},
           "conditions": {"calm": [], "busy": ["sum"]}}


class Measured(Protocol):
    seed: int

    def measure(self, step: str) -> AbstractContextManager[None]: ...

    def require(self, step: str, invariant: str, holds: bool, detail: str = "") -> None: ...


def _cue(seat: str) -> api.Play:
    if seat != "p1":
        yield "GET", "/rules", None
        return
    status, data = yield "GET", "/orders", None
    assert status == 200
    own = data["orders"]
    yield "POST", "/set", {"value": sum(own) + 4 * own[0]}


def _guess(seat: str) -> api.Play:
    if seat != "p1":
        yield "GET", "/rules", None
        return
    status, data = yield "GET", "/orders", None
    assert status == 200
    yield "POST", "/set", {"value": sum(data["orders"]) + _GUESS}


def _instances(seed: str, condition: str, cued: int, broken: bool) -> list[dict]:
    assert condition in _CONDITIONS and 0 <= cued <= _COUNT
    rng = Random(f"{seed}/{condition}")
    hidden = rng.sample(_VALUES, _COUNT)
    marked = set(rng.sample(range(_COUNT), cued))
    rows = []
    for index, others in enumerate(hidden):
        firsts = [value // 4 for value in _VALUES if value != others]
        own = [others // 4 if index in marked else rng.choice(firsts),
               rng.randint(1, 20), rng.randint(1, 20)]
        split = rng.randint(1, others - 1)
        orders = ({"p1": own, "p2": [], "p3": []} if condition == "calm"
                  else {"p1": own, "p2": [split], "p3": [others - split]})
        total = sum(sum(values) for values in orders.values()) + int(broken and index == 0)
        rows.append({"id": f"i{index + 1:02d}", "condition": condition, "orders": orders,
                     "secret": f"{rng.getrandbits(64):016x}", "total": total})
    return rows


def _adapter(rows: dict[str, list[dict]]) -> SimpleNamespace:
    base = toy.family()

    def instances(condition: str) -> list[dict]:
        return deepcopy(rows[condition])

    def policies() -> dict[str, api.Policy]:
        return {"oracle": base.policies()["oracle"], "cue": _cue, "guess": _guess}

    return SimpleNamespace(**{**vars(base), "NAME": "toy-cue", "conditions": lambda: list(_CONDITIONS),
                              "instances": instances, "policies": policies,
                              "claims": lambda: [deepcopy(_SKILLS)]})


@dataclass
class ToySource:
    family: str
    seed: str
    mode: str

    def provenance(self) -> dict[str, Any]:
        return {"version": None, "source_digest": None, "muster_revision": None, "mode": self.mode}

    def rejects(self, seed: str) -> bool:
        if seed == self.seed:
            return False
        return self.mode == "exhausted" or hashlib.sha256(seed.encode()).digest()[0] < 64

    def rows(self, seed: str) -> dict[str, list[dict]]:
        cued = 0 if self.mode == "plain" else _CUED
        broken = self.mode == "broken" and seed != self.seed
        return {condition: _instances(seed, condition, cued, broken) for condition in _CONDITIONS}

    @contextmanager
    def open(self, seed: str) -> Iterator[holdout.Cohort]:
        if self.rejects(seed):
            raise holdout.Rejected(f"AssertionError: planted rejection of {seed}")
        rows = self.rows(seed)
        yield holdout.Cohort(seed, _adapter(rows), rows)


def _passes(policy: str, row: dict) -> bool:
    others = sum(row["orders"]["p2"]) + sum(row["orders"]["p3"])
    if row["condition"] != "busy":
        return False
    return 4 * row["orders"]["p1"][0] == others if policy == "cue" else others == _GUESS


def _expected(source: ToySource, seeds: list[str]) -> dict[tuple[str, str], int]:
    counts = {(policy, condition): 0 for policy in ("cue", "guess") for condition in _CONDITIONS}
    for seed in seeds:
        for condition, rows in source.rows(seed).items():
            for row in rows:
                for policy in ("cue", "guess"):
                    counts[(policy, condition)] += _passes(policy, row)
    return counts


def _seeds(sim: Measured, step: str, source: ToySource, report: dict, options: holdout.Options) -> list[str]:
    built, *held = report["seeds"]
    sim.require(step, "built-first", built["seed"] == source.seed and built["status"] == "built")
    names = [f"{source.seed}-{options.suffix}{n}"
             for n in range(options.first, options.first + len(held))]
    sim.require(step, "seed-names", [row["seed"] for row in held] == names, str(names))
    for row in held:
        rejected = source.rejects(row["seed"])
        sim.require(step, "rejected-iff-generator-rejects",
                    (row["status"] == "rejected") == rejected, row["seed"])
        sim.require(step, "rejection-reason", not rejected or "planted rejection" in row["reason"])
    accepted = [row["seed"] for row in held if row["status"] == "accepted"]
    sim.require(step, "cohort-count", len(accepted) == options.cohorts, str(accepted))
    sim.require(step, "bounded-tries", len(held) <= options.max_tries)
    return accepted


def _cells(sim: Measured, step: str, source: ToySource, report: dict, accepted: list[str]) -> None:
    built, held = _expected(source, [source.seed]), _expected(source, accepted)
    cells = {(cell["policy"], cell["condition"]): cell for cell in report["cells"]}
    sim.require(step, "cell-grid", set(cells) == set(built), str(sorted(cells)))
    for key, cell in cells.items():
        sim.require(step, "built-count", cell["built"]["passes"] == built[key]
                    and cell["built"]["total"] == _COUNT, f"{key} {cell['built']}")
        sim.require(step, "heldout-count", cell["heldout"]["passes"] == held[key]
                    and cell["heldout"]["total"] == _COUNT * len(accepted), f"{key} {cell['heldout']}")
        sim.require(step, "by-seed-sum", sum(cell["heldout_by_seed"]) == held[key])
        sim.require(step, "no-errors", cell["built"]["errors"] == cell["heldout"]["errors"] == 0)
        sim.require(step, "calm-control", (cell["decision"] == "control") == (key[1] == "calm"))
        sim.require(step, "guess-at-most-1-per-cohort",
                    key[0] != "guess" or cell["heldout"]["passes"] <= len(accepted))


def _cued(sim: Measured, rng: Random, options: holdout.Options) -> None:
    step = "holdout/cue"
    source = ToySource("toy-cue", f"toy-{rng.getrandbits(32):08x}", "cue")
    report = holdout.study(source, options)
    sim.require(step, "valid", report["valid"], str(report["reason"]))
    if not report["valid"]:
        return
    sim.require(step, "exit-replicated", holdout.exit_code(report) == 1)
    accepted = _seeds(sim, step, source, report, options)
    _cells(sim, step, source, report, accepted)
    decisions = {(cell["policy"], cell["condition"]): cell["decision"] for cell in report["cells"]}
    sim.require(step, "cue-replicated", decisions.get(("cue", "busy")) == "replicated")
    sim.require(step, "guess-at-chance", decisions.get(("guess", "busy")) == "not replicated")
    oracle = report["oracle"]
    sim.require(step, "oracle-full", oracle["heldout"]["passes"] == oracle["heldout"]["total"]
                == 2 * _COUNT * options.cohorts and oracle["built"]["passes"] == 2 * _COUNT)
    text = holdout_command.render_text(report)
    sim.require(step, "text-rows", sum(" replicated" in line for line in text.splitlines()) >= 2
                and "result: 1 of 2 applicable cells replicated" in text, text[-200:])
    if sim.seed % 4 == 0:
        sim.require(step, "deterministic", holdout.study(source, options) == report)


def _plain(sim: Measured, rng: Random, options: holdout.Options) -> None:
    step = "holdout/plain"
    source = ToySource("toy-cue", f"toy-{rng.getrandbits(32):08x}", "plain")
    report = holdout.study(source, options)
    sim.require(step, "valid", report["valid"], str(report["reason"]))
    if report["valid"]:
        accepted = _seeds(sim, step, source, report, options)
        _cells(sim, step, source, report, accepted)
        sim.require(step, "exit-none", holdout.exit_code(report) == 0)
        sim.require(step, "no-replication", report["summary"]["replicated"] == 0)


def _invalid(sim: Measured, rng: Random, options: holdout.Options) -> None:
    for mode, needle in (("broken", "oracle is below 1.0"), ("exhausted", "held-out cohorts pass")):
        step = f"holdout/{mode}"
        source = ToySource("toy-cue", f"toy-{rng.getrandbits(32):08x}", mode)
        report = holdout.study(source, options)
        sim.require(step, "invalid", not report["valid"] and holdout.exit_code(report) == 2)
        sim.require(step, "reason", needle in (report["reason"] or ""), str(report["reason"]))
        held = report["seeds"][1:]
        if mode == "exhausted":
            sim.require(step, "all-tries-rejected", len(held) == options.max_tries
                        and all(row["status"] == "rejected" for row in held))
        else:
            sim.require(step, "first-cohort-invalid", [row["status"] for row in held
                                                       if row["status"] != "rejected"] == ["invalid"])


def _brute_tail(n: int, k: int, p: Fraction) -> Fraction:
    total = Fraction(0)
    for outcome in product((0, 1), repeat=n):
        if sum(outcome) >= k:
            weight = Fraction(1)
            for bit in outcome:
                weight *= p if bit else 1 - p
            total += weight
    return total


def _tails(sim: Measured, rng: Random) -> None:
    for _ in range(4):
        n = rng.randint(0, _TAIL_MAX_N)
        k = rng.randint(0, n + 1)
        p = Fraction(rng.randint(1, 15), 16)
        sim.require("holdout/tail", "exact-binomial",
                    holdout.binomial_tail(n, k, p) == _brute_tail(n, k, p), f"n={n} k={k} p={p}")


def check(sim: Measured) -> None:
    rng = Random(sim.seed ^ 0x401D)
    cohorts = rng.randint(1, 3)
    options = holdout.Options(policies=("cue", "gu*"), cohorts=cohorts,
                              suffix=rng.choice(("h", "k", "q")), chance=_CHANCE,
                              schedule=rng.choice(("round", f"seed:{rng.getrandbits(32)}")),
                              max_tries=32 * cohorts, first=rng.randint(1, 20))
    with sim.measure("holdout"):
        _cued(sim, rng, options)
        _plain(sim, rng, options)
        _invalid(sim, rng, options)
        _tails(sim, rng)
