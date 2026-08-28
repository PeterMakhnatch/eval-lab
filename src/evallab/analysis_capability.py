"""Deterministic T1 analysis consumers over frozen analysis-ready contracts.

This module is deliberately read-only. It accepts typed rows produced by the data
and feature-projection lanes, emits versioned analysis results, and never mutates
feature registries, projection tables, campaign manifests, approvals, or schedules.
"""

from __future__ import annotations

import hashlib
import json
import random
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from evallab.cohort import BOOTSTRAP_RESAMPLES
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


__all__ = [
    "AnalysisStatus",
    "Basis",
    "CIDisposition",
    "CascadeStatus",
    "CascadeTrialInput",
    "CascadeTrialResult",
    "DenominatorPolicy",
    "EmpiricalDiagnostics",
    "FeatureContractRow",
    "FeatureGateResult",
    "FeatureObservation",
    "RecoveryAnalysisResult",
    "RecoveryOpportunity",
    "RefusalCode",
    "StratumDiagnostics",
    "T11Report",
    "T13Report",
    "Verdict",
    "analyze_cascade_distance",
    "analyze_conditional_recovery",
    "evaluate_process_outcome_gate",
]
