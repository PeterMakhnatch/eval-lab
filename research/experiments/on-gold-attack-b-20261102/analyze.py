#!/usr/bin/env python3
"""A5B: Statistical analysis of adaptive attacker against Gold v1.1 certified set vs L0.

Recomputes headline metrics from receipts using bridge/stats.py helpers.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

STATS_PATH = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert")
sys.path.insert(0, str(STATS_PATH))
import stats  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
ROOT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/A5B")
BRIDGE_RAW = Path(
    "/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert/raw/A5B-GOLD-ATTACK"
)


def load_receipts(receipts_dir: Path) -> list[dict[str, Any]]:
    """Load per-task receipt files."""
    if not receipts_dir.is_dir():
        return []
    receipts = []
    for p in sorted(receipts_dir.glob("format-code-task-*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            receipts.append(data)
        except Exception as exc:
            print(f"Warning: could not parse {p}: {exc}", file=sys.stderr)
    return receipts


def analyze(receipts: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute summary statistics over receipts."""
    total_tasks = len(receipts)
    usable_pairs = []
    excluded_no_attempt = []
    excluded_incomplete_cell = []

    for r in receipts:
        tid = r["task_id"]
        # Check attestation
        att = r.get("attestation") or {}
        if not att.get("attested"):
            excluded_no_attempt.append({"task_id": tid, "reason": "route_not_attested"})
            continue
        if "refused" in att or "reason" in r:
            excluded_no_attempt.append(
                {"task_id": tid, "reason": att.get("refused") or r.get("reason")}
            )
            continue

        l0_info = r.get("L0") or {}
        gold_info = r.get("Gold") or {}
        l0_rew = l0_info.get("reward")
        gold_rew = gold_info.get("reward")

        if l0_rew is None or gold_rew is None:
            excluded_incomplete_cell.append({
                "task_id": tid,
                "l0_status": l0_info.get("status"),
                "gold_status": gold_info.get("status"),
            })
            continue

        usable_pairs.append({
            "task_id": tid,
            "taxonomy_class": r.get("taxonomy_class", "unknown"),
            "mechanism": r.get("mechanism", ""),
            "l0_pass": (l0_rew == 1.0),
            "gold_pass": (gold_rew == 1.0),
            "l0_reward": l0_rew,
            "gold_reward": gold_rew,
        })

    n_paired = len(usable_pairs)
    l0_passes = [p["l0_pass"] for p in usable_pairs]
    gold_passes = [p["gold_pass"] for p in usable_pairs]

    k_l0 = sum(l0_passes)
    k_gold = sum(gold_passes)

    paired_stats = stats.paired(l0_passes, gold_passes) if n_paired > 0 else {}
    wilson_l0 = stats.wilson(k_l0, n_paired) if n_paired > 0 else (0.0, 0.0)
    wilson_gold = stats.wilson(k_gold, n_paired) if n_paired > 0 else (0.0, 0.0)

    gold_bypasses = [p for p in usable_pairs if p["gold_pass"]]

    return {
        "total_receipts": total_tasks,
        "n_paired": n_paired,
        "excluded_no_attempt": excluded_no_attempt,
        "excluded_incomplete_cell": excluded_incomplete_cell,
        "k_l0": k_l0,
        "k_gold": k_gold,
        "asr_l0_fmt": stats.fmt(k_l0, n_paired) if n_paired > 0 else "N/A",
        "asr_gold_fmt": stats.fmt(k_gold, n_paired) if n_paired > 0 else "N/A",
        "wilson_l0": wilson_l0,
        "wilson_gold": wilson_gold,
        "paired": paired_stats,
        "usable_pairs": usable_pairs,
        "gold_bypasses": gold_bypasses,
    }


def print_report(res: dict[str, Any]) -> None:
    print("=" * 70)
    print("A5B-GOLD-ATTACK ANALYSIS REPORT")
    print("=" * 70)
    print(f"Total Cohort Receipts: {res['total_receipts']}")
    print(f"Evaluated Pairs (n):   {res['n_paired']}")
    print(f"Excluded (No Attempt): {len(res['excluded_no_attempt'])}")
    for exc in res["excluded_no_attempt"]:
        print(f"  - {exc['task_id']}: {exc['reason']}")
    print(f"Excluded (Cell Error): {len(res['excluded_incomplete_cell'])}")
    for exc in res["excluded_incomplete_cell"]:
        print(f"  - {exc['task_id']}: L0={exc['l0_status']}, Gold={exc['gold_status']}")

    print("\nAttack Success Rates (ASR):")
    print(f"  ASR(L0):   {res['asr_l0_fmt']}")
    print(f"  ASR(Gold): {res['asr_gold_fmt']}")

    paired = res["paired"]
    if paired:
        print("\nPaired McNemar Comparison (L0 vs Gold):")
        print(f"  Both Passed:  {paired.get('both', 0)}")
        print(f"  Neither:      {paired.get('neither', 0)}")
        print(f"  b (L0 only):  {paired.get('b', 0)}")
        print(f"  c (Gold only):{paired.get('c', 0)}")
        print(f"  Exact p-value:{paired.get('p', 1.0):.6f}")

    print("\nPer-Task Breakdown:")
    for p in res["usable_pairs"]:
        status_l0 = "PASS" if p["l0_pass"] else "FAIL"
        status_gold = "PASS" if p["gold_pass"] else "FAIL"
        print(
            f"  {p['task_id']}: L0={status_l0} ({p['l0_reward']}), "
            f"Gold={status_gold} ({p['gold_reward']}) | [{p['taxonomy_class']}] {p['mechanism'][:60]}"
        )

    if res["gold_bypasses"]:
        print(f"\nGold Bypasses ({len(res['gold_bypasses'])}):")
        for b in res["gold_bypasses"]:
            print(f"  - {b['task_id']} [{b['taxonomy_class']}]: {b['mechanism']}")
    else:
        print("\nGold Bypasses: 0 (Gold prevented all evaluated bypasses!)")
    print("=" * 70)


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze A5B Receipts")
    parser.add_argument(
        "--receipts",
        type=Path,
        default=BRIDGE_RAW / "receipts",
        help="Path to receipts directory",
    )
    args = parser.parse_args()

    receipts = load_receipts(args.receipts)
    if not receipts and ROOT.exists():
        receipts = load_receipts(ROOT / "receipts")

    if not receipts:
        print(f"No receipts found in {args.receipts} or {ROOT / 'receipts'}")
        return 1

    res = analyze(receipts)
    print_report(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
