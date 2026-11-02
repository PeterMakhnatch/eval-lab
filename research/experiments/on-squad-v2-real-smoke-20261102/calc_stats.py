#!/usr/bin/env python3
"""calc_stats.py: Compute statistical intervals and exact McNemar test from receipts."""
import json
import sys
from pathlib import Path

# Add toy_plan to path for stats import
sys.path.append(str(Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/R8/toy_plan")))
import stats  # noqa: E402

ROOT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/R8")
receipt_a = json.loads((ROOT / "receipts" / "trial_a_runner_kill.json").read_text())
receipt_bcde = json.loads((ROOT / "receipts" / "trials_b_c_d_e_receipt.json").read_text())
receipt_smoke = json.loads((ROOT / "receipts" / "toy_plan_smoke_receipt.json").read_text())

print("=== Trial A: Runner Kill Resilience (10 trials) ===")
v2_trials = receipt_a["v2_trials"]
v1_trials = receipt_a["v1_trials"]

v2_adopted_bools = [r["adopted"] for r in v2_trials]
v1_adopted_bools = [r["adopted"] for r in v1_trials]

v2_no_dup_bools = [r["duplicate_workers"] == 0 for r in v2_trials]
v1_no_dup_bools = [r["duplicate_workers"] == 0 for r in v1_trials]

v2_k = sum(v2_no_dup_bools)
v1_k = sum(v1_no_dup_bools)
n = len(v2_trials)

w_v2_adopt = stats.wilson(sum(v2_adopted_bools), n)
w_v1_adopt = stats.wilson(sum(v1_adopted_bools), n)

w_v2_nodup = stats.wilson(v2_k, n)
w_v1_nodup = stats.wilson(v1_k, n)

paired_adopt = stats.paired(v2_adopted_bools, v1_adopted_bools)
paired_nodup = stats.paired(v2_no_dup_bools, v1_no_dup_bools)

print(f"v2 Adoption: {sum(v2_adopted_bools)}/{n} ({sum(v2_adopted_bools)/n*100:.1f}%), 95% Wilson: [{w_v2_adopt[0]*100:.1f}%, {w_v2_adopt[1]*100:.1f}%]")
print(f"v1 Adoption: {sum(v1_adopted_bools)}/{n} ({sum(v1_adopted_bools)/n*100:.1f}%), 95% Wilson: [{w_v1_adopt[0]*100:.1f}%, {w_v1_adopt[1]*100:.1f}%]")
print(f"Paired McNemar Adoption: b={paired_adopt['b']}, c={paired_adopt['c']}, p={paired_adopt['p']:.6f}")

print(f"v2 Zero Duplicates: {v2_k}/{n} ({v2_k/n*100:.1f}%), 95% Wilson: [{w_v2_nodup[0]*100:.1f}%, {w_v2_nodup[1]*100:.1f}%]")
print(f"v1 Zero Duplicates: {v1_k}/{n} ({v1_k/n*100:.1f}%), 95% Wilson: [{w_v1_nodup[0]*100:.1f}%, {w_v1_nodup[1]*100:.1f}%]")
print(f"Paired McNemar Zero Duplicates: b={paired_nodup['b']}, c={paired_nodup['c']}, p={paired_nodup['p']:.6f}")

v2_max_overlap = max(r["process_overlap_s"] for r in v2_trials)
v1_max_overlap = max(r["process_overlap_s"] for r in v1_trials)
v1_mean_overlap = sum(r["process_overlap_s"] for r in v1_trials) / n
print(f"v2 Max Overlap: {v2_max_overlap:.1f}s")
print(f"v1 Max Overlap: {v1_max_overlap:.1f}s (mean {v1_mean_overlap:.2f}s)")

print("\n=== Trial B: Worker Kill (3 trials) ===")
b_trials = receipt_bcde["trial_b_worker_kill"]["trials"]
b_k = sum(1 for r in b_trials if r["success"])
b_n = len(b_trials)
w_b = stats.wilson(b_k, b_n)
print(f"Trial B Pass Rate: {b_k}/{b_n} ({b_k/b_n*100:.1f}%), 95% Wilson: [{w_b[0]*100:.1f}%, {w_b[1]*100:.1f}%]")

print("\n=== Attestation Check ===")
served_models = {}
fallback_count = 0
for r in v2_trials:
    for m, cnt in r["served_model"].items():
        served_models[m] = served_models.get(m, 0) + cnt
    fallback_count += r["fallback_events"]

print(f"Total served counts: {served_models}")
print(f"Total fallback events: {fallback_count}")
