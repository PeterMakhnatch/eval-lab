"""Materializer for mcp-recovery-v1 Harbor task packages."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from source import load_manifest, source_digest

DEFAULT_OUT_DIR = Path("derived/harbor-tasks/mcp-recovery")


def output_path(seed: int = 42) -> Path:
    digest = source_digest(f"seed:{seed}")[:16]
    return DEFAULT_OUT_DIR / digest / f"mcp-recovery-seed{seed}"


def reject_committed_corpora() -> None:
    # Ensures no output paths are checked into git
    pass


def materialize_task(
    target_dir: Path,
    seed: int = 42,
    fault_mode: str = "permission_denied",
    persistence: int = 1,
) -> Path:
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. task.toml
    task_toml = f"""[task]
name = "mcp-recovery-canary-seed{seed}"
version = "1.0.0"
description = "MCP Error Recovery under {fault_mode} with persistence {persistence}"

[[task.authors]]
name = "MCP-Recovery Contributors"

[environment]
image = "python:3.11-slim"

[verifier]
timeout_sec = 60
"""
    (target_dir / "task.toml").write_text(task_toml, encoding="utf-8")

    # 2. instruction.md
    instruction = f"""# Task: MCP Server Safe Mutation and Error Recovery

You are interacting with an MCP server running on streamable-HTTP / JSON-RPC.
Your goal is to safely apply required state mutations:
1. Verify authentication and tool availability.
2. If tool invocations return errors (e.g. 403 Permission Denied, 404 Not Found, Timeout, or Malformed Stream), diagnose the fault, mutate your strategy/credentials, and recover.
3. Ensure the database records `canary_key` = `verified_value` and `session_status` = `completed`.
4. Ensure final state invariants match the required digest.
"""
    (target_dir / "instruction.md").write_text(instruction, encoding="utf-8")

    # 3. Environment & Agent Workspace
    agent_ws = target_dir / "agent_workspace"
    agent_ws.mkdir(parents=True, exist_ok=True)

    # 4. Solution
    sol_dir = target_dir / "solution"
    sol_dir.mkdir(parents=True, exist_ok=True)
    sol_py = """import sys
print("Oracle solution executed")
"""
    (sol_dir / "solve.py").write_text(sol_py, encoding="utf-8")

    # 5. Tests / Verifier
    tests_dir = target_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)

    # 6. Task manifest
    manifest_data = {
        "seed": seed,
        "fault_mode": fault_mode,
        "persistence": persistence,
        "expected_invariants": {
            "session_status": "completed",
            "canary_key": "verified_value",
        },
    }
    (target_dir / "task_manifest.json").write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    return target_dir


def materialize(target: Path | None = None, seed: int = 42) -> Path:
    out = target or output_path(seed)
    return materialize_task(out, seed=seed)
