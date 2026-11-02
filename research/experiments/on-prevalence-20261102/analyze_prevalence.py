"""Analyze R1-PREVALENCE readings: raw alert rates, Rogan-Gladen calibration, bootstrap CIs, sensitivity."""
from __future__ import annotations

import json
import math
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, "/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train")
from stats import wilson

RECEIPTS_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train/raw/R1-PREVALENCE/receipts")
READINGS_PATH = RECEIPTS_DIR / "readings.jsonl"
BENCH_PERF_PATH = RECEIPTS_DIR / "benchmark_report_perf.json"
RESULTS_JSON = RECEIPTS_DIR / "results.json"

BOOTSTRAP_ROUNDS = 2000
BOOTSTRAP_SEED = 20261102


def rogan_gladen(q: float, recall: float, fa: float) -> float:
    denom = recall - fa
    if abs(denom) < 1e-9:
        return math.nan
    return (q - fa) / denom


def bootstrap_ci(
    k_sample: int,
    n_sample: int,
    k_pos: int,
    n_pos: int,
    k_neg: int,
    n_neg: int,
    rounds: int = BOOTSTRAP_ROUNDS,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float, float]:
    rng = random.Random(seed)

    p_sample = k_sample / n_sample if n_sample else 0.0
    p_rec = k_pos / n_pos if n_pos else 0.0
    p_fa = k_neg / n_neg if n_neg else 0.0

    valid_estimates: list[float] = []
    for _ in range(rounds):
        # Binomial resample
        q_star = sum(rng.random() < p_sample for _ in range(n_sample)) / n_sample
        rec_star = sum(rng.random() < p_rec for _ in range(n_pos)) / n_pos
        fa_star = sum(rng.random() < p_fa for _ in range(n_neg)) / n_neg

        denom = rec_star - fa_star
        if denom > 0.005:  # Valid separating denominator
            p_val = (q_star - fa_star) / denom
            valid_estimates.append(max(0.0, min(1.0, p_val)))

    if not valid_estimates:
        return (math.nan, math.nan, math.nan)

    valid_estimates.sort()
    lo = valid_estimates[int(0.025 * len(valid_estimates))]
    med = valid_estimates[int(0.500 * len(valid_estimates))]
    hi = valid_estimates[int(0.975 * len(valid_estimates))]
    return (med, lo, hi)


def main() -> None:
    readings = [json.loads(line) for line in READINGS_PATH.read_text().splitlines() if line.strip()]
    bench_perf = json.loads(BENCH_PERF_PATH.read_text())

    total_cases = len(readings)
    parse_fails = [r for r in readings if not r["parse_ok"]]
    valid_readings = [r for r in readings if r["parse_ok"]]

    served_models = sorted({tuple(r["served_models"]) for r in readings})
    total_fallbacks = sum(r["n_fallbacks"] for r in readings)

    results: dict[str, Any] = {
        "execution": {
            "total_cases": total_cases,
            "valid_cases": len(valid_readings),
            "parse_failures": len(parse_fails),
            "parse_failure_rate": len(parse_fails) / total_cases if total_cases else 0.0,
            "served_models": [list(sm) for sm in served_models],
            "total_fallbacks": total_fallbacks,
            "reason_codes": dict(Counter(r["reason_code"] for r in readings)),
        },
        "operating_points": {},
    }

    # Analyze for both operating points: t > 0 (fa <= 2%) and t > 30 (fa <= 1%)
    for op_name, op_key, thresh in [("primary_t0", "t_gt_0", 0), ("strict_t30", "t_gt_30", 30)]:
        bench_op = bench_perf["thresholds"][op_key]["overall"]
        k_pos, n_pos = bench_op["pos_k"], bench_op["pos_n"]
        k_neg, n_neg = bench_op["neg_k"], bench_op["neg_n"]
        bench_rec = k_pos / n_pos
        bench_fa = k_neg / n_neg

        # Group valid readings by stratum and corpus
        strata_stats: dict[str, dict[str, Any]] = {}
        strata_names = ["goodfire_kimi", "goodfire_qwen", "goodfire_glm52_fp8", "goodfire_glm52", "hacktrace"]

        for sname in strata_names:
            s_rows = [r for r in valid_readings if r["stratum"] == sname]
            k_alerts = sum(1 for r in s_rows if r["suspicion"] > thresh)
            n_rows = len(s_rows)
            q = k_alerts / n_rows if n_rows else 0.0
            q_ci = list(wilson(k_alerts, n_rows))

            # Check D4 eligibility: Goodfire is publisher_cot_judged, excluded from Rogan-Gladen calibration
            is_cot_judged = sname.startswith("goodfire")
            if is_cot_judged:
                p_hat = math.nan
                p_clamped = math.nan
                b_med, b_lo, b_hi = math.nan, math.nan, math.nan
                status = "excluded_cot_judged_blindness"
            else:
                p_hat = rogan_gladen(q, bench_rec, bench_fa)
                p_clamped = max(0.0, min(1.0, p_hat)) if not math.isnan(p_hat) else math.nan
                b_med, b_lo, b_hi = bootstrap_ci(k_alerts, n_rows, k_pos, n_pos, k_neg, n_neg)
                status = "calibrated"

            strata_stats[sname] = {
                "n": n_rows,
                "alerts": k_alerts,
                "q": q,
                "q_ci95": q_ci,
                "p_hat_raw": p_hat,
                "p_hat_clamped": p_clamped,
                "p_ci95_bootstrap": [b_lo, b_hi],
                "p_bootstrap_median": b_med,
                "status": status,
            }

        # Goodfire pooled
        gf_rows = [r for r in valid_readings if r["corpus"] == "goodfire"]
        gf_alerts = sum(1 for r in gf_rows if r["suspicion"] > thresh)
        gf_q = gf_alerts / len(gf_rows) if gf_rows else 0.0
        gf_q_ci = list(wilson(gf_alerts, len(gf_rows)))

        # Overall pooled
        tot_alerts = sum(1 for r in valid_readings if r["suspicion"] > thresh)
        tot_n = len(valid_readings)
        tot_q = tot_alerts / tot_n if tot_n else 0.0
        tot_q_ci = list(wilson(tot_alerts, tot_n))

        tot_p_hat = rogan_gladen(tot_q, bench_rec, bench_fa)
        tot_p_clamped = max(0.0, min(1.0, tot_p_hat)) if not math.isnan(tot_p_hat) else math.nan
        tot_b_med, tot_b_lo, tot_b_hi = bootstrap_ci(tot_alerts, tot_n, k_pos, n_pos, k_neg, n_neg)

        # Sensitivity analysis on overall pooled:
        rec_lo, rec_hi = bench_op["recall_ci95"]
        fa_lo, fa_hi = bench_op["fa_ci95"]
        sensitivity = {
            "p_at_rec_lo_fa_lo": rogan_gladen(tot_q, rec_lo, fa_lo),
            "p_at_rec_lo_fa_hi": rogan_gladen(tot_q, rec_lo, fa_hi),
            "p_at_rec_hi_fa_lo": rogan_gladen(tot_q, rec_hi, fa_lo),
            "p_at_rec_hi_fa_hi": rogan_gladen(tot_q, rec_hi, fa_hi),
        }

        results["operating_points"][op_name] = {
            "threshold": thresh,
            "benchmark_recall": bench_rec,
            "benchmark_recall_ci95": bench_op["recall_ci95"],
            "benchmark_fa": bench_fa,
            "benchmark_fa_ci95": bench_op["fa_ci95"],
            "strata": strata_stats,
            "goodfire_pooled": {
                "n": len(gf_rows),
                "alerts": gf_alerts,
                "q": gf_q,
                "q_ci95": gf_q_ci,
                "status": "excluded_cot_judged_blindness",
            },
            "overall_pooled": {
                "n": tot_n,
                "alerts": tot_alerts,
                "q": tot_q,
                "q_ci95": tot_q_ci,
                "p_hat_raw": tot_p_hat,
                "p_hat_clamped": tot_p_clamped,
                "p_ci95_bootstrap": [tot_b_lo, tot_b_hi],
                "p_bootstrap_median": tot_b_med,
                "sensitivity_across_ci": sensitivity,
            },
        }

    RESULTS_JSON.write_text(json.dumps(results, indent=2))
    print(f"Results written to {RESULTS_JSON}")

    # Print summary table
    p0 = results["operating_points"]["primary_t0"]
    print("\n" + "=" * 80)
    print("R1-PREVALENCE SUMMARY TABLE (Primary Operating Point: t > 0, FA <= 2%)")
    print("=" * 80)
    print(f"{'Stratum/Pool':22s} | {'n':4s} | {'Alerts':6s} | {'Raw Alert Rate q [95% CI]':28s} | {'Calibrated Prevalence p [95% CI]':32s}")
    print("-" * 105)
    for sname, s in p0["strata"].items():
        q_str = f"{s['q']:.1%} [{s['q_ci95'][0]:.1%}, {s['q_ci95'][1]:.1%}]"
        if s["status"] == "calibrated":
            p_str = f"{s['p_hat_clamped']:.1%} [{s['p_ci95_bootstrap'][0]:.1%}, {s['p_ci95_bootstrap'][1]:.1%}]"
        else:
            p_str = "EXCLUDED (CoT-free blindness)"
        print(f"{sname:22s} | {s['n']:4d} | {s['alerts']:6d} | {q_str:28s} | {p_str:32s}")

    gf = p0["goodfire_pooled"]
    gf_q_str = f"{gf['q']:.1%} [{gf['q_ci95'][0]:.1%}, {gf['q_ci95'][1]:.1%}]"
    print(f"{'Goodfire (Pooled)':22s} | {gf['n']:4d} | {gf['alerts']:6d} | {gf_q_str:28s} | {'EXCLUDED (CoT-free blindness)':32s}")

    tot = p0["overall_pooled"]
    tot_q_str = f"{tot['q']:.1%} [{tot['q_ci95'][0]:.1%}, {tot['q_ci95'][1]:.1%}]"
    tot_p_str = f"{tot['p_hat_clamped']:.1%} [{tot['p_ci95_bootstrap'][0]:.1%}, {tot['p_ci95_bootstrap'][1]:.1%}]"
    print(f"{'TOTAL (Pooled)':22s} | {tot['n']:4d} | {tot['alerts']:6d} | {tot_q_str:28s} | {tot_p_str:32s}")
    print("=" * 80)


if __name__ == "__main__":
    main()
