"""Normalize recorded provider billing rows and read stored usage reports."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


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


