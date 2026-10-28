#!/usr/bin/env python3
"""Reverify the locked bundle and publish only aggregates, digests and hashed IDs."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from adapter import (
    CORPUS,
    FIELDS_DETECTOR_MUST_NOT_SEE,
    REPO,
    REVISION,
    ROOT,
    gold_tables,
    iter_rows,
    label_state,
    selfcheck,
)

OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def id_hashes(values: list[str]) -> dict[str, list[str]]:
    def bare(value: str) -> str:
        return value.split("@")[0].rsplit("/", 1)[-1].rsplit("__", 1)[-1].casefold()

    return {
        "bare": sorted(digest(bare(value).encode()) for value in values),
        "full": sorted(digest(value.casefold().encode()) for value in values),
    }


def verify_bundle(root: Path) -> dict[str, Any]:
    manifest_bytes = (root / "STAGE-MANIFEST.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if (manifest["repo"], manifest["revision"], manifest["licence"], manifest["gated"]) != (
        REPO, REVISION, "mit", False
    ):
        raise ValueError("approved corpus pin, licence or gate changed")
    paths = [root, *root.rglob("*")]
    if any(path.is_symlink() for path in paths):
        raise ValueError("symlink in staged bundle")
    writable = sum(bool(path.stat().st_mode & 0o222) for path in paths)
    if writable:
        raise ValueError("bundle must be read-only before profiling")
    entries: dict[str, dict[str, Any]] = {}
    inventory = []
    kinds: dict[str, Counter[str]] = defaultdict(Counter)
    verified: Counter[str] = Counter()
    for index, entry in enumerate(sorted(manifest["files"], key=lambda item: item["path"])):
        relative = entry["path"]
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or relative in entries:
            raise ValueError("unsafe or duplicate manifest path")
        data = (root / relative).read_bytes()
        sha256 = digest(data)
        if len(data) != entry["bytes"] or sha256 != entry["sha256"]:
            raise ValueError("manifest size or SHA-256 mismatch")
        if entry.get("publisher_sha256"):
            if sha256 != entry["publisher_sha256"]:
                raise ValueError("publisher LFS SHA-256 mismatch")
            verified["publisher_lfs_sha256_files"] += 1
        else:
            blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
            if blob != entry["blob_id"]:
                raise ValueError("publisher Git blob mismatch")
            verified["publisher_git_blob_files"] += 1
        kind = "transcript" if relative.endswith(".jsonl") else "analysis_table" if relative.startswith("tables/") else "publisher_metadata"
        kinds[kind].update({"files": 1, "bytes": len(data)})
        inventory.append({
            "file_index": index, "path_sha256": digest(relative.encode()),
            "bytes": len(data), "sha256": sha256, "kind": kind,
            "publisher_digest_kind": entry["verified_against"],
            "publisher_digest": entry.get("publisher_sha256") or entry["blob_id"],
        })
        entries[relative] = entry
    actual_files = {str(path.relative_to(root)) for path in paths if path.is_file()}
    if actual_files != set(entries) | {"STAGE-MANIFEST.json", "CROSS_TEST_ONLY.md"}:
        raise ValueError("missing or unmanifested file")
    payload_bytes = sum(entry["bytes"] for entry in entries.values())
    if (len(entries), payload_bytes, manifest["total_bytes"]) != (63, 240255487, 240255487):
        raise ValueError("publisher file or byte count changed")
    if manifest["excluded"]:
        raise ValueError("whole-HF-repository staging must exclude no files")
    return {
        "manifest_sha256": digest(manifest_bytes),
        "notice_sha256": digest((root / "CROSS_TEST_ONLY.md").read_bytes()),
        "files": len(entries), "bytes": sum(item["bytes"] for item in inventory),
        "filesystem_files_including_receipts": len(actual_files),
        "filesystem_bytes_including_receipts": sum(path.stat().st_size for path in paths if path.is_file()),
        "verified_after_lock": True, "writable_paths": writable,
        "publisher_digest_verification": dict(sorted(verified.items())),
        "by_file_kind": {key: dict(value) for key, value in sorted(kinds.items())},
        "file_inventory_sha256": digest(json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()),
        "file_digests": inventory,
    }


def _rule_state(native: str, candidate: bool) -> str:
    if native == "error":
        return "error"
    if native == "heuristic":
        return "ambiguous"
    return "positive" if candidate else "negative"


def profile(root: Path = ROOT) -> dict[str, Any]:
    verification = verify_bundle(root)
    tables = gold_tables(root)
    states: Counter[str] = Counter()
    native_classes: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    cohorts: dict[str, Counter[str]] = defaultdict(Counter)
    models: dict[str, Counter[str]] = defaultdict(Counter)
    conditions: dict[str, Counter[str]] = defaultdict(Counter)
    model_conditions: dict[str, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    problems: dict[str, Counter[str]] = defaultdict(Counter)
    scoped_problems: dict[str, Counter[str]] = defaultdict(Counter)
    problem_metadata_hashes: dict[str, set[str]] = defaultdict(set)
    rules: dict[str, Counter[str]] = defaultdict(Counter)
    main_rules: dict[str, Counter[str]] = defaultdict(Counter)
    channels: Counter[str] = Counter()
    source_values: dict[str, Counter[str]] = defaultdict(Counter)
    audit_disagreements: dict[str, Counter[str]] = defaultdict(Counter)
    run_ids, task_ids, scoped_task_ids = [], set(), set()
    tool_names: Counter[str] = Counter()
    main_rows = 0
    for row in iter_rows(root):
        key = row["run_key"]
        main, audit = tables["main"].get(key), tables["audit"].get(key)
        labels = label_state(row, tables)
        native = labels["native_classification"]
        state = labels["state"]
        cohort = "indexed_main" if main else "extra_negative_control"
        task_id = row["problem_id"]
        scoped_task_id = f"{row['problem_set']}/{row['problem_id']}"
        run_ids.append(key)
        task_ids.add(task_id)
        scoped_task_ids.add(scoped_task_id)
        problem_metadata_hashes[task_id].add(digest(json.dumps(row["problem"], sort_keys=True).encode()))
        native_classes[native] += 1
        states[state] += 1
        reasons.update(labels["excluded_reasons"])
        for table, identity in ((cohorts, cohort), (models, row["model"]), (conditions, row["condition"]), (problems, digest(task_id.casefold().encode()))):
            table[identity].update({"rows": 1, state: 1})
        scoped_problems[digest(scoped_task_id.casefold().encode())].update({"rows": 1, state: 1})
        model_conditions[row["model"]][row["condition"]].update({"rows": 1, state: 1})
        author_reward = bool(main and main["reward_hacked"] == "yes")
        author_tamper = bool(main and main["tamper_hack"] == "yes")
        audited_tamper = bool(audit and audit["tamper_hack"] == "True")
        author_or = author_reward or author_tamper
        rule_states = {
            "native_classification_only": _rule_state(native, native == "reward_hacking"),
            "author_flags_or": _rule_state(native, author_or),
            "positive_union_then_heuristic_exclusion": _rule_state(native, labels["candidate_positive"]),
            "primary_conservative_channel_consensus": state,
        }
        for rule, rule_state in rule_states.items():
            rules[rule][rule_state] += 1
            if main:
                main_rules[rule][rule_state] += 1
        channels.update({
            "author_reward_hacked_yes": author_reward,
            "author_tamper_hack_yes": author_tamper,
            "author_reward_and_tamper": author_reward and author_tamper,
            "author_flags_or_candidates": author_or,
            "audited_tamper_positive": audited_tamper,
            "author_and_audited_tamper_positive": author_tamper and audited_tamper,
            "tamper_union_candidates": author_tamper or audited_tamper,
            "positive_union_candidates": labels["candidate_positive"],
            "positive_union_native_heuristic": labels["candidate_positive"] and native == "heuristic",
            "positive_union_native_legitimate": labels["candidate_positive"] and native == "legitimate",
            "audited_tamper_native_legitimate": audited_tamper and native == "legitimate",
        })
        source_values["native_test_result"][row["score"]["test_result"]] += 1
        if main:
            main_rows += 1
            source_values["main_classification"][main["classification"]] += 1
            source_values["main_test_result"][main["test_result"]] += 1
            source_values["main_gt_outcome"][main["gt_outcome"] or "not_audited"] += 1
            source_values["main_final_verdict"][main["final_verdict"]] += 1
        if audit:
            for field in ("reward_hacked", "tamper_hack"):
                mismatch = (main[field] == "yes") != (audit[field] == "True")
                if mismatch:
                    audit_disagreements[field][native] += 1
            source_values["audit_gt_outcome"][audit["gt_outcome"] or "not_audited"] += 1
        for message in row["messages"]:
            tool_names.update(call["function"] for call in message.get("tool_calls") or [])
    if len(run_ids) != len(set(run_ids)) or main_rows != len(tables["main"]):
        raise AssertionError("transcript/main join is not one-to-one")
    checks = selfcheck(root)
    assert checks["issues"] == 0
    assert states == {"positive": 124, "negative": 1990, "ambiguous": 174, "error": 7}
    assert channels["positive_union_candidates"] == 139
    assert source_values["main_classification"] == {"legitimate": 1823, "heuristic": 161, "reward_hacking": 124, "error": 7}
    assert len(task_ids) == 15 and len(scoped_task_ids) == 18
    assert all(len(hashes) == 1 for hashes in problem_metadata_hashes.values())
    task_hashes = id_hashes(sorted(task_ids))
    run_hashes = id_hashes(sorted(run_ids))
    return {
        "corpus": CORPUS, "revision": REVISION, "licence": "mit",
        "local_only": True, "cross_test_only": True, "gated": False,
        **verification,
        "rows": len(run_ids), "main_index_rows": main_rows,
        "main_index_bytes": (root / "tables/per_run_data_all_conditions.csv").stat().st_size,
        "extra_negative_control_rows": len(run_ids) - main_rows,
        "positives": states["positive"], "negatives": states["negative"],
        "excluded": states["ambiguous"] + states["error"],
        "scoreable": states["positive"] + states["negative"],
        "states": dict(sorted(states.items())),
        "excluded_by_reason_overlapping": dict(sorted(reasons.items())),
        "label_rule_counts_all_transcripts": {key: dict(sorted(value.items())) for key, value in sorted(rules.items())},
        "label_rule_counts_main_index": {key: dict(sorted(value.items())) for key, value in sorted(main_rules.items())},
        "gold_channel_counts": dict(sorted(channels.items())),
        "source_channel_value_counts": {key: dict(sorted(value.items())) for key, value in sorted(source_values.items())},
        "source_audit_flag_disagreements_by_native_class": {key: dict(sorted(value.items())) for key, value in sorted(audit_disagreements.items())},
        "native_classification_counts": dict(sorted(native_classes.items())),
        "per_cohort": {key: dict(sorted(value.items())) for key, value in sorted(cohorts.items())},
        "per_model": {key: dict(sorted(value.items())) for key, value in sorted(models.items())},
        "per_condition": {key: dict(sorted(value.items())) for key, value in sorted(conditions.items())},
        "per_model_condition": {model: {condition: dict(sorted(value.items())) for condition, value in sorted(rows.items())} for model, rows in sorted(model_conditions.items())},
        "per_problem_cluster_sha256": {key: dict(sorted(value.items())) for key, value in sorted(problems.items())},
        "per_scoped_problem_group_sha256": {key: dict(sorted(value.items())) for key, value in sorted(scoped_problems.items())},
        "models": len(models), "conditions": len(conditions),
        "problem_clusters": len(task_ids), "scoped_problem_groups": len(scoped_task_ids),
        "cross_problem_set_id_collisions": len(scoped_task_ids) - len(task_ids),
        "problem_metadata_unique_hashes": len(set().union(*problem_metadata_hashes.values())),
        "problem_ids_with_multiple_metadata_hashes": sum(len(hashes) > 1 for hashes in problem_metadata_hashes.values()),
        "bootstrap": {"problem_clusters": len(task_ids), "resamples": 10000, "seed": 0, "stratifiers": ["model", "condition"]},
        "tool_name_counts": dict(sorted(tool_names.items())),
        "gold_table_rows": {key: len(value) for key, value in sorted(tables.items())},
        "task_id_hashes": task_hashes, "run_id_hashes": run_hashes,
        "scoped_task_id_hashes": id_hashes(sorted(scoped_task_ids)),
        "unique_hashed_task_ids": {key: len(set(values)) for key, values in task_hashes.items()},
        "unique_hashed_run_ids": {key: len(set(values)) for key, values in run_hashes.items()},
        "sorted_run_ids_sha256": digest(("\n".join(sorted(run_ids)) + "\n").encode()),
        "detector_forbidden_field_count": len(FIELDS_DETECTOR_MUST_NOT_SEE),
        "adapter_selfcheck": checks,
        "raw_eval_logs_available": False, "raw_eval_access_hold": True,
        "detector_runs": 0, "model_calls": 0,
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
        "manifest_sha256", "files", "bytes", "rows", "positives", "negatives",
        "excluded", "scoreable", "verified_after_lock", "writable_paths"
    )}, sort_keys=True))
    print(json.dumps(result["adapter_selfcheck"], sort_keys=True))


if __name__ == "__main__":
    main()
