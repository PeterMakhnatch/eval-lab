#!/usr/bin/env python3
"""HAR-113 part 3: Daytona nops for census tasks still labelled ``unknown``.

One nop spec per ``unknown`` row of HAR-108's ``task_health.parquet``,
lightest image first (the pool's ``image_mib``), named ``har113-nop-<id>``.
``runner.py`` runs them in waves until the card's spend cap; the census is
then rebuilt with ``health-collect`` over HAR-105's, HAR-108's and these jobs.

Usage (from the worktree root):
    uv run python research/experiments/har113-variants/unknown_specs.py
Prints the spec paths in run order (one per line) and writes them to
``specs/unknown/``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pyarrow.parquet as pq
from common import CENSUS, HERE

sys.path.insert(0, str(HERE.parent / "har105-exploration"))
from nopspec import nop_spec  # noqa: E402

SPECS = HERE / "specs/unknown"


def write_specs(card: str = "har113", specs: Path = SPECS) -> list[Path]:
    """One ``<card>-nop-<id>`` spec per ``unknown`` census row, lightest first."""
    pool = {e["task_id"]: e for e in json.loads((CENSUS / "pool.json").read_text())["pool"]}
    rows = pq.read_table(CENSUS / "task_health.parquet").to_pylist()
    unknown = sorted(
        (r for r in rows if r["label"] == "unknown"),
        key=lambda r: (r["image_mib"] or 10**9, r["task_id"]),
    )
    specs.mkdir(parents=True, exist_ok=True)
    label = card.upper().replace("HAR", "HAR-")
    paths = []
    for row in unknown:
        task_id = row["task_id"]
        name = f"{card}-nop-{task_id.removeprefix('format-code-task-')}"
        spec = nop_spec(pool[task_id]["task"], name, f"{label} census nop of {task_id}")
        spec["submitted_by"] = f"{card}-variants"
        path = specs / f"{name}.json"
        path.write_text(json.dumps(spec, indent=1) + "\n")
        paths.append(path)
    return paths


def main() -> None:
    for path in write_specs():
        print(path)


if __name__ == "__main__":
    main()
