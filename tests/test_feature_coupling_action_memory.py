"""Tests for Feature Verdict-Coupling & Action Memory Capture Fidelity."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.interpretation.benchmark_events import (
    BenchmarkContractRecord,
    BenchmarkEventRecord,
    CorrelatedToolCall,
    FinalStateRecord,
    TrialBundle,
    load_trial_bundle,
)
from evallab.interpretation.feature_registry import (
    TRAJECTORY_FEATURE_REGISTRY,
    FeatureDefinition,
    FeatureRegistry,
    audit_predictor_eligibility,
    audit_registry_predictor_eligibility,
    audit_verdict_coupling,
)
from evallab.interpretation.producers.action_memory import (
    extract_action_memory_features,
)


def _build_mock_bundle(
    tmp_path: Path,
    *,
    expected_chunk_ids: list[str],
    events: list[dict[str, Any]],
    target_entity: str = "entity_42",
    target_attribute: str = "routing_key",
    latest_value: str = "target_val",
    initial_value: str = "initial_val",
    invariants_passed: bool = True,
    raw_dir: Path | None = None,
) -> TrialBundle:
    trial_dir = tmp_path / "mock_trial"
    trial_dir.mkdir(parents=True, exist_ok=True)

    contract = {
        "family": "action-memory-v1",
        "version": "1.0.0",
        "construct": "Context & Actionable Memory",
        "seed": 42,
        "cell_factors": {
            "expected_chunk_ids": expected_chunk_ids,
            "target_entity": target_entity,
            "target_attribute": target_attribute,
            "latest_value": latest_value,
            "initial_value": initial_value,
        },
        "task_id": "action-64k-semantic_distractor-s__8aYeUds",
        "opportunity_counts": {
            "read_opportunity_count": len(expected_chunk_ids),
            "mutation_opportunity_count": 1,
            "update_opportunity_count": 1,
        },
        "verifier_truth_digest": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    }
    (trial_dir / "benchmark-contract.json").write_text(json.dumps(contract), encoding="utf-8")
    (trial_dir / "benchmark-events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    (trial_dir / "final-state.json").write_text(
        json.dumps({
            "invariants_passed": invariants_passed,
            "mutations": [
                {
                    "entity_id": target_entity,
                    "attribute": target_attribute,
                    "bound_value": latest_value,
                }
            ],
        }),
        encoding="utf-8",
    )

    bundle = load_trial_bundle(trial_dir)
    if raw_dir is not None:
        # Override raw_dir for testing file-based ATIF discovery
        object.__setattr__(bundle, "raw_dir", raw_dir) if hasattr(bundle, "__dict__") else None
    return bundle


def test_action_memory_522_257_case(tmp_path: Path):
    """Load/mock a trial matching action-64k-semantic_distractor-s__8aYeUds where

    expected reads = 257, total issued reads = 522 (257 valid, 264 duplicates, 1 unknown).
    """
    expected_chunk_ids = [f"chunk_{i:03d}" for i in range(257)]

    events: list[dict[str, Any]] = []
    event_idx = 0

    # 1. First read all 257 valid chunks
    for cid in expected_chunk_ids:
        events.append({
            "event_index": event_idx,
            "event_type": "mcp_call",
            "call_id": f"call_{event_idx}",
            "tool_name": "get_context_chunk",
            "arguments": {"chunk_id": cid},
        })
        event_idx += 1

    # 2. Add 264 duplicate reads (repeating chunk_000)
    for _ in range(264):
        events.append({
            "event_index": event_idx,
            "event_type": "mcp_call",
            "call_id": f"call_{event_idx}",
            "tool_name": "get_context_chunk",
            "arguments": {"chunk_id": "chunk_000"},
        })
        event_idx += 1

    # 3. Add 1 unknown read
    events.append({
        "event_index": event_idx,
        "event_type": "mcp_call",
        "call_id": f"call_{event_idx}",
        "tool_name": "get_context_chunk",
        "arguments": {"chunk_id": "chunk_unknown_999"},
    })
    event_idx += 1

    # 4. Mutation call
    events.append({
        "event_index": event_idx,
        "event_type": "execute_mutation",
        "payload": {
            "entity_id": "entity_42",
            "attribute": "routing_key",
            "bound_value": "target_val",
        },
    })

    assert len(events) == 523  # 522 chunk reads + 1 mutation
    bundle = _build_mock_bundle(tmp_path, expected_chunk_ids=expected_chunk_ids, events=events)
    feat = extract_action_memory_features(bundle)

    assert feat.expected_handle_count == 257
    assert feat.issued_handle_count == 522
    assert feat.valid_handle_count == 257
    assert feat.duplicate_handle_count == 264
    assert feat.unknown_handle_count == 1
    assert feat.handle_issuance_ratio == pytest.approx(522 / 257)
    assert feat.handle_order_match is False
    assert feat.handle_set_match is False


def test_action_memory_zero_denominator_preserves_null(tmp_path: Path):
    """With expected_handle_count == 0, assert handle_coverage_rate is None and handle_issuance_ratio is None."""
    events: list[dict[str, Any]] = [
        {
            "event_index": 0,
            "event_type": "execute_mutation",
            "payload": {
                "entity_id": "entity_42",
                "attribute": "routing_key",
                "bound_value": "target_val",
            },
        }
    ]
    bundle = _build_mock_bundle(tmp_path, expected_chunk_ids=[], events=events)
    feat = extract_action_memory_features(bundle)

    assert feat.expected_handle_count == 0
    assert feat.handle_coverage_rate is None
    assert feat.handle_issuance_ratio is None


def test_action_memory_duplicate_accounting(tmp_path: Path):
    """Ingest a sequence with duplicate requests (5 total requests for 3 distinct handles)."""
    expected_chunk_ids = ["chunk_a", "chunk_b", "chunk_c"]
    requested_sequence = ["chunk_a", "chunk_b", "chunk_c", "chunk_a", "chunk_b"]

    events: list[dict[str, Any]] = []
    for idx, cid in enumerate(requested_sequence):
        events.append({
            "event_index": idx,
            "event_type": "mcp_call",
            "call_id": f"call_{idx}",
            "tool_name": "read_chunk",
            "arguments": {"chunk_id": cid},
        })
    events.append({
        "event_index": len(requested_sequence),
        "event_type": "execute_mutation",
        "payload": {
            "entity_id": "entity_42",
            "attribute": "routing_key",
            "bound_value": "target_val",
        },
    })

    bundle = _build_mock_bundle(tmp_path, expected_chunk_ids=expected_chunk_ids, events=events)
    feat = extract_action_memory_features(bundle)

    assert feat.issued_handle_count == 5
    assert feat.duplicate_handle_count == 2
    assert feat.valid_handle_count == 3
    assert feat.unknown_handle_count == 0
    assert feat.handle_issuance_ratio == pytest.approx(5 / 3)


def test_treatment_correlated_capture_mismatch(tmp_path: Path):
    """Test ATIF capture concordance diagnostics under mismatch, concordance, and absent ATIF."""
    expected_chunk_ids = ["chunk_001", "chunk_002"]
    events: list[dict[str, Any]] = [
        {
            "event_index": 0,
            "event_type": "mcp_call",
            "call_id": "c1",
            "tool_name": "get_context_chunk",
            "arguments": {"chunk_id": "chunk_001"},
        },
        {
            "event_index": 1,
            "event_type": "mcp_call",
            "call_id": "c2",
            "tool_name": "get_context_chunk",
            "arguments": {"chunk_id": "chunk_002"},
        },
    ]

    bundle = _build_mock_bundle(tmp_path, expected_chunk_ids=expected_chunk_ids, events=events)

    # 1. ATIF handle sequence differs from benchmark events (e.g. reversed order)
    atif_mismatch = {
        "steps": [
            {
                "tool_calls": [
                    {"name": "get_context_chunk", "arguments": {"chunk_id": "chunk_002"}},
                    {"name": "get_context_chunk", "arguments": {"chunk_id": "chunk_001"}},
                ]
            }
        ]
    }
    feat_mismatch = extract_action_memory_features(bundle, atif_trajectory=atif_mismatch)
    assert feat_mismatch.handle_order_concordance is False
    assert feat_mismatch.capture_concordance_status == "mismatch"
    assert feat_mismatch.retrieval_authority == "benchmark_events"

    # 2. ATIF handle sequence matches benchmark events exactly
    atif_concordant = {
        "steps": [
            {
                "tool_calls": [
                    {"name": "get_context_chunk", "arguments": {"chunk_id": "chunk_001"}},
                    {"name": "get_context_chunk", "arguments": {"chunk_id": "chunk_002"}},
                ]
            }
        ]
    }
    feat_concordant = extract_action_memory_features(bundle, atif_trajectory=atif_concordant)
    assert feat_concordant.handle_order_concordance is True
    assert feat_concordant.capture_concordance_status == "concordant"
    assert feat_concordant.retrieval_authority == "benchmark_events"

    # 3. ATIF is absent
    feat_absent = extract_action_memory_features(bundle, atif_trajectory=None)
    assert feat_absent.handle_order_concordance is None
    assert feat_absent.capture_concordance_status == "atif_unavailable"
    assert feat_absent.retrieval_authority == "benchmark_events"

    # 4. ATIF from file path
    traj_path = tmp_path / "trajectory.json"
    traj_path.write_text(json.dumps(atif_concordant), encoding="utf-8")
    feat_file = extract_action_memory_features(bundle, atif_trajectory=traj_path)
    assert feat_file.handle_order_concordance is True
    assert feat_file.capture_concordance_status == "concordant"


def test_predictor_audit_refusal():
    """Verify audit_predictor_eligibility returns expected refusal codes."""
    # 1. Reward definition leakage (defines)
    feat_defines = FeatureDefinition(
        column_name="test_defines",
        data_type="BOOLEAN",
        category="benchmark_l1_fact",
        is_screening=False,
        source_table="benchmark_events",
        formula_or_rule="test",
        null_condition="never",
        description="test",
        available_before_verdict=True,
        verdict_coupling="defines",
        coupling_basis="Verifier reward is directly defined by this",
    )
    assert audit_predictor_eligibility(feat_defines) == "REWARD_DEFINITION_LEAKAGE"

    # 2. Undeclared verdict coupling
    feat_undeclared = FeatureDefinition(
        column_name="test_undeclared",
        data_type="BOOLEAN",
        category="benchmark_l1_fact",
        is_screening=False,
        source_table="benchmark_events",
        formula_or_rule="test",
        null_condition="never",
        description="test",
        available_before_verdict=True,
        verdict_coupling=None,
    )
    assert audit_predictor_eligibility(feat_undeclared) == "UNDECLARED_VERDICT_COUPLING"

    # 3. Post-verdict temporal violation
    feat_post_verdict = FeatureDefinition(
        column_name="test_post_verdict",
        data_type="BOOLEAN",
        category="benchmark_l1_fact",
        is_screening=False,
        source_table="benchmark_events",
        formula_or_rule="test",
        null_condition="never",
        description="test",
        available_before_verdict=False,
        verdict_coupling="independent",
    )
    assert audit_predictor_eligibility(feat_post_verdict) == "POST_VERDICT_TEMPORAL_VIOLATION"

    # 4. Missing temporal availability
    feat_missing_temp = FeatureDefinition(
        column_name="test_missing_temp",
        data_type="BOOLEAN",
        category="benchmark_l1_fact",
        is_screening=False,
        source_table="benchmark_events",
        formula_or_rule="test",
        null_condition="never",
        description="test",
        available_before_verdict=None,
        verdict_coupling="independent",
    )
    assert audit_predictor_eligibility(feat_missing_temp) == "MISSING_TEMPORAL_AVAILABILITY"

    # 5. Missing coupling evidence basis
    feat_no_basis = FeatureDefinition(
        column_name="test_no_basis",
        data_type="BOOLEAN",
        category="benchmark_l1_fact",
        is_screening=False,
        source_table="benchmark_events",
        formula_or_rule="test",
        null_condition="never",
        description="test",
        available_before_verdict=True,
        verdict_coupling="correlates",
        coupling_basis=None,
    )
    assert audit_predictor_eligibility(feat_no_basis) == "MISSING_COUPLING_EVIDENCE_BASIS"

    feat_empty_basis = FeatureDefinition(
        column_name="test_empty_basis",
        data_type="BOOLEAN",
        category="benchmark_l1_fact",
        is_screening=False,
        source_table="benchmark_events",
        formula_or_rule="test",
        null_condition="never",
        description="test",
        available_before_verdict=True,
        verdict_coupling="correlates",
        coupling_basis="   ",
    )
    assert audit_predictor_eligibility(feat_empty_basis) == "MISSING_COUPLING_EVIDENCE_BASIS"

    # 6. Not applicable for prediction
    feat_not_app = FeatureDefinition(
        column_name="test_not_app",
        data_type="VARCHAR",
        category="identity",
        is_screening=False,
        source_table="benchmark_events",
        formula_or_rule="test",
        null_condition="never",
        description="test",
        available_before_verdict=True,
        verdict_coupling="not_applicable",
    )
    assert audit_predictor_eligibility(feat_not_app) == "NOT_APPLICABLE_FOR_PREDICTION"

    # 7. Independent feature with available_before_verdict=True returns None (eligible)
    feat_independent = FeatureDefinition(
        column_name="test_independent",
        data_type="BIGINT",
        category="benchmark_l1_fact",
        is_screening=False,
        source_table="benchmark_events",
        formula_or_rule="test",
        null_condition="never",
        description="test",
        available_before_verdict=True,
        verdict_coupling="independent",
    )
    assert audit_predictor_eligibility(feat_independent) is None

    # 8. Strict independence check on correlates
    feat_correlates = FeatureDefinition(
        column_name="test_correlates",
        data_type="DOUBLE",
        category="benchmark_l2_metric",
        is_screening=False,
        source_table="benchmark_events",
        formula_or_rule="test",
        null_condition="never",
        description="test",
        available_before_verdict=True,
        verdict_coupling="correlates",
        coupling_basis="Correlates with search depth",
    )
    assert audit_predictor_eligibility(feat_correlates, strict_independence=False) is None
    assert audit_predictor_eligibility(feat_correlates, strict_independence=True) == "VERDICT_CORRELATED"


def test_feature_definition_contract_validation():
    """Verify FeatureDefinition.validate_contract() catches invalid coupling configurations."""
    # Invalid unrecognized verdict_coupling
    feat_invalid_type = FeatureDefinition(
        column_name="test_invalid",
        data_type="BIGINT",
        category="benchmark_l1_fact",
        is_screening=False,
        source_table="benchmark_events",
        formula_or_rule="test",
        null_condition="never",
        description="test",
        verdict_coupling="invalid_coupling_string",  # type: ignore[arg-type]
    )
    errors = feat_invalid_type.validate_contract()
    assert any("has invalid verdict_coupling" in err for err in errors)

    registry = FeatureRegistry()
    with pytest.raises(ValueError, match="Invalid feature definition"):
        registry.register(feat_invalid_type)

    # Missing coupling basis on 'correlates'
    feat_correlates_no_basis = FeatureDefinition(
        column_name="test_correlates_bad",
        data_type="BIGINT",
        category="benchmark_l1_fact",
        is_screening=False,
        source_table="benchmark_events",
        formula_or_rule="test",
        null_condition="never",
        description="test",
        verdict_coupling="correlates",
        coupling_basis="",
    )
    errors = feat_correlates_no_basis.validate_contract()
    assert any("requires non-empty coupling_basis" in err for err in errors)

    # Missing coupling basis on 'defines'
    feat_defines_no_basis = FeatureDefinition(
        column_name="test_defines_bad",
        data_type="BIGINT",
        category="benchmark_l1_fact",
        is_screening=False,
        source_table="benchmark_events",
        formula_or_rule="test",
        null_condition="never",
        description="test",
        verdict_coupling="defines",
        coupling_basis=None,
    )
    errors = feat_defines_no_basis.validate_contract()
    assert any("requires non-empty coupling_basis" in err for err in errors)

    # Valid contract passes
    feat_valid = FeatureDefinition(
        column_name="test_valid",
        data_type="BIGINT",
        category="benchmark_l1_fact",
        is_screening=False,
        source_table="benchmark_events",
        formula_or_rule="test",
        null_condition="never",
        description="test",
        verdict_coupling="correlates",
        coupling_basis="Documented valid basis",
    )
    assert feat_valid.validate_contract() == []
    assert audit_verdict_coupling(feat_valid) is None


def test_registered_action_memory_features_audit():
    """Verify all registered Action Memory features pass contract validation and predictor audits."""
    action_memory_feats = TRAJECTORY_FEATURE_REGISTRY.by_family("action-memory-v1")
    assert len(action_memory_feats) > 0

    # Ensure all registered action-memory features have valid contracts
    for name, feat in action_memory_feats.items():
        assert feat.validate_contract() == [], f"Feature {name} has contract errors"

    refusals = audit_registry_predictor_eligibility(family="action-memory-v1")

    # Features defining verdict must be refused with REWARD_DEFINITION_LEAKAGE
    assert refusals.get("handle_set_match") == "REWARD_DEFINITION_LEAKAGE"
    assert refusals.get("handle_order_match") == "REWARD_DEFINITION_LEAKAGE"
    assert refusals.get("handle_coverage_rate") == "REWARD_DEFINITION_LEAKAGE"
    assert refusals.get("binding_matched") == "REWARD_DEFINITION_LEAKAGE"
    assert refusals.get("binding_survival_rate") == "REWARD_DEFINITION_LEAKAGE"
    assert refusals.get("stale_value_override_rate") == "REWARD_DEFINITION_LEAKAGE"

    # Features not applicable must be refused with NOT_APPLICABLE_FOR_PREDICTION
    assert refusals.get("bound_target_entity") == "NOT_APPLICABLE_FOR_PREDICTION"
    assert refusals.get("bound_target_attribute") == "NOT_APPLICABLE_FOR_PREDICTION"
    assert refusals.get("bound_target_value") == "NOT_APPLICABLE_FOR_PREDICTION"
    assert refusals.get("retrieval_authority") == "NOT_APPLICABLE_FOR_PREDICTION"
    assert refusals.get("capture_concordance_status") == "NOT_APPLICABLE_FOR_PREDICTION"

    # Independent features with temporal availability must not be refused
    assert "expected_handle_count" not in refusals
    assert "raw_binding_opportunities" not in refusals
    assert "raw_conflicting_opportunities" not in refusals
    assert "context_burn_velocity" not in refusals
