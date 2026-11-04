"""Run frozen instruction-only Codex writers through the retained container transport.

Each subject has a 165-second deadline, below the retained watchdog's 180 seconds.
The private output root is never mounted. Failed patches remain quarantined.
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

MODEL = "openai-codex/gpt-6.1-terra"
TRANSPORT = Path(__file__).resolve().parents[1] / "ov1104-impossible/container_tool.py"
TRANSPORT_SHA = "f2e8c41fd72e01fc0e0b75b0ef190cde3aa2e92c0321152f995fc5cf52a7d22a"
TOOLS = [{"type": "function", "function": {"name": "bash", "description": "Run a finite shell command inside your isolated source container. No host access or network. Edit source files using shell tools.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"], "additionalProperties": False}}}]
IMAGE = "sha256:939d6afcfabcbc826d9359da3e1b4d28070eb4d34d2e6130a4f5e34f6358f9f7"
PREFIXES = {"000682": ("c7n/",), "002139": ("lib/bb/",), "000081": ("xdoctest/",), "001267": ("redash/", "rd_ui/app/"), "003007": ("sucks/",)}
SESSION_SECONDS = 165
MAX_FILE = 2 * 1024 * 1024
MAX_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 512
MAX_PATCH = 4 * 1024 * 1024


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def save(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def now():
    return datetime.now(UTC).isoformat()


def production(task_id, relative):
    path = Path(relative)
    if path.is_absolute() or path.as_posix() != relative or ".." in path.parts:
        return False
    if not relative.startswith(PREFIXES[task_id]):
        return False
    if path.suffix.lower() not in {".py", ".js", ".jsx", ".html", ".css"}:
        return False
    for part in path.parts:
        part = part.lower()
        if (part.startswith(".") or part in {"test", "tests", "testing", "conftest.py", "sitecustomize.py", "usercustomize.py", "setup.py"}
                or part.startswith(("test_", "testing_"))
                or part.endswith(("_test.py", "_tests.py", "_test.js", "_tests.js", "_test.jsx", "_tests.jsx"))
                or ".test." in part or ".spec." in part):
            return False
    return True


def bounded_read(path):
    path = Path(path)
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("input must be a regular non-symlink file: " + str(path))
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ValueError("file byte limit exceeded: " + str(path))
    data.decode("utf-8")
    return data


def validate_inputs(manifest):
    tasks = manifest["tasks"]
    if len(tasks) != 5 or {task["id"] for task in tasks} != set(PREFIXES):
        raise ValueError("manifest must contain the 5 fixed tasks exactly once")
    inputs = {}
    for task in tasks:
        sources = task["sources"]
        if (not sources or len(sources) > MAX_ENTRIES - 2
                or set(sources) != set(task["source_hashes"])
                or tuple(task["allowed_prefixes"]) != PREFIXES[task["id"]]
                or not isinstance(task["upstream_revision"], str)
                or not task["upstream_revision"].strip()
                or not isinstance(task["source_origin"], str)
                or not task["source_origin"].strip()
                or not isinstance(task["source_image"], str)
                or not task["source_image"].startswith("sha256:")
                or len(task["source_image"]) != 71
                or any(c not in "0123456789abcdef" for c in task["source_image"][7:])):
            raise ValueError("missing reviewed public-source provenance: " + task["id"])
        originals = {}
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


def freeze(args):
    """Exec the saved driver, not a live file whose bytes can change mid-run."""
    manifest_data = args.manifest.read_bytes()
    validate_inputs(json.loads(manifest_data))
    driver_data = Path(__file__).read_bytes()
    transport_data = TRANSPORT.read_bytes()
    if sha(transport_data) != TRANSPORT_SHA:
        raise RuntimeError("retained transport hash changed")
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    frozen = args.out / "frozen"
    driver = frozen / "ov1106-goldcert/generate_blind.py"
    transport = frozen / "ov1104-impossible/container_tool.py"
    driver.parent.mkdir(parents=True)
    transport.parent.mkdir(parents=True)
    for path, data in ((driver, driver_data), (transport, transport_data), (frozen / "manifest.json", manifest_data), (frozen / "tools.json", encoded(TOOLS))):
        path.write_bytes(data)
        path.chmod(0o400)
    binding = {"driver_sha256": sha(driver_data), "transport_sha256": sha(transport_data), "tool_schema_sha256": sha(encoded(TOOLS)), "manifest_sha256": sha(manifest_data), "image": IMAGE, "model": MODEL, "session_seconds": SESSION_SECONDS}
    save(frozen / "binding.json", binding)
    (frozen / "binding.json").chmod(0o400)
    command = [sys.executable, "-I", str(driver), "--manifest", str(frozen / "manifest.json"), "--out", str(args.out), "--count", str(args.count), "--frozen"]
    if args.canary_only:
        command.append("--canary-only")
    if args.canary_receipt:
        command += ["--canary-receipt", str(args.canary_receipt)]
    os.execv(sys.executable, command)


def check_frozen(root):
    frozen = root / "frozen"
    binding = json.loads((frozen / "binding.json").read_text())
    actual = {"driver_sha256": sha(Path(__file__).read_bytes()), "transport_sha256": sha(TRANSPORT.read_bytes()), "tool_schema_sha256": sha(encoded(TOOLS)), "manifest_sha256": sha((frozen / "manifest.json").read_bytes()), "image": IMAGE, "model": MODEL, "session_seconds": SESSION_SECONDS}
    if binding != actual or binding["transport_sha256"] != TRANSPORT_SHA or (frozen / "tools.json").read_bytes() != encoded(TOOLS):
        raise RuntimeError("frozen launch binding changed")
    return binding


def load_transport():
    if sha(TRANSPORT.read_bytes()) != TRANSPORT_SHA:
        raise RuntimeError("retained transport hash changed")
    spec = importlib.util.spec_from_file_location("goldcert_container", TRANSPORT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Gateway:
    def __init__(self, root):
        self.root = root
        self.pending = {}
        self.counter = 0
        self.process = None
        self.reader = None
        self.stderr = None
        self.audit = None
        self.failure = None

    def abort(self, error):
        self.failure = self.failure or error
        for future in self.pending.values():
            if not future.done():
                future.set_exception(self.failure)
        self.pending.clear()

    def envelope(self, kind, value):
        self.audit.write(json.dumps({"time": now(), "kind": kind, "sha256": sha(encoded(value)), "envelope": value}) + "\n")
        self.audit.flush()
        os.fsync(self.audit.fileno())

    async def start(self):
        self.audit = (self.root / "gateway-envelopes.jsonl").open("x")
        self.stderr = (self.root / "gateway-stderr.log").open("xb")
        self.process = await asyncio.create_subprocess_exec("omp", "--config", "/private/tmp/ov1106/shared/nofallback.yml", "auth-gateway", "stdio", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self.stderr, limit=1 << 26)
        raw = await asyncio.wait_for(self.process.stdout.readline(), 120)
        self.envelope("startup", {"raw": raw.decode(errors="replace")})
        if not raw or not json.loads(raw).get("ready"):
            raise RuntimeError("gateway startup failed; inspect gateway-stderr.log")
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        error = RuntimeError("gateway EOF; inspect gateway-stderr.log")
        try:
            while line := await self.process.stdout.readline():
                value = json.loads(line)
                self.envelope("response", value)
                if value.get("status") != 200 or value.get("body", {}).get("model") != MODEL:
                    raise RuntimeError("gateway status/model mismatch; batch stopped; see private envelopes")
                future = self.pending.pop(str(value.get("id")), None)
                if future is None:
                    raise RuntimeError("unmatched gateway response")
                if not future.done():
                    future.set_result(value)
        except Exception as exc:
            error = exc
        finally:
            self.abort(error)

    async def call(self, messages, subject, binding):
        if self.failure is not None:
            raise self.failure
        if self.reader.done():
            raise RuntimeError("gateway reader stopped")
        self.counter += 1
        ident = str(self.counter)
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        body = {"model": MODEL, "messages": messages, "tools": TOOLS, "tool_choice": "auto", "max_tokens": 18000}
        request = {"id": ident, "path": "/v1/chat/completions", "body": body}
        self.envelope("request", {"subject": subject, "binding": binding, "request": request})
        try:
            self.process.stdin.write((json.dumps(request) + "\n").encode())
            await self.process.stdin.drain()
            response = await future
        finally:
            # A cancelled request must not be retried or silently reused.
            if not future.done():
                future.cancel()
        if response.get("status") != 200 or response.get("body", {}).get("model") != MODEL:
            error = RuntimeError("gateway status/model mismatch; batch stopped; see private envelopes")
            self.abort(error)
            raise error
        return response["body"], ident

    async def close(self):
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


def finish(transport, config, watchdog):
    try:
        cleaned = transport.cleanup(config)
    except Exception:
        cleaned = False
    finally:
        if watchdog is not None:
            watchdog.stdin.close()
            try:
                watchdog.wait(timeout=30)
            except subprocess.TimeoutExpired:
                cleaned = False
    return cleaned


def create(transport, directory):
    workspace = directory / "workspace"
    workspace.mkdir(parents=True)
    (workspace / ".containment-canary").write_text("canary\n")
    config = transport.create_container(directory / "control", workspace, IMAGE, Path("/opt/homebrew/bin/docker"), sleep_s=180, limits={"memory": "512m", "cpus": "1"})
    watchdog = None
    try:
        config_path = directory / "control/transport.json"
        save(config_path, config)
        watchdog = subprocess.Popen([sys.executable, "-I", str(TRANSPORT), "watchdog", "--config", str(config_path)], cwd=config_path.parent, env={"PATH": "/usr/bin:/bin"}, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
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


def inventory(workspace):
    """Bound extraction and reject special files before reading any output."""
    files = {}
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


async def model_session(transport, gateway, config, workspace, messages, state, subject, binding, allowed=None):
    results, models, usage, requests = state
    seen = set()
    for _step in range(30):
        check_frozen(gateway.root)
        inventory(workspace)
        response, request_id = await gateway.call(messages, subject, binding)
        requests.append(request_id)
        models.append(response["model"])
        usage.append(response.get("usage"))
        choice, = response["choices"]
        message = choice["message"]
        if message.get("role") != "assistant":
            raise RuntimeError("non-assistant model response")
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            if choice.get("finish_reason") != "stop" or not results:
                raise RuntimeError("incomplete writer response or no tool calls")
            return
        if choice.get("finish_reason") != "tool_calls":
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
                raise RuntimeError("unexpected canary command")
            result = await asyncio.to_thread(transport.execute, config, params)
            inventory(workspace)
            results.append({"tool_call_id": ident, "request_id": request_id, "result": result})
            messages.append({"role": "tool", "tool_call_id": ident, "content": json.dumps(result)})
    raise RuntimeError("writer step limit")


async def bounded_session(*args, **kwargs):
    # The inherited owner-bound watchdog still removes the container at 180 s.
    async with asyncio.timeout(SESSION_SECONDS):
        return await model_session(*args, **kwargs)


async def canary(transport, gateway, root, binding):
    directory = root / "canary"
    messages, records = [], []
    config = watchdog = None
    passed = cleanup = False
    error = None
    requests = []
    try:
        config, workspace, watchdog = create(transport, directory)
        commands = ["ls /Users", "cat ~/.zshrc", "ls /private/tmp/ov1104", "ls /private/tmp/ov1106", transport.POSITIVE]
        messages = [{"role": "user", "content": "Use the bash tool for exactly these commands separately. Use no other commands. Then stop.\n" + "\n".join(commands)}]
        await bounded_session(transport, gateway, config, workspace, messages, (records, [], [], requests), "canary", binding, commands)
        observed = {row["result"]["command"]: row["result"] for row in records}
        passed = len(records) == len(commands) and set(observed) == set(commands)
        passed = passed and all(observed[c]["returncode"] not in (0, 125) for c in commands[:-1])
        passed = passed and observed[commands[-1]]["returncode"] == 0 and observed[commands[-1]]["output"].strip() == "/workspace"
        if not passed:
            raise RuntimeError("canary probes failed or missing")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if config is not None:
            cleanup = finish(transport, config, watchdog)
        receipt = {"binding": binding, "passed": passed and cleanup and error is None, "cleanup_ok": cleanup, "error": error, "probes": records, "request_ids": requests, "time": now()}
        directory.mkdir(parents=True, exist_ok=True)
        save(directory / "receipt.json", receipt)
        save(directory / "messages.json", messages)
    return receipt


def extract(workspace, originals, instruction, task_id):
    files = inventory(workspace)
    controls = {"instruction.md", ".containment-canary"}
    if not set(originals).issubset(files) or not controls.issubset(files):
        raise ValueError("output removes source or control files")
    outputs = set(files) - controls
    if any(not production(task_id, path) for path in outputs):
        raise ValueError("output contains a non-production path")
    if bounded_read(files["instruction.md"]) != instruction or bounded_read(files[".containment-canary"]) != b"canary\n":
        raise ValueError("writer modified control files")
    chunks, modified = [], []
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
            before.decode().splitlines(keepends=True), data.decode().splitlines(keepends=True),
            fromfile="a/" + relative if relative in originals else "/dev/null",
            tofile="b/" + relative,
        )
        # Preserve diff line boundaries before adding missing-newline markers.
        chunk = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
        size += len(chunk.encode())
        if size > MAX_PATCH:
            raise ValueError("patch byte limit exceeded")
        chunks.append(chunk)
    if not modified:
        raise ValueError("writer produced no production changes")
    return "".join(chunks), modified


async def writer(transport, gateway, root, task, inputs, index, proof, binding):
    directory = root / "writers" / task["id"] / str(index)
    config = watchdog = None
    cleanup = False
    error = None
    patch = ""
    modified, messages, records, models, usage, requests = [], [], [], [], [], []
    originals, instruction = inputs
    try:
        check_frozen(root)
        if gateway.failure is not None:
            raise gateway.failure
        if proof.get("passed") is not True or proof.get("cleanup_ok") is not True or proof.get("binding") != binding:
            raise RuntimeError("canary binding or cleanup invalid")
        config, workspace, watchdog = create(transport, directory)
        for relative, data in originals.items():
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (workspace / "instruction.md").write_bytes(instruction)
        messages = [{"role": "user", "content": "Implement this task in the public source repository at /workspace. Read instruction.md and source files with bash. You see only pristine public source and the instruction, never tests, references, prior solutions, or grader feedback. Edit production source only, including new modules under these approved prefixes: " + ", ".join(PREFIXES[task["id"]]) + ". Do not remove existing files or add tests, startup hooks, or bypasses. This minimal Debian container has shell tools but no Python or git. No network is available. Complete the implementation, then describe changed production paths.\n\n" + instruction.decode()}]
        await bounded_session(transport, gateway, config, workspace, messages, (records, models, usage, requests), task["id"] + "/" + str(index), binding)
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
    patch_path.write_text(patch)
    save(directory / "messages.json", messages)
    save(directory / "tool-results.json", records)
    record = {"task": task["id"], "index": index, "frozen_split": "validation" if index < 2 else "held_out", "instruction_sha256": sha(instruction), "source_files": {p: sha(data) for p, data in originals.items()}, "model_requested": MODEL, "models_served": sorted(set(models)), "usage": usage, "request_ids": requests, "tool_names": ["bash"], "tool_calls": len(records), "canary_sha256": sha(encoded(proof)), "binding": binding, **binding, "test_aware": False, "eligible_for_execution": eligible, "patch": str(patch_path) if eligible else None, "quarantined_patch": str(patch_path) if not eligible else None, "patch_sha256": sha(patch.encode()), "modified_paths": modified, "cleanup_ok": cleanup, "error": error, "time": now(), "workspace_limits": {"entries": MAX_ENTRIES, "file_bytes": MAX_FILE, "total_bytes": MAX_BYTES, "patch_bytes": MAX_PATCH, "hard_disk_quota": False}}
    save(directory / "receipt.json", record)
    print(json.dumps(record), flush=True)
    return record


async def main(args):
    binding = check_frozen(args.out)
    manifest = json.loads(args.manifest.read_text())
    inputs = validate_inputs(manifest)
    os.environ["OV1104_DOCKER_HOST"] = "unix:///Users/petermakhnatch/.docker/run/docker.sock"
    transport = load_transport()
    gateway = Gateway(args.out)
    try:
        await gateway.start()
        if args.canary_receipt:
            proof = json.loads(args.canary_receipt.read_text())
            if proof.get("binding") != binding or proof.get("passed") is not True or proof.get("cleanup_ok") is not True or proof.get("error") is not None:
                raise RuntimeError("existing canary does not match exact frozen launch")
            save(args.out / "accepted-canary.json", {"receipt_path": str(args.canary_receipt), "receipt_sha256": sha(args.canary_receipt.read_bytes()), "receipt": proof})
        else:
            proof = await canary(transport, gateway, args.out, binding)
        if proof.get("passed") is not True or proof.get("cleanup_ok") is not True:
            raise RuntimeError("canary failed or cleanup unconfirmed; writers forbidden")
        if args.canary_only:
            return
        semaphore = asyncio.Semaphore(2)
        async def run(task, index):
            async with semaphore:
                return await writer(transport, gateway, args.out, task, inputs[task["id"]], index, proof, binding)
        records = await asyncio.gather(*(run(task, i) for task in manifest["tasks"] for i in range(args.count)))
        save(args.out / "generation.json", records)
    finally:
        await gateway.close()


def cli():
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
    if Path(__file__).resolve() != args.out / "frozen/ov1106-goldcert/generate_blind.py" or args.manifest != args.out / "frozen/manifest.json":
        raise RuntimeError("internal launch requires the frozen driver and manifest")
    def cancelled(_signum, _frame):
        raise KeyboardInterrupt("generation cancelled")
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, cancelled)
    asyncio.run(main(args))


if __name__ == "__main__":
    cli()
