"""Atomic Parquet writes shared by deterministic evidence projections."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


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


@cache
def empty_table_sha256(schema: pa.Schema) -> str:
    """Digest of the canonical empty table for *schema*.

    A pruned empty table keeps the digest a materialized zero-row file would
    have had, so projection digests stay comparable across the cutover.
    """
    return hashlib.sha256(_empty_parquet_bytes(schema)).hexdigest()


def write_table_atomic(
    path: Path,
    rows: Sequence[dict[str, Any]],
    schema: pa.Schema,
    *,
    keep_empty: bool = True,
) -> bool:
    """Write a complete Parquet table, preserving the existing atomic cutover.

    With ``keep_empty=False`` an empty table writes no file and removes any
    stale one, so re-projection converges instead of leaving a former
    non-empty table behind. Returns whether a file exists at *path* after the
    call.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows and not keep_empty:
        path.unlink(missing_ok=True)
        return False
    temporary = path.with_suffix(".parquet.tmp")
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
    temporary.replace(path)
    return True
