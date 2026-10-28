#!/usr/bin/env python3
"""Reverify the locked local bundle; emit only aggregates, digests and hashed IDs."""

from __future__ import annotations

import argparse
import binascii
import csv
import hashlib
import json
import struct
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from adapter import CORPUS, REPO, REVISION, ROOT, selfcheck

OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def id_hashes(values: list[str]) -> dict[str, list[str]]:
    def bare(value: str) -> str:
        return value.split("@")[0].rsplit("/", 1)[-1].rsplit("__", 1)[-1].casefold()

    return {
        "bare": [digest(bare(value).encode()) for value in values],
        "full": [digest(value.casefold().encode()) for value in values],
    }


def central_members(data: bytes) -> list[dict[str, Any]]:
    """Read the retained ZIP directory, independently of the extraction inventory."""
    members = []
    offset = 0
    while offset < len(data):
        fields = struct.unpack_from("<4s6H3I5H2I", data, offset)
        if fields[0] != b"PK\x01\x02":
            raise ValueError("unexpected central-directory record")
        name_bytes, extra_bytes, comment_bytes = fields[10:13]
        name = data[offset + 46:offset + 46 + name_bytes].decode(
            "utf-8" if fields[3] & 0x800 else "cp437"
        )
        size, compressed = fields[9], fields[8]
        extra_start = offset + 46 + name_bytes
        extra = data[extra_start:extra_start + extra_bytes]
        cursor = 0
        while cursor < len(extra):
            kind, length = struct.unpack_from("<HH", extra, cursor)
            payload = extra[cursor + 4:cursor + 4 + length]
            if len(payload) != length:
                raise ValueError("truncated ZIP extra field")
            if kind == 1:  # ZIP64 can encode small members too.
                position = 0
                if size == 0xFFFFFFFF:
                    size = struct.unpack_from("<Q", payload, position)[0]
                    position += 8
                if compressed == 0xFFFFFFFF:
                    compressed = struct.unpack_from("<Q", payload, position)[0]
            cursor += 4 + length
        if size == 0xFFFFFFFF or compressed == 0xFFFFFFFF:
            raise ValueError("missing ZIP64 member sizes")
        members.append({
            "path": name, "bytes": size, "compressed_bytes": compressed,
            "crc32": f"{fields[7]:08x}",
        })
        offset += 46 + name_bytes + extra_bytes + comment_bytes
    if offset != len(data):
        raise ValueError("truncated central directory")
    return members


def package_selected(name: str) -> bool:
    path = PurePosixPath(name)
    if name.endswith("/"):
        return False
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe ZIP member")
    if path.parts[0] in {"traces", "traces.csv", "trace_regrades.csv", "chart.gif"}:
        return False
    return len(path.parts) == 1 or path.parts[0] in {"grader", "evidence_verified", "tools"}


def profile(bundle: Path = ROOT) -> dict[str, Any]:
    bundle = Path(bundle)
    manifest_bytes = (bundle / "STAGE-MANIFEST.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if (manifest["repo"], manifest["revision"], manifest["licence"]) != (
        REPO, REVISION, "cc-by-4.0"
    ):
        raise ValueError("approved corpus identity, revision or licence changed")
    if manifest["pins"]["kaggle_version"] != 20:
        raise ValueError("Kaggle package version changed")
    if {entry["licence"] for entry in manifest["licences"]} != {"cc-by-4.0", "mit"}:
        raise ValueError("mixed-licence provenance is incomplete")
    paths = [bundle, *bundle.rglob("*")]
    if any(path.is_symlink() for path in paths):
        raise ValueError("symlink in staged bundle")
    writable = sum(bool(path.stat().st_mode & 0o222) for path in paths)
    if writable:
        raise ValueError("bundle must be locked before profiling")

    entries = {}
    verified = Counter()
    digest_table = []
    for entry in manifest["files"]:
        relative = entry["path"]
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or relative in entries:
            raise ValueError("unsafe or duplicate manifest member")
        path = bundle / relative
        data = path.read_bytes()
        sha256 = digest(data)
        if len(data) != entry["bytes"] or sha256 != entry["sha256"]:
            raise ValueError("manifest digest or size mismatch")
        if "zip_crc32" in entry:
            if f"{binascii.crc32(data) & 0xffffffff:08x}" != entry["zip_crc32"]:
                raise ValueError("ZIP CRC32 mismatch")
            verified["zip_crc32_files"] += 1
        if "git_blob" in entry:
            blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            if blob != entry["git_blob"]:
                raise ValueError("pinned upstream Git blob mismatch")
            verified["upstream_git_blob_files"] += 1
        if "publisher_sha256" in entry:
            if sha256 != entry["publisher_sha256"]:
                raise ValueError("publisher evidence digest mismatch")
            verified["publisher_sha256_files"] += 1
        entries[relative] = entry
        digest_table.append({"file_key": digest(relative.encode()), "sha256": sha256})
    actual_files = {str(path.relative_to(bundle)) for path in paths if path.is_file()}
    expected_files = set(entries) | {"STAGE-MANIFEST.json", "CROSS_TEST_ONLY.md"}
    if actual_files != expected_files:
        raise ValueError("unmanifested or missing local file")
    if sum(entry["bytes"] for entry in entries.values()) != manifest["total_bytes"]:
        raise ValueError("manifest total byte count mismatch")

    central = (bundle / manifest["pins"]["zip_central_directory_path"]).read_bytes()
    if digest(central) != manifest["pins"]["zip_central_directory_sha256"]:
        raise ValueError("ZIP central-directory digest mismatch")
    members = central_members(central)
    if members != manifest["pins"]["zip_members"]:
        raise ValueError("ZIP per-member pins differ from retained central directory")
    selected = {row["path"]: row for row in members if package_selected(row["path"])}
    omitted = [row for row in members
               if not row["path"].endswith("/") and not package_selected(row["path"])]
    zip_paths = {name for name, entry in entries.items() if "zip_crc32" in entry}
    if zip_paths != set(selected):
        raise ValueError("selected package membership is incomplete")
    for name, row in selected.items():
        if (entries[name]["bytes"], entries[name]["zip_crc32"]) != (row["bytes"], row["crc32"]):
            raise ValueError("member pin does not match manifest")
    excluded = manifest["excluded"]
    if (len(omitted), sum(row["bytes"] for row in omitted)) != (
        excluded["member_count"], excluded["bytes"]
    ):
        raise ValueError("excluded member accounting mismatch")

    upstream_tree = json.loads((bundle / "provenance/upstream-git-tree.json").read_bytes())
    tree_blobs = {row["path"]: row for row in upstream_tree["tree"] if row["type"] == "blob"}
    source_entries = {
        name.removeprefix("upstream/zapier__AutomationBench/"): entry
        for name, entry in entries.items() if "git_blob" in entry
    }
    if upstream_tree.get("truncated") or source_entries.keys() != tree_blobs.keys():
        raise ValueError("upstream tree membership is incomplete")
    for name, row in tree_blobs.items():
        if (source_entries[name]["git_blob"], source_entries[name]["bytes"]) != (row["sha"], row["size"]):
            raise ValueError("upstream Git tree pin mismatch")

    publisher_checks = Counter()
    missing = []
    for path in sorted((bundle / "evidence_verified").glob("*/manifest.json")):
        metadata = json.loads(path.read_text())
        publisher_checks["evidence_folders"] += 1
        if metadata["grader_patch_sha256"] != manifest["pins"]["grader_patch_sha256"]:
            raise ValueError("publisher patch pin mismatch")
        for relative, expected in metadata["files"].items():
            target = path.parent / relative
            entry = entries.get(str(target.relative_to(bundle)))
            if entry is None:
                missing.append({
                    "manifest_key": digest(str(path.relative_to(bundle)).encode()),
                    "member_key": digest(relative.encode()), "sha256": expected,
                    "suffix": Path(relative).suffix,
                })
                continue
            if entry["sha256"] != expected:
                raise ValueError("publisher declared member digest mismatch")
            publisher_checks["sha256_checked"] += 1
    expected_missing = [
        {key: row[key] for key in ("manifest_key", "member_key", "sha256", "suffix")}
        for row in manifest["publisher_inventory"]["missing_declarations"]
    ]
    if missing != expected_missing or len(missing) != 3 or any(row["suffix"] != ".pyc" for row in missing):
        raise ValueError("unaccounted publisher inventory gap")

    checks = selfcheck(bundle)
    with (bundle / "tasks.csv").open(newline="") as stream:
        tasks = list(csv.DictReader(stream))
    with (bundle / "fixes.csv").open(newline="") as stream:
        fixes = list(csv.DictReader(stream))
    if any(row["status"] != "verified" for row in fixes):
        raise ValueError("unverified fix row")
    fixed_names = {row["task_name"] for row in fixes}
    if len(fixes) != len(fixed_names) or any(
        (row["fixed"] == "True") != (row["task_name"] in fixed_names) for row in tasks
    ):
        raise ValueError("task/fix label join mismatch")
    names = [row["task_name"] for row in tasks]
    example_ids = [row["example_id"] for row in tasks]
    verified_names = [row["task_name"] for row in fixes]
    scored_by_label = Counter(
        ("defective_verified" if row["task_name"] in fixed_names else "unchanged_not_certified")
        + ("/scored" if row["scored"] == "True" else "/simple") for row in tasks
    )
    return {
        "corpus": CORPUS, "revision": REVISION, "bundle": str(bundle),
        "manifest_sha256": digest(manifest_bytes),
        "licence_counts": {"cc-by-4.0": 1, "mit": 1},
        "counts": {
            "rows": len(tasks), "positives": len(fixes), "negatives": 0,
            "unlabeled": len(tasks) - len(fixes),
            "unlabeled_not_certified": len(tasks) - len(fixes),
            "manifest_files": len(entries), "manifest_payload_bytes": manifest["total_bytes"],
            "filesystem_files_including_receipts": len(actual_files),
            "filesystem_bytes_including_receipts": sum(
                path.stat().st_size for path in paths if path.is_file()
            ),
            "zip_members": len(members), "excluded_members": len(omitted),
            "excluded_bytes": sum(row["bytes"] for row in omitted),
        },
        "label_counts": checks["label_counts"],
        "issue_group_counts": checks["issue_group_counts"],
        "domain_counts": checks["domain_counts"], "scored_counts": checks["scored_counts"],
        "label_scored_counts": dict(sorted(scored_by_label.items())),
        "issue_type_hashed_counts": dict(sorted(Counter(digest(row["issue_type"].encode()) for row in fixes).items())),
        "fix_domain_counts": dict(sorted(Counter(row["domain"] for row in fixes).items())),
        "verified_after_lock": True, "writable_paths": writable,
        "bundle_integrity": {**dict(verified), "verified_manifest_files": len(entries), "problems": 0},
        "zip_central_directory_sha256": digest(central),
        "file_digest_table_sha256": digest(json.dumps(
            sorted(digest_table, key=lambda row: row["file_key"]),
            sort_keys=True, separators=(",", ":")
        ).encode()),
        "publisher_inventory_counts": {
            **dict(publisher_checks), "absent_pyc_declarations": len(missing),
            "present_declaration_digest_mismatches": 0,
        },
        "adapter_selfcheck": checks,
        "task_id_hashes": id_hashes(names),
        "example_id_hashes": id_hashes(example_ids),
        "verified_task_id_hashes": id_hashes(verified_names),
        "verified_example_id_hashes": id_hashes([row["example_id"] for row in fixes]),
        "id_uniqueness": {
            "task_names": len(set(names)), "example_ids": len(set(example_ids)),
            "duplicate_example_id_rows": len(example_ids) - len(set(example_ids)),
            "verified_task_names": len(set(verified_names)),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    try:
        result = profile(args.bundle)
    except Exception:
        raise SystemExit("profile verification failed; corpus contents suppressed") from None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "counts": result["counts"], "label_counts": result["label_counts"],
        "reconstruction_counts": result["adapter_selfcheck"]["counts"],
        "verified_after_lock": result["verified_after_lock"],
        "writable_paths": result["writable_paths"],
        "manifest_sha256": result["manifest_sha256"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
