#!/usr/bin/env python3
"""Profile the pinned local bundle, writing aggregates and digests only."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import statistics
import struct
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import adapter

OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ids_digest(ids: list[str]) -> str:
    """SHA-256 of sorted UTF-8 identities, each followed by one newline."""
    return hashlib.sha256("".join(identity + "\n" for identity in sorted(ids)).encode()).hexdigest()


def stats(values: list[int]) -> dict[str, int | float]:
    return {
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
        "total": sum(values),
    }


def npy_header(stream: Any) -> dict[str, Any]:
    if stream.read(6) != b"\x93NUMPY":
        raise ValueError("invalid numpy magic")
    version = tuple(stream.read(2))
    length_size = 2 if version == (1, 0) else 4
    header_size = struct.unpack("<H" if length_size == 2 else "<I", stream.read(length_size))[0]
    header = ast.literal_eval(stream.read(header_size).decode("latin1"))
    return {
        "shape": list(header["shape"]),
        "dtype": header["descr"],
        "fortran": header["fortran_order"],
    }


def profile(root: Path) -> dict[str, Any]:
    manifest_path = root / "STAGE-MANIFEST.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest["repo"] != adapter.REPO or manifest["revision"] != adapter.REVISION:
        raise ValueError("bundle identity does not match the vetted pin")
    if manifest["licence"] != "cc-by-4.0" or manifest["gated"]:
        raise ValueError("unexpected licence or access terms")
    files_by_directory: dict[str, Counter[str]] = defaultdict(Counter)
    inventory = []
    named_digests = {}
    digest_problems: Counter[str] = Counter()
    for item in manifest["files"]:
        path = root / item["path"]
        digest = sha256(path)
        size = path.stat().st_size
        if digest != item["sha256_computed"]:
            digest_problems["computed_sha256"] += 1
        if size != item["bytes"]:
            digest_problems["size"] += 1
        if item["sha256"]:
            if digest != item["sha256"]:
                digest_problems["hub_lfs_sha256"] += 1
        else:
            data = path.read_bytes()
            blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
            if blob != item["blob_id"]:
                digest_problems["hub_git_blob_sha1"] += 1
        group = path.relative_to(root).parts[0] if "/" in item["path"] else "metadata"
        files_by_directory[group].update({"files": 1, "bytes": size})
        # Feature filenames contain raw record ids; persist their path digest instead.
        inventory.append(
            {"path_key": adapter.trace_key(item["path"]), "bytes": size, "sha256": digest}
        )
        if not item["path"].endswith(".npy"):
            named_digests[item["path"]] = {
                "bytes": size,
                "sha256": digest,
                "git_blob": item["blob_id"],
                "verified_against": item["verified_against"],
            }
    paths = [root, *root.rglob("*")]
    writable_files = sum(bool(path.stat().st_mode & 0o222) for path in paths if path.is_file())
    writable_directories = sum(bool(path.stat().st_mode & 0o222) for path in paths if path.is_dir())
    agents = list(adapter.iter_rows(root))
    parent_map = {row["id"]: row for row in agents}
    cohorts = {}
    for subset in adapter.FILES:
        records = agents if subset == "agent" else list(adapter.iter_rows(root, subset))
        ids = [adapter.record_id(row, subset) for row in records]
        labels = Counter()
        tasks: dict[str, Counter[str]] = defaultdict(Counter)
        splits = Counter()
        fields: dict[str, Counter[str]] = defaultdict(Counter)
        for row in records:
            parent = row if subset == "agent" else parent_map[row["id"]]
            label_field = "test_tampered" if subset == "agent" else "cheat"
            label = (
                "positive"
                if row[label_field] is True
                else "negative"
                if row[label_field] is False
                else "unlabeled"
            )
            labels[label] += 1
            tasks[adapter.trace_key(parent["task_id"])].update({"rows": 1, label: 1})
            splits[parent["split"]] += 1
            for field in adapter.GOLD_FIELDS[subset]:
                fields[field][json.dumps(row[field])] += 1
        cohorts[subset] = {
            "rows": len(records),
            "positives": labels["positive"],
            "negatives": labels["negative"],
            "unlabeled": labels["unlabeled"],
            "label_field": "test_tampered" if subset == "agent" else "cheat",
            "per_label_field": {field: dict(counts) for field, counts in fields.items()},
            "per_generator": {adapter.GENERATOR: len(records)},
            "generator_provenance": "dataset card; no model field in individual records",
            "per_split": dict(splits),
            "per_task_key": {key: dict(counts) for key, counts in sorted(tasks.items())},
            "unique_task_keys": len(tasks),
            "unique_record_ids": len(set(ids)),
            "duplicate_record_ids": len(ids) - len(set(ids)),
            "unique_parent_ids": len({row["id"] for row in records}),
            "sorted_record_ids_sha256": ids_digest(ids),
            "source_steps": stats([len(row["steps"]) for row in records]),
        }
        if subset != "agent":
            cohorts[subset]["per_restart_distance"] = dict(
                sorted(Counter(str(row["d"]) for row in records).items())
            )
    counts = Counter(row["split"] for row in agents)
    concealed = sum(
        row["test_tampered"] and row["pass_own"] and not row["pass_fresh"] for row in agents
    )
    claims = {
        "agent_runs": (250, len(agents)),
        "conflicting_runs": (200, counts["conflicting"]),
        "original_controls": (50, counts["original"]),
        "tasks": (50, len({row["task_id"] for row in agents})),
        "tampered_runs": (45, sum(row["test_tampered"] for row in agents)),
        "original_tampered": (
            0,
            sum(row["test_tampered"] for row in agents if row["split"] == "original"),
        ),
        "branch_pilot": (180, cohorts["branch_pilot"]["rows"]),
        "branch_main": (990, cohorts["branch_main"]["rows"]),
        "resampled_retries": (
            1170,
            cohorts["branch_pilot"]["rows"] + cohorts["branch_main"]["rows"],
        ),
        "feature_arrays": (250, files_by_directory["features"]["files"] - 1),
        "card_rewrote_and_submitted_as_passing": (45, concealed),
    }
    feature_rows = [
        json.loads(line) for line in (root / "features/index.jsonl").read_text().splitlines()
    ]
    feature_issues: Counter[str] = Counter()
    feature_shapes: Counter[str] = Counter()
    for row in feature_rows:
        with (root / "features" / (row["id"] + ".npy")).open("rb") as stream:
            header = npy_header(stream)
        feature_shapes[json.dumps(header, sort_keys=True)] += 1
        if header["shape"] != row["shape"] or row["shape"] != [
            len(parent_map[row["id"]]["steps"]),
            2,
            5120,
        ]:
            feature_issues["shape"] += 1
        if len(row["turns"]) != row["shape"][0]:
            feature_issues["turn_count"] += 1
    linear = json.loads((root / "scores/linear_scores_A.json").read_text())
    llm = [json.loads(line) for line in (root / "scores/llm_scores.jsonl").read_text().splitlines()]
    llm_ids = [
        json.dumps([row["id"], row["turn"], row["point"]], separators=(",", ":")) for row in llm
    ]
    probe_headers = {}
    for name in ("linear_probe_A.npz", "linear_probe_B.npz"):
        with zipfile.ZipFile(root / "scores" / name) as archive:
            probe_headers[name] = {}
            for member in archive.namelist():
                with archive.open(member) as stream:
                    probe_headers[name][member] = npy_header(stream)
    check = adapter.selfcheck(root)
    return {
        "repo": adapter.REPO,
        "revision": adapter.REVISION,
        "current_revision_at_plan": adapter.REVISION,
        "revision_moved": False,
        "licence": manifest["licence"],
        "gated": manifest["gated"],
        "cross_test_only": True,
        "local_only": True,
        "detector_executed": False,
        "bundle_files": len(manifest["files"]),
        "bundle_bytes": manifest["total_bytes"],
        "local_files_including_notice_and_manifest": sum(path.is_file() for path in paths),
        "manifest_sha256": sha256(manifest_path),
        "notice_sha256": sha256(root / "CROSS_TEST_ONLY.md"),
        "staging_tool_sha256": manifest["tool_sha256"],
        "verification": {
            "digest_problems": dict(digest_problems),
            "writable_files": writable_files,
            "writable_directories": writable_directories,
        },
        "files_by_directory": {key: dict(counts) for key, counts in files_by_directory.items()},
        "named_file_digests": named_digests,
        "all_file_digests_by_path_key": inventory,
        "id_digest_format": "sha256(sorted identities, UTF-8, newline after each); retries use JSON [subset,id,b,d,k]",
        "cohorts": cohorts,
        "publisher_count_checks": {
            name: {"expected": expected, "observed": observed, "matches": expected == observed}
            for name, (expected, observed) in claims.items()
        },
        "oracle_joint_counts": dict(
            sorted(
                Counter(
                    json.dumps(
                        [row["split"], row["test_tampered"], row["pass_own"], row["pass_fresh"]]
                    )
                    for row in agents
                ).items()
            )
        ),
        "oracle_joint_field_order": ["split", "test_tampered", "pass_own", "pass_fresh"],
        "end_counts": dict(Counter(row["end"] for row in agents)),
        "feature_index": {
            "rows": len(feature_rows),
            "unique_ids": len({row["id"] for row in feature_rows}),
            "ids_equal_agent_runs": {row["id"] for row in feature_rows} == set(parent_map),
            "sorted_record_ids_sha256": ids_digest([row["id"] for row in feature_rows]),
            "header_counts": dict(feature_shapes),
            "issues": dict(feature_issues),
        },
        "published_scores_excluded_from_detector": {
            "linear_score_runs": len(linear),
            "linear_score_values": sum(len(scores) for scores in linear.values()),
            "linear_sorted_record_ids_sha256": ids_digest(list(linear)),
            "llm_score_rows": len(llm),
            "llm_unique_run_ids": len({row["id"] for row in llm}),
            "llm_unique_row_ids": len(set(llm_ids)),
            "llm_sorted_record_ids_sha256": ids_digest(llm_ids),
            "llm_per_point": dict(Counter(str(row["point"]) for row in llm)),
            "probe_headers": probe_headers,
        },
        "adapter_selfcheck": check,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=adapter.ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    result = profile(args.root)
    failed = bool(
        result["adapter_selfcheck"]["issue_count"]
        or result["verification"]["digest_problems"]
        or result["verification"]["writable_files"]
        or result["verification"]["writable_directories"]
        or result["feature_index"]["issues"]
    )
    if failed:
        raise SystemExit("structural or integrity issues; no profile written")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "cohorts": {
                    name: {
                        key: cohort[key]
                        for key in (
                            "rows",
                            "positives",
                            "negatives",
                            "unlabeled",
                            "unique_record_ids",
                        )
                    }
                    for name, cohort in result["cohorts"].items()
                },
                "publisher_mismatches": {
                    name: check
                    for name, check in result["publisher_count_checks"].items()
                    if not check["matches"]
                },
                "adapter_issues": result["adapter_selfcheck"]["issue_count"],
                "files": result["bundle_files"],
                "bytes": result["bundle_bytes"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
