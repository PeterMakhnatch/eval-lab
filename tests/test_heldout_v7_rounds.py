"""Deterministic round/cohort boundaries; synthetic receipts are never native runs."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def rounds():
    pytest.importorskip("harbor.job_diff", reason="requires the pinned Harbor native profile")
    path = ROOT / "research/experiments/heldout-v7/rounds.py"
    spec = importlib.util.spec_from_file_location("heldout_v7_rounds", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def document(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


@pytest.fixture
def cohort(rounds, tmp_path, monkeypatch):
    """Exercise custody joins; campaign admission itself has real-package tests."""
    frozen = json.loads((ROOT / "research/experiments/mimo-training-20261016/heldout.json").read_text())["heldout"]
    tasks = [rounds.REGR["canonical_task"](task) for task in frozen]
    model = rounds.REGR["MODEL"]
    sources, labels = [], []
    patch = "diff --git a/code.py b/code.py\n--- a/code.py\n+++ b/code.py\n@@ -1 +1 @@\n-0\n+1\n"
    for index in range(70):
        source_uid = [str(UUID(int=index * 2 + 1)), str(UUID(int=index * 2 + 2))]
        trial = tmp_path / "raw" / source_uid[0] / source_uid[1]
        document(trial / "result.json", {"id": source_uid[1], "task_name": tasks[index % 33],
                 "config": {"job_id": source_uid[0], "agent": {"model_name": model}}})
        kept = trial / "verifier/agent.kept.diff"
        kept.parent.mkdir()
        kept.write_text(patch)
        source = {"source_uid": source_uid, "source_trial_dir": str(trial), "task_id": tasks[index % 33],
                  "v6_outcome": True, "v6_error": None, "diff": rounds.ref(kept),
                  "source_digest": rounds.cf.source_digest(trial), "requested_model": model,
                  "native_exception_type": "AgentTimeoutError" if index == 0 else None,
                  "arm": "base", "attempt_index": 1, "population": "official"}
        sources.append(source)
        labels.append({"source_uid": source_uid, "source": source, "task_id": source["task_id"],
                       "original_pass": True, "v7_outcome": index >= 6,
                       "label": "real_pass" if index >= 6 else "wrong_pass"})
    source_inventory = document(tmp_path / "canonical-inventory.json", {
        "schema": "heldout-v7.inventory/v1", "frozen_task_ids": sorted(tasks), "rows": sources,
    })
    baseline = document(tmp_path / "released-v7.json", {
        "schema": "mimo.heldout/v1", "successor_version": "v7", "status": "released", "heldout": frozen,
        "scoring_allowed": True, "freeze_ready": True, "pending_gates": [], "independent_verifier": {"status": "pass"},
    })
    receipt = document(tmp_path / "baseline-receipt.json", {"status": "pass"})
    admission = {"manifest": rounds.ref(baseline), "verifier_receipt": rounds.ref(receipt),
                 "operator_attested_verifier_accepted_sha256": rounds.ref(baseline)["sha256"]}

    def admitted(inventory, manifest, accepted_sha, verifier):
        assert inventory["frozen_task_ids"] == sorted(tasks)
        assert manifest == baseline and verifier == receipt and accepted_sha == admission["manifest"]["sha256"]
        return admission, {}

    monkeypatch.setitem(rounds.CAMPAIGN, "admit", admitted)
    canonical_labels = document(tmp_path / "labels.json", {
        "schema": "heldout-v7.pass-labels/v1", "labels": labels,
        "canonical_scoring_allowed": True, "admission": admission,
    })
    fresh_task = "mimo-v2.6-rl/format-code-task-009999"
    package = tmp_path / "fresh-package"
    for relative, text in {"task.toml": f'[task]\nname = "{fresh_task}"\n',
                           "environment/Dockerfile": "FROM ubuntu:24.04\n",
                           "tests/test.sh": "#!/bin/sh\nexit 0\n", "solution/solve.sh": "#!/bin/sh\nexit 0\n"}.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    digests = rounds.NATIVE["compute_task_digests"](package)
    record = document(tmp_path / "fresh-record.json", {
        "schema": "evallab.task_variant/v1", "task_name": fresh_task, "variant_digest": digests.package,
        "variant_harbor_digest": rounds.NATIVE["harbor_task_digest"](package),
        "parent": {"digest": "sha256:" + "0" * 64, "harbor_digest": "sha256:" + "1" * 64,
                   "source": {"kind": "local", "path": str(tmp_path / "synthetic-parent")}},
        "transform": "verifier-repair@1", "components_changed": ["verifier"],
        "files": [{"path": "tests/test.sh", "before_sha256": None,
                   "after_sha256": rounds.ref(package / "tests/test.sh")["sha256"],
                   "content": (package / "tests/test.sh").read_text()}],
        "rationale": "Synthetic consumer custody regression, not a qualified native release.",
        "created_by": "unit-test", "created_at": "2026-10-19T00:00:00Z", "status": "candidate",
    })
    task_row = {"task": fresh_task, "package_path": str(package), "package_digest": digests.package,
                "verifier_digest": digests.verifier, "harbor_digest": rounds.NATIVE["harbor_task_digest"](package),
                "record": rounds.ref(record)}
    fresh_uid = [str(UUID(int=1001)), str(UUID(int=1002))]
    trial = tmp_path / "fresh-job" / "trial"
    document(trial / "result.json", {"id": fresh_uid[1], "config": {"job_id": fresh_uid[0], "agent": {"model_name": model}},
             "finished_at": "2026-10-19T06:28:24Z", "exception_info": {"exception_type": "AgentTimeoutError"},
             "verifier_result": {"rewards": {"reward": 1.0}}})
    document(trial / "config.json", {"job_id": fresh_uid[0]})
    document(trial / "setup-fingerprint.json", {"declared_configuration_not_remote_attestation": True})
    document(trial.parent / "lab-metadata.json", {"exit_code": 0, "timed_out": False})
    for name in ("agent.kept.diff", "agent.full.diff"):
        path = trial / "verifier" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(patch)
    planned = [{"cell_id": f"009999-k{attempt}", "task_id": "009999", "task": fresh_task, "attempt_index": attempt}
               for attempt in (1, 2)]
    fresh_plan = document(tmp_path / "fresh-plan.json", {
        "schema": "mimo.phase2-native-plan/v1", "cells": planned, "tasks": [task_row],
        "model": model, "returned_model_required": model.removeprefix("selfhosted/"),
    })
    census_rows = [{"cell_id": planned[0]["cell_id"], "task_id": "009999", "attempt_index": 1,
                    "native_repaired_reward": 1.0, "native_job_id": fresh_uid[0], "execution_id": fresh_uid[1],
                    "trial_path": str(trial), "exception_type": "AgentTimeoutError", "problems": [],
                    "task_staging": {"declared_task_name": fresh_task, "source_package_digest": digests.package},
                    "model_identity": {"matched": True, "requested": model, "returned": model.removeprefix("selfhosted/")},
                    "kept_diff_sha256": rounds.ref(trial / "verifier/agent.kept.diff")["sha256"],
                    "full_diff_sha256": rounds.ref(trial / "verifier/agent.full.diff")["sha256"],
                    "verifier_rewards": {"reward": 1.0}, "status": "native_error_or_incomplete"},
                   {"cell_id": planned[1]["cell_id"], "task_id": "009999", "attempt_index": 2,
                    "native_repaired_reward": None, "status": "not_dispatched"}]
    census_path = tmp_path / "fresh-census.jsonl"
    census_path.write_text("".join(json.dumps(row) + "\n" for row in census_rows))
    fresh_labels = document(tmp_path / "fresh-labels.json", {"schema": "mimo.window3-native-pass-labels/v1", "labels": [
        {"cell_id": planned[0]["cell_id"], "label": "real_pass", "repaired_reward": 1.0, "evidence": "producer inspection"},
    ]})
    serving = document(tmp_path / "serving.json", {"fixture": "declared model config"})
    fresh_result = document(tmp_path / "fresh-result.json", {
        "schema": "mimo.window3-native-result/v1", "plan": rounds.ref(fresh_plan),
        "custody": {"census": rounds.ref(census_path), "pass_labels": rounds.ref(fresh_labels)},
        "serving_receipt": rounds.ref(serving), "prepared_live": rounds.ref(serving),
        "census": {"planned_cells": 2, "dispositions": 2, "actual_native_trial_endpoints": 1},
        "semantic_labels": {"adjudicated_reward1_candidates": 1}, "reservation_id": "synthetic-window3",
        "status": "blocked", "training": {"training_admitted": False},
    })
    return {"sources": source_inventory, "labels": canonical_labels, "fresh": fresh_result,
            "census": census_path, "census_rows": census_rows, "fresh_trial": trial, "native_sources": sources}


def prepare(rounds, cohort, path):
    value = rounds.inventory(cohort["sources"], cohort["labels"], cohort["fresh"])
    rounds.write_new(path, value)
    return value


def test_fixed70_distinct_uuid_not_distinct_program_and_separate_timed_out_fresh(rounds, cohort, tmp_path):
    value = prepare(rounds, cohort, tmp_path / "inventory.json")
    assert value["summary"]["canonical70"] == 70 and value["summary"]["total_cells"] == 71
    assert len({tuple(row["source"]["source_uid"]) for row in value["rows"]}) == 71
    assert len({row["source"]["diff"]["sha256"] for row in value["rows"]}) == 1
    assert value["summary"]["independent_gold_correct"] == value["summary"]["fresh_supported_by_frozen33"] == 0
    assert value["fresh_snapshot"]["snapshot_complete"] is True
    assert value["fresh_snapshot"]["planned_dispositions"] == 2
    assert value["fresh_snapshot"]["published_pass_candidates"] == 1
    assert value["fresh_snapshot"]["native_clean_pass_candidates"] == 0
    assert value["rows"][-1]["source"]["native_exception_type"] == "AgentTimeoutError"
    assert value["rows"][-1]["baseline"]["outcome"] is None
    assert all(row["gold_correct"] is None for row in value["rows"])


def test_readiness_has_every_cell_missing_not_empty_success_or_false_repair(rounds, cohort, tmp_path):
    inventory = tmp_path / "inventory.json"
    prepare(rounds, cohort, inventory)
    value = rounds.report(inventory, tmp_path / "report.json")
    assert value["round"] is None and value["target_manifest"] is None and value["status"] == "incomplete"
    assert value["overall"]["measured"] == value["overall"]["passes"] == value["overall"]["rejections"] == 0
    assert value["overall"]["missing"] == value["overall"]["unpaired"] == len(value["cells"]) == 71
    assert value["cells"][-1]["error"] == "unsupported_task_outside_frozen33"
    assert value["overall"]["gold_correct_rejection_rate"] is None
    assert sum(cell["before"]["outcome"] is True for cell in value["cells"]) == 64
    assert rounds.checked_report(tmp_path / "report.json", ROOT) == value
    with pytest.raises(ValueError, match="actual target-round reports"):
        rounds.curve([tmp_path / "report.json"], tmp_path / "curve.json")


def test_incomplete_fresh_snapshot_is_not_silently_a_smaller_cohort(rounds, cohort):
    cohort["census"].write_text(json.dumps(cohort["census_rows"][0]) + "\n")
    result = json.loads(cohort["fresh"].read_bytes())
    result["custody"]["census"] = rounds.ref(cohort["census"])
    document(cohort["fresh"], result)
    with pytest.raises(ValueError, match="omits/duplicates"):
        rounds.inventory(cohort["sources"], cohort["labels"], cohort["fresh"])


def test_fresh_kept_diff_is_bound_even_though_legacy_source_digest_omits_it(rounds, cohort, tmp_path):
    inventory = tmp_path / "inventory.json"
    prepare(rounds, cohort, inventory)
    patch = cohort["fresh_trial"] / "verifier/agent.kept.diff"
    before = rounds.cf.source_digest(cohort["fresh_trial"])
    patch.write_text(patch.read_text() + "changed retained submission\n")
    assert rounds.cf.source_digest(cohort["fresh_trial"]) == before
    with pytest.raises(ValueError, match="digest changed"):
        rounds.validate_inventory(inventory)


def test_flip_counts_are_paired_and_gold_denominator_is_explicit(rounds):
    cells = [{"before": {"outcome": before}, "outcome": after, "gold_correct": None, "status": "measured"}
             for before, after in ((True, False), (False, True), (True, True), (False, False))]
    cells.append({"before": {"outcome": True}, "outcome": None, "gold_correct": None, "status": "error"})
    summary = rounds.summarize(cells)
    assert all(summary[key] == 1 for key in ("pass_flips_1_to_0", "pass_flips_0_to_1", "retained_pass", "retained_fail", "unpaired", "errors"))
    assert summary["gold_correct_measured"] == summary["gold_correct_rejections"] == 0
    assert summary["gold_correct_rejection_rate"] is None
    cells[0]["gold_correct"] = {"path": "independent exact-submission adjudication", "sha256": "bound-in-inventory"}
    summary = rounds.summarize(cells)
    assert summary["gold_correct_measured"] == summary["gold_correct_rejections"] == 1
    assert summary["gold_correct_rejection_rate"] == 1.0


def test_pending_manifest_does_not_authorize_round_calibration(rounds, tmp_path):
    candidate = document(tmp_path / "candidate.json", {"schema": "mimo.heldout/v1", "status": "candidate", "scoring_allowed": False})
    with pytest.raises(ValueError, match="pending manifest alone"):
        rounds.validate_round(candidate, ROOT)


def test_native_run_requires_single_shared_fifo_slot_before_creating_work(rounds, monkeypatch, tmp_path):
    monkeypatch.setattr(rounds, "validate_plan", lambda *_: ({}, {}))
    monkeypatch.setenv("EVALLAB_DOCKER_LOCK_SLOTS", "4")
    work = tmp_path / "runs" / "round"
    with pytest.raises(ValueError, match="EVALLAB_DOCKER_LOCK_SLOTS=1"):
        rounds.run(tmp_path / "plan.json", work, tmp_path / "report.json", tmp_path)
    assert not work.exists()


def test_duplicate_physical_pass_is_not_silently_deduplicated(rounds, cohort):
    source = json.loads(cohort["sources"].read_bytes())
    source["rows"].append(source["rows"][0])
    document(cohort["sources"], source)
    with pytest.raises(ValueError, match="exactly70 distinct physical"):
        rounds.inventory(cohort["sources"], cohort["labels"], cohort["fresh"])


def test_original_release_cannot_reorder_the_frozen33(rounds, cohort):
    labels = json.loads(cohort["labels"].read_bytes())
    manifest_path = Path(labels["admission"]["manifest"]["path"])
    manifest = json.loads(manifest_path.read_bytes())
    manifest["heldout"][0], manifest["heldout"][1] = manifest["heldout"][1], manifest["heldout"][0]
    document(manifest_path, manifest)
    labels["admission"]["manifest"] = rounds.ref(manifest_path)
    document(cohort["labels"], labels)
    with pytest.raises(ValueError, match="frozen33 order"):
        rounds.inventory(cohort["sources"], cohort["labels"], cohort["fresh"])


@pytest.mark.parametrize("change", [None, "not_independent", "other_task", "other_patch", "other_source"])
def test_correct_route_retention_is_not_gold_for_another_submission(rounds, cohort, tmp_path, change):
    source = cohort["native_sources"][0]
    adjudication = {
        "schema": "mimo.independent-correct/v1", "independent": True, "verdict": "correct",
        "reviewer": "independent unit-test reviewer", "source_uid": source["source_uid"],
        "task_id": source["task_id"], "diff_sha256": source["diff"]["sha256"],
    }
    if change == "not_independent":
        adjudication["independent"] = False
    elif change == "other_task":
        adjudication["task_id"] = cohort["native_sources"][1]["task_id"]
    elif change == "other_patch":
        adjudication["diff_sha256"] = "sha256:" + "0" * 64
    elif change == "other_source":
        adjudication["source_uid"] = cohort["native_sources"][1]["source_uid"]
    verdict = document(tmp_path / "independent-correct.json", adjudication)
    gold = document(tmp_path / "gold.json", {"schema": "mimo.hardening-gold/v1", "labels": [
        {"source_uid": source["source_uid"], "independent_adjudication": rounds.ref(verdict)},
    ]})
    if change is not None:
        with pytest.raises(ValueError, match="exact task/submission/physical source"):
            rounds.inventory(cohort["sources"], cohort["labels"], cohort["fresh"], gold)
    else:
        value = rounds.inventory(cohort["sources"], cohort["labels"], cohort["fresh"], gold)
        assert value["summary"]["independent_gold_correct"] == 1
        assert value["rows"][0]["gold_correct"] == rounds.ref(verdict)
        assert value["rows"][1]["gold_correct"] is None


def test_run_visits_all70_even_retained_graders_and_keeps_out_of_scope_cell(rounds, cohort, monkeypatch, tmp_path):
    inventory = tmp_path / "inventory.json"
    value = prepare(rounds, cohort, inventory)
    synthetic_plan = document(tmp_path / "loop-only-plan.json", {"fixture": "cell-loop unit test, not an admitted round"})
    execution_plan = {"inventory": rounds.ref(inventory), "graders": {
        task: {"retained_grader": True} for task in value["ordered33"],
    }}
    monkeypatch.setattr(rounds, "validate_plan", lambda *_: (execution_plan, value))
    monkeypatch.setattr(rounds, "grader_for", lambda task, selected: (task, selected))
    monkeypatch.setenv("EVALLAB_DOCKER_LOCK_SLOTS", "1")
    calls = []

    def intake(source, target, **kwargs):
        assert target[1]["retained_grader"] is True
        assert kwargs["backend"] == "docker" and kwargs["require_case_execution"] is True
        calls.append(source["source_uid"])
        return {"source_uid": source["source_uid"], "v7_outcome": None,
                "v7_error": "synthetic_test_no_native_execution", "replayed": False, "native_evidence": {}}

    monkeypatch.setitem(rounds.NATIVE, "replay_and_collect", intake)

    def recovered(_inventory, _out, *, plan_path, work, repo_root):
        assert plan_path == synthetic_plan and repo_root == tmp_path
        saved = [json.loads(path.read_bytes()) for path in (work / "cells").glob("*/observation.json")]
        assert len(saved) == 71
        assert sum(row["observation"]["v7_error"] == "unsupported_task_outside_frozen33" for row in saved) == 1
        return {"cells": len(saved)}

    monkeypatch.setattr(rounds, "report", recovered)
    result = rounds.run(synthetic_plan, tmp_path / "runs/round", tmp_path / "round-report.json", tmp_path)
    assert result["cells"] == 71
    assert calls == [source["source_uid"] for source in cohort["native_sources"]]
