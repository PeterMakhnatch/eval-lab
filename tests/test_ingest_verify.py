"""Tests for ingest completeness verification, gap reconciliation, and invariants (M029)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from evallab.atif import (
    JOB_PROJECTION_FILE,
    PROJECTED_TABLES,
    PROJECTION_FAILURE_REASON,
    check_projection_invariant,
)
from evallab.ingest_verify import (
    scan_disk_trials,
    verify_ingest,
)
from evallab.queue import DirectoryQueue


def _write_complete_partition(derived_root: Path, job_id: str, trial_id: str) -> None:
    job_dir = derived_root / f"job_id={job_id}"
    trial_dir = job_dir / f"trial_id={trial_id}"
    trial_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / JOB_PROJECTION_FILE).write_bytes(b"dummy-parquet")
    for table_name in PROJECTED_TABLES:
        (trial_dir / table_name).write_bytes(b"dummy-parquet")


def test_ingest_verify_detects_unaccounted_missing_partition(tmp_path: Path) -> None:
    """Completeness invariant: an unaccounted missing parquet partition MUST fail verification."""
    derived_root = tmp_path / "derived"
    job_id = "00000000-0000-0000-0000-000000000001"
    trial_id = "00000000-0000-0000-0000-000000000002"

    # Create complete partition first
    _write_complete_partition(derived_root, job_id, trial_id)

    def mock_catalog(url: str) -> tuple[dict, dict]:
        jobs = {job_id: {"id": job_id, "name": "mock-job", "path": "runs/mock-job"}}
        trials = {
            trial_id: {
                "id": trial_id,
                "job_id": job_id,
                "name": "mock-trial",
                "path": "runs/mock-job/mock-trial",
            }
        }
        return jobs, trials

    # Ingest verification with complete partition
    res_complete = verify_ingest(
        tmp_path,
        database_url="postgresql://mock",
        derived_root=derived_root,
        events_path=tmp_path / "events.jsonl",
        catalog_loader=mock_catalog,
    )
    assert res_complete.is_complete is True
    assert len(res_complete.gaps) == 0

    # Now remove one required table to simulate silent data loss
    (derived_root / f"job_id={job_id}" / f"trial_id={trial_id}" / "trial_facts.parquet").unlink()

    # Ingest verification must catch the gap
    res_broken = verify_ingest(
        tmp_path,
        database_url="postgresql://mock",
        derived_root=derived_root,
        events_path=tmp_path / "events.jsonl",
        catalog_loader=mock_catalog,
    )
    assert res_broken.is_complete is False
    assert len(res_broken.gaps) == 1
    gap = res_broken.gaps[0]
    assert gap.store == "parquet"
    assert gap.entity_type == "trial"
    assert gap.entity_id == trial_id
    assert gap.reason == "incomplete_parquet_partition"


def test_scan_disk_trials_categorizes_unprojectable_runs(tmp_path: Path) -> None:
    """Unprojectable trial directories without result.json must be accounted with exact reasons."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True)

    # 1. Valid projectable trial
    valid_job = runs_dir / "valid-job"
    valid_trial = valid_job / "valid-trial__abc123"
    valid_trial.mkdir(parents=True)
    (valid_job / "result.json").write_text(json.dumps({"id": "j1"}))
    (valid_trial / "result.json").write_text(json.dumps({"id": "t1"}))
    (valid_trial / "config.json").write_text("{}")
    (valid_trial / "lock.json").write_text("{}")

    # 2. Crashed trial (has lock and log, but no result.json)
    crashed_job = runs_dir / "crashed-job"
    crashed_trial = crashed_job / "crashed-trial__xyz789"
    crashed_trial.mkdir(parents=True)
    (crashed_job / "result.json").write_text(json.dumps({"id": "j2"}))
    (crashed_trial / "lock.json").write_text("{}")
    (crashed_trial / "trial.log").write_text("Docker inspect failed")

    # 3. Empty trial directory
    empty_job = runs_dir / "empty-job"
    empty_trial = empty_job / "empty-trial__000000"
    empty_trial.mkdir(parents=True)
    (empty_job / "result.json").write_text(json.dumps({"id": "j3"}))

    projectable, unprojectable = scan_disk_trials(tmp_path, search_roots=[runs_dir])

    assert valid_trial in projectable
    assert len(projectable) == 1

    unprojectable_by_name = {u.trial_name: u for u in unprojectable}
    assert "crashed-trial__xyz789" in unprojectable_by_name
    assert unprojectable_by_name["crashed-trial__xyz789"].reason == "crashed_execution"

    assert "empty-trial__000000" in unprojectable_by_name
    assert unprojectable_by_name["empty-trial__000000"].reason == "empty_trial_dir"


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
    expected_detail = (
        "catalog=3 projected=1 exceptions=2 "
        "(CorruptedTrajectory=1, MissingResultJson=1) missing=0 extra=0"
    )
    assert invariant.detail == expected_detail


def test_ingest_views_duckdb_execution() -> None:
    """sql/ingest_views.sql must execute cleanly in DuckDB with fallback schemas."""
    sql_path = Path(__file__).resolve().parents[1] / "sql/ingest_views.sql"
    assert sql_path.is_file(), f"Missing SQL file: {sql_path}"

    conn = duckdb.connect(":memory:")
    conn.execute(sql_path.read_text())

    # Verify all views exist and are queryable
    reconciliation = conn.execute("SELECT * FROM v_ingest_reconciliation").fetchall()
    assert isinstance(reconciliation, list)

    summary = conn.execute("SELECT * FROM v_ingest_summary").fetchall()
    assert isinstance(summary, list)

    gaps = conn.execute("SELECT * FROM v_ingest_gaps").fetchall()
    assert isinstance(gaps, list)

    completeness = conn.execute("SELECT * FROM v_ingest_completeness").fetchone()
    assert completeness is not None
    assert completeness[0] == 0  # total_catalog_trials on empty fallback


def test_ingest_verify_accounts_for_exception_in_gaps(tmp_path: Path) -> None:
    """A cataloged job missing parquet with a recorded exception must not be an active gap."""
    queue = DirectoryQueue(tmp_path / "queue")
    job_id = "00000000-0000-0000-0000-000000000099"
    trial_id = "00000000-0000-0000-0000-000000000098"

    # Record exception in events.jsonl
    queue.append_event(
        SimpleNamespace(
            model_dump_json=lambda exclude_none=True: (
                f'{{"reason_code":"{PROJECTION_FAILURE_REASON}:{job_id}:KnownFailure"}}'
            )
        )
    )

    def mock_catalog(url: str) -> tuple[dict, dict]:
        jobs = {job_id: {"id": job_id, "name": "excepted-job", "path": "runs/excepted-job"}}
        trials = {
            trial_id: {
                "id": trial_id,
                "job_id": job_id,
                "name": "excepted-trial",
                "path": "runs/excepted-job/excepted-trial",
            }
        }
        return jobs, trials

    res = verify_ingest(
        tmp_path,
        database_url="postgresql://mock",
        derived_root=tmp_path / "derived",
        events_path=queue.events_path,
        catalog_loader=mock_catalog,
    )

    assert res.accounted_exceptions_count == 1
    assert res.accounted_exceptions_by_reason == {"KnownFailure": 1}
    assert len(res.gaps) == 0
    assert res.is_complete is True


def test_ingest_verify_cli_output(capsys: pytest.CaptureFixture[str]) -> None:
    """CLI entry point python -m evallab.ingest_verify renders summary and json."""
    from evallab.ingest_verify import main

    repo_root = Path(__file__).resolve().parents[1]

    code = main(["--root", str(repo_root), "--json"])
    assert code == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "is_complete" in data
    assert data["is_complete"] is True
    assert data["gaps_count"] == 0

    # Table output
    code_tbl = main(["--root", str(repo_root)])
    assert code_tbl == 0
    out_tbl = capsys.readouterr().out
    assert "Ingest Completeness Verification" in out_tbl
    assert "COMPLETE (0 gaps)" in out_tbl
