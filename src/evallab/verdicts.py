"""Verdict persistence, validation, and query engine (§2.1, §2.2).

Verdicts record human dispositions on discovery journal findings
(accepted, rejected, needs_evidence, pending). A verdict is strictly
append-only: changing a decision appends a new row with a new timestamp;
the prior row is never mutated or deleted.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import duckdb
import psycopg

from evallab.runner import database_url_from_environment
from evallab.schemas import Verdict

SQL_VERDICTS_PATH = Path("sql/verdicts.sql")
DEFAULT_DISCOVERIES_PATH = Path("digests/DISCOVERIES.md")

ALLOWED_STATUSES: tuple[Literal["accepted", "rejected", "needs_evidence", "pending"], ...] = (
    "accepted",
    "rejected",
    "needs_evidence",
    "pending",
)

AUTOMATED_ACTOR_EXACT: frozenset[str] = frozenset(
    {
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
        "synthetic",
        "ai",
        "llm",
        "pipeline",
        "runner",
        "none",
        "unknown",
        "eval",
        "eval-lab",
        "evallab",
    }
)

AUTOMATED_ACTOR_PREFIXES: tuple[str, ...] = (
    "agent-",
    "bot-",
    "ai-",
    "automated-",
    "harbor-",
    "codex-",
    "runner-",
    "eval-",
    "synth-",
)

_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")


def validate_human_actor(by: str) -> str:
    """Validate that the actor string represents a human name and not an automated agent.

    Refuses empty, whitespace, and known automated actor handles.
    """
    cleaned = by.strip()
    if not cleaned:
        raise ValueError("Actor (--by) is required and cannot be empty")

    lowered = cleaned.lower()
    if lowered in AUTOMATED_ACTOR_EXACT or any(
        lowered.startswith(prefix) for prefix in AUTOMATED_ACTOR_PREFIXES
    ):
        raise ValueError(
            f"Automated actor {by!r} refused: verdicts require human judgment "
            "(e.g. --by 'Peter Makhnatch')"
        )
    return cleaned


def validate_status(status: str) -> Literal["accepted", "rejected", "needs_evidence", "pending"]:
    """Validate that status is one of the four allowed §2.1 verdict literals."""
    for allowed_status in ALLOWED_STATUSES:
        if status == allowed_status:
            return allowed_status
    allowed = ", ".join(repr(s) for s in ALLOWED_STATUSES)
    raise ValueError(f"Invalid status {status!r}; must be one of: {allowed}")

def resolve_discovery_ids(
    repo_root: Path | None = None,
    discoveries_path: Path | None = None,
) -> set[str]:
    """Parse digests/DISCOVERIES.md and return all known discovery IDs.

    Extracts:
      1. Section headers (e.g. '## D-20260815-KTXJSHGZ — draft' or '## 01... — draft')
      2. Any embedded ULIDs in the text
    """
    if discoveries_path is not None:
        target = discoveries_path
    elif repo_root is not None:
        target = repo_root / DEFAULT_DISCOVERIES_PATH
    else:
        target = DEFAULT_DISCOVERIES_PATH

    if not target.is_file():
        return set()

    content = target.read_text(encoding="utf-8")
    ids: set[str] = set()

    for line in content.splitlines():
        if line.startswith("## "):
            header = line.removeprefix("## ").strip()
            for sep in (" — ", " - ", " "):
                if sep in header:
                    header = header.split(sep, 1)[0].strip()
            if header:
                ids.add(header)

    for match in re.findall(r"\b([0-7][0-9A-HJKMNP-TV-Z]{25})\b", content):
        ids.add(match)

    return ids


def validate_discovery_id(
    discovery_id: str,
    repo_root: Path | None = None,
    discoveries_path: Path | None = None,
) -> str:
    """Validate that a discovery_id exists in the discoveries journal."""
    target = discoveries_path or (
        (repo_root / DEFAULT_DISCOVERIES_PATH) if repo_root else DEFAULT_DISCOVERIES_PATH
    )
    known = resolve_discovery_ids(repo_root=repo_root, discoveries_path=discoveries_path)
    if discovery_id not in known:
        raise ValueError(f"Discovery {discovery_id!r} not found in {target}")
    return discovery_id


def execute_verdicts_views(
    conn: duckdb.DuckDBPyConnection,
    sql_path: Path | None = None,
) -> None:
    """Execute sql/verdicts.sql in a DuckDB connection to register views and fallbacks."""
    target_sql = sql_path or SQL_VERDICTS_PATH
    if not target_sql.is_file():
        raise FileNotFoundError(f"SQL file not found: {target_sql}")
    conn.execute(target_sql.read_text(encoding="utf-8"))


def write_verdict_to_catalog(
    verdict: Verdict,
    database_url: str | None = None,
) -> None:
    """Insert a verdict row into PostgreSQL catalog (Z2).

    Idempotently ensures the table exists and appends the new verdict row.
    """
    url = database_url_from_environment(database_url)
    with psycopg.connect(url, connect_timeout=2) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS verdicts (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                discovery_id text NOT NULL,
                status text NOT NULL,
                "by" text NOT NULL,
                "at" timestamptz NOT NULL,
                note text,
                ingested_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        connection.execute(
            """
            INSERT INTO verdicts (discovery_id, status, "by", "at", note)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                verdict.discovery_id,
                verdict.status,
                verdict.by,
                verdict.at,
                verdict.note,
            ),
        )


def list_current_verdicts_from_catalog(
    database_url: str | None = None,
    *,
    status: str | None = None,
) -> list[Verdict]:
    """Query current (latest by timestamp) verdicts from PostgreSQL catalog."""
    url = database_url_from_environment(database_url)
    with psycopg.connect(url, connect_timeout=2) as connection:
        query = """
            SELECT discovery_id, status, "by", "at", note
            FROM v_current_verdicts
        """
        params: list[Any] = []
        if status is not None:
            query += ' WHERE status = %s ORDER BY "at" DESC, discovery_id'
            params.append(status)
        else:
            query += ' ORDER BY "at" DESC, discovery_id'

        rows = connection.execute(query, params).fetchall()
        return [
            Verdict(
                discovery_id=str(row[0]),
                status=cast(
                    Literal["accepted", "rejected", "needs_evidence", "pending"], str(row[1])
                ),
                by=str(row[2]),
                at=row[3] if isinstance(row[3], datetime) else datetime.fromisoformat(str(row[3])),
                note=str(row[4]) if row[4] is not None else None,
            )
            for row in rows
        ]


def get_verdict_history_from_catalog(
    discovery_id: str,
    database_url: str | None = None,
) -> list[Verdict]:
    """Query full verdict history for one discovery from PostgreSQL catalog, oldest first."""
    url = database_url_from_environment(database_url)
    with psycopg.connect(url, connect_timeout=2) as connection:
        query = """
            SELECT discovery_id, status, "by", "at", note
            FROM v_verdicts_history
            WHERE discovery_id = %s
            ORDER BY "at" ASC
        """
        rows = connection.execute(query, (discovery_id,)).fetchall()
        return [
            Verdict(
                discovery_id=str(row[0]),
                status=cast(
                    Literal["accepted", "rejected", "needs_evidence", "pending"], str(row[1])
                ),
                by=str(row[2]),
                at=row[3] if isinstance(row[3], datetime) else datetime.fromisoformat(str(row[3])),
                note=str(row[4]) if row[4] is not None else None,
            )
            for row in rows
        ]


def list_current_verdicts_from_duckdb(
    conn: duckdb.DuckDBPyConnection,
    *,
    status: str | None = None,
) -> list[Verdict]:
    """Query current verdicts from a DuckDB session with views initialized."""
    query = 'SELECT discovery_id, status, "by", "at", note FROM v_current_verdicts'
    params: list[Any] = []
    if status is not None:
        query += ' WHERE status = ? ORDER BY "at" DESC, discovery_id'
        params.append(status)
    else:
        query += ' ORDER BY "at" DESC, discovery_id'
    rows = conn.execute(query, params).fetchall()
    results: list[Verdict] = []
    for row in rows:
        raw_at = row[3]
        at_dt = raw_at if isinstance(raw_at, datetime) else datetime.fromisoformat(str(raw_at))
        if at_dt.tzinfo is None:
            at_dt = at_dt.replace(tzinfo=UTC)
        results.append(
            Verdict(
                discovery_id=str(row[0]),
                status=validate_status(str(row[1])),
                by=str(row[2]),
                at=at_dt,
                note=str(row[4]) if row[4] is not None else None,
            )
        )
    return results

def get_verdict_history_from_duckdb(
    conn: duckdb.DuckDBPyConnection,
    discovery_id: str,
) -> list[Verdict]:
    """Query full history for a discovery from DuckDB session, oldest first."""
    query = """
        SELECT discovery_id, status, "by", "at", note
        FROM v_verdicts_history
        WHERE discovery_id = ?
        ORDER BY "at" ASC
    """
    rows = conn.execute(query, [discovery_id]).fetchall()
    results: list[Verdict] = []
    for row in rows:
        raw_at = row[3]
        at_dt = raw_at if isinstance(raw_at, datetime) else datetime.fromisoformat(str(raw_at))
        if at_dt.tzinfo is None:
            at_dt = at_dt.replace(tzinfo=UTC)
        results.append(
            Verdict(
                discovery_id=str(row[0]),
                status=validate_status(str(row[1])),
                by=str(row[2]),
                at=at_dt,
                note=str(row[4]) if row[4] is not None else None,
            )
        )
    return results

def record_verdict(
    discovery_id: str,
    status: str,
    *,
    by: str,
    note: str | None = None,
    at: datetime | None = None,
    repo_root: Path | None = None,
    discoveries_path: Path | None = None,
    database_url: str | None = None,
    duckdb_conn: duckdb.DuckDBPyConnection | None = None,
) -> Verdict:
    """Validate, construct, and persist a new append-only verdict."""
    valid_actor = validate_human_actor(by)
    valid_status = validate_status(status)
    validate_discovery_id(discovery_id, repo_root=repo_root, discoveries_path=discoveries_path)

    verdict_time = at or datetime.now(UTC)
    verdict = Verdict(
        discovery_id=discovery_id,
        status=valid_status,
        by=valid_actor,
        at=verdict_time,
        note=note.strip() if note else None,
    )

    if duckdb_conn is not None:
        at_str = verdict.at.isoformat()
        duckdb_conn.execute(
            """
            INSERT INTO verdicts (discovery_id, status, "by", "at", note)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                verdict.discovery_id,
                verdict.status,
                verdict.by,
                at_str,
                verdict.note,
            ],
        )
    try:
        write_verdict_to_catalog(verdict, database_url=database_url)
    except Exception:
        # In offline/CI mode with no live PostgreSQL, database write may fail.
        # If no DuckDB connection was given and catalog failed, check if database_url was explicit.
        if database_url is not None:
            raise

    return verdict


def format_verdicts_table(verdicts: Sequence[Verdict]) -> str:
    """Render a clean CLI table of current verdicts."""
    if not verdicts:
        return "No verdicts recorded."

    id_w = max(len("DISCOVERY ID"), max((len(v.discovery_id) for v in verdicts), default=0))
    st_w = max(len("STATUS"), max((len(v.status) for v in verdicts), default=0))
    by_w = max(len("BY"), max((len(v.by) for v in verdicts), default=0))
    at_w = 20

    header = (
        f"{'DISCOVERY ID':<{id_w}}  {'STATUS':<{st_w}}  {'BY':<{by_w}}  "
        f"{'AT':<{at_w}}  NOTE"
    )
    divider = f"{'-' * id_w}  {'-' * st_w}  {'-' * by_w}  {'-' * at_w}  {'-' * 4}"

    lines = [header, divider]
    for v in verdicts:
        at_str = v.at.strftime("%Y-%m-%dT%H:%M:%SZ") if v.at.tzinfo else v.at.isoformat()
        note_str = v.note or ""
        lines.append(
            f"{v.discovery_id:<{id_w}}  {v.status:<{st_w}}  {v.by:<{by_w}}  "
            f"{at_str:<{at_w}}  {note_str}"
        )
    return "\n".join(lines)


def format_verdict_history_table(discovery_id: str, history: Sequence[Verdict]) -> str:
    """Render a clean CLI table of verdict history for one discovery."""
    if not history:
        return f"No verdict history for {discovery_id}."

    st_w = max(len("STATUS"), max((len(v.status) for v in history), default=0))
    by_w = max(len("BY"), max((len(v.by) for v in history), default=0))
    at_w = 20

    header = (
        f"History for {discovery_id}:\n"
        f"{'STATUS':<{st_w}}  {'BY':<{by_w}}  {'AT':<{at_w}}  NOTE"
    )
    divider = f"{'-' * st_w}  {'-' * by_w}  {'-' * at_w}  {'-' * 4}"

    lines = [header, divider]
    for v in history:
        at_str = v.at.strftime("%Y-%m-%dT%H:%M:%SZ") if v.at.tzinfo else v.at.isoformat()
        note_str = v.note or ""
        lines.append(f"{v.status:<{st_w}}  {v.by:<{by_w}}  {at_str:<{at_w}}  {note_str}")
    return "\n".join(lines)
