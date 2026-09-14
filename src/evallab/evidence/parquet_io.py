"""Atomic Parquet writes shared by deterministic evidence projections."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from evallab.storage.fs import durable_mkdir, durable_replace


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


def write_table_atomic(
    path: Path,
    rows: Sequence[dict[str, Any]],
    schema: pa.Schema,
) -> None:
    """Publish one table, coordinating canonical lake destinations."""
    with parquet_publication_lock(path):
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
