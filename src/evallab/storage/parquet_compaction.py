"""Deterministic Parquet compaction engine (WS-E item 4).

Consolidates uncompacted granular partitions from `derived/parquet/job_id=*/` into
closed-day partition layout `derived/parquet/compact/dt=YYYY-MM-DD/*.parquet` (one
file per table per day).

Retains per-trial granular partitions for recent days (default 7 days) for fine-grained
debugging, while pruning older granular partitions after compaction and validation.
Enforces zero row loss and exact schema integrity before and after writes.
"""

from __future__ import annotations

PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "jobs": ("job_id",),
    "trajectories": ("job_id", "trial_id", "document_id"),
    "steps": ("job_id", "trial_id", "document_id", "step_id"),
    "tool_calls": ("job_id", "trial_id", "document_id", "step_id", "tool_call_id"),
    "observations": ("job_id", "trial_id", "document_id", "step_id", "observation_index"),
    "trial_facts": ("job_id", "trial_id"),
    "reward_facts": ("job_id", "trial_id", "reward_name"),
    "artifact_facts": ("job_id", "trial_id", "source"),
    "tool_usage": ("job_id", "trial_id", "function_name"),
    "state_changes": ("job_id", "trial_id", "path"),
    "state_events": ("job_id", "trial_id", "sequence"),
    "trajectory_events": ("job_id", "trial_id", "document_id", "step_id", "event_id"),
    "agent_actions": ("job_id", "trial_id", "document_id", "step_id", "action_id"),
    "llm_calls": ("job_id", "trial_id", "document_id", "step_id", "call_id"),
    "trajectory_phases": ("job_id", "trial_id", "phase_id"),
    "action_effects": ("job_id", "trial_id", "effect_id"),
    "capability_opportunities": ("trial_id", "opportunity_id"),
    "process_step_facts": ("trial_id", "source_trajectory_id", "source_step_id"),
    "constraint_facts": ("trial_id", "plan_id", "constraint_id"),
    "context_operation_facts": ("trial_id", "operation_id"),
    "paired_condition_facts": ("trial_id", "pair_id", "condition"),
    "session_dependency_facts": ("trial_id", "episode_id", "dependency_edge"),
    "evidence_coverage": ("trial_id", "benchmark", "construct"),
}

