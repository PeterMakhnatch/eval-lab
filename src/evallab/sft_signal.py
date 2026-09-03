"""Offline SFT signal gate: frozen prereg, paired held-out analysis, typed decision.

This module answers exactly one question with deterministic, offline evidence:

    Did the produced SFT checkpoint establish a predeclared held-out behavioral
    signal over its exact baseline checkpoint?

Trust boundary (mirrors Tracks D/F/G/H, per the architecture review ruling):

- The prereg freeze is a self-digested frozen contract binding BOTH checkpoint
  identities, the Track D ``FrozenHeldOutEvaluationPlan``, the pairing cluster,
  the platform/isolation receipt identities, and the predeclared decision rule.
  It is published (staged, no-replace) before any outcome is admitted; outcomes
  cite the freeze digest, never the reverse.
- Every cited byte reopens through ``artifact_authority`` at
  ``bytes-verified``: baseline and candidate trainer-result manifests re-verify
  against their own authorities (dataset vs result authority roots stay split
  as in Track H), and every observation outcome re-verifies its authority.
- Every observation must exactly match one task in the frozen evaluation set:
  task, cluster, verifier, and environment digests are all checked, and family
  is derived from the frozen task id rather than accepted from outcome data.
- The paired delta is checkpoint-specific: the one variable between arms is the
  checkpoint pair; task identity must be identical within a pair. This is a
  dedicated contract, NOT a subclass of Track G's prompt-intervention outcome
  type; only the paired-result shape and cluster-bootstrap primitives are
  reused.
- Decisions report exact denominators, typed exclusions, and per-family
  effects with percentile-cluster intervals. There is no pooled headline: any
  family regression refuses an ``established`` signal.
- Outcome authority authenticates the cited bytes, but v1 has no canonical
  outcome schema from which to re-derive ``metric_value`` or ``capture_status``.
  Decisions are self-digested structural artifacts, not signed attestations;
  consumers must recompute them from authoritative input. Advisory-only is
  structural: ``ready_for_rl=False`` and ``authorization_scope="none"`` are
  literals, and nothing here changes ``trainer_bundle`` refusals.

No trainer, model call, Harbor run, queue submission, or network access
happens anywhere in this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from evallab.artifact_authority import (
    VERIFIER_IMPLEMENTATION_DIGEST,
    ArtifactAuthority,
    AuthorityRefusal,
    reverify_authority,
)
from evallab.benchmark_program_contracts import canonical_json, compute_sha256
from evallab.immutable_directory import publish_immutable_files
from evallab.schemas import ContractModel, Digest
from evallab.training_result import (
    FrozenHeldOutEvaluationPlan,
    TrainerResultManifest,
)

SFT_SIGNAL_FREEZE_SCHEMA = "sft-signal-freeze/v1"
SFT_SIGNAL_INPUT_SCHEMA = "sft-signal-input/v1"
SFT_SIGNAL_DECISION_SCHEMA = "sft-signal-decision/v1"
SFT_READINESS_SCHEMA = "sft-readiness/v1"

BOOTSTRAP_RESAMPLES = 4_000
_ZERO_DIGEST = "sha256:" + "0" * 64
_SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_METRIC_PATTERN = r"^[a-z][a-z0-9._-]{0,79}$"
_FAMILY_PATTERN = r"^[a-z][a-z0-9._-]{0,63}$"


class SftSignalRefusalCode(StrEnum):
    """Closed, fail-closed refusal vocabulary for the SFT signal gate."""

    FREEZE_DIGEST_MISMATCH = "freeze_digest_mismatch"
    BASELINE_AUTHORITY_UNVERIFIED = "baseline_authority_unverified"
    CANDIDATE_AUTHORITY_UNVERIFIED = "candidate_authority_unverified"
    RESULT_STRUCTURE_REFUSED = "result_structure_refused"
    IDENTICAL_CHECKPOINT_IDENTITIES = "identical_checkpoint_identities"
    CHECKPOINT_CHAIN_MISMATCH = "checkpoint_chain_mismatch"
    HELDOUT_PLAN_MISMATCH = "heldout_plan_mismatch"
    MODEL_IDENTITY_MISMATCH = "model_identity_mismatch"
    MANIFEST_IDENTITY_REUSE = "manifest_identity_reuse"
    ENVIRONMENT_IDENTITY_MISMATCH = "environment_identity_mismatch"
    RUNTIME_IDENTITY_MISMATCH = "runtime_identity_mismatch"
    OBSERVATION_FREEZE_MISMATCH = "observation_freeze_mismatch"
    OBSERVATION_AUTHORITY_UNVERIFIED = "observation_authority_unverified"
    OBSERVATION_DIGEST_MISMATCH = "observation_digest_mismatch"
    EVALUATION_TASK_NOT_FROZEN = "evaluation_task_not_frozen"
    EVALUATION_TASK_IDENTITY_MISMATCH = "evaluation_task_identity_mismatch"
    EVALUATION_CLUSTER_MISMATCH = "evaluation_cluster_mismatch"
    EVALUATION_SET_INCOMPLETE = "evaluation_set_incomplete"
    METRIC_MISMATCH = "metric_mismatch"
    MISSING_PAIR_ARM = "missing_pair_arm"
    DUPLICATE_PAIR_ARM = "duplicate_pair_arm"
    DUPLICATE_TRIAL = "duplicate_trial"
    DUPLICATE_OUTCOME = "duplicate_outcome"
    DUPLICATE_EVALUATION_TASK = "duplicate_evaluation_task"
    TASK_IDENTITY_MISMATCH = "task_identity_mismatch"
    NO_ELIGIBLE_PAIRS = "no_eligible_pairs"
    FAMILY_UNINFORMATIVE = "family_uninformative"
    PROTECTED_FAMILY_UNINFORMATIVE = "protected_family_uninformative"
    FAMILY_REGRESSION = "family_regression"
    PROTECTED_FAMILY_NOT_SUPPORTED = "protected_family_not_supported"


class SftExclusionCode(StrEnum):
    """Closed reasons a structurally valid pair leaves the denominator."""

    BASELINE_CAPTURE_INCOMPLETE = "baseline_capture_incomplete"
    CANDIDATE_CAPTURE_INCOMPLETE = "candidate_capture_incomplete"


class SignalStatus(StrEnum):
    """Decision status of the predeclared SFT signal question."""

    SUPPORTED = "supported"
    NOT_ESTABLISHED = "not_established"
    UNAVAILABLE = "unavailable"
    REFUSED = "refused"


class FamilyStatus(StrEnum):
    """Per-family paired effect status (never pooled across families)."""

    SUPPORTED = "supported"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"
    UNDERPOWERED = "underpowered"
    UNAVAILABLE = "unavailable"


class ReadinessStatus(StrEnum):
    READY = "ready"
    UNAVAILABLE = "unavailable"
    REFUSED = "refused"


class SftSignalRefusal(ValueError):
    """Raised only for malformed input shapes; evidence refusals are typed data."""

    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}")


class _FrozenContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_digest(value: object) -> str:
    return f"sha256:{compute_sha256(canonical_json(value))}"


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("value must be finite")
    return value


def _task_family_from_id(task_id: str) -> str:
    """Return the family namespace carried by a frozen evaluation task id."""

    family, separator, task_name = task_id.partition("/")
    if separator != "/" or not task_name or re.fullmatch(_FAMILY_PATTERN, family) is None:
        raise ValueError("evaluation task ids must be namespaced as canonical-family/task")
    return family


def _seed_int(*parts: object) -> int:
    payload = "\0".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


class SftCheckpointIdentityV1(_FrozenContract):
    """One digest-bound checkpoint arm of the freeze."""

    role: Literal["baseline", "candidate"]
    model_revision: str = Field(min_length=1)
    model_digest: Digest
    checkpoint_artifact_digest: Digest

    @model_validator(mode="after")
    def digests_are_non_zero(self) -> SftCheckpointIdentityV1:
        if self.model_digest == _ZERO_DIGEST or (self.checkpoint_artifact_digest == _ZERO_DIGEST):
            raise ValueError("checkpoint identity digests cannot be all-zero")
        return self


class SftSignalFreezeV1(_FrozenContract):
    """Predeclared experiment contract; published before any outcome exists.

    ``freeze_digest`` canonically binds the Track D frozen held-out plan, both
    checkpoint identities, the pairing cluster, the platform and isolation
    receipt digests, and the entire decision rule. Post-hoc edits change the
    digest and therefore cannot match any observation.
    """

    schema_version: Literal["sft-signal-freeze/v1"] = SFT_SIGNAL_FREEZE_SCHEMA
    freeze_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    held_out_plan: FrozenHeldOutEvaluationPlan
    baseline_checkpoint: SftCheckpointIdentityV1
    candidate_checkpoint: SftCheckpointIdentityV1
    pairing_cluster_digest: Digest
    environment_identity_digest: Digest = Field(
        description="Expected platform_receipt_digest for both completed arms"
    )
    runtime_identity_digest: Digest = Field(
        description="Expected isolation_receipt_digest for both completed arms"
    )
    metric_name: str = Field(pattern=_METRIC_PATTERN)
    direction: Literal["higher", "lower"]
    minimum_effect: float = Field(gt=0.0)
    confidence_level: float = Field(gt=0.0, lt=1.0)
    minimum_eligible_pairs: int = Field(ge=1)
    bootstrap_resamples: int = Field(ge=1)
    protected_families: tuple[str, ...] = ()
    freeze_digest: Digest

    @field_validator("minimum_effect")
    @classmethod
    def effect_is_finite(cls, value: float) -> float:
        return _finite(value)

    @field_validator("protected_families")
    @classmethod
    def families_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for family in value:
            if re.fullmatch(_FAMILY_PATTERN, family) is None:
                raise ValueError(f"protected family {family!r} is not a canonical name")
        if value != tuple(sorted(set(value))):
            raise ValueError("protected families must be sorted and unique")
        return value

    @model_validator(mode="after")
    def freeze_is_coherent(self) -> SftSignalFreezeV1:
        if self.baseline_checkpoint.role != "baseline" or (
            self.candidate_checkpoint.role != "candidate"
        ):
            raise ValueError("checkpoint roles must match their declared arms")
        if (
            self.baseline_checkpoint.checkpoint_artifact_digest
            == self.candidate_checkpoint.checkpoint_artifact_digest
        ):
            raise ValueError("identical_checkpoint_identities")
        if self.pairing_cluster_digest != self.held_out_plan.heldout_cluster_key_digest:
            raise ValueError("pairing cluster must equal the frozen held-out cluster")
        evaluation_tasks = self.held_out_plan.evaluation_set.tasks
        if any(task.cluster_key_digest != self.pairing_cluster_digest for task in evaluation_tasks):
            raise ValueError("every frozen evaluation task must match the pairing cluster")
        evaluation_families = {_task_family_from_id(task.task_id) for task in evaluation_tasks}
        if not set(self.protected_families).issubset(evaluation_families):
            raise ValueError("protected families must be present in the frozen evaluation set")
        if not self.held_out_plan.verify_plan_digest():
            raise ValueError("held_out_plan_digest_mismatch")
        expected = _canonical_digest(self.model_dump(mode="json", exclude={"freeze_digest"}))
        if self.freeze_digest != expected:
            raise ValueError("freeze_digest does not match freeze content")
        return self


class SftSignalObservationV1(_FrozenContract):
    """One frozen-task outcome bound to one checkpoint arm and byte authority."""

    schema_version: Literal["sft-signal-observation/v1"] = "sft-signal-observation/v1"
    freeze_digest: Digest
    arm: Literal["baseline", "candidate"]
    pair_id: str = Field(pattern=_SAFE_ID_PATTERN)
    task_id: str = Field(min_length=1)
    task_digest: Digest
    cluster_key_digest: Digest
    verifier_digest: Digest
    environment_digest: Digest
    trial_id: str = Field(pattern=_SAFE_ID_PATTERN)
    outcome_artifact_digest: Digest
    metric_name: str = Field(pattern=_METRIC_PATTERN)
    metric_value: float
    capture_status: Literal["complete", "missing", "corrupt"]
    authority: ArtifactAuthority

    @field_validator("outcome_artifact_digest")
    @classmethod
    def digest_is_non_zero(cls, value: str) -> str:
        if value == _ZERO_DIGEST:
            raise ValueError("outcome digest cannot be all-zero")
        return value

    @field_validator("metric_value")
    @classmethod
    def metric_is_finite(cls, value: float) -> float:
        return _finite(value)


class SftArmResultV1(_FrozenContract):
    """One completed trainer-result manifest plus its bytes authority."""

    manifest: TrainerResultManifest
    manifest_authority: ArtifactAuthority


class SftSignalInputV1(_FrozenContract):
    """Everything the gate needs, addressable by digest and re-openable bytes."""

    schema_version: Literal["sft-signal-input/v1"] = SFT_SIGNAL_INPUT_SCHEMA
    freeze: SftSignalFreezeV1
    baseline_result: SftArmResultV1
    candidate_result: SftArmResultV1
    observations: tuple[SftSignalObservationV1, ...] = ()
    baseline_authority_repo_root: Path | None = None
    candidate_authority_repo_root: Path | None = None
    observation_authority_repo_root: Path | None = None

    @model_validator(mode="after")
    def manifests_are_distinct_and_completed(self) -> SftSignalInputV1:
        baseline = self.baseline_result.manifest
        candidate = self.candidate_result.manifest
        if baseline.manifest_digest == candidate.manifest_digest:
            raise ValueError("baseline and candidate must be distinct result manifests")
        for arm, manifest in (("baseline", baseline), ("candidate", candidate)):
            if manifest.run_identity.terminal_status != "completed":
                raise ValueError(f"{arm} result must be a completed run")
        return self


class SftPairExclusionV1(_FrozenContract):
    """One excluded pair with its closed exclusion reasons."""

    pair_id: str
    reasons: tuple[SftExclusionCode, ...]

    @model_validator(mode="after")
    def reasons_are_canonical(self) -> SftPairExclusionV1:
        if self.reasons != tuple(sorted(set(self.reasons), key=str)):
            raise ValueError("pair exclusion reasons must be sorted and unique")
        return self


class SftFamilySignalV1(_FrozenContract):
    """Per-family paired effect with a deterministic percentile interval."""

    family: str
    pair_total: int = Field(ge=0)
    eligible_pairs: int = Field(ge=0)
    wins: int = Field(ge=0)
    ties: int = Field(ge=0)
    losses: int = Field(ge=0)
    mean_direction_adjusted_delta: float | None = None
    interval_lower: float | None = None
    interval_upper: float | None = None
    resamples: int = Field(ge=0)
    status: FamilyStatus

    @model_validator(mode="after")
    def counts_reconcile(self) -> SftFamilySignalV1:
        if self.wins + self.ties + self.losses != self.eligible_pairs:
            raise ValueError(f"family {self.family!r} outcome counts do not reconcile")
        has_effect = self.mean_direction_adjusted_delta is not None
        if self.eligible_pairs == 0 and has_effect:
            raise ValueError("empty family cannot claim a mean effect")
        if has_effect != (self.interval_lower is not None):
            raise ValueError("mean effect and interval must be present together")
        return self


class SftSignalDecisionV1(_FrozenContract):
    """Typed advisory decision; structurally incapable of authorizing anything.

    Model validation proves only canonical shape and self-integrity. It is not
    an authenticity check: consumers that need a gate result must call
    :func:`analyze_sft_signal` with the authoritative input rather than trust a
    deserialized decision.
    """

    schema_version: Literal["sft-signal-decision/v1"] = SFT_SIGNAL_DECISION_SCHEMA
    freeze_id: str
    freeze_digest: Digest
    baseline_checkpoint_digest: Digest
    candidate_checkpoint_digest: Digest
    status: SignalStatus
    reason_codes: tuple[SftSignalRefusalCode, ...] = ()
    pair_total: int = Field(ge=0)
    excluded_pair_count: int = Field(ge=0)
    denominator_eligible_pairs: int = Field(ge=0)
    exclusions: tuple[SftPairExclusionV1, ...] = ()
    families: tuple[SftFamilySignalV1, ...] = ()
    ready_for_rl: Literal[False] = False
    authorization_scope: Literal["none"] = "none"
    decision_digest: Digest

    @field_validator("reason_codes")
    @classmethod
    def reasons_are_canonical(
        cls, value: tuple[SftSignalRefusalCode, ...]
    ) -> tuple[SftSignalRefusalCode, ...]:
        if value != tuple(sorted(set(value), key=str)):
            raise ValueError("decision reason codes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def decision_reconciles(self) -> SftSignalDecisionV1:
        if self.pair_total != self.excluded_pair_count + self.denominator_eligible_pairs:
            raise ValueError("pair accounting does not reconcile")
        pair_ids = tuple(exclusion.pair_id for exclusion in self.exclusions)
        if pair_ids != tuple(sorted(set(pair_ids))):
            raise ValueError("pair exclusions must be unique and canonically sorted")
        families = tuple(family.family for family in self.families)
        if families != tuple(sorted(set(families))):
            raise ValueError("families must be unique and canonically sorted")
        if self.status == SignalStatus.REFUSED:
            if not self.reason_codes or (self.pair_total or self.exclusions or self.families):
                raise ValueError("refused decision carries reasons and no statistics")
        else:
            if self.status == SignalStatus.UNAVAILABLE and self.denominator_eligible_pairs:
                raise ValueError("unavailable decision cannot claim eligible pairs")
            if (
                self.status in (SignalStatus.UNAVAILABLE, SignalStatus.NOT_ESTABLISHED)
                and not self.reason_codes
            ):
                raise ValueError(f"{self.status.value} decision requires reason codes")
            if self.status == SignalStatus.SUPPORTED and (
                self.reason_codes
                or not self.families
                or any(family.status is not FamilyStatus.SUPPORTED for family in self.families)
            ):
                raise ValueError("supported decision requires every family supported")
        expected = _canonical_digest(self.model_dump(mode="json", exclude={"decision_digest"}))
        if self.decision_digest != expected:
            raise ValueError("decision_digest does not match decision content")
        return self

    def gate_table(self) -> str:
        """Render a compact stderr gate table (audit aid, never a claim)."""

        header = "family\tpairs\twins\tties\tlosses\tmean_delta\tinterval\tstatus"
        rows = [header]
        for family in self.families:
            interval = (
                f"[{family.interval_lower:.4f}, {family.interval_upper:.4f}]"
                if family.interval_lower is not None and family.interval_upper is not None
                else "-"
            )
            mean = (
                f"{family.mean_direction_adjusted_delta:.4f}"
                if family.mean_direction_adjusted_delta is not None
                else "-"
            )
            rows.append(
                f"{family.family}\t{family.eligible_pairs}\t{family.wins}\t"
                f"{family.ties}\t{family.losses}\t{mean}\t{interval}\t"
                f"{family.status.value}"
            )
        rows.append(f"DECISION\t{self.denominator_eligible_pairs}\t\t\t\t\t\t{self.status.value}")
        return "\n".join(rows)


class SftReadinessV1(_FrozenContract):
    """Chain-readiness answer; no outcome evidence is consumed or produced."""

    schema_version: Literal["sft-readiness/v1"] = SFT_READINESS_SCHEMA
    freeze_id: str
    freeze_digest: Digest
    baseline_checkpoint_digest: Digest
    candidate_checkpoint_digest: Digest
    status: ReadinessStatus
    reason_codes: tuple[SftSignalRefusalCode, ...] = ()
    heldout_task_count: int = Field(ge=0)
    ready_for_rl: Literal[False] = False
    readiness_digest: Digest

    @field_validator("reason_codes")
    @classmethod
    def reasons_are_canonical(
        cls, value: tuple[SftSignalRefusalCode, ...]
    ) -> tuple[SftSignalRefusalCode, ...]:
        if value != tuple(sorted(set(value), key=str)):
            raise ValueError("readiness reason codes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def readiness_reconciles(self) -> SftReadinessV1:
        if self.status == ReadinessStatus.REFUSED and not self.reason_codes:
            raise ValueError("refused readiness requires reason codes")
        if self.status == ReadinessStatus.READY and self.reason_codes:
            raise ValueError("ready readiness cannot carry reason codes")
        expected = _canonical_digest(self.model_dump(mode="json", exclude={"readiness_digest"}))
        if self.readiness_digest != expected:
            raise ValueError("readiness_digest does not match readiness content")
        return self


def _refuse(reason_code: str, detail: str) -> None:
    raise SftSignalRefusal(reason_code, detail)


def _reverify_result_arm(
    arm: SftArmResultV1,
    *,
    repo_root: Path | str | None,
) -> tuple[TrainerResultManifest, tuple[SftSignalRefusalCode, ...]]:
    """Reopen the manifest bytes through the authority and require equality."""

    verification = reverify_authority(
        arm.manifest_authority,
        expected_verifier_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        repo_root=repo_root,
    )
    if isinstance(verification, AuthorityRefusal):
        return arm.manifest, (SftSignalRefusalCode.RESULT_STRUCTURE_REFUSED,)
    raw_bytes, _ = verification
    try:
        authoritative = TrainerResultManifest.model_validate(json.loads(raw_bytes))
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError):
        return arm.manifest, (SftSignalRefusalCode.RESULT_STRUCTURE_REFUSED,)
    if authoritative != arm.manifest or not authoritative.verify_manifest_digest():
        return arm.manifest, (SftSignalRefusalCode.RESULT_STRUCTURE_REFUSED,)
    return authoritative, ()


def _chain_reasons(
    freeze: SftSignalFreezeV1,
    *,
    baseline: TrainerResultManifest,
    candidate: TrainerResultManifest,
) -> tuple[SftSignalRefusalCode, ...]:
    """Typed identity-chain failures between freeze and both result manifests."""

    reasons: list[SftSignalRefusalCode] = []
    baseline_produced = (
        baseline.produced_checkpoint.produced_checkpoint_artifact_digest
        if baseline.produced_checkpoint is not None
        else None
    )
    candidate_produced = (
        candidate.produced_checkpoint.produced_checkpoint_artifact_digest
        if candidate.produced_checkpoint is not None
        else None
    )
    chain_broken = (
        baseline_produced != freeze.baseline_checkpoint.checkpoint_artifact_digest
        or candidate_produced != freeze.candidate_checkpoint.checkpoint_artifact_digest
        or candidate.input_model_checkpoint_digest
        != freeze.baseline_checkpoint.checkpoint_artifact_digest
        or baseline_produced == candidate_produced
    )
    if chain_broken:
        reasons.append(SftSignalRefusalCode.CHECKPOINT_CHAIN_MISMATCH)
    for manifest in (baseline, candidate):
        if (
            manifest.dataset.heldout_split_digest != freeze.held_out_plan.heldout_split_digest
            or manifest.dataset.heldout_cluster_key_digest
            != freeze.held_out_plan.heldout_cluster_key_digest
        ):
            reasons.append(SftSignalRefusalCode.HELDOUT_PLAN_MISMATCH)
            break
    if baseline.model != candidate.model:
        reasons.append(SftSignalRefusalCode.MODEL_IDENTITY_MISMATCH)
    if (
        baseline.model.model_digest != freeze.baseline_checkpoint.model_digest
        or candidate.model.model_digest != freeze.candidate_checkpoint.model_digest
    ):
        reasons.append(SftSignalRefusalCode.MODEL_IDENTITY_MISMATCH)
    runtime_receipts = (
        baseline.provenance.runtime_receipts,
        candidate.provenance.runtime_receipts,
    )
    if any(
        receipts is None or receipts.platform_receipt_digest != freeze.environment_identity_digest
        for receipts in runtime_receipts
    ):
        reasons.append(SftSignalRefusalCode.ENVIRONMENT_IDENTITY_MISMATCH)
    if any(
        receipts is None or receipts.isolation_receipt_digest != freeze.runtime_identity_digest
        for receipts in runtime_receipts
    ):
        reasons.append(SftSignalRefusalCode.RUNTIME_IDENTITY_MISMATCH)
    return tuple(sorted(set(reasons), key=str))


def _parse_input(value: Mapping[str, Any] | SftSignalInputV1) -> SftSignalInputV1:
    if isinstance(value, SftSignalInputV1):
        return value
    try:
        return SftSignalInputV1.model_validate(dict(value))
    except ValidationError as exc:
        _refuse("invalid_input", str(exc.errors()[:3]))


def assess_sft_readiness(
    value: Mapping[str, Any] | SftSignalInputV1,
) -> SftReadinessV1:
    """Assess whether the frozen chain is complete; ignore observations entirely."""

    payload = _parse_input(value)
    freeze = payload.freeze
    baseline, baseline_reasons = _reverify_result_arm(
        payload.baseline_result,
        repo_root=payload.baseline_authority_repo_root,
    )
    candidate, candidate_reasons = _reverify_result_arm(
        payload.candidate_result,
        repo_root=payload.candidate_authority_repo_root,
    )
    reasons = sorted({*baseline_reasons, *candidate_reasons}, key=str)
    if not reasons:
        reasons = list(_chain_reasons(freeze, baseline=baseline, candidate=candidate))
    body: dict[str, Any] = {
        "schema_version": SFT_READINESS_SCHEMA,
        "freeze_id": freeze.freeze_id,
        "freeze_digest": freeze.freeze_digest,
        "baseline_checkpoint_digest": (freeze.baseline_checkpoint.checkpoint_artifact_digest),
        "candidate_checkpoint_digest": (freeze.candidate_checkpoint.checkpoint_artifact_digest),
        "status": ReadinessStatus.REFUSED if reasons else ReadinessStatus.READY,
        "reason_codes": tuple(reasons),
        "heldout_task_count": len(freeze.held_out_plan.evaluation_set.tasks),
        "ready_for_rl": False,
    }
    return SftReadinessV1.model_validate({**body, "readiness_digest": _canonical_digest(body)})


def _audit_observations(
    freeze: SftSignalFreezeV1,
    observations: Sequence[SftSignalObservationV1],
    *,
    repo_root: Path | str | None,
) -> tuple[SftSignalRefusalCode, ...]:
    """Require exact frozen-task membership before admitting paired evidence."""

    reasons: set[SftSignalRefusalCode] = set()
    planned_tasks = {task.task_id: task for task in freeze.held_out_plan.evaluation_set.tasks}
    observed_task_ids: set[str] = set()
    task_pair_ids: dict[str, str] = {}
    trial_ids: set[str] = set()
    outcome_digests: set[str] = set()
    arms_by_pair: dict[str, set[str]] = {}
    identity_by_pair: dict[str, tuple[Any, ...]] = {}
    for observation in observations:
        if observation.freeze_digest != freeze.freeze_digest:
            reasons.add(SftSignalRefusalCode.OBSERVATION_FREEZE_MISMATCH)
            continue
        if observation.metric_name != freeze.metric_name:
            reasons.add(SftSignalRefusalCode.METRIC_MISMATCH)
        planned_task = planned_tasks.get(observation.task_id)
        if planned_task is None:
            reasons.add(SftSignalRefusalCode.EVALUATION_TASK_NOT_FROZEN)
        else:
            observed_task_ids.add(observation.task_id)
            if (
                observation.task_digest != planned_task.task_digest
                or observation.verifier_digest != planned_task.verifier_digest
                or observation.environment_digest != planned_task.environment_digest
            ):
                reasons.add(SftSignalRefusalCode.EVALUATION_TASK_IDENTITY_MISMATCH)
            if (
                observation.cluster_key_digest != planned_task.cluster_key_digest
                or observation.cluster_key_digest != freeze.pairing_cluster_digest
            ):
                reasons.add(SftSignalRefusalCode.EVALUATION_CLUSTER_MISMATCH)
            prior_pair_id = task_pair_ids.setdefault(observation.task_id, observation.pair_id)
            if prior_pair_id != observation.pair_id:
                reasons.add(SftSignalRefusalCode.DUPLICATE_EVALUATION_TASK)
        if observation.authority.artifact.digest != observation.outcome_artifact_digest:
            reasons.add(SftSignalRefusalCode.OBSERVATION_DIGEST_MISMATCH)
        verification = reverify_authority(
            observation.authority,
            expected_verifier_digest=VERIFIER_IMPLEMENTATION_DIGEST,
            repo_root=repo_root,
        )
        if isinstance(verification, AuthorityRefusal):
            reasons.add(SftSignalRefusalCode.OBSERVATION_AUTHORITY_UNVERIFIED)
        if observation.trial_id in trial_ids:
            reasons.add(SftSignalRefusalCode.DUPLICATE_TRIAL)
        trial_ids.add(observation.trial_id)
        if observation.outcome_artifact_digest in outcome_digests:
            reasons.add(SftSignalRefusalCode.DUPLICATE_OUTCOME)
        outcome_digests.add(observation.outcome_artifact_digest)
        arms = arms_by_pair.setdefault(observation.pair_id, set())
        if observation.arm in arms:
            reasons.add(SftSignalRefusalCode.DUPLICATE_PAIR_ARM)
        arms.add(observation.arm)
        identity = (
            observation.task_id,
            observation.task_digest,
            observation.cluster_key_digest,
            observation.verifier_digest,
            observation.environment_digest,
        )
        prior = identity_by_pair.setdefault(observation.pair_id, identity)
        if prior != identity:
            reasons.add(SftSignalRefusalCode.TASK_IDENTITY_MISMATCH)
    for arms in arms_by_pair.values():
        if arms != {"baseline", "candidate"}:
            reasons.add(SftSignalRefusalCode.MISSING_PAIR_ARM)
    if observations and observed_task_ids != set(planned_tasks):
        reasons.add(SftSignalRefusalCode.EVALUATION_SET_INCOMPLETE)
    return tuple(sorted(reasons, key=str))


def _family_intervals(
    *,
    freeze: SftSignalFreezeV1,
    family: str,
    deltas: Sequence[float],
) -> tuple[float | None, float | None]:
    """Deterministic percentile interval; each pair is its own cluster."""

    if not deltas:
        return None, None
    generator = random.Random(_seed_int(freeze.freeze_digest, family, len(deltas)))
    resamples = freeze.bootstrap_resamples
    means: list[float] = []
    for _ in range(resamples):
        selected = (deltas[generator.randrange(len(deltas))] for _ in deltas)
        means.append(math.fsum(selected) / len(deltas))
    tail = (1.0 - freeze.confidence_level) / 2.0
    return _quantile(means, tail), _quantile(means, 1.0 - tail)


def _family_status(
    *,
    freeze: SftSignalFreezeV1,
    family: SftFamilySignalV1,
) -> FamilyStatus:
    if family.eligible_pairs == 0:
        return FamilyStatus.UNAVAILABLE
    if family.eligible_pairs < freeze.minimum_eligible_pairs:
        return FamilyStatus.UNDERPOWERED
    assert family.interval_lower is not None and family.interval_upper is not None
    if family.interval_lower >= freeze.minimum_effect:
        return FamilyStatus.SUPPORTED
    if family.interval_upper <= -freeze.minimum_effect:
        return FamilyStatus.REFUTED
    return FamilyStatus.INCONCLUSIVE


def analyze_sft_signal(
    value: Mapping[str, Any] | SftSignalInputV1,
) -> SftSignalDecisionV1:
    """Produce the typed advisory decision; evidence failures are typed data."""

    payload = _parse_input(value)
    freeze = payload.freeze

    def _refused(
        reasons: Sequence[SftSignalRefusalCode],
    ) -> SftSignalDecisionV1:
        body: dict[str, Any] = {
            "schema_version": SFT_SIGNAL_DECISION_SCHEMA,
            "freeze_id": freeze.freeze_id,
            "freeze_digest": freeze.freeze_digest,
            "baseline_checkpoint_digest": (freeze.baseline_checkpoint.checkpoint_artifact_digest),
            "candidate_checkpoint_digest": (freeze.candidate_checkpoint.checkpoint_artifact_digest),
            "status": SignalStatus.REFUSED,
            "reason_codes": tuple(sorted(set(reasons), key=str)),
            "pair_total": 0,
            "excluded_pair_count": 0,
            "denominator_eligible_pairs": 0,
            "exclusions": (),
            "families": (),
            "ready_for_rl": False,
            "authorization_scope": "none",
        }
        return SftSignalDecisionV1.model_validate(
            {**body, "decision_digest": _canonical_digest(body)}
        )

    baseline, baseline_reasons = _reverify_result_arm(
        payload.baseline_result,
        repo_root=payload.baseline_authority_repo_root,
    )
    candidate, candidate_reasons = _reverify_result_arm(
        payload.candidate_result,
        repo_root=payload.candidate_authority_repo_root,
    )
    refusals: list[SftSignalRefusalCode] = [
        *baseline_reasons,
        *candidate_reasons,
        *_chain_reasons(freeze, baseline=baseline, candidate=candidate),
        *_audit_observations(
            freeze,
            payload.observations,
            repo_root=payload.observation_authority_repo_root,
        ),
    ]
    if refusals:
        return _refused(refusals)

    by_pair: dict[str, dict[str, SftSignalObservationV1]] = {}
    for observation in payload.observations:
        by_pair.setdefault(observation.pair_id, {})[observation.arm] = observation

    pair_total = len(by_pair)
    exclusions: list[SftPairExclusionV1] = []
    family_pair_totals: dict[str, int] = {}
    family_deltas: dict[str, list[float]] = {}
    for pair_id in sorted(by_pair):
        arms = by_pair[pair_id]
        baseline_observation = arms["baseline"]
        candidate_observation = arms["candidate"]
        family = _task_family_from_id(baseline_observation.task_id)
        family_pair_totals[family] = family_pair_totals.get(family, 0) + 1
        exclusion_reasons: set[SftExclusionCode] = set()
        if baseline_observation.capture_status != "complete":
            exclusion_reasons.add(SftExclusionCode.BASELINE_CAPTURE_INCOMPLETE)
        if candidate_observation.capture_status != "complete":
            exclusion_reasons.add(SftExclusionCode.CANDIDATE_CAPTURE_INCOMPLETE)
        if exclusion_reasons:
            exclusions.append(
                SftPairExclusionV1(
                    pair_id=pair_id, reasons=tuple(sorted(exclusion_reasons, key=str))
                )
            )
            continue
        raw_delta = candidate_observation.metric_value - baseline_observation.metric_value
        adjusted = raw_delta if freeze.direction == "higher" else -raw_delta
        family_deltas.setdefault(family, []).append(adjusted)

    families: list[SftFamilySignalV1] = []
    for family in sorted(family_pair_totals):
        deltas = family_deltas.get(family, [])
        mean = math.fsum(deltas) / len(deltas) if deltas else None
        lower, upper = _family_intervals(freeze=freeze, family=family, deltas=deltas)
        families.append(
            SftFamilySignalV1(
                family=family,
                pair_total=family_pair_totals[family],
                eligible_pairs=len(deltas),
                wins=sum(delta > 0.0 for delta in deltas),
                ties=sum(delta == 0.0 for delta in deltas),
                losses=sum(delta < 0.0 for delta in deltas),
                mean_direction_adjusted_delta=mean,
                interval_lower=lower,
                interval_upper=upper,
                resamples=freeze.bootstrap_resamples if deltas else 0,
                status=FamilyStatus.INCONCLUSIVE,
            )
        )
    families = [
        family.model_copy(
            update={
                "status": _family_status(freeze=freeze, family=family),
            }
        )
        for family in families
    ]

    denominator = pair_total - len(exclusions)
    reasons: list[SftSignalRefusalCode] = []
    if denominator == 0:
        status = SignalStatus.UNAVAILABLE
        reasons.append(SftSignalRefusalCode.NO_ELIGIBLE_PAIRS)
    else:
        protected = set(freeze.protected_families)
        any_refuted = any(family.status is FamilyStatus.REFUTED for family in families)
        any_uninformative = any(
            family.status in (FamilyStatus.UNDERPOWERED, FamilyStatus.UNAVAILABLE)
            for family in families
        )
        unsupported_protected = any(
            family.family in protected and family.status is not FamilyStatus.SUPPORTED
            for family in families
        )
        all_supported = bool(families) and all(
            family.status is FamilyStatus.SUPPORTED for family in families
        )
        if any_refuted:
            status = SignalStatus.NOT_ESTABLISHED
            reasons.append(SftSignalRefusalCode.FAMILY_REGRESSION)
        elif any_uninformative:
            status = SignalStatus.NOT_ESTABLISHED
            reasons.append(
                SftSignalRefusalCode.PROTECTED_FAMILY_UNINFORMATIVE
                if any(
                    family.family in protected
                    and family.status in (FamilyStatus.UNDERPOWERED, FamilyStatus.UNAVAILABLE)
                    for family in families
                )
                else SftSignalRefusalCode.FAMILY_UNINFORMATIVE
            )
        elif all_supported:
            status = SignalStatus.SUPPORTED
        else:
            status = SignalStatus.NOT_ESTABLISHED
            reasons.append(
                SftSignalRefusalCode.PROTECTED_FAMILY_NOT_SUPPORTED
                if unsupported_protected
                else SftSignalRefusalCode.FAMILY_UNINFORMATIVE
            )

    body = {
        "schema_version": SFT_SIGNAL_DECISION_SCHEMA,
        "freeze_id": freeze.freeze_id,
        "freeze_digest": freeze.freeze_digest,
        "baseline_checkpoint_digest": (freeze.baseline_checkpoint.checkpoint_artifact_digest),
        "candidate_checkpoint_digest": (freeze.candidate_checkpoint.checkpoint_artifact_digest),
        "status": status,
        "reason_codes": tuple(sorted(set(reasons), key=str)),
        "pair_total": pair_total,
        "excluded_pair_count": len(exclusions),
        "denominator_eligible_pairs": denominator,
        "exclusions": [item.model_dump(mode="json") for item in exclusions],
        "families": [item.model_dump(mode="json") for item in families],
        "ready_for_rl": False,
        "authorization_scope": "none",
    }
    return SftSignalDecisionV1.model_validate({**body, "decision_digest": _canonical_digest(body)})


def _normalized_json_payload(value: Any) -> Any:
    """Normalize constructor kwargs into the model's JSON shape (no aliases)."""

    if isinstance(value, ContractModel):
        return _normalized_json_payload(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {key: _normalized_json_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalized_json_payload(item) for item in value]
    return value


def create_sft_signal_freeze(**kwargs: Any) -> SftSignalFreezeV1:
    """Create a freeze and fill its deterministic content digest when omitted."""

    kwargs.setdefault("schema_version", SFT_SIGNAL_FREEZE_SCHEMA)
    if not kwargs.get("freeze_digest"):
        payload = {
            key: _normalized_json_payload(value)
            for key, value in kwargs.items()
            if key != "freeze_digest"
        }
        kwargs["freeze_digest"] = _canonical_digest(payload)
    return SftSignalFreezeV1.model_validate(kwargs)


def publish_sft_artifact(
    directory: Path,
    artifact: SftSignalDecisionV1 | SftReadinessV1,
) -> Path:
    """Publish exactly one immutable ``artifact.json`` (staged, no-replace)."""

    payload = canonical_json(artifact.model_dump(mode="json")).encode("utf-8") + b"\n"
    return publish_immutable_files(directory, {"artifact.json": payload})


__all__ = [
    "BOOTSTRAP_RESAMPLES",
    "FamilyStatus",
    "ReadinessStatus",
    "SignalStatus",
    "SftArmResultV1",
    "SftCheckpointIdentityV1",
    "SftExclusionCode",
    "SftFamilySignalV1",
    "SftPairExclusionV1",
    "SftReadinessV1",
    "SftSignalDecisionV1",
    "SftSignalFreezeV1",
    "SftSignalInputV1",
    "SftSignalObservationV1",
    "SftSignalRefusal",
    "SftSignalRefusalCode",
    "analyze_sft_signal",
    "assess_sft_readiness",
    "create_sft_signal_freeze",
]
