"""Pure paired-intervention outcome analysis over typed fixture evidence."""

from __future__ import annotations

import math
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from evallab.analysis_statistics import (
    PairedBinaryContrastResult,
    PairedBinaryInput,
    exact_paired_binary_contrast,
)
from evallab.benchmark_program_contracts import canonical_json, compute_sha256
from evallab.paired_intervention import Arm, PairedInterventionPlan, ScheduledArm
from evallab.schemas import ContractModel, Digest, TrialAdmissibilityV1

_ZERO_DIGEST = "sha256:" + "0" * 64
_SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class PairedOutcomeRefusal(ValueError):
    """A stable structural refusal from paired outcome analysis."""

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: {message}")


class PairExclusionCode(StrEnum):
    """Closed reasons why a structurally valid pair is not in the denominator."""

    ARM_NOT_ADMISSIBLE = "arm_not_admissible"
    NOT_CAUSAL_ELIGIBLE = "not_causal_eligible"
    NOT_CAUSAL_ALLOWED_USE = "not_causal_allowed_use"
    CAPTURE_INCOMPLETE = "capture_incomplete"
    NOT_EVALUATOR_BACKED = "not_evaluator_backed"
    MISSING_EVALUATOR_DIGEST = "missing_evaluator_digest"
    ENVIRONMENT_MISMATCH = "environment_mismatch"
    RUNTIME_MISMATCH = "runtime_mismatch"


class MeasurementBasis(StrEnum):
    EVALUATOR_BACKED = "evaluator_backed"
    REWARD_ONLY = "reward_only"
    SELF_REPORTED = "self_reported"


class OutcomeStatus(StrEnum):
    SUPPORTED = "supported"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"
    UNAVAILABLE = "unavailable"


class _FrozenContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PairedOutcomeDecisionRuleV1(_FrozenContract):
    """Predeclared claim threshold for one named scalar metric."""

    schema_version: Literal["paired-outcome-decision-rule/v1"] = "paired-outcome-decision-rule/v1"
    metric_name: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,79}$")
    direction: Literal["higher", "lower"]
    minimum_effect: float = Field(gt=0.0)
    minimum_eligible_pairs: int = Field(ge=1)
    binary_claims_require_exact_contrast: Literal[True] = True
    claim_scope: Literal["priority_only_never_general"] = "priority_only_never_general"

    @field_validator("minimum_effect")
    @classmethod
    def effect_is_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("minimum_effect must be finite")
        return value


class PairedTrialObservationV1(_FrozenContract):
    """One trial result bound to an exact scheduled arm and evidence authority."""

    schema_version: Literal["paired-trial-observation/v1"] = "paired-trial-observation/v1"
    plan_digest: Digest
    spec_digest: Digest
    intervention_delta_digest: Digest
    schedule_ordinal: int = Field(ge=1)
    pair_ordinal: int = Field(ge=1)
    pair_id: str = Field(pattern=_SAFE_ID_PATTERN)
    block_id: str = Field(pattern=_SAFE_ID_PATTERN)
    assignment_unit_id: str = Field(pattern=_SAFE_ID_PATTERN)
    arm: Arm
    randomization_source: Literal["plan"] = "plan"
    randomization_seed: int
    carryover_status: Literal["isolated", "violation", "unknown"]
    trial_id: str = Field(pattern=_SAFE_ID_PATTERN)
    task_ref: str = Field(min_length=1)
    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    task_version: str = Field(min_length=1)
    task_instance_id: str = Field(pattern=_SAFE_ID_PATTERN)
    generator_seed: int | str
    task_cluster_id: str = Field(pattern=_SAFE_ID_PATTERN)
    environment_name: str = Field(min_length=1)
    environment_identity_digest: Digest
    runtime_identity_digest: Digest
    outcome_artifact_digest: Digest
    trial_admissibility_digest: Digest
    trial_admissibility_decision: Literal["admissible", "rejected", "unavailable"]
    analysis_eligibility: Literal["causal-eligible", "calibration-only"]
    allowed_use: Literal["causal", "descriptive-only"]
    admissibility: TrialAdmissibilityV1
    capture_status: Literal["complete", "missing", "corrupt"]
    measurement_basis: MeasurementBasis
    metric_name: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,79}$")
    metric_value: float
    measurement_uncertainty: float | None = Field(default=None, ge=0.0)
    uncertainty_basis: Literal["standard_error", "interval_half_width", "not_available"] = (
        "not_available"
    )

    @field_validator(
        "plan_digest",
        "spec_digest",
        "intervention_delta_digest",
        "environment_identity_digest",
        "runtime_identity_digest",
        "outcome_artifact_digest",
        "trial_admissibility_digest",
    )
    @classmethod
    def digests_are_non_zero(cls, value: str) -> str:
        if value == _ZERO_DIGEST:
            raise ValueError("identity digests cannot be all-zero")
        return value

    @field_validator("metric_value")
    @classmethod
    def metric_is_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("metric_value must be finite")
        return value

    @model_validator(mode="after")
    def authority_fields_match(self) -> PairedTrialObservationV1:
        if self.measurement_uncertainty is None:
            if self.uncertainty_basis != "not_available":
                raise ValueError("missing uncertainty requires not_available basis")
        elif not math.isfinite(self.measurement_uncertainty):
            raise ValueError("measurement_uncertainty must be finite")
        elif self.uncertainty_basis == "not_available":
            raise ValueError("declared uncertainty requires a measurement basis")

        authority = TrialAdmissibilityV1.model_validate(self.admissibility.model_dump(mode="json"))
        if authority.trial_id != self.trial_id:
            raise ValueError("trial identity does not match admissibility authority")
        parity = (
            self.trial_admissibility_digest,
            self.trial_admissibility_decision,
            self.analysis_eligibility,
            self.allowed_use,
        )
        expected_parity = (
            authority.admissibility_digest,
            authority.decision,
            authority.analysis_eligibility,
            authority.allowed_use,
        )
        if parity != expected_parity:
            raise ValueError("trial admissibility fields do not match authority")
        if authority.source_digests.outcome != self.outcome_artifact_digest:
            raise ValueError("outcome artifact digest does not match trial authority")
        if (
            authority.network_isolation_evidence_digest is None
            or self.environment_identity_digest != authority.network_isolation_evidence_digest
        ):
            raise ValueError("environment identity digest does not match isolation authority")
        runtime = authority.task_runtime_identity
        if runtime is None:
            raise ValueError("observation requires exact task runtime identity")
        if (runtime.task_id, runtime.task_version) != (self.task_id, self.task_version):
            raise ValueError("task identity does not match runtime authority")
        expected_runtime_digest = _canonical_digest(runtime.model_dump(mode="json"))
        if self.runtime_identity_digest != expected_runtime_digest:
            raise ValueError("runtime identity digest does not match runtime authority")
        return self


class PairedPairOutcomeV1(_FrozenContract):
    """One complete pair, retained whether eligible or excluded."""

    schema_version: Literal["paired-pair-outcome/v1"] = "paired-pair-outcome/v1"
    pair_ordinal: int = Field(ge=1)
    pair_id: str = Field(pattern=_SAFE_ID_PATTERN)
    block_id: str = Field(pattern=_SAFE_ID_PATTERN)
    assignment_unit_id: str = Field(pattern=_SAFE_ID_PATTERN)
    control_trial_id: str = Field(pattern=_SAFE_ID_PATTERN)
    treatment_trial_id: str = Field(pattern=_SAFE_ID_PATTERN)
    control_outcome_digest: Digest
    treatment_outcome_digest: Digest
    control_metric_value: float
    treatment_metric_value: float
    paired_difference: float
    direction_adjusted_difference: float
    eligible: bool
    exclusion_reasons: tuple[PairExclusionCode, ...]
    pair_digest: Digest

    @model_validator(mode="after")
    def values_and_digest_match(self) -> PairedPairOutcomeV1:
        if not all(
            math.isfinite(value)
            for value in (
                self.control_metric_value,
                self.treatment_metric_value,
                self.paired_difference,
                self.direction_adjusted_difference,
            )
        ):
            raise ValueError("pair metric values must be finite")
        if not math.isclose(
            self.paired_difference,
            self.treatment_metric_value - self.control_metric_value,
            rel_tol=0.0,
            abs_tol=0.0,
        ):
            raise ValueError("paired_difference does not match arm values")
        if self.eligible == bool(self.exclusion_reasons):
            raise ValueError("pair eligibility and exclusion reasons disagree")
        if tuple(sorted(set(self.exclusion_reasons), key=str)) != self.exclusion_reasons:
            raise ValueError("pair exclusion reasons must be unique and canonical")
        expected = _canonical_digest(self.model_dump(mode="json", exclude={"pair_digest"}))
        if self.pair_digest != expected:
            raise ValueError("pair_digest does not match pair content")
        return self


class PairedInterventionOutcomeV1(_FrozenContract):
    """Self-validating paired outcome artifact with bounded claim scope."""

    schema_version: Literal["paired-intervention-outcome/v1"] = "paired-intervention-outcome/v1"
    artifact_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    plan: PairedInterventionPlan
    decision_rule: PairedOutcomeDecisionRuleV1
    observations: tuple[PairedTrialObservationV1, ...]
    pairs: tuple[PairedPairOutcomeV1, ...]
    planned_pair_count: int = Field(ge=1)
    denominator_eligible_pairs: int = Field(ge=0)
    excluded_pair_count: int = Field(ge=0)
    numerator_directional_improvement_pairs: int = Field(ge=0)
    directional_regression_pairs: int = Field(ge=0)
    tied_pairs: int = Field(ge=0)
    mean_paired_difference: float | None
    mean_direction_adjusted_difference: float | None
    exact_binary_contrast: PairedBinaryContrastResult | None
    status: OutcomeStatus
    claim_scope: Literal["priority_only_never_general"] = "priority_only_never_general"
    outcome_digest: Digest

    @model_validator(mode="after")
    def semantic_rehydration_matches(self) -> PairedInterventionOutcomeV1:
        expected_digest = _canonical_digest(
            self.model_dump(mode="json", exclude={"outcome_digest"})
        )
        if self.outcome_digest != expected_digest:
            raise ValueError("outcome_digest does not match artifact content")
        ordinals = tuple(observation.schedule_ordinal for observation in self.observations)
        if ordinals != tuple(sorted(ordinals)):
            raise ValueError("observations must use canonical schedule ordering")
        derived = _derive_analysis_payload(
            plan=self.plan,
            observations=self.observations,
            decision_rule=self.decision_rule,
        )
        for field_name, expected in derived.items():
            if getattr(self, field_name) != expected:
                raise ValueError(
                    f"derived outcome field {field_name!r} does not match observations"
                )
        return self


def _canonical_digest(value: object) -> str:
    return f"sha256:{compute_sha256(canonical_json(value))}"


def _refuse(reason_code: str, message: str) -> None:
    raise PairedOutcomeRefusal(reason_code, message)


def _delta_digest(plan: PairedInterventionPlan) -> str:
    return _canonical_digest(plan.delta.model_dump(mode="json"))


def _force_validate_inputs(
    plan: PairedInterventionPlan,
    observations: Sequence[PairedTrialObservationV1],
    decision_rule: PairedOutcomeDecisionRuleV1,
) -> tuple[
    PairedInterventionPlan,
    tuple[PairedTrialObservationV1, ...],
    PairedOutcomeDecisionRuleV1,
]:
    validated_plan = PairedInterventionPlan.model_validate(plan.model_dump(mode="json"))
    validated_observations = tuple(
        PairedTrialObservationV1.model_validate(observation.model_dump(mode="json"))
        for observation in observations
    )
    validated_rule = PairedOutcomeDecisionRuleV1.model_validate(
        decision_rule.model_dump(mode="json")
    )
    return validated_plan, validated_observations, validated_rule


def _validate_observation_mapping(
    observation: PairedTrialObservationV1,
    scheduled: ScheduledArm,
    *,
    plan: PairedInterventionPlan,
    delta_digest: str,
) -> None:
    if observation.plan_digest != plan.plan_digest:
        _refuse("plan_digest_mismatch", "observation names a different intervention plan")
    if observation.spec_digest != scheduled.spec_digest:
        _refuse("spec_digest_substitution", "observation names a different scheduled spec")
    if observation.intervention_delta_digest != delta_digest:
        _refuse(
            "intervention_delta_mismatch",
            "observation does not bind the plan intervention delta",
        )
    expected_pair_ordinal = (scheduled.ordinal + 1) // 2
    expected_identity = (
        scheduled.ordinal,
        expected_pair_ordinal,
        scheduled.pair_id,
        scheduled.block_id,
        scheduled.assignment_unit_id,
        scheduled.arm,
        plan.randomization_seed,
    )
    actual_identity = (
        observation.schedule_ordinal,
        observation.pair_ordinal,
        observation.pair_id,
        observation.block_id,
        observation.assignment_unit_id,
        observation.arm,
        observation.randomization_seed,
    )
    if actual_identity != expected_identity:
        _refuse("assignment_mismatch", "observation assignment differs from the plan")
    if observation.carryover_status != "isolated":
        _refuse(
            "carryover_violation",
            f"scheduled arm {scheduled.ordinal} lacks an isolated carryover boundary",
        )
    if observation.task_cluster_id != scheduled.block_id:
        _refuse(
            "cross_cluster_leakage",
            "observation task cluster differs from the declared plan block",
        )
    spec = scheduled.spec
    expected_task_identity = (
        spec.task,
        spec.task_id,
        spec.task_version,
        spec.task_instance_id,
        spec.generator_seed,
        spec.environment,
    )
    actual_task_identity = (
        observation.task_ref,
        observation.task_id,
        observation.task_version,
        observation.task_instance_id,
        observation.generator_seed,
        observation.environment_name,
    )
    if None in expected_task_identity or actual_task_identity != expected_task_identity:
        _refuse(
            "task_identity_mismatch",
            "observation task identity differs from the exact scheduled task",
        )
    runtime = observation.admissibility.task_runtime_identity
    assert runtime is not None
    if (
        spec.task_package_digest is None
        or runtime.certified_runtime_package_digest != spec.task_package_digest
    ):
        _refuse(
            "runtime_identity_substitution",
            "trial runtime package differs from the scheduled package identity",
        )
    if spec.task_runtime_identity is not None and runtime != spec.task_runtime_identity:
        _refuse(
            "runtime_identity_substitution",
            "trial runtime authority differs from the scheduled runtime identity",
        )


def _map_observations(
    *,
    plan: PairedInterventionPlan,
    observations: Sequence[PairedTrialObservationV1],
    decision_rule: PairedOutcomeDecisionRuleV1,
) -> dict[tuple[str, Arm], PairedTrialObservationV1]:
    if decision_rule.metric_name != plan.analysis_gate.analysis_spec.outcome_feature:
        _refuse(
            "metric_contract_mismatch",
            "decision metric differs from the plan analysis outcome feature",
        )
    if decision_rule.minimum_eligible_pairs < plan.analysis_gate.minimum_complete_pairs:
        _refuse(
            "analysis_gate_weakened",
            "decision rule requires fewer pairs than the plan analysis gate",
        )
    scheduled_by_key = {
        (scheduled.pair_id, scheduled.arm): scheduled for scheduled in plan.schedule
    }
    plan_pair_ids = {scheduled.pair_id for scheduled in plan.schedule}
    mapped: dict[tuple[str, Arm], PairedTrialObservationV1] = {}
    trial_ids: set[str] = set()
    outcome_digests: set[str] = set()
    delta_digest = _delta_digest(plan)
    for observation in observations:
        if observation.metric_name != decision_rule.metric_name:
            _refuse(
                "metric_contract_mismatch",
                "observation metric differs from the predeclared decision metric",
            )
        key = (observation.pair_id, observation.arm)
        scheduled = scheduled_by_key.get(key)
        if scheduled is None:
            reason = "extra_pair" if observation.pair_id not in plan_pair_ids else "unknown_arm"
            _refuse(reason, "observation does not map to a declared scheduled arm")
        if key in mapped:
            _refuse("duplicate_arm", "multiple observations map to one scheduled arm")
        if observation.trial_id in trial_ids:
            _refuse("duplicate_trial", "trial identity is reused across scheduled arms")
        if observation.outcome_artifact_digest in outcome_digests:
            _refuse("duplicate_outcome", "outcome artifact is reused across scheduled arms")
        _validate_observation_mapping(
            observation,
            scheduled,
            plan=plan,
            delta_digest=delta_digest,
        )
        mapped[key] = observation
        trial_ids.add(observation.trial_id)
        outcome_digests.add(observation.outcome_artifact_digest)
    missing = set(scheduled_by_key) - set(mapped)
    if missing:
        _refuse(
            "missing_pair_arm",
            f"observations omit {len(missing)} scheduled arm(s)",
        )
    return mapped


def _pair_exclusions(
    control: PairedTrialObservationV1,
    treatment: PairedTrialObservationV1,
) -> tuple[PairExclusionCode, ...]:
    reasons: set[PairExclusionCode] = set()
    for observation in (control, treatment):
        if observation.trial_admissibility_decision != "admissible":
            reasons.add(PairExclusionCode.ARM_NOT_ADMISSIBLE)
        if observation.analysis_eligibility != "causal-eligible":
            reasons.add(PairExclusionCode.NOT_CAUSAL_ELIGIBLE)
        if observation.allowed_use != "causal":
            reasons.add(PairExclusionCode.NOT_CAUSAL_ALLOWED_USE)
        if observation.capture_status != "complete":
            reasons.add(PairExclusionCode.CAPTURE_INCOMPLETE)
        if observation.measurement_basis != MeasurementBasis.EVALUATOR_BACKED:
            reasons.add(PairExclusionCode.NOT_EVALUATOR_BACKED)
        if observation.admissibility.source_digests.verifier is None:
            reasons.add(PairExclusionCode.MISSING_EVALUATOR_DIGEST)
    if control.environment_identity_digest != treatment.environment_identity_digest:
        reasons.add(PairExclusionCode.ENVIRONMENT_MISMATCH)
    if control.runtime_identity_digest != treatment.runtime_identity_digest:
        reasons.add(PairExclusionCode.RUNTIME_MISMATCH)
    return tuple(sorted(reasons, key=str))


def _build_pair_outcome(
    *,
    pair_ordinal: int,
    control: PairedTrialObservationV1,
    treatment: PairedTrialObservationV1,
    decision_rule: PairedOutcomeDecisionRuleV1,
) -> PairedPairOutcomeV1:
    paired_difference = treatment.metric_value - control.metric_value
    if not math.isfinite(paired_difference):
        _refuse("nonfinite_pair_difference", "paired metric difference is not finite")
    adjusted = paired_difference if decision_rule.direction == "higher" else -paired_difference
    reasons = _pair_exclusions(control, treatment)
    body: dict[str, Any] = {
        "schema_version": "paired-pair-outcome/v1",
        "pair_ordinal": pair_ordinal,
        "pair_id": control.pair_id,
        "block_id": control.block_id,
        "assignment_unit_id": control.assignment_unit_id,
        "control_trial_id": control.trial_id,
        "treatment_trial_id": treatment.trial_id,
        "control_outcome_digest": control.outcome_artifact_digest,
        "treatment_outcome_digest": treatment.outcome_artifact_digest,
        "control_metric_value": control.metric_value,
        "treatment_metric_value": treatment.metric_value,
        "paired_difference": paired_difference,
        "direction_adjusted_difference": adjusted,
        "eligible": not reasons,
        "exclusion_reasons": reasons,
    }
    return PairedPairOutcomeV1.model_validate({**body, "pair_digest": _canonical_digest(body)})


def _outcome_status(
    *,
    decision_rule: PairedOutcomeDecisionRuleV1,
    plan: PairedInterventionPlan,
    denominator: int,
    contrast: PairedBinaryContrastResult | None,
) -> OutcomeStatus:
    if denominator == 0:
        return OutcomeStatus.UNAVAILABLE
    if denominator < decision_rule.minimum_eligible_pairs:
        return OutcomeStatus.INCONCLUSIVE
    if contrast is None:
        return OutcomeStatus.INCONCLUSIVE
    risk_difference = contrast.risk_difference
    lower = contrast.risk_difference_interval_lower
    upper = contrast.risk_difference_interval_upper
    exact_p = contrast.exact_p_value
    if risk_difference is None or lower is None or upper is None or exact_p is None:
        return OutcomeStatus.INCONCLUSIVE
    if exact_p > plan.analysis_gate.analysis_spec.alpha:
        return OutcomeStatus.INCONCLUSIVE
    if decision_rule.direction == "lower":
        risk_difference, lower, upper = -risk_difference, -upper, -lower
    if lower >= decision_rule.minimum_effect:
        return OutcomeStatus.SUPPORTED
    if upper <= -decision_rule.minimum_effect:
        return OutcomeStatus.REFUTED
    return OutcomeStatus.INCONCLUSIVE


def _derive_analysis_payload(
    *,
    plan: PairedInterventionPlan,
    observations: Sequence[PairedTrialObservationV1],
    decision_rule: PairedOutcomeDecisionRuleV1,
) -> dict[str, object]:
    mapped = _map_observations(
        plan=plan,
        observations=observations,
        decision_rule=decision_rule,
    )
    pair_ids = [plan.schedule[index].pair_id for index in range(0, len(plan.schedule), 2)]
    pairs: list[PairedPairOutcomeV1] = []
    for pair_ordinal, pair_id in enumerate(pair_ids, start=1):
        pairs.append(
            _build_pair_outcome(
                pair_ordinal=pair_ordinal,
                control=mapped[(pair_id, "control")],
                treatment=mapped[(pair_id, "treatment")],
                decision_rule=decision_rule,
            )
        )
    eligible = [pair for pair in pairs if pair.eligible]
    denominator = len(eligible)
    improvements = sum(pair.direction_adjusted_difference > 0.0 for pair in eligible)
    regressions = sum(pair.direction_adjusted_difference < 0.0 for pair in eligible)
    ties = denominator - improvements - regressions
    if eligible:
        mean_difference = math.fsum(pair.paired_difference for pair in eligible) / denominator
        mean_adjusted = (
            math.fsum(pair.direction_adjusted_difference for pair in eligible) / denominator
        )
    else:
        mean_difference = None
        mean_adjusted = None

    binary = bool(eligible) and all(
        pair.control_metric_value in (0.0, 1.0) and pair.treatment_metric_value in (0.0, 1.0)
        for pair in eligible
    )
    contrast = None
    if binary:
        contrast = exact_paired_binary_contrast(
            (
                PairedBinaryInput(
                    assignment_unit_id=pair.assignment_unit_id,
                    arm_a_outcome=int(pair.treatment_metric_value),
                    arm_b_outcome=int(pair.control_metric_value),
                    capture_complete=True,
                )
                for pair in eligible
            ),
            confidence_level=1.0 - plan.analysis_gate.analysis_spec.alpha,
        )
    status = _outcome_status(
        decision_rule=decision_rule,
        plan=plan,
        denominator=denominator,
        contrast=contrast,
    )
    return {
        "pairs": tuple(pairs),
        "planned_pair_count": len(pairs),
        "denominator_eligible_pairs": denominator,
        "excluded_pair_count": len(pairs) - denominator,
        "numerator_directional_improvement_pairs": improvements,
        "directional_regression_pairs": regressions,
        "tied_pairs": ties,
        "mean_paired_difference": mean_difference,
        "mean_direction_adjusted_difference": mean_adjusted,
        "exact_binary_contrast": contrast,
        "status": status,
        "claim_scope": "priority_only_never_general",
    }


def analyze_paired_outcomes(
    *,
    artifact_id: str,
    plan: PairedInterventionPlan,
    observations: Sequence[PairedTrialObservationV1],
    decision_rule: PairedOutcomeDecisionRuleV1,
) -> PairedInterventionOutcomeV1:
    """Analyze fixture outcomes without I/O, dispatch, registration, or model calls."""

    validated_plan, validated_observations, validated_rule = _force_validate_inputs(
        plan, observations, decision_rule
    )
    canonical_observations = tuple(
        sorted(validated_observations, key=lambda observation: observation.schedule_ordinal)
    )
    derived = _derive_analysis_payload(
        plan=validated_plan,
        observations=canonical_observations,
        decision_rule=validated_rule,
    )
    body: dict[str, object] = {
        "schema_version": "paired-intervention-outcome/v1",
        "artifact_id": artifact_id,
        "plan": validated_plan.model_dump(mode="json"),
        "decision_rule": validated_rule.model_dump(mode="json"),
        "observations": [
            observation.model_dump(mode="json") for observation in canonical_observations
        ],
        **{
            key: value.model_dump(mode="json")
            if isinstance(value, ContractModel)
            else [item.model_dump(mode="json") for item in value]
            if isinstance(value, tuple) and value and isinstance(value[0], ContractModel)
            else value
            for key, value in derived.items()
        },
    }
    return PairedInterventionOutcomeV1.model_validate(
        {**body, "outcome_digest": _canonical_digest(body)}
    )
