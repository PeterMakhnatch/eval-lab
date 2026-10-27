#!/usr/bin/env python3
"""Write aggregate-only provenance/counts for the pinned, local Apollo bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from adapter import (
    CORPUS,
    EXPECTED_BYTES,
    EXPECTED_FILES,
    EXPECTED_PHASE_COUNTS,
    EXPECTED_RESULT_BYTES,
    LABELS,
    REVISION,
    ROOT,
    hashed_id,
    iter_records,
    manifest,
    metadata,
    selfcheck,
)

OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"


def distribution(values: list[int]) -> dict[str, int | float]:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "sum": sum(ordered),
        "min": ordered[0],
        "median": ordered[len(ordered) // 2],
        "max": ordered[-1],
        "mean": round(sum(ordered) / len(ordered), 3),
    }


def profile(root: Path = ROOT) -> dict[str, Any]:
    staged = manifest(root)
    files = sorted(staged["files"], key=lambda item: item["path"])
    result_files = [
        item
        for item in files
        if item["path"].startswith("results/") and item["path"].endswith(".json")
    ]
    verification: Counter[str] = Counter()
    file_categories: Counter[str] = Counter()
    digests: dict[str, Any] = {"all": hashlib.sha256()}
    phase_sizes: dict[str, list[int]] = defaultdict(list)
    for entry in files:
        data = (root / entry["path"]).read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        verification["size_mismatches"] += len(data) != entry["bytes"]
        verification["sha256_mismatches"] += sha != entry["sha256"]
        verification["git_blob_mismatches"] += blob != entry["git_blob"]
        verification["writable_files"] += bool((root / entry["path"]).stat().st_mode & 0o222)
        file_categories[entry["path"].split("/")[0] if "/" in entry["path"] else "root"] += 1
        if entry["path"].startswith("results/") and entry["path"].endswith(".json"):
            phase = metadata(entry["path"])["phase"]
            digests.setdefault(phase, hashlib.sha256()).update(bytes.fromhex(sha))
            digests["all"].update(bytes.fromhex(sha))
            phase_sizes[phase].append(len(data))
    for name in ("STAGE-MANIFEST.json", "CROSS_TEST_ONLY.md"):
        verification["writable_files"] += bool((root / name).stat().st_mode & 0o222)
    verification["writable_root"] += bool(stat.S_IMODE(root.stat().st_mode) & 0o222)
    counts: Counter[str] = Counter()
    phase_counts: Counter[str] = Counter()
    field_counts: Counter[str] = Counter()
    models: dict[str, Counter[str]] = defaultdict(Counter)
    experiments: dict[str, Counter[str]] = defaultdict(Counter)
    tasks: dict[str, Counter[str]] = defaultdict(Counter)
    labels: dict[str, Counter[str]] = {label: Counter() for label in LABELS}
    label_by_model: dict[str, dict[str, Counter[str]]] = {
        label: defaultdict(Counter) for label in LABELS
    }
    record_ids: list[str] = []
    trace_keys: set[str] = set()
    cluster_keys: Counter[str] = Counter()
    prompt_chars: list[int] = []
    response_chars: list[int] = []
    dialogue_messages: list[int] = []
    model_parameters: dict[str, Counter[str]] = defaultdict(Counter)
    input_hashes: Counter[str] = Counter()
    input_labels: dict[str, set[tuple[str, bool]]] = defaultdict(set)
    for record_id, row in iter_records(root):
        record_ids.append(record_id)
        trace_keys.add(hashed_id(record_id))
        meta = metadata(record_id)
        phase = meta["phase"]
        counts["rows"] += 1
        phase_counts[phase] += 1
        cluster_keys[meta["cluster_key"]] += 1
        field_counts.update(row.keys())
        models[row["model"]][phase] += 1
        experiments[meta["experiment"]][phase] += 1
        tasks[meta["task_key"]][phase] += 1
        present = [label for label in LABELS if label in row]
        counts["rows_with_no_native_label"] += not present
        counts["rows_with_multiple_native_labels"] += len(present) > 1
        for label in LABELS:
            state = "unlabeled" if label not in row else "positive" if row[label] else "negative"
            labels[label][state] += 1
            label_by_model[label][row["model"]][state] += 1
        if len(present) == 1:
            counts["positive_rows" if row[present[0]] else "negative_rows"] += 1
        prompt_chars.append(sum(len(message["content"]) for message in row["prompt_dialogue"]))
        response_chars.append(len(row["response"]))
        dialogue_messages.append(len(row["prompt_dialogue"]))
        for parameter in ("temperature", "top_p", "max_tokens"):
            model_parameters[parameter][str(row[parameter])] += 1
        # Only digests of repeated detector inputs are counted; no inputs are emitted.
        input_hash = hashlib.sha256(
            json.dumps(
                {"prompt_dialogue": row["prompt_dialogue"], "response": row["response"]},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        input_hashes[input_hash] += 1
        input_labels[input_hash].update((label, row[label]) for label in present)
    adapter_check = selfcheck(root)
    checks = {
        "pinned_revision": staged["revision"] == REVISION,
        "pinned_licence": staged["licence"] == "CC-BY-4.0",
        "publisher_tree_file_count": len(files) == EXPECTED_FILES,
        "publisher_tree_total_bytes": sum(item["bytes"] for item in files) == EXPECTED_BYTES,
        "publisher_tree_result_file_count": len(result_files)
        == sum(EXPECTED_PHASE_COUNTS.values()),
        "publisher_tree_result_bytes": sum(item["bytes"] for item in result_files)
        == EXPECTED_RESULT_BYTES,
        "publisher_tree_phase_counts": dict(phase_counts) == EXPECTED_PHASE_COUNTS,
        "every_file_sha256_git_blob_and_size_verified": not any(verification.values()),
        "all_records_uniquely_identified": len(record_ids)
        == len(set(record_ids))
        == len(trace_keys),
        "one_native_label_per_response": counts["rows_with_no_native_label"] == 0
        and counts["rows_with_multiple_native_labels"] == 0,
        "label_denominators_sum_to_rows": all(
            sum(value.values()) == counts["rows"] for value in labels.values()
        ),
        "linked_phase_root_count": len(cluster_keys) == EXPECTED_PHASE_COUNTS["misalignment"],
        "adapter_zero_issues": adapter_check["issue_count"] == 0,
    }
    return {
        "corpus": CORPUS,
        "revision": REVISION,
        "licence": "CC-BY-4.0",
        "bundle_path": str(root),
        "verification": dict(sorted(verification.items())),
        "checks": checks,
        "publisher_tree_expected": {
            "files": EXPECTED_FILES,
            "bytes": EXPECTED_BYTES,
            "result_files": sum(EXPECTED_PHASE_COUNTS.values()),
            "result_bytes": EXPECTED_RESULT_BYTES,
            "phase_counts": EXPECTED_PHASE_COUNTS,
        },
        "files": len(files),
        "bytes": sum(item["bytes"] for item in files),
        "file_categories": dict(sorted(file_categories.items())),
        "result_files": len(result_files),
        "result_bytes": sum(item["bytes"] for item in result_files),
        "result_file_bytes_by_phase": {
            key: distribution(value) for key, value in sorted(phase_sizes.items())
        },
        "stage_manifest_sha256": hashlib.sha256(
            (root / "STAGE-MANIFEST.json").read_bytes()
        ).hexdigest(),
        "cross_test_notice_sha256": hashlib.sha256(
            (root / "CROSS_TEST_ONLY.md").read_bytes()
        ).hexdigest(),
        "data_file_digest_algorithm": "SHA-256(concatenated raw SHA-256 digest bytes; files sorted by POSIX record id; separately per phase and all)",
        "data_file_digests": {key: value.hexdigest() for key, value in sorted(digests.items())},
        "sorted_record_ids_digest_algorithm": "SHA-256(UTF-8 newline-joined lexicographically sorted POSIX-relative record ids, with final newline)",
        "sorted_record_ids_sha256": hashlib.sha256(
            ("\n".join(sorted(record_ids)) + "\n").encode()
        ).hexdigest(),
        "unique_record_ids": len(set(record_ids)),
        "unique_trace_keys": len(trace_keys),
        "counts": dict(sorted(counts.items())),
        "phase_counts": dict(sorted(phase_counts.items())),
        "labels": {label: dict(sorted(value.items())) for label, value in labels.items()},
        "per_label_per_model": {
            label: {model: dict(sorted(value.items())) for model, value in sorted(by_model.items())}
            for label, by_model in label_by_model.items()
        },
        "per_model": {key: dict(sorted(value.items())) for key, value in sorted(models.items())},
        "per_experiment": {
            key: dict(sorted(value.items())) for key, value in sorted(experiments.items())
        },
        "per_task_key": {key: dict(sorted(value.items())) for key, value in sorted(tasks.items())},
        "field_presence": dict(sorted(field_counts.items())),
        "generation_parameters": {
            key: dict(sorted(value.items())) for key, value in sorted(model_parameters.items())
        },
        "linked_root_clusters": len(cluster_keys),
        "cluster_size_counts": dict(sorted(Counter(cluster_keys.values()).items())),
        "duplicate_detector_input_groups": sum(count > 1 for count in input_hashes.values()),
        "duplicate_detector_input_excess_rows": sum(count - 1 for count in input_hashes.values()),
        "duplicate_detector_input_rows": sum(count for count in input_hashes.values() if count > 1),
        "duplicate_input_conflicting_native_label_groups": sum(
            any((label, True) in values and (label, False) in values for label in LABELS)
            for values in input_labels.values()
        ),
        "prompt_text_characters": distribution(prompt_chars),
        "response_text_characters": distribution(response_chars),
        "prompt_message_counts": distribution(dialogue_messages),
        "adapter_selfcheck": adapter_check,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    result = profile(args.root)
    if not all(result["checks"].values()):
        print(
            json.dumps(
                {"checks": result["checks"], "verification": result["verification"]}, indent=2
            )
        )
        raise SystemExit(1)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "files",
                    "bytes",
                    "result_files",
                    "result_bytes",
                    "counts",
                    "labels",
                    "linked_root_clusters",
                    "checks",
                    "data_file_digests",
                )
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
