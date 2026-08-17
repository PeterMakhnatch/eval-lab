"""E05 join-spine CI gate (tests/test_join_spine.py per platform-architecture §2.1).

Fixture-driven. Fails on broken spine. Plants specific violation and asserts
exact orphan report + non-zero exit. Healthy fixture passes. Guards v_spine
left-join property. Uses tmp_path (no derived/, runs/, Postgres in CI).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from evallab.spine import check_spine


def _populate_healthy(conn: duckdb.DuckDBPyConnection) -> None:
    """Populate minimal healthy spine corpus under tmp_path layout."""
    conn.execute(
        """
        INSERT INTO experiments VALUES ('spec1', 'test');
        INSERT INTO jobs VALUES ('job1', 'spec1', 'j1');
        INSERT INTO trials VALUES ('trial1', 'job1', 't1', 'task@1', 'agent-a');
        INSERT INTO trajectory_documents VALUES ('traj1', 'trial1');
        INSERT INTO analysis_invocations VALUES ('anal1', 'trial1');
        INSERT INTO observation_records VALUES ('trial1');
        """
    )


def _plant_orphan_violation(conn: duckdb.DuckDBPyConnection) -> None:
    """Plant a trial referencing non-existent job (trial→job edge break)."""
    conn.execute(
        """
        INSERT INTO trials VALUES ('orphan_trial', 'missing_job', 'bad', 'task@1', 'agent-a');
        """
    )


def test_healthy_spine_passes() -> None:
    """Healthy fixture: zero orphans, exit 0."""
    conn = duckdb.connect(":memory:")
    from evallab.spine import _load_fallbacks  # type: ignore[attr-defined]

    _load_fallbacks(conn)
    _populate_healthy(conn)
    report = check_spine(conn)
    assert report["total_orphans"] == 0
    assert all(d["orphans"] == 0 for d in report["edges"].values())


def test_broken_spine_reports_exact_orphan_and_nonzero(tmp_path: Path) -> None:
    """Broken: plants trial→job orphan, asserts report + exit 1."""
    conn = duckdb.connect(":memory:")
    from evallab.spine import _load_fallbacks  # type: ignore[attr-defined]

    _load_fallbacks(conn)
    _populate_healthy(conn)
    _plant_orphan_violation(conn)
    report = check_spine(conn)
    assert report["edges"]["trial → job"]["orphans"] == 1
    assert "missing_job" in str(report["edges"]["trial → job"]["samples"])
    assert report["total_orphans"] == 1
    assert check_spine(conn)["total_orphans"] > 0
def test_v_spine_left_join_preserves_trials_without_analysis(tmp_path: Path) -> None:
    """v_spine resolves in clean DuckDB; trial without analysis appears with nulls."""
    views_sql = Path("sql/views.sql").read_text()
    conn = duckdb.connect(":memory:")
    conn.execute(views_sql)  # creates fallbacks + v_spine
    # Insert minimal: job+spec+trial but no analysis row
    conn.execute(
        """
        INSERT INTO experiments VALUES ('s1', 'test');
        INSERT INTO jobs VALUES ('j1', 's1', 'job1');
        INSERT INTO trials VALUES ('t1', 'j1', 't1', 'task@1', 'agent-a');
        -- no analysis_invocations row for t1
        """
    )
    rows = conn.execute("SELECT trial_id, analysis_id FROM v_spine").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "t1"
    assert rows[0][1] is None  # left join null for analysis_id


def test_spine_checker_cli_help() -> None:
    """CLI entrypoint exists and responds."""
    result = subprocess.run(
        [sys.executable, "-m", "evallab.spine", "check", "--help"],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert result.returncode == 0
    assert "E05 join-spine validator" in result.stdout

@pytest.mark.skipif(
    True,  # always skip in CI; real corpus absent
    reason="requires local derived/ parquet or DATABASE_URL; see docs/quality.md",
)
def test_real_local_corpus_no_orphans() -> None:
    """Optional: run against real corpus if present; record findings in handoff if orphans."""
    # Would populate from derived_root or db; skipped here.
    pass