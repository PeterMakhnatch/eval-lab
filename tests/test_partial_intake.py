"""Behavioral tests for partial-job intake and verification (M029)."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from evallab.evidence.atif import JOB_PROJECTION_FILE
from evallab.ingest_verify import verify_ingest
from evallab.partial_intake import (
    PARTIAL_MARKER_FILE,
    ingest_partial_job,
    load_partial_job,
    partial_partition_missing_tables,
    read_partial_manifest,
)
from evallab.results import load_job


def _create_partial_job_fixture(
    parent_dir: Path,
    job_name: str = "failed-network-policy-fixture",
    job_id: str = "179657b2-18d3-4ac4-9044-f3fd9d61729c",
    trial_name: str = "event-summary__AjHV5Cq",
) -> Path:
    """Build a tiny fixture mirroring the failed-network-policy shape."""
    job_dir = parent_dir / job_name
    job_dir.mkdir(parents=True, exist_ok=True)

    # Job result with finished_at=null and pending trials
    job_result = {
        "id": job_id,
        "started_at": "2026-08-13T20:30:39.937005",
        "updated_at": "2026-08-13T20:30:39.937051",
        "finished_at": None,
        "n_total_trials": 1,
        "stats": {
            "n_completed_trials": 0,
            "n_errored_trials": 0,
            "n_running_trials": 0,
            "n_pending_trials": 1,
            "n_cancelled_trials": 0,
            "n_retries": 0,
            "evals": {},
            "n_input_tokens": None,
            "n_cache_tokens": None,
            "n_output_tokens": None,
            "cost_usd": None,
        },
    }
    (job_dir / "result.json").write_text(json.dumps(job_result, indent=2))

    job_config = {
        "job_name": "event-summary-oracle-evidence",
        "jobs_dir": "/tmp/runs",
        "n_concurrent_trials": 1,
        "tasks": [{"path": "/tmp/tasks/event-summary"}],
    }
    (job_dir / "config.json").write_text(json.dumps(job_config, indent=2))

    job_lock = {
        "schema_version": 3,
        "created_at": "2026-08-14T00:30:39.940329Z",
        "harbor": {"version": "0.21.0", "is_editable": False},
        "n_concurrent_trials": 1,
    }
    (job_dir / "lock.json").write_text(json.dumps(job_lock, indent=2))
    (job_dir / "job.log").write_text("launching job\n")

    # Trial directory with lock.json + trial.log, but NO result.json
    trial_dir = job_dir / trial_name
    trial_dir.mkdir(parents=True, exist_ok=True)
    trial_lock = {
        "schema_version": 2,
        "task": {
            "name": "event-summary",
            "version": "1.0.0",
            "type": "local",
            "digest": "sha256:9d59393e8eb94b8f64de36e493cd85a0859e9a9bcadf51cff50cd91fe4d8da45",
            "path": "/tmp/tasks/event-summary",
        },
        "agent": {
            "name": "oracle",
            "model_name": "oracle-model",
            "kwargs": {},
        },
        "environment": {"type": "docker"},
        "verifier": {"disable": False},
    }
    (trial_dir / "lock.json").write_text(json.dumps(trial_lock, indent=2))
    (trial_dir / "trial.log").write_text("trial starting...\n")

    # Artifacts dir with manifest.json and a real artifact file
    artifacts_dir = trial_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    dummy_log = artifacts_dir / "stdout.txt"
    dummy_log.write_text("hello stdout\n")
    manifest = {
        "entries": [
            {
                "source": "/app/stdout.txt",
                "destination": "artifacts/stdout.txt",
                "type": "log",
                "status": "completed",
            }
        ]
    }
    (artifacts_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    return job_dir


def test_load_partial_job_lenient_loading(tmp_path: Path) -> None:
    """load_partial_job leniently loads finished_at=null evidence without throwing."""
    job_dir = _create_partial_job_fixture(tmp_path)

    # Standard load_job MUST refuse this unfinished directory
    with pytest.raises(ValueError, match="Not a completed Harbor job directory"):
        load_job(job_dir)

    # Lenient loader succeeds
    partial = load_partial_job(job_dir)
    assert partial.id == "179657b2-18d3-4ac4-9044-f3fd9d61729c"
    assert partial.name == "failed-network-policy-fixture"
    assert partial.reason == "pending_trials_unexecuted"
    assert len(partial.trials) == 1

    trial = partial.trials[0]
    # Constructed with result={} for result-less trials
    assert trial.result == {}
    assert trial.rewards == {}
    # Artifacts exist and are hashed honestly
    assert len(trial.artifacts) == 1
    assert trial.artifacts[0].exists is True
    assert trial.artifacts[0].sha256 is not None

    # Deterministic trial identity from directory name
    t_id = partial.get_trial_id(trial)
    assert len(t_id) == 36  # valid UUID format
    assert partial.get_trial_name(trial) == "event-summary__AjHV5Cq"


def test_ingest_partial_job_generates_correct_parquet_and_marker(tmp_path: Path) -> None:
    """Partial ingest yields jobs.parquet + trial_facts.parquet + _partial.json and ZERO reward/trajectory files."""
    job_dir = _create_partial_job_fixture(tmp_path)
    partial = load_partial_job(job_dir)

    derived_root = tmp_path / "derived" / "parquet"
    result = ingest_partial_job(None, partial, root=tmp_path, output_root=derived_root)

    job_partition = derived_root / f"job_id={partial.id}"
    trial_id = partial.get_trial_id(partial.trials[0])
    trial_partition = job_partition / f"trial_id={trial_id}"

    # 1. jobs.parquet exists
    jobs_parquet = job_partition / JOB_PROJECTION_FILE
    assert jobs_parquet.is_file()
    jobs_tbl = pq.read_table(jobs_parquet)
    assert jobs_tbl.num_rows == 1
    assert jobs_tbl.column("job_id")[0].as_py() == partial.id
    assert jobs_tbl.column("trial_count")[0].as_py() == 1

    # 2. trial_facts.parquet exists with honest values
    facts_parquet = trial_partition / "trial_facts.parquet"
    assert facts_parquet.is_file()
    facts_tbl = pq.read_table(facts_parquet)
    assert facts_tbl.num_rows == 1
    assert facts_tbl.column("trial_id")[0].as_py() == trial_id
    assert facts_tbl.column("job_id")[0].as_py() == partial.id
    assert facts_tbl.column("task_name")[0].as_py() == "event-summary"
    assert facts_tbl.column("task_digest")[0].as_py() == "sha256:9d59393e8eb94b8f64de36e493cd85a0859e9a9bcadf51cff50cd91fe4d8da45"

    # Honest filling: NO placeholder rewards, NO zero-filled measured semantics
    assert facts_tbl.column("primary_reward")[0].as_py() is None
    assert facts_tbl.column("duration_seconds")[0].as_py() is None
    assert facts_tbl.column("input_tokens")[0].as_py() is None
    assert facts_tbl.column("cost_usd")[0].as_py() is None
    assert facts_tbl.column("trajectory_count")[0].as_py() == 0
    assert facts_tbl.column("step_count")[0].as_py() == 0
    assert facts_tbl.column("state_journal_status")[0].as_py() == "missing"

    # 3. _partial.json marker exists with exact structure
    marker_file = job_partition / PARTIAL_MARKER_FILE
    assert marker_file.is_file()
    manifest_data = json.loads(marker_file.read_text())
    assert manifest_data == {
        "finished_at": None,
        "intake": "partial",
        "schema_version": 1,
        "tables": {
            "jobs": 1,
            "trial_facts": 1,
        },
    }

    # 4. ABSOLUTE RULES: ZERO reward/trajectory files written
    forbidden = [
        "reward_facts.parquet",
        "trajectories.parquet",
        "steps.parquet",
        "tool_calls.parquet",
        "observations.parquet",
    ]
    for filename in forbidden:
        assert not (trial_partition / filename).exists()
        assert not (job_partition / filename).exists()


def test_partial_partition_completeness_predicate(tmp_path: Path) -> None:
    """partial_partition_missing_tables reports partial-not-missing for a valid partial partition."""
    job_dir = _create_partial_job_fixture(tmp_path)
    partial = load_partial_job(job_dir)
    derived_root = tmp_path / "derived" / "parquet"

    ingest_partial_job(None, partial, root=tmp_path, output_root=derived_root)
    job_partition = derived_root / f"job_id={partial.id}"

    # Reports empty frozenset (partial-not-missing)
    missing = partial_partition_missing_tables(job_partition)
    assert missing == frozenset()

    # If trial_facts.parquet is removed, reports missing
    trial_id = partial.get_trial_id(partial.trials[0])
    trial_fact_file = job_partition / f"trial_id={trial_id}" / "trial_facts.parquet"
    trial_fact_file.unlink()
    missing_after_unlink = partial_partition_missing_tables(job_partition)
    assert f"trial_id={trial_id}/trial_facts.parquet" in missing_after_unlink

    # If forbidden file exists, reports violation
    trial_fact_file.write_bytes(b"dummy")
    forbidden_file = job_partition / f"trial_id={trial_id}" / "reward_facts.parquet"
    forbidden_file.write_bytes(b"forbidden-reward")
    missing_with_forbidden = partial_partition_missing_tables(job_partition)
    assert "forbidden:reward_facts.parquet" in missing_with_forbidden


def test_verify_ingest_reports_partial_jobs_never_gaps(tmp_path: Path) -> None:
    """verify_ingest accounts for _partial.json under partial_jobs and emits 0 gaps."""
    job_dir = _create_partial_job_fixture(tmp_path)
    partial = load_partial_job(job_dir)
    derived_root = tmp_path / "derived" / "parquet"

    ingest_partial_job(None, partial, root=tmp_path, output_root=derived_root)

    trial = partial.trials[0]
    t_id = partial.get_trial_id(trial)

    # Mock catalog loader that returns this cataloged partial job and trial
    def mock_catalog(_db_url: str):
        jobs = {
            partial.id: {
                "id": partial.id,
                "name": partial.name,
                "path": str(partial.path),
            }
        }
        trials = {
            t_id: {
                "id": t_id,
                "job_id": partial.id,
                "name": trial.path.name,
                "path": str(trial.path),
            }
        }
        return jobs, trials

    res = verify_ingest(
        repo_root=tmp_path,
        derived_root=derived_root,
        catalog_loader=mock_catalog,
    )

    # Accounted under partial_jobs, NEVER gaps or unfinished_jobs
    assert len(res.partial_jobs) == 1
    assert res.partial_jobs[0].job_id == partial.id
    assert res.partial_jobs[0].reason == "partial_intake"
    assert len(res.unfinished_jobs) == 0
    assert len(res.gaps) == 0
    assert res.is_complete is True

    # Summary table includes partial jobs line
    summary = res.summary_table()
    assert "Cataloged but partial:        1" in summary


def test_reingest_partial_job_is_idempotent(tmp_path: Path) -> None:
    """Re-ingesting a partial job produces byte-identical marker and Parquet digests."""
    job_dir = _create_partial_job_fixture(tmp_path)
    partial = load_partial_job(job_dir)
    derived_root = tmp_path / "derived" / "parquet"

    # Pass 1
    res1 = ingest_partial_job(None, partial, root=tmp_path, output_root=derived_root)
    marker_bytes_1 = res1.manifest_path.read_bytes()
    table_shas_1 = {t.table: t.sha256 for t in res1.tables}

    # Pass 2
    res2 = ingest_partial_job(None, partial, root=tmp_path, output_root=derived_root)
    marker_bytes_2 = res2.manifest_path.read_bytes()
    table_shas_2 = {t.table: t.sha256 for t in res2.tables}

    # Manifest and Parquet tables are byte-for-byte identical
    assert marker_bytes_1 == marker_bytes_2
    assert table_shas_1 == table_shas_2
    assert res1.row_counts == res2.row_counts


def test_ingest_partial_job_database_cataloging(tmp_path: Path) -> None:
    """When database_url is provided, catalog the job and trial rows without rewards."""
    from evallab.runner import database_url_from_environment
    import psycopg

    db_url = database_url_from_environment()
    try:
        with psycopg.connect(db_url, connect_timeout=1) as conn:
            conn.execute("SELECT 1")
    except Exception:
        pytest.skip("PostgreSQL catalog not reachable")

    import uuid
    job_id = str(uuid.uuid4())
    job_dir = _create_partial_job_fixture(tmp_path, job_id=job_id)
    partial = load_partial_job(job_dir)
    derived_root = tmp_path / "derived" / "parquet"

    try:
        res = ingest_partial_job(db_url, partial, root=tmp_path, output_root=derived_root)
        assert res.cataloged is True

        with psycopg.connect(db_url) as conn:
            job_row = conn.execute(
                "SELECT id, finished_at, lab_metadata FROM jobs WHERE id = %s",
                (partial.id,),
            ).fetchone()
            assert job_row is not None
            assert job_row[1] is None  # finished_at is null
            meta = job_row[2]
            assert meta.get("intake") == "partial"
            assert meta.get("finished_at") is None
            assert meta.get("reason") == partial.reason

            # Trial rows exist without reward claims
            trial_id = partial.get_trial_id(partial.trials[0])
            trial_row = conn.execute(
                "SELECT id, primary_reward, finished_at FROM trials WHERE id = %s",
                (trial_id,),
            ).fetchone()
            assert trial_row is not None
            assert trial_row[1] is None  # primary_reward is null
            assert trial_row[2] is None  # finished_at is null

            # Zero rewards in rewards table
            reward_count = conn.execute(
                "SELECT count(*) FROM rewards WHERE trial_id = %s",
                (trial_id,),
            ).fetchone()[0]
            assert reward_count == 0
    finally:
        with psycopg.connect(db_url) as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
