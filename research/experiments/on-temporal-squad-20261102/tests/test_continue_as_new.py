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
async def test_continue_as_new_preserves_state(tmp_path: Path) -> None:
    """Test (e): continue-as-new preserves cumulative state across execution generations."""
    ledger_file = tmp_path / "ledger.jsonl"
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    fake_agent_path = Path(__file__).parent / "fake_agent.py"

    items = []
    # 4 items; threshold set to 2 so continue-as-new triggers after 2 items complete
    for i in range(1, 5):
        iid = f"item-{i}"
        item_dir = run_dir / "raw" / iid
        items.append(
            ItemPlan(
                id=iid,
                title=f"Item {i}",
                cmd_override=[
                    sys.executable,
                    str(fake_agent_path),
                    "--item-id",
                    iid,
                    "--raw-dir",
                    str(item_dir),
                    "--sleep",
                    "0.1",
                    "--cost",
                    "0.25",
                    "--ledger",
                    str(ledger_file),
                ],
                raw_dir=str(item_dir),
                session_dir=str(run_dir / "sessions" / iid),
                env_override={"MOCK_COST": "0.25"},
            )
        )

    plan = SquadPlan(
        model="can-route",
        budget_usd=10.0,
        max_parallel=1,  # Serial so items 1 and 2 finish before continue-as-new
        continue_as_new_threshold=2,  # Trigger continue-as-new after 2 items
        run_dir=str(run_dir),
        items=items,
    )

    task_queue = "can-queue"

    async with await WorkflowEnvironment.start_local(ui=False) as wf_env:
        client = wf_env.client

        worker = Worker(
            client,
            task_queue=task_queue,
            workflows=[SquadWorkflow, ItemWorkflow],
            activities=[launch_worker, read_report_state, route_quota_ok, settle_spend],
        )

        async with worker:
            wf_id = f"test-can-{int(time.time())}"
            handle = await client.start_workflow(
                SquadWorkflow.run,
                plan,
                id=wf_id,
                task_queue=task_queue,
            )

            result = await handle.result()
            states = result.get("states", {})
            for i in range(1, 5):
                assert states[f"item-{i}"]["status"] == "done"

            # Check that all 4 completed and spend was aggregated across continue-as-new
            assert len(result.get("completed", [])) == 4
            assert abs(result.get("cumulative_spend", 0.0) - 1.00) < 1e-3

            # Check board query
            board_str = await handle.query(SquadWorkflow.board)
            assert "spent $1.00 of $10.00 cap" in board_str
            for i in range(1, 5):
                assert f"| item-{i} | done |" in board_str

            # Check ledger
            raw_lines = [ln for ln in ledger_file.read_text().splitlines() if ln.strip()]
            records = [json.loads(ln) for ln in raw_lines]
            for i in range(1, 5):
                starts = [
                    r for r in records if r["item_id"] == f"item-{i}" and r.get("event") == "start"
                ]
                assert len(starts) == 1, f"item-{i} started {len(starts)} times instead of 1"
