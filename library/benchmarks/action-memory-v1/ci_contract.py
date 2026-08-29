#!/usr/bin/env python3
"""CI contract for action-memory-v1 benchmark family."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from materializer import materialize, output_path, reject_committed_corpora
from action_memory_templates import mutants, nop, oracle
from verifier import verify


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def assert_harbor_reward(job_dir: Path, expected: str) -> None:
    rewards = sorted(job_dir.rglob("reward.txt"))
    if not rewards:
        raise AssertionError(f"Harbor job has no persisted reward.txt: {job_dir}")
    values = {path.read_text(encoding="utf-8").strip() for path in rewards}
    if values != {expected}:
        raise AssertionError(f"Harbor rewards {values} != {{{expected}}}: {rewards}")


def main() -> None:
    reject_committed_corpora()

    target = output_path("clean_baseline_4k", seed=42)
    materialize(target, cell_id="clean_baseline_4k", seed=42)
    first = snapshot(target)
    materialize(target, cell_id="clean_baseline_4k", seed=42)
    second = snapshot(target)
    if first != second:
        raise AssertionError("Deterministic canary regeneration mismatch")

    rewards = target / "tests" / "rewards"
    task_dir = target / "task_state"
    evidence_dir = target / "evidence"

    # Test NOP control
    nop(task_dir, evidence_dir)
    nop_res = verify(task_dir, evidence_dir, reward_dir=rewards / "nop")
    if nop_res["reward"] != 0.0:
        raise AssertionError(f"NOP candidate received reward {nop_res['reward']} != 0.0")

    # Test Oracle control
    materialize(target, cell_id="clean_baseline_4k", seed=42)
    oracle(task_dir, evidence_dir)
    oracle_res = verify(task_dir, evidence_dir, reward_dir=rewards / "oracle")
    if oracle_res["reward"] != 1.0:
        raise AssertionError(f"Oracle candidate received reward {oracle_res['reward']} != 1.0")

    # Test Mutants
    for mutant_name, mutant_fn in mutants().items():
        materialize(target, cell_id="clean_baseline_4k", seed=42)
        mutant_fn(task_dir, evidence_dir)
        mutant_res = verify(task_dir, evidence_dir, reward_dir=rewards / mutant_name)
        if mutant_res["reward"] != 0.0:
            raise AssertionError(f"Mutant {mutant_name} accepted with reward {mutant_res['reward']} != 0.0")

    # Verify contract JSON exists and is valid
    contract_file = HERE / "benchmark_contract.json"
    if not contract_file.exists():
        raise AssertionError("Missing benchmark_contract.json")
    contract = json.loads(contract_file.read_text(encoding="utf-8"))
    if contract.get("benchmark_family") != "action-memory-v1":
        raise AssertionError(f"Unexpected contract family: {contract.get('benchmark_family')}")

    print("action-memory-v1 CI contract passed: deterministic canary, oracle, NOP, mutants, contract verified")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--assert-harbor-reward":
        assert_harbor_reward(Path(sys.argv[2]), sys.argv[3])
    else:
        main()
