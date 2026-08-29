"""Atomic Parquet writes shared by deterministic evidence projections."""

from __future__ import annotations

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
    """Write a complete Parquet table, preserving the existing atomic cutover."""
    path.parent.mkdir(parents=True, exist_ok=True)
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
