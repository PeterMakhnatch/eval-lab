"""Offline, immutable composition of the governed improvement-plan stages."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from evallab.artifact_authority import (
    VERIFIER_IMPLEMENTATION_DIGEST,
    ArtifactAuthority,
    AuthorityRefusal,
    reverify_authority,
)
from evallab.benchmark_program_contracts import (
    canonical_bytes,
    compute_prefixed_sha256,
    validate_safe_relative_path,
)
from evallab.curriculum_candidates import SynthesisResult, synthesize_curriculum_candidates
from evallab.immutable_directory import staged_immutable_directory
from evallab.interpretation.capability_deficits import (
    CapabilityDeficitArtifactReceipt,
    CapabilityDeficitOutputExpectation,
    reverify_capability_deficit_artifact,
)
from evallab.paired_intervention import (
    ExtraInstructionDelta,
    PairedAnalysisGate,
    PairedArmCandidate,
    PairedInterventionPlan,
    PairedInterventionRefusal,
    RetryReplacementPolicy,
    plan_paired_intervention,
)
from evallab.paired_outcome import (
    PairedInterventionOutcomeV1,
    PairedOutcomeDecisionRuleV1,
    PairedOutcomeRefusal,
    PairedTrialObservationV1,
    analyze_paired_outcomes,
)
from evallab.queue import PolicyGate
from evallab.schemas import ContractModel, Digest, StandingApprovalsPolicy
from evallab.trainer_bundle import (
    RenderedTrainerPlanV1,
    TrainerBackendIdentityV1,
    TrainerBundleRefusal,
    TrainerBundleV1,
    TrainerTaskIdentityV1,
    rehydrate_rendered_trainer_plan,
    render_trl_plan,
    validate_trainer_bundle,
)
from evallab.training_export import (
    NormalizedTrainingEvidence,
    TrainingDatasetManifestV1,
    export_training_dataset,
)
from evallab.training_result import (
    FrozenHeldOutEvaluationPlan,
    TrainerResultManifest,
    TrainerResultManifestRefused,
    TrainerResultValidation,
    render_frozen_held_out_evaluation_plan,
    validate_trainer_result_manifest,
)

_PLAN_FILENAME = "improvement-plan.json"
_TRAINER_INPUT_DIRECTORY = "trainer-input"


class ImprovementPlanRefusalCode(StrEnum):
    """Closed fail-closed reasons owned by the composition boundary."""

    INPUT_MANIFEST_MISMATCH = "input_manifest_mismatch"
    EXISTING_OUTPUT_INVALID = "existing_output_invalid"
    OUTPUT_INVENTORY_MISMATCH = "output_inventory_mismatch"
    OUTPUT_ARTIFACT_TAMPERED = "output_artifact_tampered"
    OUTPUT_PUBLICATION_REFUSED = "output_publication_refused"
    TRAINING_SET_EMPTY = "training_set_empty"
    TRAINING_EXPORT_REFUSED = "training_export_refused"
    DEFICIT_EXPECTATION_MISSING = "deficit_expectation_missing"
    DEFICIT_AUTHORITY_UNVERIFIED = "deficit_authority_unverified"
    CURRICULUM_EMPTY = "curriculum_empty"
    CURRICULUM_REFUSED = "curriculum_refused"
    CURRICULUM_QUARANTINE_BROKEN = "curriculum_quarantine_broken"
    CURRICULUM_PAIR_MISMATCH = "curriculum_pair_mismatch"
    PAIRED_INTERVENTION_REFUSED = "paired_intervention_refused"
    TRAINING_MODE_UNSUPPORTED = "training_mode_unsupported"
    DATASET_BINDING_MISMATCH = "dataset_binding_mismatch"
    CHECKPOINT_AUTHORITY_UNVERIFIED = "checkpoint_authority_unverified"
    CHECKPOINT_BINDING_MISMATCH = "checkpoint_binding_mismatch"
    TRAINER_BUNDLE_REFUSED = "trainer_bundle_refused"
    PAIRED_OUTCOME_REFUSED = "paired_outcome_refused"
    RESULT_REFUSED = "result_refused"


class ImprovementPlanRefusal(ValueError):
    def __init__(self, reason_code: ImprovementPlanRefusalCode, detail: str) -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code.value}: {detail}")


class ImprovementStage(StrEnum):
    TRAINING_EXPORT = "training_export"
    DEFICIT_REANCHOR = "deficit_reanchor"
    CURRICULUM_SYNTHESIS = "curriculum_synthesis"
    PAIRED_INTERVENTION = "paired_intervention"
    TRAINER_BUNDLE = "trainer_bundle"
    TRAINER_PLAN = "trainer_plan"
    PAIRED_OUTCOME = "paired_outcome"
    TRAINER_RESULT = "trainer_result"
    HELD_OUT_HANDOFF = "held_out_handoff"


class StageStatus(StrEnum):
    PASSED = "passed"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"


class StageReason(StrEnum):
    DATASET_EXPORTED = "dataset_exported"
    DEFICITS_REANCHORED = "deficits_reanchored"
    QUARANTINED_PRIORITIES_SYNTHESIZED = "quarantined_priorities_synthesized"
    APPROVAL_PRESERVING_PAIRS_PLANNED = "approval_preserving_pairs_planned"
    TRAINER_BUNDLE_VALIDATED = "trainer_bundle_validated"
    EXTERNAL_SFT_PLAN_READY = "external_sft_plan_ready"
    PAIRED_OUTCOME_ANALYZED = "paired_outcome_analyzed"
    PAIRED_OUTCOME_INPUT_UNAVAILABLE = "paired_outcome_input_unavailable"
    EXTERNAL_RESULT_REQUIRED = "external_result_required"
    RESULT_AUTHORITY_VALIDATED = "result_authority_validated"
    HELD_OUT_RESULT_REQUIRED = "held_out_result_required"
    PROPOSED_HANDOFF_RENDERED = "proposed_handoff_rendered"


class _FrozenContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TrainerResultAttemptV1(_FrozenContract):
    manifest: TrainerResultManifest
    result_manifest_authority: ArtifactAuthority
    frozen_tasks: tuple[TrainerTaskIdentityV1, ...] = Field(min_length=1)
    trusted_heldout_cluster_keys: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def canonical_cluster_keys(self) -> TrainerResultAttemptV1:
        if self.trusted_heldout_cluster_keys != tuple(
            sorted(set(self.trusted_heldout_cluster_keys))
        ):
            raise ValueError("trusted held-out cluster keys must be canonical")
        return self


class PairedOutcomeInputV1(_FrozenContract):
    artifact_id: str
    observations: tuple[PairedTrialObservationV1, ...]
    decision_rule: PairedOutcomeDecisionRuleV1


class ImprovementPlanInputManifestV1(_FrozenContract):
    schema_version: Literal["improvement-plan-input/v1"] = "improvement-plan-input/v1"
    output_path: str
    training_sources: tuple[NormalizedTrainingEvidence, ...]
    deficit_receipts: tuple[CapabilityDeficitArtifactReceipt, ...] = Field(min_length=1)
    trusted_parent_outputs: dict[Digest, CapabilityDeficitOutputExpectation]
    curriculum_seed: int = Field(ge=0)
    curriculum_budget: int | None = Field(default=None, ge=1)
    paired_plan_id: str
    paired_randomization_seed: int
    paired_candidates: tuple[PairedArmCandidate, ...] = Field(min_length=2)
    paired_delta: ExtraInstructionDelta
    paired_retry_policy: RetryReplacementPolicy
    paired_analysis_gate: PairedAnalysisGate
    standing_approvals: StandingApprovalsPolicy
    spent_today_usd: float = Field(ge=0)
    trainer_bundle: TrainerBundleV1
    trainer_backend_identity: TrainerBackendIdentityV1
    checkpoint_authority: ArtifactAuthority
    authority_store_root: Path | None = None
    trainer_result: TrainerResultAttemptV1 | None = None
    paired_outcome: PairedOutcomeInputV1 | None = None

    @model_validator(mode="after")
    def canonical_input(self) -> ImprovementPlanInputManifestV1:
        validate_safe_relative_path(self.output_path)
        if self.output_path in {"", "."}:
            raise ValueError("output_path must name a child directory")
        receipt_digests = tuple(
            receipt.artifact.content_digest for receipt in self.deficit_receipts
        )
        if receipt_digests != tuple(sorted(set(receipt_digests))):
            raise ValueError("deficit receipts must be unique and canonically ordered")
        if set(self.trusted_parent_outputs) != set(receipt_digests):
            raise ValueError("trusted parent outputs must exactly cover deficit receipts")
        return self


class ImprovementStageRecordV1(_FrozenContract):
    stage: ImprovementStage
    status: StageStatus
    reason_code: StageReason
    authority_level: Literal[
        "bytes-verified",
        "structural-self-consistent",
        "not-applicable",
        "unavailable",
    ]
    input_digest: Digest
    output_digest: Digest | None
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)

    @model_validator(mode="after")
    def evidence_shape(self) -> ImprovementStageRecordV1:
        if self.numerator > self.denominator:
            raise ValueError("stage numerator cannot exceed denominator")
        if self.status == StageStatus.PASSED and self.output_digest is None:
            raise ValueError("passed stage requires an output digest")
        if self.status == StageStatus.UNAVAILABLE and (
            self.output_digest is not None or self.numerator != 0
        ):
            raise ValueError("unavailable stage cannot claim output evidence")
        return self


class ImprovementArtifactV1(_FrozenContract):
    path: str
    digest: Digest

    @model_validator(mode="after")
    def safe_path(self) -> ImprovementArtifactV1:
        validate_safe_relative_path(self.path)
        return self


class ImprovementPlanBundleV1(_FrozenContract):
    schema_version: Literal["improvement-plan-bundle/v1"] = "improvement-plan-bundle/v1"
    bundle_digest: Digest
    input_manifest_digest: Digest
    ready_for_external_sft: Literal[True] = True
    ready_for_rl: Literal[False] = False
    stages: tuple[ImprovementStageRecordV1, ...]
    artifacts: tuple[ImprovementArtifactV1, ...]
    dataset_manifest: TrainingDatasetManifestV1
    curriculum: SynthesisResult
    paired_plan: PairedInterventionPlan
    trainer_plan: RenderedTrainerPlanV1
    result_validation: TrainerResultValidation | None = None
    held_out_handoff: FrozenHeldOutEvaluationPlan | None = None
    paired_outcome: PairedInterventionOutcomeV1 | None = None

    @model_validator(mode="after")
    def canonical_bundle(self) -> ImprovementPlanBundleV1:
        expected_stages = tuple(ImprovementStage)
        if tuple(record.stage for record in self.stages) != expected_stages:
            raise ValueError("improvement stages must be complete and canonically ordered")
        paths = tuple(artifact.path for artifact in self.artifacts)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("artifact inventory must be unique and canonically ordered")
        outcome_stage = next(
            record
            for record in self.stages
            if record.stage == ImprovementStage.PAIRED_OUTCOME
        )
        if self.paired_outcome is None:
            if outcome_stage.status != StageStatus.UNAVAILABLE:
                raise ValueError("missing paired outcome must be unavailable")
        elif (
            outcome_stage.status != StageStatus.PASSED
            or self.paired_outcome.plan != self.paired_plan
        ):
            raise ValueError("paired outcome must bind the exact paired plan")
        if (self.result_validation is None) != (self.held_out_handoff is None):
            raise ValueError("result validation and held-out handoff must be present together")
        completed_result = self.result_validation is not None
        terminal_stages = (
            ImprovementStage.TRAINER_RESULT,
            ImprovementStage.HELD_OUT_HANDOFF,
        )
        for terminal_stage in terminal_stages:
            record = next(
                item for item in self.stages if item.stage == terminal_stage
            )
            expected_status = (
                StageStatus.PASSED if completed_result else StageStatus.UNAVAILABLE
            )
            if record.status != expected_status:
                raise ValueError("terminal stage status does not match result evidence")
        body = self.model_dump(mode="json", exclude={"bundle_digest"})
        if self.bundle_digest != compute_prefixed_sha256(body):
            raise ValueError("improvement plan bundle digest mismatch")
        return self


def _refuse(reason: ImprovementPlanRefusalCode, detail: str) -> None:
    raise ImprovementPlanRefusal(reason, detail)


def _input_digest(value: ImprovementPlanInputManifestV1) -> str:
    return compute_prefixed_sha256(value.model_dump(mode="json"))


def _json_bytes(value: ContractModel) -> bytes:
    return canonical_bytes(value.model_dump(mode="json")) + b"\n"


def _write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _digest_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _relative_inventory(root: Path, *, exclude: frozenset[str] = frozenset()) -> tuple[str, ...]:
    paths: list[str] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"immutable bundle contains a symlink: {path}")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if relative not in exclude:
                paths.append(relative)
        elif not path.is_dir():
            raise ValueError(f"immutable bundle contains a special file: {path}")
    return tuple(sorted(paths))


def _artifact_records(root: Path) -> tuple[ImprovementArtifactV1, ...]:
    return tuple(
        ImprovementArtifactV1(path=path, digest=_digest_bytes((root / path).read_bytes()))
        for path in _relative_inventory(root, exclude=frozenset({_PLAN_FILENAME}))
    )


def _stages(
    manifest: ImprovementPlanInputManifestV1,
    *,
    dataset: TrainingDatasetManifestV1,
    curriculum: SynthesisResult,
    paired_plan: PairedInterventionPlan,
    trainer_plan: RenderedTrainerPlanV1,
    paired_outcome: PairedInterventionOutcomeV1 | None,
    result_validation: TrainerResultValidation | None,
    handoff: FrozenHeldOutEvaluationPlan | None,
) -> tuple[ImprovementStageRecordV1, ...]:
    result_present = result_validation is not None
    outcome_present = paired_outcome is not None
    pair_count = len(curriculum.contrast_pairs)
    return (
        ImprovementStageRecordV1(
            stage=ImprovementStage.TRAINING_EXPORT,
            status=StageStatus.PASSED,
            reason_code=StageReason.DATASET_EXPORTED,
            authority_level="bytes-verified",
            input_digest=compute_prefixed_sha256(
                [source.model_dump(mode="json") for source in manifest.training_sources]
            ),
            output_digest=dataset.manifest_digest,
            numerator=len(dataset.source_refs),
            denominator=len(manifest.training_sources),
        ),
        ImprovementStageRecordV1(
            stage=ImprovementStage.DEFICIT_REANCHOR,
            status=StageStatus.PASSED,
            reason_code=StageReason.DEFICITS_REANCHORED,
            authority_level="bytes-verified",
            input_digest=compute_prefixed_sha256(
                {
                    "receipts": [
                        receipt.model_dump(mode="json")
                        for receipt in manifest.deficit_receipts
                    ],
                    "expectations": {
                        key: value.model_dump(mode="json")
                        for key, value in manifest.trusted_parent_outputs.items()
                    },
                }
            ),
            output_digest=compute_prefixed_sha256(
                [receipt.artifact.content_digest for receipt in manifest.deficit_receipts]
            ),
            numerator=len(manifest.deficit_receipts),
            denominator=len(manifest.deficit_receipts),
        ),
        ImprovementStageRecordV1(
            stage=ImprovementStage.CURRICULUM_SYNTHESIS,
            status=StageStatus.PASSED,
            reason_code=StageReason.QUARANTINED_PRIORITIES_SYNTHESIZED,
            authority_level="structural-self-consistent",
            input_digest=compute_prefixed_sha256(
                [receipt.artifact.content_digest for receipt in manifest.deficit_receipts]
            ),
            output_digest=curriculum.content_digest,
            numerator=pair_count,
            denominator=pair_count + len(curriculum.refusals),
        ),
        ImprovementStageRecordV1(
            stage=ImprovementStage.PAIRED_INTERVENTION,
            status=StageStatus.PASSED,
            reason_code=StageReason.APPROVAL_PRESERVING_PAIRS_PLANNED,
            authority_level="structural-self-consistent",
            input_digest=compute_prefixed_sha256(
                [pair.twin_pair_id for pair in curriculum.contrast_pairs]
            ),
            output_digest=paired_plan.plan_digest,
            numerator=len({entry.pair_id for entry in paired_plan.schedule}),
            denominator=pair_count,
        ),
        ImprovementStageRecordV1(
            stage=ImprovementStage.TRAINER_BUNDLE,
            status=StageStatus.PASSED,
            reason_code=StageReason.TRAINER_BUNDLE_VALIDATED,
            authority_level="bytes-verified",
            input_digest=compute_prefixed_sha256(
                manifest.trainer_bundle.model_dump(mode="json")
            ),
            output_digest=trainer_plan.expected_result.trainer_bundle_digest,
            numerator=1,
            denominator=1,
        ),
        ImprovementStageRecordV1(
            stage=ImprovementStage.TRAINER_PLAN,
            status=StageStatus.PASSED,
            reason_code=StageReason.EXTERNAL_SFT_PLAN_READY,
            authority_level="structural-self-consistent",
            input_digest=compute_prefixed_sha256(
                {
                    "bundle": trainer_plan.expected_result.trainer_bundle_digest,
                    "backend": manifest.trainer_backend_identity.model_dump(mode="json"),
                }
            ),
            output_digest=trainer_plan.expected_result.trainer_plan_digest,
            numerator=1,
            denominator=1,
        ),
        ImprovementStageRecordV1(
            stage=ImprovementStage.PAIRED_OUTCOME,
            status=(
                StageStatus.PASSED if outcome_present else StageStatus.UNAVAILABLE
            ),
            reason_code=(
                StageReason.PAIRED_OUTCOME_ANALYZED
                if outcome_present
                else StageReason.PAIRED_OUTCOME_INPUT_UNAVAILABLE
            ),
            authority_level=(
                "structural-self-consistent" if outcome_present else "unavailable"
            ),
            input_digest=compute_prefixed_sha256(
                manifest.paired_outcome.model_dump(mode="json")
                if manifest.paired_outcome is not None
                else {"plan_digest": paired_plan.plan_digest}
            ),
            output_digest=(
                paired_outcome.outcome_digest if paired_outcome is not None else None
            ),
            numerator=(
                paired_outcome.numerator_directional_improvement_pairs
                if paired_outcome is not None
                else 0
            ),
            denominator=(
                paired_outcome.denominator_eligible_pairs
                if paired_outcome is not None
                else pair_count
            ),
        ),
        ImprovementStageRecordV1(
            stage=ImprovementStage.TRAINER_RESULT,
            status=StageStatus.PASSED if result_present else StageStatus.UNAVAILABLE,
            reason_code=(
                StageReason.RESULT_AUTHORITY_VALIDATED
                if result_present
                else StageReason.EXTERNAL_RESULT_REQUIRED
            ),
            authority_level="bytes-verified" if result_present else "unavailable",
            input_digest=trainer_plan.expected_result.trainer_plan_digest,
            output_digest=(
                result_validation.manifest.manifest_digest
                if result_validation is not None
                and result_validation.manifest is not None
                else None
            ),
            numerator=1 if result_present else 0,
            denominator=1,
        ),
        ImprovementStageRecordV1(
            stage=ImprovementStage.HELD_OUT_HANDOFF,
            status=StageStatus.PASSED if handoff is not None else StageStatus.UNAVAILABLE,
            reason_code=(
                StageReason.PROPOSED_HANDOFF_RENDERED
                if handoff is not None
                else StageReason.HELD_OUT_RESULT_REQUIRED
            ),
            authority_level=(
                "structural-self-consistent" if handoff is not None else "unavailable"
            ),
            input_digest=compute_prefixed_sha256(
                {
                    "result": (
                        result_validation.manifest.manifest_digest
                        if result_validation is not None
                        and result_validation.manifest is not None
                        else None
                    ),
                    "evaluation_set": (
                        trainer_plan.expected_result.evaluation_set.task_set_digest
                    ),
                }
            ),
            output_digest=handoff.plan_digest if handoff is not None else None,
            numerator=1 if handoff is not None else 0,
            denominator=1,
        ),
    )


def _rehydrate_existing(
    destination: Path,
    manifest: ImprovementPlanInputManifestV1,
    *,
    workspace_root: Path,
) -> ImprovementPlanBundleV1:
    if destination.is_symlink() or not destination.is_dir():
        _refuse(
            ImprovementPlanRefusalCode.EXISTING_OUTPUT_INVALID,
            "existing output must be a regular directory, not a symlink",
        )
    try:
        bundle = ImprovementPlanBundleV1.model_validate_json(
            (destination / _PLAN_FILENAME).read_bytes()
        )
    except (OSError, ValueError) as exc:
        _refuse(ImprovementPlanRefusalCode.EXISTING_OUTPUT_INVALID, str(exc))
    if bundle.input_manifest_digest != _input_digest(manifest):
        _refuse(
            ImprovementPlanRefusalCode.INPUT_MANIFEST_MISMATCH,
            "existing immutable output belongs to a different input manifest",
        )
    try:
        actual = _relative_inventory(destination, exclude=frozenset({_PLAN_FILENAME}))
    except ValueError as exc:
        _refuse(ImprovementPlanRefusalCode.EXISTING_OUTPUT_INVALID, str(exc))
    expected = tuple(artifact.path for artifact in bundle.artifacts)
    if actual != expected:
        _refuse(
            ImprovementPlanRefusalCode.OUTPUT_INVENTORY_MISMATCH,
            "existing immutable output inventory differs from its bundle manifest",
        )
    for artifact in bundle.artifacts:
        if _digest_bytes((destination / artifact.path).read_bytes()) != artifact.digest:
            _refuse(
                ImprovementPlanRefusalCode.OUTPUT_ARTIFACT_TAMPERED,
                f"artifact digest mismatch: {artifact.path}",
            )
    trainer_root = destination / _TRAINER_INPUT_DIRECTORY
    try:
        rehydrated = rehydrate_rendered_trainer_plan(
            bundle.trainer_plan,
            bundle=manifest.trainer_bundle,
            root=trainer_root,
            backend_identity=manifest.trainer_backend_identity,
            store_root=manifest.authority_store_root,
        )
    except TrainerBundleRefusal as exc:
        _refuse(
            ImprovementPlanRefusalCode.OUTPUT_ARTIFACT_TAMPERED,
            exc.reason_code,
        )
    if rehydrated != bundle.trainer_plan:
        _refuse(
            ImprovementPlanRefusalCode.OUTPUT_ARTIFACT_TAMPERED,
            "trainer plan failed full deterministic rehydration",
        )
    shadow_parent = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.rehydrate-", dir=destination.parent)
    )
    shadow_destination = shadow_parent / "bundle"
    try:
        _run_improvement_plan(
            manifest,
            workspace_root=workspace_root,
            destination_override=shadow_destination,
        )
        expected_inventory = _relative_inventory(destination)
        if _relative_inventory(shadow_destination) != expected_inventory or any(
            (destination / path).read_bytes() != (shadow_destination / path).read_bytes()
            for path in expected_inventory
        ):
            _refuse(
                ImprovementPlanRefusalCode.OUTPUT_ARTIFACT_TAMPERED,
                "existing output differs from a deterministic full re-render",
            )
    finally:
        shutil.rmtree(shadow_parent, ignore_errors=True)
    return bundle


def _checkpoint_bytes(
    manifest: ImprovementPlanInputManifestV1,
    *,
    workspace_root: Path,
) -> bytes:
    checkpoint = manifest.trainer_bundle.model_identity.checkpoint
    if checkpoint is None:
        _refuse(
            ImprovementPlanRefusalCode.TRAINING_MODE_UNSUPPORTED,
            "Track H v1 requires a local checkpoint-bound TRL SFT bundle",
        )
    if manifest.checkpoint_authority.level != "bytes-verified":
        _refuse(
            ImprovementPlanRefusalCode.CHECKPOINT_AUTHORITY_UNVERIFIED,
            "checkpoint input authority must already be bytes-verified",
        )
    reverified = reverify_authority(
        manifest.checkpoint_authority,
        expected_verifier_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        repo_root=workspace_root,
        store_root=manifest.authority_store_root,
    )
    if isinstance(reverified, AuthorityRefusal):
        _refuse(
            ImprovementPlanRefusalCode.CHECKPOINT_AUTHORITY_UNVERIFIED,
            reverified.reason,
        )
    payload, authority = reverified
    if (
        authority.level != "bytes-verified"
        or authority.artifact.digest != checkpoint.content_digest
    ):
        _refuse(
            ImprovementPlanRefusalCode.CHECKPOINT_BINDING_MISMATCH,
            "checkpoint authority does not bind the trainer bundle checkpoint",
        )
    return payload


def _validate_destination_chain(root: Path, relative_path: str) -> Path:
    relative = validate_safe_relative_path(relative_path)
    destination = root / relative
    parts = Path(relative).parts
    cursor = root
    for index, part in enumerate(parts):
        cursor = cursor / part
        if not os.path.lexists(cursor):
            break
        mode = os.lstat(cursor).st_mode
        if stat.S_ISLNK(mode):
            _refuse(
                ImprovementPlanRefusalCode.EXISTING_OUTPUT_INVALID,
                f"output path traverses a symlink: {cursor}",
            )
        if index < len(parts) - 1 and not stat.S_ISDIR(mode):
            _refuse(
                ImprovementPlanRefusalCode.EXISTING_OUTPUT_INVALID,
                f"output ancestor is not a directory: {cursor}",
            )
    return destination


def _run_improvement_plan(
    manifest: ImprovementPlanInputManifestV1,
    *,
    workspace_root: Path,
    destination_override: Path | None = None,
) -> ImprovementPlanBundleV1:
    root = workspace_root.resolve(strict=True)
    destination = (
        destination_override
        if destination_override is not None
        else _validate_destination_chain(root, manifest.output_path)
    )
    if os.path.lexists(destination):
        if destination_override is None:
            return _rehydrate_existing(destination, manifest, workspace_root=root)
        _refuse(
            ImprovementPlanRefusalCode.OUTPUT_PUBLICATION_REFUSED,
            "internal render destination already exists",
        )
    if not manifest.training_sources:
        _refuse(
            ImprovementPlanRefusalCode.TRAINING_SET_EMPTY,
            "Track H requires at least one authority-admitted training source",
        )
    trainer = manifest.trainer_bundle
    if (
        trainer.objective.kind != "sft"
        or trainer.selected_representation != "prompt_response_sft"
        or trainer.rendering.representation != "prompt_response_sft"
        or manifest.trainer_backend_identity.name != "generic-trl"
    ):
        _refuse(
            ImprovementPlanRefusalCode.TRAINING_MODE_UNSUPPORTED,
            "Track H v1 admits only prompt_response_sft with the generic TRL backend",
        )

    checkpoint_payload = _checkpoint_bytes(manifest, workspace_root=root)
    expected_inventory: list[str] = []
    with staged_immutable_directory(
        destination,
        expected_inventory=expected_inventory,
    ) as staged:
        trainer_root = staged / _TRAINER_INPUT_DIRECTORY
        try:
            exported = export_training_dataset(
                manifest.training_sources,
                trainer_root,
                representations=("prompt_response_sft",),
            )
        except (OSError, ValueError) as exc:
            _refuse(ImprovementPlanRefusalCode.TRAINING_EXPORT_REFUSED, str(exc))
        if exported.manifest != trainer.dataset:
            _refuse(
                ImprovementPlanRefusalCode.DATASET_BINDING_MISMATCH,
                "trainer bundle dataset differs from the exact Track A export",
            )
        checkpoint = trainer.model_identity.checkpoint
        assert checkpoint is not None
        checkpoint_path = validate_safe_relative_path(checkpoint.path)
        _write_new(trainer_root / checkpoint_path, checkpoint_payload)

        for receipt in manifest.deficit_receipts:
            expectation = manifest.trusted_parent_outputs.get(
                receipt.artifact.content_digest
            )
            if expectation is None:
                _refuse(
                    ImprovementPlanRefusalCode.DEFICIT_EXPECTATION_MISSING,
                    receipt.artifact.content_digest,
                )
            try:
                reanchored = reverify_capability_deficit_artifact(
                    receipt,
                    expected_output=expectation,
                    authority_repo_root=root,
                    authority_store_root=manifest.authority_store_root,
                )
            except (OSError, TypeError, ValueError):
                reanchored = False
            if not reanchored:
                _refuse(
                    ImprovementPlanRefusalCode.DEFICIT_AUTHORITY_UNVERIFIED,
                    receipt.artifact.content_digest,
                )

        try:
            curriculum = synthesize_curriculum_candidates(
                manifest.deficit_receipts,
                trusted_parent_outputs=manifest.trusted_parent_outputs,
                seed=manifest.curriculum_seed,
                budget=manifest.curriculum_budget,
                authority_repo_root=root,
                authority_store_root=manifest.authority_store_root,
            )
        except (OSError, TypeError, ValueError) as exc:
            _refuse(ImprovementPlanRefusalCode.CURRICULUM_REFUSED, str(exc))
        if not curriculum.candidates or not curriculum.contrast_pairs:
            _refuse(
                ImprovementPlanRefusalCode.CURRICULUM_EMPTY,
                "no independently re-anchored deficit produced a contrast pair",
            )
        if (
            curriculum.status != "quarantined"
            or curriculum.training_eligible is not False
        ):
            _refuse(
                ImprovementPlanRefusalCode.CURRICULUM_QUARANTINE_BROKEN,
                "Track C output cannot become training evidence",
            )
        curriculum_pair_ids = {
            pair.twin_pair_id for pair in curriculum.contrast_pairs
        }
        planned_pair_ids = {
            candidate.pair_id for candidate in manifest.paired_candidates
        }
        if planned_pair_ids != curriculum_pair_ids:
            _refuse(
                ImprovementPlanRefusalCode.CURRICULUM_PAIR_MISMATCH,
                "Track E candidates must carry exactly Track C twin-pair identities",
            )
        try:
            paired_plan = plan_paired_intervention(
                plan_id=manifest.paired_plan_id,
                randomization_seed=manifest.paired_randomization_seed,
                candidates=manifest.paired_candidates,
                delta=manifest.paired_delta,
                retry_policy=manifest.paired_retry_policy,
                analysis_gate=manifest.paired_analysis_gate,
                policy_gate=PolicyGate(manifest.standing_approvals, repo_root=root),
                repo_root=root,
                spent_today_usd=manifest.spent_today_usd,
            )
        except PairedInterventionRefusal as exc:
            _refuse(
                ImprovementPlanRefusalCode.PAIRED_INTERVENTION_REFUSED,
                f"{exc.reason_code}: {exc}",
            )
        try:
            validated_trainer = validate_trainer_bundle(
                trainer,
                trainer_root,
                store_root=manifest.authority_store_root,
            )
            trainer_plan = render_trl_plan(
                trainer,
                trainer_root,
                manifest.trainer_backend_identity,
                store_root=manifest.authority_store_root,
            )
        except TrainerBundleRefusal as exc:
            _refuse(
                ImprovementPlanRefusalCode.TRAINER_BUNDLE_REFUSED,
                exc.reason_code,
            )

        paired_outcome: PairedInterventionOutcomeV1 | None = None
        if manifest.paired_outcome is not None:
            try:
                paired_outcome = analyze_paired_outcomes(
                    artifact_id=manifest.paired_outcome.artifact_id,
                    plan=paired_plan,
                    observations=manifest.paired_outcome.observations,
                    decision_rule=manifest.paired_outcome.decision_rule,
                )
            except PairedOutcomeRefusal as exc:
                _refuse(
                    ImprovementPlanRefusalCode.PAIRED_OUTCOME_REFUSED,
                    f"{exc.reason_code}: {exc}",
                )

        result_validation: TrainerResultValidation | None = None
        handoff: FrozenHeldOutEvaluationPlan | None = None
        if manifest.trainer_result is not None:
            attempt = manifest.trainer_result
            result_validation = validate_trainer_result_manifest(
                attempt.manifest,
                expected=trainer_plan.expected_result,
                dataset_manifest_authority=(
                    validated_trainer.dataset_manifest_authority
                ),
                result_manifest_authority=attempt.result_manifest_authority,
                dataset_authority_repo_root=trainer_root,
                result_authority_repo_root=root,
            )
            if (
                result_validation.status != "valid"
                or not result_validation.eligible_for_held_out_handoff
            ):
                reasons = ",".join(
                    reason.value for reason in result_validation.reason_codes
                )
                _refuse(
                    ImprovementPlanRefusalCode.RESULT_REFUSED,
                    reasons or "result ineligible",
                )
            try:
                handoff = render_frozen_held_out_evaluation_plan(
                    attempt.manifest,
                    expected=trainer_plan.expected_result,
                    frozen_tasks=attempt.frozen_tasks,
                    trusted_heldout_cluster_keys=(
                        attempt.trusted_heldout_cluster_keys
                    ),
                    dataset_manifest_authority=(
                        validated_trainer.dataset_manifest_authority
                    ),
                    result_manifest_authority=attempt.result_manifest_authority,
                    dataset_authority_repo_root=trainer_root,
                    result_authority_repo_root=root,
                )
            except TrainerResultManifestRefused as exc:
                _refuse(
                    ImprovementPlanRefusalCode.RESULT_REFUSED,
                    ",".join(reason.value for reason in exc.reason_codes),
                )

        _write_new(staged / "curriculum.json", _json_bytes(curriculum))
        _write_new(staged / "paired-plan.json", _json_bytes(paired_plan))
        _write_new(staged / "trainer-plan.json", _json_bytes(trainer_plan))
        if paired_outcome is not None:
            _write_new(staged / "paired-outcome.json", _json_bytes(paired_outcome))
        if result_validation is not None:
            _write_new(
                staged / "result-validation.json",
                _json_bytes(result_validation),
            )
        if handoff is not None:
            _write_new(staged / "held-out-handoff.json", _json_bytes(handoff))
        artifacts = _artifact_records(staged)
        body = {
            "input_manifest_digest": _input_digest(manifest),
            "stages": [
                stage.model_dump(mode="json")
                for stage in _stages(
                    manifest,
                    dataset=exported.manifest,
                    curriculum=curriculum,
                    paired_plan=paired_plan,
                    trainer_plan=trainer_plan,
                    paired_outcome=paired_outcome,
                    result_validation=result_validation,
                    handoff=handoff,
                )
            ],
            "artifacts": [
                artifact.model_dump(mode="json") for artifact in artifacts
            ],
            "dataset_manifest": exported.manifest.model_dump(mode="json"),
            "curriculum": curriculum.model_dump(mode="json"),
            "paired_plan": paired_plan.model_dump(mode="json"),
            "trainer_plan": trainer_plan.model_dump(mode="json"),
            "result_validation": (
                result_validation.model_dump(mode="json")
                if result_validation is not None
                else None
            ),
            "held_out_handoff": (
                handoff.model_dump(mode="json") if handoff is not None else None
            ),
            "paired_outcome": (
                paired_outcome.model_dump(mode="json")
                if paired_outcome is not None
                else None
            ),
        }
        digest_body = {
            "schema_version": "improvement-plan-bundle/v1",
            "ready_for_external_sft": True,
            "ready_for_rl": False,
            **body,
        }
        bundle = ImprovementPlanBundleV1.model_validate(
            {**body, "bundle_digest": compute_prefixed_sha256(digest_body)}
        )
        _write_new(staged / _PLAN_FILENAME, _json_bytes(bundle))
        expected_inventory.extend(_relative_inventory(staged))
    return bundle


def run_improvement_plan(
    manifest: ImprovementPlanInputManifestV1,
    *,
    workspace_root: Path,
) -> ImprovementPlanBundleV1:
    """Compose and immutably publish the offline plan; never submit or execute it."""

    try:
        return _run_improvement_plan(manifest, workspace_root=workspace_root)
    except ImprovementPlanRefusal:
        raise
    except PairedInterventionRefusal as exc:
        _refuse(
            ImprovementPlanRefusalCode.PAIRED_INTERVENTION_REFUSED,
            f"{exc.reason_code}: {exc}",
        )
    except PairedOutcomeRefusal as exc:
        _refuse(
            ImprovementPlanRefusalCode.PAIRED_OUTCOME_REFUSED,
            f"{exc.reason_code}: {exc}",
        )
    except TrainerBundleRefusal as exc:
        _refuse(ImprovementPlanRefusalCode.TRAINER_BUNDLE_REFUSED, exc.reason_code)
    except TrainerResultManifestRefused as exc:
        _refuse(
            ImprovementPlanRefusalCode.RESULT_REFUSED,
            ",".join(exc.reason_codes),
        )
    except (OSError, ValueError) as exc:
        _refuse(ImprovementPlanRefusalCode.OUTPUT_PUBLICATION_REFUSED, str(exc))


def gate_table(bundle: ImprovementPlanBundleV1) -> str:
    """Render a compact human-readable view without changing plan state."""

    lines = (
        "stage\tstatus\treason\tauthority\tinput_digest\toutput_digest\tcount",
    )
    rows = (
        (
            f"{item.stage.value}\t{item.status.value}\t{item.reason_code.value}\t"
            f"{item.authority_level}\t{item.input_digest}\t"
            f"{item.output_digest or '-'}\t{item.numerator}/{item.denominator}"
        )
        for item in bundle.stages
    )
    return "\n".join((*lines, *rows))


__all__ = [
    "ImprovementArtifactV1",
    "ImprovementPlanBundleV1",
    "ImprovementPlanInputManifestV1",
    "ImprovementPlanRefusal",
    "ImprovementPlanRefusalCode",
    "ImprovementStage",
    "ImprovementStageRecordV1",
    "PairedOutcomeInputV1",
    "StageReason",
    "StageStatus",
    "TrainerResultAttemptV1",
    "gate_table",
    "run_improvement_plan",
]
