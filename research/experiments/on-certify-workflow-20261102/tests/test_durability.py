"""Test (1): Exactly-once cell execution under a worker kill."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment

from certify_workflow.models import CorpusPlan, FakeCellConfig, PackageSpec
from certify_workflow.workflows import CertifyCorpusWorkflow


@pytest.mark.asyncio
async def test_exactly_once_cell_execution_under_worker_kill(tmp_path: Path) -> None:
    results_dir = tmp_path / "cells"
    results_dir.mkdir(parents=True, exist_ok=True)
    ledger_file = tmp_path / "ledger.jsonl"
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)

    worker_script = Path(__file__).parent / "worker_runner.py"

    # Plan with 1 package, mutant_count=2 (total 8 cells: ref, nop, witness, exit0, 1 train, 1 eval, 1 evalbase)
    pkg = PackageSpec(package_id="pkg-durability-1", mutant_count=2)
    fake_config = FakeCellConfig(
        results_dir=str(results_dir),
        base_cost_usd=0.005,
        latency_seconds=0.25,  # Each cell runs for 250ms to allow interception mid-execution
        ledger_file=str(ledger_file),
    )
    plan = CorpusPlan(
        packages=[pkg],
        cap_usd=2.0,
        results_dir=str(results_dir),
        manifest_output_dir=str(manifest_dir),
        fake_config=fake_config,
    )

    task_queue = "durability-queue"
    pythonpath = str(Path(__file__).parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": pythonpath}

    async with await WorkflowEnvironment.start_local(ui=False) as wf_env:
        client = wf_env.client
        target = client.service_client.config.target_host

        # 1. Start worker process 1
        worker1 = subprocess.Popen(
            [
                sys.executable,
                str(worker_script),
                "--target",
                target,
                "--task-queue",
                task_queue,
            ],
            env=env,
        )

        try:
            # 2. Start CertifyCorpusWorkflow
            handle = await client.start_workflow(
                CertifyCorpusWorkflow.run,
                plan,
                id=f"durability-test-{int(time.time())}",
                task_queue=task_queue,
            )

            # 3. Wait until at least 1 cell has begun execution and written to ledger
            t0 = time.time()
            started = False
            while time.time() - t0 < 10.0:
                if ledger_file.is_file():
                    lines = [ln.strip() for ln in ledger_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
                    if len(lines) >= 1:
                        started = True
                        break
                await asyncio.sleep(0.05)

            assert started, "Workflow did not start executing cells within 10 seconds"

            # 4. KILL -9 worker process 1 abruptly mid-execution
            os.kill(worker1.pid, signal.SIGKILL)
            worker1.wait()

            # Wait briefly while worker is dead
            await asyncio.sleep(1.0)

            # 5. Start worker process 2 on the same task queue
            worker2 = subprocess.Popen(
                [
                    sys.executable,
                    str(worker_script),
                    "--target",
                    target,
                    "--task-queue",
                    task_queue,
                ],
                env=env,
            )

            try:
                # 6. Await workflow completion via worker 2
                manifest = await handle.result()
                assert manifest is not None
                assert manifest.coverage_k == 1
                assert manifest.coverage_n == 1
                assert not manifest.partial

                pkg_res = manifest.packages["pkg-durability-1"]
                assert pkg_res["status"] in ("gold_structural", "gold_heldout")
                assert pkg_res["cost_usd"] > 0.0

                # 7. Audit ledger: each cell was executed exactly once (no duplicate runs)
                lines = [json.loads(ln) for ln in ledger_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
                cell_runs = [r for r in lines if r.get("event") == "run_cell"]

                # Check unique cell_keys
                executed_keys = [r["cell_key"] for r in cell_runs]
                unique_keys = set(executed_keys)

                assert len(executed_keys) == len(unique_keys), (
                    f"Duplicate cell executions detected in ledger! "
                    f"Total runs: {len(executed_keys)}, unique keys: {len(unique_keys)}"
                )

                # Settle spend was also executed
                settles = [r for r in lines if r.get("event") == "settle_spend"]
                assert len(settles) == 1, f"Expected exactly 1 spend settlement, got {len(settles)}"

            finally:
                worker2.terminate()
                try:
                    worker2.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    worker2.kill()

        finally:
            if worker1.poll() is None:
                worker1.kill()
