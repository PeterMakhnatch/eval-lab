"""Materializer for action-memory-v1 Harbor task packages."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
DERIVED = REPO / "derived" / "harbor-tasks" / "action-memory"


def output_path(cell_id: str = "clean_baseline_4k", seed: int = 42) -> Path:
    contract_bytes = (ROOT / "benchmark_contract.json").read_bytes()
    digest = hashlib.sha256(contract_bytes).hexdigest()
    return DERIVED / digest / f"action-memory-{cell_id}-seed{seed}"


def reject_committed_corpora() -> None:
    tracked = [
        str(p)
        for p in ROOT.rglob("*")
        if p.is_file() and ("tasks" in p.parts or "derived" in p.parts)
    ]
    if tracked:
        raise AssertionError(f"Generated task corpus is tracked in repository: {tracked}")


def materialize(
    output_dir: Path | None = None,
    cell_id: str = "clean_baseline_4k",
    seed: int = 42,
    arm: str = "clean",
    dose_bytes: int = 4096,
    inversion_count: int = 1,
    padding_position: str | None = None,
    distractor_count: int = 4,
) -> dict[str, object]:
    from state import generate_scenario

    if output_dir is None:
        output_dir = output_path(cell_id, seed)

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    spec = generate_scenario(
        seed=seed,
        cell_id=cell_id,
        arm=arm,
        dose_bytes=dose_bytes,
        inversion_count=inversion_count,
        padding_position=padding_position,
        distractor_count=distractor_count,
    )

    environment = output_dir / "environment"
    solution = output_dir / "solution"
    tests = output_dir / "tests"
    verifier_dir = output_dir / "verifier"
    workbench = output_dir / "workbench" / "adversarial"
    task_state = output_dir / "task_state"
    evidence = output_dir / "evidence"

    for d in (environment, solution, tests, verifier_dir, workbench, task_state, evidence):
        d.mkdir(parents=True, exist_ok=True)

    # Write scenario manifest in task_state
    scenario_dict = asdict(spec)
    (task_state / "scenario.json").write_text(json.dumps(scenario_dict, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (environment / "scenario.json").write_text(json.dumps(scenario_dict, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (tests / "scenario.json").write_text(json.dumps(scenario_dict, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (verifier_dir / "scenario.json").write_text(json.dumps(scenario_dict, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Copy runtime, verifier, oracle
    shutil.copy2(ROOT / "runtime.py", environment / "runtime.py")
    shutil.copy2(ROOT / "oracle.py", solution / "solve.py")
    shutil.copy2(ROOT / "verifier.py", tests / "verify.py")
    shutil.copy2(ROOT / "verifier.py", verifier_dir / "verify.py")

    # Write Dockerfile and entrypoints
    (environment / "Dockerfile").write_text(
        "FROM python:3.12-slim\nWORKDIR /app\nCOPY . /app\nENTRYPOINT [\"/app/entrypoint.sh\"]\n",
        encoding="utf-8",
    )
    (environment / "entrypoint.sh").write_text(
        "#!/bin/sh\nset -eu\npython3 /app/runtime.py --task-dir /app/scenario.json --evidence-dir /app/evidence &\nif [ \"$#\" -gt 0 ]; then exec \"$@\"; fi\nexec sleep infinity\n",
        encoding="utf-8",
    )
    (environment / "entrypoint.sh").chmod(0o755)

    (solution / "solve.sh").write_text(
        "#!/bin/sh\nset -eu\nexec python3 /app/solution/solve.py --task-dir /app/task_state --evidence-dir /app/evidence\n",
        encoding="utf-8",
    )
    (solution / "solve.sh").chmod(0o755)

    (tests / "Dockerfile").write_text(
        "FROM python:3.12-slim\nWORKDIR /tests\nCOPY . /tests\nCMD [\"sleep\", \"infinity\"]\n",
        encoding="utf-8",
    )
    (tests / "test.sh").write_text(
        "#!/bin/sh\nset -eu\nmkdir -p /logs/verifier\npython3 /tests/verify.py --task-dir /tests --evidence-dir /app/evidence --reward-dir /logs/verifier || true\nif [ ! -f /logs/verifier/reward.txt ]; then\n  printf '0.0\\n' > /logs/verifier/reward.txt\nfi\n",
        encoding="utf-8",
    )
    (tests / "test.sh").chmod(0o755)

    # Verifier Dockerfile and test.sh
    (verifier_dir / "Dockerfile").write_text(
        "FROM python:3.12-slim\nWORKDIR /verifier\nCOPY . /verifier\nCMD [\"sleep\", \"infinity\"]\n",
        encoding="utf-8",
    )
    (verifier_dir / "test.sh").write_text(
        "#!/bin/sh\nset -eu\nmkdir -p /logs/verifier\npython3 /verifier/verify.py --task-dir /verifier --evidence-dir /app/evidence --reward-dir /logs/verifier\n",
        encoding="utf-8",
    )
    (verifier_dir / "test.sh").chmod(0o755)

    # Workbench adversarial scripts
    (workbench / "stale-value.sh").write_text(
        "#!/bin/sh\nset -eu\nmkdir -p /app/evidence\npython3 -c '\nimport json\ns = json.load(open(\"/app/task_state/scenario.json\"))\njson.dump({\"status\": \"executed\", \"target_entity\": s[\"target_entity\"], \"target_attribute\": s[\"target_attribute\"], \"bound_value\": s[\"initial_value\"]}, open(\"/app/evidence/final-state.json\", \"w\"))\n'\n",
        encoding="utf-8",
    )
    (workbench / "stale-value.sh").chmod(0o755)

    (workbench / "wrong-entity.sh").write_text(
        "#!/bin/sh\nset -eu\nmkdir -p /app/evidence\npython3 -c '\nimport json\ns = json.load(open(\"/app/task_state/scenario.json\"))\njson.dump({\"status\": \"executed\", \"target_entity\": \"entity_invalid\", \"target_attribute\": s[\"target_attribute\"], \"bound_value\": s[\"latest_value\"]}, open(\"/app/evidence/final-state.json\", \"w\"))\n'\n",
        encoding="utf-8",
    )
    (workbench / "wrong-entity.sh").chmod(0o755)

    (workbench / "empty-output.sh").write_text(
        "#!/bin/sh\nset -eu\nmkdir -p /app/evidence\nexit 0\n",
        encoding="utf-8",
    )
    (workbench / "empty-output.sh").chmod(0o755)

    # Harbor task.toml & instruction.md
    task_toml_content = f'''schema_version = "1.4"
artifacts = ["/app/evidence/benchmark-events.jsonl", "/app/evidence/final-state.json"]

[task]
version = "1.0.0"
name = "action-memory/{cell_id}-seed{seed}"
description = "Actionable memory and dynamic state inversion benchmark task."

[[task.authors]]
name = "Eval Lab"
email = "eval-lab@example.invalid"

[metadata]
category = "synthetic-memory"
tags = ["action-memory", "mcp", "context-growth", "state-inversion"]
construct_name = "actionable_entity_memory"
cell_id = "{cell_id}"
arm = "{arm}"
seed = {seed}
dose_bytes = {spec.dose_bytes}
inversion_count = {inversion_count}
license = "Apache-2.0"

[agent]
timeout_sec = 600.0

[verifier]
timeout_sec = 120.0
environment_mode = "separate"

[verifier.environment]
network_mode = "no-network"

[environment]
network_mode = "public"
build_timeout_sec = 120.0
os = "linux"
cpus = 1
memory_mb = 512
storage_mb = 1024
'''
    (output_dir / "task.toml").write_text(task_toml_content, encoding="utf-8")

    instruction_md_content = f"""# Action Memory Task: Configuration State Mutation

You are operating an agent workflow against a stateful streamable HTTP MCP server running on port 8080.

## Objective
1. Inspect the system context logs and state updates for target entity `{spec.target_entity}`.
2. Note that the entity's `{spec.target_attribute}` property has received initialization and potential critical state inversion overrides.
3. Call `execute_mutation` tool with `{spec.target_entity}`, `{spec.target_attribute}`, and the strictly latest active value token.
4. Record all event traces into `/app/evidence/benchmark-events.jsonl` and ensure `/app/evidence/final-state.json` reflects the executed mutation.
"""
    (output_dir / "instruction.md").write_text(instruction_md_content, encoding="utf-8")

    return {
        "output_path": str(output_dir),
        "target_entity": spec.target_entity,
        "latest_value": spec.latest_value,
        "initial_value": spec.initial_value,
        "dose_bytes": spec.dose_bytes,
        "arm": arm,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--cell-id", type=str, default="clean_baseline_4k")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    res = materialize(output_dir=args.output_dir, cell_id=args.cell_id, seed=args.seed)
    print(json.dumps(res, indent=2))
