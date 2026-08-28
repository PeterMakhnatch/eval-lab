"""Per-trial data compliance records (Engineer-Data).

Consumes settled catalog+CAS identity and evidence views read-only.
Does not own feature_registry.py, sql/, producers, traj_card, or CLI.
Never mints identities. Never synthesizes empty-schema values.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from evallab.schemas import ContractModel

SCHEMA_VERSION = "trial-compliance/v1"
SEQUENCE_MIN_STEPS = 5
REPEAT_MIN_TRIALS = 2
FEATURED_TRIALS_CURRENT = 44
CORPUS_TRIALS_CURRENT = 107
OUTCOME_LINEAGE_TOKENS = frozenset(
    {"task_success", "verdict", "final_invariant", "primary_reward", "reward"}
)

ComplianceDisposition = Literal["QUALITY_PASS", "QUALITY_WARN", "HOLD", "QUARANTINED"]
MeasurementRole = Literal["process", "outcome", "denominator", "identity"]
RegistryBasis = Literal["REGISTRY_CONFIRMED", "EMPIRICAL_DIAGNOSTIC", "NONE"]
DenominatorPolicy = Literal["required", "not_applicable"]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def canonical_digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()}"


class PlatformSettlement(ContractModel):
    job_id: str
    trial_id: str
    cas_uri: str
    cataloged: bool
    cas_settled: bool
    catalog_digest: str | None = None
    source_watermark: str | None = None
    projection_watermark: str | None = None


class TrialEvidenceBundle(ContractModel):
    settlement: PlatformSettlement
    task_name: str | None = None
    seed: str | None = None
    benchmark_family: str | None = None
    model_name: str | None = None
    agent_name: str | None = None
    task_success: bool | None = None
    source_trajectory_id: str | None = None
    ordered_steps_digest: str | None = None
    step_count: int | None = None
    source_step_id: int | None = None
    step_index: int | None = None
    error_observed: bool | None = None
    first_error_step: int | None = None
    error_evidence_ref: str | None = None
    error_count: int | None = None
    lock_event_observed: bool | None = None
    lock_step: int | None = None
    right_censored: bool | None = None
    censor_step: int | None = None
    lock_predicate_id: str | None = None
    lock_predicate_version: str | None = None
    lock_evidence_ref: str | None = None
    source_ir_digest: str | None = None
    evidence_pack_digest: str | None = None
    verifier_truth_digest: str | None = None
    trial_source_digest: str | None = None
    feature_row_digest: str | None = None
    registry_digest: str | None = None
    provenance_catalog_digest: str | None = None
    alphabet_ready: bool | None = None
    dose_ready: bool | None = None
    result_present: bool = False
    atif_present: bool = False
    native_events_present: bool = False
    benchmark_events_present: bool = False
    state_journal_present: bool = False
    loss_manifest_present: bool = False
    schema_valid: bool | None = None
    digest_valid: bool | None = None
    lineage_valid: bool | None = None
    citation_valid: bool | None = None
    producer_live: bool | None = None
    zero_opportunity: bool = False
    zero_variance: bool = False
    corrupt_evidence: bool = False
    infrastructure_fault: bool = False
    dimension_cross_contaminated: bool = False
    feature_row: dict[str, Any] = Field(default_factory=dict)
    registered_feature_names: list[str] = Field(default_factory=list)
    cohort_cell_trial_count: int | None = None
    recovery_opportunity: bool | None = None
    recovery_outcome: bool | None = None
    fault_opportunity_id: str | None = None
    finished_at: str | None = None
    ingested_at: str | None = None


class FeatureProvenanceEntry(ContractModel):
    column_name: str
    definition: str
    named_consumer: str | None = None
    formula_or_rule: str
    denominator_sibling: str | None = None
    denominator_policy: DenominatorPolicy | None = None
    producer_module: str
    producer_version: str
    coverage: str
    example: str | None = None
    refusal: str
    basis: RegistryBasis
    measurement_role: MeasurementRole
    available_before_verdict: bool
    declared_inputs: list[str] = Field(default_factory=list)
    source_table: str
    null_condition: str


class TrialComplianceRecord(ContractModel):
    schema_version: str = SCHEMA_VERSION
    disposition: ComplianceDisposition
    hold_reasons: list[str] = Field(default_factory=list)
    analysis_ready: bool
    job_id: str
    trial_id: str
    cas_uri: str
    task_name: str | None = None
    seed: str | None = None
    benchmark_family: str | None = None
    model_name: str | None = None
    agent_name: str | None = None
    task_success: bool | None = None
    repeat_group_id: str | None = None
    cluster_id: str | None = None
    repeated_measure_eligible: bool = False
    trial_source_digest: str | None = None
    feature_row_digest: str | None = None
    registry_digest: str | None = None
    provenance_catalog_digest: str | None = None
    source_trajectory_id: str | None = None
    ordered_steps_digest: str | None = None
    step_count: int | None = None
    source_step_id: int | None = None
    step_index: int | None = None
    error_observed: bool | None = None
    first_error_step: int | None = None
    error_evidence_ref: str | None = None
    error_count: int | None = None
    lock_event_observed: bool | None = None
    lock_step: int | None = None
    right_censored: bool | None = None
    censor_step: int | None = None
    lock_predicate_id: str | None = None
    lock_predicate_version: str | None = None
    lock_evidence_ref: str | None = None
    source_ir_digest: str | None = None
    evidence_pack_digest: str | None = None
    catalog_digest: str | None = None
    verifier_truth_digest: str | None = None
    alphabet_ready: bool | None = None
    dose_ready: bool | None = None
    sequence_eligible: bool = False
    cascade_eligible: bool = False
    recovery_censored: bool | None = None
    t_lock_contract_present: bool = False
    producer_live: bool | None = None
    lag_ms: int | None = None
    evaluated_at: str
    row_digest: str = ""
    source_digest: str | None = None
    record_digest: str = ""

    @model_validator(mode="after")
    def _seal(self) -> TrialComplianceRecord:
        reasons = sorted(set(self.hold_reasons))
        ready = self.disposition == "QUALITY_PASS" and not reasons
        object.__setattr__(self, "hold_reasons", reasons)
        object.__setattr__(self, "analysis_ready", ready)
        object.__setattr__(self, "source_digest", self.trial_source_digest)
        payload = self.model_dump(mode="json", exclude={"record_digest", "row_digest"})
        digest = canonical_digest(payload)
        object.__setattr__(self, "row_digest", digest)
        object.__setattr__(self, "record_digest", digest)
        return self


def _repeat_group_id(task_name: str | None, model_name: str | None) -> str | None:
    if not task_name or not model_name:
        return None
    return canonical_digest({"task_name": task_name, "model_name": model_name})


def lineage_depends_on_outcome(declared_inputs: Sequence[str]) -> bool:
    return bool({item.strip() for item in declared_inputs} & OUTCOME_LINEAGE_TOKENS)


def current_corpus_method_readiness() -> dict[str, Any]:
    return {
        "featured_trials": FEATURED_TRIALS_CURRENT,
        "corpus_trials": CORPUS_TRIALS_CURRENT,
        "T1.1": {"status": "HOLD_GATED"},
        "T1.2": {
            "status": "HOLD",
            "refusals": ["MISSING_RECOVERY_OUTCOME", "ZERO_OPPORTUNITY"],
            "evidence_unit": "fault_opportunity_id",
        },
        "T1.3": {
            "status": "HOLD",
            "refusals": ["T_LOCK_UNAVAILABLE", "CENSORING_UNAVAILABLE"],
        },
    }


def evaluate_trial_compliance(bundle: TrialEvidenceBundle) -> TrialComplianceRecord:
    reasons: list[str] = []
    settlement = bundle.settlement
    if not settlement.job_id.strip() or not settlement.trial_id.strip() or not settlement.cas_uri.strip():
        reasons.extend(["QUARANTINED", "identity_missing"])
    if not settlement.cataloged or not settlement.cas_settled:
        reasons.extend(["QUARANTINED", "catalog_or_cas_not_settled"])
    if bundle.infrastructure_fault or bundle.corrupt_evidence:
        reasons.extend(["QUARANTINED", "quarantine_corrupt_or_infra"])
    if bundle.schema_valid is not True or bundle.lineage_valid is not True or bundle.citation_valid is not True:
        reasons.append("INCOMPLETE_PROVENANCE")
    if bundle.digest_valid is not True:
        reasons.append("DIGEST_MISMATCH")
    if bundle.producer_live is not True:
        reasons.append("producer_stale_or_unevaluated")
    if (
        bundle.dimension_cross_contaminated
        or not bundle.model_name
        or not bundle.agent_name
        or not bundle.task_name
        or bundle.task_success is None
    ):
        reasons.append("MISSING_DIMENSION")
    if bundle.zero_opportunity:
        reasons.append("ZERO_OPPORTUNITY")
    if bundle.zero_variance:
        reasons.append("ZERO_VARIANCE")
    for label, present in (
        ("result", bundle.result_present),
        ("atif", bundle.atif_present),
        ("native_events", bundle.native_events_present),
        ("benchmark_events", bundle.benchmark_events_present),
        ("state_journal", bundle.state_journal_present),
        ("loss_manifest", bundle.loss_manifest_present),
    ):
        if not present:
            reasons.append(f"source_missing_{label}")
    if bundle.registered_feature_names and any(
        name not in set(bundle.registered_feature_names) for name in bundle.feature_row
    ):
        reasons.append("UNREGISTERED_FEATURE")
    if bundle.step_count is None or bundle.step_count < SEQUENCE_MIN_STEPS:
        reasons.append("SHORT_TRAJECTORY")
    if not bundle.lock_predicate_id or not bundle.lock_predicate_version or bundle.first_error_step is None:
        reasons.append("T_LOCK_UNAVAILABLE")
    if bundle.lock_event_observed is not True and bundle.right_censored is not True:
        reasons.append("CENSORING_UNAVAILABLE")
    if bundle.lock_event_observed is True and bundle.task_success is False and not bundle.lock_evidence_ref:
        reasons.append("lock_inferred_from_terminal_failure")
    if bundle.recovery_opportunity is not True:
        reasons.append("ZERO_OPPORTUNITY")
    if bundle.recovery_outcome is None:
        reasons.append("MISSING_RECOVERY_OUTCOME")
    repeated_ok = (
        bundle.cohort_cell_trial_count is not None
        and bundle.cohort_cell_trial_count >= REPEAT_MIN_TRIALS
        and bool(bundle.model_name and bundle.task_name)
    )
    if not repeated_ok:
        reasons.append("REPEAT_INELIGIBLE")
    if bundle.alphabet_ready is not True:
        reasons.append("ALPHABET_NOT_READY")
    if bundle.dose_ready is not True:
        reasons.append("DOSE_NOT_READY")
    if (
        bundle.settlement.source_watermark
        and bundle.settlement.projection_watermark
        and bundle.settlement.projection_watermark < bundle.settlement.source_watermark
    ):
        reasons.append("STALE_SNAPSHOT")
    if "QUARANTINED" in reasons:
        disposition: ComplianceDisposition = "QUARANTINED"
    elif reasons:
        disposition = "QUALITY_WARN" if set(reasons) <= {"ZERO_VARIANCE"} else "HOLD"
    else:
        disposition = "QUALITY_PASS"
    repeat_gid = _repeat_group_id(bundle.task_name, bundle.model_name)
    sequence_eligible = bundle.step_count is not None and bundle.step_count >= SEQUENCE_MIN_STEPS
    t_lock_present = bool(bundle.lock_predicate_id and bundle.lock_predicate_version)
    if bundle.right_censored is True:
        recovery_censored: bool | None = True
    elif bundle.lock_event_observed is True:
        recovery_censored = False
    else:
        recovery_censored = None
    lag_ms = None
    if bundle.finished_at and bundle.ingested_at:
        try:
            finished = datetime.fromisoformat(bundle.finished_at.replace("Z", "+00:00"))
            ingested = datetime.fromisoformat(bundle.ingested_at.replace("Z", "+00:00"))
            lag_ms = max(0, int((ingested - finished).total_seconds() * 1000))
        except ValueError:
            lag_ms = None
    return TrialComplianceRecord.model_validate(dict(
        disposition=disposition,
        hold_reasons=reasons,
        analysis_ready=False,
        job_id=settlement.job_id,
        trial_id=settlement.trial_id,
        cas_uri=settlement.cas_uri,
        task_name=bundle.task_name,
        seed=bundle.seed,
        benchmark_family=bundle.benchmark_family,
        model_name=bundle.model_name,
        agent_name=bundle.agent_name,
        task_success=bundle.task_success,
        repeat_group_id=repeat_gid,
        cluster_id=repeat_gid or (settlement.trial_id or None),
        repeated_measure_eligible=bool(repeated_ok),
        trial_source_digest=bundle.trial_source_digest,
        feature_row_digest=bundle.feature_row_digest,
        registry_digest=bundle.registry_digest,
        provenance_catalog_digest=bundle.provenance_catalog_digest,
        source_trajectory_id=bundle.source_trajectory_id,
        ordered_steps_digest=bundle.ordered_steps_digest,
        step_count=bundle.step_count,
        source_step_id=bundle.source_step_id,
        step_index=bundle.step_index,
        error_observed=bundle.error_observed,
        first_error_step=bundle.first_error_step,
        error_evidence_ref=bundle.error_evidence_ref,
        error_count=bundle.error_count,
        lock_event_observed=bundle.lock_event_observed,
        lock_step=bundle.lock_step,
        right_censored=bundle.right_censored,
        censor_step=bundle.censor_step,
        lock_predicate_id=bundle.lock_predicate_id,
        lock_predicate_version=bundle.lock_predicate_version,
        lock_evidence_ref=bundle.lock_evidence_ref,
        source_ir_digest=bundle.source_ir_digest,
        evidence_pack_digest=bundle.evidence_pack_digest,
        catalog_digest=settlement.catalog_digest,
        verifier_truth_digest=bundle.verifier_truth_digest,
        alphabet_ready=bundle.alphabet_ready is True,
        dose_ready=bundle.dose_ready is True,
        sequence_eligible=sequence_eligible,
        cascade_eligible=(
            sequence_eligible
            and t_lock_present
            and bundle.first_error_step is not None
            and "T_LOCK_UNAVAILABLE" not in reasons
            and "CENSORING_UNAVAILABLE" not in reasons
            and "SHORT_TRAJECTORY" not in reasons
        ),
        recovery_censored=recovery_censored,
        t_lock_contract_present=t_lock_present,
        producer_live=bundle.producer_live,
        lag_ms=lag_ms,
        evaluated_at=datetime.now(UTC).isoformat(),
        source_digest=bundle.trial_source_digest,
    ))


def ingest_settled_trial(bundle: TrialEvidenceBundle) -> TrialComplianceRecord:
    return evaluate_trial_compliance(bundle)


def ingest_settled_trial_idempotent(
    bundle: TrialEvidenceBundle,
    prior: TrialComplianceRecord | None = None,
) -> TrialComplianceRecord:
    current = evaluate_trial_compliance(bundle)
    if prior is None:
        return current
    if (
        prior.job_id,
        prior.trial_id,
        prior.cas_uri,
        prior.trial_source_digest,
        tuple(prior.hold_reasons),
    ) == (
        current.job_id,
        current.trial_id,
        current.cas_uri,
        current.trial_source_digest,
        tuple(current.hold_reasons),
    ):
        return prior
    return current


MALFORMED_REGISTRY = "malformed_registry_mapping"


def _registry_column_name(raw: Mapping[str, Any] | object) -> str | None:
    if not isinstance(raw, Mapping):
        return None
    name = raw.get("column_name")
    if not isinstance(name, str) or not name.strip():
        return None
    return name


def t11_lineage_blocking(registry_rows: Sequence[Mapping[str, Any]]) -> list[str]:
    blocked: list[str] = []
    for raw in registry_rows:
        name = _registry_column_name(raw)
        if name is None:
            blocked.append(MALFORMED_REGISTRY)
            continue
        if "declared_inputs" not in raw or "available_before_verdict" not in raw:
            blocked.append(name)
            continue
        declared = list(raw.get("declared_inputs") or [])
        role = str(raw.get("measurement_role") or "process")
        if role == "process" and (
            lineage_depends_on_outcome(declared) or raw.get("available_before_verdict") is not True
        ):
            blocked.append(name)
    return blocked



def denominator_policy_refusal(
    raw: Mapping[str, Any],
    *,
    known_columns: set[str],
) -> tuple[str | None, RegistryBasis]:
    """Declaration-only denominator checks. No suffix or DOUBLE inference."""
    policy = raw.get("denominator_policy")
    sibling = raw.get("denominator_sibling")
    null_on_zero = raw.get("null_on_zero_denominator")
    if policy is None:
        return "MISSING_DENOMINATOR_APPLICABILITY_DECLARATION", "REGISTRY_CONFIRMED"
    if policy not in {"required", "not_applicable"}:
        return "INVALID_DENOMINATOR_DECLARATION", "REGISTRY_CONFIRMED"
    if policy == "required":
        if not sibling:
            return "MISSING_DENOMINATOR_DECLARATION", "REGISTRY_CONFIRMED"
        if str(sibling) not in known_columns:
            return "INVALID_DENOMINATOR_DECLARATION", "REGISTRY_CONFIRMED"
        if null_on_zero is not True:
            return "MISSING_NULL_ON_ZERO_DECLARATION", "REGISTRY_CONFIRMED"
    if policy == "not_applicable" and (sibling or null_on_zero):
        return "INVALID_DENOMINATOR_DECLARATION", "REGISTRY_CONFIRMED"
    return None, "REGISTRY_CONFIRMED"


def missing_denominator_declaration(registry_rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Report-only overlay. Does not tighten FeatureRegistry.validate_contract."""
    known = {name for raw in registry_rows if (name := _registry_column_name(raw))}
    missing: list[str] = []
    for raw in registry_rows:
        name = _registry_column_name(raw)
        if name is None:
            missing.append(MALFORMED_REGISTRY)
            continue
        refusal, _basis = denominator_policy_refusal(raw, known_columns=known)
        if refusal == "MISSING_DENOMINATOR_DECLARATION":
            missing.append(name)
    return missing


def provenance_catalog(registry_rows: Sequence[Mapping[str, Any]]) -> list[FeatureProvenanceEntry]:
    known = {name for raw in registry_rows if (name := _registry_column_name(raw))}
    entries: list[FeatureProvenanceEntry] = []
    for raw in registry_rows:
        name = _registry_column_name(raw)
        if name is None:
            entries.append(
                FeatureProvenanceEntry(
                    column_name=MALFORMED_REGISTRY,
                    definition="",
                    formula_or_rule="",
                    producer_module="unknown",
                    producer_version="unknown",
                    coverage="unknown",
                    refusal=MALFORMED_REGISTRY,
                    basis="REGISTRY_CONFIRMED",
                    measurement_role="process",
                    available_before_verdict=False,
                    source_table="",
                    null_condition="NULL",
                )
            )
            continue
        declared = list(raw.get("declared_inputs") or [])
        role_raw = str(raw.get("measurement_role") or "process")
        role: MeasurementRole = (
            role_raw if role_raw in {"process", "outcome", "denominator", "identity"} else "process"
        )
        missing_lineage = "declared_inputs" not in raw or "available_before_verdict" not in raw
        outcome_violation = role == "process" and lineage_depends_on_outcome(declared)
        denom_refusal, denom_basis = denominator_policy_refusal(raw, known_columns=known)
        policy_raw = raw.get("denominator_policy")
        policy: DenominatorPolicy | None = (
            policy_raw if policy_raw in {"required", "not_applicable"} else None
        )
        if missing_lineage:
            refusal = "MISSING_LINEAGE_DECLARATION"
            basis: RegistryBasis = "REGISTRY_CONFIRMED"
            consumer = None
        elif outcome_violation:
            refusal = "OUTCOME_LINEAGE_VIOLATION"
            basis = "REGISTRY_CONFIRMED"
            consumer = None
        elif denom_refusal:
            refusal = denom_refusal
            basis = denom_basis
            consumer = None
        else:
            refusal = str(raw.get("null_condition") or "NULL on missing or zero denominator")
            basis = "REGISTRY_CONFIRMED"
            consumer = raw.get("named_consumer")
        entries.append(
            FeatureProvenanceEntry(
                column_name=name,
                definition=str(raw.get("description") or raw.get("formula_or_rule") or ""),
                named_consumer=consumer,
                formula_or_rule=str(raw.get("formula_or_rule") or ""),
                denominator_sibling=raw.get("denominator_sibling"),
                denominator_policy=policy,
                producer_module=str(raw.get("producer_module") or "unknown"),
                producer_version=str(raw.get("producer_version") or "unknown"),
                coverage=str(raw.get("coverage") or "unknown"),
                example=raw.get("example"),
                refusal=refusal,
                basis=basis,
                measurement_role=role,
                available_before_verdict=bool(raw.get("available_before_verdict", False)),
                declared_inputs=declared,
                source_table=str(raw.get("source_table") or ""),
                null_condition=str(raw.get("null_condition") or "NULL"),
            )
        )
    return entries


def v_analysis_ready_trials(records: Sequence[TrialComplianceRecord]) -> list[TrialComplianceRecord]:
    return [row for row in records if row.disposition == "QUALITY_PASS" and row.analysis_ready]


def tracked_output_is_manifest_only(paths: Sequence[str]) -> bool:
    forbidden = ("/runs/", "derived/parquet", "derived/evidence-cas", "runs/")
    allowed = (".json", ".md", ".toml")
    return all(
        not any(token in path.replace("\\", "/") for token in forbidden) and path.endswith(allowed)
        for path in paths
    )
