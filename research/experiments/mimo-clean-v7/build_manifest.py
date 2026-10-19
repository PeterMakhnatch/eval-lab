"""Project committed evidence into the metadata-only mimo-clean-v7 catalog.

No native execution, registry mutation, network access or payload export. The
private source checkout is required only to reproduce the catalog, not to read it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

PUBLIC_REVISION = "343342ec4746573a9a2a55c472cf374a2f5393b7"
EVIDENCE_REVISION = "ad8df9689b2357cc5a7ce04d94d5ff106eb88e05"
CREATED_AT = "2026-10-19T05:31:04Z"
BRIDGE = "bridge/2026-10-17-v7/"
PRIVATE_INPUTS = {
    "heldout": "heldout-v7.json",
    "heldout_adoption": "mimo-data-heldout-v7-release-adoption-20261018.json",
    "heldout_verifier": "mimo-data-heldout-v7-independent-v7-053-proof-20261018.json",
    "batch2": "mimo-data-train-v7-batch2-controls-20261018.json",
    "batch2_inherited_scope": "mimo-data-phase2-controls-v7.json",
    "baseline40": "mimo-data-train40-baseline-controls-20261018.json",
    "snapshot34": "mimo-data-train40-setup-runtime-controls-20261018-c.json",
    "later37": "mimo-data-train40-manual-reference-controls-20261018.json",
    "train_repairs": "repairs-train-v7.json",
    "expanded_repairs": "envcheck-atlas-v2_1-intake-v7.json",
}
PUBLIC_INPUTS = {
    "frozen_heldout": "research/experiments/mimo-training-20261016/heldout.json",
    "frozen_split": "research/experiments/mimo-training-20261016/split.json",
    "atlas_items": "research/answer-keys/mimo-weak-tests-v2.1/items.jsonl",
    "atlas_summary": "research/answer-keys/mimo-weak-tests-v2.1/summary.json",
    "atlas_notice": "research/answer-keys/mimo-weak-tests-v2.1/NOTICE",
    "atlas_license": "research/answer-keys/mimo-weak-tests-v2.1/LICENSE",
    "atlas_agent_license": "research/answer-keys/mimo-weak-tests-v2.1/LICENSE-mimoagent.md",
    "qa6": "research/experiments/qa-trace/results/qa-trace-6-mimo-v7-canonical-complete.json",
    "qa7": "research/experiments/qa-trace/results/qa-trace-7-mimo-v7-canonical-complete.json",
}
NATIVE_FIELDS = (
    "execution_id",
    "agent",
    "identity_kind",
    "native_identity_verified",
    "source_package_digest",
    "source_harbor_digest",
    "executed_harbor_digest",
    "staged_harbor_digest",
    "native_task_checksum",
    "metadata_sha256",
    "result_sha256",
    "job_result_sha256",
    "rewards",
    "scored",
    "repeat_scored",
    "test_execution",
    "process_ok",
    "process_failed",
    "returncode",
    "exception_present",
    "exception_type",
    "timed_out",
    "run_error_present",
    "repeat_contract_proved",
    "strict_expected_control",
    "recorded_reference_shell_exit_code",
    "reference_workflow_completed",
    "public_behavioral_smoke_observed",
)


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def task_id(value: str) -> str:
    value = value.removeprefix("mimo-v2.6-rl/")
    return "format-code-task-" + value if re.fullmatch(r"\d{6}", value) else value


def ref(source: str, pointer: str = "") -> dict[str, str]:
    return {"source_id": source, "json_pointer": pointer}


def native(value: dict[str, Any], source: str, pointer: str) -> dict[str, Any]:
    return {
        **{key: value[key] for key in NATIVE_FIELDS if key in value},
        "evidence": ref(source, pointer),
    }


def lineage_edge(value: dict[str, Any]) -> dict[str, Any]:
    parent = value["parent"]
    origin = parent.get("source") or {}
    return {
        "transform": value["transform"],
        "package_digest": value["package_digest"],
        "harbor_digest": value["harbor_digest"],
        "components_changed": value["components_changed"],
        "record_path": None if Path(value["record_path"]).is_absolute() else value["record_path"],
        "record_access": "private-native-record"
        if Path(value["record_path"]).is_absolute()
        else "repository-relative-metadata",
        "record_sha256": "sha256:" + value["record"]["sha256"].removeprefix("sha256:"),
        "parent": {
            "package_digest": parent["digest"],
            "harbor_digest": parent.get("harbor_digest"),
            "source_kind": origin.get("kind"),
            "source_repo": origin.get("repo"),
            "source_revision": origin.get("revision"),
        },
    }


def historical_control(value: dict[str, Any], source: str, pointer: str) -> dict[str, Any]:
    result = {"evidence": ref(source, pointer)}
    if "native_observation_unchanged" in value:
        result = native(
            value["native_observation_unchanged"], source, pointer + "/native_observation_unchanged"
        )
    result.update(
        {
            key: value[key]
            for key in (
                "native_execution_id",
                "expected_rewards",
                "observed_rewards",
                "status",
                "actual_case_execution_proved",
                "control_with_actual_case_evidence",
                "native_strict_control",
                "solution_only_override",
                "verifier_digest",
                "actual_submission_package_digest",
                "canonical_base_package_digest",
                "benign_no_submission_branch_only",
            )
            if key in value
        }
    )
    result["case_proof"] = (
        ref(source, pointer + "/independent_case_execution_proof")
        if "independent_case_execution_proof" in value
        else None
    )
    return result


def build(public_root: Path, evidence_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    artifacts = []
    inputs = {}
    for private, paths in ((True, PRIVATE_INPUTS), (False, PUBLIC_INPUTS)):
        revision = EVIDENCE_REVISION if private else PUBLIC_REVISION
        root = evidence_root if private else public_root
        repo = "PeterMakhnatch/research-context" if private else "PeterMakhnatch/eval-lab"
        for name, filename in paths.items():
            path = BRIDGE + filename if private else filename
            data = subprocess.check_output(["git", "show", f"{revision}:{path}"], cwd=root)
            artifacts.append(
                {
                    "source_id": name,
                    "repository": repo,
                    "revision": revision,
                    "path": path,
                    "sha256": digest(data),
                    "bytes": len(data),
                    "url": f"https://github.com/{repo}/blob/{revision}/{path}",
                    "access": "private-repository" if private else "public",
                }
            )
            if filename.endswith(".jsonl"):
                inputs[name] = [json.loads(line) for line in data.splitlines()]
            elif filename.endswith(".json"):
                inputs[name] = json.loads(data)

    heldout = inputs["heldout"]
    excluded = [task_id(value) for value in inputs["frozen_heldout"]["heldout"]]
    assert [task_id(value["task_id"]) for value in heldout["tasks"]] == excluded
    assert heldout["status"] == "released" and heldout["scoring_allowed"] is True
    assert heldout["pending_gates"] == []
    rows: list[dict[str, Any]] = []
    cohorts: dict[str, Any] = {}

    def add(cohort: str, value: dict[str, Any]) -> dict[str, Any]:
        value["task_id"] = task_id(value["task_id"])
        value["record_id"] = value["task_id"] + "@" + value["package_digest"]
        value["heldout_excluded_from_training"] = value["task_id"] in excluded
        value["usage"] = {
            "inherited_scoring_allowed": cohort == "heldout_v7",
            "training_authorized_by_this_release": False,
            "dispatch_authorized_by_this_release": False,
        }
        rows.append(value)
        cohorts.setdefault(cohort, {"members": []})["members"].append(value["record_id"])
        return value

    for index, value in enumerate(heldout["tasks"]):
        pointer = f"/tasks/{index}"
        target = value["evaluation_target"]
        selection = value["successor_selection"]
        if selection == "unchanged-v6":
            controls = value["native_controls"]["reported_baseline_controls"]
            control_pointer = pointer + "/native_controls/reported_baseline_controls"
            observations = [
                {
                    "role": "reference" if role == "oracle" else role,
                    "observation": native(
                        item["native"], "heldout", control_pointer + f"/observations/{role}/native"
                    ),
                }
                for role, item in controls["observations"].items()
            ]
        else:
            key = (
                "semantic_repair_controls"
                if selection == "semantic-repair"
                else "runtime_repair_controls"
            )
            controls = target[key]
            control_pointer = pointer + "/evaluation_target/" + key
            observations = [
                {
                    "role": item["role"],
                    "observation": native(
                        item["native"], "heldout", control_pointer + f"/observations/{j}/native"
                    ),
                }
                for j, item in enumerate(controls["observations"])
            ]
        selected = value["native_controls"]["selected_package"]
        add(
            "heldout_v7",
            {
                "task_id": value["task_id"],
                **{
                    key: target[key]
                    for key in ("package_digest", "harbor_digest", "verifier_digest")
                },
                "status": "unchanged-clean-controls" if selection == "unchanged-v6" else "repaired",
                "repair_scope": target["repair_scope"],
                "selection": selection,
                "evidence": ref("heldout", pointer),
                "controls": {
                    "reference": {"rewards": selected["measured_reference_rewards"]},
                    "nop": {"rewards": selected["measured_nop_rewards"]},
                    "observations": observations,
                    "scope": "exact released selected package",
                },
                "lineage": {
                    "evidence": ref("heldout", pointer + "/evaluation_target/lineage"),
                    "registered_edges_newest_first": [
                        lineage_edge(edge) for edge in target["lineage"]["records_newest_first"]
                    ],
                },
            },
        )

    batch = inputs["batch2"]
    batch_observations = {
        item["observation"]["execution_id"]: (index, item)
        for index, item in enumerate(batch["native_controls"])
    }
    for index, value in enumerate(batch["tasks"]):
        observations = []
        for identity in value["controls"]:
            obs_index, item = batch_observations[identity]
            assert task_id(item["task_id"]) == task_id(value["task_id"])
            observations.append(
                {
                    "role": item["role"],
                    "case": item["case"],
                    "scope_evidence": ref("batch2", f"/native_controls/{obs_index}"),
                    "observation": native(
                        item["observation"], "batch2", f"/native_controls/{obs_index}/observation"
                    ),
                }
            )
        pair = {
            role: [o["observation"] for o in observations if o["role"] == role]
            for role in ("reference", "nop")
        }
        assert len(pair["reference"]) == len(pair["nop"]) == 1
        add(
            "train_batch2_v7",
            {
                "task_id": value["task_id"],
                **{
                    key: value["selected"][key]
                    for key in ("package_digest", "harbor_digest", "verifier_digest")
                },
                "status": "repaired",
                "repair_scope": "assertion-scoped controls; not blanket semantic certification",
                "evidence": ref("batch2", f"/tasks/{index}"),
                "controls": {
                    "reference": pair["reference"][0],
                    "nop": pair["nop"][0],
                    "observations": observations,
                    "strict_reference_nop_pair": True,
                    "scope_evidence": ref("batch2", f"/tasks/{index}"),
                    "inherited_scope_evidence": ref(
                        "batch2_inherited_scope",
                        value["coverage_scope_inherited_from"]["json_pointer"],
                    )
                    if value.get("coverage_scope_inherited_from")
                    else None,
                },
                "lineage": {
                    "record_sha256": value["lineage_record"]["sha256"],
                    "evidence": ref("batch2", f"/tasks/{index}/lineage_record"),
                },
            },
        )

    snapshot = inputs["snapshot34"]
    baseline_by_id = {}
    for index, value in enumerate(inputs["baseline40"]["tasks"]):
        identity = task_id(value["task_id"])
        pair_proved = value["measured_pair_with_actual_case_evidence"]
        controls = {
            role: historical_control(control, "baseline40", f"/tasks/{index}/controls/{role}")
            for role, control in value["controls"].items()
        }
        if identity == "format-code-task-001647":
            assert (
                value["canonical_package"]["package_digest"]
                == snapshot["canonical_package"]["package_digest"]
            )
            controls = {"superseded_healthcheck_attempts": controls}
            for obs_index, item in enumerate(snapshot["observations"]):
                role = "reference" if item["agent"] == "oracle" else item["agent"]
                controls[role] = native(item, "snapshot34", f"/observations/{obs_index}")
            pair_proved = all(
                controls[role]["strict_expected_control"] for role in ("reference", "nop")
            )
        controls["case_bound_expected_pair_at_requested_snapshot"] = pair_proved
        controls["uniform_current_reader_strictness_claimed"] = False
        lineage = value["grader_lineage"]
        row = add(
            "train40_frozen",
            {
                "task_id": identity,
                **{
                    key: value["canonical_package"][key]
                    for key in ("package_digest", "harbor_digest", "verifier_digest")
                },
                "status": "unchanged-clean-controls" if pair_proved else "blocked",
                "repair_scope": "unchanged frozen package; native setup supplement for 001647 only",
                "evidence": ref("baseline40", f"/tasks/{index}"),
                "controls": controls,
                "lineage": {
                    "kind": lineage["kind"],
                    "complete_variant_record_chain_proved": False,
                    "reported_historical_transform_chain": lineage.get("full_fleet_record", {}).get(
                        "chain"
                    ),
                    "evidence": ref("baseline40", f"/tasks/{index}/grader_lineage"),
                },
            },
        )
        baseline_by_id[identity] = row
    assert list(baseline_by_id) == [task_id(value) for value in inputs["frozen_split"]["train"]]
    assert (
        sum(
            row["controls"]["case_bound_expected_pair_at_requested_snapshot"]
            for row in baseline_by_id.values()
        )
        == 34
    )

    expanded = {
        value["package_digest"]: (index, value)
        for index, value in enumerate(
            inputs["expanded_repairs"]["historical_qualification_scope"][
                "expanded_source_native_qualification_required"
            ]
        )
    }
    for index, value in enumerate(inputs["train_repairs"]["repairs"]):
        pending = expanded.get(value["package_digest"])
        controls = {}
        for role, key in (("reference", "reference_check"), ("nop", "do_nothing_check")):
            item = value[key]
            controls[role] = {
                "rewards": item["repeats"],
                "primary_rewards": item["rewards"],
                "status": item["status"],
                "run_id": item["run"],
                "native_execution_id": None,
                "matrix_sha256": item["matrix_sha256"],
                "evidence": ref("train_repairs", f"/repairs/{index}/{key}"),
            }
        controls["recorded_wrong_cases"] = [
            {
                key: item[key]
                for key in (
                    "finding_id",
                    "patch_sha256",
                    "script_sha256",
                    "killed",
                    "rewards",
                    "repeats",
                    "run",
                    "status",
                )
                if key in item
            }
            for item in value["reproducers"]
        ]
        controls["supplied_correct_alternate_count"] = len(value["adjudicated_correct_results"])
        controls["correct_control_scope"] = value["correct_control_scope"]
        add(
            "train_repairs_v7",
            {
                "task_id": value["task_id"],
                **{
                    key: value[key]
                    for key in ("package_digest", "harbor_digest", "verifier_digest")
                },
                "status": "blocked" if pending else "repaired",
                "repair_scope": "historical source-qualified verifier repair; expanded v2.1 cases are separate",
                "evidence": ref("train_repairs", f"/repairs/{index}"),
                "controls": controls,
                "no_original_assertion_weakened": value["no_original_assertion_weakened"],
                "historical_source_training_allowed": value["training_allowed"],
                "current_case_coverage": {
                    "additional_required_wrong_cases_unmeasured": pending[1][
                        "additional_wrong_cases"
                    ]
                    if pending
                    else 0,
                    "new_cases_measured": False if pending else None,
                    "historical_qualification_transferred_to_new_cases": False,
                    "evidence": ref("expanded_repairs", "/historical_qualification_scope"),
                },
                "lineage": {
                    "parent_digest": value["parent_digest"],
                    "frozen_parent_digest": value["frozen_parent_digest"],
                    "native_record_sha256": value["native_record_sha256"],
                    "candidate_sha256": value["candidate_sha256"],
                    "qualification_sha256": value["qualification_sha256"],
                },
            },
        )

    for index, item in enumerate(inputs["atlas_items"]):
        add(
            "weak_test_atlas_v2_1",
            {
                "task_id": item["subject"]["task_id"],
                "package_digest": item["subject"]["task_digest"],
                "harbor_digest": None,
                "verifier_digest": None,
                "status": "known-weak",
                "repair_scope": "none; exact-package historical diagnostic positive",
                "evidence": {
                    "source_id": "atlas_items",
                    "jsonl_index": index,
                    "item_id": item["item_id"],
                },
                "controls": {
                    "scope": "historical atlas controls and source adjudication; not a fresh replay",
                    "witness_ids": item["witness_ids"],
                    "expected_verdict": item["expected_verdict"],
                    "label_strength": item["label_strength"],
                },
                "lineage": {
                    "scope": "atlas exact-package binding; aggregate verifier identity not reported"
                },
            },
        )

    atlas_by_task = {task_id(value["subject"]["task_id"]): value for value in inputs["atlas_items"]}
    for row in rows:
        historical = atlas_by_task.get(row["task_id"])
        exact = (
            historical is not None and historical["subject"]["task_digest"] == row["package_digest"]
        )
        row["weakness_review"] = {
            "exact_package_atlas_positive": exact,
            "same_task_historical_positive_item": historical["item_id"] if historical else None,
            "semantic_cleanliness_certified": False,
            "absence_is_not_a_clean_negative": True,
        }
        assert exact == (row["status"] == "known-weak")

    later_references = []
    for index, value in enumerate(inputs["later37"]["task_controls"]):
        original = baseline_by_id[task_id(value["task_id"])]
        assert value["canonical_package"]["package_digest"] == original["package_digest"]
        attempts = [
            native(item, "later37", f"/task_controls/{index}/new_reference_attempts/{j}")
            for j, item in enumerate(value["new_reference_attempts"])
        ]
        completed = [item for item in attempts if item["reference_workflow_completed"]]
        assert len(completed) == 1 and completed[0]["strict_expected_control"]
        later_references.append(
            {
                "record_id": original["record_id"],
                "task_id": original["task_id"],
                "package_digest": original["package_digest"],
                "reference": completed[0],
                "all_reference_attempts": attempts,
                "nop_retained_from_requested_snapshot": original["controls"]["nop"],
                "case_bound_expected_pair": value["case_bound_expected_pair"],
                "changes_requested_snapshot_or_admission": False,
            }
        )

    for cohort in cohorts.values():
        cohort["package_count"] = len(cohort["members"])
    cohorts["train40_frozen"].update(
        {"case_bound_pairs": 34, "snapshot_source_id": "snapshot34", "later_measured_pairs": 37}
    )
    cohorts["heldout_v7"]["release_authority"] = {
        "candidate_verifier": ref("heldout_verifier"),
        "gate_only_adoption": ref("heldout_adoption"),
        "final_released_bytes_separately_reread_by_independent_verifier": False,
        "release_does_not_change_evidence_or_membership": True,
    }
    cohorts["train_batch2_v7"]["scope"] = (
        "20 strict reference/nop pairs; no dispatch or semantic admission"
    )
    cohorts["train_repairs_v7"].update(
        {
            "historical_qualified": 9,
            "unchanged_required_case_sets": 6,
            "expanded_case_sets_blocked": 3,
            "added_wrong_cases_unmeasured": 21,
        }
    )
    cohorts["weak_test_atlas_v2_1"].update(
        {
            "historical_supported_positives": 114,
            "whole_patch_witnesses": 242,
            "inventory": 567,
            "not_established": 453,
            "certified_clean_negatives": 0,
        }
    )
    assert len(rows) == len({row["record_id"] for row in rows}) == 216
    assert [cohort["package_count"] for cohort in cohorts.values()] == [33, 20, 40, 9, 114]
    core = [row for row in rows if row["status"] != "known-weak"]
    assert len(core) == 102 and len({row["task_id"] for row in core}) == 91
    assert not any(row["heldout_excluded_from_training"] for row in core[33:])
    assert all(
        row["controls"][role]["rewards"] == [expected] * 3
        for row in core[:53]
        for role, expected in (("reference", 1), ("nop", 0))
    )
    manifest = {
        "schema": "mimo.clean.release/v1",
        "dataset_id": "mimo-clean-v7",
        "created_at": CREATED_AT,
        "release_kind": "source-pinned metadata catalog; no new benchmark execution",
        "producer": {
            "path": "build_manifest.py",
            "transform": "mimo-clean-v7-catalog@1",
            "sha256": digest(Path(__file__).read_bytes()),
        },
        "counts": {
            "core_selected_packages": 102,
            "core_distinct_task_ids": 91,
            "historical_weak_diagnostic_packages": 114,
            "all_package_rows": 216,
            "status_counts": dict(sorted(Counter(row["status"] for row in rows).items())),
        },
        "usage": {
            "training_authorized_by_this_release": False,
            "paid_execution_authorized": False,
            "dispatch_authorized": False,
            "raw_evidence_redistribution_authorized": False,
            "heldout_task_ids_excluded_from_training_all_versions": excluded,
            "historical_training_exposure": ref("heldout", "/training_exposure"),
            "repeat3": "three grader invocations on the same final submission, not independent solves",
            "semantic_or_honesty_certification": False,
            "identity_join": ["canonical_task_id", "full_package_digest"],
            "package_materialization": "not included; locate and verify exact digests with authorized sources",
        },
        "source_artifacts": artifacts,
        "cohorts": cohorts,
        "tasks": rows,
        "supplements": {"train40_later_references": later_references},
        "links": {
            "atlas_v2_1": "../../answer-keys/mimo-weak-tests-v2.1/README.md",
            "labelled_trajectories": "../qa-trace/README.md",
            "license_notice": "../../answer-keys/mimo-weak-tests-v2.1/NOTICE",
        },
        "licensing": {
            "catalog_metadata": "Apache-2.0",
            "task_bytes_relicensed": False,
            "project_license_audit_complete": False,
            "source_notices": ["atlas_notice", "atlas_license", "atlas_agent_license"],
        },
    }
    material = encoded(manifest)
    provenance = {
        "schema_version": 1,
        "item_id": "mimo-clean-v7",
        "zone": "04-curated",
        "source_uri": "research/experiments/mimo-clean-v7/manifest.json",
        "revision": None,
        "material_digest": digest(material),
        "license": "Apache-2.0",
        "created_at": CREATED_AT,
        "created_by": "mimo-data",
        "transform": "mimo-clean-v7-catalog@1",
        "parent_digests": [artifact["sha256"] for artifact in artifacts],
        "notes": "Allowlisted metadata projection, exact task/package deduplication and immutable cohort selection. "
        "Held-out IDs excluded from all training versions. No raw payloads, license override, "
        "new semantic certification or execution authority; private proof requires source access.",
    }
    return manifest, provenance


def encoded(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-root",
        type=Path,
        required=True,
        help="authorized research-context checkout containing the pinned revision",
    )
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent)
    args = parser.parse_args()
    public_root = Path(__file__).resolve().parents[3]
    manifest, provenance = build(public_root, args.evidence_root)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (("manifest.json", manifest), ("provenance.json", provenance)):
        (args.output_dir / name).write_bytes(encoded(value))
    print(
        json.dumps(
            {
                "dataset_id": manifest["dataset_id"],
                "counts": manifest["counts"],
                "material_digest": provenance["material_digest"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
