#!/usr/bin/env python3
"""TB4 TEST leak check for the seeker training lane (pE-SELF-6).

`research/answer-keys/tb4-audit-v1/tasks.jsonl` is the authoritative split: 41
`test` tasks and 25 `dev` tasks. This checks that no seeker training data or
experiment artifact carries a TEST task, and that the tasks the pilot actually
ran are DEV.

Matching is exact on task ids (a substring scan would flag the DEV task
`layout-config-recreation2` for containing the TEST task `layout-config-recreation`,
which the dataset already quarantines as an alias). The tokenless slugs are
checked as substrings, because that is what they are for.

Usage:
    uv run python research/experiments/seeker-sft-loss-target-20261021/leak_check.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
ANSWER_KEYS = REPO / "research/answer-keys/tb4-audit-v1/tasks.jsonl"
DATASET_DIR = REPO / "research/datasets/seeker-sft-v1"
PILOT_TASKS = (
    "cargo-flight-dispatch",
    "fin-saccr-rwa",
    "html-js-filter",
    "vf2-speedup-networkx",
    "uefi-bootkit",
    "vpp-loss-divergence",
)
SCAN_ROOTS = (
    DATASET_DIR,
    REPO / "research/experiments/seeker-sft-pilot-20261021",
    REPO / "research/experiments/seeker-sft-loss-target-20261021",
    Path.home() / "Developer/eval-lab-results/2026-10-21/seeker-sft-pilot",
    Path.home() / "Developer/eval-lab-results/2026-10-21/seeker-sft-loss-target",
)
# The dataset deliberately records the aliases it quarantines; those mentions are
# the guard itself, not a leak.
ALIAS_RECORD_FILES = {
    "README.md",
    "build.py",
    "leak-audit.json",
    "source_manifests.json",
    "tb4_dev_eval_cohort.json",
    "transfer_splits.json",
}


def _splits() -> tuple[set[str], set[str], set[str]]:
    rows = [
        json.loads(line)
        for line in ANSWER_KEYS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    test = {row["task"].rsplit("/", 1)[-1] for row in rows if row.get("split") == "test"}
    dev = {row["task"].rsplit("/", 1)[-1] for row in rows if row.get("split") == "dev"}
    slugs = {
        str(slug)
        for row in rows
        if row.get("split") == "test"
        for slug in (row.get("tokenless_slugs") or [])
    }
    return test, dev, slugs


def _training_task_ids() -> set[str]:
    ids: set[str] = set()
    for path in sorted(DATASET_DIR.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            task = row.get("task_id") or row.get("task")
            if isinstance(task, str) and task:
                ids.add(task.rsplit("/", 1)[-1])
    return ids


def main() -> int:
    test, dev, slugs = _splits()
    training = _training_task_ids()
    report: dict[str, Any] = {
        "answer_keys": ANSWER_KEYS.as_posix(),
        "test_tasks": len(test),
        "dev_tasks": len(dev),
        "training_task_ids": len(training),
        "training_test_task_hits": sorted(training & test),
        "training_dev_task_hits": sorted(training & dev),
        "training_other_task_ids": sorted(training - test - dev),
        "pilot_task_splits": {
            task: ("test" if task in test else "dev" if task in dev else "unknown")
            for task in PILOT_TASKS
        },
        "artifact_hits": [],
    }

    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix == ".pyc":
                continue
            if path.name == Path(__file__).name:
                continue
            # Replay evidence embeds the upstream task package itself; that is the
            # task's own text, not seeker training data.
            if "native/inputs/task" in path.as_posix():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            names = sorted(
                name
                for name in test
                if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", text)
            )
            slug_hits = sorted(slug for slug in slugs if slug and slug in text)
            if not names and not slug_hits:
                continue
            if path.name in ALIAS_RECORD_FILES and set(names) <= {"layout-config-recreation"}:
                continue
            report["artifact_hits"].append(
                {"path": path.as_posix(), "task_names": names, "slugs": slug_hits}
            )

    report["status"] = (
        "pass"
        if not report["training_test_task_hits"]
        and not report["artifact_hits"]
        and all(split == "dev" for split in report["pilot_task_splits"].values())
        else "fail"
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
