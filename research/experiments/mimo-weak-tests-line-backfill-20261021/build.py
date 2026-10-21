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
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
ATLAS = REPO / "research/answer-keys/mimo-weak-tests-v2.1/atlas.jsonl"
ATLAS_SHA256 = "84cc524b654b944fcbf286f2fd2c189bff3ebf2c071e3d180fe0d311ca282450"
REVIEW_LABELS = {"required_gap", "equivalent_or_optional", "non_positive", "uncertain"}
GRADE_HEADER = re.compile(
    r"^== (?P<label>.+) reward=(?P<reward>\S+) rc=(?P<rc>\S+) "
    r"(?P<status>scored|unscored|timeout|error) \d+(?:\.\d+)?s$",
    re.MULTILINE,
)


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


def recorded_grades(path: Path) -> dict[str, dict]:
    """Read the authoritative grade headers, including post-summary line grades."""
    grades = {}
    for match in GRADE_HEADER.finditer(path.read_text()):
        row = match.groupdict()
        label = row.pop("label")
        if label in grades:
            raise ValueError(f"{path}: duplicate grade label {label}")
        grades[label] = {
            "reward": None if row["reward"] == "None" else float(row["reward"]),
            "rc": None if row["rc"] == "None" else int(row["rc"]),
            "status": row["status"],
        }
    return grades


def scored(grade: dict | None, reward: float) -> bool:
    return grade == {"reward": reward, "rc": 0, "status": "scored"}


def load_reviews(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    result = {}
    for review in json.loads(path.read_text())["reports"]:
        task_id = review["task_id"]
        if task_id in result:
            raise ValueError(f"duplicate reviewed task {task_id}")
        result[task_id] = review
    return result


def new_scan(root: Path, task_id: str, review: dict | None = None) -> dict:
    """Bind a report and its semantic reviews to the actual recorded controls."""
    report_path = root / task_id / "report.json"
    report = json.loads(report_path.read_text())
    if report.get("task_id") != task_id:
        raise ValueError(f"{task_id}: report belongs to another task")
    log_path = root / task_id / "grades.log"
    grades = recorded_grades(log_path)
    controls = {label: grades.get(label) for label in ("full", "nop")}
    usable_controls = scored(controls["full"], 1) and scored(controls["nop"], 0)
    mutants = report.get("mutants") or {}
    survivors = mutants.get("survivors") or []
    patch_hashes = {
        f"{kind}_patch_sha256": sha256(path) if path.is_file() else None
        for kind in ("minimal", "dropped")
        for path in (root / task_id / f"{kind}.patch",)
    }
    survivor_ids = {survivor["id"] for survivor in survivors}
    if len(survivor_ids) != len(survivors):
        raise ValueError(f"{task_id}: duplicate survivor")
    mutant_grades = {key[7:]: grade for key, grade in grades.items() if key.startswith("mutant:")}
    if mutants.get("tried", 0) != len(mutant_grades):
        raise ValueError(f"{task_id}: mutant count disagrees with grade log")
    if survivor_ids != {
        identifier for identifier, grade in mutant_grades.items() if scored(grade, 1)
    }:
        raise ValueError(f"{task_id}: survivor list disagrees with scored return-code-zero grades")
    if survivors and not usable_controls:
        raise ValueError(f"{task_id}: survivors lack usable full/nop controls")
    reviews = {}
    if review is not None:
        bindings = {
            "package_digest": report.get("package_digest"),
            "reference_sha256": (report.get("reference") or {}).get("sha256"),
            "report_sha256": sha256(report_path),
            "grades_log_sha256": sha256(log_path),
            **patch_hashes,
        }
        for field, value in bindings.items():
            if review.get(field) != value:
                raise ValueError(f"{task_id}: stale review {field}")
        for item in review["survivors"]:
            identifier = item["id"]
            if identifier in reviews or identifier not in survivor_ids:
                raise ValueError(f"{task_id}: duplicate or foreign survivor review {identifier}")
            if item["label"] not in REVIEW_LABELS or not item["reason"] or not item["evidence_refs"]:
                raise ValueError(f"{task_id}/{identifier}: incomplete semantic review")
            if item["grade"] != mutant_grades[identifier]:
                raise ValueError(f"{task_id}/{identifier}: review changes the recorded grade")
            reviews[identifier] = item
        block_review = review.get("block")
        if block_review is not None and (
            block_review.get("label") not in {"confirmed_weak", "neutral", "out_of_scope", "uncertain"}
            or not block_review.get("reason")
            or not block_review.get("evidence_refs")
            or not usable_controls
            or report.get("verdict") != "fail"
            or not patch_hashes["minimal_patch_sha256"]
            or not scored(grades.get("confirm"), 1)
        ):
            raise ValueError(f"{task_id}: incomplete block adjudication or unusable confirmation")
    return {
        "task_id": task_id,
        "status": report.get("status"),
        "verdict": report.get("verdict"),
        "package_digest": report.get("package_digest"),
        "reference_sha256": (report.get("reference") or {}).get("sha256"),
        "controls": controls,
        "usable_controls": usable_controls,
        "block_review": review.get("block") if review else None,
        "kept_fraction": report.get("kept_fraction"),
        "mutant_stage": {
            "status": mutants.get("status"),
            "verdict": mutants.get("verdict"),
            "tried": mutants.get("tried"),
            "killed": mutants.get("killed"),
            "survivors": len(survivors),
            "unscored": mutants.get("unscored"),
            "reason": mutants.get("reason") or "",
        },
        "survivors": [
            {
                "id": survivor.get("id"),
                "file": survivor.get("file"),
                "line": survivor.get("line"),
                "review": reviews.get(survivor["id"], {}).get("label", "unreviewed"),
            }
            for survivor in survivors
        ],
        "report_sha256": sha256(report_path),
        "grades_log_sha256": sha256(log_path),
        **patch_hashes,
    }


def build(root: Path = BACKFILL_ROOT) -> dict:
    atlas = jsonl(ATLAS)
    if len(atlas) != 567 or len({row["task_id"] for row in atlas}) != 567:
        raise ValueError("the frozen cohort must contain 567 distinct tasks")
    if sha256(ATLAS) != ATLAS_SHA256:
        raise ValueError("the frozen atlas no longer matches its accepted publication")
    planned = [
        row["task_id"]
        for row in atlas
        if not row["line"]["scans"] and "line_pass_not_run_or_not_reported" in row["unknowns"]
    ]
    plan_path = root / "resume-plan-20261021.json"
    plan = json.loads(plan_path.read_text())
    bindings = {row["task_id"]: row for row in plan["tasks"]}
    if len(bindings) != len(plan["tasks"]) or set(bindings) != set(planned):
        raise ValueError("the continuation plan must cover exactly the 265 distinct backfill tasks")
    review_paths = [
        path for path in [HERE / "backfill-reviews.json", *sorted((HERE / "review-batches").glob("*.json"))]
        if path.is_file()
    ]
    reviews = {}
    for path in review_paths:
        batch = load_reviews(path)
        if overlap := reviews.keys() & batch.keys():
            raise ValueError(f"{path}: duplicate task reviews across batches: {sorted(overlap)}")
        reviews.update(batch)
    scans = [
        new_scan(root, task_id, reviews.get(task_id))
        for task_id in planned if (root / task_id / "report.json").is_file()
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
        planned_reference = bindings[scan["task_id"]]["reference_sha256"]
        if archived_reference and archived_reference != planned_reference:
            raise ValueError(f"{scan['task_id']}: continuation reference differs from the archive")
        archived_reference = planned_reference
        if archived_reference and scan["reference_sha256"] != archived_reference:
            mismatches.append(
                {"task_id": scan["task_id"], "field": "reference_sha256",
                 "executed": scan["reference_sha256"], "archived": archived_reference}
            )  # fmt: skip
    if mismatches:
        raise ValueError(f"executed package/reference binding mismatch: {mismatches}")
    survivors = [survivor for scan in scans for survivor in scan["survivors"]]
    atlas_scans = [scan for row in atlas for scan in row["line"]["scans"]]
    atlas_survivors = [s for scan in atlas_scans for s in scan["survivors"]]
    exclusion_path = HERE / "scientific-exclusions.json"
    exclusion_inventory = json.loads(exclusion_path.read_text())
    if exclusion_inventory.get("schema") != "evallab.mimo_scientific_exclusions/v1":
        raise ValueError("unsupported scientific-exclusion inventory")
    review_sources = {
        str(path.relative_to(REPO)): sha256(path) for path in review_paths
    }
    if review_sources != {
        row["path"]: row["sha256"].removeprefix("sha256:")
        for row in exclusion_inventory["review_sources"]
    }:
        raise ValueError("scientific exclusions are stale against semantic review inputs")
    confirmed_exclusions = {
        (row["task_id"], row["package_digest"])
        for row in exclusion_inventory["exclusions"]
        if row["classification"] == "confirmed_defect"
    }
    required_exclusions = {
        (row["task_id"], row["package_digest"])
        for row in atlas if row["semantic_verdict"] == "confirmed_weak"
    } | {
        (scan["task_id"], scan["package_digest"]) for scan in scans
        if (scan["block_review"] or {}).get("label") == "confirmed_weak"
        or any(survivor["review"] == "required_gap" for survivor in scan["survivors"])
    }
    if missing := required_exclusions - confirmed_exclusions:
        raise ValueError(f"confirmed wrong-patch packages lack exclusions: {sorted(missing)}")
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
            "new_reports": len(scans),
            "new_scans": sum(scan["mutant_stage"]["status"] is not None for scan in scans),
            "new_scans_by_status": dict(sorted(Counter(scan["status"] for scan in scans).items())),
            "new_scans_by_verdict": dict(
                sorted(Counter(scan["verdict"] for scan in scans).items())
            ),
            "review_inputs": [
                {"path": str(path.relative_to(REPO)), "sha256": sha256(path)}
                for path in review_paths
            ],
            "new_survivors": len(survivors),
            "new_survivors_review": dict(sorted(Counter(s["review"] for s in survivors).items())),
            "new_survivors_unreviewed": sum(s["review"] == "unreviewed" for s in survivors),
            "plan_sha256": sha256(plan_path),
            "mutant_stage_status_counts": dict(
                sorted(Counter(scan["mutant_stage"]["status"] or "not_reported" for scan in scans).items())
            ),
            "line_stage_not_reported_task_ids": [
                scan["task_id"] for scan in scans if scan["mutant_stage"]["status"] is None
            ],
            "usable_control_pairs": sum(scan["usable_controls"] for scan in scans),
            "new_block_candidates": sum(scan["verdict"] == "fail" for scan in scans),
            "new_block_unreviewed": sum(
                scan["verdict"] == "fail" and not scan["block_review"] for scan in scans
            ),
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
                "Each report is one attempt on a distinct planned task, bound to its package and "
                "reference; retries are not extra tasks. Measured line stages, unavailable attempts "
                "and inapplicable stages remain separate. Survivors need hash-bound semantic "
                "adjudication and scored return-code-zero controls; unreviewed is not a finding."
            ),
        },
        "scientific_exclusions": {
            "path": str(exclusion_path.relative_to(REPO)),
            "sha256": sha256(exclusion_path),
            **exclusion_inventory["totals"],
            "confirmed_wrong_patch_package_pairs_checked": len(required_exclusions),
            "confirmed_wrong_patch_package_pairs_missing": [],
            "limits": (
                "Exact package exclusions and conservative holds are separate from execution "
                "grades. Unlisted repaired children receive no inherited certification; "
                "historical manifests and completed experiment plans are unchanged."
            ),
        },
        "limits": [
            "No paid spend, model calls, Modal or cloud compute: local Docker only, one task per shared FIFO acquisition.",
            "remaining_task_ids and line_stage_not_reported_task_ids identify every outstanding or unmeasured line stage.",
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
