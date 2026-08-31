"""Contracts and schemas for synthetic agent-capability evaluations (V0).

Defines durable, auditable Pydantic contract models for synthetic benchmark
specifications, verification certificates, paired lineages, transformation
facts, and behavior episode records.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from evallab.schemas import (
    SHA256_DIGEST_PATTERN,
    ContractModel,
    _validate_sha256_digest,
)


#: The four core perturbation families in the synthetic agent-capability taxonomy.
class PerturbationFamily(StrEnum):
    TOOL_UNRELIABILITY = "tool_unreliability"
    EPISTEMIC_RESTRAINT = "epistemic_restraint"
    CONTEXT_PRESSURE = "context_pressure"
    FUNCTION_DAG = "function_dag"


PerturbationFamilyName = Literal[
    "tool_unreliability",
    "epistemic_restraint",
    "context_pressure",
    "function_dag",
]

SyntheticPartition = Literal["train", "dev", "test"]
SyntheticCertificateStatus = Literal["experimental", "rejected"]
BehaviorEpisodeStatus = Literal["candidate", "reviewed", "rejected", "gold"]
ConfidenceLevel = Literal["low", "medium", "high"]


def compute_canonical_digest(value: Any) -> str:
    """Compute sha256:<hex> digest of a canonical JSON payload."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=False)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def compute_synthetic_spec_id(data: Mapping[str, Any] | SyntheticEvalSpec) -> str:
    """Compute deterministic sha256:<hex> digest for a SyntheticEvalSpec payload."""
    if isinstance(data, SyntheticEvalSpec):
        payload = data.model_dump(mode="json", exclude={"spec_id"})
    elif isinstance(data, Mapping):
        raw = dict(data)
        raw["spec_id"] = "sha256:" + "0" * 64
        try:
            validated = SyntheticEvalSpec.model_validate(raw)
            payload = validated.model_dump(mode="json", exclude={"spec_id"})
        except Exception:
            raw.pop("spec_id", None)
            if "spec_version" not in raw:
                raw["spec_version"] = "synthetic/v1"
            if "partition" not in raw:
                raw["partition"] = "dev"
            if "parameters" not in raw:
                raw["parameters"] = {}
            if "source_failure_evidence" not in raw:
                raw["source_failure_evidence"] = []
            if "required_evidence" not in raw:
                raw["required_evidence"] = []
            if "family" in raw and hasattr(raw["family"], "value"):
                raw["family"] = raw["family"].value
            payload = raw
    else:
        raise TypeError(f"expected Mapping or SyntheticEvalSpec, got {type(data).__name__}")

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class SyntheticEvalSpec(ContractModel):
    """Specification contract for a synthetically generated agent-capability evaluation task."""

    spec_version: Literal["synthetic/v1"] = "synthetic/v1"
    spec_id: str = Field(
        pattern=SHA256_DIGEST_PATTERN,
        description="Deterministic content digest of semantic fields (sha256:<64 hex>)",
    )
    construct_name: str = Field(
        min_length=1, description="Agent capability construct being measured"
    )
    family: PerturbationFamily = Field(description="Perturbation family category")
    perturbation_type: str = Field(
        min_length=1, description="Specific perturbation mechanism applied"
    )
    seed: int = Field(ge=0, description="Deterministic pseudorandom generation seed")
    source_task_ref: str = Field(min_length=1, description="Reference coordinate of base task")
    source_failure_evidence: list[str] = Field(
        default_factory=list,
        description="Citations or paths to failure evidence that motivated this synthesis",
    )
    base_task_digest: str = Field(
        pattern=SHA256_DIGEST_PATTERN,
        description="Cryptographic digest of base task directory or specification",
    )
    generated_task_digest: str = Field(
        pattern=SHA256_DIGEST_PATTERN,
        description="Cryptographic digest of generated perturbed task directory",
    )
    expected_behavior: str = Field(
        min_length=1,
        description="Precise description of valid agent behavior under perturbation",
    )
    capability_opportunity: str = Field(
        min_length=1,
        description="Measurement thesis and capability gap probed by this task",
    )
    required_evidence: list[str] = Field(
        default_factory=list,
        description="Observable trajectory or state evidence artifacts required for verification",
    )
    license_provenance: str = Field(
        min_length=1,
        description="Full attribution and license provenance string for source materials",
    )
    partition: SyntheticPartition = Field(
        default="dev",
        description="Data partition assignment (train, dev, or test)",
    )
    family_id: str = Field(min_length=1, description="Identifier of the task family cluster")
    lineage_id: str = Field(
        min_length=1, description="Identifier of the paired transformation lineage"
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Family-specific structured configuration parameters",
    )

    @field_validator("spec_id", "base_task_digest", "generated_task_digest")
    @classmethod
    def _validate_digests(cls, value: str) -> str:
        return _validate_sha256_digest(value)

    def verify_spec_id(self) -> bool:
        """Verify that spec_id strictly matches the deterministic semantic hash."""
        return self.spec_id == compute_synthetic_spec_id(self)


def create_synthetic_eval_spec(**kwargs: Any) -> SyntheticEvalSpec:
    """Create a SyntheticEvalSpec instance, computing spec_id if omitted."""
    kwargs.setdefault("spec_version", "synthetic/v1")
    kwargs.setdefault("partition", "dev")
    kwargs.setdefault("parameters", {})
    kwargs.setdefault("source_failure_evidence", [])
    kwargs.setdefault("required_evidence", [])
    if "spec_id" not in kwargs or kwargs["spec_id"] is None:
        kwargs["spec_id"] = compute_synthetic_spec_id(kwargs)
    return SyntheticEvalSpec.model_validate(kwargs)


class SyntheticCertificate(ContractModel):
    """Deterministic execution-based verification certificate for synthetic eval tasks."""

    cert_version: Literal["cert/v1"] = "cert/v1"
    spec_id: str = Field(
        pattern=SHA256_DIGEST_PATTERN, description="Target SyntheticEvalSpec spec_id"
    )
    status: SyntheticCertificateStatus = Field(
        default="experimental",
        description="Certification status: experimental (all gates passed) or rejected",
    )
    static_reachability: bool = Field(
        description="Static verification that solution path is reachable"
    )
    clean_reset_passed: bool = Field(description="Deterministic environment clean reset verified")
    oracle_3x_passed: bool = Field(
        description="Reference oracle passed 3 consecutive execution trials"
    )
    nop_failed: bool = Field(description="Empty/no-op agent failed verification (non-triviality)")
    mutants_tested_count: int = Field(
        default=0, ge=0, description="Count of adversarial mutants executed"
    )
    mutants_failed_count: int = Field(
        default=0,
        ge=0,
        description="Count of adversarial mutants rejected by verifier",
    )
    alignment_audit_passed: bool = Field(
        description="Verification logic matches capability construct intent",
    )
    regeneration_idempotent: bool = Field(
        description="Seed-based task generation produces identical digest across reruns",
    )
    secret_isolation_passed: bool = Field(
        description="Verifier secrets and ground truth not exposed in agent environment",
    )
    evidence_paths: list[str] = Field(
        default_factory=list,
        description="Paths to stored execution artifacts supporting this certificate",
    )
    certified_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
        description="ISO-8601 UTC timestamp when verification was executed",
    )
    notes: str = Field(default="", description="Operator or auditor verification notes")

    @field_validator("spec_id")
    @classmethod
    def _validate_spec_id(cls, value: str) -> str:
        return _validate_sha256_digest(value)

    @model_validator(mode="after")
    def _validate_mutant_bounds(self) -> SyntheticCertificate:
        if self.mutants_failed_count > self.mutants_tested_count:
            raise ValueError(
                f"mutants_failed_count ({self.mutants_failed_count}) cannot exceed "
                f"mutants_tested_count ({self.mutants_tested_count})"
            )
        return self

    @property
    def is_passing(self) -> bool:
        """Evaluate the non-vacuous experimental certification relation."""
        return (
            self.status == "experimental"
            and self.static_reachability
            and self.clean_reset_passed
            and self.oracle_3x_passed
            and self.nop_failed
            and self.alignment_audit_passed
            and self.regeneration_idempotent
            and self.secret_isolation_passed
            and self.mutants_tested_count >= 3
            and self.mutants_failed_count == self.mutants_tested_count
        )


class TransformationFact(ContractModel):
    """Record of a single transformation step applied during synthetic derivation."""

    step_order: int = Field(ge=0, description="Zero-indexed execution order of transformation")
    transformation_name: str = Field(
        min_length=1, description="Named transformation rule or mutation operator"
    )
    input_digest: str = Field(pattern=SHA256_DIGEST_PATTERN, description="Input content digest")
    output_digest: str = Field(pattern=SHA256_DIGEST_PATTERN, description="Output content digest")
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="Transformation parameters"
    )
    diff_summary: str = Field(default="", description="Human-readable summary of applied changes")

    @field_validator("input_digest", "output_digest")
    @classmethod
    def _validate_digests(cls, value: str) -> str:
        return _validate_sha256_digest(value)


class SyntheticLineageFact(ContractModel):
    """Auditable lineage record tracking derivation history from base task to synthetic task."""

    schema_version: Literal[1] = 1
    lineage_id: str = Field(min_length=1, description="Unique lineage tracking identifier")
    family_id: str = Field(min_length=1, description="Perturbation family identifier")
    base_task_ref: str = Field(min_length=1, description="Base task reference coordinate")
    partition: SyntheticPartition = Field(default="dev", description="Data partition assignment")
    transformations: list[TransformationFact] = Field(
        default_factory=list,
        description="Sequential list of applied transformation facts",
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
        description="ISO-8601 UTC creation timestamp",
    )


class PairedLineageSpec(ContractModel):
    """Specification binding a baseline evaluation task to its perturbed synthetic counterpart."""

    schema_version: Literal[1] = 1
    lineage_id: str = Field(min_length=1, description="Lineage grouping identifier")
    family_id: str = Field(min_length=1, description="Perturbation family identifier")
    base_spec_id: str = Field(pattern=SHA256_DIGEST_PATTERN, description="Digest of base task spec")
    perturbed_spec_id: str = Field(
        pattern=SHA256_DIGEST_PATTERN, description="Digest of perturbed task spec"
    )
    perturbation_family: PerturbationFamily = Field(description="Perturbation family category")
    contrast_variable: str = Field(
        min_length=1,
        description="Single independent variable varied between base and perturbed task",
    )
    hypothesis: str = Field(
        min_length=1, description="Falsifiable capability hypothesis being tested"
    )
    partition: SyntheticPartition = Field(default="dev", description="Data partition assignment")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Supplementary lineage metadata"
    )

    @field_validator("base_spec_id", "perturbed_spec_id")
    @classmethod
    def _validate_digests(cls, value: str) -> str:
        return _validate_sha256_digest(value)


class BehaviorEpisodeRecord(ContractModel):
    """Record capturing a discrete behavioral episode observed during execution."""

    schema_version: Literal[1] = 1
    episode_id: str = Field(min_length=1, description="Unique episode identifier")
    trial_id: str = Field(min_length=1, description="Trial identifier in which behavior occurred")
    spec_id: str | None = Field(
        default=None,
        pattern=SHA256_DIGEST_PATTERN,
        description="Optional SyntheticEvalSpec spec_id digest",
    )
    behavior: str = Field(
        min_length=1, description="Behavior taxonomy label or construct identifier"
    )
    start_step: int = Field(ge=0, description="Inclusive start step index in trajectory")
    end_step: int = Field(ge=0, description="Inclusive end step index in trajectory")
    intent: str = Field(default="", description="Inferred or stated intent of the action span")
    evidence_step_ids: list[int] = Field(
        default_factory=list,
        description="List of step IDs providing direct evidence",
    )
    evidence_summary: str = Field(default="", description="Summary of trajectory evidence")
    status: BehaviorEpisodeStatus = Field(
        default="candidate",
        description="Review status (candidate, reviewed, rejected, gold)",
    )
    confidence: ConfidenceLevel = Field(
        default="medium", description="Confidence in behavioral labeling"
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Supplementary episode metadata"
    )

    @field_validator("spec_id")
    @classmethod
    def _validate_optional_spec_id(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_sha256_digest(value)
        return value

    @model_validator(mode="after")
    def _validate_step_bounds(self) -> BehaviorEpisodeRecord:
        if self.end_step < self.start_step:
            raise ValueError(
                f"end_step ({self.end_step}) cannot be less than start_step ({self.start_step})"
            )
        return self
