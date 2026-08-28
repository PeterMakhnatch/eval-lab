"""Tests for Data-private ingest hook, readiness gates, catalog, and bloat gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.interpretation.trajectory_compliance import (
    TrialEvidenceBundle,
    provenance_catalog,
)
from evallab.interpretation.trajectory_compliance_ops import (
    ArtifactRefs,
    ComplianceEngineError,
    ComplianceIngestReport,
    ComplianceSettlementError,
    SettlementIdentity,
    agent_readable_catalog,
    canonical_report_bytes,
    idempotency_key,
    ingest_after_settlement,
    report_sanitized_trial,
)

FIXTURE = Path("tests/fixtures/compliance/sanitized_gaia2_trial.json")
FINISHED_AT = "2026-08-28T00:00:00+00:00"
CAS_URI = "cas://sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _identity(**overrides: object) -> SettlementIdentity:
    payload: dict[str, object] = {
        "job_id": "job-1",
        "trial_id": "trial-1",
        "cas_uri": CAS_URI,
        "cataloged": True,
        "cas_settled": True,
        "catalog_digest": "sha256:catalog",
        "source_watermark": "2026-08-28T00:00:00+00:00",
        "projection_watermark": "2026-08-28T00:00:01+00:00",
        "ingested_at": "2026-08-28T00:00:02+00:00",
    }
    payload.update(overrides)
    return SettlementIdentity.model_validate(payload)


def _evaluation(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "task_name": "demo-task",
        "model_name": "gpt-test",
        "agent_name": "oracle",
        "task_success": True,
        "step_count": 8,
        "first_error_step": 2,
        "lock_predicate_id": "lock-v1",
        "lock_predicate_version": "1",
        "lock_event_observed": True,
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
        "feature_row": {"gold_rater_count": 3, "tool_call_count": 4},
        "registered_feature_names": ["tool_call_count"],
        "trial_source_digest": "sha256:src",
    }
    payload.update(overrides)
    return payload


def _refs(**overrides: object) -> ArtifactRefs:
    payload: dict[str, object] = {
        "result_digest": "sha256:result",
        "atif_digest": "sha256:atif",
        "ir_digest": "sha256:ir",
        "pack_digest": "sha256:pack",
        "loss_manifest_digest": "sha256:loss",
        "evaluation": _evaluation(),
    }
    if "evaluation" in overrides and isinstance(overrides["evaluation"], dict):
        merged = _evaluation()
        merged.update(overrides["evaluation"])
        overrides = {**overrides, "evaluation": merged}
    payload.update(overrides)
    return ArtifactRefs.model_validate(payload)


def test_hook_raises_before_catalog_settlement() -> None:
    with pytest.raises(ComplianceSettlementError, match="catalog_or_cas_not_settled"):
        ingest_after_settlement(
            _identity(cataloged=False, cas_settled=False),
            _refs(),
            FINISHED_AT,
        )


def test_hook_lag_ms_from_settlement_timestamps() -> None:
    result = ingest_after_settlement(_identity(), _refs(), FINISHED_AT)
    assert result.lag_ms == 2000
    assert result.disposition in {"QUALITY_PASS", "QUALITY_WARN", "HOLD", "QUARANTINED"}


def test_hook_emits_join_ready_identities() -> None:
    result = ingest_after_settlement(_identity(), _refs(), FINISHED_AT)
    assert isinstance(result, ComplianceIngestReport)
    assert result.gates.join_ready is True
    assert result.gates.model_name == "gpt-test"
    assert result.gates.agent_name == "oracle"
    assert result.record.task_name == "demo-task"
    assert result.disposition == result.record.disposition
    assert result.reasons == result.record.hold_reasons


def test_readiness_gates_refuse_short_repeat_dose_alphabet_lock_gold() -> None:
    result = ingest_after_settlement(
        _identity(),
        _refs(
            evaluation={
                "step_count": 3,
                "cohort_cell_trial_count": 1,
                "dose_ready": False,
                "alphabet_ready": False,
                "lock_predicate_id": None,
                "lock_predicate_version": None,
                "first_error_step": None,
                "lock_event_observed": None,
                "right_censored": None,
                "feature_row": {"tool_call_count": 1},
            }
        ),
        FINISHED_AT,
    )
    assert isinstance(result, ComplianceIngestReport)
    assert "SHORT_TRAJECTORY" in result.gates.refusals
    assert "REPEAT_INELIGIBLE" in result.gates.refusals
    assert "DOSE_NOT_READY" in result.gates.refusals
    assert "ALPHABET_NOT_READY" in result.gates.refusals
    assert "T_LOCK_UNAVAILABLE" in result.gates.refusals
    assert "CENSORING_UNAVAILABLE" in result.gates.refusals
    assert "gold_set_three_rater_not_ready" in result.gates.refusals
    assert result.disposition == "HOLD"


def test_bloat_gate_rejects_derived_parquet() -> None:
    result = ingest_after_settlement(
        _identity(),
        _refs(tracked_paths=("derived/parquet/traj_features.parquet",)),
        FINISHED_AT,
    )
    assert result.bloat_clean is False


def test_agent_readable_catalog_fields() -> None:
    entries = provenance_catalog(
        [
            {
                "column_name": "tool_call_count",
                "definition": "number of tool calls",
                "measurement_role": "process",
                "denominator_policy": "not_applicable",
                "description": "count of tool calls",
                "formula_or_rule": "len(tool_calls)",
                "producer_module": "evallab.traj",
                "coverage": "107/107",
                "named_consumer": "T1.1",
                "declared_inputs": ["tool_calls"],
                "available_before_verdict": True,
            }
        ]
    )
    rows = agent_readable_catalog(entries)
    assert rows[0]["column_name"] == "tool_call_count"
    assert rows[0]["consumer"] == "T1.1"
    assert rows[0]["formula"] == "len(tool_calls)"
    assert rows[0]["denominator_policy"] == "not_applicable"
    assert rows[0]["grade"] == "REGISTRY_CONFIRMED"


def test_e2e_sanitized_real_trial_bundle_holds_empty_recovery_and_t_lock() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    bundle = TrialEvidenceBundle.model_validate(raw)
    report = report_sanitized_trial(bundle)
    assert report["job_id"] == "job-gaia2-adapt-hard-1-amd64"
    assert report["trial_id"] == "gaia2-adapt-hard-1__XJL887e"
    assert report["model_name"] == "gpt-5.6-luna"
    assert report["agent_name"] == "oracle"
    assert report["disposition"] == "HOLD"
    assert "T_LOCK_UNAVAILABLE" in report["hold_reasons"]
    assert "MISSING_RECOVERY_OUTCOME" in report["hold_reasons"]
    assert report["gates"]["join_ready"] is True
    assert "runs/" not in json.dumps(report)

    settlement = raw["settlement"]
    finished_at = raw.pop("finished_at")
    ingested_at = raw.pop("ingested_at")
    identity = SettlementIdentity.model_validate({**settlement, "ingested_at": ingested_at})
    frozen = ingest_after_settlement(
        identity,
        ArtifactRefs.model_validate({"evaluation": raw}),
        finished_at,
    )
    assert frozen.disposition == "HOLD"
    assert "T_LOCK_UNAVAILABLE" in frozen.reasons
    assert "MISSING_RECOVERY_OUTCOME" in frozen.reasons


def test_report_roundtrip() -> None:
    first = ingest_after_settlement(_identity(), _refs(), FINISHED_AT)
    restored = ComplianceIngestReport.model_validate(first.model_dump(mode="json"))
    assert restored.report_digest == first.report_digest
    assert restored.disposition == first.disposition
    assert restored.reasons == first.reasons
    assert restored.lag_ms == first.lag_ms
    assert canonical_report_bytes(restored) == canonical_report_bytes(first)


def test_identical_inputs_are_byte_identical() -> None:
    first = ingest_after_settlement(_identity(), _refs(), FINISHED_AT)
    second = ingest_after_settlement(_identity(), _refs(), FINISHED_AT)
    assert first.report_digest == second.report_digest
    assert canonical_report_bytes(first) == canonical_report_bytes(second)
    assert idempotency_key(_identity(), _refs()) == idempotency_key(_identity(), _refs())


def test_changed_artifact_digest_is_new_evaluation_key() -> None:
    first_refs = _refs()
    second_refs = _refs(result_digest="sha256:other-result")
    first_key = idempotency_key(_identity(), first_refs)
    second_key = idempotency_key(_identity(), second_refs)
    assert first_key != second_key
    first = ingest_after_settlement(_identity(), first_refs, FINISHED_AT)
    second = ingest_after_settlement(_identity(), second_refs, FINISHED_AT)
    assert first.report_digest == second.report_digest


def test_evaluated_at_excluded_from_report_digest() -> None:
    report = ingest_after_settlement(_identity(), _refs(), FINISHED_AT)
    payload = json.loads(canonical_report_bytes(report))
    assert "evaluated_at" not in json.dumps(payload)
    assert "evaluated_at" not in report.report_digest
    mutated = report.model_copy(
        update={"record": report.record.model_copy(update={"evaluated_at": "1999-01-01T00:00:00+00:00"})}
    )
    sealed_mutated = mutated.model_copy(update={"report_digest": ""})
    from evallab.interpretation.trajectory_compliance_ops import _seal_report

    resealed = _seal_report(sealed_mutated)
    assert resealed.report_digest == report.report_digest


def test_compliance_engine_failure_raises_typed_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    import evallab.interpretation.trajectory_compliance_ops as ops

    def boom(_bundle: TrialEvidenceBundle) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(ops, "evaluate_trial_compliance", boom)
    with pytest.raises(ComplianceEngineError, match="RuntimeError"):
        ingest_after_settlement(_identity(), _refs(), FINISHED_AT)
