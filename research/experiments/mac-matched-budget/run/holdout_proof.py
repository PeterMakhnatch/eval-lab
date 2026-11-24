import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RUN = Path(__file__).resolve().parent
BESPOKE = Path.home() / "Developer/bespoke-macbench-lab"
B_PYTHON = BESPOKE / ".venv/bin/python"
BUILD_DEFAULT = Path.home() / "Developer/muster-runs/day-2026-11-24/mac-run/build"
SCRATCH = Path("/tmp/fable-day/macrun")
SEAT_SCRIPT = RUN / "holdout_seat.py"
LAUNCHER = RUN / "matched_trial.py"
CHECKS = ("O3", "O4", "O5", "game-call-log")
TRIALS = {
    "o4o5-silent": {"arm": "silent", "n_agents": 2, "scenario": "o4o5", "timeout_sec": 150},
    "o4o5-team": {"arm": "team", "n_agents": 2, "scenario": "o4o5", "timeout_sec": 150},
    "o5-solo": {"arm": "solo", "n_agents": 1, "scenario": "o5", "timeout_sec": 65},
    "o3": {"arm": "silent", "n_agents": 2, "scenario": "o3", "timeout_sec": 90},
}
NEEDS = {"O3": ("o3",), "O4": ("o4o5-silent", "o4o5-team"), "O5": ("o4o5-silent", "o4o5-team", "o5-solo"),
         "game-call-log": ("o4o5-silent", "o4o5-team")}
SETUP_DEADLINE_SEC = 480
SETUP_TERM_GRACE_SEC = 20
TRIAL_TOTAL_SEC = 600
TERM_GRACE_SEC = 120
KILL_GRACE_SEC = 20
CLEANUP_BUDGET_SEC = 60
TRIAL_RUN_SEC = TRIAL_TOTAL_SEC - TERM_GRACE_SEC - KILL_GRACE_SEC - CLEANUP_BUDGET_SEC
DOCKER_CMD_SEC = 30
MAX_JSONL_LINES = 200_000
FREEZE_LEFT_SEC = 30
MIN_IDLE_WORLD_SEC = 20
REACH_TILES = 1.5
MOVED_TILES = 3
AFTER_STOP_SLACK_SEC = 1.0
COMMS_TEXT = "holdout-seat probe"
COMMS_OPS = frozenset({"say", "shout", "radio", "post_note", "read_notes"})
NO_WORLD_OPS = frozenset({"wait"}) | COMMS_OPS
SCRIPT_OPS = frozenset({"wait", "move_to", "say", "radio"})
TEAM_PROBES = ("team_read_ls_remote", "team_read_fetch", "team_write_push_branch", "team_write_push_main")
CONTROL_PROBES = ("commit", "control_init", "control_write_push", "control_read_ls_remote", "control_read_fetch")
SECRET_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
WAVE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}")
B_SEAL_NOTE = ("B's seal_git alone installs only a pre-receive hook on /srv/git/team.git (run_hardened.py:157-162): "
               "it refuses pushes but not fetch or ls-remote. Read denial here comes from the launcher's token "
               "revocation after the setup clone.")
FIX_TEXT = ("Close it outside B: at stop() and when a session ends, kill every process in the seat sandbox except "
            "PID 1 (or every process of the agent user), and revoke the seat's board token on the hub; the "
            "time_up freeze refuses act (409) but still answers reads.")


def read_jsonl(path):
    path = Path(path)
    if not path.is_file():
        return []
    rows = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for i, line in enumerate(handle):
            assert i < MAX_JSONL_LINES, path
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def command(args, timeout, cwd=None):
    assert args and timeout > 0
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"rc": None, "out": "", "err": type(error).__name__}
    return {"rc": r.returncode, "out": r.stdout, "err": r.stderr[-2000:]}


def docker_rows(kind):
    assert kind in ("ps", "network", "volume")
    args = {"ps": ["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"],
            "network": ["docker", "network", "ls", "--no-trunc", "--format", "{{json .}}"],
            "volume": ["docker", "volume", "ls", "--format", "{{json .}}"]}[kind]
    r = command(args, DOCKER_CMD_SEC)
    if r["rc"] != 0:
        return None
    rows = []
    for line in r["out"].splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def docker_inventory():
    ps = docker_rows("ps")
    networks = docker_rows("network")
    volumes = docker_rows("volume")
    return {"containers": None if ps is None else {r.get("ID"): {"names": r.get("Names"), "state": r.get("State"),
                                                                 "image": r.get("Image")} for r in ps},
            "networks": None if networks is None else sorted(r.get("ID") for r in networks),
            "volumes": None if volumes is None else sorted(r.get("Name") for r in volumes)}


def scrubbed_env():
    env = {k: v for k, v in os.environ.items()
           if not k.endswith(SECRET_SUFFIXES) and k not in ("MAC_MATCHED_PAID", "PYTHONPATH")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def build_dir():
    raw = os.environ.get("HOLDOUT_PROOF_BUILD_DIR")
    return Path(raw).expanduser().resolve() if raw else BUILD_DEFAULT


def preflight(output):
    problems = []
    if not B_PYTHON.is_file():
        problems.append(f"B interpreter missing: {B_PYTHON}")
    for path in (LAUNCHER, SEAT_SCRIPT):
        if not path.is_file():
            problems.append(f"missing {path.name}")
    build = build_dir()
    if not (build / "manifest.json").is_file():
        problems.append(f"build folder has no manifest.json ({build}); run matched_trial.py prepare first")
    for arm in ("silent", "team"):
        if not (build / f"holdout-{arm}" / "task" / "environment" / "Dockerfile").is_file():
            problems.append(f"build folder has no holdout-{arm} task environment")
    if docker_rows("ps") is None:
        problems.append("docker ps failed")
    write_json(output / "preflight.json", {"build_dir": str(build), "problems": problems,
                                           "python": sys.executable, "dont_write_bytecode": sys.dont_write_bytecode})
    return problems


def group_kill(pgid, sig):
    if not isinstance(pgid, int) or pgid <= 1:
        return False
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def pid_from(path):
    try:
        text = Path(path).read_text(encoding="utf-8").strip() if path else ""
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def supervised(args, log_path, deadline_sec, env, term_grace_sec, child_pid_file=None):
    assert deadline_sec > 0 and term_grace_sec > 0
    t0 = time.time()
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                start_new_session=True, cwd=str(RUN))
        killed, child = None, None
        try:
            proc.wait(timeout=deadline_sec)
        except subprocess.TimeoutExpired:
            killed = "SIGTERM"
            group_kill(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=term_grace_sec)
            except subprocess.TimeoutExpired:
                killed = "SIGKILL"
                child = pid_from(child_pid_file)
                group_kill(child, signal.SIGKILL)
                group_kill(proc.pid, signal.SIGKILL)
                try:
                    proc.wait(timeout=KILL_GRACE_SEC)
                except subprocess.TimeoutExpired:
                    killed = "SIGKILL sent; process still present"
    return {"rc": proc.returncode, "killed_by_proof": killed, "wall_sec": round(time.time() - t0, 1),
            "deadline_sec": deadline_sec, "log": str(log_path), "child_group_killed": child}


def prebuild_image(output):
    env_dir = build_dir() / "holdout-team" / "task" / "environment"
    code = ("import docker, json, sys, time\n"
            "t = time.time()\n"
            f"c = docker.from_env(timeout={SETUP_DEADLINE_SEC})\n"
            "img, _ = c.images.build(path=sys.argv[1], dockerfile=sys.argv[2], rm=True, forcerm=True)\n"
            "print(json.dumps({'image_id': img.id, 'build_sec': round(time.time() - t, 1)}))\n")
    args = [str(B_PYTHON), "-c", code, str(env_dir), str(env_dir / "Dockerfile")]
    result = supervised(args, output / "setup-image-build.log", SETUP_DEADLINE_SEC, scrubbed_env(),
                        SETUP_TERM_GRACE_SEC)
    lines = (output / "setup-image-build.log").read_text(encoding="utf-8", errors="replace").splitlines()
    parsed = next((json.loads(x) for x in reversed(lines) if x.startswith("{\"image_id\"")), None)
    result.update({"environment": str(env_dir), "image": parsed,
                   "note": "untagged image built the way the harness docker backend builds it, so its layer "
                           "cache makes the in-trial build fast; left in place as shared cache"})
    write_json(output / "setup-image-build.json", result)
    return result


def launcher_args(key, wave, output):
    spec = TRIALS[key]
    args = [str(B_PYTHON), str(LAUNCHER), "run", "--arm", spec["arm"], "--n-agents", str(spec["n_agents"]),
            "--budget-usd", "0", "--backend", "docker", "--timeout-sec", str(spec["timeout_sec"]),
            "--wave-id", wave, "--jobs-dir", str(output / "jobs"), "--ledger", str(output / "spend-ledger.jsonl"),
            "--scripted", str(SEAT_SCRIPT), "--scripted-scenario", spec["scenario"],
            "--build-dir", str(build_dir())]
    memory = os.environ.get("HOLDOUT_PROOF_MEMORY_MB")
    if memory:
        assert memory.isdigit() and 512 <= int(memory) <= 4096, memory
        args += ["--docker-memory-mb", memory]
    return args


def final_json(log_path):
    lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
    for line in reversed(lines[-200:]):
        line = line.strip()
        if line.startswith("{") and "wave_id" in line:
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def find_trial(output, wave, reported):
    if reported and reported.get("trial_dir") and Path(reported["trial_dir"]).is_dir():
        return Path(reported["trial_dir"])
    found = sorted(p.parent for p in (output / "jobs" / wave).glob("*/result.json"))
    if found:
        return found[0]
    dirs = sorted(p.parent for p in (output / "jobs" / wave).glob("*/sandbox-ledger.jsonl"))
    return dirs[0] if dirs else None


def ledger_containers(job_dir):
    created, deleted = {}, set()
    for ledger in sorted(Path(job_dir).glob("*/sandbox-ledger.jsonl")) if Path(job_dir).is_dir() else []:
        for row in read_jsonl(ledger):
            state = row.get("state") if isinstance(row.get("state"), dict) else {}
            cid = state.get("container_id")
            if row.get("role") and isinstance(cid, str) and CONTAINER_ID.fullmatch(cid):
                created.setdefault(cid, {"name": row.get("name"), "role": row.get("role"),
                                         "session_id": row.get("session_id"), "ledger": str(ledger)})
            if isinstance(row.get("deleted"), str):
                deleted.update(CONTAINER_ID.findall(row["deleted"]))
    return created, deleted


def remove_attributed(job_dir, protected, until):
    created, deleted = ledger_containers(job_dir)
    actions = []
    for cid, info in sorted(created.items()):
        row = {"id": cid, **info, "ledger_deleted": cid in deleted}
        left = until - time.time()
        if cid in protected:
            actions.append({**row, "action": "SKIPPED: existed before the proof"})
            continue
        if left < 2:
            actions.append({**row, "action": "SKIPPED: cleanup budget spent"})
            continue
        probe = command(["docker", "inspect", "--format", "{{.State.Status}}", cid], min(DOCKER_CMD_SEC, left))
        if probe["rc"] != 0:
            actions.append({**row, "action": "already gone"})
            continue
        left = until - time.time()
        removed = command(["docker", "rm", "-f", cid], max(1, min(DOCKER_CMD_SEC, left)))
        actions.append({**row, "state": probe["out"].strip(),
                        "action": "removed" if removed["rc"] == 0 else f"REMOVE FAILED rc={removed['rc']}"})
    return actions


def run_trial(output, key, protected):
    assert key in TRIALS
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    wave = f"mmb-proof-{key}-{stamp}-{secrets.token_hex(3)}"
    assert WAVE_ID.fullmatch(wave), wave
    spec = TRIALS[key]
    assert spec["timeout_sec"] + 120 <= TRIAL_RUN_SEC, spec
    for _ in range(12):
        if os.getloadavg()[0] <= 20:
            break
        time.sleep(10)
    else:
        summary = {"key": key, "spec": spec, "trial_dir": None, "cleanup": [],
                   "obstacle": "The 1-minute load average stayed above 20 for 120 s."}
        write_json(output / "trials" / f"{key}.json", summary)
        return summary
    args = launcher_args(key, wave, output)
    (output / "logs").mkdir(parents=True, exist_ok=True)
    started = time.time()
    proc = supervised(args, output / "logs" / f"{wave}.launcher.log", TRIAL_RUN_SEC, scrubbed_env(),
                      TERM_GRACE_SEC, child_pid_file=output / "jobs" / f"{wave}.harbor.pid")
    reported = final_json(proc["log"])
    trial = find_trial(output, wave, reported)
    cleanup = remove_attributed(output / "jobs" / wave, protected, started + TRIAL_TOTAL_SEC)
    summary = {"key": key, "wave_id": wave, "spec": spec, "argv": args, "started_ts": started, **proc,
               "total_wall_sec": round(time.time() - started, 1), "total_cap_sec": TRIAL_TOTAL_SEC,
               "launcher_final": reported, "trial_dir": str(trial) if trial else None, "cleanup": cleanup}
    write_json(output / "trials" / f"{key}.json", summary)
    return summary


def seat_dirs(trial):
    agent = Path(trial) / "agent"
    return sorted(p for p in agent.glob("agent-*") if p.is_dir()) if agent.is_dir() else []


def board_rows(trial):
    for rel in ("agent/board-events.jsonl", "board-events.jsonl"):
        rows = read_jsonl(Path(trial) / rel)
        if rows:
            return rows, str(Path(trial) / rel)
    return [], None


def seat_log(seat_dir):
    return read_jsonl(Path(seat_dir) / "scripted.jsonl")


def steps(rows, name):
    return [r for r in rows if r.get("step") == name]


def metadata(trial):
    result = read_json(Path(trial) / "result.json") or {}
    meta = ((result.get("agent_result") or {}).get("metadata")) or {}
    return result, meta


def cheb(a, b):
    return max(abs(float(a[0]) - float(b[0])), abs(float(a[1]) - float(b[1])))


def frames_for(trial, seat, after_wall):
    out = []
    for row in read_jsonl(Path(trial) / "verifier" / "replay.jsonl"):
        if row.get("type") != "frame" or not isinstance(row.get("wall"), (int, float)) or row["wall"] <= after_wall:
            continue
        for s in row.get("survivors") or []:
            if s.get("seat") == seat:
                out.append({"wall": row["wall"], "x": s.get("x"), "y": s.get("y"), "act": s.get("act")})
    return out


def o4_seat(trial, seat_dir, calls):
    rows = seat_log(seat_dir)
    seat = seat_dir.name
    start = next(iter(steps(rows, "start_position")), {})
    queued = next(iter(steps(rows, "queued_orders")), {})
    exit_state = next(iter(steps(rows, "exit_state")), {})
    done = next(iter(steps(rows, "done")), {})
    ev = {"seat": seat, "start": [start.get("x"), start.get("y")], "target": queued.get("target"),
          "queued_status": queued.get("status"), "exit_wall": exit_state.get("wall"), "done_ts": done.get("ts")}
    if not (isinstance(ev["exit_wall"], (int, float)) and ev["target"] and None not in ev["start"]
            and ev["queued_status"] == 200 and done):
        return "not settled", ev
    frames = frames_for(trial, seat, ev["exit_wall"])
    later_calls = [c for c in calls if c.get("agent") == seat and float(c.get("ts", 0)) > done["ts"] + 1]
    moved = max((cheb((f["x"], f["y"]), ev["start"]) for f in frames), default=0)
    reached = min((cheb((f["x"], f["y"]), ev["target"]) for f in frames), default=None)
    ev.update({"frames_after_exit": len(frames), "max_tiles_from_start": moved, "min_tiles_to_target": reached,
               "walk_frames_after_exit": sum(f["act"] == "walk" for f in frames),
               "game_calls_after_seat_done": len(later_calls)})
    ok = (frames and moved >= MOVED_TILES and reached is not None and reached <= REACH_TILES
          and ev["walk_frames_after_exit"] > 0 and not later_calls)
    return ("pass" if ok else "fail"), ev


def o4_trial(trial, n_agents, timeout_sec):
    _, meta = metadata(trial)
    keep = meta.get("keepalive") or {}
    timeline = keep.get("timeline") or []
    freezes = [e for e in timeline if e.get("op") == "freeze"]
    ends = [e for e in timeline if e.get("op") == "end"]
    agents = meta.get("agents") or {}
    calls = [r for r in board_rows(trial)[0] if r.get("event") == "game_call"]
    ev = {"end_reason": meta.get("end_reason"), "freezes": freezes, "ends": ends, "wakes": keep.get("wakes"),
          "agent_exits": {k: v.get("exit") for k, v in agents.items()},
          "agent_sessions": {k: v.get("sessions") for k, v in agents.items()}}
    if not timeline or not agents:
        return "not settled", ev, "no keepalive timeline or agent record in result.json"
    seats = [o4_seat(trial, d, calls) for d in seat_dirs(trial)]
    ev["seats"] = [s[1] for s in seats]
    last_end = max((float(e.get("t", 0)) for e in ends), default=None)
    freeze = freezes[0] if len(freezes) == 1 else {}
    checks = {
        "one_freeze_time_up": freeze.get("why") == "time_up",
        "freeze_near_horizon": float(freeze.get("t", 0)) >= timeout_sec - FREEZE_LEFT_SEC - 15,
        "all_seats_ended_first": len(ends) == n_agents and last_end is not None
        and last_end + MIN_IDLE_WORLD_SEC <= float(freeze.get("t", 0)),
        "clean_exits": len(agents) == n_agents and all(v.get("exit") == 0 for v in agents.values()),
        "no_wakes": keep.get("wakes") == 0 and all(v.get("sessions") == 1 for v in agents.values()),
        "end_reason_all_finished": meta.get("end_reason") == "all_finished",
        "queued_orders_ran_after_exit": len(seats) == n_agents and all(s[0] == "pass" for s in seats),
    }
    ev["conditions"] = checks
    if any(s[0] == "not settled" for s in seats):
        return "not settled", ev, "a seat log lacks its start, queue or exit records"
    bad = sorted(k for k, v in checks.items() if not v)
    return ("fail", ev, "failed: " + ", ".join(bad)) if bad else ("pass", ev, "all conditions hold")


def bundle_heads(trial):
    bundle = next((Path(trial) / rel for rel in ("agent/team.bundle", "team.bundle")
                   if (Path(trial) / rel).is_file()), None)
    if bundle is None:
        return None
    SCRATCH.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bundle-", dir=SCRATCH) as tmp:
        r = command(["git", "bundle", "list-heads", str(bundle)], DOCKER_CMD_SEC, cwd=tmp)
    if r["rc"] != 0:
        return None
    return sorted(line.split()[-1] for line in r["out"].splitlines() if line.strip())


def o5_seat(seat_dir):
    probes = {r.get("probe"): {k: r.get(k) for k in ("rc", "reason", "excerpt")}
              for r in steps(seat_log(seat_dir), "git_probe")}
    ev = {"seat": seat_dir.name, "probes": probes}
    missing = [p for p in TEAM_PROBES + CONTROL_PROBES if p not in probes]
    if missing:
        return "not settled", ev, f"{seat_dir.name}: probes missing: {', '.join(missing)}"
    leaks = [p for p in TEAM_PROBES if probes[p]["rc"] == 0]
    if leaks:
        return "fail", ev, f"{seat_dir.name}: team remote allowed {', '.join(leaks)}"
    hung = [p for p in TEAM_PROBES if probes[p]["rc"] is None]
    if hung:
        return "not settled", ev, f"{seat_dir.name}: team remote probe timed out: {', '.join(hung)}"
    broken = [p for p in CONTROL_PROBES if probes[p]["rc"] != 0]
    if broken:
        return "not settled", ev, f"{seat_dir.name}: own disposable remote failed: {', '.join(broken)}"
    reasons = sorted({probes[p]["reason"] for p in TEAM_PROBES})
    return "pass", ev, f"{seat_dir.name}: 2 reads and 2 writes refused ({', '.join(reasons)}); own remote works"


def o5_trial(trial, n_agents):
    seats = [o5_seat(d) for d in seat_dirs(trial)]
    heads = bundle_heads(trial)
    revoked = sum(r.get("event") == "matched_git_revoked"
                  for r in read_jsonl(Path(trial) / "sandbox-ledger.jsonl"))
    ev = {"seats": [s[1] for s in seats], "bundle_heads": heads, "matched_git_revoked_rows": revoked,
          "original_b_behaviour": B_SEAL_NOTE}
    probe_refs = [h for h in heads or [] if h.endswith("-probe")]
    notes = [s[2] for s in seats]
    if probe_refs:
        return "fail", ev, f"exported team bundle holds seat refs {probe_refs}"
    if len(seats) != n_agents:
        return "not settled", ev, f"{len(seats)} seat logs for {n_agents} seats"
    for verdict in ("fail", "not settled"):
        if any(s[0] == verdict for s in seats):
            return verdict, ev, "; ".join(notes)
    return "pass", ev, "; ".join(notes) + f"; bundle heads {heads}; revoke rows {revoked}"


def seat_journal(rows):
    out = []
    for r in steps(rows, "game_call"):
        ops = [str(o) for o in r.get("ops") or []]
        acc = [bool(a) for a in (r.get("accepted") or [])][:len(ops)]
        out.append({"transport": r.get("transport"), "endpoint": r.get("endpoint"), "ops": ops,
                    "accepted": acc + [False] * (len(ops) - len(acc)), "status": r.get("status")})
    return out


def hub_journal(rows, seat):
    out = []
    for r in rows:
        if r.get("event") != "game_call" or r.get("agent") != seat:
            continue
        ops = r.get("operations") or []
        out.append({"transport": r.get("transport"), "endpoint": r.get("endpoint"),
                    "ops": [str(o.get("op")) for o in ops], "accepted": [o.get("accepted") is True for o in ops],
                    "mutates": [o.get("mutates_world") is True for o in ops], "status": r.get("status")})
    return out


def compare_seat(seat, mine, hub):
    key = ("transport", "endpoint", "ops", "accepted")
    mismatches = [{"i": i, "seat": {k: a[k] for k in key}, "log": {k: b[k] for k in key}}
                  for i, (a, b) in enumerate(zip(mine, hub, strict=False)) if any(a[k] != b[k] for k in key)]
    marks = []
    for i, row in enumerate(hub):
        if row["endpoint"] != "act" and row["ops"]:
            marks.append({"i": i, "problem": "operations on a non-act call"})
        for op, acc, mut in zip(row["ops"], row["accepted"], row["mutates"], strict=True):
            want = acc and (op == "radio" or op not in NO_WORLD_OPS)
            if op not in SCRIPT_OPS or mut != want:
                marks.append({"i": i, "op": op, "accepted": acc, "mutates_world": mut, "expected": want})
    counts = {}
    for row in hub:
        name = f"{row['transport']}:{row['endpoint']}"
        counts[name] = counts.get(name, 0) + 1
    return {"seat": seat, "seat_calls": len(mine), "log_rows": len(hub), "by_transport_endpoint": counts,
            "mismatches": mismatches[:20], "mark_errors": marks[:20],
            "mutating_rows": sum(any(r["mutates"]) for r in hub),
            "seat_calls_without_status": sum(r["status"] is None for r in mine)}


def comms_policy(trial, arm, logs, hub_rows):
    comms = [o for r in hub_rows if r.get("event") == "game_call"
             for o in (r.get("operations") or []) if o.get("op") in COMMS_OPS]
    heard = {name: (steps(rows, "peer_heard") or [{}])[0].get("n") for name, rows in logs.items()}
    sends = {name: (steps(rows, "board_send") or [{}])[0].get("refused") for name, rows in logs.items()}
    messages = Path(trial) / "verifier" / "messages.jsonl"
    lines = len(read_jsonl(messages)) if messages.is_file() else None
    ev = {"comms_ops_logged": len(comms), "comms_ops_accepted": sum(o.get("accepted") is True for o in comms),
          "peer_heard": heard, "board_send_refused": sends, "game_message_records": lines}
    if arm == "silent":
        ok = comms and not ev["comms_ops_accepted"] and all(v == 0 for v in heard.values()) and lines == 0
    else:
        ok = comms and ev["comms_ops_accepted"] == len(comms) and all(isinstance(v, int) and v >= 1
                                                                     for v in heard.values()) and bool(lines)
    ok = bool(ok) and bool(sends) and all(v is True for v in sends.values())
    return ok, ev


def call_log_trial(trial, arm):
    rows, path = board_rows(trial)
    call_rows = [r for r in rows if r.get("event") == "game_call"]
    logs = {d.name: seat_log(d) for d in seat_dirs(trial)}
    seats = [compare_seat(name, seat_journal(rows_), hub_journal(rows, name)) for name, rows_ in logs.items()]
    policy_ok, policy = comms_policy(trial, arm, logs, rows)
    leaked_keys = sorted({k for r in call_rows for k in r
                          if k in ("text", "params", "arguments", "body", "message", "error_text")})
    ev = {"board_events": path, "active_event": any(r.get("event") == "game_call_log_active" for r in rows),
          "seats": seats, "comms_policy": policy, "message_text_in_log": any(COMMS_TEXT in json.dumps(r)
                                                                            for r in call_rows),
          "raw_keys_in_rows": leaked_keys}
    if not path or not logs or not all(steps(r, "game_call") for r in logs.values()):
        return "not settled", ev, "no board events, or a seat logged no scripted game calls"
    if any(s["seat_calls_without_status"] for s in seats):
        return "not settled", ev, "a scripted call got no HTTP answer, so the 1-row-per-call count is open"
    bad = [s["seat"] for s in seats if s["seat_calls"] != s["log_rows"] or s["mismatches"] or s["mark_errors"]]
    problems = ([f"rows differ from the seat's own calls for {bad}"] if bad else []) + \
        (["plugin start event missing"] if not ev["active_event"] else []) + \
        (["message text or raw parameters in the log"] if ev["message_text_in_log"] or leaked_keys else []) + \
        ([f"{arm} message policy not as designed"] if not policy_ok else [])
    if problems:
        return "fail", ev, "; ".join(problems)
    calls = sum(s["log_rows"] for s in seats)
    muts = sum(s["mutating_rows"] for s in seats)
    return "pass", ev, (f"{calls} rows for {calls} scripted calls, {muts} marked mutating; {arm}: "
                        f"{policy['comms_ops_accepted']}/{policy['comms_ops_logged']} message ops accepted")


def worker_view(seat_dir, role, ref_ts):
    rows = read_jsonl(Path(seat_dir) / f"o3-worker-{role}.jsonl")
    start = next(iter(steps(rows, "worker_start")), {})
    calls = steps(rows, "worker_call")
    after = [c for c in calls if ref_ts is not None and float(c.get("ts", 0)) > ref_ts + AFTER_STOP_SLACK_SEC]
    reached = [c for c in after if c.get("status") in (200, 409, 429)]
    acts = [c for c in after if c.get("endpoint") == "act" and c.get("accepted") is True]
    return {"role": role, "logged": bool(rows), "identity": {k: start.get(k) for k in ("pid", "ppid", "pgid", "sid")},
            "got_sigterm": bool(steps(rows, "sigterm")), "calls": len(calls), "calls_after": len(after),
            "api_answers_after": len(reached), "accepted_acts_after": len(acts),
            "statuses_after": sorted({str(c.get("status")) for c in after}),
            "frozen_seen_after": any(c.get("frozen") is True for c in after),
            "last_ts": calls[-1].get("ts") if calls else None}


def o3_trial(trial):
    _, meta = metadata(trial)
    dirs = {d.name: d for d in seat_dirs(trial)}
    holder, ender = dirs.get("agent-1"), dirs.get("agent-2")
    hold_rows = seat_log(holder) if holder else []
    end_rows = seat_log(ender) if ender else []
    t_signal = next((r.get("ts") for r in steps(hold_rows, "sigterm")), None)
    ledger = read_jsonl(Path(trial) / "sandbox-ledger.jsonl")
    t_stop = next((r.get("proof_ts") for r in ledger if r.get("event") == "matched_proof_stop_done"
                   and r.get("seat") == "agent-1"), None)
    t_end = next((r.get("ts") for r in steps(end_rows, "done")), None)
    roles = ("attached", "orphan", "detached")
    ev = {"stopped_flags": {k: v.get("stopped") for k, v in (meta.get("agents") or {}).items()},
          "end_reason": meta.get("end_reason"), "holder_sigterm_ts": t_signal, "stop_done_ts": t_stop,
          "ender_done_ts": t_end,
          "holder_identity": next(iter(steps(hold_rows, "seat_identity")), None),
          "holder_workers": [worker_view(holder, r, t_stop) for r in roles] if holder else [],
          "ender_workers": [worker_view(ender, r, t_end) for r in roles] if ender else []}
    calls = [r for r in board_rows(trial)[0] if r.get("event") == "game_call"]
    ev["log_rows_after_stop"] = sum(r.get("agent") == "agent-1" and t_stop is not None
                                    and float(r.get("ts", 0)) > t_stop + AFTER_STOP_SLACK_SEC for r in calls)
    ev["log_mutating_rows_after_end"] = sum(
        r.get("agent") == "agent-2" and t_end is not None and float(r.get("ts", 0)) > t_end + AFTER_STOP_SLACK_SEC
        and any(o.get("mutates_world") is True for o in r.get("operations") or []) for r in calls)
    survived = [w["role"] for w in ev["holder_workers"] if w["api_answers_after"]]
    acted = [w["role"] for w in ev["ender_workers"] if w["accepted_acts_after"]]
    if survived:
        return "fail", ev, (f"processes {survived} still reached the game API after stop() returned; "
                            f"processes {acted or 'none'} gave accepted orders after their seat ended. " + FIX_TEXT)
    if acted:
        return "fail", ev, f"processes {acted} gave accepted orders after their seat ended. " + FIX_TEXT
    if t_stop is None or t_signal is None or ev["stopped_flags"].get("agent-1") is not True:
        return "not settled", ev, "no completed harness stop() with a holding seat SIGTERM record"
    if not all(w["logged"] for w in ev["holder_workers"] + ev["ender_workers"]):
        return "not settled", ev, "a worker log is missing"
    return "pass", ev, "no seat process reached the game API after stop() or gave orders after its seat ended"


def evaluate(check, trials):
    keys = NEEDS[check]
    outcomes = []
    for key in keys:
        summary = trials[key]
        trial = summary.get("trial_dir")
        head = f"{key} (launcher rc {summary.get('rc')}, killed {summary.get('killed_by_proof')})"
        if summary.get("obstacle"):
            outcomes.append((key, "not settled", {}, f"{key}: {summary['obstacle']}"))
            continue
        if not trial or not (Path(trial) / "result.json").is_file():
            outcomes.append((key, "not settled", {}, f"{head}: no finished trial folder"))
            continue
        spec = TRIALS[key]
        if check == "O4":
            verdict, ev, reason = o4_trial(trial, spec["n_agents"], spec["timeout_sec"])
        elif check == "O5":
            verdict, ev, reason = o5_trial(trial, spec["n_agents"])
        elif check == "game-call-log":
            verdict, ev, reason = call_log_trial(trial, spec["arm"])
        else:
            verdict, ev, reason = o3_trial(trial)
        outcomes.append((key, verdict, {"trial_dir": trial, **ev}, f"{key}: {reason}"))
    verdicts = [o[1] for o in outcomes]
    final = "fail" if "fail" in verdicts else "not settled" if "not settled" in verdicts else "pass"
    return final, " | ".join(o[3] for o in outcomes), {o[0]: {"verdict": o[1], **o[2]} for o in outcomes}


def resource_report(before, after, trials):
    ours = {a["id"] for t in trials.values() for a in t["cleanup"]}
    old = before.get("containers") or {}
    new = after.get("containers") or {}
    return {"before": before, "after": after, "attributed_container_ids": sorted(ours),
            "removed": sum(a["action"] == "removed" for t in trials.values() for a in t["cleanup"]),
            "remove_failed": [a for t in trials.values() for a in t["cleanup"] if a["action"].startswith(("REMOVE",
                                                                                                         "SKIP"))],
            "unattributed_new_containers": sorted(set(new) - set(old) - ours),
            "attributed_still_present": sorted(set(new) & ours),
            "pre_existing_changed": sorted(cid for cid, row in old.items()
                                           if cid not in new or new[cid]["state"] != row["state"]),
            "new_networks": sorted(set(after.get("networks") or []) - set(before.get("networks") or [])),
            "new_volumes": sorted(set(after.get("volumes") or []) - set(before.get("volumes") or []))}


def finish(output, results, evidence):
    out = {}
    for check, (verdict, reason, ev) in sorted(results.items()):
        assert verdict in ("pass", "fail", "not settled") and reason
        write_json(output / f"{check}.evidence.json", {"check": check, "verdict": verdict, "reason": reason,
                                                       "evidence": ev})
        out[check] = (verdict, reason)
    write_json(output / "holdout-evidence.json", {"verdicts": {c: {"verdict": v, "reason": r}
                                                               for c, (v, r) in out.items()}, **evidence})
    return out


def run(output: Path, checks: set[str]) -> dict[str, tuple[str, str]]:
    output = Path(output).resolve()
    checks = set(checks)
    assert output.is_dir(), output
    assert checks and checks <= set(CHECKS), checks
    problems = preflight(output)
    if problems:
        return finish(output, {c: ("not settled", "preflight: " + "; ".join(problems), {}) for c in checks}, {})
    before = docker_inventory()
    write_json(output / "docker-before.json", before)
    setup = prebuild_image(output)
    if setup["rc"] != 0 or not setup["image"]:
        reason = f"task image pre-build failed or passed its {SETUP_DEADLINE_SEC} s cap (setup-image-build.log)"
        return finish(output, {c: ("not settled", reason, {}) for c in checks}, {"setup": setup})
    protected = set(before.get("containers") or {})
    keys = [k for k in TRIALS if any(k in NEEDS[c] for c in checks)]
    trials = {k: run_trial(output, k, protected) for k in keys}
    after = docker_inventory()
    resources = resource_report(before, after, trials)
    write_json(output / "docker-resources.json", resources)
    results = {c: evaluate(c, trials) for c in checks}
    if resources["remove_failed"] or resources["attributed_still_present"] or resources["pre_existing_changed"]:
        note = " [docker: see docker-resources.json: cleanup or pre-existing container state needs a look]"
        results = {c: (v, r + note, e) for c, (v, r, e) in results.items()}
    return finish(output, results, {"setup": setup, "trials": trials, "docker": resources})
