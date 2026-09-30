#!/usr/bin/env python3
"""Label HAR-115's trials, write ``results.json`` and record repair verdicts.

Every nop is labelled by the census classifier (HAR-113's ``results.graded``:
``evallab.task_health`` on the package that ran). A repair's "before" is its
census row (the HAR-113 nop that found it). Verdicts follow HAR-113: a repair
variant is ``validated`` when its nop is ``sound``, ``rejected`` when it is
``broken_environment``, otherwise it stays ``candidate`` with the evidence;
an earlier attempt replaced by a retry is ``rejected`` on its own nop.
Diagnoses (``har115-diag-*``) are summarised by job; ``har115-nop-*`` are
census nops and go into the census through ``census_update.py``.

Usage (from the worktree root; the name avoids shadowing HAR-113's ``results``):
    uv run python research/experiments/har115-census/outcomes.py [--verdicts]

``--verdicts`` appends the verdicts; without it only ``results.json`` is
written (verdicts are final, so they wait for the classifier they rely on).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "har113-variants"))

from common import ROOT, census, record_path  # noqa: E402
from record_verdicts import trial, verdict  # noqa: E402
from results import RUNS, graded, parent_row, trial_of  # noqa: E402

from evallab.task_variants import load_records  # noqa: E402

BY = "har115-census"
CREATED_BY = "har115-repair"


def main() -> None:
    write_verdicts = "--verdicts" in sys.argv[1:]
    rows = census()
    repairs = json.loads((HERE / "repairs.json").read_text())["tasks"]
    out: dict[str, dict] = {"repair": {}, "diagnosis": {}, "renop": {}}
    attempts: dict[str, list[str]] = {}
    for record in load_records(ROOT):
        if record.created_by == CREATED_BY:
            attempts.setdefault(record.task_name.split("/", 1)[1], []).append(record_path(record))
    counts: dict[str, int] = {}
    for task_id, entry in sorted(repairs.items()):
        short = task_id.removeprefix("format-code-task-")
        chain = {step["record"] for step in entry["steps"]}

        def nop_of(record: str, short: str = short) -> dict | None:
            job = f"har115-rnop-{short}-{Path(record).stem}"
            return graded(job) if (RUNS / job).is_dir() else None

        after = graded(entry["nop_job"]) if (RUNS / entry["nop_job"]).is_dir() else None
        row = {
            **entry,
            "before": parent_row(task_id, rows),
            "after": after,
            "superseded": {
                record: nop_of(record)
                for record in sorted(attempts.get(task_id, []))
                if record not in chain
            },
        }
        out["repair"][task_id] = row
        if after is None or not write_verdicts:
            continue
        status = {"sound": "validated", "broken_environment": "rejected"}.get(
            after["label"], "candidate"
        )
        evidence = (
            f"nop {trial(after)}: {after['label']} ({after['evidence']}); parent nop "
            f"{row['before'].get('job')}: {row['before'].get('label')}"
        )
        final = verdict(entry["record"], status, evidence, by=BY)
        counts[final] = counts.get(final, 0) + 1
        for record, nop in row["superseded"].items():
            if nop:
                verdict(
                    record,
                    "rejected",
                    f"nop {trial(nop)}: {nop['label']} ({nop['evidence']}); superseded by "
                    f"{entry['record']}",
                    by=BY,
                )
    for job in sorted(p.name for p in RUNS.glob("har115-diag*-*")):
        found = trial_of(job)
        if found is None or not (found / "agent" / "oracle.txt").is_file():
            continue
        text = (found / "agent" / "oracle.txt").read_text(errors="replace")
        listing, _, extra = text.partition("== extra:\n")
        out["diagnosis"][job] = {
            "task_id": f"format-code-task-{job.rsplit('-', 1)[1]}",
            "trial": found.name,
            "listing": listing.splitlines()[:80],
            "extra": extra.splitlines()[:60],
            "graded": graded(job),
        }
    for job in ("har115-nop-002649",):
        if (RUNS / job).is_dir():
            out["renop"][job] = graded(job)
    (HERE / "results.json").write_text(json.dumps(out, indent=1, default=str) + "\n")
    after = [v["after"]["label"] if v.get("after") else "not_run" for v in out["repair"].values()]
    print("repair", {label: after.count(label) for label in sorted(set(after))})
    print("verdicts", counts)


if __name__ == "__main__":
    main()
