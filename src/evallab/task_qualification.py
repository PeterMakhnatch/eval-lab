"""Stored qualification table names, trial package resolution, and cost estimates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def task_dir_for_trial(trial_dir: Path) -> Path | None:
    """The task directory a trial ran, even after the queue removed its stage.

    Harbor's lock points at the executor's staged copy
    (``<jobs>/.exec-stage/<job>``), which the queue deletes once the run
    finishes. The job's ``experiment-spec.json`` keeps the source task path
    (checkout-relative, e.g. ``derived/task-store/...``); it is resolved
    against the checkout that owns the jobs directory.
    """
    lock = _read_json(trial_dir / "lock.json")
    task = lock.get("task")
    staged = task.get("path") if isinstance(task, dict) else None
    if isinstance(staged, str) and staged and (Path(staged) / "task.toml").is_file():
        return Path(staged)
    job_dir = trial_dir.parent
    source = _read_json(job_dir / "experiment-spec.json").get("task")
    if not isinstance(source, str) or not source:
        return None
    path = Path(source)
    candidates = [path] if path.is_absolute() else [job_dir.parent.parent / path, job_dir.parent / path]
    return next((c for c in candidates if (c / "task.toml").is_file()), None)
