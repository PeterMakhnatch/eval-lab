from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    # Any external paths
    pass


@activity.defn
async def temporal_step_act(args_dict: dict[str, str]) -> str:
    step_id = args_dict["step_id"]
    sec = float(args_dict["sec"])
    trial_dir = Path(args_dict["trial_dir"])
    ledger_path = Path(args_dict["ledger_path"])
    agent_path = Path(args_dict["agent_path"])

    trial_dir.mkdir(parents=True, exist_ok=True)
    report = trial_dir / f"{step_id}.md"
    p = await asyncio.create_subprocess_exec(
        sys.executable, str(agent_path),
        "--id", step_id,
        "--ledger", str(ledger_path),
        "--report", str(report),
        "--seconds", str(sec),
    )
    rc = await p.wait()
    if rc != 0:
        raise RuntimeError(f"{step_id} failed rc {rc}")
    return f"{step_id} ok"


@workflow.defn
class OriginalWorkflow:
    @workflow.run
    async def run(self, base_args: dict[str, str]) -> list[str]:
        a1 = {**base_args, "step_id": "STEP1", "sec": "2.0"}
        a2 = {**base_args, "step_id": "STEP2", "sec": "1.0"}
        r1 = await workflow.execute_activity(temporal_step_act, args=[a1], start_to_close_timeout=timedelta(seconds=10))
        r2 = await workflow.execute_activity(temporal_step_act, args=[a2], start_to_close_timeout=timedelta(seconds=10))
        return [r1, r2]


@workflow.defn
class ModifiedWorkflow:
    @workflow.run
    async def run(self, base_args: dict[str, str]) -> list[str]:
        a1 = {**base_args, "step_id": "STEP1", "sec": "2.0"}
        a_add = {**base_args, "step_id": "STEP_ADDED", "sec": "1.0"}
        a2 = {**base_args, "step_id": "STEP2", "sec": "1.0"}
        r1 = await workflow.execute_activity(temporal_step_act, args=[a1], start_to_close_timeout=timedelta(seconds=10))
        r_add = await workflow.execute_activity(temporal_step_act, args=[a_add], start_to_close_timeout=timedelta(seconds=10))
        r2 = await workflow.execute_activity(temporal_step_act, args=[a2], start_to_close_timeout=timedelta(seconds=10))
        return [r1, r_add, r2]
