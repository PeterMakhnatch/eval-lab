"""Queue connection fallback preserves durable store ownership."""

from __future__ import annotations

import json
import sqlite3
import traceback
from pathlib import Path
from typing import Any

import psycopg
import pytest

from evallab.production.cli import open_production_queue
from evallab.production.corpus_queue import CorpusRegistry, ProductionQueueConnectionError

SECRET = "do-not-expose-connection-password"
POSTGRES = f"postgresql://operator:{SECRET}@unavailable.invalid/production"


@pytest.fixture
def registry(tmp_path: Path) -> CorpusRegistry:
    keys = tmp_path / "research/answer-keys"
    for dataset in ("tb4-audit-v1", "cheatbench-actions-v1", "hacktrace-act-v1"):
        path = keys / dataset / "items.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
    (keys / "tb4-audit-v1/tasks.jsonl").write_text("", encoding="utf-8")
    inventory = tmp_path / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(
        json.dumps(
            {
                "dataset_id": "mimo-defect-map-v1",
                "heldout_task_ids_excluded_from_training_all_versions": [],
                "tasks": [],
            }
        ),
        encoding="utf-8",
    )
    return CorpusRegistry(tmp_path)


def _append(queue, event_id: str = "public-event") -> dict[str, Any]:
    return queue.append_event(
        {
            "event_id": event_id,
            "run_id": "public-replay",
            "system": "misalignment",
            "event_type": "monitor.observed",
            "status": "confirmed",
            "payload": {"public_trace": "trace.jsonl", "step": 4},
        }
    )


class _PostgresIO:
    """Fake the PostgreSQL transport; persist queue SQL in an actual SQLite file."""

    def __init__(self, path: Path, *, fail: bool = False):
        self.connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.fail = fail
        self.closed = False

    def execute(self, sql, parameters=()):
        if self.fail:
            raise psycopg.OperationalError("PostgreSQL transport disconnected")
        if sql.startswith(("SELECT pg_advisory", "CREATE OR REPLACE FUNCTION", "DO $$")):
            sql = "SELECT 1"
        if sql == "BEGIN ISOLATION LEVEL REPEATABLE READ":
            sql = "BEGIN"
        return self.connection.execute(sql.replace("%s", "?"), parameters)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.closed = True
        self.connection.close()


def test_failed_initial_postgres_connect_uses_persistent_explicit_fallback(
    tmp_path, registry, monkeypatch
):
    def unavailable(database, *, autocommit, connect_timeout):
        assert connect_timeout == 3
        raise psycopg.OperationalError(SECRET)

    monkeypatch.setattr(psycopg, "connect", unavailable)
    fallback = tmp_path / "offline/local/queue.sqlite"
    with open_production_queue(POSTGRES, registry=registry, sqlite_fallback=fallback) as connection:
        assert connection.backend == "sqlite"
        assert "initial connection unavailable" in connection.fallback_reason
        assert SECRET not in connection.fallback_reason
        event = _append(connection.queue)
    with open_production_queue(fallback, registry=registry) as reopened:
        assert reopened.fallback_reason is None
        assert reopened.queue.pending_outbox() == [event]


def test_unavailable_postgres_without_fallback_reports_no_credentials(tmp_path, registry, monkeypatch):
    def unavailable(*args, **kwargs):
        raise psycopg.OperationalError(SECRET)

    monkeypatch.setattr(psycopg, "connect", unavailable)
    with pytest.raises(ProductionQueueConnectionError) as caught:
        open_production_queue(POSTGRES, registry=registry)
    assert SECRET not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("initialization_error", [False, True])
def test_programmer_or_postconnect_error_never_opens_fallback(
    tmp_path, registry, monkeypatch, initialization_error
):
    transport = _PostgresIO(tmp_path / "postgres-transport.sqlite", fail=True)

    def connect(*args, **kwargs):
        if initialization_error:
            return transport
        raise psycopg.ProgrammingError("invalid connection parameter")

    monkeypatch.setattr(psycopg, "connect", connect)
    fallback = tmp_path / "must-not-create/queue.sqlite"
    error = psycopg.OperationalError if initialization_error else psycopg.ProgrammingError
    with pytest.raises(error):
        open_production_queue(POSTGRES, registry=registry, sqlite_fallback=fallback)
    assert not fallback.parent.exists()
    if initialization_error:
        assert transport.closed
    else:
        transport.close()


def test_corrupt_sqlite_is_not_replaced_with_a_fallback(tmp_path, registry):
    database = tmp_path / "corrupt.sqlite"
    database.write_bytes(b"not a SQLite database")
    fallback = tmp_path / "must-not-create/queue.sqlite"
    with pytest.raises(sqlite3.DatabaseError):
        open_production_queue(database, registry=registry, sqlite_fallback=fallback)
    assert not fallback.parent.exists()
    assert database.read_bytes() == b"not a SQLite database"


def test_successful_postgres_remains_source_after_later_disconnect(
    tmp_path, registry, monkeypatch
):
    transport = _PostgresIO(tmp_path / "postgres-transport.sqlite")
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: transport)
    fallback = tmp_path / "must-not-create/queue.sqlite"
    with open_production_queue(POSTGRES, registry=registry, sqlite_fallback=fallback) as connection:
        assert connection.backend == "postgresql" and connection.fallback_reason is None
        event = _append(connection.queue)
        transport.fail = True
        try:
            with pytest.raises(psycopg.OperationalError):
                connection.queue.pending_outbox()
        finally:
            transport.fail = False
        assert connection.queue.pending_outbox() == [event]
        assert connection.queue.read_events() == [event]
        assert not fallback.parent.exists()
    assert transport.closed
