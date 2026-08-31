from __future__ import annotations

import hashlib
import json

import pyarrow.parquet as pq
import pytest

from evallab.semantic_facts import (
    CapabilityOpportunity,
    ContextOperationFact,
    EvidenceCoverage,
    NormalizedFactBundle,
    ProcessStepFact,
    RetrievalFact,
    context_operation_content_digest,
    load_fact_bundle,
    normalize_bundle,
    project_fact_bundle,
    query_scorecard,
)


def digest(value: str = "source") -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def opportunity(**overrides: object) -> CapabilityOpportunity:
    values: dict[str, object] = {
        "opportunity_id": "opp-1",
        "trial_id": "trial-1",
        "benchmark": "bench",
        "construct": "retrieval",
        "eligible": True,
        "required_evidence": ("source_id",),
        "missing_evidence": (),
        "source_ref": "runs/trial-1/trajectory.json",
        "source_digest": digest(),
        "provenance_kind": "mechanical",
    }
    values.update(overrides)
    return CapabilityOpportunity.model_validate(values)


def test_rows_require_valid_source_digest_and_retrieval_source_id() -> None:
    with pytest.raises(ValueError, match="source_digest"):
        opportunity(source_digest="not-a-digest")
    with pytest.raises(ValueError, match="exposed"):
        RetrievalFact(
            trial_id="trial-1",
            utilized_status=True,
            cited_evidence_ref="step:1",
            source_ref="runs/trial-1/trajectory.json",
            source_digest=digest(),
            provenance_kind="mechanical",
        )


def test_projection_preserves_unexposed_coverage_and_aggregates_trials(tmp_path) -> None:
    unexposed = EvidenceCoverage(
        trial_id="trial-3",
        benchmark="bench",
        construct="planning",
        exposed=False,
        eligible=None,
        required_evidence=("plan",),
        observed_evidence=(),
        missing_evidence=("plan",),
        analysis_ready=None,
        source_ref="runs/trial-3/coverage.json",
        source_digest=digest("trial-3"),
        provenance_kind="benchmark_verifier",
    )
    bundle = NormalizedFactBundle(
        capability_opportunities=(
            opportunity(),
            opportunity(
                opportunity_id="opp-2",
                trial_id="trial-2",
                eligible=True,
                missing_evidence=("source_id",),
            ),
        ),
        evidence_coverage=(unexposed,),
    )
    normalized = normalize_bundle(bundle)
    assert normalize_bundle(normalized) == normalized
    paths = project_fact_bundle(normalized, tmp_path)
    assert set(paths) == {
        "capability_opportunities",
        "process_step_facts",
        "retrieval_facts",
        "constraint_facts",
        "context_operation_facts",
        "paired_condition_facts",
        "session_dependency_facts",
        "evidence_coverage",
    }
    retrieval = query_scorecard(tmp_path, benchmark="bench", construct="retrieval")[0]
    assert retrieval == {
        "benchmark": "bench",
        "construct": "retrieval",
        "opportunity_count": 2,
        "eligible_analysis_ready_opportunities": 1,
        "coverage_trials": 2,
        "eligible_trials": 2,
        "analysis_ready_trials": 1,
        "not_analysis_ready_trials": 0,
        "unknown_analysis_readiness_trials": 1,
        "exposed_trials": 2,
        "analysis_ready": None,
    }
    planning = query_scorecard(tmp_path, benchmark="bench", construct="planning")[0]
    assert planning["opportunity_count"] == 0
    assert planning["exposed_trials"] == 0
    assert planning["unknown_analysis_readiness_trials"] == 1
    assert planning["analysis_ready"] is None


def test_coverage_rejects_inconsistent_readiness() -> None:
    with pytest.raises(ValueError, match="analysis_ready"):
        EvidenceCoverage(
            trial_id="trial-1",
            benchmark="bench",
            construct="x",
            exposed=True,
            eligible=True,
            required_evidence=("a",),
            observed_evidence=(),
            missing_evidence=("a",),
            analysis_ready=True,
            source_ref="derived:coverage",
            source_digest=digest(),
            provenance_kind="derived",
        )


def test_normalization_rejects_conflicting_computed_coverage() -> None:
    conflicting = EvidenceCoverage(
        trial_id="trial-1",
        benchmark="bench",
        construct="retrieval",
        exposed=True,
        eligible=True,
        required_evidence=("source_id",),
        observed_evidence=("source_id",),
        missing_evidence=(),
        analysis_ready=True,
        source_ref="manual:coverage",
        source_digest=digest("manual"),
        provenance_kind="benchmark_verifier",
    )
    with pytest.raises(ValueError, match="conflicts with computed"):
        normalize_bundle(
            NormalizedFactBundle(
                capability_opportunities=(opportunity(),),
                evidence_coverage=(conflicting,),
            )
        )


def test_json_bundle_loader_does_not_invent_absent_facts(tmp_path) -> None:
    path = tmp_path / "bundle.json"
    path.write_text(
        json.dumps({"capability_opportunities": [opportunity().model_dump(mode="json")]})
    )
    loaded = load_fact_bundle(path)
    assert len(loaded.capability_opportunities) == 1
    assert loaded.retrieval_facts == ()
    project_fact_bundle(loaded, tmp_path / "projected")


def test_context_operation_step_index_round_trips_projection(tmp_path) -> None:
    fact = ContextOperationFact(
        source_ref="runs/trial-1/condensation.json#step=17",
        source_digest=digest("condensation-source"),
        provenance_kind="mechanical",
        trial_id="trial-1",
        operation_id="condensation-17",
        operation="compaction",
        step_index=17,
        content_digest=digest("condensation-content"),
    )
    serialized = fact.model_dump_json()
    assert ContextOperationFact.model_validate_json(serialized).step_index == 17

    paths = project_fact_bundle(
        NormalizedFactBundle(context_operation_facts=(fact,)),
        tmp_path / "projected",
    )

    assert pq.read_table(paths["context_operation_facts"]).to_pylist()[0]["step_index"] == 17


def test_historical_context_operation_order_remains_absent_without_step_join() -> None:
    fact = ContextOperationFact(
        source_ref="runs/trial-1/legacy.json",
        source_digest=digest("legacy"),
        provenance_kind="mechanical",
        trial_id="trial-1",
        operation_id="legacy-compaction",
        operation="compaction",
    )
    process_step = ProcessStepFact(
        source_ref="runs/trial-1/trajectory.json#step=9",
        source_digest=digest("trajectory"),
        provenance_kind="mechanical",
        trial_id="trial-1",
        source_trajectory_id="trajectory-1",
        source_step_id="9",
        label="neutral",
    )

    normalized = normalize_bundle(
        NormalizedFactBundle(
            process_step_facts=(process_step,),
            context_operation_facts=(fact,),
        )
    )

    assert normalized.context_operation_facts[0].step_index is None


@pytest.mark.parametrize("value", [-1, "1", 1.5, True])
def test_context_operation_rejects_invalid_step_index(value: object) -> None:
    with pytest.raises(ValueError, match="step_index"):
        ContextOperationFact(
            source_ref="runs/trial-1/context.json",
            source_digest=digest(),
            provenance_kind="mechanical",
            trial_id="trial-1",
            operation_id="invalid-step",
            operation="compaction",
            step_index=value,
        )


def test_context_payload_digest_binds_summary_order_and_compression_metadata() -> None:
    payload = {
        "summary": "The user selected the red key.",
        "forgotten_message_indices": [2, 5, 8],
        "compression_metadata": {"method": "summary", "input_tokens": 1200},
    }
    baseline = context_operation_content_digest(payload)

    summary_changed = {**payload, "summary": "The user selected the blue key."}
    order_changed = {**payload, "forgotten_message_indices": [5, 2, 8]}
    metadata_changed = {
        **payload,
        "compression_metadata": {"method": "summary", "input_tokens": 1201},
    }

    assert baseline != context_operation_content_digest(summary_changed)
    assert baseline != context_operation_content_digest(order_changed)
    assert baseline != context_operation_content_digest(metadata_changed)
