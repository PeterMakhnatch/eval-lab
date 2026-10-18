"""Provider-free tests for the exact fresh nineteen/K4 protocol; never dispatch."""
from __future__ import annotations

import copy
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "research/experiments/repaired-data-sft-v2"


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, DIRECTORY / file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


g = load("phase2_native_generation_test_module", "native_generation.py")
a = load("phase2_fresh_admission_test_module", "fresh_native_admission.py")


def matrix():
    tasks = [{"task_id": task_id, "task": "mimo-v2.6-rl/format-code-task-" + task_id} for task_id in g.TASK_IDS]
    return {"schema": "mimo.phase2-native-plan/v1", "K": 4, "max_transient_retries": 0, "tasks": tasks,
            "cells": [{"task_id": task["task_id"], "task": task["task"], "task_index": index,
                       "attempt_index": attempt, "cell_id": f"{task['task_id']}-k{attempt}",
                       "job_name": f"phase2-{task['task_id']}-k{attempt}", "spec_id": f"unique-{index}-{attempt}"}
                      for index, task in enumerate(tasks) for attempt in range(1, 5)]}


def envelope(tmp_path, admitted=True):
    plan = {"cost_model": {"usd_per_execution_minimum": .0855}}
    plan_path = tmp_path / "plan.json"
    g.write_json(plan_path, plan)
    value = {"schema": "mimo.phase2-envelope/v1", "admitted": admitted, "reservation_id": "phase2-trainer-shared-20261018",
             "approval": "Peter phase2 combined12", "approved_usd": "12.00", "plan": g.ref(plan_path),
             "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
             "native": {"cap_usd": "6.50", "usd_per_execution": "0.0855", "executions_max": 76,
                        "durable_journal_path": str(tmp_path / "native-ledger.json")},
             "serving": {"cap_usd": "2.50"}, "heldout_regrade": {"cap_usd": "3.00"}}
    path = tmp_path / "envelope.json"
    g.write_json(path, value)
    return path, plan_path, value, plan


def test_exact_independent_k4_matrix():
    plan = matrix()
    g.validate_cells(plan)
    assert len(plan["cells"]) == 76
    assert "002486" not in g.TASK_IDS and "001478" not in g.TASK_IDS


@pytest.mark.parametrize("mutation", ["held", "retry", "alias", "wrong_ancestry", "bad_name", "trial_expansion"])
def test_matrix_refuses_nonfresh_changes(mutation):
    plan = matrix()
    if mutation == "held":
        plan["tasks"][0]["task_id"] = "002486"
    elif mutation == "retry":
        plan["max_transient_retries"] = 1
    elif mutation == "alias":
        plan["cells"][1]["spec_id"] = plan["cells"][0]["spec_id"]
    elif mutation == "wrong_ancestry":
        plan["cells"][0]["task_index"] = 1
    elif mutation == "bad_name":
        plan["cells"][0]["job_name"] = "not/legal/name"
    else:
        plan["cells"].append(copy.deepcopy(plan["cells"][0]))
    with pytest.raises(g.Refusal):
        g.validate_cells(plan)


def test_sandbox_quote_not_obsolete_function_rate(tmp_path):
    quote = tmp_path / "quote.json"
    g.write_json(quote, {"cpu_usd_per_core_second": .00003942, "memory_usd_per_gib_second": .00000667})
    settings = {"sandbox_timeout_seconds": 300, "image_build_reserve_usd_per_cell": 0,
                "other_provider_reserve_usd_per_cell": .00618, "runtime": {"timeout_seconds": 180}, "cost_evidence": g.ref(quote)}
    cost = g.cost_model(settings, [{"resources": {"physical_cpus": 2, "memory_mb": 8192}}])
    assert cost["sandbox_exposure_usd_per_cell"] == pytest.approx(.07932)
    assert cost["usd_per_execution_minimum"] == .0855
    assert cost["total_reservation_minimum_usd"] == 6.498
    assert cost["sandboxes_per_cell"] == 2 and cost["sandbox_create_max_attempts"] == 1
    assert cost["original_v6_replays"] == 0 and cost["original_v6_grade"] == "not_requested_unmeasured"


def test_shared_actual_approval_not_an_independent_old_four(tmp_path):
    path, plan_path, value, plan = envelope(tmp_path)
    g.checked_envelope(path, plan_path, plan)
    value["overhead"] = {"cap_usd": .01}
    g.write_json(path, value, replace=True)
    with pytest.raises(g.Refusal, match="combined approval"):
        g.checked_envelope(path, plan_path, plan)


def test_unadmitted_envelope_refuses_without_minting_a_ledger(tmp_path):
    path, plan_path, value, plan = envelope(tmp_path, admitted=False)
    with pytest.raises(g.Refusal, match="NOT admitted"):
        g.checked_envelope(path, plan_path, plan)
    assert not Path(value["native"]["durable_journal_path"]).exists()


def test_native_durable_gate_never_refunds_or_retries(tmp_path):
    path, _, value, _ = envelope(tmp_path)
    section = value["native"]
    ledger_path = Path(section["durable_journal_path"])
    with pytest.raises(g.Refusal, match="initialize"):
        g.NativeBudget(path, value, section)
    g.write_json(ledger_path, {"reservation_id": value["reservation_id"], "envelope": g.ref(path), "approval": value["approval"],
                               "cap_usd": section["cap_usd"], "usd_per_execution": section["usd_per_execution"],
                               "executions": 0, "reserved_usd": 0, "charges": []})
    budget = g.NativeBudget(path, value, section)
    cell = matrix()["cells"][0]
    event = budget.charge(cell, {"path": "parent-owned-actual-window", "sha256": "bound-by-run-preflight"})
    assert event["reservation_type"] == "fresh_native_repaired"
    assert budget.read()["reserved_usd"] == .0855
    with pytest.raises(g.Refusal, match="no retry/reset"):
        budget.charge(cell, event["serving_window"])
    assert g.NativeBudget(path, value, section).read()["executions"] == 1
    state = budget.read()
    state["reserved_usd"] = 0
    g.write_json(ledger_path, state, replace=True)
    with pytest.raises(g.Refusal, match="nonrefundable"):
        budget.read()


def phases():
    times = [("environment_setup", 0, 1), ("agent_setup", 1, 2), ("agent_execution", 2, 3), ("verifier", 3, 4)]
    def instant(second):
        return f"2026-10-18T09:00:0{second}+00:00"

    return {**{name: {"started_at": instant(start), "finished_at": instant(end)} for name, start, end in times}, "finished_at": instant(5)}


def applications():
    def app(second, sandbox, source, mode, applied):
        return {"recorded_at": f"2026-10-18T09:00:0{second}+00:00", "sandbox_id": sandbox, "source": source,
                "network_mode": mode, "applied": applied, "allowed_hosts": []}
    return {"schema_version": 2, "applied": True,
            "applications": [app(0, "actual-agent", "creation", "public", False), app(1, "actual-agent", "apply", "no-network", True),
                             app(3, "actual-verifier", "creation", "public", False)]}


def test_effective_agent_lock_preserves_public_setup_without_false_all_locked():
    proof = a.effective_agent_lock(phases(), applications(), g)
    assert proof["generation_interval_locked"]
    assert len(proof["setup_public_applications_preserved"]) == 2
    assert proof["ordered_applications"][0]["applied"] is False


def test_any_late_applied_lock_cannot_cover_public_generation():
    document = applications()
    document["applications"][1]["recorded_at"] = "2026-10-18T09:00:03+00:00"
    # Top-level applied=true used to conceal the unlocked generation interval.
    with pytest.raises(g.Refusal, match="public/unapplied"):
        a.effective_agent_lock(phases(), document, g)


def test_missing_native_grade_is_not_a_zero_or_a_pass():
    assert g.score({"verifier_result": None}) is None
    assert g.score({"verifier_result": {"rewards": {"reward": "1"}}}) is None
    assert g.score({"verifier_result": {"rewards": {"reward": 1}}}) == 1


@pytest.mark.parametrize("changed_reasoning", [False, True])
def test_faithful_native_projection_preserves_reasoning_and_tools(tmp_path, changed_reasoning):
    Trajectory = pytest.importorskip("harbor.models.trajectories.trajectory").Trajectory

    from evallab.mimoagent_trajectory import native_to_atif

    native = {"info": {"exit_status": "Idle", "antihack": False, "antihack_blocks": [],
                       "explicit_rules": False, "model_stats": {"producer": "sdk-serializer"}},
              "trajs": {"main": {"tools": [{"type": "function", "function": {"name": "terminal", "description": "shell",
                                                                           "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}}],
                                 "tool_choice": "auto", "messages": [
                                     {"role": "system", "content": "task rules"},
                                     {"role": "user", "content": "repair source"},
                                     {"role": "assistant", "content": None, "reasoning_content": "inspect original",
                                      "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "terminal", "arguments": "{\"cmd\":\"printf hi\"}"}}]},
                                     {"role": "tool", "tool_call_id": "call-1", "name": "terminal", "content": "hi"},
                                     {"role": "assistant", "content": "complete", "reasoning_content": "checked"}]}}}
    calls = [{"name": "main", "assistant_index": index, "usage": {"prompt_tokens": 10, "completion_tokens": 5}} for index in range(2)]
    # The real worker publishes a verdict COUNT and its own finished-event
    # stats, not the SDK serializer's list/auxiliary stats. No history is edited.
    streamed = {**native, "info": {**native["info"], "antihack_blocks": 0, "model_stats": {"producer": "finished-event"}}}
    trajectory = Trajectory.model_validate(native_to_atif(streamed, calls, trajectory_id="actual-unit-trace", model_name=g.BASE_MODEL)).model_dump(mode="json", exclude_none=True)
    if changed_reasoning:
        trajectory["steps"][2]["reasoning_content"] = "substituted"
    g.write_json(tmp_path / "native.json", native)
    g.write_json(tmp_path / "trajectory.json", trajectory)
    for call in calls:
        g.append_json(tmp_path / "calls.jsonl", {"event": "model_call", **call})
    row = {"trajectory": g.ref(tmp_path / "trajectory.json"), "native_trajectory": g.ref(tmp_path / "native.json"),
           "model_calls": g.ref(tmp_path / "calls.jsonl")}
    if changed_reasoning:
        with pytest.raises(g.Refusal, match="conversation/tool/reasoning"):
            a.faithful_trace(row, g)
    else:
        proof = a.faithful_trace(row, g)
        assert proof["conversations"] == native["trajs"]
        assert proof["assistant_turns"] == 2
        assert proof["fidelity"] == "complete_native_messages_tools_reasoning"
        assert proof["projection_normalizations"]["antihack_blocks"] == "actual_worker_verdict_list_to_finished_event_count"


def serving_receipt(tmp_path, monkeypatch, upstream, name):
    window = load(name, str(ROOT / "tools/modal-mimo-serve/serving_window.py"))
    envelope_path, _, value, plan = envelope(tmp_path)
    controls = tmp_path / "controls.json"
    controls.write_text('{"strict_tasks":19}')
    monkeypatch.setattr(window, "CONTROLS_SHA256", window.sha256(controls.read_bytes()))
    value.update(ledger_tag="2026-10-17-v7", controls=g.ref(controls))
    value["serving"].update(
        app_name=window.APP_NAME, gpu="L40S", maximum_seconds=3500,
        usd_per_second_upper_bound="0.0006676325", shutdown_reserve_seconds=120,
        hard_cpu_limit=4, hard_memory_mib_limit=32768,
    )
    g.write_json(envelope_path, value, replace=True)
    current = datetime.now(UTC)
    config = window.admitted_config(
        envelope_path, g.ref(envelope_path)["sha256"].removeprefix("sha256:"),
        "fixture-owner", (current + timedelta(seconds=3500)).isoformat(),
        now=current.timestamp(),
    )
    journal = window.Journal(tmp_path / "owned-window.json")
    journal.create(config)
    app_id = "ap-fixture-owned"
    models = journal.artifact("models", {"data": [{"id": g.RETURNED_MODEL}]})
    guard = {"armed": True, "app_id": app_id, "deadline": config["expires_at"]}
    guard["evidence"] = journal.artifact("guard", guard)
    record = window.receipt(journal.update(
        state="active", app_id=app_id, upstream=upstream, stop_guard=guard,
        deployment=journal.artifact("deployment", {"app_id": app_id}),
        readiness={
            "authenticated": True, "method": "GET", "path": "/v1/models",
            "status_code": 200, "upstream": upstream, "checked_at": current.isoformat(),
            "response": models,
        },
        authenticated_binding={"requested_model": g.RETURNED_MODEL, "returned_model": g.RETURNED_MODEL},
    ))
    receipt_path = tmp_path / "fixed-ready-receipt.json"
    g.write_json(receipt_path, record)
    plan["config"] = {"sandbox_timeout_seconds": 300}
    return receipt_path, envelope_path, plan, models


def test_actual_serving_receipt_custody_is_usable_and_rejects_artifact_drift(tmp_path, monkeypatch):
    receipt_path, envelope_path, plan, models = serving_receipt(
        tmp_path, monkeypatch, "https://fixture-owned-base.modal.run",
        "phase2_native_serving_receipt_contract")
    g.checked_window(receipt_path, envelope_path, plan)
    Path(models["path"]).write_text('{"data":[{"id":"different-model"}]}')
    with pytest.raises(g.Refusal):
        g.checked_window(receipt_path, envelope_path, plan)


@pytest.mark.parametrize("upstream,accepted", [
    ("https://p-makhnatch--evallab-mimo-v26-9b-mimoserver.us-east.modal.direct", True),
    ("https://p-makhnatch--evallab-mimo-v26-9b-mimoserver.modal.run", True),
    ("http://x.us-east.modal.direct", False),
    ("https://modal.direct", False),
    ("https://x.modal.direct", False),
    ("https://x.notmodal.direct", False),
    ("https://x.evil.modal.direct", False),
    ("https://a.b.us-east.modal.direct", False),
    ("https://x.modal.direct.evil.example", False),
    ("https://x.modal.run.evil.example", False),
    ("https://user:pw@x.us-east.modal.direct", False),
    ("https://x.us-east.modal.direct:8443", False),
    ("https://x.us-east.modal.direct/v1", False),
    ("https://x.us-east.modal.direct/?q=1", False),
    ("https://x.us-east.modal.direct#frag", False),
])
def test_checked_window_accepts_only_modal_owned_origins(tmp_path, monkeypatch, upstream, accepted):
    receipt_path, envelope_path, plan, _ = serving_receipt(
        tmp_path, monkeypatch, upstream, "phase2_native_serving_origin_contract")
    if accepted:
        g.checked_window(receipt_path, envelope_path, plan)
    else:
        with pytest.raises(g.Refusal, match="fake endpoint"):
            g.checked_window(receipt_path, envelope_path, plan)
