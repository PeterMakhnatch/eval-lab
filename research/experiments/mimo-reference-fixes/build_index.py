#!/usr/bin/env python3
"""Build research/experiments/mimo-reference-fixes/index.csv deterministically.

Seed: the 192 HAR-191 oracle:pass+nop:fail rows from the committed
python-task-ledger projection (oracle_sweep.csv, which already excludes the
moved-package task 002308). Merge: classified rows from this slice's resume
waves (observations CSVs written by the reused leak-oracle sweep).

Patch bytes are hidden-solution material: they are copied to
~/Developer/eval-lab-results/2026-10-09/mimo-reference-fixes/<task>/solution.patch
(outside the repo, never visible to an agent container) and index.csv carries
only the absolute patch_path + patch_sha256. Rows without a passing oracle
patch leave patch_path/patch_sha256 empty.

Usage:
  uv run python research/experiments/mimo-reference-fixes/build_index.py
"""

from __future__ import annotations

import csv
import hashlib
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
RESULTS = Path.home() / "Developer/eval-lab-results/2026-10-09/mimo-reference-fixes"

HAR191_PROJECTION = ROOT / "research/experiments/python-task-ledger/oracle_sweep.csv"
HAR191_TASKS = Path.home() / "Developer/eval-lab-results/2026-10-07/HAR-191-oracle-sweep/tasks"

# (observations csv, wave tasks dir, source tag) for this slice's waves.
WAVES: list[tuple[Path, Path, str]] = [
    (
        HERE / "observations-w1.csv",
        Path.home() / "Developer/eval-lab-results/2026-10-09/mimo-ref-fixes-sweep-w1/tasks",
        "sweep-2026-10-09",
    ),
    (
        HERE / "observations-w2.csv",
        Path.home() / "Developer/eval-lab-results/2026-10-09/mimo-ref-fixes-sweep-w2/tasks",
        "sweep-2026-10-09",
    ),
]

INDEX_COLUMNS = ("task_id", "label", "fix_commit", "patch_path", "patch_sha256", "source")


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _publish_patch(task_id: str, patch_bytes: Path) -> tuple[str, str]:
    destination = RESULTS / task_id / "solution.patch"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(patch_bytes, destination)
    return str(destination), _sha_file(destination)


def _har191_seed() -> list[dict]:
    with HAR191_PROJECTION.open(newline="", encoding="utf-8") as stream:
        rows = [r for r in csv.DictReader(stream) if r["label"] == "oracle:pass+nop:fail"]
    if len(rows) != 192:
        raise ValueError(f"Expected 192 HAR-191 pass rows, found {len(rows)}")
    index = []
    for row in rows:
        task_id = row["task_id"]
        patch_log = HAR191_TASKS / task_id / "oracle" / "solution-patch.stdout.log"
        patch_err = HAR191_TASKS / task_id / "oracle" / "solution-patch.stderr.log"
        if not patch_log.is_file() or patch_log.stat().st_size == 0:
            raise ValueError(f"{task_id}: missing/empty retained patch bytes")
        if patch_err.is_file() and patch_err.stat().st_size:
            raise ValueError(f"{task_id}: retained patch stderr is not empty")
        patch_path, patch_sha = _publish_patch(task_id, patch_log)
        index.append(
            {
                "task_id": task_id,
                "label": row["label"],
                "fix_commit": row["fix_commit"],
                "patch_path": patch_path,
                "patch_sha256": patch_sha,
                "source": "har191",
            }
        )
    return index


def _wave_rows() -> list[dict]:
    index = []
    seen: set[str] = set()
    for observations_csv, wave_tasks, source in WAVES:
        with observations_csv.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            task_id = row["task_id"]
            if task_id in seen:
                raise ValueError(f"Duplicate wave row for {task_id}")
            seen.add(task_id)
            patch_path = patch_sha = ""
            if row["label"] == "oracle:pass+nop:fail":
                patch_log = wave_tasks / task_id / "oracle" / "solution-patch.stdout.log"
                patch_err = wave_tasks / task_id / "oracle" / "solution-patch.stderr.log"
                if not patch_log.is_file() or patch_log.stat().st_size == 0:
                    raise ValueError(f"{task_id}: pass row without retained patch bytes")
                if patch_err.is_file() and patch_err.stat().st_size:
                    raise ValueError(f"{task_id}: pass row with non-empty patch stderr")
                patch_path, patch_sha = _publish_patch(task_id, patch_log)
            index.append(
                {
                    "task_id": task_id,
                    "label": row["label"],
                    "fix_commit": row["fix_commit"],
                    "patch_path": patch_path,
                    "patch_sha256": patch_sha,
                    "source": source,
                }
            )
    return index


def main() -> int:
    index = _har191_seed()
    seed_ids = {row["task_id"] for row in index}
    for row in _wave_rows():
        if row["task_id"] in seed_ids:
            raise ValueError(f"Wave reclassifies HAR-191 seed task {row['task_id']}")
        index.append(row)
    index.sort(key=lambda row: row["task_id"])
    output = HERE / "index.csv"
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(INDEX_COLUMNS))
        writer.writeheader()
        writer.writerows(index)
    passes = sum(1 for row in index if row["label"] == "oracle:pass+nop:fail")
    print(f"index.csv: {len(index)} rows ({passes} oracle:pass+nop:fail), sha256={_sha_file(output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
