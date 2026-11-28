from __future__ import annotations

import math
import random
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ._decompose_perm import (
    _ZERO_TOL,
    EXACT_ENUM_MAX_N,
    MONTE_CARLO_DRAWS,
    _enum_sign_flip,
    _lattice_sign_flip,
    _safe_fmean,
    _to_finite_float,
    exact_mcnemar_p,
    stratified_permutation_p,
)

Outcomes = Mapping[str, Sequence[float]]


def _validate_probability_param(name: str, value: float) -> float:
    val = _to_finite_float(value, name)
    if not (0.0 < val < 1.0):
        raise ValueError(f"{name} must be strictly between 0 and 1, got {value!r}")
    return val


def _validate_outcomes(outcomes: Outcomes, tasks: Sequence[str]) -> None:
    for t in tasks:
        for x in outcomes[t]:
            _to_finite_float(x, f"outcome for task {t!r}")


def _betacf(a: float, b: float, x: float) -> float:
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 / ((1.0 + aa * d) if abs(1.0 + aa * d) > tiny else tiny)
        c = (1.0 + aa / c) if abs(1.0 + aa / c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 / ((1.0 + aa * d) if abs(1.0 + aa * d) > tiny else tiny)
        c = (1.0 + aa / c) if abs(1.0 + aa / c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h


def _betainc_pair(a: float, b: float, x: float, y: float) -> tuple[float, float]:
    if x <= 0.0 or y <= 0.0:
        return (0.0, 1.0) if x <= 0.0 else (1.0, 0.0)
    log_x = math.log1p(-y) if y < 0.5 else math.log(x)
    log_y = math.log1p(-x) if x < 0.5 else math.log(y)
    base = math.lgamma(a + b) + a * log_x + b * log_y
    if x < (a + 1.0) / (a + b + 2.0):
        cf = _betacf(a, b, x)
        if cf <= 0.0:
            return 0.0, 1.0
        log_t = base - math.lgamma(a + 1.0) - math.lgamma(b) + math.log(cf)
        if log_t >= 0.0:
            return 1.0, 0.0
        return min(1.0, max(0.0, math.exp(log_t))), min(1.0, max(0.0, -math.expm1(log_t)))
    cf = _betacf(b, a, y)
    if cf <= 0.0:
        return 1.0, 0.0
    log_c = base - math.lgamma(a) - math.lgamma(b + 1.0) + math.log(cf)
    if log_c >= 0.0:
        return 0.0, 1.0
    return min(1.0, max(0.0, -math.expm1(log_c))), min(1.0, max(0.0, math.exp(log_c)))


def _t_central_p(at: float, df: float) -> float:
    if at <= 0.0:
        return 0.0
    if math.isinf(at):
        return 1.0
    if math.isinf(df) or df >= 1e7:
        return math.erf(at / math.sqrt(2.0))
    a = df / 2.0
    if a <= 0.0:
        return 0.0
    root_df = math.sqrt(df)
    if at > 1e10 * root_df:
        log_p = (
            math.lgamma(a + 0.5)
            - math.lgamma(a + 1.0)
            - math.lgamma(0.5)
            + a * (math.log(df) - 2.0 * math.log(at))
        )
        return 0.0 if log_p >= 0.0 else min(1.0, max(0.0, -math.expm1(log_p)))
    if at < 1e-10 * root_df:
        log_c = (
            math.lgamma(a + 0.5)
            - math.lgamma(a)
            - math.lgamma(0.5)
            + math.log(2.0)
            + math.log(at)
            - 0.5 * math.log(df)
        )
        return 1.0 if log_c >= 0.0 else min(1.0, max(0.0, math.exp(log_c)))
    h = math.hypot(root_df, at)
    s = root_df / h
    u = at / h
    return _betainc_pair(a, 0.5, s * s, u * u)[1]


def t_two_sided_p(t_stat: float, df: float) -> float:
    if math.isnan(df) or df <= 0.0 or math.isnan(t_stat):
        return math.nan
    if math.isinf(t_stat):
        return 0.0
    if math.isinf(df) or df >= 1e7:
        return math.erfc(abs(t_stat) / math.sqrt(2.0))
    a = df / 2.0
    if a <= 0.0:
        return 1.0
    at = abs(t_stat)
    root_df = math.sqrt(df)
    if at > 1e10 * root_df:
        log_x = math.log(df) - 2.0 * math.log(at)
        log_p = math.lgamma(a + 0.5) - math.lgamma(a + 1.0) - math.lgamma(0.5) + a * log_x
        return 1.0 if log_p >= 0.0 else min(1.0, max(0.0, math.exp(log_p)))
    h = math.hypot(root_df, at)
    s = root_df / h
    u = at / h
    return _betainc_pair(a, 0.5, s * s, u * u)[0]


def t_quantile(p: float, df: float) -> float:
    if math.isnan(p) or math.isnan(df) or df <= 0.0 or not (0.0 <= p <= 1.0):
        return math.nan
    if p == 0.0:
        return -math.inf
    if p == 1.0:
        return math.inf
    if p == 0.5:
        return 0.0
    sign = -1.0 if p < 0.5 else 1.0
    lo, hi = 0.0, 1.0
    if abs(p - 0.5) < 0.25:
        target_c = 2.0 * abs(p - 0.5)
        for _ in range(1100):
            if _t_central_p(hi, df) >= target_c:
                break
            lo, hi = hi, hi * 2.0
            if hi > 1e300:
                return sign * math.inf
        for _ in range(200):
            mid = lo + (hi - lo) / 2.0
            lo, hi = (mid, hi) if _t_central_p(mid, df) < target_c else (lo, mid)
        return sign * (lo + (hi - lo) / 2.0)
    target = 2.0 * p if p < 0.5 else 2.0 * (1.0 - p)
    for _ in range(1100):
        if t_two_sided_p(hi, df) <= target:
            break
        lo, hi = hi, hi * 2.0
        if hi > 1e300:
            return sign * math.inf
    for _ in range(200):
        mid = lo + (hi - lo) / 2.0
        lo, hi = (mid, hi) if t_two_sided_p(mid, df) > target else (lo, mid)
    return sign * (lo + (hi - lo) / 2.0)


def sample_variance(xs: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    vals: list[float] = []
    for x in xs:
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            return None
        try:
            fx = float(x)
        except OverflowError:
            return None
        if not math.isfinite(fx):
            return None
        vals.append(fx)
    if min(vals) == max(vals):
        return 0.0
    mean_v = _safe_fmean(vals)
    if not math.isfinite(mean_v):
        return None
    diffs = [x - mean_v for x in vals]
    if not all(math.isfinite(d) for d in diffs):
        return None
    scale = max(abs(d) for d in diffs)
    if scale == 0.0:
        return 0.0
    var = (math.fsum((d / scale) ** 2 for d in diffs) / (n - 1)) * scale * scale
    return var if math.isfinite(var) else None


@dataclass(frozen=True)
class WithinNoise:
    total: float
    pairs: list[tuple[float, float]]
    imputed: int


def _within_noise(outcomes: Outcomes) -> WithinNoise | None:
    known: dict[str, float] = {}
    for t, xs in outcomes.items():
        if len(xs) < 2:
            continue
        v = sample_variance(xs)
        if v is None:
            return None
        known[t] = v
    if not known:
        return None
    borrowed = _safe_fmean(list(known.values()))
    terms = {t: known.get(t, borrowed) / len(xs) for t, xs in outcomes.items()}
    lent = sum(1.0 / len(xs) for t, xs in outcomes.items() if t not in known) / len(known)
    pairs = [
        (v * (1.0 / len(outcomes[t]) + lent), float(len(outcomes[t]) - 1)) for t, v in known.items()
    ]
    if not all(math.isfinite(w) for w, _ in pairs):
        return None
    try:
        total = math.fsum(terms.values())
    except OverflowError:
        return None
    if not math.isfinite(total):
        return None
    return WithinNoise(total=total, pairs=pairs, imputed=len(outcomes) - len(known))


def _satterthwaite_df(pairs: Sequence[tuple[float, float]]) -> float | None:
    scale = max((abs(w) for w, d in pairs if d > 0.0 and math.isfinite(w)), default=0.0)
    if scale <= 0.0:
        return None
    denom = math.fsum((w / scale) ** 2 / d for w, d in pairs if d > 0.0)
    if denom <= 0.0 or not math.isfinite(denom):
        return None
    num = math.fsum(w / scale for w, _ in pairs)
    df = (num * num) / denom
    return df if math.isfinite(df) and df > 0.0 else None


def exact_sign_flip_p(
    deltas: Sequence[float],
    denominators: Sequence[int] | None = None,
    *,
    seed: int = 0,
) -> tuple[float | None, bool]:
    if not deltas:
        return None, False
    if denominators is not None and len(denominators) != len(deltas):
        raise ValueError("denominators length must match deltas length")
    active: list[tuple[float, int | None]] = []
    for idx, raw_d in enumerate(deltas):
        val = _to_finite_float(raw_d, "delta")
        if abs(val) > _ZERO_TOL:
            active.append((val, denominators[idx] if denominators is not None else None))
    if not active:
        return 1.0, True
    values = [v for v, _ in active]
    v_scale = max(abs(v) for v in values)
    norm_values = [v / v_scale for v in values]
    observed = abs(math.fsum(norm_values))
    if len(active) <= EXACT_ENUM_MAX_N:
        return _enum_sign_flip(active, norm_values, v_scale, observed), True
    if denominators is not None:
        lat_p = _lattice_sign_flip(active)
        if lat_p is not None:
            return lat_p, True
    rng = random.Random(seed)
    mc_tol = 64.0 * sys.float_info.epsilon * observed
    hits = sum(
        1
        for _ in range(MONTE_CARLO_DRAWS)
        if abs(math.fsum(v if rng.random() < 0.5 else -v for v in norm_values)) >= observed - mc_tol
    )
    return (hits + 1) / (MONTE_CARLO_DRAWS + 1), False


@dataclass(frozen=True)
class ArmStats:
    n_tasks: int
    n_trials: int
    mean: float
    micro: float
    se_rerun: float | None
    df_rerun: float | None
    ci_rerun: tuple[float, float] | None
    se_tasks: float | None
    ci_tasks: tuple[float, float] | None


def compute_arm_stats(outcomes: Outcomes, *, level: float = 0.95) -> ArmStats:
    level = _validate_probability_param("level", level)
    tasks = sorted(t for t, xs in outcomes.items() if len(xs) > 0)
    if not tasks:
        raise ValueError("arm has no scored trials")
    _validate_outcomes(outcomes, tasks)
    kept = {t: [float(x) for x in outcomes[t]] for t in tasks}
    means = [_safe_fmean(kept[t]) for t in tasks]
    n_tasks = len(tasks)
    n_trials = sum(len(kept[t]) for t in tasks)
    mean_v = _safe_fmean(means)
    micro_v = _safe_fmean([x for xs in kept.values() for x in xs])

    se_rerun = df_rerun = None
    ci_rerun: tuple[float, float] | None = None
    noise = _within_noise(kept)
    if noise is not None:
        se_rerun = math.sqrt(noise.total) / n_tasks
        if se_rerun <= 1e-15:
            df_rerun = math.fsum(d for _, d in noise.pairs)
            ci_rerun = (mean_v, mean_v)
        else:
            df_rerun = _satterthwaite_df(noise.pairs)
            if df_rerun is not None and math.isfinite(df_rerun) and df_rerun > 0.0:
                half = -t_quantile((1.0 - level) / 2.0, df_rerun) * se_rerun
                lo, hi = mean_v - half, mean_v + half
                if math.isfinite(lo) and math.isfinite(hi):
                    ci_rerun = (lo, hi)

    var_t = sample_variance(means)
    se_tasks = math.sqrt(var_t / n_tasks) if var_t is not None else None
    ci_tasks: tuple[float, float] | None = None
    if se_tasks is not None and n_tasks >= 2:
        if se_tasks <= 1e-15:
            ci_tasks = (mean_v, mean_v)
        else:
            half_t = -t_quantile((1.0 - level) / 2.0, n_tasks - 1) * se_tasks
            lo_t, hi_t = mean_v - half_t, mean_v + half_t
            if math.isfinite(lo_t) and math.isfinite(hi_t):
                ci_tasks = (lo_t, hi_t)

    return ArmStats(
        n_tasks=n_tasks,
        n_trials=n_trials,
        mean=mean_v,
        micro=micro_v,
        se_rerun=se_rerun,
        df_rerun=df_rerun,
        ci_rerun=ci_rerun,
        se_tasks=se_tasks,
        ci_tasks=ci_tasks,
    )


@dataclass(frozen=True)
class PairedComparison:
    n_tasks: int
    baseline_mean: float
    candidate_mean: float
    difference: float
    se_rerun: float | None
    df_rerun: float | None
    ci_rerun: tuple[float, float] | None
    p_rerun: float | None
    verdict_rerun: str
    se_tasks: float | None
    ci_tasks: tuple[float, float] | None
    p_paired_t: float | None
    p_signflip: float | None
    signflip_exact: bool
    verdict_tasks: str
    candidate_better: int
    baseline_better: int
    ties: int
    task_deltas: dict[str, float] = field(default_factory=dict)
    p_mcnemar: float | None = None
    p_perm_rerun: float | None = None
    perm_rerun_exact: bool = False

    @property
    def diff(self) -> float:
        return self.difference


def _paired_rerun_stats(
    ka: dict[str, list[float]],
    kb: dict[str, list[float]],
    tasks: list[str],
    diff: float,
    *,
    level: float,
    alpha: float,
    seed: int,
) -> tuple[
    float | None,
    float | None,
    tuple[float, float] | None,
    float | None,
    float | None,
    bool,
    str,
]:
    n = len(tasks)
    assert n >= 1
    se_rerun = df_rerun = p_rerun = None
    ci_rerun: tuple[float, float] | None = None
    na, nb = _within_noise(ka), _within_noise(kb)
    if na is not None and nb is not None and math.isfinite(na.total + nb.total):
        se_rerun = math.sqrt(na.total + nb.total) / n
        if se_rerun <= 1e-15:
            df_rerun = math.fsum(d for _, d in (*na.pairs, *nb.pairs))
            p_rerun = 1.0 if abs(diff) <= _ZERO_TOL else 0.0
            ci_rerun = (diff, diff)
        else:
            df_rerun = _satterthwaite_df(na.pairs + nb.pairs)
            if df_rerun is not None and math.isfinite(df_rerun) and df_rerun > 0.0:
                p_rerun = t_two_sided_p(diff / se_rerun, df_rerun)
                half = -t_quantile((1.0 - level) / 2.0, df_rerun) * se_rerun
                lo_r, hi_r = diff - half, diff + half
                if math.isfinite(lo_r) and math.isfinite(hi_r):
                    ci_rerun = (lo_r, hi_r)
    p_perm: float | None = None
    perm_ex = False
    if na is None or nb is None:
        p_perm, perm_ex = stratified_permutation_p([(ka[t], kb[t]) for t in tasks], seed=seed)
        if p_rerun is None:
            p_rerun = p_perm
            verdict = "resolved" if (p_perm is not None and p_perm < alpha) else "not_resolved"
            return se_rerun, df_rerun, ci_rerun, p_rerun, p_perm, perm_ex, verdict
    verdict_rerun = (
        "cannot_tell"
        if p_rerun is None or not math.isfinite(p_rerun)
        else ("resolved" if p_rerun < alpha else "not_resolved")
    )
    return se_rerun, df_rerun, ci_rerun, p_rerun, p_perm, perm_ex, verdict_rerun


def _paired_tasks_stats(
    deltas: list[float],
    denoms: list[int],
    diff: float,
    *,
    level: float,
    alpha: float,
    seed: int,
) -> tuple[float | None, tuple[float, float] | None, float | None, float | None, bool, str]:
    n = len(deltas)
    var_d = sample_variance(deltas)
    se_tasks = math.sqrt(var_d / n) if var_d is not None else None
    ci_tasks: tuple[float, float] | None = None
    if se_tasks is None:
        p_t = None
    elif se_tasks <= 1e-15:
        p_t = 1.0 if abs(diff) <= _ZERO_TOL else 0.0
        ci_tasks = (diff, diff)
    else:
        p_t = t_two_sided_p(diff / se_tasks, n - 1)
        half_t = -t_quantile((1.0 - level) / 2.0, n - 1) * se_tasks
        lo_t, hi_t = diff - half_t, diff + half_t
        if math.isfinite(lo_t) and math.isfinite(hi_t):
            ci_tasks = (lo_t, hi_t)
    p_flip, flip_exact = exact_sign_flip_p(deltas, denoms, seed=seed)
    tasks_p = (
        max(p_t, p_flip)
        if p_t is not None and p_flip is not None and math.isfinite(p_t) and math.isfinite(p_flip)
        else None
    )
    verdict = (
        "cannot_tell" if tasks_p is None else ("resolved" if tasks_p < alpha else "not_resolved")
    )
    return se_tasks, ci_tasks, p_t, p_flip, flip_exact, verdict


def compare_paired_arms(
    baseline: Outcomes,
    candidate: Outcomes,
    *,
    level: float = 0.95,
    alpha: float = 0.05,
    seed: int = 0,
) -> PairedComparison:
    level = _validate_probability_param("level", level)
    alpha = _validate_probability_param("alpha", alpha)
    tasks = sorted(
        t for t in baseline if t in candidate and len(baseline[t]) > 0 and len(candidate[t]) > 0
    )
    if not tasks:
        raise ValueError("arms share no tasks with scored trials")
    _validate_outcomes(baseline, tasks)
    _validate_outcomes(candidate, tasks)
    n = len(tasks)
    ka = {t: [float(x) for x in baseline[t]] for t in tasks}
    kb = {t: [float(x) for x in candidate[t]] for t in tasks}
    base_means = [_safe_fmean(ka[t]) for t in tasks]
    cand_means = [_safe_fmean(kb[t]) for t in tasks]
    deltas = {t: cm - bm for t, bm, cm in zip(tasks, base_means, cand_means, strict=True)}
    if not all(math.isfinite(d) for d in deltas.values()):
        raise ValueError("paired arm task delta overflows float64")
    delta_list = list(deltas.values())
    diff = _safe_fmean(delta_list)

    se_r, df_r, ci_r, p_r, p_perm, perm_ex, verdict_r = _paired_rerun_stats(
        ka, kb, tasks, diff, level=level, alpha=alpha, seed=seed
    )
    denoms = [math.lcm(len(ka[t]), len(kb[t])) for t in tasks]
    se_t, ci_t, p_t, p_flip, flip_ex, verdict_t = _paired_tasks_stats(
        delta_list, denoms, diff, level=level, alpha=alpha, seed=seed
    )
    p_mcnemar = exact_mcnemar_p(list(zip(base_means, cand_means, strict=True)))
    cand_w = sum(1 for d in delta_list if d > _ZERO_TOL)
    base_w = sum(1 for d in delta_list if d < -_ZERO_TOL)
    ties = sum(1 for d in delta_list if abs(d) <= _ZERO_TOL)
    assert cand_w + base_w + ties == n

    return PairedComparison(
        n_tasks=n,
        baseline_mean=_safe_fmean(base_means),
        candidate_mean=_safe_fmean(cand_means),
        difference=diff,
        se_rerun=se_r,
        df_rerun=df_r,
        ci_rerun=ci_r,
        p_rerun=p_r,
        verdict_rerun=verdict_r,
        se_tasks=se_t,
        ci_tasks=ci_t,
        p_paired_t=p_t,
        p_signflip=p_flip,
        signflip_exact=flip_ex,
        verdict_tasks=verdict_t,
        candidate_better=cand_w,
        baseline_better=base_w,
        ties=ties,
        task_deltas=deltas,
        p_mcnemar=p_mcnemar,
        p_perm_rerun=p_perm,
        perm_rerun_exact=perm_ex,
    )
