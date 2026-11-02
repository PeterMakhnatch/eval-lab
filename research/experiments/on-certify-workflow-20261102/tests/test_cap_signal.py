"""Test (2): Cap reached then continues after raise_cap signal."""

from __future__ import annotations

import asyncio
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
async def test_cap_reached_then_continues_after_signal(tmp_path: Path) -> None:
    results_dir = tmp_path / "cells"
    results_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    ledger_file = tmp_path / "ledger.jsonl"

    pkg1 = PackageSpec(package_id="pkg-cap-1", mutant_count=2)
    pkg2 = PackageSpec(package_id="pkg-cap-2", mutant_count=2)

    # 7 cells per package @ 0.005 = 0.035 per package
    # Cap 0.04 allows pkg1 (0.035), but parks on pkg2 (0.070 > 0.04)
    fake_config = FakeCellConfig(
        results_dir=str(results_dir),
        base_cost_usd=0.005,
        latency_seconds=0.0,
        ledger_file=str(ledger_file),
    )
    plan = CorpusPlan(
        packages=[pkg1, pkg2],
        cap_usd=0.040,
        approver="initial-spec",
        cap_timeout_seconds=20.0,
        results_dir=str(results_dir),
        manifest_output_dir=str(manifest_dir),
        fake_config=fake_config,
    )

    task_queue = "cap-signal-queue"

    async with await WorkflowEnvironment.start_local(ui=False) as wf_env:
        client = wf_env.client

        worker = Worker(
            client,
            task_queue=task_queue,
            workflows=[CertifyCorpusWorkflow, CertifyPackageWorkflow],
            activities=[generate_mutants, run_cell, settle_spend, synthesize_tests, write_manifest],
        )

        async with worker:
            handle = await client.start_workflow(
                CertifyCorpusWorkflow.run,
                plan,
                id=f"test-cap-signal-{int(time.time())}",
                task_queue=task_queue,
            )

            # Wait until pkg1 completes and workflow parks waiting for cap
            board = None
            t0 = time.time()
            while time.time() - t0 < 10.0:
                board = await handle.query(CertifyCorpusWorkflow.board)
                if board.completed_packages >= 1:
                    break
                await asyncio.sleep(0.1)

            assert board is not None
            assert board.completed_packages == 1
            assert board.package_statuses["pkg-cap-1"] in ("gold_structural", "gold_heldout")
            assert board.cumulative_spend == pytest.approx(0.035, abs=1e-4)
            assert board.cap_usd == pytest.approx(0.040, abs=1e-4)

            # Send signal: raise_cap by $0.05
            await handle.signal(CertifyCorpusWorkflow.raise_cap, args=[0.05, "approver-peter"])
            # Workflow resumes and completes pkg2
            manifest = await handle.result()

            assert manifest is not None
            assert not manifest.partial
            assert manifest.coverage_k == 2
            assert manifest.coverage_n == 2
            assert manifest.certified_packages == 2
            assert manifest.cumulative_spend_usd == pytest.approx(0.070, abs=1e-4)
            assert manifest.packages["pkg-cap-2"]["status"] in ("gold_structural", "gold_heldout")
