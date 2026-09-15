"""Focused regression tests for AggregateBudget.

Validates:
1. Cross-instance shared ceiling:
   Multiple AggregateBudget instances and re-instantiated sessions share the
   same durable flock/JSON state and strictly enforce aggregate ceilings.
2. Interrupted request remains charged / no automatic retry:
   An interrupted or failed reservation remains permanently charged without
   refunds. Duplicate identity halts to prevent a second physical request.
   Previous unknown proposer outcome halts subsequent proposer requests.
3. Immutable limit binding and state integrity:
   Limits cannot be modified across instances in the same directory.
   Symlinks are rejected. Invalid or corrupt retained JSON raises ValueError
   and is never silently overwritten or reset.
4. Unknown cost honesty and proposer halt:
   Missing costs remain None and are never counted as zero.
   Any unknown proposer cost halts new proposer reservations.
   Target unknown outcomes remain counted but do not halt unrelated targets.
5. Concurrent last-slot race:
   Contending threads racing for the final budget slots cannot exceed
   the configured limit; exactly the remaining capacity succeeds.
6. Cached callers modeled by no second reserve:
   Callers with cache replay bypass reserve() and succeed without charging
   the budget. Callers that fail to check cache and re-reserve the duplicate
   identity halt immediately.
"""

from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path

import pytest

from evallab.gepa_optimizer.budget import (
    AggregateBudget,
    BudgetExhausted,
    DuplicateReservationError,
)


def test_cross_instance_shared_ceiling(tmp_path: Path) -> None:
    """Distinct AggregateBudget instances and re-instantiated sessions enforce one shared ceiling."""
    budget_a = AggregateBudget(
        tmp_path,
        max_target_attempts=3,
        max_proposer_requests=2,
        max_proposer_cost_usd=10.0,
    )
    budget_b = AggregateBudget(
        tmp_path,
        max_target_attempts=3,
        max_proposer_requests=2,
        max_proposer_cost_usd=10.0,
    )

    # Interleave reservations across instances
    budget_a.reserve("target", "target-job-1")
    budget_b.reserve("target", "target-job-2")
    budget_a.reserve("target", "target-job-3")

    # The 3rd slot fills the ceiling; neither instance can exceed it
    with pytest.raises(BudgetExhausted, match="Target attempt ceiling reached"):
        budget_b.reserve("target", "target-job-4")

    with pytest.raises(BudgetExhausted, match="Target attempt ceiling reached"):
        budget_a.reserve("target", "target-job-5")

    # A third re-instantiated session pointing to the same directory observes the ceiling
    budget_c = AggregateBudget(
        tmp_path,
        max_target_attempts=3,
        max_proposer_requests=2,
        max_proposer_cost_usd=10.0,
    )
    with pytest.raises(BudgetExhausted, match="Target attempt ceiling reached"):
        budget_c.reserve("target", "target-job-6")

    summary = budget_c.summary()
    assert summary["target_reserved"] == 3
    assert summary["target_remaining_attempts"] == 0
    assert summary["target"]["reserved"] == 3


def test_interrupted_request_remains_charged_no_automatic_retry(tmp_path: Path) -> None:
    """An interrupted or ambiguous request remains permanently charged; no refund or retry."""
    budget = AggregateBudget(
        tmp_path,
        max_target_attempts=5,
        max_proposer_requests=2,
        max_proposer_cost_usd=10.0,
    )

    budget.reserve("proposer", "proposer-req-1", metadata={"prompt_hash": "abc"})
    # Reservation is recorded before side effects; status is "reserved" (outcome unknown)

    # 1. Calling reserve again with the duplicate identity MUST halt immediately
    with pytest.raises(DuplicateReservationError, match="Duplicate proposer reservation identity"):
        budget.reserve("proposer", "proposer-req-1")

    # 2. Attempting a new proposer reservation while req-1 outcome is unknown MUST halt
    with pytest.raises(BudgetExhausted, match="unknown outcome"):
        budget.reserve("proposer", "proposer-req-2")

    # 3. Simulate explicit ambiguous failure / interruption recording
    budget.complete("proposer", "proposer-req-1", status="interrupted")

    # 4. Attempting duplicate identity still halts
    with pytest.raises(DuplicateReservationError):
        budget.reserve("proposer", "proposer-req-1")

    # 5. Failed/interrupted request is NOT refunded; new proposer reservation is still halted
    with pytest.raises(BudgetExhausted, match="unknown outcome"):
        budget.reserve("proposer", "proposer-req-2")

    summary = budget.summary()
    assert summary["proposer_reserved"] == 1
    assert summary["proposer_completed"] == 0
    assert summary["proposer_unknown"] == 1
    assert summary["proposer_remaining_requests"] == 1


def test_immutable_limit_binding_and_rejection(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Budget limits are immutable once bound; symlinks and corrupt state are rejected without reset."""
    budget = AggregateBudget(
        tmp_path,
        max_target_attempts=10,
        max_proposer_requests=4,
        max_proposer_cost_usd=2.50,
    )
    budget.reserve("target", "t1")
    budget.complete("target", "t1", status="completed")

    # 1. Attempting to change limits in another instance on the same directory must fail
    with pytest.raises(ValueError, match="Immutable budget limits violated.*max_target_attempts"):
        AggregateBudget(
            tmp_path,
            max_target_attempts=20,
            max_proposer_requests=4,
            max_proposer_cost_usd=2.50,
        )

    with pytest.raises(ValueError, match="Immutable budget limits violated.*max_proposer_requests"):
        AggregateBudget(
            tmp_path,
            max_target_attempts=10,
            max_proposer_requests=8,
            max_proposer_cost_usd=2.50,
        )

    with pytest.raises(ValueError, match="Immutable budget limits violated.*max_proposer_cost_usd"):
        AggregateBudget(
            tmp_path,
            max_target_attempts=10,
            max_proposer_requests=4,
            max_proposer_cost_usd=5.00,
        )

    # 2. Symlinked directory must be rejected
    symlink_dir = tmp_path_factory.mktemp("sym_test") / "symlink_output"
    symlink_dir.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="must not be a symlink"):
        AggregateBudget(
            symlink_dir,
            max_target_attempts=10,
            max_proposer_requests=4,
            max_proposer_cost_usd=2.50,
        )

    # 3. Symlinked state file must be rejected
    other_file = tmp_path_factory.mktemp("other") / "fake.json"
    other_file.write_text("{}", encoding="utf-8")
    state_file = budget.state_path
    state_backup = state_file.read_text(encoding="utf-8")
    state_file.unlink()
    state_file.symlink_to(other_file)

    with pytest.raises(ValueError, match="must not be a symlink"):
        AggregateBudget(
            tmp_path,
            max_target_attempts=10,
            max_proposer_requests=4,
            max_proposer_cost_usd=2.50,
        )
    state_file.unlink()
    state_file.write_text(state_backup, encoding="utf-8")

    # 4. Corrupt state file must be rejected without overwriting or resetting counters
    state_file.write_text('{"limits": {"max_target_attempts": truncated...', encoding="utf-8")
    with pytest.raises(ValueError, match="Corrupt budget state"):
        AggregateBudget(
            tmp_path,
            max_target_attempts=10,
            max_proposer_requests=4,
            max_proposer_cost_usd=2.50,
        )
    # The corrupt file is preserved and not overwritten with empty counters
    assert "truncated" in state_file.read_text(encoding="utf-8")


def test_unknown_cost_honesty_and_proposer_halt(tmp_path: Path) -> None:
    """Missing costs remain None (never zero). Proposer halts on unknown cost; target does not."""
    budget = AggregateBudget(
        tmp_path,
        max_target_attempts=5,
        max_proposer_requests=3,
        max_proposer_cost_usd=5.0,
    )

    # Proposer request 1 completes, but upstream LM returns None cost (missing meter)
    budget.reserve("proposer", "prop-1")
    budget.complete("proposer", "prop-1", status="completed", estimated_cost_usd=None)

    # Summary never counts unknown cost as known zero
    summary = budget.summary()
    assert summary["known_estimated_proposer_cost_usd"] == 0.0
    assert summary["proposer_missing_cost_count"] == 1
    assert summary["has_unknown_proposer_cost"] is True
    assert summary["actual_billing_claim"] is False
    assert summary["missingness"]["proposer_missing_costs"] == 1

    # New proposer reservation MUST halt because a previous proposer cost is unknown
    with pytest.raises(BudgetExhausted, match="unknown cost"):
        budget.reserve("proposer", "prop-2")

    # Target reservation behavior: target unknown outcomes remain charged but do NOT halt unrelated targets
    budget.reserve("target", "target-fail-1")
    budget.complete("target", "target-fail-1", status="failed")

    # Target-fail-1 remains charged toward ceiling
    assert budget.summary()["target_reserved"] == 1
    assert budget.summary()["target_unknown"] == 1

    # But subsequent UNRELATED target reservations are allowed up to the target ceiling
    budget.reserve("target", "target-success-2")
    budget.complete("target", "target-success-2", status="completed")

    assert budget.summary()["target_reserved"] == 2
    assert budget.summary()["target_completed"] == 1
    assert budget.summary()["target_unknown"] == 1


def test_proposer_known_cost_cap_reached(tmp_path: Path) -> None:
    """Proposer requests halt when sum of known estimated costs reaches max_proposer_cost_usd."""
    budget = AggregateBudget(
        tmp_path,
        max_target_attempts=10,
        max_proposer_requests=5,
        max_proposer_cost_usd=2.00,
    )

    # Request 1: $1.20 estimated cost
    budget.reserve("proposer", "p1")
    budget.complete("proposer", "p1", status="completed", estimated_cost_usd=1.20)

    # Request 2: $0.90 estimated cost -> total known: $2.10 (>= $2.00 cap)
    budget.reserve("proposer", "p2")
    budget.complete("proposer", "p2", status="completed", estimated_cost_usd=0.90)

    # Request 3: even though requests (2) < max_requests (5), cost cap is reached
    with pytest.raises(BudgetExhausted, match="Proposer estimated cost cap reached"):
        budget.reserve("proposer", "p3")

    summary = budget.summary()
    assert summary["known_estimated_proposer_cost_usd"] == pytest.approx(2.10)
    assert summary["proposer_reserved"] == 2


def test_concurrent_last_slot_race(tmp_path: Path) -> None:
    """Concurrent threads contending for the last remaining slots cannot exceed ceiling."""
    capacity = 4
    total_workers = 16

    budget = AggregateBudget(
        tmp_path,
        max_target_attempts=capacity,
        max_proposer_requests=10,
        max_proposer_cost_usd=50.0,
    )

    def worker_attempt(worker_id: int) -> str:
        identity = f"concurrent-job-{worker_id}"
        try:
            budget.reserve("target", identity)
            budget.complete("target", identity, status="completed")
            return "success"
        except BudgetExhausted:
            return "exhausted"

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker_attempt, range(total_workers)))

    successes = results.count("success")
    exhausted = results.count("exhausted")

    assert successes == capacity, f"Expected exactly {capacity} successes, got {successes}"
    assert exhausted == total_workers - capacity, f"Expected {total_workers - capacity} exhausted, got {exhausted}"

    summary = budget.summary()
    assert summary["target_reserved"] == capacity
    assert summary["target_completed"] == capacity
    assert summary["target_remaining_attempts"] == 0


def test_cached_callers_modeled_by_no_second_reserve(tmp_path: Path) -> None:
    """Callers with cache replay do not call reserve(); un-cached duplicate calls halt."""
    budget = AggregateBudget(
        tmp_path,
        max_target_attempts=2,
        max_proposer_requests=5,
        max_proposer_cost_usd=10.0,
    )

    replay_cache: dict[str, str] = {}
    physical_executions: list[str] = []

    def execute_target(candidate_id: str) -> str:
        # Cache check before reserve (matches JournaledReflectionLM and evaluator contracts)
        if candidate_id in replay_cache:
            return replay_cache[candidate_id]

        budget.reserve("target", candidate_id)
        # Perform physical side-effect
        physical_executions.append(candidate_id)
        result = f"result_for_{candidate_id}"
        budget.complete("target", candidate_id, status="completed")
        replay_cache[candidate_id] = result
        return result

    # 1. First call for candidate A: cache miss -> reserves, executes, completes
    res_a1 = execute_target("cand-A")
    assert res_a1 == "result_for_cand-A"
    assert len(physical_executions) == 1
    assert budget.summary()["target_reserved"] == 1

    # 2. Replay candidate A: cache hit -> returns without reserve; budget counter untouched
    res_a2 = execute_target("cand-A")
    assert res_a2 == "result_for_cand-A"
    assert len(physical_executions) == 1
    assert budget.summary()["target_reserved"] == 1

    # 3. Call candidate B: cache miss -> reserves second slot
    res_b1 = execute_target("cand-B")
    assert res_b1 == "result_for_cand-B"
    assert len(physical_executions) == 2
    assert budget.summary()["target_reserved"] == 2
    assert budget.summary()["target_remaining_attempts"] == 0

    # 4. Replay candidate A again when budget is full: succeeds because cached callers do not reserve
    res_a3 = execute_target("cand-A")
    assert res_a3 == "result_for_cand-A"
    assert len(physical_executions) == 2

    # 5. Call candidate C: cache miss when budget is full -> raises BudgetExhausted before execution
    with pytest.raises(BudgetExhausted, match="Target attempt ceiling reached"):
        execute_target("cand-C")
    assert len(physical_executions) == 2

    # 6. Buggy caller that bypasses cache replay and attempts second reserve on cand-A halts
    with pytest.raises(DuplicateReservationError, match="Duplicate target reservation identity"):
        budget.reserve("target", "cand-A")
