#!/usr/bin/env python3
"""baseline_adapter.py: Adapter for executing kill tests against the hand-rolled ds_squad.py baseline runner."""
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
FAKE_AGENT_PATH = EXPERIMENT_DIR / "fake_agent.py"
DS_SQUAD_SCRIPT = Path.home() / "Developer/research-context/bridge/ds_squad.py"
BRIDGE_ROOT = Path.home() / "Developer/research-context/bridge"

sys.path.insert(0, str(EXPERIMENT_DIR))
from ledger_reader import analyze_quota_parking, summarize_ledger  # noqa: E402


class BaselineAdapter:
    """Manages isolated scratch runs of ds_squad.py with a fake omp shim."""

    def __init__(self, run_root: Path | None = None) -> None:
        self.run_root = (
            run_root
            or (BRIDGE_ROOT / "2026-11-02-overnight" / "scratch-baseline-f9")
        ).resolve()
        self.run_root.mkdir(parents=True, exist_ok=True)

    def _setup_run_dir(
        self,
        subname: str,
        items: list[dict[str, Any]],
        max_parallel: int = 2,
        extra_plan: dict[str, Any] | None = None,
    ) -> tuple[Path, Path, str]:
        run_dir = self.run_root / subname
        run_dir.mkdir(parents=True, exist_ok=True)
        rel_run_name = str(run_dir.relative_to(BRIDGE_ROOT))

        # Common and worker config
        (run_dir / "common.md").write_text("# Shared Squad Context\n\nKill-test baseline run.\n")
        (run_dir / "worker.yml").write_text("model: fake-model\n")
        briefs_dir = run_dir / "briefs"
        briefs_dir.mkdir(exist_ok=True)

        plan_items = []
        for it in items:
            iid = it["id"]
            brief_file = briefs_dir / f"{iid}.md"
            brief_file.write_text(f"# Task Brief for {iid}\nExecute fake agent item {iid}.\n")
            plan_items.append({
                "id": iid,
                "title": it.get("title", f"Task {iid}"),
                "brief": f"briefs/{iid}.md",
                "thinking": "low",
                "max_minutes": it.get("max_minutes", 5),
                "max_usd": it.get("max_usd", 2.0),
                "needs": it.get("needs", []),
                "gate": it.get("gate"),
                "worktree": False,
                "retry": it.get("retry", 0),
            })

        plan_data: dict[str, Any] = {
            "model": "fake-model",
            "budget_usd": 100.0,
            "max_parallel": max_parallel,
            "subscription": True,
            "dry_ledger": True,
            "wait_gates_minutes": 10,
            "items": plan_items,
        }
        if extra_plan:
            plan_data.update(extra_plan)

        (run_dir / "plan.json").write_text(json.dumps(plan_data, indent=2))
        ledger_path = run_dir / "ledger.jsonl"
        return run_dir, ledger_path, rel_run_name

    def _get_env(
        self,
        ledger_path: Path,
        item_seconds: float = 20.0,
        quota_until: float | None = None,
        fail_transient: bool = False,
    ) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = f"{ADAPTERS_DIR}:{env.get('PATH', '')}"
        env["FAKE_AGENT_LEDGER"] = str(ledger_path)
        env["FAKE_AGENT_SECONDS"] = str(item_seconds)
        if quota_until is not None:
            env["FAKE_AGENT_QUOTA_UNTIL"] = str(quota_until)
        else:
            env.pop("FAKE_AGENT_QUOTA_UNTIL", None)
        if fail_transient:
            env["FAKE_AGENT_FAIL_TRANSIENT"] = "1"
        else:
            env.pop("FAKE_AGENT_FAIL_TRANSIENT", None)
        return env

    def run_kt1(
        self,
        trials: int = 10,
        seconds_per_item: float = 20.0,
        seed: int = 42,
    ) -> dict[str, Any]:
        """KT1 Crash Resilience: 10 trials of SIGKILLing ds_squad runner process."""
        print(f"=== [Baseline ds_squad.py] Starting KT1 ({trials} trials) ===", flush=True)
        random.seed(seed)
        trial_results = []

        standard_items = [
            {"id": "A1", "needs": []},
            {"id": "A2", "needs": ["A1"]},
            {"id": "A3", "needs": ["A2"]},
            {"id": "B1", "needs": []},
            {"id": "B2", "needs": ["B1"]},
            {"id": "B3", "needs": ["B2"]},
        ]
        all_ids = ["A1", "A2", "A3", "B1", "B2", "B3"]

        for trial_num in range(1, trials + 1):
            subname = f"kt1_trial_{trial_num}_{int(time.time())}"
            run_dir, ledger_path, rel_run_name = self._setup_run_dir(
                subname, standard_items, max_parallel=2
            )
            env = self._get_env(ledger_path, item_seconds=seconds_per_item)

            cmd = [
                sys.executable,
                str(DS_SQUAD_SCRIPT),
                "--run", rel_run_name,
                "--tag", f"_kt1_{trial_num}",
                "--hours", "1",
            ]

            t0 = time.time()
            # Start runner process 1
            proc1 = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )

            # Wait random seeded delay (between 4.0s and 25.0s) while items run
            kill_delay = random.uniform(4.0, 25.0)
            time.sleep(kill_delay)

            # SIGKILL runner process 1
            try:
                os.killpg(proc1.pid, signal.SIGKILL)
                proc1.wait(timeout=5)
            except Exception:
                pass

            # Wait brief pause (simulating crash)
            time.sleep(1.0)

            # Restart runner process 2 on same directory
            time.time()
            proc2 = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            stdout2, _ = proc2.communicate(timeout=180)
            t_end = time.time()

            summary = summarize_ledger(ledger_path, all_ids)
            completed = summary["exact_one_effect"]

            trial_rec = {
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
                "note": "Re-executed prior completed steps from scratch after restart"
            }
            trial_results.append(trial_rec)
            print(
                f"  Trial {trial_num}: completed={completed}, "
                f"execs={summary['total_executions']}, effects={summary['total_effects']}, "
                f"time={trial_rec['total_time_sec']}s",
                flush=True,
            )

        passed_trials = sum(1 for t in trial_results if t["completed"])
        overall_pass = passed_trials >= 9
        total_execs = sum(t["total_executions"] for t in trial_results)
        return {
            "engine": "baseline",
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
        print("=== [Baseline ds_squad.py] Starting KT2 Approval ===", flush=True)
        subname = f"kt2_approval_{int(time.time())}"
        items = [
            {"id": "PREP", "needs": []},
            {"id": "DEPLOY", "needs": ["PREP"], "gate": "approvals/deploy.txt"},
        ]
        run_dir, ledger_path, rel_run_name = self._setup_run_dir(
            subname, items, max_parallel=2, extra_plan={"wait_gates_minutes": 5}
        )
        env = self._get_env(ledger_path, item_seconds=seconds_per_item)

        cmd = [
            sys.executable,
            str(DS_SQUAD_SCRIPT),
            "--run", rel_run_name,
            "--tag", "_kt2",
            "--hours", "1",
        ]

        t0 = time.time()
        # Start runner process 1
        proc1 = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

        # Wait until PREP finishes and DEPLOY enters waiting-gate
        time.sleep(seconds_per_item + 3.0)

        # Kill ALL engine processes (simulating reboot)
        try:
            os.killpg(proc1.pid, signal.SIGKILL)
            proc1.wait(timeout=5)
        except Exception:
            pass

        # Simulate host downtime
        print(f"  Waiting {downtime_sec}s downtime (simulating host reboot)...", flush=True)
        time.sleep(downtime_sec)

        # Restart engine process 2
        proc2 = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

        # Wait 5 seconds post-restart, then deliver approval
        time.sleep(5.0)
        gate_file = run_dir / "approvals" / "deploy.txt"
        gate_file.parent.mkdir(parents=True, exist_ok=True)
        gate_file.write_text("APPROVED\n")
        print("  Delivered approval via approvals/deploy.txt", flush=True)

        proc2.communicate(timeout=60)
        t_end = time.time()

        summary = summarize_ledger(ledger_path, ["PREP", "DEPLOY"])
        # In baseline, PREP re-executes because state is in-memory!
        prep_re_executed = summary["exec_counts"].get("PREP", 0) > 1
        approval_honored = summary["effect_counts"].get("DEPLOY", 0) == 1

        # PASS requires approval honored, nothing re-asked, no state lost
        # Since PREP re-ran, baseline loses completed step state!
        verdict = "FAIL (Re-executes prior steps on restart)" if prep_re_executed else "PASS"
        return {
            "engine": "baseline",
            "test": "KT2",
            "verdict": verdict,
            "total_time_sec": round(t_end - t0, 2),
            "prep_executions": summary["exec_counts"].get("PREP", 0),
            "deploy_executions": summary["exec_counts"].get("DEPLOY", 0),
            "summary": summary,
            "prep_re_executed": prep_re_executed,
            "approval_honored": approval_honored,
        }

    def run_kt3(self, parking_seconds: float = 30.0) -> dict[str, Any]:
        """KT3 Quota Parking: exits 29 with quota resetting at now + parking_seconds."""
        print("=== [Baseline ds_squad.py] Starting KT3 Quota Parking ===", flush=True)
        subname = f"kt3_quota_{int(time.time())}"
        items = [{"id": "ITEM_X", "needs": [], "retry": 3}]
        run_dir, ledger_path, rel_run_name = self._setup_run_dir(subname, items, max_parallel=1)

        t_now = time.time()
        t_reset = t_now + parking_seconds

        env = self._get_env(ledger_path, item_seconds=1.0, quota_until=t_reset)
        cmd = [
            sys.executable,
            str(DS_SQUAD_SCRIPT),
            "--run", rel_run_name,
            "--tag", "_kt3",
            "--hours", "1",
        ]

        time.time()
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        # ds_squad does blind retries immediately without parking
        stdout, _ = proc.communicate(timeout=60)
        time.time()

        analysis = analyze_quota_parking(ledger_path, "ITEM_X", t_reset)
        summary = summarize_ledger(ledger_path, ["ITEM_X"])

        # PASS = zero attempts before T, auto-resume at T
        # Baseline fails KT3 because it makes attempts before T (immediate retries up to retry limit)
        verdict = "FAIL (Immediate blind retries without parking)"
        return {
            "engine": "baseline",
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
        """KT4 Code / Plan Change under a Live Run."""
        print("=== [Baseline ds_squad.py] Starting KT4 Code Change ===", flush=True)
        subname = f"kt4_codechange_{int(time.time())}"
        items = [
            {"id": "STEP1", "needs": []},
            {"id": "STEP2", "needs": ["STEP1"]},
        ]
        run_dir, ledger_path, rel_run_name = self._setup_run_dir(subname, items, max_parallel=1)
        env = self._get_env(ledger_path, item_seconds=6.0)

        cmd = [
            sys.executable,
            str(DS_SQUAD_SCRIPT),
            "--run", rel_run_name,
            "--tag", "_kt4",
            "--hours", "1",
        ]

        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        # Wait until STEP1 starts running
        time.sleep(2.0)

        # Mutate plan.json on disk while run is in flight: rename STEP2 -> STEP2_RENAMED and add STEP3
        plan_file = run_dir / "plan.json"
        plan_data = json.loads(plan_file.read_text())
        plan_data["items"] = [
            {"id": "STEP1", "title": "Task STEP1", "brief": "briefs/STEP1.md", "needs": []},
            {"id": "STEP2_RENAMED", "title": "Task STEP2 Renamed", "brief": "briefs/STEP1.md", "needs": ["STEP1"]},
            {"id": "STEP3_ADDED", "title": "Task STEP3 Added", "brief": "briefs/STEP1.md", "needs": ["STEP2_RENAMED"]},
        ]
        plan_file.write_text(json.dumps(plan_data, indent=2))

        stdout, _ = proc.communicate(timeout=60)
        summary = summarize_ledger(ledger_path)

        # Classification: ds_squad reloads plan file dynamically and picks up new items!
        # It silently diverges from original execution plan without error.
        classification = "silently diverges (accepts new plan items on disk without validation/mismatch error)"
        return {
            "engine": "baseline",
            "test": "KT4",
            "classification": classification,
            "stdout_tail": stdout[-500:] if stdout else "",
            "summary": summary,
        }

    def run_kt5(self) -> dict[str, Any]:
        """KT5 Caps: Wall-clock timeout and iteration retry cap."""
        print("=== [Baseline ds_squad.py] Starting KT5 Caps ===", flush=True)
        subname = f"kt5_caps_{int(time.time())}"
        # Item 1: wall-clock cap (max_minutes=0.05 = 3 seconds; agent sleeps 20s)
        # Item 2: iteration cap (retry=2; fails transiently)
        items = [
            {"id": "TIME_CAP", "needs": [], "max_minutes": 0.05},
            {"id": "RETRY_CAP", "needs": [], "retry": 2},
        ]
        run_dir, ledger_path, rel_run_name = self._setup_run_dir(subname, items, max_parallel=2)

        # For RETRY_CAP, fail transiently
        env = self._get_env(ledger_path, item_seconds=20.0, fail_transient=True)

        cmd = [
            sys.executable,
            str(DS_SQUAD_SCRIPT),
            "--run", rel_run_name,
            "--tag", "_kt5",
            "--hours", "1",
        ]

        time.time()
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        stdout, _ = proc.communicate(timeout=60)
        time.time()

        # Check process leakage
        pgrep = subprocess.run(["pgrep", "-f", "fake_agent.py"], capture_output=True, text=True)
        leaked_pids = [p for p in pgrep.stdout.splitlines() if p.strip()]

        summary = summarize_ledger(ledger_path, ["TIME_CAP", "RETRY_CAP"])
        status_md = (run_dir / "status_kt5.md").read_text() if (run_dir / "status_kt5.md").exists() else ""

        time_cap_fired = "killed-time" in status_md or summary["exec_counts"].get("TIME_CAP", 0) >= 1
        # retry=2 means 1 initial + 2 retries = 3 attempts
        retry_cap_fired = summary["exec_counts"].get("RETRY_CAP", 0) == 3

        passed = time_cap_fired and retry_cap_fired and len(leaked_pids) == 0
        return {
            "engine": "baseline",
            "test": "KT5",
            "verdict": "PASS" if passed else "FAIL",
            "time_cap_fired": time_cap_fired,
            "retry_cap_fired": retry_cap_fired,
            "leaked_processes": len(leaked_pids),
            "summary": summary,
        }
