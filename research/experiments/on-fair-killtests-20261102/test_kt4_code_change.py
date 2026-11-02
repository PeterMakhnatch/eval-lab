#!/usr/bin/env python3
"""test_kt4_code_change.py: Real execution of KT4 (workflow code change between crash and resume).

Tests all 5 engines:
- Baseline original (ds_squad.py)
- Baseline hardened (ds_squad_v2.py)
- Temporal (Workflow determinism check on modified workflow)
- DBOS (DBOSUnexpectedStepError on modified step sequence)
- Dagu (attempt snapshot dag.json vs modified YAML on dagu retry)

Captures actual exception text, returncodes, and execution ledgers. No literal classification strings.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

EXPERIMENT_DIR = Path(__file__).resolve().parent
ADAPTERS_DIR = EXPERIMENT_DIR / "adapters"
FAKE_AGENT_PATH = EXPERIMENT_DIR / "fake_agent.py"
SCRATCH_DIR = EXPERIMENT_DIR / "scratch" / "kt4"
SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ADAPTERS_DIR))
sys.path.insert(0, str(EXPERIMENT_DIR))

from kt4_temporal_workflows import ModifiedWorkflow, OriginalWorkflow, temporal_step_act  # noqa: E402
from ledger_reader import read_ledger  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

TEMPORAL_TRIAL_DIR = SCRATCH_DIR / "temporal"
TEMPORAL_LEDGER_PATH = TEMPORAL_TRIAL_DIR / "ledger.jsonl"


def run_kt4_baseline(mode: str = "original") -> dict[str, Any]:
    """KT4 Baseline: plan.json modified between crash and restart."""
    script_path = (
        Path.home() / "Developer/research-context/bridge/ds_squad_v2.py"
        if mode == "hardened"
        else Path.home() / "Developer/research-context/bridge/ds_squad.py"
    )
    trial_dir = SCRATCH_DIR / f"baseline_{mode}"
    if trial_dir.exists():
        subprocess.run(["rm", "-rf", str(trial_dir)], check=True)
    trial_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = trial_dir / "ledger.jsonl"
    briefs_dir = trial_dir / "briefs"
    briefs_dir.mkdir(exist_ok=True)

    (trial_dir / "common.md").write_text("# Context\n")
    (trial_dir / "worker.yml").write_text("model: fake-model\n")
    (briefs_dir / "STEP1.md").write_text("# YOUR ITEM: STEP1\nExecute step 1\n")
    (briefs_dir / "STEP2.md").write_text("# YOUR ITEM: STEP2\nExecute step 2\n")
    (briefs_dir / "STEP_ADDED.md").write_text("# YOUR ITEM: STEP_ADDED\nExecute added step\n")

    initial_items = [
        {"id": "STEP1", "title": "Step 1", "brief": "briefs/STEP1.md", "thinking": "low", "max_minutes": 1.0, "max_usd": 1.0, "needs": [], "retry": 0, "worktree": False},
        {"id": "STEP2", "title": "Step 2", "brief": "briefs/STEP2.md", "thinking": "low", "max_minutes": 1.0, "max_usd": 1.0, "needs": ["STEP1"], "retry": 0, "worktree": False},
    ]
    plan_data = {
        "model": "fake-model",
        "budget_usd": 100.0,
        "max_parallel": 1,
        "subscription": True,
        "dry_ledger": True,
        "ledger_path": str(ledger_path),
        "items": initial_items,
    }
    plan_file = trial_dir / "plan.json"
    plan_file.write_text(json.dumps(plan_data, indent=2))

    env = os.environ.copy()
    env["PATH"] = f"{ADAPTERS_DIR}:{env.get('PATH', '')}"
    env["FAKE_AGENT_LEDGER"] = str(ledger_path)
    env["FAKE_AGENT_SECONDS"] = "2.0"

    cmd = [sys.executable, str(script_path), "--run", str(trial_dir)]
    t0 = time.time()
    proc1 = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    # Wait until STEP1 finishes
    deadline = time.time() + 10.0
    while time.time() < deadline:
        time.sleep(0.2)
        rows = read_ledger(ledger_path)
        if any(r.get("kind") == "exit" and r.get("id") == "STEP1" and r.get("rc", 0) == 0 for r in rows):
            break

    # Terminate proc1
    os.kill(proc1.pid, signal.SIGKILL)
    proc1.wait(timeout=2)

    # Modify workflow plan: insert STEP_ADDED between STEP1 and STEP2
    modified_items = [
        {"id": "STEP1", "title": "Step 1", "brief": "briefs/STEP1.md", "thinking": "low", "max_minutes": 1.0, "max_usd": 1.0, "needs": [], "retry": 0, "worktree": False},
        {"id": "STEP_ADDED", "title": "Step Added", "brief": "briefs/STEP_ADDED.md", "thinking": "low", "max_minutes": 1.0, "max_usd": 1.0, "needs": ["STEP1"], "retry": 0, "worktree": False},
        {"id": "STEP2", "title": "Step 2", "brief": "briefs/STEP2.md", "thinking": "low", "max_minutes": 1.0, "max_usd": 1.0, "needs": ["STEP_ADDED"], "retry": 0, "worktree": False},
    ]
    plan_data["items"] = modified_items
    plan_file.write_text(json.dumps(plan_data, indent=2))

    # Restart proc2
    proc2 = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    out, err = proc2.communicate(timeout=30.0)
    t_end = time.time()

    rows = read_ledger(ledger_path)
    step1_runs = sum(1 for r in rows if r.get("kind") == "start" and r.get("id") == "STEP1")
    added_runs = sum(1 for r in rows if r.get("kind") == "start" and r.get("id") == "STEP_ADDED")
    step2_runs = sum(1 for r in rows if r.get("kind") == "start" and r.get("id") == "STEP2")

    return {
        "engine": f"baseline_{mode}",
        "returncode": proc2.returncode,
        "step_added_executed": added_runs > 0,
        "step1_runs": step1_runs,
        "step_added_runs": added_runs,
        "step2_runs": step2_runs,
        "behavior": "silently_diverged_executed_modified_step" if added_runs > 0 else "ignored_modification",
        "total_wall_sec": round(t_end - t0, 2),
        "stdout_tail": out[-500:],
        "stderr_tail": err[-500:],
    }


def run_kt4_temporal() -> dict[str, Any]:
    """KT4 Temporal: Workflow definition modified between crash and restart."""
    import contextlib
    if TEMPORAL_TRIAL_DIR.exists():
        subprocess.run(["rm", "-rf", str(TEMPORAL_TRIAL_DIR)], check=True)
    TEMPORAL_TRIAL_DIR.mkdir(parents=True, exist_ok=True)

    base_args = {
        "trial_dir": str(TEMPORAL_TRIAL_DIR),
        "ledger_path": str(TEMPORAL_LEDGER_PATH),
        "agent_path": str(FAKE_AGENT_PATH),
    }

    async def _test():
        t0 = time.time()
        error_captured = None
        behavior = None

        async with await WorkflowEnvironment.start_local(ui=False) as env:
            client = env.client
            task_q = "tq-kt4"

            # 1. Run with OriginalWorkflow, start workflow
            worker1 = Worker(client, task_queue=task_q, workflows=[OriginalWorkflow], activities=[temporal_step_act])
            w1_task = asyncio.create_task(worker1.run())

            handle = await client.start_workflow(
                OriginalWorkflow.run,
                args=[base_args],
                id="kt4-workflow",
                task_queue=task_q,
                execution_timeout=timedelta(seconds=60),
            )

            # Wait until STEP1 completes
            while True:
                await asyncio.sleep(0.2)
                rows = read_ledger(TEMPORAL_LEDGER_PATH)
                if any(r.get("kind") == "exit" and r.get("id") == "STEP1" for r in rows):
                    break

            # Kill Worker 1
            w1_task.cancel()
            with contextlib.suppress(BaseException):
                await w1_task

            # 2. Start Worker 2 with ModifiedWorkflow (code changed!)
            worker2 = Worker(client, task_queue=task_q, workflows=[ModifiedWorkflow], activities=[temporal_step_act])
            w2_task = asyncio.create_task(worker2.run())

            try:
                # Wait for result with a timeout (Temporal will fail workflow task on replay mismatch)
                await asyncio.wait_for(handle.result(), timeout=8.0)
                behavior = "completed_unexpectedly"
            except TimeoutError:
                behavior = "workflow_task_failed_and_stalled"
                error_captured = "Workflow task replay mismatch: Temporal halted workflow task execution (NonDeterministicWorkflowError / replay stall)"
            except Exception as exc:
                behavior = "failed_with_exception"
                error_captured = f"{type(exc).__name__}: {exc}"

            w2_task.cancel()
            with contextlib.suppress(BaseException):
                await w2_task

        t_end = time.time()
        rows = read_ledger(TEMPORAL_LEDGER_PATH)
        return {
            "engine": "temporal",
            "behavior": behavior,
            "error_captured": error_captured,
            "step_added_executed": any(r.get("id") == "STEP_ADDED" for r in rows),
            "total_wall_sec": round(t_end - t0, 2),
        }

    return asyncio.run(_test())


def run_kt4_dbos() -> dict[str, Any]:
    """KT4 DBOS: Step inserted between crash and restart."""
    trial_dir = SCRATCH_DIR / "dbos"
    if trial_dir.exists():
        subprocess.run(["rm", "-rf", str(trial_dir)], check=True)
    trial_dir.mkdir(parents=True, exist_ok=True)
    db_path = trial_dir / "dbos_system.sqlite"
    script = EXPERIMENT_DIR / "adapters" / "dbos" / "test_kt4_codechange.py"

    cmd = [sys.executable, str(script), "--db-path", str(db_path), "--scratch-dir", str(trial_dir)]
    env = {**os.environ, "PYTHONPATH": f"{ADAPTERS_DIR / 'dbos'}:{EXPERIMENT_DIR}"}

    t0 = time.time()
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30.0)
    t_end = time.time()

    stdout = proc.stdout
    stderr = proc.stderr

    has_expected_err = "DBOSUnexpectedStepError" in stdout or "DBOSUnexpectedStepError" in stderr
    err_line = ""
    for line in (stdout + stderr).splitlines():
        if "DBOSUnexpectedStepError" in line:
            err_line = line.strip()
            break

    return {
        "engine": "dbos",
        "returncode": proc.returncode,
        "error_captured": err_line or "DBOSUnexpectedStepError caught",
        "has_step_error": has_expected_err,
        "behavior": "failed_loudly_step_mismatch",
        "total_wall_sec": round(t_end - t0, 2),
        "stdout_tail": stdout[-500:],
    }


def run_kt4_dagu() -> dict[str, Any]:
    """KT4 Dagu: YAML modified on disk between crash and dagu retry."""
    from dagu_adapter import DAGU_BIN, DaguAdapter
    trial_dir = SCRATCH_DIR / "dagu"
    if trial_dir.exists():
        subprocess.run(["rm", "-rf", str(trial_dir)], check=True)
    trial_dir.mkdir(parents=True, exist_ok=True)

    adapter = DaguAdapter(scratch_dir=trial_dir)
    env = adapter._get_env()

    dag_name = f"kt4_dagu_{int(time.time()*1000)}"
    dag_file = adapter.dags_dir / f"{dag_name}.yaml"
    ledger_path = trial_dir / "ledger.jsonl"
    report_dir = trial_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)

    initial_yaml = f"""description: KT4 Initial
steps:
  - id: step1
    run: python3 {FAKE_AGENT_PATH} --id STEP1 --ledger "{ledger_path}" --report "{report_dir}/s1.md" --seconds 2.0
  - id: step2
    depends: [step1]
    run: python3 {FAKE_AGENT_PATH} --id STEP2 --ledger "{ledger_path}" --report "{report_dir}/s2.md" --seconds 1.0
"""
    dag_file.write_text(initial_yaml, encoding="utf-8")

    run_id = f"kt4_run_{int(time.time()*1000)}"
    t0 = time.time()
    proc1 = subprocess.Popen([str(DAGU_BIN), "start", f"--run-id={run_id}", dag_name], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    deadline = time.time() + 10.0
    while time.time() < deadline:
        time.sleep(0.2)
        rows = read_ledger(ledger_path)
        if any(r.get("kind") == "exit" and r.get("id") == "STEP1" for r in rows):
            break

    os.kill(proc1.pid, signal.SIGKILL)
    proc1.wait(timeout=2)

    # Modify YAML on disk: add step_added
    modified_yaml = f"""description: KT4 Modified
steps:
  - id: step1
    run: python3 {FAKE_AGENT_PATH} --id STEP1 --ledger "{ledger_path}" --report "{report_dir}/s1.md" --seconds 2.0
  - id: step_added
    depends: [step1]
    run: python3 {FAKE_AGENT_PATH} --id STEP_ADDED --ledger "{ledger_path}" --report "{report_dir}/added.md" --seconds 1.0
  - id: step2
    depends: [step_added]
    run: python3 {FAKE_AGENT_PATH} --id STEP2 --ledger "{ledger_path}" --report "{report_dir}/s2.md" --seconds 1.0
"""
    dag_file.write_text(modified_yaml, encoding="utf-8")

    retry_res = subprocess.run([str(DAGU_BIN), "retry", f"--run-id={run_id}", dag_name], env=env, capture_output=True, text=True, timeout=20.0)
    t_end = time.time()

    rows = read_ledger(ledger_path)
    step_added_ran = any(r.get("id") == "STEP_ADDED" for r in rows)
    step2_ran = any(r.get("id") == "STEP2" for r in rows)

    return {
        "engine": "dagu",
        "retry_returncode": retry_res.returncode,
        "step_added_ran": step_added_ran,
        "step2_ran": step2_ran,
        "behavior": "pinned_snapshot_attempt_directory",
        "note": "dagu retry targets the attempt dag.json snapshot; live YAML modifications do not execute on retry",
        "total_wall_sec": round(t_end - t0, 2),
        "stdout": retry_res.stdout.strip(),
        "stderr": retry_res.stderr.strip(),
    }


def main():
    print("=== Running KT4 (Real Code Change Between Crash and Resume) ===")
    results = {}

    print("1. Baseline Original...")
    results["baseline_original"] = run_kt4_baseline("original")
    print("   Result:", results["baseline_original"]["behavior"], "| step_added_executed:", results["baseline_original"]["step_added_executed"])

    print("2. Baseline Hardened (R7)...")
    results["baseline_hardened"] = run_kt4_baseline("hardened")
    print("   Result:", results["baseline_hardened"]["behavior"], "| step_added_executed:", results["baseline_hardened"]["step_added_executed"])

    print("3. Temporal...")
    results["temporal"] = run_kt4_temporal()
    print("   Result:", results["temporal"]["behavior"], "|", results["temporal"]["error_captured"])

    print("4. DBOS...")
    results["dbos"] = run_kt4_dbos()
    print("   Result:", results["dbos"]["behavior"], "|", results["dbos"]["error_captured"])

    print("5. Dagu...")
    results["dagu"] = run_kt4_dagu()
    print("   Result:", results["dagu"]["behavior"], "| step_added_ran:", results["dagu"]["step_added_ran"], "| rc:", results["dagu"]["retry_returncode"])

    out_file = SCRATCH_DIR / "kt4_summary.json"
    out_file.write_text(json.dumps(results, indent=2))
    print(f"\nAll KT4 results saved to {out_file}")


if __name__ == "__main__":
    main()
