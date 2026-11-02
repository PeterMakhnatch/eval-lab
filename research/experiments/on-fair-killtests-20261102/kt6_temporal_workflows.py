from __future__ import annotations

import asyncio
import os
import signal
import sys
from datetime import timedelta
from pathlib import Path

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    pass


@activity.defn
async def run_hang_wallclock(args_dict: dict[str, str]) -> str:
    agent_path = Path(args_dict["agent_path"])
    ledger_path = Path(args_dict["ledger_path"])
    report_path = Path(args_dict["report_path"])

    p = await asyncio.create_subprocess_exec(
        sys.executable, str(agent_path),
        "--id", "HANG_WALLCLOCK",
        "--ledger", str(ledger_path),
        "--report", str(report_path),
        "--seconds", "60.0",
        "--hang-silent",
        start_new_session=True,
    )
    try:
        await p.wait()
    except (asyncio.CancelledError, BaseException):
        if p.pid > 0:
            import contextlib
            with contextlib.suppress(Exception):
                os.killpg(p.pid, signal.SIGKILL)
@workflow.defn
class WallClockWorkflow:
    @workflow.run
    async def run(self, args_dict: dict[str, str], cap_sec: float) -> str:
        from temporalio.common import RetryPolicy
        return await workflow.execute_activity(
            run_hang_wallclock,
            args=[args_dict],
            start_to_close_timeout=timedelta(seconds=cap_sec),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
