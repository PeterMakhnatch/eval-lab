#!/usr/bin/env python3
"""CI contract: deterministic task generation, clean twin, repair oracle, NOP, and mutants."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from materializer import materialize, output_path
from templates import mutants, run_nop_baseline, run_oracle_repair
from verifier import verify_harbor_task


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def main() -> None:
    # 1. Corpus guard: ensure no generated tasks or vendors are tracked in git
    tracked = subprocess.check_output(
        ["git", "ls-files", "library/benchmarks/mcp-recovery-v1/tasks", "derived/harbor-tasks/mcp-recovery"],
        text=True,
    ).splitlines()
    if tracked:
        raise AssertionError(f"Generated task corpus is tracked in git: {tracked[:3]}")

    target = output_path(seed=42)
    # 2. Deterministic regeneration check
    materialize(target, seed=42)
    snap1 = snapshot(target)
    materialize(target, seed=42)
    snap2 = snapshot(target)
    if snap1 != snap2:
        raise AssertionError("Task materialization was non-deterministic")

    rewards_dir = target / "tests" / "rewards"

    # 3. NOP control must score 0.0
    materialize(target, seed=42)
    run_nop_baseline(target, target / "agent_workspace")
    nop_res = verify_harbor_task(target, reward_dir=rewards_dir / "nop")
    if nop_res["reward"] != 0.0:
        raise AssertionError(f"NOP baseline scored {nop_res['reward']} != 0.0")

    # 4. Oracle repair must score 1.0
    materialize(target, seed=42)
    run_oracle_repair(target, target / "agent_workspace")
    oracle_res = verify_harbor_task(target, reward_dir=rewards_dir / "oracle")
    if oracle_res["reward"] != 1.0:
        raise AssertionError(f"Oracle repair scored {oracle_res['reward']} != 1.0: {oracle_res}")

    # 5. Mutants (blind retry on permanent fault, wrong repair) must score 0.0
    for name, mutant_fn in mutants().items():
        materialize(target, seed=42)
        mutant_fn(target, target / "agent_workspace")
        mut_res = verify_harbor_task(target, reward_dir=rewards_dir / name)
        if mut_res["reward"] != 0.0:
            raise AssertionError(f"Mutant {name} scored {mut_res['reward']} != 0.0")

    print("MCP Recovery v1 CI contract PASSED: deterministic canary, oracle=1.0, NOP=0.0, mutants=0.0")


if __name__ == "__main__":
    main()
