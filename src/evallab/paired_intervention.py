"""Deterministic, approval-preserving plans for paired prompt interventions."""

from __future__ import annotations

import hashlib
import os
import stat
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from evallab.analysis_capability import (
    AnalysisMethod,
    AnalysisUnit,
    CampaignAnalysisSpecV1,
    DenominatorPolicy,
)
from evallab.benchmark_program_contracts import canonical_json, compute_sha256
from evallab.campaigns import experiment_spec_digest
from evallab.queue import PolicyGate
from evallab.schemas import ContractModel, Digest, ExperimentSpec, PolicyDecision

Arm = Literal["control", "treatment"]
_ZERO_DIGEST = "sha256:" + "0" * 64
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_PLANNING_OWNED_FIELDS = frozenset(
    {
        "name",
        "hypothesis",
        "spec_id",
        "submitted_at",
        "elicitation",
        "extra_instruction_path",
        "extra_instruction_sha256",
        "grid_id",
        "grid_point",
    }
)
_CAMPAIGN_PROVENANCE_FIELDS = (
    "campaign_ledger",
    "campaign_cell_id",
    "campaign_attempt_id",
    "campaign_attempt_index",
    "campaign_manifest_digest",
    "campaign_spec_digest",
    "campaign_evidence_store",
)


class PairedInterventionRefusal(ValueError):
    """A stable, machine-readable refusal from paired plan construction."""

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: {message}")


class _FrozenContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CaptureExpectation(_FrozenContract):
    """Symmetric evidence that every arm must capture before analysis."""

    required_artifacts: tuple[str, ...] = Field(min_length=1)
    atif_trajectory_required: Literal[True] = True
    verifier_result_required: Literal[True] = True
    environment_integrity_required: Literal[True] = True
    capture_loss_disposition: Literal["hold_complete_pair"] = "hold_complete_pair"

    @field_validator("required_artifacts", mode="before")
    @classmethod
    def canonicalize_artifacts(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("required_artifacts must be a list or tuple")
        artifacts = tuple(str(item) for item in value)
        if any(not item for item in artifacts):
            raise ValueError("required_artifacts cannot contain empty identities")
        if len(artifacts) != len(set(artifacts)):
            raise ValueError("required_artifacts must be unique")
        return tuple(sorted(artifacts))


class RetryReplacementPolicy(_FrozenContract):
    """Fail-closed replacement rules that never change assignment or arm."""

    eligible_failures: tuple[Literal["capture_loss", "harness_failure"], ...] = (
        "capture_loss",
        "harness_failure",
    )
    max_replacements_per_arm: int = Field(default=1, ge=0)
    replacement_reuses_pair_identity: Literal[True] = True
    replacement_reuses_assignment_unit: Literal[True] = True
    replacement_reuses_arm: Literal[True] = True
    replacement_requires_new_approval: Literal[True] = True
    failed_attempt_analysis_disposition: Literal["exclude_complete_pair"] = "exclude_complete_pair"


class ExtraInstructionDelta(_FrozenContract):
    """The sole executable difference between control and treatment."""

    variable: Literal["extra_instruction"] = "extra_instruction"
    control_path: None = None
    control_sha256: None = None
    treatment_path: str = Field(min_length=1)
    treatment_sha256: Digest

    @field_validator("treatment_path")
    @classmethod
    def treatment_path_is_canonical_and_relative(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or path.as_posix() != value
            or any(component in {"", ".", ".."} for component in path.parts)
        ):
            raise ValueError("treatment_path must be a canonical repo-relative path")
        return value

    @model_validator(mode="after")
    def digest_is_non_zero(self) -> ExtraInstructionDelta:
        if self.treatment_sha256 == _ZERO_DIGEST:
            raise ValueError("treatment_sha256 cannot be an all-zero digest")
        return self


class PairedAnalysisGate(_FrozenContract):
    """Analysis prerequisites for complete, class-stratified pairs only."""

    analysis_spec: CampaignAnalysisSpecV1
    complete_pairs_required: Literal[True] = True
    capture_loss_disposition: Literal["hold"] = "hold"
    stratification_keys: tuple[str, ...] = ("block_id",)
    pooled_headline_allowed: Literal[False] = False
    minimum_complete_pairs: int = Field(ge=1)

    @model_validator(mode="after")
    def analysis_is_paired_and_fail_closed(self) -> PairedAnalysisGate:
        spec = self.analysis_spec
        if spec.method != AnalysisMethod.PAIRED_SIGN:
            raise ValueError("analysis_spec must use paired_sign")
        if spec.unit != AnalysisUnit.PAIRED_SEED:
            raise ValueError("analysis_spec must use paired_seed units")
        if "pair_id" not in spec.pair_keys:
            raise ValueError("analysis_spec pair_keys must bind pair_id")
        if "assignment_unit_id" not in spec.unit_keys or "arm" not in spec.unit_keys:
            raise ValueError("analysis_spec unit_keys must bind assignment_unit_id and arm")
        if "arm" not in spec.predictor_features:
            raise ValueError("analysis_spec predictor_features must include arm")
        if spec.denominator_policy != DenominatorPolicy.REQUIRED:
            raise ValueError("analysis_spec denominator_policy must be required")
        if spec.ci_method != "exact":
            raise ValueError("paired_sign analysis must use an exact interval")
        if "block_id" not in spec.group_by or "block_id" not in self.stratification_keys:
            raise ValueError("analysis must preserve block-specific results")
        if spec.minimum_informative_units != self.minimum_complete_pairs:
            raise ValueError("analysis minimum_informative_units must equal minimum_complete_pairs")
        return self


class PairedArmCandidate(_FrozenContract):
    """One unsubmitted arm bound to a pair, block, and assignment unit."""

    pair_id: str = Field(pattern=_ID_PATTERN)
    block_id: str = Field(pattern=_ID_PATTERN)
    assignment_unit_id: str = Field(pattern=_ID_PATTERN)
    arm: Arm
    spec: ExperimentSpec
    capture: CaptureExpectation


class ScheduledArm(_FrozenContract):
    """One deterministic schedule entry consumable by DirectoryQueue.submit."""

    ordinal: int = Field(ge=1)
    pair_id: str = Field(pattern=_ID_PATTERN)
    block_id: str = Field(pattern=_ID_PATTERN)
    assignment_unit_id: str = Field(pattern=_ID_PATTERN)
    arm: Arm
    spec_digest: Digest
    spec: ExperimentSpec
    policy_decision: PolicyDecision


class PairedInterventionPlan(_FrozenContract):
    """Content-addressed paired schedule; construction performs no submission."""

    schema_version: Literal["paired-intervention-plan/v1"] = "paired-intervention-plan/v1"
    plan_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    plan_digest: Digest
    randomization_seed: int
    delta: ExtraInstructionDelta
    capture_expectation: CaptureExpectation
    retry_policy: RetryReplacementPolicy
    analysis_gate: PairedAnalysisGate
    schedule: tuple[ScheduledArm, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def digest_and_schedule_are_coherent(self) -> PairedInterventionPlan:
        expected = _canonical_digest(self.model_dump(mode="json", exclude={"plan_digest"}))
        if self.plan_digest != expected:
            raise ValueError(
                f"plan_digest {self.plan_digest!r} does not match canonical {expected!r}"
            )
        ordinals = tuple(entry.ordinal for entry in self.schedule)
        if ordinals != tuple(range(1, len(self.schedule) + 1)):
            raise ValueError("schedule ordinals must be contiguous and one-based")
        _validate_rehydrated_plan(self)
        return self

    @property
    def specs(self) -> tuple[ExperimentSpec, ...]:
        """Return the exact unsubmitted specs accepted by the existing queue API."""

        return tuple(entry.spec for entry in self.schedule)


def _canonical_digest(value: object) -> str:
    return f"sha256:{compute_sha256(canonical_json(value))}"


def _stable_order_key(seed: int, *components: str) -> str:
    joined = "\0".join((str(seed), *components)).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()


def _refuse(reason_code: str, message: str) -> None:
    raise PairedInterventionRefusal(reason_code, message)


def _read_repo_regular_file(repo_root: Path, relative_path: str) -> bytes:
    """Read repo-confined bytes without following any symlink component."""

    path = PurePosixPath(relative_path)
    components = path.parts
    if (
        path.is_absolute()
        or path.as_posix() != relative_path
        or not components
        or any(component in {"", ".", ".."} for component in components)
    ):
        _refuse(
            "invalid_intervention_path",
            "treatment path must be canonical and repo-relative",
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        directory = os.open(repo_root.resolve(), flags)
    except OSError as exc:
        _refuse("invalid_repo_root", f"cannot open repository root: {exc}")
    try:
        for component in components[:-1]:
            try:
                next_directory = os.open(component, flags, dir_fd=directory)
            except OSError as exc:
                _refuse(
                    "invalid_intervention_path",
                    f"cannot traverse treatment path {relative_path!r}: {exc}",
                )
            os.close(directory)
            directory = next_directory
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(components[-1], file_flags, dir_fd=directory)
        except OSError as exc:
            _refuse(
                "invalid_intervention_path",
                f"cannot open treatment path {relative_path!r}: {exc}",
            )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                _refuse(
                    "invalid_intervention_path",
                    f"treatment path {relative_path!r} must be a regular file",
                )
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 64 * 1024):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _require_planning_state(candidate: PairedArmCandidate) -> None:
    spec = candidate.spec
    if spec.purpose != "elicitation":
        _refuse("invalid_purpose", "paired interventions require purpose=elicitation")
    if spec.elicitation is None or spec.prereg is None:
        _refuse(
            "missing_experiment_contract",
            "each arm requires elicitation and preregistration contracts",
        )
    if not spec.billable:
        _refuse(
            "approval_gate_not_preserved",
            "paired intervention arms must use a billable agent requiring human approval",
        )
    if spec.spec_id is not None or spec.submitted_at is not None:
        _refuse("already_submitted_spec", "planner accepts only unsubmitted specs")
    if spec.policy_rule is not None:
        _refuse(
            "preassigned_policy_rule",
            "planner does not accept a preassigned standing approval rule",
        )
    if spec.grid_id is not None or spec.grid_point is not None:
        _refuse("preassigned_schedule_identity", "planner owns grid identity metadata")
    if any(getattr(spec, field) is not None for field in _CAMPAIGN_PROVENANCE_FIELDS):
        _refuse(
            "preassigned_campaign_provenance",
            "planner accepts no registered campaign attempt provenance",
        )
    if spec.task_instance_id != candidate.assignment_unit_id:
        _refuse(
            "assignment_identity_mismatch",
            "assignment_unit_id must equal the spec task_instance_id",
        )
    if spec.attempts != 1 or spec.concurrency != 1:
        _refuse(
            "duplicate_assignment",
            "each paired arm must represent exactly one attempt at concurrency one",
        )


def _execution_invariants(spec: ExperimentSpec) -> dict[str, Any]:
    return spec.model_dump(mode="json", exclude=_PLANNING_OWNED_FIELDS)


def _validate_pair(
    control: PairedArmCandidate,
    treatment: PairedArmCandidate,
    *,
    delta: ExtraInstructionDelta,
) -> None:
    if control.block_id != treatment.block_id:
        _refuse("block_identity_mismatch", f"pair {control.pair_id!r} spans blocks")
    if control.assignment_unit_id != treatment.assignment_unit_id:
        _refuse(
            "assignment_identity_mismatch",
            f"pair {control.pair_id!r} spans assignment units",
        )
    if control.capture != treatment.capture:
        _refuse(
            "capture_asymmetry",
            f"pair {control.pair_id!r} declares different capture expectations",
        )
    control_spec = control.spec
    treatment_spec = treatment.spec
    assert control_spec.elicitation is not None
    assert treatment_spec.elicitation is not None
    changed_elicitation_fields = control_spec.elicitation.diff_fields(treatment_spec.elicitation)
    if changed_elicitation_fields != ["preamble_hash"]:
        _refuse(
            "invalid_intervention_delta",
            "control and treatment must differ in exactly elicitation.preamble_hash",
        )
    if (
        control_spec.elicitation.preamble_hash is not None
        or treatment_spec.elicitation.preamble_hash != delta.treatment_sha256
        or control_spec.extra_instruction_path is not None
        or control_spec.extra_instruction_sha256 is not None
        or treatment_spec.extra_instruction_path != delta.treatment_path
        or treatment_spec.extra_instruction_sha256 != delta.treatment_sha256
    ):
        _refuse(
            "unbound_intervention_payload",
            "arm elicitation and extra-instruction path/digest do not match the declared delta",
        )
    control_fixed = _execution_invariants(control_spec)
    treatment_fixed = _execution_invariants(treatment_spec)
    if control_fixed != treatment_fixed:
        changed = sorted(
            key
            for key in control_fixed.keys() | treatment_fixed.keys()
            if control_fixed.get(key) != treatment_fixed.get(key)
        )
        _refuse(
            "simultaneous_variable_change",
            f"pair {control.pair_id!r} changes execution fields {changed!r}",
        )


def _group_complete_pairs(
    candidates: Sequence[PairedArmCandidate],
) -> dict[str, tuple[PairedArmCandidate, PairedArmCandidate]]:
    if not candidates:
        _refuse("empty_plan", "at least one complete pair is required")
    grouped: dict[str, list[PairedArmCandidate]] = defaultdict(list)
    assignment_to_pair: dict[str, str] = {}
    names: set[str] = set()
    for candidate in candidates:
        _require_planning_state(candidate)
        prior_pair = assignment_to_pair.setdefault(candidate.assignment_unit_id, candidate.pair_id)
        if prior_pair != candidate.pair_id:
            _refuse(
                "duplicate_assignment",
                f"assignment unit {candidate.assignment_unit_id!r} appears in multiple pairs",
            )
        if candidate.spec.name in names:
            _refuse("duplicate_assignment", f"spec name {candidate.spec.name!r} is duplicated")
        names.add(candidate.spec.name)
        grouped[candidate.pair_id].append(candidate)

    complete: dict[str, tuple[PairedArmCandidate, PairedArmCandidate]] = {}
    for pair_id, arms in grouped.items():
        by_arm = {candidate.arm: candidate for candidate in arms}
        if len(arms) != 2 or set(by_arm) != {"control", "treatment"}:
            _refuse(
                "missing_twin",
                f"pair {pair_id!r} must contain exactly one control and one treatment",
            )
        complete[pair_id] = (by_arm["control"], by_arm["treatment"])
    return complete


def _interleaved_pair_order(
    pairs: Mapping[str, tuple[PairedArmCandidate, PairedArmCandidate]],
    *,
    seed: int,
) -> list[tuple[PairedArmCandidate, PairedArmCandidate]]:
    by_block: dict[str, list[tuple[PairedArmCandidate, PairedArmCandidate]]] = defaultdict(list)
    for pair in pairs.values():
        by_block[pair[0].block_id].append(pair)
    block_order = sorted(by_block, key=lambda block: _stable_order_key(seed, "block", block))
    queues: dict[str, deque[tuple[PairedArmCandidate, PairedArmCandidate]]] = {}
    for block in block_order:
        ordered = sorted(
            by_block[block],
            key=lambda pair: _stable_order_key(seed, "pair", block, pair[0].pair_id),
        )
        queues[block] = deque(ordered)
    interleaved: list[tuple[PairedArmCandidate, PairedArmCandidate]] = []
    while any(queues.values()):
        for block in block_order:
            if queues[block]:
                interleaved.append(queues[block].popleft())
    return interleaved


def _require_scheduled_state(
    entry: ScheduledArm,
    *,
    plan: PairedInterventionPlan,
) -> PairedArmCandidate:
    spec = entry.spec
    planning_spec = spec.model_copy(update={"grid_id": None, "grid_point": None})
    candidate = PairedArmCandidate(
        pair_id=entry.pair_id,
        block_id=entry.block_id,
        assignment_unit_id=entry.assignment_unit_id,
        arm=entry.arm,
        spec=planning_spec,
        capture=plan.capture_expectation,
    )
    _require_planning_state(candidate)
    expected_grid_point = {
        "pair_id": entry.pair_id,
        "block_id": entry.block_id,
        "assignment_unit_id": entry.assignment_unit_id,
        "arm": entry.arm,
        "intervention_variable": plan.delta.variable,
        "preamble_sha256": spec.extra_instruction_sha256,
    }
    if spec.grid_id != plan.plan_id or spec.grid_point != expected_grid_point:
        _refuse(
            "schedule_metadata_mismatch",
            f"spec {spec.name!r} grid metadata does not match its schedule entry",
        )
    expected_spec_digest = experiment_spec_digest(spec)
    if entry.spec_digest != expected_spec_digest:
        _refuse(
            "spec_digest_mismatch",
            f"spec {spec.name!r} does not match its declared execution digest",
        )
    if (
        entry.policy_decision.admitted
        or entry.policy_decision.reason_code != "paid_run_unauthorized"
    ):
        _refuse(
            "approval_gate_not_preserved",
            f"spec {spec.name!r} does not carry the required paid-run refusal",
        )
    return candidate


def _validate_rehydrated_plan(plan: PairedInterventionPlan) -> None:
    grouped: dict[str, list[PairedArmCandidate]] = defaultdict(list)
    assignment_to_pair: dict[str, str] = {}
    names: set[str] = set()
    for entry in plan.schedule:
        candidate = _require_scheduled_state(entry, plan=plan)
        prior_pair = assignment_to_pair.setdefault(entry.assignment_unit_id, entry.pair_id)
        if prior_pair != entry.pair_id:
            _refuse(
                "duplicate_assignment",
                f"assignment unit {entry.assignment_unit_id!r} appears in multiple pairs",
            )
        if entry.spec.name in names:
            _refuse(
                "duplicate_assignment",
                f"spec name {entry.spec.name!r} is duplicated",
            )
        names.add(entry.spec.name)
        grouped[entry.pair_id].append(candidate)

    complete: dict[str, tuple[PairedArmCandidate, PairedArmCandidate]] = {}
    for pair_id, candidates in grouped.items():
        by_arm = {candidate.arm: candidate for candidate in candidates}
        if len(candidates) != 2 or set(by_arm) != {"control", "treatment"}:
            _refuse(
                "missing_twin",
                f"pair {pair_id!r} must contain exactly one control and one treatment",
            )
        control = by_arm["control"]
        treatment = by_arm["treatment"]
        _validate_pair(control, treatment, delta=plan.delta)
        complete[pair_id] = (control, treatment)

    if len(complete) < plan.analysis_gate.minimum_complete_pairs:
        _refuse(
            "insufficient_complete_pairs",
            f"plan has {len(complete)} complete pairs; analysis requires "
            f"{plan.analysis_gate.minimum_complete_pairs}",
        )
    if len(plan.schedule) % 2:
        _refuse("missing_twin", "interleaved schedule must contain adjacent complete pairs")
    actual_pair_order: list[str] = []
    for pair_index in range(0, len(plan.schedule), 2):
        first, second = plan.schedule[pair_index : pair_index + 2]
        if first.pair_id != second.pair_id:
            _refuse(
                "nonadjacent_twins",
                "each control/treatment twin must be adjacent in the schedule",
            )
        expected_arms: tuple[Arm, Arm] = (
            ("control", "treatment") if pair_index // 2 % 2 == 0 else ("treatment", "control")
        )
        if (first.arm, second.arm) != expected_arms:
            _refuse(
                "arm_balance_mismatch",
                "schedule first-arm assignment is not deterministically counterbalanced",
            )
        actual_pair_order.append(first.pair_id)
    expected_pair_order = [
        pair[0].pair_id for pair in _interleaved_pair_order(complete, seed=plan.randomization_seed)
    ]
    if actual_pair_order != expected_pair_order:
        _refuse(
            "schedule_order_mismatch",
            "schedule does not match deterministic block-interleaved pair order",
        )


def _scheduled_spec(
    candidate: PairedArmCandidate,
    *,
    plan_id: str,
    delta: ExtraInstructionDelta,
) -> ExperimentSpec:
    payload = candidate.spec.model_dump(mode="json")
    payload["grid_id"] = plan_id
    payload["grid_point"] = {
        "pair_id": candidate.pair_id,
        "block_id": candidate.block_id,
        "assignment_unit_id": candidate.assignment_unit_id,
        "arm": candidate.arm,
        "intervention_variable": delta.variable,
        "preamble_sha256": candidate.spec.extra_instruction_sha256,
    }
    return ExperimentSpec.model_validate(payload)


def plan_paired_intervention(
    *,
    plan_id: str,
    randomization_seed: int,
    candidates: Sequence[PairedArmCandidate],
    delta: ExtraInstructionDelta,
    retry_policy: RetryReplacementPolicy,
    analysis_gate: PairedAnalysisGate,
    policy_gate: PolicyGate,
    repo_root: Path,
    spent_today_usd: float,
) -> PairedInterventionPlan:
    """Build an offline paired schedule while preserving per-spec human approval."""

    intervention_bytes = _read_repo_regular_file(repo_root, delta.treatment_path)
    actual_digest = f"sha256:{hashlib.sha256(intervention_bytes).hexdigest()}"
    if actual_digest != delta.treatment_sha256:
        _refuse(
            "intervention_digest_mismatch",
            f"treatment path {delta.treatment_path!r} does not match its declared digest",
        )

    pairs = _group_complete_pairs(candidates)
    if len(pairs) < analysis_gate.minimum_complete_pairs:
        _refuse(
            "insufficient_complete_pairs",
            f"plan has {len(pairs)} complete pairs; analysis requires "
            f"{analysis_gate.minimum_complete_pairs}",
        )
    captures: set[str] = set()
    for control, treatment in pairs.values():
        _validate_pair(control, treatment, delta=delta)
        captures.add(_canonical_digest(control.capture.model_dump(mode="json")))
    if len(captures) != 1:
        _refuse(
            "capture_asymmetry",
            "all pairs must use the same capture expectation",
        )

    ordered_pairs = _interleaved_pair_order(pairs, seed=randomization_seed)
    scheduled: list[ScheduledArm] = []
    for pair_index, pair in enumerate(ordered_pairs):
        control, treatment = pair
        arm_order = (control, treatment) if pair_index % 2 == 0 else (treatment, control)
        for candidate in arm_order:
            spec = _scheduled_spec(candidate, plan_id=plan_id, delta=delta)
            decision = policy_gate.decide(spec, spent_today_usd=spent_today_usd)
            if decision.admitted or decision.reason_code != "paid_run_unauthorized":
                _refuse(
                    "approval_gate_not_preserved",
                    f"spec {spec.name!r} must reach the existing paid_run_unauthorized gate; "
                    f"received {decision.reason_code!r}",
                )
            scheduled.append(
                ScheduledArm(
                    ordinal=len(scheduled) + 1,
                    pair_id=candidate.pair_id,
                    block_id=candidate.block_id,
                    assignment_unit_id=candidate.assignment_unit_id,
                    arm=candidate.arm,
                    spec_digest=experiment_spec_digest(spec),
                    spec=spec,
                    policy_decision=decision,
                )
            )

    capture_expectation = ordered_pairs[0][0].capture
    body: dict[str, object] = {
        "schema_version": "paired-intervention-plan/v1",
        "plan_id": plan_id,
        "randomization_seed": randomization_seed,
        "delta": delta.model_dump(mode="json"),
        "capture_expectation": capture_expectation.model_dump(mode="json"),
        "retry_policy": retry_policy.model_dump(mode="json"),
        "analysis_gate": analysis_gate.model_dump(mode="json"),
        "schedule": [entry.model_dump(mode="json") for entry in scheduled],
    }
    return PairedInterventionPlan.model_validate({**body, "plan_digest": _canonical_digest(body)})
