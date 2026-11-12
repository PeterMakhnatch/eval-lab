"""Tests for discovery verdict persistence, validation, and views (§2.1, §2.2).

Verdicts are append-only human judgements on discovery findings.
CI has no PostgreSQL or derived corpus — tests use tmp_path and in-memory DuckDB.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import duckdb
import pytest

from evallab.runner import database_url_from_environment
from evallab.verdicts import (
    SQL_VERDICTS_PATH,
    list_current_verdicts_from_catalog,
    validate_human_actor,
    validate_status,
)

SAMPLE_DISCOVERY_HEADER_ID = "D-20260815-KTXJSHGZ"
SAMPLE_DISCOVERY_HEADER_ID_2 = "D-20260816-7CQRVDQ6"
SAMPLE_CITATION_ULID = "01KZZCK33HJM4R8HW3V0Y25DXE"


def test_standalone_sql_script_with_fallbacks() -> None:
    """Test that sql/verdicts.sql resolves in clean DuckDB with zero pre-created tables."""
    sql = SQL_VERDICTS_PATH.read_text(encoding="utf-8")
    with duckdb.connect(":memory:") as con:
        con.execute(sql)
        for view_name in ["v_verdicts_history", "v_current_verdicts"]:
            rows = con.execute(f"SELECT * FROM {view_name}").fetchall()
            assert rows == []


def test_refuse_empty_or_whitespace_actor() -> None:
    """An empty or whitespace-only actor is refused."""
    for bad_actor in ["", "   ", "\t\n"]:
        with pytest.raises(ValueError, match="Actor \\(--by\\) is required and cannot be empty"):
            validate_human_actor(bad_actor)


def test_refuse_automated_actor() -> None:
    """An obviously-automated actor name is refused with a clear guidance message."""
    automated_names = [
        "autopilot",
        "autopilot-researcher",
        "bot",
        "agent",
        "harbor",
        "automated",
        "ci",
        "github-actions",
        "codex",
        "oracle",
        "nop",
        "system",
        "agent-worker",
        "bot-auto",
        "ai-assistant",
        "automated-pipeline",
        "harbor-evaluator",
        "codex-judge",
    ]
    for bad_actor in automated_names:
        with pytest.raises(
            ValueError, match="Automated actor .* refused: verdicts require human judgment"
        ):
            validate_human_actor(bad_actor)


def test_refuse_invalid_status() -> None:
    """A status outside the four §2.1 literals is refused."""
    for bad_status in ["maybe", "approved", "failed", "unreviewed", ""]:
        with pytest.raises(ValueError, match="Invalid status .* must be one of:"):
            validate_status(bad_status)


def test_unreachable_catalog_fails_distinctly(capsys: pytest.CaptureFixture[str]) -> None:
    """An unreachable catalog produces a distinguishable failure with a reason, not empty."""
    unreachable_url = "postgresql://127.0.0.1:59999/evallab_unreachable"

    # 1. list_current_verdicts_from_catalog fails with non-zero exit and informative stderr
    with pytest.raises(SystemExit) as exc_list:
        list_current_verdicts_from_catalog(database_url=unreachable_url)
    assert exc_list.value.code == 2
    out_list, err_list = capsys.readouterr()
    assert "No verdicts recorded." not in out_list
    assert "No verdicts recorded." not in err_list
    assert "catalog read failed [unavailable]" in err_list
    assert "127.0.0.1:59999" in err_list
    assert "v_current_verdicts" in err_list


_DSN_FOR_TEST = database_url_from_environment()


def _catalog_reachable(dsn: str | None = None) -> bool:
    try:
        import psycopg

        url = dsn or _DSN_FOR_TEST
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
    if not _catalog_reachable():
        yield _DSN_FOR_TEST
        return

    import psycopg

    base_url = _DSN_FOR_TEST
    db_name = f"test_verdicts_{uuid4().hex[:12]}"
    target_url = _derive_isolated_db_url(base_url, db_name)

    with psycopg.connect(base_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')

    try:
        yield target_url
    finally:
        with psycopg.connect(base_url, autocommit=True) as conn:
            try:
                conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
            except Exception:
                conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')


@pytest.fixture(autouse=True)
def _isolate_database_env(
    isolated_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure DATABASE_URL environment variable points to the isolated test database."""
    if _catalog_reachable():
        monkeypatch.setenv("DATABASE_URL", isolated_database_url)


@pytest.mark.skipif(
    not _catalog_reachable(),
    reason=f"requires live PostgreSQL {_DSN_FOR_TEST}",
)
def test_postgres_catalog_roundtrip_if_available(
    tmp_path: Path, isolated_database_url: str
) -> None:
    """Full PostgreSQL catalog roundtrip when DATABASE_URL is reachable."""
    db_url = isolated_database_url

    now = datetime.now(UTC)
    import psycopg

    from evallab.database import initialize
    initialize(db_url)
    with psycopg.connect(db_url) as connection:
        connection.execute('INSERT INTO verdicts (discovery_id, status, "by", "at", note) VALUES (%s, %s, %s, %s, %s)', (SAMPLE_DISCOVERY_HEADER_ID, "accepted", "Peter Makhnatch", now, 'Catalog roundtrip test'))

    current = list_current_verdicts_from_catalog(database_url=db_url)
    matching = [v for v in current if v.discovery_id == SAMPLE_DISCOVERY_HEADER_ID]
    assert len(matching) == 1
    assert matching[0].status == "accepted"
    assert matching[0].by == "Peter Makhnatch"
    assert matching[0].note == "Catalog roundtrip test"


@pytest.mark.skipif(
    not _catalog_reachable(),
    reason=f"requires live PostgreSQL {_DSN_FOR_TEST}",
)
def test_postgres_verdict_writes_leave_live_catalog_untouched(
    tmp_path: Path, isolated_database_url: str
) -> None:
    """Live verdicts row count is recorded before/after Postgres writes and asserted unchanged."""
    import psycopg

    live_url = _DSN_FOR_TEST

    # Record live verdicts count before
    with psycopg.connect(live_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM verdicts")
        count_before = cur.fetchone()[0]

    now = datetime.now(UTC)

    # Perform writes to the isolated database
    import psycopg

    from evallab.database import initialize
    initialize(isolated_database_url)
    with psycopg.connect(isolated_database_url) as connection:
        connection.execute('INSERT INTO verdicts (discovery_id, status, "by", "at", note) VALUES (%s, %s, %s, %s, %s)', (SAMPLE_DISCOVERY_HEADER_ID, "accepted", "Peter Makhnatch", now, 'Isolation verification test'))

    current = list_current_verdicts_from_catalog(database_url=isolated_database_url)
    assert len(current) >= 1
    matching = [v for v in current if v.discovery_id == SAMPLE_DISCOVERY_HEADER_ID]
    assert len(matching) == 1
    assert matching[0].status == "accepted"

    # Record live verdicts count after
    with psycopg.connect(live_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM verdicts")
        count_after = cur.fetchone()[0]

    assert count_before == count_after, (
        f"Live catalog polluted: count changed from {count_before} to {count_after}"
    )

