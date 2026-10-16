"""Score the held-out evaluation of the repaired-data SFT arms.

Inputs:
- one `evallab rollout` `results.jsonl` per arm (`--arm NAME=PATH`, repeatable; include the
  untrained base as its own arm);
- the frozen held-out manifest (`--heldout`): MiMo data's `mimo.training-manifest/v1`
  (`heldout: list[str]` of bare source ids). Scoring is refused unless it says
  `scoring_allowed: true`;
- the arm exports' `summary.json` (`--exports`), used to prove that no held-out task was
  trained on;
- optionally, `--original-regrade` JSONL rows `{arm, trial, original_pass}`, written by
  `incorrect_pass.py`. Each row comes from replaying one held-out trial's submission on
  the task's *original* v5 grader.

Two definitions:
- **Solve:** reward 1 on the v6@10 held-out package, which is the only grade the trial
  receives.
- **Incorrect pass:** the original grader passes a submission that the v6@10 grader
  rejects. The model would have been rewarded for wrong work under the original grader.
  - Only v6-failing trials need a replay; a v6 pass cannot be an incorrect pass.
  - The denominator is the v6 passes plus the v6 failures whose original-grader replay
    was measured.
  - An unreplayed v6 failure is counted as unmeasured, never as correct.

Uncertainty:
- exact Clopper–Pearson 95% intervals for every rate;
- an exact two-sided sign test on paired per-task solve rates, for B vs C (primary) and
  for each arm vs the base. With one attempt per task this is McNemar's exact test.

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
    """Rollout `passed` (reward >= 1), or None for an errored or unmeasured trial."""
    if row.get("error"):
        return None
    if row.get("passed") is not None:
        return bool(row["passed"])
    reward = row.get("reward")
    return None if reward is None else float(reward) >= 1.0


def norm(task: str) -> str:
    """Bare MiMo source id from an id, a namespaced id, or a task-store package path."""
    for part in reversed(task.split("/")):
        if "__" in part:
            return part.split("__", 1)[1]
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

    # One list of (trial name, passed) per task: `evallab rollout --attempts` gives several.
    arms: dict[str, dict[str, list[tuple[str, bool | None]]]] = {}
    for spec in args.arm:
        name, _, path = spec.partition("=")
        rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
        by_task: dict[str, list[tuple[str, bool | None]]] = {}
        for row in rows:
            task = norm(task_of(row))
            if task not in heldout:
                print(f"REFUSED: arm {name} ran non-held-out task {task}", file=sys.stderr)
                return 2
            by_task.setdefault(task, []).append((str(row.get("name") or task), passed(row)))
        arms[name] = by_task

    # Incorrect-pass replays, keyed by (arm, trial name): original-grader verdict.
    original: dict[tuple[str, str], bool] = {}
    if args.original_regrade:
        for line in args.original_regrade.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                original[(r["arm"], r["trial"])] = bool(r["original_pass"])

    report: dict[str, Any] = {"heldout_tasks": len(heldout), "arms": {}, "paired": {}}
    for name, by_task in arms.items():
        trials = [(t, n, v) for t, items in by_task.items() for n, v in items]
        measured = [v for _, _, v in trials if v is not None]
        solves = sum(measured)
        # v6 passes plus v6 failures with a measured original-grader replay.
        replayed = [
            (n, v) for _, n, v in trials if v is True or (v is False and (name, n) in original)
        ]
        incorrect = sum(1 for n, v in replayed if v is False and original[(name, n)])
        report["arms"][name] = {
            "examples": exports.get("arms", {}).get(name, {}).get("rows"),
            "tasks": len(by_task),
            "trials": len(trials),
            "measured": len(measured),
            "errors": len(trials) - len(measured),
            "solves": solves,
            "solve_rate": round(solves / len(measured), 4) if measured else None,
            "solve_ci95": clopper_pearson(solves, len(measured)),
            "incorrect_pass_measured": len(replayed),
            "incorrect_passes": incorrect,
            "incorrect_pass_rate": round(incorrect / len(replayed), 4) if replayed else None,
            "incorrect_pass_ci95": clopper_pearson(incorrect, len(replayed)),
        }
        if len(trials) > len(by_task):
            report["arms"][name]["note"] = (
                "several attempts per task: the trial-level interval ignores within-task "
                "correlation; the paired test below works per task"
            )

    def task_rate(items: list[tuple[str, bool | None]]) -> float | None:
        values = [v for _, v in items if v is not None]
        return sum(values) / len(values) if values else None

    # Paired exact sign test per task (McNemar when there is one attempt per task).
    pairs = [("B", "C")] + [(n, "base") for n in arms if n != "base" and "base" in arms]
    for left, right in pairs:
        if left not in arms or right not in arms:
            continue
        rates = {
            t: (task_rate(arms[left][t]), task_rate(arms[right].get(t, []))) for t in arms[left]
        }
        common = {t: r for t, r in rates.items() if None not in r}
        better = sum(1 for lr, rr in common.values() if lr > rr)
        worse = sum(1 for lr, rr in common.values() if lr < rr)
        report["paired"][f"{left}_vs_{right}"] = {
            "tasks": len(common),
            f"{left}_better": better,
            f"{right}_better": worse,
            "mean_difference": round(sum(lr - rr for lr, rr in common.values()) / len(common), 4)
            if common
            else None,
            "exact_sign_p": round(mcnemar_exact(better, worse), 4),
        }
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
