"""Disposable trajectory context pack for agent-facing runtime context.

This module is a deterministic, disposable transport/context pack compiler. It is not
a source of truth or a persistent store. Primary truth remains TrialAnalysisSidecar,
AnalysisReview, BehaviorEpisode, and NormalizedFactBundle.
"""

from __future__ import annotations

import posixpath
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from evallab.behavior_episodes import BehaviorEpisode
from evallab.schemas import AnalysisReview, AnalysisSourceDigests, TrialAnalysisSidecar
from evallab.semantic_facts import (
    CapabilityOpportunity,
    ConstraintFact,
    ContextOperationFact,
    EvidenceCoverage,
    FactRow,
    NormalizedFactBundle,
    PairedConditionFact,
    ProcessStepFact,
    RetrievalFact,
    SessionDependencyFact,
)

EntryKind = Literal["analysis", "episode", "semantic_fact", "unknown"]
EntryStatus = Literal[
    "accepted",
    "reviewed",
    "confirmed",
    "candidate",
    "rejected",
    "needs_revision",
    "superseded",
    "unreviewed",
    "invalid",
]

_KIND_ORDER: dict[EntryKind, int] = {
    "analysis": 0,
    "episode": 1,
    "semantic_fact": 2,
    "unknown": 3,
}


@dataclass(frozen=True)
class ContextCitation:
    path: str
    digest: str | None
    step_id: int | None
    tool_call_id: str | None
    supports: str | None


@dataclass(frozen=True)
class ContextEntry:
    kind: EntryKind
    entry_id: str
    claim: str
    status: str
    # non-None for candidate/rejected/unreviewed/needs_revision/superseded/invalid
    label: str | None
    citations: tuple[ContextCitation, ...]
    provenance: Mapping[str, Any]
    source: str  # "TrialAnalysisSidecar" | "BehaviorEpisode" | "CapabilityOpportunity" | ...


@dataclass(frozen=True)
class TruncationMetadata:
    truncated: bool
    max_entries: int | None
    included_count: int
    omitted_count: int
    omitted_entry_ids: tuple[str, ...]


def _to_json_compatible(obj: Any) -> Any:
    if isinstance(obj, ContextCitation):
        return {
            "path": obj.path,
            "digest": obj.digest,
            "step_id": obj.step_id,
            "tool_call_id": obj.tool_call_id,
            "supports": obj.supports,
        }
    if isinstance(obj, ContextEntry):
        return {
            "kind": obj.kind,
            "entry_id": obj.entry_id,
            "claim": obj.claim,
            "status": obj.status,
            "label": obj.label,
            "citations": [_to_json_compatible(c) for c in obj.citations],
            "provenance": _to_json_compatible(obj.provenance),
            "source": obj.source,
        }
    if isinstance(obj, TruncationMetadata):
        return {
            "truncated": obj.truncated,
            "max_entries": obj.max_entries,
            "included_count": obj.included_count,
            "omitted_count": obj.omitted_count,
            "omitted_entry_ids": list(obj.omitted_entry_ids),
        }
    if isinstance(obj, TrajectoryContextPack):
        return {
            "trial_id": obj.trial_id,
            "entries": [_to_json_compatible(e) for e in obj.entries],
            "unknowns": [_to_json_compatible(u) for u in obj.unknowns],
            "truncation": _to_json_compatible(obj.truncation),
        }
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Mapping):
        return {str(k): _to_json_compatible(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_json_compatible(v) for v in obj]
    return obj


def _format_markdown_entry(entry: ContextEntry) -> list[str]:
    tag = f"[{entry.label if entry.label is not None else entry.status}]"
    lines = [f"- {tag} {entry.entry_id}: {entry.claim}"]
    for citation in entry.citations:
        parts = [f"`{citation.path}`"]
        if citation.step_id is not None:
            parts.append(f"step={citation.step_id}")
        if citation.tool_call_id is not None:
            parts.append(f"tool={citation.tool_call_id}")
        if citation.digest is not None:
            parts.append(f"digest={citation.digest}")
        lines.append(f"  - {' '.join(parts)}")
    return lines


@dataclass(frozen=True)
class TrajectoryContextPack:
    trial_id: str
    entries: tuple[ContextEntry, ...]
    unknowns: tuple[ContextEntry, ...]
    truncation: TruncationMetadata

    def to_dict(self) -> dict[str, Any]:
        """JSON-compatible dict: UUIDs as str, tuples as lists, nested dicts only."""
        return _to_json_compatible(self)

    def to_markdown(self) -> str:
        """Concise Markdown with citations. Deterministic."""
        lines = [f"# Trajectory context — {self.trial_id}"]

        analyses = [e for e in self.entries if e.kind == "analysis"]
        episodes = [e for e in self.entries if e.kind == "episode"]
        facts = [e for e in self.entries if e.kind == "semantic_fact"]

        if analyses:
            lines.append("")
            lines.append("## Analyses")
            for entry in analyses:
                lines.extend(_format_markdown_entry(entry))

        if episodes:
            lines.append("")
            lines.append("## Episodes")
            for entry in episodes:
                lines.extend(_format_markdown_entry(entry))

        if facts:
            lines.append("")
            lines.append("## Semantic facts")
            for entry in facts:
                lines.extend(_format_markdown_entry(entry))

        if self.unknowns:
            lines.append("")
            lines.append("## Unknowns")
            for entry in self.unknowns:
                lines.extend(_format_markdown_entry(entry))

        if self.truncation.truncated and self.truncation.omitted_entry_ids:
            lines.append("")
            lines.append("## Truncation")
            for omitted_id in self.truncation.omitted_entry_ids:
                lines.append(f"- {omitted_id}")

        return "\n".join(lines) + "\n"


def latest_review(reviews: Sequence[AnalysisReview]) -> AnalysisReview | None:
    """Latest review for one analysis. Empty -> None.

    Total order: (reviewed_at, str(review_id)) ascending, take the max.
    Do not follow superseded_by here; just pick the latest record.
    """
    if not reviews:
        return None
    return max(reviews, key=lambda r: (r.reviewed_at, str(r.review_id)))


def reviews_by_analysis(
    reviews: Sequence[AnalysisReview],
) -> dict[str, tuple[AnalysisReview, ...]]:
    """Group by str(analysis_id). Each group sorted by the same total order."""
    grouped: dict[str, list[AnalysisReview]] = defaultdict(list)
    for review in reviews:
        grouped[str(review.analysis_id)].append(review)
    return {
        analysis_id: tuple(sorted(group, key=lambda r: (r.reviewed_at, str(r.review_id))))
        for analysis_id, group in sorted(grouped.items())
    }


def _citation_sort_key(citation: ContextCitation) -> tuple[str, bool, int, str]:
    return (
        citation.path,
        citation.step_id is None,
        citation.step_id or 0,
        citation.tool_call_id or "",
    )


def _resolve_analysis_citation_digest(
    path: str, digests: AnalysisSourceDigests
) -> str | None:
    if path in digests.files:
        return digests.files[path]
    basename = posixpath.basename(path).lower()
    if "result" in basename:
        return digests.result
    if "task" in basename:
        return digests.task
    if "trajectory" in path.lower():
        return digests.trajectory
    return None


def _fact_row_provenance(row: FactRow) -> dict[str, Any]:
    dumped = row.model_dump()
    return {k: tuple(v) if isinstance(v, list) else v for k, v in dumped.items()}


def build_trajectory_context(
    *,
    trial_id: str,
    analyses: Sequence[TrialAnalysisSidecar] = (),
    reviews: Sequence[AnalysisReview] = (),
    episodes: Sequence[BehaviorEpisode] = (),
    facts: NormalizedFactBundle | None = None,
    include_candidates: bool = False,
    include_rejected: bool = False,
    max_entries: int | None = None,
) -> TrajectoryContextPack:
    """Compile one trial's agent-facing context pack."""
    includable_entries: list[ContextEntry] = []
    unknown_entries: list[ContextEntry] = []

    grouped_reviews = reviews_by_analysis(reviews)
    for sidecar in analyses:
        if str(sidecar.source_trial_id) != trial_id:
            continue
        if sidecar.validation_status != "valid":
            continue

        sidecar_reviews = grouped_reviews.get(str(sidecar.analysis_id), ())
        review = latest_review(sidecar_reviews)

        status: str
        label: str | None

        if review is None:
            if not include_candidates:
                continue
            status = "unreviewed"
            label = "candidate"
        else:
            if review.disposition == "accepted":
                status = "accepted"
                label = None
            elif review.disposition == "needs_revision":
                if not include_candidates:
                    continue
                status = "needs_revision"
                label = "candidate"
            elif review.disposition == "rejected":
                if not include_rejected:
                    continue
                status = "rejected"
                label = "rejected"
            elif review.disposition == "superseded":
                if not include_rejected:
                    continue
                status = "superseded"
                label = "superseded"
            else:
                continue

        citations = tuple(
            sorted(
                (
                    ContextCitation(
                        path=ev.path,
                        digest=_resolve_analysis_citation_digest(ev.path, sidecar.source_digests),
                        step_id=ev.step_id,
                        tool_call_id=ev.tool_call_id,
                        supports=ev.supports,
                    )
                    for ev in sidecar.output.evidence
                ),
                key=_citation_sort_key,
            )
        )

        provenance = {
            "analysis_id": str(sidecar.analysis_id),
            "validation_status": sidecar.validation_status,
            "review_id": str(review.review_id) if review is not None else None,
            "review_disposition": review.disposition if review is not None else None,
            "reviewer": review.reviewer if review is not None else None,
            "agent": sidecar.analysis_provenance.agent,
            "model": sidecar.analysis_provenance.model,
            "prompt_digest": sidecar.analysis_provenance.prompt_digest,
            "rubric_digest": sidecar.analysis_provenance.rubric_digest,
            "raw_response_digest": sidecar.raw_response_digest,
            "source_trial_path": sidecar.source_trial_path,
            "source_digests.result": sidecar.source_digests.result,
            "source_digests.task": sidecar.source_digests.task,
            "source_digests.trajectory": sidecar.source_digests.trajectory,
        }

        includable_entries.append(
            ContextEntry(
                kind="analysis",
                entry_id=str(sidecar.analysis_id),
                claim=sidecar.output.summary,
                status=status,
                label=label,
                citations=citations,
                provenance=provenance,
                source="TrialAnalysisSidecar",
            )
        )

    for episode in episodes:
        if str(episode.trial_id) != trial_id:
            continue

        ep_status: str
        ep_label: str | None

        if episode.status in {"reviewed", "confirmed"}:
            ep_status = episode.status
            ep_label = None
        elif episode.status == "candidate":
            if not include_candidates:
                continue
            ep_status = "candidate"
            ep_label = "candidate"
        elif episode.status == "rejected":
            if not include_rejected:
                continue
            ep_status = "rejected"
            ep_label = "rejected"
        else:
            continue

        ep_citations_list: list[ContextCitation] = []
        for step in episode.evidence_step_ids:
            ep_citations_list.append(
                ContextCitation(
                    path=episode.document_id,
                    digest=episode.source_sha256,
                    step_id=int(step),
                    tool_call_id=None,
                    supports=episode.label,
                )
            )
        for span in episode.evidence_span_ids:
            ep_citations_list.append(
                ContextCitation(
                    path=episode.document_id,
                    digest=episode.input_digest,
                    step_id=None,
                    tool_call_id=str(span),
                    supports=episode.label,
                )
            )
        ep_citations = tuple(sorted(ep_citations_list, key=_citation_sort_key))

        ep_provenance = {
            "episode_id": episode.episode_id,
            "trial_id": episode.trial_id,
            "document_id": episode.document_id,
            "trajectory_id": episode.trajectory_id,
            "session_id": episode.session_id,
            "label": episode.label,
            "status": episode.status,
            "start_step": episode.start_step,
            "end_step": episode.end_step,
            "annotator_kind": episode.annotator_kind,
            "annotator_id": episode.annotator_id,
            "source_sha256": episode.source_sha256,
            "input_digest": episode.input_digest,
            "detector_version": episode.detector_version,
            "rubric_version": episode.rubric_version,
        }

        includable_entries.append(
            ContextEntry(
                kind="episode",
                entry_id=episode.episode_id,
                claim=episode.rationale,
                status=ep_status,
                label=ep_label,
                citations=ep_citations,
                provenance=ep_provenance,
                source="BehaviorEpisode",
            )
        )

    if facts is not None:
        for opp in facts.capability_opportunities:
            if str(opp.trial_id) != trial_id:
                continue
            opp_citation = ContextCitation(
                path=opp.source_ref,
                digest=opp.source_digest,
                step_id=opp.start_step,
                tool_call_id=None,
                supports=opp.construct,
            )
            opp_prov = _fact_row_provenance(opp)
            if opp.eligible is not True or bool(opp.missing_evidence):
                if opp.eligible is False:
                    f_status = "ineligible"
                elif opp.missing_evidence:
                    f_status = "missing_evidence"
                else:
                    f_status = "unknown_eligibility"
                if opp.missing_evidence:
                    f_claim = (
                        f"Opportunity {opp.opportunity_id} for construct {opp.construct} "
                        f"is missing evidence: {', '.join(sorted(opp.missing_evidence))}."
                    )
                elif opp.eligible is False:
                    f_claim = (
                        f"Opportunity {opp.opportunity_id} for construct {opp.construct} "
                        "is ineligible."
                    )
                else:
                    f_claim = (
                        f"Opportunity {opp.opportunity_id} for construct {opp.construct} "
                        "has unknown eligibility."
                    )
                unknown_entries.append(
                    ContextEntry(
                        kind="unknown",
                        entry_id=opp.opportunity_id,
                        claim=f_claim,
                        status=f_status,
                        label="unknown",
                        citations=(opp_citation,),
                        provenance=opp_prov,
                        source="CapabilityOpportunity",
                    )
                )
            else:
                f_status = "eligible"
                f_claim = (
                    f"Opportunity {opp.opportunity_id} for construct {opp.construct} "
                    "is eligible with complete evidence."
                )
                includable_entries.append(
                    ContextEntry(
                        kind="semantic_fact",
                        entry_id=opp.opportunity_id,
                        claim=f_claim,
                        status=f_status,
                        label=None,
                        citations=(opp_citation,),
                        provenance=opp_prov,
                        source="CapabilityOpportunity",
                    )
                )

        for cov in facts.evidence_coverage:
            if str(cov.trial_id) != trial_id:
                continue
            cov_citation = ContextCitation(
                path=cov.source_ref,
                digest=cov.source_digest,
                step_id=None,
                tool_call_id=None,
                supports=cov.construct,
            )
            cov_prov = _fact_row_provenance(cov)
            entry_id = f"{cov.benchmark}:{cov.construct}"
            if cov.analysis_ready is not True or not cov.exposed or bool(cov.missing_evidence):
                if not cov.exposed:
                    c_status = "unexposed"
                elif cov.missing_evidence:
                    c_status = "missing_evidence"
                elif cov.analysis_ready is False:
                    c_status = "not_ready"
                else:
                    c_status = "unknown_readiness"
                if not cov.exposed:
                    c_claim = f"Evidence coverage for construct {cov.construct} is unexposed."
                elif cov.missing_evidence:
                    c_claim = (
                        f"Evidence coverage for construct {cov.construct} is missing evidence: "
                        f"{', '.join(sorted(cov.missing_evidence))}."
                    )
                elif cov.analysis_ready is False:
                    c_claim = (
                        f"Evidence coverage for construct {cov.construct} is not analysis ready."
                    )
                else:
                    c_claim = (
                        f"Evidence coverage for construct {cov.construct} has unknown readiness."
                    )
                unknown_entries.append(
                    ContextEntry(
                        kind="unknown",
                        entry_id=entry_id,
                        claim=c_claim,
                        status=c_status,
                        label="unknown",
                        citations=(cov_citation,),
                        provenance=cov_prov,
                        source="EvidenceCoverage",
                    )
                )
            else:
                c_status = "analysis_ready"
                c_claim = f"Evidence coverage for construct {cov.construct} is analysis ready."
                includable_entries.append(
                    ContextEntry(
                        kind="semantic_fact",
                        entry_id=entry_id,
                        claim=c_claim,
                        status=c_status,
                        label=None,
                        citations=(cov_citation,),
                        provenance=cov_prov,
                        source="EvidenceCoverage",
                    )
                )

        for step_fact in facts.process_step_facts:
            if str(step_fact.trial_id) != trial_id:
                continue
            cit = ContextCitation(
                path=step_fact.source_ref,
                digest=step_fact.source_digest,
                step_id=None,
                tool_call_id=None,
                supports="process_step",
            )
            includable_entries.append(
                ContextEntry(
                    kind="semantic_fact",
                    entry_id=f"{step_fact.source_trajectory_id}:{step_fact.source_step_id}",
                    claim=(
                        f"Step {step_fact.source_step_id} in {step_fact.source_trajectory_id} "
                        f"is labeled {step_fact.label}."
                    ),
                    status=step_fact.label,
                    label=None,
                    citations=(cit,),
                    provenance=_fact_row_provenance(step_fact),
                    source="ProcessStepFact",
                )
            )

        for ret_fact in facts.retrieval_facts:
            if str(ret_fact.trial_id) != trial_id:
                continue
            cit = ContextCitation(
                path=ret_fact.source_ref,
                digest=ret_fact.source_digest,
                step_id=None,
                tool_call_id=None,
                supports="retrieval",
            )
            ref = ret_fact.cited_evidence_ref or ret_fact.source_ref
            includable_entries.append(
                ContextEntry(
                    kind="semantic_fact",
                    entry_id=ref,
                    claim=f"Retrieval fact for {ref}.",
                    status="utilized" if ret_fact.utilized_status else "retrieved",
                    label=None,
                    citations=(cit,),
                    provenance=_fact_row_provenance(ret_fact),
                    source="RetrievalFact",
                )
            )

        for c_fact in facts.constraint_facts:
            if str(c_fact.trial_id) != trial_id:
                continue
            cit = ContextCitation(
                path=c_fact.source_ref,
                digest=c_fact.source_digest,
                step_id=None,
                tool_call_id=None,
                supports=c_fact.constraint_scope,
            )
            claim = f"Constraint {c_fact.constraint_id} has verdict {c_fact.verdict}."
            prov = _fact_row_provenance(c_fact)
            if c_fact.verdict == "unknown":
                unknown_entries.append(
                    ContextEntry(
                        kind="unknown",
                        entry_id=c_fact.constraint_id,
                        claim=claim,
                        status="unknown",
                        label="unknown",
                        citations=(cit,),
                        provenance=prov,
                        source="ConstraintFact",
                    )
                )
            else:
                includable_entries.append(
                    ContextEntry(
                        kind="semantic_fact",
                        entry_id=c_fact.constraint_id,
                        claim=claim,
                        status=c_fact.verdict,
                        label=None,
                        citations=(cit,),
                        provenance=prov,
                        source="ConstraintFact",
                    )
                )

        for op_fact in facts.context_operation_facts:
            if str(op_fact.trial_id) != trial_id:
                continue
            cit = ContextCitation(
                path=op_fact.source_ref,
                digest=op_fact.source_digest,
                step_id=None,
                tool_call_id=None,
                supports=op_fact.operation,
            )
            includable_entries.append(
                ContextEntry(
                    kind="semantic_fact",
                    entry_id=op_fact.operation_id,
                    claim=(
                        f"Context operation {op_fact.operation_id} performed {op_fact.operation}."
                    ),
                    status=op_fact.operation,
                    label=None,
                    citations=(cit,),
                    provenance=_fact_row_provenance(op_fact),
                    source="ContextOperationFact",
                )
            )

        for pair_fact in facts.paired_condition_facts:
            if str(pair_fact.trial_id) != trial_id:
                continue
            cit = ContextCitation(
                path=pair_fact.source_ref,
                digest=pair_fact.source_digest,
                step_id=None,
                tool_call_id=None,
                supports="paired_condition",
            )
            claim = (
                f"Paired condition {pair_fact.pair_id} for {pair_fact.task_id} "
                f"has verdict {pair_fact.primary_verdict}."
            )
            prov = _fact_row_provenance(pair_fact)
            if pair_fact.primary_verdict == "unknown":
                unknown_entries.append(
                    ContextEntry(
                        kind="unknown",
                        entry_id=pair_fact.pair_id,
                        claim=claim,
                        status="unknown",
                        label="unknown",
                        citations=(cit,),
                        provenance=prov,
                        source="PairedConditionFact",
                    )
                )
            else:
                includable_entries.append(
                    ContextEntry(
                        kind="semantic_fact",
                        entry_id=pair_fact.pair_id,
                        claim=claim,
                        status=pair_fact.primary_verdict,
                        label=None,
                        citations=(cit,),
                        provenance=prov,
                        source="PairedConditionFact",
                    )
                )

        for dep_fact in facts.session_dependency_facts:
            if str(dep_fact.trial_id) != trial_id:
                continue
            cit = ContextCitation(
                path=dep_fact.source_ref,
                digest=dep_fact.source_digest,
                step_id=None,
                tool_call_id=None,
                supports="session_dependency",
            )
            entry_id = f"{dep_fact.session_id}:{dep_fact.dependency_edge}"
            includable_entries.append(
                ContextEntry(
                    kind="semantic_fact",
                    entry_id=entry_id,
                    claim=f"Session dependency {dep_fact.dependency_edge} in {dep_fact.session_id}.",
                    status=dep_fact.progress or "unknown",
                    label=None,
                    citations=(cit,),
                    provenance=_fact_row_provenance(dep_fact),
                    source="SessionDependencyFact",
                )
            )

    all_entries = sorted(
        includable_entries,
        key=lambda e: (_KIND_ORDER[e.kind], e.entry_id),
    )

    if max_entries is None:
        kept_entries = all_entries
        omitted_ids: tuple[str, ...] = ()
    else:
        limit = max(0, max_entries)
        kept_entries = all_entries[:limit]
        omitted_ids = tuple(e.entry_id for e in all_entries[limit:])

    truncation = TruncationMetadata(
        truncated=len(omitted_ids) > 0,
        max_entries=max_entries,
        included_count=len(kept_entries),
        omitted_count=len(omitted_ids),
        omitted_entry_ids=omitted_ids,
    )

    unknowns = tuple(
        sorted(
            unknown_entries,
            key=lambda u: (u.source, u.entry_id),
        )
    )

    return TrajectoryContextPack(
        trial_id=trial_id,
        entries=tuple(kept_entries),
        unknowns=unknowns,
        truncation=truncation,
    )
