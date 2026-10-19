"""Provider-free tests for the exact fresh nineteen/K4 protocol; never dispatch."""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

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


def live_window():
    now = datetime.now(UTC)
    return {"schema": "envcheck.modal-serving-window/v1",
            "reservation_id": "trainer-finish-data-20261019-window-3",
            "window_id": "trainer-finish-data-20261019-window-3",
            "starts_at": (now - timedelta(hours=1)).isoformat(),
            "expires_at": (now + timedelta(hours=2)).isoformat(),
            "upstream": "https://fixture-owned-base.modal.run",
            "receipt": {"path": "/unused/receipt.json", "sha256": "sha256:" + "0" * 64}}


def done_settings(done_path):
    return {"concurrency": 7, "shared_window_client_done": {
        "client": "cheatbench", "path": str(done_path), "concurrency_after_done": 20}}


def done_receipt(window, **changes):
    value = {"schema": "mimo.shared-serving-window.client-done/v1", "client": "cheatbench",
             "window_receipt": "bridge/2026-10-17-v7/serving-window-v7.json",
             "remote_reservation_id": window["reservation_id"],
             "window_observed": {"last_observed_at_utc": datetime.now(UTC).isoformat(),
                                 "last_observed_status": "active"},
             "outcome": "completed_all_planned_waves",
             "recorded_at_utc": datetime.now(UTC).isoformat()}
    value.update(changes)
    return value


def test_client_done_widens_only_on_the_exact_live_window_receipt(tmp_path):
    window = live_window()
    done_path = tmp_path / "cheatbench-serving-window-done.json"
    settings = done_settings(done_path)
    # A missing or malformed receipt is simply not this window's completion.
    assert g.dispatch_width(settings, window) == 7
    done_path.write_text("{malformed")
    assert g.dispatch_width(settings, window) == 7
    # The historical WINDOW2 file can never count for this window.
    historical = done_receipt(window, remote_reservation_id="trainer-finish-data-20261018-window-2",
                              outcome="not_used_no_trial_fit_after_readiness",
                              recorded_at_utc="2026-10-18T17:32:00Z")
    g.write_json(done_path, historical, replace=True)
    assert g.dispatch_width(settings, window) == 7
    # Wrong client, wrong reservation, stale or future recording all stay at the plan bound.
    for changes in ({"client": "trainer"},
                    {"remote_reservation_id": "some-other-window"},
                    {"schema": "mimo.shared-serving-window.client-done/v0"},
                    {"recorded_at_utc": (datetime.now(UTC) - timedelta(hours=2)).isoformat()},
                    {"recorded_at_utc": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
                    {"recorded_at_utc": "not-a-timestamp"}):
        g.write_json(done_path, done_receipt(window, **changes), replace=True)
        assert g.dispatch_width(settings, window) == 7
    # The exact final receipt for this live window widens to the admitted bound.
    g.write_json(done_path, done_receipt(window), replace=True)
    assert g.dispatch_width(settings, window) == 20
    # Without the optional setting the existing bound is unchanged.
    assert g.dispatch_width({"concurrency": 7}, window) == 7


def native_settings(tmp_path, monkeypatch):
    """Minimum real SHA-bound settings fixture for provider-free checked_settings."""
    controls = tmp_path / "controls.json"
    controls.write_text("{}")
    request = tmp_path / "request.json"
    request.write_text("{}")
    monkeypatch.setattr(g, "CONTROLS_SHA", g.ref(controls)["sha256"])
    monkeypatch.setattr(g, "REQUEST_SHA", g.ref(request)["sha256"])
    cost = tmp_path / "cost.json"
    g.write_json(cost, {"pricing_basis": "resource_bound_plus_build_buffer",
                        "provider_product": "modal_sandbox",
                        "cpu_usd_per_core_second": .00003942,
                        "memory_usd_per_gib_second": .00000667,
                        "image_build_usd_per_cell": .01, "other_provider_usd_per_cell": .01,
                        "unknown_extras_acknowledged": True, "mechanism": "fixture resource quote",
                        "source_urls": ["https://modal.com/pricing"]})
    admission = tmp_path / "admission.json"
    g.write_json(admission, {"schema": "repaired_data_sft_v2.control_admission/v1",
                             "source_controls_handoff": g.ref(controls),
                             "tasks": [{"task_id": task_id, "controls_qualified": True}
                                       for task_id in g.TASK_IDS]})
    settings = {"schema": "mimo.phase2-native-settings/v1", "campaign_id": "phase2-window3",
                "reference_profile": "fixture-profile", "modal_app_name": "fixture-app",
                "concurrency": 7,
                "runtime": {"timeout_seconds": 180, "max_requests": 4,
                            "max_input_tokens": 1024, "cost_limit_usd": .5},
                "sandbox_timeout_seconds": 300,
                "image_build_reserve_usd_per_cell": .01,
                "other_provider_reserve_usd_per_cell": .01,
                "cost_evidence": g.ref(cost), "controls": g.ref(controls),
                "request": g.ref(request), "control_admission": g.ref(admission)}
    return settings, tmp_path / "settings.json"


def test_shared_done_setting_admits_only_the_exact_7_to_20_split(tmp_path, monkeypatch):
    settings, path = native_settings(tmp_path, monkeypatch)
    done = {"client": "cheatbench", "path": str(tmp_path / "done.json")}
    for concurrency, after_done, accepted in ((7, 20, True), (16, 20, False), (6, 20, False),
                                              (7, 19, False), (7, 21, False), (7, None, False)):
        settings.update(concurrency=concurrency,
                        shared_window_client_done={**done, "concurrency_after_done": after_done})
        g.write_json(path, settings, replace=True)
        if accepted:
            assert g.checked_settings(path)["concurrency"] == 7
        else:
            with pytest.raises(g.Refusal):
                g.checked_settings(path)
    # Without the optional setting the ordinary 1..16 bound is unchanged.
    del settings["shared_window_client_done"]
    for concurrency in (1, 16):
        settings["concurrency"] = concurrency
        g.write_json(path, settings, replace=True)
        assert g.checked_settings(path)["concurrency"] == concurrency


def test_wave_offsets_advance_by_actual_wave_length_covering_all_76_once():
    plan = matrix()
    cells = plan["cells"]
    specs = [f"spec-{cell['spec_id']}" for cell in cells]
    widths = iter([7, 20])
    waves = list(g.wave_plan(cells, specs, lambda: next(widths, 20)))
    assert [len(wave) for wave in waves] == [7, 20, 20, 20, 9]
    ordered = [cell["cell_id"] for wave in waves for cell, _ in wave]
    assert len(ordered) == len(set(ordered)) == 76
    assert ordered == [cell["cell_id"] for cell in cells]
    assert [spec for wave in waves for _, spec in wave] == specs
    with pytest.raises(g.Refusal):
        list(g.wave_plan(cells, specs, lambda: 21))
    with pytest.raises(g.Refusal):
        list(g.wave_plan(cells, specs, lambda: 0))


class FakeResponse:
    def __init__(self, status, payload=b""):
        self.status = status
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


def fake_upstream(*outcomes):
    """Sequential provider-free opener; the last outcome repeats when exhausted."""
    state = {"urls": [], "index": 0}

    def open_(request, timeout=None):
        state["urls"].append(request.full_url)
        outcome = outcomes[min(state["index"], len(outcomes) - 1)]
        state["index"] += 1
        if isinstance(outcome, Exception):
            raise outcome
        status, payload = outcome
        return FakeResponse(status, payload)

    return open_, state


MODELS_BODY = json.dumps({"data": [{"id": g.RETURNED_MODEL}]}).encode()
COMPLETION_BODY = json.dumps({"model": g.RETURNED_MODEL, "choices": [{"message": {"content": "OK"}}]}).encode()


def fake_clock():
    tick = [0.0]
    return (lambda: tick[0]), (lambda seconds: tick.__setitem__(0, tick[0] + seconds))


def test_live_binding_round_trips_the_exact_model():
    open_, state = fake_upstream((200, b""), (200, MODELS_BODY), (200, COMPLETION_BODY))
    result = g.live_binding(live_window(), key="fixture-key", budget_seconds=5, open_fn=open_)
    assert result["authenticated"] is True and result["attempts"] == 1
    assert result["returned_model"] == g.RETURNED_MODEL
    assert [url.rsplit("/", 1)[-1] for url in state["urls"]] == ["health", "models", "completions"]


def test_live_binding_accepts_a_plain_text_health_response():
    open_, _ = fake_upstream((200, b"OK"), (200, MODELS_BODY), (200, COMPLETION_BODY))
    result = g.live_binding(live_window(), key="fixture-key", budget_seconds=5, open_fn=open_)
    assert result["authenticated"] is True and result["attempts"] == 1


def test_live_binding_waits_out_a_cold_start_then_binds():
    window = live_window()
    cold = urllib.error.HTTPError(window["upstream"], 503, "cold", {}, None)
    open_, _ = fake_upstream(cold, cold, (200, b""), (200, MODELS_BODY), (200, COMPLETION_BODY))
    clock, sleeper = fake_clock()
    result = g.live_binding(window, key="fixture-key", budget_seconds=10, open_fn=open_,
                            clock=clock, sleeper=sleeper)
    assert result["attempts"] == 3


def test_live_binding_refuses_a_currently_unready_endpoint():
    window = live_window()
    cold = urllib.error.HTTPError(window["upstream"], 503, "cold", {}, None)
    open_, state = fake_upstream(cold)
    clock, sleeper = fake_clock()
    with pytest.raises(g.Refusal):
        g.live_binding(window, key="fixture-key", budget_seconds=5, open_fn=open_,
                       clock=clock, sleeper=sleeper)
    assert len(state["urls"]) > 1


def test_live_binding_refuses_non_cold_rejection_without_burning_the_budget():
    window = live_window()
    rejected = urllib.error.HTTPError(window["upstream"], 401, "unauthorized", {}, None)
    open_, state = fake_upstream(rejected)
    with pytest.raises(g.Refusal):
        g.live_binding(window, key="fixture-key", budget_seconds=60, open_fn=open_)
    assert len(state["urls"]) == 1


def test_live_binding_refuses_a_different_model_identity():
    window = live_window()
    wrong = json.dumps({"data": [{"id": g.RETURNED_MODEL}, {"id": "other-model"}]}).encode()
    open_, _ = fake_upstream((200, b""), (200, wrong), (200, COMPLETION_BODY))
    with pytest.raises(g.Refusal):
        g.live_binding(window, key="fixture-key", budget_seconds=60, open_fn=open_)
    mismatched = json.dumps({"model": "other-model", "choices": [{}]}).encode()
    open_, _ = fake_upstream((200, b""), (200, MODELS_BODY), (200, mismatched))
    with pytest.raises(g.Refusal):
        g.live_binding(window, key="fixture-key", budget_seconds=60, open_fn=open_)


def wave_fixture(tmp_path, monkeypatch, count=2):
    plan = {"tasks": [{"task_id": "000089", "task": "mimo-v2.6-rl/format-code-task-000089"}],
            "controls": {"fixture": True}}
    wave = []
    for index in range(count):
        cell = {"cell_id": f"000089-k{index + 1}", "spec_id": f"s-{index + 1}", "job_name": f"job-k{index + 1}",
                "task_index": 0, "task_id": "000089", "task": "mimo-v2.6-rl/format-code-task-000089"}
        spec = SimpleNamespace(spec_id=cell["spec_id"], jobs_dir="runs", name=cell["job_name"])
        wave.append((cell, spec))
    window = live_window()
    envelope_path = tmp_path / "envelope.json"
    g.write_json(envelope_path, {"schema": "mimo.phase2-envelope/v1"})
    envelope = {"approval": "fixture-approval"}
    monkeypatch.setattr(g, "checked_window", lambda *args, **kwargs: window)
    monkeypatch.setattr(g, "checked_envelope", lambda *args, **kwargs: (envelope, {}))
    monkeypatch.setattr(g, "checked_plan", lambda *args, **kwargs: plan)
    native = SimpleNamespace(validate_request=lambda *args, **kwargs: None)
    queue_module = SimpleNamespace(Executor=SimpleNamespace(
        prepare_request=staticmethod(lambda spec, repo_root: SimpleNamespace())))
    queue = Mock()
    queue.locate.side_effect = FileNotFoundError
    queue.load.side_effect = lambda submitted: SimpleNamespace(spec_id=submitted.spec_id)
    out = tmp_path / "out"
    out.mkdir()
    return {"wave": wave, "window": window, "plan": plan, "envelope": envelope,
            "envelope_path": envelope_path, "plan_path": tmp_path / "plan.json",
            "serving_window": tmp_path / "window.json", "modules": (native, queue_module),
            "queue": queue, "out": out}


def test_wave_charges_nothing_when_the_endpoint_is_currently_unready(tmp_path, monkeypatch):
    fixture = wave_fixture(tmp_path, monkeypatch)
    cold = urllib.error.HTTPError(fixture["window"]["upstream"], 503, "cold", {}, None)
    open_, _ = fake_upstream(cold)
    clock, sleeper = fake_clock()

    def binding(window, **kwargs):
        return g.live_binding(
            window, key="fixture-key", budget_seconds=5, open_fn=open_, clock=clock, sleeper=sleeper)

    budget = Mock()
    executor = Mock()
    executor.last_tick_reason = None
    rows, stop = g.dispatch_wave(fixture["wave"], window=fixture["window"], plan=fixture["plan"],
        plan_path=fixture["plan_path"], envelope_path=fixture["envelope_path"],
        envelope=fixture["envelope"], serving_window=fixture["serving_window"],
        modules=fixture["modules"], budget=budget, queue=fixture["queue"], executor=executor,
        out=fixture["out"], key="fixture-key", binding_check=binding)
    assert stop is True and len(rows) == len(fixture["wave"])
    budget.charge.assert_not_called()
    executor.submit.assert_not_called()
    executor.tick.assert_not_called()
    for row in rows:
        assert row["dispatch_error"] is not None
        assert row["reservation"] is None and row["fresh_binding"] is None


def test_wave_dispatches_only_after_a_fresh_exact_binding(tmp_path, monkeypatch):
    fixture = wave_fixture(tmp_path, monkeypatch)

    def binding(window, **kwargs):
        return {"authenticated": True, "checked_at": "fixture",
                "returned_model": g.RETURNED_MODEL}

    budget = Mock()
    budget.charge.side_effect = lambda cell, receipt: {"cell_id": cell["cell_id"], "ordinal": 1}
    executor = Mock()
    executor.last_tick_reason = "fixture-tick"
    executor.submit.side_effect = lambda spec: (spec, SimpleNamespace(
        admitted=False, reason_code="paid_run_unauthorized"))
    executor.tick.return_value = len(fixture["wave"])
    rows, stop = g.dispatch_wave(fixture["wave"], window=fixture["window"], plan=fixture["plan"],
        plan_path=fixture["plan_path"], envelope_path=fixture["envelope_path"],
        envelope=fixture["envelope"], serving_window=fixture["serving_window"],
        modules=fixture["modules"], budget=budget, queue=fixture["queue"], executor=executor,
        out=fixture["out"], key="fixture-key", binding_check=binding)
    assert stop is False
    assert budget.charge.call_count == len(fixture["wave"])
    assert executor.submit.call_count == len(fixture["wave"])
    assert executor.tick.call_args.kwargs["parallel"] == len(fixture["wave"])
    journal = [json.loads(line) for line in
               (fixture["out"] / "dispatch-journal.jsonl").read_text().splitlines()]
    tick = [event for event in journal if event["event"] == "native_tick_requested"]
    assert len(tick) == 1
    assert tick[0]["wave_width"] == len(fixture["wave"])
    assert tick[0]["fresh_binding"] == {"authenticated": True, "checked_at": "fixture",
                                        "returned_model": g.RETURNED_MODEL}
    for row in rows:
        assert row["dispatch_error"] is None
        assert row["reservation"]["cell_id"] == row["cell_id"]
        assert row["fresh_binding"]["authenticated"] is True
