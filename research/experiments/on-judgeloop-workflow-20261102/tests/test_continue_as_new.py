"""Test Continue-As-New for long-lived intake workflow with state compaction."""

from __future__ import annotations

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
from judgeloop_workflow.models import IntakeBatch, RunSpec
from judgeloop_workflow.workflows import FactoryIntakeWorkflow, RunMonitorWorkflow


@pytest.mark.asyncio
async def test_continue_as_new_intake_workflow(tmp_path: Path) -> None:
    """Verify that a long-lived intake workflow compacts history using continue-as-new."""
    ledger_file = tmp_path / "ledger.jsonl"

    runs = []
    # Create 6 clean runs (these complete quickly without waiting for human review)
    for i in range(1, 7):
        runs.append(
            RunSpec(
                run_id=f"intake-run-{i:03d}",
                dataset_id="terminal_wrench_dev",
                item_id=f"tw-clean-{i}",
                steps=[
                    {"step_id": 0, "action": "inspect_code", "tool": "grep"},
                    {"step_id": 1, "action": "edit_file", "tool": "edit"},
                ],
                ledger_path=str(ledger_file),
            )
        )

    # continue_as_new_threshold = 2, so 6 runs will trigger continue_as_new twice
    batch = IntakeBatch(
        batch_id="batch-can-001",
        runs=runs,
        max_concurrent_runs=2,
        continue_as_new_threshold=2,
        ledger_path=str(ledger_file),
    )

    task_queue = "can-intake-queue"

    async with await WorkflowEnvironment.start_time_skipping(
        test_server_startup_timeout=timedelta(seconds=20)
    ) as wf_env:
        client = wf_env.client

        worker = Worker(
            client,
            task_queue=task_queue,
            workflows=[FactoryIntakeWorkflow, RunMonitorWorkflow],
            activities=[
                evaluate_l0_activity,
                call_autoreview_activity,
                enqueue_judge_station_activity,
                escalate_review_sla_activity,
                record_verdict_and_calibrate_activity,
            ],
        )

        async with worker:
            handle = await client.start_workflow(
                FactoryIntakeWorkflow.run,
                batch,
                id="test-can-intake-wf",
                task_queue=task_queue,
            )

            result = await handle.result()

            assert result["batch_id"] == "batch-can-001"
            assert result["completed_total"] == 6
            assert len(result["summaries"]) == 6
            # Generation must have incremented due to continue_as_new
            assert result["final_generation"] >= 2, (
                f"Expected >= 2 generations, got {result['final_generation']}"
            )
