"""Mechanical baseline facts, Screening heuristics, and Trace Baseline View (v_trace_baseline).

Deterministic per-trial mechanical facts over ATIF trajectories, with:
- 1 row per trial
- Null-preserving metrics (never convert missing denominators to 0.0)
- Explicit `_screening` suffixes for all heuristic/approximate metrics
- Documented column-level provenance registry
- CBV (Context Burn Velocity) regression slope calculation
- Exit-code cascade streak calculation
- Linearity Index (LI) and Tool Error Rate (TER) screening calculations
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import pyarrow as pa

from evallab.traj import StepOutline, TrajectoryOutline, extract_features


@dataclass(frozen=True)
class BaselineProvenance:
    """Provenance metadata for one baseline column."""

    column_name: str
    data_type: str
    category: str  # "identity" | "mechanical_fact" | "screening_heuristic"
    is_screening: bool
    source_table: str
    formula_or_rule: str
    null_condition: str
    description: str


TRACE_BASELINE_PROVENANCE: dict[str, BaselineProvenance] = {
    "trial_id": BaselineProvenance(
        column_name="trial_id",
        data_type="VARCHAR",
        category="identity",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Deterministic sha256 digest of (job_id, trial_name)",
        null_condition="Never NULL for valid trials",
        description="Unique identifier for the trial.",
    ),
    "job_id": BaselineProvenance(
        column_name="job_id",
        data_type="VARCHAR",
        category="identity",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Job execution identifier from runner metadata",
        null_condition="Never NULL for valid trials",
        description="Identifier of the containing job.",
    ),
    "trial_name": BaselineProvenance(
        column_name="trial_name",
        data_type="VARCHAR",
        category="identity",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Trial directory or record name",
        null_condition="Never NULL",
        description="Human-readable name of the trial.",
    ),
    "job_name": BaselineProvenance(
        column_name="job_name",
        data_type="VARCHAR",
        category="identity",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Job directory or record name",
        null_condition="Never NULL",
        description="Human-readable name of the job.",
    ),
    "task_name": BaselineProvenance(
        column_name="task_name",
        data_type="VARCHAR",
        category="identity",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Task identifier extracted from result.json or config.json",
        null_condition="Never NULL",
        description="Evaluated task name.",
    ),
    "agent_name": BaselineProvenance(
        column_name="agent_name",
        data_type="VARCHAR",
        category="identity",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Agent identifier from trial config/result",
        null_condition="Never NULL",
        description="Name of the evaluated agent.",
    ),
    "agent_version": BaselineProvenance(
        column_name="agent_version",
        data_type="VARCHAR",
        category="identity",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Agent version string from config or 'unknown'",
        null_condition="Never NULL",
        description="Version string of the agent.",
    ),
    "model_name": BaselineProvenance(
        column_name="model_name",
        data_type="VARCHAR",
        category="identity",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Model name from trial config/steps or 'unknown'",
        null_condition="Never NULL",
        description="Underlying foundation model identifier.",
    ),
    "status": BaselineProvenance(
        column_name="status",
        data_type="VARCHAR",
        category="identity",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="'featured' when trajectory parsed; 'accounted_unavailable' on missing/corrupt",
        null_condition="Never NULL",
        description="Trajectory extraction and availability status.",
    ),
    "unavailable_reason": BaselineProvenance(
        column_name="unavailable_reason",
        data_type="VARCHAR",
        category="identity",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Explicit reason when status == 'accounted_unavailable'",
        null_condition="NULL when status == 'featured'",
        description="Reason why trajectory evidence is unavailable.",
    ),
    "source_path": BaselineProvenance(
        column_name="source_path",
        data_type="VARCHAR",
        category="identity",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Relative path to trajectory.json or primary evidence",
        null_condition="Never NULL",
        description="Path to primary source evidence relative to trial root.",
    ),
    "source_sha256": BaselineProvenance(
        column_name="source_sha256",
        data_type="VARCHAR",
        category="identity",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Hex-encoded SHA-256 of raw trajectory source file",
        null_condition="Empty string when file missing",
        description="Content hash of raw trajectory source file.",
    ),
    "primary_reward": BaselineProvenance(
        column_name="primary_reward",
        data_type="DOUBLE",
        category="mechanical_fact",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Primary numeric reward from verifier result (1.0 = pass, 0.0 = fail)",
        null_condition="NULL when verifier did not emit reward",
        description="Primary verifier reward score.",
    ),
    "exception_class": BaselineProvenance(
        column_name="exception_class",
        data_type="VARCHAR",
        category="mechanical_fact",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Exception class name from exception_info if trial crashed",
        null_condition="NULL when no harness/runtime exception occurred",
        description="Exception class name if trial crashed.",
    ),
    "duration_seconds": BaselineProvenance(
        column_name="duration_seconds",
        data_type="DOUBLE",
        category="mechanical_fact",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Finished timestamp minus started timestamp in seconds",
        null_condition="NULL when timestamps unavailable",
        description="Total wall-clock duration of trial in seconds.",
    ),
    "step_count": BaselineProvenance(
        column_name="step_count",
        data_type="BIGINT",
        category="mechanical_fact",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Total count of ordered steps in trajectory",
        null_condition="0 for unavailable trajectories",
        description="Total number of steps in trajectory.",
    ),
    "agent_step_count": BaselineProvenance(
        column_name="agent_step_count",
        data_type="BIGINT",
        category="mechanical_fact",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Count of steps where actor == 'agent'",
        null_condition="0 for unavailable trajectories",
        description="Number of agent execution steps.",
    ),
    "system_step_count": BaselineProvenance(
        column_name="system_step_count",
        data_type="BIGINT",
        category="mechanical_fact",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Count of steps where actor in ('system', 'verifier', 'setup')",
        null_condition="0 for unavailable trajectories",
        description="Number of system/verifier/setup steps.",
    ),
    "user_step_count": BaselineProvenance(
        column_name="user_step_count",
        data_type="BIGINT",
        category="mechanical_fact",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Count of steps where actor == 'user'",
        null_condition="0 for unavailable trajectories",
        description="Number of user/steering steps.",
    ),
    "tool_call_count": BaselineProvenance(
        column_name="tool_call_count",
        data_type="BIGINT",
        category="mechanical_fact",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Total count of tool calls across all steps",
        null_condition="0 for unavailable trajectories",
        description="Total tool calls invoked.",
    ),
    "unique_tools_count": BaselineProvenance(
        column_name="unique_tools_count",
        data_type="BIGINT",
        category="mechanical_fact",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Count of distinct tool names invoked",
        null_condition="0 for unavailable trajectories",
        description="Number of distinct tool names invoked.",
    ),
    "error_count": BaselineProvenance(
        column_name="error_count",
        data_type="BIGINT",
        category="mechanical_fact",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Count of steps with non-zero exit code or error observation",
        null_condition="0 for unavailable trajectories",
        description="Number of steps that resulted in execution or tool errors.",
    ),
    "recovery_count": BaselineProvenance(
        column_name="recovery_count",
        data_type="BIGINT",
        category="mechanical_fact",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Count of successful steps immediately following an error step",
        null_condition="0 for unavailable trajectories",
        description="Number of successful steps immediately following error steps.",
    ),
    "linear_innocence_screening": BaselineProvenance(
        column_name="linear_innocence_screening",
        data_type="DOUBLE",
        category="screening_heuristic",
        is_screening=True,
        source_table="v_trace_baseline",
        formula_or_rule="round(unique_tools_count / tool_call_count, 4)",
        null_condition="NULL when tool_call_count == 0",
        description="Screening heuristic for linearity/tool reuse (distinct tools / total calls).",
    ),
    "tool_error_rate_screening": BaselineProvenance(
        column_name="tool_error_rate_screening",
        data_type="DOUBLE",
        category="screening_heuristic",
        is_screening=True,
        source_table="v_trace_baseline",
        formula_or_rule="round(error_count / tool_call_count, 4)",
        null_condition="NULL when tool_call_count == 0",
        description="Screening heuristic for tool call failure rate (errors / tool calls).",
    ),
    "context_burn_velocity_screening": BaselineProvenance(
        column_name="context_burn_velocity_screening",
        data_type="DOUBLE",
        category="screening_heuristic",
        is_screening=True,
        source_table="v_trace_baseline",
        formula_or_rule="regr_slope(prompt_tokens, step_index) over steps with prompt_tokens",
        null_condition="NULL when < 2 prompt token observations exist",
        description="Screening heuristic: Context Burn Velocity (slope of prompt tokens per step).",
    ),
    "max_exit_code_cascade_screening": BaselineProvenance(
        column_name="max_exit_code_cascade_screening",
        data_type="BIGINT",
        category="screening_heuristic",
        is_screening=True,
        source_table="v_trace_baseline",
        formula_or_rule="Maximum consecutive streak of non-zero exit code steps",
        null_condition="0 when no error steps occur",
        description="Screening heuristic: longest consecutive cascade of failed tool executions.",
    ),
    "cache_hit_rate_screening": BaselineProvenance(
        column_name="cache_hit_rate_screening",
        data_type="DOUBLE",
        category="screening_heuristic",
        is_screening=True,
        source_table="v_trace_baseline",
        formula_or_rule="round(cached_tokens / prompt_tokens, 4)",
        null_condition="NULL when prompt_tokens == 0 or cached_tokens is NULL",
        description="Screening heuristic: token cache hit ratio.",
    ),
    "subagent_overhead_ratio_screening": BaselineProvenance(
        column_name="subagent_overhead_ratio_screening",
        data_type="DOUBLE",
        category="screening_heuristic",
        is_screening=True,
        source_table="v_trace_baseline",
        formula_or_rule="round(subagent_step_count / step_count, 4)",
        null_condition="NULL when step_count == 0; 0.0 when no subagents",
        description="Screening heuristic: ratio of subagent steps to total trajectory steps.",
    ),
    "prompt_tokens": BaselineProvenance(
        column_name="prompt_tokens",
        data_type="BIGINT",
        category="mechanical_fact",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Sum of prompt tokens across LLM calls / steps",
        null_condition="NULL when tokens not tracked",
        description="Total prompt tokens consumed.",
    ),
    "completion_tokens": BaselineProvenance(
        column_name="completion_tokens",
        data_type="BIGINT",
        category="mechanical_fact",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Sum of completion tokens across LLM calls / steps",
        null_condition="NULL when tokens not tracked",
        description="Total completion tokens generated.",
    ),
    "cached_tokens": BaselineProvenance(
        column_name="cached_tokens",
        data_type="BIGINT",
        category="mechanical_fact",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Sum of cached prompt tokens",
        null_condition="NULL when tokens not tracked",
        description="Total cached prompt tokens read.",
    ),
    "total_tokens": BaselineProvenance(
        column_name="total_tokens",
        data_type="BIGINT",
        category="mechanical_fact",
        is_screening=False,
        source_table="v_trace_baseline",
        formula_or_rule="coalesce(prompt_tokens, 0) + coalesce(completion_tokens, 0)",
        null_condition="NULL when both prompt_tokens and completion_tokens are NULL",
        description="Total tokens consumed (prompt + completion).",
    ),
    "cost_usd": BaselineProvenance(
        column_name="cost_usd",
        data_type="DOUBLE",
        category="mechanical_fact",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Total model inference cost in USD",
        null_condition="NULL when cost not recorded",
        description="Total inference cost in USD.",
    ),
    "loop_suspicion_score": BaselineProvenance(
        column_name="loop_suspicion_score",
        data_type="DOUBLE",
        category="screening_heuristic",
        is_screening=True,
        source_table="traj_features",
        formula_or_rule="Deterministic loop heuristic score [0.0, 1.0]",
        null_condition="0.0 for normal execution",
        description="Loop suspicion heuristic score.",
    ),
    "loop_suspicion_detected": BaselineProvenance(
        column_name="loop_suspicion_detected",
        data_type="BOOLEAN",
        category="screening_heuristic",
        is_screening=True,
        source_table="traj_features",
        formula_or_rule="loop_suspicion_score >= 0.5",
        null_condition="Never NULL (False by default)",
        description="Flag indicating whether loop suspicion threshold was exceeded.",
    ),
    "loop_reasons_json": BaselineProvenance(
        column_name="loop_reasons_json",
        data_type="VARCHAR",
        category="screening_heuristic",
        is_screening=True,
        source_table="traj_features",
        formula_or_rule="JSON array of specific loop reason codes triggered",
        null_condition="'[]' when no loop reasons",
        description="JSON array of specific loop reason codes.",
    ),
    "repeated_command_count": BaselineProvenance(
        column_name="repeated_command_count",
        data_type="BIGINT",
        category="mechanical_fact",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="Count of identical consecutive or cyclic commands",
        null_condition="0 when no repeated commands",
        description="Number of repeated commands detected.",
    ),
    "created_at": BaselineProvenance(
        column_name="created_at",
        data_type="VARCHAR",
        category="identity",
        is_screening=False,
        source_table="traj_features",
        formula_or_rule="ISO-8601 UTC timestamp of record creation",
        null_condition="Never NULL",
        description="Timestamp when baseline record was generated.",
    ),
}


@dataclass(frozen=True)
class TraceBaselineRecord:
    """One deterministic baseline row per trial."""

    trial_id: str
    job_id: str
    trial_name: str
    job_name: str
    task_name: str
    agent_name: str
    agent_version: str
    model_name: str
    status: str
    unavailable_reason: str | None
    source_path: str
    source_sha256: str
    primary_reward: float | None
    exception_class: str | None
    duration_seconds: float | None
    step_count: int
    agent_step_count: int
    system_step_count: int
    user_step_count: int
    tool_call_count: int
    unique_tools_count: int
    error_count: int
    recovery_count: int
    linear_innocence_screening: float | None
    tool_error_rate_screening: float | None
    context_burn_velocity_screening: float | None
    max_exit_code_cascade_screening: int
    cache_hit_rate_screening: float | None
    subagent_overhead_ratio_screening: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    total_tokens: int | None
    cost_usd: float | None
    loop_suspicion_score: float
    loop_suspicion_detected: bool
    loop_reasons_json: str
    repeated_command_count: int
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _compute_cbv_slope(steps: Sequence[StepOutline]) -> float | None:
    """Compute regression slope of prompt_tokens over step_ordinal.

    Returns None if fewer than 2 steps have prompt_tokens or if step indices have 0 variance.
    """
    points: list[tuple[int, int]] = []
    for step in steps:
        if step.prompt_tokens is not None:
            points.append((step.step_id, step.prompt_tokens))

    if len(points) < 2:
        return None

    n = len(points)
    sum_x = sum(x for x, _ in points)
    sum_y = sum(y for _, y in points)
    sum_xy = sum(x * y for x, y in points)
    sum_x2 = sum(x * x for x, _ in points)

    denom = (n * sum_x2) - (sum_x * sum_x)
    if denom == 0:
        return None

    slope = ((n * sum_xy) - (sum_x * sum_y)) / denom
    return round(slope, 4)


def _compute_exit_code_cascade(steps: Sequence[StepOutline]) -> int:
    """Compute the maximum streak of consecutive steps with non-zero exit codes."""
    max_streak = 0
    current_streak = 0

    for step in steps:
        is_failing = (step.exit_code is not None and step.exit_code != 0) or step.is_error
        if is_failing:
            current_streak += 1
            if current_streak > max_streak:
                max_streak = current_streak
        else:
            current_streak = 0

    return max_streak


def _compute_subagent_overhead(outline: TrajectoryOutline) -> float | None:
    """Compute ratio of subagent steps to total steps.

    Returns None when step_count is 0, 0.0 when no subagents exist.
    """
    if outline.total_steps == 0:
        return None

    # Check for subagent indicators in step outlines
    subagent_steps = 0
    for step in outline.steps:
        if step.source == "subagent":
            subagent_steps += 1

    return round(subagent_steps / outline.total_steps, 4)


def compute_trace_baseline(outline: TrajectoryOutline) -> TraceBaselineRecord:
    """Extract deterministic mechanical baseline facts and screening metrics from a TrajectoryOutline."""
    # Mechanical facts from base extraction
    feat = extract_features(outline)

    # 1. Linear Innocence (LI) screening: unique tools / total tool calls (NULL when 0 calls)
    li_screening: float | None = None
    if feat.tool_call_count > 0:
        li_screening = round(feat.unique_tools_count / feat.tool_call_count, 4)

    # 2. Tool Error Rate (TER) screening: error_count / tool_call_count (NULL when 0 calls)
    ter_screening: float | None = None
    if feat.tool_call_count > 0:
        ter_screening = round(feat.error_count / feat.tool_call_count, 4)

    # 3. Context Burn Velocity (CBV) screening: regr_slope(prompt_tokens, step_id)
    cbv_screening = _compute_cbv_slope(outline.steps)

    # 4. Max Exit-Code Cascade screening
    max_cascade = _compute_exit_code_cascade(outline.steps)

    # 5. Cache hit rate screening
    # 5. Cache hit rate screening (ATIF cached_tokens is a subset of prompt_tokens)
    cache_hit_rate: float | None = None
    p_tokens = feat.prompt_tokens
    c_tokens = feat.cached_tokens
    if p_tokens is not None and c_tokens is not None and p_tokens > 0:
        cache_hit_rate = round(c_tokens / p_tokens, 4)
    # 6. Total tokens (both prompt and completion tokens must be present, else NULL)
    comp_tokens = feat.completion_tokens
    total_tokens: int | None = None
    if p_tokens is not None and comp_tokens is not None:
        total_tokens = p_tokens + comp_tokens
    # 7. Subagent overhead
    subagent_overhead = _compute_subagent_overhead(outline)

    return TraceBaselineRecord(
        trial_id=feat.trial_id,
        job_id=feat.job_id,
        trial_name=feat.trial_name,
        job_name=feat.job_name,
        task_name=feat.task_name,
        agent_name=feat.agent_name,
        agent_version=feat.agent_version or "unknown",
        model_name=feat.model_name,
        status=feat.status,
        unavailable_reason=feat.unavailable_reason,
        source_path=feat.source_path,
        source_sha256=feat.source_sha256,
        primary_reward=feat.primary_reward,
        exception_class=feat.exception_class,
        duration_seconds=feat.duration_seconds,
        step_count=feat.step_count,
        agent_step_count=feat.agent_step_count,
        system_step_count=feat.system_step_count,
        user_step_count=feat.user_step_count,
        tool_call_count=feat.tool_call_count,
        unique_tools_count=feat.unique_tools_count,
        error_count=feat.error_count,
        recovery_count=feat.recovery_count,
        linear_innocence_screening=li_screening,
        tool_error_rate_screening=ter_screening,
        context_burn_velocity_screening=cbv_screening,
        max_exit_code_cascade_screening=max_cascade,
        cache_hit_rate_screening=cache_hit_rate,
        subagent_overhead_ratio_screening=subagent_overhead,
        prompt_tokens=p_tokens,
        completion_tokens=comp_tokens,
        cached_tokens=c_tokens,
        total_tokens=total_tokens,
        cost_usd=feat.cost_usd,
        loop_suspicion_score=feat.loop_suspicion_score,
        loop_suspicion_detected=feat.loop_suspicion_detected,
        loop_reasons_json=feat.loop_reasons_json,
        repeated_command_count=feat.repeated_command_count,
        created_at=feat.created_at,
    )


TRACE_BASELINE_PARQUET_SCHEMA = pa.schema(
    [
        pa.field("trial_id", pa.string(), nullable=False),
        pa.field("job_id", pa.string(), nullable=False),
        pa.field("trial_name", pa.string(), nullable=False),
        pa.field("job_name", pa.string(), nullable=False),
        pa.field("task_name", pa.string(), nullable=False),
        pa.field("agent_name", pa.string(), nullable=False),
        pa.field("agent_version", pa.string(), nullable=False),
        pa.field("model_name", pa.string(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
        pa.field("unavailable_reason", pa.string(), nullable=True),
        pa.field("source_path", pa.string(), nullable=False),
        pa.field("source_sha256", pa.string(), nullable=False),
        pa.field("primary_reward", pa.float64(), nullable=True),
        pa.field("exception_class", pa.string(), nullable=True),
        pa.field("duration_seconds", pa.float64(), nullable=True),
        pa.field("step_count", pa.int64(), nullable=False),
        pa.field("agent_step_count", pa.int64(), nullable=False),
        pa.field("system_step_count", pa.int64(), nullable=False),
        pa.field("user_step_count", pa.int64(), nullable=False),
        pa.field("tool_call_count", pa.int64(), nullable=False),
        pa.field("unique_tools_count", pa.int64(), nullable=False),
        pa.field("error_count", pa.int64(), nullable=False),
        pa.field("recovery_count", pa.int64(), nullable=False),
        pa.field("linear_innocence_screening", pa.float64(), nullable=True),
        pa.field("tool_error_rate_screening", pa.float64(), nullable=True),
        pa.field("context_burn_velocity_screening", pa.float64(), nullable=True),
        pa.field("max_exit_code_cascade_screening", pa.int64(), nullable=False),
        pa.field("cache_hit_rate_screening", pa.float64(), nullable=True),
        pa.field("subagent_overhead_ratio_screening", pa.float64(), nullable=True),
        pa.field("prompt_tokens", pa.int64(), nullable=True),
        pa.field("completion_tokens", pa.int64(), nullable=True),
        pa.field("cached_tokens", pa.int64(), nullable=True),
        pa.field("total_tokens", pa.int64(), nullable=True),
        pa.field("cost_usd", pa.float64(), nullable=True),
        pa.field("loop_suspicion_score", pa.float64(), nullable=False),
        pa.field("loop_suspicion_detected", pa.bool_(), nullable=False),
        pa.field("loop_reasons_json", pa.string(), nullable=False),
        pa.field("repeated_command_count", pa.int64(), nullable=False),
        pa.field("created_at", pa.string(), nullable=False),
    ]
)

# Public function aliases
compute_cbv_slope = _compute_cbv_slope
compute_exit_code_cascade = _compute_exit_code_cascade
compute_subagent_overhead = _compute_subagent_overhead


def get_column_provenance(column_name: str) -> BaselineProvenance | None:
    """Get column-level provenance record for a trace baseline column."""
    return TRACE_BASELINE_PROVENANCE.get(column_name)


def create_trace_baseline_table(records: Sequence[TraceBaselineRecord]) -> pa.Table:
    """Create a PyArrow table from a sequence of TraceBaselineRecord objects."""
    dicts = [asdict(r) for r in records]
    if not dicts:
        return pa.Table.from_batches([], schema=TRACE_BASELINE_PARQUET_SCHEMA)
    return pa.Table.from_pylist(dicts, schema=TRACE_BASELINE_PARQUET_SCHEMA)
