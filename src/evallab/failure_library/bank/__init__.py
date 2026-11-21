from __future__ import annotations

import fcntl
import hashlib
import importlib
import json
import os
import pkgutil
import re
import tempfile
import threading
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Literal

from pydantic import Field, model_validator

from evallab.failure_library.records import Split
from evallab.failure_library.sources import normalize_split
from evallab.failure_library.store import _atomic_write, _line, default_state_root
from evallab.schemas import ContractModel
from evallab.trajectory_ir import SourceEventRef

if TYPE_CHECKING:
    from evallab.production.corpus_queue import CorpusRegistry

SCHEMA_VERSION: Literal["issue-bank/v1"] = "issue-bank/v1"
MANIFEST_PATH = Path("research/failure-library/bank/manifest.json")

Kind = Literal[
    "cheat",
    "grader_wrong_pass",
    "grader_unfair_fail",
    "task_defect",
    "grader_repair",
    "infra",
    "loop",
    "runaway",
    "latency",
]
Polarity = Literal["positive", "negative"]
LabelType = Literal[
    "executed",
    "human",
    "judge",
    "consensus",
    "construction",
    "upstream_fix",
    "static",
    "detector",
]
Granularity = Literal[
    "run", "step", "passage", "command", "segment", "task", "test", "repair_pair"
]
SubjectType = Literal["run", "task", "repair"]

_SLUG = r"^[a-z0-9][a-z0-9._-]*$"
_BARE_NUMBER = re.compile(r"^\d+$")
STATE_SUBDIR = "bank"
CLOUD_DB_ENV = "EVALLAB_CLOUD_TRACE_DB"


_THREAD_LOCK = threading.RLock()
_HELD_LOCKS: dict[Path, tuple[BinaryIO, int]] = {}


@contextmanager
def bank_writer_lock(bank: IssueBank) -> Iterator[None]:
    lock_root = Path(tempfile.gettempdir()) / f"evallab-issue-bank-locks-{os.getuid()}"
    keys = sorted(
        {str(bank.manifest_path.resolve()), str(bank.items_root.resolve())}
    )
    with _THREAD_LOCK:
        lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        acquired: list[Path] = []
        try:
            for key in keys:
                path = lock_root / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
                held = _HELD_LOCKS.get(path)
                if held is not None:
                    _HELD_LOCKS[path] = (held[0], held[1] + 1)
                else:
                    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
                    handle = os.fdopen(descriptor, "a+b")
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    except BaseException:
                        handle.close()
                        raise
                    _HELD_LOCKS[path] = (handle, 1)
                acquired.append(path)
            yield
        finally:
            for path in reversed(acquired):
                handle, depth = _HELD_LOCKS[path]
                if depth > 1:
                    _HELD_LOCKS[path] = (handle, depth - 1)
                else:
                    del _HELD_LOCKS[path]
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        handle.close()


def make_item_id(source: str, subject_id: str, kind: str, locator: str = "") -> str:
    material = f"{source}\x1f{subject_id}\x1f{kind}\x1f{locator}"
    return "ib-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


class Provenance(ContractModel):

    store: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    digest: str | None = None
    note: str | None = Field(default=None, max_length=300)
    who: str | None = None
    how: str | None = None
    version: str | None = None


class ExposureEvent(ContractModel):

    consumer: str
    purpose: str
    at: str | None = None
    artifact_sha256: str | None = None


class IssueItem(ContractModel):

    schema_version: Literal["issue-bank/v1"] = SCHEMA_VERSION
    item_id: str = Field(pattern=r"^ib-[0-9a-f]{16}$")
    source: str = Field(pattern=_SLUG)
    kind: Kind
    polarity: Polarity | None
    label_type: LabelType
    label: str = Field(min_length=1, max_length=200)
    granularity: Granularity
    subject_type: SubjectType
    subject_id: str = Field(min_length=1)
    task_id: str | None = None
    locator: str = ""
    positive_units: int | None = Field(default=None, ge=0)
    declared_split: str = Field(min_length=1)
    split: Split
    license: str | None = None
    actor_transcript: bool | None = None
    harness_selector: str | None = None
    provenance: Provenance
    label_state: Literal["executed-positive", "reviewed-control", "attempted", "unjudged"] | None = None
    protocol: Literal["action-v1", "action-v2"] | None = None
    mechanism: str | None = None
    step_locators: tuple[SourceEventRef, ...] = ()
    task_ref: str | None = None
    task_digest: str | None = None
    grader_ref: str | None = None
    grader_digest: str | None = None
    task_available: bool | None = None
    grader_available: bool | None = None
    trajectory_available: bool | None = None
    trajectory_ref: str | None = None
    trajectory_digest: str | None = None
    exposure: Literal["exposed", "unexposed", "unknown",
                      "no recorded exposure; prior history unverifiable"] = "unknown"
    exposure_events: tuple[ExposureEvent, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    def gold_exclusion(self) -> dict[str, str] | None:
        from evallab.gold.exclusions import source_exclusion

        return (
            source_exclusion(self.source)
            or source_exclusion(self.provenance.store)
            or source_exclusion(self.subject_id.split(":", 1)[0])
            or source_exclusion(self.subject_id.split("/", 1)[0])
        )

    @model_validator(mode="after")
    def _gold_state(self) -> IssueItem:
        if self.label_state is None:
            if self.polarity is None:
                raise ValueError("Null polarity requires an explicit gold label state")
            return self
        exclusion = self.gold_exclusion()
        if exclusion is not None:
            raise ValueError(f"Excluded gold source {exclusion['name']}: {exclusion['reason']}")
        expected = {"executed-positive": "positive", "reviewed-control": "negative"}.get(
            self.label_state
        )
        if self.polarity != expected:
            raise ValueError("Gold label state contradicts polarity")
        if self.label_state == "executed-positive" and not any(
            ref.normalized_step_id is not None and ref.event_kind in {"call", "result"}
            and ref.resolution != "unmatched-result" for ref in self.step_locators
        ):
            raise ValueError("Executed-positive needs a resolved source action")
        return self

    @model_validator(mode="after")
    def _deterministic_id(self) -> IssueItem:
        expected = make_item_id(self.source, self.subject_id, self.kind, self.locator)
        if self.item_id != expected:
            raise ValueError(f"item_id {self.item_id} is not deterministic (want {expected})")
        return self


class SourceSummary(ContractModel):

    source: str = Field(pattern=_SLUG)
    kind: Kind
    granularity: Granularity
    split: Split
    license: str | None
    actor_transcript: bool | None
    counts: dict[Polarity, int]
    reason: str = Field(min_length=1, max_length=300)
    provenance: Provenance


def new_item(**fields: Any) -> IssueItem:
    item_id = make_item_id(
        fields["source"], fields["subject_id"], fields["kind"], fields.get("locator", "")
    )
    return IssueItem(item_id=item_id, **fields)


@dataclass
class BankContext:

    repo_root: Path
    state_root: Path = field(default_factory=default_state_root)
    cloud_db: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                CLOUD_DB_ENV,
                Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite",
            )
        )
    )

    @cached_property
    def registry(self) -> CorpusRegistry:
        from evallab.production.corpus_queue import CorpusRegistry

        return CorpusRegistry(self.repo_root)

    def strict_split(
        self,
        declared: str,
        *,
        key_id: str | None = None,
        item_id: str | None = None,
        task_id: str | None = None,
        task_digest: str | None = None,
    ) -> Split:
        split = normalize_split(declared)
        if split != "DEV":
            return split
        registry = self.registry
        if not registry.has_assignment_authority():
            return "unassigned"
        if key_id is not None and registry.has_split_assignments(key_id):
            assigned = registry.assigned_split(key_id, item_id=item_id, task_id=task_id)
            if assigned == "test":
                return "TEST"
        if registry.sealed_identity(task_id=task_id, task_digest=task_digest) is not None:
            return "TEST"
        distinctive = item_id if item_id and not _BARE_NUMBER.match(item_id) else None
        if registry.assigned_test_anywhere(item_id=distinctive, task_id=task_id):
            return "TEST"
        return "DEV"


def source_modules() -> list[str]:
    from evallab.failure_library.bank import sources as source_package

    return sorted(info.name for info in pkgutil.iter_modules(source_package.__path__))


def _load_source(name: str) -> Any:
    from evallab.failure_library.bank import sources as source_package

    return importlib.import_module(f"{source_package.__name__}.{name}")


def available_sources() -> dict[str, str]:
    found: dict[str, str] = {}
    for name in source_modules():
        slug = _load_source(name).SOURCE
        if slug in found:
            raise ValueError(f"source {slug!r} is defined by {found[slug]} and {name}")
        found[slug] = name
    return found


def _breakdown(items: Sequence[IssueItem]) -> dict[str, Any]:
    def count(key: str) -> dict[str, int]:
        return dict(sorted(Counter(str(getattr(item, key)) for item in items).items()))

    combos = Counter(
        f"{item.kind}|{item.polarity}|{item.split}" for item in items
    )
    return {
        "count": len(items),
        "by_kind": count("kind"),
        "by_polarity": count("polarity"),
        "by_split": count("split"),
        "by_label_type": count("label_type"),
        "by_granularity": count("granularity"),
        "by_license": count("license"),
        "by_actor_transcript": count("actor_transcript"),
        "by_kind_polarity_split": dict(sorted(combos.items())),
        "runnable_in_harness": sum(item.harness_selector is not None for item in items),
        "positive_units": sum(item.positive_units or 0 for item in items),
    }


class IssueBank:

    def __init__(self, repo_root: Path, *, state_root: Path | None = None) -> None:
        self.repo_root = repo_root
        self.state_root = state_root if state_root is not None else default_state_root()
        self.items_root = self.state_root / STATE_SUBDIR
        self.manifest_path = repo_root / MANIFEST_PATH

    def items_path(self, source: str) -> Path:
        if not re.fullmatch(_SLUG, source):
            raise ValueError(f"invalid source slug: {source!r}")
        return self.items_root / f"{source}.jsonl"

    def read_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.is_file():
            return {"schema_version": SCHEMA_VERSION, "sources": {}}
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def build(self, ctx: BankContext, sources: Iterable[str] | None = None) -> dict[str, Any]:
        with bank_writer_lock(self):
            known = available_sources()
            wanted = list(known) if sources is None else list(sources)
            unknown = sorted(set(wanted) - set(known))
            if unknown:
                raise ValueError(f"unknown bank sources: {unknown}; known: {sorted(known)}")
            manifest = self.read_manifest()
            entries: dict[str, Any] = dict(manifest.get("sources", {}))
            for slug in wanted:
                entries[slug] = self._build_source(ctx, slug, _load_source(known[slug]))
                manifest = {
                    "schema_version": SCHEMA_VERSION,
                    "split_assignments_digest": ctx.registry.split_assignments_digest,
                    "sources": dict(sorted(entries.items())),
                    "totals": _totals(entries),
                }
                _atomic_write(
                    self.manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n"
                )
            return manifest

    def _build_source(self, ctx: BankContext, slug: str, module: Any) -> dict[str, Any]:
        items: dict[str, IssueItem] = {}
        summaries: list[SourceSummary] = []
        for record in module.build(ctx):
            if isinstance(record, SourceSummary):
                if record.source != slug:
                    raise ValueError(f"{slug}: summary names source {record.source}")
                summaries.append(record)
                continue
            if record.source != slug:
                raise ValueError(f"{slug}: item {record.item_id} names source {record.source}")
            previous = items.get(record.item_id)
            if previous is not None and previous != record:
                raise ValueError(f"{slug}: conflicting duplicate item {record.item_id}")
            items[record.item_id] = record
        rows = [items[key] for key in sorted(items)]
        text = "".join(_line(item.model_dump(mode="json")) + "\n" for item in rows)
        path = self.items_path(slug)
        if rows:
            _atomic_write(path, text)
        elif path.exists():
            path.unlink()
        entry: dict[str, Any] = {
            "itemized": bool(rows),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if rows else None,
            **_breakdown(rows),
        }
        if summaries:
            entry["summaries"] = [summary.model_dump(mode="json") for summary in summaries]
        return entry

    def iter_items(
        self,
        sources: Iterable[str] | None = None,
        *,
        split: str | None = None,
        kind: str | None = None,
    ) -> Iterator[IssueItem]:
        names = (
            sorted(path.stem for path in self.items_root.glob("*.jsonl"))
            if sources is None
            else list(sources)
        )
        for name in names:
            path = self.items_path(name)
            if not path.is_file():
                continue
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    item = IssueItem.model_validate_json(line)
                    exclusion = item.gold_exclusion()
                    if item.kind == "cheat" and exclusion and exclusion["procedure"] in {"R1", "R3", "R7"}:
                        continue
                    if split is not None and item.split != split:
                        continue
                    if kind is not None and item.kind != kind:
                        continue
                    yield item

    def verify(self) -> list[str]:
        problems: list[str] = []
        for slug, entry in self.read_manifest().get("sources", {}).items():
            if not entry.get("itemized"):
                continue
            path = self.items_path(slug)
            if not path.is_file():
                problems.append(f"{slug}: item bytes missing at {path}")
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != entry.get("sha256"):
                problems.append(f"{slug}: sha256 {digest} differs from manifest {entry.get('sha256')}")
        return problems


def _totals(entries: dict[str, Any]) -> dict[str, Any]:
    items: Counter[str] = Counter()
    by_source_kind: Counter[str] = Counter()
    for slug, entry in entries.items():
        for combo, number in entry.get("by_kind_polarity_split", {}).items():
            kind, polarity, split = combo.split("|")
            items[f"{polarity}|{split}"] += number
            by_source_kind[f"{slug}|{kind}"] += number
        for summary in entry.get("summaries", []):
            for polarity, number in summary["counts"].items():
                items[f"{polarity}|{summary['split']}(summary)"] += number
                by_source_kind[f"{slug}|{summary['kind']}(summary)"] += number
    return {
        "items": sum(entry.get("count", 0) for entry in entries.values()),
        "by_polarity_split": dict(sorted(items.items())),
        "by_source_kind": dict(sorted(by_source_kind.items())),
    }
