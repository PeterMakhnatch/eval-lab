"""DEV-only, unadjudicated trace hard cases and a frozen offline review sample.

Run ``python -m evallab.failure_library.bank.hard_cases --out NEW_DIRECTORY``.
The inputs are the current FW-2 matrix and the three FW-3 first-pass files;
no detector, model, database writer, or replay is invoked. Source labels and
first-pass explanations remain claims, never fresh adjudications. Case identity
is (system, canonical subject, case kind, bank kind), not a publisher row.

Only sampled, admitted actor contexts are exported, through Run Atlas's existing
blinding/rendering projection. Its bounded observations and final-parent-path
scope are not the complete native graph (QUIRKS Q25/Q26). Missing cited original
steps block a runnable export instead of silently substituting another sample.
All output is private local evidence; use a fresh directory to freeze a sample.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from evallab.failure_library.bank import (
    BankContext,
    IssueBank,
    IssueItem,
    Kind,
    LabelType,
    Polarity,
)
from evallab.failure_library.bank.matrix import GENERIC_SELECTORS, Identity, is_circular
from evallab.failure_library.bank.sources.library_runs import goodfire_trace_id
from evallab.failure_library.store import _atomic_write, _line
from evallab.schemas import ContractModel

if TYPE_CHECKING:
    from evallab.production.run_atlas import AtlasRun

SCHEMA_VERSION = "issue-hard-case/v1"
SAMPLE_SEED = "fw3-opus-adjudication/v1"
ANNOTATION_NAMES = (
    "kimi-goodfire-miss-even.jsonl",
    "kimi-goodfire-miss-odd.jsonl",
    "kimi-cheatbench-miss-and-fa.jsonl",
)
System = Literal["trace-watcher", "task-qa", "seeker-data-quality"]
CaseKind = Literal["miss", "false_alarm", "label_disagreement", "infra", "measurement_missing"]
BankId = Annotated[str, Field(pattern=r"^ib-[0-9a-f]{16}$")]
StepId = Annotated[int, Field(strict=True, ge=0)]


def make_case_id(system: str, subject_id: str, case_kind: str, kind: str) -> str:
    material = _line({"system": system, "subject_id": subject_id, "case_kind": case_kind, "kind": kind})
    return "hc-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


class HardCase(ContractModel):
    """One source-backed DEV lead; ``input_ref=None`` means it is not runnable."""

    schema_version: Literal["issue-hard-case/v1"] = SCHEMA_VERSION
    case_id: str = Field(pattern=r"^hc-[0-9a-f]{24}$")
    system: System
    item_ids: list[BankId] = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    task_id: str | None = None
    split: Literal["DEV"] = "DEV"
    case_kind: CaseKind
    kind: Kind
    polarity: Polarity
    label_type: LabelType
    explanation: str = Field(min_length=1)
    evidence_steps: list[StepId] = Field(default_factory=list)
    input_ref: str | None = None
    input_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_refs: list[str] = Field(min_length=1)
    source_digests: dict[str, str] = Field(default_factory=dict)
    adjudication: Literal["pending", "confirmed", "refuted", "uncertain"] = "pending"
    adjudicator_model: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _derive_id(cls, value: Any) -> Any:
        keys = ("system", "subject_id", "case_kind", "kind")
        if isinstance(value, Mapping) and "case_id" not in value and all(k in value for k in keys):
            return {**value, "case_id": make_case_id(*(value[k] for k in keys))}
        return value

    @field_validator("explanation")
    @classmethod
    def _word_limit(cls, value: str) -> str:
        if not value.strip() or len(value.split()) > 120:
            raise ValueError("explanation must contain 1 to 120 words")
        return value

    @field_validator("item_ids", "evidence_steps", "source_refs")
    @classmethod
    def _distinct(cls, value: list[Any]) -> list[Any]:
        if any(isinstance(v, str) and not v.strip() for v in value):
            raise ValueError("references must not be blank")
        return sorted(set(value))

    @field_validator("source_digests")
    @classmethod
    def _digests(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not ref.strip() or not re.fullmatch(r"[0-9a-f]{64}", digest) for ref, digest in value.items()):
            raise ValueError("source_digests must map nonempty refs to SHA-256 hex digests")
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def _bound_identity(self) -> HardCase:
        expected = make_case_id(self.system, self.subject_id, self.case_kind, self.kind)
        if self.case_id != expected:
            raise ValueError(f"case_id is not deterministic (want {expected})")
        if (self.input_ref is None) != (self.input_sha256 is None):
            raise ValueError("input_ref and input_sha256 must be supplied together")
        if self.input_ref is not None and not self.input_ref.strip():
            raise ValueError("input_ref must not be blank")
        if self.system == "seeker-data-quality" and self.case_kind not in {"infra", "measurement_missing"}:
            raise ValueError("Seeker cases cover infrastructure or missing measurement only")
        return self


def load_cases(paths: Sequence[Path]) -> list[HardCase]:
    """Load deterministic JSONL collections, refusing conflicting copies of a case."""
    found: dict[str, HardCase] = {}
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                case = HardCase.model_validate_json(line)
                previous = found.get(case.case_id)
                if previous is not None and previous != case:
                    raise ValueError(f"{path}:{number}: conflicting case {case.case_id}")
                found[case.case_id] = case
    return [found[key] for key in sorted(found)]


def write_cases(path: Path, cases: Iterable[HardCase]) -> None:
    """Atomically write a sorted collection; do not discard conflicting duplicates."""
    found: dict[str, dict[str, Any]] = {}
    for case in cases:
        payload = HardCase.model_validate(case.model_dump()).model_dump(mode="json")
        previous = found.get(case.case_id)
        if previous is not None and previous != payload:
            raise ValueError(f"conflicting case {case.case_id}")
        found[case.case_id] = payload
    _atomic_write(path, "".join(_line(found[key]) + "\n" for key in sorted(found)))


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _rows(path: Path, digests: dict[str, str]) -> list[tuple[str, dict[str, Any]]]:
    raw = path.read_bytes()
    digests[str(path)] = hashlib.sha256(raw).hexdigest()
    rows = []
    for number, line in enumerate(raw.splitlines(), 1):
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{number}: expected a JSON object")
            rows.append((f"{path}#L{number}", row))
    return rows


def _subject(item: IssueItem) -> str:
    if item.subject_type == "run":
        selector = item.harness_selector
        return goodfire_trace_id(item.subject_id) or (
            selector if selector and selector not in GENERIC_SELECTORS else item.subject_id
        )
    return item.subject_id


def _canonical_subject(subject: str) -> str:
    return goodfire_trace_id(subject) or subject


def _strict_dev(item: IssueItem, ctx: BankContext) -> bool:
    if item.split != "DEV":
        return False
    key_id = None
    original_id = item.subject_id
    if item.provenance.store.startswith("answer-keys:"):
        key_id = item.provenance.store.partition(":")[2]
        original_id = item.provenance.ref
    if ctx.strict_split(
        item.declared_split, key_id=key_id, item_id=original_id, task_id=item.task_id
    ) != "DEV":
        return False
    canonical = _subject(item)
    return canonical == original_id or ctx.strict_split(
        item.declared_split, item_id=canonical, task_id=item.task_id
    ) == "DEV"


def _steps(row: Mapping[str, Any], ref: str) -> set[int]:
    values = row.get("evidence_steps", [])
    if not isinstance(values, list) or any(type(v) is not int or v < 0 for v in values):
        raise ValueError(f"{ref}: evidence_steps must be original nonnegative integer ids")
    return set(values)


@dataclass(frozen=True)
class AnnotationRef:
    case_id: str
    subject_id: str
    task_id: str | None
    corpus: str
    original_case: str
    label_agrees: bool | Literal["unsure"]
    source_ref: str


@dataclass
class Materialization:
    cases: list[HardCase]
    annotations: list[AnnotationRef]
    source_digests: dict[str, str]
    counts: Counter[str]
    excluded: list[dict[str, str]]


@dataclass
class _CaseGroup:
    system: System
    subject_id: str
    case_kind: CaseKind
    kind: Kind
    items: dict[str, IssueItem] = field(default_factory=dict)
    explanations: list[tuple[int, str, str]] = field(default_factory=list)
    steps: set[int] = field(default_factory=set)
    refs: set[str] = field(default_factory=set)
    digests: dict[str, str] = field(default_factory=dict)

    def add(self, item: IssueItem, row: Mapping[str, Any], ref: str, path: Path,
            source_digests: Mapping[str, str], *, explanation: str, priority: int) -> None:
        self.items[item.item_id] = item
        self.explanations.append((priority, ref, explanation))
        self.steps.update(_steps(row, ref))
        bank_ref = f"issue-bank:{item.item_id}"
        provenance_ref = f"{item.provenance.store}#{item.provenance.ref}"
        self.refs.update((ref, bank_ref, provenance_ref))
        incoming = {
            bank_ref: hashlib.sha256(_line(item.model_dump(mode="json")).encode()).hexdigest(),
            str(path): source_digests[str(path)],
        }
        if item.provenance.digest:
            incoming[provenance_ref] = item.provenance.digest.removeprefix("sha256:")
        extra_refs = row.get("source_refs") or []
        if not isinstance(extra_refs, list) or any(not isinstance(v, str) or not v.strip() for v in extra_refs):
            raise ValueError(f"{ref}: source_refs must be nonempty path/URI strings")
        self.refs.update(extra_refs)
        if row.get("source_ref") is not None:
            if not isinstance(row["source_ref"], str) or not row["source_ref"].strip():
                raise ValueError(f"{ref}: invalid source_ref")
            self.refs.add(row["source_ref"])
        extra_digests = row.get("source_digests") or {}
        if not isinstance(extra_digests, dict):
            raise ValueError(f"{ref}: source_digests must be a mapping")
        for source, digest in [*incoming.items(), *extra_digests.items()]:
            if not isinstance(source, str) or not source.strip() or not isinstance(digest, str):
                raise ValueError(f"{ref}: invalid source digest")
            digest = digest.removeprefix("sha256:")
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"{ref}: invalid SHA-256 for {source}")
            previous = self.digests.get(source)
            if previous is not None and previous != digest:
                raise ValueError(f"{ref}: conflicting source digest for {source}")
            self.digests[source] = digest

    def build(self) -> HardCase:
        # The cloud identity supplies the canonical task namespace for duplicate
        # Goodfire library/cloud observations. Other labels remain in provenance.
        items = sorted(self.items.values(), key=lambda i: (i.source != "cloud-traces", i.item_id))
        item = items[0]
        if len({i.polarity for i in items}) != 1:
            raise ValueError(f"{self.subject_id}: conflicting source polarities for {self.case_kind}")
        explanation = min(self.explanations)[2]
        return HardCase.model_validate({
            "system": self.system, "subject_id": self.subject_id, "task_id": item.task_id,
            "case_kind": self.case_kind, "kind": self.kind, "polarity": item.polarity,
            "label_type": item.label_type, "item_ids": list(self.items),
            "explanation": " ".join(explanation.split()[:120]), "evidence_steps": list(self.steps),
            "source_refs": list(self.refs), "source_digests": self.digests,
        })


def _route(item: IssueItem, case_kind: CaseKind, row: Mapping[str, Any]) -> tuple[System, CaseKind]:
    if item.kind == "cheat" and (item.actor_transcript is False or row.get("actor_transcript") is False):
        return "seeker-data-quality", "measurement_missing"
    if item.kind == "infra":
        return "seeker-data-quality", "infra"
    if item.subject_type != "run":
        return "task-qa", case_kind
    return "trace-watcher", case_kind


def materialize_cases(
    items: Iterable[IssueItem], ctx: BankContext, *, matrix_dir: Path,
    annotation_paths: Sequence[Path],
) -> Materialization:
    """Merge source claims, rechecking DEV authority and canonicalizing run aliases."""
    digests: dict[str, str] = {}
    matrix_rows = []
    for name in ("misses.jsonl", "false_alarms.jsonl"):
        path = matrix_dir / name
        matrix_rows.extend((path, ref, row) for ref, row in _rows(path, digests))
    annotation_rows = []
    for path in sorted(annotation_paths):
        annotation_rows.extend((path, ref, row) for ref, row in _rows(path, digests))
    wanted_ids = {row.get("item_id") for _, _, row in matrix_rows}
    wanted_subjects = {
        _canonical_subject(str(row.get("subject_id") or row.get("run_id") or ""))
        for _, _, row in [*matrix_rows, *annotation_rows]
    }
    admitted: dict[str, IssueItem] = {}
    by_subject: dict[str, list[IssueItem]] = defaultdict(list)
    counts: Counter[str] = Counter(matrix_rows=len(matrix_rows), first_pass_rows=len(annotation_rows))
    for item in items:
        if item.item_id not in wanted_ids and _subject(item) not in wanted_subjects:
            continue
        if not _strict_dev(item, ctx):
            counts["bank_items_not_strict_dev"] += 1
            continue
        previous = admitted.get(item.item_id)
        if previous is not None:
            if previous != item:
                raise ValueError(f"conflicting bank item {item.item_id}")
            continue
        admitted[item.item_id] = item
        by_subject[_subject(item)].append(item)
    counts["bank_items_strict_dev"] = len(admitted)
    groups: dict[tuple[str, str, str, str], _CaseGroup] = {}
    annotation_refs: list[AnnotationRef] = []
    excluded: list[dict[str, str]] = []

    def exclude(ref: str, reason: str) -> None:
        counts[f"excluded_{reason}"] += 1
        excluded.append({"source_ref": ref, "reason": reason})

    def add(item: IssueItem, row: Mapping[str, Any], ref: str, path: Path,
            case_kind: CaseKind, explanation: str, priority: int) -> _CaseGroup:
        system, case_kind = _route(item, case_kind, row)
        if case_kind == "measurement_missing":
            explanation = (
                "No actor transcript is recorded by the bank or matrix. Semantic cheat/clean "
                "measurement is missing, not a clean judgment. Source labels remain unadjudicated."
            )
        subject = _subject(item)
        key = (system, subject, case_kind, item.kind)
        group = groups.setdefault(key, _CaseGroup(system, subject, case_kind, item.kind))
        group.add(item, row, ref, path, digests, explanation=explanation, priority=priority)
        return group

    for path, ref, row in matrix_rows:
        if row.get("cell") not in {"miss", "false_alarm"}:
            exclude(ref, "not_a_judged_hard_case")  # not_run is never clear
            continue
        item = admitted.get(row.get("item_id"))
        if item is None:
            exclude(ref, "item_not_strict_dev_or_missing")
            continue
        detector = str(row.get("detector") or "")
        if not detector:
            raise ValueError(f"{ref}: missing detector identity")
        if row.get("circular") or is_circular(item, Identity(detector.split("@", 1)[0], "")):
            exclude(ref, "circular_detector_label")
            continue
        if row.get("members"):
            exclude(ref, "union_row")
            continue
        expected_polarity = "positive" if row["cell"] == "miss" else "negative"
        if row.get("kind", item.kind) != item.kind or item.polarity != expected_polarity:
            raise ValueError(f"{ref}: matrix kind/polarity does not match the bank item")
        if row.get("subject_id") and _canonical_subject(row["subject_id"]) != _subject(item):
            raise ValueError(f"{ref}: matrix subject does not match the bank item")
        explanation = f"Unadjudicated matrix {row['cell']} for {detector}: {row.get('summary') or row.get('reason') or 'no explanation retained'}"
        add(item, row, ref, path, row["cell"], explanation, 1)

    for path, ref, row in annotation_rows:
        original_case = row.get("case")
        agrees = row.get("label_agrees")
        if original_case not in {"miss", "false_alarm"} or not (type(agrees) is bool or agrees == "unsure"):
            raise ValueError(f"{ref}: expected a first-pass miss/false_alarm with label_agrees")
        subject = _canonical_subject(str(row.get("run_id") or ""))
        expected_polarity = "positive" if original_case == "miss" else "negative"
        matches = [i for i in by_subject.get(subject, []) if i.kind == "cheat" and i.polarity == expected_polarity]
        if not matches:
            exclude(ref, "annotation_subject_not_strict_dev_or_label_mismatch")
            continue
        explanation = row.get("explanation")
        if not isinstance(explanation, str) or not explanation.strip():
            raise ValueError(f"{ref}: missing first-pass explanation")
        case_kind: CaseKind = "label_disagreement" if agrees is False or agrees == "unsure" else original_case
        for item in matches:
            if any(is_circular(item, Identity(str(name).split("@", 1)[0], ""))
                   for name in (row.get("detectors") or {})):
                exclude(ref, "circular_detector_label")
                continue
            group = add(item, row, ref, path, case_kind, explanation, 0)
            if group.system == "trace-watcher":
                annotation_refs.append(AnnotationRef(
                    make_case_id(group.system, group.subject_id, group.case_kind, group.kind),
                    subject, item.task_id, str(row.get("corpus") or "unknown"), original_case, agrees, ref,
                ))
    cases = sorted((group.build() for group in groups.values()), key=lambda c: c.case_id)
    counts["cases"] = len(cases)
    return Materialization(cases, annotation_refs, dict(sorted(digests.items())), counts, excluded)


def select_adjudication_sample(result: Materialization, *, seed: str = SAMPLE_SEED) -> list[dict[str, Any]]:
    """Freeze at most 24 first-pass trace subjects, never duplicate source rows.

    Disagreements (up to 16), false alarms (up to 7 including overlaps), then
    corpus/kind-stratified hash-ranked misses. Prefer tasks not already selected.
    """
    cases = {case.case_id: case for case in result.cases}
    grouped: dict[str, list[AnnotationRef]] = defaultdict(list)
    for annotation in result.annotations:
        grouped[annotation.subject_id].append(annotation)
    candidates: list[AnnotationRef] = []
    for subject_annotations in grouped.values():
        candidates.append(min(subject_annotations, key=lambda a: (
            a.label_agrees is not False and a.label_agrees != "unsure",
            a.original_case != "false_alarm", a.case_id, a.source_ref,
        )))
    selected: dict[str, dict[str, Any]] = {}
    tasks: set[str] = set()
    strata: Counter[tuple[str, str]] = Counter()

    def take(pool: Sequence[AnnotationRef], limit: int, reason: str) -> None:
        remaining = [a for a in pool if a.subject_id not in selected]
        for _ in range(min(limit, len(remaining))):
            def rank(a: AnnotationRef) -> tuple[bool, int, str]:
                case = cases[a.case_id]
                task = case.task_id or case.subject_id
                digest = hashlib.sha256(f"{seed}\x1f{a.case_id}".encode()).hexdigest()
                return task in tasks, strata[(a.corpus, case.kind)], digest
            annotation = min(remaining, key=rank)
            remaining.remove(annotation)
            case = cases[annotation.case_id]
            reasons = [reason]
            if annotation.original_case == "false_alarm" and reason != "first_pass_false_alarm":
                reasons.append("first_pass_false_alarm")
            selected[annotation.subject_id] = {
                "case_id": case.case_id, "case_ref": f"{case.system}.jsonl#{case.case_id}",
                "reasons": reasons, "first_pass_label_agrees": annotation.label_agrees,
                "first_pass_refs": sorted({a.source_ref for a in grouped[annotation.subject_id]}),
                "original_case": annotation.original_case,
            }
            tasks.add(case.task_id or case.subject_id)
            strata[(annotation.corpus, case.kind)] += 1

    take([a for a in candidates if a.label_agrees is False or a.label_agrees == "unsure"], 16,
         "first_pass_label_false_or_unsure")
    false_alarms = sum(row["original_case"] == "false_alarm" for row in selected.values())
    take([a for a in candidates if a.original_case == "false_alarm"], max(0, 7 - false_alarms),
         "first_pass_false_alarm")
    take([a for a in candidates if a.original_case == "miss"], 24 - len(selected),
         "hash_stratified_miss")
    return list(selected.values())


def _load_original_runs(
    subject_ids: Sequence[str], ctx: BankContext, results_root: Path | None,
) -> tuple[dict[str, AtlasRun], dict[str, str]]:
    """Resolve just the frozen subjects, with one admission pass per source."""
    from evallab.production.run_atlas import CHEATBENCH_DATASET, cheatbench_runs, cloud_runs
    from evallab.results_home import results_root as default_results_root

    cheat = sorted({subject for subject in subject_ids if subject.startswith(CHEATBENCH_DATASET + ":")})
    cloud = sorted(set(subject_ids) - set(cheat))
    loaders: list[tuple[list[str], Iterator[AtlasRun]]] = []
    if cloud:
        loaders.append((cloud, cloud_runs(ctx.cloud_db, registry=ctx.registry, trace_ids=cloud)))
    if cheat:
        loaders.append((cheat, cheatbench_runs(
            ctx.registry, results_root or default_results_root(), item_ids=cheat,
        )))
    found: dict[str, AtlasRun] = {}
    unavailable: dict[str, str] = {}
    for subjects, runs in loaders:
        try:
            for run in runs:
                if run.run_id not in subjects:
                    raise ValueError("original loader returned an unrequested subject")
                if run.run_id in found:
                    del found[run.run_id]
                    raise ValueError("original loader returned a duplicate subject")
                found[run.run_id] = run
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            unavailable.update({subject: str(error) for subject in subjects if subject not in found})
    for subject in subject_ids:
        if subject not in found:
            unavailable.setdefault(subject, "original run unavailable or no longer DEV-admitted")
    return found, unavailable


def _folded_feedback_origins(ctx: BankContext, run: AtlasRun, origins: Sequence[Any]) -> dict[int, int]:
    """Map native tool-result ids to the rendered agent step that owns that feedback.

    This mirrors ``run_atlas.cloud_doc`` ownership, not a nearest-step guess. Nodes
    off the final parent path remain unavailable. No label/annotation is projected.
    """
    if not ctx.cloud_db.is_file():
        return {}
    from evallab.production.run_atlas import connect_readonly

    by_origin = {origin: index for index, origin in enumerate(origins)}
    agents = {
        step["step_id"]: step
        for step in run.doc.get("steps", [])
        if step.get("source") == "agent"
    }
    owners: dict[str, int] = {}
    last_agent: int | None = None
    aliases: dict[int, int] = {}
    connection = connect_readonly(ctx.cloud_db)
    try:
        rows = connection.execute(
            "SELECT step_index, role, flags_json FROM trace_steps"
            " WHERE trace_id = ? ORDER BY step_index",
            (run.run_id,),
        )
        for row in rows:
            try:
                flags = json.loads(row["flags_json"] or "{}")
            except ValueError:
                flags = {}
            if not isinstance(flags, Mapping):
                flags = {}
            if flags.get("on_final_parent_path") is False:
                continue
            original = row["step_index"]
            if row["role"] in ("assistant", "agent") and original in agents:
                last_agent = original
                for call in agents[original].get("tool_calls") or []:
                    call_id = call.get("tool_call_id")
                    if call_id:
                        owners[str(call_id)] = original
            elif row["role"] == "tool":
                call_id = flags.get("tool_call_id")
                owner = owners.get(str(call_id)) if call_id else None
                owner = owner if owner is not None else last_agent
                if owner in by_origin:
                    aliases[original] = by_origin[owner]
    finally:
        connection.close()
    return aliases


def export_sample(
    cases: Sequence[HardCase], sample: list[dict[str, Any]], ctx: BankContext,
    out: Path, *, results_root: Path | None = None,
) -> tuple[list[HardCase], dict[str, Any]]:
    """Export only sampled actor inputs; private original-step maps stay outside inputs/."""
    from evallab.production.run_atlas import blind_run, render_segments, source_pattern
    from evallab.production.trace_harness import actor_inputs_available

    by_id = {case.case_id: case for case in cases}
    originals, unavailable = _load_original_runs(
        [by_id[entry["case_id"]].subject_id for entry in sample], ctx, results_root,
    )
    private_map: dict[str, Any] = {}
    for entry in sample:
        case = by_id[entry["case_id"]]
        entry.update(input_ref=None, input_sha256=None, runnable=False)
        try:
            run = originals.get(case.subject_id)
            if run is None:
                raise ValueError(unavailable[case.subject_id])
            if run.split != "DEV":
                raise ValueError("original run is not DEV")
            blind = blind_run(run)
            if not actor_inputs_available(blind.steps):
                raise ValueError("original loader retained no actor input")
            mapping = {str(step["step_id"]): origin for step, origin in zip(blind.steps, blind.origins, strict=True)}
            feedback = _folded_feedback_origins(ctx, run, blind.origins)
            by_origin = {origin: index for index, origin in enumerate(blind.origins)}
            missing = sorted(set(case.evidence_steps) - set(by_origin) - set(feedback))
            private_map[case.case_id] = {
                "subject_id": case.subject_id, "original_step_ids": mapping,
                "evidence_steps_original": case.evidence_steps, "unmapped_evidence_steps": missing,
                "feedback_step_aliases": {str(original): model for original, model in feedback.items()},
                "evidence_steps_model": sorted({
                    by_origin[original] if original in by_origin else feedback[original]
                    for original in case.evidence_steps if original not in missing
                }),
                "original_document_sha256": hashlib.sha256(_line(dict(run.doc)).encode()).hexdigest(),
            }
            if missing:
                raise ValueError(f"original evidence steps absent from Run Atlas projection: {missing}")
            pattern = source_pattern((run.source, run.run_id))
            text = "\n\n".join(segment.text for segment in render_segments(blind, pattern=pattern)) + "\n"
            if pattern.search(text):
                raise ValueError("source identity survived blinding")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            opaque = hashlib.sha256(case.case_id.encode()).hexdigest()[:32]
            reference = f"inputs/{opaque}.txt"
            _atomic_write(out / reference, text)
            by_id[case.case_id] = HardCase.model_validate({
                **case.model_dump(), "input_ref": reference, "input_sha256": digest,
            })
            entry.update(input_ref=reference, input_sha256=digest, runnable=True)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            entry["blocked_reason"] = str(error)
    return [by_id[key] for key in sorted(by_id)], private_map


def materialize_to_directory(
    ctx: BankContext, out: Path, *, matrix_dir: Path, annotation_paths: Sequence[Path],
    results_root: Path | None = None, seed: str = SAMPLE_SEED,
) -> dict[str, Any]:
    """Read existing stores only and freeze a complete private materialization."""
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError(f"output must be a fresh or empty directory; refusing to alter {out}")
    bank = IssueBank(ctx.repo_root, state_root=ctx.state_root)
    result = materialize_cases(bank.iter_items(), ctx, matrix_dir=matrix_dir, annotation_paths=annotation_paths)
    sample = select_adjudication_sample(result, seed=seed)
    out.mkdir(parents=True, exist_ok=True)
    cases, private_map = export_sample(result.cases, sample, ctx, out, results_root=results_root)
    outputs: dict[str, str] = {}
    systems = {"trace-watcher", "seeker-data-quality"} | {case.system for case in cases}
    for system in sorted(systems):
        path = out / f"{system}.jsonl"
        write_cases(path, (case for case in cases if case.system == system))
        outputs[path.name] = _sha256(path)
    _atomic_write(out / "input-map.json", json.dumps(private_map, indent=2, sort_keys=True) + "\n")
    plan = {
        "schema_version": "issue-hard-case-adjudication-plan/v1", "seed": seed,
        "adjudicator_family": "Opus", "model_invoked": False,
        "selection": "first-pass subjects: disagreement<=16, false_alarm<=7, hash-stratified misses to 24; distinct tasks first",
        "source_digests": result.source_digests, "case_files": outputs,
        "input_map_sha256": _sha256(out / "input-map.json"),
        "context_projection": "Run Atlas blind_run/render_segments; final parent path; bounded non-agent text",
        "sample": sample, "shortfall": max(0, 24 - len(sample)),
    }
    _atomic_write(out / "adjudication-plan.json", json.dumps(plan, indent=2, sort_keys=True) + "\n")
    by_id = {case.case_id: case for case in cases}
    summary = {
        "schema_version": SCHEMA_VERSION, "counts": dict(sorted(result.counts.items())),
        "by_system": dict(sorted(Counter(case.system for case in cases).items())),
        "by_case_kind": dict(sorted(Counter(case.case_kind for case in cases).items())),
        "distinct_subjects": len({case.subject_id for case in cases}),
        "distinct_bank_items": len({item_id for case in cases for item_id in case.item_ids}),
        "sample_cases": len(sample), "sample_tasks": len({
            by_id[row["case_id"]].task_id or by_id[row["case_id"]].subject_id
            for row in sample
        }),
        "sample_shortfall": max(0, 24 - len(sample)),
        "runnable_sample_cases": sum(row["runnable"] for row in sample),
        "source_ref_only_cases": sum(case.input_ref is None for case in cases),
        "adjudication": {"pending": len(cases)}, "source_digests": result.source_digests,
        "case_files": outputs, "adjudication_plan_sha256": _sha256(out / "adjudication-plan.json"),
        "excluded": result.excluded,
        "blocked_sample": [{"case_id": row["case_id"], "reason": row["blocked_reason"]}
                           for row in sample if not row["runnable"]],
    }
    _atomic_write(out / "summary.json", json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="Fresh private local output directory")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--state-root", type=Path, help="Existing failure-library state (read only)")
    parser.add_argument("--matrix", type=Path, help="Latest matrix artifact directory (default: fw2/matrix)")
    parser.add_argument("--annotations", type=Path, action="append", help="First-pass JSONL; repeat to merge")
    parser.add_argument("--results-root", type=Path, help="Existing retained Harbor results (read only)")
    parser.add_argument("--seed", default=SAMPLE_SEED)
    args = parser.parse_args(argv)
    ctx = BankContext(args.repo_root.resolve())
    if args.state_root is not None:
        ctx.state_root = args.state_root.expanduser().resolve()
    flywheel = ctx.prod_live / "data-flywheel"
    annotation_paths = args.annotations if args.annotations is not None else [flywheel / "fw3" / name for name in ANNOTATION_NAMES]
    try:
        summary = materialize_to_directory(
            ctx, args.out.expanduser().resolve(), matrix_dir=(args.matrix or flywheel / "fw2/matrix").resolve(),
            annotation_paths=[path.expanduser().resolve() for path in annotation_paths],
            results_root=args.results_root, seed=args.seed,
        )
    except (OSError, ValueError) as error:
        parser.exit(2, f"hard-case materialization refused: {error}\n")
    print(json.dumps({key: summary[key] for key in (
        "counts", "by_system", "by_case_kind", "distinct_subjects", "distinct_bank_items",
        "sample_cases", "sample_tasks", "sample_shortfall", "runnable_sample_cases", "blocked_sample",
    )}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
