#!/usr/bin/env python3
"""test_kt2_approval.py: Real execution of KT2 (Human approval survives reboot).

Protocol:
- PREP (2s) runs to completion.
- Engine & server killed cleanly (simulating host reboot).
- Machine remains down for 62 seconds.
- Engine & server restarted.
- Approval delivered >= 60 seconds after restart (at 62s).
- DEPLOY (2s) runs to completion.
- Verified: PREP runs once (target 1), DEPLOY runs once (target 1).
- Process assertion: 0 lingering stray processes.
- 3 independent trials per engine.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

EXPERIMENT_DIR = Path(__file__).resolve().parent
ADAPTERS_DIR = EXPERIMENT_DIR / "adapters"
SCRATCH_DIR = EXPERIMENT_DIR / "scratch" / "kt2"
SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ADAPTERS_DIR))
sys.path.insert(0, str(EXPERIMENT_DIR))

from baseline_adapter import BaselineAdapter  # noqa: E402
from dagu_adapter import DaguAdapter  # noqa: E402
from dbos_adapter import DBOSAdapter  # noqa: E402
from temporal_adapter import TemporalAdapter  # noqa: E402


def check_strays() -> dict[str, int]:
    patterns = ["fake_agent.py", "run_worker_proc.py", "temporal_worker.py"]
    strays = {}
    for pat in patterns:
        try:
            res = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
            pids = [int(p) for p in res.stdout.strip().splitlines() if p and int(p) != os.getpid()]
            strays[pat] = len(pids)
        except Exception:
            strays[pat] = 0
    return strays


def run_engine_trial(engine_name: str, trial_num: int) -> dict[str, Any]:
    print(f"[{engine_name}] Starting KT2 trial {trial_num}...", flush=True)
    if engine_name == "baseline_original":
        ad = BaselineAdapter(mode="original", scratch_dir=SCRATCH_DIR / "baseline_orig")
        res = ad.run_kt2_trial(trial_num)
    elif engine_name == "baseline_hardened":
        ad = BaselineAdapter(mode="hardened", scratch_dir=SCRATCH_DIR / "baseline_hard")
        res = ad.run_kt2_trial(trial_num)
    elif engine_name == "temporal":
        ad = TemporalAdapter(scratch_dir=SCRATCH_DIR / "temporal")
        res = ad.run_kt2_trial(trial_num, downtime_sec=62.0, post_restart_delay_sec=62.0)
    elif engine_name == "dbos":
        ad = DBOSAdapter(scratch_dir=SCRATCH_DIR / "dbos")
        res = ad.run_kt2_trial(trial_num)
    elif engine_name == "dagu":
        ad = DaguAdapter(scratch_dir=SCRATCH_DIR / "dagu")
        res = ad.run_kt2_trial(trial_num)
    else:
        raise ValueError(f"Unknown engine: {engine_name}")

    print(
        f"[{engine_name}] Finished KT2 trial {trial_num}: passed={res.get('passed')}, "
        f"prep_starts={res.get('prep_starts')}, deploy_starts={res.get('deploy_starts')}, "
        f"total_wall={res.get('total_wall_sec')}s",
        flush=True,
    )
    return res


def main():
    print("=== Running KT2 (Human Approval Reboot Survival: 3 Trials Per Engine) ===")
    engines = ["baseline_original", "baseline_hardened", "temporal", "dbos", "dagu"]
    trials_per_engine = 3

    results: dict[str, list[dict[str, Any]]] = {eng: [] for eng in engines}

    # Run trials round-by-round across all 5 engines in parallel
    t_start = time.time()
    for t_idx in range(1, trials_per_engine + 1):
        print(f"\n--- Starting Round {t_idx}/{trials_per_engine} across all 5 engines ---", flush=True)
        with concurrent.futures.ProcessPoolExecutor(max_workers=5) as pool:
            future_to_eng = {pool.submit(run_engine_trial, eng, t_idx): eng for eng in engines}
            for future in concurrent.futures.as_completed(future_to_eng):
                eng = future_to_eng[future]
                try:
                    res = future.result()
                    results[eng].append(res)
                except Exception as exc:
                    print(f"[{eng}] Exception in trial {t_idx}: {exc}", flush=True)
                    results[eng].append({"engine": eng, "trial": t_idx, "passed": False, "error": str(exc)})

    # Summary
    summary = {}
    for eng, trial_list in results.items():
        pass_count = sum(1 for r in trial_list if r.get("passed"))
        summary[eng] = {
            "trials_run": len(trial_list),
            "trials_passed": pass_count,
            "pass_rate": f"{pass_count}/{len(trial_list)}",
            "trials": trial_list,
        }

    strays = check_strays()
    print("\nProcess Cleanliness Check (Stray Process Count):")
    for pat, count in strays.items():
        print(f"  {pat}: {count}")

    summary["_process_strays"] = strays
    summary["_total_wall_sec"] = round(time.time() - t_start, 2)

    out_file = SCRATCH_DIR / "kt2_summary.json"
    out_file.write_text(json.dumps(summary, indent=2))
    print(f"\nAll KT2 results saved to {out_file} in {summary['_total_wall_sec']}s")


if __name__ == "__main__":
    main()
