"""Offline HAR-191 admission/crash accounting using real local JSON journals.

No Modal objects, provider calls, task-code execution, or scientific trajectories.
Billing fixtures are retained-row data; tests exercise cost decisions, not SDK echoes.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "research/experiments/leak-oracle/budget.py"
spec = importlib.util.spec_from_file_location("leak_oracle_budget", SCRIPT)
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)

RATES = {"cpu_hour_cost_sandbox": "0.141900", "mem_gib_hour_cost_sandbox": "0.024000"}
MANIFEST = "a" * 64
AUTHORITY = "HAR-191 scoped CPU verifier replay, 2026-10-07T04:06Z"
START = datetime(2026, 10, 7, 4, 6, tzinfo=UTC)


def row(app: str = "ap-study", cost: str = "0", **changes) -> dict:
    value = {
        "object_id": app,
        "description": "har191-oracle-sweep",
        "environment_name": "main",
        "interval_start": START.replace(minute=0).isoformat(),
        "cost": cost,
        "cost_by_resource": {"cpu": cost},
        "tags": {"linear_card": "HAR-191"},
    }
    value.update(changes)
    return value


def snapshot(cost: str = "0", *, apps: list[str] | None = None, rows: list | None = None) -> dict:
    owned = apps or ["ap-study"]
    if rows is None:
        rows = [] if Decimal(cost) == 0 else [row(owned[0], cost)]
    return budget._billing_rows_snapshot(
        rows, owned, START, START + timedelta(minutes=1), START + timedelta(minutes=1, seconds=1)
    )


def ledger(tmp_path: Path, rates: dict | None = None) -> budget.BudgetLedger:
    return budget.BudgetLedger(tmp_path / "budget.json", rates or RATES, MANIFEST, AUTHORITY)


def tasks(count: int, offset: int = 0) -> list[str]:
    return [f"format-code-task-{i + offset:06d}" for i in range(count)]


def terminal(index: int, seconds: int = 180, **changes) -> dict:
    value = {
        "sandbox_id": f"sb-local-{index}",
        "creation_attempted": True,
        "terminal_confirmed": True,
        "lifetime_upper_seconds": seconds,
        "creation_started_at": START.isoformat(),
        "terminated_at": (START + timedelta(seconds=seconds)).isoformat(),
        "build_seconds": 31.25,
        "error": None,
    }
    value.update(changes)
    return value


def never_allocated() -> dict:
    return {
        "sandbox_id": None,
        "creation_attempted": False,
        "terminal_confirmed": True,
        "lifetime_upper_seconds": 0,
        "error": "Local image preflight refused before create",
    }


def finish_all(
    value: budget.BudgetLedger,
    batch: dict,
    seconds: int = 180,
    offset: int = 0,
    observation: dict | None = None,
) -> dict:
    reports = [terminal(offset + i, seconds) for i in range(batch["sandbox_count"])]
    observation = observation or snapshot(apps=batch["snapshot_before"]["app_ids"])
    return value.finish(batch["batch_id"], observation, reports)


def test_decimal_physical_core_and_hard_memory_rates() -> None:
    # Observed SDK Sandbox rates, not the cheaper function CPU/memory rates.
    assert budget.per_sandbox_worst_case(RATES) == Decimal("0.011895")
    alternate = {**RATES, "cpu_hour_cost": "0.00001", "mem_gib_hour_cost": "0.00001"}
    assert budget.per_sandbox_worst_case(alternate) == Decimal("0.011895")
    with localcontext() as context:
        context.prec = 3
        assert budget.per_sandbox_worst_case(RATES) == Decimal("0.011895")


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"cpu_hour_cost": "0.1", "mem_gib_hour_cost": "0.1"},
        {**RATES, "cpu_hour_cost_sandbox": "0"},
        {**RATES, "mem_gib_hour_cost_sandbox": "-1"},
        {**RATES, "cpu_hour_cost_sandbox": "NaN"},
        {**RATES, "cpu_hour_cost_sandbox": "Infinity"},
        {**RATES, "cpu_hour_cost_sandbox": "unknown"},
        {**RATES, "cpu_hour_cost_sandbox": 0.1419},
    ],
)
def test_missing_invalid_nonpositive_sandbox_rates_refuse(bad) -> None:
    with pytest.raises(budget.BudgetError):
        budget.per_sandbox_worst_case(bad)


def test_84_paired_tasks_cover_both_arms_and_85_refuse(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    with pytest.raises(budget.BudgetError, match="exceed"):
        value.reserve(tasks(85), 170, snapshot(), app_id="ap-study")
    assert value.batches == []
    batch = value.reserve(tasks(84), 168, snapshot(), app_id="ap-study")
    assert Decimal(batch["reserved_usd"]) == Decimal("1.99836")
    assert value.exposure_usd == Decimal("1.99836")
    assert value.pending
    # The admitted record exists on disk before any Sandbox could be created.
    stored = json.loads(value.path.read_text())
    assert stored["batches"][0]["batch_id"] == batch["batch_id"]
    assert stored["batches"][0]["sandbox_count"] == 168


def test_exact_cap_and_sub_decimal_epsilon_boundary(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    batch = value.reserve(tasks(84), 168, snapshot("0.00164"), app_id="ap-study")
    assert value.exposure_usd == Decimal("1.99836")
    assert Decimal(batch["actual_before_usd"]) + Decimal(batch["reserved_usd"]) == budget.CAP_USD
    other = tmp_path / "other"
    other.mkdir()
    second = ledger(other)
    with pytest.raises(budget.BudgetError, match="exceed"):
        second.reserve(
            tasks(84), 168, snapshot("0.0016400000000000000000000000001"), app_id="ap-study"
        )
    resumed = ledger(other)
    assert resumed.actual_usd == Decimal("0.0016400000000000000000000000001")
    assert resumed.batches == []


@pytest.mark.parametrize(
    "task_ids,count,kind",
    [
        ([], 0, "locked"),
        (["a", "a"], 4, "locked"),
        (["a"], 1, "locked"),
        (["a"], 2, "open"),
        (["a"], True, "open"),
        (tasks(101), 202, "locked"),
        (["a"], 2, "typo"),
    ],
)
def test_invalid_batch_shape_never_reserves(tmp_path: Path, task_ids, count, kind) -> None:
    value = ledger(tmp_path)
    with pytest.raises(budget.BudgetError):
        value.reserve(task_ids, count, snapshot(), kind=kind, app_id="ap-study")
    assert value.batches == []
    assert not value.pending


def test_100_task_limit_is_distinct_from_dollar_boundary(tmp_path: Path) -> None:
    cheap = {"cpu_hour_cost_sandbox": "0.001", "mem_gib_hour_cost_sandbox": "0.001"}
    value = ledger(tmp_path, cheap)
    batch = value.reserve(tasks(100), 200, snapshot(), app_id="ap-study")
    assert batch["sandbox_count"] == 200
    assert value.exposure_usd == Decimal("0.05")


def test_reserved_crash_and_unknown_allocation_cannot_relaunch(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    batch = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    restarted = ledger(tmp_path)
    assert restarted.pending
    assert restarted.exposure_usd == Decimal("0.02379")
    with pytest.raises(budget.BudgetError, match="Pending"):
        restarted.reserve(["b"], 2, snapshot(), app_id="ap-study")
    unknown = {
        "sandbox_id": None,
        "creation_attempted": True,
        "terminal_confirmed": False,
        "lifetime_upper_seconds": 180,
        "error": "Create request timed out; allocation unknown",
    }
    result = restarted.finish(batch["batch_id"], snapshot(), [unknown, terminal(1, 180)])
    assert result["status"] == "pending"
    assert result["pending_reasons"]
    again = ledger(tmp_path)
    assert again.pending
    assert again.exposure_usd == Decimal("0.02379")
    with pytest.raises(budget.BudgetError, match="Pending"):
        again.reserve(["b"], 2, snapshot(), app_id="ap-study")


def test_missing_report_and_false_cleanup_remain_full_bound(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    batch = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    value.finish(batch["batch_id"], snapshot(), [terminal(0, 180, terminal_confirmed=False)])
    assert value.pending
    assert value.exposure_usd == Decimal("0.02379")
    assert ledger(tmp_path).pending


def test_pending_cleanup_can_be_reconciled_but_id_cannot_be_replaced(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    batch = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    reports = [terminal(0, terminal_confirmed=False), terminal(1, 30)]
    value.finish(batch["batch_id"], snapshot(), reports)
    resumed = ledger(tmp_path)
    with pytest.raises(budget.BudgetError, match="replace"):
        resumed.finish(batch["batch_id"], snapshot(), [terminal(2), terminal(1, 30)])
    result = resumed.finish(batch["batch_id"], snapshot(), [terminal(0, 60), terminal(1, 30)])
    assert result["status"] == "finished"
    assert len(result["finish_observations"]) == 2
    assert not resumed.pending
    assert resumed.exposure_usd == Decimal("0.0059475")
    assert not ledger(tmp_path).pending


def test_completed_runtime_upper_bound_remains_during_billing_lag(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    batch = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    result = finish_all(value, batch, 90)
    assert result["sandboxes"][0]["build_seconds"] == 31.25
    assert value.actual_usd == 0
    assert value.exposure_usd == Decimal("0.011895")
    assert not value.pending
    resumed = ledger(tmp_path)
    assert resumed.exposure_usd == Decimal("0.011895")
    next_batch = resumed.reserve(
        ["b"], 2, snapshot(apps=["ap-study", "ap-second"]), app_id="ap-second"
    )
    assert Decimal(next_batch["exposure_before_usd"]) == Decimal("0.011895")
    assert resumed.exposure_usd == Decimal("0.035685")


def test_timestamp_span_is_not_lowered_by_underreported_runtime(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    batch = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    report = terminal(
        0, 1, terminated_at=(START + timedelta(seconds=89, microseconds=1)).isoformat()
    )
    value.finish(batch["batch_id"], snapshot(), [report, terminal(1, 90)])
    assert value.exposure_usd == Decimal("0.011895")


def test_one_second_bound_rounds_up_and_is_context_independent(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    batch = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    with localcontext() as context:
        context.prec = 3
        finish_all(value, batch, 1)
    with localcontext() as context:
        context.prec = 100
        exact = Decimal("0.2379") * 2 / 3600
        assert value.exposure_usd >= exact
        assert value.exposure_usd - exact < Decimal("1e-48")


def test_proven_no_attempt_zero_does_not_erase_observed_build_charges(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    batch = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    value.finish(batch["batch_id"], snapshot("0.0042"), [never_allocated(), never_allocated()])
    assert not value.pending
    assert value.actual_usd == Decimal("0.0042")
    assert value.exposure_usd == Decimal("0.0042")
    assert ledger(tmp_path).exposure_usd == Decimal("0.0042")


def test_observed_cost_is_high_water_not_added_twice_to_runtime(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    batch = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    finish_all(value, batch, 180, observation=snapshot("0.03"))
    assert value.exposure_usd == Decimal("0.03")
    second = value.reserve(
        ["b"],
        2,
        snapshot("0.01", apps=["ap-study", "ap-second"]),
        app_id="ap-second",
    )
    assert second["actual_before_usd"] == "0.03"
    assert second["exposure_before_usd"] == "0.03"
    assert ledger(tmp_path).actual_usd == Decimal("0.03")
    assert ledger(tmp_path).exposure_usd == Decimal("0.05379")


def test_late_actual_over_cap_is_recorded_then_next_batch_refused(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    batch = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    finish_all(value, batch, 1, observation=snapshot("2.01"))
    assert value.exposure_usd == Decimal("2.01")
    with pytest.raises(budget.BudgetError, match="exceed"):
        value.reserve(
            ["b"], 2, snapshot("2.02", apps=["ap-study", "ap-second"]), app_id="ap-second"
        )
    assert ledger(tmp_path).actual_usd == Decimal("2.02")
    assert len(value.batches) == 1


def test_distinct_open_limit_and_duplicate_admission_survive_resume(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    locked = value.reserve(tasks(20), 40, snapshot(), app_id="ap-study")
    finish_all(value, locked, 1)
    opened = value.reserve(
        tasks(20),
        20,
        snapshot(apps=["ap-study", "ap-open"]),
        kind="open",
        app_id="ap-open",
    )
    finish_all(value, opened, 1, offset=100)
    resumed = ledger(tmp_path)
    assert resumed.open_task_ids == set(tasks(20))
    with pytest.raises(budget.BudgetError, match="Duplicate"):
        resumed.reserve([tasks(20)[0]], 1, snapshot(), kind="open", app_id="ap-study")
    with pytest.raises(budget.BudgetError, match="20"):
        resumed.reserve(tasks(1, 20), 1, snapshot(), kind="open", app_id="ap-study")
    with pytest.raises(budget.BudgetError, match="Duplicate"):
        resumed.reserve([tasks(20)[0]], 2, snapshot(), app_id="ap-study")


def test_duplicate_sandbox_and_extra_allocation_are_not_hidden(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    batch = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    result = value.finish(batch["batch_id"], snapshot(), [terminal(0), terminal(0), terminal(2)])
    assert result["status"] == "pending"
    assert value.exposure_usd == Decimal("0.035685")
    assert ledger(tmp_path).exposure_usd == Decimal("0.035685")


@pytest.mark.parametrize(
    "mutation",
    [
        {"terminal_confirmed": True, "terminated_at": None},
        {"sandbox_id": None},
        {"lifetime_upper_seconds": -1},
        {"lifetime_upper_seconds": True},
        {"lifetime_upper_seconds": 181},
        {"terminated_at": (START - timedelta(seconds=1)).isoformat()},
    ],
)
def test_invalid_terminal_evidence_keeps_full_reservation(tmp_path: Path, mutation: dict) -> None:
    value = ledger(tmp_path)
    batch = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    value.finish(batch["batch_id"], snapshot(), [terminal(0, 1, **mutation), terminal(1)])
    assert value.pending
    assert value.exposure_usd == Decimal("0.02379")
    assert ledger(tmp_path).pending


def test_report_exact_scope_ceil_hour_and_missing_rows_not_settled(tmp_path: Path) -> None:
    # The prior pilot and a same-description foreign app must not seed our cap.
    source = [
        row("ap-prior-pilot", "0.04174183"),
        row("ap-study", "0.010"),
        row("ap-foreign", "1234"),
        row("workspace-credit", "-5000"),
    ]
    path = tmp_path / "retained-billing.json"
    path.write_text(json.dumps(source))
    result = snapshot(rows=json.loads(path.read_text()), apps=["ap-study", "ap-new-study"])
    assert result["actual_usd"] == "0.01"
    assert result["window_start"] == "2026-10-07T04:00:00Z"
    assert result["window_end"] == "2026-10-07T05:00:00Z"
    assert [r["object_id"] for r in result["rows"]] == ["ap-study"]
    assert result["missing_app_ids"] == ["ap-new-study"]
    assert result["billing_settled"] is False
    assert result["partial_current_hour"] is True
    assert result["rows"][0]["tags"] == {"linear_card": "HAR-191"}
    boundary = datetime(2026, 10, 7, 5, tzinfo=UTC)
    at_hour = budget._billing_rows_snapshot([], ["ap-study"], START, boundary, boundary)
    assert at_hour["window_end"] == "2026-10-07T05:00:00Z"
    assert at_hour["partial_current_hour"] is False
    assert at_hour["billing_settled"] is False


def test_resource_breakdown_never_uses_net_credit_and_negative_owned_refuses() -> None:
    result = snapshot(rows=[row(cost="0.01", cost_by_resource={"cpu": "0.02", "memory": "0.03"})])
    assert result["actual_usd"] == "0.05"
    assert result["rows"][0]["cost"] == "0.01"
    with pytest.raises(budget.BudgetError, match="nonnegative"):
        snapshot(rows=[row(cost="-1")])
    with pytest.raises(budget.BudgetError, match="nonnegative"):
        snapshot(rows=[row(cost_by_resource={"credit": "-1"})])
    with pytest.raises(budget.BudgetError, match="Duplicate"):
        snapshot(rows=[row(), row()])


@pytest.mark.parametrize(
    "field,value",
    [
        ("actual_usd", None),
        ("actual_usd", "NaN"),
        ("actual_usd", "-1"),
        ("rows", None),
        ("app_ids", []),
        ("window_end", "2026-10-07T04:59:00Z"),
    ],
)
def test_unknown_cost_or_malformed_snapshot_refuses_before_admission(
    tmp_path: Path, field, value
) -> None:
    journal = ledger(tmp_path)
    observation = snapshot()
    observation[field] = value
    with pytest.raises(budget.BudgetError):
        journal.reserve(["a"], 2, observation, app_id="ap-study")
    assert journal.batches == []


def test_billing_scope_cannot_shrink_but_new_owned_app_is_allowed(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    batch = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    finish_all(value, batch, 1)
    with pytest.raises(budget.BudgetError, match="omitted"):
        value.reserve(["b"], 2, snapshot(apps=["ap-replacement"]), app_id="ap-replacement")
    changed_start = snapshot()
    changed_start["window_start"] = "2026-10-07T03:00:00Z"
    with pytest.raises(budget.BudgetError, match="start changed"):
        value.reserve(["b"], 2, changed_start, app_id="ap-new")
    admitted = value.reserve(["b"], 2, snapshot(apps=["ap-study", "ap-new"]), app_id="ap-new")
    assert admitted["snapshot_before"]["app_ids"] == ["ap-study", "ap-new"]
    assert ledger(tmp_path).pending


@pytest.mark.parametrize("identity", ["manifest", "rates", "authority"])
def test_resume_identity_change_refuses_without_reset(tmp_path: Path, identity: str) -> None:
    value = ledger(tmp_path)
    value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    original = value.path.read_bytes()
    rates = {**RATES, "cpu_hour_cost_sandbox": "0.142"} if identity == "rates" else RATES
    manifest = "b" * 64 if identity == "manifest" else MANIFEST
    authority = "different study" if identity == "authority" else AUTHORITY
    with pytest.raises(budget.BudgetError, match="mismatch"):
        budget.BudgetLedger(value.path, rates, manifest, authority)
    assert value.path.read_bytes() == original


def test_corrupt_truncated_and_underreserved_journal_cannot_resume(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    state = json.loads(value.path.read_text())
    state["batches"][0]["retained_upper_usd"] = "0"
    value.path.write_text(json.dumps(state))
    with pytest.raises(budget.BudgetError, match="inconsistent"):
        ledger(tmp_path)
    value.path.write_text('{"schema_version": 1,')
    with pytest.raises(budget.BudgetError, match="Unreadable"):
        ledger(tmp_path)


def test_stale_writer_and_mutable_return_values_cannot_erase_reservation(tmp_path: Path) -> None:
    first = ledger(tmp_path)
    stale = ledger(tmp_path)
    batch = first.reserve(["a"], 2, snapshot(), app_id="ap-study")
    batch["task_ids"].clear()
    exposed = first.batches
    exposed.clear()
    assert first.batches[0]["task_ids"] == ["a"]
    with pytest.raises(budget.BudgetError, match="changed outside"):
        stale.reserve(["b"], 2, snapshot(), app_id="ap-study")
    assert ledger(tmp_path).pending


def test_failure_before_atomic_replace_returns_no_admission(tmp_path: Path, monkeypatch) -> None:
    value = ledger(tmp_path)
    original = value.path.read_bytes()

    def fail_replace(source, destination):
        raise OSError("simulated crash before rename")

    with monkeypatch.context() as patch:
        patch.setattr(budget.os, "replace", fail_replace)
        with pytest.raises(OSError, match="before rename"):
            value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    assert value.path.read_bytes() == original
    assert not ledger(tmp_path).pending
    with pytest.raises(budget.BudgetError, match="write failed"):
        value.reserve(["b"], 2, snapshot(), app_id="ap-study")


def test_failure_after_rename_leaves_reservation_for_crash_resume(
    tmp_path: Path, monkeypatch
) -> None:
    value = ledger(tmp_path)
    real_fsync = budget.os.fsync
    calls = 0

    def fail_directory_fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated crash after rename")
        real_fsync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(budget.os, "fsync", fail_directory_fsync)
        with pytest.raises(OSError, match="after rename"):
            value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    restarted = ledger(tmp_path)
    assert restarted.pending
    assert restarted.exposure_usd == Decimal("0.02379")
    with pytest.raises(budget.BudgetError, match="Pending"):
        restarted.reserve(["b"], 2, snapshot(), app_id="ap-study")


def test_unknown_finish_cost_leaves_original_pending_reservation(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    batch = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    missing_cost = copy.deepcopy(snapshot())
    missing_cost["actual_usd"] = None
    with pytest.raises(budget.BudgetError):
        value.finish(batch["batch_id"], missing_cost, [terminal(0), terminal(1)])
    resumed = ledger(tmp_path)
    assert resumed.pending
    assert resumed.exposure_usd == Decimal("0.02379")


def test_reconciliation_cannot_reduce_a_known_completed_runtime(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    batch = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    value.finish(
        batch["batch_id"], snapshot(), [terminal(0, 60), terminal(1, terminal_confirmed=False)]
    )
    resumed = ledger(tmp_path)
    result = resumed.finish(batch["batch_id"], snapshot(), [terminal(0, 1), terminal(1, 30)])
    assert result["status"] == "finished"
    assert resumed.exposure_usd == Decimal("0.0059475")
    assert ledger(tmp_path).exposure_usd == Decimal("0.0059475")


def test_recovery_cannot_erase_unknown_allocation_slots_or_relaunch(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    batch = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    unknown = {
        "sandbox_id": None,
        "creation_attempted": True,
        "terminal_confirmed": False,
        "lifetime_upper_seconds": 180,
    }
    value.finish(batch["batch_id"], snapshot(), [unknown, terminal(1), copy.deepcopy(unknown)])
    with pytest.raises(budget.BudgetError, match="allocation slot"):
        value.finish(batch["batch_id"], snapshot(), [terminal(0), terminal(1)])
    with pytest.raises(budget.BudgetError, match="non-attempt"):
        value.finish(batch["batch_id"], snapshot(), [never_allocated(), terminal(1), unknown])
    assert ledger(tmp_path).pending


def test_zero_reported_runtime_for_actual_sandbox_is_not_optimistic_zero(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    batch = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    value.finish(batch["batch_id"], snapshot(), [terminal(0, 0), terminal(1, 0)])
    assert not value.pending
    assert value.exposure_usd > 0
    assert ledger(tmp_path).exposure_usd == value.exposure_usd


def test_foreign_rows_and_net_actual_cannot_enter_ledger_snapshot(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    foreign = snapshot()
    foreign["rows"] = [row("ap-prior-pilot", "0.04174183")]
    with pytest.raises(budget.BudgetError, match="unowned"):
        value.reserve(["a"], 2, foreign, app_id="ap-study")
    net = snapshot("0.03")
    net["actual_usd"] = "0.001"
    with pytest.raises(budget.BudgetError, match="gross"):
        value.reserve(["a"], 2, net, app_id="ap-study")
    assert value.actual_usd == 0
    assert value.batches == []


def test_finished_batch_evidence_cannot_be_replaced(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    batch = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    finish_all(value, batch)
    with pytest.raises(budget.BudgetError, match="already finished"):
        value.finish(batch["batch_id"], snapshot(), [never_allocated(), never_allocated()])
    assert ledger(tmp_path).exposure_usd == Decimal("0.02379")


def test_cross_batch_delayed_billing_never_swallows_prior_observed_overhead(tmp_path: Path) -> None:
    rates = {"cpu_hour_cost_sandbox": "0.5", "mem_gib_hour_cost_sandbox": "0.125"}
    value = ledger(tmp_path, rates)
    first = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    finish_all(value, first, observation=snapshot("0.17"))
    assert value.exposure_usd == Decimal("0.17")
    second_snapshot = snapshot("0.17", apps=["ap-study", "ap-second"])
    second = value.reserve(["b"], 2, second_snapshot, app_id="ap-second")
    assert Decimal(second["reserved_usd"]) == Decimal("0.10")
    assert value.exposure_usd == Decimal("0.27")
    finish_all(value, second, offset=10, observation=second_snapshot)
    assert value.actual_usd == Decimal("0.17")
    assert value.exposure_usd == Decimal("0.27")
    resumed = ledger(tmp_path, rates)
    assert resumed.exposure_usd == Decimal("0.27")
    assert [batch["app_id"] for batch in resumed.batches] == ["ap-study", "ap-second"]


def test_cross_batch_overhead_enforces_cap_with_another_unposted_batch(tmp_path: Path) -> None:
    rates = {"cpu_hour_cost_sandbox": "0.5", "mem_gib_hour_cost_sandbox": "0.125"}
    value = ledger(tmp_path, rates)
    first = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    finish_all(value, first, observation=snapshot("1.87"))
    two_apps = snapshot("1.87", apps=["ap-study", "ap-second"])
    second = value.reserve(["b"], 2, two_apps, app_id="ap-second")
    finish_all(value, second, offset=10, observation=two_apps)
    assert value.exposure_usd == Decimal("1.97")
    third_snapshot = snapshot("1.87", apps=["ap-study", "ap-second", "ap-third"])
    with pytest.raises(budget.BudgetError, match="exceed"):
        value.reserve(["c"], 2, third_snapshot, app_id="ap-third")
    assert ledger(tmp_path, rates).exposure_usd == Decimal("1.97")
    assert len(value.batches) == 2


def test_late_observe_preserves_app_attribution_without_fake_allocation(tmp_path: Path) -> None:
    rates = {"cpu_hour_cost_sandbox": "0.5", "mem_gib_hour_cost_sandbox": "0.125"}
    value = ledger(tmp_path, rates)
    first = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    finish_all(value, first)
    second = value.reserve(["b"], 2, snapshot(apps=["ap-study", "ap-second"]), app_id="ap-second")
    finish_all(value, second, offset=10)
    original_batches = value.batches
    apps = ["ap-study", "ap-second", "ap-unassigned"]
    value.observe(
        snapshot(
            apps=apps,
            rows=[
                row("ap-study", "0.17"),
                row("ap-second", "0.08"),
                row("ap-unassigned", "0.03"),
            ],
        )
    )
    assert value.actual_usd == Decimal("0.28")
    assert value.exposure_usd == Decimal("0.30")
    assert value.batches == original_batches
    assert not value.pending
    resumed = ledger(tmp_path, rates)
    assert resumed.actual_usd == Decimal("0.28")
    assert resumed.exposure_usd == Decimal("0.30")
    resumed.observe(
        snapshot(
            apps=apps,
            rows=[
                row("ap-study", "0.16"),
                row("ap-second", "0.09"),
                row("ap-unassigned", "0.02"),
            ],
        )
    )
    # Do not lose one app's previously observed charge when another app rises.
    assert resumed.actual_usd == Decimal("0.29")
    assert ledger(tmp_path, rates).exposure_usd == Decimal("0.30")
    stored = json.loads(resumed.path.read_text())
    assert stored["actual_by_app_usd"] == {
        "ap-study": "0.17",
        "ap-second": "0.09",
        "ap-unassigned": "0.03",
    }


def test_batch_requires_present_fresh_app_identity(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    with pytest.raises(TypeError, match="app_id"):
        value.reserve(["a"], 2, snapshot())
    with pytest.raises(budget.BudgetError, match="absent"):
        value.reserve(["a"], 2, snapshot(), app_id="ap-absent")
    first = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    finish_all(value, first)
    with pytest.raises(budget.BudgetError, match="already used"):
        value.reserve(["b"], 2, snapshot(), app_id="ap-study")
    resumed = ledger(tmp_path)
    with pytest.raises(budget.BudgetError, match="already used"):
        resumed.reserve(["b"], 2, snapshot(), app_id="ap-study")


def test_unattributed_aggregate_cost_refuses_instead_of_ignoring_it(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    observation = snapshot("0.01")
    observation["actual_usd"] = "0.03"
    with pytest.raises(budget.BudgetError, match="gross"):
        value.observe(observation)
    assert value.actual_usd == 0
    assert value.batches == []


def test_resume_rejects_lost_attribution_or_reused_batch_app(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    first = value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    finish_all(value, first, observation=snapshot("0.03"))
    value.reserve(["b"], 2, snapshot("0.03", apps=["ap-study", "ap-second"]), app_id="ap-second")
    original = json.loads(value.path.read_text())
    lost = copy.deepcopy(original)
    lost["actual_by_app_usd"]["ap-study"] = "0"
    value.path.write_text(json.dumps(lost))
    with pytest.raises(budget.BudgetError, match="attribution"):
        ledger(tmp_path)
    reused = copy.deepcopy(original)
    reused["batches"][1]["app_id"] = "ap-study"
    value.path.write_text(json.dumps(reused))
    with pytest.raises(budget.BudgetError, match="reused"):
        ledger(tmp_path)


def test_unbound_legacy_journal_is_not_silently_reinterpreted(tmp_path: Path) -> None:
    value = ledger(tmp_path)
    value.reserve(["a"], 2, snapshot(), app_id="ap-study")
    state = json.loads(value.path.read_text())
    state["schema_version"] = 1
    del state["batches"][0]["app_id"]
    value.path.write_text(json.dumps(state))
    with pytest.raises(budget.BudgetError, match="schema"):
        ledger(tmp_path)
