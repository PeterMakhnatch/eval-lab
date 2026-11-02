#!/usr/bin/env python3
"""test_kt1_suite.py: Standardized, matched event-driven KT1 crash recovery suite.

Matched Protocol:
- 6 items (A1->A2->A3, B1->B2->B3), concurrency 2.
- 20.0s per item.
- Kill trigger: Event-driven when >= 1 item completed AND >= 1 item in flight.
- 10 trials per engine in GUARDED mode.
- 10 trials per engine in UNGUARDED mode.
- Scenario O1: Surviving orphan workers (runner/worker killed with SIGKILL, agents survive).
- Scenario O2: Terminated workers (runner/worker and agents killed simultaneously).
- Ledger-derived metrics:
  - completed_re_executed (target 0)
  - in_flight_duplicated (count)
  - overlap_seconds (concurrent live PID overlap)
  - items_lost (target 0)
  - recovery_wall_sec
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
SCRATCH_DIR = EXPERIMENT_DIR / "scratch" / "kt1"
SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ADAPTERS_DIR))
sys.path.insert(0, str(EXPERIMENT_DIR))

from baseline_adapter import BaselineAdapter  # noqa: E402
from dagu_adapter import DaguAdapter  # noqa: E402
from dbos_adapter import DBOSAdapter  # noqa: E402
from temporal_adapter import TemporalAdapter  # noqa: E402


def run_single_kt1_trial(
    engine_name: str,
    trial_num: int,
    fixture_mode: str,
    orphan_scenario: str,
    item_seconds: float = 20.0,
) -> dict[str, Any]:
    scratch = SCRATCH_DIR / f"{engine_name}_{fixture_mode}_{orphan_scenario}_t{trial_num}"
    if engine_name == "baseline_original":
        ad = BaselineAdapter(mode="original", scratch_dir=scratch)
        res = ad.run_kt1_trial(trial_num, item_seconds=item_seconds, fixture_mode=fixture_mode, orphan_scenario=orphan_scenario)
    elif engine_name == "baseline_hardened":
        ad = BaselineAdapter(mode="hardened", scratch_dir=scratch)
        res = ad.run_kt1_trial(trial_num, item_seconds=item_seconds, fixture_mode=fixture_mode, orphan_scenario=orphan_scenario)
    elif engine_name == "temporal":
        ad = TemporalAdapter(scratch_dir=scratch)
        res = ad.run_kt1(trial_num, item_seconds=item_seconds, fixture_mode=fixture_mode, orphan_scenario=orphan_scenario)
    elif engine_name == "dbos":
        ad = DBOSAdapter(scratch_dir=scratch)
        res = ad.run_kt1(trial_num, item_seconds=item_seconds, fixture_mode=fixture_mode, orphan_scenario=orphan_scenario)
    elif engine_name == "dagu":
        ad = DaguAdapter(scratch_dir=scratch)
        res = ad.run_kt1(trial_num, item_seconds=item_seconds, fixture_mode=fixture_mode, orphan_scenario=orphan_scenario)
    else:
        raise ValueError(f"Unknown engine: {engine_name}")

    res["scratch_dir"] = str(scratch)
    return res


def run_engine_batch(
    engine_name: str,
    fixture_mode: str = "guarded",
    orphan_scenario: str = "O1",
    num_trials: int = 10,
    item_seconds: float = 20.0,
    max_workers: int = 4,
) -> list[dict[str, Any]]:
    print(f"\n=== [{engine_name}] Starting KT1: mode={fixture_mode}, orphan={orphan_scenario} ({num_trials} trials, item_sec={item_seconds}s) ===", flush=True)
    t0 = time.time()
    results = []

    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(run_single_kt1_trial, engine_name, i, fixture_mode, orphan_scenario, item_seconds)
            for i in range(1, num_trials + 1)
        ]
        for f in concurrent.futures.as_completed(futures):
            try:
                res = f.result()
                results.append(res)
                print(
                    f"  Trial {res['trial']}: completed_re_executed={res['completed_re_executed']}, "
                    f"in_flight_duplicated={res['in_flight_duplicated']}, "
                    f"overlap_sec={res['overlap_seconds']}s, "
                    f"items_lost={len(res['items_lost'])}, "
                    f"wall={res['total_wall_sec']}s",
                    flush=True,
                )
            except Exception as exc:
                print(f"  Trial failed with exception: {exc}", flush=True)

    results.sort(key=lambda r: r.get("trial", 0))
    print(f"=== [{engine_name}] Finished {len(results)} trials in {round(time.time() - t0, 1)}s ===", flush=True)
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", default="all", help="Engine to run (or 'all')")
    parser.add_argument("--workers", type=int, default=4, help="Concurrency workers")
    args = parser.parse_args()

    all_engines = ["baseline_original", "baseline_hardened", "temporal", "dbos", "dagu"]
    engines = [args.engine] if args.engine != "all" else all_engines

    out_file = SCRATCH_DIR / "kt1_summary.json"
    if out_file.exists():
        try:
            all_summary = json.loads(out_file.read_text())
        except Exception:
            all_summary = {}
    else:
        all_summary = {}

    for eng in engines:
        # 1. Primary Matched KT1: GUARDED mode, Scenario O1 (Surviving Orphans)
        guarded_o1 = run_engine_batch(eng, fixture_mode="guarded", orphan_scenario="O1", num_trials=10, item_seconds=20.0, max_workers=args.workers)

        # 2. UNGUARDED mode: Scenario O1 (Detecting duplicate side-effects)
        unguarded_o1 = run_engine_batch(eng, fixture_mode="unguarded", orphan_scenario="O1", num_trials=10, item_seconds=20.0, max_workers=args.workers)

        # 3. Scenario O2: Variant where agents die with worker (3 trials)
        guarded_o2 = run_engine_batch(eng, fixture_mode="guarded", orphan_scenario="O2", num_trials=3, item_seconds=20.0, max_workers=args.workers)

        all_summary[eng] = {
            "guarded_o1": guarded_o1,
            "unguarded_o1": unguarded_o1,
            "guarded_o2": guarded_o2,
        }
        out_file.write_text(json.dumps(all_summary, indent=2))
        print(f"[{eng}] Summary saved to {out_file}", flush=True)

    print("\nAll specified KT1 runs completed!")

if __name__ == "__main__":
    main()
