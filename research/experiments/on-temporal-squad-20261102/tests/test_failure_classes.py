from __future__ import annotations

import json
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


@pytest.mark.asyncio
async def test_route_exhausted_parks_item(tmp_path: Path) -> None:
    """Test RouteExhausted: non-retryable failure parks item as blocked."""
    quota_file = tmp_path / "quota.json"
    quota_file.write_text(json.dumps({"routes": {"exhausted-route": {"remaining": 0.0}}}))

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    item = ItemPlan(
        id="item-exhausted",
        title="Exhausted quota item",
        route="exhausted-route",
        raw_dir=str(run_dir / "raw" / "item-exhausted"),
        session_dir=str(run_dir / "sessions" / "item-exhausted"),
    )

    plan = SquadPlan(
        model="exhausted-route",
        budget_usd=10.0,
        max_parallel=1,
        quota_file=str(quota_file),
        run_dir=str(run_dir),
        items=[item],
    )

    task_queue = "quota-queue"
    async with await WorkflowEnvironment.start_time_skipping() as wf_env:
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
                id=f"test-exhausted-{int(time.time())}",
                task_queue=task_queue,
            )
            result = await handle.result()
            states = result.get("states", {})
            assert states["item-exhausted"]["status"] == "blocked"
            assert "RouteExhausted" in states["item-exhausted"]["note"]


@pytest.mark.asyncio
async def test_transient_launch_failure_retries_with_backoff(tmp_path: Path) -> None:
    """Test transient launch failure: retried via Activity retry policy and succeeds."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    fake_agent_path = Path(__file__).parent / "fake_agent.py"
    item_dir = run_dir / "raw" / "item-transient"

    item = ItemPlan(
        id="item-transient",
        title="Transient failure item",
        cmd_override=[
            sys.executable,
            str(fake_agent_path),
            "--item-id",
            "item-transient",
            "--raw-dir",
            str(item_dir),
            "--sleep",
            "0.1",
        ],
        env_override={"SIMULATE_TRANSIENT_LAUNCH_ERROR": "1"},
        raw_dir=str(item_dir),
        session_dir=str(run_dir / "sessions" / "item-transient"),
    )

    plan = SquadPlan(
        model="transient-route",
        budget_usd=10.0,
        max_parallel=1,
        run_dir=str(run_dir),
        items=[item],
    )

    task_queue = "transient-queue"
    async with await WorkflowEnvironment.start_time_skipping() as wf_env:
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
                id=f"test-transient-{int(time.time())}",
                task_queue=task_queue,
            )
            result = await handle.result()
            states = result.get("states", {})
            assert states["item-transient"]["status"] == "done"


@pytest.mark.asyncio
async def test_report_missing_retries_continuation(tmp_path: Path) -> None:
    """Test ReportMissing: retry once with continuation mode when first attempt report is missing."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    item_dir = run_dir / "raw" / "item-missing"

    # A custom agent script that fails to write report on first invocation, but writes on second
    attempt_tracker = tmp_path / ".attempt_counter"
    agent_script = tmp_path / "flaky_report_agent.py"
    agent_script.write_text(
        f"""
import sys
from pathlib import Path

tracker = Path({str(attempt_tracker)!r})
count = int(tracker.read_text()) if tracker.exists() else 0
tracker.write_text(str(count + 1))

raw = Path({str(item_dir)!r})
raw.mkdir(parents=True, exist_ok=True)
if count > 0:
    (raw / "report.md").write_text("# Report\\nDS-DONE: item-missing\\n")
"""
    )

    item = ItemPlan(
        id="item-missing",
        title="Missing report item",
        retry=1,  # Allows 1 retry
        cmd_override=[sys.executable, str(agent_script)],
        raw_dir=str(item_dir),
        session_dir=str(run_dir / "sessions" / "item-missing"),
    )

    plan = SquadPlan(
        model="missing-route",
        budget_usd=10.0,
        max_parallel=1,
        run_dir=str(run_dir),
        items=[item],
    )

    task_queue = "missing-queue"
    async with await WorkflowEnvironment.start_time_skipping() as wf_env:
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
                id=f"test-missing-{int(time.time())}",
                task_queue=task_queue,
            )
            result = await handle.result()
            states = result.get("states", {})
            assert states["item-missing"]["status"] == "done"
            assert int(attempt_tracker.read_text()) == 2
