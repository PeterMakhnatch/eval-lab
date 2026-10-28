#!/usr/bin/env python3
"""Reverify locked MisTrace bytes and emit aggregate counts, digests and hashed ids."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from adapter import CORPUS, FILES, REPO, REVISION, ROOT, iter_rows, selfcheck

OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"
MANIFEST_SHA256 = "6c3531926c33a3ece4a63f310fbedb50c4667490c629be2979c46a8e7d7a66b5"
NOTICE_SHA256 = "051098b5e9b67a710a6cd6b9caa20b1b2dc7485d07423a14bb572e318c770abb"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalise_id(value: str, mode: str) -> str:
    if mode == "bare":
        return value.split("@")[0].rsplit("/", 1)[-1].rsplit("__", 1)[-1].casefold()
    if mode == "full":
        return value.casefold()
    raise ValueError("unknown identity normalisation")


def _file_digests(path: Path, size: int) -> tuple[str, str]:
    sha256 = hashlib.sha256()
    blob = hashlib.sha1(b"blob %d\0" % size)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            sha256.update(block)
            blob.update(block)
    return sha256.hexdigest(), blob.hexdigest()


def profile(bundle: Path = ROOT) -> dict[str, Any]:
    manifest_bytes = (bundle / "STAGE-MANIFEST.json").read_bytes()
    if digest(manifest_bytes) != MANIFEST_SHA256:
        raise ValueError("manifest differs from the pinned staging receipt")
    manifest = json.loads(manifest_bytes)
    if (manifest["repo"], manifest["revision"], manifest["licence"]) != (REPO, REVISION, "cc-by-4.0"):
        raise ValueError("source identity or licence differs from the approved pin")
    if manifest["gated"] is not False or manifest["config"] != "reward-hacking":
        raise ValueError("unexpected gate or selected configuration")
    if digest((bundle / "CROSS_TEST_ONLY.md").read_bytes()) != NOTICE_SHA256:
        raise ValueError("local-only evaluation restrictions changed")
    paths = [bundle, *bundle.rglob("*")]
    if any(path.is_symlink() for path in paths):
        raise ValueError("symlinks are forbidden in the immutable bundle")
    writable_paths = sum(bool(path.stat().st_mode & 0o222) for path in paths)
    if writable_paths:
        raise ValueError("bundle is not locked read-only")
    inventory = []
    files: set[Path] = set()
    for index, entry in enumerate(manifest["files"], 1):
        relative = PurePosixPath(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe manifest path")
        path = bundle / relative
        if path in files or not path.is_file() or path.stat().st_size != entry["bytes"]:
            raise ValueError("duplicate, missing or wrong-sized manifest file")
        sha256, blob = _file_digests(path, entry["bytes"])
        if sha256 != entry["sha256"] or sha256 != entry["sha256_computed"]:
            raise ValueError("staged file SHA-256 mismatch")
        verification = entry["verified_against"]
        if verification == "hub_git_blob_sha1":
            if blob != entry["blob_id"]:
                raise ValueError("publisher Git blob digest mismatch")
        elif verification != "hub_lfs_sha256":
            raise ValueError("missing publisher digest verification")
        inventory.append({
            "file_index": index, "file_path_sha256": digest(entry["path"].encode()),
            "bytes": entry["bytes"], "sha256": sha256,
            "publisher_digest_kind": verification,
            "publisher_digest": blob if verification == "hub_git_blob_sha1" else sha256,
        })
        files.add(path)
    filesystem_files = {path for path in paths if path.is_file()}
    if filesystem_files != files | {bundle / "STAGE-MANIFEST.json", bundle / "CROSS_TEST_ONLY.md"}:
        raise ValueError("missing or unmanifested bundle files")
    if sum(entry["bytes"] for entry in inventory) != manifest["total_bytes"]:
        raise ValueError("manifest payload-byte count mismatch")
    excluded = manifest["excluded"]
    selected_paths = {entry["path"] for entry in manifest["files"]}
    excluded_paths = {entry["path"] for entry in excluded}
    if (len(excluded_paths) != len(excluded) or selected_paths & excluded_paths
            or len(selected_paths | excluded_paths) != manifest["source_file_count"]):
        raise ValueError("source inventory/exclusion coverage mismatch")
    if manifest["total_bytes"] + sum(entry["bytes"] for entry in excluded) != manifest["source_total_bytes"]:
        raise ValueError("source inventory total-byte count mismatch")
    checks = selfcheck(bundle)
    identities = [(row["task_id"], row["rollout_id"]) for row in iter_rows(bundle)]
    ids = {"task": sorted({task for task, _ in identities}),
           "run": sorted({run for _, run in identities})}
    hashed = {
        kind: {mode: sorted(digest(normalise_id(value, mode).encode("utf-8")) for value in values)
               for mode in ("bare", "full")}
        for kind, values in ids.items()
    }
    return {
        "corpus": CORPUS, "revision": REVISION, "licence": "cc-by-4.0",
        "manifest_sha256": MANIFEST_SHA256, "notice_sha256": NOTICE_SHA256,
        "counts": checks["counts"], "label_states": checks["label_states"],
        "excluded_by_reason": checks["excluded_by_reason"],
        "per_model": checks["models"], "mechanisms": checks["mechanisms"],
        "source_sandbox_counts": checks["source_sandbox_counts"],
        "segments": checks["segments"], "human_vs_judge_confusion": checks["human_vs_judge_confusion"],
        "segment_level_localization_scorable": True,
        "command_level_localization_scorable": False,
        "known_family_overlap": True,
        "known_family_overlap_corpora": ["Reflex-Sentinel", "Assistant Axis", "Goodfire"],
        "bootstrap": {"cluster_key": "task", "clusters": len(ids["task"]),
                      "primary_scorable_clusters": checks["counts"]["primary_unique_tasks"],
                      "stratifier": "model", "resamples": 10000, "seed": 0},
        "task_id_hashes": hashed["task"], "run_id_hashes": hashed["run"],
        "identity_counts": {
            kind: {mode: {"hashed_ids": len(values), "unique_hashes": len(set(values))}
                   for mode, values in normalisations.items()}
            for kind, normalisations in hashed.items()
        },
        "sorted_raw_id_digests": {
            kind: digest("\n".join(values).encode("utf-8")) for kind, values in ids.items()
        },
        "parent_measured_overlap": {
            split: {mode: 0 for mode in ("bare_bare", "bare_full", "full_bare", "full_full")}
            for split in ("DEV", "TEST", "cloud_task_store")
        },
        "source_file_digests": inventory,
        "bundle_integrity": {
            "manifest_files": len(files), "manifest_payload_bytes": manifest["total_bytes"],
            "parquet_files": len(FILES),
            "parquet_bytes": sum((bundle / name).stat().st_size for name in FILES),
            "source_inventory_files": manifest["source_file_count"],
            "source_inventory_bytes": manifest["source_total_bytes"],
            "excluded_source_files": len(excluded),
            "excluded_source_bytes": sum(entry["bytes"] for entry in excluded),
            "hf_git_blob_verified_files": sum(entry["verified_against"] == "hub_git_blob_sha1"
                                               for entry in manifest["files"]),
            "hf_lfs_verified_files": sum(entry["verified_against"] == "hub_lfs_sha256"
                                          for entry in manifest["files"]),
            "filesystem_files_including_receipts": len(filesystem_files),
            "filesystem_bytes_including_receipts": sum(path.stat().st_size for path in filesystem_files),
            "problems": 0,
        },
        "verified_after_lock": True, "writable_paths": writable_paths,
        "adapter_selfcheck": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    result = profile(args.bundle)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"counts": result["counts"], "label_states": result["label_states"],
                      "bundle_integrity": result["bundle_integrity"],
                      "verified_after_lock": result["verified_after_lock"],
                      "writable_paths": result["writable_paths"]}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
