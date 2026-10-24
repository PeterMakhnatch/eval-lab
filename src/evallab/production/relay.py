"""Initial-open database failover and an acknowledged, at-least-once outbox relay.

Only an initial PostgreSQL connection failure can select the explicit SQLite
fallback. Once opened, a queue remains the source of truth until it is closed;
publication failures never migrate events or acknowledge local work.

JetStream's ``Nats-Msg-Id`` deduplicates publish-before-mark retries within the
stream's deduplication window. Consumers must still be idempotent by event_id:
this is at-least-once delivery, not an unbounded exactly-once guarantee.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from evallab.production.corpus_queue import (
    ContinuousWorkQueue,
    CorpusRegistry,
    ProductionQueueConnectionError,
)
from evallab.production.outbox_relay import (
    STREAM_NAME,
    DrainReport,
    PoisonQuarantine,
    PubAck,
    TransientBrokerError,
)
from evallab.production.outbox_relay import (
    OutboxRelay as JournalRelay,
)

Backend = Literal["sqlite", "postgresql"]


@dataclass
class QueueConnection:
    """Own one queue without permitting a later switch between durable stores."""

    queue: ContinuousWorkQueue
    backend: Backend
    fallback_reason: str | None = None
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        if not self._closed:
            self.queue.close()
            self._closed = True

    def __enter__(self) -> QueueConnection:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def _sqlite_path(database: str | Path) -> Path:
    path = str(database)
    if path.startswith("sqlite:///"):
        path = path[len("sqlite:///") :]
    result = Path(path).expanduser()
    result.parent.mkdir(parents=True, exist_ok=True)
    return result


def open_production_queue(
    database: str | Path,
    *,
    registry: CorpusRegistry,
    sqlite_fallback: Path | None = None,
    connect_timeout: int = 3,
) -> QueueConnection:
    """Open the requested store, falling back only before PostgreSQL connects.

    A malformed DSN, schema error, corrupt database, or any later disconnect is
    not an availability fallback. The dedicated connection error contains no
    driver exception text or connection credentials.
    """
    if type(connect_timeout) is not int or connect_timeout <= 0:
        raise ValueError("connect_timeout must be a positive integer")
    if not str(database).startswith(("postgresql://", "postgres://")):
        return QueueConnection(ContinuousWorkQueue(_sqlite_path(database), registry=registry), "sqlite")
    try:
        queue = ContinuousWorkQueue(database, registry=registry, connect_timeout=connect_timeout)
    except ProductionQueueConnectionError:
        if sqlite_fallback is None:
            raise
        queue = ContinuousWorkQueue(_sqlite_path(sqlite_fallback), registry=registry)
        return QueueConnection(
            queue,
            "sqlite",
            "PostgreSQL initial connection unavailable; using the explicit SQLite fallback.",
        )
    return QueueConnection(queue, "postgresql")


class Publisher(Protocol):
    """The acknowledged JetStream publish surface; injected publishers are borrowed."""

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        headers: dict[str, str],
        timeout: float,
        stream: str,
    ) -> Any: ...


def _seconds(value: float, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be finite and positive")
    return float(value)


class _AsyncPublisherBridge:
    """Borrow an async NATS publisher for the shared synchronous drain engine."""

    def __init__(
        self, publisher: Publisher, loop: asyncio.AbstractEventLoop, timeout: float,
    ) -> None:
        self._publisher = publisher
        self._loop = loop
        self._timeout = timeout
        self._closed = False

    async def _publish(self, subject: str, msg_id: str, payload: bytes) -> PubAck:
        async with asyncio.timeout(self._timeout):
            ack = await self._publisher.publish(
                subject, payload, headers={"Nats-Msg-Id": msg_id},
                timeout=self._timeout, stream=STREAM_NAME,
            )
        duplicate = getattr(ack, "duplicate", None)
        if duplicate is None:
            duplicate = False  # nats-py represents an omitted duplicate flag as None.
        if (
            getattr(ack, "stream", None) != STREAM_NAME
            or type(getattr(ack, "seq", None)) is not int
            or ack.seq <= 0
            or type(duplicate) is not bool
        ):
            raise TransientBrokerError("JetStream did not acknowledge publication")
        return PubAck(subject, msg_id, ack.seq, duplicate, ack.stream)

    def publish(self, subject: str, msg_id: str, payload: bytes) -> PubAck:
        if self._closed:
            raise RuntimeError("publisher bridge is closed")
        future = asyncio.run_coroutine_threadsafe(self._publish(subject, msg_id, payload), self._loop)
        try:
            # Also bound a stalled/stopping event loop, not just broker I/O.
            return future.result(timeout=self._timeout + 1.0)
        except Exception:
            raise TransientBrokerError("JetStream did not acknowledge publication") from None
        finally:
            if not future.done():
                future.cancel()

    def close(self) -> None:
        # The coordinator owns the async client; this drain only borrows it.
        self._closed = True


class ProductionOutboxRelay:
    """Manage async broker lifecycle around the canonical journal drain engine.

    Routing, poison quarantine and pub-ack-before-mark belong to
    ``outbox_relay.OutboxRelay``. This coordinator only connects NATS, ensures a
    fresh file-backed stream, and bridges its async client into that engine.
    Missing NATS leaves the source outbox unpublished. Existing stream settings
    are never overwritten, and broker diagnostics never expose credentials.
    """

    def __init__(
        self,
        queue: ContinuousWorkQueue,
        *,
        nats_url: str | None = None,
        batch_size: int = 100,
        connect_timeout: float = 2.0,
        publish_timeout: float = 2.0,
        publisher: Publisher | None = None,
        quarantine_path: Path | None = None,
    ) -> None:
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        self._queue = queue
        self._nats_url = nats_url
        self._batch_size = batch_size
        self._connect_timeout = _seconds(connect_timeout, "connect_timeout")
        self._publish_timeout = _seconds(publish_timeout, "publish_timeout")
        self._quarantine_path = quarantine_path or (
            queue.registry.repo_root / "derived/production/relay-poison.sqlite"
        )
        self._injected = publisher is not None
        self._publisher = publisher
        self._client: Any = None
        self._broker_available = False
        self._closed = False
        self._lock = asyncio.Lock()

    async def _on_broker_error(self, error: Exception) -> None:
        self._broker_available = False

    async def _close_client(self) -> None:
        client, self._client = self._client, None
        if not self._injected:
            self._publisher = None
        if client is not None:
            try:
                async with asyncio.timeout(self._connect_timeout):
                    await client.close()
            except Exception:
                self._broker_available = False

    async def _open_publisher(self) -> Publisher:
        if self._publisher is not None and (
            self._injected or self._client is not None and self._client.is_connected
        ):
            return self._publisher
        await self._close_client()
        if not self._nats_url:
            raise RuntimeError("NATS is not configured")

        from nats.aio.client import Client
        from nats.js.api import StorageType
        from nats.js.errors import NotFoundError

        client = Client()
        self._client = client
        async with asyncio.timeout(self._connect_timeout):
            await client.connect(
                servers=[self._nats_url], connect_timeout=self._connect_timeout,
                allow_reconnect=False, max_reconnect_attempts=0,
                error_cb=self._on_broker_error,
            )
        publisher = client.jetstream(timeout=self._publish_timeout)
        async with asyncio.timeout(self._connect_timeout):
            try:
                info = await publisher.stream_info(STREAM_NAME)
            except NotFoundError:
                info = await publisher.add_stream(
                    name=STREAM_NAME, subjects=["evallab.>"], storage=StorageType.FILE,
                )
            if info.config.storage != StorageType.FILE:
                raise RuntimeError("JetStream stream must use file-backed storage")
        self._publisher = publisher
        return publisher

    def _drain(self, publisher: Publisher, loop: asyncio.AbstractEventLoop) -> DrainReport:
        quarantine = PoisonQuarantine(_sqlite_path(self._quarantine_path))
        with JournalRelay(
            self._queue, _AsyncPublisherBridge(publisher, loop, self._publish_timeout),
            quarantine=quarantine, batch_limit=self._batch_size,
        ) as relay:
            return relay.drain()

    async def run_once(self) -> dict[str, Any]:
        """Drain once without changing queue backend or losing in-flight writes."""
        async with self._lock:
            if self._closed:
                raise RuntimeError("Outbox relay is closed")
            delivered = quarantined = 0
            reason: str | None = None
            self._broker_available = False
            if not self._nats_url and not self._injected:
                reason = "NATS is not configured; events remain in the durable outbox."
            else:
                try:
                    publisher = await self._open_publisher()
                except ImportError:
                    reason = "NATS client is not installed; events remain in the durable outbox."
                    await self._close_client()
                except Exception:
                    reason = "NATS JetStream is unavailable; unpublished events remain in the durable outbox."
                    await self._close_client()
                else:
                    self._broker_available = True
                    work = asyncio.create_task(asyncio.to_thread(
                        self._drain, publisher, asyncio.get_running_loop(),
                    ))
                    try:
                        report = await asyncio.shield(work)
                    except asyncio.CancelledError:
                        # A worker may still be persisting an ACK or quarantine.
                        # Keep its loop/queue alive before propagating cancellation.
                        await work
                        raise
                    if report.mark_failed or report.quarantine_failed:
                        raise RuntimeError("Outbox durability update failed; events remain unpublished")
                    delivered = len(report.published)
                    quarantined = len(report.quarantined)
                    if report.halted:
                        reason = "JetStream did not acknowledge publication; unpublished events remain in the durable outbox."
                        self._broker_available = False
                        await self._close_client()
            return {
                "delivered": delivered,
                "quarantined": quarantined,
                "pending": await asyncio.to_thread(self._queue.pending_outbox_count),
                "backend": "postgresql" if self._queue._postgres else "sqlite",
                "broker_available": self._broker_available,
                "fallback_reason": reason,
            }

    async def close(self) -> None:
        async with self._lock:
            if not self._closed:
                self._closed = True
                self._broker_available = False
                await self._close_client()
