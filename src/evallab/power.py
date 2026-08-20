"""Statistical power planning and sample size estimation for eval-lab comparisons.

Provides task-paired pass@k power calculations, minimum detectable effect (MDE)
estimation, and PowerSpec generation for experiment planning.
"""

from __future__ import annotations

from evallab.cohort import (
    minimum_detectable_effect,
    pass_at_k_probability,
    power_requirements,
    required_tasks_for_effect,
)
from evallab.schemas import PowerSpec

__all__ = [
    "minimum_detectable_effect",
    "pass_at_k_probability",
    "plan_power_spec",
    "power_requirements",
    "required_tasks_for_effect",
]


def plan_power_spec(
    *,
    n_tasks: int,
    k: int = 1,
    baseline: float = 0.0,
    alpha: float = 0.05,
    target_power: float = 0.8,
    pair_correlation: float = 0.0,
) -> PowerSpec:
    """Construct a validated PowerSpec with planned_n and minimum detectable difference."""
    if n_tasks < 2:
        return PowerSpec(
            mdd=None,
            planned_n=n_tasks,
        )

    try:
        mde = minimum_detectable_effect(
            n_tasks=n_tasks,
            k=k,
            baseline=baseline,
            alpha=alpha,
            target_power=target_power,
            pair_correlation=pair_correlation,
        )
    except (ValueError, ZeroDivisionError):
        mde = None

    return PowerSpec(
        mdd=round(mde, 4) if mde is not None else None,
        planned_n=n_tasks,
    )
