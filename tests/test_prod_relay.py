"""Real SQLite durability with fake PostgreSQL/NATS external I/O only."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from evallab.production.corpus_queue import CorpusRegistry, ProductionQueueConnectionError
from evallab.production.relay import OutboxRelay, open_production_queue

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


class _Crash(BaseException):
    """Simulate process loss after broker persistence, before the database mark."""


class _Publisher:
    def __init__(self) -> None:
        self.accepted: dict[str, bytes] = {}
        self.attempts: list[str] = []
        self.fail_on: set[int] = set()
        self.crash_once = False
        self.acknowledge = True
        self.missing_stream = False
        self.storage = "file"
        self.created: dict[str, Any] | None = None

    async def stream_info(self, name):
        if self.missing_stream:
            from nats.js.errors import NotFoundError

            raise NotFoundError()
        return SimpleNamespace(config=SimpleNamespace(storage=self.storage))

    async def add_stream(self, **options):
        self.created = options
        self.missing_stream = False
        return SimpleNamespace(config=SimpleNamespace(storage=options["storage"]))

    async def publish(self, subject, payload, *, headers, timeout, stream):
        event_id = headers["Nats-Msg-Id"]
        self.attempts.append(event_id)
        if len(self.attempts) in self.fail_on:
            raise OSError(SECRET)
        duplicate = event_id in self.accepted
        if duplicate:
            assert self.accepted[event_id] == payload
        else:
            self.accepted[event_id] = payload
        if self.crash_once:
            self.crash_once = False
            raise _Crash()
        if not self.acknowledge:
            return None
        return SimpleNamespace(
            stream=stream, seq=list(self.accepted).index(event_id) + 1, duplicate=duplicate
        )


class _NatsConnection:
    def __init__(self, publisher: _Publisher, *, fail: bool = False, hang: bool = False):
        self.publisher = publisher
        self.fail = fail
        self.hang = hang
        self.is_connected = False
        self.closed = False

    async def connect(self, **options):
        if self.fail:
            raise OSError(SECRET)
        if self.hang:
            await asyncio.Event().wait()
        self.is_connected = True

    def jetstream(self, **options):
        return self.publisher

    async def close(self):
        self.closed = True
        self.is_connected = False


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


def test_no_broker_retains_exact_pending_count_across_reopen(tmp_path, registry):
    database = tmp_path / "new/production/queue.sqlite"
    with open_production_queue(database, registry=registry) as connection:
        events = [_append(connection.queue, f"event-{index}") for index in range(3)]
        relay = OutboxRelay(connection.queue, batch_size=1)

        async def run():
            stats = await relay.run_once()
            await relay.close()
            return stats

        stats = asyncio.run(run())
        assert stats["delivered"] == 0
        assert stats["pending"] == 3
        assert stats["backend"] == "sqlite"
        assert stats["broker_available"] is False
        assert "not configured" in stats["fallback_reason"]
        # Relay close does not close the durable queue.
        assert connection.queue.pending_outbox() == events
    connection.close()
    with open_production_queue(f"sqlite:///{database}", registry=registry) as reopened:
        assert reopened.queue.pending_outbox() == events


def test_publish_before_mark_crash_retries_same_broker_identity(tmp_path, registry):
    database = tmp_path / "queue.sqlite"
    publisher = _Publisher()
    publisher.crash_once = True
    with open_production_queue(database, registry=registry) as connection:
        event = _append(connection.queue)
        relay = OutboxRelay(connection.queue, publisher=publisher)

        async def crash():
            try:
                await relay.run_once()
            finally:
                await relay.close()

        with pytest.raises(_Crash):
            asyncio.run(crash())
        assert connection.queue.pending_outbox() == [event]
        assert json.loads(publisher.accepted[event["event_id"]]) == event
    with open_production_queue(database, registry=registry) as reopened:
        relay = OutboxRelay(reopened.queue, publisher=publisher)

        async def resume():
            try:
                return await relay.run_once()
            finally:
                await relay.close()

        stats = asyncio.run(resume())
        assert stats["delivered"] == 1 and stats["pending"] == 0
        assert publisher.attempts == [event["event_id"], event["event_id"]]
        assert len(publisher.accepted) == 1
    with open_production_queue(database, registry=registry) as reopened:
        assert reopened.queue.pending_outbox() == []
        assert reopened.queue.read_events() == [event]


def test_mid_batch_failure_keeps_only_unacknowledged_rows_pending(tmp_path, registry):
    publisher = _Publisher()
    publisher.fail_on = {2}
    with open_production_queue(tmp_path / "queue.sqlite", registry=registry) as connection:
        events = [_append(connection.queue, f"event-{index}") for index in range(3)]
        relay = OutboxRelay(connection.queue, publisher=publisher)

        async def run():
            first = await relay.run_once()
            assert first["delivered"] == 1 and first["pending"] == 2
            assert first["broker_available"] is False
            assert SECRET not in json.dumps(first)
            assert connection.queue.pending_outbox() == events[1:]
            second = await relay.run_once()
            await relay.close()
            return second

        stats = asyncio.run(run())
        assert stats["delivered"] == 2 and stats["pending"] == 0
        assert publisher.attempts == ["event-0", "event-1", "event-1", "event-2"]


def test_core_publish_without_jetstream_ack_never_marks_delivered(tmp_path, registry):
    publisher = _Publisher()
    publisher.acknowledge = False
    with open_production_queue(tmp_path / "queue.sqlite", registry=registry) as connection:
        event = _append(connection.queue)
        relay = OutboxRelay(connection.queue, publisher=publisher)

        async def run():
            stats = await relay.run_once()
            await relay.close()
            return stats

        stats = asyncio.run(run())
        assert stats["delivered"] == 0 and stats["pending"] == 1
        assert stats["broker_available"] is False
        assert "did not acknowledge" in stats["fallback_reason"]
        assert connection.queue.pending_outbox() == [event]


def test_publish_timeout_leaves_event_available_for_retry(tmp_path, registry):
    class HangingPublisher(_Publisher):
        async def publish(self, subject, payload, *, headers, timeout, stream):
            await asyncio.Event().wait()

    database = tmp_path / "queue.sqlite"
    with open_production_queue(database, registry=registry) as connection:
        event = _append(connection.queue)
        relay = OutboxRelay(
            connection.queue, publisher=HangingPublisher(), publish_timeout=0.01
        )

        async def run():
            try:
                return await relay.run_once()
            finally:
                await relay.close()

        stats = asyncio.run(run())
        assert stats["delivered"] == 0 and stats["pending"] == 1
        assert stats["broker_available"] is False
    with open_production_queue(database, registry=registry) as reopened:
        assert reopened.queue.pending_outbox() == [event]


def test_unreachable_nats_is_sanitized_and_next_cycle_reconnects(tmp_path, registry, monkeypatch):
    publisher = _Publisher()
    failed = _NatsConnection(publisher, fail=True)
    healthy = _NatsConnection(publisher)
    clients = iter((failed, healthy))
    monkeypatch.setattr("nats.aio.client.Client", lambda: next(clients))
    with open_production_queue(tmp_path / "queue.sqlite", registry=registry) as connection:
        _append(connection.queue)
        relay = OutboxRelay(connection.queue, nats_url=f"nats://operator:{SECRET}@127.0.0.1:4222")

        async def run():
            first = await relay.run_once()
            assert first["delivered"] == 0 and first["pending"] == 1
            assert first["broker_available"] is False
            assert SECRET not in json.dumps(first)
            assert failed.closed
            second = await relay.run_once()
            await relay.close()
            return second

        stats = asyncio.run(run())
        assert stats["delivered"] == 1 and stats["pending"] == 0
        assert stats["broker_available"] is True
        assert healthy.closed


def test_fresh_jetstream_creates_file_storage_and_closes_client(tmp_path, registry, monkeypatch):
    publisher = _Publisher()
    publisher.missing_stream = True
    client = _NatsConnection(publisher)
    monkeypatch.setattr("nats.aio.client.Client", lambda: client)
    with open_production_queue(tmp_path / "queue.sqlite", registry=registry) as connection:
        _append(connection.queue)
        relay = OutboxRelay(connection.queue, nats_url="nats://127.0.0.1:4222")

        async def run():
            try:
                return await relay.run_once()
            finally:
                await relay.close()
                await relay.close()

        assert asyncio.run(run())["pending"] == 0
        assert publisher.created == {
            "name": "EVALLAB_PRODUCTION",
            "subjects": ["evallab.production.events"],
            "storage": "file",
        }
        assert client.closed


@pytest.mark.parametrize("memory_stream", [False, True])
def test_unavailable_or_nondurable_broker_retains_local_rows(
    tmp_path, registry, monkeypatch, memory_stream
):
    publisher = _Publisher()
    if memory_stream:
        publisher.storage = "memory"
    client = _NatsConnection(publisher, hang=not memory_stream)
    monkeypatch.setattr("nats.aio.client.Client", lambda: client)
    with open_production_queue(tmp_path / "queue.sqlite", registry=registry) as connection:
        event = _append(connection.queue)
        relay = OutboxRelay(
            connection.queue, nats_url="nats://127.0.0.1:4222", connect_timeout=0.01
        )

        async def run():
            try:
                return await relay.run_once()
            finally:
                await relay.close()

        stats = asyncio.run(run())
        assert stats["delivered"] == 0 and stats["pending"] == 1
        assert stats["broker_available"] is False
        assert connection.queue.pending_outbox() == [event]
        assert client.closed


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


def test_successful_postgres_remains_source_after_ack_and_later_disconnect(
    tmp_path, registry, monkeypatch
):
    transport = _PostgresIO(tmp_path / "postgres-transport.sqlite")
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: transport)
    fallback = tmp_path / "must-not-create/queue.sqlite"
    publisher = _Publisher()
    with open_production_queue(POSTGRES, registry=registry, sqlite_fallback=fallback) as connection:
        assert connection.backend == "postgresql" and connection.fallback_reason is None
        event = _append(connection.queue)
        relay = OutboxRelay(connection.queue, publisher=publisher)

        async def run():
            first = await relay.run_once()
            assert first["backend"] == "postgresql" and first["delivered"] == 1
            transport.fail = True
            try:
                with pytest.raises(psycopg.OperationalError):
                    await relay.run_once()
            finally:
                transport.fail = False
                await relay.close()

        asyncio.run(run())
        assert connection.queue.pending_outbox() == []
        assert connection.queue.read_events() == [event]
        assert not fallback.parent.exists()
    assert transport.closed
