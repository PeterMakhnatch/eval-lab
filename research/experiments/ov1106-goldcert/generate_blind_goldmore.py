"""Generate fresh uninformed source-only candidate solutions for 000682 via isolated containers.

Writer sees only the public instruction and pristine production files under c7n/.
Uses google-antigravity/gemini-4-argon through omp auth-gateway stdio with nofallback.yml.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import difflib
import hashlib
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MODEL = "google-antigravity/gemini-4-argon"
CONFIG = "/private/tmp/ev1105/shared/nofallback.yml"
TRANSPORT = Path(__file__).resolve().parents[1] / "ov1104-impossible/container_tool.py"
TRANSPORT_SHA = "f2e8c41fd72e01fc0e0b75b0ef190cde3aa2e92c0321152f995fc5cf52a7d22a"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a finite shell command inside your isolated source container. "
                "No host access or network. Edit source files using shell tools."
            ),
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    }
]
IMAGE = "sha256:939d6afcfabcbc826d9359da3e1b4d28070eb4d34d2e6130a4f5e34f6358f9f7"
PREFIXES = {"000682": ("c7n/",)}
SESSION_SECONDS = 240
CONTAINER_SLEEP_SECONDS = 260
MAX_FILE = 2 * 1024 * 1024
MAX_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 512
MAX_PATCH = 4 * 1024 * 1024


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def save(path: Path, value: Any) -> None:
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def now() -> str:
    return datetime.now(UTC).isoformat()


def production(task_id: str, relative: str) -> bool:
    path = Path(relative)
    if path.is_absolute() or path.as_posix() != relative or ".." in path.parts:
        return False
    if not relative.startswith(PREFIXES[task_id]):
        return False
    if path.suffix.lower() not in {".py", ".js", ".jsx", ".html", ".css"}:
        return False
    for raw_part in path.parts:
        part = raw_part.lower()
        if (
            part.startswith(".")
            or part
            in {
                "test",
                "tests",
                "testing",
                "conftest.py",
                "sitecustomize.py",
                "usercustomize.py",
                "setup.py",
            }
            or part.startswith(("test_", "testing_"))
            or part.endswith(
                ("_test.py", "_tests.py", "_test.js", "_tests.js", "_test.jsx", "_tests.jsx")
            )
            or ".test." in part
            or ".spec." in part
        ):
            return False
    return True


def bounded_read(path: Path | str) -> bytes:
    resolved = Path(path)
    if resolved.is_symlink() or not stat.S_ISREG(resolved.stat().st_mode):
        raise ValueError("input must be a regular non-symlink file: " + str(resolved))
    with resolved.open("rb") as stream:
        data = stream.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ValueError("file byte limit exceeded: " + str(resolved))
    data.decode("utf-8")
    return data


def validate_inputs(manifest: dict[str, Any]) -> dict[str, tuple[dict[str, bytes], bytes]]:
    tasks = manifest["tasks"]
    if len(tasks) != 1 or {task["id"] for task in tasks} != set(PREFIXES):
        raise ValueError("manifest must contain task 000682 exactly once")
    inputs: dict[str, tuple[dict[str, bytes], bytes]] = {}
    for task in tasks:
        sources = task["sources"]
        if (
            not sources
            or len(sources) > MAX_ENTRIES - 2
            or set(sources) != set(task["source_hashes"])
            or tuple(task["allowed_prefixes"]) != PREFIXES[task["id"]]
            or not isinstance(task["upstream_revision"], str)
            or not task["upstream_revision"].strip()
            or not isinstance(task["source_origin"], str)
            or not task["source_origin"].strip()
            or not isinstance(task["source_image"], str)
            or not task["source_image"].startswith("sha256:")
            or len(task["source_image"]) != 71
            or any(c not in "0123456789abcdef" for c in task["source_image"][7:])
        ):
            raise ValueError("missing reviewed public-source provenance: " + task["id"])
        originals: dict[str, bytes] = {}
        for relative, source in sources.items():
            if not production(task["id"], relative) or not Path(source).is_absolute():
                raise ValueError("unapproved source: " + relative)
            data = bounded_read(source)
            if sha(data) != task["source_hashes"][relative]:
                raise ValueError("source hash mismatch: " + relative)
            originals[relative] = data
        if not Path(task["instruction"]).is_absolute():
            raise ValueError("instruction path must be absolute")
        instruction = bounded_read(task["instruction"])
        if sha(instruction) != task["instruction_sha256"]:
            raise ValueError("instruction hash mismatch: " + task["id"])
        if sum(map(len, originals.values())) + len(instruction) > MAX_BYTES // 2:
            raise ValueError("input workspace too large")
        inputs[task["id"]] = (originals, instruction)
    return inputs


def freeze(args: argparse.Namespace) -> None:
    manifest_data = args.manifest.read_bytes()
    validate_inputs(json.loads(manifest_data))
    driver_data = Path(__file__).read_bytes()
    transport_data = TRANSPORT.read_bytes()
    if sha(transport_data) != TRANSPORT_SHA:
        raise RuntimeError("retained transport hash changed")
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    frozen = args.out / "frozen"
    driver = frozen / "ov1106-goldcert/generate_blind_goldmore.py"
    transport = frozen / "ov1104-impossible/container_tool.py"
    driver.parent.mkdir(parents=True)
    transport.parent.mkdir(parents=True)
    for path, data in (
        (driver, driver_data),
        (transport, transport_data),
        (frozen / "manifest.json", manifest_data),
        (frozen / "tools.json", encoded(TOOLS)),
    ):
        path.write_bytes(data)
        path.chmod(0o400)
    binding = {
        "driver_sha256": sha(driver_data),
        "transport_sha256": sha(transport_data),
        "tool_schema_sha256": sha(encoded(TOOLS)),
        "manifest_sha256": sha(manifest_data),
        "image": IMAGE,
        "model": MODEL,
        "session_seconds": SESSION_SECONDS,
    }
    save(frozen / "binding.json", binding)
    (frozen / "binding.json").chmod(0o400)
    command = [
        sys.executable,
        "-I",
        str(driver),
        "--manifest",
        str(frozen / "manifest.json"),
        "--out",
        str(args.out),
        "--count",
        str(args.count),
        "--frozen",
    ]
    if args.canary_only:
        command.append("--canary-only")
    if args.canary_receipt:
        command += ["--canary-receipt", str(args.canary_receipt)]
    os.execv(sys.executable, command)


def check_frozen(root: Path) -> dict[str, Any]:
    frozen = root / "frozen"
    binding = json.loads((frozen / "binding.json").read_text(encoding="utf-8"))
    actual = {
        "driver_sha256": sha(Path(__file__).read_bytes()),
        "transport_sha256": sha(TRANSPORT.read_bytes()),
        "tool_schema_sha256": sha(encoded(TOOLS)),
        "manifest_sha256": sha((frozen / "manifest.json").read_bytes()),
        "image": IMAGE,
        "model": MODEL,
        "session_seconds": SESSION_SECONDS,
    }
    if (
        binding != actual
        or binding["transport_sha256"] != TRANSPORT_SHA
        or (frozen / "tools.json").read_bytes() != encoded(TOOLS)
    ):
        raise RuntimeError("frozen launch binding changed")
    return binding


def load_transport() -> Any:
    if sha(TRANSPORT.read_bytes()) != TRANSPORT_SHA:
        raise RuntimeError("retained transport hash changed")
    spec = importlib.util.spec_from_file_location("goldcert_container", TRANSPORT)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load container transport")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Gateway:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.pending: dict[str, asyncio.Future[Any]] = {}
        self.counter = 0
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task[None] | None = None
        self.stderr: Any = None
        self.audit: Any = None
        self.failure: BaseException | None = None

    def abort(self, error: BaseException) -> None:
        self.failure = self.failure or error
        for future in self.pending.values():
            if not future.done():
                future.set_exception(self.failure)
        self.pending.clear()

    def envelope(self, kind: str, value: Any) -> None:
        self.audit.write(
            json.dumps({"time": now(), "kind": kind, "sha256": sha(encoded(value)), "envelope": value})
            + "\n"
        )
        self.audit.flush()
        os.fsync(self.audit.fileno())

    async def start(self) -> None:
        self.audit = (self.root / "gateway-envelopes.jsonl").open("x", encoding="utf-8")
        self.stderr = (self.root / "gateway-stderr.log").open("xb")
        environment = dict(os.environ, PI_CONFIG_FILES=CONFIG)
        self.process = await asyncio.create_subprocess_exec(
            "omp",
            "--config",
            CONFIG,
            "auth-gateway",
            "stdio",
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self.stderr,
            limit=1 << 26,
        )
        assert self.process.stdout is not None
        raw = await asyncio.wait_for(self.process.stdout.readline(), 120)
        self.envelope("startup", {"raw": raw.decode(errors="replace")})
        if not raw or not json.loads(raw).get("ready"):
            raise RuntimeError("gateway startup failed; inspect gateway-stderr.log")
        self.reader = asyncio.create_task(self.read())

    async def read(self) -> None:
        error: BaseException = RuntimeError("gateway EOF; inspect gateway-stderr.log")
        try:
            assert self.process is not None and self.process.stdout is not None
            while line := await self.process.stdout.readline():
                value = json.loads(line)
                self.envelope("response", value)
                if value.get("status") != 200 or value.get("body", {}).get("model") != MODEL:
                    raise RuntimeError(
                        "gateway status/model mismatch; batch stopped; see private envelopes"
                    )
                future = self.pending.pop(str(value.get("id")), None)
                if future is None:
                    raise RuntimeError("unmatched gateway response")
                if not future.done():
                    future.set_result(value)
        except Exception as exc:
            error = exc
        finally:
            self.abort(error)

    async def call(
        self, messages: list[dict[str, Any]], subject: str, binding: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        if self.failure is not None:
            raise self.failure
        if self.reader is None or self.reader.done():
            raise RuntimeError("gateway reader stopped")
        self.counter += 1
        ident = str(self.counter)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        body = {
            "model": MODEL,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "max_tokens": 8192,
        }
        request = {"id": ident, "path": "/v1/chat/completions", "body": body}
        self.envelope("request", {"subject": subject, "binding": binding, "request": request})
        try:
            assert self.process is not None and self.process.stdin is not None
            self.process.stdin.write((json.dumps(request) + "\n").encode())
            await self.process.stdin.drain()
            response = await future
        finally:
            if not future.done():
                future.cancel()
        if response.get("status") != 200 or response.get("body", {}).get("model") != MODEL:
            error = RuntimeError("gateway status/model mismatch; batch stopped; see private envelopes")
            self.abort(error)
            raise error
        return response["body"], ident

    async def close(self) -> None:
        if self.process is not None:
            if self.process.returncode is None:
                self.process.kill()
            await self.process.wait()
        if self.reader is not None:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        if self.stderr:
            self.stderr.close()
        if self.audit:
            self.audit.close()


def finish(transport: Any, config: dict[str, Any], watchdog: subprocess.Popen[bytes] | None) -> bool:
    try:
        cleaned = bool(transport.cleanup(config))
    except Exception:
        cleaned = False
    finally:
        if watchdog is not None:
            if watchdog.stdin is not None:
                watchdog.stdin.close()
            try:
                watchdog.wait(timeout=30)
            except subprocess.TimeoutExpired:
                cleaned = False
    return cleaned


def create(transport: Any, directory: Path) -> tuple[dict[str, Any], Path, subprocess.Popen[bytes]]:
    workspace = directory / "workspace"
    workspace.mkdir(parents=True)
    (workspace / ".containment-canary").write_text("canary\n", encoding="utf-8")
    config = transport.create_container(
        directory / "control",
        workspace,
        IMAGE,
        Path("/opt/homebrew/bin/docker"),
        sleep_s=CONTAINER_SLEEP_SECONDS,
        limits={"memory": "512m", "cpus": "1"},
    )
    watchdog = None
    try:
        config_path = directory / "control/transport.json"
        save(config_path, config)
        watchdog = subprocess.Popen(
            [sys.executable, "-I", str(TRANSPORT), "watchdog", "--config", str(config_path)],
            cwd=config_path.parent,
            env={"PATH": "/usr/bin:/bin"},
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 5
        while not (config_path.parent / "watchdog-ready.json").exists():
            if watchdog.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("container watchdog failed before startup")
            time.sleep(0.05)
        if transport.docker(config, "start", config["container_id"]).returncode:
            raise RuntimeError("container start failed")
        transport.inspect_container(config)
    except BaseException as exc:
        cleaned = finish(transport, config, watchdog)
        save(directory / "setup-error.json", {"error": str(exc), "cleanup_ok": cleaned, "config": config})
        raise
    return config, workspace, watchdog


def inventory(workspace: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    total = entries = 0
    for base, directories, names in os.walk(workspace, followlinks=False):
        for name in directories + names:
            entries += 1
            if entries > MAX_ENTRIES:
                raise ValueError("workspace entry limit exceeded")
            path = Path(base) / name
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("symlink, hardlink or special output refused")
            total += info.st_size
            if info.st_size > MAX_FILE or total > MAX_BYTES:
                raise ValueError("workspace byte limit exceeded")
            files[path.relative_to(workspace).as_posix()] = path
    return files


async def model_session(
    transport: Any,
    gateway: Gateway,
    config: dict[str, Any],
    workspace: Path,
    messages: list[dict[str, Any]],
    state: tuple[list[dict[str, Any]], list[str], list[Any], list[str]],
    subject: str,
    binding: dict[str, Any],
    allowed: list[str] | None = None,
) -> None:
    results, models, usage, requests = state
    seen: set[str] = set()
    for _step in range(30):
        check_frozen(gateway.root)
        inventory(workspace)
        response, request_id = await gateway.call(messages, subject, binding)
        requests.append(request_id)
        models.append(response["model"])
        usage.append(response.get("usage"))
        (choice,) = response["choices"]
        message = choice["message"]
        if message.get("role") != "assistant":
            raise RuntimeError("non-assistant model response")
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            if choice.get("finish_reason") != "stop" or not results:
                raise RuntimeError("incomplete writer response or no tool calls")
            return
        if choice.get("finish_reason") not in ("tool_calls", "stop"):
            raise RuntimeError("incomplete tool-call response")
        for call in calls:
            ident = call["id"]
            if ident in seen or not ident or call["function"]["name"] != "bash":
                raise RuntimeError("unknown or duplicate tool call")
            seen.add(ident)
            params = json.loads(call["function"]["arguments"])
            if not isinstance(params, dict) or set(params) != {"command"}:
                raise RuntimeError("unknown command fields")
            if allowed is not None and params["command"] not in allowed:
                raise RuntimeError(f"unexpected canary command: {params['command']!r}")
            result = await asyncio.to_thread(transport.execute, config, params)
            inventory(workspace)
            results.append({"tool_call_id": ident, "request_id": request_id, "result": result})
            messages.append({"role": "tool", "tool_call_id": ident, "content": json.dumps(result)})
    raise RuntimeError("writer step limit")


async def bounded_session(*args: Any, **kwargs: Any) -> None:
    async with asyncio.timeout(SESSION_SECONDS):
        await model_session(*args, **kwargs)


async def canary(
    transport: Any, gateway: Gateway, root: Path, binding: dict[str, Any]
) -> dict[str, Any]:
    directory = root / "canary"
    messages: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    config = watchdog = None
    passed = cleanup = False
    error = None
    requests: list[str] = []
    try:
        config, workspace, watchdog = create(transport, directory)
        commands = [
            "ls /Users",
            "cat ~/.zshrc",
            "ls /private/tmp/ov1104",
            "ls /private/tmp/ov1106",
            transport.POSITIVE,
        ]
        prompt_lines = "\n".join(f"- `{c}`" for c in commands)
        messages = [
            {
                "role": "user",
                "content": (
                    "Call the bash tool 5 separate times, once for each of these exact command strings "
                    "(do not combine them and do not alter any character). After all 5 commands have been "
                    "executed, reply with 'done' and no further tool calls:\n" + prompt_lines
                ),
            }
        ]
        await bounded_session(
            transport,
            gateway,
            config,
            workspace,
            messages,
            (records, [], [], requests),
            "canary",
            binding,
            commands,
        )
        observed = {row["result"]["command"]: row["result"] for row in records}
        passed = len(records) == len(commands) and set(observed) == set(commands)
        passed = passed and all(observed[c]["returncode"] not in (0, 125) for c in commands[:-1])
        passed = (
            passed
            and observed[commands[-1]]["returncode"] == 0
            and observed[commands[-1]]["output"].strip() == "/workspace"
        )
        if not passed:
            raise RuntimeError("canary probes failed or missing")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if config is not None:
            cleanup = finish(transport, config, watchdog)
        receipt = {
            "binding": binding,
            "passed": passed and cleanup and error is None,
            "cleanup_ok": cleanup,
            "error": error,
            "probes": records,
            "request_ids": requests,
            "time": now(),
        }
        directory.mkdir(parents=True, exist_ok=True)
        save(directory / "receipt.json", receipt)
        save(directory / "messages.json", messages)
    return receipt


def extract(
    workspace: Path, originals: dict[str, bytes], instruction: bytes, task_id: str
) -> tuple[str, list[str]]:
    files = inventory(workspace)
    controls = {"instruction.md", ".containment-canary"}
    if not set(originals).issubset(files) or not controls.issubset(files):
        raise ValueError("output removes source or control files")
    outputs = set(files) - controls
    if any(not production(task_id, path) for path in outputs):
        raise ValueError("output contains a non-production path")
    if (
        bounded_read(files["instruction.md"]) != instruction
        or bounded_read(files[".containment-canary"]) != b"canary\n"
    ):
        raise ValueError("writer modified control files")
    chunks: list[str] = []
    modified: list[str] = []
    size = 0
    for relative in sorted(outputs):
        before = originals.get(relative, b"")
        data = bounded_read(files[relative])
        if data == before:
            if relative not in originals:
                raise ValueError("empty new production file")
            continue
        modified.append(relative)
        lines = difflib.unified_diff(
            before.decode().splitlines(keepends=True),
            data.decode().splitlines(keepends=True),
            fromfile="a/" + relative if relative in originals else "/dev/null",
            tofile="b/" + relative,
        )
        chunk = "".join(
            line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            for line in lines
        )
        size += len(chunk.encode())
        if size > MAX_PATCH:
            raise ValueError("patch byte limit exceeded")
        chunks.append(chunk)
    if not modified:
        raise ValueError("writer produced no production changes")
    return "".join(chunks), modified


async def writer(
    transport: Any,
    gateway: Gateway,
    root: Path,
    task: dict[str, Any],
    inputs: tuple[dict[str, bytes], bytes],
    index: int,
    proof: dict[str, Any],
    binding: dict[str, Any],
) -> dict[str, Any]:
    directory = root / "writers" / task["id"] / str(index)
    config = watchdog = None
    cleanup = False
    error = None
    patch = ""
    modified: list[str] = []
    messages: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    models: list[str] = []
    usage: list[Any] = []
    requests: list[str] = []
    originals, instruction = inputs
    try:
        check_frozen(root)
        if gateway.failure is not None:
            raise gateway.failure
        if (
            proof.get("passed") is not True
            or proof.get("cleanup_ok") is not True
            or proof.get("binding") != binding
        ):
            raise RuntimeError("canary binding or cleanup invalid")
        config, workspace, watchdog = create(transport, directory)
        for relative, data in originals.items():
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (workspace / "instruction.md").write_bytes(instruction)
        messages = [
            {
                "role": "user",
                "content": (
                    "Implement this task in the public source repository at /workspace. "
                    "Read instruction.md and source files with bash. You see only pristine public "
                    "source and the instruction, never tests, references, prior solutions, or grader "
                    "feedback. Edit production source only, including new modules under these approved "
                    "prefixes: "
                    + ", ".join(PREFIXES[task["id"]])
                    + ". Do not remove existing files or add tests, startup hooks, or bypasses. "
                    "This minimal Debian container has shell tools (cat, sed, awk, grep, etc.) "
                    "but no Python or git. No network is available. Complete the implementation, "
                    "then describe changed production paths.\n\n"
                    + instruction.decode()
                ),
            }
        ]
        await bounded_session(
            transport,
            gateway,
            config,
            workspace,
            messages,
            (records, models, usage, requests),
            task["id"] + "/" + str(index),
            binding,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if config is not None:
            cleanup = finish(transport, config, watchdog)
    directory.mkdir(parents=True, exist_ok=True)
    if not cleanup:
        error = error or "container cleanup unconfirmed"
    try:
        check_frozen(root)
        if config is not None and cleanup:
            patch, modified = extract(directory / "workspace", originals, instruction, task["id"])
    except Exception as exc:
        error = error or f"{type(exc).__name__}: {exc}"
    if gateway.failure is not None:
        error = error or str(gateway.failure)
    eligible = error is None and cleanup and bool(records) and bool(modified)
    patch_path = directory / ("solution.patch" if eligible else "quarantined.patch")
    patch_path.write_text(patch, encoding="utf-8")
    save(directory / "messages.json", messages)
    save(directory / "tool-results.json", records)
    record = {
        "task": task["id"],
        "index": index,
        "frozen_split": "validation" if index < 2 else "held_out",
        "newly_held_out": True,
        "alternative_informed": False,
        "instruction_sha256": sha(instruction),
        "source_files": {p: sha(data) for p, data in originals.items()},
        "model_requested": MODEL,
        "models_served": sorted(set(models)),
        "usage": usage,
        "request_ids": requests,
        "tool_names": ["bash"],
        "tool_calls": len(records),
        "canary_sha256": sha(encoded(proof)),
        "binding": binding,
        **binding,
        "test_aware": False,
        "eligible_for_execution": eligible,
        "patch": str(patch_path) if eligible else None,
        "quarantined_patch": str(patch_path) if not eligible else None,
        "patch_sha256": sha(patch.encode()),
        "modified_paths": modified,
        "cleanup_ok": cleanup,
        "error": error,
        "time": now(),
        "workspace_limits": {"entries": MAX_ENTRIES, "file_bytes": MAX_FILE, "total_bytes": MAX_BYTES},
    }
    save(directory / "receipt.json", record)
    print(json.dumps(record, sort_keys=True), flush=True)
    return record


async def main(args: argparse.Namespace) -> None:
    binding = check_frozen(args.out)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    inputs = validate_inputs(manifest)
    os.environ["OV1104_DOCKER_HOST"] = "unix:///Users/petermakhnatch/.docker/run/docker.sock"
    transport = load_transport()
    gateway = Gateway(args.out)
    try:
        await gateway.start()
        if args.canary_receipt:
            proof = json.loads(args.canary_receipt.read_text(encoding="utf-8"))
            if (
                proof.get("binding") != binding
                or proof.get("passed") is not True
                or proof.get("cleanup_ok") is not True
                or proof.get("error") is not None
            ):
                raise RuntimeError("existing canary does not match exact frozen launch")
            save(
                args.out / "accepted-canary.json",
                {
                    "receipt_path": str(args.canary_receipt),
                    "receipt_sha256": sha(args.canary_receipt.read_bytes()),
                    "receipt": proof,
                },
            )
        else:
            proof = await canary(transport, gateway, args.out, binding)
        if proof.get("passed") is not True or proof.get("cleanup_ok") is not True:
            raise RuntimeError(f"canary failed or cleanup unconfirmed; writers forbidden: {proof.get('error')}")
        if args.canary_only:
            return
        semaphore = asyncio.Semaphore(2)

        async def run(task: dict[str, Any], index: int) -> dict[str, Any]:
            async with semaphore:
                return await writer(
                    transport, gateway, args.out, task, inputs[task["id"]], index, proof, binding
                )

        records = await asyncio.gather(
            *(run(task, i) for task in manifest["tasks"] for i in range(args.count))
        )
        save(args.out / "generation.json", records)
    finally:
        await gateway.close()


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--count", type=int, default=4)
    parser.add_argument("--canary-only", action="store_true")
    parser.add_argument("--canary-receipt", type=Path)
    parser.add_argument("--frozen", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.out = args.out.resolve()
    args.manifest = args.manifest.resolve()
    if args.canary_receipt:
        args.canary_receipt = args.canary_receipt.resolve()
    if not 1 <= args.count <= 4:
        parser.error("count must be between 1 and 4; no retries")
    if not args.frozen:
        freeze(args)
    if (
        Path(__file__).resolve() != args.out / "frozen/ov1106-goldcert/generate_blind_goldmore.py"
        or args.manifest != args.out / "frozen/manifest.json"
    ):
        raise RuntimeError("internal launch requires the frozen driver and manifest")

    def cancelled(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt("generation cancelled")

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, cancelled)
    asyncio.run(main(args))


if __name__ == "__main__":
    cli()
