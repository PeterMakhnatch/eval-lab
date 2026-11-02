#!/usr/bin/env python3
"""Generate summary metrics and SHA256 manifest for F10-ROADMAP-FINAL receipts.

Calculates Wilson score confidence intervals, cross-engine kill-test statistics,
empirical friction coverage ratios, and writes a cryptographic manifest.
"""

import hashlib
import json
import math
from pathlib import Path


def wilson_interval(k: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """Calculate the Wilson score interval for a binomial proportion."""
    if n == 0:
        return (0.0, 0.0)
    z = 1.95996  # 95% confidence
    p = k / n
    denominator = 1 + z**2 / n
    centre_adjusted_probability = p + z**2 / (2 * n)
    adjusted_standard_deviation = math.sqrt((p * (1 - p) + z**2 / (4 * n)) / n)
    lower_bound = (centre_adjusted_probability - z * adjusted_standard_deviation) / denominator
    upper_bound = (centre_adjusted_probability + z * adjusted_standard_deviation) / denominator
    return (max(0.0, lower_bound), min(1.0, upper_bound))


def sha256_file(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def main():
    # Load evidence summary
    evidence_path = Path("research/experiments/on-workflow-roadmap-20261102/receipts/evidence_summary.json")
    with open(evidence_path) as f:
        ev = json.load(f)

    # KT1 10-trial binomial stats (10/10 passed for all engines)
    kt1_k = 10
    kt1_n = 10
    kt1_low, kt1_high = wilson_interval(kt1_k, kt1_n)

    # Friction and pain points coverage
    total_friction = ev["friction_item_count"]  # 56
    tooling_friction = ev["tooling_environment_friction_count"]  # 14
    workflow_friction = total_friction - tooling_friction  # 42
    census_incidents = ev["census_incident_count"]  # 28
    total_pain_points = ev["total_evidence_points"]  # 84
    workflow_pain_points = total_pain_points - tooling_friction  # 70

    covered_friction_by_top_5 = sum(item["friction_count"] for item in ev["top_addressed_workflows"])  # 32
    covered_census_by_top_5 = sum(item["census_incident_count"] for item in ev["top_addressed_workflows"])  # 16
    covered_pain_points_by_top_5 = covered_friction_by_top_5 + covered_census_by_top_5  # 48

    # Wilson intervals
    cov_pain_low, cov_pain_high = wilson_interval(covered_pain_points_by_top_5, workflow_pain_points)
    cov_fric_all_low, cov_fric_all_high = wilson_interval(covered_friction_by_top_5, total_friction)
    cov_fric_wf_low, cov_fric_wf_high = wilson_interval(covered_friction_by_top_5, workflow_friction)
    cov_census_low, cov_census_high = wilson_interval(covered_census_by_top_5, census_incidents)

    summary_data = {
        "title": "F10-ROADMAP-FINAL: Workflow Roadmap & Decision Memo Summary",
        "date": "2026-11-02",
        "key_metrics": {
            "total_tonight_friction_items": total_friction,
            "total_census_documented_incidents": census_incidents,
            "total_empirical_evidence_points": total_pain_points,
            "tooling_environment_friction_count": tooling_friction,
            "workflow_pain_points_count": workflow_pain_points,
            "top_5_workflows_covered_pain_points": covered_pain_points_by_top_5,
            "top_5_workflows_pain_point_coverage_ratio": f"{covered_pain_points_by_top_5}/{workflow_pain_points} ({covered_pain_points_by_top_5/workflow_pain_points*100:.1f}%)",
            "top_5_workflows_pain_point_coverage_95_wilson": [round(cov_pain_low, 3), round(cov_pain_high, 3)],
            "friction_covered_by_top_5": covered_friction_by_top_5,
            "friction_coverage_ratio_all": f"{covered_friction_by_top_5}/{total_friction} ({covered_friction_by_top_5/total_friction*100:.1f}%)",
            "friction_coverage_all_95_wilson": [round(cov_fric_all_low, 3), round(cov_fric_all_high, 3)],
            "friction_coverage_ratio_workflow": f"{covered_friction_by_top_5}/{workflow_friction} ({covered_friction_by_top_5/workflow_friction*100:.1f}%)",
            "friction_coverage_workflow_95_wilson": [round(cov_fric_wf_low, 3), round(cov_fric_wf_high, 3)],
            "census_covered_by_top_5": covered_census_by_top_5,
            "census_coverage_ratio": f"{covered_census_by_top_5}/{census_incidents} ({covered_census_by_top_5/census_incidents*100:.1f}%)",
            "census_coverage_95_wilson": [round(cov_census_low, 3), round(cov_census_high, 3)],
            "kill_test_results": {
                "dbos": {
                    "scorecard": "5/5 safe (4 pass + 1 loud fail)",
                    "kt1_crash_survival_rate": f"{kt1_k}/{kt1_n} (100.0%)",
                    "kt1_raw_executions": 68,
                    "kt1_duplicate_effects": 0,
                    "kt1_95_wilson": [round(kt1_low, 3), round(kt1_high, 3)],
                    "kt2_approval_reboot": "PASS (survived 62s downtime via SQLite DBOS.recv)",
                    "kt3_quota_attempts_before_T": 0,
                    "kt3_resume_tolerance_sec": 0.631,
                    "kt4_code_mutation": "FAILS LOUDLY (DBOSUnexpectedStepError)",
                    "kt5_caps": "PASS (wall-clock killed-time, iteration cap DBOS Error 7, 0 leaked PIDs)"
                },
                "temporal": {
                    "scorecard": "4/5 (3 pass + 1 loud fail, 1 native FAIL on KT3)",
                    "kt1_crash_survival_rate": f"{kt1_k}/{kt1_n} (100.0%)",
                    "kt1_raw_executions": 60,
                    "kt1_duplicate_effects": 0,
                    "kt1_qualifier": "At-least-once: 2 starts if agent dies with worker per VF-F-WORKFLOWS S3; exactly-once if agent outlives worker",
                    "kt1_95_wilson": [round(kt1_low, 3), round(kt1_high, 3)],
                    "kt2_approval_reboot": "PASS (survives dev server restart on SQLite)",
                    "kt3_quota_attempts_before_T": 3,
                    "kt3_verdict": "FAIL (3 blind continuation retries in <1.5s; 0 after T; exhausts retries)",
                    "kt4_code_mutation": "FAILS LOUDLY (NonDeterministicWorkflowError)",
                    "kt5_caps": "PASS (activity timeout cancelled, retry cap halted, 0 leaked PIDs)"
                },
                "dagu": {
                    "scorecard": "4/5 (3 pass + 1 recover, 1 native FAIL on KT3)",
                    "kt1_crash_survival_rate": f"{kt1_k}/{kt1_n} (100.0%)",
                    "kt1_raw_executions": 79,
                    "kt1_duplicate_effects": 0,
                    "kt1_95_wilson": [round(kt1_low, 3), round(kt1_high, 3)],
                    "kt2_approval_reboot": "PASS (human.task state survives 65s downtime)",
                    "kt3_quota_attempts_before_T": 4,
                    "kt3_verdict": "FAIL Native (4 static retries before T; external wrapper required)",
                    "kt4_code_mutation": "RECOVERS (pins immutable dag.json attempt snapshot)",
                    "kt5_caps": "PASS (wall-clock cap fired at 2s, retry cap halted, 0 leaked PIDs)"
                },
                "baseline": {
                    "scorecard": "2/5 (2 pass, 2 fail, 1 silent divergence)",
                    "kt1_crash_survival_rate": f"{kt1_k}/{kt1_n} (100.0%)",
                    "kt1_raw_executions": 76,
                    "kt1_duplicate_executions": 16,
                    "kt1_duplicate_effects": 0,
                    "kt1_behavior": "Re-executes completed tasks from scratch on every crash",
                    "kt1_95_wilson": [round(kt1_low, 3), round(kt1_high, 3)],
                    "kt2_approval_reboot": "FAIL (re-executes completed PREP step from scratch)",
                    "kt3_quota_attempts_before_T": 4,
                    "kt3_verdict": "FAIL (4 blind rapid retries in <3s; 0 after T)",
                    "kt4_code_mutation": "SILENT DIVERGENCE (dynamically reloads modified plan.json mid-run)",
                    "kt5_caps": "PASS (wall-clock killed-time, retry cap halted at 3 attempts, 0 leaked PIDs)"
                }
            },
            "loc_comparison": {
                "temporal_squad_loc_core": 1037,
                "dbos_squad_loc_core": 663,
                "loc_reduction_dbos_vs_temporal_pct": f"{(1037 - 663) / 1037 * 100:.1f}%",
                "baseline_ds_squad_loc": 458
            },
            "phase_0_module_references": {
                "s1_modal_cells_pr": "#1738 (OPEN)",
                "r7_ds_harden_status": "ds_squad_v2.py (RUN/state.json, worker adoption, quota classifier, heartbeat, ledger rows)"
            }
        },
        "recommendation": {
            "primary_engine_choice": "DBOS on SQLite (embedded library; zero daemons, zero ports, flawless 0-attempt KT3 quota parking)",
            "secondary_engine_role": "Temporal dev-server (benchmark / distributed multi-machine agent fleet expansion)",
            "dagu_role": "Static CLI batch DAGs and web approval forms only; rejected for dynamic agent loops",
            "plain_scripts_role": "Phase 0 zero-infra hardening (R7-DS-HARDEN, S1 #1738); retire baseline ds_squad.py thereafter"
        }
    }

    # Write summary
    summary_paths = [
        Path("research/experiments/on-workflow-roadmap-20261102/receipts/summary.json"),
        Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F6-ROADMAP/receipts/summary.json"),
        Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F6-ROADMAP/receipts/summary.json"),
        Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F10-ROADMAP-FINAL/receipts/summary.json"),
        Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F10-ROADMAP-FINAL/receipts/summary.json"),
    ]
    for p in summary_paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            json.dump(summary_data, f, indent=2)
        print(f"Wrote summary to {p}")

    # Compute manifest of all receipts in research/experiments/on-workflow-roadmap-20261102/receipts/
    receipt_dir = Path("research/experiments/on-workflow-roadmap-20261102/receipts")
    manifest = {}
    for fpath in sorted(receipt_dir.glob("*.json")):
        if fpath.name == "manifest.json":
            continue
        sha = sha256_file(fpath)
        manifest[fpath.name] = {
            "path": str(fpath),
            "sha256": sha,
            "sha256_12": sha[:12],
            "size_bytes": fpath.stat().st_size
        }

    manifest_paths = [
        Path("research/experiments/on-workflow-roadmap-20261102/receipts/manifest.json"),
        Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F6-ROADMAP/receipts/manifest.json"),
        Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F6-ROADMAP/receipts/manifest.json"),
        Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F10-ROADMAP-FINAL/receipts/manifest.json"),
        Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F10-ROADMAP-FINAL/receipts/manifest.json"),
    ]
    for p in manifest_paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            json.dump(manifest, f, indent=2)
        print(f"Wrote manifest to {p}")


if __name__ == "__main__":
    main()
