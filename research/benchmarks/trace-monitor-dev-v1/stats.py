"""Statistics helpers for overnight workers (no third-party imports).

The functions that were misused in earlier reports now take the raw paired outcomes, so a
caller cannot hand in the wrong discordant counts:

    stats.paired(a_bools, b_bools)        -> dict(b, c, both, neither, p)   # exact McNemar
    stats.recall_at_fa(pos, neg, fa_max)  -> dict(k, n, recall, threshold, false_alarms, n_neg)
    stats.auroc(pos, neg)                 -> float (ties count one half)
"""
from __future__ import annotations

import math


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials; (nan, nan) when n == 0."""
    if n <= 0:
        return (math.nan, math.nan)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p from discordant counts b (A only) and c (B only)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / 2**n
    return min(1.0, 2 * tail)


def paired(a: list[bool], b: list[bool]) -> dict:
    """Exact McNemar on two equal-length boolean outcome lists for the same cases.

    `b` in the result counts cases where only A is True; `c` counts cases where only B is True.
    """
    if len(a) != len(b):
        raise ValueError(f"paired outcomes differ in length: {len(a)} vs {len(b)}")
    only_a = sum(1 for x, y in zip(a, b) if x and not y)
    only_b = sum(1 for x, y in zip(a, b) if y and not x)
    both = sum(1 for x, y in zip(a, b) if x and y)
    neither = len(a) - only_a - only_b - both
    return {"b": only_a, "c": only_b, "both": both, "neither": neither, "n": len(a),
            "p": mcnemar_exact(only_a, only_b)}


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact p for the 2x2 table [[a, b], [c, d]] (sum of tables as or less likely)."""
    r1, r2, c1, n = a + b, c + d, a + c, a + b + c + d

    def prob(x: int) -> float:
        return math.comb(r1, x) * math.comb(r2, c1 - x) / math.comb(n, c1)

    lo, hi = max(0, c1 - r2), min(r1, c1)
    p_obs = prob(a)
    return min(1.0, sum(prob(x) for x in range(lo, hi + 1) if prob(x) <= p_obs * (1 + 1e-9)))


def auroc(pos: list[float], neg: list[float]) -> float:
    """Area under the ROC curve; ties count one half. nan when either side is empty."""
    if not pos or not neg:
        return math.nan
    wins = sum((p > q) + 0.5 * (p == q) for p in pos for q in neg)
    return wins / (len(pos) * len(neg))


def recall_at_fa(pos: list[float], neg: list[float], fa_max: float) -> dict:
    """Best recall with at most floor(fa_max * len(neg)) false alarms, alert = score >= threshold.

    Thresholds are chosen on the scores given, so the result is an in-sample operating point:
    choose the threshold on a tuning fold and apply it to a reporting fold when you need an honest rate.
    """
    allowed = math.floor(fa_max * len(neg) + 1e-9)
    best = {"k": 0, "n": len(pos), "recall": 0.0, "threshold": math.inf, "false_alarms": 0, "n_neg": len(neg)}
    for t in sorted(set(pos) | set(neg)):
        fa = sum(1 for q in neg if q >= t)
        if fa <= allowed:
            k = sum(1 for p in pos if p >= t)
            if k > best["k"]:
                best = {"k": k, "n": len(pos), "recall": k / len(pos) if pos else math.nan,
                        "threshold": t, "false_alarms": fa, "n_neg": len(neg)}
    return best


def tertiles(values: list[float]) -> tuple[float, float]:
    """The two cut points that split values into three groups of (almost) equal size."""
    s = sorted(values)
    if not s:
        return (math.nan, math.nan)
    return (s[len(s) // 3], s[(2 * len(s)) // 3])


def tertile_of(value: float, cuts: tuple[float, float]) -> int:
    """0, 1 or 2: which tertile `value` falls in given the cut points from `tertiles`."""
    return 0 if value < cuts[0] else (1 if value < cuts[1] else 2)


def median(values: list[float]) -> float:
    s = sorted(values)
    if not s:
        return math.nan
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def fmt(k: int, n: int) -> str:
    """'k/n = x.x% (lo-hi)' with the Wilson interval, for reports."""
    if n == 0:
        return "0/0"
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {100 * k / n:.1f}% ({100 * lo:.1f}-{100 * hi:.1f})"


if __name__ == "__main__":
    assert abs(wilson(25, 80)[0] - 0.2217) < 0.01
    assert mcnemar_exact(10, 0) < 0.005 and mcnemar_exact(3, 3) == 1.0
    r = paired([True, True, False, False, True], [True, False, False, True, False])
    assert (r["b"], r["c"], r["both"], r["neither"]) == (2, 1, 1, 1), r
    assert abs(mcnemar_exact(8, 2) - 0.109375) < 1e-9          # last night's 0.031 was McNemar(6, 0)
    assert abs(fisher_exact(10, 0, 0, 10) - 2 * math.comb(10, 10) / math.comb(20, 10)) < 1e-12
    assert auroc([3, 4], [1, 2]) == 1.0 and auroc([1], [1]) == 0.5
    rf = recall_at_fa([5, 4, 3, 1], [4, 2, 1, 0, 0, 0, 0, 0, 0, 0], 0.1)
    assert rf["false_alarms"] <= 1 and rf["k"] == 3 and rf["threshold"] == 3, rf
    print(fmt(14, 80), "| tertiles", tertiles([1, 2, 3, 4, 5, 6, 7, 8, 9]), "| stats self-test ok")
