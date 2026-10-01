"""Back-fill HAR-78 counts for the landed HAR-81, HAR-104, and HAR-110 trials.

Reads the trials already on disk. Does not start a run. Writes a JSONL next
to this script and prints the campaign rollup.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from evallab.counts import attach_counts, find_label_root, summarize_counts
from evallab.process_job import _process_trial

REPO = find_label_root(Path(__file__))
WORKTREES = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees")
OUT = Path(__file__).with_name("backfill.jsonl")
ROOT = Path(__file__).resolve().parents[3]


def _campaign_trials() -> list[tuple[str, str]]:
    wanted: list[tuple[str, str]] = []
    with (ROOT / "research/experiments/har114-tokenflow/runs.csv").open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["source"] == "HAR-81":
                wanted.append(("HAR-81", row["trial"]))
    hand = ROOT / "research/explorations/trace-lab/har109/hand"
    for path in sorted(hand.glob("*.json")):
        wanted.append(("HAR-104", path.stem))
    live = WORKTREES / "har110-live/runs"
    if live.is_dir():
        for job in sorted(p for p in live.iterdir() if p.is_dir() and not p.name.startswith(".")):
            for trial in sorted(p for p in job.iterdir() if (p / "result.json").is_file()):
                wanted.append(("HAR-110", trial.name))
    return wanted


def _index_trials() -> dict[str, Path]:
    found: dict[str, Path] = {}
    if not WORKTREES.is_dir():
        return found
    for result in WORKTREES.glob("*/runs/*/*/result.json"):
        found.setdefault(result.parent.name, result.parent)
    return found


def main() -> int:
    if REPO is None:
        print("label root not found", file=sys.stderr)
        return 1
    located = _index_trials()
    rows: list[dict] = []
    missing: list[str] = []
    for campaign, trial_name in _campaign_trials():
        trial = located.get(trial_name)
        if trial is None:
            missing.append(f"{campaign} {trial_name}")
            continue
        record = _process_trial(trial, trial.parent)
        result = json.loads((trial / "result.json").read_text(encoding="utf-8"))
        counts = attach_counts(record, result, label_root=REPO)
        rows.append(
            {
                "campaign": campaign,
                "trial": trial_name,
                "task": record.get("task_name"),
                "raw_reward": counts["raw_reward"],
                "verdict": counts["verdict"],
                "reasons": counts["reasons"],
                "flags": [flag["code"] for flag in counts["flags"]],
            }
        )
        print(f"{campaign} {trial_name} {counts['verdict']} {counts['reasons']}", flush=True)
    OUT.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    print("\n# rollup")
    for campaign in ("HAR-81", "HAR-104", "HAR-110"):
        group = [{"counts": row} for row in rows if row["campaign"] == campaign]
        print(campaign, len(group), summarize_counts(group))
    if missing:
        print("missing", len(missing))
        for item in missing:
            print(" ", item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
