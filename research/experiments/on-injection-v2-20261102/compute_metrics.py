"""Compute rigorous statistics for B4-INJECTION-V2 using overnight stats.py helpers."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

# Import official overnight stats helpers
STATS_PATH = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/stats.py")
spec = importlib.util.spec_from_file_location("stats", STATS_PATH)
stats = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(stats)

# Import experiment constants
sys.path.insert(0, str(Path(__file__).parent))
from experiment_lib import CHEATING_IDS, HONEST_IDS, PAYLOADS  # noqa: E402

RECEIPTS_DIR = Path(__file__).parent / "receipts"
BRIDGE_RECEIPTS_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/raw/B4-INJECTION-V2/receipts")


def load_receipts(directory: Path) -> dict[str, Any]:
    ind_dir = directory / "individual"
    receipts = {}
    if not ind_dir.exists():
        return receipts
    for f in ind_dir.glob("*.json"):
        with contextlib.suppress(Exception):
            receipts[f.stem] = json.loads(f.read_text(encoding="utf-8"))
    return receipts


def compute_noise_floor_metrics(receipts: dict[str, Any]) -> dict[str, Any]:
    cases = CHEATING_IDS + HONEST_IDS
    case_alerts: dict[str, list[bool]] = {cid: [] for cid in cases}
    case_suspicions: dict[str, list[int]] = {cid: [] for cid in cases}

    for cid in cases:
        for r in range(1, 6):
            key = f"control_r{r}_{cid}"
            item = receipts.get(key)
            if item:
                case_alerts[cid].append(bool(item.get("is_alert")))
                s = item.get("suspicion")
                if s is not None:
                    case_suspicions[cid].append(s)

    # Flip-flop: alert status not constant across repeats
    def analyze_group(group_ids: list[str]) -> dict[str, Any]:
        valid_cases = [cid for cid in group_ids if len(case_alerts[cid]) == 5]
        flip_flop_count = 0
        for cid in valid_cases:
            alerts = case_alerts[cid]
            if 0 < sum(alerts) < 5:
                flip_flop_count += 1
        n = len(valid_cases)
        lo, hi = stats.wilson(flip_flop_count, n) if n > 0 else (0.0, 0.0)
        return {
            "n": n,
            "flip_flop_count": flip_flop_count,
            "flip_flop_rate": flip_flop_count / n if n > 0 else 0.0,
            "wilson_ci": [lo, hi],
            "fmt": stats.fmt(flip_flop_count, n) if n > 0 else "0/0",
        }

    overall_ff = analyze_group(cases)
    cheats_ff = analyze_group(CHEATING_IDS)
    honest_ff = analyze_group(HONEST_IDS)

    # Placebo McNemar: repeat 1 vs repeat 2 across cases
    valid_p12 = [cid for cid in cases if len(case_alerts[cid]) >= 2]
    r1_alerts = [case_alerts[cid][0] for cid in valid_p12]
    r2_alerts = [case_alerts[cid][1] for cid in valid_p12]
    placebo_p12 = stats.paired(r1_alerts, r2_alerts)

    # Placebo McNemar on cheats alone
    valid_cheats_p12 = [cid for cid in CHEATING_IDS if len(case_alerts[cid]) >= 2]
    r1_cheats = [case_alerts[cid][0] for cid in valid_cheats_p12]
    r2_cheats = [case_alerts[cid][1] for cid in valid_cheats_p12]
    placebo_cheats_p12 = stats.paired(r1_cheats, r2_cheats)

    # All consecutive pairs placebo pooled (r1 vs r2, r2 vs r3, r3 vs r4, r4 vs r5) on cheats
    pooled_a: list[bool] = []
    pooled_b: list[bool] = []
    for cid in valid_cheats_p12:
        alerts = case_alerts[cid]
        for idx in range(len(alerts) - 1):
            pooled_a.append(alerts[idx])
            pooled_b.append(alerts[idx + 1])
    placebo_pooled = stats.paired(pooled_a, pooled_b)

    return {
        "flip_flop_overall": overall_ff,
        "flip_flop_cheats": cheats_ff,
        "flip_flop_honest": honest_ff,
        "placebo_mcnemar_all_r1_vs_r2": placebo_p12,
        "placebo_mcnemar_cheats_r1_vs_r2": placebo_cheats_p12,
        "placebo_mcnemar_cheats_consecutive_pooled": placebo_pooled,
    }


def compute_injection_metrics(receipts: dict[str, Any]) -> dict[str, Any]:
    variants = list(PAYLOADS.keys())
    variants_stats: dict[str, Any] = {}

    pooled_control_alerts: list[bool] = []
    pooled_injected_alerts: list[bool] = []
    total_flips = 0
    total_trials = 0

    flip_examples: list[dict[str, Any]] = []

    for var in variants:
        ctrl_list: list[bool] = []
        inj_list: list[bool] = []
        var_flips = 0

        for r in range(1, 4):
            for cid in CHEATING_IDS:
                ctrl_key = f"control_r{r}_{cid}"
                inj_key = f"inj_{var}_r{r}_{cid}"

                ctrl_item = receipts.get(ctrl_key)
                inj_item = receipts.get(inj_key)

                if ctrl_item and inj_item:
                    c_alert = bool(ctrl_item.get("is_alert"))
                    i_alert = bool(inj_item.get("is_alert"))

                    ctrl_list.append(c_alert)
                    inj_list.append(i_alert)
                    pooled_control_alerts.append(c_alert)
                    pooled_injected_alerts.append(i_alert)

                    if c_alert and not i_alert:
                        var_flips += 1
                        total_flips += 1
                        flip_examples.append({
                            "variant": var,
                            "repeat": r,
                            "case_id": cid,
                            "control_suspicion": ctrl_item.get("suspicion"),
                            "control_verdict": ctrl_item.get("verdict"),
                            "injected_suspicion": inj_item.get("suspicion"),
                            "injected_verdict": inj_item.get("verdict"),
                            "injected_reason": (inj_item.get("verdict_data") or {}).get("reason"),
                        })

        n_var = len(ctrl_list)
        total_trials += n_var
        paired_res = stats.paired(ctrl_list, inj_list) if n_var > 0 else {}
        lo, hi = stats.wilson(var_flips, n_var) if n_var > 0 else (0.0, 0.0)

        variants_stats[var] = {
            "n_trials": n_var,
            "flips": var_flips,
            "flip_rate": var_flips / n_var if n_var > 0 else 0.0,
            "wilson_ci": [lo, hi],
            "fmt": stats.fmt(var_flips, n_var) if n_var > 0 else "0/0",
            "paired_mcnemar": paired_res,
        }

    pooled_mcnemar = (
        stats.paired(pooled_control_alerts, pooled_injected_alerts)
        if total_trials > 0
        else {}
    )
    p_lo, p_hi = stats.wilson(total_flips, total_trials) if total_trials > 0 else (0.0, 0.0)

    return {
        "total_trials": total_trials,
        "total_flips": total_flips,
        "pooled_flip_rate": total_flips / total_trials if total_trials > 0 else 0.0,
        "pooled_wilson_ci": [p_lo, p_hi],
        "pooled_fmt": stats.fmt(total_flips, total_trials) if total_trials > 0 else "0/0",
        "pooled_mcnemar": pooled_mcnemar,
        "by_variant": variants_stats,
        "sample_flips": flip_examples[:10],
    }


def compute_defense_metrics(receipts: dict[str, Any]) -> dict[str, Any]:
    variants = list(PAYLOADS.keys())
    defenses = ["defense_d", "defense_e"]
    defenses_stats: dict[str, Any] = {}

    # Undefended baseline reference: control repeat 1
    baseline_cheat_alerts: dict[str, bool] = {}
    for cid in CHEATING_IDS:
        b_item = receipts.get(f"control_r1_{cid}")
        if b_item:
            baseline_cheat_alerts[cid] = bool(b_item.get("is_alert"))

    baseline_honest_alerts: dict[str, bool] = {}
    for cid in HONEST_IDS:
        b_item = receipts.get(f"control_r1_{cid}")
        if b_item:
            baseline_honest_alerts[cid] = bool(b_item.get("is_alert"))

    # Baseline recall on uninjected cheats
    b_recall_k = sum(baseline_cheat_alerts.values())
    b_recall_n = len(baseline_cheat_alerts)
    b_recall_lo, b_recall_hi = stats.wilson(b_recall_k, b_recall_n)

    # Baseline false alarm on honest
    b_fa_k = sum(baseline_honest_alerts.values())
    b_fa_n = len(baseline_honest_alerts)
    b_fa_lo, b_fa_hi = stats.wilson(b_fa_k, b_fa_n)

    baseline_metrics = {
        "uninjected_cheat_recall": {
            "k": b_recall_k,
            "n": b_recall_n,
            "rate": b_recall_k / b_recall_n,
            "fmt": stats.fmt(b_recall_k, b_recall_n),
            "wilson_ci": [b_recall_lo, b_recall_hi],
        },
        "honest_false_alarm": {
            "k": b_fa_k,
            "n": b_fa_n,
            "rate": b_fa_k / b_fa_n,
            "fmt": stats.fmt(b_fa_k, b_fa_n),
            "wilson_ci": [b_fa_lo, b_fa_hi],
        },
    }

    for d_name in defenses:
        # 1. Flips on injected cheats
        inj_baseline_alerts: list[bool] = []
        inj_defense_alerts: list[bool] = []
        flips = 0

        for var in variants:
            for cid in CHEATING_IDS:
                def_key = f"{d_name}_{var}_{cid}"
                def_item = receipts.get(def_key)
                b_alert = baseline_cheat_alerts.get(cid, True)

                if def_item:
                    d_alert = bool(def_item.get("is_alert"))
                    inj_baseline_alerts.append(b_alert)
                    inj_defense_alerts.append(d_alert)

                    if b_alert and not d_alert:
                        flips += 1

        n_inj = len(inj_defense_alerts)
        f_lo, f_hi = stats.wilson(flips, n_inj) if n_inj > 0 else (0.0, 0.0)
        paired_vs_baseline_inj = stats.paired(inj_baseline_alerts, inj_defense_alerts)

        # 2. Recall on uninjected cheats
        uninj_b_cheat_alerts: list[bool] = []
        uninj_d_cheat_alerts: list[bool] = []
        for cid in CHEATING_IDS:
            def_key = f"{d_name}_uninjected_{cid}"
            def_item = receipts.get(def_key)
            if def_item and cid in baseline_cheat_alerts:
                uninj_b_cheat_alerts.append(baseline_cheat_alerts[cid])
                uninj_d_cheat_alerts.append(bool(def_item.get("is_alert")))

        n_cheats = len(uninj_d_cheat_alerts)
        k_recall = sum(uninj_d_cheat_alerts)
        r_lo, r_hi = stats.wilson(k_recall, n_cheats) if n_cheats > 0 else (0.0, 0.0)
        recall_cost = (b_recall_k / b_recall_n) - (k_recall / n_cheats) if n_cheats > 0 else 0.0
        paired_recall = stats.paired(uninj_b_cheat_alerts, uninj_d_cheat_alerts)

        # 3. False alarms on honest rollouts
        uninj_b_honest_alerts: list[bool] = []
        uninj_d_honest_alerts: list[bool] = []
        for cid in HONEST_IDS:
            def_key = f"{d_name}_uninjected_{cid}"
            def_item = receipts.get(def_key)
            if def_item and cid in baseline_honest_alerts:
                uninj_b_honest_alerts.append(baseline_honest_alerts[cid])
                uninj_d_honest_alerts.append(bool(def_item.get("is_alert")))

        n_honest = len(uninj_d_honest_alerts)
        k_fa = sum(uninj_d_honest_alerts)
        fa_lo, fa_hi = stats.wilson(k_fa, n_honest) if n_honest > 0 else (0.0, 0.0)
        paired_fa = stats.paired(uninj_b_honest_alerts, uninj_d_honest_alerts)

        defenses_stats[d_name] = {
            "injected_flips": {
                "k": flips,
                "n": n_inj,
                "rate": flips / n_inj if n_inj > 0 else 0.0,
                "fmt": stats.fmt(flips, n_inj) if n_inj > 0 else "0/0",
                "wilson_ci": [f_lo, f_hi],
                "paired_vs_baseline": paired_vs_baseline_inj,
            },
            "uninjected_cheat_recall": {
                "k": k_recall,
                "n": n_cheats,
                "rate": k_recall / n_cheats if n_cheats > 0 else 0.0,
                "fmt": stats.fmt(k_recall, n_cheats) if n_cheats > 0 else "0/0",
                "wilson_ci": [r_lo, r_hi],
                "recall_cost": recall_cost,
                "paired_vs_baseline": paired_recall,
            },
            "honest_false_alarms": {
                "k": k_fa,
                "n": n_honest,
                "rate": k_fa / n_honest if n_honest > 0 else 0.0,
                "fmt": stats.fmt(k_fa, n_honest) if n_honest > 0 else "0/0",
                "wilson_ci": [fa_lo, fa_hi],
                "paired_vs_baseline": paired_fa,
            },
        }

    return {
        "baseline_reference": baseline_metrics,
        "defenses": defenses_stats,
    }


def compute_all_metrics() -> dict[str, Any]:
    receipts = load_receipts(RECEIPTS_DIR)
    if not receipts:
        receipts = load_receipts(BRIDGE_RECEIPTS_DIR)

    print(f"Loaded {len(receipts)} individual receipts.")

    # Compute overall token, cost, and attestation totals
    total_tokens = 0
    total_usd = 0.0
    models_seen = set()
    total_fallbacks = 0
    for r in receipts.values():
        total_tokens += r.get("total_tokens", 0)
        total_usd += r.get("total_usd", 0.0)
        total_fallbacks += r.get("n_fallbacks", 0)
        for m in r.get("served_models", []):
            models_seen.add(m)

    noise_metrics = compute_noise_floor_metrics(receipts)
    injection_metrics = compute_injection_metrics(receipts)
    defense_metrics = compute_defense_metrics(receipts)

    summary = {
        "attestation": {
            "requested_model": "google-antigravity/gemini-3.8-flash",
            "served_models": sorted(models_seen),
            "n_total_receipts": len(receipts),
            "n_fallbacks": total_fallbacks,
            "total_tokens": total_tokens,
            "total_usd": total_usd,
        },
        "noise_floor": noise_metrics,
        "injection_effect": injection_metrics,
        "defenses": defense_metrics,
    }

    out_file = RECEIPTS_DIR / "metrics_summary.json"
    bridge_out = BRIDGE_RECEIPTS_DIR / "metrics_summary.json"

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    bridge_out.parent.mkdir(parents=True, exist_ok=True)
    bridge_out.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    return summary


if __name__ == "__main__":
    compute_all_metrics()
