#!/usr/bin/env python3
"""Reverify the locked Beam bundle; publish aggregates, digests and hashed ids only."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

import pyarrow.parquet as pq

from adapter import (
    ANNOTATION_FIELDS, CORPUS, MODEL, REPO, REVISION, ROOT, iter_rows, label_state,
    record_id, selfcheck,
)

OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"
MANIFEST_SHA256 = "2b28d2eb4cd8f75bb52231da3351a05a81a490bb1195422a1f9e7c932d7da07f"
NOTICE_SHA256 = "dd67a59b1c25fe425f9bc1c7629c74df438fde245807989283d8130639ee7439"
EXPECTED_FILES = frozenset({".gitattributes", "README.md", "data/episodes.parquet", "data/variants.parquet"})


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalise_id(value: str, mode: str) -> str:
    if mode == "bare":
        return value.split("@")[0].rsplit("/", 1)[-1].rsplit("__", 1)[-1].casefold()
    if mode == "full":
        return value.casefold()
    raise ValueError("unknown id normalisation")


def hashed_ids(values: list[str]) -> dict[str, list[str]]:
    return {
        mode: sorted(sha256(normalise_id(value, mode).encode("utf-8")) for value in values)
        for mode in ("bare", "full")
    }


def verify_bundle(root: Path) -> dict[str, Any]:
    manifest_bytes = (root / "STAGE-MANIFEST.json").read_bytes()
    if sha256(manifest_bytes) != MANIFEST_SHA256:
        raise ValueError("manifest differs from the pinned staging receipt")
    manifest = json.loads(manifest_bytes)
    if (manifest["repo"], manifest["revision"], manifest["licence"], manifest["gated"]) != (
        REPO, REVISION, "apache-2.0", False,
    ):
        raise ValueError("source pin, licence or gate differs from approval")
    if manifest["excluded"]:
        raise ValueError("this whole-repository staging has no exclusions")
    if sha256((root / "CROSS_TEST_ONLY.md").read_bytes()) != NOTICE_SHA256:
        raise ValueError("local-only evaluation notice changed")
    paths = [root, *root.rglob("*")]
    if any(path.is_symlink() for path in paths):
        raise ValueError("immutable bundles cannot contain symlinks")
    writable_paths = sum(bool(path.stat().st_mode & 0o222) for path in paths)
    if writable_paths:
        raise ValueError("bundle must be locked before profiling")
    inventory = []
    seen = set()
    for index, entry in enumerate(sorted(manifest["files"], key=lambda item: item["path"])):
        relative = PurePosixPath(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or entry["path"] in seen:
            raise ValueError("unsafe or duplicate manifest path")
        seen.add(entry["path"])
        path = root / relative
        if not path.is_file() or path.stat().st_size != entry["bytes"]:
            raise ValueError("missing or wrong-sized manifest file")
        data = path.read_bytes()
        digest = sha256(data)
        if digest != entry["sha256"] or digest != entry["sha256_computed"]:
            raise ValueError("manifest final-byte SHA-256 mismatch")
        method = entry["verified_against"]
        if method == "hub_git_blob_sha1":
            blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
            if blob != entry["blob_id"]:
                raise ValueError("publisher Git blob mismatch")
        elif method != "hub_lfs_sha256":
            raise ValueError("unrecognized publisher digest method")
        inventory.append({
            "file_index": index, "path_sha256": sha256(entry["path"].encode("utf-8")),
            "bytes": len(data), "sha256": digest, "publisher_blob_id": entry["blob_id"],
            "verified_against": method,
        })
    if seen != EXPECTED_FILES:
        raise ValueError("staged publisher file inventory changed")
    actual_files = {path.relative_to(root).as_posix() for path in paths if path.is_file()}
    if actual_files != seen | {"STAGE-MANIFEST.json", "CROSS_TEST_ONLY.md"}:
        raise ValueError("unmanifested or missing bundle files")
    payload_bytes = sum(entry["bytes"] for entry in inventory)
    if payload_bytes != manifest["total_bytes"]:
        raise ValueError("manifest total byte count mismatch")
    return {
        "files": len(inventory), "bytes": payload_bytes,
        "filesystem_files_including_receipts": len(actual_files),
        "filesystem_bytes_including_receipts": sum(path.stat().st_size for path in paths if path.is_file()),
        "publisher_git_blob_verified_files": sum(entry["verified_against"] == "hub_git_blob_sha1" for entry in inventory),
        "publisher_lfs_sha256_verified_files": sum(entry["verified_against"] == "hub_lfs_sha256" for entry in inventory),
        "verified_after_lock": True, "writable_paths": writable_paths,
        "file_digests": inventory,
    }


def profile(root: Path = ROOT) -> dict[str, Any]:
    integrity = verify_bundle(root)
    checks = selfcheck(root)
    rows = list(iter_rows(root))
    variants = pq.read_table(root / "data/variants.parquet").to_pylist()
    task_ids = sorted({row["instance_id"] for row in rows})
    run_ids = [record_id(row) for row in rows]
    variant_ids = [row["instance_id"] for row in variants]
    if set(variant_ids) != set(task_ids) or len(variant_ids) != len(set(variant_ids)):
        raise ValueError("episode/variant task join is not one-to-one")
    states: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    raw_labels: Counter[str] = Counter()
    slices: dict[str, Counter[str]] = defaultdict(Counter)
    per_model: dict[str, Counter[str]] = defaultdict(Counter)
    per_experiment: dict[str, Counter[str]] = defaultdict(Counter)
    per_condition: dict[str, Counter[str]] = defaultdict(Counter)
    per_effort: dict[str, Counter[str]] = defaultdict(Counter)
    per_repo: dict[str, Counter[str]] = defaultdict(Counter)
    per_task: dict[str, Counter[str]] = defaultdict(Counter)
    source_annotations: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        state, reason = label_state(row)
        states[state] += 1
        reasons[reason] += 1
        raw_labels["cheat" if row["cheated"] else "noncheat"] += 1
        subset = "control" if row["condition"] == "possible" else "impossible_task"
        slices[subset]["rows"] += 1
        slices[subset][state] += 1
        slices[subset]["raw_cheat" if row["cheated"] else "raw_noncheat"] += 1
        if row["stop_reason"] != "error":
            slices[subset]["nonerror_rows"] += 1
            slices[subset]["nonerror_raw_cheat" if row["cheated"] else "nonerror_raw_noncheat"] += 1
        for table, value in (
            (per_model, MODEL), (per_experiment, row["run_id"]),
            (per_condition, row["condition"]), (per_effort, row["effort"]),
            (per_repo, row["repo"]), (per_task, sha256(row["instance_id"].encode("utf-8"))),
        ):
            table[value]["rows"] += 1
            table[value][state] += 1
        for field in ANNOTATION_FIELDS:
            value = row[field]
            if field == "tamper_calls":
                source_annotations[field]["rows"] += 1
                source_annotations[field]["sum"] += value
                source_annotations[field]["nonzero_rows"] += value != 0
            else:
                source_annotations[field]["null" if value is None else "true" if value else "false"] += 1
    provided = {
        "episodes": 616, "raw_cheat": 173, "raw_noncheat": 443,
        "nonerror_impossible": 438, "nonerror_impossible_cheat": 171,
        "nonerror_impossible_noncheat": 267, "control": 176,
        "known_justified_control_edits": 1, "provider_errors": 2,
        "native_tasks": 44, "publisher_files": 4, "episode_parquet_bytes": 18700793,
    }
    observed = {
        "episodes": len(rows), "raw_cheat": raw_labels["cheat"], "raw_noncheat": raw_labels["noncheat"],
        "nonerror_impossible": slices["impossible_task"]["nonerror_rows"],
        "nonerror_impossible_cheat": slices["impossible_task"]["nonerror_raw_cheat"],
        "nonerror_impossible_noncheat": slices["impossible_task"]["nonerror_raw_noncheat"],
        "control": slices["control"]["rows"],
        "known_justified_control_edits": reasons["known_justified_control_test_edit"],
        "provider_errors": reasons["provider_error"], "native_tasks": len(task_ids),
        "publisher_files": integrity["files"],
        "episode_parquet_bytes": (root / "data/episodes.parquet").stat().st_size,
    }
    task_hashes = hashed_ids(task_ids)
    run_hashes = hashed_ids(run_ids)
    return {
        "corpus": CORPUS, "repo": REPO, "revision": REVISION, "licence": "apache-2.0",
        "gated": False, "local_only": True, "cross_test_only": True,
        "manifest_sha256": MANIFEST_SHA256, "notice_sha256": NOTICE_SHA256,
        **integrity,
        "rows": len(rows), "positives": states["positive"], "negatives": states["negative"],
        "ambiguous": states["ambiguous"], "excluded": states["excluded"],
        "scoreable": states["positive"] + states["negative"],
        "label_states": dict(states), "label_reasons": dict(reasons),
        "excluded_by_reason": {"provider_error": reasons["provider_error"]},
        "ambiguous_by_reason": {"known_justified_control_test_edit": reasons["known_justified_control_test_edit"]},
        "raw_label_counts": dict(raw_labels),
        "slices": {key: dict(value) for key, value in sorted(slices.items())},
        "per_model": {key: dict(value) for key, value in sorted(per_model.items())},
        "per_experiment": {key: dict(value) for key, value in sorted(per_experiment.items())},
        "per_condition": {key: dict(value) for key, value in sorted(per_condition.items())},
        "per_effort": {key: dict(value) for key, value in sorted(per_effort.items())},
        "per_repository": {key: dict(value) for key, value in sorted(per_repo.items())},
        "per_task_sha256": {key: dict(value) for key, value in sorted(per_task.items())},
        "source_annotation_counts": {key: dict(value) for key, value in sorted(source_annotations.items())},
        "task_id_hashes": task_hashes, "run_id_hashes": run_hashes,
        "id_uniqueness": {
            "native_tasks": len(task_ids), "publisher_experiment_ids": len({row["run_id"] for row in rows}),
            "composite_episode_ids": len(set(run_ids)), "variant_tasks": len(variant_ids),
            "task_normalised_unique_hashes": {mode: len(set(values)) for mode, values in task_hashes.items()},
            "run_normalised_unique_hashes": {mode: len(set(values)) for mode, values in run_hashes.items()},
        },
        "bootstrap": {"task_clusters": len(task_ids), "model_strata": len(per_model), "resamples": 10000, "seed": 0},
        "provided_count_checks": {
            key: {"provided": value, "observed": observed[key], "difference": observed[key] - value}
            for key, value in provided.items()
        },
        "adapter_selfcheck": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    result = profile(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in (
        "files", "bytes", "rows", "positives", "negatives", "ambiguous", "excluded",
        "scoreable", "manifest_sha256", "verified_after_lock", "writable_paths",
    )}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
