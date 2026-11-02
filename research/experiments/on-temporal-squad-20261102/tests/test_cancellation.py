from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_squad.activities import (
    launch_worker,
    read_report_state,
    route_quota_ok,
    settle_spend,
)
from temporal_squad.models import ItemPlan, SquadPlan
from temporal_squad.workflows import ItemWorkflow, SquadWorkflow


def is_process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
        ).stdout.strip()
        return bool(out and "Z" not in out)
    except OSError:
        return False


@pytest.mark.asyncio
async def test_cancellation_kills_process_group_no_orphans(tmp_path: Path) -> None:
    """Test (f): cancelling an item kills the child process group with no orphaned processes."""
    ledger_file = tmp_path / "ledger.jsonl"
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    fake_agent_path = Path(__file__).parent / "fake_agent.py"
    item_dir = run_dir / "raw" / "item-cancel"

    # Agent sleeps 60 seconds and spawns child processes in its process group
    item_cancel = ItemPlan(
        id="item-cancel",
        title="To be cancelled",
        cmd_override=[
            sys.executable,
            str(fake_agent_path),
            "--item-id",
            "item-cancel",
            "--raw-dir",
            str(item_dir),
            "--sleep",
            "60.0",
            "--spawn-children",
            "--ledger",
            str(ledger_file),
        ],
        raw_dir=str(item_dir),
        session_dir=str(run_dir / "sessions" / "item-cancel"),
    )

    plan = SquadPlan(
        model="cancel-route",
        budget_usd=10.0,
        max_parallel=2,
        run_dir=str(run_dir),
        items=[item_cancel],
    )

    task_queue = "cancel-queue"

    async with await WorkflowEnvironment.start_local(ui=False) as wf_env:
        client = wf_env.client

        worker = Worker(
            client,
            task_queue=task_queue,
            workflows=[SquadWorkflow, ItemWorkflow],
            activities=[launch_worker, read_report_state, route_quota_ok, settle_spend],
        )

        async with worker:
            handle = await client.start_workflow(
                SquadWorkflow.run,
                plan,
                id=f"test-cancel-{int(time.time())}",
                task_queue=task_queue,
            )

            # Wait until process and child are running
            child_pid_file = item_dir / "child.pid"
            meta_pid_file = item_dir / "worker-attempt-0.json"

            t0 = time.time()
            main_pid = None
            child_pid = None
            while time.time() - t0 < 10.0:
                if meta_pid_file.exists() and child_pid_file.exists():
                    try:
                        main_pid = json.loads(meta_pid_file.read_text()).get("pid")
                        child_pid = int(child_pid_file.read_text().strip())
                        if main_pid and child_pid:
                            # Verify both are alive
                            os.kill(main_pid, 0)
                            os.kill(child_pid, 0)
                            break
                    except (OSError, ValueError):
                        pass
                await asyncio.sleep(0.1)

            assert main_pid is not None, "Main process PID not found"
            assert child_pid is not None, "Child process PID not found"

            # Now cancel the item via signal
            await handle.signal(SquadWorkflow.cancel_item, "item-cancel")

            # Await workflow completion
            result = await handle.result()
            states = result.get("states", {})
            assert states["item-cancel"]["status"] == "skipped"

            # Wait for cancellation to reach activity and kill process group
            t_cleanup = time.time()
            while time.time() - t_cleanup < 5.0:
                if not is_process_running(main_pid) and not is_process_running(child_pid):
                    break
                await asyncio.sleep(0.1)

            assert not is_process_running(main_pid), f"Main process {main_pid} was not killed"
            assert not is_process_running(
                child_pid
            ), f"Child process {child_pid} was not killed (orphan detected)"
