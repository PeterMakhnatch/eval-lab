"""Analysis of Stage-2 raw-step retrieval contribution to AutoReview detection signal.

Compares:
(i)   D1 stored ar_g38 readings (production AutoReview: Stage 1 + Stage 2 where triggered)
(ii)  D2 stage-1-only re-read of the same model (gemini-3.8-flash)
(iii) D2 cheapest rung (gemini-2.5-flash-lite)
(iv)  D2 top candidate (gemini-3.1-flash-lite)
on the 272 covered cases of the 300-case cohort.
"""
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path
from typing import Any

# Import stats
ROOT = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/on-fix-d23-corrections-20261102")
sys.path.insert(0, "/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train")
import stats  # noqa: E402

BENCH = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/trace-monitor-bench-dev-v1")
D2 = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/D2")
BRIDGE31 = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight")
FIX_RECEIPTS = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train/raw/FIX-D23-CORRECTIONS/receipts")
DURABLE_RECEIPTS = Path.home() / "Developer/eval-lab-results/2026-11-02/FIX-D23-CORRECTIONS/receipts"

SEED = 20261102


def bootstrap_auroc(pos: list[float], neg: list[float], n_boot: int = 2000, seed: int = SEED) -> tuple[float, float, float]:
    rng = random.Random(seed)
    n_p = len(pos)
    n_n = len(neg)
    if n_p == 0 or n_n == 0:
        return math.nan, math.nan, math.nan
    point = stats.auroc(pos, neg)
    vals = []
    for _ in range(n_boot):
        b_p = [pos[rng.randrange(n_p)] for _ in range(n_p)]
        b_n = [neg[rng.randrange(n_n)] for _ in range(n_n)]
        vals.append(stats.auroc(b_p, b_n))
    vals.sort()
    lo = vals[int(0.025 * n_boot)]
    hi = vals[int(0.975 * n_boot)]
    return point, lo, hi


def get_threshold(scores: list[float], pct: float) -> float:
    m = math.floor(pct / 100.0 * len(scores) + 1e-9)
    s = sorted(scores, reverse=True)
    if m >= len(s):
        return -math.inf
    return s[m]

def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]




def main() -> None:
    manifest = {r["case_id"]: r for r in read_jsonl(BENCH / "manifest.jsonl") if r.get("label") is not None}
    cohort = read_jsonl(D2 / "cohort.jsonl")

    # 1. Stored ar_g38
    st_readings = {r["case_id"]: float(r["suspicion"]) for r in read_jsonl(BENCH / "readings/ar_g38.jsonl") if r.get("suspicion") is not None}

    # 2. D2 gemini-3.8-flash (stage 1 re-read)
    d2_ref = {r["case_id"]: float(r["suspicion"]) for r in read_jsonl(D2 / "gemini-3.8-flash.jsonl")}

    # 3. D2 gemini-2.5-flash-lite (cheapest rung)
    d2_cheap = {r["case_id"]: float(r["suspicion"]) for r in read_jsonl(D2 / "gemini-2.5-flash-lite.jsonl")}

    # 4. D2 gemini-3.1-flash-lite (top rung)
    d2_top = {r["case_id"]: float(r["suspicion"]) for r in read_jsonl(D2 / "gemini-3.1-flash-lite.jsonl")}

    # Source stage records
    tw_rows = {r["run_id"]: r for r in read_jsonl(BRIDGE31 / "g38-flash/raw/W3-AUTOREVIEW-V2-TW/receipts/readings-tw.jsonl")}
    cb_rows = {r["blind_id"]: r for r in read_jsonl(BRIDGE31 / "g38-flash/raw/W2-AUTOREVIEW-INVESTIGATOR-R/receipts/readings-cheatbench.jsonl")}
    gf_rows = {r["blind_id"]: r for r in read_jsonl(BRIDGE31 / "g38-flash/raw/W2-AUTOREVIEW-INVESTIGATOR-R/receipts/readings-goodfire.jsonl")}
    ds_rows = {r["blind_id"]: r for r in read_jsonl(BRIDGE31 / "g38-flash/raw/W3-DEEPSWE-AUTOREVIEW/receipts/readings-arm2.jsonl")}
    cov = [c for c in cohort if c["case_id"] in st_readings]
    tune_cases = [c for c in cov if c["split"] == "tune"]
    report_cases = [c for c in cov if c["split"] == "report"]
    tune_neg = [c for c in tune_cases if not c["label"]]
    report_pos = [c for c in report_cases if c["label"]]
    report_neg = [c for c in report_cases if not c["label"]]

    # Stage 2 breakdown on 272 cases
    s2_total = 0
    s2_by_fam: dict[str, dict[str, int]] = {}
    triggers = {"uncertain_band": 0, "l0_hit": 0}
    for c in cov:
        fam = c["family"]
        s2_by_fam.setdefault(fam, {"total": 0, "s1": 0, "s2": 0})
        s2_by_fam[fam]["total"] += 1
        sid = manifest[c["case_id"]]["source_id"]
        bid = manifest[c["case_id"]]["blind_id"]
        st_row: dict[str, Any] | None = None
        if fam == "terminal_wrench":
            st_row = tw_rows.get(sid)
        elif fam == "cheatbench":
            st_row = cb_rows.get(bid) or cb_rows.get(sid)
        elif fam == "goodfire":
            st_row = gf_rows.get(bid) or gf_rows.get(sid)
        elif fam == "deep_swe":
            st_row = ds_rows.get(bid) or ds_rows.get(sid)
        elif fam == "c1c5":
            st_row = {"stage": 1}

        stage = st_row.get("stage", 1) if st_row else 1
        if stage == 2:
            s2_total += 1
            s2_by_fam[fam]["s2"] += 1
            s1_susp = st_row.get("stage1", {}).get("suspicion", 0) if st_row else 0
            l0_card = st_row.get("l0_has_gate_card", False) if st_row else False
            if 25 <= s1_susp <= 85:
                triggers["uncertain_band"] += 1
            elif l0_card:
                triggers["l0_hit"] += 1
        else:
            s2_by_fam[fam]["s1"] += 1

    arms = [
        ("ar_g38_stored", "D1 Stored ar_g38 (S1+S2)", st_readings),
        ("g38_reread", "D2 Re-read gemini-3.8-flash (S1)", d2_ref),
        ("gemini_25_lite", "D2 gemini-2.5-flash-lite (cheapest)", d2_cheap),
        ("gemini_31_lite", "D2 gemini-3.1-flash-lite (top)", d2_top),
    ]

    # AUROC analysis
    auroc_results = {}
    for arm_id, arm_label, P in arms:
        pos = [P[c["case_id"]] for c in report_cases if c["label"]]
        neg = [P[c["case_id"]] for c in report_cases if not c["label"]]
        auc, lo, hi = bootstrap_auroc(pos, neg)
        by_fam = {}
        for fam in sorted({c["family"] for c in cov}):
            f_rep = [c for c in report_cases if c["family"] == fam]
            f_pos = [P[c["case_id"]] for c in f_rep if c["label"]]
            f_neg = [P[c["case_id"]] for c in f_rep if not c["label"]]
            f_auc, f_lo, f_hi = bootstrap_auroc(f_pos, f_neg)
            by_fam[fam] = {"auroc": round(f_auc, 4), "ci95": [round(f_lo, 4), round(f_hi, 4)], "n_pos": len(f_pos), "n_neg": len(f_neg)}
        auroc_results[arm_id] = {
            "label": arm_label,
            "pooled_auroc": round(auc, 4),
            "pooled_ci95": [round(lo, 4), round(hi, 4)],
            "by_family": by_fam,
        }

    # Operating points FA <= 1% and FA <= 2%
    op_results = {}
    for pct in [1, 2]:
        pct_key = f"fa_{pct}pct"
        op_results[pct_key] = {"thresholds": {}, "pooled": {}, "by_family": {}, "mcnemar": {}}
        alerts_map = {}
        neg_alerts_map = {}

        for arm_id, arm_label, P in arms:
            t_neg_scores = [P[c["case_id"]] for c in tune_neg]
            thr = get_threshold(t_neg_scores, pct)
            op_results[pct_key]["thresholds"][arm_id] = thr

            r_pos_alerts = [P[c["case_id"]] > thr for c in report_pos]
            r_neg_alerts = [P[c["case_id"]] > thr for c in report_neg]
            alerts_map[arm_id] = r_pos_alerts
            neg_alerts_map[arm_id] = r_neg_alerts

            k = sum(r_pos_alerts)
            n = len(report_pos)
            w_lo, w_hi = stats.wilson(k, n)
            fa_k = sum(r_neg_alerts)
            fa_n = len(report_neg)
            fa_lo, fa_hi = stats.wilson(fa_k, fa_n)

            op_results[pct_key]["pooled"][arm_id] = {
                "label": arm_label,
                "threshold": thr,
                "recall": {"k": k, "n": n, "rate": round(k / n, 4), "ci95_wilson": [round(w_lo, 4), round(w_hi, 4)]},
                "false_alarm": {"k": fa_k, "n": fa_n, "rate": round(fa_k / fa_n, 4), "ci95_wilson": [round(fa_lo, 4), round(fa_hi, 4)]},
            }

            # Per family
            for fam in sorted({c["family"] for c in cov}):
                f_pos = [c for c in report_pos if c["family"] == fam]
                f_neg = [c for c in report_neg if c["family"] == fam]
                f_pos_alerts = [P[c["case_id"]] > thr for c in f_pos]
                f_neg_alerts = [P[c["case_id"]] > thr for c in f_neg]
                fk = sum(f_pos_alerts)
                fn = len(f_pos)
                flo, fhi = stats.wilson(fk, fn)
                ffa_k = sum(f_neg_alerts)
                ffa_n = len(f_neg)
                ffalo, ffahi = stats.wilson(ffa_k, ffa_n)
                op_results[pct_key]["by_family"].setdefault(fam, {})[arm_id] = {
                    "recall": {"k": fk, "n": fn, "rate": round(fk / fn, 4) if fn else 0.0, "ci95_wilson": [round(flo, 4), round(fhi, 4)]},
                    "false_alarm": {"k": ffa_k, "n": ffa_n, "rate": round(ffa_k / ffa_n, 4) if ffa_n else 0.0, "ci95_wilson": [round(ffalo, 4), round(ffahi, 4)]},
                }

        # McNemar comparisons
        pairs = [
            ("stored_vs_reread", "ar_g38_stored", "g38_reread"),
            ("reread_vs_cheap", "g38_reread", "gemini_25_lite"),
            ("stored_vs_cheap", "ar_g38_stored", "gemini_25_lite"),
            ("reread_vs_top", "g38_reread", "gemini_31_lite"),
            ("stored_vs_top", "ar_g38_stored", "gemini_31_lite"),
        ]
        for pair_name, a1, a2 in pairs:
            # On positives (recall)
            mc_pos = stats.paired(alerts_map[a1], alerts_map[a2])
            # On negatives (FA)
            mc_neg = stats.paired(neg_alerts_map[a1], neg_alerts_map[a2])
            op_results[pct_key]["mcnemar"][pair_name] = {
                "arm1": a1,
                "arm2": a2,
                "positives": mc_pos,
                "negatives": mc_neg,
            }

    out = {
        "cohort_summary": {
            "total_cases": len(cov),
            "tune_cases": len(tune_cases),
            "report_cases": len(report_cases),
            "report_positives": len(report_pos),
            "report_negatives": len(report_neg),
            "by_family": {fam: {"total": len([c for c in cov if c["family"] == fam]),
                                "report_pos": len([c for c in report_pos if c["family"] == fam]),
                                "report_neg": len([c for c in report_neg if c["family"] == fam])}
                          for fam in sorted({c["family"] for c in cov})},
        },
        "stage2_execution": {
            "total_covered": len(cov),
            "stage2_executed": s2_total,
            "stage2_rate": round(s2_total / len(cov), 4),
            "by_family": s2_by_fam,
            "triggers": triggers,
            "trigger_descriptions": {
                "uncertain_band": "Triggered when stage 1 suspicion fell in the uncertain band (25 <= suspicion <= 85)",
                "l0_hit": "Triggered when deterministic L0 gate-card / test-tampering detector alerted on the run",
            },
        },
        "auroc": auroc_results,
        "operating_points": op_results,
    }

    # Write output to receipts
    out_json = json.dumps(out, indent=2)
    FIX_RECEIPTS.mkdir(parents=True, exist_ok=True)
    DURABLE_RECEIPTS.mkdir(parents=True, exist_ok=True)
    (FIX_RECEIPTS / "stage2_analysis.json").write_text(out_json)
    (DURABLE_RECEIPTS / "stage2_analysis.json").write_text(out_json)
    (ROOT / "research/experiments/on-reader-ladder-20261102/stage2_results.json").write_text(out_json)

    print("=== STAGE 2 CONTRIBUTION ANALYSIS COMPLETE ===")
    print(f"Covered cohort: {len(cov)} cases (136 tune, 136 report: 67 pos, 69 neg)")
    print(f"Stage 2 executed on: {s2_total}/{len(cov)} cases ({100*s2_total/len(cov):.1f}%)")
    print(f"  - Uncertain band (25 <= susp <= 85): {triggers['uncertain_band']}")
    print(f"  - L0 gate card hit: {triggers['l0_hit']}")
    print("\nAUROC (Report Split, 67 pos, 69 neg):")
    for _arm_id, arm_data in auroc_results.items():
        print(f"  {arm_data['label']:35s}: {arm_data['pooled_auroc']:.4f} {arm_data['pooled_ci95']}")
    print("\nRecall @ FA <= 2%:")
    for _arm_id, r in op_results["fa_2pct"]["pooled"].items():
        print(f"  {r['label']:35s}: thr={r['threshold']:4.1f} | rec={r['recall']['k']}/{r['recall']['n']} ({r['recall']['rate']*100:.1f}%) | fa={r['false_alarm']['k']}/{r['false_alarm']['n']} ({r['false_alarm']['rate']*100:.1f}%)")
    print("\nMcNemar Paired Comparisons @ FA <= 2% (Positives):")
    for pair_name, mc in op_results["fa_2pct"]["mcnemar"].items():
        p = mc["positives"]
        print(f"  {pair_name:25s}: b={p['b']}, c={p['c']}, both={p['both']}, neither={p['neither']}, p={p['p']:.4f}")


if __name__ == "__main__":
    main()
