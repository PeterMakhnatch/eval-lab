"""Deterministic T1 analysis consumers over frozen analysis-ready contracts.

This module is deliberately read-only. It accepts typed rows produced by the data
and feature-projection lanes, emits versioned analysis results, and never mutates
feature registries, projection tables, campaign manifests, approvals, or schedules.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from evallab.cohort import BOOTSTRAP_RESAMPLES
from evallab.evidence.capture_authority import CaptureAuthority
from evallab.schemas import ContractModel

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
FeatureValue = bool | int | float | str | None

T11_METHOD_VERSION = "t1.1-outcome-lineage/v1"
T12_METHOD_VERSION = "t1.2-conditional-recovery/v1"
T13_METHOD_VERSION = "t1.3-cascade-distance/v1"

_POST_VERDICT_INPUTS = frozenset(
    {
        "invariants_passed",
        "task_success",
        "primary_reward",
        "verdict",
        "final_verdict",
    }
)

_ZERO_DIGEST = "sha256:" + "0" * 64
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _clean_source_digest(val: Any) -> str | None:
    if not val or not isinstance(val, str):
        return None
    s = val.strip()
    if not _DIGEST_PATTERN.match(s) or s == _ZERO_DIGEST:
        return None
    return s


ADMISSIBLE_CAPTURE_AUTHORITIES = frozenset(
    {
        CaptureAuthority.BENCHMARK_EVENTS,
        CaptureAuthority.ATIF_TRAJECTORY,
        CaptureAuthority.BENCHMARK_EVENTS.value,
        CaptureAuthority.ATIF_TRAJECTORY.value,
    }
)


def _validate_non_zero_digest(value: str | None, field_name: str) -> None:
    if value is not None and value == _ZERO_DIGEST:
        raise ValueError(f"{field_name} cannot be all-zero digest")


class Verdict(StrEnum):
    LINEAGE_VIOLATION = "LINEAGE_VIOLATION"
    MISSING_LINEAGE_DECLARATION = "MISSING_LINEAGE_DECLARATION"
    MISSING_DENOMINATOR_APPLICABILITY_DECLARATION = "MISSING_DENOMINATOR_APPLICABILITY_DECLARATION"
    MISSING_DENOMINATOR_DECLARATION = "MISSING_DENOMINATOR_DECLARATION"
    MISSING_NULL_ON_ZERO_DECLARATION = "MISSING_NULL_ON_ZERO_DECLARATION"
    INVALID_DENOMINATOR_DECLARATION = "INVALID_DENOMINATOR_DECLARATION"
    EMPIRICAL_SUSPECT = "EMPIRICAL_SUSPECT"
    CLEAR = "CLEAR"
    UNDERPOWERED = "UNDERPOWERED"
    SINGLE_OUTCOME_CLASS = "SINGLE_OUTCOME_CLASS"


class Basis(StrEnum):
    REGISTRY_CONFIRMED = "REGISTRY_CONFIRMED"
    EMPIRICAL_DIAGNOSTIC = "EMPIRICAL_DIAGNOSTIC"
    NONE = "NONE"


class CIDisposition(StrEnum):
    BLOCK = "BLOCK"
    ADVISORY = "ADVISORY"
    CLEAR = "CLEAR"


class DenominatorPolicy(StrEnum):
    REQUIRED = "required"
    NOT_APPLICABLE = "not_applicable"


class RefusalCode(StrEnum):
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    DIGEST_MISMATCH = "DIGEST_MISMATCH"
    MISSING_LINEAGE_DECLARATION = "MISSING_LINEAGE_DECLARATION"
    OUTCOME_LINEAGE_VIOLATION = "OUTCOME_LINEAGE_VIOLATION"
    MISSING_DENOMINATOR_APPLICABILITY_DECLARATION = "MISSING_DENOMINATOR_APPLICABILITY_DECLARATION"
    MISSING_DENOMINATOR_DECLARATION = "MISSING_DENOMINATOR_DECLARATION"
    MISSING_NULL_ON_ZERO_DECLARATION = "MISSING_NULL_ON_ZERO_DECLARATION"
    INVALID_DENOMINATOR_DECLARATION = "INVALID_DENOMINATOR_DECLARATION"
    UNDERPOWERED = "UNDERPOWERED"
    SINGLE_OUTCOME_CLASS = "SINGLE_OUTCOME_CLASS"
    ZERO_VARIANCE = "ZERO_VARIANCE"
    ZERO_OPPORTUNITY = "ZERO_OPPORTUNITY"
    MISSING_RECOVERY_OUTCOME = "MISSING_RECOVERY_OUTCOME"
    REPEAT_INELIGIBLE = "REPEAT_INELIGIBLE"
    SHORT_TRAJECTORY = "SHORT_TRAJECTORY"
    T_ERR_UNAVAILABLE = "T_ERR_UNAVAILABLE"
    T_LOCK_UNAVAILABLE = "T_LOCK_UNAVAILABLE"
    CENSORING_UNAVAILABLE = "CENSORING_UNAVAILABLE"
    INVALID_CASCADE_ORDER = "INVALID_CASCADE_ORDER"
    DUPLICATE_ASSIGNMENT_UNIT = "DUPLICATE_ASSIGNMENT_UNIT"
    MISSING_PAIR_ARM = "MISSING_PAIR_ARM"
    UNDERFILLED_REPEATS = "UNDERFILLED_REPEATS"
    INVALID_BINARY_INPUT = "INVALID_BINARY_INPUT"
    CAPTURE_INCOMPLETE = "CAPTURE_INCOMPLETE"
    ANALYSIS_UNIT_UNDECLARED = "ANALYSIS_UNIT_UNDECLARED"
    PAIRING_IDENTITY_MISMATCH = "PAIRING_IDENTITY_MISMATCH"
    CAPTURE_AUTHORITY_UNRESOLVED = "CAPTURE_AUTHORITY_UNRESOLVED"
    REVIEW_QUEUE_INELIGIBLE = "REVIEW_QUEUE_INELIGIBLE"
    SEMANTIC_DECISION_USE_FORBIDDEN = "SEMANTIC_DECISION_USE_FORBIDDEN"
    UNSUPPORTED_ANALYSIS_METHOD = "UNSUPPORTED_ANALYSIS_METHOD"


class AnalysisStatus(StrEnum):
    VALID = "VALID"
    REFUSAL = "REFUSAL"


class CascadeStatus(StrEnum):
    OBSERVED = "OBSERVED"
    CENSORED = "CENSORED"
    REFUSED = "REFUSED"


class FeatureContractRow(ContractModel):
    """Registry metadata consumed by T1.1 without mutating the registry."""

    feature_name: str = Field(min_length=1)
    is_new_feature: bool
    declared_inputs: tuple[str, ...] | None = None
    available_before_verdict: bool | None = None
    denominator_policy: DenominatorPolicy | None = None
    denominator_sibling: str | None = None
    null_on_zero_denominator: bool = False
    binary_projection: bool = False


class FeatureObservation(ContractModel):
    feature_name: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    task_success: bool
    value: FeatureValue


class StratumDiagnostics(ContractModel):
    task_success: bool
    n: int = Field(ge=0)
    distinct_count: int = Field(ge=0)
    variance: float | None = Field(default=None, ge=0)


class EmpiricalDiagnostics(ContractModel):
    n_nonnull: int = Field(ge=0)
    strata: tuple[StratumDiagnostics, StratumDiagnostics]
    auc_x_to_task_success: float | None = Field(default=None, ge=0, le=1)
    disagreement_rate: float | None = Field(default=None, ge=0, le=1)
    sample_degenerate: bool
    zero_variance: bool
    refusal_code: RefusalCode | None = None


class FeatureGateResult(ContractModel):
    feature_name: str
    verdict: Verdict
    basis: Basis
    ci_disposition: CIDisposition
    requires_allowlist: bool
    structural_violations: tuple[Verdict, ...] = ()
    empirical: EmpiricalDiagnostics


class T11Report(ContractModel):
    method_version: Literal["t1.1-outcome-lineage/v1"]
    source_analysis_snapshot_digest: Digest
    input_digest: Digest
    results: tuple[FeatureGateResult, ...]
    report_digest: Digest


class RecoveryOpportunity(ContractModel):
    fault_opportunity_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    repeat_group_id: str | None = None
    repeat_eligible: bool | None = None
    task_name: str | None = None
    model_name: str | None = None
    eligible: bool
    recovered: bool | None = None
    source_digest: Digest

    @property
    def cluster_id(self) -> str:
        return self.repeat_group_id or self.trial_id


class RecoveryAnalysisResult(ContractModel):
    method_version: Literal["t1.2-conditional-recovery/v1"]
    source_analysis_snapshot_digest: Digest
    cohort_key: str
    input_digest: Digest
    result_digest: Digest
    evidence_unit: Literal["fault_opportunity_id"]
    cluster_key: Literal["coalesce(repeat_group_id,trial_id)"]
    n_total: int = Field(ge=0)
    n_effective: int = Field(ge=0)
    recovered_count: int = Field(ge=0)
    estimate: float | None = Field(default=None, ge=0, le=1)
    interval_lower: float | None = Field(default=None, ge=0, le=1)
    interval_upper: float | None = Field(default=None, ge=0, le=1)
    confidence_level: float = Field(gt=0, lt=1)
    uncertainty_method: Literal["percentile_cluster_bootstrap"]
    resamples: int = Field(ge=0)
    seed_digest: Digest
    status: AnalysisStatus
    refusal_code: RefusalCode | None = None


class CascadeTrialInput(ContractModel):
    trial_id: str = Field(min_length=1)
    step_count: int = Field(ge=0)
    first_error_step: int | None = None
    lock_step: int | None = None
    lock_event_observed: bool
    right_censored: bool
    censor_step: int | None = None
    lock_predicate_id: str | None = None
    lock_predicate_version: str | None = None
    lock_evidence_ref: str | None = None
    source_digest: Digest


class CascadeTrialResult(ContractModel):
    trial_id: str
    status: CascadeStatus
    first_error_step: int | None = None
    lock_step: int | None = None
    censor_step: int | None = None
    cascade_distance: int | None = Field(default=None, ge=0)
    refusal_code: RefusalCode | None = None
    source_digest: Digest


class T13Report(ContractModel):
    method_version: Literal["t1.3-cascade-distance/v1"]
    source_analysis_snapshot_digest: Digest
    input_digest: Digest
    results: tuple[CascadeTrialResult, ...]
    report_digest: Digest


_STRUCTURAL_PRECEDENCE = (
    Verdict.LINEAGE_VIOLATION,
    Verdict.MISSING_LINEAGE_DECLARATION,
    Verdict.MISSING_DENOMINATOR_APPLICABILITY_DECLARATION,
    Verdict.MISSING_DENOMINATOR_DECLARATION,
    Verdict.MISSING_NULL_ON_ZERO_DECLARATION,
    Verdict.INVALID_DENOMINATOR_DECLARATION,
)


def _in_step_range(step: int, step_count: int) -> bool:
    return 1 <= step <= step_count


def _repeat_cell_underfilled(rows: Sequence[RecoveryOpportunity]) -> bool:
    cells: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        if not row.task_name or not row.model_name:
            return True
        cells[(row.task_name, row.model_name)].add(row.trial_id)
    return any(len(trial_ids) < 2 for trial_ids in cells.values())


def _canonical_digest(value: object) -> str:
    payload_value: object = (
        value.model_dump(mode="json", exclude_none=False) if isinstance(value, BaseModel) else value
    )
    payload = json.dumps(
        payload_value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _report_digest(body: object) -> str:
    return _canonical_digest(body)


def _canonical_input_name(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _structural_violations(contract: FeatureContractRow) -> tuple[Verdict, ...]:
    violations: list[Verdict] = []
    if contract.declared_inputs is None or contract.available_before_verdict is None:
        violations.append(Verdict.MISSING_LINEAGE_DECLARATION)
    elif not contract.available_before_verdict or any(
        _canonical_input_name(name) in _POST_VERDICT_INPUTS for name in contract.declared_inputs
    ):
        violations.append(Verdict.LINEAGE_VIOLATION)

    if contract.denominator_policy is None:
        violations.append(Verdict.MISSING_DENOMINATOR_APPLICABILITY_DECLARATION)
    elif contract.denominator_policy is DenominatorPolicy.REQUIRED:
        if not contract.denominator_sibling:
            violations.append(Verdict.MISSING_DENOMINATOR_DECLARATION)
        if contract.null_on_zero_denominator is not True:
            violations.append(Verdict.MISSING_NULL_ON_ZERO_DECLARATION)
    elif contract.denominator_sibling or contract.null_on_zero_denominator:
        violations.append(Verdict.INVALID_DENOMINATOR_DECLARATION)

    return tuple(verdict for verdict in _STRUCTURAL_PRECEDENCE if verdict in violations)


def _numeric_value(value: FeatureValue) -> float | None:
    if isinstance(value, (bool, int, float)):
        return float(value)
    return None


def _auc(observations: Sequence[FeatureObservation]) -> float | None:
    numeric = [(_numeric_value(row.value), row.task_success) for row in observations]
    if any(value is None for value, _ in numeric):
        return None
    pairs = sorted((float(value), outcome) for value, outcome in numeric if value is not None)
    positives = sum(outcome for _, outcome in pairs)
    negatives = len(pairs) - positives
    if positives == 0 or negatives == 0:
        return None

    positive_rank_sum = 0.0
    index = 0
    while index < len(pairs):
        stop = index + 1
        while stop < len(pairs) and pairs[stop][0] == pairs[index][0]:
            stop += 1
        average_rank = ((index + 1) + stop) / 2
        positive_rank_sum += average_rank * sum(outcome for _, outcome in pairs[index:stop])
        index = stop
    return (positive_rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def _empirical_diagnostics(
    contract: FeatureContractRow,
    observations: Sequence[FeatureObservation],
    *,
    clearance_n: int,
) -> EmpiricalDiagnostics:
    populated = [row for row in observations if row.value is not None]
    strata: list[StratumDiagnostics] = []
    for outcome in (False, True):
        values = [row.value for row in populated if row.task_success is outcome]
        numeric = [_numeric_value(value) for value in values]
        variance = None
        if len(values) >= 2 and all(value is not None for value in numeric):
            variance = statistics.pvariance(float(value) for value in numeric if value is not None)
        strata.append(
            StratumDiagnostics(
                task_success=outcome,
                n=len(values),
                distinct_count=len(set(values)),
                variance=variance,
            )
        )

    total_distinct = len({row.value for row in populated})
    zero_variance = bool(populated) and total_distinct <= 1
    both_classes = all(stratum.n > 0 for stratum in strata)
    display_ready = all(stratum.n >= 2 for stratum in strata)
    sample_degenerate = both_classes and all(stratum.distinct_count == 1 for stratum in strata)

    disagreement_rate = None
    if (
        contract.binary_projection
        and populated
        and all(isinstance(row.value, bool) or row.value in (0, 1) for row in populated)
    ):
        disagreement_rate = statistics.fmean(
            bool(row.value) is not row.task_success for row in populated
        )

    refusal_code = None
    if len(populated) < clearance_n:
        refusal_code = RefusalCode.UNDERPOWERED
    elif not both_classes:
        refusal_code = RefusalCode.SINGLE_OUTCOME_CLASS

    return EmpiricalDiagnostics(
        n_nonnull=len(populated),
        strata=(strata[0], strata[1]),
        auc_x_to_task_success=_auc(populated) if display_ready else None,
        disagreement_rate=disagreement_rate if display_ready else None,
        sample_degenerate=sample_degenerate,
        zero_variance=zero_variance,
        refusal_code=refusal_code,
    )


def evaluate_process_outcome_gate(
    contracts: Iterable[FeatureContractRow],
    observations: Iterable[FeatureObservation],
    *,
    source_analysis_snapshot_digest: Digest,
    clearance_n: int = 20,
) -> T11Report:
    """Evaluate structural T1.1 gates and advisory empirical diagnostics."""
    if clearance_n < 1:
        raise ValueError("clearance_n must be positive")
    contract_rows = sorted(contracts, key=lambda row: row.feature_name)
    if len({row.feature_name for row in contract_rows}) != len(contract_rows):
        raise ValueError("feature contracts must have unique feature_name values")

    observation_rows = sorted(observations, key=lambda row: (row.feature_name, row.trial_id))
    observation_keys = [(row.feature_name, row.trial_id) for row in observation_rows]
    if len(set(observation_keys)) != len(observation_keys):
        raise ValueError("feature observations must be unique by (feature_name, trial_id)")
    known_features = {row.feature_name for row in contract_rows}
    unknown = sorted({row.feature_name for row in observation_rows} - known_features)
    if unknown:
        raise ValueError(f"observations reference unregistered features: {', '.join(unknown)}")

    grouped: dict[str, list[FeatureObservation]] = defaultdict(list)
    for row in observation_rows:
        grouped[row.feature_name].append(row)

    results: list[FeatureGateResult] = []
    for contract in contract_rows:
        empirical = _empirical_diagnostics(
            contract,
            grouped.get(contract.feature_name, ()),
            clearance_n=clearance_n,
        )
        violations = _structural_violations(contract)
        if violations:
            verdict = violations[0]
            basis = Basis.REGISTRY_CONFIRMED
            disposition = CIDisposition.BLOCK if contract.is_new_feature else CIDisposition.ADVISORY
            requires_allowlist = not contract.is_new_feature
        elif empirical.refusal_code is RefusalCode.UNDERPOWERED:
            verdict = Verdict.UNDERPOWERED
            basis = Basis.NONE
            disposition = CIDisposition.ADVISORY
            requires_allowlist = False
        elif empirical.refusal_code is RefusalCode.SINGLE_OUTCOME_CLASS:
            verdict = Verdict.SINGLE_OUTCOME_CLASS
            basis = Basis.NONE
            disposition = CIDisposition.ADVISORY
            requires_allowlist = False
        elif empirical.sample_degenerate or empirical.zero_variance:
            verdict = Verdict.EMPIRICAL_SUSPECT
            basis = Basis.EMPIRICAL_DIAGNOSTIC
            disposition = CIDisposition.ADVISORY
            requires_allowlist = False
        else:
            verdict = Verdict.CLEAR
            basis = Basis.EMPIRICAL_DIAGNOSTIC
            disposition = CIDisposition.CLEAR
            requires_allowlist = False
        results.append(
            FeatureGateResult(
                feature_name=contract.feature_name,
                verdict=verdict,
                basis=basis,
                ci_disposition=disposition,
                requires_allowlist=requires_allowlist,
                structural_violations=violations,
                empirical=empirical,
            )
        )

    input_body = {
        "contracts": [row.model_dump(mode="json") for row in contract_rows],
        "observations": [row.model_dump(mode="json") for row in observation_rows],
        "clearance_n": clearance_n,
    }
    input_digest = _canonical_digest(input_body)
    report_body = {
        "method_version": T11_METHOD_VERSION,
        "source_analysis_snapshot_digest": source_analysis_snapshot_digest,
        "input_digest": input_digest,
        "results": [row.model_dump(mode="json") for row in results],
    }
    return T11Report(
        **report_body,
        report_digest=_report_digest(report_body),
    )


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _recovery_result(
    *,
    source_analysis_snapshot_digest: str,
    cohort_key: str,
    input_digest: str,
    n_total: int,
    n_effective: int,
    recovered_count: int,
    confidence_level: float,
    resamples: int,
    seed_digest: str,
    status: AnalysisStatus,
    refusal_code: RefusalCode | None,
    estimate: float | None = None,
    interval_lower: float | None = None,
    interval_upper: float | None = None,
) -> RecoveryAnalysisResult:
    body = {
        "method_version": T12_METHOD_VERSION,
        "source_analysis_snapshot_digest": source_analysis_snapshot_digest,
        "cohort_key": cohort_key,
        "input_digest": input_digest,
        "evidence_unit": "fault_opportunity_id",
        "cluster_key": "coalesce(repeat_group_id,trial_id)",
        "n_total": n_total,
        "n_effective": n_effective,
        "recovered_count": recovered_count,
        "estimate": estimate,
        "interval_lower": interval_lower,
        "interval_upper": interval_upper,
        "confidence_level": confidence_level,
        "uncertainty_method": "percentile_cluster_bootstrap",
        "resamples": resamples,
        "seed_digest": seed_digest,
        "status": status,
        "refusal_code": refusal_code,
    }
    return RecoveryAnalysisResult(**body, result_digest=_report_digest(body))


def analyze_conditional_recovery(
    opportunities: Iterable[RecoveryOpportunity],
    *,
    source_analysis_snapshot_digest: Digest,
    cohort_key: str,
    confidence_level: float = 0.95,
    resamples: int = BOOTSTRAP_RESAMPLES,
    minimum_effective_n: int = 2,
) -> RecoveryAnalysisResult:
    """Estimate per-fault recovery with deterministic cluster bootstrap."""
    if not 0 < confidence_level < 1:
        raise ValueError("confidence_level must be between zero and one")
    if resamples < 1:
        raise ValueError("resamples must be positive")
    if minimum_effective_n < 2:
        raise ValueError("minimum_effective_n must be at least 2")

    rows = sorted(
        opportunities,
        key=lambda row: (row.cluster_id, row.trial_id, row.fault_opportunity_id),
    )
    opportunity_ids = [row.fault_opportunity_id for row in rows]
    if len(set(opportunity_ids)) != len(opportunity_ids):
        raise ValueError("fault_opportunity_id values must be unique")
    input_digest = _canonical_digest([row.model_dump(mode="json") for row in rows])
    seed_digest = _canonical_digest(
        {
            "snapshot": source_analysis_snapshot_digest,
            "method_version": T12_METHOD_VERSION,
            "cohort_key": cohort_key,
            "input_digest": input_digest,
        }
    )

    eligible = [row for row in rows if row.eligible]
    if not eligible:
        return _recovery_result(
            source_analysis_snapshot_digest=source_analysis_snapshot_digest,
            cohort_key=cohort_key,
            input_digest=input_digest,
            n_total=0,
            n_effective=0,
            recovered_count=0,
            confidence_level=confidence_level,
            resamples=0,
            seed_digest=seed_digest,
            status=AnalysisStatus.REFUSAL,
            refusal_code=RefusalCode.ZERO_OPPORTUNITY,
        )
    if any(row.repeat_eligible is not True for row in eligible) or _repeat_cell_underfilled(
        eligible
    ):
        return _recovery_result(
            source_analysis_snapshot_digest=source_analysis_snapshot_digest,
            cohort_key=cohort_key,
            input_digest=input_digest,
            n_total=len(eligible),
            n_effective=len({row.cluster_id for row in eligible}),
            recovered_count=sum(row.recovered is True for row in eligible),
            confidence_level=confidence_level,
            resamples=0,
            seed_digest=seed_digest,
            status=AnalysisStatus.REFUSAL,
            refusal_code=RefusalCode.REPEAT_INELIGIBLE,
        )
    if any(row.recovered is None for row in eligible):
        return _recovery_result(
            source_analysis_snapshot_digest=source_analysis_snapshot_digest,
            cohort_key=cohort_key,
            input_digest=input_digest,
            n_total=len(eligible),
            n_effective=len({row.cluster_id for row in eligible}),
            recovered_count=sum(row.recovered is True for row in eligible),
            confidence_level=confidence_level,
            resamples=0,
            seed_digest=seed_digest,
            status=AnalysisStatus.REFUSAL,
            refusal_code=RefusalCode.MISSING_RECOVERY_OUTCOME,
        )

    grouped: dict[str, list[int]] = defaultdict(list)
    for row in eligible:
        grouped[row.cluster_id].append(int(row.recovered is True))
    cluster_ids = sorted(grouped)
    if len(cluster_ids) < minimum_effective_n:
        return _recovery_result(
            source_analysis_snapshot_digest=source_analysis_snapshot_digest,
            cohort_key=cohort_key,
            input_digest=input_digest,
            n_total=len(eligible),
            n_effective=len(cluster_ids),
            recovered_count=sum(row.recovered is True for row in eligible),
            confidence_level=confidence_level,
            resamples=0,
            seed_digest=seed_digest,
            status=AnalysisStatus.REFUSAL,
            refusal_code=RefusalCode.UNDERPOWERED,
        )

    recovered_count = sum(row.recovered is True for row in eligible)
    estimate = recovered_count / len(eligible)
    generator = random.Random(int(seed_digest.removeprefix("sha256:"), 16))
    bootstrap: list[float] = []
    for _ in range(resamples):
        selected = [cluster_ids[generator.randrange(len(cluster_ids))] for _ in cluster_ids]
        values = [value for cluster_id in selected for value in grouped[cluster_id]]
        bootstrap.append(sum(values) / len(values))
    tail = (1 - confidence_level) / 2
    return _recovery_result(
        source_analysis_snapshot_digest=source_analysis_snapshot_digest,
        cohort_key=cohort_key,
        input_digest=input_digest,
        n_total=len(eligible),
        n_effective=len(cluster_ids),
        recovered_count=recovered_count,
        estimate=estimate,
        interval_lower=_quantile(bootstrap, tail),
        interval_upper=_quantile(bootstrap, 1 - tail),
        confidence_level=confidence_level,
        resamples=resamples,
        seed_digest=seed_digest,
        status=AnalysisStatus.VALID,
        refusal_code=None,
    )


def _cascade_refusal(row: CascadeTrialInput, code: RefusalCode) -> CascadeTrialResult:
    return CascadeTrialResult(
        trial_id=row.trial_id,
        status=CascadeStatus.REFUSED,
        first_error_step=row.first_error_step,
        lock_step=row.lock_step,
        censor_step=row.censor_step,
        refusal_code=code,
        source_digest=row.source_digest,
    )


def _evaluate_cascade(row: CascadeTrialInput) -> CascadeTrialResult:
    if row.step_count < 5:
        return _cascade_refusal(row, RefusalCode.SHORT_TRAJECTORY)
    if row.first_error_step is None or not _in_step_range(row.first_error_step, row.step_count):
        return _cascade_refusal(row, RefusalCode.T_ERR_UNAVAILABLE)
    if row.lock_event_observed and row.right_censored:
        return _cascade_refusal(row, RefusalCode.CENSORING_UNAVAILABLE)

    if row.lock_event_observed:
        if (
            not row.lock_predicate_id
            or not row.lock_predicate_version
            or row.lock_step is None
            or not row.lock_evidence_ref
        ):
            return _cascade_refusal(row, RefusalCode.T_LOCK_UNAVAILABLE)
        if not row.first_error_step <= row.lock_step <= row.step_count:
            return _cascade_refusal(row, RefusalCode.INVALID_CASCADE_ORDER)
        return CascadeTrialResult(
            trial_id=row.trial_id,
            status=CascadeStatus.OBSERVED,
            first_error_step=row.first_error_step,
            lock_step=row.lock_step,
            cascade_distance=row.lock_step - row.first_error_step,
            source_digest=row.source_digest,
        )

    if not row.right_censored or row.censor_step is None:
        return _cascade_refusal(row, RefusalCode.CENSORING_UNAVAILABLE)
    if row.lock_step is not None or row.lock_evidence_ref is not None:
        return _cascade_refusal(row, RefusalCode.CENSORING_UNAVAILABLE)
    if not row.first_error_step <= row.censor_step <= row.step_count:
        return _cascade_refusal(row, RefusalCode.CENSORING_UNAVAILABLE)
    return CascadeTrialResult(
        trial_id=row.trial_id,
        status=CascadeStatus.CENSORED,
        first_error_step=row.first_error_step,
        censor_step=row.censor_step,
        source_digest=row.source_digest,
    )


def analyze_cascade_distance(
    trials: Iterable[CascadeTrialInput],
    *,
    source_analysis_snapshot_digest: Digest,
) -> T13Report:
    """Emit observed cascade distances, censored rows, and conjunctive refusals."""
    rows = sorted(trials, key=lambda row: row.trial_id)
    if len({row.trial_id for row in rows}) != len(rows):
        raise ValueError("cascade trial_id values must be unique")
    input_digest = _canonical_digest([row.model_dump(mode="json") for row in rows])
    results = tuple(_evaluate_cascade(row) for row in rows)
    report_body = {
        "method_version": T13_METHOD_VERSION,
        "source_analysis_snapshot_digest": source_analysis_snapshot_digest,
        "input_digest": input_digest,
        "results": [row.model_dump(mode="json") for row in results],
    }
    return T13Report(
        **report_body,
        report_digest=_report_digest(report_body),
    )


# =============================================================================
# Architecture Slice P/E Analysis Contracts & Deterministic Runner
# =============================================================================


class AnalysisMethod(StrEnum):
    RATE_WILSON = "rate_wilson"
    PAIRED_SIGN = "paired_sign"
    FISHER_2X2 = "fisher_2x2"
    DISPERSION_ICC = "dispersion_icc"
    DESIGN_EFFECT = "design_effect"
    ORDER_DISTANCE = "order_distance"
    DESCRIPTIVE_COUNTS = "descriptive_counts"


class AnalysisUnit(StrEnum):
    TRIAL = "trial"
    CELL = "cell"
    PAIRED_SEED = "paired_seed"
    LOGICAL_TRAJECTORY = "logical_trajectory"


class ContextCitation(ContractModel):
    path: str = Field(min_length=1)
    digest: Digest | None = None
    step_id: int | None = None
    tool_call_id: str | None = None
    supports: str | None = None


def compute_spec_digest(spec: Mapping[str, Any] | CampaignAnalysisSpecV1) -> str:
    if isinstance(spec, CampaignAnalysisSpecV1):
        payload = spec.model_dump(mode="json", exclude={"spec_digest"})
    else:
        raw = dict(spec)
        raw.pop("spec_digest", None)
        normalized = {
            "schema_version": raw.get("schema_version", "campaign-analysis-spec/v1"),
            "spec_id": raw["spec_id"],
            "method": raw["method"].value
            if isinstance(raw["method"], StrEnum)
            else str(raw["method"]),
            "outcome_feature": raw["outcome_feature"],
            "predictor_features": [
                f.value if isinstance(f, StrEnum) else str(f)
                for f in raw.get("predictor_features", ())
            ],
            "group_by": [
                g.value if isinstance(g, StrEnum) else str(g) for g in raw.get("group_by", ())
            ],
            "unit": raw["unit"].value if isinstance(raw["unit"], StrEnum) else str(raw["unit"]),
            "unit_keys": list(raw["unit_keys"]),
            "pair_keys": list(raw.get("pair_keys", ())),
            "cluster_keys": list(raw.get("cluster_keys", ())),
            "denominator_policy": (
                raw["denominator_policy"].value
                if isinstance(raw["denominator_policy"], StrEnum)
                else str(raw["denominator_policy"])
            ),
            "alpha": float(raw.get("alpha", 0.05)),
            "ci_method": raw.get("ci_method", "wilson"),
            "bootstrap_resamples": raw.get("bootstrap_resamples"),
            "minimum_informative_units": raw.get("minimum_informative_units"),
            "retrieval_inputs_allowed": False,
        }
        payload = normalized
    return _canonical_digest(payload)


class CampaignAnalysisSpecV1(ContractModel):
    schema_version: Literal["campaign-analysis-spec/v1"] = "campaign-analysis-spec/v1"
    spec_id: str = Field(min_length=1)
    spec_digest: Digest
    method: AnalysisMethod
    outcome_feature: str = Field(min_length=1)
    predictor_features: tuple[str, ...] = ()
    group_by: tuple[str, ...] = ()
    unit: AnalysisUnit
    unit_keys: tuple[str, ...]
    pair_keys: tuple[str, ...] = ()
    cluster_keys: tuple[str, ...] = ()
    denominator_policy: DenominatorPolicy
    alpha: float = Field(default=0.05, gt=0.0, lt=1.0)
    ci_method: Literal["wilson", "cluster_bootstrap", "exact", "none"]
    bootstrap_resamples: int | None = None
    minimum_informative_units: int | None = None
    retrieval_inputs_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _validate_spec_invariants(self) -> CampaignAnalysisSpecV1:
        _validate_non_zero_digest(self.spec_digest, "spec_digest")
        if (
            not self.unit_keys
            or len(self.unit_keys) != len(set(self.unit_keys))
            or any(not k for k in self.unit_keys)
        ):
            raise ValueError("unit_keys must be non-empty with unique non-empty string components")
        if self.method == AnalysisMethod.PAIRED_SIGN and (
            not self.pair_keys or any(not k for k in self.pair_keys)
        ):
            raise ValueError("PAIRED_SIGN analysis requires explicit non-empty pair_keys")
        if self.ci_method == "cluster_bootstrap":
            if not self.cluster_keys or any(not k for k in self.cluster_keys):
                raise ValueError("cluster_bootstrap requires explicit non-empty cluster_keys")
            if self.bootstrap_resamples is None or self.bootstrap_resamples <= 0:
                raise ValueError("cluster_bootstrap requires positive bootstrap_resamples")
        expected = compute_spec_digest(self)
        if self.spec_digest != expected:
            raise ValueError(
                f"spec_digest {self.spec_digest!r} does not match canonical {expected!r}"
            )
        return self


def create_campaign_analysis_spec(
    *,
    spec_id: str,
    method: AnalysisMethod,
    outcome_feature: str,
    unit: AnalysisUnit,
    unit_keys: tuple[str, ...],
    denominator_policy: DenominatorPolicy,
    ci_method: Literal["wilson", "cluster_bootstrap", "exact", "none"],
    predictor_features: tuple[str, ...] = (),
    group_by: tuple[str, ...] = (),
    pair_keys: tuple[str, ...] = (),
    cluster_keys: tuple[str, ...] = (),
    alpha: float = 0.05,
    bootstrap_resamples: int | None = None,
    minimum_informative_units: int | None = None,
) -> CampaignAnalysisSpecV1:
    body = {
        "schema_version": "campaign-analysis-spec/v1",
        "spec_id": spec_id,
        "method": method,
        "outcome_feature": outcome_feature,
        "predictor_features": predictor_features,
        "group_by": group_by,
        "unit": unit,
        "unit_keys": unit_keys,
        "pair_keys": pair_keys,
        "cluster_keys": cluster_keys,
        "denominator_policy": denominator_policy,
        "alpha": alpha,
        "ci_method": ci_method,
        "bootstrap_resamples": bootstrap_resamples,
        "minimum_informative_units": minimum_informative_units,
        "retrieval_inputs_allowed": False,
    }
    spec_digest = compute_spec_digest(body)
    return CampaignAnalysisSpecV1.model_validate({**body, "spec_digest": spec_digest})


class CampaignAnalysisResultV1(ContractModel):
    schema_version: Literal["campaign-analysis-result/v1"] = "campaign-analysis-result/v1"
    spec_id: str = Field(min_length=1)
    spec_digest: Digest
    snapshot_digest: Digest
    status: AnalysisStatus
    refusals: tuple[RefusalCode, ...] = ()
    observed_rows: int = Field(ge=0)
    analysis_units: int = Field(ge=0)
    informative_units: int | None = None
    effective_n: float | None = None
    estimate: float | None = None
    interval: tuple[float, float] | None = None
    p_value: float | None = None
    mde: float | None = None
    attainable_p_floor: float | None = None
    design_effect: float | None = None
    source_refs: tuple[ContextCitation, ...] = ()
    result_digest: Digest

    @model_validator(mode="after")
    def _validate_result_invariants(self) -> CampaignAnalysisResultV1:
        _validate_non_zero_digest(self.spec_digest, "spec_digest")
        _validate_non_zero_digest(self.snapshot_digest, "snapshot_digest")
        _validate_non_zero_digest(self.result_digest, "result_digest")
        if self.status == AnalysisStatus.VALID:
            if self.refusals:
                raise ValueError("VALID status must not have refusals")
        elif self.status == AnalysisStatus.REFUSAL:
            if not self.refusals:
                raise ValueError("REFUSAL status must contain at least one refusal code")
            if any(code != RefusalCode.UNDERPOWERED for code in self.refusals) and (
                self.estimate is not None or self.interval is not None or self.p_value is not None
            ):
                raise ValueError(
                    "Refusal that invalidates the estimand must set estimate, interval, and p_value to None"
                )
            elif RefusalCode.UNDERPOWERED in self.refusals and (
                self.interval is not None or self.p_value is not None
            ):
                raise ValueError(
                    "UNDERPOWERED inferential claims must set interval and p_value to None"
                )
        body = self.model_dump(mode="json", exclude={"result_digest"})
        expected = _canonical_digest(body)
        if self.result_digest != expected:
            raise ValueError(
                f"result_digest {self.result_digest!r} does not match canonical {expected!r}"
            )
        return self


def create_campaign_analysis_result(
    *,
    spec_id: str,
    spec_digest: str,
    snapshot_digest: str,
    status: AnalysisStatus,
    refusals: tuple[RefusalCode, ...] = (),
    observed_rows: int = 0,
    analysis_units: int = 0,
    informative_units: int | None = None,
    effective_n: float | None = None,
    estimate: float | None = None,
    interval: tuple[float, float] | None = None,
    p_value: float | None = None,
    mde: float | None = None,
    attainable_p_floor: float | None = None,
    design_effect: float | None = None,
    source_refs: tuple[ContextCitation, ...] = (),
) -> CampaignAnalysisResultV1:
    body: dict[str, Any] = {
        "schema_version": "campaign-analysis-result/v1",
        "spec_id": spec_id,
        "spec_digest": spec_digest,
        "snapshot_digest": snapshot_digest,
        "status": status,
        "refusals": refusals,
        "observed_rows": observed_rows,
        "analysis_units": analysis_units,
        "informative_units": informative_units,
        "effective_n": effective_n,
        "estimate": estimate,
        "interval": interval,
        "p_value": p_value,
        "mde": mde,
        "attainable_p_floor": attainable_p_floor,
        "design_effect": design_effect,
        "source_refs": [
            ref.model_dump(mode="json") if isinstance(ref, ContractModel) else ref
            for ref in source_refs
        ],
    }
    canonical = _canonical_digest(body)
    return CampaignAnalysisResultV1.model_validate({**body, "result_digest": canonical})


class RetrievalPolicyV1(ContractModel):
    schema_version: Literal["retrieval-policy/v1"] = "retrieval-policy/v1"
    enabled: bool = False
    purpose: Literal["review_queue", "counterexample"] = "review_queue"
    backend_kind: Literal["lexical", "semantic"] = "lexical"
    embedder_id: str = "hashing_embedder"
    embedder_version: str = "v1"
    embedder_digest: Digest
    tokenizer_id: str = "default_tokenizer"
    tokenizer_version: str = "v1"
    dimension: int = 128
    normalization: Literal["l2", "none"] = "none"
    distance_metric: Literal["cosine", "l2", "dot"] = "cosine"
    redaction_policy_digest: Digest
    k: int = 10


class ReviewQueueEntryV1(ContractModel):
    rank: int = Field(ge=1)
    job_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    source_cas_uri: str = Field(min_length=1)
    citation: ContextCitation
    window_start_step: int = Field(ge=0)
    window_end_step: int = Field(ge=0)
    window_digest: Digest
    distance: float = Field(ge=0.0)
    reason: Literal["similar_exemplar", "candidate_counterexample"]


def compute_review_queue_digest(artifact: Mapping[str, Any] | ReviewQueueArtifactV1) -> str:
    if isinstance(artifact, ReviewQueueArtifactV1):
        payload = artifact.model_dump(mode="json", exclude={"queue_digest"})
    else:
        raw = dict(artifact)
        raw.pop("queue_digest", None)
        policy_dict = (
            raw["policy"].model_dump(mode="json")
            if isinstance(raw["policy"], ContractModel)
            else dict(raw["policy"])
        )
        entries_list = [
            e.model_dump(mode="json") if isinstance(e, ContractModel) else dict(e)
            for e in raw.get("entries", ())
        ]
        refusals_list = [
            r.value if isinstance(r, StrEnum) else str(r) for r in raw.get("refusals", ())
        ]
        payload = {
            "schema_version": raw.get("schema_version", "review-queue/v1"),
            "queue_id": raw["queue_id"],
            "manifest_digest": raw["manifest_digest"],
            "snapshot_digest": raw["snapshot_digest"],
            "policy": policy_dict,
            "query_digest": raw["query_digest"],
            "candidate_pool_digest": raw["candidate_pool_digest"],
            "index_digest": raw["index_digest"],
            "coverage_complete": bool(raw.get("coverage_complete", False)),
            "entries": entries_list,
            "refusals": refusals_list,
            "decision_eligible": False,
        }
    return _canonical_digest(payload)


class ReviewQueueArtifactV1(ContractModel):
    schema_version: Literal["review-queue/v1"] = "review-queue/v1"
    queue_id: str = Field(min_length=1)
    queue_digest: Digest
    manifest_digest: Digest
    snapshot_digest: Digest
    policy: RetrievalPolicyV1
    query_digest: Digest
    candidate_pool_digest: Digest
    index_digest: Digest
    coverage_complete: bool
    entries: tuple[ReviewQueueEntryV1, ...] = ()
    refusals: tuple[RefusalCode, ...] = ()
    decision_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _validate_queue_digest(self) -> ReviewQueueArtifactV1:
        _validate_non_zero_digest(self.queue_digest, "queue_digest")
        _validate_non_zero_digest(self.manifest_digest, "manifest_digest")
        _validate_non_zero_digest(self.snapshot_digest, "snapshot_digest")
        _validate_non_zero_digest(self.query_digest, "query_digest")
        _validate_non_zero_digest(self.candidate_pool_digest, "candidate_pool_digest")
        _validate_non_zero_digest(self.index_digest, "index_digest")
        body = self.model_dump(mode="json", exclude={"queue_digest"})
        expected = _canonical_digest(body)
        if self.queue_digest != expected:
            raise ValueError(
                f"queue_digest {self.queue_digest!r} does not match canonical {expected!r}"
            )
        return self


class ReviewQueueRef(ContractModel):
    queue_id: str = Field(min_length=1)
    queue_digest: Digest
    queue_cas_uri: str = Field(min_length=1)
    decision_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _validate_ref_digest(self) -> ReviewQueueRef:
        _validate_non_zero_digest(self.queue_digest, "queue_digest")
        return self


class NextRunAction(StrEnum):
    REPAIR_CAPTURE_AUTHORITY = "repair_capture_authority"
    ADD_INDEPENDENT_SEEDS = "add_independent_seeds"
    ADD_CELL_REPEATS = "add_cell_repeats"
    BACKFILL_FEATURE_LINEAGE = "backfill_feature_lineage"
    HOLD_SEMANTIC_DECISION_ANALYSIS = "hold_semantic_decision_analysis"
    MANUAL_REVIEW = "manual_review"


class RunRecommendationV1(ContractModel):
    action: NextRunAction
    basis_result_digests: tuple[Digest, ...]
    target_estimand: str
    target_unit: AnalysisUnit
    requested_units: int | None = None
    blocking: bool = False
    reason_codes: tuple[str, ...] = ()


class NextRunFeedbackV1(ContractModel):
    schema_version: Literal["next-run-feedback/v1"] = "next-run-feedback/v1"
    source_report_digest: Digest
    source_snapshot_digest: Digest
    recommendations: tuple[RunRecommendationV1, ...]
    execution_authorized: Literal[False] = False
    authorizing_actor_required: Literal[True] = True
    feedback_digest: Digest

    @model_validator(mode="after")
    def _validate_feedback_digest(self) -> NextRunFeedbackV1:
        _validate_non_zero_digest(self.source_report_digest, "source_report_digest")
        _validate_non_zero_digest(self.source_snapshot_digest, "source_snapshot_digest")
        _validate_non_zero_digest(self.feedback_digest, "feedback_digest")
        body = self.model_dump(mode="json", exclude={"feedback_digest"})
        expected = _canonical_digest(body)
        if self.feedback_digest != expected:
            raise ValueError(
                f"feedback_digest {self.feedback_digest!r} does not match canonical {expected!r}"
            )
        return self


class CampaignAnalysisConfigV1(ContractModel):
    schema_version: Literal["campaign-analysis-config/v1"] = "campaign-analysis-config/v1"
    feature_registry_digest: Digest
    producer_digests: dict[str, Digest]
    cohort_policy_digest: Digest
    redaction_policy_digest: Digest
    specs: tuple[CampaignAnalysisSpecV1, ...] = ()
    retrieval: RetrievalPolicyV1 | None = None

    @model_validator(mode="after")
    def _validate_config_invariants(self) -> CampaignAnalysisConfigV1:
        _validate_non_zero_digest(self.feature_registry_digest, "feature_registry_digest")
        _validate_non_zero_digest(self.cohort_policy_digest, "cohort_policy_digest")
        _validate_non_zero_digest(self.redaction_policy_digest, "redaction_policy_digest")
        for k, v in self.producer_digests.items():
            _validate_non_zero_digest(v, f"producer_digests.{k}")
        if self.retrieval is not None:
            _validate_non_zero_digest(self.retrieval.embedder_digest, "retrieval.embedder_digest")
            _validate_non_zero_digest(
                self.retrieval.redaction_policy_digest, "retrieval.redaction_policy_digest"
            )
        spec_ids = [s.spec_id for s in self.specs]
        if len(spec_ids) != len(set(spec_ids)):
            raise ValueError("CampaignAnalysisConfigV1 spec_ids must be unique")
        spec_digests = [s.spec_digest for s in self.specs]
        if len(spec_digests) != len(set(spec_digests)):
            raise ValueError("CampaignAnalysisConfigV1 spec_digests must be unique")
        return self


# ---------------------------------------------------------------------------
# Pure Statistical Spec Runner & Adapters
# ---------------------------------------------------------------------------


def _to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "success", "yes"}:
            return True
        if lowered in {"0", "false", "failure", "no"}:
            return False
        return None
    return None


def _extract_source_refs(
    rows: Sequence[Mapping[str, Any]], target_outcome: str
) -> tuple[ContextCitation, ...]:
    refs: list[ContextCitation] = []
    seen: set[tuple[str, str | None]] = set()
    for row in rows:
        trial_id = str(row.get("trial_id") or "").strip()
        if not trial_id:
            continue
        path = f"{trial_id}/result.json"
        raw_digest = (
            row.get("task_digest")
            or row.get("verifier_digest")
            or row.get("ir_digest")
            or row.get("digest")
        )
        digest = _clean_source_digest(raw_digest)
        key = (path, digest)
        if key not in seen:
            seen.add(key)
            refs.append(
                ContextCitation(
                    path=path,
                    digest=digest,
                    supports=target_outcome,
                )
            )
    refs.sort(key=lambda c: (c.path, c.digest or ""))
    return tuple(refs)


def _refusal_result(
    spec: CampaignAnalysisSpecV1,
    snapshot_digest: str,
    code: RefusalCode,
    observed_rows: int,
    source_refs: tuple[ContextCitation, ...] = (),
) -> CampaignAnalysisResultV1:
    return create_campaign_analysis_result(
        spec_id=spec.spec_id,
        spec_digest=spec.spec_digest,
        snapshot_digest=snapshot_digest,
        status=AnalysisStatus.REFUSAL,
        refusals=(code,),
        observed_rows=observed_rows,
        analysis_units=0,
        informative_units=None,
        effective_n=None,
        estimate=None,
        interval=None,
        p_value=None,
        mde=None,
        attainable_p_floor=None,
        design_effect=None,
        source_refs=source_refs,
    )


def _rate_wilson(
    spec: CampaignAnalysisSpecV1,
    rows: Sequence[Mapping[str, Any]],
    snapshot_digest: str,
) -> CampaignAnalysisResultV1:
    from evallab.analysis_statistics import wilson_score_interval

    source_refs = _extract_source_refs(rows, spec.outcome_feature)
    for row in rows:
        if row.get("capture_complete") is False:
            return _refusal_result(
                spec, snapshot_digest, RefusalCode.CAPTURE_INCOMPLETE, len(rows), source_refs
            )

    if spec.denominator_policy == DenominatorPolicy.REQUIRED:
        denominators: list[Any] = []
        for row in rows:
            denom = row.get("denominator")
            if denom is None:
                return _refusal_result(
                    spec,
                    snapshot_digest,
                    RefusalCode.MISSING_DENOMINATOR_DECLARATION,
                    len(rows),
                    source_refs,
                )
            denominators.append(denom)
        if any(not isinstance(d, (int, float)) or d < 0 for d in denominators):
            return _refusal_result(
                spec,
                snapshot_digest,
                RefusalCode.INVALID_DENOMINATOR_DECLARATION,
                len(rows),
                source_refs,
            )
        total = sum(int(d) for d in denominators if d > 0)
        successes = 0
        for row, denom in zip(rows, denominators, strict=False):
            if denom > 0 and _to_bool(row.get(spec.outcome_feature)) is True:
                successes += 1
    else:
        values = [_to_bool(row.get(spec.outcome_feature)) for row in rows]
        total = sum(1 for v in values if v is not None)
        successes = sum(1 for v in values if v is True)

    if total <= 0:
        return _refusal_result(
            spec, snapshot_digest, RefusalCode.ZERO_OPPORTUNITY, len(rows), source_refs
        )

    confidence_level = 1.0 - spec.alpha
    wilson = wilson_score_interval(successes, total, confidence_level=confidence_level)
    interval = (
        (wilson.lower, wilson.upper)
        if wilson.lower is not None and wilson.upper is not None
        else None
    )
    return create_campaign_analysis_result(
        spec_id=spec.spec_id,
        spec_digest=spec.spec_digest,
        snapshot_digest=snapshot_digest,
        status=AnalysisStatus.VALID,
        refusals=(),
        observed_rows=len(rows),
        analysis_units=total,
        informative_units=total,
        effective_n=float(total),
        estimate=wilson.proportion,
        interval=interval,
        p_value=None,
        mde=None,
        attainable_p_floor=None,
        design_effect=None,
        source_refs=source_refs,
    )


def _paired_sign(
    spec: CampaignAnalysisSpecV1,
    rows: Sequence[Mapping[str, Any]],
    snapshot_digest: str,
) -> CampaignAnalysisResultV1:
    from evallab.analysis_statistics import PairedBinaryInput, exact_paired_binary_contrast

    source_refs = _extract_source_refs(rows, spec.outcome_feature)
    if not spec.pair_keys or any(not str(k).strip() for k in spec.pair_keys):
        return _refusal_result(
            spec, snapshot_digest, RefusalCode.ANALYSIS_UNIT_UNDECLARED, len(rows), source_refs
        )

    pair_cols = spec.pair_keys
    has_arm = any("arm" in row and row["arm"] is not None for row in rows)
    has_dose = any("dose" in row and row["dose"] is not None for row in rows)
    if not has_arm and not has_dose:
        return _refusal_result(
            spec, snapshot_digest, RefusalCode.MISSING_PAIR_ARM, len(rows), source_refs
        )

    pairs: dict[str, dict[str, list[float]]] = {}
    for row in rows:
        if row.get("capture_complete") is not True:
            return _refusal_result(
                spec, snapshot_digest, RefusalCode.CAPTURE_INCOMPLETE, len(rows), source_refs
            )
        auth = row.get("capture_authority")
        if auth not in ADMISSIBLE_CAPTURE_AUTHORITIES:
            return _refusal_result(
                spec,
                snapshot_digest,
                RefusalCode.CAPTURE_AUTHORITY_UNRESOLVED,
                len(rows),
                source_refs,
            )

        key_parts: list[str] = []
        for k in pair_cols:
            val = row.get(k)
            if val is None or str(val).strip() == "":
                return _refusal_result(
                    spec,
                    snapshot_digest,
                    RefusalCode.PAIRING_IDENTITY_MISMATCH,
                    len(rows),
                    source_refs,
                )
            key_parts.append(str(val).strip())
        pair_id = ":".join(key_parts)

        if has_arm and row.get("arm") is not None:
            arm_raw = str(row.get("arm")).strip().lower()
            if arm_raw in ("0", "control", "neutral", "neutral_padding"):
                arm = "control"
            elif arm_raw in ("1", "treatment", "distractor", "semantic", "semantic_distractor"):
                arm = "treatment"
            else:
                return _refusal_result(
                    spec, snapshot_digest, RefusalCode.MISSING_PAIR_ARM, len(rows), source_refs
                )
        else:
            dose = row.get("dose")
            if dose is None:
                return _refusal_result(
                    spec, snapshot_digest, RefusalCode.MISSING_PAIR_ARM, len(rows), source_refs
                )
            if dose in (0, "0", "control") or (
                isinstance(dose, str)
                and dose.strip().lower() in ("control", "neutral", "neutral_padding")
            ):
                arm = "control"
            else:
                arm = "treatment"

        out_raw = row.get(spec.outcome_feature)
        if out_raw is None:
            return _refusal_result(
                spec, snapshot_digest, RefusalCode.CAPTURE_INCOMPLETE, len(rows), source_refs
            )
        out_bool = _to_bool(out_raw)
        if out_bool is not None:
            out_val = 1.0 if out_bool else 0.0
        elif isinstance(out_raw, (int, float)):
            out_val = float(out_raw)
        else:
            return _refusal_result(
                spec, snapshot_digest, RefusalCode.CAPTURE_INCOMPLETE, len(rows), source_refs
            )
        pairs.setdefault(pair_id, {"control": [], "treatment": []})[arm].append(out_val)

    inputs: list[PairedBinaryInput] = []
    for pair_id, arms in sorted(pairs.items()):
        if not arms["control"] or not arms["treatment"]:
            return _refusal_result(
                spec, snapshot_digest, RefusalCode.MISSING_PAIR_ARM, len(rows), source_refs
            )
        if len(arms["control"]) != len(arms["treatment"]):
            return _refusal_result(
                spec, snapshot_digest, RefusalCode.PAIRING_IDENTITY_MISMATCH, len(rows), source_refs
            )
        control_mean = sum(arms["control"]) / len(arms["control"])
        treatment_mean = sum(arms["treatment"]) / len(arms["treatment"])
        if control_mean > treatment_mean:
            arm_a_out, arm_b_out = True, False
        elif control_mean < treatment_mean:
            arm_a_out, arm_b_out = False, True
        else:
            arm_a_out, arm_b_out = False, False
        inputs.append(
            PairedBinaryInput(
                assignment_unit_id=pair_id,
                arm_a_outcome=arm_a_out,
                arm_b_outcome=arm_b_out,
                capture_complete=True,
            )
        )

    if not inputs:
        return _refusal_result(
            spec, snapshot_digest, RefusalCode.ZERO_OPPORTUNITY, len(rows), source_refs
        )

    result = exact_paired_binary_contrast(inputs, arm_a_id="control", arm_b_id="treatment")
    status = AnalysisStatus.VALID
    refusals: tuple[RefusalCode, ...] = ()
    if result.status == AnalysisStatus.REFUSAL:
        status = AnalysisStatus.REFUSAL
        refusals = (
            (result.refusal_code,)
            if result.refusal_code
            else (RefusalCode.UNSUPPORTED_ANALYSIS_METHOD,)
        )
        return create_campaign_analysis_result(
            spec_id=spec.spec_id,
            spec_digest=spec.spec_digest,
            snapshot_digest=snapshot_digest,
            status=status,
            refusals=refusals,
            observed_rows=len(rows),
            analysis_units=result.n_pairs,
            informative_units=result.n_discordant,
            effective_n=float(result.n_pairs),
            estimate=None,
            interval=None,
            p_value=None,
            mde=None,
            attainable_p_floor=None,
            design_effect=None,
            source_refs=source_refs,
        )

    if result.design_floor_limited or (
        spec.minimum_informative_units is not None
        and result.n_discordant < spec.minimum_informative_units
    ):
        status = AnalysisStatus.REFUSAL
        refusals = (RefusalCode.UNDERPOWERED,)
        return create_campaign_analysis_result(
            spec_id=spec.spec_id,
            spec_digest=spec.spec_digest,
            snapshot_digest=snapshot_digest,
            status=status,
            refusals=refusals,
            observed_rows=len(rows),
            analysis_units=result.n_pairs,
            informative_units=result.n_discordant,
            effective_n=float(result.n_pairs),
            estimate=result.risk_difference,
            interval=None,
            p_value=None,
            mde=None,
            attainable_p_floor=result.min_attainable_p_value,
            design_effect=None,
            source_refs=source_refs,
        )

    return create_campaign_analysis_result(
        spec_id=spec.spec_id,
        spec_digest=spec.spec_digest,
        snapshot_digest=snapshot_digest,
        status=status,
        refusals=refusals,
        observed_rows=len(rows),
        analysis_units=result.n_pairs,
        informative_units=result.n_discordant,
        effective_n=float(result.n_pairs),
        estimate=result.risk_difference,
        interval=(result.risk_difference_interval_lower, result.risk_difference_interval_upper)
        if result.risk_difference_interval_lower is not None
        and result.risk_difference_interval_upper is not None
        else None,
        p_value=result.exact_p_value,
        mde=None,
        attainable_p_floor=result.min_attainable_p_value,
        design_effect=None,
        source_refs=source_refs,
    )


def _fisher_2x2(
    spec: CampaignAnalysisSpecV1,
    rows: Sequence[Mapping[str, Any]],
    snapshot_digest: str,
) -> CampaignAnalysisResultV1:
    from evallab.analysis_statistics import fisher_exact_2x2

    source_refs = _extract_source_refs(rows, spec.outcome_feature)
    if not spec.predictor_features:
        return _refusal_result(
            spec, snapshot_digest, RefusalCode.UNSUPPORTED_ANALYSIS_METHOD, len(rows), source_refs
        )

    for row in rows:
        if row.get("capture_complete") is not True:
            return _refusal_result(
                spec, snapshot_digest, RefusalCode.CAPTURE_INCOMPLETE, len(rows), source_refs
            )
        auth = row.get("capture_authority")
        if auth not in ADMISSIBLE_CAPTURE_AUTHORITIES:
            return _refusal_result(
                spec,
                snapshot_digest,
                RefusalCode.CAPTURE_AUTHORITY_UNRESOLVED,
                len(rows),
                source_refs,
            )

    pred = spec.predictor_features[0]
    a = b = c = d = 0
    for row in rows:
        pred_val = _to_bool(row.get(pred))
        out = _to_bool(row.get(spec.outcome_feature))
        if pred_val is None or out is None:
            return _refusal_result(
                spec, snapshot_digest, RefusalCode.CAPTURE_INCOMPLETE, len(rows), source_refs
            )
        if not pred_val and out:
            a += 1
        elif not pred_val and not out:
            b += 1
        elif pred_val and out:
            c += 1
        else:
            d += 1

    total = a + b + c + d
    if total <= 0:
        return _refusal_result(
            spec, snapshot_digest, RefusalCode.ZERO_OPPORTUNITY, len(rows), source_refs
        )

    result = fisher_exact_2x2([[a, b], [c, d]])
    status = result.status
    refusals: tuple[RefusalCode, ...] = ()
    estimate = result.odds_ratio
    p_value = result.exact_p_value
    if status == AnalysisStatus.REFUSAL:
        refusals = (
            (result.refusal_code,)
            if result.refusal_code
            else (RefusalCode.UNSUPPORTED_ANALYSIS_METHOD,)
        )
        estimate = None
        p_value = None

    return create_campaign_analysis_result(
        spec_id=spec.spec_id,
        spec_digest=spec.spec_digest,
        snapshot_digest=snapshot_digest,
        status=status,
        refusals=refusals,
        observed_rows=len(rows),
        analysis_units=total,
        informative_units=total,
        effective_n=float(total),
        estimate=estimate,
        interval=None,
        p_value=p_value,
        mde=None,
        attainable_p_floor=None,
        design_effect=None,
        source_refs=source_refs,
    )


def _repeat_heterogeneity(
    spec: CampaignAnalysisSpecV1,
    rows: Sequence[Mapping[str, Any]],
    snapshot_digest: str,
) -> CampaignAnalysisResultV1:
    from evallab.analysis_statistics import RepeatCellInput, analyze_repeat_heterogeneity

    source_refs = _extract_source_refs(rows, spec.outcome_feature)
    cluster_cols = spec.cluster_keys or spec.unit_keys
    if not cluster_cols or any(not str(k).strip() for k in cluster_cols):
        return _refusal_result(
            spec, snapshot_digest, RefusalCode.ANALYSIS_UNIT_UNDECLARED, len(rows), source_refs
        )

    for row in rows:
        if row.get("capture_complete") is False:
            return _refusal_result(
                spec, snapshot_digest, RefusalCode.CAPTURE_INCOMPLETE, len(rows), source_refs
            )

    by_cell: dict[str, dict[str, int]] = {}
    for row in rows:
        key_parts: list[str] = []
        for k in cluster_cols:
            val = row.get(k)
            if val is None or str(val).strip() == "":
                return _refusal_result(
                    spec,
                    snapshot_digest,
                    RefusalCode.PAIRING_IDENTITY_MISMATCH,
                    len(rows),
                    source_refs,
                )
            key_parts.append(str(val).strip())
        cell_id = ":".join(key_parts)
        if cell_id not in by_cell:
            by_cell[cell_id] = {"successes": 0, "repeats": 0}
        out = _to_bool(row.get(spec.outcome_feature))
        if out is True:
            by_cell[cell_id]["successes"] += 1
        if out is not None:
            by_cell[cell_id]["repeats"] += 1

    valid_cells = [
        RepeatCellInput(
            cell_id=cell_id,
            successes=counts["successes"],
            repeats=counts["repeats"],
            capture_complete=True,
        )
        for cell_id, counts in sorted(by_cell.items())
        if counts["repeats"] > 0
    ]
    if not valid_cells:
        return _refusal_result(
            spec, snapshot_digest, RefusalCode.ZERO_OPPORTUNITY, len(rows), source_refs
        )

    report = analyze_repeat_heterogeneity(valid_cells)
    status = report.status
    refusals: tuple[RefusalCode, ...] = ()
    estimate = report.pooled_probability
    p_value = report.dispersion_p_value
    if status == AnalysisStatus.REFUSAL:
        refusals = (
            (report.refusal_code,)
            if report.refusal_code
            else (RefusalCode.UNSUPPORTED_ANALYSIS_METHOD,)
        )
        estimate = None
        p_value = None
    elif (
        spec.minimum_informative_units is not None
        and report.n_cells < spec.minimum_informative_units
    ):
        status = AnalysisStatus.REFUSAL
        refusals = (RefusalCode.UNDERPOWERED,)
        p_value = None

    return create_campaign_analysis_result(
        spec_id=spec.spec_id,
        spec_digest=spec.spec_digest,
        snapshot_digest=snapshot_digest,
        status=status,
        refusals=refusals,
        observed_rows=len(rows),
        analysis_units=report.n_cells,
        informative_units=report.n_cells,
        effective_n=report.effective_n,
        estimate=estimate,
        interval=None,
        p_value=p_value,
        mde=None,
        attainable_p_floor=None,
        design_effect=report.design_effect,
        source_refs=source_refs,
    )


def _order_distance(
    spec: CampaignAnalysisSpecV1,
    rows: Sequence[Mapping[str, Any]],
    snapshot_digest: str,
) -> CampaignAnalysisResultV1:
    from evallab.analysis_statistics import compute_sequence_fidelity

    source_refs = _extract_source_refs(rows, spec.outcome_feature)
    if len(spec.predictor_features) >= 2:
        a_col, b_col = spec.predictor_features[0], spec.predictor_features[1]
    elif spec.predictor_features:
        a_col = spec.outcome_feature
        b_col = spec.predictor_features[0]
    else:
        return _refusal_result(
            spec, snapshot_digest, RefusalCode.UNSUPPORTED_ANALYSIS_METHOD, len(rows), source_refs
        )

    for row in rows:
        if row.get("capture_complete") is False:
            return _refusal_result(
                spec, snapshot_digest, RefusalCode.CAPTURE_INCOMPLETE, len(rows), source_refs
            )

    def _sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
        return tuple(row.get(k) for k in spec.unit_keys)

    sorted_rows = sorted(rows, key=_sort_key)
    seq_a = [r.get(a_col) for r in sorted_rows]
    seq_b = [r.get(b_col) for r in sorted_rows]
    if not seq_a or not seq_b:
        return _refusal_result(
            spec, snapshot_digest, RefusalCode.ZERO_OPPORTUNITY, len(rows), source_refs
        )

    result = compute_sequence_fidelity(seq_a, seq_b)
    status = result.status
    refusals: tuple[RefusalCode, ...] = ()
    estimate = result.normalized_footrule_distance
    if estimate is None:
        estimate = result.jaccard_similarity
    if status == AnalysisStatus.REFUSAL:
        refusals = (
            (result.refusal_code,)
            if result.refusal_code
            else (RefusalCode.UNSUPPORTED_ANALYSIS_METHOD,)
        )
        estimate = None

    return create_campaign_analysis_result(
        spec_id=spec.spec_id,
        spec_digest=spec.spec_digest,
        snapshot_digest=snapshot_digest,
        status=status,
        refusals=refusals,
        observed_rows=len(rows),
        analysis_units=len(seq_a),
        informative_units=None,
        effective_n=None,
        estimate=estimate,
        interval=None,
        p_value=None,
        mde=None,
        attainable_p_floor=None,
        design_effect=None,
        source_refs=source_refs,
    )


def _descriptive_counts(
    spec: CampaignAnalysisSpecV1,
    rows: Sequence[Mapping[str, Any]],
    snapshot_digest: str,
) -> CampaignAnalysisResultV1:
    source_refs = _extract_source_refs(rows, spec.outcome_feature)
    for row in rows:
        if row.get("capture_complete") is False:
            return _refusal_result(
                spec, snapshot_digest, RefusalCode.CAPTURE_INCOMPLETE, len(rows), source_refs
            )

    if not spec.group_by:
        values = [_to_bool(row.get(spec.outcome_feature)) for row in rows]
        total = sum(1 for v in values if v is not None)
        successes = sum(1 for v in values if v is True)
        if total <= 0:
            return _refusal_result(
                spec, snapshot_digest, RefusalCode.ZERO_OPPORTUNITY, len(rows), source_refs
            )
        estimate = successes / total
        return create_campaign_analysis_result(
            spec_id=spec.spec_id,
            spec_digest=spec.spec_digest,
            snapshot_digest=snapshot_digest,
            status=AnalysisStatus.VALID,
            refusals=(),
            observed_rows=len(rows),
            analysis_units=total,
            informative_units=total,
            effective_n=float(total),
            estimate=estimate,
            interval=None,
            p_value=None,
            mde=None,
            attainable_p_floor=None,
            design_effect=None,
            source_refs=source_refs,
        )

    by_group: dict[tuple[Any, ...], dict[str, int]] = {}
    for row in rows:
        gkey = tuple(row.get(k) for k in spec.group_by)
        if gkey not in by_group:
            by_group[gkey] = {"n": 0, "successes": 0}
        out = _to_bool(row.get(spec.outcome_feature))
        if out is not None:
            by_group[gkey]["n"] += 1
            if out:
                by_group[gkey]["successes"] += 1

    total = sum(v["n"] for v in by_group.values())
    successes = sum(v["successes"] for v in by_group.values())
    if total <= 0:
        return _refusal_result(
            spec, snapshot_digest, RefusalCode.ZERO_OPPORTUNITY, len(rows), source_refs
        )
    estimate = successes / total
    return create_campaign_analysis_result(
        spec_id=spec.spec_id,
        spec_digest=spec.spec_digest,
        snapshot_digest=snapshot_digest,
        status=AnalysisStatus.VALID,
        refusals=(),
        observed_rows=len(rows),
        analysis_units=len(by_group),
        informative_units=len(by_group),
        effective_n=float(total),
        estimate=estimate,
        interval=None,
        p_value=None,
        mde=None,
        attainable_p_floor=None,
        design_effect=None,
        source_refs=source_refs,
    )


def _validate_outcome_and_predictors(
    spec: CampaignAnalysisSpecV1,
    feature_registry: Any,
    snapshot_digest: str,
    observed_rows: int,
    source_refs: tuple[ContextCitation, ...] = (),
) -> CampaignAnalysisResultV1 | None:
    if feature_registry is None:
        return None

    def get(name: str) -> Any:
        if hasattr(feature_registry, "get"):
            return feature_registry.get(name)
        return None

    from evallab.interpretation.feature_registry import feature_analysis_eligibility

    outcome = get(spec.outcome_feature)
    if outcome is None:
        return _refusal_result(
            spec,
            snapshot_digest,
            RefusalCode.MISSING_LINEAGE_DECLARATION,
            observed_rows,
            source_refs,
        )
    if not feature_analysis_eligibility(outcome).outcome_allowed:
        return _refusal_result(
            spec,
            snapshot_digest,
            RefusalCode.MISSING_LINEAGE_DECLARATION,
            observed_rows,
            source_refs,
        )

    for pred in spec.predictor_features:
        if pred == spec.outcome_feature:
            return _refusal_result(
                spec,
                snapshot_digest,
                RefusalCode.OUTCOME_LINEAGE_VIOLATION,
                observed_rows,
                source_refs,
            )
        feat = get(pred)
        if feat is None:
            return _refusal_result(
                spec,
                snapshot_digest,
                RefusalCode.MISSING_LINEAGE_DECLARATION,
                observed_rows,
                source_refs,
            )
        if feat.available_before_verdict is None:
            return _refusal_result(
                spec,
                snapshot_digest,
                RefusalCode.MISSING_LINEAGE_DECLARATION,
                observed_rows,
                source_refs,
            )
        if not feat.available_before_verdict:
            return _refusal_result(
                spec,
                snapshot_digest,
                RefusalCode.OUTCOME_LINEAGE_VIOLATION,
                observed_rows,
                source_refs,
            )
        audit = feature_analysis_eligibility(feat).predictor_refusal
        if audit:
            if audit in (
                "POST_VERDICT_TEMPORAL_VIOLATION",
                "REWARD_DEFINITION_LEAKAGE",
                "INVALID_VERDICT_COUPLING",
                "VERDICT_CORRELATED",
            ):
                return _refusal_result(
                    spec,
                    snapshot_digest,
                    RefusalCode.OUTCOME_LINEAGE_VIOLATION,
                    observed_rows,
                    source_refs,
                )
            if audit in (
                "MISSING_TEMPORAL_AVAILABILITY",
                "UNDECLARED_VERDICT_COUPLING",
                "MISSING_COUPLING_EVIDENCE_BASIS",
            ):
                return _refusal_result(
                    spec,
                    snapshot_digest,
                    RefusalCode.MISSING_LINEAGE_DECLARATION,
                    observed_rows,
                    source_refs,
                )
            if audit == "NOT_APPLICABLE_FOR_PREDICTION":
                return _refusal_result(
                    spec,
                    snapshot_digest,
                    RefusalCode.ANALYSIS_UNIT_UNDECLARED,
                    observed_rows,
                    source_refs,
                )
            return _refusal_result(
                spec,
                snapshot_digest,
                RefusalCode.UNSUPPORTED_ANALYSIS_METHOD,
                observed_rows,
                source_refs,
            )
    return None


def run_campaign_analysis(
    spec: CampaignAnalysisSpecV1,
    rows: Sequence[Mapping[str, Any]],
    feature_registry: Any = None,
    *,
    snapshot_digest: str,
) -> CampaignAnalysisResultV1:
    source_refs = _extract_source_refs(rows, spec.outcome_feature)
    refusal = _validate_outcome_and_predictors(
        spec, feature_registry, snapshot_digest, len(rows), source_refs
    )
    if refusal is not None:
        return refusal

    if spec.method == AnalysisMethod.RATE_WILSON:
        return _rate_wilson(spec, rows, snapshot_digest)
    if spec.method == AnalysisMethod.PAIRED_SIGN:
        return _paired_sign(spec, rows, snapshot_digest)
    if spec.method == AnalysisMethod.FISHER_2X2:
        return _fisher_2x2(spec, rows, snapshot_digest)
    if spec.method in (AnalysisMethod.DISPERSION_ICC, AnalysisMethod.DESIGN_EFFECT):
        return _repeat_heterogeneity(spec, rows, snapshot_digest)
    if spec.method == AnalysisMethod.ORDER_DISTANCE:
        return _order_distance(spec, rows, snapshot_digest)
    if spec.method == AnalysisMethod.DESCRIPTIVE_COUNTS:
        return _descriptive_counts(spec, rows, snapshot_digest)
    return _refusal_result(
        spec, snapshot_digest, RefusalCode.UNSUPPORTED_ANALYSIS_METHOD, len(rows), source_refs
    )


__all__ = [
    "ADMISSIBLE_CAPTURE_AUTHORITIES",
    "AnalysisMethod",
    "AnalysisStatus",
    "AnalysisUnit",
    "Basis",
    "CIDisposition",
    "CampaignAnalysisConfigV1",
    "CampaignAnalysisResultV1",
    "CampaignAnalysisSpecV1",
    "CascadeStatus",
    "CascadeTrialInput",
    "CascadeTrialResult",
    "ContextCitation",
    "DenominatorPolicy",
    "EmpiricalDiagnostics",
    "FeatureContractRow",
    "FeatureGateResult",
    "FeatureObservation",
    "NextRunAction",
    "NextRunFeedbackV1",
    "RecoveryAnalysisResult",
    "RecoveryOpportunity",
    "RefusalCode",
    "RetrievalPolicyV1",
    "ReviewQueueArtifactV1",
    "ReviewQueueEntryV1",
    "ReviewQueueRef",
    "RunRecommendationV1",
    "StratumDiagnostics",
    "T11Report",
    "T13Report",
    "Verdict",
    "analyze_cascade_distance",
    "analyze_conditional_recovery",
    "compute_review_queue_digest",
    "compute_spec_digest",
    "create_campaign_analysis_result",
    "create_campaign_analysis_spec",
    "evaluate_process_outcome_gate",
    "run_campaign_analysis",
]
