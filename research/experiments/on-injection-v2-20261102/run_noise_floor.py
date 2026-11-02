"""Run 5 repeats of uninjected baseline control across all 48 cohort cases."""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# Ensure local library importable
sys.path.insert(0, str(Path(__file__).parent))
from experiment_lib import (
    BENCH_ROOT,
    CHEATING_IDS,
    HONEST_IDS,
    evaluate_single_arm,
)

RECEIPTS_DIR = Path(__file__).parent / "receipts" / "individual"
BRIDGE_RECEIPTS_DIR = (
    Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/raw/B4-INJECTION-V2/receipts/individual")
)
WORK_DIR = Path("/tmp/b4_injection_v2_work")


def run_one(case_id: str, repeat: int) -> dict[str, Any]:
    key = f"control_r{repeat}_{case_id}"
    receipt_file = RECEIPTS_DIR / f"{key}.json"
    bridge_file = BRIDGE_RECEIPTS_DIR / f"{key}.json"

    # Resume check
    if receipt_file.exists():
        try:
            return json.loads(receipt_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    fp = BENCH_ROOT / f"{case_id}.json"
    doc = json.loads(fp.read_text(encoding="utf-8"))

    res = evaluate_single_arm(
        run_id=case_id,
        doc=doc,
        condition="baseline",
        work_dir=WORK_DIR / key,
        session_id=key,
        timeout=180,
    )
    res["repeat"] = repeat
    res["is_cheat"] = case_id in CHEATING_IDS

    # Save to local and bridge receipts
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    BRIDGE_RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)

    text = json.dumps(res, indent=2)
    receipt_file.write_text(text, encoding="utf-8")
    bridge_file.write_text(text, encoding="utf-8")

    return res


def main() -> None:
    cases = CHEATING_IDS + HONEST_IDS
    print(f"Starting Noise Floor: {len(cases)} cases x 5 repeats = {len(cases) * 5} calls")
    t0 = time.time()

    tasks = [(cid, r) for r in range(1, 6) for cid in cases]
    results: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(run_one, cid, r): (cid, r) for cid, r in tasks}
        for completed, fut in enumerate(as_completed(futures), 1):
            cid, r = futures[fut]
            try:
                res = fut.result()
                results.append(res)
                alert_str = "ALERT" if res["is_alert"] else "clean"
                print(f"[{completed}/{len(tasks)}] r{r} {cid} -> {res['verdict']} (susp={res['suspicion']}, {alert_str})")
            except Exception as e:
                print(f"[{completed}/{len(tasks)}] ERROR on r{r} {cid}: {e}")

    elapsed = time.time() - t0
    print(f"Noise floor completed in {elapsed:.1f}s ({len(results)}/{len(tasks)} finished)")

    summary_file = Path(__file__).parent / "receipts" / "noise_floor_summary.json"
    bridge_summary = (
        Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/raw/B4-INJECTION-V2/receipts/noise_floor_summary.json")
    )
    summary_data = {
        "n_cases": len(cases),
        "n_repeats": 5,
        "n_total": len(results),
        "elapsed_s": elapsed,
        "results": results,
    }
    summary_file.parent.mkdir(parents=True, exist_ok=True)
    summary_file.write_text(json.dumps(summary_data, indent=2), encoding="utf-8")
    bridge_summary.parent.mkdir(parents=True, exist_ok=True)
    bridge_summary.write_text(json.dumps(summary_data, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
