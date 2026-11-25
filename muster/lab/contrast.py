from __future__ import annotations

import re
import tomllib
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from math import comb
from operator import itemgetter
from pathlib import Path
from random import Random
from statistics import fmean
from typing import Any

from muster import load, render
from muster.model import InputError, Problem, SeatTrial

RESAMPLES = 10_000
MIN_PAIRS = 5
MAX_TRIALS = 1_000_000
UNKNOWN = "unknown"
LIMITS = (
    "Differences are between arms within one model, not a ranking of models; "
    "attempts of one instance are not independent samples."
)
NAME = re.compile(r"[a-z][a-z0-9_]*")
INSTANCE = re.compile(r"i[0-9]{2}")


@dataclass(frozen=True, order=True)
class Cell:
    family: str
    facet: str
    arm: str
    instance: str

    @classmethod
    def from_parts(cls, family: Any, condition: Any, instance: Any) -> Cell | None:
        if not all(isinstance(part, str) for part in (family, condition, instance)):
            return None
        facet, sep, arm = condition.rpartition("-")
        if not sep or not all(NAME.fullmatch(part) for part in (family, facet, arm)):
            return None
        if not INSTANCE.fullmatch(instance):
            return None
        return cls(family, facet, arm, instance)


def place(trial: SeatTrial) -> Cell | None:
    if trial.task_path is None:
        return None
    path = Path(trial.task_path)
    metadata_path = path / "task.toml"
    for candidate in (metadata_path, trial.path / "task/task.toml"):
        if not candidate.is_file():
            continue
        try:
            metadata = tomllib.loads(candidate.read_text(encoding="utf-8")).get("metadata")
        except (OSError, ValueError):
            return None
        if not isinstance(metadata, dict):
            return None
        condition = metadata.get("condition")
        facet, arm = metadata.get("facet"), metadata.get("arm")
        if isinstance(facet, str) and isinstance(arm, str):
            derived = f"{facet}-{arm}"
            if condition is not None and condition != derived:
                return None
            condition = derived
        cell = Cell.from_parts(metadata.get("family"), condition, metadata.get("instance"))
        return cell
    if path.parent.parent.name != "build":
        return None
    family, sep, instance = path.name.rpartition("-")
    if not sep:
        return None
    return Cell.from_parts(family, path.parent.name, instance)


def roster(trial: SeatTrial) -> str:
    labels = {seat.label for seat in trial.seats}
    assert labels
    return next(iter(labels)) if len(labels) == 1 else trial.roster_key()


def loss(trial: SeatTrial) -> str | None:
    exceptions = {trial.exception, *(seat.exception for seat in trial.seats)} - {None}
    if exceptions:
        timeout = any("timeout" in str(value).lower() for value in exceptions)
        return "timeout" if timeout else "exception"
    reward_file = trial.path / "verifier/reward.json"
    if not trial.reward_valid or not trial.rewards or not reward_file.is_file():
        return "no_valid_reward"
    return None


def sign_test(differences: Sequence[float]) -> float:
    positive = sum(value > 0 for value in differences)
    negative = sum(value < 0 for value in differences)
    n = positive + negative
    return min(1.0, 2 * sum(comb(n, count) for count in range(min(positive, negative) + 1)) / 2**n)


@lru_cache(maxsize=16)
def _resamplers(n: int, seed: int) -> tuple[Callable, ...]:
    assert n >= MIN_PAIRS
    rng = Random(seed)
    return tuple(itemgetter(*rng.choices(range(n), k=n)) for _ in range(RESAMPLES))


def paired(differences: Sequence[float], *, seed: int) -> dict[str, Any]:
    n = len(differences)
    constant = n > 0 and min(differences) == max(differences)
    mean = differences[0] if constant else fmean(differences) if n else UNKNOWN
    result = {"difference": mean, "pairs": n, "interval": UNKNOWN, "sign_p": UNKNOWN}
    if n < MIN_PAIRS:
        return result
    if constant:
        interval = [differences[0], differences[0]]
    else:
        means = sorted(fmean(draw(differences)) for draw in _resamplers(n, seed))
        interval = [means[RESAMPLES // 40 - 1], means[RESAMPLES - RESAMPLES // 40]]
    return {**result, "interval": interval, "sign_p": sign_test(differences)}


@dataclass(frozen=True)
class Attempt:
    cell: Cell
    roster: str
    rewards: dict[str, float]
    lost: str | None


def collect(jobs: Sequence[Path]) -> tuple[list[Attempt], list[dict[str, str]]]:
    attempts: list[Attempt] = []
    unplaced: list[dict[str, str]] = []
    seen: set[Path] = set()
    for root in jobs:
        if not root.is_dir():
            raise InputError(f"{root}: not a directory")
        for item in load.walk(root):
            path = item.path.resolve()
            if path in seen:
                continue
            seen.add(path)
            if len(seen) > MAX_TRIALS:
                raise InputError(f"more than {MAX_TRIALS} trials")
            if isinstance(item, Problem):
                unplaced.append({"path": str(path), "reason": item.reason})
                continue
            cell = place(item)
            if cell is None:
                unplaced.append({"path": str(path), "reason": "unknown task cell"})
                continue
            attempts.append(Attempt(cell, roster(item), item.rewards, loss(item)))
    return attempts, sorted(unplaced, key=lambda row: row["path"])


def _cell(attempts: Sequence[Attempt], measure: str) -> tuple[dict[str, Any], dict[str, Fraction]]:
    values: dict[str, list[Fraction]] = defaultdict(list)
    for attempt in attempts:
        if attempt.lost is None and measure in attempt.rewards:
            values[attempt.cell.instance].append(Fraction(str(attempt.rewards[measure])))
    means = {key: sum(values[key], start=Fraction(0)) / len(values[key]) for key in sorted(values)}
    valid = sum(attempt.lost is None for attempt in attempts)
    observed = sum(map(len, values.values()))
    lost = len(attempts) - valid
    assert 0 <= observed <= valid <= len(attempts)
    return {
        "mean": float(sum(means.values(), start=Fraction(0)) / len(means)) if means else UNKNOWN,
        "instances": len(means), "valid_trials": valid, "measure_trials": observed,
        "lost_trials": lost, "trials": len(attempts),
    }, means


def _facet(
    cells: dict[tuple[str, str], list[Attempt]], facet: str, arms: Sequence[str],
    measures: Sequence[str], *, base: str, seed: int,
) -> dict[str, Any]:
    measured = {}
    contrasts = {}
    for measure in measures:
        rows = {arm: _cell(cells.get((facet, arm), []), measure) for arm in arms}
        measured[measure] = {arm: row[0] for arm, row in rows.items()}
        baseline = rows.get(base, ({}, {}))[1]
        comparisons = {}
        for arm, (_, means) in rows.items():
            if arm == base:
                continue
            common = sorted(means.keys() & baseline.keys())
            differences = [float(means[key] - baseline[key]) for key in common]
            comparisons[arm] = {**paired(differences, seed=seed), "instances": common}
        contrasts[measure] = comparisons
    return {"cells": measured, "contrasts": contrasts}


def analyze(
    attempts: Iterable[Attempt], *, family: str | None = None,
    measures: Sequence[str] = (), base: str = "probe", seed: int = 0,
) -> dict[str, Any]:
    groups: dict[tuple[str, str], dict[tuple[str, str], list[Attempt]]] = defaultdict(
        lambda: defaultdict(list)
    )
    keys = {"reward", *measures}
    placed = filtered = 0
    for attempt in attempts:
        cell = attempt.cell
        if family is not None and cell.family != family:
            filtered += 1
            continue
        placed += 1
        groups[attempt.roster, cell.family][cell.facet, cell.arm].append(attempt)
        if "focal" in attempt.rewards:
            keys.add("focal")
    ordered = ["reward", *sorted(keys - {"reward"})]
    results = []
    for (label, name), cells in sorted(groups.items()):
        arms = sorted({arm for _, arm in cells})
        facets = sorted({facet for facet, _ in cells})
        results.append({
            "roster": label, "family": name, "arms": arms,
            "facets": {facet: _facet(cells, facet, arms, ordered, base=base, seed=seed)
                       for facet in facets},
        })
    return {
        "base": base, "seed": seed, "measures": ordered, "placed": placed, "filtered": filtered,
        "groups": results, "limits": LIMITS, "unplaced": [],
    }


def report(jobs: Sequence[Path], **options: Any) -> dict[str, Any]:
    attempts, unplaced = collect(jobs)
    result = analyze(attempts, **options)
    result["unplaced"] = unplaced
    return result


def _number(value: Any) -> str:
    return value if isinstance(value, str) else format(value, ".6g")


def _cell_text(cell: dict[str, Any]) -> str:
    return (
        f"{_number(cell['mean'])} ({cell['instances']} i, {cell['valid_trials']} valid, "
        f"{cell['lost_trials']} lost, {cell['measure_trials']} measured)"
    )


def text(result: dict[str, Any]) -> str:
    lines = [result["limits"], f"Placed: {result['placed']}; filtered: {result['filtered']}; "
             f"unplaced: {len(result['unplaced'])}; base: {result['base']}; seed: {result['seed']}"]
    measures = result["measures"]
    for group in result["groups"]:
        lines.extend(["", f"{group['roster']} / {group['family']}"])
        arms = group["arms"]
        rows = []
        contrasts = []
        for facet, row in group["facets"].items():
            rows.append([facet, *["; ".join(f"{m}={_cell_text(row['cells'][m][arm])}"
                                           for m in measures) for arm in arms]])
            for measure in measures:
                for arm, stat in row["contrasts"][measure].items():
                    interval = stat["interval"]
                    ci = (interval if isinstance(interval, str)
                          else "[" + ", ".join(map(_number, interval)) + "]")
                    contrasts.append([facet, measure, f"{arm} - {result['base']}",
                                      _number(stat["difference"]), stat["pairs"], ci,
                                      _number(stat["sign_p"])])
        lines.append(render.table(["facet", *arms], rows))
        headers = ["facet", "measure", "contrast", "difference", "pairs", "95% CI", "sign p"]
        lines.append(render.table(headers, contrasts))
    for row in result["unplaced"]:
        lines.append(f"unplaced: {row['path']}: {row['reason']}")
    return "\n".join(lines) + "\n"
