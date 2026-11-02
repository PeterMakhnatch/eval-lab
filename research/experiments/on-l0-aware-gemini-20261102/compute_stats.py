#!/usr/bin/env python3
"""Compute exact stats, Wilson intervals, and McNemar test for K7D receipts."""

from __future__ import annotations

import importlib.util
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
STATS_PATH = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/stats.py")
spec = importlib.util.spec_from_file_location("stats", STATS_PATH)
stats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stats)

ROOT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/K7D")
RAW = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/raw/K7D-L0AWARE-GEMINI")


def main():
    l0aware_path = ROOT / "l0aware.jsonl"
    spend_path = ROOT / "spend.jsonl"

    # 1. Attestation analysis from spend.jsonl
    spend_records = [json.loads(line) for line in spend_path.read_text().splitlines() if line.strip()]
    total_calls = len(spend_records)
    attested_calls = sum(1 for r in spend_records if r.get("attested"))
    served_routes = Counter(r.get("served") for r in spend_records)
    requested_routes = Counter(r.get("route") for r in spend_records)
    fallbacks = sum(1 for r in spend_records if not r.get("attested"))
    statuses = Counter(r.get("status") for r in spend_records)

    # 2. Package-level analysis from l0aware.jsonl
    packages = [json.loads(line) for line in l0aware_path.read_text().splitlines() if line.strip()]
    n = len(packages)

    a1_wins = []
    a0_wins = []
    a1_pass_l0_any = []
    a1_clean_any = []
    a0_clean = []

    attempt_counts = Counter()
    rules_fired_counts = Counter()
    classes_counts = Counter()

    for pkg in packages:
        # A0 (naive baseline)
        a0_pass = pkg["a0"]["pass_L0"]
        # In K7B, only 000931 was clean, all others had findings
        is_000931 = "000931" in pkg["task_id"]
        a0_is_clean = is_000931
        a0_win = a0_pass and a0_is_clean
        a0_wins.append(a0_win)
        a0_clean.append(a0_is_clean)

        # A1 (informed attacker)
        a1_win = pkg["win"]
        a1_wins.append(a1_win)
        a1_pass_l0_any.append(pkg["pass_L0_any"])
        a1_clean_any.append(pkg["clean_any"])

        for att in pkg["attempts"]:
            if att.get("no_candidate"):
                attempt_counts["no_candidate"] += 1
                continue
            pass_l0 = att.get("pass_L0")
            clean = att.get("clean")
            if pass_l0 and not clean:
                attempt_counts["pass_L0_and_fired"] += 1
            elif not pass_l0 and clean:
                attempt_counts["fail_L0_and_clean"] += 1
            elif pass_l0 and clean:
                attempt_counts["pass_L0_and_clean_WIN"] += 1
            else:
                attempt_counts["fail_L0_and_fired"] += 1

            for r in att.get("fired", []):
                rules_fired_counts[r] += 1
            if att.get("class"):
                classes_counts[att.get("class")] += 1

    # Statistical tests
    paired_result = stats.paired(a1_wins, a0_wins)
    k_a1 = sum(a1_wins)
    k_a0 = sum(a0_wins)
    wilson_a1 = stats.wilson(k_a1, n)
    wilson_a0 = stats.wilson(k_a0, n)

    summary = {
        "n_packages": n,
        "attestation": {
            "total_calls": total_calls,
            "attested_calls": attested_calls,
            "fallbacks": fallbacks,
            "served_routes": dict(served_routes),
            "requested_routes": dict(requested_routes),
            "call_statuses": dict(statuses),
        },
        "a1_informed": {
            "wins": k_a1,
            "rate_pct": round(100 * k_a1 / n, 2),
            "wilson_95": [round(wilson_a1[0], 4), round(wilson_a1[1], 4)],
            "fmt": stats.fmt(k_a1, n),
            "pass_l0_any_k": sum(a1_pass_l0_any),
            "pass_l0_any_fmt": stats.fmt(sum(a1_pass_l0_any), n),
            "clean_any_k": sum(a1_clean_any),
            "clean_any_fmt": stats.fmt(sum(a1_clean_any), n),
        },
        "a0_naive": {
            "wins": k_a0,
            "rate_pct": round(100 * k_a0 / n, 2),
            "wilson_95": [round(wilson_a0[0], 4), round(wilson_a0[1], 4)],
            "fmt": stats.fmt(k_a0, n),
        },
        "paired_comparison": {
            "b_a1_only": paired_result["b"],
            "c_a0_only": paired_result["c"],
            "total_discordant": paired_result["b"] + paired_result["c"],
            "both": paired_result["both"],
            "neither": paired_result["neither"],
            "mcnemar_exact_p": round(paired_result["p"], 4),
        },
        "attempt_breakdown": dict(attempt_counts),
        "rules_fired_counts": dict(rules_fired_counts),
        "taxonomy_classes": dict(classes_counts),
    }

    print(json.dumps(summary, indent=2))
    summary_path = RAW / "receipts" / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"\nWrote summary to {summary_path}")


if __name__ == "__main__":
    main()
