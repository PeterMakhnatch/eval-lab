from __future__ import annotations

import pytest

from evallab.analysis_capability import (
    AnalysisStatus,
    Basis,
    CascadeStatus,
    CascadeTrialInput,
    CIDisposition,
    DenominatorPolicy,
    FeatureContractRow,
    FeatureObservation,
    RecoveryOpportunity,
    RefusalCode,
    Verdict,
    analyze_cascade_distance,
    analyze_conditional_recovery,
    evaluate_process_outcome_gate,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


# =============================================================================
# T1.1 Process-Outcome Discrimination Gate Tests
# =============================================================================


def test_t11_known_positive_pr267_lineage_violation() -> None:
    """PR #267 known-positive: post-verdict declared input fails statically."""
    contracts = [
        FeatureContractRow(
            feature_name="value_propagation_accuracy",
            is_new_feature=True,
            declared_inputs=("final_state.invariants_passed", "required_value_bindings"),
            available_before_verdict=False,
            denominator_policy=DenominatorPolicy.REQUIRED,
            denominator_sibling="required_value_bindings",
            null_on_zero_denominator=True,
        ),
        FeatureContractRow(
            feature_name="dag_edge_conformance_rate",
            is_new_feature=True,
            declared_inputs=("invariants_passed", "required_dag_edges"),
            available_before_verdict=False,
            denominator_policy=DenominatorPolicy.REQUIRED,
            denominator_sibling="required_dag_edges",
            null_on_zero_denominator=True,
        ),
    ]

    report = evaluate_process_outcome_gate(
        contracts=contracts,
        observations=[],
        source_analysis_snapshot_digest=DIGEST_A,
    )

    assert len(report.results) == 2
    for res in report.results:
        assert res.verdict == Verdict.LINEAGE_VIOLATION
        assert res.basis == Basis.REGISTRY_CONFIRMED
        assert res.ci_disposition == CIDisposition.BLOCK
        assert res.requires_allowlist is False
        assert Verdict.LINEAGE_VIOLATION in res.structural_violations


def test_t11_ci_disposition_split_new_vs_existing() -> None:
    """New features block on structural violations; existing features are advisory."""
    new_feat = FeatureContractRow(
        feature_name="new_contaminated_feature",
        is_new_feature=True,
        declared_inputs=("task_success",),
        available_before_verdict=False,
        denominator_policy=DenominatorPolicy.NOT_APPLICABLE,
    )
    existing_feat = FeatureContractRow(
        feature_name="legacy_contaminated_feature",
        is_new_feature=False,
        declared_inputs=("task_success",),
        available_before_verdict=False,
        denominator_policy=DenominatorPolicy.NOT_APPLICABLE,
    )

    report = evaluate_process_outcome_gate(
        contracts=[new_feat, existing_feat],
        observations=[],
        source_analysis_snapshot_digest=DIGEST_A,
    )

    by_name = {res.feature_name: res for res in report.results}
    assert by_name["new_contaminated_feature"].ci_disposition == CIDisposition.BLOCK
    assert by_name["new_contaminated_feature"].requires_allowlist is False

    assert by_name["legacy_contaminated_feature"].ci_disposition == CIDisposition.ADVISORY
    assert by_name["legacy_contaminated_feature"].requires_allowlist is True


@pytest.mark.parametrize(
    ("contract", "expected_verdict"),
    [
        (
            FeatureContractRow(
                feature_name="missing_lineage",
                is_new_feature=True,
                declared_inputs=None,
                available_before_verdict=None,
                denominator_policy=DenominatorPolicy.NOT_APPLICABLE,
            ),
            Verdict.MISSING_LINEAGE_DECLARATION,
        ),
        (
            FeatureContractRow(
                feature_name="missing_denom_applicability",
                is_new_feature=True,
                declared_inputs=("step_count",),
                available_before_verdict=True,
                denominator_policy=None,
            ),
            Verdict.MISSING_DENOMINATOR_APPLICABILITY_DECLARATION,
        ),
        (
            FeatureContractRow(
                feature_name="missing_denom_sibling",
                is_new_feature=True,
                declared_inputs=("step_count",),
                available_before_verdict=True,
                denominator_policy=DenominatorPolicy.REQUIRED,
                denominator_sibling=None,
                null_on_zero_denominator=True,
            ),
            Verdict.MISSING_DENOMINATOR_DECLARATION,
        ),
        (
            FeatureContractRow(
                feature_name="missing_null_on_zero",
                is_new_feature=True,
                declared_inputs=("step_count",),
                available_before_verdict=True,
                denominator_policy=DenominatorPolicy.REQUIRED,
                denominator_sibling="total_steps",
                null_on_zero_denominator=False,
            ),
            Verdict.MISSING_NULL_ON_ZERO_DECLARATION,
        ),
        (
            FeatureContractRow(
                feature_name="invalid_denom_declaration",
                is_new_feature=True,
                declared_inputs=("step_count",),
                available_before_verdict=True,
                denominator_policy=DenominatorPolicy.NOT_APPLICABLE,
                denominator_sibling="total_steps",
                null_on_zero_denominator=True,
            ),
            Verdict.INVALID_DENOMINATOR_DECLARATION,
        ),
    ],
)
def test_t11_structural_declarations_fail_closed(
    contract: FeatureContractRow, expected_verdict: Verdict
) -> None:
    report = evaluate_process_outcome_gate(
        contracts=[contract],
        observations=[],
        source_analysis_snapshot_digest=DIGEST_A,
    )
    result = report.results[0]
    assert result.verdict == expected_verdict
    assert result.basis == Basis.REGISTRY_CONFIRMED
    assert result.ci_disposition == CIDisposition.BLOCK


def test_t11_empirical_diagnostics_sample_degeneracy_and_auc() -> None:
    contract = FeatureContractRow(
        feature_name="clean_clean_feature",
        is_new_feature=False,
        declared_inputs=("clean_steps",),
        available_before_verdict=True,
        denominator_policy=DenominatorPolicy.NOT_APPLICABLE,
        binary_projection=True,
    )

    # 20 observations, 10 pass / 10 fail.
    # In each stratum, feature is constant (0 on fail, 1 on pass) -> sample-degenerate!
    observations = [
        FeatureObservation(
            feature_name="clean_clean_feature",
            trial_id=f"t_fail_{i}",
            task_success=False,
            value=0,
        )
        for i in range(10)
    ] + [
        FeatureObservation(
            feature_name="clean_clean_feature",
            trial_id=f"t_pass_{i}",
            task_success=True,
            value=1,
        )
        for i in range(10)
    ]

    report = evaluate_process_outcome_gate(
        contracts=[contract],
        observations=observations,
        source_analysis_snapshot_digest=DIGEST_A,
        clearance_n=20,
    )

    res = report.results[0]
    assert res.verdict == Verdict.EMPIRICAL_SUSPECT
    assert res.basis == Basis.EMPIRICAL_DIAGNOSTIC
    assert res.ci_disposition == CIDisposition.ADVISORY
    assert res.empirical.sample_degenerate is True
    assert res.empirical.auc_x_to_task_success == 1.0
    assert res.empirical.disagreement_rate == 0.0


def test_t11_empirical_underpowered_below_clearance_slo() -> None:
    contract = FeatureContractRow(
        feature_name="clean_feature",
        is_new_feature=False,
        declared_inputs=("clean_steps",),
        available_before_verdict=True,
        denominator_policy=DenominatorPolicy.NOT_APPLICABLE,
    )

    observations = [
        FeatureObservation(
            feature_name="clean_feature",
            trial_id=f"t_{i}",
            task_success=i % 2 == 0,
            value=float(i),
        )
        for i in range(10)
    ]

    report = evaluate_process_outcome_gate(
        contracts=[contract],
        observations=observations,
        source_analysis_snapshot_digest=DIGEST_A,
        clearance_n=20,
    )

    res = report.results[0]
    assert res.verdict == Verdict.UNDERPOWERED
    assert res.basis == Basis.NONE
    assert res.empirical.refusal_code == RefusalCode.UNDERPOWERED
    assert res.empirical.n_nonnull == 10


# =============================================================================
# T1.2 Conditional Recovery Analysis Tests
# =============================================================================


def test_t12_zero_opportunity_returns_null_and_refusal() -> None:
    opps = [
        RecoveryOpportunity(
            fault_opportunity_id="fault_1",
            trial_id="trial_1",
            eligible=False,
            recovered=None,
            source_digest=DIGEST_A,
        )
    ]

    result = analyze_conditional_recovery(
        opportunities=opps,
        source_analysis_snapshot_digest=DIGEST_A,
        cohort_key="recovery_test_v1",
    )

    assert result.status == AnalysisStatus.REFUSAL
    assert result.refusal_code == RefusalCode.ZERO_OPPORTUNITY
    assert result.estimate is None
    assert result.interval_lower is None
    assert result.interval_upper is None
    assert result.n_total == 0


def test_t12_missing_recovery_outcome_refuses() -> None:
    opps = [
        RecoveryOpportunity(
            fault_opportunity_id="fault_1",
            trial_id="trial_1",
            eligible=True,
            recovered=None,
            source_digest=DIGEST_A,
        )
    ]

    result = analyze_conditional_recovery(
        opportunities=opps,
        source_analysis_snapshot_digest=DIGEST_A,
        cohort_key="recovery_test_v1",
    )

    assert result.status == AnalysisStatus.REFUSAL
    assert result.refusal_code == RefusalCode.MISSING_RECOVERY_OUTCOME
    assert result.estimate is None


def test_t12_deterministic_byte_identical_cluster_bootstrap() -> None:
    opps = [
        RecoveryOpportunity(
            fault_opportunity_id=f"fault_{i}_{j}",
            trial_id=f"trial_{i}",
            repeat_group_id=f"task_group_{i // 2}",
            eligible=True,
            recovered=(i + j) % 2 == 0,
            source_digest=DIGEST_A,
        )
        for i in range(10)
        for j in range(3)
    ]

    res1 = analyze_conditional_recovery(
        opportunities=opps,
        source_analysis_snapshot_digest=DIGEST_A,
        cohort_key="recovery_cohort_alpha",
        resamples=1000,
    )
    res2 = analyze_conditional_recovery(
        opportunities=list(reversed(opps)),
        source_analysis_snapshot_digest=DIGEST_A,
        cohort_key="recovery_cohort_alpha",
        resamples=1000,
    )

    assert res1.status == AnalysisStatus.VALID
    assert res1.estimate is not None
    assert res1.interval_lower is not None
    assert res1.interval_upper is not None
    assert res1.n_total == 30
    assert res1.n_effective == 5  # 5 distinct repeat_group_id clusters
    assert res1.estimate == res2.estimate
    assert res1.interval_lower == res2.interval_lower
    assert res1.interval_upper == res2.interval_upper
    assert res1.result_digest == res2.result_digest
    assert res1.seed_digest == res2.seed_digest


# =============================================================================
# T1.3 Cascade Distance Analysis Tests
# =============================================================================


def test_t13_short_trajectory_refusal() -> None:
    trials = [
        CascadeTrialInput(
            trial_id="t_short",
            step_count=4,
            first_error_step=1,
            lock_step=3,
            lock_event_observed=True,
            right_censored=False,
            lock_predicate_id="pred_1",
            lock_predicate_version="v1",
            lock_evidence_ref="cit:1",
            source_digest=DIGEST_A,
        )
    ]

    report = analyze_cascade_distance(
        trials=trials,
        source_analysis_snapshot_digest=DIGEST_A,
    )

    assert len(report.results) == 1
    res = report.results[0]
    assert res.status == CascadeStatus.REFUSED
    assert res.refusal_code == RefusalCode.SHORT_TRAJECTORY
    assert res.cascade_distance is None


def test_t13_observed_cascade_distance_calculation() -> None:
    trials = [
        CascadeTrialInput(
            trial_id="t_observed",
            step_count=10,
            first_error_step=2,
            lock_step=7,
            lock_event_observed=True,
            right_censored=False,
            lock_predicate_id="pred_1",
            lock_predicate_version="v1",
            lock_evidence_ref="cit:1",
            source_digest=DIGEST_A,
        )
    ]

    report = analyze_cascade_distance(
        trials=trials,
        source_analysis_snapshot_digest=DIGEST_A,
    )

    assert len(report.results) == 1
    res = report.results[0]
    assert res.status == CascadeStatus.OBSERVED
    assert res.first_error_step == 2
    assert res.lock_step == 7
    assert res.cascade_distance == 5
    assert res.refusal_code is None


def test_t13_censored_trajectory_handling() -> None:
    trials = [
        CascadeTrialInput(
            trial_id="t_censored",
            step_count=12,
            first_error_step=3,
            lock_step=None,
            lock_event_observed=False,
            right_censored=True,
            censor_step=11,
            lock_predicate_id="pred_1",
            lock_predicate_version="v1",
            lock_evidence_ref=None,
            source_digest=DIGEST_A,
        )
    ]

    report = analyze_cascade_distance(
        trials=trials,
        source_analysis_snapshot_digest=DIGEST_A,
    )

    assert len(report.results) == 1
    res = report.results[0]
    assert res.status == CascadeStatus.CENSORED
    assert res.first_error_step == 3
    assert res.censor_step == 11
    assert res.cascade_distance is None
    assert res.refusal_code is None


@pytest.mark.parametrize(
    ("trial", "expected_code"),
    [
        (
            CascadeTrialInput(
                trial_id="t_missing_terr",
                step_count=8,
                first_error_step=None,
                lock_step=4,
                lock_event_observed=True,
                right_censored=False,
                lock_predicate_id="pred_1",
                lock_predicate_version="v1",
                lock_evidence_ref="cit:1",
                source_digest=DIGEST_A,
            ),
            RefusalCode.T_ERR_UNAVAILABLE,
        ),
        (
            CascadeTrialInput(
                trial_id="t_missing_predicate",
                step_count=8,
                first_error_step=2,
                lock_step=5,
                lock_event_observed=True,
                right_censored=False,
                lock_predicate_id=None,
                lock_predicate_version=None,
                lock_evidence_ref="cit:1",
                source_digest=DIGEST_A,
            ),
            RefusalCode.T_LOCK_UNAVAILABLE,
        ),
        (
            CascadeTrialInput(
                trial_id="t_invalid_order",
                step_count=8,
                first_error_step=5,
                lock_step=2,
                lock_event_observed=True,
                right_censored=False,
                lock_predicate_id="pred_1",
                lock_predicate_version="v1",
                lock_evidence_ref="cit:1",
                source_digest=DIGEST_A,
            ),
            RefusalCode.INVALID_CASCADE_ORDER,
        ),
        (
            CascadeTrialInput(
                trial_id="t_contradictory_censoring",
                step_count=8,
                first_error_step=2,
                lock_step=4,
                lock_event_observed=True,
                right_censored=True,
                censor_step=7,
                lock_predicate_id="pred_1",
                lock_predicate_version="v1",
                lock_evidence_ref="cit:1",
                source_digest=DIGEST_A,
            ),
            RefusalCode.CENSORING_UNAVAILABLE,
        ),
    ],
)
def test_t13_conjunctive_refusals(trial: CascadeTrialInput, expected_code: RefusalCode) -> None:
    report = analyze_cascade_distance(
        trials=[trial],
        source_analysis_snapshot_digest=DIGEST_A,
    )
    res = report.results[0]
    assert res.status == CascadeStatus.REFUSED
    assert res.refusal_code == expected_code
