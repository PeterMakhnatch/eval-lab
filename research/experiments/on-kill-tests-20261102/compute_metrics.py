#!/usr/bin/env python3
"""compute_metrics.py: Computes exact numbers, Wilson score intervals, and sha256 manifests from kill-test receipts."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

STATS_SCRIPT = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/stats.py")
RECEIPTS_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F9-KILLTEST-RESULTS/receipts")
EXPERIMENT_DIR = Path(__file__).resolve().parent

# Import stats helpers

spec = importlib.util.spec_from_file_location("stats", STATS_SCRIPT)
stats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stats)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def main():
    print("=== Recomputing Kill-Test Metrics from Receipts ===")
    engines = ["baseline", "temporal", "dbos", "dagu"]
    metrics: dict[str, dict] = {}

    for eng in engines:
        metrics[eng] = {}
        # Load KT1
        kt1_file = RECEIPTS_DIR / f"{eng}_kt1.json"
        if kt1_file.exists():
            kt1 = json.loads(kt1_file.read_text())
            trials = kt1.get("trials_run", 10)
            passed = kt1.get("trials_passed", 0)
            raw_execs = kt1.get("total_raw_executions", 0)
            wilson_str = stats.fmt(passed, trials)
            metrics[eng]["KT1"] = {
                "passed": passed,
                "trials": trials,
                "wilson": wilson_str,
                "raw_execs": raw_execs,
                "verdict": kt1.get("verdict"),
            }

        # Load KT2
        kt2_file = RECEIPTS_DIR / f"{eng}_kt2.json"
        if kt2_file.exists():
            kt2 = json.loads(kt2_file.read_text())
            metrics[eng]["KT2"] = {
                "verdict": kt2.get("verdict"),
                "total_time_sec": kt2.get("total_time_sec"),
            }

        # Load KT3
        kt3_file = RECEIPTS_DIR / f"{eng}_kt3.json"
        if kt3_file.exists():
            kt3 = json.loads(kt3_file.read_text())
            metrics[eng]["KT3"] = {
                "verdict": kt3.get("verdict"),
                "attempts_before_t": kt3.get("attempts_before_t"),
                "total_refusals": kt3.get("total_refusals"),
                "tolerance_sec": kt3.get("analysis", {}).get("tolerance_sec"),
            }

        # Load KT4
        kt4_file = RECEIPTS_DIR / f"{eng}_kt4.json"
        if kt4_file.exists():
            kt4 = json.loads(kt4_file.read_text())
            metrics[eng]["KT4"] = {
                "classification": kt4.get("classification"),
            }

        # Load KT5
        kt5_file = RECEIPTS_DIR / f"{eng}_kt5.json"
        if kt5_file.exists():
            kt5 = json.loads(kt5_file.read_text())
            metrics[eng]["KT5"] = {
                "verdict": kt5.get("verdict"),
                "time_cap_fired": kt5.get("time_cap_fired"),
                "retry_cap_fired": kt5.get("retry_cap_fired"),
                "leaked_processes": kt5.get("leaked_processes"),
            }

    print("\n--- Summary Table ---")
    print(f"{'Engine':<12} | {'KT1 Crash':<25} | {'KT2 Approval':<16} | {'KT3 Quota':<20} | {'KT4 Code Change':<30} | {'KT5 Caps'}")
    print("-" * 120)
    for eng in engines:
        m = metrics[eng]
        kt1_str = f"{m.get('KT1', {}).get('wilson', 'N/A')} (raw: {m.get('KT1', {}).get('raw_execs', 0)})"
        kt2_str = m.get("KT2", {}).get("verdict", "N/A")[:15]
        kt3_str = m.get("KT3", {}).get("verdict", "N/A")[:18]
        kt4_str = m.get("KT4", {}).get("classification", "N/A")[:28]
        kt5_str = m.get("KT5", {}).get("verdict", "N/A")
        print(f"{eng:<12} | {kt1_str:<25} | {kt2_str:<16} | {kt3_str:<20} | {kt4_str:<30} | {kt5_str}")

    print("\n--- SHA256 Checksums (receipts & scripts) ---")
    manifest = {}
    for p in sorted(RECEIPTS_DIR.glob("*.json")):
        sha = sha256_file(p)
        manifest[p.name] = {"path": str(p), "sha256": sha, "sha12": sha[:12]}
        print(f"{p.name:<30} {sha[:12]} {p.stat().st_size:>6}B")

    # Experiment files
    for fname in ["pyproject.toml", "fake_agent.py", "ledger_reader.py", "run_all_killtests.py", "compute_metrics.py"]:
        p = EXPERIMENT_DIR / fname
        if p.exists():
            sha = sha256_file(p)
            manifest[fname] = {"path": str(p), "sha256": sha, "sha12": sha[:12]}
            print(f"{fname:<30} {sha[:12]} {p.stat().st_size:>6}B")

    manifest_file = EXPERIMENT_DIR / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2))
    print(f"\nManifest written to {manifest_file}")


if __name__ == "__main__":
    main()
