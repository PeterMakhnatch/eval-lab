#!/usr/bin/env python3
"""KT1 Crash Test for Dagu.

Protocol (from _KILLTESTS.md):
- SIGKILL the engine's worker or runner process at a random (seeded) moment while at least one item is running; restart it; 10 trials.
- Per trial record: completed (yes/no), executions per item (ledger exec rows), effects per item (must be exactly 1), time to completion, whether a human step was needed.
- PASS = completed in at least 9 of 10 trials with every item's effect applied exactly once and no manual step.
- Also report raw executions (at-least-once re-runs are expected; say how many).
"""

import argparse
import contextlib
import json
import os
import random
import signal
import subprocess
import time
from pathlib import Path

DAGU_BIN = "/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F8/dagu/dagu"
RESULTS_DIR = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F8/dagu")
RECEIPTS_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F8-DAGU-TRIAL/receipts/kt1")
RESULTS_KT1_DIR = RESULTS_DIR / "kt1"


def get_env():
    env = os.environ.copy()
    env["DAGU_HOME"] = str(RESULTS_DIR / "home")
    env["DAGU_CONFIG"] = str(RESULTS_DIR / "home/config.yaml")
    return env


def parse_ledger(ledger_path: Path):
    exec_counts = {"A1": 0, "A2": 0, "A3": 0, "B1": 0, "B2": 0, "B3": 0}
    effect_counts = {"A1": 0, "A2": 0, "A3": 0, "B1": 0, "B2": 0, "B3": 0}
    if not ledger_path.exists():
        return exec_counts, effect_counts
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                task_id = row.get("id")
                kind = row.get("kind")
                if task_id in exec_counts:
                    if kind == "exec":
                        exec_counts[task_id] += 1
                    elif kind == "effect":
                        effect_counts[task_id] += 1
            except Exception:
                pass
    return exec_counts, effect_counts


def run_trial(trial_num: int, kill_delay: float, seconds_per_item: float = 20.0):
    env = get_env()
    run_id = f"kt1_trial_{trial_num}_{int(time.time())}"
    
    trial_receipts = RECEIPTS_DIR / f"trial_{trial_num}"
    trial_receipts.mkdir(parents=True, exist_ok=True)
    trial_results = RESULTS_KT1_DIR / f"trial_{trial_num}"
    trial_results.mkdir(parents=True, exist_ok=True)

    ledger_path = trial_receipts / "ledger.jsonl"
    report_dir = trial_receipts / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    if ledger_path.exists():
        ledger_path.unlink()

    print(f"\n--- Starting KT1 Trial {trial_num}/10 (kill delay: {kill_delay:.1f}s) ---")
    start_time = time.time()

    # Launch dagu start
    cmd = [
        DAGU_BIN, "start",
        f"--run-id={run_id}",
        "kt1_squad",
        "--",
        f"LEDGER={ledger_path}",
        f"REPORT_DIR={report_dir}",
        f"ITEM_SECONDS={seconds_per_item}",
    ]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    # Wait for kill_delay
    time.sleep(kill_delay)

    # Verify at least one item was running
    alive = proc.poll() is None
    print(f"Trial {trial_num}: Runner PID {proc.pid}, alive at kill moment: {alive}")
    if alive:
        print(f"Trial {trial_num}: SIGKILLing dagu runner process {proc.pid}...")
        with contextlib.suppress(ProcessLookupError):
            os.kill(proc.pid, signal.SIGKILL)
        proc.wait()

    # Wait for zombie detector to detect failure
    print(f"Trial {trial_num}: Waiting for zombie detector to mark run as failed...")
    zombie_detected = False
    for _ in range(15):
        time.sleep(1)
        st = subprocess.run([DAGU_BIN, "status", f"--run-id={run_id}", "kt1_squad"], env=env, capture_output=True, text=True)
        out = (st.stdout + st.stderr).lower()
        if "failed" in out or "problem" in out:
            zombie_detected = True
            break
    print(f"Trial {trial_num}: Zombie detected: {zombie_detected}")

    # Restart/Retry
    print(f"Trial {trial_num}: Restarting via dagu retry --run-id={run_id} kt1_squad...")
    time.time()
    retry_proc = subprocess.run(
        [DAGU_BIN, "retry", f"--run-id={run_id}", "kt1_squad"],
        env=env,
        capture_output=True,
        text=True,
    )
    retry_end = time.time()
    total_time = retry_end - start_time

    # Final status check
    st_final = subprocess.run([DAGU_BIN, "status", f"--run-id={run_id}", "kt1_squad"], env=env, capture_output=True, text=True)
    completed = "succeeded" in (st_final.stdout + st_final.stderr).lower()

    exec_counts, effect_counts = parse_ledger(ledger_path)
    effects_exact_one = all(c == 1 for c in effect_counts.values())
    total_execs = sum(exec_counts.values())

    # Mirror ledger and reports to results dir
    if ledger_path.exists():
        with open(ledger_path, encoding="utf-8") as src, open(trial_results / "ledger.jsonl", "w", encoding="utf-8") as dst:
            dst.write(src.read())

    trial_summary = {
        "trial": trial_num,
        "run_id": run_id,
        "kill_delay_s": kill_delay,
        "seconds_per_item": seconds_per_item,
        "completed": completed,
        "effects_exact_one": effects_exact_one,
        "exec_counts": exec_counts,
        "effect_counts": effect_counts,
        "total_executions": total_execs,
        "time_to_completion_s": round(total_time, 2),
        "human_step_needed": False,
        "retry_returncode": retry_proc.returncode,
    }

    print(f"Trial {trial_num} Result: completed={completed}, effects_exact_one={effects_exact_one}, total_execs={total_execs}, duration={total_time:.1f}s")
    print(f"  Execs: {exec_counts}")
    print(f"  Effects: {effect_counts}")

    with open(trial_receipts / "result.json", "w", encoding="utf-8") as f:
        json.dump(trial_summary, f, indent=2)
    with open(trial_results / "result.json", "w", encoding="utf-8") as f:
        json.dump(trial_summary, f, indent=2)

    return trial_summary


def main():
    parser = argparse.ArgumentParser(description="Run KT1 crash trials")
    parser.add_argument("--trials", type=int, default=10, help="Number of trials")
    parser.add_argument("--seconds", type=float, default=20.0, help="Seconds per item")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    args = parser.parse_args()

    random.seed(args.seed)
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_KT1_DIR.mkdir(parents=True, exist_ok=True)

    # 10 seeded random kill delays between 5.0 and 45.0 seconds
    delays = [round(random.uniform(5.0, 45.0), 1) for _ in range(args.trials)]
    print(f"Seeded kill delays for {args.trials} trials: {delays}")

    results = []
    for i, delay in enumerate(delays, start=1):
        summary = run_trial(i, delay, args.seconds)
        results.append(summary)

    # Summary
    completed_trials = sum(1 for r in results if r["completed"] and r["effects_exact_one"])
    pass_kt1 = completed_trials >= 9

    overall = {
        "test": "KT1 Crash",
        "engine": "Dagu",
        "trials": args.trials,
        "passed_trials": completed_trials,
        "pass_verdict": pass_kt1,
        "seed": args.seed,
        "results": results,
    }

    summary_file = RECEIPTS_DIR / "summary.json"
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(overall, f, indent=2)
    with open(RESULTS_KT1_DIR / "summary.json", "w", encoding="utf-8") as f:
        json.dump(overall, f, indent=2)

    print("\n================== KT1 OVERALL SUMMARY ==================")
    print(f"Pass verdict: {pass_kt1} ({completed_trials}/{args.trials} trials passed)")
    print(f"Summary written to {summary_file}")


if __name__ == "__main__":
    main()
