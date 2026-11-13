"""Stored qualification table names and resource cost estimates."""

from __future__ import annotations

from pathlib import Path

TABLE_FILENAME = "task_qualification.parquet"


TABLE = Path(TABLE_FILENAME).stem


DAYTONA_RATE_CARD = {
    "backend": "daytona",
    "vcpu_usd_per_hour": 0.0504,
    "memory_usd_per_gib_hour": 0.0162,
    "storage_usd_per_gib_hour": 0.000108,
    "storage_free_gib": 5.0,
    "source": "https://www.daytona.io/pricing",
    "retrieved": "2026-09-28",
}


RATE_CARDS = {"daytona": DAYTONA_RATE_CARD}


def estimate_cost_usd(
    *,
    backend: str | None,
    sandbox_seconds: float | None,
    cpus: int | None,
    memory_mb: int | None,
    storage_mb: int | None,
    rate_backend: str = "daytona",
) -> float | None:
    """Sandbox cost under the active rate card, else null.

    Only the rate-card backend is priced (today: Daytona list prices, see
    ``DAYTONA_RATE_CARD``). Missing cpus/memory count as 0, and missing
    storage counts as 0 billable GiB: the lab never invents a disk size to
    make a trial look more expensive.
    """
    rates = RATE_CARDS.get(rate_backend)
    if rates is None or backend != rate_backend:
        return None
    if sandbox_seconds is None:
        return None
    vcpu = float(cpus or 0)
    mem_gib = float(memory_mb or 0) / 1024.0
    storage_gib = float(storage_mb or 0) / 1024.0
    billable_storage_gib = max(0.0, storage_gib - float(rates["storage_free_gib"]))
    hourly = (
        vcpu * float(rates["vcpu_usd_per_hour"])
        + mem_gib * float(rates["memory_usd_per_gib_hour"])
        + billable_storage_gib * float(rates["storage_usd_per_gib_hour"])
    )
    return sandbox_seconds / 3600.0 * hourly


