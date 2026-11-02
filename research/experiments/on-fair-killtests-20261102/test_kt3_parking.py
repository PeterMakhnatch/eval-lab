#!/usr/bin/env python3
"""test_kt3_parking.py: Real execution of KT3 (Quota park until T=now+90s with auto-resume).

Evaluates all 5 engines with idiomatic native mechanisms:
- Baseline original: Blind retries (no parking)
- Baseline hardened (R7): Built-in classify_quota() -> parked-quota
- Temporal: Idiomatic ApplicationError(next_retry_delay=timedelta(seconds=wait))
- DBOS: Idiomatic DBOS.sleep(wait)
- Dagu: Idiomatic stepRetryPolicy with interval_sec: 90

Runs in parallel across isolated scratch directories.
"""
from __future__ import annotations

import concurrent.futures
import json
import sys
import time
from pathlib import Path
from typing import Any

EXPERIMENT_DIR = Path(__file__).resolve().parent
ADAPTERS_DIR = EXPERIMENT_DIR / "adapters"
SCRATCH_DIR = EXPERIMENT_DIR / "scratch" / "kt3"
SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ADAPTERS_DIR))
sys.path.insert(0, str(EXPERIMENT_DIR))

from baseline_adapter import BaselineAdapter  # noqa: E402
from dagu_adapter import DaguAdapter  # noqa: E402
from dbos_adapter import DBOSAdapter  # noqa: E402
from temporal_adapter import TemporalAdapter  # noqa: E402


def run_one_engine(engine_name: str, park_seconds: float = 90.0) -> dict[str, Any]:
    print(f"[{engine_name}] Starting KT3 (T = now + {park_seconds}s)...", flush=True)
    if engine_name == "baseline_original":
        ad = BaselineAdapter(mode="original", scratch_dir=SCRATCH_DIR / "baseline_orig")
        res = ad.run_kt3_trial(trial_num=1, parking_seconds=park_seconds)
    elif engine_name == "baseline_hardened":
        ad = BaselineAdapter(mode="hardened", scratch_dir=SCRATCH_DIR / "baseline_hard")
        res = ad.run_kt3_trial(trial_num=1, parking_seconds=park_seconds)
    elif engine_name == "temporal":
        ad = TemporalAdapter(scratch_dir=SCRATCH_DIR / "temporal")
        res = ad.run_kt3(parking_seconds=park_seconds)
    elif engine_name == "dbos":
        ad = DBOSAdapter(scratch_dir=SCRATCH_DIR / "dbos")
        res = ad.run_kt3(parking_seconds=park_seconds)
    elif engine_name == "dagu":
        ad = DaguAdapter(scratch_dir=SCRATCH_DIR / "dagu")
        res = ad.run_kt3(parking_seconds=park_seconds)
    else:
        raise ValueError(f"Unknown engine: {engine_name}")

    print(
        f"[{engine_name}] Finished KT3: passed={res.get('passed')}, "
        f"attempts_before_t={res.get('attempts_before_t')}, "
        f"first_start_delay={res.get('first_start_delay_sec')}s, "
        f"total_wall={res.get('total_wall_sec')}s",
        flush=True,
    )
    return res


def main():
    print("=== Running KT3 (Quota Park until T=now+90s with Auto-Resume) ===")
    park_sec = 90.0
    engines = ["baseline_original", "baseline_hardened", "temporal", "dbos", "dagu"]

    t0 = time.time()
    results = {}
    with concurrent.futures.ProcessPoolExecutor(max_workers=5) as pool:
        future_to_eng = {pool.submit(run_one_engine, eng, park_sec): eng for eng in engines}
        for future in concurrent.futures.as_completed(future_to_eng):
            eng = future_to_eng[future]
            try:
                results[eng] = future.result()
            except Exception as exc:
                print(f"[{eng}] Exception: {exc}", flush=True)
                results[eng] = {"engine": eng, "passed": False, "error": str(exc)}

    out_file = SCRATCH_DIR / "kt3_summary.json"
    out_file.write_text(json.dumps(results, indent=2))
    print(f"\nAll KT3 results saved to {out_file} (total wall time: {round(time.time() - t0, 2)}s)")


if __name__ == "__main__":
    main()
