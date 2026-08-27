"""Disposable trajectory context pack for agent-facing runtime context.

This module is a deterministic, disposable transport/context pack compiler. It is not
a source of truth or a persistent store. Primary truth remains TrialAnalysisSidecar,
AnalysisReview, BehaviorEpisode, and NormalizedFactBundle.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import UUID

from evallab.behavior_episodes import BehaviorEpisode, load_behavior_episodes
from evallab.runner import database_url_from_environment
from evallab.schemas import (
    AnalysisEvidenceCitation,
    AnalysisReview,
    AnalysisSourceDigests,
    TrialAnalysisSidecar,
)
from evallab.semantic_facts import (
    FACT_TYPES,
    FactRow,
    NormalizedFactBundle,
    normalize_bundle,
)
from evallab.storage.paths import derived_root_from_environment


@dataclass(frozen=True)
class InvalidCitation:
    """A citation omitted from a pack, with an explicit validation reason."""

    path: str
    step_id: int | None
    tool_call_id: str | None
    reason: str


@dataclass(frozen=True)
class _CitationIndex:
    """Normalized trajectory evidence used only to validate durable citations."""

    files: frozenset[tuple[str, str]]
    steps: frozenset[tuple[str, str, int]]
    tool_calls: frozenset[tuple[str, str, int | None, str]]


EntryKind = Literal["analysis", "episode", "semantic_fact", "unknown"]
ContextOutputFormat = Literal["markdown", "json"]

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
    invalid_citations: tuple[InvalidCitation, ...] = ()


@dataclass(frozen=True)
class TruncationMetadata:
    truncated: bool
    max_entries: int | None
    max_bytes: int | None = None
    included_count: int = 0
    omitted_count: int = 0
    omitted_entry_ids: tuple[str, ...] = ()
    total_bytes: int = 0
    max_tokens: int | None = None
    tokenizer_bound: bool = False


def _to_json_compatible(obj: Any) -> Any:
    if isinstance(obj, InvalidCitation):
        return {
            "path": obj.path,
            "step_id": obj.step_id,
            "tool_call_id": obj.tool_call_id,
            "reason": obj.reason,
        }
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
            "invalid_citations": [_to_json_compatible(c) for c in obj.invalid_citations],
            "provenance": _to_json_compatible(obj.provenance),
            "source": obj.source,
        }
    if isinstance(obj, TruncationMetadata):
        return {
            "truncated": obj.truncated,
            "max_entries": obj.max_entries,
            "max_bytes": obj.max_bytes,
            "included_count": obj.included_count,
            "omitted_count": obj.omitted_count,
            "omitted_entry_ids": list(obj.omitted_entry_ids),
            "total_bytes": obj.total_bytes,
            "max_tokens": obj.max_tokens,
            "tokenizer_bound": obj.tokenizer_bound,
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
    for invalid in entry.invalid_citations:
        lines.append(f"  - invalid citation `{invalid.path}`: {invalid.reason}")
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

    def to_json(self) -> str:
        """Deterministic JSON serialization used by the claims CLI."""
        return json.dumps(self.to_dict(), sort_keys=True, indent=2) + "\n"

    def to_markdown(self) -> str:
        """Concise Markdown with citations. Deterministic."""
        lines = [f"# Trajectory context — {self.trial_id}"]

        analyses = [e for e in self.entries if e.kind == "analysis"]
        episodes = [e for e in self.entries if e.kind == "episode"]
        facts = [e for e in self.entries if e.kind == "semantic_fact"]

        if analyses:
            lines.extend(["", "## Analyses"])
            for entry in analyses:
                lines.extend(_format_markdown_entry(entry))

        if episodes:
            lines.extend(["", "## Episodes"])
            for entry in episodes:
                lines.extend(_format_markdown_entry(entry))

        if facts:
            lines.extend(["", "## Semantic facts"])
            for entry in facts:
                lines.extend(_format_markdown_entry(entry))

        if self.unknowns:
            lines.extend(["", "## Unknowns"])
            for entry in self.unknowns:
                lines.extend(_format_markdown_entry(entry))

        if self.truncation.truncated and self.truncation.omitted_entry_ids:
            lines.extend(["", "## Truncation"])
            for omitted_id in self.truncation.omitted_entry_ids:
                lines.append(f"- {omitted_id}")

        return "\n".join(lines) + "\n"

    def render(self, output_format: ContextOutputFormat = "markdown") -> str:
        if output_format == "markdown":
            return self.to_markdown()
        if output_format == "json":
            return self.to_json()
        raise ValueError(f"unsupported context output format: {output_format}")


def _with_total_bytes(
    pack: TrajectoryContextPack,
    output_format: ContextOutputFormat,
) -> TrajectoryContextPack:
    """Reach a deterministic serialization-size fixed point."""
    current = pack
    for _ in range(8):
        total_bytes = len(current.render(output_format).encode("utf-8"))
        if current.truncation.total_bytes == total_bytes:
            return current
        current = replace(
            current,
            truncation=replace(current.truncation, total_bytes=total_bytes),
        )
    raise RuntimeError("trajectory context serialization size did not converge")


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


def _resolve_analysis_citation_digest(path: str, digests: AnalysisSourceDigests) -> str | None:
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


def _path_keys(path: str) -> tuple[str, ...]:
    normalized = posixpath.normpath(path)
    return tuple(dict.fromkeys((path, normalized)))


def _load_trajectory_index(derived_root: Path, trial_id: str) -> _CitationIndex:
    """Read normalized steps/tool_calls Parquet rows without introducing new storage."""
    import pyarrow.parquet as pq

    files: set[tuple[str, str]] = set()
    steps: set[tuple[str, str, int]] = set()
    tool_calls: set[tuple[str, str, int | None, str]] = set()
    candidates = sorted(
        {
            path
            for path in (
                *derived_root.glob(f"job_id=*/trial_id={trial_id}/steps.parquet"),
                *derived_root.glob(f"job_id=*/trial_id={trial_id}/tool_calls.parquet"),
                *derived_root.glob("steps/steps.parquet"),
                *derived_root.glob("tool_calls/tool_calls.parquet"),
                *derived_root.glob("steps.parquet"),
                *derived_root.glob("tool_calls.parquet"),
            )
            if path.is_file()
        }
    )
    for path in candidates:
        table = pq.read_table(path)
        rows = table.to_pylist()
        for row in rows:
            if str(row.get("trial_id")) != trial_id:
                continue
            source = str(row.get("source_path") or row.get("document_id") or "")
            digest = str(row.get("source_sha256") or "")
            if source and digest:
                files.update((key, digest) for key in _path_keys(source))
            step = row.get("step_id")
            if path.name == "steps.parquet" and source and digest and step is not None:
                steps.update((key, digest, int(step)) for key in _path_keys(source))
            call = row.get("tool_call_id")
            if path.name == "tool_calls.parquet" and source and digest and call:
                tool_calls.update(
                    (key, digest, int(step) if step is not None else None, str(call))
                    for key in _path_keys(source)
                )
    return _CitationIndex(frozenset(files), frozenset(steps), frozenset(tool_calls))


def _load_semantic_parquet(root: Path, trial_id: str) -> NormalizedFactBundle | None:
    import pyarrow.parquet as pq

    rows_by_type: dict[str, list[FactRow]] = {}
    for name, model in FACT_TYPES.items():
        candidates = (
            root / f"{name}.parquet",
            root / "semantic_facts" / f"{name}.parquet",
        )
        path = next((item for item in candidates if item.is_file()), None)
        if path is None:
            continue
        rows_by_type[name] = [
            model.model_validate(row)
            for row in pq.read_table(path).to_pylist()
            if str(row.get("trial_id")) == trial_id
        ]
    if not rows_by_type:
        return None
    return normalize_bundle(NormalizedFactBundle.model_validate(rows_by_type))


def _load_sidecars(sidecar_roots: Sequence[Path], trial_id: str) -> list[TrialAnalysisSidecar]:
    from evallab.schemas import ANALYSIS_SIDECAR_FILENAME

    loaded: dict[str, TrialAnalysisSidecar] = {}
    for root in sorted({Path(item).expanduser() for item in sidecar_roots}):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob(ANALYSIS_SIDECAR_FILENAME)):
            sidecar = TrialAnalysisSidecar.model_validate_json(path.read_text())
            if str(sidecar.source_trial_id) == trial_id:
                loaded[str(sidecar.analysis_id)] = sidecar
    return [loaded[key] for key in sorted(loaded)]


def _load_sidecar_reviews(
    sidecar_roots: Sequence[Path], analysis_ids: set[str]
) -> list[AnalysisReview]:
    from evallab.schemas import ANALYSIS_REVIEWS_DIRNAME

    loaded: dict[str, AnalysisReview] = {}
    for root in sorted({Path(item).expanduser() for item in sidecar_roots}):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob(f"{ANALYSIS_REVIEWS_DIRNAME}/*.json")):
            review = AnalysisReview.model_validate_json(path.read_text())
            if str(review.analysis_id) in analysis_ids:
                loaded[str(review.review_id)] = review
    return [loaded[key] for key in sorted(loaded)]


_POSTGRES_REVIEWS_SQL = """
SELECT analysis_reviews.id, analysis_reviews.analysis_id,
       analysis_reviews.disposition, analysis_reviews.rationale,
       analysis_reviews.reviewer, analysis_reviews.reviewed_at,
       analysis_reviews.superseded_by
FROM analysis_reviews
JOIN analysis_invocations
  ON analysis_invocations.id = analysis_reviews.analysis_id
WHERE analysis_invocations.source_trial_id = %s
ORDER BY analysis_reviews.reviewed_at, analysis_reviews.id
"""


def _load_postgres_analysis(
    database_url: str, trial_id: str
) -> tuple[list[TrialAnalysisSidecar], list[AnalysisReview]]:
    import psycopg

    analyses: list[TrialAnalysisSidecar] = []
    reviews: list[AnalysisReview] = []
    with psycopg.connect(database_url, connect_timeout=2) as connection:
        rows = connection.execute(
            """
            SELECT id, raw_sidecar FROM analysis_invocations
            WHERE source_trial_id = %s ORDER BY id
            """,
            (trial_id,),
        ).fetchall()
        by_id: dict[str, TrialAnalysisSidecar] = {}
        for analysis_id, raw_sidecar in rows:
            if raw_sidecar:
                by_id[str(analysis_id)] = TrialAnalysisSidecar.model_validate(raw_sidecar)
        finding_rows = connection.execute(
            """
            SELECT f.analysis_id, f.validity, f.primary_category, f.summary,
                   f.earliest_failure_step_id, f.confidence,
                   f.proposed_discriminator, f.alternative_explanations
            FROM analysis_findings f
            JOIN analysis_invocations i ON i.id = f.analysis_id
            WHERE i.source_trial_id = %s ORDER BY f.analysis_id
            """,
            (trial_id,),
        ).fetchall()
        citation_rows = connection.execute(
            """
            SELECT c.analysis_id, c.citation_index, c.source_path,
                   c.step_id, c.tool_call_id, c.supports
            FROM analysis_evidence_citations c
            JOIN analysis_invocations i ON i.id = c.analysis_id
            WHERE i.source_trial_id = %s
            ORDER BY c.analysis_id, c.citation_index
            """,
            (trial_id,),
        ).fetchall()
        citations_by_id: dict[str, list[AnalysisEvidenceCitation]] = defaultdict(list)
        for row in citation_rows:
            citations_by_id[str(row[0])].append(
                AnalysisEvidenceCitation(
                    path=row[2],
                    step_id=row[3],
                    tool_call_id=row[4],
                    supports=row[5],
                )
            )
        for row in finding_rows:
            analysis_id = str(row[0])
            sidecar = by_id.get(analysis_id)
            if sidecar is None:
                continue
            alternatives = row[7] if isinstance(row[7], list) else json.loads(row[7] or "[]")
            output = sidecar.output.model_copy(
                update={
                    "validity": row[1],
                    "primary_category": row[2],
                    "summary": row[3],
                    "earliest_failure_step_id": row[4],
                    "confidence": row[5],
                    "proposed_discriminator": row[6],
                    "alternative_explanations": alternatives,
                    "evidence": citations_by_id.get(analysis_id, list(sidecar.output.evidence)),
                }
            )
            by_id[analysis_id] = sidecar.model_copy(update={"output": output})
        analyses = [by_id[key] for key in sorted(by_id)]
        review_rows = connection.execute(
            _POSTGRES_REVIEWS_SQL,
            (trial_id,),
        ).fetchall()
        for row in review_rows:
            reviews.append(
                AnalysisReview(
                    review_id=row[0],
                    analysis_id=row[1],
                    disposition=row[2],
                    rationale=row[3],
                    reviewer=row[4],
                    reviewed_at=row[5],
                    superseded_by=row[6],
                )
            )
    return analyses, reviews


def _validate_citation(
    citation: ContextCitation,
    *,
    expected_digest: str | None,
    index: _CitationIndex | None,
) -> str | None:
    if index is None:
        return None
    keys = _path_keys(citation.path)
    if expected_digest is None:
        return "source digest unavailable"
    if not any((key, expected_digest) in index.files for key in keys):
        return "source digest does not match normalized trajectory"
    if citation.step_id is not None and not any(
        (key, expected_digest, citation.step_id) in index.steps for key in keys
    ):
        return f"step {citation.step_id} is not present in normalized trajectory"
    if citation.tool_call_id is not None:
        if citation.step_id is not None:
            found = any(
                (key, expected_digest, citation.step_id, citation.tool_call_id) in index.tool_calls
                for key in keys
            )
        else:
            found = any(
                item[0] in keys and item[1] == expected_digest and item[3] == citation.tool_call_id
                for item in index.tool_calls
            )
        if not found:
            return f"tool call {citation.tool_call_id!r} is not present in normalized trajectory"
    return None


def _citation_path_matches(source_path: str, citation_path: str) -> bool:
    source_parts = PurePosixPath(source_path.replace("\\", "/")).parts
    citation_parts = PurePosixPath(citation_path).parts
    return bool(citation_parts) and (
        source_parts == citation_parts
        or len(source_parts) >= len(citation_parts)
        and source_parts[-len(citation_parts) :] == citation_parts
    )


def _augment_index_with_sidecar_files(
    index: _CitationIndex,
    analyses: Sequence[TrialAnalysisSidecar],
    repo_root: Path,
) -> _CitationIndex:
    files = set(index.files)
    steps = set(index.steps)
    tool_calls = set(index.tool_calls)
    for sidecar in analyses:
        trial_path = Path(sidecar.source_trial_path)
        trial_root = trial_path if trial_path.is_absolute() else repo_root / trial_path
        for evidence in sidecar.output.evidence:
            expected = _resolve_analysis_citation_digest(evidence.path, sidecar.source_digests)
            if expected is None:
                continue
            citation_path = PurePosixPath(evidence.path)
            if citation_path.is_absolute() or ".." in citation_path.parts:
                continue
            candidates = sorted(
                {
                    (repo_root / evidence.path).resolve(),
                    (trial_root / evidence.path).resolve(),
                    (repo_root / "runs" / str(sidecar.source_trial_id) / evidence.path).resolve(),
                },
                key=lambda path: path.as_posix(),
            )
            for candidate in candidates:
                if not candidate.is_file():
                    continue
                digest = "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()
                if digest != expected:
                    continue
                citation_keys = _path_keys(evidence.path)
                files.update((key, expected) for key in citation_keys)
                for source, source_digest, step_id in index.steps:
                    if source_digest == expected and _citation_path_matches(source, evidence.path):
                        steps.update((key, expected, step_id) for key in citation_keys)
                for source, source_digest, step_id, tool_call_id in index.tool_calls:
                    if source_digest == expected and _citation_path_matches(source, evidence.path):
                        tool_calls.update(
                            (key, expected, step_id, tool_call_id) for key in citation_keys
                        )
                break
    return _CitationIndex(
        frozenset(files),
        frozenset(steps),
        frozenset(tool_calls),
    )


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
    max_bytes: int | None = None,
    max_tokens: int | None = None,
    tokenizer: Callable[[str], int] | Any | None = None,
    output_format: ContextOutputFormat = "markdown",
    citation_index: _CitationIndex | None = None,
) -> TrajectoryContextPack:
    """Compile a deterministic pack.

    The sole supersession rule is: select each analysis' latest review by
    ``(reviewed_at, review_id)``; a current ``superseded`` review removes that
    analysis, and only its explicit ``superseded_by`` chain's terminal current
    review can be emitted. This preserves the review store as the only truth.
    Token limits are accepted only with an explicit tokenizer and are marked
    tokenizer-bound; byte limits are exact UTF-8 bounds, never token estimates.
    """
    if max_tokens is not None and tokenizer is None:
        raise ValueError("max_tokens requires an explicit tokenizer")
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    if max_tokens is not None and max_tokens < 0:
        raise ValueError("max_tokens must be non-negative")
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

        valid_citations: list[ContextCitation] = []
        invalid_citations: list[InvalidCitation] = []
        for ev in sidecar.output.evidence:
            digest = _resolve_analysis_citation_digest(ev.path, sidecar.source_digests)
            citation = ContextCitation(
                path=ev.path,
                digest=digest,
                step_id=ev.step_id,
                tool_call_id=ev.tool_call_id,
                supports=ev.supports,
            )
            reason = _validate_citation(citation, expected_digest=digest, index=citation_index)
            if reason is None:
                valid_citations.append(citation)
            else:
                invalid_citations.append(
                    InvalidCitation(ev.path, ev.step_id, ev.tool_call_id, reason)
                )
        citations = tuple(sorted(valid_citations, key=_citation_sort_key))
        invalid_citations_sorted = tuple(
            sorted(
                invalid_citations,
                key=lambda item: (
                    item.path,
                    item.step_id is None,
                    item.step_id or 0,
                    item.tool_call_id or "",
                    item.reason,
                ),
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
            "source_digests.trajectory": sidecar.source_digests.trajectory,
            "citation_rejections": tuple(
                (item.path, item.step_id, item.tool_call_id, item.reason)
                for item in invalid_citations_sorted
            ),
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
                invalid_citations=invalid_citations_sorted,
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
                f_status = (
                    "ineligible"
                    if opp.eligible is False
                    else ("missing_evidence" if opp.missing_evidence else "unknown_eligibility")
                )
                f_claim = (
                    f"Opportunity {opp.opportunity_id} for construct {opp.construct} is missing evidence: {', '.join(sorted(opp.missing_evidence))}."
                    if opp.missing_evidence
                    else (
                        f"Opportunity {opp.opportunity_id} for construct {opp.construct} is ineligible."
                        if opp.eligible is False
                        else f"Opportunity {opp.opportunity_id} for construct {opp.construct} has unknown eligibility."
                    )
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
                includable_entries.append(
                    ContextEntry(
                        kind="semantic_fact",
                        entry_id=opp.opportunity_id,
                        claim=f"Opportunity {opp.opportunity_id} for construct {opp.construct} is eligible with complete evidence.",
                        status="eligible",
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
                c_status = (
                    "unexposed"
                    if not cov.exposed
                    else (
                        "missing_evidence"
                        if cov.missing_evidence
                        else ("not_ready" if cov.analysis_ready is False else "unknown_readiness")
                    )
                )
                c_claim = (
                    f"Evidence coverage for construct {cov.construct} is unexposed."
                    if not cov.exposed
                    else (
                        f"Evidence coverage for construct {cov.construct} is missing evidence: {', '.join(sorted(cov.missing_evidence))}."
                        if cov.missing_evidence
                        else (
                            f"Evidence coverage for construct {cov.construct} is not analysis ready."
                            if cov.analysis_ready is False
                            else f"Evidence coverage for construct {cov.construct} has unknown readiness."
                        )
                    )
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
                includable_entries.append(
                    ContextEntry(
                        kind="semantic_fact",
                        entry_id=entry_id,
                        claim=f"Evidence coverage for construct {cov.construct} is analysis ready.",
                        status="ready",
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
                supports=step_fact.source_step_id,
            )
            entry_id = f"{step_fact.source_trajectory_id}:{step_fact.source_step_id}"
            includable_entries.append(
                ContextEntry(
                    kind="semantic_fact",
                    entry_id=entry_id,
                    claim=f"Process step {step_fact.source_step_id} is {step_fact.label}.",
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
            doc_or_file = (
                ret_fact.document_id
                or ret_fact.file_id
                or ret_fact.block_id
                or ret_fact.line_id
                or "unknown"
            )
            cit = ContextCitation(
                path=ret_fact.source_ref,
                digest=ret_fact.source_digest,
                step_id=None,
                tool_call_id=ret_fact.call_id,
                supports=doc_or_file,
            )
            entry_id = f"{ret_fact.query_id or 'q'}:{doc_or_file}"
            prov = _fact_row_provenance(ret_fact)
            if ret_fact.utilized_status is None:
                unknown_entries.append(
                    ContextEntry(
                        kind="unknown",
                        entry_id=entry_id,
                        claim=f"Retrieval for query {ret_fact.query_id or 'unknown'} on {doc_or_file} has unrecorded utilization.",
                        status="unknown_utilization",
                        label="unknown",
                        citations=(cit,),
                        provenance=prov,
                        source="RetrievalFact",
                    )
                )
            else:
                util_label = "utilized" if ret_fact.utilized_status else "not_utilized"
                includable_entries.append(
                    ContextEntry(
                        kind="semantic_fact",
                        entry_id=entry_id,
                        claim=f"Retrieval for query {ret_fact.query_id or 'unknown'} on {doc_or_file} was {util_label}.",
                        status=util_label,
                        label=None,
                        citations=(cit,),
                        provenance=prov,
                        source="RetrievalFact",
                    )
                )

        for const_fact in facts.constraint_facts:
            if str(const_fact.trial_id) != trial_id:
                continue
            cit = ContextCitation(
                path=const_fact.source_ref,
                digest=const_fact.source_digest,
                step_id=None,
                tool_call_id=const_fact.action_id,
                supports=const_fact.constraint_id,
            )
            entry_id = f"{const_fact.plan_id}:{const_fact.constraint_id}"
            prov = _fact_row_provenance(const_fact)
            claim = f"Constraint {const_fact.constraint_id} in {const_fact.plan_id} has verdict {const_fact.verdict}."
            if const_fact.verdict == "unknown":
                unknown_entries.append(
                    ContextEntry(
                        kind="unknown",
                        entry_id=entry_id,
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
                        entry_id=entry_id,
                        claim=claim,
                        status=const_fact.verdict,
                        label=None,
                        citations=(cit,),
                        provenance=prov,
                        source="ConstraintFact",
                    )
                )

        for ctx_fact in facts.context_operation_facts:
            if str(ctx_fact.trial_id) != trial_id:
                continue
            cit = ContextCitation(
                path=ctx_fact.source_ref,
                digest=ctx_fact.source_digest,
                step_id=None,
                tool_call_id=None,
                supports=ctx_fact.operation,
            )
            prov = _fact_row_provenance(ctx_fact)
            if ctx_fact.realized_size is None:
                unknown_entries.append(
                    ContextEntry(
                        kind="unknown",
                        entry_id=ctx_fact.operation_id,
                        claim=f"Context operation {ctx_fact.operation_id} ({ctx_fact.operation}) has unknown realized size.",
                        status="unknown_size",
                        label="unknown",
                        citations=(cit,),
                        provenance=prov,
                        source="ContextOperationFact",
                    )
                )
            else:
                includable_entries.append(
                    ContextEntry(
                        kind="semantic_fact",
                        entry_id=ctx_fact.operation_id,
                        claim=f"Context operation {ctx_fact.operation_id} ({ctx_fact.operation}) realized {ctx_fact.realized_size} units.",
                        status="recorded",
                        label=None,
                        citations=(cit,),
                        provenance=prov,
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
            claim = f"Paired condition {pair_fact.pair_id} for {pair_fact.task_id} has verdict {pair_fact.primary_verdict}."
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

    all_unknowns = sorted(
        unknown_entries,
        key=lambda entry: (entry.source, entry.entry_id),
    )
    entry_limit = len(all_entries)
    if max_entries is not None:
        entry_limit = min(entry_limit, max(0, max_entries))
    max_entry_omissions = tuple(all_entries[entry_limit:])
    budget_candidates = [
        *((False, entry) for entry in all_entries[:entry_limit]),
        *((True, entry) for entry in all_unknowns),
    ]
    candidate_limit = len(budget_candidates)

    def token_count(text: str) -> int:
        if tokenizer is None:
            return 0
        if callable(tokenizer):
            count = int(tokenizer(text))
        else:
            encode = getattr(tokenizer, "encode", None)
            if not callable(encode):
                raise TypeError("tokenizer must be callable or provide encode()")
            count = len(encode(text))
        if count < 0:
            raise ValueError("tokenizer returned a negative token count")
        return count

    def candidate_pack(count: int) -> TrajectoryContextPack:
        kept = budget_candidates[:count]
        omitted_entries = [
            *max_entry_omissions,
            *(entry for _, entry in budget_candidates[count:]),
        ]
        omitted = tuple(entry.entry_id for entry in omitted_entries)
        kept_entries = tuple(entry for is_unknown, entry in kept if not is_unknown)
        kept_unknowns = tuple(entry for is_unknown, entry in kept if is_unknown)
        pack = TrajectoryContextPack(
            trial_id=trial_id,
            entries=kept_entries,
            unknowns=kept_unknowns,
            truncation=TruncationMetadata(
                truncated=bool(omitted),
                max_entries=max_entries,
                max_bytes=max_bytes,
                included_count=len(kept_entries),
                omitted_count=len(omitted),
                omitted_entry_ids=omitted,
                max_tokens=max_tokens,
                tokenizer_bound=max_tokens is not None,
            ),
        )
        return _with_total_bytes(pack, output_format)

    if max_bytes is None and max_tokens is None:
        return candidate_pack(candidate_limit)

    for count in range(candidate_limit, -1, -1):
        candidate = candidate_pack(count)
        rendered = candidate.render(output_format)
        if (max_bytes is None or len(rendered.encode("utf-8")) <= max_bytes) and (
            max_tokens is None or token_count(rendered) <= max_tokens
        ):
            return candidate
    raise ValueError(f"{output_format} context pack cannot satisfy the requested output budget")


# Functional alias
compile_context_pack = build_trajectory_context


def build_durable_trajectory_context(
    *,
    trial_id: str,
    repo_root: Path | None = None,
    derived_root: Path | None = None,
    database_url: str | None = None,
    sidecar_roots: Sequence[Path] = (),
    semantic_root: Path | None = None,
    include_candidates: bool = False,
    include_rejected: bool = False,
    max_entries: int | None = None,
    max_bytes: int | None = None,
    max_tokens: int | None = None,
    tokenizer: Callable[[str], int] | Any | None = None,
    output_format: ContextOutputFormat = "markdown",
) -> TrajectoryContextPack:
    """Load durable catalog/Parquet/sidecar surfaces, then compile one pack."""
    root = (repo_root or Path.cwd()).resolve()
    derived = (
        derived_root.resolve() if derived_root is not None else derived_root_from_environment(root)
    )
    roots = list(sidecar_roots)
    if not roots:
        roots = [
            root / "derived" / "analyses",
            root / "research" / "evidence" / "analyses",
            root / "research" / "analysis",
        ]
    analyses = _load_sidecars(roots, trial_id)
    reviews = _load_sidecar_reviews(roots, {str(item.analysis_id) for item in analyses})
    configured_database_url = database_url
    if configured_database_url is None and os.environ.get("DATABASE_URL"):
        configured_database_url = database_url_from_environment()
    if configured_database_url:
        db_analyses, db_reviews = _load_postgres_analysis(
            configured_database_url,
            trial_id,
        )
        by_id = {str(item.analysis_id): item for item in analyses}
        for item in db_analyses:
            by_id.setdefault(str(item.analysis_id), item)
        analyses = [by_id[key] for key in sorted(by_id)]
        by_review = {str(item.review_id): item for item in reviews}
        by_review.update({str(item.review_id): item for item in db_reviews})
        reviews = [by_review[key] for key in sorted(by_review)]
    episodes = load_behavior_episodes(repo_root=root, derived_root=derived)
    semantic = _load_semantic_parquet(semantic_root or derived, trial_id)
    index = _load_trajectory_index(derived, trial_id)
    index = _augment_index_with_sidecar_files(index, analyses, root)
    return build_trajectory_context(
        trial_id=trial_id,
        analyses=analyses,
        reviews=reviews,
        episodes=episodes,
        facts=semantic,
        include_candidates=include_candidates,
        include_rejected=include_rejected,
        max_entries=max_entries,
        max_bytes=max_bytes,
        max_tokens=max_tokens,
        tokenizer=tokenizer,
        output_format=output_format,
        citation_index=index,
    )


compile_durable_context_pack = build_durable_trajectory_context
