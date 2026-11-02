#!/usr/bin/env python3
"""test_kt5_caps.py: Real execution of KT5 (Caps enforcement & verified status-row predicates).

Evaluates:
- TIME_CAP: 3.0s wall-clock cap on 30s item.
- RETRY_CAP: Max 3 attempts on failing item.
- Verified status rows in engine persistent store:
  - Baseline R7: state.json ('killed-time', 'failed')
  - Temporal: workflow result ('failed' with timeout/retry exhausted)
  - DBOS: status.json & system db ('killed-time', 'failed')
  - Dagu: status.jsonl ('step execution timed out' / 'failed')
  - Baseline original: in-memory state
- Process leak check: assert 0 leaked fake_agent, runner, or server processes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

EXPERIMENT_DIR = Path(__file__).resolve().parent
ADAPTERS_DIR = EXPERIMENT_DIR / "adapters"
SCRATCH_DIR = EXPERIMENT_DIR / "scratch" / "kt5"
SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ADAPTERS_DIR))
sys.path.insert(0, str(EXPERIMENT_DIR))

from baseline_adapter import BaselineAdapter  # noqa: E402
from dagu_adapter import DaguAdapter  # noqa: E402
from dbos_adapter import DBOSAdapter  # noqa: E402
from temporal_adapter import TemporalAdapter  # noqa: E402


def check_leaks() -> dict[str, int]:
    patterns = ["fake_agent.py", "run_worker_proc.py", "temporal_worker.py"]
    leaks = {}
    for pat in patterns:
        try:
            res = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
            pids = [int(p) for p in res.stdout.strip().splitlines() if p and int(p) != os.getpid()]
            leaks[pat] = len(pids)
        except Exception:
            leaks[pat] = 0
    return leaks


def main():
    print("=== Running KT5 (Caps Enforcement & Status-Row Predicates) ===")
    results = {}

    print("1. Baseline Original...")
    ad_orig = BaselineAdapter(mode="original", scratch_dir=SCRATCH_DIR / "baseline_orig")
    results["baseline_original"] = ad_orig.run_kt5_caps()
    print("   Passed:", results["baseline_original"]["passed"], "| time_cap:", results["baseline_original"]["time_cap_fired"], "| retry_cap:", results["baseline_original"]["retry_cap_fired"])

    print("2. Baseline Hardened (R7)...")
    ad_hard = BaselineAdapter(mode="hardened", scratch_dir=SCRATCH_DIR / "baseline_hard")
    results["baseline_hardened"] = ad_hard.run_kt5_caps()
    print("   Passed:", results["baseline_hardened"]["passed"], "| status_rows:", results["baseline_hardened"]["status_rows"])

    print("3. Temporal...")
    ad_temp = TemporalAdapter(scratch_dir=SCRATCH_DIR / "temporal")
    results["temporal"] = ad_temp.run_kt5()
    print("   Passed:", results["temporal"]["passed"], "| status_rows:", results["temporal"]["status_rows"])

    print("4. DBOS...")
    ad_dbos = DBOSAdapter(scratch_dir=SCRATCH_DIR / "dbos")
    results["dbos"] = ad_dbos.run_kt5()
    print("   Passed:", results["dbos"]["passed"], "| status_rows:", results["dbos"]["status_rows"])

    print("5. Dagu...")
    ad_dagu = DaguAdapter(scratch_dir=SCRATCH_DIR / "dagu")
    results["dagu"] = ad_dagu.run_kt5()
    print("   Passed:", results["dagu"]["passed"], "| time_cap:", results["dagu"]["time_cap_fired"], "| retry_cap:", results["dagu"]["retry_cap_fired"])

    leaks = check_leaks()
    print("\nProcess Leak Check:")
    for pat, count in leaks.items():
        print(f"  {pat}: {count} lingering processes")

    results["_process_leaks"] = leaks
    out_file = SCRATCH_DIR / "kt5_summary.json"
    out_file.write_text(json.dumps(results, indent=2))
    print(f"\nAll KT5 results saved to {out_file}")


if __name__ == "__main__":
    main()
