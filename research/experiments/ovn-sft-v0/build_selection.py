#!/usr/bin/env python3
"""Build the G3 SFT selection record (``evallab.sft_selection/1``).

A trial is selected when all of these hold:

1. ``counts.verdict == "counted_pass"`` (``evallab.counts`` via
   ``process-job``; the job is re-processed into a scratch directory with
   ``--no-ingest --no-publish`` so every source is judged by the same code);
2. its task is ``train`` in the sealed split
   (``../har81-mimo-sft/split.json``, ``c3df70a5…``; it agrees with the
   ledger on all 1,180 Python tasks) and ``usable`` in the Python task ledger;
3. Traces (HAR-128) labelled it ``clean`` and its kept window ends with the
   model's own completion (``ends_with_completion``: a window cut before a
   loop-break or budget stop would teach stopping mid-task); its
   ``cut_step_id``/``cut_file`` come from that label;
4. its source is admissible: proxy-captured, or ``reconstructed_validated``
   only when ``--qualification`` (``qualify_reconstruction.py``'s
   ``qualification.json``) says ``admit_reconstructed`` (Research-Harbor's
   04:45Z ruling on HAR-127); its sha256 is recorded in the selection. A
   captured trial must also reproduce exactly in that qualification: a
   ``complete`` coverage entry (every delivered call rebuilt byte for byte
   from ATIF; a harness re-ask ATIF does not record breaks this);
5. at most ``MAX_PER_TASK`` per task: captured before reconstructed, then
   the newer job, then trial name.

Every candidate that fails a rule is listed with its reason in
``selection_exclusions.json``.

Usage (from the checkout root):
    uv run python research/experiments/ovn-sft-v0/build_selection.py \\
        --job JOB_DIR ... --labels LABELS.jsonl [--captured JOB_NAME ...] \\
        [--qualification QUAL/qualification.json] --out DIR
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SPLIT = ROOT / "research/experiments/har81-mimo-sft/split.json"
LEDGER = ROOT / "research/experiments/python-task-ledger/ledger.csv"
MAX_PER_TASK = 2
SCHEMA = "evallab.sft_selection/1"


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def processed_trials(job: Path, scratch: Path) -> list[dict]:
    """Counts records for every trial of ``job``, re-processed by current code."""
    out = scratch / job.name
    subprocess.run(
        [
            sys.executable,
            "-m",
            "evallab.cli",
            "process-job",
            str(job),
            "--output-dir",
            str(out),
            "--no-ingest",
            "--no-publish",
        ],
        check=True,
        capture_output=True,
    )
    return [json.loads(path.read_text()) for path in sorted(out.glob("trial-*.json"))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--job", type=Path, action="append", required=True)
    parser.add_argument("--labels", type=Path, action="append", default=[])
    parser.add_argument("--captured", action="append", default=[], help="job names with capture")
    parser.add_argument("--qualification", type=Path, help="qualify_reconstruction.py output")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    admit_reconstructed = False
    reproduced: set[str] = set()
    if args.qualification is not None:
        qualification = json.loads(args.qualification.read_text())
        admit_reconstructed = qualification["admit_reconstructed"] is True
        reproduced = {c["trial"] for c in qualification["coverage"] if c["complete"]}
    split = json.loads(SPLIT.read_text())
    split_of = {entry["task_id"]: entry["split"] for entry in split["tasks"]}
    ledger = {row["task_id"]: row for row in csv.DictReader(LEDGER.open())}
    labels: dict[tuple[str, str], dict] = {}
    for path in args.labels:
        for line in path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                labels[(row["job"], row["trial"])] = row
    args.out.mkdir(parents=True, exist_ok=True)
    scratch = args.out / "processed"

    candidates, exclusions, not_passes = [], [], 0
    for job in args.job:
        for record in processed_trials(job.resolve(), scratch):
            task_id = (record.get("task_name") or "").rsplit("/", 1)[-1]
            trial = record.get("trial_name")
            key = (job.name, trial)
            if (record.get("counts") or {}).get("verdict") != "counted_pass":
                not_passes += 1
                continue
            base = {"job": job.name, "trial": trial, "task_id": task_id}
            reason = None
            if split_of.get(task_id) != "train":
                reason = f"split:{split_of.get(task_id)}"
            elif (ledger.get(task_id) or {}).get("status") != "usable":
                reason = f"ledger:{(ledger.get(task_id) or {}).get('status')}"
            elif key not in labels:
                reason = "traces:unlabelled"
            elif labels[key].get("clean") is not True:
                reason = f"traces:not_clean ({labels[key].get('reason')})"
            elif labels[key].get("ends_with_completion") is not True:
                reason = "traces:no_completion_in_kept_window"
            elif job.name not in args.captured and not admit_reconstructed:
                reason = "source:reconstruction_not_qualified"
            elif job.name in args.captured and trial not in reproduced:
                reason = "capture:reconstruction_differs"
            if reason is not None:
                exclusions.append({**base, "reason": reason})
                continue
            candidates.append({**base, "captured": job.name in args.captured, "label": labels[key]})

    # Preference within a task: captured first, then the newer job (job names
    # carry the card number), then trial name. Stable sorts, last key first.
    order = sorted(candidates, key=lambda c: c["trial"])
    order.sort(key=lambda c: c["job"], reverse=True)
    order.sort(key=lambda c: (c["task_id"], not c["captured"]))
    picked: list[dict] = []
    per_task: dict[str, int] = {}
    for candidate in order:
        if per_task.get(candidate["task_id"], 0) >= MAX_PER_TASK:
            exclusions.append(
                {
                    "job": candidate["job"],
                    "trial": candidate["trial"],
                    "task_id": candidate["task_id"],
                    "reason": f"cap:{MAX_PER_TASK}_per_task",
                }
            )
            continue
        per_task[candidate["task_id"]] = per_task.get(candidate["task_id"], 0) + 1
        picked.append(candidate)

    selection = {
        "schema": SCHEMA,
        "split_manifest_digest": split["manifest_digest"],
        "labels": {path.name: sha256(path) for path in args.labels},
        "qualification": sha256(args.qualification) if args.qualification else None,
        "trials": [
            {
                "job": c["job"],
                "trial": c["trial"],
                "source": "captured" if c["captured"] else "reconstructed_validated",
                # Traces' note: kept steps whose observation carries a Terminus warning.
                "format_warning_steps_kept": c["label"].get("format_warning_steps_kept"),
                "cut_step_id": c["label"].get("cut_step_id"),
                "cut_file": c["label"].get("cut_file"),
            }
            for c in picked
        ],
    }
    (args.out / "selection.json").write_text(json.dumps(selection, indent=1) + "\n")
    (args.out / "selection_exclusions.json").write_text(
        json.dumps(sorted(exclusions, key=lambda e: (e["job"], e["trial"] or "")), indent=1) + "\n"
    )
    print(
        "selected",
        len(picked),
        "tasks",
        len(per_task),
        "excluded passes",
        len(exclusions),
        "non-passes",
        not_passes,
    )
    print("selection sha256", sha256(args.out / "selection.json"))


if __name__ == "__main__":
    main()
