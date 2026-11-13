from __future__ import annotations

import math
import statistics

NOT_COMPARABLE = "not distinguishable / not comparable"


def wilson_interval(
    successes: int,
    denominator: int,
    z: float = 1.959963984540054,
) -> tuple[float, float] | None:
    if denominator == 0:
        return None
    proportion = successes / denominator
    z_squared = z * z
    scale = 1 + z_squared / denominator
    center = (proportion + z_squared / (2 * denominator)) / scale
    half_width = (
        z
        * math.sqrt(
            proportion * (1 - proportion) / denominator
            + z_squared / (4 * denominator * denominator)
        )
        / scale
    )
    lower = max(0.0, center - half_width)
    upper = min(1.0, center + half_width)
    return lower, upper


def pass_at_k_probability(attempt_probability: float, k: int) -> float:
    """Model-based independent-attempt power-planning transform, not an empirical estimator.

    This is ``1 - (1-p)**k``. It is not realized first-k (any/all over ordered attempts)
    and it is not the Chen/Yao combinatorial estimator computed from observed (n, c, k).
    """
    if not 0 <= attempt_probability <= 1:
        raise ValueError("attempt probability must be between zero and one")
    if k < 1:
        raise ValueError("k must be positive")
    return 1 - (1 - attempt_probability) ** k


def _normal_cutoff(alpha: float, target_power: float) -> float:
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between zero and one")
    if not 0 < target_power < 1:
        raise ValueError("power must be between zero and one")
    normal = statistics.NormalDist()
    return normal.inv_cdf(1 - alpha / 2) + normal.inv_cdf(target_power)


def _paired_variance(p0: float, p1: float, correlation: float) -> float:
    if not -0.99 <= correlation <= 0.99:
        raise ValueError("pair correlation must be between -0.99 and 0.99")
    covariance = correlation * math.sqrt(p0 * (1 - p0) * p1 * (1 - p1))
    return max(p0 * (1 - p0) + p1 * (1 - p1) - 2 * covariance, 0.0)


def required_tasks_for_effect(
    *,
    baseline: float,
    attempt_effect: float,
    k: int,
    alpha: float = 0.05,
    target_power: float = 0.8,
    pair_correlation: float = 0.0,
) -> int | None:
    if attempt_effect <= 0 or baseline + attempt_effect > 1:
        raise ValueError("target effect must be positive and keep probability at or below one")
    p0 = pass_at_k_probability(baseline, k)
    p1 = pass_at_k_probability(baseline + attempt_effect, k)
    task_effect = p1 - p0
    if task_effect <= 0:
        return None
    variance = _paired_variance(p0, p1, pair_correlation)
    required = (_normal_cutoff(alpha, target_power) ** 2) * variance / (task_effect**2)
    return max(2, math.ceil(required))


def minimum_detectable_effect(
    *,
    n_tasks: int,
    k: int,
    baseline: float,
    alpha: float = 0.05,
    target_power: float = 0.8,
    pair_correlation: float = 0.0,
) -> float | None:
    if n_tasks < 2:
        raise ValueError("n_tasks must be at least two")
    if not 0 <= baseline < 1:
        raise ValueError("baseline must be at least zero and below one")

    def detectable(attempt_effect: float) -> bool:
        required = required_tasks_for_effect(
            baseline=baseline,
            attempt_effect=attempt_effect,
            k=k,
            alpha=alpha,
            target_power=target_power,
            pair_correlation=pair_correlation,
        )
        return required is not None and required <= n_tasks

    upper = 1 - baseline
    if not detectable(upper):
        return None
    lower = 0.0
    for _ in range(60):
        midpoint = (lower + upper) / 2
        if detectable(midpoint):
            upper = midpoint
        else:
            lower = midpoint
    return upper
