"""Fixed-submission hardening curves; native Docker calibration, never released scoring.

Inventory binds canonical70 and the entire separately published window3 snapshot.
A strict freeze.py repair round is required for plan/run. No existing campaign
admission, paid grant, native job, submission or release is modified by this tool.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from evallab.qa import counterfactual as cf
from evallab.task_variants import VariantRecord

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
REGR = runpy.run_path(str(HERE / "regrade.py"))
FREEZE = runpy.run_path(str(HERE.parent / "mimo-training-20261016/freeze.py"))
NATIVE = runpy.run_path(str(HERE / "native.py"))
CAMPAIGN = runpy.run_path(str(HERE / "campaign.py"))
COHORTS = ("canonical70", "fresh_window3")
CONTRACT = "counterfactual-retained-diff/docker-model-free/v1"


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def object_at(path: Path) -> dict[str, Any]:
    return REGR["json_object"](path)


def ref(path: Path, expected: str | None = None) -> dict[str, Any]:
    return REGR["file_ref"](path, expected)


def bound(binding: dict[str, Any]) -> bytes:
    return REGR["bound_bytes"](binding)[0]


def identity(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def write_new(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    return REGR["create_json"](path, value)


def uid(source: dict[str, Any]) -> tuple[str, str]:
    value = source["source_uid"]
    require(isinstance(value, list) and len(value) == 2, "source requires job/trial UUID pair")
    return tuple(REGR["_native_id"](item) for item in value)


def checked_source(source: dict[str, Any]) -> None:
    job_id, trial_id = uid(source)
    trial = Path(source["source_trial_dir"]).resolve(strict=True)
    raw = object_at(trial / "result.json")
    require(raw.get("id") == trial_id and raw["config"].get("job_id") == job_id,
            "retained native source UUID changed")
    require(cf.source_digest(trial) == source["source_digest"], "retained source bytes changed")
    require(raw["config"]["agent"].get("model_name") == source["requested_model"],
            "retained source model changed")
    if source.get("diff") is not None:
        bound(source["diff"])


def _fresh(result_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    result = object_at(result_path)
    require(result.get("schema") == "mimo.window3-native-result/v1", "unsupported window3 result")
    bindings = {"result": ref(result_path), "plan": result["plan"],
                "census": result["custody"]["census"], "labels": result["custody"]["pass_labels"],
                "serving_receipt": result["serving_receipt"], "prepared_live": result["prepared_live"]}
    documents = {key: json.loads(bound(value)) for key, value in bindings.items() if key != "census"}
    census = [json.loads(line) for line in bound(bindings["census"]).decode().splitlines() if line.strip()]
    plan, labels = documents["plan"], documents["labels"]
    require(plan.get("schema") == "mimo.phase2-native-plan/v1"
            and labels.get("schema") == "mimo.window3-native-pass-labels/v1", "unsupported window3 custody")
    cells = {row["cell_id"]: row for row in plan["cells"]}
    census_by_id = {row["cell_id"]: row for row in census}
    require(len(cells) == len(plan["cells"]) == len(census_by_id) == len(census)
            and cells.keys() == census_by_id.keys(), "window3 snapshot omits/duplicates planned dispositions")
    require(result["census"]["planned_cells"] == len(cells)
            and result["census"]["dispositions"] == len(census), "window3 published snapshot count changed")
    label_by_id = {row["cell_id"]: row for row in labels["labels"]}
    require(len(label_by_id) == len(labels["labels"]) and label_by_id.keys() <= cells.keys(),
            "window3 duplicate/unknown label cell")
    tasks = {row["task"]: row for row in plan["tasks"]}
    rows = []
    for cell_id, raw in census_by_id.items():
        planned = cells[cell_id]
        require(raw["task_id"] == planned["task_id"] and raw["attempt_index"] == planned["attempt_index"],
                "window3 census changed planned task/attempt")
        reward = raw.get("native_repaired_reward")
        if reward is not None:
            require(type(reward) in (int, float) and reward in (0, 1), "window3 nonbinary/bool raw reward")
        if reward != 1:
            continue
        require(cell_id in label_by_id, "published fresh pass lacks a retained label disposition")
        task = REGR["canonical_task"](planned["task"])
        package = tasks[task]
        trial = Path(raw["trial_path"]).resolve(strict=True)
        native = object_at(trial / "result.json")
        source_uid = [REGR["_native_id"](raw["native_job_id"]), REGR["_native_id"](raw["execution_id"])]
        require(native["id"] == source_uid[1] and native["config"]["job_id"] == source_uid[0],
                "window3 census/native UUID disagreement")
        native_reward = (native.get("verifier_result") or {}).get("rewards", {}).get("reward")
        require(type(native_reward) in (int, float) and native_reward == reward
                and (native.get("exception_info") or {}).get("exception_type") == raw.get("exception_type"),
                "window3 raw reward/exception disagrees with census")
        require(raw["task_staging"]["declared_task_name"] == task
                and raw["task_staging"]["source_package_digest"] == package["package_digest"],
                "window3 source package/task mismatch")
        model = raw["model_identity"]
        require(model.get("matched") is True and model.get("requested") == plan["model"]
                and model.get("returned") == plan["returned_model_required"], "window3 model identity mismatch")
        # source_digest does not include this filename; bind retained patch bytes separately.
        patch = ref(trial / "verifier/agent.kept.diff", raw["kept_diff_sha256"])
        full = ref(trial / "verifier/agent.full.diff", raw["full_diff_sha256"])
        digests = NATIVE["compute_task_digests"](Path(package["package_path"]))
        require(digests.package == package["package_digest"] and digests.verifier == package["verifier_digest"]
                and NATIVE["harbor_task_digest"](Path(package["package_path"])) == package["harbor_digest"],
                "window3 retained source package identity changed")
        record = VariantRecord.model_validate(json.loads(bound(package["record"])))
        require(record.task_name == task and record.variant_digest == package["package_digest"]
                and record.variant_harbor_digest == package["harbor_digest"],
                "window3 retained VariantRecord does not identify its source package")
        metadata_path = trial.parent / "lab-metadata.json"
        metadata = object_at(metadata_path)
        cases = runpy.run_path(str(HERE.parent / "mimo-v6-full-fleet/census.py"))["positive_case_execution"](trial)
        native_clean = (native.get("exception_info") is None and bool(native.get("finished_at"))
                        and type(metadata.get("exit_code")) is int and metadata["exit_code"] == 0
                        and metadata.get("timed_out") is False and cases is True and not raw.get("problems"))
        source = {
            "source_uid": source_uid, "source_trial_dir": str(trial), "native_job_dir": str(trial.parent),
            "task_id": task, "arm": "window3_base", "attempt_index": raw["attempt_index"],
            "population": "fresh_window3_published_reward1", "requested_model": plan["model"],
            "model_identity": model, "task_staging": raw["task_staging"],
            "source_package": {"path": package["package_path"], "sha256": package["package_digest"]},
            "source_digest": cf.source_digest(trial), "diff": patch, "full_diff": full,
            "v6_outcome": None, "v6_error": "original_v6_unmeasured",
            "native_exception_type": raw.get("exception_type"), "native_raw_reward": raw.get("verifier_rewards"),
            "native_artifacts": {"trial_result": ref(trial / "result.json"), "job_metadata": ref(metadata_path),
                                 "source_record": package["record"],
                                 "trial_config": ref(trial / "config.json"),
                                 "setup_fingerprint": ref(trial / "setup-fingerprint.json")},
        }
        checked_source(source)
        rows.append({"cohort": "fresh_window3", "source": source,
                     "published_pass": {"reward": reward, "label": label_by_id[cell_id],
                                        "native_status": raw["status"], "exception_type": raw.get("exception_type"),
                                        "native_clean": native_clean},
                     "baseline": {"outcome": None, "error": "no_same_task_predecessor_grade",
                                  "origin": "producer repaired reward is not a heldout-round baseline"},
                     "gold_correct": None, "fresh_cell_id": cell_id})
    require(len(rows) == result["semantic_labels"]["adjudicated_reward1_candidates"],
            "window3 published candidate count mismatch")
    endpoints = [row for row in census if row.get("execution_id")]
    require(len(endpoints) == result["census"]["actual_native_trial_endpoints"], "window3 endpoint census mismatch")
    return rows, {
        "bindings": bindings, "reservation_id": result["reservation_id"], "model": plan["model"],
        "returned_model": plan["returned_model_required"], "producer_status": result["status"],
        "snapshot_complete": True, "planned_dispositions": len(census), "native_endpoints": len(endpoints),
        "published_pass_candidates": len(rows),
        "native_clean_pass_candidates": sum(row["published_pass"]["native_clean"] for row in rows),
        "producer_census": result["census"],
        "status_counts": dict(Counter(row["status"] for row in census)),
        "source_error_candidates": sum(row["published_pass"]["exception_type"] is not None for row in rows),
        "plan_tasks": [REGR["canonical_task"](row["task"]) for row in plan["tasks"]],
        "limits": ["Snapshot completeness means one disposition per planned cell, not a completed rollout.",
                   "Producer real_pass is not independent gold; source timeouts and original-v6 unmeasured persist."],
    }


def inventory(canonical_inventory: Path, canonical_labels: Path, fresh_result: Path,
              gold_labels: Path | None = None) -> dict[str, Any]:
    source_inventory, labels = object_at(canonical_inventory), object_at(canonical_labels)
    require(source_inventory.get("schema") == "heldout-v7.inventory/v1"
            and labels.get("schema") == "heldout-v7.pass-labels/v1", "unsupported canonical inputs")
    pass_rows = [row for row in source_inventory["rows"] if row["v6_outcome"] is True]
    passes = {uid(row): row for row in pass_rows}
    require(len(pass_rows) == len(passes) == len(labels["labels"]) == 70,
            "canonical cohort must contain exactly70 distinct physical passes")
    require(labels.get("canonical_scoring_allowed") is True, "baseline labels are not the released original-v7 publication")
    admission = labels["admission"]
    manifest = json.loads(bound(admission["manifest"]))
    bound(admission["verifier_receipt"])
    require(manifest["successor_version"] == "v7" and manifest["status"] == "released"
            and manifest.get("scoring_allowed") is True and manifest.get("freeze_ready") is True
            and manifest.get("pending_gates") == []
            and manifest["independent_verifier"]["status"] == "pass", "baseline must be independently accepted originalv7")
    require(hashlib.sha256(("\n".join(manifest["heldout"]) + "\n").encode()).hexdigest()
            == FREEZE["SUCCESSOR_ORDER_SHA256"], "canonical release changes frozen33 order or membership")
    admitted, _ = CAMPAIGN["admit"](
        source_inventory, Path(admission["manifest"]["path"]),
        admission["operator_attested_verifier_accepted_sha256"], Path(admission["verifier_receipt"]["path"]),
    )
    require(admitted["manifest"] == admission["manifest"] and admitted["verifier_receipt"] == admission["verifier_receipt"],
            "canonical publication changed its exact accepted baseline admission")
    ordered33 = [REGR["canonical_task"](task) for task in manifest["heldout"]]
    require(set(ordered33) == set(source_inventory["frozen_task_ids"]), "canonical frozen33 mismatch")
    rows, seen = [], set()
    for label in labels["labels"]:
        source_id = uid(label)
        require(source_id in passes and source_id not in seen, "canonical missing/duplicate physical source")
        seen.add(source_id)
        source = passes[source_id]
        require(label["original_pass"] is True and label["task_id"] == source["task_id"]
                and label["source"]["source_digest"] == source["source_digest"]
                and label["source"].get("diff") == source.get("diff"), "canonical source identity changed")
        checked_source(source)
        before = label.get("v7_outcome")
        require(before is None or type(before) is bool, "nonboolean canonical baseline")
        rows.append({"cohort": "canonical70", "source": source, "gold_correct": None,
                     "baseline": {"outcome": before, "error": None if before is not None else "released_v7_unmeasured",
                                  "origin": "retained_original_v7_grade_not_fresh_execution",
                                  "manifest": admission["manifest"]}})
    fresh, fresh_snapshot = _fresh(fresh_result)
    rows.extend(fresh)
    all_uids = [uid(row["source"]) for row in rows]
    require(len(all_uids) == len(set(all_uids)), "physical source appears twice across cohorts")
    for row in rows:
        row["cell_id"] = identity({"cohort": row["cohort"], "source_uid": row["source"]["source_uid"]})[7:]
        row["target_task_in_frozen33"] = row["source"]["task_id"] in ordered33
    sources = {"canonical_inventory": ref(canonical_inventory), "canonical_labels": ref(canonical_labels),
               "canonical_baseline_manifest": admission["manifest"], "fresh_result": ref(fresh_result)}
    if gold_labels is not None:
        gold = object_at(gold_labels)
        require(gold.get("schema") == "mimo.hardening-gold/v1", "unsupported independent gold contract")
        indexed = {uid(row["source"]): row for row in rows}
        gold_seen = set()
        for label in gold["labels"]:
            key = uid(label)
            require(key in indexed and key not in gold_seen, "unknown/duplicate gold source")
            gold_seen.add(key)
            row = indexed[key]
            adjudication = json.loads(bound(label["independent_adjudication"]))
            require(adjudication.get("schema") == "mimo.independent-correct/v1"
                    and adjudication.get("independent") is True and adjudication.get("verdict") == "correct"
                    and isinstance(adjudication.get("reviewer"), str) and bool(adjudication["reviewer"])
                    and adjudication.get("source_uid") == row["source"]["source_uid"]
                    and adjudication.get("task_id") == row["source"]["task_id"]
                    and adjudication.get("diff_sha256") == row["source"]["diff"]["sha256"],
                    "gold must independently adjudicate this exact task/submission/physical source")
            row["gold_correct"] = label["independent_adjudication"]
        sources["gold_labels"] = ref(gold_labels)
    return {"schema": "mimo.hardening-inventory/v1", "sources": sources, "ordered33": ordered33,
            "fresh_snapshot": fresh_snapshot, "rows": rows,
            "summary": {"canonical70": 70, "fresh_window3": len(fresh), "total_cells": len(rows),
                        "fresh_supported_by_frozen33": sum(row["target_task_in_frozen33"] for row in fresh),
                        "source_errors": dict(Counter(row["cohort"] for row in rows
                                                       if row["source"].get("native_exception_type") is not None)),
                        "independent_gold_correct": sum(row["gold_correct"] is not None for row in rows)},
            "training_allowed": False, "scoring_allowed": False,
            "limits": ["Fixed physical executions, not distinct programs/tasks; equal patchSHA never deduplicates UUIDs.",
                       "Pass flips measure retrospective grader sensitivity, not cheating or model-behavior gains."]}


def validate_inventory(path: Path) -> dict[str, Any]:
    value = object_at(path)
    require(value.get("schema") == "mimo.hardening-inventory/v1", "unsupported round inventory")
    inputs = value["sources"]
    for binding in inputs.values():
        bound(binding)
    expected = inventory(Path(inputs["canonical_inventory"]["path"]), Path(inputs["canonical_labels"]["path"]),
                         Path(inputs["fresh_result"]["path"]),
                         Path(inputs["gold_labels"]["path"]) if "gold_labels" in inputs else None)
    require(value == expected, "round inventory differs from exact published cohorts")
    return value


def validate_round(manifest_path: Path, repo_root: Path) -> dict[str, Any]:
    manifest = object_at(manifest_path)
    round_id = manifest.get("round") or {}
    require(round_id.get("measurement_mode") == "native_calibration_only"
            and type(round_id.get("number")) is int, "pending manifest alone is not a strict qualified repair round")
    require(manifest.get("training_allowed") is False and manifest.get("scoring_allowed") is False
            and manifest.get("freeze_ready") is False, "native calibration must not carry released scoring/training rights")
    repairs, predecessor = round_id["repairs"], round_id["predecessor"]
    bound(repairs)
    bound(predecessor)
    frozen = Path(manifest["frozen_sources"]["heldout.json"]["path"]).parent
    FREEZE["validate_successor"](manifest, frozen, repairs_handoff=Path(repairs["path"]),
                                  predecessor_manifest=Path(predecessor["path"]),
                                  round_number=round_id["number"], repo_root=repo_root)
    return manifest


def predecessor_accepts(manifest: dict[str, Any], previous_target: dict[str, Any]) -> bool:
    predecessor = json.loads(bound(manifest["predecessor"]))
    accepted = [manifest["predecessor"], predecessor["independent_verifier"]["qualified_candidate"]]
    return any(REGR["bare_sha"](binding["sha256"]) == REGR["bare_sha"](previous_target["sha256"])
               for binding in accepted)


def plan(inventory_path: Path, manifest_path: Path, repo_root: Path,
         previous_report: Path | None = None) -> dict[str, Any]:
    cohort = validate_inventory(inventory_path)
    manifest = validate_round(manifest_path, repo_root)
    require([REGR["canonical_task"](task) for task in manifest["heldout"]] == cohort["ordered33"],
            "round changed frozen ordered33")
    previous = None
    if previous_report is not None:
        previous = checked_report(previous_report, repo_root)
        require(previous["inventory"] == ref(inventory_path)
                and previous["round"]["number"] == manifest["round"]["number"] - 1
                and predecessor_accepts(manifest, previous["target_manifest"]),
                "previous report does not bind this exact cohort and accepted predecessor candidate")
        require({row["cell_id"] for row in previous["cells"]} == {row["cell_id"] for row in cohort["rows"]},
                "previous report dropped source cells")
    graders = {REGR["canonical_task"](row["task_id"]): row["evaluation_target"] for row in manifest["tasks"]}
    return {"schema": "mimo.hardening-plan/v1", "inventory": ref(inventory_path),
            "manifest": ref(manifest_path), "round": manifest["round"], "graders": graders,
            "previous_report": ref(previous_report) if previous_report else None,
            "baseline_is_original_v7": manifest["predecessor"]["sha256"].removeprefix("sha256:")
                == cohort["sources"]["canonical_baseline_manifest"]["sha256"].removeprefix("sha256:"),
            "calibration_contract": CONTRACT, "environment": "docker", "model_calls": 0,
            "native_trial_concurrency": 1, "lane_container_ceiling": 2, "shared_fifo_slots": 1,
            "paid_execution_allowed": False, "scoring_allowed": False, "training_allowed": False,
            "cell_ids": [row["cell_id"] for row in cohort["rows"]],
            "coverage": cohort["summary"],
            "limits": ["Qualified model-free counterfactual calibration only; does not call or weaken campaign.admit.",
                       "All canonical/fresh cells persist, including unsupported tasks and failed attempts.",
                       "Frozen33 has no task swaps or rights inherited from its predecessor."]}


def validate_plan(path: Path, repo_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    value = object_at(path)
    require(value.get("schema") == "mimo.hardening-plan/v1", "unsupported native round plan")
    for key in ("inventory", "manifest", "previous_report"):
        if value.get(key) is not None:
            bound(value[key])
    expected = plan(Path(value["inventory"]["path"]), Path(value["manifest"]["path"]), repo_root,
                    Path(value["previous_report"]["path"]) if value.get("previous_report") else None)
    require(value == expected, "native round plan differs from its exact closed-gate calibration inputs")
    return value, object_at(Path(value["inventory"]["path"]))


def grader_for(task: str, selected: dict[str, Any]) -> cf.Grader:
    chain = selected["lineage"]["records_newest_first"]
    return cf.Grader(task_name=task, transform="hardening-round",
                     variant_digest=selected["package_digest"], package=Path(selected["package_path"]),
                     record=chain[0]["record"]["path"] if chain else "")


def cell_identity(row: dict[str, Any], selected: dict[str, Any] | None) -> dict[str, Any]:
    return {"cell_id": row["cell_id"], "source_identity": identity(row["source"]), "target": selected}


def run(plan_path: Path, work: Path, out: Path, repo_root: Path) -> dict[str, Any]:
    execution_plan, cohort = validate_plan(plan_path, repo_root)
    require(os.environ.get("EVALLAB_DOCKER_LOCK_SLOTS") == "1",
            "serial round calibration requires EVALLAB_DOCKER_LOCK_SLOTS=1 on the existing shared FIFO")
    work = work.resolve()
    require(work.is_relative_to(repo_root.resolve() / "runs"), "round work must be in the owned front-door runs tree")
    require(not work.exists() and not out.exists(), "native round work/output must be new; failed attempts are immutable")
    work.mkdir(parents=True)
    write_new(work / "plan-binding.json", {"plan": ref(plan_path)})
    for row in cohort["rows"]:
        selected = execution_plan["graders"].get(row["source"]["task_id"])
        directory = work / "cells" / row["cell_id"]
        start = {"schema": "mimo.hardening-attempt/v1", "plan": ref(plan_path), **cell_identity(row, selected)}
        write_new(directory / "attempt.json", start)
        if selected is None:
            observation = {"source_uid": row["source"]["source_uid"], "v7_outcome": None,
                           "v7_error": "unsupported_task_outside_frozen33", "replayed": False,
                           "native_evidence": {"dispatched": False, "files": {}}}
        else:
            observation = NATIVE["replay_and_collect"](
                row["source"], grader_for(row["source"]["task_id"], selected),
                repo_root=repo_root, work=directory / "native", backend="docker", require_case_execution=True,
            )
        write_new(directory / "observation.json", start | {"observation": observation})
    return report(Path(execution_plan["inventory"]["path"]), out, plan_path=plan_path, work=work, repo_root=repo_root)


def _measurement(document: dict[str, Any], row: dict[str, Any], selected: dict[str, Any] | None,
                 plan_path: Path, directory: Path, repo_root: Path) -> tuple[bool | None, str | None]:
    require(document.get("plan") == ref(plan_path)
            and all(document.get(key) == value for key, value in cell_identity(row, selected).items()),
            "observation belongs to another round/source/grader")
    observation = document["observation"]
    require(observation.get("source_uid") == row["source"]["source_uid"], "observation source UID mismatch")
    evidence = observation["native_evidence"]
    for binding in evidence.get("files", {}).values():
        bound(binding)
    outcome = observation.get("v7_outcome")
    require(outcome is None or type(outcome) is bool, "round observation is not nullable boolean")
    if outcome is None:
        return None, observation.get("v7_error") or "native_outcome_unmeasured"
    require(selected is not None and observation.get("v7_error") is None
            and evidence.get("dispatched") is True and evidence.get("backend") == "docker",
            "measured round row has no actual Docker dispatch")
    require(evidence.get("source_digest_before") == evidence.get("source_digest_after") == row["source"]["source_digest"],
            "native source custody does not match fixed submission")
    checked_source(row["source"])
    receipt_path = Path(evidence["matrix_receipt"]["path"])
    receipt = json.loads(bound(evidence["matrix_receipt"]))
    invocation_path = Path(evidence["invocations"]["path"])
    bound(evidence["invocations"])
    raw = json.loads(bound(evidence["cf_result"]))
    script = bound(evidence["script"])
    execution = SimpleNamespace(receipt_path=receipt_path, invocation_path=invocation_path,
                                results=receipt["results"], exit_code=0)
    proved = NATIVE["_qualify"](raw, execution, evidence["submitted_matrix"],
                                grader_for(row["source"]["task_id"], selected), repo_root.resolve(),
                                directory / "native", script, dict(evidence), backend="docker", require_case_execution=True)
    require(proved is outcome, "saved outcome differs from actual native proof")
    return outcome, None


def summarize(cells: list[dict[str, Any]]) -> dict[str, Any]:
    transitions = Counter()
    gold = [cell for cell in cells if cell["gold_correct"] is not None]
    measured = [cell for cell in cells if cell["outcome"] is not None]
    for cell in cells:
        before, after = cell["before"]["outcome"], cell["outcome"]
        if before is None or after is None:
            transitions["unpaired"] += 1
        else:
            transitions[f"{int(before)}->{int(after)}"] += 1
    gold_measured = [cell for cell in gold if cell["outcome"] is not None]
    paired = len(cells) - transitions["unpaired"]
    return {"expected": len(cells), "measured": len(measured), "passes": sum(cell["outcome"] is True for cell in cells),
            "rejections": sum(cell["outcome"] is False for cell in cells),
            "missing": sum(cell["status"] == "missing" for cell in cells),
            "errors": sum(cell["status"] == "error" for cell in cells),
            "paired": paired,
            "pass_flips_1_to_0": transitions["1->0"] if paired else None,
            "pass_flips_0_to_1": transitions["0->1"] if paired else None,
            "retained_pass": transitions["1->1"] if paired else None,
            "retained_fail": transitions["0->0"] if paired else None,
            "unpaired": transitions["unpaired"], "independent_gold_correct": len(gold),
            "gold_correct_measured": len(gold_measured),
            "gold_correct_rejections": sum(cell["outcome"] is False for cell in gold_measured),
            "gold_correct_rejection_rate": (sum(cell["outcome"] is False for cell in gold_measured) / len(gold_measured)
                                             if gold_measured else None)}


def report(inventory_path: Path, out: Path | None, *, plan_path: Path | None = None, work: Path | None = None,
           repo_root: Path = ROOT) -> dict[str, Any]:
    cohort = validate_inventory(inventory_path)
    execution_plan = None
    previous = {}
    if plan_path is not None:
        execution_plan, _ = validate_plan(plan_path, repo_root)
        require(execution_plan["inventory"] == ref(inventory_path), "report inventory differs from plan")
        if execution_plan.get("previous_report"):
            previous = {cell["cell_id"]: cell for cell in json.loads(bound(execution_plan["previous_report"]))["cells"]}
    require(work is None or execution_plan is not None, "native work requires its exact plan")
    if work is not None:
        require(object_at(work / "plan-binding.json") == {"plan": ref(plan_path)}, "work belongs to another plan")
        require({path.parent.name for path in (work / "cells").glob("*/observation.json")}
                <= {row["cell_id"] for row in cohort["rows"]}, "native work contains unknown source cells")
    cells = []
    measured_native_ids: set[tuple[str, str]] = set()
    for row in cohort["rows"]:
        selected = execution_plan["graders"].get(row["source"]["task_id"]) if execution_plan else None
        before = row["baseline"]
        if execution_plan and previous:
            prior = previous[row["cell_id"]]
            before = {"outcome": prior["outcome"], "error": prior["error"],
                      "origin": "prior_round_actual_native_result", "report": execution_plan["previous_report"]}
        elif execution_plan and not execution_plan["baseline_is_original_v7"]:
            before = {"outcome": None, "error": "exact_predecessor_grade_missing", "origin": "not_imputed"}
        outcome, error, evidence_ref = None, ("qualified_round_missing" if row["target_task_in_frozen33"]
                                             else "unsupported_task_outside_frozen33"), None
        native_uid = None
        status = "missing"
        if execution_plan:
            error = "unsupported_task_outside_frozen33" if selected is None else "target_round_not_executed"
        if work is not None:
            directory = work / "cells" / row["cell_id"]
            observation_path = directory / "observation.json"
            if observation_path.is_file():
                document = object_at(observation_path)
                outcome, error = _measurement(document, row, selected, plan_path, directory, repo_root)
                if outcome is not None:
                    proof = document["observation"]["native_evidence"]
                    native_uid = [REGR["_native_id"](proof["native_job_id"]), REGR["_native_id"](proof["native_trial_id"])]
                    require(tuple(native_uid) not in measured_native_ids, "physical native grade reused for two source cells")
                    measured_native_ids.add(tuple(native_uid))
                evidence_ref = ref(observation_path)
                status = "measured" if outcome is not None else ("missing" if selected is None else "error")
            elif (directory / "attempt.json").is_file():
                error, status = "native_attempt_incomplete_raw_evidence_retained", "error"
                evidence_ref = ref(directory / "attempt.json")
        cells.append({"cell_id": row["cell_id"], "cohort": row["cohort"],
                      "source_uid": row["source"]["source_uid"], "task_id": row["source"]["task_id"],
                      "diff": row["source"]["diff"], "model": row["source"]["requested_model"],
                      "source_error": row["source"].get("native_exception_type"),
                      "before": before, "outcome": outcome, "error": error, "status": status,
                      "gold_correct": row["gold_correct"], "evidence": evidence_ref, "native_uid": native_uid,
                      "target": selected})
    overall = summarize(cells)
    value = {"schema": "mimo.hardening-report/v1", "inventory": ref(inventory_path),
             "plan": ref(plan_path) if plan_path else None,
             "target_manifest": execution_plan["manifest"] if execution_plan else None,
             "round": execution_plan["round"] if execution_plan else None,
             "work": str(work.resolve()) if work else None,
             "status": "measured_complete" if overall["measured"] == len(cells) else "incomplete",
             "overall": overall, "cohorts": {name: summarize([cell for cell in cells if cell["cohort"] == name])
                                                for name in COHORTS},
             "per_task": {task: summarize([cell for cell in cells if cell["task_id"] == task])
                          for task in dict.fromkeys(cohort["ordered33"] + [cell["task_id"] for cell in cells])},
             "fresh_snapshot": cohort["fresh_snapshot"], "cells": cells,
             "scoring_allowed": False, "training_allowed": False, "independent_acceptance": False,
             "limits": ["Missing/error is never a rejection; zero flips with zero measured pairs is not a successful round.",
                        "Gold-correct rejection has only independently adjudicated exact submissions in its denominator.",
                        "Replayed old programs test grader sensitivity; no fresh model-behavior or semantic/honesty claim."]}
    if out is not None:
        write_new(out, value)
    return value


def checked_report(path: Path, repo_root: Path) -> dict[str, Any]:
    value = object_at(path)
    require(value.get("schema") == "mimo.hardening-report/v1", "unsupported hardening report")
    bound(value["inventory"])
    if value.get("plan"):
        bound(value["plan"])
    expected = report(Path(value["inventory"]["path"]), None,
                      plan_path=Path(value["plan"]["path"]) if value.get("plan") else None,
                      work=Path(value["work"]) if value.get("work") else None, repo_root=repo_root)
    require(value == expected, "saved report differs from current exact native evidence and missing dispositions")
    return value


def curve(reports: list[Path], out: Path, repo_root: Path = ROOT) -> dict[str, Any]:
    values = [(path, checked_report(path, repo_root)) for path in reports]
    require(bool(values) and all(value.get("schema") == "mimo.hardening-report/v1" and value.get("round")
                                 for _, value in values), "curve requires actual target-round reports, not readiness reports")
    values.sort(key=lambda item: item[1]["round"]["number"])
    numbers = [value["round"]["number"] for _, value in values]
    require(len(numbers) == len(set(numbers)), "duplicate curve round")
    first = values[0][1]
    for index, (_, value) in enumerate(values):
        require(value["inventory"] == first["inventory"]
                and [(cell["cell_id"], cell["source_uid"], cell["diff"]) for cell in value["cells"]]
                    == [(cell["cell_id"], cell["source_uid"], cell["diff"]) for cell in first["cells"]],
                "curve changed fixed cohort/submission bytes")
        if index:
            require(numbers[index] == numbers[index - 1] + 1
                    and predecessor_accepts(json.loads(bound(value["target_manifest"])),
                                            values[index - 1][1]["target_manifest"]),
                    "curve has a missing/non-successor round")
    value = {"schema": "mimo.hardening-curve/v1", "inventory": first["inventory"],
             "rounds": [{"number": value["round"]["number"], "report": ref(path),
                         "target_manifest": value["target_manifest"], "status": value["status"],
                         "overall": value["overall"], "cohorts": value["cohorts"], "per_task": value["per_task"]}
                        for path, value in values], "scoring_allowed": False, "training_allowed": False,
             "limits": first["limits"]}
    write_new(out, value)
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("inventory", help="Bind all70 plus the complete separate fresh snapshot; no dispatch")
    prepare.add_argument("--canonical-inventory", type=Path, required=True)
    prepare.add_argument("--canonical-labels", type=Path, required=True)
    prepare.add_argument("--fresh-result", type=Path, required=True)
    prepare.add_argument("--gold-labels", type=Path)
    assembly = commands.add_parser("assemble", help="Immutable same33 strict round successor via freeze.py")
    assembly.add_argument("--frozen-source-dir", type=Path, required=True)
    assembly.add_argument("--predecessor", type=Path, required=True)
    assembly.add_argument("--repairs", type=Path, required=True)
    assembly.add_argument("--round-number", type=int, required=True)
    planning = commands.add_parser("plan", help="Admit only exact qualified closed-gate native calibration")
    planning.add_argument("--inventory", type=Path, required=True)
    planning.add_argument("--manifest", type=Path, required=True)
    planning.add_argument("--previous-report", type=Path)
    execute = commands.add_parser("run", help="All cells, serial model-free $0 Docker through the shared native FIFO")
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--work", type=Path, required=True)
    reporting = commands.add_parser("report", help="Actual native proof or one explicit missing/error per source cell")
    reporting.add_argument("--inventory", type=Path, required=True)
    reporting.add_argument("--plan", type=Path)
    reporting.add_argument("--work", type=Path)
    curves = commands.add_parser("curve", help="Combine immutable consecutive same-cohort round reports")
    curves.add_argument("--report", type=Path, action="append", required=True)
    for command in (prepare, assembly, planning, execute, reporting, curves):
        command.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            value = inventory(args.canonical_inventory, args.canonical_labels, args.fresh_result, args.gold_labels)
            write_new(args.out, value)
        elif args.command == "assemble":
            value = FREEZE["assemble_successor"](args.frozen_source_dir, args.out,
                                                 predecessor_manifest=args.predecessor, repairs_handoff=args.repairs,
                                                 round_number=args.round_number, repo_root=args.repo_root)
        elif args.command == "plan":
            value = plan(args.inventory, args.manifest, args.repo_root, args.previous_report)
            write_new(args.out, value)
        elif args.command == "run":
            value = run(args.plan, args.work, args.out, args.repo_root)
        elif args.command == "report":
            value = report(args.inventory, args.out, plan_path=args.plan, work=args.work, repo_root=args.repo_root)
        else:
            value = curve(args.report, args.out, args.repo_root)
        print(json.dumps({"output": ref(args.out), "schema": value["schema"],
                          "summary": value.get("overall", value.get("summary", value.get("counts", value.get("coverage")))),
                          "status": value.get("status"), "scoring_allowed": False, "training_allowed": False}, sort_keys=True))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, f"rounds: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
