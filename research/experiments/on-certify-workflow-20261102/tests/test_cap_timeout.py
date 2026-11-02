"""Test (3): Cap timeout produces a partial manifest with correct coverage."""

from __future__ import annotations

import json
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
async def test_cap_timeout_produces_partial_manifest(tmp_path: Path) -> None:
    results_dir = tmp_path / "cells"
    results_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    ledger_file = tmp_path / "ledger.jsonl"

    pkg1 = PackageSpec(package_id="pkg-to-1", mutant_count=2)
    pkg2 = PackageSpec(package_id="pkg-to-2", mutant_count=2)
    pkg3 = PackageSpec(package_id="pkg-to-3", mutant_count=2)

    # 7 cells per package @ 0.005 = 0.035. Cap 0.040 allows only pkg1.
    fake_config = FakeCellConfig(
        results_dir=str(results_dir),
        base_cost_usd=0.005,
        latency_seconds=0.0,
        ledger_file=str(ledger_file),
    )
    plan = CorpusPlan(
        packages=[pkg1, pkg2, pkg3],
        cap_usd=0.040,
        approver="test-operator",
        cap_timeout_seconds=1.5,  # 1.5 second timeout for approval
        results_dir=str(results_dir),
        manifest_output_dir=str(manifest_dir),
        fake_config=fake_config,
    )

    task_queue = "cap-timeout-queue"

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
                id=f"test-cap-timeout-{int(time.time())}",
                task_queue=task_queue,
            )

            # Wait for workflow to finish without sending any raise_cap signal
            manifest = await handle.result()

            # Assert partial manifest invariants
            assert manifest is not None
            assert manifest.partial, "Manifest must be marked partial when cap is exhausted"
            assert manifest.certified_packages == 1
            assert manifest.total_packages == 3
            assert manifest.cumulative_spend_usd == pytest.approx(0.035, abs=1e-4)

            # Check per-package statuses
            assert manifest.packages["pkg-to-1"]["status"] in ("gold_structural", "gold_heldout")
            assert manifest.packages["pkg-to-2"]["status"] == "cap_exhausted"
            assert manifest.packages["pkg-to-3"]["status"] == "cap_exhausted"
            assert "CapReached" in manifest.packages["pkg-to-2"]["failure_reason"]
            assert "CapReached" in manifest.packages["pkg-to-3"]["failure_reason"]

            # Assert manifest was written to disk
            disk_manifest_file = manifest_dir / "gold-v1-manifest.json"
            assert disk_manifest_file.is_file()
            disk_data = json.loads(disk_manifest_file.read_text(encoding="utf-8"))
            assert disk_data["partial"] is True
            assert disk_data["certified_packages"] == 1
            assert disk_data["total_packages"] == 3
