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


def cases_for_layer_value(
    unique_rate: float,
    *,
    alpha: float = 0.05,
    power: float = 0.8,
    max_n: int = 100_000,
) -> int | None:
    """Smallest case count with the requested chance of sufficient unique catches.

    This plug-in projection assumes independent cases and a constant unique-catch
    rate. It uses the exact binomial lower tail, not a normal approximation.
    """
    _probability(alpha, "alpha", open_interval=True)
    _probability(power, "power", open_interval=True)
    _count(max_n, "max_n")
    if (
        not isinstance(unique_rate, (int, float))
        or isinstance(unique_rate, bool)
        or not math.isfinite(unique_rate)
    ):
        raise ValueError("unique_rate must be a finite number")
    if unique_rate > 1:
        raise ValueError("unique_rate must not exceed 1")
    minimum = min_n_mcnemar(alpha)["n"]
    if unique_rate <= 0 or max_n < minimum:
        return None
    if unique_rate == 1:
        return minimum
    log_rate = math.log(unique_rate)
    log_miss = math.log1p(-unique_rate)
    log_target = math.log1p(-power)

    def reaches_power(n: int) -> bool:
        # Anchor at the largest term in the lower tail so its recurrence cannot
        # underflow before reaching a significant probability mass.
        anchor = min(minimum - 1, int((n + 1) * unique_rate))
        log_anchor = (
            math.lgamma(n + 1) - math.lgamma(anchor + 1)
            - math.lgamma(n - anchor + 1)
            + anchor * log_rate + (n - anchor) * log_miss
        )
        scaled = 1.0
        total = 1.0
        for k in range(anchor, 0, -1):
            scaled *= k / (n - k + 1) * ((1 - unique_rate) / unique_rate)
            total += scaled
        scaled = 1.0
        for k in range(anchor + 1, minimum):
            scaled *= (n - k + 1) / k * (unique_rate / (1 - unique_rate))
            total += scaled
        return log_anchor + math.log(total) <= log_target

    if not reaches_power(max_n):
        return None
    low, high = minimum, max_n
    while low < high:
        middle = (low + high) // 2
        if reaches_power(middle):
            high = middle
        else:
            low = middle + 1
    return low


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
