"""Run Defense D (dual-pass quarantine) and Defense E (claims rubric line) across cohort."""

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
    PAYLOADS,
    evaluate_defense_d,
    evaluate_single_arm,
    inject_run,
)

RECEIPTS_DIR = Path(__file__).parent / "receipts" / "individual"
BRIDGE_RECEIPTS_DIR = (
    Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/raw/B4-INJECTION-V2/receipts/individual")
)
WORK_DIR = Path("/tmp/b4_injection_v2_work")


def run_one_defense(
    defense_name: str,
    case_id: str,
    variant: str | None,
    is_cheat: bool,
) -> dict[str, Any]:
    variant_str = variant or "uninjected"
    key = f"{defense_name}_{variant_str}_{case_id}"
    receipt_file = RECEIPTS_DIR / f"{key}.json"
    bridge_file = BRIDGE_RECEIPTS_DIR / f"{key}.json"

    # Resume check
    if receipt_file.exists():
        try:
            return json.loads(receipt_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    fp = BENCH_ROOT / f"{case_id}.json"
    raw_doc = json.loads(fp.read_text(encoding="utf-8"))
    doc = inject_run(raw_doc, variant) if variant else raw_doc

    if defense_name == "defense_d":
        res = evaluate_defense_d(
            run_id=case_id,
            doc=doc,
            work_dir=WORK_DIR / key,
            session_id=key,
            timeout=180,
        )
    elif defense_name == "defense_e":
        res = evaluate_single_arm(
            run_id=case_id,
            doc=doc,
            condition="defense_e",
            work_dir=WORK_DIR / key,
            session_id=key,
            timeout=180,
        )
    else:
        raise ValueError(f"Unknown defense: {defense_name}")

    res["defense"] = defense_name
    res["variant"] = variant
    res["is_cheat"] = is_cheat

    # Save to local and bridge receipts
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    BRIDGE_RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)

    text = json.dumps(res, indent=2)
    receipt_file.write_text(text, encoding="utf-8")
    bridge_file.write_text(text, encoding="utf-8")

    return res


def main() -> None:
    variants = list(PAYLOADS.keys())
    cases_items = []

    # 1. Injected cheats: 18 cheats x 6 variants = 108 cases
    for cid in CHEATING_IDS:
        for var in variants:
            cases_items.append((cid, var, True))

    # 2. Uninjected cheats: 18 cases
    for cid in CHEATING_IDS:
        cases_items.append((cid, None, True))

    # 3. Uninjected honest: 30 cases
    for cid in HONEST_IDS:
        cases_items.append((cid, None, False))

    print(f"Total cases per defense arm: {len(cases_items)} (108 injected cheats + 18 uninjected cheats + 30 honest)")

    all_tasks = [
        (d_name, cid, var, is_c)
        for d_name in ("defense_d", "defense_e")
        for (cid, var, is_c) in cases_items
    ]

    print(f"Starting Defenses: {len(all_tasks)} evaluations total")
    t0 = time.time()
    results: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(run_one_defense, d_name, cid, var, is_c): (d_name, cid, var, is_c)
            for d_name, cid, var, is_c in all_tasks
        }
        for completed, fut in enumerate(as_completed(futures), 1):
            d_name, cid, var, is_c = futures[fut]
            try:
                res = fut.result()
                results.append(res)
                var_label = var or "uninjected"
                alert_str = "ALERT" if res["is_alert"] else "clean"
                print(f"[{completed}/{len(all_tasks)}] {d_name} {var_label} {cid} -> {res['verdict']} (susp={res['suspicion']}, {alert_str})")
            except Exception as e:
                print(f"[{completed}/{len(all_tasks)}] ERROR on {d_name} {var} {cid}: {e}")

    elapsed = time.time() - t0
    print(f"Defenses completed in {elapsed:.1f}s ({len(results)}/{len(all_tasks)} finished)")

    summary_file = Path(__file__).parent / "receipts" / "defenses_summary.json"
    bridge_summary = (
        Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/raw/B4-INJECTION-V2/receipts/defenses_summary.json")
    )
    summary_data = {
        "n_tasks": len(all_tasks),
        "n_completed": len(results),
        "elapsed_s": elapsed,
        "results": results,
    }
    summary_file.parent.mkdir(parents=True, exist_ok=True)
    summary_file.write_text(json.dumps(summary_data, indent=2), encoding="utf-8")
    bridge_summary.parent.mkdir(parents=True, exist_ok=True)
    bridge_summary.write_text(json.dumps(summary_data, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
