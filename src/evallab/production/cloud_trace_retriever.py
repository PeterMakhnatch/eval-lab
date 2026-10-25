"""Deterministic compact DEV-only verified-trace retrieval for seeker prompt augmentation.

``CloudTraceRetriever`` answers ``retrieve(task_id=..., query=...)`` with a
``TraceRetrievalResult``: at most eight safe ``RetrievedTraceHit`` projections
plus a receipt, and ``render_prompt_markdown`` caps the rendered block at 4500
characters including the heading so it can be spliced into an
``OmpHeadlessAttemptGenerator`` prompt with no tools or model calls.

Public contract:

- Construction takes an existing ``CloudTraceDatabase`` (or any object with a
  compatible ``search_traces`` method — caller-owned and never closed by the
  retriever) or a plain local SQLite path. Path mode opens the source
  read-only through a SQLite ``mode=ro`` URI, snapshots it with the sqlite3
  backup API into a private temporary file, and instantiates
  ``CloudTraceDatabase`` on the copy only; the receipt then records the
  resolved source path and the SHA-256 of the snapshot actually queried. The
  original file is never opened for write, which matters because
  ``CloudTraceDatabase.__init__`` executes DDL. A caller-owned store instead
  requires explicit ``source_path``/``source_digest`` keyword provenance: the
  caller attests a plain local path and the lowercase SHA-256 hex digest of
  the snapshot the instance was built on (the retriever cannot observe an
  already-open connection's origin). Environment database configuration is
  never consulted; URLs and DSNs are refused. ``close()`` (or the context
  manager) releases only owned snapshot state.
- ``retrieve`` requires a nonblank ``task_id`` and a ``query`` containing at
  least one ``\\w+`` word token; blank or punctuation-only queries raise
  ``ValueError`` before the store is touched. The query is deterministically
  normalized to its space-joined word tokens before calling
  ``search_traces(query, only_verified=True)``.
- ``search_traces`` returns every match (it has no limit), so the retriever
  deterministically re-sorts candidates by ``trace_id``, issues by
  ``issue_id``, and steps by ``step_index``, postfilters the full candidate
  list, and only then applies the inclusion caps — it never slices eight
  candidates before filtering. Receipt counts therefore cover the entire
  candidate set, not a truncated page. Candidates are excluded, in check
  order, when they belong to the caller's own task (``same_task``), have a
  missing or mismatched hydrated task (``task_mismatch``), lack an explicit
  ``dev`` split after case normalization (``non_dev_split``), lack a receipt
  status of ``confirmed``/``verified`` checked independently of the backend
  filter (``unverified_receipt``), or carry no usable ``confirmed`` issue
  (``no_confirmed_issue``). Issues whose ``task_id`` disagrees with their
  trace's are dropped per issue (``issue_mismatch``). Projected hits that
  dedupe to an already-seen projection count as ``duplicate``; admissible
  hits beyond the eight-hit cap count as ``hit_cap``; admissible hits whose
  bullet would not fit the rendered character budget count as
  ``rendered_char_cap``.
- Hits project only ``task_id``, ``defect_class``, a bounded ``mechanism``
  derived from explicit issue evidence (direction + defect class + first
  observed step tool name), a ``citation`` (``file[:line]``), and at most one
  short step excerpt. Hydrated traces, raw CAS URIs, task instruction text,
  reproducer scripts, complete steps, receipts, and arbitrary metadata are
  never returned in hits, the result, or the markdown. Trace and issue IDs
  appear only inside the receipt.
- Inclusion is render-budgeted: a hit is only included when its main bullet
  still fits within ``MAX_PROMPT_CHARACTERS``, so every receipt ID
  corresponds to a rendered bullet. ``render_prompt_markdown`` may still drop
  optional citation/excerpt detail lines under the 4500-character cap, and
  escapes markdown metacharacters; every projected field has control
  characters blanked and whitespace (including newlines) collapsed before it
  is stored on the hit.
"""

from __future__ import annotations

import copy
import hashlib
import re
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.production.cloud_trace_db import CloudTraceDatabase

MAX_INCLUDED_HITS = 8
MAX_PROMPT_CHARACTERS = 4500

_DEV_SPLIT = "dev"
_CONFIRMED_ISSUE = "confirmed"
_VERIFIED_RECEIPTS = frozenset({"confirmed", "verified"})
_WORD = re.compile(r"\w+", flags=re.UNICODE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]<>])")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_STEP_TEXT_FIELDS = ("content_text", "command_or_action", "observation_preview")
_TASK_ID_LIMIT = 160
_DEFECT_CLASS_LIMIT = 120
_MECHANISM_LIMIT = 200
_CITATION_LIMIT = 200
_EXCERPT_LIMIT = 240
_HEADING = "## Related verified DEV evidence"


def _clip(text: Any, limit: int) -> str:
    """Blank control characters, collapse whitespace, and bound to ``limit``."""
    collapsed = " ".join(_CONTROL.sub(" ", str(text)).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _markdown(text: str) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def _citation(issue: Mapping[str, Any]) -> str | None:
    file = issue.get("citation_file")
    line = issue.get("citation_line")
    if isinstance(file, str) and file.strip():
        return _clip(file if line is None else f"{file}:{line}", _CITATION_LIMIT)
    if line is not None:
        return _clip(f"line {line}", _CITATION_LIMIT)
    return None


def _mechanism(issue: Mapping[str, Any], steps: Any) -> str:
    direction = _clip(issue.get("direction") or "", 40) or "defect"
    defect = _clip(issue.get("defect_class") or "", _DEFECT_CLASS_LIMIT) or "unclassified"
    tool = ""
    if isinstance(steps, Sequence) and not isinstance(steps, str):
        for step in steps:
            if isinstance(step, Mapping):
                name = step.get("tool_name")
                if isinstance(name, str) and name.strip():
                    tool = _clip(name, 60)
                    break
    text = f"{direction} {defect}" + (f" observed via {tool}" if tool else "")
    return _clip(text, _MECHANISM_LIMIT)


def _step_excerpt(steps: Any, terms: Sequence[str]) -> str | None:
    if not isinstance(steps, Sequence) or isinstance(steps, str):
        return None
    candidates: list[str] = []
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        for field_name in _STEP_TEXT_FIELDS:
            text = step.get(field_name)
            if isinstance(text, str) and text.strip():
                candidates.append(text)
    if not candidates:
        return None
    lowered = [term.casefold() for term in terms]
    for candidate in candidates:
        folded = candidate.casefold()
        if any(term in folded for term in lowered):
            return _clip(candidate, _EXCERPT_LIMIT)
    return _clip(candidates[0], _EXCERPT_LIMIT)


def _project(trace: Mapping[str, Any], issue: Mapping[str, Any], terms: Sequence[str]) -> RetrievedTraceHit:
    steps = _ordered_steps(trace.get("steps"))
    defect = _clip(issue.get("defect_class") or "", _DEFECT_CLASS_LIMIT) or "unclassified"
    return RetrievedTraceHit(
        task_id=_clip(trace.get("task_id") or "", _TASK_ID_LIMIT),
        defect_class=defect,
        mechanism=_mechanism(issue, steps),
        citation=_citation(issue),
        step_excerpt=_step_excerpt(steps, terms),
    )


def _trace_key(trace: Any) -> str:
    if isinstance(trace, Mapping) and trace.get("trace_id") is not None:
        return str(trace["trace_id"])
    return ""


def _issue_key(issue: Any) -> str:
    if isinstance(issue, Mapping) and issue.get("issue_id") is not None:
        return str(issue["issue_id"])
    return ""


def _ordered_steps(steps: Any) -> list[Any]:
    if not isinstance(steps, Sequence) or isinstance(steps, str):
        return []
    indexed = list(enumerate(steps))
    indexed.sort(key=_step_order)
    return [step for _, step in indexed]


def _step_order(pair: tuple[int, Any]) -> tuple[int, int, int]:
    index, step = pair
    value = step.get("step_index") if isinstance(step, Mapping) else None
    if isinstance(value, int) and not isinstance(value, bool):
        return (0, value, index)
    return (1, 0, index)


def _hit_bullet(hit: RetrievedTraceHit) -> str:
    return f"- `{_markdown(hit.task_id)}` — {_markdown(hit.defect_class)}: {_markdown(hit.mechanism)}"


def _hit_detail(hit: RetrievedTraceHit) -> str:
    details = []
    if hit.citation:
        details.append(f"citation `{_markdown(hit.citation)}`")
    if hit.step_excerpt:
        details.append(f"excerpt: {_markdown(hit.step_excerpt)}")
    return "  " + " · ".join(details) if details else ""


@dataclass(frozen=True)
class RetrievedTraceHit:
    """Safe bounded projection of one confirmed issue on a verified DEV trace."""

    task_id: str
    defect_class: str
    mechanism: str
    citation: str | None = None
    step_excerpt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "defect_class": self.defect_class,
            "mechanism": self.mechanism,
            "citation": self.citation,
            "step_excerpt": self.step_excerpt,
        }


@dataclass(frozen=True)
class TraceRetrievalResult:
    """Bounded retrieval output; ``receipt`` records provenance and counts."""

    hits: tuple[RetrievedTraceHit, ...]
    receipt: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": [hit.to_dict() for hit in self.hits],
            "receipt": copy.deepcopy(self.receipt),
        }

    def render_prompt_markdown(self) -> str:
        """Render hits as an escaped markdown block capped at 4500 characters.

        Every included hit keeps its main bullet; only optional
        citation/excerpt detail lines are dropped under the cap.
        """
        text = _HEADING
        if not self.hits:
            return text + "\n\n_No verified DEV trace evidence matched this query._"
        remaining = [1 + len(_hit_bullet(hit)) for hit in self.hits]
        for index, hit in enumerate(self.hits):
            bullet = _hit_bullet(hit)
            detail = _hit_detail(hit)
            block = bullet + ("\n" + detail if detail else "")
            rest = sum(remaining[index + 1 :])
            if len(text) + 1 + len(block) + rest <= MAX_PROMPT_CHARACTERS:
                text += "\n" + block
            elif len(text) + 1 + len(bullet) + rest <= MAX_PROMPT_CHARACTERS:
                text += "\n" + bullet
        return text


class CloudTraceRetriever:
    """DEV-only verified-evidence retriever over a CloudTraceDatabase projection.

    Pass an existing ``CloudTraceDatabase`` (caller keeps ownership; the
    retriever never closes it) together with explicit ``source_path`` /
    ``source_digest`` attestation, or a plain local SQLite file path which is
    opened read-only and snapshotted into a private temporary copy before
    ``CloudTraceDatabase`` runs its DDL on the copy.
    """

    def __init__(
        self,
        database: CloudTraceDatabase | str | Path | None = None,
        *,
        source_path: str | Path | None = None,
        source_digest: str | None = None,
    ) -> None:
        self._db: CloudTraceDatabase
        self._owned = False
        self._provenance = "caller_attested"
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        self._source_path: str | None = None
        self._source_digest: str | None = None
        if database is None:
            raise ValueError(
                "pass an existing CloudTraceDatabase or a local SQLite path; "
                "environment database configuration is never consulted"
            )
        if isinstance(database, (str, Path)):
            if source_path is not None or source_digest is not None:
                raise ValueError(
                    "path databases record derived provenance; "
                    "do not pass source_path/source_digest"
                )
            self._open_path(database)
        elif callable(getattr(database, "search_traces", None)):
            self._attest_provenance(source_path, source_digest)
            self._db = database
        else:
            raise TypeError("database must be a CloudTraceDatabase or a local SQLite path")

    def __enter__(self) -> CloudTraceRetriever:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @property
    def source_path(self) -> str | None:
        """Resolved (snapshot) or caller-attested source path."""
        return self._source_path

    @property
    def source_digest(self) -> str | None:
        """SHA-256 of the queried snapshot (measured) or attested source."""
        return self._source_digest

    def _attest_provenance(self, source_path: str | Path | None, source_digest: str | None) -> None:
        if source_path is None or source_digest is None:
            raise ValueError(
                "caller-owned databases require explicit source_path and source_digest "
                "provenance: the plain local path and the lowercase SHA-256 of the "
                "snapshot the instance was built on"
            )
        raw = str(source_path).strip()
        if not raw or "://" in raw or raw.startswith("file:"):
            raise ValueError("source_path must be a nonblank plain local path, not a URL or DSN")
        if not isinstance(source_digest, str) or not _SHA256.fullmatch(source_digest):
            raise ValueError("source_digest must be a 64-character lowercase SHA-256 hex digest")
        self._source_path = raw
        self._source_digest = source_digest

    def _open_path(self, database: str | Path) -> None:
        raw = str(database)
        if "://" in raw or raw.startswith("file:"):
            raise ValueError("database must be a plain local SQLite path, not a URL or DSN")
        try:
            resolved = Path(raw).expanduser().resolve(strict=True)
        except FileNotFoundError:
            raise FileNotFoundError(f"trace database not found: {raw}") from None
        if not resolved.is_file():
            raise ValueError(f"trace database is not a file: {resolved}")
        readonly = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)
        try:
            self._tempdir = tempfile.TemporaryDirectory(prefix="evallab-trace-snapshot-")
            snapshot = Path(self._tempdir.name) / "snapshot.sqlite3"
            target = sqlite3.connect(str(snapshot))
            try:
                readonly.backup(target)
            finally:
                target.close()
            self._db = CloudTraceDatabase(snapshot)
        except BaseException:
            if self._tempdir is not None:
                self._tempdir.cleanup()
                self._tempdir = None
            raise
        finally:
            readonly.close()
        self._owned = True
        self._provenance = "snapshot"
        self._source_path = str(resolved)
        self._source_digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()

    def close(self) -> None:
        """Drop owned snapshot state; a caller-supplied database stays open."""
        if self._owned:
            self._db.close()
            self._owned = False
        if self._tempdir is not None:
            self._tempdir.cleanup()
            self._tempdir = None

    def retrieve(self, task_id: str, query: str) -> TraceRetrievalResult:
        """Return bounded safe hits for verified DEV traces related to ``query``.

        ``task_id`` is the caller's current task: its own traces are excluded.
        Blank ``task_id`` or a blank/punctuation-only ``query`` raises
        ``ValueError`` before the store is accessed.
        """
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id must be a nonblank string")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a nonblank string")
        terms = _WORD.findall(query)
        if not terms:
            raise ValueError("query must contain at least one word token")
        current = task_id.strip()
        normalized = " ".join(terms)

        candidates = list(self._db.search_traces(normalized, only_verified=True) or [])
        candidates.sort(key=_trace_key)
        counts: dict[str, int] = {
            "same_task": 0,
            "task_mismatch": 0,
            "non_dev_split": 0,
            "unverified_receipt": 0,
            "issue_mismatch": 0,
            "no_confirmed_issue": 0,
            "duplicate": 0,
            "hit_cap": 0,
            "rendered_char_cap": 0,
        }
        hits: list[RetrievedTraceHit] = []
        trace_ids: list[str] = []
        issue_ids: list[str] = []
        seen: set[tuple[str | None, ...]] = set()
        rendered_budget = len(_HEADING)
        for trace in candidates:
            if not isinstance(trace, Mapping):
                counts["task_mismatch"] += 1
                continue
            if trace.get("task_id") == current:
                counts["same_task"] += 1
                continue
            task = trace.get("task")
            if not isinstance(task, Mapping) or task.get("task_id") != trace.get("task_id"):
                counts["task_mismatch"] += 1
                continue
            if str(task.get("split") or "").strip().casefold() != _DEV_SPLIT:
                counts["non_dev_split"] += 1
                continue
            receipt = trace.get("verified_receipt_json")
            if (
                not isinstance(receipt, Mapping)
                or str(receipt.get("status") or "").strip().casefold() not in _VERIFIED_RECEIPTS
            ):
                counts["unverified_receipt"] += 1
                continue
            raw_issues = trace.get("issues")
            confirmed: list[Mapping[str, Any]] = []
            if isinstance(raw_issues, Sequence) and not isinstance(raw_issues, str):
                for issue in raw_issues:
                    if not isinstance(issue, Mapping):
                        continue
                    if issue.get("task_id") != trace.get("task_id"):
                        counts["issue_mismatch"] += 1
                        continue
                    if str(issue.get("status") or "").strip().casefold() == _CONFIRMED_ISSUE:
                        confirmed.append(issue)
            confirmed.sort(key=_issue_key)
            if not confirmed:
                counts["no_confirmed_issue"] += 1
                continue
            for issue in confirmed:
                hit = _project(trace, issue, terms)
                key = (hit.task_id, hit.defect_class, hit.mechanism, hit.citation, hit.step_excerpt)
                if key in seen:
                    counts["duplicate"] += 1
                    continue
                seen.add(key)
                if len(hits) >= MAX_INCLUDED_HITS:
                    counts["hit_cap"] += 1
                    continue
                bullet_length = 1 + len(_hit_bullet(hit))
                if rendered_budget + bullet_length > MAX_PROMPT_CHARACTERS:
                    counts["rendered_char_cap"] += 1
                    continue
                rendered_budget += bullet_length
                hits.append(hit)
                trace_ids.append(str(trace.get("trace_id") or ""))
                issue_ids.append(str(issue.get("issue_id") or ""))
        receipt: dict[str, Any] = {
            "query": normalized,
            "query_terms": terms,
            "task_id": current,
            "candidates": len(candidates),
            "included_hits": len(hits),
            "exclusion_counts": counts,
            "trace_ids": list(dict.fromkeys(trace_ids)),
            "issue_ids": list(dict.fromkeys(issue_ids)),
            "provenance": self._provenance,
            "source_path": self._source_path,
            "source_digest": self._source_digest,
        }
        return TraceRetrievalResult(hits=tuple(hits), receipt=receipt)
