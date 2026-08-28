"""Tests for Data-private ingest hook, readiness gates, catalog, and bloat gate."""

from __future__ import annotations

import json
from pathlib import Path

from evallab.interpretation.trajectory_compliance import (
    PlatformSettlement,
    TrialEvidenceBundle,
    provenance_catalog,
)
from evallab.interpretation.trajectory_compliance_ops import (
    BackpressureHold,
    ComplianceIngestReport,
    agent_readable_catalog,
    ingest_after_settlement,
    report_sanitized_trial,
)

FIXTURE = Path("tests/fixtures/compliance/sanitized_gaia2_trial.json")


def _settled(**overrides: object) -> PlatformSettlement:
    payload = {
        "job_id": "job-1",
        "trial_id": "trial-1",
        "cas_uri": "cas://sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "cataloged": True,
        "cas_settled": True,
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
        "finished_at": "2026-08-28T00:00:00+00:00",
        "ingested_at": "2026-08-28T00:00:02+00:00",
        "feature_row": {"gold_rater_count": 3, "tool_call_count": 4},
        "registered_feature_names": ["tool_call_count"],
    }
    payload.update(overrides)
    return TrialEvidenceBundle.model_validate(payload)


def test_hook_holds_before_catalog_settlement() -> None:
    result = ingest_after_settlement(_bundle(settlement=_settled(cataloged=False, cas_settled=False)))
    assert isinstance(result, BackpressureHold)
    assert result.reason == "catalog_or_cas_not_settled"


def test_hook_backpressure_on_ingest_lag() -> None:
    result = ingest_after_settlement(_bundle(), max_lag_seconds=0)
    assert isinstance(result, BackpressureHold)
    assert result.reason == "ingest_lag"


def test_hook_emits_join_ready_identities() -> None:
    result = ingest_after_settlement(_bundle())
    assert isinstance(result, ComplianceIngestReport)
    assert result.gates.join_ready is True
    assert result.gates.model_name == "gpt-test"
    assert result.gates.agent_name == "oracle"
    assert result.record.task_name == "demo-task"


def test_readiness_gates_refuse_short_repeat_dose_alphabet_lock_gold() -> None:
    result = ingest_after_settlement(
        _bundle(
            step_count=3,
            cohort_cell_trial_count=1,
            dose_ready=False,
            alphabet_ready=False,
            lock_predicate_id=None,
            lock_predicate_version=None,
            first_error_step=None,
            lock_event_observed=None,
            right_censored=None,
            feature_row={"tool_call_count": 1},
        )
    )
    assert isinstance(result, ComplianceIngestReport)
    assert "SHORT_TRAJECTORY" in result.gates.refusals
    assert "REPEAT_INELIGIBLE" in result.gates.refusals
    assert "DOSE_NOT_READY" in result.gates.refusals
    assert "ALPHABET_NOT_READY" in result.gates.refusals
    assert "T_LOCK_UNAVAILABLE" in result.gates.refusals
    assert "CENSORING_UNAVAILABLE" in result.gates.refusals
    assert "gold_set_three_rater_not_ready" in result.gates.refusals


def test_bloat_gate_rejects_derived_parquet() -> None:
    result = ingest_after_settlement(
        _bundle(),
        tracked_paths=["derived/parquet/traj_features.parquet"],
    )
    assert isinstance(result, ComplianceIngestReport)
    assert result.bloat_clean is False


def test_agent_readable_catalog_fields() -> None:
    entries = provenance_catalog(
        [
            {
                "column_name": "tool_call_count",
                "declared_inputs": ["tool_calls"],
                "available_before_verdict": True,
                "measurement_role": "process",
                "denominator_policy": "not_applicable",
                "description": "count of tool calls",
                "formula_or_rule": "len(tool_calls)",
                "producer_module": "evallab.traj",
                "coverage": "107/107",
                "named_consumer": "T1.1",
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
