"""Statistics helpers for overnight experiments (vendored, no external imports)."""

from __future__ import annotations

import math
from typing import Any


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials; (nan, nan) when n == 0."""
    if n <= 0:
        return (float("nan"), float("nan"))
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denom
    half = (z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p from discordant counts b (A only) and c (B only)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2**n)
    return min(1.0, 2 * tail)


def paired(a: list[bool], b: list[bool]) -> dict[str, Any]:
    """Exact McNemar on two equal-length boolean outcome lists for the same cases."""
    if len(a) != len(b):
        raise ValueError(f"paired expects equal lengths, got {len(a)} and {len(b)}")
    only_a = sum(1 for x, y in zip(a, b, strict=True) if x and not y)
    only_b = sum(1 for x, y in zip(a, b, strict=True) if not x and y)
    both = sum(1 for x, y in zip(a, b, strict=True) if x and y)
    neither = sum(1 for x, y in zip(a, b, strict=True) if not x and not y)
    return {
        "n": len(a),
        "both": both,
        "neither": neither,
        "b": only_b,
        "c": only_a,
        "p": mcnemar_exact(only_a, only_b),
    }


def fmt(k: int, n: int) -> str:
    """'k/n = x.x% (lo-hi)' with the Wilson interval, for reports."""
    if n == 0:
        return "0/0 (nan)"
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {100 * k / n:.1f}% ({100 * lo:.1f}–{100 * hi:.1f}%)"
