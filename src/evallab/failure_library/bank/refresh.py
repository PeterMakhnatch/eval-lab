"""Incremental, local-only refresh of the lab's own issue-bank collections.

Source builders still own labels. Seeker receipts are rebuilt in a fresh local
staging directory by the existing witness exporter, never applied to the cloud
DB. A source is published only after its files and relevant committed database
rows are stable; incomplete evidence leaves the previous collection in place.

``bank_writer_lock`` is also the integration hook for ``IssueBank.build``. All
bank writers must hold it across manifest read/modify/write, not just replace.
"""

from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO

from evallab.failure_library.bank import BankContext, IssueBank, IssueItem, _totals
from evallab.failure_library.bank.sources import grader_repairs, run_health, seeker
from evallab.failure_library.store import _atomic_write
from evallab.production import seeker_known_hacks

SCHEMA_VERSION = "issue-bank-refresh/v1"
PAIR_SCHEMA_VERSION = "issue-bank-repair-pair/v1"
SOURCES = ("grader-repairs", "run-health", "seeker")
REFRESH_RELATIVE = Path("bank") / "refresh"
RECEIPT_FILE = "receipt.json"
# CorpusRegistry reads these in addition to the assignment ledger. Its package
# and heldout seals must invalidate a cached DEV refresh as well.
SPLIT_AUTHORITY_INPUTS = (
    Path("research/answer-keys/splits/assignments.csv"),
    Path("research/answer-keys/tb4-audit-v1/items.jsonl"),
    Path("research/answer-keys/tb4-audit-v1/tasks.jsonl"),
    Path("research/answer-keys/cheatbench-actions-v1/items.jsonl"),
    Path("research/answer-keys/hacktrace-act-v1/items.jsonl"),
    Path("research/experiments/mimo-defect-map-v1/inventory.json"),
)
_SOURCE_ERRORS = (OSError, ValueError, KeyError, TypeError, sqlite3.Error, csv.Error)
_INCOMPLETE_WITNESS_GATES = frozenset(
    {
        "retained_evidence", "witness_verification", "response_digest_mismatch",
        "missing_response_digest", "identity_mismatch",
    }
)
_THREAD_LOCK = threading.RLock()
_HELD_LOCKS: dict[Path, tuple[BinaryIO, int]] = {}


class SourceNotReady(ValueError):
    """A source cannot yet replace the last good bank collection."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


@dataclass(frozen=True)
class InputSnapshot:
    digests: dict[str, str | None]
    roots: tuple[Path, ...] = ()
    seeker_cells_roots: tuple[Path, ...] = ()

    def receipt(self) -> dict[str, Any]:
        return {
            "input_digests": self.digests,
            "input_roots": [str(path) for path in self.roots],
        }


def receipt_path(bank: IssueBank) -> Path:
    return bank.state_root / REFRESH_RELATIVE / RECEIPT_FILE


@contextmanager
def bank_writer_lock(bank: IssueBank) -> Iterator[None]:
    """Serialize writers sharing either a manifest or local item directory.

    Locks live outside the checkout, and are keyed by the protected path rather
    than ``state_root`` alone: two state roots can still share one manifest.
    The process lock makes the file locks thread-safe and reentrant, so the core
    build hook can nest inside a refresh without acquiring its own lock twice.
    """
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


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def _digest(path: Path) -> str | None:
    if path.is_symlink():
        raise SourceNotReady(f"symlink source refused: {path}")
    if not path.exists():
        return None
    before = path.stat()
    if not path.is_file():
        raise SourceNotReady(f"source is not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise SourceNotReady(f"source changed while hashing: {path}")
    return digest.hexdigest()


def _objects(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise SourceNotReady(f"expected a JSON object at {path}#{number}")
            yield row


def _cells_roots(ctx: BankContext, explicit: tuple[Path, ...] | None) -> tuple[Path, ...]:
    roots = set(explicit or ())
    if explicit is None:
        results = seeker._results_root().expanduser().absolute()
        candidates = [results, *sorted(results.glob("*/*"))]
        for candidate in candidates:
            if candidate.is_dir() and next(candidate.glob("*/*/*/turn-*/result.json"), None):
                roots.add(candidate)
    # Published labels are discovery leads only. Their flags/receipts never bypass
    # the exporter's independent task, route, candidate and native witness gates.
    labels = ctx.prod_live / seeker.LABELS_RELATIVE
    if labels.is_file():
        for row in _objects(labels):
            verdict = row.get("source_label") or {}
            if not isinstance(verdict, dict):
                raise SourceNotReady(f"malformed seeker source_label: {labels}")
            if (verdict.get("verdict_enacted") or verdict.get("verdict")) != "yes":
                continue
            evidence = row.get("evidence") or {}
            directory = evidence.get("cell_directory") if isinstance(evidence, dict) else None
            if not isinstance(directory, str) or not directory:
                raise SourceNotReady(f"missing retained cell_directory for seeker witness in {labels}")
            cell = Path(directory).expanduser().absolute()
            if not cell.is_dir() or len(cell.parents) < 4:
                raise SourceNotReady(f"retained seeker witness directory is unavailable: {cell}")
            roots.add(cell.parents[3])
    return tuple(sorted(path.expanduser().absolute() for path in roots))


def _cloud_digest(ctx: BankContext, source: str) -> str | None:
    """Hash the rows a collector uses, not gigabytes of unrelated actor data.

    Each read transaction is a consistent SQLite snapshot, including committed
    WAL rows. Unrelated database writes must not invalidate every watch cycle.
    Actor availability uses the collector's exact blinding rule; actor contents
    themselves are bound separately when an input is materialized for review.
    """
    if ctx.cloud_db.is_symlink():
        raise SourceNotReady(f"symlink source refused: {ctx.cloud_db}")
    if not ctx.cloud_db.exists():
        return None
    connection = sqlite3.connect(f"file:{ctx.cloud_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    digest = hashlib.sha256()

    def add(tag: str, rows: Iterable[Iterable[Any]]) -> None:
        digest.update(tag.encode() + b"\n")
        for row in rows:
            digest.update(json.dumps(list(row), ensure_ascii=False, allow_nan=False).encode() + b"\n")

    try:
        connection.execute("BEGIN")
        if source == grader_repairs.SOURCE:
            add("repair-task-identities/v1", connection.execute(
                "SELECT task_id, dataset_id, split, original_grader_digest,"
                " repaired_grader_digest, metadata_json FROM corpus_tasks ORDER BY task_id"
            ))
        elif source == seeker.SOURCE:
            add("seeker-task-identities/v1", connection.execute(
                "SELECT task_id, dataset_id FROM corpus_tasks ORDER BY task_id"
            ))
            add("seeker-trace-identities/v1", connection.execute(
                "SELECT trace_id FROM agent_traces ORDER BY trace_id"
            ))
        elif source == run_health.SOURCE:
            from evallab.failure_library.bank.sources.cloud_traces import _IDENTITY_SQL

            add("health-admission-identities/v1", connection.execute(_IDENTITY_SQL))
            run_ids: set[str] = set()
            findings = run_health.latest_findings(ctx.prod_live / "run-health")
            if findings is not None:
                run_ids.update(
                    str(row["run_id"]) for _, row in run_health._jsonl(findings)
                    if row.get("verdict") == "alarm" and row.get("kind") in run_health.KINDS
                    and row.get("run_id")
                )
            key = ctx.cross_test_registry.parent / run_health.AUDIT_RELATIVE / "key.jsonl"
            if key.is_file():
                run_ids.update(str(row["run_id"]) for _, row in run_health._jsonl(key) if row.get("run_id"))
            for run_id in sorted(run_ids):
                add(f"health-run:{run_id}", connection.execute(run_health._SQL, (run_id,)))
                add("actor-availability", [(run_health._has_actor_transcript(connection, run_id),)])
        else:
            raise ValueError(f"unsupported refresh source: {source}")
    finally:
        connection.close()
    return digest.hexdigest()


def _snapshot(
    ctx: BankContext, source: str, explicit_roots: tuple[Path, ...] | None
) -> InputSnapshot:
    files = {ctx.repo_root / relative for relative in SPLIT_AUTHORITY_INPUTS}
    # SQLite is fingerprinted by relevant committed rows below, not physical
    # database/WAL bytes (which change whenever any other lane writes).
    roots: tuple[Path, ...] = ()
    cells_roots: tuple[Path, ...] = ()
    if source == grader_repairs.SOURCE:
        files.update(
            ctx.repo_root / "library/task-variants" / f"{record}.json"
            for _, _, _, record in grader_repairs._TS_REPAIRS
        )
        files.add(grader_repairs.ts14_summary_path())
        base = ctx.repo_root / grader_repairs._V6_DIR
        files.update((base / "ports.csv", base / "results.csv"))
    elif source == run_health.SOURCE:
        findings = ctx.prod_live / "run-health"
        roots = (findings,)
        files.update(
            path for path in findings.glob("findings-run-health-*.jsonl")
            if run_health.FINDINGS_PATTERN.match(path.name)
        )
        audit = ctx.cross_test_registry.parent / run_health.AUDIT_RELATIVE
        files.add(audit / "key.jsonl")
        files.update(audit.glob("out-*.jsonl"))
    elif source == seeker.SOURCE:
        results = seeker._results_root().expanduser().absolute()
        cells_roots = _cells_roots(ctx, explicit_roots)
        roots = (results, *cells_roots)
        files.update((ctx.prod_live / seeker.LABELS_RELATIVE, ctx.prod_live / seeker.RUNS_RELATIVE))
        for cells in results.glob(seeker.PILOT_GLOB):
            files.add(cells)
            files.update(cells.parent.glob("*/*/*/turn-*/response.jsonl"))
        for root in cells_roots:
            if root.is_symlink():
                raise SourceNotReady(f"symlink seeker cells root refused: {root}")
            for result in root.glob("*/*/*/turn-*/result.json"):
                files.update(path for path in result.parent.rglob("*") if path.is_file() or path.is_symlink())
    else:
        raise ValueError(f"unsupported refresh source: {source}")
    digests = {
        str(path.expanduser().absolute()): _digest(path.expanduser().absolute())
        for path in sorted(files)
    }
    digests[f"sqlite-rows:{ctx.cloud_db.absolute()}#{source}/v1"] = _cloud_digest(ctx, source)
    return InputSnapshot(digests, roots, cells_roots)


def _full_digest(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    digest = value.removeprefix("sha256:").lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return None
    return "sha256:" + digest


def _witness_bindings(ctx: BankContext, state_dir: Path) -> tuple[list[str], int, int]:
    """Fail closed if the exporter cannot attest exact per-side identities.

    Older exporter receipts can confirm rewards without retaining mandatory
    package or per-side candidate bindings. Such receipts are not automatically
    promoted merely because their outcome or exporter-confirmed count is true.
    """
    labels_path = state_dir / seeker_known_hacks.LABELS_FILE
    runs_path = state_dir / seeker_known_hacks.RUNS_FILE
    if not labels_path.is_file():
        return [], 0, 0
    labels = list(_objects(labels_path))
    runs = list(_objects(runs_path))
    envelopes = {row["run_id"]: row for row in runs}
    missing: set[str] = set()
    excluded: set[str] = set()
    for label in labels:
        task = envelopes[label["run_id"]].get("task") or {}
        evidence = label.get("evidence") or {}
        candidate = _full_digest(label.get("candidate_digest"))
        before = _full_digest(task.get("original_grader_digest"))
        after = _full_digest(task.get("repaired_grader_digest"))
        if any(
            ctx.strict_split(
                "DEV", key_id=label.get("dataset_id"), item_id=str(label["task_id"]).rsplit("/", 1)[-1],
                task_id=label["task_id"], task_digest=digest,
            ) != "DEV"
            for digest in (before, after)
        ):
            excluded.add(label["run_id"])
            continue
        if before is None or after is None or before == after:
            missing.add("distinct full original/repaired package digests")
        for side, expected in (("original", before), ("repaired", after)):
            identity = (evidence.get(side) or {}).get("identity") or {}
            if expected is None or _full_digest(identity.get("task_digest")) != expected:
                missing.add(f"{side} native receipt task_digest bound to its exact package")
            if candidate is None or _full_digest(identity.get("candidate_digest")) != candidate:
                missing.add(f"{side} native receipt candidate_digest bound to the executed candidate")
    if excluded:
        for path, rows in ((labels_path, labels), (runs_path, runs)):
            text = "".join(
                json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"
                for row in rows if row["run_id"] not in excluded
            )
            _atomic_write(path, text)
    return sorted(missing), len(excluded), len(labels) - len(excluded)


def _stage_seeker(ctx: BankContext, roots: tuple[Path, ...], live: Path) -> dict[str, Any]:
    state_dir = live / "seeker-known-hacks"
    totals: Counter[str] = Counter()
    rejected: Counter[str] = Counter()
    for root in roots:
        # Stage is explicitly local and fresh; apply/CloudTraceDatabase is never
        # used. The exporter, not a result/outcome flag, constructs every label.
        report = seeker_known_hacks.export_known_hacks(
            root,
            state_dir=state_dir,
            assignments_csv=ctx.repo_root / "research/answer-keys/splits/assignments.csv",
            mode="stage",
        )
        totals.update({key: report[key] for key in ("cells_seen", "confirmed", "exported", "already_exported")})
        rejected.update(report["rejected_by_gate"])
    details = {
        "status": "no-verified-witness",
        "cells_seen": totals["cells_seen"],
        "exporter_confirmed": totals["confirmed"],
        "confirmed": 0,
        "staged": totals["exported"],
        "already_staged": totals["already_exported"],
        "rejected_by_gate": dict(sorted(rejected.items())),
        "cells_roots": [str(path) for path in roots],
    }
    if not roots:
        details["missing_prerequisite"] = "Retained SB-2 cells root; supply --seeker-cells-root to validate witnesses"
    incomplete = sorted(_INCOMPLETE_WITNESS_GATES.intersection(rejected))
    missing, excluded, admitted = _witness_bindings(ctx, state_dir)
    details["strict_non_dev"] = excluded
    if missing:
        details["missing_witness_bindings"] = missing
    if incomplete or missing:
        details["status"] = "source-not-ready"
        reason = ", ".join(incomplete + missing)
        raise SourceNotReady(
            "seeker retained witness is incomplete or insufficiently bound: " + reason,
            details={"verified_witness": details},
        )
    details["confirmed"] = admitted
    details["status"] = "verified" if admitted else "no-verified-witness"
    return details


def _output(bank: IssueBank, source: str) -> dict[str, Any]:
    path = bank.items_path(source)
    item_digests: dict[str, str] = {}
    if path.is_file():
        with path.open("rb") as handle:
            for line in handle:
                if not line.strip():
                    continue
                item = IssueItem.model_validate_json(line)
                if item.item_id in item_digests:
                    raise ValueError(f"duplicate output item: {item.item_id}")
                item_digests[item.item_id] = hashlib.sha256(line.rstrip(b"\n")).hexdigest()
    return {
        "count": len(item_digests),
        "sha256": _digest(path),
        "item_digests": dict(sorted(item_digests.items())),
    }


def _repair_pairs(ctx: BankContext, items: Iterable[IssueItem]) -> dict[str, Any]:
    """Retain full pairs alongside the builder's intentionally short subject id."""
    cloud: dict[tuple[str, str, str], tuple[str, str]] = {}
    for found in grader_repairs._cloud_variants(ctx):
        pair = grader_repairs._cloud_pair(found.variant)
        if pair is None:
            continue
        before, after = pair
        origin = str(found.variant.get("source") or "unknown")
        locator = str(found.variant.get("variant_id") or f"{origin}:{grader_repairs._short(after)}")
        cloud[(found.task_id, grader_repairs._subject_id(found.task_id, before, after), locator)] = pair
    ports: dict[str, dict[str, str]] = {}
    ports_path = ctx.repo_root / grader_repairs._V6_DIR / "ports.csv"
    if ports_path.is_file():
        with ports_path.open(newline="", encoding="utf-8") as handle:
            ports = {row["task"]: row for row in csv.DictReader(handle)}
    pairs: dict[str, Any] = {}
    for item in items:
        store = item.provenance.store
        if store.startswith("git:pr/"):
            variant = json.loads((ctx.repo_root / item.provenance.ref).read_text(encoding="utf-8"))
            before, after = variant["parent"]["digest"], variant["variant_digest"]
            digest_kind = "package"
        elif store == grader_repairs._CLOUD_STORE:
            before, after = cloud[(str(item.task_id), item.subject_id, item.locator)]
            digest_kind = "grader-manifest"
        elif store == "research-experiments:mimo-clean-v6-repaired":
            row = ports[str(item.task_id)]
            before, after = row["v6_parent_digest"], row["repaired_variant"]
            digest_kind = "package"
        else:
            raise SourceNotReady(f"repair pair source is unsupported: {store}")
        if not isinstance(before, str) or not isinstance(after, str) or not before or not after or before == after:
            raise SourceNotReady(f"repair has no distinct before/after pair: {item.provenance.ref}")
        if grader_repairs._subject_id(str(item.task_id), before, after) != item.subject_id:
            raise SourceNotReady(f"repair pair identity changed: {item.provenance.ref}")
        qualification = next(
            (status for status in (grader_repairs.STATIC_ONLY, grader_repairs.NATIVE_PARTIAL, grader_repairs.NATIVE_VALIDATED)
             if f":{status}" in item.label),
            None,
        )
        if qualification is None:
            raise SourceNotReady(f"repair qualification is unavailable: {item.provenance.ref}")
        pairs[item.item_id] = {
            "schema_version": PAIR_SCHEMA_VERSION,
            "task_id": item.task_id,
            "before_digest": before,
            "after_digest": after,
            "digest_kind": digest_kind,
            "qualification": qualification,
            "native_qualified": qualification == grader_repairs.NATIVE_VALIDATED,
            "repaired_reference_rejected": "repaired reference rejected" in item.label,
            "label_type": item.label_type,
            "split": item.split,
            "source_ref": item.provenance.ref,
        }
    return dict(sorted(pairs.items()))


def _load_receipt(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": SCHEMA_VERSION, "sources": {}}
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict) or receipt.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported bank refresh receipt: {path}")
    if not isinstance(receipt.get("sources"), dict):
        raise ValueError(f"malformed bank refresh receipt sources: {path}")
    return receipt


def _publish_source(bank: IssueBank, staged: IssueBank, source: str, manifest: dict[str, Any]) -> None:
    path, replacement = bank.items_path(source), staged.items_path(source)
    if replacement.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        replacement.replace(path)
    else:
        path.unlink(missing_ok=True)
    _atomic_write(bank.manifest_path, _json(manifest))


def _quarantine_stale_dev(
    bank: IssueBank, ctx: BankContext, source: str, prior: dict[str, Any]
) -> dict[str, Any]:
    """Retain labels, but never retain DEV after unverified authority changes."""
    ctx = replace(ctx)
    authority: dict[str, str | None] = {}
    for relative in SPLIT_AUTHORITY_INPUTS:
        path = (ctx.repo_root / relative).expanduser().absolute()
        try:
            authority[str(path)] = _digest(path)
        except _SOURCE_ERRORS:
            authority[str(path)] = None
    baseline = prior.get("input_digests", {})
    if all(path in baseline and baseline[path] == digest for path, digest in authority.items()):
        return {}
    ledger = (ctx.repo_root / SPLIT_AUTHORITY_INPUTS[0]).expanduser().absolute()
    current_digest = authority[str(ledger)]
    items = list(bank.iter_items([source]))
    changed: dict[str, str] = {}
    for index, item in enumerate(items):
        if item.split != "DEV":
            continue
        try:
            split = ctx.strict_split("DEV", task_id=item.task_id, item_id=item.task_id)
        except _SOURCE_ERRORS:
            split = "unassigned"
        # Missing source identities cannot certify a new DEV admission. Keep an
        # explicit TEST seal when known, otherwise quarantine without relabeling.
        split = "TEST" if split == "TEST" else "unassigned"
        items[index] = item.model_copy(update={"split": split, "harness_selector": None})
        changed[item.item_id] = split
    if not changed:
        return {}
    work = bank.state_root / REFRESH_RELATIVE
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".admission-", dir=work) as directory:
        staged = IssueBank(ctx.repo_root, state_root=Path(directory))
        entry = staged._build_source(ctx, source, SimpleNamespace(build=lambda _: iter(items)))
        manifest = bank.read_manifest()
        entries = dict(manifest.get("sources", {}))
        entries[source] = entry
        manifest.update(
            sources=dict(sorted(entries.items())), totals=_totals(entries),
            split_assignments_digest="sha256:" + current_digest if current_digest else None,
        )
        _publish_source(bank, staged, source, manifest)
    details: dict[str, Any] = {
        "output": _output(bank, source), "retained_dev_quarantined": len(changed),
    }
    if "repair_pairs" in prior:
        details["repair_pairs"] = {
            key: {**pair, "split": changed.get(key, pair["split"])}
            for key, pair in prior["repair_pairs"].items()
        }
    return details


def _validate_rows(path: Path, fields: tuple[str, ...]) -> None:
    for number, row in enumerate(_objects(path), 1):
        if any(not isinstance(row.get(key), str) or not row[key].strip() for key in fields):
            raise SourceNotReady(f"incomplete source row in {path}#{number}; required: {', '.join(fields)}")


def _validate_source(ctx: BankContext, source: str) -> None:
    """Reject partial structures the historical builders permissively skip."""
    if source == run_health.SOURCE:
        latest = run_health.latest_findings(ctx.prod_live / "run-health")
        if latest is not None:
            _validate_rows(latest, ("run_id", "kind", "verdict"))
        audit = ctx.cross_test_registry.parent / run_health.AUDIT_RELATIVE
        if (audit / "key.jsonl").is_file():
            _validate_rows(audit / "key.jsonl", ("run_id", "packet"))
            for path in audit.glob("out-*.jsonl"):
                _validate_rows(path, ("packet", "verdict"))
    elif source == seeker.SOURCE:
        for path in seeker._results_root().glob(seeker.PILOT_GLOB):
            _validate_rows(path, ("dataset_id", "task", "model"))
            for row in _objects(path):
                if not any(turn in row for turn in seeker.TURNS) or any(
                    turn in row and not isinstance(row[turn], dict) for turn in seeker.TURNS
                ):
                    raise SourceNotReady(f"incomplete seeker turn receipts: {path}")
    elif source == grader_repairs.SOURCE:
        summary_path = grader_repairs.ts14_summary_path()
        if summary_path.is_file():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            if not isinstance(summary, dict) or not isinstance(summary.get("cases"), list):
                raise SourceNotReady(f"native repair summary has no cases array: {summary_path}")
            seen: set[int] = set()
            for case in summary["cases"]:
                if (
                    not isinstance(case, dict) or type(case.get("pr")) is not int
                    or case["pr"] in seen
                    or not all(isinstance(case.get(side), dict) for side in ("oracle_original", "oracle_repaired"))
                ):
                    raise SourceNotReady(f"incomplete or duplicate native before/after receipt: {summary_path}")
                seen.add(case["pr"])
        base = ctx.repo_root / grader_repairs._V6_DIR
        for path in (base / "ports.csv", base / "results.csv"):
            if not path.is_file():
                continue
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle, strict=True)
                if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                    raise SourceNotReady(f"incomplete repair CSV header: {path}")
                for row in reader:
                    if None in row or any(value is None for value in row.values()):
                        raise SourceNotReady(f"incomplete repair CSV row: {path}")


def _build_source(
    bank: IssueBank,
    ctx: BankContext,
    source: str,
    snapshot: InputSnapshot,
    explicit_roots: tuple[Path, ...] | None,
) -> dict[str, Any]:
    _validate_source(ctx, source)
    work = bank.state_root / REFRESH_RELATIVE
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{source}-", dir=work) as directory:
        temporary = Path(directory)
        staged = IssueBank(ctx.repo_root, state_root=temporary / "state")
        staged.manifest_path = temporary / "manifest.json"
        _atomic_write(staged.manifest_path, _json(bank.read_manifest()))
        build_ctx = replace(ctx)
        details: dict[str, Any] = {}
        if source == seeker.SOURCE:
            live = temporary / "prod-live"
            details["verified_witness"] = _stage_seeker(ctx, snapshot.seeker_cells_roots, live)
            build_ctx = replace(ctx, prod_live=live)
        if source == grader_repairs.SOURCE:
            base = ctx.repo_root / grader_repairs._V6_DIR
            if (base / "ports.csv").is_file() != (base / "results.csv").is_file():
                raise SourceNotReady(f"repair ports/results pair is incomplete: {base}")
        manifest = staged.build(build_ctx, [source])
        output = _output(staged, source)
        if source == grader_repairs.SOURCE:
            details["repair_pairs"] = _repair_pairs(build_ctx, staged.iter_items([source]))
        if _snapshot(ctx, source, explicit_roots) != snapshot:
            raise SourceNotReady(f"{source} inputs changed during refresh; previous collection retained")
        if source == seeker.SOURCE:
            local_live = work / "prod-live"
            for name in (seeker_known_hacks.RUNS_FILE, seeker_known_hacks.LABELS_FILE):
                origin = build_ctx.prod_live / "seeker-known-hacks" / name
                destination = local_live / "seeker-known-hacks" / name
                _atomic_write(destination, origin.read_text(encoding="utf-8") if origin.is_file() else "")
            details["verified_witness"]["local_labels_jsonl"] = str(local_live / seeker.LABELS_RELATIVE)
            details["verified_witness"]["staging_jsonl"] = str(local_live / seeker.RUNS_RELATIVE)
        _publish_source(bank, staged, source, manifest)
        return {"status": "refreshed", **snapshot.receipt(), "output": output, **details}


def refresh_once(
    ctx: BankContext,
    *,
    bank: IssueBank | None = None,
    seeker_cells_roots: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Refresh only changed own-run sources; never open a production DB writer.

    An absent receipt means all sources are pending, including changes that
    predate watcher startup. Failed attempts do not become successful input
    baselines, and are retried on the next cycle without replacing bank bytes.
    """
    bank = bank if bank is not None else IssueBank(ctx.repo_root, state_root=ctx.state_root)
    explicit = None if seeker_cells_roots is None else tuple(
        sorted(path.expanduser().absolute() for path in seeker_cells_roots)
    )
    # CorpusRegistry caches parsed assignments: a tightened ledger must apply on
    # every cycle, not only when the watcher was first constructed.
    ctx = replace(ctx)
    with bank_writer_lock(bank):
        previous = _load_receipt(receipt_path(bank))
        entries: dict[str, Any] = {}
        for source in SOURCES:
            prior = previous["sources"].get(source, {})
            observed: dict[str, Any] = {}
            current = _output(bank, source)
            try:
                snapshot = _snapshot(ctx, source, explicit)
                observed = snapshot.receipt()
                missing = [
                    path for path, digest in prior.get("input_digests", {}).items()
                    if digest is not None and snapshot.digests.get(path) is None
                    and path != str(Path(str(ctx.cloud_db) + "-wal").expanduser().absolute())
                ]
                if missing:
                    raise SourceNotReady("previously published source bytes are missing: " + ", ".join(missing))
                entry = bank.read_manifest().get("sources", {}).get(source)
                if (
                    prior.get("status") in {"refreshed", "unchanged"}
                    and all(prior.get(key) == value for key, value in observed.items())
                    and prior.get("output") == current
                    and entry is not None
                    and entry.get("count") == current["count"]
                    and entry.get("sha256") == current["sha256"]
                ):
                    entries[source] = {**prior, "status": "unchanged"}
                    continue
                entries[source] = _build_source(bank, ctx, source, snapshot, explicit)
            except _SOURCE_ERRORS as error:
                entries[source] = {
                    **prior,
                    **getattr(error, "details", {}),
                    "status": "source-not-ready",
                    "observed_inputs": observed,
                    "error": f"{type(error).__name__}: {error}",
                    "output": current,
                    **_quarantine_stale_dev(bank, ctx, source, prior),
                }
        receipt = {"schema_version": SCHEMA_VERSION, "sources": entries}
        path = receipt_path(bank)
        text = _json(receipt)
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            _atomic_write(path, text)
        return receipt


def watch(
    ctx: BankContext,
    *,
    interval: float = 60.0,
    seeker_cells_roots: Iterable[Path] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield one immediate refresh and subsequent scoped cycles until interrupted."""
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("interval must be a finite positive number of seconds")
    roots = None if seeker_cells_roots is None else tuple(seeker_cells_roots)
    while True:
        yield refresh_once(ctx, seeker_cells_roots=roots)
        time.sleep(interval)
