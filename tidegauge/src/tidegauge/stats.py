"""Statistics for benchmarks that run each task several times.

Pure functions, standard library only, no I/O. Every result is deterministic:
exact tests use exact arithmetic, and the Monte Carlo fallbacks use a fixed seed.

The input everywhere is ``outcomes``: a mapping ``task -> list of trial values``
(usually rewards in {0, 1}, but any real number works).

Two different questions get two different error bars, and they must not be mixed:

* **rerun**: the task set is fixed. Would the number change if I ran the same
  thing again? Only the trial-to-trial variance inside each task matters.
* **tasks**: the tasks are a sample of the work I care about. Would the number
  hold on other tasks like these? The spread between tasks matters as well.

docs/STATISTICS.md derives every formula used here.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from statistics import NormalDist, fmean

Outcomes = Mapping[str, Sequence[float]]

_NORMAL = NormalDist()

# Largest lattice (number of grid points) the exact tests will build before
# they fall back to seeded Monte Carlo.
EXACT_LATTICE_LIMIT = 2_000_000
MONTE_CARLO_DRAWS = 20_000


# --------------------------------------------------------------------------- #
# Small numeric helpers
# --------------------------------------------------------------------------- #


def z_quantile(p: float) -> float:
    """Standard normal quantile."""
    return _NORMAL.inv_cdf(p)


def normal_two_sided_p(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2.0))  # erfc keeps precision in the far tail


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Lentz's method)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x)
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t: float, df: float) -> float:
    """Two-sided p-value of Student's t with ``df`` degrees of freedom."""
    if df <= 0 or math.isnan(t):
        return math.nan
    if math.isinf(t):
        return 0.0
    return _betainc(df / 2.0, 0.5, df / (df + t * t))


def t_quantile(p: float, df: float) -> float:
    """Student's t quantile, by bisection on the exact CDF."""
    if df <= 0:
        return math.nan
    if p == 0.5:
        return 0.0
    if p < 0.5:
        return -t_quantile(1.0 - p, df)
    target = 2.0 * (1.0 - p)  # two-sided tail mass for the positive quantile
    lo, hi = 0.0, 1.0
    while t_two_sided_p(hi, df) > target:
        hi *= 2.0
        if hi > 1e9:
            return math.inf
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if t_two_sided_p(mid, df) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def sample_variance(xs: Sequence[float]) -> float | None:
    """Unbiased sample variance. ``None`` when fewer than 2 values exist, exactly 0 when all are equal."""
    n = len(xs)
    if n < 2:
        return None
    if min(xs) == max(xs):
        return 0.0  # the rounded mean can miss the common value and show a spread that is not there
    m = fmean(xs)
    return sum((x - m) ** 2 for x in xs) / (n - 1)


# --------------------------------------------------------------------------- #
# One arm: score and its two error bars
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Interval:
    low: float
    high: float

    def excludes(self, value: float = 0.0) -> bool:
        return self.low > value or self.high < value


@dataclass(frozen=True, slots=True)
class Score:
    """Score of one arm on a set of tasks.

    ``mean`` is the task-macro mean: every task has weight 1, whatever its number
    of trials. ``micro`` is the plain mean over trials. They differ only when
    tasks have different numbers of trials.
    """

    n_tasks: int
    n_trials: int
    mean: float
    micro: float
    se_rerun: float | None  # None: no task has 2 or more trials, or no task shows any trial noise
    df_rerun: float | None  # Satterthwaite degrees of freedom of ``se_rerun``
    se_tasks: float | None  # None: fewer than 2 tasks
    attempts_min: int
    attempts_max: int
    mixed_tasks: int  # tasks whose trials disagree
    imputed_tasks: int  # single-trial tasks that borrowed the mean variance

    def ci_rerun(self, level: float = 0.95) -> Interval | None:
        if self.se_rerun is None or self.df_rerun is None:
            return None
        h = t_quantile(0.5 + level / 2.0, self.df_rerun) * self.se_rerun
        return Interval(self.mean - h, self.mean + h)

    def ci_tasks(self, level: float = 0.95) -> Interval | None:
        if self.se_tasks is None:
            return None
        h = t_quantile(0.5 + level / 2.0, self.n_tasks - 1) * self.se_tasks
        return Interval(self.mean - h, self.mean + h)

    def noise_floor(self, level: float = 0.95) -> float | None:
        """The gap that 2 identical runs stay below ``level`` of the time.

        A difference between 2 runs that is smaller than this is not evidence of a change.
        """
        if self.se_rerun is None or self.df_rerun is None:
            return None
        # The difference of 2 runs with equal noise has twice the degrees of freedom of 1 run.
        return t_quantile(0.5 + level / 2.0, 2.0 * self.df_rerun) * math.sqrt(2.0) * self.se_rerun


def _check(outcomes: Outcomes) -> None:
    for task, xs in outcomes.items():
        if not all(math.isfinite(x) for x in xs):
            raise ValueError(f"task {task!r} has a value that is not a finite number")


@dataclass(frozen=True, slots=True)
class _Noise:
    """Trial noise of one arm."""

    terms: dict[str, float]  # variance of each task mean: s_t^2 / k_t
    pairs: list[tuple[float, float]]  # independent variance estimates and their degrees of freedom
    imputed: int  # single-trial tasks that borrowed the mean variance

    @property
    def total(self) -> float:
        return math.fsum(self.terms.values())


def _noise(outcomes: Outcomes) -> _Noise:
    """Per-task ``s_t^2 / k_t``, the variance of each task mean.

    A task with one trial has no variance estimate of its own. It borrows the
    mean within-task variance of the tasks that have one (the donors). Raises
    ``ValueError`` when no task has 2 or more trials.

    ``pairs`` holds 1 independent estimate per donor. All borrowed terms rest on
    the same donors, so each donor carries its own term plus its share of the
    borrowed ones. The pairs add up to the same total as ``terms``.
    """
    known = {t: v for t, xs in outcomes.items() if (v := sample_variance(xs)) is not None}
    if not known:
        raise ValueError("no task has 2 or more trials")
    borrowed = fmean(known.values())
    terms = {t: known.get(t, borrowed) / len(xs) for t, xs in outcomes.items()}
    lent = sum(1.0 / len(xs) for t, xs in outcomes.items() if t not in known) / len(known)
    pairs = [(v * (1.0 / len(outcomes[t]) + lent), float(len(outcomes[t]) - 1)) for t, v in known.items()]
    return _Noise(terms, pairs, len(outcomes) - len(known))


def _satterthwaite(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Degrees of freedom of a sum of variance terms. ``None`` when every term is 0.

    Each pair is ``(w, d)``: a variance estimate ``w`` with ``d`` degrees of freedom.
    The sum behaves like a chi-square with ``(sum w)^2 / sum(w^2 / d)`` degrees of freedom.
    """
    denominator = math.fsum(w * w / d for w, d in pairs if d > 0)
    if denominator <= 0.0:
        return None
    return math.fsum(w for w, _ in pairs) ** 2 / denominator


def score(outcomes: Outcomes) -> Score:
    """Score one arm."""
    tasks = [t for t, xs in outcomes.items() if len(xs) > 0]
    if not tasks:
        raise ValueError("no trials")
    kept = {t: outcomes[t] for t in tasks}
    _check(kept)
    means = [fmean(outcomes[t]) for t in tasks]
    n_trials = sum(len(outcomes[t]) for t in tasks)
    micro = sum(sum(outcomes[t]) for t in tasks) / n_trials
    n = len(tasks)
    se_rerun = df_rerun = None
    imputed = 0
    try:
        noise = _noise(kept)
        imputed = noise.imputed
        df_rerun = _satterthwaite(noise.pairs)
        if df_rerun is not None:
            se_rerun = math.sqrt(noise.total) / n
    except ValueError:
        pass
    var_tasks = sample_variance(means)
    se_tasks = math.sqrt(var_tasks / n) if var_tasks is not None else None
    attempts = [len(outcomes[t]) for t in tasks]
    mixed = sum(1 for t in tasks if len(set(outcomes[t])) > 1)
    return Score(
        n_tasks=n,
        n_trials=n_trials,
        mean=fmean(means),
        micro=micro,
        se_rerun=se_rerun,
        df_rerun=df_rerun,
        se_tasks=se_tasks,
        attempts_min=min(attempts),
        attempts_max=max(attempts),
        mixed_tasks=mixed,
        imputed_tasks=imputed,
    )


# --------------------------------------------------------------------------- #
# Two arms: paired comparison
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Comparison:
    """Paired comparison of arm B (candidate) against arm A (baseline).

    ``delta`` is ``mean(B) - mean(A)`` over the shared tasks, with every task
    weighted equally.
    """

    n_tasks: int
    mean_a: float
    mean_b: float
    delta: float
    # rerun question: fixed tasks, trial noise only
    se_rerun: float | None  # None: an arm has no task with 2 or more trials, or no trial noise shows
    df_rerun: float | None  # Satterthwaite degrees of freedom of ``se_rerun``
    p_rerun: float | None  # t-test with unpooled within-task variances
    p_permutation: float | None  # stratified permutation test of "no effect on any task"
    permutation_exact: bool
    balanced: bool  # every task has the same number of trials in both arms
    # tasks question: tasks are a sample
    se_tasks: float | None
    p_tasks: float | None  # paired t-test over tasks
    p_signflip: float | None  # sign-flip test over tasks
    signflip_exact: bool
    # per-task picture
    b_better: int
    a_better: int
    ties: int
    b_better_unanimous: int  # B passed every trial and A failed every trial
    a_better_unanimous: int
    between_task_variance: float | None  # variance of the true per-task effects
    imputed_tasks: int
    task_deltas: Mapping[str, float] = field(repr=False, default_factory=dict)

    @property
    def exact_rerun_test(self) -> bool:
        """True when the exact permutation test answers the rerun question.

        The permutation test asks "no effect on any task". With the same number
        of trials per task in both arms it is also safe for "no effect on
        average". With different numbers it is not, and the t-test answers.
        """
        return self.permutation_exact and self.balanced

    @property
    def rerun_test(self) -> str:
        """Name of the test behind ``rerun_p()``."""
        if self.exact_rerun_test:
            return "exact permutation"
        if self.p_rerun is not None:
            return "t-test"
        if self.p_permutation is None:
            return "none"
        return "permutation" if self.balanced else "permutation, unequal trial counts"

    def rerun_p(self) -> float | None:
        """The p-value for the rerun question, from the best test that applies.

        1. The exact permutation test, when both arms have the same number of trials per task.
        2. The t-test, when both arms have a task with 2 or more trials and noise shows in at least 1 arm.
        3. Otherwise the permutation test. It tests only "no effect on any task", and it can
           reject too often when 1 arm hides its noise (1 trial per task) and the effects have both signs.
        """
        if self.exact_rerun_test or self.p_rerun is None:
            return self.p_permutation
        return self.p_rerun

    def tasks_p(self) -> float | None:
        """The p-value for the tasks question: the larger p of the paired t-test and the sign-flip test.

        A tasks difference is resolved only when both tests agree. With few tasks, or with
        differences that take few values, the t-test alone rejects too often.
        """
        if self.p_tasks is None or self.p_signflip is None:
            return None
        return max(self.p_tasks, self.p_signflip)

    def ci_rerun(self, level: float = 0.95) -> Interval | None:
        if self.se_rerun is None or self.df_rerun is None:
            return None
        h = t_quantile(0.5 + level / 2.0, self.df_rerun) * self.se_rerun
        return Interval(self.delta - h, self.delta + h)

    def ci_tasks(self, level: float = 0.95) -> Interval | None:
        if self.se_tasks is None:
            return None
        h = t_quantile(0.5 + level / 2.0, self.n_tasks - 1) * self.se_tasks
        return Interval(self.delta - h, self.delta + h)

    def mde_rerun(self, alpha: float = 0.05, power: float = 0.8) -> float | None:
        """Smallest true difference that the t-test of this design detects with the given power.

        The exact permutation test is a little more careful, so its power at this
        difference is lower by some points.
        """
        if self.se_rerun is None or self.df_rerun is None:
            return None
        return (t_quantile(1.0 - alpha / 2.0, self.df_rerun) + z_quantile(power)) * self.se_rerun

    def mde_tasks(self, alpha: float = 0.05, power: float = 0.8) -> float | None:
        if self.se_tasks is None:
            return None
        return (t_quantile(1.0 - alpha / 2.0, self.n_tasks - 1) + z_quantile(power)) * self.se_tasks


def compare(a: Outcomes, b: Outcomes, *, seed: int = 0) -> Comparison:
    """Compare two arms on the tasks they share.

    Trials of one task are independent between the arms, and the tasks are the
    pairing unit. ``seed`` matters only when an exact test is not possible.
    """
    tasks = sorted(t for t in a if t in b and len(a[t]) > 0 and len(b[t]) > 0)
    if not tasks:
        raise ValueError("the arms share no task")
    n = len(tasks)
    kept_a, kept_b = {t: a[t] for t in tasks}, {t: b[t] for t in tasks}
    _check(kept_a)
    _check(kept_b)
    mean_a_t = {t: fmean(a[t]) for t in tasks}
    mean_b_t = {t: fmean(b[t]) for t in tasks}
    deltas = {t: mean_b_t[t] - mean_a_t[t] for t in tasks}
    delta = fmean(deltas.values())

    # rerun: Var(delta) = (1/T^2) * sum_t (s2_a/k_a + s2_b/k_b), judged against Student's t
    # with Satterthwaite degrees of freedom. No noise in the data means no interval and no p.
    within = se_rerun = df_rerun = p_rerun = None
    imputed = 0
    try:
        noise_a, noise_b = _noise(kept_a), _noise(kept_b)
        imputed = noise_a.imputed + noise_b.imputed
        within = noise_a.total + noise_b.total
        df_rerun = _satterthwaite(noise_a.pairs + noise_b.pairs)
        if df_rerun is not None:
            se_rerun = math.sqrt(within) / n
            p_rerun = t_two_sided_p(delta / se_rerun, df_rerun)
    except ValueError:
        pass

    p_perm, perm_exact = stratified_permutation_p([(a[t], b[t]) for t in tasks], seed=seed)

    # tasks: paired over tasks
    var_d = sample_variance(list(deltas.values()))
    se_tasks = math.sqrt(var_d / n) if var_d is not None else None
    if se_tasks is None:
        p_tasks = None
    elif se_tasks == 0.0:
        p_tasks = 1.0 if delta == 0.0 else 0.0
    else:
        p_tasks = t_two_sided_p(delta / se_tasks, n - 1)
    denominators = [math.lcm(len(a[t]), len(b[t])) for t in tasks]
    p_flip, flip_exact = sign_flip_p(list(deltas.values()), denominators, seed=seed)

    # Variance of the true per-task effects: observed spread minus trial noise.
    between = None
    if var_d is not None and within is not None:
        between = max(0.0, var_d - within / n)

    def unanimous(win: Outcomes, lose: Outcomes, t: str) -> bool:
        return min(win[t]) > max(lose[t]) and len(set(win[t])) == 1 and len(set(lose[t])) == 1

    return Comparison(
        n_tasks=n,
        mean_a=fmean(mean_a_t.values()),
        mean_b=fmean(mean_b_t.values()),
        delta=delta,
        se_rerun=se_rerun,
        df_rerun=df_rerun,
        p_rerun=p_rerun,
        p_permutation=p_perm,
        permutation_exact=perm_exact,
        balanced=all(len(a[t]) == len(b[t]) for t in tasks),
        se_tasks=se_tasks,
        p_tasks=p_tasks,
        p_signflip=p_flip,
        signflip_exact=flip_exact,
        b_better=sum(1 for d in deltas.values() if d > 0),
        a_better=sum(1 for d in deltas.values() if d < 0),
        ties=sum(1 for d in deltas.values() if d == 0),
        b_better_unanimous=sum(1 for t in tasks if unanimous(b, a, t)),
        a_better_unanimous=sum(1 for t in tasks if unanimous(a, b, t)),
        between_task_variance=between,
        imputed_tasks=imputed,
        task_deltas=deltas,
    )


# --------------------------------------------------------------------------- #
# Exact tests on a lattice
# --------------------------------------------------------------------------- #


def _convolve(dist: dict[int, float], step: dict[int, float]) -> dict[int, float]:
    out: dict[int, float] = {}
    for x, px in dist.items():
        for y, py in step.items():
            out[x + y] = out.get(x + y, 0.0) + px * py
    return out


def _tail_p(dist: dict[int, float], observed: int) -> float:
    """Two-sided p-value: mass at values at least as far from zero as observed."""
    if observed == 0:
        return 1.0
    return min(1.0, math.fsum(p for x, p in dist.items() if abs(x) >= abs(observed)))


def stratified_permutation_p(
    strata: Sequence[tuple[Sequence[float], Sequence[float]]], *, seed: int = 0
) -> tuple[float | None, bool]:
    """Permutation test of "the two arms have the same outcome distribution on every task".

    Arm labels are shuffled inside each task. The statistic is the difference of
    task-macro means. For 0/1 outcomes the null distribution is a convolution of
    hypergeometric distributions and the p-value is exact. Other outcomes use
    seeded Monte Carlo. Returns ``(p, exact)``.
    """
    strata = [(list(xa), list(xb)) for xa, xb in strata if len(xa) > 0 and len(xb) > 0]
    if not strata:
        return None, False
    if not all(math.isfinite(v) for xa, xb in strata for v in (*xa, *xb)):
        raise ValueError("a value is not a finite number")
    binary = all(v in (0, 1, 0.0, 1.0) for xa, xb in strata for v in (*xa, *xb))
    if binary:
        # With S successes in a task and X of them landing in arm A,
        # d_t = (S - X)/kb - X/ka and X ~ Hypergeometric(ka + kb, S, ka).
        # Scale by L = lcm of all trial counts so that every d_t is an integer.
        scale = math.lcm(*(k for xa, xb in strata for k in (len(xa), len(xb))))
        points = sum(min(len(xa), len(xb)) * (scale // len(xa) + scale // len(xb)) for xa, xb in strata)
        if points <= EXACT_LATTICE_LIMIT:
            dist: dict[int, float] = {0: 1.0}
            observed = 0
            for xa, xb in strata:
                ka, kb = len(xa), len(xb)
                sa, sb = int(sum(xa)), int(sum(xb))
                total = sa + sb
                wa, wb = scale // ka, scale // kb
                # the statistic is B minus A
                observed += sb * wb - sa * wa
                if total == 0 or total == ka + kb:
                    continue  # no variation: this task always contributes 0
                denom = math.comb(ka + kb, ka)
                step: dict[int, float] = {}
                for x in range(max(0, total - kb), min(ka, total) + 1):
                    prob = math.comb(total, x) * math.comb(ka + kb - total, ka - x) / denom
                    value = (total - x) * wb - x * wa
                    step[value] = step.get(value, 0.0) + prob
                dist = _convolve(dist, step)
            return _tail_p(dist, observed), True

    rng = random.Random(seed)
    # Both sides add with fsum, and the comparison allows a relative error: a relabelling
    # that equals the observed one must count as a hit whatever the size of the values.
    observed_f = abs(math.fsum(fmean(xb) - fmean(xa) for xa, xb in strata))
    tolerance = 1e-9 * max(1.0, observed_f)
    hits = 0
    pooled = [(xa + xb, len(xa)) for xa, xb in strata]
    for _ in range(MONTE_CARLO_DRAWS):
        parts = []
        for values, ka in pooled:
            rng.shuffle(values)
            parts.append(fmean(values[ka:]) - fmean(values[:ka]))
        if abs(math.fsum(parts)) >= observed_f - tolerance:
            hits += 1
    return (hits + 1) / (MONTE_CARLO_DRAWS + 1), False


def sign_flip_p(
    deltas: Sequence[float], denominators: Sequence[int] | None = None, *, seed: int = 0
) -> tuple[float | None, bool]:
    """Sign-flip test of "the per-task differences are symmetric around zero".

    When every difference is a multiple of ``1/denominator`` the null
    distribution is computed exactly. Otherwise seeded Monte Carlo is used.
    Returns ``(p, exact)``.
    """
    if not deltas:
        return None, False
    if not all(math.isfinite(d) for d in deltas):
        raise ValueError("a value is not a finite number")
    values = [d for d in deltas if d != 0]
    if not values:
        return 1.0, True
    if denominators is not None:
        scale = math.lcm(*denominators)
        scaled = [d * scale for d in values]
        if all(abs(s - round(s)) < 1e-6 for s in scaled):
            ints = [round(s) for s in scaled]
            if sum(abs(i) for i in ints) * 2 <= EXACT_LATTICE_LIMIT:
                dist: dict[int, float] = {0: 1.0}
                for i in ints:
                    dist = _convolve(dist, {i: 0.5, -i: 0.5} if i else {0: 1.0})
                return _tail_p(dist, sum(ints)), True
    rng = random.Random(seed)
    observed = abs(math.fsum(values))
    tolerance = 1e-9 * max(1.0, observed)
    hits = 0
    for _ in range(MONTE_CARLO_DRAWS):
        total = math.fsum(v if rng.random() < 0.5 else -v for v in values)
        if abs(total) >= observed - tolerance:
            hits += 1
    return (hits + 1) / (MONTE_CARLO_DRAWS + 1), False


# --------------------------------------------------------------------------- #
# Planning: how much to run
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PlanRow:
    attempts: int  # trials per task, per arm
    trials_per_arm: int
    mde_rerun: float
    mde_tasks: float | None


@dataclass(frozen=True, slots=True)
class Plan:
    """What a two-arm comparison on these tasks can detect, per number of attempts."""

    n_tasks: int
    within_variance: float  # mean over tasks of the trial variance, summed over both arms
    between_task_variance: float | None
    alpha: float
    power: float
    rows: tuple[PlanRow, ...]
    floor_tasks: float | None  # what no number of reruns gets below

    def attempts_for(self, mde: float) -> int | None:
        """Smallest number of attempts per task that detects ``mde`` (rerun question)."""
        if mde <= 0:
            return None
        zsum = z_quantile(1.0 - self.alpha / 2.0) + z_quantile(self.power)
        need = self.within_variance * zsum * zsum / (self.n_tasks * mde * mde)
        return max(1, math.ceil(need - 1e-12))


def plan(
    a: Outcomes,
    b: Outcomes | None = None,
    *,
    alpha: float = 0.05,
    power: float = 0.8,
    attempts: Sequence[int] = (1, 2, 3, 5, 8, 10, 15, 20),
) -> Plan:
    """Plan a paired comparison from pilot data.

    With one arm, the candidate is assumed to be as noisy as the pilot. With two
    arms, the between-task variance of the effect is estimated too, and that
    gives the floor that extra reruns cannot get below.
    """
    if b is None:
        tasks = sorted(t for t in a if len(a[t]) > 0)
        terms = _noise({t: a[t] for t in tasks}).terms
        # terms hold s2/k; multiply back to get s2, and count it for both arms.
        within = 2.0 * fmean(terms[t] * len(a[t]) for t in tasks)
        between = None
    else:
        tasks = sorted(t for t in a if t in b and len(a[t]) > 0 and len(b[t]) > 0)
        if not tasks:
            raise ValueError("the arms share no task")
        terms_a = _noise({t: a[t] for t in tasks}).terms
        terms_b = _noise({t: b[t] for t in tasks}).terms
        within = fmean(terms_a[t] * len(a[t]) + terms_b[t] * len(b[t]) for t in tasks)
        between = compare(a, b).between_task_variance
    n = len(tasks)
    z_alpha, z_power = z_quantile(1.0 - alpha / 2.0), z_quantile(power)
    rows = []
    for k in attempts:
        se_rerun = math.sqrt(within / (n * k))
        mde_tasks = None
        if between is not None and n > 1:
            se_tasks = math.sqrt((between + within / k) / n)
            mde_tasks = (t_quantile(1.0 - alpha / 2.0, n - 1) + z_power) * se_tasks
        rows.append(PlanRow(k, n * k, (z_alpha + z_power) * se_rerun, mde_tasks))
    floor = None
    if between is not None and n > 1:
        floor = (t_quantile(1.0 - alpha / 2.0, n - 1) + z_power) * math.sqrt(between / n)
    return Plan(n, within, between, alpha, power, tuple(rows), floor)


# --------------------------------------------------------------------------- #
# Many arms: which ranks would survive a rerun
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Ranked:
    name: str
    score: Score
    rank: int
    rank_low: int
    rank_high: int
    p_first: float | None  # None: no arm has a noise estimate, so the ranks cannot be simulated
    noise_borrowed: bool  # the arm has no trial noise of its own and uses that of the other arms


def rank(
    arms: Mapping[str, Outcomes], *, level: float = 0.95, draws: int = 10_000, seed: int = 0
) -> list[Ranked]:
    """Rank arms and give each rank an interval for the rerun question.

    Every draw adds independent normal rerun noise to each score and ranks
    again. The interval holds the central ``level`` of the simulated ranks.
    An arm without a noise estimate of its own (1 trial per task, or no task
    with mixed outcomes) borrows the mean trial variance of the other arms.
    When no arm has a noise estimate, every interval is the full range.
    """
    scores = {name: score(o) for name, o in arms.items()}
    names = sorted(scores, key=lambda n: (-scores[n].mean, n))

    def inverse_attempts(name: str) -> float:
        return sum(1.0 / len(xs) for xs in arms[name].values() if len(xs) > 0)

    # Mean trial variance of each arm that can estimate it: se^2 = v * sum(1/k) / T^2.
    variances = [
        (s.se_rerun * s.n_tasks) ** 2 / inverse_attempts(name)
        for name, s in scores.items()
        if s.se_rerun is not None
    ]
    if not variances:
        return [
            Ranked(n, scores[n], position, 1, len(names), None, True)
            for position, n in enumerate(names, start=1)
        ]
    noise: dict[str, float] = {}
    for name, s in scores.items():
        if s.se_rerun is not None:
            noise[name] = s.se_rerun
        else:
            noise[name] = math.sqrt(fmean(variances) * inverse_attempts(name)) / s.n_tasks
    rng = random.Random(seed)
    seen: dict[str, list[int]] = {n: [0] * (len(names) + 1) for n in names}
    for _ in range(draws):
        sim = {n: rng.gauss(scores[n].mean, noise[n]) for n in names}
        for position, n in enumerate(sorted(names, key=lambda n: (-sim[n], n)), start=1):
            seen[n][position] += 1
    tail = (1.0 - level) / 2.0
    out = []
    for position, n in enumerate(names, start=1):
        low = high = position
        cumulative = 0
        found_low = False
        for r in range(1, len(names) + 1):
            cumulative += seen[n][r]
            if not found_low and cumulative > tail * draws:
                low, found_low = r, True
            if cumulative >= (1.0 - tail) * draws:
                high = r
                break
        out.append(Ranked(n, scores[n], position, low, high, seen[n][1] / draws, scores[n].se_rerun is None))
    return out
