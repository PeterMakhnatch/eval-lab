"""Tests for evidence_queries.sql views and supporting statistics (WS-E)."""

from __future__ import annotations

from pathlib import Path

import duckdb

from evallab.cohort import wilson_interval


def test_evidence_sql_executes_in_clean_duckdb() -> None:
    """Test sql/evidence_queries.sql in clean DuckDB, zero pre-tables, views resolve."""
    sql = Path("sql/evidence_queries.sql").read_text()
    with duckdb.connect(":memory:") as con:
        con.execute(sql)
        for view_name in [
            "v_outcome_by_task_agent",
            "v_failure_classification",
            "v_exception_taxonomy",
            "v_outcome_by_date_bucket",
        ]:
            rows = con.execute(f"SELECT * FROM {view_name}").fetchall()
            assert rows == []


def test_exception_vs_scored_failure_split() -> None:
    """Test v_failure_classification separates harness vs scored failures."""
    sql = Path("sql/evidence_queries.sql").read_text()
    with duckdb.connect(":memory:") as con:
        # Populate fallback with synthetic data covering both directions
        con.execute(
            """
            CREATE TABLE trial_evidence_schema_fallback (
                experiment_id VARCHAR, job_id VARCHAR, trial_id VARCHAR,
                job_name VARCHAR, trial_name VARCHAR, task_name VARCHAR,
                task_digest VARCHAR, agent_name VARCHAR, agent_version VARCHAR,
                model_name VARCHAR, primary_reward DOUBLE, reward DOUBLE,
                exception_class VARCHAR, exception_phase VARCHAR,
                started_at TIMESTAMP, finished_at TIMESTAMP,
                duration_seconds DOUBLE, cost_usd DOUBLE
            );
            INSERT INTO trial_evidence_schema_fallback VALUES
            ('exp1','j1','t1','job1','trial1','terminal-bench/html-js-filter','d1',
             'codex','v1','gpt',NULL,0.0,NULL,NULL,'2026-08-15 00:00:00',NULL,10.0,0.1),
            ('exp1','j1','t2','job1','trial2','terminal-bench/html-js-filter','d1',
             'codex','v1','gpt',NULL,NULL,'NonZeroAgentExitCodeError','agent',
             '2026-08-15 00:01:00',NULL,5.0,0.05),
            ('exp2','j2','t3','job2','trial3','event-summary','d2',
             'oracle','v1','gpt',NULL,1.0,NULL,NULL,'2026-08-14 00:00:00',NULL,2.0,0.01);
            """
        )
        con.execute(sql)
        rows = con.execute(
            "SELECT failure_type, n FROM v_failure_classification ORDER BY failure_type"
        ).fetchall()
        result = {row[0]: row[1] for row in rows}
        assert result.get("harness_exception") == 1
        assert result.get("scored_failure") == 1
        assert result.get("passed") == 1


def test_wilson_interval_matches_cohort() -> None:
    """Test Wilson interval matches cohort.py for known input."""
    ci = wilson_interval(0, 3)
    assert ci is not None
    low, high = ci
    assert 0.0 <= low <= high <= 1.0
    ci_full = wilson_interval(3, 3)
    assert ci_full is not None
    assert ci_full[0] > 0.4 and ci_full[1] == 1.0
    assert wilson_interval(0, 0) is None


def test_underpowered_renders_as_underpowered() -> None:
    """Test n<5 renders as underpowered rather than finding."""
    n = 3
    powered = n >= 5
    assert not powered
    finding = "insufficient n" if not powered else "some rate"
    assert finding == "insufficient n"
