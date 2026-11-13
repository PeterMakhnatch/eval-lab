"""Typed, provenance-preserving semantic facts for benchmark analysis.

This module is deliberately a small boundary: normalized benchmark facts enter as
Pydantic rows, and leave as deterministic typed Parquet tables. It does not infer
labels or provide a universal score.
"""

from __future__ import annotations

import re
from typing import Literal, TypeVar, get_args, get_type_hints

import pyarrow as pa
from pydantic import Field, field_validator, model_validator

from evallab.schemas import ContractModel

Digest = str
ProvenanceKind = Literal["mechanical", "benchmark_verifier", "human", "model", "derived"]
Verdict = Literal["satisfied", "violated", "unknown"]

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")




class FactRow(ContractModel):
    """Common immutable source identity carried by every semantic fact row."""

    source_ref: str = Field(min_length=1)
    source_digest: Digest
    provenance_kind: ProvenanceKind

    @field_validator("source_ref")
    @classmethod
    def _source_ref(cls, value: str) -> str:
        if value.strip() != value or any(char in value for char in "\r\n\x00"):
            raise ValueError("source_ref must be a trimmed, newline-free reference")
        return value

    @field_validator("source_digest")
    @classmethod
    def _source_digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("source_digest must be sha256:<64 lowercase hex digits>")
        return value


class CapabilityOpportunity(FactRow):
    opportunity_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    benchmark: str = Field(min_length=1)
    construct: str = Field(min_length=1)
    start_step: int | None = Field(default=None, ge=0)
    end_step: int | None = Field(default=None, ge=0)
    eligible: bool | None = None
    required_evidence: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _step_order(self) -> CapabilityOpportunity:
        if (
            self.start_step is not None
            and self.end_step is not None
            and self.end_step < self.start_step
        ):
            raise ValueError("end_step must not precede start_step")
        if not set(self.missing_evidence).issubset(self.required_evidence):
            raise ValueError("missing_evidence must be a subset of required_evidence")
        return self


class ProcessStepFact(FactRow):
    trial_id: str = Field(min_length=1)
    source_trajectory_id: str = Field(min_length=1)
    source_step_id: str = Field(min_length=1)
    label: Literal["correct", "neutral", "incorrect"]
    original_label: str | None = None
    propagated_from_step: str | None = None
    first_error: bool | None = None


class RetrievalFact(FactRow):
    trial_id: str = Field(min_length=1)
    query_id: str | None = None
    call_id: str | None = None
    result_id: str | None = None
    document_id: str | None = None
    file_id: str | None = None
    block_id: str | None = None
    line_id: str | None = None
    rank: int | None = Field(default=None, ge=1)
    gold_status: str | None = None
    utilized_status: bool | None = None
    cited_evidence_ref: str | None = None
    token_volume: int | None = Field(default=None, ge=0)
    byte_volume: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _retrieval_evidence(self) -> RetrievalFact:
        if not any((self.document_id, self.file_id, self.block_id, self.line_id)):
            raise ValueError("retrieval facts require an exposed document, file, block, or line ID")
        if self.utilized_status is not None and not self.cited_evidence_ref:
            raise ValueError("utilized_status requires cited_evidence_ref")
        return self


class ConstraintFact(FactRow):
    trial_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    action_id: str | None = None
    constraint_id: str = Field(min_length=1)
    constraint_scope: Literal["local", "global"]
    required: bool | None = None
    verdict: Verdict = "unknown"
    verifier_evidence: str | None = None


class ContextOperationFact(FactRow):
    trial_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    operation: Literal[
        "compaction",
        "clear",
        "evict",
        "memory_read",
        "memory_write",
        "memory_use",
        "session_boundary",
    ]
    configured_size: int | None = Field(default=None, ge=0)
    realized_size: int | None = Field(default=None, ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    before_token_count: int | None = Field(default=None, ge=0)
    after_token_count: int | None = Field(default=None, ge=0)
    content_digest: Digest | None = None
    session_id: str | None = None
    step_index: int | None = Field(default=None, ge=0, strict=True)
    context_position_tokens: int | None = Field(default=None, ge=0)

    @field_validator("content_digest")
    @classmethod
    def _content_digest(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("content_digest must be sha256:<64 lowercase hex digits>")
        return value


class PairedConditionFact(FactRow):
    trial_id: str = Field(min_length=1)
    pair_id: str = Field(min_length=1)
    session_id: str | None = None
    task_id: str = Field(min_length=1)
    variant: str = Field(min_length=1)
    condition: str = Field(min_length=1)
    trigger: str = Field(min_length=1)
    critical_action: str | None = None
    state_diff: str | None = None
    primary_verdict: Verdict = "unknown"
    secondary_verdict: Verdict = "unknown"


class SessionDependencyFact(FactRow):
    trial_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    subtask_id: str = Field(min_length=1)
    dependency_edge: str = Field(min_length=1)
    required_prior_fact: str = Field(min_length=1)
    observed_memory_reference: str | None = None
    progress: str | None = None
    outcome: str | None = None


class EvidenceCoverage(FactRow):
    trial_id: str = Field(min_length=1)
    benchmark: str = Field(min_length=1)
    construct: str = Field(min_length=1)
    exposed: bool
    eligible: bool | None = None
    required_evidence: tuple[str, ...] = ()
    observed_evidence: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    analysis_ready: bool | None = None

    @model_validator(mode="after")
    def _coverage_consistency(self) -> EvidenceCoverage:
        if not set(self.observed_evidence).issubset(self.required_evidence):
            raise ValueError("observed_evidence must be a subset of required_evidence")
        if not set(self.missing_evidence).issubset(self.required_evidence):
            raise ValueError("missing_evidence must be a subset of required_evidence")
        if set(self.observed_evidence) & set(self.missing_evidence):
            raise ValueError("evidence cannot be both observed and missing")
        expected = self.exposed and self.eligible is True and not self.missing_evidence
        if self.analysis_ready is not None and self.analysis_ready != expected:
            raise ValueError("analysis_ready must reflect eligibility and missing evidence")
        return self


FACT_TYPES: dict[str, type[FactRow]] = {
    "capability_opportunities": CapabilityOpportunity,
    "process_step_facts": ProcessStepFact,
    "retrieval_facts": RetrievalFact,
    "constraint_facts": ConstraintFact,
    "context_operation_facts": ContextOperationFact,
    "paired_condition_facts": PairedConditionFact,
    "session_dependency_facts": SessionDependencyFact,
    "evidence_coverage": EvidenceCoverage,
}

_LIST_FIELDS = {"required_evidence", "missing_evidence", "observed_evidence"}


def _schema(model: type[FactRow]) -> pa.Schema:
    fields: list[pa.Field] = []
    hints = get_type_hints(model)
    for name, annotation in model.model_fields.items():
        hint = hints[name]
        members = get_args(hint)
        if name in _LIST_FIELDS:
            typ = pa.list_(pa.string())
        elif hint is bool or bool in members:
            typ = pa.bool_()
        elif hint is int or int in members:
            typ = pa.int64()
        else:
            typ = pa.string()
        required = annotation.is_required()
        fields.append(pa.field(name, typ, nullable=not required))
    return pa.schema(fields)


SEMANTIC_FACT_SCHEMAS = {name: _schema(model) for name, model in FACT_TYPES.items()}










T = TypeVar("T", bound=FactRow)








# Explicit aliases make the public boundary easy to discover without a registry.

