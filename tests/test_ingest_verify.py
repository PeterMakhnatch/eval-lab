"""Projection invariants and SQL ingest views."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import duckdb

from evallab.evidence.atif import (
    JOB_PROJECTION_FILE,
    PROJECTED_TABLES,
    PROJECTION_FAILURE_REASON,
    check_projection_invariant,
)
from evallab.queue import DirectoryQueue


def _write_complete_partition(
    derived_root: Path,
    job_id: str,
    trial_id: str,
) -> None:
    job_dir = derived_root / f"job_id={job_id}"
    trial_dir = job_dir / f"trial_id={trial_id}"
    trial_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / JOB_PROJECTION_FILE).write_bytes(b"dummy-parquet")
    for table_name in PROJECTED_TABLES:
        (trial_dir / table_name).write_bytes(b"dummy-parquet")


def test_projection_invariant_per_reason_breakdown(tmp_path: Path) -> None:
    """ProjectionInvariant must report per-reason breakdown of accounted exceptions."""
    queue = DirectoryQueue(tmp_path / "queue")
    projected_job = "00000000-0000-0000-0000-000000000001"
    excepted_job_1 = "00000000-0000-0000-0000-000000000002"
    excepted_job_2 = "00000000-0000-0000-0000-000000000003"

    _write_complete_partition(
        tmp_path / "derived",
        projected_job,
        "00000000-0000-0000-0000-000000000010",
    )

    # Record two exceptions with different reasons
    queue.append_event(
        SimpleNamespace(
            model_dump_json=lambda exclude_none=True: (
                f'{{"reason_code":"{PROJECTION_FAILURE_REASON}:{excepted_job_1}:MissingResultJson"}}'
            )
        )
    )
    queue.append_event(
        SimpleNamespace(
            model_dump_json=lambda exclude_none=True: (
                f'{{"reason_code":"{PROJECTION_FAILURE_REASON}:{excepted_job_2}:CorruptedTrajectory"}}'
            )
        )
    )

    rows = [
        (projected_job, "projected", "00000000-0000-0000-0000-000000000010"),
        (excepted_job_1, "excepted-1", "00000000-0000-0000-0000-000000000020"),
        (excepted_job_2, "excepted-2", "00000000-0000-0000-0000-000000000030"),
    ]

    invariant = check_projection_invariant(
        "postgresql://test",
        tmp_path / "derived",
        queue.events_path,
        catalog_rows_loader=lambda url: rows,
    )

    assert invariant.ok is True
    assert invariant.projected_job_ids == {projected_job}
    assert invariant.excepted_job_ids == {excepted_job_1, excepted_job_2}
    assert invariant.exceptions_by_reason == {
        "CorruptedTrajectory": frozenset({excepted_job_2}),
        "MissingResultJson": frozenset({excepted_job_1}),
    }


def test_ingest_views_duckdb_execution() -> None:
    """sql/ingest_views.sql must execute cleanly in DuckDB with fallback schemas."""
    sql_path = Path(__file__).resolve().parents[1] / "sql/ingest_views.sql"
    assert sql_path.is_file(), f"Missing SQL file: {sql_path}"

    conn = duckdb.connect(":memory:")
    conn.execute(sql_path.read_text())

    # Verify all views exist and are queryable
    reconciliation = conn.execute("SELECT * FROM v_ingest_reconciliation")
    assert [d[0] for d in reconciliation.description][:3] == ["job_id", "job_name", "trial_id"]
    assert reconciliation.fetchall() == []

    summary = conn.execute("SELECT * FROM v_ingest_summary")
    assert [d[0] for d in summary.description][:3] == ["task_name", "agent_name", "total_trials"]
    assert summary.fetchall() == []

    gaps = conn.execute("SELECT * FROM v_ingest_gaps")
    assert [d[0] for d in gaps.description][:3] == ["job_id", "job_name", "trial_id"]
    assert gaps.fetchall() == []
    completeness = conn.execute("SELECT * FROM v_ingest_completeness").fetchone()
    assert completeness is not None
    assert completeness[0] == 0  # total_catalog_trials on empty fallback


