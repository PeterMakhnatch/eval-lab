#!/usr/bin/env python3
"""collect_receipts_and_compute.py: Synthesizes receipts, computes Wilson intervals and manifests for F11."""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import sys
from pathlib import Path
from typing import Any

EXPERIMENT_DIR = Path(__file__).resolve().parent
SCRATCH_DIR = EXPERIMENT_DIR / "scratch"
BRIDGE_RECEIPTS_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F11-FAIR-KILLTESTS/receipts")
RESULTS_RECEIPTS_DIR = Path.home() / "Developer/eval-lab-results/2026-11-02/F11-FAIR-KILLTESTS/receipts"

BRIDGE_RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf")))
import stats  # noqa: E402


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def compute_metrics():
    kt1 = json.loads((SCRATCH_DIR / "kt1/kt1_summary.json").read_text())
    kt2 = json.loads((SCRATCH_DIR / "kt2/kt2_summary.json").read_text())
    kt3 = json.loads((SCRATCH_DIR / "kt3/kt3_summary.json").read_text())
    kt4 = json.loads((SCRATCH_DIR / "kt4/kt4_summary.json").read_text())
    kt5 = json.loads((SCRATCH_DIR / "kt5/kt5_summary.json").read_text())
    kt6 = json.loads((SCRATCH_DIR / "kt6/kt6_summary.json").read_text())
    kt7 = json.loads((SCRATCH_DIR / "kt7/kt7_summary.json").read_text())

    engines = ["baseline_original", "baseline_hardened", "temporal", "dbos", "dagu"]
    metrics: dict[str, Any] = {}

    for eng in engines:
        # KT1 Guarded
        g_trials = kt1[eng]["guarded_o1"]
        g_success = sum(1 for t in g_trials if t.get("success"))
        g_wilson = stats.wilson(g_success, len(g_trials))

        g_completed_reexec = [t.get("completed_re_executed", 0) for t in g_trials]
        g_inflight_dup = [t.get("in_flight_duplicated", 0) for t in g_trials]
        g_overlap_sec = [t.get("overlap_seconds", 0.0) for t in g_trials]

        # KT1 Unguarded
        u_trials = kt1[eng]["unguarded_o1"]
        u_success = sum(1 for t in u_trials if t.get("success"))
        u_wilson = stats.wilson(u_success, len(u_trials))
        u_inflight_dup = [t.get("in_flight_duplicated", 0) for t in u_trials]
        u_overlap_sec = [t.get("overlap_seconds", 0.0) for t in u_trials]

        # KT2
        k2_data = kt2.get(eng, {})
        k2_trials = k2_data.get("trials", [])
        k2_pass = k2_data.get("trials_passed", 0)
        k2_wilson = stats.wilson(k2_pass, len(k2_trials)) if k2_trials else (math.nan, math.nan)

        # KT3
        k3_data = kt3.get(eng, {})
        k3_passed = k3_data.get("passed", False)
        k3_attempts_before_t = k3_data.get("attempts_before_t", None)
        k3_delay = k3_data.get("first_start_delay_sec", None)

        # KT4
        k4_data = kt4.get(eng, {})
        k4_behavior = k4_data.get("behavior")
        k4_step_added = k4_data.get("step_added_executed", False) or k4_data.get("step_added_ran", False)

        # KT5
        k5_data = kt5.get(eng, {})
        k5_passed = k5_data.get("passed", False)

        # KT6
        k6_data = kt6.get(eng, {})
        k6_caught = k6_data.get("caught", False)
        k6_elapsed = k6_data.get("elapsed_sec")
        k6_mech = k6_data.get("mechanism")

        metrics[eng] = {
            "kt1": {
                "guarded_success": f"{g_success}/{len(g_trials)}",
                "guarded_wilson_ci": [round(g_wilson[0] * 100, 1), round(g_wilson[1] * 100, 1)],
                "completed_re_executed_sum": sum(g_completed_reexec),
                "in_flight_duplicated_sum": sum(g_inflight_dup),
                "overlap_seconds_max": max(g_overlap_sec) if g_overlap_sec else 0.0,
                "overlap_seconds_mean": round(sum(g_overlap_sec) / len(g_overlap_sec), 2) if g_overlap_sec else 0.0,
                "unguarded_success": f"{u_success}/{len(u_trials)}",
                "unguarded_wilson_ci": [round(u_wilson[0] * 100, 1), round(u_wilson[1] * 100, 1)],
                "unguarded_duplicated_sum": sum(u_inflight_dup),
                "unguarded_overlap_max": max(u_overlap_sec) if u_overlap_sec else 0.0,
            },
            "kt2": {
                "pass_rate": f"{k2_pass}/{len(k2_trials)}",
                "wilson_ci": [round(k2_wilson[0] * 100, 1), round(k2_wilson[1] * 100, 1)],
            },
            "kt3": {
                "passed": k3_passed,
                "attempts_before_t": k3_attempts_before_t,
                "first_start_delay_sec": k3_delay,
            },
            "kt4": {
                "behavior": k4_behavior,
                "step_added_executed": k4_step_added,
                "error_captured": k4_data.get("error_captured"),
            },
            "kt5": {
                "passed": k5_passed,
                "status_rows": k5_data.get("status_rows"),
            },
            "kt6": {
                "caught": k6_caught,
                "elapsed_sec": k6_elapsed,
                "mechanism": k6_mech,
            },
        }
    metrics["_kt7_summary"] = {
        "unlocked_collisions": kt7.get("unlocked_failures", 0),
        "flocked_failures": kt7.get("flocked_failures", 0),
    }

    # Save master synthesized metrics
    out_path = EXPERIMENT_DIR / "all_killtests_summary.json"
    out_path.write_text(json.dumps(metrics, indent=2))
    print(f"Master summary saved to {out_path}")

    # Copy files to receipts
    files_to_copy = [
        (SCRATCH_DIR / "kt1/kt1_summary.json", "kt1_summary.json"),
        (SCRATCH_DIR / "kt2/kt2_summary.json", "kt2_summary.json"),
        (SCRATCH_DIR / "kt3/kt3_summary.json", "kt3_summary.json"),
        (SCRATCH_DIR / "kt4/kt4_summary.json", "kt4_summary.json"),
        (SCRATCH_DIR / "kt5/kt5_summary.json", "kt5_summary.json"),
        (SCRATCH_DIR / "kt6/kt6_summary.json", "kt6_summary.json"),
        (SCRATCH_DIR / "kt7/kt7_summary.json", "kt7_summary.json"),
        (out_path, "all_killtests_summary.json"),
        (EXPERIMENT_DIR / "prereg.md", "prereg.md"),
    ]

    manifest: dict[str, Any] = {}
    for src, name in files_to_copy:
        dst_bridge = BRIDGE_RECEIPTS_DIR / name
        dst_results = RESULTS_RECEIPTS_DIR / name
        shutil.copy2(src, dst_bridge)
        shutil.copy2(src, dst_results)
        sha = sha256_file(src)
        manifest[name] = {
            "sha256": sha,
            "sha12": sha[:12],
            "size_bytes": src.stat().st_size,
            "bridge_path": str(dst_bridge),
            "results_path": str(dst_results),
        }

    manifest_bridge = BRIDGE_RECEIPTS_DIR / "manifest.json"
    manifest_results = RESULTS_RECEIPTS_DIR / "manifest.json"
    manifest_local = EXPERIMENT_DIR / "manifest.json"

    m_json = json.dumps(manifest, indent=2)
    manifest_bridge.write_text(m_json)
    manifest_results.write_text(m_json)
    manifest_local.write_text(m_json)

    print(f"Manifest written to {manifest_local} ({len(manifest)} entries)")
    print(f"Master manifest SHA256: {sha256_file(manifest_local)}")
    return metrics, manifest


if __name__ == "__main__":
    compute_metrics()
