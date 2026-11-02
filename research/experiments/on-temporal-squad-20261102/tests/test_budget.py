from __future__ import annotations

import json
import sys
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
async def test_squad_budget_kill(tmp_path: Path) -> None:
    """Test (b): cumulative budget kill stops pending items and marks them killed-budget."""
    ledger_file = tmp_path / "ledger.jsonl"
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    fake_agent_path = Path(__file__).parent / "fake_agent.py"

    item_a_dir = run_dir / "raw" / "item-A"
    item_b_dir = run_dir / "raw" / "item-B"
    item_c_dir = run_dir / "raw" / "item-C"

    # Item A spends $1.50; Item B depends on A and spends $1.00 -> total $2.50 exceeds $2.00 cap
    # Item C depends on B and should be killed-budget without starting
    item_a = ItemPlan(
        id="item-A",
        title="Item A",
        cmd_override=[
            sys.executable,
            str(fake_agent_path),
            "--item-id",
            "item-A",
            "--raw-dir",
            str(item_a_dir),
            "--sleep",
            "0.1",
            "--cost",
            "1.50",
            "--ledger",
            str(ledger_file),
        ],
        raw_dir=str(item_a_dir),
        session_dir=str(run_dir / "sessions" / "item-A"),
        env_override={"MOCK_COST": "1.50"},
    )
    item_b = ItemPlan(
        id="item-B",
        title="Item B",
        needs=["item-A"],
        cmd_override=[
            sys.executable,
            str(fake_agent_path),
            "--item-id",
            "item-B",
            "--raw-dir",
            str(item_b_dir),
            "--sleep",
            "0.1",
            "--cost",
            "1.00",
            "--ledger",
            str(ledger_file),
        ],
        raw_dir=str(item_b_dir),
        session_dir=str(run_dir / "sessions" / "item-B"),
        env_override={"MOCK_COST": "1.00"},
    )
    item_c = ItemPlan(
        id="item-C",
        title="Item C",
        needs=["item-B"],
        cmd_override=[
            sys.executable,
            str(fake_agent_path),
            "--item-id",
            "item-C",
            "--raw-dir",
            str(item_c_dir),
            "--sleep",
            "0.1",
            "--cost",
            "0.50",
            "--ledger",
            str(ledger_file),
        ],
        raw_dir=str(item_c_dir),
        session_dir=str(run_dir / "sessions" / "item-C"),
        env_override={"MOCK_COST": "0.50"},
    )

    plan = SquadPlan(
        model="budget-test-route",
        budget_usd=2.00,  # Cap is $2.00
        max_parallel=2,
        run_dir=str(run_dir),
        items=[item_a, item_b, item_c],
    )

    task_queue = "budget-queue"

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
                id="test-budget-kill-wf",
                task_queue=task_queue,
            )

            result = await handle.result()

            states = result.get("states", {})
            assert states["item-A"]["status"] == "done"
            assert states["item-B"]["status"] == "done"
            assert states["item-C"]["status"] == "killed-budget"
            assert "budget" in states["item-C"]["note"].lower()

            # Verify ledger: item-C never ran
            if ledger_file.exists():
                raw_lines = [ln for ln in ledger_file.read_text().splitlines() if ln.strip()]
                lines = [json.loads(ln) for ln in raw_lines]
                ran_c = [r for r in lines if r.get("item_id") == "item-C"]
                assert len(ran_c) == 0, f"item-C should never have run, but found records: {ran_c}"
