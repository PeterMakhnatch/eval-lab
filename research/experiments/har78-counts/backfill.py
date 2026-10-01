"""Back-fill HAR-78 counts for landed HAR-81, HAR-104, and HAR-110 trials.

Rollup matches RESULTS.md:
- HAR-81: 44 ARVO cyber tasks (no ledger covers these tasks / unchecked)
- HAR-104: 10 dev and v1 baseline plain runs
- HAR-110: 27 real trials organized per round and arm:
    - development: plain (6), seed-addendum (6), gepa-candidate (6)
    - held-out: plain (4), seed-addendum (4), gepa-candidate (4)
    - v1-only (dropped): plain (3), seed-addendum (3)
    (Non-trial GEPA search attempt records are excluded)
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from evallab.counts import attach_counts, find_label_root, summarize_counts
from evallab.process_job import _process_trial

REPO = find_label_root(Path(__file__))
ROOT = Path(__file__).resolve().parents[3]
WORKTREES = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees")
OUT = Path(__file__).with_name("backfill.jsonl")


def _index_trials() -> dict[str, Path]:
    found: dict[str, Path] = {}
    if not WORKTREES.is_dir():
        return found
    for result in WORKTREES.glob("*/runs/*/*/result.json"):
        found.setdefault(result.parent.name, result.parent)
    return found


def _counts_for_trial(trial_dir: Path, record: dict, result: dict) -> dict:
    spec_path = trial_dir.parent / "experiment-spec.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8")) if spec_path.is_file() else {}
    return attach_counts(
        record,
        result,
        label_root=REPO,
        package_digest=spec.get("task_package_digest"),
        task_id=spec.get("task_id"),
    )


def _load_har81(located: dict[str, Path]) -> list[dict]:
    rows: list[dict] = []
    csv_path = ROOT / "research/experiments/har114-tokenflow/runs.csv"
    with csv_path.open(encoding="utf-8") as handle:
        for item in csv.DictReader(handle):
            if item["source"] != "HAR-81":
                continue
            trial_name = item["trial"]
            t_dir = located.get(trial_name)
            if not t_dir:
                continue
            record = _process_trial(t_dir, t_dir.parent)
            result = json.loads((t_dir / "result.json").read_text(encoding="utf-8"))
            counts = _counts_for_trial(t_dir, record, result)
            rows.append(
                {
                    "campaign": "HAR-81",
                    "round_arm": "arvo_cyber",
                    "task": record.get("task_name"),
                    "trial": trial_name,
                    "ledger_note": "no ledger covers these tasks (not checked)",
                    "raw_reward": counts["raw_reward"],
                    "verdict": counts["verdict"],
                    "reasons": counts["reasons"],
                    "flags": [flag["code"] for flag in counts["flags"]],
                }
            )
    return rows


def _load_har104(located: dict[str, Path]) -> list[dict]:
    rows: list[dict] = []
    hand_dir = ROOT / "research/explorations/trace-lab/har109/hand"
    for path in sorted(hand_dir.glob("*.json")):
        trial_name = path.stem
        t_dir = located.get(trial_name)
        if not t_dir:
            continue
        record = _process_trial(t_dir, t_dir.parent)
        result = json.loads((t_dir / "result.json").read_text(encoding="utf-8"))
        counts = _counts_for_trial(t_dir, record, result)
        rows.append(
            {
                "campaign": "HAR-104",
                "round_arm": "plain_baseline",
                "task": record.get("task_name"),
                "trial": trial_name,
                "ledger_note": "census + HAR-109/111 hand labels",
                "raw_reward": counts["raw_reward"],
                "verdict": counts["verdict"],
                "reasons": counts["reasons"],
                "flags": [flag["code"] for flag in counts["flags"]],
            }
        )
    return rows


def _load_har110(located: dict[str, Path]) -> list[dict]:
    rows: list[dict] = []
    v2_trials = ROOT / "research/experiments/har110-python-gepa/results-v2-trials.jsonl"
    lines = [json.loads(line) for line in v2_trials.read_text(encoding="utf-8").splitlines() if line.strip()]
    for item in lines:
        job = item["job"]
        split = item["split"]
        arm = item["arm"]
        task_id = item["task"]
        # Find matching trial directory
        t_dir = None
        for _name, path in located.items():
            if path.parent.name == job:
                t_dir = path
                break
        if not t_dir:
            continue
        record = _process_trial(t_dir, t_dir.parent)
        result = json.loads((t_dir / "result.json").read_text(encoding="utf-8"))
        counts = _counts_for_trial(t_dir, record, result)
        rows.append(
            {
                "campaign": "HAR-110",
                "round_arm": f"{split} / {arm}",
                "task": record.get("task_name") or f"format-code-task-{task_id}",
                "trial": t_dir.name,
                "job": job,
                "ledger_note": "census + HAR-109/111 hand labels",
                "raw_reward": counts["raw_reward"],
                "verdict": counts["verdict"],
                "reasons": counts["reasons"],
                "flags": [flag["code"] for flag in counts["flags"]],
            }
        )
    return rows


def main() -> int:
    if REPO is None:
        print("label root not found", file=sys.stderr)
        return 1

    located = _index_trials()
    har81 = _load_har81(located)
    har104 = _load_har104(located)
    har110 = _load_har110(located)

    all_rows = har81 + har104 + har110
    OUT.write_text("".join(json.dumps(row) + "\n" for row in all_rows), encoding="utf-8")

    print("# Campaign Rollup\n")
    print(f"HAR-81: {len(har81)} trials")
    print("  ", summarize_counts([{"counts": r} for r in har81]))
    print(f"HAR-104: {len(har104)} trials")
    print("  ", summarize_counts([{"counts": r} for r in har104]))

    print(f"\nHAR-110 by arm ({len(har110)} real trials; excludes search queue records):")
    arms: dict[str, list[dict]] = {}
    for r in har110:
        arms.setdefault(r["round_arm"], []).append(r)
    for arm, items in arms.items():
        summary = summarize_counts([{"counts": r} for r in items])
        raw_passes = sum(1 for r in items if r["raw_reward"] is not None and r["raw_reward"] >= 1.0)
        print(f"  {arm:32}: N={len(items)} raw_pass={raw_passes} -> {summary}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
