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
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from evallab.production.corpus_queue import (
    ContinuousWorkQueue,
    CorpusRegistry,
    ProductionQueueConnectionError,
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


class OutboxRelay:
    """Publish ordered envelopes, marking each row only after a JetStream ACK.

    The relay owns its lazy NATS client, not the queue or an injected publisher.
    A fresh broker receives one file-backed stream; existing stream settings are
    never overwritten. No broker configuration means no network access and no
    publication acknowledgement. All reported broker errors are fixed text,
    never exception messages that might contain connection credentials.
    """

    def __init__(
        self,
        queue: ContinuousWorkQueue,
        *,
        nats_url: str | None = None,
        subject: str = "evallab.production.events",
        stream: str = "EVALLAB_PRODUCTION",
        batch_size: int = 100,
        connect_timeout: float = 2.0,
        publish_timeout: float = 2.0,
        publisher: Publisher | None = None,
    ) -> None:
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        self._queue = queue
        self._nats_url = nats_url
        self._subject = subject
        self._stream = stream
        self._batch_size = batch_size
        self._connect_timeout = _seconds(connect_timeout, "connect_timeout")
        self._publish_timeout = _seconds(publish_timeout, "publish_timeout")
        self._injected = publisher is not None
        self._publisher = publisher
        self._client: Any = None
        self._broker_available = False
        self._closed = False
        self._lock = asyncio.Lock()

    async def _on_broker_error(self, error: Exception) -> None:
        # The client's default error callback logs raw external exceptions.
        # Availability is reported by run_once instead, without credential text.
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
        nats_url = self._nats_url
        if not nats_url:
            raise RuntimeError("NATS is not configured")

        from nats.aio.client import Client
        from nats.js.api import StorageType
        from nats.js.errors import NotFoundError

        client = Client()
        self._client = client
        async with asyncio.timeout(self._connect_timeout):
            await client.connect(
                servers=[nats_url],
                connect_timeout=self._connect_timeout,
                allow_reconnect=False,
                max_reconnect_attempts=0,
                error_cb=self._on_broker_error,
            )
        publisher = client.jetstream(timeout=self._publish_timeout)
        async with asyncio.timeout(self._connect_timeout):
            try:
                info = await publisher.stream_info(self._stream)
            except NotFoundError:
                info = await publisher.add_stream(
                    name=self._stream, subjects=[self._subject], storage=StorageType.FILE
                )
            if info.config.storage != StorageType.FILE:
                raise RuntimeError("JetStream stream must use file-backed storage")
        self._publisher = publisher
        return publisher

    async def run_once(self) -> dict[str, Any]:
        """Attempt one bounded batch; unavailable publication leaves rows pending.

        Database failures propagate: they do not trigger a second store, and an
        ACK followed by a failed mark is deliberately retried on the next cycle.
        Cancellation also propagates without acknowledging unpublished events.
        """
        async with self._lock:
            if self._closed:
                raise RuntimeError("Outbox relay is closed")
            delivered = 0
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
                    events = await asyncio.to_thread(
                        self._queue.pending_outbox, limit=self._batch_size
                    )
                    for event in events:
                        payload = json.dumps(
                            event, sort_keys=True, separators=(",", ":"), allow_nan=False
                        ).encode("utf-8")
                        try:
                            async with asyncio.timeout(self._publish_timeout):
                                ack = await publisher.publish(
                                    self._subject,
                                    payload,
                                    headers={"Nats-Msg-Id": event["event_id"]},
                                    timeout=self._publish_timeout,
                                    stream=self._stream,
                                )
                        except Exception:
                            reason = "NATS JetStream publication failed; unpublished events remain in the durable outbox."
                            self._broker_available = False
                            await self._close_client()
                            break
                        if (
                            getattr(ack, "stream", None) != self._stream
                            or type(getattr(ack, "seq", None)) is not int
                            or ack.seq <= 0
                        ):
                            reason = "JetStream did not acknowledge publication; unpublished events remain in the durable outbox."
                            self._broker_available = False
                            await self._close_client()
                            break
                        await asyncio.to_thread(self._queue.mark_published, event["event_id"])
                        delivered += 1
            return {
                "delivered": delivered,
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
