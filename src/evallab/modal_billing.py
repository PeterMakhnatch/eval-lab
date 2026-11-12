"""Modal billing reconcile: billed GPU-seconds versus lab-computed cost.

``modal billing report`` (read-only) is the billed side. The lab side sums
catalog ``trials.cost_usd`` for self-hosted MiMo trials per UTC day. Ingested
self-hosted trials record a native model id (the base
``XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`` or an admitted ``...:<adapter>``),
not the ``selfhosted/`` queue selector, so both spellings match. Missing data is ``None`` with a reason,
never 0: the self-hosted route bills by server time, so per-trial cost is
routinely absent from the catalog.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


#: Catalog table holding fetched Modal billing rows. Also applied through
#: ``sql/schema.sql``; created here too so direct users get the same shape.
BILLING_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS modal_billing_rows (
    object_id text NOT NULL,
    description text NOT NULL,
    environment text NOT NULL,
    interval_start timestamptz NOT NULL,
    resource text NOT NULL DEFAULT '',
    cost_usd double precision NOT NULL,
    resolution text NOT NULL,
    reported_at timestamptz NOT NULL DEFAULT now(),
    tags jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (object_id, interval_start, resource)
);
ALTER TABLE modal_billing_rows
    ADD COLUMN IF NOT EXISTS tags jsonb NOT NULL DEFAULT '{}'::jsonb
""".strip()


@dataclass(frozen=True)
class BillingRow:
    object_id: str
    description: str
    environment: str
    interval_start: datetime
    resource: str
    cost_usd: float
    tags: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.tags, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.tags.items()
        ):
            raise ValueError("billing tags must be a string-to-string object")




def normalize_report_rows(payload: Any, *, resolution: str) -> list[BillingRow]:
    """Validate provider rows, including SDK 1.6.1's JSON-encoded tag objects.

    Reports without ``tags`` remain valid and normalize to an empty mapping.
    No tag keys or values are coerced, including in decoded CLI tag strings.
    """
    if not isinstance(payload, list):
        raise ValueError("billing report payload must be a JSON array")
    rows: list[BillingRow] = []
    for index, entry in enumerate(payload):
        if not isinstance(entry, dict):
            raise ValueError(f"billing row {index} is not an object")
        try:
            interval = datetime.fromisoformat(str(entry["interval_start"]))
            cost = float(str(entry["cost"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"billing row {index} has no usable interval/cost") from exc
        for key in ("object_id", "description", "environment"):
            if not isinstance(entry.get(key), str):
                raise ValueError(f"billing row {index} has no {key}")
        resource = entry.get("resource")
        tags = entry.get("tags", {})
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except json.JSONDecodeError as exc:
                raise ValueError(f"billing row {index} has malformed tags") from exc
        rows.append(
            BillingRow(
                object_id=str(entry["object_id"]),
                description=str(entry["description"]),
                environment=str(entry["environment"]),
                interval_start=interval,
                resource=str(resource) if resource is not None else "",
                cost_usd=cost,
                tags=tags,
            )
        )
    return rows




def store_billing_rows(database_url: str, rows: list[BillingRow], *, resolution: str) -> int:
    """Upsert fetched rows into the catalog. Returns the row count.

    Pure upsert: inserts new rows and updates existing rows on conflict,
    never deleting existing rows so older partial fetches cannot erase newer data.
    Tags merge by key: reported values replace known values, while absent keys
    (including all keys in legacy untagged reports) retain their stored labels.
    """
    with psycopg.connect(database_url) as connection:
        connection.execute(BILLING_TABLE_DDL)
        for row in rows:
            connection.execute(
                """
                INSERT INTO modal_billing_rows
                    (object_id, description, environment, interval_start,
                     resource, cost_usd, resolution, tags)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (object_id, interval_start, resource)
                DO UPDATE SET cost_usd = EXCLUDED.cost_usd,
                              resolution = EXCLUDED.resolution,
                              reported_at = now(),
                              tags = modal_billing_rows.tags || EXCLUDED.tags
                """,
                (
                    row.object_id,
                    row.description,
                    row.environment,
                    row.interval_start,
                    row.resource,
                    row.cost_usd,
                    resolution,
                    Jsonb(row.tags),
                ),
            )
    return len(rows)


def fetch_modal_billing_report(
    *,
    start: date,
    end: date,
    repo_root: Any | None = None,
    runner: Any | None = None,
    resolution: str = "h",
) -> list[BillingRow]:
    """Fetch Modal billing rows (read-only) via Modal CLI report.

    Returns the normalized list of :class:`BillingRow` objects in memory.
    Raises ``RuntimeError`` if the runner fails or returns unreadable output.
    """
    from pathlib import Path

    from evallab.modal_ops import default_runner

    run = runner or default_runner(Path(repo_root) if repo_root else Path.cwd())
    completed = run(
        [
            "billing",
            "report",
            "--start",
            start.isoformat(),
            "--end",
            end.isoformat(),
            "--resolution",
            resolution,
            "--tag-names",
            "*",
            "--json",
        ]
    )
    if completed.returncode != 0:
        err = getattr(completed, "stderr", "") or f"exit code {completed.returncode}"
        raise RuntimeError(f"modal billing report failed: {err[-500:]}")
    try:
        payload = json.loads(completed.stdout)
    except Exception as exc:
        raise RuntimeError(f"modal billing report unreadable: {exc}") from exc
    return normalize_report_rows(payload, resolution=resolution)












