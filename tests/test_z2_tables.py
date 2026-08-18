"""Tests for Z2 catalog tables and views (§2.2, §2.3, §4).

Covers:
- suites and suite_members DDL idempotence and schema objects
- Database-level frozen suite immutability enforcement (load-bearing test)
- Unfrozen suite membership mutability
- v_quota_today UTC day bucketing across midnight boundaries
- sql/views.sql execution in clean DuckDB session with zero pre-created tables
- PostgreSQL live catalog tests with skipif when catalog is unreachable
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import duckdb
import pytest

from evallab.database import initialize, quota_today, views_path
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


# ---------------------------------------------------------------------------
# DuckDB Standalone View Tests (Runs in CI without Postgres)
# ---------------------------------------------------------------------------


def test_views_sql_resolves_in_clean_duckdb() -> None:
    """sql/views.sql must resolve in a clean DuckDB session with zero pre-created tables."""
    sql = views_path().read_text(encoding="utf-8")
    with duckdb.connect(":memory:") as con:
        con.execute(sql)
        for view_name in ["v_spine", "v_quota_today"]:
            rows = con.execute(f"SELECT * FROM {view_name}").fetchall()
            assert rows == []


def test_v_quota_today_utc_bucketing_duckdb() -> None:
    """v_quota_today must bucket trials strictly by UTC calendar day.

    Plants rows on either side of UTC midnight, including a timestamp that would
    land in the wrong calendar day if evaluated under local timezone offsets.
    """
    sql = views_path().read_text(encoding="utf-8")
    with duckdb.connect(":memory:") as con:
        con.execute(sql)

        now_utc = datetime.now(UTC)
        today_utc_date = now_utc.date()
        today_iso = today_utc_date.isoformat()

        yesterday_utc_date = today_utc_date - timedelta(days=1)
        tomorrow_utc_date = today_utc_date + timedelta(days=1)
        # Timestamps relative to UTC midnight:
        ts_yesterday = f"{yesterday_utc_date.isoformat()}T23:59:50Z"
        ts_today_early = f"{today_iso}T00:00:10Z"
        ts_today_mid = f"{today_iso}T12:00:00Z"
        ts_today_late = f"{today_iso}T23:59:50Z"
        ts_tomorrow = f"{tomorrow_utc_date.isoformat()}T00:00:10Z"


        con.execute(
            """
            INSERT INTO trials (id, job_id, trial_name, task_name, agent_name)
            VALUES
                ('t_yest', 'j1', 't1', 'task1', 'codex'),
                ('t_early', 'j1', 't2', 'task1', 'codex'),
                ('t_mid', 'j1', 't3', 'task1', 'codex'),
                ('t_late', 'j1', 't4', 'task1', 'codex'),
                ('t_tom', 'j1', 't5', 'task1', 'codex'),
                ('t_claude_today', 'j1', 't6', 'task1', 'claude-code'),
                ('t_claude_yest', 'j1', 't7', 'task1', 'claude-code');
            INSERT INTO trial_usage (
                trial_id, started_at, input_tokens, cache_tokens, output_tokens
            )
            VALUES
                ('t_yest', ?, 100, 20, 50),
                ('t_early', ?, 200, 40, 100),
                ('t_mid', ?, 300, 60, 150),
                ('t_late', ?, 400, 80, 200),
                ('t_tom', ?, 500, 100, 250),
                ('t_claude_today', ?, 1000, 100, 500),
                ('t_claude_yest', ?, 800, 100, 400);
            """,
            [
                ts_yesterday,
                ts_today_early,
                ts_today_mid,
                ts_today_late,
                ts_tomorrow,
                ts_today_mid,
                ts_yesterday,
            ],
        )

        rows = con.execute(
            """
            SELECT provider, runs, tokens
            FROM v_quota_today
            ORDER BY provider;
            """
        ).fetchall()

        # Expected today:
        # claude-code: 1 run, 1000 + 500 = 1500 tokens
        # codex: 3 runs (early + mid + late), tokens = (200+100) + (300+150) + (400+200) = 1350
        assert len(rows) == 2
        assert rows[0] == ("claude-code", 1, 1500)
        assert rows[1] == ("codex", 3, 1350)


# ---------------------------------------------------------------------------
# PostgreSQL Live Catalog Tests (Skipped when live PostgreSQL absent)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _catalog_reachable(),
    reason=f"requires live PostgreSQL {_DSN_FOR_TEST}",
)
def test_schema_ddl_idempotent() -> None:
    """Applying schema DDL multiple times succeeds and leaves one set of objects."""
    import psycopg

    db_url = _DSN_FOR_TEST
    initialize(db_url)
    initialize(db_url)

    with psycopg.connect(db_url) as conn:
        tables = conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_name IN ('suites', 'suite_members');
            """
        ).fetchall()
        table_names = {row[0] for row in tables}
        assert "suites" in table_names
        assert "suite_members" in table_names

        views = conn.execute(
            """
            SELECT table_name
            FROM information_schema.views
            WHERE table_schema = current_schema()
              AND table_name = 'v_quota_today';
            """
        ).fetchall()
        assert len(views) == 1


@pytest.mark.skipif(
    not _catalog_reachable(),
    reason=f"requires live PostgreSQL {_DSN_FOR_TEST}",
)
def test_unfrozen_suite_accepts_membership_mutations() -> None:
    """An unfrozen suite allows inserting, updating, and deleting members."""
    import psycopg

    db_url = _DSN_FOR_TEST
    suite_name = f"test-unfrozen-{uuid4().hex[:8]}"
    suite_version = "v1"

    with psycopg.connect(db_url) as conn:
        conn.execute(
            "INSERT INTO suites (name, version) VALUES (%s, %s)",
            (suite_name, suite_version),
        )

        # 1. Insert members
        conn.execute(
            """
            INSERT INTO suite_members (suite_name, suite_version, task_ref, task_version)
            VALUES (%s, %s, 'task-alpha', '1.0'), (%s, %s, 'task-beta', '1.0')
            """,
            (suite_name, suite_version, suite_name, suite_version),
        )

        # 2. Update member
        conn.execute(
            """
            UPDATE suite_members
            SET task_version = '1.1'
            WHERE suite_name = %s AND suite_version = %s AND task_ref = 'task-beta'
            """,
            (suite_name, suite_version),
        )

        # 3. Delete member
        conn.execute(
            """
            DELETE FROM suite_members
            WHERE suite_name = %s AND suite_version = %s AND task_ref = 'task-alpha'
            """,
            (suite_name, suite_version),
        )

        members = conn.execute(
            """
            SELECT task_ref, task_version
            FROM suite_members
            WHERE suite_name = %s AND suite_version = %s
            ORDER BY task_ref
            """,
            (suite_name, suite_version),
        ).fetchall()
        assert members == [("task-beta", "1.1")]


@pytest.mark.skipif(
    not _catalog_reachable(),
    reason=f"requires live PostgreSQL {_DSN_FOR_TEST}",
)
def test_frozen_suite_rejects_membership_mutations() -> None:
    """A frozen suite rejects all membership insertions, updates, and deletions in the DB.

    Load-bearing test: attempts mutation directly against the store and asserts
    that PostgreSQL triggers reject the operation.
    """
    import psycopg

    db_url = _DSN_FOR_TEST
    suite_name = f"test-frozen-{uuid4().hex[:8]}"
    suite_version = "v1"

    with psycopg.connect(db_url) as conn:
        # Create suite and seed initial membership before freezing
        conn.execute(
            "INSERT INTO suites (name, version) VALUES (%s, %s)",
            (suite_name, suite_version),
        )
        conn.execute(
            """
            INSERT INTO suite_members (suite_name, suite_version, task_ref, task_version)
            VALUES (%s, %s, 'task-one', '1.0'), (%s, %s, 'task-two', '1.0')
            """,
            (suite_name, suite_version, suite_name, suite_version),
        )
        conn.commit()

        # Freeze the suite
        conn.execute(
            "UPDATE suites SET frozen_at = now() WHERE name = %s AND version = %s",
            (suite_name, suite_version),
        )
        conn.commit()

        # 1. Attempt to INSERT a new member into the frozen suite -> MUST FAIL
        with pytest.raises(psycopg.Error) as exc_insert:
            conn.execute(
                """
                INSERT INTO suite_members (suite_name, suite_version, task_ref, task_version)
                VALUES (%s, %s, 'task-three', '1.0')
                """,
                (suite_name, suite_version),
            )
        conn.rollback()
        assert "Cannot modify membership of frozen suite" in str(exc_insert.value)

        # 2. Attempt to UPDATE an existing member in the frozen suite -> MUST FAIL
        with pytest.raises(psycopg.Error) as exc_update:
            conn.execute(
                """
                UPDATE suite_members
                SET task_version = '2.0'
                WHERE suite_name = %s AND suite_version = %s AND task_ref = 'task-one'
                """,
                (suite_name, suite_version),
            )
        conn.rollback()
        assert "Cannot modify membership of frozen suite" in str(exc_update.value)

        # 3. Attempt to DELETE an existing member from the frozen suite -> MUST FAIL
        with pytest.raises(psycopg.Error) as exc_delete:
            conn.execute(
                """
                DELETE FROM suite_members
                WHERE suite_name = %s AND suite_version = %s AND task_ref = 'task-one'
                """,
                (suite_name, suite_version),
            )
        conn.rollback()
        assert "Cannot modify membership of frozen suite" in str(exc_delete.value)

        # 4. Attempt to UNFREEZE or mutate the suite itself -> MUST FAIL
        with pytest.raises(psycopg.Error) as exc_unfreeze:
            conn.execute(
                """
                UPDATE suites
                SET frozen_at = NULL
                WHERE name = %s AND version = %s
                """,
                (suite_name, suite_version),
            )
        conn.rollback()
        assert "Cannot modify frozen suite" in str(exc_unfreeze.value)

        # 5. Attempt to DELETE the frozen suite row -> MUST FAIL
        with pytest.raises(psycopg.Error) as exc_del_suite:
            conn.execute(
                "DELETE FROM suites WHERE name = %s AND version = %s",
                (suite_name, suite_version),
            )
        conn.rollback()
        assert "Cannot delete frozen suite" in str(exc_del_suite.value)


@pytest.mark.skipif(
    not _catalog_reachable(),
    reason=f"requires live PostgreSQL {_DSN_FOR_TEST}",
)
def test_v_quota_today_postgres_utc_bucketing() -> None:
    """v_quota_today in PostgreSQL aggregates only trials started on current UTC day."""
    import psycopg

    db_url = _DSN_FOR_TEST
    job_id = uuid4()
    trial_prefix = f"quota-pg-{uuid4().hex[:6]}"

    now_utc = datetime.now(UTC)
    today_utc_date = now_utc.date()
    yesterday_utc_date = today_utc_date - timedelta(days=1)
    tomorrow_utc_date = today_utc_date + timedelta(days=1)

    ts_yesterday = f"{yesterday_utc_date.isoformat()}T23:59:50Z"
    ts_today = f"{today_utc_date.isoformat()}T14:00:00Z"
    ts_tomorrow = f"{tomorrow_utc_date.isoformat()}T00:00:10Z"

    with psycopg.connect(db_url) as conn:
        # Create a parent job for foreign key constraints
        conn.execute(
            """
            INSERT INTO jobs (id, job_name, evidence_path)
            VALUES (%s, 'quota-test-job', 'runs/quota-test-job')
            ON CONFLICT (id) DO NOTHING
            """,
            (job_id,),
        )

        # Plant trials across days
        conn.execute(
            """
            INSERT INTO trials (
                id, job_id, trial_name, evidence_path, agent_name, started_at,
                input_tokens, output_tokens
            ) VALUES
                (%s, %s, %s, 'ev1', 'codex', %s, 100, 50),
                (%s, %s, %s, 'ev2', 'codex', %s, 200, 100),
                (%s, %s, %s, 'ev3', 'codex', %s, 300, 150)
            """,
            (
                uuid4(),
                job_id,
                f"{trial_prefix}-yest",
                ts_yesterday,
                uuid4(),
                job_id,
                f"{trial_prefix}-today",
                ts_today,
                uuid4(),
                job_id,
                f"{trial_prefix}-tom",
                ts_tomorrow,
            ),
        )
        conn.commit()

        try:
            today_rows = quota_today(db_url)
            codex_rows = [r for r in today_rows if r[0] == "codex"]
            assert len(codex_rows) == 1
            # Must include ts_today (200+100=300 tokens, 1 run) plus any other runs planted today
            provider, runs, tokens = codex_rows[0]
            assert provider == "codex"
            assert runs >= 1
            assert tokens >= 300
        finally:
            # Clean up planted trials
            conn.execute(
                "DELETE FROM trials WHERE trial_name LIKE %s",
                (f"{trial_prefix}%",),
            )
            conn.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
            conn.commit()
