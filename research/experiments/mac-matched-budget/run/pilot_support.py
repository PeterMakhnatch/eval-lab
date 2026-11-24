from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ARM_NAME = re.compile(r"w(\d{2})-(team|silent)-(\d+)")
SCHEMA = "campaign-plan/v1"
APPROVED = "approved"
POLL_SECONDS = 1.0
LEAD_SECONDS = 3.0
STAGGER_SECONDS = 2.0
PREFLIGHT_SECONDS = 180
MAX_ARMS = 256
MAX_WAVES = 99
MAX_LEDGER_ROWS = 100_000
MAX_BARRIER_SECONDS = 7200
EXIT_REFUSED = 2
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
NO_NETWORK = "(version 1)(allow default)(deny network*)"
PLAN_REL = Path("campaign/plans/mac-matched-pilot.toml")
KEYS_STUB = """#!/bin/sh
case "$*" in
  *api/v1/credits*) printf '%s\\n' '{"data":{"total_credits":50.0,"total_usage":10.0}}' ;;
  *api/v1/key*) printf '%s\\n' '{"data":{"limit":null,"limit_remaining":null}}' ;;
  *) exit 9 ;;
esac
"""
LAUNCHER_STUB = """import json, os, sys, time
args = sys.argv[1:]
if "--dry-run" in args:
    sys.exit(0)
value = lambda flag: args[args.index(flag) + 1]
wave_id, ledger, jobs = value("--wave-id"), value("--ledger"), value("--jobs-dir")
job = os.path.join(jobs, wave_id)
os.makedirs(job)
with open(os.path.join(job, "started.json"), "w") as handle:
    json.dump({"t": time.time()}, handle)
def row(record):
    with open(ledger, "a") as handle:
        handle.write(json.dumps(record) + "\\n")
row({"event": "reserve", "wave_id": wave_id, "reserved_usd": 0.475})
time.sleep(1)
if os.environ.get("STUB_NO_SETTLE") != wave_id:
    row({"event": "settle", "wave_id": wave_id, "actual_usd": 0.40, "trials": [{"undeleted_sandboxes": []}]})
"""


class Refused(Exception):
    pass


def expand(text: str) -> Path:
    assert isinstance(text, str) and text
    return Path(os.path.normpath(os.path.expanduser(text)))


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def load_plan(path: Path) -> dict[str, Any]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data.get("schema") == SCHEMA, path
    assert 1 <= len(data["arms"]) <= MAX_ARMS
    return data


def plan_reserves(plan: dict[str, Any]) -> dict[str, float]:
    reserves: dict[str, float] = {}
    for item in plan["arms"]:
        name = item["name"]
        assert ARM_NAME.fullmatch(name) and name not in reserves, name
        reserve = finite(item["reserve_usd"])
        assert reserve is not None and reserve > 0, name
        reserves[name] = reserve
    assert len(reserves) == len(plan["arms"])
    return reserves


def wave_key(name: str) -> tuple[str, str, int]:
    match = ARM_NAME.fullmatch(name)
    assert match, name
    return f"w{match[1]}", match[2], int(match[3])


def wave_number(wave: str) -> int:
    assert re.fullmatch(r"w\d{2}", wave), wave
    return int(wave[1:])


def members(reserves: dict[str, float], wave: str) -> list[str]:
    found = sorted(name for name in reserves if wave_key(name)[0] == wave)
    assert len(found) == 2 and {wave_key(name)[1] for name in found} == {"team", "silent"}, found
    return found


def python_entry(plan: dict[str, Any]) -> Path:
    found = [arg for arg in plan["command"]["argv"] if Path(arg).name in ("python", "python3")]
    assert len(found) == 1, found
    return expand(found[0])


def env_gate() -> None:
    if os.environ.get("MAC_MATCHED_APPROVAL") != APPROVED:
        raise Refused("the plan field MAC_MATCHED_APPROVAL does not say approved")
    if os.environ.get("MAC_MATCHED_PAID") != "1":
        raise Refused("the environment has no MAC_MATCHED_PAID=1")


def bind(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, float], str]:
    plan = load_plan(Path(os.path.abspath(args.plan)))
    reserves = plan_reserves(plan)
    out = Path(os.path.abspath(args.out))
    if out.name not in reserves:
        raise Refused(f"{out.name} is not an arm of {args.plan}")
    _, variant, n_agents = wave_key(out.name)
    harbor = Path(os.path.abspath(args.harbor))
    checks = {
        "output root": out.parent == expand(plan["output_root"]),
        "agents": n_agents == args.n_agents,
        "tasks": Path(os.path.abspath(args.tasks)).parts[-2:] == (f"holdout-{variant}", "task"),
        "one trial": args.attempts == 1 and args.concurrency == 1,
        "harbor wrapper": harbor.parent == expand(plan["state_root"]) and harbor.name.startswith("harbor-"),
        "harbor program": plan["harbor"]["run_args"] == []
        and expand(plan["harbor"]["program"]).parent == python_entry(plan).parent,
        "backend": args.backend == "daytona",
        "barrier": 1 <= args.barrier_seconds <= MAX_BARRIER_SECONDS,
    }
    failed = sorted(key for key, ok in checks.items() if not ok)
    if failed:
        raise Refused(f"{out.name}: the arguments do not match the plan: {', '.join(failed)}")
    return plan, reserves, out.name


def launcher_argv(args: argparse.Namespace, name: str) -> list[str]:
    _, variant, n_agents = wave_key(name)
    out = Path(os.path.abspath(args.out))
    build = Path(os.path.abspath(args.tasks)).parent.parent
    argv = [sys.executable, os.path.abspath(args.launcher), "run", "--arm", variant, "--n-agents", str(n_agents),
            "--budget-usd", args.budget_usd, "--wave-id", name, "--paid-wave", name,
            "--plan", os.path.abspath(args.plan), "--jobs-dir", str(out.parent), "--ledger", os.path.abspath(args.ledger),
            "--backend", args.backend, "--timeout-sec", args.timeout_sec, "--grade-at", args.grade_at,
            "--model", args.model, "--harness", args.harness, "--effort", args.effort, "--build-dir", str(build),
            "--out", str(out)]
    assert all(isinstance(arg, str) and arg for arg in argv)
    return argv


def preflight(argv: list[str]) -> None:
    try:
        done = subprocess.run([*argv, "--dry-run"], stdin=subprocess.DEVNULL, timeout=PREFLIGHT_SECONDS, check=False)
    except subprocess.TimeoutExpired as error:
        raise Refused(f"the launcher dry run took more than {PREFLIGHT_SECONDS} s") from error
    if done.returncode != 0:
        raise Refused(f"the launcher dry run exited {done.returncode}")


@contextlib.contextmanager
def locked(waves: Path, wave: str) -> Iterator[None]:
    waves.mkdir(parents=True, exist_ok=True)
    with open(waves / f"{wave}.lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_state(waves: Path, wave: str) -> dict[str, Any]:
    path = waves / f"{wave}.json"
    if not path.exists():
        return {"wave": wave, "status": "open", "arrived": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(state, dict) and state.get("wave") == wave, path
    assert state.get("status") in ("open", "go", "void"), path
    return state


def write_state(waves: Path, state: dict[str, Any]) -> None:
    path = waves / f"{state['wave']}.json"
    staged = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    staged.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(staged, path)


def alive(entry: Any) -> bool:
    if not isinstance(entry, dict) or entry.get("host") != socket.gethostname():
        return False
    pid = entry.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def ledger_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if lines and not text.endswith("\n"):
        lines.pop()
    if len(lines) > MAX_LEDGER_ROWS:
        raise Refused(f"{path} has more than {MAX_LEDGER_ROWS} rows")
    rows = []
    for number, line in enumerate(lines, 1):
        try:
            row = json.loads(line)
        except ValueError as error:
            raise Refused(f"{path}:{number} is not JSON") from error
        if not isinstance(row, dict):
            raise Refused(f"{path}:{number} is not an object")
        rows.append(row)
    return rows


def ledger_view(
    rows: list[dict[str, Any]], reserves: dict[str, float]
) -> tuple[dict[str, float], dict[str, float], list[str]]:
    reserved: dict[str, float] = {}
    settled: dict[str, float] = {}
    problems: list[str] = []
    for row in rows:
        name, event = row.get("wave_id"), row.get("event")
        if name not in reserves or event not in ("reserve", "settle"):
            continue
        key = "reserved_usd" if event == "reserve" else "actual_usd"
        value = finite(row.get(key))
        if value is None or value < 0:
            problems.append(f"the {event} row of {name} has no usable {key}")
            continue
        target = reserved if event == "reserve" else settled
        target[name] = max(target.get(name, 0.0), value)
        trials = row.get("trials", []) if event == "settle" else []
        if not isinstance(trials, list) or any(not isinstance(t, dict) or t.get("undeleted_sandboxes") for t in trials):
            problems.append(f"the settle row of {name} shows undeleted sandboxes or unreadable trials")
    return reserved, settled, problems


def went_arms(waves: Path) -> set[str]:
    found: set[str] = set()
    paths = sorted(waves.glob("w[0-9][0-9].json"))
    assert len(paths) <= MAX_WAVES
    for path in paths:
        state = read_state(waves, path.stem)
        if state["status"] == "go":
            found.update(state["arrived"])
    return found


def go_problem(args: argparse.Namespace, reserves: dict[str, float], state: dict[str, Any], name: str) -> tuple[str, str]:
    wave = state["wave"]
    reserved, settled, problems = ledger_view(ledger_rows(expand(args.ledger)), reserves)
    if problems:
        return "refuse", problems[0]
    started = set(reserved) | went_arms(expand(args.waves))
    pair = members(reserves, wave)
    if started & set(pair):
        return "refuse", f"wave {wave} already has a trial in the trial ledger"
    charged = sum(max(reserves[arm], reserved.get(arm, 0.0), settled.get(arm, 0.0)) for arm in started)
    envelope = sum(reserves.values())
    need = sum(reserves[arm] for arm in pair)
    if round(charged + need, 6) > round(envelope, 6):
        return "refuse", (f"charged ${charged:.4f} + wave reserve ${need:.4f} is above the envelope "
                          f"${envelope:.4f} (sum of plan reserves)")
    earlier = [arm for arm in started if wave_number(wave_key(arm)[0]) < wave_number(wave)]
    unsettled = sorted(arm for arm in earlier if arm not in settled)
    if unsettled:
        return "wait", f"earlier trials have no settle row: {', '.join(unsettled[:4])}"
    partner = [arm for arm in pair if arm != name][0]
    if partner in state["arrived"] and not alive(state["arrived"][partner]):
        return "refuse", f"{partner} arrived and then ended without a start"
    if partner not in state["arrived"]:
        return "wait", f"{partner} has not arrived"
    return "", ""


def first_arm(plan: dict[str, Any], pair: list[str]) -> str:
    digest = hashlib.sha256(f"{plan['id']}/{wave_key(pair[0])[0]}".encode()).digest()
    return pair[digest[0] & 1]


def start_at(state: dict[str, Any], name: str) -> float:
    go_at = finite(state.get("go_at"))
    assert go_at is not None and name in state["arrived"]
    return go_at if state["first"] == name else go_at + STAGGER_SECONDS


def wait_go(args: argparse.Namespace, plan: dict[str, Any], reserves: dict[str, float], name: str) -> float:
    waves, wave = expand(args.waves), wave_key(name)[0]
    reason = "no poll ran"
    for _ in range(math.ceil(args.barrier_seconds / POLL_SECONDS) + 1):
        with locked(waves, wave):
            state = read_state(waves, wave)
            if state["status"] == "go":
                return start_at(state, name)
            if state["status"] == "void":
                raise Refused(f"wave {wave} became void while {name} waited: {state.get('reason')}")
            kind, reason = go_problem(args, reserves, state, name)
            if kind == "refuse":
                raise Refused(reason)
            if kind == "":
                pair = members(reserves, wave)
                state.update(status="go", go_at=time.time() + LEAD_SECONDS, first=first_arm(plan, pair), go_utc=utc())
                write_state(waves, state)
                return start_at(state, name)
        time.sleep(POLL_SECONDS)
    raise Refused(f"wave {wave}: the barrier timed out after {args.barrier_seconds} s: {reason}")


def void_wave(waves: Path, wave: str, name: str, reason: str) -> None:
    with locked(waves, wave):
        state = read_state(waves, wave)
        if state["status"] == "open":
            state.update(status="void", reason=reason[:300], void_by=name, void_utc=utc())
            write_state(waves, state)


def barrier(args: argparse.Namespace, plan: dict[str, Any], reserves: dict[str, float], name: str) -> float | None:
    waves, wave = expand(args.waves), wave_key(name)[0]
    with locked(waves, wave):
        state = read_state(waves, wave)
        if state["status"] == "void":
            return None
        if state["status"] != "open" or name in state["arrived"]:
            raise Refused(f"wave {wave} is {state['status']} and already holds {name}")
        if any(not alive(entry) for entry in state["arrived"].values()):
            state.update(status="void", reason="the partner arm ended without a start", void_by=name,
                         void_utc=utc())
            write_state(waves, state)
            return None
        state["arrived"][name] = {"pid": os.getpid(), "host": socket.gethostname(), "utc": utc()}
        write_state(waves, state)
    try:
        return wait_go(args, plan, reserves, name)
    except BaseException as error:
        void_wave(waves, wave, name, str(error))
        raise


def stop_on_term(signum: int, frame: Any) -> None:
    raise SystemExit(f"signal {signum} while the arm waited")


def cmd_wave(args: argparse.Namespace) -> int:
    signal.signal(signal.SIGTERM, stop_on_term)
    signal.signal(signal.SIGHUP, stop_on_term)
    try:
        env_gate()
        plan, reserves, name = bind(args)
        argv = launcher_argv(args, name)
        preflight(argv)
        start = barrier(args, plan, reserves, name)
    except Refused as error:
        print(f"pilot_support: refused: {error}", file=sys.stderr, flush=True)
        return EXIT_REFUSED
    if start is None:
        print(f"pilot_support: wave of {name} is void; this arm starts no trial", flush=True)
        return 0
    delay = start - time.time()
    assert delay <= LEAD_SECONDS + STAGGER_SECONDS + POLL_SECONDS, delay
    time.sleep(max(0.0, delay))
    print(f"pilot_support: {name} starts at {utc()}", flush=True)
    os.execv(argv[0], argv)
    raise AssertionError("execv returned")


def write_exec(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def fixture(home: Path, repo: Path, plan: dict[str, Any]) -> Path:
    bin_dir = home / "fakebin"
    forward = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n'
    write_exec(bin_dir / "keys", KEYS_STUB)
    write_exec(bin_dir / "curl", "#!/bin/sh\nexit 7\n")
    write_exec(bin_dir / "python3", forward)
    write_exec(python_entry(plan), forward)
    write_exec(expand(plan["harbor"]["program"]), "#!/bin/sh\nexit 97\n")
    write_exec(home / "stub_launcher.py", LAUNCHER_STUB)
    cwd = expand(plan["command"]["cwd"])
    cwd.parent.mkdir(parents=True, exist_ok=True)
    cwd.symlink_to(repo)
    for item in plan["arms"]:
        tasks = expand(item["tasks"])
        tasks.mkdir(parents=True, exist_ok=True)
        (tasks / "task.toml").write_text('[task]\nname = "fake"\n', encoding="utf-8")
    expand(plan["money"]["ledger"]).parent.mkdir(parents=True, exist_ok=True)
    return bin_dir


def fake_env(home: Path, bin_dir: Path, extra: dict[str, str]) -> dict[str, str]:
    return {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8", **extra}


def sandboxed(argv: list[str]) -> list[str]:
    assert SANDBOX_EXEC.is_file(), SANDBOX_EXEC
    return [str(SANDBOX_EXEC), "-p", NO_NETWORK, *argv]


def run_check(repo: Path, home: Path, bin_dir: Path, paid: bool) -> dict[str, Any]:
    extra = {"MAC_MATCHED_PAID": "1"} if paid else {}
    argv = sandboxed([sys.executable, "-m", "campaign", "check", str(repo / PLAN_REL)])
    done = subprocess.run(argv, cwd=repo, env=fake_env(home, bin_dir, extra), capture_output=True, text=True,
                          timeout=180, check=False)
    problems = [line for line in done.stdout.splitlines() if line.startswith("problem: ")]
    money = [line for line in done.stdout.splitlines() if line.startswith("money now: ")]
    if paid:
        ok = done.returncode == 0 and not problems and money != [] and "$40.00" in money[0]
    else:
        ok = done.returncode == 1 and len(problems) == 1 and "balance probe exited with 3" in problems[0]
    return {"pass": ok, "exit": done.returncode, "problems": problems, "money": money,
            "stderr_tail": done.stderr[-400:]}


def rendered(repo: Path, name: str, launcher: Path, barrier_seconds: int) -> tuple[list[str], dict[str, str], Path]:
    sys.dont_write_bytecode = True
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from campaign import plan as campaign_plan

    loaded = campaign_plan.load(repo / PLAN_REL)
    arm, lane = loaded.arm(name), loaded.lane_of(name)
    argv, env = campaign_plan.arm_command(loaded, name, arm.tasks, arm.attempts, lane.concurrency)
    assert argv[:3] == ["keys", "run", "--"], argv[:3]
    argv = argv[3:]
    argv[argv.index("--launcher") + 1] = str(launcher)
    argv[argv.index("--barrier-seconds") + 1] = str(barrier_seconds)
    return argv, env, loaded.cwd


def spawn_arms(repo: Path, home: Path, bin_dir: Path, names: list[str], extra: dict[str, str],
               barrier_seconds: int) -> list[int]:
    procs = []
    for name in names:
        argv, env, cwd = rendered(repo, name, home / "stub_launcher.py", barrier_seconds)
        full = {**fake_env(home, bin_dir, {}), **env, **extra}
        procs.append(subprocess.Popen(sandboxed(argv), cwd=cwd, env=full, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
    codes = []
    for proc in procs:
        output, _ = proc.communicate(timeout=barrier_seconds + 120)
        print(output.strip(), flush=True)
        codes.append(proc.returncode)
    return codes


def started(plan: dict[str, Any], name: str) -> float | None:
    path = expand(plan["output_root"]) / name / "started.json"
    if not path.is_file():
        return None
    return finite(json.loads(path.read_text(encoding="utf-8")).get("t"))


def plan_flag(plan: dict[str, Any], flag: str) -> Path:
    argv = plan["command"]["argv"]
    return expand(argv[argv.index(flag) + 1])


def wave_state(plan: dict[str, Any], wave: str) -> dict[str, Any]:
    path = plan_flag(plan, "--waves") / f"{wave}.json"
    if not path.is_file():
        return {"status": "absent"}
    return json.loads(path.read_text(encoding="utf-8"))


def wave_status(plan: dict[str, Any], wave: str) -> str:
    return str(wave_state(plan, wave)["status"])


def rehearse_gates(repo: Path, home: Path, bin_dir: Path, plan: dict[str, Any]) -> dict[str, Any]:
    paid = {"MAC_MATCHED_PAID": "1"}
    approved = {"MAC_MATCHED_APPROVAL": APPROVED}
    results: dict[str, Any] = {}
    codes = spawn_arms(repo, home, bin_dir, ["w01-team-2"], paid, 4)
    results["unapproved plan refuses"] = {"pass": codes == [EXIT_REFUSED] and wave_status(plan, "w01") == "absent",
                                          "codes": codes}
    codes = spawn_arms(repo, home, bin_dir, ["w01-team-2"], approved, 4)
    results["missing MAC_MATCHED_PAID refuses"] = {
        "pass": codes == [EXIT_REFUSED] and wave_status(plan, "w01") == "absent", "codes": codes}
    pair = ["w01-team-2", "w01-silent-2"]
    codes = spawn_arms(repo, home, bin_dir, pair, {**paid, **approved}, 20)
    team, silent = started(plan, pair[0]), started(plan, pair[1])
    drawn = wave_state(plan, "w01").get("first")
    gap = abs(team - silent) if team is not None and silent is not None else None
    first = pair[0] if team is not None and silent is not None and team < silent else pair[1]
    results["pair starts together in drawn order"] = {
        "pass": codes == [0, 0] and gap is not None and STAGGER_SECONDS - 0.5 <= gap <= STAGGER_SECONDS + 1.5
        and drawn == first, "codes": codes, "gap_seconds": gap, "first": drawn}
    return results


def rehearse_waits(repo: Path, home: Path, bin_dir: Path, plan: dict[str, Any]) -> dict[str, Any]:
    env = {"MAC_MATCHED_PAID": "1", "MAC_MATCHED_APPROVAL": APPROVED}
    results: dict[str, Any] = {}
    codes = spawn_arms(repo, home, bin_dir, ["w02-team-2"], env, 4)
    results["lone arm voids its wave"] = {"pass": codes == [EXIT_REFUSED] and wave_status(plan, "w02") == "void"
                                          and started(plan, "w02-team-2") is None, "codes": codes}
    codes = spawn_arms(repo, home, bin_dir, ["w02-silent-2"], env, 4)
    results["late arm of a void wave skips"] = {"pass": codes == [0] and started(plan, "w02-silent-2") is None,
                                                "codes": codes}
    codes = spawn_arms(repo, home, bin_dir, ["w03-team-2", "w03-silent-2"], {**env, "STUB_NO_SETTLE": "w03-team-2"}, 20)
    after = spawn_arms(repo, home, bin_dir, ["w04-team-2", "w04-silent-2"], env, 5)
    results["unsettled earlier wave blocks"] = refused_pair(plan, "w04", codes == [0, 0], after, "no settle row")
    ledger = plan_flag(plan, "--ledger")
    with open(ledger, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "settle", "wave_id": "w03-team-2", "actual_usd": 15.0, "trials": []}) + "\n")
    begun = time.monotonic()
    codes = spawn_arms(repo, home, bin_dir, ["w05-team-2", "w05-silent-2"], env, 30)
    elapsed = round(time.monotonic() - begun, 1)
    results["envelope refuses at once"] = refused_pair(plan, "w05", elapsed < 20, codes, "above the envelope")
    results["envelope refuses at once"]["seconds"] = elapsed
    return results


def refused_pair(plan: dict[str, Any], wave: str, before: bool, codes: list[int], reason: str) -> dict[str, Any]:
    state = wave_state(plan, wave)
    names = [f"{wave}-team-2", f"{wave}-silent-2"]
    launched = [name for name in names if (expand(plan["output_root"]) / name).exists()]
    valid = sorted(codes) in ([EXIT_REFUSED, EXIT_REFUSED], [0, EXIT_REFUSED])
    ok = (before and valid and state.get("status") == "void" and reason in str(state.get("reason"))
          and launched == [])
    return {"pass": ok, "codes": codes, "launched": launched, "state": state}


def cmd_fake_check(args: argparse.Namespace) -> int:
    repo = Path(__file__).resolve().parents[4]
    home = Path(os.path.abspath(args.home))
    assert (repo / PLAN_REL).is_file(), repo
    assert not home.exists(), f"{home} exists; give a new folder"
    home.mkdir(parents=True)
    os.environ["HOME"] = str(home)
    plan = load_plan(repo / PLAN_REL)
    bin_dir = fixture(home, repo, plan)
    results = {
        "check with MAC_MATCHED_PAID=1 and stub keys": run_check(repo, home, bin_dir, True),
        "check without MAC_MATCHED_PAID": run_check(repo, home, bin_dir, False),
        **rehearse_gates(repo, home, bin_dir, plan),
        **rehearse_waits(repo, home, bin_dir, plan),
    }
    verdict = "pass" if all(item["pass"] for item in results.values()) else "fail"
    report = {"check": "pilot plan fake probe and wave barrier", "verdict": verdict, "home": str(home),
              "network": f"denied by {SANDBOX_EXEC} for every child", "plan": str(repo / PLAN_REL), "results": results}
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    for key, item in results.items():
        print(f"{'pass' if item['pass'] else 'FAIL'}: {key}")
    print(f"verdict {verdict}; report {args.report}")
    return 0 if verdict == "pass" else 1


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="pilot_support")
    sub = top.add_subparsers(dest="command", required=True)
    wave = sub.add_parser("wave")
    for flag in ("--plan", "--tasks", "--out", "--harbor", "--waves", "--ledger", "--launcher", "--backend",
                 "--budget-usd", "--timeout-sec", "--grade-at", "--model", "--harness", "--effort"):
        wave.add_argument(flag, required=True)
    for flag in ("--attempts", "--concurrency", "--n-agents", "--barrier-seconds"):
        wave.add_argument(flag, type=int, required=True)
    wave.set_defaults(run=cmd_wave)
    fake = sub.add_parser("fake-check")
    fake.add_argument("--home", required=True)
    fake.add_argument("--report", required=True)
    fake.set_defaults(run=cmd_fake_check)
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    sys.exit(main())
