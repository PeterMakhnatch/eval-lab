"""Incorrect-pass replays for the held-out evaluation: would the original grader have passed it?

For each arm's `evallab rollout` results, every v6-failing trial is replayed on its task's
*original* v5 package. `heldout-v5-graders.csv` pins those packages by digest. The replay
uses QA Trace's counterfactual regrade (`evallab qa trace regrade --passes ...
--grader-manifest heldout-v5-graders.csv`). A v6 pass needs no replay: it cannot be an
incorrect pass.

Run every step from a venv synced with `uv sync --extra laminar`: cloud regrade refuses any
Harbor other than the lab-pinned one.

    # 1. pass list for the regrade (one row per v6-failing trial, all arms)
    uv run python research/experiments/repaired-data-sft-v1/incorrect_pass.py plan \
        --arm A=runs/rollout/arm-a/results.jsonl ... --out replays.jsonl
    # 2a. count the uncached executions and the estimate (writes OUT/plan.json, runs nothing)
    uv run evallab qa trace regrade --passes replays.jsonl --environment modal --plan \
        --grader-manifest research/experiments/repaired-data-sft-v1/heldout-v5-graders.csv \
        --out runs/rds1-incorrect-pass
    # 2b. replay on Modal under the spend gate (paid; deterministic, no model calls).
    #     2026-10-16: QA Trace launches this on the Trainer's cap. The first 2 rows are the
    #     smoke, and it continues only if both are measured.
    uv run evallab qa trace regrade --passes replays.jsonl --environment modal --workers 8 \
        --grader-manifest research/experiments/repaired-data-sft-v1/heldout-v5-graders.csv \
        --max-usd 2.00 --approve "<recorded approval>" --out runs/rds1-incorrect-pass
    # 3. analyze.py input: {arm, trial, original_pass}. Kept only when the replay ran on
    #    the task's pinned v5 package, its v5 grader controls are valid, and it was measured.
    #    The regrade also grades any test-repaired variant found for the task; `collect`
    #    drops those rows.
    uv run python research/experiments/repaired-data-sft-v1/incorrect_pass.py collect \
        --regrade runs/rds1-incorrect-pass \
        --grader-manifest research/experiments/repaired-data-sft-v1/heldout-v5-graders.csv \
        --out original-regrade.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

MIMO = "mimo-v2.6-rl"


def task_id(task: str) -> str:
    for part in reversed(task.split("/")):
        if "__" in part:
            return part.split("__", 1)[1]
    return task.rsplit("/", 1)[-1]


def trial_dir(job_dir: Path) -> Path | None:
    """The one Harbor trial directory inside a rollout job directory: below the job's
    own `result.json`, a directory holding both `result.json` and `config.json`.
    """
    trials = {
        p.parent
        for p in job_dir.rglob("result.json")
        if p.parent != job_dir and (p.parent / "config.json").is_file()
    }
    return trials.pop() if len(trials) == 1 else None


def plan(args: argparse.Namespace) -> int:
    rows: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    for spec in args.arm:
        arm, _, path = spec.partition("=")
        for line in Path(path).read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("error") or r.get("passed") is not False:
                continue
            trial = trial_dir(Path(r["job_dir"]))
            if trial is None:
                skipped[arm] = skipped.get(arm, 0) + 1
                continue
            rows.append(
                {
                    "item_id": f"{arm}:{r['name']}",
                    "task": f"{MIMO}/{task_id(r['task'])}",
                    "trial_path": str(trial),
                    "labels": {"arm": arm, "trial": r["name"]},
                }
            )
    args.out.write_text("".join(json.dumps(row) + "\n" for row in rows))
    print(json.dumps({"replays": len(rows), "skipped_no_single_trial_dir": skipped}))
    return 0


def collect(args: argparse.Namespace) -> int:
    pinned = {
        f"{MIMO}/{r['task_id']}": r["final_digest"]
        for r in csv.DictReader(args.grader_manifest.open(encoding="utf-8"))
    }
    out: list[dict[str, Any]] = []
    unmeasured: dict[str, int] = {}
    for path in sorted((args.regrade / "items").glob("*/*.json")):
        row = json.loads(path.read_text())
        labels = row.get("labels") or {}
        regraded = row.get("regraded")
        if (row.get("grader_source") or {}).get("variant_digest") != pinned.get(row["task"]):
            continue  # a newer grader the regrade also ran, not the original v5 package
        reason = None
        if not row.get("valid_grader"):
            reason = "invalid_v5_grader_controls"
        elif row.get("outcome") == "refused" or regraded is None:
            reason = "replay_refused_or_unmeasured"
        if reason:
            unmeasured[reason] = unmeasured.get(reason, 0) + 1
            continue
        out.append(
            {
                "arm": labels["arm"],
                "trial": labels["trial"],
                "task": row["task"],
                "original_pass": float(regraded) >= 1.0,
                "grader": row["grader"],
            }
        )
    args.out.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in out))
    print(json.dumps({"measured": len(out), "unmeasured": unmeasured}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--arm", action="append", required=True, help="NAME=results.jsonl")
    p.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("collect")
    c.add_argument("--regrade", type=Path, required=True)
    c.add_argument("--grader-manifest", type=Path, required=True, help="heldout-v5-graders.csv")
    c.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    return plan(args) if args.cmd == "plan" else collect(args)


if __name__ == "__main__":
    raise SystemExit(main())
