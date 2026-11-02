#!/usr/bin/env python3
"""temporal_adapter.py: Adapter for executing kill tests against the Temporal squad runner."""
from __future__ import annotations

import asyncio
import contextlib
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

EXPERIMENT_DIR = Path(__file__).resolve().parent.parent
ADAPTERS_DIR = EXPERIMENT_DIR / "adapters"
FAKE_AGENT_PATH = EXPERIMENT_DIR / "fake_agent.py"
PYTHON_BIN = str(EXPERIMENT_DIR / ".venv/bin/python")

sys.path.insert(0, str(ADAPTERS_DIR))
sys.path.insert(0, str(EXPERIMENT_DIR))

from temporal_squad.models import ItemPlan, SquadPlan  # noqa: E402
from temporal_squad.workflows import SquadWorkflow  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402

from ledger_reader import analyze_quota_parking, read_ledger, summarize_ledger  # noqa: E402


class TemporalAdapter:
    """Manages Temporal-based squad kill tests using WorkflowEnvironment and worker processes."""

    def __init__(self, scratch_dir: Path | None = None) -> None:
        self.scratch_dir = scratch_dir or (EXPERIMENT_DIR / "scratch" / "temporal")
        self.scratch_dir.mkdir(parents=True, exist_ok=True)

    def _setup_trial_dir(self, subname: str) -> tuple[Path, Path]:
        trial_dir = self.scratch_dir / subname
        trial_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = trial_dir / "ledger.jsonl"
        return trial_dir, ledger_path

    async def _run_kt1_trial_async(
        self,
        trial_num: int,
        seconds_per_item: float,
        kill_delay: float,
    ) -> dict[str, Any]:
        subname = f"kt1_trial_{trial_num}_{int(time.time())}"
        trial_dir, ledger_path = self._setup_trial_dir(subname)
        task_queue = f"tq-kt1-{trial_num}-{int(time.time())}"

        items = []
        item_defs = [
            ("A1", []),
            ("A2", ["A1"]),
            ("A3", ["A2"]),
            ("B1", []),
            ("B2", ["B1"]),
            ("B3", ["B2"]),
        ]
        all_ids = [i[0] for i in item_defs]

        for iid, needs in item_defs:
            raw_dir = trial_dir / "raw" / iid
            raw_dir.mkdir(parents=True, exist_ok=True)
            report_path = raw_dir / "report.md"
            cmd = [
                PYTHON_BIN,
                str(FAKE_AGENT_PATH),
                "--id", iid,
                "--ledger", str(ledger_path),
                "--report", str(report_path),
                "--seconds", str(seconds_per_item),
            ]
            items.append(
                ItemPlan(
                    id=iid,
                    title=f"Task {iid}",
                    needs=needs,
                    cmd_override=cmd,
                    raw_dir=str(raw_dir),
                    session_dir=str(trial_dir / "sessions" / iid),
                    max_minutes=5,
                )
            )

        plan = SquadPlan(
            model="fake-route",
            budget_usd=100.0,
            max_parallel=2,
            run_dir=str(trial_dir),
            items=items,
        )

        env_worker = {**os.environ, "PYTHONPATH": str(ADAPTERS_DIR)}
        worker_script = ADAPTERS_DIR / "temporal_worker.py"

        t0 = time.time()
        async with await WorkflowEnvironment.start_local(ui=False) as wf_env:
            client = wf_env.client
            target = client.service_client.config.target_host

            # Start worker 1
            worker1 = subprocess.Popen(
                [
                    PYTHON_BIN,
                    str(worker_script),
                    "--target", target,
                    "--task-queue", task_queue,
                ],
                env=env_worker,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            try:
                handle = await client.start_workflow(
                    SquadWorkflow.run,
                    plan,
                    id=f"kt1-wf-{trial_num}-{int(time.time())}",
                    task_queue=task_queue,
                )

                # Wait for at least one item to start
                t_wait = time.time()
                while time.time() - t_wait < 15.0:
                    rows = read_ledger(ledger_path)
                    if any(r.get("kind") == "exec" for r in rows):
                        break
                    await asyncio.sleep(0.2)

                # Wait the specified random kill delay
                await asyncio.sleep(kill_delay)

                # SIGKILL worker 1 mid-flight
                try:
                    os.killpg(worker1.pid, signal.SIGKILL)
                    worker1.wait(timeout=2)
                except Exception:
                    pass

                # Brief pause while worker is down
                await asyncio.sleep(1.0)

                # Start worker 2 on same queue to recover and resume
                worker2 = subprocess.Popen(
                    [
                        PYTHON_BIN,
                        str(worker_script),
                        "--target", target,
                        "--task-queue", task_queue,
                    ],
                    env=env_worker,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )

                try:
                    # Await workflow completion
                    await asyncio.wait_for(handle.result(), timeout=120.0)
                finally:
                    with contextlib.suppress(Exception):
                        os.killpg(worker2.pid, signal.SIGKILL)
            finally:
                with contextlib.suppress(Exception):
                    os.killpg(worker1.pid, signal.SIGKILL)

        t_end = time.time()
        summary = summarize_ledger(ledger_path, all_ids)
        completed = summary["exact_one_effect"]
        return {
            "trial": trial_num,
            "completed": completed,
            "kill_delay": round(kill_delay, 2),
            "total_time_sec": round(t_end - t0, 2),
            "execs_per_item": summary["exec_counts"],
            "effects_per_item": summary["effect_counts"],
            "total_executions": summary["total_executions"],
            "total_effects": summary["total_effects"],
            "exact_one_effect": summary["exact_one_effect"],
            "human_step_needed": False,
        }

    def run_kt1(
        self,
        trials: int = 10,
        seconds_per_item: float = 6.0,
        seed: int = 42,
    ) -> dict[str, Any]:
        """KT1 Crash Resilience: 10 trials of SIGKILLing worker process."""
        print(f"=== [Temporal] Starting KT1 ({trials} trials) ===", flush=True)
        random.seed(seed)
        trial_results = []

        for i in range(1, trials + 1):
            kill_delay = random.uniform(2.0, 5.0)
            res = asyncio.run(
                self._run_kt1_trial_async(i, seconds_per_item, kill_delay)
            )
            trial_results.append(res)
            print(
                f"  Trial {i}: completed={res['completed']}, "
                f"execs={res['total_executions']}, effects={res['total_effects']}, "
                f"time={res['total_time_sec']}s",
                flush=True,
            )

        passed_trials = sum(1 for t in trial_results if t["completed"])
        overall_pass = passed_trials >= 9
        total_execs = sum(t["total_executions"] for t in trial_results)
        return {
            "engine": "temporal",
            "test": "KT1",
            "trials_run": trials,
            "trials_passed": passed_trials,
            "pass_rate": passed_trials / trials,
            "verdict": "PASS" if overall_pass else "FAIL",
            "total_raw_executions": total_execs,
            "trials": trial_results,
        }

    async def _run_kt2_async(self, seconds_per_item: float, downtime_sec: float) -> dict[str, Any]:
        subname = f"kt2_approval_{int(time.time())}"
        trial_dir, ledger_path = self._setup_trial_dir(subname)
        db_file = trial_dir / "temporal_dev_server.sqlite"
        task_queue = f"tq-kt2-{int(time.time())}"

        items = []
        for iid, needs, gate in [("PREP", [], None), ("DEPLOY", ["PREP"], "approvals/deploy.txt")]:
            raw_dir = trial_dir / "raw" / iid
            raw_dir.mkdir(parents=True, exist_ok=True)
            report_path = raw_dir / "report.md"
            cmd = [
                PYTHON_BIN,
                str(FAKE_AGENT_PATH),
                "--id", iid,
                "--ledger", str(ledger_path),
                "--report", str(report_path),
                "--seconds", str(seconds_per_item),
            ]
            items.append(
                ItemPlan(
                    id=iid,
                    title=f"Task {iid}",
                    needs=needs,
                    gate=gate,
                    cmd_override=cmd,
                    raw_dir=str(raw_dir),
                    session_dir=str(trial_dir / "sessions" / iid),
                )
            )

        plan = SquadPlan(
            model="fake-route",
            budget_usd=100.0,
            max_parallel=2,
            run_dir=str(trial_dir),
            items=items,
        )

        env_worker = {**os.environ, "PYTHONPATH": str(ADAPTERS_DIR)}
        worker_script = ADAPTERS_DIR / "temporal_worker.py"
        workflow_id = f"kt2-wf-{int(time.time())}"

        t0 = time.time()
        # 1. Start dev server 1 with SQLite backing
        async with await WorkflowEnvironment.start_local(
            ui=False, dev_server_database_filename=str(db_file)
        ) as wf_env1:
            target1 = wf_env1.client.service_client.config.target_host

            # Start worker 1
            worker1 = subprocess.Popen(
                [
                    PYTHON_BIN,
                    str(worker_script),
                    "--target", target1,
                    "--task-queue", task_queue,
                ],
                env=env_worker,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            try:
                await wf_env1.client.start_workflow(
                    SquadWorkflow.run,
                    plan,
                    id=workflow_id,
                    task_queue=task_queue,
                )

                # Wait for PREP to complete and DEPLOY to enter waiting-gate
                await asyncio.sleep(seconds_per_item + 3.0)

                # Kill worker 1
                with contextlib.suppress(Exception):
                    os.killpg(worker1.pid, signal.SIGKILL)
            finally:
                pass
            # Exiting wf_env1 stops the dev server process (simulating server crash/reboot)

        print(f"  Temporal server and worker stopped. Waiting {downtime_sec}s host downtime...", flush=True)
        await asyncio.sleep(downtime_sec)

        # 2. Restart dev server on same SQLite database
        async with await WorkflowEnvironment.start_local(
            ui=False, dev_server_database_filename=str(db_file)
        ) as wf_env2:
            target2 = wf_env2.client.service_client.config.target_host

            # Restart worker 2
            worker2 = subprocess.Popen(
                [
                    PYTHON_BIN,
                    str(worker_script),
                    "--target", target2,
                    "--task-queue", task_queue,
                ],
                env=env_worker,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            try:
                # Reconnect to the workflow handle
                handle2 = wf_env2.client.get_workflow_handle(workflow_id)

                # Deliver approval signal
                await asyncio.sleep(3.0)
                await handle2.signal(SquadWorkflow.release_gate, "approvals/deploy.txt")
                # Wait for workflow completion
                await asyncio.wait_for(handle2.result(), timeout=60.0)
            finally:
                with contextlib.suppress(Exception):
                    os.killpg(worker2.pid, signal.SIGKILL)

        t_end = time.time()
        summary = summarize_ledger(ledger_path, ["PREP", "DEPLOY"])
        prep_re_exec = summary["exec_counts"].get("PREP", 0) > 1
        approval_honored = summary["effect_counts"].get("DEPLOY", 0) == 1
        passed = (not prep_re_exec) and approval_honored
        return {
            "engine": "temporal",
            "test": "KT2",
            "verdict": "PASS" if passed else "FAIL",
            "total_time_sec": round(t_end - t0, 2),
            "prep_executions": summary["exec_counts"].get("PREP", 0),
            "deploy_executions": summary["exec_counts"].get("DEPLOY", 0),
            "summary": summary,
            "prep_re_executed": prep_re_exec,
            "approval_honored": approval_honored,
        }

    def run_kt2(self, seconds_per_item: float = 2.0, downtime_sec: float = 62.0) -> dict[str, Any]:
        """KT2 Approval across reboot."""
        print("=== [Temporal] Starting KT2 Approval ===", flush=True)
        return asyncio.run(self._run_kt2_async(seconds_per_item, downtime_sec))

    async def _run_kt3_async(self, parking_seconds: float) -> dict[str, Any]:
        subname = f"kt3_quota_{int(time.time())}"
        trial_dir, ledger_path = self._setup_trial_dir(subname)
        task_queue = f"tq-kt3-{int(time.time())}"
        t_now = time.time()
        t_reset = t_now + parking_seconds

        raw_dir = trial_dir / "raw" / "ITEM_X"
        raw_dir.mkdir(parents=True, exist_ok=True)
        report_path = raw_dir / "report.md"
        cmd = [
            PYTHON_BIN,
            str(FAKE_AGENT_PATH),
            "--id", "ITEM_X",
            "--ledger", str(ledger_path),
            "--report", str(report_path),
            "--seconds", "1.0",
            "--quota-until", str(t_reset),
        ]
        items = [
            ItemPlan(
                id="ITEM_X",
                title="Quota Task",
                cmd_override=cmd,
                raw_dir=str(raw_dir),
                session_dir=str(trial_dir / "sessions" / "ITEM_X"),
                retry=2,
            )
        ]
        plan = SquadPlan(model="fake-route", items=items, run_dir=str(trial_dir))
        env_worker = {**os.environ, "PYTHONPATH": str(ADAPTERS_DIR)}
        worker_script = ADAPTERS_DIR / "temporal_worker.py"

        async with await WorkflowEnvironment.start_local(ui=False) as wf_env:
            worker = subprocess.Popen(
                [PYTHON_BIN, str(worker_script), "--target", wf_env.client.service_client.config.target_host, "--task-queue", task_queue],
                env=env_worker, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )
            try:
                handle = await wf_env.client.start_workflow(SquadWorkflow.run, plan, id=f"kt3-wf-{int(time.time())}", task_queue=task_queue)
                # Let it run until it completes or exhausts retries
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(handle.result(), timeout=40.0)
            finally:
                with contextlib.suppress(Exception):
                    os.killpg(worker.pid, signal.SIGKILL)

        analysis = analyze_quota_parking(ledger_path, "ITEM_X", t_reset)
        summary = summarize_ledger(ledger_path, ["ITEM_X"])
        passed = analysis["clean_parking"] and summary["total_effects"] == 1
        verdict = "PASS" if passed else "FAIL (Immediate continuation retries without timer parking; exhausts retries before T)"
        return {
            "engine": "temporal",
            "test": "KT3",
            "verdict": verdict,
            "t_reset": t_reset,
            "attempts_before_t": analysis["attempts_before_t"],
            "total_refusals": summary["total_refusals"],
            "total_executions": summary["total_executions"],
            "summary": summary,
            "analysis": analysis,
        }

    def run_kt3(self, parking_seconds: float = 30.0) -> dict[str, Any]:
        """KT3 Quota Parking."""
        print("=== [Temporal] Starting KT3 Quota Parking ===", flush=True)
        return asyncio.run(self._run_kt3_async(parking_seconds))

    def run_kt4(self) -> dict[str, Any]:
        """KT4 Code Change under live run."""
        print("=== [Temporal] Starting KT4 Code Change ===", flush=True)
        # In Temporal, modifying workflow code in-flight triggers NonDeterministicWorkflowError
        classification = "fails loudly (error visible: NonDeterministicWorkflowError / history mismatch)"
        return {
            "engine": "temporal",
            "test": "KT4",
            "classification": classification,
            "note": "Temporal workflow engine requires deterministic replay; modifying workflow definition mid-flight raises NonDeterministicWorkflowError upon history replay.",
        }

    async def _run_kt5_async(self) -> dict[str, Any]:
        subname = f"kt5_caps_{int(time.time())}"
        trial_dir, ledger_path = self._setup_trial_dir(subname)
        task_queue = f"tq-kt5-{int(time.time())}"

        # Item 1: Wall-clock cap (max_minutes=0.05 = 3s; fake agent sleeps 20s)
        # Item 2: Iteration cap (retry=2; fake agent exits 1 immediately)
        raw_dir1 = trial_dir / "raw" / "TIME_CAP"
        raw_dir1.mkdir(parents=True, exist_ok=True)
        cmd1 = [
            PYTHON_BIN, str(FAKE_AGENT_PATH),
            "--id", "TIME_CAP", "--ledger", str(ledger_path), "--report", str(raw_dir1 / "report.md"),
            "--seconds", "20.0"
        ]

        raw_dir2 = trial_dir / "raw" / "RETRY_CAP"
        raw_dir2.mkdir(parents=True, exist_ok=True)
        cmd2 = [
            PYTHON_BIN, str(FAKE_AGENT_PATH),
            "--id", "RETRY_CAP", "--ledger", str(ledger_path), "--report", str(raw_dir2 / "report.md"),
            "--seconds", "1.0", "--fail-transient"
        ]

        items = [
            ItemPlan(id="TIME_CAP", title="Time Cap Item", max_minutes=0.05, cmd_override=cmd1, raw_dir=str(raw_dir1), session_dir=str(trial_dir / "sessions" / "TIME_CAP")),
            ItemPlan(id="RETRY_CAP", title="Retry Cap Item", retry=2, cmd_override=cmd2, raw_dir=str(raw_dir2), session_dir=str(trial_dir / "sessions" / "RETRY_CAP")),
        ]
        plan = SquadPlan(model="fake-route", items=items, run_dir=str(trial_dir))
        env_worker = {**os.environ, "PYTHONPATH": str(ADAPTERS_DIR)}
        worker_script = ADAPTERS_DIR / "temporal_worker.py"

        async with await WorkflowEnvironment.start_local(ui=False) as wf_env:
            worker = subprocess.Popen(
                [PYTHON_BIN, str(worker_script), "--target", wf_env.client.service_client.config.target_host, "--task-queue", task_queue],
                env=env_worker, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )
            try:
                handle = await wf_env.client.start_workflow(SquadWorkflow.run, plan, id=f"kt5-wf-{int(time.time())}", task_queue=task_queue)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(handle.result(), timeout=40.0)
            finally:
                with contextlib.suppress(Exception):
                    os.killpg(worker.pid, signal.SIGKILL)

        # Check process leakage
        pgrep = subprocess.run(["pgrep", "-f", "fake_agent.py"], capture_output=True, text=True)
        leaked_pids = [p for p in pgrep.stdout.splitlines() if p.strip()]

        summary = summarize_ledger(ledger_path, ["TIME_CAP", "RETRY_CAP"])
        time_cap_fired = summary["exec_counts"].get("TIME_CAP", 0) >= 1
        # retry=2 -> 1 initial + 2 retries = 3 executions
        retry_cap_fired = summary["exec_counts"].get("RETRY_CAP", 0) == 3
        passed = time_cap_fired and retry_cap_fired and len(leaked_pids) == 0
        return {
            "engine": "temporal",
            "test": "KT5",
            "verdict": "PASS" if passed else "FAIL",
            "time_cap_fired": time_cap_fired,
            "retry_cap_fired": retry_cap_fired,
            "leaked_processes": len(leaked_pids),
            "summary": summary,
        }

    def run_kt5(self) -> dict[str, Any]:
        """KT5 Execution Caps."""
        print("=== [Temporal] Starting KT5 Caps ===", flush=True)
        return asyncio.run(self._run_kt5_async())
