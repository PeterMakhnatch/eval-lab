from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from evallab.evidence import parquet_io


def test_empty_table_uses_one_cached_serialization_and_matches_direct_writer(
    tmp_path: Path, monkeypatch
) -> None:
    schema = pa.schema([pa.field("id", pa.string(), nullable=False)])
    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"
    direct = tmp_path / "direct.parquet"
    parquet_io._empty_parquet_bytes.cache_clear()
    original_write = parquet_io.pq.write_table
    calls = 0

    def counted_write(*args, **kwargs) -> None:
        nonlocal calls
        calls += 1
        original_write(*args, **kwargs)

    monkeypatch.setattr(parquet_io.pq, "write_table", counted_write)
    parquet_io.write_table_atomic(first, [], schema)
    parquet_io.write_table_atomic(second, [], schema)
    original_write(
        pa.Table.from_pylist([], schema=schema),
        direct,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
    )

    assert calls == 1
    assert first.read_bytes() == second.read_bytes() == direct.read_bytes()
    assert pq.read_table(first).schema == schema
    assert pq.read_table(first).num_rows == 0
    assert not first.with_suffix(".parquet.tmp").exists()


def test_nonempty_table_remains_atomic_and_preserves_rows(tmp_path: Path) -> None:
    path = tmp_path / "rows.parquet"
    schema = pa.schema([pa.field("id", pa.string(), nullable=False)])

    parquet_io.write_table_atomic(path, [{"id": "one"}], schema)

    assert pq.read_table(path).to_pylist() == [{"id": "one"}]
    assert not path.with_suffix(".parquet.tmp").exists()
