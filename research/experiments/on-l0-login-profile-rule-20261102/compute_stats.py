#!/usr/bin/env python3
"""Compute exact statistical metrics and Wilson intervals from summary receipts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

STATS_PATH = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train/stats.py")
sys.path.insert(0, str(STATS_PATH.parent))
import stats  # noqa: E402

RECEIPTS_PATH = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train/raw/L0-LOGIN-PROFILE-RULE/receipts/summary.json")


def main() -> int:
    if not RECEIPTS_PATH.exists():
        print(f"Error: {RECEIPTS_PATH} does not exist", file=sys.stderr)
        return 1

    data = json.loads(RECEIPTS_PATH.read_text())

    evasions = data["evasions"]
    honest = data["honest_negatives"]

    n_ev = evasions["n"]
    before_ev_k = evasions["before_k"]
    after_ev_k = evasions["after_k"]

    before_ev_rate = stats.fmt(before_ev_k, n_ev)
    after_ev_rate = stats.fmt(after_ev_k, n_ev)
    ev_paired = evasions["paired"]

    print("=" * 60)
    print("L0-LOGIN-PROFILE-RULE STATISTICAL SUMMARY")
    print("=" * 60)
    print(f"Receipt path: {RECEIPTS_PATH}")
    print()
    print("--- 1. Known Evasions Cohort (n = 4) ---")
    print(f"Before L0 recall: {before_ev_rate}")
    print(f"After L0 recall:  {after_ev_rate}")
    print(f"Discordant b (after only): {ev_paired['b']}, c (before only): {ev_paired['c']}")
    print(f"Paired exact McNemar p: {ev_paired['p']:.6f}")
    print()
    print("--- 2. Honest Benchmark Negatives Cohort (n = 399) ---")
    n_neg = honest["n"]
    hard_drops = honest["new_hard_drops_count"]
    soft_reviews = honest["new_soft_reviews_count"]

    hard_drop_fmt = stats.fmt(hard_drops, n_neg)
    soft_review_fmt = stats.fmt(soft_reviews, n_neg)

    print(f"Total scored negatives evaluated: n = {n_neg}")
    print(f"New Hard Drops (False Drops): {hard_drop_fmt}")
    print(f"New Soft Reviews (Review queue): {soft_review_fmt}")
    print()
    print("Breakdown by family:")
    for fam, s in sorted(honest["by_family"].items()):
        print(f"  {fam:18s} n = {s['n']:3d} | hard drops: {s['hard_drop']:2d} | soft reviews: {s['soft_review']:2d}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
