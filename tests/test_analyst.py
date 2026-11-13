"""Tests for ANALYST durable agent analysis with stored reasoning trajectories."""

from __future__ import annotations

from pathlib import Path

import duckdb


def test_sql_views_in_clean_duckdb_session() -> None:
    """The view resolves in a clean DuckDB session with no pre-created tables."""
    sql = Path("sql/analyst.sql").read_text(encoding="utf-8")
    with duckdb.connect(":memory:") as con:
        con.execute(sql)

        for view_name in [
            "v_analysis_records",
            "v_analyst_trajectories",
            "v_analysis_with_trajectory",
        ]:
            rows = con.execute(f"SELECT * FROM {view_name}").fetchall()
            assert rows == [], f"View {view_name} should return empty result in clean session"
