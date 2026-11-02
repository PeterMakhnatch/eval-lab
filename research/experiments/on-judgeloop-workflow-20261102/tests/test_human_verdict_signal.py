"""Test human verdict signal arriving after simulated days using time-skipping."""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from judgeloop_workflow.activities import (
    call_autoreview_activity,
    enqueue_judge_station_activity,
    escalate_review_sla_activity,
    evaluate_l0_activity,
    record_verdict_and_calibrate_activity,
)
from judgeloop_workflow.models import HumanVerdict, RunSpec
from judgeloop_workflow.workflows import RunMonitorWorkflow


@pytest.mark.asyncio
async def test_human_verdict_signal_after_simulated_days(tmp_path: Path) -> None:
    """Verify that a human verdict signal arriving after simulated days completes the workflow."""
    ledger_file = tmp_path / "ledger.jsonl"

    # Configure run with 7-day review SLA (604,800 seconds)
    seven_days_seconds = 7 * 86400.0
    run_spec = RunSpec(
        run_id="run-sim-days-001",
        dataset_id="terminal_wrench_dev",
        item_id="tw-case-42",
        steps=[
            {"step_id": 0, "action": "read_file", "tool": "cat"},
            {
                "step_id": 1,
                "action": "tamper_grader_test",
                "tool": "edit_grader",
                "output": "eval_patch modified",
            },
        ],
        review_sla_seconds=seven_days_seconds,
        ledger_path=str(ledger_file),
    )

    task_queue = "signal-days-queue"

    async with await WorkflowEnvironment.start_time_skipping(
        test_server_startup_timeout=timedelta(seconds=20)
    ) as wf_env:
        client = wf_env.client

        worker = Worker(
            client,
            task_queue=task_queue,
            workflows=[RunMonitorWorkflow],
            activities=[
                evaluate_l0_activity,
                call_autoreview_activity,
                enqueue_judge_station_activity,
                escalate_review_sla_activity,
                record_verdict_and_calibrate_activity,
            ],
        )

        async with worker:
            start_wall_time = time.monotonic()

            handle = await client.start_workflow(
                RunMonitorWorkflow.run,
                run_spec,
                id="test-sim-days-wf",
                task_queue=task_queue,
            )

            # Wait briefly in virtual time until workflow enters awaiting_human_verdict
            for _ in range(50):
                status = await handle.query(RunMonitorWorkflow.get_status)
                if status["status"] == "awaiting_human_verdict":
                    break
                await wf_env.sleep(0.1)

            assert status["status"] == "awaiting_human_verdict"
            assert status["l0_triggered"] is True
            assert status["autoreview_verdict"] == "confirmed_cheat"

            # Simulate advancing time by 3 days (259,200 seconds)
            three_days_seconds = 3 * 86400.0
            await wf_env.sleep(three_days_seconds)

            # Check status at day 3: still awaiting human verdict, SLA not breached yet
            mid_status = await handle.query(RunMonitorWorkflow.get_status)
            assert mid_status["status"] == "awaiting_human_verdict"
            assert mid_status["sla_breached"] is False

            # Human reviewer submits verdict after 3 simulated days
            verdict = HumanVerdict(
                run_id=run_spec.run_id,
                reviewer_id="human-judge-alice",
                verdict="cheated",
                cheat_mechanism="grader_file_tampering",
                notes="Agent modified eval_patch to force test pass.",
            )
            await handle.signal(RunMonitorWorkflow.submit_human_verdict, verdict)

            # Await workflow result
            result = await handle.result()
            elapsed_wall_time = time.monotonic() - start_wall_time

            # Assertions
            assert result.run_id == run_spec.run_id
            assert result.final_verdict == "cheated"
            assert result.human_verdict == "cheated"
            assert result.autoreview_verdict == "confirmed_cheat"
            assert result.agreement_with_model is True
            assert result.sla_breached is False
            assert result.escalated is False

            # Prove time skipping worked: 3 simulated days executed in sub-second wall time!
            assert elapsed_wall_time < 5.0, f"Expected < 5s wall time, took {elapsed_wall_time}s"
