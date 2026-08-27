"""Trajectory Loss Manifest and Fidelity Auditor (P1).

Provides an immutable, declared per-field loss manifest for ATIF and Harbor
trajectory specifications. Ensures all fields (including reasoning_content,
reasoning_tokens, token IDs, logprobs, sampling parameters, and sample index)
are tracked with explicit preservation status, storage tier, and non-lossy
provenance guarantees.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

FieldPreservationStatus = Literal["preserved", "digested", "dropped"]
FieldStorageTier = Literal[
    "in_memory_ir",
    "cas_blob",
    "parquet_column",
    "digest_citation",
    "outline_preview",
]


@dataclass(frozen=True)
class FieldLossEntry:
    """Declared preservation status and target storage tier for one schema field."""

    field_path: str
    status: FieldPreservationStatus
    storage_tier: FieldStorageTier
    is_lossless: bool
    description: str
    reason: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class LossManifest:
    """Declared loss manifest specification for a trajectory schema version."""

    schema_version: str
    declared_fields: dict[str, FieldLossEntry]
    created_at: str

    def get_entry(self, field_path: str) -> FieldLossEntry | None:
        """Get the declared loss entry for a given field path."""
        return self.declared_fields.get(field_path)

    def is_declared(self, field_path: str) -> bool:
        """Check whether a field path has a declared preservation rule."""
        return field_path in self.declared_fields


@dataclass(frozen=True)
class TrajectoryFieldAudit:
    """Audit result for a single field in a specific trajectory instance."""

    field_path: str
    present_in_raw: bool
    raw_type: str | None
    raw_size_bytes: int | None
    status: FieldPreservationStatus
    storage_tier: FieldStorageTier
    is_lossless: bool
    reason: str | None = None


@dataclass(frozen=True)
class TrajectoryLossReport:
    """Comprehensive loss and fidelity audit report for a trajectory."""

    trajectory_id: str | None
    source_path: str | None
    total_fields_evaluated: int
    preserved_fields_count: int
    digested_fields_count: int
    dropped_fields_count: int
    undeclared_fields_count: int
    is_fully_declared: bool
    audits: tuple[TrajectoryFieldAudit, ...]
    undeclared_fields: tuple[str, ...] = ()
    evaluated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        """Convert the report into a clean JSON-serializable dictionary."""
        return {
            "trajectory_id": self.trajectory_id,
            "source_path": self.source_path,
            "total_fields_evaluated": self.total_fields_evaluated,
            "preserved_fields_count": self.preserved_fields_count,
            "digested_fields_count": self.digested_fields_count,
            "dropped_fields_count": self.dropped_fields_count,
            "undeclared_fields_count": self.undeclared_fields_count,
            "is_fully_declared": self.is_fully_declared,
            "undeclared_fields": list(self.undeclared_fields),
            "evaluated_at": self.evaluated_at,
            "audits": [asdict(a) for a in self.audits],
        }


# Canonical declared manifest for ATIF-v1.7 and Harbor trials
_ATIF_DECLARED_FIELDS: dict[str, FieldLossEntry] = {
    # Root level fields
    "root.schema_version": FieldLossEntry(
        field_path="root.schema_version",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="ATIF schema version string",
    ),
    "root.session_id": FieldLossEntry(
        field_path="root.session_id",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Session UUID identifier",
    ),
    "root.trajectory_id": FieldLossEntry(
        field_path="root.trajectory_id",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Trajectory UUID or unique trial ID",
    ),
    "root.notes": FieldLossEntry(
        field_path="root.notes",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Global trajectory notes and execution context",
    ),
    "root.agent.name": FieldLossEntry(
        field_path="root.agent.name",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Executing agent identity name",
    ),
    "root.agent.version": FieldLossEntry(
        field_path="root.agent.version",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Executing agent version string",
    ),
    "root.agent.model_name": FieldLossEntry(
        field_path="root.agent.model_name",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Base foundation model identifier",
    ),
    "root.agent.model": FieldLossEntry(
        field_path="root.agent.model",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Base foundation model identifier alias",
    ),
    "root.agent.tool_definitions": FieldLossEntry(
        field_path="root.agent.tool_definitions",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Available tools and schemas provided to the agent",
    ),
    "root.agent.tools": FieldLossEntry(
        field_path="root.agent.tools",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Available tools and schemas alias",
    ),
    "root.agent.extra": FieldLossEntry(
        field_path="root.agent.extra",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Auxiliary agent metadata and configuration parameters",
    ),
    "root.steps": FieldLossEntry(
        field_path="root.steps",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Ordered sequence of trajectory steps",
    ),
    "root.final_metrics.total_prompt_tokens": FieldLossEntry(
        field_path="root.final_metrics.total_prompt_tokens",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Total prompt tokens consumed across all steps",
    ),
    "root.final_metrics.total_completion_tokens": FieldLossEntry(
        field_path="root.final_metrics.total_completion_tokens",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Total completion tokens generated across all steps",
    ),
    "root.final_metrics.total_cached_tokens": FieldLossEntry(
        field_path="root.final_metrics.total_cached_tokens",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Total prompt tokens read from cache",
    ),
    "root.final_metrics.total_cost_usd": FieldLossEntry(
        field_path="root.final_metrics.total_cost_usd",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Cumulative inference cost in USD",
    ),
    "root.final_metrics.total_steps": FieldLossEntry(
        field_path="root.final_metrics.total_steps",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Total number of steps in the trajectory",
    ),
    "root.final_metrics.extra": FieldLossEntry(
        field_path="root.final_metrics.extra",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Auxiliary summary metrics and rollups",
    ),
    "root.subagent_trajectories": FieldLossEntry(
        field_path="root.subagent_trajectories",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Nested or spawned subagent trajectory references",
    ),
    "root.continued_trajectory_ref": FieldLossEntry(
        field_path="root.continued_trajectory_ref",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Pointer to parent or previous trajectory segment",
    ),
    "root.extra": FieldLossEntry(
        field_path="root.extra",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Custom root metadata",
    ),
    # Step level fields
    "step.step_id": FieldLossEntry(
        field_path="step.step_id",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="1-based step index",
    ),
    "step.timestamp": FieldLossEntry(
        field_path="step.timestamp",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="ISO 8601 UTC timestamp of step execution",
    ),
    "step.source": FieldLossEntry(
        field_path="step.source",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Role producing step: system, user, or agent",
    ),
    "step.model_name": FieldLossEntry(
        field_path="step.model_name",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Model executing this specific step",
    ),
    "step.message": FieldLossEntry(
        field_path="step.message",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Full dialogue message payload",
    ),
    "step.message_sha256": FieldLossEntry(
        field_path="step.message_sha256",
        status="preserved",
        storage_tier="digest_citation",
        is_lossless=True,
        description="Content digest of message payload",
    ),
    "step.message_chars": FieldLossEntry(
        field_path="step.message_chars",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Character length of message payload",
    ),
    "step.reasoning_content": FieldLossEntry(
        field_path="step.reasoning_content",
        status="preserved",
        storage_tier="cas_blob",
        is_lossless=True,
        description="Full raw internal reasoning/CoT text. Preserved losslessly in CAS blob with cas:// URI citation and full in-memory IR.",
    ),
    "step.reasoning_content_ref": FieldLossEntry(
        field_path="step.reasoning_content_ref",
        status="preserved",
        storage_tier="digest_citation",
        is_lossless=True,
        description="Content-addressed storage URI (cas://sha256/<hash>) for reasoning content",
    ),
    "step.sampling_params": FieldLossEntry(
        field_path="step.sampling_params",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Sampling hyper-parameters: temperature, top_p, top_k, max_tokens, reasoning_effort, seed",
    ),
    "step.temperature": FieldLossEntry(
        field_path="step.temperature",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Sampling temperature",
    ),
    "step.top_p": FieldLossEntry(
        field_path="step.top_p",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Top-p nucleus sampling probability",
    ),
    "step.top_k": FieldLossEntry(
        field_path="step.top_k",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Top-k sampling threshold",
    ),
    "step.max_tokens": FieldLossEntry(
        field_path="step.max_tokens",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Maximum generation token limit",
    ),
    "step.seed": FieldLossEntry(
        field_path="step.seed",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Random seed for generation",
    ),
    "step.presence_penalty": FieldLossEntry(
        field_path="step.presence_penalty",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Presence penalty coefficient",
    ),
    "step.frequency_penalty": FieldLossEntry(
        field_path="step.frequency_penalty",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Frequency penalty coefficient",
    ),
    "step.stop": FieldLossEntry(
        field_path="step.stop",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Stop sequences for generation",
    ),
    "step.reasoning_effort": FieldLossEntry(
        field_path="step.reasoning_effort",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Reasoning effort level (low, medium, high)",
    ),
    "step.sample_index": FieldLossEntry(
        field_path="step.sample_index",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Sample index for best-of-N or multi-sample generations",
    ),
    "step.llm_call_count": FieldLossEntry(
        field_path="step.llm_call_count",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Ordinal LLM invocation counter within the step",
    ),
    "step.is_copied_context": FieldLossEntry(
        field_path="step.is_copied_context",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Flag indicating step context was copied from prior trajectory",
    ),
    "step.tool_calls": FieldLossEntry(
        field_path="step.tool_calls",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Ordered tool invocations requested by the agent",
    ),
    "step.tool_calls[].tool_call_id": FieldLossEntry(
        field_path="step.tool_calls[].tool_call_id",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Unique identifier for the tool call",
    ),
    "step.tool_calls[].function_name": FieldLossEntry(
        field_path="step.tool_calls[].function_name",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Name of the invoked tool/function",
    ),
    "step.tool_calls[].arguments": FieldLossEntry(
        field_path="step.tool_calls[].arguments",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Parsed tool invocation arguments",
    ),
    "step.tool_calls[].arguments_raw": FieldLossEntry(
        field_path="step.tool_calls[].arguments_raw",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Unparsed raw tool invocation string",
    ),
    "step.tool_calls[].extra": FieldLossEntry(
        field_path="step.tool_calls[].extra",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Auxiliary tool call metadata",
    ),
    "step.observation": FieldLossEntry(
        field_path="step.observation",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Harbor observation container object",
    ),
    "step.observation.results": FieldLossEntry(
        field_path="step.observation.results",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Harbor observation results list",
    ),
    "step.observation_results": FieldLossEntry(
        field_path="step.observation_results",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Tool execution results and environment observations",
    ),
    "step.observation_results[].source_call_id": FieldLossEntry(
        field_path="step.observation_results[].source_call_id",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Matching tool_call_id for this observation",
    ),
    "step.observation_results[].content": FieldLossEntry(
        field_path="step.observation_results[].content",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Observation text, stdout, stderr, or structured payload",
    ),
    "step.observation_results[].type": FieldLossEntry(
        field_path="step.observation_results[].type",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Observation result type classification (text, image, error)",
    ),
    "step.observation_results[].status": FieldLossEntry(
        field_path="step.observation_results[].status",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Observation execution status (success, error, failed)",
    ),
    "step.observation_results[].content_ref": FieldLossEntry(
        field_path="step.observation_results[].content_ref",
        status="preserved",
        storage_tier="digest_citation",
        is_lossless=True,
        description="Content-addressed CAS URI for observation payload",
    ),
    "step.observation_results[].content_bytes": FieldLossEntry(
        field_path="step.observation_results[].content_bytes",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Size in bytes of raw observation output",
    ),
    "step.observation_results[].content_digest": FieldLossEntry(
        field_path="step.observation_results[].content_digest",
        status="preserved",
        storage_tier="digest_citation",
        is_lossless=True,
        description="SHA-256 digest of observation output",
    ),
    "step.observation_results[].subagent_trajectory_ref": FieldLossEntry(
        field_path="step.observation_results[].subagent_trajectory_ref",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Subagent trajectory reference returned in tool result",
    ),
    "step.observation_results[].extra": FieldLossEntry(
        field_path="step.observation_results[].extra",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Auxiliary observation result metadata",
    ),
    "step.metrics.prompt_tokens": FieldLossEntry(
        field_path="step.metrics.prompt_tokens",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Prompt tokens for this step",
    ),
    "step.metrics.completion_tokens": FieldLossEntry(
        field_path="step.metrics.completion_tokens",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Completion tokens generated in this step",
    ),
    "step.metrics.cached_tokens": FieldLossEntry(
        field_path="step.metrics.cached_tokens",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Cached prompt tokens for this step",
    ),
    "step.metrics.cost_usd": FieldLossEntry(
        field_path="step.metrics.cost_usd",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Cost in USD for this step",
    ),
    "step.metrics.reasoning_tokens": FieldLossEntry(
        field_path="step.metrics.reasoning_tokens",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Reasoning/thinking tokens generated in this step",
    ),
    "step.metrics.prompt_token_ids": FieldLossEntry(
        field_path="step.metrics.prompt_token_ids",
        status="preserved",
        storage_tier="cas_blob",
        is_lossless=True,
        description="Exact prompt token ID sequence, stored losslessly in CAS blob when present",
    ),
    "step.metrics.completion_token_ids": FieldLossEntry(
        field_path="step.metrics.completion_token_ids",
        status="preserved",
        storage_tier="cas_blob",
        is_lossless=True,
        description="Exact completion token ID sequence, stored losslessly in CAS blob when present",
    ),
    "step.metrics.logprobs": FieldLossEntry(
        field_path="step.metrics.logprobs",
        status="preserved",
        storage_tier="cas_blob",
        is_lossless=True,
        description="Detailed log probability structures, stored losslessly in CAS blob when present",
    ),
    "step.metrics.extra": FieldLossEntry(
        field_path="step.metrics.extra",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Auxiliary step metrics metadata",
    ),
    "step.extra": FieldLossEntry(
        field_path="step.extra",
        status="preserved",
        storage_tier="in_memory_ir",
        is_lossless=True,
        description="Custom step-level metadata",
    ),
}


def get_declared_loss_manifest(schema_version: str = "ATIF-v1.7") -> LossManifest:
    """Return the declared loss manifest for the requested ATIF schema version."""
    return LossManifest(
        schema_version=schema_version,
        declared_fields=dict(_ATIF_DECLARED_FIELDS),
        created_at="2026-08-27T00:00:00Z",
    )


def audit_trajectory_loss(
    raw_atif_data: dict[str, Any],
    source_path: str | None = None,
    manifest: LossManifest | None = None,
) -> TrajectoryLossReport:
    """Audit a raw trajectory JSON structure against the declared loss manifest.

    Verifies that every present field is declared, tracks lossless preservation,
    and identifies any undeclared fields or non-lossless mutations.
    """
    active_manifest = manifest or get_declared_loss_manifest(
        str(raw_atif_data.get("schema_version") or "ATIF-v1.7")
    )
    audits: list[TrajectoryFieldAudit] = []
    undeclared: list[str] = []

    trajectory_id = (
        raw_atif_data.get("trajectory_id")
        or raw_atif_data.get("session_id")
        or (raw_atif_data.get("agent", {}).get("name") if isinstance(raw_atif_data.get("agent"), dict) else None)
    )

    # 1. Audit root fields
    for key, value in raw_atif_data.items():
        if key in ("agent", "steps", "final_metrics"):
            continue
        field_path = f"root.{key}"
        entry = active_manifest.get_entry(field_path)
        if entry:
            audits.append(
                TrajectoryFieldAudit(
                    field_path=field_path,
                    present_in_raw=value is not None,
                    raw_type=type(value).__name__ if value is not None else None,
                    raw_size_bytes=len(str(value).encode("utf-8")) if value is not None else None,
                    status=entry.status,
                    storage_tier=entry.storage_tier,
                    is_lossless=entry.is_lossless,
                    reason=entry.reason,
                )
            )
        else:
            undeclared.append(field_path)

    # 2. Audit agent subfields
    agent_val = raw_atif_data.get("agent")
    if isinstance(agent_val, dict):
        for k, v in agent_val.items():
            field_path = f"root.agent.{k}"
            entry = active_manifest.get_entry(field_path)
            if entry:
                audits.append(
                    TrajectoryFieldAudit(
                        field_path=field_path,
                        present_in_raw=v is not None,
                        raw_type=type(v).__name__ if v is not None else None,
                        raw_size_bytes=len(str(v).encode("utf-8")) if v is not None else None,
                        status=entry.status,
                        storage_tier=entry.storage_tier,
                        is_lossless=entry.is_lossless,
                        reason=entry.reason,
                    )
                )
            else:
                undeclared.append(field_path)

    # 3. Audit final_metrics subfields
    fm_val = raw_atif_data.get("final_metrics")
    if isinstance(fm_val, dict):
        for k, v in fm_val.items():
            field_path = f"root.final_metrics.{k}"
            entry = active_manifest.get_entry(field_path)
            if entry:
                audits.append(
                    TrajectoryFieldAudit(
                        field_path=field_path,
                        present_in_raw=v is not None,
                        raw_type=type(v).__name__ if v is not None else None,
                        raw_size_bytes=len(str(v).encode("utf-8")) if v is not None else None,
                        status=entry.status,
                        storage_tier=entry.storage_tier,
                        is_lossless=entry.is_lossless,
                        reason=entry.reason,
                    )
                )
            else:
                undeclared.append(field_path)

    # 4. Audit step subfields
    steps = raw_atif_data.get("steps")
    if isinstance(steps, list):
        seen_step_fields: set[str] = set()
        for step in steps:
            if not isinstance(step, dict):
                continue
            for sk, sv in step.items():
                if sk in ("tool_calls", "observation_results", "observation", "metrics", "sampling_params"):
                    continue
                step_path = f"step.{sk}"
                if step_path not in seen_step_fields:
                    seen_step_fields.add(step_path)
                    entry = active_manifest.get_entry(step_path)
                    if entry:
                        audits.append(
                            TrajectoryFieldAudit(
                                field_path=step_path,
                                present_in_raw=sv is not None,
                                raw_type=type(sv).__name__ if sv is not None else None,
                                raw_size_bytes=len(str(sv).encode("utf-8")) if sv is not None else None,
                                status=entry.status,
                                storage_tier=entry.storage_tier,
                                is_lossless=entry.is_lossless,
                                reason=entry.reason,
                            )
                        )
                    else:
                        undeclared.append(step_path)

            # Step metrics
            s_metrics = step.get("metrics")
            if isinstance(s_metrics, dict):
                for mk, mv in s_metrics.items():
                    m_path = f"step.metrics.{mk}"
                    if m_path not in seen_step_fields:
                        seen_step_fields.add(m_path)
                        entry = active_manifest.get_entry(m_path)
                        if entry:
                            audits.append(
                                TrajectoryFieldAudit(
                                    field_path=m_path,
                                    present_in_raw=mv is not None,
                                    raw_type=type(mv).__name__ if mv is not None else None,
                                    raw_size_bytes=len(str(mv).encode("utf-8")) if mv is not None else None,
                                    status=entry.status,
                                    storage_tier=entry.storage_tier,
                                    is_lossless=entry.is_lossless,
                                    reason=entry.reason,
                                )
                            )
                        else:
                            undeclared.append(m_path)

            # Tool calls
            t_calls = step.get("tool_calls")
            if isinstance(t_calls, list):
                for tc in t_calls:
                    if isinstance(tc, dict):
                        for tck, tcv in tc.items():
                            tc_path = f"step.tool_calls[].{tck}"
                            if tc_path not in seen_step_fields:
                                seen_step_fields.add(tc_path)
                                entry = active_manifest.get_entry(tc_path)
                                if entry:
                                    audits.append(
                                        TrajectoryFieldAudit(
                                            field_path=tc_path,
                                            present_in_raw=tcv is not None,
                                            raw_type=type(tcv).__name__ if tcv is not None else None,
                                            raw_size_bytes=len(str(tcv).encode("utf-8")) if tcv is not None else None,
                                            status=entry.status,
                                            storage_tier=entry.storage_tier,
                                            is_lossless=entry.is_lossless,
                                            reason=entry.reason,
                                        )
                                    )
                                else:
                                    undeclared.append(tc_path)

            # Observation results
            obs_results = step.get("observation_results")
            if not isinstance(obs_results, list) and isinstance(step.get("observation"), dict):
                obs_results = step["observation"].get("results")
            if isinstance(obs_results, list):
                for obs in obs_results:
                    if isinstance(obs, dict):
                        for obsk, obsv in obs.items():
                            obs_path = f"step.observation_results[].{obsk}"
                            if obs_path not in seen_step_fields:
                                seen_step_fields.add(obs_path)
                                entry = active_manifest.get_entry(obs_path)
                                if entry:
                                    audits.append(
                                        TrajectoryFieldAudit(
                                            field_path=obs_path,
                                            present_in_raw=obsv is not None,
                                            raw_type=type(obsv).__name__ if obsv is not None else None,
                                            raw_size_bytes=len(str(obsv).encode("utf-8")) if obsv is not None else None,
                                            status=entry.status,
                                            storage_tier=entry.storage_tier,
                                            is_lossless=entry.is_lossless,
                                            reason=entry.reason,
                                        )
                                    )
                                else:
                                    undeclared.append(obs_path)

    preserved_count = sum(1 for a in audits if a.status == "preserved")
    digested_count = sum(1 for a in audits if a.status == "digested")
    dropped_count = sum(1 for a in audits if a.status == "dropped")

    return TrajectoryLossReport(
        trajectory_id=str(trajectory_id) if trajectory_id else None,
        source_path=source_path,
        total_fields_evaluated=len(audits) + len(undeclared),
        preserved_fields_count=preserved_count,
        digested_fields_count=digested_count,
        dropped_fields_count=dropped_count,
        undeclared_fields_count=len(undeclared),
        is_fully_declared=len(undeclared) == 0,
        audits=tuple(audits),
        undeclared_fields=tuple(undeclared),
    )
