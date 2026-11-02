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
async def test_dependency_order_and_gate_signal(tmp_path: Path) -> None:
    """Test (c): verify dependency DAG ordering and gate unblocking via workflow signal."""
    ledger_file = tmp_path / "ledger.jsonl"
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    fake_agent_path = Path(__file__).parent / "fake_agent.py"

    item_a_dir = run_dir / "raw" / "item-A"
    item_b_dir = run_dir / "raw" / "item-B"
    item_c_dir = run_dir / "raw" / "item-C"

    # Item A: 0.3s
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
            "0.3",
            "--ledger",
            str(ledger_file),
        ],
        raw_dir=str(item_a_dir),
        session_dir=str(run_dir / "sessions" / "item-A"),
    )
    # Item B: needs A, 0.2s
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
            "0.2",
            "--ledger",
            str(ledger_file),
        ],
        raw_dir=str(item_b_dir),
        session_dir=str(run_dir / "sessions" / "item-B"),
    )
    # Item C: gated by gate-approval-C
    item_c = ItemPlan(
        id="item-C",
        title="Item C",
        gate="approvals/c-gate.txt",
        cmd_override=[
            sys.executable,
            str(fake_agent_path),
            "--item-id",
            "item-C",
            "--raw-dir",
            str(item_c_dir),
            "--sleep",
            "0.2",
            "--ledger",
            str(ledger_file),
        ],
        raw_dir=str(item_c_dir),
        session_dir=str(run_dir / "sessions" / "item-C"),
    )

    plan = SquadPlan(
        model="gate-route",
        budget_usd=10.0,
        max_parallel=3,
        run_dir=str(run_dir),
        items=[item_a, item_b, item_c],
    )

    task_queue = "gate-queue"

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
                id=f"test-dag-gates-{int(time.time())}",
                task_queue=task_queue,
            )

            # Wait until item-A and item-B complete
            t0 = time.time()
            while time.time() - t0 < 15.0:
                st = await handle.query(SquadWorkflow.status)
                states = st.get("states", {})
                if (
                    states.get("item-A", {}).get("status") == "done"
                    and states.get("item-B", {}).get("status") == "done"
                ):
                    break
                await pytest.importorskip("asyncio").sleep(0.2)

            # Check status of item-C before gate release
            st = await handle.query(SquadWorkflow.status)
            assert st["states"]["item-C"]["status"] == "waiting-gate"

            # Check board() query formatting
            board_text = await handle.query(SquadWorkflow.board)
            assert "| item-C | waiting-gate |" in board_text

            # Now release gate via signal
            t_gate = time.time()
            await handle.signal(SquadWorkflow.release_gate, "approvals/c-gate.txt")

            # Await workflow completion
            result = await handle.result()
            states = result.get("states", {})
            assert states["item-A"]["status"] == "done"
            assert states["item-B"]["status"] == "done"
            assert states["item-C"]["status"] == "done"

            # Read ledger to verify strict timestamp order:
            # 1. Item B started after Item A finished
            # 2. Item C started after gate release signal
            raw_lines = [ln for ln in ledger_file.read_text().splitlines() if ln.strip()]
            records = [json.loads(ln) for ln in raw_lines]
            fin_a = next(
                r for r in records if r["item_id"] == "item-A" and r.get("event") == "finish"
            )
            start_b = next(
                r for r in records if r["item_id"] == "item-B" and r.get("event") == "start"
            )
            start_c = next(
                r for r in records if r["item_id"] == "item-C" and r.get("event") == "start"
            )

            assert start_b["t0"] >= fin_a["t1"], (
                f"Item B started at {start_b['t0']} before A finished at {fin_a['t1']}"
            )
            assert start_c["t0"] >= t_gate - 0.1, (
                f"Item C started at {start_c['t0']} before gate signal at {t_gate}"
            )
