"""External trainer-result manifest and frozen held-out evaluation handoff.

This module validates output produced by an external trainer against Track D's
typed ``ExpectedTrainerResultV1`` projection and renders a proposed frozen
Harbor evaluation handoff. It never imports a trainer backend or submits,
registers, or executes anything.

The manifest is conditional: failed/cancelled pre-checkpoint records are
representable, while completed records require checkpoint, metrics, artifacts,
receipts, and non-contamination evidence. Input checkpoint identity is bound
only to Track D's input; a produced checkpoint names output bytes only.

Structural fixtures remain non-authorizing. A completed record becomes eligible
for a real handoff only after the shared b53 artifact-authority surface
re-reads and verifies the expected dataset-manifest bytes.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from evallab.analysis_statistics import canonical_digest
from evallab.artifact_authority import (
    VERIFIER_IMPLEMENTATION_DIGEST,
    ArtifactAuthority,
    AuthorityRefusal,
    reverify_authority,
)
from evallab.schemas import ContractModel, Digest
from evallab.trainer_bundle import (
    ExpectedTrainerResultV1,
    TrainerEvaluationSetV1,
    TrainerTaskIdentityV1,
)

TRAINER_RESULT_MANIFEST_SCHEMA = "trainer-result-manifest-v1"


class TrainerResultRefusalCode(StrEnum):
    """Closed set of fail-closed result-manifest validation outcomes."""

    UNKNOWN_FIELD = "unknown_field"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    MISSING_UNCERTAINTY = "missing_uncertainty"
    INVALID_DIGEST = "invalid_digest"
    INVALID_COMMIT = "invalid_commit"
    MANIFEST_DIGEST_MISMATCH = "manifest_digest_mismatch"
    TRAINER_BUNDLE_DIGEST_MISMATCH = "trainer_bundle_digest_mismatch"
    TRAINER_PLAN_DIGEST_MISMATCH = "trainer_plan_digest_mismatch"
    EXPECTED_PROJECTION_MISMATCH = "expected_projection_mismatch"
    INPUT_CHECKPOINT_MISMATCH = "input_checkpoint_mismatch"
    HELD_OUT_SPLIT_IN_TRAIN_INPUTS = "held_out_split_in_train_inputs"
    CLUSTER_KEY_OVERLAP = "cluster_key_overlap"
    MISSING_NON_CONTAMINATION_EVIDENCE = "missing_non_contamination_evidence"
    UNSAFE_ARTIFACT_REF = "unsafe_artifact_ref"
    NON_CONTAMINATION_EVIDENCE_MISMATCH = "non_contamination_evidence_mismatch"
    UNSUPPORTED_TRAINING_BACKEND = "unsupported_training_backend"
    UNSUPPORTED_ADAPTER_CONTRACT = "unsupported_adapter_contract"
    HELD_OUT_RESULT_IN_MANIFEST = "held_out_result_in_manifest"
    TERMINAL_STATUS_NOT_COMPLETED = "terminal_status_not_completed"
    COMPLETED_RUN_REQUIRES_METRICS = "completed_run_requires_metrics"
    COMPLETED_RUN_REQUIRES_CHECKPOINT = "completed_run_requires_checkpoint"
    COMPLETED_RUN_REQUIRES_RECEIPTS = "completed_run_requires_receipts"
    COMPLETED_RUN_REQUIRES_ARTIFACTS = "completed_run_requires_artifacts"
    CHECKPOINT_BINDING_MISMATCH = "checkpoint_binding_mismatch"
    SPLIT_PARITY_MISMATCH = "split_parity_mismatch"
    PLAN_DIGEST_MISMATCH = "plan_digest_mismatch"
    FROZEN_TASK_SET_MISMATCH = "frozen_task_set_mismatch"
    SOURCE_MANIFEST_MISMATCH = "source_manifest_mismatch"
    INVALID_MANIFEST = "invalid_manifest"
    FROZEN_TASK_CLUSTER_MISMATCH = "frozen_task_cluster_mismatch"
    AUTHORITY_NOT_REVERIFIED = "authority_not_reverified"


class TrainerResultManifestRefused(ValueError):
    """Raised when a pure plan renderer receives a refused or ineligible manifest."""

    def __init__(self, reason_codes: Sequence[TrainerResultRefusalCode]) -> None:
        self.reason_codes = tuple(reason_codes)
        super().__init__("trainer result manifest refused: " + ", ".join(self.reason_codes))


def _safe_relative_artifact_ref(value: str, label: str) -> str:
    """Require a canonical POSIX relative artifact coordinate."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty repository-relative path")
    path = PurePosixPath(value)
    if (
        "\\" in value
        or path.is_absolute()
        or not path.parts
        or value != path.as_posix()
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{label} must be a safe canonical repository-relative path")
    return value


def compute_cluster_key_digest(keys: Sequence[str]) -> Digest:
    """Digest a non-empty canonical logical key set without retaining raw keys."""
    normalized = tuple(sorted(set(keys)))
    if not normalized or any(not isinstance(key, str) or not key.strip() for key in normalized):
        raise ValueError("cluster keys must be non-empty strings")
    return canonical_digest(normalized)


class ModelCheckpointIdentity(ContractModel):
    """Immutable identity of the base model being trained (Track D naming)."""

    model_revision: str = Field(min_length=1)
    model_digest: Digest
    tokenizer_revision: str = Field(min_length=1)
    tokenizer_digest: Digest
    chat_template_revision: str = Field(min_length=1)
    chat_template_digest: Digest


class ProducedCheckpoint(ContractModel):
    """The single output checkpoint a completed run is accountable for.

    This names PRODUCED bytes only; it is never compared against the bound
    input checkpoint and is bytes-verified at ingestion.
    """

    produced_checkpoint_artifact_digest: Digest


class DatasetSplitBinding(ContractModel):
    """Dataset identity with one train split and digest-only cluster boundary."""

    dataset_manifest_digest: Digest
    dataset_manifest_authority_digest: Digest
    dataset_manifest_verifier_digest: Digest
    dataset_manifest_authority_level: Literal["bytes-verified"]
    dataset_digest: Digest
    train_split_digest: Digest
    heldout_split_digest: Digest
    train_cluster_key_digest: Digest
    heldout_cluster_key_digest: Digest

    @model_validator(mode="after")
    def _enforce_split_integrity(self) -> DatasetSplitBinding:
        if self.heldout_split_digest == self.train_split_digest:
            raise ValueError("held_out_split_in_train_inputs")
        if self.heldout_cluster_key_digest == self.train_cluster_key_digest:
            raise ValueError("cluster_key_overlap")
        return self


class MetricUncertainty(ContractModel):
    """Reported uncertainty interval; a bare point estimate is inadmissible."""

    method: str = Field(min_length=1)
    lower_bound: float
    upper_bound: float

    @model_validator(mode="after")
    def _validate_bounds(self) -> MetricUncertainty:
        if not math.isfinite(self.lower_bound) or not math.isfinite(self.upper_bound):
            raise ValueError("uncertainty bounds must be finite")
        if self.lower_bound > self.upper_bound:
            raise ValueError("uncertainty lower_bound must not exceed upper_bound")
        return self


class ReportedMetric(ContractModel):
    """A trainer-reported metric with an interval and its denominator.

    ``scope`` is closed to train/validation: an accepted held-out result can
    never be embedded in this manifest, because the held-out split is neither a
    permitted scope nor a representable field.
    """

    metric_name: str = Field(min_length=1)
    scope: Literal["train", "validation"] = "train"
    estimate: float
    uncertainty: MetricUncertainty
    sample_size: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_estimate(self) -> ReportedMetric:
        if not math.isfinite(self.estimate):
            raise ValueError("metric estimate must be finite")
        if not self.uncertainty.lower_bound <= self.estimate <= self.uncertainty.upper_bound:
            raise ValueError("metric estimate must lie within its uncertainty interval")
        return self


class ResultArtifact(ContractModel):
    """Digest-bound external result artifact addressed under its result bundle."""

    artifact_ref: str = Field(min_length=1)
    artifact_digest: Digest
    media_type: str = Field(min_length=1)

    @field_validator("artifact_ref")
    @classmethod
    def _validate_artifact_ref(cls, value: str) -> str:
        return _safe_relative_artifact_ref(value, "artifact_ref")


class ImmutableRuntimeReceipts(ContractModel):
    """Canonical receipt digests for every runtime isolation and hardware claim."""

    platform_receipt_digest: Digest
    isolation_receipt_digest: Digest
    allowlist_receipt_digest: Digest
    hardware_receipt_digest: Digest


class TrainerResultProvenance(ContractModel):
    """Typed immutable identity of the external training result's source record.

    ``runtime_receipts`` is status-conditional evidence: only a completed run
    must carry it, so a pre-checkpoint failure stays recordable without
    minting isolation claims it cannot support.
    """

    source_job_identity: str = Field(min_length=1)
    source_trial_identity: str = Field(min_length=1)
    source_artifact_digest: Digest
    runtime_receipts: ImmutableRuntimeReceipts | None = None
    result_adapter_identity: str = Field(min_length=1)
    benchmark_family: str = Field(min_length=1)


TrainingBackendNameV1 = Literal["generic-trl", "spade-external-consumer"]
AdapterContractV1 = Literal["trl-sft-plan/v1", "spade-shaped-plan/v1"]


class TrainingBackendIdentity(ContractModel):
    """Backend identity in this module's unified literal set (no mapping layer).

    Agent Lightning and verl are rejected for v1: they are absent from the
    closed backend-name set, so naming them refuses at validation time.
    """

    backend_name: TrainingBackendNameV1
    backend_version: str = Field(min_length=1)
    backend_source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    backend_image_digest: Digest


TerminalStatusV1 = Literal["completed", "failed", "cancelled"]


class TrainingRunIdentity(ContractModel):
    """Seed and terminal-status identity of the external training run.

    This is a conditional contract: ``completed``, ``failed``, and
    ``cancelled`` runs are all representable records.  Eligibility for the
    held-out handoff is decided downstream of the record itself.
    """

    seed: int = Field(ge=0)
    terminal_status: TerminalStatusV1
    terminal_status_reason: str = Field(min_length=1)




def compute_split_integrity_binding_digest(
    *,
    train_split_digest: str,
    heldout_split_digest: str,
    train_cluster_key_digest: str,
    heldout_cluster_key_digest: str,
) -> Digest:
    """Hash the mechanically observed split and cluster separation facts."""
    return canonical_digest(
        {
            "train_split_digest": train_split_digest,
            "heldout_split_digest": heldout_split_digest,
            "train_cluster_key_digest": train_cluster_key_digest,
            "heldout_cluster_key_digest": heldout_cluster_key_digest,
        }
    )


class NonContaminationEvidence(ContractModel):
    """Immutable enforcement evidence, not a free-form assertion of separation."""

    evidence_ref: str = Field(min_length=1)
    evidence_digest: Digest
    enforcement_status: Literal["verified"] = "verified"
    observed_train_split_digest: Digest
    observed_heldout_split_digest: Digest
    observed_train_cluster_key_digest: Digest
    observed_heldout_cluster_key_digest: Digest
    split_integrity_binding_digest: Digest

    @field_validator("evidence_ref")
    @classmethod
    def _validate_evidence_ref(cls, value: str) -> str:
        return _safe_relative_artifact_ref(value, "evidence_ref")

    @model_validator(mode="after")
    def _verify_observed_binding(self) -> NonContaminationEvidence:
        if self.observed_heldout_split_digest == self.observed_train_split_digest:
            raise ValueError("held_out_split_in_train_inputs")
        if self.observed_heldout_cluster_key_digest == self.observed_train_cluster_key_digest:
            raise ValueError("cluster_key_overlap")
        expected = compute_split_integrity_binding_digest(
            train_split_digest=self.observed_train_split_digest,
            heldout_split_digest=self.observed_heldout_split_digest,
            train_cluster_key_digest=self.observed_train_cluster_key_digest,
            heldout_cluster_key_digest=self.observed_heldout_cluster_key_digest,
        )
        if self.split_integrity_binding_digest != expected:
            raise ValueError("non_contamination_evidence_mismatch")
        return self


class TrainerResultManifest(ContractModel):
    """External training output bound to Track D's expected projection.

    Conditional contract: only a ``completed`` run must carry metrics, the
    produced checkpoint, artifacts, receipts, and non-contamination evidence.
    """

    schema_version: Literal["trainer-result-manifest-v1"] = TRAINER_RESULT_MANIFEST_SCHEMA
    manifest_digest: Digest
    source_authority_status: Literal["copied_digest_refs_only"]
    result_manifest_path: str = Field(min_length=1)
    adapter_contract: AdapterContractV1
    trainer_bundle_digest: Digest
    trainer_plan_digest: Digest
    backend_identity: TrainingBackendIdentity
    model: ModelCheckpointIdentity
    input_model_checkpoint_digest: Digest
    produced_checkpoint: ProducedCheckpoint | None = None
    dataset: DatasetSplitBinding
    run_identity: TrainingRunIdentity
    effective_config_digest: Digest
    reported_metrics: tuple[ReportedMetric, ...] = ()
    training_log_artifacts: tuple[ResultArtifact, ...] = ()
    checkpoint_artifacts: tuple[ResultArtifact, ...] = ()
    result_artifacts: tuple[ResultArtifact, ...] = ()
    provenance: TrainerResultProvenance
    non_contamination_evidence: tuple[NonContaminationEvidence, ...] = ()
    exclusion_notes: tuple[str, ...] = ()
    provenance_notes: tuple[str, ...] = ()

    @field_validator("result_manifest_path")
    @classmethod
    def _validate_result_manifest_path(cls, value: str) -> str:
        return _safe_relative_artifact_ref(value, "result_manifest_path")

    @field_validator("reported_metrics")
    @classmethod
    def _canonicalize_metrics(
        cls, values: tuple[ReportedMetric, ...]
    ) -> tuple[ReportedMetric, ...]:
        names = [metric.metric_name for metric in values]
        if len(set(names)) != len(names):
            raise ValueError("reported metric names must be unique")
        return tuple(sorted(values, key=lambda metric: metric.metric_name))

    @field_validator("training_log_artifacts", "checkpoint_artifacts", "result_artifacts")
    @classmethod
    def _canonicalize_artifacts(
        cls, values: tuple[ResultArtifact, ...]
    ) -> tuple[ResultArtifact, ...]:
        refs = [artifact.artifact_ref for artifact in values]
        if len(set(refs)) != len(refs):
            raise ValueError("artifact references must be unique")
        return tuple(sorted(values, key=lambda artifact: artifact.artifact_ref))

    @field_validator("non_contamination_evidence")
    @classmethod
    def _canonicalize_evidence(
        cls, values: tuple[NonContaminationEvidence, ...]
    ) -> tuple[NonContaminationEvidence, ...]:
        refs = [evidence.evidence_ref for evidence in values]
        if len(set(refs)) != len(refs):
            raise ValueError("evidence references must be unique")
        return tuple(sorted(values, key=lambda evidence: evidence.evidence_ref))

    @field_validator("exclusion_notes", "provenance_notes")
    @classmethod
    def _canonicalize_notes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("notes must not contain blank entries")
        return tuple(sorted(set(values)))

    @model_validator(mode="after")
    def _validate_conditional_contract(self) -> TrainerResultManifest:
        if self.run_identity.terminal_status != "completed":
            return self
        if not self.reported_metrics:
            raise ValueError("completed_run_requires_metrics")
        if self.produced_checkpoint is None or not self.checkpoint_artifacts:
            raise ValueError("completed_run_requires_checkpoint")
        if self.produced_checkpoint.produced_checkpoint_artifact_digest not in {
            artifact.artifact_digest for artifact in self.checkpoint_artifacts
        }:
            raise ValueError("checkpoint_binding_mismatch")
        if self.provenance.runtime_receipts is None:
            raise ValueError("completed_run_requires_receipts")
        if not self.result_artifacts or not self.training_log_artifacts:
            raise ValueError("completed_run_requires_artifacts")
        if not self.non_contamination_evidence:
            raise ValueError("missing_non_contamination_evidence")
        for evidence in self.non_contamination_evidence:
            if (
                evidence.observed_train_split_digest != self.dataset.train_split_digest
                or evidence.observed_heldout_split_digest != self.dataset.heldout_split_digest
                or evidence.observed_train_cluster_key_digest
                != self.dataset.train_cluster_key_digest
                or evidence.observed_heldout_cluster_key_digest
                != self.dataset.heldout_cluster_key_digest
            ):
                raise ValueError("non_contamination_evidence_mismatch")
        return self

    def verify_manifest_digest(self) -> bool:
        """Return whether the stored content digest matches semantic manifest data."""
        return self.manifest_digest == compute_trainer_result_manifest_digest(self)


class TrainerResultValidation(ContractModel):
    """Typed outcome of checking an external trainer result against Track D.

    ``status="valid"`` means the record itself binds correctly (including for
    failed/cancelled runs); ``eligible_for_held_out_handoff`` is true only for
    a valid, completed run.
    """

    status: Literal["valid", "refused"]
    reason_codes: tuple[TrainerResultRefusalCode, ...] = ()
    manifest: TrainerResultManifest | None = None
    eligible_for_held_out_handoff: bool = False

    @model_validator(mode="after")
    def _validate_outcome_shape(self) -> TrainerResultValidation:
        if self.status == "valid" and (self.reason_codes or self.manifest is None):
            raise ValueError("valid result requires a manifest and no refusal reasons")
        if self.status == "refused" and (
            not self.reason_codes or self.manifest is not None or self.eligible_for_held_out_handoff
        ):
            raise ValueError("refused result requires reasons, no manifest, and no eligibility")
        return self


class FrozenHeldOutEvaluationPlan(ContractModel):
    """Data-only proposed handoff; it does not authorize or execute evaluation.

    This is a separate record from the manifest: the Harbor request/result for
    the held-out evaluation never lives inside ``TrainerResultManifest``.  The
    plan is self-authenticating: ``plan_digest`` canonically binds every other
    field, and its self-bound ``evaluation_set`` binds the exact tasks.
    """

    plan_kind: Literal["frozen-harbor-evaluation"] = "frozen-harbor-evaluation"
    execution_mode: Literal["proposed-handoff"] = "proposed-handoff"
    submission_permitted: Literal[False] = False
    source_result_manifest_digest: Digest
    trainer_bundle_digest: Digest
    trainer_plan_digest: Digest
    evaluation_set: TrainerEvaluationSetV1
    produced_checkpoint_artifact_digest: Digest
    heldout_split_digest: Digest
    heldout_cluster_key_digest: Digest
    cluster_separation_verified: Literal[True] = True
    plan_digest: Digest

    def verify_plan_digest(self) -> bool:
        """Return whether the plan content digest matches its semantic payload."""
        return self.plan_digest == compute_frozen_plan_digest(self)


def compute_frozen_plan_digest(plan: FrozenHeldOutEvaluationPlan) -> Digest:
    """Canonically digest a frozen handoff plan excluding its own digest field."""
    payload = plan.model_dump(mode="json", exclude={"plan_digest"})
    return canonical_digest(payload)


class RehydratedHeldOutEvaluationView(ContractModel):
    """Semantic rehydration of a frozen handoff, for downstream Harbor consumers.

    Pure data: every field is a digest or a frozen task coordinate.  The view
    digest canonically binds all other fields, so a downstream CAS-backed
    consumer can re-verify it without trusting the in-memory object.
    """

    source_result_manifest_digest: Digest
    trainer_bundle_digest: Digest
    trainer_plan_digest: Digest
    evaluation_set: TrainerEvaluationSetV1
    plan_digest: Digest
    produced_checkpoint_artifact_digest: Digest
    model_revision: str = Field(min_length=1)
    model_digest: Digest
    dataset_manifest_digest: Digest
    dataset_digest: Digest
    heldout_split_digest: Digest
    heldout_cluster_key_digest: Digest
    view_digest: Digest

    def verify_view_digest(self) -> bool:
        """Return whether the view digest matches the canonical semantic payload."""
        return self.view_digest == compute_rehydration_view_digest(self)


def compute_rehydration_view_digest(data: RehydratedHeldOutEvaluationView) -> Digest:
    """Canonically digest a rehydrated view excluding its own digest field."""
    payload = data.model_dump(mode="json", exclude={"view_digest"})
    return canonical_digest(payload)


def compute_trainer_result_manifest_digest(
    data: Mapping[str, Any] | TrainerResultManifest,
) -> Digest:
    """Compute the canonical digest of a result manifest excluding its digest field."""
    if isinstance(data, TrainerResultManifest):
        payload = data.model_dump(mode="json", exclude={"manifest_digest"})
    elif isinstance(data, Mapping):
        raw = dict(data)
        raw["manifest_digest"] = "sha256:" + "0" * 64
        manifest = TrainerResultManifest.model_validate(raw)
        payload = manifest.model_dump(mode="json", exclude={"manifest_digest"})
    else:
        raise TypeError(f"expected Mapping or TrainerResultManifest, got {type(data).__name__}")
    return canonical_digest(payload)


def create_trainer_result_manifest(**kwargs: Any) -> TrainerResultManifest:
    """Create a manifest and fill its deterministic content digest when omitted."""
    kwargs.setdefault("schema_version", TRAINER_RESULT_MANIFEST_SCHEMA)
    if not kwargs.get("manifest_digest"):
        kwargs["manifest_digest"] = compute_trainer_result_manifest_digest(kwargs)
    return TrainerResultManifest.model_validate(kwargs)


def _reason_code_from_validation_error(error: dict[str, Any]) -> TrainerResultRefusalCode:
    location = ".".join(str(part) for part in error.get("loc", ()))
    error_type = str(error.get("type", ""))
    message = str(error.get("msg", ""))
    if error_type == "extra_forbidden":
        return TrainerResultRefusalCode.UNKNOWN_FIELD
    if "uncertainty" in location:
        return TrainerResultRefusalCode.MISSING_UNCERTAINTY
    if "backend_name" in location and error_type == "literal_error":
        return TrainerResultRefusalCode.UNSUPPORTED_TRAINING_BACKEND
    if "adapter_contract" in location and error_type == "literal_error":
        return TrainerResultRefusalCode.UNSUPPORTED_ADAPTER_CONTRACT
    if "scope" in location and str(error.get("input")) in {"held-out", "heldout", "test"}:
        return TrainerResultRefusalCode.HELD_OUT_RESULT_IN_MANIFEST
    if "held_out_split_in_train_inputs" in message:
        return TrainerResultRefusalCode.HELD_OUT_SPLIT_IN_TRAIN_INPUTS
    if "cluster_key_overlap" in message:
        return TrainerResultRefusalCode.CLUSTER_KEY_OVERLAP
    if "non_contamination_evidence_mismatch" in message:
        return TrainerResultRefusalCode.NON_CONTAMINATION_EVIDENCE_MISMATCH
    if "checkpoint_binding_mismatch" in message:
        return TrainerResultRefusalCode.CHECKPOINT_BINDING_MISMATCH
    if "completed_run_requires_metrics" in message:
        return TrainerResultRefusalCode.COMPLETED_RUN_REQUIRES_METRICS
    if "completed_run_requires_checkpoint" in message:
        return TrainerResultRefusalCode.COMPLETED_RUN_REQUIRES_CHECKPOINT
    if "completed_run_requires_receipts" in message:
        return TrainerResultRefusalCode.COMPLETED_RUN_REQUIRES_RECEIPTS
    if "completed_run_requires_artifacts" in message:
        return TrainerResultRefusalCode.COMPLETED_RUN_REQUIRES_ARTIFACTS
    if "missing_non_contamination_evidence" in message:
        return TrainerResultRefusalCode.MISSING_NON_CONTAMINATION_EVIDENCE
    if "frozen_task_set_mismatch" in message:
        return TrainerResultRefusalCode.FROZEN_TASK_SET_MISMATCH
    if "artifact_ref" in location or "evidence_ref" in location:
        return TrainerResultRefusalCode.UNSAFE_ARTIFACT_REF
    if error_type == "missing":
        return TrainerResultRefusalCode.MISSING_REQUIRED_FIELD
    if "digest" in location:
        return TrainerResultRefusalCode.INVALID_DIGEST
    if "commit" in location:
        return TrainerResultRefusalCode.INVALID_COMMIT
    return TrainerResultRefusalCode.INVALID_MANIFEST


def _canonical_refusal_codes(
    reason_codes: Sequence[TrainerResultRefusalCode],
) -> tuple[TrainerResultRefusalCode, ...]:
    """Deduplicate refusal reasons in their public declaration order."""
    present = set(reason_codes)
    return tuple(code for code in TrainerResultRefusalCode if code in present)


def _refused(*reason_codes: TrainerResultRefusalCode) -> TrainerResultValidation:
    return TrainerResultValidation(
        status="refused",
        reason_codes=_canonical_refusal_codes(reason_codes),
        manifest=None,
        eligible_for_held_out_handoff=False,
    )


def _expected_projection_reasons(
    manifest: TrainerResultManifest, expected: ExpectedTrainerResultV1
) -> list[TrainerResultRefusalCode]:
    """Compare every identity field against the one strict Track D projection."""
    reasons: list[TrainerResultRefusalCode] = []
    if manifest.adapter_contract != expected.adapter_contract:
        reasons.append(TrainerResultRefusalCode.EXPECTED_PROJECTION_MISMATCH)
    if manifest.trainer_bundle_digest != expected.trainer_bundle_digest:
        reasons.append(TrainerResultRefusalCode.TRAINER_BUNDLE_DIGEST_MISMATCH)
    if manifest.trainer_plan_digest != expected.trainer_plan_digest:
        reasons.append(TrainerResultRefusalCode.TRAINER_PLAN_DIGEST_MISMATCH)
    backend_mismatch = (
        manifest.backend_identity.backend_name != expected.backend_name
        or manifest.backend_identity.backend_version != expected.backend_version
        or manifest.backend_identity.backend_source_commit != expected.backend_source_commit
        or manifest.backend_identity.backend_image_digest != expected.backend_image_digest
    )
    model_mismatch = (
        manifest.model.model_revision != expected.model_revision
        or manifest.model.model_digest != expected.model_digest
        or manifest.model.tokenizer_revision != expected.tokenizer_revision
        or manifest.model.tokenizer_digest != expected.tokenizer_digest
        or manifest.model.chat_template_revision != expected.chat_template_revision
        or manifest.model.chat_template_digest != expected.chat_template_digest
    )
    if (
        manifest.schema_version != expected.result_schema
        or manifest.source_authority_status != expected.source_authority_status
        or manifest.result_manifest_path != expected.result_manifest_path
    ):
        reasons.append(TrainerResultRefusalCode.EXPECTED_PROJECTION_MISMATCH)
    if backend_mismatch or model_mismatch:
        reasons.append(TrainerResultRefusalCode.EXPECTED_PROJECTION_MISMATCH)
    if manifest.input_model_checkpoint_digest != expected.input_model_checkpoint_digest:
        reasons.append(TrainerResultRefusalCode.INPUT_CHECKPOINT_MISMATCH)
    if manifest.effective_config_digest != expected.effective_config_digest:
        reasons.append(TrainerResultRefusalCode.EXPECTED_PROJECTION_MISMATCH)
    dataset_mismatch = (
        manifest.dataset.dataset_manifest_digest != expected.dataset_manifest_digest
        or (
            manifest.dataset.dataset_manifest_authority_digest
            != expected.dataset_manifest_authority_digest
        )
        or (
            manifest.dataset.dataset_manifest_verifier_digest
            != expected.dataset_manifest_verifier_digest
        )
        or (
            manifest.dataset.dataset_manifest_authority_level
            != expected.dataset_manifest_authority_level
        )
        or manifest.dataset.dataset_digest != expected.dataset_digest
        or manifest.dataset.train_split_digest != expected.train_split_digest
        or manifest.dataset.heldout_split_digest != expected.heldout_split_digest
        or manifest.dataset.train_cluster_key_digest != expected.train_cluster_key_digest
        or manifest.dataset.heldout_cluster_key_digest != expected.heldout_cluster_key_digest
    )
    if dataset_mismatch:
        reasons.append(TrainerResultRefusalCode.SPLIT_PARITY_MISMATCH)
    return reasons

def _reverified_dataset_authority_matches_expected(
    expected: ExpectedTrainerResultV1,
    authority: ArtifactAuthority | None,
    *,
    repo_root: Path | str | None,
) -> bool:
    """Re-read the expected dataset manifest through the b53 authority surface."""
    if authority is None:
        return False
    verification = reverify_authority(
        authority,
        expected_verifier_digest=expected.dataset_manifest_verifier_digest,
        repo_root=repo_root,
    )
    if isinstance(verification, AuthorityRefusal):
        return False
    _, verified_authority = verification
    return (
        verified_authority.authority_digest == expected.dataset_manifest_authority_digest
        and verified_authority.artifact.digest == expected.dataset_manifest_digest
        and verified_authority.level == expected.dataset_manifest_authority_level
    )


def _reverified_result_authority_matches_manifest(
    manifest: TrainerResultManifest,
    expected: ExpectedTrainerResultV1,
    authority: ArtifactAuthority | None,
    *,
    repo_root: Path | str | None,
) -> bool:
    """Require the exact authenticated result-manifest bytes to match the result."""
    if authority is None or authority.artifact.ref != expected.result_manifest_path:
        return False
    verification = reverify_authority(
        authority,
        expected_verifier_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        repo_root=repo_root,
    )
    if isinstance(verification, AuthorityRefusal):
        return False
    raw_bytes, _ = verification
    try:
        authoritative_manifest = TrainerResultManifest.model_validate(json.loads(raw_bytes))
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError):
        return False
    return authoritative_manifest.verify_manifest_digest() and authoritative_manifest == manifest


def _raw_completed_requirement_reasons(
    value: Mapping[str, Any],
) -> tuple[TrainerResultRefusalCode, ...]:
    """Collect independent completed-run omissions before strict parsing."""
    run_identity = value.get("run_identity")
    if not isinstance(run_identity, Mapping) or run_identity.get("terminal_status") != "completed":
        return ()

    reasons: list[TrainerResultRefusalCode] = []
    if not value.get("reported_metrics"):
        reasons.append(TrainerResultRefusalCode.COMPLETED_RUN_REQUIRES_METRICS)
    if not value.get("produced_checkpoint") or not value.get("checkpoint_artifacts"):
        reasons.append(TrainerResultRefusalCode.COMPLETED_RUN_REQUIRES_CHECKPOINT)
    provenance = value.get("provenance")
    if not isinstance(provenance, Mapping) or not provenance.get("runtime_receipts"):
        reasons.append(TrainerResultRefusalCode.COMPLETED_RUN_REQUIRES_RECEIPTS)
    if not value.get("result_artifacts") or not value.get("training_log_artifacts"):
        reasons.append(TrainerResultRefusalCode.COMPLETED_RUN_REQUIRES_ARTIFACTS)
    if not value.get("non_contamination_evidence"):
        reasons.append(TrainerResultRefusalCode.MISSING_NON_CONTAMINATION_EVIDENCE)
    return tuple(reasons)


def validate_trainer_result_manifest(
    value: Mapping[str, Any] | TrainerResultManifest,
    *,
    expected: ExpectedTrainerResultV1,
    dataset_manifest_authority: ArtifactAuthority | None = None,
    result_manifest_authority: ArtifactAuthority | None = None,
    authority_repo_root: Path | str | None = None,
) -> TrainerResultValidation:
    """Validate parity; require authenticated dataset and result bytes for handoff."""
    raw_reasons: tuple[TrainerResultRefusalCode, ...] = ()
    if isinstance(value, TrainerResultManifest):
        manifest = value
    elif not isinstance(value, Mapping):
        return _refused(TrainerResultRefusalCode.INVALID_MANIFEST)
    else:
        try:
            raw_reasons = _raw_completed_requirement_reasons(value)
            manifest = TrainerResultManifest.model_validate(value)
        except ValidationError as exc:
            return _refused(
                *raw_reasons,
                *(_reason_code_from_validation_error(error) for error in exc.errors()),
            )
        except (KeyError, TypeError, ValueError):
            return _refused(*raw_reasons, TrainerResultRefusalCode.INVALID_MANIFEST)

    reasons: list[TrainerResultRefusalCode] = list(raw_reasons)
    if not manifest.verify_manifest_digest():
        reasons.append(TrainerResultRefusalCode.MANIFEST_DIGEST_MISMATCH)
    reasons.extend(_expected_projection_reasons(manifest, expected))
    dataset_authority_reverified = _reverified_dataset_authority_matches_expected(
        expected,
        dataset_manifest_authority,
        repo_root=authority_repo_root,
    )
    result_authority_reverified = _reverified_result_authority_matches_manifest(
        manifest,
        expected,
        result_manifest_authority,
        repo_root=authority_repo_root,
    )
    if dataset_manifest_authority is not None and not dataset_authority_reverified:
        reasons.append(TrainerResultRefusalCode.AUTHORITY_NOT_REVERIFIED)
    if result_manifest_authority is not None and not result_authority_reverified:
        reasons.append(TrainerResultRefusalCode.AUTHORITY_NOT_REVERIFIED)
    if reasons:
        return _refused(*reasons)

    return TrainerResultValidation(
        status="valid",
        reason_codes=(),
        manifest=manifest,
        eligible_for_held_out_handoff=(
            manifest.run_identity.terminal_status == "completed"
            and dataset_authority_reverified
            and result_authority_reverified
        ),
    )


def render_frozen_held_out_evaluation_plan(
    manifest: Mapping[str, Any] | TrainerResultManifest,
    *,
    expected: ExpectedTrainerResultV1,
    frozen_tasks: Sequence[TrainerTaskIdentityV1 | Mapping[str, Any]],
    trusted_heldout_cluster_keys: Sequence[str],
    dataset_manifest_authority: ArtifactAuthority | None = None,
    result_manifest_authority: ArtifactAuthority | None = None,
    authority_repo_root: Path | str | None = None,
) -> FrozenHeldOutEvaluationPlan:
    """Render only an authority-reverified, exact Track D frozen handoff."""
    validation = validate_trainer_result_manifest(
        manifest,
        expected=expected,
        dataset_manifest_authority=dataset_manifest_authority,
        result_manifest_authority=result_manifest_authority,
        authority_repo_root=authority_repo_root,
    )
    reasons: list[TrainerResultRefusalCode] = list(validation.reason_codes)
    if validation.status == "valid" and not validation.eligible_for_held_out_handoff:
        reasons.append(
            TrainerResultRefusalCode.AUTHORITY_NOT_REVERIFIED
            if dataset_manifest_authority is None or result_manifest_authority is None
            else TrainerResultRefusalCode.TERMINAL_STATUS_NOT_COMPLETED
        )
    if reasons:
        raise TrainerResultManifestRefused(reasons)

    result = validation.manifest
    assert result is not None  # narrowed by TrainerResultValidation invariant above
    evaluation_set = expected.evaluation_set
    tasks = tuple(
        task
        if isinstance(task, TrainerTaskIdentityV1)
        else TrainerTaskIdentityV1.model_validate(task)
        for task in frozen_tasks
    )
    if not tasks:
        raise TrainerResultManifestRefused((TrainerResultRefusalCode.INVALID_MANIFEST,))

    canonical_cluster_keys = tuple(sorted(set(trusted_heldout_cluster_keys)))
    if (
        not canonical_cluster_keys
        or any(not key.strip() for key in canonical_cluster_keys)
        or compute_cluster_key_digest(canonical_cluster_keys)
        != expected.heldout_cluster_key_digest
    ):
        reasons.append(TrainerResultRefusalCode.FROZEN_TASK_CLUSTER_MISMATCH)
    if tasks != evaluation_set.tasks:
        reasons.append(TrainerResultRefusalCode.FROZEN_TASK_SET_MISMATCH)
    if reasons:
        raise TrainerResultManifestRefused(reasons)

    assert result.produced_checkpoint is not None  # completed-run invariant
    seeded_plan = FrozenHeldOutEvaluationPlan(
        source_result_manifest_digest=result.manifest_digest,
        trainer_bundle_digest=result.trainer_bundle_digest,
        trainer_plan_digest=result.trainer_plan_digest,
        evaluation_set=evaluation_set,
        produced_checkpoint_artifact_digest=(
            result.produced_checkpoint.produced_checkpoint_artifact_digest
        ),
        heldout_split_digest=result.dataset.heldout_split_digest,
        heldout_cluster_key_digest=result.dataset.heldout_cluster_key_digest,
        plan_digest="sha256:" + "0" * 64,
    )
    payload = seeded_plan.model_dump(mode="json")
    payload["plan_digest"] = compute_frozen_plan_digest(seeded_plan)
    return FrozenHeldOutEvaluationPlan.model_validate(payload)


def rehydrate_frozen_held_out_evaluation_plan(
    plan: FrozenHeldOutEvaluationPlan,
    *,
    manifest: TrainerResultManifest,
    expected: ExpectedTrainerResultV1,
    dataset_manifest_authority: ArtifactAuthority | None = None,
    result_manifest_authority: ArtifactAuthority | None = None,
    authority_repo_root: Path | str | None = None,
) -> RehydratedHeldOutEvaluationView:
    """Rehydrate only after plan, manifest, expectation, and authority re-verification."""
    validation = validate_trainer_result_manifest(
        manifest,
        expected=expected,
        dataset_manifest_authority=dataset_manifest_authority,
        result_manifest_authority=result_manifest_authority,
        authority_repo_root=authority_repo_root,
    )
    refusals: list[TrainerResultRefusalCode] = list(validation.reason_codes)
    if validation.status == "valid" and not validation.eligible_for_held_out_handoff:
        refusals.append(
            TrainerResultRefusalCode.AUTHORITY_NOT_REVERIFIED
            if dataset_manifest_authority is None or result_manifest_authority is None
            else TrainerResultRefusalCode.TERMINAL_STATUS_NOT_COMPLETED
        )
    if not plan.verify_plan_digest():
        refusals.append(TrainerResultRefusalCode.PLAN_DIGEST_MISMATCH)
    if plan.source_result_manifest_digest != manifest.manifest_digest:
        refusals.append(TrainerResultRefusalCode.SOURCE_MANIFEST_MISMATCH)
    if plan.trainer_bundle_digest != manifest.trainer_bundle_digest:
        refusals.append(TrainerResultRefusalCode.TRAINER_BUNDLE_DIGEST_MISMATCH)
    if plan.trainer_plan_digest != manifest.trainer_plan_digest:
        refusals.append(TrainerResultRefusalCode.TRAINER_PLAN_DIGEST_MISMATCH)
    produced = (
        manifest.produced_checkpoint.produced_checkpoint_artifact_digest
        if manifest.produced_checkpoint is not None
        else None
    )
    if plan.produced_checkpoint_artifact_digest != produced:
        refusals.append(TrainerResultRefusalCode.CHECKPOINT_BINDING_MISMATCH)
    if plan.heldout_split_digest != manifest.dataset.heldout_split_digest:
        refusals.append(TrainerResultRefusalCode.SPLIT_PARITY_MISMATCH)
    if plan.heldout_cluster_key_digest != manifest.dataset.heldout_cluster_key_digest:
        refusals.append(TrainerResultRefusalCode.SPLIT_PARITY_MISMATCH)
    if manifest.run_identity.terminal_status != "completed":
        refusals.append(TrainerResultRefusalCode.TERMINAL_STATUS_NOT_COMPLETED)
    if plan.evaluation_set != expected.evaluation_set:
        refusals.append(TrainerResultRefusalCode.FROZEN_TASK_SET_MISMATCH)
    if refusals:
        raise TrainerResultManifestRefused(refusals)

    seeded = RehydratedHeldOutEvaluationView(
        source_result_manifest_digest=plan.source_result_manifest_digest,
        trainer_bundle_digest=plan.trainer_bundle_digest,
        trainer_plan_digest=plan.trainer_plan_digest,
        evaluation_set=plan.evaluation_set,
        plan_digest=plan.plan_digest,
        produced_checkpoint_artifact_digest=plan.produced_checkpoint_artifact_digest,
        model_revision=manifest.model.model_revision,
        model_digest=manifest.model.model_digest,
        dataset_manifest_digest=manifest.dataset.dataset_manifest_digest,
        dataset_digest=manifest.dataset.dataset_digest,
        heldout_split_digest=plan.heldout_split_digest,
        heldout_cluster_key_digest=plan.heldout_cluster_key_digest,
        view_digest="sha256:" + "0" * 64,
    )
    payload = seeded.model_dump(mode="json")
    payload["view_digest"] = compute_rehydration_view_digest(seeded)
    return RehydratedHeldOutEvaluationView(**payload)
