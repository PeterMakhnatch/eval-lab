"""Tests for evidence_queries.sql views and supporting statistics (WS-E)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import evallab.paths
from evallab.cohort import wilson_interval
from evallab.lessons import DEFAULT_POWER_THRESHOLD

repo_root_for_check = Path.cwd()
try:
    derived_for_check = evallab.paths.derived_root_from_environment(
        repo_root_for_check
    )
    real_corpus_present = (
        derived_for_check.exists()
        and bool(list(derived_for_check.glob("job_id=*")))
    )
except Exception:
    real_corpus_present = False

def test_evidence_sql_executes_in_clean_duckdb() -> None:
    """Test sql/evidence_queries.sql in clean DuckDB, zero pre-tables, views resolve."""
    sql = Path("sql/evidence_queries.sql").read_text()
    with duckdb.connect(":memory:") as con:
        con.execute(sql)
        for view_name in [
            "v_outcome_by_task_agent",
            "v_task_summary",
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
                task_digest VARCHAR, verifier_digest VARCHAR, environment_digest VARCHAR,
                agent_config_digest VARCHAR, agent_name VARCHAR, agent_version VARCHAR,
                model_name VARCHAR, primary_reward DOUBLE,
                exception_class VARCHAR, exception_phase VARCHAR,
                duration_seconds DOUBLE, cost_usd DOUBLE
            );
            INSERT INTO trial_evidence_schema_fallback VALUES
            ('exp1','j1','t1','job1','trial1','terminal-bench/html-js-filter','d1',
             NULL,NULL,NULL,'codex','v1','gpt',0.0,NULL,NULL,10.0,0.1),
            ('exp1','j1','t2','job1','trial2','terminal-bench/html-js-filter','d1',
             NULL,NULL,NULL,'codex','v1','gpt',NULL,'NonZeroAgentExitCodeError','unknown',5.0,0.05),
            ('exp2','j2','t3','job2','trial3','event-summary','d2',
             NULL,NULL,NULL,'oracle','v1','gpt',1.0,NULL,NULL,2.0,0.01);
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
    """Test Wilson interval matches cohort.py for known inputs."""
    ci_0_3 = wilson_interval(0, 3)
    assert ci_0_3 is not None
    assert 0.0 <= ci_0_3[0] <= ci_0_3[1] <= 1.0
    assert round(ci_0_3[1], 3) == 0.561

    ci_0_6 = wilson_interval(0, 6)
    assert ci_0_6 is not None
    assert round(ci_0_6[0], 3) == 0.0
    assert round(ci_0_6[1], 3) == 0.390

    ci_6_6 = wilson_interval(6, 6)
    assert ci_6_6 is not None
    assert round(ci_6_6[0], 3) == 0.610
    assert ci_6_6[1] == 1.0

    ci_5_5 = wilson_interval(5, 5)
    assert ci_5_5 is not None
    assert round(ci_5_5[0], 3) == 0.566
    assert ci_5_5[1] == 1.0

    assert wilson_interval(0, 0) is None


def test_powered_threshold_and_inversion() -> None:
    """Test that n=3 is underpowered (n<5) while n=6 is powered (n>=5)."""
    assert DEFAULT_POWER_THRESHOLD == 5

    n_promoted = 3
    assert n_promoted < DEFAULT_POWER_THRESHOLD

    n_corpus = 6
    assert n_corpus >= DEFAULT_POWER_THRESHOLD

    ci = wilson_interval(0, n_corpus)
    assert ci is not None
    assert ci[1] < 0.40


def test_full_corpus_derived_parquet_coverage(tmp_path, monkeypatch) -> None:
    """Test parquet corpus via seam + both never/measured >0 on tasks."""
    fixture_root = tmp_path / "derived"
    # 4 rows: 2 jobs × 2 trials. html-js-filter has both measured and never-measured.
    fixture_trials = (
        {
            "job_id": "j1",
            "trial_id": "t1",
            "task_name": "terminal-bench/html-js-filter",
            "primary_reward": None,
            "exception_class": "ValueError",
            "exception_phase": "unknown",
            "agent_version": "v1",
            "agent_name": "codex",
            "job_name": "canary",
        },
        {
            "job_id": "j1",
            "trial_id": "t2",
            "task_name": "terminal-bench/html-js-filter",
            "primary_reward": 0.0,
            "exception_class": None,
            "exception_phase": None,
            "agent_version": "v1",
            "agent_name": "codex",
            "job_name": "canary",
        },
        {
            "job_id": "j2",
            "trial_id": "t3",
            "task_name": "local-lab/event-summary",
            "primary_reward": 1.0,
            "exception_class": None,
            "exception_phase": None,
            "agent_version": "v1",
            "agent_name": "oracle",
            "job_name": "ctrl",
        },
        {
            "job_id": "j2",
            "trial_id": "t4",
            "task_name": "local-lab/event-summary",
            "primary_reward": None,
            "exception_class": "NonZeroAgentExitCodeError",
            "exception_phase": "unknown",
            "agent_version": "v1",
            "agent_name": "oracle",
            "job_name": "ctrl",
        },
    )
    for trial in fixture_trials:
        trial_dir = (
            fixture_root / f"job_id={trial['job_id']}" / f"trial_id={trial['trial_id']}"
        )
        trial_dir.mkdir(parents=True)
        tbl = pa.table(
            {
                "task_name": pa.array([trial["task_name"]], type=pa.string()),
                "primary_reward": pa.array([trial["primary_reward"]], type=pa.float64()),
                "exception_class": pa.array(
                    [trial["exception_class"]], type=pa.string()
                ),
                "exception_phase": pa.array(
                    [trial["exception_phase"]], type=pa.string()
                ),
                "trial_id": pa.array([trial["trial_id"]], type=pa.string()),
                "agent_version": pa.array([trial["agent_version"]], type=pa.string()),
                "agent_name": pa.array([trial["agent_name"]], type=pa.string()),
                "job_name": pa.array([trial["job_name"]], type=pa.string()),
            }
        )
        pq.write_table(tbl, trial_dir / "trial_facts.parquet")

    def _fake_derived_root(_repo_root: Path) -> Path:
        return fixture_root

    monkeypatch.setattr(
        "evallab.paths.derived_root_from_environment", _fake_derived_root
    )

    repo_root = Path.cwd()
    derived_root = evallab.paths.derived_root_from_environment(repo_root)
    trial_glob = str(derived_root / "job_id=*/trial_id=*/trial_facts.parquet")

    with duckdb.connect(":memory:") as con:
        con.execute(
            f"CREATE TABLE trial_evidence AS "
            f"SELECT * FROM read_parquet('{trial_glob}', union_by_name=true)"
        )
        sql = Path("sql/evidence_queries.sql").read_text()
        con.execute(sql)

        total_trials = con.execute("SELECT count(*) FROM trial_evidence").fetchone()[0]
        assert total_trials == 4

        measured_count, never_measured_count = con.execute(
            "SELECT "
            "sum(CASE WHEN exception_class IS NULL THEN 1 ELSE 0 END), "
            "sum(CASE WHEN exception_class IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM trial_evidence"
        ).fetchone()
        assert measured_count == 2
        assert never_measured_count == 2

        summary_rows = con.execute(
            "SELECT task_name, n, never_measured, measured, passes, scored_failures "
            "FROM v_task_summary"
        ).fetchall()
        summary = {r[0]: r[1:] for r in summary_rows}

        assert "terminal-bench/html-js-filter" in summary
        assert "local-lab/event-summary" in summary
        assert summary["terminal-bench/html-js-filter"] == (2, 1, 1, 0, 1)
        assert summary["local-lab/event-summary"] == (2, 1, 1, 1, 0)

        # coverage: every task must report never_measured >0 and measured >0
        for task_name, (n, never_m, measured, _p, _f) in summary.items():
            assert n == never_m + measured
            assert never_m > 0, f"Task {task_name} must have non-zero never_measured trials"
            assert measured > 0, f"Task {task_name} must have non-zero measured trials"

        tax_rows = con.execute(
            "SELECT exception_class, exception_phase, n, tasks_affected FROM v_exception_taxonomy"
        ).fetchall()
        total_exceptions = sum(r[2] for r in tax_rows)
        assert total_exceptions == 2


@pytest.mark.skipif(
    not real_corpus_present,
    reason=f"real corpus at {derived_for_check} absent in CI (gitignored; use fixture)"
)
def test_full_corpus_derived_parquet_coverage_real() -> None:
    """Test queries observe 92-trial corpus (skipped when absent)."""
    repo_root = Path.cwd()
    derived_root = evallab.paths.derived_root_from_environment(repo_root)
    trial_glob = str(derived_root / "job_id=*/trial_id=*/trial_facts.parquet")

    with duckdb.connect(":memory:") as con:
        con.execute(
            f"CREATE TABLE trial_evidence AS "
            f"SELECT * FROM read_parquet('{trial_glob}', union_by_name=true)"
        )
        sql = Path("sql/evidence_queries.sql").read_text()
        con.execute(sql)

        # 1. Total trial count must equal 92 rows in trial_facts.parquet
        total_trials = con.execute("SELECT count(*) FROM trial_evidence").fetchone()[0]
        assert total_trials == 92, f"Expected 92 corpus trials, got {total_trials}"

        # 2. Task summary counts
        summary_rows = con.execute(
            "SELECT task_name, n, never_measured, measured, passes, scored_failures "
            "FROM v_task_summary"
        ).fetchall()
        summary = {r[0]: r[1:] for r in summary_rows}

        assert "local-lab/event-summary" in summary
        assert "petermakhnatch/transaction-reconciliation" in summary
        assert "terminal-bench/html-js-filter" in summary

        assert summary["local-lab/event-summary"] == (67, 3, 64, 62, 2)
        assert summary["petermakhnatch/transaction-reconciliation"] == (13, 7, 6, 6, 0)
        assert summary["terminal-bench/html-js-filter"] == (12, 6, 6, 0, 6)

        # 3. Tasks with both measured and never-measured trials must report both > 0
        for task_name, (n, never_m, measured, _p, _f) in summary.items():
            assert n == never_m + measured
            assert never_m > 0, f"Task {task_name} must have non-zero never_measured trials"
            assert measured > 0, f"Task {task_name} must have non-zero measured trials"

        # 4. Exception taxonomy asserts
        tax_rows = con.execute(
            "SELECT exception_class, exception_phase, n, tasks_affected FROM v_exception_taxonomy"
        ).fetchall()
        taxonomy = {r[0]: (r[1], r[2], r[3]) for r in tax_rows}

        assert taxonomy["ValueError"] == ("unknown", 9, 3)
        assert taxonomy["NonZeroAgentExitCodeError"] == ("unknown", 7, 2)
        total_exceptions = sum(r[2] for r in tax_rows)
        assert total_exceptions == 16


def test_promoted_only_subset_fails_coverage_assertions() -> None:
    """Assert that a promoted-bundles-only dataset fails the full-corpus requirements."""
    sql = Path("sql/evidence_queries.sql").read_text()
    with duckdb.connect(":memory:") as con:
        # Synthetic promoted-only data (11 trials, 0 exceptions)
        con.execute(
            """
            CREATE TABLE trial_evidence_schema_fallback (
                experiment_id VARCHAR, job_id VARCHAR, trial_id VARCHAR,
                job_name VARCHAR, trial_name VARCHAR, task_name VARCHAR,
                task_digest VARCHAR, verifier_digest VARCHAR, environment_digest VARCHAR,
                agent_config_digest VARCHAR, agent_name VARCHAR, agent_version VARCHAR,
                model_name VARCHAR, primary_reward DOUBLE,
                exception_class VARCHAR, exception_phase VARCHAR,
                duration_seconds DOUBLE, cost_usd DOUBLE
            );
            INSERT INTO trial_evidence_schema_fallback VALUES
            ('e','j1','t1','canary','t1','local-lab/event-summary',NULL,NULL,NULL,NULL,'codex','v1','m',1.0,NULL,NULL,1.0,0.0),
            ('e','j1','t2','canary','t2','local-lab/event-summary',NULL,NULL,NULL,NULL,'codex','v1','m',1.0,NULL,NULL,1.0,0.0),
            ('e','j1','t3','canary','t3','local-lab/event-summary',NULL,NULL,NULL,NULL,'codex','v1','m',1.0,NULL,NULL,1.0,0.0),
            ('e','j2','t4','canary','t4','petermakhnatch/transaction-reconciliation',NULL,NULL,NULL,NULL,'codex','v1','m',1.0,NULL,NULL,1.0,0.0),
            ('e','j2','t5','canary','t5','petermakhnatch/transaction-reconciliation',NULL,NULL,NULL,NULL,'codex','v1','m',1.0,NULL,NULL,1.0,0.0),
            ('e','j2','t6','canary','t6','petermakhnatch/transaction-reconciliation',NULL,NULL,NULL,NULL,'codex','v1','m',1.0,NULL,NULL,1.0,0.0),
            ('e','j3','t7','canary','t7','terminal-bench/html-js-filter',NULL,NULL,NULL,NULL,'codex','v1','m',0.0,NULL,NULL,1.0,0.0),
            ('e','j3','t8','canary','t8','terminal-bench/html-js-filter',NULL,NULL,NULL,NULL,'codex','v1','m',0.0,NULL,NULL,1.0,0.0),
            ('e','j3','t9','canary','t9','terminal-bench/html-js-filter',NULL,NULL,NULL,NULL,'codex','v1','m',0.0,NULL,NULL,1.0,0.0),
            ('e','j4','t10','ctrl','t10','local-lab/event-summary',NULL,NULL,NULL,NULL,'oracle','v1','m',1.0,NULL,NULL,1.0,0.0),
            ('e','j5','t11','ctrl','t11','local-lab/event-summary',NULL,NULL,NULL,NULL,'nop','v1','m',0.0,NULL,NULL,1.0,0.0);
            """
        )
        con.execute(sql)
        total_trials = con.execute("SELECT count(*) FROM trial_evidence").fetchone()[0]
        # Promoted subset has only 11 trials instead of 92
        assert total_trials != 92

        summary = dict(
            con.execute("SELECT task_name, never_measured FROM v_task_summary").fetchall()
        )
        # Promoted subset has 0 never_measured trials for html-js-filter (fails non-zero check)
        assert summary["terminal-bench/html-js-filter"] == 0
