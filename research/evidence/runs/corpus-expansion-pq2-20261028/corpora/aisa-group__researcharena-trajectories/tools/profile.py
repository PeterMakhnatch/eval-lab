#!/usr/bin/env python3
"""Reverify the locked ResearchArena bundle; emit only aggregates/digests/hashed ids."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from adapter import CORPUS, INDEX, REPO, REVISION, ROOT, label_state, load_index, selfcheck

OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"
MANIFEST_SHA256 = "ce13cc48d07618385e9d9226ed07e2f2ae2ad9794adc5efd7b9ebb18c814c396"
NOTICE_SHA256 = "e61a80f84daa24bad2126f0aa938fe15e65734a695677deba82383f40040a5f8"
INDEX_SHA256 = "1f7dd4d846a186546fee2c4aa09335621a4135826d33f948a0e6cd6f3bbd752c"
WEBSITE_REVISION = "05a03d24915401a5f53ae97aad8bfd67ca3498a6"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalise_id(value: str, mode: str) -> str:
    if mode == "bare":
        return value.split("@")[0].rsplit("/", 1)[-1].rsplit("__", 1)[-1].casefold()
    if mode == "full":
        return value.casefold()
    raise ValueError("unknown id normalisation")


def id_hashes(values: list[str]) -> dict[str, list[str]]:
    return {mode: sorted(digest(normalise_id(value, mode).encode("utf-8")) for value in values)
            for mode in ("bare", "full")}


def file_digests(path: Path, size: int, with_blob: bool) -> tuple[str, str | None]:
    sha256 = hashlib.sha256()
    blob = hashlib.sha1(b"blob %d\0" % size) if with_blob else None
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            sha256.update(block)
            if blob is not None:
                blob.update(block)
    return sha256.hexdigest(), blob.hexdigest() if blob is not None else None


def profile(bundle: Path = ROOT) -> dict[str, Any]:
    manifest_data = (bundle / "STAGE-MANIFEST.json").read_bytes()
    if digest(manifest_data) != MANIFEST_SHA256:
        raise ValueError("manifest differs from the frozen staging receipt")
    manifest = json.loads(manifest_data)
    if (manifest["repo"], manifest["revision"], manifest["licence"]) != (REPO, REVISION, "apache-2.0"):
        raise ValueError("bundle identity/licence mismatch")
    if digest((bundle / "CROSS_TEST_ONLY.md").read_bytes()) != NOTICE_SHA256:
        raise ValueError("local-only evaluation notice changed")
    paths = [bundle, *bundle.rglob("*")]
    if any(path.is_symlink() for path in paths):
        raise ValueError("bundle contains a symlink")
    writable_paths = sum(bool(path.stat().st_mode & 0o222) for path in paths)
    if writable_paths:
        raise ValueError("bundle is not locked read-only")
    listed_files: set[Path] = set()
    inventory = []
    for index, entry in enumerate(manifest["files"], 1):
        relative = PurePosixPath(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe manifest path")
        path = bundle / relative
        if path in listed_files or not path.is_file() or path.stat().st_size != entry["bytes"]:
            raise ValueError("duplicate, missing or wrong-sized manifest file")
        sha256, blob = file_digests(path, entry["bytes"], bool(entry.get("git_blob")))
        if sha256 != entry["sha256"]:
            raise ValueError("file SHA-256 mismatch")
        if blob is not None and blob != entry["git_blob"]:
            raise ValueError("publisher Git blob mismatch")
        if entry.get("lfs_oid") and entry["lfs_oid"] != "sha256:" + sha256:
            raise ValueError("publisher LFS oid mismatch")
        inventory.append({"file_index": index, "file_path_sha256": digest(entry["path"].encode()),
                          "bytes": entry["bytes"], "sha256": sha256,
                          "verified_against": entry["verified_against"],
                          **({"git_blob": blob} if blob is not None else {}),
                          **({"lfs_oid": entry["lfs_oid"]} if entry.get("lfs_oid") else {})})
        listed_files.add(path)
    filesystem_files = {path for path in paths if path.is_file()}
    if filesystem_files != listed_files | {bundle / "STAGE-MANIFEST.json", bundle / "CROSS_TEST_ONLY.md"}:
        raise ValueError("unmanifested or missing bundle files")
    if (len(listed_files), sum(item["bytes"] for item in inventory)) != (171, 93086756):
        raise ValueError("manifest payload count differs from frozen receipt")
    if sum(item["bytes"] for item in inventory) != manifest["total_bytes"]:
        raise ValueError("manifest total-byte mismatch")
    source_pins = {source["repo"]: source["revision"] for source in manifest["sources"]}
    if source_pins != {REPO: REVISION, "aisa-group/researcharena-website": WEBSITE_REVISION}:
        raise ValueError("publisher source pin mismatch")
    if digest((bundle / INDEX).read_bytes()) != INDEX_SHA256:
        raise ValueError("publisher index differs from byte-identical mirror receipt")
    source_tree = json.loads((bundle / "provenance/hf-tree.json").read_bytes())
    source_files = [item for item in source_tree if item["type"] == "file"]
    hf_entries = [entry for entry in manifest["files"] if entry.get("source") == "huggingface"]
    if len(source_files) != len(hf_entries) + manifest["excluded"]["files"]:
        raise ValueError("source file partition is incomplete")
    if sum(item["size"] for item in source_files) != (
        sum(entry["bytes"] for entry in hf_entries) + manifest["excluded"]["bytes"]
    ):
        raise ValueError("source byte partition is incomplete")
    source_by_path = {item["path"]: item for item in source_files}
    for entry in hf_entries:
        original = source_by_path[entry["path"]]
        expected = original.get("lfs", {}).get("oid")
        if expected is not None and entry.get("lfs_oid") != "sha256:" + expected:
            raise ValueError("manifest LFS oid differs from pinned tree")
        if expected is None and entry.get("git_blob") != original["oid"]:
            raise ValueError("manifest Git blob differs from pinned tree")
    checks = selfcheck(bundle)
    if checks["issue_count"] or checks["counts"]["label_fields_in_steps"]:
        raise ValueError("adapter selfcheck failed")
    rows = [row for row in load_index(bundle)["runs"] if label_state(row)[0] != "excluded"]
    run_ids = [row["id"] for row in rows]
    task_ids = [row["setting"] for row in rows]
    excluded_ids = [row["run_id"] for row in manifest["excluded"]["runs"]]
    result_paths = {entry["run_id"]: entry["path"] for entry in manifest["files"]
                    if entry.get("kind") == "executed_result"}
    outcomes: Counter[str] = Counter()
    for row in rows:
        executed = json.loads((bundle / result_paths[row["id"]]).read_bytes())
        outcomes["index_main_success_is_continuous_not_boolean"] += row["main_success"] not in (0, 1)
        outcomes["index_main_success_matches_executed_main_score"] += row["main_success"] == executed["main_task_score"]
        outcomes["index_side_score_differs_from_executed_side_score"] += row["side_score"] != executed["side_task_score"]
        outcomes["main_task_pass"] += executed["raw_metrics"]["main_task_pass"] is True
        if row["side_task"] == "embedded":
            outcomes["executed_benchmark_gaming_success"] += executed["raw_metrics"]["benchmark_gaming_side_task_success"] is True
    counts = checks["counts"]
    return {
        "corpus": CORPUS, "revision": REVISION, "licence": "apache-2.0",
        "manifest_sha256": MANIFEST_SHA256, "notice_sha256": NOTICE_SHA256,
        "publisher_index_sha256": INDEX_SHA256,
        "counts": {key: counts[key] for key in (
            "rows", "positives", "negatives", "excluded", "core_excluded", "indexed_runs", "bootstrap_clusters")},
        "per_class_counts": {"positive": counts["positives"], "negative": counts["negatives"], "excluded": counts["excluded"]},
        "per_agent_counts": checks["per_agent_counts"], "per_provider_counts": checks["per_provider_counts"],
        "per_arm_counts": {"embedded": counts["positives"], "baseline": counts["negatives"]},
        "per_setting_counts": {"inference_optimization": counts["rows"]},
        "excluded_by_reason": checks["excluded_by_reason"],
        "excluded_file_groups": manifest["excluded"]["groups"],
        "source_inventory_counts": manifest["source_inventory"],
        "source_outcome_field_checks": dict(outcomes),
        "source_monitor_counts": {
            "indexed_primary_monitor_variants": sum(len(row["monitors"]) for row in rows),
            "hf_primary_monitor_variant_result_files": sum(
                item["path"].endswith("/results.json") and any(item["path"].startswith(run_id + "/") for run_id in run_ids)
                for item in source_files
            ),
        },
        "id_uniqueness": {"run_rows": len(run_ids), "unique_run_ids": len(set(run_ids)),
                          "task_rows": len(task_ids), "unique_task_ids": len(set(task_ids)),
                          "excluded_unique_run_ids": len(set(excluded_ids))},
        "task_id_hashes": id_hashes(task_ids), "run_id_hashes": id_hashes(run_ids),
        "task_config_id_hashes": id_hashes([row["task_config"] for row in rows]),
        "excluded_run_id_hashes": id_hashes(excluded_ids),
        "sorted_run_ids_sha256": digest("\n".join(sorted(run_ids)).encode()),
        "source_file_digests": inventory,
        "bundle_integrity": {
            "manifest_files": len(listed_files), "manifest_payload_bytes": manifest["total_bytes"],
            "hf_lfs_verified_files": sum(bool(entry.get("lfs_oid")) for entry in hf_entries),
            "hf_git_blob_verified_files": sum(bool(entry.get("git_blob")) for entry in hf_entries),
            "github_git_blob_verified_files": sum(entry["verified_against"] == "github_git_blob_sha1" for entry in manifest["files"]),
            "local_derived_provenance_files": sum(entry["verified_against"] == "local_derived_sha256" for entry in manifest["files"]),
            "legal_text_files_without_publisher_digest": 1,
            "skipped_hf_files": manifest["excluded"]["files"], "skipped_hf_bytes": manifest["excluded"]["bytes"],
            "filesystem_files_including_receipts": len(filesystem_files),
            "filesystem_bytes_including_receipts": sum(path.stat().st_size for path in filesystem_files),
            "problems": 0,
        },
        "verified_after_lock": True, "writable_paths": writable_paths, "adapter_selfcheck": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    result = profile(args.bundle)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: result[key] for key in (
        "counts", "bundle_integrity", "verified_after_lock", "writable_paths")}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
