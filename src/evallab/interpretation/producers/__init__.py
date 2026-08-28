"""Benchmark-specific feature producers and extractors."""

from __future__ import annotations

from collections.abc import Sequence

from evallab.interpretation.benchmark_events import TrialBundle
from evallab.interpretation.benchmark_projection import BenchmarkProjectionDimensions
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
    extract_mcp_recovery_features,
)

__all__ = [
    "ActionMemoryFeatures",
    "McpFuncDagFeatures",
    "McpRecoveryFeatures",
    "extract_action_memory_features",
    "extract_mcp_funcdag_features",
    "extract_mcp_recovery_features",
    "extract_benchmark_features",
]


def extract_benchmark_features(
    bundle: TrialBundle,
    step_tokens: Sequence[int] | None = None,
    cache_hits: Sequence[bool] | None = None,
    dimensions: BenchmarkProjectionDimensions | None = None,
) -> ActionMemoryFeatures | McpFuncDagFeatures | McpRecoveryFeatures:
    """Extract benchmark-specific features according to the trial bundle's family."""
    family = bundle.contract.family
    if family == "action-memory-v1":
        return extract_action_memory_features(
            bundle, step_tokens=step_tokens, cache_hits=cache_hits, dimensions=dimensions
        )
    elif family == "mcp-funcdag-v1":
        return extract_mcp_funcdag_features(
            bundle, step_tokens=step_tokens, cache_hits=cache_hits, dimensions=dimensions
        )
    elif family == "mcp-recovery-v1":
        return extract_mcp_recovery_features(
            bundle, step_tokens=step_tokens, cache_hits=cache_hits, dimensions=dimensions
        )
    else:
        raise ValueError(f"Unsupported benchmark family: '{family}'")
