#!/usr/bin/env python3
"""All-cell deterministic C3 controls without planted verifier invariants."""
from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))

from contract import CAMPAIGN0_FAULTS, CAMPAIGN0_PERSISTENCE
from materializer import materialize, output_path
from templates import mutants, run_blind_retry_control, run_nop_baseline, run_oracle_repair, run_wrong_repair_mutant
from verifier import verify_harbor_task


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def assert_reward(task: Path, expected: float, control: str) -> None:
    result = verify_harbor_task(task, reward_dir=task / "tests" / "rewards" / control)
    if result["reward"] != expected:
        raise AssertionError(f"{task.name} {control} scored {result['reward']} != {expected}: {result}")


def main() -> None:
    # 1. Corpus guard: ensure no generated tasks or vendors are tracked in git
    tracked = subprocess.check_output(
        ["git", "ls-files", "library/benchmarks/mcp-recovery-v1/tasks", "derived/harbor-tasks/mcp-recovery"],
        text=True,
    ).splitlines()
    if tracked:
        raise AssertionError(f"Generated task corpus is tracked in git: {tracked[:3]}")

    # 2. Deterministic regeneration check using explicit 0400 key
    with tempfile.NamedTemporaryFile(mode="wb", delete=False) as kf:
        kf.write(secrets.token_bytes(32))
        key_file_path = Path(kf.name)
    os.chmod(key_file_path, 0o400)

    try:
        for is_clean in (False, True):
            canary = output_path(seed=42, is_clean_twin=is_clean, evidence_key=key_file_path)
            materialize(canary, seed=42, is_clean_twin=is_clean, evidence_key=key_file_path)
            first = snapshot(canary)
            materialize(canary, seed=42, is_clean_twin=is_clean, evidence_key=key_file_path)
            if snapshot(canary) != first:
                raise AssertionError(f"Task materialization was non-deterministic (is_clean={is_clean})")

        # 3. Control evaluation across all 10 fault cells and 10 clean twin cells
        count = 0
        for fault in CAMPAIGN0_FAULTS:
            for persistence in CAMPAIGN0_PERSISTENCE:
                pair_key = secrets.token_bytes(32)
                for is_clean in (False, True):
                    task = output_path(seed=42, fault_mode=fault, persistence=persistence, is_clean_twin=is_clean, evidence_key=pair_key)
                    materialize(task, seed=42, fault_mode=fault, persistence=persistence, is_clean_twin=is_clean, evidence_key=pair_key)

                    # NOP baseline -> 0.0
                    run_nop_baseline(task, task / "agent_workspace")
                    assert_reward(task, 0.0, "nop")

                    # Oracle -> 1.0
                    run_oracle_repair(task, task / "agent_workspace")
                    assert_reward(task, 1.0, "oracle")

                    # Identical fixed-policy blind retry:
                    # On fault arm, must score 0.0 (auto-clear does not earn C3 recovery).
                    # On clean twin arm, establishes baseline 1.0 under identical policy.
                    expected_blind = 1.0 if is_clean else 0.0
                    run_blind_retry_control(task, task / "agent_workspace")
                    assert_reward(task, expected_blind, "blind_retry")

                    # All arm-specific adversarial mutants -> 0.0
                    for name, mutant in mutants(is_clean_twin=is_clean).items():
                        mutant(task, task / "agent_workspace")
                        assert_reward(task, 0.0, name)

                    count += 1
    finally:
        if key_file_path.exists():
            key_file_path.unlink()

    print(f"MCP Recovery v1 CI contract PASSED: {count} cells (10 fault + 10 clean twins), paired policy verified")


if __name__ == "__main__":
    main()
