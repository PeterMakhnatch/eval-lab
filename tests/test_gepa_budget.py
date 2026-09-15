"""Aggregate limits survive races, interruptions and campaign resumption."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from evallab.gepa_optimizer.budget import (
    AggregateBudget,
    BudgetExhausted,
    DuplicateReservationError,
)


def budget(path: Path, **limits) -> AggregateBudget:
    return AggregateBudget(
        path,
        **{
            "max_target_attempts": 2,
            "max_proposer_requests": 2,
            "max_proposer_cost_usd": 0.1,
            **limits,
        },
    )


def test_shared_attempt_limit_survives_reinstantiation(tmp_path):
    first = budget(tmp_path)
    first.reserve("target", "failed-job")
    first.complete("target", "failed-job", status="error")
    resumed = budget(tmp_path)
    resumed.reserve("target", "next-job")
    with pytest.raises(BudgetExhausted):
        first.reserve("target", "extra-job")
    assert resumed.summary()["target"] == {
        "reserved": 2,
        "completed": 0,
        "unsettled": 1,
        "errors": 1,
    }


def test_unknown_proposer_outcome_is_charged_and_not_retried(tmp_path):
    budget(tmp_path).reserve("proposer", "unknown-response")
    resumed = budget(tmp_path)
    with pytest.raises(DuplicateReservationError):
        resumed.reserve("proposer", "unknown-response")
    with pytest.raises(BudgetExhausted):
        resumed.reserve("proposer", "different-prompt")
    assert resumed.summary()["proposer"]["reserved"] == 1
    assert resumed.summary()["proposer"]["complete_estimated_cost_usd"] is None


def test_unknown_cost_stops_new_proposals_without_blocking_target_receipts(tmp_path):
    ledger = budget(tmp_path)
    ledger.reserve("proposer", "response")
    ledger.complete("proposer", "response", status="completed")
    with pytest.raises(BudgetExhausted):
        ledger.reserve("proposer", "next")
    ledger.reserve("target", "candidate")
    ledger.complete("target", "candidate", status="completed")
    report = ledger.summary()
    assert report["proposer"]["missing_cost_count"] == 1
    assert report["proposer"]["complete_estimated_cost_usd"] is None
    assert report["target"]["completed"] == 1


def test_reported_cost_cap_stops_before_next_request(tmp_path):
    ledger = budget(tmp_path)
    ledger.reserve("proposer", "first")
    ledger.complete("proposer", "first", status="completed", estimated_cost_usd=0.1)
    with pytest.raises(BudgetExhausted):
        ledger.reserve("proposer", "second")
    assert ledger.summary()["proposer"]["reserved"] == 1


def test_changed_limits_and_missing_state_never_reset_spend(tmp_path):
    ledger = budget(tmp_path)
    ledger.reserve("target", "one")
    with pytest.raises(ValueError):
        budget(tmp_path, max_target_attempts=3)
    ledger.state_path.unlink()
    with pytest.raises(ValueError):
        budget(tmp_path)


def test_last_slot_race_between_distinct_instances(tmp_path):
    budget(tmp_path)

    def attempt(index):
        try:
            budget(tmp_path).reserve("target", str(index))
            return True
        except BudgetExhausted:
            return False

    with ThreadPoolExecutor(max_workers=8) as workers:
        outcomes = list(workers.map(attempt, range(16)))
    assert sum(outcomes) == 2
    assert budget(tmp_path).summary()["target"]["reserved"] == 2


def test_settlement_replay_is_idempotent_but_cannot_rewrite_outcome(tmp_path):
    ledger = budget(tmp_path)
    ledger.reserve("target", "one")
    ledger.complete("target", "one", status="completed", metadata={"job": "native-job"})
    budget(tmp_path).complete("target", "one", status="completed", metadata={"job": "native-job"})
    with pytest.raises(ValueError):
        ledger.complete("target", "one", status="error")
    assert ledger.summary()["target"]["completed"] == 1


def test_nonfinite_limits_and_symlink_ancestors_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        budget(tmp_path / "nan", max_proposer_cost_usd=float("nan"))
    with pytest.raises(ValueError):
        budget(tmp_path / "fractional", max_target_attempts=2.5)
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError):
        budget(alias / "nested")
