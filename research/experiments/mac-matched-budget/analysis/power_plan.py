from __future__ import annotations

import argparse
import json
import math
import random
import sys
from bisect import bisect_left
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cache
from itertools import repeat
from operator import getitem
from statistics import NormalDist, fmean

from tidegauge.stats import sample_variance, sign_flip_p, t_quantile, t_two_sided_p, z_quantile

CONTRACT = "mac-power-plan/2"
EXACT_PAIRS = 22
HITS_ALLOWED = 9
MAX_FLIPS = 20_000
MAX_DRAWS = 20_000
MAX_PAIRS = 20_000
MAX_ATTRITION = 0.5
MAX_START = 8 * MAX_PAIRS
ANALYTIC_CEILING = 10_000_000
FUNDED_PROB = 0.95
NP_MARGIN = 0.02
SPEND = (0.2, 0.3, 0.5)
MIN_ALPHA = 0.0025
BAND_Z = 1.96
MDE_STEPS = 10
SELF_CHECKS = 24
TARGETS = (("p80", 0.8), ("p90", 0.9))
_NORMAL = NormalDist()


@dataclass(frozen=True, slots=True)
class Spec:
    control_sd: float
    team_sd: float
    effect: float
    alpha: float
    rho: float
    attrition: float
    seed: int
    draws: int
    max_pairs: int
    looks: int
    futility_z: float

    @property
    def diff_sd(self) -> float:
        variance = self.control_sd**2 + self.team_sd**2 - 2.0 * self.rho * self.control_sd * self.team_sd
        assert variance > 0.0
        return math.sqrt(variance)

    @property
    def pair_rate(self) -> float:
        return (1.0 - self.attrition) ** 2

    @property
    def arm_rate(self) -> float:
        return 1.0 - self.attrition


@dataclass(frozen=True, slots=True)
class Cost:
    control_model: float
    control_sandbox: float
    team_model: float
    team_sandbox: float

    @property
    def pair(self) -> float:
        return self.control_model + self.control_sandbox + self.team_model + self.team_sandbox

    @property
    def model(self) -> float:
        return self.control_model + self.team_model

    @property
    def sandbox(self) -> float:
        return self.control_sandbox + self.team_sandbox


@dataclass(frozen=True, slots=True)
class Estimate:
    hits: int
    draws: int

    @property
    def rate(self) -> float:
        assert 0 <= self.hits <= self.draws
        return self.hits / self.draws

    @property
    def se(self) -> float:
        return math.sqrt(self.rate * (1.0 - self.rate) / self.draws)


@dataclass(frozen=True, slots=True)
class Plan:
    complete_pairs: int
    cap_start: int
    look_pairs: tuple[int, ...]
    look_alpha: tuple[float, ...]
    flips: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Search:
    found: int | None
    kind: str


def flips_for(level: float) -> int:
    assert 0.0 < level < 1.0
    needed = math.ceil((HITS_ALLOWED + 1) / level - 1e-9)
    if needed - 1 > MAX_FLIPS:
        return 0
    flips = needed - 1
    assert (HITS_ALLOWED + 1) / (flips + 1) <= level + 1e-12
    assert (HITS_ALLOWED + 2) / (flips + 1) > level
    return flips


def _signed_sums(values: Sequence[float]) -> list[float]:
    sums = [0.0]
    for v in values:
        sums = [s + v for s in sums] + [s - v for s in sums]
    return sums


def exact_sign_flip_p(deltas: Sequence[float]) -> float:
    n = len(deltas)
    assert 0 < n <= EXACT_PAIRS
    observed = abs(math.fsum(deltas))
    threshold = observed - 1e-9 * max(1.0, observed)
    if threshold <= 0.0:
        return 1.0
    magnitudes = [abs(d) for d in deltas]
    left = _signed_sums(magnitudes[: n // 2])
    right = sorted(_signed_sums(magnitudes[n // 2 :]))
    below = sum(map(bisect_left, repeat(right), [threshold - x for x in left]))
    upper = len(left) * len(right) - below
    assert 1 <= upper <= 2 ** (n - 1)
    p = 2 * upper / 2**n
    assert 0.0 < p <= 1.0
    return p


def _chunk_tables(magnitudes: Sequence[float]) -> list[list[float]]:
    tables = []
    for start in range(0, len(magnitudes), 8):
        sums = [0.0]
        for v in magnitudes[start : start + 8]:
            sums = sums + [s + v for s in sums]
        tables.append(sums)
    return tables


def randomized_reject(deltas: Sequence[float], flips: int, rng: random.Random) -> bool:
    n = len(deltas)
    assert n > 0 and flips > 0
    observed = abs(math.fsum(deltas))
    threshold = observed - 1e-9 * max(1.0, observed)
    if threshold <= 0.0:
        return False
    magnitudes = [abs(d) for d in deltas]
    total = math.fsum(magnitudes)
    tables = _chunk_tables(magnitudes)
    width = len(tables)
    hits = 0
    for _ in range(flips):
        half = sum(map(getitem, tables, rng.getrandbits(n).to_bytes(width, "little")))
        if abs(2.0 * half - total) >= threshold:
            hits += 1
            if hits > HITS_ALLOWED:
                return False
    return True


def paired_reject(deltas: Sequence[float], level: float, flips: int, rng: random.Random) -> bool:
    if not deltas:
        return False
    if len(deltas) <= EXACT_PAIRS:
        return exact_sign_flip_p(deltas) <= level
    if flips == 0:
        return False
    return randomized_reject(deltas, flips, rng)


def welch_direction(control: Sequence[float], team: Sequence[float], alpha: float) -> int:
    nc, nt = len(control), len(team)
    if nc < 2 or nt < 2:
        return 0
    var_c, var_t = sample_variance(control), sample_variance(team)
    assert var_c is not None and var_t is not None
    vc, vt = var_c / nc, var_t / nt
    se2 = vc + vt
    if se2 <= 0.0:
        return 0
    diff = fmean(team) - fmean(control)
    df = se2 * se2 / (vc * vc / (nc - 1) + vt * vt / (nt - 1))
    p = t_two_sided_p(diff / math.sqrt(se2), df)
    assert 0.0 <= p <= 1.0
    if p > alpha or diff == 0.0:
        return 0
    return 1 if diff > 0.0 else -1


def _rng(seed: int, tag: str, replicate: int) -> random.Random:
    return random.Random(f"{seed}/{tag}/{replicate}")


def _draw_pair(rng: random.Random, sd: float, effect: float, attrition: float) -> float | None:
    kept = attrition == 0.0 or (rng.random() >= attrition) & (rng.random() >= attrition)
    value = effect + sd * rng.gauss(0.0, 1.0)
    return value if kept else None


def _pair_deltas(rng: random.Random, sd: float, effect: float, start: int, attrition: float) -> list[float]:
    out = []
    for _ in range(start):
        value = _draw_pair(rng, sd, effect, attrition)
        if value is not None:
            out.append(value)
    assert len(out) <= start
    return out


def _arm(rng: random.Random, mean: float, sd: float, start: int, attrition: float) -> list[float]:
    out = []
    for _ in range(start):
        kept = attrition == 0.0 or rng.random() >= attrition
        value = mean + sd * rng.gauss(0.0, 1.0)
        if kept:
            out.append(value)
    assert len(out) <= start
    return out


def _counts(effect: float, total: float) -> bool:
    return effect == 0.0 or total * effect > 0.0


def paired_estimate(spec: Spec, effect: float, start: int, attrition: float) -> Estimate:
    assert start >= 1
    flips = flips_for(spec.alpha)
    hits = 0
    for replicate in range(spec.draws):
        rng = _rng(spec.seed, "paired", replicate)
        deltas = _pair_deltas(rng, spec.diff_sd, effect, start, attrition)
        reject = paired_reject(deltas, spec.alpha, flips, rng)
        hits += reject and _counts(effect, math.fsum(deltas))
    return Estimate(hits, spec.draws)


def unpaired_estimate(spec: Spec, effect: float, start: int, attrition: float) -> Estimate:
    assert start >= 1
    hits = 0
    for replicate in range(spec.draws):
        control = _arm(_rng(spec.seed, "control", replicate), 0.0, spec.control_sd, start, attrition)
        team = _arm(_rng(spec.seed, "team", replicate), effect, spec.team_sd, start, attrition)
        direction = welch_direction(control, team, spec.alpha)
        hits += direction != 0 and _counts(effect, float(direction))
    return Estimate(hits, spec.draws)


@cache
def _critical(level: float, df: int) -> float:
    if df > 1000:
        return z_quantile(1.0 - level / 2.0)
    return t_quantile(1.0 - level / 2.0, df)


def _paired_ncp(spec: Spec, effect: float, n: float) -> tuple[float, int]:
    return abs(effect) * math.sqrt(n) / spec.diff_sd, max(1, math.floor(n) - 1)


def _unpaired_ncp(spec: Spec, effect: float, n: float) -> tuple[float, int]:
    vc, vt = spec.control_sd**2, spec.team_sd**2
    assert vc + vt > 0.0
    df = (vc + vt) ** 2 / ((vc * vc + vt * vt) / max(1.0, n - 1.0))
    return abs(effect) / math.sqrt((vc + vt) / n), max(1, math.floor(df))


def _approx_power(spec: Spec, ncp_fn: Callable, effect: float, n: float, level: float) -> float:
    ncp, df = ncp_fn(spec, effect, n)
    return _NORMAL.cdf(ncp - _critical(level, df))


def analytic_n(spec: Spec, ncp_fn: Callable, target: float, floor: int, level: float) -> int:
    lo, hi = floor, ANALYTIC_CEILING
    if _approx_power(spec, ncp_fn, spec.effect, lo, level) >= target:
        return lo
    if _approx_power(spec, ncp_fn, spec.effect, hi, level) < target:
        return hi
    for _ in range(64):
        if hi - lo <= 1:
            break
        mid = (lo + hi) // 2
        if _approx_power(spec, ncp_fn, spec.effect, mid, level) >= target:
            hi = mid
        else:
            lo = mid
    assert hi - lo == 1
    return hi


def _bracket(power: Callable[[int], float], target: float, guess: int, floor: int, ceiling: int):
    lo = max(floor, min(ceiling, int(guess * 0.6)))
    hi = max(lo + 1, min(ceiling, math.ceil(guess * 1.5) + 3))
    for _ in range(24):
        if power(hi) >= target or hi == ceiling:
            break
        lo, hi = hi, min(ceiling, hi * 2)
    if power(hi) < target:
        return None
    for _ in range(24):
        if lo <= floor or power(lo) < target:
            break
        hi, lo = lo, max(floor, lo // 2)
    assert power(hi) >= target
    return lo, hi


def search_n(
    evaluate: Callable[[int], Estimate],
    bound: Callable[[int], float],
    table: dict[int, Estimate],
    target: float,
    guess: int,
    ceiling: int,
) -> Search:
    assert ceiling >= 1

    def power(n: int) -> float:
        if n not in table:
            table[n] = evaluate(n)
        return table[n].rate

    top = min(EXACT_PAIRS, ceiling)
    for n in range(1, top + 1):
        if bound(n) >= target - NP_MARGIN and power(n) >= target:
            return Search(n, "scan")
    if top == ceiling or guess > ceiling * 1.25:
        return Search(None, "out_of_range")
    bracket = _bracket(power, target, guess, top, ceiling)
    if bracket is None:
        return Search(None, "out_of_range")
    lo, hi = bracket
    if power(lo) >= target:
        return Search(lo, "bisection")
    for _ in range(64):
        if hi - lo <= 1:
            break
        mid = (lo + hi) // 2
        if power(mid) >= target:
            hi = mid
        else:
            lo = mid
    assert hi - lo == 1 and power(hi) >= target > power(lo)
    return Search(hi, "bisection")


def _dips(table: dict[int, Estimate], found: int | None, target: float) -> list[int]:
    if found is None:
        return []
    return [m for m in sorted(table) if found < m <= EXACT_PAIRS and table[m].rate < target]


def np_bound(spec: Spec, ncp_fn: Callable, n: int) -> float:
    ncp, _ = ncp_fn(spec, spec.effect, n)
    return _NORMAL.cdf(ncp - z_quantile(1.0 - spec.alpha))


def _first(meets: Callable[[int], bool], lo: int, hi: int) -> int | None:
    assert lo <= hi
    if not meets(hi):
        return None
    if meets(lo):
        return lo
    for _ in range(64):
        if hi - lo <= 1:
            break
        mid = (lo + hi) // 2
        if meets(mid):
            hi = mid
        else:
            lo = mid
    return hi


def _band(table: dict[int, Estimate], evaluate: Callable[[int], Estimate], target: float, found: int, ceiling: int):
    def estimate(n: int) -> Estimate:
        if n not in table:
            table[n] = evaluate(n)
        return table[n]

    def optimistic(n: int) -> bool:
        return estimate(n).rate + BAND_Z * estimate(n).se >= target

    def conservative(n: int) -> bool:
        return estimate(n).rate - BAND_Z * estimate(n).se >= target

    low = _first(optimistic, max(1, found // 2), found)
    high = _first(conservative, found, min(ceiling, 2 * found + 10))
    return low, high


def binom_tail(start: int, rate: float, need: int) -> float:
    assert start >= 0 and 0.0 < rate <= 1.0
    if need <= 0:
        return 1.0
    if need > start:
        return 0.0
    if rate == 1.0:
        return 1.0
    log_rate, log_miss = math.log(rate), math.log1p(-rate)
    head = math.lgamma(start + 1)
    terms = [
        math.exp(head - math.lgamma(j + 1) - math.lgamma(start - j + 1) + j * log_rate + (start - j) * log_miss)
        for j in range(need, start + 1)
    ]
    return min(1.0, math.fsum(terms))


def funded_start(need: int, rate: float, prob: float, arms: int) -> int:
    assert need >= 1 and arms in (1, 2)

    def enough(start: int) -> bool:
        return binom_tail(start, rate, need) ** arms >= prob

    lo, hi = need - 1, max(need, math.ceil(need / rate))
    assert hi <= MAX_START
    for _ in range(64):
        if enough(hi):
            break
        lo, hi = hi, hi * 2
    assert enough(hi)
    for _ in range(64):
        if hi - lo <= 1:
            break
        mid = (lo + hi) // 2
        if enough(mid):
            hi = mid
        else:
            lo = mid
    return hi


def _usd(cost: Cost | None, start: float, part: str = "pair") -> float | None:
    if cost is None:
        return None
    return round(start * getattr(cost, part), 4)


def look_weights(count: int) -> tuple[float, ...]:
    assert 1 <= count <= len(SPEND)
    chosen = SPEND[:count]
    weights = tuple(w / math.fsum(chosen) for w in chosen)
    assert abs(math.fsum(weights) - 1.0) < 1e-12
    return weights


def make_plan(spec: Spec, complete_pairs: int, cap_start: int) -> Plan:
    assert complete_pairs >= 1 and cap_start >= complete_pairs
    raw = [math.ceil(complete_pairs * k / spec.looks) for k in range(1, spec.looks + 1)]
    looks = tuple(sorted(set(raw)))
    levels = tuple(spec.alpha * w for w in look_weights(len(looks)))
    flips = tuple(flips_for(level) for level in levels)
    assert looks[-1] == complete_pairs and len(looks) <= 3
    assert math.fsum(levels) <= spec.alpha + 1e-12 and all(flips)
    return Plan(complete_pairs, cap_start, looks, levels, flips)


def _futile(spec: Spec, deltas: Sequence[float]) -> bool:
    square = math.fsum(d * d for d in deltas)
    statistic = 0.0 if square == 0.0 else math.copysign(1.0, spec.effect) * math.fsum(deltas) / math.sqrt(square)
    return statistic < spec.futility_z


def run_sequential(spec: Spec, plan: Plan, effect: float, rng: random.Random) -> tuple[bool, bool, int, int, bool]:
    deltas: list[float] = []
    started = 0
    operational: bool | None = None
    stop_start, stop_look, stop_capped = 0, 0, False
    last = len(plan.look_pairs) - 1
    for k, need in enumerate(plan.look_pairs):
        for _ in range(plan.cap_start):
            if len(deltas) >= need or started >= plan.cap_start:
                break
            started += 1
            value = _draw_pair(rng, spec.diff_sd, effect, spec.attrition)
            if value is not None:
                deltas.append(value)
        capped = len(deltas) < need
        final = k == last or capped
        reject = paired_reject(deltas, plan.look_alpha[k], plan.flips[k], rng)
        success = reject and _counts(effect, math.fsum(deltas))
        if operational is None and (reject or final or _futile(spec, deltas)):
            operational, stop_start, stop_look, stop_capped = success, started, k + 1, capped
        if reject or final:
            assert operational is not None
            return operational, success, stop_start, stop_look, stop_capped
    raise AssertionError("sequential run ended without a final look")


def sequential_estimate(spec: Spec, plan: Plan, effect: float) -> dict:
    hits = ignoring = capped = 0
    starts: list[int] = []
    stops = [0] * len(plan.look_pairs)
    for replicate in range(spec.draws):
        rng = _rng(spec.seed, "sequential", replicate)
        operational, no_futility, started, look, was_capped = run_sequential(spec, plan, effect, rng)
        hits += operational
        ignoring += no_futility
        capped += was_capped
        starts.append(started)
        stops[look - 1] += 1
    assert sum(stops) == spec.draws and max(starts) <= plan.cap_start
    rate = Estimate(hits, spec.draws)
    return {
        "reject": rate.rate,
        "reject_se": rate.se,
        "reject_ignoring_futility": ignoring / spec.draws,
        "cap_stop": capped / spec.draws,
        "expected_start_pairs": fmean(starts),
        "stop_by_look": [round(s / spec.draws, 4) for s in stops],
    }


def describe_plan(cost: Cost | None, plan: Plan, effect: dict, null: dict) -> dict:
    return {
        "max_complete_pairs": plan.complete_pairs,
        "look_complete_pairs": list(plan.look_pairs),
        "look_alpha": [round(a, 6) for a in plan.look_alpha],
        "flips": list(plan.flips),
        "power": round(effect["reject"], 4),
        "power_se": round(effect["reject_se"], 4),
        "power_ignoring_futility": round(effect["reject_ignoring_futility"], 4),
        "null_reject": round(null["reject"], 4),
        "null_reject_se": round(null["reject_se"], 4),
        "null_reject_ignoring_futility": round(null["reject_ignoring_futility"], 4),
        "cap_stop_effect": round(effect["cap_stop"], 4),
        "cap_stop_null": round(null["cap_stop"], 4),
        "expected_start_pairs_effect": round(effect["expected_start_pairs"], 2),
        "expected_start_pairs_null": round(null["expected_start_pairs"], 2),
        "expected_usd_effect": _usd(cost, effect["expected_start_pairs"]),
        "expected_usd_null": _usd(cost, null["expected_start_pairs"]),
        "stop_by_look_effect": effect["stop_by_look"],
        "stop_by_look_null": null["stop_by_look"],
        "hard_cap_start_pairs": plan.cap_start,
        "hard_cap_usd": _usd(cost, plan.cap_start),
    }


def evaluate_plan(spec: Spec, cost: Cost | None, plan: Plan) -> dict:
    effect = sequential_estimate(spec, plan, spec.effect)
    null = sequential_estimate(spec, plan, 0.0)
    return describe_plan(cost, plan, effect, null)


def sequential_design(spec: Spec, cost: Cost | None, table: dict[int, Estimate], target: float) -> dict:
    count = len(set(math.ceil(1000 * k / spec.looks) for k in range(1, spec.looks + 1)))
    final_level = spec.alpha * look_weights(count)[-1]
    guess = analytic_n(spec, _paired_ncp, target, 1, final_level)

    def evaluate(n: int) -> Estimate:
        plan = make_plan(spec, n, funded_start(n, spec.pair_rate, FUNDED_PROB, 1))
        result = sequential_estimate(spec, plan, spec.effect)
        return Estimate(round(result["reject"] * spec.draws), spec.draws)

    search = search_n(evaluate, lambda n: np_bound(spec, _paired_ncp, n), table, target, guess, spec.max_pairs)
    head = {"status": "ok" if search.found is not None else "out_of_range", "target": target, "search": search.kind}
    head |= {"scan_dips": _dips(table, search.found, target), "analytic_final_look_pairs": guess, "evaluated_points": len(table)}
    if search.found is None:
        return head
    plan = make_plan(spec, search.found, funded_start(search.found, spec.pair_rate, FUNDED_PROB, 1))
    return head | evaluate_plan(spec, cost, plan)


def at_pairs_plan(spec: Spec, cost: Cost | None, pairs: int) -> dict:
    complete = max(1, math.floor(pairs * spec.pair_rate))
    plan = make_plan(spec, complete, pairs)
    head = {"status": "evaluated", "basis": "cap = --pairs started; max complete pairs = floor(pairs * pair_complete_rate)"}
    return head | evaluate_plan(spec, cost, plan)


def _estimate_view(estimate: Estimate) -> dict:
    return {"power": round(estimate.rate, 4), "power_se": round(estimate.se, 4)}


def mde(spec: Spec, estimate_fn: Callable, ncp_fn: Callable, start: int, rate: float, target: float) -> dict:
    unit, df = ncp_fn(spec, 1.0, max(2.0, start * rate))
    guess = (_critical(spec.alpha, df) + z_quantile(target)) / unit
    sign = math.copysign(1.0, spec.effect)
    lo, hi = 0.0, 3.0 * guess
    best = estimate_fn(spec, sign * hi, start, spec.attrition)
    for _ in range(6):
        if best.rate >= target:
            break
        lo, hi = hi, 2.0 * hi
        best = estimate_fn(spec, sign * hi, start, spec.attrition)
    if best.rate < target:
        return {"status": "out_of_range", "analytic_effect_abs": round(guess, 6), "largest_tried_abs": round(hi, 6)}
    for _ in range(MDE_STEPS):
        mid = (lo + hi) / 2.0
        trial = estimate_fn(spec, sign * mid, start, spec.attrition)
        if trial.rate >= target:
            hi, best = mid, trial
        else:
            lo = mid
    head = {"status": "ok", "effect_abs": round(hi, 6), "resolution_abs": round(hi - lo, 6)}
    return head | {"analytic_effect_abs": round(guess, 6)} | _estimate_view(best)


def size_section(spec: Spec, cost: Cost | None, table: dict[int, Estimate], target: float, paired: bool) -> dict:
    floor, arms = (1, 1) if paired else (2, 2)
    ncp_fn = _paired_ncp if paired else _unpaired_ncp
    rate = spec.pair_rate if paired else spec.arm_rate
    unit = "pairs" if paired else "per_arm"

    def evaluate(n: int) -> Estimate:
        if paired:
            return paired_estimate(spec, spec.effect, n, 0.0)
        return unpaired_estimate(spec, spec.effect, n, 0.0)

    guess = analytic_n(spec, ncp_fn, target, floor, spec.alpha)
    search = search_n(evaluate, lambda n: np_bound(spec, ncp_fn, n), table, target, guess, spec.max_pairs)
    found = search.found
    band_low, band_high = (None, None) if found is None else _band(table, evaluate, target, found, spec.max_pairs)
    out: dict = {
        "status": "ok" if found is not None else "out_of_range",
        "target": target,
        "search": search.kind,
        "scan_dips": _dips(table, found, target),
        f"analytic_complete_{unit}": guess,
        f"complete_{unit}": found,
        "evaluated_points": len(table),
        "band_low": band_low,
        "band_high": band_high,
    }
    if found is None:
        return out
    start = math.ceil(found / rate - 1e-9)
    funded = funded_start(found, rate, FUNDED_PROB, arms)
    return out | _estimate_view(table[found]) | {
        f"start_{unit}": start,
        f"funded_{unit}": funded,
        "usd_start": _usd(cost, start),
        "usd_funded": _usd(cost, funded),
        "usd_funded_model": _usd(cost, funded, "model"),
        "usd_funded_sandbox": _usd(cost, funded, "sandbox"),
    }


def fixed_section(spec: Spec, cost: Cost | None, pairs: int) -> dict:
    views = {}
    for name, fn in (("paired", paired_estimate), ("unpaired", unpaired_estimate)):
        effect = fn(spec, spec.effect, pairs, spec.attrition)
        null = fn(spec, 0.0, pairs, spec.attrition)
        views[name] = _estimate_view(effect) | {"null_reject": round(null.rate, 4), "null_reject_se": round(null.se, 4)}
    return {
        "start_pairs": pairs,
        "pair_complete_rate": round(spec.pair_rate, 6),
        "expected_complete_pairs": round(pairs * spec.pair_rate, 3),
        "min_pairs_any_reject": math.ceil(1.0 + math.log2(1.0 / spec.alpha) - 1e-12),
        "usd": _usd(cost, pairs),
        "usd_model": _usd(cost, pairs, "model"),
        "usd_sandbox": _usd(cost, pairs, "sandbox"),
    } | views


def self_check(seed: int) -> int:
    rng = random.Random(f"{seed}/self-check")
    for _ in range(SELF_CHECKS):
        n = rng.randint(1, 12)
        values = [float(rng.randint(-6, 6)) for _ in range(n)]
        lattice, exact = sign_flip_p(values, [1] * n)
        assert exact and lattice is not None
        assert abs(lattice - exact_sign_flip_p(values)) < 1e-9
    return SELF_CHECKS


def assumptions() -> list[str]:
    return [
        "INFERRED: the unit is a randomized block of 1 control trial and 1 team trial on the same task at the same total budget; under no effect the 2 labels are exchangeable within a block, so the block difference is symmetric about 0.",
        "INFERRED: block differences are Gaussian with sd sqrt(control_sd^2 + team_sd^2 - 2 rho control_sd team_sd); this is a sensitivity model, not fitted data.",
        "INFERRED: each trial loses its score independently with probability attrition (missing completely at random); a missing score is dropped, never scored 0; a block counts only when both trials have a score.",
        "INFERRED: a trial without a score is charged in full (model and sandbox).",
        "INFERRED: the unpaired comparison is an unblocked design with the same started trials per arm and independent arms; rho does not enter it.",
        "INFERRED: power counts a rejection only in the direction of --effect; null_reject counts a rejection in either direction.",
        "INFERRED: every number is a Monte Carlo estimate under this model; *_se is the binomial Monte Carlo standard error only and excludes uncertainty in the input spreads.",
    ]


def method(spec: Spec) -> dict:
    return {
        "paired_test": "two-sided sign-flip test on the sum of complete-block differences (same as muster-boxes PREREGISTRATION)",
        "exact_complete_pairs_max": EXACT_PAIRS,
        "exact": f"n <= {EXACT_PAIRS}: all 2^n sign patterns counted exactly by meet-in-the-middle; checked at start against tidegauge.stats.sign_flip_p integer lattice on {SELF_CHECKS} seeded integer vectors",
        "randomized": f"n > {EXACT_PAIRS}: B = ceil(10/level) - 1 random sign patterns, p = (1 + hits)/(B + 1), reject iff hits <= {HITS_ALLOWED}, valid at level; drawing stops once hits > {HITS_ALLOWED}, which gives the same decision as drawing all B; no approximation or shortcut",
        "flips_at_alpha": flips_for(spec.alpha),
        "unpaired_test": "two-sided Welch t test on scored trials (tidegauge.stats.t_two_sided_p, Satterthwaite df); allows unequal spreads and 1 arm with sd 0",
        "monte_carlo": f"{spec.draws} replicates per point; replicate r uses random.Random('seed/tag/r'); the same replicate streams are reused across n and effect (common random numbers)",
        "search": f"n from 1 to {EXACT_PAIRS} is scanned upward and stops at the first n whose simulated power reaches the target (search = scan); n is skipped without simulation when the known-variance one-sided Neyman-Pearson bound Phi(|effect| sqrt(n) / sd - z(1 - alpha)), an upper bound on the power of any level-alpha test, is below target - {NP_MARGIN}; above {EXACT_PAIRS}, an analytic guess (normal approximation with tidegauge t_quantile) sets a bracket and integer bisection on simulated power gives the estimated crossing (search = bisection; simulated power is not exactly monotone); scan_dips lists evaluated n above the answer and <= {EXACT_PAIRS} whose power is below the target (discreteness); resolution 1 complete pair (unpaired: 1 trial per arm); out_of_range without simulation when the analytic guess exceeds 1.25 x max_pairs",
        "band": "band_low (in [n/2, n]) and band_high (in [n, 2n+10]) are bisection crossings where simulated power +1.96 se and -1.96 se reach the target; a Monte Carlo band only",
        "search_attrition": "sample sizes are complete pairs (or scored trials per arm); start = ceil(need / complete rate); funded = smallest start with P(complete >= need) >= 0.95 (both arms for unpaired)",
        "mde": f"bisection on |effect| at --pairs started with attrition, {MDE_STEPS} halvings from a bracket of 3x the analytic guess; resolution_abs reported",
        "sequential_test": f"Bonferroni alpha split over looks (weights {list(SPEND)} for 3 looks, renormalized first weights for 1 or 2 distinct looks); each look runs the same sign-flip test (exact or randomized) at its own look_alpha; family-wise type I error <= alpha for any data-dependent stopping, without a Gaussian approximation; null_reject is the simulated check",
        "sequential_futility": "nonbinding: at an interim look stop when sign(effect) * sum(d) / sqrt(sum(d^2)) < futility_z; *_ignoring_futility keeps going; the alpha split ignores futility",
        "sequential_sizing": "max_complete_pairs = smallest simulated n (same scan then bisection as the fixed search) whose power with futility, attrition and funded cap reaches the target",
        "sequential_cap": "estimates include attrition and the funded cap (hard_cap_start_pairs); pairs start one at a time until the look's complete-pair count or the cap; if the cap comes first, that look tests the pairs it has at its own look_alpha only and the run stops (cap_stop_* is how often)",
    }


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="power_plan")
    p.add_argument("--control-sd", type=float, required=True)
    p.add_argument("--team-sd", type=float, required=True)
    p.add_argument("--effect", type=float, required=True)
    p.add_argument("--pairs", type=int, required=True)
    p.add_argument("--alpha", type=float, default=0.05)
    p.add_argument("--rho", type=float, default=0.0)
    p.add_argument("--attrition", type=float, default=0.0)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--draws", type=int, default=1000)
    p.add_argument("--max-pairs", type=int, default=2000)
    p.add_argument("--looks", type=int, choices=(1, 2, 3), default=3)
    p.add_argument("--futility-z", type=float, default=0.0)
    p.add_argument("--fixed-only", action="store_true")
    for name in ("--control-model", "--control-sandbox", "--team-model", "--team-sandbox"):
        p.add_argument(name, type=float, default=None)
    return p


def build(argv: Sequence[str] | None) -> tuple[argparse.Namespace, Spec, Cost | None]:
    parser = _parser()
    a = parser.parse_args(argv)
    prices = [a.control_model, a.control_sandbox, a.team_model, a.team_sandbox]
    checks = [
        (all(math.isfinite(v) and v >= 0.0 for v in (a.control_sd, a.team_sd)), "spreads must be finite and >= 0"),
        (math.isfinite(a.effect) and a.effect != 0.0, "--effect must be finite and not 0"),
        (MIN_ALPHA <= a.alpha <= 0.5, f"--alpha must be in [{MIN_ALPHA}, 0.5] so every look can resolve"),
        (-1.0 <= a.rho <= 1.0, "--rho must be in [-1, 1]"),
        (0.0 <= a.attrition <= MAX_ATTRITION, f"--attrition must be in [0, {MAX_ATTRITION}] per arm"),
        (1 <= a.draws <= MAX_DRAWS, f"--draws must be in [1, {MAX_DRAWS}]"),
        (1 <= a.pairs <= MAX_PAIRS and 2 <= a.max_pairs <= MAX_PAIRS, f"--pairs and --max-pairs must be <= {MAX_PAIRS}"),
        (math.isfinite(a.futility_z), "--futility-z must be finite"),
        (all(v is None for v in prices) or all(v is not None for v in prices), "give all 4 cost flags or none"),
        (all(v is None or (math.isfinite(v) and v >= 0.0) for v in prices), "costs must be finite and >= 0"),
    ]
    for ok, message in checks:
        if not ok:
            parser.error(message)
    if a.control_sd**2 + a.team_sd**2 - 2.0 * a.rho * a.control_sd * a.team_sd <= 0.0:
        parser.error("the pair difference variance is 0 for these spreads and --rho")
    spec = Spec(a.control_sd, a.team_sd, a.effect, a.alpha, a.rho, a.attrition, a.seed, a.draws, a.max_pairs, a.looks, a.futility_z)
    cost = None if a.control_model is None else Cost(a.control_model, a.control_sandbox, a.team_model, a.team_sandbox)
    return a, spec, cost


def _cost_view(cost: Cost | None) -> dict:
    if cost is None:
        return dict.fromkeys(("control_trial_usd", "team_trial_usd", "pair_model_usd", "pair_sandbox_usd", "pair_usd"))
    return {
        "control_trial_usd": cost.control_model + cost.control_sandbox,
        "team_trial_usd": cost.team_model + cost.team_sandbox,
        "pair_model_usd": cost.model,
        "pair_sandbox_usd": cost.sandbox,
        "pair_usd": cost.pair,
    }


def planned(spec: Spec, cost: Cost | None, pairs: int) -> dict:
    tables: dict[str, dict[int, Estimate]] = {"paired": {}, "unpaired": {}, "sequential": {}}
    sizes: dict = {"paired": {}, "unpaired": {}}
    designs: dict = {}
    mdes: dict = {"paired": {}, "unpaired": {}}
    for key, target in TARGETS:
        sizes["paired"][key] = size_section(spec, cost, tables["paired"], target, True)
        sizes["unpaired"][key] = size_section(spec, cost, tables["unpaired"], target, False)
        designs[key] = sequential_design(spec, cost, tables["sequential"], target)
        mdes["paired"][key] = mde(spec, paired_estimate, _paired_ncp, pairs, spec.pair_rate, target)
        mdes["unpaired"][key] = mde(spec, unpaired_estimate, _unpaired_ncp, pairs, spec.arm_rate, target)
    sequential = {
        "looks": spec.looks,
        "spending": "Bonferroni split of alpha",
        "weights": list(look_weights(spec.looks)),
        "alpha": spec.alpha,
        "futility_z": spec.futility_z,
        "design": designs,
        "at_pairs": at_pairs_plan(spec, cost, pairs),
    }
    return {"mde": mdes, "sample_size": sizes, "sequential": sequential}


def main(argv: Sequence[str] | None = None) -> int:
    args, spec, cost = build(argv)
    checked = self_check(spec.seed)
    inputs = dict(vars(args)) | {"difference_sd": round(spec.diff_sd, 6)}
    report: dict = {
        "contract": CONTRACT,
        "inputs": inputs,
        "assumptions": assumptions(),
        "method": method(spec) | {"self_checks_passed": checked},
        "cost": _cost_view(cost),
        "fixed": fixed_section(spec, cost, args.pairs),
    }
    if not args.fixed_only:
        report |= planned(spec, cost, args.pairs)
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
