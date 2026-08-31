"""Explicit feature registry and producer CI validation for trajectory baseline metrics.

Every mechanical fact, screening heuristic, rate, and ratio column in v_trace_baseline
and TrajectoryFeatures must be explicitly registered, classified by provenance category,
typed, documented, and declare its denominator sibling for null-on-zero invariants.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import pyarrow as pa

from evallab.analysis_capability import FeatureContractRow


def compute_prompt_cache_hit_rate(
    step_tokens: Sequence[int] | None,
    cached_step_tokens: Sequence[int] | None,
) -> float | None:
    """Strict token-weighted prompt cache hit rate.

    Returns float(sum(cached_step_tokens) / sum(step_tokens)).
    Strictly returns None (NULL) / fails closed when:
    - step_tokens or cached_step_tokens is None or empty
    - sequence lengths are misaligned (len(cached_step_tokens) != len(step_tokens))
    - any element in step_tokens or cached_step_tokens is negative (< 0)
    - total prompt tokens <= 0
    - total cached tokens exceeds total prompt tokens (sum(cached_step_tokens) > sum(step_tokens))
    """
    if not step_tokens or not cached_step_tokens:
        return None
    if len(cached_step_tokens) != len(step_tokens):
        return None
    if any(s < 0 for s in step_tokens) or any(c < 0 for c in cached_step_tokens):
        return None
    total_prompt = sum(step_tokens)
    total_cached = sum(cached_step_tokens)
    if total_prompt <= 0:
        return None
    if total_cached > total_prompt:
        return None
    return float(total_cached / total_prompt)


FeatureCategory = Literal[
    "identity",
    "mechanical_fact",
    "screening_heuristic",
    "benchmark_ground_truth",
    "benchmark_l1_fact",
    "benchmark_l2_metric",
]
FeatureDataType = Literal["VARCHAR", "BIGINT", "DOUBLE", "BOOLEAN"]
DenominatorPolicy = Literal["required", "not_applicable"]
VerdictCoupling = Literal["defines", "correlates", "independent", "not_applicable"]


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
    denominator_policy: DenominatorPolicy | None = None
    declared_inputs: tuple[str, ...] | None = None
    available_before_verdict: bool | None = None
    verdict_coupling: VerdictCoupling | None = None
    coupling_basis: str | None = None
    binary_projection: bool = False
    is_new_feature: bool = False
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
        if self.verdict_coupling is not None:
            if self.verdict_coupling not in (
                "defines",
                "correlates",
                "independent",
                "not_applicable",
            ):
                errors.append(
                    f"Feature {self.column_name!r} has invalid verdict_coupling={self.verdict_coupling!r}"
                )
            if self.verdict_coupling in ("defines", "correlates") and not (
                self.coupling_basis and self.coupling_basis.strip()
            ):
                errors.append(
                    f"Feature {self.column_name!r} with verdict_coupling={self.verdict_coupling!r} requires non-empty coupling_basis"
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


def audit_denominator_policy(feature: FeatureDefinition) -> str | None:
    """Return the T1.1 registry verdict for an explicit denominator declaration."""
    if feature.denominator_policy is None:
        return "MISSING_DENOMINATOR_APPLICABILITY_DECLARATION"
    if feature.denominator_policy == "required" and not feature.denominator_sibling:
        return "MISSING_DENOMINATOR_DECLARATION"
    if feature.denominator_policy == "required" and not feature.null_on_zero_denominator:
        return "MISSING_NULL_ON_ZERO_DECLARATION"
    if feature.denominator_policy == "not_applicable" and (
        feature.denominator_sibling or feature.null_on_zero_denominator
    ):
        return "INVALID_DENOMINATOR_DECLARATION"
    return None


def feature_contract_row(feature: FeatureDefinition) -> FeatureContractRow:
    """Adapt registry metadata to the immutable T1.1 consumer contract."""
    return FeatureContractRow(
        feature_name=feature.column_name,
        is_new_feature=feature.is_new_feature,
        declared_inputs=feature.declared_inputs,
        available_before_verdict=feature.available_before_verdict,
        denominator_policy=feature.denominator_policy,
        denominator_sibling=feature.denominator_sibling,
        null_on_zero_denominator=feature.null_on_zero_denominator,
        binary_projection=feature.binary_projection,
    )


def audit_registry_denominator_policies() -> dict[str, str]:
    """Report legacy denominator-policy debt without tightening import-time validation."""
    return {
        feature.column_name: verdict
        for feature in TRAJECTORY_FEATURE_REGISTRY.all_features().values()
        if (verdict := audit_denominator_policy(feature)) is not None
    }


def audit_predictor_eligibility(
    feature: FeatureDefinition, *, strict_independence: bool = False
) -> str | None:
    """Return the registry verdict for candidate predictor eligibility.

    Refuses predictor eligibility when:
    - temporal availability is undeclared or post-verdict (distinguished from verdict coupling)
    - verdict coupling is undeclared or 'defines'
    - coupling is 'defines' or 'correlates' but lacks an evidence basis
    - feature is 'not_applicable' (e.g. identity / projection metadata)
    """
    if feature.available_before_verdict is None:
        return "MISSING_TEMPORAL_AVAILABILITY"
    if feature.available_before_verdict is False:
        return "POST_VERDICT_TEMPORAL_VIOLATION"
    if feature.verdict_coupling is None:
        return "UNDECLARED_VERDICT_COUPLING"
    if feature.verdict_coupling not in ("defines", "correlates", "independent", "not_applicable"):
        return "INVALID_VERDICT_COUPLING"
    if feature.verdict_coupling == "defines":
        return "REWARD_DEFINITION_LEAKAGE"
    if feature.verdict_coupling in ("defines", "correlates") and not (
        feature.coupling_basis and feature.coupling_basis.strip()
    ):
        return "MISSING_COUPLING_EVIDENCE_BASIS"
    if feature.verdict_coupling == "not_applicable":
        return "NOT_APPLICABLE_FOR_PREDICTION"
    if strict_independence and feature.verdict_coupling == "correlates":
        return "VERDICT_CORRELATED"
    return None


def audit_verdict_coupling(feature: FeatureDefinition) -> str | None:
    """Return the verdict-coupling audit code for a feature."""
    if feature.verdict_coupling is None:
        return "UNDECLARED_VERDICT_COUPLING"
    if feature.verdict_coupling not in ("defines", "correlates", "independent", "not_applicable"):
        return "INVALID_VERDICT_COUPLING"
    if feature.verdict_coupling in ("defines", "correlates") and not (
        feature.coupling_basis and feature.coupling_basis.strip()
    ):
        return "MISSING_COUPLING_EVIDENCE_BASIS"
    return None


@dataclass(frozen=True)
class FeatureAnalysisEligibility:
    """Explicit allowed analysis roles derived from one governed feature contract."""

    outcome_allowed: bool
    predictor_allowed: bool
    descriptive_allowed: bool
    predictor_refusal: str | None


def feature_analysis_eligibility(feature: FeatureDefinition) -> FeatureAnalysisEligibility:
    """Resolve outcome, predictor, and descriptive eligibility without guessing."""
    coupling_audit = audit_verdict_coupling(feature)
    denominator_audit = audit_denominator_policy(feature)
    coupling_governed = coupling_audit is None
    fully_governed = coupling_governed and denominator_audit is None
    descriptive_allowed = fully_governed and feature.verdict_coupling != "not_applicable"
    outcome_allowed = (
        coupling_governed
        and feature.verdict_coupling != "not_applicable"
        and feature.category != "identity"
    )
    predictor_refusal = audit_predictor_eligibility(feature, strict_independence=True)
    return FeatureAnalysisEligibility(
        outcome_allowed=outcome_allowed,
        predictor_allowed=fully_governed and predictor_refusal is None,
        descriptive_allowed=descriptive_allowed,
        predictor_refusal=predictor_refusal,
    )


def audit_registry_predictor_eligibility(*, family: str | None = None) -> dict[str, str]:
    """Report predictor eligibility refusals across registered features."""
    target = (
        TRAJECTORY_FEATURE_REGISTRY.by_family(family)
        if family
        else TRAJECTORY_FEATURE_REGISTRY.all_features()
    )
    return {
        feature.column_name: verdict
        for feature in target.values()
        if (verdict := audit_predictor_eligibility(feature)) is not None
    }


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
    denominator_policy: DenominatorPolicy | None = None,
    declared_inputs: tuple[str, ...] | None = None,
    available_before_verdict: bool | None = None,
    verdict_coupling: VerdictCoupling | None = None,
    coupling_basis: str | None = None,
    verdict_coupling_basis: str | None = None,
    binary_projection: bool = False,
    is_new_feature: bool = False,
    producer_module: str = "evallab.traj",
    construct: str | None = None,
    causal_grade: str | None = None,
    evidence_grade: str | None = None,
    metric_order: int | None = None,
    eligibility_precondition: str | None = None,
    family: str | None = None,
) -> FeatureDefinition:
    """Helper to register a trajectory feature in the global registry."""
    actual_coupling_basis = coupling_basis if coupling_basis is not None else verdict_coupling_basis
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
        denominator_policy=denominator_policy,
        declared_inputs=declared_inputs,
        available_before_verdict=available_before_verdict,
        verdict_coupling=verdict_coupling,
        coupling_basis=actual_coupling_basis,
        binary_projection=binary_projection,
        is_new_feature=is_new_feature,
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
    available_before_verdict=False,
    verdict_coupling="defines",
    coupling_basis="Primary deterministic reward from verifier (1.0 = pass, 0.0 = fail)",
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
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Total tool requests initiated during trial correlates with search depth and budget consumption",
    producer_module="evallab.interpretation.producers",
    construct="Benchmark Observables",
    causal_grade="C0",
    evidence_grade="Grade A",
    metric_order=1,
)
register_trajectory_feature(
    "prompt_tokens_per_step",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="prompt_tokens / step_count",
    null_condition="NULL when step_count == 0",
    description="Average prompt tokens consumed per execution step (C0 manipulation check).",
    denominator_sibling="step_count",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("prompt_tokens", "step_count"),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.interpretation.producers",
    construct="Benchmark Observables",
    causal_grade="C0",
    evidence_grade="Grade A",
    metric_order=1,
)
register_trajectory_feature(
    "prompt_cache_hit_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="cached_tokens / prompt_tokens",
    null_condition="NULL when prompt_tokens == 0 or prompt_tokens is NULL",
    description="Fraction of prompt tokens served from prefix cache (C1 manipulation check for matched padding arms).",
    denominator_sibling="prompt_tokens",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("cached_tokens", "prompt_tokens"),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.interpretation.producers",
    construct="Benchmark Observables",
    causal_grade="C1",
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
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
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
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
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
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="not_applicable",
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
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="not_applicable",
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
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="not_applicable",
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
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="Verifier reward contract directly evaluates whether bound target value matches ground truth latest value",
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
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Binding an outdated stale value indicates context override failure and correlates strongly with zero verifier reward",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "expected_handle_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Count of expected context retrieval handles declared in contract",
    null_condition="0 by default",
    description="Count of expected context retrieval handles declared in benchmark contract.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "valid_handle_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Count of requested retrieval handles matching expected contract handle universe",
    null_condition="0 by default",
    description="Count of requested context retrieval handles matching expected contract handle universe.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "unknown_handle_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Count of requested retrieval handles not present in declared contract universe",
    null_condition="0 by default",
    description="Count of requested context retrieval handles not present in declared contract universe (hallucination/corruption check).",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "duplicate_handle_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="total_handle_requests - distinct_valid_handles",
    null_condition="0 by default",
    description="Count of repeated redundant requests for identical retrieval handles.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C0",
    evidence_grade="Grade A",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "issued_handle_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Count of total handle retrieval requests issued during trial (valid + unknown + duplicate)",
    null_condition="0 by default",
    description="Total count of context retrieval handles requested by the agent.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Total handle retrieval requests correlate with search breadth and trial budget consumption",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "handle_set_match",
    data_type="BOOLEAN",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="expected_handle_universe <= set(observed_handles)",
    null_condition="False if unfulfilled",
    description="Boolean indicating whether all expected contract retrieval handles were requested.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="Verifier contract requires complete retrieval of expected contract handle universe for task success",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "handle_order_match",
    data_type="BOOLEAN",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="observed_handles == expected_handle_sequence",
    null_condition="False if unfulfilled",
    description="Boolean indicating whether retrieval handles appeared in strictly conformed canonical chronological order.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="Verifier contract requires canonical chronological handle retrieval order for task success",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "handle_coverage_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="valid_handle_count / expected_handle_count",
    null_condition="NULL when expected_handle_count == 0",
    description="Fraction of expected contract retrieval handles successfully requested without omission.",
    denominator_sibling="expected_handle_count",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("valid_handle_count", "expected_handle_count"),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="Verifier reward is directly conditioned on complete retrieval coverage (handle_coverage_rate == 1.0)",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=2,
    eligibility_precondition="expected_handle_count > 0",
    family="action-memory-v1",
)
register_trajectory_feature(
    "handle_issuance_ratio",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="issued_handle_count / expected_handle_count",
    null_condition="NULL when expected_handle_count == 0",
    description="Ratio of total issued retrieval handles to expected contract handles (measures over/under-issuance).",
    denominator_sibling="expected_handle_count",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("issued_handle_count", "expected_handle_count"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Ratio of total issued handles to expected contract handles correlates with retrieval efficiency and thrashing",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=2,
    eligibility_precondition="expected_handle_count > 0",
    family="action-memory-v1",
)
register_trajectory_feature(
    "handle_order_concordance",
    data_type="BOOLEAN",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="atif_handles == event_handles when both available, NULL if ATIF absent",
    null_condition="NULL when ATIF trajectory is absent or unavailable",
    description="Whether ATIF-issued and benchmark-event-issued handle sequences match in exact chronological order.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Concordance between ATIF trace and benchmark events validates capture integrity without defining verifier reward",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "retrieval_authority",
    data_type="VARCHAR",
    category="identity",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Declared authority for handle retrieval sequence ('benchmark_events', 'atif_trajectory', or 'unavailable')",
    null_condition="Never NULL",
    description="Declared source of truth for context handle retrieval sequences.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="not_applicable",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C0",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "capture_concordance_status",
    data_type="VARCHAR",
    category="identity",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="'concordant', 'mismatch', 'atif_unavailable', or 'unavailable'",
    null_condition="Never NULL",
    description="Status of capture fidelity comparison between ATIF trajectory and benchmark events.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="not_applicable",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C0",
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
    denominator_policy="required",
    declared_inputs=("total_tool_calls",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Schema conformance rate correlates with execution validity without defining task success",
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
    denominator_policy="required",
    declared_inputs=("binding_matched", "raw_binding_opportunities"),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="Binding survival rate is directly computed from binding_matched, which defines verifier outcome",
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
    denominator_policy="required",
    declared_inputs=("stale_value_bound", "binding_matched", "raw_conflicting_opportunities"),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="Stale value override rate directly measures whether latest value overcame conflicting updates to satisfy verifier contract",
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
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
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
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Occupancy at first failure correlates with memory pressure and degradation",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C0",
    metric_order=2,
    family="action-memory-v1",
)
register_trajectory_feature(
    "write_update_event_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Count of explicit agent memory write or update mutations",
    null_condition="0 when no write or update mutation was emitted",
    description="Explicit memory lifecycle write and update operations performed by the agent.",
    denominator_policy="not_applicable",
    declared_inputs=("total_tool_calls",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Write and update behavior may correlate with success but does not alone define reward",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "conflict_resolution_success",
    data_type="BOOLEAN",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="binding_matched and not stale_value_bound when conflicting opportunities exist",
    null_condition="NULL when raw_conflicting_opportunities == 0",
    description="Whether the agent resolved conflicting memory state in favor of the latest valid value.",
    denominator_policy="not_applicable",
    declared_inputs=("binding_matched", "stale_value_bound", "raw_conflicting_opportunities"),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="The action-memory verifier requires the latest value to survive conflicting state",
    binary_projection=True,
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "retained_obsolete_fact_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="1 when an obsolete or stale value is retained, otherwise 0",
    null_condition="NULL when raw_conflicting_opportunities == 0",
    description="Count of obsolete memory facts retained into the final bound state.",
    denominator_policy="not_applicable",
    declared_inputs=("stale_value_bound", "raw_conflicting_opportunities"),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="Retaining the stale value violates the current action-memory reward contract",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "selective_forgetting_success",
    data_type="BOOLEAN",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="not stale_value_bound when conflicting opportunities exist",
    null_condition="NULL when raw_conflicting_opportunities == 0",
    description="Whether obsolete conflicting state was excluded from the final action.",
    denominator_policy="not_applicable",
    declared_inputs=("stale_value_bound", "raw_conflicting_opportunities"),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="The action-memory verifier rejects a final action that retains the stale value",
    binary_projection=True,
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "cross_session_retrieval_opportunities",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Declared count of retrievals whose source session differs from the active session",
    null_condition="0 when the benchmark declares no cross-session retrieval opportunities",
    description="Opportunity denominator for cross-session memory retrieval.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
    coupling_basis="Cross-session opportunities are assigned by the frozen task contract",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "cross_session_retrieval_successes",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Successful explicit retrievals marked as crossing session boundaries",
    null_condition="0 when no successful cross-session retrieval is observed",
    description="Successful cross-session memory retrieval count.",
    denominator_policy="not_applicable",
    declared_inputs=("cross_session_retrieval_opportunities",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Cross-session retrieval may support success but does not alone define reward",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="action-memory-v1",
)
register_trajectory_feature(
    "cross_session_retrieval_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="cross_session_retrieval_successes / cross_session_retrieval_opportunities",
    null_condition="NULL when cross_session_retrieval_opportunities == 0",
    description="Rate of successful retrieval from prior sessions.",
    denominator_sibling="cross_session_retrieval_opportunities",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("cross_session_retrieval_successes", "cross_session_retrieval_opportunities"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Cross-session retrieval rate may support success but does not alone define reward",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=2,
    eligibility_precondition="cross_session_retrieval_opportunities > 0",
    family="action-memory-v1",
)
register_trajectory_feature(
    "temporal_consistency_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="1.0 when the latest conflicting update controls the final binding, otherwise 0.0",
    null_condition="NULL when raw_conflicting_opportunities == 0",
    description="Temporal consistency of final memory state after ordered updates.",
    denominator_sibling="raw_conflicting_opportunities",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("conflict_resolution_success", "raw_conflicting_opportunities"),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="Latest-update consistency directly determines the action-memory verifier outcome",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=2,
    eligibility_precondition="raw_conflicting_opportunities > 0",
    family="action-memory-v1",
)
register_trajectory_feature(
    "causal_consistency_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="1.0 when the bound value and final invariants agree with verifier truth",
    null_condition="NULL when raw_binding_opportunities == 0",
    description="Consistency between the retrieved causal update chain and the final action state.",
    denominator_sibling="raw_binding_opportunities",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("binding_matched", "raw_binding_opportunities"),
    available_before_verdict=False,
    verdict_coupling="defines",
    coupling_basis="Final invariant verification is part of the benchmark outcome contract",
    producer_module="evallab.interpretation.producers.action_memory",
    construct="Context & Actionable Memory",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=2,
    eligibility_precondition="raw_binding_opportunities > 0",
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
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
    coupling_basis="Declared by the frozen task contract before agent execution",
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
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
    coupling_basis="Declared by the frozen task contract before agent execution",
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
    denominator_policy="not_applicable",
    declared_inputs=("required_dag_edges",),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="Verifier success requires execution of the required dependency edges",
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
    denominator_policy="not_applicable",
    declared_inputs=("required_value_bindings",),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="Verifier success requires correct intermediate and sink value bindings",
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
    denominator_policy="not_applicable",
    declared_inputs=("total_tool_calls",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Redundant calls may consume budget and correlate with failure without defining reward",
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
    denominator_policy="not_applicable",
    declared_inputs=("executed_dag_edges",),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="The verifier contract rejects cyclic dependency execution",
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
    denominator_policy="not_applicable",
    declared_inputs=("executed_dag_edges", "required_dag_edges"),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="Derived from executed required edges that directly determine conformance",
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
    denominator_policy="not_applicable",
    declared_inputs=("executed_dag_edges",),
    available_before_verdict=True,
    verdict_coupling="independent",
    coupling_basis="Process timing is observed before the final verifier verdict and does not define reward",
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
    denominator_policy="required",
    declared_inputs=("correct_value_bindings", "required_value_bindings"),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="The verifier contract directly checks required value propagation",
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
    denominator_policy="required",
    declared_inputs=("executed_dag_edges", "required_dag_edges"),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="The verifier contract directly checks required DAG edge conformance",
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
    denominator_policy="required",
    declared_inputs=("redundant_tool_calls", "total_tool_calls"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Redundant call share may correlate with failure without defining reward",
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
    denominator_policy="required",
    declared_inputs=("first_edge_step", "satisfied_edge_opportunities"),
    available_before_verdict=True,
    verdict_coupling="independent",
    coupling_basis="Process latency is observed before the final verifier verdict and does not define reward",
)

register_trajectory_feature(
    "required_milestones",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Declared DAG edge and value-binding milestones required by the task contract",
    null_condition="Never NULL for mcp-funcdag trials",
    description="Total explicit intermediate milestones required for task completion.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
    coupling_basis="Milestone opportunities are declared by the frozen task contract",
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="mcp-funcdag-v1",
)
register_trajectory_feature(
    "completed_milestones",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Satisfied required edges plus correct required value bindings",
    null_condition="0 when no required milestone is completed",
    description="Intermediate task milestones completed by the agent.",
    denominator_policy="not_applicable",
    declared_inputs=("executed_dag_edges", "correct_value_bindings"),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="Required edge and value milestones directly determine FuncDAG task completion",
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="mcp-funcdag-v1",
)
register_trajectory_feature(
    "milestone_progress_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="completed_milestones / required_milestones",
    null_condition="NULL when required_milestones == 0",
    description="AgentBoard-style normalized progress across explicit task milestones.",
    denominator_sibling="required_milestones",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("completed_milestones", "required_milestones"),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="The metric is derived from edge and binding requirements that define task completion",
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=2,
    eligibility_precondition="required_milestones > 0",
    family="mcp-funcdag-v1",
)
register_trajectory_feature(
    "state_dependency_satisfaction_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="min(executed_dag_edges, required_dag_edges) / required_dag_edges",
    null_condition="NULL when required_dag_edges == 0",
    description="ToolSandbox-style satisfaction of required state dependencies.",
    denominator_sibling="required_dag_edges",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("executed_dag_edges", "required_dag_edges"),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="Required state dependencies are the benchmark's executable DAG edges",
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=2,
    eligibility_precondition="required_dag_edges > 0",
    family="mcp-funcdag-v1",
)
register_trajectory_feature(
    "policy_violation_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Count of explicit policy_violation benchmark events",
    null_condition="0 when the complete event stream contains no policy violation",
    description="Explicit domain or tool policy violations committed during execution.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="A policy violation may cause failure but is not universally part of the task reward",
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="mcp-funcdag-v1",
)
register_trajectory_feature(
    "plan_revision_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Count of explicit plan_revision or replan events",
    null_condition="0 when the complete event stream contains no explicit revision",
    description="Explicit agent plan revisions during tool execution.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Plan revision is a process behavior that may correlate with outcome",
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C0",
    metric_order=1,
    family="mcp-funcdag-v1",
)
register_trajectory_feature(
    "post_error_review_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Count of explicit review or replan events following an observed error",
    null_condition="0 when no post-error review is explicitly emitted",
    description="T-Eval-style review behavior after a failed tool action.",
    denominator_policy="not_applicable",
    declared_inputs=("plan_revision_count",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Review behavior may improve outcome but does not itself define reward",
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C0",
    metric_order=1,
    family="mcp-funcdag-v1",
)
register_trajectory_feature(
    "insufficient_information_opportunities",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Declared count of task states requiring clarification or abstention",
    null_condition="0 when the task contract declares no insufficient-information state",
    description="Opportunity denominator for handling insufficient information.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
    coupling_basis="Insufficient-information opportunities are assigned by the frozen task contract",
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="mcp-funcdag-v1",
)
register_trajectory_feature(
    "insufficient_information_handled",
    data_type="BOOLEAN",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Explicit clarification or insufficient-information responses cover all declared opportunities",
    null_condition="NULL when insufficient_information_opportunities == 0",
    description="Whether the agent handled insufficient information rather than fabricating an action.",
    denominator_policy="not_applicable",
    declared_inputs=("insufficient_information_opportunities",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Clarification behavior may support success but does not universally define reward",
    binary_projection=True,
    producer_module="evallab.interpretation.producers.mcp_funcdag",
    construct="Tool Selection, Composition & Value Propagation",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
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
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
    coupling_basis="Fault injection is assigned by the frozen environment before recovery behavior",
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
    denominator_policy="not_applicable",
    declared_inputs=("injected_fault_record",),
    available_before_verdict=True,
    verdict_coupling="independent",
    coupling_basis="Fault opportunity count is assigned by the environment before recovery behavior",
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
    denominator_policy="not_applicable",
    declared_inputs=("injected_fault_count",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Diagnostic reaction may correlate with recovery but does not itself certify success",
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
    denominator_policy="not_applicable",
    declared_inputs=("injected_fault_count",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Retry count may correlate with recovery difficulty without defining final success",
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
    denominator_policy="not_applicable",
    declared_inputs=("post_fault_retries",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Repeated unchanged failures may correlate with poor recovery without defining reward",
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
    denominator_policy="not_applicable",
    declared_inputs=("injected_fault_count",),
    available_before_verdict=False,
    verdict_coupling="defines",
    coupling_basis="Recovery certification depends on verifier-confirmed final invariants",
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
    denominator_policy="not_applicable",
    declared_inputs=("injected_fault_record",),
    available_before_verdict=True,
    verdict_coupling="independent",
    coupling_basis="Fault timing is assigned and observable before the final verifier verdict",
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
    denominator_policy="not_applicable",
    declared_inputs=("fault_detected_count", "post_fault_retries"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Observed recovery timing may correlate with success but does not certify final invariants",
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
    denominator_policy="required",
    declared_inputs=("certified_recovered_faults", "injected_fault_count"),
    available_before_verdict=False,
    verdict_coupling="defines",
    coupling_basis="The numerator is verifier-certified recovery and directly defines the recovery outcome",
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
    denominator_policy="required",
    declared_inputs=("fault_detected_count", "injected_fault_count"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Detection rate may correlate with recovery but does not certify final invariants",
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
    denominator_policy="required",
    declared_inputs=("blind_retries", "post_fault_retries"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Blind retry share may correlate with poor recovery without defining reward",
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
    denominator_policy="required",
    declared_inputs=("step_to_first_fault", "step_to_recovery", "certified_recovered_faults"),
    available_before_verdict=False,
    verdict_coupling="defines",
    coupling_basis="Latency is emitted only for verifier-certified recovered faults",
)


register_trajectory_feature(
    "diagnosis_class",
    data_type="VARCHAR",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="First explicit diagnosis_class or diagnosed_fault_class event",
    null_condition="NULL when the trajectory emits no explicit diagnosis",
    description="Agent-emitted diagnosis of the encountered fault class.",
    denominator_policy="not_applicable",
    declared_inputs=("injected_fault_record",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="A diagnosis may support recovery but does not certify final invariants",
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C2",
    evidence_grade="Grade A",
    metric_order=1,
    family="mcp-recovery-v1",
)
register_trajectory_feature(
    "source_error_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Count of errors carrying an explicit fault injection event",
    null_condition="0 when no injected source error occurs",
    description="Errors directly caused by the benchmark's injected fault.",
    denominator_policy="not_applicable",
    declared_inputs=("injected_fault_record",),
    available_before_verdict=True,
    verdict_coupling="independent",
    coupling_basis="Source errors are assigned by the controlled benchmark environment",
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C3",
    evidence_grade="Grade A",
    metric_order=1,
    family="mcp-recovery-v1",
)
register_trajectory_feature(
    "propagated_error_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Non-injected error calls observed after the first injected source error",
    null_condition="0 when no downstream error propagation is observed",
    description="Downstream errors propagated from an earlier source fault.",
    denominator_policy="not_applicable",
    declared_inputs=("source_error_count",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Propagation may cause task failure without universally defining the verifier reward",
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C2",
    evidence_grade="Grade A",
    metric_order=1,
    family="mcp-recovery-v1",
)
register_trajectory_feature(
    "strategy_changed_after_failure",
    data_type="BOOLEAN",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="A post-fault retry changes tool or arguments, or explicitly detects the fault",
    null_condition="NULL when no fault is injected",
    description="Whether the agent adapted its execution strategy following failure.",
    denominator_policy="not_applicable",
    declared_inputs=("post_fault_retries", "blind_retries", "fault_detected_count"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Strategy change may enable recovery but does not certify final invariants",
    binary_projection=True,
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C2",
    evidence_grade="Grade A",
    metric_order=1,
    family="mcp-recovery-v1",
)
register_trajectory_feature(
    "controlled_replay_available",
    data_type="BOOLEAN",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="A clean twin, paired trial, or controlled replay identity is declared",
    null_condition="False when no replay identity is declared",
    description="Whether a controlled replay or clean-twin counterfactual is available.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
    coupling_basis="Replay availability is assigned by the frozen campaign design",
    binary_projection=True,
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C2",
    evidence_grade="Grade A",
    metric_order=1,
    family="mcp-recovery-v1",
)
register_trajectory_feature(
    "controlled_replay_outcome_delta",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Declared paired outcome difference between replay and observed trajectory",
    null_condition="NULL when no controlled replay outcome is joined",
    description="Counterfactual outcome contrast from a controlled replay or clean twin.",
    denominator_policy="not_applicable",
    declared_inputs=("controlled_replay_available",),
    available_before_verdict=False,
    verdict_coupling="defines",
    coupling_basis="The contrast is computed directly from paired benchmark outcomes",
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C3",
    evidence_grade="Grade A",
    metric_order=2,
    eligibility_precondition="controlled_replay_available == true",
    family="mcp-recovery-v1",
)
register_trajectory_feature(
    "max_blind_retry_streak",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="Maximum consecutive retries repeating the same failing tool and arguments",
    null_condition="0 when no blind retry occurs",
    description="Longest run of repeated blind retries after a fault.",
    denominator_policy="not_applicable",
    declared_inputs=("blind_retries",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Blind retry streaks may correlate with poor recovery without defining reward",
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="mcp-recovery-v1",
)
register_trajectory_feature(
    "recovery_succeeded_at_persistence",
    data_type="BOOLEAN",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="benchmark_events",
    formula_or_rule="certified_recovered_faults > 0 for the trial's native persistence level",
    null_condition="NULL when no fault is injected",
    description="Certified recovery outcome at the declared fault persistence level.",
    denominator_policy="not_applicable",
    declared_inputs=("certified_recovered_faults", "injected_fault_count"),
    available_before_verdict=False,
    verdict_coupling="defines",
    coupling_basis="The value is derived from verifier-certified recovery",
    binary_projection=True,
    producer_module="evallab.interpretation.producers.mcp_recovery",
    construct="Error Detection & Autonomous Recovery",
    causal_grade="C3",
    evidence_grade="Grade A",
    metric_order=1,
    family="mcp-recovery-v1",
)

# -----------------------------------------------------------------------------
# 4. Autonomous Research (autonomous-research-v1 / Autonomous Research & Method Improvement)
# -----------------------------------------------------------------------------
register_trajectory_feature(
    "score_direction",
    data_type="VARCHAR",
    category="benchmark_ground_truth",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Optimization direction declared by benchmark contract ('higher' or 'lower')",
    null_condition="Never NULL ('higher' by default)",
    description="Direction of visible score optimization: 'higher' for reward/accuracy, 'lower' for loss/error.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C0",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
# 4.1 Experiment Throughput & Validity (RSI-Exam, MLE-bench, RE-Bench)
register_trajectory_feature(
    "iteration_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Total count of logged iterations in research run",
    null_condition="0 by default",
    description="Total count of experiment iterations executed during autonomous research.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Iteration volume reflects exploration search depth and budget consumption",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "measured_iteration_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Count of iterations with recorded visible evaluation score",
    null_condition="0 by default",
    description="Count of experiment iterations with empirical visible score evaluation.",
    denominator_policy="not_applicable",
    declared_inputs=("iteration_count",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Evaluated iterations provide empiric basis for search and selection",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "valid_experiment_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Count of measured iterations where execution succeeded and disposition is not invalid",
    null_condition="0 by default",
    description="Count of validly executed and measured experiments.",
    denominator_policy="not_applicable",
    declared_inputs=("measured_iteration_count",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Valid executions yield empirical signals for research decisions",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "invalid_iteration_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Count of iterations with invalid or failed execution",
    null_condition="0 by default",
    description="Count of invalid experiment iterations due to syntax or runtime errors.",
    denominator_policy="not_applicable",
    declared_inputs=("iteration_count",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Invalid execution rate reflects execution friction or syntax/runtime errors",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "valid_experiment_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="valid_experiment_count / iteration_count",
    null_condition="NULL when iteration_count == 0",
    description="Ratio of valid experiments to total logged iterations.",
    denominator_sibling="iteration_count",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("valid_experiment_count", "iteration_count"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Higher validity rate ensures compute budget translates into measurable results",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    eligibility_precondition="iteration_count > 0",
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "experiment_throughput_per_hour",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="iteration_count / (elapsed_seconds / 3600.0)",
    null_condition="NULL when elapsed_seconds is NULL or elapsed_seconds <= 0",
    description="Rate of executed experiments per hour of elapsed run time.",
    denominator_sibling="elapsed_seconds",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("iteration_count", "elapsed_seconds"),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    eligibility_precondition="elapsed_seconds > 0",
    family="autonomous-research-v1",
)

# 4.2 Hypothesis Turnover & Exploration (RSI-Exam, MLE-bench)
register_trajectory_feature(
    "unique_hypothesis_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Count of distinct normalized hypothesis strings across iterations",
    null_condition="0 by default",
    description="Count of unique hypotheses tested during the autonomous research run.",
    denominator_policy="not_applicable",
    declared_inputs=("iteration_count",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Hypothesis diversity reflects conceptual exploration versus local parameter exploitation",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "repeated_hypothesis_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="iteration_count - unique_hypothesis_count when hypotheses are present else 0",
    null_condition="0 by default",
    description="Count of iterations testing duplicate or repeated hypotheses.",
    denominator_policy="not_applicable",
    declared_inputs=("iteration_count", "unique_hypothesis_count"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Repeated hypothesis testing indicates loop stagnation or deliberate ablation",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "hypothesis_turnover_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="unique_hypothesis_count / iteration_count",
    null_condition="NULL when iteration_count == 0",
    description="Ratio of unique hypotheses to total logged iterations.",
    denominator_sibling="iteration_count",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("unique_hypothesis_count", "iteration_count"),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    eligibility_precondition="iteration_count > 0",
    family="autonomous-research-v1",
)

# 4.3 Regressions & Rollback Control (RSI-Exam, RE-Bench)
register_trajectory_feature(
    "kept_iteration_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Count of iterations with disposition == 'kept'",
    null_condition="0 by default",
    description="Count of experiment iterations where modifications were kept.",
    denominator_policy="not_applicable",
    declared_inputs=("iteration_count",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Kept modifications represent accepted steps in the method evolution",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "reverted_iteration_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Count of iterations with disposition == 'reverted'",
    null_condition="0 by default",
    description="Count of experiment iterations where changes were reverted after negative evaluation.",
    denominator_policy="not_applicable",
    declared_inputs=("iteration_count",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Reverted iterations indicate active rollback upon observing negative experimental results",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "regression_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Sum of (visible_score[i] < visible_score[i-1]) for consecutive measured iterations",
    null_condition="0 by default",
    description="Count of transitions between consecutive experiments where visible score decreased.",
    denominator_policy="not_applicable",
    declared_inputs=("measured_iteration_count",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Regressions reflect noisy evaluations or unpromising exploration branches",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "max_consecutive_regressions",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Maximum length of contiguous score decrease sequence across consecutive measured iterations",
    null_condition="0 by default",
    description="Longest uninterrupted streak of declining visible scores.",
    denominator_policy="not_applicable",
    declared_inputs=("measured_iteration_count",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Long regression streaks indicate failure to roll back or re-anchor to best baseline",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "rollback_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="reverted_iteration_count / measured_iteration_count",
    null_condition="NULL when measured_iteration_count == 0",
    description="Ratio of reverted iterations to measured iterations.",
    denominator_sibling="measured_iteration_count",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("reverted_iteration_count", "measured_iteration_count"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Disciplined research loops revert unhelpful experiments",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    eligibility_precondition="measured_iteration_count > 0",
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "regression_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="regression_count / (measured_iteration_count - 1)",
    null_condition="NULL when measured_iteration_count <= 1",
    description="Ratio of regression steps to total measured experiment transitions.",
    denominator_sibling="measured_iteration_count",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("regression_count", "measured_iteration_count"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="High regression rate without rollback indicates unguided trial-and-error",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    eligibility_precondition="measured_iteration_count > 1",
    family="autonomous-research-v1",
)

# 4.4 Score-Time Curves & Dynamics (RE-Bench, RSI-Exam, AgentBoard)
register_trajectory_feature(
    "baseline_visible_score",
    data_type="DOUBLE",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Initial unoptimized baseline visible score declared by run trace",
    null_condition="NULL if no baseline score provided",
    description="Initial baseline score on the visible evaluation dataset before research loop.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C0",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "best_visible_score",
    data_type="DOUBLE",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="max(visible_score) across measured iterations",
    null_condition="NULL when measured_iteration_count == 0",
    description="Maximum visible evaluation score achieved during research run.",
    denominator_policy="not_applicable",
    declared_inputs=("measured_iteration_count",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Best visible score measures the empirical upper envelope achieved during the run",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "final_visible_score",
    data_type="DOUBLE",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="visible_score of the final executed iteration",
    null_condition="NULL when measured_iteration_count == 0",
    description="Visible evaluation score of the final submitted research checkpoint.",
    denominator_policy="not_applicable",
    declared_inputs=("measured_iteration_count",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Final visible score determines the agent's chosen candidate for validation",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "first_improvement_iteration",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="1-indexed iteration index where visible_score first exceeds baseline_visible_score",
    null_condition="NULL when no iteration improves over baseline",
    description="Iteration number where the agent first improved over baseline performance.",
    denominator_policy="not_applicable",
    declared_inputs=("baseline_visible_score", "measured_iteration_count"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Early discovery of improvement unlocks compounding iterative gains",
    producer_module="evallab.autonomous_research",
    construct="Score-Time Dynamics & Budget Scaling",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "best_improvement_iteration",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="1-indexed iteration index where best_visible_score was first achieved",
    null_condition="NULL when measured_iteration_count == 0",
    description="Iteration number where the peak visible score was discovered.",
    denominator_policy="not_applicable",
    declared_inputs=("best_visible_score", "measured_iteration_count"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Identifies when maximum performance was discovered in the search trajectory",
    producer_module="evallab.autonomous_research",
    construct="Score-Time Dynamics & Budget Scaling",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "time_to_first_improvement_seconds",
    data_type="DOUBLE",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Cumulative elapsed seconds at first_improvement_iteration",
    null_condition="NULL when no improvement over baseline or elapsed_seconds missing",
    description="Wall-clock seconds elapsed until first score improvement over baseline.",
    denominator_policy="not_applicable",
    declared_inputs=("first_improvement_iteration", "elapsed_seconds"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Latency to first productive discovery measures search efficiency",
    producer_module="evallab.autonomous_research",
    construct="Score-Time Dynamics & Budget Scaling",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "time_to_best_score_seconds",
    data_type="DOUBLE",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Cumulative elapsed seconds at best_improvement_iteration",
    null_condition="NULL when measured_iteration_count == 0 or elapsed_seconds missing",
    description="Wall-clock seconds elapsed until peak visible score discovery.",
    denominator_policy="not_applicable",
    declared_inputs=("best_improvement_iteration", "elapsed_seconds"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Time to peak performance reflects search trajectory dynamics",
    producer_module="evallab.autonomous_research",
    construct="Score-Time Dynamics & Budget Scaling",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "stalled_iteration_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Count of iterations executed after peak score discovery without further improvement",
    null_condition="0 by default",
    description="Count of tail iterations executed without improving on best score.",
    denominator_policy="not_applicable",
    declared_inputs=("iteration_count", "best_improvement_iteration"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Tail iterations without gain indicate plateauing or exhausted search ideas",
    producer_module="evallab.autonomous_research",
    construct="Score-Time Dynamics & Budget Scaling",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "plateau_streak_max",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Maximum consecutive measured iterations without setting a new high visible score",
    null_condition="0 by default",
    description="Longest consecutive sequence of experiments without score improvement.",
    denominator_policy="not_applicable",
    declared_inputs=("measured_iteration_count",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Plateau duration reflects resilience and exploration strategy under stagnating returns",
    producer_module="evallab.autonomous_research",
    construct="Score-Time Dynamics & Budget Scaling",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "visible_improvement",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="best_visible_score - baseline_visible_score",
    null_condition="NULL when best_visible_score or baseline_visible_score is NULL",
    description="Total visible score improvement over initial baseline.",
    denominator_policy="not_applicable",
    declared_inputs=("best_visible_score", "baseline_visible_score"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Primary empirical outcome metric for visible method optimization",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "improvement_per_experiment",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="visible_improvement / measured_iteration_count",
    null_condition="NULL when visible_improvement is NULL or measured_iteration_count == 0",
    description="Visible improvement normalized by number of measured experiments.",
    denominator_sibling="measured_iteration_count",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("visible_improvement", "measured_iteration_count"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Marginal gain per experiment reflects search policy precision",
    producer_module="evallab.autonomous_research",
    construct="Autonomous Research & Method Improvement",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    eligibility_precondition="measured_iteration_count > 0",
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "late_improvement_share",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="max(0, best_visible - early_best) / max(1e-9, total_gain)",
    null_condition="NULL when visible_improvement is NULL or visible_improvement <= 0",
    description="Fraction of total score improvement achieved in the second half of iterations.",
    denominator_sibling="visible_improvement",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("visible_improvement", "measured_iteration_count"),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.autonomous_research",
    construct="Score-Time Dynamics & Budget Scaling",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=2,
    eligibility_precondition="visible_improvement > 0",
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "stalled_iteration_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="stalled_iteration_count / iteration_count",
    null_condition="NULL when iteration_count == 0",
    description="Ratio of post-peak stalled iterations to total logged iterations.",
    denominator_sibling="iteration_count",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("stalled_iteration_count", "iteration_count"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="High stall rate indicates diminishing returns or lack of stopping criteria",
    producer_module="evallab.autonomous_research",
    construct="Score-Time Dynamics & Budget Scaling",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    eligibility_precondition="iteration_count > 0",
    family="autonomous-research-v1",
)

# 4.5 Milestone & Rubric Progression (PaperBench, AgentBoard, CORE-Bench)
register_trajectory_feature(
    "required_milestones",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Total count of required task milestones declared by benchmark contract",
    null_condition="0 by default",
    description="Total number of required milestone checkpoints in the research workflow.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.autonomous_research",
    construct="Milestone & Rubric Progression",
    causal_grade="C0",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "completed_milestones",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Count of validated milestones completed during run",
    null_condition="0 by default",
    description="Number of required task milestones completed and verified.",
    denominator_policy="not_applicable",
    declared_inputs=("required_milestones",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Milestone completion tracks staged progress across multi-phase workflows",
    producer_module="evallab.autonomous_research",
    construct="Milestone & Rubric Progression",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "total_rubric_subtasks",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Total count of verifiable rubric subtasks in research protocol",
    null_condition="0 by default",
    description="Total count of fine-grained rubric subtasks defined for paper replication.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.autonomous_research",
    construct="Milestone & Rubric Progression",
    causal_grade="C0",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "completed_rubric_subtasks",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Count of rubric subtasks satisfying deterministic verification criteria",
    null_condition="0 by default",
    description="Count of rubric subtasks passed according to deterministic checks.",
    denominator_policy="not_applicable",
    declared_inputs=("total_rubric_subtasks",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Rubric subtask completion measures granular methodological adherence",
    producer_module="evallab.autonomous_research",
    construct="Milestone & Rubric Progression",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "milestone_completion_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="completed_milestones / required_milestones",
    null_condition="NULL when required_milestones == 0",
    description="Fraction of required workflow milestones successfully completed.",
    denominator_sibling="required_milestones",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("completed_milestones", "required_milestones"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Stage gate progress rate reflects end-to-end task execution completeness",
    producer_module="evallab.autonomous_research",
    construct="Milestone & Rubric Progression",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    eligibility_precondition="required_milestones > 0",
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "rubric_completion_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="completed_rubric_subtasks / total_rubric_subtasks",
    null_condition="NULL when total_rubric_subtasks == 0",
    description="Fraction of scientific rubric subtasks satisfying verification criteria.",
    denominator_sibling="total_rubric_subtasks",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("completed_rubric_subtasks", "total_rubric_subtasks"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Paper replication fidelity is parameterized by fine-grained rubric subtask coverage",
    producer_module="evallab.autonomous_research",
    construct="Milestone & Rubric Progression",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    eligibility_precondition="total_rubric_subtasks > 0",
    family="autonomous-research-v1",
)

# 4.6 Final-Selection Regret (RSI-Exam, MLE-bench)
register_trajectory_feature(
    "optimal_selection_flag",
    data_type="BOOLEAN",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="final_visible_score == best_visible_score",
    null_condition="NULL when measured_iteration_count == 0",
    description="Boolean flag indicating whether the final submitted iteration achieved peak score.",
    denominator_policy="not_applicable",
    declared_inputs=("final_visible_score", "best_visible_score"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Optimal selection indicates the agent correctly submitted its highest-performing checkpoint",
    producer_module="evallab.autonomous_research",
    construct="Selection & Generalization",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "final_selection_regret",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="best_visible_score - final_visible_score",
    null_condition="NULL when best_visible_score or final_visible_score is NULL",
    description="Score loss incurred by submitting a checkpoint inferior to the best discovered.",
    denominator_policy="not_applicable",
    declared_inputs=("best_visible_score", "final_visible_score"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Selection regret quantifies model degradation caused by selecting a suboptimal checkpoint",
    producer_module="evallab.autonomous_research",
    construct="Selection & Generalization",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)

# 4.7 Hidden-Transfer Gap & Generalization (RSI-Exam, MLE-bench)
register_trajectory_feature(
    "score_scale_compatible",
    data_type="BOOLEAN",
    category="benchmark_ground_truth",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Declared benchmark compatibility between visible and hidden evaluation metrics",
    null_condition="False by default",
    description="Flag declaring that visible and hidden test scores use an identical metric scale.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.autonomous_research",
    construct="Selection & Generalization",
    causal_grade="C0",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "hidden_score",
    data_type="DOUBLE",
    category="benchmark_ground_truth",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Evaluation score on held-out private test benchmark slice",
    null_condition="NULL if no hidden evaluation performed",
    description="Primary ground-truth score on the private/held-out test split.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=False,
    verdict_coupling="defines",
    coupling_basis="Hidden score defines benchmark primary outcome on private generalization slice",
    producer_module="evallab.autonomous_research",
    construct="Selection & Generalization",
    causal_grade="C3",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "visible_hidden_transfer_gap",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="hidden_score - final_visible_score if score_scale_compatible else NULL",
    null_condition="NULL when score_scale_compatible is False or hidden_score is NULL or final_visible_score is NULL",
    description="Signed difference between hidden generalization score and visible score. Refused when scales differ.",
    denominator_policy="not_applicable",
    declared_inputs=("hidden_score", "final_visible_score", "score_scale_compatible"),
    available_before_verdict=False,
    verdict_coupling="correlates",
    coupling_basis="Transfer gap isolates overfitting to visible validation split vs true generalization",
    producer_module="evallab.autonomous_research",
    construct="Selection & Generalization",
    causal_grade="C2",
    evidence_grade="Grade A",
    metric_order=1,
    eligibility_precondition="score_scale_compatible == true",
    family="autonomous-research-v1",
)

# 4.8 Artifact Replay & Reproducibility (RSI-Exam, CORE-Bench, PaperBench)
register_trajectory_feature(
    "final_artifact_digest",
    data_type="VARCHAR",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="SHA-256 CAS digest of final submitted research artifact",
    null_condition="NULL if no artifact produced",
    description="Content-addressed digest of the final submitted code or model artifact.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.autonomous_research",
    construct="Reproducibility & Replay",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "artifact_replay_verified",
    data_type="BOOLEAN",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Deterministic verification that executing submitted artifact reproduces claimed visible score",
    null_condition="NULL if replay verification was not performed",
    description="Boolean verification that re-executing artifact reproduces claimed metrics.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=False,
    verdict_coupling="correlates",
    coupling_basis="Replay verification guarantees the submission is an executable and reproducible artifact",
    producer_module="evallab.autonomous_research",
    construct="Reproducibility & Replay",
    causal_grade="C2",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "reproducibility_evaluated_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Count of iterations where reproducibility was explicitly evaluated",
    null_condition="0 by default",
    description="Count of experiment iterations with explicit reproducibility evaluation.",
    denominator_policy="not_applicable",
    declared_inputs=("iteration_count",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Evaluated reproducibility checks provide the empirical basis for auditability",
    producer_module="evallab.autonomous_research",
    construct="Reproducibility & Replay",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "reproducible_iteration_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Count of iterations where artifact execution was verified reproducible",
    null_condition="0 by default",
    description="Count of intermediate iterations with verified reproducible artifacts.",
    denominator_policy="not_applicable",
    declared_inputs=("reproducibility_evaluated_count",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Intermediate replay checks ensure steady artifact auditability throughout the trajectory",
    producer_module="evallab.autonomous_research",
    construct="Reproducibility & Replay",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "reproducibility_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="reproducible_iteration_count / reproducibility_evaluated_count",
    null_condition="NULL when reproducibility_evaluated_count == 0",
    description="Ratio of verified reproducible iterations to evaluated iterations.",
    denominator_sibling="reproducibility_evaluated_count",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("reproducible_iteration_count", "reproducibility_evaluated_count"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="High reproducibility rate indicates consistent build and artifact hygiene",
    producer_module="evallab.autonomous_research",
    construct="Reproducibility & Replay",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    eligibility_precondition="reproducibility_evaluated_count > 0",
    family="autonomous-research-v1",
)

# 4.9 Environment Reconstruction & Dependency Repair (CORE-Bench)
register_trajectory_feature(
    "environment_setup_seconds",
    data_type="DOUBLE",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Time spent in initial environment configuration and dependency resolution",
    null_condition="NULL if setup timing not tracked",
    description="Wall-clock seconds spent in runtime environment setup.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Setup latency captures computational environment friction and package resolution overhead",
    producer_module="evallab.autonomous_research",
    construct="Environment Reconstruction & Dependency Repair",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "dependency_repair_attempts",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Count of environment or dependency errors diagnosed and attempted",
    null_condition="0 by default",
    description="Number of environment or dependency repair operations attempted.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Dependency errors represent external setup obstacles overcome during environment provisioning",
    producer_module="evallab.autonomous_research",
    construct="Environment Reconstruction & Dependency Repair",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "dependency_repair_successes",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Count of dependency or environment repairs that resolved the issue",
    null_condition="0 by default",
    description="Number of dependency or environment repairs successfully resolved.",
    denominator_policy="not_applicable",
    declared_inputs=("dependency_repair_attempts",),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Successful dependency repairs enable downstream execution of research scripts",
    producer_module="evallab.autonomous_research",
    construct="Environment Reconstruction & Dependency Repair",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "runtime_environment_repaired",
    data_type="BOOLEAN",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="dependency_repair_successes >= dependency_repair_attempts if attempts > 0 else True",
    null_condition="True by default",
    description="Flag indicating whether all encountered environment faults were resolved.",
    denominator_policy="not_applicable",
    declared_inputs=("dependency_repair_attempts", "dependency_repair_successes"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Functional environment is a prerequisite for executing research pipelines",
    producer_module="evallab.autonomous_research",
    construct="Environment Reconstruction & Dependency Repair",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "dependency_repair_success_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="dependency_repair_successes / dependency_repair_attempts",
    null_condition="NULL when dependency_repair_attempts == 0",
    description="Fraction of dependency repair attempts that successfully restored execution.",
    denominator_sibling="dependency_repair_attempts",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("dependency_repair_successes", "dependency_repair_attempts"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Autonomous environment self-healing rate in scientific reproducibility tasks",
    producer_module="evallab.autonomous_research",
    construct="Environment Reconstruction & Dependency Repair",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    eligibility_precondition="dependency_repair_attempts > 0",
    family="autonomous-research-v1",
)

# 4.10 Budget & Cost Efficiency (RE-Bench, RSI-Exam, MLE-bench)
register_trajectory_feature(
    "budget_seconds",
    data_type="DOUBLE",
    category="benchmark_ground_truth",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Total allotted wall-clock seconds for research session",
    null_condition="NULL if no budget limit specified",
    description="Time budget limit assigned to the research trial in seconds.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.autonomous_research",
    construct="Score-Time Dynamics & Budget Scaling",
    causal_grade="C0",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "elapsed_seconds",
    data_type="DOUBLE",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Total elapsed wall-clock seconds for research session",
    null_condition="NULL if elapsed time not measured",
    description="Total wall-clock duration of the autonomous research run in seconds.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.autonomous_research",
    construct="Score-Time Dynamics & Budget Scaling",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "total_cost_usd",
    data_type="DOUBLE",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Total LLM API and compute expenditure in USD",
    null_condition="NULL if cost tracking unavailable",
    description="Total monetary expenditure incurred across all model calls and tools.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.autonomous_research",
    construct="Score-Time Dynamics & Budget Scaling",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "total_tokens",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Total tokens consumed across all model calls in the run",
    null_condition="NULL if token usage not recorded",
    description="Total prompt and completion tokens consumed during the research run.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.autonomous_research",
    construct="Score-Time Dynamics & Budget Scaling",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "total_changed_bytes",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Sum of changed_bytes across all iterations",
    null_condition="0 by default",
    description="Total volume of code and file modifications across iterations in bytes.",
    denominator_policy="not_applicable",
    declared_inputs=("iteration_count",),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.autonomous_research",
    construct="Score-Time Dynamics & Budget Scaling",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "budget_utilization_rate",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="elapsed_seconds / budget_seconds",
    null_condition="NULL when budget_seconds is NULL or budget_seconds <= 0 or elapsed_seconds is NULL",
    description="Fraction of allotted wall-clock budget consumed by research run.",
    denominator_sibling="budget_seconds",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("elapsed_seconds", "budget_seconds"),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.autonomous_research",
    construct="Score-Time Dynamics & Budget Scaling",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    eligibility_precondition="budget_seconds > 0",
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "cost_per_improvement",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="total_cost_usd / visible_improvement",
    null_condition="NULL when total_cost_usd is NULL or visible_improvement is NULL or visible_improvement <= 0",
    description="Monetary cost in USD per unit of visible score improvement.",
    denominator_sibling="visible_improvement",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("total_cost_usd", "visible_improvement"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Economic efficiency of autonomous research breakthroughs",
    producer_module="evallab.autonomous_research",
    construct="Score-Time Dynamics & Budget Scaling",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    eligibility_precondition="visible_improvement > 0",
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "tokens_per_experiment",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="total_tokens / iteration_count",
    null_condition="NULL when total_tokens is NULL or iteration_count == 0",
    description="Average LLM token consumption per executed experiment iteration.",
    denominator_sibling="iteration_count",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("total_tokens", "iteration_count"),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.autonomous_research",
    construct="Score-Time Dynamics & Budget Scaling",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    eligibility_precondition="iteration_count > 0",
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "changed_bytes_per_improvement",
    data_type="DOUBLE",
    category="benchmark_l2_metric",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="total_changed_bytes / visible_improvement",
    null_condition="NULL when visible_improvement is NULL or visible_improvement <= 0",
    description="Code change volume in bytes per unit of visible score improvement.",
    denominator_sibling="visible_improvement",
    null_on_zero_denominator=True,
    denominator_policy="required",
    declared_inputs=("total_changed_bytes", "visible_improvement"),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Code edit parsimony relative to empirical metric gain",
    producer_module="evallab.autonomous_research",
    construct="Score-Time Dynamics & Budget Scaling",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    eligibility_precondition="visible_improvement > 0",
    family="autonomous-research-v1",
)

# 4.11 Data Integrity & Contamination Prevention (MLE-bench, RSI-Exam)
register_trajectory_feature(
    "leakage_detected_flag",
    data_type="BOOLEAN",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Flag indicating whether test set access or data leakage was detected",
    null_condition="False by default",
    description="Boolean flag set when evaluation data contamination is detected.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="defines",
    coupling_basis="Contamination or data leakage invalidates experimental credibility",
    producer_module="evallab.autonomous_research",
    construct="Data Integrity & Contamination Prevention",
    causal_grade="C0",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "leakage_warning_count",
    data_type="BIGINT",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="Count of data boundary warnings or blocked test accesses logged during run",
    null_condition="0 by default",
    description="Count of protected evaluation resource access attempts or warnings.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="correlates",
    coupling_basis="Warning frequency measures boundary adherence against protected evaluation assets",
    producer_module="evallab.autonomous_research",
    construct="Data Integrity & Contamination Prevention",
    causal_grade="C1",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
)
register_trajectory_feature(
    "train_val_split_intact",
    data_type="BOOLEAN",
    category="benchmark_l1_fact",
    is_screening=False,
    source_table="autonomous_research_runs",
    formula_or_rule="True if training and validation split remained uncompromised and leakage-free",
    null_condition="True by default",
    description="Integrity flag confirming separation of training and validation data.",
    denominator_policy="not_applicable",
    declared_inputs=(),
    available_before_verdict=True,
    verdict_coupling="independent",
    producer_module="evallab.autonomous_research",
    construct="Data Integrity & Contamination Prevention",
    causal_grade="C0",
    evidence_grade="Grade A",
    metric_order=1,
    family="autonomous-research-v1",
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


# Dimension-safe benchmark projection fields.  These are explicit registry facts,
# not inferred labels; all must be present before an analysis-ready view admits a row.
for _name, _type, _rule in (
    ("cas_uri", "VARCHAR", "Settled Data CAS URI"),
    ("harness_version", "VARCHAR", "Declared harness version"),
    ("scaffold_version", "VARCHAR", "Declared agent scaffold version"),
    ("repeat_group_id", "VARCHAR", "Declared repeated-measure group identifier"),
    ("dose_axis", "VARCHAR", "Declared treatment dose axis"),
    ("dose_value", "DOUBLE", "Declared treatment dose value"),
    ("dose_unit", "VARCHAR", "Declared treatment dose unit"),
    ("alphabet_id", "VARCHAR", "Declared action alphabet identifier"),
    ("alphabet_version", "VARCHAR", "Declared action alphabet version"),
    ("quality_status", "VARCHAR", "Read-only Data compliance disposition"),
    ("report_digest", "VARCHAR", "Read-only ComplianceIngestReport digest"),
    ("source_digest", "VARCHAR", "Settled source artifact digest"),
    ("producer_version", "VARCHAR", "Agent-Data producer version"),
    ("projection_identity", "VARCHAR", "Idempotent projection identity"),
    ("dimension_digest", "VARCHAR", "Full join-dimension digest"),
    ("projection_status", "VARCHAR", "Projected or refused dimension state"),
    ("projection_refusals", "VARCHAR", "Deterministic projection refusal codes"),
    ("analysis_ready", "BOOLEAN", "QUALITY_PASS and fully verified join dimensions"),
):
    register_trajectory_feature(
        _name,
        data_type=_type,
        category="identity",
        is_screening=False,
        source_table="benchmark_projection",
        formula_or_rule=_rule,
        null_condition="NULL or false refuses analysis-ready projection",
        description=_rule,
        denominator_policy="not_applicable",
        declared_inputs=(),
        available_before_verdict=True,
        verdict_coupling="not_applicable",
        producer_module="evallab.interpretation.benchmark_projection",
    )
