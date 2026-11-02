"""Test signals (pause, cancel_package) and failure classes (InfraError retry, SubmissionFailure)."""

import asyncio
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
from certify_workflow.workflows import (
    CertifyCorpusWorkflow,
    CertifyPackageWorkflow,
    compute_cell_key,
)


@pytest.mark.asyncio
async def test_infra_error_retry_and_submission_failure(tmp_path: Path) -> None:
    """Verify InfraError retries with backoff and succeeds, while SubmissionFailure does not retry."""
    results_dir = tmp_path / "cells"
    results_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)

    pkg1 = PackageSpec(package_id="pkg-retry-1", mutant_count=2)
    pkg2 = PackageSpec(package_id="pkg-cancel-2", mutant_count=2)

    # Injected failures:
    # 1. Reference cell of pkg-retry-1 fails with InfraError for 2 attempts, then succeeds on 3rd attempt
    ref_key = compute_cell_key("pkg-retry-1", "reference")
    # 2. Witness cell fails candidate submission
    witness_key = compute_cell_key("pkg-retry-1", "witness")

    fake_config = FakeCellConfig(
        results_dir=str(results_dir),
        base_cost_usd=0.005,
        inject_infra_errors={ref_key: 2},
        inject_submission_failures=[witness_key],
    )
    plan = CorpusPlan(
        packages=[pkg1, pkg2],
        cap_usd=2.0,
        results_dir=str(results_dir),
        manifest_output_dir=str(manifest_dir),
        fake_config=fake_config,
    )

    task_queue = "failures-queue"

    async with await WorkflowEnvironment.start_time_skipping() as wf_env:
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
                id="test-failures-wf",
                task_queue=task_queue,
            )

            # Signal cancellation of pkg2 before it runs
            await handle.signal(CertifyCorpusWorkflow.cancel_package, "pkg-cancel-2")

            manifest = await handle.result()

            assert manifest is not None
            assert manifest.coverage_k == 2

            # pkg1 completed despite 2 transient infra errors on ref cell
            pkg1_res = manifest.packages["pkg-retry-1"]
            assert pkg1_res["status"] == "not_gold"  # Because witness cell had submission failure
            assert "Structural gate failure" in pkg1_res["failure_reason"]

            # pkg2 was cancelled via signal
            pkg2_res = manifest.packages["pkg-cancel-2"]
            assert pkg2_res["status"] == "cancelled"
            assert "Cancelled by signal" in pkg2_res["failure_reason"]


@pytest.mark.asyncio
async def test_pause_and_resume_signal(tmp_path: Path) -> None:
    """Verify pause signal holds dispatch and resume signal continues."""
    results_dir = tmp_path / "cells"
    results_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)

    pkg1 = PackageSpec(package_id="pkg-p-1", mutant_count=2)
    pkg2 = PackageSpec(package_id="pkg-p-2", mutant_count=2)

    fake_config = FakeCellConfig(
        results_dir=str(results_dir),
        base_cost_usd=0.005,
        latency_seconds=0.05,
    )
    plan = CorpusPlan(
        packages=[pkg1, pkg2],
        cap_usd=2.0,
        results_dir=str(results_dir),
        manifest_output_dir=str(manifest_dir),
        fake_config=fake_config,
    )

    task_queue = "pause-queue"

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
                id="test-pause-wf",
                task_queue=task_queue,
            )

            # Signal pause immediately
            await handle.signal(CertifyCorpusWorkflow.pause, True)
            await asyncio.sleep(0.2)

            board = await handle.query(CertifyCorpusWorkflow.board)
            assert board.paused is True

            # Resume
            await handle.signal(CertifyCorpusWorkflow.pause, False)
            manifest = await handle.result()

            assert manifest is not None
            assert manifest.coverage_k == 2
            assert not manifest.partial
