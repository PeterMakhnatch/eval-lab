"""Verdict persistence, validation, and query engine (§2.1, §2.2).

Verdicts record human dispositions on discovery journal findings
(accepted, rejected, needs_evidence, pending). A verdict is strictly
append-only: changing a decision appends a new row with a new timestamp;
the prior row is never mutated or deleted.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import psycopg

from evallab.runner import database_url_from_environment
from evallab.schemas import Verdict
from evallab.storage.attach import postgres_identity

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


def list_current_verdicts_from_catalog(
    database_url: str | None = None,
    *,
    status: str | None = None,
) -> list[Verdict]:
    """Query current (latest by timestamp) verdicts from PostgreSQL catalog."""
    url = database_url_from_environment(database_url)
    target_name = postgres_identity(url)
    try:
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
            results: list[Verdict] = []
            for row in rows:
                try:
                    raw_at = row[3]
                    at_dt = (
                        raw_at
                        if isinstance(raw_at, datetime)
                        else datetime.fromisoformat(str(raw_at))
                    )
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
                except Exception:
                    continue
            return results
    except Exception as exc:
        msg = (
            f"catalog read failed [unavailable] "
            f"(examined postgresql://{target_name} relation v_current_verdicts): {exc}"
        )
        print(f"error: {msg}", file=sys.stderr)
        raise SystemExit(2) from exc


