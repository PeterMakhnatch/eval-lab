from __future__ import annotations

import pytest

from evallab.analysis_capability import AnalysisStatus, RefusalCode
from evallab.analysis_statistics import (
    BinaryArmObservation,
    PairedBinaryInput,
    RepeatCellInput,
    analyze_repeat_heterogeneity,
    compute_design_effect,
    compute_sequence_fidelity,
    compute_wilson_interval,
    exact_paired_binary_contrast,
    fisher_exact_2x2,
    normal_quantile,
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
    assert pytest.approx(res.design_floor_p_value, abs=1e-6) == 0.125
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


def test_paired_contrast_field_semantics() -> None:
    """Preserve distinction between planned all-pairs floor and observed discordant floor."""
    # 10 pairs: 6 concordant, 4 discordant (4-0)
    inputs = [
        PairedBinaryInput(assignment_unit_id="u0", arm_a_outcome=True, arm_b_outcome=False),
        PairedBinaryInput(assignment_unit_id="u1", arm_a_outcome=True, arm_b_outcome=False),
        PairedBinaryInput(assignment_unit_id="u2", arm_a_outcome=True, arm_b_outcome=False),
        PairedBinaryInput(assignment_unit_id="u3", arm_a_outcome=True, arm_b_outcome=False),
        *(
            PairedBinaryInput(assignment_unit_id=f"u{i}", arm_a_outcome=True, arm_b_outcome=True)
            for i in range(4, 10)
        ),
    ]
    res = exact_paired_binary_contrast(inputs)
    assert res.status == AnalysisStatus.VALID
    assert res.n_pairs == 10
    assert res.n_discordant == 4
    # Observed discordant floor: 2^(1 - 4) = 0.125
    assert pytest.approx(res.min_attainable_p_value, abs=1e-6) == 0.125
    # Theoretical all-pairs design floor: 2^(1 - 10) = 2^(-9) = 1/512
    assert pytest.approx(res.design_floor_p_value, abs=1e-6) == 2.0**-9
    assert res.is_design_floor is True
    assert res.design_floor_limited is True


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

    # Duplicate in PairedBinaryInput
    dup_paired = [
        PairedBinaryInput(assignment_unit_id="u1", arm_a_outcome=1, arm_b_outcome=0),
        PairedBinaryInput(assignment_unit_id="u1", arm_a_outcome=0, arm_b_outcome=1),
    ]
    assert (
        exact_paired_binary_contrast(dup_paired).refusal_code
        == RefusalCode.DUPLICATE_ASSIGNMENT_UNIT
    )

    # Missing arm
    missing = [BinaryArmObservation(assignment_unit_id="u1", arm_id="a", outcome=1)]
    assert exact_paired_binary_contrast(missing).refusal_code == RefusalCode.MISSING_PAIR_ARM

    # Invalid outcome value
    invalid_out = [
        BinaryArmObservation(assignment_unit_id="u1", arm_id="a", outcome=5),
        BinaryArmObservation(assignment_unit_id="u1", arm_id="b", outcome=0),
    ]
    assert (
        exact_paired_binary_contrast(invalid_out).refusal_code == RefusalCode.INVALID_BINARY_INPUT
    )

    # Invalid confidence level raises ValueError
    with pytest.raises(ValueError, match="confidence_level"):
        exact_paired_binary_contrast(
            [PairedBinaryInput(assignment_unit_id="u1", arm_a_outcome=1, arm_b_outcome=0)],
            confidence_level=1.5,
        )


def test_arbitrary_confidence_levels_and_normal_quantile() -> None:
    # Check Acklam normal quantile at standard points
    assert pytest.approx(normal_quantile(0.975), abs=1e-6) == 1.959963984540054
    assert pytest.approx(normal_quantile(0.95), abs=1e-5) == 1.644853
    assert pytest.approx(normal_quantile(0.995), abs=1e-5) == 2.575829

    with pytest.raises(ValueError):
        normal_quantile(0.0)
    with pytest.raises(ValueError):
        normal_quantile(1.0)

    # Test compute_wilson_interval helper
    cw = compute_wilson_interval(successes=10, denominator=20, confidence_level=0.95)
    assert cw is not None
    assert compute_wilson_interval(0, 0) is None

    # Test Wilson interval with different confidence levels
    w90 = wilson_score_interval(successes=10, denominator=20, confidence_level=0.90)
    w95 = wilson_score_interval(successes=10, denominator=20, confidence_level=0.95)
    w99 = wilson_score_interval(successes=10, denominator=20, confidence_level=0.99)

    assert w90.lower is not None and w90.upper is not None
    assert w95.lower is not None and w95.upper is not None
    assert w99.lower is not None and w99.upper is not None
    # Higher confidence level produces wider intervals
    assert (w90.upper - w90.lower) < (w95.upper - w95.lower) < (w99.upper - w99.lower)


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


def test_repeat_heterogeneity_refusals() -> None:
    # Zero opportunity
    assert analyze_repeat_heterogeneity([]).refusal_code == RefusalCode.ZERO_OPPORTUNITY

    # Single cell (n=1) must refuse UNDERPOWERED rather than claiming no detectable heterogeneity
    single = [RepeatCellInput(cell_id="c1", successes=1, repeats=2)]
    assert analyze_repeat_heterogeneity(single).refusal_code == RefusalCode.UNDERPOWERED

    # Duplicate cell_id
    dup = [
        RepeatCellInput(cell_id="c1", successes=1, repeats=2),
        RepeatCellInput(cell_id="c1", successes=2, repeats=2),
    ]
    assert analyze_repeat_heterogeneity(dup).refusal_code == RefusalCode.DUPLICATE_ASSIGNMENT_UNIT

    # Capture incomplete
    incomplete = [
        RepeatCellInput(cell_id="c1", successes=1, repeats=2, capture_complete=False),
        RepeatCellInput(cell_id="c2", successes=1, repeats=2),
    ]
    assert analyze_repeat_heterogeneity(incomplete).refusal_code == RefusalCode.CAPTURE_INCOMPLETE

    # Underfilled / unequal repeats
    underfilled = [
        RepeatCellInput(cell_id="c1", successes=1, repeats=2),
        RepeatCellInput(cell_id="c2", successes=1, repeats=3),
    ]
    assert analyze_repeat_heterogeneity(underfilled).refusal_code == RefusalCode.UNDERFILLED_REPEATS

    # Invalid successes (> repeats)
    invalid_succ = [
        RepeatCellInput(cell_id="c1", successes=3, repeats=2),
        RepeatCellInput(cell_id="c2", successes=1, repeats=2),
    ]
    assert (
        analyze_repeat_heterogeneity(invalid_succ).refusal_code == RefusalCode.INVALID_BINARY_INPUT
    )


def test_repeat_heterogeneity_zero_variance_dispersion_p_is_none() -> None:
    """Zero-variance dispersion p remains unavailable/None."""
    # All successes = 0
    all_zero = [RepeatCellInput(cell_id=f"c{i}", successes=0, repeats=2) for i in range(5)]
    rep_zero = analyze_repeat_heterogeneity(all_zero)
    assert rep_zero.status == AnalysisStatus.VALID
    assert rep_zero.dispersion_p_value is None
    assert rep_zero.no_detectable_heterogeneity is True

    # All successes = m
    all_m = [RepeatCellInput(cell_id=f"c{i}", successes=2, repeats=2) for i in range(5)]
    rep_m = analyze_repeat_heterogeneity(all_m)
    assert rep_m.status == AnalysisStatus.VALID
    assert rep_m.dispersion_p_value is None
    assert rep_m.no_detectable_heterogeneity is True


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


def test_fisher_exact_validations_and_refusals() -> None:
    # Invalid shape
    assert fisher_exact_2x2([[1, 2]]).refusal_code == RefusalCode.INVALID_BINARY_INPUT
    assert fisher_exact_2x2([[1, 2, 3], [4, 5, 6]]).refusal_code == RefusalCode.INVALID_BINARY_INPUT

    # Non-integer / negative
    assert fisher_exact_2x2([[-1, 2], [3, 4]]).refusal_code == RefusalCode.INVALID_BINARY_INPUT
    assert fisher_exact_2x2([[True, 2], [3, 4]]).refusal_code == RefusalCode.INVALID_BINARY_INPUT

    # Zero total opportunity
    assert fisher_exact_2x2([[0, 0], [0, 0]]).refusal_code == RefusalCode.ZERO_OPPORTUNITY

    # Degenerate margin -> ZERO_VARIANCE refusal (not OR=1)
    assert fisher_exact_2x2([[0, 0], [5, 5]]).refusal_code == RefusalCode.ZERO_VARIANCE
    assert fisher_exact_2x2([[5, 0], [5, 0]]).refusal_code == RefusalCode.ZERO_VARIANCE


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


def test_sequence_fidelity_unhashable_refusal() -> None:
    # Unhashable elements (e.g. nested lists) return refusal with INVALID_BINARY_INPUT
    seq_a = [[1, 2], [3, 4]]
    seq_b = [[1, 2], [3, 4]]
    fid = compute_sequence_fidelity(seq_a, seq_b)
    assert fid.status == AnalysisStatus.REFUSAL
    assert fid.refusal_code == RefusalCode.INVALID_BINARY_INPUT
