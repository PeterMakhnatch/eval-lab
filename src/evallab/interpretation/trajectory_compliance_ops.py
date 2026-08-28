"""Data-private ingest hook, readiness gates, catalog, and bloat/quarantine ops.

Does not edit cli.py, storage/data_backfill.py, feature_registry.py, or SQL views.
Consumes PlatformSettlement after catalog+CAS; never writes Z2.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import Field

from evallab.interpretation.trajectory_compliance import (
    FeatureProvenanceEntry,
    PlatformSettlement,
    TrialComplianceRecord,
    TrialEvidenceBundle,
    canonical_digest,
    evaluate_trial_compliance,
    ingest_settled_trial,
    provenance_catalog,
    tracked_output_is_manifest_only,
)
from evallab.schemas import ContractModel

GOLD_RATER_MIN = 3
BackpressureReason = Literal[
    "ingest_lag",
    "quality_audit_lag",
    "catalog_or_cas_not_settled",
    "quarantine_fraction_exceeded",
]


class BackpressureHold(ContractModel):
    held: Literal[True] = True
    reason: BackpressureReason
    lag_seconds: int | None = None
    max_lag_seconds: int | None = None
    settlement: PlatformSettlement


class ReadinessGates(ContractModel):
    job_id: str
    trial_id: str
    cas_uri: str
    model_name: str | None
    agent_name: str | None
    task_name: str | None
    repeat_eligible: bool
    sequence_eligible: bool
    dose_ready: bool
    alphabet_ready: bool
    t_lock_contract_present: bool
    censoring_available: bool
    gold_set_three_rater_ready: bool
    join_ready: bool
    refusals: list[str] = Field(default_factory=list)


class ComplianceIngestReport(ContractModel):
    record: TrialComplianceRecord
    gates: ReadinessGates
    lag_ms: int | None
    backpressure: BackpressureHold | None = None
    catalog_entries: list[FeatureProvenanceEntry] = Field(default_factory=list)
    bloat_clean: bool
    report_digest: str = ""


def _gold_ready(bundle: TrialEvidenceBundle) -> bool:
    count = bundle.feature_row.get("gold_rater_count") if bundle.feature_row else None
    return isinstance(count, int) and count >= GOLD_RATER_MIN


def readiness_gates(bundle: TrialEvidenceBundle, record: TrialComplianceRecord) -> ReadinessGates:
    refusals: list[str] = []
    if not record.repeated_measure_eligible:
        refusals.append("REPEAT_INELIGIBLE")
    if not record.sequence_eligible:
        refusals.append("SHORT_TRAJECTORY")
    if record.dose_ready is not True:
        refusals.append("DOSE_NOT_READY")
    if record.alphabet_ready is not True:
        refusals.append("ALPHABET_NOT_READY")
    if not record.t_lock_contract_present:
        refusals.append("T_LOCK_UNAVAILABLE")
    censor_ok = record.right_censored is True or record.lock_event_observed is True
    if not censor_ok:
        refusals.append("CENSORING_UNAVAILABLE")
    gold_ok = _gold_ready(bundle)
    if not gold_ok:
        refusals.append("gold_set_three_rater_not_ready")
    join_ok = bool(record.model_name and record.agent_name and record.task_name)
    if not join_ok:
        refusals.append("MISSING_DIMENSION")
    return ReadinessGates(
        job_id=record.job_id,
        trial_id=record.trial_id,
        cas_uri=record.cas_uri,
        model_name=record.model_name,
        agent_name=record.agent_name,
        task_name=record.task_name,
        repeat_eligible=record.repeated_measure_eligible,
        sequence_eligible=record.sequence_eligible,
        dose_ready=record.dose_ready is True,
        alphabet_ready=record.alphabet_ready is True,
        t_lock_contract_present=record.t_lock_contract_present,
        censoring_available=censor_ok,
        gold_set_three_rater_ready=gold_ok,
        join_ready=join_ok,
        refusals=sorted(set(refusals)),
    )


def ingest_after_settlement(
    bundle: TrialEvidenceBundle,
    *,
    max_lag_seconds: int | None = None,
    tracked_paths: Sequence[str] = (),
    registry_rows: Sequence[Mapping[str, Any]] = (),
) -> ComplianceIngestReport | BackpressureHold:
    """Event-driven hook. Platform must have cataloged+CAS-settled first."""
    settlement = bundle.settlement
    if not settlement.cataloged or not settlement.cas_settled:
        return BackpressureHold(reason="catalog_or_cas_not_settled", settlement=settlement)
    record = ingest_settled_trial(bundle)
    lag_seconds = None if record.lag_ms is None else record.lag_ms / 1000.0
    if max_lag_seconds is not None and (lag_seconds is None or lag_seconds > max_lag_seconds):
        return BackpressureHold(
            reason="ingest_lag",
            lag_seconds=None if lag_seconds is None else int(lag_seconds),
            max_lag_seconds=max_lag_seconds,
            settlement=settlement,
        )
    gates = readiness_gates(bundle, record)
    catalog = list(provenance_catalog(registry_rows)) if registry_rows else []
    bloat_clean = tracked_output_is_manifest_only(tracked_paths) if tracked_paths else True
    report = ComplianceIngestReport(
        record=record,
        gates=gates,
        lag_ms=record.lag_ms,
        catalog_entries=catalog,
        bloat_clean=bloat_clean,
    )
    digest = canonical_digest(report.model_dump(mode="json", exclude={"report_digest"}))
    object.__setattr__(report, "report_digest", digest)
    return report


def agent_readable_catalog(entries: Sequence[FeatureProvenanceEntry]) -> list[dict[str, Any]]:
    return [
        {
            "column_name": entry.column_name,
            "definition": entry.definition,
            "consumer": entry.named_consumer,
            "formula": entry.formula_or_rule,
            "denominator": entry.denominator_sibling,
            "denominator_policy": entry.denominator_policy,
            "grade": entry.basis,
            "producer": entry.producer_module,
            "coverage": entry.coverage,
            "example": entry.example,
            "refusal": entry.refusal,
            "measurement_role": entry.measurement_role,
        }
        for entry in entries
    ]


def report_sanitized_trial(bundle: TrialEvidenceBundle) -> dict[str, Any]:
    """End-to-end compliance report over one sanitized trial bundle."""
    record = evaluate_trial_compliance(bundle)
    gates = readiness_gates(bundle, record)
    return {
        "job_id": record.job_id,
        "trial_id": record.trial_id,
        "cas_uri": record.cas_uri,
        "model_name": record.model_name,
        "agent_name": record.agent_name,
        "task_name": record.task_name,
        "disposition": record.disposition,
        "hold_reasons": record.hold_reasons,
        "gates": gates.model_dump(mode="json"),
        "lag_ms": record.lag_ms,
        "record_digest": record.record_digest,
    }
