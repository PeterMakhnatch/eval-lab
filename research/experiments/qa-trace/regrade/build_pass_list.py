#!/usr/bin/env python3
"""Reproduce the complete ordinary MiMo pass census, without forcing 75 rows.

The retained scan has 76 ordinary-by-prompt reward=1 observations. Two pairs
are duplicate paths and four rows are copies of an already-listed execution.
UUID-bound source diffs may be replayed once; observations are never silently
removed. Unflagged fixes are unreviewed, not established clean.

    uv run python research/experiments/qa-trace/regrade/build_pass_list.py [--check]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "src"))

from evallab.results_home import results_root  # noqa: E402
from evallab.storage.paths import shared_checkout_root  # noqa: E402

SCAN = shared_checkout_root(REPO) / "derived/detector-roots/own-scan"
PROMPTED = re.compile(r"cheat|exploit", re.IGNORECASE)
FAULT = {"partial_fix_passed": "faulty", "correct": "reviewed_correct", "unclear": "unclear"}


def _jsonl(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)


def build() -> dict[Path, str]:
    review = {
        row["trial_id"]: row
        for row in json.loads((SCAN / "notes/GraderDesign.result.json").read_text())[
            "suspicious_passes"
        ]
    }
    diffs = json.loads((SCAN / "notes/GraderDesign_passes.json").read_text())
    reviewed_by_path = {row["trial_dir"]: row for row in diffs}
    root = results_root()

    def annotation(trial: Path, job: str) -> dict[str, Any]:
        verdict = review.get(trial.name, {})
        cohort = "prompted" if PROMPTED.search(job) else "ordinary"
        fault = FAULT.get(verdict.get("verdict"), "unreviewed")
        return {
            "group": f"{cohort}/{fault}",
            "cohort": cohort,
            "fault": fault,
            "review": verdict.get("verdict", "unflagged"),
            "review_why": verdict.get("why"),
        }

    observations: list[dict[str, Any]] = []
    by_execution: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line, text in enumerate((SCAN / "agent_trials.jsonl").read_text().splitlines(), 1):
        row = json.loads(text)
        if not (row["task"].startswith("mimo-v2.6-rl/") and (row.get("reward") or 0) > 0):
            continue
        if PROMPTED.search(row["job"]):
            continue
        trial = Path(row["trial_dir"])
        result = json.loads((trial / "result.json").read_text())
        entry = {
            **row,
            "scan_line": line,
            "execution_id": result["id"],
            "own_diff": (trial / "verifier/agent.diff").is_file(),
        }
        observations.append(entry)
        by_execution[result["id"]].append(entry)

    ordinary = []
    for row in observations:
        copies = by_execution[row["execution_id"]]
        if len({copy["task"] for copy in copies}) != 1:
            raise ValueError(f"Execution UUID spans different tasks: {row['execution_id']}")
        # A retained-copy row can use a source only through an exact execution UUID.
        source = next((copy for copy in copies if copy["own_diff"]), copies[0])
        trial = Path(source["trial_dir"])
        own = Path(row["trial_dir"])
        ordinary.append(
            {
                "item_id": f"own-scan:{row['scan_line']}",
                "task": row["task"],
                "trial_path": str(trial.relative_to(root)),
                "observation_trial_path": str(own.relative_to(root)),
                "execution_id": row["execution_id"],
                "scan_line": row["scan_line"],
                "source_scan_line": source["scan_line"],
                "source_binding": "own_trial" if trial == own else "same_execution_uuid",
                "own_diff": row["own_diff"],
                "graderdesign_idx": reviewed_by_path.get(str(trial), {}).get("idx"),
                "observed_reward": row["reward"],
                "observed_exception": row.get("exception"),
                **annotation(trial, row["job"]),
            }
        )

    reviewed = [
        {
            "item_id": f"graderdesign:{row['idx']}",
            "task": f"mimo-v2.6-rl/{row['task']}",
            "trial_path": str(Path(row["trial_dir"]).relative_to(root)),
            **annotation(Path(row["trial_dir"]), row["job"]),
        }
        for row in sorted(diffs, key=lambda r: r["idx"])
    ]
    return {
        HERE / "ordinary-passes.jsonl": _jsonl(ordinary),
        HERE / "reviewed-passes.jsonl": _jsonl(reviewed),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="fail if either committed census differs"
    )
    args = parser.parse_args()
    stale = False
    for path, text in build().items():
        if args.check:
            same = path.is_file() and path.read_text() == text
            print(f"{path.name}: {'up to date' if same else 'stale'}")
            stale |= not same
        else:
            path.write_text(text)
            print(f"wrote {path.name}: {len(text.splitlines())} observations")
    return int(stale)


if __name__ == "__main__":
    raise SystemExit(main())
