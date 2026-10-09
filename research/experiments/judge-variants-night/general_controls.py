"""Start a real general workplace with host-verified downloads and offline wheels.

Only the download/install transport is substituted in the copied setup script;
MCP startup, permissions, G2's live SQLite snapshot and ready sentinel execute
inside the original cached image with network disabled. No model calls.
"""

from __future__ import annotations

import hashlib
import json
import subprocess

from build import GENERAL, HERE, REPO, SCRATCH, get_file

from evallab.task_variants import default_variants_root

TASK = "s3k_0067_accounting_audit_tax_en_t3_rl_007"
IMAGE = "docker.io/xiaomimimo/mimo-v2.6-rl-oss@sha256:7b59eff30fb4cef86cc6a4e1c278b119c06c7167807ba30983f0346bc2b5d02e"
CLIENT = '''import asyncio, json
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession
async def main():
    counts = {}
    for port in (39101, 39102, 39103, 39104):
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                counts[port] = len(tools.tools)
    print("MCP_TOOLS:" + json.dumps(counts))
asyncio.run(main())
'''


def main() -> None:
    selected = next(json.loads(line) for line in (HERE / "packages.jsonl").read_text().splitlines()
                    if json.loads(line)["task"] == TASK)
    digest = selected["package_digest"]
    package = default_variants_root(REPO) / ("mimo-v2.6-rl__" + TASK) / digest.removeprefix("sha256:")[:12]
    root = SCRATCH / "general-control"
    root.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((GENERAL / "tasks" / TASK / "environment/setup/files/fetch.json").read_text())
    for entry in manifest["files"]:
        get_file(manifest, entry, root / "payload" / entry["target"].lstrip("/"))
    original = (package / "environment/setup/setup.sh").read_text()
    fetch = 'python3 "$M/files/fetch.py" "$M/files/fetch.json"'
    pip = "pip install -q 'mcp==1.26.0'"
    if original.count(fetch) != 1 or original.count(pip) != 1:
        raise ValueError("unrecognized setup transport")
    adapted = original.replace(fetch, 'cp -a /payload/work/. /work/ && cp -a /payload/installed-agent/. /installed-agent/')
    adapted = adapted.replace(pip, "pip install --no-index --find-links /wheelhouse -q 'mcp==1.26.0'")
    adapted = adapted.replace('M=/var/lib/mimo', 'M=/var/lib/mimo\nmkdir -p "$M/files" /work /installed-agent\ncp /control/blocklist "$M/files/blocklist"')
    (root / "blocklist").write_bytes((package / "environment/setup/files/blocklist").read_bytes())
    (root / "setup.sh").write_text(adapted)
    (root / "client.py").write_text(CLIENT)
    command = "bash /control/setup.sh && /opt/openai-agents-venv/bin/python /control/client.py && python3 -c 'import sqlite3;from pathlib import Path; dbs=list(Path(\"/work/system\").glob(\"*/state.db\"));print(\"SNAPSHOTS:\",len(dbs));assert dbs;[sqlite3.connect(p.with_name(\"state.db.pinned_backup\")).execute(\"PRAGMA integrity_check\").fetchall() == [(\"ok\",)] or (_ for _ in ()).throw(RuntimeError(str(p))) for p in dbs]'"
    proc = subprocess.run([
        "docker", "run", "--rm", "--name", "mimo-judge-variants-general-control", "--network", "none",
        "--memory", "1536m", "--cpus", "2", "--entrypoint", "bash",
        "-v", f"{root}:/control", "-v", f"{root / 'payload'}:/payload:ro",
        "-v", f"{SCRATCH / 'wheelhouse'}:/wheelhouse:ro", IMAGE, "-c", command,
    ], capture_output=True, text=True, timeout=900)
    report = {"evidence_tier": "MEASURED", "task": TASK, "package_digest": digest,
              "command": "uv run python research/experiments/judge-variants-night/general_controls.py",
              "image": IMAGE, "network": "none", "containers": 1, "rm": True, "model_calls": 0,
              "download_transport_substituted": True, "mcp_wheel_version": "1.26.0",
              "wheel_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (SCRATCH / "wheelhouse").glob("*.whl")},
              "exit_code": proc.returncode, "stdout": proc.stdout[-6000:], "stderr": proc.stderr[-2000:]}
    (HERE / "general-control.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if proc.returncode:
        raise SystemExit(proc.returncode)


if __name__ == "__main__":
    main()
