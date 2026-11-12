"""Tests for behavioral analysis views and programmatic reporting."""

from __future__ import annotations

from pathlib import Path

import duckdb


def test_behavior_sql_in_clean_duckdb() -> None:
    """Test sql/behavior.sql in clean in-memory DuckDB with zero pre-created tables."""
    sql = Path("sql/behavior.sql").read_text()
    with duckdb.connect(":memory:") as con:
        con.execute(sql)
        for view_name in [
            "v_behavior_trial_summary",
            "v_behavior_effort_by_outcome",
            "v_behavior_efficiency",
            "v_behavior_struggle_signals",
            "v_behavior_step_shape",
            "v_behavior_token_economics",
        ]:
            rows = con.execute(f"SELECT * FROM {view_name}").fetchall()
            assert rows == []


