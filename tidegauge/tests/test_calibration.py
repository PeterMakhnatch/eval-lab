"""Seeded simulations that guard the statistical claims of ``tidegauge.stats``.

Each test draws 0/1 trials from fixed per-task success probabilities and checks
a rate (rejections, coverage, power) against its target. A tolerance is a
nominal value plus or minus about 3 Monte Carlo standard errors of the rate,
sqrt(p (1 - p) / reps); the comment at each bound gives the numbers. Where the
method is known to miss its target, the bound encodes the measured behaviour.

The default run takes a few seconds. ``TIDEGAUGE_SLOW=1`` also runs a large
version. ``uv run python tests/test_calibration.py`` prints the tables that
docs/STATISTICS.md quotes.
"""

from __future__ import annotations

import itertools
import math
import os
import random
from collections import Counter
from statistics import NormalDist, fmean

import pytest

from tidegauge import stats

NORMAL = NormalDist()
Z975, Z80 = NORMAL.inv_cdf(0.975), NORMAL.inv_cdf(0.8)
ALPHA = 0.05

slow = pytest.mark.skipif(os.environ.get("TIDEGAUGE_SLOW") != "1", reason="set TIDEGAUGE_SLOW=1 to run")


def spread(n: int, lo: float = 0.0, hi: float = 1.0) -> list[float]:
    """``n`` success probabilities spread evenly over (lo, hi)."""
    return [lo + (hi - lo) * (i + 0.5) / n for i in range(n)]


# Per-task success probabilities. Real agent benchmarks have many tasks that an
# agent always or never solves; the rest spread in between.
PROFILES = {
    "mid30": [0.0] * 8 + [1.0] * 7 + spread(15),  # score 0.48
    "mid89": [0.0] * 25 + [1.0] * 20 + spread(44),  # score 0.47, Terminal-Bench 2.0 size
    "low89": [0.0] * 50 + [1.0] * 5 + spread(34, 0.0, 0.5),  # score 0.15, a weak agent
}
SPLIT = [0.0] * 30 + [1.0] * 30  # every task always or never solved, score 0.5
HALF = [0.5] * 60  # every task solved half of the time, score 0.5

# Trials per task for the imputation path: 5 on some tasks, 1 on the others.
MIXED_ATTEMPTS = {
    "half": [5 if i % 2 == 0 else 1 for i in range(89)],  # 45 tasks with 5 trials, 44 with 1
    "few": [5 if i % 10 == 0 else 1 for i in range(89)],  # 9 tasks with 5 trials, 80 with 1
}


def mc_se(p: float, reps: int) -> float:
    """Monte Carlo standard error of a rate ``p`` estimated from ``reps`` draws."""
    return math.sqrt(p * (1.0 - p) / reps)


def draw(rng: random.Random, ps: list[float], k: int | list[int]) -> dict[str, list[int]]:
    """One arm: ``k`` trials per task (an int, or one count per task)."""
    ks = k if isinstance(k, list) else [k] * len(ps)
    return {
        f"t{i:03d}": [1 if rng.random() < p else 0 for _ in range(n)]
        for i, (p, n) in enumerate(zip(ps, ks, strict=True))
    }


def shifted(ps: list[float], theta: float) -> list[float]:
    """Candidate profile: tasks that are sometimes solved gain ``theta``, capped at 1."""
    return [p if p in (0.0, 1.0) else min(1.0, p + theta) for p in ps]


def alternating(ps: list[float], theta: float) -> list[float]:
    """Sometimes-solved tasks move up and down by ``theta`` in turn; the mean stays the same."""
    out, sign = list(ps), 1
    for i, p in enumerate(ps):
        if 0.0 < p < 1.0:
            out[i] = min(1.0, max(0.0, p + sign * theta))
            sign = -sign
    return out


def true_noise(pa: list[float], pb: list[float], k: int) -> tuple[float, float]:
    """(standard error, Satterthwaite degrees of freedom) of the difference, from the true rates."""
    w = [x * (1 - x) / k for x in (*pa, *pb)]
    return math.sqrt(sum(w)) / len(pa), sum(w) ** 2 / sum(v * v / (k - 1) for v in w)


def theta_at_mde(pa: list[float], k: int) -> float:
    """Shift whose true difference equals (t_0.975 + z_0.8) times its true standard error."""
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        pb = shifted(pa, mid)
        se, df = true_noise(pa, pb, k)
        mde = (stats.t_quantile(0.975, df) + Z80) * se
        lo, hi = (mid, hi) if fmean(pb) - fmean(pa) < mde else (lo, mid)
    return (lo + hi) / 2.0


def rejects(p: float | None) -> bool:
    """A missing p-value (no noise estimate) is not a rejection: the gate says undecided."""
    return p is not None and p <= ALPHA


# --------------------------------------------------------------------------- #
# Simulations
# --------------------------------------------------------------------------- #

TESTS = ("rerun", "t", "permutation", "tasks", "signflip")  # rerun_p(), p_rerun, p_permutation, ...


def comparison_rates(pa, pb, ka, kb, reps: int, seed: int) -> tuple[dict[str, float], Counter]:
    """Rejection rate at ALPHA of each test, and how often each test decides ``rerun_p()``."""
    rng = random.Random(seed)
    hits = dict.fromkeys(TESTS, 0)
    used: Counter = Counter()
    for _ in range(reps):
        c = stats.compare(draw(rng, pa, ka), draw(rng, pb, kb))
        used[c.rerun_test] += 1
        for name, p in zip(
            TESTS, (c.rerun_p(), c.p_rerun, c.p_permutation, c.p_tasks, c.p_signflip), strict=True
        ):
            hits[name] += rejects(p)
    return {name: n / reps for name, n in hits.items()}, used


def aa_rates(profile: str, k: int | list[int], reps: int, seed: int) -> dict[str, float]:
    """Rejection rates when both arms come from the same profile."""
    ps = PROFILES[profile]
    return comparison_rates(ps, ps, k, k, reps, seed)[0]


def score_coverage(profile: str, k: int | list[int], reps: int, seed: int) -> tuple[float, float]:
    """(share of 95% rerun intervals that hold the true mean, share of runs without an interval)."""
    ps, rng = PROFILES[profile], random.Random(seed)
    truth = fmean(ps)
    inside = missing = 0
    for _ in range(reps):
        ci = stats.score(draw(rng, ps, k)).ci_rerun()
        if ci is None:
            missing += 1
        else:
            inside += ci.low <= truth <= ci.high
    return inside / (reps - missing), missing / reps


def normal_interval_coverage(profile: str, k: int, reps: int, seed: int) -> float:
    """Coverage of score ± 1.96 se_rerun: the normal interval that tidegauge used before the t interval."""
    ps, rng = PROFILES[profile], random.Random(seed)
    truth = fmean(ps)
    inside = counted = 0
    for _ in range(reps):
        s = stats.score(draw(rng, ps, k))
        if s.se_rerun is not None:
            counted += 1
            inside += abs(s.mean - truth) <= Z975 * s.se_rerun
    return inside / counted


def noise_floor_coverage(profile: str, k: int, reps: int, seed: int) -> float:
    """Share of rerun pairs whose gap stays below the noise floor of the first run."""
    ps, rng = PROFILES[profile], random.Random(seed)
    inside = counted = 0
    for _ in range(reps):
        first = stats.score(draw(rng, ps, k))
        floor = first.noise_floor()
        rerun = fmean(fmean(xs) for xs in draw(rng, ps, k).values())
        if floor is not None:
            counted += 1
            inside += abs(first.mean - rerun) < floor
    return inside / counted


def comparison_coverage(
    profile: str, k: int | list[int], reps: int, seed: int, theta: float = 0.15
) -> tuple[float, float]:
    """(share of 95% rerun intervals of the difference that hold the truth, share without one)."""
    pa, rng = PROFILES[profile], random.Random(seed)
    pb = shifted(pa, theta)
    truth = fmean(pb) - fmean(pa)
    inside = missing = 0
    for _ in range(reps):
        ci = stats.compare(draw(rng, pa, k), draw(rng, pb, k)).ci_rerun()
        if ci is None:
            missing += 1
        else:
            inside += ci.low <= truth <= ci.high
    return inside / (reps - missing), missing / reps


def power_at_mde(profile: str, k: int, reps: int, seed: int) -> dict[str, float]:
    """Rejection rates when the true difference equals the detectable effect of the design."""
    pa = PROFILES[profile]
    return comparison_rates(pa, shifted(pa, theta_at_mde(pa, k)), k, k, reps, seed)[0]


# A pilot with 40 trials per task whose success counts are exactly the truth.
PILOT_TRIALS = 40
PLAN_COUNTS_A = [0] * 25 + [40] * 20 + [round(40 * p) for p in spread(44)]
PLAN_COUNTS_B = [c if c in (0, 40) else min(40, c + 6) for c in PLAN_COUNTS_A]


def plan_pilots() -> tuple[dict, dict, list[float], list[float]]:
    pilot_a = {f"t{i:03d}": [1] * c + [0] * (PILOT_TRIALS - c) for i, c in enumerate(PLAN_COUNTS_A)}
    pilot_b = {f"t{i:03d}": [1] * c + [0] * (PILOT_TRIALS - c) for i, c in enumerate(PLAN_COUNTS_B)}
    return (
        pilot_a,
        pilot_b,
        [c / PILOT_TRIALS for c in PLAN_COUNTS_A],
        [c / PILOT_TRIALS for c in PLAN_COUNTS_B],
    )


def plan_check(attempts: int, reps: int, seed: int) -> tuple[float, dict[str, float]]:
    """(power that ``plan`` predicts, simulated rejection rates)."""
    pilot_a, pilot_b, pa, pb = plan_pilots()
    delta = fmean(pb) - fmean(pa)
    (row,) = stats.plan(pilot_a, pilot_b, attempts=(attempts,)).rows
    se = row.mde_rerun / (Z975 + Z80)
    predicted = NORMAL.cdf(delta / se - Z975) + NORMAL.cdf(-delta / se - Z975)
    return predicted, comparison_rates(pa, pb, attempts, attempts, reps, seed)[0]


def optional_stopping_rate(profile: str, first: int, last: int, reps: int, seed: int) -> float:
    """A/A share of runs that ever reach ``rerun_p() <= ALPHA`` when 1 trial per task is added after each look."""
    ps, rng = PROFILES[profile], random.Random(seed)
    hits = 0
    for _ in range(reps):
        a, b = draw(rng, ps, first), draw(rng, ps, first)
        for k in range(first, last + 1):
            if k > first:
                for task, p in zip(sorted(a), ps, strict=True):
                    a[task].append(1 if rng.random() < p else 0)
                    b[task].append(1 if rng.random() < p else 0)
            if rejects(stats.compare(a, b).rerun_p()):
                hits += 1
                break
    return hits / reps


def rank_neighbor_rate(profile: str, k: int, arms: int, reps: int, seed: int) -> tuple[float, float]:
    """Identical arms: (share of runs with a 'separated' neighbor pair, mean separated pairs per run)."""
    ps, rng = PROFILES[profile], random.Random(seed)
    any_hit = total = 0
    for _ in range(reps):
        outcomes = {f"arm{j}": draw(rng, ps, k) for j in range(arms)}
        ranked = stats.rank(outcomes, draws=200, seed=0)
        found = sum(
            rejects(stats.compare(outcomes[lower.name], outcomes[upper.name]).rerun_p())
            for upper, lower in itertools.pairwise(ranked)
        )
        any_hit += found > 0
        total += found
    return any_hit / reps, total / reps


# --------------------------------------------------------------------------- #
# Default tests: small counts, a few seconds in total
# --------------------------------------------------------------------------- #

# Every ci_rerun() call computes a t quantile (about 1.5 ms), so the coverage checks use
# fewer replicates than the rejection checks.
AA_CASES = [("mid30", 2), ("mid30", 5), ("mid89", 2), ("low89", 3)]
AA_REPS = 1200  # Monte Carlo SE of a 5% rate: 0.63 points


def test_aa_rejection_rates():
    rates = {case: aa_rates(*case, AA_REPS, seed=100 + i) for i, case in enumerate(AA_CASES)}
    bound = ALPHA + 3 * mc_se(ALPHA, AA_REPS)  # 0.069
    for (profile, k), r in rates.items():
        # rerun_p() is the exact permutation test here (balanced 0/1 data): 2.5 to 3.7% measured.
        assert 0.01 <= r["rerun"] <= bound, (profile, k, r)
        # The Satterthwaite t-test: 3.5 to 5.1% measured with 20,000 replicates.
        assert 0.015 <= r["t"] <= bound, (profile, k, r)
        assert 0.025 <= r["tasks"] <= bound, (profile, k, r)
        assert 0.008 <= r["signflip"] <= bound, (profile, k, r)
    pooled = fmean(r["rerun"] for r in rates.values())
    assert pooled <= ALPHA + 3 * mc_se(ALPHA, AA_REPS * len(AA_CASES))  # 0.059


COVERAGE_REPS = 1200  # Monte Carlo SE of a 95% rate: 0.63 points


@pytest.mark.parametrize(("profile", "k", "high"), [("mid30", 2, 0.998), ("low89", 5, 0.975)])
def test_score_rerun_interval_coverage(profile, k, high):
    coverage, missing = score_coverage(profile, k, COVERAGE_REPS, seed=200 + k)
    # Measured with 20,000 replicates: 98.8% for mid30 at k = 2 (t with few degrees of
    # freedom is careful), about 95% at k = 5. A normal interval held 91% for mid30 at k = 2.
    assert 0.95 - 3 * mc_se(0.95, COVERAGE_REPS) <= coverage <= high, coverage  # lower 0.931
    assert missing < 0.01, missing


def test_comparison_rerun_interval_coverage():
    coverage, missing = comparison_coverage("mid30", 2, COVERAGE_REPS, seed=302)
    # Measured with 10,000 replicates: 96.9%.
    assert 0.95 - 3 * mc_se(0.95, COVERAGE_REPS) <= coverage <= 0.99, coverage  # lower 0.931
    assert missing == 0.0


@pytest.mark.parametrize(("profile", "k"), [("mid89", 3), ("mid30", 5)])
def test_power_at_the_detectable_effect(profile, k):
    reps = 800  # Monte Carlo SE of an 80% rate: 1.4 points
    r = power_at_mde(profile, k, reps, seed=400 + k)
    # Measured with 10,000 replicates: t-test 78.8% and 79.4%, exact test 75.0% and 73.9%.
    assert abs(r["t"] - 0.79) <= 4 * mc_se(0.79, reps), r  # 0.058
    assert 0.6 <= r["rerun"] < r["t"], r


def test_unequal_trial_counts_use_the_t_test_and_stay_controlled():
    # Mean difference 0, effects of both signs, 8 vs 2 trials: the exact permutation test
    # rejects 16.2% here. rerun_p() answers with the t-test instead (case 2).
    reps = 1200
    r, used = comparison_rates(SPLIT, HALF, 8, 2, reps, seed=700)
    assert used == Counter({"t-test": reps})
    assert r["rerun"] == r["t"]
    assert r["rerun"] <= ALPHA + 3 * mc_se(ALPHA, reps), r  # 0.069


def test_single_trial_tasks_borrow_noise():
    # 45 tasks with 5 trials and 44 with 1, the same in both arms.
    r = aa_rates("mid89", MIXED_ATTEMPTS["half"], AA_REPS, seed=800)
    assert r["t"] <= ALPHA + 3 * mc_se(ALPHA, AA_REPS), r  # 0.069
    assert r["rerun"] <= ALPHA + 3 * mc_se(ALPHA, AA_REPS), r
    coverage, missing = score_coverage("mid89", MIXED_ATTEMPTS["half"], 800, seed=801)
    assert 0.95 - 3 * mc_se(0.95, 800) <= coverage <= 0.975, coverage  # lower 0.927
    assert missing == 0.0
    # 9 tasks with 5 trials lend noise to 80 tasks with 1. Measured with 10,000 replicates: 96.5%.
    # Counting each borrowed term as its own estimate (D8) gave 93.4% on the same runs.
    coverage, missing = score_coverage("mid89", MIXED_ATTEMPTS["few"], 1000, seed=802)
    assert 0.95 - 3 * mc_se(0.95, 1000) <= coverage <= 0.99, coverage  # lower 0.929
    assert missing < 0.01, missing


@pytest.mark.parametrize("attempts", [2, 5])
def test_plan_predicts_the_rejection_rate(attempts):
    reps = 600
    predicted, r = plan_check(attempts, reps, seed=500 + attempts)
    # plan uses normal quantiles; the t-test is a little more careful at k = 2.
    tolerance = 3.5 * mc_se(predicted, reps) + 0.03
    assert abs(r["t"] - predicted) <= tolerance, (predicted, r)


def test_plan_attempts_for_reaches_the_power():
    pilot_a, pilot_b, pa, pb = plan_pilots()
    k = stats.plan(pilot_a, pilot_b).attempts_for(fmean(pb) - fmean(pa))
    assert k is not None
    reps = 600
    r = comparison_rates(pa, pb, k, k, reps, seed=600)[0]
    # Rounding k up gives at least 80% in theory; allow 3 Monte Carlo SE below.
    assert r["t"] >= 0.8 - 3 * mc_se(0.8, reps), (k, r)  # 0.751


# --------------------------------------------------------------------------- #
# Large version, opt-in
# --------------------------------------------------------------------------- #


@slow
@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_aa_rejection_rates_large(profile):
    reps = 20_000  # Monte Carlo SE of a 5% rate: 0.15 points
    bound = ALPHA + 3 * mc_se(ALPHA, reps)  # 0.0546
    for k in (2, 3, 5):
        r = aa_rates(profile, k, reps, seed=1000 + k)
        for name in TESTS:
            assert r[name] <= bound, (k, name, r)


# --------------------------------------------------------------------------- #
# The tables in docs/STATISTICS.md
# --------------------------------------------------------------------------- #


def main() -> None:
    """Print the calibration tables. Seeded: the same numbers come out every run."""
    print("Table 1: balanced arms, 0/1 rewards")
    print(
        "| Profile | k | A/A rerun_p() | A/A t-test | A/A paired t | A/A sign-flip | score 95% "
        "| noise floor 95% | delta 95% | power t-test | power rerun_p() |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    missing_runs = []
    for profile in PROFILES:
        for k in (2, 3, 5):
            aa = aa_rates(profile, k, 20_000, seed=1000 + k)
            cov, missing = score_coverage(profile, k, 20_000, seed=2000 + k)
            floor = noise_floor_coverage(profile, k, 20_000, seed=2500 + k)
            dcov, dmissing = comparison_coverage(profile, k, 10_000, seed=3000 + k)
            pw = power_at_mde(profile, k, 10_000, seed=4000 + k)
            missing_runs.append((profile, k, missing, dmissing))
            print(
                f"| {profile} | {k} | {aa['rerun']:.1%} | {aa['t']:.1%} | {aa['tasks']:.1%} "
                f"| {aa['signflip']:.1%} | {cov:.1%} | {floor:.1%} | {dcov:.1%} | {pw['t']:.1%} | {pw['rerun']:.1%} |",
                flush=True,
            )
    normal = ", ".join(
        f"{profile} {normal_interval_coverage(profile, 2, 20_000, seed=2000 + 2):.1%}" for profile in PROFILES
    )
    print(f"normal 95% interval of 1 arm (score ± 1.96 se_rerun) at k = 2: {normal}")
    worst = max(missing_runs, key=lambda m: m[2])
    print(f"runs without a 1-arm interval: at most {worst[2]:.2%} ({worst[0]}, k = {worst[1]})")
    print(f"comparisons without an interval: at most {max(m[3] for m in missing_runs):.2%}")
    for attempts in (2, 5):
        predicted, r = plan_check(attempts, 10_000, seed=5000 + attempts)
        print(
            f"plan, {attempts} trials per task: predicted {predicted:.1%}, "
            f"t-test {r['t']:.1%}, rerun_p() {r['rerun']:.1%}"
        )
    print("Table 2: mean difference 0 with effects of both signs")
    mid = PROFILES["mid89"]
    for label, pa, pb, ka, kb in [
        ("mid89, +-0.3 in turn, 3 vs 3 trials", mid, alternating(mid, 0.3), 3, 3),
        ("mid89, +-0.3 in turn, 2 vs 8 trials", mid, alternating(mid, 0.3), 2, 8),
        ("always/never vs 50%, 5 vs 5 trials", SPLIT, HALF, 5, 5),
        ("always/never vs 50%, 8 vs 2 trials", SPLIT, HALF, 8, 2),
        ("mid89 +-0.3 in turn (1 trial) vs mid89 (5 trials)", alternating(mid, 0.3), mid, 1, 5),
        ("mid89 (1 trial) vs mid89 +-0.3 in turn (5 trials)", mid, alternating(mid, 0.3), 1, 5),
        ("50% (1 trial) vs always/never (5 trials)", HALF, SPLIT, 1, 5),
    ]:
        r, used = comparison_rates(pa, pb, ka, kb, 5000, seed=6000 + ka * 10 + kb + len(label))
        tests = ", ".join(f"{name} {n / 5000:.0%}" for name, n in sorted(used.items()))
        print(f"| {label} | {r['rerun']:.1%} | {r['permutation']:.1%} | {r['t']:.1%} | {tests} |", flush=True)
    print("Table 3: single-trial tasks that borrow noise (same trial counts in both arms)")
    for name, ks in MIXED_ATTEMPTS.items():
        aa = aa_rates("mid89", ks, 10_000, seed=9000 + len(name))
        cov, _ = score_coverage("mid89", ks, 10_000, seed=9100 + len(name))
        dcov, _ = comparison_coverage("mid89", ks, 10_000, seed=9200 + len(name))
        print(
            f"| {name}: {ks.count(5)} tasks x 5 trials, {ks.count(1)} x 1 | {aa['t']:.1%} | {aa['rerun']:.1%} "
            f"| {cov:.1%} | {dcov:.1%} |",
            flush=True,
        )
    donors = {"d1": [1, 0], "d2": [0, 1], **{f"s{i}": [i % 2] for i in range(20)}}
    print(
        f"2 tasks [1, 0] and [0, 1] lend noise to 20 tasks with 1 trial: df_rerun = {stats.score(donors).df_rerun:.1f}"
    )
    rate = optional_stopping_rate("mid89", 2, 10, 2000, seed=7000)
    print(
        f"A/A, look after each added trial from 2 to 10 trials per task, stop at rerun_p() <= 0.05: {rate:.1%}"
    )
    share, mean = rank_neighbor_rate("mid89", 5, 10, 1000, seed=8000)
    print(
        f"10 identical arms, 5 trials per task: {share:.1%} of runs show a separated neighbor pair ({mean:.2f})"
    )


if __name__ == "__main__":
    main()
