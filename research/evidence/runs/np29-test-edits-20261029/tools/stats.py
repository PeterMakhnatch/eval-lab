"""NP29-CM-1 statistics and the disclosure rule. Pure functions; nothing here reads a data file or prints a cell.

Disclosure rule (README section 6): an exact 0 is printed; a count of 1 or 2 is suppressed; counts of 3 or more are printed.
A partition (cells that sum to a total) with exactly one suppressed cell would let the reader recover it from the total, so the
smallest non-zero printed cell is suppressed as well. A 1-2 cell derivable only through exact zeros of a total that is already
public is accepted and logged (``zero_exact_derivations``).
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from typing import Any

Z = 1.96
SUPPRESSED = "suppressed(1-2)"
SECONDARY = "suppressed(secondary)"


def wilson(k: int, n: int) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + Z * Z / n
    c = (p + Z * Z / (2 * n)) / d
    h = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / d
    return [round(100 * max(0.0, c - h), 1), round(100 * min(1.0, c + h), 1)]


def wilson_upper_at(k: int, n: int) -> float | None:
    interval = wilson(k, n)
    return interval[1] if interval else None


def newcombe(k1: int, n1: int, k2: int, n2: int) -> dict[str, Any]:
    """Difference p1 - p2 in points with the Newcombe hybrid-score 95% interval."""

    def score(k: int, n: int) -> tuple[float, float, float]:
        p = k / n
        d = 1 + Z * Z / n
        c = (p + Z * Z / (2 * n)) / d
        h = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / d
        return p, c - h, c + h

    p1, l1, u1 = score(k1, n1)
    p2, l2, u2 = score(k2, n2)
    diff = p1 - p2
    lo = diff - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    hi = diff + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return {"diff_pts": round(100 * diff, 1), "ci": [round(100 * lo, 1), round(100 * hi, 1)]}


def katz(k1: int, n1: int, k2: int, n2: int) -> dict[str, Any]:
    """Rate ratio (k1/n1)/(k2/n2) with the Katz log interval; 0.5 is added to every cell only when a cell is 0."""
    add = 0.5 if 0 in (k1, k2) else 0.0
    a, b = k1 + add, k2 + add
    m1, m2 = n1 + 2 * add, n2 + 2 * add
    ratio = (a / m1) / (b / m2)
    se = math.sqrt(1 / a - 1 / m1 + 1 / b - 1 / m2)
    return {
        "ratio": round(ratio, 3),
        "ci": [round(ratio * math.exp(-Z * se), 3), round(ratio * math.exp(Z * se), 3)],
        "half_added": bool(add),
    }


def fisher(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact p for [[a, b], [c, d]]."""
    n1, n2, k = a + b, c + d, a + c
    total = n1 + n2

    def pm(x: int) -> float:
        return math.comb(n1, x) * math.comb(n2, k - x) / math.comb(total, k)

    lo, hi = max(0, k - n2), min(n1, k)
    p0 = pm(a)
    return round(min(1.0, sum(pm(x) for x in range(lo, hi + 1) if pm(x) <= p0 * (1 + 1e-9))), 4)


def cluster_bootstrap_ratio(
    rows: Sequence[tuple[str, bool, bool]], draws: int = 10_000, seed: int = 20261029
) -> dict[str, Any]:
    """Task-cluster bootstrap of ``P(flag | S) / P(flag | not S)`` over rows ``(task, is_S, flag)`` pooled across classes.

    Tasks are resampled with replacement; a draw with an empty S or non-S group is dropped (the dropped share is reported).
    """
    by_task: dict[str, list[tuple[bool, bool]]] = {}
    for task, is_s, flag in rows:
        by_task.setdefault(task, []).append((is_s, flag))
    tasks = sorted(by_task)
    rng = random.Random(seed)
    ratios: list[float] = []
    dropped = 0
    for _ in range(draws):
        ks = ns = kn = nn = 0
        for t in (tasks[rng.randrange(len(tasks))] for _ in tasks):
            for is_s, flag in by_task[t]:
                if is_s:
                    ns += 1
                    ks += flag
                else:
                    nn += 1
                    kn += flag
        if ns == 0 or nn == 0 or kn == 0:
            dropped += 1
            continue
        ratios.append((ks / ns) / (kn / nn))
    ratios.sort()
    out: dict[str, Any] = {
        "draws": draws,
        "dropped_share": round(dropped / draws, 3),
        "tasks": len(tasks),
    }
    if len(ratios) >= 100:
        out["ci"] = [
            round(ratios[int(0.025 * len(ratios))], 3),
            round(ratios[int(0.975 * len(ratios)) - 1], 3),
        ]
    return out


def cluster_bootstrap_ratio_of_rates(
    num: Sequence[tuple[str, bool]],
    den: Sequence[tuple[str, bool]],
    draws: int = 10_000,
    seed: int = 20261029,
) -> dict[str, Any]:
    """Task-cluster bootstrap of ``P(S | num class) / P(S | den class)`` (``LR_S``); tasks pooled across both classes."""
    by_task: dict[str, list[tuple[int, bool]]] = {}
    for task, flag in num:
        by_task.setdefault(task, []).append((1, flag))
    for task, flag in den:
        by_task.setdefault(task, []).append((0, flag))
    tasks = sorted(by_task)
    rng = random.Random(seed)
    ratios: list[float] = []
    dropped = 0
    for _ in range(draws):
        kn = nn = kd = nd = 0
        for t in (tasks[rng.randrange(len(tasks))] for _ in tasks):
            for side, flag in by_task[t]:
                if side:
                    nn += 1
                    kn += flag
                else:
                    nd += 1
                    kd += flag
        if nn == 0 or nd == 0 or kd == 0:
            dropped += 1
            continue
        ratios.append((kn / nn) / (kd / nd))
    ratios.sort()
    out: dict[str, Any] = {
        "draws": draws,
        "dropped_share": round(dropped / draws, 3),
        "tasks": len(tasks),
    }
    if len(ratios) >= 100:
        out["ci"] = [
            round(ratios[int(0.025 * len(ratios))], 3),
            round(ratios[int(0.975 * len(ratios)) - 1], 3),
        ]
    return out


# ---------------------------------------------------------------- disclosure


def sup(k: int) -> int | str:
    return k if k == 0 or k >= 3 else SUPPRESSED


def partition(
    counts: Mapping[str, int], log: list[dict[str, Any]], name: str
) -> dict[str, int | str]:
    """Printable form of cells that sum to a known total.

    1-2 cells are suppressed; if exactly one cell is suppressed the smallest non-zero printed cell is suppressed too, unless
    every other cell is an exact zero (then the total, which is public or printed beside, determines it: logged and accepted).
    """
    out: dict[str, int | str] = {k: sup(v) for k, v in counts.items()}
    hidden = [k for k, v in out.items() if v == SUPPRESSED]
    if len(hidden) == 1:
        printed = sorted((v, k) for k, v in counts.items() if out[k] != SUPPRESSED and v > 0)
        if printed:
            out[printed[0][1]] = SECONDARY
            log.append({"partition": name, "rule": "secondary suppression", "cells": len(counts)})
        else:
            log.append(
                {"partition": name, "rule": "zero_exact_derivation accepted", "cells": len(counts)}
            )
    elif len(hidden) == 0:
        log.append({"partition": name, "rule": "no suppressed cell", "cells": len(counts)})
    else:
        log.append(
            {
                "partition": name,
                "rule": "two or more suppressed cells; not derivable",
                "cells": len(counts),
            }
        )
    return out


def cell(k: int, n: int, log: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """A k-of-n group-size or alarm cell; the pair (k, n - k) is a two-cell partition of n."""
    parts = partition({"flagged": k, "not_flagged": n - k}, log, name)
    return {"n": n, **parts}
