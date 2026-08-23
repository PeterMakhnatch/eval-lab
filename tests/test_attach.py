"""Tests for E04 unified attach surface.

CI has no derived/, runs/, or PostgreSQL — tests must pass without them.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from evallab.attach import attach
from evallab.cli import run_cli
from evallab.runner import database_url_from_environment


def _catalog_reachable(dsn: str | None = None) -> bool:
    try:
        import psycopg

        url = database_url_from_environment(dsn)
        with psycopg.connect(url, connect_timeout=1) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


_DSN_FOR_TEST = database_url_from_environment()

def _write_parquet_tree(root: Path, table: str, rows: list[dict]) -> None:
    job_dir = root / "job_id=testjob" / "trial_id=testtrial"
    job_dir.mkdir(parents=True, exist_ok=True)
    table_path = job_dir / f"{table}.parquet"
    tbl = pa.Table.from_pylist(rows) if rows else pa.table({"id": pa.array([], type=pa.string())})
    pq.write_table(tbl, table_path)


def test_z3_views_return_written_rows(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()
    _write_parquet_tree(
        derived,
        "trial_facts",
        [{"trial_id": "t1", "reward": 1.0}, {"trial_id": "t2", "reward": 0.0}],
    )
    _write_parquet_tree(
        derived,
        "jobs",
        [{"job_id": "testjob", "status": "done"}],
    )
    _write_parquet_tree(
        derived,
        "behavior_labels",
        [{
            "label_id": "label-1",
            "trial_id": "t1",
            "target_type": "trajectory",
            "model_name": "judge-model",
        }],
    )
    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        rows = result.connection.execute("SELECT COUNT(*) FROM trial_facts").fetchone()
        assert rows is not None
        assert rows[0] == 2
        rows_z3 = result.connection.execute("SELECT COUNT(*) FROM z3.trial_facts").fetchone()
        assert rows_z3 is not None
        assert rows_z3[0] == 2
        job_rows = result.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()
        assert job_rows is not None
        assert job_rows[0] == 1
        job_rows_z3 = result.connection.execute("SELECT COUNT(*) FROM z3.jobs").fetchone()
        assert job_rows_z3 is not None
        assert job_rows_z3[0] == 1
        label_rows = result.connection.execute(
            "SELECT target_type, model_name FROM behavior_labels"
        ).fetchall()
        assert label_rows == [("trajectory", "judge-model")]
    finally:
        result.connection.close()


def test_standalone_parquet_is_attached_once_and_preamble_has_one_glob(
    tmp_path: Path,
) -> None:
    derived = tmp_path / "derived"
    labels_dir = derived / "behavior_labels"
    labels_dir.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist([{"label_id": "label-1", "trial_id": "t1"}]),
        labels_dir / "behavior_labels.parquet",
    )

    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        assert result.connection.execute(
            "SELECT count(*), count(DISTINCT label_id) FROM behavior_labels"
        ).fetchone() == (1, 1)
        assert result.connection.execute(
            "SELECT count(*), count(DISTINCT label_id) FROM z3.behavior_labels"
        ).fetchone() == (1, 1)
        assert "behavior_labels/behavior_labels.parquet" not in result.sql_preamble
        assert result.sql_preamble.count("behavior_labels/*.parquet") == 2
    finally:
        result.connection.close()
def test_z2_unavailable_reports_reason_not_empty_view(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:5432/nowhere")
    result = attach(repo_root=Path.cwd())
    try:
        z2 = next(z for z in result.zones if z.name == "z2")
        assert z2.attached is False
        assert z2.reason is not None
    finally:
        result.connection.close()


def test_z4_front_matter_populates_from_docs(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "test.md").write_text(
        "---\nstatus: living\naudience:\n  - builder\n---\n\n# Title\nbody",
        encoding="utf-8",
    )
    result = attach(repo_root=tmp_path)
    try:
        rows = result.connection.execute(
            "SELECT status, audience FROM z4.front_matter WHERE path = 'docs/test.md'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "living"
        rows_unqualified = result.connection.execute(
            "SELECT status, audience FROM front_matter WHERE path = 'docs/test.md'"
        ).fetchall()
        assert len(rows_unqualified) == 1
        assert rows_unqualified[0][0] == "living"
    finally:
        result.connection.close()

def test_sql_preamble_is_byte_identical(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()
    r1 = attach(repo_root=tmp_path, explicit_derived=derived)
    r2 = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        assert r1.sql_preamble == r2.sql_preamble
    finally:
        r1.connection.close()
        r2.connection.close()


def test_cross_zone_join_when_postgres_unavailable_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:5432/nowhere")
    result = attach(repo_root=Path.cwd())
    try:
        z2 = next(z for z in result.zones if z.name == "z2")
        assert z2.attached is False
    finally:
        result.connection.close()
def test_cli_zones_reports_z3_with_row_counts(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: E501
    derived = tmp_path / "derived"
    derived.mkdir()
    _write_parquet_tree(derived, "trial_facts", [{"trial_id": "t1"}, {"trial_id": "t2"}])
    _write_parquet_tree(derived, "jobs", [{"job_id": "j1"}])
    for t in ["reward_facts", "artifact_facts", "trajectories", "steps", "tool_calls", "tool_usage", "observations", "state_changes"]:  # noqa: E501
        _write_parquet_tree(derived, t, [{"trial_id": "t1"}])
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:5432/nowhere")
    code = run_cli(["db", "attach", "--derived-root", "derived", "--zones"], workspace=tmp_path)
    out, _ = capsys.readouterr()
    assert code == 0
    assert "z3: attached" in out
    assert "10/17 tables" in out


def test_cli_zones_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: E501
    # every zone unavailable -> non-zero
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:5432/nowhere")
    code = run_cli(["db", "attach", "--derived-root", "derived", "--zones"], workspace=tmp_path)
    assert code == 1
    # partial (z3 present) -> zero
    derived = tmp_path / "derived"
    derived.mkdir()
    _write_parquet_tree(derived, "trial_facts", [{"trial_id": "t1"}])
    code = run_cli(["db", "attach", "--derived-root", "derived", "--zones"], workspace=tmp_path)
    assert code == 0


def test_cli_print_sql_byte_identical(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: E501
    derived = tmp_path / "derived"
    derived.mkdir()
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:5432/nowhere")
    code1 = run_cli(["db", "attach", "--derived-root", "derived", "--print-sql"], workspace=tmp_path)  # noqa: E501
    out1, _ = capsys.readouterr()
    code2 = run_cli(["db", "attach", "--derived-root", "derived", "--print-sql"], workspace=tmp_path)  # noqa: E501
    out2, _ = capsys.readouterr()
    assert code1 == 0 and code2 == 0
    assert out1 == out2


def test_cli_query_returns_fixture_rows(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: E501
    derived = tmp_path / "derived"
    derived.mkdir()
    _write_parquet_tree(derived, "trial_facts", [{"trial_id": "t1", "reward": 1.0}])
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:5432/nowhere")
    code = run_cli(["db", "attach", "--derived-root", "derived", "--query", "select count(*) from trial_facts"], workspace=tmp_path)  # noqa: E501
    out, _ = capsys.readouterr()
    assert code == 0
    assert out.strip() == "(1,)"


def test_duckdb_z2_catalog_prefix_resolution_standalone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In a clean DuckDB session with attached database z2, z2.<table> resolves unambiguously."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:5432/nowhere")
    db_path = tmp_path / "z2_catalog.duckdb"
    with duckdb.connect(str(db_path)) as z2_con:
        z2_con.execute("CREATE TABLE verdicts (id TEXT, status TEXT)")
        z2_con.execute(
            "INSERT INTO verdicts VALUES ('testjob', 'accepted'), ('otherjob', 'rejected')"
        )
    derived = tmp_path / "derived"
    derived.mkdir()
    _write_parquet_tree(derived, "trial_facts", [{"trial_id": "t1", "job_id": "1"}])

    # Attach with z2 as an attached catalog (simulating what postgres_scanner does)
    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        result.connection.execute(f"ATTACH '{db_path}' AS z2")
        # Qualified query must resolve unambiguously
        rows = result.connection.execute("SELECT count(*) FROM z2.verdicts").fetchall()
        assert rows == [(2,)]
        # Cross-zone join spanning z2 catalog and z3 parquet view
        join_rows = result.connection.execute(
            "SELECT v.status, COUNT(*) FROM z2.verdicts v "
            "JOIN z3.trial_facts t ON v.id::text = t.job_id::text GROUP BY v.status"
        ).fetchall()
        assert join_rows == [("accepted", 1)]
    finally:
        result.connection.close()


@pytest.mark.skipif(
    not _catalog_reachable(),
    reason=f"requires live PostgreSQL {_DSN_FOR_TEST}",
)
def test_cli_query_with_real_postgres(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()
    _write_parquet_tree(derived, "trial_facts", [{"trial_id": "t1", "job_id": "j1"}])
    code = run_cli(["db", "attach", "--derived-root", "derived", "--query", "SELECT COUNT(*) FROM z2.verdicts"], workspace=tmp_path)  # noqa: E501
    out, _ = capsys.readouterr()
    assert code == 0
    assert out.strip().startswith("(")


@pytest.mark.skipif(
    not _catalog_reachable(),
    reason=f"requires live PostgreSQL {_DSN_FOR_TEST}",
)
def test_cross_zone_join_with_real_postgres(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()
    _write_parquet_tree(derived, "trial_facts", [{"trial_id": "t1", "job_id": "j1"}])
    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        # Both qualified z3.trial_facts and unqualified trial_facts join with z2.jobs
        rows1 = result.connection.execute(
            "SELECT j.job_name, COUNT(*) FROM z2.jobs j "
            "JOIN z3.trial_facts t ON j.id::text = t.job_id::text GROUP BY j.job_name"
        ).fetchall()
        assert isinstance(rows1, list)
        rows2 = result.connection.execute(
            "SELECT j.job_name, COUNT(*) FROM z2.jobs j "
            "JOIN trial_facts t ON j.id::text = t.job_id::text GROUP BY j.job_name"
        ).fetchall()
        assert isinstance(rows2, list)
    finally:
        result.connection.close()
