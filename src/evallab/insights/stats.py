"""Small deterministic statistics exposed to insight analysts."""

from __future__ import annotations

import inspect
import math
from collections.abc import Callable
from typing import Any

from evallab.analysis_statistics import compute_wilson_interval, fisher_exact_2x2


def _count(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _probability(value: float, name: str, *, open_interval: bool = False) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a finite probability")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be between 0 and 1")
    if open_interval and not 0 < value < 1:
        raise ValueError(f"{name} must be strictly between 0 and 1")


def _binomial(k: int, n: int) -> None:
    _count(k, "k")
    _count(n, "n")
    if k > n:
        raise ValueError("k must not exceed n")


def wilson(k: int, n: int, confidence: float = 0.95) -> dict[str, float | None]:
    """Return a Wilson interval; zero opportunities have no estimated proportion."""
    _binomial(k, n)
    _probability(confidence, "confidence", open_interval=True)
    bounds = compute_wilson_interval(k, n, confidence)
    if bounds is None:
        return {"p": None, "low": None, "high": None}
    return {"p": k / n, "low": bounds[0], "high": bounds[1]}


def mcnemar_exact(b: int, c: int) -> dict[str, float | None]:
    """Two-sided exact binomial test on discordant paired outcomes."""
    _count(b, "b")
    _count(c, "c")
    n = b + c
    # The existing paired helper requires materializing one object per observation.
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) if n else 0
    p = 1.0 if n == 0 else min(1.0, 2 * tail / 2**n)
    return {"p_value": p}


def fisher(a: int, b: int, c: int, d: int) -> dict[str, float | None]:
    for name, value in (("a", a), ("b", b), ("c", c), ("d", d)):
        _count(value, name)
    result = fisher_exact_2x2(((a, b), (c, d)))
    return {"p_value": result.exact_p_value}


def two_proportion(k1: int, n1: int, k2: int, n2: int) -> dict[str, float | None]:
    """First minus second, with Newcombe hybrid-score bounds and Fisher p."""
    _binomial(k1, n1)
    _binomial(k2, n2)
    bounds1 = compute_wilson_interval(k1, n1)
    bounds2 = compute_wilson_interval(k2, n2)
    p_value = fisher(k1, n1 - k1, k2, n2 - k2)["p_value"]
    if bounds1 is None or bounds2 is None:
        return {"diff": None, "low": None, "high": None, "p_value": p_value}
    p1, p2 = k1 / n1, k2 / n2
    diff = p1 - p2
    low = diff - math.hypot(p1 - bounds1[0], bounds2[1] - p2)
    high = diff + math.hypot(bounds1[1] - p1, p2 - bounds2[0])
    return {"diff": diff, "low": max(-1.0, low), "high": min(1.0, high), "p_value": p_value}


def rule_of_three(n: int) -> dict[str, float | None]:
    """One-sided 95% zero-event bound, plus the usual 3/n approximation.

    For all successes, mirror the bound: lower95 = 1 - upper95.
    """
    _count(n, "n")
    if n == 0:
        return {"upper95": None, "three_over_n": None}
    return {"upper95": -math.expm1(math.log(0.05) / n), "three_over_n": 3 / n}


def bh(p_values: list[float], q: float = 0.05) -> dict[str, list[float] | list[bool]]:
    """Benjamini-Hochberg adjusted p-values, in the original input order."""
    _probability(q, "q", open_interval=True)
    if not isinstance(p_values, list):
        raise ValueError("p_values must be a list of probabilities")
    for value in p_values:
        _probability(value, "p_values entry")
    size = len(p_values)
    order = sorted(range(size), key=p_values.__getitem__)
    adjusted = [1.0] * size
    running = 1.0
    for rank in range(size, 0, -1):
        index = order[rank - 1]
        running = min(running, p_values[index] * size / rank)
        adjusted[index] = running
    return {"adjusted": adjusted, "rejected": [value <= q for value in adjusted]}


def min_n_mcnemar(alpha: float = 0.05) -> dict[str, int]:
    _probability(alpha, "alpha", open_interval=True)
    n = 1
    while math.ldexp(1.0, 1 - n) >= alpha:
        n += 1
    return {"n": n}


_OPERATIONS: dict[str, Callable[..., Any]] = {
    "wilson": wilson,
    "mcnemar": mcnemar_exact,
    "fisher": fisher,
    "two_proportion": two_proportion,
    "rule_of_three": rule_of_three,
    "bh": bh,
    "min_n_mcnemar": min_n_mcnemar,
}


def compute(op: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate an operation's arguments and return its JSON-compatible result."""
    if not isinstance(op, str) or op not in _OPERATIONS:
        raise ValueError(f"unknown statistic: {op}")
    if not isinstance(args, dict) or not all(isinstance(key, str) for key in args):
        raise ValueError("args must be a JSON object with named arguments")
    operation = _OPERATIONS[op]
    try:
        inspect.signature(operation).bind(**args)
    except TypeError as exc:
        raise ValueError(f"invalid arguments for {op}: {exc}") from exc
    return operation(**args)
