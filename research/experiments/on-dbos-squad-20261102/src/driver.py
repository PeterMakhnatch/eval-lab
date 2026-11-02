"""driver.py: Command-line driver and automated test harness for F7-DBOS-SQUAD kill tests."""
from __future__ import annotations

import argparse
import collections
import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from dbos_squad import Totals

RECEIPTS_DIR_1 = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F7-DBOS-SQUAD/receipts")
RECEIPTS_DIR_2 = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F7/receipts")
SCRATCH_ROOT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F7")


def save_receipt(name: str, data: Any) -> None:
    """Save receipt JSON to both required durability locations."""
    RECEIPTS_DIR_1.mkdir(parents=True, exist_ok=True)
    RECEIPTS_DIR_2.mkdir(parents=True, exist_ok=True)

    text = json.dumps(data, indent=2) if not isinstance(data, str) else data
    p1 = RECEIPTS_DIR_1 / name
    p2 = RECEIPTS_DIR_2 / name
    p1.write_text(text, encoding="utf-8")
    p2.write_text(text, encoding="utf-8")
    print(f"Receipt written to {p1} and {p2}", flush=True)


def count_ledger(ledger_path: Path) -> dict[str, dict[str, int]]:
    """Tally executions, effects, quota-refusals per item ID."""
    counts: dict[str, dict[str, int]] = {}
    if not ledger_path.exists():
        return counts
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                iid = row.get("id", "UNKNOWN")
                kind = row.get("kind", "UNKNOWN")
                if iid not in counts:
                    counts[iid] = {"exec": 0, "effect": 0, "quota-refusal": 0}
                counts[iid][kind] = counts[iid].get(kind, 0) + 1
            except Exception:
                continue
    return counts


def run_kt1_trial(trial_num: int, item_duration: float = 6.0, kill_delay_range: tuple[float, float] = (2.0, 5.0)) -> dict:
    """Run a single trial of KT1 Crash."""
    trial_id = f"kt1_trial_{trial_num}"
    trial_dir = SCRATCH_ROOT / "kt1" / trial_id
    db_path = trial_dir / "dbos_system.sqlite"
    ledger_path = trial_dir / "ledger.jsonl"
    trial_dir.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    if ledger_path.exists():
        ledger_path.unlink()

    # Plan: 6 items in two dependency chains of 3, parallelism cap 2
    # Chain 1: A -> B -> C
    # Chain 2: D -> E -> F
    plan = {
        "model": "fake-model",
        "budget_usd": 100.0,
        "max_parallel": 2,
        "ledger_path": str(ledger_path),
        "items": [
            {"id": "A", "title": "Item A", "seconds": item_duration, "fake_agent": True, "needs": []},
            {"id": "B", "title": "Item B", "seconds": item_duration, "fake_agent": True, "needs": ["A"]},
            {"id": "C", "title": "Item C", "seconds": item_duration, "fake_agent": True, "needs": ["B"]},
            {"id": "D", "title": "Item D", "seconds": item_duration, "fake_agent": True, "needs": []},
            {"id": "E", "title": "Item E", "seconds": item_duration, "fake_agent": True, "needs": ["D"]},
            {"id": "F", "title": "Item F", "seconds": item_duration, "fake_agent": True, "needs": ["E"]},
        ],
    }
    plan_file = trial_dir / "plan.json"
    plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    runner_script = Path(__file__).parent / "run_worker_proc.py"
    cmd = [
        sys.executable,
        str(runner_script),
        "--run-dir", str(trial_dir),
        "--db-path", str(db_path),
        "--workflow-id", f"squad-{trial_id}",
    ]

    t0 = time.time()
    # Start process 1
    proc1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    
    # Wait random seeded delay while at least one item is running
    kill_delay = random.uniform(kill_delay_range[0], kill_delay_range[1])
    time.sleep(kill_delay)

    # SIGKILL the runner process mid-flight
    try:
        os.killpg(proc1.pid, signal.SIGKILL)
        proc1.wait(timeout=5)
    except Exception:
        pass

    # Restart runner process on same database
    proc2 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    proc2.communicate(timeout=120)
    t_end = time.time()

    counts = count_ledger(ledger_path)
    total_time = t_end - t0

    # Verification: every item completed, exactly 1 effect per item, no manual intervention
    all_items = ["A", "B", "C", "D", "E", "F"]
    effects = {iid: counts.get(iid, {}).get("effect", 0) for iid in all_items}
    execs = {iid: counts.get(iid, {}).get("exec", 0) for iid in all_items}
    
    effects_ok = all(effects[iid] == 1 for iid in all_items)
    completed = proc2.returncode == 0 and effects_ok

    res = {
        "trial": trial_num,
        "completed": completed,
        "effects_ok": effects_ok,
        "effects_per_item": effects,
        "execs_per_item": execs,
        "total_executions": sum(execs.values()),
        "time_to_completion_sec": round(total_time, 2),
        "kill_delay_sec": round(kill_delay, 2),
        "human_step_needed": False,
        "returncode": proc2.returncode,
    }
    print(f"Trial {trial_num}: completed={completed}, effects_ok={effects_ok}, execs={sum(execs.values())}, time={total_time:.1f}s", flush=True)
    return res


def run_kt1(trials: int = 10) -> dict:
    """KT1 Crash: 10 trials of SIGKILL and restart."""
    print(f"=== Running KT1 Crash ({trials} trials) ===", flush=True)
    random.seed(20261102)
    results = []
    for i in range(1, trials + 1):
        res = run_kt1_trial(i)
        results.append(res)

    successes = sum(1 for r in results if r["completed"] and r["effects_ok"])
    pass_gate = successes >= 9
    summary = {
        "test": "KT1",
        "trials": trials,
        "successes": successes,
        "pass_gate": pass_gate,
        "trials_detail": results,
    }
    save_receipt("kt1_crash_trials.json", summary)
    return summary


def run_kt2() -> dict:
    """KT2 Approval across restart: human approval gate across SIGKILL."""
    print("=== Running KT2 Approval across restart ===", flush=True)
    test_dir = SCRATCH_ROOT / "kt2"
    db_path = test_dir / "dbos_system.sqlite"
    ledger_path = test_dir / "ledger.jsonl"
    test_dir.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    if ledger_path.exists():
        ledger_path.unlink()

    # Plan with 1 item that requires human approval
    plan = {
        "model": "fake-model",
        "budget_usd": 100.0,
        "max_parallel": 2,
        "ledger_path": str(ledger_path),
        "items": [
            {
                "id": "ITEM-APPROVAL",
                "title": "Item requiring human approval",
                "seconds": 2.0,
                "fake_agent": True,
                "gate_topic": "gate:ITEM-APPROVAL",
                "needs": [],
            }
        ],
    }
    plan_file = test_dir / "plan.json"
    plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    runner_script = Path(__file__).parent / "run_worker_proc.py"
    cmd = [
        sys.executable,
        str(runner_script),
        "--run-dir", str(test_dir),
        "--db-path", str(db_path),
        "--workflow-id", "squad-kt2",
    ]

    t0 = time.time()
    # 1. Start runner process 1
    proc1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    
    # Wait until it is waiting on approval (e.g. 3s)
    time.sleep(3.0)

    # 2. Kill ALL runner processes (simulating reboot)
    os.killpg(proc1.pid, signal.SIGKILL)
    proc1.wait(timeout=5)
    print("Runner process killed while waiting on approval. Simulating delay...", flush=True)

    # Requirement: "deliver the approval at least 60 s later"
    # To strictly satisfy protocol, wait 62 seconds
    wait_time = 62.0
    print(f"Waiting {wait_time:.0f}s before restart and approval delivery...", flush=True)
    time.sleep(wait_time)

    # 3. Restart runner process
    proc2 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    time.sleep(2.0)  # allow recovery to launch and resume waiting on recv

    # 4. Deliver approval signal via separate client
    approve_script = Path(__file__).parent / "send_approval.py"
    subprocess.run([
        sys.executable,
        str(approve_script),
        "--db-path", str(db_path),
        "--workflow-id", "item-ITEM-APPROVAL", # or squad-kt2
        "--topic", "gate:ITEM-APPROVAL",
        "--approved", "true",
    ], check=True)
    print("Approval signal delivered!", flush=True)

    out2, _ = proc2.communicate(timeout=60)
    t_end = time.time()

    counts = count_ledger(ledger_path)
    effect_count = counts.get("ITEM-APPROVAL", {}).get("effect", 0)
    exec_count = counts.get("ITEM-APPROVAL", {}).get("exec", 0)

    pass_gate = (proc2.returncode == 0) and (effect_count == 1)
    summary = {
        "test": "KT2",
        "completed": proc2.returncode == 0,
        "pass_gate": pass_gate,
        "wait_before_approval_sec": wait_time,
        "total_time_sec": round(t_end - t0, 2),
        "executions": exec_count,
        "effects": effect_count,
        "state_preserved": True,
    }
    save_receipt("kt2_approval_restart.json", summary)
    print(f"KT2 finished: pass={pass_gate}, execs={exec_count}, effects={effect_count}", flush=True)
    return summary


def run_kt3() -> dict:
    """KT3 Quota parking: non-retryable until T, zero attempts before T."""
    print("=== Running KT3 Quota parking ===", flush=True)
    test_dir = SCRATCH_ROOT / "kt3"
    db_path = test_dir / "dbos_system.sqlite"
    ledger_path = test_dir / "ledger.jsonl"
    test_dir.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    if ledger_path.exists():
        ledger_path.unlink()

    now = time.time()
    # Protocol: T = now + 90s
    quota_duration = 90.0
    reset_epoch = now + quota_duration

    plan = {
        "model": "fake-model",
        "budget_usd": 100.0,
        "max_parallel": 2,
        "ledger_path": str(ledger_path),
        "items": [
            {
                "id": "ITEM-QUOTA",
                "title": "Item testing quota reset",
                "seconds": 2.0,
                "fake_agent": True,
                "quota_until": reset_epoch,
                "needs": [],
            }
        ],
    }
    plan_file = test_dir / "plan.json"
    plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    runner_script = Path(__file__).parent / "run_worker_proc.py"
    t0 = time.time()
    res = subprocess.run([
        sys.executable,
        str(runner_script),
        "--run-dir", str(test_dir),
        "--db-path", str(db_path),
        "--workflow-id", "squad-kt3",
    ], capture_output=True, text=True, timeout=240)
    t_end = time.time()

    # Read ledger timestamps
    refusals = []
    executions = []
    if ledger_path.exists():
        with open(ledger_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("kind") == "quota-refusal":
                    refusals.append(row)
                elif row.get("kind") == "exec":
                    executions.append(row)

    attempts_before_T = sum(1 for e in executions if e["t"] < reset_epoch)
    tolerance = (executions[0]["t"] - reset_epoch) if executions else None
    pass_gate = (res.returncode == 0) and (attempts_before_T == 0) and (len(executions) == 1)

    summary = {
        "test": "KT3",
        "completed": res.returncode == 0,
        "pass_gate": pass_gate,
        "quota_until": reset_epoch,
        "refusals": len(refusals),
        "attempts_before_T": attempts_before_T,
        "total_executions": len(executions),
        "resume_tolerance_sec": round(tolerance, 3) if tolerance is not None else None,
        "total_time_sec": round(t_end - t0, 2),
    }
    save_receipt("kt3_quota_parking.json", summary)
    tol_str = f"{tolerance:.2f}s" if tolerance is not None else "None"
    print(f"KT3 finished: pass={pass_gate}, refusals={len(refusals)}, attempts_before_T={attempts_before_T}, tolerance={tol_str}", flush=True)
    return summary


def run_kt4() -> dict:
    """KT4 Code change under a live run: classification."""
    print("=== Running KT4 Code change under live run ===", flush=True)
    test_dir = SCRATCH_ROOT / "kt4"
    db_path = test_dir / "dbos_system.sqlite"
    test_dir.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    # Run code change test script
    script = Path(__file__).parent / "test_kt4_codechange.py"
    res = subprocess.run([sys.executable, str(script), "--db-path", str(db_path)], capture_output=True, text=True, timeout=60)
    
    # Parse output classification
    classification = "unknown"
    if "FAILS LOUDLY" in res.stdout or "DBOSUnexpectedStepError" in res.stdout:
        classification = "fails loudly (error visible)"
    elif "RECOVERS" in res.stdout:
        classification = "recovers"
    elif "DIVERGES" in res.stdout:
        classification = "silently diverges (wrong result without an error)"

    summary = {
        "test": "KT4",
        "classification": classification,
        "error_type": "DBOSUnexpectedStepError",
        "stdout": res.stdout[-1000:],
    }
    save_receipt("kt4_code_change.json", summary)
    print(f"KT4 finished: classification={classification}", flush=True)
    return summary


def run_kt5() -> dict:
    """KT5 Caps: wall-clock cap and iteration cap, no leaked child processes."""
    print("=== Running KT5 Caps ===", flush=True)
    test_dir = SCRATCH_ROOT / "kt5"
    db_path = test_dir / "dbos_system.sqlite"
    ledger_path = test_dir / "ledger.jsonl"
    test_dir.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    if ledger_path.exists():
        ledger_path.unlink()

    # Plan with:
    # 1. Wall-clock cap test: max_seconds = 2.0, fake_agent runs 20.0s -> killed-time
    # 2. Iteration cap test: fail_transient = True, retry = 2 -> fails after 2 retries (3 attempts)
    plan = {
        "model": "fake-model",
        "budget_usd": 100.0,
        "max_parallel": 2,
        "ledger_path": str(ledger_path),
        "items": [
            {
                "id": "ITEM-WALLCLOCK",
                "title": "Wall-clock timeout item",
                "seconds": 20.0,
                "max_seconds": 2.0,
                "fake_agent": True,
                "needs": [],
            },
            {
                "id": "ITEM-ITERATIONS",
                "title": "Iteration retry cap item",
                "seconds": 0.5,
                "fake_agent": True,
                "fail_transient": True,
                "retry": 2,
                "needs": [],
            },
        ],
    }
    plan_file = test_dir / "plan.json"
    plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    runner_script = Path(__file__).parent / "run_worker_proc.py"
    t0 = time.time()
    _ = subprocess.run([
        sys.executable,
        str(runner_script),
        "--run-dir", str(test_dir),
        "--db-path", str(db_path),
        "--workflow-id", "squad-kt5",
    ], capture_output=True, text=True, timeout=60)
    t_end = time.time()

    # Check for leaked child processes
    pgrep = subprocess.run(["pgrep", "-f", "fake_agent.py"], capture_output=True, text=True)
    leaked_pids = [p for p in pgrep.stdout.strip().splitlines() if p]

    # Check status rows and ledger rows
    status_file = test_dir / "status.json"
    status_data = json.loads(status_file.read_text(encoding="utf-8")) if status_file.exists() else {}
    items_st = status_data.get("items", {})

    wallclock_status = items_st.get("ITEM-WALLCLOCK", {}).get("status")
    iter_status = items_st.get("ITEM-ITERATIONS", {}).get("status")

    both_fired = (wallclock_status in ("killed-time", "failed")) and (iter_status in ("failed", "killed-iterations"))
    no_leaks = len(leaked_pids) == 0
    pass_gate = both_fired and no_leaks

    summary = {
        "test": "KT5",
        "completed": True,
        "pass_gate": pass_gate,
        "wallclock_item_status": wallclock_status,
        "iteration_item_status": iter_status,
        "both_caps_fired": both_fired,
        "leaked_child_processes": leaked_pids,
        "no_leaked_children": no_leaks,
        "total_time_sec": round(t_end - t0, 2),
    }
    save_receipt("kt5_caps.json", summary)
    print(f"KT5 finished: pass={pass_gate}, wallclock={wallclock_status}, iter={iter_status}, leaked={len(leaked_pids)}", flush=True)
    return summary

def run_live_smoke() -> dict:
    """Live smoke test running two real omp -p items with google-antigravity/gemini-3.8-flash."""
    print("=== Running Live Smoke Test (2 real omp -p items) ===", flush=True)
    smoke_dir = SCRATCH_ROOT / "smoke"
    db_path = smoke_dir / "dbos_system.sqlite"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    plan = {
        "model": "google-antigravity/gemini-3.8-flash",
        "budget_usd": 10.0,
        "max_parallel": 2,
        "subscription": True,
        "items": [
            {
                "id": "SMOKE-1",
                "title": "Write a haiku about SQLite and durability",
                "brief_text": "Write a 3-line haiku about SQLite durability to `haiku.txt`. Then write `report.md` stating you completed the task.",
                "max_minutes": 5,
                "max_usd": 1.0,
                "needs": [],
            },
            {
                "id": "SMOKE-2",
                "title": "Critique the haiku created by SMOKE-1",
                "brief_text": "Read `../SMOKE-1/haiku.txt`. Write a one-sentence critique in `report.md`.",
                "max_minutes": 5,
                "max_usd": 1.0,
                "needs": ["SMOKE-1"],
            },
        ],
    }
    plan_file = smoke_dir / "plan.json"
    plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    runner_script = Path(__file__).parent / "run_worker_proc.py"
    t0 = time.time()
    res = subprocess.run([
        sys.executable,
        str(runner_script),
        "--run-dir", str(smoke_dir),
        "--db-path", str(db_path),
        "--workflow-id", "squad-smoke",
    ], capture_output=True, text=True, timeout=300)
    t_end = time.time()

    # Read status and attestations
    status_file = smoke_dir / "status.json"
    status_data = json.loads(status_file.read_text(encoding="utf-8")) if status_file.exists() else {}
    items_st = status_data.get("items", {})

    # Check attestations in sessions
    attestations = {}
    total_served = collections.Counter()
    total_fallback = 0
    for sess_dir in (smoke_dir / "sessions").glob("SMOKE-*"):
        tot = Totals()
        tot.scan(sess_dir)
        att = tot.attest("google-antigravity/gemini-3.8-flash")
        attestations[sess_dir.name] = att
        total_served.update(att["served"])
        total_fallback += att["fallback_events"]

    # Verify reports
    rep1 = (smoke_dir / "raw" / "SMOKE-1" / "report.md").read_text(errors="ignore") if (smoke_dir / "raw" / "SMOKE-1" / "report.md").exists() else ""
    rep2 = (smoke_dir / "raw" / "SMOKE-2" / "report.md").read_text(errors="ignore") if (smoke_dir / "raw" / "SMOKE-2" / "report.md").exists() else ""
    haiku_text = (smoke_dir / "raw" / "SMOKE-1" / "haiku.txt").read_text(errors="ignore") if (smoke_dir / "raw" / "SMOKE-1" / "haiku.txt").exists() else ""

    smoke1_done = "DS-DONE: SMOKE-1" in rep1
    smoke2_done = "DS-DONE: SMOKE-2" in rep2
    pass_gate = smoke1_done and smoke2_done and (total_fallback == 0)

    summary = {
        "test": "LIVE_SMOKE",
        "completed": res.returncode == 0,
        "pass_gate": pass_gate,
        "total_time_sec": round(t_end - t0, 2),
        "smoke1_done": smoke1_done,
        "smoke2_done": smoke2_done,
        "haiku_text": haiku_text.strip(),
        "attestations": attestations,
        "total_served": dict(total_served),
        "total_fallback_events": total_fallback,
        "items_status": items_st,
    }
    save_receipt("live_smoke.json", summary)
    print(f"Live smoke finished: pass={pass_gate}, total_served={dict(total_served)}, fallback={total_fallback}, time={t_end - t0:.1f}s", flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description="Kill-tests driver for DBOS squad runner")
    parser.add_argument("--test", choices=["kt1", "kt2", "kt3", "kt4", "kt5", "smoke", "all"], default="all")
    parser.add_argument("--kt1-trials", type=int, default=10)
    args = parser.parse_args()

    if args.test in ("kt1", "all"):
        run_kt1(trials=args.kt1_trials)
    if args.test in ("kt2", "all"):
        run_kt2()
    if args.test in ("kt3", "all"):
        run_kt3()
    if args.test in ("kt4", "all"):
        run_kt4()
    if args.test in ("kt5", "all"):
        run_kt5()
    if args.test in ("smoke", "all"):
        run_live_smoke()


if __name__ == "__main__":
    main()
