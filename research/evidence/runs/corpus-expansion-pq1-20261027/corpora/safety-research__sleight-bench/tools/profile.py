"""Verify the locked local bundle and emit counts/digests only, never corpus text."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from adapter import CORPUS, REVISION, ROOT, group_id, key, record_id, segment_paths, selfcheck

OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def distribution(values: list[int]) -> dict[str, int | float]:
    return {
        "count": len(values),
        "sum": sum(values),
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
    }


def hashed_counts(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(key(value) for value in values).items()))


def profile(root: Path = ROOT) -> dict[str, Any]:
    manifest_data = (root / "STAGE-MANIFEST.json").read_bytes()
    manifest = json.loads(manifest_data)
    if manifest["revision"] != REVISION or manifest["licence"] != "MIT":
        raise ValueError("revision or licence differs from approved pin")
    checked_files = []
    source_inventory = []
    encrypted_inventory = []
    for index, entry in enumerate(manifest["files"], 1):
        path = root / entry["path"]
        data = path.read_bytes()
        if len(data) != entry["bytes"] or digest(data) != entry["sha256"]:
            raise ValueError("bundle digest or size mismatch")
        if entry.get("git_blob"):
            blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            if blob != entry["git_blob"]:
                raise ValueError("source Git blob mismatch")
            receipt = {
                "file_index": index,
                "file_key": key(entry["path"]),
                "bytes": len(data),
                "sha256": digest(data),
                "git_blob": blob,
            }
            source_inventory.append(receipt)
            if path.name == "encrypted.bin":
                encrypted_inventory.append(receipt)
        checked_files.append(path)
    filesystem_files = {path for path in root.rglob("*") if path.is_file()}
    expected_files = set(checked_files) | {
        root / "STAGE-MANIFEST.json",
        root / "CROSS_TEST_ONLY.md",
    }
    if filesystem_files != expected_files:
        raise ValueError("unmanifested or missing bundle files")
    paths_to_check = [root, *root.rglob("*")]
    writable = sum(bool(path.stat().st_mode & 0o222) for path in paths_to_check)
    if writable:
        raise ValueError("bundle is not locked read-only")

    segments = segment_paths(root)
    metadata_rows = [json.loads((path / "metadata.json").read_text()) for path in segments]
    ids = [record_id(meta, arm) for meta in metadata_rows for arm in ("attack", "benign")]
    group_segments = Counter(group_id(path) for path in segments)
    lengths = []
    transcript_digests: dict[bool, list[str]] = {True: [], False: []}
    source_block_counts = Counter()
    roles = Counter()
    content_types = Counter()
    source_fields = Counter()
    metadata_fields = Counter(field for meta in metadata_rows for field in meta)
    for segment in segments:
        for positive, filename in ((True, "transcript.jsonl"), (False, "benign.jsonl")):
            path = segment / filename
            data = path.read_bytes()
            lengths.append(len(data))
            transcript_digests[positive].append(digest(data))
            for line in data.decode().splitlines():
                if not line.strip():
                    continue
                block = json.loads(line)
                source_fields.update(block.keys())
                source_block_counts["attack" if positive else "benign"] += 1
                if "role" in block:
                    roles[block["role"]] += 1
                    content_types[block["content"]["type"]] += 1
    checks = selfcheck(root)
    if checks["issues"]:
        raise ValueError("adapter selfcheck reported issues")
    publisher_checks = {
        "attack_groups_40": len(group_segments) == 40,
        "benign_groups_40": len(group_segments) == 40 and len(transcript_digests[False]) == 43,
        "attack_segments_43": len(transcript_digests[True]) == 43,
        "benign_segments_43": len(transcript_digests[False]) == 43,
        "categories_11": len({path.parent.name for path in segments}) == 11,
        "multi_segment_groups_3": Counter(group_segments.values()) == {1: 37, 2: 3},
        "encrypted_files_43": len(encrypted_inventory) == 43,
        "encrypted_bytes_8981684": sum(item["bytes"] for item in encrypted_inventory) == 8981684,
        "excluded_refusal_forcing_absent": not (root / "attacks/refusal_forcing").exists(),
        "paired_files_all_present": all((path / "benign.jsonl").is_file() for path in segments),
    }
    if not all(publisher_checks.values()):
        raise ValueError("publisher count check failed")
    encrypted_digest_table = [
        {"file_key": item["file_key"], "sha256": item["sha256"]}
        for item in sorted(encrypted_inventory, key=lambda item: item["file_key"])
    ]
    return {
        "corpus": CORPUS,
        "revision": REVISION,
        "licence": "MIT",
        "bundle": str(root),
        "manifest_sha256": digest(manifest_data),
        "counts": {
            "rows": len(ids),
            "positives": len(transcript_digests[True]),
            "negatives": len(transcript_digests[False]),
            "unlabeled": 0,
            "pairs": len(group_segments),
            "segments": len(segments),
            "categories": len({path.parent.name for path in segments}),
            "single_segment_pairs": 37,
            "two_segment_pairs": 3,
        },
        "publisher_count_checks": publisher_checks,
        "per_label": {"positive": 43, "negative": 43, "unlabeled": 0},
        "per_task_rows": dict(
            sorted((key(task), 2 * count) for task, count in group_segments.items())
        ),
        "per_category_segments": hashed_counts([path.parent.name for path in segments]),
        "per_scenario_rows": hashed_counts(
            [meta["scenario"] for meta in metadata_rows for _ in range(2)]
        ),
        "per_outcome_segments": hashed_counts([meta["outcome"] for meta in metadata_rows]),
        "per_tag_segments": hashed_counts([tag for meta in metadata_rows for tag in meta["tags"]]),
        "per_model_rows": {"unreported": len(ids)},
        "per_generator_rows": {"unreported": len(ids)},
        "metadata_field_segment_counts": dict(sorted(metadata_fields.items())),
        "transcript_field_block_counts": dict(sorted(source_fields.items())),
        "role_block_counts": dict(sorted(roles.items())),
        "content_type_block_counts": dict(sorted(content_types.items())),
        "source_blocks_per_arm": dict(sorted(source_block_counts.items())),
        "transcript_bytes": distribution(lengths),
        "id_uniqueness": {
            "record_ids": len(set(ids)),
            "trace_keys": len({key(value) for value in ids}),
            "segment_uuids": len({meta["uuid"] for meta in metadata_rows}),
            "segment_attack_ids": len({meta["attack_id"] for meta in metadata_rows}),
            "task_groups": len(group_segments),
            "duplicates": len(ids) - len(set(ids)),
            "short_hash_collisions": len(set(ids)) - len({key(value) for value in ids}),
        },
        "sorted_record_ids_sha256": digest(("\n".join(sorted(ids)) + "\n").encode()),
        "exact_transcript_uniqueness": {
            "attack": len(set(transcript_digests[True])),
            "benign": len(set(transcript_digests[False])),
            "all": len(set(transcript_digests[True] + transcript_digests[False])),
            "cross_label_shared": len(
                set(transcript_digests[True]) & set(transcript_digests[False])
            ),
        },
        "bundle_integrity": {
            "verified_source_files": len(source_inventory),
            "verified_derived_files": len(checked_files) - len(source_inventory),
            "verified_manifest_files": len(checked_files),
            "manifest_payload_bytes": sum(path.stat().st_size for path in checked_files),
            "filesystem_files_including_receipts": len(filesystem_files),
            "filesystem_bytes_including_receipts": sum(
                path.stat().st_size for path in filesystem_files
            ),
            "writeable_entries": writable,
            "problems": 0,
        },
        "encrypted_digest_table_sha256": digest(
            json.dumps(encrypted_digest_table, sort_keys=True, separators=(",", ":")).encode()
        ),
        "source_file_digests": source_inventory,
        "adapter_selfcheck": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    result = profile(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "counts": result["counts"],
                "integrity": result["bundle_integrity"],
                "issues": result["adapter_selfcheck"]["issues"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
