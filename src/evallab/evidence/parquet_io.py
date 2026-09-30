"""Atomic Parquet writes and immutable snapshot publication."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import shutil
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import Any
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from evallab.storage.fs import durable_mkdir, durable_replace, fsync_directory

_GENERATION_RE = re.compile(r"^[0-9a-f]{32}$")
_SNAPSHOT_BASENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.parquet$")
_SNAPSHOT_POINTER = "current.json"
_SNAPSHOT_SCHEMA = "evallab.parquet-snapshot/v1"


@contextmanager
def parquet_root_lock(root: Path, *, exclusive: bool = True) -> Iterator[None]:
    """Exclude projection publication and compaction under one local lake root.

    The stable lock file is outside job directories and must never be unlinked.
    All writers must participate; this is advisory exclusion, not a filesystem
    transaction or protection against arbitrary external edits. Acquisition is
    not reentrant: already-excluded compaction uses its private staging writer.
    """
    durable_mkdir(root)
    with (root / ".parquet.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _publication_root(path: Path) -> Path | None:
    resolved = path.resolve()
    for parent in resolved.parents:
        if parent.name.startswith("job_id=") or parent.name == "compact":
            return parent.parent
    return None


@contextmanager
def parquet_publication_lock(path: Path, *, exclusive: bool = True) -> Iterator[None]:
    """Coordinate hot/cold lake writes without adding files to standalone bundles."""
    root = _publication_root(path)
    if root is None:
        yield
    else:
        with parquet_root_lock(root, exclusive=exclusive):
            yield


def write_parquet_bytes_atomic(path: Path, payload: bytes) -> None:
    """Publish serialized bytes, coordinating canonical lake destinations."""
    with parquet_publication_lock(path):
        durable_mkdir(path.parent)
        temporary = path.with_suffix(".parquet.tmp")
        try:
            temporary.write_bytes(payload)
            durable_replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


@cache
def _empty_parquet_bytes(schema: pa.Schema) -> bytes:
    """Serialize each schema's required empty table once per process."""
    sink = pa.BufferOutputStream()
    pq.write_table(
        pa.Table.from_pylist([], schema=schema),
        sink,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
    )
    return sink.getvalue().to_pybytes()


def _write_table_file(
    path: Path,
    rows: Sequence[dict[str, Any]],
    schema: pa.Schema,
) -> None:
    """Serialize one table without taking the publication lock."""
    durable_mkdir(path.parent)
    temporary = path.with_suffix(".parquet.tmp")
    try:
        if rows:
            pq.write_table(
                pa.Table.from_pylist(rows, schema=schema),
                temporary,
                compression="zstd",
                use_dictionary=False,
                write_statistics=True,
            )
        else:
            temporary.write_bytes(_empty_parquet_bytes(schema))
        durable_replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_table_atomic(
    path: Path,
    rows: Sequence[dict[str, Any]],
    schema: pa.Schema,
) -> None:
    """Publish one table, coordinating canonical lake destinations."""
    with parquet_publication_lock(path):
        _write_table_file(path, rows, schema)


def _snapshot_basename(name: object) -> str:
    if not isinstance(name, str) or _SNAPSHOT_BASENAME_RE.fullmatch(name) is None:
        raise ValueError(f"invalid snapshot table name: {name!r}")
    return name


def _validated_snapshot_tables(
    tables: Mapping[str, tuple[Sequence[dict[str, Any]], pa.Schema]],
) -> list[tuple[str, Sequence[dict[str, Any]], pa.Schema]]:
    if not isinstance(tables, Mapping) or not tables:
        raise ValueError("snapshot tables must be a non-empty mapping")
    specs: list[tuple[str, Sequence[dict[str, Any]], pa.Schema]] = []
    seen: set[str] = set()
    for name, value in tables.items():
        filename = _snapshot_basename(name)
        folded = filename.casefold()
        if folded in seen:
            raise ValueError(f"snapshot table names collide: {filename}")
        seen.add(folded)
        if not isinstance(value, tuple) or len(value) != 2:
            raise ValueError(f"snapshot table {filename} must be (rows, schema)")
        rows, schema = value
        if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
            raise ValueError(f"snapshot rows must be a sequence of dicts: {filename}")
        if not isinstance(schema, pa.Schema):
            raise ValueError(f"snapshot schema must be a pyarrow schema: {filename}")
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"snapshot rows must be dicts: {filename}")
        specs.append((filename, rows, schema))
    specs.sort(key=lambda item: item[0])
    return specs


def _snapshot_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _contained_snapshot_file(root: Path, generation: str, name: str) -> Path:
    filename = _snapshot_basename(name)
    if not isinstance(generation, str) or _GENERATION_RE.fullmatch(generation) is None:
        raise ValueError("snapshot generation is malformed")
    root_resolved = root.resolve()
    relative = ("generations", generation, filename)
    current = root_resolved
    for part in relative:
        current = current / part
        if current.is_symlink():
            raise ValueError("snapshot path must not traverse a symlink")
    resolved = current.resolve()
    if resolved != root_resolved.joinpath(*relative):
        raise ValueError("snapshot path escapes snapshot root")
    return resolved


def _discard_staging(path: Path) -> None:
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink(missing_ok=True)


def _publish_snapshot_pointer(
    root: Path,
    generation: str,
    entries: Sequence[Mapping[str, Any]],
) -> None:
    payload = {
        "schema": _SNAPSHOT_SCHEMA,
        "generation": generation,
        "files": [dict(entry) for entry in entries],
    }
    temporary = root / ".current.json.tmp"
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        temporary.write_bytes(encoded)
        durable_replace(temporary, root / _SNAPSHOT_POINTER)
    finally:
        temporary.unlink(missing_ok=True)


def write_parquet_snapshot(
    root: Path,
    tables: Mapping[str, tuple[Sequence[dict[str, Any]], pa.Schema]],
) -> dict[str, Path]:
    """Publish one immutable generation, then atomically replace ``current.json``.

    The caller holds ``parquet_root_lock`` for the catalog. This function does
    not take that lock. A failure before the pointer is replaced leaves the
    previous snapshot readable, or leaves no snapshot when none was published.
    Older generations are not deleted.
    """
    specs = _validated_snapshot_tables(tables)
    durable_mkdir(root)
    generation = uuid4().hex
    generations = root / "generations"
    staging = generations / f"{generation}.staging"
    final = generations / generation
    if staging.exists() or final.exists():
        raise ValueError(f"snapshot generation already exists: {generation}")
    for name, _, _ in specs:
        _contained_snapshot_file(root, generation, name)
    try:
        durable_mkdir(staging)
        for name, rows, schema in specs:
            _write_table_file(staging / name, rows, schema)
        fsync_directory(staging)
        staging.rename(final)
        fsync_directory(generations)
        entries = [
            {
                "name": name,
                "sha256": _snapshot_sha256(final / name),
                "rows": pq.read_metadata(final / name).num_rows,
            }
            for name, _, _ in specs
        ]
        entries.sort(key=lambda item: item["name"])
        _publish_snapshot_pointer(root, generation, entries)
    except Exception:
        _discard_staging(staging)
        raise
    return {
        entry["name"]: _contained_snapshot_file(root, generation, entry["name"])
        for entry in entries
    }


def _load_snapshot_manifest(pointer: Path) -> dict[str, Any]:
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("snapshot pointer is malformed") from exc
    if not isinstance(payload, dict) or set(payload) != {"files", "generation", "schema"}:
        raise ValueError("snapshot pointer is malformed")
    if payload["schema"] != _SNAPSHOT_SCHEMA:
        raise ValueError("snapshot pointer schema is unsupported")
    generation = payload["generation"]
    if not isinstance(generation, str) or _GENERATION_RE.fullmatch(generation) is None:
        raise ValueError("snapshot generation is malformed")
    if not isinstance(payload["files"], list) or not payload["files"]:
        raise ValueError("snapshot pointer has no files")
    return payload


def _manifest_entries(files: Sequence[Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"name", "rows", "sha256"}:
            raise ValueError("snapshot file entry is malformed")
        name = _snapshot_basename(item["name"])
        folded = name.casefold()
        if folded in seen:
            raise ValueError(f"duplicate snapshot file: {name}")
        seen.add(folded)
        digest = item["sha256"]
        rows = item["rows"]
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise ValueError(f"snapshot digest is malformed: {name}")
        hexdigest = digest[7:]
        if len(hexdigest) != 64 or any(char not in "0123456789abcdef" for char in hexdigest):
            raise ValueError(f"snapshot digest is malformed: {name}")
        if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
            raise ValueError(f"snapshot row count is malformed: {name}")
        entries.append({"name": name, "sha256": digest, "rows": rows})
    return entries


def _snapshot_row_count(path: Path, name: str) -> int:
    try:
        return pq.read_metadata(path).num_rows
    except (OSError, pa.ArrowException) as exc:
        raise ValueError(f"snapshot file is not readable parquet: {name}") from exc


def parquet_snapshot_paths(root: Path) -> dict[str, Path] | None:
    """Resolve one current snapshot, or ``None`` when no pointer exists.

    A malformed pointer, missing file, symlink, path escape, or hash mismatch
    raises ``ValueError``. There is no fallback to another generation.
    """
    pointer = root / _SNAPSHOT_POINTER
    if pointer.is_symlink():
        raise ValueError("snapshot pointer must not be a symlink")
    if not pointer.exists():
        return None
    if not pointer.is_file():
        raise ValueError("snapshot pointer is not a file")
    manifest = _load_snapshot_manifest(pointer)
    generation = str(manifest["generation"])
    resolved: dict[str, Path] = {}
    for entry in _manifest_entries(manifest["files"]):
        path = _contained_snapshot_file(root, generation, entry["name"])
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"snapshot file missing: {entry['name']}")
        try:
            actual = _snapshot_sha256(path)
        except OSError as exc:
            raise ValueError(f"snapshot file unreadable: {entry['name']}") from exc
        if actual != entry["sha256"]:
            raise ValueError(f"snapshot hash mismatch: {entry['name']}")
        if _snapshot_row_count(path, entry["name"]) != entry["rows"]:
            raise ValueError(f"snapshot row count mismatch: {entry['name']}")
        resolved[entry["name"]] = path
    return resolved
