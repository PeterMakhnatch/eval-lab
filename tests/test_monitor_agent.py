"""Lifetime budget regressions without provider calls."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from evallab.interpretation import monitor_agent as agent
from evallab.interpretation.monitor_agent import (
    BudgetExhausted,
    InvestigationBudget,
    ModelCompletion,
)
from evallab.researchers import CallLedger


def _budget(path: Path, *, dollars: float = 10, calls: int = 100, input_rate: float = 1, output_rate: float = 2):
    return InvestigationBudget(
        path, budget_usd=dollars, max_calls=calls,
        input_usd_per_million=input_rate, output_usd_per_million=output_rate,
    )


def test_concurrent_reservations_obey_one_lifetime_cap(tmp_path):
    path = tmp_path / "spend.jsonl"
    barrier = threading.Barrier(8)

    def reserve(index):
        budget = _budget(path, calls=3)
        barrier.wait(timeout=10)
        try:
            budget.reserve(pass_id=f"case-{index}", payload={"model": "test", "max_tokens": 128}, max_output_tokens=128)
            return "reserved"
        except BudgetExhausted:
            return "denied"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(reserve, range(8)))
    assert outcomes.count("reserved") == 3
    assert outcomes.count("denied") == 5
    assert _budget(path, calls=3).totals()[0] == 3


def test_lifetime_call_cap_has_no_midnight_reset_and_config_cannot_change(tmp_path, monkeypatch):
    path = tmp_path / "spend.jsonl"
    budget = _budget(path, calls=1)

    class Clock:
        day = 1

        @classmethod
        def now(cls, tz):
            return datetime(2026, 10, cls.day, tzinfo=UTC)

    monkeypatch.setattr(agent, "datetime", Clock)
    budget.reserve(pass_id="one", payload={"max_tokens": 128}, max_output_tokens=128)
    Clock.day = 2
    with pytest.raises(BudgetExhausted):
        _budget(path, calls=1).reserve(pass_id="two", payload={"max_tokens": 128}, max_output_tokens=128)
    with pytest.raises(ValueError, match="immutable"):
        _budget(path, calls=2)
    with pytest.raises(ValueError, match="immutable"):
        _budget(path, calls=1, input_rate=0.5)
    with pytest.raises(ValueError, match="immutable"):
        _budget(path, calls=1, dollars=20)


def test_dollar_cap_is_exact_at_reservation_boundary(tmp_path):
    payload = {"max_tokens": 128}
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    charge = (Decimal(len(encoded) + 1024) + Decimal(128) * 2) / 1_000_000
    budget = _budget(tmp_path / "spend.jsonl", dollars=float(charge), calls=3)
    reservation = budget.reserve(pass_id="one", payload=payload, max_output_tokens=128)
    budget.finish(reservation, ModelCompletion("{}", input_tokens=0, output_tokens=0), failed=True)
    assert budget.totals() == (1, float(charge))
    with pytest.raises(BudgetExhausted):
        _budget(tmp_path / "spend.jsonl", dollars=float(charge), calls=3).reserve(pass_id="two", payload=payload, max_output_tokens=128)


@pytest.mark.parametrize("suffix", ["", ".config.json", ".hold.json"])
def test_budget_state_symlink_is_rejected_without_touching_target(tmp_path, suffix):
    target = tmp_path / "immutable-source.json"
    target.write_text("IMMUTABLE SOURCE BYTES\\n")
    path = tmp_path / "spend.jsonl"
    Path(str(path) + suffix).symlink_to(target)
    with pytest.raises(ValueError, match="symlinks"):
        _budget(path)
    assert target.read_text() == "IMMUTABLE SOURCE BYTES\\n"


@pytest.mark.parametrize("observed_usage", [None, (None, 20), (10, None), (0, 0), (10, 20), (-1, 20), (True, 20)])
def test_unknown_usage_is_not_zero_and_known_zero_does_not_refund(tmp_path, observed_usage):
    path = tmp_path / "spend.jsonl"
    budget = _budget(path)
    reservation = budget.reserve(pass_id="one", payload={"max_tokens": 128}, max_output_tokens=128)
    budget.finish(reservation, None, failed=False, observed_usage=observed_usage)
    record = CallLedger(path).records()[-1]
    observed = json.loads(record.reason)["observed"]
    input_count, output_count = observed_usage or (None, None)
    if isinstance(input_count, bool) or input_count is not None and input_count < 0:
        input_count = None
    assert observed["input_tokens"] == input_count and observed["output_tokens"] == output_count
    if input_count is None or output_count is None:
        assert record.usage is None and observed["estimated_usage_usd"] is None
    else:
        assert record.usage.input_tokens == input_count and record.usage.output_tokens == output_count
        assert observed["estimated_usage_usd"] == (input_count + 2 * output_count) / 1_000_000
    assert budget.totals("one") == (1, reservation.reserved_usd)
    assert budget.totals("other") == (0, 0)


@pytest.mark.parametrize("usage", [(1_000_000, 20), (30, 3000), (None, 3000)])
@pytest.mark.parametrize("completion", [False, True])
def test_observed_usage_breach_is_accounted_and_holds_all_later_cases_after_restart(tmp_path, usage, completion):
    path = tmp_path / "spend.jsonl"
    budget = _budget(path)
    reservation = budget.reserve(pass_id="one", payload={"max_tokens": 128}, max_output_tokens=128)
    budget.finish(
        reservation, ModelCompletion("{}", input_tokens=usage[0], output_tokens=usage[1]) if completion else None,
        failed=False, observed_usage=usage,
    )
    hold = json.loads(budget.hold_path.read_text())
    record = CallLedger(path).records()[-1]
    assert hold["reason"] == "usage_bound_breach" and record.event == "failed"
    assert hold["observed"]["input_tokens"] == usage[0] and hold["observed"]["output_tokens"] == usage[1]
    assert json.loads(record.reason)["observed"] == hold["observed"]
    before = budget.totals()
    with pytest.raises(BudgetExhausted, match="budget_on_hold_usage_bound_breach"):
        _budget(path).reserve(pass_id="another-case", payload={"max_tokens": 128}, max_output_tokens=128)
    assert _budget(path).totals() == before
