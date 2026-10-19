"""Join v7.1 delta controls to retained v7 evidence without inheriting release rights."""
from __future__ import annotations

import argparse
import hashlib
import json
import runpy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]


def encoded(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def build(manifest_path: Path, predecessor_controls: Path, repo_root: Path) -> dict[str, Any]:
    freeze = runpy.run_path(str(repo_root / "research/experiments/mimo-training-20261016/freeze.py"))
    sources: dict[str, dict[str, Any]] = {}

    def load(path: Path) -> dict[str, Any]:
        return json.loads(freeze["source_bytes"](path.resolve(), sources))

    def bind(reference: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
        return freeze["_successor_binding"](path or Path(reference["path"]), reference, sources)

    manifest = load(manifest_path)
    if manifest.get("successor_version") != "v7.1":
        raise ValueError("expected the closed v7.1 candidate")
    predecessor_ref = manifest["predecessor"]
    bind(predecessor_ref)
    predecessor_path = Path(predecessor_ref["path"])
    predecessor = load(predecessor_path)
    frozen = Path(manifest["frozen_sources"]["heldout.json"]["path"]).parent
    repairs_path = Path(manifest["semantic_repairs"]["source"]["path"])
    freeze["validate_successor"](
        manifest, frozen, repairs_handoff=repairs_path,
        predecessor_manifest=predecessor_path, repo_root=repo_root,
    )
    previous = load(predecessor_controls)
    if (previous.get("schema") != "mimo-data.heldout-v7-selected-control-join/v1"
            or previous["heldout"] != manifest["heldout"]
            or [row["task_id"] for row in previous["tasks"]] != manifest["heldout"]):
        raise ValueError("predecessor control index changes the ordered frozen33")
    # The released file superseded its pre-release bytes at the same path. The
    # Verifier's qualified_candidate retains those exact old bytes; no other
    # historical source alias is accepted.
    qualified_ref = predecessor["independent_verifier"]["qualified_candidate"]
    if previous["manifest"]["sha256"] != qualified_ref["sha256"].removeprefix("sha256:"):
        raise ValueError("control index does not bind the independently qualified predecessor")
    bind(qualified_ref)
    qualified = load(Path(qualified_ref["path"]))
    if any(qualified[key] != predecessor[key] for key in ("heldout", "tasks", "train", "training_exposure", "frozen_sources")):
        raise ValueError("released predecessor changed selected packages or evidence after qualification")
    for reference in previous["sources"]:
        if reference == previous["manifest"]:
            continue
        bind(reference)
    handoff = load(repairs_path)
    if "native_consumer_proof" in handoff:
        bind(handoff["native_consumer_proof"])
    native_reader = runpy.run_path(str(repo_root / "research/experiments/mimo-v6-full-fleet/census.py"))["native_observations"]
    executions: set[str] = set()

    def measured(native: dict[str, Any], expected: float, repeated: bool | None) -> dict[str, Any]:
        metadata = native["metadata"] if "metadata" in native else {
            "path": native["metadata_path"], "sha256": native["metadata_sha256"],
        }
        result = native["native_result"] if "native_result" in native else {
            "path": native["result_path"], "sha256": native["result_sha256"],
        }
        bind(metadata)
        bind(result)
        actual_rows = native_reader({Path(metadata["path"])})
        if len(actual_rows) != 1:
            raise ValueError("control lacks exactly one physical native observation")
        actual = actual_rows[0]
        for key in ("execution_id", "rewards", "source_package_digest", "source_harbor_digest", "staged_harbor_digest"):
            if actual[key] != native[key]:
                raise ValueError(f"retained native control differs from actual bytes: {key}")
        repeat_count = len(actual["rewards"]) if repeated is None else 3 if repeated else 1
        if (actual["execution_id"] in executions or actual["scored"] is not True
                or repeat_count not in {1, 3} or actual["rewards"] != [expected] * repeat_count
                or actual["test_execution"] != [True] * len(actual["rewards"])
                or actual["process_ok"] is not True or actual["native_identity_verified"] is not True
                or actual["exception_present"] is not False or actual["timed_out"] is not False
                or actual["returncode"] != 0):
            raise ValueError(f"control lacks distinct, case-bound, successful physical measurement: {actual['execution_id']}")
        executions.add(actual["execution_id"])
        return {
            **{key: actual[key] for key in (
                "execution_id", "rewards", "test_execution", "scored", "source_package_digest",
                "source_harbor_digest", "staged_harbor_digest", "native_task_checksum",
            )},
            "metadata": bind(metadata), "native_result": bind(result),
            "normal_pipeline_counted_model_pass_inferred": False,
        }

    tasks = []
    previous_rows = {row["task_id"]: row for row in previous["tasks"]}
    changes = manifest["semantic_repairs"]["selections"]
    for row in manifest["tasks"]:
        task_id, target = row["task_id"], row["evaluation_target"]
        selected = {key: target[key] for key in ("package_path", "package_digest", "harbor_digest", "verifier_digest")}
        old = previous_rows[task_id]
        if task_id not in changes:
            if old["selected"] != selected:
                raise ValueError("retained control belongs to a different selected package")
            for role, expected in (("reference", 1.0), ("nop", 0.0)):
                measured(old["baseline"][role], expected, True)
            for key, expected in (("adjudicated_wrong_controls", 0.0), ("adjudicated_correct_controls", 1.0)):
                for control in old[key]:
                    bind(control["patch"])
                    bind(control["script"])
                    measured(control["native"], expected, None if expected == 1.0 else False)
            tasks.append(old)
            continue
        observations = target["semantic_repair_controls"]["observations"]
        by_run = {entry["run"]: entry for entry in observations}
        baseline = {}
        for role, expected in (("reference", 1.0), ("nop", 0.0)):
            matches = [entry for entry in observations if entry["role"] == role]
            if len(matches) != 1:
                raise ValueError("changed package lacks its unique native baseline")
            native = matches[0]["native"]
            if native["source_package_digest"] != selected["package_digest"]:
                raise ValueError("baseline was transferred from another package")
            baseline[role] = measured(native, expected, True)
        receipt = target["selection_handoff"]["receipt"]
        wrong = []
        for control in receipt["reproducers"]:
            observation = by_run[control["run"]]
            if observation["role"] != "known-wrong" or control["status"] != "ok" or control["killed"] is not True:
                raise ValueError("wrong-submission control was not a completed native rejection")
            wrong.append({
                "finding_id": control["finding_id"], "effective_role": "known-wrong",
                "validation_stage": control.get("validation_stage"), "run": control["run"],
                "patch": bind({"path": control["patch"], "sha256": control["patch_sha256"]}),
                "script": bind({"path": control["script"], "sha256": control["script_sha256"]}),
                "native": measured(observation["native"], 0.0, False),
            })
        # Producer inventories may shorten finding IDs. Program identity is the
        # task plus exact patch bytes, not the spelling of a historical label.
        inherited_wrong = {item["patch"]["sha256"] for item in old["adjudicated_wrong_controls"]}
        current_wrong = {item["patch"]["sha256"] for item in wrong}
        if not inherited_wrong <= current_wrong:
            raise ValueError("changed grader omits a previously adjudicated wrong program")
        for control in wrong:
            control["predecessor_finding_ids"] = [
                item["finding_id"] for item in old["adjudicated_wrong_controls"]
                if item["patch"]["sha256"] == control["patch"]["sha256"]
            ]
        if old["adjudicated_correct_controls"] or receipt["adjudicated_correct_controls"]:
            raise ValueError("this two-task delta requires no supplied adjudicated-correct alternates")
        unspecified = [item | {
            "optional_historical_observations": [], "selected_v71_outcome": None,
            "scope": "Unspecified verdict; no directional requirement or selected-v7.1 measurement.",
        } for item in old["unspecified_inventory"]]
        tasks.append(old | {
            "selected": selected, "baseline": baseline, "adjudicated_wrong_controls": wrong,
            "unspecified_inventory": unspecified, "predecessor_unspecified_inventory": old["unspecified_inventory"],
        })
    wrong_controls = [control for row in tasks for control in row["adjudicated_wrong_controls"]]
    correct_controls = [control for row in tasks for control in row["adjudicated_correct_controls"]]
    for reference in list(sources.values()):
        bind(reference)
    return {
        "schema": "mimo-data.heldout-v71-selected-control-join/v1",
        "status": "measured_native_join_pending_independent_semantic_release",
        "manifest": dict(sources[str(manifest_path.resolve())]),
        "predecessor_controls": dict(sources[str(predecessor_controls.resolve())]),
        "heldout": manifest["heldout"], "tasks": tasks,
        "counts": {
            "reference_nop_pairs": len(tasks), "reference_nop_grades": 6 * len(tasks),
            "unique_wrong_programs": len({(row["task_id"], control["patch"]["sha256"])
                                          for row in tasks for control in row["adjudicated_wrong_controls"]}),
            "wrong_control_executions": len(wrong_controls), "adjudicated_correct_payloads": len(correct_controls),
            "fresh_s3_wrong_executions": sum(control.get("validation_stage") == "fresh-s3" for control in wrong_controls),
            "strict_control_native_uuids": len(executions),
            "strict_control_grader_invocations": 6 * len(tasks) + sum(len(control["native"]["rewards"])
                                                                     for control in wrong_controls + correct_controls),
        },
        "sources": sorted(sources.values(), key=lambda item: item["path"]),
        "independent_verifier": {"status": "pending", "evidence": None},
        "scoring_allowed": False, "training_allowed": False, "freeze_ready": False,
        "new_native_trials": 0, "new_grades": 0, "new_model_provider_modal_paid_calls": 0,
        "limits": ["Physical controls are not independent semantic certification.",
                   "No independently adjudicated-correct alternates were supplied for the two changed tasks.",
                   "Historical release flags, paid scopes and training permissions are not inherited."],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--predecessor-controls", required=True, type=Path)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    payload = build(args.manifest, args.predecessor_controls, args.repo_root.resolve())
    if args.check:
        if args.output.read_text() != encoded(payload):
            raise ValueError("delivered index differs from actual manifest, controls or source bytes")
    else:
        with args.output.open("x") as stream:
            stream.write(encoded(payload))
    print(json.dumps({"index": {"path": str(args.output.resolve()), "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest()},
                      "counts": payload["counts"], "scoring_allowed": False, "checked_serialized_bytes": args.check}, sort_keys=True))


if __name__ == "__main__":
    main()
