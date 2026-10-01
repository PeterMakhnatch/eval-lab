#!/usr/bin/env python3
"""Freeze the overnight SFT experiment's eval set (HAR-127 G1).

20 ``usable`` held-out tasks from the Python task ledger
(``../python-task-ledger/ledger.csv``), lightest image first, one per
repository, disjoint from every training-split task:

* the repository key (census ``project_key``, compared by last path segment,
  case- and ``-``/``_``-insensitive), the raw ``project_key`` and the census
  ``split_group`` appear in no training-split task;
* the task's project modules (``evallab.hidden_patch.project_modules``: what
  its hidden tests import and the packages its test files live in) share
  nothing with any training-split task's, neither side's repository key is
  the other side's module, and no training-split instruction or hidden test
  imports one of them or uses it as ``module.name`` (a same-repository task
  whose census repository is unknown);
* its instruction is no near-duplicate of a training-split instruction:
  word 5-shingle Jaccard below ``NEAR_DUP`` against all 1,047.

A task whose repository is unknown (``project_key`` is its own task id) or
whose tests name no project module is left out: disjointness could not be
shown. HAR-116's tasks are excluded. Every training candidate (the HAR-120
proposal, SFT passes from HAR-104/110/116/120) is a training-split task, so
checking against the whole training split covers them.

Usage (from the checkout root):
    uv run python research/experiments/ovn-sft-v0/select_eval.py
Writes ``eval_tasks.csv`` and ``contamination.json``; prints the sha256.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path

import pyarrow.parquet as pq

from evallab.hidden_patch import project_modules

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
LEDGER = ROOT / "research/experiments/python-task-ledger/ledger.csv"
CENSUS = ROOT / "research/experiments/har108-python-census/task_health.parquet"
SNAPSHOT = (
    Path.home()
    / "Developer/eval-lab/derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks"
)
SIZE = 20
NEAR_DUP = 0.3
SHINGLE = 5
HAR116 = {
    f"format-code-task-{n}"
    for n in [
        "000383",
        "002256",
        "002391",
        "001832",
        "001896",
        "002864",
        "001161",
        "000495",
        "001181",
        "000587",
        "000226",
        "000927",
        "000146",
        "002308",
        "002402",
    ]
}


def repo_key(project: str) -> str | None:
    if project.startswith("format-code-task-"):
        return None
    return project.rstrip("/").rsplit("/", 1)[-1].lower().replace("-", "_")


def shingles(text: str) -> frozenset[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return frozenset(tuple(words[i : i + SHINGLE]) for i in range(len(words) - SHINGLE + 1))


def jaccard(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


#: Top-level directories that name no package; ``usercase-test-coderl`` is
#: the synthetic hidden-test directory 218 tasks share.
GENERIC_ROOTS = frozenset(
    {
        "tests",
        "test",
        "testing",
        "t",
        "src",
        "lib",
        "docs",
        "doc",
        "examples",
        "scripts",
        "python",
        "usercase_test_coderl",
        "whole_repo_tests",
        "api",
        "app",
        "apps",
        "core",
        "server",
        "backend",
        "packages",
        "python_modules",
        "all",
    }
)
_DIFF_PATH = re.compile(r"^diff --git a/(\S+) b/", re.MULTILINE)


def patch_roots(task_dir: Path) -> frozenset[str]:
    """Package directories the hidden test patch's files live under.

    002209's tests patch ``pandas/tests/io/test_parquet.py``: the task is
    pandas, although its tests import only fastparquet (a dependency), which
    ``project_modules`` reports. A root counts when the path has a directory
    and the directory is not a generic one.
    """
    patch = (task_dir / "tests" / "test.patch").read_text(errors="replace")
    roots = set()
    for path in _DIFF_PATH.findall(patch):
        parts = path.split("/")
        root = parts[0].lower().replace("-", "_")
        if len(parts) >= 2 and root not in GENERIC_ROOTS:
            roots.add(root)
    return frozenset(roots)


def main() -> None:
    ledger = list(csv.DictReader(LEDGER.open()))
    census = {row["task_id"]: row for row in pq.read_table(CENSUS).to_pylist()}
    modules = {row["task_id"]: project_modules(SNAPSHOT / row["task_id"]) for row in ledger}
    roots = {row["task_id"]: patch_roots(SNAPSHOT / row["task_id"]) for row in ledger}
    text = {
        row["task_id"]: shingles((SNAPSHOT / row["task_id"] / "instruction.md").read_text())
        for row in ledger
    }
    train = [row for row in ledger if row["split"] == "train"]
    train_keys = {repo_key(row["project"]) for row in train} - {None}
    train_projects = {row["project"] for row in train}
    train_groups = {census[row["task_id"]]["split_group"] for row in train}
    train_modules = set().union(*(modules[row["task_id"]] for row in train))
    train_roots = set().union(*(roots[row["task_id"]] for row in train))
    train_source = {
        row["task_id"]: (SNAPSHOT / row["task_id"] / "instruction.md").read_text()
        + "\n"
        + (SNAPSHOT / row["task_id"] / "tests" / "test.patch").read_text(errors="replace")
        for row in train
    }

    def used_in_training(module: str) -> list[str]:
        """Training tasks whose instruction or hidden tests use ``module``.

        Catches a same-repository training task the census could not name
        (000141's instruction uses ``pytorch_lightning.Trainer``; 000338's
        tests ``import albumentations``); a version line or an issue link
        that only mentions the name does not count.
        """
        pattern = re.compile(
            rf"\bimport {re.escape(module)}\b|\bfrom {re.escape(module)}\b"
            rf"|\b{re.escape(module)}\.[A-Za-z_]"
        )
        return sorted(task for task, source in train_source.items() if pattern.search(source))

    def excluded(row: dict) -> str | None:
        task_id, key = row["task_id"], repo_key(row["project"])
        if task_id in HAR116:
            return "HAR-116 task"
        if key is None:
            return "repository unknown"
        if not modules[task_id]:
            return "tests name no project module"
        if key in train_keys or row["project"] in train_projects:
            return f"repository {key} has training-split tasks"
        if census[task_id]["split_group"] in train_groups:
            return "split_group shared with the training split"
        shared = modules[task_id] & (train_modules | train_keys | train_roots)
        if shared or key in train_modules or key in train_roots:
            return f"module shared with the training split: {sorted(shared) or key}"
        shared_roots = roots[task_id] & (train_modules | train_keys | train_roots)
        if shared_roots:
            return f"test files live in a training-split package: {sorted(shared_roots)}"
        identity = modules[task_id] | roots[task_id]
        users = sorted({task for module in identity for task in used_in_training(module)})
        if users:
            return f"training-split tasks use its modules: {users}"
        return None

    held = sorted(
        (row for row in ledger if row["split"] == "heldout" and row["status"] == "usable"),
        key=lambda row: (row["image_mib"] == "", float(row["image_mib"] or 0), row["task_id"]),
    )
    picked: list[dict] = []
    seen: set[str] = set()
    report: dict = {"near_dup_threshold": NEAR_DUP, "shingle_words": SHINGLE, "skipped": {}}
    for row in held:
        reason = excluded(row)
        if reason is None and repo_key(row["project"]) in seen:
            reason = "repository already picked"
        if reason is None:
            best = max(
                ((jaccard(text[row["task_id"]], text[t["task_id"]]), t["task_id"]) for t in train),
            )
            if best[0] >= NEAR_DUP:
                reason = f"near-duplicate instruction: {best[1]} Jaccard {best[0]:.3f}"
        if reason is not None:
            report["skipped"][row["task_id"]] = reason
            continue
        seen.add(repo_key(row["project"]))
        picked.append(row)
        if len(picked) == SIZE:
            break
    if len(picked) < SIZE:
        raise SystemExit(f"only {len(picked)} tasks pass")
    rows = [
        {
            "task": row["task_id"],
            "digest": row["run_digest"],
            "run": row["run"],
            "repo": row["project"],
            "image_mib": row["image_mib"],
        }
        for row in picked
    ]
    with (HERE / "eval_tasks.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    report["eval"] = {
        row["task_id"]: {
            "modules": sorted(modules[row["task_id"]]),
            "max_jaccard_vs_train": round(
                max(jaccard(text[row["task_id"]], text[t["task_id"]]) for t in train), 4
            ),
            "max_jaccard_pair": max(
                (jaccard(text[row["task_id"]], text[t["task_id"]]), t["task_id"]) for t in train
            )[1],
        }
        for row in picked
    }
    (HERE / "contamination.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    digest = hashlib.sha256((HERE / "eval_tasks.csv").read_bytes()).hexdigest()
    print("eval_tasks.csv sha256", digest)
    print("skipped before 20 found:", len(report["skipped"]))


if __name__ == "__main__":
    main()
