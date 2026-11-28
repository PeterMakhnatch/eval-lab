from __future__ import annotations

import itertools
import math
import random
import sys
from collections.abc import Sequence
from decimal import Decimal
from fractions import Fraction

EXACT_ENUM_MAX_N = 18
EXACT_LATTICE_LIMIT = 2_000_000
EXACT_MAX_SCALE = 10**12
EXACT_PERM_MAX_COMBS = 65_536
MONTE_CARLO_DRAWS = 20_000
_ZERO_TOL = 1e-12


def _safe_fmean(values: Sequence[float]) -> float:
    n = len(values)
    assert n > 0
    try:
        total = math.fsum(values)
        if math.isfinite(total):
            return total / n
    except OverflowError:
        pass
    scale = max(abs(x) for x in values)
    if scale == 0.0:
        return 0.0
    exp = math.frexp(scale)[1]
    return math.ldexp(math.fsum(math.ldexp(x, -exp) for x in values) / n, exp)


def _to_finite_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number, got {value!r}")
    try:
        val = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} overflows float64: {value!r}") from exc
    if not math.isfinite(val):
        raise ValueError(f"{label} must be finite, got {value!r}")
    return val


def exact_mcnemar_p(pairs: Sequence[tuple[float, float]]) -> float | None:
    if not pairs:
        return None
    n01 = n10 = 0
    for a, b in pairs:
        if isinstance(a, bool) or isinstance(b, bool):
            return None
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return None
        try:
            fa, fb = float(a), float(b)
        except OverflowError:
            return None
        if fa not in (0.0, 1.0) or fb not in (0.0, 1.0):
            return None
        n01 += int(fa == 0.0 and fb == 1.0)
        n10 += int(fa == 1.0 and fb == 0.0)
    n = n01 + n10
    assert 0 <= n <= len(pairs)
    if n == 0 or n01 == n10:
        return 1.0
    log_fact_n = math.lgamma(n + 1.0)
    n_log2 = n * math.log(2.0)
    tail = math.fsum(
        math.exp(log_fact_n - math.lgamma(i + 1.0) - math.lgamma(n - i + 1.0) - n_log2)
        for i in range(min(n01, n10) + 1)
    )
    p = min(1.0, max(0.0, 2.0 * tail))
    assert 0.0 <= p <= 1.0
    return p


def _stratum_exact_deltas(b: list[float], c: list[float]) -> list[float]:
    na, nb = len(b), len(c)
    assert na >= 1 and nb >= 1
    if na == 1 and nb == 1:
        d = c[0] - b[0]
        return [d, -d]
    pooled = b + c
    n_tot = na + nb
    out: list[float] = []
    for base_idx in itertools.combinations(range(n_tot), na):
        in_base = set(base_idx)
        b_sub = [pooled[j] for j in base_idx]
        c_sub = [pooled[j] for j in range(n_tot) if j not in in_base]
        out.append(_safe_fmean(c_sub) - _safe_fmean(b_sub))
    return out


def _exact_strata_perm_p(clean: list[tuple[list[float], list[float]]], obs: float) -> float:
    m = len(clean)
    assert m >= 1
    acc: list[float] = [0.0]
    for b, c in clean:
        scaled = [d / m for d in _stratum_exact_deltas(b, c)]
        acc = [prev + sd for prev in acc for sd in scaled]
    assert 1 <= len(acc) <= EXACT_PERM_MAX_COMBS
    tol = 64.0 * sys.float_info.epsilon * obs
    hits = sum(1 for val in acc if abs(val) >= obs - tol)
    assert 1 <= hits <= len(acc)
    return min(1.0, hits / len(acc))


def _center_and_scale_strata(
    clean: list[tuple[list[float], list[float]]],
) -> list[tuple[list[float], list[float]]]:
    raw_max = max(abs(x) for b, c in clean for x in (*b, *c))
    if raw_max > 1e300:
        clean = [([math.ldexp(x, -2) for x in b], [math.ldexp(x, -2) for x in c]) for b, c in clean]
    centered = [
        ([x - mu for x in b], [x - mu for x in c]) for b, c in clean for mu in (_safe_fmean(b + c),)
    ]
    scale = max(abs(x) for b, c in centered for x in (*b, *c))
    if scale <= 0.0:
        return centered
    exp = math.frexp(scale)[1]
    return [
        ([math.ldexp(x, -exp) for x in b], [math.ldexp(x, -exp) for x in c]) for b, c in centered
    ]


def stratified_permutation_p(
    strata: Sequence[tuple[Sequence[float], Sequence[float]]],
    *,
    seed: int = 0,
    max_draws: int = 10_000,
) -> tuple[float | None, bool]:
    if isinstance(max_draws, bool) or not isinstance(max_draws, int) or max_draws < 1:
        raise ValueError(f"max_draws must be a positive int, got {max_draws!r}")
    clean = [
        (
            [_to_finite_float(x, f"stratum[{idx}].base") for x in b_raw],
            [_to_finite_float(x, f"stratum[{idx}].cand") for x in c_raw],
        )
        for idx, (b_raw, c_raw) in enumerate(strata)
        if len(b_raw) > 0 and len(c_raw) > 0
    ]
    if not clean:
        return None, False
    raw_diffs = [_safe_fmean(c) - _safe_fmean(b) for b, c in clean]
    if not all(math.isfinite(d) for d in raw_diffs):
        raise ValueError("stratum difference overflows float64")
    clean = _center_and_scale_strata(clean)
    task_diffs = [_safe_fmean(c) - _safe_fmean(b) for b, c in clean]
    obs = abs(_safe_fmean(task_diffs))
    mag = _safe_fmean([abs(d) for d in task_diffs])
    assert math.isfinite(obs) and obs >= 0.0
    total_combs = 1
    exact_ok = True
    for b, c in clean:
        total_combs *= math.comb(len(b) + len(c), len(b))
        if total_combs > EXACT_PERM_MAX_COMBS:
            exact_ok = False
            break
    if obs == 0.0 or obs <= 64.0 * sys.float_info.epsilon * mag:
        return 1.0, True
    if exact_ok:
        return _exact_strata_perm_p(clean, obs), True
    rng = random.Random(seed)
    pooled_strata = [(b + c, len(b)) for b, c in clean]
    tol = 64.0 * sys.float_info.epsilon * obs
    ge_count = 0
    for _ in range(max_draws):
        draw_diffs = []
        for pool, na in pooled_strata:
            rng.shuffle(pool)
            draw_diffs.append(_safe_fmean(pool[na:]) - _safe_fmean(pool[:na]))
        ge_count += int(abs(_safe_fmean(draw_diffs)) >= obs - tol)
    assert 0 <= ge_count <= max_draws
    return (ge_count + 1) / (max_draws + 1), False


def _convolve(dist: dict[int, float], step: dict[int, float]) -> dict[int, float]:
    out: dict[int, float] = {}
    for x, px in dist.items():
        for y, py in step.items():
            out[x + y] = out.get(x + y, 0.0) + px * py
    return out


def _bounded_lcm(values: Sequence[int], limit: int) -> int | None:
    scale = 1
    for dn in values:
        if type(dn) is not int or dn <= 0:
            return None
        scale = math.lcm(scale, dn)
        if scale > limit:
            return None
    return scale


def _enum_sign_flip(
    active: list[tuple[float, int | None]],
    norm_values: list[float],
    v_scale: float,
    observed: float,
) -> float:
    v_min = min(abs(v) for v, _ in active)
    if v_scale > 1e6 * v_min or observed < 1e-6:
        fracs: list[Fraction] = []
        snap_tol = 64.0 * sys.float_info.epsilon
        for v, dn in active:
            if dn is not None and type(dn) is int and 1 <= dn <= EXACT_MAX_SCALE:
                sc = v * dn
                if math.isfinite(sc) and abs(sc - round(sc)) <= snap_tol * max(1.0, abs(sc)):
                    fracs.append(Fraction(round(sc), dn))
                    continue
            fracs.append(Fraction(Decimal(str(v))))
        common_den = math.lcm(*(f.denominator for f in fracs))
        exact_ints = [int(f * common_den) for f in fracs]
        obs_exact = abs(sum(exact_ints))
        int_sums = [0]
        for iv in exact_ints:
            int_sums = [s + iv for s in int_sums] + [s - iv for s in int_sums]
        return min(1.0, sum(1 for s in int_sums if abs(s) >= obs_exact) / len(int_sums))
    tol = 64.0 * sys.float_info.epsilon * observed
    sums = [0.0]
    for v in sorted(norm_values, key=abs):
        sums = [s + v for s in sums] + [s - v for s in sums]
    return min(1.0, sum(1 for s in sums if abs(s) >= observed - tol) / len(sums))


def _lattice_sign_flip(active: list[tuple[float, int | None]]) -> float | None:
    active_denoms = [dn for _, dn in active if dn is not None]
    scale = (
        _bounded_lcm(active_denoms, EXACT_MAX_SCALE) if len(active_denoms) == len(active) else None
    )
    if scale is None:
        return None
    scaled = [v * scale for v, _ in active]
    int_tol = 64.0 * sys.float_info.epsilon
    if not all(
        math.isfinite(s)
        and abs(s) <= EXACT_LATTICE_LIMIT
        and round(s) != 0
        and abs(s - round(s)) <= int_tol * max(1.0, abs(s))
        for s in scaled
    ):
        return None
    tot_err = math.fsum(abs(s - round(s)) for s in scaled)
    tot_mag = math.fsum(abs(s) for s in scaled)
    ints = [round(s) for s in scaled]
    if (
        tot_err > 1e-13 * max(1.0, tot_mag)
        or len(ints) * sum(abs(i) for i in ints) > EXACT_LATTICE_LIMIT
    ):
        return None
    dist: dict[int, float] = {0: 1.0}
    for iv in ints:
        dist = _convolve(dist, {iv: 0.5, -iv: 0.5})
    obs_int = abs(sum(ints))
    return min(1.0, math.fsum(p for x, p in dist.items() if abs(x) >= obs_int))
