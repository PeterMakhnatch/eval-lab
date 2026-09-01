"""Atomic Parquet writes shared by deterministic evidence projections."""

from __future__ import annotations

import os
import tempfile
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


def write_table_atomic(
    path: Path,
    rows: Sequence[dict[str, Any]],
    schema: pa.Schema,
) -> None:
    """Verify a complete temporary table before atomically publishing it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
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
        actual_schema = pq.read_schema(temporary)
        if not actual_schema.equals(schema, check_metadata=False):
            raise ValueError(f"temporary Parquet schema mismatch: {path}")
        actual_rows = pq.ParquetFile(temporary).metadata.num_rows
        if actual_rows != len(rows):
            raise ValueError(
                f"temporary Parquet row count mismatch: expected={len(rows)} "
                f"actual={actual_rows} path={path}"
            )
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
