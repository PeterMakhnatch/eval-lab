import argparse
import contextlib
import fcntl
import functools
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import matched_build as mb

RUN = Path(__file__).resolve().parent
REPO = RUN.parents[3]
GAME_CALL_LOG = RUN / "game_call_log.py"
ARMS = ("solo", "silent", "team")
WAVE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}")
PAID_ARM = re.compile(r"w(\d{2})-(team|silent)-(\d)")
SCENARIO = re.compile(r"[a-z0-9_]{1,32}")
MAX_AGENTS = 8
DEFAULT_UPSTREAM = "https://openrouter.ai/api"
DEFAULT_TTL = 75
DEFAULT_TMP_ROOT = Path("/tmp/fable-day/macrun/tmp")
WALL_EXTRA_SEC = 3600
MAX_WALL_SEC = 4 * 3600
SIGNAL_SETTLE_SEC = 120
TERM_GRACE_SEC = 30
CLEANUP_TIMEOUT_SEC = 120
CONFIG_TIMEOUT_SEC = 120
PINNED_CLI_VERSION = "2.1.330"
PLAN_SCHEMA = "campaign-plan/v1"
PAID_ENV = "MAC_MATCHED_PAID"
APPROVAL = "MAC_MATCHED_APPROVAL"
APPROVED = "approved"
OPENROUTER = "OPENROUTER_API_KEY"
DAYTONA = "DAYTONA_API_KEY"
KEY_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
            OPENROUTER, DAYTONA)
SCRUB_PREFIXES = ("PROXY_", "BOARD_", "MULTI_AGENT_HARNESS_", "PYTHON")
PAID_PINS = {"--backend": "backend", "--n-agents": "n_agents", "--budget-usd": "budget_usd",
             "--timeout-sec": "timeout_sec", "--grade-at": "grade_at", "--model": "model",
             "--harness": "harness", "--effort": "effort", "--ledger": "ledger"}
HUB_GIT_TOKENS = "/run/multi-agent-harness/git-tokens.json"
HUB_TEAM_GIT = "/srv/git/team.git"
HUB_MAIN = f"git -c safe.directory='*' --git-dir={HUB_TEAM_GIT} rev-parse --verify -q refs/heads/main"


def same_path(left: Path | str, right: Path | str) -> bool:
    return os.path.realpath(os.path.expanduser(str(left))) == os.path.realpath(os.path.expanduser(str(right)))


def task_dir(a: argparse.Namespace) -> Path:
    variant = "silent" if a.arm == "silent" else "team"
    return Path(a.build_dir) / f"holdout-{variant}" / "task"


def check_common(a: argparse.Namespace) -> None:
    assert WAVE_ID.fullmatch(a.wave_id), f"wave id {a.wave_id!r}"
    assert a.arm in ARMS
    assert 1 <= a.n_agents <= MAX_AGENTS, "n_agents"
    if a.arm == "solo":
        assert a.n_agents == 1, "solo is 1 agent"
    else:
        assert a.n_agents >= 2, f"{a.arm} is a team of at least 2 agents"
    assert a.budget_usd >= 0, "budget_usd"
    assert a.agent_budget_usd is None or 0 < a.agent_budget_usd <= a.budget_usd, "agent_budget_usd"
    assert 60 <= a.timeout_sec <= 4 * 3600, "timeout_sec"
    assert a.timeout_sec < a.wall_sec <= MAX_WALL_SEC, "wall_sec"
    assert 1 <= a.ttl_minutes <= 24 * 60, "ttl_minutes"
    for what, path in (("ledger", a.ledger), ("jobs dir", a.jobs_dir), ("tmp root", a.tmp_root),
                       ("build dir", a.build_dir)):
        assert Path(path).is_absolute(), f"{what} must be absolute: {path}"
        mb.refuse_foreign(Path(path), what, Path(a.lab))
    assert GAME_CALL_LOG.is_file(), f"missing {GAME_CALL_LOG}"


def check_backend(a: argparse.Namespace) -> None:
    if a.backend == "docker":
        assert a.scripted, "backend docker runs only $0 scripted seats (--scripted); no real model on docker"
        assert a.paid_wave is None and a.plan is None, "a scripted docker run names no paid wave"
        script = Path(a.scripted)
        assert script.is_absolute() and script.suffix == ".py" and script.is_file(), "scripted seat path"
        assert a.scripted_scenario and SCENARIO.fullmatch(a.scripted_scenario), "scripted scenario"
        assert a.docker_memory_mb is None or 512 <= a.docker_memory_mb <= 4096, "docker memory"
        return
    assert a.backend == "daytona"
    assert a.docker_memory_mb is None, "--docker-memory-mb is for local docker proofs only"
    assert not a.scripted and not a.scripted_scenario, "scripted seats run on docker only"
    assert a.paid_wave and a.plan, "daytona is paid: name the plan arm with --paid-wave and --plan"
    assert a.harness == "claude-code", "the paid pilot runs claude-code through the metered proxy"
    assert a.model_upstream == DEFAULT_UPSTREAM, "paid runs use the pinned OpenRouter upstream"
    assert a.ttl_minutes == DEFAULT_TTL, "paid runs use the pinned sandbox ttl"
    assert a.cli_version == PINNED_CLI_VERSION, f"paid runs pin Claude Code {PINNED_CLI_VERSION}"
    assert a.wall_sec <= a.timeout_sec + WALL_EXTRA_SEC, "paid wall limit above the default"


def git_clean(path: Path) -> bool:
    tracked = subprocess.run(["git", "-C", str(REPO), "--no-optional-locks", "ls-files", "--error-unmatch",
                              str(path)], capture_output=True, timeout=60).returncode == 0
    same = subprocess.run(["git", "-C", str(REPO), "--no-optional-locks", "diff", "--quiet", "HEAD", "--",
                           str(path)], capture_output=True, timeout=60).returncode == 0
    return tracked and same


def plan_pins(argv: list) -> dict[str, str]:
    assert isinstance(argv, list) and len(argv) <= 512
    pins: dict[str, str] = {}
    for index, token in enumerate(argv[:-1]):
        if token in PAID_PINS:
            assert token not in pins, f"plan command repeats {token}"
            pins[token] = str(argv[index + 1])
    assert "--agent-budget-usd" not in argv, "the pilot plan pins no per-seat cap"
    return pins


def pin_matches(flag: str, planned: str, value) -> bool:
    if flag in ("--n-agents", "--timeout-sec"):
        return int(planned) == int(value)
    if flag == "--budget-usd":
        return abs(float(planned) - float(value)) < 1e-12
    if flag == "--ledger":
        return same_path(planned, value)
    return planned == str(value)


def check_plan(a: argparse.Namespace) -> dict:
    path = Path(a.plan).resolve()
    assert path.parent == REPO / "campaign" / "plans", f"plan {path} is not a checked-in campaign plan"
    raw = path.read_bytes()
    data = tomllib.loads(raw.decode())
    assert data.get("schema") == PLAN_SCHEMA, "plan schema"
    match = PAID_ARM.fullmatch(a.paid_wave)
    assert match, f"paid wave {a.paid_wave!r} is not a plan arm id"
    assert a.paid_wave == a.wave_id, "--paid-wave must equal --wave-id"
    assert match.group(2) == a.arm and int(match.group(3)) == a.n_agents, "arm id disagrees with --arm/--n-agents"
    arms = [arm for arm in data.get("arms", []) if arm.get("name") == a.wave_id]
    assert len(arms) == 1, f"{a.wave_id} is not exactly 1 arm of {path}"
    assert same_path(arms[0]["tasks"], task_dir(a)), "plan arm tasks is not this build's task"
    assert same_path(data["output_root"], a.jobs_dir), "--jobs-dir must be the plan output_root"
    assert not same_path(data["money"]["ledger"], a.ledger), "trial ledger must not be the campaign ledger"
    pins = plan_pins(data["command"]["argv"])
    missing = sorted(set(PAID_PINS) - set(pins))
    assert not missing, f"plan command does not pin {missing}"
    wrong = [flag for flag, attr in PAID_PINS.items() if not pin_matches(flag, pins[flag], getattr(a, attr))]
    assert not wrong, f"flags differ from the plan: {wrong}"
    assert a.agent_budget_usd is None, "the plan pins no per-seat cap"
    approval = (data["command"].get("env") or {}).get(APPROVAL)
    return {"plan": str(path), "plan_sha256": mb.sha256_bytes(raw), "plan_id": data.get("id"),
            "plan_checked_in": git_clean(path), "approved": approval == APPROVED,
            "arm_reserve_usd": arms[0].get("reserve_usd")}


def load_runtime(lab: Path):
    sys.dont_write_bytecode = True
    for entry in (str(lab / "agents"), str(lab)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    import run_hardened
    assert Path(run_hardened.__file__).resolve() == (lab / "run_hardened.py").resolve()
    assert run_hardened.HARD_MAX_CONCURRENT == 2
    return run_hardened


def harbor_extra(a: argparse.Namespace, task: Path) -> list[str]:
    hub = task / "environment" / "hub"
    plugins = [task / "build" / "holdout_hub_plugin.py", hub / "game_plugin.py", hub / "keepalive_plugin.py",
               GAME_CALL_LOG]
    assert all(p.is_file() for p in plugins) and (hub / "board_policy.py").is_file()
    args = ["--ek", f"board_plugins={','.join(str(p) for p in plugins)}",
            "--ek", f"board_policy={hub / 'board_policy.py'}", "--ek", "hub_state_dirs=/var/lib/holdout",
            "--ek", f"ttl_minutes={a.ttl_minutes}", "--ek", "seal_git=true"]
    if a.agent_budget_usd is not None:
        args += ["--ek", f"agent_budget_usd={a.agent_budget_usd}"]
    if a.backend == "docker":
        args += ["--ek", "allow_unenforced_network=true"]
        if a.docker_memory_mb is not None:
            args += ["--override-memory-mb", str(a.docker_memory_mb)]
    else:
        args += ["--ek", f"model_upstream={a.model_upstream}"]
    marks = ["--ak", f"orchestration={Path(a.build_dir) / mb.ORCHESTRATION}", "--ak", f"grade_at={a.grade_at}",
             "--ak", "checkpoint_every_min=15"]
    if a.scripted:
        return args + ["-a", "matched_trial:MatchedScriptedAgent", "--ak", f"script_path={a.scripted}",
                       "--ak", f"scenario={a.scripted_scenario}", "--ak", f"arm={a.arm}",
                       "--ak", "agent_names=numbered", *marks]
    args += ["-a", "matched_trial:MatchedTeamAgent", "--ak", f"harness={a.harness}", "--ak", f"model={a.model}",
             "--ak", f"cli_version={a.cli_version}", *marks]
    if a.effort and a.harness in ("claude-code", "codex"):
        args += ["--ak", f"effort={a.effort}"]
    return args


def child_env(a: argparse.Namespace, environ: dict) -> tuple[dict, list[str], list[str]]:
    env = {k: v for k, v in environ.items()
           if k not in KEY_VARS and not k.startswith(SCRUB_PREFIXES) and k != "TMPDIR"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join([str(RUN), str(Path(a.lab)), str(Path(a.lab) / "agents")])
    env["TMPDIR"] = str(Path(a.tmp_root) / a.wave_id)
    if a.backend == "daytona":
        if environ.get(OPENROUTER):
            env["ANTHROPIC_API_KEY"] = environ[OPENROUTER]
        if environ.get(DAYTONA):
            env[DAYTONA] = environ[DAYTONA]
    removed = sorted(set(environ) - set(env))
    set_names = sorted(k for k in env if environ.get(k) != env[k] or k in ("ANTHROPIC_API_KEY", DAYTONA))
    assert all(k not in env for k in KEY_VARS if k not in ("ANTHROPIC_API_KEY", DAYTONA))
    if a.backend == "docker":
        assert not any(k in env for k in KEY_VARS), "a scripted run carries no key"
    return env, set_names, removed


def launch_spec(a: argparse.Namespace, rh, environ: dict) -> dict:
    task = task_dir(a)
    ns = argparse.Namespace(task=str(task), backend=a.backend, n_agents=a.n_agents, budget_usd=a.budget_usd,
                            proxy_max_concurrent=rh.HARD_MAX_CONCURRENT, wave_id=a.wave_id,
                            timeout_sec=a.timeout_sec, harbor_args=harbor_extra(a, task))
    jobs = Path(a.jobs_dir)
    config_argv = rh.harbor_cmd(ns, jobs) + ["--print-config"]
    harbor = str(Path(a.lab) / ".venv" / "bin" / "harbor")
    assert config_argv[:2] == [harbor, "run"]
    assert "run_hardened:HardenedTeamEnvironment" in config_argv and "seal_git=true" in config_argv
    config_path = jobs / f"{a.wave_id}.job.json"
    env, set_names, removed = child_env(a, environ)
    reserve = rh.reservation(task, a.n_agents, a.budget_usd, a.timeout_sec, a.backend)
    return {"config_argv": config_argv, "config_path": str(config_path),
            "argv": [harbor, "run", "-c", str(config_path)], "cwd": str(jobs), "env": env,
            "env_set": set_names, "env_removed": removed, "log": str(jobs / f"{a.wave_id}.harbor.log"),
            "pid_file": str(jobs / f"{a.wave_id}.harbor.pid"), "job": str(jobs / a.wave_id),
            "reserve": reserve, "task": str(task)}


def materialize(a: argparse.Namespace, spec: dict) -> dict:
    scratch = Path(spec["env"]["TMPDIR"])
    scratch.mkdir(parents=True, exist_ok=True)
    keyless = {k: v for k, v in spec["env"].items() if k not in KEY_VARS}
    done = subprocess.run(spec["config_argv"], env=keyless, cwd=str(scratch), capture_output=True,
                          text=True, timeout=CONFIG_TIMEOUT_SEC)
    assert done.returncode == 0, f"harbor --print-config failed ({done.returncode})"
    text = done.stdout
    start = text.find("{")
    assert start >= 0, "harbor --print-config printed no JSON"
    config = json.loads(text[start:])
    assert isinstance(config, dict) and config.get("job_name") == a.wave_id, "config job_name"
    assert same_path(config.get("jobs_dir", ""), a.jobs_dir), "config jobs_dir"
    blob = json.dumps(config, sort_keys=True)
    assert "run_hardened:HardenedTeamEnvironment" in blob and "seal_git" in blob
    assert all(name not in blob for name in KEY_VARS)
    return config


def append_row(rh, ledger: Path, row: dict) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger.with_name(ledger.name + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            assert same_path(rh.LEDGER, ledger)
            rh.append_ledger(row)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def provenance(a: argparse.Namespace, spec: dict, build: dict, plan: dict | None) -> dict:
    files = {"matched_trial.py": Path(__file__).resolve(), "matched_build.py": RUN / "matched_build.py",
             "game_call_log.py": GAME_CALL_LOG}
    if a.scripted:
        files["scripted_seat"] = Path(a.scripted)
    return {"wave_id": a.wave_id, "arm": a.arm, "n_agents": a.n_agents, "backend": a.backend,
            "budget_usd": a.budget_usd, "agent_budget_usd": a.agent_budget_usd, "scripted": a.scripted,
            "scenario": a.scripted_scenario, "argv": spec["argv"], "cwd": spec["cwd"],
            "env_set": spec["env_set"], "env_removed": spec["env_removed"], "build": build, "plan": plan,
            "sha256": {name: mb.sha256_file(path) for name, path in files.items()}}


def gates(a: argparse.Namespace) -> tuple[dict, dict | None]:
    a.lab, a.build_dir = str(Path(a.lab).resolve()), str(Path(a.build_dir).expanduser())
    for name in ("ledger", "jobs_dir", "tmp_root"):
        setattr(a, name, os.path.abspath(os.path.expanduser(getattr(a, name))))
    check_common(a)
    check_backend(a)
    plan = check_plan(a) if a.backend == "daytona" else None
    build = mb.verify_build(Path(a.build_dir), Path(a.lab))
    assert task_dir(a).is_dir(), task_dir(a)
    assert not (Path(a.jobs_dir) / a.wave_id).exists(), f"{Path(a.jobs_dir) / a.wave_id} exists"
    if a.out is not None:
        assert os.path.abspath(os.path.expanduser(a.out)) == str(Path(a.jobs_dir) / a.wave_id), \
            "--out must be <jobs-dir>/<wave-id>"
    return build, plan


def signal_group(proc: subprocess.Popen, number: int) -> None:
    assert proc.pid > 1
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, number)


def stop_harbor(proc: subprocess.Popen) -> int:
    signal_group(proc, signal.SIGTERM)
    try:
        return proc.wait(timeout=SIGNAL_SETTLE_SEC)
    except subprocess.TimeoutExpired:
        signal_group(proc, signal.SIGKILL)
    return proc.wait(timeout=TERM_GRACE_SEC)


def supervise(proc: subprocess.Popen, wall_sec: int, stop: dict) -> tuple[int, str | None]:
    deadline = time.monotonic() + wall_sec
    for _ in range(wall_sec + 2):
        try:
            return proc.wait(timeout=1), None
        except subprocess.TimeoutExpired:
            pass
        if stop["signal"] is not None:
            return stop_harbor(proc), f"signal {stop['signal']}"
        if time.monotonic() >= deadline:
            break
    return stop_harbor(proc), "wall"


def pending_sandboxes(job: Path) -> list[str]:
    if not job.is_dir():
        return []
    ledgers = sorted(job.rglob("sandbox-ledger.jsonl"))[:64]
    created, deleted = set(), set()
    for ledger in ledgers:
        for line in ledger.read_text().splitlines()[:100_000]:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("deleted"):
                deleted.add(str(row["deleted"]))
            elif isinstance(row, dict) and row.get("id"):
                created.add(str(row["id"]))
    return sorted(created - deleted)


def cleanup(a: argparse.Namespace, job: Path, env: dict) -> dict:
    pending = pending_sandboxes(job)
    if not pending:
        return {"pending_sandboxes": 0, "cleanup": "not needed"}
    tool = Path(a.lab) / ".venv" / "bin" / "multi-agent-harness-cleanup"
    try:
        done = subprocess.run([str(tool), str(job)], env=env, cwd=str(job), capture_output=True, text=True,
                              timeout=CLEANUP_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return {"pending_sandboxes": len(pending), "cleanup": "timeout"}
    lines = (done.stdout or "").splitlines()[-40:]
    gone = sum(1 for line in lines if "deleted" in line or "already gone" in line)
    return {"pending_sandboxes": len(pending), "cleanup": "ran", "cleanup_rc": done.returncode,
            "cleanup_gone_lines": gone}


def settle(rh, job: Path, task: Path) -> dict:
    trials = sorted(p for p in job.iterdir() if p.is_dir() and (p / "config.json").is_file()) if job.is_dir() else []
    if not trials:
        return {"trials": [], "model_usd": None, "compute_usd": None, "actual_usd": None, "trial_dir": None}
    out = rh.settle(job, task)
    unknown = sum(1 for row in out["trials"] if row.get("model_usd") is None)
    unknown_compute = any(row.get("backend") is None or row.get("undeleted_sandboxes") for row in out["trials"])
    actual = None if any(row.get("backend") != "docker" for row in out["trials"]) and (
        unknown or unknown_compute
    ) else out["actual_usd"]
    return {**out, "actual_usd": actual, "unknown_model_usd_trials": unknown, "trial_dir": str(trials[0])}


def install_stop() -> dict:
    stop: dict = {"signal": None}

    def note(signum, _frame) -> None:
        stop["signal"] = signum

    signal.signal(signal.SIGTERM, note)
    signal.signal(signal.SIGINT, note)
    signal.signal(signal.SIGHUP, note)
    return stop


def run_paid_env(a: argparse.Namespace) -> None:
    if a.backend != "daytona":
        return
    assert os.environ.get(PAID_ENV) == "1", f"{PAID_ENV}=1 is required for a paid run"
    assert os.environ.get(OPENROUTER), f"{OPENROUTER} must be set (keys run -- ...)"
    assert os.environ.get(DAYTONA), f"{DAYTONA} must be set (keys run -- ...)"


def dry_run(a: argparse.Namespace, spec: dict, build: dict, plan: dict | None) -> int:
    shown = {k: v for k, v in spec.items() if k != "env"}
    shown["env_names_present"] = {name: bool(os.environ.get(name)) for name in (PAID_ENV, OPENROUTER, DAYTONA)}
    shown["ledger"] = a.ledger
    shown["build"] = build
    shown["plan"] = plan
    config = materialize(a, spec)
    shown["config_sha256"] = mb.sha256_bytes((json.dumps(config, indent=2, sort_keys=True) + "\n").encode())
    print(json.dumps(shown, indent=1, sort_keys=True))
    return 0


def launch(a: argparse.Namespace, spec: dict, stop: dict) -> tuple[int | None, str | None]:
    if stop["signal"] is not None:
        return None, f"signal {stop['signal']} before launch"
    with open(spec["log"], "w") as log:
        proc = subprocess.Popen(spec["argv"], env=spec["env"], cwd=spec["cwd"], stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
        Path(spec["pid_file"]).write_text(f"{proc.pid}\n")
        return supervise(proc, int(a.wall_sec), stop)


def cmd_run(a: argparse.Namespace) -> int:
    build, plan = gates(a)
    rh = load_runtime(Path(a.lab))
    spec = launch_spec(a, rh, dict(os.environ))
    if a.dry_run:
        return dry_run(a, spec, build, plan)
    run_paid_env(a)
    assert plan is None or plan["plan_checked_in"], "a paid run needs the plan committed and unchanged"
    assert plan is None or plan["approved"], f"the plan's command.env.{APPROVAL} is not {APPROVED!r}"
    stop = install_stop()
    ledger, jobs, job = Path(a.ledger), Path(a.jobs_dir), Path(spec["job"])
    rh.LEDGER = ledger
    config = materialize(a, spec)
    jobs.mkdir(parents=True, exist_ok=True)
    with open(spec["config_path"], "x") as handle:
        handle.write(json.dumps(config, indent=2, sort_keys=True) + "\n")
    tags = {"arm": a.arm, "study": "mac-matched-budget", "paid_wave": a.paid_wave,
            "plan_sha256": (plan or {}).get("plan_sha256"), "scripted": bool(a.scripted)}
    append_row(rh, ledger, {"event": "reserve", "wave_id": a.wave_id, "provider": a.backend, "phase": "bespoke",
                            "n_trials": 1, "n_agents": a.n_agents, "task": Path(spec["task"]).name,
                            **spec["reserve"], **tags})
    (jobs / f"{a.wave_id}.matched.json").write_text(
        json.dumps(provenance(a, spec, build, plan), indent=1, sort_keys=True) + "\n")
    t0 = time.time()
    rc, killed = launch(a, spec, stop)
    swept = cleanup(a, job, spec["env"])
    out = settle(rh, job, Path(spec["task"]))
    append_row(rh, ledger, {"event": "settle", "wave_id": a.wave_id, "provider": a.backend, "phase": "bespoke",
                            "harbor_rc": rc, "wall_sec": round(time.time() - t0, 1), "killed": killed,
                            **swept, **{k: v for k, v in out.items() if k != "trial_dir"}, **tags})
    print(json.dumps({"wave_id": a.wave_id, "harbor_rc": rc, "killed": killed, "trial_dir": out["trial_dir"],
                      "log": spec["log"], "model_usd": out["model_usd"], "compute_usd": out["compute_usd"],
                      "actual_usd": out["actual_usd"], **swept}, sort_keys=True))
    return rc if killed is None and rc is not None else 124


def cmd_prepare(a: argparse.Namespace) -> int:
    sys.dont_write_bytecode = True
    result = mb.prepare(Path(a.lab).resolve(), Path(os.path.abspath(os.path.expanduser(a.build_dir))))
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="matched_trial")
    sub = p.add_subparsers(dest="cmd", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--build-dir", default=str(mb.DEFAULT_BUILD))
    prep.add_argument("--lab", default=str(mb.LAB))
    r = sub.add_parser("run")
    r.add_argument("--arm", choices=ARMS, required=True)
    r.add_argument("--n-agents", type=int, required=True)
    r.add_argument("--budget-usd", type=float, required=True)
    r.add_argument("--agent-budget-usd", type=float)
    r.add_argument("--wave-id", required=True)
    r.add_argument("--jobs-dir", required=True)
    r.add_argument("--ledger", required=True)
    r.add_argument("--backend", choices=["docker", "daytona"], required=True)
    r.add_argument("--timeout-sec", type=int, default=1800)
    r.add_argument("--grade-at", default="15m,30m")
    r.add_argument("--model", default="claude-haiku-5-5")
    r.add_argument("--harness", default="claude-code")
    r.add_argument("--effort", default="medium")
    r.add_argument("--model-upstream", default=DEFAULT_UPSTREAM)
    r.add_argument("--ttl-minutes", type=int, default=DEFAULT_TTL)
    r.add_argument("--scripted")
    r.add_argument("--scripted-scenario")
    r.add_argument("--paid-wave")
    r.add_argument("--plan")
    r.add_argument("--build-dir", default=str(mb.DEFAULT_BUILD))
    r.add_argument("--lab", default=str(mb.LAB))
    r.add_argument("--tmp-root", default=str(DEFAULT_TMP_ROOT))
    r.add_argument("--wall-sec", type=int)
    r.add_argument("--docker-memory-mb", type=int)
    r.add_argument("--cli-version", default=PINNED_CLI_VERSION)
    r.add_argument("--out")
    r.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    if a.cmd == "run" and a.wall_sec is None:
        a.wall_sec = a.timeout_sec + WALL_EXTRA_SEC
    if a.cmd == "run" and a.scripted and not a.scripted_scenario:
        a.scripted_scenario = "hello"
    try:
        return cmd_prepare(a) if a.cmd == "prepare" else cmd_run(a)
    except AssertionError as e:
        print(f"matched_trial: refused: {e}", file=sys.stderr)
        return 2


async def revoke_team_git(agent, env) -> None:
    import asyncio

    from multi_agent_harness.harbor.agent import GIT_CREDENTIALS
    seats = list(agent.agents)
    assert seats and hasattr(env, "hub") and hasattr(env, "_ledger")
    before = (await env.hub.check(HUB_MAIN, "read hub main before git revocation")).strip()
    await env.hub.upload_bytes(b"{}", HUB_GIT_TOKENS)
    await env.hub.check(f"chmod 600 {HUB_GIT_TOKENS} && test \"$(cat {HUB_GIT_TOKENS})\" = '{{}}'",
                        "revoke the team git tokens")
    after = (await env.hub.check(HUB_MAIN, "read hub main after git revocation")).strip()
    assert before and before == after, "git revocation changed hub main"
    workdir = shlex.quote(agent._workdir(env))
    probe = (f"set -e; : > {GIT_CREDENTIALS}; git config --global core.askPass /bin/false; cd {workdir}; "
             f"if GIT_TERMINAL_PROMPT=0 timeout 60 git ls-remote origin >/dev/null 2>&1; then exit 3; fi; "
             f"test ! -s {GIT_CREDENTIALS}; git rev-parse --verify -q HEAD >/dev/null")
    results = await asyncio.gather(*(env.service_exec(probe, service=s, timeout_sec=120) for s in seats))
    open_seats = [s for s, r in zip(seats, results, strict=True) if r.return_code != 0]
    if open_seats:
        raise RuntimeError(f"team git still reachable or seat clone missing after revocation: {open_seats}")
    env._ledger(event="matched_git_revoked", tokens_file=HUB_GIT_TOKENS, hub_main_before=before,
                hub_main_after=after, seats=sorted(seats), seat_probe="git ls-remote origin refused",
                setup_clone="TeamAgent._configure cloned main before revocation")


def revoking(base):
    class GitRevoked(base):
        async def _lock_down(self, env) -> str:
            mode = await super()._lock_down(env)
            await revoke_team_git(self, env)
            return mode

    return GitRevoked


@functools.cache
def team_agent_class():
    from multi_agent_harness.harbor.agent import TeamAgent

    class MatchedTeamAgent(revoking(TeamAgent)):
        pass

    return MatchedTeamAgent


@functools.cache
def scripted_agent_class():
    from multi_agent_harness.harbor.agent import TeamAgent, _put, header_vars
    from scripted_team_agent import SEAT_CONF, SEAT_SCRIPT, ScriptedTeamAgent

    class MatchedScriptedAgent(revoking(ScriptedTeamAgent)):
        def __init__(self, *args, script_path: str = "", scenario: str = "", arm: str = "", **kwargs):
            source = Path(script_path)
            assert source.is_absolute() and source.suffix == ".py" and source.is_file(), script_path
            assert SCENARIO.fullmatch(scenario) and arm in ARMS
            kwargs["harness"] = "codex"
            kwargs.setdefault("agent_names", "numbered")
            TeamAgent.__init__(self, *args, **kwargs)
            assert self.harnesses == {"codex"}
            self.script = scenario
            self.matched_arm = arm
            self.matched_source = source.read_text()

        async def _configure(self, env, name: str) -> None:
            await TeamAgent._configure(self, env, name)
            conf = {"header_vars": header_vars(env), "scenario": self.script, "arm": self.matched_arm,
                    "n_agents": len(self.agents), "workdir": self._workdir(env)}
            await _put(env, name, SEAT_SCRIPT, self.matched_source)
            await _put(env, name, SEAT_CONF, json.dumps(conf, allow_nan=False))

        async def _proof_stop(self) -> None:
            import asyncio

            await asyncio.sleep(25)
            seat = "agent-1"
            assert seat in self.running, "O3 holding seat must still be running"
            self._env._ledger(event="matched_proof_stop_begin", seat=seat, proof_ts=time.time())
            await self.stop(seat)
            assert seat in self.stopped, "O3 must use the harness stop() method"
            self._env._ledger(event="matched_proof_stop_done", seat=seat, proof_ts=time.time())

        async def _supervise(self) -> str:
            if self.script != "o3":
                return await super()._supervise()
            import asyncio

            proof = asyncio.create_task(self._proof_stop())
            try:
                return await super()._supervise()
            finally:
                if proof.done():
                    proof.result()
                else:
                    proof.cancel()
                    await asyncio.gather(proof, return_exceptions=True)

    return MatchedScriptedAgent


def __getattr__(name: str):
    if name == "MatchedTeamAgent":
        return team_agent_class()
    if name == "MatchedScriptedAgent":
        return scripted_agent_class()
    raise AttributeError(name)


if __name__ == "__main__":
    sys.exit(main())
