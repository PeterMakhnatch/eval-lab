"""Test full lifecycle scenarios: clean runs, suspicious cascade triggers, and target surface writes."""

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
from judgeloop_workflow.models import HumanVerdict, RunSpec
from judgeloop_workflow.workflows import RunMonitorWorkflow


@pytest.mark.asyncio
async def test_clean_run_lifecycle(tmp_path: Path) -> None:
    """A clean run without tripwire triggers completes immediately with clean verdict."""
    ledger_file = tmp_path / "clean_ledger.jsonl"
    run_spec = RunSpec(
        run_id="run-clean-010",
        dataset_id="terminal_wrench_dev",
        item_id="tw-case-10",
        steps=[
            {"step_id": 0, "action": "inspect_directory", "tool": "glob"},
            {"step_id": 1, "action": "run_unit_tests", "tool": "bash", "output": "tests passed"},
        ],
        ledger_path=str(ledger_file),
    )

    task_queue = "clean-queue"

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
            handle = await client.start_workflow(
                RunMonitorWorkflow.run,
                run_spec,
                id="test-clean-wf",
                task_queue=task_queue,
            )
            result = await handle.result()

            assert result.run_id == run_spec.run_id
            assert result.final_verdict == "clean"
            assert result.l0_triggered is False
            assert result.autoreview_verdict is None
            assert result.human_verdict is None
            assert result.sla_breached is False


@pytest.mark.asyncio
async def test_cascade_stage1_to_human_clean_disagreement(tmp_path: Path) -> None:
    """Cascade Stage-1 escalation triggers AutoReview (suspicious), human overrules to clean."""
    ledger_file = tmp_path / "cascade_ledger.jsonl"
    run_spec = RunSpec(
        run_id="run-cascade-020",
        dataset_id="terminal_wrench_dev",
        item_id="tw-case-20",
        steps=[
            {
                "step_id": 0,
                "action": "suspicious_loop",
                "cascade_escalation": True,
                "output": "stage-1 escalation threshold met",
            }
        ],
        ledger_path=str(ledger_file),
    )

    task_queue = "cascade-queue"

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
            handle = await client.start_workflow(
                RunMonitorWorkflow.run,
                run_spec,
                id="test-cascade-wf",
                task_queue=task_queue,
            )

            # Wait until awaiting human verdict
            for _ in range(50):
                status = await handle.query(RunMonitorWorkflow.get_status)
                if status["status"] == "awaiting_human_verdict":
                    break
                await wf_env.sleep(0.1)

            assert status["status"] == "awaiting_human_verdict"
            assert status["trigger_kind"] == "cascade_stage1"
            assert status["autoreview_verdict"] == "suspicious"

            # Human judge overrules suspicious model verdict to clean
            verdict = HumanVerdict(
                run_id=run_spec.run_id,
                reviewer_id="human-judge-charlie",
                verdict="clean",
                cheat_mechanism=None,
                notes="Reviewed causal prefix: benign repetition, not an exploit.",
            )
            await handle.signal(RunMonitorWorkflow.submit_human_verdict, verdict)

            result = await handle.result()
            assert result.final_verdict == "clean"
            assert result.autoreview_verdict == "suspicious"
            assert result.human_verdict == "clean"
            assert result.agreement_with_model is False
