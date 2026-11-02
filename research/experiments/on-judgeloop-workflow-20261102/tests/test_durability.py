"""Test worker crash durability and exactly-once execution under worker kill."""

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

from judgeloop_workflow.models import HumanVerdict, RunSpec
from judgeloop_workflow.workflows import RunMonitorWorkflow


@pytest.mark.asyncio
async def test_worker_kill_durability_and_deduplication(tmp_path: Path) -> None:
    """Verify that a worker kill (-9) mid-run recovers without duplicate activity execution."""
    ledger_file = tmp_path / "durability_ledger.jsonl"
    worker_script = Path(__file__).parent / "worker_runner.py"

    run_spec = RunSpec(
        run_id="run-crash-test-003",
        dataset_id="terminal_wrench_dev",
        item_id="tw-case-77",
        steps=[
            {
                "step_id": 0,
                "action": "tamper_grader_bypass",
                "tool": "edit_grader",
                "output": "eval_patch bypassed",
            }
        ],
        review_sla_seconds=3600.0,
        ledger_path=str(ledger_file),
    )

    task_queue = "durability-judgeloop-queue"
    src_dir = str(Path(__file__).parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": f"{src_dir}:{os.environ.get('PYTHONPATH', '')}"}

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
            # 2. Start RunMonitorWorkflow
            handle = await client.start_workflow(
                RunMonitorWorkflow.run,
                run_spec,
                id=f"durability-judgeloop-wf-{int(time.time())}",
                task_queue=task_queue,
            )

            # 3. Wait until autoreview starts or completes in ledger
            t0 = time.time()
            autoreview_reached = False
            while time.time() - t0 < 15.0:
                if ledger_file.exists():
                    lines = [
                        json.loads(ln)
                        for ln in ledger_file.read_text(encoding="utf-8").splitlines()
                        if ln.strip()
                    ]
                    if any(
                        r.get("run_id") == run_spec.run_id
                        and r.get("event") in ("autoreview_start", "autoreview_complete")
                        for r in lines
                    ):
                        autoreview_reached = True
                        break
                await asyncio.sleep(0.1)

            assert autoreview_reached, "AutoReview activity was not reached within timeout"

            # 4. KILL -9 worker process 1 mid-execution!
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
                # 6. Wait until workflow reaches awaiting_human_verdict on the new worker
                for _ in range(50):
                    status = await handle.query(RunMonitorWorkflow.get_status)
                    if status.get("status") == "awaiting_human_verdict":
                        break
                    await asyncio.sleep(0.2)

                assert status.get("status") == "awaiting_human_verdict"

                # 7. Submit human verdict signal
                verdict = HumanVerdict(
                    run_id=run_spec.run_id,
                    reviewer_id="human-judge-recovery",
                    verdict="cheated",
                    cheat_mechanism="grader_bypass",
                    notes="Post-crash recovery verification",
                )
                await handle.signal(RunMonitorWorkflow.submit_human_verdict, verdict)

                # 8. Await workflow completion through worker 2
                result = await handle.result()
                assert result.run_id == run_spec.run_id
                assert result.final_verdict == "cheated"
                assert result.autoreview_verdict == "confirmed_cheat"
                assert result.agreement_with_model is True

                # 9. Verify ledger: activities completed without duplicate duplicate execution
                lines = [
                    json.loads(ln)
                    for ln in ledger_file.read_text(encoding="utf-8").splitlines()
                    if ln.strip()
                ]

                l0_starts = [
                    r
                    for r in lines
                    if r.get("run_id") == run_spec.run_id and r.get("event") == "l0_start"
                ]
                autoreview_completes = [
                    r
                    for r in lines
                    if r.get("run_id") == run_spec.run_id and r.get("event") == "autoreview_complete"
                ]
                queue_enqueues = [
                    r
                    for r in lines
                    if r.get("run_id") == run_spec.run_id and r.get("event") == "judge_queue_enqueued"
                ]
                verdict_records = [
                    r
                    for r in lines
                    if r.get("run_id") == run_spec.run_id and r.get("event") == "verdict_recorded"
                ]

                # Exactly 1 start for L0 and exactly 1 completed AutoReview, enqueue, verdict
                assert len(l0_starts) == 1, (
                    f"Expected exactly 1 l0_start, got {len(l0_starts)}"
                )
                assert len(autoreview_completes) == 1, (
                    f"Expected exactly 1 autoreview_complete, got {len(autoreview_completes)}"
                )
                assert len(queue_enqueues) == 1, (
                    f"Expected exactly 1 judge_queue_enqueued, got {len(queue_enqueues)}"
                )
                assert len(verdict_records) == 1, (
                    f"Expected exactly 1 verdict_recorded, got {len(verdict_records)}"
                )

            finally:
                worker2.terminate()
                try:
                    worker2.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    worker2.kill()

        finally:
            if worker1.poll() is None:
                worker1.kill()
