#!/usr/bin/env python3
"""Offline tests for A4 Gold Batch Driver using FakeBackend without Modal.

Demonstrates:
1. Injected infrastructure failure with automatic retry.
2. Simulated mid-batch kill / abort and resumable restart.
3. Invariant (a): No cell is paid twice across restarts.
4. Invariant (b): Manifest has exactly one row per package with proper reason codes.
5. Invariant (c): Meter total equals the sum of settled cell costs.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    import evallab
    deps_pkg = Path("/tmp/eval-lab-a4p-deps/evallab")
    if deps_pkg.is_dir() and str(deps_pkg) not in evallab.__path__:
        evallab.__path__.insert(0, str(deps_pkg))
    s1_pkg = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/on-s1-modal-cells-20261102/src/evallab")
    a3_pkg = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/on-a3-rounds-20261102/src/evallab")
    if s1_pkg.is_dir() and str(s1_pkg) not in evallab.__path__:
        evallab.__path__.append(str(s1_pkg))
    if a3_pkg.is_dir() and str(a3_pkg) not in evallab.__path__:
        evallab.__path__.append(str(a3_pkg))
except Exception:
    pass

from run_gold_batch import (  # noqa: E402
    GoldBatchDriver,
    load_universe_tasks,
)

from evallab.modal_cells import (  # noqa: E402
    CellCancelledError,
    CellInfrastructureError,
    sha256_text,
)


@dataclass
class FakeObservation:
    status: str
    reason: str | None
    facts: dict[str, Any]


class FakeModalBackend:
    """Mock ModalBackend that tracks calls and simulates sandbox container runs."""

    def __init__(self, repo_root: Path, spend_gate: Any):
        self.repo_root = repo_root
        self.spend_gate = spend_gate
        self.calls: list[dict[str, Any]] = []
        self.fail_infrastructure_on: set[str] = set()
        self.fail_submission_on: set[str] = set()
        self.abort_on: str | None = None
        self.aborted = False

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
        script_text = script or ""
        task_name = package.name if package.parent.name == "synthetic_packages" else package.parent.name
        if "NOP_APPLIED" in script_text:
            variant = "gates/nop"
        elif "WITNESS_APPLIED" in script_text:
            variant = "gates/witness"
        elif "EXIT0_PTH_APPLIED" in script_text:
            variant = "gates/exit0_pth"
        elif "REFERENCE_APPLIED" in script_text:
            variant = "reference"
        elif "MUTANT_APPLIED" in script_text:
            parts = script_text.strip().split()
            sha_part = parts[-1] if parts else "mutant"
            variant = f"mutant-{sha_part}"
        else:
            variant = trial_dir.parent.name

        cell_id = f"{task_name}/{variant}"

        self.calls.append({
            "task": task_name,
            "variant": variant,
            "cell_id": cell_id,
            "package": str(package),
            "script": script,
        })

        # Simulated kill / abort
        if self.abort_on and self.abort_on in cell_id:
            self.aborted = True
            raise KeyboardInterrupt("Simulated mid-batch kill -9")

        # Injected infrastructure failure (retried by ModalCellRunner)
        if any(fail_tag in cell_id for fail_tag in list(self.fail_infrastructure_on)):
            for fail_tag in list(self.fail_infrastructure_on):
                if fail_tag in cell_id:
                    self.fail_infrastructure_on.remove(fail_tag)
            raise CellInfrastructureError("503 Service Unavailable: Modal worker container lost")
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "agent").mkdir(parents=True, exist_ok=True)
        (trial_dir / "verifier").mkdir(parents=True, exist_ok=True)

        # Determine reward based on script
        reward = 1.0
        script_text = script or ""

        if "FAIL_SUBMISSION" in script_text or cell_id in self.fail_submission_on:
            reward = 0.0
            (trial_dir / "verifier/test_output.log").write_text("Test failed: AssertionError\n")
            return FakeObservation(status="executed", reason="Tests failed", facts={"reward": 0.0})

        if "NOP_APPLIED" in script_text or "WITNESS_APPLIED" in script_text or "EXIT0_PTH_APPLIED" in script_text:
            # Control tests should fail (reward 0.0) for valid hardening
            reward = 0.0
            (trial_dir / "verifier/test_output.log").write_text("Hardening control caught adversarial submission\n")
            return FakeObservation(status="executed", reason="Control rejected", facts={"reward": 0.0})

        if "MUTANT_APPLIED" in script_text:
            # Simulated mutant: killed by the test suite (reward 0.0) unless designated survivor
            if "survivor" in script_text:
                reward = 1.0  # Mutant survived
                (trial_dir / "verifier/test_output.log").write_text("Mutant passed tests (survived)\n")
            else:
                reward = 0.0  # Mutant killed
                (trial_dir / "verifier/test_output.log").write_text("Mutant failed tests (killed)\n")
            return FakeObservation(status="executed", reason="Mutant evaluated", facts={"reward": reward})

        # Reference solution passes
        (trial_dir / "verifier/test_output.log").write_text("All 5 tests passed\n")
        return FakeObservation(status="executed", reason=None, facts={"reward": 1.0})


def make_dummy_package(pkg_path: Path) -> Path:
    pkg_path.mkdir(parents=True, exist_ok=True)
    (pkg_path / "task.toml").write_text(
        '[environment]\nworkdir = "/testbed"\ncpus = 2\nmemory_mb = 4096\n'
    )
    (pkg_path / "instruction.md").write_text("Fix issue in dummy module.\n")
    (pkg_path / "tests").mkdir(exist_ok=True)
    (pkg_path / "tests/test.sh").write_text('#!/bin/bash\npytest tests/\n')
    (pkg_path / "tests/test_outputs.py").write_text('def test_dummy(): assert True\n')
    return pkg_path


def make_synthetic_env(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create 3 synthetic packages, universe JSON, and weak-tasks JSON."""
    pkgs_root = tmp_path / "synthetic_packages"
    tasks: list[dict[str, Any]] = []

    for i in (1, 2, 3):
        tid = f"syn-task-{i}"
        pkg = make_dummy_package(pkgs_root / tid)
        patch = f"--- a/main.py\n+++ b/main.py\n@@ -1,1 +1,1 @@\n-old_{i}\n+new_{i}\n"
        patch_file = tmp_path / f"{tid}.patch"
        patch_file.write_text(patch)

        tasks.append({
            "task_id": tid,
            "sources": ["synthetic-test"],
            "strictly_dev": True,
            "package_path": str(pkg),
            "package_exists": True,
            "has_environment": True,
            "has_tests": True,
            "reference_patch_present": True,
            "reference_patch_path": str(patch_file),
            "reference_patch_sha256": sha256_text(patch),
            "extractable_hook_command": "python3",
            "gold_ok": True,
            "expected_cells": {"gold_round_1": 8, "gold_round_2": 16},
            "expected_cost_real_usd": {"gold_round_1": 0.052, "gold_round_2": 0.104},
            "expected_cost_reserved_usd": {"gold_round_1": 0.08, "gold_round_2": 0.16},
        })

    universe_file = tmp_path / "synthetic-universe.json"
    universe_file.write_text(json.dumps({"tasks": tasks}, indent=2))

    weak_file = tmp_path / "synthetic-weak.json"
    # Mark syn-task-2 and syn-task-1 as weak tasks (syn-task-2 first)
    weak_file.write_text(json.dumps([
        {"task_id": "syn-task-2", "number_of_survivors": 2},
        {"task_id": "syn-task-1", "number_of_survivors": 1},
    ], indent=2))

    return universe_file, weak_file, pkgs_root


def mock_generator(task_id: str, r_idx: int, prompt: str) -> str:
    """Mock synthesizer generator returning valid contract tests."""
    return f"""import unittest

class ContractTest(unittest.TestCase):
    def test_contract_{task_id.replace('-', '_')}_{r_idx}(self):
        self.assertTrue(True)
"""


def test_priority_order(tmp_path: Path) -> None:
    """Verify priority order puts weak tasks first then sorts remaining."""
    u_file, w_file, _ = make_synthetic_env(tmp_path)
    ordered = load_universe_tasks(u_file, w_file)

    assert len(ordered) == 3
    # Weak tasks first in order: syn-task-2, then syn-task-1
    assert ordered[0]["task_id"] == "syn-task-2"
    assert ordered[0]["priority_rank"] == 1
    assert ordered[0]["is_weak"] is True

    assert ordered[1]["task_id"] == "syn-task-1"
    assert ordered[1]["priority_rank"] == 2
    assert ordered[1]["is_weak"] is True

    # Remaining task: syn-task-3
    assert ordered[2]["task_id"] == "syn-task-3"
    assert ordered[2]["priority_rank"] == 3
    assert ordered[2]["is_weak"] is False


def test_plan_mode(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Verify --plan outputs projected cells and cost, flagging >5% cap items."""
    u_file, w_file, _ = make_synthetic_env(tmp_path)
    out_dir = tmp_path / "plan_out"
    ledger = tmp_path / "plan_ledger.json"

    # Set cap to $1.00 so that 5% is $0.05.
    # Expected cost for R2 is $0.104 > $0.05, so all 3 will be flagged.
    driver = GoldBatchDriver(
        out_dir=out_dir,
        universe_path=u_file,
        weak_tasks_path=w_file,
        cap_usd=1.00,
        spend_ledger=ledger,
    )
    plan_data = driver.plan()

    assert plan_data["total_tasks"] == 3
    assert plan_data["threshold_5pct_usd"] == 0.05
    assert plan_data["flagged_count"] == 3
    assert plan_data["total_cells_round_1"] == 24
    assert plan_data["total_cost_round_1_usd"] == pytest.approx(0.156)


def test_resumable_driver_injected_failure_and_kill9_recovery(tmp_path: Path) -> None:
    """End-to-end test with injected failure, mid-batch kill -9, and restart.

    Verifies:
    (a) No cell is paid twice across restarts
    (b) Manifest has 1 row per package with valid reason codes
    (c) Meter total equals the sum of settled cell costs
    """
    u_file, w_file, _ = make_synthetic_env(tmp_path)
    out_dir = tmp_path / "gold_out"
    ledger = tmp_path / "test-ledger.json"
    spend_log = tmp_path / "test-spend.jsonl"

    shared_backend = FakeModalBackend(tmp_path, None)
    # Inject infrastructure failure on syn-task-2 probe (will be retried and succeed)
    shared_backend.fail_infrastructure_on.add("syn-task-2/mutant-sha_pr1")

    # Set abort trigger on syn-task-1 gates/nop to simulate mid-batch kill -9
    shared_backend.abort_on = "syn-task-1/gates/nop"

    # -------------------------------------------------------------
    # RUN 1: Starts batch, runs syn-task-2, hits abort on syn-task-1
    # -------------------------------------------------------------
    driver1 = GoldBatchDriver(
        out_dir=out_dir,
        universe_path=u_file,
        weak_tasks_path=w_file,
        cap_usd=3.00,
        spend_ledger=ledger,
        spend_log=spend_log,
        backend_factory=lambda **_: shared_backend,
        generator_fn=mock_generator,
        max_task_concurrency=1,  # Serial for deterministic abort sequence
    )
    shared_backend.spend_gate = driver1.spend_gate

    # First run will raise KeyboardInterrupt on syn-task-1/gates/nop
    with pytest.raises((KeyboardInterrupt, CellCancelledError)):
        driver1.run_batch()

    assert shared_backend.aborted is True
    calls_after_run1 = len(shared_backend.calls)
    assert calls_after_run1 > 0
    settled_after_run1 = driver1.spend_gate.settled_usd
    assert settled_after_run1 > 0

    # Task 2 should be completed and written to disk
    task2_summary = out_dir / "tasks/syn-task-2/summary.json"
    assert task2_summary.is_file()

    # Task 1 was aborted mid-flight (gates/nop was not saved as completed task)
    task1_summary = out_dir / "tasks/syn-task-1/summary.json"
    assert not task1_summary.is_file()

    # -------------------------------------------------------------
    # RUN 2: Restart driver with identical configuration
    # -------------------------------------------------------------
    # Clear abort trigger so run 2 can finish
    shared_backend.abort_on = None

    driver2 = GoldBatchDriver(
        out_dir=out_dir,
        universe_path=u_file,
        weak_tasks_path=w_file,
        cap_usd=3.00,
        spend_ledger=ledger,
        spend_log=spend_log,
        backend_factory=lambda **_: shared_backend,
        generator_fn=mock_generator,
        max_task_concurrency=2,
    )
    shared_backend.spend_gate = driver2.spend_gate

    manifest = driver2.run_batch()

    # -------------------------------------------------------------
    # VERIFICATION OF THE THREE INVARIANTS:
    # -------------------------------------------------------------

    # (a) No cell is paid twice:
    # Every completed cell executed in Run 1 should NOT be executed again in Run 2.
    # syn-task-2 completed in Run 1 -> 0 calls in Run 2
    # syn-task-1 cells that finished before abort (ref, probes, evals) -> 0 calls in Run 2
    # Only aborted cell (gates/nop) and unrun cells (witness, exit0, syn-task-3) are run in Run 2.
    unique_cells_executed = {(c["task"], c["variant"]) for c in shared_backend.calls}
    syn1_calls_in_run2 = [c for c in shared_backend.calls[calls_after_run1:] if c["task"] == "syn-task-1"]
    # Only gates/nop (re-run), gates/witness, gates/exit0_pth ran in Run 2
    assert len(syn1_calls_in_run2) == 3
    assert {c["variant"] for c in syn1_calls_in_run2} == {"gates/nop", "gates/witness", "gates/exit0_pth"}

    # Total calls across both runs:
    # unique cells (27) + 1 retried infra failure + 1 aborted uncompleted cell = 29
    assert len(shared_backend.calls) == len(unique_cells_executed) + 2
    # Check that syn-task-2 cells were never re-executed during run 2:
    syn2_calls_in_run1 = [c for c in shared_backend.calls[:calls_after_run1] if c["task"] == "syn-task-2"]
    assert len(syn2_calls_in_run1) > 0
    syn2_calls_in_run2 = [c for c in shared_backend.calls[calls_after_run1:] if c["task"] == "syn-task-2"]
    assert len(syn2_calls_in_run2) == 0  # Zero new backend calls for already finished task!

    # (b) Manifest has one row per package with a reason code for every non-certified one:
    assert (out_dir / "gold-v1-manifest.json").is_file()
    assert (out_dir / "gold-v1-summary.md").is_file()

    manifest_tasks = manifest["tasks"]
    assert len(manifest_tasks) == 3
    task_ids_in_manifest = [t["task_id"] for t in manifest_tasks]
    assert task_ids_in_manifest == ["syn-task-2", "syn-task-1", "syn-task-3"]

    for t in manifest_tasks:
        assert "status" in t
        assert "reason" in t
        assert t["status"] in {"gold_structural", "gold_heldout", "not_gold", "not_run"}
        if t["status"] not in {"gold_structural", "gold_heldout"}:
            assert t["reason"] != "ok"

    # Data policy invariant: Verify no mutant diffs or source code leaked in manifest
    manifest_raw = (out_dir / "gold-v1-manifest.json").read_text(encoding="utf-8")
    assert "diff --git" not in manifest_raw
    assert "--- a/" not in manifest_raw
    assert "def test_contract" not in manifest_raw

    # (c) The meter total equals the sum of settled cells:
    final_settled_usd = driver2.spend_gate.settled_usd
    assert manifest["meter_settled_usd"] == pytest.approx(final_settled_usd, abs=1e-5)

    # Sum of est_usd across all result.json files
    cell_results = list((out_dir / "cells").glob("*/result.json"))
    sum_cell_costs = sum(json.loads(p.read_text())["est_usd"] for p in cell_results)
    assert final_settled_usd == pytest.approx(sum_cell_costs, abs=1e-4)

    # Spend ledger sanity
    ledger_data = json.loads(ledger.read_text())
    assert ledger_data["settled_usd"] == pytest.approx(final_settled_usd, abs=1e-5)
    assert ledger_data["reserved_usd"] == 0.0  # All reservations settled or released
