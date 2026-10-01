#!/usr/bin/env python3
"""Oracle-pilot plan artifact: the 40-task pilot selection (NOT RUN).

Strata (assignment §3): 15 ledger `usable`; 10 `review` where only the
checker says broken; 5 `review` hand-one-rater/grader_suspect; 10
`discarded`. Train split only, so no held-out task is touched by a future
true-fix run. Within each stratum, lightest image first; ties broken by
sha256("oracle-pilot:1:" + task_id), so the pick is seeded and reproducible.

Records the package to run (ledger `run`: original, leak-closed or repair
variant) and its `run_digest`, per the Xiaomi-faithful grading note.

Writes selection.json next to this file and prints the pick.

Usage (from the checkout root):
    uv run python research/experiments/oracle-pilot/selection.py
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
LEDGER = ROOT / "research/experiments/python-task-ledger/ledger.csv"

SEED = "oracle-pilot:1"


def rank(task_id: str) -> str:
    return hashlib.sha256((SEED + ":" + task_id).encode()).hexdigest()


def mib(row: dict) -> float:
    try:
        return float(row["image_mib"] or "inf")
    except ValueError:
        return float("inf")


def pick(rows: list[dict], n: int) -> list[dict]:
    return sorted(rows, key=lambda r: (mib(r), rank(r["task_id"])))[:n]


def main() -> None:
    rows = [r for r in csv.DictReader(LEDGER.open()) if r["split"] == "train"]

    usable = [r for r in rows if r["status"] == "usable"]
    checker_only = [
        r for r in rows
        if r["status"] == "review"
        and r["checker_label"] == "broken"
        and "broken" not in (r["hand_labels"] or "")
        and not (r["reason"] or "").startswith("one hand rater")
        and "grader_suspect" not in (r["reason"] or "")
    ]
    hand_or_suspect = [
        r for r in rows
        if r["status"] == "review" and r not in checker_only
    ]
    discarded = [r for r in rows if r["status"] == "discarded"]

    sel = {
        "seed": SEED,
        "rule": "train split only; lightest image_mib first, sha256-seed tiebreak",
        "strata": {
            "usable": pick(usable, 15),
            "review_checker_only": pick(checker_only, 10),
            "review_hand_or_grader_suspect": pick(hand_or_suspect, 5),
            "discarded": pick(discarded, 10),
        },
    }
    slim = {
        k: [
            {c: r[c] for c in (
                "task_id", "project", "image_mib", "status", "reason",
                "run", "run_digest", "census_label", "checker_label",
                "hand_labels", "leak_channel",
            )}
            for r in v
        ]
        for k, v in sel["strata"].items()
    }
    (HERE / "selection.json").write_text(
        json.dumps({"seed": SEED, "rule": sel["rule"], "strata": slim}, indent=1) + "\n")
    for k, v in slim.items():
        print(f"## {k} ({len(v)})")
        for r in v:
            print(f"  {r['task_id']} {r['project']} {r['image_mib']}MiB run={r['run']}")
    need = {"usable": 15, "review_checker_only": 10,
            "review_hand_or_grader_suspect": 5, "discarded": 10}
    short = {k: n - len(slim[k]) for k, n in need.items() if len(slim[k]) < n}
    if short:
        print(f"SHORT: {short}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())
