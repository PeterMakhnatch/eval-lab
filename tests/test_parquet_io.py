from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

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


def _pair_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("job_name", pa.string()),
            pa.field("trial_name", pa.string()),
            pa.field("produced_at", pa.string()),
        ]
    )


def _publish_pair(
    root: Path,
    treatment: list[dict[str, str]],
    capture: list[dict[str, str]],
) -> dict[str, Path]:
    generations = root / "generations"
    number = len(list(generations.iterdir())) + 1 if generations.exists() else 1
    generation = f"{number:032x}"
    directory = generations / generation
    directory.mkdir(parents=True)
    paths = {}
    entries = []
    for name, rows in (("trial_capture.parquet", capture), ("trial_treatment.parquet", treatment)):
        path = directory / name
        parquet_io.write_table_atomic(path, rows, _pair_schema())
        paths[name] = path
        entries.append(
            {
                "name": name,
                "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                "rows": len(rows),
            }
        )
    (root / "current.json").write_text(
        json.dumps(
            {"schema": "evallab.parquet-snapshot/v1", "generation": generation, "files": entries}
        ),
        encoding="utf-8",
    )
    return paths


def test_snapshot_refuses_missing_or_hash_mismatched_files(tmp_path: Path) -> None:
    assert parquet_io.parquet_snapshot_paths(tmp_path / "absent") is None
    missing_root = tmp_path / "missing"
    missing = _publish_pair(
        missing_root,
        [{"job_name": "job", "trial_name": "t", "produced_at": "1"}],
        [{"job_name": "job", "trial_name": "t", "produced_at": "1"}],
    )
    missing["trial_capture.parquet"].unlink()
    with pytest.raises(ValueError):
        parquet_io.parquet_snapshot_paths(missing_root)

    corrupt_root = tmp_path / "corrupt"
    corrupt = _publish_pair(
        corrupt_root,
        [{"job_name": "job", "trial_name": "t", "produced_at": "1"}],
        [{"job_name": "job", "trial_name": "t", "produced_at": "1"}],
    )
    target = corrupt["trial_capture.parquet"]
    payload = bytearray(target.read_bytes())
    payload[-1] ^= 0xFF
    target.write_bytes(payload)
    with pytest.raises(ValueError):
        parquet_io.parquet_snapshot_paths(corrupt_root)


def test_snapshot_refuses_path_escape(tmp_path: Path) -> None:
    root = tmp_path / "snap"
    published = _publish_pair(
        root,
        [{"job_name": "job", "trial_name": "t", "produced_at": "1"}],
        [{"job_name": "job", "trial_name": "t", "produced_at": "1"}],
    )
    pointer = root / "current.json"
    original = pointer.read_text(encoding="utf-8")

    def refuse(mutated: dict) -> None:
        pointer.write_text(json.dumps(mutated), encoding="utf-8")
        with pytest.raises(ValueError):
            parquet_io.parquet_snapshot_paths(root)
        pointer.write_text(original, encoding="utf-8")

    escaped_name = json.loads(original)
    escaped_name["files"][0]["name"] = "../outside.parquet"
    refuse(escaped_name)
    escaped_generation = json.loads(original)
    escaped_generation["generation"] = "../0000000000000001"
    refuse(escaped_generation)
    assert parquet_io.parquet_snapshot_paths(root) == published

    outside_file = tmp_path / "outside.parquet"
    outside_file.write_bytes(published["trial_capture.parquet"].read_bytes())
    published["trial_capture.parquet"].unlink()
    published["trial_capture.parquet"].symlink_to(outside_file)
    with pytest.raises(ValueError):
        parquet_io.parquet_snapshot_paths(root)

    linked = tmp_path / "linked"
    linked_paths = _publish_pair(
        linked,
        [{"job_name": "job", "trial_name": "t", "produced_at": "1"}],
        [{"job_name": "job", "trial_name": "t", "produced_at": "1"}],
    )
    generation = linked_paths["trial_treatment.parquet"].parent
    outside_generation = tmp_path / "outside-generation"
    generation.rename(outside_generation)
    generation.symlink_to(outside_generation, target_is_directory=True)
    with pytest.raises(ValueError):
        parquet_io.parquet_snapshot_paths(linked)

    pointer_root = tmp_path / "pointer"
    _publish_pair(
        pointer_root,
        [{"job_name": "job", "trial_name": "t", "produced_at": "1"}],
        [{"job_name": "job", "trial_name": "t", "produced_at": "1"}],
    )
    current = pointer_root / "current.json"
    external_pointer = tmp_path / "external-current.json"
    external_pointer.write_bytes(current.read_bytes())
    current.unlink()
    current.symlink_to(external_pointer)
    with pytest.raises(ValueError):
        parquet_io.parquet_snapshot_paths(pointer_root)


def test_prior_generation_remains_readable_after_republish(tmp_path: Path) -> None:
    root = tmp_path / "snap"
    first_rows = [{"job_name": "job", "trial_name": "old", "produced_at": "1"}]
    second_rows = [{"job_name": "job", "trial_name": "new", "produced_at": "2"}]
    first = _publish_pair(root, first_rows, first_rows)
    second = _publish_pair(root, second_rows, second_rows)

    assert first != second
    assert first["trial_treatment.parquet"].is_file()
    assert pq.read_table(first["trial_treatment.parquet"]).to_pylist() == first_rows
    assert pq.read_table(first["trial_capture.parquet"]).to_pylist() == first_rows
    assert pq.read_table(second["trial_treatment.parquet"]).to_pylist() == second_rows
    assert parquet_io.parquet_snapshot_paths(root) == second
