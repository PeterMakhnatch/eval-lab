"""One genuine OpenCode proposal behind the existing physical-request broker.

This is an LM transport, not an optimization engine or Harbor task runner.
OpenCode receives only a short-lived capability. Its process is OS-contained;
all candidate text is data and no shell/file tools are enabled.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any

from evallab.execution_contracts import (
    ZAI_INPUT_COST_MICROS_PER_MILLION,
    ZAI_OUTPUT_COST_MICROS_PER_MILLION,
    ProxyTrialLimits,
    materialize_zai_secret_file,
    persist_private_bytes,
)
from evallab.runner import _read_proxy_usage

OPENCODE_VERSION = "1.18.9"
MODEL = "zai-coding-plan/glm-5.3-flash"
MAX_TRANSCRIPT_BYTES = 4 * 1024 * 1024
GIT_EXECUTABLE = Path("/Library/Developer/CommandLineTools/usr/bin/git")


class OpenCodeTransportError(RuntimeError):
    """Retained transport failure; callers must not automatically retry."""


def _private_json(path: Path, payload: Any) -> None:
    persist_private_bytes(
        path, (json.dumps(payload, indent=2, allow_nan=False) + "\n").encode(), secrets=()
    )


def sandbox_profile(executable: Path, workspace: Path, port: int) -> str:
    """Deny candidate execution, host data access and all non-broker egress."""
    if not 0 < port < 65536:
        raise ValueError("Invalid broker port")
    binary = json.dumps(str(executable.resolve()))
    work = json.dumps(str(workspace.resolve()))
    git = json.dumps(str(GIT_EXECUTABLE))
    stdout = json.dumps(str(workspace.parent / "events.jsonl"))
    stderr = json.dumps(str(workspace.parent / "client.log"))
    return f"""(version 1)
(deny default)
(allow process-fork process-info* sysctl-read mach-lookup)
(allow file-read-metadata)
(allow file-read-data file-map-executable
    (literal "/")
    (subpath "/System") (subpath "/usr/lib") (subpath "/usr/share")
    (subpath "/Library/Apple") (subpath "/private/var/db/timezone")
    (literal "/dev/null") (literal "/dev/urandom")
    (literal "/private/etc/localtime") (literal "/private/etc/hosts")
    (literal "/private/etc/resolv.conf") (literal {binary}) (literal {git}))
(allow file-read* file-write* (subpath {work}))
(allow file-write-data (literal {stdout}) (literal {stderr}) (literal "/dev/null"))
(allow process-exec (literal {binary}) (literal {git}))
(allow network-outbound (remote ip "localhost:{port}"))
"""


def opencode_config(endpoint: str, capability: str) -> dict[str, Any]:
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": MODEL,
        "small_model": MODEL,
        "enabled_providers": ["zai-coding-plan"],
        "share": "disabled",
        "autoupdate": False,
        "snapshot": False,
        "formatter": False,
        "lsp": False,
        "plugin": [],
        "mcp": {},
        "permission": {"*": "deny"},
        "compaction": {"auto": False, "prune": False},
        "provider": {
            "zai-coding-plan": {
                "options": {"baseURL": endpoint, "apiKey": capability},
                "models": {"glm-5.3-flash": {"name": "GLM-5.3 Flash"}},
            }
        },
        "agent": {
            "proposer": {
                "mode": "primary",
                "description": "Return a Python code proposal without executing it",
                "model": MODEL,
                "permission": {"*": "deny"},
                "tools": {"*": False},
                "steps": 1,
            },
            "title": {"disable": True},
            "summary": {"disable": True},
            "compaction": {"disable": True},
        },
    }


def parse_response(path: Path) -> str:
    """Accept a completed text-only turn, never partial output or tool execution."""
    if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
        raise OpenCodeTransportError("OpenCode transcript exceeded its bound")
    chunks: list[str] = []
    finishes = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if not isinstance(event, dict):
            raise OpenCodeTransportError("Invalid OpenCode event")
        kind = event.get("type")
        part = event.get("part", {})
        if kind in {"error", "tool_use"}:
            raise OpenCodeTransportError("OpenCode reported an error or attempted a tool call")
        if kind == "text":
            text = part.get("text") if isinstance(part, dict) else None
            if not isinstance(text, str):
                raise OpenCodeTransportError("OpenCode text event is invalid")
            chunks.append(text)
        elif kind == "step_finish":
            if not isinstance(part, dict) or part.get("reason") != "stop":
                raise OpenCodeTransportError("OpenCode response did not finish normally")
            finishes += 1
    response = "".join(chunks)
    if finishes != 1 or not response.strip():
        raise OpenCodeTransportError("OpenCode returned no complete single-turn proposal")
    return response


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


class OpenCodeTransport:
    def __init__(self, *, repo_root: Path, limits: ProxyTrialLimits, timeout_seconds: int = 180):
        if limits.max_requests != 1:
            raise ValueError("One proposal must permit exactly one physical upstream request")
        self.repo_root = repo_root.resolve()
        self.limits = limits
        self.timeout_seconds = timeout_seconds
        self.executable = Path(shutil.which("opencode") or "/nonexistent-opencode").resolve()

    def preflight(self) -> dict[str, Any]:
        if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            raise OpenCodeTransportError("OpenCode proposer requires qualified macOS Seatbelt")
        if not self.executable.is_file():
            raise OpenCodeTransportError("Installed OpenCode executable is unavailable")
        version = (
            subprocess.run(
                [str(self.executable), "--version"],
                capture_output=True,
                check=True,
                timeout=20,
                env={"PATH": "/usr/bin:/bin", "HOME": str(self.repo_root)},
            )
            .stdout.decode()
            .strip()
        )
        if version != OPENCODE_VERSION:
            raise OpenCodeTransportError(f"OpenCode proposer requires version {OPENCODE_VERSION}")
        with self.executable.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return {
            "transport": "opencode",
            "version": version,
            "executable_sha256": "sha256:" + digest,
            "model": MODEL,
            "limits": asdict(self.limits),
            "containment": "seatbelt_default_deny_exact_loopback_broker",
            "physical_request_cap": 1,
        }

    def request(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        directory: Path,
        offline_upstream: str | None = None,
    ) -> dict[str, Any]:
        """Run the real CLI; optional loopback upstream is a no-model protocol control."""
        facts = self.preflight()
        if any(p.is_symlink() for p in (directory, *directory.parents)):
            raise ValueError("Proposer directories must not follow symlinks")
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        workspace = directory / "client"
        workspace.mkdir(mode=0o700)
        for child in ("home", "data", "config", "cache", "state", "tmp"):
            (workspace / child).mkdir(mode=0o700)
        capability = os.urandom(32).hex()
        capability_id = "sha256:" + hashlib.sha256(capability.encode()).hexdigest()
        attempt_id = directory.name
        key_file = directory / "provider-key"
        if offline_upstream is None:
            upstream = "https://api.z.ai"
        else:
            from urllib.parse import urlsplit

            parsed = urlsplit(offline_upstream)
            if (
                parsed.scheme != "http"
                or parsed.hostname != "127.0.0.1"
                or parsed.username
                or parsed.password
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or not parsed.port
            ):
                raise ValueError("Offline protocol upstream must be an explicit loopback endpoint")
            upstream = offline_upstream
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        endpoint = f"http://127.0.0.1:{port}/api/paas/v4"
        config = opencode_config(endpoint, capability)
        _private_json(workspace / "config.json", config)
        _private_json(
            directory / "transport.json", {**facts, "offline_control": offline_upstream is not None}
        )
        profile = directory / "client.sb"
        profile.write_text(sandbox_profile(self.executable, workspace, port))
        usage_file = directory / "proxy-usage.json"
        broker_env = {
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "PORT": str(port),
            "EVALLAB_ZAI_PROXY_BIND_HOST": "127.0.0.1",
            "EVALLAB_ZAI_SECRET_PATH": str(key_file),
            "EVALLAB_ZAI_UPSTREAM": upstream,
            "EVALLAB_ZAI_PROXY_CAPABILITY": capability,
            "EVALLAB_ZAI_CAPABILITY_EXPIRES_AT": str(time.time() + self.timeout_seconds + 30),
            "EVALLAB_ZAI_ATTEMPT_ID": attempt_id,
            "EVALLAB_ZAI_USAGE_FILE": str(usage_file),
            "EVALLAB_ZAI_INPUT_COST_MICROS_PER_MILLION": str(ZAI_INPUT_COST_MICROS_PER_MILLION),
            "EVALLAB_ZAI_OUTPUT_COST_MICROS_PER_MILLION": str(ZAI_OUTPUT_COST_MICROS_PER_MILLION),
            **{
                "EVALLAB_ZAI_" + key.upper(): str(value)
                for key, value in asdict(self.limits).items()
            },
        }
        client_env = {
            "PATH": str(GIT_EXECUTABLE.parent) + ":/usr/bin:/bin",
            "HOME": str(workspace / "home"),
            "XDG_DATA_HOME": str(workspace / "data"),
            "XDG_CONFIG_HOME": str(workspace / "config"),
            "XDG_CACHE_HOME": str(workspace / "cache"),
            "XDG_STATE_HOME": str(workspace / "state"),
            "TMPDIR": str(workspace / "tmp"),
            "OPENCODE_CONFIG": str(workspace / "config.json"),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
            "OPENCODE_DISABLE_CLAUDE_CODE": "true",
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "OPENCODE_DISABLE_MODELS_FETCH": "true",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
            "DO_NOT_TRACK": "1",
            "CI": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CEILING_DIRECTORIES": str(workspace.parent),
            "GIT_OPTIONAL_LOCKS": "0",
        }
        text = prompt if isinstance(prompt, str) else json.dumps(prompt, ensure_ascii=False)
        prompt_file = workspace / "prompt.txt"
        persist_private_bytes(prompt_file, text.encode(), secrets=())
        command = [
            "/usr/bin/sandbox-exec",
            "-f",
            str(profile),
            str(self.executable),
            "run",
            "--pure",
            "--format",
            "json",
            "--agent",
            "proposer",
            "--model",
            MODEL,
            "--title",
            "GEPA code proposal",
            "--dir",
            str(workspace),
        ]
        client = None
        broker = None
        try:
            if offline_upstream is None:
                materialize_zai_secret_file(key_file)
            else:
                persist_private_bytes(
                    key_file, b"local-protocol-control-only\n", secrets=(), mode=0o400
                )
            with (directory / "broker.log").open("wb") as broker_log:
                broker = subprocess.Popen(
                    [sys.executable, str(self.repo_root / "containers/zai_secret_proxy.py")],
                    env=broker_env,
                    cwd=directory,
                    stdout=broker_log,
                    stderr=broker_log,
                    start_new_session=True,
                )
                deadline = time.monotonic() + 10
                while True:
                    if broker.poll() is not None:
                        raise OpenCodeTransportError("Broker exited before readiness")
                    try:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/healthz", timeout=0.2
                        ) as response:
                            if response.status == 200:
                                break
                    except (OSError, urllib.error.URLError):
                        pass
                    if time.monotonic() >= deadline:
                        raise OpenCodeTransportError("Broker readiness timed out")
                    time.sleep(0.05)
                with (
                    (directory / "events.jsonl").open("wb") as stdout,
                    (directory / "client.log").open("wb") as stderr,
                    prompt_file.open("rb") as stdin,
                ):
                    client = subprocess.Popen(
                        command,
                        env=client_env,
                        cwd=workspace,
                        stdin=stdin,
                        stdout=stdout,
                        stderr=stderr,
                        start_new_session=True,
                    )
                    deadline = time.monotonic() + self.timeout_seconds
                    while client.poll() is None:
                        if usage_file.is_file():
                            observed = json.loads(usage_file.read_text())
                            if any(
                                call.get("state") in {"unresolved", "exceeded"}
                                for call in observed.get("calls", [])
                            ):
                                raise OpenCodeTransportError(
                                    "Broker rejected the first response; no automatic retry"
                                )
                        if (directory / "events.jsonl").stat().st_size:
                            events = (directory / "events.jsonl").read_text()
                            for line in events.splitlines():
                                try:
                                    event = json.loads(line)
                                except json.JSONDecodeError:
                                    continue  # A writer may not have finished its last line.
                                if event.get("type") in {"tool_use", "error"}:
                                    raise OpenCodeTransportError(
                                        "OpenCode attempted a tool or reported an error"
                                    )
                        if time.monotonic() >= deadline:
                            raise OpenCodeTransportError("OpenCode timed out; no automatic retry")
                        if max(stdout.tell(), stderr.tell()) > MAX_TRANSCRIPT_BYTES:
                            raise OpenCodeTransportError("OpenCode output limit reached")
                        time.sleep(0.1)
                    if client.returncode != 0:
                        raise OpenCodeTransportError(
                            f"OpenCode exited with status {client.returncode}; inspect retained logs"
                        )
                usage = _read_proxy_usage(
                    usage_file,
                    capability_id=capability_id,
                    attempt_id=attempt_id,
                    limits=self.limits,
                    provider_label="Z.ai OpenCode proposer",
                    expected_pricing={
                        "input_cost_micros_per_million": ZAI_INPUT_COST_MICROS_PER_MILLION,
                        "output_cost_micros_per_million": ZAI_OUTPUT_COST_MICROS_PER_MILLION,
                    },
                )
                if usage["unresolved_requests"] or usage["totals"]["requests"] != 1:
                    raise OpenCodeTransportError(
                        "Proposer physical usage is not one reconciled request"
                    )
                call = usage["calls"][0]
                if call.get("returned_model") != "glm-5.3-flash" or call.get("status") != 200:
                    raise OpenCodeTransportError(
                        "Proposer response identity/status is not qualified"
                    )
                return {
                    "response": parse_response(directory / "events.jsonl"),
                    "usage": usage,
                    "upstream_estimated_cost_usd": usage["totals"]["cost_micros"] / 1_000_000,
                    "actual_cost_usd": None,
                    "transport": facts,
                    "offline_control": offline_upstream is not None,
                }
        finally:
            if client is not None:
                _stop(client)
            if broker is not None:
                _stop(broker)
            key_file.unlink(missing_ok=True)
            (workspace / "config.json").unlink(missing_ok=True)
