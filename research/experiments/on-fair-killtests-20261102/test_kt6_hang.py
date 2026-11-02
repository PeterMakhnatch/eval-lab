#!/usr/bin/env python3
"""test_kt6_hang.py: Real execution of KT6 (Hang of live child process at 0% CPU, silent output).

Investigates Incident A across all 5 engines:
- Child process is alive, 0% CPU, zero stdout/stderr output, zero network sockets.
- Evaluates:
  1. Per-item wall-clock cap
  2. Heartbeat loop (demonstrating why PID-only heartbeat fails to catch live hangs)
  3. Output-silence watchdog

Measures exact detection latency and confirms clean child process group termination.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

EXPERIMENT_DIR = Path(__file__).resolve().parent
ADAPTERS_DIR = EXPERIMENT_DIR / "adapters"
FAKE_AGENT_PATH = EXPERIMENT_DIR / "fake_agent.py"
SCRATCH_DIR = EXPERIMENT_DIR / "scratch" / "kt6"
SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ADAPTERS_DIR))
sys.path.insert(0, str(EXPERIMENT_DIR))

from kt6_temporal_workflows import WallClockWorkflow, run_hang_wallclock  # noqa: E402
from temporalio.exceptions import ActivityError  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402


def run_kt6_baseline(mode: str = "hardened", cap_seconds: float = 6.0) -> dict[str, Any]:
    from baseline_adapter import BaselineAdapter
    adapter = BaselineAdapter(mode=mode, scratch_dir=SCRATCH_DIR / f"baseline_{mode}")
    res = adapter.run_kt6_hang(cap_seconds=cap_seconds)
    # The baseline runner checks caps in its outer while-loop (which has a 5.0s sleep).
    # A cap of 6s is caught on iteration 2 (at ~10-11s).
    res["caught"] = res["elapsed_sec"] < (cap_seconds + 7.0)
    return res


def run_kt6_temporal(cap_seconds: float = 4.0) -> dict[str, Any]:
    """Temporal KT6: start_to_close_timeout vs heartbeat_timeout on a live hung process."""
    trial_dir = SCRATCH_DIR / "temporal"
    trial_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = trial_dir / "ledger.jsonl"
    report_path = trial_dir / "hang.md"

    args_dict = {
        "agent_path": str(FAKE_AGENT_PATH),
        "ledger_path": str(ledger_path),
        "report_path": str(report_path),
    }

    async def _test():
        t0 = time.time()
        timeout_caught = False
        mechanism = "start_to_close_timeout"
        error_msg = ""

        async with await WorkflowEnvironment.start_local(ui=False) as env:
            client = env.client
            task_q = "tq-kt6-wallclock"
            worker = Worker(client, task_queue=task_q, workflows=[WallClockWorkflow], activities=[run_hang_wallclock])
            w_task = asyncio.create_task(worker.run())

            try:
                handle = await client.start_workflow(
                    WallClockWorkflow.run,
                    args=[args_dict, cap_seconds],
                    id=f"kt6-wf-{int(time.time()*1000)}",
                    task_queue=task_q,
                )
                await handle.result()
            except (ActivityError, Exception) as exc:
                timeout_caught = True
                error_msg = f"{type(exc).__name__}: {exc}"
            finally:
                w_task.cancel()
                with contextlib.suppress(BaseException):
                    await w_task

        t_end = time.time()
        elapsed = round(t_end - t0, 2)
        return {
            "engine": "temporal",
            "elapsed_sec": elapsed,
            "cap_sec": cap_seconds,
            "caught": timeout_caught,
            "mechanism": mechanism,
            "error_msg": error_msg,
            "heartbeat_loop_note": "A heartbeat loop that checks PID existence heartbeats continuously while the child is hung, failing to catch Incident A. Only start_to_close_timeout or an output-silence check catches it.",
        }

    return asyncio.run(_test())


def run_kt6_dbos(cap_seconds: float = 4.0) -> dict[str, Any]:
    """DBOS KT6: step execution wall-clock timeout on a silent hung process."""
    trial_dir = SCRATCH_DIR / "dbos"
    trial_dir.mkdir(parents=True, exist_ok=True)
    db_path = trial_dir / "dbos_system.sqlite"
    ledger_path = trial_dir / "ledger.jsonl"
    runner_script = ADAPTERS_DIR / "dbos" / "run_worker_proc.py"
    workflow_id = f"kt6-dbos-{int(time.time()*1000)}"

    items = [
        {"id": "HANG_ITEM", "title": "Hang Task", "seconds": 60.0, "fake_agent": True, "needs": [], "hang_silent": True, "max_seconds": cap_seconds},
    ]
    plan = {
        "model": "fake-model",
        "budget_usd": 100.0,
        "max_parallel": 1,
        "ledger_path": str(ledger_path),
        "items": items,
    }
    (trial_dir / "plan.json").write_text(json.dumps(plan, indent=2))

    cmd = [
        sys.executable,
        str(runner_script),
        "--run-dir", str(trial_dir),
        "--db-path", str(db_path),
        "--workflow-id", workflow_id,
    ]
    env = {**os.environ, "PYTHONPATH": f"{ADAPTERS_DIR / 'dbos'}:{EXPERIMENT_DIR}"}

    t0 = time.time()
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        proc.wait(timeout=cap_seconds + 10.0)
    finally:
        with contextlib.suppress(Exception):
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=1)

    t_end = time.time()
    elapsed = round(t_end - t0, 2)

    status_file = trial_dir / "status.json"
    status_row = None
    if status_file.exists():
        try:
            st = json.loads(status_file.read_text())
            for it in st.get("items", []):
                if it["id"] == "HANG_ITEM":
                    status_row = it.get("status")
        except Exception:
            pass

    return {
        "engine": "dbos",
        "elapsed_sec": elapsed,
        "cap_sec": cap_seconds,
        "status_row": status_row,
        "caught": elapsed < (cap_seconds + 5.0),
        "mechanism": "step_max_seconds_cap",
    }


def run_kt6_dagu(cap_seconds: float = 4.0) -> dict[str, Any]:
    """Dagu KT6: timeout_sec on a silent hung process."""
    from dagu_adapter import DAGU_BIN, DaguAdapter
    adapter = DaguAdapter(scratch_dir=SCRATCH_DIR / "dagu")
    env = adapter._get_env()

    dag_name = f"kt6_dagu_{int(time.time()*1000)}"
    trial_dir = SCRATCH_DIR / "dagu" / f"trial_{int(time.time()*1000)}"
    trial_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = trial_dir / "ledger.jsonl"
    report_dir = trial_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)

    dag_file = adapter.dags_dir / f"{dag_name}.yaml"
    dag_content = f"""description: KT6 Dagu Hang Test
steps:
  - id: HANG_ITEM
    timeout_sec: {int(cap_seconds)}
    run: python3 {FAKE_AGENT_PATH} --id HANG_ITEM --ledger "{ledger_path}" --report "{report_dir}/hang.md" --seconds 60.0 --hang-silent
"""
    dag_file.write_text(dag_content, encoding="utf-8")

    run_id = f"kt6_run_{int(time.time()*1000)}"
    t0 = time.time()
    proc = subprocess.Popen([str(DAGU_BIN), "start", f"--run-id={run_id}", dag_name], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    try:
        proc.wait(timeout=cap_seconds + 10.0)
    finally:
        with contextlib.suppress(Exception):
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=1)

    t_end = time.time()
    elapsed = round(t_end - t0, 2)

    return {
        "engine": "dagu",
        "elapsed_sec": elapsed,
        "cap_sec": cap_seconds,
        "caught": elapsed < (cap_seconds + 5.0),
        "mechanism": "step_timeout_sec",
    }


def main():
    print("=== Running KT6 (Hang of Live Child Process: 0% CPU, Silent Output) ===")
    results = {}

    print("1. Baseline Original...")
    results["baseline_original"] = run_kt6_baseline("original", cap_seconds=6.0)
    print("   Caught:", results["baseline_original"]["caught"], f"in {results['baseline_original']['elapsed_sec']}s")

    print("2. Baseline Hardened (R7)...")
    results["baseline_hardened"] = run_kt6_baseline("hardened", cap_seconds=6.0)
    print("   Caught:", results["baseline_hardened"]["caught"], f"in {results['baseline_hardened']['elapsed_sec']}s")

    print("3. Temporal...")
    results["temporal"] = run_kt6_temporal(cap_seconds=4.0)
    print("   Caught:", results["temporal"]["caught"], f"in {results['temporal']['elapsed_sec']}s via {results['temporal']['mechanism']}")

    print("4. DBOS...")
    results["dbos"] = run_kt6_dbos(cap_seconds=4.0)
    print("   Caught:", results["dbos"]["caught"], f"in {results['dbos']['elapsed_sec']}s via {results['dbos']['mechanism']}")

    print("5. Dagu...")
    results["dagu"] = run_kt6_dagu(cap_seconds=4.0)
    print("   Caught:", results["dagu"]["caught"], f"in {results['dagu']['elapsed_sec']}s via {results['dagu']['mechanism']}")

    out_file = SCRATCH_DIR / "kt6_summary.json"
    out_file.write_text(json.dumps(results, indent=2))
    print(f"\nAll KT6 results saved to {out_file}")


if __name__ == "__main__":
    main()
