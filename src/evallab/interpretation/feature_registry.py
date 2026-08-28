"""Explicit feature registry and producer CI validation for trajectory baseline metrics.

Every mechanical fact, screening heuristic, rate, and ratio column in v_trace_baseline
and TrajectoryFeatures must be explicitly registered, classified by provenance category,
typed, documented, and declare its denominator sibling for null-on-zero invariants.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import pyarrow as pa

FeatureCategory = Literal[
    "identity",
    "mechanical_fact",
    "screening_heuristic",
    "benchmark_ground_truth",
    "benchmark_l1_fact",
    "benchmark_l2_metric",
]
FeatureDataType = Literal["VARCHAR", "BIGINT", "DOUBLE", "BOOLEAN"]


@dataclass(frozen=True)
class FeatureDefinition:
    """Explicit definition and validation contract for a trajectory feature."""

    column_name: str
    data_type: FeatureDataType
    category: FeatureCategory
    is_screening: bool
    source_table: str
    formula_or_rule: str
    null_condition: str
    description: str
    denominator_sibling: str | None = None
    null_on_zero_denominator: bool = False
    producer_module: str = "evallab.traj"
    construct: str | None = None
    causal_grade: str | None = None
    evidence_grade: str | None = None
    metric_order: int | None = None
    eligibility_precondition: str | None = None
    family: str | None = None

    def validate_contract(self) -> list[str]:
        """Validate naming, typing, and denominator invariants for this feature."""
        errors: list[str] = []
        if self.is_screening and not self.column_name.endswith("_screening"):
            errors.append(
                f"Feature {self.column_name!r} marked is_screening=True must end with '_screening'"
            )
        if not self.is_screening and self.column_name.endswith("_screening"):
            errors.append(
                f"Feature {self.column_name!r} ends with '_screening' but is_screening=False"
            )
        if self.is_screening and self.category != "screening_heuristic":
            errors.append(
                f"Screening feature {self.column_name!r} must have category='screening_heuristic', got {self.category!r}"
            )
        if not self.is_screening and self.category == "screening_heuristic":
            errors.append(
                f"Feature {self.column_name!r} has category='screening_heuristic' but is_screening=False"
            )
        if self.null_on_zero_denominator and not self.denominator_sibling:
            errors.append(
                f"Feature {self.column_name!r} requires null_on_zero_denominator=True but has no denominator_sibling declared"
            )
        return errors


class FeatureRegistry:
    """Registry of validated trajectory features with producer CI contracts."""

    def __init__(self) -> None:
        self._features: dict[str, FeatureDefinition] = {}

    def register(self, feature: FeatureDefinition) -> FeatureDefinition:
        """Register a feature definition, asserting contract invariants."""
        contract_errors = feature.validate_contract()
        if contract_errors:
            raise ValueError(
                f"Invalid feature definition for {feature.column_name}: {'; '.join(contract_errors)}"
            )
        self._features[feature.column_name] = feature
        return feature

    def get(self, name: str) -> FeatureDefinition | None:
        """Get feature definition by column name."""
        return self._features.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._features

    def __len__(self) -> int:
        return len(self._features)

    def all_features(self) -> dict[str, FeatureDefinition]:
        """Return shallow copy of all registered features."""
        return dict(self._features)

    def by_family(self, family: str) -> dict[str, FeatureDefinition]:
        """Return all features registered for a specific benchmark family."""
        return {k: v for k, v in self._features.items() if v.family == family}

    def by_construct(self, construct: str) -> dict[str, FeatureDefinition]:
        """Return all features registered for a specific construct."""
        return {k: v for k, v in self._features.items() if v.construct == construct}

    def by_category(self, category: FeatureCategory) -> dict[str, FeatureDefinition]:
        """Return all features registered under a specific category."""
        return {k: v for k, v in self._features.items() if v.category == category}

    def verify_against_schema(self, schema: pa.Schema) -> list[str]:
        """Verify that all fields in a PyArrow schema match registered contracts."""
        errors: list[str] = []
        for field in schema:
            name = field.name
            feat = self.get(name)
            if feat is None:
                errors.append(f"Unregistered column in schema: {name!r}")
                continue

            # Verify PyArrow type mapping
            pa_type = str(field.type)
            if feat.data_type == "VARCHAR" and not (
                pa.types.is_string(field.type) or pa.types.is_large_string(field.type)
            ):
                errors.append(f"Column {name!r} expects VARCHAR, got PyArrow {pa_type}")
            elif feat.data_type == "BIGINT" and not pa.types.is_integer(field.type):
                errors.append(f"Column {name!r} expects BIGINT, got PyArrow {pa_type}")
            elif feat.data_type == "DOUBLE" and not pa.types.is_floating(field.type):
                errors.append(f"Column {name!r} expects DOUBLE, got PyArrow {pa_type}")
            elif feat.data_type == "BOOLEAN" and not pa.types.is_boolean(field.type):
                errors.append(f"Column {name!r} expects BOOLEAN, got PyArrow {pa_type}")

            # Verify denominator sibling presence
            if feat.denominator_sibling and feat.denominator_sibling not in self._features:
                errors.append(
                    f"Column {name!r} references missing denominator sibling {feat.denominator_sibling!r}"
                )

        return errors


# Global pre-populated registry instance
TRAJECTORY_FEATURE_REGISTRY = FeatureRegistry()


def register_trajectory_feature(
    column_name: str,
    *,
    data_type: FeatureDataType,
    category: FeatureCategory,
    is_screening: bool,
    source_table: str = "traj_features",
    formula_or_rule: str,
    null_condition: str,
    description: str,
    denominator_sibling: str | None = None,
    null_on_zero_denominator: bool = False,
    producer_module: str = "evallab.traj",
    construct: str | None = None,
    causal_grade: str | None = None,
    evidence_grade: str | None = None,
    metric_order: int | None = None,
    eligibility_precondition: str | None = None,
    family: str | None = None,
) -> FeatureDefinition:
    """Helper to register a trajectory feature in the global registry."""
    feat = FeatureDefinition(
        column_name=column_name,
        data_type=data_type,
        category=category,
        is_screening=is_screening,
        source_table=source_table,
        formula_or_rule=formula_or_rule,
        null_condition=null_condition,
        description=description,
        denominator_sibling=denominator_sibling,
        null_on_zero_denominator=null_on_zero_denominator,
        producer_module=producer_module,
        construct=construct,
        causal_grade=causal_grade,
        evidence_grade=evidence_grade,
        metric_order=metric_order,
        eligibility_precondition=eligibility_precondition,
        family=family,
    )
    return TRAJECTORY_FEATURE_REGISTRY.register(feat)


# Register all standard trace baseline and mechanical trajectory features
# 1. Identity
register_trajectory_feature(
    "trial_id",
    data_type="VARCHAR",
    category="identity",
    is_screening=False,
    formula_or_rule="Deterministic sha256 digest of (job_id, trial_name)",
    null_condition="Never NULL for valid trials",
    description="Unique identifier for the trial.",
)
register_trajectory_feature(
    "job_id",
    data_type="VARCHAR",
    category="identity",
    is_screening=False,
    formula_or_rule="Job execution identifier from runner metadata",
    null_condition="Never NULL for valid trials",
    description="Identifier of the containing job.",
)
register_trajectory_feature(
    "trial_name",
    data_type="VARCHAR",
    category="identity",
    is_screening=False,
    formula_or_rule="Trial directory or record name",
    null_condition="Never NULL",
    description="Human-readable name of the trial.",
)
register_trajectory_feature(
    "job_name",
    data_type="VARCHAR",
    category="identity",
    is_screening=False,
    formula_or_rule="Job directory or record name",
    null_condition="Never NULL",
    description="Human-readable name of the job.",
)
register_trajectory_feature(
    "task_name",
    data_type="VARCHAR",
    category="identity",
    is_screening=False,
    formula_or_rule="Task identifier extracted from result.json or config.json",
    null_condition="Never NULL",
    description="Evaluated task name.",
)
register_trajectory_feature(
    "agent_name",
    data_type="VARCHAR",
    category="identity",
    is_screening=False,
    formula_or_rule="Agent identifier from trial config/result",
    null_condition="Never NULL",
    description="Name of the evaluated agent.",
)
register_trajectory_feature(
    "agent_version",
    data_type="VARCHAR",
    category="identity",
    is_screening=False,
    formula_or_rule="Agent version string from config or 'unknown'",
    null_condition="Never NULL",
    description="Version string of the agent.",
)
register_trajectory_feature(
    "model_name",
    data_type="VARCHAR",
    category="identity",
    is_screening=False,
    formula_or_rule="Model name from trial config/steps or 'unknown'",
    null_condition="Never NULL",
    description="Underlying foundation model identifier.",
)
register_trajectory_feature(
    "status",
    data_type="VARCHAR",
    category="identity",
    is_screening=False,
    formula_or_rule="'featured' when trajectory.json exists, else 'accounted_unavailable'",
    null_condition="Never NULL",
    description="Availability status of the trajectory.",
)
register_trajectory_feature(
    "unavailable_reason",
    data_type="VARCHAR",
    category="identity",
    is_screening=False,
    formula_or_rule="Reason code when status='accounted_unavailable', else NULL",
    null_condition="NULL when status='featured'",
    description="Detailed reason when trajectory is unavailable.",
)
register_trajectory_feature(
    "source_path",
    data_type="VARCHAR",
    category="identity",
    is_screening=False,
    formula_or_rule="Path to raw source trajectory file",
    null_condition="Never NULL",
    description="Filesystem path of source trajectory.",
)
register_trajectory_feature(
    "source_sha256",
    data_type="VARCHAR",
    category="identity",
    is_screening=False,
    formula_or_rule="SHA-256 hash of source trajectory file",
    null_condition="Never NULL",
    description="Content digest of source trajectory.",
)
register_trajectory_feature(
    "primary_reward",
    data_type="DOUBLE",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Primary numeric reward from result.json (1.0 = pass, 0.0 = fail)",
    null_condition="NULL when result.json reward is absent",
    description="Deterministic primary evaluation reward.",
)
register_trajectory_feature(
    "exception_class",
    data_type="VARCHAR",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Class name of unhandled exception if execution failed",
    null_condition="NULL when trial executed without uncaught exception",
    description="Exception class name on execution failure.",
)
register_trajectory_feature(
    "duration_seconds",
    data_type="DOUBLE",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Wall-clock duration of trial execution in seconds",
    null_condition="NULL when duration is missing or unparseable",
    description="Total execution duration in seconds.",
)

# 2. Step metrics
register_trajectory_feature(
    "step_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Total count of steps in the trajectory outline",
    null_condition="0 by default",
    description="Total number of steps in the trajectory.",
)
register_trajectory_feature(
    "agent_step_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of steps where source='agent'",
    null_condition="0 by default",
    description="Count of agent-initiated steps.",
)
register_trajectory_feature(
    "system_step_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of steps where source='system'",
    null_condition="0 by default",
    description="Count of system/environment steps.",
)
register_trajectory_feature(
    "user_step_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of steps where source='user'",
    null_condition="0 by default",
    description="Count of user-initiated steps.",
)

# 3. Tool & Command metrics
register_trajectory_feature(
    "tool_call_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of steps with non-null tool_name",
    null_condition="0 by default",
    description="Total tool calls executed.",
)
register_trajectory_feature(
    "unique_tools_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of distinct tool_name values across steps",
    null_condition="0 by default",
    description="Number of unique tools invoked.",
)
register_trajectory_feature(
    "repeated_command_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of consecutive identical command executions",
    null_condition="0 by default",
    description="Count of repeated identical commands.",
)

# 4. Error & Recovery metrics
register_trajectory_feature(
    "error_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of steps with true error status (excluding expected probe misses)",
    null_condition="0 by default",
    description="Total error step count.",
)
register_trajectory_feature(
    "recovery_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of successful error-to-success step transitions",
    null_condition="0 by default",
    description="Count of error recoveries.",
)
register_trajectory_feature(
    "is_expected_negative",
    data_type="BOOLEAN",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Boolean flag indicating expected negative probes were observed",
    null_condition="False by default",
    description="Flag indicating expected negative probe presence.",
)
register_trajectory_feature(
    "expected_probe_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of reconnaissance/probe commands that exit non-zero by design",
    null_condition="0 by default",
    description="Count of expected probe misses.",
)
register_trajectory_feature(
    "step_to_first_error",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="1-based step ordinal of the first non-probe error",
    null_condition="NULL when no errors occur",
    description="Step index of first observed error.",
)
register_trajectory_feature(
    "time_to_first_error_seconds",
    data_type="DOUBLE",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Elapsed time from start to first error in seconds",
    null_condition="NULL when no errors occur",
    description="Time to first error onset in seconds.",
)
register_trajectory_feature(
    "recovery_latency_steps",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Step difference between first error and first recovery",
    null_condition="NULL when no recovery occurs",
    description="Steps required to achieve first recovery.",
)
register_trajectory_feature(
    "recovery_latency_seconds",
    data_type="DOUBLE",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Elapsed seconds between first error and first recovery",
    null_condition="NULL when no recovery occurs",
    description="Wall time required to achieve first recovery.",
)
register_trajectory_feature(
    "unrecovered_at_terminal",
    data_type="BOOLEAN",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="True if final step was in error or trial ended without recovering",
    null_condition="False by default",
    description="Flag indicating trial ended in unrecovered error state.",
)

# 5. Intervention provenance
register_trajectory_feature(
    "intervention_category",
    data_type="VARCHAR",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="'autonomous' | 'user_assisted' | 'system_assisted'",
    null_condition="Never NULL ('autonomous' by default)",
    description="Intervention classification of trial execution.",
)
register_trajectory_feature(
    "autonomous_step_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of steps executed by the agent/assistant",
    null_condition="0 by default",
    description="Number of autonomous agent steps.",
)
register_trajectory_feature(
    "assisted_step_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of steps originating from human or user input",
    null_condition="0 by default",
    description="Number of human/user assisted steps.",
)
register_trajectory_feature(
    "intervention_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of human intervention steps occurring at or after initial error",
    null_condition="0 by default",
    description="Count of interventions following error onset.",
)

# 6. State & Edit metrics (State-Journal-grounded)
register_trajectory_feature(
    "state_journal_status",
    data_type="VARCHAR",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="State journal document status: 'available' | 'missing' | 'malformed' | 'not_observed'",
    null_condition="Never NULL ('not_observed' by default)",
    description="Validation status of state-journal and state-diff.",
)
register_trajectory_feature(
    "state_journal_reason",
    data_type="VARCHAR",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Detailed failure or hold reason when state-journal is malformed or missing",
    null_condition="NULL when state-journal status is 'available' or 'not_observed'",
    description="Explanation of state-journal unavailability.",
)
register_trajectory_feature(
    "state_events_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of raw filesystem state events recorded by state-journal",
    null_condition="0 by default",
    description="Total count of state journal events.",
)
register_trajectory_feature(
    "state_mutations_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of mutating state events (added, modified, deleted)",
    null_condition="0 by default",
    description="Total count of filesystem mutations.",
)
register_trajectory_feature(
    "state_files_created_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of files added in state-diff",
    null_condition="0 by default",
    description="Count of files created during trial.",
)
register_trajectory_feature(
    "state_files_modified_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of existing files modified in state-diff",
    null_condition="0 by default",
    description="Count of files modified during trial.",
)
register_trajectory_feature(
    "state_files_deleted_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of files deleted in state-diff",
    null_condition="0 by default",
    description="Count of files deleted during trial.",
)
register_trajectory_feature(
    "state_diff_observed",
    data_type="BOOLEAN",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="True when validated state-diff.json was loaded",
    null_condition="False by default",
    description="Flag indicating validated state-diff observation.",
)
register_trajectory_feature(
    "state_diff_path_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of distinct paths recorded in state-diff.json",
    null_condition="0 by default",
    description="Number of distinct paths changed in state diff.",
)
register_trajectory_feature(
    "state_diff_bytes_delta",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Net sum of size deltas (after - before) across all state-diff changes",
    null_condition="0 by default",
    description="Net byte size change across all changed paths.",
)
register_trajectory_feature(
    "edit_tool_call_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of tool calls executing file edits or writes",
    null_condition="0 by default",
    description="Denominator for edit efficiency screening.",
)
register_trajectory_feature(
    "edit_efficiency_screening",
    data_type="DOUBLE",
    category="screening_heuristic",
    is_screening=True,
    formula_or_rule="state_diff_path_count / edit_tool_call_count",
    null_condition="NULL when edit_tool_call_count == 0",
    denominator_sibling="edit_tool_call_count",
    null_on_zero_denominator=True,
    description="Ratio of distinct changed paths to edit tool calls.",
)
register_trajectory_feature(
    "unobserved_state_mutations_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of state-diff mutations on paths never referenced in agent tool calls",
    null_condition="0 by default",
    description="Count of filesystem changes with no corresponding agent tool reference.",
)

# 7. Reference validity metrics
register_trajectory_feature(
    "path_reference_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of file/path arguments passed to agent tools",
    null_condition="0 by default",
    description="Denominator for path reference validity screening.",
)
register_trajectory_feature(
    "valid_path_reference_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of path references that resolved or executed without missing-file errors",
    null_condition="0 by default",
    description="Count of valid path references.",
)
register_trajectory_feature(
    "invalid_path_reference_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of path references that produced missing-file errors",
    null_condition="0 by default",
    description="Count of invalid or missing path references.",
)
register_trajectory_feature(
    "path_reference_validity_rate_screening",
    data_type="DOUBLE",
    category="screening_heuristic",
    is_screening=True,
    formula_or_rule="valid_path_reference_count / path_reference_count",
    null_condition="NULL when path_reference_count == 0",
    denominator_sibling="path_reference_count",
    null_on_zero_denominator=True,
    description="Ratio of valid path references to total path references.",
)
register_trajectory_feature(
    "citation_reference_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of citations cited in trajectory / evidence pack",
    null_condition="0 by default",
    description="Denominator for citation reference validity screening.",
)
register_trajectory_feature(
    "valid_citation_reference_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of citations with valid path and 64-hex sha256 digest",
    null_condition="0 by default",
    description="Count of valid citation references.",
)
register_trajectory_feature(
    "invalid_citation_reference_count",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Count of citations with missing or malformed digest",
    null_condition="0 by default",
    description="Count of invalid citation references.",
)
register_trajectory_feature(
    "citation_reference_validity_rate_screening",
    data_type="DOUBLE",
    category="screening_heuristic",
    is_screening=True,
    formula_or_rule="valid_citation_reference_count / citation_reference_count",
    null_condition="NULL when citation_reference_count == 0",
    denominator_sibling="citation_reference_count",
    null_on_zero_denominator=True,
    description="Ratio of valid citations to total citations.",
)

# 8. Screening Rates & Ratios (with explicit denominator siblings)
register_trajectory_feature(
    "linear_innocence_screening",
    data_type="DOUBLE",
    category="screening_heuristic",
    is_screening=True,
    formula_or_rule="unique_tools_count / tool_call_count",
    null_condition="NULL when tool_call_count == 0",
    denominator_sibling="tool_call_count",
    null_on_zero_denominator=True,
    description="Linear innocence screening metric (unique tools / tool calls).",
)
register_trajectory_feature(
    "tool_error_rate_screening",
    data_type="DOUBLE",
    category="screening_heuristic",
    is_screening=True,
    formula_or_rule="error_count / tool_call_count",
    null_condition="NULL when tool_call_count == 0",
    denominator_sibling="tool_call_count",
    null_on_zero_denominator=True,
    description="Tool error rate screening metric (errors / tool calls).",
)
register_trajectory_feature(
    "recovery_rate_screening",
    data_type="DOUBLE",
    category="screening_heuristic",
    is_screening=True,
    formula_or_rule="recovery_count / error_count",
    null_condition="NULL when error_count == 0",
    denominator_sibling="error_count",
    null_on_zero_denominator=True,
    description="Ratio of successful recoveries to total errors.",
)
register_trajectory_feature(
    "autonomous_step_ratio_screening",
    data_type="DOUBLE",
    category="screening_heuristic",
    is_screening=True,
    formula_or_rule="autonomous_step_count / step_count",
    null_condition="NULL when step_count == 0",
    denominator_sibling="step_count",
    null_on_zero_denominator=True,
    description="Ratio of autonomous steps to total steps.",
)
register_trajectory_feature(
    "assisted_step_ratio_screening",
    data_type="DOUBLE",
    category="screening_heuristic",
    is_screening=True,
    formula_or_rule="assisted_step_count / step_count",
    null_condition="NULL when step_count == 0",
    denominator_sibling="step_count",
    null_on_zero_denominator=True,
    description="Ratio of human-assisted steps to total steps.",
)
register_trajectory_feature(
    "cache_hit_rate_screening",
    data_type="DOUBLE",
    category="screening_heuristic",
    is_screening=True,
    formula_or_rule="cached_tokens / prompt_tokens",
    null_condition="NULL when prompt_tokens == 0 or prompt_tokens is NULL",
    denominator_sibling="prompt_tokens",
    null_on_zero_denominator=True,
    description="Prompt token cache hit rate screening.",
)
register_trajectory_feature(
    "subagent_overhead_ratio_screening",
    data_type="DOUBLE",
    category="screening_heuristic",
    is_screening=True,
    formula_or_rule="subagent_steps / step_count",
    null_condition="NULL when step_count == 0",
    denominator_sibling="step_count",
    null_on_zero_denominator=True,
    description="Ratio of subagent steps to total steps.",
)
register_trajectory_feature(
    "context_burn_velocity_screening",
    data_type="DOUBLE",
    category="screening_heuristic",
    is_screening=True,
    formula_or_rule="Regression slope of prompt_tokens over step_ordinal",
    null_condition="NULL when fewer than 2 steps have prompt_tokens or zero variance",
    description="Context burn velocity screening slope.",
)
register_trajectory_feature(
    "max_exit_code_cascade_screening",
    data_type="BIGINT",
    category="screening_heuristic",
    is_screening=True,
    formula_or_rule="Maximum consecutive run of steps with non-zero exit codes or errors",
    null_condition="0 by default",
    description="Longest consecutive exit-code error streak.",
)

# 9. Token & Cost metrics
register_trajectory_feature(
    "prompt_tokens",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Sum of prompt tokens across all steps",
    null_condition="NULL when not reported",
    description="Total prompt tokens consumed.",
)
register_trajectory_feature(
    "completion_tokens",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Sum of completion tokens across all steps",
    null_condition="NULL when not reported",
    description="Total completion tokens generated.",
)
register_trajectory_feature(
    "cached_tokens",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Sum of prompt tokens read from cache",
    null_condition="NULL when not reported",
    description="Total cached prompt tokens.",
)
register_trajectory_feature(
    "total_tokens",
    data_type="BIGINT",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="prompt_tokens + completion_tokens",
    null_condition="NULL when either prompt or completion tokens are missing",
    description="Total token consumption.",
)
register_trajectory_feature(
    "cost_usd",
    data_type="DOUBLE",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Sum of dollar cost across all steps",
    null_condition="NULL when not reported",
    description="Total execution cost in USD.",
)

# 10. Loop Suspicion metrics
register_trajectory_feature(
    "loop_suspicion_score",
    data_type="DOUBLE",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Weighted heuristic score based on repeated commands, errors, and cycles",
    null_condition="0.0 by default",
    description="Composite loop suspicion score [0.0, 1.0].",
)
register_trajectory_feature(
    "loop_suspicion_detected",
    data_type="BOOLEAN",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="Boolean flag indicating loop_suspicion_score >= 0.5",
    null_condition="False by default",
    description="Thresholded loop detection flag.",
)
register_trajectory_feature(
    "loop_reasons_json",
    data_type="VARCHAR",
    category="mechanical_fact",
    is_screening=False,
    formula_or_rule="JSON array of detected loop pattern reasons",
    null_condition="'[]' by default",
    description="List of detected loop trigger reasons in JSON.",
)
register_trajectory_feature(
    "created_at",
    data_type="VARCHAR",
    category="identity",
    is_screening=False,
    formula_or_rule="ISO-8601 UTC timestamp of record creation",
    null_condition="Never NULL",
    description="Timestamp when baseline record was generated.",
)


def verify_feature_registry() -> list[str]:
    """Producer CI self-test verifying registry completeness and contract invariants."""
    errors: list[str] = []
    for feat in TRAJECTORY_FEATURE_REGISTRY.all_features().values():
        errors.extend(feat.validate_contract())
    return errors


# =============================================================================
# Benchmark-Specific Feature Registrations (Action Memory, Tool Composition, Error Recovery)
# =============================================================================

# -----------------------------------------------------------------------------
# 1. Action Memory (action-memory-v1 / Context & Actionable Memory)
# -----------------------------------------------------------------------------
register_trajectory_feature(
    "total_tool_calls",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Count of tool call requests initiated during trial",
    null_condition="0 by default",
    description="Total count of tool requests initiated in benchmark trial.",
    producer_module="evallab.interpretation.producers",
    construct="Benchmark Observables",
    causal_grade="C0",
    evidence_grade="Grade A",
    metric_order=1,
)
register_trajectory_feature(
    "raw_binding_opportunities",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Opportunity count for target entity binding from contract",
    null_condition="Never NULL for action-memory trials",
    description="Opportunity count for target entity binding (denominator for binding_survival_rate).",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "raw_conflicting_opportunities",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Opportunity count for conflicting/stale entity binding from contract",
    null_condition="Never NULL for action-memory trials",
    description="Opportunity count for conflicting entity binding (denominator for stale_value_override_rate).",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "bound_target_entity",
    data_type="VARCHAR",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Entity identifier bound in mutations",
    null_condition="NULL if no entity mutation occurred",
    description="Target entity identifier mutated by the agent.",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C0",
    family="action-memory-v1",
)
register_trajectory_feature(
    "bound_target_attribute",
    data_type="VARCHAR",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Attribute identifier bound in mutations",
    null_condition="NULL if no attribute mutation occurred",
    description="Target attribute identifier mutated by the agent.",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C0",
    family="action-memory-v1",
)
register_trajectory_feature(
    "bound_target_value",
    data_type="VARCHAR",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Value string bound in mutations",
    null_condition="NULL if no value was bound",
    description="Target value bound in state mutations.",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C0",
    family="action-memory-v1",
)
register_trajectory_feature(
    "binding_matched",
    data_type="BOOLEAN",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Boolean flag indicating bound value matched latest target entity ground truth",
    null_condition="False if unfulfilled",
    description="Whether the bound value matched the latest target value.",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "stale_value_bound",
    data_type="BOOLEAN",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Boolean flag indicating bound value matched initial/stale value instead of latest",
    null_condition="False if unfulfilled",
    description="Whether the agent bound an outdated/stale value.",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "schema_conformance_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="valid_schema_calls / total_tool_calls",
    null_condition="NULL when total_tool_calls == 0",
    description="Rate of tool requests conforming to benchmark schema without syntax/schema error.",
    denominator_sibling="total_tool_calls",
    null_on_zero_denominator=True,
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=2,
    eligibility_precondition="total_tool_calls > 0",
    family="action-memory-v1",
)
register_trajectory_feature(
    "binding_survival_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="binding_matched / raw_binding_opportunities",
    null_condition="NULL when raw_binding_opportunities == 0",
    description="Fraction of target entity binding opportunities correctly surviving to final state.",
    denominator_sibling="raw_binding_opportunities",
    null_on_zero_denominator=True,
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=2,
    eligibility_precondition="raw_binding_opportunities > 0",
    family="action-memory-v1",
)
register_trajectory_feature(
    "stale_value_override_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="(1.0 - stale_value_bound) / raw_conflicting_opportunities if raw_conflicting_opportunities > 0",
    null_condition="NULL when raw_conflicting_opportunities == 0",
    description="Rate of successfully overriding stale memory values when conflicting updates occurred.",
    denominator_sibling="raw_conflicting_opportunities",
    null_on_zero_denominator=True,
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=2,
    eligibility_precondition="raw_conflicting_opportunities > 0",
    family="action-memory-v1",
)
register_trajectory_feature(
    "context_burn_velocity",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="OLS slope of prompt tokens across trajectory step sequence",
    null_condition="NULL when step count < 2",
    description="Prompt token accumulation velocity (tokens per step slope).",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C0",
    metric_order=2,
    family="action-memory-v1",
)
register_trajectory_feature(
    "occupancy_first_failure",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Context byte/token occupancy ratio at step of first failure",
    null_condition="NULL if no failure occurred",
    description="Context buffer occupancy fraction when first failure was observed.",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C0",
    metric_order=2,
    family="action-memory-v1",
)

# -----------------------------------------------------------------------------
# 2. Tool Composition (mcp-funcdag-v1 / Tool Selection & Composition)
# -----------------------------------------------------------------------------
register_trajectory_feature(
    "required_dag_edges",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Required dependency edges in composition DAG from contract",
    null_condition="Never NULL for mcp-funcdag trials",
    description="Required dependency edge count in the composition DAG.",
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="mcp-funcdag-v1",
)
register_trajectory_feature(
    "required_value_bindings",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Required intermediate/sink value bindings from contract",
    null_condition="Never NULL for mcp-funcdag trials",
    description="Required value binding count across DAG nodes.",
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="mcp-funcdag-v1",
)
register_trajectory_feature(
    "executed_dag_edges",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Count of successfully executed DAG dependency edges",
    null_condition="0 by default",
    description="Count of successfully executed composition DAG edges.",
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C1",
    family="mcp-funcdag-v1",
)
register_trajectory_feature(
    "correct_value_bindings",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Count of correctly bound intermediate/sink values",
    null_condition="0 by default",
    description="Count of correctly bound values in composition chain.",
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C1",
    family="mcp-funcdag-v1",
)
register_trajectory_feature(
    "redundant_tool_calls",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Count of repeated identical tool calls or distractor invocations",
    null_condition="0 by default",
    description="Count of redundant or distractor tool invocations.",
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C0",
    family="mcp-funcdag-v1",
)
register_trajectory_feature(
    "cycle_violations",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Count of cyclic dependency edges detected during DAG execution",
    null_condition="0 by default",
    description="Count of cyclic tool call dependencies violating DAG acyclicity constraint.",
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="mcp-funcdag-v1",
)
register_trajectory_feature(
    "satisfied_edge_opportunities",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="min(executed_dag_edges, required_dag_edges)",
    null_condition="0 by default",
    description="Count of satisfied edge opportunities eligible for latency analysis.",
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C0",
    family="mcp-funcdag-v1",
)
register_trajectory_feature(
    "first_edge_step",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="1-based step index when first DAG edge was executed",
    null_condition="NULL if no DAG edges executed",
    description="Step index of first composition edge execution.",
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C0",
    family="mcp-funcdag-v1",
)
register_trajectory_feature(
    "value_propagation_accuracy",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="correct_value_bindings / required_value_bindings",
    null_condition="NULL when required_value_bindings == 0",
    description="Accuracy of value propagation through intermediate and sink nodes.",
    denominator_sibling="required_value_bindings",
    null_on_zero_denominator=True,
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=2,
    eligibility_precondition="required_value_bindings > 0",
    family="mcp-funcdag-v1",
)
register_trajectory_feature(
    "dag_edge_conformance_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="min(executed_dag_edges, required_dag_edges) / required_dag_edges",
    null_condition="NULL when required_dag_edges == 0",
    description="Conformance rate of executed dependency edges against required DAG schema.",
    denominator_sibling="required_dag_edges",
    null_on_zero_denominator=True,
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=2,
    eligibility_precondition="required_dag_edges > 0",
    family="mcp-funcdag-v1",
)
register_trajectory_feature(
    "redundant_call_ratio",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="redundant_tool_calls / total_tool_calls",
    null_condition="NULL when total_tool_calls == 0",
    description="Ratio of redundant/distractor tool calls to total tool requests.",
    denominator_sibling="total_tool_calls",
    null_on_zero_denominator=True,
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C0",
    metric_order=2,
    eligibility_precondition="total_tool_calls > 0",
    family="mcp-funcdag-v1",
)
register_trajectory_feature(
    "first_edge_latency",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="first_edge_step if satisfied_edge_opportunities > 0 else NULL",
    null_condition="NULL when satisfied_edge_opportunities == 0",
    description="Latency (in steps) to execute first composition edge under satisfied opportunities.",
    denominator_sibling="satisfied_edge_opportunities",
    null_on_zero_denominator=True,
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C0",
    metric_order=2,
    eligibility_precondition="satisfied_edge_opportunities > 0",
    family="mcp-funcdag-v1",
)

# -----------------------------------------------------------------------------
# 3. Error Recovery (mcp-recovery-v1 / Error Detection & Autonomous Recovery)
# -----------------------------------------------------------------------------
register_trajectory_feature(
    "injected_fault_record",
    data_type="VARCHAR",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="JSON array of injected fault classes",
    null_condition="NULL if no faults injected",
    description="Record of injected fault classes in trial.",
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C3",
    evidence_grade="Grade A",
    metric_order=1,
    family="mcp-recovery-v1",
)
register_trajectory_feature(
    "injected_fault_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Count of fault injection events",
    null_condition="0 by default",
    description="Number of faults injected during trial.",
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C3",
    evidence_grade="Grade A",
    metric_order=1,
    family="mcp-recovery-v1",
)
register_trajectory_feature(
    "fault_detected_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Count of faults acknowledged by argument modification or tool switch",
    null_condition="0 by default",
    description="Number of faults actively detected and handled by the agent.",
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C2",
    evidence_grade="Grade A",
    metric_order=1,
    family="mcp-recovery-v1",
)
register_trajectory_feature(
    "post_fault_retries",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Count of tool call attempts following a fault injection",
    null_condition="0 by default",
    description="Number of retry attempts executed after fault occurrence.",
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C0",
    family="mcp-recovery-v1",
)
register_trajectory_feature(
    "blind_retries",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Count of retries repeating exact identical failing arguments without change",
    null_condition="0 by default",
    description="Number of blind retries repeating identical failing parameters.",
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C0",
    family="mcp-recovery-v1",
)
register_trajectory_feature(
    "certified_recovered_faults",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Count of faults recovered with final invariants verified",
    null_condition="0 by default",
    description="Number of faults certified as recovered with verified final state.",
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C3",
    evidence_grade="Grade A",
    metric_order=1,
    family="mcp-recovery-v1",
)
register_trajectory_feature(
    "step_to_first_fault",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="1-based step index of first injected fault",
    null_condition="NULL if no faults injected",
    description="Step index of first injected fault.",
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C0",
    family="mcp-recovery-v1",
)
register_trajectory_feature(
    "step_to_recovery",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="1-based step index of successful recovery call",
    null_condition="NULL if no recovery occurred",
    description="Step index when successful recovery call completed.",
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C0",
    family="mcp-recovery-v1",
)
register_trajectory_feature(
    "autonomous_recovery_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="certified_recovered_faults / injected_fault_count",
    null_condition="NULL when injected_fault_count == 0",
    description="Rate of autonomous certified fault recoveries over injected faults.",
    denominator_sibling="injected_fault_count",
    null_on_zero_denominator=True,
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C3",
    evidence_grade="Grade A",
    metric_order=2,
    eligibility_precondition="injected_fault_count > 0",
    family="mcp-recovery-v1",
)
register_trajectory_feature(
    "fault_detection_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="min(fault_detected_count, injected_fault_count) / injected_fault_count",
    null_condition="NULL when injected_fault_count == 0",
    description="Rate of fault detection and diagnostic reaction over injected faults.",
    denominator_sibling="injected_fault_count",
    null_on_zero_denominator=True,
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C2",
    evidence_grade="Grade A",
    metric_order=2,
    eligibility_precondition="injected_fault_count > 0",
    family="mcp-recovery-v1",
)
register_trajectory_feature(
    "blind_retry_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="blind_retries / post_fault_retries",
    null_condition="NULL when post_fault_retries == 0",
    description="Fraction of retries repeating exact failing arguments blindly.",
    denominator_sibling="post_fault_retries",
    null_on_zero_denominator=True,
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C0",
    metric_order=2,
    eligibility_precondition="post_fault_retries > 0",
    family="mcp-recovery-v1",
)
register_trajectory_feature(
    "fault_recovery_latency",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="(step_to_recovery - step_to_first_fault) if certified_recovered_faults > 0 else NULL",
    null_condition="NULL when certified_recovered_faults == 0",
    description="Step latency from fault injection to certified recovery under recovered trials.",
    denominator_sibling="certified_recovered_faults",
    null_on_zero_denominator=True,
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C0",
    metric_order=2,
    eligibility_precondition="certified_recovered_faults > 0",
    family="mcp-recovery-v1",
)


def compute_benchmark_feature_yield(
    records: list[dict[str, Any]],
    family: str | None = None,
) -> dict[str, Any]:
    """Compute per-feature yield and coverage diagnostics over benchmark records."""
    if not records:
        return {
            "total_records": 0,
            "family": family,
            "feature_stats": {},
        }

    total_count = len(records)
    target_features = (
        TRAJECTORY_FEATURE_REGISTRY.by_family(family)
        if family
        else TRAJECTORY_FEATURE_REGISTRY.all_features()
    )

    feature_stats: dict[str, dict[str, Any]] = {}
    for col_name, feat in target_features.items():
        non_null_count = sum(1 for r in records if r.get(col_name) is not None)
        null_count = total_count - non_null_count
        yield_pct = (non_null_count / total_count * 100.0) if total_count > 0 else 0.0

        feature_stats[col_name] = {
            "total": total_count,
            "non_null": non_null_count,
            "null": null_count,
            "yield_pct": round(yield_pct, 2),
            "category": feat.category,
            "construct": feat.construct,
            "causal_grade": feat.causal_grade,
            "evidence_grade": feat.evidence_grade,
            "null_on_zero_denominator": feat.null_on_zero_denominator,
            "denominator_sibling": feat.denominator_sibling,
        }

    return {
        "total_records": total_count,
        "family": family,
        "feature_stats": feature_stats,
    }


def verify_benchmark_feature_coverage(
    records: list[dict[str, Any]],
    family: str,
) -> dict[str, Any]:
    """Verify that all registered features for a family are present in records with expected nullity."""
    yield_diag = compute_benchmark_feature_yield(records, family=family)
    missing_features: list[str] = []
    zero_yield_features: list[str] = []

    family_feats = TRAJECTORY_FEATURE_REGISTRY.by_family(family)
    stats = yield_diag["feature_stats"]

    for col_name, feat in family_feats.items():
        if col_name not in stats:
            missing_features.append(col_name)
        elif (
            stats[col_name]["non_null"] == 0
            and not feat.null_on_zero_denominator
            and feat.category == "benchmark_l1_fact"
        ):
            # L1 facts should generally have non-zero yield in valid runs
            zero_yield_features.append(col_name)
    passed = len(missing_features) == 0 and len(zero_yield_features) == 0
    return {
        "family": family,
        "passed": passed,
        "missing_features": missing_features,
        "zero_yield_features": zero_yield_features,
        "diagnostics": yield_diag,
    }
