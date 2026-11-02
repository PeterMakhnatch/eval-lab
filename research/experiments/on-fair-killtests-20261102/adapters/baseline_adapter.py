#!/usr/bin/env python3
"""baseline_adapter.py: Adapter for executing kill tests against baseline runners.

Supports:
- Baseline Original: ds_squad.py (in-memory state, no adoption, no parking)
- Baseline R7: ds_squad_v2.py (atomic state.json, worker adoption, quota parking)
"""
from __future__ import annotations

import json
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
BRIDGE_ROOT = Path.home() / "Developer/research-context/bridge"
ORIGINAL_SCRIPT = BRIDGE_ROOT / "ds_squad.py"
HARDENED_SCRIPT = BRIDGE_ROOT / "ds_squad_v2.py"

sys.path.insert(0, str(EXPERIMENT_DIR))
from ledger_reader import analyze_kt1_ledger, analyze_kt3_ledger, read_ledger  # noqa: E402


class BaselineAdapter:
    """Manages baseline squad runner executions (original and R7 hardened)."""

    def __init__(self, mode: str = "hardened", scratch_dir: Path | None = None) -> None:
        assert mode in ("original", "hardened")
        self.mode = mode
        self.script_path = HARDENED_SCRIPT if mode == "hardened" else ORIGINAL_SCRIPT
        self.scratch_dir = (
            scratch_dir
            or (EXPERIMENT_DIR / "scratch" / f"baseline_{mode}")
        ).resolve()
        self.scratch_dir.mkdir(parents=True, exist_ok=True)

    def _setup_run_dir(
        self,
        subname: str,
        items: list[dict[str, Any]],
        max_parallel: int = 2,
    ) -> tuple[Path, Path, str]:
        run_dir = self.scratch_dir / subname
        run_dir.mkdir(parents=True, exist_ok=True)
        rel_run_name = str(run_dir.relative_to(BRIDGE_ROOT)) if run_dir.is_relative_to(BRIDGE_ROOT) else str(run_dir)

        (run_dir / "common.md").write_text("# Shared Context\n\nKill-test baseline run.\n")
        (run_dir / "worker.yml").write_text("model: fake-model\n")
        briefs_dir = run_dir / "briefs"
        briefs_dir.mkdir(exist_ok=True)

        plan_items = []
        for it in items:
            iid = it["id"]
            (briefs_dir / f"{iid}.md").write_text(f"# YOUR ITEM: {iid}\nExecute fake agent item {iid}.\n")
            plan_items.append({
                "id": iid,
                "title": it.get("title", f"Task {iid}"),
                "brief": f"briefs/{iid}.md",
                "thinking": "low",
                "max_minutes": it.get("max_minutes", 5.0),
                "max_usd": it.get("max_usd", 2.0),
                "needs": it.get("needs", []),
                "gate": it.get("gate"),
                "worktree": False,
                "retry": it.get("retry", 0),
            })

        ledger_path = run_dir / "ledger.jsonl"
        plan_data = {
            "model": "fake-model",
            "budget_usd": 100.0,
            "max_parallel": max_parallel,
            "subscription": True,
            "dry_ledger": True,
            "wait_gates_minutes": 10,
            "ledger_path": str(ledger_path),
            "items": plan_items,
        }
        (run_dir / "plan.json").write_text(json.dumps(plan_data, indent=2))
        return run_dir, ledger_path, rel_run_name

    def _get_env(
        self,
        ledger_path: Path,
        item_seconds: float = 20.0,
        mode: str = "guarded",
        quota_until: float | None = None,
        fail_transient: bool = False,
        hang_silent: bool = False,
    ) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = f"{ADAPTERS_DIR}:{env.get('PATH', '')}"
        env["FAKE_AGENT_LEDGER"] = str(ledger_path)
        env["FAKE_AGENT_SECONDS"] = str(item_seconds)
        env["FAKE_AGENT_MODE"] = mode
        if quota_until is not None:
            env["FAKE_AGENT_QUOTA_UNTIL"] = str(quota_until)
        else:
            env.pop("FAKE_AGENT_QUOTA_UNTIL", None)
        if fail_transient:
            env["FAKE_AGENT_FAIL_TRANSIENT"] = "1"
        else:
            env.pop("FAKE_AGENT_FAIL_TRANSIENT", None)
        if hang_silent:
            env["FAKE_AGENT_HANG_SILENT"] = "1"
        else:
            env.pop("FAKE_AGENT_HANG_SILENT", None)
        return env

    def run_kt1_trial(
        self,
        trial_num: int,
        item_seconds: float = 20.0,
        fixture_mode: str = "guarded",
        orphan_scenario: str = "O1",  # O1: orphans survive; O2: orphans die
    ) -> dict[str, Any]:
        """Matched event-driven KT1 trial."""
        subname = f"kt1_trial_{trial_num}_{fixture_mode}_{orphan_scenario}_{int(time.time()*1000)}"
        items = [
            {"id": "A1", "needs": []},
            {"id": "A2", "needs": ["A1"]},
            {"id": "A3", "needs": ["A2"]},
            {"id": "B1", "needs": []},
            {"id": "B2", "needs": ["B1"]},
            {"id": "B3", "needs": ["B2"]},
        ]
        all_ids = ["A1", "A2", "A3", "B1", "B2", "B3"]
        run_dir, ledger_path, _ = self._setup_run_dir(subname, items, max_parallel=2)
        env = self._get_env(ledger_path, item_seconds=item_seconds, mode=fixture_mode)

        cmd = [sys.executable, str(self.script_path), "--run", str(run_dir)]
        t0 = time.time()
        proc1 = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        # Event-driven trigger: poll ledger until >=1 completed AND >=1 in flight
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

        # Execute kill
        try:
            if orphan_scenario == "O2":
                # Kill runner and process group
                os.killpg(proc1.pid, signal.SIGKILL)
            else:
                # O1: Kill only the runner process; child agents survive as orphans
                os.kill(proc1.pid, signal.SIGKILL)
            proc1.wait(timeout=2)
        except Exception:
            pass

        # Downtime / recovery gap
        time.sleep(1.0)

        # Restart runner
        proc2 = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        # Wait for workflow completion (up to 2 * total duration)
        try:
            proc2.wait(timeout=item_seconds * 4)
        except subprocess.TimeoutExpired:
            os.killpg(proc2.pid, signal.SIGKILL)
            proc2.wait(timeout=2)

        t_end = time.time()
        analysis = analyze_kt1_ledger(ledger_path, all_ids, t_kill)
        analysis["trial"] = trial_num
        analysis["engine"] = f"baseline_{self.mode}"
        analysis["fixture_mode"] = fixture_mode
        analysis["orphan_scenario"] = orphan_scenario
        analysis["total_wall_sec"] = round(t_end - t0, 2)
        analysis["recovery_wall_sec"] = round(t_end - t_kill, 2)
        return analysis

    def run_kt2_trial(self, trial_num: int) -> dict[str, Any]:
        """KT2 Human approval survives reboot with 60s+ downtime."""
        subname = f"kt2_trial_{trial_num}_{int(time.time()*1000)}"
        run_dir = self.scratch_dir / subname
        run_dir.mkdir(parents=True, exist_ok=True)
        gate_file = run_dir / "approval.gate"

        items = [
            {"id": "PREP", "needs": []},
            {"id": "DEPLOY", "needs": ["PREP"], "gate": str(gate_file)},
        ]
        run_dir, ledger_path, _ = self._setup_run_dir(subname, items, max_parallel=1)
        env = self._get_env(ledger_path, item_seconds=2.0)

        cmd = [sys.executable, str(self.script_path), "--run", str(run_dir)]
        t0 = time.time()
        proc1 = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        # Wait until PREP completes
        deadline = time.time() + 10.0
        while time.time() < deadline:
            time.sleep(0.2)
        # Kill runner cleanly
        os.kill(proc1.pid, signal.SIGTERM)
        try:
            proc1.wait(timeout=2)
        except Exception:
            os.kill(proc1.pid, signal.SIGKILL)
        time.sleep(62.0)

        # Restart runner
        proc2 = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        # Spec: approval delivered at least 60 seconds after restart
        time.sleep(62.0)
        gate_file.write_text("APPROVED\n", encoding="utf-8")

        proc2.wait(timeout=30.0)
        t_end = time.time()

        rows = read_ledger(ledger_path)
        prep_starts = sum(1 for r in rows if r.get("kind") == "start" and r.get("id") == "PREP")
        deploy_starts = sum(1 for r in rows if r.get("kind") == "start" and r.get("id") == "DEPLOY")
        deploy_effects = sum(1 for r in rows if r.get("kind") == "effect" and r.get("id") == "DEPLOY")

        # In hardened baseline: PREP stays completed (starts == 1). In original baseline: PREP restarts (starts == 2).
        passed = (prep_starts == 1) and (deploy_starts == 1) and (deploy_effects == 1)

        return {
            "trial": trial_num,
            "engine": f"baseline_{self.mode}",
            "passed": passed,
            "prep_starts": prep_starts,
            "deploy_starts": deploy_starts,
            "deploy_effects": deploy_effects,
            "total_wall_sec": round(t_end - t0, 2),
        }

    def run_kt3_trial(self, trial_num: int, parking_seconds: float = 90.0) -> dict[str, Any]:
        """KT3 Quota park until T=now+90s with auto-resume."""
        subname = f"kt3_trial_{trial_num}_{int(time.time()*1000)}"
        t_reset = time.time() + parking_seconds
        items = [
            {"id": "QUOTA_ITEM", "needs": [], "retry": 3},
        ]
        run_dir, ledger_path, _ = self._setup_run_dir(subname, items, max_parallel=1)
        env = self._get_env(ledger_path, item_seconds=1.0, quota_until=t_reset)

        cmd = [sys.executable, str(self.script_path), "--run", str(run_dir)]
        t0 = time.time()
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        # Wait for completion (parking_seconds + small buffer)
        try:
            proc.wait(timeout=parking_seconds + 30.0)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=2)

        t_end = time.time()
        analysis = analyze_kt3_ledger(ledger_path, t_reset)
        analysis["trial"] = trial_num
        analysis["engine"] = f"baseline_{self.mode}"
        analysis["total_wall_sec"] = round(t_end - t0, 2)
        analysis["t_reset"] = t_reset
        return analysis

    def run_kt5_caps(self) -> dict[str, Any]:
        """KT5 Caps enforcement & verified status-row predicates."""
        subname = f"kt5_caps_{int(time.time()*1000)}"
        items = [
            {"id": "TIME_CAP", "needs": [], "max_minutes": 0.05},  # 3.0 seconds
            {"id": "RETRY_CAP", "needs": [], "retry": 2},          # max 3 attempts
        ]
        run_dir, ledger_path, _ = self._setup_run_dir(subname, items, max_parallel=2)
        env = self._get_env(ledger_path, item_seconds=30.0, fail_transient=True)

        cmd = [sys.executable, str(self.script_path), "--run", str(run_dir)]
        t0 = time.time()
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            proc.wait(timeout=25.0)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=2)

        t_end = time.time()

        # Read status from engine's persistent state
        status_rows: dict[str, Any] = {}
        if self.mode == "hardened":
            state_file = run_dir / "state.json"
            if not state_file.exists():
                state_file = run_dir / "RUN" / "state.json"
            if state_file.exists():
                try:
                    st_data = json.loads(state_file.read_text())
                    items_field = st_data.get("items", {})
                    if isinstance(items_field, dict):
                        for iid, it in items_field.items():
                            status_rows[iid] = it.get("status")
                    elif isinstance(items_field, list):
                        for it in items_field:
                            status_rows[it["id"]] = it.get("status")
                except Exception:
                    pass

        rows = read_ledger(ledger_path)
        time_cap_execs = sum(1 for r in rows if r.get("kind") == "start" and r.get("id") == "TIME_CAP")
        retry_cap_execs = sum(1 for r in rows if r.get("kind") == "start" and r.get("id") == "RETRY_CAP")

        time_cap_fired = (time_cap_execs == 1) and (status_rows.get("TIME_CAP") == "killed-time" if self.mode == "hardened" else True)
        retry_cap_fired = (retry_cap_execs == 3) and (status_rows.get("RETRY_CAP") == "failed" if self.mode == "hardened" else True)

        return {
            "engine": f"baseline_{self.mode}",
            "time_cap_fired": time_cap_fired,
            "retry_cap_fired": retry_cap_fired,
            "status_rows": status_rows,
            "time_cap_execs": time_cap_execs,
            "retry_cap_execs": retry_cap_execs,
            "total_wall_sec": round(t_end - t0, 2),
            "passed": time_cap_fired and retry_cap_fired,
        }

    def run_kt6_hang(self, cap_seconds: float = 4.0) -> dict[str, Any]:
        """KT6 Hang of live child (0% CPU, silent stall)."""
        subname = f"kt6_hang_{int(time.time()*1000)}"
        items = [
            {"id": "HANG_ITEM", "needs": [], "max_minutes": cap_seconds / 60.0},
        ]
        run_dir, ledger_path, _ = self._setup_run_dir(subname, items, max_parallel=1)
        env = self._get_env(ledger_path, item_seconds=60.0, hang_silent=True)

        cmd = [sys.executable, str(self.script_path), "--run", str(run_dir)]
        t0 = time.time()
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        # The runner should terminate the child around cap_seconds
        try:
            proc.wait(timeout=cap_seconds + 10.0)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=2)

        t_end = time.time()
        elapsed = round(t_end - t0, 2)

        # Check if hang was caught by runner wall-clock cap
        status_row = None
        if self.mode == "hardened":
            state_file = run_dir / "RUN" / "state.json"
            if state_file.exists():
                try:
                    st_data = json.loads(state_file.read_text())
                    for it in st_data.get("items", []):
                        if it["id"] == "HANG_ITEM":
                            status_row = it.get("status")
                except Exception:
                    pass

        caught = elapsed < (cap_seconds + 5.0)
        return {
            "engine": f"baseline_{self.mode}",
            "elapsed_sec": elapsed,
            "cap_sec": cap_seconds,
            "status_row": status_row,
            "mechanism": "runner_wall_clock_cap",
            "heartbeat_loop_caught": False,  # Heartbeat checking PID only does not catch it
            "caught": caught,
        }
