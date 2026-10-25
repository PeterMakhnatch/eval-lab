"""Publish durable production journal envelopes to scoped JetStream subjects.

``OutboxRelay`` drains ``ContinuousWorkQueue.pending_outbox`` and publishes each
complete envelope to ``evallab.<family>.<token>`` with ``Nats-Msg-Id`` equal to
``event_id``. Publication is at-least-once: the relay awaits a real JetStream
pub-ack before calling ``queue.mark_published``, so a crash between ack and
mark only causes a republish that broker message-id deduplication absorbs.

Routing is explicit for every existing production event type; unknown future
types fall back to ``evallab.capture.<system>``, the passive-observation
family, and are never silently promoted to findings, control, or assets.

- ``evallab.capture.<system>``: raw observation/telemetry — ``trace.*``,
  ``run.*``, monitor capture (``monitor.prefix_scored``, ``monitor.coverage_gap``,
  ``monitor.triage_unavailable``, ``monitor.gate_decided``), and seeker attempt
  telemetry (``seeker.attempt_started``, ``seeker.grader_feedback``).
- ``evallab.findings.<system>``: determinations — ``monitor.suspicion``,
  ``seeker.exploit_confirmed``, ``seeker.job_refused``, ``task_quality.failed``,
  ``task_quality.repair_qualified``, ``task_quality.repair_rejected``,
  ``task_quality.repair_unavailable``, and the task-quality ``qa.finding/v1``
  finding records.
- ``evallab.control.<system>``: dispatch and lifecycle — ``control.*``,
  ``executor.control_*``, ``operator.control_*``, ``worker.*``, and queue job
  events (worker-system ``*_task`` types such as ``task_quality.repair_task``).
- ``evallab.assets.<kind>``: asset events (``asset.*``, ``verified_asset.*``);
  ``<kind>`` comes from the payload's ``kind``/``asset_type`` (or a nested
  ``asset`` mapping), defaulting to ``unknown``.

Poison handling: invalid envelopes, TEST/heldout payloads (a defense-in-depth
seal on top of queue admission), and permanently rejected publishes are
quarantined durably *before* the event is marked consumed, so one bad envelope
never starves valid ones. Transient broker errors (timeouts, resets, 5xx, no
responders) leave the event unpublished and halt the drain; they stay retryable
on the next drain and are never quarantined or discarded.

Transports: ``LocalJetStreamAdapter`` is a zero-config JetStream-shaped broker
with memory or SQLite-durable storage, message-id suppression, immutable
snapshots, cursor replay, and a durable poison table. ``NatsTcpTransport``
speaks the real NATS TCP wire protocol (stdlib socket; JetStream pub-acks read
from a wildcard inbox). ``HttpGatewayTransport`` POSTs to an explicitly
configured HTTP relay gateway — plain NATS has no HTTP publish endpoint, so a
gateway must terminate HTTP and return a real pub-ack JSON body.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from evallab.production.corpus_queue import STATUSES, SYSTEMS, WORKER_SYSTEMS

SUBJECT_ROOT = "evallab"
STREAM_NAME = "EVALLAB-PRODUCTION"
MAX_MSG_ID = 1024
_MAX_BROKER_LINE = 1 << 20

_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: Event-type tokens also admit the ``name/version`` schema convention used by
#: producers such as ``qa.finding/v1``; the event type never enters the routed
#: subject, but the envelope contract keeps it segmentable.
_EVENT_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}(?:/[A-Za-z0-9_-]{1,64})?")
_FINDING_TYPES = frozenset(
    {
        "monitor.suspicion",
        "seeker.exploit_confirmed",
        "seeker.job_refused",
        "task_quality.failed",
        "task_quality.repair_qualified",
        "task_quality.repair_rejected",
        "task_quality.repair_unavailable",
        "qa.finding/v1",
    }
)
_ASSET_PREFIXES = ("asset.", "verified_asset.")
_CONTROL_PREFIXES = ("control.", "executor.", "operator.", "worker.")
_HELDOUT_SPLITS = ("test", "heldout")
_TRANSIENT_ERR_HINTS = (
    "stale connection",
    "slow consumer",
    "maximum connections",
    "lame duck",
    "shutdown",
)


class OutboxRelayError(Exception):
    """Base class for outbox relay failures."""


class TransientBrokerError(OutboxRelayError):
    """Timeout, connection loss, or broker 5xx; the event stays unpublished and retryable."""


class PermanentPublishError(OutboxRelayError):
    """The broker or transport rejected the message; retrying cannot help."""


class InvalidEnvelopeError(ValueError):
    """An outbox envelope does not satisfy the production envelope contract."""

    reason = "invalid_envelope"


class HeldoutEnvelopeError(InvalidEnvelopeError):
    """The envelope carries TEST/heldout content and must never be published."""

    reason = "heldout_payload"


@dataclass(frozen=True)
class PubAck:
    """A real broker acknowledgement: stream sequence plus dedup flag."""

    subject: str
    msg_id: str
    seq: int
    duplicate: bool
    stream: str


@dataclass(frozen=True)
class StoredMessage:
    """One immutable stored envelope snapshot; ``seq`` is the stream cursor."""

    seq: int
    subject: str
    msg_id: str
    payload: bytes

    @property
    def envelope(self) -> dict[str, Any]:
        decoded = json.loads(self.payload)
        if not isinstance(decoded, dict):
            raise ValueError("stored payload is not a JSON object")
        return decoded


@dataclass(frozen=True)
class PoisonRecord:
    """A durable quarantine entry, persisted before the event is consumed."""

    key: str
    event_id: str | None
    subject: str | None
    reason: str
    detail: str
    envelope: dict[str, Any]
    quarantined_at: str


@dataclass(frozen=True)
class DrainReport:
    """Per-drain outcome; mark failures stay distinct from poisoned envelopes.

    ``published``/``duplicates``/``quarantined`` carry event keys.
    ``mark_failed`` lists events whose queue mark did not happen (raised or
    impossible for an envelope without a usable ``event_id``); those stay
    pending and are distinguishable from invalid envelopes via the quarantine
    record ``reason``. ``deferred`` counts pending envelopes not durably
    consumed this drain (halted tail plus anything left unpublished).
    """

    examined: int
    published: tuple[str, ...]
    duplicates: tuple[str, ...]
    quarantined: tuple[str, ...]
    mark_failed: tuple[str, ...]
    quarantine_failed: tuple[str, ...]
    halted: bool
    transient_error: str | None
    deferred: int


@runtime_checkable
class OutboxSource(Protocol):
    """The drain surface the relay needs from ``ContinuousWorkQueue``."""

    def pending_outbox(self, *, limit: int = 100) -> list[dict[str, Any]]: ...

    def mark_published(self, event_id: str) -> None: ...


@runtime_checkable
class EnvelopePublisher(Protocol):
    """Synchronous publisher that awaits a real pub-ack per message."""

    def publish(self, subject: str, msg_id: str, payload: bytes) -> PubAck: ...

    def close(self) -> None: ...


@runtime_checkable
class PoisonSink(Protocol):
    """Durable, inspectable quarantine for poison events (first record wins)."""

    def record_poison(
        self,
        *,
        key: str,
        event_id: str | None,
        subject: str | None,
        reason: str,
        detail: str,
        envelope: dict[str, Any],
    ) -> PoisonRecord: ...

    def poisoned(self) -> tuple[PoisonRecord, ...]: ...

    def close(self) -> None: ...


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(envelope: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(envelope), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidEnvelopeError(f"{field} must be a non-empty string")
    return value


def _require_msg_id(msg_id: Any) -> None:
    if (
        not isinstance(msg_id, str)
        or not msg_id
        or len(msg_id) > MAX_MSG_ID
        or any(ord(char) < 33 or ord(char) == 127 for char in msg_id)
    ):
        raise PermanentPublishError("Nats-Msg-Id must be 1-1024 printable non-space bytes")


def _require_literal_subject(subject: Any) -> None:
    if not isinstance(subject, str) or any(
        not _TOKEN.fullmatch(token) for token in subject.split(".")
    ):
        raise PermanentPublishError(f"subject is not a valid literal NATS subject: {subject!r}")


def _subject_matcher(pattern: str) -> Callable[[str], bool]:
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("replay subject must be a non-empty string")
    tokens = pattern.split(".")
    for index, token in enumerate(tokens):
        if token == ">":
            if index != len(tokens) - 1:
                raise ValueError("'>' wildcard must be the final subject token")
        elif token != "*" and not _TOKEN.fullmatch(token):
            raise ValueError(f"invalid subject token: {token!r}")

    def match(subject: str) -> bool:
        parts = subject.split(".")
        for index, token in enumerate(tokens):
            if token == ">":
                return len(parts) > index
            if index >= len(parts) or (token != "*" and token != parts[index]):
                return False
        return len(parts) == len(tokens)

    return match


def _validate_envelope(envelope: Any) -> Mapping[str, Any]:
    if not isinstance(envelope, Mapping):
        raise InvalidEnvelopeError("envelope must be a mapping")
    event_id = envelope.get("event_id")
    if (
        not isinstance(event_id, str)
        or not event_id
        or len(event_id) > MAX_MSG_ID
        or any(ord(char) < 33 or ord(char) == 127 for char in event_id)
    ):
        raise InvalidEnvelopeError("event_id must be a non-empty header-safe string")
    _require_text(envelope.get("run_id"), "run_id")
    system = envelope.get("system")
    if not isinstance(system, str) or system not in SYSTEMS:
        raise InvalidEnvelopeError("system is not in the production envelope contract")
    event_type = envelope.get("event_type")
    if (
        not isinstance(event_type, str)
        or not event_type
        or len(event_type) > 256
        or any(not _EVENT_TOKEN.fullmatch(token) for token in event_type.split("."))
    ):
        raise InvalidEnvelopeError("event_type must be dotted routing-safe tokens")
    ts = _require_text(envelope.get("ts"), "ts")
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidEnvelopeError("ts must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise InvalidEnvelopeError("ts must include a timezone")
    seq = envelope.get("seq")
    if type(seq) is not int or seq < 0:
        raise InvalidEnvelopeError("seq must be a non-negative integer")
    if envelope.get("status") not in STATUSES:
        raise InvalidEnvelopeError("status is not in the production envelope contract")
    step = envelope.get("prefix_step")
    if step is not None and (type(step) is not int or step < 0):
        raise InvalidEnvelopeError("prefix_step must be a non-negative integer or None")
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        raise InvalidEnvelopeError("payload must be a mapping")
    try:
        _canonical(envelope)
    except (TypeError, ValueError):
        raise InvalidEnvelopeError("envelope must be JSON-serializable") from None
    split = payload.get("split")
    if isinstance(split, str) and split.strip().lower() in _HELDOUT_SPLITS:
        raise HeldoutEnvelopeError("TEST/heldout payloads are never published")
    return envelope


def _is_job_type(event_type: str) -> bool:
    return event_type.partition(".")[0] in WORKER_SYSTEMS and event_type.endswith("_task")


def _asset_kind(payload: Mapping[str, Any]) -> str:
    kind = payload.get("kind") or payload.get("asset_type")
    if kind is None:
        asset = payload.get("asset")
        if isinstance(asset, Mapping):
            kind = asset.get("kind") or asset.get("asset_type")
    if kind is None:
        return "unknown"
    if not isinstance(kind, str) or not _TOKEN.fullmatch(kind):
        raise InvalidEnvelopeError("asset kind is not a valid subject token")
    return kind


def route_subject(envelope: Mapping[str, Any]) -> str:
    """Validate the production envelope contract and return its scoped subject.

    Every existing production event type has an explicit family; unknown types
    fall back to ``evallab.capture.<system>`` so a new event is observed but
    never auto-promoted to findings, control, or assets. Raises
    ``InvalidEnvelopeError`` (or ``HeldoutEnvelopeError`` for TEST/heldout
    payloads) when the envelope cannot be safely routed.
    """
    _validate_envelope(envelope)
    system = envelope["system"]
    event_type = envelope["event_type"]
    if event_type in _FINDING_TYPES:
        family = "findings"
    elif event_type.startswith(_ASSET_PREFIXES):
        family = "assets"
    elif _is_job_type(event_type) or event_type.startswith(_CONTROL_PREFIXES):
        family = "control"
    else:
        family = "capture"
    token = _asset_kind(envelope["payload"]) if family == "assets" else system
    if not _TOKEN.fullmatch(token):
        raise InvalidEnvelopeError(f"routing token is not subject-safe: {token!r}")
    return f"{SUBJECT_ROOT}.{family}.{token}"


class _Store(Protocol):
    def insert_message(self, subject: str, msg_id: str, payload: bytes) -> tuple[int, bool]: ...

    def messages(
        self, match: Callable[[str], bool], after_seq: int, limit: int
    ) -> list[StoredMessage]: ...

    def insert_poison(self, record: PoisonRecord) -> PoisonRecord: ...

    def poisons(self) -> tuple[PoisonRecord, ...]: ...

    def close(self) -> None: ...


class _MemoryStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._closed = False
        self._seq = 0
        self._messages: dict[int, tuple[str, str, bytes]] = {}
        self._by_msg_id: dict[str, int] = {}
        self._poison: dict[str, PoisonRecord] = {}

    def _check(self) -> None:
        if self._closed:
            raise PermanentPublishError("local JetStream storage is closed")

    def insert_message(self, subject: str, msg_id: str, payload: bytes) -> tuple[int, bool]:
        with self._lock:
            self._check()
            existing = self._by_msg_id.get(msg_id)
            if existing is not None:
                return existing, False
            self._seq += 1
            self._messages[self._seq] = (subject, msg_id, payload)
            self._by_msg_id[msg_id] = self._seq
            return self._seq, True

    def messages(
        self, match: Callable[[str], bool], after_seq: int, limit: int
    ) -> list[StoredMessage]:
        with self._lock:
            self._check()
            out: list[StoredMessage] = []
            for seq, (subject, msg_id, payload) in self._messages.items():
                if seq <= after_seq or not match(subject):
                    continue
                out.append(StoredMessage(seq, subject, msg_id, payload))
                if len(out) >= limit:
                    break
            return out

    def insert_poison(self, record: PoisonRecord) -> PoisonRecord:
        with self._lock:
            self._check()
            existing = self._poison.get(record.key)
            if existing is None:
                self._poison[record.key] = record
                return record
            return existing

    def poisons(self) -> tuple[PoisonRecord, ...]:
        with self._lock:
            return tuple(self._poison.values())

    def close(self) -> None:
        with self._lock:
            self._closed = True


class _SqliteStore:
    def __init__(self, path: str | Path) -> None:
        self._lock = threading.RLock()
        self._closed = False
        raw = str(path)
        if raw.startswith("sqlite:///"):
            raw = raw[len("sqlite:///") :]
        self._connection = sqlite3.connect(
            raw, timeout=30, isolation_level=None, check_same_thread=False
        )
        try:
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS js_messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject TEXT NOT NULL, msg_id TEXT NOT NULL UNIQUE,
                    payload BLOB NOT NULL, stored_at TEXT NOT NULL)"""
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS js_messages_subject ON js_messages(subject, seq)"
            )
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS js_poison (
                    event_key TEXT PRIMARY KEY, event_id TEXT, subject TEXT,
                    reason TEXT NOT NULL, detail TEXT NOT NULL,
                    envelope TEXT NOT NULL, quarantined_at TEXT NOT NULL)"""
            )
        except BaseException:
            self._connection.close()
            raise

    def _check(self) -> None:
        if self._closed:
            raise PermanentPublishError("local JetStream storage is closed")

    def insert_message(self, subject: str, msg_id: str, payload: bytes) -> tuple[int, bool]:
        with self._lock:
            self._check()
            try:
                cursor = self._connection.execute(
                    "INSERT INTO js_messages(subject, msg_id, payload, stored_at) VALUES (?, ?, ?, ?)",
                    (subject, msg_id, payload, _utcnow()),
                )
                seq = cursor.lastrowid
                if seq is None:
                    raise PermanentPublishError("store did not assign a stream sequence")
                return int(seq), True
            except sqlite3.IntegrityError:
                row = self._connection.execute(
                    "SELECT seq FROM js_messages WHERE msg_id = ?", (msg_id,)
                ).fetchone()
                if row is None:
                    raise PermanentPublishError(
                        "duplicate suppression lost the stored message"
                    ) from None
                return int(row[0]), False

    def messages(
        self, match: Callable[[str], bool], after_seq: int, limit: int
    ) -> list[StoredMessage]:
        with self._lock:
            self._check()
            cursor = self._connection.execute(
                "SELECT seq, subject, msg_id, payload FROM js_messages WHERE seq > ? ORDER BY seq",
                (after_seq,),
            )
            out: list[StoredMessage] = []
            for seq, subject, msg_id, payload in cursor:
                if not match(str(subject)):
                    continue
                out.append(
                    StoredMessage(int(seq), str(subject), str(msg_id), bytes(payload))
                )
                if len(out) >= limit:
                    break
            cursor.close()
            return out

    def insert_poison(self, record: PoisonRecord) -> PoisonRecord:
        with self._lock:
            self._check()
            self._connection.execute(
                """INSERT INTO js_poison(event_key, event_id, subject, reason, detail, envelope, quarantined_at)
                VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(event_key) DO NOTHING""",
                (
                    record.key,
                    record.event_id,
                    record.subject,
                    record.reason,
                    record.detail,
                    _canonical(record.envelope).decode(),
                    record.quarantined_at,
                ),
            )
            row = self._connection.execute(
                "SELECT event_key, event_id, subject, reason, detail, envelope, quarantined_at FROM js_poison WHERE event_key = ?",
                (record.key,),
            ).fetchone()
            if row is None:
                raise PermanentPublishError("quarantine record was not persisted")
            return PoisonRecord(
                str(row[0]),
                row[1],
                row[2],
                str(row[3]),
                str(row[4]),
                json.loads(row[5]),
                str(row[6]),
            )

    def poisons(self) -> tuple[PoisonRecord, ...]:
        with self._lock:
            self._check()
            rows = self._connection.execute(
                "SELECT event_key, event_id, subject, reason, detail, envelope, quarantined_at FROM js_poison ORDER BY rowid"
            ).fetchall()
            return tuple(
                PoisonRecord(
                    str(key), event_id, subject, str(reason), str(detail), json.loads(env), str(at)
                )
                for key, event_id, subject, reason, detail, env, at in rows
            )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._connection.close()


def _open_store(storage: str | Path | None) -> _Store:
    if storage is None or str(storage) in ("", ":memory:"):
        return _MemoryStore()
    return _SqliteStore(storage)


def _wrap_store_error(exc: sqlite3.Error) -> OutboxRelayError:
    if isinstance(exc, sqlite3.OperationalError):
        return TransientBrokerError(f"local broker storage failed: {exc}")
    return PermanentPublishError(f"local broker storage rejected the write: {exc}")


class LocalJetStreamAdapter:
    """Zero-config JetStream-shaped broker with memory or SQLite storage.

    ``storage`` is ``None``/``":memory:"`` for ephemeral memory, or a SQLite
    path / ``sqlite:///`` URL for durable storage. Duplicate ``Nats-Msg-Id``
    values are suppressed by ``msg_id``: the original stored snapshot is kept
    and the ack reports ``duplicate=True``. ``replay_subject`` replays stored
    immutable envelope snapshots in stream order after a caller-supplied
    sequence cursor. The adapter is also a ``PoisonSink``: poison records are
    durable and inspectable via ``poisoned()``.
    """

    def __init__(
        self, storage: str | Path | None = None, *, stream: str = STREAM_NAME
    ) -> None:
        self._stream = stream
        self._store = _open_store(storage)

    def publish(self, subject: str, msg_id: str, payload: bytes) -> PubAck:
        """Store the message and return a real ack; duplicate msg_ids dedupe."""
        _require_literal_subject(subject)
        _require_msg_id(msg_id)
        if not isinstance(payload, bytes | bytearray | memoryview):
            raise PermanentPublishError("payload must be bytes")
        try:
            seq, stored = self._store.insert_message(subject, msg_id, bytes(payload))
        except sqlite3.Error as exc:
            raise _wrap_store_error(exc) from exc
        return PubAck(subject, msg_id, seq, not stored, self._stream)

    def replay_subject(
        self, subject: str, *, after_seq: int = 0, limit: int = 100
    ) -> list[StoredMessage]:
        """Replay stored envelopes whose subject matches, in stream order.

        ``subject`` is an exact subject or a scoped pattern (``*`` matches one
        token, trailing ``>`` matches the tail). ``after_seq`` is the caller's
        stream cursor: only messages with ``seq > after_seq`` are returned.
        """
        match = _subject_matcher(subject)
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("after_seq must be a non-negative integer")
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        return self._store.messages(match, after_seq, limit)

    def record_poison(
        self,
        *,
        key: str,
        event_id: str | None,
        subject: str | None,
        reason: str,
        detail: str,
        envelope: dict[str, Any],
    ) -> PoisonRecord:
        """Persist a poison record durably; the first record for a key wins."""
        record = PoisonRecord(key, event_id, subject, reason, detail, envelope, _utcnow())
        try:
            return self._store.insert_poison(record)
        except sqlite3.Error as exc:
            raise _wrap_store_error(exc) from exc

    def poisoned(self) -> tuple[PoisonRecord, ...]:
        """Inspect every quarantined event in record order."""
        return self._store.poisons()

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> LocalJetStreamAdapter:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class PoisonQuarantine:
    """Standalone durable poison sink for remote transports without local storage."""

    def __init__(self, storage: str | Path | None = None) -> None:
        self._store = _open_store(storage)

    def record_poison(
        self,
        *,
        key: str,
        event_id: str | None,
        subject: str | None,
        reason: str,
        detail: str,
        envelope: dict[str, Any],
    ) -> PoisonRecord:
        record = PoisonRecord(key, event_id, subject, reason, detail, envelope, _utcnow())
        try:
            return self._store.insert_poison(record)
        except sqlite3.Error as exc:
            raise _wrap_store_error(exc) from exc

    def poisoned(self) -> tuple[PoisonRecord, ...]:
        return self._store.poisons()

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> PoisonQuarantine:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def _parse_puback(body: bytes, subject: str, msg_id: str) -> PubAck:
    try:
        document = json.loads(body)
    except ValueError:
        raise TransientBrokerError("pub-ack is not valid JSON") from None
    if not isinstance(document, dict):
        raise TransientBrokerError("pub-ack is not a JSON object")
    error = document.get("error")
    if error is not None:
        code = error.get("code") if isinstance(error, Mapping) else None
        description = (
            error.get("description") if isinstance(error, Mapping) else str(error)
        )
        if type(code) is int and code >= 500:
            raise TransientBrokerError(f"broker publish error {code}: {description}")
        raise PermanentPublishError(f"broker rejected the publish: {description}")
    seq = document.get("seq")
    if type(seq) is not int or seq <= 0:
        raise TransientBrokerError("pub-ack carries no positive stream sequence")
    stream = document.get("stream")
    if not isinstance(stream, str) or not stream:
        raise TransientBrokerError("pub-ack carries no stream name")
    duplicate = document.get("duplicate", False)
    if type(duplicate) is not bool:
        raise TransientBrokerError("pub-ack duplicate flag is not boolean")
    return PubAck(subject, msg_id, seq, duplicate, stream)


def _classify_err(description: str) -> OutboxRelayError:
    if any(hint in description.lower() for hint in _TRANSIENT_ERR_HINTS):
        return TransientBrokerError(f"broker error: {description}")
    return PermanentPublishError(f"broker error: {description}")


def _hmsg_status(header: bytes) -> int | None:
    first = header.split(b"\r\n", 1)[0]
    parts = first.split(b" ", 2)
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    return None


class NatsTcpTransport:
    """NATS JetStream publisher over the plain-text TCP wire protocol.

    Performs the INFO/CONNECT handshake once, subscribes a single wildcard
    inbox, and for each publish sends ``HPUB`` carrying a real ``Nats-Msg-Id``
    header with the reply inbox set. The returned ``PubAck`` is the broker's
    JetStream pub-ack JSON, never fabricated locally. Timeouts and connection
    loss drop the socket and raise ``TransientBrokerError`` (the event stays
    unpublished and retryable); ``-ERR`` frames and pub-ack error codes below
    500 raise ``PermanentPublishError``; a 503 no-responders status means no
    stream is bound to the subject and stays transient.
    """

    def __init__(
        self,
        host: str,
        port: int = 4222,
        *,
        connect_timeout: float = 5.0,
        request_timeout: float = 10.0,
        name: str = "evallab-outbox-relay",
    ) -> None:
        if not host or not str(host).strip():
            raise ValueError("host is required")
        if min(connect_timeout, request_timeout) <= 0:
            raise ValueError("timeouts must be positive")
        self.host, self.port = str(host), int(port)
        self.connect_timeout = float(connect_timeout)
        self.request_timeout = float(request_timeout)
        self.name = name
        self._sock: socket.socket | None = None
        self._buf = b""
        self._inbox = ""
        self._counter = 0
        self._op_lock = threading.Lock()

    def _drop(self) -> None:
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
        self._sock, self._buf = None, b""

    def _deadline(self, timeout: float) -> float:
        return time.monotonic() + timeout

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("broker operation deadline exceeded")
        return remaining

    def _recv(self, deadline: float) -> bytes:
        sock = self._sock
        if sock is None:
            raise ConnectionError("not connected to the broker")
        sock.settimeout(self._remaining(deadline))
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("broker closed the connection")
        return chunk

    def _read_line(self, deadline: float) -> bytes:
        while b"\r\n" not in self._buf:
            self._buf += self._recv(deadline)
            if len(self._buf) > _MAX_BROKER_LINE:
                raise PermanentPublishError("broker control line exceeds the 1 MiB limit")
        line, self._buf = self._buf.split(b"\r\n", 1)
        return line

    def _read_exactly(self, count: int, deadline: float) -> bytes:
        while len(self._buf) < count:
            self._buf += self._recv(deadline)
        data, self._buf = self._buf[:count], self._buf[count:]
        return data

    def _send(self, data: bytes, deadline: float) -> None:
        sock = self._sock
        if sock is None:
            raise ConnectionError("not connected to the broker")
        sock.settimeout(self._remaining(deadline))
        sock.sendall(data)

    def _frame_size(self, field: bytes) -> int:
        try:
            size = int(field)
        except ValueError:
            raise TransientBrokerError(
                f"broker frame length is not an integer: {field[:32]!r}"
            ) from None
        if not 0 <= size <= _MAX_BROKER_LINE:
            raise TransientBrokerError(
                f"broker frame length {size} exceeds the {_MAX_BROKER_LINE}-byte limit"
            )
        return size

    def _read_frame_payload(self, size: int, deadline: float) -> bytes:
        data = self._read_exactly(size + 2, deadline)
        if data[-2:] != b"\r\n":
            raise TransientBrokerError("broker frame is missing its trailing CRLF")
        return data[:-2]

    def _ensure(self, deadline: float) -> None:
        if self._sock is not None:
            return
        handshake_deadline = min(deadline, self._deadline(self.connect_timeout))
        try:
            sock = socket.create_connection(
                (self.host, self.port),
                timeout=max(self._remaining(handshake_deadline), 0.001),
            )
        except OSError as exc:
            raise TransientBrokerError(f"cannot connect to broker: {exc}") from exc
        self._sock, self._buf = sock, b""
        self._inbox = f"_INBOX.{uuid4().hex}"
        try:
            greeting = self._read_line(handshake_deadline)
            if not greeting.startswith(b"INFO"):
                raise PermanentPublishError("first broker frame is not INFO")
            connect = {
                "verbose": False,
                "pedantic": False,
                "headers": True,
                "no_responders": True,
                "protocol": 1,
                "name": self.name,
                "lang": "python",
                "version": "1.0",
            }
            self._send(
                f"CONNECT {json.dumps(connect, separators=(',', ':'))}\r\nPING\r\n".encode(),
                handshake_deadline,
            )
            while True:
                line = self._read_line(handshake_deadline)
                if line == b"PONG":
                    break
                if line.startswith((b"+OK", b"INFO")):
                    continue
                if line == b"PING":
                    self._send(b"PONG\r\n", handshake_deadline)
                    continue
                if line.startswith(b"-ERR"):
                    raise _classify_err(line[4:].decode(errors="replace").strip(" '"))
                raise PermanentPublishError(f"unexpected broker greeting: {line[:80]!r}")
            self._send(f"SUB {self._inbox}.* 1\r\n".encode(), handshake_deadline)
        except BaseException:
            self._drop()
            raise

    def publish(self, subject: str, msg_id: str, payload: bytes) -> PubAck:
        """HPUB the envelope with its Nats-Msg-Id and await the real pub-ack."""
        _require_literal_subject(subject)
        _require_msg_id(msg_id)
        if not isinstance(payload, bytes | bytearray | memoryview):
            raise PermanentPublishError("payload must be bytes")
        body = bytes(payload)
        with self._op_lock:
            deadline = self._deadline(self.request_timeout)
            try:
                self._ensure(deadline)
                self._counter += 1
                inbox = f"{self._inbox}.{self._counter}"
                header = f"NATS/1.0\r\nNats-Msg-Id: {msg_id}\r\n\r\n".encode()
                frame = (
                    f"HPUB {subject} {inbox} {len(header)} {len(header) + len(body)}\r\n".encode()
                    + header
                    + body
                    + b"\r\n"
                )
                self._send(frame, deadline)
                return self._await_ack(inbox, subject, msg_id, deadline)
            except TimeoutError as exc:
                self._drop()
                raise TransientBrokerError(
                    "timed out waiting for the JetStream pub-ack"
                ) from exc
            except OSError as exc:
                self._drop()
                raise TransientBrokerError(f"broker connection failed: {exc}") from exc
            except OutboxRelayError:
                self._drop()
                raise

    def _await_ack(self, inbox: str, subject: str, msg_id: str, deadline: float) -> PubAck:
        while True:
            line = self._read_line(deadline)
            op, _, rest = line.partition(b" ")
            if op == b"PING":
                self._send(b"PONG\r\n", deadline)
                continue
            if op in (b"+OK", b"INFO", b"PONG"):
                continue
            if op == b"-ERR":
                raise _classify_err(rest.decode(errors="replace").strip(" '"))
            if op in (b"MSG", b"HMSG"):
                fields = rest.split(b" ")
                if len(fields) < 3 or (op == b"HMSG" and len(fields) < 4):
                    raise TransientBrokerError(f"malformed broker frame: {line[:80]!r}")
                addressed = fields[0].decode(errors="replace")
                if op == b"MSG":
                    size = self._frame_size(fields[-1])
                    payload = self._read_frame_payload(size, deadline)
                    if addressed != inbox:
                        continue
                    return _parse_puback(payload, subject, msg_id)
                header_size, total_size = (
                    self._frame_size(fields[-2]),
                    self._frame_size(fields[-1]),
                )
                if header_size > total_size:
                    raise TransientBrokerError(
                        f"broker frame header {header_size} exceeds total {total_size}"
                    )
                blob = self._read_frame_payload(total_size, deadline)
                if addressed != inbox:
                    continue
                status = _hmsg_status(blob[:header_size])
                if status == 503:
                    raise TransientBrokerError(
                        "no responders: no JetStream stream is bound to the subject"
                    )
                if status is not None:
                    if status >= 500:
                        raise TransientBrokerError(f"broker returned status {status}")
                    raise PermanentPublishError(f"broker rejected the publish: status {status}")
                return _parse_puback(blob[header_size:], subject, msg_id)
            raise PermanentPublishError(f"unexpected broker frame: {line[:80]!r}")

    def close(self) -> None:
        self._drop()

    def __enter__(self) -> NatsTcpTransport:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class HttpGatewayTransport:
    """Publisher for an explicitly configured HTTP relay gateway.

    Plain NATS exposes no HTTP publish endpoint: the configured gateway must
    terminate HTTP, publish to JetStream, and return a real pub-ack JSON body
    (``{"seq": ..., "duplicate": ...}``). A 2xx without a valid ack is not an
    acknowledgement — the event stays unpublished and retryable. 4xx codes
    (except retryable 408/429) are permanent; 5xx, timeouts, and connection
    errors are transient.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 10.0,
        token: str | None = None,
    ) -> None:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("gateway url must be an explicit http(s) endpoint")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.url, self.timeout, self.token = url, float(timeout), token

    def publish(self, subject: str, msg_id: str, payload: bytes) -> PubAck:
        _require_literal_subject(subject)
        _require_msg_id(msg_id)
        if not isinstance(payload, bytes | bytearray | memoryview):
            raise PermanentPublishError("payload must be bytes")
        headers = {
            "Content-Type": "application/json",
            "X-Subject": subject,
            "Nats-Msg-Id": msg_id,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.url, data=bytes(payload), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status, body = response.getcode(), response.read(_MAX_BROKER_LINE + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in (408, 429) or exc.code >= 500:
                raise TransientBrokerError(f"gateway returned HTTP {exc.code}") from exc
            raise PermanentPublishError(f"gateway rejected the publish: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise TransientBrokerError(f"gateway publish failed: {exc}") from exc
        if status is None or not 200 <= status < 300:
            raise TransientBrokerError(f"gateway returned unexpected status {status}")
        return _parse_puback(body, subject, msg_id)

    def close(self) -> None:
        """Stateless transport; nothing to release."""

    def __enter__(self) -> HttpGatewayTransport:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def _envelope_event_id(envelope: Any) -> str | None:
    if not isinstance(envelope, Mapping):
        return None
    event_id = envelope.get("event_id")
    return event_id if isinstance(event_id, str) and event_id else None


def _event_key(envelope: Any) -> str:
    event_id = _envelope_event_id(envelope)
    if event_id is not None:
        return f"event:{event_id}"
    if isinstance(envelope, Mapping) and type(envelope.get("seq")) is int:
        return f"seq:{envelope['seq']}"
    digest = hashlib.sha256(repr(envelope).encode()).hexdigest()[:16]
    return f"digest:{digest}"


def _snapshot(envelope: Any) -> dict[str, Any]:
    if isinstance(envelope, Mapping):
        try:
            decoded = json.loads(_canonical(envelope))
            if isinstance(decoded, dict):
                return decoded
        except (TypeError, ValueError):
            pass
    return {"malformed": repr(envelope)[:2048]}


class OutboxRelay:
    """Drain the durable production outbox into scoped JetStream subjects.

    ``drain`` validates each pending envelope (including the defense-in-depth
    TEST/heldout seal), routes it to ``evallab.<family>.<token>``, awaits a
    real pub-ack, and only then calls ``queue.mark_published``. A crash
    between ack and mark is repaired by ``Nats-Msg-Id`` deduplication on the
    next drain. Poison envelopes are quarantined durably before being marked
    consumed; transient broker failures halt the drain and stay retryable.
    ``quarantine`` is required for remote transports; ``LocalJetStreamAdapter``
    supplies its own durable poison table.
    """

    def __init__(
        self,
        queue: OutboxSource,
        publisher: EnvelopePublisher,
        *,
        quarantine: PoisonSink | None = None,
        batch_limit: int = 100,
    ) -> None:
        if quarantine is None:
            quarantine = publisher if isinstance(publisher, PoisonSink) else None
        if quarantine is None:
            raise ValueError(
                "remote transports require an explicit PoisonSink quarantine"
            )
        if type(batch_limit) is not int or batch_limit < 1:
            raise ValueError("batch_limit must be a positive integer")
        self.queue = queue
        self.publisher = publisher
        self.quarantine = quarantine
        self.batch_limit = batch_limit
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.publisher.close()
        if self.quarantine is not self.publisher:
            self.quarantine.close()

    def __enter__(self) -> OutboxRelay:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def drain(self, *, limit: int | None = None) -> DrainReport:
        """Publish pending outbox envelopes once each; returns the outcome.

        The drain halts on the first transient broker error (the broker is the
        shared resource; remaining envelopes keep their place and stay
        retryable). Invalid, heldout, and permanently rejected envelopes are
        quarantined and consumed so they cannot starve valid events.
        """
        if self._closed:
            raise RuntimeError("outbox relay is closed")
        pending = self.queue.pending_outbox(limit=limit if limit is not None else self.batch_limit)
        published: list[str] = []
        duplicates: list[str] = []
        quarantined: list[str] = []
        mark_failed: list[str] = []
        quarantine_failed: list[str] = []
        halted = False
        transient_error: str | None = None
        consumed = 0

        def mark(event_id: str) -> bool:
            try:
                self.queue.mark_published(event_id)
            except Exception:
                mark_failed.append(event_id)
                return False
            return True

        def quarantine(
            envelope: Any,
            key: str,
            event_id: str | None,
            subject: str | None,
            reason: str,
            detail: str,
        ) -> None:
            nonlocal consumed
            try:
                self.quarantine.record_poison(
                    key=key,
                    event_id=event_id,
                    subject=subject,
                    reason=reason,
                    detail=detail,
                    envelope=_snapshot(envelope),
                )
            except Exception:
                quarantine_failed.append(key)
                return
            quarantined.append(key)
            if event_id is None:
                mark_failed.append(key)
                return
            if mark(event_id):
                consumed += 1

        for envelope in pending:
            key = _event_key(envelope)
            event_id = _envelope_event_id(envelope)
            try:
                subject = route_subject(envelope)
                payload = _canonical(envelope)
            except InvalidEnvelopeError as exc:
                quarantine(envelope, key, event_id, None, exc.reason, str(exc))
                continue
            if event_id is None:
                # Unreachable: _validate_envelope requires a usable event_id.
                quarantine(envelope, key, None, subject, "invalid_envelope", "missing event_id")
                continue
            try:
                ack = self.publisher.publish(subject, event_id, payload)
            except TransientBrokerError as exc:
                halted = True
                transient_error = f"{type(exc).__name__}: {exc}"
                break
            except PermanentPublishError as exc:
                quarantine(envelope, key, event_id, subject, "publish_rejected", str(exc))
                continue
            if mark(event_id):
                consumed += 1
                published.append(event_id)
                if ack.duplicate:
                    duplicates.append(event_id)
        return DrainReport(
            examined=len(pending),
            published=tuple(published),
            duplicates=tuple(duplicates),
            quarantined=tuple(quarantined),
            mark_failed=tuple(mark_failed),
            quarantine_failed=tuple(quarantine_failed),
            halted=halted,
            transient_error=transient_error,
            deferred=len(pending) - consumed,
        )
