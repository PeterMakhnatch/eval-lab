#!/usr/bin/env python3
"""Reverify the locked PAIChecker bundle; emit aggregates, digests and hashed ids only."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from adapter import CORPUS, REPO, REVISION, ROOT, SUB_CORPORA, load_labels, selfcheck

OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"
MANIFEST_SHA256 = "7c6709f726f8305e516fc8cbbee8a40d03b7409f4978ebdce907724ef6a53ab2"
NOTICE_SHA256 = "a57549ef1729bfe9ea998f97cb88dac9eab070a83a97026423ad1f885867d674"
HF_PINS = {
    "SWE-bench/SWE-bench_Multilingual": "846e647b9f33c0b51b739d005d13d85493c9af09",
    "SWE-Gym/SWE-Gym": "bb94ed9e39bbeb96a7fcbfb533b80f25a7fd59cb",
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digests(path: Path, size: int, with_blob: bool) -> tuple[str, str | None]:
    sha256 = hashlib.sha256()
    git_blob = hashlib.sha1(b"blob %d\0" % size) if with_blob else None
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            sha256.update(block)
            if git_blob is not None:
                git_blob.update(block)
    return sha256.hexdigest(), git_blob.hexdigest() if git_blob is not None else None


def normalise_id(instance_id: str, mode: str) -> str:
    if mode == "bare":
        return instance_id.split("@")[0].rsplit("/", 1)[-1].rsplit("__", 1)[-1].casefold()
    if mode == "full":
        return instance_id.casefold()
    raise ValueError("unknown task id normalisation")


def profile(bundle: Path = ROOT) -> dict[str, Any]:
    manifest_bytes = (bundle / "STAGE-MANIFEST.json").read_bytes()
    if digest(manifest_bytes) != MANIFEST_SHA256:
        raise ValueError("manifest differs from the approved staging receipt")
    manifest = json.loads(manifest_bytes)
    if (manifest["repo"], manifest["revision"], manifest["licence"]) != (REPO, REVISION, "mit"):
        raise ValueError("bundle identity/licence differs from approved pin")
    if digest((bundle / "CROSS_TEST_ONLY.md").read_bytes()) != NOTICE_SHA256:
        raise ValueError("local-only evaluation restrictions changed")
    paths = [bundle, *bundle.rglob("*")]
    if any(path.is_symlink() for path in paths):
        raise ValueError("symlinks are not permitted in the immutable bundle")
    writable_paths = sum(bool(path.stat().st_mode & 0o222) for path in paths)
    if writable_paths:
        raise ValueError("bundle is not locked read-only")
    files = set()
    inventory = []
    for index, entry in enumerate(manifest["files"], 1):
        relative = PurePosixPath(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe manifest path")
        path = bundle / relative
        if path in files or not path.is_file() or path.stat().st_size != entry["bytes"]:
            raise ValueError("duplicate, missing or wrong-sized manifest file")
        sha256, git_blob = file_digests(path, entry["bytes"], bool(entry.get("git_blob")))
        if sha256 != entry["sha256"]:
            raise ValueError("manifest file SHA-256 mismatch")
        if git_blob is not None and git_blob != entry["git_blob"]:
            raise ValueError("publisher Git blob SHA-1 mismatch")
        if entry.get("lfs_oid") and entry["lfs_oid"] != "sha256:" + sha256:
            raise ValueError("publisher LFS oid mismatch")
        inventory.append(
            {
                "file_index": index,
                "file_path_sha256": digest(entry["path"].encode("utf-8")),
                "bytes": entry["bytes"],
                "sha256": sha256,
                **({"git_blob": git_blob} if git_blob else {}),
                **({"lfs_oid": entry["lfs_oid"]} if entry.get("lfs_oid") else {}),
            }
        )
        files.add(path)
    filesystem_files = {path for path in paths if path.is_file()}
    expected_files = files | {bundle / "STAGE-MANIFEST.json", bundle / "CROSS_TEST_ONLY.md"}
    if filesystem_files != expected_files:
        raise ValueError("unmanifested or missing bundle files")
    if sum(item["bytes"] for item in inventory) != manifest["total_bytes"]:
        raise ValueError("manifest total-byte count mismatch")
    if {source["repo"]: source["revision"] for source in manifest["sources"]} != HF_PINS:
        raise ValueError("grader source pins differ from the approved revisions")
    entries_by_path = {entry["path"]: entry for entry in manifest["files"]}
    for source in manifest["sources"]:
        if source["licence"] != "mit" or source["gated"] is not False:
            raise ValueError("grader source licence/gating differs from approval")
        for source_file in source["files"]:
            entry = entries_by_path[source_file["path"]]
            if source_file["sha256"] != entry["sha256"]:
                raise ValueError("grader source digest differs from file manifest")
            if source_file.get("lfs_oid") != entry.get("lfs_oid"):
                raise ValueError("grader source LFS oid differs from file manifest")
    checks = selfcheck(bundle)
    if checks["issues"] or checks["label_fields_in_auditor_input"]:
        raise ValueError("adapter selfcheck failed")
    ids = []
    per_sub_corpus_id_counts = {}
    for name, source in SUB_CORPORA.items():
        sub_ids = list(load_labels(bundle / source["labels"]))
        ids.extend(sub_ids)
        per_sub_corpus_id_counts[name] = {
            mode: {
                "rows": len(sub_ids),
                "unique_hashes": len({normalise_id(value, mode) for value in sub_ids}),
            }
            for mode in ("bare", "full")
        }
    task_id_hashes = {
        mode: sorted(digest(normalise_id(value, mode).encode("utf-8")) for value in ids)
        for mode in ("bare", "full")
    }
    gym = checks["sub_corpora"]["swe_gym"]
    if gym["grader_rows"] != 2438 or gym["grader_unlabelled_ids"] != 151:
        raise ValueError("paper/file denominator gap differs from the pinned receipt")
    return {
        "corpus": CORPUS,
        "revision": REVISION,
        "licence": "mit",
        "manifest_sha256": MANIFEST_SHA256,
        "notice_sha256": NOTICE_SHA256,
        "counts": {key: checks[key] for key in ("rows", "positives", "negatives", "unlabeled")},
        "sub_corpora": checks["sub_corpora"],
        "paper_vs_file": {
            "swe_gym_reported_paper_rows": 2438,
            "swe_gym_label_file_rows": gym["label_rows"],
            "swe_gym_absent_from_label_file": gym["grader_unlabelled_ids"],
        },
        "id_uniqueness": {
            "raw_unique_ids": len(set(ids)),
            "raw_duplicate_rows": len(ids) - len(set(ids)),
            "normalisations": {
                mode: {
                    "hashed_rows": len(values),
                    "unique_hashes": len(set(values)),
                    "duplicate_hashes": len(values) - len(set(values)),
                }
                for mode, values in task_id_hashes.items()
            },
            "sub_corpora": per_sub_corpus_id_counts,
        },
        "sorted_raw_ids_sha256": digest("\n".join(sorted(ids)).encode("utf-8")),
        "task_id_hashes": task_id_hashes,
        "source_file_digests": inventory,
        "bundle_integrity": {
            "manifest_files": len(files),
            "manifest_payload_bytes": manifest["total_bytes"],
            "github_blob_verified_files": sum(
                entry.get("verified_against") == "github_git_blob_sha1"
                for entry in manifest["files"]
            ),
            "hf_git_blob_verified_files": sum(
                entry.get("verified_against") == "hub_git_blob_sha1"
                for entry in manifest["files"]
            ),
            "hf_lfs_verified_files": sum(bool(entry.get("lfs_oid")) for entry in manifest["files"]),
            "filesystem_files_including_receipts": len(filesystem_files),
            "filesystem_bytes_including_receipts": sum(path.stat().st_size for path in filesystem_files),
            "problems": 0,
        },
        "verified_after_lock": True,
        "writable_paths": writable_paths,
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
    print(
        json.dumps(
            {
                "counts": result["counts"],
                "sub_corpora": result["sub_corpora"],
                "bundle_integrity": result["bundle_integrity"],
                "verified_after_lock": result["verified_after_lock"],
                "writable_paths": result["writable_paths"],
                "label_fields_in_auditor_input": result["adapter_selfcheck"]["label_fields_in_auditor_input"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
