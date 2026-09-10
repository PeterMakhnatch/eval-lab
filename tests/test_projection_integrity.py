"""Projection integrity: concurrent-writer and identity-replacement regressions.

Every test here reproduces an observed production failure mode in isolation
(scratch derived roots, isolated scratch databases) and pins the repair:

- shared fixed ``.parquet.tmp`` names let concurrent writers tear a file;
- a same-path re-ingest under a new Harbor UUID silently evicted the old
  catalog row (and its trials via cascade) with zero forensic link;
- a mid-projection interruption could leave a half-written live partition.

Never touches production R2, the shared catalog, or real runs/.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from evallab.coverage_report import (
    CoverageReport,
    JobCategorySummary,
    RepairPathEntry,
    summarize_binding,
)
from evallab.evidence.atif import PARTITION_MANIFEST_FILE, project_jobs
from evallab.evidence.parquet_io import (
    STAGING_PREFIX,
    new_staging_root,
    publish_staged_job,
    purge_inert_staging,
    write_table_atomic,
)
from evallab.results import load_job
from evallab.storage.paths import discover_parquet_partitions

FIXTURES = (Path(__file__).parent / "fixtures" / "explorer" / "jobs").resolve()
INT_SCHEMA = pa.schema([pa.field("n", pa.int64(), nullable=False)])


def _dsn_reachable(dsn: str) -> bool:
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=1) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def isolated_database_url() -> Any:
    """Scratch database, created and dropped around the module."""
    import psycopg

    base_url = "postgresql://evallab:local-development-only@localhost:54329/evallab"
    if not _dsn_reachable(base_url):
        pytest.skip("requires local PostgreSQL catalog")
    from urllib.parse import urlsplit, urlunsplit

    db_name = f"test_integrity_{uuid.uuid4().hex[:12]}"
    parsed = urlsplit(base_url)
    target_url = urlunsplit(parsed._replace(path=f"/{db_name}"))
    with psycopg.connect(base_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')
    try:
        from evallab.database import initialize

        initialize(target_url, force=True)
        yield target_url
    finally:
        with psycopg.connect(base_url, autocommit=True) as conn:
            try:
                conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
            except Exception:
                conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')


def test_concurrent_same_path_writes_never_tear(tmp_path: Path) -> None:
    """Two writers racing on one path leave a complete file, never a torn one."""
    target = tmp_path / "trial_facts.parquet"
    rows_a = [{"n": i} for i in range(50)]
    rows_b = [{"n": 1000 + i} for i in range(50)]
    errors: list[BaseException] = []

    def write(rows: list[dict[str, Any]]) -> None:
        try:
            for _ in range(10):
                write_table_atomic(target, rows, INT_SCHEMA)
        except BaseException as exc:  # noqa: BLE001 - collected, asserted below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(rows,)) for rows in (rows_a, rows_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    table = pq.read_table(target)
    values = sorted(table.column("n").to_pylist())
    assert values == list(range(50)) or values == list(range(1000, 1050))
    assert list(tmp_path.glob("*.tmp")) == []


def test_staging_is_invisible_to_discovery(tmp_path: Path) -> None:
    """A populated staging root contributes zero partitions to discovery."""
    staging = new_staging_root(tmp_path)
    (staging / "job_id=aaa" / "trial_id=bbb").mkdir(parents=True)
    write_table_atomic(
        staging / "job_id=aaa" / "trial_id=bbb" / "steps.parquet",
        [{"n": 1}],
        INT_SCHEMA,
    )
    discovery = discover_parquet_partitions(tmp_path)
    assert [p for p in discovery.partitions if "aaa" in str(p.path)] == []
    assert [jd for jd in discovery.job_directories if "aaa" in jd.name] == []


def test_interrupted_projection_leaves_no_live_partition(tmp_path: Path) -> None:
    """A fault mid-projection records the failure and leaves no torn live dir."""
    import evallab.evidence.facts as facts_module

    job = load_job(FIXTURES / "job-pass")
    original = facts_module.rebuild_from_raw

    def flaky(jobs: list[Any], output_root: Path) -> Any:
        result = original(jobs, output_root)
        raise RuntimeError("simulated mid-projection crash")

    facts_module.rebuild_from_raw = flaky  # type: ignore[method-assign]
    try:
        tables, failures = project_jobs([job], tmp_path)
    finally:
        facts_module.rebuild_from_raw = original  # type: ignore[method-assign]
    assert len(failures) == 1 and failures[0].error_type == "RuntimeError"
    assert tables == ()
    assert not (tmp_path / f"job_id={job.id}").exists()
    assert list(discover_parquet_partitions(tmp_path).partitions) == []



def test_successful_projection_publishes_complete_live_tree(tmp_path: Path) -> None:
    """The happy path still lands every table plus manifest, with no staging residue."""
    job = load_job(FIXTURES / "job-pass")
    tables, failures = project_jobs([job], tmp_path)
    assert failures == ()
    live = tmp_path / f"job_id={job.id}"
    assert (live / "jobs.parquet").is_file()
    manifests = list(live.glob(f"trial_id=*/{PARTITION_MANIFEST_FILE}"))
    assert manifests, "expected a partition manifest in a trial partition"
    assert list(tmp_path.glob(f"{STAGING_PREFIX}*")) == []
    # Pruned empty tables intentionally have no file; every other record must.
    for table in tables:
        if table.rows == 0:
            assert not table.path.is_file()
        else:
            assert table.path.is_file()


def test_same_path_replacement_records_supersession(isolated_database_url: str, tmp_path: Path) -> None:
    """Re-ingesting one path under a new UUID links the evicted row instead of hiding it."""
    import psycopg

    from evallab import database

    src = tmp_path / "runs" / "job-x"
    src.mkdir(parents=True)
    shutil.copytree(FIXTURES / "job-pass", src, dirs_exist_ok=True)
    job_a = load_job(src)
    new_id = str(uuid.uuid4())
    result_path = src / "result.json"
    payload = json.loads(result_path.read_text())
    payload["id"] = new_id
    result_path.write_text(json.dumps(payload))
    job_b = load_job(src)
    assert str(job_a.id) != str(job_b.id)

    with psycopg.connect(isolated_database_url) as conn:
        database.ingest_job(conn, job_a, root=tmp_path)
        database.ingest_job(conn, job_b, root=tmp_path)
        rows = conn.execute("SELECT id::text, lab_metadata FROM jobs").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == str(job_b.id)
    assert rows[0][1]["supersedes"] == str(job_a.id)


def test_same_id_regeneration_records_no_supersession(isolated_database_url: str, tmp_path: Path) -> None:
    """Intentional regeneration (same UUID) keeps working and records nothing."""
    import psycopg

    from evallab import database

    src = tmp_path / "runs" / "job-x"
    src.mkdir(parents=True)
    shutil.copytree(FIXTURES / "job-pass", src, dirs_exist_ok=True)
    job = load_job(src)
    with psycopg.connect(isolated_database_url) as conn:
        database.ingest_job(conn, job, root=tmp_path)
        database.ingest_job(conn, job, root=tmp_path)
        rows = conn.execute("SELECT id::text, lab_metadata FROM jobs").fetchall()
    assert len(rows) == 1
    assert "supersedes" not in rows[0][1]


def _summary(count: int, jobs: tuple[str, ...] = ()) -> JobCategorySummary:
    return JobCategorySummary(count=count, jobs=jobs, truncated=False)


def _report(**overrides: Any) -> CoverageReport:
    base: dict[str, Any] = {
        "native_jobs_present": _summary(0),
        "catalogued": _summary(0),
        "projected": _summary(0),
        "excepted": _summary(0),
        "failed": _summary(0),
        "trajectory_availability_by_agent": {},
        "reasons": {},
        "repair_path": (),
    }
    base.update(overrides)
    return CoverageReport(**base)  # type: ignore[arg-type]


def test_summarize_binding_matrix() -> None:
    """Link status derives from counted state; it cannot be hand-written stale."""
    assert (
        summarize_binding(
            _report(
                catalogued=_summary(1, ("r2",)),
                projected=_summary(1, ("r2",)),
            ),
            job_name="r2",
        )
        == "bound-catalogued-projected"
    )
    assert (
        summarize_binding(_report(catalogued=_summary(1, ("r2",))), job_name="r2")
        == "bound-catalogued-unprojected"
    )
    assert (
        summarize_binding(
            _report(
                repair_path=(
                    RepairPathEntry(
                        job_name="r2",
                        reason="not_cataloged",
                        resumable_command="evallab ingest runs/r2",
                        origin="disk-only",
                    ),
                )
            ),
            job_name="r2",
        )
        == "bound-excepted-not_cataloged"
    )
    assert summarize_binding(_report(), job_name="r2") == "unbound-unknown"


def test_purge_leaves_fresh_staging_and_live_data(tmp_path: Path) -> None:
    """The opt-in sweeper removes only old staging roots, never live data."""
    import os

    from evallab.evidence.parquet_io import purge_inert_staging

    live = tmp_path / "job_id=live"
    live.mkdir()
    (live / "jobs.parquet").write_bytes(b"live")
    old = tmp_path / f"{STAGING_PREFIX}old"
    old.mkdir()
    ancient = time.time() - 100_000
    os.utime(old, (ancient, ancient))
    fresh = new_staging_root(tmp_path)
    removed = purge_inert_staging(tmp_path, older_than_hours=24.0)
    assert removed == (old,)
    assert not old.exists()
    assert fresh.is_dir()
    assert (live / "jobs.parquet").read_bytes() == b"live"


def test_publish_staged_job_is_atomic_swap(tmp_path: Path) -> None:
    """Publishing swaps a complete staged tree into place, preserving old bytes nowhere live."""
    staging = new_staging_root(tmp_path)
    (staging / "job_id=j1" / "trial_id=t1").mkdir(parents=True)
    write_table_atomic(staging / "job_id=j1" / "trial_id=t1" / "n.parquet", [{"n": 7}], INT_SCHEMA)
    live = publish_staged_job(tmp_path, staging, "j1")
    assert (live / "trial_id=t1" / "n.parquet").is_file()
    assert pq.read_table(live / "trial_id=t1" / "n.parquet").column("n").to_pylist() == [7]
    shutil.rmtree(staging, ignore_errors=True)
    assert list(tmp_path.glob(f"{STAGING_PREFIX}*")) == []
