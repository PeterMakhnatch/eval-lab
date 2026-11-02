"""Workflows for the judge loop and monitor durable workflow."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from judgeloop_workflow.activities import (
        call_autoreview_activity,
        enqueue_judge_station_activity,
        escalate_review_sla_activity,
        evaluate_l0_activity,
        record_verdict_and_calibrate_activity,
    )
    from judgeloop_workflow.models import (
        AutoReviewRequest,
        AutoReviewResult,
        CalibrationOutcome,
        EscalationNotice,
        HumanVerdict,
        IntakeBatch,
        JudgeQueueEntry,
        L0Evaluation,
        RunSpec,
        RunSummary,
    )


@workflow.defn
class RunMonitorWorkflow:
    """Per-run workflow managing the live monitor, AutoReview, and human review loop."""

    def __init__(self) -> None:
        self._run_spec: RunSpec | None = None
        self._status: str = "initialized"
        self._l0: L0Evaluation | None = None
        self._autoreview: AutoReviewResult | None = None
        self._human_verdict: HumanVerdict | None = None
        self._sla_breached: bool = False
        self._escalated: bool = False
        self._calibration: CalibrationOutcome | None = None

    @workflow.signal
    def submit_human_verdict(self, verdict: HumanVerdict) -> None:
        """Signal received from human reviewer / Judge Station."""
        self._human_verdict = verdict

    @workflow.query
    def get_status(self) -> dict[str, Any]:
        """Query current execution state and adjudication metadata."""
        return {
            "run_id": self._run_spec.run_id if self._run_spec else None,
            "status": self._status,
            "l0_triggered": self._l0.triggered if self._l0 else None,
            "trigger_kind": self._l0.trigger_kind if self._l0 else None,
            "autoreview_verdict": self._autoreview.verdict if self._autoreview else None,
            "human_verdict": self._human_verdict.verdict if self._human_verdict else None,
            "sla_breached": self._sla_breached,
            "escalated": self._escalated,
            "agreement": self._calibration.agreement if self._calibration else None,
        }

    @workflow.run
    async def run(self, run: RunSpec) -> RunSummary:
        self._run_spec = run
        self._status = "evaluating_l0"

        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=1),
            maximum_interval=timedelta(seconds=10),
            maximum_attempts=3,
        )

        # Stage 1: Deterministic L0 tripwire evaluation
        self._l0 = await workflow.execute_activity(
            evaluate_l0_activity,
            run,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )

        if self._l0.triggered:
            # Stage 2: Model AutoReview request
            self._status = "requesting_autoreview"
            req = AutoReviewRequest(
                run_id=run.run_id,
                trigger_kind=self._l0.trigger_kind,
                prefix_step=self._l0.step_index or 0,
                model_route=run.model_route,
                ledger_path=run.ledger_path,
            )
            self._autoreview = await workflow.execute_activity(
                call_autoreview_activity,
                req,
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=retry_policy,
            )

            # If model flags suspicious or confirmed cheat -> route to Judge Station human queue
            if self._autoreview.verdict in ("confirmed_cheat", "suspicious"):
                self._status = "awaiting_human_verdict"
                priority = 2 if self._autoreview.verdict == "confirmed_cheat" else 1
                entry = JudgeQueueEntry(
                    run_id=run.run_id,
                    priority=priority,
                    trigger_kind=self._l0.trigger_kind,
                    enqueued_at=workflow.now().isoformat(),
                    hidden_model_verdict=self._autoreview.verdict,
                )
                await workflow.execute_activity(
                    enqueue_judge_station_activity,
                    args=[entry, run.ledger_path],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry_policy,
                )

                # Stage 3: Durable timer wait with SLA escalation
                sla_timeout = timedelta(seconds=run.review_sla_seconds)
                try:
                    await workflow.wait_condition(
                        lambda: self._human_verdict is not None,
                        timeout=sla_timeout,
                    )
                except TimeoutError:
                    # SLA timer fired before human verdict arrived!
                    self._sla_breached = True
                    self._escalated = True
                    self._status = "sla_escalated"
                    notice = EscalationNotice(
                        run_id=run.run_id,
                        escalated_at=workflow.now().isoformat(),
                        elapsed_seconds=run.review_sla_seconds,
                        sla_threshold_seconds=run.review_sla_seconds,
                        level="manager_escalation",
                        reason=f"Review SLA of {run.review_sla_seconds}s exceeded without verdict.",
                    )
                    await workflow.execute_activity(
                        escalate_review_sla_activity,
                        args=[notice, run.ledger_path],
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=retry_policy,
                    )
                    # After escalation, wait indefinitely for human verdict signal
                    await workflow.wait_condition(lambda: self._human_verdict is not None)

                # Stage 4: Calibration & final recording upon human verdict
                self._status = "calibrating"
                self._calibration = await workflow.execute_activity(
                    record_verdict_and_calibrate_activity,
                    args=[self._human_verdict, self._autoreview, run.ledger_path],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry_policy,
                )

                self._status = "completed"
                return RunSummary(
                    run_id=run.run_id,
                    final_verdict=self._human_verdict.verdict,
                    l0_triggered=True,
                    trigger_kind=self._l0.trigger_kind,
                    autoreview_verdict=self._autoreview.verdict,
                    human_verdict=self._human_verdict.verdict,
                    sla_breached=self._sla_breached,
                    escalated=self._escalated,
                    agreement_with_model=self._calibration.agreement,
                    completed_at=workflow.now().isoformat(),
                )

        # Clean run (no tripwire or AutoReview found clean)
        self._status = "completed"
        return RunSummary(
            run_id=run.run_id,
            final_verdict="clean",
            l0_triggered=self._l0.triggered,
            trigger_kind=self._l0.trigger_kind,
            autoreview_verdict=self._autoreview.verdict if self._autoreview else None,
            human_verdict=None,
            sla_breached=False,
            escalated=False,
            agreement_with_model=(
                True if (self._autoreview and self._autoreview.verdict == "clean") else None
            ),
            completed_at=workflow.now().isoformat(),
        )


@workflow.defn
class FactoryIntakeWorkflow:
    """Long-lived intake supervisor workflow with Continue-As-New state compaction."""

    def __init__(self) -> None:
        self._generation: int = 1
        self._completed_total: int = 0
        self._in_flight: int = 0
        self._summaries: list[RunSummary] = []

    @workflow.query
    def get_intake_progress(self) -> dict[str, Any]:
        return {
            "generation": self._generation,
            "completed_total": self._completed_total,
            "in_flight": self._in_flight,
            "summary_count": len(self._summaries),
        }

    @workflow.run
    async def run(self, batch: IntakeBatch) -> dict[str, Any]:
        self._generation = batch.generation
        self._completed_total = batch.completed_count
        self._completed_run_ids = list(batch.completed_run_ids)

        pending_runs = list(batch.runs)
        completed_this_gen = 0
        active_tasks: dict[str, asyncio.Task[RunSummary]] = {}

        async def _run_child(run_item: RunSpec) -> RunSummary:
            child_handle = await workflow.start_child_workflow(
                RunMonitorWorkflow.run,
                run_item,
                id=f"{workflow.info().workflow_id}-run-{run_item.run_id}",
            )
            return await child_handle

        while pending_runs or active_tasks:
            # Spawn child workflows up to max concurrency
            while pending_runs and len(active_tasks) < batch.max_concurrent_runs:
                next_run = pending_runs.pop(0)
                active_tasks[next_run.run_id] = asyncio.create_task(_run_child(next_run))

            self._in_flight = len(active_tasks)

            if not active_tasks:
                break

            # Wait for at least one active child to finish
            done, _ = await workflow.wait(
                list(active_tasks.values()),
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in done:
                finished_id = None
                for rid, t in active_tasks.items():
                    if t == task:
                        finished_id = rid
                        break
                if finished_id:
                    active_tasks.pop(finished_id)
                summary = await task
                self._summaries.append(summary)
                self._completed_run_ids.append(summary.run_id)
                self._completed_total += 1
                completed_this_gen += 1
            # Check if continue-as-new threshold reached for state compaction
            if completed_this_gen >= batch.continue_as_new_threshold and pending_runs:
                # Drain any remaining active tasks before continuing as new
                while active_tasks:
                    done_remaining, _ = await workflow.wait(
                        list(active_tasks.values()),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done_remaining:
                        finished_id = None
                        for rid, t in active_tasks.items():
                            if t == task:
                                finished_id = rid
                                break
                        if finished_id:
                            active_tasks.pop(finished_id)
                        summary = await task
                        self._summaries.append(summary)
                        self._completed_run_ids.append(summary.run_id)
                        self._completed_total += 1
                next_batch = IntakeBatch(
                    batch_id=batch.batch_id,
                    runs=pending_runs,
                    max_concurrent_runs=batch.max_concurrent_runs,
                    continue_as_new_threshold=batch.continue_as_new_threshold,
                    generation=batch.generation + 1,
                    completed_count=self._completed_total,
                    completed_run_ids=self._completed_run_ids,
                    ledger_path=batch.ledger_path,
                )
                workflow.continue_as_new(next_batch)
        return {
            "batch_id": batch.batch_id,
            "final_generation": self._generation,
            "completed_total": self._completed_total,
            "summaries": self._completed_run_ids,
            "finished_at": workflow.now().isoformat(),
        }
