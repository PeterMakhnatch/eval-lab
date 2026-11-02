#!/usr/bin/env python3
"""run_trial_a.py: Runner kill resilience test with real omp workers.
10 trials of ds_squad_v2.py vs 10 trials of ds_squad_v1.py.
"""
from __future__ import annotations

import contextlib
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/R8")
SRC_V2 = ROOT / "ds_squad_v2.py"
SRC_V1 = ROOT / "ds_squad_v1.py"
WORKER_YML = ROOT / "toy_plan" / "worker.yml"
COMMON_MD = ROOT / "toy_plan" / "common.md"
STATS_PY = ROOT / "toy_plan" / "stats.py"


def is_pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


def get_omp_children(run_name: str | None = None) -> list[int]:
    """Find omp worker PIDs scoped to run_name if given."""
    pids = []
    pattern = f"omp -p.*{run_name}" if run_name else "omp -p.*ITEM-SMOKE"
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


def run_single_trial(version: str, trial_idx: int, seed: int) -> dict:
    rng = random.Random(seed)
    run_dir = ROOT / f"trial_a_{version}" / f"trial_{trial_idx:02d}_seed_{seed}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "briefs").mkdir(parents=True, exist_ok=True)
    (run_dir / "approvals").mkdir(parents=True, exist_ok=True)
    (run_dir / "raw").mkdir(parents=True, exist_ok=True)
    (run_dir / "sessions").mkdir(parents=True, exist_ok=True)

    shutil.copy(WORKER_YML, run_dir / "worker.yml")
    shutil.copy(COMMON_MD, run_dir / "common.md")
    shutil.copy(STATS_PY, run_dir / "stats.py")

    ledger_file = run_dir / "spend.jsonl"
    plan = {
        "model": "google-antigravity/gemini-3.8-flash",
        "subscription": True,
        "budget_usd": 20,
        "max_parallel": 2,
        "linger_minutes": 0,
        "wait_gates_minutes": 0,
        "no_git": True,
        "ledger_path": str(ledger_file),
        "items": [
            {
                "id": "ITEM-SMOKE",
                "title": f"Runner kill smoke trial {trial_idx}",
                "brief": "briefs/ITEM-SMOKE.md",
                "thinking": "low",
                "max_minutes": 3,
                "max_usd": 1.0,
                "worktree": False,
                "retry": 0,
                "tools": "read,bash,edit,write,grep,glob,todo",
            }
        ],
    }
    (run_dir / "plan.json").write_text(json.dumps(plan, indent=1) + "\n")

    brief_content = """# TASK: Smoke worker task

You are a worker executing ITEM-SMOKE.
Execute these steps strictly in order:
1. Use the bash tool to run: `python3 -c "import time; time.sleep(6); print('work done')"`
2. Use the write tool to write `{RAW}/report.md` with content:
```markdown
# Report ITEM-SMOKE
Task completed.

DS-DONE: ITEM-SMOKE
```
The last line of `{RAW}/report.md` MUST be exactly `DS-DONE: ITEM-SMOKE`.
"""
    (run_dir / "briefs" / "ITEM-SMOKE.md").write_text(brief_content)

    runner_script = SRC_V2 if version == "v2" else SRC_V1
    runner_cmd = [sys.executable, str(runner_script), "--run", str(run_dir)]
    if version == "v1":
        runner_cmd.extend(["--tag", "-v1"])

    t_start = time.time()
    env = {**os.environ, "EVALLAB_DISABLE_LOCAL_DOCKER": "1"}
    runner1 = subprocess.Popen(
        runner_cmd,
        cwd=run_dir,
        env=env,
        stdout=open(run_dir / "runner1.log", "wb"),  # noqa: SIM115
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    runner1_pid = runner1.pid

    # Wait for worker process to appear
    initial_worker_pid = None
    state_file = run_dir / "state.json"

    wait_deadline = time.time() + 25.0
    while time.time() < wait_deadline:
        if version == "v2" and state_file.exists():
            try:
                st_data = json.loads(state_file.read_text())
                it_st = st_data.get("items", {}).get("ITEM-SMOKE", {})
                if it_st.get("status") == "running" and it_st.get("pid"):
                    p = it_st["pid"]
                    if is_pid_alive(p):
                        initial_worker_pid = p
                        break
            except Exception:
                pass
        pids = get_omp_children(run_dir.name)
        if pids:
            initial_worker_pid = pids[0]
            break
        time.sleep(0.2)

    if not initial_worker_pid or not is_pid_alive(initial_worker_pid):
        kill_process(runner1_pid)
        raise RuntimeError(f"Trial {trial_idx} ({version}): worker failed to start within timeout")

    # Let the worker run for a random seeded delay (1.0 to 2.5s) while executing sleep(6)
    kill_delay = rng.uniform(1.0, 2.5)
    time.sleep(kill_delay)

    # Verify worker still alive before kill
    worker_alive_before_kill = is_pid_alive(initial_worker_pid)
    if not worker_alive_before_kill:
        kill_process(runner1_pid)
        raise RuntimeError(f"Trial {trial_idx} ({version}): worker died before runner kill")

    t_kill = time.time()
    # KILL RUNNER ONLY with SIGKILL
    os.kill(runner1_pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        runner1.wait(timeout=2)

    not is_pid_alive(runner1_pid)
    worker_survived = is_pid_alive(initial_worker_pid)

    # Restart runner
    t_restart = time.time()
    runner2 = subprocess.Popen(
        runner_cmd,
        cwd=run_dir,
        env=env,
        stdout=open(run_dir / "runner2.log", "wb"),  # noqa: SIM115
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    runner2_pid = runner2.pid

    # Monitor adoption / duplicate spawn
    second_worker_pid = None
    adopted = False
    overlap_start = None
    overlap_end = None
    overlap_s = 0.0

    monitor_deadline = time.time() + 45.0
    while time.time() < monitor_deadline:
        current_workers = get_omp_children(run_dir.name)
        for w in current_workers:
            if w != initial_worker_pid and second_worker_pid is None:
                second_worker_pid = w
                overlap_start = time.time()

        if second_worker_pid and is_pid_alive(initial_worker_pid) and is_pid_alive(second_worker_pid):
            overlap_end = time.time()

        if version == "v2" and not adopted:
            log2 = run_dir / "runner2.log"
            if log2.exists():
                txt = log2.read_text(errors="ignore")
                if f"ADOPTED surviving worker pid {initial_worker_pid}" in txt:
                    adopted = True

        if runner2.poll() is not None:
            break
        time.sleep(0.2)

    try:
        runner2.wait(timeout=10)
    except Exception:
        kill_process(runner2_pid)

    # Let any lingering processes settle
    time.sleep(0.5)
    t_end = time.time()

    if overlap_start and overlap_end:
        overlap_s = round(overlap_end - overlap_start, 3)

    # Inspect results
    report_file = run_dir / "raw" / "ITEM-SMOKE" / "report.md"
    rep_text = report_file.read_text(errors="ignore").strip() if report_file.exists() else ""
    rep_lines = [line.strip() for line in rep_text.splitlines() if line.strip()]
    last_line = rep_lines[-1] if rep_lines else ""
    terminal_done = last_line.startswith("DS-DONE:")

    # Read ledger
    ledger_rows = []
    if ledger_file.exists():
        for line in ledger_file.read_text().splitlines():
            if line.strip():
                with contextlib.suppress(Exception):
                    ledger_rows.append(json.loads(line))

    # Read attestation
    attest_file = run_dir / "raw" / "ITEM-SMOKE" / "attest.json"
    attest_data = {}
    if attest_file.exists():
        with contextlib.suppress(Exception):
            attest_data = json.loads(attest_file.read_text())

    # Clean up any leftover omp workers
    for p in get_omp_children(run_dir.name):
        kill_process(p)

    recovery_time = round(t_restart - t_kill, 3)
    total_duration = round(t_end - t_start, 2)

    result = {
        "version": version,
        "trial_idx": trial_idx,
        "seed": seed,
        "runner1_pid": runner1_pid,
        "runner2_pid": runner2_pid,
        "initial_worker_pid": initial_worker_pid,
        "worker_survived_kill": worker_survived,
        "adopted": adopted,
        "second_worker_pid": second_worker_pid,
        "duplicate_workers": 1 if second_worker_pid else 0,
        "total_workers_run": 2 if second_worker_pid else 1,
        "process_overlap_s": overlap_s,
        "recovery_time_s": recovery_time,
        "total_duration_s": total_duration,
        "terminal_done": terminal_done,
        "ledger_row_count": len(ledger_rows),
        "terminal_status_reached_once": (len(ledger_rows) == 1 and terminal_done),
        "attestation": attest_data,
        "requested_model": plan["model"],
        "served_model": attest_data.get("served", {}),
        "fallback_events": attest_data.get("fallback_events", 0),
        "off_route": attest_data.get("off_route", {}),
    }
    return result


def main():
    trials_to_run = 10
    if len(sys.argv) > 1:
        trials_to_run = int(sys.argv[1])

    seeds = [2001 + i * 17 for i in range(trials_to_run)]
    print(f"Starting Trial A: {trials_to_run} trials v2 vs {trials_to_run} trials v1 with seeds {seeds}...", flush=True)

    v2_results = []
    print("\n=== Running ds_squad_v2.py (Phase 0 Hardened) ===", flush=True)
    for i, seed in enumerate(seeds, start=1):
        res = run_single_trial("v2", i, seed)
        v2_results.append(res)
        print(
            f"  [v2 trial {i:02d}/{trials_to_run:02d}] seed={seed} adopted={res['adopted']} "
            f"duplicates={res['duplicate_workers']} overlap={res['process_overlap_s']}s "
            f"done={res['terminal_done']} ledger_rows={res['ledger_row_count']} "
            f"dur={res['total_duration_s']}s",
            flush=True,
        )

    v1_results = []
    print("\n=== Running ds_squad_v1.py (Original Baseline) ===", flush=True)
    for i, seed in enumerate(seeds, start=1):
        res = run_single_trial("v1", i, seed)
        v1_results.append(res)
        print(
            f"  [v1 trial {i:02d}/{trials_to_run:02d}] seed={seed} adopted={res['adopted']} "
            f"duplicates={res['duplicate_workers']} overlap={res['process_overlap_s']}s "
            f"done={res['terminal_done']} ledger_rows={res['ledger_row_count']} "
            f"dur={res['total_duration_s']}s",
            flush=True,
        )

    receipt = {
        "benchmark": "R8-V2-REAL-SMOKE Trial A: Runner Kill Resilience",
        "timestamp": time.time(),
        "trials_count": trials_to_run,
        "seeds": seeds,
        "v2_summary": {
            "adoption_rate": f"{sum(1 for r in v2_results if r['adopted'])}/{trials_to_run}",
            "duplicate_rate": f"{sum(1 for r in v2_results if r['duplicate_workers'] > 0)}/{trials_to_run}",
            "total_duplicates": sum(r["duplicate_workers"] for r in v2_results),
            "max_overlap_s": max((r["process_overlap_s"] for r in v2_results), default=0.0),
            "single_terminal_row_rate": f"{sum(1 for r in v2_results if r['terminal_status_reached_once'])}/{trials_to_run}",
        },
        "v1_summary": {
            "adoption_rate": f"{sum(1 for r in v1_results if r['adopted'])}/{trials_to_run}",
            "duplicate_rate": f"{sum(1 for r in v1_results if r['duplicate_workers'] > 0)}/{trials_to_run}",
            "total_duplicates": sum(r["duplicate_workers"] for r in v1_results),
            "max_overlap_s": max((r["process_overlap_s"] for r in v1_results), default=0.0),
            "single_terminal_row_rate": f"{sum(1 for r in v1_results if r['terminal_status_reached_once'])}/{trials_to_run}",
        },
        "v2_trials": v2_results,
        "v1_trials": v1_results,
    }

    out_json = ROOT / "receipts" / "trial_a_runner_kill.json"
    out_json.write_text(json.dumps(receipt, indent=2))
    print(f"\nReceipt written to {out_json}", flush=True)

    dest_receipt = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/infra/raw/R8-V2-REAL-SMOKE/receipts/trial_a_runner_kill.json")
    dest_receipt.parent.mkdir(parents=True, exist_ok=True)
    dest_receipt.write_text(json.dumps(receipt, indent=2))
    print(f"Receipt mirrored to {dest_receipt}", flush=True)


if __name__ == "__main__":
    main()
