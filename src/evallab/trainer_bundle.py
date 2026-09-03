"""Backend-neutral, non-executing trainer bundle validation and plan rendering."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Never

from pydantic import ConfigDict, Field, field_serializer, field_validator, model_validator

from evallab.artifact_authority import (
    VERIFIER_IMPLEMENTATION_DIGEST,
    ArtifactAuthority,
    ArtifactRef,
    AuthorityRefusal,
    compute_authority_digest,
    verify_artifact,
)
from evallab.benchmark_program_contracts import compute_prefixed_sha256, validate_safe_relative_path
from evallab.results import sha256_file
from evallab.schemas import ContractModel

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Backend = Literal["trl", "spade"]
ExternalBackendName = Literal["generic-trl", "spade-external-consumer"]
_EXTERNAL_BACKEND_NAME: dict[Backend, ExternalBackendName] = {
    "trl": "generic-trl",
    "spade": "spade-external-consumer",
}
Representation = Literal["prompt_response_sft", "episode_steps"]
SafeFieldName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
_RESULT_SCHEMA = "trainer-result-manifest-v1"
_REQUIRED_RESULT_FIELDS = (
    "trainer_bundle_digest",
    "trainer_plan_digest",
    "checkpoint_artifact_digest",
)
_PROHIBITED_TRAINING_FAMILIES = frozenset({"syn-funcdag-easy"})
_FORBIDDEN_TRAINING_KEYS = frozenset(
    {
        "attention_mask",
        "completion_mask",
        "evaluator_only",
        "hidden_verifier",
        "input_ids",
        "label",
        "labels",
        "log_probs",
        "logprobs",
        "loss_mask",
        "mask",
        "masks",
        "reward",
        "rewards",
        "assistant_mask",
        "token_id",
        "token_ids",
        "verifier_reward",
    }
)
_CAS_URI = re.compile(r"^cas://sha256/[0-9a-f]{64}$")


class TrainerBundleRefusal(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class _FrozenContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TrainerSplitBindingV1(_FrozenContract):
    path: str = Field(min_length=1)
    digest: Digest
    cluster_key_digest: Digest
    record_count: int = Field(ge=0)


class TrainerSourceBindingV1(_FrozenContract):
    job_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    source_digest: Digest
    registry_allowed_use: Literal["training"] = "training"
    task_registry_record_digest: Digest
    trial_admissibility_digest: Digest
    trial_admissibility_decision: Literal["admissible", "rejected", "unavailable"]
    trial_analysis_eligibility: Literal["causal-eligible", "calibration-only"]
    trial_admissibility_allowed_use: Literal["causal", "descriptive-only"]


class TrainerExporterBindingV1(_FrozenContract):
    name: Literal["evallab.training_export"] = "evallab.training_export"
    version: Literal["1"] = "1"
    digest: Digest


class RepresentationCountV1(_FrozenContract):
    representation: Representation
    count: int = Field(ge=0)


class TrainerDatasetBindingV1(_FrozenContract):
    """Exact Track A manifest shape; authority records remain copied digest refs."""

    schema_version: Literal["training-dataset-manifest/v1"] = "training-dataset-manifest/v1"
    manifest_path: str | None = "manifest.json"
    cas_uri: str | None = None
    manifest_digest: Digest
    dataset_digest: Digest
    train_split: TrainerSplitBindingV1
    validation_split: TrainerSplitBindingV1
    test_split: TrainerSplitBindingV1
    source_refs: tuple[TrainerSourceBindingV1, ...]
    exporter: TrainerExporterBindingV1
    benchmark_families: tuple[str, ...]
    task_families: tuple[str, ...]
    environment_integrity: Literal["passed"] = "passed"
    capture_complete: Literal[True] = True
    redaction_status: Literal["redacted"] = "redacted"
    registry_allowed_use: Literal["training"] = "training"
    exclusions_path: str = "exclusions.jsonl"
    exclusions_digest: Digest
    exclusion_count: int = Field(ge=0)
    representation_counts: tuple[RepresentationCountV1, ...]

    @field_validator("representation_counts", mode="before")
    @classmethod
    def freeze_representation_counts(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return [
                {"representation": name, "count": count} for name, count in sorted(value.items())
            ]
        return value

    @field_serializer("representation_counts")
    def serialize_representation_counts(
        self, value: tuple[RepresentationCountV1, ...]
    ) -> dict[str, int]:
        return {item.representation: item.count for item in value}

    @model_validator(mode="after")
    def validate_shape(self) -> TrainerDatasetBindingV1:
        if (self.manifest_path is None) == (self.cas_uri is None):
            raise ValueError("exactly one of manifest_path or cas_uri is required")
        representations = tuple(item.representation for item in self.representation_counts)
        if representations != ("episode_steps", "prompt_response_sft"):
            raise ValueError("representation counts must contain each representation once")
        return self


class TrainerArtifactRefV1(_FrozenContract):
    path: str = Field(min_length=1)
    content_digest: Digest
    cas_uri: str = Field(min_length=1)


class TrainerRevisionIdentityV1(_FrozenContract):
    name: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    digest: Digest


class TrainerModelIdentityV1(_FrozenContract):
    provider: str = Field(min_length=1)
    model: TrainerRevisionIdentityV1
    tokenizer: TrainerRevisionIdentityV1
    chat_template: TrainerRevisionIdentityV1
    access_mode: Literal["checkpoint", "api_only"]
    checkpoint: TrainerArtifactRefV1 | None = None
    enable_thinking: Literal[False] = False

    @model_validator(mode="after")
    def validate_access(self) -> TrainerModelIdentityV1:
        if self.access_mode == "checkpoint" and self.checkpoint is None:
            raise ValueError("checkpoint access requires a checkpoint artifact")
        if self.access_mode == "api_only" and self.checkpoint is not None:
            raise ValueError("api_only access cannot claim a checkpoint artifact")
        return self


class TrainerObjectiveV1(_FrozenContract):
    kind: Literal["sft", "verifier_reward_episode"]
    verifier_contract_digest: Digest | None = None

    @model_validator(mode="after")
    def validate_objective(self) -> TrainerObjectiveV1:
        if self.kind == "sft" and self.verifier_contract_digest is not None:
            raise ValueError("SFT cannot claim a verifier reward contract")
        if self.kind == "verifier_reward_episode" and self.verifier_contract_digest is None:
            raise ValueError("verifier-reward episodes require a verifier contract digest")
        return self


class TrainerRenderingContractV1(_FrozenContract):
    representation: Representation
    sft_format: Literal["messages", "prompt_completion"] | None = None
    messages_field: SafeFieldName | None = None
    prompt_field: SafeFieldName | None = None
    completion_field: SafeFieldName | None = None
    episode_field: SafeFieldName | None = None
    truncation: Literal["error"] = "error"
    assistant_only_loss: Literal[False] = False

    @model_validator(mode="after")
    def validate_fields(self) -> TrainerRenderingContractV1:
        fields = (self.messages_field, self.prompt_field, self.completion_field, self.episode_field)
        if self.representation == "episode_steps":
            expected = (False, False, False, True)
            if self.sft_format is not None:
                raise ValueError("episode rendering cannot set an SFT format")
        elif self.sft_format == "messages":
            expected = (True, False, False, False)
        elif self.sft_format == "prompt_completion":
            expected = (False, True, True, False)
        else:
            raise ValueError("SFT rendering requires messages or prompt_completion format")
        if tuple(field is not None for field in fields) != expected:
            raise ValueError("rendering fields do not match representation")
        return self


class TrainerHyperparametersV1(_FrozenContract):
    epochs: int = Field(gt=0)
    learning_rate: float = Field(gt=0)
    batch_size: int = Field(gt=0)
    gradient_accumulation_steps: int = Field(gt=0)
    max_sequence_length: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_finite_values(self) -> TrainerHyperparametersV1:
        if not math.isfinite(self.learning_rate):
            raise ValueError("learning_rate must be finite")
        return self


class TrainerBackendRequirementsV1(_FrozenContract):
    requires_on_policy_tokens: bool = False
    requires_token_logprobs: bool = False
    requires_gpu: bool = False
    requires_network: bool = False
    required_runtime: str | None = None


class TrainerResultContractV1(_FrozenContract):
    result_schema: Literal["trainer-result-manifest-v1"] = _RESULT_SCHEMA
    manifest_path: str = Field(min_length=1)
    required_fields: tuple[str, ...] = _REQUIRED_RESULT_FIELDS


class TrainerBackendIdentityV1(_FrozenContract):
    name: ExternalBackendName
    version: str = Field(min_length=1)
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    image_digest: Digest


class TrainerTaskIdentityV1(_FrozenContract):
    task_id: str = Field(min_length=1)
    task_digest: Digest
    cluster_key_digest: Digest
    verifier_digest: Digest
    environment_digest: Digest


def trainer_task_set_digest(tasks: tuple[TrainerTaskIdentityV1, ...]) -> str:
    return compute_prefixed_sha256(
        [task.model_dump(mode="json", exclude_none=False) for task in tasks]
    )


def trainer_evaluation_suite_digest(suite_name: str, task_set_digest: str) -> str:
    return compute_prefixed_sha256(
        {
            "schema_version": "trainer-evaluation-suite/v1",
            "suite_name": suite_name,
            "task_set_digest": task_set_digest,
        }
    )


class TrainerEvaluationSetV1(_FrozenContract):
    suite_name: str = Field(min_length=1)
    suite_digest: Digest
    task_set_digest: Digest
    tasks: tuple[TrainerTaskIdentityV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_tasks(self) -> TrainerEvaluationSetV1:
        identities = tuple((task.task_id, task.task_digest) for task in self.tasks)
        task_ids = tuple(task.task_id for task in self.tasks)
        task_digests = tuple(task.task_digest for task in self.tasks)
        if (
            tuple(sorted(identities)) != identities
            or len(set(task_ids)) != len(task_ids)
            or len(set(task_digests)) != len(task_digests)
        ):
            raise ValueError("evaluation tasks must be non-aliased, unique, and canonically sorted")
        if self.task_set_digest != trainer_task_set_digest(self.tasks):
            raise ValueError("task_set_digest mismatch")
        if self.suite_digest != trainer_evaluation_suite_digest(
            self.suite_name, self.task_set_digest
        ):
            raise ValueError("suite_digest mismatch")
        return self


class TrainerAuthorityGateV1(_FrozenContract):
    scope: Literal["fixture_only"] = "fixture_only"
    source_authority_status: Literal["copied_digest_refs_only"] = "copied_digest_refs_only"
    dataset_manifest_authority_required: Literal["bytes-verified"] = "bytes-verified"
    real_corpus_training_allowed: Literal[False] = False


class TrainerBundleV1(_FrozenContract):
    schema_version: Literal["trainer-bundle/v1"] = "trainer-bundle/v1"
    model_identity: TrainerModelIdentityV1
    dataset: TrainerDatasetBindingV1
    dataset_manifest_artifact_digest: Digest
    selected_split: str = Field(min_length=1)
    heldout_split: Literal["validation", "test"]
    selected_representation: Representation
    objective: TrainerObjectiveV1
    rendering: TrainerRenderingContractV1
    seed: int = Field(ge=0)
    hyperparameters: TrainerHyperparametersV1
    backend_requirements: TrainerBackendRequirementsV1
    result_contract: TrainerResultContractV1
    evaluation_set: TrainerEvaluationSetV1
    authority_gate: TrainerAuthorityGateV1


class ValidatedTrainerBundleV1(_FrozenContract):
    dataset_manifest_authority: ArtifactAuthority
    trainer_bundle_digest: Digest
    dataset_digest: Digest
    train_split_digest: Digest
    heldout_split_digest: Digest
    input_checkpoint_digest: Digest | None
    source_authority_status: Literal["copied_digest_refs_only"] = "copied_digest_refs_only"
    validated_artifact_paths: tuple[str, ...]


class ExpectedTrainerResultV1(_FrozenContract):
    """Strict digest-bound Track D projection consumed by Track G."""

    schema_version: Literal["expected-trainer-result/v1"] = "expected-trainer-result/v1"
    result_schema: Literal["trainer-result-manifest-v1"] = _RESULT_SCHEMA
    result_manifest_path: str = Field(min_length=1)
    trainer_bundle_digest: Digest
    trainer_plan_digest: Digest
    backend_name: ExternalBackendName
    dataset_manifest_authority_digest: Digest
    dataset_manifest_verifier_digest: Digest
    dataset_manifest_authority_level: Literal["bytes-verified"] = "bytes-verified"
    backend_version: str = Field(min_length=1)
    backend_source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    backend_image_digest: Digest
    adapter_contract: Literal["trl-sft-plan/v1", "spade-shaped-plan/v1"]
    dataset_manifest_digest: Digest
    dataset_digest: Digest
    train_split_digest: Digest
    train_cluster_key_digest: Digest
    heldout_split_digest: Digest
    heldout_cluster_key_digest: Digest
    input_model_checkpoint_digest: Digest
    model_revision: str = Field(min_length=1)
    model_digest: Digest
    tokenizer_revision: str = Field(min_length=1)
    tokenizer_digest: Digest
    chat_template_revision: str = Field(min_length=1)
    chat_template_digest: Digest
    effective_config_digest: Digest
    evaluation_set: TrainerEvaluationSetV1
    source_authority_status: Literal["copied_digest_refs_only"] = "copied_digest_refs_only"
    required_result_fields: tuple[
        Literal["trainer_bundle_digest"],
        Literal["trainer_plan_digest"],
        Literal["checkpoint_artifact_digest"],
    ] = _REQUIRED_RESULT_FIELDS


class IncompatibilityCode(StrEnum):
    API_ONLY_MODEL = "api_only_model"
    SFT_SIGNAL_NOT_ESTABLISHED = "sft_signal_not_established"
    UNSUPPORTED_OBJECTIVE = "unsupported_objective"
    ON_POLICY_TOKENS_REQUIRED = "on_policy_tokens_required"
    TOKEN_LOGPROBS_REQUIRED = "token_logprobs_required"
    GPU_REQUIRED = "gpu_required"
    NETWORK_REQUIRED = "network_required"
    TRAINER_RUNTIME_REQUIRED = "trainer_runtime_required"


class BackendIncompatibilityV1(_FrozenContract):
    backend: Backend
    code: IncompatibilityCode
    reason_code: str = Field(min_length=1)
    requirement: str = Field(min_length=1)


class TRLPlanPayloadV1(_FrozenContract):
    schema_version: Literal["trl-sft-plan/v1"] = "trl-sft-plan/v1"
    adoption_stage: Literal["adopted_s0"] = "adopted_s0"
    trainer_class: Literal["trl.SFTTrainer"] = "trl.SFTTrainer"
    sft_format: Literal["messages", "prompt_completion"]
    dataset_path: str
    checkpoint_path: str
    messages_field: str | None
    prompt_field: str | None
    completion_field: str | None
    truncation: Literal["error"] = "error"
    assistant_only_loss: Literal[False] = False
    epochs: int
    learning_rate: float
    batch_size: int
    gradient_accumulation_steps: int
    max_sequence_length: int
    result_manifest_path: str


class SpadeArmV1(_FrozenContract):
    arm: Literal["hinted", "unhinted"]
    hint_available: bool


class SpadePlanPayloadV1(_FrozenContract):
    schema_version: Literal["spade-shaped-plan/v1"] = "spade-shaped-plan/v1"
    adoption_stage: Literal["adapt_only"] = "adapt_only"
    consumer_kind: Literal["external_spade_shaped"] = "external_spade_shaped"
    pair_id: Digest
    arms: tuple[SpadeArmV1, SpadeArmV1]
    episode_path: str
    checkpoint_path: str
    verifier_contract_digest: Digest
    curriculum_strategy: Literal["hint_regret"] = "hint_regret"
    candidate_selection: Literal["hard_feasible"] = "hard_feasible"
    result_manifest_path: str


class RenderedTrainerPlanV1(_FrozenContract):
    schema_version: Literal["rendered-trainer-plan/v1"] = "rendered-trainer-plan/v1"
    backend: Backend
    adapter_contract: Literal["trl-sft-plan/v1", "spade-shaped-plan/v1"]
    expected_result: ExpectedTrainerResultV1
    model_identity: TrainerModelIdentityV1
    objective: Literal["sft", "verifier_reward_episode"]
    seed: int = Field(ge=0)
    payload: TRLPlanPayloadV1 | SpadePlanPayloadV1

    @model_validator(mode="after")
    def validate_backend_payload(self) -> RenderedTrainerPlanV1:
        if self.backend == "trl" and not isinstance(self.payload, TRLPlanPayloadV1):
            raise ValueError("TRL plan requires a TRL payload")
        if self.backend == "spade" and not isinstance(self.payload, SpadePlanPayloadV1):
            raise ValueError("SPADE plan requires a SPADE-shaped payload")
        if self.adapter_contract != self.payload.schema_version:
            raise ValueError("adapter contract must match payload schema")
        if (
            self.expected_result.backend_name != _EXTERNAL_BACKEND_NAME[self.backend]
            or self.expected_result.adapter_contract != self.adapter_contract
            or self.expected_result.model_revision != self.model_identity.model.revision
            or self.expected_result.model_digest != self.model_identity.model.digest
            or self.expected_result.tokenizer_revision != self.model_identity.tokenizer.revision
            or self.expected_result.tokenizer_digest != self.model_identity.tokenizer.digest
            or self.expected_result.chat_template_revision
            != self.model_identity.chat_template.revision
            or self.expected_result.chat_template_digest != self.model_identity.chat_template.digest
        ):
            raise ValueError("expected result must bind the rendered plan identity")
        if self.expected_result.trainer_plan_digest != trainer_plan_digest(self):
            raise ValueError("expected result trainer plan digest mismatch")
        return self


def trainer_bundle_digest(bundle: TrainerBundleV1) -> str:
    return compute_prefixed_sha256(bundle.model_dump(mode="json", exclude_none=False))


def trainer_config_digest(bundle: TrainerBundleV1) -> str:
    return compute_prefixed_sha256(
        {
            "backend_requirements": bundle.backend_requirements.model_dump(mode="json"),
            "heldout_split": bundle.heldout_split,
            "hyperparameters": bundle.hyperparameters.model_dump(mode="json"),
            "objective": bundle.objective.model_dump(mode="json"),
            "rendering": bundle.rendering.model_dump(mode="json"),
            "seed": bundle.seed,
            "selected_representation": bundle.selected_representation,
            "selected_split": bundle.selected_split,
        }
    )


def trainer_plan_digest(plan: RenderedTrainerPlanV1 | Mapping[str, Any]) -> str:
    payload = (
        plan.model_dump(mode="json", exclude_none=False)
        if isinstance(plan, RenderedTrainerPlanV1)
        else dict(plan)
    )
    expected = dict(payload["expected_result"])
    expected.pop("trainer_plan_digest", None)
    payload["expected_result"] = expected
    return compute_prefixed_sha256(payload)


def _refuse(code: str) -> Never:
    raise TrainerBundleRefusal(f"blocked:trainer_bundle:{code}")


def _safe_path(path: str, *, label: str) -> str:
    try:
        return validate_safe_relative_path(path)
    except ValueError:
        _refuse(f"path_invalid:{label}")


def _bundle_root(root: Path) -> Path:
    expanded = root.expanduser().absolute()
    if expanded.is_symlink():
        _refuse("symlink:bundle_root")
    if not expanded.is_dir():
        _refuse("bundle_root_missing")
    return expanded


def _bound_file(root: Path, path: str, digest: str, *, label: str) -> tuple[str, Path]:
    relative = _safe_path(path, label=label)
    current = root
    for part in PurePosixPath(relative).parts:
        current /= part
        if current.is_symlink():
            _refuse(f"symlink:{label}")
    if not current.is_file():
        _refuse(f"artifact_missing:{label}")
    if f"sha256:{sha256_file(current)}" != digest:
        _refuse(f"digest_mismatch:{label}")
    return relative, current


def _manifest_digest(data: Mapping[str, Any]) -> str:
    body = dict(data)
    body.pop("manifest_digest", None)
    body.pop("cas_uri", None)
    return compute_prefixed_sha256(body)


def _dataset_digest(dataset: TrainerDatasetBindingV1) -> str:
    return compute_prefixed_sha256(
        {
            "train_split": dataset.train_split.model_dump(mode="json"),
            "validation_split": dataset.validation_split.model_dump(mode="json"),
            "test_split": dataset.test_split.model_dump(mode="json"),
            "exclusions_digest": dataset.exclusions_digest,
        }
    )


def _first_forbidden_field(value: Any) -> str | None:
    pending = [value]
    keys: set[str] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            keys.update(str(key).casefold() for key in current)
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    forbidden = sorted(keys.intersection(_FORBIDDEN_TRAINING_KEYS))
    return forbidden[0] if forbidden else None


def _validate_exclusions_rows(exclusions_path: Path, expected_count: int) -> None:
    count = 0
    try:
        with exclusions_path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                count += 1
                forbidden = _first_forbidden_field(json.loads(line))
                if forbidden is not None:
                    _refuse(f"prohibited_exclusion_field:{forbidden}")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _refuse("exclusions_invalid_jsonl")
    if count != expected_count:
        _refuse("exclusions_record_count_mismatch")


def _validate_training_rows(
    split_path: Path, expected_count: int, rendering: TrainerRenderingContractV1
) -> None:
    count = 0
    try:
        with split_path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                count += 1
                row = json.loads(line)
                if not isinstance(row, Mapping):
                    _refuse("training_row_not_object")
                forbidden = _first_forbidden_field(row)
                if forbidden is not None:
                    _refuse(f"prohibited_training_field:{forbidden}")
                required = tuple(
                    field
                    for field in (
                        rendering.messages_field,
                        rendering.prompt_field,
                        rendering.completion_field,
                        rendering.episode_field,
                    )
                    if field is not None
                )
                if any(field not in row for field in required):
                    _refuse("rendering_field_missing")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _refuse("training_split_invalid_jsonl")
    if count != expected_count:
        _refuse("training_split_record_count_mismatch")


def _validate_dataset(
    root: Path,
    dataset: TrainerDatasetBindingV1,
    rendering: TrainerRenderingContractV1,
    heldout_name: Literal["validation", "test"],
) -> tuple[list[str], TrainerSplitBindingV1]:
    if dataset.dataset_digest != _dataset_digest(dataset):
        _refuse("dataset_digest_mismatch")
    paths: list[str] = []
    if dataset.manifest_path is not None:
        manifest_path = _safe_path(dataset.manifest_path, label="dataset_manifest")
        current = root
        for part in PurePosixPath(manifest_path).parts:
            current /= part
            if current.is_symlink():
                _refuse("symlink:dataset_manifest")
        if not current.is_file():
            _refuse("artifact_missing:dataset_manifest")
        try:
            raw = json.loads(current.read_bytes())
            persisted = TrainerDatasetBindingV1.model_validate(raw)
        except (OSError, ValueError, json.JSONDecodeError):
            _refuse("manifest_contract_invalid")
        if _manifest_digest(raw) != dataset.manifest_digest:
            _refuse("manifest_digest_mismatch")
        if persisted != dataset:
            _refuse("manifest_binding_mismatch")
        paths.append(manifest_path)
    else:
        expected_uri = f"cas://sha256/{dataset.manifest_digest.removeprefix('sha256:')}"
        if dataset.cas_uri != expected_uri or not _CAS_URI.fullmatch(dataset.cas_uri):
            _refuse("mutable_source:dataset_manifest")
        if _manifest_digest(dataset.model_dump(mode="json")) != dataset.manifest_digest:
            _refuse("manifest_digest_mismatch")

    splits = (dataset.train_split, dataset.validation_split, dataset.test_split)
    if (
        len({split.path for split in splits}) != len(splits)
        or len({split.digest for split in splits}) != len(splits)
        or len({split.cluster_key_digest for split in splits}) != len(splits)
    ):
        _refuse("split_overlap")

    split_path, split_file = _bound_file(
        root, dataset.train_split.path, dataset.train_split.digest, label="training_split"
    )
    _validate_training_rows(split_file, dataset.train_split.record_count, rendering)
    paths.append(split_path)
    exclusions_path, exclusions_file = _bound_file(
        root, dataset.exclusions_path, dataset.exclusions_digest, label="exclusions"
    )
    _validate_exclusions_rows(exclusions_file, dataset.exclusion_count)
    paths.append(exclusions_path)
    heldout = dataset.validation_split if heldout_name == "validation" else dataset.test_split
    for name, split in (
        ("validation_split", dataset.validation_split),
        ("test_split", dataset.test_split),
    ):
        hidden_path, _ = _bound_file(root, split.path, split.digest, label=name)
        paths.append(hidden_path)
    return paths, heldout


def _validate_checkpoint(root: Path, checkpoint: TrainerArtifactRefV1) -> str:
    expected_uri = f"cas://sha256/{checkpoint.content_digest.removeprefix('sha256:')}"
    if checkpoint.cas_uri != expected_uri:
        _refuse("mutable_source:checkpoint")
    path, _ = _bound_file(root, checkpoint.path, checkpoint.content_digest, label="checkpoint")
    return path


def _validate_output_path(root: Path, path: str, input_paths: set[str]) -> str:
    relative = _safe_path(path, label="result_manifest")
    if relative in input_paths:
        _refuse("result_path_overwrites_input")
    current = root
    for part in PurePosixPath(relative).parts[:-1]:
        current /= part
        if current.is_symlink():
            _refuse("symlink:result_manifest")
    target = root / relative
    if target.is_symlink():
        _refuse("symlink:result_manifest")
    if target.exists():
        _refuse("result_manifest_exists")
    return relative


def _dataset_manifest_ref(bundle: TrainerBundleV1) -> ArtifactRef:
    ref = bundle.dataset.manifest_path or bundle.dataset.cas_uri
    if ref is None:
        raise AssertionError("dataset manifest contract requires a reference")
    try:
        return ArtifactRef(ref=ref, digest=bundle.dataset_manifest_artifact_digest)
    except ValueError:
        _refuse("dataset_manifest_authority:ref_not_canonical")


def _verify_dataset_manifest_authority(
    bundle: TrainerBundleV1,
    root: Path,
    store_root: Path | None,
) -> ArtifactAuthority:
    result = verify_artifact(
        _dataset_manifest_ref(bundle),
        minimum_level="bytes-verified",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        repo_root=root,
        store_root=store_root,
    )
    if isinstance(result, AuthorityRefusal):
        _refuse(f"dataset_manifest_authority:{result.reason}")
    if result.level != "bytes-verified":
        _refuse("dataset_manifest_authority:authority_level_insufficient")
    return result


def validate_trainer_bundle(
    bundle: TrainerBundleV1,
    root: Path,
    *,
    store_root: Path | None = None,
) -> ValidatedTrainerBundleV1:
    """Validate immutable fixtures without invoking a trainer, network, or GPU."""

    bundle_root = _bundle_root(root)
    authority = bundle.authority_gate
    if (
        authority.scope != "fixture_only"
        or authority.source_authority_status != "copied_digest_refs_only"
        or authority.dataset_manifest_authority_required != "bytes-verified"
        or authority.real_corpus_training_allowed is not False
    ):
        _refuse("authority_gate_not_satisfied")
    dataset_manifest_authority = _verify_dataset_manifest_authority(bundle, bundle_root, store_root)
    evaluation = bundle.evaluation_set
    if evaluation.task_set_digest != trainer_task_set_digest(
        evaluation.tasks
    ) or evaluation.suite_digest != trainer_evaluation_suite_digest(
        evaluation.suite_name, evaluation.task_set_digest
    ):
        _refuse("evaluation_set_digest_mismatch")
    if not math.isfinite(bundle.hyperparameters.learning_rate):
        _refuse("nonfinite_hyperparameter")
    dataset = bundle.dataset
    paths, heldout = _validate_dataset(bundle_root, dataset, bundle.rendering, bundle.heldout_split)
    if bundle.selected_split != "train":
        _refuse("hidden_or_nontraining_split")
    if _PROHIBITED_TRAINING_FAMILIES.intersection(
        family.casefold() for family in dataset.task_families
    ):
        _refuse("prohibited_corpus:syn-funcdag-easy")
    for source in dataset.source_refs:
        if (
            source.trial_admissibility_decision != "admissible"
            or source.trial_analysis_eligibility != "causal-eligible"
            or source.trial_admissibility_allowed_use != "causal"
        ):
            _refuse("source_authority_not_training_admissible")
    identities = {
        (
            ref.job_id,
            ref.trial_id,
            ref.source_digest,
            ref.trial_admissibility_digest,
            ref.task_registry_record_digest,
        )
        for ref in dataset.source_refs
    }
    if not identities or len(dataset.source_refs) != len(identities):
        _refuse("source_identity_invalid")
    if not dataset.benchmark_families or not dataset.task_families:
        _refuse("training_family_missing")

    total_records = sum(
        split.record_count
        for split in (dataset.train_split, dataset.validation_split, dataset.test_split)
    )
    counts = {item.representation: item.count for item in dataset.representation_counts}
    if set(counts) != {"prompt_response_sft", "episode_steps"}:
        _refuse("representation_counts_invalid")
    if sum(counts.values()) != total_records:
        _refuse("representation_count_mismatch")
    if counts.get(bundle.selected_representation, 0) <= 0:
        _refuse("selected_representation_unavailable")
    expected_representation = (
        "prompt_response_sft" if bundle.objective.kind == "sft" else "episode_steps"
    )
    if (
        bundle.selected_representation != expected_representation
        or bundle.rendering.representation != expected_representation
    ):
        _refuse("rendering_objective_mismatch")

    if bundle.result_contract.required_fields != _REQUIRED_RESULT_FIELDS:
        _refuse("result_manifest_identity_fields_incomplete")
    input_paths = set(paths)
    result_path = _validate_output_path(
        bundle_root, bundle.result_contract.manifest_path, input_paths
    )
    checkpoint_digest: str | None = None
    checkpoint = bundle.model_identity.checkpoint
    if checkpoint is not None:
        checkpoint_path = _validate_checkpoint(bundle_root, checkpoint)
        if checkpoint_path == result_path:
            _refuse("result_path_overwrites_input")
        checkpoint_digest = checkpoint.content_digest
        paths.append(checkpoint_path)
    return ValidatedTrainerBundleV1(
        dataset_manifest_authority=dataset_manifest_authority,
        trainer_bundle_digest=trainer_bundle_digest(bundle),
        dataset_digest=dataset.dataset_digest,
        train_split_digest=dataset.train_split.digest,
        heldout_split_digest=heldout.digest,
        input_checkpoint_digest=checkpoint_digest,
        validated_artifact_paths=tuple(sorted(paths)),
    )


def backend_incompatibilities(
    bundle: TrainerBundleV1, backend: Backend
) -> tuple[BackendIncompatibilityV1, ...]:
    """Return typed reasons a plan-only adapter cannot satisfy."""

    incompatible: list[tuple[IncompatibilityCode, str]] = []
    if bundle.model_identity.access_mode == "api_only":
        incompatible.append((IncompatibilityCode.API_ONLY_MODEL, "local checkpoint"))
    supported_objective = "sft" if backend == "trl" else "verifier_reward_episode"
    if bundle.objective.kind != supported_objective:
        incompatible.append((IncompatibilityCode.UNSUPPORTED_OBJECTIVE, supported_objective))
    if backend == "spade":
        incompatible.append(
            (
                IncompatibilityCode.SFT_SIGNAL_NOT_ESTABLISHED,
                "bytes-verified completed SFT result authority unavailable in v1",
            )
        )
    requirements = bundle.backend_requirements
    for required, code, label in (
        (
            requirements.requires_on_policy_tokens,
            IncompatibilityCode.ON_POLICY_TOKENS_REQUIRED,
            "on-policy token collection",
        ),
        (
            requirements.requires_token_logprobs,
            IncompatibilityCode.TOKEN_LOGPROBS_REQUIRED,
            "token log probabilities",
        ),
        (requirements.requires_gpu, IncompatibilityCode.GPU_REQUIRED, "GPU execution"),
        (requirements.requires_network, IncompatibilityCode.NETWORK_REQUIRED, "network access"),
    ):
        if required:
            incompatible.append((code, label))
    if requirements.required_runtime is not None:
        incompatible.append(
            (IncompatibilityCode.TRAINER_RUNTIME_REQUIRED, requirements.required_runtime)
        )
    return tuple(
        BackendIncompatibilityV1(
            backend=backend,
            code=code,
            reason_code=f"blocked:trainer_backend_incompatible:{backend}:{code.value}",
            requirement=requirement,
        )
        for code, requirement in incompatible
    )


def project_expected_trainer_result(
    bundle: TrainerBundleV1,
    *,
    plan_digest: str,
    backend_identity: TrainerBackendIdentityV1,
    adapter_contract: Literal["trl-sft-plan/v1", "spade-shaped-plan/v1"],
) -> ExpectedTrainerResultV1:
    """Project the one strict Track G expectation from immutable bundle inputs."""

    checkpoint = bundle.model_identity.checkpoint
    if checkpoint is None:
        raise ValueError("expected trainer results require a checkpoint artifact")
    heldout = (
        bundle.dataset.validation_split
        if bundle.heldout_split == "validation"
        else bundle.dataset.test_split
    )
    return ExpectedTrainerResultV1(
        result_manifest_path=bundle.result_contract.manifest_path,
        trainer_bundle_digest=trainer_bundle_digest(bundle),
        trainer_plan_digest=plan_digest,
        backend_name=backend_identity.name,
        backend_version=backend_identity.version,
        backend_source_commit=backend_identity.source_commit,
        backend_image_digest=backend_identity.image_digest,
        adapter_contract=adapter_contract,
        dataset_manifest_authority_digest=compute_authority_digest(
            _dataset_manifest_ref(bundle),
            "bytes-verified",
            VERIFIER_IMPLEMENTATION_DIGEST,
        ),
        dataset_manifest_verifier_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        dataset_manifest_digest=bundle.dataset.manifest_digest,
        dataset_digest=bundle.dataset.dataset_digest,
        train_split_digest=bundle.dataset.train_split.digest,
        train_cluster_key_digest=bundle.dataset.train_split.cluster_key_digest,
        heldout_split_digest=heldout.digest,
        heldout_cluster_key_digest=heldout.cluster_key_digest,
        input_model_checkpoint_digest=checkpoint.content_digest,
        model_revision=bundle.model_identity.model.revision,
        model_digest=bundle.model_identity.model.digest,
        tokenizer_revision=bundle.model_identity.tokenizer.revision,
        tokenizer_digest=bundle.model_identity.tokenizer.digest,
        chat_template_revision=bundle.model_identity.chat_template.revision,
        chat_template_digest=bundle.model_identity.chat_template.digest,
        effective_config_digest=trainer_config_digest(bundle),
        evaluation_set=bundle.evaluation_set,
    )


def expected_trainer_result_has_parity(
    bundle: TrainerBundleV1,
    plan: RenderedTrainerPlanV1,
    backend_identity: TrainerBackendIdentityV1,
) -> bool:
    """Check exact projection parity without trusting caller-provided kwargs."""

    return plan.expected_result == project_expected_trainer_result(
        bundle,
        plan_digest=trainer_plan_digest(plan),
        backend_identity=backend_identity,
        adapter_contract=plan.adapter_contract,
    )


def _render_context(
    bundle: TrainerBundleV1,
    root: Path,
    backend: Backend,
    backend_identity: TrainerBackendIdentityV1,
    store_root: Path | None,
) -> TrainerArtifactRefV1:
    validated = validate_trainer_bundle(bundle, root, store_root=store_root)
    if backend_identity.name != _EXTERNAL_BACKEND_NAME[backend]:
        raise ValueError("backend identity does not match renderer")
    incompatible = backend_incompatibilities(bundle, backend)
    if incompatible:
        raise TrainerBundleRefusal(incompatible[0].reason_code)
    checkpoint = bundle.model_identity.checkpoint
    if checkpoint is None or validated.input_checkpoint_digest is None:
        raise AssertionError("compatible plan must bind a validated checkpoint")
    return checkpoint


def _bind_rendered_plan(
    bundle: TrainerBundleV1,
    backend: Backend,
    backend_identity: TrainerBackendIdentityV1,
    adapter: Literal["trl-sft-plan/v1", "spade-shaped-plan/v1"],
    payload: TRLPlanPayloadV1 | SpadePlanPayloadV1,
) -> RenderedTrainerPlanV1:
    placeholder = project_expected_trainer_result(
        bundle,
        plan_digest=compute_prefixed_sha256("unbound-plan-digest"),
        backend_identity=backend_identity,
        adapter_contract=adapter,
    )
    raw = {
        "schema_version": "rendered-trainer-plan/v1",
        "backend": backend,
        "adapter_contract": adapter,
        "expected_result": placeholder.model_dump(mode="json"),
        "model_identity": bundle.model_identity.model_dump(mode="json"),
        "objective": bundle.objective.kind,
        "seed": bundle.seed,
        "payload": payload.model_dump(mode="json"),
    }
    expected = project_expected_trainer_result(
        bundle,
        plan_digest=trainer_plan_digest(raw),
        backend_identity=backend_identity,
        adapter_contract=adapter,
    )
    raw["expected_result"] = expected.model_dump(mode="json")
    plan = RenderedTrainerPlanV1.model_validate(raw)
    if not expected_trainer_result_has_parity(bundle, plan, backend_identity):
        raise AssertionError("rendered trainer result projection lost parity")
    return plan


def render_trl_plan(
    bundle: TrainerBundleV1,
    root: Path,
    backend_identity: TrainerBackendIdentityV1,
    *,
    store_root: Path | None = None,
) -> RenderedTrainerPlanV1:
    """Render adopted S0 TRL SFT configuration; never import or invoke TRL."""

    if backend_identity.name != "generic-trl":
        raise ValueError("TRL renderer requires a generic-trl backend identity")
    checkpoint = _render_context(bundle, root, "trl", backend_identity, store_root)
    rendering = bundle.rendering
    if rendering.sft_format is None:
        raise AssertionError("compatible TRL plan requires an SFT format")
    params = bundle.hyperparameters
    payload = TRLPlanPayloadV1(
        sft_format=rendering.sft_format,
        dataset_path=bundle.dataset.train_split.path,
        checkpoint_path=checkpoint.path,
        messages_field=rendering.messages_field,
        prompt_field=rendering.prompt_field,
        completion_field=rendering.completion_field,
        epochs=params.epochs,
        learning_rate=params.learning_rate,
        batch_size=params.batch_size,
        gradient_accumulation_steps=params.gradient_accumulation_steps,
        max_sequence_length=params.max_sequence_length,
        result_manifest_path=bundle.result_contract.manifest_path,
    )
    return _bind_rendered_plan(
        bundle,
        "trl",
        backend_identity,
        "trl-sft-plan/v1",
        payload,
    )


def render_spade_plan(
    bundle: TrainerBundleV1,
    root: Path,
    backend_identity: TrainerBackendIdentityV1,
    *,
    store_root: Path | None = None,
) -> RenderedTrainerPlanV1:
    """Render adapt-only paired SPADE shape; never import or invoke SPADE."""

    if backend_identity.name != "spade-external-consumer":
        raise ValueError("SPADE renderer requires a spade-external-consumer backend identity")
    checkpoint = _render_context(bundle, root, "spade", backend_identity, store_root)
    verifier_digest = bundle.objective.verifier_contract_digest
    if verifier_digest is None:
        raise AssertionError("compatible SPADE plan requires verifier identity")
    pair_id = compute_prefixed_sha256(
        {
            "kind": "spade-hint-pair/v1",
            "trainer_bundle_digest": trainer_bundle_digest(bundle),
            "seed": bundle.seed,
        }
    )
    payload = SpadePlanPayloadV1(
        pair_id=pair_id,
        arms=(
            SpadeArmV1(arm="hinted", hint_available=True),
            SpadeArmV1(arm="unhinted", hint_available=False),
        ),
        episode_path=bundle.dataset.train_split.path,
        checkpoint_path=checkpoint.path,
        verifier_contract_digest=verifier_digest,
        result_manifest_path=bundle.result_contract.manifest_path,
    )
    return _bind_rendered_plan(
        bundle,
        "spade",
        backend_identity,
        "spade-shaped-plan/v1",
        payload,
    )


__all__ = [
    "BackendIncompatibilityV1",
    "ExpectedTrainerResultV1",
    "IncompatibilityCode",
    "RenderedTrainerPlanV1",
    "SpadePlanPayloadV1",
    "TRLPlanPayloadV1",
    "TrainerArtifactRefV1",
    "TrainerAuthorityGateV1",
    "TrainerBackendIdentityV1",
    "TrainerBackendRequirementsV1",
    "TrainerBundleRefusal",
    "TrainerBundleV1",
    "TrainerDatasetBindingV1",
    "TrainerEvaluationSetV1",
    "TrainerExporterBindingV1",
    "TrainerHyperparametersV1",
    "TrainerModelIdentityV1",
    "TrainerObjectiveV1",
    "TrainerRenderingContractV1",
    "TrainerResultContractV1",
    "TrainerRevisionIdentityV1",
    "TrainerSourceBindingV1",
    "TrainerTaskIdentityV1",
    "TrainerSplitBindingV1",
    "ValidatedTrainerBundleV1",
    "backend_incompatibilities",
    "expected_trainer_result_has_parity",
    "project_expected_trainer_result",
    "render_spade_plan",
    "render_trl_plan",
    "trainer_bundle_digest",
    "trainer_evaluation_suite_digest",
    "trainer_config_digest",
    "trainer_plan_digest",
    "trainer_task_set_digest",
    "validate_trainer_bundle",
]
