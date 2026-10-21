"""Build HAR-209's exact-package scientific-use vetoes without executing controls.

    uv run --no-sync python research/experiments/mimo-weak-tests-line-backfill-20261021/build_exclusions.py

The frozen atlas, audited static defects, and optional backfill-reviews.json
are inputs, never rewritten. Unknowns and unmeasured child packages remain
conservative holds. A recorded control result never transfers by task ID.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import runpy
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

from evallab.registry import task_directory_digest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
ATLAS_REL = Path("research/answer-keys/mimo-weak-tests-v2.1/atlas.jsonl")
AUDIT_REL = HERE.relative_to(REPO) / "exclusion-audit.json"
SCHEMA = "evallab.mimo_scientific_exclusions/v1"
TERMINAL_RAW_DIGESTS = {
    "candidate-0036-software-data-engineering":
        "sha256:1cd36c1c322547754d4034c1f08a75581e3c7bebfa51f1fd58bebd75a4920572",
    "candidate-1789-security-appsec":
        "sha256:3bb4e1a6626adc35d5837ad7ecea65d89a288be20dd362697b751ef4809a17c2",
    "candidate-1702-ml-inference":
        "sha256:a2a1fda29150f53687f641535c6f83b5c47778895c0ad6a871e683130e514eaf",
}
STATIC_VERIFY = {
    "format-code-task-001694": "fail:vacuous-grader:compile-only",
    "format-code-task-001833": "fail:unfair-grader:non-idempotent",
    "format-code-task-000865": "fail:unfair-grader:literal-mismatch",
    "format-code-task-000803": "fail:unfair-grader:unstated-edge",
    "format-code-task-000838": "fail:unfair-grader:unstated-api",
}


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def require_hash(path: Path, expected: str) -> str:
    actual = sha256(path)
    if actual != "sha256:" + expected.removeprefix("sha256:"):
        raise ValueError(f"stale evidence hash: {path}")
    return actual


def add(entries: dict[tuple[str, str], dict], entry: dict) -> None:
    key = (entry["task_id"], entry["package_digest"])
    previous = entries.get(key)
    if previous is not None:
        if (
            previous["classification"] == "confirmed_defect"
            and entry["classification"] != "confirmed_defect"
        ):
            return
        entry["reason"] = previous["reason"] + " " + entry["reason"]
        entry["evidence"] = previous["evidence"] + entry["evidence"]
    entries[key] = entry


def exclusion(
    task_id: str,
    digest: str,
    classification: str,
    claim_status: str,
    defect_class: str,
    verify: str,
    reason: str,
    evidence: list[dict],
) -> dict:
    return {
        "task_id": task_id,
        "package_digest": digest,
        "classification": classification,
        "claim_status": claim_status,
        "defect_class": defect_class,
        "verify": verify,
        "reason": reason,
        "evidence": evidence,
    }


def atlas_exclusions(atlas: list[dict], atlas_hash: str) -> list[dict]:
    result = []
    for row in atlas:
        if row["semantic_verdict"] != "confirmed_weak":
            continue
        witness = row["witnesses"][0]
        result.append(
            exclusion(
                row["task_id"], row["package_digest"], "confirmed_defect", "confirmed",
                "accepted-required-gap", "fail:weak-tests", witness["reason"],
                [{
                    "kind": "reviewed_accepted_wrong_patch",
                    "path": str(ATLAS_REL), "sha256": atlas_hash,
                    "selector": {
                        "task_id": row["task_id"], "package_digest": row["package_digest"],
                        "semantic_verdict": "confirmed_weak",
                    },
                    "supported_witness_ids": [item["id"] for item in row["witnesses"]],
                    "example_witness": {
                        key: witness[key]
                        for key in ("id", "path", "sha256", "review_label", "controls")
                    },
                }],
            )
        )
    return result


def audited_code_exclusions(audit: dict, audit_hash: str, package_root: Path) -> list[dict]:
    result = []
    for case in audit["cases"]:
        task_id = case["task_id"]
        base = (case.get("base_phase_gap") or {}).get("verdict") == "confirmed"
        if task_id.startswith("candidate-") or not (
            case["verdict"] in ("confirmed", "unknown") or base
        ):
            continue
        partial = case["claim"] == "accepted_required_partial_fix"
        unknown = case["verdict"] == "unknown"
        classification = "conservative_hold" if unknown or partial else "confirmed_defect"
        status = "unknown" if unknown else "unmeasured_on_package" if partial else "confirmed"
        verify = (
            "unverified:semantic-hold" if classification == "conservative_hold"
            else "fail:base-regression-gap" if base else STATIC_VERIFY[task_id]
        )
        evidence = [{
            "kind": "claim_specific_adjudication", "path": str(AUDIT_REL),
            "sha256": audit_hash, "selector": {"task_id": task_id, "claim": case["claim"]},
            "recorded_verdict": case["verdict"], "base_phase_gap": case.get("base_phase_gap"),
        }]
        evidence.extend(
            {"kind": key, **case["evidence"][key]}
            for key in ("instruction", "hidden_tests", "historical_finding")
            if case["evidence"].get(key) is not None
        )
        manifests = case["manifest_records"]
        for generation in ("run", "v5", "v6"):
            version = "v6" if generation == "run" else generation
            digest = manifests["v6"]["run_digest"] if generation == "run" else manifests[version]["final_digest"]
            package_evidence = [*evidence, {
                "kind": "exact_manifest_identity",
                "path": f"research/experiments/mimo-clean-{version}/manifest.csv",
                "field": "run_digest" if generation == "run" else "final_digest",
                "task_id": task_id, "package_digest": digest,
            }]
            if generation != "run" and (base or partial):
                package = package_root / manifests[version]["package_path"]
                instruction_hash = require_hash(package / "instruction.md", case["evidence"]["instruction"]["sha256"])
                patch_hash = require_hash(package / "tests/test.patch", case["evidence"]["hidden_tests"]["sha256"])
                package_evidence.append({
                    "kind": "static_semantic_inputs_unchanged", "path": str(package),
                    "instruction_sha256": instruction_hash, "test_patch_sha256": patch_hash,
                    "execution_grade_transferred": False,
                })
            elif generation == "v6" and case.get("current_v6_static_bytes") is not None:
                package_evidence.append({
                    "kind": "static_semantic_inputs_unchanged",
                    "path": str(AUDIT_REL), "sha256": audit_hash,
                    "selector": {"task_id": task_id, "field": "current_v6_static_bytes"},
                    **case["current_v6_static_bytes"],
                })
            reason = case["reason"]
            if partial:
                reason = (
                    "Conservative hold, not a newly measured failed grade: original required-gap "
                    "evidence remains; unchanged instruction/test.patch do not establish repaired-package "
                    "semantic coverage. " + reason
                )
            elif unknown:
                reason = "Conservative hold only; the alleged semantic defect remains unknown. " + reason
            result.append(exclusion(
                task_id, digest, classification, status,
                "base-regression-gap" if base else case["claim"], verify, reason, package_evidence,
            ))
        # A manifest's run digest need not identify the retained source package.
        # Bind its actual bytes and audited semantic inputs independently.
        source = Path(case["evidence"]["instruction"]["path"]).parent
        source_name = tomllib.loads((source / "task.toml").read_text())["task"]["name"]
        if source_name.rsplit("/", 1)[-1] != task_id:
            raise ValueError(f"{task_id}: audited source package belongs to {source_name}")
        instruction_hash = require_hash(
            source / "instruction.md", case["evidence"]["instruction"]["sha256"]
        )
        patch_hash = require_hash(
            source / "tests/test.patch", case["evidence"]["hidden_tests"]["sha256"]
        )
        result.append(exclusion(
            task_id, task_directory_digest(source), classification, status,
            "base-regression-gap" if base else case["claim"], verify, reason,
            [*evidence, {
                "kind": "static_semantic_inputs_unchanged", "path": str(source),
                "instruction_sha256": instruction_hash, "test_patch_sha256": patch_hash,
                "execution_grade_transferred": False,
            }],
        ))
    return result


def terminal_exclusions(audit: dict, audit_hash: str) -> list[dict]:
    result = []
    for case in audit["cases"]:
        task_id = case["task_id"]
        if not task_id.startswith("candidate-"):
            continue
        unknown = case["verdict"] == "unknown"
        package = Path(case["evidence"]["instruction"]["path"]).parent
        digest = task_directory_digest(package)
        if digest != TERMINAL_RAW_DIGESTS[task_id]:
            raise ValueError(f"terminal raw package changed; no task-ID re-pin allowed: {package}")
        for key in ("instruction", "hidden_tests"):
            source = case["evidence"][key]
            require_hash(Path(source["path"]), source["sha256"])
        evidence = [{
            "kind": "claim_specific_adjudication", "path": str(AUDIT_REL),
            "sha256": audit_hash, "selector": {"task_id": task_id, "claim": case["claim"]},
            "recorded_verdict": case["verdict"],
        }]
        evidence.extend(
            {"kind": key, **case["evidence"][key]}
            for key in ("instruction", "hidden_tests", "historical_finding")
            if case["evidence"].get(key) is not None
        )
        verify = (
            "unverified:semantic-hold" if unknown
            else "fail:source-literal-grader" if "0036" in task_id
            else "env-broken:missing-bandit-metadata"
        )
        result.append(exclusion(
            task_id, digest, "conservative_hold" if unknown else "confirmed_defect",
            "unknown" if unknown else "confirmed", case["claim"], verify, case["reason"], evidence,
        ))
        if "0036" in task_id:
            for record in case["manifest_records"]["v7"]["records"]:
                selected_digest = record["package_digest"]
                result.append(exclusion(
                    task_id, selected_digest, "confirmed_defect", "confirmed", case["claim"],
                    "fail:source-literal-grader", case["reason"], [{
                        "kind": "same_literal_design_on_selected_package", "path": str(AUDIT_REL),
                        "sha256": audit_hash,
                        "selector": {"task_id": task_id, "manifest_records.v7.records.record_id": record["record_id"]},
                        "test_outputs_sha256": case["evidence"]["hidden_tests"]["sha256"],
                        "instruction_sha256": case["evidence"]["instruction"]["sha256"],
                        "passing_incorrect_patch_demonstrated": False,
                        "engine_behavior_certified": False,
                    }],
                ))
        for source in audit["source_file_hashes"]:
            relative = Path(source["path"])
            if relative.parent.as_posix() != f"library/task-variants/mimo-v2.6-rl__{task_id}":
                continue
            record_path = REPO / relative
            require_hash(record_path, source["sha256"])
            record = json.loads(record_path.read_text())
            if record["parent"]["digest"] != digest:
                raise ValueError(f"terminal repair is not a raw-parent sibling: {record_path}")
            result.append(exclusion(
                task_id, record["variant_digest"], "conservative_hold",
                "unknown" if unknown else "unmeasured_on_package",
                "uncomposed-environment-and-guard", "unverified:semantic-hold",
                "Conservative hold: environment-dependency and terminal-guard variants were derived "
                "separately from the raw parent. No successful reference plus genuine negative controls "
                "demonstrate this exact package as a composed repair; a parent defect is not a fresh "
                "failed grade for this child.", [{
                    "kind": "separate_single_transform_lineage", "path": str(relative),
                    "package_digest": record["variant_digest"],
                }, {
                    "kind": "composition_not_demonstrated", "path": str(AUDIT_REL),
                    "sha256": audit_hash, "selector": {"task_id": task_id},
                }],
            ))
    return result


def additional_holds(audit: dict, audit_hash: str, atlas: list[dict], atlas_hash: str) -> list[dict]:
    cases = {case["task_id"]: case for case in audit["cases"]}
    result = []
    for row in atlas:
        case = cases.get(row["task_id"])
        if (
            case is None
            or case["verdict"] != "unknown"
            or row["semantic_verdict"] != "not_established"
        ):
            continue
        result.append(exclusion(
            row["task_id"], row["package_digest"], "conservative_hold", "unknown", case["claim"],
            "unverified:semantic-hold", "Conservative hold only; no supported atlas witness establishes "
            "the alleged defect. " + case["reason"], [{
                "kind": "unresolved_atlas_adjudication", "path": str(ATLAS_REL), "sha256": atlas_hash,
                "selector": {"task_id": row["task_id"], "package_digest": row["package_digest"],
                             "semantic_verdict": row["semantic_verdict"]},
            }],
        ))
    for row in audit["expanded_repair_coverage"]:
        coverage = row["current_case_coverage"]
        if row["status"] != "blocked" or coverage["new_cases_measured"]:
            continue
        count = coverage["additional_required_wrong_cases_unmeasured"]
        result.append(exclusion(
            row["task_id"], row["package_digest"], "conservative_hold", "unmeasured_on_package",
            "expanded-required-gaps-unmeasured", "unverified:expanded-semantic-controls-pending",
            f"Historical selected-child controls cover only their declared wrong/correct cases. The {count} "
            "additional required atlas wrong cases are unmeasured on this exact child; do not transfer a "
            "task-ID-wide certification or infer that the child has failed an unrun case.", [{
                "kind": "expanded_repair_coverage_hold", "path": str(AUDIT_REL), "sha256": audit_hash,
                "selector": {"expanded_repair_coverage.task_id": row["task_id"], "package_digest": row["package_digest"]},
                "additional_required_wrong_cases_unmeasured": count,
                "historical_qualification_transferred": False,
            }],
        ))
    return result


def review_paths(explicit: Path | None = None) -> list[Path]:
    """One immutable review file or the initial file plus ordered batch files."""
    if explicit is not None:
        if not explicit.is_file():
            raise FileNotFoundError(explicit)
        return [explicit]
    return [
        path for path in [
            HERE / "backfill-reviews.json",
            *sorted((HERE / "review-batches").glob("*.json")),
        ] if path.is_file()
    ]


def backfill_exclusions(
    root: Path, reviews_path: Path | None, helpers: dict[str, Any]
) -> list[dict]:
    """Accept only reviewed wrong witnesses with current byte/score custody.

    Reuse the receipt builder's report/control/adjudication checks, then bind
    the actual package, reference, and complete witness patch bytes too.
    No reward or semantic label is generated here.
    """
    reviews = {}
    sources = {}
    for path in review_paths(reviews_path):
        if json.loads(path.read_text()).get("schema") != "mimo.line_backfill_reviews/v1":
            raise ValueError(f"unsupported backfill review schema: {path}")
        for task_id, review in helpers["load_reviews"](path).items():
            if task_id in reviews:
                raise ValueError(f"duplicate reviewed task {task_id}: {sources[task_id]} and {path}")
            reviews[task_id] = review
            sources[task_id] = path
    result = []
    for task_id, review in sorted(reviews.items()):
        source_path = sources[task_id]
        scan = helpers["new_scan"](root, task_id, review)
        positives = [item for item in review["survivors"] if item["label"] == "required_gap"]
        block = review.get("block")
        if block is not None and block["label"] == "confirmed_weak":
            positives.append({**block, "id": "block", "patch_path": review["minimal_patch_path"],
                              "patch_sha256": review["minimal_patch_sha256"]})
        if not positives:
            continue
        report_path = root / task_id / "report.json"
        log_path = root / task_id / "grades.log"
        for expected_path, field in ((report_path, "report_path"), (log_path, "grades_log_path")):
            if Path(review[field]).resolve() != expected_path.resolve():
                raise ValueError(f"{task_id}: foreign {field}")
        if not scan["usable_controls"] or review["controls"] != scan["controls"]:
            raise ValueError(f"{task_id}: supported witness lacks bound usable controls")
        report = json.loads(report_path.read_text())
        package = Path(report["package"])
        task_name = tomllib.loads((package / "task.toml").read_text())["task"]["name"]
        if task_name.rsplit("/", 1)[-1] != task_id:
            raise ValueError(f"{task_id}: source package belongs to {task_name}")
        package_digest = task_directory_digest(package)
        if package_digest != scan["package_digest"]:
            raise ValueError(f"{task_id}: source package bytes changed")
        require_hash(Path(report["reference"]["patch"]), review["reference_sha256"])
        grades = helpers["recorded_grades"](log_path)
        for item in positives:
            identifier = item["id"]
            witness_path = (
                root / task_id / "minimal.patch" if identifier == "block"
                else root / task_id / "mutants" / f"{identifier}.patch"
            )
            if Path(item["patch_path"]).resolve() != witness_path.resolve():
                raise ValueError(f"{task_id}/{identifier}: foreign witness path")
            witness_hash = require_hash(witness_path, item["patch_sha256"])
            grade = grades.get("confirm" if identifier == "block" else f"mutant:{identifier}")
            if not helpers["scored"](grade, 1) or item["grade"] != grade:
                raise ValueError(f"{task_id}/{identifier}: witness was not accepted by the bound grader")
            result.append(exclusion(
                task_id, package_digest, "confirmed_defect", "confirmed", "accepted-required-gap",
                "fail:weak-tests", item["reason"], [{
                    "kind": "reviewed_backfill_accepted_wrong_patch",
                    "path": str(source_path.relative_to(REPO)) if source_path.is_relative_to(REPO) else str(source_path),
                    "sha256": sha256(source_path), "selector": {"task_id": task_id, "witness_id": identifier},
                    "report_path": str(report_path), "report_sha256": sha256(report_path),
                    "grades_log_path": str(log_path), "grades_log_sha256": sha256(log_path),
                    "package_path": report["package"], "package_digest": package_digest,
                    "reference_sha256": "sha256:" + review["reference_sha256"].removeprefix("sha256:"),
                    "witness_path": str(witness_path), "witness_sha256": witness_hash,
                    "controls": scan["controls"], "witness_grade": grade,
                    "review_label": item["label"], "evidence_refs": item["evidence_refs"],
                    "execution_grade_transferred": False,
                }],
            ))
    return result


def build(package_root: Path, root: Path, reviews_path: Path | None = None) -> dict:
    helpers = runpy.run_path(str(HERE / "build.py"))
    atlas_path = REPO / ATLAS_REL
    atlas_hash = require_hash(atlas_path, helpers["ATLAS_SHA256"])
    atlas = helpers["jsonl"](atlas_path)
    audit_path = REPO / AUDIT_REL
    audit = json.loads(audit_path.read_text())
    audit_hash = sha256(audit_path)
    for binding in audit["manifest_identity"].values():
        require_hash(REPO / binding["path"], binding["sha256"])
    entries: dict[tuple[str, str], dict] = {}
    original = atlas_exclusions(atlas, atlas_hash)
    fresh = backfill_exclusions(root, reviews_path, helpers)
    for entry in [
        *original, *audited_code_exclusions(audit, audit_hash, package_root),
        *terminal_exclusions(audit, audit_hash),
        *additional_holds(audit, audit_hash, atlas, atlas_hash), *fresh,
    ]:
        add(entries, entry)
    return {
        "schema": SCHEMA,
        "issue": "HAR-209",
        "scope": "Exact task/package pairs excluded from accepted scientific use (training, scored "
                 "benchmark evaluation, and admission/certification). Diagnostic oracle/nop, mutation, "
                 "and repair controls remain allowed; this inventory is not dispatch or spend authorization.",
        "identity": "Full task ID plus registry.task_directory_digest; no prefix, task-ID-only, descendant, "
                    "or inherited certification match. Unknown and unmeasured-package holds are not confirmed defects.",
        "historical_evidence_unchanged": True,
        "fresh_execution_claimed": False,
        "review_sources": [
            {
                "path": str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path),
                "sha256": sha256(path),
            }
            for path in review_paths(reviews_path)
        ],
        "totals": {
            "exact_package_pairs": len(entries),
            "by_classification": dict(sorted(Counter(row["classification"] for row in entries.values()).items())),
            "by_claim_status": dict(sorted(Counter(row["claim_status"] for row in entries.values()).items())),
            "supported_original_atlas_packages_excluded": len(original),
            "fresh_review_supported_packages": len({(row["task_id"], row["package_digest"]) for row in fresh}),
            "fresh_review_supported_witnesses": len(fresh),
            "confirmed_base_regression_tasks": sum(
                (case.get("base_phase_gap") or {}).get("verdict") == "confirmed"
                for case in audit["cases"]
            ),
            "unresolved_task_ids": sorted(case["task_id"] for case in audit["cases"] if case["verdict"] == "unknown"),
        },
        "exclusions": [entries[key] for key in sorted(entries)],
    }


def main() -> None:
    helpers = runpy.run_path(str(HERE / "build.py"))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--package-root", type=Path, default=Path.home() / "Developer/eval-lab")
    parser.add_argument("--root", type=Path, default=helpers["BACKFILL_ROOT"])
    parser.add_argument(
        "--reviews", type=Path,
        help="replace default backfill-reviews.json plus sorted review-batches/*.json",
    )
    parser.add_argument("--output", type=Path, default=HERE / "scientific-exclusions.json")
    args = parser.parse_args()
    payload = build(args.package_root, args.root, args.reviews)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), **payload["totals"]}, sort_keys=True))


if __name__ == "__main__":
    main()
