#!/usr/bin/env python3
"""Reverify a locked local bundle; emit only aggregates, digests and hashed ids."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from adapter import (
    CORPUS,
    FIELDS_DETECTOR_MUST_NOT_SEE,
    REPO,
    REVISION,
    ROOT,
    TASK_ID,
    jsonl_rows,
    parquet_rows,
    selfcheck,
)

OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"
MANIFEST_SHA256 = "a61f0bd278d36620cbf159b520d4a3899ab4efea1bbf2645bcea553e3b252405"
NOTICE_SHA256 = "4290c95fc4bc55db458b71a0a84dd8e676c0614da12b51de57a238abb97f5caf"
CODE_REVISION = "59a214d8b44512ba762a0cc18d93396969794bf3"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digests(path: Path, size: int, with_blob: bool) -> tuple[str, str | None]:
    sha256 = hashlib.sha256()
    blob = hashlib.sha1(b"blob %d\0" % size) if with_blob else None
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            sha256.update(block)
            if blob is not None:
                blob.update(block)
    return sha256.hexdigest(), blob.hexdigest() if blob is not None else None


def normalise_id(value: str, mode: str) -> str:
    if mode == "bare":
        return value.split("@")[0].rsplit("/", 1)[-1].rsplit("__", 1)[-1].casefold()
    if mode == "full":
        return value.casefold()
    raise ValueError("unknown id normalization")


def id_hashes(values: list[str]) -> dict[str, list[str]]:
    return {
        mode: sorted(digest(normalise_id(value, mode).encode("utf-8")) for value in values)
        for mode in ("bare", "full")
    }


def profile(root: Path = ROOT) -> dict[str, Any]:
    manifest_bytes = (root / "STAGE-MANIFEST.json").read_bytes()
    if digest(manifest_bytes) != MANIFEST_SHA256:
        raise ValueError("manifest differs from approved staging receipt")
    manifest = json.loads(manifest_bytes)
    if (manifest["repo"], manifest["revision"], manifest["licence"], manifest["gated"]) != (
        REPO, REVISION, "cc-by-4.0", False
    ):
        raise ValueError("bundle identity, licence or ungated state changed")
    if manifest["gate_terms"] or manifest["licence_override_reason"] is not None:
        raise ValueError("unexpected gate terms or licence override")
    if manifest["inspected_code"]["revision"] != CODE_REVISION:
        raise ValueError("inspected provenance-code pin changed")
    if digest((root / "CROSS_TEST_ONLY.md").read_bytes()) != NOTICE_SHA256:
        raise ValueError("local-only evaluation restrictions changed")
    paths = [root, *root.rglob("*")]
    if any(path.is_symlink() for path in paths):
        raise ValueError("symlink in immutable bundle")
    writable_paths = sum(bool(path.stat().st_mode & 0o222) for path in paths)
    if writable_paths:
        raise ValueError("bundle is not locked read-only")
    inventory = []
    files: set[Path] = set()
    publisher_checks: Counter[str] = Counter()
    kinds: dict[str, Counter[str]] = {}
    for index, entry in enumerate(manifest["files"], 1):
        relative = PurePosixPath(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe manifest path")
        if relative.parts[0] not in {"rollouts", "grades"} and entry["path"] != "README.md":
            raise ValueError("unexpected staged path outside run-only selection")
        path = root / relative
        if path in files or not path.is_file() or path.stat().st_size != entry["bytes"]:
            raise ValueError("duplicate, missing or wrong-sized manifest file")
        kind = entry["verified_against"]
        if kind not in {"hub_git_blob_sha1", "hub_lfs_sha256"}:
            raise ValueError("file lacks publisher-digest verification")
        sha256, git_blob = file_digests(path, entry["bytes"], kind == "hub_git_blob_sha1")
        if sha256 != entry["sha256"] or sha256 != entry["sha256_computed"]:
            raise ValueError("staged SHA-256 differs from frozen manifest")
        if git_blob is not None and git_blob != entry["blob_id"]:
            raise ValueError("publisher Git blob digest mismatch")
        inventory.append(
            {
                "file_index": index,
                "file_path_sha256": digest(entry["path"].encode()),
                "bytes": entry["bytes"],
                "sha256": sha256,
                **({"git_blob": git_blob} if git_blob else {"lfs_oid": "sha256:" + sha256}),
            }
        )
        files.add(path)
        publisher_checks[kind] += 1
        group = relative.parts[0] if len(relative.parts) > 1 else "publisher_card"
        kinds.setdefault(group, Counter()).update(files=1, bytes=entry["bytes"])
    filesystem_files = {path for path in paths if path.is_file()}
    if filesystem_files != files | {root / "STAGE-MANIFEST.json", root / "CROSS_TEST_ONLY.md"}:
        raise ValueError("unmanifested or missing bundle files")
    payload_bytes = sum(entry["bytes"] for entry in inventory)
    if len(files) != 13 or payload_bytes != manifest["total_bytes"] or payload_bytes != 43928919:
        raise ValueError("pinned run-only file count or byte count changed")
    if sum(item["files"] for item in manifest["excluded"]) != 85:
        raise ValueError("excluded file groups do not cover publisher inventory")
    excluded_bytes = sum(item["bytes"] for item in manifest["excluded"])
    if excluded_bytes != 228119216 or payload_bytes + excluded_bytes != 272048135:
        raise ValueError("excluded byte groups do not cover publisher inventory")
    if len(files) + 85 != manifest["source_inventory"]["files"]:
        raise ValueError("publisher file inventory changed")
    card = (root / "README.md").read_text(encoding="utf-8")
    if "\nlicense: cc-by-4.0\n" not in card or "precommit_hook" not in card:
        raise ValueError("pinned card lacks licence or single-task provenance")
    checks = selfcheck(root)
    if checks["issues"] or checks["label_fields_in_steps"] or checks["structured_verdict_text_in_steps"]:
        raise ValueError("adapter selfcheck failed")
    runs = parquet_rows(root, "runs")
    grades = jsonl_rows(root, "grades")
    auxiliary = jsonl_rows(root, "judge")
    verification = jsonl_rows(root, "verify")
    run_ids = [row["run_id"] for row in runs]
    run_id_set = set(run_ids)
    grade_ids = {row["run_id"] for row in grades}
    verify_ids = {row["run_id"] for row in verification}
    judge_ids = {row["run_id"] for row in auxiliary}
    source_counts = {
        "runs": len(runs),
        "authoritative_grades": len(grades),
        "conversations": checks["conversations"],
        "quarantined": sum(row["quarantined"] for row in runs),
        "missing_transcript": checks["excluded_by_reason_nonadditive"]["missing_transcript"],
        "run_files": kinds["rollouts"]["files"] + kinds["grades"]["files"],
        "run_bytes": kinds["rollouts"]["bytes"] + kinds["grades"]["bytes"],
        "publisher_files": manifest["source_inventory"]["files"],
        "publisher_bytes": manifest["source_inventory"]["bytes"],
    }
    claimed = {
        "runs": 1219, "authoritative_grades": 1215, "conversations": 1218,
        "quarantined": 4, "missing_transcript": 1, "run_files": 12,
        "run_bytes": 43920568, "publisher_files": 98, "publisher_bytes": 272048135,
    }
    differences = {key: source_counts[key] - value for key, value in claimed.items()}
    if any(differences.values()):
        raise ValueError("parent-supplied source counts differ from measured staging")
    hashes = {"task": id_hashes([TASK_ID]), "run": id_hashes(run_ids)}
    per_class_checker: dict[str, Counter[str]] = {}
    mechanisms: Counter[str] = Counter()
    for grade in grades:
        counter = per_class_checker.setdefault(grade["outcome"], Counter())
        verify = grade["verify"]
        counter.update(
            rows=1,
            committed=int(grade["committed"]),
            mypy_zero=int(verify["final_errors"] == 0),
            mypy_null=int(verify["final_errors"] is None),
            hook_rc_zero=int(verify["hook_rc"] == 0),
            hook_rc_nonzero=int(verify["hook_rc"] not in (0, None)),
            hook_rc_null=int(verify["hook_rc"] is None),
            workaround_nonempty=int(bool(grade["workarounds"])),
        )
        mechanisms.update(grade["workarounds"])
    return {
        "corpus": CORPUS,
        "revision": REVISION,
        "licence": "cc-by-4.0",
        "manifest_sha256": MANIFEST_SHA256,
        "notice_sha256": NOTICE_SHA256,
        "counts": {key: checks[key] for key in ("rows", "positives", "negatives", "excluded", "scoreable", "task_count")},
        "source_counts": source_counts,
        "parent_count_differences": differences,
        "negative_classes": checks["negative_classes"],
        "outcome_classes": checks["outcome_classes"],
        "excluded_by_reason_nonadditive": checks["excluded_by_reason_nonadditive"],
        "excluded_by_reason_combination": checks["excluded_by_reason_combination"],
        "per_model_arm": checks["per_model_arm"],
        "per_seeded_error_count": checks["per_seeded_error_count"],
        "per_step_budget": checks["per_step_budget"],
        "per_class_checker_state_counts": {key: dict(sorted(value.items())) for key, value in sorted(per_class_checker.items())},
        "mechanism_counts_nonadditive": dict(sorted(mechanisms.items())),
        "authoritative_grade_join": {
            "grade_rows": len(grades), "unique_grade_ids": len(grade_ids),
            "grade_ids_missing_runs": len(grade_ids - run_id_set),
            "runs_without_grade": len(run_id_set - grade_ids),
            "duplicated_run_column_differences": 0,
        },
        "auxiliary_judge": {
            "rows": len(auxiliary), "unique_ids": len(judge_ids),
            "ids_matching_runs": len(judge_ids & run_id_set),
            "ids_not_in_runs": len(judge_ids - run_id_set),
            "error_rows": sum("error" in row for row in auxiliary),
            "models": dict(sorted(Counter(row["judge_model"] for row in auxiliary).items())),
            "independent_gold_rows": 0,
        },
        "verification_history": {
            "rows": len(verification), "unique_ids": len(verify_ids),
            "duplicate_id_rows": len(verification) - len(verify_ids),
            "ids_matching_runs": len(verify_ids & run_id_set),
            "ids_not_in_runs": len(verify_ids - run_id_set),
            "runs_without_verification": len(run_id_set - verify_ids),
        },
        "task_id_hashes": hashes["task"],
        "run_id_hashes": hashes["run"],
        "id_uniqueness": {
            kind: {
                mode: {"hashed_rows": len(values), "unique_hashes": len(set(values)), "duplicate_hashes": len(values) - len(set(values))}
                for mode, values in modes.items()
            }
            for kind, modes in hashes.items()
        },
        "sorted_raw_run_ids_sha256": digest("\n".join(sorted(run_ids)).encode("utf-8")),
        "source_file_digests": inventory,
        "bundle_integrity": {
            "manifest_files": len(files), "manifest_payload_bytes": payload_bytes,
            "publisher_verified_files": dict(sorted(publisher_checks.items())),
            "by_kind": {key: dict(value) for key, value in sorted(kinds.items())},
            "excluded_files": 85, "excluded_bytes": excluded_bytes,
            "excluded_groups": len(manifest["excluded"]),
            "filesystem_files_including_receipts": len(filesystem_files),
            "filesystem_bytes_including_receipts": sum(path.stat().st_size for path in filesystem_files),
            "problems": 0,
        },
        "verified_after_lock": True,
        "writable_paths": writable_paths,
        "fields_detector_must_not_see_count": len(FIELDS_DETECTOR_MUST_NOT_SEE),
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
    print(json.dumps({key: result[key] for key in ("counts", "bundle_integrity", "verified_after_lock", "writable_paths")}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
