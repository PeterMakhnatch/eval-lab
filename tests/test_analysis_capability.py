from __future__ import annotations

import pytest
from pydantic import ValidationError

from evallab.analysis_capability import (
    AnalysisMethod,
    AnalysisStatus,
    AnalysisUnit,
    Basis,
    CampaignAnalysisResultV1,
    CampaignAnalysisSpecV1,
    CascadeStatus,
    CascadeTrialInput,
    CIDisposition,
    ContextCitation,
    DenominatorPolicy,
    FeatureContractRow,
    FeatureObservation,
    NextRunAction,
    NextRunFeedbackV1,
    RecoveryOpportunity,
    RefusalCode,
    ReviewQueueEntryV1,
    ReviewQueueArtifactV1,
    RunRecommendationV1,
    Verdict,
    analyze_cascade_distance,
    analyze_conditional_recovery,
    compute_spec_digest,
    create_campaign_analysis_result,
    evaluate_process_outcome_gate,
    run_campaign_analysis,
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


def test_t11_constant_feature_both_outcomes_is_empirical_suspect() -> None:
    """A constant feature at n>=20 with both outcome classes is advisory, not underpowered."""
    contract = FeatureContractRow(
        feature_name="constant_feature",
        is_new_feature=False,
        declared_inputs=("clean_steps",),
        available_before_verdict=True,
        denominator_policy=DenominatorPolicy.NOT_APPLICABLE,
    )
    observations = [
        FeatureObservation(
            feature_name="constant_feature",
            trial_id=f"t_{i}",
            task_success=i < 10,
            value=0,
        )
        for i in range(20)
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
    assert res.empirical.zero_variance is True
    assert res.empirical.sample_degenerate is True
    assert res.empirical.refusal_code is None
    assert res.empirical.n_nonnull == 20


def test_t11_single_outcome_class_distinct_from_underpowered() -> None:
    contract = FeatureContractRow(
        feature_name="one_class_feature",
        is_new_feature=False,
        declared_inputs=("clean_steps",),
        available_before_verdict=True,
        denominator_policy=DenominatorPolicy.NOT_APPLICABLE,
    )
    observations = [
        FeatureObservation(
            feature_name="one_class_feature",
            trial_id=f"t_{i}",
            task_success=True,
            value=float(i),
        )
        for i in range(20)
    ]

    report = evaluate_process_outcome_gate(
        contracts=[contract],
        observations=observations,
        source_analysis_snapshot_digest=DIGEST_A,
        clearance_n=20,
    )

    res = report.results[0]
    assert res.verdict == Verdict.SINGLE_OUTCOME_CLASS
    assert res.basis == Basis.NONE
    assert res.empirical.refusal_code == RefusalCode.SINGLE_OUTCOME_CLASS
    assert res.empirical.n_nonnull == 20


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
            fault_opportunity_id=f"fault_{trial}",
            trial_id=trial,
            repeat_group_id="cell_a",
            repeat_eligible=True,
            task_name="task_a",
            model_name="model_a",
            eligible=True,
            recovered=None,
            source_digest=DIGEST_A,
        )
        for trial in ("trial_1", "trial_2")
    ]

    result = analyze_conditional_recovery(
        opportunities=opps,
        source_analysis_snapshot_digest=DIGEST_A,
        cohort_key="recovery_test_v1",
    )

    assert result.status == AnalysisStatus.REFUSAL
    assert result.refusal_code == RefusalCode.MISSING_RECOVERY_OUTCOME
    assert result.estimate is None


def test_t12_omitted_repeat_eligible_refuses() -> None:
    opps = [
        RecoveryOpportunity(
            fault_opportunity_id=f"fault_{trial}",
            trial_id=trial,
            repeat_group_id="cell_a",
            task_name="task_a",
            model_name="model_a",
            eligible=True,
            recovered=True,
            source_digest=DIGEST_A,
        )
        for trial in ("trial_1", "trial_2")
    ]

    result = analyze_conditional_recovery(
        opportunities=opps,
        source_analysis_snapshot_digest=DIGEST_A,
        cohort_key="recovery_test_v1",
    )

    assert result.status == AnalysisStatus.REFUSAL
    assert result.refusal_code == RefusalCode.REPEAT_INELIGIBLE
    assert result.estimate is None


def test_t12_single_trial_per_task_model_refuses() -> None:
    opps = [
        RecoveryOpportunity(
            fault_opportunity_id="fault_1",
            trial_id="trial_1",
            repeat_group_id="cell_a",
            repeat_eligible=True,
            task_name="task_a",
            model_name="model_a",
            eligible=True,
            recovered=True,
            source_digest=DIGEST_A,
        )
    ]

    result = analyze_conditional_recovery(
        opportunities=opps,
        source_analysis_snapshot_digest=DIGEST_A,
        cohort_key="recovery_test_v1",
    )

    assert result.status == AnalysisStatus.REFUSAL
    assert result.refusal_code == RefusalCode.REPEAT_INELIGIBLE
    assert result.estimate is None


def test_t12_deterministic_byte_identical_cluster_bootstrap() -> None:
    opps = [
        RecoveryOpportunity(
            fault_opportunity_id=f"fault_{i}_{j}",
            trial_id=f"trial_{i}",
            repeat_group_id=f"task_group_{i // 2}",
            repeat_eligible=True,
            task_name=f"task_{i // 2}",
            model_name="model_a",
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


def test_t13_terminal_step_error_lock_and_censor_are_valid() -> None:
    observed = analyze_cascade_distance(
        trials=[
            CascadeTrialInput(
                trial_id="t_terminal_lock",
                step_count=12,
                first_error_step=3,
                lock_step=12,
                lock_event_observed=True,
                right_censored=False,
                lock_predicate_id="pred_1",
                lock_predicate_version="v1",
                lock_evidence_ref="cit:1",
                source_digest=DIGEST_A,
            )
        ],
        source_analysis_snapshot_digest=DIGEST_A,
    ).results[0]
    censored = analyze_cascade_distance(
        trials=[
            CascadeTrialInput(
                trial_id="t_terminal_censor",
                step_count=12,
                first_error_step=3,
                lock_step=None,
                lock_event_observed=False,
                right_censored=True,
                censor_step=12,
                lock_predicate_id=None,
                lock_predicate_version=None,
                lock_evidence_ref=None,
                source_digest=DIGEST_A,
            )
        ],
        source_analysis_snapshot_digest=DIGEST_A,
    ).results[0]
    terminal_error = analyze_cascade_distance(
        trials=[
            CascadeTrialInput(
                trial_id="t_terminal_error",
                step_count=12,
                first_error_step=12,
                lock_step=12,
                lock_event_observed=True,
                right_censored=False,
                lock_predicate_id="pred_1",
                lock_predicate_version="v1",
                lock_evidence_ref="cit:1",
                source_digest=DIGEST_A,
            )
        ],
        source_analysis_snapshot_digest=DIGEST_A,
    ).results[0]

    assert observed.status == CascadeStatus.OBSERVED
    assert observed.lock_step == 12
    assert observed.cascade_distance == 9
    assert observed.refusal_code is None
    assert censored.status == CascadeStatus.CENSORED
    assert censored.censor_step == 12
    assert censored.refusal_code is None
    assert terminal_error.status == CascadeStatus.OBSERVED
    assert terminal_error.cascade_distance == 0
    assert terminal_error.refusal_code is None


def test_t13_zero_based_step_is_unavailable() -> None:
    report = analyze_cascade_distance(
        trials=[
            CascadeTrialInput(
                trial_id="t_zero_based",
                step_count=12,
                first_error_step=0,
                lock_step=11,
                lock_event_observed=True,
                right_censored=False,
                lock_predicate_id="pred_1",
                lock_predicate_version="v1",
                lock_evidence_ref="cit:1",
                source_digest=DIGEST_A,
            )
        ],
        source_analysis_snapshot_digest=DIGEST_A,
    )
    res = report.results[0]
    assert res.status == CascadeStatus.REFUSED
    assert res.refusal_code == RefusalCode.T_ERR_UNAVAILABLE


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


def test_t13_censored_without_lock_predicate_is_censored() -> None:
    """Valid right-censored input must not require a lock predicate or evidence."""
    trials = [
        CascadeTrialInput(
            trial_id="t_censored_no_predicate",
            step_count=12,
            first_error_step=3,
            lock_step=None,
            lock_event_observed=False,
            right_censored=True,
            censor_step=11,
            lock_predicate_id=None,
            lock_predicate_version=None,
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
    assert res.lock_step is None
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


# =============================================================================
# v1 campaign-analysis contracts and runner
# =============================================================================


ZERO_DIGEST = "sha256:" + "0" * 64


def _paired_spec() -> CampaignAnalysisSpecV1:
    body = {
        "schema_version": "campaign-analysis-spec/v1",
        "spec_id": "paired-seed-test",
        "method": AnalysisMethod.PAIRED_SIGN,
        "outcome_feature": "outcome",
        "unit": AnalysisUnit.PAIRED_SEED,
        "unit_keys": ("dose", "seed"),
        "pair_keys": ("seed",),
        "denominator_policy": DenominatorPolicy.NOT_APPLICABLE,
        "ci_method": "none",
        "minimum_informative_units": None,
        "retrieval_inputs_allowed": False,
    }
    spec_digest = compute_spec_digest(body)
    return CampaignAnalysisSpecV1.model_validate({**body, "spec_digest": spec_digest})


def test_exact_paired_units_aggregation() -> None:
    rows = []
    for seed in range(1, 5):  # 4 worse: control succeeds, treatment fails
        rows.append({"dose": 0, "seed": seed, "outcome": 1})
        rows.append({"dose": 1, "seed": seed, "outcome": 0})
    for seed in range(5, 10):  # 5 ties: both fail
        rows.append({"dose": 0, "seed": seed, "outcome": 0})
        rows.append({"dose": 1, "seed": seed, "outcome": 0})

    spec = _paired_spec()
    result = run_campaign_analysis(spec, rows, snapshot_digest=ZERO_DIGEST)

    assert result.observed_rows == 18
    assert result.analysis_units == 9
    assert result.informative_units == 4
    assert result.status == AnalysisStatus.REFUSAL
    assert RefusalCode.UNDERPOWERED in result.refusals
    assert result.p_value == pytest.approx(0.125)
    assert result.attainable_p_floor == pytest.approx(0.125)
    assert result.estimate == pytest.approx(4 / 9)


def test_spec_digest_excludes_spec_digest_field() -> None:
    body = {
        "schema_version": "campaign-analysis-spec/v1",
        "spec_id": "digest-test",
        "method": AnalysisMethod.RATE_WILSON,
        "outcome_feature": "success",
        "unit": AnalysisUnit.TRIAL,
        "unit_keys": ("trial_id",),
        "denominator_policy": DenominatorPolicy.NOT_APPLICABLE,
        "ci_method": "wilson",
        "retrieval_inputs_allowed": False,
    }
    d1 = compute_spec_digest(body)
    body["spec_digest"] = d1
    d2 = compute_spec_digest(body)
    assert d1 == d2
    body["spec_id"] = "different"
    d3 = compute_spec_digest(body)
    assert d3 != d1


def test_result_digest_binds_content_identity() -> None:
    body = {
        "schema_version": "campaign-analysis-result/v1",
        "spec_id": "r1",
        "spec_digest": DIGEST_A,
        "snapshot_digest": DIGEST_B,
        "status": AnalysisStatus.VALID,
        "refusals": (),
        "observed_rows": 10,
        "analysis_units": 10,
        "source_refs": (ContextCitation(path="test.json", digest=DIGEST_A),),
    }
    r1 = create_campaign_analysis_result(**body)
    r2 = create_campaign_analysis_result(**body)
    assert r1.result_digest == r2.result_digest

    r3 = create_campaign_analysis_result(**{**body, "observed_rows": 11})
    assert r3.result_digest != r1.result_digest


def test_review_queue_entry_is_not_decision_result() -> None:
    entry = ReviewQueueEntryV1(
        rank=1,
        job_id="j1",
        trial_id="t1",
        source_cas_uri="cas://sha256/" + "1" * 64,
        citation=ContextCitation(path="test.json"),
        window_start_step=0,
        window_end_step=1,
        window_digest=DIGEST_A,
        distance=0.1,
        reason="similar_exemplar",
    )
    with pytest.raises(ValidationError):
        CampaignAnalysisResultV1.model_validate(entry.model_dump(mode="json"))

    artifact = ReviewQueueArtifactV1(
        queue_id="q1",
        queue_digest=DIGEST_A,
        manifest_digest=DIGEST_A,
        snapshot_digest=DIGEST_A,
        policy={"schema_version": "retrieval-policy/v1", "embedder_digest": DIGEST_A, "redaction_policy_digest": DIGEST_A},
        query_digest=DIGEST_A,
        candidate_pool_digest=DIGEST_A,
        index_digest=DIGEST_A,
        coverage_complete=False,
    )
    with pytest.raises(ValidationError):
        CampaignAnalysisResultV1.model_validate(artifact.model_dump(mode="json"))


def test_next_run_feedback_is_unauthorized() -> None:
    recommendation = RunRecommendationV1(
        action=NextRunAction.HOLD_SEMANTIC_DECISION_ANALYSIS,
        basis_result_digests=(DIGEST_A,),
        target_estimand="outcome",
        target_unit=AnalysisUnit.TRIAL,
        blocking=True,
    )
    feedback = NextRunFeedbackV1(
        source_report_digest=DIGEST_A,
        source_snapshot_digest=DIGEST_B,
        recommendations=(recommendation,),
        feedback_digest=DIGEST_A,  # will be recomputed/validated by tests
    )
    assert feedback.execution_authorized is False
    assert feedback.authorizing_actor_required is True
    assert feedback.feedback_digest.startswith("sha256:")


def test_run_campaign_analysis_refuses_unsupported_or_empty() -> None:
    spec_body = {
        "schema_version": "campaign-analysis-spec/v1",
        "spec_id": "unsupported",
        "method": AnalysisMethod.FISHER_2X2,
        "outcome_feature": "outcome",
        "unit": AnalysisUnit.TRIAL,
        "unit_keys": ("trial_id",),
        "denominator_policy": DenominatorPolicy.NOT_APPLICABLE,
        "ci_method": "none",
        "retrieval_inputs_allowed": False,
    }
    spec = CampaignAnalysisSpecV1.model_validate(
        {**spec_body, "spec_digest": compute_spec_digest(spec_body)}
    )
    result = run_campaign_analysis(spec, [{"outcome": 1}])
    assert result.status == AnalysisStatus.REFUSAL
    assert RefusalCode.UNSUPPORTED_ANALYSIS_METHOD in result.refusals


def test_predictor_outcome_lineage_violation_refuses() -> None:
    from evallab.interpretation.feature_registry import FeatureDefinition, FeatureRegistry

    registry = FeatureRegistry()
    registry.register(
        FeatureDefinition(
            column_name="defines_outcome",
            category="action_memory",
            data_type="BOOLEAN",
            description="Defines outcome feature",
            declared_inputs=("invariants_passed",),
            available_before_verdict=True,
            denominator_policy="not_applicable",
            verdict_coupling="defines",
            coupling_basis="exact verifier basis",
        )
    )
    spec_body = {
        "schema_version": "campaign-analysis-spec/v1",
        "spec_id": "lineage-violation-test",
        "method": AnalysisMethod.FISHER_2X2,
        "outcome_feature": "defines_outcome",
        "predictor_features": ("defines_outcome",),
        "unit": AnalysisUnit.TRIAL,
        "unit_keys": ("trial_id",),
        "denominator_policy": DenominatorPolicy.NOT_APPLICABLE,
        "ci_method": "none",
        "retrieval_inputs_allowed": False,
    }
    spec = CampaignAnalysisSpecV1.model_validate(
        {**spec_body, "spec_digest": compute_spec_digest(spec_body)}
    )
    result = run_campaign_analysis(spec, [{"defines_outcome": 1}], feature_registry=registry)
    assert result.status == AnalysisStatus.REFUSAL
    assert RefusalCode.OUTCOME_LINEAGE_VIOLATION in result.refusals


def test_predictor_missing_lineage_declaration_refuses() -> None:
    from evallab.interpretation.feature_registry import FeatureRegistry

    registry = FeatureRegistry()
    spec_body = {
        "schema_version": "campaign-analysis-spec/v1",
        "spec_id": "missing-lineage-test",
        "method": AnalysisMethod.FISHER_2X2,
        "outcome_feature": "unregistered_outcome",
        "predictor_features": ("unregistered_pred",),
        "unit": AnalysisUnit.TRIAL,
        "unit_keys": ("trial_id",),
        "denominator_policy": DenominatorPolicy.NOT_APPLICABLE,
        "ci_method": "none",
        "retrieval_inputs_allowed": False,
    }
    spec = CampaignAnalysisSpecV1.model_validate(
        {**spec_body, "spec_digest": compute_spec_digest(spec_body)}
    )
    result = run_campaign_analysis(
        spec, [{"unregistered_outcome": 1, "unregistered_pred": 1}], feature_registry=registry
    )
    assert result.status == AnalysisStatus.REFUSAL
    assert RefusalCode.MISSING_LINEAGE_DECLARATION in result.refusals
