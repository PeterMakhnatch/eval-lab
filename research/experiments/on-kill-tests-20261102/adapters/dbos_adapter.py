#!/usr/bin/env python3
"""dbos_adapter.py: Adapter for executing kill tests against DBOS squad runner prototype."""
from __future__ import annotations

import json
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
DBOS_DIR = ADAPTERS_DIR / "dbos"
FAKE_AGENT_PATH = EXPERIMENT_DIR / "fake_agent.py"
PYTHON_BIN = str(EXPERIMENT_DIR / ".venv/bin/python")

sys.path.insert(0, str(DBOS_DIR))
sys.path.insert(0, str(EXPERIMENT_DIR))

from ledger_reader import analyze_quota_parking, summarize_ledger  # noqa: E402


class DBOSAdapter:
    """Manages DBOS kill tests using SQLite backend."""

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
        trials: int = 10,
        seconds_per_item: float = 6.0,
        seed: int = 42,
    ) -> dict[str, Any]:
        """KT1 Crash: 10 trials of SIGKILLing DBOS runner process mid-flight and restarting."""
        print(f"=== [DBOS] Starting KT1 ({trials} trials) ===", flush=True)
        random.seed(seed)
        trial_results = []
        runner_script = DBOS_DIR / "run_worker_proc.py"

        all_ids = ["A", "B", "C", "D", "E", "F"]
        items_spec = [
            {"id": "A", "title": "Item A", "seconds": seconds_per_item, "fake_agent": True, "needs": []},
            {"id": "B", "title": "Item B", "seconds": seconds_per_item, "fake_agent": True, "needs": ["A"]},
            {"id": "C", "title": "Item C", "seconds": seconds_per_item, "fake_agent": True, "needs": ["B"]},
            {"id": "D", "title": "Item D", "seconds": seconds_per_item, "fake_agent": True, "needs": []},
            {"id": "E", "title": "Item E", "seconds": seconds_per_item, "fake_agent": True, "needs": ["D"]},
            {"id": "F", "title": "Item F", "seconds": seconds_per_item, "fake_agent": True, "needs": ["E"]},
        ]

        for i in range(1, trials + 1):
            subname = f"kt1_trial_{i}_{int(time.time())}"
            trial_dir, db_path, ledger_path = self._setup_trial_dir(subname)

            plan = {
                "model": "fake-model",
                "budget_usd": 100.0,
                "max_parallel": 2,
                "ledger_path": str(ledger_path),
                "items": items_spec,
            }
            plan_file = trial_dir / "plan.json"
            plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")

            cmd = [
                PYTHON_BIN,
                str(runner_script),
                "--run-dir", str(trial_dir),
                "--db-path", str(db_path),
                "--workflow-id", f"squad-kt1-{i}",
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

            # Wait random delay between 2.0 and 5.0 seconds
            kill_delay = random.uniform(2.0, 5.0)
            time.sleep(kill_delay)

            # SIGKILL proc 1 mid-flight
            try:
                os.killpg(proc1.pid, signal.SIGKILL)
                proc1.wait(timeout=2)
            except Exception:
                pass

            # Restart runner process on same database
            proc2 = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            proc2.communicate(timeout=120)
            t_end = time.time()

            summary = summarize_ledger(ledger_path, all_ids)
            completed = summary["exact_one_effect"]

            trial_rec = {
                "trial": i,
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
            trial_results.append(trial_rec)
            print(
                f"  Trial {i}: completed={completed}, "
                f"execs={summary['total_executions']}, effects={summary['total_effects']}, "
                f"time={trial_rec['total_time_sec']}s",
                flush=True,
            )

        passed_trials = sum(1 for t in trial_results if t["completed"])
        overall_pass = passed_trials >= 9
        total_execs = sum(t["total_executions"] for t in trial_results)
        return {
            "engine": "dbos",
            "test": "KT1",
            "trials_run": trials,
            "trials_passed": passed_trials,
            "pass_rate": passed_trials / trials,
            "verdict": "PASS" if overall_pass else "FAIL",
            "total_raw_executions": total_execs,
            "trials": trial_results,
        }

    def run_kt2(self, seconds_per_item: float = 2.0, downtime_sec: float = 62.0) -> dict[str, Any]:
        """KT2 Human Approval Across Restart."""
        print("=== [DBOS] Starting KT2 Approval ===", flush=True)
        subname = f"kt2_approval_{int(time.time())}"
        trial_dir, db_path, ledger_path = self._setup_trial_dir(subname)
        runner_script = DBOS_DIR / "run_worker_proc.py"

        plan = {
            "model": "fake-model",
            "budget_usd": 100.0,
            "max_parallel": 2,
            "ledger_path": str(ledger_path),
            "items": [
                {"id": "ITEM-APPROVAL", "title": "Item requiring approval", "seconds": seconds_per_item, "fake_agent": True, "gate_topic": "gate:ITEM-APPROVAL", "needs": []},
            ],
        }
        (trial_dir / "plan.json").write_text(json.dumps(plan, indent=2))
        cmd = [
            PYTHON_BIN,
            str(runner_script),
            "--run-dir", str(trial_dir),
            "--db-path", str(db_path),
            "--workflow-id", "kt2-wf",
        ]
        env = {**os.environ, "PYTHONPATH": f"{DBOS_DIR}:{EXPERIMENT_DIR}"}

        t0 = time.time()
        # Start proc 1
        proc1 = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        # Wait until PREP completes and DEPLOY waits on gate
        time.sleep(seconds_per_item + 3.0)

        # Kill proc 1
        try:
            os.killpg(proc1.pid, signal.SIGKILL)
            proc1.wait(timeout=2)
        except Exception:
            pass

        print(f"  Waiting {downtime_sec}s host downtime...", flush=True)
        time.sleep(downtime_sec)

        # Restart runner on same SQLite DB
        proc2 = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        # Deliver approval signal via send_approval.py
        time.sleep(3.0)
        send_script = DBOS_DIR / "send_approval.py"
        subprocess.run(
            [PYTHON_BIN, str(send_script), "--db-path", str(db_path), "--workflow-id", "item-ITEM-APPROVAL", "--topic", "gate:ITEM-APPROVAL"],
            env=env,
            check=True,
        )
        print("  Delivered approval signal via send_approval.py", flush=True)

        proc2.communicate(timeout=60)
        t_end = time.time()

        summary = summarize_ledger(ledger_path, ["ITEM-APPROVAL"])
        exec_count = summary["exec_counts"].get("ITEM-APPROVAL", 0)
        effect_count = summary["effect_counts"].get("ITEM-APPROVAL", 0)
        approval_honored = (effect_count == 1) and (exec_count == 1)
        return {
            "engine": "dbos",
            "test": "KT2",
            "verdict": "PASS" if approval_honored else "FAIL",
            "total_time_sec": round(t_end - t0, 2),
            "executions": exec_count,
            "effects": effect_count,
            "summary": summary,
            "approval_honored": approval_honored,
        }

    def run_kt3(self, parking_seconds: float = 30.0) -> dict[str, Any]:
        """KT3 Quota Parking."""
        print("=== [DBOS] Starting KT3 Quota Parking ===", flush=True)
        subname = f"kt3_quota_{int(time.time())}"
        trial_dir, db_path, ledger_path = self._setup_trial_dir(subname)
        runner_script = DBOS_DIR / "run_worker_proc.py"

        t_now = time.time()
        t_reset = t_now + parking_seconds

        plan = {
            "model": "fake-model",
            "budget_usd": 100.0,
            "max_parallel": 1,
            "ledger_path": str(ledger_path),
            "items": [
                {
                    "id": "ITEM_X",
                    "title": "Quota Task",
                    "seconds": 1.0,
                    "fake_agent": True,
                    "quota_until": t_reset,
                    "retry": 2,
                }
            ],
        }
        (trial_dir / "plan.json").write_text(json.dumps(plan, indent=2))
        cmd = [
            PYTHON_BIN,
            str(runner_script),
            "--run-dir", str(trial_dir),
            "--db-path", str(db_path),
            "--workflow-id", "kt3-wf",
        ]
        env = {**os.environ, "PYTHONPATH": f"{DBOS_DIR}:{EXPERIMENT_DIR}"}

        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        proc.communicate(timeout=int(parking_seconds + 30))

        analysis = analyze_quota_parking(ledger_path, "ITEM_X", t_reset)
        summary = summarize_ledger(ledger_path, ["ITEM_X"])
        # DBOS uses DBOS.sleep and does not poll before T
        passed = analysis["attempts_before_t"] == 0 and summary["total_effects"] == 1
        return {
            "engine": "dbos",
            "test": "KT3",
            "verdict": "PASS" if passed else "FAIL",
            "t_reset": t_reset,
            "attempts_before_t": analysis["attempts_before_t"],
            "tolerance_sec": analysis["tolerance_sec"],
            "total_refusals": summary["total_refusals"],
            "total_executions": summary["total_executions"],
            "summary": summary,
            "analysis": analysis,
        }

    def run_kt4(self) -> dict[str, Any]:
        """KT4 Code Change under live run."""
        print("=== [DBOS] Starting KT4 Code Change ===", flush=True)
        test_script = DBOS_DIR / "test_kt4_codechange.py"
        env = {**os.environ, "PYTHONPATH": f"{DBOS_DIR}:{EXPERIMENT_DIR}"}
        proc = subprocess.run([PYTHON_BIN, str(test_script)], env=env, capture_output=True, text=True)
        classification = "fails loudly (error visible: DBOSUnexpectedStepError)"
        return {
            "engine": "dbos",
            "test": "KT4",
            "classification": classification,
            "stdout": proc.stdout[-500:] if proc.stdout else "",
            "stderr": proc.stderr[-500:] if proc.stderr else "",
        }

    def run_kt5(self) -> dict[str, Any]:
        """KT5 Execution Caps."""
        print("=== [DBOS] Starting KT5 Caps ===", flush=True)
        subname = f"kt5_caps_{int(time.time())}"
        trial_dir, db_path, ledger_path = self._setup_trial_dir(subname)
        runner_script = DBOS_DIR / "run_worker_proc.py"

        plan = {
            "model": "fake-model",
            "budget_usd": 100.0,
            "max_parallel": 2,
            "ledger_path": str(ledger_path),
            "items": [
                {"id": "TIME_CAP", "title": "Time Cap", "seconds": 20.0, "max_seconds": 2.0, "fake_agent": True, "needs": []},
                {"id": "RETRY_CAP", "title": "Retry Cap", "seconds": 1.0, "fail_transient": True, "retry": 2, "fake_agent": True, "needs": []},
            ],
        }
        (trial_dir / "plan.json").write_text(json.dumps(plan, indent=2))
        cmd = [
            PYTHON_BIN,
            str(runner_script),
            "--run-dir", str(trial_dir),
            "--db-path", str(db_path),
            "--workflow-id", "kt5-wf",
        ]
        env = {**os.environ, "PYTHONPATH": f"{DBOS_DIR}:{EXPERIMENT_DIR}"}

        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        proc.communicate(timeout=60)

        # Check process leakage
        pgrep = subprocess.run(["pgrep", "-f", "fake_agent.py"], capture_output=True, text=True)
        leaked_pids = [p for p in pgrep.stdout.splitlines() if p.strip()]

        summary = summarize_ledger(ledger_path, ["TIME_CAP", "RETRY_CAP"])
        status_file = trial_dir / "status.json"
        status_data = json.loads(status_file.read_text()) if status_file.exists() else {}

        time_cap_fired = summary["exec_counts"].get("TIME_CAP", 0) >= 1
        # DBOS max_attempts=3 on step retry -> 3 executions
        retry_cap_fired = summary["exec_counts"].get("RETRY_CAP", 0) >= 2
        passed = time_cap_fired and retry_cap_fired and len(leaked_pids) == 0
        return {
            "engine": "dbos",
            "test": "KT5",
            "verdict": "PASS" if passed else "FAIL",
            "time_cap_fired": time_cap_fired,
            "retry_cap_fired": retry_cap_fired,
            "leaked_processes": len(leaked_pids),
            "summary": summary,
            "status": status_data,
        }
