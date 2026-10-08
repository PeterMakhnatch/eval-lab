#!/usr/bin/env python3
"""Build the cyber task ledger: one row per MiMo-V2.6-RL cyber task (1,000).

Reads the pinned Harbor snapshot plus the vendored agentleak ID lists in
``inputs/`` (see ``SOURCES.md``); writes ``ledger.csv``. Deterministic:
re-running reproduces the file byte for byte given the same snapshot and
inputs. Decisions live in ``src/evallab/cyber_ledger.py`` (pure, tested).

```bash
uv run python research/experiments/cyber-task-ledger/build.py
```
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))

from evallab.cyber_ledger import (  # noqa: E402
    DupPair,
    OverlapEntry,
    RelatedEntry,
    SecbenchEntry,
    decide_verdicts,
)
from evallab.storage.paths import shared_checkout_root  # noqa: E402

SNAPSHOT = "FineEnvs__MiMo-V2.6-RL-harbor-cyber@763882ade5fc"
VARIANTS = ROOT / "library/task-variants"
VARIANT_TRANSFORM = "cyber-instruction-submit@1"
TASK_PREFIX = "mimo-v2.6-rl/"

COLUMNS = (
    "task_id",
    "verdict",
    "reason",
    "evidence",
    "split_group",
    "project",
    "dup_twin",
    "variant_record",
)


def snapshot_tasks() -> Path:
    primary = shared_checkout_root(ROOT)
    path = primary / "derived/task-store/hf" / SNAPSHOT / "tasks"
    if not path.is_dir():
        raise SystemExit(f"pinned cyber snapshot missing: {path}")
    return path


def parse_inputs(inputs: Path):
    overlap: dict[str, OverlapEntry] = {}
    with open(inputs / "overlap.csv", newline="") as handle:
        for row in csv.DictReader(handle):
            ids = tuple(m.strip() for m in row["mimo_instance_ids"].split(";"))
            gym = tuple(c.strip() for c in row["cybergym_task_ids"].split(";"))
            for task in ids:
                overlap[task] = OverlapEntry(
                    canonical_bug_id=row["canonical_bug_id"],
                    cybergym_ids=gym,
                    match_type=row["match_type"],
                )
    suspects: set[str] = set()
    with open(inputs / "mimo_suspect_specs.csv", newline="") as handle:
        for row in csv.DictReader(handle):
            suspects.add(row["mimo_instance_id"])
    pairs: list[DupPair] = []
    with open(inputs / "mimo_duplicates.csv", newline="") as handle:
        for row in csv.DictReader(handle):
            ids = [m.strip() for m in row["mimo_instance_ids"].split(";")]
            if len(ids) != 2:
                raise ValueError(f"duplicate row is not a pair: {row}")
            pairs.append(
                DupPair(
                    canonical_bug_id=row["canonical_bug_id"],
                    first=ids[0],
                    second=ids[1],
                    identical=row["specs_identical"] == "True",
                )
            )
    related: dict[str, list[RelatedEntry]] = {}
    with open(inputs / "related_bugs.csv", newline="") as handle:
        for row in csv.DictReader(handle):
            entry = RelatedEntry(
                mimo_bug=row["mimo_bug"],
                cybergym_task_id=row["cybergym_task_id"],
                same_fix_commit=row["same_fix_commit"] == "True",
            )
            for task in (m.strip() for m in row["mimo_instance_ids"].split(";")):
                related.setdefault(task, []).append(entry)
    secbench: dict[str, list[SecbenchEntry]] = {}
    with open(inputs / "mimo_secbench.csv", newline="") as handle:
        for row in csv.DictReader(handle):
            entry = SecbenchEntry(
                secbench_id=row["secbench_instance_id"],
                split=row["secbench_split"],
                id_match=row["id_match"],
                same_fix=row["same_fix"] == "True",
                crash_check=row["crash_check"],
            )
            for task in (m.strip() for m in row["mimo_instance_ids"].split(";")):
                secbench.setdefault(task, []).append(entry)
    clean = {
        line.strip()
        for line in (inputs / "mimo_cyber_clean_ids.txt").read_text().splitlines()
        if line.strip()
    }
    fix_binary: set[str] = set()
    with open(inputs / "image_build_steps.csv", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["fix_binary"] == "True":
                fix_binary.add(row["mimo_instance_id"])
    return overlap, suspects, pairs, related, secbench, clean, fix_binary


def read_snapshot(tasks: Path):
    task_ids = sorted(p.name for p in tasks.iterdir() if p.is_dir())
    if len(task_ids) != 1000:
        raise ValueError(f"snapshot has {len(task_ids)} tasks, want 1000")
    projects: dict[str, str] = {}
    instruction_sha: dict[str, str] = {}
    for task in task_ids:
        with open(tasks / task / "task.toml", "rb") as handle:
            meta = tomllib.load(handle)["metadata"]
        crash_file: str = meta["expected_crash"]["file"]
        projects[task] = crash_file.split("/")[0]
        blob = (tasks / task / "instruction.md").read_bytes()
        instruction_sha[task] = hashlib.sha256(blob).hexdigest()
    return task_ids, projects, instruction_sha


def variant_records() -> dict[str, str]:
    """Latest ``cyber-instruction-submit@1`` record per task, repo-relative."""
    found: dict[str, tuple[str, str, str]] = {}
    if not VARIANTS.is_dir():
        return {}
    for record_path in sorted(VARIANTS.glob("mimo-v2.6-rl__arvo_*/*.json")):
        try:
            record = json.loads(record_path.read_text())
        except (OSError, ValueError):
            continue
        if record.get("transform") != VARIANT_TRANSFORM:
            continue
        name = record.get("task_name", "")
        if not name.startswith(TASK_PREFIX):
            continue
        task = name[len(TASK_PREFIX):]
        key = (str(record.get("status")), str(record.get("created_at")))
        if task not in found or key > (found[task][1], found[task][2]):
            found[task] = (str(record_path.relative_to(ROOT)), key[0], key[1])
    return {task: path for task, (path, _, _) in found.items()}


def main() -> None:
    inputs = HERE / "inputs"
    tasks = snapshot_tasks()
    overlap, suspects, pairs, related, secbench, clean, fix_binary = parse_inputs(inputs)
    task_ids, projects, instruction_sha = read_snapshot(tasks)
    rows = decide_verdicts(
        task_ids=task_ids,
        projects=projects,
        instruction_sha=instruction_sha,
        overlap=overlap,
        suspects=suspects,
        dup_pairs=pairs,
        related=related,
        secbench=secbench,
        clean=clean,
        fix_binary=fix_binary,
    )
    records = variant_records()
    for task, row in rows.items():
        row.variant_record = records.get(task, "")
    with open(HERE / "ledger.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS))
        writer.writeheader()
        for task in sorted(rows):
            row = rows[task]
            writer.writerow(
                {
                    "task_id": row.task_id,
                    "verdict": row.verdict,
                    "reason": row.reason,
                    "evidence": row.evidence,
                    "split_group": row.split_group,
                    "project": row.project,
                    "dup_twin": row.dup_twin,
                    "variant_record": row.variant_record,
                }
            )
    counts: dict[str, int] = {}
    for row in rows.values():
        counts[row.verdict] = counts.get(row.verdict, 0) + 1
    print(f"wrote ledger.csv: {len(rows)} rows {counts}")


if __name__ == "__main__":
    main()
