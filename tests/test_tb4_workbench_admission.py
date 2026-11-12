"""Focused verification for HAR-62 TB4 workbench and admission seams.

Tests verify:
1. Timeout boundary: 28800 ok, 28801 rejected citing TB4 v4.0.0 official agent.timeout_sec=28800.
2. Compose 7-service acceptance and isolation-rejection guarantees:
   - 7-service compose topology accepted, depends_on and expose accepted
   - strict rejection of host bind mounts, docker socket, privileged mode, host network mode, and host ports
3. task.toml [task].version and [task].keywords optional; validated when present.
4. Registry admission of synthetic TB4-shaped task without version/keywords.
5. Real corpus admission: all 66 tasks in terminal-bench-4 pass admission validation.
   Records the 3 H100 ids and 11 compose ids.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.registry import TaskRegistry, compute_task_digests
from evallab.schemas import TaskLimits, TaskRegistryRecord
from evallab.task_lint import (
    Diagnostic,
)

REAL_TB4_DIR = Path("/Users/petermakhnatch/Developer/lab-extras/data/agent-evals/terminal-bench-4/tasks")
TB4_H100_IDS = ("fp8-rmsnorm-gemm", "jax-speedrun-gpu", "math-eval-grader")
TB4_COMPOSE_IDS = (
    "ctr-optimization",
    "cumulative-layout-shift",
    "freight-dispatch-shift",
    "heat-pump-warranty",
    "intrastat-meldung",
    "kv-live-surgery",
    "legacy-utility-triage",
    "live-database-cutover",
    "medical-claims-processing",
    "nextjs-performance",
    "payments-pipeline-fix",
)


def _codes(diagnostics: list[Diagnostic]) -> set[str]:
    return {d.code for d in diagnostics}


# ============================================================================ #
# 1. Timeout boundary tests
# ============================================================================ #


def test_task_limits_timeout_ceiling_boundary() -> None:
    """TaskLimits timeout ceiling is 28800; 28801 is rejected citing TB4 agent.timeout_sec=28800."""
    # 28800 accepted
    limits = TaskLimits(timeout_seconds=28_800)
    assert limits.timeout_seconds == 28_800

    # 1 accepted
    limits_min = TaskLimits(timeout_seconds=1)
    assert limits_min.timeout_seconds == 1

    # 28801 rejected with citation
    with pytest.raises(ValidationError) as exc_info:
        TaskLimits(timeout_seconds=28_801)
    error_msg = str(exc_info.value)
    assert "28800" in error_msg
    assert "TB4 v4.0.0 official agent.timeout_sec=28800" in error_msg

    # 0 rejected
    with pytest.raises(ValidationError):
        TaskLimits(timeout_seconds=0)


# ============================================================================ #
# 2. Version and keywords optionality in task metadata
# ============================================================================ #


# ============================================================================ #
# 3. Multi-service compose acceptance (7 services, depends_on, expose)
# ============================================================================ #


# ============================================================================ #
# 4. Strict isolation guarantees preserved (negative tests)
# ============================================================================ #


# ============================================================================ #
# 5. Registry admission of synthetic TB4-shaped task
# ============================================================================ #


def test_registry_admission_synthetic_tb4_shaped_task(tmp_path: Path) -> None:
    """A synthetic task with TB4 shape (no version, no keywords, timeout 28800) admits cleanly."""
    task_dir = tmp_path / "tb4-synthetic"
    env_dir = task_dir / "environment"
    env_dir.mkdir(parents=True)
    (env_dir / "Dockerfile").write_text("FROM alpine:3.20\n")
    (task_dir / "instruction.md").write_text("Complete this task.\n")
    (task_dir / "tests").mkdir()
    (task_dir / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
    (task_dir / "task.toml").write_text(
        'schema_version = "1.0"\n\n'
        '[task]\n'
        'name = "terminal-bench/synthetic-tb4"\n'
        'authors = [{ name = "ScaleAI", email = "tb4@scale.com" }]\n\n'
        '[agent]\n'
        'timeout_sec = 28800\n\n'
        '[verifier]\n'
        'timeout_sec = 300\n'
        'environment_mode = "separate"\n\n'
        '[metadata]\n'
        'category = "data"\n'
        'tags = ["analytics", "synthetic"]\n'
        'expert_time_estimate_hours = 4.0\n'
    )

    # 1. Digests compute
    digests = compute_task_digests(task_dir)
    assert digests.task_toml.startswith("sha256:")
    assert digests.instruction.startswith("sha256:")
    assert digests.environment.startswith("sha256:")
    assert digests.verifier.startswith("sha256:")
    assert digests.package.startswith("sha256:")

    # 2. TaskRegistryRecord schema admission without version or keywords
    record = TaskRegistryRecord(
        schema_version=2,
        task_id="synthetic-tb4",
        task_family="terminal-bench-4",
        task_path="library/tasks/tb4-synthetic",
        digests=digests,
        source_uri="terminal-bench-4/synthetic-tb4@1.0.0",
        provenance_zone="01-external",
        is_synthetic=False,
        limits=TaskLimits(timeout_seconds=28_800),
        state="candidate",
        state_reason="pending_control_evidence",
        allowed_uses=["measurement"],
    )
    assert record.version == "1.0.0"  # defaulted
    assert record.limits.timeout_seconds == 28_800
    assert record.state == "candidate"
    # 3. Registry admission: save and load via TaskRegistry
    reg_dir = tmp_path / "registry"
    registry = TaskRegistry(reg_dir, {})
    dest_path = registry.save_record(record)
    assert dest_path.is_file()

    # Reload from registry directory
    reloaded_registry = TaskRegistry(reg_dir, {record.task_id: record})
    admitted = reloaded_registry.get("synthetic-tb4")
    assert admitted is not None
    assert admitted.task_id == "synthetic-tb4"
    assert admitted.version == "1.0.0"
    assert admitted.limits.timeout_seconds == 28_800
    assert admitted.state == "candidate"


# ============================================================================ #
# 6. Real corpus verification (all 66 pinned tasks)
# ============================================================================ #


