from __future__ import annotations

import pytest

from evallab.analysis_statistics import (
    AnalysisStatus,
    BinaryArmObservation,
    PairedBinaryInput,
    RefusalCode,
    RepeatCellInput,
    analyze_repeat_heterogeneity,
    compute_design_effect,
    compute_sequence_fidelity,
    exact_paired_binary_contrast,
    fisher_exact_2x2,
    wilson_score_interval,
)
from evallab.cohort import wilson_interval as cohort_wilson_interval


def test_paired_contrast_4_0_discordant_design_floor() -> None:
    """Acceptance test: 4-0 discordant units produce p=0.125 and design-floor flag."""
    inputs = [
        PairedBinaryInput(assignment_unit_id=f"u{i}", arm_a_outcome=True, arm_b_outcome=False)
        for i in range(4)
    ]
    res = exact_paired_binary_contrast(inputs)
    assert res.status == AnalysisStatus.VALID
    assert res.n_pairs == 4
    assert res.n_discordant == 4
    assert res.discordant_a_only == 4
    assert res.discordant_b_only == 0
    assert pytest.approx(res.exact_p_value, abs=1e-6) == 0.125
    assert pytest.approx(res.min_attainable_p_value, abs=1e-6) == 0.125
    assert res.is_design_floor is True
    assert res.design_floor_limited is True
    assert res.risk_difference == 1.0


def test_36_row_campaign_9_independent_assignment_pairs() -> None:
    """Acceptance test: 36-row campaign is aggregated as 9 independent matched pairs."""
    # 3 seeds x 3 dose conditions = 9 assignment units
    # Each evaluated with 2 arms: neutral_padding vs semantic_distractor
    observations: list[BinaryArmObservation] = []
    seeds = [42, 1337, 2026]
    doses = [4096, 16384, 65536]
    for seed in seeds:
        for dose in doses:
            unit_id = f"seed_{seed}_dose_{dose}"
            observations.append(
                BinaryArmObservation(
                    assignment_unit_id=unit_id,
                    arm_id="neutral_padding",
                    outcome=True,
                )
            )
            observations.append(
                BinaryArmObservation(
                    assignment_unit_id=unit_id,
                    arm_id="semantic_distractor",
                    outcome=(dose < 65536),
                )
            )

    assert len(observations) == 18
    res = exact_paired_binary_contrast(
        observations,
        arm_a_id="neutral_padding",
        arm_b_id="semantic_distractor",
    )
    assert res.status == AnalysisStatus.VALID
    assert res.n_pairs == 9
    assert res.concordant_success_a_b == 6
    assert res.discordant_a_only == 3
    assert res.discordant_b_only == 0
    assert res.concordant_failure_a_b == 0
    assert pytest.approx(res.risk_difference, abs=1e-6) == 3 / 9


def test_paired_refusals() -> None:
    # Zero opportunity
    assert exact_paired_binary_contrast([]).refusal_code == RefusalCode.ZERO_OPPORTUNITY

    # Capture incomplete
    incomplete = [
        PairedBinaryInput(
            assignment_unit_id="u1", arm_a_outcome=1, arm_b_outcome=0, capture_complete=False
        )
    ]
    assert exact_paired_binary_contrast(incomplete).refusal_code == RefusalCode.CAPTURE_INCOMPLETE

    # Duplicate assignment unit
    dup = [
        BinaryArmObservation(assignment_unit_id="u1", arm_id="a", outcome=1),
        BinaryArmObservation(assignment_unit_id="u1", arm_id="a", outcome=0),
        BinaryArmObservation(assignment_unit_id="u1", arm_id="b", outcome=1),
    ]
    assert exact_paired_binary_contrast(dup).refusal_code == RefusalCode.DUPLICATE_ASSIGNMENT_UNIT

    # Missing arm
    missing = [BinaryArmObservation(assignment_unit_id="u1", arm_id="a", outcome=1)]
    assert exact_paired_binary_contrast(missing).refusal_code == RefusalCode.MISSING_PAIR_ARM


def test_repeat_heterogeneity_boundary_and_clamping() -> None:
    """Acceptance test: exposes no-detectable-heterogeneity boundary without claiming homogeneity."""
    # Balanced m=2 repeats with identical outcome rates across 10 cells
    cells = [RepeatCellInput(cell_id=f"c{i}", successes=1, repeats=2) for i in range(10)]
    report = analyze_repeat_heterogeneity(cells)

    assert report.status == AnalysisStatus.VALID
    assert report.n_cells == 10
    assert report.repeats_per_cell == 2
    assert report.total_observations == 20
    assert report.total_successes == 10
    assert report.observed_success_distribution == {0: 0, 1: 10, 2: 0}
    assert report.raw_icc is not None and report.raw_icc <= 0.0
    assert report.icc == 0.0
    assert report.icc_clamped is True
    assert report.no_detectable_heterogeneity is True
    assert report.design_effect == 1.0
    assert report.effective_n == 20.0
    assert compute_design_effect(2, 0.0) == 1.0


def test_repeat_heterogeneity_overdispersed() -> None:
    # 5 cells with 2 successes, 5 cells with 0 successes (high between-cell variance)
    cells = [
        RepeatCellInput(cell_id=f"c{i}", successes=2 if i < 5 else 0, repeats=2) for i in range(10)
    ]
    report = analyze_repeat_heterogeneity(cells)

    assert report.status == AnalysisStatus.VALID
    assert report.raw_icc == 1.0
    assert report.icc == 1.0
    assert report.icc_clamped is False
    assert report.no_detectable_heterogeneity is False
    assert report.design_effect == 2.0
    assert report.effective_n == 10.0


def test_fisher_exact_and_wilson() -> None:
    f_res = fisher_exact_2x2([[8, 2], [1, 5]])
    assert f_res.status == AnalysisStatus.VALID
    assert f_res.odds_ratio == 20.0
    assert f_res.exact_p_value is not None

    w_res = wilson_score_interval(successes=7, denominator=10)
    cohort_bounds = cohort_wilson_interval(7, 10)
    assert cohort_bounds is not None
    assert pytest.approx(w_res.lower, abs=1e-6) == cohort_bounds[0]
    assert pytest.approx(w_res.upper, abs=1e-6) == cohort_bounds[1]


def test_sequence_fidelity() -> None:
    seq_a = [1, 2, 3, 4, 5]
    seq_b = [1, 2, 4, 3, 5]
    fid = compute_sequence_fidelity(seq_a, seq_b)

    assert fid.status == AnalysisStatus.VALID
    assert fid.is_identical is False
    assert fid.first_mismatch_index == 2
    assert fid.common_prefix_length == 2
    assert fid.kendall_inversion_count == 1
    assert fid.spearman_footrule_distance == 2.0
    assert fid.jaccard_similarity == 1.0
