"""Feature eligibility refuses reward leakage and invalid coupling contracts."""

from dataclasses import replace

import pytest

from evallab.interpretation.run_report import (
    FeatureDefinition,
    FeatureRegistry,
    audit_predictor_eligibility,
    audit_verdict_coupling,
)

FEATURE = FeatureDefinition(
    column_name="test_feature",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    source_table="events",
    formula_or_rule="count",
    null_condition="missing evidence",
    description="Observed event count",
    available_before_verdict=True,
    verdict_coupling="independent",
)


@pytest.mark.parametrize(
    ("changes", "strict", "refusal"),
    [
        ({"verdict_coupling": "defines", "coupling_basis": "reward rule"}, False, "REWARD_DEFINITION_LEAKAGE"),
        ({"verdict_coupling": None}, False, "UNDECLARED_VERDICT_COUPLING"),
        ({"available_before_verdict": False}, False, "POST_VERDICT_TEMPORAL_VIOLATION"),
        ({"available_before_verdict": None}, False, "MISSING_TEMPORAL_AVAILABILITY"),
        ({"verdict_coupling": "correlates"}, False, "MISSING_COUPLING_EVIDENCE_BASIS"),
        ({"verdict_coupling": "correlates", "coupling_basis": "   "}, False, "MISSING_COUPLING_EVIDENCE_BASIS"),
        ({"verdict_coupling": "not_applicable"}, False, "NOT_APPLICABLE_FOR_PREDICTION"),
        ({}, False, None),
        ({"verdict_coupling": "correlates", "coupling_basis": "search depth"}, False, None),
        ({"verdict_coupling": "correlates", "coupling_basis": "search depth"}, True, "VERDICT_CORRELATED"),
        ({"verdict_coupling": "unrecognized_value"}, False, "INVALID_VERDICT_COUPLING"),
    ],
)
def test_predictor_audit_refusal(changes, strict, refusal):
    assert audit_predictor_eligibility(replace(FEATURE, **changes), strict_independence=strict) == refusal


@pytest.mark.parametrize(
    "changes",
    [
        {"verdict_coupling": "invalid"},
        {"verdict_coupling": "correlates", "coupling_basis": ""},
        {"verdict_coupling": "defines", "coupling_basis": None},
    ],
)
def test_registry_rejects_invalid_coupling(changes):
    with pytest.raises(ValueError):
        FeatureRegistry().register(replace(FEATURE, **changes))


def test_feature_definition_accepts_documented_coupling():
    feature = replace(FEATURE, verdict_coupling="correlates", coupling_basis="search depth")
    registry = FeatureRegistry()
    registry.register(feature)
    assert registry.get(feature.column_name) == feature
    assert audit_verdict_coupling(feature) is None
