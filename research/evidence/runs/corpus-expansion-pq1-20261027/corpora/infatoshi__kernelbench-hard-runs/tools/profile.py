#!/usr/bin/env python3
"""Publish counts, field names, file indices and digests only; never corpus text."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from adapter import CORPUS, REPO, REVISION, ROOT, annotation, iter_rows, selfcheck, trace_key

OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_kind(path: str) -> str:
    if path.startswith("annotations/"):
        return "annotation" if path.endswith(".yaml") else "annotation_schema"
    name = Path(path).name
    if path.startswith("runs/"):
        return {
            "transcript.jsonl": "transcript",
            "solution.py": "solution",
            "result.json": "result",
        }[name]
    return "run_index" if path == "runs.jsonl" else "publisher_metadata"


def profile(root: Path = ROOT) -> dict[str, Any]:
    manifest = json.loads((root / "STAGE-MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["repo"] == REPO and manifest["revision"] == REVISION
    assert manifest["licence"] == "cc-by-4.0" and manifest["gated"] is False
    rows = list(iter_rows(root))
    ids = [row["run_id"] for row in rows]
    per_model: dict[str, Counter[str]] = defaultdict(Counter)
    per_task: dict[str, Counter[str]] = defaultdict(Counter)
    per_harness: dict[str, Counter[str]] = defaultdict(Counter)
    labels: Counter[str] = Counter()
    metadata_differences: Counter[str] = Counter()
    quote_audit: Counter[str] = Counter()
    transcript_digests: list[str] = []
    annotation_fields: Counter[str] = Counter()
    quote_fields: Counter[str] = Counter()
    for row in rows:
        gold = annotation(row, root)
        label = gold["verdict"] if gold else "unlabeled"
        labels[label] += 1
        for table, field in (
            (per_model, "model_label"),
            (per_task, "problem"),
            (per_harness, "harness"),
        ):
            table[row[field]][label] += 1
            table[row[field]]["total"] += 1
        transcript_digests.append(sha256((root / row["transcript_path"]).read_bytes()))
        if gold is None:
            continue
        annotation_fields.update(gold.keys())
        for field, index_field in (
            ("model", "model_id"),
            ("peak_fraction", "peak_fraction"),
            ("effort", "reasoning_effort"),
            ("harness", "harness"),
            ("problem", "problem"),
            ("run_id", "run_id"),
        ):
            if gold.get(field) != row[index_field]:
                metadata_differences[field] += 1
        for quote in gold.get("quotes", []):
            quote_fields.update(quote.keys())
            quote_audit["quotes"] += 1
            artifact = root / "runs" / row["run_id"] / quote["file"]
            lines = artifact.read_text(encoding="utf-8").splitlines()
            bounds = quote["lines"]
            valid = (
                len(bounds) == 2
                and all(type(n) is int for n in bounds)
                and 1 <= bounds[0] <= bounds[1] <= len(lines)
            )
            quote_audit["solution_artifact_quotes"] += artifact == root / row["solution_path"]
            quote_audit["valid_ranges" if valid else "invalid_ranges"] += 1
            if valid:
                span = "\n".join(lines[bounds[0] - 1 : bounds[1]])
                quote_audit["exact_quote_matches"] += quote["text"].strip() in span
            quote_audit[f"{label}_quotes"] += 1
        if label == "rubric_leak":
            valid = any(len(q["lines"]) == 2 and q["lines"][0] > 0 for q in gold.get("quotes", []))
            quote_audit["positive_rows_with_valid_ranges"] += valid
            quote_audit["positive_rows_without_valid_ranges"] += not valid
    inventory = []
    by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    for index, item in enumerate(sorted(manifest["files"], key=lambda f: f["path"])):
        path = root / item["path"]
        data = path.read_bytes()
        digest = sha256(data)
        git_blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        assert len(data) == item["bytes"]
        assert digest == item["sha256_computed"] and git_blob == item["blob_id"]
        assert not path.stat().st_mode & 0o222, "bundle must be locked before profiling"
        kind = file_kind(item["path"])
        by_kind[kind].update({"files": 1, "bytes": len(data)})
        inventory.append(
            {
                "file_index": index,
                "path_sha256": sha256(item["path"].encode()),
                "kind": kind,
                "bytes": len(data),
                "sha256": digest,
                "git_blob": git_blob,
            }
        )
    annotation_count = len(list((root / "annotations").glob("*.yaml")))
    publisher_card = (root / "README.md").read_text(encoding="utf-8")
    claimed = {"rows": 84, "models": 12, "tasks": 7, "annotated": 30}
    observed = {
        "rows": len(rows),
        "models": len(per_model),
        "tasks": len(per_task),
        "annotated": len(rows) - labels["unlabeled"],
    }
    claims_checks = {
        key: {
            "publisher_count": count,
            "observed": observed[key],
            "count_present_in_card": bool(
                re.search(r"(?<!\d)" + str(count) + r"(?!\d)", publisher_card)
            ),
            "matches": count == observed[key],
        }
        for key, count in claimed.items()
    }
    assert all(
        check["matches"] and check["count_present_in_card"] for check in claims_checks.values()
    )
    assert labels == {"rubric_leak": 11, "clean": 19, "unlabeled": 54}
    assert len(ids) == len(set(ids)) == len({trace_key(i) for i in ids}) == 84
    assert annotation_count == observed["annotated"] == 30
    assert len(inventory) == 270 and sum(f["bytes"] for f in inventory) == 26551983
    check = selfcheck(root)
    assert check["issues"] == 0
    index_data = (root / "runs.jsonl").read_bytes()
    inventory_bytes = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    return {
        "corpus": CORPUS,
        "repo": REPO,
        "revision": REVISION,
        "licence": manifest["licence"],
        "gated": False,
        "local_only": True,
        "cross_test_only": True,
        "files": len(inventory),
        "bytes": sum(f["bytes"] for f in inventory),
        "all_files_git_blob_verified": True,
        "all_files_locked": True,
        "manifest_sha256": sha256((root / "STAGE-MANIFEST.json").read_bytes()),
        "notice_sha256": sha256((root / "CROSS_TEST_ONLY.md").read_bytes()),
        "runs_jsonl_sha256": sha256(index_data),
        "runs_jsonl_git_blob": hashlib.sha1(
            b"blob %d\0" % len(index_data) + index_data
        ).hexdigest(),
        "file_digest_inventory_sha256": sha256(inventory_bytes),
        "file_inventory_order": "lexicographically sorted publisher-relative paths; paths are hashed",
        "by_file_kind": {k: dict(v) for k, v in sorted(by_kind.items())},
        "rows": len(rows),
        "positives": labels["rubric_leak"],
        "negatives": labels["clean"],
        "unlabeled": labels["unlabeled"],
        "scoreable": observed["annotated"],
        "per_label": {
            label: labels[label]
            for label in ("rubric_leak", "clean", "reward_hack", "interesting", "bug", "unlabeled")
        },
        "per_model": {k: dict(v) for k, v in sorted(per_model.items())},
        "per_task": {k: dict(v) for k, v in sorted(per_task.items())},
        "per_harness": {k: dict(v) for k, v in sorted(per_harness.items())},
        "id_uniqueness": {
            "rows": len(ids),
            "unique_ids": len(set(ids)),
            "unique_trace_keys": len({trace_key(i) for i in ids}),
        },
        "sorted_record_ids_sha256": sha256(("\n".join(sorted(ids)) + "\n").encode()),
        "sorted_record_ids_digest_encoding": "UTF-8, sorted ids joined by newline, terminal newline",
        "unique_transcript_sha256": len(set(transcript_digests)),
        "missing_solution_files": sum(not (root / r["solution_path"]).is_file() for r in rows),
        "publisher_claim_checks": claims_checks,
        "annotation_field_counts": dict(sorted(annotation_fields.items())),
        "quote_field_counts": dict(sorted(quote_fields.items())),
        "annotation_index_metadata_differences": dict(sorted(metadata_differences.items())),
        "gold_evidence_audit": dict(quote_audit),
        "adapter_selfcheck": check,
        "file_digests": inventory,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    result = profile(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "files",
                    "bytes",
                    "rows",
                    "positives",
                    "negatives",
                    "unlabeled",
                    "scoreable",
                    "runs_jsonl_sha256",
                )
            }
        )
    )
    print(json.dumps(result["adapter_selfcheck"], sort_keys=True))


if __name__ == "__main__":
    main()
