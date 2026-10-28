"""Synthetic route-local continuation custody; no benchmark or native calls."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import test_oq6_task_quality_scheduler as helpers

scheduler = helpers.scheduler
SONNET, _, KIMI = scheduler.ROUTES


def _role(directory: Path):
    receipt = directory / "receipt.json"
    files = [path for path in directory.rglob("*") if path.is_file() and path != receipt]
    return {
        "directory": str(directory),
        "consumed": receipt.exists() or any((directory / name).exists() for name in helpers.Native.OWNED_ARTIFACTS),
        "receipt_path": str(receipt) if receipt.exists() else None,
        "receipt_sha256": scheduler.sha(receipt.read_bytes()) if receipt.exists() else None,
        "status": scheduler.read_json(receipt)["status"] if receipt.exists() else None,
        "owned_files": [{"path": str(path), "sha256": scheduler.sha(path.read_bytes())} for path in files],
    }


def _continuation_lab(tmp_path, *, action="prior_terminal", seed=True, stage="dev", unavailable=False):
    cases = [
        helpers.case("D1", stage=stage, prepared=not unavailable),
        helpers.case("D2", stage=stage, keys=0, priority=1),
    ]
    lab = helpers.Lab(tmp_path, cases=cases)
    parent_root = tmp_path / "closed-parent"
    original_plan = copy.deepcopy(lab.plan)
    audit_path = parent_root / "calls" / SONNET.replace("/", "--") / f"A/{stage}/D1/audit"
    lab.native.status["audit"] = "answered" if action == "resume_scorer" else "unanswered"
    lab.native.findings = [{"description": "synthetic finding"}]
    if not unavailable:
        lab.native.call(audit_path, route=SONNET, prompt="frozen auditor A", system="frozen auditor system",
                        expected_array="findings", config=Path("synthetic-config"),
                        config_sha256="config-hash", timeout_seconds=600)
    lab.native.status.clear()
    lab.native.sent.clear()
    snapshot = {"schema": "oq6.prior-consumption-snapshot/v1", "parent_root": str(parent_root),
                "stage": stage, "captured_at": scheduler.utc(900), "workers": {}}
    for worker in lab.plan["workers"]:
        arm = worker["arm"]
        records = {}
        for case in cases:
            cid = case["case_id"]
            base = parent_root / "calls" / SONNET.replace("/", "--") / arm / stage / cid
            previous_action = "unavailable_input" if unavailable and cid == "D1" else (
                action if (arm, cid) == ("A", "D1") else "audit")
            records[cid] = {"action": previous_action,
                            "audit": _role(base / "audit"), "scorer": _role(base / "scorer"),
                            "cell_path": None, "cell_sha256": None,
                            "prediction_path": None, "prediction_sha256": None}
        snapshot["workers"][scheduler.worker_key(SONNET, arm)] = {"route": SONNET, "arm": arm, "cases": records}
        skipped = ["D1"] if unavailable or arm == "A" and action == "prior_terminal" else []
        worker["case_ids"] = [case["case_id"] for case in cases if case["case_id"] not in skipped]
        worker["prior_case_ids"] = skipped
        worker["resume_scorer_case_ids"] = ["D1"] if arm == "A" and action == "resume_scorer" and not unavailable else []
    parent_protocol_path, parent_plan_path = tmp_path / "closed-protocol.json", tmp_path / "closed-plan.json"
    scheduler.write_json(parent_protocol_path, {"models": [SONNET]})
    scheduler.write_json(parent_plan_path, original_plan)
    snapshot["parent_protocol_sha256"] = scheduler.sha(parent_protocol_path.read_bytes())
    snapshot["parent_plan_sha256"] = scheduler.sha(parent_plan_path.read_bytes())
    snapshot_path = tmp_path / "prior-consumption.json"
    scheduler.write_json(snapshot_path, snapshot)
    lab.plan["continuation"] = {
        "schema": "oq6.stage-continuation-plan/v1", "stage": stage, "parent_root": str(parent_root),
        "parent_protocol_path": str(parent_protocol_path),
        "parent_protocol_sha256": snapshot["parent_protocol_sha256"],
        "parent_plan_path": str(parent_plan_path), "parent_plan_sha256": snapshot["parent_plan_sha256"],
        "prior_snapshot_path": str(snapshot_path), "prior_snapshot_sha256": scheduler.sha(snapshot_path.read_bytes()),
    }
    if action == "resume_scorer" and seed and not unavailable:
        cached = lab.root / "calls" / SONNET.replace("/", "--") / f"A/{stage}/D1/audit"
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.symlink_to(audit_path, target_is_directory=True)
    for key, state in scheduler.initial_states(lab.protocol, lab.plan, helpers.FREEZE, helpers.PLAN, now=lab.clock()).items():
        scheduler.write_json(lab.root / "pending" / (key + ".json"), state)
    lab.cursor(SONNET, "B", len(lab.plan["workers"][1]["case_ids"]))
    lab.worker = lab.make_worker()
    return lab, snapshot, parent_root


def _call(worker, directory, *, role="audit"):
    return worker.quota_call(directory, route=SONNET,
                             prompt="frozen auditor A" if role == "audit" else "frozen scorer",
                             system="frozen auditor system" if role == "audit" else "frozen scorer system",
                             expected_array="findings" if role == "audit" else "judgments",
                             config=Path("synthetic-config"), config_sha256="config-hash", timeout_seconds=600)


def test_continuation_keeps_unequal_arms_and_every_original_case(tmp_path):
    lab, _, _ = _continuation_lab(tmp_path)
    cases, workers = scheduler.validate_plan(lab.protocol, lab.plan)
    assert set(cases) == {"D1", "D2"}
    assert workers[scheduler.worker_key(SONNET, "A")]["case_ids"] == ["D2"]
    assert workers[scheduler.worker_key(SONNET, "B")]["case_ids"] == ["D1", "D2"]
    assert lab.worker._states()[lab.worker.key]["next_case_id"] == "D2"
    summary = lab.worker.run()
    assert summary["planned_cells"] == summary["complete_paired_cells"] == 1
    assert len(lab.native.sent) == 2  # One new audit and its scorer, not prior D1.
    assert all("D2" in str(row["directory"]) for row in lab.native.sent)


def test_route_local_unkeyed_work_is_not_idled_by_another_route_keyed_queue(tmp_path):
    sonnet = helpers.Lab(tmp_path, cases=[helpers.case("U1", stage="dev", keys=0)])
    kimi = helpers.Lab(tmp_path, cases=[helpers.case("D1", stage="dev")], routes=(KIMI,), route=KIMI)
    kimi.cursor(KIMI, "A", 0, status="quota_pending", blocked_until=scheduler.utc(1500),
                blocked_route=KIMI, wait_basis="provider_reset")
    assert sonnet.worker.run()["complete_paired_cells"] == 1
    assert len(sonnet.native.sent) == 1
    assert kimi.worker._states()[kimi.worker.key]["status"] == "quota_pending"
    assert not kimi.native.sent


@pytest.mark.parametrize("mutation", ["unrepresented", "duplicate", "cross", "source"])
def test_continuation_rejects_scope_or_source_relabeling(tmp_path, mutation):
    lab, _, _ = _continuation_lab(tmp_path)
    if mutation == "unrepresented":
        lab.plan["workers"][0]["prior_case_ids"] = []
    elif mutation == "duplicate":
        lab.plan["workers"][0]["prior_case_ids"].append("D2")
    elif mutation == "cross":
        lab.plan["cases"][0]["stage"] = "cross"
    else:
        lab.plan["cases"][0]["source_sha256"] = "changed"
    with pytest.raises(ValueError, match="denominator|stage|sources or prompts"):
        lab.make_worker()
    assert not lab.native.sent and not lab.quota.calls


def test_continuation_rechecks_prior_consumption_before_admission(tmp_path):
    lab, _, parent = _continuation_lab(tmp_path)
    lab.worker.current = lab.plan["cases"][1]
    unexpected = parent / "calls" / SONNET.replace("/", "--") / "A/dev/D2/audit/call-consumed.json"
    scheduler.write_json(unexpected, {"attempt": "unexpected prior consumption"})
    with pytest.raises(ValueError, match="prior consumption changed"):
        _call(lab.worker, lab.worker._cell_directory(lab.worker.current) / "audit")
    assert not lab.native.sent and not lab.quota.calls


def test_prior_answer_is_only_cached_while_one_unsent_scorer_runs(tmp_path):
    lab, _, parent = _continuation_lab(tmp_path, action="resume_scorer")
    before = helpers.footprint(parent)
    lab.worker.current = lab.plan["cases"][0]
    directory = lab.worker._cell_directory(lab.worker.current)
    assert _call(lab.worker, directory / "audit")["status"] == "answered"
    assert not lab.native.sent and len(lab.native.reread) == 1
    assert _call(lab.worker, directory / "scorer", role="scorer")["status"] == "answered"
    _call(lab.worker, directory / "scorer", role="scorer")
    assert len(lab.native.sent) == 1 and lab.native.sent[0]["directory"].name == "scorer"
    assert helpers.footprint(parent) == before


def test_resume_cannot_silently_start_another_auditor_without_the_cache(tmp_path):
    lab, _, _ = _continuation_lab(tmp_path, action="resume_scorer", seed=False)
    lab.worker.current = lab.plan["cases"][0]
    with pytest.raises(ValueError, match="consumed prior role must not be reissued"):
        _call(lab.worker, lab.worker._cell_directory(lab.worker.current) / "audit")
    assert not lab.native.sent and not lab.quota.calls


def test_original_unsent_cross_slot_can_continue_without_touching_the_consumed_slot(tmp_path):
    lab, _, parent = _continuation_lab(tmp_path, stage="cross")
    before = helpers.footprint(parent)
    assert lab.worker.run()["complete_paired_cells"] == 1
    assert all("D2" in str(row["directory"]) for row in lab.native.sent)
    assert helpers.footprint(parent) == before


def test_unavailable_original_cross_is_represented_but_not_redispatched(tmp_path):
    lab, snapshot, _ = _continuation_lab(tmp_path, stage="cross", unavailable=True)
    assert lab.plan["workers"][0]["prior_case_ids"] == ["D1"]
    assert snapshot["workers"][scheduler.worker_key(SONNET, "A")]["cases"]["D1"]["action"] == "unavailable_input"
    assert lab.worker.run()["complete_paired_cells"] == 1
    assert all("D2" in str(row["directory"]) for row in lab.native.sent)


def test_separate_stage_roots_coordinate_through_the_same_provider_lock(tmp_path):
    shared = tmp_path / "provider-coordination"
    first = helpers.Lab(tmp_path / "first", cases=[helpers.case("D1", stage="dev")])
    second = helpers.Lab(tmp_path / "second", cases=[helpers.case("X1", stage="cross")])
    for lab in (first, second):
        lab.protocol["execution"]["provider_lock_root"] = str(shared)
        lab.worker = lab.make_worker()
    assert first.worker.provider_lock_root == second.worker.provider_lock_root == shared
    with scheduler.file_lock(first.worker.provider_lock_root / "provider-anthropic.lock"):
        assert second.worker.step()["status"] == "provider_wait"
    assert not second.native.sent and not second.quota.calls
