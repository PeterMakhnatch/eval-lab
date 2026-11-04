"""Run instruction-only Codex writers through the retained container-only transport.

The gateway is a credential boundary, not a model tool. Only the imported
container bash executor handles model tool requests. Raw messages stay private.
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import importlib.util
import json
import os
from datetime import UTC, datetime
from pathlib import Path

MODEL = "openai-codex/gpt-6.1-terra"
TRANSPORT = Path(__file__).resolve().parents[1] / "ov1104-impossible/container_tool.py"
TRANSPORT_SHA = "f2e8c41fd72e01fc0e0b75b0ef190cde3aa2e92c0321152f995fc5cf52a7d22a"
TOOLS = [{"type": "function", "function": {"name": "bash", "description": "Run a finite shell command inside your isolated source container. No host access or network. Edit source files using shell tools.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"], "additionalProperties": False}}}]
IMAGE = "sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_transport():
    if sha(TRANSPORT.read_bytes()) != TRANSPORT_SHA:
        raise RuntimeError("retained transport hash changed")
    spec = importlib.util.spec_from_file_location("goldcert_container", TRANSPORT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Gateway:
    async def start(self):
        self.process = await asyncio.create_subprocess_exec("omp", "auth-gateway", "stdio", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1 << 26)
        ready = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 120))
        if not ready.get("ready"):
            raise RuntimeError("gateway not ready")
        self.pending = {}
        self.counter = 0
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        while line := await self.process.stdout.readline():
            value = json.loads(line)
            future = self.pending.pop(str(value.get("id")), None)
            if future is not None:
                future.set_result(value)

    async def call(self, messages):
        self.counter += 1
        ident = str(self.counter)
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        body = {"model": MODEL, "messages": messages, "tools": TOOLS, "tool_choice": "auto", "max_tokens": 18000}
        self.process.stdin.write((json.dumps({"id": ident, "path": "/v1/chat/completions", "body": body}) + "\n").encode())
        await self.process.stdin.drain()
        response = await asyncio.wait_for(future, 900)
        if response.get("status") != 200 or response.get("body", {}).get("model") != MODEL:
            raise RuntimeError("gateway status/model mismatch: " + json.dumps(response)[:300])
        return response["body"]

    async def close(self):
        self.process.kill()
        await self.process.wait()
        self.reader.cancel()


def create(transport, directory: Path):
    workspace = directory / "workspace"
    workspace.mkdir(parents=True)
    (workspace / ".containment-canary").write_text("canary\n")
    config = transport.create_container(directory / "control", workspace, IMAGE, Path("/opt/homebrew/bin/docker"), sleep_s=3600, limits={"memory": "512m", "cpus": "1"})
    if transport.docker(config, "start", config["container_id"]).returncode:
        transport.cleanup(config)
        raise RuntimeError("container start failed")
    transport.inspect_container(config)
    return config, workspace


async def canary(transport, gateway, root: Path):
    directory = root / "canary"
    config, _ = create(transport, directory)
    commands = ["ls /Users", "cat ~/.zshrc", "ls /private/tmp/ov1104", "ls /private/tmp/ov1106", transport.POSITIVE]
    records = []
    messages = [{"role": "user", "content": "Use the bash tool for exactly these commands separately. Use no other commands. Then stop.\n" + "\n".join(commands)}]
    passed = False
    try:
        for _ in range(8):
            response = await gateway.call(messages)
            message = response["choices"][0]["message"]
            messages.append(message)
            calls = message.get("tool_calls") or []
            if not calls:
                break
            for call in calls:
                if call["function"]["name"] != "bash":
                    raise RuntimeError("unknown tool")
                params = json.loads(call["function"]["arguments"])
                if set(params) != {"command"} or params["command"] not in commands:
                    raise RuntimeError("unexpected canary command")
                result = await asyncio.to_thread(transport.execute, config, params)
                records.append(result)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)})
        observed = {r["command"]: r for r in records}
        passed = len(records) == len(commands) and set(observed) == set(commands)
        passed = passed and all(observed[c]["returncode"] not in (0, 125) for c in commands[:-1])
        passed = passed and observed[commands[-1]]["returncode"] == 0 and observed[commands[-1]]["output"].strip() == "/workspace"
        if not passed:
            raise RuntimeError("canary probes failed or missing")
    finally:
        cleaned = transport.cleanup(config)
        receipt = {"passed": passed and cleaned, "cleanup_ok": cleaned, "transport_sha256": TRANSPORT_SHA, "driver_sha256": sha(Path(__file__).read_bytes()), "image": IMAGE, "model": MODEL, "probes": records, "boundary": {"network": "none", "host_mounts": "only private public-source workspace", "read_only_root": True}, "time": datetime.now(UTC).isoformat()}
        (directory / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        (directory / "messages.json").write_text(json.dumps(messages, indent=2) + "\n")
    return receipt


async def writer(transport, gateway, root, task, index, canary_receipt):
    directory = root / "writers" / task["id"] / str(index)
    config, workspace = create(transport, directory)
    originals = {}
    for relative, source in task["sources"].items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or any(part.lower() in {"tests", "test", ".git"} for part in path.parts):
            raise ValueError("non-public source path")
        data = Path(source).read_bytes()
        target = workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        originals[relative] = data
    instruction = Path(task["instruction"]).read_text()
    (workspace / "instruction.md").write_text(instruction)
    messages = [{"role": "user", "content": "Implement this task in the public source repository at /workspace. Read instruction.md and the source files with bash. You see only pristine public source and the instruction, never tests, references, prior solutions, or grader feedback. Edit production source only. Do not add tests, startup hooks, or bypasses. This minimal Debian container has shell tools but no Python or git. Use shell commands to read and edit files. No network is available. Complete the implementation, then describe the changed production paths.\n\n" + instruction}]
    models, usage, calls = [], [], []
    error = None
    try:
        for _step in range(30):
            response = await gateway.call(messages)
            models.append(response["model"])
            usage.append(response.get("usage"))
            message = response["choices"][0]["message"]
            messages.append(message)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                break
            for call in tool_calls:
                if call["function"]["name"] != "bash":
                    raise RuntimeError("unknown writer tool")
                params = json.loads(call["function"]["arguments"])
                if set(params) != {"command"}:
                    raise RuntimeError("unknown command fields")
                result = await asyncio.to_thread(transport.execute, config, params)
                calls.append(result)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)})
        else:
            error = "writer step limit"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        cleanup = transport.cleanup(config)
    patch = ""
    modified = []
    for path in sorted(workspace.rglob("*")):
        if path.is_symlink():
            error = "symlink output refused"
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(workspace).as_posix()
        if relative in {"instruction.md", ".containment-canary"}:
            continue
        if any(part.lower() in {"tests", "test", ".git"} for part in path.relative_to(workspace).parts) or not relative.endswith((".py", ".js", ".html", ".css")):
            error = "non-production output refused: " + relative
            continue
        data = path.read_bytes()
        before = originals.get(relative, b"")
        if data == before:
            continue
        modified.append(relative)
        patch += "".join(difflib.unified_diff(before.decode().splitlines(keepends=True), data.decode().splitlines(keepends=True), fromfile="a/" + relative if relative in originals else "/dev/null", tofile="b/" + relative))
    for relative in originals:
        if not (workspace / relative).exists():
            error = "source deletion refused: " + relative
    (directory / "solution.patch").write_text(patch)
    (directory / "messages.json").write_text(json.dumps(messages, indent=2) + "\n")
    record = {"task": task["id"], "index": index, "frozen_split": "validation" if index < 2 else "held_out", "instruction_sha256": sha(instruction.encode()), "source_files": {p: sha(data) for p, data in originals.items()}, "model_requested": MODEL, "models_served": sorted(set(models)), "usage": usage, "tool_names": ["bash"], "tool_calls": len(calls), "canary_sha256": sha(json.dumps(canary_receipt, sort_keys=True).encode()), "driver_sha256": sha(Path(__file__).read_bytes()), "transport_sha256": TRANSPORT_SHA, "test_aware": False, "patch": str(directory / "solution.patch"), "patch_sha256": sha(patch.encode()), "modified_paths": modified, "cleanup_ok": cleanup, "error": error, "time": datetime.now(UTC).isoformat()}
    (directory / "receipt.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record), flush=True)
    return record


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--count", type=int, default=4)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    os.environ["OV1104_DOCKER_HOST"] = "unix:///Users/petermakhnatch/.docker/run/docker.sock"
    transport = load_transport()
    gateway = Gateway()
    await gateway.start()
    try:
        proof = await canary(transport, gateway, args.out)
        semaphore = asyncio.Semaphore(2)
        async def run(task, index):
            async with semaphore:
                return await writer(transport, gateway, args.out, task, index, proof)
        records = await asyncio.gather(*(run(task, i) for task in manifest["tasks"] for i in range(args.count)))
        (args.out / "generation.json").write_text(json.dumps(records, indent=2) + "\n")
    finally:
        await gateway.close()


if __name__ == "__main__":
    asyncio.run(main())
