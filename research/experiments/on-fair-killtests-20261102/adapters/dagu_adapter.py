#!/usr/bin/env python3
"""dagu_adapter.py: Adapter for executing kill tests against Dagu."""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

EXPERIMENT_DIR = Path(__file__).resolve().parent.parent
ADAPTERS_DIR = EXPERIMENT_DIR / "adapters"
FAKE_AGENT_PATH = EXPERIMENT_DIR / "fake_agent.py"
DAGU_BIN = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F8/dagu/dagu")

sys.path.insert(0, str(EXPERIMENT_DIR))
from ledger_reader import analyze_kt1_ledger, analyze_kt3_ledger, read_ledger  # noqa: E402


class DaguAdapter:
    """Manages Dagu kill tests using an isolated DAGU_HOME without pkill -f."""

    def __init__(self, scratch_dir: Path | None = None) -> None:
        self.scratch_dir = (scratch_dir or (EXPERIMENT_DIR / "scratch" / "dagu")).resolve()
        self.dagu_home = self.scratch_dir / "home"
        self.dags_dir = self.dagu_home / "dags"
        self.dags_dir.mkdir(parents=True, exist_ok=True)
        self._init_config()

    def _init_config(self) -> None:
        cfg = self.dagu_home / "config.yaml"
        if not cfg.exists():
            content = (
                "host: 127.0.0.1\n"
                "port: 18081\n"
                "auth:\n"
                "  mode: none\n"
                "coordinator:\n"
                "  enabled: false\n"
                "proc:\n"
                "  heartbeat_interval: 1s\n"
                "  stale_threshold: 2s\n"
                "scheduler:\n"
                "  zombie_detection_interval: 1s\n"
                "  failure_threshold: 1\n"
            )
            cfg.write_text(content, encoding="utf-8")

    def _get_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["DAGU_HOME"] = str(self.dagu_home)
        env["DAGU_CONFIG"] = str(self.dagu_home / "config.yaml")
        return env

    def _write_squad_dag(self, dag_name: str, seconds_per_item: float = 20.0, mode: str = "guarded") -> Path:
        dag_file = self.dags_dir / f"{dag_name}.yaml"
        lines = [
            f"description: Squad DAG for {dag_name}",
            "max_active_steps: 2",
            "params:",
            "  LEDGER: ''",
            "  REPORT_DIR: ''",
            f"  ITEM_SECONDS: {seconds_per_item}",
            f"  MODE: {mode}",
            "steps:",
        ]
        chain_a = [("A1", []), ("A2", ["A1"]), ("A3", ["A2"])]
        chain_b = [("B1", []), ("B2", ["B1"]), ("B3", ["B2"])]
        for iid, deps in (chain_a + chain_b):
            lines.append(f"  - id: {iid}")
            if deps:
                lines.append(f"    depends: [{', '.join(deps)}]")
            lines.append(
                f"    run: python3 {FAKE_AGENT_PATH} --id {iid} --ledger \"${{LEDGER}}\" --report \"${{REPORT_DIR}}/{iid}.md\" --seconds ${{ITEM_SECONDS}} --mode ${{MODE}}"
            )
        dag_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return dag_file

    def run_kt1(
        self,
        trial_num: int,
        item_seconds: float = 20.0,
        fixture_mode: str = "guarded",
        orphan_scenario: str = "O1",
    ) -> dict[str, Any]:
        """KT1 Crash: Dagu runner kill mid-run and retry."""
        dag_name = f"kt1_dag_{trial_num}_{fixture_mode}_{orphan_scenario}_{int(time.time()*1000)}"
        self._write_squad_dag(dag_name, seconds_per_item=item_seconds, mode=fixture_mode)
        env = self._get_env()

        trial_dir = self.scratch_dir / "kt1" / f"trial_{trial_num}_{int(time.time()*1000)}"
        trial_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = trial_dir / "ledger.jsonl"
        report_dir = trial_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)

        run_id = f"kt1_{trial_num}_{int(time.time()*1000)}"
        cmd = [
            str(DAGU_BIN), "start",
            f"--run-id={run_id}",
            dag_name,
            "--",
            f"LEDGER={ledger_path}",
            f"REPORT_DIR={report_dir}",
            f"ITEM_SECONDS={item_seconds}",
            f"MODE={fixture_mode}",
        ]

        all_ids = ["A1", "A2", "A3", "B1", "B2", "B3"]
        t0 = time.time()
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

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
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                # O1: kill only the runner process
                os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=2)
        except Exception:
            pass

        # Wait for zombie detector to recognize the killed runner
        for _ in range(10):
            time.sleep(1)
            st = subprocess.run([str(DAGU_BIN), "status", f"--run-id={run_id}", dag_name], env=env, capture_output=True, text=True)
            out = (st.stdout + st.stderr).lower()
            if "failed" in out or "problem" in out:
                break

        # Retry the run
        retry_proc = subprocess.Popen(
            [str(DAGU_BIN), "retry", f"--run-id={run_id}", dag_name],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        try:
            retry_proc.wait(timeout=item_seconds * 4)
        except subprocess.TimeoutExpired:
            os.killpg(retry_proc.pid, signal.SIGKILL)
            retry_proc.wait(timeout=2)
        finally:
            with contextlib.suppress(Exception):
                if retry_proc.poll() is None:
                    os.killpg(retry_proc.pid, signal.SIGKILL)
                    retry_proc.wait(timeout=1)

        t_end = time.time()
        analysis = analyze_kt1_ledger(ledger_path, all_ids, t_kill)
        analysis["trial"] = trial_num
        analysis["engine"] = "dagu"
        analysis["fixture_mode"] = fixture_mode
        analysis["orphan_scenario"] = orphan_scenario
        analysis["total_wall_sec"] = round(t_end - t0, 2)
        analysis["recovery_wall_sec"] = round(t_end - t_kill, 2)
        return analysis

    def run_kt2_trial(self, trial_num: int) -> dict[str, Any]:
        """KT2 Human approval survives reboot with 60s+ downtime."""
        env = self._get_env()
        dag_name = f"kt2_approval_{trial_num}_{int(time.time()*1000)}"
        trial_dir = self.scratch_dir / "kt2" / f"trial_{trial_num}_{int(time.time()*1000)}"
        trial_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = trial_dir / "ledger.jsonl"
        report_dir = trial_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)

        dag_file = self.dags_dir / f"{dag_name}.yaml"
        dag_content = f"""description: KT2 Human Approval Reboot
steps:
  - id: prep
    run: python3 {FAKE_AGENT_PATH} --id PREP --ledger "{ledger_path}" --report "{report_dir}/PREP.md" --seconds 2
  - id: gate
    action: human.task
    depends: [prep]
    with:
      prompt: "Approve continuation of deployment"
  - id: deploy
    depends: [gate]
    run: python3 {FAKE_AGENT_PATH} --id DEPLOY --ledger "{ledger_path}" --report "{report_dir}/DEPLOY.md" --seconds 2
"""
        dag_file.write_text(dag_content, encoding="utf-8")

        run_id = f"kt2_run_{trial_num}_{int(time.time()*1000)}"
        t0 = time.time()

        # Start dagu runner process
        proc1 = subprocess.Popen([str(DAGU_BIN), "start", f"--run-id={run_id}", dag_name], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        # Wait until PREP finishes
        deadline = time.time() + 10.0
        while time.time() < deadline:
            time.sleep(0.5)
            rows = read_ledger(ledger_path)
            if any(r.get("kind") == "exit" and r.get("id") == "PREP" and r.get("rc", 0) == 0 for r in rows):
                break

        # Stop dagu runner process cleanly (no pkill -f)
        os.kill(proc1.pid, signal.SIGTERM)
        try:
            proc1.wait(timeout=2)
        except Exception:
            os.kill(proc1.pid, signal.SIGKILL)

        # 62s host downtime
        time.sleep(62.0)

        # Restart runner
        proc2 = subprocess.Popen([str(DAGU_BIN), "start", f"--run-id={run_id}", dag_name], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        # Spec: approval delivered at least 60 seconds after restart
        time.sleep(62.0)

        # Deliver approval using dagu human-task complete
        subprocess.run(
            [str(DAGU_BIN), "human-task", "complete", f"--run-id={run_id}", "--step=gate", "--input", "decision=approved", dag_name],
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
            "engine": "dagu",
            "passed": passed,
            "prep_starts": prep_starts,
            "deploy_starts": deploy_starts,
            "deploy_effects": deploy_effects,
            "total_wall_sec": round(t_end - t0, 2),
        }

    def run_kt3(self, parking_seconds: float = 90.0) -> dict[str, Any]:
        """KT3 Quota parking via Dagu retry policy."""
        env = self._get_env()
        dag_name = f"kt3_park_{int(time.time()*1000)}"
        trial_dir = self.scratch_dir / "kt3" / f"trial_{int(time.time()*1000)}"
        trial_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = trial_dir / "ledger.jsonl"
        report_dir = trial_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)

        t_reset = time.time() + parking_seconds
        dag_file = self.dags_dir / f"{dag_name}.yaml"
        dag_content = f"""description: KT3 Quota Park
steps:
  - id: quota_task
    run: python3 {FAKE_AGENT_PATH} --id QUOTA_ITEM --ledger "{ledger_path}" --report "{report_dir}/QUOTA_ITEM.md" --seconds 1.0 --quota-until {t_reset}
    retry_policy:
      limit: 2
      interval_sec: {int(parking_seconds)}
"""
        dag_file.write_text(dag_content, encoding="utf-8")

        run_id = f"kt3_run_{int(time.time()*1000)}"
        t0 = time.time()
        proc = subprocess.Popen([str(DAGU_BIN), "start", f"--run-id={run_id}", dag_name], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        try:
            proc.wait(timeout=parking_seconds + 30.0)
        finally:
            with contextlib.suppress(Exception):
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=1)

        t_end = time.time()
        analysis = analyze_kt3_ledger(ledger_path, t_reset)
        analysis["engine"] = "dagu"
        analysis["total_wall_sec"] = round(t_end - t0, 2)
        analysis["t_reset"] = t_reset
        return analysis

    def run_kt5(self) -> dict[str, Any]:
        """KT5 Caps enforcement & verified status row in Dagu."""
        env = self._get_env()
        dag_name = f"kt5_caps_{int(time.time()*1000)}"
        trial_dir = self.scratch_dir / "kt5" / f"trial_{int(time.time()*1000)}"
        trial_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = trial_dir / "ledger.jsonl"
        report_dir = trial_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)

        dag_file = self.dags_dir / f"{dag_name}.yaml"
        dag_content = f"""description: KT5 Caps Enforcement
steps:
  - id: TIME_CAP
    timeout_sec: 3
    run: python3 {FAKE_AGENT_PATH} --id TIME_CAP --ledger "{ledger_path}" --report "{report_dir}/time.md" --seconds 30.0
  - id: RETRY_CAP
    run: python3 {FAKE_AGENT_PATH} --id RETRY_CAP --ledger "{ledger_path}" --report "{report_dir}/retry.md" --seconds 0.5 --fail-transient
    retry_policy:
      limit: 2
      interval_sec: 1
"""
        dag_file.write_text(dag_content, encoding="utf-8")

        run_id = f"kt5_run_{int(time.time()*1000)}"
        t0 = time.time()
        proc = subprocess.Popen([str(DAGU_BIN), "start", f"--run-id={run_id}", dag_name], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        try:
            proc.wait(timeout=25.0)
        finally:
            with contextlib.suppress(Exception):
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=1)

        t_end = time.time()

        rows = read_ledger(ledger_path)
        time_cap_execs = sum(1 for r in rows if r.get("kind") == "start" and r.get("id") == "TIME_CAP")
        retry_cap_execs = sum(1 for r in rows if r.get("kind") == "start" and r.get("id") == "RETRY_CAP")

        # Read status from dagu status
        st = subprocess.run([str(DAGU_BIN), "status", f"--run-id={run_id}", dag_name], env=env, capture_output=True, text=True)
        status_text = st.stdout + st.stderr

        time_cap_fired = (time_cap_execs == 1) and ("timeout" in status_text.lower() or "failed" in status_text.lower() or t_end - t0 < 10.0)
        retry_cap_fired = (retry_cap_execs == 3) and ("failed" in status_text.lower() or "error" in status_text.lower())

        return {
            "engine": "dagu",
            "time_cap_fired": time_cap_fired,
            "retry_cap_fired": retry_cap_fired,
            "time_cap_execs": time_cap_execs,
            "retry_cap_execs": retry_cap_execs,
            "total_wall_sec": round(t_end - t0, 2),
            "passed": time_cap_fired and retry_cap_fired,
        }
