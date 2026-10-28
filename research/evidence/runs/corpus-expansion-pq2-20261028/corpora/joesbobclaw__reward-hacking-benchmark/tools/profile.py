#!/usr/bin/env python3
"""Reverify the immutable bundle; emit counts, digests and hashed ids only."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from adapter import CORPUS, REPO, REVISION, ROOT, STUDIES, digest, iter_rows, selfcheck

OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"
MANIFEST_SHA256 = "2bdb0df19b002603519e99a733392bb253a70a025e2e8bb0d105a9d4d34ca9e5"
NOTICE_SHA256 = "ee030b31d7ae4e39ed2fc4395e98df3bc33fa5b3fcdcfd2d28cb1f68e2c2f649"


def normalise_id(value: str, mode: str) -> str:
    if mode == "bare":
        return value.split("@")[0].rsplit("/", 1)[-1].rsplit("__", 1)[-1].casefold()
    if mode == "full":
        return value.casefold()
    raise ValueError("unknown id normalisation")


def file_digests(path: Path, size: int) -> tuple[str, str]:
    sha256 = hashlib.sha256()
    git_blob = hashlib.sha1(b"blob %d\0" % size)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            sha256.update(block)
            git_blob.update(block)
    return sha256.hexdigest(), git_blob.hexdigest()


def profile(root: Path = ROOT) -> dict[str, Any]:
    manifest_bytes = (root / "STAGE-MANIFEST.json").read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != MANIFEST_SHA256:
        raise ValueError("manifest differs from pinned staging receipt")
    manifest = json.loads(manifest_bytes)
    if (manifest["repo"], manifest["revision"], manifest["licence"]) != (
        REPO, REVISION, "apache-2.0"
    ):
        raise ValueError("bundle identity or licence changed")
    if hashlib.sha256((root / "CROSS_TEST_ONLY.md").read_bytes()).hexdigest() != NOTICE_SHA256:
        raise ValueError("local custody restrictions changed")
    paths = [root, *root.rglob("*")]
    if any(path.is_symlink() for path in paths):
        raise ValueError("immutable bundle contains symlinks")
    writable_paths = sum(bool(path.stat().st_mode & 0o222) for path in paths)
    if writable_paths:
        raise ValueError("bundle is not locked read-only")
    expected: set[Path] = set()
    inventory = []
    categories: dict[str, Counter[str]] = {}
    for index, entry in enumerate(manifest["files"], 1):
        relative = PurePosixPath(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("unsafe manifest path")
        path = root / relative
        if path in expected or not path.is_file() or path.stat().st_size != entry["bytes"]:
            raise ValueError("duplicate, missing or wrong-sized payload file")
        sha256, git_blob = file_digests(path, entry["bytes"])
        if sha256 != entry["sha256"] or git_blob != entry["git_blob"]:
            raise ValueError("SHA-256 or publisher Git blob SHA-1 mismatch")
        if entry["verified_against"] != "github_git_blob_sha1":
            raise ValueError("publisher blob verification missing")
        inventory.append(
            {"file_index": index, "file_path_sha256": digest(entry["path"]),
             "bytes": entry["bytes"], "sha256": sha256, "git_blob": git_blob}
        )
        category = relative.parts[0] if len(relative.parts) > 1 else "root_metadata"
        counts = categories.setdefault(category, Counter())
        counts["files"] += 1
        counts["bytes"] += entry["bytes"]
        expected.add(path)
    receipts = {root / "STAGE-MANIFEST.json", root / "CROSS_TEST_ONLY.md"}
    filesystem_files = {path for path in paths if path.is_file()}
    if filesystem_files != expected | receipts:
        raise ValueError("missing or unmanifested bundle files")
    if sum(entry["bytes"] for entry in manifest["files"]) != manifest["total_bytes"]:
        raise ValueError("manifest byte total mismatch")
    if (len(expected), manifest["total_bytes"]) != (1710, 21221998):
        raise ValueError("publisher whole-tree inventory changed")
    if manifest["excluded"] or not manifest["source_tree_complete"]:
        raise ValueError("published tree is not completely staged")
    checks = selfcheck(root)
    task_ids, run_ids, raw_run_ids = [], [], []
    for study, _, source_manifest, row in iter_rows(root):
        task_ids.append(source_manifest["scenario_sha256"])
        run_ids.append(f"{study}/{row['run_id']}")
        raw_run_ids.append(row["run_id"])
    hashed_ids = {
        kind: {
            mode: sorted(digest(normalise_id(value, mode)) for value in values)
            for mode in ("bare", "full")
        }
        for kind, values in (("task", task_ids), ("run", run_ids))
    }
    cohort_run_files = {f"runs/{study}/runs.jsonl" for study, _ in STUDIES}
    cohort_summary_files = {path.replace("runs.jsonl", "summary.csv") for path in cohort_run_files}
    archive_counts: Counter[str] = Counter()
    for entry in manifest["files"]:
        relative = entry["path"]
        if relative.endswith("runs.jsonl") and relative not in cohort_run_files:
            archive_counts["run_files"] += 1
            archive_counts["run_file_bytes"] += entry["bytes"]
            with (root / relative).open(encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        row = json.loads(line)
                        archive_counts["rows"] += 1
                        archive_counts[row["status"]] += 1
    assert archive_counts["rows"] == 211
    cohort_bytes = {
        "runs_files": len(cohort_run_files),
        "runs_bytes": sum(entry["bytes"] for entry in manifest["files"] if entry["path"] in cohort_run_files),
        "summary_files": len(cohort_summary_files),
        "summary_bytes": sum(entry["bytes"] for entry in manifest["files"] if entry["path"] in cohort_summary_files),
    }
    assert (cohort_bytes["runs_bytes"], cohort_bytes["summary_bytes"]) == (14312574, 105727)
    return {
        "corpus": CORPUS, "revision": REVISION, "licence": "apache-2.0",
        "manifest_sha256": MANIFEST_SHA256, "notice_sha256": NOTICE_SHA256,
        "adapter_sha256": hashlib.sha256(Path(__file__).with_name("adapter.py").read_bytes()).hexdigest(),
        "profile_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "counts": {
            "rows": checks["counts"]["rows"], "completed": checks["counts"]["completed"],
            "positives": checks["counts"]["positive"], "negatives": checks["counts"]["negative"],
            "excluded": checks["counts"]["excluded"],
            "attempt_successes": checks["counts"]["attempt_successes"],
            "attempt_failures": checks["counts"]["attempt_failures"],
            "completed_without_success": checks["counts"]["completed_without_success"],
        },
        "excluded_by_reason": checks["excluded_by_reason"],
        "cohort_files": cohort_bytes,
        "archived_outside_cohort": dict(archive_counts),
        "study_family_count": 7, "study_collection_count": len(STUDIES),
        "bootstrap_cluster_count": checks["bootstrap_clusters"],
        "per_collection_counts": checks["by_collection"],
        "per_model_counts": checks["by_model"],
        "per_arm_sha256_counts": checks["by_arm_sha256"],
        "per_condition_sha256_counts": checks["by_condition_sha256"],
        "per_phase_sha256_counts": checks["by_phase_sha256"],
        "id_uniqueness": {
            "raw_run_ids": len(set(raw_run_ids)), "canonical_run_ids": len(set(run_ids)),
            "family_run_key_unique": checks["family_run_key_unique"],
            "normalisations": {
                kind: {mode: {"hashed_rows": len(values), "unique_hashes": len(set(values)),
                              "duplicate_hashes": len(values) - len(set(values))}
                       for mode, values in modes.items()}
                for kind, modes in hashed_ids.items()
            },
        },
        "sorted_canonical_run_ids_sha256": digest("\n".join(sorted(run_ids))),
        "task_id_hashes": hashed_ids["task"], "run_id_hashes": hashed_ids["run"],
        "file_groups": {key: dict(value) for key, value in sorted(categories.items())},
        "source_file_digests": inventory,
        "bundle_integrity": {
            "manifest_files": len(expected), "manifest_payload_bytes": manifest["total_bytes"],
            "github_blob_verified_files": len(expected),
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
    print(json.dumps({key: result[key] for key in (
        "counts", "excluded_by_reason", "cohort_files", "bundle_integrity",
        "verified_after_lock", "writable_paths",
    )}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
