"""Atomic Parquet writes and immutable snapshot reading."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from evallab.storage.fs import durable_mkdir, durable_replace

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
