"""Focused tests for Feature Governance Control, Refusal Reconciliation, and Operator Views."""

from __future__ import annotations

import duckdb

from evallab.analysis_capability import RefusalCode as AnalysisRefusalCode
from evallab.interpretation.feature_registry import (
    TRAJECTORY_FEATURE_REGISTRY,
    FeatureDefinition,
    FeatureRefusalCode,
    audit_predictor_eligibility,
    audit_registry_denominator_policies,
    audit_registry_predictor_eligibility,
    audit_verdict_coupling,
    create_predictor_eligibility_duckdb_view,
    predictor_eligibility_summary,
    predictor_eligibility_view,
    verify_feature_registry,
)
from evallab.multi_eval import RefusalCode as MultiEvalRefusalCode


def test_feature_registry_zero_contract_errors() -> None:
    """Producer CI validation passes with zero contract errors across all registered features."""
    errors = verify_feature_registry()
    assert errors == [], f"Unexpected contract errors in registry: {errors}"


def test_feature_registry_zero_temporal_gaps() -> None:
    """All 240 features in the registry have an explicit temporal availability declaration."""
    features = TRAJECTORY_FEATURE_REGISTRY.all_features()
    assert len(features) == 240
    missing_temp = [
        name for name, feat in features.items() if feat.available_before_verdict is None
    ]
    assert missing_temp == [], f"Features with missing temporal availability: {missing_temp}"


def test_feature_registry_zero_denominator_gaps() -> None:
    """All registered features have explicit denominator policies and valid siblings."""
    debt = audit_registry_denominator_policies()
    assert debt == {}, f"Unexpected denominator debt in registry: {debt}"


def test_feature_registry_zero_coupling_gaps() -> None:
    """All registered features have explicit verdict coupling and required coupling bases."""
    features = TRAJECTORY_FEATURE_REGISTRY.all_features()
    coupling_refusals = {
        name: audit
        for name, feat in features.items()
        if (audit := audit_verdict_coupling(feat)) is not None
    }
    assert coupling_refusals == {}, f"Unexpected coupling refusals: {coupling_refusals}"


def test_action_memory_coupling_refusals_resolved() -> None:
    """Action memory handle count features have explicit correlates coupling declarations."""
    for handle_feat_name in (
        "valid_handle_count",
        "unknown_handle_count",
        "duplicate_handle_count",
    ):
        feat = TRAJECTORY_FEATURE_REGISTRY.get(handle_feat_name)
        assert feat is not None, f"Missing feature {handle_feat_name}"
        assert feat.available_before_verdict is True
        assert feat.denominator_policy == "not_applicable"
        assert feat.verdict_coupling == "correlates"
        assert feat.coupling_basis is not None and len(feat.coupling_basis.strip()) > 0
        assert audit_verdict_coupling(feat) is None
        assert audit_predictor_eligibility(feat) is None


def test_null_on_zero_ratio_contracts_name_valid_denominator_siblings() -> None:
    """All screening and benchmark ratios with null-on-zero name existing registered denominator siblings."""
    features = TRAJECTORY_FEATURE_REGISTRY.all_features()
    for name, feat in features.items():
        if feat.null_on_zero_denominator:
            assert feat.denominator_policy == "required"
            assert feat.denominator_sibling is not None, (
                f"Feature {name} has no denominator sibling"
            )
            assert feat.denominator_sibling in features, (
                f"Feature {name} references non-existent denominator sibling {feat.denominator_sibling!r}"
            )
            sibling = features[feat.denominator_sibling]
            assert sibling.data_type in (
                "BIGINT",
                "DOUBLE",
            ), f"Sibling {sibling.column_name} is not numeric"


def test_reward_definition_and_post_verdict_exclusions_refused() -> None:
    """Reward-definition leakage (verdict_coupling='defines') and post-verdict exclusions remain refused."""
    refusals = audit_registry_predictor_eligibility()

    # 10 Post-verdict temporal exclusions
    post_verdict_expected = {
        "primary_reward",
        "causal_consistency_rate",
        "certified_recovered_faults",
        "autonomous_recovery_rate",
        "fault_recovery_latency",
        "controlled_replay_outcome_delta",
        "recovery_succeeded_at_persistence",
        "hidden_score",
        "visible_hidden_transfer_gap",
        "artifact_replay_verified",
    }
    for feat_name in post_verdict_expected:
        assert refusals.get(feat_name) == FeatureRefusalCode.POST_VERDICT_TEMPORAL_VIOLATION, (
            f"Expected POST_VERDICT_TEMPORAL_VIOLATION for {feat_name}"
        )

    # 19 Reward-definition leakage exclusions
    defines_expected = {
        "binding_matched",
        "handle_set_match",
        "handle_order_match",
        "handle_coverage_rate",
        "binding_survival_rate",
        "stale_value_override_rate",
        "conflict_resolution_success",
        "retained_obsolete_fact_count",
        "selective_forgetting_success",
        "temporal_consistency_rate",
        "executed_dag_edges",
        "correct_value_bindings",
        "cycle_violations",
        "satisfied_edge_opportunities",
        "value_propagation_accuracy",
        "dag_edge_conformance_rate",
        "milestone_progress_rate",
        "state_dependency_satisfaction_rate",
        "leakage_detected_flag",
    }
    for feat_name in defines_expected:
        assert refusals.get(feat_name) == FeatureRefusalCode.REWARD_DEFINITION_LEAKAGE, (
            f"Expected REWARD_DEFINITION_LEAKAGE for {feat_name}"
        )


def test_predictor_eligibility_operator_view_surface() -> None:
    """Operator projection view returns exactly one row per registered feature with exact fields."""
    rows = predictor_eligibility_view()
    assert len(rows) == 240

    names = {r.feature_name for r in rows}
    assert len(names) == 240
    assert names == set(TRAJECTORY_FEATURE_REGISTRY.all_features().keys())

    # Check row contracts
    for r in rows:
        assert isinstance(r.feature_name, str) and len(r.feature_name) > 0
        assert r.data_type in ("VARCHAR", "BIGINT", "DOUBLE", "BOOLEAN")
        assert r.category in (
            "identity",
            "mechanical_fact",
            "screening_heuristic",
            "benchmark_ground_truth",
            "benchmark_l1_fact",
            "benchmark_l2_metric",
        )
        assert isinstance(r.predictor_eligible, bool)
        assert isinstance(r.predictor_refusal_code, str)
        if r.predictor_eligible:
            assert r.predictor_refusal_code == "ELIGIBLE"
        else:
            assert r.predictor_refusal_code in (
                "NOT_APPLICABLE_FOR_PREDICTION",
                "POST_VERDICT_TEMPORAL_VIOLATION",
                "REWARD_DEFINITION_LEAKAGE",
            )


def test_predictor_eligibility_summary_counts() -> None:
    """Summary counts match exact cleared counts without requiring custom scripts."""
    summary = predictor_eligibility_summary()
    assert summary.total_features == 240
    assert summary.eligible_predictors == 171
    assert summary.refused_predictors == 69
    assert summary.missing_temporal_count == 0
    assert summary.missing_denominator_count == 0
    assert summary.undeclared_coupling_count == 0
    assert summary.refusals_by_code == {
        "NOT_APPLICABLE_FOR_PREDICTION": 40,
        "POST_VERDICT_TEMPORAL_VIOLATION": 10,
        "REWARD_DEFINITION_LEAKAGE": 19,
    }


def test_duckdb_v_predictor_eligibility_view_materialization() -> None:
    """DuckDB view v_predictor_eligibility materializes cleanly and yields exact counts."""
    conn = duckdb.connect(":memory:")
    create_predictor_eligibility_duckdb_view(conn)

    total = conn.execute("SELECT count(*) FROM v_predictor_eligibility").fetchone()[0]
    assert total == 240

    eligible = conn.execute(
        "SELECT count(*) FROM v_predictor_eligibility WHERE predictor_eligible"
    ).fetchone()[0]
    assert eligible == 171

    refused = conn.execute(
        "SELECT count(*) FROM v_predictor_eligibility WHERE NOT predictor_eligible"
    ).fetchone()[0]
    assert refused == 69

    breakdown = dict(
        conn.execute(
            "SELECT predictor_refusal_code, count(*) FROM v_predictor_eligibility WHERE NOT predictor_eligible GROUP BY 1"
        ).fetchall()
    )
    assert breakdown == {
        "NOT_APPLICABLE_FOR_PREDICTION": 40,
        "POST_VERDICT_TEMPORAL_VIOLATION": 10,
        "REWARD_DEFINITION_LEAKAGE": 19,
    }


def test_refusal_codes_are_namespaced_by_owning_module() -> None:
    """Feature, analysis, and runner-planning refusals remain distinct typed domains."""
    assert MultiEvalRefusalCode is not AnalysisRefusalCode
    assert MultiEvalRefusalCode is not FeatureRefusalCode
    assert AnalysisRefusalCode is not FeatureRefusalCode
    assert MultiEvalRefusalCode.UNSUPPORTED_WINDOWS == "unsupported_windows"
    assert AnalysisRefusalCode.MISSING_LINEAGE_DECLARATION == "MISSING_LINEAGE_DECLARATION"
    assert FeatureRefusalCode.MISSING_TEMPORAL_AVAILABILITY == "MISSING_TEMPORAL_AVAILABILITY"
    assert FeatureRefusalCode.POST_VERDICT_TEMPORAL_VIOLATION == "POST_VERDICT_TEMPORAL_VIOLATION"
    assert FeatureRefusalCode.REWARD_DEFINITION_LEAKAGE == "REWARD_DEFINITION_LEAKAGE"
    assert FeatureRefusalCode.ELIGIBLE == "ELIGIBLE"


def test_contract_validation_rejection() -> None:
    """FeatureDefinition.validate_contract() rejects missing denominator siblings and malformed coupling."""
    # Missing denominator sibling for required policy
    feat_missing_sibling = FeatureDefinition(
        column_name="test_rate_screening",
        data_type="DOUBLE",
        category="screening_heuristic",
        is_screening=True,
        source_table="traj_features",
        formula_or_rule="a / b",
        null_condition="NULL",
        description="test",
        denominator_policy="required",
        denominator_sibling=None,
        null_on_zero_denominator=True,
        available_before_verdict=True,
        verdict_coupling="correlates",
        coupling_basis="test basis",
    )
    errors = feat_missing_sibling.validate_contract()
    assert any("no denominator_sibling declared" in err for err in errors)

    # Missing coupling basis for defines coupling
    feat_missing_basis = FeatureDefinition(
        column_name="test_feat",
        data_type="BIGINT",
        category="mechanical_fact",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="count",
        null_condition="0",
        description="test",
        denominator_policy="not_applicable",
        available_before_verdict=True,
        verdict_coupling="defines",
        coupling_basis=None,
    )
    errors = feat_missing_basis.validate_contract()
    assert any("requires non-empty coupling_basis" in err for err in errors)
