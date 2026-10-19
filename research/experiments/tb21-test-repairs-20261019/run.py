#!/usr/bin/env python3
"""Qualify append-only TB2.1 test repairs and run one more mutant search (round 2).

Each repaired package is the frozen original package plus ``patches/<task>.patch``
applied to ``tests/test_outputs.py``. ``evallab.ref_subset.probe_task`` runs
unchanged with the original targets, limits and mutation seed, so the native
oracle (solve.sh), nop and full-reconstruction controls and the same seeded line
mutants are graded by the repaired tests.

    uv run python research/experiments/tb21-test-repairs-20261019/run.py prepare
    uv run python research/experiments/tb21-test-repairs-20261019/run.py run [TASK ...]
    uv run python research/experiments/tb21-test-repairs-20261019/run.py summarize
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from evallab.ref_subset import probe_task
from evallab.registry import compute_task_digests

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
RUNS = REPO / "runs/tb21-repairs-20261019"
COHORT = REPO / "research/experiments/weak-tests-generalization-20261019/cohort.json"
PRIOR = Path(
    "/Users/petermakhnatch/Developer/eval-lab/.worktrees/traces-tb21-weak-tests-v1-20261019"
    "/runs/px1-tb21-weak-tests-v1-20261019/packages/tb21"
)
TARGETS = {
    "multi-source-data-merger": ["/app/merged_users.parquet", "/app/conflicts.json"],
    "sam-cell-seg": ["/app/convert_masks.py"],
    "cobol-modernization": ["/app/program.py"],
}
SEED = "tb21-weak-tests-v1"


def prepare(source: Path) -> None:
    frame = {row["task_id"]: row for row in json.loads(COHORT.read_text())["tb21"]["frame"]}
    for task in TARGETS:
        original = RUNS / "packages/original" / task
        if not original.is_dir():
            shutil.copytree(source / task, original, symlinks=True)
        digest = compute_task_digests(original).package
        if digest != frame[task]["package_digest"]:
            raise ValueError(f"{task}: package differs from the frozen frame")
        repaired = RUNS / "packages/repaired" / task
        if repaired.exists():
            shutil.rmtree(repaired)
        shutil.copytree(original, repaired, symlinks=True)
        subprocess.run(
            [
                "git",
                "apply",
                "--unsafe-paths",
                f"--directory={repaired.relative_to(REPO)}",
                str(HERE / "patches" / f"{task}.patch"),
            ],
            cwd=REPO,
            check=True,
        )


def run(tasks: list[str]) -> None:
    os.environ["EVALLAB_DERIVED_ROOT"] = str(REPO / "derived/parquet")
    os.environ["EVALLAB_DOCKER_LOCK_SLOTS"] = "2"
    for task in tasks:
        out = RUNS / "round2" / task
        if (out / "report.json").is_file():
            raise ValueError(f"refusing to overwrite completed evidence: {out}")
        report = probe_task(
            RUNS / "packages/repaired" / task,
            None,
            out,
            max_grades=24,
            mutants=8,
            targets=TARGETS[task],
            mutation_seed=SEED,
            repo_root=REPO,
        )
        print(
            json.dumps(
                {
                    "task": task,
                    "status": report["status"],
                    "verdict": report["verdict"],
                    "finished_at": datetime.now(UTC).isoformat(),
                }
            ),
            flush=True,
        )


# Round-1 survivors recorded by the 2026-10-19 survey (Traces campaign or Auditor retry).
ROUND1_SURVIVORS = {
    "multi-source-data-merger": [
        "1f604f9086",
        "812424b32a",
        "1e9ed0f32a",
        "8c6d91f8c1",
        "46fb731802",
    ],
    "sam-cell-seg": ["25f5b35db4", "175edfb9e4"],
    "cobol-modernization": ["6326967e40", "6d38c09872"],
}


def _ctrf(trial: str) -> dict[str, str]:
    path = REPO / trial / "verifier/ctrf.json"
    if not path.is_file():
        return {}
    tests = json.loads(path.read_text())["results"]["tests"]
    return {test["name"]: test["status"] for test in tests}


def summarize() -> None:
    tasks = {}
    for task in TARGETS:
        path = RUNS / "round2" / task / "report.json"
        if not path.is_file():
            tasks[task] = {"status": "not_run"}
            continue
        report = json.loads(path.read_text())
        grades = {grade["label"]: grade for grade in report["grades"]}
        oracle = grades["oracle"]
        oracle_tests = _ctrf(oracle["native"]["trials"][0])
        original = (RUNS / "packages/original" / task / "tests/test_outputs.py").read_text()
        mutants = report["mutants"]
        outcomes = {run["id"]: run["outcome"] for run in mutants["runs"]}
        tasks[task] = {
            "report": str(path.relative_to(REPO)),
            "controls": {label: grades[label]["reward"] for label in ("oracle", "nop", "full")},
            "oracle_tests": oracle_tests,
            "original_tests_pass_on_oracle": bool(oracle_tests)
            and all(
                status == "passed"
                for name, status in oracle_tests.items()
                if f"def {name.split('::')[-1]}(" in original
            ),
            "round1_survivors": {
                mid: outcomes.get(mid, "not_selected") for mid in ROUND1_SURVIVORS[task]
            },
            "round2": {
                "generated": mutants.get("generated"),
                "tried": mutants.get("tried"),
                "killed": mutants.get("killed"),
                "survivors": [
                    {key: survivor[key] for key in ("id", "operator", "file", "line", "edit")}
                    for survivor in mutants.get("survivors", [])
                ],
            },
        }
    (HERE / "results.json").write_text(json.dumps(tasks, indent=2, sort_keys=True) + "\n")
    print(json.dumps({task: row.get("round2", row.get("status")) for task, row in tasks.items()}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "summarize"))
    parser.add_argument("tasks", nargs="*")
    parser.add_argument("--source", type=Path, default=PRIOR)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.source)
    elif args.command == "summarize":
        summarize()
    else:
        run(args.tasks or list(TARGETS))


if __name__ == "__main__":
    main()
