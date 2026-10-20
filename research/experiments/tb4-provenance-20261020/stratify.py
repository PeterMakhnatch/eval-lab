"""Stratify frozen existing labels without relabelling, guessed identities or drops.

Canonical native lock identity, upstream static bytes, and reported outcomes are
separate axes. None of these outputs is a runtime-integrity adjudication.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import acquire

from evallab import hub_trials as hub

JOIN_SCHEMA = "evallab.tb4_provenance_join/v1"
FINGERPRINT_SCHEMA = "evallab.tb4_fingerprints/v1"


def frozen_labels(spec_path: Path, repo: Path) -> tuple[dict, list[dict], list[dict]]:
    spec, spec_receipt = acquire.read_receipted(spec_path)
    if spec.get("schema") != "evallab.tb4_provenance_experiment/v1":
        raise hub.HubError("Unsupported frozen experiment specification")
    labels, evidence, census = [], [spec_receipt], None
    for item in spec["label_inputs"]:
        path = hub._inside(repo, item["path"])
        raw, actual = acquire.read_bytes_receipted(path)
        if actual["sha256"] != item["sha256"] or actual["bytes"] != item["bytes"]:
            raise hub.IntegrityError("Frozen existing-label input changed")
        evidence.append(actual)
        if path.suffix == ".jsonl":
            labels.extend(json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip())
        elif path.name == "census.json":
            census = json.loads(raw)
    if census is None or len(labels) != census["trials"]:
        raise hub.HubError("Frozen label count differs from the published census")
    if sorted({label["task"] for label in labels}) != sorted(spec["primary_tasks"]) or sorted(census["tasks"]) != sorted(spec["primary_tasks"]):
        raise hub.HubError("Frozen primary task boundary changed")
    return spec, labels, evidence


def fingerprint_index(payload: dict | None) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = defaultdict(list)
    if payload is None:
        return result
    if payload.get("schema") != FINGERPRINT_SCHEMA:
        raise hub.HubError("Unsupported package fingerprint schema")
    for entry in payload["packages"]:
        expected = acquire.package_key(entry["source"], entry["digest"], entry["task_name"])
        if entry["key"] != expected:
            raise hub.HubError("Fingerprint key does not bind the exact recorded lock")
        result[entry["key"]].append(entry)
    return result


def static_stratum(trial: dict, fingerprints: dict[str, list[dict]]) -> dict[str, Any]:
    key = trial.get("package_key")
    if key is None:
        return {"status": "unmeasured_no_task_lock", "package_key": None}
    expected = acquire.package_key(trial["lock_task"]["source"], trial["lock_task"]["digest"], trial["task_name"])
    if key != expected:
        raise hub.HubError("Native package key does not bind its recorded lock")
    matches = fingerprints.get(key, [])
    if len(matches) != 1:
        return {"status": "unmeasured_missing_fingerprint" if not matches else "ambiguous_fingerprint", "package_key": key, "candidate_count": len(matches)}
    entry = matches[0]
    binding = entry.get("binding_status")
    coverage = {}
    for category in ("instruction", "tests", "environment", "config", "reference", "support"):
        fact = (entry.get("content_hash_coverage") or {}).get(category, {})
        covered = fact.get("full_compared_content_paths_digest_covered")
        coverage[category] = {
            "current_export_equal": entry.get(f"{category}_equal"),
            "all_exported_content_paths_hashed": fact.get("all_exported_content_paths_hashed"),
            "full_compared_content_paths_digest_covered": covered,
            "historically_digest_covered_equal": entry.get(f"{category}_equal") if covered is True and binding == "bound" and (entry.get("native_hash_scope") or {}).get("status") == "measured" else None,
            **{f"{name}_count": len(fact[name]) if isinstance(fact.get(name), list) else None for name in ("native_hash_included_paths", "exported_content_paths_not_hashed", "unmeasured_exported_content_paths", "upstream_content_paths_missing_from_export")},
            "path_inventory_field": f"content_hash_coverage.{category}",
        }
    historical = [coverage[category]["historically_digest_covered_equal"] for category in ("instruction", "tests")]
    hash_scope = entry.get("native_hash_scope") or {}
    result = {
        "package_key": key, "binding_status": binding, "fingerprint_status": entry.get("status"),
        **{name: entry.get(name) for name in ("binding_scope", "actual_export_source", "actual_export_ref", "actual_export_receipt", "declared_source_binding", "recorded_source_matches_actual_export_source", "canonical_dataset_eligible")},
        **{name: entry.get(name) for name in ("instruction_equal", "tests_equal", "environment_equal", "config_equal", "reference_equal", "support_equal")},
        "comparison": entry.get("comparison"),
        "download": {name: (entry.get("download") or {}).get(name) for name in ("pin", "computed_native_digest", "receipt_path")},
        "content_hash_coverage": coverage,
        "native_hash_scope": {
            "status": hash_scope.get("status", "unmeasured"),
            "mode_and_symlink_metadata_hashed": hash_scope.get("mode_and_symlink_metadata_hashed"),
            **{f"{name}_count": len(hash_scope[name]) if isinstance(hash_scope.get(name), list) else None for name in ("included_paths", "not_hashed_paths")},
            "path_inventory_field": "native_hash_scope",
        },
        "historical_instruction_tests_content_comparison": "unmeasured" if any(value is None for value in historical) else ("equal" if all(historical) else "different"),
        "historical_full_inventory_binding": entry.get("historical_full_inventory_binding", "unmeasured"),
        "runtime_equivalence": "not_established",
    }
    # Only the package worker's affirmative, recomputed exact-digest binding can
    # qualify byte equality. Alias/declaration matches cannot do so.
    if binding != "bound":
        result["status"] = "unmeasured_unbound_package"
    elif entry.get("status") != "measured":
        result["status"] = "unmeasured_upstream_comparison"
    elif entry.get("instruction_equal") is None or entry.get("tests_equal") is None:
        result["status"] = "unmeasured_instruction_or_tests"
    elif entry["instruction_equal"] is True and entry["tests_equal"] is True:
        result["status"] = "bound_instruction_and_tests_equal"
    else:
        result["status"] = "bound_instruction_or_tests_changed"
    return result


def outcome_side(reward: Any) -> str:
    return "unscored" if reward is None else ("pass" if reward >= 1 else "fail")


def join_labels(labels: list[dict], trials: list[dict], fingerprints: dict[str, list[dict]]) -> list[dict]:
    by_hub: dict[str, list[dict]] = defaultdict(list)
    by_archive: dict[str, list[dict]] = defaultdict(list)
    duplicate_cells = Counter((label["row_id"], label["trial_id"]) for label in labels)
    for trial in trials:
        by_hub[trial["hub_trial_id"]].append(trial)
        archive = trial.get("archive_link") or {}
        if archive.get("status") == "inventory_and_native_detail_linked" and archive.get("archive_result_id"):
            by_archive[archive["archive_result_id"]].append(trial)
    joined = []
    for ordinal, label in enumerate(labels):
        identity = label["trial_id"]
        # Do not fall back to names, task-name similarity, suffixes or ambient jobs.
        matches = by_hub.get(identity, [])
        basis = "exact_hub_trial_id"
        if not matches:
            matches = by_archive.get(identity, [])
            basis = "explicit_archive_result_id"
        record: dict[str, Any] = {"label_ordinal": ordinal, "existing_label": label, "join_basis": basis, "candidate_hub_trial_ids": [trial["hub_trial_id"] for trial in matches], "issues": []}
        if duplicate_cells[(label["row_id"], identity)] > 1:
            record["issues"].append("duplicate_existing_label_cell")
        if not matches:
            record["join_status"] = "unmatched"
        elif len(matches) > 1:
            record["join_status"] = "ambiguous"
        else:
            trial = matches[0]
            if label["row_id"] not in trial["row_ids"]:
                record["issues"].append("row_membership_mismatch")
            if trial["task_name"] != f"terminal-bench/{label['task']}":
                record["issues"].append("task_identity_mismatch")
            if label["reward"] != trial["native_reward"]:
                record["issues"].append("reported_reward_mismatch")
            if label["side"] != outcome_side(label["reward"]):
                record["issues"].append("existing_side_reward_mismatch")
            record["issues"].extend(trial.get("issues", []))
            record.update({
                "join_status": "conflict" if record["issues"] else "matched",
                "hub_trial_id": trial["hub_trial_id"], "job_id": trial["job_id"],
                "native_lock_stratum": trial["provenance_status"], "static_bytes": static_stratum(trial, fingerprints),
                "native_status": trial["native_status"], "native_reward": trial["native_reward"],
                "reported_outcome_measured": trial["reported_outcome_measured"],
                "exact_source_eligible": trial.get("exact_source_eligible") is True,
                "job_dataset_matches": trial.get("job_dataset_matches"),
                "native_error_type": trial.get("native_error_type"), "detail_status": trial["detail_status"],
                "archive_link": trial.get("archive_link"), "evidence": trial["evidence"],
            })
        if "static_bytes" not in record:
            record["native_lock_stratum"] = "unmeasured_join_failure"
            record["static_bytes"] = {"status": "unmeasured_join_failure"}
            record["reported_outcome_measured"] = False
        record["original_archive_binding"] = (record.get("archive_link") or {}).get("status", "unmeasured")
        record["qualified_canonical_static_label"] = (
            record["join_status"] == "matched" and record["native_lock_stratum"] == "canonical_exact_lock"
            and record["static_bytes"]["status"] == "bound_instruction_and_tests_equal"
            and record["reported_outcome_measured"] and record.get("exact_source_eligible") is True
            and record["original_archive_binding"] == "inventory_and_native_detail_linked"
        )
        record["qualified_canonical_hash_covered_label"] = (
            record["qualified_canonical_static_label"]
            and record["static_bytes"].get("historical_instruction_tests_content_comparison") == "equal"
        )
        joined.append(record)
    return joined


def label_counts(entries: list[dict]) -> dict[str, Any]:
    labels = [entry["existing_label"] for entry in entries]
    measured = [entry for entry in entries if entry["reported_outcome_measured"] and entry["join_status"] == "matched"]
    qualified = [entry for entry in entries if entry["qualified_canonical_static_label"]]
    hash_covered = [entry for entry in entries if entry["qualified_canonical_hash_covered_label"]]
    return {
        "label_cells": len(entries), "distinct_label_trial_ids": len({label["trial_id"] for label in labels}),
        "sides": dict(Counter(label["side"] for label in labels)), "labels": dict(Counter(label["label"] for label in labels)),
        "join_statuses": dict(Counter(entry["join_status"] for entry in entries)),
        "original_archive_bindings": dict(Counter(entry["original_archive_binding"] for entry in entries)),
        "native_lock_strata": dict(Counter(entry["native_lock_stratum"] for entry in entries)),
        "static_byte_strata": dict(Counter(entry["static_bytes"]["status"] for entry in entries)),
        "static_binding_scopes": dict(Counter(entry["static_bytes"].get("binding_scope") or "unreported" for entry in entries)),
        "declared_source_bindings": dict(Counter(entry["static_bytes"].get("declared_source_binding") or "unreported" for entry in entries)),
        "reported_outcome_measured_cells": len(measured),
        "reported_outcome_measured_labels": dict(Counter(entry["existing_label"]["label"] for entry in measured)),
        "qualified_canonical_static_cells": len(qualified),
        "qualified_canonical_static_labels": dict(Counter(entry["existing_label"]["label"] for entry in qualified)),
        "qualified_canonical_hash_covered_cells": len(hash_covered),
        "qualified_canonical_hash_covered_labels": dict(Counter(entry["existing_label"]["label"] for entry in hash_covered)),
    }


def grouped_labels(entries: list[dict], key: Any) -> dict[str, Any]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        groups[key(entry)].append(entry)
    return {name: label_counts(group) for name, group in sorted(groups.items())}


def board_counts(trials: list[dict], fingerprints: dict[str, list[dict]]) -> dict[str, Any]:
    return {
        "distinct_trials": len(trials), "row_trial_memberships": sum(len(trial["row_ids"]) for trial in trials),
        "native_statuses": dict(Counter(trial["native_status"] or "unavailable" for trial in trials)),
        "native_lock_strata": dict(Counter(trial["provenance_status"] for trial in trials)),
        "native_lock_statuses": dict(Counter(trial.get("native_lock_status", "unreported") for trial in trials)),
        "static_byte_strata": dict(Counter(static_stratum(trial, fingerprints)["status"] for trial in trials)),
        "declared_source_bindings": dict(Counter(static_stratum(trial, fingerprints).get("declared_source_binding") or "unreported" for trial in trials)),
        "reported_measured_outcomes": dict(Counter(outcome_side(trial["native_reward"]) for trial in trials if trial["reported_outcome_measured"])),
        "exact_source_eligible_trials": sum(trial.get("exact_source_eligible") is True for trial in trials),
        "nonmeasured_outcome_ids": [trial["hub_trial_id"] for trial in trials if not trial["reported_outcome_measured"]],
        "missing_detail_ids": [trial["hub_trial_id"] for trial in trials if trial["detail_status"] != "complete"],
        "missing_task_lock_ids": [trial["hub_trial_id"] for trial in trials if trial.get("package_key") is None],
    }


def stratify(manifest: dict, labels: list[dict], fingerprints: dict | None) -> tuple[dict, dict]:
    if manifest.get("schema") != acquire.INPUT_SCHEMA:
        raise hub.HubError("Unsupported provenance input manifest schema")
    index = fingerprint_index(fingerprints)
    trials = manifest["trials"]
    joined = join_labels(labels, trials, index)
    matched_cells = {(entry["existing_label"]["row_id"], entry.get("hub_trial_id")) for entry in joined if entry["join_status"] == "matched"}
    tasks = {f"terminal-bench/{label['task']}" for label in labels}
    missing_cells = [
        {"row_id": row_id, "hub_trial_id": trial["hub_trial_id"], "task_name": trial["task_name"]}
        for trial in trials if trial["task_name"] in tasks for row_id in trial["row_ids"]
        if (row_id, trial["hub_trial_id"]) not in matched_cells
    ]
    label_ordinals: dict[str, list[int]] = defaultdict(list)
    for entry in joined:
        if entry.get("hub_trial_id") is not None:
            label_ordinals[entry["hub_trial_id"]].append(entry["label_ordinal"])
    primary = {
        "schema": JOIN_SCHEMA, "scope": "existing_primary_labels", "summary": label_counts(joined),
        "by_native_and_static": grouped_labels(joined, lambda entry: f"{entry['native_lock_stratum']}|{entry['static_bytes']['status']}"),
        "by_task": grouped_labels(joined, lambda entry: entry["existing_label"]["task"]),
        "by_row": grouped_labels(joined, lambda entry: entry["existing_label"]["row_id"]),
        "by_task_native_and_static": grouped_labels(joined, lambda entry: f"{entry['existing_label']['task']}|{entry['native_lock_stratum']}|{entry['static_bytes']['status']}"),
        "unmatched_labels": [entry["label_ordinal"] for entry in joined if entry["join_status"] == "unmatched"],
        "ambiguous_labels": [entry["label_ordinal"] for entry in joined if entry["join_status"] == "ambiguous"],
        "conflicting_labels": [entry["label_ordinal"] for entry in joined if entry["join_status"] == "conflict"],
        "primary_board_cells_without_matched_label": missing_cells, "entries": joined,
    }
    board = {
        "schema": JOIN_SCHEMA, "scope": "frozen_full_board", "summary": board_counts(trials, index),
        "by_row": {row["id"]: board_counts([trial for trial in trials if row["id"] in trial["row_ids"]], index) for row in manifest["rows"]},
        "by_task": {task or "__unmeasured_task__": board_counts([trial for trial in trials if trial["task_name"] == task], index) for task in sorted({trial["task_name"] for trial in trials}, key=lambda task: task or "")},
        "entries": [{"hub_trial_id": trial["hub_trial_id"], "row_ids": trial["row_ids"], "task_name": trial["task_name"], "native_lock_stratum": trial["provenance_status"], "static_bytes": static_stratum(trial, index), "native_status": trial["native_status"], "native_reward": trial["native_reward"], "reported_outcome_measured": trial["reported_outcome_measured"], "existing_label_ordinals": label_ordinals[trial["hub_trial_id"]]} for trial in trials],
        "unassociated_job_trials_excluded": manifest["coverage"]["unassociated_job_trials_excluded"],
    }
    return primary, board


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--fingerprints", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--spec", type=Path, default=Path(__file__).with_name("spec.json"))
    args = parser.parse_args()
    try:
        spec, labels, evidence = frozen_labels(args.spec, Path(__file__).resolve().parents[3])
        manifest_path = args.native / "input-manifest.json"
        manifest, manifest_receipt = acquire.read_receipted(manifest_path)
        if manifest["dataset"]["id"] != spec["canonical_dataset_id"] or manifest["dataset"]["digest"] != spec["canonical_dataset_digest"] or manifest["board"]["ref"] != spec["canonical_board"]:
            raise hub.HubError("Input census differs from frozen experiment pins")
        fingerprints = None
        if args.fingerprints is not None and args.fingerprints.exists():
            fingerprints, fingerprint_receipt = acquire.read_receipted(args.fingerprints)
            evidence.append(fingerprint_receipt)
        evidence.append(manifest_receipt)
        primary, board = stratify(manifest, labels, fingerprints)
        limits = {
            "source_inputs": evidence, "classification_limits": spec["classification_limits"],
            "fingerprints_available": fingerprints is not None, "runtime_integrity": "not_adjudicated",
            "qualification_scopes": {
                "qualified_canonical_static": "Original archive linkage, exact canonical native lock and measured reported outcome plus current exported instruction/tests content equality.",
                "qualified_canonical_hash_covered": "Additionally all compared instruction/tests content paths are covered by the recorded native Packager digest.",
                "historical_full_inventory_and_executed_runtime": "Not established: native hashes exclude some content and all mode/link metadata; static equality is not executed-runtime attestation.",
                "path_inventories": "Resolve each static package_key and path_inventory_field in the receipted fingerprints source input.",
            },
        }
        primary.update(limits)
        board.update(limits)
        args.output.mkdir(parents=True, exist_ok=True)
        primary_receipt = acquire.publish(args.output, "primary.json", primary)
        board_receipt = acquire.publish(args.output, "board.json", board)
        summary = {"schema": JOIN_SCHEMA, "primary": primary["summary"], "board": board["summary"], **limits, "outputs": {"primary": primary_receipt, "board": board_receipt}}
        acquire.publish(args.output, "summary.json", summary)
    except (hub.HubError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, hub.HubError) else type(exc).__name__}))
        return 2
    print(json.dumps({"primary": primary["summary"], "board": board["summary"], "outputs": {"primary": primary_receipt, "board": board_receipt}}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
