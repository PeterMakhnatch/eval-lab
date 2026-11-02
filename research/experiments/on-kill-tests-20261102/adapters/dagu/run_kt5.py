#!/usr/bin/env python3
"""KT5 Caps Test for Dagu.

Protocol:
- One wall-clock cap and one iteration cap (stop after K executions of a retry loop) both fire
  and each leaves a ledger or status row (including for an item with an empty result).
- PASS = both fire, both leave rows, no leaked child process (pgrep -f fake_agent is empty afterwards).
"""

import json
import os
import subprocess
import time
from pathlib import Path

DAGU_BIN = "/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F8/dagu/dagu"
RESULTS_DIR = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F8/dagu")
RECEIPTS_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F8-DAGU-TRIAL/receipts/kt5")
RESULTS_KT5_DIR = RESULTS_DIR / "kt5"


def get_env():
    env = os.environ.copy()
    env["DAGU_HOME"] = str(RESULTS_DIR / "home")
    env["DAGU_CONFIG"] = str(RESULTS_DIR / "home/config.yaml")
    return env


def check_leaked_processes():
    res = subprocess.run(["pgrep", "-f", "fake_agent.py"], capture_output=True, text=True)
    pids = [p.strip() for p in res.stdout.splitlines() if p.strip()]
    return pids


def test_wallclock_cap():
    env = get_env()
    trial_receipts = RECEIPTS_DIR / "wallclock"
    trial_receipts.mkdir(parents=True, exist_ok=True)
    ledger_path = trial_receipts / "ledger.jsonl"
    report_dir = trial_receipts / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    if ledger_path.exists():
        ledger_path.unlink()

    # Step sleeps 30s, but timeout_sec is 3s
    dag_yaml = f"""description: KT5 Wallclock Cap Test
steps:
  - id: step_timeout
    timeout_sec: 3
    run: python3 /Users/petermakhnatch/Developer/eval-lab/.worktrees/on-f8-dagu-trial-20261102/research/experiments/on-dagu-trial-20261102/fake_agent.py --id TIMEOUT_TASK --ledger "{ledger_path}" --report "{report_dir}/timeout.md" --seconds 30
"""
    dag_file = RESULTS_DIR / "home/dags/kt5_wallclock.yaml"
    with open(dag_file, "w") as f:
        f.write(dag_yaml)

    run_id = f"kt5_wallclock_{int(time.time())}"
    print("\n--- Testing KT5 Wall-Clock Cap (timeout_sec: 3, agent sleep: 30s) ---")
    t0 = time.time()
    subprocess.run([DAGU_BIN, "start", f"--run-id={run_id}", "kt5_wallclock"], env=env, capture_output=True, text=True)
    elapsed = time.time() - t0

    # Wait a moment to ensure OS process cleanup
    time.sleep(1)
    leaked = check_leaked_processes()

    # Status check
    st = subprocess.run([DAGU_BIN, "status", f"--run-id={run_id}", "kt5_wallclock"], env=env, capture_output=True, text=True)
    status_text = st.stdout + st.stderr
    cap_fired = "timed out" in status_text.lower() or "failed" in status_text.lower()

    # Read ledger rows
    ledger_rows = []
    if ledger_path.exists():
        with open(ledger_path) as f:
            ledger_rows = [json.loads(line) for line in f if line.strip()]

    # Check status.jsonl
    status_jsonl_found = False
    pattern = list((RESULTS_DIR / "home/data/dag-runs/kt5_wallclock/dag-runs").glob(f"**/*{run_id}*/**/status.jsonl"))
    if pattern:
        status_jsonl_found = True
        with open(pattern[-1]) as f:
            lines = [line_str.strip() for line_str in f if line_str.strip()]
            if lines:
                json.loads(lines[-1])

    print("Wallclock cap results:")
    print(f"  Elapsed: {elapsed:.2f}s (cap fired: {cap_fired})")
    print(f"  Ledger rows: {len(ledger_rows)} (exec recorded, effect NOT recorded)")
    print(f"  Status row recorded: {status_jsonl_found}")
    print(f"  Leaked fake_agent processes: {leaked}")

    return {
        "cap_type": "wall_clock",
        "configured_timeout_sec": 3,
        "elapsed_s": round(elapsed, 2),
        "cap_fired": cap_fired,
        "ledger_rows_count": len(ledger_rows),
        "status_row_recorded": status_jsonl_found,
        "leaked_processes": leaked,
        "no_leaked_processes": len(leaked) == 0,
        "ledger_rows": ledger_rows,
        "status_details": status_text[:400],
    }


def test_iteration_cap():
    env = get_env()
    trial_receipts = RECEIPTS_DIR / "iteration"
    trial_receipts.mkdir(parents=True, exist_ok=True)
    ledger_path = trial_receipts / "ledger.jsonl"
    report_dir = trial_receipts / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    if ledger_path.exists():
        ledger_path.unlink()

    # Step exits with code 1 immediately, retry limit = 3
    failing_script = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/on-f8-dagu-trial-20261102/research/experiments/on-dagu-trial-20261102/failing_step.py")
    dag_yaml = f"""description: KT5 Iteration Cap Test
steps:
  - id: step_retry_cap
    run: python3 {failing_script} {ledger_path}
    retry_policy:
      limit: 3
      interval_sec: 1
"""
    dag_file = RESULTS_DIR / "home/dags/kt5_iteration.yaml"
    with open(dag_file, "w") as f:
        f.write(dag_yaml)

    run_id = f"kt5_iter_{int(time.time())}"
    print("\n--- Testing KT5 Iteration Cap (retry_policy.limit: 3) ---")
    t0 = time.time()
    subprocess.run([DAGU_BIN, "start", f"--run-id={run_id}", "kt5_iteration"], env=env, capture_output=True, text=True)
    elapsed = time.time() - t0

    time.sleep(1)
    leaked = check_leaked_processes()

    st = subprocess.run([DAGU_BIN, "status", f"--run-id={run_id}", "kt5_iteration"], env=env, capture_output=True, text=True)
    status_text = st.stdout + st.stderr
    cap_fired = "failed" in status_text.lower()

    # Read ledger rows (should have initial attempt + 3 retries = 4 executions)
    ledger_rows = []
    if ledger_path.exists():
        with open(ledger_path) as f:
            ledger_rows = [json.loads(line) for line in f if line.strip()]

    status_jsonl_found = False
    pattern = list((RESULTS_DIR / "home/data/dag-runs/kt5_iteration/dag-runs").glob(f"**/*{run_id}*/**/status.jsonl"))
    if pattern:
        status_jsonl_found = True

    print("Iteration cap results:")
    print(f"  Elapsed: {elapsed:.2f}s")
    print(f"  Executions recorded in ledger: {len(ledger_rows)} (1 initial + 3 retries = 4 attempts)")
    print(f"  Cap stopped retrying: {cap_fired and len(ledger_rows) == 4}")
    print(f"  Status row recorded: {status_jsonl_found}")
    print(f"  Leaked fake_agent processes: {leaked}")

    return {
        "cap_type": "iteration_cap",
        "retry_limit": 3,
        "total_executions": len(ledger_rows),
        "cap_fired": cap_fired and len(ledger_rows) == 4,
        "ledger_rows_count": len(ledger_rows),
        "status_row_recorded": status_jsonl_found,
        "leaked_processes": leaked,
        "no_leaked_processes": len(leaked) == 0,
        "status_details": status_text[:400],
    }


def main():
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_KT5_DIR.mkdir(parents=True, exist_ok=True)

    res_wallclock = test_wallclock_cap()
    res_iteration = test_iteration_cap()

    final_leaked = check_leaked_processes()

    pass_kt5 = (
        res_wallclock["cap_fired"]
        and res_wallclock["status_row_recorded"]
        and res_iteration["cap_fired"]
        and res_iteration["status_row_recorded"]
        and len(final_leaked) == 0
    )

    summary = {
        "test": "KT5 Caps",
        "engine": "Dagu",
        "pass_verdict": pass_kt5,
        "wallclock_cap": res_wallclock,
        "iteration_cap": res_iteration,
        "final_leaked_processes": final_leaked,
    }

    with open(RECEIPTS_DIR / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    with open(RESULTS_KT5_DIR / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("\n================== KT5 SUMMARY ==================")
    print(f"Pass verdict: {pass_kt5}")
    print(f"Wallclock cap fired: {res_wallclock['cap_fired']} (stopped in {res_wallclock['elapsed_s']}s, timeout 3s)")
    print(f"Iteration cap fired: {res_iteration['cap_fired']} (stopped after {res_iteration['total_executions']} attempts)")
    print(f"Leaked fake_agent processes: {final_leaked} (clean: {len(final_leaked) == 0})")


if __name__ == "__main__":
    main()
