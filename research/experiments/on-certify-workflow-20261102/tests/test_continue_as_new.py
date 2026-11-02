"""Test (5): Continue-As-New for 500 cells keeps state; history size stays bounded."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from certify_workflow.activities import (
    generate_mutants,
    run_cell,
    settle_spend,
    synthesize_tests,
    write_manifest,
)
from certify_workflow.models import CorpusPlan, FakeCellConfig, PackageSpec
from certify_workflow.workflows import CertifyCorpusWorkflow, CertifyPackageWorkflow


@pytest.mark.asyncio
async def test_continue_as_new_for_500_cells_bounds_history(tmp_path: Path) -> None:
    results_dir = tmp_path / "cells"
    results_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    ledger_file = tmp_path / "ledger.jsonl"
    # 50 packages with mutant_count=4
    # 4 base cells (ref, nop, witness, exit0) + 2 train + 2 eval + 2 evalbase = 10 cells/package
    # 50 packages * 10 cells = 500 cells total!
    total_pkgs = 50
    pkgs = [PackageSpec(package_id=f"pkg-can-{i:03d}", mutant_count=4) for i in range(total_pkgs)]

    # continue_as_new every 50 cells (every 5 packages) -> 10 generations
    fake_config = FakeCellConfig(
        results_dir=str(results_dir),
        base_cost_usd=0.001,
        latency_seconds=0.0,
        ledger_file=str(ledger_file),
    )
    plan = CorpusPlan(
        packages=pkgs,
        cap_usd=5.0,
        approver="can-test",
        continue_as_new_threshold=50,  # Compaction threshold
        modal_concurrency=8,
        synth_concurrency=4,
        results_dir=str(results_dir),
        manifest_output_dir=str(manifest_dir),
        fake_config=fake_config,
    )

    task_queue = "can-500-queue"

    async with await WorkflowEnvironment.start_local(ui=False) as wf_env:
        client = wf_env.client

        worker = Worker(
            client,
            task_queue=task_queue,
            workflows=[CertifyCorpusWorkflow, CertifyPackageWorkflow],
            activities=[generate_mutants, run_cell, settle_spend, synthesize_tests, write_manifest],
            max_concurrent_activities=20,
        )

        async with worker:
            wf_id = f"test-can-500-{int(time.time())}"
            handle = await client.start_workflow(
                CertifyCorpusWorkflow.run,
                plan,
                id=wf_id,
                task_queue=task_queue,
            )

            manifest = await handle.result()

            # 1. State preservation across all 10 continue-as-new generations
            assert manifest is not None
            assert not manifest.partial
            assert manifest.coverage_k == total_pkgs
            assert manifest.coverage_n == total_pkgs
            assert manifest.certified_packages == total_pkgs

            # 2. Cumulative spend preserved across generations (500 cells * $0.001 = $0.500)
            expected_spend = total_pkgs * 10 * 0.001
            assert manifest.cumulative_spend_usd == pytest.approx(expected_spend, abs=1e-3)

            # 3. Verify all 50 packages are in manifest
            for i in range(total_pkgs):
                pkg_id = f"pkg-can-{i:03d}"
                assert pkg_id in manifest.packages
                assert manifest.packages[pkg_id]["status"] in ("gold_structural", "gold_heldout")

            # 4. History size stays strictly bounded:
            # Without continue-as-new, 50 packages would produce >1,500 workflow events.
            # With continue-as-new every 50 cells, the final generation run history remains compact.
            history = await handle.fetch_history()
            event_count = len(history.events)
            assert event_count < 150, (
                f"Expected compact event history (<150 events), got {event_count} events!"
            )
