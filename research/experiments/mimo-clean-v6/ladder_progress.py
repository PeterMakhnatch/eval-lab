"""Ladder progress table for the ranked mimo-clean-v6 rows (HAR-286).

Joins the v6 unverified ranking (ranks 1-100) with every committed
``ladder-*.json`` cell file of schema ``evallab.mimo-v6-ladder-cells/v1`` and
writes ``ladder-progress.csv``: one row per rank, with the cell's verdict and
the ``basis`` that verdict rests on, read from the cell's ``verifier_decision``:

* ``tamper-refusal`` -- the verifier refused the agent diff on a tamper
  signature, so the ``clean`` verdict is a refusal, not a graded run;
* ``graded`` -- the verifier ran the task's own runner (``rc=...``);
* ``unscored`` -- neither (no decision recorded);
* ``none`` -- no committed cell covers the rank (verdict ``none``).

Pure stdlib; no Docker. Regenerate with ``python ladder_progress.py``; verify
the committed table with ``--check`` (exit 1 on drift).
"""

from __future__ import annotations

import argparse
import csv
import difflib
import io
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent

RANKING = "research/experiments/mimo-clean-v5-coverage/unverified-ranking.csv"
CELLS_GLOB = "ladder-*.json"
CELLS_SCHEMA = "evallab.mimo-v6-ladder-cells/v1"
OUTPUT_NAME = "ladder-progress.csv"
FIRST_RANK = 1
LAST_RANK = 100

COLUMNS = (
    "rank",
    "task_id",
    "phase",
    "language",
    "ladder_version",
    "verdict",
    "basis",
    "wall_seconds",
    "source_file",
)

TAMPER_SIGNATURE = "tamper signature"
GRADED_MARKER = "rc="


class ProgressError(RuntimeError):
    """An input no longer matches the contract this builder reads."""


def classify_basis(verifier_decision: object) -> str:
    """Name what a cell's verdict rests on, from its ``verifier_decision``."""
    decision = str(verifier_decision or "")
    if TAMPER_SIGNATURE in decision:
        return "tamper-refusal"
    if GRADED_MARKER in decision:
        return "graded"
    return "unscored"


def read_ranking(root: Path = ROOT) -> list[dict[str, str]]:
    path = root / RANKING
    if not path.is_file():
        raise ProgressError(f"ranking is missing: {path}")
    with path.open(encoding="utf-8", newline="") as handle:
        rows = [
            row for row in csv.DictReader(handle) if FIRST_RANK <= int(row["rank"]) <= LAST_RANK
        ]
    ranks = [int(row["rank"]) for row in rows]
    if ranks != list(range(FIRST_RANK, LAST_RANK + 1)):
        raise ProgressError(f"{RANKING} does not hold ranks {FIRST_RANK}-{LAST_RANK} once each")
    return rows


def read_cells(cells_dir: Path, root: Path = ROOT) -> list[dict[str, Any]]:
    """Every cell of every ``ladder-*.json`` with the v6 cell schema, with its file."""
    cells: list[dict[str, Any]] = []
    for path in sorted(cells_dir.glob(CELLS_GLOB)):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != CELLS_SCHEMA:
            continue
        source = path.relative_to(root).as_posix()
        for task in data.get("tasks", []):
            cells.append({**task, "_source_file": source})
    return cells


def _newest_cell(cells: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """A re-run supersedes an earlier cell on the same rank: latest finish wins."""
    return max(cells, key=lambda cell: (str(cell.get("finished_utc") or ""), cell["_source_file"]))


def assemble(
    ranking: Sequence[dict[str, str]], cells: Sequence[dict[str, Any]]
) -> list[dict[str, str]]:
    """One row per ranked task; rows without a cell read ``verdict=none``."""
    by_rank: dict[int, list[dict[str, Any]]] = {}
    for cell in cells:
        by_rank.setdefault(int(cell["ranking"]["rank"]), []).append(cell)
    known = {int(row["rank"]) for row in ranking}
    stray = sorted(rank for rank in by_rank if rank not in known and rank <= LAST_RANK)
    if stray:
        raise ProgressError(f"cells name ranks missing from the ranking: {stray}")
    rows: list[dict[str, str]] = []
    for ranked in ranking:
        rank = int(ranked["rank"])
        row = {
            "rank": str(rank),
            "task_id": ranked["task_id"],
            "phase": ranked["phase"],
            "language": ranked["language"],
            "ladder_version": "",
            "verdict": "none",
            "basis": "none",
            "wall_seconds": "",
            "source_file": "",
        }
        if rank in by_rank:
            cell = _newest_cell(by_rank[rank])
            if cell["task_id"] != ranked["task_id"]:
                raise ProgressError(
                    f"rank {rank}: cell is for {cell['task_id']}, ranking says {ranked['task_id']}"
                )
            row.update(
                ladder_version=str(cell["ladder_version"]),
                verdict=str(cell["verdict"]),
                basis=classify_basis(cell.get("verifier_decision")),
                wall_seconds=str(cell["wall_seconds"]),
                source_file=cell["_source_file"],
            )
        rows.append(row)
    return rows


def build(root: Path = ROOT, *, cells_dir: Path) -> list[dict[str, str]]:
    return assemble(read_ranking(root), read_cells(cells_dir, root))


def render(rows: Sequence[dict[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def basis_counts(rows: Sequence[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["basis"]] = counts.get(row["basis"], 0) + 1
    return counts


def check(rebuilt: str, target: Path) -> bool:
    """Compare ``rebuilt`` against the committed file; True when it matches."""
    committed = target.read_text(encoding="utf-8") if target.is_file() else ""
    if committed == rebuilt:
        print(f"{OUTPUT_NAME}: reproduces from committed cells")
        return True
    print(f"{OUTPUT_NAME}: stale; rerun without --check to regenerate", file=sys.stderr)
    diff = difflib.unified_diff(
        committed.splitlines(),
        rebuilt.splitlines(),
        fromfile=f"{OUTPUT_NAME} (committed)",
        tofile=f"{OUTPUT_NAME} (rebuilt)",
        lineterm="",
    )
    for line in list(diff)[:40]:
        print(line, file=sys.stderr)
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help=f"exit 1 if the committed {OUTPUT_NAME} differs from a rebuild",
    )
    args = parser.parse_args(argv)
    rows = build(ROOT, cells_dir=HERE)
    if args.check:
        return 0 if check(render(rows), HERE / OUTPUT_NAME) else 1
    (HERE / OUTPUT_NAME).write_text(render(rows), encoding="utf-8")
    counts = ", ".join(f"{name}={count}" for name, count in sorted(basis_counts(rows).items()))
    print(f"wrote {OUTPUT_NAME}: {len(rows)} rows ({counts})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
