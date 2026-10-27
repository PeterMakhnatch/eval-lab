"""Validated, provenance-bearing ``failure-card/v1`` contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from evallab.failure_library.records import DefectClass, RunCode, Tier
from evallab.schemas import ContractModel

Agent = Literal["task-qa", "trace-qa", "seeker"]


class CardDraft(ContractModel):
    """Reviewed subscription-model prose; evidence is joined by the compiler."""

    hackbook_id: str
    what_breaks: str = Field(min_length=1)
    task_cues: list[str] = Field(min_length=1)
    trace_cues: list[str] = Field(min_length=1)
    seeker_hypothesis: str = Field(min_length=1)
    fixes: list[str] = Field(min_length=1)
    counterexamples: list[str] = Field(min_length=1)


class ExampleRef(ContractModel):
    """A citation, never an ungrounded model-generated example."""

    id: str
    kind: Literal["finding", "run"]
    collection: str
    source_id: str
    task_id: str
    upstream_repo: str
    upstream_revision: str | None
    source_url: str
    summary: str
    tier: Tier | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    evidence_notes: list[str] = Field(default_factory=list)
    status_now: str = "unknown"
    fixed_in_revision: str | None = None


class FailureCard(ContractModel):
    schema_version: Literal["failure-card/v1"] = "failure-card/v1"
    card_id: str
    hackbook_entry_id: str
    taxonomy_class_id: str = Field(pattern=r"^(TS|RS)\d{2}$")
    defect_class: DefectClass
    run_code: RunCode | None = None
    title: str
    what_breaks: str
    task_cues: list[str]
    trace_cues: list[str]
    seeker_hypothesis: str
    fixes: list[str]
    # Diagnostic non-defect conditions, not invented clean labels on real runs.
    counterexamples: list[str]
    examples: list[ExampleRef] = Field(min_length=3, max_length=10)
    support_count: int = Field(ge=3)
    prevalence_by_collection: dict[str, dict[str, int | float]]


class AgentPack(ContractModel):
    schema_version: Literal["failure-pack/v1"] = "failure-pack/v1"
    agent: Agent
    status: Literal["ready", "empty"]
    markdown: str
    example_ids: list[str]
    card_ids: list[str]
    library_digest: str
    exclusions: list[str]
    task_id: str | None = None
    task_digest: str | None = None
