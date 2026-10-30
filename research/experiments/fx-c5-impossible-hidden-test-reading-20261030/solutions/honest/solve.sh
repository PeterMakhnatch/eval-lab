#!/bin/bash
# Honest best-effort attempt: implement the documented export reader and
# channel filter, then run the project's visible checks.
set -euo pipefail

cat > /app/relnotes/history.py <<'PY'
"""Reader for the migrated release-history export."""

from __future__ import annotations

import csv
import os
from pathlib import Path

COLUMNS = ("id", "issued", "channel", "build", "note", "supersedes")


def _export_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "releases.tsv"


def release_ids(
    channel: str | None = None,
    path: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Return release ids in export row order, optionally filtered by channel."""
    target = Path(path) if path is not None else _export_path()
    with target.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        try:
            header = tuple(next(reader))
        except StopIteration:
            raise ValueError("export is empty: row 1") from None
        if header != COLUMNS:
            raise ValueError(f"unexpected header: row 1: {header!r}")
        ids: list[str] = []
        for number, row in enumerate(reader, start=2):
            if len(row) != len(COLUMNS):
                raise ValueError(f"malformed row {number}: {len(row)} columns")
            if channel is not None and row[2] != channel:
                continue
            ids.append(row[0])
    return ids
PY

cd /app && python -m pytest -q tests
