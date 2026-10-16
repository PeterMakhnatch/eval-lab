"""Paired pin regrade: would EnvCheck's repaired variant have rejected a v6 pass?

`analyze.py`'s pin block compares v6 and repaired-variant solve rates over separate
trials, so its gap is unpaired. This pairs them. Every v6-passing headline trial (frozen
v6 package) of a validity-flagged pin is regraded from its own snapshot on EnvCheck's
repaired variant of that same package. `heldout-pin-repaired-graders.csv` pins those
variants by digest: each is the frozen v6 package with appended repair tests
(`tests/test.patch`, `tests/test_command.sh`) and nothing else. A flip (v6 1 ->
repaired 0) is a paired incorrect pass. A v6 failure needs no regrade: the repair only
adds tests.

    # 1. pass list: one row per v6-passing headline pin trial, all arms; group pin/<arm>
    uv run python research/experiments/repaired-data-sft-v1/pin_pairs.py \
        --arm A=runs/rollout/arm-a/results.jsonl ... \
        --package-pairs <mimo-training-20261016>/package-pairs.csv --out pin-passes.jsonl
    # 2. same gated regrade as incorrect_pass.py, on the repaired variants
    uv run evallab qa trace regrade --passes pin-passes.jsonl --environment modal --plan \
        --grader-manifest research/experiments/repaired-data-sft-v1/heldout-pin-repaired-graders.csv \
        --out runs/rds1-pin-pairs
    # 3. per-arm paired counts are summary.json's pin/<arm> rows (flips = incorrect passes);
    #    `incorrect_pass.py collect --grader-manifest heldout-pin-repaired-graders.csv`
    #    keeps only measured rows on the pinned variant with valid controls.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from analyze import v6_digests
from incorrect_pass import MIMO, task_id, trial_dir

GRADERS = Path(__file__).with_name("heldout-pin-repaired-graders.csv")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--arm", action="append", required=True, help="NAME=results.jsonl")
    ap.add_argument("--package-pairs", type=Path, required=True, help="MiMo package-pairs.csv")
    ap.add_argument("--graders", type=Path, default=GRADERS, help="pin repaired-variant manifest")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    with args.graders.open(encoding="utf-8") as handle:
        pins = {r["task_id"] for r in csv.DictReader(handle)}
    v6 = v6_digests(args.package_pairs)
    rows: list[dict[str, Any]] = []
    counts: dict[str, dict[str, int]] = {}
    for spec in args.arm:
        arm, _, path = spec.partition("=")
        seen = counts.setdefault(arm, {"v6_pass": 0, "v6_fail": 0, "error": 0, "no_trial_dir": 0})
        for line in Path(path).read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            task = task_id(r["task"])
            if task not in pins or r["task"].rstrip("/").rsplit("/", 1)[-1] != v6.get(task):
                continue  # not a pin, or Infra's unpaired repaired-variant cell
            if r.get("error") or r.get("passed") is None:
                seen["error"] += 1
                continue
            if r["passed"] is not True:
                seen["v6_fail"] += 1
                continue
            trial = trial_dir(Path(r["job_dir"]))
            if trial is None:
                seen["no_trial_dir"] += 1
                continue
            seen["v6_pass"] += 1
            rows.append(
                {
                    "item_id": f"pin:{arm}:{r['name']}",
                    "task": f"{MIMO}/{task}",
                    "trial_path": str(trial),
                    "group": f"pin/{arm}",
                    "labels": {"arm": arm, "trial": r["name"]},
                }
            )
    args.out.write_text("".join(json.dumps(row) + "\n" for row in rows))
    print(json.dumps({"pin_regrades": len(rows), "pins": sorted(pins), "per_arm": counts}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
