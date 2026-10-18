from __future__ import annotations

import csv
import json
import runpy
from copy import deepcopy
from pathlib import Path

import pytest
from test_heldout_v7_campaign import (
    HERE,
)
from test_heldout_v7_campaign import (
    inventory_module as inventory_module,
)
from test_heldout_v7_campaign import (
    no_native_input as no_native_input,
)
from test_heldout_v7_campaign import (
    released_source as released_source,
)
from test_heldout_v7_report import inventory as report_inventory
from test_heldout_v7_report import observation as report_observation
from test_heldout_v7_report import source as report_source

from evallab.registry import compute_task_digests, harbor_task_digest
from evallab.regrade import SpendCapReached
from evallab.task_variants import derive_task


@pytest.fixture
def staged():
    return runpy.run_path(str(HERE / "staged.py"))


@pytest.fixture
def staged_inputs(released_source, inventory_module, staged, tmp_path, monkeypatch):
    """Real isolated package/record/native bytes, never a scientific release fixture."""
    kwargs, _, manifest, config_path = released_source
    config = json.loads(config_path.read_bytes())
    source_path = Path(config["source_files"]["A"]["path"])
    rows = [json.loads(line) for line in source_path.read_text().splitlines()]
    repairs, replacements, pairs = [], [], []
    for cell, entry in zip(rows, manifest["tasks"], strict=True):
        task = "mimo-v2.6-rl/" + entry["task_id"]
        suffix = task.rsplit("-", 1)[-1]
        frozen = deepcopy(entry["evaluation_target"])
        package = Path(frozen["package_path"])
        frozen["verifier_digest"] = compute_task_digests(package).verifier
        entry["frozen_parent"] = frozen
        entry["native_controls"] = {"status": "unit_fixture_not_scientific_acceptance"}
        cell["task"] = str(package)
        pairs.append({"task_id": entry["task_id"], "v6_package_digest": frozen["package_digest"]})
        job = Path(cell["job_dir"])
        metadata = json.loads((job / "lab-metadata.json").read_bytes())
        metadata["task_staging"]["source_package_digest"] = frozen["package_digest"]
        (job / "lab-metadata.json").write_text(json.dumps(metadata))
        lock_path = job / "trial/lock.json"
        lock = json.loads(lock_path.read_bytes())
        lock["task"].update(path=str(package), digest=harbor_task_digest(package))
        lock_path.write_text(json.dumps(lock))
        (job / "trial/config.json").write_text(json.dumps({"job_id": json.loads((job / "result.json").read_bytes())["id"]}))
        if suffix not in staged["REPAIRS"]:
            continue
        record = derive_task(
            package, changes={"tests/test.sh": f"#!/bin/sh\n# isolated staged {suffix}\nexit 2\n".encode()},
            transform="verifier-repair@2", rationale="isolated staged endpoint-selection contract",
            created_by="pytest", repo_root=tmp_path / "repo", records_dir=Path("library/task-variants"),
            variants_root=tmp_path / "variants",
        )
        repaired = tmp_path / "variants" / record.task_slug / record.digest12
        record_path = tmp_path / "repo/library/task-variants" / record.task_slug / f"{record.digest12}.json"
        candidate_path = tmp_path / f"candidate-{suffix}.json"
        candidate = {
            "schema": "envcheck.v7.candidate/v1", "task_id": task, "heldout": True,
            "package_path": str(repaired), "package_digest": record.variant_digest,
            "harbor_digest": record.variant_harbor_digest, "verifier_digest": compute_task_digests(repaired).verifier,
            "native_record": str(record_path), "native_record_sha256": inventory_module["file_ref"](record_path)["sha256"],
            "qualified": False,
        }
        candidate_path.write_text(json.dumps(candidate))
        qualification = tmp_path / f"qualification-{suffix}.json"
        qualification.write_text(json.dumps({"package_digest": record.variant_digest, "checks": [],
                                             "qualified": suffix not in {*staged["REPLACEMENTS"], "002371"}}))
        producer = {**candidate, "candidate": str(candidate_path),
                    "candidate_sha256": inventory_module["file_ref"](candidate_path)["sha256"],
                    "qualification": str(qualification),
                    "qualification_sha256": inventory_module["file_ref"](qualification)["sha256"],
                    "qualified_for_v7": suffix not in {*staged["REPLACEMENTS"], "002371"},
                    "status": "partial" if suffix in {*staged["REPLACEMENTS"], "002371"} else "complete"}
        repairs.append(producer)
        if suffix in staged["REPLACEMENTS"]:
            # Real derived fixture hashes, not fabricated full hashes from production prefixes.
            monkeypatch.setitem(staged["REPLACEMENTS"], suffix, record.digest12)
            replacements.append({"task_id": task, "candidate": str(candidate_path),
                                 "candidate_sha256": producer["candidate_sha256"],
                                 "package_digest": record.variant_digest})
        entry["evaluation_target"] = {**candidate, "lineage": {
            "record_sha256": candidate["native_record_sha256"], "records_newest_first": [{
                "record": inventory_module["file_ref"](record_path), "package_digest": record.variant_digest,
                "harbor_digest": record.variant_harbor_digest, "transform": record.transform,
            }],
        }}
    pair_path = Path(config["package_pairs"]["path"])
    with pair_path.open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=["task_id", "v6_package_digest"])
        writer.writeheader()
        writer.writerows(pairs)
    source_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    config["package_pairs"] = inventory_module["file_ref"](pair_path)
    config["source_files"]["A"] = inventory_module["file_ref"](source_path)
    config_path.write_text(json.dumps(config))
    inventory = inventory_module["inventory"](config_path)
    kwargs["inventory_path"].write_text(json.dumps(inventory))
    manifest.update(scoring_allowed=False, freeze_ready=False, pending_gates=["independent all33 acceptance"])
    kwargs["manifest_path"].write_text(json.dumps(manifest))
    repair_path, scope_path = tmp_path / "repairs.json", tmp_path / "replacement-scope.json"
    repair_path.write_text(json.dumps({"schema": "envcheck.repairs-v7/v1", "repairs": repairs,
                                      "complete_count": 8, "partial_count": 4}))
    scope_path.write_text(json.dumps({"per_task": replacements, "historical_receipts_not_overwritten": []}))
    inputs = {"inventory_path": kwargs["inventory_path"], "manifest_path": kwargs["manifest_path"],
              "repairs_path": repair_path, "replacement_scope": scope_path,
              "rate_evidence": kwargs["rate_evidence"], "usd_per_execution": 0.0666,
              "sandbox_timeout_seconds": 210}
    return inputs, inventory, manifest, kwargs


def test_staged_selects_exact_original33_and_twelve_actual_endpoints(staged_inputs, staged):
    inputs, inventory, _, _ = staged_inputs
    plan = staged["prepare"](**inputs)
    assert set(plan["selected_packages"]) == set(inventory["frozen_task_ids"])
    changed = {task.rsplit("-", 1)[-1] for task, row in plan["selected_packages"].items() if row["changed_from_v6"]}
    assert changed == staged["REPAIRS"]
    assert plan["counts"]["changed_graders"] == 12
    assert plan["counts"]["unchanged_graders"] == 21
    assert plan["counts"]["native_execution_count"] == plan["counts"]["new_cost_reservation_count"] == 12
    for task, selected in plan["selected_packages"].items():
        if task.rsplit("-", 1)[-1] in staged["REPLACEMENTS"]:
            assert selected["package_digest"].startswith("sha256:" + staged["REPLACEMENTS"][task.rsplit("-", 1)[-1]])
        assert len(selected["package_digest"].removeprefix("sha256:")) == 64
        assert selected["harbor_digest"] == harbor_task_digest(Path(selected["package_path"]))
        assert selected["record"]["sha256"] == staged["file_ref"](Path(selected["record"]["path"]))["sha256"]
    partial = next(row for row in plan["caveats"] if row["task_id"].endswith("002371"))
    assert partial["producer_status"] == "partial"
    assert partial["replacement"] is False
    assert plan["canonical_scoring_allowed"] is plan["citable"] is False
    assert plan["budget"]["cap_usd"] == 3
    assert plan["budget"]["combined_trainer_ceiling_usd"] == 12
    assert plan["budget"]["sandbox_timeout_seconds"] == 210
    assert plan["budget"]["modal_sandbox_create_max_attempts"] == 1


@pytest.mark.parametrize("change", ["omit_repair", "wrong_prefix", "duplicate_repair"])
def test_staged_never_silently_narrows_or_changes_selected_endpoints(staged_inputs, staged, change):
    inputs, _, _, _ = staged_inputs
    if change == "wrong_prefix":
        staged["REPLACEMENTS"]["000371"] = "0" * 12
    else:
        repairs = json.loads(inputs["repairs_path"].read_bytes())
        if change == "omit_repair":
            repairs["repairs"].pop()
        else:
            repairs["repairs"].append(deepcopy(repairs["repairs"][0]))
        inputs["repairs_path"].write_text(json.dumps(repairs))
    with pytest.raises(ValueError, match="twelve|authorized prefix"):
        staged["prepare"](**inputs)


def test_plan_freezes_actual_source_and_record_bytes_before_producer_release(staged_inputs, staged, tmp_path):
    inputs, inventory, _, _ = staged_inputs
    path = tmp_path / "staged-plan.json"
    staged["write_plan"](path, **inputs)
    inputs["manifest_path"].write_text('{"producer":"published different later bytes"}')
    inputs["repairs_path"].write_text('{"producer":"published different later bytes"}')
    plan, graders = staged["load_plan"](path, inventory)
    assert set(graders) == set(inventory["frozen_task_ids"])
    assert Path(plan["sources"]["manifest"]["path"]).parent == tmp_path / "staged-plan.sources"
    assert plan["sources"]["manifest"]["origin_path"] == str(inputs["manifest_path"])
    assert plan["admission"]["scoring_allowed"] is False


def test_identical_grader_inherits_actual_measured_reward_not_new_native(staged_inputs, staged):
    inputs, inventory, _, _ = staged_inputs
    plan = staged["prepare"](**inputs)
    row = next(row for row in inventory["rows"] if not plan["selected_packages"][row["task_id"]]["changed_from_v6"])
    target = plan["selected_packages"][row["task_id"]]
    result = staged["inherit"](row, target)
    assert result["v7_outcome"] is False
    assert result["v7_error"] is None
    assert result["replayed"] is False
    assert result["native_evidence"]["dispatched"] is False
    assert result["measurement_origin"] == "inherited_identical_v6_grader"
    assert result["native_evidence"]["identical_package_digest"] == target["package_digest"]
    assert result["native_evidence"]["measured_original_reward"] == 0
    different = staged["inherit"](row, {**target, "package_digest": "sha256:" + "0" * 64})
    assert different["v7_outcome"] is None
    assert different["v7_error"] == "original_and_selected_grader_not_identical"
    contradictory = staged["inherit"]({**row, "v6_outcome": True}, target)
    assert contradictory["v7_outcome"] is None
    assert contradictory["v7_error"] == "original_native_reward_does_not_prove_canonical_outcome"


@pytest.mark.parametrize("error", [None, "proxy_usage_reconciliation", "native_timeout"])
def test_null_or_error_source_remains_unmeasured_even_with_raw_pass(staged_inputs, staged, error):
    inputs, inventory, _, _ = staged_inputs
    plan = staged["prepare"](**inputs)
    row = next(row for row in inventory["rows"] if not plan["selected_packages"][row["task_id"]]["changed_from_v6"])
    result_path = Path(row["source_trial_dir"]) / "result.json"
    raw = json.loads(result_path.read_bytes())
    raw["verifier_result"]["rewards"]["reward"] = 1.0
    result_path.write_text(json.dumps(raw))
    result = staged["inherit"]({**row, "v6_outcome": None, "v6_error": error}, plan["selected_packages"][row["task_id"]])
    assert result["v7_outcome"] is None
    assert result["v7_error"] == (error or "canonical_outcome_unmeasured")
    assert result["replayed"] is False


def test_canonical_run_and_promotion_stay_closed_for_staged_manifest(staged_inputs, staged):
    inputs, inventory, _, kwargs = staged_inputs
    campaign = runpy.run_path(str(HERE / "campaign.py"))
    digest = staged["file_ref"](inputs["manifest_path"])["sha256"]
    with pytest.raises(ValueError, match="producer release"):
        campaign["admit"](inventory, inputs["manifest_path"], digest, kwargs["verifier_receipt"])
    with pytest.raises(ValueError, match="producer release"):
        staged["promote"](inventory_path=inputs["inventory_path"], manifest_path=inputs["manifest_path"],
                           accepted_sha=digest, verifier_receipt=kwargs["verifier_receipt"],
                           work=kwargs["work"], output=kwargs["output"])
    assert not kwargs["work"].exists()
    assert not kwargs["output"].exists()


def test_staged_report_keeps_every_attempt_pass_nonpass_and_null(staged):
    report_module = runpy.run_path(str(HERE / "report.py"))
    rows = [report_source(arm, original=arm != "C") for arm in ("base", "B", "C")]
    rows += [report_source("B", attempt=2), report_source("A", original=None, error="original_unmeasured")]
    inventory = report_inventory(rows)
    observations = [report_observation(row, None if row["arm"] == "A" else row["arm"] != "C",
                                       error="original_unmeasured" if row["arm"] == "A" else None,
                                       replayed=row["arm"] != "base") for row in rows]
    observations[0]["measurement_origin"] = "inherited_identical_v6_grader"
    admission = {"schema": "heldout-v7.staged-admission/v1", **staged["FLAGS"],
                 "frozen_task_ids": inventory["frozen_task_ids"],
                 "graders": dict.fromkeys(inventory["frozen_task_ids"], {})}
    report = report_module["build_report"](inventory, observations, admission, replicates=31)
    assert len(report["source_results"]) == len(rows)
    assert len(report["per_pass_labels"]) == 3
    assert report["canonical_scoring_allowed"] is report["scoring_allowed"] is report["citable"] is False
    assert report["arms"]["base"]["coverage"]["inherited_identical_grader_sources"] == 1
    assert report["arms"]["base"]["coverage"]["replayed_native_sources"] == 0
    assert report["arms"]["base"]["coverage"]["qualified_v7_sources"] == 0
    assert report["arms"]["A"]["v7"]["denominator"] == 0
    assert report["primary"]["paired"]["B_vs_C"]["matched_attempt_ids"] == [1]
    assert report["secondary"]["v7"]["B_full_two_attempts"]["role"].startswith("secondary")
    assert all(pair["cohort_name"] == report["primary"]["cohort"]["name"] for pair in report["primary"]["paired"].values())


def test_staged_and_final_changed_grader_share_one_cumulative_journal(staged, tmp_path):
    campaign = runpy.run_path(str(HERE / "campaign.py"))
    journal = tmp_path / "classifiers-v7-reservations.jsonl"
    gate = campaign["JournalGate"](journal, 1)
    uid = ("job", "trial")
    gate.context.source_uid = uid
    gate.context.grader_digest = "sha256:" + "a" * 64
    gate.charge()
    restarted = campaign["JournalGate"](journal, 1)
    assert restarted.is_reserved(uid, "sha256:" + "a" * 64)
    restarted.context.source_uid, restarted.context.grader_digest = uid, "sha256:" + "a" * 64
    with pytest.raises(SpendCapReached, match="already reserved"):
        restarted.charge()
    restarted.context.grader_digest = "sha256:" + "b" * 64
    restarted.charge()
    assert restarted.to_json()["reserved_usd"] == 2
    assert restarted.executions == 2
    assert not restarted.is_reserved(uid, "sha256:" + "c" * 64)
    entries = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [entry["execution"] for entry in entries] == [1, 2]
    assert [entry["grader_digest"] for entry in entries] == ["sha256:" + "a" * 64, "sha256:" + "b" * 64]


def test_promotion_reuses_identical_custody_without_retrying_null_grades(staged_inputs, staged, tmp_path):
    inputs, inventory, manifest, kwargs = staged_inputs
    plan_path = tmp_path / "plan.json"
    staged["write_plan"](plan_path, **inputs)
    plan, _ = staged["load_plan"](plan_path, inventory)
    campaign = runpy.run_path(str(HERE / "campaign.py"))
    work = tmp_path / "retained-staged"
    campaign["create_json"](work / "campaign.json", {"inventory": campaign["file_ref"](inputs["inventory_path"])})
    original = {}
    for index, source in enumerate(inventory["rows"]):
        target = plan["selected_packages"][source["task_id"]]
        observation = staged["inherit"](source, target) if not target["changed_from_v6"] else {
            "source_uid": source["source_uid"], "v7_outcome": None, "v7_error": "retained_native_timeout",
            "replayed": False, "native_evidence": {"files": {}, "dispatched": False},
        }
        observation["measurement_binding"] = staged["measurement_binding"](source, target)
        original[tuple(source["source_uid"])] = deepcopy(observation)
        case = work / "cases" / str(index)
        reference = campaign["create_json"](case / "observation.json", observation)
        campaign["create_json"](case / "observation-ref.json", reference)
    manifest.update(scoring_allowed=True, freeze_ready=True, pending_gates=[], independent_verifier={"status": "pass"})
    inputs["manifest_path"].write_text(json.dumps(manifest))
    output = tmp_path / "canonical.json"
    result = staged["promote"](
        inventory_path=inputs["inventory_path"], manifest_path=inputs["manifest_path"],
        accepted_sha=campaign["file_ref"](inputs["manifest_path"])["sha256"],
        verifier_receipt=kwargs["verifier_receipt"], work=work, output=output,
    )
    report = json.loads(Path(result["report"]["path"]).read_bytes())
    assert len(report["source_results"]) == len(inventory["rows"])
    for source in report["source_results"]:
        prior = original[tuple(source["source_uid"])]
        assert source["v7_outcome"] is prior["v7_outcome"]
        assert source["v7_error"] == prior["v7_error"]
        assert source["native_evidence"] == prior["native_evidence"]
        assert source["observation"]["measurement_binding"] == prior["measurement_binding"]
        assert source["observation"]["reuse"]["new_native_executions"] == 0
    assert report["promotion"]["new_reservations"] == report["promotion"]["new_native_executions"] == 0
    assert not Path(plan["budget"]["journal"]).exists()


def test_changed_package_or_diff_cannot_reuse_a_staged_grade(staged_inputs, staged):
    inputs, inventory, _, _ = staged_inputs
    plan = staged["prepare"](**inputs)
    source = inventory["rows"][0]
    target = plan["selected_packages"][source["task_id"]]
    observation = {"measurement_binding": staged["measurement_binding"](source, target)}
    assert staged["same_measurement"](source, observation, target)
    assert not staged["same_measurement"](source, observation, {**target, "package_digest": "sha256:" + "a" * 64})
    assert not staged["same_measurement"]({**source, "diff": None}, observation, target)


def test_measured_original_reward_and_agent_exception_remain_distinct(staged_inputs, staged):
    inputs, inventory, _, _ = staged_inputs
    plan = staged["prepare"](**inputs)
    source = deepcopy(next(row for row in inventory["rows"] if not plan["selected_packages"][row["task_id"]]["changed_from_v6"]))
    trial = Path(source["source_trial_dir"])
    path = trial / "result.json"
    result = json.loads(path.read_bytes())
    result["exception_info"] = {"exception_type": "AgentTimeoutError", "exception_message": "agent stopped; verifier subsequently measured reward"}
    path.write_text(json.dumps(result))
    for group in source["native_artifacts"].values():
        for name, reference in group.items():
            if reference is not None and Path(reference["path"]) == path:
                group[name] = staged["file_ref"](path)
    source["source_digest"] = staged["cf"].source_digest(trial)
    observation = staged["inherit"](source, plan["selected_packages"][source["task_id"]])
    assert observation["v7_outcome"] is False
    assert observation["v7_error"] is None
    assert observation["native_evidence"]["native_exception_info"] == result["exception_info"]
    assert observation["replayed"] is False


def test_legacy_reservation_cannot_be_reassigned_to_a_new_grader(tmp_path):
    campaign = runpy.run_path(str(HERE / "campaign.py"))
    path = tmp_path / "journal.jsonl"
    legacy = campaign["JournalGate"](path, 1)
    legacy.context.source_uid = ["job", "trial"]
    legacy.charge()
    current = campaign["JournalGate"](path, 1)
    current.context.source_uid = ["job", "trial"]
    current.context.grader_digest = "sha256:" + "a" * 64
    with pytest.raises(SpendCapReached, match="already reserved"):
        current.charge()
    assert current.executions == 1
