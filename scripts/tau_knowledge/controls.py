#!/usr/bin/env python3
"""Run Tau oracle, no-op, and deliberate-mutant controls through Harbor."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

COMMANDS = {
    "oracle": ("uv", "run", "harbor", "trial", "start", "-p", "{task_path}", "-a", "oracle"),
    "nop": ("uv", "run", "harbor", "trial", "start", "-p", "{task_path}", "-a", "nop"),
}
EXPECTED_REWARDS = {"oracle": 1.0, "nop": 0.0, "mutant": 0.0}


def _mutant_copy(task: Path, temp: Path) -> Path:
    mutant = temp / task.name
    shutil.copytree(task, mutant)
    solution = mutant / "solution" / "solve.sh"
    solution.write_text(
        "#!/bin/sh\n# Deliberate mutant: no action is submitted.\nexit 0\n",
        encoding="utf-8",
    )
    solution.chmod(0o755)
    return mutant


def _persisted_reward(trials_dir: Path) -> float:
    results = sorted(trials_dir.rglob("result.json"))
    for path in results:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if "verifier_result" in payload:
            return float(payload["verifier_result"]["rewards"]["reward"])
        if "reward" in payload:
            return float(payload["reward"])
    if not results:
        raise RuntimeError(f"Harbor produced no persisted result.json under {trials_dir}")
    raise RuntimeError(
        f"Harbor result files contain no persisted reward under {trials_dir}"
    )


def run_control(
    task: Path,
    mode: str,
    *,
    dry_run: bool = False,
    trials_dir: Path | None = None,
    expected_reward: float | None = None,
) -> list[str]:
    if not task.is_dir() or not (task / "task.toml").is_file():
        raise RuntimeError(f"incomplete materialized task: {task}")
    with tempfile.TemporaryDirectory(prefix="tau-mutant-") as scratch:
        target = _mutant_copy(task, Path(scratch)) if mode == "mutant" else task
        agent_mode = "oracle" if mode == "mutant" else mode
        command = [item.replace("{task_path}", str(target)) for item in COMMANDS[agent_mode]]
        if trials_dir is not None:
            trials_dir.mkdir(parents=True, exist_ok=True)
            command.extend(["--trials-dir", str(trials_dir)])
        if not dry_run:
            subprocess.run(command, check=True, cwd=os.environ.get("HARBOR_ROOT") or None)
            if trials_dir is not None:
                actual = _persisted_reward(trials_dir)
                expected = EXPECTED_REWARDS[mode] if expected_reward is None else expected_reward
                if actual != expected:
                    raise RuntimeError(
                        f"{mode} control reward mismatch: expected {expected}, got {actual}"
                    )
        return command


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--mode", choices=["oracle", "nop", "mutant"], required=True)
    parser.add_argument("--trials-dir", type=Path, default=None)
    parser.add_argument("--expect-reward", type=float, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(
        " ".join(
            run_control(
                args.task.resolve(),
                args.mode,
                dry_run=args.dry_run,
                trials_dir=args.trials_dir,
                expected_reward=args.expect_reward,
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
