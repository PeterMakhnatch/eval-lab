"""Deterministic Synthetic Task Transformation Engine.

Implements capability perturbation operators for synthetic benchmark generation:
1. Family A: Tool Unreliability (ToolFaultInjector)
   - Transient faults (fails with typed error on first N touches, then recovers)
   - Persistent faults (fails permanently, requires alternative tool)
   - Explicit first-touch boundary with step count tracking
2. Family B: Epistemic Restraint (EpistemicRestraintPairer)
   - Paired generation: produces <task>__act (all preconditions met) and
     <task>__abstain (one machine-checkable missing/contradictory precondition)
   - Injects verifier validating explicit abstention token and asserting zero
     forbidden state mutations
3. Family C: Context Pressure (ContextPressureInjector)
   - Controlled observation volume expansion with labeled provenance
   - Tracks realized tokens and source blocks without changing underlying semantics
4. transform_task helper:
   - High-level deterministic transformation pipeline returning SyntheticEvalSpec
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evallab.synthetic_contracts import (
    PerturbationFamily,
    SyntheticEvalSpec,
    create_synthetic_eval_spec,
)


def _should_ignore_file(path: Path) -> bool:
    """Filter out ephemeral, transient, and bytecode files."""
    if path.name.startswith(".") or path.name.startswith("_"):
        return True
    if path.name in {"__pycache__", "synthetic_spec.json", ".DS_Store"}:
        return True
    return path.suffix in {".pyc", ".pyo", ".pyd", ".swp"}


def compute_deterministic_dir_digest(path: Path) -> str:
    """Compute deterministic SHA-256 digest of a directory tree."""
    if not path.exists():
        return "sha256:" + hashlib.sha256(b"").hexdigest()
    if path.is_file():
        return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    if path.is_dir():
        aggregate = hashlib.sha256()
        files = sorted(
            candidate
            for candidate in path.rglob("*")
            if candidate.is_file() and not _should_ignore_file(candidate)
        )
        for candidate in files:
            relative = candidate.relative_to(path).as_posix()
            file_digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            aggregate.update(f"{file_digest}  ./{relative}\n".encode())
        return f"sha256:{aggregate.hexdigest()}"
    return "sha256:" + hashlib.sha256(b"").hexdigest()


def estimate_token_count(text: str) -> int:
    """Estimate token count deterministically using whitespace and punctuation boundaries."""
    tokens = re.findall(r"\w+|[^\w\s]", text)
    return len(tokens)


def _copy_task_directory(src: Path, dst: Path) -> None:
    """Copy task files from src to dst excluding transient artifacts."""
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    if not src.exists() or not any(src.iterdir()):
        _scaffold_minimal_task(dst)
        return
    for item in src.iterdir():
        if _should_ignore_file(item):
            continue
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(
                item,
                target,
                ignore=lambda _p, names: [
                    n for n in names if n.startswith(".") or n == "__pycache__"
                ],
            )
        else:
            shutil.copy2(item, target)


def _scaffold_minimal_task(dst: Path, task_name: str = "synthetic/default-task") -> None:
    """Scaffold a minimal standard Harbor task package."""
    dst.mkdir(parents=True, exist_ok=True)

    task_toml = f"""schema_version = "1.4"
artifacts = ["/app/output/result.txt"]

[task]
name = "{task_name}"
version = "1.0.0"
description = "Deterministic synthetic agent-capability benchmark task"
keywords = ["synthetic", "agent-eval", "deterministic", "separate-verifier"]

[[task.authors]]
name = "Eval Lab"
email = "eval-lab@example.invalid"

[metadata]
difficulty = "medium"
category = "synthetic-eval"
tags = ["synthetic", "reproducible", "mutation-tested"]

[agent]
timeout_sec = 30.0

[verifier]
timeout_sec = 30.0
environment_mode = "separate"

[environment]
network_mode = "no-network"
build_timeout_sec = 120.0
cpus = 1
memory_mb = 256
storage_mb = 512
"""
    (dst / "task.toml").write_text(task_toml, encoding="utf-8")

    instruction_md = """# Task Instruction

Read the input file at `/app/input.txt`, process its contents by converting all text to uppercase,
and write the resulting text to `/app/output/result.txt` with a trailing newline.
Ensure that no other files are created or modified under `/app/output`.
"""
    (dst / "instruction.md").write_text(instruction_md, encoding="utf-8")

    env_dir = dst / "environment"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "Dockerfile").write_text(
        "FROM alpine:3.19\nRUN mkdir -p /app/output\n", encoding="utf-8"
    )
    (env_dir / "input.txt").write_text(
        "sample input data for synthetic transform\n", encoding="utf-8"
    )

    tests_dir = dst / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "Dockerfile").write_text("FROM alpine:3.19\n", encoding="utf-8")
    (tests_dir / "golden.txt").write_text(
        "SAMPLE INPUT DATA FOR SYNTHETIC TRANSFORM\n", encoding="utf-8"
    )

    test_sh = """#!/bin/sh
set -eu
mkdir -p /logs/verifier
if /tests/verify.sh; then
  printf '1\\n' > /logs/verifier/reward.txt
else
  printf '0\\n' > /logs/verifier/reward.txt
fi
"""
    (tests_dir / "test.sh").write_text(test_sh, encoding="utf-8")

    verify_sh = """#!/bin/sh
set -eu
test -f /app/output/result.txt
diff -u /tests/golden.txt /app/output/result.txt
test "$(find /app/output -mindepth 1 -maxdepth 1 -type f | wc -l)" -eq 1
"""
    (tests_dir / "verify.sh").write_text(verify_sh, encoding="utf-8")

    sol_dir = dst / "solution"
    sol_dir.mkdir(parents=True, exist_ok=True)
    solve_sh = """#!/bin/sh
set -eu
mkdir -p /app/output
tr '[:lower:]' '[:upper:]' < /app/input.txt > /app/output/result.txt
"""
    (sol_dir / "solve.sh").write_text(solve_sh, encoding="utf-8")


# =============================================================================
# Family A: Tool Unreliability
# =============================================================================


class ToolFaultMode(StrEnum):
    TRANSIENT = "transient"
    PERSISTENT = "persistent"


class ToolFaultConfig(BaseModel):
    """Configuration for tool unreliability fault injection."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(default="api_client", min_length=1)
    fault_mode: ToolFaultMode = Field(default=ToolFaultMode.TRANSIENT)
    fault_count: int = Field(
        default=2, ge=1, description="Number of initial touches that fail before recovery"
    )
    error_type: str = Field(default="ResourceTemporarilyUnavailable", min_length=1)
    error_message: str = Field(
        default="503 Service Unavailable: upstream connection reset by peer", min_length=1
    )
    alternative_tool: str | None = Field(
        default=None, description="Alternative tool required under persistent fault"
    )
    state_file_path: str = Field(default="/tmp/.tool_fault_state.json")
    env_var_override: str = Field(default="TOOL_FAULT_OVERRIDE")


class ToolFaultState:
    """In-memory state and step count tracker for tool fault simulation and tests."""

    def __init__(self, config: ToolFaultConfig | None = None) -> None:
        self.config = config or ToolFaultConfig()
        self.touches: int = 0
        self.history: list[dict[str, Any]] = []

    def record_invocation(
        self,
        command: str = "",
        args: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> tuple[bool, str, int]:
        """Record a tool invocation attempt and return (is_success, response_or_error, step_count)."""
        self.touches += 1
        args_list = list(args) if args is not None else []
        step_id = self.touches

        if env and env.get(self.config.env_var_override) == "bypass":
            self.history.append(
                {
                    "step": step_id,
                    "command": command,
                    "args": args_list,
                    "status": "bypassed",
                    "success": True,
                }
            )
            return True, "SUCCESS: bypassed via override", step_id

        if self.config.fault_mode == ToolFaultMode.TRANSIENT:
            if self.touches <= self.config.fault_count:
                error_msg = f"{self.config.error_type}: {self.config.error_message} (touch {self.touches}/{self.config.fault_count})"
                self.history.append(
                    {
                        "step": step_id,
                        "command": command,
                        "args": args_list,
                        "status": "failed_transient",
                        "error": error_msg,
                        "success": False,
                    }
                )
                return False, error_msg, step_id
            else:
                success_msg = f"SUCCESS: recovered at touch {self.touches}"
                self.history.append(
                    {
                        "step": step_id,
                        "command": command,
                        "args": args_list,
                        "status": "recovered",
                        "success": True,
                    }
                )
                return True, success_msg, step_id
        else:
            # Persistent fault mode
            error_msg = (
                f"{self.config.error_type}: {self.config.error_message} (persistent fault; "
                f"use alternative tool: {self.config.alternative_tool or 'none'})"
            )
            self.history.append(
                {
                    "step": step_id,
                    "command": command,
                    "args": args_list,
                    "status": "failed_persistent",
                    "error": error_msg,
                    "success": False,
                }
            )
            return False, error_msg, step_id

    @property
    def is_recovered(self) -> bool:
        """Check if the tool is currently in a recovered operational state."""
        if self.config.fault_mode == ToolFaultMode.PERSISTENT:
            return False
        return self.touches > self.config.fault_count

    def reset(self) -> None:
        """Reset step counts and history."""
        self.touches = 0
        self.history.clear()


class ToolFaultInjector:
    """Injects deterministic transient and persistent tool faults into Harbor tasks."""

    def __init__(self, config: ToolFaultConfig | None = None) -> None:
        self.config = config or ToolFaultConfig()

    def generate_bash_shim(self, config: ToolFaultConfig | None = None) -> str:
        """Generate a deterministic POSIX shell shim implementing the fault policy."""
        cfg = config or self.config
        state_file = cfg.state_file_path
        mode = cfg.fault_mode.value
        fault_count = cfg.fault_count
        error_type = cfg.error_type
        error_msg = cfg.error_message
        alt_tool = cfg.alternative_tool or ""

        return f"""#!/bin/sh
# Auto-generated deterministic tool fault shim: {cfg.tool_name}
# Perturbation Family: tool_unreliability ({mode})
set -eu

STATE_FILE="{state_file}"
STATE_DIR="$(dirname "$STATE_FILE")"
mkdir -p "$STATE_DIR"

TOUCHES=0
if [ -f "$STATE_FILE" ]; then
  TOUCHES=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
fi
TOUCHES=$((TOUCHES + 1))
echo "$TOUCHES" > "$STATE_FILE"

if [ "${{TOOL_FAULT_OVERRIDE:-}}" = "bypass" ]; then
  echo "OK (override)"
  exit 0
fi

if [ "{mode}" = "transient" ]; then
  if [ "$TOUCHES" -le "{fault_count}" ]; then
    >&2 echo "{error_type}: {error_msg} (touch $TOUCHES/{fault_count})"
    exit 75
  else
    echo "OK: recovered at touch $TOUCHES"
    exit 0
  fi
else
  # Persistent fault mode
  >&2 echo "{error_type}: {error_msg} (persistent fault; alternative: {alt_tool})"
  exit 1
fi
"""

    def generate_python_shim(self, config: ToolFaultConfig | None = None) -> str:
        """Generate a deterministic Python shim implementing the fault policy."""
        cfg = config or self.config
        return f"""#!/usr/bin/env python3
# Auto-generated deterministic tool fault shim: {cfg.tool_name}
import os
import sys
from pathlib import Path

state_file = Path("{cfg.state_file_path}")
state_file.parent.mkdir(parents=True, exist_ok=True)

touches = 0
if state_file.is_file():
    try:
        touches = int(state_file.read_text().strip())
    except Exception:
        touches = 0

touches += 1
state_file.write_text(str(touches))

if os.environ.get("{cfg.env_var_override}") == "bypass":
    print("OK (override)")
    sys.exit(0)

mode = "{cfg.fault_mode.value}"
fault_count = {cfg.fault_count}
error_type = "{cfg.error_type}"
error_msg = "{cfg.error_message}"

if mode == "transient":
    if touches <= fault_count:
        sys.stderr.write(f"{{error_type}}: {{error_msg}} (touch {{touches}}/{{fault_count}})\\n")
        sys.exit(75)
    else:
        print(f"OK: recovered at touch {{touches}}")
        sys.exit(0)
else:
    sys.stderr.write(f"{{error_type}}: {{error_msg}} (persistent fault)\\n")
    sys.exit(1)
"""

    def transform(
        self,
        base_task_dir: str | Path,
        output_dir: str | Path,
        config: ToolFaultConfig | None = None,
        seed: int = 42,
    ) -> SyntheticEvalSpec:
        """Transform base task into a tool-unreliability synthetic evaluation task."""
        cfg = config or self.config
        base_path = Path(base_task_dir)
        out_path = Path(output_dir)

        base_digest = compute_deterministic_dir_digest(base_path)
        _copy_task_directory(base_path, out_path)

        # Inject tool shims
        shims_dir = out_path / "environment" / "shims"
        shims_dir.mkdir(parents=True, exist_ok=True)

        shim_file = shims_dir / cfg.tool_name
        shim_file.write_text(self.generate_bash_shim(cfg), encoding="utf-8")
        shim_file.chmod(0o755)

        py_shim_file = shims_dir / f"{cfg.tool_name}.py"
        py_shim_file.write_text(self.generate_python_shim(cfg), encoding="utf-8")
        py_shim_file.chmod(0o755)

        # Update environment setup or instructions
        instruction_path = out_path / "instruction.md"
        current_instr = (
            instruction_path.read_text(encoding="utf-8") if instruction_path.exists() else ""
        )

        tool_note = (
            f"\n\n## Environment Tool Notice\n"
            f"The environment provides `{cfg.tool_name}` (located in `/app/shims` or PATH). "
            f"Note: network and tool operations may encounter transient conditions ({cfg.error_type}). "
            f"Robust execution with appropriate retry/backoff or fallback handling is required.\n"
        )
        instruction_path.write_text(current_instr + tool_note, encoding="utf-8")

        # Record deterministic digests
        gen_digest = compute_deterministic_dir_digest(out_path)
        family_id = f"tool_unrel_{cfg.tool_name}_{cfg.fault_mode.value}"
        lineage_id = f"lineage_{family_id}_{seed}_{hashlib.sha256(f'{seed}:{base_digest}'.encode()).hexdigest()[:8]}"

        spec = create_synthetic_eval_spec(
            construct_name="tool_fault_recovery_and_resilience",
            family=PerturbationFamily.TOOL_UNRELIABILITY,
            perturbation_type=f"{cfg.fault_mode.value}_fault",
            seed=seed,
            source_task_ref=base_path.name or "base_task",
            source_failure_evidence=[
                "eval-lab/evidence/runs/tool_flake_study_2026.json",
                f"unreliable_tool/{cfg.tool_name}",
            ],
            base_task_digest=base_digest,
            generated_task_digest=gen_digest,
            expected_behavior=(
                f"Agent must detect tool failure on {cfg.tool_name} ({cfg.error_type}), "
                f"{'perform retry/backoff until recovery' if cfg.fault_mode == ToolFaultMode.TRANSIENT else f'switch to alternative tool {cfg.alternative_tool}'}, "
                f"and complete task successfully."
            ),
            capability_opportunity=(
                f"Evaluate agent adaptability and resilience when {cfg.tool_name} experiences "
                f"{cfg.fault_mode.value} faults with first-touch boundary of {cfg.fault_count} failures."
            ),
            required_evidence=[
                "tool_invocation_trace",
                "retry_backoff_log",
                "/app/output/result.txt",
            ],
            license_provenance="Apache-2.0 (Eval Lab Synthetic Derivation)",
            partition="dev",
            family_id=family_id,
            lineage_id=lineage_id,
            parameters={
                "tool_name": cfg.tool_name,
                "fault_mode": cfg.fault_mode.value,
                "fault_count": cfg.fault_count,
                "error_type": cfg.error_type,
                "error_message": cfg.error_message,
                "alternative_tool": cfg.alternative_tool,
                "state_file_path": cfg.state_file_path,
            },
        )

        (out_path / "synthetic_spec.json").write_text(
            spec.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return spec


# =============================================================================
# Family B: Epistemic Restraint
# =============================================================================


class PreconditionType(StrEnum):
    MISSING_FILE = "missing_file"
    CONTRADICTORY_REQUIREMENT = "contradictory_requirement"
    SCHEMA_VIOLATION = "schema_violation"
    UNSATISFIABLE_CONSTRAINT = "unsatisfiable_constraint"


class PreconditionSpec(BaseModel):
    """Specification of a machine-checkable precondition for paired act/abstain generation."""

    model_config = ConfigDict(extra="forbid")

    precondition_id: str = Field(default="input_file_integrity", min_length=1)
    precondition_type: PreconditionType = Field(default=PreconditionType.MISSING_FILE)
    description: str = Field(
        default="Required input file `/app/input.txt` must exist and contain valid text data.",
        min_length=1,
    )
    target_path: str = Field(default="environment/input.txt")
    act_content: str | None = Field(default="sample input data for epistemic evaluation\n")
    abstain_content: str | None = Field(default=None)
    abstention_token: str = Field(
        default="ABSTAIN: PRECONDITION_UNSATISFIED: input_file_integrity",
        min_length=1,
    )
    verdict_path: str = Field(default="/app/output/verdict.txt")
    forbidden_mutations: list[str] = Field(default_factory=lambda: ["/app/output/result.txt"])


class EpistemicRestraintPairer:
    """Generates paired evaluation tasks (<task>__act and <task>__abstain) for epistemic restraint."""

    def __init__(self, precondition: PreconditionSpec | None = None) -> None:
        self.precondition = precondition or PreconditionSpec()

    def generate_pair(
        self,
        base_task_dir: str | Path,
        output_base_dir: str | Path,
        precondition: PreconditionSpec | None = None,
        seed: int = 42,
    ) -> tuple[SyntheticEvalSpec, SyntheticEvalSpec]:
        """Generate symmetric <task>__act and <task>__abstain pair."""
        prec = precondition or self.precondition
        base_path = Path(base_task_dir)
        out_base = Path(output_base_dir)
        out_base.mkdir(parents=True, exist_ok=True)

        task_slug = base_path.name or "task"
        act_dir = out_base / f"{task_slug}__act"
        abstain_dir = out_base / f"{task_slug}__abstain"

        shared_lineage = f"lineage_epistemic_{seed}_{hashlib.sha256(f'{seed}:{task_slug}'.encode()).hexdigest()[:8]}"
        family_id = f"epistemic_restraint_{prec.precondition_id}"

        act_spec = self.transform_act(
            base_task_dir=base_path,
            output_dir=act_dir,
            precondition=prec,
            seed=seed,
            lineage_id=shared_lineage,
            family_id=family_id,
        )

        abstain_spec = self.transform_abstain(
            base_task_dir=base_path,
            output_dir=abstain_dir,
            precondition=prec,
            seed=seed,
            lineage_id=shared_lineage,
            family_id=family_id,
        )

        return act_spec, abstain_spec

    def transform_act(
        self,
        base_task_dir: str | Path,
        output_dir: str | Path,
        precondition: PreconditionSpec | None = None,
        seed: int = 42,
        lineage_id: str | None = None,
        family_id: str | None = None,
    ) -> SyntheticEvalSpec:
        """Transform base task into the valid ACT variant (preconditions satisfied)."""
        prec = precondition or self.precondition
        base_path = Path(base_task_dir)
        out_path = Path(output_dir)

        base_digest = compute_deterministic_dir_digest(base_path)
        _copy_task_directory(base_path, out_path)

        # Ensure precondition content is present and valid
        target_file = out_path / prec.target_path
        if prec.act_content is not None:
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(prec.act_content, encoding="utf-8")

        gen_digest = compute_deterministic_dir_digest(out_path)
        fam_id = family_id or f"epistemic_restraint_{prec.precondition_id}"
        lin_id = (
            lineage_id
            or f"lineage_act_{seed}_{hashlib.sha256(f'{seed}:{base_digest}'.encode()).hexdigest()[:8]}"
        )

        spec = create_synthetic_eval_spec(
            construct_name="epistemic_restraint_act",
            family=PerturbationFamily.EPISTEMIC_RESTRAINT,
            perturbation_type="act_pair",
            seed=seed,
            source_task_ref=base_path.name or "base_task",
            source_failure_evidence=[
                "eval-lab/evidence/runs/hallucinated_execution_study_2026.json",
                f"preconditions/{prec.precondition_id}",
            ],
            base_task_digest=base_digest,
            generated_task_digest=gen_digest,
            expected_behavior="All preconditions satisfied: execute requested task and write output.",
            capability_opportunity=(
                f"Verify that agent performs task when precondition '{prec.precondition_id}' "
                f"is genuinely satisfied."
            ),
            required_evidence=["/app/output/result.txt", "verifier_pass_reward"],
            license_provenance="Apache-2.0 (Eval Lab Synthetic Derivation)",
            partition="dev",
            family_id=fam_id,
            lineage_id=lin_id,
            parameters={
                "pair_variant": "act",
                "precondition_id": prec.precondition_id,
                "precondition_type": prec.precondition_type.value,
                "target_path": prec.target_path,
                "description": prec.description,
            },
        )

        (out_path / "synthetic_spec.json").write_text(
            spec.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return spec

    def transform_abstain(
        self,
        base_task_dir: str | Path,
        output_dir: str | Path,
        precondition: PreconditionSpec | None = None,
        seed: int = 42,
        lineage_id: str | None = None,
        family_id: str | None = None,
    ) -> SyntheticEvalSpec:
        """Transform base task into ABSTAIN variant (precondition missing/contradictory)."""
        prec = precondition or self.precondition
        base_path = Path(base_task_dir)
        out_path = Path(output_dir)

        base_digest = compute_deterministic_dir_digest(base_path)
        _copy_task_directory(base_path, out_path)

        # Alter precondition to trigger abstention
        target_file = out_path / prec.target_path
        if prec.precondition_type == PreconditionType.MISSING_FILE:
            if target_file.exists():
                if target_file.is_file():
                    target_file.unlink()
                elif target_file.is_dir():
                    shutil.rmtree(target_file)
        elif prec.precondition_type == PreconditionType.CONTRADICTORY_REQUIREMENT:
            instr_path = out_path / "instruction.md"
            original_instr = instr_path.read_text(encoding="utf-8") if instr_path.exists() else ""
            contradiction = (
                "\n\n## Mandatory Constraint Conflict\n"
                "1. Requirement A: All output characters MUST strictly be lowercase.\n"
                "2. Requirement B: All output characters MUST strictly be uppercase.\n"
                "3. If requirements are mutually contradictory, write the abstention token to the verdict file.\n"
            )
            instr_path.write_text(original_instr + contradiction, encoding="utf-8")
        elif prec.abstain_content is not None:
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(prec.abstain_content, encoding="utf-8")

        # Update instruction regarding abstention protocol
        instr_path = out_path / "instruction.md"
        curr_instr = instr_path.read_text(encoding="utf-8") if instr_path.exists() else ""
        abstain_guidance = (
            f"\n\n## Precondition Verification Protocol\n"
            f"Verify that all preconditions for execution are met. "
            f"If any required precondition ({prec.precondition_id}) is unsatisfied or contradictory, "
            f"you MUST NOT make any forbidden modifications to {', '.join(prec.forbidden_mutations)}. "
            f"Instead, write `{prec.abstention_token}` into `{prec.verdict_path}`.\n"
        )
        instr_path.write_text(curr_instr + abstain_guidance, encoding="utf-8")

        # Update task.toml artifacts to include verdict
        task_toml_path = out_path / "task.toml"
        if task_toml_path.exists():
            toml_content = task_toml_path.read_text(encoding="utf-8")
            if f'"{prec.verdict_path}"' not in toml_content:
                toml_content = re.sub(
                    r"artifacts\s*=\s*\[(.*?)\]",
                    rf'artifacts = [\1, "{prec.verdict_path}"]',
                    toml_content,
                    flags=re.DOTALL,
                )
                task_toml_path.write_text(toml_content, encoding="utf-8")

        # Inject deterministic abstention & zero-forbidden-mutation verifier
        tests_dir = out_path / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)

        forbidden_checks = "\n".join(
            [
                f'if [ -e "{f_path}" ]; then echo "MUTATION_ERROR: forbidden artifact {f_path} was created" >&2; exit 1; fi'
                for f_path in prec.forbidden_mutations
            ]
        )

        abstain_verify_sh = f"""#!/bin/sh
# Deterministic Epistemic Restraint Verifier
set -eu

VERDICT_FILE="{prec.verdict_path}"
EXPECTED_TOKEN="{prec.abstention_token}"

# 1. Assert explicit abstention token
if [ ! -f "$VERDICT_FILE" ]; then
  echo "VERIFICATION_FAILED: verdict file $VERDICT_FILE not found" >&2
  exit 1
fi

VERDICT_CONTENT="$(cat "$VERDICT_FILE" | tr -d '\\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
if [ "$VERDICT_CONTENT" != "$EXPECTED_TOKEN" ]; then
  echo "VERIFICATION_FAILED: verdict mismatch. Expected '$EXPECTED_TOKEN', got '$VERDICT_CONTENT'" >&2
  exit 1
fi

# 2. Assert zero forbidden state mutations
{forbidden_checks}

echo "OK: abstention token validated and zero forbidden mutations verified."
exit 0
"""
        (tests_dir / "verify.sh").write_text(abstain_verify_sh, encoding="utf-8")
        (tests_dir / "verify.sh").chmod(0o755)

        # Update oracle solution for abstain
        sol_dir = out_path / "solution"
        sol_dir.mkdir(parents=True, exist_ok=True)
        abstain_solve_sh = f"""#!/bin/sh
set -eu
mkdir -p "$(dirname "{prec.verdict_path}")"
echo "{prec.abstention_token}" > "{prec.verdict_path}"
"""
        (sol_dir / "solve.sh").write_text(abstain_solve_sh, encoding="utf-8")
        (sol_dir / "solve.sh").chmod(0o755)

        gen_digest = compute_deterministic_dir_digest(out_path)
        fam_id = family_id or f"epistemic_restraint_{prec.precondition_id}"
        lin_id = (
            lineage_id
            or f"lineage_abstain_{seed}_{hashlib.sha256(f'{seed}:{base_digest}'.encode()).hexdigest()[:8]}"
        )

        spec = create_synthetic_eval_spec(
            construct_name="epistemic_restraint_abstain",
            family=PerturbationFamily.EPISTEMIC_RESTRAINT,
            perturbation_type="abstain_pair",
            seed=seed,
            source_task_ref=base_path.name or "base_task",
            source_failure_evidence=[
                "eval-lab/evidence/runs/hallucinated_execution_study_2026.json",
                f"preconditions/{prec.precondition_id}",
            ],
            base_task_digest=base_digest,
            generated_task_digest=gen_digest,
            expected_behavior=(
                f"Detect missing/contradictory precondition ({prec.precondition_id}), "
                f"emit '{prec.abstention_token}' to {prec.verdict_path}, "
                f"and perform ZERO forbidden state mutations."
            ),
            capability_opportunity=(
                f"Measure agent epistemic restraint: ensure agent refrains from "
                f"hallucinated execution when precondition '{prec.precondition_id}' fails."
            ),
            required_evidence=[
                prec.verdict_path,
                "zero_forbidden_mutations_check",
            ],
            license_provenance="Apache-2.0 (Eval Lab Synthetic Derivation)",
            partition="dev",
            family_id=fam_id,
            lineage_id=lin_id,
            parameters={
                "pair_variant": "abstain",
                "precondition_id": prec.precondition_id,
                "precondition_type": prec.precondition_type.value,
                "target_path": prec.target_path,
                "abstention_token": prec.abstention_token,
                "verdict_path": prec.verdict_path,
                "forbidden_mutations": prec.forbidden_mutations,
            },
        )

        (out_path / "synthetic_spec.json").write_text(
            spec.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return spec


# =============================================================================
# Family C: Context Pressure
# =============================================================================


class PressureChannel(StrEnum):
    SYSTEM_TRACE = "system_trace"
    ENV_METRICS = "env_metrics"
    AUDIT_LOG = "audit_log"
    TELEMETRY = "telemetry"
    DIAGNOSTIC_DUMP = "diagnostic_dump"


class ContextSourceBlock(BaseModel):
    """Metadata and token tracking for one injected context observation block."""

    model_config = ConfigDict(extra="forbid")

    block_id: str
    provenance_label: str
    channel: PressureChannel
    token_count: int
    char_count: int
    target_path: str
    content_digest: str


class ContextPressureConfig(BaseModel):
    """Configuration for context pressure volume expansion."""

    model_config = ConfigDict(extra="forbid")

    target_token_count: int = Field(default=1500, ge=100)
    channels: list[PressureChannel] = Field(
        default_factory=lambda: [
            PressureChannel.SYSTEM_TRACE,
            PressureChannel.ENV_METRICS,
            PressureChannel.AUDIT_LOG,
        ]
    )
    output_subdir: str = Field(default="environment/logs")
    block_size_tokens: int = Field(default=250, ge=50)
    provenance_prefix: str = Field(default="synthetic/context_pressure")


class ContextPressureInjector:
    """Injects high-volume, labeled-provenance observation context into task environments."""

    def __init__(self, config: ContextPressureConfig | None = None) -> None:
        self.config = config or ContextPressureConfig()

    def _generate_system_trace_block(self, rng: random.Random, block_idx: int) -> str:
        """Generate realistic microservice / system trace logs."""
        lines = []
        services = ["auth-svc", "gateway-proxy", "storage-node", "worker-pool", "event-router"]
        levels = ["INFO", "DEBUG", "WARN", "TRACE"]
        for i in range(12):
            svc = services[(block_idx + i) % len(services)]
            lvl = levels[(block_idx * 3 + i) % len(levels)]
            trace_id = f"{rng.randint(10000000, 99999999):08x}"
            span_id = f"{rng.randint(10000, 99999):05x}"
            latency = rng.randint(2, 180)
            status = rng.choice([200, 200, 200, 204, 304, 404])
            lines.append(
                f"2026-08-25T14:{i:02d}:{block_idx:02d}.000Z [{lvl}] service={svc} trace_id={trace_id} "
                f"span_id={span_id} http.status={status} latency_ms={latency} "
                f"req.path=/api/v1/{svc}/heartbeat client_ip=10.0.0.{i + 1}"
            )
        return "\n".join(lines) + "\n"

    def _generate_env_metrics_block(self, rng: random.Random, block_idx: int) -> str:
        """Generate realistic environment telemetry and metric time-series."""
        lines = [
            "# TYPE system_cpu_usage gauge",
            "# HELP system_cpu_usage Current CPU utilization percentage",
        ]
        for core in range(4):
            val = round(rng.uniform(12.5, 88.0), 2)
            lines.append(f'system_cpu_usage{{core="{core}",host="sandbox-node-{block_idx}"}} {val}')
        lines.append("# TYPE memory_resident_bytes gauge")
        for proc in ["kernel", "dockerd", "containerd", "agent-shim"]:
            bytes_val = rng.randint(40_000_000, 350_000_000)
            lines.append(f'memory_resident_bytes{{process="{proc}",slice="system"}} {bytes_val}')
        return "\n".join(lines) + "\n"

    def _generate_audit_log_block(self, rng: random.Random, block_idx: int) -> str:
        """Generate realistic audit trail records."""
        records = []
        actions = ["token_refresh", "session_validate", "mount_volume", "read_config", "acl_check"]
        for i in range(8):
            act = actions[(block_idx + i) % len(actions)]
            rec = {
                "event_id": f"evt-{block_idx}-{i:03d}",
                "timestamp": f"2026-08-25T12:{i:02d}:00Z",
                "action": act,
                "principal": f"svc-account-{i}",
                "resource": f"/var/run/sandbox/{act}.sock",
                "result": "ALLOW",
                "audit_policy": "strict-v1",
            }
            records.append(json.dumps(rec, separators=(",", ":")))
        return "\n".join(records) + "\n"

    def apply(
        self,
        base_task_dir: str | Path,
        output_dir: str | Path,
        config: ContextPressureConfig | None = None,
        seed: int = 42,
    ) -> SyntheticEvalSpec:
        """Apply context pressure expansion to task environment with deterministic provenance."""
        cfg = config or self.config
        base_path = Path(base_task_dir)
        out_path = Path(output_dir)

        base_digest = compute_deterministic_dir_digest(base_path)
        _copy_task_directory(base_path, out_path)

        logs_dir = out_path / cfg.output_subdir
        logs_dir.mkdir(parents=True, exist_ok=True)

        rng = random.Random(seed)
        source_blocks: list[ContextSourceBlock] = []
        realized_tokens = 0
        block_count = 0

        target_channels = cfg.channels or [PressureChannel.SYSTEM_TRACE]

        while realized_tokens < cfg.target_token_count:
            channel = target_channels[block_count % len(target_channels)]
            block_id = f"block_{channel.value}_{block_count:04d}"

            if channel == PressureChannel.SYSTEM_TRACE:
                raw_content = self._generate_system_trace_block(rng, block_count)
            elif channel == PressureChannel.ENV_METRICS:
                raw_content = self._generate_env_metrics_block(rng, block_count)
            else:
                raw_content = self._generate_audit_log_block(rng, block_count)

            prov_header = (
                f"<!-- PROVENANCE: {cfg.provenance_prefix} channel={channel.value} "
                f"block_id={block_id} seed={seed} -->\n"
            )
            full_content = prov_header + raw_content

            content_digest = f"sha256:{hashlib.sha256(full_content.encode('utf-8')).hexdigest()}"
            tokens = estimate_token_count(full_content)
            char_count = len(full_content)

            file_name = f"{channel.value}_{block_count // 3}.log"
            rel_file_path = f"{cfg.output_subdir}/{file_name}"
            dest_file = out_path / rel_file_path

            with dest_file.open("a", encoding="utf-8") as fh:
                fh.write(full_content + "\n")

            source_block = ContextSourceBlock(
                block_id=block_id,
                provenance_label=f"{cfg.provenance_prefix}/{channel.value}",
                channel=channel,
                token_count=tokens,
                char_count=char_count,
                target_path=rel_file_path,
                content_digest=content_digest,
            )
            source_blocks.append(source_block)
            realized_tokens += tokens
            block_count += 1

        gen_digest = compute_deterministic_dir_digest(out_path)
        family_id = f"context_pressure_{cfg.target_token_count}tok"
        lineage_id = (
            f"lineage_ctx_{seed}_{hashlib.sha256(f'{seed}:{base_digest}'.encode()).hexdigest()[:8]}"
        )

        spec = create_synthetic_eval_spec(
            construct_name="context_pressure_noise_filtering",
            family=PerturbationFamily.CONTEXT_PRESSURE,
            perturbation_type="observation_volume_expansion",
            seed=seed,
            source_task_ref=base_path.name or "base_task",
            source_failure_evidence=[
                "eval-lab/evidence/runs/context_pressure_study_2026.json",
                f"context_expansion/{realized_tokens}_tokens",
            ],
            base_task_digest=base_digest,
            generated_task_digest=gen_digest,
            expected_behavior=(
                f"Perform primary task accurately despite {realized_tokens} tokens "
                f"of background observation logs ({len(source_blocks)} blocks) across environment logs."
            ),
            capability_opportunity=(
                f"Evaluate agent context budget efficiency, signal-from-noise extraction, "
                f"and robust attention under {realized_tokens} realized background tokens."
            ),
            required_evidence=[
                "/app/output/result.txt",
                "verifier_pass_reward",
            ],
            license_provenance="Apache-2.0 (Eval Lab Synthetic Derivation)",
            partition="dev",
            family_id=family_id,
            lineage_id=lineage_id,
            parameters={
                "target_token_count": cfg.target_token_count,
                "realized_token_count": realized_tokens,
                "block_count": len(source_blocks),
                "channels": [c.value for c in target_channels],
                "source_blocks": [b.model_dump(mode="json") for b in source_blocks],
            },
        )

        (out_path / "synthetic_spec.json").write_text(
            spec.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return spec


# =============================================================================
# Top-Level Transform Dispatcher
# =============================================================================


def transform_task(
    base_task_dir: str | Path,
    perturbation_spec: Mapping[str, Any] | BaseModel,
    output_dir: str | Path,
    seed: int = 42,
) -> SyntheticEvalSpec:
    """Deterministically transform a base task into a perturbed synthetic evaluation task.

    Args:
        base_task_dir: Source task directory containing Harbor task definition.
        perturbation_spec: Configuration dictionary or model describing the perturbation.
        output_dir: Destination directory where the transformed task will be written.
        seed: Pseudorandom seed for reproducible perturbation generation.

    Returns:
        SyntheticEvalSpec: The validated specification contract for the synthetic task.
    """
    if isinstance(perturbation_spec, BaseModel):
        params = perturbation_spec.model_dump(mode="json")
    elif isinstance(perturbation_spec, Mapping):
        params = dict(perturbation_spec)
    else:
        raise TypeError(
            f"expected Mapping or BaseModel for perturbation_spec, got {type(perturbation_spec).__name__}"
        )

    raw_family = params.get("family", params.get("family_name", "tool_unreliability"))
    if hasattr(raw_family, "value"):
        raw_family = raw_family.value

    family_str = str(raw_family).lower()

    if family_str in ("tool_unreliability", PerturbationFamily.TOOL_UNRELIABILITY.value):
        fault_cfg = ToolFaultConfig(
            tool_name=params.get("tool_name", "api_client"),
            fault_mode=ToolFaultMode(params.get("fault_mode", ToolFaultMode.TRANSIENT.value)),
            fault_count=int(params.get("fault_count", 2)),
            error_type=params.get("error_type", "ResourceTemporarilyUnavailable"),
            error_message=params.get(
                "error_message", "503 Service Unavailable: upstream connection reset by peer"
            ),
            alternative_tool=params.get("alternative_tool"),
            state_file_path=params.get("state_file_path", "/tmp/.tool_fault_state.json"),
        )
        injector = ToolFaultInjector(config=fault_cfg)
        return injector.transform(
            base_task_dir=base_task_dir, output_dir=output_dir, config=fault_cfg, seed=seed
        )

    elif family_str in ("epistemic_restraint", PerturbationFamily.EPISTEMIC_RESTRAINT.value):
        variant = params.get("pair_variant", params.get("variant", "abstain"))
        prec = PreconditionSpec(
            precondition_id=params.get("precondition_id", "input_file_integrity"),
            precondition_type=PreconditionType(
                params.get("precondition_type", PreconditionType.MISSING_FILE.value)
            ),
            description=params.get(
                "description", "Required input file `/app/input.txt` must exist."
            ),
            target_path=params.get("target_path", "environment/input.txt"),
            act_content=params.get("act_content", "sample input data for epistemic evaluation\n"),
            abstain_content=params.get("abstain_content"),
            abstention_token=params.get(
                "abstention_token", "ABSTAIN: PRECONDITION_UNSATISFIED: input_file_integrity"
            ),
            verdict_path=params.get("verdict_path", "/app/output/verdict.txt"),
            forbidden_mutations=params.get("forbidden_mutations", ["/app/output/result.txt"]),
        )
        pairer = EpistemicRestraintPairer(precondition=prec)
        if variant == "act":
            return pairer.transform_act(
                base_task_dir=base_task_dir, output_dir=output_dir, precondition=prec, seed=seed
            )
        else:
            return pairer.transform_abstain(
                base_task_dir=base_task_dir, output_dir=output_dir, precondition=prec, seed=seed
            )

    elif family_str in ("context_pressure", PerturbationFamily.CONTEXT_PRESSURE.value):
        ctx_cfg = ContextPressureConfig(
            target_token_count=int(params.get("target_token_count", 1500)),
            channels=[
                PressureChannel(c)
                for c in params.get("channels", ["system_trace", "env_metrics", "audit_log"])
            ],
            output_subdir=params.get("output_subdir", "environment/logs"),
            block_size_tokens=int(params.get("block_size_tokens", 250)),
            provenance_prefix=params.get("provenance_prefix", "synthetic/context_pressure"),
        )
        ctx_injector = ContextPressureInjector(config=ctx_cfg)
        return ctx_injector.apply(
            base_task_dir=base_task_dir, output_dir=output_dir, config=ctx_cfg, seed=seed
        )

    else:
        raise ValueError(f"unsupported perturbation family: {raw_family}")
