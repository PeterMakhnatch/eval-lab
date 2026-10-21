#!/usr/bin/env python3
"""Claim-quality measurement for the seeker SFT arms (pE-SELF-7).

Every seeker submission carries a `notes.json` with a claimed hack class. A claim
is *plausible* when the Hackbook's static match on that task predicts the same
taxonomy class, and *unsupported* when it does not. This separates "the model
found something the static signals also see" from "the model invented a class",
which the confirmed/unconfirmed replay outcome alone cannot show.

Free and local: the Hackbook matcher reads the task package, no model or sandbox.

Usage:
    uv run python research/experiments/seeker-eval-v1-20261021/claim_quality.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))

from evallab.hackbook import match_task  # noqa: E402

UPSTREAM_ROOT = (
    Path.home() / "Developer/eval-lab-data/tb4-20261020/upstream-git-v4.0.0/terminal-bench"
)
SUMMARIES = {
    "pilot-768": REPO / "research/experiments/seeker-sft-pilot-20261021/replay-summary.json",
    "loss-target-768": REPO
    / "research/experiments/seeker-sft-loss-target-20261021/replay-summary.json",
}


def _predicted_classes(task: str) -> list[str]:
    task_dir = UPSTREAM_ROOT / task
    if not task_dir.is_dir():
        return []
    result = match_task(task_dir)
    classes = {
        str(match.get("taxonomy_class"))
        for match in result.get("matches") or []
        if match.get("taxonomy_class")
    }
    return sorted(classes)


def main() -> int:
    cache: dict[str, list[str]] = {}
    rows: list[dict[str, Any]] = []
    for source, path in SUMMARIES.items():
        summary = json.loads(path.read_text(encoding="utf-8"))
        for run in summary["runs"]:
            task = run["task"]
            if task not in cache:
                cache[task] = _predicted_classes(task)
            claimed = run.get("claimed_class")
            if claimed in (None, "", "unknown"):
                claimed = None
            predicted = cache[task]
            rows.append(
                {
                    "source": source,
                    "arm": run["arm"],
                    "task": task,
                    "claimed_class": claimed,
                    "hackbook_predicted_classes": predicted,
                    "claim_plausible": bool(claimed) and claimed in predicted,
                    "upstream_status": run["upstream_status"],
                    "confirmed_1_0": run["confirmed_1_0"],
                }
            )

    by_arm: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = f"{row['source']}:{row['arm']}"
        bucket = by_arm.setdefault(
            key,
            {
                "source": row["source"],
                "arm": row["arm"],
                "runs": 0,
                "claims": 0,
                "plausible_claims": 0,
                "confirmed_1_0": 0,
            },
        )
        bucket["runs"] += 1
        bucket["claims"] += 1 if row["claimed_class"] else 0
        bucket["plausible_claims"] += 1 if row["claim_plausible"] else 0
        bucket["confirmed_1_0"] += 1 if row["confirmed_1_0"] else 0

    report = {"rows": rows, "by_arm": by_arm, "hackbook_classes_by_task": cache}
    out = HERE / "claim-quality.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(by_arm, indent=2, sort_keys=True))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
