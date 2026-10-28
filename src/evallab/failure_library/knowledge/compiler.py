"""Compile DEV evidence into existing Hackbook mechanisms without model calls.

Admission reuses CloudTraceRetriever's structured gate. Leave-subject-out admits
an analogy only when both subjects have distinct authoritative split-ledger
units; a different publisher is not proof of task-family independence.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from itertools import batched
from pathlib import Path
from typing import Any, cast

from evallab.failure_library.knowledge.models import (
    CardDraft,
    ExampleRef,
    FailureCard,
    TaxonomyGuidance,
)
from evallab.failure_library.records import DefectClass, Finding, RunCode, RunRecord, TaskRef
from evallab.failure_library.store import LibraryStore, RunManifest
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
        cached: dict[str, Counter[str]] = {}
        self.rows: list[Counter[str]] = []
        for text in documents:
            row = cached.get(text)
            if row is None:
                row = Counter(tokens(text))
                cached[text] = row
            self.rows.append(row)
        self.lengths = [sum(row.values()) for row in self.rows]
        self.average = sum(self.lengths) / max(1, len(self.rows)) or 1.0
        self.df = Counter(term for row in self.rows for term in row)

    def score_terms(self, terms: Sequence[str], position: int) -> float:
        """Score a prepared, sorted set of query terms without repeated tokenization."""
        row = self.rows[position]
        score = 0.0
        for term in terms:
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
    curated: tuple[TaxonomyGuidance, ...] = ()


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
    """Assign each claim to its nearest existing sub-mechanism, within its TS/RS class.

    Sparse sub-mechanisms fold into the class's strongest supported mechanism.
    A class with fewer than three real DEV ids remains explicitly unsupported,
    rather than manufacturing citations or relabeling unknown runs as clean.
    """
    prose = {draft.hackbook_id: draft for draft in drafts}
    candidates = [e for e in entries if e.id in prose]
    index = BM25([_entry_text(e, prose[e.id]) for e in candidates])
    positions_by_class: dict[str, list[int]] = defaultdict(list)
    for position, entry in enumerate(candidates):
        positions_by_class[entry.taxonomy_class_id].append(position)
    grouped: dict[str, list[KnowledgeExample]] = defaultdict(list)
    by_class: dict[str, list[KnowledgeExample]] = defaultdict(list)
    for example in examples:
        if not example.defect_classes:
            continue
        terms = tuple(sorted(set(tokens(example.search_text))))
        for code in example.defect_classes:
            positions = positions_by_class.get(code, [])
            if not positions:
                continue
            position = min(
                positions, key=lambda i: (-index.score_terms(terms, i), candidates[i].id)
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
            taxonomy_class_id=entry.taxonomy_class_id,
            defect_class=cast(DefectClass, entry.taxonomy_class_id)
            if entry.side == "task"
            else "unclassified",
            run_code=cast(RunCode, entry.taxonomy_class_id) if entry.side == "run" else None,
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


def _stream_records(
    path: Path,
    model: type[Finding] | type[RunRecord],
    digests: dict[str, str],
    source: str,
    expected: str | None = None,
) -> Iterator[Finding | RunRecord]:
    """Hash the exact bytes being parsed, without materializing a large collection.

    The final hash check completes before compilation can return or publish a pack.
    Reading and hashing the same open file also avoids a verify-then-reopen race.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for line in handle:
            digest.update(line)
            if line.strip():
                yield model.model_validate_json(line)
    actual = digest.hexdigest()
    if expected is not None and actual != expected:
        raise ValueError(f"{source}: sha256 {actual} differs from manifest {expected}")
    digests[source] = "sha256:" + actual


def _source_records(
    store: LibraryStore, digests: dict[str, str], missing_runs: list[str]
) -> Iterator[Finding | RunRecord]:
    # Findings precede runs, so every finding join has already passed admission.
    for name in store.collections("findings"):
        path = store.findings_path(name)
        yield from _stream_records(path, Finding, digests, f"findings/{path.name}")
    for path in sorted(store.manifest_root.glob("*.manifest.json")):
        payload = path.read_bytes()
        manifest = RunManifest.model_validate_json(payload)
        name = path.name.removesuffix(".manifest.json")
        digests[f"manifests/{path.name}"] = "sha256:" + hashlib.sha256(payload).hexdigest()
        runs_path = store.runs_path(name)
        if not runs_path.is_file():
            missing_runs.append(name)
            continue
        yield from _stream_records(
            runs_path, RunRecord, digests, f"runs/{runs_path.name}", manifest.sha256
        )


def _admitted_records(
    records: Iterable[Finding | RunRecord], registry: CorpusRegistry
) -> Iterator[tuple[Finding | RunRecord, bool]]:
    """Bound transient models and structured-gate inputs independently of corpus size."""
    for batch in batched(records, 512):
        identities = [_identity(record) for record in batch]
        admitted = admitted_cloud_task_ids(identities, registry=registry)
        for record, identity in zip(batch, identities, strict=True):
            yield record, identity["task_id"] in admitted


def _same_task_version(left: TaskRef, right: TaskRef) -> bool:
    """Related task names are not proof that a defect applies to the run's bytes."""
    if left.task_version_digest and right.task_version_digest:
        return left.task_version_digest == right.task_version_digest
    return left.upstream_revision is not None and all(
        getattr(left, field) == getattr(right, field)
        for field in ("source_id", "task_id", "upstream_repo", "upstream_revision")
    )


def _taxonomy(root: Path) -> tuple[tuple[TaxonomyGuidance, ...], str]:
    raw = (root / "research/hackbook/taxonomy.json").read_bytes()
    payload = json.loads(raw)
    guidance = tuple(
        TaxonomyGuidance(
            code=row["id"],
            title=row["name"],
            definition=row["definition"],
            repair_location=row.get("fix_location", ""),
        )
        for row in [*payload["task_side"], *payload["run_side"]]
    )
    return guidance, "sha256:" + hashlib.sha256(raw).hexdigest()


def compile_curated(root: Path) -> Knowledge:
    """Use only pre-existing taxonomy fields, never example-conditioned card drafts."""
    guidance, taxonomy_sha = _taxonomy(root)
    metadata = {
        "scope": "pre-existing taxonomy only; no source examples or card drafts",
        "guidance": [guide.model_dump() for guide in guidance],
    }
    digest = "sha256:" + hashlib.sha256(canonical(metadata).encode()).hexdigest()
    return Knowledge(
        (),
        (),
        (),
        (),
        digest,
        {
            "admitted_dev_examples": 0,
            "cards": 0,
            "curated_mechanisms": len(guidance),
            "source_records_read": 0,
            "retrieval_scope": "curated_only",
            "taxonomy_sha256": taxonomy_sha,
            "taxonomy_projection": ["id", "name", "definition", "fix_location"],
        },
        {},
        (),
        guidance,
    )


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
    missing_runs: list[str] = []
    source_digests: dict[str, str] = {}
    records = _source_records(store, source_digests, missing_runs)
    seals = sealed_pattern(registry.sealed_identifiers() | protected_task_ids(root))
    classes: dict[str, tuple[str, TaskRef]] = {}
    examples: dict[str, KnowledgeExample] = {}
    subjects: dict[str, TaskRef] = {}
    rejected = 0
    unbound_links = 0
    for record, is_admitted in _admitted_records(records, registry):
        subject_key = canonical(record.task.model_dump(mode="json"))
        subject = subjects.setdefault(subject_key, record.task)
        # Admission is per-record, not just per task: a DEV sibling cannot admit TEST.
        if (
            record.split != "DEV"
            or not is_admitted
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
        if isinstance(record, Finding):
            codes = (record.defect_class,)
        else:
            linked_codes: set[str] = set()
            for identifier in record.finding_ids:
                code, finding_task = classes[identifier]
                if _same_task_version(record.task, finding_task):
                    linked_codes.add(code)
                else:
                    unbound_links += 1
            codes = tuple(
                sorted(linked_codes | {label.rs_code for label in record.labels if label.rs_code})
            )
        name = CorpusRegistry.task_name(record.task.task_id)
        instruction = _task_text(record.task, root)
        if seals is not None and seals.search(instruction):
            instruction = ""
        example = KnowledgeExample(
            ref,
            subject,
            codes,
            units.get(name, frozenset()),
            " ".join((ref.summary, instruction)),
        )
        if ref.id in examples and examples[ref.id] != example:
            raise ValueError(f"conflicting library ids: {ref.id}")
        examples[ref.id] = example
        if isinstance(record, Finding):
            classes[record.finding_id] = (record.defect_class, record.task)
    # Model prose cannot smuggle sealed subjects into an otherwise safe card.
    if seals is not None and any(seals.search(canonical(d.model_dump())) for d in drafts):
        raise ValueError("card prose contains a protected task identity")
    selected = tuple(examples[key] for key in sorted(examples))
    cards, sparse = compile_cards(selected, entries, drafts)
    curated, taxonomy_sha = _taxonomy(root)
    material = {
        "digest_format": "failure-knowledge-content/v2",
        "drafts": [d.model_dump() for d in drafts],
        "entries": [e.dump() for e in entries],
        "units": {k: sorted(v) for k, v in sorted(units.items())},
        "ledger": registry.split_assignments_digest,
        "source_collection_digests": source_digests,
        "curated_taxonomy": [guide.model_dump() for guide in curated],
    }
    content_digest = hashlib.sha256(canonical(material).encode())
    for example in selected:
        content_digest.update(b"\n")
        content_digest.update(
            canonical(
                {
                    "ref": example.ref.model_dump(mode="json"),
                    "search_text": example.search_text,
                    "classes": example.defect_classes,
                }
            ).encode()
        )
    digest = "sha256:" + content_digest.hexdigest()
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
            "unbound_finding_links": unbound_links,
            "split_assignments_digest": registry.split_assignments_digest,
            "source_collection_digests": source_digests,
            "source_digest_format": "sha256-file-bytes",
            "taxonomy_sha256": taxonomy_sha,
        },
        units,
        tuple(subjects[key] for key in sorted(subjects)),
        curated,
    )
