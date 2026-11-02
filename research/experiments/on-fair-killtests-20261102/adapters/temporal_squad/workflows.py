from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from temporal_squad.activities import (
        launch_worker,
        read_report_state,
        route_quota_ok,
        settle_spend,
    )
    from temporal_squad.models import (
        ItemPlan,
        ItemResult,
        ItemState,
        ItemWorkflowInput,
        LaunchWorkerRequest,
        ReportResult,
        SettleSpendRequest,
        SpendResult,
        SquadPlan,
        SquadWorkflowInput,
    )


@workflow.defn
class ItemWorkflow:
    @workflow.run
    async def run(self, inp: ItemWorkflowInput) -> ItemResult:
        item = inp.item
        plan = inp.plan
        route = item.route or plan.model
        attempt = inp.attempt
        max_attempts = max(1, item.retry + 1)
        last_error_note = ""

        # 1. Route quota verification (parks item if quota exhausted)
        try:
            await workflow.execute_activity(
                route_quota_ok,
                args=[plan.quota_file, route],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(
                    maximum_attempts=1,
                    non_retryable_error_types=["RouteExhausted"],
                ),
            )
        except ActivityError as exc:
            cause = exc.cause
            if isinstance(cause, ApplicationError) and cause.type == "RouteExhausted":
                return ItemResult(
                    id=item.id,
                    status="blocked",
                    minutes=0.0,
                    cost=0.0,
                    note=f"RouteExhausted: {cause.message}",
                )
            raise

        # 2. Worker launch and execution loop with continuation retry support
        current_attempt = attempt
        while current_attempt < max_attempts:
            # Build launch request
            cmd = item.cmd_override or [
                "omp",
                "-p",
                "--model",
                route,
                "--thinking",
                item.thinking,
                "--tools",
                item.tools,
            ]
            raw_dir = item.raw_dir or f"{plan.run_dir}/raw/{item.id}"
            session_dir = item.session_dir or f"{plan.run_dir}/sessions/{item.id}"

            launch_req = LaunchWorkerRequest(
                item_id=item.id,
                attempt=current_attempt,
                cmd=cmd,
                cwd=raw_dir,
                raw_dir=raw_dir,
                env_vars=item.env_override or {},
                heartbeat_interval_sec=1.0,
            )

            # Execute launch activity
            t0 = workflow.now()
            await workflow.execute_activity(
                launch_worker,
                args=[launch_req],
                start_to_close_timeout=timedelta(minutes=item.max_minutes),
                heartbeat_timeout=timedelta(seconds=30),
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=1),
                    backoff_coefficient=2.0,
                    maximum_attempts=3,
                    non_retryable_error_types=["RouteExhausted", "BudgetExceeded"],
                ),
            )
            elapsed_minutes = (workflow.now() - t0).total_seconds() / 60.0

            # 3. Read report state
            report_status = "failed"
            report_note = ""
            try:
                rep_res: ReportResult = await workflow.execute_activity(
                    read_report_state,
                    args=[raw_dir, item.id],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(
                        maximum_attempts=1,
                        non_retryable_error_types=["ReportMissing"],
                    ),
                )
                report_status = rep_res.status
                report_note = rep_res.note
            except ActivityError as exc:
                cause = exc.cause
                if isinstance(cause, ApplicationError) and cause.type == "ReportMissing":
                    last_error_note = cause.message
                    if current_attempt + 1 < max_attempts:
                        # Retry with continuation
                        current_attempt += 1
                        workflow.logger.info(
                            f"Item {item.id} missing report; retry continuation {current_attempt}"
                        )
                        continue
                    report_status = "failed"
                    report_note = last_error_note
                else:
                    raise

            # 4. Settle spend
            settle_req = SettleSpendRequest(
                item_id=item.id,
                attempt=current_attempt,
                session_dir=session_dir,
                route=route,
                subscription=plan.subscription,
                dry_ledger=plan.dry_ledger,
                fixed_cost=float(item.env_override.get("MOCK_COST", 0.0))
                if item.env_override and "MOCK_COST" in item.env_override
                else None,
            )
            spend_res: SpendResult = await workflow.execute_activity(
                settle_spend,
                args=[settle_req],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )

            # Check if per-item max_usd was exceeded
            final_status = report_status
            final_note = report_note
            if spend_res.cost > item.max_usd:
                final_status = "killed-budget"
                final_note = f"Spend ${spend_res.cost:.2f} exceeded max_usd ${item.max_usd:.2f}"

            attestation = {
                "requested": route,
                "served": spend_res.served,
                "off_route": spend_res.off_route,
                "fallback_events": spend_res.fallback_events,
            }

            return ItemResult(
                id=item.id,
                status=final_status,
                minutes=elapsed_minutes,
                cost=spend_res.cost,
                note=final_note,
                attestation=attestation,
            )

        # Retries exhausted without valid report
        return ItemResult(
            id=item.id,
            status="failed",
            minutes=0.0,
            cost=0.0,
            note=f"Retries exhausted: {last_error_note}",
        )


@workflow.defn
class SquadWorkflow:
    def __init__(self) -> None:
        self.plan: SquadPlan = SquadPlan()
        self.items: dict[str, ItemPlan] = {}
        self.states: dict[str, ItemState] = {}
        self.cumulative_spend: float = 0.0
        self.completed_item_ids: list[str] = []
        self.released_gates: set[str] = set()
        self.cancelled_items: set[str] = set()
        self.current_max_parallel: int = 2
        self.active_handles: dict[str, Any] = {}
        self.active_routes: dict[str, int] = {}
        self.budget_killed: bool = False

    @workflow.signal
    def add_item(self, item: ItemPlan) -> None:
        """Signal to dynamically add a new item to the squad."""
        if item.id not in self.items:
            self.items[item.id] = item
            self.states[item.id] = ItemState(id=item.id, status="pending")
            workflow.logger.info(f"Signal add_item: added {item.id}")

    @workflow.signal
    def cancel_item(self, item_id: str) -> None:
        """Signal to cancel a pending or running item."""
        self.cancelled_items.add(item_id)
        if item_id in self.active_handles:
            handle = self.active_handles[item_id]
            handle.cancel()
            workflow.logger.info(f"Signal cancel_item: cancelling active child {item_id}")
        elif item_id in self.states and self.states[item_id].status in ("pending", "waiting-gate"):
            self.states[item_id].status = "skipped"
            self.states[item_id].note = "cancelled by signal"

    @workflow.signal
    def set_parallelism(self, max_parallel: int) -> None:
        """Signal to dynamically adjust parallelism limit."""
        workflow.logger.info(
            f"Signal set_parallelism: {self.current_max_parallel} -> {max_parallel}"
        )
        self.current_max_parallel = max(1, max_parallel)

    @workflow.signal
    def release_gate(self, gate_name: str) -> None:
        """Signal to release a named gate dependency."""
        workflow.logger.info(f"Signal release_gate: {gate_name}")
        self.released_gates.add(gate_name)

    @workflow.query
    def board(self) -> str:
        """Query returning status markdown table matching ds_squad format."""
        rows = ["| item | state | min | $ | note |", "|---|---|---:|---:|---|"]
        for it_id, s in self.states.items():
            mins = s.minutes
            cost = s.cost
            rows.append(f"| {it_id} | {s.status} | {mins:.0f} | {cost:.2f} | {s.note[:70]} |")

        now_str = workflow.now().strftime("%Y-%m-%d %H:%M:%S UTC")
        header = (
            f"# Squad Status ({now_str})\n\n"
            f"model `{self.plan.model}`; spent ${self.cumulative_spend:.2f} of "
            f"${self.plan.budget_usd:.2f} cap; running: {len(self.active_handles)}\n\n"
        )
        return header + "\n".join(rows) + "\n"

    @workflow.query
    def status(self) -> dict[str, Any]:
        """Query returning structured dictionary status."""
        return {
            "model": self.plan.model,
            "budget_usd": self.plan.budget_usd,
            "cumulative_spend": self.cumulative_spend,
            "max_parallel": self.current_max_parallel,
            "running": list(self.active_handles.keys()),
            "states": {k: vars(v) for k, v in self.states.items()},
            "completed": list(self.completed_item_ids),
        }

    def _can_schedule_any(self, terminal_states: set[str]) -> bool:
        if self.budget_killed:
            return any(
                s.status in ("pending", "waiting-gate") for s in self.states.values()
            )

        if any(
            it_id in self.cancelled_items
            and self.states[it_id].status in ("pending", "waiting-gate")
            for it_id in self.items
        ):
            return True

        for it_id, item in self.items.items():
            state = self.states.get(it_id)
            if not state or state.status not in ("pending", "waiting-gate"):
                continue

            dead_deps = [
                d
                for d in item.needs
                if d in self.states
                and self.states[d].status in terminal_states
                and self.states[d].status != "done"
            ]
            if dead_deps:
                return True

            if any(
                self.states.get(d) is None or self.states[d].status != "done"
                for d in item.needs
            ):
                continue

            if item.gate and item.gate not in self.released_gates:
                continue

            route = item.route or self.plan.model
            route_limit = self.plan.route_parallel_limits.get(
                route, self.current_max_parallel
            )
            current_route_count = self.active_routes.get(route, 0)
            if (
                len(self.active_handles) < self.current_max_parallel
                and current_route_count < route_limit
            ):
                return True

        return False

    @workflow.run
    async def run(self, inp: SquadWorkflowInput | SquadPlan) -> dict[str, Any]:
        if isinstance(inp, SquadPlan):
            self.plan = inp
            self.current_max_parallel = inp.max_parallel
            for it in inp.items:
                self.items[it.id] = it
                self.states[it.id] = ItemState(id=it.id, status="pending")
        else:
            self.plan = inp.plan
            self.current_max_parallel = inp.plan.max_parallel
            self.cumulative_spend = inp.cumulative_spend
            self.completed_item_ids = list(inp.completed_item_ids)
            self.states = dict(inp.states)
            for it in inp.plan.items:
                self.items[it.id] = it
                if it.id not in self.states:
                    self.states[it.id] = ItemState(id=it.id, status="pending")

        TERMINAL_STATES = {"done", "blocked", "failed", "killed-time", "killed-budget", "skipped"}

        # Running tasks dict: item_id -> asyncio.Task[ItemResult]
        running_tasks: dict[str, asyncio.Task[ItemResult]] = {}
        items_completed_this_run = 0
        while True:
            # Check Continue-As-New condition
            if (
                items_completed_this_run >= self.plan.continue_as_new_threshold
                and any(s.status not in TERMINAL_STATES for s in self.states.values())
                and not running_tasks
            ):
                workflow.logger.info("Triggering continue_as_new due to item threshold")
                workflow.continue_as_new(
                    SquadWorkflowInput(
                        plan=self.plan,
                        states=self.states,
                        cumulative_spend=self.cumulative_spend,
                        completed_item_ids=self.completed_item_ids,
                    )
                )

            # Check overall budget limit
            if self.cumulative_spend >= self.plan.budget_usd and not self.budget_killed:
                self.budget_killed = True
                workflow.logger.warning(
                    f"Cumulative spend ${self.cumulative_spend:.2f} "
                    f"exceeded budget ${self.plan.budget_usd:.2f}"
                )
                for state in self.states.values():
                    if state.status in ("pending", "waiting-gate"):
                        state.status = "killed-budget"
                        state.note = f"squad budget cap ${self.plan.budget_usd:.2f} reached"

            # Check if any cancelled items need stopping
            for cancel_id in list(self.cancelled_items):
                if cancel_id in running_tasks:
                    running_tasks[cancel_id].cancel()

            # Schedule eligible items
            for it_id, item in list(self.items.items()):
                state = self.states[it_id]
                if state.status not in ("pending", "waiting-gate"):
                    continue

                if it_id in self.cancelled_items:
                    state.status = "skipped"
                    state.note = "cancelled"
                    continue

                if self.budget_killed:
                    state.status = "killed-budget"
                    state.note = "squad budget reached"
                    continue

                # Check dependencies (needs)
                dead_deps = [
                    d
                    for d in item.needs
                    if d in self.states
                    and self.states[d].status in TERMINAL_STATES
                    and self.states[d].status != "done"
                ]
                if dead_deps:
                    state.status = "skipped"
                    state.note = f"needs {dead_deps} not done"
                    continue

                if any(
                    self.states.get(d) is None or self.states[d].status != "done"
                    for d in item.needs
                ):
                    continue

                # Check gate
                if item.gate and item.gate not in self.released_gates:
                    state.status = "waiting-gate"
                    state.note = f"waiting for gate {item.gate}"
                    continue

                # Check concurrency limits (global & per-route)
                route = item.route or self.plan.model
                route_limit = self.plan.route_parallel_limits.get(route, self.current_max_parallel)
                current_route_count = self.active_routes.get(route, 0)

                if (
                    len(running_tasks) >= self.current_max_parallel
                    or current_route_count >= route_limit
                ):
                    continue

                # Launch child workflow
                state.status = "running"
                state.t0 = workflow.now().timestamp()
                self.active_routes[route] = current_route_count + 1

                async def _run_child(
                    target_item: ItemPlan, target_route: str
                ) -> ItemResult:
                    child_input = ItemWorkflowInput(item=target_item, plan=self.plan)
                    child_handle = await workflow.start_child_workflow(
                        ItemWorkflow.run,
                        args=[child_input],
                        id=f"{workflow.info().workflow_id}-item-{target_item.id}",
                    )
                    self.active_handles[target_item.id] = child_handle
                    try:
                        return await child_handle
                    finally:
                        self.active_handles.pop(target_item.id, None)

                task = asyncio.create_task(_run_child(item, route))
                running_tasks[it_id] = task

            # Check if all items are terminal and no running tasks
            all_terminal = all(s.status in TERMINAL_STATES for s in self.states.values())
            if all_terminal and not running_tasks:
                break

            if not running_tasks:
                if not self._can_schedule_any(TERMINAL_STATES):
                    waiting = [
                        it_id
                        for it_id, s in self.states.items()
                        if s.status in ("pending", "waiting-gate")
                    ]
                    if not waiting:
                        break

                    # Wait until a signal enables scheduling
                    await workflow.wait_condition(
                        lambda: self._can_schedule_any(TERMINAL_STATES)
                    )
                continue

            # Wait for any running child task to complete
            done_tasks, _ = await workflow.wait(
                list(running_tasks.values()), return_when=asyncio.FIRST_COMPLETED
            )

            for it_id, task in list(running_tasks.items()):
                if task in done_tasks:
                    running_tasks.pop(it_id)
                    item = self.items[it_id]
                    route = item.route or self.plan.model
                    self.active_routes[route] = max(0, self.active_routes.get(route, 1) - 1)

                    if it_id in self.cancelled_items or task.cancelled():
                        self.states[it_id].status = "skipped"
                        self.states[it_id].note = "cancelled"
                    elif task.exception():
                        exc = task.exception()
                        self.states[it_id].status = "failed"
                        self.states[it_id].note = f"Exception: {exc}"
                    else:
                        result: ItemResult = task.result()
                        self.states[it_id].status = result.status
                        self.states[it_id].minutes = result.minutes
                        self.states[it_id].cost = result.cost
                        self.states[it_id].note = result.note
                        self.cumulative_spend += result.cost
                        self.completed_item_ids.append(it_id)
                        items_completed_this_run += 1

        return self.status()
