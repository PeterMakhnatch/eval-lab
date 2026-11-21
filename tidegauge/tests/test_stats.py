"""Exact, hand-checkable tests for ``tidegauge.stats``.

Every expected value comes from an independent derivation: a closed form, exact
fractions, numerical integration of the t density, or brute-force enumeration of
every label permutation or sign pattern. No expected value comes from the
function under test.
"""

from __future__ import annotations

import dataclasses
import functools
import itertools
import math
import random
import statistics
from collections import Counter
from fractions import Fraction as F

import pytest

from tidegauge import stats

# Standard normal quantiles (Abramowitz & Stegun, table 26.1, to 9 digits).
Z975 = 1.959963985
Z95 = 1.644853627
Z90 = 1.281551566
Z80 = 0.841621234
Z995 = 2.575829304


# --------------------------------------------------------------------------- #
# Independent references
# --------------------------------------------------------------------------- #


def mean_f(xs):
    return sum(F(x) for x in xs) / len(xs)


def var_f(xs):
    """Unbiased sample variance in exact fractions."""
    m = mean_f(xs)
    return sum((F(x) - m) ** 2 for x in xs) / (len(xs) - 1)


def t_two_sided_ref(t: float, nu: int) -> float:
    """Two-sided Student t p-value, closed form for integer df (Abramowitz & Stegun 26.7.3)."""
    theta = math.atan(abs(t) / math.sqrt(nu))
    s, c = math.sin(theta), math.cos(theta)
    series, coef, j = 0.0, 1.0, 0
    if nu % 2:
        while 2 * j + 1 <= nu - 2:
            series += coef * c ** (2 * j + 1)
            j += 1
            coef *= (2 * j) / (2 * j + 1)
        inside = 2.0 / math.pi * (theta + s * series)
    else:
        while 2 * j <= nu - 2:
            series += coef * c ** (2 * j)
            j += 1
            coef *= (2 * j - 1) / (2 * j)
        inside = s * series
    return 1.0 - inside


def t_quantile_ref(p: float, nu: int) -> float:
    """Upper quantile (p > 0.5) by bisection on the closed form above."""
    lo, hi = 0.0, 1e4
    for _ in range(200):
        mid = (lo + hi) / 2.0
        lo, hi = (mid, hi) if t_two_sided_ref(mid, nu) > 2.0 * (1.0 - p) else (lo, mid)
    return (lo + hi) / 2.0


def t_two_sided_any(t: float, nu: float, steps: int = 4000) -> float:
    """Two-sided Student t p-value for any real df: 1 - 2 * (integral of the density over [0, |t|]).

    Composite Simpson rule. Against scipy it is accurate to about 1e-13 here.
    """
    c = math.exp(math.lgamma((nu + 1) / 2) - math.lgamma(nu / 2)) / math.sqrt(nu * math.pi)

    def density(x: float) -> float:
        return c * (1 + x * x / nu) ** (-(nu + 1) / 2)

    b = abs(t)
    h = b / steps
    odd = math.fsum(density((2 * i - 1) * h) for i in range(1, steps // 2 + 1))
    even = math.fsum(density(2 * i * h) for i in range(1, steps // 2))
    return 1.0 - 2.0 * (density(0.0) + density(b) + 4 * odd + 2 * even) * h / 3.0


@functools.cache
def t_quantile_any(p: float, nu: float) -> float:
    """Upper quantile for any real df, by bisection on the Simpson rule above."""
    lo, hi = 0.0, 100.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        lo, hi = (mid, hi) if t_two_sided_any(mid, nu) > 2.0 * (1.0 - p) else (lo, mid)
    return (lo + hi) / 2.0


def satterthwaite_ref(pairs) -> F:
    """(sum w)^2 / sum(w^2 / d) in exact fractions, for variance terms w with d degrees of freedom."""
    return sum(F(w) for w, _ in pairs) ** 2 / sum(F(w) ** 2 / d for w, d in pairs if w)


def normal_two_sided_tail_ref(z: float) -> float:
    """2 * (1 - Phi(z)) for large z, from the asymptotic series phi(z)/z * (1 - 1/z^2 + 3/z^4 - ...)."""
    phi = math.exp(-z * z / 2) / math.sqrt(2 * math.pi)
    return 2 * phi / z * (1 - 1 / z**2 + 3 / z**4 - 15 / z**6 + 105 / z**8)


def brute_permutation_p(strata) -> F:
    """Deal each task's pooled trials back to the arms in every possible way."""
    observed = sum(mean_f(xb) - mean_f(xa) for xa, xb in strata)
    per_task = []
    for xa, xb in strata:
        pooled = [F(v) for v in (*xa, *xb)]
        counts: Counter = Counter()
        for chosen in itertools.combinations(range(len(pooled)), len(xa)):
            picked = [pooled[i] for i in chosen]
            rest = [pooled[i] for i in range(len(pooled)) if i not in chosen]
            counts[sum(rest) / len(rest) - sum(picked) / len(picked)] += 1
        per_task.append(counts)
    hits = total = 0
    for combo in itertools.product(*(c.items() for c in per_task)):
        weight = math.prod(n for _, n in combo)
        total += weight
        if abs(sum(v for v, _ in combo)) >= abs(observed):
            hits += weight
    return F(hits, total)


def brute_signflip_p(deltas) -> F:
    observed = abs(sum(deltas))
    hits = 0
    for signs in itertools.product((1, -1), repeat=len(deltas)):
        hits += abs(sum(s * d for s, d in zip(signs, deltas, strict=True))) >= observed
    return F(hits, 2 ** len(deltas))


def mc_tolerance(p: float, draws: int = stats.MONTE_CARLO_DRAWS) -> float:
    """4.5 Monte Carlo standard errors, plus the +1 of the (hits + 1) / (draws + 1) rule."""
    return 4.5 * math.sqrt(p * (1.0 - p) / draws) + 1.0 / (draws + 1)


def approx(value, rel=1e-12, abs_=1e-12):
    return pytest.approx(float(value), rel=rel, abs=abs_)


# --------------------------------------------------------------------------- #
# Numeric helpers against published values and closed forms
# --------------------------------------------------------------------------- #


def test_references_match_published_tables():
    # guards the references themselves: Student t table, two-sided 0.05, 3 decimals
    for nu, table in [(1, 12.706), (2, 4.303), (5, 2.571), (10, 2.228), (30, 2.042)]:
        assert abs(t_quantile_ref(0.975, nu) - table) < 5e-4
        assert abs(t_quantile_any(0.975, float(nu)) - table) < 5e-4
    for nu in (1, 3, 10):
        for t in (0.5, 2.0, 6.0):
            assert t_two_sided_any(t, float(nu)) == approx(t_two_sided_ref(t, nu), rel=1e-10)


@pytest.mark.parametrize(
    ("df", "table"),
    [(1, 12.706), (2, 4.303), (3, 3.182), (4, 2.776), (5, 2.571), (10, 2.228), (30, 2.042)],
)
def test_t_quantile_published_table(df, table):
    assert abs(stats.t_quantile(0.975, df) - table) < 5e-4


def test_t_quantile_df_88():
    # R: qt(0.975, 88) = 1.987290 (Terminal-Bench 2.0 has 89 tasks)
    assert abs(stats.t_quantile(0.975, 88) - 1.987290) < 5e-7


@pytest.mark.parametrize("df", [1, 2, 3, 4, 5, 10, 30, 88])
@pytest.mark.parametrize("p", [0.6, 0.9, 0.975, 0.995])
def test_t_quantile_closed_form(df, p):
    assert stats.t_quantile(p, df) == approx(t_quantile_ref(p, df), rel=1e-10)


@pytest.mark.parametrize("df", [1.5, 12 / 7, 507 / 145, 8.977, 42.0])
def test_t_quantile_and_p_for_fractional_df(df):
    # Satterthwaite degrees of freedom are fractions: check against numerical integration.
    assert stats.t_quantile(0.975, df) == approx(t_quantile_any(0.975, df), rel=1e-9)
    assert stats.t_two_sided_p(2.0, df) == approx(t_two_sided_any(2.0, df), rel=1e-9)


def test_t_quantile_df_1_and_2_exact():
    # df = 1 is Cauchy: tan(pi (p - 1/2)). df = 2: a * sqrt(2 / (1 - a^2)), a = 2p - 1.
    for p in (0.6, 0.9, 0.975, 0.995):
        assert stats.t_quantile(p, 1) == approx(math.tan(math.pi * (p - 0.5)), rel=1e-11)
        a = 2.0 * p - 1.0
        assert stats.t_quantile(p, 2) == approx(a * math.sqrt(2.0 / (1.0 - a * a)), rel=1e-11)


def test_t_quantile_symmetry_and_edges():
    assert stats.t_quantile(0.5, 4) == 0.0
    assert stats.t_quantile(0.025, 5) == -stats.t_quantile(0.975, 5)
    assert math.isnan(stats.t_quantile(0.975, 0))
    # large df: close to the normal quantile (t_0.975 with df = 1e5 is 1.959988)
    assert abs(stats.t_quantile(0.975, 100_000) - Z975) < 5e-5


def test_t_two_sided_p_published():
    # Student t, t = 2, df = 10: two-sided p = 0.07339
    assert abs(stats.t_two_sided_p(2.0, 10) - 0.07339) < 5e-6
    assert stats.t_two_sided_p(-2.0, 10) == stats.t_two_sided_p(2.0, 10)


@pytest.mark.parametrize("df", [1, 2, 3, 4, 7, 10, 29, 88])
@pytest.mark.parametrize("t", [0.3, 1.0, 2.0, 3.5, 10.0])
def test_t_two_sided_p_closed_form(df, t):
    assert stats.t_two_sided_p(t, df) == approx(t_two_sided_ref(t, df), rel=1e-9, abs_=1e-12)


def test_t_two_sided_p_small_df_formulas():
    for t in (0.3, 1.0, 2.0, 3.5, 10.0):
        assert stats.t_two_sided_p(t, 1) == approx(1.0 - 2.0 / math.pi * math.atan(t))
        assert stats.t_two_sided_p(t, 2) == approx(1.0 - t / math.sqrt(2.0 + t * t))


def test_t_two_sided_p_edges():
    assert stats.t_two_sided_p(0.0, 5) == 1.0
    assert stats.t_two_sided_p(math.inf, 5) == 0.0
    assert math.isnan(stats.t_two_sided_p(1.0, 0))
    assert math.isnan(stats.t_two_sided_p(math.nan, 5))


@pytest.mark.parametrize("df", [1, 3, 7, 29, 88])
@pytest.mark.parametrize("p", [0.6, 0.9, 0.975, 0.995])
def test_t_round_trip(df, p):
    assert stats.t_two_sided_p(stats.t_quantile(p, df), df) == approx(2.0 * (1.0 - p), rel=1e-9)


def test_z_quantile_published():
    for p, z in [(0.975, Z975), (0.95, Z95), (0.9, Z90), (0.8, Z80), (0.995, Z995)]:
        assert abs(stats.z_quantile(p) - z) < 1e-9
    assert stats.z_quantile(0.5) == 0.0
    assert stats.normal_two_sided_p(Z975) == approx(0.05, rel=1e-8)
    assert stats.normal_two_sided_p(0.0) == 1.0


@pytest.mark.parametrize("z", [10.0, 12.0, 20.0])
def test_normal_two_sided_p_far_tail(z):
    # D6: 2 * (1 - Phi(z)) rounds to 0 beyond z = 8.3. At z = 10 the true value is 1.524e-23.
    assert stats.normal_two_sided_p(z) == approx(normal_two_sided_tail_ref(z), rel=1e-6, abs_=0.0)
    assert stats.normal_two_sided_p(-z) == stats.normal_two_sided_p(z)


def test_sample_variance():
    assert stats.sample_variance([]) is None
    assert stats.sample_variance([3.0]) is None
    assert stats.sample_variance([1, 0]) == 0.5
    assert stats.sample_variance([1, 1, 0, 1, 0]) == approx(var_f([1, 1, 0, 1, 0]))
    assert stats.sample_variance([2.5, 2.5, 2.5]) == 0.0


def test_equal_values_have_a_variance_of_exactly_0():
    # D11: fsum(xs) / n can miss the common value by rounding. Each of these gave about 1e-33, not 0.
    for value, n in ((1 / 3, 25), (0.2, 3), (0.1, 3)):
        assert statistics.fmean([value] * n) != value
        assert stats.sample_variance([value] * n) == 0.0
    # 10 tasks with the trials 0.1, 0.1, 0.1 show no trial noise: no rerun interval (D2), not one of width 0.
    s = stats.score({f"t{i}": [0.1, 0.1, 0.1] for i in range(10)})
    assert (s.se_rerun, s.df_rerun, s.ci_rerun()) == (None, None, None)
    # Every one of 25 tasks gives the difference 1/3: the tasks interval has width 0 exactly.
    c = stats.compare({f"t{i}": [0, 0, 0] for i in range(25)}, {f"t{i}": [1, 0, 0] for i in range(25)})
    assert c.se_tasks == 0.0 and c.ci_tasks().low == c.ci_tasks().high


def test_interval_excludes():
    assert stats.Interval(0.1, 0.3).excludes()
    assert stats.Interval(-0.3, -0.1).excludes()
    assert not stats.Interval(-0.1, 0.1).excludes()
    assert stats.Interval(0.1, 0.3).excludes(0.05)
    assert not stats.Interval(0.1, 0.3).excludes(0.2)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_values_raise(bad):
    # D5: a NaN used to give a "significant" Monte Carlo p-value of 1/20001.
    with pytest.raises(ValueError):
        stats.score({"a": [1.0, bad], "b": [0.0, 1.0]})
    with pytest.raises(ValueError):
        stats.compare({"a": [1.0, 0.0]}, {"a": [bad, 1.0]})
    with pytest.raises(ValueError):
        stats.stratified_permutation_p([([bad, 1.0], [0.0, 1.0])])
    with pytest.raises(ValueError):
        stats.sign_flip_p([bad, 0.5], [2, 2])
    with pytest.raises(ValueError):
        stats.sign_flip_p([bad, 0.5])


# --------------------------------------------------------------------------- #
# score
# --------------------------------------------------------------------------- #


def test_score_macro_and_micro_with_uneven_attempts():
    outcomes = {"a": [1, 1, 1, 0], "b": [0, 0], "c": [1], "d": [1, 0, 0]}
    s = stats.score(outcomes)
    # task means 3/4, 0, 1, 1/3: macro = 25/48; micro = 5 successes / 10 trials
    assert s.mean == approx(F(25, 48))
    assert s.micro == 0.5
    assert (s.n_tasks, s.n_trials, s.attempts_min, s.attempts_max) == (4, 10, 1, 4)
    assert s.mixed_tasks == 2  # a and d
    assert s.imputed_tasks == 1  # c has 1 trial
    # s^2: a = 1/4, b = 0, d = 1/3; c borrows their mean 7/36.
    # sum of s^2/k = 1/16 + 0 + 7/36 + 1/9 = 53/144, so se = sqrt(53)/48.
    assert s.se_rerun == approx(math.sqrt(53) / 48)
    means = [F(3, 4), F(0), F(1), F(1, 3)]
    assert s.se_tasks == approx(math.sqrt(var_f(means) / 4))
    # c borrows (1/4 + 0 + 1/3) / 3 = 7/36. That term rests on the 3 donors, so each donor j carries
    # s_j^2 * (1/k_j + 1/3), where 1/3 = (1/k_c) / (3 donors), with k_j - 1 degrees of freedom:
    # a: 1/4 * (1/4 + 1/3) = 7/48 (3 df), b: 0 (1 df), d: 1/3 * (1/3 + 1/3) = 2/9 (2 df).
    # They add up to 53/144, the same total as the terms.
    pairs = [(F(7, 48), 3), (0, 1), (F(2, 9), 2)]
    assert sum(w for w, _ in pairs) == F(53, 144)
    assert s.df_rerun == approx(satterthwaite_ref(pairs))
    assert s.df_rerun == approx(F(2809, 659))


def test_se_rerun_is_the_terminal_bench_leaderboard_formula():
    outcomes = {
        "t1": [1, 0, 0],
        "t2": [1, 1, 0, 1, 0],
        "t3": [0, 1],
        "t4": [1, 1, 1, 1],
        "t5": [0, 0, 1, 1, 1, 1],
    }
    n = len(outcomes)
    # Terminal-Bench leaderboard: s^2 = (1/n^2) * sum p_i (1 - p_i) / (k_i - 1)
    leaderboard = sum(mean_f(xs) * (1 - mean_f(xs)) / (len(xs) - 1) for xs in outcomes.values()) / n**2
    # sqrt(sum_t s_t^2 / k_t) / T with the unbiased variance (stdlib implementation)
    direct = math.sqrt(sum(statistics.variance(xs) / len(xs) for xs in outcomes.values())) / n
    s = stats.score(outcomes)
    assert s.se_rerun == approx(math.sqrt(leaderboard))
    assert s.se_rerun == approx(direct)
    assert s.imputed_tasks == 0
    terms = [(var_f(xs) / len(xs), len(xs) - 1) for xs in outcomes.values()]
    assert s.df_rerun == approx(satterthwaite_ref(terms))


def test_satterthwaite_degrees_of_freedom_by_hand():
    # 3 tasks with s^2/k = 1/4 and 1 degree of freedom each: (3/4)^2 / (3 * 1/16) = 3
    assert stats.score({"a": [1, 0], "b": [0, 1], "c": [1, 0]}).df_rerun == approx(3)
    # only 1 task shows noise: the degrees of freedom are its own, 4 - 1 = 3
    assert stats.score({"a": [1, 0, 0, 1], "b": [1, 1], "c": [0, 0, 0]}).df_rerun == approx(3)
    # terms 1/4 (s^2 = 1/2, k = 2, 1 df) and 1/16 (s^2 = 1/4, k = 4, 3 df):
    # (5/16)^2 / (1/16 + 1/768) = 75/49
    s = stats.score({"a": [1, 0], "b": [1, 1, 1, 0]})
    assert s.df_rerun == approx(satterthwaite_ref([(F(1, 4), 1), (F(1, 16), 3)]))
    assert s.df_rerun == approx(F(75, 49))


def test_score_single_trial_tasks_borrow_the_mean_variance():
    s = stats.score({"x": [1, 0], "y": [1, 1, 0, 0], "z": [1], "w": [0]})
    # s^2: x = 1/2, y = 1/3, mean 5/12 for z and w. Terms: 1/4 + 1/12 + 5/12 + 5/12 = 7/6.
    assert s.se_rerun == approx(math.sqrt(F(7, 6)) / 4)
    assert s.imputed_tasks == 2
    # D8: z and w do not add independent estimates. Each donor carries its share of them:
    # x: 1/2 * (1/2 + 2/2) = 3/4 (1 df), y: 1/3 * (1/4 + 2/2) = 5/12 (3 df). Sum 7/6.
    # (7/6)^2 / ((3/4)^2 + (5/12)^2 / 3) = 147/67.
    assert s.df_rerun == approx(satterthwaite_ref([(F(3, 4), 1), (F(5, 12), 3)]))
    assert s.df_rerun == approx(F(147, 67))


def test_borrowed_noise_has_the_degrees_of_freedom_of_its_donors():
    # D8: 2 donors with 1 degree of freedom each lend to 20 tasks with 1 trial. The 22 terms rest
    # on 2 estimates, so df_rerun is 2. Each donor carries 1/2 * (1/2 + 20/2) = 21/4.
    s = stats.score({"d1": [1, 0], "d2": [0, 1], **{f"s{i}": [i % 2] for i in range(20)}})
    assert s.se_rerun == approx(math.sqrt(F(21, 2)) / 22)
    assert s.df_rerun == approx(satterthwaite_ref([(F(21, 4), 1), (F(21, 4), 1)]))
    assert s.df_rerun == approx(2)
    assert s.imputed_tasks == 20


def test_score_without_repeats_has_no_rerun_error():
    s = stats.score({"a": [1], "b": [0], "c": [1]})
    assert (s.se_rerun, s.df_rerun) == (None, None)
    assert s.ci_rerun() is None
    assert s.noise_floor() is None
    assert s.imputed_tasks == 0
    # task means 1, 0, 1: variance 1/3, se_tasks = sqrt(1/9) = 1/3
    assert s.mean == approx(F(2, 3))
    assert s.se_tasks == approx(F(1, 3))
    half = t_quantile_ref(0.975, 2) / 3
    ci = s.ci_tasks()
    assert (ci.low, ci.high) == (approx(F(2, 3) - F(half)), approx(F(2, 3) + F(half)))


def test_score_without_any_trial_noise():
    # D2: every variance estimate is 0, so there is no rerun error bar at all (not one of width 0).
    s = stats.score({"a": [1, 1], "b": [0, 0, 0], "c": [1]})
    assert (s.se_rerun, s.df_rerun) == (None, None)
    assert s.ci_rerun() is None
    assert s.noise_floor() is None
    assert s.imputed_tasks == 1


def test_score_intervals_and_noise_floor():
    s = stats.score({"a": [1, 0], "b": [1, 1], "c": [0, 0], "d": [1, 0, 0, 1]})
    # se_rerun = sqrt(1/4 + 0 + 0 + 1/12) / 4 = sqrt(1/3) / 4
    se = math.sqrt(1 / 3) / 4
    assert s.se_rerun == approx(se)
    # terms (1/4, 1 df) and (1/12, 3 df): (1/3)^2 / (1/16 + 1/432) = 12/7 degrees of freedom
    assert s.df_rerun == approx(F(12, 7))
    t = t_quantile_any(0.975, 12 / 7)
    ci = s.ci_rerun()
    assert ci.low == approx(s.mean - t * se, rel=1e-9)
    assert ci.high == approx(s.mean + t * se, rel=1e-9)
    # the difference of 2 runs: sqrt(2) * se, with 2 * 12/7 degrees of freedom
    assert s.noise_floor() == approx(t_quantile_any(0.975, 24 / 7) * math.sqrt(2) * se, rel=1e-9)
    assert s.noise_floor(0.9) == approx(t_quantile_any(0.95, 24 / 7) * math.sqrt(2) * se, rel=1e-9)
    t3 = t_quantile_ref(0.975, 3)
    assert s.ci_tasks().high == approx(s.mean + t3 * s.se_tasks, rel=1e-10)


def test_score_edge_cases():
    with pytest.raises(ValueError):
        stats.score({})
    with pytest.raises(ValueError):
        stats.score({"a": [], "b": []})
    # a task with 0 trials is left out
    assert stats.score({"a": [1, 0], "b": []}) == stats.score({"a": [1, 0]})
    one = stats.score({"a": [1, 0, 1]})
    assert one.n_tasks == 1
    assert one.se_tasks is None
    assert one.ci_tasks() is None
    # identical outcomes everywhere: no rerun error bar, and a tasks error bar of exactly 0
    flat = stats.score({"a": [1, 1, 1], "b": [1, 1, 1]})
    assert (flat.mean, flat.se_rerun, flat.df_rerun, flat.se_tasks, flat.mixed_tasks) == (
        1.0,
        None,
        None,
        0.0,
        0,
    )
    assert flat.ci_rerun() is None


# --------------------------------------------------------------------------- #
# compare
# --------------------------------------------------------------------------- #

A = {"t1": [1, 0], "t2": [0, 0, 0], "t3": [1, 1, 1], "t4": [0, 1, 0, 0], "only_a": [1, 1]}
B = {"t1": [1, 1, 1], "t2": [1, 0], "t3": [1, 0], "t4": [0, 0], "only_b": [0]}
# Terms s^2/k with k - 1 degrees of freedom, task by task, for the shared tasks t1 to t4.
AB_TERMS = [(F(1, 4), 1), (0, 2), (0, 2), (F(1, 16), 3), (0, 2), (F(1, 4), 1), (F(1, 4), 1), (0, 1)]
AB_DF = 507 / 145  # (13/16)^2 / (3/16 + 1/768)


def test_compare_example():
    c = stats.compare(A, B)
    assert c.n_tasks == 4
    # task deltas 1/2, 1/2, -1/2, -1/4
    assert dict(c.task_deltas) == {"t1": 0.5, "t2": 0.5, "t3": -0.5, "t4": -0.25}
    assert (c.mean_a, c.mean_b, c.delta) == (approx(F(7, 16)), approx(F(1, 2)), approx(F(1, 16)))
    # s^2/k: A = 1/4, 0, 0, 1/16; B = 0, 1/4, 1/4, 0. Sum 13/16, se = sqrt(13)/16.
    assert c.se_rerun == approx(math.sqrt(13) / 16)
    assert c.df_rerun == approx(satterthwaite_ref(AB_TERMS))
    assert c.df_rerun == approx(AB_DF)
    # t = (1/16) / (sqrt(13)/16) = 1/sqrt(13), judged against Student's t with 507/145 df
    assert c.p_rerun == approx(t_two_sided_any(1 / math.sqrt(13), AB_DF), rel=1e-9)
    # deltas have variance 17/64: se_tasks = sqrt(17)/16, paired t with 3 df
    assert c.se_tasks == approx(math.sqrt(17) / 16)
    assert c.p_tasks == approx(t_two_sided_ref(1 / math.sqrt(17), 3), rel=1e-9)
    # between = 17/64 - (13/16)/4 = 1/16
    assert c.between_task_variance == approx(F(1, 16))
    assert (c.b_better, c.a_better, c.ties) == (2, 2, 0)
    assert (c.b_better_unanimous, c.a_better_unanimous, c.imputed_tasks) == (0, 0, 0)
    shared = [(A[t], B[t]) for t in ("t1", "t2", "t3", "t4")]
    assert c.permutation_exact
    assert c.p_permutation == approx(brute_permutation_p(shared))
    assert c.signflip_exact
    assert c.p_signflip == approx(brute_signflip_p([F(1, 2), F(1, 2), F(-1, 2), F(-1, 4)]))
    # the trial counts differ on t1, t2, t3 and t4, so the t-test answers the rerun question
    assert not c.balanced
    assert not c.exact_rerun_test
    assert c.rerun_test == "t-test"
    assert c.rerun_p() == c.p_rerun


def test_compare_intervals_and_detectable_effects():
    c = stats.compare(A, B)
    se, se_t = math.sqrt(13) / 16, math.sqrt(17) / 16
    t, t3 = t_quantile_any(0.975, AB_DF), t_quantile_ref(0.975, 3)
    assert c.ci_rerun().low == approx(1 / 16 - t * se, rel=1e-9)
    assert c.ci_rerun().high == approx(1 / 16 + t * se, rel=1e-9)
    assert c.ci_tasks().high == approx(1 / 16 + t3 * se_t, rel=1e-10)
    assert c.mde_rerun() == approx((t + Z80) * se, rel=1e-9)
    assert c.mde_tasks() == approx((t3 + Z80) * se_t, rel=1e-8)
    assert c.mde_rerun(alpha=0.1, power=0.9) == approx((t_quantile_any(0.95, AB_DF) + Z90) * se, rel=1e-9)


def test_compare_counts_wins_losses_ties_and_unanimous():
    a = {"w1": [0, 0], "w2": [0], "l1": [1, 1, 1], "tie": [1, 0], "m": [1, 0, 0], "pass": [1, 1], "fail": [0]}
    b = {
        "w1": [1, 1, 1],
        "w2": [1],
        "l1": [0, 0],
        "tie": [0, 1, 1, 0],
        "m": [1, 1, 0],
        "pass": [1],
        "fail": [0, 0],
    }
    c = stats.compare(a, b)
    assert (c.b_better, c.a_better, c.ties) == (3, 1, 3)
    # "pass" and "fail" agree in every trial: they are ties, not unanimous wins
    assert (c.b_better_unanimous, c.a_better_unanimous) == (2, 1)


def test_compare_with_no_shared_task():
    with pytest.raises(ValueError):
        stats.compare({"a": [1]}, {"b": [1]})
    with pytest.raises(ValueError):
        stats.compare({"a": []}, {"a": [1, 0]})
    with pytest.raises(ValueError):
        stats.compare({}, {})


def test_rerun_p_case_1_exact_permutation():
    # balanced 0/1 data: the exact permutation test decides
    c = stats.compare(PILOT_A, PILOT_B)
    assert c.balanced and c.permutation_exact and c.exact_rerun_test
    assert c.rerun_test == "exact permutation"
    assert c.rerun_p() == c.p_permutation
    assert c.p_permutation == approx(brute_permutation_p([(PILOT_A[t], PILOT_B[t]) for t in sorted(PILOT_A)]))
    assert c.p_rerun is not None and c.p_rerun != c.p_permutation


def test_rerun_p_case_2_t_test():
    # unequal trial counts, noise in both arms: the t-test decides (test_compare_example)
    assert stats.compare(A, B).rerun_test == "t-test"
    # equal trial counts but values other than 0/1: no exact test, so the t-test decides
    a = {"t1": [0.5, 0.25], "t2": [1.0, 0.75]}
    b = {"t1": [1.0, 0.75], "t2": [0.25, 0.5]}
    c = stats.compare(a, b)
    assert c.balanced and not c.permutation_exact and not c.exact_rerun_test
    assert c.rerun_test == "t-test"
    assert c.rerun_p() == c.p_rerun


def test_rerun_p_case_3_permutation_with_unequal_trial_counts():
    # A has no noise estimate of its own (1 trial per task) and the counts differ:
    # no t-test exists, so the permutation test of "no effect on any task" decides.
    a = {"t1": [1], "t2": [0], "t3": [1]}
    b = {"t1": [1, 1, 0], "t2": [0, 1, 1], "t3": [1, 1, 1]}
    c = stats.compare(a, b)
    assert (c.se_rerun, c.df_rerun, c.p_rerun) == (None, None, None)
    assert not c.balanced and c.permutation_exact and not c.exact_rerun_test
    assert c.rerun_test == "permutation, unequal trial counts"
    assert c.rerun_p() == c.p_permutation
    assert c.p_permutation == approx(brute_permutation_p([(a[t], b[t]) for t in ("t1", "t2", "t3")]))


def test_rerun_p_case_3_with_balanced_arms():
    # D9: equal trial counts, rewards other than 0/1, 1 trial per task: no exact test and no t-test.
    # The Monte Carlo permutation test decides, and its name does not claim unequal trial counts.
    a = {"t1": [0.5], "t2": [0.25], "t3": [0.0]}
    b = {"t1": [0.75], "t2": [1.0], "t3": [0.5]}
    c = stats.compare(a, b)
    assert c.balanced and not c.permutation_exact and c.p_rerun is None
    assert c.rerun_test == "permutation"
    assert c.rerun_p() == c.p_permutation
    exact = float(brute_permutation_p([(a[t], b[t]) for t in ("t1", "t2", "t3")]))
    assert abs(c.rerun_p() - exact) < mc_tolerance(exact)


def test_rerun_p_without_any_test():
    c = dataclasses.replace(stats.compare(A, B), p_rerun=None, p_permutation=None, permutation_exact=False)
    assert c.rerun_test == "none"
    assert c.rerun_p() is None


def test_tasks_p_needs_the_t_test_and_the_sign_flip_test():
    # D10: 12 tasks with 1 trial per task. B passes 4 tasks that A fails, and the other 8 are equal.
    a = {f"t{i:02d}": [0] for i in range(12)}
    c = stats.compare(a, {f"t{i:02d}": [int(i < 4)] for i in range(12)})
    # d_t is +1 on 4 tasks and 0 on 8: mean 1/3, variance 8/33, se^2 = 2/99, so t^2 = 5.5 with 11 df.
    assert c.se_tasks == approx(math.sqrt(F(2, 99)))
    assert c.p_tasks == approx(t_two_sided_ref(math.sqrt(5.5), 11), rel=1e-9)
    assert c.p_tasks == pytest.approx(0.0388, abs=5e-5)
    # The sign-flip test sees 4 differences of +1: 2 of the 16 sign patterns reach |4|.
    assert (c.p_signflip, c.signflip_exact) == (approx(brute_signflip_p([1, 1, 1, 1])), True)
    assert c.tasks_p() == c.p_signflip == 0.125
    # 7 differences of +1 and 5 of 0: t^2 = (49/144) / (35/1584) = 15.4. Both tests are below 0.05.
    c = stats.compare(a, {f"t{i:02d}": [int(i < 7)] for i in range(12)})
    assert c.p_tasks == approx(t_two_sided_ref(math.sqrt(15.4), 11), rel=1e-9)
    assert c.p_tasks < c.p_signflip == approx(F(2, 128))
    assert c.tasks_p() == c.p_signflip
    # 1 large difference and 5 small ones (in tenths: 10, 1, 1, 1, 1, 1): the sign-flip p is 2/64, and the
    # t-test is the larger p. Mean 1/4, variance 0.135, se = 0.15, so t = 5/3 with 5 df.
    tenths = {f"t{i}": [1] * 10 if i == 0 else [1] + [0] * 9 for i in range(6)}
    c = stats.compare({f"t{i}": [0] for i in range(6)}, tenths)
    assert (c.p_signflip, c.signflip_exact) == (approx(F(2, 64)), True)
    assert c.tasks_p() == c.p_tasks == approx(t_two_sided_ref(5 / 3, 5), rel=1e-9)
    # No t-test with 1 task: no p for the tasks question.
    assert stats.compare({"t": [0]}, {"t": [1]}).tasks_p() is None


def test_compare_when_only_one_arm_has_repeats():
    a = {"t1": [1, 0], "t2": [1, 1], "t3": [0, 1]}
    b = {"t1": [1], "t2": [0], "t3": [1]}
    c = stats.compare(a, b)
    assert (c.se_rerun, c.df_rerun, c.p_rerun, c.between_task_variance) == (None, None, None, None)
    assert c.ci_rerun() is None
    assert c.mde_rerun() is None
    assert c.imputed_tasks == 0
    # the exact test needs no repeats; with 2 vs 1 trials it is case 3 of rerun_p()
    assert c.permutation_exact
    assert c.p_permutation == approx(brute_permutation_p([(a[t], b[t]) for t in ("t1", "t2", "t3")]))
    assert c.rerun_test == "permutation, unequal trial counts"
    assert c.rerun_p() == c.p_permutation
    # deltas 1/2, -1, 1/2: tasks question still works
    assert c.se_tasks == approx(math.sqrt(var_f([F(1, 2), F(-1), F(1, 2)]) / 3))


def test_compare_single_trial_task_borrows_inside_its_arm():
    c = stats.compare({"t1": [1, 0], "t2": [1, 1]}, {"t1": [1], "t2": [0, 1]})
    # A: 1/4 + 0. B: t2 = 1/4, t1 borrows B's mean variance 1/2 (k = 1). Sum 1, se = 1/2.
    assert c.se_rerun == approx(0.5)
    assert c.imputed_tasks == 1
    # A has no borrower: (1/4, 1) and (0, 1). B's donor t2 carries 1/2 * (1/2 + 1/1) = 3/4 (1 df).
    # 1^2 / (1/16 + 9/16) = 8/5 degrees of freedom.
    assert c.df_rerun == approx(satterthwaite_ref([(F(1, 4), 1), (0, 1), (F(3, 4), 1)]))
    assert c.df_rerun == approx(F(8, 5))


def test_compare_joins_the_borrowed_noise_of_both_arms():
    a = {"t1": [1, 0], "t2": [1], "t3": [0, 0, 1]}
    b = {"t1": [1], "t2": [0, 1], "t3": [1, 1, 0]}
    c = stats.compare(a, b)
    # Each arm: donors with s^2 = 1/2 (k = 2) and 1/3 (k = 3), 1 borrower, so lent = 1/2.
    # Donor pairs: 1/2 * (1/2 + 1/2) = 1/2 (1 df) and 1/3 * (1/3 + 1/2) = 5/18 (2 df).
    # Total 7/9 per arm: the terms 1/4 + 1/9 + 5/12 give the same.
    pairs = [(F(1, 2), 1), (F(5, 18), 2)] * 2
    assert c.se_rerun == approx(math.sqrt(F(14, 9)) / 3)
    assert c.df_rerun == approx(satterthwaite_ref(pairs))
    assert c.df_rerun == approx(F(784, 187))
    assert c.imputed_tasks == 2
    # deltas 1/2, -1/2, 1/3: delta = 1/9; t-test with 784/187 degrees of freedom
    assert c.delta == approx(F(1, 9))
    t = (1 / 9) / (math.sqrt(14 / 9) / 3)
    assert c.p_rerun == approx(t_two_sided_any(t, 784 / 187), rel=1e-9)
    assert c.rerun_test == "t-test"


def test_compare_identical_arms():
    a = {"t1": [1, 0, 1], "t2": [0, 0, 1], "t3": [1, 1, 1]}
    c = stats.compare(a, a)
    assert (c.delta, c.p_rerun, c.p_permutation, c.p_tasks, c.p_signflip) == (0.0, 1.0, 1.0, 1.0, 1.0)
    assert c.ties == 3
    assert c.rerun_test == "exact permutation"


def test_compare_with_identical_outcomes_inside_every_task():
    # D2: every within-task variance estimate is 0. There is no t-test and no rerun interval.
    a = {f"t{i}": [1, 1] for i in range(3)}
    b = {f"t{i}": [0, 0] for i in range(3)}
    c = stats.compare(a, b)
    assert c.delta == -1.0
    assert (c.se_rerun, c.df_rerun, c.p_rerun) == (None, None, None)
    assert c.ci_rerun() is None
    assert c.mde_rerun() is None
    # per task: both successes land in A with probability 1/6, so p = 2 * (1/6)^3
    assert c.p_permutation == approx(F(1, 108))
    assert c.p_permutation == approx(brute_permutation_p([(a[t], b[t]) for t in sorted(a)]))
    assert c.rerun_test == "exact permutation"
    assert c.rerun_p() == c.p_permutation
    assert c.p_signflip == approx(F(2, 8))
    assert (c.se_tasks, c.p_tasks, c.a_better_unanimous) == (0.0, 0.0, 3)
    assert c.between_task_variance == 0.0


def test_compare_without_trial_noise_and_unequal_trial_counts():
    a = {"t1": [1, 1], "t2": [0, 0]}
    b = {"t1": [0, 0, 0], "t2": [0, 0, 0]}
    c = stats.compare(a, b)
    assert (c.se_rerun, c.p_rerun) == (None, None)
    # with no trial noise, all the spread of the deltas -1 and 0 is between tasks: 1/2
    assert c.between_task_variance == approx(F(1, 2))
    assert c.rerun_test == "permutation, unequal trial counts"
    # t1: 2 passes in 5 trials; both land in A with probability 1/C(5, 2) = 1/10
    assert c.rerun_p() == approx(F(1, 10))
    assert c.rerun_p() == approx(brute_permutation_p([(a[t], b[t]) for t in ("t1", "t2")]))


def test_compare_non_binary_uses_seeded_monte_carlo():
    a = {"t1": [0.5, 0.25], "t2": [1.0, 0.75, 0.5], "t3": [0.0, 0.25]}
    b = {"t1": [1.0, 0.75], "t2": [0.25, 0.5], "t3": [0.75, 0.5, 1.0]}
    c1 = stats.compare(a, b, seed=3)
    c2 = stats.compare(a, b, seed=3)
    assert c1 == c2
    assert not c1.permutation_exact
    assert c1.rerun_test == "t-test"
    strata = [(a[t], b[t]) for t in ("t1", "t2", "t3")]
    exact = float(brute_permutation_p(strata))
    assert abs(c1.p_permutation - exact) < mc_tolerance(exact)


# --------------------------------------------------------------------------- #
# stratified_permutation_p
# --------------------------------------------------------------------------- #

PERMUTATION_CASES = {
    "balanced k=2": [([1, 0], [1, 1]), ([0, 0], [1, 0]), ([1, 1], [0, 1]), ([0, 1], [1, 1])],
    "balanced k=3": [([1, 0, 0], [1, 1, 0]), ([0, 0, 0], [1, 0, 1]), ([1, 1, 0], [1, 1, 1])],
    "unbalanced 1 vs 3": [([0], [1, 1, 0]), ([1], [0, 0, 1]), ([0], [1, 1, 1])],
    # trial counts 1, 2, 3, 4, 5: the lattice scale is lcm = 60
    "mixed k": [
        ([1, 0], [1, 1, 0]),
        ([0, 1, 0], [1, 1, 0, 1]),
        ([0], [1, 1, 0, 1, 1]),
        ([1, 1, 0, 0], [0, 1]),
    ],
    "one task": [([0, 0, 0], [1, 1, 1])],
}


@pytest.mark.parametrize("name", sorted(PERMUTATION_CASES))
def test_permutation_exact_matches_brute_force(name):
    strata = PERMUTATION_CASES[name]
    p, exact = stats.stratified_permutation_p(strata)
    assert exact
    assert p == approx(brute_permutation_p(strata))


def test_permutation_hand_value():
    # 3 successes in 6 trials, all in B: probability 1/C(6,3) = 1/20 per tail
    assert stats.stratified_permutation_p([([0, 0, 0], [1, 1, 1])]) == (approx(0.1), True)


def test_permutation_random_small_cases_match_brute_force():
    rng = random.Random(2026)
    for _ in range(40):
        strata = []
        for _ in range(rng.randint(1, 4)):
            ka, kb = rng.randint(1, 4), rng.randint(1, 4)
            strata.append(([rng.randint(0, 1) for _ in range(ka)], [rng.randint(0, 1) for _ in range(kb)]))
        p, exact = stats.stratified_permutation_p(strata)
        assert exact
        assert p == approx(brute_permutation_p(strata))


def test_permutation_ignores_tasks_without_variation():
    strata = PERMUTATION_CASES["mixed k"]
    padded = [*strata, ([1, 1], [1, 1, 1]), ([0], [0, 0]), ([], [1]), ([0, 1], [])]
    assert stats.stratified_permutation_p(padded) == stats.stratified_permutation_p(strata)


def test_permutation_without_strata():
    assert stats.stratified_permutation_p([]) == (None, False)
    assert stats.stratified_permutation_p([([], [1]), ([1], [])]) == (None, False)


NON_BINARY = [([0.5, 0.0, 1.0], [1.0, 0.75]), ([0.25, 0.5], [1.0, 1.0, 0.5]), ([0.0, 0.0], [0.5, 1.0])]


def test_permutation_monte_carlo_for_non_binary_outcomes():
    exact = float(brute_permutation_p(NON_BINARY))
    p, flag = stats.stratified_permutation_p(NON_BINARY, seed=0)
    assert not flag
    assert stats.stratified_permutation_p(NON_BINARY, seed=0) == (p, False)
    assert abs(p - exact) < mc_tolerance(exact)
    other, _ = stats.stratified_permutation_p(NON_BINARY, seed=1)
    assert abs(other - exact) < mc_tolerance(exact)


def test_monte_carlo_counts_ties_whatever_the_size_of_the_values():
    # D1: 4 tasks with 1 trial per arm and values near 50,000. The observed labels and their
    # mirror image are 2 of 2^4 equally likely ways, so the exact p is 2/16. The old code
    # missed both ties by 1 unit in the last place and returned 1/20001.
    strata = [
        ([50588.84832347348], [76117.97095033541]),
        ([33885.158106897194], [45276.44934617021]),
        ([23518.95113842175], [117918.44105079943]),
        ([77953.98455434103], [149464.84851954324]),
    ]
    assert brute_permutation_p(strata) == F(1, 8)
    p, exact = stats.stratified_permutation_p(strata)
    assert not exact
    assert abs(p - 0.125) < mc_tolerance(0.125)
    # the same for the sign-flip test: 3 positive differences, 2 of 8 sign patterns
    deltas = [50000.1, 300000.7, 1200000.3]
    q, exact = stats.sign_flip_p(deltas)
    assert not exact
    assert abs(q - 0.25) < mc_tolerance(0.25)


def test_monte_carlo_p_value_is_never_below_one_over_draws_plus_one():
    # The observed labels are 1 of 20^8 equally likely ways (plus the mirror image), so no
    # random draw reaches them and p = (0 + 1) / (draws + 1). A p-value of 0 would be invalid.
    floor = 1 / (stats.MONTE_CARLO_DRAWS + 1)
    strata = [([0.0, 0.1, 0.2], [10.0, 10.1, 10.2])] * 8
    assert stats.stratified_permutation_p(strata) == (approx(floor), False)
    deltas = [0.31 + i / 1000 for i in range(30)]  # all positive, off the lattice: 2 of 2^30 patterns
    assert stats.sign_flip_p(deltas, [2] * 30) == (approx(floor), False)


def test_permutation_monte_carlo_when_the_lattice_is_too_large(monkeypatch):
    strata = PERMUTATION_CASES["mixed k"]
    exact = float(brute_permutation_p(strata))
    monkeypatch.setattr(stats, "EXACT_LATTICE_LIMIT", 0)
    p, flag = stats.stratified_permutation_p(strata, seed=5)
    assert not flag
    assert stats.stratified_permutation_p(strata, seed=5) == (p, False)
    assert abs(p - exact) < mc_tolerance(exact)


# --------------------------------------------------------------------------- #
# sign_flip_p
# --------------------------------------------------------------------------- #

SIGNFLIP_CASES = [
    ([F(1, 2), F(1, 2), F(-1, 3), F(1, 6)], [2, 2, 3, 6]),
    ([F(1), F(1), F(1), F(-1), F(1, 2)], [1, 1, 1, 1, 2]),
    ([F(2, 3), F(-1, 4), F(1, 12), F(1, 3), F(-1, 2), F(5, 6)], [3, 4, 12, 3, 2, 6]),
    ([F(1, 5)] * 8, [5] * 8),
    ([F(3, 4), F(-2, 3), F(1, 2), F(1, 6), F(-1, 4), F(1, 3), F(1, 12), F(-5, 6), F(1, 2)], [12] * 9),
]


@pytest.mark.parametrize("case", range(len(SIGNFLIP_CASES)))
def test_signflip_exact_matches_brute_force(case):
    deltas, denominators = SIGNFLIP_CASES[case]
    p, exact = stats.sign_flip_p([float(d) for d in deltas], denominators)
    assert exact
    assert p == approx(brute_signflip_p(deltas))


def test_signflip_hand_values():
    # 5 positive differences of 1: only all-plus and all-minus reach |sum| = 5
    assert stats.sign_flip_p([1.0] * 5, [1] * 5) == (approx(2 / 32), True)
    assert stats.sign_flip_p([0.2] * 8, [5] * 8) == (approx(2 / 256), True)


def test_signflip_zero_differences():
    assert stats.sign_flip_p([]) == (None, False)
    assert stats.sign_flip_p([0.0, 0.0], [1, 1]) == (1.0, True)
    # zeros add nothing to the sum under any sign: they drop out
    assert stats.sign_flip_p([0.5, 0.0, 0.0, 0.5], [2, 1, 1, 2]) == stats.sign_flip_p([0.5, 0.5], [2, 2])
    assert stats.sign_flip_p([0.5, 0.5], [2, 2]) == (approx(0.5), True)


def test_signflip_monte_carlo_off_the_lattice():
    values = [0.1234, -0.05, 0.31, 0.2, 0.17, -0.09, 0.4]
    exact = float(brute_signflip_p([F(v) for v in values]))
    p, flag = stats.sign_flip_p(values, [2] * len(values), seed=0)
    assert not flag
    assert stats.sign_flip_p(values, [2] * len(values), seed=0) == (p, False)
    assert abs(p - exact) < mc_tolerance(exact)
    # without denominators the test is always Monte Carlo
    lattice = [1.0, 1.0, 1.0, -0.5, 0.5]
    exact = float(brute_signflip_p([F(v) for v in lattice]))  # 6/32
    q, flag = stats.sign_flip_p(lattice, None)
    assert not flag
    assert abs(q - exact) < mc_tolerance(exact)


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #

PILOT = {"t1": [1, 0], "t2": [1, 1, 0], "t3": [0, 0], "t4": [1]}
# Same number of trials everywhere, and a between-task variance above 0.
PILOT_A = {"t1": [0, 0, 0], "t2": [1, 1, 0], "t3": [1, 1, 1], "t4": [0, 1, 0], "t5": [1, 0, 0]}
PILOT_B = {"t1": [1, 1, 1], "t2": [1, 1, 1], "t3": [1, 1, 0], "t4": [1, 1, 0], "t5": [0, 0, 0]}


def test_plan_one_arm():
    p = stats.plan(PILOT)
    # s^2 = 1/2, 1/3, 0 and t4 borrows 5/18: mean 5/18, doubled for two arms
    within = 10 / 18
    assert p.within_variance == approx(within)
    assert (p.n_tasks, p.alpha, p.power) == (4, 0.05, 0.8)
    assert (p.between_task_variance, p.floor_tasks) == (None, None)
    assert [r.attempts for r in p.rows] == [1, 2, 3, 5, 8, 10, 15, 20]
    for row in p.rows:
        assert row.trials_per_arm == 4 * row.attempts
        assert row.mde_rerun == approx((Z975 + Z80) * math.sqrt(within / (4 * row.attempts)), rel=1e-8)
        assert row.mde_tasks is None


def test_plan_attempts_for():
    p = stats.plan(PILOT)
    zsum = Z975 + Z80

    def smallest(mde):
        return next(k for k in itertools.count(1) if zsum * math.sqrt(p.within_variance / (4 * k)) <= mde)

    for mde in (0.6, 0.3, 0.2, 0.1, 0.05):
        assert p.attempts_for(mde) == smallest(mde)
    # 10/18 * 2.80158522^2 / (4 * 0.1^2) = 109.01, so 110 attempts
    assert p.attempts_for(0.1) == 110
    assert p.attempts_for(5.0) == 1
    assert p.attempts_for(0.0) is None
    assert p.attempts_for(-0.1) is None


def test_plan_two_arms():
    p = stats.plan(PILOT_A, PILOT_B)
    # mean over tasks of s2_a + s2_b: (0 + 1/3 + 1/3 + 2/3 + 1/3) / 5 = 1/3
    assert p.within_variance == approx(F(1, 3))
    # deltas 1, 1/3, -1/3, 1/3, -1/3 have variance 14/45; trial noise of a task mean is 1/9
    assert p.between_task_variance == approx(F(1, 5))
    t4 = t_quantile_ref(0.975, 4)
    assert p.floor_tasks == approx((t4 + Z80) * math.sqrt(0.2 / 5), rel=1e-8)
    for row in p.rows:
        k = row.attempts
        assert row.mde_rerun == approx((Z975 + Z80) * math.sqrt((1 / 3) / (5 * k)), rel=1e-8)
        assert row.mde_tasks == approx((t4 + Z80) * math.sqrt((0.2 + (1 / 3) / k) / 5), rel=1e-8)
        assert row.mde_tasks > p.floor_tasks
    tasks = [r.mde_tasks for r in p.rows]
    assert tasks == sorted(tasks, reverse=True)


def test_plan_agrees_with_compare_at_the_pilot_size():
    p = stats.plan(PILOT_A, PILOT_B)
    c = stats.compare(PILOT_A, PILOT_B)
    row = next(r for r in p.rows if r.attempts == 3)  # the pilot has 3 trials per task
    # plan uses normal quantiles; at the pilot size its standard error is the one of compare
    assert row.mde_rerun / (Z975 + Z80) == approx(c.se_rerun, rel=1e-8)
    # 5 terms of 1/9 with 2 degrees of freedom each: (5/9)^2 / (5/162) = 10
    assert c.df_rerun == approx(10)
    assert c.mde_rerun() == approx((t_quantile_ref(0.975, 10) + Z80) * c.se_rerun, rel=1e-9)
    assert row.mde_tasks == approx(c.mde_tasks(), rel=1e-10)
    assert p.between_task_variance == approx(c.between_task_variance)
    t4 = t_quantile_ref(0.975, 4)
    assert p.floor_tasks == approx((t4 + Z80) * math.sqrt(c.between_task_variance / c.n_tasks), rel=1e-8)


def test_plan_options_and_errors():
    p = stats.plan(PILOT, alpha=0.1, power=0.9, attempts=(4,))
    assert len(p.rows) == 1
    assert p.rows[0].mde_rerun == approx((Z95 + Z90) * math.sqrt((10 / 18) / 16), rel=1e-8)
    with pytest.raises(ValueError):
        stats.plan({"a": [1], "b": [0]})  # no task has 2 trials
    with pytest.raises(ValueError):
        stats.plan({"a": [1, 0]}, {"b": [1, 0]})  # no shared task
    with pytest.raises(ValueError):
        stats.plan({})


# --------------------------------------------------------------------------- #
# rank
# --------------------------------------------------------------------------- #


def test_rank_order_and_ties_by_name():
    arms = {
        "b": {"t1": [1, 0], "t2": [0, 0]},
        "a": {"t1": [1, 0], "t2": [0, 0]},
        "c": {"t1": [1, 1], "t2": [1, 0]},
    }
    ranked = stats.rank(arms, draws=500)
    assert [(r.name, r.rank) for r in ranked] == [("c", 1), ("a", 2), ("b", 3)]
    assert ranked[0].score == stats.score(arms["c"])


def test_rank_is_deterministic_for_a_seed():
    arms = {f"arm{i}": {f"t{j}": [(i + j) % 2, (i * j) % 3 == 0, j % 2] for j in range(6)} for i in range(4)}
    assert stats.rank(arms, seed=7, draws=2000) == stats.rank(arms, seed=7, draws=2000)


def test_rank_separated_arm_and_identical_arms():
    tasks = range(10)
    top = {f"t{j}": [1, 1, 1] if j else [1, 1, 0] for j in tasks}
    same = {f"t{j}": [1, 0, 0] if j % 2 else [0, 1, 1] for j in tasks}
    arms = {"top": top, "x1": same, "x2": same, "x3": same}
    ranked = {r.name: r for r in stats.rank(arms, draws=4000)}
    assert (ranked["top"].rank, ranked["top"].rank_low, ranked["top"].rank_high) == (1, 1, 1)
    assert ranked["top"].p_first == 1.0
    for name in ("x1", "x2", "x3"):
        assert (ranked[name].rank_low, ranked[name].rank_high) == (2, 4)
        assert ranked[name].p_first == 0.0
    # three identical arms share ranks 2 to 4 about equally
    level50 = {r.name: r for r in stats.rank({"x1": same, "x2": same, "x3": same}, draws=4000)}
    for r in level50.values():
        assert (r.rank_low, r.rank_high) == (1, 3)
        assert abs(r.p_first - 1 / 3) < 4.5 * math.sqrt(2 / 9 / 4000)


def test_rank_borrowed_noise():
    repeat = {"t1": [1, 0], "t2": [1, 0], "t3": [1, 1], "t4": [0, 0]}  # mean 1/2, se^2 = 1/32
    single = {"t1": [1], "t2": [1], "t3": [0], "t4": [1]}  # mean 3/4, no noise of its own
    # mean trial variance of "repeat": (1/2) / (sum of 1/k = 2) = 1/4.
    # "single" borrows it: noise^2 = (1/4) * 4 / 4^2 = 1/16.
    draws = 20_000
    ranked = {r.name: r for r in stats.rank({"repeat": repeat, "single": single}, draws=draws)}
    assert ranked["single"].noise_borrowed
    assert not ranked["repeat"].noise_borrowed
    expected = statistics.NormalDist().cdf((3 / 4 - 1 / 2) / math.sqrt(1 / 16 + 1 / 32))  # 0.7929
    assert abs(ranked["single"].p_first - expected) < 4.5 * math.sqrt(expected * (1 - expected) / draws)
    assert ranked["single"].p_first + ranked["repeat"].p_first == approx(1.0)


def test_rank_arm_without_trial_noise_borrows():
    repeat = {"t1": [1, 0], "t2": [1, 0], "t3": [1, 1], "t4": [0, 0]}  # mean 1/2, se^2 = 1/32
    flat = {"t1": [1, 1], "t2": [1, 1], "t3": [1, 1], "t4": [0, 0]}  # mean 3/4, every trial agrees
    # "flat" borrows the mean trial variance 1/4 with its own sum of 1/k = 2: noise^2 = 1/4 * 2 / 16 = 1/32
    draws = 20_000
    ranked = {r.name: r for r in stats.rank({"repeat": repeat, "flat": flat}, draws=draws)}
    assert ranked["flat"].noise_borrowed
    expected = statistics.NormalDist().cdf((3 / 4 - 1 / 2) / math.sqrt(1 / 32 + 1 / 32))  # Phi(1) = 0.8413
    assert abs(ranked["flat"].p_first - expected) < 4.5 * math.sqrt(expected * (1 - expected) / draws)


def test_rank_without_any_noise_estimate():
    # D7: no arm has a noise estimate, so the ranks cannot be simulated: every interval is the
    # full range 1 to N and p_first is None.
    arms = {"a": {"t1": [1], "t2": [1]}, "b": {"t1": [1], "t2": [0]}, "c": {"t1": [0], "t2": [0]}}
    ranked = stats.rank(arms, draws=100)
    assert [(r.name, r.rank, r.rank_low, r.rank_high, r.p_first) for r in ranked] == [
        ("a", 1, 1, 3, None),
        ("b", 2, 1, 3, None),
        ("c", 3, 1, 3, None),
    ]
    assert all(r.noise_borrowed for r in ranked)


def test_rank_edge_cases():
    assert stats.rank({}) == []
    with pytest.raises(ValueError):
        stats.rank({"a": {"t1": []}})


# --------------------------------------------------------------------------- #
# The worked example in docs/STATISTICS.md
# --------------------------------------------------------------------------- #

DOC_A = {"t1": [1, 0], "t2": [0, 0], "t3": [1, 1]}
DOC_B = {"t1": [1, 1], "t2": [1, 0], "t3": [1, 1]}
T_975_1 = math.tan(0.475 * math.pi)  # 12.706: df = 1 is the Cauchy distribution
T_975_2 = 0.95 * math.sqrt(2 / (1 - 0.95**2))  # 4.303: df = 2 closed form


def test_docs_worked_example():
    sa, sb = stats.score(DOC_A), stats.score(DOC_B)
    assert (sa.mean, sa.se_rerun, sa.df_rerun, sa.se_tasks) == (
        0.5,
        approx(F(1, 6)),
        1.0,
        approx(math.sqrt(1 / 12)),
    )
    assert (sb.mean, sb.se_rerun, sb.df_rerun) == (approx(F(5, 6)), approx(F(1, 6)), 1.0)
    assert sa.ci_rerun().high == approx(0.5 + T_975_1 / 6, rel=1e-10)  # 2.618
    assert sa.noise_floor() == approx(T_975_2 * math.sqrt(2) / 6, rel=1e-10)  # 1.014
    c = stats.compare(DOC_A, DOC_B)
    assert c.delta == approx(F(1, 3))
    assert c.se_rerun == approx(math.sqrt(0.5) / 3)
    assert c.df_rerun == approx(2)  # (1/4 + 1/4)^2 / (1/16 + 1/16)
    assert c.p_rerun == approx(1 - math.sqrt(2) / 2)  # t = sqrt(2) with 2 df: 0.2929
    assert c.ci_rerun().low == approx(1 / 3 - T_975_2 * math.sqrt(0.5) / 3, rel=1e-10)  # -0.681
    assert c.mde_rerun() == approx((T_975_2 + Z80) * math.sqrt(0.5) / 3, rel=1e-8)  # 1.213
    assert c.rerun_test == "exact permutation"
    assert (c.rerun_p(), c.p_permutation, c.p_signflip) == (approx(0.5), approx(0.5), approx(0.5))
    assert c.se_tasks == approx(F(1, 6))
    assert c.p_tasks == approx(1 - 2 / math.sqrt(6))  # t = 2 with 2 df: 0.1835
    assert c.between_task_variance == 0.0
    p = stats.plan(DOC_A, DOC_B)
    assert p.within_variance == approx(F(1, 3))
    assert p.attempts_for(0.10) == 88
    assert p.floor_tasks == 0.0
