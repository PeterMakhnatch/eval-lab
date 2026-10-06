#!/usr/bin/env python3
"""Baseline v1 task sample: 12 MiMo Python code tasks (HAR-158; Peter 2026-10-02 21:10Z).

Eligible: ledger ``usable`` (``../python-task-ledger/ledger.csv``), sound
under the egress lock (``../har122-egress-lock/har146-locked-nop.csv``,
``locked_nop == sound``), and not one of the 20 G1 held-out tasks
(``../ovn-sft-v0/eval_tasks.csv``).

* ``CLEAN`` tasks with a clean pass in the HAR-143 relabel
  (``../har143-copy-check/relabel.csv``, label ``pass``): distinct
  repositories, taken in sha256(``SEED`` + task) order.
* ``NEVER_RUN`` tasks with no run in the relabel: known repository only, one
  per repository, none from a repository already picked. They are stratified
  by image size: the eligible pool is cut into ``BANDS`` equal-count size
  bands and each band gets an equal share (the remainder goes to the
  lightest bands). Within a band, repositories are taken in
  sha256(``SEED`` + repository) order and each repository's lightest task is
  used.

Usage (from the checkout root):
    uv run python research/experiments/baseline-v1/select.py
Writes ``tasks.csv`` next to this file.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXP = HERE.parent
LEDGER = EXP / "python-task-ledger/ledger.csv"
LOCKED = EXP / "har122-egress-lock/har146-locked-nop.csv"
HELDOUT = EXP / "ovn-sft-v0/eval_tasks.csv"
RELABEL = EXP / "har143-copy-check/relabel.csv"
RESULTS = Path.home() / "Developer/eval-lab-results"
CLEAN = 6
NEVER_RUN = 6
BANDS = 3
SEED = "baseline-v1:1"


def read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def task_id(number: str) -> str:
    return f"format-code-task-{number}"


def run_task(row: dict[str, str]) -> str:
    """The task a relabelled run ran, from its stored config/result."""
    trial = RESULTS / row["trial_dir"]
    text = "".join(
        (trial / name).read_text(errors="replace")
        for name in ("config.json", "result.json")
        if (trial / name).is_file()
    )
    numbers = set(re.findall(r"format-code-task-(\d{6})", text))
    if len(numbers) != 1:
        raise SystemExit(f"{row['trial_dir']}: task id not unique: {sorted(numbers)}")
    return task_id(numbers.pop())


def repo_key(project: str) -> str | None:
    if project.startswith("format-code-task-"):
        return None
    return project.rstrip("/").rsplit("/", 1)[-1].lower().replace("-", "_")


def rank(key: str) -> str:
    return hashlib.sha256(f"{SEED}:{key}".encode()).hexdigest()


def main() -> None:
    ledger = {row["task_id"]: row for row in read(LEDGER)}
    locked = {row["task_id"]: row["locked_nop"] for row in read(LOCKED)}
    heldout = {row["task"] for row in read(HELDOUT)}
    eligible = {
        name: row
        for name, row in ledger.items()
        if row["status"] == "usable" and locked.get(name) == "sound" and name not in heldout
    }
    runs = [(run_task(row), row["label"]) for row in read(RELABEL)]
    ran = {name for name, _ in runs}
    clean = sorted({name for name, label in runs if label == "pass"})
    missing = [name for name in clean if name not in eligible]
    picked: list[dict[str, str]] = []
    seen: set[str | None] = set()
    for name in sorted((name for name in clean if name in eligible), key=rank):
        key = repo_key(eligible[name]["project"]) or name
        if len(picked) == CLEAN or key in seen:
            continue
        seen.add(key)
        picked.append({**eligible[name], "stratum": "clean_pass"})
    if len(picked) < CLEAN:
        raise SystemExit(f"only {len(picked)} clean-pass tasks from distinct repositories")

    pool = sorted(
        (
            row
            for name, row in eligible.items()
            if name not in ran and repo_key(row["project"]) is not None
        ),
        key=lambda row: (float(row["image_mib"] or "inf"), row["task_id"]),
    )
    need = NEVER_RUN
    width = -(-len(pool) // BANDS)
    for band in range(BANDS):
        rows = pool[band * width : (band + 1) * width]
        quota = need // BANDS + (1 if band < need % BANDS else 0)
        lightest: dict[str, dict[str, str]] = {}
        for row in rows:
            lightest.setdefault(repo_key(row["project"]) or "", row)
        for key in sorted(lightest, key=rank):
            if quota == 0:
                break
            if key in seen:
                continue
            seen.add(key)
            picked.append({**lightest[key], "stratum": f"never_run_size_band_{band + 1}"})
            quota -= 1
        if quota:
            raise SystemExit(f"band {band + 1}: {quota} short")

    columns = (
        "task_id",
        "stratum",
        "project",
        "image_mib",
        "split",
        "run",
        "run_digest",
        "run_transform",
    )
    with (HERE / "tasks.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(picked)
    print(
        json.dumps(
            {
                "tasks": len(picked),
                "strata": Counter(row["stratum"] for row in picked),
                "clean_pass_tasks": len(clean),
                "clean_pass_not_eligible": missing,
                "eligible": len(eligible),
                "never_run_pool": len(pool),
                "repositories": len({row["project"] for row in picked}),
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
