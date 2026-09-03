"""Deterministic, backend-neutral training-example export.

Only normalized, post-redaction fixture evidence with authoritative task-registry
and trial-admissibility bindings is accepted. Legacy evidence is never inferred
into the contract: every failure becomes a typed exclusion without retaining
message content.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import ConfigDict, Field, JsonValue, model_validator

from evallab.evidence_store import EvidenceLocator, materialize_evidence
from evallab.explorer import redact_text
from evallab.interpretation.feature_registry import TRAJECTORY_FEATURE_REGISTRY
from evallab.registry import task_registry_record_digest
from evallab.schemas import ContractModel, TaskRegistryRecord, TrialAdmissibilityV1

TRAINING_EXPORTER_NAME = "evallab.training_export"
TRAINING_EXPORTER_VERSION = "1"
MANIFEST_FILENAME = "manifest.json"
EXCLUSIONS_FILENAME = "exclusions.jsonl"
_SPLITS: tuple[TrainingSplit, ...] = ("train", "validation", "test")
_SPLIT_FILENAMES: dict[TrainingSplit, str] = {
    "train": "train.jsonl",
    "validation": "validation.jsonl",
    "test": "test.jsonl",
}

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CAS_URI = re.compile(r"^cas://sha256/[0-9a-f]{64}$")
_HIDDEN_VERIFIER_TEXT = re.compile(
    r"(?i)(?:^|[\s'\"`])(?:/?tests/|/?solution/)|hidden[-_ ]verifier|"
    r"verifier[-_ ]secret|reference[-_ ]answer|answer[-_ ]key"
)
_REDACTION_MARKER = re.compile(r"<<evallab-redacted:")
_PROHIBITED_CORPORA = frozenset({"syn-funcdag-easy"})
_TRAINER_ONLY_KEYS = frozenset(
    {
        "labels",
        "label_mask",
        "logprobs",
        "loss_mask",
        "rewards",
        "token_ids",
        "trainer_config",
    }
)


def _contains_trainer_only_key(value: JsonValue | dict[str, Any]) -> bool:
    if isinstance(value, dict):
        return any(
            key in _TRAINER_ONLY_KEYS or _contains_trainer_only_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_trainer_only_key(child) for child in value)
    return False


TrainingRepresentation = Literal["prompt_response_sft", "episode_steps"]
TrainingSplit = Literal["train", "validation", "test"]
TrainingExclusionReason = Literal[
    "admissibility_identity_mismatch",
    "ambiguous_latest_history",
    "capture_incomplete",
    "cluster_split_conflict",
    "duplicate_content",
    "empty_conversation",
    "environment_integrity_failed",
    "evaluator_missing",
    "hidden_verifier_leakage",
    "inadmissible",
    "invalid_lineage_digest",
    "invalid_message_sequence",
    "invalid_source_digest",
    "invalid_source_cas_uri",
    "source_cas_digest_mismatch",
    "invalid_tool_linkage",
    "lineage_digest_mismatch",
    "missing_admissibility",
    "missing_trusted_provenance",
    "missing_assistant_response",
    "missing_registry_record",
    "prohibited_corpus",
    "quarantined_feature",
    "redacted_content_unavailable",
    "canonical_set_mismatch",
    "registry_identity_mismatch",
    "reward_only_without_semantic_evidence",
    "receipt_admissibility_unavailable",
    "receipt_contradiction",
    "receipt_digest_mismatch",
    "secret_detected",
    "source_digest_mismatch",
    "source_path_mismatch",
    "truncated_terminal_span",
    "source_bytes_mismatch",
    "trainer_only_material",
    "superseded_history",
    "training_use_not_allowed",
    "unredacted_prompt",
    "unregistered_feature",
    "unsafe_source_path",
    "unsafe_lineage_path",
    "untrusted_provenance",
]


class _FrozenContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TrainingFunctionDefinition(_FrozenContract):
    """Standard OpenAI function-tool definition with a JSON Schema object."""

    name: str = Field(min_length=1)
    description: str | None = None
    parameters: dict[str, JsonValue]

    @model_validator(mode="after")
    def parameters_are_object_schema(self) -> TrainingFunctionDefinition:
        if self.parameters.get("type") != "object":
            raise ValueError("function tool parameters must be a JSON Schema object")
        return self


class TrainingTool(_FrozenContract):
    type: Literal["function"] = "function"
    function: TrainingFunctionDefinition


class TrainingFunctionCall(_FrozenContract):
    name: str = Field(min_length=1)
    arguments: str


class TrainingToolCall(_FrozenContract):
    id: str = Field(min_length=1)
    type: Literal["function"] = "function"
    function: TrainingFunctionCall


class TrainingMessage(_FrozenContract):
    """One normalized, post-redaction OpenAI-compatible conversational step."""

    sequence: int = Field(ge=0, strict=True)
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[TrainingToolCall, ...] = ()
    visibility: Literal["public", "hidden_verifier", "evaluator_only"] = "public"

    @model_validator(mode="after")
    def tool_fields_match_role(self) -> TrainingMessage:
        if self.tool_call_id is not None and self.role != "tool":
            raise ValueError("tool_call_id is only valid on tool messages")
        if self.role == "tool" and self.tool_call_id is None:
            raise ValueError("tool messages require tool_call_id")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("tool_calls are only valid on assistant messages")
        return self


class TrainingReceiptSourceV1(_FrozenContract):
    source_kind: Literal["lineage", "trajectory"]
    path: str
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def path_is_canonical(self) -> TrainingReceiptSourceV1:
        if not _safe_evidence_path(self.path):
            raise ValueError("receipt source path must be canonical evidence")
        return self


class TrainingSourceReceiptV1(_FrozenContract):
    """Independent CAS anchors for source bytes and the G7 authority record."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    schema_version: Literal["training-source-receipt/v1"] = "training-source-receipt/v1"
    cas_record_kind: Literal["source-receipt"] = "source-receipt"
    cas_record_id: str = Field(min_length=1)
    cas_record_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    cas_content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evidence_locator: EvidenceLocator
    admissibility_record_path: str
    admissibility_record_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_digests: tuple[TrainingReceiptSourceV1, ...]
    consumer_name: Literal["evallab.training_export"] = TRAINING_EXPORTER_NAME
    consumer_version: Literal["1"] = TRAINING_EXPORTER_VERSION
    consumer_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    created_at: datetime

    @model_validator(mode="after")
    def anchors_are_canonical(self) -> TrainingSourceReceiptV1:
        locator = self.evidence_locator
        if (
            locator.kind != self.cas_record_kind
            or locator.record_id != self.cas_record_id
            or locator.expected_record_digest != self.cas_record_digest
            or locator.expected_content_digest != self.cas_content_digest
        ):
            raise ValueError("receipt CAS anchors do not match evidence locator")
        if not _safe_evidence_path(self.admissibility_record_path):
            raise ValueError("admissibility record path must be canonical evidence")
        kinds = tuple(source.source_kind for source in self.source_digests)
        if kinds != ("lineage", "trajectory"):
            raise ValueError("receipt source_digests must be sorted and complete")
        paths = (
            self.admissibility_record_path,
            *(source.path for source in self.source_digests),
        )
        if len(paths) != len(set(paths)):
            raise ValueError("receipt evidence paths must be distinct")
        if self.consumer_digest != _digest_bytes(Path(__file__).read_bytes()):
            raise ValueError("receipt consumer digest does not match exporter")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() != UTC.utcoffset(
            self.created_at
        ):
            raise ValueError("receipt created_at must be UTC")
        return self


class NormalizedTrainingEvidence(_FrozenContract):
    """One candidate source with independently anchored CAS provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    job_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    benchmark_family: str = Field(min_length=1)
    task_family: str = Field(min_length=1)
    corpus_id: str = Field(min_length=1)
    split: TrainingSplit
    cluster_key: str = Field(min_length=1)
    history_key: str = Field(min_length=1)
    history_revision: int = Field(ge=0, strict=True)
    source_path: str = Field(min_length=1)
    source_artifact_digest: str | None
    source_cas_uri: str | None
    lineage_path: str = Field(min_length=1)
    lineage_digest: str | None
    source_receipt: TrainingSourceReceiptV1 | None
    registry_record: TaskRegistryRecord | None
    admissibility: TrialAdmissibilityV1 | None
    capture_status: Literal["complete", "gapped", "missing"]
    environment_integrity: Literal["passed", "failed", "unknown"]
    evaluator_status: Literal["present", "missing"]
    semantic_evidence_status: Literal["complete", "reward_only", "missing"]
    redaction_status: Literal["redacted", "unredacted", "unknown"]
    terminal_span_status: Literal["complete", "truncated"] = "complete"
    feature_names: tuple[str, ...] = ()
    tools: tuple[TrainingTool, ...] = ()
    messages: tuple[TrainingMessage, ...]

    @model_validator(mode="after")
    def logical_sets_are_canonical(self) -> NormalizedTrainingEvidence:
        if self.feature_names != tuple(sorted(set(self.feature_names))):
            raise ValueError("feature_names must be sorted and unique")
        tool_names = tuple(tool.function.name for tool in self.tools)
        if tool_names != tuple(sorted(set(tool_names))):
            raise ValueError("tools must be sorted by unique function name")
        return self


class TrainingSourceBinding(_FrozenContract):
    job_id: str
    trial_id: str
    task_id: str
    benchmark_family: str
    task_family: str
    corpus_id: str
    split: TrainingSplit
    cluster_key: str
    history_key: str
    history_revision: int
    source_path: str
    source_artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_cas_uri: str = Field(pattern=r"^cas://sha256/[0-9a-f]{64}$")
    lineage_path: str
    lineage_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evidence_kind: Literal["source-receipt"]
    evidence_record_id: str = Field(min_length=1, pattern=r"^[^/\x00]+$")
    evidence_record_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evidence_content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    trial_admissibility_record_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    registry_allowed_use: Literal["training"] = "training"
    task_registry_record_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    trial_admissibility_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    trial_admissibility_decision: Literal["admissible", "rejected", "unavailable"]
    trial_analysis_eligibility: Literal["causal-eligible", "calibration-only"]
    trial_admissibility_allowed_use: Literal["causal", "descriptive-only"]
    extractor_name: Literal["evallab.training_export"] = TRAINING_EXPORTER_NAME
    extractor_version: Literal["1"] = TRAINING_EXPORTER_VERSION

    @model_validator(mode="after")
    def authority_is_accepted(self) -> TrainingSourceBinding:
        if (
            self.trial_admissibility_decision != "admissible"
            or self.trial_analysis_eligibility != "causal-eligible"
            or self.trial_admissibility_allowed_use != "causal"
        ):
            raise ValueError("training source binding requires accepted causal authority")
        if self.evidence_record_id in {".", ".."}:
            raise ValueError("training source evidence_record_id is not canonical")
        if self.source_cas_uri.removeprefix(
            "cas://sha256/"
        ) != self.evidence_content_digest.removeprefix("sha256:"):
            raise ValueError("training source CAS URI must bind evidence_content_digest")
        if not _safe_source_path(self.source_path):
            raise ValueError("training source path must be safe normalized agent evidence")
        if not _safe_evidence_path(self.lineage_path):
            raise ValueError("training lineage path must be safe normalized evidence")
        return self


class TrainingExampleRecord(_FrozenContract):
    schema_version: Literal["training-example/v1"] = "training-example/v1"
    example_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    representation: TrainingRepresentation
    source: TrainingSourceBinding
    payload: dict[str, Any]

    @model_validator(mode="after")
    def payload_is_canonical_and_bound(self) -> TrainingExampleRecord:
        required = (
            {"prompt", "response"} if self.representation == "prompt_response_sft" else {"steps"}
        )
        allowed = required | {"tools"}
        if not required <= self.payload.keys() or not self.payload.keys() <= allowed:
            raise ValueError("training payload does not match its representation")
        if _contains_trainer_only_key(self.payload):
            raise ValueError("canonical training records cannot contain trainer-only material")
        expected_content = _digest_json(
            {"representation": self.representation, "payload": self.payload}
        )
        expected_example = _digest_json(
            {
                "content_digest": expected_content,
                "source": self.source.model_dump(mode="json"),
            }
        )
        if self.content_digest != expected_content or self.example_id != expected_example:
            raise ValueError("training example content identity mismatch")
        return self


class TrainingExclusionRecord(_FrozenContract):
    schema_version: Literal["training-exclusion/v1"] = "training-exclusion/v1"
    job_id: str
    trial_id: str
    task_id: str
    benchmark_family: str
    task_family: str
    corpus_id: str
    split: TrainingSplit
    cluster_key: str
    history_key: str
    history_revision: int
    source_path: str
    source_artifact_digest: str | None
    registry_record_digest: str | None
    admissibility_digest: str | None
    representation: TrainingRepresentation | None = None
    content_digest: str | None = None
    retained_example_id: str | None = None
    reasons: tuple[TrainingExclusionReason, ...]
    details: tuple[str, ...] = ()
    extractor_name: Literal["evallab.training_export"] = TRAINING_EXPORTER_NAME
    extractor_version: Literal["1"] = TRAINING_EXPORTER_VERSION

    @model_validator(mode="after")
    def logical_sets_are_canonical(self) -> TrainingExclusionRecord:
        if self.reasons != tuple(sorted(set(self.reasons))) or self.details != tuple(
            sorted(set(self.details))
        ):
            raise ValueError("canonical_set_mismatch: exclusion sets")
        return self


class TrainingSourceRefV1(_FrozenContract):
    job_id: str
    trial_id: str
    source_path: str
    source_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_cas_uri: str = Field(pattern=r"^cas://sha256/[0-9a-f]{64}$")
    lineage_path: str
    lineage_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evidence_kind: Literal["source-receipt"]
    evidence_record_id: str = Field(min_length=1, pattern=r"^[^/\x00]+$")
    evidence_record_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evidence_content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    trial_admissibility_record_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    registry_allowed_use: Literal["training"] = "training"
    task_registry_record_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    trial_admissibility_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    trial_admissibility_decision: Literal["admissible", "rejected", "unavailable"]
    trial_analysis_eligibility: Literal["causal-eligible", "calibration-only"]
    trial_admissibility_allowed_use: Literal["causal", "descriptive-only"]

    @model_validator(mode="after")
    def authority_is_accepted(self) -> TrainingSourceRefV1:
        if (
            self.trial_admissibility_decision != "admissible"
            or self.trial_analysis_eligibility != "causal-eligible"
            or self.trial_admissibility_allowed_use != "causal"
        ):
            raise ValueError("training source ref requires accepted causal authority")
        if self.evidence_record_id in {".", ".."}:
            raise ValueError("training source ref evidence_record_id is not canonical")
        if self.source_cas_uri.removeprefix(
            "cas://sha256/"
        ) != self.evidence_content_digest.removeprefix("sha256:"):
            raise ValueError("training source ref CAS URI must bind evidence_content_digest")
        if not _safe_source_path(self.source_path) or not _safe_evidence_path(self.lineage_path):
            raise ValueError("training source ref paths must be canonical evidence paths")
        return self


class TrainingExporterIdentityV1(_FrozenContract):
    name: Literal["evallab.training_export"] = TRAINING_EXPORTER_NAME
    version: Literal["1"] = TRAINING_EXPORTER_VERSION
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class TrainingSplitRefV1(_FrozenContract):
    path: str
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    cluster_key_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    record_count: int = Field(ge=0)

    @model_validator(mode="after")
    def path_is_safe_relative(self) -> TrainingSplitRefV1:
        if not _safe_relative_path(self.path):
            raise ValueError("training split path must be canonical and relative")
        return self


class TrainingDatasetManifestV1(_FrozenContract):
    """Immutable boundary consumed by backend-neutral trainer bundles."""

    schema_version: Literal["training-dataset-manifest/v1"] = "training-dataset-manifest/v1"
    manifest_path: str | None = MANIFEST_FILENAME
    cas_uri: str | None = None
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    dataset_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    train_split: TrainingSplitRefV1
    validation_split: TrainingSplitRefV1
    test_split: TrainingSplitRefV1
    source_refs: tuple[TrainingSourceRefV1, ...]
    exporter: TrainingExporterIdentityV1
    benchmark_families: tuple[str, ...]
    task_families: tuple[str, ...]
    environment_integrity: Literal["passed"] = "passed"
    capture_complete: Literal[True] = True
    redaction_status: Literal["redacted"] = "redacted"
    registry_allowed_use: Literal["training"] = "training"
    exclusions_path: str = EXCLUSIONS_FILENAME
    exclusions_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    exclusion_count: int = Field(ge=0)
    representation_counts: dict[TrainingRepresentation, int]

    @model_validator(mode="after")
    def identity_and_digest_match(self) -> TrainingDatasetManifestV1:
        if (self.manifest_path is None) == (self.cas_uri is None):
            raise ValueError("exactly one of manifest_path or cas_uri is required")
        if self.manifest_path is not None and self.manifest_path != MANIFEST_FILENAME:
            raise ValueError("manifest_path must use the exact canonical path")
        if self.cas_uri is not None:
            if not _CAS_URI.fullmatch(self.cas_uri):
                raise ValueError("cas_uri must be a canonical sha256 CAS URI")
            if self.cas_uri.removeprefix("cas://sha256/") != self.manifest_digest.removeprefix(
                "sha256:"
            ):
                raise ValueError("manifest CAS URI must bind manifest_digest")
        if self.exclusions_path != EXCLUSIONS_FILENAME:
            raise ValueError("exclusions_path must use the exact canonical path")
        if (
            self.train_split.path,
            self.validation_split.path,
            self.test_split.path,
        ) != (
            _SPLIT_FILENAMES["train"],
            _SPLIT_FILENAMES["validation"],
            _SPLIT_FILENAMES["test"],
        ):
            raise ValueError("split paths must use the exact canonical paths")
        if self.benchmark_families != tuple(sorted(set(self.benchmark_families))):
            raise ValueError("canonical_set_mismatch: benchmark_families")
        if self.task_families != tuple(sorted(set(self.task_families))):
            raise ValueError("canonical_set_mismatch: task_families")
        expected_source_refs = tuple(
            sorted(
                self.source_refs,
                key=lambda source: (
                    source.job_id,
                    source.trial_id,
                    source.source_digest,
                ),
            )
        )
        if self.source_refs != expected_source_refs:
            raise ValueError("canonical_set_mismatch: source_refs")
        source_identities = {
            (source.job_id, source.trial_id, source.source_digest) for source in self.source_refs
        }
        if len(source_identities) != len(self.source_refs):
            raise ValueError("canonical_set_mismatch: duplicate source_refs")
        if tuple(self.representation_counts) != (
            "prompt_response_sft",
            "episode_steps",
        ) or any(count < 0 for count in self.representation_counts.values()):
            raise ValueError("canonical_set_mismatch: representation_counts")
        expected_dataset_digest = _digest_json(
            {
                "train_split": self.train_split.model_dump(mode="json"),
                "validation_split": self.validation_split.model_dump(mode="json"),
                "test_split": self.test_split.model_dump(mode="json"),
                "exclusions_digest": self.exclusions_digest,
            }
        )
        if self.dataset_digest != expected_dataset_digest:
            raise ValueError("training dataset digest mismatch")
        record_count = (
            self.train_split.record_count
            + self.validation_split.record_count
            + self.test_split.record_count
        )
        if sum(self.representation_counts.values()) != record_count:
            raise ValueError("representation counts do not match split record counts")
        if self.exporter.digest != _digest_bytes(Path(__file__).read_bytes()):
            raise ValueError("manifest exporter digest does not match implementation")
        body = self.model_dump(mode="json", exclude={"manifest_digest", "cas_uri"})
        if self.manifest_digest != _digest_json(body):
            raise ValueError("training manifest digest mismatch")
        return self


@dataclass(frozen=True)
class TrainingExportResult:
    root: Path
    manifest_path: Path
    exclusions_path: Path
    split_paths: dict[TrainingSplit, Path]
    manifest: TrainingDatasetManifestV1
    records: tuple[TrainingExampleRecord, ...]
    exclusions: tuple[TrainingExclusionRecord, ...]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _digest_json(value: Any) -> str:
    return _digest_bytes(_canonical_json(value))


def _jsonl(records: tuple[_FrozenContract, ...]) -> bytes:
    return b"".join(
        _canonical_json(record.model_dump(mode="json", exclude_none=False)) + b"\n"
        for record in records
    )


def _safe_relative_path(value: str) -> bool:
    if not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and value == path.as_posix() and ".." not in path.parts


def _safe_source_path(value: str) -> bool:
    if not _safe_relative_path(value):
        return False
    path = PurePosixPath(value)
    return (
        bool(path.parts)
        and path.parts[0] == "agent"
        and not any(part in {"tests", "solution"} for part in path.parts)
    )


def _safe_evidence_path(value: str) -> bool:
    if not _safe_relative_path(value):
        return False
    return not any(part in {"tests", "solution"} for part in PurePosixPath(value).parts)


def _prohibited(source: NormalizedTrainingEvidence) -> bool:
    return any(
        value in _PROHIBITED_CORPORA
        or any(value.endswith(f"/{corpus}") for corpus in _PROHIBITED_CORPORA)
        for value in (source.corpus_id, source.task_id)
    )


def _safe_exclusion_path(value: str) -> str:
    if _safe_source_path(value) and redact_text(value) == value:
        return value
    return f"unsafe-path-{hashlib.sha256(value.encode()).hexdigest()}"


def _sensitive_text(value: str) -> bool:
    return (
        _HIDDEN_VERIFIER_TEXT.search(value) is not None
        or redact_text(value) != value
        or _REDACTION_MARKER.search(value) is not None
    )


def _safe_metadata(value: str) -> str:
    if not _sensitive_text(value):
        return value
    return f"redacted-metadata-{hashlib.sha256(value.encode()).hexdigest()}"


def _provenance_reasons(
    source: NormalizedTrainingEvidence,
) -> list[TrainingExclusionReason]:
    receipt = source.source_receipt
    if receipt is None:
        return ["missing_trusted_provenance"]
    if (
        not _safe_source_path(source.source_path)
        or not _safe_evidence_path(source.lineage_path)
        or source.source_artifact_digest is None
        or not _DIGEST.fullmatch(source.source_artifact_digest)
        or source.lineage_digest is None
        or not _DIGEST.fullmatch(source.lineage_digest)
        or source.source_cas_uri is None
        or not _CAS_URI.fullmatch(source.source_cas_uri)
    ):
        return []
    reasons: list[TrainingExclusionReason] = []
    locator = receipt.evidence_locator
    kinds = tuple(item.source_kind for item in receipt.source_digests)
    if kinds != ("lineage", "trajectory"):
        reasons.append("canonical_set_mismatch")
        return reasons
    receipt_paths = (
        receipt.admissibility_record_path,
        *(item.path for item in receipt.source_digests),
    )
    if (
        receipt.cas_record_kind != "source-receipt"
        or locator.kind != receipt.cas_record_kind
        or locator.record_id != receipt.cas_record_id
        or locator.expected_record_digest != receipt.cas_record_digest
        or locator.expected_content_digest != receipt.cas_content_digest
        or receipt.consumer_name != TRAINING_EXPORTER_NAME
        or receipt.consumer_version != TRAINING_EXPORTER_VERSION
        or receipt.consumer_digest != _digest_bytes(Path(__file__).read_bytes())
        or not _safe_evidence_path(receipt.admissibility_record_path)
        or len(receipt_paths) != len(set(receipt_paths))
        or any(not _safe_evidence_path(path) for path in receipt_paths)
    ):
        reasons.append("receipt_contradiction")
    expected_uri = f"cas://sha256/{receipt.cas_content_digest.removeprefix('sha256:')}"
    if source.source_cas_uri != expected_uri:
        reasons.append("source_cas_digest_mismatch")
    receipt_sources = {item.source_kind: item for item in receipt.source_digests}
    trajectory = receipt_sources["trajectory"]
    lineage = receipt_sources["lineage"]
    if (
        source.source_path != trajectory.path
        or source.lineage_path != lineage.path
        or source.source_artifact_digest != trajectory.digest
        or source.lineage_digest != lineage.digest
    ):
        reasons.append("receipt_contradiction")
    try:
        with materialize_evidence(receipt.evidence_locator) as root:
            actual_sources: dict[str, str] = {}
            for item in receipt.source_digests:
                path = root.joinpath(*PurePosixPath(item.path).parts)
                if not path.is_file():
                    raise FileNotFoundError(item.path)
                actual_sources[item.source_kind] = _digest_bytes(path.read_bytes())
            if any(
                actual_sources[item.source_kind] != item.digest for item in receipt.source_digests
            ):
                reasons.append("receipt_digest_mismatch")
            authority_path = root.joinpath(*PurePosixPath(receipt.admissibility_record_path).parts)
            if not authority_path.is_file():
                reasons.append("receipt_admissibility_unavailable")
            else:
                authority_bytes = authority_path.read_bytes()
                if _digest_bytes(authority_bytes) != receipt.admissibility_record_digest:
                    reasons.append("receipt_digest_mismatch")
                try:
                    authority = TrialAdmissibilityV1.model_validate_json(authority_bytes)
                except ValueError:
                    reasons.append("receipt_admissibility_unavailable")
                else:
                    canonical_authority = _canonical_json(authority.model_dump(mode="json")) + b"\n"
                    if authority_bytes != canonical_authority:
                        reasons.append("receipt_digest_mismatch")
                    if source.admissibility is None or authority != source.admissibility:
                        reasons.append("receipt_contradiction")
                    if authority.source_digests.trajectory != actual_sources.get("trajectory"):
                        reasons.append("receipt_contradiction")
    except OSError:
        reasons.append("untrusted_provenance")
    except ValueError:
        reasons.append("receipt_digest_mismatch")
    return reasons


def _valid_tool_linkage(source: NormalizedTrainingEvidence) -> bool:
    declared_tools = {tool.function.name for tool in source.tools}
    pending: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    for message in source.messages:
        if pending and message.role != "tool":
            return False
        if message.role == "assistant":
            call_ids = tuple(call.id for call in message.tool_calls)
            if call_ids != tuple(sorted(set(call_ids))):
                return False
            for call in message.tool_calls:
                if call.id in seen_ids or call.function.name not in declared_tools:
                    return False
                try:
                    arguments = json.loads(call.function.arguments)
                except json.JSONDecodeError:
                    return False
                if (
                    not isinstance(arguments, dict)
                    or _canonical_json(arguments).decode("utf-8") != call.function.arguments
                ):
                    return False
                seen_ids.add(call.id)
                pending.append((call.id, call.function.name))
        elif message.role == "tool":
            if not pending or message.tool_call_id != pending[0][0]:
                return False
            _, function_name = pending.pop(0)
            if message.name is not None and message.name != function_name:
                return False
    return not pending


def _source_details(source: NormalizedTrainingEvidence) -> dict[str, Any]:
    registry_digest = (
        task_registry_record_digest(source.registry_record)
        if source.registry_record is not None
        else None
    )
    return {
        "job_id": _safe_metadata(source.job_id),
        "trial_id": _safe_metadata(source.trial_id),
        "task_id": _safe_metadata(source.task_id),
        "benchmark_family": _safe_metadata(source.benchmark_family),
        "task_family": _safe_metadata(source.task_family),
        "corpus_id": _safe_metadata(source.corpus_id),
        "split": source.split,
        "cluster_key": _safe_metadata(source.cluster_key),
        "history_key": _safe_metadata(source.history_key),
        "history_revision": source.history_revision,
        "source_path": _safe_metadata(_safe_exclusion_path(source.source_path)),
        "source_artifact_digest": (
            source.source_artifact_digest
            if source.source_artifact_digest is None
            or _DIGEST.fullmatch(source.source_artifact_digest)
            else _safe_metadata(source.source_artifact_digest)
        ),
        "registry_record_digest": registry_digest,
        "admissibility_digest": (
            source.admissibility.admissibility_digest if source.admissibility else None
        ),
    }


def _exclusion(
    source: NormalizedTrainingEvidence,
    reasons: list[TrainingExclusionReason],
    *,
    details: list[str] | None = None,
    representation: TrainingRepresentation | None = None,
    content_digest: str | None = None,
    retained_example_id: str | None = None,
) -> TrainingExclusionRecord:
    return TrainingExclusionRecord(
        **_source_details(source),
        representation=representation,
        content_digest=content_digest,
        retained_example_id=retained_example_id,
        reasons=tuple(sorted(set(reasons))),
        details=tuple(sorted({_safe_metadata(detail) for detail in details or ()})),
    )


def _gate_source(
    source: NormalizedTrainingEvidence,
) -> tuple[list[TrainingExclusionReason], list[str]]:
    reasons: list[TrainingExclusionReason] = []
    details: list[str] = []
    registry_record = source.registry_record
    for value in (
        source.job_id,
        source.trial_id,
        source.task_id,
        source.benchmark_family,
        source.task_family,
        source.corpus_id,
        source.cluster_key,
        source.history_key,
        source.source_path,
        source.lineage_path,
    ):
        if _HIDDEN_VERIFIER_TEXT.search(value):
            reasons.append("hidden_verifier_leakage")
        if redact_text(value) != value:
            reasons.append("secret_detected")
        if _REDACTION_MARKER.search(value):
            reasons.append("redacted_content_unavailable")
    registry_digest: str | None = None
    if registry_record is None:
        reasons.append("missing_registry_record")
    else:
        try:
            validated_registry = TaskRegistryRecord.model_validate(
                registry_record.model_dump(mode="json")
            )
        except ValueError:
            reasons.append("registry_identity_mismatch")
        else:
            if validated_registry != registry_record:
                reasons.append("registry_identity_mismatch")
        registry_digest = task_registry_record_digest(registry_record)
        if (
            registry_record.task_id != source.task_id
            or registry_record.task_family != source.task_family
            or registry_record.state != "registered"
        ):
            reasons.append("registry_identity_mismatch")
        if "training" not in registry_record.allowed_uses:
            reasons.append("training_use_not_allowed")

    admissibility = source.admissibility
    if admissibility is None:
        reasons.append("missing_admissibility")
    else:
        if (
            admissibility.decision != "admissible"
            or admissibility.analysis_eligibility != "causal-eligible"
            or admissibility.allowed_use != "causal"
        ):
            reasons.append("inadmissible")
            details.append(admissibility.reason)
        if admissibility.trial_id != source.trial_id:
            reasons.append("admissibility_identity_mismatch")
        runtime_identity = admissibility.task_runtime_identity
        if (
            runtime_identity is None
            or runtime_identity.task_id != source.task_id
            or runtime_identity.registry_record_digest != registry_digest
        ):
            reasons.append("admissibility_identity_mismatch")
        if admissibility.source_digests.trajectory != source.source_artifact_digest:
            reasons.append("source_digest_mismatch")
        source_paths = admissibility.source_paths
        if source_paths is None or source.source_path not in source_paths.trajectory:
            reasons.append("source_path_mismatch")
    if source.source_artifact_digest is None or not _DIGEST.fullmatch(
        source.source_artifact_digest
    ):
        reasons.append("invalid_source_digest")
    if source.source_cas_uri is None or not _CAS_URI.fullmatch(source.source_cas_uri):
        reasons.append("invalid_source_cas_uri")
    if source.lineage_digest is None or not _DIGEST.fullmatch(source.lineage_digest):
        reasons.append("invalid_lineage_digest")
    if not _safe_source_path(source.source_path):
        reasons.append("unsafe_source_path")
    if not _safe_evidence_path(source.lineage_path):
        reasons.append("unsafe_lineage_path")
    reasons.extend(_provenance_reasons(source))
    if source.capture_status != "complete":
        reasons.append("capture_incomplete")
    if source.environment_integrity != "passed":
        reasons.append("environment_integrity_failed")
    if source.evaluator_status != "present":
        reasons.append("evaluator_missing")
    if source.semantic_evidence_status != "complete":
        reasons.append("reward_only_without_semantic_evidence")
    if source.redaction_status != "redacted":
        reasons.append("unredacted_prompt")
    if _prohibited(source):
        reasons.append("prohibited_corpus")
    if source.terminal_span_status != "complete":
        reasons.append("truncated_terminal_span")
    if not source.messages:
        reasons.append("empty_conversation")
    sequences = [message.sequence for message in source.messages]
    if sequences != list(range(len(source.messages))):
        reasons.append("invalid_message_sequence")
    if not _valid_tool_linkage(source):
        reasons.append("invalid_tool_linkage")
    if not any(message.role == "assistant" for message in source.messages):
        reasons.append("missing_assistant_response")
    for message in source.messages:
        if message.visibility != "public":
            reasons.append("hidden_verifier_leakage")
        serialized_message = _canonical_json(message.model_dump(mode="json")).decode("utf-8")
        if _HIDDEN_VERIFIER_TEXT.search(serialized_message):
            reasons.append("hidden_verifier_leakage")
        if redact_text(serialized_message) != serialized_message:
            reasons.append("secret_detected")
        if _REDACTION_MARKER.search(serialized_message):
            reasons.append("redacted_content_unavailable")
    for tool in source.tools:
        tool_payload = tool.model_dump(mode="json")
        serialized_tool = _canonical_json(tool_payload).decode("utf-8")
        if _HIDDEN_VERIFIER_TEXT.search(serialized_tool):
            reasons.append("hidden_verifier_leakage")
        if redact_text(serialized_tool) != serialized_tool:
            reasons.append("secret_detected")
        if _REDACTION_MARKER.search(serialized_tool):
            reasons.append("redacted_content_unavailable")
        if _contains_trainer_only_key(tool_payload):
            reasons.append("trainer_only_material")
    for name in source.feature_names:
        feature = TRAJECTORY_FEATURE_REGISTRY.get(name)
        if feature is None:
            reasons.append("unregistered_feature")
            details.append(
                f"unregistered feature digest: {hashlib.sha256(name.encode()).hexdigest()}"
            )
        elif feature.is_quarantined():
            reasons.append("quarantined_feature")
            details.append(f"quarantined feature: {name} ({feature.quarantine_reason})")
    return reasons, details


def _source_content_digest(source: NormalizedTrainingEvidence) -> str:
    return _digest_json(
        {
            "task_id": source.task_id,
            "benchmark_family": source.benchmark_family,
            "task_family": source.task_family,
            "split": source.split,
            "cluster_key": source.cluster_key,
            "source_artifact_digest": source.source_artifact_digest,
            "lineage_digest": source.lineage_digest,
            "messages": [message.model_dump(mode="json") for message in source.messages],
            "tools": [tool.model_dump(mode="json") for tool in source.tools],
        }
    )


def _latest_history(
    sources: list[NormalizedTrainingEvidence],
) -> tuple[list[NormalizedTrainingEvidence], list[TrainingExclusionRecord]]:
    grouped: dict[str, list[NormalizedTrainingEvidence]] = defaultdict(list)
    for source in sources:
        grouped[source.history_key].append(source)
    selected: list[NormalizedTrainingEvidence] = []
    exclusions: list[TrainingExclusionRecord] = []
    for history_key in sorted(grouped):
        group = grouped[history_key]
        latest_revision = max(source.history_revision for source in group)
        latest = [source for source in group if source.history_revision == latest_revision]
        for source in group:
            if source.history_revision != latest_revision:
                exclusions.append(_exclusion(source, ["superseded_history"]))
        identities = {_source_content_digest(source) for source in latest}
        if len(identities) != 1:
            exclusions.extend(_exclusion(source, ["ambiguous_latest_history"]) for source in latest)
            continue
        selected.extend(latest)
    return selected, exclusions


def _source_binding(source: NormalizedTrainingEvidence) -> TrainingSourceBinding:
    assert source.source_artifact_digest is not None
    assert source.source_cas_uri is not None
    assert source.lineage_digest is not None
    assert source.source_receipt is not None
    assert source.registry_record is not None
    assert source.admissibility is not None
    receipt = source.source_receipt
    return TrainingSourceBinding(
        job_id=source.job_id,
        trial_id=source.trial_id,
        task_id=source.task_id,
        benchmark_family=source.benchmark_family,
        task_family=source.task_family,
        corpus_id=source.corpus_id,
        split=source.split,
        cluster_key=source.cluster_key,
        history_key=source.history_key,
        history_revision=source.history_revision,
        source_path=source.source_path,
        source_artifact_digest=source.source_artifact_digest,
        source_cas_uri=source.source_cas_uri,
        lineage_path=source.lineage_path,
        lineage_digest=source.lineage_digest,
        evidence_kind=receipt.cas_record_kind,
        evidence_record_id=receipt.cas_record_id,
        evidence_record_digest=receipt.cas_record_digest,
        evidence_content_digest=receipt.cas_content_digest,
        trial_admissibility_record_digest=receipt.admissibility_record_digest,
        registry_allowed_use="training",
        task_registry_record_digest=task_registry_record_digest(source.registry_record),
        trial_admissibility_digest=source.admissibility.admissibility_digest,
        trial_admissibility_decision=source.admissibility.decision,
        trial_analysis_eligibility=source.admissibility.analysis_eligibility,
        trial_admissibility_allowed_use=source.admissibility.allowed_use,
    )


def _candidate_records(
    source: NormalizedTrainingEvidence,
    representations: tuple[TrainingRepresentation, ...],
) -> list[TrainingExampleRecord]:
    ordered = sorted(source.messages, key=lambda message: message.sequence)
    binding = _source_binding(source)
    tools = [tool.model_dump(mode="json") for tool in source.tools]
    candidates: list[TrainingExampleRecord] = []
    for representation in representations:
        payloads: list[dict[str, Any]] = []
        if representation == "episode_steps":
            episode: dict[str, Any] = {
                "steps": [
                    {
                        "step": message.sequence,
                        **message.model_dump(
                            mode="json",
                            exclude={"sequence", "visibility"},
                            exclude_none=True,
                        ),
                    }
                    for message in ordered
                ]
            }
            if tools:
                episode["tools"] = tools
            payloads.append(episode)
        else:
            for index, message in enumerate(ordered):
                if message.role != "assistant":
                    continue
                sft: dict[str, Any] = {
                    "prompt": [
                        prior.model_dump(
                            mode="json",
                            exclude={"sequence", "visibility"},
                            exclude_none=True,
                        )
                        for prior in ordered[:index]
                    ],
                    "response": message.model_dump(
                        mode="json",
                        exclude={"sequence", "visibility"},
                        exclude_none=True,
                    ),
                }
                if tools:
                    sft["tools"] = tools
                payloads.append(sft)
        for payload in payloads:
            content_digest = _digest_json(
                {
                    "representation": representation,
                    "payload": payload,
                }
            )
            example_id = _digest_json(
                {
                    "content_digest": content_digest,
                    "source": binding.model_dump(mode="json"),
                }
            )
            candidates.append(
                TrainingExampleRecord(
                    example_id=example_id,
                    content_digest=content_digest,
                    representation=representation,
                    source=binding,
                    payload=payload,
                )
            )
    return candidates


def _write_staged_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _prepare_output_parent(parent: Path) -> None:
    cursor = parent
    while not os.path.lexists(cursor):
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    if cursor.is_symlink() or cursor.resolve(strict=True) != cursor:
        raise ValueError(f"refusing symlink destination chain: {parent}")
    parent.mkdir(parents=True, exist_ok=True)


def _publish_directory(destination: Path, payloads: dict[str, bytes]) -> Path:
    absolute = Path(os.path.abspath(destination))
    if os.path.lexists(absolute):
        raise FileExistsError(f"training export destination already exists: {destination}")
    parent = absolute.parent
    _prepare_output_parent(parent)
    staged = Path(tempfile.mkdtemp(prefix=f".{absolute.name}.staging-", dir=parent))
    expected = tuple(sorted(payloads))
    for name in expected:
        if not _safe_relative_path(name) or PurePosixPath(name).parent != PurePosixPath("."):
            raise ValueError(f"invalid staged output name: {name}")
        _write_staged_file(staged / name, payloads[name])
    _fsync_directory(staged)
    if os.path.lexists(absolute):
        raise FileExistsError(f"training export destination already exists: {destination}")
    os.rename(staged, absolute)
    _fsync_directory(parent)
    published = tuple(sorted(path.name for path in absolute.iterdir()))
    if published != expected or any(
        path.is_symlink() or not path.is_file() for path in absolute.iterdir()
    ):
        raise RuntimeError("published training export inventory mismatch")
    return absolute


def _split_ref(
    split: TrainingSplit,
    records: tuple[TrainingExampleRecord, ...],
    payload: bytes,
) -> TrainingSplitRefV1:
    cluster_keys = sorted({record.source.cluster_key for record in records})
    return TrainingSplitRefV1(
        path=_SPLIT_FILENAMES[split],
        digest=_digest_bytes(payload),
        cluster_key_digest=_digest_json(cluster_keys),
        record_count=len(records),
    )


def export_training_dataset(
    sources: list[NormalizedTrainingEvidence] | tuple[NormalizedTrainingEvidence, ...],
    destination: Path,
    *,
    representations: tuple[TrainingRepresentation, ...] = (
        "prompt_response_sft",
        "episode_steps",
    ),
) -> TrainingExportResult:
    """Export stable split JSONL, exclusions, and a canonical immutable manifest."""
    if os.path.lexists(Path(os.path.abspath(destination))):
        raise FileExistsError(f"training export destination already exists: {destination}")
    canonical_representations = tuple(
        representation
        for representation in ("prompt_response_sft", "episode_steps")
        if representation in representations
    )
    if not representations or representations != canonical_representations:
        raise ValueError("canonical_set_mismatch: representations")

    initially_valid: list[NormalizedTrainingEvidence] = []
    exclusions: list[TrainingExclusionRecord] = []
    for source in sources:
        reasons, details = _gate_source(source)
        if reasons:
            exclusions.append(_exclusion(source, reasons, details=details))
        else:
            initially_valid.append(source)

    latest, history_exclusions = _latest_history(initially_valid)
    exclusions.extend(history_exclusions)

    splits_by_cluster: dict[str, set[TrainingSplit]] = defaultdict(set)
    for source in latest:
        splits_by_cluster[source.cluster_key].add(source.split)
    split_conflicts = {cluster for cluster, splits in splits_by_cluster.items() if len(splits) > 1}
    selected: list[NormalizedTrainingEvidence] = []
    for source in latest:
        if source.cluster_key in split_conflicts:
            exclusions.append(_exclusion(source, ["cluster_split_conflict"]))
        else:
            selected.append(source)

    source_by_key: dict[tuple[str, str, str, str, int], NormalizedTrainingEvidence] = {}
    candidates: list[TrainingExampleRecord] = []
    for source in selected:
        key = (
            source.job_id,
            source.trial_id,
            source.source_artifact_digest or "",
            source.history_key,
            source.history_revision,
        )
        source_by_key[key] = source
        candidates.extend(_candidate_records(source, representations))

    grouped_records: dict[str, list[TrainingExampleRecord]] = defaultdict(list)
    for record in candidates:
        grouped_records[record.content_digest].append(record)
    records: list[TrainingExampleRecord] = []
    for content_digest in sorted(grouped_records):
        group = sorted(
            grouped_records[content_digest],
            key=lambda record: (
                record.source.job_id,
                record.source.trial_id,
                record.source.source_artifact_digest,
                record.source.history_key,
                record.source.history_revision,
            ),
        )
        retained = group[0]
        records.append(retained)
        for duplicate in group[1:]:
            key = (
                duplicate.source.job_id,
                duplicate.source.trial_id,
                duplicate.source.source_artifact_digest,
                duplicate.source.history_key,
                duplicate.source.history_revision,
            )
            exclusions.append(
                _exclusion(
                    source_by_key[key],
                    ["duplicate_content"],
                    representation=duplicate.representation,
                    content_digest=duplicate.content_digest,
                    retained_example_id=retained.example_id,
                )
            )

    record_tuple = tuple(sorted(records, key=lambda record: record.example_id))
    exclusion_tuple = tuple(
        sorted(
            exclusions,
            key=lambda record: (
                record.job_id,
                record.trial_id,
                record.history_revision,
                record.representation or "",
                record.content_digest or "",
                record.reasons,
            ),
        )
    )
    records_by_split: dict[TrainingSplit, tuple[TrainingExampleRecord, ...]] = {
        split: tuple(record for record in record_tuple if record.source.split == split)
        for split in _SPLITS
    }
    split_payloads = {split: _jsonl(records_by_split[split]) for split in records_by_split}
    split_refs = {
        split: _split_ref(split, records_by_split[split], split_payloads[split])
        for split in records_by_split
    }
    exclusions_bytes = _jsonl(exclusion_tuple)
    exclusions_digest = _digest_bytes(exclusions_bytes)
    dataset_digest = _digest_json(
        {
            "train_split": split_refs["train"].model_dump(mode="json"),
            "validation_split": split_refs["validation"].model_dump(mode="json"),
            "test_split": split_refs["test"].model_dump(mode="json"),
            "exclusions_digest": exclusions_digest,
        }
    )
    unique_sources = {
        _digest_json(record.source.model_dump(mode="json")): record.source
        for record in record_tuple
    }
    source_refs = tuple(
        TrainingSourceRefV1(
            job_id=source.job_id,
            trial_id=source.trial_id,
            source_path=source.source_path,
            source_digest=source.source_artifact_digest,
            source_cas_uri=source.source_cas_uri,
            lineage_path=source.lineage_path,
            lineage_digest=source.lineage_digest,
            evidence_kind=source.evidence_kind,
            evidence_record_id=source.evidence_record_id,
            evidence_record_digest=source.evidence_record_digest,
            evidence_content_digest=source.evidence_content_digest,
            trial_admissibility_record_digest=source.trial_admissibility_record_digest,
            registry_allowed_use=source.registry_allowed_use,
            task_registry_record_digest=source.task_registry_record_digest,
            trial_admissibility_digest=source.trial_admissibility_digest,
            trial_admissibility_decision=source.trial_admissibility_decision,
            trial_analysis_eligibility=source.trial_analysis_eligibility,
            trial_admissibility_allowed_use=source.trial_admissibility_allowed_use,
        )
        for source in sorted(
            unique_sources.values(),
            key=lambda value: (
                value.job_id,
                value.trial_id,
                value.source_artifact_digest,
            ),
        )
    )
    representation_counts: dict[TrainingRepresentation, int] = {
        "prompt_response_sft": 0,
        "episode_steps": 0,
    }
    for record in record_tuple:
        representation_counts[record.representation] += 1
    exporter = TrainingExporterIdentityV1(digest=_digest_bytes(Path(__file__).read_bytes()))
    manifest_body = {
        "schema_version": "training-dataset-manifest/v1",
        "manifest_path": MANIFEST_FILENAME,
        "cas_uri": None,
        "dataset_digest": dataset_digest,
        "train_split": split_refs["train"].model_dump(mode="json"),
        "validation_split": split_refs["validation"].model_dump(mode="json"),
        "test_split": split_refs["test"].model_dump(mode="json"),
        "source_refs": [source.model_dump(mode="json") for source in source_refs],
        "exporter": exporter.model_dump(mode="json"),
        "benchmark_families": sorted({record.source.benchmark_family for record in record_tuple}),
        "task_families": sorted({record.source.task_family for record in record_tuple}),
        "environment_integrity": "passed",
        "capture_complete": True,
        "redaction_status": "redacted",
        "registry_allowed_use": "training",
        "exclusions_path": EXCLUSIONS_FILENAME,
        "exclusions_digest": exclusions_digest,
        "exclusion_count": len(exclusion_tuple),
        "representation_counts": representation_counts,
    }
    manifest = TrainingDatasetManifestV1.model_validate(
        {
            **manifest_body,
            "manifest_digest": _digest_json(
                {key: value for key, value in manifest_body.items() if key != "cas_uri"}
            ),
        }
    )

    manifest_bytes = _canonical_json(manifest.model_dump(mode="json")) + b"\n"
    payloads = {
        **{_SPLIT_FILENAMES[split]: split_payloads[split] for split in _SPLITS},
        EXCLUSIONS_FILENAME: exclusions_bytes,
        MANIFEST_FILENAME: manifest_bytes,
    }
    published = _publish_directory(destination, payloads)
    split_paths: dict[TrainingSplit, Path] = {
        split: published / _SPLIT_FILENAMES[split] for split in _SPLITS
    }
    exclusions_path = published / EXCLUSIONS_FILENAME
    manifest_path = published / MANIFEST_FILENAME
    return TrainingExportResult(
        root=published,
        manifest_path=manifest_path,
        exclusions_path=exclusions_path,
        split_paths=split_paths,
        manifest=manifest,
        records=record_tuple,
        exclusions=exclusion_tuple,
    )


__all__ = [
    "NormalizedTrainingEvidence",
    "TrainingDatasetManifestV1",
    "TrainingExampleRecord",
    "TrainingExclusionRecord",
    "TrainingExportResult",
    "TrainingExporterIdentityV1",
    "TrainingFunctionCall",
    "TrainingFunctionDefinition",
    "TrainingMessage",
    "TrainingReceiptSourceV1",
    "TrainingSourceBinding",
    "TrainingSourceReceiptV1",
    "TrainingSourceRefV1",
    "TrainingSplitRefV1",
    "TrainingTool",
    "TrainingToolCall",
    "export_training_dataset",
]
