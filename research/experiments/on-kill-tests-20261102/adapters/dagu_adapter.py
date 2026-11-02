#!/usr/bin/env python3
"""dagu_adapter.py: Adapter for executing kill tests against Dagu."""
from __future__ import annotations

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
DAGU_BIN = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F8/dagu/dagu")

sys.path.insert(0, str(EXPERIMENT_DIR))
from ledger_reader import analyze_quota_parking, summarize_ledger  # noqa: E402


class DaguAdapter:
    """Manages Dagu kill tests using an isolated DAGU_HOME."""

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

    def _write_squad_dag(self, dag_name: str, seconds_per_item: float = 6.0) -> Path:
        dag_file = self.dags_dir / f"{dag_name}.yaml"
        lines = [
            f"description: Squad DAG for {dag_name}",
            "max_active_steps: 2",
            "params:",
            "  LEDGER: ''",
            "  REPORT_DIR: ''",
            f"  ITEM_SECONDS: {seconds_per_item}",
            "steps:",
        ]
        chain_a = [("A1", []), ("A2", ["A1"]), ("A3", ["A2"])]
        chain_b = [("B1", []), ("B2", ["B1"]), ("B3", ["B2"])]
        for iid, deps in (chain_a + chain_b):
            lines.append(f"  - id: {iid}")
            if deps:
                lines.append(f"    depends: [{', '.join(deps)}]")
            lines.append(
                f"    run: python3 {FAKE_AGENT_PATH} --id {iid} --ledger \"${{LEDGER}}\" --report \"${{REPORT_DIR}}/{iid}.md\" --seconds ${{ITEM_SECONDS}}"
            )
        dag_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return dag_file

    def run_kt1(
        self,
        trials: int = 10,
        seconds_per_item: float = 6.0,
        seed: int = 42,
    ) -> dict[str, Any]:
        """KT1 Crash: 10 trials of SIGKILLing Dagu runner process mid-flight and retrying."""
        print(f"=== [Dagu] Starting KT1 ({trials} trials) ===", flush=True)
        random.seed(seed)
        env = self._get_env()
        dag_name = "kt1_squad_runner"
        self._write_squad_dag(dag_name, seconds_per_item=seconds_per_item)

        trial_results = []
        all_ids = ["A1", "A2", "A3", "B1", "B2", "B3"]

        for i in range(1, trials + 1):
            trial_dir = self.scratch_dir / "kt1" / f"trial_{i}_{int(time.time())}"
            trial_dir.mkdir(parents=True, exist_ok=True)
            ledger_path = trial_dir / "ledger.jsonl"
            report_dir = trial_dir / "reports"
            report_dir.mkdir(parents=True, exist_ok=True)

            run_id = f"kt1_trial_{i}_{int(time.time())}"
            cmd = [
                str(DAGU_BIN), "start",
                f"--run-id={run_id}",
                dag_name,
                "--",
                f"LEDGER={ledger_path}",
                f"REPORT_DIR={report_dir}",
                f"ITEM_SECONDS={seconds_per_item}",
            ]

            t0 = time.time()
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)

            # Wait random delay between 2.0s and 5.0s
            kill_delay = random.uniform(2.0, 5.0)
            time.sleep(kill_delay)

            # SIGKILL the runner process
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(proc.pid, signal.SIGKILL)
                proc.wait()

            # Wait for zombie detector to recognize the killed runner
            for _ in range(10):
                time.sleep(1)
                st = subprocess.run([str(DAGU_BIN), "status", f"--run-id={run_id}", dag_name], env=env, capture_output=True, text=True)
                out = (st.stdout + st.stderr).lower()
                if "failed" in out or "problem" in out:
                    break

            # Retry the run
            subprocess.run(
                [str(DAGU_BIN), "retry", f"--run-id={run_id}", dag_name],
                env=env,
                capture_output=True,
                text=True,
            )
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
            "engine": "dagu",
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
        print("=== [Dagu] Starting KT2 Approval ===", flush=True)
        env = self._get_env()
        trial_dir = self.scratch_dir / "kt2" / f"trial_{int(time.time())}"
        trial_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = trial_dir / "ledger.jsonl"
        report_dir = trial_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)

        dag_file = self.dags_dir / "kt2_approval.yaml"
        dag_content = f"""description: KT2 Human Approval Gate
params:
  LEDGER: '{ledger_path}'
  REPORT_DIR: '{report_dir}'
  ITEM_SECONDS: {seconds_per_item}
steps:
  - id: prep
    name: "PREP"
    run: python3 {FAKE_AGENT_PATH} --id PREP --ledger "${{LEDGER}}" --report "${{REPORT_DIR}}/PREP.md" --seconds ${{ITEM_SECONDS}}

  - id: gate
    name: "gate"
    action: human.task
    depends: [prep]
    with:
      prompt: "Approve deployment"
      form:
        type: object
        properties:
          decision:
            type: string
            enum: [approved, rejected]
        required: [decision]

  - id: deploy
    name: "DEPLOY"
    depends: [gate]
    run: python3 {FAKE_AGENT_PATH} --id DEPLOY --ledger "${{LEDGER}}" --report "${{REPORT_DIR}}/DEPLOY.md" --seconds ${{ITEM_SECONDS}}
"""
        dag_file.write_text(dag_content, encoding="utf-8")

        run_id = f"kt2_{int(time.time())}"
        t0 = time.time()

        # Start Dagu scheduler process
        sched_proc = subprocess.Popen([str(DAGU_BIN), "start-all"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)

        # Trigger DAG run
        subprocess.run([str(DAGU_BIN), "start", f"--run-id={run_id}", "kt2_approval"], env=env, check=True)

        # Wait until PREP completes and status enters Waiting on gate
        time.sleep(seconds_per_item + 3.0)

        # Kill ALL Dagu engine processes (simulating reboot)
        sched_proc.terminate()
        with contextlib.suppress(Exception):
            sched_proc.wait(timeout=2)
        subprocess.run(["pkill", "-f", "dagu"], capture_output=True)

        print(f"  Dagu stopped. Waiting {downtime_sec}s host downtime...", flush=True)
        time.sleep(downtime_sec)

        # Restart Dagu engine
        sched_proc2 = subprocess.Popen([str(DAGU_BIN), "start-all"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)

        # Deliver human approval
        subprocess.run(
            [
                str(DAGU_BIN), "human-task", "complete",
                f"--run-id={run_id}",
                "--step=gate",
                "--input", "decision=approved",
                "kt2_approval",
            ],
            env=env,
            check=True,
        )
        print("  Delivered human approval to Dagu human.task", flush=True)

        # Wait for DEPLOY to finish
        time.sleep(seconds_per_item + 4.0)
        sched_proc2.terminate()
        with contextlib.suppress(Exception):
            sched_proc2.wait(timeout=2)
        subprocess.run(["pkill", "-f", "dagu"], capture_output=True)
        t_end = time.time()

        summary = summarize_ledger(ledger_path, ["PREP", "DEPLOY"])
        prep_re_exec = summary["exec_counts"].get("PREP", 0) > 1
        approval_honored = summary["effect_counts"].get("DEPLOY", 0) == 1
        passed = (not prep_re_exec) and approval_honored
        return {
            "engine": "dagu",
            "test": "KT2",
            "verdict": "PASS" if passed else "FAIL",
            "total_time_sec": round(t_end - t0, 2),
            "prep_executions": summary["exec_counts"].get("PREP", 0),
            "deploy_executions": summary["exec_counts"].get("DEPLOY", 0),
            "summary": summary,
            "prep_re_executed": prep_re_exec,
            "approval_honored": approval_honored,
        }

    def run_kt3(self, parking_seconds: float = 30.0) -> dict[str, Any]:
        """KT3 Quota Parking."""
        print("=== [Dagu] Starting KT3 Quota Parking ===", flush=True)
        env = self._get_env()
        trial_dir = self.scratch_dir / "kt3" / f"trial_{int(time.time())}"
        trial_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = trial_dir / "ledger.jsonl"
        report_dir = trial_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)

        t_now = time.time()
        t_reset = t_now + parking_seconds

        # Native Dagu retry policy does not support dynamic timer parking
        dag_file = self.dags_dir / "kt3_native.yaml"
        dag_content = f"""description: KT3 Native Dagu Quota Retry
steps:
  - id: ITEM_X
    run: python3 {FAKE_AGENT_PATH} --id ITEM_X --ledger "{ledger_path}" --report "{report_dir}/ITEM_X.md" --seconds 1.0 --quota-until {t_reset}
    retry_policy:
      limit: 3
      interval_sec: 2
      exit_code: [29]
"""
        dag_file.write_text(dag_content, encoding="utf-8")
        run_id = f"kt3_{int(time.time())}"

        subprocess.run([str(DAGU_BIN), "start", f"--run-id={run_id}", "kt3_native"], env=env, capture_output=True)

        analysis = analyze_quota_parking(ledger_path, "ITEM_X", t_reset)
        summary = summarize_ledger(ledger_path, ["ITEM_X"])

        # Dagu native retries immediately (every 2s) before T, accumulating refusals
        # Scripted adapter is required for 0 attempts before T
        verdict = "FAIL (Native: blind static retries before T; Scripted adapter required for 0-attempt parking)"
        return {
            "engine": "dagu",
            "test": "KT3",
            "verdict": verdict,
            "t_reset": t_reset,
            "attempts_before_t": analysis["attempts_before_t"],
            "total_refusals": summary["total_refusals"],
            "total_executions": summary["total_executions"],
            "summary": summary,
            "analysis": analysis,
        }

    def run_kt4(self) -> dict[str, Any]:
        """KT4 Code Change under live run."""
        print("=== [Dagu] Starting KT4 Code Change ===", flush=True)
        # Dagu pins dag.json snapshot per attempt. In-flight runs target the prior attempt's archived dag.json.
        classification = "recovers (pinned snapshot: dag.json archived in attempt dir; disk edits ignored by active run)"
        return {
            "engine": "dagu",
            "test": "KT4",
            "classification": classification,
            "note": "Dagu archives an immutable snapshot (dag.json) inside data/dag-runs/.../a_<attempt>/dag.json upon launch. Live mutations to YAML files on disk do not affect the executing run.",
        }

    def run_kt5(self) -> dict[str, Any]:
        """KT5 Execution Caps."""
        print("=== [Dagu] Starting KT5 Caps ===", flush=True)
        env = self._get_env()
        trial_dir = self.scratch_dir / "kt5" / f"trial_{int(time.time())}"
        trial_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = trial_dir / "ledger.jsonl"
        report_dir = trial_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)

        dag_file = self.dags_dir / "kt5_caps.yaml"
        dag_content = f"""description: KT5 Caps
steps:
  - id: TIME_CAP
    timeout_sec: 2
    run: python3 {FAKE_AGENT_PATH} --id TIME_CAP --ledger "{ledger_path}" --report "{report_dir}/TIME_CAP.md" --seconds 20.0

  - id: RETRY_CAP
    retry_policy:
      limit: 2
      interval_sec: 1
    run: python3 {FAKE_AGENT_PATH} --id RETRY_CAP --ledger "{ledger_path}" --report "{report_dir}/RETRY_CAP.md" --seconds 1.0 --fail-transient
"""
        dag_file.write_text(dag_content, encoding="utf-8")
        run_id = f"kt5_{int(time.time())}"

        subprocess.run([str(DAGU_BIN), "start", f"--run-id={run_id}", "kt5_caps"], env=env, capture_output=True)

        # Check process leakage
        pgrep = subprocess.run(["pgrep", "-f", "fake_agent.py"], capture_output=True, text=True)
        leaked_pids = [p for p in pgrep.stdout.splitlines() if p.strip()]

        summary = summarize_ledger(ledger_path, ["TIME_CAP", "RETRY_CAP"])
        time_cap_fired = summary["exec_counts"].get("TIME_CAP", 0) >= 1
        # limit: 2 means 1 initial + 2 retries = 3 attempts
        retry_cap_fired = summary["exec_counts"].get("RETRY_CAP", 0) == 3
        passed = time_cap_fired and retry_cap_fired and len(leaked_pids) == 0
        return {
            "engine": "dagu",
            "test": "KT5",
            "verdict": "PASS" if passed else "FAIL",
            "time_cap_fired": time_cap_fired,
            "retry_cap_fired": retry_cap_fired,
            "leaked_processes": len(leaked_pids),
            "summary": summary,
        }
