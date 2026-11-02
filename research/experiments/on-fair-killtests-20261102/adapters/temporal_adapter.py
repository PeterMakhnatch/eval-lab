#!/usr/bin/env python3
"""temporal_adapter.py: Adapter for executing kill tests against Temporal runner."""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

EXPERIMENT_DIR = Path(__file__).resolve().parent.parent
ADAPTERS_DIR = EXPERIMENT_DIR / "adapters"
FAKE_AGENT_PATH = EXPERIMENT_DIR / "fake_agent.py"

sys.path.insert(0, str(ADAPTERS_DIR))
sys.path.insert(0, str(EXPERIMENT_DIR))

from ledger_reader import analyze_kt1_ledger, analyze_kt3_ledger, read_ledger  # noqa: E402
from temporal_squad.models import ItemPlan, SquadPlan  # noqa: E402
from temporal_squad.workflows import SquadWorkflow  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402


class TemporalAdapter:
    """Manages Temporal squad kill tests with SQLite persistence and event-driven kills."""

    def __init__(self, scratch_dir: Path | None = None) -> None:
        self.scratch_dir = scratch_dir or (EXPERIMENT_DIR / "scratch" / "temporal")
        self.scratch_dir.mkdir(parents=True, exist_ok=True)

    def _setup_trial_dir(self, subname: str) -> tuple[Path, Path]:
        trial_dir = self.scratch_dir / subname
        trial_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = trial_dir / "ledger.jsonl"
        return trial_dir, ledger_path

    async def run_kt1_trial_async(
        self,
        trial_num: int,
        item_seconds: float = 20.0,
        fixture_mode: str = "guarded",
        orphan_scenario: str = "O1",
    ) -> dict[str, Any]:
        subname = f"kt1_trial_{trial_num}_{fixture_mode}_{orphan_scenario}_{int(time.time()*1000)}"
        trial_dir, ledger_path = self._setup_trial_dir(subname)
        task_queue = f"tq-kt1-{trial_num}-{int(time.time()*1000)}"

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
                sys.executable,
                str(FAKE_AGENT_PATH),
                "--id", iid,
                "--ledger", str(ledger_path),
                "--report", str(report_path),
                "--seconds", str(item_seconds),
                "--mode", fixture_mode,
            ]
            items.append(
                ItemPlan(
                    id=iid,
                    title=f"Task {iid}",
                    needs=needs,
                    cmd_override=cmd,
                    raw_dir=str(raw_dir),
                    session_dir=str(trial_dir / "sessions" / iid),
                    max_minutes=5.0,
                )
            )

        plan = SquadPlan(
            model="fake-route",
            budget_usd=100.0,
            max_parallel=2,
            run_dir=str(trial_dir),
            items=items,
        )

        env_worker = {**os.environ, "PYTHONPATH": f"{ADAPTERS_DIR}:{EXPERIMENT_DIR}"}
        worker_script = ADAPTERS_DIR / "temporal_worker.py"

        t0 = time.time()
        async with await WorkflowEnvironment.start_local(ui=False) as wf_env:
            client = wf_env.client
            target = client.service_client.config.target_host

            # Start worker 1
            cmd_w1 = [
                sys.executable,
                str(worker_script),
                "--target", target,
                "--task-queue", task_queue,
            ]
            worker1 = subprocess.Popen(
                cmd_w1,
                env=env_worker,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            # Start workflow handle
            wf_handle = await client.start_workflow(
                SquadWorkflow.run,
                args=[plan],
                id=f"wf-kt1-{trial_num}-{int(time.time()*1000)}",
                task_queue=task_queue,
                execution_timeout=timedelta(minutes=10),
            )

            # Event-driven trigger: poll until >=1 completed and >=1 in flight
            t_kill = None
            deadline = time.time() + (item_seconds * 3.5)
            while time.time() < deadline:
                await asyncio.sleep(0.2)
                rows = read_ledger(ledger_path)
                exits = {r["id"] for r in rows if r.get("kind") == "exit" and r.get("rc", 0) == 0}
                starts = {r["id"] for r in rows if r.get("kind") == "start"}
                in_flight = starts - exits
                if len(exits) >= 1 and len(in_flight) >= 1:
                    t_kill = time.time()
                    break

            if t_kill is None:
                t_kill = time.time()

            # Execute kill
            try:
                if orphan_scenario == "O2":
                    os.killpg(worker1.pid, signal.SIGKILL)
                else:
                    # O1: kill only the worker process, leave child agents running
                    os.kill(worker1.pid, signal.SIGKILL)
                worker1.wait(timeout=2)
            except Exception:
                pass

            await asyncio.sleep(1.0)

            # Start worker 2
            worker2 = subprocess.Popen(
                cmd_w1,
                env=env_worker,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            # Wait for workflow completion
            try:
                await wf_handle.result()
            finally:
                with contextlib.suppress(Exception):
                    os.killpg(worker2.pid, signal.SIGKILL)
                    worker2.wait(timeout=2)

        t_end = time.time()
        analysis = analyze_kt1_ledger(ledger_path, all_ids, t_kill)
        analysis["trial"] = trial_num
        analysis["engine"] = "temporal"
        analysis["fixture_mode"] = fixture_mode
        analysis["orphan_scenario"] = orphan_scenario
        analysis["total_wall_sec"] = round(t_end - t0, 2)
        analysis["recovery_wall_sec"] = round(t_end - t_kill, 2)
        return analysis

    def run_kt1(
        self,
        trial_num: int,
        item_seconds: float = 20.0,
        fixture_mode: str = "guarded",
        orphan_scenario: str = "O1",
    ) -> dict[str, Any]:
        return asyncio.run(
            self.run_kt1_trial_async(
                trial_num,
                item_seconds=item_seconds,
                fixture_mode=fixture_mode,
                orphan_scenario=orphan_scenario,
            )
        )
    async def run_kt2_trial_async(
        self,
        trial_num: int,
        downtime_sec: float = 62.0,
        post_restart_delay_sec: float = 62.0,
    ) -> dict[str, Any]:
        subname = f"kt2_trial_{trial_num}_{int(time.time()*1000)}"
        trial_dir, ledger_path = self._setup_trial_dir(subname)
        db_file = trial_dir / "temporal_system.sqlite"
        task_queue = f"tq-kt2-{trial_num}-{int(time.time()*1000)}"
        workflow_id = f"kt2-wf-{trial_num}-{int(time.time()*1000)}"

        raw_prep = trial_dir / "raw" / "PREP"
        raw_deploy = trial_dir / "raw" / "DEPLOY"
        raw_prep.mkdir(parents=True, exist_ok=True)
        raw_deploy.mkdir(parents=True, exist_ok=True)

        cmd_prep = [sys.executable, str(FAKE_AGENT_PATH), "--id", "PREP", "--ledger", str(ledger_path), "--report", str(raw_prep / "report.md"), "--seconds", "2.0"]
        cmd_deploy = [sys.executable, str(FAKE_AGENT_PATH), "--id", "DEPLOY", "--ledger", str(ledger_path), "--report", str(raw_deploy / "report.md"), "--seconds", "2.0"]

        items = [
            ItemPlan(id="PREP", title="Prep", cmd_override=cmd_prep, raw_dir=str(raw_prep), max_minutes=1.0),
            ItemPlan(id="DEPLOY", title="Deploy", cmd_override=cmd_deploy, raw_dir=str(raw_deploy), needs=["PREP"], gate="approvals/deploy.txt", max_minutes=2.0),
        ]
        plan = SquadPlan(model="fake-route", items=items, run_dir=str(trial_dir))

        env_worker = {**os.environ, "PYTHONPATH": f"{ADAPTERS_DIR}:{EXPERIMENT_DIR}"}
        worker_script = ADAPTERS_DIR / "temporal_worker.py"

        t0 = time.time()
        # 1. Start dev server 1 with SQLite persistence
        async with await WorkflowEnvironment.start_local(ui=False, dev_server_database_filename=str(db_file)) as wf_env1:
            target1 = wf_env1.client.service_client.config.target_host
            worker1 = subprocess.Popen([sys.executable, str(worker_script), "--target", target1, "--task-queue", task_queue], env=env_worker, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                await wf_env1.client.start_workflow(SquadWorkflow.run, plan, id=workflow_id, task_queue=task_queue)
                # Wait for PREP to complete
                while True:
                    await asyncio.sleep(0.5)
                    rows = read_ledger(ledger_path)
                    if any(r.get("kind") == "exit" and r.get("id") == "PREP" for r in rows):
                        break
            finally:
                with contextlib.suppress(Exception):
                    os.killpg(worker1.pid, signal.SIGKILL)
                    worker1.wait(timeout=2)

        # Machine downtime
        await asyncio.sleep(downtime_sec)

        # 2. Restart dev server on same SQLite database
        async with await WorkflowEnvironment.start_local(ui=False, dev_server_database_filename=str(db_file)) as wf_env2:
            target2 = wf_env2.client.service_client.config.target_host
            worker2 = subprocess.Popen([sys.executable, str(worker_script), "--target", target2, "--task-queue", task_queue], env=env_worker, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                handle2 = wf_env2.client.get_workflow_handle(workflow_id)
                # Approval delivered at least 60s after restart
                await asyncio.sleep(post_restart_delay_sec)
                await handle2.signal(SquadWorkflow.release_gate, "approvals/deploy.txt")
                await asyncio.wait_for(handle2.result(), timeout=60.0)
            finally:
                with contextlib.suppress(Exception):
                    os.killpg(worker2.pid, signal.SIGKILL)
                    worker2.wait(timeout=2)

        t_end = time.time()
        rows = read_ledger(ledger_path)
        prep_starts = sum(1 for r in rows if r.get("kind") == "start" and r.get("id") == "PREP")
        deploy_starts = sum(1 for r in rows if r.get("kind") == "start" and r.get("id") == "DEPLOY")
        deploy_effects = sum(1 for r in rows if r.get("kind") == "effect" and r.get("id") == "DEPLOY")
        passed = (prep_starts == 1) and (deploy_starts == 1) and (deploy_effects == 1)

        return {
            "trial": trial_num,
            "engine": "temporal",
            "passed": passed,
            "prep_starts": prep_starts,
            "deploy_starts": deploy_starts,
            "deploy_effects": deploy_effects,
            "total_wall_sec": round(t_end - t0, 2),
        }

    def run_kt2_trial(
        self,
        trial_num: int,
        downtime_sec: float = 62.0,
        post_restart_delay_sec: float = 62.0,
    ) -> dict[str, Any]:
        return asyncio.run(
            self.run_kt2_trial_async(
                trial_num,
                downtime_sec=downtime_sec,
                post_restart_delay_sec=post_restart_delay_sec,
            )
        )

    async def run_kt3_async(self, parking_seconds: float = 90.0) -> dict[str, Any]:
        """KT3 Quota park using idiomatic ApplicationError(next_retry_delay=...)."""
        subname = f"kt3_{int(time.time()*1000)}"
        trial_dir, ledger_path = self._setup_trial_dir(subname)
        task_queue = f"tq-kt3-{int(time.time()*1000)}"
        t_reset = time.time() + parking_seconds

        raw_dir = trial_dir / "raw" / "QUOTA_ITEM"
        raw_dir.mkdir(parents=True, exist_ok=True)
        report_path = raw_dir / "report.md"

        cmd = [
            sys.executable,
            str(FAKE_AGENT_PATH),
            "--id", "QUOTA_ITEM",
            "--ledger", str(ledger_path),
            "--report", str(report_path),
            "--seconds", "1.0",
            "--quota-until", str(t_reset),
        ]

        items = [
            ItemPlan(
                id="QUOTA_ITEM",
                title="Quota Task",
                cmd_override=cmd,
                raw_dir=str(raw_dir),
                session_dir=str(trial_dir / "sessions" / "QUOTA_ITEM"),
                max_minutes=5.0,
                retry=3,
            )
        ]
        plan = SquadPlan(model="fake-route", items=items, run_dir=str(trial_dir))

        env_worker = {**os.environ, "PYTHONPATH": f"{ADAPTERS_DIR}:{EXPERIMENT_DIR}"}
        worker_script = ADAPTERS_DIR / "temporal_worker.py"

        t0 = time.time()
        async with await WorkflowEnvironment.start_local(ui=False) as wf_env:
            client = wf_env.client
            target = client.service_client.config.target_host

            worker = subprocess.Popen(
                [sys.executable, str(worker_script), "--target", target, "--task-queue", task_queue],
                env=env_worker,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            try:
                await client.execute_workflow(
                    SquadWorkflow.run,
                    args=[plan],
                    id=f"wf-kt3-{int(time.time()*1000)}",
                    task_queue=task_queue,
                    execution_timeout=timedelta(seconds=parking_seconds + 60.0),
                )
            finally:
                with contextlib.suppress(Exception):
                    os.killpg(worker.pid, signal.SIGKILL)
                    worker.wait(timeout=2)

        t_end = time.time()
        analysis = analyze_kt3_ledger(ledger_path, t_reset)
        analysis["engine"] = "temporal"
        analysis["total_wall_sec"] = round(t_end - t0, 2)
        analysis["t_reset"] = t_reset
        return analysis

    def run_kt3(self, parking_seconds: float = 90.0) -> dict[str, Any]:
        return asyncio.run(self.run_kt3_async(parking_seconds=parking_seconds))

    async def run_kt5_async(self) -> dict[str, Any]:
        """KT5 Caps enforcement & verified status row."""
        subname = f"kt5_caps_{int(time.time()*1000)}"
        trial_dir, ledger_path = self._setup_trial_dir(subname)
        task_queue = f"tq-kt5-{int(time.time()*1000)}"

        # 1. Time cap: 3s cap
        raw_t = trial_dir / "raw" / "TIME_CAP"
        raw_t.mkdir(parents=True, exist_ok=True)
        cmd_t = [
            sys.executable,
            str(FAKE_AGENT_PATH),
            "--id", "TIME_CAP",
            "--ledger", str(ledger_path),
            "--report", str(raw_t / "report.md"),
            "--seconds", "30.0",
        ]

        # 2. Retry cap: 3 attempts
        raw_r = trial_dir / "raw" / "RETRY_CAP"
        raw_r.mkdir(parents=True, exist_ok=True)
        cmd_r = [
            sys.executable,
            str(FAKE_AGENT_PATH),
            "--id", "RETRY_CAP",
            "--ledger", str(ledger_path),
            "--report", str(raw_r / "report.md"),
            "--seconds", "0.5",
            "--fail-transient",
        ]

        items = [
            ItemPlan(id="TIME_CAP", title="Time Cap", cmd_override=cmd_t, raw_dir=str(raw_t), max_minutes=0.05),
            ItemPlan(id="RETRY_CAP", title="Retry Cap", cmd_override=cmd_r, raw_dir=str(raw_r), retry=2, max_minutes=2.0),
        ]
        plan = SquadPlan(model="fake-route", items=items, run_dir=str(trial_dir))

        env_worker = {**os.environ, "PYTHONPATH": f"{ADAPTERS_DIR}:{EXPERIMENT_DIR}"}
        worker_script = ADAPTERS_DIR / "temporal_worker.py"

        t0 = time.time()
        async with await WorkflowEnvironment.start_local(ui=False) as wf_env:
            client = wf_env.client
            target = client.service_client.config.target_host

            worker = subprocess.Popen(
                [sys.executable, str(worker_script), "--target", target, "--task-queue", task_queue],
                env=env_worker,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            try:
                res = await client.execute_workflow(
                    SquadWorkflow.run,
                    args=[plan],
                    id=f"wf-kt5-{int(time.time()*1000)}",
                    task_queue=task_queue,
                    execution_timeout=timedelta(seconds=45.0),
                )
            finally:
                with contextlib.suppress(Exception):
                    os.killpg(worker.pid, signal.SIGKILL)
                    worker.wait(timeout=2)

        t_end = time.time()
        rows = read_ledger(ledger_path)
        time_cap_execs = sum(1 for r in rows if r.get("kind") == "start" and r.get("id") == "TIME_CAP")
        retry_cap_execs = sum(1 for r in rows if r.get("kind") == "start" and r.get("id") == "RETRY_CAP")

        # Verify status rows in result
        status_rows = {}
        if isinstance(res, dict) and "states" in res:
            for iid, st in res["states"].items():
                status_rows[iid] = st.get("status") if isinstance(st, dict) else getattr(st, "status", None)
        elif isinstance(res, dict) and "results" in res:
            for it in res["results"]:
                status_rows[it.get("id")] = it.get("status")
        time_cap_fired = (time_cap_execs == 1) and (status_rows.get("TIME_CAP") in ("failed", "killed-time"))
        retry_cap_fired = (retry_cap_execs == 3) and (status_rows.get("RETRY_CAP") == "failed")

        return {
            "engine": "temporal",
            "time_cap_fired": time_cap_fired,
            "retry_cap_fired": retry_cap_fired,
            "status_rows": status_rows,
            "time_cap_execs": time_cap_execs,
            "retry_cap_execs": retry_cap_execs,
            "total_wall_sec": round(t_end - t0, 2),
            "passed": time_cap_fired and retry_cap_fired,
        }

    def run_kt5(self) -> dict[str, Any]:
        return asyncio.run(self.run_kt5_async())
