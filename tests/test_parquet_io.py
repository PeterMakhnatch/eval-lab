from __future__ import annotations

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
    schema = _pair_schema()
    return parquet_io.write_parquet_snapshot(
        root,
        {
            "trial_capture.parquet": (capture, schema),
            "trial_treatment.parquet": (treatment, schema),
        },
    )


def test_two_table_snapshot_is_one_consistent_generation(tmp_path: Path) -> None:
    root = tmp_path / "snap"
    treatment = [{"job_name": "job", "trial_name": "t", "produced_at": "1"}]
    capture = [
        {"job_name": "job", "trial_name": "t", "produced_at": "1"},
        {"job_name": "job", "trial_name": "u", "produced_at": "1"},
    ]

    published = _publish_pair(root, treatment, capture)
    resolved = parquet_io.parquet_snapshot_paths(root)
    manifest = json.loads((root / "current.json").read_text(encoding="utf-8"))

    assert resolved == published
    assert set(published) == {"trial_capture.parquet", "trial_treatment.parquet"}
    assert published["trial_capture.parquet"].parent == published["trial_treatment.parquet"].parent
    assert published["trial_treatment.parquet"].parent.name == manifest["generation"]
    assert pq.read_table(published["trial_treatment.parquet"]).to_pylist() == treatment
    assert pq.read_table(published["trial_capture.parquet"]).to_pylist() == capture
    assert {item["name"]: item["rows"] for item in manifest["files"]} == {
        "trial_capture.parquet": 2,
        "trial_treatment.parquet": 1,
    }
    assert not (root / ".current.json.tmp").exists()


def test_invalid_second_table_preserves_prior_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "snap"
    prior_rows = [{"job_name": "job", "trial_name": "old", "produced_at": "1"}]
    prior = _publish_pair(root, prior_rows, prior_rows)
    prior_bytes = {name: path.read_bytes() for name, path in prior.items()}
    pointer = (root / "current.json").read_bytes()
    schema = _pair_schema()
    invalid = pa.schema([pa.field("continuation_split", pa.bool_())])

    with pytest.raises(pa.ArrowInvalid):
        parquet_io.write_parquet_snapshot(
            root,
            {
                "alpha.parquet": (prior_rows, schema),
                "beta.parquet": ([{"continuation_split": {"used": 1}}], invalid),
            },
        )

    assert (root / "current.json").read_bytes() == pointer
    assert parquet_io.parquet_snapshot_paths(root) == prior
    assert {name: path.read_bytes() for name, path in prior.items()} == prior_bytes
    assert not any((root / "generations").glob("*.staging"))


def test_failed_first_publication_leaves_no_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "snap"
    schema = _pair_schema()
    invalid = pa.schema([pa.field("continuation_split", pa.bool_())])

    with pytest.raises(ValueError):
        parquet_io.write_parquet_snapshot(root, {})
    with pytest.raises(pa.ArrowInvalid):
        parquet_io.write_parquet_snapshot(
            root,
            {
                "alpha.parquet": (
                    [{"job_name": "j", "trial_name": "t", "produced_at": "1"}],
                    schema,
                ),
                "beta.parquet": ([{"continuation_split": {"used": 1}}], invalid),
            },
        )

    assert parquet_io.parquet_snapshot_paths(root) is None
    assert parquet_io.parquet_snapshot_paths(tmp_path / "missing") is None
    assert not (root / "current.json").exists()
    assert not (root / ".current.json.tmp").exists()
    assert not any((root / "generations").glob("*.staging"))


def test_interrupted_pointer_publication_preserves_prior_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "snap"
    prior_rows = [{"job_name": "job", "trial_name": "old", "produced_at": "1"}]
    prior = _publish_pair(root, prior_rows, prior_rows)
    prior_bytes = {name: path.read_bytes() for name, path in prior.items()}
    pointer = (root / "current.json").read_bytes()
    original = parquet_io.durable_replace

    def fail_pointer(source: Path, destination: Path) -> None:
        if destination.name == "current.json":
            raise OSError("pointer publication interrupted")
        original(source, destination)

    monkeypatch.setattr(parquet_io, "durable_replace", fail_pointer)
    new_rows = [{"job_name": "job", "trial_name": "new", "produced_at": "2"}]
    with pytest.raises(OSError):
        _publish_pair(root, new_rows, new_rows)

    resolved = parquet_io.parquet_snapshot_paths(root)
    assert resolved == prior
    assert (root / "current.json").read_bytes() == pointer
    assert {name: path.read_bytes() for name, path in prior.items()} == prior_bytes
    assert pq.read_table(resolved["trial_treatment.parquet"]).to_pylist() == prior_rows
    assert not (root / ".current.json.tmp").exists()


def test_snapshot_refuses_missing_or_hash_mismatched_files(tmp_path: Path) -> None:
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
