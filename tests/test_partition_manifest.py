"""Partition manifest contract: empty tables are pruned but stay accounted.

Zero-row Parquet files were the only signal that a projected table had been
written at all, so completeness checks required all fifteen files per trial.
Pruning them without a manifest would report every partition as incomplete;
keeping them costs a file open per table per trial on every scan. These tests
pin the resulting contract from a consumer's point of view.
"""

from __future__ import annotations

from pathlib import Path

from evallab.evidence.atif import (
    PARTITION_MANIFEST_FILE,
    PROJECTED_TABLES,
    partition_missing_tables,
    project_jobs,
    read_partition_manifest,
)
from evallab.results import load_job

FIXTURES = Path(__file__).parent / "fixtures" / "explorer" / "jobs"


def _partitions(derived_root: Path) -> list[Path]:
    return sorted(derived_root.glob("job_id=*/trial_id=*"))


def test_projection_prunes_empty_tables_yet_partition_verifies_complete(
    tmp_path: Path,
) -> None:
    job = load_job(FIXTURES / "job-notraj")
    tables, failures = project_jobs([job], tmp_path)
    assert failures == ()

    partitions = _partitions(tmp_path)
    assert partitions, "projection produced no trial partition"

    empty_tables = {table.table for table in tables if table.rows == 0}
    assert empty_tables, "fixture no longer exercises empty tables"

    for partition in partitions:
        written = {child.name for child in partition.glob("*.parquet")}
        # Pruned: no file for a table that had no rows.
        assert not {f"{name}.parquet" for name in empty_tables} & written
        # Accounted: the manifest still records every projected table.
        manifest = read_partition_manifest(partition)
        assert manifest is not None
        assert {f"{name}.parquet" for name in manifest} == set(PROJECTED_TABLES)
        # Complete: consumers see nothing missing.
        assert partition_missing_tables(partition) == frozenset()


def test_missing_nonempty_table_is_still_reported_missing(tmp_path: Path) -> None:
    """A writer that dies mid-projection must not look like an empty table."""
    job = load_job(FIXTURES / "job-fail")
    tables, failures = project_jobs([job], tmp_path)
    assert failures == ()

    partition = _partitions(tmp_path)[0]
    populated = next(
        child
        for child in sorted(partition.glob("*.parquet"))
        if child.name in PROJECTED_TABLES
    )
    populated.unlink()

    assert partition_missing_tables(partition) == frozenset({populated.name})


def test_legacy_partition_without_manifest_requires_every_file(tmp_path: Path) -> None:
    """Partitions written before manifests keep the rule they were built under."""
    partition = tmp_path / "job_id=j" / "trial_id=t"
    partition.mkdir(parents=True)
    for name in PROJECTED_TABLES:
        (partition / name).write_bytes(b"parquet")

    assert partition_missing_tables(partition) == frozenset()

    (partition / "steps.parquet").unlink()
    assert partition_missing_tables(partition) == frozenset({"steps.parquet"})


def test_manifest_is_deterministic_across_reprojection(tmp_path: Path) -> None:
    """Byte-identical rebuilds must survive the manifest (no timestamps)."""
    job = load_job(FIXTURES / "job-notraj")
    project_jobs([job], tmp_path)
    first = {
        path: (tmp_path / path).read_bytes()
        for path in [
            p.relative_to(tmp_path) for p in tmp_path.rglob(PARTITION_MANIFEST_FILE)
        ]
    }
    assert first

    project_jobs([job], tmp_path)
    second = {path: (tmp_path / path).read_bytes() for path in first}
    assert second == first


def test_fully_pruned_table_stays_queryable_by_column(tmp_path: Path) -> None:
    """A table empty across the whole lake must query as empty, not fail to bind."""
    from evallab.storage.attach import attach

    job = load_job(FIXTURES / "job-notraj")
    project_jobs([job], tmp_path)
    assert not list(tmp_path.glob("job_id=*/trial_id=*/steps.parquet"))

    connection = attach(explicit_derived=tmp_path).connection
    try:
        assert connection.execute("SELECT count(*) FROM steps").fetchone() == (0,)
        assert connection.execute("SELECT trial_id, step_id FROM steps").fetchall() == []
    finally:
        connection.close()
