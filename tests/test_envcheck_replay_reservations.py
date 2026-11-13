"""Local recipe reservation plans; synthetic custody, no heldout data or dispatch."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab.qa import counterfactual as cf
from evallab.regrade import CloudSpendGate

SCRIPTS = Path(__file__).resolve().parents[1] / "research/experiments/test-repair-performance-20261017"
EXECUTION = "00000000-0000-0000-0000-000000000001"
APPROVAL = "synthetic local test; no execution authorized"


@pytest.fixture
def recipes(monkeypatch: pytest.MonkeyPatch):
    def load(name: str):
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    def no_dispatch(*args, **kwargs):
        pytest.fail("reservation planning must not dispatch")

    monkeypatch.delenv("EVALLAB_NO_PROCESS_JOB", raising=False)
    monkeypatch.setattr(cf, "replay", no_dispatch)
    partition = load("partition")
    replay = load("replay_passes")
    postgrade = load("postgrade_evaluation")
    monkeypatch.setattr(replay, "execute_jobs", no_dispatch)
    monkeypatch.setattr(postgrade, "execute_once", no_dispatch)
    return partition, replay, postgrade


@pytest.mark.parametrize(("reserve_fraction", "executions"), [(1, 1), (1, 2), (0.5, 1)])
def test_replay_plan_computes_remaining_reservations_and_refuses_below_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recipes, reserve_fraction: float, executions: int,
) -> None:
    partition, replay, _ = recipes
    reserve = cf.CLOUD_USD_PER_EXECUTION * reserve_fraction
    cap = 4 * cf.CLOUD_USD_PER_EXECUTION
    budget = tmp_path / "budget.json"
    budget.write_text(json.dumps(CloudSpendGate(APPROVAL, cap, reserve, executions).to_json()))
    original_budget = budget.read_bytes()
    inventory = tmp_path / "inventory.jsonl"
    inventory.write_text(json.dumps({"execution_id": EXECUTION, "task": "synthetic/task", "heldout": False}) + "\n")
    variants = tmp_path / "variants.json"
    variants.write_text("{}")
    pin = {"task": "synthetic/task", "repaired_digest": "sha256:" + "a" * 64}
    # Substitute only custody admission. The CLI, reservation checks, real spend
    # gate, plan arithmetic and refusal envelopes remain the production paths.
    monkeypatch.setattr(partition, "frozen_heldout", lambda path: (set(), {"synthetic": True}))
    monkeypatch.setattr(partition, "variants", lambda path: [pin])
    monkeypatch.setattr(partition, "parent_task", lambda task: task)
    monkeypatch.setattr(replay, "dependencies", lambda: (partition, cf, SimpleNamespace(__file__=cf.__file__)))
    monkeypatch.setattr(replay, "source_custody", lambda *args, **kwargs: (tmp_path / "source", {}))
    monkeypatch.setattr(replay, "grader_custody", lambda *args: (None, None, {}))
    monkeypatch.setattr(replay, "matrix_support", lambda *args: [])
    monkeypatch.setattr(replay, "LANE", tmp_path)
    output = tmp_path / "replay-v1"
    monkeypatch.setattr(sys, "argv", [
        "replay_passes.py", "--inventory", str(inventory), "--variants", str(variants),
        "--execution", EXECUTION, "--out", str(output), "--budget", str(budget),
        "--max-usd", str(cap), "--reserve-usd", str(reserve), "--approval", APPROVAL,
    ])

    assert replay.main() == (1 if reserve_fraction < 1 else 0)
    plan = json.loads((output / "plan.json").read_text())
    receipt = json.loads((output / "receipt.json").read_text())
    assert plan["planned_native_executions"] == 3  # Source, nop and reference.
    assert plan["budget"]["additional_reservation_usd"] == round(3 * reserve, 4)
    assert plan["budget"]["initial_state"]["reserved_usd"] == round(executions * reserve, 4)
    assert plan["budget"]["fits_remaining_lane_fence"] is ((executions + 3) * reserve <= cap)
    if reserve_fraction < 1:
        reason = f"reservation_below_native_cf_floor:{cf.CLOUD_USD_PER_EXECUTION}"
        assert plan["status"] == "refused"
        assert plan["problems"] == [reason]
        assert reason in json.loads((output / "summary.json").read_text())["rows"][0]["refusals"]
    else:
        assert plan["status"] == "ready"
        assert plan["problems"] == []
    assert receipt["budget"]["native_executions_reserved_this_run"] == 0
    assert receipt["budget"]["native_matrix_dispatch_attempts"] == 0
    assert receipt["training_exported"] is False
    assert budget.read_bytes() == original_budget


@pytest.mark.parametrize("refusal", [None, "floor", "consumed"])
def test_postgrade_plan_preserves_fixed_allocation_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recipes, refusal: str | None,
) -> None:
    partition, _, postgrade = recipes
    monkeypatch.setattr(postgrade, "LANE", tmp_path)
    monkeypatch.setattr(postgrade, "OWNED_ROOT", tmp_path)
    output = tmp_path / "evaluation-postgrade-v1"
    budget = tmp_path / "budget.json"
    state = CloudSpendGate(APPROVAL, 0.04, 0.04, int(refusal == "consumed")).to_json()
    state["charges"] = [{"synthetic": True}] if refusal == "consumed" else []
    budget.write_text(json.dumps(state))
    original_budget = budget.read_bytes()
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({
        "schema": "envcheck.global-exposure-ledger/v1", "approval": APPROVAL,
        "cap_usd": 4, "total_exposure_usd": 0.04,
        "reservations": [{"reservation_id": "evaluation-correction-final-grade",
                          "kind": "native_evaluation_correction_grade",
                          "reserved_usd": 0.04, "budget_path": str(budget)}],
    }))
    original_ledger = ledger.read_bytes()
    helper = SimpleNamespace(execution_binding=lambda *args: {})
    monkeypatch.setattr(postgrade, "modules", lambda: (partition, None, None, helper, cf))
    monkeypatch.setattr(postgrade, "bound_row", lambda *args: {})
    monkeypatch.setattr(postgrade, "request_binding", lambda *args: {})
    context = SimpleNamespace(
        audit={"controls_reused": True}, completion={"execution": {}, "native_reward": 0},
        native={"execution_id": EXECUTION}, request={"source_execution_id": "synthetic-original"},
    )
    monkeypatch.setattr(postgrade, "checked_context", lambda *args: context)
    if refusal == "floor":
        monkeypatch.setattr(cf, "CLOUD_USD_PER_EXECUTION", 0.05)
    monkeypatch.setattr(sys, "argv", [
        "postgrade_evaluation.py", "--requests", str(tmp_path / "requests.jsonl"),
        "--executions", str(tmp_path / "executions.jsonl"), "--execution", EXECUTION,
        "--out", str(output), "--budget", str(budget), "--global-ledger", str(ledger),
        "--max-usd", "0.04", "--reserve-usd", "0.04", "--approval", APPROVAL,
    ])

    assert postgrade.main() == (3 if refusal else 0)
    plan = json.loads((output / "plan.json").read_text())
    receipt = json.loads((output / "receipt.json").read_text())
    if refusal:
        assert plan["status"] == "refused_actual_prerequisite"
        assert receipt["status"] == "refused_actual_prerequisite"
        assert plan["problems"][0]["type"] == "ValueError"
        expected = "below the native CF execution floor" if refusal == "floor" else "already consumed"
        assert expected in plan["problems"][0]["message"]
    else:
        assert plan["status"] == "ready_not_executed"
        assert plan["budget"]["additional_reservation_usd"] == 0.04
        assert plan["budget"]["initial_state"]["cap_usd"] == 0.04
        assert plan["budget"]["initial_state"]["usd_per_execution"] == 0.04
        assert plan["budget"]["initial_state"]["reserved_usd"] == 0
        assert receipt["status"] == "plan_only_not_executed"
    assert receipt["new_native_replay_reservations"] == 0
    assert receipt["native_matrix_dispatch_attempts"] == 0
    assert receipt["provider_calls_this_invocation"] == 0
    assert receipt["training_exports"] == 0
    assert budget.read_bytes() == original_budget
    assert ledger.read_bytes() == original_ledger
