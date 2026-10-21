"""Build mimo-executed-clean-v1: a pool defined by executed evidence, not screen silence.

Sources (all already published in this repo, no new executions):
- research/experiments/mimo-clean-v7/manifest.json (executed repeat-3 controls,
  status cohorts unchanged-clean-controls / repaired / blocked / known-weak)
- research/experiments/mimo-defect-map-v1/findings.json (open supported findings
  on exact packages; repairs.json source qualification for repaired children)

Pool rule (exact package, keyed by task_id + package_digest):
1. clean-v7 status is `unchanged-clean-controls` or `repaired`;
2. strict controls on that exact package: reference rewards [1,1,1], nop [0,0,0];
3. no source-supported or differential-confirmed finding on that exact package
   in mimo-defect-map-v1 (an open finding excludes, even if controls pass);
4. repaired rows additionally need a defect-map repair row on the exact child
   package whose source_qualification is qualified (qualified_for_v7 /
   qualified / reference_nop_repeat3_pair_proved).

This is executed-evidence cleanliness for the stated control scope only. It is
not semantic certification and grants no training admission; the frozen 33
held-out task IDs stay excluded from training under every package version.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CLEAN_V7 = ROOT / "research/experiments/mimo-clean-v7/manifest.json"
DEFECT = ROOT / "research/experiments/mimo-defect-map-v1"


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def main() -> None:
    clean = json.loads(CLEAN_V7.read_bytes())
    findings = json.loads((DEFECT / "findings.json").read_bytes())["findings"]
    repairs = json.loads((DEFECT / "repairs.json").read_bytes())["repairs"]

    open_packages = {
        (row["task_id"], row["package_digest"])
        for row in findings
        if row.get("task_id")
        and row.get("package_digest")
        and row["evidence_status"] in {"source_supported_historical", "differential_confirmed"}
    }
    qualified_repairs = {}
    for row in repairs:
        qualification = row.get("source_qualification") or {}
        qualified_repairs[(row["task_id"], row["child_package_digest"])] = bool(
            qualification.get("qualified_for_v7")
            or qualification.get("qualified")
            or qualification.get("reference_nop_repeat3_pair_proved")
        )

    members, excluded = [], Counter()
    for row in clean["tasks"]:
        if row["status"] not in {"unchanged-clean-controls", "repaired"}:
            excluded[f"status_{row['status']}"] += 1
            continue
        controls = row.get("controls") or {}
        reference = (controls.get("reference") or {}).get("rewards")
        nop = (controls.get("nop") or {}).get("rewards")
        if reference != [1.0, 1.0, 1.0] or nop != [0.0, 0.0, 0.0]:
            excluded["controls_not_strict_ref111_nop000"] += 1
            continue
        if (row["task_id"], row["package_digest"]) in open_packages:
            excluded["open_supported_finding_on_exact_package"] += 1
            continue
        if row["status"] == "repaired" and not qualified_repairs.get(
            (row["task_id"], row["package_digest"])
        ):
            excluded["repair_not_source_qualified"] += 1
            continue
        members.append(
            {
                "record_id": row["record_id"],
                "task_id": row["task_id"],
                "package_digest": row["package_digest"],
                "harbor_digest": row.get("harbor_digest"),
                "verifier_digest": row.get("verifier_digest"),
                "cohort": row["status"],
                "controls": {"reference": reference, "nop": nop},
                "heldout_excluded_from_training": bool(
                    row.get("heldout_excluded_from_training")
                ),
            }
        )
    members.sort(key=lambda row: row["record_id"])

    training = [row for row in findings if row["category"] == "training_wrong_pass8"]
    pool_packages = {(row["task_id"], row["package_digest"]) for row in members}
    pool_tasks = {row["task_id"] for row in members}
    manifest: dict[str, Any] = {
        "schema": "mimo.executed-clean/v1",
        "dataset_id": "mimo-executed-clean-v1",
        "definition": (
            "Exact packages with executed evidence: clean-v7 status "
            "unchanged-clean-controls or repaired, strict repeat-3 controls "
            "(reference [1,1,1], nop [0,0,0]) on that exact package, no "
            "source-supported/differential-confirmed finding on that exact "
            "package in mimo-defect-map-v1, and (for repaired) a source-qualified "
            "repair row on the exact child package."
        ),
        "not_semantic_certification": True,
        "training_authorized_by_this_release": False,
        "counts": {
            "pool_packages": len(members),
            "pool_distinct_task_ids": len(pool_tasks),
            "by_cohort": dict(sorted(Counter(row["cohort"] for row in members).items())),
            "heldout_packages_in_pool": sum(
                row["heldout_excluded_from_training"] for row in members
            ),
            "excluded_by_first_failed_condition": dict(sorted(excluded.items())),
        },
        "training_pool_wrong_pass_overlap": {
            "source": "mimo-defect-map-v1 findings category training_wrong_pass8",
            "known_wrong_pass_rows": len(training),
            "rows_on_pool_exact_package": sum(
                (row.get("task_id"), row.get("package_digest")) in pool_packages
                for row in training
            ),
            "distinct_task_ids_overlapping_pool_by_id_only": sorted(
                {row["task_id"] for row in training if row.get("task_id")} & pool_tasks
            ),
            "interpretation": (
                "Wrong passes were executed on original packages; the pool holds "
                "repaired/qualified or control-clean packages. Task-ID overlap is "
                "not exact-package overlap and does not contaminate the pool, "
                "but those task IDs need the repaired grader for any regrade."
            ),
        },
        "members": members,
        "sources": [
            {"path": "research/experiments/mimo-clean-v7/manifest.json", "sha256": digest(CLEAN_V7.read_bytes())},
            {"path": "research/experiments/mimo-defect-map-v1/findings.json", "sha256": digest((DEFECT / "findings.json").read_bytes())},
            {"path": "research/experiments/mimo-defect-map-v1/repairs.json", "sha256": digest((DEFECT / "repairs.json").read_bytes())},
        ],
        "spend_usd": 0,
    }
    out = HERE / "manifest.json"
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest["counts"], sort_keys=True))
    print(json.dumps(manifest["training_pool_wrong_pass_overlap"], sort_keys=True))


if __name__ == "__main__":
    main()
