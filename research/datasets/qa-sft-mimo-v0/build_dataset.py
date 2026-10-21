"""Build qa-sft-mimo-v0: leak-free SFT examples for task QA (MiMo only).

Inputs are reference-by-digest only: no hidden tests, solutions, reference
patches or trajectories are inlined. Sources are the published, merged
artifacts mimo-defect-map-v1 (atlas positives), mimo-executed-clean-v1
(executed-evidence negatives) and mimo-clean-v7 (control rewards).

Positives: one example per atlas weak task/package; target findings are the
task's source-supported atlas witnesses converted to qa.finding/v1
(src/evallab/qa/models.py Finding; load_findings_dir-compatible).
Negatives: one example per executed-clean pool package; target is an empty
finding list plus the executed control evidence that justifies it.

Split is by task_id (sha256 bucket, ~80/10/10); a task never crosses splits.
Hard rule: no tb4-audit-v1 data. This producer never reads that key, and it
fails if any emitted row references it.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DEFECT = ROOT / "research/experiments/mimo-defect-map-v1"
POOL = ROOT / "research/experiments/mimo-executed-clean-v1/manifest.json"
CLEAN = ROOT / "research/experiments/mimo-clean-v7/manifest.json"


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def split_for(task_id: str) -> str:
    bucket = int(hashlib.sha256(task_id.encode()).hexdigest()[:8], 16) % 10
    return "train" if bucket < 8 else "dev" if bucket == 8 else "test"


def finding_from_atlas(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "qa.finding/v1",
        "id": row["finding_id"],
        "subject": {
            "kind": "task",
            "task_id": row["task_id"],
            "task_digest": row["package_digest"],
        },
        "stage": "mimo-atlas-v2.1",
        "verdict": "fail",
        "category": "weak",
        "severity": "high",
        "fault": "eval",
        "summary": row.get("safe_summary"),
        "evidence_refs": [
            f"{ref['source_id']}:{ref['json_pointer']}" for ref in row.get("evidence", [])
        ],
        "reproducer": {
            "kind": "patch",
            "path": (row.get("program") or {}).get("source_program_id", row["finding_id"]),
            "expected_reward": 1.0,
        },
        "refutation": {"status": "unverified"},
        "reviewer": {"agent": "mimo-weak-tests-v2.1-atlas", "cost_usd": 0.0},
        "measurement": "measured",
        "root_cause": [row["primary_class"]["key"]] if row.get("primary_class") else [],
    }


def main() -> None:
    findings = json.loads((DEFECT / "findings.json").read_bytes())["findings"]
    inventory = {
        row["record_id"]: row
        for row in json.loads((DEFECT / "inventory.json").read_bytes())["tasks"]
    }
    pool = json.loads(POOL.read_bytes())
    clean_tasks = {
        row["record_id"]: row for row in json.loads(CLEAN.read_bytes())["tasks"]
    }

    atlas = [
        row
        for row in findings
        if row["category"] == "atlas114"
        and row["evidence_status"] == "source_supported_historical"
    ]
    by_package: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in atlas:
        by_package.setdefault((row["task_id"], row["package_digest"]), []).append(row)

    examples: list[dict[str, Any]] = []
    for (task_id, package), rows in sorted(by_package.items()):
        record_id = f"{task_id}@{package}"
        inv = inventory.get(record_id, {})
        examples.append(
            {
                "id": f"qa-sft-mimo-v0:pos:{record_id}",
                "split": split_for(task_id),
                "label": "positive",
                "task_id": task_id,
                "package_digest": package,
                "input": {
                    "task_ref": record_id,
                    "package_digest": package,
                    "harbor_digest": inv.get("harbor_digest"),
                    "verifier_digest": inv.get("verifier_digest"),
                    "note": "Task view by digest only; fetch the exact package via the task-variant custody contract. No tests/solutions inlined.",
                },
                "target": {"findings": [finding_from_atlas(row) for row in rows]},
                "label_source": "mimo-weak-tests-v2.1 atlas (source_supported_historical witnesses)",
            }
        )

    for member in pool["members"]:
        clean_row = clean_tasks.get(member["record_id"], {})
        controls = clean_row.get("controls") or {}
        examples.append(
            {
                "id": f"qa-sft-mimo-v0:neg:{member['record_id']}",
                "split": split_for(member["task_id"]),
                "label": "negative",
                "task_id": member["task_id"],
                "package_digest": member["package_digest"],
                "input": {
                    "task_ref": member["record_id"],
                    "package_digest": member["package_digest"],
                    "harbor_digest": member.get("harbor_digest"),
                    "verifier_digest": member.get("verifier_digest"),
                    "note": "Task view by digest only; fetch the exact package via the task-variant custody contract. No tests/solutions inlined.",
                },
                "target": {"findings": []},
                "executed_evidence": {
                    "cohort": member["cohort"],
                    "reference_rewards": (controls.get("reference") or {}).get("rewards"),
                    "nop_rewards": (controls.get("nop") or {}).get("rewards"),
                    "heldout_excluded_from_training": member[
                        "heldout_excluded_from_training"
                    ],
                },
                "label_source": "mimo-executed-clean-v1 (executed strict controls, no open supported finding)",
            }
        )

    blob = json.dumps(examples, sort_keys=True)
    assert "tb4-audit" not in blob and "tb4_audit" not in blob, "tb4-audit leakage"

    splits: dict[str, list[dict[str, Any]]] = {"train": [], "dev": [], "test": []}
    for example in examples:
        splits[example["split"]].append(example)
    bindings = {}
    for name, rows in splits.items():
        data = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows).encode()
        (HERE / f"{name}.jsonl").write_bytes(data)
        bindings[f"{name}.jsonl"] = {"sha256": digest(data), "rows": len(rows)}

    manifest = {
        "schema": "qa-sft-mimo-v0",
        "dataset_id": "qa-sft-mimo-v0",
        "counts": {
            "examples": len(examples),
            "positives": sum(row["label"] == "positive" for row in examples),
            "negatives": sum(row["label"] == "negative" for row in examples),
            "findings_in_positive_targets": sum(
                len(row["target"]["findings"]) for row in examples
            ),
            "by_split": {name: len(rows) for name, rows in splits.items()},
            "by_split_label": {
                name: dict(sorted(Counter(row["label"] for row in rows).items()))
                for name, rows in splits.items()
            },
        },
        "split_rule": "sha256(task_id) bucket ~80/10/10; no task_id in two splits",
        "tb4_audit_v1_check": "producer never reads tb4-audit-v1; emitted blob scanned for tb4-audit references: none",
        "training_authorized_by_this_release": False,
        "heldout_rule": "examples whose task is in the frozen 33 held-out IDs are flagged in executed_evidence/input sources and must be excluded from training use",
        "artifacts": bindings,
        "sources": [
            {"path": "research/experiments/mimo-defect-map-v1/findings.json", "sha256": digest((DEFECT / "findings.json").read_bytes())},
            {"path": "research/experiments/mimo-defect-map-v1/inventory.json", "sha256": digest((DEFECT / "inventory.json").read_bytes())},
            {"path": "research/experiments/mimo-executed-clean-v1/manifest.json", "sha256": digest(POOL.read_bytes())},
            {"path": "research/experiments/mimo-clean-v7/manifest.json", "sha256": digest(CLEAN.read_bytes())},
        ],
        "spend_usd": 0,
    }
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest["counts"], sort_keys=True))


if __name__ == "__main__":
    main()
