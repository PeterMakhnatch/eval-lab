#!/usr/bin/env python3
"""dbos_adapter.py: Adapter for executing kill tests against DBOS squad runner prototype."""
from __future__ import annotations

import contextlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

EXPERIMENT_DIR = Path(__file__).resolve().parent.parent
ADAPTERS_DIR = EXPERIMENT_DIR / "adapters"
DBOS_DIR = ADAPTERS_DIR / "dbos"
FAKE_AGENT_PATH = EXPERIMENT_DIR / "fake_agent.py"

sys.path.insert(0, str(DBOS_DIR))
sys.path.insert(0, str(EXPERIMENT_DIR))

from ledger_reader import analyze_kt1_ledger, analyze_kt3_ledger, read_ledger  # noqa: E402


class DBOSAdapter:
    """Manages DBOS kill tests with SQLite persistence and event-driven kills."""

    def __init__(self, scratch_dir: Path | None = None) -> None:
        self.scratch_dir = scratch_dir or (EXPERIMENT_DIR / "scratch" / "dbos")
        self.scratch_dir.mkdir(parents=True, exist_ok=True)

    def _setup_trial_dir(self, subname: str) -> tuple[Path, Path, Path]:
        trial_dir = self.scratch_dir / subname
        trial_dir.mkdir(parents=True, exist_ok=True)
        db_path = trial_dir / "dbos_system.sqlite"
        ledger_path = trial_dir / "ledger.jsonl"
        return trial_dir, db_path, ledger_path

    def run_kt1(
        self,
        trial_num: int,
        item_seconds: float = 20.0,
        fixture_mode: str = "guarded",
        orphan_scenario: str = "O1",
    ) -> dict[str, Any]:
        subname = f"kt1_trial_{trial_num}_{fixture_mode}_{orphan_scenario}_{int(time.time()*1000)}"
        trial_dir, db_path, ledger_path = self._setup_trial_dir(subname)
        runner_script = DBOS_DIR / "run_worker_proc.py"

        all_ids = ["A1", "A2", "A3", "B1", "B2", "B3"]
        items_spec = [
            {"id": "A1", "title": "A1", "seconds": item_seconds, "fake_agent": True, "needs": [], "mode": fixture_mode},
            {"id": "A2", "title": "A2", "seconds": item_seconds, "fake_agent": True, "needs": ["A1"], "mode": fixture_mode},
            {"id": "A3", "title": "A3", "seconds": item_seconds, "fake_agent": True, "needs": ["A2"], "mode": fixture_mode},
            {"id": "B1", "title": "B1", "seconds": item_seconds, "fake_agent": True, "needs": [], "mode": fixture_mode},
            {"id": "B2", "title": "B2", "seconds": item_seconds, "fake_agent": True, "needs": ["B1"], "mode": fixture_mode},
            {"id": "B3", "title": "B3", "seconds": item_seconds, "fake_agent": True, "needs": ["B2"], "mode": fixture_mode},
        ]

        plan = {
            "model": "fake-model",
            "budget_usd": 100.0,
            "max_parallel": 2,
            "ledger_path": str(ledger_path),
            "items": items_spec,
        }
        plan_file = trial_dir / "plan.json"
        plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")

        workflow_id = f"squad-kt1-{trial_num}-{int(time.time()*1000)}"
        cmd = [
            sys.executable,
            str(runner_script),
            "--run-dir", str(trial_dir),
            "--db-path", str(db_path),
            "--workflow-id", workflow_id,
        ]
        env = {**os.environ, "PYTHONPATH": f"{DBOS_DIR}:{EXPERIMENT_DIR}"}

        t0 = time.time()
        proc1 = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        # Event-driven trigger
        t_kill = None
        deadline = time.time() + (item_seconds * 3.5)
        while time.time() < deadline:
            time.sleep(0.2)
            rows = read_ledger(ledger_path)
            exits = {r["id"] for r in rows if r.get("kind") == "exit" and r.get("rc", 0) == 0}
            starts = {r["id"] for r in rows if r.get("kind") == "start"}
            in_flight = starts - exits
            if len(exits) >= 1 and len(in_flight) >= 1:
                t_kill = time.time()
                break

        if t_kill is None:
            t_kill = time.time()

        try:
            if orphan_scenario == "O2":
                os.killpg(proc1.pid, signal.SIGKILL)
            else:
                # O1: kill only the runner process; child agents survive as orphans
                os.kill(proc1.pid, signal.SIGKILL)
            proc1.wait(timeout=2)
        except Exception:
            pass

        time.sleep(1.0)

        # Restart runner
        proc2 = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        try:
            proc2.wait(timeout=item_seconds * 4)
        except subprocess.TimeoutExpired:
            os.killpg(proc2.pid, signal.SIGKILL)
            proc2.wait(timeout=2)
        finally:
            # Ensure proc2 is terminated cleanly to prevent orphan runners
            with contextlib.suppress(Exception):
                if proc2.poll() is None:
                    os.killpg(proc2.pid, signal.SIGKILL)
                    proc2.wait(timeout=1)

        t_end = time.time()
        analysis = analyze_kt1_ledger(ledger_path, all_ids, t_kill)
        analysis["trial"] = trial_num
        analysis["engine"] = "dbos"
        analysis["fixture_mode"] = fixture_mode
        analysis["orphan_scenario"] = orphan_scenario
        analysis["total_wall_sec"] = round(t_end - t0, 2)
        analysis["recovery_wall_sec"] = round(t_end - t_kill, 2)
        return analysis

    def run_kt2_trial(self, trial_num: int) -> dict[str, Any]:
        """KT2 Human approval survives reboot with 60s+ downtime."""
        subname = f"kt2_trial_{trial_num}_{int(time.time()*1000)}"
        trial_dir, db_path, ledger_path = self._setup_trial_dir(subname)
        runner_script = DBOS_DIR / "run_worker_proc.py"
        send_script = DBOS_DIR / "send_approval.py"
        workflow_id = f"kt2-wf-{trial_num}-{int(time.time()*1000)}"

        items = [
            {"id": "PREP", "title": "Prep Task", "seconds": 2.0, "fake_agent": True, "needs": []},
            {"id": "DEPLOY", "title": "Deploy Task", "seconds": 2.0, "fake_agent": True, "needs": ["PREP"], "approval": True},
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
        env = {**os.environ, "PYTHONPATH": f"{DBOS_DIR}:{EXPERIMENT_DIR}"}

        t0 = time.time()
        proc1 = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        # Wait until PREP completes
        deadline = time.time() + 10.0
        while time.time() < deadline:
            time.sleep(0.2)
            rows = read_ledger(ledger_path)
            if any(r.get("kind") == "exit" and r.get("id") == "PREP" and r.get("rc", 0) == 0 for r in rows):
                break

        # Stop runner cleanly
        os.kill(proc1.pid, signal.SIGTERM)
        try:
            proc1.wait(timeout=2)
        except Exception:
            os.kill(proc1.pid, signal.SIGKILL)

        # 62s host downtime
        time.sleep(62.0)

        # Restart runner
        proc2 = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        # Spec: approval delivered at least 60 seconds after restart
        time.sleep(62.0)
        subprocess.run(
            [sys.executable, str(send_script), "--db-path", str(db_path), "--workflow-id", workflow_id, "--approved", "true"],
            env=env,
            capture_output=True,
            timeout=10,
        )

        try:
            proc2.wait(timeout=30.0)
        finally:
            with contextlib.suppress(Exception):
                if proc2.poll() is None:
                    os.killpg(proc2.pid, signal.SIGKILL)
                    proc2.wait(timeout=1)

        t_end = time.time()
        rows = read_ledger(ledger_path)
        prep_starts = sum(1 for r in rows if r.get("kind") == "start" and r.get("id") == "PREP")
        deploy_starts = sum(1 for r in rows if r.get("kind") == "start" and r.get("id") == "DEPLOY")
        deploy_effects = sum(1 for r in rows if r.get("kind") == "effect" and r.get("id") == "DEPLOY")

        passed = (prep_starts == 1) and (deploy_starts == 1) and (deploy_effects == 1)
        return {
            "trial": trial_num,
            "engine": "dbos",
            "passed": passed,
            "prep_starts": prep_starts,
            "deploy_starts": deploy_starts,
            "deploy_effects": deploy_effects,
            "total_wall_sec": round(t_end - t0, 2),
        }

    def run_kt3(self, parking_seconds: float = 90.0) -> dict[str, Any]:
        """KT3 Quota park until T=now+90s via DBOS.sleep."""
        subname = f"kt3_{int(time.time()*1000)}"
        trial_dir, db_path, ledger_path = self._setup_trial_dir(subname)
        runner_script = DBOS_DIR / "run_worker_proc.py"
        workflow_id = f"kt3-wf-{int(time.time()*1000)}"
        t_reset = time.time() + parking_seconds

        items = [
            {"id": "QUOTA_ITEM", "title": "Quota Task", "seconds": 1.0, "fake_agent": True, "needs": [], "quota_until": t_reset, "retry": 3},
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
        env = {**os.environ, "PYTHONPATH": f"{DBOS_DIR}:{EXPERIMENT_DIR}"}

        t0 = time.time()
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        try:
            proc.wait(timeout=parking_seconds + 30.0)
        finally:
            with contextlib.suppress(Exception):
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=1)

        t_end = time.time()
        analysis = analyze_kt3_ledger(ledger_path, t_reset)
        analysis["engine"] = "dbos"
        analysis["total_wall_sec"] = round(t_end - t0, 2)
        analysis["t_reset"] = t_reset
        return analysis

    def run_kt5(self) -> dict[str, Any]:
        """KT5 Caps enforcement & verified status row in DBOS system database."""
        subname = f"kt5_caps_{int(time.time()*1000)}"
        trial_dir, db_path, ledger_path = self._setup_trial_dir(subname)
        runner_script = DBOS_DIR / "run_worker_proc.py"
        workflow_id = f"kt5-wf-{int(time.time()*1000)}"

        items = [
            {"id": "TIME_CAP", "title": "Time Cap", "seconds": 30.0, "fake_agent": True, "needs": [], "max_seconds": 3.0},
            {"id": "RETRY_CAP", "title": "Retry Cap", "seconds": 0.5, "fake_agent": True, "needs": [], "fail_transient": True, "retry": 2},
        ]
        plan = {
            "model": "fake-model",
            "budget_usd": 100.0,
            "max_parallel": 2,
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
        env = {**os.environ, "PYTHONPATH": f"{DBOS_DIR}:{EXPERIMENT_DIR}"}

        t0 = time.time()
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            proc.wait(timeout=25.0)
        finally:
            with contextlib.suppress(Exception):
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=1)

        t_end = time.time()

        # Read status from DBOS SQLite database
        status_rows: dict[str, Any] = {}
        status_file = trial_dir / "status.json"
        if status_file.exists():
            try:
                st_data = json.loads(status_file.read_text())
                items_field = st_data.get("items", {})
                if isinstance(items_field, dict):
                    for iid, it in items_field.items():
                        status_rows[iid] = it.get("status")
                elif isinstance(items_field, list):
                    for it in items_field:
                        status_rows[it["id"]] = it.get("status")
            except Exception:
                pass

        # Also inspect dbos system db if needed
        if db_path.exists():
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                cur = conn.cursor()
                cur.execute("SELECT workflow_id, status FROM dbos_workflow_status WHERE workflow_id = ?", (workflow_id,))
                for row in cur.fetchall():
                    status_rows[f"_workflow_{row[0]}"] = row[1]
                conn.close()
            except Exception:
                pass

        rows = read_ledger(ledger_path)
        time_cap_execs = sum(1 for r in rows if r.get("kind") == "start" and r.get("id") == "TIME_CAP")
        retry_cap_execs = sum(1 for r in rows if r.get("kind") == "start" and r.get("id") == "RETRY_CAP")

        time_cap_fired = (time_cap_execs == 1) and (status_rows.get("TIME_CAP") in ("failed", "killed-time"))
        retry_cap_fired = (retry_cap_execs == 3) and (status_rows.get("RETRY_CAP") == "failed")

        return {
            "engine": "dbos",
            "time_cap_fired": time_cap_fired,
            "retry_cap_fired": retry_cap_fired,
            "status_rows": status_rows,
            "time_cap_execs": time_cap_execs,
            "retry_cap_execs": retry_cap_execs,
            "total_wall_sec": round(t_end - t0, 2),
            "passed": time_cap_fired and retry_cap_fired,
        }
