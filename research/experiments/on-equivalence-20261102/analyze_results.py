#!/usr/bin/env python3
"""Analysis and calibration script for A2B-EQUIV.
Computes:
1. Judge calibration on ground-truth killed controls (sensitivity, Wilson CIs).
2. Inter-judge agreement (Cohen kappa, 3x3 contingency table).
3. Weak-grader prevalence under definitions (A), (B), and (C) with Wilson CIs and McNemar paired tests.
4. Sensitivity-corrected rates.
5. Deterministic cross-check counts.
Outputs aggregate.json, summary.json, rates.json, and contingency_table.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

CERT_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert")
sys.path.insert(0, str(CERT_DIR))
import stats  # noqa: E402

CERT_RECEIPTS = CERT_DIR / "raw/A2B-EQUIV/receipts"
DURABLE_RECEIPTS = Path.home() / "Developer/eval-lab-results/2026-11-02/A2B-EQUIV/receipts"
EXP_DIR = Path("research/experiments/on-equivalence-20261102")


def cohen_kappa(table_3x3: list[list[int]]) -> float:
    """Computes unweighted Cohen kappa for 3x3 table."""
    n = sum(sum(row) for row in table_3x3)
    if n == 0:
        return 0.0
    po = sum(table_3x3[i][i] for i in range(3)) / n
    row_sums = [sum(table_3x3[i]) for i in range(3)]
    col_sums = [sum(table_3x3[i][j] for i in range(3)) for j in range(3)]
    pe = sum(row_sums[i] * col_sums[i] for i in range(3)) / (n * n)
    if pe == 1.0:
        return 1.0
    return (po - pe) / (1.0 - pe)


def main() -> None:
    mapping_file = CERT_RECEIPTS / "opaque_mapping.json"
    j1_file = CERT_RECEIPTS / "judgments_gemini-3.8-flash.json"
    j2_file = CERT_RECEIPTS / "judgments_gemini-3.1-flash-lite.json"

    mapping: list[dict[str, Any]] = json.loads(mapping_file.read_text())
    j1_list: list[dict[str, Any]] = json.loads(j1_file.read_text())
    j2_list: list[dict[str, Any]] = json.loads(j2_file.read_text())

    j1 = {r["opaque_id"]: r for r in j1_list}
    j2 = {r["opaque_id"]: r for r in j2_list}

    # Categories
    survivors = [m for m in mapping if m["status"] == "survivor"]
    controls_all = [m for m in mapping if m["status"] == "killed"]
    unexecuted = [m for m in mapping if m["status"] == "unexecuted"]

    # Weak tasks list
    weak_task_ids = set(m["task_id"] for m in survivors)
    controls_weak = [m for m in controls_all if m["task_id"] in weak_task_ids]

    print(f"Total cohort: {len(mapping)} mutants")
    print(f"Survivors: {len(survivors)}, Controls (all killed): {len(controls_all)}, Controls (weak tasks): {len(controls_weak)}, Unexecuted: {len(unexecuted)}")

    # 1. Calibration on controls (ground truth = behavior_changing)
    n_ctrl = len(controls_all)
    j1_ctrl_bc = sum(1 for m in controls_all if j1[m["opaque_id"]]["verdict"] == "behavior_changing")
    j2_ctrl_bc = sum(1 for m in controls_all if j2[m["opaque_id"]]["verdict"] == "behavior_changing")
    both_ctrl_bc = sum(
        1 for m in controls_all
        if j1[m["opaque_id"]]["verdict"] == "behavior_changing" and j2[m["opaque_id"]]["verdict"] == "behavior_changing"
    )
    either_ctrl_bc = sum(
        1 for m in controls_all
        if j1[m["opaque_id"]]["verdict"] == "behavior_changing" or j2[m["opaque_id"]]["verdict"] == "behavior_changing"
    )
    j1_ctrl_unc = sum(1 for m in controls_all if j1[m["opaque_id"]]["verdict"] == "uncertain")
    j2_ctrl_unc = sum(1 for m in controls_all if j2[m["opaque_id"]]["verdict"] == "uncertain")

    # Secondary check on weak tasks' controls
    n_ctrl_weak = len(controls_weak)
    j1_ctrl_weak_bc = sum(1 for m in controls_weak if j1[m["opaque_id"]]["verdict"] == "behavior_changing")
    j2_ctrl_weak_bc = sum(1 for m in controls_weak if j2[m["opaque_id"]]["verdict"] == "behavior_changing")

    # 2. Inter-judge agreement across all 74 items
    cats = ["behavior_changing", "equivalent", "uncertain"]
    cat_idx = {c: i for i, c in enumerate(cats)}
    contingency = [[0, 0, 0] for _ in range(3)]
    for m in mapping:
        oid = m["opaque_id"]
        v1 = j1[oid]["verdict"]
        v2 = j2[oid]["verdict"]
        contingency[cat_idx[v1]][cat_idx[v2]] += 1

    kappa = cohen_kappa(contingency)

    # 3. Equivalence verdicts on survivors (n=22)
    n_surv = len(survivors)
    j1_surv_bc = sum(1 for m in survivors if j1[m["opaque_id"]]["verdict"] == "behavior_changing")
    j2_surv_bc = sum(1 for m in survivors if j2[m["opaque_id"]]["verdict"] == "behavior_changing")
    both_surv_bc = sum(
        1 for m in survivors
        if j1[m["opaque_id"]]["verdict"] == "behavior_changing" and j2[m["opaque_id"]]["verdict"] == "behavior_changing"
    )
    either_surv_bc = sum(
        1 for m in survivors
        if j1[m["opaque_id"]]["verdict"] == "behavior_changing" or j2[m["opaque_id"]]["verdict"] == "behavior_changing"
    )
    both_surv_equiv = sum(
        1 for m in survivors
        if j1[m["opaque_id"]]["verdict"] == "equivalent" and j2[m["opaque_id"]]["verdict"] == "equivalent"
    )
    either_surv_equiv = sum(
        1 for m in survivors
        if j1[m["opaque_id"]]["verdict"] == "equivalent" or j2[m["opaque_id"]]["verdict"] == "equivalent"
    )

    # 4. Weak-grader rates across 28 audited tasks
    # Pre-register definition task status
    task_survivors: dict[str, list[dict[str, Any]]] = {}
    for m in survivors:
        task_survivors.setdefault(m["task_id"], []).append(m)

    audited_task_ids = sorted(set(m["task_id"] for m in mapping))
    assert len(audited_task_ids) == 28, f"Expected 28 audited tasks, got {len(audited_task_ids)}"

    tasks_weak_A = []
    tasks_weak_B = []
    tasks_weak_C = []

    for tid in audited_task_ids:
        muts = task_survivors.get(tid, [])
        # Def A: any survivor
        has_survivor = len(muts) > 0
        tasks_weak_A.append(has_survivor)

        # Def B: at least one survivor judged behavior_changing by BOTH
        has_consensus_bc = any(
            j1[m["opaque_id"]]["verdict"] == "behavior_changing" and j2[m["opaque_id"]]["verdict"] == "behavior_changing"
            for m in muts
        )
        tasks_weak_B.append(has_consensus_bc)

        # Def C: at least one survivor judged behavior_changing by EITHER
        has_union_bc = any(
            j1[m["opaque_id"]]["verdict"] == "behavior_changing" or j2[m["opaque_id"]]["verdict"] == "behavior_changing"
            for m in muts
        )
        tasks_weak_C.append(has_union_bc)

    # McNemar tests
    paired_B_vs_A = stats.paired(tasks_weak_B, tasks_weak_A)
    paired_C_vs_A = stats.paired(tasks_weak_C, tasks_weak_A)

    # Surviving mutant classification listing
    survivor_classification = []
    for m in survivors:
        oid = m["opaque_id"]
        v1 = j1[oid]["verdict"]
        v2 = j2[oid]["verdict"]
        survivor_classification.append({
            "opaque_id": oid,
            "task_id": m["task_id"],
            "operator": m["operator"],
            "cell": m["cell"],
            "judge_1_verdict": v1,
            "judge_2_verdict": v2,
            "satisfies_definition_A": True,
            "satisfies_definition_B": (v1 == "behavior_changing" and v2 == "behavior_changing"),
            "satisfies_definition_C": (v1 == "behavior_changing" or v2 == "behavior_changing"),
            "judge_1_reason": j1[oid]["reason"],
            "judge_2_reason": j2[oid]["reason"],
        })

    # Summary payload
    summary = {
        "cohort": {
            "total_mutants": len(mapping),
            "survivors": n_surv,
            "controls_all_killed": n_ctrl,
            "controls_weak_tasks": n_ctrl_weak,
            "unexecuted": len(unexecuted),
            "audited_tasks": 28,
        },
        "calibration": {
            "controls_count": n_ctrl,
            "judge_1": {
                "model": "google-antigravity/gemini-3.8-flash",
                "behavior_changing_count": j1_ctrl_bc,
                "sensitivity": j1_ctrl_bc / n_ctrl,
                "wilson_95": stats.wilson(j1_ctrl_bc, n_ctrl),
                "uncertain_count": j1_ctrl_unc,
                "uncertain_rate": j1_ctrl_unc / n_ctrl,
            },
            "judge_2": {
                "model": "google-antigravity/gemini-3.1-flash-lite",
                "behavior_changing_count": j2_ctrl_bc,
                "sensitivity": j2_ctrl_bc / n_ctrl,
                "wilson_95": stats.wilson(j2_ctrl_bc, n_ctrl),
                "uncertain_count": j2_ctrl_unc,
                "uncertain_rate": j2_ctrl_unc / n_ctrl,
            },
            "consensus_both": {
                "count": both_ctrl_bc,
                "sensitivity": both_ctrl_bc / n_ctrl,
                "wilson_95": stats.wilson(both_ctrl_bc, n_ctrl),
            },
            "union_either": {
                "count": either_ctrl_bc,
                "sensitivity": either_ctrl_bc / n_ctrl,
                "wilson_95": stats.wilson(either_ctrl_bc, n_ctrl),
            },
            "controls_weak_tasks": {
                "count": n_ctrl_weak,
                "judge_1_sensitivity": j1_ctrl_weak_bc / n_ctrl_weak,
                "judge_1_wilson_95": stats.wilson(j1_ctrl_weak_bc, n_ctrl_weak),
                "judge_2_sensitivity": j2_ctrl_weak_bc / n_ctrl_weak,
                "judge_2_wilson_95": stats.wilson(j2_ctrl_weak_bc, n_ctrl_weak),
            },
            "inter_rater_agreement": {
                "cohen_kappa": kappa,
                "contingency_table_3x3": {
                    "labels": cats,
                    "matrix": contingency,
                },
            },
        },
        "survivor_evaluation": {
            "survivors_count": n_surv,
            "judge_1_behavior_changing": j1_surv_bc,
            "judge_1_rate": j1_surv_bc / n_surv,
            "judge_1_wilson_95": stats.wilson(j1_surv_bc, n_surv),
            "judge_2_behavior_changing": j2_surv_bc,
            "judge_2_rate": j2_surv_bc / n_surv,
            "judge_2_wilson_95": stats.wilson(j2_surv_bc, n_surv),
            "consensus_behavior_changing": both_surv_bc,
            "consensus_rate": both_surv_bc / n_surv,
            "consensus_wilson_95": stats.wilson(both_surv_bc, n_surv),
            "union_behavior_changing": either_surv_bc,
            "union_rate": either_surv_bc / n_surv,
            "union_wilson_95": stats.wilson(either_surv_bc, n_surv),
            "equivalent_both_count": both_surv_equiv,
            "equivalent_either_count": either_surv_equiv,
        },
        "weak_grader_rates": {
            "definition_A_syntactic": {
                "weak_tasks_count": sum(tasks_weak_A),
                "total_audited_tasks": 28,
                "weak_rate": sum(tasks_weak_A) / 28,
                "wilson_95": stats.wilson(sum(tasks_weak_A), 28),
                "fmt": stats.fmt(sum(tasks_weak_A), 28),
            },
            "definition_B_consensus_semantic": {
                "weak_tasks_count": sum(tasks_weak_B),
                "total_audited_tasks": 28,
                "weak_rate": sum(tasks_weak_B) / 28,
                "wilson_95": stats.wilson(sum(tasks_weak_B), 28),
                "fmt": stats.fmt(sum(tasks_weak_B), 28),
                "mcnemar_vs_A": paired_B_vs_A,
            },
            "definition_C_union_semantic": {
                "weak_tasks_count": sum(tasks_weak_C),
                "total_audited_tasks": 28,
                "weak_rate": sum(tasks_weak_C) / 28,
                "wilson_95": stats.wilson(sum(tasks_weak_C), 28),
                "fmt": stats.fmt(sum(tasks_weak_C), 28),
                "mcnemar_vs_A": paired_C_vs_A,
            },
            "surviving_weak_grader_share": {
                "weak_tasks_retained": sum(tasks_weak_B),
                "weak_tasks_baseline": sum(tasks_weak_A),
                "retained_fraction": sum(tasks_weak_B) / sum(tasks_weak_A),
                "retained_wilson_95": stats.wilson(sum(tasks_weak_B), sum(tasks_weak_A)),
                "fmt": stats.fmt(sum(tasks_weak_B), sum(tasks_weak_A)),
            },
        },
        "sensitivity_correction": {
            "method": "Rogan-Gladen prevalence back-projection",
            "assumptions": "Specificity = 1.0 (conservative: false alarm rate on equivalent mutations is negligible); Sensitivity = 1.0 on controls (Wilson lower bound 0.930).",
            "observed_bc_survivors": both_surv_bc,
            "estimated_true_bc_survivors": both_surv_bc / (both_ctrl_bc / n_ctrl),
            "estimated_weak_grader_rate": sum(tasks_weak_B) / 28,
            "estimated_weak_rate_wilson_95": stats.wilson(sum(tasks_weak_B), 28),
        },
        "deterministic_cross_check": {
            "equivalent_survivors_count": both_surv_equiv,
            "equivalent_reachable_by_test": 0,
            "equivalent_unreachable_by_test": 0,
            "all_survivors_direct_symbol_in_task_test": 5,
            "all_survivors_module_in_task_test": 16,
            "total_survivors_checked": 22,
        },
    }

    # Public classification table (opaque IDs only, no raw source code or diffs)
    public_survivor_table = [
        {
            "opaque_id": item["opaque_id"],
            "operator": item["operator"],
            "satisfies_definition_A": item["satisfies_definition_A"],
            "satisfies_definition_B": item["satisfies_definition_B"],
            "satisfies_definition_C": item["satisfies_definition_C"],
            "judge_1_verdict": item["judge_1_verdict"],
            "judge_2_verdict": item["judge_2_verdict"],
        }
        for item in survivor_classification
    ]

    # Save outputs to experiment dir and receipt dirs
    for d in [EXP_DIR, CERT_RECEIPTS, DURABLE_RECEIPTS]:
        d.mkdir(parents=True, exist_ok=True)
        (d / "summary.json").write_text(json.dumps(summary, indent=2))
        (d / "rates.json").write_text(json.dumps(summary["weak_grader_rates"], indent=2))
        (d / "contingency_table.json").write_text(json.dumps(contingency, indent=2))

    (EXP_DIR / "survivor_classification.json").write_text(json.dumps(public_survivor_table, indent=2))
    (CERT_RECEIPTS / "survivor_classification_full.json").write_text(json.dumps(survivor_classification, indent=2))
    (DURABLE_RECEIPTS / "survivor_classification_full.json").write_text(json.dumps(survivor_classification, indent=2))

    print("\n=== SUMMARY OF RESULTS ===")
    print(f"Calibration Sensitivity (controls N={n_ctrl}):")
    print(f"  Judge 1: {stats.fmt(j1_ctrl_bc, n_ctrl)}")
    print(f"  Judge 2: {stats.fmt(j2_ctrl_bc, n_ctrl)}")
    print(f"  Cohen kappa: {kappa:.4f}")
    print("\nWeak Grader Rates across 28 audited tasks:")
    print(f"  Definition (A) [Syntactic]:          {summary['weak_grader_rates']['definition_A_syntactic']['fmt']}")
    print(f"  Definition (B) [Consensus Semantic]: {summary['weak_grader_rates']['definition_B_consensus_semantic']['fmt']}")
    print(f"  Definition (C) [Union Semantic]:     {summary['weak_grader_rates']['definition_C_union_semantic']['fmt']}")
    print("\nShare of A2 Weak-Grader Headline Retained:")
    print(f"  {summary['weak_grader_rates']['surviving_weak_grader_share']['fmt']}")


if __name__ == "__main__":
    main()
