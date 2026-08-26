"""Platform-owned MachineJudgment v1 contract.

Normative source: PR #189 ``automated-trajectory-interpretation-v1.schema.json``.
This module references Agent Data CitationHandle IDs only; it defines no Data type.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from evallab.schemas import ContractModel

SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
Digest = Annotated[str, Field(pattern=SHA256_PATTERN)]
TRAJECTORY_ONTOLOGY_V1_CLASSES = frozenset(
    {
        "infrastructure_failure",
        "verifier_failure",
        "wrong_target_or_action",
        "tool_schema_misuse",
        "expected_negative_exit",
        "repeated_failure_or_thrashing",
        "false_verification_or_unsupported_terminal_claim",
        "missed_recovery_opportunity",
        "successful_recovery",
        "context_or_constraint_loss",
        "appropriate_action",
        "appropriate_abstention",
    }
)



def canonical_json_digest(value: Any) -> str:
    """Return the sha256 digest of canonical JSON-compatible content."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    serialized = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(serialized.encode()).hexdigest()


class JudgmentLabel(ContractModel):
    namespace: str
    ontology_version: str
    class_id: str

    @model_validator(mode="after")
    def validate_frozen_ontology_class(self) -> JudgmentLabel:
        if (
            self.namespace == "traj.judge.v1"
            and self.ontology_version == "traj.judge.ontology.v1"
            and self.class_id not in TRAJECTORY_ONTOLOGY_V1_CLASSES
        ):
            raise ValueError("class_id is not in the frozen trajectory ontology v1")
        return self


class JudgmentConfidence(ContractModel):
    raw_label: str | None
    raw_score: float | None
    calibrated_probability: float | None
    calibration_version: str | None


class ModelIdentity(ContractModel):
    provider: str
    model: str
    family: str
    settings_digest: str = Field(pattern=SHA256_PATTERN)


class MachineJudgment(ContractModel):
    """One validated model judgment or deterministic pre-judge abstention."""

    schema_version: Literal["machine-judgment/v1"]
    judgment_id: str = Field(pattern=SHA256_PATTERN)
    judgment_digest: str = Field(pattern=SHA256_PATTERN)
    producer_kind: Literal["model", "deterministic_abstention"]
    pack_id: str = Field(pattern=SHA256_PATTERN)
    pack_digest: str = Field(pattern=SHA256_PATTERN)
    validity: Literal["supported", "contradicted", "insufficient_evidence"]
    primary_label: JudgmentLabel | None
    finding_summary: str
    earliest_supported_event_id: str | None
    citation_ids: list[Digest]
    alternative_explanations: list[str]
    coverage_gaps: list[str]
    proposed_discriminator: str | None
    confidence: JudgmentConfidence
    model_identity: ModelIdentity | None
    prompt_digest: str | None = Field(pattern=SHA256_PATTERN)
    rubric_digest: str | None = Field(pattern=SHA256_PATTERN)
    output_schema_digest: str = Field(pattern=SHA256_PATTERN)
    raw_response_digest: str | None = Field(pattern=SHA256_PATTERN)
    produced_at: datetime

    @field_validator("citation_ids")
    @classmethod
    def validate_citation_ids(cls, values: list[str]) -> list[str]:
        for value in values:
            if re.fullmatch(SHA256_PATTERN, value) is None:
                raise ValueError("citation IDs must be canonical sha256 digests")
        if len(set(values)) != len(values):
            raise ValueError("citation IDs must be unique")
        return sorted(values)

    @field_validator("alternative_explanations", "coverage_gaps")
    @classmethod
    def canonicalize_text_lists(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("list values must be unique")
        return sorted(values)

    @model_validator(mode="after")
    def validate_producer_contract(self) -> MachineJudgment:
        model_fields = (
            self.model_identity,
            self.prompt_digest,
            self.rubric_digest,
            self.raw_response_digest,
        )
        if self.producer_kind == "model" and any(
            field is None for field in model_fields
        ):
            raise ValueError(
                "model judgment requires model, prompt, rubric, and raw-response identity"
            )
        if self.producer_kind == "deterministic_abstention":
            if self.primary_label is not None:
                raise ValueError("deterministic abstention cannot assign a primary label")
            if any(field is not None for field in model_fields):
                raise ValueError(
                    "deterministic abstention cannot carry model invocation identity"
                )
            if self.validity != "insufficient_evidence":
                raise ValueError(
                    "deterministic abstention requires insufficient_evidence validity"
                )
        return self

    def identity_payload(self) -> dict[str, Any]:
        """Canonical identity body, excluding publication time and its own digest."""
        payload = self.model_dump(mode="json")
        payload.pop("produced_at")
        payload.pop("judgment_digest")
        return payload

    def expected_judgment_digest(self) -> str:
        return canonical_json_digest(self.identity_payload())
