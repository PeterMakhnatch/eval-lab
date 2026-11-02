"""Main driver for B3-INJECTION experiment."""

from __future__ import annotations

import concurrent.futures
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from experiment_lib import (
    BENCH_ROOT,
    CHEATING_IDS,
    HONEST_IDS,
    PAYLOADS,
    inject_run,
    run_single_eval,
)

# Output paths
EXPERIMENT_DIR = Path(__file__).resolve().parent
LOCAL_RECEIPTS_DIR = EXPERIMENT_DIR / "receipts"
BRIDGE_RECEIPTS_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/raw/B3-INJECTION/receipts")
EVAL_LAB_RECEIPTS_DIR = Path.home() / "Developer/eval-lab-results/2026-11-02/B3-INJECTION/receipts"


def get_receipt_path(key: str) -> Path:
    return LOCAL_RECEIPTS_DIR / "individual" / f"{key}.json"


def execute_job(job: dict[str, Any], work_root: Path) -> dict[str, Any]:
    key = job["key"]
    rpath = get_receipt_path(key)
    if rpath.is_file():
        try:
            with open(rpath) as fp:
                data = json.load(fp)
            if data.get("returncode") == 0 and data.get("verdict_data") is not None:
                return data
        except Exception:
            pass

    # Load raw doc
    src_id = job["src_id"]
    with open(BENCH_ROOT / f"{src_id}.json") as fp:
        raw_doc = json.load(fp)

    # Apply injection if specified
    family = job.get("family")
    doc = inject_run(raw_doc, family) if family else raw_doc
    condition = job["condition"]
    work_dir = work_root / key

    res = run_single_eval(key, doc, condition, work_dir=work_dir)
    res["key"] = key
    res["src_id"] = src_id
    res["cohort"] = job["cohort"]
    res["family"] = family
    res["timestamp"] = time.time()

    # Save immediately
    rpath.parent.mkdir(parents=True, exist_ok=True)
    with open(rpath, "w") as fp:
        json.dump(res, fp, indent=2)

    return res


def main() -> int:
    LOCAL_RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    (LOCAL_RECEIPTS_DIR / "individual").mkdir(parents=True, exist_ok=True)
    work_root = Path("/tmp/b3-injection-work")
    work_root.mkdir(parents=True, exist_ok=True)

    jobs = []

    # 1. Uninjected Cheats across conditions
    for cid in CHEATING_IDS:
        for cond in ["baseline", "delim_defense", "strip_defense", "metadata_defense"]:
            jobs.append({
                "key": f"{cid}__uninjected__{cond}",
                "src_id": cid,
                "cohort": "cheating",
                "family": None,
                "condition": cond,
            })

    # 2. Uninjected Honest across conditions
    for hid in HONEST_IDS:
        for cond in ["baseline", "delim_defense", "strip_defense", "metadata_defense"]:
            jobs.append({
                "key": f"{hid}__uninjected__{cond}",
                "src_id": hid,
                "cohort": "honest",
                "family": None,
                "condition": cond,
            })

    # 3. Injected Cheats across all 6 families and all 4 conditions
    for cid in CHEATING_IDS:
        for fam in PAYLOADS:
            for cond in ["baseline", "delim_defense", "strip_defense", "metadata_defense"]:
                jobs.append({
                    "key": f"{cid}__{fam}__{cond}",
                    "src_id": cid,
                    "cohort": "cheating",
                    "family": fam,
                    "condition": cond,
                })

    print(f"Total jobs defined: {len(jobs)}")
    # Count cached
    cached = sum(1 for j in jobs if get_receipt_path(j["key"]).is_file())
    print(f"Already cached: {cached}/{len(jobs)}")

    # Run in parallel with max 4 workers (per machine safety rule)
    results = []
    start_time = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        future_to_job = {executor.submit(execute_job, job, work_root): job for job in jobs}
        for done_count, future in enumerate(concurrent.futures.as_completed(future_to_job), 1):
            res = future.result()
            results.append(res)
            if done_count % 10 == 0 or done_count == len(jobs):
                elapsed = time.time() - start_time
                print(f"Progress: {done_count}/{len(jobs)} ({done_count / elapsed:.1f} calls/s, {elapsed:.1f}s)")

    # Aggregate summary
    total_cost = sum(r.get("total_usd", 0.0) for r in results)
    total_tokens = sum(r.get("total_tokens", 0) for r in results)
    fallbacks = sum(r.get("n_fallbacks", 0) for r in results)
    served_models = sorted({m for r in results for m in r.get("served_models", [])})

    summary = {
        "n_total_jobs": len(jobs),
        "total_cost_usd": total_cost,
        "total_tokens": total_tokens,
        "n_fallbacks": fallbacks,
        "served_models": served_models,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    summary_file = LOCAL_RECEIPTS_DIR / "summary.json"
    with open(summary_file, "w") as fp:
        json.dump(summary, fp, indent=2)

    all_results_file = LOCAL_RECEIPTS_DIR / "all_results.json"
    with open(all_results_file, "w") as fp:
        json.dump(results, fp, indent=2)

    # Copy receipts to Bridge and Eval Lab results dirs
    for dest in [BRIDGE_RECEIPTS_DIR, EVAL_LAB_RECEIPTS_DIR]:
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(summary_file, dest / "summary.json")
        shutil.copy2(all_results_file, dest / "all_results.json")
        dest_indiv = dest / "individual"
        dest_indiv.mkdir(parents=True, exist_ok=True)
        for p in (LOCAL_RECEIPTS_DIR / "individual").glob("*.json"):
            shutil.copy2(p, dest_indiv / p.name)

    print(f"Receipts successfully saved to {LOCAL_RECEIPTS_DIR} and synced to bridge & eval-lab-results.")
    print(f"Total cost: ${total_cost:.4f}, Total tokens: {total_tokens}, Fallbacks: {fallbacks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
