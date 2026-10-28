#!/usr/bin/env python3
"""Reverify locked bytes; emit only aggregates, digests and hashed identifiers."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from adapter import CORPUS, INDEX_DETECTORS, REPO, REVISION, ROOT, iter_rows, selfcheck

OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"
MANIFEST_SHA256 = "cf3e743f8d3205324fd49045312e2ff02d19522578bf184b4ec51809d9929598"
NOTICE_SHA256 = "ac08955afe5f8ecdc7f81f3f08a525c787b7035f634d1ab46959bdd306d145ec"
RELEASE_SHA256 = "cd4acd0d723fe1f25e41b18b1c5fe7a12ac7d1095ad34145b35b01d7bec92d54"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalise_id(value: str, mode: str) -> str:
    if mode == "bare":
        return value.split("@")[0].rsplit("/", 1)[-1].rsplit("__", 1)[-1].casefold()
    if mode == "full":
        return value.casefold()
    raise ValueError("unknown id normalisation")


def file_digests(path: Path, size: int, with_blob: bool) -> tuple[str, str | None]:
    sha256 = hashlib.sha256()
    git_blob = hashlib.sha1(b"blob %d\0" % size) if with_blob else None
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            sha256.update(block)
            if git_blob is not None:
                git_blob.update(block)
    return sha256.hexdigest(), git_blob.hexdigest() if git_blob is not None else None


def profile(bundle: Path = ROOT) -> dict[str, Any]:
    manifest_bytes = (bundle / "STAGE-MANIFEST.json").read_bytes()
    if digest(manifest_bytes) != MANIFEST_SHA256:
        raise ValueError("staging manifest differs from approved receipt")
    manifest = json.loads(manifest_bytes)
    if (manifest["repo"], manifest["revision"], manifest["licence"]) != (REPO, REVISION, "apache-2.0"):
        raise ValueError("bundle identity or licence differs from pinned receipt")
    if digest((bundle / "CROSS_TEST_ONLY.md").read_bytes()) != NOTICE_SHA256:
        raise ValueError("local-only custody terms changed")
    pins = manifest["pins"]
    if pins["release_asset_sha256"] != RELEASE_SHA256 or pins["release_asset_bytes"] != 24228607:
        raise ValueError("release asset pin changed")
    if pins["tag_commit"] != REVISION or pins["quality_gated_detectors"] != list(INDEX_DETECTORS):
        raise ValueError("tag or quality-gated label pin changed")
    paths = [bundle, *bundle.rglob("*")]
    if any(path.is_symlink() for path in paths):
        raise ValueError("symlinks are forbidden in the immutable bundle")
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
        if sha256 != entry["sha256"] or (git_blob is not None and git_blob != entry["git_blob"]):
            raise ValueError("staged file SHA-256 or publisher Git blob mismatch")
        if entry.get("archive_sha256") and entry["archive_sha256"] != RELEASE_SHA256:
            raise ValueError("extracted file has the wrong archive provenance")
        inventory.append({
            "file_index": index, "file_path_sha256": digest(entry["path"].encode()),
            "bytes": entry["bytes"], "sha256": sha256,
            "category": "native_release" if entry.get("archive_sha256") else "repository_provenance",
            **({"git_blob": git_blob} if git_blob else {}),
        })
        files.add(path)
    filesystem_files = {path for path in paths if path.is_file()}
    if filesystem_files != files | {bundle / "STAGE-MANIFEST.json", bundle / "CROSS_TEST_ONLY.md"}:
        raise ValueError("missing or unmanifested bundle file")
    if sum(e["bytes"] for e in inventory) != manifest["total_bytes"]:
        raise ValueError("manifest total-byte mismatch")
    checks = selfcheck(bundle)
    if checks["issues"] or checks["label_fields_in_steps"]:
        raise ValueError("adapter selfcheck failed")
    rows = list(iter_rows(bundle))
    tasks = sorted({row["task"]["id"] for row in rows})
    runs = [f"{row['config']['slug']}/{row['run_id']}@attempt{row['attempt']}" for row in rows]
    hashes = {
        category: {
            mode: sorted(digest(normalise_id(value, mode).encode()) for value in values)
            for mode in ("bare", "full")
        }
        for category, values in (("task_id_hashes", tasks), ("run_id_hashes", runs))
    }
    leaderboard = json.loads((bundle / "repo/results/v0.1.0/leaderboard.json").read_bytes())
    audit = json.loads((bundle / "repo/results/v0.1.0/audit.json").read_bytes())
    configs = leaderboard["configs"]
    publisher = {
        "planned_runs": sum(c["runs_total"] for c in configs),
        "valid_runs": sum(c["runs_valid"] for c in configs),
        "errored": sum(c["runs_errored"] for c in configs),
        "rules_not_loaded": sum(c["rules_not_loaded"] for c in configs),
        "gated_tamper_positive": sum(c["tamper_f13"]["k"] + c["tamper_f4"]["k"] for c in configs),
        "trap_compound_positive": sum(c["aci"]["k"] for c in configs),
        "trap_compound_denominator": sum(c["aci"]["n"] for c in configs),
        "blind_label_audit_runs": audit["runs"],
        "configurations": len(configs),
        "task_definition_files": len(list((bundle / "repo/tasks").rglob("task.yaml"))),
        "quality_gated_detector_count": len(leaderboard["index_detectors"]),
        "leaderboard_bytes": (bundle / "repo/results/v0.1.0/leaderboard.json").stat().st_size,
    }
    assert publisher == {
        "planned_runs": 1170, "valid_runs": 1162, "errored": 1, "rules_not_loaded": 7,
        "gated_tamper_positive": 62, "trap_compound_positive": 191,
        "trap_compound_denominator": 892, "blind_label_audit_runs": 100,
        "configurations": 10, "task_definition_files": 40,
        "quality_gated_detector_count": 3, "leaderboard_bytes": 580479,
    }
    assert tuple(leaderboard["index_detectors"]) == INDEX_DETECTORS
    return {
        "corpus": CORPUS, "revision": REVISION, "licence": "apache-2.0",
        "manifest_sha256": MANIFEST_SHA256, "notice_sha256": NOTICE_SHA256,
        "release_asset_sha256": RELEASE_SHA256,
        "counts": {k: checks[k] for k in ("rows", "valid_runs", "score_eligible", "positives", "negatives", "ambiguous", "excluded", "unparsed", "task_clusters")},
        "excluded_by_reason": checks["excluded_by_reason"],
        "secondary_trap_compound": checks["secondary_trap_compound"],
        "source_flag_positive_counts": checks["source_flag_positive_counts"],
        "publisher_aggregate_checks": publisher,
        "sweep_differences": {
            "tag_commit_differs": True,
            "raw_tampered_true_is_76_not_62": True,
            "quality_gated_62_agrees_with_sweep": True,
            "primary_ambiguous_excluded": 14,
            "archived_attempts_above_planned_runs": 8,
        },
        "id_uniqueness": {
            category: {mode: {"hashed_rows": len(values), "unique_hashes": len(set(values)), "duplicate_hashes": len(values) - len(set(values))} for mode, values in modes.items()}
            for category, modes in hashes.items()
        },
        **hashes,
        "sorted_composite_run_ids_sha256": digest("\n".join(sorted(runs)).encode()),
        "source_file_digests": inventory,
        "bundle_integrity": {
            "manifest_files": len(files), "manifest_payload_bytes": manifest["total_bytes"],
            "github_blob_verified_files": sum(bool(e.get("git_blob")) for e in manifest["files"]),
            "release_archive_verified_files": sum(bool(e.get("archive_sha256")) for e in manifest["files"]),
            "excluded_repository_files": len(manifest["excluded"]),
            "excluded_repository_bytes": sum(e["bytes"] for e in manifest["excluded"]),
            "filesystem_files_including_receipts": len(filesystem_files),
            "filesystem_bytes_including_receipts": sum(p.stat().st_size for p in filesystem_files),
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
    print(json.dumps({k: result[k] for k in ("counts", "bundle_integrity", "manifest_sha256", "verified_after_lock", "writable_paths")}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
