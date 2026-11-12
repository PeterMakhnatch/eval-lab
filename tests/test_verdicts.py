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
from evallab.schemas import Verdict
from evallab.verdicts import (
    DEFAULT_DISCOVERIES_PATH,
    SQL_VERDICTS_PATH,
    format_verdict_history_table,
    format_verdicts_table,
    get_verdict_history_from_catalog,
    list_current_verdicts_from_catalog,
    record_verdict,
    resolve_discovery_ids,
    validate_discovery_id,
    validate_human_actor,
    validate_status,
)

SAMPLE_DISCOVERY_HEADER_ID = "D-20260815-KTXJSHGZ"
SAMPLE_DISCOVERY_HEADER_ID_2 = "D-20260816-7CQRVDQ6"
SAMPLE_CITATION_ULID = "01KZZCK33HJM4R8HW3V0Y25DXE"


def _create_sample_discoveries_journal(root: Path) -> Path:
    journal_path = root / DEFAULT_DISCOVERIES_PATH
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        f"""# Eval lab discovery journal

## {SAMPLE_DISCOVERY_HEADER_ID} — draft

- Claim: Across this small control-only cohort, verifiers showed expected pattern.
- Builds on: new thread
- Evidence:
  - [queue/researchers/passes/2026-08-15/{SAMPLE_CITATION_ULID}/evidence.json](../evidence.json)
- Proposed spec: [queue/proposed/spec-01KZZCN7X9PA643W1QCKQNNNY5.json](../spec.json)

## {SAMPLE_DISCOVERY_HEADER_ID_2} — draft

- Claim: A verified second finding for test cohorts.
- Builds on: {SAMPLE_DISCOVERY_HEADER_ID}
""",
        encoding="utf-8",
    )
    return journal_path


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


def test_refuse_unknown_discovery_id(tmp_path: Path) -> None:
    """A verdict on an unknown discovery_id is refused, naming what was not found."""
    _create_sample_discoveries_journal(tmp_path)
    unknown_id = "D-20260815-NONEXIST"

    with pytest.raises(ValueError, match=f"Discovery '{unknown_id}' not found"):
        validate_discovery_id(unknown_id, repo_root=tmp_path)


def test_refuse_malformed_discovery_id(tmp_path: Path) -> None:
    """Malformed discovery IDs are refused at validation time."""
    _create_sample_discoveries_journal(tmp_path)
    for bad_id in [
        "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "not-a-discovery-id",
        "D-2026081-SHORT",
        "20260815-KTXJSHGZ",
    ]:
        with pytest.raises(ValueError):
            record_verdict(
                bad_id,
                "accepted",
                by="Peter Makhnatch",
                repo_root=tmp_path,
            )


def test_refuse_invalid_status() -> None:
    """A status outside the four §2.1 literals is refused."""
    for bad_status in ["maybe", "approved", "failed", "unreviewed", ""]:
        with pytest.raises(ValueError, match="Invalid status .* must be one of:"):
            validate_status(bad_status)


def test_resolve_discovery_ids_parsing(tmp_path: Path) -> None:
    """Discovery IDs are correctly extracted from headers and embedded citations."""
    _create_sample_discoveries_journal(tmp_path)
    ids = resolve_discovery_ids(repo_root=tmp_path)

    assert SAMPLE_DISCOVERY_HEADER_ID in ids
    assert SAMPLE_DISCOVERY_HEADER_ID_2 in ids
    assert SAMPLE_CITATION_ULID in ids
    assert "01KZZCN7X9PA643W1QCKQNNNY5" in ids


def test_format_verdicts_table() -> None:
    """Formatting renders headers and rows or empty message."""
    assert format_verdicts_table([]) == "No verdicts recorded."

    now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
    v = [
        Verdict(
            discovery_id=SAMPLE_DISCOVERY_HEADER_ID,
            status="accepted",
            by="Peter Makhnatch",
            at=now,
            note="Note here",
        )
    ]
    rendered = format_verdicts_table(v)
    assert "DISCOVERY ID" in rendered
    assert "STATUS" in rendered
    assert SAMPLE_DISCOVERY_HEADER_ID in rendered
    assert "accepted" in rendered
    assert "Peter Makhnatch" in rendered


def test_format_verdict_history_table() -> None:
    """Formatting history table renders chronological entries."""
    assert (
        format_verdict_history_table(SAMPLE_DISCOVERY_HEADER_ID, [])
        == f"No verdict history for {SAMPLE_DISCOVERY_HEADER_ID}."
    )

    t1 = datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
    history = [
        Verdict(
            discovery_id=SAMPLE_DISCOVERY_HEADER_ID,
            status="pending",
            by="Peter Makhnatch",
            at=t1,
            note="First",
        ),
        Verdict(
            discovery_id=SAMPLE_DISCOVERY_HEADER_ID,
            status="accepted",
            by="Peter Makhnatch",
            at=t2,
            note="Second",
        ),
    ]
    rendered = format_verdict_history_table(SAMPLE_DISCOVERY_HEADER_ID, history)
    assert f"History for {SAMPLE_DISCOVERY_HEADER_ID}:" in rendered
    assert "pending" in rendered
    assert "accepted" in rendered


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

    # 2. get_verdict_history_from_catalog fails with non-zero exit and informative stderr
    with pytest.raises(SystemExit) as exc_hist:
        get_verdict_history_from_catalog(
            SAMPLE_DISCOVERY_HEADER_ID, database_url=unreachable_url
        )
    assert exc_hist.value.code == 2
    out_hist, err_hist = capsys.readouterr()
    assert f"No verdict history for {SAMPLE_DISCOVERY_HEADER_ID}." not in out_hist
    assert f"No verdict history for {SAMPLE_DISCOVERY_HEADER_ID}." not in err_hist
    assert "catalog read failed [unavailable]" in err_hist
    assert "127.0.0.1:59999" in err_hist
    assert "v_verdicts_history" in err_hist


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
    _create_sample_discoveries_journal(tmp_path)

    now = datetime.now(UTC)
    verdict = record_verdict(
        SAMPLE_DISCOVERY_HEADER_ID,
        "accepted",
        by="Peter Makhnatch",
        note="Catalog roundtrip test",
        at=now,
        repo_root=tmp_path,
        database_url=db_url,
    )
    assert verdict.status == "accepted"

    current = list_current_verdicts_from_catalog(database_url=db_url)
    matching = [v for v in current if v.discovery_id == SAMPLE_DISCOVERY_HEADER_ID]
    assert len(matching) == 1
    assert matching[0].status == "accepted"
    assert matching[0].by == "Peter Makhnatch"
    assert matching[0].note == "Catalog roundtrip test"

    history = get_verdict_history_from_catalog(SAMPLE_DISCOVERY_HEADER_ID, database_url=db_url)
    assert len(history) >= 1
    assert history[-1].status == "accepted"
    assert history[-1].note == "Catalog roundtrip test"


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

    _create_sample_discoveries_journal(tmp_path)
    now = datetime.now(UTC)

    # Perform writes to the isolated database
    verdict = record_verdict(
        SAMPLE_DISCOVERY_HEADER_ID,
        "accepted",
        by="Peter Makhnatch",
        note="Isolation verification test",
        at=now,
        repo_root=tmp_path,
        database_url=isolated_database_url,
    )
    assert verdict.status == "accepted"

    current = list_current_verdicts_from_catalog(database_url=isolated_database_url)
    assert len(current) >= 1
    matching = [v for v in current if v.discovery_id == SAMPLE_DISCOVERY_HEADER_ID]
    assert len(matching) == 1
    assert matching[0].status == "accepted"

    history = get_verdict_history_from_catalog(
        SAMPLE_DISCOVERY_HEADER_ID, database_url=isolated_database_url
    )
    assert len(history) >= 1
    assert history[-1].status == "accepted"

    # Record live verdicts count after
    with psycopg.connect(live_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM verdicts")
        count_after = cur.fetchone()[0]

    assert count_before == count_after, (
        f"Live catalog polluted: count changed from {count_before} to {count_after}"
    )
