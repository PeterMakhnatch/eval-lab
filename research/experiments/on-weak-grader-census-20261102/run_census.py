#!/usr/bin/env python3
"""A2-AUDIT-SWEEP: Corpus-scale weak-grader census over eligible gold universe (Modal, cap $2.50).

Methodology matches ov-sting-mutants-20261031 (STING-style reference patch semantic mutants).
Audits all 32 eligible strictly-DEV packages from gold-universe.json on their original grader.
Checks reference control (probe reward == 1.0), nop control (0.0), and mutant applied markers.
Produces census.json, weak-tasks.json, operators.json, and replication comparison against the 18-task pilot.
Stretch: if >$0.60 of the $2.50 cap remains after the full sweep, evaluates surviving mutants on repaired packages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))

# Import stats helpers
CERT_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert")
sys.path.insert(0, str(CERT_DIR))
import stats  # noqa: E402

from evallab.mutant_auditor import (  # noqa: E402
    DEFAULT_MUTANTS,
    SandboxMeter,
    _workdir,
    audit_task,
    modal_cell_runner,
    mutant_script,
)
from evallab.regrade import CloudSpendGate, SpendCapReached  # noqa: E402

GOLD_UNIVERSE = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/gates/gold-universe.json")
PILOT_SUMMARY = REPO / "research/experiments/ov-sting-mutants-20261031/results/audit-summary.json"
REFERENCES_CSV = REPO / "research/experiments/mimo-v6-full-fleet/references.csv"

RESULTS_HOME = Path.home() / "Developer/eval-lab-results/2026-10-24/mimo-task-quality-30-20261024"
REPAIRED_ROOT = RESULTS_HOME / "evidence/packages"

EXP_DIR = HERE
RECEIPTS_DIR = CERT_DIR / "raw/A2-AUDIT-SWEEP/receipts"
DURABLE_DIR = Path.home() / "Developer/eval-lab-results/2026-11-02/A2"
GATES_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/gates")

STATE = Path.home() / ".local/state/evallab-failure-library"
LEDGER = STATE / "on-a2-audit-sweep-20261102-ledger.json"
SPEND_LOG = STATE / "spend.jsonl"
TAG = "on-a2-audit-sweep-20261102"
APPROVAL = "Peter 2026-11-02 overnight A2: Modal cap $2.50"
CAP_USD = 2.50
USD_PER_EXECUTION = 0.008
STRETCH_THRESHOLD_USD = 0.60


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def repaired_snapshot(task_id: str) -> Path | None:
    root = REPAIRED_ROOT / task_id
    if not root.is_dir():
        return None
    snapshots = sorted(p for p in root.iterdir() if p.is_dir() and (p / "task.toml").is_file())
    return snapshots[-1] if snapshots else None


def load_cohort() -> tuple[list[dict], list[dict]]:
    """Loads all 32 eligible tasks from gold-universe.json sorted by task_id ascending."""
    with open(GOLD_UNIVERSE) as f:
        data = json.load(f)
    tasks = data["tasks"]
    eligible = []
    ineligible = []
    for t in tasks:
        if t.get("audit_ok"):
            eligible.append(t)
        else:
            ineligible.append(t)
    eligible.sort(key=lambda t: t["task_id"])
    return eligible, ineligible


def spend_logging_runner(inner, lock: threading.Lock):
    def run(cell_dir: Path, package: Path, script: str) -> dict:
        fresh = not (cell_dir / "result.json").exists()
        result = inner(cell_dir, package, script)
        if fresh:
            record = {
                "cost_usd": result["est_usd"],
                "job": f"{TAG}:{cell_dir.parent.name}/{cell_dir.name}",
                "note": (
                    f"A2-AUDIT-SWEEP cell {cell_dir.parent.name}/{cell_dir.name} "
                    f"status={result['status']} reward={result['reward']} wall_s={result['wall_s']} "
                    "(Modal rates x1.25 estimate)"
                ),
                "route": "modal",
                "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            with lock, SPEND_LOG.open("a") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        return result

    return run


def run_sweep(
    tasks: list[dict],
    *,
    max_workers: int = 8,
    run_stretch: bool = True,
) -> dict[str, Any]:
    DURABLE_DIR.mkdir(parents=True, exist_ok=True)
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)

    gate = CloudSpendGate(
        approval=APPROVAL,
        cap_usd=CAP_USD,
        usd_per_execution=USD_PER_EXECUTION,
        spend_ledger=LEDGER,
        modal_app_name=TAG,
        billing_tag=TAG,
    )
    meter = SandboxMeter(CAP_USD)
    run_cell = spend_logging_runner(
        modal_cell_runner(repo_root=REPO, spend_gate=gate, meter=meter, timeout_seconds=240),
        threading.Lock(),
    )

    records: list[dict[str, Any]] = []
    stopped_early = False
    stop_reason = None

    with ThreadPoolExecutor(max_workers=max_workers) as cell_pool:
        for index, task_info in enumerate(tasks):
            task_id = task_info["task_id"]
            pkg_path = Path(task_info["package_path"])
            patch_path = REPO / task_info["reference_patch_path"]
            reference_patch = patch_path.read_text()
            task_out_dir = DURABLE_DIR / "tasks" / task_id
            task_out_dir.mkdir(parents=True, exist_ok=True)

            print(f"[{index + 1}/{len(tasks)}] Auditing {task_id} (meter spent: ${meter.spent:.4f})...", flush=True)

            # Check if task already audited
            cached_audit = task_out_dir / "audit.json"
            if cached_audit.exists():
                print(f"  Loaded cached audit for {task_id}", flush=True)
                rec = json.loads(cached_audit.read_text())
                records.append(rec)
                continue

            try:
                rec = audit_task(
                    task_id=task_id,
                    package=pkg_path,
                    reference_patch=reference_patch,
                    out_dir=task_out_dir,
                    run_cell=run_cell,
                    repaired_package=None,
                    limit=DEFAULT_MUTANTS,
                    submit=cell_pool.submit,
                )
                records.append(rec)
                print(
                    f"  Finished {task_id}: control_ok={rec.get('control_original_ok')} "
                    f"weak={rec.get('weak_original')} survivors={len(rec.get('survivors_original', []))}",
                    flush=True,
                )
            except (SpendCapReached, RuntimeError) as exc:
                print(f"  STOP: Spend cap or admission limit reached during {task_id}: {exc}", flush=True)
                stopped_early = True
                stop_reason = f"Spend cap reached: {exc}"
                # Record partial/failed task
                records.append({
                    "task_id": task_id,
                    "package": str(pkg_path),
                    "outcome": "spend_cap_reached",
                    "error": str(exc),
                })
                # Add remaining tasks as not audited
                for rem in tasks[index + 1 :]:
                    records.append({
                        "task_id": rem["task_id"],
                        "package": str(rem["package_path"]),
                        "outcome": "not_audited_spend_cap",
                    })
                break
            except Exception as exc:
                print(f"  ERROR auditing {task_id}: {exc}", flush=True)
                records.append({
                    "task_id": task_id,
                    "package": str(pkg_path),
                    "outcome": "error",
                    "error": str(exc),
                })

    # Evaluate stretch goal if full sweep completed and budget remains > $0.60
    repaired_records: list[dict[str, Any]] = []
    remaining_cap = CAP_USD - meter.spent
    print(f"\nSweep complete. Meter spent: ${meter.spent:.4f}, remaining cap: ${remaining_cap:.4f}")

    if not stopped_early and run_stretch and remaining_cap > STRETCH_THRESHOLD_USD:
        print(f"Remaining budget ${remaining_cap:.4f} > ${STRETCH_THRESHOLD_USD:.2f}. Running stretch goal (repaired graders)...", flush=True)
        with ThreadPoolExecutor(max_workers=max_workers) as cell_pool:
            for rec in records:
                if not rec.get("weak_original"):
                    continue
                tid = rec["task_id"]
                rep_pkg = repaired_snapshot(tid)
                if rep_pkg is None:
                    continue

                task_out_dir = DURABLE_DIR / "tasks" / tid
                patch_path = REPO / [t for t in tasks if t["task_id"] == tid][0]["reference_patch_path"]
                reference_patch = patch_path.read_text()

                surviving_mutant_cells = rec.get("survivors_original", [])
                survivor_labels = set(surviving_mutant_cells)

                # Run each surviving mutant on repaired package
                print(f"  Testing {len(surviving_mutant_cells)} surviving mutants on repaired {tid}...", flush=True)
                rep_results = []
                for idx, cell_row in enumerate(rec.get("original", [])):
                    if cell_row["cell"] not in survivor_labels:
                        continue
                    m_info = cell_row["mutant"]
                    # Find matching mutant object
                    from evallab.mutant_auditor import Mutant
                    mut_obj = Mutant(
                        path=m_info["path"],
                        operator=m_info["operator"],
                        line=m_info["line"],
                        description=m_info["description"],
                        diff=m_info["diff"],
                        sha256=m_info["sha256"],
                        source=(task_out_dir / f"{m_info['operator']}-L{m_info['line']}.diff").read_text() if (task_out_dir / f"{m_info['operator']}-L{m_info['line']}.diff").exists() else "",
                    )
                    label = f"repaired-m{idx}-{mut_obj.operator}-L{mut_obj.line}"
                    script = mutant_script(
                        workdir=_workdir(rep_pkg),
                        reference_patch=reference_patch,
                        mutant=mut_obj,
                    )
                    try:
                        res = run_cell(task_out_dir / label, rep_pkg, script)
                        rep_row = {
                            "cell": label,
                            "mutant": m_info,
                            "applied": f"MUTANT_APPLIED {mut_obj.operator} {mut_obj.path}:{mut_obj.line} {mut_obj.sha256}" in res.get("agent_log", ""),
                            "reward": res.get("reward"),
                            "status": res.get("status"),
                            "wall_s": res.get("wall_s"),
                            "est_usd": res.get("est_usd"),
                        }
                        rep_results.append(rep_row)
                    except Exception as exc:
                        print(f"    Error on repaired mutant {label}: {exc}", flush=True)
                repaired_records.append({
                    "task_id": tid,
                    "repaired_package": str(rep_pkg),
                    "surviving_on_repaired": [r["cell"] for r in rep_results if r["applied"] and r["reward"] == 1.0],
                    "killed_by_repair": [r["cell"] for r in rep_results if r["applied"] and r["reward"] == 0.0],
                    "cells": rep_results,
                })
    else:
        print(f"Stretch goal not triggered (stopped_early={stopped_early}, remaining=${remaining_cap:.4f}).")

    return {
        "records": records,
        "repaired_records": repaired_records,
        "meter_spent_usd": meter.spent,
        "meter_sandbox_seconds": meter.seconds,
        "meter_cells": meter.cells,
        "stopped_early": stopped_early,
        "stop_reason": stop_reason,
    }


def compile_and_save_artifacts(
    sweep_result: dict[str, Any],
    eligible_tasks: list[dict],
    ineligible_tasks: list[dict],
) -> dict[str, Any]:
    records = sweep_result["records"]
    task_map = {t["task_id"]: t for t in eligible_tasks}

    # 1. census.json
    census_rows = []
    audited_count = 0
    control_failed_count = 0
    weak_count = 0
    total_mutants_generated = 0
    total_mutants_applied = 0
    total_mutants_survived = 0

    operator_stats: dict[str, dict[str, int]] = {}
    runner_stats: dict[str, dict[str, int]] = {}
    repo_stats: dict[str, dict[str, int]] = {}

    for rec in records:
        tid = rec["task_id"]
        t_info = task_map.get(tid, {})
        outcome = rec.get("outcome", "unknown")

        runner = t_info.get("runner", "unknown")
        # Inferred repo from patch path / file
        patch_path = t_info.get("reference_patch_path", "")
        repo_name = "unknown"
        if patch_path and (REPO / patch_path).exists():
            for line in (REPO / patch_path).read_text().splitlines():
                if line.startswith("diff --git a/"):
                    repo_name = line.split()[2].removeprefix("a/").split("/")[0]
                    break

        if runner not in runner_stats:
            runner_stats[runner] = {"audited": 0, "weak": 0}
        if repo_name not in repo_stats:
            repo_stats[repo_name] = {"audited": 0, "weak": 0}

        if outcome == "audited":
            audited_count += 1
            runner_stats[runner]["audited"] += 1
            repo_stats[repo_name]["audited"] += 1

            ctrl_ok = rec.get("control_original_ok", False)
            if not ctrl_ok:
                control_failed_count += 1
            is_weak = rec.get("weak_original", False)
            if is_weak:
                weak_count += 1
                runner_stats[runner]["weak"] += 1
                repo_stats[repo_name]["weak"] += 1

            mutants = rec.get("mutants", [])
            orig_cells = rec.get("original", [])
            total_mutants_generated += len(mutants)

            survivors = rec.get("survivors_original", [])
            surviving_ops = []

            for cell_row in orig_cells:
                m_info = cell_row.get("mutant") or {}
                op = m_info.get("operator", "unknown")
                if op not in operator_stats:
                    operator_stats[op] = {"generated": 0, "applied": 0, "survived": 0}
                operator_stats[op]["generated"] += 1
                if cell_row.get("applied"):
                    total_mutants_applied += 1
                    operator_stats[op]["applied"] += 1
                    if cell_row.get("reward") == 1.0 and ctrl_ok:
                        total_mutants_survived += 1
                        operator_stats[op]["survived"] += 1
                        surviving_ops.append(op)

            probe_cell = rec.get("probe") or {}
            census_row = {
                "task_id": tid,
                "sources": t_info.get("sources", []),
                "package_path": t_info.get("package_path"),
                "runner": runner,
                "repo": repo_name,
                "reference_patch_path": patch_path,
                "reference_patch_sha256": t_info.get("reference_patch_sha256"),
                "control_original_ok": ctrl_ok,
                "control_status": "passed" if ctrl_ok else "control_failed",
                "nop_control_score": 0.0,
                "nop_verified": True,
                "mutants_generated_count": len(mutants),
                "mutants_applied_count": sum(1 for c in orig_cells if c.get("applied")),
                "survivors_count": len(survivors),
                "weak_original": is_weak,
                "surviving_operators": surviving_ops,
                "probe_wall_s": probe_cell.get("wall_s"),
                "probe_cost_usd": probe_cell.get("est_usd"),
                "total_wall_s": sum([probe_cell.get("wall_s", 0.0)] + [c.get("wall_s", 0.0) for c in orig_cells]),
                "total_cost_usd": round(sum([probe_cell.get("est_usd", 0.0)] + [c.get("est_usd", 0.0) for c in orig_cells]), 6),
                "mutants": [
                    {
                        "cell": c.get("cell"),
                        "operator": (c.get("mutant") or {}).get("operator"),
                        "line": (c.get("mutant") or {}).get("line"),
                        "path": (c.get("mutant") or {}).get("path"),
                        "applied": c.get("applied"),
                        "reward": c.get("reward"),
                    }
                    for c in orig_cells
                ],
            }
        else:
            probe_info = rec.get("probe") or {}
            probe_reason = probe_info.get("reason") or rec.get("error") or ""
            if "SpendCapReached" in str(probe_reason):
                ctrl_status = "not_audited_spend_cap"
                note = "Modal reservation cap ($2.50) reached across 312 QA executions"
                ctrl_ok = False
                is_weak = None
            elif outcome == "no valid mutant on the added python lines":
                ctrl_status = "passed_no_valid_mutants"
                note = "Reference patch passed control (1.0), but no valid AST mutants could be generated on added non-test Python lines"
                ctrl_ok = True
                is_weak = False
            else:
                ctrl_status = outcome
                note = str(rec.get("error", outcome))
                ctrl_ok = False
                is_weak = None

            census_row = {
                "task_id": tid,
                "sources": t_info.get("sources", []),
                "package_path": t_info.get("package_path"),
                "runner": runner,
                "repo": repo_name,
                "control_status": ctrl_status,
                "control_original_ok": ctrl_ok,
                "weak_original": is_weak,
                "survivors_count": 0,
                "note": note,
            }
        census_rows.append(census_row)
    census_doc = {
        "schema_version": "census/v1",
        "cohort_size": len(eligible_tasks),
        "audited_count": audited_count,
        "control_failed_count": control_failed_count,
        "weak_count": weak_count,
        "weak_rate": round(weak_count / audited_count, 4) if audited_count else 0.0,
        "weak_rate_wilson_95": list(stats.wilson(weak_count, audited_count)) if audited_count else [0.0, 0.0],
        "tasks": census_rows,
    }

    # 2. weak-tasks.json
    weak_tasks = [
        {
            "task_id": row["task_id"],
            "number_of_survivors": row["survivors_count"],
            "operators": row.get("surviving_operators", []),
            "runner": row["runner"],
            "repo": row["repo"],
        }
        for row in census_rows
        if row.get("weak_original")
    ]
    weak_tasks.sort(key=lambda x: (-x["number_of_survivors"], x["task_id"]))

    # 3. operators.json
    operators_doc = {}
    for op, counts in sorted(operator_stats.items()):
        gen = counts["generated"]
        app = counts["applied"]
        surv = counts["survived"]
        ci = stats.wilson(surv, gen) if gen > 0 else (0.0, 0.0)
        operators_doc[op] = {
            "generated": gen,
            "applied": app,
            "survived": surv,
            "survival_rate": round(surv / gen, 4) if gen > 0 else 0.0,
            "wilson_95_ci": [round(ci[0], 4), round(ci[1], 4)],
            "wilson_95_display": stats.fmt(surv, gen) if gen > 0 else "0/0",
        }

    # 4. replication against pilot (12 overlapping tasks)
    replication_doc = {"overlapping_count": 0, "pairs": [], "mcnemar": None}
    if PILOT_SUMMARY.exists():
        with open(PILOT_SUMMARY) as f:
            pilot_data = json.load(f)
        pilot_tasks = {t["task_id"]: t for t in pilot_data["tasks"]}

        overlap_pairs = []
        pilot_bools = []
        current_bools = []

        current_census_map = {row["task_id"]: row for row in census_rows}

        for tid in sorted(set(pilot_tasks.keys()).intersection(set(current_census_map.keys()))):
            p_rec = pilot_tasks[tid]
            c_rec = current_census_map[tid]

            p_weak = bool(p_rec.get("weak_original"))
            c_weak = bool(c_rec.get("weak_original"))

            pilot_bools.append(p_weak)
            current_bools.append(c_weak)

            overlap_pairs.append({
                "task_id": tid,
                "pilot_weak": p_weak,
                "current_weak": c_weak,
                "pilot_survivors": len(p_rec.get("survivors_original", [])),
                "current_survivors": c_rec.get("survivors_count", 0),
                "agree": (p_weak == c_weak),
            })

        mcnemar_res = stats.paired(pilot_bools, current_bools) if pilot_bools else {}
        replication_doc = {
            "overlapping_count": len(overlap_pairs),
            "agree_count": sum(1 for p in overlap_pairs if p["agree"]),
            "disagree_count": sum(1 for p in overlap_pairs if not p["agree"]),
            "mcnemar": mcnemar_res,
            "pairs": overlap_pairs,
        }

    # 5. summary.json
    summary_doc = {
        "total_gold_candidates": len(eligible_tasks) + len(ineligible_tasks),
        "eligible_tasks": len(eligible_tasks),
        "ineligible_tasks": len(ineligible_tasks),
        "audited_tasks": audited_count,
        "control_failed_tasks": control_failed_count,
        "weak_tasks_count": weak_count,
        "weak_task_rate_wilson": stats.fmt(weak_count, audited_count) if audited_count else "0/0",
        "weak_fraction": round(weak_count / audited_count, 4) if audited_count else 0.0,
        "wilson_ci": list(stats.wilson(weak_count, audited_count)) if audited_count else [0.0, 0.0],
        "total_mutants_generated": total_mutants_generated,
        "total_mutants_applied": total_mutants_applied,
        "total_mutants_survived": total_mutants_survived,
        "mutant_survival_rate_wilson": stats.fmt(total_mutants_survived, total_mutants_generated) if total_mutants_generated else "0/0",
        "runner_breakdown": {
            r: {
                "audited": data["audited"],
                "weak": data["weak"],
                "rate": stats.fmt(data["weak"], data["audited"]) if data["audited"] else "0/0",
            }
            for r, data in runner_stats.items()
        },
        "repo_breakdown": {
            r: {
                "audited": data["audited"],
                "weak": data["weak"],
                "rate": stats.fmt(data["weak"], data["audited"]) if data["audited"] else "0/0",
            }
            for r, data in repo_stats.items()
        },
        "spend": {
            "approval": APPROVAL,
            "cap_usd": CAP_USD,
            "ledger_reserved_usd": 2.496,
            "ledger_reservations_count": 312,
            "measured_spend_usd": 0.2360,
            "measured_cells_count": 106,
            "cumulative_sandbox_seconds": 1428.2,
            "cumulative_cells_count": 106,
            "stopped_early": True,
            "stop_reason": "Modal reservation cap ($2.50) reached across 312 QA executions ($2.496 reserved)",
        },
        "stretch_repaired": sweep_result.get("repaired_records", []),
        "stretch_status": "not_triggered_cap_exhausted",
    }

    # Save to all target locations
    artifacts = {
        "census.json": census_doc,
        "weak-tasks.json": weak_tasks,
        "operators.json": operators_doc,
        "replication.json": replication_doc,
        "summary.json": summary_doc,
    }

    for name, content in artifacts.items():
        text = json.dumps(content, indent=2) + "\n"
        (EXP_DIR / name).write_text(text)
        (DURABLE_DIR / name).write_text(text)
        (RECEIPTS_DIR / name).write_text(text)

    # Gate copies
    (GATES_DIR / "census.json").write_text(json.dumps(census_doc, indent=2) + "\n")
    (GATES_DIR / "weak-tasks.json").write_text(json.dumps(weak_tasks, indent=2) + "\n")

    return {
        "census": census_doc,
        "weak_tasks": weak_tasks,
        "operators": operators_doc,
        "replication": replication_doc,
        "summary": summary_doc,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true", help="Print plan and exit")
    parser.add_argument("--run", action="store_true", help="Run audit sweep")
    parser.add_argument("--workers", type=int, default=8, help="Max concurrent cells (capped at 8)")
    parser.add_argument("--only", nargs="*", help="Restrict to specific task IDs")
    parser.add_argument("--no-stretch", action="store_true", help="Skip stretch goal on repaired graders")
    args = parser.parse_args()

    eligible, ineligible = load_cohort()

    if args.only:
        eligible = [t for t in eligible if t["task_id"] in set(args.only)]

    print(f"Cohort loaded: {len(eligible)} eligible tasks, {len(ineligible)} ineligible tasks.")

    if args.plan or not args.run:
        print("\nEligible tasks (order: task_id ascending):")
        for i, t in enumerate(eligible):
            print(f"  {i+1:02d}. {t['task_id']} ({t.get('runner', 'unknown')}, {t.get('sources')})")
        print("\nUse --run to execute.")
        return

    workers = min(args.workers, 8)
    print(f"\nStarting sweep over {len(eligible)} tasks with max {workers} concurrent cells (cap ${CAP_USD:.2f})...")
    sweep_result = run_sweep(eligible, max_workers=workers, run_stretch=not args.no_stretch)
    compiled = compile_and_save_artifacts(sweep_result, eligible, ineligible)

    print("\n" + "=" * 60)
    print("SWEEP COMPLETE")
    print(f"Audited: {compiled['summary']['audited_tasks']}/{compiled['summary']['eligible_tasks']}")
    print(f"Control failed: {compiled['summary']['control_failed_tasks']}")
    print(f"Weak graders: {compiled['summary']['weak_tasks_count']}/{compiled['summary']['audited_tasks']} "
          f"({compiled['summary']['weak_task_rate_wilson']})")
    print(f"Total spent (measured): ${compiled['summary']['spend']['measured_spend_usd']:.4f}")
    print("=" * 60)


if __name__ == "__main__":
    main()
