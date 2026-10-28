#!/usr/bin/env python3
"""Offline PAIChecker task projection; serialize only ``auditor_input`` to an auditor.

Labels describe issue/PR/test semantic alignment, not weak-test false passes.
Source tools, prompts, examples, and auxiliary evidence are never executed or read
by this adapter. No network or model calls are made.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

CORPUS = "manyi-w__paichecker"
REPO = "https://github.com/manyi-w/PAIChecker"
REVISION = "0f0a2aee87a3cfb40c92120c47225c4d83cb3dd7"
ROOT = Path.home() / ".local/state/evallab-cross-test" / CORPUS / REVISION[:12]
SUB_CORPORA = {
    "swe_bench_multilingual": {
        "labels": "labelled_data/swe_bench_multilingual.csv",
        "tasks": "graders/swe-bench__swe-bench_multilingual/data/test-00000-of-00001.parquet",
        "expected_rows": 300,
    },
    "swe_gym": {
        "labels": "labelled_data/swe_gym_all.csv",
        "tasks": "graders/swe-gym__swe-gym/data/train-00000-of-00001.parquet",
        "expected_rows": 2287,
    },
}
FAMILIES = ("DP", "FP", "IS", "Others", "SC", "UL")
AUDITOR_INPUT_FIELDS = (
    "repo",
    "base_commit",
    "problem_statement",
    "hints_text",
    "test_patch",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "patch",
)
FIELDS_AUDITOR_MUST_NOT_SEE = frozenset(
    {
        "human_label",
        "families",
        "positive",
        "instance_id",
        "sub_corpus",
        "sub-corpus",
        "gold",
        "labels",
        "identity",
        "label",
        "label_tokens",
        "label_subtypes",
        "label_counts",
        "defect_text",
        "fix_description",
        "fixed_version",
        "after_version",
        "status",
        "publisher_comments",
        "evidence",
        "source_tool_output",
        "label_derived_fields",
    }
)


def families_of(label: str) -> tuple[str, ...]:
    """Collapse numeric Gym subtypes to the five README families, plus Others."""
    if label == "No Misalignment":
        return ()
    tokens = [token.strip() for token in label.split(",")]
    if not tokens or any(
        re.fullmatch(r"(?:SC|DP|FP|IS|UL)(?:-\d+)?|Others", token) is None
        for token in tokens
    ):
        raise ValueError("unrecognized or empty label taxonomy; no row content emitted")
    return tuple(sorted({token.split("-", 1)[0] for token in tokens}))


def load_labels(path: Path) -> dict[str, tuple[str, ...]]:
    labels = {}
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["instance_id", "human_label"]:
            raise ValueError("unexpected label schema")
        for row in reader:
            instance_id = row["instance_id"]
            if not instance_id or instance_id in labels or None in row:
                raise ValueError("empty/duplicate label identity or malformed label row")
            labels[instance_id] = families_of(row["human_label"])
    return labels


def task_rows(path: Path, columns: tuple[str, ...]) -> Iterator[dict[str, Any]]:
    parquet = pq.ParquetFile(path)
    for batch in parquet.iter_batches(batch_size=128, columns=list(columns)):
        yield from batch.to_pylist()


def specs(bundle: Path = ROOT) -> Iterator[dict[str, Any]]:
    """Yield labelled tasks from BOTH sub-corpora; never expose complete specs.

    ``patch`` is the benchmark's reference PR and is deliberately auditor-visible.
    PAIChecker misalignment gold, raw ids, and the sub-corpus remain scorer-side.
    The 151 unlabelled Gym grader rows are excluded, never made into negatives.
    """
    for sub_corpus, source in SUB_CORPORA.items():
        labels = load_labels(bundle / source["labels"])
        seen = set()
        matched = set()
        for row in task_rows(bundle / source["tasks"], ("instance_id", *AUDITOR_INPUT_FIELDS)):
            instance_id = row["instance_id"]
            if not instance_id or instance_id in seen:
                raise ValueError("empty/duplicate grader identity")
            seen.add(instance_id)
            if instance_id not in labels:
                continue
            matched.add(instance_id)
            families = labels[instance_id]
            yield {
                "auditor_input": {field: row[field] for field in AUDITOR_INPUT_FIELDS},
                "gold": {"positive": bool(families), "families": list(families)},
                "identity": {
                    "instance_id": instance_id,
                    "sub_corpus": sub_corpus,
                    "repo": row["repo"],
                },
            }
        if matched != labels.keys():
            raise ValueError("label/grader join incomplete")


def selfcheck(bundle: Path = ROOT) -> dict[str, Any]:
    """Inspect all joined tasks and emit aggregates only, never ids or text."""
    summaries = {}
    for sub_corpus, source in SUB_CORPORA.items():
        labels = load_labels(bundle / source["labels"])
        grader_ids = [
            row["instance_id"]
            for row in task_rows(bundle / source["tasks"], ("instance_id",))
        ]
        grader_id_set = set(grader_ids)
        family_counts = Counter(family for families in labels.values() for family in families)
        missing = len(labels.keys() - grader_id_set)
        duplicates = len(grader_ids) - len(grader_id_set)
        if missing or duplicates or len(labels) != source["expected_rows"]:
            raise ValueError("pinned join/count check failed")
        summaries[sub_corpus] = {
            "rows": len(labels),
            "positives": sum(bool(families) for families in labels.values()),
            "negatives": sum(not families for families in labels.values()),
            "unlabeled": 0,
            "label_rows": len(labels),
            "grader_rows": len(grader_ids),
            "label_ids_found": len(labels) - missing,
            "label_ids_missing": missing,
            "label_duplicate_rows": 0,
            "grader_duplicate_rows": duplicates,
            "grader_unlabelled_ids": len(grader_id_set - labels.keys()),
            "families": {family: family_counts[family] for family in FAMILIES},
        }
    counts = Counter()
    input_nulls = Counter()
    input_empty = Counter()
    rows_per_sub_corpus = Counter()
    repo_clusters: dict[str, Counter[str]] = {name: Counter() for name in SUB_CORPORA}
    for spec in specs(bundle):
        auditor_input = spec["auditor_input"]
        if set(spec) != {"auditor_input", "gold", "identity"}:
            raise ValueError("unexpected spec envelope")
        if set(auditor_input) != set(AUDITOR_INPUT_FIELDS):
            raise ValueError("auditor input differs from exact allowlist")
        counts["label_fields_in_auditor_input"] += len(
            FIELDS_AUDITOR_MUST_NOT_SEE.intersection(auditor_input)
        )
        if counts["label_fields_in_auditor_input"]:
            raise ValueError("label field in auditor input")
        for field, value in auditor_input.items():
            input_nulls[field] += value is None
            input_empty[field] += value == "" or value == []
        for field in ("FAIL_TO_PASS", "PASS_TO_PASS"):
            value = auditor_input[field]
            if not isinstance(value, list) or any(not isinstance(test, str) for test in value):
                raise ValueError("test names must retain native string-list structure")
        if not isinstance(auditor_input["patch"], str):
            raise ValueError("reference patch must retain native string structure")
        gold = spec["gold"]
        if gold["families"] != sorted(set(gold["families"])):
            raise ValueError("gold families are not a sorted set")
        if gold["positive"] is not bool(gold["families"]):
            raise ValueError("binary gold differs from family membership")
        identity = spec["identity"]
        if identity["repo"] != auditor_input["repo"]:
            raise ValueError("bootstrap cluster differs from source repository")
        sub_corpus = identity["sub_corpus"]
        rows_per_sub_corpus[sub_corpus] += 1
        repo_hash = hashlib.sha256(identity["repo"].encode("utf-8")).hexdigest()
        repo_clusters[sub_corpus][repo_hash] += 1
        counts["rows"] += 1
        counts["positives" if gold["positive"] else "negatives"] += 1
    if any(rows_per_sub_corpus[name] != item["rows"] for name, item in summaries.items()):
        raise ValueError("adapter yielded the wrong number of tasks")
    assert counts["label_fields_in_auditor_input"] == 0
    return {
        **dict(sorted(counts.items())),
        "unlabeled": 0,
        "sub_corpora": summaries,
        "repo_cluster_task_counts": {
            name: dict(sorted(values.items())) for name, values in repo_clusters.items()
        },
        "input_null_counts": dict(sorted(input_nulls.items())),
        "input_empty_counts": dict(sorted(input_empty.items())),
        "issues": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("selfcheck",), nargs="?", default="selfcheck")
    parser.add_argument("--bundle", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(selfcheck(args.bundle), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
