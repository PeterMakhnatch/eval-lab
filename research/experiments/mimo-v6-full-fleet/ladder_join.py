"""Join the ranked v6 ladder cells onto the 567-task fleet census (HAR-372).

``fleet-ladder-join.csv`` answers one question per fleet task: does a ranked
cheat-ladder cell exist, and did that cell run the fleet's own package bytes?

Inputs, all committed:

- ``mimo-v6-fleet-census.csv``: the fleet release's 567 current packages. Its
  ``v6_package_digest`` is the fleet digest; this builder refuses a census row
  whose digest disagrees with ``manifest.csv``'s ``final_digest``.
- ``../mimo-clean-v6/ladder-progress.csv``: the 100 ranked ladder cells. It
  names each cell's source ledger in ``source_file`` but records no digest, so
  the digest is read from that ledger's task entry (``final_digest``) and must
  equal ``../mimo-clean-v6/manifest.csv``'s ``final_digest`` for the task, which
  HAR-312 pins equal.

A manifest that repeats a task is refused, because a dict built from its rows
would let the later row win over the earlier one without a word.

``digest_match`` is ``yes`` when the cell ran the fleet digest, ``no`` when it
ran different bytes, and ``no-cell`` when the task has no ranked cell at all.
A cell is a measurement of whatever bytes it ran; nothing here promotes a cell
verdict into fleet acceptance, and the census buckets are emitted unchanged.

Reproduce (repo root, locked environment):

    uv run python research/experiments/mimo-v6-full-fleet/ladder_join.py --check
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

FLEET_DIR = "research/experiments/mimo-v6-full-fleet"
CLEAN_DIR = "research/experiments/mimo-clean-v6"
CENSUS = f"{FLEET_DIR}/mimo-v6-fleet-census.csv"
FLEET_MANIFEST = f"{FLEET_DIR}/manifest.csv"
PROGRESS = f"{CLEAN_DIR}/ladder-progress.csv"
CLEAN_MANIFEST = f"{CLEAN_DIR}/manifest.csv"
JOIN_NAME = "fleet-ladder-join.csv"

COLUMNS = (
    "task_id",
    "language",
    "bucket",
    "fleet_digest",
    "ladder_rank",
    "ladder_verdict",
    "ladder_basis",
    "ladder_digest",
    "digest_match",
)

#: ``digest_match`` values; a task without a cell is not a stale measurement.
MATCH_YES = "yes"
MATCH_NO = "no"
MATCH_NONE = "no-cell"


class JoinError(RuntimeError):
    """An input no longer matches the contract this builder reads."""


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise JoinError(f"input is missing: {path}")
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise JoinError(f"input is empty: {path}")
    return rows


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise JoinError(f"cell ledger is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest(path: Path) -> dict[str, dict[str, str]]:
    """Index manifest rows by task; a repeat would silently win over an earlier row."""
    index: dict[str, dict[str, str]] = {}
    for row in _read_csv(path):
        task = row["task_id"]
        if task in index:
            raise JoinError(f"{path} repeats {task}")
        index[task] = row
    return index


def fleet_rows(root: Path = ROOT) -> list[dict[str, str]]:
    """The census rows, with each fleet digest checked against the manifest."""
    census = _read_csv(root / CENSUS)
    manifest = _manifest(root / FLEET_MANIFEST)
    if len({row["task_id"] for row in census}) != len(census):
        raise JoinError(f"{CENSUS} repeats a task_id")
    for row in census:
        task = row["task_id"]
        described = manifest.get(task)
        if described is None:
            raise JoinError(f"{task} is in the census but not in {FLEET_MANIFEST}")
        if row["v6_package_digest"] != described["final_digest"]:
            raise JoinError(
                f"{task} census digest {row['v6_package_digest']} "
                f"disagrees with manifest {described['final_digest']}"
            )
    return [
        {
            "task_id": row["task_id"],
            "language": row["language"],
            "bucket": row["bucket"],
            "fleet_digest": row["v6_package_digest"],
        }
        for row in census
    ]


def ladder_cells(root: Path = ROOT) -> dict[str, dict[str, str]]:
    """Every ranked cell, keyed by task, with the digest it actually ran."""
    progress = _read_csv(root / PROGRESS)
    manifest = _manifest(root / CLEAN_MANIFEST)
    if len({row["task_id"] for row in progress}) != len(progress):
        raise JoinError(f"{PROGRESS} ranks a task twice")
    ranks = [int(row["rank"]) for row in progress]
    if len(set(ranks)) != len(ranks):
        raise JoinError(f"{PROGRESS} repeats a rank")
    cells: dict[str, dict[str, str]] = {}
    for row in progress:
        task = row["task_id"]
        ledger = _read_json(root / row["source_file"])
        if ledger.get("manifest") != CLEAN_MANIFEST:
            raise JoinError(
                f"{row['source_file']} is bound to {ledger.get('manifest')!r}, not {CLEAN_MANIFEST}"
            )
        entries = [entry for entry in ledger.get("tasks", []) if entry.get("task_id") == task]
        if len(entries) != 1:
            raise JoinError(f"{row['source_file']} has {len(entries)} entries for {task}")
        cell = entries[0]
        digest = str(cell.get("final_digest") or "")
        if not digest:
            raise JoinError(f"{task} cell in {row['source_file']} records no digest")
        if cell.get("verdict") != row["verdict"]:
            raise JoinError(
                f"{task} ledger verdict {cell.get('verdict')!r} "
                f"disagrees with {PROGRESS} {row['verdict']!r}"
            )
        expected = manifest.get(task)
        if expected is None:
            raise JoinError(f"{task} is ranked but absent from {CLEAN_MANIFEST}")
        if digest != expected["final_digest"]:
            raise JoinError(
                f"{task} cell digest {digest} disagrees with "
                f"{CLEAN_MANIFEST} {expected['final_digest']}"
            )
        cells[task] = {
            "ladder_rank": row["rank"],
            "ladder_verdict": row["verdict"],
            "ladder_basis": row["basis"],
            "ladder_digest": digest,
        }
    return cells


def assemble(
    fleet: Sequence[dict[str, str]], cells: dict[str, dict[str, str]]
) -> list[dict[str, str]]:
    """One row per fleet task, in task-id order, with the cell when one exists."""
    rows: list[dict[str, str]] = []
    for task in fleet:
        cell = cells.get(task["task_id"])
        if cell is None:
            rank = verdict = basis = digest = ""
            match = MATCH_NONE
        else:
            rank = cell["ladder_rank"]
            verdict = cell["ladder_verdict"]
            basis = cell["ladder_basis"]
            digest = cell["ladder_digest"]
            match = MATCH_YES if digest == task["fleet_digest"] else MATCH_NO
        rows.append(
            {
                "task_id": task["task_id"],
                "language": task["language"],
                "bucket": task["bucket"],
                "fleet_digest": task["fleet_digest"],
                "ladder_rank": rank,
                "ladder_verdict": verdict,
                "ladder_basis": basis,
                "ladder_digest": digest,
                "digest_match": match,
            }
        )
    return rows


def bucket_counts(rows: Sequence[dict[str, str]]) -> list[tuple[str, int, int, int]]:
    """(bucket, fleet tasks, tasks with a cell, cells at the fleet digest), in order."""
    buckets = sorted({row["bucket"] for row in rows})
    counts: list[tuple[str, int, int, int]] = []
    for bucket in [*buckets, "all"]:
        selected = rows if bucket == "all" else [row for row in rows if row["bucket"] == bucket]
        counts.append(
            (
                bucket,
                len(selected),
                sum(1 for row in selected if row["ladder_rank"]),
                sum(1 for row in selected if row["digest_match"] == MATCH_YES),
            )
        )
    return counts


def build(root: Path = ROOT) -> list[dict[str, str]]:
    return assemble(fleet_rows(root), ladder_cells(root))


def render(rows: Sequence[dict[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(COLUMNS), extrasaction="ignore", lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _check(name: str, rebuilt: str) -> bool:
    """Compare ``rebuilt`` against the committed file's bytes; True when equal."""
    target = HERE / name
    committed = target.read_bytes() if target.is_file() else b""
    if committed == rebuilt.encode("utf-8"):
        print(f"{name}: reproduces from committed evidence")
        return True
    print(f"{name}: stale; rerun without --check to regenerate", file=sys.stderr)
    diff = difflib.unified_diff(
        committed.decode("utf-8", "replace").splitlines(),
        rebuilt.splitlines(),
        fromfile=f"{name} (committed)",
        tofile=f"{name} (rebuilt)",
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
        help=f"exit 1 if the committed {JOIN_NAME} differs",
    )
    args = parser.parse_args(argv)
    rows = build()
    rebuilt = render(rows)
    if args.check:
        return 0 if _check(JOIN_NAME, rebuilt) else 1
    (HERE / JOIN_NAME).write_bytes(rebuilt.encode("utf-8"))
    print(f"wrote {JOIN_NAME}: {len(rows)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
