"""Harbor package assembly helpers for Action Memory FastMCP sidecars."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from evallab.mcp_substrate import (
    DEFAULT_MCP_PORT,
    DEFAULT_SIDECAR_SERVICE,
    SubstrateError,
)

ROOT = Path(__file__).resolve().parent
SIDECAR_DIRNAME = "mcp-server"


def embed_client() -> str:
    text = (ROOT / "client.py").read_text(encoding="utf-8")
    if text.startswith('"""'):
        closer = text.find('"""', 3)
        if closer != -1:
            text = text[closer + 3 :].lstrip("\n")
    return text


def oracle_solve_py() -> str:
    return (
        "#!/usr/bin/env python3\n"
        + embed_client()
        + "\nimport re\nimport time\n\n"
        + "def wait_ready(session):\n"
        + "    for _ in range(50):\n"
        + "        status, _raw = session.initialize()\n"
        + "        if status == 200:\n"
        + "            return\n"
        + "        time.sleep(0.1)\n"
        + "    raise RuntimeError('mcp sidecar did not become ready')\n\n"
        + "def main():\n"
        + "    session = McpHttpSession()\n"
        + "    wait_ready(session)\n"
        + "    listed = session.call_tool('list_context_chunks', {})\n"
        + "    if not isinstance(listed, dict) or 'chunk_ids' not in listed:\n"
        + "        raise RuntimeError('list_context_chunks returned %r' % (listed,))\n"
        + "    target_entity = None\n"
        + "    latest_value = None\n"
        + "    target_attribute = 'routing_key'\n"
        + "    for chunk_id in listed['chunk_ids']:\n"
        + "        chunk = session.call_tool('get_context_chunk', {'chunk_id': chunk_id})\n"
        + "        text = json.dumps(chunk) if isinstance(chunk, dict) else str(chunk)\n"
        + "        if target_entity is None:\n"
        + "            match = re.search(r'for\\s+(entity_\\d+)', text)\n"
        + "            if match:\n"
        + "                target_entity = match.group(1)\n"
        + '        if target_entity and re.search(rf"\\b{re.escape(target_entity)}\\b", text):\n'
        + "            match_value = re.search(r\"'(?P<val>[^']+)'\", text)\n"
        + "            if match_value:\n"
        + "                latest_value = match_value.group('val')\n"
        + "    if not target_entity or not latest_value:\n"
        + "        raise RuntimeError('could not resolve latest bound value')\n"
        + "    session.call_tool('execute_mutation', {'entity_id': target_entity, 'attribute': target_attribute, 'bound_value': latest_value})\n\n"
        + "if __name__ == '__main__':\n"
        + "    main()\n"
    )


def python_mcp_snippet(tool: str, arguments: dict[str, str]) -> str:
    args_literal = json.dumps(arguments)
    return (
        "python3 - <<'PY'\n"
        + embed_client()
        + "\nsession = McpHttpSession()\n"
        + "session.initialize()\n"
        + f"session.call_tool({tool!r}, {args_literal})\n"
        + "PY\n"
    )


def write_environment_build_proof(env_dir: Path, sidecar_dir: Path) -> None:
    lockfile = sidecar_dir / "requirements.txt"
    wheelhouse = sidecar_dir / "wheelhouse"
    if not lockfile.is_file() or not wheelhouse.is_dir():
        return
    wheels = sorted(wheelhouse.glob("*.whl"))
    if not wheels:
        return
    pinned = []
    for wheel in wheels:
        stem_parts = wheel.name[:-4].split("-")
        pinned.append(
            {
                "name": stem_parts[0].replace("_", "-"),
                "version": stem_parts[1] if len(stem_parts) > 1 else "0",
                "wheel": wheel.name,
            }
        )
    proof = {
        "kind": "offline_build_proof",
        "ecosystem": "pip",
        "lockfile": f"{SIDECAR_DIRNAME}/requirements.txt",
        "lockfile_digest": "sha256:" + hashlib.sha256(lockfile.read_bytes()).hexdigest(),
        "pinned_dependencies": pinned,
        "reviewed_by": "eval-lab-action-memory-v1",
    }
    (env_dir / "offline-build-proof.json").write_text(
        json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def task_toml(
    safe_cell: str,
    seed: int,
    arm: str,
    spec: object,
    inversion_count: int,
    extra_metadata: dict[str, object],
) -> str:
    return f'''schema_version = "1.4"
artifacts = ["/app/output/benchmark-events.jsonl", "/app/output/final-state.json"]

[task]
version = "1.0.0"
name = "evallab/action-memory-{safe_cell}-seed{seed}"
description = "Actionable memory and dynamic state inversion benchmark task with streamable HTTP MCP server"
keywords = ["action-memory", "mcp", "context-growth", "state-inversion", "synthetic"]

[[task.authors]]
name = "Eval Lab"
email = "eval-lab@example.invalid"

[metadata]
difficulty = "easy"
category = "synthetic"
tags = ["action-memory", "mcp", "context-growth", "state-inversion"]
construct_name = "actionable_entity_memory"
cell_id = "{safe_cell}"
arm = "{arm}"
seed = {seed}
dose_bytes = {spec.dose_bytes}
inversion_count = {inversion_count}
license = "Apache-2.0"
dose_axis_version = "{extra_metadata.get("dose_axis_version", "")}"
handle_axis_version = "{extra_metadata.get("handle_axis_version", "")}"
base_task_pair_id = "{extra_metadata.get("base_task_pair_id", "")}"
declared_delta = "{extra_metadata.get("declared_delta", "")}"
representation = "{extra_metadata.get("representation", "")}"
representation_digest = "{extra_metadata.get("representation_digest", "")}"

[agent]
timeout_sec = 600.0

[verifier]
timeout_sec = 120.0
environment_mode = "separate"

[verifier.environment]
network_mode = "no-network"

[[verifier.collect]]
service = "main"
command = "if [ -f /app/output/benchmark-events.jsonl ]; then cp -f /app/output/benchmark-events.jsonl /app/output/benchmark-events.jsonl; fi"

[[verifier.collect]]
service = "main"
command = "if [ -f /app/output/final-state.json ]; then cp -f /app/output/final-state.json /app/output/final-state.json; fi"

[environment]
network_mode = "no-network"
build_timeout_sec = 120.0
os = "linux"
cpus = 1
memory_mb = 512
storage_mb = 1024

[[environment.mcp_servers]]
name = "memory_mcp"
transport = "streamable-http"
url = "http://{DEFAULT_SIDECAR_SERVICE}:{DEFAULT_MCP_PORT}/mcp"
'''


def instruction_md(_spec: object) -> str:
    """Render the agent instruction without private truth or output-location hints."""
    return f"""# Action Memory Task: Configuration State Mutation

You are operating against a stateful streamable HTTP MCP server at `http://{DEFAULT_SIDECAR_SERVICE}:{DEFAULT_MCP_PORT}/mcp`.

## Objective
1. List the available context handles.
2. Read every listed context record and derive the current state from those records.
3. Invoke `execute_mutation` only with arguments derived from the retrieved context.
"""


def fair_alternative_mcp_snippet() -> str:
    """Return a self-contained fair workbench control using the public MCP path."""
    return (
        "python3 - <<PY\n"
        + embed_client()
        + "\nimport re\nimport time\n"
        + "session = McpHttpSession()\n"
        + "for _ in range(50):\n"
        + "    if session.initialize()[0] == 200:\n"
        + "        break\n"
        + "    time.sleep(0.1)\n"
        + "else:\n"
        + "    raise RuntimeError(\"mcp sidecar did not become ready\")\n"
        + "listed = session.call_tool(\"list_context_chunks\", {})\n"
        + "entity = attribute = value = None\n"
        + "for chunk_id in listed[\"chunk_ids\"]:\n"
        + "    chunk = session.call_tool(\"get_context_chunk\", {\"chunk_id\": chunk_id})\n"
        + "    text = json.dumps(chunk)\n"
        + "    if entity is None:\n"
        + "        match = re.search(r\"for\\s+(entity_\\d+)\", text)\n"
        + "        if match: entity = match.group(1)\n"
        + "    if attribute is None:\n"
        + "        match = re.search(r\"attribute\\s+([A-Za-z_][A-Za-z0-9_]*)\\s+(?:is|now)\", text)\n"
        + "        if match: attribute = match.group(1)\n"
        + "    if entity and re.search(rf\"\\b{re.escape(entity)}\\b\", text):\n"
        + "        match = re.search(r\"\\x27([^\\x27]+)\\x27\", text)\n"
        + "        if match: value = match.group(1)\n"
        + "if not entity or not attribute or not value: raise RuntimeError(\"could not derive mutation from context\")\n"
        + "session.call_tool(\"execute_mutation\", {\"entity_id\": entity, \"attribute\": attribute, \"bound_value\": value})\n"
        + "PY\n"
    )
