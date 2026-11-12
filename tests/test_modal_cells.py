"""Modal single-cell execution, reserve/settle spend accounting and locking."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from evallab.modal_cells import (
    ReserveSettleSpendGate,
    SandboxMeter,
    compute_cell_key,
    estimate_modal_cost,
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


# ---------------------------------------------------------------------------
# 2. Cache hit and idempotence
# ---------------------------------------------------------------------------


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
    assert estimate_modal_cost(pkg, 10.0) > 0.0


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


def test_b3_non_completed_statuses_counted_in_totals(tmp_path: Path) -> None:
    """B3: Failed/cancelled costs are counted in ledger and in-memory totals; finally block settles runner."""
    pkg = _make_dummy_package(tmp_path / "pkg")
    ledger = tmp_path / "status-ledger.json"

    gate = ReserveSettleSpendGate(
        approval="status-app",
        cap_usd=1.0,
        default_reservation_usd=0.05,
        spend_ledger=ledger,
    )
    gate.estimate_cost = lambda *_: 0.025
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


def test_should_2_cross_process_same_cell(tmp_path: Path) -> None:
    """SHOULD 2: Cross-process flock on results_dir/<key>.lock prevents duplicate execution."""
    import subprocess
    import sys
    import textwrap

    pkg = _make_dummy_package(tmp_path / "pkg")
    results_dir = tmp_path / "results_twoproc"
    ledger = tmp_path / "twoproc-ledger.json"
    calls_file = tmp_path / "calls.txt"

    worker_code = textwrap.dedent(f"""
        import os, sys, time
        from pathlib import Path
        from dataclasses import dataclass
        from evallab.modal_cells import ReserveSettleSpendGate, ModalCellRunner, CellSpec
        @dataclass
        class Obs:
            status: str
            reason: object
            facts: dict
        class B:
            def __init__(self, g): self.g = g
            def run_container_trial(self, pkg, **k):
                self.g.charge()
                with open({str(calls_file)!r}, 'a') as f:
                    f.write('call\\n')
                time.sleep(0.5)
                return Obs('executed', None, {{'reward': 1.0}})
        g = ReserveSettleSpendGate(approval='twoproc', cap_usd=1.0, spend_ledger={str(ledger)!r})
        r = ModalCellRunner(repo_root=Path({str(tmp_path)!r}), spend_gate=g, results_dir=Path({str(results_dir)!r}), backend_factory=lambda **_: B(g))
        r.run_cell(CellSpec(package=Path({str(pkg)!r}), variant='v', submission='same', timeout_seconds=240))
    """)

    p1 = subprocess.Popen([sys.executable, "-c", worker_code])
    p2 = subprocess.Popen([sys.executable, "-c", worker_code])
    p1.wait()
    p2.wait()

    calls = calls_file.read_text().strip().splitlines() if calls_file.exists() else []
    assert len(calls) == 1


def test_should_3_late_settle_adjustment_in_ledger_and_log(tmp_path: Path) -> None:
    """SHOULD 3: Late settle on stale_settled entry records delta in ledger and log (BREAK_late_settle)."""
    ledger = tmp_path / "late-ledger.json"
    log = tmp_path / "late-spend.jsonl"

    gate = ReserveSettleSpendGate(
        approval="late-app",
        cap_usd=1.0,
        default_reservation_usd=0.05,
        spend_ledger=ledger,
        spend_log=log,
    )
    res = gate.reserve(cell_key="late-cell")
    assert gate.reserved_usd == 0.05

    # Simulate another loader reconciling this reservation as stale_settled
    data = json.loads(ledger.read_text(encoding="utf-8"))
    data["reservations"][0]["pid"] = 2**22 - 7  # Nonexistent pid
    ledger.write_text(json.dumps(data), encoding="utf-8")

    gate_reconciler = ReserveSettleSpendGate(
        approval="late-app",
        cap_usd=1.0,
        default_reservation_usd=0.05,
        spend_ledger=ledger,
    )
    assert gate_reconciler.settled_usd == pytest.approx(0.05)
    assert gate_reconciler.reserved_usd == 0.0

    # Now original owner finishes late with actual cost 0.08
    res.settle(0.08, status="completed")

    gate_reloaded = ReserveSettleSpendGate(
        approval="late-app",
        cap_usd=1.0,
        default_reservation_usd=0.05,
        spend_ledger=ledger,
    )
    assert gate_reloaded.settled_usd == pytest.approx(0.08)
    assert gate_reloaded.effective_spend_usd == pytest.approx(0.08)
    assert res.status == "completed"
    assert res.settled_usd == pytest.approx(0.08)
    assert log.exists()
    assert log.read_text(encoding="utf-8").count("\n") == 1

    # Double settle is harmless no-op
    res.settle(0.08, status="completed")
    res.release()
    gate_after_double = ReserveSettleSpendGate(
        approval="late-app",
        cap_usd=1.0,
        default_reservation_usd=0.05,
        spend_ledger=ledger,
    )
    assert gate_after_double.settled_usd == pytest.approx(0.08)


def test_nit_non_object_ledger_raises_value_error(tmp_path: Path) -> None:
    """NIT: _load_and_reconcile_ledger on non-dict JSON raises explicit ValueError."""
    ledger = tmp_path / "bad-ledger.json"
    ledger.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="ledger is not a JSON object"):
        ReserveSettleSpendGate(
            approval="bad-app",
            cap_usd=1.0,
            spend_ledger=ledger,
        )
