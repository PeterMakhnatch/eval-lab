"""Focused tests for Engineer-Data per-trial compliance records."""

from __future__ import annotations

import warnings

from evallab.interpretation.trajectory_compliance import (
    MALFORMED_REGISTRY,
    PlatformSettlement,
    TrialComplianceRecord,
    TrialEvidenceBundle,
    current_corpus_method_readiness,
    evaluate_trial_compliance,
    ingest_settled_trial_idempotent,
    lineage_depends_on_outcome,
    missing_denominator_declaration,
    provenance_catalog,
    t11_lineage_blocking,
    tracked_output_is_manifest_only,
    v_analysis_ready_trials,
)


def _settled(**overrides: object) -> PlatformSettlement:
    payload = {
        "job_id": "job-1",
        "trial_id": "trial-1",
        "cas_uri": "cas://sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "cataloged": True,
        "cas_settled": True,
        "catalog_digest": "sha256:catalog",
        "source_watermark": "2026-08-28T00:00:00+00:00",
        "projection_watermark": "2026-08-28T00:00:01+00:00",
    }
    payload.update(overrides)
    return PlatformSettlement.model_validate(payload)


def _bundle(**overrides: object) -> TrialEvidenceBundle:
    payload: dict[str, object] = {
        "settlement": _settled(),
        "task_name": "demo-task",
        "model_name": "gpt-test",
        "agent_name": "oracle",
        "task_success": True,
        "step_count": 8,
        "first_error_step": 2,
        "error_observed": True,
        "error_evidence_ref": "cas://sha256:error",
        "lock_event_observed": True,
        "lock_step": 5,
        "lock_predicate_id": "lock-v1",
        "lock_predicate_version": "1",
        "lock_evidence_ref": "cas://sha256:lock",
        "right_censored": False,
        "result_present": True,
        "atif_present": True,
        "native_events_present": True,
        "benchmark_events_present": True,
        "state_journal_present": True,
        "loss_manifest_present": True,
        "schema_valid": True,
        "digest_valid": True,
        "lineage_valid": True,
        "citation_valid": True,
        "producer_live": True,
        "alphabet_ready": True,
        "dose_ready": True,
        "cohort_cell_trial_count": 2,
        "recovery_opportunity": True,
        "recovery_outcome": True,
        "fault_opportunity_id": "fault-1",
        "trial_source_digest": "sha256:src",
        "registered_feature_names": ["tool_call_count"],
        "feature_row": {"tool_call_count": 3},
    }
    payload.update(overrides)
    return TrialEvidenceBundle.model_validate(payload)


def test_missing_catalog_settlement_quarantines() -> None:
    record = evaluate_trial_compliance(_bundle(settlement=_settled(cataloged=False, cas_settled=False)))
    assert record.disposition == "QUARANTINED"
    assert "catalog_or_cas_not_settled" in record.hold_reasons
    assert record.analysis_ready is False


def test_corrupt_evidence_quarantines() -> None:
    record = evaluate_trial_compliance(_bundle(corrupt_evidence=True))
    assert record.disposition == "QUARANTINED"
    assert "quarantine_corrupt_or_infra" in record.hold_reasons


def test_missing_dimensions_hold() -> None:
    record = evaluate_trial_compliance(_bundle(model_name=None, agent_name=None))
    assert record.disposition == "HOLD"
    assert "MISSING_DIMENSION" in record.hold_reasons


def test_zero_opportunity_and_missing_recovery_hold() -> None:
    record = evaluate_trial_compliance(_bundle(recovery_opportunity=False, recovery_outcome=None))
    assert "ZERO_OPPORTUNITY" in record.hold_reasons
    assert "MISSING_RECOVERY_OUTCOME" in record.hold_reasons
    assert record.disposition == "HOLD"


def test_short_trajectory_and_missing_t_lock_hold() -> None:
    record = evaluate_trial_compliance(
        _bundle(
            step_count=3,
            first_error_step=None,
            lock_predicate_id=None,
            lock_predicate_version=None,
            lock_event_observed=None,
            right_censored=None,
        )
    )
    assert "SHORT_TRAJECTORY" in record.hold_reasons
    assert "T_LOCK_UNAVAILABLE" in record.hold_reasons
    assert "CENSORING_UNAVAILABLE" in record.hold_reasons


def test_repeat_ineligible_when_single_trial_cell() -> None:
    record = evaluate_trial_compliance(_bundle(cohort_cell_trial_count=1))
    assert "REPEAT_INELIGIBLE" in record.hold_reasons


def test_stale_producer_and_unregistered_feature() -> None:
    record = evaluate_trial_compliance(
        _bundle(
            producer_live=False,
            feature_row={"secret_score": 1},
            registered_feature_names=["tool_call_count"],
        )
    )
    assert "producer_stale_or_unevaluated" in record.hold_reasons
    assert "UNREGISTERED_FEATURE" in record.hold_reasons


def test_dimension_cross_contamination_hold() -> None:
    record = evaluate_trial_compliance(_bundle(dimension_cross_contaminated=True))
    assert "MISSING_DIMENSION" in record.hold_reasons


def test_idempotent_ingest_zero_churn() -> None:
    bundle = _bundle()
    first = ingest_settled_trial_idempotent(bundle)
    second = ingest_settled_trial_idempotent(bundle, prior=first)
    assert second is first


def test_tracked_output_rejects_runs_and_parquet() -> None:
    assert tracked_output_is_manifest_only(["research/experiments/manifests/compliance.json"]) is True
    assert tracked_output_is_manifest_only(["derived/parquet/traj_features.parquet"]) is False
    assert tracked_output_is_manifest_only(["runs/job/result.json"]) is False


def test_lineage_blocking_fail_closed_without_declared_inputs() -> None:
    blocked = t11_lineage_blocking(
        [
            {"column_name": "legacy_feature"},
            {
                "column_name": "ok_count",
                "declared_inputs": ["tool_calls"],
                "measurement_role": "process",
                "available_before_verdict": True,
            },
            {
                "column_name": "missing_before_verdict",
                "declared_inputs": ["tool_calls"],
                "measurement_role": "process",
            },
            {
                "column_name": "verdict_derived",
                "declared_inputs": ["task_success"],
                "measurement_role": "process",
                "available_before_verdict": True,
            },
        ]
    )
    assert "legacy_feature" in blocked
    assert "missing_before_verdict" in blocked
    assert "verdict_derived" in blocked
    assert "ok_count" not in blocked
    assert lineage_depends_on_outcome(["task_success"]) is True



def test_provenance_catalog_denominator_is_per_column() -> None:
    catalog = provenance_catalog(
        [
            {
                "column_name": "prr",
                "declared_inputs": ["faults"],
                "measurement_role": "process",
                "available_before_verdict": True,
                "denominator_policy": "required",
                "denominator_sibling": "fault_exposure_count",
                "null_on_zero_denominator": True,
                "producer_module": "recovery_metrics",
            },
            {
                "column_name": "fault_exposure_count",
                "declared_inputs": ["faults"],
                "measurement_role": "denominator",
                "available_before_verdict": True,
                "denominator_policy": "not_applicable",
            },
            {
                "column_name": "rc",
                "declared_inputs": ["trials"],
                "measurement_role": "process",
                "available_before_verdict": True,
                "denominator_policy": "required",
                "denominator_sibling": "trial_count",
                "null_on_zero_denominator": True,
                "producer_module": "recovery_metrics",
            },
            {
                "column_name": "trial_count",
                "declared_inputs": ["trials"],
                "measurement_role": "denominator",
                "available_before_verdict": True,
                "denominator_policy": "not_applicable",
            },
        ]
    )
    by_name = {row.column_name: row for row in catalog}
    assert by_name["prr"].denominator_sibling == "fault_exposure_count"
    assert by_name["rc"].denominator_sibling == "trial_count"
    assert by_name["prr"].basis == "REGISTRY_CONFIRMED"


def test_denominator_policy_tri_state_refusals() -> None:
    none_policy = provenance_catalog(
        [
            {
                "column_name": "legacy",
                "declared_inputs": ["x"],
                "available_before_verdict": True,
                "measurement_role": "process",
            }
        ]
    )
    assert none_policy[0].refusal == "MISSING_DENOMINATOR_APPLICABILITY_DECLARATION"
    assert none_policy[0].basis == "REGISTRY_CONFIRMED"

    missing_sib = provenance_catalog(
        [
            {
                "column_name": "rate_a",
                "declared_inputs": ["x"],
                "available_before_verdict": True,
                "measurement_role": "process",
                "denominator_policy": "required",
                "null_on_zero_denominator": True,
            }
        ]
    )
    assert missing_sib[0].refusal == "MISSING_DENOMINATOR_DECLARATION"
    assert missing_denominator_declaration(
        [
            {
                "column_name": "rate_a",
                "denominator_policy": "required",
                "null_on_zero_denominator": True,
            }
        ]
    ) == ["rate_a"]

    missing_null = provenance_catalog(
        [
            {
                "column_name": "rate_b",
                "declared_inputs": ["x"],
                "available_before_verdict": True,
                "measurement_role": "process",
                "denominator_policy": "required",
                "denominator_sibling": "den_b",
            },
            {
                "column_name": "den_b",
                "declared_inputs": ["x"],
                "available_before_verdict": True,
                "measurement_role": "denominator",
                "denominator_policy": "not_applicable",
            },
        ]
    )
    assert {row.column_name: row.refusal for row in missing_null}["rate_b"] == (
        "MISSING_NULL_ON_ZERO_DECLARATION"
    )

    invalid = provenance_catalog(
        [
            {
                "column_name": "cost_usd",
                "declared_inputs": ["x"],
                "available_before_verdict": True,
                "measurement_role": "process",
                "denominator_policy": "not_applicable",
                "denominator_sibling": "n",
            }
        ]
    )
    assert invalid[0].refusal == "INVALID_DENOMINATOR_DECLARATION"


def test_current_corpus_does_not_synthesize_t12_t13() -> None:
    freeze = current_corpus_method_readiness()
    assert freeze["featured_trials"] == 44
    assert freeze["T1.2"]["refusals"] == ["MISSING_RECOVERY_OUTCOME", "ZERO_OPPORTUNITY"]
    assert freeze["T1.3"]["refusals"] == ["T_LOCK_UNAVAILABLE", "CENSORING_UNAVAILABLE"]
    assert v_analysis_ready_trials([]) == []


def test_identities_are_upstream_not_minted() -> None:
    record = evaluate_trial_compliance(_bundle())
    assert record.job_id == "job-1"
    assert record.trial_id == "trial-1"
    assert record.cas_uri.startswith("cas://sha256:")
    assert record.task_name == "demo-task"


def test_constructor_and_model_validate_seal_digests() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        via_init = TrialComplianceRecord(
            disposition="HOLD",
            hold_reasons=["MISSING_DIMENSION", "MISSING_DIMENSION"],
            analysis_ready=True,
            job_id="job-1",
            trial_id="trial-1",
            cas_uri="cas://sha256:aa",
            trial_source_digest="sha256:src",
            evaluated_at="2026-08-28T00:00:00+00:00",
        )
    via_validate = TrialComplianceRecord.model_validate(via_init.model_dump(mode="json"))
    assert via_init.hold_reasons == ["MISSING_DIMENSION"]
    assert via_init.analysis_ready is False
    assert via_init.source_digest == "sha256:src"
    assert via_init.record_digest == via_init.row_digest
    assert via_init.record_digest != ""
    assert via_validate.record_digest == via_init.record_digest
    assert via_validate.model_dump(mode="json") == via_init.model_dump(mode="json")


def test_record_roundtrip_tamper_changes_digest() -> None:
    record = evaluate_trial_compliance(_bundle())
    dumped = record.model_dump(mode="json")
    restored = TrialComplianceRecord.model_validate(dumped)
    assert restored.record_digest == record.record_digest
    dumped["hold_reasons"] = ["TAMPERED"]
    tampered = TrialComplianceRecord.model_validate(dumped)
    assert tampered.record_digest != record.record_digest
    assert "TAMPERED" in tampered.hold_reasons


def test_cascade_requires_first_error_step_and_lock() -> None:
    missing_error = evaluate_trial_compliance(_bundle(first_error_step=None))
    assert missing_error.cascade_eligible is False
    assert "T_LOCK_UNAVAILABLE" in missing_error.hold_reasons
    missing_lock = evaluate_trial_compliance(
        _bundle(lock_predicate_id=None, lock_predicate_version=None)
    )
    assert missing_lock.cascade_eligible is False


def test_malformed_registry_mapping_does_not_raise() -> None:
    blocked = t11_lineage_blocking([{}, {"column_name": 1}])
    assert blocked == [MALFORMED_REGISTRY, MALFORMED_REGISTRY]
    missing = missing_denominator_declaration([{"denominator_policy": "required"}])
    assert missing == [MALFORMED_REGISTRY]
    catalog = provenance_catalog([{"declared_inputs": ["x"]}])
    assert catalog[0].column_name == MALFORMED_REGISTRY
    assert catalog[0].refusal == MALFORMED_REGISTRY
