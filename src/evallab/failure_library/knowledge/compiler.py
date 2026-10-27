"""Compile DEV evidence into existing Hackbook mechanisms without model calls.

Admission reuses CloudTraceRetriever's structured gate. Unknown group metadata
is handled conservatively: leave-subject-out excludes the entire upstream repo
unless both subjects have authoritative split-ledger units.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from evallab.failure_library.knowledge.models import CardDraft, ExampleRef, FailureCard
from evallab.failure_library.records import DefectClass, Finding, RunRecord, TaskRef
from evallab.failure_library.store import LibraryStore
from evallab.hackbook.models import HackbookEntry, load_hackbook
from evallab.production.cloud_trace_db import (
    _literal_alternation,
    admitted_cloud_task_ids,
    protected_task_ids,
)
from evallab.production.corpus_queue import CorpusRegistry

KNOWLEDGE_DIR = Path("research/failure-library/knowledge")
KEYS = {
    "pm-tb4-findings": "tb4-audit-v1",
    "envcheck-tb4": "envcheck-tb4-39",
    "tb21-weak-tests": "tb21-weak-tests-v1",
    "tb2-to-tb21-fixes": "tb2-to-tb21-v1",
    "mimo-weak-tests": "mimo-weak-tests-v2.1",
    "mimo-defect-map": "mimo-defect-map-v1",
}
_STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "is",
        "are",
        "for",
        "that",
        "with",
        "this",
        "on",
        "as",
        "by",
        "from",
        "not",
    ]
)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.casefold()) if len(t) > 1 and t not in _STOP]


class BM25:
    """Small deterministic lexical index; no embeddings, network or persisted cache."""

    def __init__(self, documents: Sequence[str]) -> None:
        self.rows = [Counter(tokens(text)) for text in documents]
        self.lengths = [sum(row.values()) for row in self.rows]
        self.average = sum(self.lengths) / max(1, len(self.rows)) or 1.0
        self.df = Counter(term for row in self.rows for term in row)

    def score(self, query: str, position: int) -> float:
        row = self.rows[position]
        score = 0.0
        for term in sorted(set(tokens(query))):
            frequency = row[term]
            if frequency:
                idf = math.log1p((len(self.rows) - self.df[term] + 0.5) / (self.df[term] + 0.5))
                score += (
                    idf
                    * frequency
                    * 2.2
                    / (frequency + 1.2 * (0.25 + 0.75 * self.lengths[position] / self.average))
                )
        return score


@dataclass(frozen=True)
class KnowledgeExample:
    ref: ExampleRef
    task: TaskRef
    defect_classes: tuple[str, ...]
    groups: frozenset[str]
    search_text: str


@dataclass(frozen=True)
class Knowledge:
    examples: tuple[KnowledgeExample, ...]
    cards: tuple[FailureCard, ...]
    entries: tuple[HackbookEntry, ...]
    drafts: tuple[CardDraft, ...]
    digest: str
    report: dict[str, Any]
    units: dict[str, frozenset[str]]
    subjects: tuple[TaskRef, ...]


def ledger_units(root: Path) -> dict[str, frozenset[str]]:
    units: dict[str, set[str]] = defaultdict(set)
    path = root / "research/answer-keys/splits/assignments.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, strict=True):
            name = CorpusRegistry.task_name(row.get("task") or "")
            unit = (row.get("unit") or "").strip().casefold()
            if name and unit:
                units[name].add(unit)
    return {name: frozenset(values) for name, values in units.items()}


def _native_item(finding: Finding) -> str:
    if finding.collection == "envcheck-tb4":
        return "terminal-bench/" + finding.task.task_id
    if finding.collection == "tb21-weak-tests":
        return "tb21-weak-tests-v1:task:" + finding.task.task_id
    if finding.collection == "mimo-weak-tests":
        digest = (finding.task.task_version_digest or "").removeprefix("sha256:")[:12]
        return f"mimo-weak-tests-v2.1:task:{finding.task.task_id}@{digest}"
    return finding.locator


def _identity(record: Finding | RunRecord) -> dict[str, Any]:
    item = _native_item(record) if isinstance(record, Finding) else record.run_id
    identifier = record.finding_id if isinstance(record, Finding) else record.run_id
    return {
        "task_id": f"{identifier}/{record.task.source_id}/{record.task.task_id}",
        "dataset_id": KEYS.get(record.collection, record.collection),
        "split": record.split,
        "item_id": item,
        "item_type": "text",
        "original_grader_digest": record.task.task_version_digest,
        "repaired_grader_digest": None,
    }


def sealed_pattern(values: Iterable[str]) -> re.Pattern[str] | None:
    choices = sorted({v.casefold() for v in values if v.strip()})
    if not choices:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + _literal_alternation(choices) + r")(?![a-z0-9])", re.I)


def _task_text(task: TaskRef, root: Path) -> str:
    # Only declared, in-checkout instruction bytes are read, after DEV admission.
    if not task.task_path:
        return ""
    base = (root / task.task_path).resolve()
    if not base.is_relative_to(root.resolve()):
        return ""
    path = base / "instruction.md"
    if path.is_file() and not path.is_symlink() and path.stat().st_size <= 32_000:
        return path.read_text(encoding="utf-8")
    return ""


def _ref(record: Finding | RunRecord) -> ExampleRef:
    finding = isinstance(record, Finding)
    return ExampleRef(
        id=record.finding_id if finding else record.run_id,
        kind="finding" if finding else "run",
        collection=record.collection,
        source_id=record.task.source_id,
        task_id=record.task.task_id,
        upstream_repo=record.task.upstream_repo,
        upstream_revision=record.task.upstream_revision,
        source_url=record.source_url if finding else record.transcript_ref,
        summary=record.summary
        if finding
        else (
            f"Outcome: {record.outcome}; "
            + "; ".join(
                f"{label.label} [{label.kind}{', ' + label.rs_code if label.rs_code else ''}]"
                for label in record.labels
            )
        ),
        tier=record.tier if finding else None,
        evidence_refs=sorted(
            {
                value
                for evidence in record.evidence
                for value in (
                    evidence.receipt_ref,
                    evidence.reproducer_ref,
                    evidence.url,
                    evidence.fix_revision,
                    evidence.label_source,
                )
                if value
            }
        )
        if finding
        else sorted({record.transcript_ref, *(label.source for label in record.labels)}),
        status_now=record.status_now if finding else "unknown",
        fixed_in_revision=record.fixed_in_revision if finding else None,
        evidence_notes=sorted({e.note for e in record.evidence if e.note}) if finding else [],
    )


def _entry_text(entry: HackbookEntry, draft: CardDraft) -> str:
    return " ".join(
        (
            entry.title,
            entry.mechanism,
            *entry.signatures.structural_signals,
            draft.what_breaks,
            *draft.task_cues,
            *draft.trace_cues,
        )
    )


def compile_cards(
    examples: Sequence[KnowledgeExample],
    entries: Sequence[HackbookEntry],
    drafts: Sequence[CardDraft],
) -> tuple[tuple[FailureCard, ...], dict[str, int]]:
    """Assign each claim to its nearest existing sub-mechanism, within its TS class.

    Sparse sub-mechanisms fold into the class's strongest supported mechanism.
    A class with fewer than three real DEV ids remains explicitly unsupported,
    rather than manufacturing citations or relabeling unknown runs as clean.
    """
    prose = {draft.hackbook_id: draft for draft in drafts}
    candidates = [e for e in entries if e.side == "task" and e.id in prose]
    index = BM25([_entry_text(e, prose[e.id]) for e in candidates])
    grouped: dict[str, list[KnowledgeExample]] = defaultdict(list)
    by_class: dict[str, list[KnowledgeExample]] = defaultdict(list)
    for example in examples:
        for code in example.defect_classes:
            positions = [i for i, e in enumerate(candidates) if e.taxonomy_class_id == code]
            if not positions:
                continue
            position = min(
                positions, key=lambda i: (-index.score(example.search_text, i), candidates[i].id)
            )
            grouped[candidates[position].id].append(example)
            by_class[code].append(example)
    sparse: dict[str, int] = {}
    for code, members in by_class.items():
        members = list({e.ref.id: e for e in members}.values())
        ids = [e.id for e in candidates if e.taxonomy_class_id == code]
        if len(members) < 3:
            sparse[code] = len(members)
            for entry_id in ids:
                grouped.pop(entry_id, None)
            continue
        primary = min(ids, key=lambda key: (-len(grouped[key]), key))
        for entry_id in ids:
            if entry_id != primary and len(grouped[entry_id]) < 3:
                grouped[primary].extend(grouped.pop(entry_id, []))
    totals = Counter(example.ref.collection for example in examples)
    cards: list[FailureCard] = []
    for entry in candidates:
        members = sorted(
            {e.ref.id: e for e in grouped.get(entry.id, [])}.values(), key=lambda e: e.ref.id
        )
        if len(members) < 3:
            continue
        draft = prose[entry.id]
        counts = Counter(e.ref.collection for e in members)
        card = FailureCard(
            card_id="FC-" + entry.id,
            hackbook_entry_id=entry.id,
            defect_class=cast(DefectClass, entry.taxonomy_class_id),
            title=entry.title,
            **draft.model_dump(exclude={"hackbook_id"}),
            examples=[e.ref for e in members[:10]],
            support_count=len(members),
            prevalence_by_collection={
                name: {
                    "count": count,
                    "dev_total": totals[name],
                    "fraction": round(count / totals[name], 6),
                }
                for name, count in sorted(counts.items())
            },
        )
        validate_card(card, {e.ref.id: e.ref for e in examples})
        cards.append(card)
    return tuple(sorted(cards, key=lambda c: c.card_id)), sparse


def validate_card(card: FailureCard, admitted: dict[str, ExampleRef]) -> None:
    """Reject hallucinated, non-DEV, stale or altered citations, including provenance."""
    seen: set[str] = set()
    for ref in card.examples:
        if ref.id in seen or admitted.get(ref.id) != ref:
            raise ValueError(
                f"card {card.card_id}: citation is not a unique admitted DEV id: {ref.id}"
            )
        seen.add(ref.id)


def compile_library(
    root: Path,
    store: LibraryStore,
    *,
    drafts: Sequence[CardDraft] | None = None,
    entries: Sequence[HackbookEntry] | None = None,
) -> Knowledge:
    registry = CorpusRegistry(root)
    if not registry.has_assignment_authority():
        raise ValueError("knowledge requires a validated, nonempty split assignment authority")
    units = ledger_units(root)
    entries = tuple(load_hackbook(root) if entries is None else entries)
    if drafts is None:
        payload = json.loads((root / KNOWLEDGE_DIR / "mechanisms.json").read_text(encoding="utf-8"))
        drafts = [CardDraft.model_validate(row) for row in payload]
    drafts = tuple(drafts)
    draft_ids = [d.hackbook_id for d in drafts]
    if len(set(draft_ids)) != len(draft_ids) or set(draft_ids) - {e.id for e in entries}:
        raise ValueError("drafts must reference unique existing Hackbook ids")
    findings = list(store.iter_findings())
    runs: list[RunRecord] = []
    missing_runs: list[str] = []
    for path in sorted(store.manifest_root.glob("*.manifest.json")):
        name = path.name.removesuffix(".manifest.json")
        if not store.runs_path(name).is_file():
            missing_runs.append(name)
            continue
        problems = store.verify_runs(name)
        if problems:
            raise ValueError("; ".join(problems))
        runs.extend(store.iter_runs(name))
    records: list[Finding | RunRecord] = [*findings, *runs]
    source_rows: dict[str, list[str]] = defaultdict(list)
    for record in records:
        source_rows[record.collection].append(canonical(record.model_dump(mode="json")))
    source_digests = {
        name: "sha256:" + hashlib.sha256(("\n".join(sorted(rows)) + "\n").encode()).hexdigest()
        for name, rows in sorted(source_rows.items())
    }
    identities = [_identity(record) for record in records]
    admitted = admitted_cloud_task_ids(identities, registry=registry)
    seals = sealed_pattern(registry.sealed_identifiers() | protected_task_ids(root))
    classes: dict[str, str] = {}
    examples: dict[str, KnowledgeExample] = {}
    rejected = 0
    for record, identity in zip(records, identities, strict=True):
        # Admission is per-record, not just per task: a DEV sibling cannot admit TEST.
        if (
            record.split != "DEV"
            or identity["task_id"] not in admitted
            or any(
                "cheatbench" in value.casefold() or "hackbench" in value.casefold()
                for value in (record.collection, record.task.source_id)
            )
        ):
            rejected += 1
            continue
        if isinstance(record, RunRecord) and any(
            finding_id not in classes for finding_id in record.finding_ids
        ):
            rejected += 1
            continue
        ref = _ref(record)
        text = canonical(ref.model_dump(mode="json"))
        if seals is not None and seals.search(text):
            rejected += 1
            continue
        codes = (
            (record.defect_class,)
            if isinstance(record, Finding)
            else tuple(sorted({classes[identifier] for identifier in record.finding_ids}))
        )
        name = CorpusRegistry.task_name(record.task.task_id)
        instruction = _task_text(record.task, root)
        if seals is not None and seals.search(instruction):
            instruction = ""
        example = KnowledgeExample(
            ref,
            record.task,
            codes,
            units.get(name, frozenset()),
            " ".join((ref.summary, instruction)),
        )
        if ref.id in examples and examples[ref.id] != example:
            raise ValueError(f"conflicting library ids: {ref.id}")
        examples[ref.id] = example
        if isinstance(record, Finding):
            classes[record.finding_id] = record.defect_class
    # Model prose cannot smuggle sealed subjects into an otherwise safe card.
    if seals is not None and any(seals.search(canonical(d.model_dump())) for d in drafts):
        raise ValueError("card prose contains a protected task identity")
    selected = tuple(examples[key] for key in sorted(examples))
    cards, sparse = compile_cards(selected, entries, drafts)
    material = {
        "examples": [
            {
                "ref": e.ref.model_dump(mode="json"),
                "search_text": e.search_text,
                "classes": e.defect_classes,
            }
            for e in selected
        ],
        "drafts": [d.model_dump() for d in drafts],
        "entries": [e.dump() for e in entries],
        "units": {k: sorted(v) for k, v in sorted(units.items())},
        "ledger": registry.split_assignments_digest,
        "source_collection_digests": source_digests,
    }
    digest = "sha256:" + hashlib.sha256(canonical(material).encode()).hexdigest()
    return Knowledge(
        selected,
        cards,
        entries,
        drafts,
        digest,
        {
            "admitted_dev_examples": len(selected),
            "rejected_examples": rejected,
            "cards": len(cards),
            "insufficient_dev_support": sparse,
            "by_collection": dict(sorted(Counter(e.ref.collection for e in selected).items())),
            "by_tier": dict(sorted(Counter(e.ref.tier or "run_label" for e in selected).items())),
            "by_split": {"DEV": len(selected)},
            "missing_run_collections": missing_runs,
            "split_assignments_digest": registry.split_assignments_digest,
            "source_collection_digests": source_digests,
        },
        units,
        tuple(record.task for record in records),
    )
