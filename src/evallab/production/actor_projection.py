"""Physically separate, caller-admitted SQLite projections for cloud actors.

The caller must obtain the exact task IDs from the canonical admission gate.
Checking a source row's DEV label here is a consistency check, not authorization
or evidence verification. No source pages, source FTS tables, or CAS bytes are
copied; the new database contains only explicitly selected relational rows.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from evallab.production.cloud_trace_db import CloudTraceDatabase

_TABLES = ("corpus_tasks", "task_issues", "agent_traces", "trace_steps")
_ORDER_BY = {
    "corpus_tasks": "row.task_id",
    "task_issues": "row.issue_id",
    "agent_traces": "row.trace_id",
    "trace_steps": "row.trace_id, row.step_index",
}
_IDENTITY_FIELDS = (
    "task_id", "dataset_id", "split", "original_grader_digest", "repaired_grader_digest",
    "item_id", "item_type",
)


def _admitted_ids(task_ids: Iterable[str]) -> list[str]:
    if isinstance(task_ids, (str, bytes)):
        raise TypeError("task_ids must be an explicit collection of task IDs")
    admitted: set[str] = set()
    for task_id in task_ids:
        if not isinstance(task_id, str) or not task_id.strip() or "\x00" in task_id:
            raise ValueError("task_ids must contain nonempty strings without NUL")
        admitted.add(task_id)
    if not admitted:
        raise ValueError("task_ids must contain at least one explicitly admitted task")
    return sorted(admitted)


def _expected_identities(
    admitted: list[str], expected: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, dict[str, Any]] | None:
    if expected is None:
        return None
    if set(expected) != set(admitted):
        raise ValueError("expected_identities must cover exactly the admitted task IDs")
    identities = {}
    for task_id in admitted:
        identity = dict(expected[task_id])
        if set(identity) != set(_IDENTITY_FIELDS) or identity["task_id"] != task_id:
            raise ValueError("expected_identities must contain every exact task identity field")
        identities[task_id] = identity
    return identities


def source_read_uri(source: Path) -> str:
    """Return a read URI that cannot create sidecar files beside the source.

    SQLite writes ``-shm``/``-wal`` bookkeeping for any read of a WAL-format
    database, which would mutate the source directory. A closed, checkpointed WAL
    database can be read as an immutable image with no side effects; a WAL database
    with a live journal cannot, so that case is refused rather than silently read
    without its committed tail.
    """
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        header = os.read(descriptor, 100)
    finally:
        os.close(descriptor)
    if len(header) < 20:
        raise ValueError("source database is too small to be a SQLite database")
    if header[18:20] == b"\x01\x01":
        return source.as_uri() + "?mode=ro"
    for suffix in ("-wal", "-shm"):
        if (source.parent / (source.name + suffix)).exists():
            raise ValueError(
                "source database has a live WAL; close and checkpoint it before projection "
                "so no sidecar files are written beside the source",
            )
    return source.as_uri() + "?mode=ro&immutable=1"


def _paths(source_database: Path, destination_database: Path) -> tuple[Path, Path]:
    source = Path(source_database)
    source_stat = source.lstat()
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
        raise ValueError("source_database must be a regular, non-symlink file")
    destination = Path(destination_database)
    if destination.is_symlink():
        raise ValueError("destination_database must not be a symlink")
    if destination.exists():
        raise FileExistsError("destination_database already exists; refusing overwrite")
    # Resolve parents once, including platform aliases such as /tmp on macOS.
    # The output directory must already exist; this function owns only its staging directory.
    source = source.resolve(strict=True)
    destination = destination.parent.resolve(strict=True) / destination.name
    if destination == source:
        raise ValueError("source_database and destination_database must be distinct")
    return source, destination


def _columns(connection: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    columns: dict[str, tuple[str, ...]] = {}
    for table in _TABLES:
        kind = connection.execute(
            "SELECT type FROM source.sqlite_schema WHERE name = ?", (table,),
        ).fetchone()
        if kind != ("table",):
            raise ValueError(f"source database is missing the core table {table}")
        expected = connection.execute(f'PRAGMA main.table_xinfo("{table}")').fetchall()
        actual = connection.execute(f'PRAGMA source.table_xinfo("{table}")').fetchall()
        if actual != expected:
            raise ValueError(f"source columns differ from the CloudTraceDatabase schema: {table}")
        columns[table] = tuple(row[1] for row in expected)
    return columns


def _validate_admission(
    connection: sqlite3.Connection, expected: dict[str, dict[str, Any]] | None,
) -> None:
    # Identity and split metadata only: do not load any task/trace body before
    # every requested ID has passed. BINARY prevents a source collation from
    # widening exact caller-supplied identities. JSON remains inside SQLite;
    # only item identity fields cross the boundary for comparison.
    item_fields = (
        "json_extract(task.metadata_json, '$.item_id'), "
        "json_type(task.metadata_json, '$.item_id')"
        if expected is not None else "NULL, NULL"
    )
    rows = connection.execute(
        f"""SELECT admitted.task_id, task.task_id, task.dataset_id, task.split,
                   task.original_grader_digest, task.repaired_grader_digest, {item_fields}
            FROM temp.actor_admitted_tasks AS admitted
            LEFT JOIN source.corpus_tasks AS task
              ON task.task_id COLLATE BINARY = admitted.task_id
            ORDER BY admitted.task_id""",
    )
    for row in rows:
        requested, found, _, split, *_ = row
        if found is None:
            raise ValueError(f"requested task does not exist in source: {requested!r}")
        if not isinstance(split, str) or split.strip().casefold() != "dev":
            raise ValueError(f"requested task is not DEV in source: {requested!r}")
        if expected is not None:
            actual = dict(zip(_IDENTITY_FIELDS, row[1:], strict=True))
            if actual != expected[requested]:
                raise ValueError(f"source identity changed for admitted task: {requested!r}")


def _validate_step_counts(connection: sqlite3.Connection, schema: str) -> None:
    # Both callers supply an internal schema constant. For source, inspect only
    # admitted identities/counts; unrelated source corruption is not exported.
    admission = (
        "JOIN temp.actor_admitted_tasks AS admitted "
        "ON trace.task_id COLLATE BINARY = admitted.task_id"
        if schema == "source" else ""
    )
    mismatch = connection.execute(
        f"""SELECT trace.trace_id FROM {schema}.agent_traces AS trace {admission}
            WHERE trace.step_count IS NOT (
                SELECT COUNT(*) FROM {schema}.trace_steps AS step
                WHERE step.trace_id COLLATE BINARY = trace.trace_id
            ) LIMIT 1""",
    ).fetchone()
    if mismatch is not None:
        raise ValueError("admitted trace step_count does not match its stored steps")


def _copy_rows(
    connection: sqlite3.Connection, columns: dict[str, tuple[str, ...]],
) -> None:
    for table in _TABLES:
        names = ", ".join(f'"{name}"' for name in columns[table])
        values = ", ".join(f'row."{name}"' for name in columns[table])
        join = (
            "JOIN main.agent_traces AS trace "
            "ON row.trace_id COLLATE BINARY = trace.trace_id"
            if table == "trace_steps" else
            "JOIN temp.actor_admitted_tasks AS admitted "
            "ON row.task_id COLLATE BINARY = admitted.task_id"
        )
        connection.execute(
            f'INSERT INTO main."{table}" ({names}) '
            f'SELECT {values} FROM source."{table}" AS row {join} '
            f"ORDER BY {_ORDER_BY[table]}",
        )


def _validate_projection(connection: sqlite3.Connection) -> dict[str, int]:
    if connection.execute("PRAGMA main.foreign_key_check").fetchone() is not None:
        raise ValueError("projected database failed foreign-key validation")
    integrity = connection.execute("PRAGMA main.integrity_check").fetchall()
    if integrity != [("ok",)]:
        raise ValueError("projected database failed SQLite integrity validation")
    _validate_step_counts(connection, "main")
    counts = {}
    for table in _TABLES:
        index = f"{table}_fts"
        if connection.execute(
            "SELECT 1 FROM main.sqlite_schema WHERE type = 'table' AND name = ?", (index,),
        ).fetchone() is None:
            raise ValueError("projected database requires SQLite FTS5 support")
        # rank=1 checks the regenerated index against its external content table.
        connection.execute(
            f'INSERT INTO main."{index}" ("{index}", rank) VALUES (\'integrity-check\', 1)',
        )
        counts[table] = connection.execute(f'SELECT COUNT(*) FROM main."{table}"').fetchone()[0]
    return counts


def project_actor_database(
    source_database: Path, destination_database: Path, *, task_ids: Iterable[str],
    expected_identities: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Publish a fresh database containing exactly the caller-admitted DEV tasks.

    Empty admission is refused, as are missing/non-DEV tasks, schema drift, and
    inconsistent step counts. IDs are deduplicated but never normalized. The
    existing destination parent must be a directory; source/destination file
    symlinks and any existing destination are refused. Source reads share one
    SQLite snapshot through a mode=ro attachment. Publication is an atomic,
    no-clobber hard link on the destination filesystem, after validation/close.
    When supplied, expected_identities must cover exactly the admitted IDs and
    every identity field; stale dataset/split/digest/item metadata is rejected
    inside that same source snapshot, before any content is copied.

    The receipt records task IDs, core counts, database hash/size and the supplied
    identity-set digest. Without expected_identities that digest is null: bare-ID
    extraction is not a snapshot-bound registry admission assertion. Labels,
    JSON and text retain source values, not independently verified evidence.
    """
    admitted = _admitted_ids(task_ids)
    expected = _expected_identities(admitted, expected_identities)
    source, destination = _paths(source_database, destination_database)
    with tempfile.TemporaryDirectory(
        prefix=f".{destination.name}.projection-", dir=destination.parent,
    ) as directory:
        staged = Path(directory) / "projection.sqlite"
        # The canonical class owns core DDL, FTS definitions, and their triggers.
        # Never open the source through this initializing, write-capable class.
        with CloudTraceDatabase(staged):
            pass
        os.chmod(staged, 0o600)
        with closing(sqlite3.connect(staged, isolation_level=None, uri=True)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("ATTACH DATABASE ? AS source", (source_read_uri(source),))
            connection.execute(
                "CREATE TEMP TABLE actor_admitted_tasks "
                "(task_id TEXT PRIMARY KEY) WITHOUT ROWID",
            )
            connection.execute("BEGIN")
            with connection:
                connection.executemany(
                    "INSERT INTO temp.actor_admitted_tasks (task_id) VALUES (?)",
                    ((task_id,) for task_id in admitted),
                )
                columns = _columns(connection)
                _validate_admission(connection, expected)
                _validate_step_counts(connection, "source")
                _copy_rows(connection, columns)
                counts = _validate_projection(connection)
        # A bundle file must stand alone: refuse to publish a WAL-mode database
        # whose committed pages can live in a -wal sidecar left behind.
        with closing(sqlite3.connect(staged, isolation_level=None)) as connection:
            mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        if mode != ("delete",):
            raise ValueError(f"could not normalize projected journal mode: {mode!r}")
        with staged.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            os.fsync(stream.fileno())
        receipt = {
            "schema": "evallab.actor_projection/v1",
            "admitted_task_ids": admitted,
            "table_counts": counts,
            "database_sha256": digest,
            "database_bytes": staged.stat().st_size,
            "admitted_identities_sha256": (
                hashlib.sha256(json.dumps(
                    expected, sort_keys=True, separators=(",", ":"), allow_nan=False,
                ).encode()).hexdigest() if expected is not None else None
            ),
        }
        # os.replace would overwrite a concurrently created destination. link
        # refuses it atomically, including a dangling symlink; cleanup removes
        # only our private staging directory, never the caller's output path.
        os.link(staged, destination, follow_symlinks=False)
    return receipt
