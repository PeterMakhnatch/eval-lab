"""Data-private ingest hook, readiness gates, catalog, and bloat/quarantine ops.

Platform integration surface is ingest_after_settlement(identity, refs, finished_at).
Data evaluates purely from those frozen inputs and raises typed exceptions.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from evallab.interpretation.trajectory_compliance import (
    SCHEMA_VERSION,
    ComplianceDisposition,
    FeatureProvenanceEntry,
    PlatformSettlement,
    TrialComplianceRecord,
    TrialEvidenceBundle,
    canonical_digest,
    canonical_json,
    evaluate_trial_compliance,
    provenance_catalog,
    tracked_output_is_manifest_only,
)
from evallab.schemas import ContractModel

GOLD_RATER_MIN = 3
HOOK_VERSION = "ingest-after-settlement/v1"
EVALUATOR_VERSION = "evaluate_trial_compliance/v1"
_CLOCK_KEYS = frozenset({"evaluated_at"})


class ComplianceError(Exception):
    """Typed compliance exception. Platform must not fabricate a Data report."""

    def __init__(self, message: str, *, input_digest: str) -> None:
        super().__init__(message)
        self.input_digest = input_digest


class ComplianceSettlementError(ComplianceError):
    """Catalog/CAS is not settled; Data refuses evaluation."""


class ComplianceEngineError(ComplianceError):
    """Unexpected evaluation failure. Platform must not fabricate a Data report."""



class SettlementIdentity(ContractModel):
    """Immutable job/trial/CAS/catalog settlement identity."""

    job_id: str
    trial_id: str
    cas_uri: str
    cataloged: bool
    cas_settled: bool
    catalog_digest: str | None = None
    source_watermark: str | None = None
    projection_watermark: str | None = None
    ingested_at: str | None = None


class ArtifactRefs(ContractModel):
    """Resolved immutable evaluation payload plus CAS artifact URIs and digests."""

    result_uri: str | None = None
    result_digest: str | None = None
    atif_uri: str | None = None
    atif_digest: str | None = None
    ir_uri: str | None = None
    ir_digest: str | None = None
    pack_uri: str | None = None
    pack_digest: str | None = None
    loss_manifest_uri: str | None = None
    loss_manifest_digest: str | None = None
    extra_digests: dict[str, str] = Field(default_factory=dict)
    tracked_paths: tuple[str, ...] = Field(default_factory=tuple)
    registry_rows: tuple[dict[str, Any], ...] = Field(default_factory=tuple)
    evaluation: dict[str, Any] = Field(default_factory=dict)


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
    report_digest: str = ""
    input_digest: str = ""
    disposition: ComplianceDisposition
    reasons: list[str] = Field(default_factory=list)
    lag_ms: int | None
    record: TrialComplianceRecord
    gates: ReadinessGates
    catalog_entries: list[FeatureProvenanceEntry] = Field(default_factory=list)
    bloat_clean: bool


def normalize_finished_at(finished_at: str) -> str:
    parsed = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _finished_at_for_digest(finished_at: str) -> str:
    try:
        return normalize_finished_at(finished_at)
    except ValueError:
        return finished_at


def compliance_input_digest(
    settlement_identity: SettlementIdentity,
    artifact_refs: ArtifactRefs,
    finished_at: str,
) -> str:
    """Data-owned pre-call digest. Platform must not re-derive this formula."""
    return canonical_digest(
        {
            "schema_version": SCHEMA_VERSION,
            "hook_version": HOOK_VERSION,
            "evaluator_version": EVALUATOR_VERSION,
            "settlement_identity": settlement_identity.model_dump(mode="json"),
            "artifact_refs": artifact_refs.model_dump(mode="json"),
            "finished_at": _finished_at_for_digest(finished_at),
        }
    )




def _strip_clocks(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_clocks(item) for key, item in value.items() if key not in _CLOCK_KEYS}
    if isinstance(value, list):
        return [_strip_clocks(item) for item in value]
    return value


def canonical_report_payload(report: ComplianceIngestReport) -> dict[str, Any]:
    return _strip_clocks(report.model_dump(mode="json", exclude={"report_digest"}))


def canonical_report_bytes(report: ComplianceIngestReport) -> bytes:
    return canonical_json(canonical_report_payload(report)).encode("utf-8")


def _seal_report(report: ComplianceIngestReport) -> ComplianceIngestReport:
    digest = canonical_digest(canonical_report_payload(report))
    object.__setattr__(report, "report_digest", digest)
    return report


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
    censor_ok = record.recovery_censored is not None
    if not censor_ok:
        refusals.append("CENSORING_UNAVAILABLE")
    gold_ok = _gold_ready(bundle)
    if not gold_ok:
        refusals.append("gold_set_three_rater_not_ready")
    join_ok = bool(record.model_name and record.agent_name and record.task_name)
    if not join_ok:
        refusals.append("JOIN_IDENTITY_MISSING")
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


def _evaluation_view(
    identity: SettlementIdentity,
    refs: ArtifactRefs,
    finished_at: str,
) -> TrialEvidenceBundle:
    """Materialize the evaluator view from frozen Platform inputs. No CAS I/O."""
    payload = dict(refs.evaluation)
    payload.pop("settlement", None)
    payload["settlement"] = PlatformSettlement(
        job_id=identity.job_id,
        trial_id=identity.trial_id,
        cas_uri=identity.cas_uri,
        cataloged=identity.cataloged,
        cas_settled=identity.cas_settled,
        catalog_digest=identity.catalog_digest,
        source_watermark=identity.source_watermark,
        projection_watermark=identity.projection_watermark,
    )
    payload["finished_at"] = finished_at
    payload["ingested_at"] = identity.ingested_at
    if refs.ir_digest is not None:
        payload["source_ir_digest"] = refs.ir_digest
    if refs.pack_digest is not None:
        payload["evidence_pack_digest"] = refs.pack_digest
    return TrialEvidenceBundle.model_validate(payload)


def ingest_after_settlement(
    settlement_identity: SettlementIdentity,
    artifact_refs: ArtifactRefs,
    finished_at: str,
) -> ComplianceIngestReport:
    """Pure evaluation after terminal execution, sanitization, CAS, and catalog settlement."""
    finished_at = _finished_at_for_digest(finished_at)
    pre_digest = compliance_input_digest(settlement_identity, artifact_refs, finished_at)
    if not settlement_identity.cataloged or not settlement_identity.cas_settled:
        raise ComplianceSettlementError("catalog_or_cas_not_settled", input_digest=pre_digest)
    try:
        bundle = _evaluation_view(settlement_identity, artifact_refs, finished_at)
        record = evaluate_trial_compliance(bundle)
        gates = readiness_gates(bundle, record)
        catalog = list(provenance_catalog(artifact_refs.registry_rows)) if artifact_refs.registry_rows else []
        bloat_clean = (
            tracked_output_is_manifest_only(artifact_refs.tracked_paths) if artifact_refs.tracked_paths else True
        )
        report = ComplianceIngestReport(
            input_digest=pre_digest,
            disposition=record.disposition,
            reasons=list(record.hold_reasons),
            lag_ms=record.lag_ms,
            record=record,
            gates=gates,
            catalog_entries=catalog,
            bloat_clean=bloat_clean,
        )
        sealed = _seal_report(report)
        if sealed.input_digest != pre_digest:
            raise ComplianceEngineError("input_digest_mismatch", input_digest=pre_digest)
        return sealed
    except ComplianceError:
        raise
    except Exception as exc:
        raise ComplianceEngineError(type(exc).__name__, input_digest=pre_digest) from exc


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
