"""Benchmark-specific feature producers and extractors."""

from __future__ import annotations

from collections.abc import Sequence

from evallab.interpretation.benchmark_events import TrialBundle
from evallab.interpretation.benchmark_projection import BenchmarkProjectionDimensions
from evallab.interpretation.feature_registry import compute_prompt_cache_hit_rate
from evallab.interpretation.producers.action_memory import (
    ActionMemoryFeatures,
    extract_action_memory_features,
)
from evallab.interpretation.producers.mcp_funcdag import (
    McpFuncDagFeatures,
    extract_mcp_funcdag_features,
)
from evallab.interpretation.producers.mcp_recovery import (
    McpRecoveryFeatures,
    RecoveryPersistencePoint,
    build_recovery_persistence_curve,
    extract_mcp_recovery_features,
)
from evallab.interpretation.producers.memgym import (
    MemGymOutcome,
    extract_context_operation_facts_from_memgym,
    extract_memgym_outcome,
)
from evallab.interpretation.producers.memory_continuity import (
    MemoryContinuityFeatures,
    MemoryContinuityStatus,
    extract_context_operation_facts_from_atif,
    extract_memory_continuity_features,
    extract_memory_continuity_features_from_atif,
)

__all__ = [
    "ActionMemoryFeatures",
    "McpFuncDagFeatures",
    "McpRecoveryFeatures",
    "MemoryContinuityFeatures",
    "MemoryContinuityStatus",
    "RecoveryPersistencePoint",
    "build_recovery_persistence_curve",
    "compute_prompt_cache_hit_rate",
    "extract_action_memory_features",
    "extract_context_operation_facts_from_atif",
    "extract_mcp_funcdag_features",
    "extract_memory_continuity_features",
    "extract_memory_continuity_features_from_atif",
    "extract_mcp_recovery_features",
    "extract_benchmark_features",
    "extract_context_operation_facts_from_memgym",
    "extract_memgym_outcome",
    "MemGymOutcome",
]


def extract_benchmark_features(
    bundle: TrialBundle,
    step_tokens: Sequence[int] | None = None,
    dimensions: BenchmarkProjectionDimensions | None = None,
    cached_step_tokens: Sequence[int] | None = None,
    *,
    governed: bool = False,
) -> ActionMemoryFeatures | McpFuncDagFeatures | McpRecoveryFeatures:
    """Extract benchmark-specific features according to the trial bundle's family."""
    if governed:
        bundle.require_causal_admissibility()
    family = bundle.contract.family
    if family == "action-memory-v1":
        return extract_action_memory_features(
            bundle,
            step_tokens=step_tokens,
            dimensions=dimensions,
            cached_step_tokens=cached_step_tokens,
        )
    elif family == "mcp-funcdag-v1":
        return extract_mcp_funcdag_features(
            bundle,
            step_tokens=step_tokens,
            dimensions=dimensions,
            cached_step_tokens=cached_step_tokens,
        )
    elif family == "mcp-recovery-v1":
        return extract_mcp_recovery_features(
            bundle,
            step_tokens=step_tokens,
            dimensions=dimensions,
            cached_step_tokens=cached_step_tokens,
        )
    else:
        raise ValueError(f"Unsupported benchmark family: '{family}'")
