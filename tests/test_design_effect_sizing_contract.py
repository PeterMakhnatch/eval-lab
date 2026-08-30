"""Contract tests for clustered / repeated-measure design-effect sizing.

Verifies the fail-closed design-effect contract:
- Explicit ICC (rho) and cluster-size declarations are required.
- Refuses absent, out-of-range, and non-finite ICC values.
- Distinguishes paired covariance (within-pair correlation) from cluster inflation.
- Reports effective sample size (n / DE) and MDE (scaled by sqrt(DE)).
- Preserves exact rho=0 / cluster_size=1 independent behavior.
- Strictly monotonic in ICC and cluster size.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from evallab.cohort import (
    clustered_minimum_detectable_effect,
    clustered_power_requirements,
    clustered_required_tasks_for_effect,
    design_effect,
    effective_sample_size,
    minimum_detectable_effect,
    required_tasks_for_effect,
)
from evallab.design_effect import (
    clustered_minimum_detectable_effect as de_mde,
)
from evallab.design_effect import (
    clustered_power_requirements as de_power,
)
from evallab.design_effect import (
    clustered_required_tasks_for_effect as de_req,
)
from evallab.design_effect import (
    design_effect as de_func,
)
from evallab.design_effect import (
    effective_sample_size as de_eff,
)

# ===========================================================================
# 1. Public API & Re-Export Parity
# ===========================================================================


def test_design_effect_module_exports_all_contract_symbols() -> None:
    """The dedicated design_effect module re-exports the exact cohort functions."""
    assert de_func is design_effect
    assert de_eff is effective_sample_size
    assert de_req is clustered_required_tasks_for_effect
    assert de_mde is clustered_minimum_detectable_effect
    assert de_power is clustered_power_requirements


# ===========================================================================
# 2. Deterministic Boundary & Exact Values
# ===========================================================================


def test_design_effect_rho_zero_preserves_independent_baseline() -> None:
    """rho=0 yields DE=1.0 regardless of cluster size (exact independent preservation)."""
    for cluster_size in (1, 2, 5, 10, 100):
        factor = design_effect(icc=0.0, cluster_size=cluster_size)
        assert factor == 1.0
        assert effective_sample_size(n_units=200, icc=0.0, cluster_size=cluster_size) == 200.0


def test_design_effect_cluster_size_one_preserves_independent_baseline() -> None:
    """cluster_size=1 yields DE=1.0 regardless of valid ICC."""
    for icc in (0.0, 0.05, 0.1, 0.5, 0.99):
        factor = design_effect(icc=icc, cluster_size=1)
        assert factor == 1.0
        assert effective_sample_size(n_units=150, icc=icc, cluster_size=1) == 150.0


def test_design_effect_known_analytical_values() -> None:
    """DE = 1 + (m - 1) * rho evaluates to exact closed-form values."""
    assert math.isclose(design_effect(icc=0.1, cluster_size=5), 1.4)
    assert math.isclose(design_effect(icc=0.25, cluster_size=9), 3.0)
    assert math.isclose(design_effect(icc=0.5, cluster_size=3), 2.0)
    assert math.isclose(effective_sample_size(n_units=140, icc=0.1, cluster_size=5), 100.0)


def test_clustered_required_tasks_rho_zero_equals_independent() -> None:
    """When rho=0, clustered requirement exactly matches the independent requirement."""
    for k in (1, 2, 4):
        indep = required_tasks_for_effect(
            baseline=0.2, attempt_effect=0.15, k=k, pair_correlation=0.3
        )
        clustered = clustered_required_tasks_for_effect(
            baseline=0.2,
            attempt_effect=0.15,
            k=k,
            pair_correlation=0.3,
            icc=0.0,
            cluster_size=10,
        )
        assert clustered["design_effect"] == 1.0
        assert clustered["required_n_tasks_independent"] == indep
        assert clustered["required_n_tasks_clustered"] == indep
        assert clustered["effective_n_tasks"] == indep


def test_clustered_mde_rho_zero_equals_independent() -> None:
    """When rho=0, clustered MDE exactly matches the independent MDE."""
    indep = minimum_detectable_effect(n_tasks=120, k=2, baseline=0.4, pair_correlation=0.2)
    clustered = clustered_minimum_detectable_effect(
        n_tasks=120,
        k=2,
        baseline=0.4,
        pair_correlation=0.2,
        icc=0.0,
        cluster_size=8,
    )
    assert clustered["design_effect"] == 1.0
    assert clustered["minimum_detectable_effect"] == pytest.approx(indep)
    assert clustered["effective_n_tasks"] == 120.0


def test_clustered_mde_scales_strictly_by_sqrt_design_effect() -> None:
    """Clustered MDE = independent MDE * sqrt(DE) for detectable effects."""
    indep = minimum_detectable_effect(n_tasks=150, k=1, baseline=0.3)
    assert indep is not None
    plan = clustered_minimum_detectable_effect(
        n_tasks=150, k=1, baseline=0.3, icc=0.1, cluster_size=5
    )
    de = design_effect(icc=0.1, cluster_size=5)
    expected_mde = indep * math.sqrt(de)
    assert plan["minimum_detectable_effect"] == pytest.approx(expected_mde)


# ===========================================================================
# 3. Monotonicity Invariants
# ===========================================================================


def test_design_effect_strictly_monotonic_in_icc() -> None:
    """Increasing ICC strictly increases DE, required n, and MDE."""
    cluster_size = 6
    iccs = [0.0, 0.05, 0.1, 0.2, 0.4, 0.8]
    des = [design_effect(icc=rho, cluster_size=cluster_size) for rho in iccs]
    for i in range(len(des) - 1):
        assert des[i] < des[i + 1]

    reqs = [
        clustered_required_tasks_for_effect(
            baseline=0.3,
            attempt_effect=0.2,
            k=1,
            icc=rho,
            cluster_size=cluster_size,
        )["required_n_tasks_clustered"]
        for rho in iccs
    ]
    for i in range(len(reqs) - 1):
        assert reqs[i] <= reqs[i + 1]

    mdes = [
        clustered_minimum_detectable_effect(
            n_tasks=200,
            k=1,
            baseline=0.3,
            icc=rho,
            cluster_size=cluster_size,
        )["minimum_detectable_effect"]
        for rho in iccs
    ]
    for i in range(len(mdes) - 1):
        assert mdes[i] < mdes[i + 1]


def test_design_effect_strictly_monotonic_in_cluster_size() -> None:
    """Increasing cluster size strictly increases DE and reduces effective sample size."""
    icc = 0.15
    sizes = [1, 2, 4, 8, 16, 32]
    des = [design_effect(icc=icc, cluster_size=cs) for cs in sizes]
    for i in range(len(des) - 1):
        assert des[i] < des[i + 1]

    eff_n = [effective_sample_size(n_units=500, icc=icc, cluster_size=cs) for cs in sizes]
    for i in range(len(eff_n) - 1):
        assert eff_n[i] > eff_n[i + 1]


# ===========================================================================
# 4. Fail-Closed Refusal Contract
# ===========================================================================


@pytest.mark.parametrize(
    "invalid_icc", [-0.5, -0.001, 1.0, 1.01, 2.5, float("nan"), float("inf"), float("-inf")]
)
def test_design_effect_refuses_invalid_icc(invalid_icc: float) -> None:
    """ICC outside [0, 1) or non-finite must be refused."""
    with pytest.raises(ValueError, match="icc"):
        design_effect(icc=invalid_icc, cluster_size=5)


@pytest.mark.parametrize("invalid_cluster_size", [0, -1, -100, 0.5, 2.3, "five"])  # type: ignore[list-item]
def test_design_effect_refuses_invalid_cluster_size(invalid_cluster_size: Any) -> None:
    """Cluster size below 1 or non-integer must be refused."""
    with pytest.raises(ValueError, match="cluster_size"):
        design_effect(icc=0.1, cluster_size=invalid_cluster_size)


@pytest.mark.parametrize("invalid_n", [0, -1, -50, 0.5])  # type: ignore[list-item]
def test_effective_sample_size_refuses_sub_one_n_units(invalid_n: Any) -> None:
    """n_units must be an integer >= 1."""
    with pytest.raises(ValueError, match="n_units"):
        effective_sample_size(n_units=invalid_n, icc=0.1, cluster_size=5)


def test_clustered_power_requirements_refuses_invalid_max_k() -> None:
    """max_k must be positive."""
    with pytest.raises(ValueError, match="max_k"):
        clustered_power_requirements(
            baseline=0.3, attempt_effect=0.2, max_k=0, icc=0.1, cluster_size=5
        )


# ===========================================================================
# 5. Paired Covariance vs. Cluster Inflation Orthogonality
# ===========================================================================


def test_paired_covariance_and_cluster_inflation_operate_independently() -> None:
    """Paired covariance reduces variance; cluster inflation multiplies total variance.

    Both must operate simultaneously:
    1. Positive pair_correlation reduces the required independent task count.
    2. Positive ICC inflates that reduced requirement by the design effect.
    """
    baseline = 0.3
    effect = 0.2
    k = 1
    icc = 0.1
    cluster_size = 5
    de = design_effect(icc=icc, cluster_size=cluster_size)

    # Independent without pairing vs with pairing
    unpaired_indep = required_tasks_for_effect(
        baseline=baseline, attempt_effect=effect, k=k, pair_correlation=0.0
    )
    paired_indep = required_tasks_for_effect(
        baseline=baseline, attempt_effect=effect, k=k, pair_correlation=0.5
    )
    assert unpaired_indep is not None and paired_indep is not None
    assert paired_indep < unpaired_indep  # pairing reduces sample size

    # Clustered with and without pairing
    unpaired_clustered = clustered_required_tasks_for_effect(
        baseline=baseline,
        attempt_effect=effect,
        k=k,
        pair_correlation=0.0,
        icc=icc,
        cluster_size=cluster_size,
    )["required_n_tasks_clustered"]
    paired_clustered = clustered_required_tasks_for_effect(
        baseline=baseline,
        attempt_effect=effect,
        k=k,
        pair_correlation=0.5,
        icc=icc,
        cluster_size=cluster_size,
    )["required_n_tasks_clustered"]

    assert unpaired_clustered is not None and paired_clustered is not None
    assert unpaired_clustered == math.ceil(unpaired_indep * de)
    assert paired_clustered == math.ceil(paired_indep * de)
    assert paired_clustered < unpaired_clustered


# ===========================================================================
# 6. Table & Output Structure
# ===========================================================================


def test_clustered_power_requirements_table_structure() -> None:
    """Clustered requirement table returns all expected design-effect columns."""
    rows = clustered_power_requirements(
        baseline=0.25,
        attempt_effect=0.2,
        max_k=4,
        icc=0.08,
        cluster_size=6,
    )
    assert len(rows) == 4
    for index, row in enumerate(rows, start=1):
        assert row["k"] == index
        assert row["icc"] == 0.08
        assert row["cluster_size"] == 6
        assert row["design_effect"] == pytest.approx(1.4)
        assert row["required_n_tasks_clustered"] is not None
        assert row["required_n_tasks_independent"] is not None
        assert row["required_n_tasks_clustered"] >= row["required_n_tasks_independent"]
        assert row["effective_n_tasks"] == pytest.approx(row["required_n_tasks_clustered"] / 1.4)
        assert row["total_attempts_two_cohorts"] == 2 * index * row["required_n_tasks_clustered"]
