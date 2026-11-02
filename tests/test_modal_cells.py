"""Unit tests for evallab.modal_cells: keyed cells, reserve/settle spend, concurrency, errors, resume."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from evallab.modal_cells import (
    CellBatchStatus,
    CellCancelledError,
    CellCapReachedError,
    CellError,
    CellInfrastructureError,
    CellSpec,
    ModalCellRunner,
    ReserveSettleSpendGate,
    SandboxMeter,
    compute_cell_key,
    estimate_modal_cost,
    is_infrastructure_error,
    modal_cell_runner,
    sha256_text,
)
from evallab.regrade import SpendCapReached


@dataclass
class FakeObservation:
    status: str
    reason: str | None
    facts: dict[str, Any]


class FakeBackend:
    """Mock ModalBackend for deterministic offline testing."""

    def __init__(self, repo_root: Path, spend_gate: Any):
        self.repo_root = repo_root
        self.spend_gate = spend_gate
        self.calls: list[dict[str, Any]] = []
        self.fail_infrastructure_attempts: int = 0
        self.fail_submission: bool = False
        self.raise_exc: BaseException | None = None

    def run_container_trial(
        self,
        package: Path,
        *,
        digest: str,
        mode: str,
        script: str | None,
        trial_dir: Path,
        timeout_seconds: int = 1800,
    ) -> FakeObservation:
        self.spend_gate.charge()
        self.calls.append({
            "package": package,
            "digest": digest,
            "script": script,
            "trial_dir": trial_dir,
        })
        if self.raise_exc is not None:
            raise self.raise_exc

        if self.fail_infrastructure_attempts > 0:
            self.fail_infrastructure_attempts -= 1
            raise CellInfrastructureError("503 Service Unavailable: Modal worker rebooted")

        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "agent").mkdir(parents=True, exist_ok=True)
        (trial_dir / "verifier").mkdir(parents=True, exist_ok=True)

        if self.fail_submission:
            (trial_dir / "agent/oracle.txt").write_text("Command failed with exit code 1\n")
            (trial_dir / "verifier/test_output.log").write_text("FAIL: test_feature\n")
            return FakeObservation(
                status="executed",
                reason="QA script exited 1",
                facts={"reward": 0.0},
            )

        (trial_dir / "agent/oracle.txt").write_text("PATCH_APPLIED\n")
        (trial_dir / "verifier/test_output.log").write_text("6 passed in 0.20s\n")
        return FakeObservation(
            status="executed",
            reason=None,
            facts={"reward": 1.0},
        )


def _make_dummy_package(path: Path, cpus: int = 2, memory_mb: int = 8192) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "task.toml").write_text(
        f'[environment]\ncpus = {cpus}\nmemory_mb = {memory_mb}\nworkdir = "/testbed"\n',
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# 1. Key stability and sensitivity
# ---------------------------------------------------------------------------


def test_key_stability_and_sensitivity(tmp_path: Path) -> None:
    pkg = _make_dummy_package(tmp_path / "pkg")
    pkg_digest = sha256_text("pkg_v1")
    variant = "reference"
    sub_digest = sha256_text("echo HELLO")
    img = "modal-default"
    hook = "pytest"

    key1 = compute_cell_key(pkg_digest, variant, sub_digest, img, hook)
    # 1. Stability: exact same inputs produce exact same key
    assert key1 == compute_cell_key(pkg_digest, variant, sub_digest, img, hook)
    assert len(key1) == 64

    # 2. Sensitivity: changing any single input alters key
    assert compute_cell_key(sha256_text("pkg_v2"), variant, sub_digest, img, hook) != key1
    assert compute_cell_key(pkg_digest, "mutant-1", sub_digest, img, hook) != key1
    assert compute_cell_key(pkg_digest, variant, sha256_text("echo BYE"), img, hook) != key1
    assert compute_cell_key(pkg_digest, variant, sub_digest, "custom-image", hook) != key1
    assert compute_cell_key(pkg_digest, variant, sub_digest, img, "unittest") != key1

    # 3. CellSpec.key() produces consistent hash
    spec = CellSpec(
        package=pkg,
        variant=variant,
        submission="echo HELLO",
        backend_image=img,
        grader_hook=hook,
        package_digest=pkg_digest,
    )
    assert spec.key() == key1


# ---------------------------------------------------------------------------
# 2. Cache hit and idempotence
# ---------------------------------------------------------------------------


def test_cache_hit_and_idempotence(tmp_path: Path) -> None:
    pkg = _make_dummy_package(tmp_path / "pkg")
    results_dir = tmp_path / "results"
    gate = ReserveSettleSpendGate(
        approval="test-approval",
        cap_usd=1.0,
        default_reservation_usd=0.01,
    )

    backend = FakeBackend(tmp_path, gate)
    runner = ModalCellRunner(
        repo_root=tmp_path,
        spend_gate=gate,
        results_dir=results_dir,
        backend_factory=lambda **_: backend,
    )

    cell = CellSpec(
        package=pkg,
        variant="reference",
        submission="echo TEST",
    )

    # First run: cache miss, executes backend
    res1 = runner.run_cell(cell)
    assert res1["cache_hit"] is False
    assert res1["reward"] == 1.0
    assert len(backend.calls) == 1
    settled_first = gate.settled_usd
    assert settled_first > 0

    # Second run: cache hit, backend NOT called, spend unchanged
    res2 = runner.run_cell(cell)
    assert res2["cache_hit"] is True
    assert res2["reward"] == 1.0
    assert len(backend.calls) == 1  # Unchanged
    assert gate.settled_usd == settled_first  # $0 charged on cache hit
    assert gate.reserved_usd == 0.0


# ---------------------------------------------------------------------------
# 3. Reserve/settle arithmetic including K4 scenario
# ---------------------------------------------------------------------------


def test_reserve_settle_arithmetic_k4(tmp_path: Path) -> None:
    ledger = tmp_path / "k4-ledger.json"
    gate = ReserveSettleSpendGate(
        approval="test-k4-approval",
        cap_usd=1.50,
        default_reservation_usd=0.01,
        spend_ledger=ledger,
    )

    # Single cell reserve ($0.01) -> settle ($0.0065) -> release ($0.0035)
    res = gate.reserve(0.01, cell_key="cell-001")
    assert gate.reserved_usd == 0.01
    assert gate.settled_usd == 0.0
    assert gate.effective_spend_usd == 0.01
    assert gate.remaining_usd == 1.49

    released = res.settle(0.0065)
    assert released == pytest.approx(0.0035, abs=1e-6)
    assert gate.reserved_usd == 0.0
    assert gate.settled_usd == 0.0065
    assert gate.effective_spend_usd == 0.0065
    assert gate.remaining_usd == pytest.approx(1.50 - 0.0065, abs=1e-6)

    # K4 failure reproduction: under old flat reservation, 150 cells would hit $1.50 cap
    # even though real spend was only 150 * 0.0065 = $0.975.
    # With reserve/settle, run 200 cells:
    for i in range(2, 201):
        r = gate.reserve(0.01, cell_key=f"cell-{i:03d}")
        r.settle(0.0065)

    # All 200 cells ran successfully!
    assert gate.executions == 200
    assert gate.reserved_usd == 0.0
    assert gate.settled_usd == pytest.approx(200 * 0.0065, abs=1e-6)
    assert gate.settled_usd == pytest.approx(1.30, abs=1e-6)
    assert gate.effective_spend_usd <= 1.50


# ---------------------------------------------------------------------------
# 4. Cap boundary
# ---------------------------------------------------------------------------


def test_cap_boundary(tmp_path: Path) -> None:
    gate = ReserveSettleSpendGate(
        approval="test-cap-boundary",
        cap_usd=0.025,
        default_reservation_usd=0.01,
    )

    r1 = gate.reserve(0.01, cell_key="c1")
    assert gate.effective_spend_usd == 0.01

    r2 = gate.reserve(0.01, cell_key="c2")
    assert gate.effective_spend_usd == 0.02

    # Third reservation of $0.01 would bring effective spend to $0.03 > $0.025 cap
    with pytest.raises(SpendCapReached, match="cap"):
        gate.reserve(0.01, cell_key="c3")

    # Settle r1 down to $0.004 -> releases $0.006
    r1.settle(0.004)
    # Now effective spend is $0.004 (settled) + $0.01 (r2 reserved) = $0.014
    assert gate.effective_spend_usd == pytest.approx(0.014, abs=1e-6)

    # Now c3 can be reserved! ($0.014 + $0.01 = $0.024 <= $0.025)
    r3 = gate.reserve(0.01, cell_key="c3")
    assert gate.effective_spend_usd == pytest.approx(0.024, abs=1e-6)
    r2.settle(0.004)
    r3.settle(0.004)
    assert gate.reserved_usd == 0.0
    assert gate.settled_usd == pytest.approx(0.012, abs=1e-6)


# ---------------------------------------------------------------------------
# 5. Concurrent reservations and file locking
# ---------------------------------------------------------------------------


def test_concurrent_reservations_and_file_locking(tmp_path: Path) -> None:
    ledger = tmp_path / "concurrent-ledger.json"
    gate = ReserveSettleSpendGate(
        approval="test-concurrency",
        cap_usd=5.0,
        default_reservation_usd=0.01,
        spend_ledger=ledger,
    )

    num_threads = 8
    ops_per_thread = 5

    def worker(worker_id: int) -> list[float]:
        settled_amounts = []
        for i in range(ops_per_thread):
            key = f"w{worker_id}-{i}"
            res = gate.reserve(0.01, cell_key=key)
            actual = 0.003
            res.settle(actual)
            settled_amounts.append(actual)
        return settled_amounts

    with ThreadPoolExecutor(max_workers=num_threads) as pool:
        futures = [pool.submit(worker, i) for i in range(num_threads)]
        for f in as_completed(futures):
            f.result()

    summary = gate.summary()
    total_ops = num_threads * ops_per_thread
    expected_settled = round(total_ops * 0.003, 6)

    assert summary["reserved_usd"] == 0.0
    assert summary["settled_usd"] == pytest.approx(expected_settled, abs=1e-6)
    assert summary["executions"] == total_ops

    # Verify ledger on disk is valid JSON and consistent
    raw = json.loads(ledger.read_text(encoding="utf-8"))
    assert raw["settled_usd"] == pytest.approx(expected_settled, abs=1e-6)
    assert raw["reserved_usd"] == 0.0
    assert len(raw["reservations"]) == total_ops


# ---------------------------------------------------------------------------
# 6. Error classification with fake backend
# ---------------------------------------------------------------------------


def test_error_classification_with_fake_backend(tmp_path: Path) -> None:
    pkg = _make_dummy_package(tmp_path / "pkg")
    results_dir = tmp_path / "results"

    # Case A: Infrastructure error with successful retry
    gate = ReserveSettleSpendGate(approval="app", cap_usd=1.0)
    backend_a = FakeBackend(tmp_path, gate)
    backend_a.fail_infrastructure_attempts = 2  # Fails twice, succeeds on attempt 3
    runner_a = ModalCellRunner(
        repo_root=tmp_path,
        spend_gate=gate,
        results_dir=results_dir,
        max_retries=3,
        backoff_factor=1.1,
        backend_factory=lambda **_: backend_a,
    )
    cell_a = CellSpec(package=pkg, variant="retry_test", submission="echo 1")
    res_a = runner_a.run_cell(cell_a)
    assert res_a["status"] == "executed"
    assert res_a["attempts"] == 3
    assert gate.reserved_usd == 0.0

    # Case B: Infrastructure error exhausting retries
    backend_b = FakeBackend(tmp_path, gate)
    backend_b.fail_infrastructure_attempts = 10
    runner_b = ModalCellRunner(
        repo_root=tmp_path,
        spend_gate=gate,
        results_dir=results_dir,
        max_retries=2,
        backoff_factor=1.1,
        backend_factory=lambda **_: backend_b,
    )
    cell_b = CellSpec(package=pkg, variant="exhaust_test", submission="echo 2")
    with pytest.raises(CellInfrastructureError):
        runner_b.run_cell(cell_b)
    # Reservation should have been settled/cleaned up
    assert gate.reserved_usd == 0.0

    # Case C: Submission failure (evaluated result, NOT retried!)
    backend_c = FakeBackend(tmp_path, gate)
    backend_c.fail_submission = True
    runner_c = ModalCellRunner(
        repo_root=tmp_path,
        spend_gate=gate,
        results_dir=results_dir,
        backend_factory=lambda **_: backend_c,
    )
    cell_c = CellSpec(package=pkg, variant="sub_fail", submission="exit 1")
    res_c = runner_c.run_cell(cell_c)
    assert res_c["reward"] == 0.0
    assert res_c["reason"] == "QA script exited 1"
    assert len(backend_c.calls) == 1  # Not retried!
    assert res_c["cache_hit"] is False
    assert gate.reserved_usd == 0.0

    # Case D: Spend cap reached cleanly stops
    tiny_gate = ReserveSettleSpendGate(approval="app", cap_usd=0.005, default_reservation_usd=0.01)
    runner_d = ModalCellRunner(
        repo_root=tmp_path,
        spend_gate=tiny_gate,
        results_dir=results_dir,
        backend_factory=lambda **_: backend_c,
    )
    with pytest.raises(CellCapReachedError):
        runner_d.run_cell(CellSpec(package=pkg, variant="cap_cell", submission="echo 3"))

    # Case E: Cancelled error
    backend_e = FakeBackend(tmp_path, gate)
    backend_e.raise_exc = KeyboardInterrupt()
    runner_e = ModalCellRunner(
        repo_root=tmp_path,
        spend_gate=gate,
        results_dir=results_dir,
        backend_factory=lambda **_: backend_e,
    )
    with pytest.raises(CellCancelledError):
        runner_e.run_cell(CellSpec(package=pkg, variant="cancelled_cell", submission="echo 4"))

    # Helper classifier checks
    assert is_infrastructure_error("Connection refused by peer")
    assert is_infrastructure_error("503 Service Unavailable")
    assert not is_infrastructure_error("QA script exited 1")
    assert estimate_modal_cost(pkg, 10.0) > 0.0


# ---------------------------------------------------------------------------
# 7. Resume after simulated stop
# ---------------------------------------------------------------------------


def test_resume_after_simulated_stop(tmp_path: Path) -> None:
    pkg = _make_dummy_package(tmp_path / "pkg")
    results_dir = tmp_path / "results"
    ledger = tmp_path / "resume-ledger.json"

    cells = [
        CellSpec(package=pkg, variant=f"cell-{i}", submission=f"echo {i}") for i in range(6)
    ]

    # Run 1: Allow only 3 cells before hitting cap
    gate1 = ReserveSettleSpendGate(
        approval="resume-app",
        cap_usd=0.035,  # Enough for 3 reservations of $0.01
        default_reservation_usd=0.01,
        spend_ledger=ledger,
    )
    backend1 = FakeBackend(tmp_path, gate1)
    gate1.estimate_cost = lambda *_: 0.01
    runner1 = ModalCellRunner(
        repo_root=tmp_path,
        spend_gate=gate1,
        results_dir=results_dir,
        backend_factory=lambda **_: backend1,
    )

    batch1 = runner1.run_batch(cells, concurrency=2)
    # Exactly 3 cells ran and finished, remaining 3 stopped by cap
    assert len(batch1) == 6
    assert sum(1 for r in batch1 if r.get("status") == "executed") == 3
    assert sum(1 for r in batch1 if r.get("status") in {"cap_stopped", "cap_reached"}) == 3
    assert len(backend1.calls) == 3
    # Run 2: Resume with increased cap ($0.10)
    gate2 = ReserveSettleSpendGate(
        approval="resume-app",
        cap_usd=0.10,
        default_reservation_usd=0.01,
        spend_ledger=ledger,
    )
    backend2 = FakeBackend(tmp_path, gate2)
    gate2.estimate_cost = lambda *_: 0.01
    runner2 = ModalCellRunner(
        repo_root=tmp_path,
        spend_gate=gate2,
        results_dir=results_dir,
        backend_factory=lambda **_: backend2,
    )

    plan2 = runner2.plan(cells)
    assert plan2.cached == 3
    assert plan2.uncached == 3

    statuses: list[CellBatchStatus] = []
    batch2 = runner2.run_batch(cells, concurrency=2, on_status=statuses.append)
    assert len(batch2) == 6
    # Only 3 new calls were dispatched to the backend!
    assert len(backend2.calls) == 3

    cache_hits = sum(1 for r in batch2 if r.get("cache_hit") is True)
    assert cache_hits == 3


# ---------------------------------------------------------------------------
# 8. Calibration file p95 lookup
# ---------------------------------------------------------------------------


def test_calibration_file_p95(tmp_path: Path) -> None:
    cal_file = tmp_path / "calibration.json"
    cal_file.write_text(
        json.dumps({
            "modal": [0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007, 0.008, 0.009, 0.010],
        }),
        encoding="utf-8",
    )
    gate = ReserveSettleSpendGate(
        approval="cal-test",
        cap_usd=1.0,
        calibration_path=cal_file,
    )
    p95 = gate.get_p95_calibration("modal")
    assert p95 == pytest.approx(0.010, abs=1e-4)
    assert gate.estimate_reservation(backend="modal") == pytest.approx(0.010, abs=1e-4)


# ---------------------------------------------------------------------------
# 9. modal_cell_runner backwards compatibility
# ---------------------------------------------------------------------------


def test_modal_cell_runner_compat(tmp_path: Path) -> None:
    pkg = _make_dummy_package(tmp_path / "pkg")
    cell_dir = tmp_path / "cells/001/ref"
    gate = ReserveSettleSpendGate(approval="app", cap_usd=1.0)
    meter = SandboxMeter(1.0)
    backend = FakeBackend(tmp_path, gate)

    runner = modal_cell_runner(
        repo_root=tmp_path,
        spend_gate=gate,
        meter=meter,
        backend_factory=lambda **_: backend,
    )

    res = runner(cell_dir, pkg, "echo HELLO")
    assert res["status"] == "executed"
    assert res["reward"] == 1.0
    assert (cell_dir / "result.json").is_file()
    assert meter.cells == 1
    assert meter.spent > 0

    # Running again returns cached result immediately
    res_cached = runner(cell_dir, pkg, "echo HELLO")
    assert res_cached["cache_hit"] is True
    assert len(backend.calls) == 1


# ---------------------------------------------------------------------------
# 10. Reviewer B1-B4 and S1-S6 regression tests
# ---------------------------------------------------------------------------


def test_b1_legacy_ledger_import_and_rewrite(tmp_path: Path) -> None:
    """B1: Legacy CloudSpendGate ledgers preserve effective spend and migrate schema."""
    ledger = tmp_path / "legacy-ledger.json"
    legacy_data = {
        "schema": "evallab.upstream_grader.spend/v1",
        "approval": "test-b1-legacy",
        "max_usd": 1.75,
        "price_basis": {
            "cpu_usd_per_core_second": 0.00003942,
            "memory_usd_per_gib_second": 0.00000667,
        },
        "reservations": [
            {"reserved_usd": 0.50},
            {"reserved_usd": 0.94},
        ],
        "reserved_usd": 1.44,
    }
    ledger.write_text(json.dumps(legacy_data), encoding="utf-8")

    # Load with ReserveSettleSpendGate
    gate = ReserveSettleSpendGate(
        approval="test-b1-legacy",
        cap_usd=1.75,
        spend_ledger=ledger,
    )
    summary = gate.summary()
    assert summary["settled_usd"] == pytest.approx(1.44)
    assert summary["effective_spend_usd"] == pytest.approx(1.44)
    assert summary["remaining_usd"] == pytest.approx(0.31)
    assert summary["executions"] == 2
    # Verify rewritten file on disk has new schema and preserved spend
    rewritten = json.loads(ledger.read_text(encoding="utf-8"))
    assert rewritten["schema"] == "evallab.modal_cells.spend/v1"
    assert rewritten["settled_usd"] == pytest.approx(1.44)
    assert rewritten["effective_spend_usd"] == pytest.approx(1.44)

    # Re-loading preserves spend again
    gate2 = ReserveSettleSpendGate(
        approval="test-b1-legacy",
        cap_usd=1.75,
        spend_ledger=ledger,
    )
    assert gate2.effective_spend_usd == pytest.approx(1.44)

    # Refuse invalid schemas
    bad_ledger = tmp_path / "bad-ledger.json"
    bad_ledger.write_text(
        json.dumps({"schema": "unknown/v99", "approval": "app"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unsupported spend ledger schema"):
        ReserveSettleSpendGate(approval="app", cap_usd=1.0, spend_ledger=bad_ledger)


def test_b2_ledger_mode_retry_accumulates_cost(tmp_path: Path) -> None:
    """B2: Retried cells accumulate measured cost across all attempts in ledger mode."""
    pkg = _make_dummy_package(tmp_path / "pkg")
    results_dir = tmp_path / "results"
    ledger = tmp_path / "retry-ledger.json"

    gate = ReserveSettleSpendGate(
        approval="retry-app",
        cap_usd=1.0,
        default_reservation_usd=0.05,
        spend_ledger=ledger,
    )
    # Simulate attempt cost of 0.02 each
    gate.estimate_cost = lambda *_: 0.02
    backend = FakeBackend(tmp_path, gate)
    backend.fail_infrastructure_attempts = 1

    runner = ModalCellRunner(
        repo_root=tmp_path,
        spend_gate=gate,
        results_dir=results_dir,
        max_retries=2,
        backend_factory=lambda **_: backend,
    )

    cell = CellSpec(package=pkg, variant="v1", submission="echo retry")
    res = runner.run_cell(cell)

    assert res["attempts"] == 2
    assert res["est_usd"] == pytest.approx(0.04)
    # Settle must record cost of BOTH attempts in the gate and ledger
    assert gate.settled_usd == pytest.approx(0.04)

    ledger_data = json.loads(ledger.read_text(encoding="utf-8"))
    assert ledger_data["settled_usd"] == pytest.approx(0.04)
    completed_entries = [r for r in ledger_data["reservations"] if r.get("status") == "completed"]
    assert len(completed_entries) == 1
    assert completed_entries[0]["settled_usd"] == pytest.approx(0.04)


def test_b3_non_completed_statuses_counted_in_totals(tmp_path: Path) -> None:
    """B3: Failed/cancelled costs are counted in ledger and in-memory totals; finally block settles runner."""
    pkg = _make_dummy_package(tmp_path / "pkg")
    results_dir = tmp_path / "results"
    ledger = tmp_path / "status-ledger.json"

    gate = ReserveSettleSpendGate(
        approval="status-app",
        cap_usd=1.0,
        default_reservation_usd=0.05,
        spend_ledger=ledger,
    )
    gate.estimate_cost = lambda *_: 0.025
    backend = FakeBackend(tmp_path, gate)
    backend.raise_exc = RuntimeError("Fatal container failure")

    runner = ModalCellRunner(
        repo_root=tmp_path,
        spend_gate=gate,
        results_dir=results_dir,
        backend_factory=lambda **_: backend,
    )

    cell = CellSpec(package=pkg, variant="fail-cell", submission="echo fail")
    with pytest.raises(RuntimeError, match="Fatal container failure"):
        runner.run_cell(cell)

    # Cost settled with status="failed" is counted in gate and ledger totals
    assert gate.settled_usd == pytest.approx(0.025)
    ledger_data = json.loads(ledger.read_text(encoding="utf-8"))
    assert ledger_data["settled_usd"] == pytest.approx(0.025)

    # Also test modal_cell_runner finally settlement on exception
    meter = SandboxMeter(1.0)
    backend2 = FakeBackend(tmp_path, gate)
    backend2.raise_exc = RuntimeError("Verifier crashed")
    cell_runner = modal_cell_runner(
        repo_root=tmp_path,
        spend_gate=gate,
        meter=meter,
        backend_factory=lambda **_: backend2,
    )
    prev_settled = gate.settled_usd
    with pytest.raises(RuntimeError, match="Verifier crashed"):
        cell_runner(tmp_path / "cell_dir", pkg, "echo fail")
    assert gate.settled_usd >= prev_settled


def test_b4_under_reservation_and_cap_enforcement(tmp_path: Path) -> None:
    """B4: Cap is enforced even when reservation < actual cost; overrun <= 1 in-flight cell."""
    pkg = _make_dummy_package(tmp_path / "pkg")
    results_dir = tmp_path / "results"
    ledger = tmp_path / "cap-ledger.json"

    gate = ReserveSettleSpendGate(
        approval="under-reserve-app",
        cap_usd=0.10,
        default_reservation_usd=0.02,
        spend_ledger=ledger,
    )
    # Measured cost is 0.04 (larger than reservation 0.02)
    gate.estimate_cost = lambda *_: 0.04
    backend = FakeBackend(tmp_path, gate)

    runner = ModalCellRunner(
        repo_root=tmp_path,
        spend_gate=gate,
        results_dir=results_dir,
        backend_factory=lambda **_: backend,
    )

    cells = [CellSpec(package=pkg, variant=f"v{i}", submission=f"s{i}") for i in range(12)]
    results = runner.run_batch(cells, concurrency=4)

    assert len(results) == 12
    # Overrun cannot exceed cap plus one in-flight cell
    assert gate.settled_usd <= 0.10 + 0.04
    # Status counters should reflect cap_stopped
    cap_stopped_count = sum(
        1 for r in results if r.get("status") in {"cap_stopped", "cap_reached"}
    )
    assert cap_stopped_count > 0


def test_b4_200_cheap_cells_flow_under_cap(tmp_path: Path) -> None:
    """B4: 200 cheap cells ($0.002 each) under $1.50 cap are NOT stopped by reservations."""
    pkg = _make_dummy_package(tmp_path / "pkg")
    results_dir = tmp_path / "results"
    ledger = tmp_path / "cheap-ledger.json"

    gate = ReserveSettleSpendGate(
        approval="cheap-app",
        cap_usd=1.50,
        default_reservation_usd=0.05,  # Large reservation released at settle time
        spend_ledger=ledger,
    )
    # Actual cost is only $0.002
    gate.estimate_cost = lambda *_: 0.002
    backend = FakeBackend(tmp_path, gate)

    runner = ModalCellRunner(
        repo_root=tmp_path,
        spend_gate=gate,
        results_dir=results_dir,
        backend_factory=lambda **_: backend,
    )

    cells = [CellSpec(package=pkg, variant=f"c{i}", submission=f"echo {i}") for i in range(200)]
    results = runner.run_batch(cells, concurrency=4)

    assert len(results) == 200
    assert all(r.get("status") == "executed" for r in results)
    assert gate.settled_usd == pytest.approx(200 * 0.002, abs=1e-4)


def test_s1_stale_reconcile_preserves_spend(tmp_path: Path) -> None:
    """S1: Stale reservations from dead processes are settled at reserved amount, not wiped."""
    ledger = tmp_path / "stale-ledger.json"
    data = {
        "schema": "evallab.modal_cells.spend/v1",
        "approval": "stale-app",
        "max_usd": 1.0,
        "reserved_usd": 0.05,
        "settled_usd": 0.0,
        "cumulative_spend_usd": 0.0,
        "effective_spend_usd": 0.05,
        "executions": 1,
        "reservations": [
            {
                "reservation_id": "r1",
                "cell_key": "k1",
                "pid": 99999999,  # Non-existent PID
                "reserved_usd": 0.05,
                "status": "reserved",
            }
        ],
    }
    ledger.write_text(json.dumps(data), encoding="utf-8")

    gate = ReserveSettleSpendGate(
        approval="stale-app",
        cap_usd=1.0,
        spend_ledger=ledger,
        reconcile_stale=True,
    )
    # Reconciled entry becomes stale_settled and its spend is counted
    assert gate.settled_usd == pytest.approx(0.05)
    assert gate.effective_spend_usd == pytest.approx(0.05)

    rewritten = json.loads(ledger.read_text(encoding="utf-8"))
    assert rewritten["reservations"][0]["status"] == "stale_settled"
    assert rewritten["settled_usd"] == pytest.approx(0.05)


def test_s2_batch_deduplication(tmp_path: Path) -> None:
    """S2: Duplicate cells within a single batch are executed only once."""
    pkg = _make_dummy_package(tmp_path / "pkg")
    results_dir = tmp_path / "results"

    gate = ReserveSettleSpendGate(approval="dedup-app", cap_usd=1.0)
    gate.estimate_cost = lambda *_: 0.01
    backend = FakeBackend(tmp_path, gate)

    runner = ModalCellRunner(
        repo_root=tmp_path,
        spend_gate=gate,
        results_dir=results_dir,
        backend_factory=lambda **_: backend,
    )

    cell = CellSpec(package=pkg, variant="same", submission="echo identical")
    results = runner.run_batch([cell, cell, cell], concurrency=3)

    assert len(results) == 3
    # Only 1 backend call made despite 3 inputs
    assert len(backend.calls) == 1
    assert all(r.get("status") == "executed" for r in results)


def test_s3_plan_uses_exact_reservation_formula(tmp_path: Path) -> None:
    """S3: plan() computes estimated spend using estimate_reservation()."""
    pkg = _make_dummy_package(tmp_path / "pkg", cpus=4, memory_mb=16384)
    gate = ReserveSettleSpendGate(
        approval="plan-app",
        cap_usd=1.0,
        default_reservation_usd=0.01,
    )
    runner = ModalCellRunner(
        repo_root=tmp_path,
        spend_gate=gate,
        results_dir=tmp_path / "results",
    )

    cells = [CellSpec(package=pkg, variant=f"v{i}", submission="s") for i in range(5)]
    plan = runner.plan(cells)

    expected_per_cell = gate.estimate_reservation(
        package=pkg, timeout_seconds=cells[0].timeout_seconds
    )
    assert plan.estimated_spend_usd == pytest.approx(5 * expected_per_cell, abs=1e-4)


def test_s4_run_batch_ordering_and_error_reporting(tmp_path: Path) -> None:
    """S4: run_batch preserves exact input order and includes error information."""
    pkg = _make_dummy_package(tmp_path / "pkg")
    results_dir = tmp_path / "results"

    gate = ReserveSettleSpendGate(approval="order-app", cap_usd=1.0)
    gate.estimate_cost = lambda *_: 0.01

    class ExplodingBackend(FakeBackend):
        def run_container_trial(self, package: Path, **kwargs: Any) -> FakeObservation:
            if "fail" in str(kwargs.get("script", "")):
                raise RuntimeError("Explosion on cell 1")
            return super().run_container_trial(package, **kwargs)

    backend = ExplodingBackend(tmp_path, gate)
    runner = ModalCellRunner(
        repo_root=tmp_path,
        spend_gate=gate,
        results_dir=results_dir,
        backend_factory=lambda **_: backend,
    )

    cells = [
        CellSpec(package=pkg, variant="c0", submission="echo ok0"),
        CellSpec(package=pkg, variant="c1", submission="echo fail"),
        CellSpec(package=pkg, variant="c2", submission="echo ok2"),
    ]
    results = runner.run_batch(cells, concurrency=1)

    assert len(results) == 3
    assert results[0]["status"] == "executed"
    assert results[1]["status"] == "failed"
    assert "Explosion on cell 1" in results[1]["error"]
    assert results[2]["status"] == "executed"


def test_s5_failed_verdict_not_cached_and_oserror_narrowing(tmp_path: Path) -> None:
    """S5: Non-verdict failures are never cached; FileNotFoundError is not treated as infra."""
    pkg = _make_dummy_package(tmp_path / "pkg")
    results_dir = tmp_path / "results"

    gate = ReserveSettleSpendGate(approval="s5-app", cap_usd=1.0)
    gate.estimate_cost = lambda *_: 0.01

    class FailedObservationBackend(FakeBackend):
        def run_container_trial(self, package: Path, **kwargs: Any) -> FakeObservation:
            return FakeObservation(status="failed", reason="Backend Internal Error", facts={})

    backend = FailedObservationBackend(tmp_path, gate)
    runner = ModalCellRunner(
        repo_root=tmp_path,
        spend_gate=gate,
        results_dir=results_dir,
        backend_factory=lambda **_: backend,
    )

    cell = CellSpec(package=pkg, variant="v", submission="s")
    with pytest.raises(CellError, match="Execution failed"):
        runner.run_cell(cell)

    # Result must NOT be cached
    assert runner.check_cache(cell.key()) is None

    # Verify is_infrastructure_error narrows OSError
    assert not is_infrastructure_error(FileNotFoundError("no file"))
    assert not is_infrastructure_error(PermissionError("denied"))
    assert is_infrastructure_error(ConnectionResetError("reset"))
    assert is_infrastructure_error(TimeoutError("timed out"))


def test_s6_charge_fallback_micro_charge(tmp_path: Path) -> None:
    """S6: charge() outside active reservation creates a settled micro-charge without leaking."""
    ledger = tmp_path / "charge-ledger.json"
    gate = ReserveSettleSpendGate(
        approval="charge-app",
        cap_usd=1.0,
        usd_per_execution=0.005,
        spend_ledger=ledger,
    )

    # Outside thread reservation
    gate.charge()
    assert gate.reserved_usd == 0.0  # No leaked reservation!
    assert gate.settled_usd == pytest.approx(0.005)
    assert gate.executions == 1

    ledger_data = json.loads(ledger.read_text(encoding="utf-8"))
    assert ledger_data["reserved_usd"] == 0.0
    assert ledger_data["settled_usd"] == pytest.approx(0.005)
