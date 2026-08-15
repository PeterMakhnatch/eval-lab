"""Dashboard adapter for the single operator status projection."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from evallab.status import StatusSnapshot, build_status_snapshot, snapshot_as_dict

BooleanProbe = Callable[[], bool]


def load_operator_snapshot(
    root: Path,
    *,
    postgres_probe: BooleanProbe | None = None,
    phoenix_probe: BooleanProbe | None = None,
    postgres_url: str | None = None,
    generated_at: datetime | None = None,
) -> StatusSnapshot:
    """Same object `evallab status` builds. Dashboard must not invent a second meaning."""

    return build_status_snapshot(
        root,
        postgres_probe=postgres_probe,
        phoenix_probe=phoenix_probe,
        postgres_url=postgres_url,
        generated_at=generated_at,
    )


def dashboard_view(snapshot: StatusSnapshot) -> dict[str, Any]:
    return snapshot_as_dict(snapshot)
