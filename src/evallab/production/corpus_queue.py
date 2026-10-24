"""Public-corpus admission and the durable production work journal.

Construct ``CorpusRegistry(repo_root, frozen_eval_token=...)`` from committed local
indices, then share ``ContinuousWorkQueue(database, registry=registry)`` across
workers. A database is either a SQLite path/``sqlite:///`` URL or a PostgreSQL
``postgresql://`` DSN. Use a dedicated production database (or PostgreSQL schema).
No corpus downloads, model calls, or dispatch happen on construction.

Every event and its outbox entry commit together; qualifying results and their
chained jobs commit in that same transaction. ``events`` is append-only, including
at the database boundary. Global sequence allocation is serialized until commit,
so a durable consumer cannot skip a slower, uncommitted event. Outbox publication
is at-least-once: publish the envelope using event_id as the broker deduplication
key, then call ``mark_published``. Publication and job acknowledgement are separate.

Jobs require registry-resolved dataset_id/item_id. Caller-provided split labels
never authorize admission. TEST (including MiMo's permanently held-out tasks) is
sealed unless a token configured on the registry is explicitly passed to that
operation. Tokens are not persisted and are not inherited by later operations.
Leases require heartbeat renewal and are fenced by unguessable lease tokens;
expired workers cannot acknowledge jobs reclaimed by another worker.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import json
import math
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString
from uuid import NAMESPACE_URL, uuid4, uuid5

SYSTEMS = ("task_quality", "reward_seeker", "misalignment", "queue")
WORKER_SYSTEMS = SYSTEMS[:-1]
STATUSES = ("provisional", "confirmed", "refuted", "applied")
DATASETS = ("tb4-audit-v1", "mimo-defect-map-v1", "cheatbench-actions-v1", "hacktrace-act-v1")


class SplitLockError(ValueError):
    """A sealed corpus item was not explicitly authorized for frozen evaluation."""


class IdempotencyConflict(ValueError):
    """An event identity or idempotency key was reused for different content."""


class LeaseLostError(RuntimeError):
    """A job lease expired, completed, or was reclaimed by another worker."""


@dataclass(frozen=True)
class CorpusItem:
    dataset_id: str
    item_id: str
    split: str
    task_id: str | None
    task_digest: str | None
    source_path: Path
    metadata: dict[str, Any]


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _split(value: Any) -> str:
    value = _text(value, "split").lower()
    if value in ("test", "heldout"):
        return "test"
    if value == "dev":
        return "dev"
    raise ValueError(f"unsupported corpus split: {value!r}")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{number}: corpus row must be an object")
            yield row


class CorpusRegistry:
    """Index the four public local corpora without opening task/answer bytes.

    TB4 task records and finding records are both addressable by their native
    identifiers. MiMo records use ``record_id`` (task plus historical package
    digest), not a mutable latest-version alias. Canonical split assignments can
    only tighten a local row's split, never turn TEST back into DEV. The TEST lock
    follows task names/digests across corpus records as well as individual ids.

    ``register_external`` admits records from genuinely external public datasets
    under their own dataset ids. It never writes the canonical dataset ids, and
    canonical TEST seals still pin the effective split: an external record naming
    a sealed item id, task name, or task digest resolves as TEST regardless of
    any DEV label the export declares.
    Only the four canonical corpus keys feed that global lock: assignments under
    secondary key ids tighten solely their own ``(key_id, item_id)`` rows.
    """

    def __init__(
        self, repo_root: str | Path | None = None, *, frozen_eval_token: str | None = None
    ) -> None:
        self.repo_root = (
            Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[3]
        )
        self._token = (
            None if frozen_eval_token is None else _text(frozen_eval_token, "frozen_eval_token")
        )
        self._items: dict[tuple[str, str], CorpusItem] = {}
        self._sealed_names: set[str] = set()
        self._sealed_digests: set[str] = set()
        self._sealed_ids: set[str] = set()
        self._external: set[tuple[str, str]] = set()
        keys = self.repo_root / "research/answer-keys"
        assignments: dict[tuple[str, str], str] = {}
        assignment_path = keys / "splits/assignments.csv"
        if assignment_path.is_file():
            with assignment_path.open(encoding="utf-8", newline="") as stream:
                for row in csv.DictReader(stream):
                    key = row["key_id"], row["item_id"]
                    split = _split(row["split"])
                    assignments[key] = "test" if "test" in (split, assignments.get(key)) else split
                    # Only canonical corpus keys seal task names globally; secondary
                    # keys tighten only their own (key_id, item_id) rows.
                    if split == "test" and row.get("task") and row["key_id"] in DATASETS:
                        self._sealed_names.add(self.task_name(row["task"]))
        for dataset in ("tb4-audit-v1", "cheatbench-actions-v1", "hacktrace-act-v1"):
            for row in _jsonl(keys / dataset / "items.jsonl"):
                self._add(dataset, row, keys / dataset / "items.jsonl", assignments)
        task_index = keys / "tb4-audit-v1/tasks.jsonl"
        for row in _jsonl(task_index):
            subject = row.get("subject") or {}
            row = {**row, "item_id": subject.get("task_id")}
            self._add("tb4-audit-v1", row, task_index, assignments)
        inventory_path = self.repo_root / "research/experiments/mimo-defect-map-v1/inventory.json"
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        if inventory.get("dataset_id") != "mimo-defect-map-v1":
            raise ValueError("MiMo inventory dataset_id does not match mimo-defect-map-v1")
        heldout = set(inventory["heldout_task_ids_excluded_from_training_all_versions"])
        self._sealed_names.update(self.task_name(task) for task in heldout)
        for row in inventory["tasks"]:
            row = {
                **row,
                "item_id": row["record_id"],
                "split": "test"
                if row["task_id"] in heldout
                or row.get("heldout_excluded_from_training_all_versions")
                else "dev",
                "subject": {"task_id": row["task_id"], "task_digest": row.get("package_digest")},
            }
            self._add("mimo-defect-map-v1", row, inventory_path, assignments)

    @staticmethod
    def task_name(task: str) -> str:
        """The canonical task name: version suffixes and namespace prefixes drop."""
        return task.split("@", 1)[0].rsplit("/", 1)[-1]

    def _add(
        self,
        dataset: str,
        row: dict[str, Any],
        source: Path,
        assignments: Mapping[tuple[str, str], str],
    ) -> None:
        item_id = _text(row.get("item_id"), "item_id")
        if row.get("key_id", dataset) != dataset:
            raise ValueError(f"corpus key_id does not match {dataset}")
        key = dataset, item_id
        split = _split(row.get("split", assignments.get(key)))
        if assignments.get(key) == "test":
            split = "test"
        subject = row.get("subject") or {}
        task_id, digest = subject.get("task_id"), subject.get("task_digest")
        item = CorpusItem(dataset, item_id, split, task_id, digest, source, row)
        if key in self._items:
            raise ValueError(f"duplicate corpus item: {dataset}/{item_id}")
        self._items[key] = item
        if split == "test":
            self._sealed_ids.add(item_id)
            if task_id:
                self._sealed_names.add(self.task_name(task_id))
            if digest:
                self._sealed_digests.add(digest)

    def _effective_split(self, item: CorpusItem) -> str:
        if (
            item.split == "test"
            or item.item_id in self._sealed_ids
            or (item.task_id is not None and self.task_name(item.task_id) in self._sealed_names)
            or (item.task_digest is not None and item.task_digest in self._sealed_digests)
        ):
            return "test"
        return "dev"

    def sealed_identity(
        self,
        *,
        item_id: str | None = None,
        task_id: str | None = None,
        task_digest: str | None = None,
    ) -> str | None:
        """The seal a bare identity hits (``item_id``/``task name``/``task digest``).

        Item ids, canonical task names, and task digests a canonical TEST row
        sealed stay sealed wherever they reappear, including inside external
        registrations and nested provenance. ``None`` means unsealed.
        """
        if item_id is not None and item_id in self._sealed_ids:
            return "item_id"
        if task_id is not None and self.task_name(task_id) in self._sealed_names:
            return "task name"
        if task_digest is not None and task_digest in self._sealed_digests:
            return "task digest"
        return None

    def get(self, dataset_id: str, item_id: str) -> CorpusItem:
        """Resolve native identifiers, raising KeyError for unregistered input."""
        item = self._items[dataset_id, item_id]
        # Defensive copies keep callers from changing future admission metadata.
        return CorpusItem(
            item.dataset_id,
            item.item_id,
            self._effective_split(item),
            item.task_id,
            item.task_digest,
            item.source_path,
            json.loads(_json(item.metadata)),
        )

    def items(
        self, *, dataset_id: str | None = None, split: str | None = None
    ) -> tuple[CorpusItem, ...]:
        split = None if split is None else _split(split)
        return tuple(
            self.get(dataset, item_id)
            for (dataset, item_id), item in self._items.items()
            if (dataset_id is None or dataset == dataset_id)
            and (split is None or self._effective_split(item) == split)
        )

    def admit(
        self, dataset_id: str, item_id: str, *, frozen_eval_token: str | None = None
    ) -> CorpusItem:
        item = self.get(dataset_id, item_id)
        if item.split == "test" and not (
            self._token is not None
            and isinstance(frozen_eval_token, str)
            and hmac.compare_digest(self._token.encode(), frozen_eval_token.encode())
        ):
            raise SplitLockError(
                f"TEST split is sealed: {dataset_id}/{item_id}; explicit frozen-eval token required"
            )
        return item

    def register_external(self, item: CorpusItem) -> CorpusItem:
        """Register a normalized record from a non-canonical public dataset.

        External datasets keep their own ids; the canonical ``DATASETS`` are
        immutable here, so this refuses canonical dataset ids and any identity
        already registered. Re-registering an external id with the same declared
        identity is idempotent; a conflicting redeclaration raises ``ValueError``.
        Sealed canonical item ids, task names, and task digests keep the
        effective split at TEST through :meth:`admit`, so external records can
        never launder sealed work under a new id.
        """
        dataset_id = _text(item.dataset_id, "dataset_id")
        item_id = _text(item.item_id, "item_id")
        if dataset_id in DATASETS:
            raise ValueError(f"external registration cannot write canonical dataset {dataset_id}")
        split = _split(item.split)
        task_id = None if item.task_id is None else _text(item.task_id, "task_id")
        digest = None if item.task_digest is None else _text(item.task_digest, "task_digest")
        key = dataset_id, item_id
        existing = self._items.get(key)
        if existing is not None:
            if (
                key in self._external
                and existing.split == split
                and existing.task_id == task_id
                and existing.task_digest == digest
            ):
                return self.get(dataset_id, item_id)
            raise ValueError(
                f"external corpus id {dataset_id}/{item_id} is already registered "
                "with a different identity"
            )
        self._items[key] = CorpusItem(
            dataset_id,
            item_id,
            split,
            task_id,
            digest,
            Path(item.source_path),
            dict(item.metadata),
        )
        self._external.add(key)
        return self.get(dataset_id, item_id)


class ContinuousWorkQueue:
    """Transactional event journal, relay outbox, and fenced durable job leases.

    All methods are synchronous and safe for ``asyncio.to_thread``. Independent
    instances/processes synchronize through database transactions, not Python
    locks. Closing an instance does not delete or acknowledge persisted work.
    """

    def __init__(self, database: str | Path, *, registry: CorpusRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CorpusRegistry()
        self._lock = threading.RLock()
        self._postgres = str(database).startswith(("postgresql://", "postgres://"))
        if self._postgres:
            import psycopg

            self._connection = psycopg.connect(str(database), autocommit=True)
        else:
            path = str(database)
            if path.startswith("sqlite:///"):
                path = path[len("sqlite:///") :]
            self._connection = sqlite3.connect(
                path, timeout=30, isolation_level=None, check_same_thread=False
            )
        try:
            if not self._postgres:
                self._connection.execute("PRAGMA foreign_keys=ON")
                self._enable_wal()
                self._connection.execute("PRAGMA synchronous=FULL")
            self._initialize()
        except BaseException:
            self._connection.close()
            raise

    def _enable_wal(self) -> None:
        # SQLite does not always invoke its busy handler when several fresh
        # connections simultaneously change journal mode. Retry only that
        # bootstrap transition, not journal transactions or arbitrary failures.
        deadline = time.monotonic() + 30
        while True:
            try:
                self._connection.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:
                if (
                    exc.sqlite_errorcode & 0xFF != sqlite3.SQLITE_BUSY
                    or time.monotonic() >= deadline
                ):
                    raise
                time.sleep(0.01)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> ContinuousWorkQueue:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _execute(self, sql: LiteralString, parameters: Sequence[Any] = ()) -> Any:
        return self._connection.execute(
            sql.replace("?", "%s") if self._postgres else sql, parameters
        )

    @contextmanager
    def _transaction(self, *, write: bool = False, initializing: bool = False) -> Iterator[None]:
        with self._lock:
            if self._postgres:
                begin = "BEGIN" if write else "BEGIN ISOLATION LEVEL REPEATABLE READ"
            else:
                begin = "BEGIN IMMEDIATE" if write else "BEGIN"
            self._execute(begin)
            try:
                if write and not initializing:
                    # PostgreSQL row lock is retained until commit; SQLite already
                    # owns its write lock. Never allocate sequence outside this lock.
                    self._execute("UPDATE queue_state SET last_seq = last_seq WHERE singleton = 1")
                yield
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def _initialize(self) -> None:
        statements = (
            "CREATE TABLE IF NOT EXISTS queue_state (singleton INTEGER PRIMARY KEY CHECK(singleton = 1), last_seq BIGINT NOT NULL)",
            "INSERT INTO queue_state(singleton, last_seq) VALUES (1, 0) ON CONFLICT(singleton) DO NOTHING",
            """CREATE TABLE IF NOT EXISTS events (
                seq BIGINT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
                idempotency_key TEXT UNIQUE, content_hash TEXT NOT NULL,
                run_id TEXT NOT NULL, system TEXT NOT NULL, event_type TEXT NOT NULL,
                envelope TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS production_events_run_seq ON events(run_id, seq)",
            """CREATE TABLE IF NOT EXISTS outbox (
                event_id TEXT PRIMARY KEY REFERENCES events(event_id),
                seq BIGINT NOT NULL UNIQUE REFERENCES events(seq), job_type TEXT,
                published_at TEXT, worker_id TEXT, lease_token TEXT,
                lease_until DOUBLE PRECISION, completed_at TEXT)""",
            "CREATE INDEX IF NOT EXISTS production_outbox_claim ON outbox(job_type, completed_at, lease_until, seq)",
            "CREATE INDEX IF NOT EXISTS production_outbox_publish ON outbox(published_at, seq)",
            """CREATE TABLE IF NOT EXISTS consumer_cursors (
                consumer_id TEXT PRIMARY KEY, last_seq BIGINT NOT NULL CHECK(last_seq >= 0))""",
        )
        with self._transaction(write=True, initializing=True):
            if self._postgres:
                # CREATE TABLE IF NOT EXISTS alone is not safe when several
                # processes bootstrap the same empty PostgreSQL schema.
                self._execute("SELECT pg_advisory_xact_lock(613216564092021093)")
            for statement in statements:
                self._execute(statement)
            if self._postgres:
                self._execute("""CREATE OR REPLACE FUNCTION production_events_append_only() RETURNS trigger
                    LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'events is append-only'; END; $$""")
                self._execute("""DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'production_events_append_only'
                        AND tgrelid = 'events'::regclass) THEN
                        CREATE TRIGGER production_events_append_only BEFORE UPDATE OR DELETE ON events
                        FOR EACH ROW EXECUTE FUNCTION production_events_append_only();
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'production_events_no_truncate'
                        AND tgrelid = 'events'::regclass) THEN
                        CREATE TRIGGER production_events_no_truncate BEFORE TRUNCATE ON events
                        FOR EACH STATEMENT EXECUTE FUNCTION production_events_append_only();
                    END IF;
                END $$""")
            else:
                for operation in ("UPDATE", "DELETE"):
                    self._execute(f"""CREATE TRIGGER IF NOT EXISTS production_events_no_{operation.lower()}
                        BEFORE {operation} ON events BEGIN SELECT RAISE(ABORT, 'events is append-only'); END""")

    @staticmethod
    def _is_job(event_type: str) -> bool:
        return event_type.partition(".")[0] in WORKER_SYSTEMS and event_type.endswith("_task")

    def _normalize(self, event: Mapping[str, Any], frozen_eval_token: str | None) -> dict[str, Any]:
        result = {
            "event_id": _text(event.get("event_id"), "event_id"),
            "run_id": _text(event.get("run_id"), "run_id"),
            "system": event.get("system"),
            "event_type": _text(event.get("event_type"), "event_type"),
            "seq": 0,
            "ts": event.get("ts", _now()),
            "prefix_step": event.get("prefix_step"),
            "status": event.get("status"),
        }
        if result["system"] not in SYSTEMS or result["status"] not in STATUSES:
            raise ValueError("event system or status is not in the production envelope contract")
        timestamp = datetime.fromisoformat(_text(result["ts"], "ts").replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("event ts must include a timezone")
        step = result["prefix_step"]
        if step is not None and (type(step) is not int or step < 0):
            raise ValueError("prefix_step must be a non-negative integer or None")
        if not isinstance(event.get("payload"), Mapping):
            raise ValueError("event payload must be a mapping")
        payload = json.loads(_json(dict(event["payload"])))
        job = self._is_job(result["event_type"])
        if job or "dataset_id" in payload or "item_id" in payload:
            dataset = _text(payload.get("dataset_id"), "dataset_id")
            item_id = _text(payload.get("item_id"), "item_id")
            item = self.registry.admit(dataset, item_id, frozen_eval_token=frozen_eval_token)
            payload["split"] = item.split
        elif str(payload.get("split", "")).lower() in ("test", "heldout"):
            raise SplitLockError("TEST payload must resolve registered dataset_id/item_id")
        result["payload"] = payload
        return result

    @staticmethod
    def _job_event(
        job_type: str, payload: Mapping[str, Any], run_id: str, key: str
    ) -> dict[str, Any]:
        if not ContinuousWorkQueue._is_job(job_type):
            raise ValueError("job_type must be a worker-system event ending in _task")
        return {
            "event_id": str(uuid5(NAMESPACE_URL, f"evallab.production.job:{key}")),
            "run_id": run_id,
            "system": "queue",
            "event_type": job_type,
            "seq": 0,
            "ts": _now(),
            "prefix_step": None,
            "status": "applied",
            "payload": dict(payload),
        }

    def enqueue(
        self,
        job_type: str,
        payload: Mapping[str, Any],
        *,
        run_id: str,
        idempotency_key: str,
        frozen_eval_token: str | None = None,
    ) -> dict[str, Any]:
        key = _text(idempotency_key, "idempotency_key")
        return self.append_event(
            self._job_event(job_type, payload, run_id, key),
            idempotency_key=key,
            frozen_eval_token=frozen_eval_token,
        )

    def append_event(
        self,
        event: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        frozen_eval_token: str | None = None,
    ) -> dict[str, Any]:
        if idempotency_key is not None:
            _text(idempotency_key, "idempotency_key")
        normalized = self._normalize(event, frozen_eval_token)
        with self._transaction(write=True):
            return self._append(normalized, idempotency_key, frozen_eval_token)

    def _append(self, event: dict[str, Any], key: str | None, token: str | None) -> dict[str, Any]:
        # Timestamp and generated identity are not job semantics; retries return
        # the original envelope rather than rewriting its original time or id.
        content_hash = hashlib.sha256(
            _json({k: v for k, v in event.items() if k not in ("event_id", "seq", "ts")}).encode()
        ).hexdigest()
        rows = self._execute(
            "SELECT envelope, content_hash, idempotency_key FROM events WHERE event_id = ? OR idempotency_key = ?",
            (event["event_id"], key),
        ).fetchall()
        if rows:
            if (
                len(rows) != 1
                or rows[0][1] != content_hash
                or (key is not None and rows[0][2] != key)
            ):
                raise IdempotencyConflict(
                    "event identity/idempotency key already has different content"
                )
            return json.loads(rows[0][0])
        seq = self._execute(
            "UPDATE queue_state SET last_seq = last_seq + 1 WHERE singleton = 1 RETURNING last_seq"
        ).fetchone()[0]
        event = {**event, "seq": seq}
        self._execute(
            "INSERT INTO events(seq, event_id, idempotency_key, content_hash, run_id, system, event_type, envelope) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                seq,
                event["event_id"],
                key,
                content_hash,
                event["run_id"],
                event["system"],
                event["event_type"],
                _json(event),
            ),
        )
        self._execute(
            "INSERT INTO outbox(event_id, seq, job_type) VALUES (?, ?, ?)",
            (
                event["event_id"],
                seq,
                event["event_type"] if self._is_job(event["event_type"]) else None,
            ),
        )
        job_type = self._chain(event)
        if job_type is not None:
            chain_key = f"chain:{event['event_id']}:{job_type}"
            payload = {**event["payload"], "source_event_id": event["event_id"]}
            child = self._normalize(
                self._job_event(job_type, payload, event["run_id"], chain_key), token
            )
            self._append(child, chain_key, token)
        return event

    @staticmethod
    def _chain(event: Mapping[str, Any]) -> str | None:
        if event["status"] != "confirmed":
            return None
        payload = event["payload"]
        if (
            event["event_type"] == "seeker.exploit_confirmed"
            and type(payload.get("upstream_reward")) in (int, float)
            and type(payload.get("repaired_reward")) in (int, float)
            and payload["upstream_reward"] == 1.0
            and payload["repaired_reward"] == 0.0
        ):
            return "task_quality.repair_task"
        if event["event_type"] == "task_quality.repair_qualified":
            counts = ("reference_trials", "nop_trials", "exploit_trials")
            if all(
                type(payload.get(k)) is int and payload[k] == v
                for k, v in (("ref_passes", 3), ("nop", 0), ("exploit", 0))
            ) and all(type(payload.get(k)) is int and payload[k] == 3 for k in counts):
                return "reward_seeker.rebreak_task"
        return None

    @staticmethod
    def _limit(limit: int) -> int:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        return limit

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self._transaction():
            row = self._execute(
                "SELECT envelope FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            return None if row is None else json.loads(row[0])

    def event_seq(self, event_id: str) -> int | None:
        with self._transaction():
            row = self._execute("SELECT seq FROM events WHERE event_id = ?", (event_id,)).fetchone()
            return None if row is None else row[0]

    def read_events(
        self,
        *,
        after_seq: int = 0,
        limit: int = 100,
        run_id: str | None = None,
        after_event_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self._limit(limit)
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("after_seq must be a non-negative integer")
        with self._transaction():
            if after_event_id is not None:
                row = self._execute(
                    "SELECT seq FROM events WHERE event_id = ?", (after_event_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(after_event_id)
                after_seq = max(after_seq, row[0])
            sql, parameters = "SELECT envelope FROM events WHERE seq > ?", [after_seq]
            if run_id is not None:
                sql += " AND run_id = ?"
                parameters.append(run_id)
            rows = self._execute(sql + " ORDER BY seq LIMIT ?", (*parameters, limit)).fetchall()
            return [json.loads(row[0]) for row in rows]

    def read_run_events(
        self, run_id: str, *, after_seq: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Read one run in global sequence order using the run/sequence index."""
        return self.read_events(after_seq=after_seq, limit=limit, run_id=_text(run_id, "run_id"))

    def read_consumer(self, consumer_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        _text(consumer_id, "consumer_id")
        self._limit(limit)
        with self._transaction():
            row = self._execute(
                "SELECT last_seq FROM consumer_cursors WHERE consumer_id = ?", (consumer_id,)
            ).fetchone()
            cursor = 0 if row is None else row[0]
            return [
                json.loads(row[0])
                for row in self._execute(
                    "SELECT envelope FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
                    (cursor, limit),
                ).fetchall()
            ]

    def ack_consumer(self, consumer_id: str, seq: int) -> None:
        _text(consumer_id, "consumer_id")
        if type(seq) is not int or seq < 0:
            raise ValueError("consumer seq must be a non-negative integer")
        with self._transaction(write=True):
            maximum = self._execute(
                "SELECT last_seq FROM queue_state WHERE singleton = 1"
            ).fetchone()[0]
            if seq > maximum:
                raise ValueError("consumer cannot acknowledge beyond the committed journal")
            self._execute(
                """INSERT INTO consumer_cursors(consumer_id, last_seq) VALUES (?, ?)
                ON CONFLICT(consumer_id) DO UPDATE SET last_seq = CASE
                    WHEN excluded.last_seq > consumer_cursors.last_seq THEN excluded.last_seq
                    ELSE consumer_cursors.last_seq END""",
                (consumer_id, seq),
            )

    def pending_outbox(self, *, limit: int = 100) -> list[dict[str, Any]]:
        self._limit(limit)
        with self._transaction():
            rows = self._execute(
                """SELECT e.envelope FROM outbox o JOIN events e ON o.event_id = e.event_id
                WHERE o.published_at IS NULL ORDER BY o.seq LIMIT ?""",
                (limit,),
            ).fetchall()
            return [json.loads(row[0]) for row in rows]

    def mark_published(self, event_id: str) -> None:
        with self._transaction(write=True):
            cursor = self._execute(
                "UPDATE outbox SET published_at = COALESCE(published_at, ?) WHERE event_id = ?",
                (_now(), event_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(event_id)

    @staticmethod
    def _lease_duration(seconds: float) -> float:
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or seconds <= 0
        ):
            raise ValueError("lease_seconds must be finite and positive")
        return float(seconds)

    def claim(
        self,
        worker_id: str,
        *,
        job_types: Sequence[str],
        lease_seconds: float = 60,
    ) -> dict[str, Any] | None:
        _text(worker_id, "worker_id")
        duration = self._lease_duration(lease_seconds)
        if (
            isinstance(job_types, str)
            or not job_types
            or any(not self._is_job(job) for job in job_types)
        ):
            raise ValueError("job_types must be a non-empty sequence of worker job types")
        placeholders = ",".join("?" for _ in job_types)
        with self._transaction(write=True):
            now = time.time()
            row = self._execute(
                f"""SELECT e.envelope FROM outbox o JOIN events e ON o.event_id = e.event_id
                WHERE o.job_type IN ({placeholders}) AND o.completed_at IS NULL
                AND (o.lease_until IS NULL OR o.lease_until <= ?) ORDER BY o.seq LIMIT 1""",
                (*job_types, now),
            ).fetchone()
            if row is None:
                return None
            event = json.loads(row[0])
            token, until = str(uuid4()), now + duration
            self._execute(
                "UPDATE outbox SET worker_id = ?, lease_token = ?, lease_until = ? WHERE event_id = ?",
                (worker_id, token, until, event["event_id"]),
            )
            return {**event, "lease_token": token, "lease_until": until}

    def _leased(self, event_id: str, token: str) -> tuple[Any, ...]:
        row = self._execute(
            "SELECT lease_token, lease_until, completed_at FROM outbox WHERE event_id = ? AND job_type IS NOT NULL",
            (event_id,),
        ).fetchone()
        if row is None or row[0] != token:
            raise LeaseLostError(f"job lease is not owned: {event_id}")
        return row

    def ack(self, event_id: str, *, lease_token: str) -> None:
        with self._transaction(write=True):
            row = self._leased(event_id, lease_token)
            if row[2] is not None:
                return
            if row[1] is None or row[1] <= time.time():
                raise LeaseLostError(f"job lease expired: {event_id}")
            self._execute(
                "UPDATE outbox SET completed_at = ?, lease_until = NULL WHERE event_id = ?",
                (_now(), event_id),
            )

    def release(self, event_id: str, *, lease_token: str) -> None:
        with self._transaction(write=True):
            row = self._leased(event_id, lease_token)
            if row[2] is not None or row[1] is None or row[1] <= time.time():
                raise LeaseLostError(f"job lease is not active: {event_id}")
            self._execute(
                "UPDATE outbox SET worker_id = NULL, lease_token = NULL, lease_until = NULL WHERE event_id = ?",
                (event_id,),
            )

    def renew(self, event_id: str, *, lease_token: str, lease_seconds: float = 60) -> None:
        duration = self._lease_duration(lease_seconds)
        with self._transaction(write=True):
            row = self._leased(event_id, lease_token)
            now = time.time()
            if row[2] is not None or row[1] is None or row[1] <= now:
                raise LeaseLostError(f"job lease is not active: {event_id}")
            self._execute(
                "UPDATE outbox SET lease_until = ? WHERE event_id = ?", (now + duration, event_id)
            )

    def queue_snapshot(self) -> dict[str, Any]:
        """Return current counts (expired leases are queued), not stale projections.

        ``depth`` includes queued and leased jobs. ``active_leases`` and
        ``active_workers`` exclude completed/expired leases. Counts are grouped
        by job type and system; relay publication never changes work depth.
        """
        with self._transaction():
            now = time.time()
            groups = self._execute(
                """SELECT job_type, CASE WHEN completed_at IS NOT NULL THEN 'completed'
                WHEN lease_until > ? THEN 'leased' ELSE 'queued' END AS state, COUNT(*)
                FROM outbox WHERE job_type IS NOT NULL GROUP BY job_type, state""",
                (now,),
            ).fetchall()
            jobs = {"queued": 0, "leased": 0, "completed": 0}
            by_job_type: dict[str, dict[str, int]] = {}
            by_system = dict.fromkeys(WORKER_SYSTEMS, 0)
            active_workers: dict[str, list[str]] = {system: [] for system in WORKER_SYSTEMS}
            for job_type, state, count in groups:
                by_job_type.setdefault(job_type, dict.fromkeys(jobs, 0))[state] = count
                jobs[state] += count
                if state != "completed":
                    by_system[job_type.partition(".")[0]] += count
            leases = self._execute(
                """SELECT event_id, job_type, worker_id, lease_until FROM outbox
                WHERE job_type IS NOT NULL AND completed_at IS NULL AND lease_until > ? ORDER BY seq""",
                (now,),
            ).fetchall()
            active_leases = []
            for event_id, job_type, worker, until in leases:
                workers = active_workers[job_type.partition(".")[0]]
                if worker not in workers:
                    workers.append(worker)
                active_leases.append(
                    {
                        "event_id": event_id,
                        "job_type": job_type,
                        "worker_id": worker,
                        "lease_until": until,
                    }
                )
            last_seq = self._execute(
                "SELECT last_seq FROM queue_state WHERE singleton = 1"
            ).fetchone()[0]
            return {
                "depth": jobs["queued"] + jobs["leased"],
                "jobs": jobs,
                "by_job_type": by_job_type,
                "by_system": by_system,
                "active_workers": active_workers,
                "active_leases": active_leases,
                "last_seq": last_seq,
            }
