#!/usr/bin/env python3
"""run_trials_b_c_d_e.py: Worker kill, Late gate, Quota parking, and Process hygiene.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/R8")
SRC_V2 = ROOT / "ds_squad_v2.py"
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
    pids = []
    pattern = f"omp -p.*{run_name}" if run_name else "omp -p.*R8"
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


def clean_strays(run_name: str | None = None) -> list[int]:
    """Find and kill any stray omp processes belonging to R8 runs."""
    killed = []
    pids = get_omp_children(run_name)
    for p in pids:
        kill_process(p)
        killed.append(p)
    return killed


# ==========================================
# TRIAL B: WORKER KILL & CONTINUATION RETRY
# ==========================================
def run_trial_b_single(trial_idx: int, seed: int) -> dict:
    run_dir = ROOT / "trial_b_worker_kill" / f"trial_{trial_idx:02d}_seed_{seed}"
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
                "id": "ITEM-WORKER-KILL",
                "title": f"Worker kill trial {trial_idx}",
                "brief": "briefs/ITEM-WORKER-KILL.md",
                "thinking": "low",
                "max_minutes": 3,
                "max_usd": 1.0,
                "worktree": False,
                "retry": 1,
                "tools": "read,bash,edit,write,grep,glob,todo",
            }
        ],
    }
    (run_dir / "plan.json").write_text(json.dumps(plan, indent=1) + "\n")

    brief_content = """# TASK: Worker kill resilient task

You are a worker executing ITEM-WORKER-KILL.
Check if this is a continuation (look for prompt-r1.md or previous attempt notes).
If this is attempt 0:
  Run bash tool: `python3 -c "import time; time.sleep(8); print('attempt 0 done')"`
  Write `{RAW}/report.md` ending with `DS-DONE: ITEM-WORKER-KILL`.
If this is attempt 1 (continuation):
  Write `{RAW}/report.md` ending with `DS-DONE: ITEM-WORKER-KILL`.
"""
    (run_dir / "briefs" / "ITEM-WORKER-KILL.md").write_text(brief_content)

    t_start = time.time()
    env = {**os.environ, "EVALLAB_DISABLE_LOCAL_DOCKER": "1"}
    runner = subprocess.Popen(
        [sys.executable, str(SRC_V2), "--run", str(run_dir)],
        cwd=run_dir,
        env=env,
        stdout=open(run_dir / "runner.log", "wb"),  # noqa: SIM115
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    runner_pid = runner.pid

    # Wait for first worker process to appear
    initial_worker_pid = None
    state_file = run_dir / "state.json"
    wait_deadline = time.time() + 25.0
    while time.time() < wait_deadline:
        if state_file.exists():
            try:
                st = json.loads(state_file.read_text()).get("items", {}).get("ITEM-WORKER-KILL", {})
                if st.get("status") == "running" and st.get("pid"):
                    p = st["pid"]
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

    if not initial_worker_pid:
        kill_process(runner_pid)
        raise RuntimeError(f"Trial B-{trial_idx}: Worker 0 failed to start")

    # Sleep 1.5 seconds into the 8s sleep, then kill worker with SIGKILL
    time.sleep(1.5)
    time.time()
    os.kill(initial_worker_pid, signal.SIGKILL)

    # Wait 0.3s and verify dead or reaped
    time.sleep(0.5)

    # Monitor for retry launch (attempt 1)
    retry_worker_pid = None
    prompt_r1_created = False
    retry_deadline = time.time() + 35.0
    while time.time() < retry_deadline:
        if (run_dir / "raw" / "ITEM-WORKER-KILL" / "prompt-r1.md").exists():
            prompt_r1_created = True

        pids = get_omp_children(run_dir.name)
        for p in pids:
            if p != initial_worker_pid and is_pid_alive(p):
                retry_worker_pid = p
                break

        if runner.poll() is not None:
            break
        time.sleep(0.2)

    try:
        runner.wait(timeout=15)
    except Exception:
        kill_process(runner_pid)

    # Check results
    report_file = run_dir / "raw" / "ITEM-WORKER-KILL" / "report.md"
    rep_text = report_file.read_text(errors="ignore").strip() if report_file.exists() else ""
    rep_lines = [line.strip() for line in rep_text.splitlines() if line.strip()]
    last_line = rep_lines[-1] if rep_lines else ""
    terminal_done = last_line.startswith("DS-DONE:")

    # Read state
    final_st = {}
    if state_file.exists():
        with contextlib.suppress(Exception):
            final_st = json.loads(state_file.read_text()).get("items", {}).get("ITEM-WORKER-KILL", {})

    # Read ledger
    ledger_rows = []
    if ledger_file.exists():
        for line in ledger_file.read_text().splitlines():
            if line.strip():
                with contextlib.suppress(Exception):
                    ledger_rows.append(json.loads(line))

    clean_strays(run_dir.name)

    return {
        "trial_idx": trial_idx,
        "seed": seed,
        "worker0_pid": initial_worker_pid,
        "worker0_killed": True,
        "prompt_r1_created": prompt_r1_created,
        "retry_worker_pid": retry_worker_pid,
        "retry_launched": retry_worker_pid is not None,
        "final_status": final_st.get("status"),
        "final_attempt": final_st.get("attempt"),
        "terminal_done": terminal_done,
        "ledger_rows_count": len(ledger_rows),
        "success": terminal_done and final_st.get("status") == "done" and final_st.get("attempt") == 1,
        "total_duration_s": round(time.time() - t_start, 2),
    }


def run_trial_b() -> list[dict]:
    print("\n=== Running Trial B: Worker Kill (3 trials) ===", flush=True)
    results = []
    seeds = [3001, 3002, 3003]
    for i, seed in enumerate(seeds, start=1):
        res = run_trial_b_single(i, seed)
        results.append(res)
        print(
            f"  [Trial B {i}/3] seed={seed} worker0_killed={res['worker0_killed']} "
            f"prompt_r1={res['prompt_r1_created']} retry_launched={res['retry_launched']} "
            f"final_status={res['final_status']} attempt={res['final_attempt']} "
            f"done={res['terminal_done']} dur={res['total_duration_s']}s",
            flush=True,
        )
    return results


# ==========================================
# TRIAL C: LATE GATE FILE RELEASE
# ==========================================
def run_trial_c() -> dict:
    print("\n=== Running Trial C: Late Gate File Creation ===", flush=True)
    run_dir = ROOT / "trial_c_late_gate"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "briefs").mkdir(parents=True, exist_ok=True)
    (run_dir / "approvals").mkdir(parents=True, exist_ok=True)
    (run_dir / "raw").mkdir(parents=True, exist_ok=True)
    (run_dir / "sessions").mkdir(parents=True, exist_ok=True)

    shutil.copy(WORKER_YML, run_dir / "worker.yml")
    shutil.copy(COMMON_MD, run_dir / "common.md")
    shutil.copy(STATS_PY, run_dir / "stats.py")

    gate_rel_path = "approvals/late_gate.txt"
    gate_abs_path = run_dir / gate_rel_path
    ledger_file = run_dir / "spend.jsonl"

    plan = {
        "model": "google-antigravity/gemini-3.8-flash",
        "subscription": True,
        "budget_usd": 20,
        "max_parallel": 2,
        "linger_minutes": 0,
        "wait_gates_minutes": 5,
        "no_git": True,
        "ledger_path": str(ledger_file),
        "items": [
            {
                "id": "ITEM-LATE-GATE",
                "title": "Late gate release trial",
                "brief": "briefs/ITEM-LATE-GATE.md",
                "thinking": "low",
                "max_minutes": 3,
                "max_usd": 1.0,
                "worktree": False,
                "retry": 0,
                "gate": gate_rel_path,
                "tools": "read,bash,edit,write,grep,glob,todo",
            }
        ],
    }
    (run_dir / "plan.json").write_text(json.dumps(plan, indent=1) + "\n")

    brief_content = """# TASK: Gate item task

Gate released. Write `{RAW}/report.md` ending with `DS-DONE: ITEM-LATE-GATE`.
"""
    (run_dir / "briefs" / "ITEM-LATE-GATE.md").write_text(brief_content)

    t_start = time.time()
    env = {**os.environ, "EVALLAB_DISABLE_LOCAL_DOCKER": "1"}
    runner = subprocess.Popen(
        [sys.executable, str(SRC_V2), "--run", str(run_dir)],
        cwd=run_dir,
        env=env,
        stdout=open(run_dir / "runner.log", "wb"),  # noqa: SIM115
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    runner_pid = runner.pid

    # Verify status is waiting-gate
    state_file = run_dir / "state.json"
    saw_waiting_gate = False
    for _ in range(25):
        if state_file.exists():
            try:
                st = json.loads(state_file.read_text()).get("items", {}).get("ITEM-LATE-GATE", {})
                if st.get("status") == "waiting-gate":
                    saw_waiting_gate = True
                    break
            except Exception:
                pass
        time.sleep(0.2)

    # Verify worker NOT running before gate
    omp_before_gate = get_omp_children(run_dir.name)
    no_worker_before_gate = (len(omp_before_gate) == 0)

    # Sleep 3 seconds, then touch gate file
    time.sleep(3.0)
    t_gate_touched = time.time()
    gate_abs_path.write_text("APPROVED\n")

    # Monitor when worker starts
    t_worker_started = None
    deadline = time.time() + 25.0
    while time.time() < deadline:
        if state_file.exists():
            try:
                st = json.loads(state_file.read_text()).get("items", {}).get("ITEM-LATE-GATE", {})
                if st.get("status") == "running" and st.get("pid"):
                    p = st["pid"]
                    if is_pid_alive(p):
                        t_worker_started = time.time()
                        break
            except Exception:
                pass
        pids = get_omp_children(run_dir.name)
        if pids:
            pids[0]
            t_worker_started = time.time()
            break
        time.sleep(0.2)

    try:
        runner.wait(timeout=25)
    except Exception:
        kill_process(runner_pid)

    poll_latency = round(t_worker_started - t_gate_touched, 3) if t_worker_started else None

    # Check terminal report
    report_file = run_dir / "raw" / "ITEM-LATE-GATE" / "report.md"
    rep_text = report_file.read_text(errors="ignore").strip() if report_file.exists() else ""
    rep_lines = [line.strip() for line in rep_text.splitlines() if line.strip()]
    terminal_done = bool(rep_lines and rep_lines[-1].startswith("DS-DONE:"))

    clean_strays(run_dir.name)

    result = {
        "saw_waiting_gate": saw_waiting_gate,
        "no_worker_before_gate": no_worker_before_gate,
        "gate_touched_ts": t_gate_touched,
        "worker_started_ts": t_worker_started,
        "poll_latency_s": poll_latency,
        "started_within_one_poll": (poll_latency is not None and poll_latency <= 10.5),
        "terminal_done": terminal_done,
        "total_duration_s": round(time.time() - t_start, 2),
    }
    print(
        f"  [Trial C Result] saw_waiting_gate={saw_waiting_gate} "
        f"no_worker_before={no_worker_before_gate} poll_latency={poll_latency}s "
        f"within_one_poll={result['started_within_one_poll']} done={terminal_done}",
        flush=True,
    )
    return result


# ==========================================
# TRIAL D: QUOTA PARKING & RESUMPTION
# ==========================================
def run_trial_d() -> dict:
    print("\n=== Running Trial D: Quota Parking (Auto & Signal Resume) ===", flush=True)

    # Sub-test 1: Auto-resume with short T (5.0s cooldown)
    run_dir_auto = ROOT / "trial_d_quota_auto"
    if run_dir_auto.exists():
        shutil.rmtree(run_dir_auto)
    (run_dir_auto / "briefs").mkdir(parents=True, exist_ok=True)
    (run_dir_auto / "approvals").mkdir(parents=True, exist_ok=True)
    (run_dir_auto / "raw").mkdir(parents=True, exist_ok=True)
    (run_dir_auto / "sessions").mkdir(parents=True, exist_ok=True)

    shutil.copy(WORKER_YML, run_dir_auto / "worker.yml")
    shutil.copy(COMMON_MD, run_dir_auto / "common.md")
    shutil.copy(STATS_PY, run_dir_auto / "stats.py")

    mock_omp = run_dir_auto / "mock_omp.py"
    mock_omp_script = """#!/usr/bin/env python3
import sys, os, time
from pathlib import Path

raw_dir = None
for i, a in enumerate(sys.argv):
    if a.startswith("@"):
        prompt_path = Path(a[1:])
        raw_dir = prompt_path.parent
        break

attempt_file = raw_dir / ".mock_attempt"
attempt = 0
if attempt_file.exists():
    attempt = int(attempt_file.read_text().strip()) + 1
attempt_file.write_text(str(attempt))

worker_log = raw_dir / "worker.log"
if attempt == 0:
    with open(worker_log, "a") as f:
        f.write("Error: HTTP 429 rate limit exceeded; quota exhausted; resets in 5.0s\\n")
    sys.exit(29)
else:
    report_file = raw_dir / "report.md"
    report_file.write_text("# Report ITEM-QUOTA-AUTO\\nSuccess after quota reset.\\n\\nDS-DONE: ITEM-QUOTA-AUTO\\n")
    sys.exit(0)
"""
    mock_omp.write_text(mock_omp_script)
    mock_omp.chmod(0o755)

    fake_bin_dir = run_dir_auto / "bin"
    fake_bin_dir.mkdir(parents=True, exist_ok=True)
    fake_omp_symlink = fake_bin_dir / "omp"
    fake_omp_symlink.symlink_to(mock_omp)

    ledger_file_auto = run_dir_auto / "spend.jsonl"
    plan_auto = {
        "model": "google-antigravity/gemini-3.8-flash",
        "subscription": True,
        "budget_usd": 20,
        "max_parallel": 2,
        "linger_minutes": 0,
        "wait_gates_minutes": 0,
        "no_git": True,
        "ledger_path": str(ledger_file_auto),
        "items": [
            {
                "id": "ITEM-QUOTA-AUTO",
                "title": "Quota parking auto resume test",
                "brief": "briefs/ITEM-QUOTA-AUTO.md",
                "thinking": "low",
                "max_minutes": 3,
                "max_usd": 1.0,
                "worktree": False,
                "retry": 2,
                "tools": "read,bash",
            }
        ],
    }
    (run_dir_auto / "plan.json").write_text(json.dumps(plan_auto, indent=1) + "\n")
    (run_dir_auto / "briefs" / "ITEM-QUOTA-AUTO.md").write_text("Execute quota test task.\n")

    env_auto = {
        **os.environ,
        "PATH": f"{fake_bin_dir}:{os.environ.get('PATH', '')}",
        "EVALLAB_DISABLE_LOCAL_DOCKER": "1",
    }

    t0_auto = time.time()
    runner_auto = subprocess.Popen(
        [sys.executable, str(SRC_V2), "--run", str(run_dir_auto)],
        cwd=run_dir_auto,
        env=env_auto,
        stdout=open(run_dir_auto / "runner.log", "wb"),  # noqa: SIM115
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    state_file_auto = run_dir_auto / "state.json"
    saw_parked_quota = False
    attempts_while_parked = 0
    reset_time_parsed = None

    deadline = time.time() + 30.0
    while time.time() < deadline:
        if state_file_auto.exists():
            try:
                st = json.loads(state_file_auto.read_text()).get("items", {}).get("ITEM-QUOTA-AUTO", {})
                status = st.get("status")
                if status == "parked-quota":
                    if not saw_parked_quota:
                        saw_parked_quota = True
                        time.time()
                        reset_time_parsed = st.get("reset_time")
                    if st.get("attempt", 0) > 0:
                        attempts_while_parked += 1
            except Exception:
                pass
        if runner_auto.poll() is not None:
            break
        time.sleep(0.2)

    try:
        runner_auto.wait(timeout=10)
    except Exception:
        kill_process(runner_auto.pid)

    report_auto = run_dir_auto / "raw" / "ITEM-QUOTA-AUTO" / "report.md"
    rep_text_auto = report_auto.read_text(errors="ignore").strip() if report_auto.exists() else ""
    auto_done = rep_text_auto.endswith("DS-DONE: ITEM-QUOTA-AUTO")

    st_auto_final = {}
    if state_file_auto.exists():
        with contextlib.suppress(Exception):
            st_auto_final = json.loads(state_file_auto.read_text()).get("items", {}).get("ITEM-QUOTA-AUTO", {})

    auto_result = {
        "mode": "automatic_resume",
        "saw_parked_quota": saw_parked_quota,
        "reset_time_parsed": reset_time_parsed,
        "attempts_burned_while_parked": attempts_while_parked,
        "final_status": st_auto_final.get("status"),
        "final_attempt": st_auto_final.get("attempt"),
        "terminal_done": auto_done,
        "elapsed_s": round(time.time() - t0_auto, 2),
    }
    print(
        f"  [Quota Auto] parked={saw_parked_quota} reset_parsed={reset_time_parsed is not None} "
        f"burned_retries={attempts_while_parked} final_status={st_auto_final.get('status')} "
        f"done={auto_done} elapsed={auto_result['elapsed_s']}s",
        flush=True,
    )

    # Sub-test 2: Signal resume via RUN/RESUME with 1-hour reset
    run_dir_sig = ROOT / "trial_d_quota_signal"
    if run_dir_sig.exists():
        shutil.rmtree(run_dir_sig)
    (run_dir_sig / "briefs").mkdir(parents=True, exist_ok=True)
    (run_dir_sig / "approvals").mkdir(parents=True, exist_ok=True)
    (run_dir_sig / "raw").mkdir(parents=True, exist_ok=True)
    (run_dir_sig / "sessions").mkdir(parents=True, exist_ok=True)

    shutil.copy(WORKER_YML, run_dir_sig / "worker.yml")
    shutil.copy(COMMON_MD, run_dir_sig / "common.md")
    shutil.copy(STATS_PY, run_dir_sig / "stats.py")

    mock_omp_sig = run_dir_sig / "mock_omp.py"
    mock_omp_sig_script = """#!/usr/bin/env python3
import sys, os, time
from pathlib import Path

raw_dir = None
for i, a in enumerate(sys.argv):
    if a.startswith("@"):
        prompt_path = Path(a[1:])
        raw_dir = prompt_path.parent
        break

attempt_file = raw_dir / ".mock_attempt"
attempt = 0
if attempt_file.exists():
    attempt = int(attempt_file.read_text().strip()) + 1
attempt_file.write_text(str(attempt))

worker_log = raw_dir / "worker.log"
if attempt == 0:
    with open(worker_log, "a") as f:
        f.write("Error: HTTP 429 quota ceiling reached; resets in 3600s\\n")
    sys.exit(29)
else:
    report_file = raw_dir / "report.md"
    report_file.write_text("# Report ITEM-QUOTA-SIGNAL\\nSuccess on signal resume.\\n\\nDS-DONE: ITEM-QUOTA-SIGNAL\\n")
    sys.exit(0)
"""
    mock_omp_sig.write_text(mock_omp_sig_script)
    mock_omp_sig.chmod(0o755)

    fake_bin_sig = run_dir_sig / "bin"
    fake_bin_sig.mkdir(parents=True, exist_ok=True)
    (fake_bin_sig / "omp").symlink_to(mock_omp_sig)

    ledger_file_sig = run_dir_sig / "spend.jsonl"
    plan_sig = {
        "model": "google-antigravity/gemini-3.8-flash",
        "subscription": True,
        "budget_usd": 20,
        "max_parallel": 2,
        "linger_minutes": 0,
        "wait_gates_minutes": 0,
        "no_git": True,
        "ledger_path": str(ledger_file_sig),
        "items": [
            {
                "id": "ITEM-QUOTA-SIGNAL",
                "title": "Quota parking signal resume test",
                "brief": "briefs/ITEM-QUOTA-SIGNAL.md",
                "thinking": "low",
                "max_minutes": 3,
                "max_usd": 1.0,
                "worktree": False,
                "retry": 2,
                "tools": "read,bash",
            }
        ],
    }
    (run_dir_sig / "plan.json").write_text(json.dumps(plan_sig, indent=1) + "\n")
    (run_dir_sig / "briefs" / "ITEM-QUOTA-SIGNAL.md").write_text("Execute signal quota test.\n")

    env_sig = {
        **os.environ,
        "PATH": f"{fake_bin_sig}:{os.environ.get('PATH', '')}",
        "EVALLAB_DISABLE_LOCAL_DOCKER": "1",
    }

    t0_sig = time.time()
    runner_sig = subprocess.Popen(
        [sys.executable, str(SRC_V2), "--run", str(run_dir_sig)],
        cwd=run_dir_sig,
        env=env_sig,
        stdout=open(run_dir_sig / "runner.log", "wb"),  # noqa: SIM115
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    state_file_sig = run_dir_sig / "state.json"
    saw_parked_sig = False
    for _ in range(25):
        if state_file_sig.exists():
            try:
                st = json.loads(state_file_sig.read_text()).get("items", {}).get("ITEM-QUOTA-SIGNAL", {})
                if st.get("status") == "parked-quota":
                    saw_parked_sig = True
                    break
            except Exception:
                pass
        time.sleep(0.2)

    time.sleep(2.0)
    time.time()
    (run_dir_sig / "RESUME").touch()

    try:
        runner_sig.wait(timeout=25)
    except Exception:
        kill_process(runner_sig.pid)

    report_sig = run_dir_sig / "raw" / "ITEM-QUOTA-SIGNAL" / "report.md"
    rep_text_sig = report_sig.read_text(errors="ignore").strip() if report_sig.exists() else ""
    sig_done = rep_text_sig.endswith("DS-DONE: ITEM-QUOTA-SIGNAL")

    resume_file_consumed = not (run_dir_sig / "RESUME").exists()

    st_sig_final = {}
    if state_file_sig.exists():
        with contextlib.suppress(Exception):
            st_sig_final = json.loads(state_file_sig.read_text()).get("items", {}).get("ITEM-QUOTA-SIGNAL", {})

    sig_result = {
        "mode": "signal_resume",
        "saw_parked_quota": saw_parked_sig,
        "resume_file_consumed": resume_file_consumed,
        "final_status": st_sig_final.get("status"),
        "final_attempt": st_sig_final.get("attempt"),
        "terminal_done": sig_done,
        "elapsed_s": round(time.time() - t0_sig, 2),
    }
    print(
        f"  [Quota Signal] parked={saw_parked_sig} resume_consumed={resume_file_consumed} "
        f"final_status={st_sig_final.get('status')} done={sig_done} elapsed={sig_result['elapsed_s']}s",
        flush=True,
    )

    clean_strays(run_dir_auto.name)
    clean_strays(run_dir_sig.name)

    return {
        "auto_resume": auto_result,
        "signal_resume": sig_result,
        "pass": auto_result["terminal_done"] and sig_result["terminal_done"] and auto_result["attempts_burned_while_parked"] == 0,
    }


# ==========================================
# TRIAL E: PROCESS HYGIENE & CLEANUP
# ==========================================
def run_trial_e() -> dict:
    print("\n=== Running Trial E: Process Hygiene Check (R8-owned) ===", flush=True)
    stray_omp = []
    try:
        out = subprocess.run(["pgrep", "-fl", "omp -p.*R8"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if line.strip():
                stray_omp.append(line.strip())
    except Exception:
        pass

    stray_runners = []
    try:
        out = subprocess.run(["pgrep", "-fl", "ds_squad.*R8"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if line.strip():
                stray_runners.append(line.strip())
    except Exception:
        pass

    stray_sleepers = []
    try:
        out = subprocess.run(["pgrep", "-fl", "python.*R8"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if line.strip() and "run_trials" not in line:
                stray_sleepers.append(line.strip())
    except Exception:
        pass

    result = {
        "stray_omp_found": len(stray_omp),
        "stray_runners_found": len(stray_runners),
        "stray_sleepers_found": len(stray_sleepers),
        "stray_omp_list": stray_omp,
        "stray_runners_list": stray_runners,
        "stray_sleepers_list": stray_sleepers,
        "clean_zero_strays": (len(stray_omp) == 0 and len(stray_runners) == 0 and len(stray_sleepers) == 0),
    }
    print(
        f"  [Trial E Result] R8 stray_omp={result['stray_omp_found']} "
        f"stray_runners={result['stray_runners_found']} stray_sleepers={result['stray_sleepers_found']} "
        f"clean={result['clean_zero_strays']}",
        flush=True,
    )
    return result


def main():
    print("Starting Trials B, C, D, E suite...", flush=True)
    t0 = time.time()

    b_results = run_trial_b()
    c_result = run_trial_c()
    d_result = run_trial_d()
    e_result = run_trial_e()

    receipt = {
        "benchmark": "R8-V2-REAL-SMOKE Trials B, C, D, E",
        "timestamp": time.time(),
        "total_elapsed_s": round(time.time() - t0, 2),
        "trial_b_worker_kill": {
            "trials_run": len(b_results),
            "passed": sum(1 for r in b_results if r["success"]),
            "rate": f"{sum(1 for r in b_results if r['success'])}/{len(b_results)}",
            "trials": b_results,
        },
        "trial_c_late_gate": c_result,
        "trial_d_quota_parking": d_result,
        "trial_e_process_hygiene": e_result,
    }

    out_file = ROOT / "receipts" / "trials_b_c_d_e_receipt.json"
    out_file.write_text(json.dumps(receipt, indent=2))
    print(f"\nReceipt written to {out_file}", flush=True)

    dest_file = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/infra/raw/R8-V2-REAL-SMOKE/receipts/trials_b_c_d_e_receipt.json")
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    dest_file.write_text(json.dumps(receipt, indent=2))
    print(f"Receipt mirrored to {dest_file}", flush=True)


if __name__ == "__main__":
    main()
