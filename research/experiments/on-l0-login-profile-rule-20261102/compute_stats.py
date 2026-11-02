#!/usr/bin/env python3
"""Compute exact statistical metrics and Wilson intervals from summary receipts."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
import stats  # noqa: E402

CANDIDATE_PATHS = [
    Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train/raw/FIX-L0-1792/receipts/summary.json"),
    Path(os.path.expanduser("~/Developer/eval-lab-results/2026-11-02/FIX-L0-1792/summary.json")),
    Path(os.path.expanduser("~/Developer/eval-lab-results/2026-11-02/L0-LOGIN-PROFILE-RULE/summary.json")),
]


def main() -> int:
    receipt_path = None
    for p in CANDIDATE_PATHS:
        if p.exists():
            receipt_path = p
            break

    if receipt_path is None:
        print("Error: No summary receipt found in candidate paths", file=sys.stderr)
        return 1

    data = json.loads(receipt_path.read_text())

    evasions = data["evasions"]
    honest = data["honest_negatives"]

    p4 = evasions["primary_cohort_n4"]
    p7 = evasions["all_attempts_n7"]

    print("=" * 60)
    print("FIX-L0-1792 / L0-LOGIN-PROFILE-RULE STATISTICAL SUMMARY")
    print("=" * 60)
    print(f"Receipt path: {receipt_path}")
    print()
    print("--- 1. In-Sample Known Evasions Cohort ---")
    print(f"Status: {evasions.get('status')}")
    print(f"Primary cohort (n = {p4['n']}):")
    print(f"  Before recall: {stats.fmt(p4['before_k'], p4['n'])}")
    print(f"  After recall:  {stats.fmt(p4['after_k'], p4['n'])}")
    print(f"  Discordant b (after only): {p4['paired']['b']}, c (before only): {p4['paired']['c']}")
    print(f"  Paired exact McNemar p: {p4['paired']['p']:.6f} (not significant)")
    print()
    print(f"All attempts cohort (n = {p7['n']}):")
    print(f"  Before recall: {stats.fmt(p7['before_k'], p7['n'])}")
    print(f"  After recall:  {stats.fmt(p7['after_k'], p7['n'])}")
    print(f"  Discordant b: {p7['paired']['b']}, c: {p7['paired']['c']}, p: {p7['paired']['p']:.6f}")
    print()
    print("--- 2. Honest Benchmark Negatives Cohort (n = 399) ---")
    n_neg = honest["n"]
    hard_drops = honest["after_hard_drops_count"]
    soft_reviews = honest["after_soft_reviews_count"]

    print(f"Total scored negatives evaluated: n = {n_neg}")
    print(f"New Hard Drops (False Drops): {stats.fmt(hard_drops, n_neg)}")
    print(f"New Soft Reviews (Review queue): {stats.fmt(soft_reviews, n_neg)}")
    print()
    print("Breakdown by family:")
    for fam, s in sorted(honest["by_family"].items()):
        print(f"  {fam:18s} n = {s['n']:3d} | hard drops: {s['hard_drop']:2d} | soft reviews: {s['soft_review']:2d}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
