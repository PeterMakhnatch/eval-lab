"""Modal billing reconcile: billed GPU-seconds versus lab-computed cost.

``modal billing report`` (read-only) is the billed side. The lab side sums
catalog ``trials.cost_usd`` for self-hosted MiMo trials per UTC day. Ingested
self-hosted trials record the native model id
(``XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B``), not the ``selfhosted/`` queue
selector, so both spellings match. Missing data is ``None`` with a reason,
never 0: the self-hosted route bills by server time, so per-trial cost is
routinely absent from the catalog.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import psycopg

from evallab.modal_ops import SELFHOSTED_CATALOG_MODELS, daytona_sandbox_counts

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
    PRIMARY KEY (object_id, interval_start, resource)
)
""".strip()


@dataclass(frozen=True)
class BillingRow:
    object_id: str
    description: str
    environment: str
    interval_start: datetime
    resource: str
    cost_usd: float


@dataclass(frozen=True)
class DayComparison:
    day: date
    modal_billed_usd: float | None
    modal_reason: str | None
    lab_selfhosted_usd: float | None
    lab_reason: str | None
    lab_trials: int
    lab_trials_without_cost: int


def normalize_report_rows(payload: Any, *, resolution: str) -> list[BillingRow]:
    """Validate one ``modal billing report --json`` payload."""
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
        rows.append(
            BillingRow(
                object_id=str(entry["object_id"]),
                description=str(entry["description"]),
                environment=str(entry["environment"]),
                interval_start=interval,
                resource=str(resource) if resource is not None else "",
                cost_usd=cost,
            )
        )
    return rows


def aggregate_daily(rows: list[BillingRow]) -> dict[date, float]:
    """Sum billed cost per UTC day."""
    totals: dict[date, float] = {}
    for row in rows:
        day = row.interval_start.date()
        totals[day] = totals.get(day, 0.0) + row.cost_usd
    return totals


def store_billing_rows(database_url: str, rows: list[BillingRow], *, resolution: str) -> int:
    """Upsert fetched rows into the catalog. Returns the row count.

    When hourly rows are stored (``resolution="h"``), older day-level rows
    (``resolution="d"``) covering the same UTC dates are removed first so
    queries over hourly intervals never double-count against daily rows.
    """
    with psycopg.connect(database_url) as connection:
        connection.execute(BILLING_TABLE_DDL)
        if resolution == "h" and rows:
            days = {row.interval_start.date() for row in rows}
            for target_day in days:
                connection.execute(
                    """
                    DELETE FROM modal_billing_rows
                    WHERE resolution = 'd'
                      AND (interval_start AT TIME ZONE 'UTC')::date = %s
                    """,
                    (target_day,),
                )
        for row in rows:
            connection.execute(
                """
                INSERT INTO modal_billing_rows
                    (object_id, description, environment, interval_start,
                     resource, cost_usd, resolution)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (object_id, interval_start, resource)
                DO UPDATE SET cost_usd = EXCLUDED.cost_usd,
                              resolution = EXCLUDED.resolution,
                              reported_at = now()
                """,
                (
                    row.object_id,
                    row.description,
                    row.environment,
                    row.interval_start,
                    row.resource,
                    row.cost_usd,
                    resolution,
                ),
            )
    return len(rows)


def refresh_modal_billing(
    database_url: str,
    *,
    start: date,
    end: date,
    repo_root: Any | None = None,
    runner: Any | None = None,
    resolution: str = "h",
) -> int:
    """Fetch Modal billing rows (read-only) and upsert them into the catalog.

    Reuses ``modal_default_runner``, ``normalize_report_rows``, and
    ``store_billing_rows``. Returns the count of stored rows.
    Raises ``RuntimeError`` if the runner fails or returns unreadable output.
    """
    import json
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
    rows = normalize_report_rows(payload, resolution=resolution)
    return store_billing_rows(database_url, rows, resolution=resolution)


def lab_selfhosted_daily(database_url: str, day: date) -> tuple[float | None, int, int, str | None]:
    """Lab-computed self-hosted cost for one UTC day.

    Returns ``(total_or_None, trial_count, trials_without_cost, reason)``.
    """
    native = SELFHOSTED_CATALOG_MODELS[0]
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            """
            SELECT coalesce(sum(cost_usd), 0), count(cost_usd), count(*)
            FROM trials
            WHERE (model_name = %s OR model_name LIKE 'selfhosted/%%')
              AND (started_at::timestamptz AT TIME ZONE 'UTC')::date = %s
            """,
            (native, day.isoformat()),
        ).fetchone()
    if row is None:
        return None, 0, 0, "catalog query returned no row"
    total, valued, count = row
    count = int(count)
    valued = int(valued)
    if count == 0:
        return None, 0, 0, "no self-hosted trials in catalog"
    if valued == 0:
        return (
            None,
            count,
            count,
            "self-hosted trials carry no per-trial cost (time-billed)",
        )
    reason = None if valued == count else f"partial: {count - valued} of {count} trials lack cost"
    return float(total), count, count - valued, reason


def compare_days(
    days: list[date],
    billed: dict[date, float],
    lab: dict[date, tuple[float | None, int, int, str | None]],
) -> list[DayComparison]:
    """Join the two sides. Absent Modal rows are None with a reason."""
    comparisons: list[DayComparison] = []
    for day in days:
        total, count, without_cost, reason = lab[day]
        if day in billed:
            modal_total, modal_reason = billed[day], None
        else:
            modal_total, modal_reason = None, "no billing rows reported"
        comparisons.append(
            DayComparison(
                day=day,
                modal_billed_usd=modal_total,
                modal_reason=modal_reason,
                lab_selfhosted_usd=total,
                lab_reason=reason,
                lab_trials=count,
                lab_trials_without_cost=without_cost,
            )
        )
    return comparisons


def _money(value: float | None) -> str:
    return f"${value:.2f}" if value is not None else "n/a"


def render_comparisons(comparisons: list[DayComparison]) -> str:
    """Human-readable per-day table for the CLI receipt."""
    lines = ["date | modal billed | lab self-hosted | trials | note"]
    for item in comparisons:
        note = item.modal_reason or item.lab_reason or ""
        lines.append(
            f"{item.day.isoformat()} | {_money(item.modal_billed_usd)} | "
            f"{_money(item.lab_selfhosted_usd)} | {item.lab_trials} | {note}"
        )
    return "\n".join(lines)


def sandbox_receipt_line() -> str:
    """Read-only Daytona census for reconcile/teardown receipts."""
    counts = daytona_sandbox_counts()
    if counts.total is None:
        return f"daytona sandboxes: n/a ({counts.reason})"
    if counts.harbor_managed is None:
        return f"daytona sandboxes: {counts.total} total"
    return f"daytona sandboxes: {counts.harbor_managed} harbor-managed / {counts.total} total"
