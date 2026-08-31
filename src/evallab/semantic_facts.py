"""Typed, provenance-preserving semantic facts for benchmark analysis.

This module is deliberately a small boundary: normalized benchmark facts enter as
Pydantic rows, and leave as deterministic typed Parquet tables. It does not infer
labels or provide a universal score.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, TypeVar, get_args, get_type_hints

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import Field, field_validator, model_validator

from evallab.schemas import ContractModel

Digest = str
ProvenanceKind = Literal["mechanical", "benchmark_verifier", "human", "model", "derived"]
Verdict = Literal["satisfied", "violated", "unknown"]

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


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
    step_index: int | None = Field(default=None, ge=0)
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
FACT_SCHEMAS = SEMANTIC_FACT_SCHEMAS


class NormalizedFactBundle(ContractModel):
    """The non-generic, named input boundary for normalized benchmark facts."""

    capability_opportunities: tuple[CapabilityOpportunity, ...] = ()
    process_step_facts: tuple[ProcessStepFact, ...] = ()
    retrieval_facts: tuple[RetrievalFact, ...] = ()
    constraint_facts: tuple[ConstraintFact, ...] = ()
    context_operation_facts: tuple[ContextOperationFact, ...] = ()
    paired_condition_facts: tuple[PairedConditionFact, ...] = ()
    session_dependency_facts: tuple[SessionDependencyFact, ...] = ()
    evidence_coverage: tuple[EvidenceCoverage, ...] = ()

    def without_coverage(self) -> NormalizedFactBundle:
        return self.model_copy(update={"evidence_coverage": ()})


def _coverage_rows(bundle: NormalizedFactBundle) -> tuple[EvidenceCoverage, ...]:
    grouped: dict[tuple[str, str, str], list[CapabilityOpportunity]] = defaultdict(list)
    for opportunity in bundle.capability_opportunities:
        grouped[(opportunity.trial_id, opportunity.benchmark, opportunity.construct)].append(
            opportunity
        )
    rows: list[EvidenceCoverage] = []
    for (trial_id, benchmark, construct), opportunities in sorted(grouped.items()):
        required = tuple(sorted({item for row in opportunities for item in row.required_evidence}))
        missing = tuple(sorted({item for row in opportunities for item in row.missing_evidence}))
        observed = tuple(item for item in required if item not in missing)
        eligibility = [row.eligible for row in opportunities]
        eligible = (
            True
            if any(value is True for value in eligibility)
            else False
            if all(value is False for value in eligibility)
            else None
        )
        rows.append(
            EvidenceCoverage(
                trial_id=trial_id,
                benchmark=benchmark,
                construct=construct,
                exposed=True,
                eligible=eligible,
                required_evidence=required,
                observed_evidence=observed,
                missing_evidence=missing,
                analysis_ready=(
                    True
                    if eligible is True and not missing
                    else False
                    if eligible is False
                    else None
                ),
                source_ref=f"derived:evidence_coverage/{trial_id}/{construct}",
                source_digest=_digest([row.model_dump(mode="json") for row in opportunities]),
                provenance_kind="derived",
            )
        )
    return tuple(rows)


def _coverage_key(row: EvidenceCoverage) -> tuple[str, str, str]:
    return row.trial_id, row.benchmark, row.construct


def normalize_bundle(value: NormalizedFactBundle | Mapping[str, Any]) -> NormalizedFactBundle:
    if isinstance(value, NormalizedFactBundle):
        bundle = value
    else:
        bundle = NormalizedFactBundle.model_validate(value)
    computed = {_coverage_key(row): row for row in _coverage_rows(bundle)}
    explicit: dict[tuple[str, str, str], EvidenceCoverage] = {}
    for row in bundle.evidence_coverage:
        key = _coverage_key(row)
        previous = explicit.get(key)
        if previous is not None and previous != row:
            raise ValueError(f"conflicting duplicate evidence_coverage row for {key!r}")
        explicit[key] = row
    for key in computed.keys() & explicit.keys():
        if computed[key] != explicit[key]:
            raise ValueError(
                f"evidence_coverage conflicts with computed opportunity row for {key!r}"
            )
        explicit.pop(key)
    merged = tuple(
        row for _, row in sorted((*computed.items(), *explicit.items()), key=lambda item: item[0])
    )
    return bundle.model_copy(update={"evidence_coverage": merged})


T = TypeVar("T", bound=FactRow)


def _row(model: FactRow) -> dict[str, Any]:
    value = model.model_dump(mode="python")
    for key in _LIST_FIELDS:
        if key in value and value[key] is not None:
            value[key] = list(value[key])
    return value


def project_fact_bundle(
    bundle: NormalizedFactBundle | Mapping[str, Any], output_dir: str | Path
) -> dict[str, Path]:
    """Write all named fact tables, including derived trial×construct coverage."""
    normalized = normalize_bundle(bundle)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name in FACT_TYPES:
        rows = getattr(normalized, name)
        table = pa.Table.from_pylist(
            [_row(item) for item in rows], schema=SEMANTIC_FACT_SCHEMAS[name]
        )
        path = destination / f"{name}.parquet"
        temporary = path.with_suffix(".parquet.tmp")
        pq.write_table(
            table, temporary, compression="zstd", use_dictionary=False, write_statistics=True
        )
        temporary.replace(path)
        paths[name] = path
    return paths


def load_fact_bundle(path: str | Path) -> NormalizedFactBundle:
    """Load a named JSON bundle; JSONL is accepted as one object per line."""
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    payload: Any
    if source.suffix == ".jsonl":
        payload = json.loads(
            "[" + ",".join(line for line in text.splitlines() if line.strip()) + "]"
        )
    else:
        payload = json.loads(text)
    if isinstance(payload, list):
        payload = {"capability_opportunities": payload}
    if not isinstance(payload, Mapping):
        raise ValueError("normalized fact bundle must be a JSON object")
    return normalize_bundle(payload)


def query_scorecard(
    output_dir: str | Path,
    *,
    benchmark: str | None = None,
    construct: str | None = None,
    group_by: Sequence[str] = ("benchmark", "construct"),
) -> list[dict[str, Any]]:
    """Return benchmark×construct readiness counts; reject unsupported aggregates."""
    if tuple(group_by) != ("benchmark", "construct"):
        raise ValueError(
            "only benchmark×construct scorecards are supported; universal aggregates are rejected"
        )
    root = Path(output_dir)
    coverage_path = root / "evidence_coverage.parquet"
    opportunity_path = root / "capability_opportunities.parquet"
    if not coverage_path.is_file() or not opportunity_path.is_file():
        raise ValueError("projected fact bundle is missing required coverage/opportunity tables")
    coverage = [
        EvidenceCoverage.model_validate(row) for row in pq.read_table(coverage_path).to_pylist()
    ]
    opportunities = [
        CapabilityOpportunity.model_validate(row)
        for row in pq.read_table(opportunity_path).to_pylist()
    ]
    groups: dict[tuple[str, str], list[CapabilityOpportunity]] = defaultdict(list)
    for row in opportunities:
        if benchmark is not None and row.benchmark != benchmark:
            continue
        if construct is not None and row.construct != construct:
            continue
        groups[(row.benchmark, row.construct)].append(row)
    coverage_groups: dict[tuple[str, str], list[EvidenceCoverage]] = defaultdict(list)
    for row in coverage:
        if benchmark is not None and row.benchmark != benchmark:
            continue
        if construct is not None and row.construct != construct:
            continue
        coverage_groups[(row.benchmark, row.construct)].append(row)
    result: list[dict[str, Any]] = []
    for key in sorted(set(groups) | set(coverage_groups)):
        rows = groups[key]
        coverage_rows = coverage_groups[key]
        ready_opportunities = [
            row for row in rows if row.eligible is True and not row.missing_evidence
        ]
        ready_trials = sum(row.analysis_ready is True for row in coverage_rows)
        not_ready_trials = sum(row.analysis_ready is False for row in coverage_rows)
        unknown_trials = sum(row.analysis_ready is None for row in coverage_rows)
        support = len(coverage_rows)
        overall_ready = (
            True
            if support and ready_trials == support
            else False
            if support and not_ready_trials == support
            else None
        )
        result.append(
            {
                "benchmark": key[0],
                "construct": key[1],
                "opportunity_count": len(rows),
                "eligible_analysis_ready_opportunities": len(ready_opportunities),
                "coverage_trials": support,
                "eligible_trials": sum(row.eligible is True for row in coverage_rows),
                "analysis_ready_trials": ready_trials,
                "not_analysis_ready_trials": not_ready_trials,
                "unknown_analysis_readiness_trials": unknown_trials,
                "exposed_trials": sum(row.exposed for row in coverage_rows),
                "analysis_ready": overall_ready,
            }
        )
    return result


# Explicit aliases make the public boundary easy to discover without a registry.
project_normalized_fact_bundle = project_fact_bundle
query_semantic_scorecard = query_scorecard

__all__ = [
    "FactRow",
    "Digest",
    "ProvenanceKind",
    "Verdict",
    "CapabilityOpportunity",
    "ProcessStepFact",
    "RetrievalFact",
    "ConstraintFact",
    "ContextOperationFact",
    "PairedConditionFact",
    "SessionDependencyFact",
    "EvidenceCoverage",
    "NormalizedFactBundle",
    "SEMANTIC_FACT_SCHEMAS",
    "FACT_SCHEMAS",
    "normalize_bundle",
    "load_fact_bundle",
    "project_fact_bundle",
    "project_normalized_fact_bundle",
    "query_scorecard",
    "query_semantic_scorecard",
]
