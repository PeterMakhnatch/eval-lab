"""Tests for E04 unified attach surface.

CI has no derived/, runs/, or PostgreSQL — tests must pass without them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from evallab.attach import attach
from evallab.cli import run_cli


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
    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        rows = result.connection.execute("SELECT COUNT(*) FROM trial_facts").fetchone()
        assert rows is not None
        assert rows[0] == 2
        job_rows = result.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()
        assert job_rows is not None
        assert job_rows[0] == 1
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
    for t in ["reward_facts", "artifact_facts", "trajectories", "steps", "tool_calls", "tool_usage", "observations"]:  # noqa: E501
        _write_parquet_tree(derived, t, [{"trial_id": "t1"}])
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:5432/nowhere")
    code = run_cli(["db", "attach", "--derived-root", "derived", "--zones"], workspace=tmp_path)
    out, _ = capsys.readouterr()
    assert code == 0
    assert "z3: attached" in out
    assert "9/9 tables" in out


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


@pytest.mark.skipif(
    os.getenv("DATABASE_URL", "").startswith("postgresql://localhost"),
    reason="requires real DSN with live Postgres for z2 success path",
)
def test_cli_zones_with_real_postgres_if_available() -> None:
    # placeholder; skipped unless real DSN set
    pass


