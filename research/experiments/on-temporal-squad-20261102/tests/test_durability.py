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

from temporal_squad.models import ItemPlan, SquadPlan
from temporal_squad.workflows import SquadWorkflow


@pytest.mark.asyncio
async def test_worker_crash_durability(tmp_path: Path) -> None:
    """Test (a): kill -9 worker process mid-item, restart worker, assert exact-once execution."""
    ledger_file = tmp_path / "ledger.jsonl"
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    fake_agent_path = Path(__file__).parent / "fake_agent.py"
    worker_script = Path(__file__).parent / "worker_runner.py"

    item_a_dir = run_dir / "raw" / "item-A"
    item_b_dir = run_dir / "raw" / "item-B"

    # Item A sleeps for 2.0 seconds; Item B depends on Item A
    item_a = ItemPlan(
        id="item-A",
        title="First item",
        cmd_override=[
            sys.executable,
            str(fake_agent_path),
            "--item-id",
            "item-A",
            "--raw-dir",
            str(item_a_dir),
            "--sleep",
            "2.0",
            "--ledger",
            str(ledger_file),
        ],
        raw_dir=str(item_a_dir),
        session_dir=str(run_dir / "sessions" / "item-A"),
    )
    item_b = ItemPlan(
        id="item-B",
        title="Second item",
        needs=["item-A"],
        cmd_override=[
            sys.executable,
            str(fake_agent_path),
            "--item-id",
            "item-B",
            "--raw-dir",
            str(item_b_dir),
            "--sleep",
            "0.5",
            "--ledger",
            str(ledger_file),
        ],
        raw_dir=str(item_b_dir),
        session_dir=str(run_dir / "sessions" / "item-B"),
    )

    plan = SquadPlan(
        model="fake-route",
        budget_usd=10.0,
        max_parallel=2,
        run_dir=str(run_dir),
        items=[item_a, item_b],
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
            # 2. Start SquadWorkflow
            handle = await client.start_workflow(
                SquadWorkflow.run,
                plan,
                id=f"durability-workflow-{int(time.time())}",
                task_queue=task_queue,
            )

            # 3. Wait until item-A starts executing
            t0 = time.time()
            item_a_started = False
            while time.time() - t0 < 10.0:
                if ledger_file.exists():
                    lines = [json.loads(ln) for ln in ledger_file.read_text().splitlines() if ln.strip()]
                    if any(r.get("item_id") == "item-A" and r.get("event") == "start" for r in lines):
                        item_a_started = True
                        break
                await asyncio.sleep(0.1)

            assert item_a_started, "item-A did not start within timeout"

            # 4. KILL -9 worker process 1 mid-item!
            os.kill(worker1.pid, signal.SIGKILL)
            worker1.wait()

            # Wait briefly while worker is dead and item-A finishes its agent work
            await asyncio.sleep(2.5)

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
                # 6. Await workflow completion through worker 2
                result = await handle.result()
                assert result is not None

                # 7. Check final states
                states = result.get("states", {})
                assert states["item-A"]["status"] == "done"
                assert states["item-B"]["status"] == "done"

                # 8. Check ledger: each item was executed exactly once
                lines = [json.loads(ln) for ln in ledger_file.read_text().splitlines() if ln.strip()]
                starts_a = [r for r in lines if r.get("item_id") == "item-A" and r.get("event") == "start"]
                starts_b = [r for r in lines if r.get("item_id") == "item-B" and r.get("event") == "start"]

                assert len(starts_a) == 1, f"Expected item-A exactly 1 start, got {len(starts_a)}"
                assert len(starts_b) == 1, f"Expected item-B exactly 1 start, got {len(starts_b)}"

            finally:
                worker2.terminate()
                try:
                    worker2.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    worker2.kill()

        finally:
            if worker1.poll() is None:
                worker1.kill()
