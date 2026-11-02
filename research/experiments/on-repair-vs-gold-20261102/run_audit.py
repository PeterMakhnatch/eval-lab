#!/usr/bin/env python3
"""R3-REPAIR-VS-GOLD: Audit repaired MiMo graders on identical AST mutant seeds.

Paired comparison of original vs repaired graders on identical mutants.
Uses ModalBackend via evallab.modal_cells with ReserveSettleSpendGate.
Budget: Modal cap $0.80.
Approval: "Peter 2026-11-02 overnight R3: Modal cap $0.80".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))

CERT_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert")
sys.path.insert(0, str(CERT_DIR))
import stats  # noqa: E402

from evallab.modal_cells import (  # noqa: E402
    ReserveSettleSpendGate,
    SandboxMeter,
    modal_cell_runner,
)
from evallab.mutant_auditor import (  # noqa: E402
    APPLIED_MARKER,
    Mutant,
    _workdir,
    mutant_script,
    mutants_for_patch,
    parse_probe,
)

CENSUS_PATH = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/gates/census.json")
GOLD_MANIFEST_PATH = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/gates/gold-v1-manifest.json")
RESULTS_20261024 = Path.home() / "Developer/eval-lab-results/2026-10-24/mimo-task-quality-30-20261024"
VERIFICATION_RECEIPT = RESULTS_20261024 / "evidence/verification-receipt.json"
REPAIRED_ROOT = RESULTS_20261024 / "evidence/packages"
A2_TASKS_DIR = Path.home() / "Developer/eval-lab-results/2026-11-02/A2/tasks"

EXP_DIR = HERE
RAW_RECEIPTS_DIR = CERT_DIR / "raw/R3-REPAIR-VS-GOLD/receipts"
DURABLE_DIR = Path.home() / "Developer/eval-lab-results/2026-11-02/R3-REPAIR-VS-GOLD"

STATE_DIR = Path.home() / ".local/state/evallab-failure-library"
LEDGER_PATH = STATE_DIR / "on-r3-repair-vs-gold-20261102-ledger.json"
SPEND_LOG_PATH = STATE_DIR / "spend.jsonl"
APPROVAL = "Peter 2026-11-02 overnight R3: Modal cap $0.80"
BILLING_TAG = "on-r3-repair-vs-gold-20261102"
CAP_USD = 0.80


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_repaired_mapping() -> dict[str, Path]:
    """Map task_id -> qualified repaired package directory."""
    with open(VERIFICATION_RECEIPT) as f:
        receipt = json.load(f)
    prefix_old = "/Users/petermakhnatch/Developer/eval-lab/.worktrees/mimo-batch-verifier-20261024/runs/mimo-batch-verifier30-20261024/packages"
    mapping = {}
    for t in receipt["tasks"]:
        tid = t["task_id"]
        rel = t["repaired_package"].replace(prefix_old + "/", "")
        pkg_dir = REPAIRED_ROOT / rel
        if pkg_dir.is_dir() and (pkg_dir / "task.toml").is_file():
            mapping[tid] = pkg_dir
        else:
            raise FileNotFoundError(f"Missing qualified package for {tid} at {pkg_dir}")
    return mapping


def load_audit_tasks(repaired_map: dict[str, Path]) -> list[dict[str, Any]]:
    """Load tasks from census.json that are audited with mutants and exist in repaired-30."""
    with open(CENSUS_PATH) as f:
        census = json.load(f)

    tasks = []
    for t in census["tasks"]:
        tid = t["task_id"]
        if tid in repaired_map and t.get("control_status") == "passed" and t.get("mutants_generated_count", 0) > 0:
            tasks.append(t)
    tasks.sort(key=lambda x: x["task_id"])
    return tasks


def run_repaired_cell(
    *,
    runner: Any,
    cell_dir: Path,
    repaired_pkg: Path,
    patch: str,
    mutant: Mutant,
    label: str,
) -> dict[str, Any]:
    """Execute one mutant cell against the repaired package."""
    script = mutant_script(
        workdir=_workdir(repaired_pkg),
        reference_patch=patch,
        mutant=mutant,
    )
    result = runner(cell_dir, repaired_pkg, script)
    applied = mutant.operator in result.get("agent_log", "") and (
        f"{APPLIED_MARKER} {mutant.operator} {mutant.path}:{mutant.line} {mutant.sha256}"
        in result.get("agent_log", "")
    )
    return {
        "cell": label,
        "operator": mutant.operator,
        "line": mutant.line,
        "path": mutant.path,
        "sha256": mutant.sha256,
        "applied": applied,
        "reward": result.get("reward"),
        "status": result.get("status"),
        "reason": result.get("reason"),
        "wall_s": result.get("wall_s"),
        "est_usd": result.get("est_usd"),
        "evidence_sha256": result.get("evidence_sha256"),
        "cache_hit": result.get("cache_hit", False),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit repaired graders on identical AST mutants")
    parser.add_argument("--workers", type=int, default=5, help="Modal concurrency (default 5)")
    parser.add_argument("--dry-run", action="store_true", help="Print plan and exit")
    args = parser.parse_args()

    repaired_map = load_repaired_mapping()
    tasks = load_audit_tasks(repaired_map)
    print(f"Loaded {len(tasks)} eligible audited tasks with repaired packages.")

    # Prepare cell plans
    plan = []
    for t in tasks:
        tid = t["task_id"]
        a2_dir = A2_TASKS_DIR / tid
        probe_res = json.loads((a2_dir / "probe/result.json").read_text())
        sources = parse_probe(probe_res["agent_log"])
        patch = (REPO / t["reference_patch_path"]).read_text()
        mutants = mutants_for_patch(patch, sources, limit=3)
        rep_pkg = repaired_map[tid]

        # Verify mutant alignment with A2 census
        orig_mutants_info = t["mutants"]
        assert len(mutants) == len(orig_mutants_info), f"Mutant count mismatch on {tid}"

        for idx, m in enumerate(mutants):
            orig_m = orig_mutants_info[idx]
            assert m.operator == orig_m["operator"] and m.line == orig_m["line"], f"Mutant mismatch on {tid} #{idx}"
            label = f"repaired-m{idx}-{m.operator}-L{m.line}"
            plan.append({
                "task_id": tid,
                "repaired_pkg": rep_pkg,
                "patch": patch,
                "mutant": m,
                "orig_info": orig_m,
                "label": label,
                "out_dir": DURABLE_DIR / "tasks" / tid / label,
            })

    print(f"Total planned mutant executions on repaired packages: {len(plan)}")
    if args.dry_run:
        return 0

    DURABLE_DIR.mkdir(parents=True, exist_ok=True)
    RAW_RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)

    gate = ReserveSettleSpendGate(
        approval=APPROVAL,
        cap_usd=CAP_USD,
        spend_ledger=LEDGER_PATH,
        spend_log=SPEND_LOG_PATH,
        billing_tag=BILLING_TAG,
    )
    meter = SandboxMeter(CAP_USD, gate=gate)
    runner = modal_cell_runner(
        repo_root=REPO,
        spend_gate=gate,
        meter=meter,
        timeout_seconds=240,
    )

    results_by_task: dict[str, list[dict[str, Any]]] = {t["task_id"]: [] for t in tasks}
    started_at = time.time()

    print(f"\nStarting execution of {len(plan)} cells across {args.workers} workers...")
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {
            executor.submit(
                run_repaired_cell,
                runner=runner,
                cell_dir=item["out_dir"],
                repaired_pkg=item["repaired_pkg"],
                patch=item["patch"],
                mutant=item["mutant"],
                label=item["label"],
            ): item
            for item in plan
        }

        for completed, fut in enumerate(as_completed(future_map), 1):
            item = future_map[fut]
            tid = item["task_id"]
            lbl = item["label"]
            try:
                cell_res = fut.result()
                cell_res["orig_reward"] = item["orig_info"]["reward"]
                cell_res["orig_applied"] = item["orig_info"]["applied"]
                results_by_task[tid].append(cell_res)
                print(
                    f"[{completed}/{len(plan)}] {tid} {lbl} -> "
                    f"reward={cell_res['reward']} (orig={item['orig_info']['reward']}) "
                    f"wall={cell_res['wall_s']}s cost=${cell_res['est_usd']:.4f} "
                    f"[Gate settled: ${gate.settled_usd:.4f}]",
                    flush=True,
                )
            except Exception as exc:
                print(f"[{completed}/{len(plan)}] ERROR on {tid} {lbl}: {exc}", flush=True)
                results_by_task[tid].append({
                    "cell": lbl,
                    "operator": item["mutant"].operator,
                    "line": item["mutant"].line,
                    "path": item["mutant"].path,
                    "sha256": item["mutant"].sha256,
                    "applied": False,
                    "reward": None,
                    "status": "error",
                    "reason": str(exc),
                    "orig_reward": item["orig_info"]["reward"],
                    "orig_applied": item["orig_info"]["applied"],
                })

    elapsed_s = time.time() - started_at
    print(f"\nAll cells finished in {elapsed_s:.1f}s.")
    print(f"Spend gate summary: {gate.summary()}")
    print(f"Meter summary: spent=${meter.spent:.4f}, seconds={meter.seconds:.1f}, cells={meter.cells}")

    # Compile paired results per task
    task_summaries = []
    paired_orig_weak = []
    paired_rep_weak = []
    mutant_orig_surv = []
    mutant_rep_surv = []

    degraded_tasks = []

    for t in tasks:
        tid = t["task_id"]
        rep_cells = sorted(results_by_task[tid], key=lambda x: (x["path"], x["line"]))
        
        orig_survivors = [m for m in t["mutants"] if m["applied"] and m["reward"] == 1.0]
        rep_survivors = [c for c in rep_cells if c["applied"] and c["reward"] == 1.0]

        is_orig_weak = bool(orig_survivors)
        is_rep_weak = bool(rep_survivors)

        paired_orig_weak.append(is_orig_weak)
        paired_rep_weak.append(is_rep_weak)

        # Check for degradation: mutant killed by orig but accepted by rep
        task_degraded = False
        degraded_reasons = []
        for c in rep_cells:
            m_orig_surv = (c["orig_applied"] and c["orig_reward"] == 1.0)
            m_rep_surv = (c["applied"] and c["reward"] == 1.0)
            mutant_orig_surv.append(m_orig_surv)
            mutant_rep_surv.append(m_rep_surv)

            if not m_orig_surv and m_rep_surv:
                task_degraded = True
                degraded_reasons.append(
                    f"Mutant {c['operator']} at {c['path']}:{c['line']} killed by original (0.0) but survived on repaired (1.0)"
                )

        if len(rep_survivors) > len(orig_survivors):
            task_degraded = True
            degraded_reasons.append(
                f"Repaired grader has more survivors ({len(rep_survivors)}) than original ({len(orig_survivors)})"
            )

        if task_degraded:
            degraded_tasks.append({
                "task_id": tid,
                "reasons": degraded_reasons,
                "orig_survivors_count": len(orig_survivors),
                "rep_survivors_count": len(rep_survivors),
            })

        task_summaries.append({
            "task_id": tid,
            "runner": t.get("runner"),
            "repo": t.get("repo"),
            "repaired_package": str(repaired_map[tid]),
            "mutants_count": len(rep_cells),
            "orig_weak": is_orig_weak,
            "rep_weak": is_rep_weak,
            "orig_survivors_count": len(orig_survivors),
            "rep_survivors_count": len(rep_survivors),
            "orig_survivors": [m["cell"] for m in orig_survivors],
            "rep_survivors": [c["cell"] for c in rep_survivors],
            "degraded": task_degraded,
            "degraded_reasons": degraded_reasons,
            "cells": rep_cells,
        })

    # Statistical tests using stats.paired
    task_test = stats.paired(paired_orig_weak, paired_rep_weak)
    mutant_test = stats.paired(mutant_orig_surv, mutant_rep_surv)

    n_tasks = len(tasks)
    orig_weak_count = sum(paired_orig_weak)
    rep_weak_count = sum(paired_rep_weak)

    orig_weak_ci = stats.wilson(orig_weak_count, n_tasks)
    rep_weak_ci = stats.wilson(rep_weak_count, n_tasks)

    tot_mutants = len(mutant_orig_surv)
    orig_mut_surv_count = sum(mutant_orig_surv)
    rep_mut_surv_count = sum(mutant_rep_surv)

    orig_mut_ci = stats.wilson(orig_mut_surv_count, tot_mutants)
    rep_mut_ci = stats.wilson(rep_mut_surv_count, tot_mutants)

    # 2x2 table: Repair-Qualified vs Gold v1 Manifest
    with open(GOLD_MANIFEST_PATH) as f:
        gold_manifest = json.load(f)
    gold_tasks_map = {t["task_id"]: t for t in gold_manifest.get("tasks", [])}

    with open(CENSUS_PATH) as f:
        census = json.load(f)

    # All 27 overlap tasks
    all_overlap_tids = sorted(repaired_map.keys() & set(t["task_id"] for t in census["tasks"]))
    overlap_2x2 = {
        "repair_qual_and_gold": 0,
        "repair_qual_and_not_gold": 0,
        "not_repair_qual_and_gold": 0,
        "not_repair_qual_and_not_gold": 0,
    }
    gold_reasons_counter: dict[str, int] = {}
    for tid in all_overlap_tids:
        gold_info = gold_tasks_map.get(tid, {})
        is_gold = gold_info.get("status") in ("gold_structural", "gold_heldout")
        reason = gold_info.get("reason", "unknown")
        gold_reasons_counter[reason] = gold_reasons_counter.get(reason, 0) + 1
        if is_gold:
            overlap_2x2["repair_qual_and_gold"] += 1
        else:
            overlap_2x2["repair_qual_and_not_gold"] += 1

    summary_data = {
        "created_at": datetime.now(UTC).isoformat(),
        "approval": APPROVAL,
        "cap_usd": CAP_USD,
        "meter_settled_usd": gate.settled_usd,
        "meter_sandbox_seconds": meter.seconds,
        "cells_executed": len(plan),
        "cohort": {
            "repaired_30_total": 30,
            "census_total": len(census["tasks"]),
            "overlap_total": len(all_overlap_tids),
            "audited_paired_tasks": n_tasks,
            "total_paired_mutants": tot_mutants,
        },
        "task_level_paired": {
            "n": n_tasks,
            "orig_weak_count": orig_weak_count,
            "orig_weak_rate": round(orig_weak_count / n_tasks, 4),
            "orig_weak_ci_95": [round(orig_weak_ci[0], 4), round(orig_weak_ci[1], 4)],
            "rep_weak_count": rep_weak_count,
            "rep_weak_rate": round(rep_weak_count / n_tasks, 4),
            "rep_weak_ci_95": [round(rep_weak_ci[0], 4), round(rep_weak_ci[1], 4)],
            "both_weak": task_test["both"],
            "neither_weak": task_test["neither"],
            "b_repair_fixed": task_test["b"],  # orig weak, rep robust
            "c_repair_degraded": task_test["c"],  # orig robust, rep weak
            "mcnemar_p": task_test["p"],
        },
        "mutant_level_paired": {
            "n": tot_mutants,
            "orig_survivors_count": orig_mut_surv_count,
            "orig_survivor_rate": round(orig_mut_surv_count / tot_mutants, 4),
            "orig_survivor_ci_95": [round(orig_mut_ci[0], 4), round(orig_mut_ci[1], 4)],
            "rep_survivors_count": rep_mut_surv_count,
            "rep_survivor_rate": round(rep_mut_surv_count / tot_mutants, 4),
            "rep_survivor_ci_95": [round(rep_mut_ci[0], 4), round(rep_mut_ci[1], 4)],
            "both_survived": mutant_test["both"],
            "neither_survived": mutant_test["neither"],
            "b_killed_by_repair": mutant_test["b"],
            "c_accepted_by_repair_only": mutant_test["c"],
            "mcnemar_p": mutant_test["p"],
        },
        "gold_overlap_2x2": {
            "cohort_size": len(all_overlap_tids),
            "table": overlap_2x2,
            "gold_reasons": gold_reasons_counter,
        },
        "degraded_tasks_count": len(degraded_tasks),
        "degraded_tasks": degraded_tasks,
        "tasks": task_summaries,
    }

    # Save to disk across required locations
    # 1. repair_vs_gold.json (full summary)
    for dest_dir in (EXP_DIR, RAW_RECEIPTS_DIR, DURABLE_DIR):
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "repair_vs_gold.json").write_text(json.dumps(summary_data, indent=2))
        (dest_dir / "summary.json").write_text(json.dumps(summary_data, indent=2))

    # 2. paired_comparison.json (clean paired data for stats verification)
    paired_data = {
        "created_at": summary_data["created_at"],
        "cohort_size": n_tasks,
        "task_level": summary_data["task_level_paired"],
        "mutant_level": summary_data["mutant_level_paired"],
        "degraded_tasks_count": len(degraded_tasks),
        "degraded_tasks": degraded_tasks,
        "pairs": [
            {
                "task_id": ts["task_id"],
                "orig_weak": ts["orig_weak"],
                "rep_weak": ts["rep_weak"],
                "orig_survivors_count": ts["orig_survivors_count"],
                "rep_survivors_count": ts["rep_survivors_count"],
                "degraded": ts["degraded"],
            }
            for ts in task_summaries
        ],
    }
    for dest_dir in (EXP_DIR, RAW_RECEIPTS_DIR, DURABLE_DIR):
        (dest_dir / "paired_comparison.json").write_text(json.dumps(paired_data, indent=2))

    # 3. manifest_overlap.json (2x2 table and breakdown of overlap tasks)
    overlap_data = {
        "created_at": summary_data["created_at"],
        "cohort_overlap_size": len(all_overlap_tids),
        "contingency_table_2x2": overlap_2x2,
        "gold_certified_status_reasons": gold_reasons_counter,
        "overlap_tasks": [
            {
                "task_id": tid,
                "repair_qualified": True,
                "gold_certified": gold_tasks_map.get(tid, {}).get("status") in ("gold_structural", "gold_heldout"),
                "gold_certified_status": gold_tasks_map.get(tid, {}).get("status", "unknown"),
                "gold_taxonomy_code": gold_tasks_map.get(tid, {}).get("reason", "unknown"),
            }
            for tid in all_overlap_tids
        ],
    }
    for dest_dir in (EXP_DIR, RAW_RECEIPTS_DIR, DURABLE_DIR):
        (dest_dir / "manifest_overlap.json").write_text(json.dumps(overlap_data, indent=2))

    # Also copy individual task json receipts
    for ts in task_summaries:
        tid = ts["task_id"]
        for dest_dir in (RAW_RECEIPTS_DIR / "tasks", DURABLE_DIR / "tasks"):
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / f"{tid}.json").write_text(json.dumps(ts, indent=2))

    print("\nArtifacts successfully written to:")
    print(f"  {EXP_DIR}")
    print(f"  {RAW_RECEIPTS_DIR}")
    print(f"  {DURABLE_DIR}")

    print("\n--- RESULTS OVERVIEW ---")
    print(f"Task weak rate: orig={orig_weak_count}/{n_tasks} ({orig_weak_count/n_tasks:.1%}), rep={rep_weak_count}/{n_tasks} ({rep_weak_count/n_tasks:.1%})")
    print(f"Task McNemar: b={task_test['b']}, c={task_test['c']}, p={task_test['p']:.6f}")
    print(f"Mutant survivor rate: orig={orig_mut_surv_count}/{tot_mutants} ({orig_mut_surv_count/tot_mutants:.1%}), rep={rep_mut_surv_count}/{tot_mutants} ({rep_mut_surv_count/tot_mutants:.1%})")
    print(f"Mutant McNemar: b={mutant_test['b']}, c={mutant_test['c']}, p={mutant_test['p']:.6f}")
    print(f"Degraded tasks count: {len(degraded_tasks)}")
    print(f"Gold 2x2: {overlap_2x2}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
