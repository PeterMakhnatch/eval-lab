#!/usr/bin/env python3
"""run_all_killtests.py: Master runner executing all five kill tests across Baseline, Temporal, DBOS, and Dagu."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EXPERIMENT_DIR = Path(__file__).resolve().parent
ADAPTERS_DIR = EXPERIMENT_DIR / "adapters"
PYTHON_BIN = str(EXPERIMENT_DIR / ".venv/bin/python")

sys.path.insert(0, str(ADAPTERS_DIR))
sys.path.insert(0, str(EXPERIMENT_DIR))

from baseline_adapter import BaselineAdapter  # noqa: E402
from dagu_adapter import DaguAdapter  # noqa: E402
from dbos_adapter import DBOSAdapter  # noqa: E402
from temporal_adapter import TemporalAdapter  # noqa: E402

RECEIPTS_DIR_1 = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F9-KILLTEST-RESULTS/receipts")
RECEIPTS_DIR_2 = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F9-KILLTEST-RESULTS")


def save_receipt(name: str, data: Any) -> None:
    text = json.dumps(data, indent=2) if not isinstance(data, str) else data
    for d in (RECEIPTS_DIR_1, RECEIPTS_DIR_2):
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    print(f"Receipt written: {name}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run kill tests across all 4 engines.")
    parser.add_argument("--engines", default="baseline,temporal,dbos,dagu", help="Comma-separated engine list")
    parser.add_argument("--tests", default="kt1,kt2,kt3,kt4,kt5", help="Comma-separated test list")
    parser.add_argument("--trials", type=int, default=10, help="Number of KT1 trials (default: 10)")
    parser.add_argument("--item-seconds", type=float, default=4.0, help="Seconds per item in KT1")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    args = parser.parse_args()

    selected_engines = [e.strip().lower() for e in args.engines.split(",") if e.strip()]
    selected_tests = [t.strip().lower() for t in args.tests.split(",") if t.strip()]

    print(f"Starting Kill Test Suite at {datetime.now(UTC).isoformat()}", flush=True)
    print(f"Engines: {selected_engines}", flush=True)
    print(f"Tests: {selected_tests}", flush=True)

    adapters: dict[str, Any] = {}
    if "baseline" in selected_engines:
        adapters["baseline"] = BaselineAdapter()
    if "temporal" in selected_engines:
        adapters["temporal"] = TemporalAdapter()
    if "dbos" in selected_engines:
        adapters["dbos"] = DBOSAdapter()
    if "dagu" in selected_engines:
        adapters["dagu"] = DaguAdapter()

    all_results: dict[str, dict[str, Any]] = {e: {} for e in selected_engines}

    for engine_name in selected_engines:
        adapter = adapters.get(engine_name)
        if adapter is None:
            continue
        print("\n=======================================================", flush=True)
        print(f"=== Running Engine: {engine_name.upper()} ===", flush=True)
        print("=======================================================", flush=True)

        # KT1
        if "kt1" in selected_tests:
            try:
                res1 = adapter.run_kt1(trials=args.trials, seconds_per_item=args.item_seconds, seed=args.seed)
                all_results[engine_name]["KT1"] = res1
                save_receipt(f"{engine_name}_kt1.json", res1)
            except Exception as exc:
                print(f"ERROR in {engine_name} KT1: {exc}", flush=True)
                all_results[engine_name]["KT1"] = {"verdict": "ERROR", "error": str(exc)}

        # KT2
        if "kt2" in selected_tests:
            try:
                res2 = adapter.run_kt2(seconds_per_item=2.0, downtime_sec=62.0)
                all_results[engine_name]["KT2"] = res2
                save_receipt(f"{engine_name}_kt2.json", res2)
            except Exception as exc:
                print(f"ERROR in {engine_name} KT2: {exc}", flush=True)
                all_results[engine_name]["KT2"] = {"verdict": "ERROR", "error": str(exc)}

        # KT3
        if "kt3" in selected_tests:
            try:
                res3 = adapter.run_kt3(parking_seconds=30.0)
                all_results[engine_name]["KT3"] = res3
                save_receipt(f"{engine_name}_kt3.json", res3)
            except Exception as exc:
                print(f"ERROR in {engine_name} KT3: {exc}", flush=True)
                all_results[engine_name]["KT3"] = {"verdict": "ERROR", "error": str(exc)}

        # KT4
        if "kt4" in selected_tests:
            try:
                res4 = adapter.run_kt4()
                all_results[engine_name]["KT4"] = res4
                save_receipt(f"{engine_name}_kt4.json", res4)
            except Exception as exc:
                print(f"ERROR in {engine_name} KT4: {exc}", flush=True)
                all_results[engine_name]["KT4"] = {"classification": "ERROR", "error": str(exc)}

        # KT5
        if "kt5" in selected_tests:
            try:
                res5 = adapter.run_kt5()
                all_results[engine_name]["KT5"] = res5
                save_receipt(f"{engine_name}_kt5.json", res5)
            except Exception as exc:
                print(f"ERROR in {engine_name} KT5: {exc}", flush=True)
                all_results[engine_name]["KT5"] = {"verdict": "ERROR", "error": str(exc)}

    # Save summary of all results
    summary_data = {
        "timestamp": datetime.now(UTC).isoformat(),
        "engines": selected_engines,
        "results": all_results,
    }
    save_receipt("all_killtests_summary.json", summary_data)
    print("\nAll kill tests completed. Summary saved to receipts.", flush=True)


if __name__ == "__main__":
    main()
