#!/usr/bin/env python3
"""Profile the local CUAVerifierBench slice; emit counts/hashes only, never corpus text."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from adapter import (
    DATA_FILES,
    INPUT_COLUMNS,
    MODEL,
    REPO,
    REVISION,
    ROOT,
    SPLIT,
    blind_gold,
    file_sha256,
    load_reviews,
    selfcheck,
    trace_key,
    trajectory_rows,
)

OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"
EXPECTED_DIGESTS = {
    "README.md": "44d7190b72ae489297f59d6fadff635ced6dd067c845c77b04f187a7b2d7bec7",
    DATA_FILES[0]: "10d459cf76e888de082534be156f60974176617242ce5ebdaac554461468ab84",
    DATA_FILES[1]: "00f6083c3465d9644a51ec6279d6bc13795375d2c5f6deb00e2322210076654a",
    DATA_FILES[2]: "79b22396664658e548c2c0ef3e4e5b28760265cf701724bb6a1edae6ae47122e",
}


def publisher_counts(root: Path) -> dict[str, Any]:
    """Read only the selected-split count table; do not reproduce any card prose."""
    readme = (root / "README.md").read_text(encoding="utf-8")
    table_rows = [
        line
        for line in readme.splitlines()
        if SPLIT in line and "|" in line and re.search(r"\b106\b", line)
    ]
    if len(table_rows) != 1:
        raise ValueError("publisher split-count table must have exactly one selected row")
    numbers = [int(value) for value in re.findall(r"(?<![\w.])\d+(?![\w.])", table_rows[0])]
    if numbers[:2] != [106, 215]:
        raise ValueError("publisher selected-split counts differ from vetted counts")
    if "MIT License" not in readme:
        raise ValueError("publisher MIT License declaration is missing")
    return {"trajectory_rows": numbers[0], "annotation_rows": numbers[1]}


def file_receipt(root: Path) -> dict[str, Any]:
    manifest = json.loads((root / "STAGE-MANIFEST.json").read_text(encoding="utf-8"))
    if (manifest["repo"], manifest["revision"], manifest["licence"]) != (
        REPO,
        REVISION,
        "mit",
    ):
        raise ValueError("bundle identity or license differs from the vetted pin")
    if manifest["gated"] or manifest["gate_terms"]:
        raise ValueError("unexpected gate or terms")
    if {item["path"] for item in manifest["files"]} != set(EXPECTED_DIGESTS):
        raise ValueError("bundle must contain only the four approved publisher files")
    files = []
    for item in manifest["files"]:
        path = root / item["path"]
        digest = file_sha256(path)
        if digest != EXPECTED_DIGESTS[item["path"]] or path.stat().st_size != item["bytes"]:
            raise ValueError("a pinned file digest or size differs")
        if digest != item["sha256_computed"]:
            raise ValueError("a file digest differs from the staging manifest")
        if item["sha256"] is None:
            data = path.read_bytes()
            blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
            if blob != item["blob_id"]:
                raise ValueError("README Git blob differs from the publisher manifest")
        elif digest != item["sha256"]:
            raise ValueError("Parquet digest differs from the publisher LFS digest")
        files.append(
            {
                "path": item["path"],
                "bytes": path.stat().st_size,
                "sha256": digest,
                "git_blob": item["blob_id"],
                "verified_against": item["verified_against"],
            }
        )
    if not (root / "CROSS_TEST_ONLY.md").is_file():
        raise ValueError("cross-test restriction notice is missing")
    paths = [root, *root.rglob("*")]
    writable_paths = sum(bool(path.stat().st_mode & 0o222) for path in paths)
    if writable_paths:
        raise ValueError("bundle has writable paths; lock it before profiling")
    expected_paths = set(EXPECTED_DIGESTS) | {"STAGE-MANIFEST.json", "CROSS_TEST_ONLY.md"}
    actual_paths = {str(path.relative_to(root)) for path in paths if path.is_file()}
    if actual_paths != expected_paths:
        raise ValueError("bundle contains files outside the approved set and receipts")
    total_bytes = sum(item["bytes"] for item in files)
    if total_bytes != manifest["total_bytes"] or total_bytes != 875_867_136:
        raise ValueError("publisher file bytes differ from the vetted total")
    return {
        "files": files,
        "publisher_files": len(files),
        "publisher_bytes": total_bytes,
        "all_local_files": len(actual_paths),
        "all_local_bytes": sum(path.stat().st_size for path in paths if path.is_file()),
        "writable_paths": writable_paths,
        "verified_after_lock": True,
        "staging_tool_sha256": manifest["tool_sha256"],
    }


def profile(root: Path = ROOT) -> dict[str, Any]:
    receipt = file_receipt(root)
    expected = publisher_counts(root)
    reviews = load_reviews(root)
    columns = [
        *INPUT_COLUMNS,
        "majority_human_outcome_vote",
        "final_human_outcome_label",
        "n_screenshots",
        "is_aborted",
    ]
    ids: list[str] = []
    per_task = {}
    gold_counts: Counter[str] = Counter()
    process_counts: Counter[str] = Counter()
    outcome_counts: Counter[str] = Counter()
    text_sizes: Counter[str] = Counter()
    review_histogram: Counter[str] = Counter()
    reviewer_counts: Counter[str] = Counter()
    process_scores: list[float] = []
    screenshots: list[int] = []
    majority_mismatches = final_mismatches = ties = aborted = 0
    final0_majority1 = final1_majority0 = 0
    for row in trajectory_rows(root, columns):
        record_id = row["task_id"]
        ids.append(record_id)
        task_reviews = reviews[record_id]
        gold = blind_gold(task_reviews)
        key = trace_key(record_id)
        per_task[key] = {"runs": 1, "reviews": len(task_reviews)}
        review_histogram[str(len(task_reviews))] += 1
        gold_counts["correct" if gold["correct"] else "incorrect"] += 1
        majority_mismatches += row["majority_human_outcome_vote"] != int(gold["correct"])
        final_mismatches += row["final_human_outcome_label"] != int(gold["correct"])
        final0_majority1 += row["final_human_outcome_label"] == 0 and gold["correct"]
        final1_majority0 += row["final_human_outcome_label"] == 1 and not gold["correct"]
        ties += gold["tie"]
        screenshots.append(row["n_screenshots"])
        aborted += bool(row["is_aborted"])
        for field in ("instruction", "init_url", "web_surfer_log", "final_answer"):
            text_sizes[field] += len(row[field] or "")
        for review in task_reviews:
            outcome_counts[review["human_judgement_outcome"]] += 1
            process_value = review["human_judgement_process"]
            if process_value not in {"Correct", "Incorrect"}:
                raise ValueError("public process label is not a vetted binary value")
            process_counts[process_value] += 1
            process_scores.append(review["human_process_score"])
            reviewer_counts[review["annotator"]] += 1
    row_count = len(ids)
    review_count = sum(len(items) for items in reviews.values())
    checks = {
        "publisher_trajectory_count": row_count == expected["trajectory_rows"],
        "publisher_annotation_count": review_count == expected["annotation_rows"],
        "trajectory_ids_unique": len(set(ids)) == row_count,
        "trace_keys_unique": len(per_task) == row_count,
        "annotation_task_ids_equal_trajectory_ids": set(reviews) == set(ids),
        "blind_majority_matches_released_aggregate": majority_mismatches == 0,
        "vetted_run_label_counts": dict(gold_counts) == {"incorrect": 77, "correct": 29},
        "vetted_review_outcome_counts": dict(outcome_counts) == {"Incorrect": 134, "Correct": 81},
        "vetted_final_disagreement_count": final_mismatches == 14,
        "process_scores_in_unit_interval": all(
            isinstance(value, (int, float)) and 0 <= value <= 1 for value in process_scores
        ),
    }
    adapter_check = selfcheck(root)
    checks["adapter_all_rows_zero_issues"] = not adapter_check["issues"]
    result = {
        "schema": "pq1-cuaverifierbench-profile/v1",
        "repo": REPO,
        "revision": REVISION,
        "split": SPLIT,
        "evaluation_kind": "fixed_run_correctness_wrong_pass_not_cheating",
        "file_receipt": receipt,
        "publisher_claims_selected_split": expected,
        "rows": row_count,
        "annotations": review_count,
        "positives_incorrect": gold_counts["incorrect"],
        "negatives_correct": gold_counts["correct"],
        "unlabeled": 0,
        "per_label": {
            "human_judgement_outcome": dict(sorted(outcome_counts.items())),
            "human_judgement_process": dict(sorted(process_counts.items())),
            "majority_human_outcome_vote": dict(sorted(gold_counts.items())),
        },
        "per_model": {MODEL: row_count},
        "per_generator": {"Fara-7B / Browserbase / Online-Mind2Web": row_count},
        "per_task_hashed": dict(sorted(per_task.items())),
        "reviews_per_task_histogram": dict(sorted(review_histogram.items())),
        "reviewers": len(reviewer_counts),
        "per_reviewer_counts_sorted_without_ids": sorted(reviewer_counts.values()),
        "duplicate_task_reviewer_pairs": review_count
        - len(
            {
                (task_id, review["annotator"])
                for task_id, items in reviews.items()
                for review in items
            }
        ),
        "sorted_record_ids_sha256": hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest(),
        "sorted_record_ids_digest_encoding": "UTF-8 sorted task_id values, newline-joined, no trailing newline",
        "blind_majority_ties_to_incorrect": ties,
        "blind_majority_aggregate_mismatches": majority_mismatches,
        "final_adjudication_majority_disagreements": final_mismatches,
        "final_incorrect_majority_correct": final0_majority1,
        "final_correct_majority_incorrect": final1_majority0,
        "human_process_score": {
            "non_null": len(process_scores),
            "min": min(process_scores),
            "max": max(process_scores),
            "distinct": len(set(process_scores)),
        },
        "sizes": {
            "input_text_characters_by_field": dict(sorted(text_sizes.items())),
            "screenshots_retained_in_source_only": sum(screenshots),
            "screenshots_per_run_min": min(screenshots),
            "screenshots_per_run_max": max(screenshots),
            "aborted_runs": aborted,
        },
        "checks": checks,
        "adapter_selfcheck": adapter_check,
    }
    if not all(checks.values()) or result["duplicate_task_reviewer_pairs"]:
        raise ValueError("profile invariants failed; no receipt written")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    result = profile(args.root)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "rows": result["rows"],
                "annotations": result["annotations"],
                "positives_incorrect": result["positives_incorrect"],
                "negatives_correct": result["negatives_correct"],
                "unlabeled": result["unlabeled"],
                "sorted_record_ids_sha256": result["sorted_record_ids_sha256"],
                "checks_passed": all(result["checks"].values()),
                "adapter_issues": len(result["adapter_selfcheck"]["issues"]),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
