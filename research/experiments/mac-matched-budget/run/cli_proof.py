import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

RUN_DIR = Path(__file__).resolve().parent
B_ROOT = Path.home() / "Developer/bespoke-macbench-lab"
BUILD_DIR = Path.home() / "Developer/muster-runs/day-2026-11-24/mac-run/build"
PROOFS_DIR = Path.home() / "Developer/muster-runs/day-2026-11-24/mac-run/proofs"
SCRATCH = Path("/tmp/fable-day/macrun/cli")
CHECKS = ("O1", "O6")
CC_VERSION = "2.1.330"
BOOTSTRAP_URL = "https://downloads.claude.ai/claude-code-releases/bootstrap.sh"
HARBOR_INSTALL = (f"curl -fsSL {BOOTSTRAP_URL} | bash -s -- {CC_VERSION} && "
                  "echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.bashrc && "
                  'export PATH="$HOME/.local/bin:$PATH" && claude --version')
DEADLINE_SEC = 570.0
BUILD_MAX_SEC = 330.0
CLEANUP_RESERVE_SEC = 25.0
MODEL = "claude-haiku-5-5"
PORT = 18791
FAKE_KEY = "multi-agent-harness-fable-fake-not-a-key"
SEAT = "seat1"
WORKDIR = "/app"
LANE_LABEL = "fable.lane=mac-run"
O1_FRESH_SEC = 90
O1_RESUME_SEC = 45
O6_SEC = 75
HORIZON_SEC = 1800.0
CAP_AT_SEC = 600.0
MAX_FILES = 10000
SENT_STATUSES = {124, 137}
PINNED_KEEPALIVE = "upstream/tasks/holdout/task/environment/hub/keepalive.py"
FIXED_KEEPALIVE = "orchestrations/holdout_keepalive_fixed_horizon.py"
PINNED_MANAGED = "build/holdout-team/task/environment/claude-managed.json"
CHANGED_ENV = "holdout-team/task/environment"
WEB_TOOLS = ("WebSearch", "WebFetch")


class Obstacle(Exception):
    pass


class Deadline:
    def __init__(self, total: float) -> None:
        assert 0 < total < 600
        self.end = time.monotonic() + total

    def left(self) -> float:
        return self.end - time.monotonic()

    def cap(self, want: float, need: float) -> float:
        assert want > 0 and need > 0
        usable = self.left() - CLEANUP_RESERVE_SEC
        if usable < need:
            raise Obstacle(f"deadline: {usable:.0f} s left, step needs {need:.0f} s")
        return min(want, usable)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True, default=str) + "\n")
    return path


def b_agent_module(b_root: Path):
    sys.dont_write_bytecode = True
    harness_src = b_root / "upstream/harness/src"
    assert harness_src.is_dir(), harness_src
    if str(harness_src) not in sys.path:
        sys.path.append(str(harness_src))
    try:
        import multi_agent_harness.harbor.agent as agent_mod
    except ImportError as e:
        raise Obstacle(f"B harness not importable ({type(e).__name__}: {e}); run under B/.venv/bin/python") from e
    return agent_mod


def harness_stub(timeout_sec: int) -> SimpleNamespace:
    assert timeout_sec > 0
    seat = SimpleNamespace(harness="claude-code", short_model=MODEL, effort=None, model=f"anthropic/{MODEL}")
    return SimpleNamespace(_seat=lambda slot=None: seat, timeout_sec=timeout_sec, _claude_sub=False,
                           _claude_scrub=True, orchestration=SimpleNamespace(cli_subagents=False))


def harness_files(agent_mod) -> dict[str, str]:
    stub = harness_stub(60)
    env = SimpleNamespace(team_proxy_url=f"http://127.0.0.1:{PORT}", team_board_url="http://127.0.0.1:9/api",
                          team_hub_headers=None, team_preview_token=None)
    files = {"agent.env": agent_mod.TeamAgent._agent_env(stub, env, SEAT, None, FAKE_KEY, SEAT),
             "managed-settings.json": agent_mod.TeamAgent._claude_settings(stub)}
    assert "ANTHROPIC_BASE_URL" in files["agent.env"] and FAKE_KEY in files["agent.env"]
    return files


def harness_command(agent_mod, timeout_sec: int, session: int, resume: bool) -> str:
    assert session >= 1
    cmd = agent_mod.TeamAgent._command(harness_stub(timeout_sec), WORKDIR, agent_mod.LOGS, session, resume,
                                       slot=SEAT)
    assert "--permission-mode=bypassPermissions" in cmd and f"timeout -k {agent_mod.KILL_GRACE_SEC}" in cmd
    return cmd


async def proxy_reply(server, httpx, scratch: Path, budget: float, agent_budget: float | None) -> dict:
    scratch.mkdir(parents=True, exist_ok=True)
    log, keys = scratch / "proxy-usage.jsonl", scratch / "proxy-keys.json"
    for path in (log, keys):
        path.unlink(missing_ok=True)
    log.write_text(json.dumps({"agent": SEAT, "usd": agent_budget or budget, "tokens": {}}) + "\n")
    upstream = httpx.MockTransport(lambda request: httpx.Response(599))
    proxy = server.Proxy("fable-admin", budget, agent_budget, log_path=str(log), keys_path=str(keys),
                         api_keys={"anthropic": "fable-fake-upstream"}, transport=upstream, max_concurrent=2)
    key = proxy.register(SEAT)
    body = {"model": MODEL, "max_tokens": 16, "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    transport = httpx.ASGITransport(app=server.build_app(proxy))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
            resp = await client.post("/anthropic/v1/messages", json=body,
                                     headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    finally:
        await proxy.client.aclose()
    keep = {k: v for k, v in resp.headers.items() if k.lower() not in ("content-length", "date", "server")}
    rows = [json.loads(line) for line in log.read_text().splitlines()[1:8]]
    keys.unlink(missing_ok=True)
    return {"status": resp.status_code, "headers": keep, "body": resp.text, "usage_rows": rows}


def record_proxy_429(b_root: Path, scratch: Path) -> dict:
    b_agent_module(b_root)
    try:
        import httpx
        from multi_agent_harness.proxy import server
    except ImportError as e:
        raise Obstacle(f"B proxy not importable: {e}") from e
    team = asyncio.run(proxy_reply(server, httpx, scratch / "proxy-team", 0.10, None))
    seat = asyncio.run(proxy_reply(server, httpx, scratch / "proxy-seat", 1.0, 0.05))
    for got in (team, seat):
        assert got["status"] == 429, got
        assert json.loads(got["body"])["error"]["type"] == "budget_exceeded"
        assert any(r.get("reason") == "budget" for r in got["usage_rows"])
        assert got["headers"].get("x-should-retry") == "false", got["headers"]
    return {"team_cap": team, "seat_cap": seat, "server_sha256": sha256(Path(server.__file__).read_bytes())}


def docker(args: list[str], timeout: float, env: dict | None = None) -> subprocess.CompletedProcess:
    assert args and timeout > 0
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, **(env or {})}, check=False)
    except subprocess.TimeoutExpired as e:
        raise Obstacle(f"docker {args[0]} exceeded {timeout:.0f} s") from e
    except FileNotFoundError as e:
        raise Obstacle("docker CLI not found") from e


def context_digest(ctx: Path) -> str:
    files = sorted(p for p in ctx.rglob("*") if p.is_file())
    assert 0 < len(files) <= MAX_FILES, len(files)
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(ctx)).encode() + b"\0" + sha256(path.read_bytes()).encode())
    return digest.hexdigest()


def build_context(build_dir: Path, scratch: Path) -> tuple[Path, dict]:
    source = build_dir / CHANGED_ENV
    if not (source / "Dockerfile").is_file():
        raise Obstacle(f"no prepared build at {source}; run matched_trial.py prepare first")
    managed = json.loads((source / "claude-managed.json").read_text())
    deny = managed["permissions"]["deny"]
    if not all(tool in deny for tool in WEB_TOOLS):
        raise Obstacle(f"prepared managed settings lack {WEB_TOOLS} in deny: {deny}")
    ctx = scratch / "image-context"
    shutil.rmtree(ctx, ignore_errors=True)
    shutil.copytree(source, ctx, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    original = (source / "Dockerfile").read_bytes()
    extra = ("\nSHELL [\"/bin/bash\", \"-c\"]\n"
             "RUN apt-get update && apt-get install -y --no-install-recommends procps git curl "
             "&& rm -rf /var/lib/apt/lists/*\n"
             f"RUN set -euo pipefail; {HARBOR_INSTALL}\n")
    (ctx / "Dockerfile").write_bytes(original + extra.encode())
    info = {"source": str(source), "dockerfile_sha256": sha256(original), "appended": extra,
            "managed_settings_sha256": sha256((source / "claude-managed.json").read_bytes()),
            "deny": deny, "context_sha256": context_digest(ctx)}
    return ctx, info


def harbor_install_matches(b_root: Path) -> bool:
    paths = sorted((b_root / ".venv/lib").glob("python3*/site-packages/harbor/agents/installed/claude_code.py"))
    assert len(paths) <= 4
    return bool(paths) and f"curl -fsSL {BOOTSTRAP_URL} | bash -s --" in paths[0].read_text()


class Docker:
    def __init__(self, run_id: str, deadline: Deadline, keep_image: bool) -> None:
        assert run_id.isalnum()
        self.run_id = run_id
        self.deadline = deadline
        self.keep_image = keep_image
        self.containers: list[str] = []
        self.image: str | None = None
        self.built = False
        self.log: list[dict] = []
        self.removed: list[str] = []

    def call(self, args: list[str], want: float, need: float = 5.0) -> subprocess.CompletedProcess:
        timeout = self.deadline.cap(want, need)
        t0 = time.monotonic()
        result = docker(args, timeout)
        self.log.append({"args": args[:3], "rc": result.returncode, "sec": round(time.monotonic() - t0, 1)})
        return result

    def preflight(self) -> dict:
        load = os.getloadavg()[0]
        for _ in range(6):
            if load <= 20:
                break
            time.sleep(10)
            load = os.getloadavg()[0]
        if load > 20:
            raise Obstacle(f"load average {load:.1f} above 20")
        info = self.call(["info", "--format", "{{.MemTotal}} {{.OSType}} {{.Architecture}}"], 30)
        if info.returncode != 0:
            raise Obstacle(f"docker info failed: {info.stderr.strip()[-200:]}")
        return {"load": load, "docker": info.stdout.strip()}

    def ensure_image(self, ctx: Path, info: dict) -> dict:
        tag = f"fable-macrun-cli:cc{CC_VERSION}-{info['context_sha256'][:12]}"
        self.image = tag
        if self.call(["image", "inspect", "--format", "{{.Id}}", tag], 30).returncode == 0:
            return {"image": tag, "built": False}
        want = min(BUILD_MAX_SEC, self.deadline.left() - 200)
        t0 = time.monotonic()
        self.built = True
        result = self.call(["build", "--label", LANE_LABEL, "--label", "fable.proof=cli", "-t", tag,
                            str(ctx)], max(want, 60), need=60)
        (ctx.parent / "build.log").write_text(result.stdout[-20000:] + "\n" + result.stderr[-20000:])
        if result.returncode != 0:
            raise Obstacle(f"docker build failed rc={result.returncode}: {result.stderr.strip()[-300:]}")
        return {"image": tag, "built": True, "build_sec": round(time.monotonic() - t0, 1)}

    def start(self, case: str) -> str:
        assert self.image and case.replace("-", "").isalnum()
        name = f"fable-macrun-cli-{self.run_id}-{case}"
        self.containers.append(name)
        result = self.call(["run", "-d", "--name", name, "--label", LANE_LABEL, "--label",
                            f"fable.proof.run={self.run_id}", "--network", "none", "--memory", "1g",
                            "--cpus", "2", "--pids-limit", "512", self.image, "sleep", "infinity"], 60)
        if result.returncode != 0:
            raise Obstacle(f"docker run failed: {result.stderr.strip()[-300:]}")
        return name

    def sh(self, name: str, script: str, want: float, env: dict | None = None) -> subprocess.CompletedProcess:
        flags = [x for k, v in (env or {}).items() for x in ("-e", f"{k}={v}")]
        return self.call(["exec", *flags, name, "bash", "-c", script], want)

    def remove(self, name: str) -> None:
        assert name in self.containers
        if name not in self.removed and docker(["rm", "-f", name], 30).returncode == 0:
            self.removed.append(name)

    def cleanup(self) -> dict:
        errors = []
        for name in self.containers[:16]:
            self.remove(name)
            if name not in self.removed:
                errors.append(name)
        stray = docker(["ps", "-aq", "--filter", f"label=fable.proof.run={self.run_id}"], 30).stdout.split()
        for ident in stray[:16]:
            docker(["rm", "-f", ident], 30)
        image_removed = False
        if self.built and self.image and not self.keep_image:
            image_removed = docker(["image", "rm", self.image], 60).returncode == 0
        left = docker(["ps", "-aq", "--filter", f"label=fable.proof.run={self.run_id}"], 30).stdout.split()
        return {"containers_removed": self.removed, "errors": errors, "stray_removed": stray, "left": left,
                "image": self.image, "image_built": self.built, "image_removed": image_removed,
                "networks": [], "volumes": []}


def setup_script(agent_mod, dropin: bool) -> str:
    secrets, managed = agent_mod.SECRETS, agent_mod.CLAUDE_MANAGED
    dropin_dest = f"{agent_mod.CLAUDE_MANAGED_DIR}/managed-settings.d/50-holdout.json"
    return (f"set -e; mkdir -p {secrets}/claude {agent_mod.LOGS} {WORKDIR} {agent_mod.CLAUDE_MANAGED_DIR}; "
            f"cp /proof/agent.env {agent_mod.AGENT_ENV}; chmod 700 {secrets}; chmod 600 {agent_mod.AGENT_ENV}; "
            f"cp /proof/managed-settings.json {managed}; chown 0:0 {managed}; chmod 644 {managed}; "
            + (f"cp /proof/dropin.json {dropin_dest}; chmod 644 {dropin_dest}; " if dropin else "")
            + f"sha256sum {dropin_dest} {managed}; "
            'export PATH="$HOME/.local/bin:$PATH"; claude --version; id -u')


def stage_case(stage: Path, files: dict[str, str], replay: dict | None, dropin: bytes | None) -> None:
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    for name, text in files.items():
        (stage / name).write_text(text)
    shutil.copy2(RUN_DIR / "cli_fake_anthropic.py", stage / "cli_fake_anthropic.py")
    if replay is not None:
        write_json(stage / "replay.json", {k: replay[k] for k in ("status", "headers", "body")})
    if dropin is not None:
        (stage / "dropin.json").write_bytes(dropin)


def open_case(dk: Docker, agent_mod, case: str, stage: Path, mode: str, dropin: bool) -> tuple[str, dict]:
    name = dk.start(case)
    if dk.sh(name, "mkdir -p /proof", 30).returncode != 0:
        raise Obstacle(f"{case}: mkdir /proof failed")
    if dk.call(["cp", f"{stage}/.", f"{name}:/proof"], 60).returncode != 0:
        raise Obstacle(f"{case}: docker cp into the container failed")
    setup = dk.sh(name, setup_script(agent_mod, dropin), 60)
    if setup.returncode != 0:
        raise Obstacle(f"{case}: setup failed: {setup.stderr.strip()[-300:]}")
    replay = " --replay /proof/replay.json" if mode == "budget" else ""
    started = dk.call(["exec", "-d", name, "python3", "/proof/cli_fake_anthropic.py", "--mode", mode,
                       "--port", str(PORT), "--log", "/proof/fake.jsonl", *replay.split()], 30)
    health = dk.sh(name, "for i in $(seq 1 40); do python3 -c \"import urllib.request as u; "
                   f"u.urlopen('http://127.0.0.1:{PORT}/healthz', timeout=2)\" 2>/dev/null && exit 0; "
                   "sleep 0.25; done; exit 1", 30)
    if started.returncode != 0 or health.returncode != 0:
        raise Obstacle(f"{case}: fake endpoint did not start")
    lines = setup.stdout.strip().splitlines()
    return name, {"setup": lines[-6:], "cli_version": next((x for x in lines if "Claude Code" in x), None)}


def launch(dk: Docker, agent_mod, name: str, seat_sec: int, session: int, resume: bool) -> dict:
    command = harness_command(agent_mod, seat_sec, session, resume)
    want = seat_sec + 2 * agent_mod.KILL_GRACE_SEC + 15
    dk.deadline.cap(want, want)
    t0 = time.monotonic()
    try:
        result = dk.sh(name, command, want, env={"MULTI_AGENT_HARNESS_PROMPT": "Reply with the word ok."})
        rc, host_timeout = result.returncode, False
    except Obstacle:
        rc, host_timeout = None, True
    wall = round(time.monotonic() - t0, 1)
    procs = dk.sh(name, "ps -eo pid,etimes,args | grep -v grep | grep -E 'claude|timeout' || true", 20)
    return {"session": session, "resume": resume, "seat_timeout_sec": seat_sec, "rc": rc, "wall_sec": wall,
            "host_timeout": host_timeout, "command_sha256": sha256(command.encode()),
            "lingering": procs.stdout.strip().splitlines()[:10]}


def collect(dk: Docker, agent_mod, name: str, dest: Path) -> None:
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    dk.call(["cp", f"{name}:{agent_mod.LOGS}", str(dest / "logs")], 60)
    dk.call(["cp", f"{name}:/proof/fake.jsonl", str(dest / "fake.jsonl")], 30)


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(errors="replace").splitlines()[:5000]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def stream_tool_results(rows: list[dict]) -> list[dict]:
    from cli_fake_anthropic import WEB_FETCH_ID, WEB_SEARCH_ID

    results = []
    for row in rows:
        message = row.get("message")
        if row.get("type") != "user" or not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content[:64]:
            if not isinstance(part, dict) or part.get("type") != "tool_result":
                continue
            tool_id = part.get("tool_use_id")
            if tool_id in (WEB_FETCH_ID, WEB_SEARCH_ID):
                results.append({"tool_use_id": tool_id, "is_error": part.get("is_error") is True,
                                "text": str(part.get("content"))[:300]})
    return results


def stream_summary(path: Path) -> dict:
    rows = read_jsonl(path)
    kinds: dict[str, int] = {}
    for row in rows:
        key = f"{row.get('type')}/{row.get('subtype')}"
        kinds[key] = kinds.get(key, 0) + 1
    init = next((r for r in rows if r.get("type") == "system" and r.get("subtype") == "init"), {})
    result = next((r for r in reversed(rows) if r.get("type") == "result"), {})
    retries = [{k: r.get(k) for k in ("attempt", "max_retries", "retry_delay_ms", "error_status", "error")}
               for r in rows if r.get("subtype") == "api_retry"][:20]
    return {"present": path.is_file(), "events": kinds, "version": init.get("claude_code_version"),
            "tools": init.get("tools"), "session_id_seen": bool(init.get("session_id")),
            "result": {k: result.get(k) for k in ("subtype", "is_error", "num_turns", "duration_ms",
                                                  "duration_api_ms", "api_error_status")},
            "result_text": str(result.get("result") or "")[:300], "api_retries": retries,
            "tool_results": stream_tool_results(rows)}


def fake_summary(path: Path) -> dict:
    rows = [r for r in read_jsonl(path) if r.get("method") == "POST"]
    messages = [r for r in rows if str(r.get("path", "")).split("?")[0].endswith("/v1/messages")]
    return {"posts": len(rows), "messages": len(messages),
            "times": [r.get("t") for r in messages][:40],
            "retry_counts": [r.get("retry_count") for r in messages][:40],
            "replies": [r.get("reply") for r in messages][:40],
            "tool_lists": [r.get("tools") for r in messages][:8],
            "tool_results": [x for r in messages for x in r.get("tool_results") or []][:10],
            "web_search_api_calls": sum(any(str(t).startswith("web_search") for t in r.get("tools") or [])
                                        for r in messages)}


def posts(dk: Docker, name: str) -> int | None:
    got = dk.sh(name, "grep -c '\"POST\"' /proof/fake.jsonl || true", 20).stdout.strip()
    return int(got) if got.isdigit() else None


def case_o1(dk: Docker, agent_mod, files: dict, proxy: dict, scratch: Path, evidence: Path) -> dict:
    stage = scratch / "stage-o1"
    stage_case(stage, files, proxy["team_cap"], None)
    name, opened = open_case(dk, agent_mod, "o1", stage, "budget", False)
    fresh = launch(dk, agent_mod, name, O1_FRESH_SEC, 1, False)
    fresh["posts_at_exit"] = posts(dk, name)
    time.sleep(5)
    fresh["posts_after_5s"] = posts(dk, name)
    has_sid = dk.sh(name, f"test -s {agent_mod.LOGS}/session-id && echo yes || echo no", 20).stdout.strip()
    resumed = None
    if has_sid == "yes":
        resumed = launch(dk, agent_mod, name, O1_RESUME_SEC, 2, True)
        resumed["posts_at_exit"] = posts(dk, name)
    collect(dk, agent_mod, name, evidence / "o1")
    dk.remove(name)
    logs = evidence / "o1/logs"
    return {**opened, "fresh": fresh, "resume": resumed, "session_id_file": has_sid == "yes",
            "stream_fresh": stream_summary(logs / "claude-code.txt"),
            "stream_resume": stream_summary(logs / "claude-code.2.txt"),
            "fake": fake_summary(evidence / "o1/fake.jsonl")}


def case_o6(dk: Docker, agent_mod, files: dict, scratch: Path, evidence: Path, case: str,
            dropin: bytes | None) -> dict:
    stage = scratch / f"stage-{case}"
    stage_case(stage, files, None, dropin)
    name, opened = open_case(dk, agent_mod, case, stage, "tools", dropin is not None)
    result = launch(dk, agent_mod, name, O6_SEC, 1, False)
    collect(dk, agent_mod, name, evidence / case)
    dk.remove(name)
    return {**opened, "run": result, "managed_dropin": "B pinned" if dropin else "prepared build",
            "stream": stream_summary(evidence / case / "logs/claude-code.txt"),
            "fake": fake_summary(evidence / case / "fake.jsonl")}


def web_tools(case: dict) -> dict:
    if str(RUN_DIR) not in sys.path:
        sys.path.insert(0, str(RUN_DIR))
    from cli_fake_anthropic import WEB_FETCH_ID, WEB_SEARCH_ID

    offered = set(case["stream"].get("tools") or [])
    offered.update(t for tools in case["fake"]["tool_lists"] for t in tools or [])
    results = {r.get("tool_use_id"): r for r in case["stream"].get("tool_results", [])}
    state = {}
    for tool, tool_id in zip(WEB_TOOLS, (WEB_SEARCH_ID, WEB_FETCH_ID), strict=True):
        state[tool] = {"offered": tool in offered, "result": results.get(tool_id)}
    state["WebSearch"]["api_calls"] = case["fake"]["web_search_api_calls"]
    state["reached"] = bool(case["fake"]["tool_lists"]) or case["stream"].get("tools") is not None
    return state


def o6_verdict(cases: dict, obstacles: dict) -> tuple[str, str]:
    deny = cases.get("o6-deny")
    if deny is None:
        return "not settled", f"O6: {obstacles.get('O6', 'deny case did not run')}"
    got = web_tools(deny)
    control = web_tools(cases["o6-pinned"]) if "o6-pinned" in cases else None
    note = (f"pinned-settings control offered WebSearch={control['WebSearch']['offered']}, "
            f"WebFetch={control['WebFetch']['offered']}" if control
            else f"pinned control missing: {obstacles.get('O6-pinned', 'not run')}")
    version = deny["stream"].get("version") or deny.get("cli_version")
    if not got["reached"]:
        return "not settled", (f"O6: Claude Code {version} never sent a request with tools to the fake "
                               f"endpoint (rc={deny['run']['rc']}); see cli/o6-deny")
    leaked = [t for t in WEB_TOOLS if got[t]["offered"]] + (["WebSearch api"] if got["WebSearch"]["api_calls"]
                                                            else [])
    texts = "; ".join(f"{t}: {str((got[t]['result'] or {}).get('text'))[:120]}" for t in WEB_TOOLS)
    if leaked:
        return "fail", f"O6: under the changed managed settings Claude Code {version} still offered {leaked}; {note}"
    not_refused = []
    for tool in WEB_TOOLS:
        result = got[tool]["result"] or {}
        if result.get("is_error") is not True or f"{tool} is disabled for this session" not in result.get("text", ""):
            not_refused.append(tool)
    if not_refused:
        return "not settled", (f"O6: Claude Code {version} did not offer the tools, but its stream "
                               f"lacks explicit refusal results for {not_refused}; {note}")
    return "pass", (f"O6: Claude Code {version} with the changed managed settings did not offer WebSearch or "
                    f"WebFetch and made 0 web_search API calls; canned calls answered: {texts}; {note}")


def o1_verdict(cases: dict, obstacles: dict, sim: dict) -> tuple[str, str]:
    case = cases.get("o1")
    if case is None:
        return "not settled", f"O1: {obstacles.get('O1', 'did not run')}"
    fresh, fake = case["fresh"], case["fake"]
    version = case["stream_fresh"].get("version") or case.get("cli_version")
    if fake["messages"] == 0:
        return "not settled", (f"O1: Claude Code {version} sent no request to the fake endpoint "
                               f"(rc={fresh['rc']}); see cli/o1")
    resume = case["resume"] or {}
    facts = (f"Claude Code {version} got 429 budget_exceeded (x-should-retry: false) from the replayed proxy "
             f"reply; fresh session rc={fresh['rc']} after {fresh['wall_sec']} s with {fresh['posts_at_exit']} "
             f"POSTs ({fresh['posts_after_5s']} 5 s later; retry headers {fake['retry_counts'][:12]}); "
             f"resumed session rc={resume.get('rc')} after {resume.get('wall_sec')} s; "
             f"keepalive: {sim.get('line', 'not simulated')}")
    if fresh["host_timeout"] or fresh["rc"] in SENT_STATUSES:
        return "fail", f"O1: the CLI did not end by itself within {fresh['seat_timeout_sec']} s. {facts}"
    return "pass", f"O1: {facts}"


class SimTeam:
    def __init__(self, caps: dict, exit_code: int, session_sec: float, spend: tuple[float, float]) -> None:
        assert caps and session_sec > 0 and len(spend) == 2
        self.agents = sorted(caps)
        self.caps = caps
        self.exit_code = exit_code
        self.session_sec = session_sec
        self.spend = spend
        self.timeout_sec = HORIZON_SEC
        self._env = None
        self.now = 0.0
        self.ends: dict[str, tuple[float, int | None]] = {}
        self.exits: dict[str, int | None] = {}
        self.sessions = dict.fromkeys(self.agents, 0)
        self.freezes: list[float] = []

    @property
    def running(self) -> set[str]:
        return set(self.ends)

    def facing(self, slot: str) -> str:
        return slot

    def ordered(self, names, slot: str | None = None) -> list[str]:
        return sorted(names)

    def reap(self) -> None:
        for name, (end, code) in list(self.ends.items()):
            if end <= self.now:
                self.exits[name] = code
                del self.ends[name]

    def launch(self, name: str, prompt: str, resume: bool = False, fallback: str | None = None) -> None:
        assert name in self.sessions and name not in self.ends
        self.sessions[name] += 1
        cap = self.caps[name]
        if cap is None:
            self.ends[name] = (float("inf"), None)
            return
        self.ends[name] = (max(self.now, cap) + self.session_sec, self.exit_code)

    async def _hub_admin(self, env, service: str, method: str, url: str) -> dict:
        assert service == "proxy" and method == "GET"
        return {"total": {"usd": self.spend[1] if self.now >= CAP_AT_SEC else self.spend[0]}, "budget_usd": 1.0}

    async def board_admin(self, method: str, path: str, body: dict | None = None) -> dict:
        if path.endswith("/freeze"):
            self.freezes.append(self.now)
            return {"grade": None}
        if path.endswith("/dead"):
            return {"dead": []}
        if path.endswith("/unread"):
            return {"unread": {}}
        raise RuntimeError(f"404 {path}")


SCENARIOS = {
    "team_cap_at_start": ({"a1": 0.0, "a2": 0.0}, (1.0, 1.0)),
    "team_cap_at_600s": ({"a1": CAP_AT_SEC, "a2": CAP_AT_SEC}, (0.3, 1.0)),
    "one_seat_cap_at_600s": ({"a1": CAP_AT_SEC, "a2": None}, (0.3, 0.6)),
}


async def simulate(cls, agent_mod, scenario: str, exit_code: int, session_sec: float) -> dict:
    caps, spend = SCENARIOS[scenario]
    team = SimTeam(caps, exit_code, session_sec, spend)
    keep = cls()
    keep.clock = lambda: team.now
    await keep.start(team)
    for name in team.agents:
        team.launch(name, "start")
    deadline = HORIZON_SEC + 3 * agent_mod.KILL_GRACE_SEC
    reason = None
    for _ in range(int(deadline / agent_mod.TICK_SEC) + 8):
        team.reap()
        reason = keep.end_reason(team)
        if reason:
            break
        if team.now >= deadline:
            reason = "timeout"
            break
        await keep.tick(team)
        team.now += agent_mod.TICK_SEC
    assert reason is not None
    timeline = keep.timeline
    return {"scenario": scenario, "exit_code": exit_code, "session_sec": session_sec, "end_reason": reason,
            "end_t": team.now, "freeze": [e for e in timeline if e.get("op") == "freeze"],
            "sessions": team.sessions, "retries": sum(e.get("kind") == "retry" for e in timeline),
            "timeline": timeline[:40]}


def load_keepalive(path: Path, tag: str):
    assert path.is_file(), path
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location(f"fable_cli_keepalive_{tag}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    found = [v for v in vars(module).values()
             if isinstance(v, type) and v.__module__ == module.__name__ and hasattr(v, "tick")]
    assert len(found) == 1, found
    return found[0]


def keepalive_sim(b_root: Path, build_dir: Path, o1: dict | None) -> dict:
    agent_mod = b_agent_module(b_root)
    rc = (o1 or {}).get("fresh", {}).get("rc")
    observed = rc is not None and not o1["fresh"]["host_timeout"]
    runs = [(rc, max(1.0, float(o1["fresh"]["wall_sec"])))] if observed else [(0, 5.0), (1, 5.0)]
    modules = {"pinned": b_root / PINNED_KEEPALIVE, "fixed_horizon": build_dir / FIXED_KEEPALIVE}
    out: dict = {"basis": "observed exit code" if observed else "conditional: CLI exit not observed",
                 "sha256": {k: sha256(p.read_bytes()) for k, p in modules.items() if p.is_file()}, "runs": []}
    for tag, path in modules.items():
        if not path.is_file():
            out["runs"].append({"module": tag, "error": f"missing {path}"})
            continue
        try:
            cls = load_keepalive(path, tag)
            for scenario in SCENARIOS:
                for code, sec in runs:
                    got = asyncio.run(simulate(cls, agent_mod, scenario, code, sec))
                    out["runs"].append({"module": tag, **got})
        except Exception as e:
            out["runs"].append({"module": tag, "error": f"{type(e).__name__}: {e}"})
    out["line"] = "; ".join(sim_line(r) for r in out["runs"])
    return out


def sim_line(row: dict) -> str:
    if "error" in row:
        return f"{row['module']}: {row['error']}"
    freeze = row["freeze"][0] if row["freeze"] else {}
    return (f"{row['module']} {row['scenario']} exit={row['exit_code']}: {row['end_reason']} at "
            f"{row['end_t']:.0f} s, freeze {freeze.get('why')} at {freeze.get('t')} s, "
            f"retries {row['retries']}, sessions {sum(row['sessions'].values())}")


def prepare(dk: Docker, b_root: Path, build_dir: Path, scratch: Path, record: dict):
    agent_mod = b_agent_module(b_root)
    files = harness_files(agent_mod)
    record["proxy"] = record_proxy_429(b_root, scratch)
    ctx, info = build_context(build_dir, scratch)
    record["context"] = info
    record["harbor_install_line_found"] = harbor_install_matches(b_root)
    record["install_note"] = (f"pinned {CC_VERSION} (pilot trajectories); the harness installs Harbor's "
                              "unpinned latest at trial time unless the roster sets cli_version")
    record["preflight"] = dk.preflight()
    record["image"] = dk.ensure_image(ctx, info)
    return agent_mod, files


def execute(dk: Docker, wanted: list[str], paths: dict, record: dict, cases: dict, obstacles: dict) -> None:
    b_root, build_dir, scratch, evidence = paths["b"], paths["build"], paths["scratch"], paths["evidence"]
    try:
        agent_mod, files = prepare(dk, b_root, build_dir, scratch, record)
    except Obstacle as e:
        obstacles.update(dict.fromkeys(wanted, str(e)))
        return
    if "O1" in wanted:
        try:
            cases["o1"] = case_o1(dk, agent_mod, files, record["proxy"], scratch, evidence)
        except Obstacle as e:
            obstacles["O1"] = str(e)
    if "O6" not in wanted:
        return
    try:
        cases["o6-deny"] = case_o6(dk, agent_mod, files, scratch, evidence, "o6-deny", None)
    except Obstacle as e:
        obstacles["O6"] = str(e)
        return
    try:
        pinned = (b_root / PINNED_MANAGED).read_bytes()
        cases["o6-pinned"] = case_o6(dk, agent_mod, files, scratch, evidence, "o6-pinned", pinned)
    except Obstacle as e:
        obstacles["O6-pinned"] = str(e)


def selected(checks: set[str]) -> list[str]:
    names = {c.strip().upper() for c in checks if c.strip()}
    if not names or "ALL" in names:
        return list(CHECKS)
    return [c for c in CHECKS if c in names]


def run(output: Path, checks: set[str], *, build_dir: Path = BUILD_DIR, b_root: Path = B_ROOT,
        scratch: Path = SCRATCH, keep_image: bool = False) -> dict[str, tuple[str, str]]:
    wanted = selected(checks)
    if not wanted:
        return {}
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    evidence = output / "cli"
    evidence.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    run_id = f"{int(time.time())}{os.getpid()}"
    dk = Docker(run_id, Deadline(DEADLINE_SEC), keep_image)
    record: dict = {"run_id": run_id, "checks": wanted, "cc_version": CC_VERSION, "model": MODEL,
                    "network": "none at run time", "build_dir": str(build_dir), "b_root": str(b_root)}
    cases: dict = {}
    obstacles: dict = {}
    paths = {"b": b_root, "build": build_dir, "scratch": scratch, "evidence": evidence}
    try:
        execute(dk, wanted, paths, record, cases, obstacles)
    finally:
        try:
            record["docker_cleanup"] = dk.cleanup()
        except Obstacle as e:
            record["docker_cleanup"] = {"error": str(e), "containers": dk.containers}
        record["docker_calls"] = dk.log
    sim: dict = {}
    if "O1" in wanted:
        try:
            sim = keepalive_sim(b_root, build_dir, cases.get("o1"))
        except (Obstacle, AssertionError, OSError) as e:
            sim = {"line": f"simulation failed: {type(e).__name__}: {e}"}
    record.update({"cases": cases, "obstacles": obstacles, "keepalive": sim})
    verdicts = {}
    if "O1" in wanted:
        verdicts["O1"] = o1_verdict(cases, obstacles, sim)
    if "O6" in wanted:
        verdicts["O6"] = o6_verdict(cases, obstacles)
    record["verdicts"] = verdicts
    write_json(evidence / "cli-proof.json", record)
    assert set(verdicts) == set(wanted)
    return verdicts


def main() -> None:
    parser = argparse.ArgumentParser(description="O1 and O6: real Claude Code against a fake endpoint")
    parser.add_argument("--proofs-dir", type=Path, default=PROOFS_DIR)
    parser.add_argument("--check", default="all")
    parser.add_argument("--build-dir", type=Path, default=BUILD_DIR)
    parser.add_argument("--b-root", type=Path, default=B_ROOT)
    parser.add_argument("--scratch", type=Path, default=SCRATCH)
    parser.add_argument("--keep-image", action="store_true")
    args = parser.parse_args()
    got = run(args.proofs_dir, set(args.check.split(",")), build_dir=args.build_dir, b_root=args.b_root,
              scratch=args.scratch, keep_image=args.keep_image)
    print(json.dumps({k: {"verdict": v, "reason": r} for k, (v, r) in got.items()}, indent=1))


if __name__ == "__main__":
    main()
