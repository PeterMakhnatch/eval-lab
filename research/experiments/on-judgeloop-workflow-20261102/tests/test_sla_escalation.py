"""Test review-SLA timer that escalates when human verdict is overdue."""

from __future__ import annotations

import json
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
async def test_review_sla_escalation_timer(tmp_path: Path) -> None:
    """Verify that exceeding the review SLA triggers escalation activity and alerts."""
    ledger_file = tmp_path / "ledger.jsonl"

    # Configure run with 24-hour SLA (86,400 seconds)
    one_day_seconds = 86400.0
    run_spec = RunSpec(
        run_id="run-sla-breach-002",
        dataset_id="terminal_wrench_dev",
        item_id="tw-case-99",
        steps=[
            {
                "step_id": 0,
                "action": "tamper_grader_env",
                "tool": "edit_grader",
                "output": "eval_patch hacked",
            }
        ],
        review_sla_seconds=one_day_seconds,
        ledger_path=str(ledger_file),
    )

    task_queue = "sla-queue"

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
                id="test-sla-escalation-wf",
                task_queue=task_queue,
            )

            # Wait until awaiting human verdict
            for _ in range(50):
                status = await handle.query(RunMonitorWorkflow.get_status)
                if status["status"] == "awaiting_human_verdict":
                    break
                await wf_env.sleep(0.1)

            assert status["status"] == "awaiting_human_verdict"
            assert status["sla_breached"] is False
            assert status["escalated"] is False

            # Simulate advancing time past SLA (e.g. 26 hours = 93,600 seconds)
            await wf_env.sleep(26 * 3600.0)

            # Query status: SLA timer must have fired and escalated
            for _ in range(50):
                status = await handle.query(RunMonitorWorkflow.get_status)
                if status["sla_breached"]:
                    break
                await wf_env.sleep(0.1)

            assert status["sla_breached"] is True
            assert status["escalated"] is True
            assert status["status"] == "sla_escalated"

            # Check ledger to confirm escalate_review_sla_activity executed
            assert ledger_file.exists()
            records = [
                json.loads(line)
                for line in ledger_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            escalations = [r for r in records if r.get("event") == "sla_escalated"]
            assert len(escalations) == 1
            assert escalations[0]["run_id"] == run_spec.run_id
            assert escalations[0]["level"] == "manager_escalation"

            # Reviewer finally arrives at simulated hour 72 (day 3) and submits verdict
            await wf_env.sleep(46 * 3600.0)

            verdict = HumanVerdict(
                run_id=run_spec.run_id,
                reviewer_id="human-judge-bob",
                verdict="cheated",
                cheat_mechanism="eval_patch_override",
                notes="Confirmed late adjudication following manager escalation.",
            )
            await handle.signal(RunMonitorWorkflow.submit_human_verdict, verdict)

            result = await handle.result()
            assert result.final_verdict == "cheated"
            assert result.sla_breached is True
            assert result.escalated is True
            assert result.agreement_with_model is True
