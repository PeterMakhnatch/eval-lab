#!/usr/bin/env python3
"""A1: classify every `format-code-task-*` package in the MiMo DEV set for grader hardening (local, no cloud).

For each package: strict DEV assignment, vendored reference patch (sha256 matches `references.csv`), the single-container
`tests/test.sh` shape the transform anchors on (proved by a dry `build_changes`), runner kind and `PYTEST_ADDOPTS`
handling, and from those: `full` (L1 + L3), `l1-only`, or `not-hardenable`, with the exact reason.

    classify.py --raw ~/Developer/research-context/bridge/2026-10-30-factory/raw/A
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "research/experiments/h1-grader-hardening-20261030"))

from evallab.grader_hardening import build_changes, report_gate_blocker  # noqa: E402
from evallab.production.corpus_queue import CorpusRegistry  # noqa: E402
from evallab.task_variants import VariantInvalid  # noqa: E402

BRIDGE = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/raw")
DATASET = "mimo-defect-map-v1"
FAMILY = "format-code-task-"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def classify() -> list[dict]:
    registry = CorpusRegistry(repo_root=REPO)
    references = {
        r["task_id"]: r
        for r in csv.DictReader(
            (REPO / "research/experiments/mimo-v6-full-fleet/references.csv").open()
        )
    }
    rows = [
        json.loads(line) for line in (BRIDGE / "SK-2/receipts/tasks.jsonl").read_text().splitlines()
    ]
    out = []
    for row in sorted(rows, key=lambda r: r["task_id"]):
        task = row["task_id"]
        if not task.startswith(FAMILY):
            continue
        package = Path(row["original_task"])
        test_sh = package / "tests/test.sh"
        record: dict = {
            "task_id": task,
            "package": str(package),
            "strict_dev": registry.assigned_split(DATASET, task_id=task) == "dev"
            and not registry.assigned_test_anywhere(task_id=task),
            "reference_patch": False,
            "test_sh_reset_list_shape": False,
            "test_sh_sha256": sha256(test_sh.read_bytes()) if test_sh.is_file() else None,
            "runner": None,
            "addopts": None,
            "anchors_apply": False,
        }
        ref = references.get(task)
        if ref is not None:
            patch = (REPO / ref["patch_path"]).read_bytes()
            record["reference_patch"] = sha256(patch) == ref["patch_sha256"]
            record["reference_patch_sha256"] = ref["patch_sha256"]
        record["test_sh_reset_list_shape"] = (
            test_sh.is_file() and "EOF_RESET_TEST_FILES" in test_sh.read_text()
        )
        blocker = report_gate_blocker(package) if record["test_sh_reset_list_shape"] else "n/a"
        if blocker is None:
            record["runner"], record["addopts"] = "pytest", "kept"
        elif blocker and "does not mention pytest" in blocker:
            record["runner"], record["addopts"] = "not pytest", "n/a"
        elif blocker and "clears PYTEST_ADDOPTS" in blocker:
            record["runner"], record["addopts"] = "pytest", "cleared by the wrapper"
        try:
            build_changes(package, expected_tests=1, allow_no_report_gate=True)
            record["anchors_apply"] = True
        except VariantInvalid as exc:
            record["anchor_error"] = str(exc)[:200]
        if not record["strict_dev"]:
            cls, why = "not-hardenable", "not strictly DEV: out of scope for this set"
        elif not record["reference_patch"]:
            cls, why = (
                "not-hardenable",
                "no vendored reference patch: the correct arm cannot be run",
            )
        elif not record["test_sh_reset_list_shape"] or not record["anchors_apply"]:
            cls, why = (
                "not-hardenable",
                "tests/test.sh is not the reset-list shape the transform anchors on",
            )
        elif blocker is not None:
            cls, why = "l1-only", blocker
        else:
            cls, why = "full", "pytest runner that keeps PYTEST_ADDOPTS: L1 + L3"
        record["class"], record["reason"] = cls, why
        out.append(record)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", type=Path, required=True)
    args = parser.parse_args()
    records = classify()
    args.raw.mkdir(parents=True, exist_ok=True)
    counts = {
        c: sum(r["class"] == c for r in records) for c in ("full", "l1-only", "not-hardenable")
    }
    (args.raw / "classification.json").write_text(
        json.dumps({"tasks": records, "counts": counts, "n": len(records)}, indent=1) + "\n"
    )
    lines = [
        "| task | strict DEV | reference | reset-list shape | runner | PYTEST_ADDOPTS | class | reason |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in records:
        lines.append(
            f"| {r['task_id'].removeprefix(FAMILY)} | {r['strict_dev']} | {r['reference_patch']} | "
            f"{r['test_sh_reset_list_shape']} | {r['runner']} | {r['addopts']} | {r['class']} | {r['reason']} |"
        )
    (args.raw / "classification.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(counts), len(records))


if __name__ == "__main__":
    main()
