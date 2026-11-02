"""Test (4): Per-route concurrency limits hold."""

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from certify_workflow.activities import (
    generate_mutants,
    run_cell,
    settle_spend,
    synthesize_tests,
    write_manifest,
)
from certify_workflow.models import (
    CellResult,
    CellSpec,
    CorpusPlan,
    FakeCellConfig,
    MutantSummary,
    PackageSpec,
)
from certify_workflow.workflows import CertifyCorpusWorkflow, CertifyPackageWorkflow


@pytest.mark.asyncio
async def test_per_route_concurrency_limits(tmp_path: Path) -> None:
    results_dir = tmp_path / "cells"
    results_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    timing_file = tmp_path / "activity_intervals.jsonl"

    modal_limit = 2
    synth_limit = 1

    pkgs = [PackageSpec(package_id=f"pkg-conc-{i}", mutant_count=2) for i in range(4)]
    fake_config = FakeCellConfig(
        results_dir=str(results_dir),
        base_cost_usd=0.005,
        latency_seconds=0.05,
    )
    plan = CorpusPlan(
        packages=pkgs,
        cap_usd=5.0,
        modal_concurrency=modal_limit,
        synth_concurrency=synth_limit,
        results_dir=str(results_dir),
        manifest_output_dir=str(manifest_dir),
        fake_config=fake_config,
    )

    lock = threading.Lock()
    active_counts: dict[str, int] = {"modal": 0, "synth": 0}
    max_observed: dict[str, int] = {"modal": 0, "synth": 0}

    # Custom monitored wrappers for activities to observe route concurrency
    @activity.defn(name="run_cell")
    async def monitored_run_cell(spec: CellSpec, config: FakeCellConfig) -> CellResult:
        with lock:
            active_counts["modal"] += 1
            if active_counts["modal"] > max_observed["modal"]:
                max_observed["modal"] = active_counts["modal"]
            t_start = time.time()

        try:
            res = await run_cell(spec, config)
            return res
        finally:
            with lock:
                t_end = time.time()
                active_counts["modal"] -= 1
                with timing_file.open("a", encoding="utf-8") as f:
                    f.write(
                        json.dumps({
                            "route": "modal",
                            "start": t_start,
                            "end": t_end,
                            "key": spec.cell_key,
                        })
                        + "\n"
                    )

    @activity.defn(name="synthesize_tests")
    async def monitored_synthesize_tests(pkg: PackageSpec, train_mutants: list[MutantSummary]):
        with lock:
            active_counts["synth"] += 1
            if active_counts["synth"] > max_observed["synth"]:
                max_observed["synth"] = active_counts["synth"]
            t_start = time.time()

        try:
            res = await synthesize_tests(pkg, train_mutants)
            await asyncio.sleep(0.05)  # Simulate synthesis delay
            return res
        finally:
            with lock:
                t_end = time.time()
                active_counts["synth"] -= 1
                with timing_file.open("a", encoding="utf-8") as f:
                    f.write(
                        json.dumps({
                            "route": "synth",
                            "start": t_start,
                            "end": t_end,
                            "pkg": pkg.package_id,
                        })
                        + "\n"
                    )

    task_queue = "concurrency-queue"

    async with await WorkflowEnvironment.start_local(ui=False) as wf_env:
        client = wf_env.client

        worker = Worker(
            client,
            task_queue=task_queue,
            workflows=[CertifyCorpusWorkflow, CertifyPackageWorkflow],
            activities=[
                generate_mutants,
                monitored_run_cell,
                settle_spend,
                monitored_synthesize_tests,
                write_manifest,
            ],
            max_concurrent_activities=10,
        )

        async with worker:
            handle = await client.start_workflow(
                CertifyCorpusWorkflow.run,
                plan,
                id="test-concurrency-wf",
                task_queue=task_queue,
            )

            manifest = await handle.result()
            assert manifest is not None
            assert manifest.coverage_k == 4
            assert not manifest.partial

            # Check peak concurrent activity observations
            assert max_observed["modal"] <= modal_limit, (
                f"Peak Modal concurrency {max_observed['modal']} exceeded configured limit {modal_limit}"
            )
            assert max_observed["synth"] <= synth_limit, (
                f"Peak Synth concurrency {max_observed['synth']} exceeded configured limit {synth_limit}"
            )
