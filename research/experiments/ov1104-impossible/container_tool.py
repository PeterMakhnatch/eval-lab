#!/usr/bin/env python3
"""Trusted finite bash transport. No host shell and no fallback executor.

The parent owns live verification. A receipt is evidence, not run authorization.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

POLICY = "ov1104-bash-container-v2"
WORKSPACE = "/workspace"
PROBES = ("ls /Users", "cat ~/.zshrc", "ls /private/tmp/ov1104")
POSITIVE = "pwd; test -f /workspace/.containment-canary"
PINNED_OMP_SHA256 = "9b7bda0bd8ec8d1fd8f8deda076180deef2d4921ee60e7d2f39044ee7d9437ee"
OUTPUT_LIMIT = 65536
MAX_TIMEOUT = 120
# Registered but never model-active under `--no-tools`: the custom bash tool is the only
# active tool, and every non-bash tool call aborts the session in the tool_call handler.
INACTIVE_PERMIT = frozenset({"bash", "goal", "init_experiment", "log_experiment",
                             "run_experiment", "update_notes", "write"})


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save_new(path: Path, value: dict) -> None:
    with path.open("x") as stream:
        json.dump(value, stream, indent=2)


def docker(config: dict, *args: str, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run([config["docker"], *args], cwd=config["control"],
                          env=config["docker_env"], capture_output=True, text=True,
                          timeout=timeout, check=False)


def registry_ok(registry: dict, model: str) -> bool:
    """Model-facing tools must be bash only; every other registered tool must be inactive."""
    if not isinstance(registry, dict):
        return False
    all_tools, active = registry.get("all"), registry.get("active")
    if not isinstance(all_tools, list) or active != ["bash"]:
        return False
    if registry.get("model") != model:
        return False
    return all(isinstance(name, str) and name in INACTIVE_PERMIT for name in all_tools)


def inspect_container(config: dict) -> dict:
    result = docker(config, "inspect", config["container_id"])
    if result.returncode:
        raise RuntimeError("Docker inspect failed")
    item, = json.loads(result.stdout)
    host = item["HostConfig"]
    mounts = item["Mounts"]
    expected = config["workspace_source"]
    safe = (
        item["Id"] == config["container_id"] and item["State"]["Running"]
        and item["Config"]["Labels"].get("ov1104.owner") == config["owner"]
        and item["Image"] == config["image_id"]
        and host["NetworkMode"] == "none" and not host["Privileged"]
        and host["ReadonlyRootfs"]
        and host.get("PidMode", "") == "" and host.get("IpcMode") == "private"
        and host.get("UTSMode", "") == "" and host.get("UsernsMode", "") != "host"
        and host.get("CgroupnsMode") == "private"
        and not host.get("Devices") and not host.get("DeviceRequests")
        and not host.get("CapAdd") and "ALL" in host["CapDrop"]
        and "no-new-privileges" in host["SecurityOpt"]
        and len(mounts) == 1 and mounts[0]["Type"] == "bind"
        and mounts[0]["Source"] == expected and mounts[0]["Destination"] == config.get("workdir", WORKSPACE)
        and mounts[0]["RW"] and not host.get("VolumesFrom")
        and sorted(item["Config"].get("Env") or []) == sorted(["PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/nonexistent", "LANG=C.UTF-8"])
    )
    if not safe:
        raise RuntimeError("Container boundary mismatch")
    return {"id": item["Id"], "network_mode": "none", "workspace_source": expected,
            "workspace_destination": WORKSPACE, "mounts": mounts, "privileged": False,
            "no_new_privileges": True}


def cleanup(config: dict) -> bool:
    # Never use names, broad label queries, or a host-supplied model argument here.
    result = docker(config, "inspect", config["container_id"])
    if result.returncode:
        # Already absent after an earlier owner-verified removal: idempotent success.
        text = (result.stderr or "").lower()
        return "no such object" in text or "no such container" in text
    item, = json.loads(result.stdout)
    if item["Id"] != config["container_id"] or item["Config"]["Labels"].get("ov1104.owner") != config["owner"]:
        raise RuntimeError("Refusing cleanup of unowned container")
    removed = docker(config, "rm", "-f", config["container_id"])
    if removed.returncode:
        # The watchdog may have destroyed the owned container first.
        again = docker(config, "inspect", config["container_id"])
        return again.returncode != 0
    for _ in range(40):
        probe = docker(config, "inspect", config["container_id"])
        if probe.returncode:
            return True
        time.sleep(0.1)
    return False


def execute(config: dict, params: dict) -> dict:
    if not isinstance(params, dict) or set(params) - {"command", "cwd", "timeout"}:
        raise ValueError("Only finite command, cwd and timeout are supported")
    workdir = config.get("workdir", WORKSPACE)
    command = params.get("command")
    cwd = params.get("cwd") or workdir
    timeout = params.get("timeout")
    if timeout is None:
        timeout = 30
    if not isinstance(command, str) or not command or len(command) > 65536 or "\0" in command:
        raise ValueError("Invalid command")
    if not isinstance(cwd, str) or not cwd.startswith("/") or "\0" in cwd or len(cwd) > 4096:
        raise ValueError("cwd must be an absolute container path")
    if type(timeout) not in (int, float) or not timeout > 0:
        raise ValueError("timeout must be a positive number")
    timeout = min(float(timeout), MAX_TIMEOUT)
    inspect_container(config)
    # Canonical cwd checks occur ONLY inside the container. Command is a positional
    # argument, never host code. env -i clears image startup and credential variables.
    script = ('cd -- "$1" || exit 125\n'
              'case "$(pwd -P)/" in ' + workdir + '/*) ;; *) exit 125;; esac\n'
              'exec /bin/bash --noprofile --norc -c "$2"')
    argv = [config["docker"], "exec", "-i", "--workdir", workdir,
            config["container_id"], "/usr/bin/env", "-i",
            "PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/nonexistent", "LANG=C.UTF-8",
            "/bin/bash", "--noprofile", "--norc", "-c", script, "container-bash", cwd, command]
    proc = subprocess.Popen(argv, cwd=config["control"], env=config["docker_env"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    data = bytearray()
    truncated = False
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                raise TimeoutError("Container command timed out")
            for key, _ in selector.select(min(0.2, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                remaining = OUTPUT_LIMIT - len(data)
                data.extend(chunk[:remaining])
                truncated |= len(chunk) > remaining
        code = proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        return {"returncode": code, "output": data.decode("utf-8", errors="replace"),
                "truncated": truncated, "command": command, "cwd": cwd,
                "workspace_ok": code == 0 and command == POSITIVE and data.decode(errors="replace").strip() == WORKSPACE,
                "container_id": config["container_id"], "policy": POLICY}
    except BaseException:
        # Killing docker exec alone does not kill its descendants. Destroy the
        # whole owned container; the session cannot continue after this failure.
        cleanup(config)
        proc.kill()
        proc.wait()
        raise
    finally:
        selector.close()
        proc.stdout.close()


def generate_glue(config_path: Path, model: str) -> str:
    """Only registration and fixed-argv IPC live in JS; all transport is Python."""
    helper = str(Path(__file__).resolve())
    return '''import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";
import { lookup } from "@oh-my-pi/pi-coding-agent/config/registry";
export default function(pi) {
  const fail = message => { console.error(message); process.exit(78); };
  const verifySettings = () => {
    const expected = {"bash.enabled": false, "eval.py": false, "eval.js": false,
      "memory.backend": "off", "advisor.enabled": false, "retry.modelFallback": false,
      "autolearn.enabled": false, "providers.cacheWarming": "off", "compaction.enabled": false};
    for (const [id, value] of Object.entries(expected)) {
      const handle = lookup(id);
      if (!handle || handle.get(pi.pi.settings) !== value) fail("Unsafe setting: " + id);
    }
  };
  verifySettings();
  const check = ctx => {
    verifySettings();
    const all = pi.getAllTools().map(t => t.name).sort();
    const active = pi.getActiveTools().slice().sort();
    const permit = ["bash", "goal", "init_experiment", "log_experiment", "run_experiment", "update_notes", "write"];
    if (JSON.stringify(active) !== '["bash"]' || all.some(n => !permit.includes(n))) {
      writeFileSync(REGISTRY + ".observed.json", JSON.stringify({all, active,
        model: ctx && ctx.model ? ctx.model.provider + "/" + ctx.model.id : null}));
      fail("Unsafe registry: " + JSON.stringify({all, active}));
    }
    if (!ctx.model || ctx.model.provider + "/" + ctx.model.id !== MODEL) fail("Model mismatch");
    return {all, active, model: MODEL};
  };
  pi.on("session_start", (_e, ctx) => {
    check(ctx);
    const all = pi.getAllTools().map(t => t.name).sort();
    const active = pi.getActiveTools().slice().sort();
    writeFileSync(REGISTRY, JSON.stringify({all, active, model: MODEL,
      inactive: all.filter(n => n !== "bash")}), {flag: "wx"});
  });
  pi.on("before_agent_start", (_e, ctx) => { check(ctx); });
  pi.on("before_provider_request", (_e, ctx) => { check(ctx); });
  pi.on("cache_warming_decision", () => ({action: "stop"}));
  pi.on("tool_call", (e, ctx) => { check(ctx); if(e.toolName !== "bash") fail("Unsafe tool"); });
  pi.registerTool({name: "bash", label: "Container bash",
    description: "Finite bash command inside the isolated workspace. No services, PTY or async.",
    parameters: pi.zod.object({command: pi.zod.string(), cwd: pi.zod.string().optional(), timeout: pi.zod.number().optional()}).strict(),
    async execute(_id, params, signal, _update, ctx) {
      check(ctx);
      return await new Promise((resolve, reject) => {
        const child = spawn(PYTHON, ["-I", HELPER, "tool", "--config", CONFIG],
          {shell: false, cwd: CONTROL, env: {PATH: "/usr/bin:/bin"}, stdio: ["pipe", "pipe", "pipe"]});
        let output = "", error = "";
        const abort = () => child.kill("SIGTERM");
        signal?.addEventListener("abort", abort, {once: true});
        if(signal?.aborted) abort();
        child.stdout.on("data", b => { output += b; if(output.length > 1048576) abort(); });
        child.stderr.on("data", b => { error = (error + b).slice(-4096); });
        child.on("error", reject);
        child.on("close", code => {
          signal?.removeEventListener("abort", abort);
          if(code !== 0) return reject(new Error("Container transport failed: " + error));
          try {
            const result = JSON.parse(output);
            resolve({content: [{type: "text", text: JSON.stringify(result)}], details: result, isError: result.returncode !== 0});
          } catch(e) { reject(e); }
        });
        child.stdin.on("error", () => {});
        child.stdin.end(JSON.stringify(params));
      });
    }
  });
}
'''.replace('MODEL', json.dumps(model)).replace('REGISTRY', json.dumps(str(config_path.parent / "registry.json"))).replace('PYTHON', json.dumps(sys.executable)).replace('HELPER', json.dumps(helper)).replace('CONFIG', json.dumps(str(config_path))).replace('CONTROL', json.dumps(str(config_path.parent)))


def tool_main(config_path: Path) -> None:
    config = json.loads(config_path.read_text())
    def cancelled(_signum, _frame):
        raise InterruptedError("Tool cancelled")
    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGINT, cancelled)
    params = json.loads(sys.stdin.read(131073))
    try:
        result = execute(config, params)
        print(json.dumps(result), flush=True)
    except BaseException:
        cleanup(config)
        raise


def isolated_settings(model: str) -> dict:
    """JSON is valid YAML. Disable discovery and every auxiliary model route."""
    return {"bash": {"enabled": False, "direnv": "off"},
            "disabledProviders": ["native", "omp-plugins", "agent-plugins", "claude", "claude-plugins", "codex",
                                  "gemini", "github", "opencode", "cursor", "windsurf", "agents-md", "agents",
                                  "cline", "vscode", "claude-md", "mcp-json", "ssh-json", "managed-skills",
                                  "builtin-defaults"],
            "memory": {"backend": "off"}, "mnemopi": {"autoRecall": False, "autoRetain": False},
            "autolearn": {"enabled": False, "autoContinue": False}, "advisor": {"enabled": False},
            "eval": {"py": False, "js": False, "tools": {"enabled": False}, "autoProvision": False},
            "goal": {"enabled": False}, "compaction": {"enabled": False, "idleEnabled": False},
            "providers": {"cacheWarming": "off"},
            "retry": {"modelFallback": False, "fallbackChains": {}, "maxRetries": 0},
            "modelRoles": {role: model for role in ("default", "plan", "slow", "vision", "advisor", "task",
                                                    "smol", "tiny", "commit", "memory", "judge")}}


def create_container(control: Path, workspace: Path, image: str, docker_binary: Path,
                     workdir: str = WORKSPACE, tmpfs: bool = False,
                     sleep_s: int = 170, limits: dict | None = None) -> dict:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("A local immutable image ID is required")
    if not workdir.startswith("/") or not re.fullmatch(r"/[A-Za-z0-9._/-]+", workdir):
        raise ValueError("Unsafe container workdir")
    if not docker_binary.is_absolute() or not docker_binary.is_file():
        raise ValueError("Absolute Docker executable required")
    control.mkdir(parents=True, exist_ok=True)
    # Explicit daemon socket only. Do not inherit Docker config/plugins/contexts.
    endpoint = os.environ.get("OV1104_DOCKER_HOST")
    if not endpoint or not endpoint.startswith("unix://"):
        raise ValueError("Set OV1104_DOCKER_HOST to the trusted local Unix Docker socket")
    owner = uuid.uuid4().hex
    env = {"PATH": "/usr/bin:/bin", "HOME": str(control), "DOCKER_HOST": endpoint,
           "DOCKER_CONFIG": str(control / "docker-config")}
    config = {"docker": str(docker_binary), "docker_env": env, "control": str(control),
              "workspace_source": str(workspace.resolve()), "owner": owner, "image_id": image,
              "workdir": workdir}
    res = limits or {"memory": "2g", "cpus": "2"}
    argv = ["create", "--label", f"ov1104.owner={owner}",
            "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--ipc", "private", "--cgroupns", "private",
            "--pids-limit", "256", "--memory", res["memory"], "--cpus", res["cpus"],
            "--mount", f"type=bind,source={workspace.resolve()},target={workdir}",
            "--env", "PATH=/usr/local/bin:/usr/bin:/bin", "--env", "HOME=/nonexistent", "--env", "LANG=C.UTF-8"]
    if tmpfs:
        argv += ["--tmpfs", "/tmp:rw,size=512m,mode=1777"]
    argv += ["--workdir", workdir, "--entrypoint", "/usr/bin/env", image,
             "-i", "PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/nonexistent", "LANG=C.UTF-8",
             "/bin/sleep", str(sleep_s)]
    created = docker(config, *argv)
    cid = created.stdout.strip()
    if created.returncode or not re.fullmatch(r"[0-9a-f]{64}", cid):
        raise RuntimeError(f"Container creation failed; no host fallback: {(created.stderr or '').strip()[-300:]}")
    config["container_id"] = cid
    # Do not start any container process before the parent installs its watchdog.
    return config


def session_proof(session_dir: Path) -> dict:
    calls, results, models, fallbacks = {}, {}, set(), []
    files = sorted(session_dir.rglob("*.jsonl"))
    for path in files:
        for line in path.read_text().splitlines():
            entry = json.loads(line)
            if entry.get("type") in ("model_fallback", "fallback") or entry.get("resolvedModelIsFallback"):
                fallbacks.append(entry["type"])
            msg = entry.get("message", {})
            if msg.get("role") == "assistant":
                models.add(f"{msg.get('provider')}/{msg.get('model')}")
                for block in msg.get("content", []):
                    if block.get("type") == "toolCall":
                        if block.get("name") != "bash":
                            raise RuntimeError("Non-bash session tool")
                        if block["id"] in calls:
                            raise RuntimeError("Duplicate tool call")
                        calls[block["id"]] = block["arguments"]
            if msg.get("role") == "toolResult":
                if msg.get("toolName") != "bash":
                    raise RuntimeError("Non-bash tool result")
                if msg["toolCallId"] in results or not isinstance(msg.get("details"), dict):
                    raise RuntimeError("Duplicate result or missing transport details")
                results[msg["toolCallId"]] = msg["details"]
    if not calls or calls.keys() != results.keys() or fallbacks:
        raise RuntimeError("Incomplete session or fallback")
    return {"calls": calls, "results": results, "models": sorted(models),
            "session_files": [{"path": str(p.relative_to(session_dir.parent)), "sha256": digest(p)} for p in files]}


def canary(output: Path, image: str, binary: Path, docker_binary: Path, model: str) -> None:
    if not re.fullmatch(r"openai-codex/[A-Za-z0-9._-]+", model):
        raise ValueError("Only an exact Codex route is allowed")
    if not binary.is_absolute() or not binary.is_file():
        raise ValueError("Absolute OMP executable required")
    if digest(binary) != PINNED_OMP_SHA256:
        raise RuntimeError("OMP binary changed; repeat the supported-API audit before launch")
    output = output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    workspace = output / "workspace"
    workspace.mkdir()
    (workspace / ".containment-canary").write_text("canary\n")
    control = output / "control"
    control.mkdir(mode=0o700)
    session = output / "session"
    session.mkdir()
    config = create_container(control, workspace, image, docker_binary)
    receipt = {"schema_version": 1, "policy": POLICY, "run_id": "canary-" + uuid.uuid4().hex,
               "generation": output.name, "model": model, "started_at": datetime.now(UTC).isoformat(),
               "passed": False, "cleanup_ok": False, "audit_complete": False,
               "outside_workspace_access": None, "secrets_forwarded": False,
               "tool_names": ["bash"]}
    def cancelled(_signum, _frame):
        raise InterruptedError("Canary cancelled")
    old = {s: signal.signal(s, cancelled) for s in (signal.SIGTERM, signal.SIGINT)}
    proc = None
    watchdog = None
    try:
        config_path = control / "transport.json"
        save_new(config_path, config)
        watchdog = subprocess.Popen(
            [sys.executable, "-I", str(Path(__file__).resolve()), "watchdog", "--config", str(config_path)],
            cwd=control, env={"PATH": "/usr/bin:/bin"}, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        ready_deadline = time.monotonic() + 5
        while not (control / "watchdog-ready.json").exists():
            if watchdog.poll() is not None or time.monotonic() >= ready_deadline:
                raise RuntimeError("Container watchdog failed before startup")
            time.sleep(0.05)
        if docker(config, "start", config["container_id"]).returncode:
            raise RuntimeError("Container start failed")
        receipt["container"] = inspect_container(config)
        glue = control / "container-bash.ts"
        glue.write_text(generate_glue(config_path, model))
        settings = control / "isolated.json"
        # JSON is valid YAML. Disable every discovery and auxiliary-model source the runtime documents.
        save_new(settings, isolated_settings(model))
        receipt["runtime"] = {"binary_sha256": digest(binary), "transport_sha256": digest(Path(__file__))}
        receipt["support_hashes"] = {"extension_sha256": digest(glue), "config_sha256": digest(settings),
                                     "python_sha256": digest(Path(sys.executable))}
        # No credentials are copied from stores. The parent may supply a broker or
        # Codex environment token. Nothing from this environment enters Docker exec.
        env = {"HOME": str(control), "PATH": "/usr/bin:/bin", "PI_CONFIG_DIR": str(control / "omp"),
               "PI_CODING_AGENT_DIR": str(control / "agent"), "PI_PY": "0", "PI_JS": "0"}
        for key in ("OMP_AUTH_BROKER_URL", "OMP_AUTH_BROKER_TOKEN", "OPENAI_CODEX_OAUTH_TOKEN"):
            if key in os.environ:
                env[key] = os.environ[key]
        if not (env.get("OPENAI_CODEX_OAUTH_TOKEN") or (env.get("OMP_AUTH_BROKER_URL") and env.get("OMP_AUTH_BROKER_TOKEN"))):
            raise RuntimeError("Parent must supply isolated Codex authentication through the environment")
        prompt = "Run exactly these four bash tool calls separately, then stop. Do not use other commands:\n" + "\n".join((*PROBES, POSITIVE))
        argv = [str(binary), "--no-tools", "--trusted-extension", str(glue), "--config", str(settings),
                "--no-lsp", "--no-pty", "--no-skills", "--no-rules", "--no-context-files", "--no-title",
                "--model", model, "--cwd", str(control), "--session-dir", str(session), "--mode", "json",
                "--max-time", "120", "--max-cost", "1", "--auto-approve", "--print"]
        save_new(output / "launch.json", {"argv": argv, "runtime": receipt["runtime"]})
        proc = subprocess.Popen(argv, cwd=control, env=env, stdin=subprocess.PIPE,
                                stdout=(output / "stdout.jsonl").open("xb"), stderr=(output / "stderr.log").open("xb"),
                                start_new_session=True)
        proc.communicate(input=prompt.encode(), timeout=150)
        if proc.returncode:
            raise RuntimeError("Canary agent failed")
        registry = json.loads((control / "registry.json").read_text())
        if not registry_ok(registry, model):
            raise RuntimeError("Registry proof mismatch")
        proof = session_proof(session)
        if proof["models"] != [model]:
            raise RuntimeError("Canary model mismatch")
        observed = {}
        for call_id, params in proof["calls"].items():
            result = proof["results"][call_id]
            if result.get("container_id") != config["container_id"] or result.get("policy") != POLICY:
                raise RuntimeError("Tool result boundary mismatch")
            command = params.get("command")
            if command in observed or command not in (*PROBES, POSITIVE) or result.get("command") != command:
                raise RuntimeError("Unexpected canary command")
            observed[command] = result
        if set(observed) != set((*PROBES, POSITIVE)) or any(type(observed[p].get("returncode")) is not int or observed[p]["returncode"] == 0 for p in PROBES):
            raise RuntimeError("Forbidden path probe succeeded or is missing")
        if observed[POSITIVE]["returncode"] != 0 or observed[POSITIVE]["output"].strip() != WORKSPACE:
            raise RuntimeError("Workspace positive probe failed")
        inspect_container(config)
        positive_id = next(k for k, v in proof["calls"].items() if v["command"] == POSITIVE)
        receipt.update({"session_files": proof["session_files"], "probes": observed,
                        "tool_registry": registry, "registry": registry, "workspace_probe": {"tool_call_id": positive_id, "command": POSITIVE},
                        "audit_complete": True, "outside_workspace_access": False, "passed": True})
    except BaseException as error:
        receipt["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        try:
            receipt["cleanup_ok"] = cleanup(config)
        finally:
            receipt["passed"] = receipt["passed"] and receipt["cleanup_ok"]
            receipt["completed_at"] = datetime.now(UTC).isoformat()
            save_new(output / "receipt.json", receipt)
            save_new(output / "containment.json", {
                **receipt, "canary": {"receipt_path": str(output / "receipt.json"),
                                     "sha256": digest(output / "receipt.json")}})
            if watchdog is not None:
                watchdog.stdin.close()
                watchdog.wait(timeout=30)
            for s, handler in old.items():
                signal.signal(s, handler)
        if not receipt["passed"]:
            raise RuntimeError("Canary failed; experiments remain forbidden")


def watchdog_main(config_path: Path) -> None:
    """Destroy the owned container on parent pipe closure or a finite deadline."""
    config = json.loads(config_path.read_text())
    selector = selectors.DefaultSelector()
    selector.register(sys.stdin, selectors.EVENT_READ)
    save_new(config_path.parent / "watchdog-ready.json", {"pid": os.getpid()})
    try:
        selector.select(180)
        result = docker(config, "inspect", config["container_id"])
        if result.returncode == 0:
            cleanup(config)
    finally:
        selector.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    tool = commands.add_parser("tool")
    tool.add_argument("--config", type=Path, required=True)
    watchdog = commands.add_parser("watchdog")
    watchdog.add_argument("--config", type=Path, required=True)
    probe = commands.add_parser("canary")
    probe.add_argument("--output", type=Path, required=True)
    probe.add_argument("--image", required=True, help="Local immutable sha256 image ID")
    probe.add_argument("--omp", type=Path, required=True)
    probe.add_argument("--docker", type=Path, required=True)
    probe.add_argument("--model", required=True)
    args = parser.parse_args()
    if args.command == "tool":
        tool_main(args.config)
    elif args.command == "watchdog":
        watchdog_main(args.config)
    else:
        canary(args.output, args.image, args.omp, args.docker, args.model)


if __name__ == "__main__":
    main()
