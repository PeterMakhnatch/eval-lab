#!/usr/bin/env python3
"""Generate summary metrics and SHA256 manifest for F6-ROADMAP receipts."""

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
    with open("research/experiments/on-workflow-roadmap-20261102/receipts/evidence_summary.json") as f:
        ev = json.load(f)

    # KT1 DBOS crash trial stats
    kt1_k = 10
    kt1_n = 10
    kt1_low, kt1_high = wilson_interval(kt1_k, kt1_n)

    # Friction item coverage
    total_friction = ev["friction_item_count"]
    workflow_friction = total_friction - ev["tooling_environment_friction_count"]
    covered_by_top_5 = sum(item["friction_count"] for item in ev["top_addressed_workflows"])
    cov_low, cov_high = wilson_interval(covered_by_top_5, workflow_friction)

    summary_data = {
        "title": "F6-ROADMAP: Workflow Roadmap & Decision Memo Summary",
        "date": "2026-11-02",
        "key_metrics": {
            "total_tonight_friction_items": total_friction,
            "total_census_documented_incidents": ev["census_incident_count"],
            "total_empirical_evidence_points": ev["total_evidence_points"],
            "friction_covered_by_candidate_workflows": covered_by_top_5,
            "friction_coverage_ratio": f"{covered_by_top_5}/{total_friction} ({covered_by_top_5/total_friction*100:.1f}%)",
            "friction_coverage_95_wilson": [round(cov_low, 3), round(cov_high, 3)],
            "dbos_kt1_crash_survival_rate": f"{kt1_k}/{kt1_n} (100.0%)",
            "dbos_kt1_95_wilson": [round(kt1_low, 3), round(kt1_high, 3)],
            "dbos_quota_resume_tolerance_sec": 0.243,
            "temporal_squad_loc_core": 1037,
            "dbos_squad_loc_core": 663,
            "loc_reduction_dbos_vs_temporal_pct": f"{(1037 - 663) / 1037 * 100:.1f}%",
            "dagu_native_kt3_attempts_before_T": 6,
            "dbos_native_kt3_attempts_before_T": 0
        },
        "recommendation": {
            "primary_engine_choice": "DBOS on SQLite (embedded library)",
            "secondary_engine_role": "Temporal for distributed multi-machine or rich UI requirements",
            "dagu_role": "Static CLI batch DAGs only; rejected for dynamic agent squads",
            "plain_scripts_role": "Phase 0 zero-infra hardening; rejected as long-term stateful supervisor"
        }
    }

    # Write summary
    summary_paths = [
        Path("research/experiments/on-workflow-roadmap-20261102/receipts/summary.json"),
        Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F6-ROADMAP/receipts/summary.json"),
        Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F6-ROADMAP/receipts/summary.json")
    ]
    for p in summary_paths:
        with open(p, "w") as f:
            json.dump(summary_data, f, indent=2)
        print(f"Wrote summary to {p}")

    # Compute manifest of all receipts in research/experiments/on-workflow-roadmap-20261102/receipts/
    receipt_dir = Path("research/experiments/on-workflow-roadmap-20261102/receipts")
    manifest = {}
    for fpath in sorted(receipt_dir.glob("*.json")):
        sha = sha256_file(fpath)
        manifest[fpath.name] = {
            "sha256": sha,
            "sha256_12": sha[:12],
            "bytes": fpath.stat().st_size
        }

    manifest_paths = [
        receipt_dir / "manifest.json",
        Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F6-ROADMAP/receipts/manifest.json"),
        Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F6-ROADMAP/receipts/manifest.json")
    ]
    for p in manifest_paths:
        with open(p, "w") as f:
            json.dump(manifest, f, indent=2)
        print(f"Wrote manifest to {p}")

if __name__ == "__main__":
    main()
