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
async def test_per_route_parallelism_cap(tmp_path: Path) -> None:
    """Test (d): verify per-route parallelism cap is respected using fake-agent timestamps."""
    ledger_file = tmp_path / "ledger.jsonl"
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    fake_agent_path = Path(__file__).parent / "fake_agent.py"

    items = []
    # 4 parallel items on the same route
    for i in range(1, 5):
        iid = f"item-{i}"
        item_dir = run_dir / "raw" / iid
        items.append(
            ItemPlan(
                id=iid,
                title=f"Parallel item {i}",
                route="parallel-route",
                cmd_override=[
                    sys.executable,
                    str(fake_agent_path),
                    "--item-id",
                    iid,
                    "--raw-dir",
                    str(item_dir),
                    "--sleep",
                    "0.4",
                    "--ledger",
                    str(ledger_file),
                ],
                raw_dir=str(item_dir),
                session_dir=str(run_dir / "sessions" / iid),
            )
        )

    plan = SquadPlan(
        model="parallel-route",
        budget_usd=10.0,
        max_parallel=2,  # Maximum 2 concurrent items
        route_parallel_limits={"parallel-route": 2},
        run_dir=str(run_dir),
        items=items,
    )

    task_queue = "parallel-queue"

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
                id=f"test-parallelism-{int(time.time())}",
                task_queue=task_queue,
            )

            result = await handle.result()
            states = result.get("states", {})
            for i in range(1, 5):
                assert states[f"item-{i}"]["status"] == "done"

            # Parse ledger intervals
            raw_lines = [ln for ln in ledger_file.read_text().splitlines() if ln.strip()]
            records = [json.loads(ln) for ln in raw_lines]

            intervals = []
            for i in range(1, 5):
                iid = f"item-{i}"
                start_rec = next(
                    r for r in records if r["item_id"] == iid and r.get("event") == "start"
                )
                fin_rec = next(
                    r for r in records if r["item_id"] == iid and r.get("event") == "finish"
                )
                intervals.append((start_rec["t0"], fin_rec["t1"]))

            # Sample time points to check overlap
            all_timestamps = [t for start, end in intervals for t in (start, end)]
            max_concurrency = 0
            for t in all_timestamps:
                # Add tiny epsilon to check mid-interval concurrency
                test_t = t + 0.001
                concurrent = sum(1 for start, end in intervals if start <= test_t <= end)
                if concurrent > max_concurrency:
                    max_concurrency = concurrent

            assert (
                max_concurrency <= 2
            ), f"Max observed concurrency was {max_concurrency}, which exceeds cap 2"
