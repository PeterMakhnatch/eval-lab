#!/usr/bin/env python3
"""KT2 Approval Across Restart Test for Dagu.

Protocol:
- One item requires a human approval.
- Kill ALL engine processes (server and worker; this simulates a reboot) while it waits.
- Restart them, and deliver the approval at least 60 s later.
- Where the engine offers a time-skipping test environment or a durable sleep, also simulate an 8-hour wait.
- PASS = the item completes with the approval honored, nothing re-asked, no state lost.
"""

import json
import os
import signal
import subprocess
import time
from pathlib import Path

DAGU_BIN = "/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F8/dagu/dagu"
RESULTS_DIR = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F8/dagu")
RECEIPTS_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F8-DAGU-TRIAL/receipts/kt2")
RESULTS_KT2_DIR = RESULTS_DIR / "kt2"


def get_env():
    env = os.environ.copy()
    env["DAGU_HOME"] = str(RESULTS_DIR / "home")
    env["DAGU_CONFIG"] = str(RESULTS_DIR / "home/config.yaml")
    return env


def parse_ledger(ledger_path: Path):
    counts = {}
    if not ledger_path.exists():
        return counts
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                key = (row.get("id"), row.get("kind"))
                counts[key] = counts.get(key, 0) + 1
            except Exception:
                pass
    return counts


def stop_dagu_services():
    """Kill all dagu processes (server, scheduler, runner) to simulate a complete machine reboot."""
    print("Stopping all Dagu engine processes (simulating reboot)...")
    # Kill any running dagu binary processes
    try:
        pids = subprocess.check_output(["pgrep", "-f", DAGU_BIN]).decode().splitlines()
        for pid_str in pids:
            try:
                pid = int(pid_str)
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
    except subprocess.CalledProcessError:
        pass
    time.sleep(2)
    # Verify no dagu processes remain
    res = subprocess.run(["pgrep", "-f", DAGU_BIN], capture_output=True, text=True)
    running_pids = res.stdout.strip().splitlines()
    print(f"Remaining dagu processes after kill: {running_pids}")
    return len(running_pids) == 0


def start_dagu_services():
    """Restart Dagu engine (server + scheduler) in background."""
    print("Restarting Dagu engine (dagu start-all)...")
    env = get_env()
    env["DAGU_COORDINATOR_ENABLED"] = "false"
    cmd = [DAGU_BIN, "start-all", "--port", "18081"]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    # Verify port 18081 responds
    for _ in range(10):
        try:
            res = subprocess.run(["curl", "-s", "http://127.0.0.1:18081/api/v1/dags"], capture_output=True, text=True, timeout=2)
            if res.returncode == 0 and "kt2_approval" in res.stdout:
                print("Dagu engine restarted successfully and API is responsive.")
                return proc
        except Exception:
            time.sleep(1)
    print("Warning: API readiness check timed out.")
    return proc


def main():
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_KT2_DIR.mkdir(parents=True, exist_ok=True)

    ledger_path = RECEIPTS_DIR / "ledger.jsonl"
    report_dir = RECEIPTS_DIR / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    if ledger_path.exists():
        ledger_path.unlink()

    run_id = f"kt2_run_{int(time.time())}"
    env = get_env()

    print(f"=== Starting KT2 Approval Across Restart (run_id: {run_id}) ===")
    t0 = time.time()

    # Step 1: Start the DAG
    print("[T+0s] Starting DAG kt2_approval...")
    cmd = [
        DAGU_BIN, "start",
        f"--run-id={run_id}",
        "kt2_approval",
        "--",
        f"LEDGER={ledger_path}",
        f"REPORT_DIR={report_dir}",
    ]
    start_res = subprocess.run(cmd, env=env, capture_output=True, text=True)
    print("Initial run output:\n", start_res.stdout[:400])

    # Verify run entered Waiting state
    st1 = subprocess.run([DAGU_BIN, "status", f"--run-id={run_id}", "kt2_approval"], env=env, capture_output=True, text=True)
    print("Status before kill:\n", st1.stdout[:300])
    is_waiting_before = "waiting" in (st1.stdout + st1.stderr).lower()
    print(f"Is waiting on human task before kill: {is_waiting_before}")

    # Check ledger for PREP completion
    counts_before = parse_ledger(ledger_path)
    prep_done_before = counts_before.get(("PREP", "effect"), 0) == 1
    print(f"PREP completed before kill: {prep_done_before}")

    # Step 2: Kill ALL engine processes to simulate a reboot
    stop_dagu_services()

    # Wait 65 seconds while all engine processes are dead (simulating reboot downtime)
    dead_wait_s = 65
    print(f"Sleeping {dead_wait_s} seconds with ALL engine processes dead (simulating host downtime / reboot)...")
    time.sleep(dead_wait_s)

    # Step 3: Restart engine processes
    start_dagu_services()

    # Verify state after engine restart
    st2 = subprocess.run([DAGU_BIN, "status", f"--run-id={run_id}", "kt2_approval"], env=env, capture_output=True, text=True)
    print("Status after engine restart:\n", st2.stdout[:300])
    is_waiting_after_restart = "waiting" in (st2.stdout + st2.stderr).lower()
    print(f"Is STILL waiting on human task after restart: {is_waiting_after_restart}")

    # Step 4: Protocol requires delivering approval AT LEAST 60 s after restart!
    approval_wait_s = 65
    print(f"Waiting {approval_wait_s} seconds after engine restart before delivering approval...")
    time.sleep(approval_wait_s)

    # Step 5: Deliver the approval
    print(f"[T+{time.time()-t0:.1f}s] Delivering human approval (decision=approved)...")
    comp = subprocess.run(
        [DAGU_BIN, "human-task", "complete", f"--run-id={run_id}", "--step=gate", "--input", "decision=approved", "kt2_approval"],
        env=env,
        capture_output=True,
        text=True,
    )
    print("Complete output:", comp.stdout)
    if comp.stderr:
        print("Complete stderr:", comp.stderr)

    # Step 6: Wait for downstream step (deploy) to execute and complete
    print("Waiting for downstream DEPLOY step to finish...")
    completed = False
    for i in range(30):
        time.sleep(1)
        st3 = subprocess.run([DAGU_BIN, "status", f"--run-id={run_id}", "kt2_approval"], env=env, capture_output=True, text=True)
        out = (st3.stdout + st3.stderr).lower()
        lines = [line_str.strip().lower() for line_str in out.splitlines() if line_str.strip()]
        if any("result: succeeded" in line_str for line_str in lines):
            completed = True
            print(f"Run completed in Succeeded state after {i+1}s!")
            break
        elif "failed" in out or "problem" in out:
            print(f"Run ended in failure: {out}")
            break

    total_time = time.time() - t0
    final_counts = parse_ledger(ledger_path)
    print(f"Final ledger counts: {final_counts}")

    prep_execs = final_counts.get(("PREP", "exec"), 0)
    prep_effects = final_counts.get(("PREP", "effect"), 0)
    deploy_execs = final_counts.get(("DEPLOY", "exec"), 0)
    deploy_effects = final_counts.get(("DEPLOY", "effect"), 0)

    # PASS requirements:
    # 1. completed == True
    # 2. approval honored (deploy step ran)
    # 3. nothing re-asked (prep not re-executed: prep_execs == 1, prep_effects == 1)
    # 4. no state lost (deploy_effects == 1)
    pass_kt2 = (
        completed
        and is_waiting_before
        and is_waiting_after_restart
        and prep_execs == 1
        and prep_effects == 1
        and deploy_execs == 1
        and deploy_effects == 1
    )

    result = {
        "test": "KT2 Approval across restart",
        "engine": "Dagu",
        "run_id": run_id,
        "completed": completed,
        "is_waiting_before_kill": is_waiting_before,
        "is_waiting_after_restart": is_waiting_after_restart,
        "dead_wait_duration_s": dead_wait_s,
        "post_restart_approval_wait_s": approval_wait_s,
        "total_duration_s": round(total_time, 2),
        "ledger_counts": {f"{k[0]}_{k[1]}": v for k, v in final_counts.items()},
        "pass_verdict": pass_kt2,
        "time_skipping_support": "None (Dagu is a compiled Go binary without time-skipping test harness; tested with >60s downtime + >60s post-restart durable wait; disk state has no TTL and persists indefinitely)",
    }

    with open(RECEIPTS_DIR / "result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    with open(RESULTS_KT2_DIR / "result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    if ledger_path.exists():
        with open(ledger_path, encoding="utf-8") as src, open(RESULTS_KT2_DIR / "ledger.jsonl", "w", encoding="utf-8") as dst:
            dst.write(src.read())

    print("\n================== KT2 OVERALL SUMMARY ==================")
    print(f"Pass verdict: {pass_kt2}")
    print(f"Completed: {completed}")
    print(f"PREP execs/effects: {prep_execs}/{prep_effects} (expected 1/1, not re-run)")
    print(f"DEPLOY execs/effects: {deploy_execs}/{deploy_effects} (expected 1/1)")
    print(f"Total time: {total_time:.1f}s")


if __name__ == "__main__":
    main()
