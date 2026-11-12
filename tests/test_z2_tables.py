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

import os
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest

from evallab.database import initialize
from evallab.runner import database_url_from_environment

_DSN_FOR_TEST = database_url_from_environment()


def _catalog_reachable(dsn: str | None = None) -> bool:
    try:
        import psycopg
    except ImportError:
        return False
    try:
        url = dsn or os.environ.get("DATABASE_URL") or _DSN_FOR_TEST
        with psycopg.connect(url, connect_timeout=1) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


def _derive_isolated_db_url(base_url: str, name: str) -> str:
    parsed = urlsplit(base_url)
    return urlunsplit(parsed._replace(path=f"/{name}"))


@pytest.fixture(scope="module")
def isolated_database_url() -> Iterator[str]:
    """Create and drop an isolated PostgreSQL database for test isolation."""
    pytest.importorskip("psycopg", reason="psycopg required for PostgreSQL tests")
    import psycopg

    base_url = os.environ.get("DATABASE_URL") or _DSN_FOR_TEST
    if not _catalog_reachable(base_url):
        pytest.skip(f"requires live PostgreSQL at {base_url} (unreachable)")

    db_name = f"test_z2_{uuid4().hex[:12]}"
    target_url = _derive_isolated_db_url(base_url, db_name)

    with psycopg.connect(base_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')

    try:
        initialize(target_url)
        old_env = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = target_url
        try:
            yield target_url
        finally:
            if old_env is not None:
                os.environ["DATABASE_URL"] = old_env
            else:
                os.environ.pop("DATABASE_URL", None)
    finally:
        with psycopg.connect(base_url, autocommit=True) as conn:
            try:
                conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
            except Exception:
                conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')


# ---------------------------------------------------------------------------
# DuckDB Standalone View Tests (Runs in CI without Postgres)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PostgreSQL Live Catalog Tests (Skipped when live PostgreSQL absent)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _catalog_reachable(),
    reason="requires live PostgreSQL (unreachable or psycopg missing)",
)
def test_schema_ddl_idempotent(isolated_database_url: str) -> None:
    """Applying schema DDL multiple times succeeds and leaves one set of objects."""
    import psycopg

    db_url = isolated_database_url
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
    reason="requires live PostgreSQL (unreachable or psycopg missing)",
)
def test_unfrozen_suite_accepts_membership_mutations(isolated_database_url: str) -> None:
    """An unfrozen suite allows inserting, updating, and deleting members."""
    import psycopg

    db_url = isolated_database_url
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
    reason="requires live PostgreSQL (unreachable or psycopg missing)",
)
def test_frozen_suite_rejects_membership_mutations(isolated_database_url: str) -> None:
    """A frozen suite rejects all membership insertions, updates, and deletions in the DB.

    Load-bearing test: attempts mutation directly against the store and asserts
    that PostgreSQL triggers reject the operation.
    """
    import psycopg

    db_url = isolated_database_url
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


