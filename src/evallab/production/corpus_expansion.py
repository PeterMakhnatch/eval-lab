"""Import explicitly cleared, locally staged task bundles into a held SQLite store.

The identity-only ``evallab.corpus_import_pack/v1`` document contains ``objects``
with exactly ``source_dataset``, ``source_revision``, ``native_id``, ``path``, and
``sha256``. Paths are relative to the pack, never URLs or parent traversals.
Every selected identity must pass independently pinned ``operator_raw`` clearance
before any bundle is opened or hashed. The default dry run reads metadata only;
its success is not evidence that a bundle, its digest, or a database is valid.

Each bundle has exactly ``task`` and ``traces``; each trace entry has ``trace``
and ``steps``. Rows use the CloudTraceDatabase contract. Tasks must already be
``held`` and match their cleared canonical dataset/task identity. Model IDs must
be explicitly ``source-reported:<id>``. Trace receipts must have a provisional or
unverified status, and neither exploit nor task-defect confirmation may be set.
Publisher labels remain uninterpreted metadata; no issues are created.

All supported row values, source receipts, and steps are retained without text
clipping or base64 rewriting. Oversized files, unknown fields, duplicate records,
and every CAS reference are rejected rather than partially imported. CAS copying
is deliberately unsupported. Source locators in metadata are preserved but never
followed. The selected batch is inserted, never upserted, in one local transaction.
The receipt destination is exclusively reserved before any database write and is
written once; an existing receipt is never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from evallab.production import cloud_trace_db as cloud
from evallab.production.corpus_clearance import (
    ClearanceDenied,
    ClearanceManifest,
    ClearanceRecord,
)

PACK_SCHEMA = "evallab.corpus_import_pack/v1"
SCHEMA = "evallab.corpus_import_receipt/v1"
HELD_SPLIT = "held"
MAX_SPEC_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_BATCH_BYTES = 256 * 1024 * 1024
MAX_OBJECTS = 1000
_DIGEST = re.compile(r"[0-9a-fA-F]{64}\Z")
_UNVERIFIED_STATUSES = frozenset({"provisional", "unverified", "unverified_public_archive"})
_CAS_FIELDS = frozenset({"cas", "cas_uri", "cas_refs", "cas_references", "cas_index"})
_CONFIRMATION_FIELDS = frozenset({"confirmed", "verified", "is_exploit", "is_task_defect_fail"})


@dataclass(frozen=True)
class ImportObject:
    source_dataset: str
    source_revision: str
    native_id: str
    path: str
    sha256: str

    def identity(self) -> dict[str, str]:
        return {
            "source_dataset": self.source_dataset,
            "source_revision": self.source_revision,
            "native_id": self.native_id,
        }


@dataclass(frozen=True)
class ImportPack:
    path: Path
    objects: tuple[ImportObject, ...]


@dataclass(frozen=True)
class PreparedBundle:
    task: dict[str, Any]
    traces: tuple[tuple[dict[str, Any], list[dict[str, Any]]], ...]


def _local_path(value: str | Path, label: str) -> Path:
    text = str(value)
    if (
        not text.strip()
        or "\x00" in text
        or ":" in text
        or text.startswith("//")
    ):
        raise ValueError(f"{label} must be an explicit local filesystem path, not a URL")
    # abspath is lexical: a dry run must not stat or open the database or bundles.
    return Path(os.path.abspath(text))


def _read_descriptor(descriptor: int, *, limit: int) -> bytes:
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("input must be a regular local file")
        if info.st_size > limit:
            raise ValueError("input exceeds the bounded local-file byte limit")
        raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("input exceeds the bounded local-file byte limit")
        return raw


def _read_regular(path: Path, *, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    return _read_descriptor(descriptor, limit=limit)


def _load_clearance(path: Path, *, expected_sha256: str) -> ClearanceManifest:
    # The shared API takes a path, not bytes. Feed it an exact bounded snapshot
    # so a FIFO, oversized file, or changing input cannot bypass this front door.
    raw = _read_regular(path, limit=MAX_SPEC_BYTES)
    with tempfile.NamedTemporaryFile(prefix="evallab-clearance-", suffix=".json") as snapshot:
        snapshot.write(raw)
        snapshot.flush()
        return ClearanceManifest.load(snapshot.name, expected_sha256=expected_sha256)


def _read_bundle(root: Path, relative_path: str, *, limit: int) -> bytes:
    """Open only the listed file, refusing symlinks at every staged component."""
    parts = PurePosixPath(relative_path).parts
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in parts[:-1]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory,
            )
            os.close(directory)
            directory = child
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory,
        )
        return _read_descriptor(descriptor, limit=limit)
    finally:
        os.close(directory)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON member")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError("nonstandard JSON number")


def _json(raw: bytes) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("input must be bounded, strict UTF-8 JSON") from error


def _exact_fields(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.keys() != fields:
        raise ValueError(f"{label} must have exactly its documented fields")
    return value


def _identity_text(value: Any, label: str) -> str:
    cloud._text(value, label)
    if value != value.strip() or any(ord(character) < 32 for character in value):
        raise ValueError(f"{label} must be an unpadded identity without control characters")
    return value


def _load_pack(path: Path, raw: bytes) -> ImportPack:
    document = _exact_fields(_json(raw), {"schema", "objects"}, "import pack")
    if document["schema"] != PACK_SCHEMA:
        raise ValueError("unsupported import-pack schema")
    objects = document["objects"]
    if not isinstance(objects, list) or not 1 <= len(objects) <= MAX_OBJECTS:
        raise ValueError(f"import pack must select between 1 and {MAX_OBJECTS} objects")
    selected = []
    identities: set[tuple[str, ...]] = set()
    paths: set[str] = set()
    for value in objects:
        row = _exact_fields(
            value, {"source_dataset", "source_revision", "native_id", "path", "sha256"},
            "import object",
        )
        identity = {
            field: _identity_text(row[field], field)
            for field in ("source_dataset", "source_revision", "native_id")
        }
        key = tuple(value.casefold() for value in identity.values())
        if key in identities:
            raise ValueError("duplicate normalized source identity in import pack")
        identities.add(key)
        relative_path = _identity_text(row["path"], "object path")
        parts = relative_path.split("/")
        if (
            any(part in {"", ".", ".."} for part in parts)
            or ":" in relative_path
            or "\\" in relative_path
        ):
            raise ValueError("object path must be a normalized relative local path")
        if relative_path.casefold() in paths:
            raise ValueError("duplicate object path in import pack")
        paths.add(relative_path.casefold())
        digest = row["sha256"]
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ValueError("object sha256 must be a hexadecimal SHA-256 digest")
        selected.append(ImportObject(**identity, path=relative_path, sha256=digest.lower()))
    return ImportPack(path, tuple(selected))


def _reject_cas(value: Any) -> None:
    """Reject CAS references everywhere, including nested source/step metadata."""
    if isinstance(value, str):
        if "cas://" in value.casefold():
            raise ValueError("CAS references are unsupported; no referenced content was read")
    elif isinstance(value, dict):
        for key, item in value.items():
            field = key.casefold()
            if (
                field in _CAS_FIELDS or field.endswith(("_cas_uri", "_cas_ref", "_cas_refs"))
            ) and item is not None:
                raise ValueError("CAS references are unsupported; no referenced content was read")
            _reject_cas(item)
    elif isinstance(value, list):
        for item in value:
            _reject_cas(item)


def _row(
    value: Any, columns: Sequence[str], defaults: Mapping[str, Any], label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return cloud._record(value, columns, defaults)


def _optional_text(row: Mapping[str, Any], fields: Sequence[str]) -> None:
    for field in fields:
        if row[field] is not None and not isinstance(row[field], str):
            raise ValueError(f"{field} must be a string or null")


def _json_column(value: Any) -> Any:
    # The cloud contract also accepts encoded JSON. Decode it strictly here so
    # duplicate statuses or source references cannot disappear during ingestion.
    if isinstance(value, str):
        value = _json(value.encode("utf-8"))
    return cloud._json_value(value)


def _unverified_receipt(value: Any) -> dict[str, Any]:
    receipt = _json_column(value)
    if (
        not isinstance(receipt, dict)
        or not isinstance(receipt.get("status"), str)
        or receipt["status"] not in _UNVERIFIED_STATUSES
    ):
        raise ValueError("trace receipt must explicitly be provisional or unverified")
    if any(receipt.get(field) not in (None, False) for field in _CONFIRMATION_FIELDS):
        raise ValueError("trace receipt cannot assert verification or defect confirmation")
    _reject_cas(receipt)
    return receipt


def _prepare_bundle(document: Any, clearance: ClearanceRecord) -> PreparedBundle:
    bundle = _exact_fields(document, {"task", "traces"}, "task bundle")
    _reject_cas(bundle)
    task = _row(bundle["task"], cloud._TASK_COLUMNS, {
        "original_grader_digest": None, "repaired_grader_digest": None,
        "cas_uri": None, "metadata_json": {},
    }, "task")
    for field in ("task_id", "dataset_id", "split"):
        _identity_text(task[field], field)
    canonical = clearance.canonical_identity
    if canonical is None or (
        task["task_id"] != canonical.task_id or task["dataset_id"] != canonical.dataset_id
    ):
        raise ValueError("task does not match its cleared canonical identity")
    if task["split"] != HELD_SPLIT:
        raise ValueError("staged tasks must be held; source or database DEV labels grant nothing")
    if not isinstance(task["instruction_text"], str):
        raise ValueError("instruction_text must be a string")
    _optional_text(task, ("original_grader_digest", "repaired_grader_digest"))
    task["metadata_json"] = _json_column(task["metadata_json"])
    _reject_cas(task["metadata_json"])
    trace_entries = bundle["traces"]
    if not isinstance(trace_entries, list):
        raise ValueError("traces must be a list")
    traces = []
    for entry in trace_entries:
        entry = _exact_fields(entry, {"trace", "steps"}, "trace bundle")
        steps = entry["steps"]
        if not isinstance(steps, list):
            raise ValueError("steps must be a list")
        trace = _row(entry["trace"], cloud._TRACE_COLUMNS, {
            "outcome": "unknown", "reward": None, "is_exploit": False,
            "is_task_defect_fail": False, "step_count": len(steps), "raw_cas_uri": None,
            "verified_receipt_json": None,
        }, "trace")
        for field in ("trace_id", "run_id", "task_id", "model_id", "outcome"):
            _identity_text(trace[field], field)
        if trace["task_id"] != task["task_id"]:
            raise ValueError("trace task_id does not match its cleared parent task")
        prefix, separator, model = trace["model_id"].partition(":")
        if (
            prefix != "source-reported" or not separator or not model.strip()
            or model != model.strip()
            or model.casefold() in {"unknown", "unspecified", "unavailable", "none", "null", "n/a"}
        ):
            raise ValueError("model_id must identify an explicit source-reported model")
        for field in ("is_exploit", "is_task_defect_fail"):
            if trace[field] is not False:
                raise ValueError("publisher labels cannot set exploit or task-defect confirmation")
        reward = trace["reward"]
        if reward is not None and (
            isinstance(reward, bool) or not isinstance(reward, (int, float))
            or not math.isfinite(reward) or float(reward) != reward
        ):
            raise ValueError("reward must be a losslessly representable finite number or null")
        trace["verified_receipt_json"] = _unverified_receipt(trace["verified_receipt_json"])
        if cloud._integer(trace["step_count"], "step_count") != len(steps):
            raise ValueError("step_count does not match the complete supplied step set")
        prepared_steps = []
        indices: set[int] = set()
        for value in steps:
            step = _row(value, cloud._STEP_COLUMNS, {
                "trace_id": trace["trace_id"], "tool_name": None, "command_or_action": None,
                "content_text": None, "observation_preview": None,
                "observation_cas_uri": None, "flags_json": {},
            }, "step")
            if step["trace_id"] != trace["trace_id"]:
                raise ValueError("step trace_id does not match its parent trace")
            index = cloud._integer(step["step_index"], "step_index")
            if index in indices:
                raise ValueError("duplicate step_index in trace")
            indices.add(index)
            _identity_text(step["role"], "role")
            _optional_text(step, (
                "tool_name", "command_or_action", "content_text", "observation_preview",
            ))
            step["flags_json"] = _json_column(step["flags_json"])
            _reject_cas(step["flags_json"])
            prepared_steps.append(step)
        traces.append((trace, prepared_steps))
    return PreparedBundle(task, tuple(traces))


def _counts(database: cloud.CloudTraceDatabase) -> dict[str, int]:
    return {
        table: database._execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in cloud._SEARCH_COLUMNS
    }


def _insert(database: cloud.CloudTraceDatabase, table: str, row: Mapping[str, Any]) -> None:
    # Table and column names come exclusively from the validated cloud row contract.
    values = [
        cloud._json_text(value) if field in cloud._JSON_COLUMNS and value is not None else value
        for field, value in row.items()
    ]
    database._execute(
        f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
        values,
    )


def _insert_batch(
    path: Path, bundles: Sequence[PreparedBundle],
) -> tuple[dict[str, int], dict[str, int]]:
    with cloud.CloudTraceDatabase(path) as database, database._transaction(write=True):
        before = _counts(database)
        # Check the whole batch before inserting anything; INSERT also guards races.
        for bundle in bundles:
            if database._execute(
                "SELECT 1 FROM corpus_tasks WHERE task_id = ?", (bundle.task["task_id"],),
            ).fetchone() is not None:
                raise ValueError("existing task rows cannot be imported or overwritten")
            for trace, _ in bundle.traces:
                if database._execute(
                    "SELECT 1 FROM agent_traces WHERE trace_id = ?", (trace["trace_id"],),
                ).fetchone() is not None:
                    raise ValueError("existing trace rows cannot be imported or overwritten")
        for bundle in bundles:
            _insert(database, "corpus_tasks", bundle.task)
            for trace, steps in bundle.traces:
                _insert(database, "agent_traces", trace)
                for step in steps:
                    _insert(database, "trace_steps", step)
        after = _counts(database)
    return before, after


def _check_database_target(path: Path, inputs: Sequence[Path]) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("database must be a regular local staged SQLite file, not a symlink")
    if any(os.path.samefile(path, source) for source in inputs):
        raise ValueError("database must not alias a source file or the receipt")


def import_pack(
    *,
    database: str | Path,
    pack: str | Path,
    clearance: str | Path,
    clearance_sha256: str,
    output: str | Path,
    execute: bool = False,
) -> dict[str, Any]:
    """Write an immutable metadata receipt; only explicit execution reads bodies.

    Rejected/denied selections return a receipt with no imported rows. An unusable
    or existing receipt destination raises before source or database access.
    Database/receipt filesystem failures are surfaced, never retried or overwritten.
    """
    if not isinstance(execute, bool):
        raise ValueError("execute must be an explicit boolean")
    database_path = _local_path(database, "database")
    pack_path = _local_path(pack, "pack")
    clearance_path = _local_path(clearance, "clearance")
    output_path = _local_path(output, "output")
    if output_path in {database_path, pack_path, clearance_path}:
        raise ValueError("output must be distinct from the database and metadata inputs")
    descriptor = os.open(
        output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        receipt: dict[str, Any] = {
            "schema": SCHEMA,
            "mode": "execute" if execute else "dry_run",
            "status": "rejected",
            "database": str(database_path),
            "pack": str(pack_path),
            "clearance": str(clearance_path),
            "pack_sha256": None,
            "clearance_sha256": None,
            "expected_clearance_sha256": clearance_sha256,
            "importer_sha256": None,
            "selected": 0,
            "cleared": 0,
            "denied": 0,
            "objects": [],
            "body_validation": "not_performed",
            "bundle_bytes_hashed": 0,
            "row_counts_before": None,
            "row_counts_after": None,
            "row_counts_added": dict.fromkeys(cloud._SEARCH_COLUMNS, 0),
            "admissions": dict.fromkeys((
                "operator_raw", "actor_retrieval", "scorer", "derivatives", "training",
                "redistribution", "cloud",
            ), 0),
        }
        try:
            receipt["importer_sha256"] = hashlib.sha256(
                _read_regular(Path(__file__), limit=MAX_SPEC_BYTES),
            ).hexdigest()
            raw = _read_regular(pack_path, limit=MAX_SPEC_BYTES)
            receipt["pack_sha256"] = hashlib.sha256(raw).hexdigest()
            selected_pack = _load_pack(pack_path, raw)
            receipt["selected"] = len(selected_pack.objects)
            manifest = _load_clearance(clearance_path, expected_sha256=clearance_sha256)
            receipt["clearance_sha256"] = manifest.sha256
            selected: list[tuple[ImportObject, ClearanceRecord]] = []
            canonical_tasks: set[str] = set()
            for item in selected_pack.objects:
                identity = item.identity()
                entry: dict[str, Any] = {
                    **identity, "path": item.path, "sha256": item.sha256,
                }
                try:
                    record = manifest.require(**identity, use="operator_raw")
                    canonical = record.canonical_identity
                    if canonical is None:
                        raise ClearanceDenied("canonical identity authority is missing")
                    if canonical.task_id.casefold() in canonical_tasks:
                        raise ClearanceDenied("selection repeats a canonical task through an alias")
                    canonical_tasks.add(canonical.task_id.casefold())
                    entry.update(
                        decision="allow",
                        canonical_identity={
                            "dataset_id": canonical.dataset_id, "item_id": canonical.item_id,
                            "task_id": canonical.task_id,
                        },
                        family_id=record.family_id,
                    )
                    selected.append((item, record))
                except ClearanceDenied as error:
                    entry.update(decision="deny", reason=str(error))
                    receipt["denied"] += 1
                receipt["objects"].append(entry)
            receipt["cleared"] = len(selected)
            if receipt["denied"]:
                receipt["status"] = "denied"
            elif not execute:
                receipt["status"] = "dry_run"
            else:
                receipt["body_validation"] = "failed"
                bundles = []
                trace_ids: set[str] = set()
                remaining = MAX_BATCH_BYTES
                for item, record in selected:
                    raw = _read_bundle(
                        selected_pack.path.parent, item.path,
                        limit=min(MAX_BUNDLE_BYTES, remaining),
                    )
                    receipt["bundle_bytes_hashed"] += len(raw)
                    remaining -= len(raw)
                    if hashlib.sha256(raw).hexdigest() != item.sha256:
                        raise ValueError("bundle SHA-256 does not match its listed identity object")
                    bundle = _prepare_bundle(_json(raw), record)
                    for trace, _ in bundle.traces:
                        if trace["trace_id"] in trace_ids:
                            raise ValueError("duplicate trace_id in selected bundles")
                        trace_ids.add(trace["trace_id"])
                    bundles.append(bundle)
                receipt["body_validation"] = "passed"
                _check_database_target(database_path, [
                    pack_path, clearance_path, output_path,
                    *(selected_pack.path.parent / item.path for item, _ in selected),
                ])
                before, after = _insert_batch(database_path, bundles)
                receipt.update(
                    status="imported", row_counts_before=before, row_counts_after=after,
                    row_counts_added={key: after[key] - before[key] for key in before},
                )
                receipt["admissions"]["operator_raw"] = len(bundles)
        except ClearanceDenied as error:
            receipt["status"] = "denied"
            receipt["denied"] = receipt["selected"]
            receipt["error"] = str(error)
        except (OSError, ValueError, OverflowError, RecursionError, sqlite3.Error) as error:
            receipt["status"] = "rejected"
            receipt["error"] = str(error)
        json.dump(receipt, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        os.fchmod(handle.fileno(), 0o444)
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", required=True, help="explicit local staged SQLite path")
    parser.add_argument("--pack", required=True, help="identity-only local import-pack JSON")
    parser.add_argument("--clearance", required=True, help="identity-only clearance manifest")
    parser.add_argument("--clearance-sha256", required=True, help="externally pinned manifest SHA-256")
    parser.add_argument("--output", required=True, help="new immutable receipt path; must not exist")
    parser.add_argument(
        "--execute", action="store_true", help="read validated bundles and insert held rows",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        receipt = import_pack(
            database=arguments.database, pack=arguments.pack, clearance=arguments.clearance,
            clearance_sha256=arguments.clearance_sha256, output=arguments.output,
            execute=arguments.execute,
        )
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps({
        "status": receipt["status"], "selected": receipt["selected"], "denied": receipt["denied"],
        "row_counts_added": receipt["row_counts_added"],
    }, sort_keys=True))
    return 0 if receipt["status"] in {"dry_run", "imported"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
