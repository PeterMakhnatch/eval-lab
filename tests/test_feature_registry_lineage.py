"""T1.1 paperwork regression: every gated baseline column declares honest lineage.

The 46 names below are the MISSING_LINEAGE_DECLARATION verdicts from the first
T1.1 discrimination-gate run on the featured corpus
(research/analysis/t11_discrimination_gate.json, t11_report.report_digest
sha256:e639174c357c8858d31a415d772f68d1eb2afb5a3ae0ea95a785388dbf65cd9b).
Each registered column must carry lineage metadata naming its producing source,
or an explicit unknown marker where no producer exists, plus the denominator
paperwork the gate reads through feature_contract_row. The list is frozen here
on purpose: re-running the gate input step regenerates the artifact with the
verdicts cleared, so the test must not read the artifact to know its scope.
"""

from __future__ import annotations

import pytest

from evallab.interpretation.feature_registry import (
    TRAJECTORY_FEATURE_REGISTRY,
    audit_denominator_policy,
    audit_lineage_declaration,
    feature_contract_row,
)

T11_GATED_BASELINE_COLUMNS: frozenset[str] = frozenset(
    {
        "agent_name",
        "agent_step_count",
        "agent_version",
        "assisted_step_count",
        "assisted_step_ratio_screening",
        "autonomous_step_count",
        "autonomous_step_ratio_screening",
        "cache_hit_rate_screening",
        "cached_tokens",
        "citation_reference_count",
        "completion_tokens",
        "context_burn_velocity_screening",
        "cost_usd",
        "created_at",
        "duration_seconds",
        "edit_tool_call_count",
        "exception_class",
        "intervention_category",
        "intervention_count",
        "job_id",
        "job_name",
        "linear_innocence_screening",
        "loop_reasons_json",
        "loop_suspicion_detected",
        "loop_suspicion_score",
        "model_name",
        "path_reference_count",
        "primary_reward",
        "prompt_tokens",
        "recovery_rate_screening",
        "repeated_command_count",
        "source_path",
        "source_sha256",
        "status",
        "step_count",
        "subagent_overhead_ratio_screening",
        "system_step_count",
        "task_name",
        "tool_call_count",
        "tool_error_rate_screening",
        "trial_id",
        "trial_name",
        "unique_tools_count",
        "user_step_count",
        "valid_citation_reference_count",
        "valid_path_reference_count",
    }
)


def test_t11_gated_columns_are_registered() -> None:
    missing = sorted(
        T11_GATED_BASELINE_COLUMNS - set(TRAJECTORY_FEATURE_REGISTRY.all_features())
    )
    assert missing == []


@pytest.mark.parametrize("name", sorted(T11_GATED_BASELINE_COLUMNS))
def test_gated_column_carries_lineage_metadata_or_unknown_marker(name: str) -> None:
    feature = TRAJECTORY_FEATURE_REGISTRY.get(name)
    assert feature is not None
    if feature.lineage_unknown:
        # Explicit unknown marker: the true source is unknown and must not be invented.
        assert feature.lineage_source is None
        return
    assert feature.lineage_source is not None and feature.lineage_source.strip()
    assert audit_lineage_declaration(feature) is None
    assert audit_denominator_policy(feature) is None


@pytest.mark.parametrize("name", sorted(T11_GATED_BASELINE_COLUMNS))
def test_gated_column_contract_is_free_of_paperwork_violations(name: str) -> None:
    """Mirror of the gate's structural paperwork check (MISSING_* verdicts only).

    The gate's substantive LINEAGE_VIOLATION for post-verdict columns is expected
    and honest for the outcome-defining primary_reward; paperwork verdicts must
    never come back once lineage and denominator applicability are declared.
    """
    feature = TRAJECTORY_FEATURE_REGISTRY.get(name)
    assert feature is not None
    contract = feature_contract_row(feature)
    assert contract.declared_inputs is not None
    assert contract.available_before_verdict is not None
    assert contract.denominator_policy is not None
    if contract.denominator_policy == "required":
        assert contract.denominator_sibling
        assert contract.null_on_zero_denominator is True
    else:
        assert contract.denominator_sibling is None
        assert contract.null_on_zero_denominator is False
    if name != "primary_reward":
        assert contract.available_before_verdict is True


def test_edit_tool_call_count_declares_unknown_not_invented_source() -> None:
    """No producer emits edit_tool_call_count (the producer emits edit_call_count)."""
    feature = TRAJECTORY_FEATURE_REGISTRY.get("edit_tool_call_count")
    assert feature is not None
    assert feature.lineage_unknown is True
    assert feature.lineage_source is None


def test_required_denominator_ratio_features_define_their_denominator() -> None:
    ratios = {
        name
        for name in T11_GATED_BASELINE_COLUMNS
        if TRAJECTORY_FEATURE_REGISTRY.get(name).denominator_policy == "required"
    }
    assert ratios == {
        "assisted_step_ratio_screening",
        "autonomous_step_ratio_screening",
        "cache_hit_rate_screening",
        "linear_innocence_screening",
        "recovery_rate_screening",
        "subagent_overhead_ratio_screening",
        "tool_error_rate_screening",
    }
    for name in sorted(ratios):
        feature = TRAJECTORY_FEATURE_REGISTRY.get(name)
        assert feature is not None
        assert feature.denominator_definition is not None
        assert feature.denominator_definition.strip()
