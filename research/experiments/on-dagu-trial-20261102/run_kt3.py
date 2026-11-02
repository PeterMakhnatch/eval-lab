#!/usr/bin/env python3
"""KT3 Quota Parking Test for Dagu.

Protocol:
- The first executions of item X exit 29 with a quota message resetting at T = now + 90 s (or tested window).
- PASS = the engine treats it as non-retryable until T, makes ZERO execution attempts before T (ledger timestamps),
  and resumes at T plus a stated tolerance automatically. Count refusals and attempts.
- Investigation: does Dagu's retry policy have a way to "park until T" with zero attempts before T?
  Record exactly what exists and what you had to script.
"""

import contextlib
import json
import os
import subprocess
import time
from pathlib import Path

DAGU_BIN = "/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F8/dagu/dagu"
RESULTS_DIR = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F8/dagu")
RECEIPTS_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F8-DAGU-TRIAL/receipts/kt3")
RESULTS_KT3_DIR = RESULTS_DIR / "kt3"


def get_env():
    env = os.environ.copy()
    env["DAGU_HOME"] = str(RESULTS_DIR / "home")
    env["DAGU_CONFIG"] = str(RESULTS_DIR / "home/config.yaml")
    return env


def parse_ledger(ledger_path: Path):
    rows = []
    if not ledger_path.exists():
        return rows
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            with contextlib.suppress(Exception):
                rows.append(json.loads(line))
    return rows


def run_native_retry_trial(parking_duration_s: float = 30.0, retry_interval_s: int = 5):
    """Test 3A: Native Dagu retry policy.
    Dagu has interval_sec and limit, but no park_until.
    With retry_interval_s < parking_duration_s, Dagu retries before T, producing quota refusals.
    """
    env = get_env()
    t_start = time.time()
    t_reset = t_start + parking_duration_s

    trial_receipts = RECEIPTS_DIR / "native_retry"
    trial_receipts.mkdir(parents=True, exist_ok=True)
    ledger_path = trial_receipts / "ledger.jsonl"
    report_dir = trial_receipts / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    if ledger_path.exists():
        ledger_path.unlink()

    # Create DAG with native retry policy
    dag_yaml = f"""description: KT3 Native Retry Policy Test
steps:
  - id: quota_task
    run: python3 /Users/petermakhnatch/Developer/eval-lab/.worktrees/on-f8-dagu-trial-20261102/research/experiments/on-dagu-trial-20261102/fake_agent.py --id QUOTA_X --ledger "{ledger_path}" --report "{report_dir}/QUOTA_X.md" --seconds 2 --quota-until {t_reset}
    retry_policy:
      limit: 10
      interval_sec: {retry_interval_s}
      exit_code: [29]
"""
    dag_file = RESULTS_DIR / "home/dags/kt3_native.yaml"
    with open(dag_file, "w") as f:
        f.write(dag_yaml)

    run_id = f"kt3_native_{int(time.time())}"
    print(f"\n--- Running KT3 Part A: Native Dagu retry_policy (T = now + {parking_duration_s}s, retry_interval = {retry_interval_s}s) ---")
    subprocess.run([DAGU_BIN, "start", f"--run-id={run_id}", "kt3_native"], env=env, capture_output=True, text=True)
    
    t_end = time.time()
    ledger_rows = parse_ledger(ledger_path)

    refusals = [r for r in ledger_rows if r.get("kind") == "quota-refusal"]
    execs = [r for r in ledger_rows if r.get("kind") == "exec"]
    effects = [r for r in ledger_rows if r.get("kind") == "effect"]
    attempts_before_t = [r for r in refusals if r.get("t", 0) < t_reset]

    print("Native Dagu results:")
    print(f"  Total run time: {t_end - t_start:.1f}s")
    print(f"  Quota refusals before T: {len(attempts_before_t)}")
    print(f"  Total refusals: {len(refusals)}")
    print(f"  Successful executions after T: {len(execs)}")
    print(f"  Effects: {len(effects)}")

    return {
        "mode": "native_retry_policy",
        "parking_duration_s": parking_duration_s,
        "retry_interval_s": retry_interval_s,
        "t_reset": t_reset,
        "total_duration_s": round(t_end - t_start, 2),
        "quota_refusals_count": len(refusals),
        "attempts_before_t": len(attempts_before_t),
        "effects_count": len(effects),
        "zero_attempts_before_t": len(attempts_before_t) == 0,
        "ledger_rows": ledger_rows,
    }


def run_scripted_parking_trial(parking_duration_s: float = 30.0):
    """Test 3B: Scripted wrapper quota parking adapter.
    Dagu has no native dynamic retry-after parking, so an adapter script intercepts exit 29,
    extracts reset time T, sleeps until T, and re-invokes with zero attempts before T.
    """
    env = get_env()
    t_start = time.time()
    t_reset = t_start + parking_duration_s

    trial_receipts = RECEIPTS_DIR / "scripted_parking"
    trial_receipts.mkdir(parents=True, exist_ok=True)
    ledger_path = trial_receipts / "ledger.jsonl"
    report_dir = trial_receipts / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    if ledger_path.exists():
        ledger_path.unlink()

    wrapper_script = """#!/usr/bin/env python3
import sys, subprocess, time, re, datetime

cmd = sys.argv[1:]
while True:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 29:
        # Parse quota exhausted message: 'quota exhausted; resets at <ISO of T>'
        match = re.search(r"resets at ([0-9T:+\-\.Z]+)", res.stderr + res.stdout)
        if match:
            iso_str = match.group(1)
            reset_dt = datetime.datetime.fromisoformat(iso_str)
            reset_epoch = reset_dt.timestamp()
            now = time.time()
            sleep_time = max(0.0, reset_epoch - now)
            print(f"[parking-wrapper] Caught 429 quota exhaustion. Parking for {sleep_time:.2f}s until {iso_str} with 0 attempts...", file=sys.stderr)
            time.sleep(sleep_time + 0.1) # sleep until T + tolerance
            continue
    # Non-29 return or success
    if res.stdout:
        sys.stdout.write(res.stdout)
    if res.stderr:
        sys.stderr.write(res.stderr)
    sys.exit(res.returncode)
"""
    wrapper_path = RESULTS_DIR / "quota_parking_wrapper.py"
    with open(wrapper_path, "w") as f:
        f.write(wrapper_script)
    os.chmod(wrapper_path, 0o755)

    dag_yaml = f"""description: KT3 Scripted Quota Parking Test
steps:
  - id: quota_task_wrapped
    run: python3 {wrapper_path} python3 /Users/petermakhnatch/Developer/eval-lab/.worktrees/on-f8-dagu-trial-20261102/research/experiments/on-dagu-trial-20261102/fake_agent.py --id QUOTA_X --ledger "{ledger_path}" --report "{report_dir}/QUOTA_X.md" --seconds 2 --quota-until {t_reset}
"""
    dag_file = RESULTS_DIR / "home/dags/kt3_scripted.yaml"
    with open(dag_file, "w") as f:
        f.write(dag_yaml)

    run_id = f"kt3_scripted_{int(time.time())}"
    print(f"\n--- Running KT3 Part B: Scripted Quota Parking Wrapper (T = now + {parking_duration_s}s) ---")
    subprocess.run([DAGU_BIN, "start", f"--run-id={run_id}", "kt3_scripted"], env=env, capture_output=True, text=True)

    t_end = time.time()
    ledger_rows = parse_ledger(ledger_path)

    refusals = [r for r in ledger_rows if r.get("kind") == "quota-refusal"]
    execs = [r for r in ledger_rows if r.get("kind") == "exec"]
    effects = [r for r in ledger_rows if r.get("kind") == "effect"]

    # Attempts before T: any refusal after the initial one, or any exec before T
    initial_refusal_time = refusals[0]["t"] if refusals else 0
    attempts_between_refusal_and_t = [
        r for r in ledger_rows 
        if r.get("t", 0) > initial_refusal_time and r.get("t", 0) < t_reset
    ]

    tolerance_s = round(execs[0]["t"] - t_reset, 2) if execs else None

    print("Scripted wrapper results:")
    print(f"  Total run time: {t_end - t_start:.1f}s")
    print(f"  Initial quota refusal recorded: {len(refusals) > 0}")
    print(f"  Attempts between initial refusal and T: {len(attempts_between_refusal_and_t)}")
    print(f"  Successful execution time: {execs[0]['t'] - t_start:.1f}s (resumed at T + {tolerance_s}s)")
    print(f"  Effects: {len(effects)}")

    return {
        "mode": "scripted_parking_wrapper",
        "parking_duration_s": parking_duration_s,
        "t_reset": t_reset,
        "total_duration_s": round(t_end - t_start, 2),
        "quota_refusals_count": len(refusals),
        "attempts_between_refusal_and_t": len(attempts_between_refusal_and_t),
        "zero_attempts_before_t": len(attempts_between_refusal_and_t) == 0,
        "tolerance_s": tolerance_s,
        "effects_count": len(effects),
        "ledger_rows": ledger_rows,
    }


def main():
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_KT3_DIR.mkdir(parents=True, exist_ok=True)

    print("================== KT3 QUOTA PARKING INVESTIGATION ==================")
    print("Question: Does Dagu's retry policy have a way to 'park until T' with zero attempts before T?")
    
    # 1. Native retry policy test (30s window, 5s retry interval)
    res_native = run_native_retry_trial(parking_duration_s=30.0, retry_interval_s=5)

    # 2. Scripted parking adapter test (30s window)
    res_scripted = run_scripted_parking_trial(parking_duration_s=30.0)

    summary = {
        "test": "KT3 Quota parking",
        "engine": "Dagu",
        "dagu_native_capability": {
            "has_park_until": False,
            "has_dynamic_retry_after": False,
            "has_non_retryable_classification": False,
            "supported_fields": ["interval_sec", "limit", "backoff", "exit_code", "max_interval_sec"],
            "verdict": "FAIL (Native Dagu retry policy cannot park until T. It executes static retries, issuing repeated attempts before T).",
        },
        "native_trial": res_native,
        "scripted_trial": res_scripted,
    }

    with open(RECEIPTS_DIR / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    with open(RESULTS_KT3_DIR / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("\n================== KT3 SUMMARY ==================")
    print(f"Native Dagu: attempts before T = {res_native['attempts_before_t']} (refusals: {res_native['quota_refusals_count']}) -> ZERO attempts? {res_native['zero_attempts_before_t']}")
    print(f"Scripted Dagu: attempts before T = {res_scripted['attempts_between_refusal_and_t']} (refusals: {res_scripted['quota_refusals_count']}) -> ZERO attempts? {res_scripted['zero_attempts_before_t']}")
    print(f"Scripted tolerance: +{res_scripted['tolerance_s']}s")


if __name__ == "__main__":
    main()
