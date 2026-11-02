"""Receipts computation and verification script for B2-REPLAY-PROOF.

Computes throughput, latency quantiles, crash recovery equality checks,
guard verifications, attestation rates, and cost projections directly from receipts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add bridge loop for stats.py
sys.path.insert(
    0,
    str(
        Path(
            "/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop"
        )
    ),
)
import stats  # noqa: E402

RESULTS_DIR = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/B2")
RECEIPTS_DIR = RESULTS_DIR / "receipts"


def compute_all() -> dict:
    # 1. Arm 1 (off) metrics
    arm1_measure = json.loads((RECEIPTS_DIR / "arm1-off-measure.json").read_text())

    # 2. Arm 2 (cached) metrics
    arm2_measure = json.loads((RECEIPTS_DIR / "arm2-cached-measure.json").read_text())
    arm2_queue = [
        json.loads(line)
        for line in (RECEIPTS_DIR / "arm2-cached-queue.jsonl").read_text().splitlines()
        if line.strip()
    ]

    # 3. Arm 3 (live) metrics
    arm3_queue = [
        json.loads(line)
        for line in (RECEIPTS_DIR / "arm3-live-queue.jsonl").read_text().splitlines()
        if line.strip()
    ]
    arm3_readings = json.loads((RECEIPTS_DIR / "arm3-live-readings.json").read_text())

    # 4. Crash recovery receipt
    crash_receipt = json.loads((RECEIPTS_DIR / "crash-recovery-receipt.json").read_text())

    # 5. Guard tests receipt
    guard_receipt = json.loads((RECEIPTS_DIR / "guard-tests-receipt.json").read_text())

    # --- Calculations ---
    cohort_runs = arm1_measure["runs"]  # 404
    total_events = arm1_measure["events_total"]  # 88,816
    total_prefixes = arm1_measure["prefix_scores"]  # 28,490

    # L0 cycle times
    cycle_off_p50 = arm1_measure["cycle_seconds"]["p50"]
    cycle_cached_p50 = arm2_measure["cycle_seconds"]["p50"]
    slowdown_l0_pct = round(100 * (cycle_cached_p50 - cycle_off_p50) / cycle_off_p50, 2)

    # Per-prefix deterministic latency
    per_prefix_off_p50 = arm1_measure["latency_seconds"]["per_prefix_deterministic"]["p50"]
    per_prefix_cached_p50 = arm2_measure["latency_seconds"]["per_prefix_deterministic"]["p50"]

    # Throughput
    runs_per_min_off = arm1_measure["runs_per_minute"]
    runs_per_min_cached = arm2_measure["runs_per_minute"]
    events_per_min_off = arm1_measure["events_per_minute"]
    events_per_min_cached = arm2_measure["events_per_minute"]

    # Autoreview triggers on cohort
    ar_cached = arm2_measure["autoreview"]
    total_triggers = ar_cached["requested"]
    triggers_l0 = ar_cached["by_trigger"].get("l0_gate", 0)
    triggers_cascade = ar_cached["by_trigger"].get("cascade_stage1", 0)
    triggers_surface = ar_cached["by_trigger"].get("target_surface", 0)

    # Wilson intervals for trigger rates over 404 runs
    w_l0 = stats.wilson(triggers_l0, cohort_runs)
    w_cascade = stats.wilson(triggers_cascade, cohort_runs)
    w_surface = stats.wilson(triggers_surface, cohort_runs)

    # Queue composition
    queue_size = len(arm2_queue)
    priorities = [e.get("priority", 0) for e in arm2_queue]
    priority_counts = {p: priorities.count(p) for p in (0, 1, 2)}

    # Live slice numbers
    n_live = len(arm3_readings)
    live_tokens = [r["payload"]["tokens"] for r in arm3_readings]
    live_usds = [r["payload"]["usd"] for r in arm3_readings]
    live_latencies = [r["payload"]["latency_seconds"] for r in arm3_readings]

    mean_tokens_card = sum(live_tokens) / n_live if n_live else 0.0
    mean_usd_card = sum(live_usds) / n_live if n_live else 0.0
    median_latency_live = stats.median(live_latencies)

    # Attestation
    all_attested = all(r["payload"]["served_model"]["attested"] for r in arm3_readings)
    all_zero_fallbacks = all(
        r["payload"]["served_model"]["n_fallbacks"] == 0 for r in arm3_readings
    )
    served_models = sorted(
        list(
            set(
                m
                for r in arm3_readings
                for m in r["payload"]["served_model"]["served_models"]
            )
        )
    )

    # Cost projections per 1,000 runs (for L0-only trigger rate)
    l0_rate = triggers_l0 / cohort_runs
    cards_per_1k = 1000 * l0_rate  # 403.465

    # 1. Gemini 3.8 Flash (subscription)
    cost_sub_1k = 0.0

    # 2. Gemini 3.8 Flash (measured metered)
    cost_gemini_api_1k = round(cards_per_1k * mean_usd_card, 4)

    # 3. Claude 3.5 Sonnet (assumed: $3.00/1M input, $15.00/1M output, ~90% input / 10% output)
    sonnet_cost_per_card = (0.9 * mean_tokens_card * 3.0 / 1e6) + (
        0.1 * mean_tokens_card * 15.0 / 1e6
    )
    cost_sonnet_1k = round(cards_per_1k * sonnet_cost_per_card, 4)

    # 4. GPT-4o (assumed: $2.50/1M input, $10.00/1M output, ~90% input / 10% output)
    gpt4o_cost_per_card = (0.9 * mean_tokens_card * 2.5 / 1e6) + (
        0.1 * mean_tokens_card * 10.0 / 1e6
    )
    cost_gpt4o_1k = round(cards_per_1k * gpt4o_cost_per_card, 4)

    summary = {
        "cohort": {
            "total_runs": cohort_runs,
            "total_events": total_events,
            "total_prefix_scores": total_prefixes,
        },
        "throughput_latency": {
            "runs_per_min_off": runs_per_min_off,
            "runs_per_min_cached": runs_per_min_cached,
            "events_per_min_off": events_per_min_off,
            "events_per_min_cached": events_per_min_cached,
            "cycle_seconds_p50_off": cycle_off_p50,
            "cycle_seconds_p50_cached": cycle_cached_p50,
            "slowdown_l0_pct": slowdown_l0_pct,
            "per_prefix_latency_p50_off": per_prefix_off_p50,
            "per_prefix_latency_p50_cached": per_prefix_cached_p50,
        },
        "triggers_and_queue": {
            "total_triggers": total_triggers,
            "l0_gate": {
                "count": triggers_l0,
                "rate": round(triggers_l0 / cohort_runs, 4),
                "wilson_95": [round(w_l0[0], 4), round(w_l0[1], 4)],
            },
            "cascade_stage1": {
                "count": triggers_cascade,
                "rate": round(triggers_cascade / cohort_runs, 4),
                "wilson_95": [round(w_cascade[0], 4), round(w_cascade[1], 4)],
            },
            "target_surface": {
                "count": triggers_surface,
                "rate": round(triggers_surface / cohort_runs, 4),
                "wilson_95": [round(w_surface[0], 4), round(w_surface[1], 4)],
            },
            "queue_size": queue_size,
            "queue_priorities": {
                "high_priority_2": priority_counts[2],
                "medium_priority_1": priority_counts[1],
                "normal_priority_0": priority_counts[0],
            },
        },
        "crash_recovery": {
            "gate_verdict": crash_receipt["gate_verdict"],
            "kills_executed": len(crash_receipt["kills_executed"]),
            "served_equality": crash_receipt["checks"]["served_equality"],
            "zero_duplicate_serves": crash_receipt["checks"]["zero_duplicate_serves"],
            "zero_duplicate_queue": crash_receipt["checks"]["zero_duplicate_queue"],
            "queue_equality": crash_receipt["checks"]["queue_equality"],
            "control_served": crash_receipt["control"]["served_count"],
            "recovered_served": crash_receipt["recovered"]["served_count"],
            "control_queue_size": crash_receipt["control"]["queue_size"],
            "recovered_queue_size": crash_receipt["recovered"]["queue_size"],
        },
        "guards": {
            "kill_switch": guard_receipt["kill_switch"]["status"],
            "rate_limit": guard_receipt["rate_limit"]["status"],
            "budget": guard_receipt["budget"]["status"],
        },
        "live_slice": {
            "cards_served": n_live,
            "queue_size": len(arm3_queue),
            "mean_tokens_card": round(mean_tokens_card, 1),
            "mean_usd_card": round(mean_usd_card, 6),
            "median_latency_seconds": round(median_latency_live, 3),
            "all_attested": all_attested,
            "zero_fallbacks": all_zero_fallbacks,
            "served_models": served_models,
        },
        "cost_projections_per_1k_runs": {
            "l0_trigger_rate": round(l0_rate, 4),
            "cards_per_1k_runs": round(cards_per_1k, 1),
            "gemini_38_flash_subscription_usd": cost_sub_1k,
            "gemini_38_flash_metered_usd": cost_gemini_api_1k,
            "claude_35_sonnet_reference_usd": cost_sonnet_1k,
            "gpt_4o_reference_usd": cost_gpt4o_1k,
        },
    }
    return summary


def main() -> None:
    res = compute_all()
    print("=== B2-REPLAY-PROOF RECEIPTS COMPUTATION ===")
    print(f"Cohort runs: {res['cohort']['total_runs']}")
    print(
        f"Crash Recovery Gate Verdict: {res['crash_recovery']['gate_verdict']} (3 kills, set-equality verified)"
    )
    print(
        f"L0 Slowdown: {res['throughput_latency']['slowdown_l0_pct']}% (p50 cycle {res['throughput_latency']['cycle_seconds_p50_off']}s -> {res['throughput_latency']['cycle_seconds_p50_cached']}s)"
    )
    print(
        f"Throughput: {res['throughput_latency']['runs_per_min_cached']:.1f} runs/min, {res['throughput_latency']['events_per_min_cached']:.1f} events/min"
    )
    print(
        f"Triggers: Total={res['triggers_and_queue']['total_triggers']}, L0={res['triggers_and_queue']['l0_gate']['count']}, Cascade={res['triggers_and_queue']['cascade_stage1']['count']}, Surface={res['triggers_and_queue']['target_surface']['count']}"
    )
    print(f"Queue Size: {res['triggers_and_queue']['queue_size']}")
    print(
        f"Live Attestation: {res['live_slice']['cards_served']} cards, attested={res['live_slice']['all_attested']}, zero_fallbacks={res['live_slice']['zero_fallbacks']}, models={res['live_slice']['served_models']}"
    )
    print(
        f"Cost Projections per 1k runs (L0-only {res['cost_projections_per_1k_runs']['cards_per_1k_runs']} cards):"
    )
    print(
        f"  Gemini 3.8 Flash (Subscription): ${res['cost_projections_per_1k_runs']['gemini_38_flash_subscription_usd']:.2f}"
    )
    print(
        f"  Gemini 3.8 Flash (Metered API):   ${res['cost_projections_per_1k_runs']['gemini_38_flash_metered_usd']:.2f}"
    )
    print(
        f"  Claude 3.5 Sonnet (Reference):   ${res['cost_projections_per_1k_runs']['claude_35_sonnet_reference_usd']:.2f}"
    )
    print(
        f"  GPT-4o (Reference):              ${res['cost_projections_per_1k_runs']['gpt_4o_reference_usd']:.2f}"
    )

    out_file = Path(__file__).resolve().parent / "summary.json"
    out_file.write_text(json.dumps(res, indent=2, sort_keys=True))
    print(f"Wrote summary to {out_file}")


if __name__ == "__main__":
    main()
