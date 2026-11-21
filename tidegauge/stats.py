from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from statistics import NormalDist, fmean

Outcomes = Mapping[str, Sequence[float]]

_NORMAL = NormalDist()

EXACT_LATTICE_LIMIT = 2_000_000
MONTE_CARLO_DRAWS = 20_000


def z_quantile(p: float) -> float:
    return _NORMAL.inv_cdf(p)


def normal_two_sided_p(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2.0))


def _betacf(a: float, b: float, x: float) -> float:
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
    if df <= 0 or math.isnan(t):
        return math.nan
    if math.isinf(t):
        return 0.0
    return _betainc(df / 2.0, 0.5, df / (df + t * t))


def t_quantile(p: float, df: float) -> float:
    if df <= 0:
        return math.nan
    if p == 0.5:
        return 0.0
    sign = 1.0
    if p < 0.5:
        p = 1.0 - p
        sign = -1.0
    target = 2.0 * (1.0 - p)
    lo, hi = 0.0, 1.0
    for _ in range(30):
        if t_two_sided_p(hi, df) > target:
            hi *= 2.0
        else:
            break
    else:
        return sign * math.inf
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if t_two_sided_p(mid, df) > target:
            lo = mid
        else:
            hi = mid
    assert 0 <= lo <= hi <= 1e9
    return sign * ((lo + hi) / 2.0)


def sample_variance(xs: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    if min(xs) == max(xs):
        return 0.0
    m = fmean(xs)
    return sum((x - m) ** 2 for x in xs) / (n - 1)


@dataclass(frozen=True, slots=True)
class Interval:
    low: float
    high: float

    def excludes(self, value: float = 0.0) -> bool:
        return self.low > value or self.high < value


def _symmetric_interval(center: float, half_width: float) -> Interval:
    low, high = center - half_width, center + half_width
    if half_width >= 0 and math.isfinite(center):
        assert low <= center <= high
    return Interval(low, high)


@dataclass(frozen=True, slots=True)
class Score:
    n_tasks: int
    n_trials: int
    mean: float
    micro: float
    se_rerun: float | None
    df_rerun: float | None
    se_tasks: float | None
    attempts_min: int
    attempts_max: int
    mixed_tasks: int
    imputed_tasks: int

    def ci_rerun(self, level: float = 0.95) -> Interval | None:
        if self.se_rerun is None or self.df_rerun is None:
            return None
        h = t_quantile(0.5 + level / 2.0, self.df_rerun) * self.se_rerun
        return _symmetric_interval(self.mean, h)

    def ci_tasks(self, level: float = 0.95) -> Interval | None:
        if self.se_tasks is None:
            return None
        h = t_quantile(0.5 + level / 2.0, self.n_tasks - 1) * self.se_tasks
        return _symmetric_interval(self.mean, h)

    def noise_floor(self, level: float = 0.95) -> float | None:
        if self.se_rerun is None or self.df_rerun is None:
            return None
        return t_quantile(0.5 + level / 2.0, 2.0 * self.df_rerun) * math.sqrt(2.0) * self.se_rerun


def _check(outcomes: Outcomes) -> None:
    for task, xs in outcomes.items():
        if not all(math.isfinite(x) for x in xs):
            raise ValueError(f"task {task!r} has a value that is not a finite number")


@dataclass(frozen=True, slots=True)
class _Noise:
    terms: dict[str, float]
    pairs: list[tuple[float, float]]
    imputed: int

    @property
    def total(self) -> float:
        return math.fsum(self.terms.values())


def _noise(outcomes: Outcomes) -> _Noise:
    known = {t: v for t, xs in outcomes.items() if (v := sample_variance(xs)) is not None}
    if not known:
        raise ValueError("no task has 2 or more trials")
    assert 0 < len(known) <= len(outcomes)
    borrowed = fmean(known.values())
    terms = {t: known.get(t, borrowed) / len(xs) for t, xs in outcomes.items()}
    lent = sum(1.0 / len(xs) for t, xs in outcomes.items() if t not in known) / len(known)
    pairs = [(v * (1.0 / len(outcomes[t]) + lent), float(len(outcomes[t]) - 1)) for t, v in known.items()]
    return _Noise(terms, pairs, len(outcomes) - len(known))


def _satterthwaite(pairs: Sequence[tuple[float, float]]) -> float | None:
    denominator = math.fsum(w * w / d for w, d in pairs if d > 0)
    if denominator <= 0.0:
        return None
    return math.fsum(w for w, _ in pairs) ** 2 / denominator


def score(outcomes: Outcomes) -> Score:
    tasks = [t for t, xs in outcomes.items() if len(xs) > 0]
    if not tasks:
        raise ValueError("no trials")
    kept = {t: outcomes[t] for t in tasks}
    _check(kept)
    means = [fmean(outcomes[t]) for t in tasks]
    n_trials = sum(len(outcomes[t]) for t in tasks)
    micro = sum(sum(outcomes[t]) for t in tasks) / n_trials
    n = len(tasks)
    assert 0 < n <= n_trials
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
    assert 0 <= mixed <= n and 0 <= imputed <= n
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


@dataclass(frozen=True, slots=True)
class Comparison:
    n_tasks: int
    mean_a: float
    mean_b: float
    delta: float
    se_rerun: float | None
    df_rerun: float | None
    p_rerun: float | None
    p_permutation: float | None
    permutation_exact: bool
    balanced: bool
    se_tasks: float | None
    p_tasks: float | None
    p_signflip: float | None
    signflip_exact: bool
    b_better: int
    a_better: int
    ties: int
    b_better_unanimous: int
    a_better_unanimous: int
    between_task_variance: float | None
    imputed_tasks: int
    task_deltas: Mapping[str, float] = field(repr=False, default_factory=dict)

    @property
    def exact_rerun_test(self) -> bool:
        return self.permutation_exact and self.balanced

    @property
    def rerun_test(self) -> str:
        if self.exact_rerun_test:
            return "exact permutation"
        if self.p_rerun is not None:
            return "t-test"
        if self.p_permutation is None:
            return "none"
        return "permutation" if self.balanced else "permutation, unequal trial counts"

    def rerun_p(self) -> float | None:
        if self.exact_rerun_test or self.p_rerun is None:
            return self.p_permutation
        return self.p_rerun

    def tasks_p(self) -> float | None:
        if self.p_tasks is None or self.p_signflip is None:
            return None
        return max(self.p_tasks, self.p_signflip)

    def ci_rerun(self, level: float = 0.95) -> Interval | None:
        if self.se_rerun is None or self.df_rerun is None:
            return None
        h = t_quantile(0.5 + level / 2.0, self.df_rerun) * self.se_rerun
        return _symmetric_interval(self.delta, h)

    def ci_tasks(self, level: float = 0.95) -> Interval | None:
        if self.se_tasks is None:
            return None
        h = t_quantile(0.5 + level / 2.0, self.n_tasks - 1) * self.se_tasks
        return _symmetric_interval(self.delta, h)

    def mde_rerun(self, alpha: float = 0.05, power: float = 0.8) -> float | None:
        if self.se_rerun is None or self.df_rerun is None:
            return None
        return (t_quantile(1.0 - alpha / 2.0, self.df_rerun) + z_quantile(power)) * self.se_rerun

    def mde_tasks(self, alpha: float = 0.05, power: float = 0.8) -> float | None:
        if self.se_tasks is None:
            return None
        return (t_quantile(1.0 - alpha / 2.0, self.n_tasks - 1) + z_quantile(power)) * self.se_tasks


def _comparison_rerun(
    a: Outcomes, b: Outcomes, delta: float, n: int
) -> tuple[float | None, float | None, float | None, float | None, int]:
    assert 0 < n == len(a) == len(b)
    within = se = df = p = None
    imputed = 0
    try:
        noise_a, noise_b = _noise(a), _noise(b)
        imputed = noise_a.imputed + noise_b.imputed
        within = noise_a.total + noise_b.total
        df = _satterthwaite(noise_a.pairs + noise_b.pairs)
        if df is not None:
            se = math.sqrt(within) / n
            p = t_two_sided_p(delta / se, df)
    except ValueError:
        pass
    return within, se, df, p, imputed


def compare(a: Outcomes, b: Outcomes, *, seed: int = 0) -> Comparison:
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

    within, se_rerun, df_rerun, p_rerun, imputed = _comparison_rerun(kept_a, kept_b, delta, n)

    p_perm, perm_exact = stratified_permutation_p([(a[t], b[t]) for t in tasks], seed=seed)

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

    between = None
    if var_d is not None and within is not None:
        between = max(0.0, var_d - within / n)

    def unanimous(win: Outcomes, lose: Outcomes, t: str) -> bool:
        return min(win[t]) > max(lose[t]) and len(set(win[t])) == 1 and len(set(lose[t])) == 1

    comparison = Comparison(
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
    assert comparison.a_better + comparison.b_better + comparison.ties == n
    assert 0 <= comparison.a_better_unanimous + comparison.b_better_unanimous <= n
    return comparison


def _convolve(dist: dict[int, float], step: dict[int, float]) -> dict[int, float]:
    out: dict[int, float] = {}
    for x, px in dist.items():
        for y, py in step.items():
            out[x + y] = out.get(x + y, 0.0) + px * py
    return out


def _tail_p(dist: dict[int, float], observed: int) -> float:
    if observed == 0:
        return 1.0
    p = min(1.0, math.fsum(p for x, p in dist.items() if abs(x) >= abs(observed)))
    assert 0.0 <= p <= 1.0
    return p


def stratified_permutation_p(
    strata: Sequence[tuple[Sequence[float], Sequence[float]]], *, seed: int = 0
) -> tuple[float | None, bool]:
    strata = [(list(xa), list(xb)) for xa, xb in strata if len(xa) > 0 and len(xb) > 0]
    if not strata:
        return None, False
    if not all(math.isfinite(v) for xa, xb in strata for v in (*xa, *xb)):
        raise ValueError("a value is not a finite number")
    binary = all(v in (0, 1, 0.0, 1.0) for xa, xb in strata for v in (*xa, *xb))
    if binary:
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
                observed += sb * wb - sa * wa
                if total == 0 or total == ka + kb:
                    continue
                denom = math.comb(ka + kb, ka)
                step: dict[int, float] = {}
                for x in range(max(0, total - kb), min(ka, total) + 1):
                    prob = math.comb(total, x) * math.comb(ka + kb - total, ka - x) / denom
                    value = (total - x) * wb - x * wa
                    step[value] = step.get(value, 0.0) + prob
                dist = _convolve(dist, step)
            return _tail_p(dist, observed), True

    rng = random.Random(seed)
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
    assert 0 <= hits <= MONTE_CARLO_DRAWS
    return (hits + 1) / (MONTE_CARLO_DRAWS + 1), False


def sign_flip_p(
    deltas: Sequence[float], denominators: Sequence[int] | None = None, *, seed: int = 0
) -> tuple[float | None, bool]:
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
    assert 0 <= hits <= MONTE_CARLO_DRAWS
    return (hits + 1) / (MONTE_CARLO_DRAWS + 1), False


@dataclass(frozen=True, slots=True)
class PlanRow:
    attempts: int
    trials_per_arm: int
    mde_rerun: float
    mde_tasks: float | None


@dataclass(frozen=True, slots=True)
class Plan:
    n_tasks: int
    within_variance: float
    between_task_variance: float | None
    alpha: float
    power: float
    rows: tuple[PlanRow, ...]
    floor_tasks: float | None

    def attempts_for(self, mde: float) -> int | None:
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
    if b is None:
        tasks = sorted(t for t in a if len(a[t]) > 0)
        terms = _noise({t: a[t] for t in tasks}).terms
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
    assert n > 0
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


@dataclass(frozen=True, slots=True)
class Ranked:
    name: str
    score: Score
    rank: int
    rank_low: int
    rank_high: int
    p_first: float | None
    noise_borrowed: bool


def rank(
    arms: Mapping[str, Outcomes], *, level: float = 0.95, draws: int = 10_000, seed: int = 0
) -> list[Ranked]:
    scores = {name: score(o) for name, o in arms.items()}
    names = sorted(scores, key=lambda n: (-scores[n].mean, n))

    def inverse_attempts(name: str) -> float:
        return sum(1.0 / len(xs) for xs in arms[name].values() if len(xs) > 0)

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
