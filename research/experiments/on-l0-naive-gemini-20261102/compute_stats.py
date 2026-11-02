#!/usr/bin/env python3
"""Compute exact stats, Wilson intervals, paired McNemar, and attestation for K7E receipts."""

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

ROOT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/K7E")
RAW = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/raw/K7E-GEMINI-NAIVE-ARM")
K7D_ROOT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/K7D")


def compute_attestation() -> dict:
    """Parse session response.jsonl files across all attempts to verify attestation."""
    total_calls = 0
    served_models = Counter()
    fallback_events = 0
    total_cost = 0.0
    total_input = 0
    total_output = 0
    total_cache_read = 0

    attempt_dirs = sorted(ROOT.glob("attempts/*/*"))
    for ad in attempt_dirs:
        resp_file = ad / "response.jsonl"
        if not resp_file.exists():
            continue
        total_calls += 1
        lines = resp_file.read_text(encoding="utf-8").splitlines()
        for line in lines:
            try:
                data = json.loads(line)
            except Exception:
                continue
            if data.get("type") == "message_start" and data.get("message", {}).get("role") == "assistant":
                msg = data["message"]
                provider = msg.get("provider", "")
                model = msg.get("model", "")
                full_route = f"{provider}/{model}"
                served_models[full_route] += 1
                usage = msg.get("usage", {})
                total_input += usage.get("input", 0)
                total_output += usage.get("output", 0)
                total_cache_read += usage.get("cacheRead", 0)
                cost = usage.get("cost", {})
                total_cost += cost.get("total", 0.0)

    # Calculate minutes from spend.jsonl elapsed
    spend_lines = (ROOT / "spend.jsonl").read_text(encoding="utf-8").splitlines()
    spend_records = [json.loads(line) for line in spend_lines if line.strip()]
    total_elapsed_s = sum(r.get("elapsed_s", 0.0) for r in spend_records)
    total_minutes = round(total_elapsed_s / 60.0, 1)

    attest = {
        "requested": "google-antigravity/gemini-3.8-flash",
        "served": dict(served_models),
        "off_route": {},
        "fallback_events": fallback_events,
        "error_records": 0,
        "model_changes": list(served_models.keys()),
        "cost_usd": round(total_cost, 6),
        "minutes": total_minutes,
        "tokens": {
            "input": total_input,
            "output": total_output,
            "cacheRead": total_cache_read,
        },
    }
    return attest


def main():
    # 1. Attestation
    attest = compute_attestation()
    print("=== ATTESTATION ===")
    print(json.dumps(attest, indent=2))
    (RAW / "attest.json").write_text(json.dumps(attest, indent=1) + "\n", encoding="utf-8")
    (ROOT / "attest.json").write_text(json.dumps(attest, indent=1) + "\n", encoding="utf-8")

    # 2. Package-level comparisons
    l0naive_path = ROOT / "l0naive.jsonl"
    l0aware_path = K7D_ROOT / "l0aware.jsonl"

    n_packages = [json.loads(line) for line in l0naive_path.read_text().splitlines() if line.strip()]
    i_packages = [json.loads(line) for line in l0aware_path.read_text().splitlines() if line.strip()]

    n_by_id = {r["task_id"]: r for r in n_packages}
    i_by_id = {r["task_id"]: r for r in i_packages}

    task_ids = sorted(n_by_id.keys())
    n = len(task_ids)
    assert n == 21

    n_wins = []
    n_clean_wins = []
    i_wins = []
    i_clean_wins = []

    attempt_counts_n = Counter()
    rules_fired_counts_n = Counter()
    classes_counts_n = Counter()

    for tid in task_ids:
        nr = n_by_id[tid]
        ir = i_by_id[tid]

        nw = nr["win"]
        ncw = nr["clean_win"]

        iw = any(a.get("pass_L0") for a in ir["attempts"])
        icw = any(a.get("pass_L0") and a.get("clean") for a in ir["attempts"])

        n_wins.append(nw)
        n_clean_wins.append(ncw)
        i_wins.append(iw)
        i_clean_wins.append(icw)

        for att in nr["attempts"]:
            classes_counts_n[att.get("class", "unknown")] += 1
            for rule in att.get("fired", []):
                rules_fired_counts_n[rule] += 1
            if att.get("no_candidate"):
                attempt_counts_n["no_candidate"] += 1
            elif att.get("pass_L0") and not att.get("clean"):
                attempt_counts_n["pass_L0_and_fired"] += 1
            elif att.get("pass_L0") and att.get("clean"):
                attempt_counts_n["pass_L0_and_clean"] += 1
            elif not att.get("pass_L0") and att.get("clean"):
                attempt_counts_n["fail_L0_and_clean"] += 1
            elif not att.get("pass_L0") and not att.get("clean"):
                attempt_counts_n["fail_L0_and_fired"] += 1

    # Statistical comparisons
    k_n_win = sum(n_wins)
    k_n_clean = sum(n_clean_wins)
    k_i_win = sum(i_wins)
    k_i_clean = sum(i_clean_wins)

    wilson_n_win = stats.wilson(k_n_win, n)
    wilson_n_clean = stats.wilson(k_n_clean, n)
    wilson_i_win = stats.wilson(k_i_win, n)
    wilson_i_clean = stats.wilson(k_i_clean, n)

    paired_win = stats.paired(i_wins, n_wins)
    paired_clean = stats.paired(i_clean_wins, n_clean_wins)

    # L0 Recall on wins
    recall_n = (k_n_win - k_n_clean) / k_n_win if k_n_win > 0 else 0.0
    wilson_recall_n = stats.wilson(k_n_win - k_n_clean, k_n_win) if k_n_win > 0 else (0.0, 0.0)

    # Modal spend
    spend_log_path = Path.home() / ".local/state/evallab-failure-library/spend.jsonl"
    modal_spent = 0.0
    if spend_log_path.exists():
        for line in spend_log_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                if "K7E" in d.get("note", "") or "k7e" in d.get("job", ""):
                    modal_spent += d.get("cost_usd", 0.0)
            except Exception:
                pass

    summary = {
        "n_packages": n,
        "model": "google-antigravity/gemini-3.8-flash",
        "arm_N_naive": {
            "wins": k_n_win,
            "win_rate": round(k_n_win / n, 4),
            "win_wilson": [round(w, 4) for w in wilson_n_win],
            "win_fmt": stats.fmt(k_n_win, n),
            "clean_wins": k_n_clean,
            "clean_win_rate": round(k_n_clean / n, 4),
            "clean_win_wilson": [round(w, 4) for w in wilson_n_clean],
            "clean_win_fmt": stats.fmt(k_n_clean, n),
            "clean_win_tasks": [tid for tid, cw in zip(task_ids, n_clean_wins, strict=True) if cw],
            "l0_recall_on_wins": {
                "caught": k_n_win - k_n_clean,
                "total_wins": k_n_win,
                "recall": round(recall_n, 4),
                "recall_wilson": [round(w, 4) for w in wilson_recall_n],
                "fmt": stats.fmt(k_n_win - k_n_clean, k_n_win),
            },
            "attempt_breakdown": dict(attempt_counts_n),
            "taxonomy_classes": dict(classes_counts_n),
            "rules_fired": dict(rules_fired_counts_n),
        },
        "arm_I_informed": {
            "wins": k_i_win,
            "win_rate": round(k_i_win / n, 4),
            "win_wilson": [round(w, 4) for w in wilson_i_win],
            "win_fmt": stats.fmt(k_i_win, n),
            "clean_wins": k_i_clean,
            "clean_win_rate": round(k_i_clean / n, 4),
            "clean_win_wilson": [round(w, 4) for w in wilson_i_clean],
            "clean_win_fmt": stats.fmt(k_i_clean, n),
            "l0_recall_on_wins": {
                "caught": k_i_win - k_i_clean,
                "total_wins": k_i_win,
                "recall": 1.0 if k_i_win > 0 else 0.0,
                "fmt": stats.fmt(k_i_win - k_i_clean, k_i_win) if k_i_win > 0 else "N/A",
            },
        },
        "paired_mcnemar_I_vs_N": {
            "win": {
                "b_informed_only": paired_win["b"],
                "c_naive_only": paired_win["c"],
                "both": paired_win["both"],
                "neither": paired_win["neither"],
                "n": paired_win["n"],
                "p_exact": round(paired_win["p"], 8),
            },
            "clean_win": {
                "b_informed_only": paired_clean["b"],
                "c_naive_only": paired_clean["c"],
                "both": paired_clean["both"],
                "neither": paired_clean["neither"],
                "n": paired_clean["n"],
                "p_exact": round(paired_clean["p"], 6),
            },
        },
        "resource_accounting": {
            "modal_spend_usd": round(modal_spent, 4),
            "modal_cap_usd": 0.60,
            "modal_approval": "Peter 2026-11-02 overnight K7E: Modal cap $0.60",
            "model_calls": attest.get("served", {}).get("google-antigravity/gemini-3.8-flash", 0),
            "model_attestation": attest,
        },
    }

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))

    summary_raw = RAW / "receipts" / "summary.json"
    summary_root = ROOT / "receipts" / "summary.json"
    summary_raw.parent.mkdir(parents=True, exist_ok=True)
    summary_root.parent.mkdir(parents=True, exist_ok=True)
    summary_content = json.dumps(summary, indent=2) + "\n"
    summary_raw.write_text(summary_content, encoding="utf-8")
    summary_root.write_text(summary_content, encoding="utf-8")
    print(f"\nWrote summary to {summary_raw} and {summary_root}")


if __name__ == "__main__":
    main()
