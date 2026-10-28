"""Deterministic JSONL storage for the Failure Library.

Findings are small and reviewed, so they live in Git:
``research/failure-library/findings/<collection>.jsonl``. Run records can number
in the hundreds of thousands, so they live in local state
(``$EVALLAB_FAILURE_LIBRARY_STATE`` or ``~/.local/state/evallab-failure-library``)
and Git keeps a manifest with their count and digest beside the findings.

Writes are whole-collection, sorted by id and atomic. Every published collection
and manifest is also retained in the existing evidence CAS before replacement.
One id with two different bodies in one write is refused.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from evallab.evidence_store import store_blob
from evallab.failure_library.records import SCHEMA_VERSION, Finding, RunRecord
from evallab.schemas import ContractModel

STATE_ENV = "EVALLAB_FAILURE_LIBRARY_STATE"
LIBRARY_DIR = Path("research/failure-library")


def default_state_root() -> Path:
    configured = os.environ.get(STATE_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "evallab-failure-library"


class RunManifest(ContractModel):
    """Committed provenance for one run collection kept outside Git."""

    schema_version: Literal["failure-library/v1"] = SCHEMA_VERSION
    collection: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    source_revision: str = Field(min_length=1)
    count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcomes: dict[str, int]
    #: Label occurrences by kind; one run can carry several labels of one kind.
    label_kinds: dict[str, int]


def _line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _unique_by_id(records: Iterable[Any], key: str) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for record in records:
        payload = record.model_dump(mode="json")
        record_id = payload[key]
        previous = seen.get(record_id)
        if previous is not None and previous != payload:
            raise ValueError(f"conflicting records share {key}={record_id}")
        seen[record_id] = payload
    return [seen[record_id] for record_id in sorted(seen)]


def _collection_name(collection: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", collection):
        raise ValueError(f"invalid collection name: {collection!r}")
    return collection


class LibraryStore:
    """Read and write findings and run records for named collections."""

    def __init__(self, *, findings_root: Path, manifest_root: Path, runs_root: Path) -> None:
        self.findings_root = findings_root
        self.manifest_root = manifest_root
        self.runs_root = runs_root
        self.history_root = runs_root.parent / "history"

    @classmethod
    def for_repo(cls, repo_root: Path, *, state_root: Path | None = None) -> LibraryStore:
        library = repo_root / LIBRARY_DIR
        state = state_root if state_root is not None else default_state_root()
        return cls(
            findings_root=library / "findings",
            manifest_root=library / "runs",
            runs_root=state / "runs",
        )

    def findings_path(self, collection: str) -> Path:
        return self.findings_root / f"{_collection_name(collection)}.jsonl"

    def runs_path(self, collection: str) -> Path:
        return self.runs_root / f"{_collection_name(collection)}.jsonl"

    def manifest_path(self, collection: str) -> Path:
        return self.manifest_root / f"{_collection_name(collection)}.manifest.json"

    def _publish(self, path: Path, text: str) -> None:
        """Retain old and new bytes before advancing a mutable collection pointer."""
        if path.is_file():
            store_blob(self.history_root, path.read_bytes())
        store_blob(self.history_root, text)
        _atomic_write(path, text)

    def write_findings(self, collection: str, findings: Iterable[Finding]) -> Path:
        items = list(findings)
        strays = sorted({item.collection for item in items if item.collection != collection})
        if strays:
            raise ValueError(f"findings from {strays} cannot be written to collection {collection}")
        rows = _unique_by_id(items, "finding_id")
        path = self.findings_path(collection)
        self._publish(path, "".join(_line(row) + "\n" for row in rows))
        return path

    def write_runs(
        self, collection: str, runs: Iterable[RunRecord], *, source_revision: str
    ) -> RunManifest:
        items = list(runs)
        strays = sorted({item.collection for item in items if item.collection != collection})
        if strays:
            raise ValueError(f"runs from {strays} cannot be written to collection {collection}")
        rows = _unique_by_id(items, "run_id")
        text = "".join(_line(row) + "\n" for row in rows)
        manifest = RunManifest(
            collection=collection,
            source_revision=source_revision,
            count=len(rows),
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            outcomes=dict(sorted(Counter(row["outcome"] for row in rows).items())),
            label_kinds=dict(
                sorted(Counter(label["kind"] for row in rows for label in row["labels"]).items())
            ),
        )
        # Validate the manifest before advancing either latest pointer.
        self._publish(self.runs_path(collection), text)
        self._publish(
            self.manifest_path(collection), _line(manifest.model_dump(mode="json")) + "\n"
        )
        return manifest

    def collections(self, kind: Literal["findings", "runs"]) -> list[str]:
        root = self.findings_root if kind == "findings" else self.runs_root
        if not root.is_dir():
            return []
        return sorted(path.name.removesuffix(".jsonl") for path in root.glob("*.jsonl"))

    def iter_findings(self, collection: str | None = None) -> Iterator[Finding]:
        names = [collection] if collection is not None else self.collections("findings")
        for name in names:
            with self.findings_path(name).open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        yield Finding.model_validate_json(line)

    def iter_runs(self, collection: str | None = None) -> Iterator[RunRecord]:
        names = [collection] if collection is not None else self.collections("runs")
        for name in names:
            with self.runs_path(name).open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        yield RunRecord.model_validate_json(line)

    def verify_runs(self, collection: str) -> list[str]:
        """Compare local run bytes with the committed manifest; return the problems."""
        manifest_path = self.manifest_path(collection)
        if not manifest_path.is_file():
            return [f"no committed manifest for {collection}"]
        manifest = RunManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        runs_path = self.runs_path(collection)
        if not runs_path.is_file():
            return [f"run bytes for {collection} are missing at {runs_path}"]
        digest = hashlib.sha256(runs_path.read_bytes()).hexdigest()
        if digest != manifest.sha256:
            return [f"{collection}: sha256 {digest} differs from manifest {manifest.sha256}"]
        return []

    def stats(self) -> dict[str, Any]:
        findings: dict[str, Counter[str]] = {
            "collection": Counter(),
            "tier": Counter(),
            "defect_class": Counter(),
            "split": Counter(),
            "status_now": Counter(),
            "evidence_kind": Counter(),
            "record_basis": Counter(),
            "execution_support": Counter(),
        }
        task_ids: set[tuple[str, str, str]] = set()
        task_versions: set[tuple[str, str, str, str | None, str | None]] = set()
        for finding in self.iter_findings():
            findings["collection"][finding.collection] += 1
            findings["tier"][finding.tier] += 1
            findings["defect_class"][finding.defect_class] += 1
            findings["split"][finding.split] += 1
            findings["status_now"][finding.status_now] += 1
            kinds = {item.kind for item in finding.evidence}
            for kind in kinds:
                findings["evidence_kind"][kind] += 1
            basis = "constructed" if "construction" in kinds else "reported_failure"
            findings["record_basis"][basis] += 1
            executed = [item for item in finding.evidence if item.kind == "executed"]
            support = (
                "receipt_referenced"
                if any(item.receipt_ref for item in executed)
                else "reproducer_referenced_without_receipt"
                if executed
                else "no_execution_evidence"
            )
            findings["execution_support"][support] += 1
            task = finding.task
            identity = (task.source_id, task.upstream_repo, task.task_id)
            task_ids.add(identity)
            task_versions.add((*identity, task.upstream_revision, task.task_version_digest))
        runs: dict[str, Counter[str]] = {
            "collection": Counter(),
            "outcome": Counter(),
            "label_kind": Counter(),
            "runs_with_label_kind": Counter(),
            "split": Counter(),
        }
        for run in self.iter_runs():
            runs["collection"][run.collection] += 1
            runs["outcome"][run.outcome] += 1
            runs["split"][run.split] += 1
            for label in run.labels:
                runs["label_kind"][label.kind] += 1
            for kind in {label.kind for label in run.labels}:
                runs["runs_with_label_kind"][kind] += 1
        return {
            "findings": {
                "total": sum(findings["collection"].values()),
                "unique_task_ids": len(task_ids),
                "unique_task_versions": len(task_versions),
                **{f"by_{name}": dict(sorted(counts.items())) for name, counts in findings.items()},
            },
            "runs": {
                "total": sum(runs["collection"].values()),
                **{f"by_{name}": dict(sorted(counts.items())) for name, counts in runs.items()},
            },
        }
