#!/usr/bin/env python3
"""run_toy_plan_smoke.py: End-to-end smoke of the 4-item toy plan on ds_squad_v2.py.
Exercises:
  - 2 trivial write & report items (attested gemini-3.8-flash)
  - 1 late-gated item released by touching gate file
  - 1 long worker (90s) killed mid-run and adopted by restarted runner
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/R8")
PLAN_DIR = ROOT / "toy_plan"
SRC_V2 = ROOT / "ds_squad_v2.py"


def is_pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


def get_omp_children(run_name: str | None = None) -> list[int]:
    pids = []
    pattern = f"omp -p.*{run_name}" if run_name else "omp -p.*toy_plan"
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                p = int(line)
                if p != os.getpid():
                    pids.append(p)
    except Exception:
        pass
    return pids


def kill_process(pid: int):
    if not pid:
        return
    try:
        pg = os.getpgid(pid)
        os.killpg(pg, signal.SIGKILL)
    except Exception:
        with contextlib.suppress(Exception):
            os.kill(pid, signal.SIGKILL)


def main():
    print("Starting 4-item Toy Plan End-to-End Smoke...", flush=True)
    t0 = time.time()

    # Reset any prior runs in toy_plan
    for sub in ("raw", "sessions", "approvals"):
        d = PLAN_DIR / sub
        if d.exists():
            for p in d.iterdir():
                if p.is_file() or p.is_symlink():
                    p.unlink()
                elif p.is_dir():
                    import shutil
                    shutil.rmtree(p)
    for f in PLAN_DIR.glob("state*.json*"):
        f.unlink()
    for f in PLAN_DIR.glob("heartbeat*.json*"):
        f.unlink()
    for f in PLAN_DIR.glob("status*.md*"):
        f.unlink()
    for f in PLAN_DIR.glob("_ds*.log"):
        f.unlink()
    for f in PLAN_DIR.glob("ds-squad*.md"):
        f.unlink()

    # Plan with 0 linger so it completes when all items finish
    plan_file = PLAN_DIR / "plan.json"
    plan_data = json.loads(plan_file.read_text())
    plan_data["linger_minutes"] = 0
    plan_data["wait_gates_minutes"] = 10
    plan_data["max_parallel"] = 2
    plan_file.write_text(json.dumps(plan_data, indent=1) + "\n")

    env = {**os.environ, "EVALLAB_DISABLE_LOCAL_DOCKER": "1"}

    # Launch Runner 1
    t_runner1_start = time.time()
    runner1 = subprocess.Popen(
        [sys.executable, str(SRC_V2), "--run", str(PLAN_DIR)],
        cwd=PLAN_DIR,
        env=env,
        stdout=open(PLAN_DIR / "runner1.log", "wb"),  # noqa: SIM115
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    runner1_pid = runner1.pid
    print(f"Runner 1 started (pid {runner1_pid})", flush=True)

    state_file = PLAN_DIR / "state.json"
    gate_touched = False
    runner_killed = False
    runner2 = None
    runner2_pid = None
    adopted_long_worker = False
    surviving_long_pid = None

    deadline = time.time() + 300.0  # 5 min max
    while time.time() < deadline:
        if state_file.exists():
            try:
                st = json.loads(state_file.read_text()).get("items", {})
                st.get("ITEM-TRIVIAL-1", {}).get("status")
                st.get("ITEM-TRIVIAL-2", {}).get("status")
                st_gate = st.get("ITEM-GATE", {}).get("status")
                st_long = st.get("ITEM-LONG-WORKER", {}).get("status")

                # Step 1: When gate item is waiting-gate and trivial items progressing, release gate after 8s
                if not gate_touched and st_gate == "waiting-gate" and time.time() - t_runner1_start > 8.0:
                    print("Touching approvals/late_gate.txt to release ITEM-GATE...", flush=True)
                    (PLAN_DIR / "approvals" / "late_gate.txt").write_text("APPROVED\n")
                    gate_touched = True

                # Step 2: Mid-run kill when ITEM-LONG-WORKER is running
                if not runner_killed and st_long == "running":
                    long_pid = st.get("ITEM-LONG-WORKER", {}).get("pid")
                    if long_pid and is_pid_alive(long_pid):
                        # Let it run for 10s of real hashing output
                        print(f"ITEM-LONG-WORKER running (pid {long_pid}); waiting 10s for real output before runner kill...", flush=True)
                        time.sleep(10.0)
                        print(f"Killing RUNNER 1 (pid {runner1_pid}) mid-run via SIGKILL...", flush=True)
                        os.kill(runner1_pid, signal.SIGKILL)
                        with contextlib.suppress(Exception):
                            runner1.wait(timeout=2)
                        runner_killed = True
                        surviving_long_pid = long_pid
                        print(f"Runner 1 dead. Checking long worker pid {surviving_long_pid}: alive={is_pid_alive(surviving_long_pid)}", flush=True)

                        # Restart runner
                        time.sleep(1.0)
                        print("Restarting runner as Runner 2...", flush=True)
                        runner2 = subprocess.Popen(
                            [sys.executable, str(SRC_V2), "--run", str(PLAN_DIR)],
                            cwd=PLAN_DIR,
                            env=env,
                            stdout=open(PLAN_DIR / "runner2.log", "wb"),  # noqa: SIM115
                            stderr=subprocess.STDOUT,
                            start_new_session=True,
                        )
                        runner2_pid = runner2.pid
                        print(f"Runner 2 started (pid {runner2_pid})", flush=True)
            except Exception:
                pass

        if runner_killed and runner2 and not adopted_long_worker:
            log2 = PLAN_DIR / "runner2.log"
            if log2.exists():
                txt = log2.read_text(errors="ignore")
                if f"ADOPTED surviving worker pid {surviving_long_pid}" in txt:
                    adopted_long_worker = True
                    print(f"Runner 2 ADOPTED surviving worker pid {surviving_long_pid}!", flush=True)

        active_runner = runner2 if runner2 else runner1
        if active_runner.poll() is not None and runner_killed:
            print("Runner 2 exited cleanly.", flush=True)
            break

        time.sleep(1.0)

    # Let any strays settle
    time.sleep(1.0)

    # Inspect all items
    final_st = {}
    if state_file.exists():
        final_st = json.loads(state_file.read_text()).get("items", {})

    all_done = all(final_st.get(iid, {}).get("status") == "done" for iid in ("ITEM-TRIVIAL-1", "ITEM-TRIVIAL-2", "ITEM-GATE", "ITEM-LONG-WORKER"))

    # Check attestations
    attestations = {}
    for iid in ("ITEM-TRIVIAL-1", "ITEM-TRIVIAL-2", "ITEM-GATE", "ITEM-LONG-WORKER"):
        att_file = PLAN_DIR / "raw" / iid / "attest.json"
        if att_file.exists():
            with contextlib.suppress(Exception):
                attestations[iid] = json.loads(att_file.read_text())

    # Read ledger
    ledger_rows = []
    ledger_file = ROOT / "spend.jsonl"
    if ledger_file.exists():
        for line in ledger_file.read_text().splitlines():
            if line.strip():
                with contextlib.suppress(Exception):
                    ledger_rows.append(json.loads(line))

    total_elapsed = round(time.time() - t0, 2)
    receipt = {
        "benchmark": "R8-V2-REAL-SMOKE 4-Item Toy Plan End-to-End",
        "timestamp": time.time(),
        "total_elapsed_s": total_elapsed,
        "all_items_done": all_done,
        "gate_touched": gate_touched,
        "runner_killed_mid_run": runner_killed,
        "surviving_long_worker_pid": surviving_long_pid,
        "adopted_long_worker": adopted_long_worker,
        "final_items": final_st,
        "attestations": attestations,
        "ledger_rows_count": len(ledger_rows),
    }

    out_file = ROOT / "receipts" / "toy_plan_smoke_receipt.json"
    out_file.write_text(json.dumps(receipt, indent=2))
    print(f"\nReceipt written to {out_file}", flush=True)

    dest_file = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/infra/raw/R8-V2-REAL-SMOKE/receipts/toy_plan_smoke_receipt.json")
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    dest_file.write_text(json.dumps(receipt, indent=2))
    print(f"Receipt mirrored to {dest_file}", flush=True)

    print(f"\nFinal Verdict: all_done={all_done} adopted_long_worker={adopted_long_worker} elapsed={total_elapsed}s", flush=True)


if __name__ == "__main__":
    main()
