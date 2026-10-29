"""Pure synthetic OQ10 driver tests: no provider, corpus, key or real-task reads."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "research/experiments/oq10-task-quality-cross"


def module(name: str):
    sys.path.insert(0, str(SOURCE))
    try:
        spec = importlib.util.spec_from_file_location(name, SOURCE / (name + ".py"))
        result = importlib.util.module_from_spec(spec)
        sys.modules[name] = result
        spec.loader.exec_module(result)
        return result
    finally:
        sys.path.pop(0)


study = module("study")
freeze = module("freeze_study")
driver = module("run_study")


def prepared(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    instruction = package / "instruction.md"
    instruction.write_text("Synthetic task: accept the stated value.\n")
    case = {
        "case_id": "C0001", "corpus": "automationbench-verified",
        "preparation_status": "prepared", "package": str(package),
        "source_record_sha256": study.sha(b"native-synthetic-record"),
        "record_binding_sha256": study.sha(b"frozen-native-identity-and-bundle"),
        "files": [{"path": "instruction.md", "sha256": study.sha(instruction.read_bytes())}],
    }
    for name in ("source", "prompt", "source_manifest", "context_provenance", "context_display"):
        path = tmp_path / (name + ".txt")
        path.write_text("Synthetic " + name)
        case[name] = study.pin(path)
    return case


def helper(case, tmp_path, dispositions):
    state = {"invocations": 0, "sent": 0, "parser_calls": 0}
    response = tmp_path / "attested-response.json"
    response.write_text('{"findings":[]}')
    def run_native(*args, **kwargs):
        disposition, terminal, sent = dispositions[state["invocations"]]
        state["invocations"] += 1
        state["sent"] += sent
        return {
            "disposition": disposition, "terminal": terminal, "sent": sent, "reason": None,
            "answer": {"findings": []},
            "receipt": {"response_path": str(response), "response_sha256": study.sha(response.read_bytes()),
                        "prompt_sha256": case["prompt"]["sha256"]},
        }
    def export_flags(payloads, *, parse_status, **kwargs):
        complete = parse_status == "ok"
        return {"status": parse_status, "memberships": {},
                **{gate: False if complete else None for gate in study.GATES}}
    def audit_proposals(self, package, **kwargs):
        state["parser_calls"] += 1
        assert kwargs["min_verified_confidence"] == kwargs["min_unverified_confidence"] == 0
        return []
    return (SimpleNamespace(run_native=run_native, export_flags=export_flags),
            SimpleNamespace(audit_proposals=audit_proposals), state)


def step(case, output, native, worker, monkeypatch):
    monkeypatch.setattr(study, "count_window", lambda *args, **kwargs: {"synthetic": True})
    return study.step_role(
        case, role="kimi", route="kimi-code/k3.1-pro", output=output, system="synthetic",
        runtime_manifest={}, native_helper=native, task_quality_worker=worker,
        paired=SimpleNamespace(gate_memberships=lambda payloads: {}), verify_freeze=lambda: None,
    )


def test_unsent_pending_resumes_only_the_original_role_and_cached_cells_never_send(tmp_path, monkeypatch):
    case = prepared(tmp_path)
    native, worker, state = helper(case, tmp_path, [("quota_pending", False, False), ("answered", True, True)])
    output = tmp_path / "results"
    pending = step(case, output, native, worker, monkeypatch)
    assert pending["terminal"] is False and set(pending["flags"].values()) == {None}
    assert not (output / "cells/kimi/C0001.json").exists()
    final = step(case, output, native, worker, monkeypatch)
    assert final["status"] == "ok" and set(final["flags"].values()) == {False}
    assert step(case, output, native, worker, monkeypatch) == final
    assert state == {"invocations": 2, "sent": 1, "parser_calls": 1}
    assert final["record_binding_sha256"] == final["prediction"]["record_binding_sha256"] == case["record_binding_sha256"]
    Path(case["source"]["path"]).write_text("tampered synthetic source")
    with pytest.raises(ValueError):
        step(case, output, native, worker, monkeypatch)
    assert state["invocations"] == 2


@pytest.mark.parametrize("disposition", ["provider_error", "refused", "malformed", "wrong_identity"])
def test_consumed_failures_are_terminal_missing_and_never_reissued(tmp_path, monkeypatch, disposition):
    case = prepared(tmp_path)
    native, worker, state = helper(case, tmp_path, [(disposition, True, True)])
    output = tmp_path / "results"
    final = step(case, output, native, worker, monkeypatch)
    assert final["status"] == "missing" and set(final["flags"].values()) == {None}
    assert step(case, output, native, worker, monkeypatch) == final
    assert state == {"invocations": 1, "sent": 1, "parser_calls": 0}


def test_cached_cell_with_foreign_record_binding_is_rejected_before_any_send(tmp_path, monkeypatch):
    case = prepared(tmp_path)
    native, worker, state = helper(case, tmp_path, [("answered", True, True)])
    output = tmp_path / "results"
    step(case, output, native, worker, monkeypatch)
    path = output / "cells/kimi/C0001.json"
    cell = json.loads(path.read_bytes())
    cell["record_binding_sha256"] = study.sha(b"another native record")
    path.write_text(json.dumps(cell))
    with pytest.raises(ValueError, match="cached terminal cell"):
        step(case, output, native, worker, monkeypatch)
    assert state["invocations"] == 1


def test_actual_prompt_and_attested_response_bytes_are_enforced(tmp_path, monkeypatch):
    case = prepared(tmp_path)
    native, worker, _ = helper(case, tmp_path, [("answered", True, True)])
    result = native.run_native()
    result["receipt"]["prompt_sha256"] = study.sha(b"foreign prompt")
    with pytest.raises(ValueError, match="frozen prompt"):
        study.parse_flags(case, result, route="kimi-code/k3.1-pro", task_quality_worker=worker,
                          paired=None, native_helper=native)
    result["receipt"]["prompt_sha256"] = case["prompt"]["sha256"]
    Path(result["receipt"]["response_path"]).write_text('{"findings":["changed"]}')
    with pytest.raises(ValueError, match="attested response bytes"):
        study.parse_flags(case, result, route="kimi-code/k3.1-pro", task_quality_worker=worker,
                          paired=None, native_helper=native)


def test_orphan_attempts_and_cache_inclusive_usage_are_counted_once(tmp_path):
    directory = tmp_path / "calls/kimi/C0001"
    directory.mkdir(parents=True)
    (directory / "call-consumed.json").write_text("{}")
    receipt = {"usage": {"input": 10, "cacheRead": 20, "cacheWrite": 30}, "tokens_in": 60,
               "tokens_out": 7, "reasoning_tokens": 3, "cost_usd": None, "billing_mode": "subscription"}
    (directory / "receipt.json").write_text(json.dumps(receipt))
    usage = driver.reconcile_usage(tmp_path, "kimi", ["C0001", "C0001"])
    assert usage["consumed_native_slots"] == 1
    assert (usage["input_tokens"], usage["output_tokens"], usage["reasoning_tokens"]) == (60, 7, 3)
    assert usage["actual_paid_usd"] is None
    orphan = tmp_path / "calls/kimi/C0002"
    orphan.mkdir()
    (orphan / "call-consumed.json").write_text("{}")
    usage = driver.reconcile_usage(tmp_path, "kimi", ["C0001", "C0002"])
    assert usage["consumed_native_slots"] == 2 and usage["orphan_without_receipt"] == 1
    assert usage["input_tokens"] is usage["output_tokens"] is None


def test_unknown_usage_is_null_not_zero_or_a_crash(tmp_path):
    directory = tmp_path / "calls/kimi/C0001"
    directory.mkdir(parents=True)
    (directory / "receipt.json").write_text(json.dumps({"usage": None, "cost_usd": None, "billing_mode": "subscription"}))
    usage = driver.reconcile_usage(tmp_path, "kimi", ["C0001"])
    assert usage["consumed_native_slots"] == 1 and usage["input_tokens"] is None


def test_slot_partition_preserves_unsent_orphans_and_terminal_missing(tmp_path):
    records = [
        {"role": "kimi", "case_id": "C0001", "status": "ok", "prediction": {"status": "ok"},
         "flags": {gate: False for gate in study.GATES}},
        {"role": "kimi", "case_id": "C0002", "status": "missing", "flags": {gate: None for gate in study.GATES}},
    ]
    (tmp_path / "quota-pending/kimi/C0004").mkdir(parents=True)
    result = driver.slot_coverage(tmp_path, "kimi", [f"C000{i}" for i in range(1, 6)], records,
                                  {"slots": [{"case_id": "C0001"}, {"case_id": "C0003"}]})
    assert result == {"planned": 5, "complete": 1, "terminal_missing": 1,
                      "consumed_without_final_quality": 1, "quota_unsent": 1, "not_yet_sent": 1}


def test_proof_replay_cannot_call_a_provider_without_prior_consumption(tmp_path, monkeypatch):
    registration, result = tmp_path / "registration.json", tmp_path / "results/result.json"
    result.parent.mkdir()
    registration.write_text(json.dumps({"requested_route": freeze.ROLES["kimi"]}))
    result.write_text(json.dumps({"sent": True, "disposition": "answered"}))
    monkeypatch.setattr(freeze, "load_module", lambda *args: pytest.fail("must reject before loading a transport"))
    with pytest.raises(ValueError, match="actual consumed native receipt"):
        freeze.positive_route_proof({"roles": freeze.ROLES}, registration=registration, result=result)


def test_mutable_checkout_custody_import_is_rejected_before_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(freeze, "environment_guard", lambda: None)
    value = {"schema": freeze.SCHEMA_CODE, "roles": freeze.ROLES, "planned_tasks": 180,
             "own": {"freeze_study.py": {"path": str(tmp_path / "private/freeze_study.py")}}}
    path = tmp_path / "code.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="frozen private custody modules"):
        freeze.load_code(path, study.sha(path.read_bytes()))


def test_opaque_schedule_interleaves_sources_without_looking_at_class_labels():
    groups = {
        "automationbench-verified": [
            {"task_id": "ab-second", "subcorpus": "ab", "positive": False, "case_id": "must-not-escape"},
            {"task_id": "ab-first", "subcorpus": "ab", "positive": True},
        ],
        "paichecker": [
            {"task_id": "multi-second", "subcorpus": "swe_bench_multilingual", "positive": True},
            {"task_id": "gym-second", "subcorpus": "swe_gym", "positive": False},
            {"task_id": "multi-first", "subcorpus": "swe_bench_multilingual", "positive": False},
            {"task_id": "gym-first", "subcorpus": "swe_gym", "positive": True},
        ],
    }
    selected = study.ordered_selection(groups)
    assert [row["task_id"] for row in selected] == [
        "ab-second", "multi-second", "gym-second", "ab-first", "multi-first", "gym-first",
    ]
    assert [row["case_id"] for row in selected] == [f"C{i:04d}" for i in range(1, 7)]
    flipped = {key: [{**row, "positive": not row["positive"]} for row in rows] for key, rows in groups.items()}
    assert [row["task_id"] for row in study.ordered_selection(flipped)] == [row["task_id"] for row in selected]


def test_triage_is_only_a_ranking_and_never_drops_missing_or_unflagged_tasks():
    cases = [{"case_id": f"C{i:04d}"} for i in range(1, 4)]
    cells = {
        "C0001": {"prediction": {"parsed_payloads": []}},
        "C0002": {"prediction": {"parsed_payloads": [{"confidence": 0.9}]}},
    }
    assert study.triage_order(cases, cells) == ["C0002", "C0001", "C0003"]
