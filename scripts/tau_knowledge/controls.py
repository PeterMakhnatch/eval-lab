#!/usr/bin/env python3
"""Run Tau oracle, no-op, and deliberate-mutant controls through Harbor."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

COMMANDS = {
    "oracle": ("uv", "run", "harbor", "trial", "start", "-p", "{task_path}", "-a", "oracle"),
    "nop": ("uv", "run", "harbor", "trial", "start", "-p", "{task_path}", "-a", "nop"),
}


def _mutant_copy(task: Path, temp: Path) -> Path:
    mutant = temp / task.name
    shutil.copytree(task, mutant)
    solution = mutant / "solution" / "solve.sh"
    solution.write_text("#!/bin/sh\n# Deliberate mutant: no action is submitted.\nexit 0\n", encoding="utf-8")
    solution.chmod(0o755)
    return mutant


def run_control(task: Path, mode: str, *, dry_run: bool = False) -> list[str]:
    if not task.is_dir() or not (task / "task.toml").is_file():
        raise RuntimeError(f"incomplete materialized task: {task}")
    with tempfile.TemporaryDirectory(prefix="tau-mutant-") as scratch:
        target = _mutant_copy(task, Path(scratch)) if mode == "mutant" else task
        agent_mode = "nop" if mode == "mutant" else mode
        command = [item.replace("{task_path}", str(target)) for item in COMMANDS[agent_mode]]
        if not dry_run:
            subprocess.run(command, check=True, cwd=os.environ.get("HARBOR_ROOT") or None)
        return command


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--mode", choices=["oracle", "nop", "mutant"], required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(" ".join(run_control(args.task.resolve(), args.mode, dry_run=args.dry_run)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
