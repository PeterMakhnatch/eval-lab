"""Build the HAR-209 weak-test coverage receipt from frozen and newly executed evidence.

    uv run python research/experiments/mimo-weak-tests-line-backfill-20261021/build.py [--check]

Inputs are read-only:

* the frozen v2.1 atlas (567-task cohort, primary reports, block and line adjudications);
* the line-backfill output root, one ``report.json`` per newly executed task.

The receipt never promotes a new survivor: a surviving line mutant is
``unreviewed`` until a semantic review batch accepts it, exactly as v2.1
requires. Missing evidence stays missing; a task with no report is listed as
remaining, never as a pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
ATLAS = REPO / "research/answer-keys/mimo-weak-tests-v2.1/atlas.jsonl"


def default_root() -> Path:
    """The line-backfill output root, which lives in the shared derived store.

    A linked worktree has no ``derived/`` of its own, so the shared checkout is
    the default; ``EVALLAB_DERIVED_ROOT`` or ``--root`` overrides it.
    """
    override = os.environ.get("EVALLAB_DERIVED_ROOT")
    if override:
        return Path(override) / "qa/ref-subset/line-backfill-265"
    shared = Path.home() / "Developer/eval-lab/derived"
    if shared.is_dir():
        return shared / "qa/ref-subset/line-backfill-265"
    return REPO / "derived/qa/ref-subset/line-backfill-265"


BACKFILL_ROOT = default_root()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def new_scan(root: Path, task_id: str) -> dict:
    """One executed report, reduced to the fields the receipt may quote."""
    report = json.loads((root / task_id / "report.json").read_text())
    grades = report.get("grades") or []
    controls = {
        label: next((g.get("reward") for g in grades if g.get("label") == label), None)
        for label in ("full", "nop")
    }
    mutants = report.get("mutants") or {}
    survivors = mutants.get("survivors") or []
    return {
        "task_id": task_id,
        "status": report.get("status"),
        "verdict": report.get("verdict"),
        "package_digest": report.get("package_digest"),
        "reference_sha256": (report.get("reference") or {}).get("sha256"),
        "controls": controls,
        "kept_fraction": report.get("kept_fraction"),
        "mutant_stage": {
            "status": mutants.get("status"),
            "verdict": mutants.get("verdict"),
            "tried": mutants.get("tried"),
            "killed": mutants.get("killed"),
            "survivors": len(survivors),
            "reason": mutants.get("reason") or "",
        },
        "survivors": [
            {
                "id": survivor.get("id"),
                "file": survivor.get("file"),
                "line": survivor.get("line"),
                "review": "unreviewed",
            }
            for survivor in survivors
        ],
        "report_sha256": sha256(root / task_id / "report.json"),
    }


def build(root: Path = BACKFILL_ROOT) -> dict:
    atlas = jsonl(ATLAS)
    if len(atlas) != 567:
        raise ValueError("the frozen cohort must contain 567 tasks")
    planned = [
        row["task_id"]
        for row in atlas
        if not row["line"]["scans"] and "line_pass_not_run_or_not_reported" in row["unknowns"]
    ]
    scans = [
        new_scan(root, task_id) for task_id in planned if (root / task_id / "report.json").is_file()
    ]
    executed = {scan["task_id"] for scan in scans}
    remaining = [task_id for task_id in planned if task_id not in executed]
    archived = {row["task_id"]: row for row in atlas}
    mismatches = []
    for scan in scans:
        row = archived[scan["task_id"]]
        if scan["package_digest"] != row["package_digest"]:
            mismatches.append(
                {"task_id": scan["task_id"], "field": "package_digest",
                 "executed": scan["package_digest"], "archived": row["package_digest"]}
            )  # fmt: skip
        archived_reference = (row["primary"].get("reference") or {}).get("sha256")
        if archived_reference and scan["reference_sha256"] != archived_reference:
            mismatches.append(
                {"task_id": scan["task_id"], "field": "reference_sha256",
                 "executed": scan["reference_sha256"], "archived": archived_reference}
            )  # fmt: skip
    survivors = [survivor for scan in scans for survivor in scan["survivors"]]
    atlas_scans = [scan for row in atlas for scan in row["line"]["scans"]]
    atlas_survivors = [s for scan in atlas_scans for s in scan["survivors"]]
    return {
        "schema": "har209.weak_test_coverage/v1",
        "cohort": {
            "tasks": len(atlas),
            "atlas": str(ATLAS.relative_to(REPO)),
            "atlas_sha256": sha256(ATLAS),
            "primary_reports": sum(row["primary"]["completion"] == "reported" for row in atlas),
            "primary_source_counts": dict(
                sorted(Counter(row["primary"]["source"] for row in atlas).items())
            ),
            "primary_status_counts": dict(
                sorted(Counter(row["primary"]["status"] for row in atlas).items())
            ),
            "primary_verdict_counts": dict(
                sorted(Counter(row["primary"]["verdict"] for row in atlas).items())
            ),
        },
        "candidate_adjudication": {
            "block_candidates": sum(row["primary"]["verdict"] == "fail" for row in atlas),
            "block_labels": dict(
                sorted(
                    Counter(
                        row["block"]["review"]["label"] for row in atlas if row["block"]["review"]
                    ).items()
                )
            ),
            "block_unreviewed": sum(
                row["primary"]["verdict"] == "fail" and not row["block"]["review"] for row in atlas
            ),
            "line_survivors": len(atlas_survivors),
            "line_survivor_labels": dict(
                sorted(Counter(survivor["review"]["label"] for survivor in atlas_survivors).items())
            ),
            "line_survivors_unreviewed": sum(
                survivor["review"]["label"] == "unreviewed" for survivor in atlas_survivors
            ),
            "limits": (
                "Block and line adjudications are instruction-level source reviews over recorded "
                "full=1/nop=0 controls on the bound package; they are not fresh replays or "
                "repaired-package certificates. Uncertain labels stay uncertain."
            ),
        },
        "line_coverage": {
            "planned_backfill_tasks": len(planned),
            "planned_sha256": hashlib.sha256(
                "".join(f"{task}\n" for task in planned).encode()
            ).hexdigest(),
            "frozen_line_scans": len(atlas_scans),
            "frozen_line_scans_by_source": dict(
                sorted(Counter(scan["source"] for scan in atlas_scans).items())
            ),
            "new_scans": len(scans),
            "new_scans_by_status": dict(sorted(Counter(scan["status"] for scan in scans).items())),
            "new_scans_by_verdict": dict(
                sorted(Counter(scan["verdict"] for scan in scans).items())
            ),
            "new_survivors": len(survivors),
            "new_survivors_review": dict(sorted(Counter(s["review"] for s in survivors).items())),
            "identity_binding": {
                "checked_fields": ["package_digest", "reference_sha256"],
                "mismatches": mismatches,
                "all_bound": not mismatches,
            },
            "remaining_tasks": len(remaining),
            "remaining_task_ids": remaining,
            "scans": sorted(scans, key=lambda scan: scan["task_id"]),
            "output_root": str(root),
            "limits": (
                "A new scan is one executed ref-subset run on the archived package with the archived "
                "reference patch; its survivors are unreviewed and are not findings. New scans are not "
                "ingested into a published atlas version, so they change no answer key."
            ),
        },
        "limits": [
            "No paid spend, model calls, Modal or cloud compute: local Docker only, one task per shared FIFO acquisition.",
            "Not every planned backfill task has a scan; remaining_task_ids is the exact outstanding set.",
            "Primary reports and adjudications are archival evidence bound to package digests; a rematerialized package needs its own controls.",
            "The PR #918 perf profile remains failed; see pr-918-profile.json.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare the committed receipt")
    parser.add_argument(
        "--root", type=Path, default=BACKFILL_ROOT, help="line-backfill output root"
    )
    args = parser.parse_args()
    text = json.dumps(build(args.root), indent=2, sort_keys=True) + "\n"
    target = HERE / "summary.json"
    if args.check:
        if not target.is_file() or target.read_text() != text:
            print(f"stale: {target}", file=sys.stderr)
            return 1
    else:
        target.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
