#!/usr/bin/env python3
"""Qualify append-only TB2.1 test repairs and run one more mutant search (round 2).

Each repaired package is the frozen original package plus ``patches/<task>.patch``
applied to ``tests/test_outputs.py``. ``evallab.ref_subset.probe_task`` runs
unchanged with the original targets, limits and mutation seed, so the native
oracle (solve.sh), nop and full-reconstruction controls and the same seeded line
mutants are graded by the repaired tests.

    uv run python research/experiments/tb21-test-repairs-20261019/run.py prepare
    uv run python research/experiments/tb21-test-repairs-20261019/run.py run [TASK ...]
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("tasks", nargs="*")
    parser.add_argument("--source", type=Path, default=PRIOR)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.source)
    else:
        run(args.tasks or list(TARGETS))


if __name__ == "__main__":
    main()
