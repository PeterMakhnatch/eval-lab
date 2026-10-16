"""Score the held-out evaluation of the repaired-data SFT arms.

Inputs:
- one `evallab rollout` `results.jsonl` per arm (`--arm NAME=PATH`, repeatable; include the
  untrained base as its own arm);
- the frozen held-out manifest (`--heldout`): MiMo data's `mimo.training-manifest/v1`
  (`heldout: list[str]` of bare source ids). Scoring is refused unless it says
  `scoring_allowed: true`;
- the arm exports' `summary.json` (`--exports`), used to prove that no held-out task was
  trained on;
- optionally, `--original-regrade` JSONL rows `{arm, task, original_pass}`. Each row comes
  from replaying an arm's held-out submission on the task's *original* grader.

Two definitions:
- **Solve:** reward 1 on the repaired held-out package. That is the only grade the trial
  receives.
- **Incorrect pass:** the original grader passes a submission that the repaired grader
  rejects. The model would have been rewarded for wrong work had the grader not been
  repaired. Its denominator is the trials whose original-grader replay was measured.
  Unmeasured trials are counted as unmeasured, never as correct.

Uncertainty:
- exact Clopper–Pearson 95% intervals for every rate;
- exact two-sided McNemar tests on paired tasks for B vs C (primary) and for each arm vs
  the base.

    uv run python research/experiments/repaired-data-sft-v1/analyze.py \
        --arm base=… --arm A=… --arm B=… --arm C=… \
        --heldout heldout.json --exports <exports>/summary.json [--original-regrade rows.jsonl] \
        --out results.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


def binom_cdf(k: int, n: int, p: float) -> float:
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))


def _root(f, target: float) -> float:
    """p in [0, 1] with f(p) == target, for f increasing in p (bisection)."""
    lo, hi = 0.0, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if f(mid) < target else (lo, mid)
    return (lo + hi) / 2


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float] | None:
    """Exact two-sided interval: P(X >= k | lower) = alpha/2, P(X <= k | upper) = alpha/2."""
    if n == 0:
        return None
    lower = 0.0 if k == 0 else _root(lambda p: 1 - binom_cdf(k - 1, n, p), alpha / 2)
    upper = 1.0 if k == n else _root(lambda p: 1 - binom_cdf(k, n, p), 1 - alpha / 2)
    return round(lower, 4), round(upper, 4)


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the discordant counts."""
    n = b + c
    if n == 0:
        return 1.0
    return min(1.0, 2 * binom_cdf(min(b, c), n, 0.5))


def task_of(row: dict[str, Any]) -> str:
    task = row.get("task") or row.get("task_id") or row.get("task_name")
    if not task:
        raise ValueError(f"result row has no task: {sorted(row)}")
    return str(task)


def passed(row: dict[str, Any]) -> bool | None:
    if row.get("error"):
        return None
    if "pass" in row and row["pass"] is not None:
        return bool(row["pass"])
    reward = row.get("reward")
    return None if reward is None else float(reward) >= 1.0


def norm(task: str) -> str:
    return task.rsplit("/", 1)[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--arm", action="append", required=True, help="NAME=results.jsonl")
    ap.add_argument("--heldout", type=Path, required=True)
    ap.add_argument("--exports", type=Path, required=True, help="to_export.py summary.json")
    ap.add_argument("--original-regrade", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    held = json.loads(args.heldout.read_text())
    if held.get("scoring_allowed") is not True:
        print("REFUSED: held-out manifest does not set scoring_allowed: true", file=sys.stderr)
        return 2
    heldout = {norm(t) for t in held["heldout"]}
    exports = json.loads(args.exports.read_text())
    trained_tasks: set[str] = set()
    for arm in ("A", "B", "C", "all"):
        manifest_path = args.exports.parent / arm / "manifest.json"
        if manifest_path.is_file():
            trained_tasks |= {
                norm(t) for t in json.loads(manifest_path.read_text()).get("tasks", [])
            }
    source = exports.get("source_manifest", {})
    # All of build_arms' tasks (42 on 2026-10-16), including rows the eligibility rule dropped.
    trained_tasks |= {norm(t) for t in source.get("tasks", [])}
    if not source.get("tasks"):
        print("REFUSED: exports summary lacks build_arms' task list", file=sys.stderr)
        return 2
    leak = sorted(heldout & trained_tasks)
    if leak:
        print(f"REFUSED: {len(leak)} held-out task(s) were trained on: {leak[:5]}", file=sys.stderr)
        return 2

    arms: dict[str, dict[str, bool | None]] = {}
    for spec in args.arm:
        name, _, path = spec.partition("=")
        rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
        by_task: dict[str, bool | None] = {}
        for row in rows:
            task = norm(task_of(row))
            if task not in heldout:
                print(f"REFUSED: arm {name} ran non-held-out task {task}", file=sys.stderr)
                return 2
            if task in by_task:
                raise ValueError(f"arm {name}: task {task} appears twice")
            by_task[task] = passed(row)
        arms[name] = by_task

    original: dict[tuple[str, str], bool] = {}
    if args.original_regrade:
        for line in args.original_regrade.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                original[(r["arm"], norm(r["task"]))] = bool(r["original_pass"])

    report: dict[str, Any] = {"heldout_tasks": len(heldout), "arms": {}, "paired": {}}
    for name, by_task in arms.items():
        measured = {t: v for t, v in by_task.items() if v is not None}
        solves = sum(measured.values())
        inc_measured = [(t, v) for t, v in by_task.items() if (name, t) in original]
        incorrect = sum(1 for t, v in inc_measured if original[(name, t)] and v is False)
        report["arms"][name] = {
            "examples": exports.get("arms", {}).get(name, {}).get("rows"),
            "trials": len(by_task),
            "measured": len(measured),
            "errors": len(by_task) - len(measured),
            "solves": solves,
            "solve_rate": round(solves / len(measured), 4) if measured else None,
            "solve_ci95": clopper_pearson(solves, len(measured)),
            "incorrect_pass_measured": len(inc_measured),
            "incorrect_passes": incorrect,
            "incorrect_pass_rate": round(incorrect / len(inc_measured), 4)
            if inc_measured
            else None,
            "incorrect_pass_ci95": clopper_pearson(incorrect, len(inc_measured)),
        }

    pairs = [("B", "C")] + [(n, "base") for n in arms if n != "base" and "base" in arms]
    for left, right in pairs:
        if left not in arms or right not in arms:
            continue
        common = [
            t for t in arms[left] if arms[left][t] is not None and arms[right].get(t) is not None
        ]
        only_left = sum(1 for t in common if arms[left][t] and not arms[right][t])
        only_right = sum(1 for t in common if arms[right][t] and not arms[left][t])
        report["paired"][f"{left}_vs_{right}"] = {
            "tasks": len(common),
            f"{left}_only": only_left,
            f"{right}_only": only_right,
            "difference": round((only_left - only_right) / len(common), 4) if common else None,
            "mcnemar_exact_p": round(mcnemar_exact(only_left, only_right), 4),
        }
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
