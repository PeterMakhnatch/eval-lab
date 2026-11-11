"""Run report: one comprehensive, cited account of a Harbor trial or job.

``build_run_report(trial_dir)`` reads a trial's ``result.json`` and its ATIF
trajectory (following continuation segments and subagent references) and
returns a JSON-ready dictionary with schema ``evallab.run_report/v1``.
``render_run_report_markdown`` renders exactly that dictionary, so a person and
a model read the same facts. ``build_job_report`` rolls a job's trials up.

Rules the report keeps:

- Every total names its source. A missing input is ``None`` with a reason in
  ``data_quality``, never a silent zero.
- Tool status is tri-state. ``ok``/``error`` need a harness signal (exit code,
  error flag, envelope, code-mode script status, rejection message); strong
  output-text signals mark ``error`` with ``evidence: "output_text"``; Reef
  native tools (``execute``/``run_bash``/``write_file``/``read_file``)
  additionally fail on a leading ``timed out after 60s`` or ``refused: …``
  line (same ``output_text`` channel); anything else is ``unknown``.
- A *revisit* is an action whose exact signature (tool plus normalized
  command/arguments) already occurred in the run. An *exact revisit* also got
  the same normalized result back: the agent was at the same spot and nothing
  had changed. Polls (empty keystrokes, wait tools) are excluded.
- Excerpts pass through ``sanitize_excerpt`` (hidden verifier paths and
  secret-like tokens are redacted).
- Deterministic: no wall clock, network, or model calls.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
import shlex
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median
from typing import TYPE_CHECKING, Any, Literal, Protocol

from evallab.edit_signals import blank_quoted_and_heredocs
from evallab.interpretation import run_report_scale
from evallab.interpretation.claude_sessions import (
    normalize_timestamp,
    sidechain_session_tables,
)
from evallab.interpretation.codex_rollouts import read_codex_rollouts
from evallab.interpretation.features import (
    _ASSIGN_RE,
    _program,
    _raw_segments,
    _segments,
    _strip_heredocs,
)
from evallab.interpretation.price_table import estimate_cost_usd, lookup_price
from evallab.interpretation.run_report_scale import (
    MIN_STEPS_FOR_WINDOWS,
    WINDOW_COUNT,
    infer_compactions,
    repeat_onset,
    step_windows,
    time_windows,
)
from evallab.step_layers import (
    STEP_LAYERS_KEY,
    StitchStats,
    classify_stop_reason,
    copied_layers,
    coverage_record,
    discover_trajectory_parts,
    effective_tool_calls,
    execution_problems,
    limit_hit_summary,
    reconstruct_layers,
    stop_category,
    summarize_layers,
    verifier_outcome,
)
from evallab.traj import (
    CONTROL_AGENTS,
    LoopStep,
    TrajectoryError,
    TrajectoryOutline,
    _analyze_loop_suspicion,
    _compute_cbv_slope,
    _compute_exit_code_cascade,
    _resolve_chain_segments,
    extract_features,
    extract_loop_step,
    resolve_trial_target,
    stitched_chain_action_steps,
)
from evallab.trajectory_error_taxonomy import (
    STRONG_ERROR_TEXT_RE,
    classify_step_error,
    split_envelope,
)
from evallab.trajectory_ir import _extract_reasoning_tokens
from evallab.trial_diagnosis import sanitize_excerpt

if TYPE_CHECKING:
    from evallab.interpretation.trajectory_ir import IREpisode, IREvent, TrajectoryIR



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
    recovery_rate_screening: float | None
    context_burn_velocity_screening: float | None
    max_exit_code_cascade_screening: int
    cache_hit_rate_screening: float | None
    subagent_overhead_ratio_screening: float | None
    autonomous_step_ratio_screening: float | None
    assisted_step_ratio_screening: float | None
    is_expected_negative: bool
    expected_probe_count: int
    step_to_first_error: int | None
    time_to_first_error_seconds: float | None
    recovery_latency_steps: int | None
    recovery_latency_seconds: float | None
    unrecovered_at_terminal: bool
    intervention_category: str
    autonomous_step_count: int
    assisted_step_count: int
    intervention_count: int
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    total_tokens: int | None
    cost_usd: float | None
    loop_suspicion_score: float
    loop_suspicion_detected: bool
    loop_reasons_json: str
    repeated_command_count: int
    state_diff_observed: bool
    state_journal_status: str
    state_journal_reason: str | None
    state_events_count: int
    state_mutations_count: int
    state_files_created_count: int
    state_files_modified_count: int
    state_files_deleted_count: int
    state_diff_path_count: int
    state_diff_bytes_delta: int
    unobserved_state_mutations_count: int
    path_reference_count: int
    valid_path_reference_count: int
    invalid_path_reference_count: int
    citation_reference_count: int
    valid_citation_reference_count: int
    invalid_citation_reference_count: int
    edit_call_count: int
    edit_efficiency_screening: float | None
    path_reference_validity_rate_screening: float | None
    citation_reference_validity_rate_screening: float | None
    created_at: str
    inferred_error_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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

    # 2b. Recovery Rate screening (NULL when 0 errors)
    recovery_rate: float | None = None
    if feat.error_count > 0:
        recovery_rate = round(feat.recovery_count / feat.error_count, 4)

    # 2c. Autonomous and Assisted Step Ratio screenings (NULL when 0 steps)
    autonomous_ratio: float | None = None
    assisted_ratio: float | None = None
    if feat.step_count > 0:
        autonomous_ratio = round(feat.autonomous_step_count / feat.step_count, 4)
        assisted_ratio = round(feat.assisted_step_count / feat.step_count, 4)

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
        inferred_error_count=feat.inferred_error_count,
        recovery_count=feat.recovery_count,
        linear_innocence_screening=li_screening,
        tool_error_rate_screening=ter_screening,
        recovery_rate_screening=recovery_rate,
        context_burn_velocity_screening=cbv_screening,
        max_exit_code_cascade_screening=max_cascade,
        cache_hit_rate_screening=cache_hit_rate,
        subagent_overhead_ratio_screening=subagent_overhead,
        autonomous_step_ratio_screening=autonomous_ratio,
        assisted_step_ratio_screening=assisted_ratio,
        is_expected_negative=feat.is_expected_negative,
        expected_probe_count=feat.expected_probe_count,
        step_to_first_error=feat.step_to_first_error,
        time_to_first_error_seconds=feat.time_to_first_error_seconds,
        recovery_latency_steps=feat.recovery_latency_steps,
        recovery_latency_seconds=feat.recovery_latency_seconds,
        unrecovered_at_terminal=feat.unrecovered_at_terminal,
        intervention_category=feat.intervention_category,
        autonomous_step_count=feat.autonomous_step_count,
        assisted_step_count=feat.assisted_step_count,
        intervention_count=feat.intervention_count,
        prompt_tokens=p_tokens,
        completion_tokens=comp_tokens,
        cached_tokens=c_tokens,
        total_tokens=total_tokens,
        cost_usd=feat.cost_usd,
        loop_suspicion_score=feat.loop_suspicion_score,
        loop_suspicion_detected=feat.loop_suspicion_detected,
        loop_reasons_json=feat.loop_reasons_json,
        repeated_command_count=feat.repeated_command_count,
        state_diff_observed=feat.state_diff_observed,
        state_journal_status=feat.state_journal_status,
        state_journal_reason=feat.state_journal_reason,
        state_events_count=feat.state_events_count,
        state_mutations_count=feat.state_mutations_count,
        state_files_created_count=feat.state_files_created_count,
        state_files_modified_count=feat.state_files_modified_count,
        state_files_deleted_count=feat.state_files_deleted_count,
        state_diff_path_count=feat.state_diff_path_count,
        state_diff_bytes_delta=feat.state_diff_bytes_delta,
        unobserved_state_mutations_count=feat.unobserved_state_mutations_count,
        path_reference_count=feat.path_reference_count,
        valid_path_reference_count=feat.valid_path_reference_count,
        invalid_path_reference_count=feat.invalid_path_reference_count,
        citation_reference_count=feat.citation_reference_count,
        valid_citation_reference_count=feat.valid_citation_reference_count,
        invalid_citation_reference_count=feat.invalid_citation_reference_count,
        edit_call_count=feat.edit_call_count,
        edit_efficiency_screening=feat.edit_efficiency_screening,
        path_reference_validity_rate_screening=feat.path_reference_validity_rate_screening,
        citation_reference_validity_rate_screening=feat.citation_reference_validity_rate_screening,
        created_at=feat.created_at,
    )


@dataclass(frozen=True)
class EvidenceCoverageMetrics:
    """Fine-grained coverage metrics across raw sources, events, episodes, errors, state, and verifier."""

    # 1. Raw Source Coverage
    has_atif: bool
    has_result: bool
    has_state_journal: bool
    has_ctrf_verifier: bool
    has_cas_archive: bool
    is_production_cas: bool

    # 2. Event Coverage
    total_events: int
    user_messages_count: int
    agent_messages_count: int
    tool_calls_count: int
    observations_count: int
    state_changes_count: int
    verifier_checks_count: int
    context_management_count: int

    # 3. Episode Coverage
    total_episodes: int
    setup_episodes_count: int
    instruction_episodes_count: int
    inspection_episodes_count: int
    mutation_episodes_count: int
    verification_episodes_count: int
    screening_recovery_episodes_count: int
    terminal_episodes_count: int

    # 4. Error Coverage
    total_errors: int
    unhandled_exceptions_count: int
    tool_errors_count: int
    exit_code_cascades_max: int
    screening_recovered_errors_count: int
    unrecovered_errors_count: int

    # 5. State Coverage
    state_diff_observed: bool
    state_mutations_count: int
    certified_state_pass: bool
    state_before_after_linked: bool

    # 6. Verifier Coverage
    verifier_executed: bool
    verifier_reward_observed: bool
    verifier_tests_count: int
    verifier_passed_count: int
    unsupported_terminal_claims_count: int

    # 7. Linkage & Analysis Readiness
    unpaired_tool_calls_count: int
    linkage_coverage: str  # "complete" | "degraded" | "unlinked"
    analysis_ready: bool
    hold_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "has_atif": self.has_atif,
            "has_result": self.has_result,
            "has_state_journal": self.has_state_journal,
            "has_ctrf_verifier": self.has_ctrf_verifier,
            "has_cas_archive": self.has_cas_archive,
            "is_production_cas": self.is_production_cas,
            "total_events": self.total_events,
            "user_messages_count": self.user_messages_count,
            "agent_messages_count": self.agent_messages_count,
            "tool_calls_count": self.tool_calls_count,
            "observations_count": self.observations_count,
            "state_changes_count": self.state_changes_count,
            "verifier_checks_count": self.verifier_checks_count,
            "context_management_count": self.context_management_count,
            "total_episodes": self.total_episodes,
            "setup_episodes_count": self.setup_episodes_count,
            "instruction_episodes_count": self.instruction_episodes_count,
            "inspection_episodes_count": self.inspection_episodes_count,
            "mutation_episodes_count": self.mutation_episodes_count,
            "verification_episodes_count": self.verification_episodes_count,
            "screening_recovery_episodes_count": self.screening_recovery_episodes_count,
            "terminal_episodes_count": self.terminal_episodes_count,
            "total_errors": self.total_errors,
            "unhandled_exceptions_count": self.unhandled_exceptions_count,
            "tool_errors_count": self.tool_errors_count,
            "exit_code_cascades_max": self.exit_code_cascades_max,
            "screening_recovered_errors_count": self.screening_recovered_errors_count,
            "unrecovered_errors_count": self.unrecovered_errors_count,
            "state_diff_observed": self.state_diff_observed,
            "state_mutations_count": self.state_mutations_count,
            "certified_state_pass": self.certified_state_pass,
            "state_before_after_linked": self.state_before_after_linked,
            "verifier_executed": self.verifier_executed,
            "verifier_reward_observed": self.verifier_reward_observed,
            "verifier_tests_count": self.verifier_tests_count,
            "verifier_passed_count": self.verifier_passed_count,
            "unsupported_terminal_claims_count": self.unsupported_terminal_claims_count,
            "unpaired_tool_calls_count": self.unpaired_tool_calls_count,
            "linkage_coverage": self.linkage_coverage,
            "analysis_ready": self.analysis_ready,
            "hold_reasons": list(self.hold_reasons),
        }


def compute_evidence_coverage_metrics(
    ir: TrajectoryIR | None = None,
    *,
    events: Sequence[IREvent] | None = None,
    episodes: Sequence[IREpisode] | None = None,
    status: str | None = None,
    unavailable_reason: str | None = None,
    quality_status: str | None = None,
    unpaired_count: int | None = None,
    linkage_coverage: str | None = None,
    is_production_cas: bool | None = None,
    cas_uri: str | None = None,
    result_sha: str | None = None,
    primary_reward: float | None = None,
    exception_class: str | None = None,
    final_verdict: str | None = None,
    recovery_count: int | None = None,
    max_cascade: int | None = None,
    trial_dir: Path | None = None,
    state_journal_hold_reason: str | None = None,
) -> EvidenceCoverageMetrics:
    """Canonical, single-producer computation of category-wise evidence coverage metrics."""
    if ir is not None:
        ev_list = ir.events
        ep_list = ir.episodes
        stat = ir.status
        q_stat = ir.quality_status
        unpaired = ir.unpaired_tool_calls_count
        linkage = ir.linkage_coverage
        is_cas = ir.is_production_cas
        c_uri = ir.source_digests.get("cas_uri")
        r_sha = ir.source_digests.get("result_sha256")
        p_reward = ir.primary_reward
        exc_class = ir.exception_class
        verdict = ir.final_verdict
        rec_count = ir.baseline_metrics.recovery_count
        m_cascade = ir.baseline_metrics.max_exit_code_cascade_screening
        t_dir = trial_dir
        sj_hold = state_journal_hold_reason
    else:
        ev_list = events or ()
        ep_list = episodes or ()
        stat = status or "unknown"
        q_stat = quality_status or "unknown"
        unpaired = unpaired_count or 0
        linkage = linkage_coverage or "complete"
        is_cas = bool(is_production_cas)
        c_uri = cas_uri
        r_sha = result_sha
        p_reward = primary_reward
        exc_class = exception_class
        verdict = final_verdict or "UNKNOWN"
        rec_count = recovery_count or 0
        m_cascade = max_cascade or 0
        t_dir = trial_dir
        sj_hold = state_journal_hold_reason

    has_atif = False
    has_result = False
    has_state_journal = False
    has_ctrf = False
    has_cas = False

    if t_dir and t_dir.is_dir():
        has_atif = (t_dir / "agent" / "trajectory.json").is_file() or (t_dir / "trajectory.json").is_file()
        has_result = (t_dir / "result.json").is_file()
        has_state_journal = (t_dir / "state-journal").is_dir() or (t_dir / "state-events.jsonl").is_file()
        has_ctrf = (t_dir / "verifier" / "ctrf.json").is_file() or (t_dir / "verifier" / "ctrf.redacted.json").is_file()
        has_cas = bool(c_uri)
    elif ir is not None:
        has_atif = bool(ir.source_digests.get("source_sha256"))
        has_result = bool(ir.source_digests.get("result_sha256"))
        has_state_journal = any(e.event_type == "state_change" for e in ev_list)
        has_ctrf = any(e.event_type == "verifier_check" for e in ev_list)
        has_cas = bool(ir.source_digests.get("cas_uri"))
    else:
        has_atif = len(ev_list) > 0
        has_result = bool(r_sha)
        has_state_journal = any(e.event_type == "state_change" for e in ev_list)
        has_ctrf = any(e.event_type == "verifier_check" for e in ev_list)
        has_cas = bool(c_uri)

    user_msgs = sum(1 for e in ev_list if e.event_type == "user_message" or e.actor == "user")
    agent_msgs = sum(1 for e in ev_list if e.event_type == "agent_message" or (e.actor == "agent" and not e.status_owning_program))
    tool_calls = sum(1 for e in ev_list if e.event_type == "tool_call" or e.call_index is not None)
    observations = sum(1 for e in ev_list if e.event_type == "observation" or (e.event_type == "tool_call" and e.exit_semantics != "unobserved"))
    state_changes = sum(1 for e in ev_list if e.event_type == "state_change")
    verifier_checks = sum(1 for e in ev_list if e.event_type == "verifier_check" or e.phase == "verifier")
    context_mgmt = sum(1 for e in ev_list if e.event_type == "context_management" or e.action_family == "context_control")

    setup_eps = sum(1 for ep in ep_list if ep.episode_type == "setup")
    inst_eps = sum(1 for ep in ep_list if ep.episode_type == "instruction")
    insp_eps = sum(1 for ep in ep_list if ep.episode_type == "inspection")
    mut_eps = sum(1 for ep in ep_list if ep.episode_type == "mutation")
    ver_eps = sum(1 for ep in ep_list if ep.episode_type == "verification")
    rec_eps = sum(1 for ep in ep_list if ep.episode_type == "screening_recovery")
    term_eps = sum(1 for ep in ep_list if ep.episode_type == "terminal")

    total_errs = sum(1 for e in ev_list if e.is_error)
    tool_errs = sum(1 for e in ev_list if e.is_error and e.event_type == "tool_call")
    unhandled_exc = 1 if exc_class else 0
    rec_errs = rec_count
    unrec_errs = max(0, total_errs - rec_errs)

    state_mutations = sum(1 for e in ev_list if e.action_family in ("file_edit", "file_write"))
    state_diff_obs = bool(has_state_journal)
    certified_pass = p_reward is not None and p_reward >= 1.0
    state_linked = any(e.state_before_digest or e.state_after_digest for e in ev_list) or has_state_journal

    verifier_exec = verifier_checks > 0 or has_ctrf
    verifier_reward_obs = p_reward is not None
    verifier_tests = 1 if verifier_exec else 0
    verifier_passed = 1 if (p_reward and p_reward >= 1.0) else 0
    unsupported_claims = 1 if (verdict == "PASS" and not verifier_exec and not verifier_reward_obs) else 0

    hold_reasons: list[str] = []
    if not has_atif and not is_cas:
        hold_reasons.append("missing_atif_evidence")
    if unpaired > 0:
        hold_reasons.append("degraded_tool_linkage")
    if q_stat in ("fail", "quarantined"):
        hold_reasons.append(f"quarantine_quality_status_{q_stat}")
    if len(ev_list) == 0 and stat != "accounted_unavailable":
        hold_reasons.append("empty_event_sequence")
    if unsupported_claims > 0:
        hold_reasons.append("unsupported_terminal_claim")
    if sj_hold:
        hold_reasons.append(sj_hold)

    analysis_ready = len(hold_reasons) == 0
    return EvidenceCoverageMetrics(
        has_atif=has_atif,
        has_result=has_result,
        has_state_journal=has_state_journal,
        has_ctrf_verifier=has_ctrf,
        has_cas_archive=has_cas,
        is_production_cas=is_cas,
        total_events=len(ev_list),
        user_messages_count=user_msgs,
        agent_messages_count=agent_msgs,
        tool_calls_count=tool_calls,
        observations_count=observations,
        state_changes_count=state_changes,
        verifier_checks_count=verifier_checks,
        context_management_count=context_mgmt,
        total_episodes=len(ep_list),
        setup_episodes_count=setup_eps,
        instruction_episodes_count=inst_eps,
        inspection_episodes_count=insp_eps,
        mutation_episodes_count=mut_eps,
        verification_episodes_count=ver_eps,
        screening_recovery_episodes_count=rec_eps,
        terminal_episodes_count=term_eps,
        total_errors=total_errs,
        unhandled_exceptions_count=unhandled_exc,
        tool_errors_count=tool_errs,
        exit_code_cascades_max=m_cascade,
        screening_recovered_errors_count=rec_errs,
        unrecovered_errors_count=unrec_errs,
        state_diff_observed=state_diff_obs,
        state_mutations_count=state_mutations,
        certified_state_pass=certified_pass,
        state_before_after_linked=state_linked,
        verifier_executed=verifier_exec,
        verifier_reward_observed=verifier_reward_obs,
        verifier_tests_count=verifier_tests,
        verifier_passed_count=verifier_passed,
        unsupported_terminal_claims_count=unsupported_claims,
        unpaired_tool_calls_count=unpaired,
        linkage_coverage=linkage,
        analysis_ready=analysis_ready,
        hold_reasons=tuple(hold_reasons),
    )


CLIP_CHARS = 160

# Hosts whose content is upstream source or release artifacts.
_CODE_HOSTS = (
    "github.com",
    "gitlab.com",
    "pypi.org",
    "files.pythonhosted.org",
    "raw.githubusercontent.com",
)

_FETCH_TOOLS = frozenset({"curl", "wget", "aria2c"})


# Reading a fetched artifact back into the transcript.
_READ_PROGRAMS = frozenset(
    {"cat", "head", "tail", "less", "more", "sed", "awk", "unzip", "tar", "zcat", "gunzip", "bzcat"}
)

# A command that changes files (an edit), used only to order the copied-pass flag.
_EDIT_PROGRAMS = frozenset({"sed", "patch", "tee", "cp", "mv", "install"})
_EDIT_TOOLS = frozenset(
    {
        "edit",
        "write",
        "write_file",
        "str_replace",
        "str_replace_editor",
        "apply_patch",
        "create_file",
        "file_write",
        "notebook_edit",
    }
)

_HOST_RE = re.compile(r"https?://([^/\s\"'`]+)", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s\"'`|;>&)]+", re.IGNORECASE)
_PIN_RE = re.compile(r"^[A-Za-z0-9_.-]+(==|>=|<=|!=|~=|<|>)[^\s]+$")
_PKG_NAME_RE = re.compile(r"^([A-Za-z0-9_.-]+)")
_PATH_RE = re.compile(r"(?:^|[\s'\"=])(/[^\s'\"|;>&]+)")


@dataclass(frozen=True)
class OutsideFetch:
    """One outside-code fetch and, when seen, the step that read it back."""

    step: int
    kind: str
    command: str
    target: str
    read_back_step: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "kind": self.kind,
            "command": self.command,
            "target": self.target,
            "read_back_step": self.read_back_step,
        }


def _host_of(url: str) -> str | None:
    match = _HOST_RE.match(url)
    if match is None:
        return None
    return match.group(1).split("@")[-1].split(":")[0].lower().removeprefix("www.")


def _is_code_host(host: str | None) -> bool:
    if not host:
        return False
    return any(host == known or host.endswith("." + known) for known in _CODE_HOSTS)




def _option_value(args: Sequence[str], names: set[str]) -> str | None:
    for index, arg in enumerate(args):
        for name in names:
            if arg == name and index + 1 < len(args):
                return args[index + 1]
            if arg.startswith(name + "="):
                return arg.split("=", 1)[1]
    return None


def _pip_requirement(token: str) -> tuple[str, str] | None:
    """A requirement that pins, upgrades, or comes from a URL or VCS."""
    if token.startswith("-") or token in {".", ".."}:
        return None
    lowered = token.lower()
    if lowered.startswith(("git+", "hg+", "svn+", "bzr+")) or "://" in token:
        return "vcs" if token.startswith(("git+", "hg+", "svn+", "bzr+")) else "url", token
    if _PIN_RE.match(token):
        return "pinned", token
    return None


def _match_pip(args: list[str]) -> tuple[str, str] | None:
    """``pip download``, or ``pip install`` of a pinned/upgraded/remote requirement.

    Plain ``pip install -e .`` and ``pip install pytest`` (no version) are not
    fetches: they install what is already local or whatever the index serves.
    """
    if not args or args[0] not in {"install", "download"}:
        return None
    sub, rest = args[0], args[1:]
    positional = [arg for arg in rest if not arg.startswith("-")]
    upgraded = any(arg in {"-U", "--upgrade"} for arg in rest)
    if sub == "download":
        target = positional[0] if positional else _option_value(rest, {"-r", "--requirement"})
        return "pip_download", target or "pip download"
    matches = [found for found in (_pip_requirement(arg) for arg in positional) if found]
    if not matches and not upgraded:
        return None
    if matches and all(kind == "url" or kind == "vcs" for kind, _ in matches):
        kind = "pip_install_url"
    elif matches:
        kind = "pip_install_pinned"
    else:
        kind = "pip_install_upgrade"
    target = ", ".join(token for _, token in matches) or " ".join(positional) or "--upgrade"
    return kind, target


def _match_fetch_tool(program: str, args: list[str]) -> tuple[str, str] | None:
    urls = [arg for arg in args if "://" in arg]
    if not urls:
        urls = [arg for arg in args if not arg.startswith("-")]
    host = _host_of(urls[0]) if urls else None
    if not _is_code_host(host):
        return None
    return program, urls[0]


def _python_fetches(command: str) -> list[tuple[str, str]]:
    """``urllib``/``requests`` calls in this segment whose URL is a code host.

    A URL merely mentioned, or a URL that belongs to a later segment of a
    joined step, is not a Python fetch. Heredoc bodies are the model's own
    text and are not scanned.
    """
    command = _strip_heredocs(command)
    if "urllib" not in command and "requests" not in command:
        return []
    kind = "python_requests" if "requests" in command else "python_urllib"
    found: list[tuple[str, str]] = []
    for url in dict.fromkeys(_URL_RE.findall(command)):
        if not _is_code_host(_host_of(url)):
            continue
        found.append((kind, url.rstrip(".,")))
    return found


def _match_segment(raw: str) -> tuple[str, str] | None:
    """Classify one tool call or one ``&&``/``;`` segment, never its neighbors."""
    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError:
        tokens = raw.split()
    found = _program(tokens)
    if found is None:
        return None
    program, args = found
    if program in {"pip", "pip3"}:
        return _match_pip(args)
    if program == "git" and args and args[0] == "clone":
        remotes = [arg for arg in args[1:] if not arg.startswith("-")]
        return "git_clone", remotes[0] if remotes else "git clone"
    if program in _FETCH_TOOLS:
        return _match_fetch_tool(program, args)
    if program in {"python", "python3", "py"}:
        fetches = _python_fetches(raw)
        return fetches[0] if fetches else None
    return None


def match_outside_fetch(command: str) -> tuple[str, str] | None:
    """Classify each tool call and each ``&&``/``;`` segment on its own.

    The kind comes from the segment that holds the fetch. A joined step
    (``python3 -c "import soupsieve" ; timeout 10 curl URL``) is a ``curl``,
    not a Python fetch: the URL is not in the Python call.
    """
    if not command.strip():
        return None
    for raw in _raw_segments(command):
        matched = _match_segment(raw)
        if matched is not None:
            return matched
    return None


def _fetch_locations(command: str, kind: str, target: str) -> list[str]:
    """Paths and names a later command could read the fetch back from."""
    locations: list[str] = []
    for tokens in _segments(command):
        found = _program(tokens)
        if found is None:
            continue
        program, args = found
        if program in {"pip", "pip3"}:
            dest = _option_value(args, {"-d", "--dest"})
            if dest:
                locations.append(dest.rstrip("/"))
        elif program == "git" and args and args[0] == "clone":
            positional = [arg for arg in args[1:] if not arg.startswith("-")]
            if len(positional) >= 2:
                locations.append(positional[-1].rstrip("/"))
            elif positional:
                name = positional[0].rstrip("/").rsplit("/", 1)[-1]
                locations.append(name.removesuffix(".git"))
        elif program in _FETCH_TOOLS:
            output = _option_value(args, {"-o", "--output", "-O", "--output-document"})
            if output and output != "-":
                locations.append(output)
    if kind == "pip_download":
        name = _PKG_NAME_RE.match(target.split(",", 1)[0].strip())
        if name:
            locations.append(name.group(1))
    return [loc for loc in dict.fromkeys(locations) if len(loc) >= 2]


def _reads_back(command: str, locations: Sequence[str]) -> bool:
    """Whether a later command reads from one of the fetch's locations."""
    if not locations or not command.strip():
        return False
    for tokens in _segments(command):
        found = _program(tokens)
        if found is None:
            continue
        program, args = found
        if program not in _READ_PROGRAMS and program not in {"python", "python3", "py"}:
            continue
        joined = " ".join(args)
        for location in locations:
            if location not in command and location not in joined:
                continue
            if program in {"python", "python3", "py"} and "open(" not in command:
                continue
            return True
    return False


def _is_edit(command: str, tool: str) -> bool:
    """A command that writes files, so the copied-pass flag can be ordered."""
    if tool.lower() in _EDIT_TOOLS:
        return True
    if re.search(r"""open\(\s*['\"][^'\"]+['\"]\s*,\s*['\"][wa]""", command):
        return True
    unquoted = blank_quoted_and_heredocs(command)
    for tokens in _segments(unquoted):
        found = _program(tokens)
        if found is None:
            continue
        program, args = found
        if program in _EDIT_PROGRAMS and (
            program != "sed" or any(a in {"-i", "--in-place"} for a in args)
        ):
            return True
        if program in {"python", "python3", "py"} and ".write(" in command:
            return True
        if any(arg.startswith(">") for arg in args):
            return True
    return False


_WRITE_REDIRECT_RE = re.compile(r"(?<![-=])>>?\s*([^\s|;&>'\"]+)")
_OPEN_WRITE_RE = re.compile(r"open\(\s*([A-Za-z_]\w*|['\"][^'\"]+['\"])\s*,\s*['\"][wa]")
_OPEN_DOT_WRITE_RE = re.compile(
    r"open\(\s*([A-Za-z_]\w*|['\"][^'\"]+['\"])[^)]*\)\s*\.\s*write\s*\("
)
_NAME_WRITE_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\.\s*(?:write_text|write_bytes|write)\s*\(")
_PATH_WRITE_RE = re.compile(
    r"Path\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\.\s*(?:write_text|write_bytes|open)\s*\("
)
_SED_FILE_RE = re.compile(r"\bsed\s+(?:-[^\s|;&]*\s+)*-[^\s|;&]*i[^\s|;&]*\s+(.*?)(?:\s*[|;&]|\s*$)")
_TEE_FILE_RE = re.compile(r"\btee\s+(?:-[^\s|;&]+\s+)*([^\s|;&]+)")
_COPY_DEST_RE = re.compile(r"\b(?:cp|mv|install)\b(.*?)(?:\s*[|;&]|\s*$)")


def _is_scratch_path(path: str) -> bool:
    """Writes under /tmp, /dev, /proc or /sys are scratch, never repo edits."""
    return not path or path == "/dev/null" or path.startswith(("/tmp/", "/dev/", "/proc/", "/sys/"))


def repo_edit_paths(tool: str, command: str) -> list[str]:
    """Repo paths an action writes, or [] when it writes only scratch or nothing.

    Heredoc bodies are the model's own script text, so a write there counts
    only when it names a repo path -- directly, or through a ``name = 'path'``
    / ``name = Path('path')`` assignment in the same command (the
    ``path.read_text()`` ... ``path.write_text()`` idiom). Shell ``>`` targets
    skip ``->`` arrows; ``sed -i`` takes path-like trailing tokens, never the
    ``s`` command.
    """
    if tool.lower() in _EDIT_TOOLS:
        paths = re.findall(r"[A-Za-z0-9_./~+-]+(?:/[A-Za-z0-9_./~+-]+)+", command[:200])
        if paths and all(_is_scratch_path(path) for path in paths):
            return []
        return ["<edit-tool>"]
    assigns = dict(_ASSIGN_RE.findall(command))

    def _resolve(name: str) -> str | None:
        if len(name) > 1 and name[0] in "'\"":
            return name.strip("'\"")
        return assigns.get(name)

    paths: list[str] = []
    unquoted = blank_quoted_and_heredocs(command)
    for match in _WRITE_REDIRECT_RE.finditer(unquoted):
        if match.group(1) not in ("&1", "&2", "1", "2"):
            paths.append(match.group(1))
    for match in _OPEN_WRITE_RE.finditer(command):
        resolved = _resolve(match.group(1))
        if resolved:
            paths.append(resolved)
    for match in _OPEN_DOT_WRITE_RE.finditer(command):
        resolved = _resolve(match.group(1))
        if resolved:
            paths.append(resolved)
    for match in _NAME_WRITE_RE.finditer(command):
        resolved = assigns.get(match.group(1))
        if resolved:
            paths.append(resolved)
    for match in _PATH_WRITE_RE.finditer(command):
        paths.append(match.group(1))
    for match in _SED_FILE_RE.finditer(command):
        tokens = [
            token.strip("'\"")
            for token in match.group(1).split()
            if token and not token.startswith("-") and "=" not in token
        ]
        tokens = [token for token in tokens if token and "(" not in token and ")" not in token]
        files = [token for token in tokens if "/" in token or re.search(r"\.\w+$", token)]
        paths += files[-1:] if files else []
    match = _TEE_FILE_RE.search(command)
    if match:
        paths.append(match.group(1))
    for match in _COPY_DEST_RE.finditer(command):
        tokens = [
            token
            for token in match.group(1).split()
            if token and not token.startswith("-") and "=" not in token and not token.startswith("$")
        ]
        if tokens:
            paths.append(tokens[-1])
    return [path for path in dict.fromkeys(paths) if not _is_scratch_path(path)]


def _fetch_excerpt(command: str) -> str:
    return sanitize_excerpt(command, CLIP_CHARS) or ""


def detect_outside_fetches(actions: Sequence[Any]) -> list[OutsideFetch]:
    """Find outside-code fetches in the report's action list.

    ``actions`` are the report's ``_Action`` rows (anything with ``step``,
    ``target``, and ``shell``). A later action that reads a fetch's recorded
    location sets ``read_back_step``; the earliest such read wins.
    """
    fetches: list[OutsideFetch] = []
    pending: list[tuple[int, list[str]]] = []
    for action in actions:
        command = str(getattr(action, "target", "") or "")
        step = int(getattr(action, "step", 0))
        if getattr(action, "shell", False) or match_outside_fetch(command):
            matched = match_outside_fetch(command)
        else:
            matched = None
        if matched is not None:
            kind, target = matched
            fetches.append(OutsideFetch(step, kind, _fetch_excerpt(command), target))
            pending.append((len(fetches) - 1, _fetch_locations(command, kind, target)))
            continue
        for index, locations in pending:
            if fetches[index].read_back_step is None and _reads_back(command, locations):
                fetches[index] = OutsideFetch(
                    fetches[index].step,
                    fetches[index].kind,
                    fetches[index].command,
                    fetches[index].target,
                    step,
                )
    return fetches


def pass_may_be_copied(
    fetches: Sequence[OutsideFetch], reward: float | None, edit_steps: Sequence[int]
) -> dict[str, Any] | None:
    """Flag a passing run whose fetch was read back before the edit that landed.

    "Before the first edit that stayed" is approximated as before the last edit
    seen in the action list: an edit after the read-back is the one that could
    carry the fetched code. The wording stays "may be copied"; a read-back is
    evidence, not proof.
    """
    if reward is None or reward < 1.0:
        return None
    read_back = [
        (fetch, fetch.read_back_step) for fetch in fetches if fetch.read_back_step is not None
    ]
    if not read_back:
        return None
    last_edit = max(edit_steps) if edit_steps else None
    evidence = [fetch for fetch, read_at in read_back if last_edit is None or read_at < last_edit]
    if not evidence:
        return None
    return {
        "flag": "pass_may_be_copied",
        "evidence_steps": [
            {"fetch_step": fetch.step, "read_back_step": fetch.read_back_step} for fetch in evidence
        ],
    }


def outside_fetch_section(
    actions: Sequence[Any], reward: float | None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The report's ``outside_fetches`` section and the copied-pass flag."""
    fetches = detect_outside_fetches(actions)
    edit_steps = [
        int(getattr(action, "step", 0))
        for action in actions
        if _is_edit(
            str(getattr(action, "target", "") or ""), str(getattr(action, "tool", "") or "")
        )
    ]
    section = {
        "count": len(fetches),
        "items": [fetch.to_dict() for fetch in fetches],
    }
    return section, pass_may_be_copied(fetches, reward, edit_steps)


def render_outside_fetch_line(section: dict[str, Any]) -> str:
    """One outcome line: the first fetch and its read-back, or ``none``."""
    items = section.get("items") or []
    if not items:
        return "- Outside code fetched: none"
    first = items[0]
    detail = f"step {first['step']} {first['kind'].replace('_', ' ')} {first['target']}"
    if first.get("read_back_step") is not None:
        detail += f" (read back at step {first['read_back_step']})"
    extra = f" (+{len(items) - 1} more)" if len(items) > 1 else ""
    return f"- Outside code fetched: {detail}{extra}"


_HOSPITAL_NAME = "synthetic_hospital"
_HOSPITAL_VERSION = "1"

REWARD_PATH = Path("verifier/reward.json")

_CHART_TOOLS = frozenset(
    {
        "open_chart",
        "view_encounters",
        "view_encounter_detail",
        "view_section",
        "view_results",
        "search_chart",
        "view_problem_list",
        "view_medications",
    }
)
_SUBMIT_TOOLS = frozenset(
    {
        "submit_diagnosis",
        "submit_summary",
        "submit_pre_read",
        "submit_rankings",
    }
)
_SH_CALL_RE = re.compile(r"sh-agent\s+call\s+([A-Za-z_]\w*)(.*)$", re.DOTALL)
_SH_SUBMIT_RE = re.compile(r"sh-agent\s+submit\b")
_MAX_CHAIN_DOCS = 32


def _task_markers(result: dict[str, Any], trial_dir: Path) -> list[str]:
    """Identity strings that name the task: task dir basename, task_name, trial name."""
    markers: list[str] = [trial_dir.name]
    task_name = result.get("task_name")
    if isinstance(task_name, str) and task_name:
        markers.append(task_name)
    task_id = result.get("task_id")
    if isinstance(task_id, dict) and isinstance(task_id.get("path"), str):
        markers.append(Path(str(task_id["path"])).name)
    elif isinstance(task_id, str):
        markers.append(Path(task_id).name)
    return markers


def _reward_shape(trial_dir: Path) -> bool:
    """True when verifier/reward.json has the hospital verifier's exact contract."""
    try:
        payload = json.loads((trial_dir / REWARD_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("reward"), (int, float))
        and isinstance(payload.get("steps"), int)
        and isinstance(payload.get("submitted"), (int, float))
    )


def _hospital_detect(trial_dir: Path, result: dict[str, Any]) -> bool:
    for marker in _task_markers(result, trial_dir):
        lowered = marker.lower()
        if lowered.startswith("sh-") or "synthetic-hospital" in lowered or "synthetic_hospital" in lowered:
            return True
    return _reward_shape(trial_dir)


def _hospital_unreadable(reason: str) -> dict[str, Any]:
    return {"plugin": _HOSPITAL_NAME, "version": _HOSPITAL_VERSION, "status": "unreadable", "reason": reason}


def _read_reward(trial_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    """The verifier file, or (None, reason) when it is missing or malformed."""
    path = trial_dir / REWARD_PATH
    if not path.is_file():
        return None, "verifier/reward.json missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"verifier/reward.json unreadable ({type(exc).__name__})"
    if not isinstance(payload, dict):
        return None, "verifier/reward.json is not an object"
    reward = payload.get("reward")
    steps = payload.get("steps")
    submitted = payload.get("submitted")
    if (
        isinstance(reward, bool)
        or not isinstance(reward, (int, float))
        or isinstance(steps, bool)
        or not isinstance(steps, int)
        or isinstance(submitted, bool)
        or not isinstance(submitted, (int, float))
    ):
        return None, "verifier/reward.json lacks {reward, steps, submitted}"
    return {"reward": float(reward), "steps": steps, "submitted": float(submitted)}, None


def _norm_query(value: Any) -> str:
    return _WS_RE.sub(" ", str(value).lower()).strip()


def _section_key(tool: str, args: Any) -> tuple[str, ...]:
    """Finest chart address a read call names (documented in the module docstring)."""
    params = args if isinstance(args, dict) else {}
    lowered = {str(key).lower(): value for key, value in params.items()}
    if tool == "view_section" and lowered.get("section_id") is not None:
        return (tool, str(lowered["section_id"]))
    if tool == "view_encounter_detail" and lowered.get("encounter_id") is not None:
        return (tool, str(lowered["encounter_id"]))
    if tool == "search_chart":
        return (tool, str(lowered.get("patient_id")), _norm_query(lowered.get("query", "")))
    if tool == "view_results":
        return (tool, str(lowered.get("patient_id")), str(lowered.get("result_type", "")).lower())
    return (tool, str(lowered.get("patient_id", "")))


def _split_sh_call(command: str) -> tuple[str | None, Any] | None:
    """Parse ``sh-agent call <tool> '<json>'``; None when not an sh-agent call."""
    match = _SH_CALL_RE.search(command)
    if not match:
        return None
    tool, rest = match.group(1), match.group(2).strip()
    if not rest:
        return (tool, {})
    if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in ("'", '"'):
        rest = rest[1:-1]
    try:
        return (tool, json.loads(rest))
    except ValueError:
        return (tool, {"_raw": rest})


def _iter_tool_calls(trial_dir: Path) -> tuple[list[tuple[str, Any]], str | None]:
    """(function_name, arguments) pairs across the trajectory chain.

    Returns ([], reason) when the trajectory is absent or unreadable; the
    caller reports zeros with that reason rather than failing.
    """
    try:
        _, traj_path, _ = resolve_trial_target(trial_dir, repo_root=trial_dir, explicit_runs_root=trial_dir)
    except Exception as exc:
        return [], f"trajectory target unresolvable ({type(exc).__name__})"
    if traj_path is None or not traj_path.is_file():
        return [], "trajectory file missing"
    calls: list[tuple[str, Any]] = []
    seen: set[Path] = set()
    current: Path | None = traj_path
    while current is not None and len(seen) < _MAX_CHAIN_DOCS:
        resolved = current.resolve()
        if resolved in seen:
            break
        seen.add(resolved)
        try:
            doc = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return calls, "a continuation segment is unreadable"
        if not isinstance(doc, dict):
            return calls, "a continuation segment is not an object"
        for step in doc.get("steps") or []:
            if not isinstance(step, dict):
                continue
            for call in step.get("tool_calls") or []:
                if isinstance(call, dict):
                    calls.append((str(call.get("function_name") or "unknown"), call.get("arguments")))
        ref = doc.get("continued_trajectory_ref")
        current = (resolved.parent / ref).resolve() if isinstance(ref, str) and ref else None
    return calls, None


def _chart_reads(calls: list[tuple[str, Any]]) -> tuple[list[tuple[str, tuple[str, ...], str]], list[str]]:
    """Map tool calls to (tool, section key, arg source); collect submit tools."""
    reads: list[tuple[str, tuple[str, ...], str]] = []
    submits: list[str] = []
    for name, args in calls:
        if name in _SUBMIT_TOOLS:
            submits.append(name)
            continue
        if name in _CHART_TOOLS:
            reads.append((name, _section_key(name, args), "native"))
            continue
        command: str | None = None
        if isinstance(args, dict):
            for key in ("command", "cmd", "input"):
                value = args.get(key)
                if isinstance(value, str) and "sh-agent" in value:
                    command = value
                    break
        elif isinstance(args, str) and "sh-agent" in args:
            command = args
        if command is None:
            continue
        if _SH_SUBMIT_RE.search(command):
            submits.append("submit (sh-agent)")
            continue
        parsed = _split_sh_call(command)
        if parsed is None:
            continue
        inner, inner_args = parsed
        if inner in _SUBMIT_TOOLS:
            submits.append(inner)
        elif inner in _CHART_TOOLS:
            reads.append((inner, _section_key(inner, inner_args), "sh-agent"))
    return reads, submits


def _hospital_build(trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    reward, problem = _read_reward(trial_dir)
    if reward is None:
        return _hospital_unreadable(str(problem))
    sources = [REWARD_PATH.as_posix()]
    calls, traj_problem = _iter_tool_calls(trial_dir)
    reads, submits = _chart_reads(calls)
    if traj_problem is not None:
        # The trajectory was not fully available: any count over it would be a
        # fabricated zero (or a silent undercount on a partial chain), so every
        # trajectory-derived measure stays None with the reason.
        chart_section: dict[str, Any] = {
            "chart_reads": None,
            "distinct_sections": None,
            "redundant_queries": None,
            "most_repeated": [],
            "trajectory_note": traj_problem,
        }
        witnessed: list[str] | None = None
    else:
        counts: dict[tuple[str, tuple[str, ...]], list[int]] = {}
        for index, (tool, key, _source) in enumerate(reads):
            counts.setdefault((tool, key), []).append(index)
        repeated: list[tuple[str, list[str], int]] = sorted(
            ((tool, list(key), len(steps)) for (tool, key), steps in counts.items() if len(steps) > 1),
            key=lambda row: (-row[2], row[0]),
        )[:5]
        distinct = len(counts)
        chart_section = {
            "chart_reads": len(reads),
            "distinct_sections": distinct,
            "redundant_queries": len(reads) - distinct,
            "most_repeated": [
                {"tool": tool, "key": key, "reads": reads, "redundant": reads - 1}
                for tool, key, reads in repeated
            ],
        }
        witnessed = sorted(set(submits))
    return {
        "plugin": _HOSPITAL_NAME,
        "version": _HOSPITAL_VERSION,
        "status": "ok",
        "sources": sources,
        "reward": reward["reward"],
        "verifier_steps": reward["steps"],
        "submitted": reward["submitted"],
        "submitted_via_trajectory": witnessed,
        "chart_section_queries": chart_section,
    }


def _hospital_markdown(section: dict[str, Any]) -> list[str]:
    if section.get("status") == "unreadable":
        return [f"- Hospital verifier output unreadable ({section.get('reason')})."]
    lines = [
        f"- Verifier: reward {section['reward']:g}, {section['verifier_steps']} steps, "
        f"submitted {section['submitted']:g}.",
    ]
    witnessed = section.get("submitted_via_trajectory") or []
    note = (section.get("chart_section_queries") or {}).get("trajectory_note")
    if witnessed:
        lines.append("- Submission seen in the trajectory via " + ", ".join(f"`{tool}`" for tool in witnessed) + ".")
    elif note:
        lines.append(f"- No trajectory ({note}): submission witnessed by the verifier alone.")
    else:
        lines.append("- No submit call in the trajectory (the verifier closed the episode).")
    queries = section.get("chart_section_queries") or {}
    if queries.get("trajectory_note"):
        lines.append(f"- Chart reads: unavailable ({queries['trajectory_note']}).")
        return lines
    lines.append(
        f"- Chart reads: {queries.get('chart_reads', 0)} across "
        f"{queries.get('distinct_sections', 0)} distinct sections; "
        f"{queries.get('redundant_queries', 0)} redundant re-reads."
    )
    for row in queries.get("most_repeated") or []:
        lines.append(f"  - {row['reads']}× `{row['tool']}` {row['key']} ({row['redundant']} redundant)")
    return lines


class SyntheticHospitalPlugin:
    """Hospital verifier evidence in the native run report."""

    name = _HOSPITAL_NAME
    version = _HOSPITAL_VERSION

    def detect(self, trial_dir: Path, result: dict[str, Any]) -> bool:
        return _hospital_detect(trial_dir, result)

    def build(self, trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
        return _hospital_build(trial_dir, result)

    def render_markdown(self, section: dict[str, Any]) -> list[str]:
        return _hospital_markdown(section)


_ATLAS_NAME = "atlas_finance"
_ATLAS_VERSION = "1"

INFO_PATH = Path("verifier/grader/info.json")

MIN_WEIGHT = 3.0
_TASK_SLUG_RE = re.compile(r"-env\d+__task_\d+", re.IGNORECASE)




def _info_shape(trial_dir: Path) -> bool:
    """True when verifier/grader/info.json has the gandalf EvaluationInfo shape."""
    try:
        payload = json.loads((trial_dir / INFO_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("criterion_results"), list)
        and isinstance(payload.get("section_results"), list)
    )


def _atlas_detect(trial_dir: Path, result: dict[str, Any]) -> bool:
    for marker in _task_markers(result, trial_dir):
        lowered = marker.lower()
        if "atlas" in lowered or _TASK_SLUG_RE.search(marker):
            return True
    return _info_shape(trial_dir)


def _atlas_unreadable(reason: str) -> dict[str, Any]:
    return {"plugin": _ATLAS_NAME, "version": _ATLAS_VERSION, "status": "unreadable", "reason": reason}


def _read_info(trial_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = trial_dir / INFO_PATH
    if not path.is_file():
        return None, "verifier/grader/info.json missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"verifier/grader/info.json unreadable ({type(exc).__name__})"
    if not isinstance(payload, dict):
        return None, "verifier/grader/info.json is not an object"
    criteria = payload.get("criterion_results")
    sections = payload.get("section_results")
    if not isinstance(criteria, list) or not isinstance(sections, list):
        return None, "verifier/grader/info.json lacks criterion_results/section_results"
    return payload, None


def _weight(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _section_failed(entry: dict[str, Any]) -> bool:
    """Section-gate failure per pass_at_k: section-gate indices/flags only."""
    if entry.get("failed_section_gate_indices"):
        return True
    return entry.get("section_gate_met") is False or entry.get("section_gates_met") is False


def _criterion_problems(
    entry: dict[str, Any], *, label: str
) -> tuple[list[str], list[str], list[str]]:
    """(required_missed, penalties_triggered, unevaluated) reason strings for one criterion."""
    weight = _weight(entry.get("weight"))
    met = entry.get("met")
    text = str(entry.get("criterion", ""))[:90]
    context = f"{label}: {text}" if label else text
    if entry.get("skipped"):
        return [], [], []
    if met is None:
        if weight >= MIN_WEIGHT or weight < 0:
            return [], [], [f"unevaluated criterion (w={weight:g}): {context}"]
        return [], [], []
    if weight >= MIN_WEIGHT and not met:
        return [f"required criterion not met (w={weight:g}): {context}"], [], []
    if weight < 0 and met:
        return [], [f"penalty triggered (w={weight:g}): {context}"], []
    return [], [], []


def _atlas_build(trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    info, problem = _read_info(trial_dir)
    if info is None:
        return _atlas_unreadable(str(problem))
    criteria = [c for c in info["criterion_results"] if isinstance(c, dict)]
    raw_sections = [s for s in info["section_results"] if isinstance(s, dict)]

    by_section: dict[str, list[dict[str, Any]]] = {}
    for entry in criteria:
        section = entry.get("section")
        by_section.setdefault(section if isinstance(section, str) else "unsectioned", []).append(entry)

    sections: list[dict[str, Any]] = []
    order = [s.get("section") for s in raw_sections if isinstance(s.get("section"), str)]
    for name in order + [n for n in by_section if n not in order]:
        gate_entry = next((s for s in raw_sections if s.get("section") == name), {})
        gate_failed = _section_failed(gate_entry)
        missed: list[str] = []
        penalties: list[str] = []
        unevaluated: list[str] = []
        required = sum(1 for c in by_section.get(name, []) if _weight(c.get("weight")) >= MIN_WEIGHT)
        for entry in by_section.get(name, []):
            miss, pen, uneval = _criterion_problems(entry, label=name)
            missed.extend(miss)
            penalties.extend(pen)
            unevaluated.extend(uneval)
        if gate_failed:
            missed.append(f"section gate failed: {name}")
        verdict = "fail" if (missed or penalties or unevaluated) else "pass"
        sections.append(
            {
                "section": name,
                "verdict": verdict,
                "gate_failed": gate_failed,
                "gated_out": gate_entry.get("gated_out") is True,
                "criteria": len(by_section.get(name, [])),
                "required": required,
                "required_missed": missed,
                "penalties_triggered": penalties,
                "unevaluated": unevaluated,
            }
        )

    failed_gates = [s["section"] for s in sections if s["gate_failed"]]
    first_gate = failed_gates[0] if failed_gates else None
    downstream_loss: dict[str, Any] = {"attributed_to": first_gate, "weight": 0.0, "criteria": []}
    if first_gate is not None:
        position = next(i for i, s in enumerate(sections) if s["section"] == first_gate)
        for entry in [c for s in sections[position + 1 :] for c in by_section.get(s["section"], [])]:
            weight = _weight(entry.get("weight"))
            if weight >= MIN_WEIGHT and entry.get("met") is False and not entry.get("skipped"):
                downstream_loss["weight"] += weight
                downstream_loss["criteria"].append(str(entry.get("criterion", ""))[:90])
        downstream_loss["weight"] = round(downstream_loss["weight"], 4)

    all_problems = [p for s in sections for p in s["required_missed"] + s["penalties_triggered"] + s["unevaluated"]]
    return {
        "plugin": _ATLAS_NAME,
        "version": _ATLAS_VERSION,
        "status": "ok",
        "sources": [INFO_PATH.as_posix()],
        "reward": info.get("reward") if isinstance(info.get("reward"), (int, float)) else None,
        "raw_score": info.get("raw_score") if isinstance(info.get("raw_score"), (int, float)) else None,
        "maximum_score": info.get("maximum_score") if isinstance(info.get("maximum_score"), (int, float)) else None,
        "passes": not all_problems,
        "reasons": all_problems,
        "sections": sections,
        "first_failed_gate": first_gate,
        "downstream_loss": downstream_loss,
    }


def _atlas_markdown(section: dict[str, Any]) -> list[str]:
    if section.get("status") == "unreadable":
        return [f"- Finance grader output unreadable ({section.get('reason')})."]
    lines = [
        f"- Grader: {'PASS' if section['passes'] else 'FAIL'}"
        + (
            f" — reward {section['reward']:g}"
            if isinstance(section.get("reward"), (int, float))
            else ""
        )
        + ".",
    ]
    for entry in section.get("sections") or []:
        lines.append(
            f"  - {entry['section']}: **{entry['verdict']}** "
            f"({entry['criteria']} criteria, {entry['required']} required"
            + (", gate failed" if entry["gate_failed"] else "")
            + (", gated out" if entry["gated_out"] else "")
            + ")."
        )
        for problem in entry["required_missed"] + entry["penalties_triggered"] + entry["unevaluated"]:
            lines.append(f"    - {problem}")
    loss = section.get("downstream_loss") or {}
    if loss.get("attributed_to") is not None:
        lines.append(
            f"- Downstream loss attributed to the first failed gate "
            f"`{loss['attributed_to']}`: weight {loss['weight']:g} across "
            f"{len(loss['criteria'])} later required criteria."
        )
    return lines


class AtlasFinancePlugin:
    """Finance verifier evidence in the native run report."""

    name = _ATLAS_NAME
    version = _ATLAS_VERSION

    def detect(self, trial_dir: Path, result: dict[str, Any]) -> bool:
        return _atlas_detect(trial_dir, result)

    def build(self, trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
        return _atlas_build(trial_dir, result)

    def render_markdown(self, section: dict[str, Any]) -> list[str]:
        return _atlas_markdown(section)


SIDECAR_DIR = "ceo_bench"
META_FILE = "meta.json"


def _read_sidecar(trial_dir: Path, name: str) -> dict[str, Any]:
    try:
        data = json.loads((trial_dir / SIDECAR_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{SIDECAR_DIR}/{name} unreadable: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{SIDECAR_DIR}/{name} is not an object")
    return data


def _money(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unknown"
    return f"${value:,.2f}"


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []

class CeoBenchPlugin:
    """Cash trajectory, bankruptcy, forecast error, no-op weeks, split spend."""

    name = "ceo_bench"
    version = "1"

    def detect(self, trial_dir: Path, result: dict[str, Any]) -> bool:
        try:
            meta = _read_sidecar(Path(trial_dir), META_FILE)
        except ValueError:
            return False
        return meta.get("plugin") == self.name

    def build(self, trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
        trial = Path(trial_dir)
        try:
            meta = _read_sidecar(trial, META_FILE)
            cash_doc = _read_sidecar(trial, "cash_daily.json")
            spend_doc = _read_sidecar(trial, "spend.json")
            forecasts_doc = _read_sidecar(trial, "forecasts.json")
            weeks_doc = _read_sidecar(trial, "weeks.json")
        except ValueError as exc:
            return {
                "plugin": self.name,
                "version": self.version,
                "status": "unreadable",
                "reason": str(exc),
            }
        daily = _as_list(cash_doc.get("daily"))
        rows = [r for r in daily if isinstance(r, dict)]
        cashes = [r["cash"] for r in rows if isinstance(r.get("cash"), (int, float))]
        weekly = [r for i, r in enumerate(rows) if i % 7 == 0]
        if rows and (not weekly or weekly[-1] is not rows[-1]):
            weekly = [*weekly, rows[-1]]
        forecast_rows = _as_list(forecasts_doc.get("rows"))
        scored = [
            r for r in forecast_rows
            if isinstance(r, dict) and isinstance(r.get("abs_error"), (int, float))
        ]
        abs_errors = [float(r["abs_error"]) for r in scored]
        worst = sorted(scored, key=lambda r: float(r["abs_error"]), reverse=True)[:5]
        weeks = _as_list(weeks_doc.get("weeks"))
        noop = _as_list(weeks_doc.get("noop_weeks"))
        bankruptcy = _as_dict(meta.get("bankruptcy"))
        sources = [f"{SIDECAR_DIR}/{name}" for name in
                   ("meta.json", "cash_daily.json", "spend.json", "forecasts.json", "weeks.json")]
        return {
            "plugin": self.name,
            "version": self.version,
            "sources": sources,
            "run_id": meta.get("run_id"),
            "model": meta.get("model"),
            "outcome": meta.get("outcome"),
            "bankruptcy": {
                "bankrupt": bool(bankruptcy.get("bankrupt")),
                "day": bankruptcy.get("day"),
            },
            "cash": {
                "source": cash_doc.get("source"),
                "initial": meta.get("initial_cash"),
                "final": meta.get("final_cash"),
                "min": min(cashes) if cashes else None,
                "min_day": next(
                    (r.get("day") for r in rows if r.get("cash") == min(cashes)), None
                ) if cashes else None,
                "days_tracked": len(rows),
                "weekly": [{"day": r.get("day"), "cash": r.get("cash")} for r in weekly],
            },
            "spend": {
                "agent": spend_doc.get("agent"),
                "simulator": spend_doc.get("simulator"),
                "separation_rule": spend_doc.get("separation_rule"),
            },
            "forecasts": {
                "status": forecasts_doc.get("status"),
                "reason": forecasts_doc.get("reason"),
                "n_forecasts": len([r for r in forecast_rows if isinstance(r, dict)]),
                "n_scored": len(scored),
                "mae": round(mean(abs_errors), 2) if abs_errors else None,
                "median_abs_error": round(median(abs_errors), 2) if abs_errors else None,
                "worst": [
                    {
                        "submit_day": r.get("submit_day"),
                        "horizon_days": r.get("horizon_days"),
                        "target_day": r.get("target_day"),
                        "predicted_value": r.get("predicted_value"),
                        "actual_cash": r.get("actual_cash"),
                        "abs_error": r.get("abs_error"),
                    }
                    for r in worst
                ],
            },
            "weeks": {
                "n_weeks": len(weeks),
                "n_noop": len(noop),
                "noop_weeks": noop,
                "rule": weeks_doc.get("rule"),
            },
        }

    def render_markdown(self, section: dict[str, Any]) -> list[str]:
        if section.get("status") == "unreadable":
            return [f"- Domain section unreadable ({section.get('reason')})."]
        lines: list[str] = []
        bankruptcy = section.get("bankruptcy") or {}
        cash = section.get("cash") or {}
        if bankruptcy.get("bankrupt"):
            lines.append(
                f"- Bankrupt on sim day {bankruptcy.get('day')} "
                f"(cash {_money(cash.get('min'))})."
            )
        else:
            lines.append(f"- No bankruptcy (outcome: {section.get('outcome')}).")
        lines.append(
            f"- Cash {_money(cash.get('initial'))} initial -> "
            f"{_money(cash.get('final'))} final "
            f"(min {_money(cash.get('min'))} on day {cash.get('min_day')}; "
            f"{cash.get('days_tracked')} days tracked, source {cash.get('source')})."
        )
        spend = section.get("spend") or {}
        agent = spend.get("agent") or {}
        simulator = spend.get("simulator") or {}
        if agent.get("cost_usd") is not None:
            agent_part = (
                f"Agent spend {_money(agent.get('cost_usd'))} "
                f"(source {agent.get('source')})"
            )
        else:
            agent_part = f"Agent spend unavailable: {agent.get('reason') or 'unknown'}"
        if simulator.get("cost_usd") is not None:
            simulator_part = f"simulator spend {_money(simulator.get('cost_usd'))}"
        else:
            simulator_part = (
                f"simulator spend unavailable: {simulator.get('reason') or 'unknown'}"
            )
        lines.append(
            f"- {agent_part}; {simulator_part} metered separately, "
            "never merged into agent cost."
        )
        forecasts = section.get("forecasts") or {}
        if forecasts.get("status") == "ok":
            lines.append(
                f"- Forecasts: {forecasts.get('n_scored')} of "
                f"{forecasts.get('n_forecasts')} scored, "
                f"MAE {_money(forecasts.get('mae'))}."
            )
        else:
            lines.append(f"- Forecasts unavailable ({forecasts.get('reason')}).")
        weeks = section.get("weeks") or {}
        lines.append(
            f"- No-op weeks: {weeks.get('n_noop')} of {weeks.get('n_weeks')} "
            f"(weeks {weeks.get('noop_weeks')})."
        )
        return lines


class DomainPlugin(Protocol):
    """A benchmark-specific section of the run report."""

    name: str  # stable id, e.g. "synthetic_hospital"
    version: str  # plugin section schema version, e.g. "1"

    def detect(self, trial_dir: Path, result: dict[str, Any]) -> bool:
        """True when this plugin owns the trial (task identity or verifier shape)."""
        ...

    def build(self, trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
        """Build the section dict. Must include ``plugin``, ``version`` and ``sources``.

        ``sources`` lists trial-relative paths actually read. On malformed
        input return ``{"plugin", "version", "status": "unreadable", "reason"}``.
        """
        ...

    def render_markdown(self, section: dict[str, Any]) -> list[str]:
        """Render the section body (the ``## Domain: <name>`` heading is added by the caller)."""
        ...


PLUGINS: tuple[DomainPlugin, ...] = (
    SyntheticHospitalPlugin(),
    AtlasFinancePlugin(),
    CeoBenchPlugin(),
)


def domain_section(trial_dir: Path, result: dict[str, Any]) -> dict[str, Any] | None:
    """First matching plugin's section, or None when no plugin detects the trial."""
    trial = Path(trial_dir)
    for plugin in PLUGINS:
        try:
            detected = plugin.detect(trial, result)
        except Exception:
            continue
        if not detected:
            continue
        try:
            section = plugin.build(trial, result)
        except Exception as exc:
            return {
                "plugin": plugin.name,
                "version": plugin.version,
                "status": "unreadable",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        if not isinstance(section, dict):
            return {
                "plugin": plugin.name,
                "version": plugin.version,
                "status": "unreadable",
                "reason": "plugin returned a non-object section",
            }
        section.setdefault("plugin", plugin.name)
        section.setdefault("version", plugin.version)
        return section
    return None


def render_domain_markdown(section: dict[str, Any] | None) -> list[str]:
    """Markdown lines for the domain section (empty when there is none)."""
    if not section:
        return []
    name = section.get("plugin", "unknown")
    lines = [f"## Domain: {name}", ""]
    for plugin in PLUGINS:
        if plugin.name == name:
            try:
                lines.extend(plugin.render_markdown(section))
            except Exception as exc:
                lines.append(f"- Domain section unreadable: {type(exc).__name__}: {exc}")
            return lines
    status = section.get("status")
    reason = section.get("reason")
    if status == "unreadable":
        lines.append(f"- Domain section unreadable ({reason}).")
    else:
        lines.append(f"- No renderer for domain plugin {name!r}.")
    return lines


RUN_REPORT_SCHEMA = "evallab.run_report/v1"
JOB_REPORT_SCHEMA = "evallab.job_report/v1"
DEFAULT_TIMELINE_LIMIT = 60
TOP_N = 5
TARGET_CHARS = 160
PASS_REWARD = 1.0
MIN_REPEATED_OUTPUT_CHARS = 32
MAX_CONTEXT_EVENT_LINES = 40

# Argument keys that carry the command text, compared case-insensitively.
_COMMAND_KEYS = ("command", "cmd", "keystrokes", "commandline", "script", "code", "input")
_SHELL_KEYS = frozenset({"command", "cmd", "keystrokes", "commandline"})
_DELEGATION_TOOLS = frozenset(
    {"task", "agent", "spawn_agent", "delegate", "subagent", "dispatch_agent", "new_task"}
)
_POLL_TOOLS = frozenset({"wait", "sleep", "poll", "wait_agent"})
_CODE_MODE_CALL_RE = re.compile(r"\btools\.([A-Za-z_]\w*)\s*\(")
_PATCH_FILE_RE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: ([^\n\\\"']+)")
_CODE_MODE_STATUS_RE = re.compile(r"^\s*Script (completed|failed)\b")
_WALL_TIME_RE = re.compile(r"Wall time:?\s*[\d.]+\s*(?:seconds|s)\b", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_EXIT_PREFIX_RE = re.compile(r"^\s*exit (-?\d+)\b")
# Error-text inference uses the taxonomy's shared pattern (D3): the report and
# the trajectory outline infer errors from command output through one regex.
# Reef native harness text-only failures (``reef/harness/runners/native/seed.py``:
# ``run_bash``/``execute`` return ``"timed out after 60s"`` on tool timeout and
# the native tools return ``"refused: <reason>"`` when they decline to run).
# Both arrive with no exit code, error flag, or envelope, and successful
# outputs never lead with these lines (normal results start with ``exit N``,
# ``wrote N …``, or file text), so a leading match names the error while
# deeper mentions stay ``unknown`` data. Scoped to Reef's native tool names so
# other tools' successful outputs that mention errors are unaffected.
_REEF_NATIVE_TOOLS = frozenset({"execute", "run_bash", "write_file", "read_file"})
_REEF_TIMEOUT_LEAD_RE = re.compile(r"^timed out after \d+s")
_REEF_REFUSED_LEAD_RE = re.compile(r"^refused:")
_CACHE_WRITE_KEYS = (
    "cache_write_input_tokens",
    "cache_creation_input_tokens",
    "cache_write_tokens",
)
_HARBOR_PHASES = ("environment_setup", "agent_setup", "agent_execution", "verifier")
_COMPACTION_MIN_PROMPT = 8_000
_COMPACTION_DROP_RATIO = 0.6
_CONTROL_REASON = "control agent (oracle/nop) does not emit a trajectory"


# --------------------------------------------------------------------------- #
# Normalized views
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Output:
    text: str
    exit_code: int | None
    failed: bool | None
    result_type: str | None
    result_status: str | None
    signal: str | None = None


@dataclass(frozen=True)
class _Call:
    tool: str
    wrapper: str | None
    target: str
    key: str
    inner_tools: tuple[str, ...]
    shell: bool = False


@dataclass(frozen=True)
class _Action:
    index: int
    step: int
    timestamp: datetime | None
    tool: str
    wrapper: str | None
    target: str
    signature: str
    is_poll: bool
    output_chars: int | None
    output_hash: str | None
    status: str
    evidence: str | None
    category: str
    excerpt: str | None
    inner_tools: tuple[str, ...]
    shell: bool = False
    call_count: int = 1


@dataclass(frozen=True)
class _Step:
    step: int
    native_step_id: int | None
    segment: int
    source: str
    timestamp: datetime | None
    model: str | None
    message: str
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    reasoning_tokens: int | None
    cache_write_tokens: int | None
    cost_usd: float | None
    is_sidechain: bool
    agent_id: str | None
    context_event: str | None
    subagent_refs: tuple[dict[str, Any], ...]
    actions: tuple[_Action, ...]
    notices: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds(), 3)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _clip(text: str | None, limit: int = TARGET_CHARS) -> str | None:
    if text is None:
        return None
    return sanitize_excerpt(text, limit)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return json.dumps(content, sort_keys=True, default=str)


def _code_mode_parts(text: str) -> list[str] | None:
    """Codex code-mode results are a Python-repr list of ``input_text`` parts."""
    stripped = text.lstrip()
    if not stripped.startswith("[{'type': 'input_text'"):
        return None
    try:
        parts = ast.literal_eval(stripped)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    if not isinstance(parts, list):
        return None
    return [p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)]


def _normalized_output(text: str) -> str:
    return _WS_RE.sub(" ", _WALL_TIME_RE.sub("", text)).strip()


def _target_of(args: Any) -> str:
    """Command text of a call (first non-blank command field) or its canonical arguments.

    A call whose only command fields are blank (terminal keystrokes ``""``) has an
    empty target: it is a wait, not an action.
    """
    if isinstance(args, str):
        return args.strip()
    if isinstance(args, dict):
        lowered = {str(key).lower(): value for key, value in args.items()}
        present = [lowered[key] for key in _COMMAND_KEYS if isinstance(lowered.get(key), str)]
        for value in present:
            if value.strip():
                return value.strip()
        if present:
            return ""
        return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return "" if args is None else json.dumps(args, sort_keys=True, default=str)


_WRAPPER_PROGRAMS = frozenset({"sudo", "doas", "env", "nice", "nohup", "time", "timeout", "stdbuf"})


_PROGRAM_NAME_RE = re.compile(r"^[A-Za-z_][\w.+-]*$")


def _program_of(command: str) -> str | None:
    """First program of a shell command, skipping ``cd x &&``, env assignments, and wrappers.

    A segment that only assigns a command substitution (``tmp=$(mktemp)``) names no
    program of its own; a head that is not a plausible program name is skipped.
    """
    for segment in re.split(r"&&|\|\||;|\n", command):
        tokens = segment.strip().split()
        if tokens and re.match(r"^[A-Za-z_]\w*=(\$\(|`)", tokens[0]):
            continue
        while tokens and (
            ("=" in tokens[0] and not tokens[0].startswith(("-", "/", ".")))
            or tokens[0] in _WRAPPER_PROGRAMS
            or (tokens[0].startswith("-") and len(tokens) > 1)
            or tokens[0].replace(".", "").isdigit()
        ):
            tokens = tokens[1:]
        if not tokens or tokens[0] in {"cd", "set", "export", "source", "."}:
            continue
        head = tokens[0].rsplit("/", 1)[-1]
        if _PROGRAM_NAME_RE.match(head):
            return head[:40]
    return None


def _is_shell_args(args: dict[str, Any]) -> bool:
    """Arguments that carry a shell command line (not code or structured fields)."""
    return any(
        str(key).lower() in _SHELL_KEYS and isinstance(value, str) for key, value in args.items()
    )


# --------------------------------------------------------------------------- #
# Calls, outputs, and status
# --------------------------------------------------------------------------- #


def _unwrap_code_mode(name: str, args: Any) -> _Call | None:
    """Codex code-mode wraps real tools in JavaScript: ``tools.exec_command({...})``."""
    if name != "exec" or not isinstance(args, dict):
        return None
    source = args.get("input")
    if not isinstance(source, str):
        return None
    matches = list(_CODE_MODE_CALL_RE.finditer(source))
    if not matches:
        return None
    decoder = json.JSONDecoder()
    inner: list[tuple[str, str | None]] = []
    shell = False
    for match in matches:
        start = match.end()
        while start < len(source) and source[start].isspace():
            start += 1
        target: str | None = None
        if source.startswith("{", start):
            try:
                obj, _ = decoder.raw_decode(source, start)
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                target = _target_of(obj)
                shell = shell or _is_shell_args(obj)
        inner.append((match.group(1), target))
    if len(inner) == 1 and inner[0][1] is not None:
        display = inner[0][1] or ""
        key = display
    else:
        files = list(dict.fromkeys(f.strip() for f in _PATCH_FILE_RE.findall(source)))
        pieces = [t if t is not None else tool for tool, t in inner]
        display = f"files: {', '.join(files)}" if files else " ; ".join(pieces)
        key = _WS_RE.sub(" ", source).strip()
    return _Call(
        tool=inner[0][0],
        wrapper=name,
        target=display,
        key=key,
        inner_tools=tuple(tool for tool, _ in inner),
        shell=shell and len(inner) == 1,
    )


def _call_from_raw(raw_call: dict[str, Any]) -> _Call:
    name = str(raw_call.get("function_name") or "unknown")
    args = raw_call.get("arguments")
    unwrapped = _unwrap_code_mode(name, args)
    if unwrapped is not None:
        return unwrapped
    target = _target_of(args)
    return _Call(
        tool=name,
        wrapper=None,
        target=target,
        key=target,
        inner_tools=(),
        shell=isinstance(args, dict) and _is_shell_args(args),
    )


_PAYLOAD_OK = frozenset({"ok", "success", "succeeded", "completed"})
_PAYLOAD_ERROR = frozenset({"error", "failed", "failure"})


def _payload_failed(text: str) -> bool | None:
    """Status of a structured tool payload (MCP ``isError``, ``{"status": ...}``).

    A transport-level success that wraps an error value (``{"status": "ok",
    "value": {"error": "not_found"}}``) is a failed call for the agent.
    """
    stripped = text.strip()
    if not stripped.startswith("{") or len(stripped) > 1_000_000:
        return None
    try:
        payload = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("isError", "is_error"):
        if isinstance(payload.get(key), bool):
            return payload[key]
    value = payload.get("value")
    wrapped_error = isinstance(value, dict) and bool(value.get("error"))
    status = payload.get("status")
    if isinstance(status, str) and status.lower() in _PAYLOAD_OK | _PAYLOAD_ERROR:
        return status.lower() in _PAYLOAD_ERROR or wrapped_error
    if isinstance(payload.get("ok"), bool):
        return not payload["ok"] or wrapped_error
    if payload.get("error") or wrapped_error:
        return True
    return None


def _envelope_note(raw_text: str) -> str | None:
    try:
        payload = json.loads(raw_text)
    except ValueError:
        return None
    note = payload.get("exception_info") if isinstance(payload, dict) else None
    return str(note) if note else None


def _decode_result(result: dict[str, Any], call_extra: dict[str, Any]) -> _Output:
    """Decode one observation result, keeping the strongest status signal and its channel."""
    text = _content_text(result.get("content"))
    extra = _dict(result.get("extra"))
    exit_code: int | None = None
    for key in ("exit_code", "returncode", "return_code"):
        exit_code = _int(extra.get(key))
        if exit_code is not None:
            break
    signal = "exit_code" if exit_code is not None else None
    raw_text = text
    envelope_code, envelope_text = split_envelope(text)
    if envelope_code is not None:
        text = envelope_text
        if exit_code is None:
            exit_code, signal = envelope_code, "envelope"
        if envelope_code == -1 and not text.strip() and _envelope_note(raw_text):
            # The harness intercepted the action (for example mini-swe-agent's submit
            # sentinel): nothing ran, so there is no success or failure to report.
            note = _envelope_note(raw_text) or ""
            return _Output(note, None, None, None, None, "not_executed")
    if exit_code is None:
        # REPL-style harnesses lead the output with ``exit N``.
        prefix = _EXIT_PREFIX_RE.match(text)
        if prefix:
            exit_code, signal = int(prefix.group(1)), "exit_prefix"
    failed: bool | None = None if exit_code is None else exit_code != 0
    parts = _code_mode_parts(text)
    if parts is not None:
        status = _CODE_MODE_STATUS_RE.match(parts[0]) if parts else None
        if status:
            parts = parts[1:]
            if failed is None:
                failed, signal = status.group(1) == "failed", "script_status"
        # The script can complete while a tool inside it returned an error payload.
        if failed is not True and any(_payload_failed(part) for part in parts):
            failed, signal = True, "result_payload"
        text = "\n".join(parts)
    for flags in (extra, call_extra):
        for key in ("tool_result_is_error", "is_error"):
            if flags.get(key) is True:
                failed, signal = True, "error_flag"
            elif flags.get(key) is False and failed is None:
                failed, signal = False, "error_flag"
    result_type = str(result.get("type") or "").lower() or None
    result_status = str(result.get("status") or "").lower() or None
    if result_type in {"error", "tool_error"} or result_status in {"error", "failed"}:
        failed, signal = True, "error_flag"
    if failed is None:
        payload = _payload_failed(text)
        if payload is not None:
            failed, signal = payload, "result_payload"
    return _Output(text, exit_code, failed, result_type, result_status, signal)


def _merge_outputs(outputs: Sequence[_Output], call_extras: Sequence[dict[str, Any]]) -> _Output:
    """One observation for several calls: the decisive output names the code and channel.

    Any failure fails the batch; otherwise any success makes it ok; otherwise unknown.
    A per-call error flag fails the batch even when the shared observation is silent.
    """
    decisive = next((o for o in outputs if o.failed is True), None) or next(
        (o for o in outputs if o.failed is False), None
    )
    flagged = any(
        extra.get(key) is True
        for extra in call_extras
        for key in ("tool_result_is_error", "is_error")
    )
    return _Output(
        text="\n".join(o.text for o in outputs),
        exit_code=decisive.exit_code if decisive else None,
        failed=True if flagged else (decisive.failed if decisive else None),
        result_type=decisive.result_type if decisive else None,
        result_status=decisive.result_status if decisive else None,
        signal="error_flag"
        if flagged and not (decisive and decisive.failed)
        else (decisive.signal if decisive else None),
    )


def _status(call: _Call, output: _Output | None) -> tuple[str, str | None, str, str | None]:
    """Return (status, evidence, error_category, excerpt); evidence names the status channel."""
    if output is None:
        return "unknown", "no_result", "none", None
    if output.signal == "not_executed":
        return "unknown", "not_executed", "not_executed", _clip(output.text, 120)
    # Only a real exit code can make a call an expected probe miss (grep exiting 1);
    # an explicit error flag without a code stays an error.
    classification = classify_step_error(
        tool_name=call.tool,
        tool_command=call.target,
        exit_code=output.exit_code,
        output_content=output.text,
        result_type=output.result_type,
        result_status=output.result_status
        or ("failed" if output.failed and output.exit_code is None else None),
    )
    if classification.is_expected_probe:
        return "ok", output.signal, "expected_probe_miss", None
    if output.failed is True or classification.is_error:
        return (
            "error",
            output.signal if output.failed else "harness_rejection",
            classification.category.value,
            _clip(output.text or classification.error_message or "", 240),
        )
    if output.failed is False:
        return "ok", output.signal, "none", None
    strong = STRONG_ERROR_TEXT_RE.search(output.text)
    if strong:
        start = max(0, strong.start() - 80)
        return "error", "output_text", "inferred_from_output", _clip(output.text[start:], 240)
    if call.tool.lower() in _REEF_NATIVE_TOOLS:
        lead = output.text.strip()
        if _REEF_TIMEOUT_LEAD_RE.match(lead):
            return "error", "output_text", "timeout", _clip(lead, 240)
        if _REEF_REFUSED_LEAD_RE.match(lead):
            return "error", "output_text", "inferred_from_output", _clip(lead, 240)
    return "unknown", None, "none", None


def _make_action(
    index: int,
    step: int,
    timestamp: datetime | None,
    call: _Call,
    output: _Output | None,
    call_count: int = 1,
) -> _Action:
    status, evidence, category, excerpt = _status(call, output)
    normalized = _normalized_output(output.text) if output is not None else None
    output_hash = _digest(normalized) if normalized is not None else None
    # Polls are wait tools and blank terminal/command input, never no-argument calls.
    is_poll = call.tool.lower() in _POLL_TOOLS or (call.shell and not call.key.strip())
    return _Action(
        index=index,
        step=step,
        timestamp=timestamp,
        tool=call.tool,
        wrapper=call.wrapper,
        target=call.target,
        signature=_digest(f"{call.tool}\0{_WS_RE.sub(' ', call.key).strip()}"),
        is_poll=is_poll,
        output_chars=len(output.text) if output is not None else None,
        output_hash=output_hash,
        status=status,
        evidence=evidence,
        category=category,
        excerpt=excerpt,
        inner_tools=call.inner_tools,
        shell=call.shell,
        call_count=call_count,
    )


def _observation_results(raw_step: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    observation = raw_step.get("observation")
    if isinstance(observation, dict) and isinstance(observation.get("results"), list):
        results.extend(r for r in observation["results"] if isinstance(r, dict))
    if isinstance(raw_step.get("observation_results"), list):
        results.extend(r for r in raw_step["observation_results"] if isinstance(r, dict))
    return results


def _replay_parser() -> Any:
    """Stock Terminus parser for offline layer reconstruction, or None.

    Harbor-free environments (and unit tests) get ``None``: steps then keep
    their recorded layers only, and anything else reads as missing instead
    of being guessed. To reconstruct pre-HAR-92 trials on such a machine,
    run the report with Harbor on the path instead, e.g.
    ``uv run --no-sync --with harbor==0.21.0 evallab report run --json <trial-dir>``.
    """
    try:
        from evallab.harbor_terminus import mimo_replay_parser
    except ImportError:
        return None
    try:
        return mimo_replay_parser()
    except Exception:
        return None


def _positioned_layers(
    positioned: Sequence[tuple[int, Any]], parse: Any
) -> list[dict[str, Any] | None]:
    """Step layers aligned with ``positioned`` raws (recorded or reconstructed)."""
    layers: list[dict[str, Any] | None] = []
    for _, raw in positioned:
        if not isinstance(raw, dict):
            layers.append(None)
            continue
        if parse is None:
            if raw.get("is_copied_context"):
                # Copied context is replay, not a live turn: it never ran in
                # this segment, so it is never "missing", parser or not.
                layers.append(copied_layers())
                continue
            extra = raw.get("extra")
            stored = extra.get(STEP_LAYERS_KEY) if isinstance(extra, dict) else None
            layers.append(stored if isinstance(stored, dict) else None)
            continue
        try:
            layers.append(reconstruct_layers(raw, parse=parse))
        except Exception:
            layers.append(None)
    return layers


def _lab_metadata(trial: Path) -> dict[str, Any]:
    """Run-level metadata (provider usage) beside the trial directory."""
    for candidate in (trial.parent / "lab-metadata.json", trial / "lab-metadata.json"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


_DAYTONA_USAGE_FILENAME = "daytona-usage.json"
_DAYTONA_DIMENSIONS = ("cpu", "memory_gib", "disk_gib", "gpu", "concurrent_sandboxes")
_DEFAULT_DAYTONA_SAFETY_FRACTION = 0.8


def _daytona_usage(trial: Path) -> dict[str, Any] | None:
    """Per-trial Daytona capacity evidence, or None when the monitor wrote none.

    Old trials (and non-Daytona trials) predate the environment monitor: a
    missing or unreadable file reads as unavailable, never as zero usage.
    """
    try:
        data = json.loads((trial / _DAYTONA_USAGE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _daytona_block(snapshot: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    block = snapshot.get(key)
    return block if isinstance(block, Mapping) else {}


def _daytona_latest_snapshot(usage: Mapping[str, Any]) -> tuple[Mapping[str, Any], str]:
    """Latest capacity snapshot and which evidence field it came from.

    Prefers the disappearance-time current usage, then the last monitor
    sample, then the admission snapshot: the reading closest to the trial's
    end names the pressure the trial actually ran under.
    """
    disappearance = usage.get("disappearance")
    if isinstance(disappearance, Mapping):
        current = disappearance.get("current_usage")
        if isinstance(current, Mapping):
            return current, "disappearance current usage"
    last = usage.get("last_observation")
    if isinstance(last, Mapping):
        return last, "last monitor observation"
    admission = usage.get("admission")
    if isinstance(admission, Mapping):
        return admission, "admission snapshot"
    return {}, "unavailable (no snapshot recorded)"


def _daytona_number(value: Any) -> str:
    if isinstance(value, bool):
        return "unavailable"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return "unavailable"


def _daytona_pressure_note(snapshot: Any) -> str:
    if not isinstance(snapshot, Mapping):
        return "pressure unknown (no snapshot recorded)"
    pressure = snapshot.get("at_or_near_limit")
    if isinstance(pressure, list) and pressure:
        return "at/near limit: " + ", ".join(str(dim) for dim in pressure)
    return "no dimension at/near limit"


def render_daytona_usage_lines(usage: dict[str, Any] | None) -> list[str]:
    """Human-visible Daytona capacity section, or [] when no evidence exists.

    Missing evidence stays missing: dimensions without data render as
    unavailable, never zero. A disappearance beside recent near-limit
    pressure is capacity correlation, never a confirmed provider cause.
    """
    if not isinstance(usage, dict):
        return []
    snapshot, provenance = _daytona_latest_snapshot(usage)
    limits = _daytona_block(snapshot, "limits")
    used = _daytona_block(snapshot, "used")
    pending = _daytona_block(snapshot, "pending")
    requested = _daytona_block(snapshot, "requested")
    safety = snapshot.get("safety_fraction")
    if isinstance(safety, bool) or not isinstance(safety, (int, float)):
        safety = _DEFAULT_DAYTONA_SAFETY_FRACTION
    else:
        safety = float(safety)
    rows: list[list[str]] = []
    for dim in _DAYTONA_DIMENSIONS:
        quota = limits.get(dim)
        cap = (
            quota * safety
            if isinstance(quota, (int, float)) and not isinstance(quota, bool)
            else None
        )
        rows.append(
            [
                dim,
                _daytona_number(quota),
                _daytona_number(cap),
                _daytona_number(used.get(dim)),
                _daytona_number(pending.get(dim)),
                _daytona_number(requested.get(dim)),
            ]
        )
    inventory = snapshot.get("inventory")
    live = str(len(inventory)) if isinstance(inventory, list) else "unavailable"
    lines = [
        "",
        "## Environment capacity (Daytona)",
        f"- Source: {snapshot.get('source') or 'unknown source'} at "
        f"{snapshot.get('observed_at') or 'unknown time'} (snapshot: {provenance}; "
        f"region {snapshot.get('region') or 'unknown'}, "
        f"class {snapshot.get('sandbox_class') or 'unknown'}).",
    ]
    lines += _table(
        ["Dimension", "Raw quota", f"Safety cap ({safety:.0%})", "Used", "Pending", "Requested"],
        rows,
    )
    lines.append(f"- Live sandboxes in provider inventory: {live}.")
    monitor_error = usage.get("monitor_error")
    if isinstance(monitor_error, str) and monitor_error.strip():
        lines.append(
            f"- Monitor gap: {monitor_error}. Usage since then is unavailable, never zero."
        )
    disappearance = usage.get("disappearance")
    if isinstance(disappearance, dict):
        name = (
            disappearance.get("sandbox_name")
            or disappearance.get("sandbox_id")
            or "unknown sandbox"
        )
        when = disappearance.get("observed_at") or "unknown time"
        cause = disappearance.get("cause")
        strength = disappearance.get("evidence_strength")
        if cause == "usage_limit_pressure":
            lines.append(
                f"- Sandbox `{name}` disappeared at {when}: capacity pressure was observed "
                f"nearby (cause `{cause}`; evidence `{strength}`). Correlation with the "
                "disappearance, not a confirmed provider deletion cause."
            )
        else:
            lines.append(
                f"- Sandbox `{name}` disappeared at {when}: cause unknown "
                f"(cause `{cause}`; evidence `{strength}`). No capacity evidence either way."
            )
        basis = disappearance.get("pressure_source")
        lines.append(
            "- Pressure at disappearance: current "
            f"{_daytona_pressure_note(disappearance.get('current_usage'))}; last sample "
            f"{_daytona_pressure_note(disappearance.get('last_observation'))}"
            f" (pressure basis: {basis if basis in ('current_usage', 'last_observation') else 'unrecorded'})."
        )
        if cause != "usage_limit_pressure":
            if disappearance.get("error_type"):
                lines.append(f"- Disappearance probe error: `{disappearance['error_type']}`.")
            if disappearance.get("read_error"):
                lines.append(f"- Usage read error: {disappearance['read_error']}.")
    return lines


def _final_turn_flags(
    positioned: Sequence[tuple[int, Any]],
    layers: Sequence[dict[str, Any] | None],
) -> tuple[bool | None, bool | None]:
    """Last agent turn's accepted ``(task_complete, prose_completion)`` flags."""
    padded = list(layers) + [None] * max(0, len(positioned) - len(layers))
    for (_, raw), layer in reversed(list(zip(positioned, padded, strict=False))):
        if (
            not isinstance(raw, dict)
            or raw.get("source") != "agent"
            or raw.get("is_copied_context")
        ):
            continue
        accepted = layer.get("accepted") if isinstance(layer, Mapping) else None
        if not isinstance(accepted, Mapping):
            return None, None
        return (
            accepted.get("task_complete") if accepted.get("kind") != "parse_error" else False,
            accepted.get("kind") == "prose_completion" or None,
        )
    return None, None


def _step_actions(
    raw_step: dict[str, Any],
    step: int,
    timestamp: datetime | None,
    start_index: int,
    layer: dict[str, Any] | None = None,
) -> tuple[list[_Action], list[str]]:
    """Pair a step's tool calls with its results; return (actions, unpaired harness notices).

    ``raw_content`` Terminus steps carry no native ``tool_calls``; the calls
    the harness accepted (and executed) are synthesized from the step layers
    instead, so executed work counts instead of reading as zero.
    """
    raw_calls = effective_tool_calls(raw_step, layer)
    if not raw_calls:
        return [], []
    calls = [_call_from_raw(c) for c in raw_calls]
    call_extras = [_dict(c.get("extra")) for c in raw_calls]
    results = _observation_results(raw_step)
    by_id = {r["source_call_id"]: r for r in results if isinstance(r.get("source_call_id"), str)}
    call_ids = [c.get("tool_call_id") for c in raw_calls]
    paired: list[dict[str, Any] | None]
    if by_id and all(isinstance(cid, str) for cid in call_ids):
        paired = [by_id.get(str(cid)) for cid in call_ids]
        extras = [r for r in results if r.get("source_call_id") not in set(call_ids)]
    elif len(results) >= len(calls):
        # Positional pairing; trailing results are harness notices (for example a
        # length-limit warning appended after the tool outputs).
        paired = [result for result in results[: len(calls)]]
        extras = results[len(calls) :]
    else:
        # Fewer observations than calls (terminal harnesses send several keystroke
        # batches and read one screen): the step is the action.
        merged = (
            _merge_outputs([_decode_result(r, {}) for r in results], call_extras)
            if results
            else None
        )
        joined = _Call(
            tool=calls[0].tool
            if len({c.tool for c in calls}) == 1
            else "+".join(dict.fromkeys(c.tool for c in calls)),
            wrapper=calls[0].wrapper,
            target=" ; ".join(c.target for c in calls if c.target),
            key="\n".join(c.key for c in calls),
            inner_tools=tuple(t for c in calls for t in c.inner_tools),
            shell=any(c.shell for c in calls),
        )
        return [_make_action(start_index, step, timestamp, joined, merged, len(calls))], []
    actions = [
        _make_action(
            start_index + offset,
            step,
            timestamp,
            call,
            _decode_result(result, call_extras[offset]) if result is not None else None,
        )
        for offset, (call, result) in enumerate(zip(calls, paired, strict=True))
    ]
    return actions, [_content_text(r.get("content")) for r in extras]


def _cache_write(metrics: dict[str, Any]) -> int | None:
    extra = _dict(metrics.get("extra"))
    for key in _CACHE_WRITE_KEYS:
        value = _int(extra.get(key))
        if value is not None:
            return value
    return None


def _build_steps(
    positioned: Sequence[tuple[int, Any]],
    layers: Sequence[dict[str, Any] | None] = (),
) -> tuple[list[_Step], list[LoopStep]]:
    steps: list[_Step] = []
    loop_steps: list[LoopStep] = []
    action_index = 0
    # Number only well-formed steps so ordinals stay contiguous.
    padded = list(layers) + [None] * max(0, len(positioned) - len(layers))
    well_formed = [
        (segment, raw, layer)
        for (segment, raw), layer in zip(positioned, padded, strict=False)
        if isinstance(raw, dict)
    ]
    for ordinal, (segment, raw, layer) in enumerate(well_formed, start=1):
        loop_steps.append(extract_loop_step(raw)[0])
        timestamp = _parse_ts(raw.get("timestamp"))
        metrics = _dict(raw.get("metrics"))
        extra = _dict(raw.get("extra"))
        actions, notices = _step_actions(raw, ordinal, timestamp, action_index, layer)
        action_index += len(actions)
        refs: list[dict[str, Any]] = []
        for result in _observation_results(raw):
            refs_value = result.get("subagent_trajectory_ref")
            for ref in [refs_value] if isinstance(refs_value, dict) else refs_value or []:
                if isinstance(ref, dict):
                    refs.append(ref)
        context = _dict(extra.get("context_management"))
        steps.append(
            _Step(
                step=ordinal,
                native_step_id=_int(raw.get("step_id")),
                segment=segment,
                source=str(raw.get("source") or "agent"),
                timestamp=timestamp,
                model=raw.get("model_name") if isinstance(raw.get("model_name"), str) else None,
                message=_content_text(raw.get("message")),
                prompt_tokens=_int(metrics.get("prompt_tokens")),
                completion_tokens=_int(metrics.get("completion_tokens")),
                cached_tokens=_int(metrics.get("cached_tokens")),
                reasoning_tokens=_extract_reasoning_tokens(raw, metrics) if metrics else None,
                cache_write_tokens=_cache_write(metrics),
                cost_usd=_float(metrics.get("cost_usd")),
                is_sidechain=extra.get("is_sidechain") is True,
                agent_id=extra.get("agent_id") if isinstance(extra.get("agent_id"), str) else None,
                context_event=str(context.get("type")) if context.get("type") else None,
                subagent_refs=tuple(refs),
                actions=tuple(actions),
                notices=tuple(notices),
            )
        )
    return steps, loop_steps


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


def _phase_timing(result: dict[str, Any]) -> tuple[dict[str, Any], datetime | None]:
    started = _parse_ts(result.get("started_at"))
    finished = _parse_ts(result.get("finished_at"))
    phases: dict[str, Any] = {}
    known = 0.0
    for name in _HARBOR_PHASES:
        span = _dict(result.get(name))
        start = _parse_ts(span.get("started_at"))
        end = _parse_ts(span.get("finished_at"))
        seconds = _seconds(start, end)
        if seconds is not None:
            known += seconds
        phases[name] = {
            "seconds": seconds,
            "starts_at_offset_seconds": _seconds(started, start),
        }
    total = _seconds(started, finished)
    return {
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "total_seconds": total,
        "phases": phases,
        "unaccounted_seconds": round(max(total - known, 0.0), 3) if total is not None else None,
        "phases_overlap": total is not None and known > total + 1.0,
    }, started


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "median": None, "p90": None, "max": None, "total": None}
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)]
    return {
        "count": len(ordered),
        "median": round(median(ordered), 3),
        "p90": round(p90, 3),
        "max": round(ordered[-1], 3),
        "total": round(sum(ordered), 3),
    }


def _action_preview(actions: Sequence[_Action]) -> str | None:
    if not actions:
        return None
    first = actions[0]
    more = f" (+{len(actions) - 1} more)" if len(actions) > 1 else ""
    target = _clip(first.target, 100) if first.target else "(wait, no input)"
    return f"{first.tool}: {target}{more}"


def _timing(
    result: dict[str, Any], steps: Sequence[_Step]
) -> tuple[dict[str, Any], datetime | None]:
    section, trial_start = _phase_timing(result)
    agent_start = _parse_ts(_dict(result.get("agent_execution")).get("started_at"))
    stamped = [s for s in steps if s.timestamp is not None]
    origin = agent_start or (stamped[0].timestamp if stamped else None) or trial_start
    # Gaps between consecutive agent steps: the model's turn plus the tool time of the
    # previous action. Time spent before the first agent step is reported separately.
    agent_steps = [s for s in stamped if s.source == "agent"]
    gaps: list[tuple[float, _Step]] = []
    for previous, step in zip(agent_steps, agent_steps[1:], strict=False):
        if previous.timestamp and step.timestamp:
            gaps.append(((step.timestamp - previous.timestamp).total_seconds(), step))
    section["first_agent_step_offset_seconds"] = (
        _seconds(origin, agent_steps[0].timestamp) if agent_steps else None
    )
    section["offset_origin"] = (
        "agent_execution.started_at"
        if agent_start
        else ("first_step_timestamp" if stamped else "trial.started_at")
    )
    section["steps_with_timestamps"] = len(stamped)
    section["agent_span_seconds"] = (
        _seconds(stamped[0].timestamp, stamped[-1].timestamp) if len(stamped) > 1 else None
    )
    section["step_gap_seconds"] = _distribution([g for g, _ in gaps if g >= 0])
    section["slowest_steps"] = [
        {
            "step": step.step,
            "seconds": round(gap, 3),
            "action": _action_preview(step.actions),
        }
        for gap, step in sorted(gaps, key=lambda item: (-item[0], item[1].step))[:TOP_N]
    ]
    return section, origin


def _sum(values: Iterable[int | float | None]) -> int | float | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _trial_model(result: dict[str, Any]) -> str | None:
    """Resolve the trial model id for price-table matching.

    Same precedence as ``_identity`` for the sources visible here
    (``result.json`` only): ``agent_info.model_info.name`` first, then
    ``config.agent.model_name``. Returns the raw id; normalization to a
    table key happens in ``price_table.lookup_price``.
    """
    agent_info = _dict(result.get("agent_info"))
    model_info = _dict(agent_info.get("model_info"))
    agent_cfg = _dict(_dict(result.get("config")).get("agent"))
    model = model_info.get("name") or agent_cfg.get("model_name")
    return str(model) if isinstance(model, str) and model.strip() else None


def _tokens_and_cost(
    result: dict[str, Any],
    terminal: dict[str, Any],
    steps: Sequence[_Step],
    lab_metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    agent_result = _dict(result.get("agent_result"))
    final = _dict(terminal.get("final_metrics"))
    final_extra = _dict(final.get("extra"))
    llm_steps = [s for s in steps if s.source == "agent"]
    metered = [
        s for s in llm_steps if s.prompt_tokens is not None or s.completion_tokens is not None
    ]
    step_sums = {
        "input": _sum(s.prompt_tokens for s in steps),
        "cached_input": _sum(s.cached_tokens for s in steps),
        "output": _sum(s.completion_tokens for s in steps),
        "reasoning": _sum(s.reasoning_tokens for s in steps),
        "cache_write": _sum(s.cache_write_tokens for s in steps),
        "cost_usd": _sum(s.cost_usd for s in steps),
    }
    declared = {
        "result_json": {
            "input": _int(agent_result.get("n_input_tokens")),
            "cached_input": _int(agent_result.get("n_cache_tokens")),
            "output": _int(agent_result.get("n_output_tokens")),
            "cost_usd": _float(agent_result.get("cost_usd")),
        },
        "trajectory_final_metrics": {
            "input": _int(final.get("total_prompt_tokens")),
            "cached_input": _int(final.get("total_cached_tokens")),
            "output": _int(final.get("total_completion_tokens")),
            "cost_usd": _float(final.get("total_cost_usd")),
        },
        "step_sum": step_sums,
    }
    warnings: list[str] = []

    def pick(field: str) -> tuple[Any, str | None]:
        for source in ("result_json", "trajectory_final_metrics", "step_sum"):
            value = declared[source].get(field)
            if value is not None:
                return value, source
        return None, None

    chosen: dict[str, Any] = {}
    sources: dict[str, str | None] = {}
    for field in ("input", "cached_input", "output"):
        chosen[field], sources[field] = pick(field)
    reasoning_total = _first(
        _int(final_extra.get("total_reasoning_tokens")),
        _int(final_extra.get("reasoning_output_tokens")),
    )
    chosen["reasoning"] = reasoning_total if reasoning_total is not None else step_sums["reasoning"]
    sources["reasoning"] = (
        "trajectory_final_metrics"
        if reasoning_total is not None
        else ("step_sum" if step_sums["reasoning"] is not None else None)
    )
    chosen["cache_write"] = step_sums["cache_write"]
    uncached = (
        chosen["input"] - chosen["cached_input"]
        if chosen["input"] is not None and chosen["cached_input"] is not None
        else None
    )
    total = (
        chosen["input"] + chosen["output"]
        if chosen["input"] is not None and chosen["output"] is not None
        else None
    )
    for field in ("input", "output"):
        a = declared["result_json"][field]
        b = declared["trajectory_final_metrics"][field]
        if a is not None and b is not None and max(a, b) > 0 and abs(a - b) / max(a, b) > 0.01:
            warnings.append(
                f"{field} tokens disagree: result.json {a:,} vs trajectory final_metrics {b:,}"
            )
    prompts = [(s.step, s.prompt_tokens) for s in steps if s.prompt_tokens is not None]
    peak = max(prompts, key=lambda item: item[1]) if prompts else None
    by_step = sorted(
        (s for s in steps if s.prompt_tokens is not None or s.completion_tokens is not None),
        key=lambda s: (
            -(s.cost_usd or 0.0),
            -((s.prompt_tokens or 0) + (s.completion_tokens or 0)),
            s.step,
        ),
    )
    tokens = {
        "input": chosen["input"],
        "cached_input": chosen["cached_input"],
        "uncached_input": uncached,
        "output": chosen["output"],
        "reasoning": chosen["reasoning"],
        "cache_write": chosen["cache_write"],
        "total": total,
        "sources": sources,
        "declared": declared,
        "llm_steps": len(llm_steps),
        "llm_steps_with_usage": len(metered),
        "context": {
            "first_prompt_tokens": prompts[0][1] if prompts else None,
            "peak_prompt_tokens": peak[1] if peak else None,
            "peak_step": peak[0] if peak else None,
            "last_prompt_tokens": prompts[-1][1] if prompts else None,
        },
        "top_steps": [
            {
                "step": s.step,
                "input": s.prompt_tokens,
                "output": s.completion_tokens,
                "cost_usd": s.cost_usd,
                "action": _action_preview(s.actions)
                or (_clip(s.message, 100) if s.message.strip() else None),
            }
            for s in by_step[:TOP_N]
        ],
    }
    if metered and len(metered) < len(llm_steps):
        warnings.append(f"token usage recorded on {len(metered)} of {len(llm_steps)} agent steps")
    for field in ("input", "output"):
        total_value, step_value = chosen[field], step_sums[field]
        if total_value and step_value is not None and sources[field] != "step_sum":
            gap = total_value - step_value
            if abs(gap) > 0.05 * total_value:
                warnings.append(
                    f"{gap:,} {field} tokens ({gap / total_value:.1%}) of the run total are not "
                    "attributed to any step; per-step and per-window figures undercount"
                    if gap > 0
                    else f"per-step {field} tokens exceed the run total by {-gap:,}"
                )
    if not metered and chosen["input"] is None:
        warnings.append("no token usage recorded anywhere for this run")

    # The metered proxy ledger (lab-metadata ``cost`` block, used tokens
    # times pinned pricing) is authoritative for proxy-metered trials and
    # wins over Harbor/litellm's own estimate. Anything else keeps the
    # existing precedence below.
    ledger_block = _dict((lab_metadata or {}).get("cost"))
    ledger_usd = ledger_block.get("cost_usd")
    cost_value, cost_source = None, None
    if isinstance(ledger_usd, (int, float)) and not isinstance(ledger_usd, bool):
        cost_value, cost_source = ledger_usd, "proxy_ledger"
    else:
        for source in ("result_json", "trajectory_final_metrics"):
            value = declared[source]["cost_usd"]
            if value is not None:
                cost_value, cost_source = value, source
                break
    steps_with_cost = [s for s in llm_steps if s.cost_usd is not None]
    lower_bound = False
    if cost_value is None and steps_with_cost:
        cost_value, cost_source = step_sums["cost_usd"], "step_sum"
        lower_bound = len(steps_with_cost) < len(llm_steps)
    price_entry = None
    price_model: str | None = None
    if cost_value is None:
        # No harness-reported cost anywhere: an estimate from the pinned
        # provider price table is the only figure allowed, and only for a
        # model with a verified list price. It is never added to harness
        # spend because there is none on this path by construction.
        price_model = _trial_model(result)
        price_entry = lookup_price(price_model)
        if price_entry is None:
            warnings.append(
                "cost unavailable: the harness recorded no cost for this run; "
                f"model {price_model!r} has no pinned price-table entry"
            )
        else:
            estimate = estimate_cost_usd(
                chosen["input"], chosen["cached_input"], chosen["output"], price_entry
            )
            if estimate is None:
                warnings.append(
                    "cost unavailable: the harness recorded no cost for this run; "
                    f"model {price_model!r} matches price-table key "
                    f"{price_entry.key!r} but input/output token counts are missing"
                )
            else:
                cost_value, cost_source = estimate, "price_table_estimate"
    estimated = cost_source == "price_table_estimate" and price_entry is not None
    cost = {
        "total_usd": round(cost_value, 6) if cost_value is not None else None,
        "source": cost_source,
        "harness_cost_source": final_extra.get("cost_source")
        if isinstance(final_extra.get("cost_source"), str)
        else None,
        "is_lower_bound": lower_bound,
        "steps_with_cost": len(steps_with_cost),
        "per_1k_output_tokens_usd": (
            round(cost_value / chosen["output"] * 1000, 6)
            if cost_value is not None and chosen["output"]
            else None
        ),
        "note": (
            "Estimate from pinned provider list prices; not a metered charge or provider invoice."
            if estimated
            else (
                "Metered proxy usage times pinned pricing; not a provider invoice."
                if cost_source == "proxy_ledger"
                else "Harness-reported native ledger; not a provider invoice."
            )
        ),
        "ledger": ledger_block or None,
        "price_table": (
            {
                "model": price_model,
                "matched_key": price_entry.key,
                "source_url": price_entry.source_url,
                "retrieved_on": price_entry.retrieved_on,
                "usd_per_mtok": {
                    "input": price_entry.input_usd_per_mtok,
                    "cached_input": price_entry.cached_input_usd_per_mtok,
                    "output": price_entry.output_usd_per_mtok,
                },
            }
            if estimated
            else None
        ),
    }
    return tokens, cost, warnings


def _tools(actions: Sequence[_Action]) -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {}
    programs: Counter[str] = Counter()
    wrappers: Counter[str] = Counter()
    for action in actions:
        row = rows.setdefault(
            action.tool,
            {
                "tool": action.tool,
                "calls": 0,
                "ok": 0,
                "errors": 0,
                "unknown": 0,
                "output_chars": 0,
                "first_step": action.step,
                "last_step": action.step,
            },
        )
        # A batch of calls sharing one observation fails once, not once per call.
        row["calls"] += action.call_count
        if action.status == "error":
            row["errors"] += 1
        elif action.status == "ok":
            row["ok"] += action.call_count
        row["unknown"] = row["calls"] - row["ok"] - row["errors"]
        row["output_chars"] += action.output_chars or 0
        row["last_step"] = action.step
        if action.wrapper:
            wrappers[action.wrapper] += 1
        program = _program_of(action.target) if action.shell and action.target else None
        if program:
            programs[program] += 1
    table = sorted(rows.values(), key=lambda r: (-r["calls"], r["tool"]))
    for row in table:
        judged = row["ok"] + row["errors"]
        row["judged"] = judged
        row["error_rate"] = round(row["errors"] / judged, 4) if judged else None
    return {
        "total_calls": sum(a.call_count for a in actions),
        "actions": len(actions),
        "distinct_tools": len(rows),
        "polls": sum(1 for a in actions if a.is_poll),
        "by_tool": table,
        "top_programs": [{"program": p, "calls": n} for p, n in programs.most_common(10)],
        "wrapped_calls": dict(wrappers),
    }


# A command cycle is a block of 2-3 distinct normalized commands repeated
# back-to-back at least 3 full times. Signatures reuse the identical-run
# normalization (tool plus whitespace-collapsed command). Only shell actions
# with substantive text count: completion claims such as
# ``mark_task_complete: {}`` interleave with real commands but are not
# commands cycling.
_CYCLE_PERIODS = (2, 3)
_CYCLE_MIN_REPEATS = 3
_CYCLE_MIN_COMMAND_CHARS = 6
_MISSING_DEPENDENCY_RE = re.compile(r"ModuleNotFoundError|No module named [\w.]+")


def _is_cycle_command(action: _Action) -> bool:
    return action.shell and len((action.target or "").strip()) >= _CYCLE_MIN_COMMAND_CHARS


def _longest_cycle(counted: Sequence[_Action]) -> dict[str, Any] | None:
    """Longest back-to-back command cycle, or None when no block repeats 3 times."""
    best: dict[str, Any] | None = None
    total_actions = len(counted)
    for period in _CYCLE_PERIODS:
        if total_actions < period * _CYCLE_MIN_REPEATS:
            continue
        for start in range(total_actions - period * _CYCLE_MIN_REPEATS + 1):
            block = counted[start : start + period]
            if not all(_is_cycle_command(action) for action in block):
                continue
            signatures = [action.signature for action in block]
            if len(set(signatures)) != period:
                continue
            repeats = 1
            while (
                start + (repeats + 1) * period <= total_actions
                and [
                    counted[start + repeats * period + offset].signature for offset in range(period)
                ]
                == signatures
                and all(
                    _is_cycle_command(counted[start + repeats * period + offset])
                    for offset in range(period)
                )
            ):
                repeats += 1
            if repeats < _CYCLE_MIN_REPEATS:
                continue
            total = period * repeats
            if best is None or total > best["period"] * best["repeats"]:
                best = {
                    "period": period,
                    "repeats": repeats,
                    "start_step": counted[start].step,
                    "end_step": counted[start + total - 1].step,
                    "commands": [_clip(action.target) for action in block],
                }
    return best


def _identical_run_loop_delta(length: int) -> float:
    """Score an identical run of ``length`` would add to an empty baseline.

    Mirrors the repeated-command part of the shared loop-suspicion scorer, so
    a command cycle raises suspicion exactly as an identical run of the same
    total length would.
    """
    delta = 0.35 + min(0.35, 0.15)
    if length > 3:
        delta += min(0.30, 0.10 * math.log10(length / 3))
    return delta


def _repeated_outputs(group: Sequence[_Action]) -> int:
    """Occurrences whose result equals an earlier occurrence's result (exact revisits)."""
    seen: set[str] = set()
    count = 0
    for action in group:
        if action.output_hash in seen:
            count += 1
        elif action.output_hash:
            seen.add(action.output_hash)
    return count


def _revisits(
    actions: Sequence[_Action],
    loop_steps: Sequence[LoopStep] | None,
    total_steps: int,
) -> dict[str, Any]:
    counted = [a for a in actions if not a.is_poll]
    windowed = total_steps >= MIN_STEPS_FOR_WINDOWS
    window_counted = [0] * WINDOW_COUNT
    window_repeats = [0] * WINDOW_COUNT
    seen: set[str] = set()
    states: set[tuple[str, str]] = set()
    outputs: dict[str, str] = {}
    groups: dict[str, list[_Action]] = defaultdict(list)
    repeats = returns = consecutive = exact = same_output = 0
    previous: str | None = None
    run: list[int] = []
    longest: list[int] = []
    revisit_steps: list[int] = []
    for action in counted:
        if windowed:
            window_counted[run_report_scale.window_of(action.step, total_steps)] += 1
        groups[action.signature].append(action)
        state = (action.signature, action.output_hash) if action.output_hash else None
        # Cross-action matches need substantial output: many unrelated commands print nothing.
        substantial = action.output_hash is not None and (
            (action.output_chars or 0) >= MIN_REPEATED_OUTPUT_CHARS
        )
        other_producer = substantial and outputs.get(action.output_hash or "") not in (
            None,
            action.signature,
        )
        if action.signature in seen:
            repeats += 1
            if windowed:
                window_repeats[run_report_scale.window_of(action.step, total_steps)] += 1
            revisit_steps.append(action.step)
            if previous == action.signature:
                consecutive += 1
            else:
                returns += 1
            if state in states:
                exact += 1
            elif other_producer:
                same_output += 1
        else:
            seen.add(action.signature)
            if other_producer:
                same_output += 1
        if state is not None:
            states.add(state)
        if substantial and action.output_hash:
            outputs.setdefault(action.output_hash, action.signature)
        run = [*run, action.step] if previous == action.signature else [action.step]
        if len(run) > len(longest):
            longest = run
        previous = action.signature
    error_signatures: Counter[str] = Counter(
        f"{a.tool}\0{a.category}\0{_normalized_output(a.excerpt or '')[:80]}"
        for a in counted
        if a.status == "error"
    )
    repeated_groups = sorted(
        (g for g in groups.values() if len(g) > 1),
        key=lambda g: (-len(g), g[0].step),
    )
    loop = _analyze_loop_suspicion(loop_steps) if loop_steps is not None else None
    cycle = _longest_cycle(counted)
    if loop is not None:
        loop_section: dict[str, Any] | None = {
            "detected": loop.detected,
            "score": loop.score,
            "reasons": list(loop.reasons),
        }
        if cycle is not None:
            cycle_length = cycle["period"] * cycle["repeats"]
            loop_section["reasons"].append(
                f"repeating_command_cycle: period={cycle['period']} "
                f"({cycle['repeats']}\u00d7, steps {cycle['start_step']}\u2013{cycle['end_step']})"
            )
            loop_section["score"] = min(
                1.0, round(loop_section["score"] + _identical_run_loop_delta(cycle_length), 4)
            )
            loop_section["detected"] = loop_section["score"] >= 0.50
    else:
        loop_section = None
    section: dict[str, Any] = {
        "actions_considered": len(counted),
        "distinct_actions": len(groups),
        "repeated_actions": repeats,
        "repeat_rate": round(repeats / len(counted), 4) if counted else None,
        "returns_to_earlier_action": returns,
        "consecutive_repeats": consecutive,
        "exact_revisits": exact,
        "same_result_from_different_action": same_output,
        "repeated_errors": sum(n - 1 for n in error_signatures.values() if n > 1),
        "longest_identical_run": {
            "length": len(longest),
            "steps": list(dict.fromkeys(longest))[:20],
            "start_step": longest[0] if longest else None,
            "end_step": longest[-1] if longest else None,
        },
        "longest_cycle": cycle,
        "most_repeated": [
            {
                "tool": g[0].tool,
                "target": _clip(g[0].target),
                "count": len(g),
                "steps": list(dict.fromkeys(a.step for a in g))[:20],
                "identical_results": _repeated_outputs(g),
            }
            for g in repeated_groups[:TOP_N]
        ],
        "loop_suspicion": loop_section,
    }
    if windowed:
        section["repeats_by_window"] = window_repeats
        onset = repeat_onset(window_counted, window_repeats)
        if onset["status"] == "onset":
            onset["steps"] = list(run_report_scale.window_bounds(onset["window"], total_steps))
        section["revisits_started"] = onset
    return section


def _load_child(
    ref: dict[str, Any],
    embedded: dict[str, dict[str, Any]],
    segment_dir: Path,
    trial_root: Path,
) -> tuple[dict[str, Any] | None, str]:
    trajectory_id = ref.get("trajectory_id")
    if isinstance(trajectory_id, str) and trajectory_id in embedded:
        return embedded[trajectory_id], f"embedded:{trajectory_id}"
    path_value = ref.get("trajectory_path")
    if not isinstance(path_value, str) or not path_value:
        return None, "unresolved"
    candidate = Path(path_value)
    candidate = (
        trial_root / candidate.as_posix().lstrip("/")
        if candidate.is_absolute()
        else segment_dir / candidate
    )
    resolved = candidate.resolve()
    if resolved != trial_root and trial_root not in resolved.parents:
        return None, "escaping"
    if not resolved.is_file():
        return None, f"missing:{path_value}"
    try:
        loaded = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, f"unparseable:{path_value}"
    return (loaded if isinstance(loaded, dict) else None), str(resolved.relative_to(trial_root))


def _first(*values: Any) -> Any:
    """First value that is not None (0 and 0.0 are real measurements)."""
    return next((v for v in values if v is not None), None)


def _child_summary(doc: dict[str, Any]) -> dict[str, Any]:
    raw_steps = [s for s in doc.get("steps") or [] if isinstance(s, dict)]
    stamps = [t for t in (_parse_ts(s.get("timestamp")) for s in raw_steps) if t is not None]
    metrics = [_dict(s.get("metrics")) for s in raw_steps]
    final = _dict(doc.get("final_metrics"))
    agent = _dict(doc.get("agent"))
    children = [c for c in doc.get("subagent_trajectories") or [] if isinstance(c, dict)]
    return {
        "model": agent.get("model_name"),
        "steps": len(raw_steps),
        "tool_calls": sum(
            len(s["tool_calls"]) for s in raw_steps if isinstance(s.get("tool_calls"), list)
        ),
        "input_tokens": _first(
            _int(final.get("total_prompt_tokens")),
            _sum(_int(m.get("prompt_tokens")) for m in metrics),
        ),
        "output_tokens": _first(
            _int(final.get("total_completion_tokens")),
            _sum(_int(m.get("completion_tokens")) for m in metrics),
        ),
        "cost_usd": _first(
            _float(final.get("total_cost_usd")), _sum(_float(m.get("cost_usd")) for m in metrics)
        ),
        "started_at": _iso(min(stamps)) if stamps else None,
        "ended_at": _iso(max(stamps)) if stamps else None,
        "duration_seconds": _seconds(min(stamps), max(stamps)) if len(stamps) > 1 else None,
        "nested_subagents": len(children),
    }


def _raw_step_call_ids(
    step: _Step, segments: Sequence[tuple[Path, dict[str, Any], str]]
) -> list[str]:
    """Raw ATIF ``tool_call_id`` values for a built step.

    Located by (segment, native id): converter-emitted step ids are
    sequential within their own document, so the native id indexes that
    document's step list; any mismatch (foreign layouts, filtered chains)
    yields no ids and the caller falls back.
    """
    if step.native_step_id is None or not 0 <= step.segment < len(segments):
        return []
    raw_steps = segments[step.segment][1].get("steps")
    if not isinstance(raw_steps, list) or not 1 <= step.native_step_id <= len(raw_steps):
        return []
    raw = raw_steps[step.native_step_id - 1]
    if not isinstance(raw, dict):
        return []
    calls = raw.get("tool_calls")
    if not isinstance(calls, list):
        return []
    return [
        call["tool_call_id"]
        for call in calls
        if isinstance(call, dict) and isinstance(call.get("tool_call_id"), str)
    ]


def _subagents(
    steps: Sequence[_Step],
    segments: Sequence[tuple[Path, dict[str, Any], str]],
    trial_dir: Path,
    origin: datetime | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    trial_root = trial_dir.resolve()
    embedded: dict[str, dict[str, Any]] = {}
    for _, data, _ in segments:
        for child in data.get("subagent_trajectories") or []:
            if isinstance(child, dict) and isinstance(child.get("trajectory_id"), str):
                embedded.setdefault(child["trajectory_id"], child)
    items: list[dict[str, Any]] = []
    context_events: list[dict[str, Any]] = []
    referenced: set[str] = set()
    for step in steps:
        segment_dir = segments[step.segment][0].parent
        for ref in step.subagent_refs:
            doc, location = _load_child(ref, embedded, segment_dir, trial_root)
            if isinstance(ref.get("trajectory_id"), str):
                referenced.add(ref["trajectory_id"])
            label = str(ref.get("trajectory_path") or ref.get("trajectory_id") or "")
            kind = "context_summarization" if "summar" in label.lower() else "delegated"
            item = {
                "id": ref.get("trajectory_id") or ref.get("session_id") or label,
                "kind": kind,
                "evidence": "atif_subagent_ref",
                "location": location,
                "spawned_at_step": step.step,
                "spawned_at_offset_seconds": _seconds(origin, step.timestamp),
                **(_child_summary(doc) if doc is not None else {"captured": False}),
            }
            items.append(item)
            if kind == "context_summarization":
                context_events.append(
                    {"step": step.step, "kind": "summarization_subagent", "detail": label}
                )
    for trajectory_id, child in embedded.items():
        if trajectory_id not in referenced:
            items.append(
                {
                    "id": trajectory_id,
                    "kind": "delegated",
                    "evidence": "atif_embedded_unreferenced",
                    "location": f"embedded:{trajectory_id}",
                    "spawned_at_step": None,
                    "spawned_at_offset_seconds": None,
                    **_child_summary(child),
                }
            )
    # Claude Code sidechain attribution, strongest signal first: the per-step
    # ATIF agent id; then joins against the retained native session — the
    # converter copies each raw tool_use block id to ATIF tool_call_id, and
    # the raw event timestamp verbatim to the step timestamp, while the raw
    # events holding those keys carry the camelCase agentId the converter
    # otherwise drops. Timestamp attribution applies only when the timestamp
    # names exactly one agent; then contiguous-run fallback for steps with no
    # joined key. The session tables build lazily — most harnesses have no
    # anonymous sidechain steps at all. (HAR-76 harness gaps.)
    session_tables: tuple[dict[str, str], dict[datetime, set[str]]] | None = None

    def _joined_agent(step: _Step) -> str | None:
        nonlocal session_tables
        if step.agent_id:
            return step.agent_id
        if session_tables is None:
            session_tables = sidechain_session_tables(trial_dir)
        by_call, by_stamp = session_tables
        for call_id in _raw_step_call_ids(step, segments):
            if call_id in by_call:
                return by_call[call_id]
        if step.timestamp is not None:
            agents = by_stamp.get(normalize_timestamp(step.timestamp))
            if agents is not None and len(agents) == 1:
                return next(iter(agents))
        return None

    sidechain_groups: dict[str, list[_Step]] = {}
    current_run = 0
    previous_side = False
    for step in steps:
        if step.is_sidechain:
            if not previous_side:
                current_run += 1
            key = _joined_agent(step) or f"sidechain-run-{current_run}"
            sidechain_groups.setdefault(key, []).append(step)
        previous_side = step.is_sidechain
    for key, group in sidechain_groups.items():
        stamps = [s.timestamp for s in group if s.timestamp]
        items.append(
            {
                "id": key,
                "kind": "delegated",
                "evidence": "claude_code_sidechain",
                "location": "inline sidechain steps",
                "spawned_at_step": group[0].step,
                "spawned_at_offset_seconds": _seconds(origin, group[0].timestamp),
                "model": group[0].model,
                "steps": len(group),
                "tool_calls": sum(len(s.actions) for s in group),
                "input_tokens": _sum(s.prompt_tokens for s in group),
                "output_tokens": _sum(s.completion_tokens for s in group),
                "cost_usd": _sum(s.cost_usd for s in group),
                "started_at": _iso(min(stamps)) if stamps else None,
                "ended_at": _iso(max(stamps)) if stamps else None,
                "duration_seconds": _seconds(min(stamps), max(stamps)) if len(stamps) > 1 else None,
            }
        )
    delegations: list[dict[str, Any]] = [
        {
            "step": step.step,
            "offset_seconds": _seconds(origin, step.timestamp),
            "tool": action.tool,
            "target": _clip(action.target, 120),
        }
        for step in steps
        if not step.is_sidechain
        for action in step.actions
        if action.tool.lower() in _DELEGATION_TOOLS
        or any(t.lower() in _DELEGATION_TOOLS for t in action.inner_tools)
    ]
    codex_rollouts: dict[str, Any] | None = None
    reason: str | None = None
    root_doc = segments[0][1] if segments else {}
    agent_name = _dict(root_doc.get("agent")).get("name")
    if agent_name is None and not segments:
        try:
            fallback = json.loads((trial_root / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            fallback = {}
        if isinstance(fallback, dict):
            agent_name = _dict(fallback.get("agent_info")).get("name") or _dict(
                _dict(fallback.get("config")).get("agent")
            ).get("name")
    if agent_name == "codex":
        session_id = root_doc.get("session_id")
        read = read_codex_rollouts(
            trial_dir, parent_session_id=session_id if isinstance(session_id, str) else None
        )
        codex_rollouts = {
            "status": read.status,
            "reason": read.reason,
            "parent": read.parent,
            "files": list(read.files),
            "other_threads": list(read.other_threads),
        }
        spawn_steps = [d for d in delegations if d["tool"].lower() == "spawn_agent"]
        for child in read.children:
            spawned_at_step: int | None = None
            spawned_at_offset: float | None = None
            if child.spawn_call_id is not None and child.spawn_call_id in read.parent_spawns:
                index = read.parent_spawns.index(child.spawn_call_id)
                if index < len(spawn_steps):
                    spawned_at_step = spawn_steps[index]["step"]
                    spawned_at_offset = spawn_steps[index]["offset_seconds"]
            items.append(
                {
                    "id": child.thread_id,
                    "kind": "delegated",
                    "evidence": "codex_native_rollout",
                    "location": child.location,
                    "spawned_at_step": spawned_at_step,
                    "spawned_at_offset_seconds": spawned_at_offset,
                    "model": child.model,
                    "steps": child.steps,
                    "tool_calls": child.tool_calls,
                    "input_tokens": child.input_tokens,
                    "output_tokens": child.output_tokens,
                    "cost_usd": None,
                    "started_at": child.started_at,
                    "ended_at": child.ended_at,
                    "duration_seconds": child.duration_seconds,
                    "nested_subagents": None,
                    "depth": child.depth,
                    "agent_role": child.agent_role,
                    "agent_nickname": child.agent_nickname,
                }
            )
    items.sort(
        key=lambda i: (i["spawned_at_step"] is None, i["spawned_at_step"] or 0, str(i["id"]))
    )
    if items:
        observability = "captured"
    elif delegations:
        observability = "delegations_only"
    elif codex_rollouts is not None and codex_rollouts["status"] in ("absent", "unreadable"):
        observability = "unavailable"
        reason = codex_rollouts["reason"]
    else:
        observability = "none_observed"
    return {
        "observability": observability,
        "reason": reason,
        "count": len(items),
        "delegated_count": sum(1 for i in items if i["kind"] == "delegated"),
        "summarization_count": sum(1 for i in items if i["kind"] == "context_summarization"),
        "delegation_calls": delegations,
        "items": items,
        "codex_rollouts": codex_rollouts,
    }, context_events


def _context(
    steps: Sequence[_Step],
    segments: Sequence[tuple[Path, dict[str, Any], str]],
    subagent_events: list[dict[str, Any]],
    trial_dir: Path,
    compactions: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    events = list(subagent_events)
    previous_segment: int | None = None
    for step in steps:
        if previous_segment is not None and step.segment != previous_segment:
            name = segments[step.segment][0].name
            events.append({"step": step.step, "kind": "continuation_segment", "detail": name})
        if step.context_event:
            events.append(
                {"step": step.step, "kind": "context_management", "detail": step.context_event}
            )
        for notice in step.notices:
            events.append(
                {"step": step.step, "kind": "harness_notice", "detail": _clip(notice, 160)}
            )
        previous_segment = step.segment
    events.extend(
        {
            "step": drop["step"],
            "kind": "inferred_context_drop",
            "detail": f"input tokens fell {drop['from_tokens']:,} -> {drop['to_tokens']:,}",
        }
        for drop in compactions
    )
    copied = sum(
        1
        for _, data, _ in segments
        for raw in data.get("steps") or []
        if isinstance(raw, dict) and raw.get("is_copied_context")
    )
    root = trial_dir.resolve()
    return {
        "segments": [
            str(path.resolve().relative_to(root)) if root in path.resolve().parents else path.name
            for path, _, _ in segments
        ],
        "copied_context_steps_excluded": copied,
        "events": sorted(events, key=lambda e: (e["step"], e["kind"])),
    }


def _errors(
    actions: Sequence[_Action], result: dict[str, Any], origin: datetime | None
) -> dict[str, Any]:
    errors = [a for a in actions if a.status == "error"]
    categories = Counter(a.category for a in errors)
    exception = _dict(result.get("exception_info"))
    message = exception.get("exception_message") or exception.get("message")
    last_judged = next((a for a in reversed(actions) if a.status != "unknown"), None)
    return {
        "tool_errors": len(errors),
        "harness_signalled": sum(1 for a in errors if a.evidence not in (None, "output_text")),
        "inferred_from_output": sum(1 for a in errors if a.evidence == "output_text"),
        "status_unknown_calls": sum(a.call_count for a in actions)
        - sum(a.call_count for a in actions if a.status == "ok")
        - len(errors),
        "calls_without_result": sum(a.call_count for a in actions if a.evidence == "no_result"),
        "not_executed_calls": sum(a.call_count for a in actions if a.evidence == "not_executed"),
        "expected_probe_misses": sum(1 for a in actions if a.category == "expected_probe_miss"),
        "by_category": dict(sorted(categories.items(), key=lambda kv: (-kv[1], kv[0]))),
        "first_error": {
            "step": errors[0].step,
            "offset_seconds": _seconds(origin, errors[0].timestamp),
            "evidence": errors[0].evidence,
        }
        if errors
        else None,
        "ended_in_error": bool(last_judged and last_judged.status == "error"),
        "examples": [
            {
                "step": a.step,
                "tool": a.tool,
                "target": _clip(a.target, 120),
                "category": a.category,
                "evidence": a.evidence,
                "excerpt": a.excerpt,
            }
            for a in errors[:TOP_N]
        ],
        "exception": {
            "type": exception.get("exception_type") or exception.get("type"),
            "message": _clip(str(message), 300) if message else None,
            "occurred_at": exception.get("occurred_at"),
        }
        if exception
        else None,
    }


def _first_failure(
    steps: Sequence[_Step],
    actions: Sequence[_Action],
    fetches: Sequence[Mapping[str, Any]],
    cycle: Mapping[str, Any] | None,
    reward: float | None,
    total_steps: int,
) -> dict[str, Any]:
    """Estimate the first outcome-relevant failure: ``outcome.first_failure``.

    ``errors.first_error`` answers "what errored first", which on hand labels
    matches where a run really went wrong only 3/10 times: early
    exploration noise (a missed grep, a failed import probe) fires before the
    decisive step. This estimate instead walks a precedence of cheap,
    deterministic signals, first match wins:

    1. ``upstream_fetch`` (high): the first outside-code fetch that was read
       back into the transcript. A fetch nobody reads is reconnaissance; a
       read-back fetch is the step a copied pass starts from.
    2. ``harness_rejection`` (high): the first agent step with no executed
       tool call. The harness declined the turn (malformed call, prose with
       no call), so nothing ran.
    3. ``stuck_cycle`` (medium): the start of an alternating command cycle
       that runs to the final step of a passing run. After a pass, ping-
       ponging instead of confirming means the agent stalled on the finish;
       after a failure the same shape is thrash around the real bug, so the
       rule stays silent and the edit signals below decide. A plain
       identical run to the end is still task-directed verification and does
       not count.
    4. ``tool_error`` (low): a missing-dependency error
       (``ModuleNotFoundError`` / ``No module named ...``) before the first
       repo edit, on a run that never passes. A missing dependency is
       pre-existing breakage no later code edit can fix, so it outranks the
       edit signals below; anything else pre-edit is exploration noise the
       agent worked around. Recovery is run-level (pass/fail) because
       per-command success is unobservable: the modal harness never emits
       an ``ok`` status. Tried and rejected: any pre-edit error on a failed
       run (loses two hand reads whose probes errored before the decisive
       edit), and any pre-edit error whose text recurs later (a hand-read
       probe's ``AttributeError`` recurs in later exploration noise).
    5. ``bad_edit`` (medium): the first repo edit (writes under /tmp and
       other scratch never count) when a tool error occurs at or after it.
       The edit changed what later checks observe.

    No signal fires for a silent failure (an incomplete edit with no failing
    check, a detour that never touches the repo): the field is null rather
    than a guess. Calibrated on 10 HAR-104 hand reads (8/10 within +/-2
    steps, misses are the two silent cases) and 44 probe-03 capability
    labels (29/44 agreement, up from 10/44 for ``first_error``, with no
    baseline hit lost).
    """
    none: dict[str, Any] = {"step": None, "kind": None, "evidence": None, "confidence": None}
    if not steps:
        return {**none, "evidence": "no trajectory"}
    for fetch in fetches:
        if fetch.get("read_back_step") is not None:
            return {
                "step": fetch["step"],
                "kind": "upstream_fetch",
                "evidence": (
                    f"{fetch.get('kind')} {fetch.get('target')} "
                    f"(read back at step {fetch['read_back_step']})"
                ),
                "confidence": "high",
            }
    for step in steps:
        if step.source == "agent" and not step.actions:
            detail = _clip(f"agent step with no executed tool call: {step.message.strip()}", 160)
            return {
                "step": step.step,
                "kind": "harness_rejection",
                "evidence": detail or "agent step with no executed tool call",
                "confidence": "high",
            }
    if (
        cycle is not None
        and reward is not None
        and reward >= PASS_REWARD
        and (cycle.get("end_step") or 0) >= total_steps - 1
    ):
        commands = " / ".join(
            clipped
            for command in (cycle.get("commands") or [])[:2]
            if (clipped := _clip(command, 60))
        )
        return {
            "step": cycle["start_step"],
            "kind": "stuck_cycle",
            "evidence": (
                f"period-{cycle.get('period')} command cycle "
                f"x{cycle.get('repeats')} to step {cycle.get('end_step')}"
                + (f": {commands}" if commands else "")
            ),
            "confidence": "medium",
        }
    edits: dict[int, list[str]] = {}
    for action in actions:
        for path in repo_edit_paths(action.tool, action.target or ""):
            edits.setdefault(action.step, []).append(path)
    errors = [action for action in actions if action.status == "error"]
    if edits:
        first_edit = min(edits)
        first_error = min((action.step for action in errors), default=None)
        recovered = reward is not None and reward >= PASS_REWARD
        if (
            first_error is not None
            and first_error < first_edit
            and not recovered
            and any(
                _MISSING_DEPENDENCY_RE.search(action.excerpt or "")
                for action in errors
                if action.step == first_error
            )
        ):
            culprit = next(action for action in errors if action.step == first_error)
            return {
                "step": first_error,
                "kind": "tool_error",
                "evidence": _clip(f"{culprit.tool} {culprit.target} [{culprit.category}]", 160),
                "confidence": "low",
            }
        if any(action.step >= first_edit for action in errors):
            wrote = sorted(set(edits[first_edit]))[:3]
            first_error_at = min(action.step for action in errors if action.step >= first_edit)
            return {
                "step": first_edit,
                "kind": "bad_edit",
                "evidence": (
                    f"first repo edit ({', '.join(wrote)}) with a tool error "
                    f"at step {first_error_at} or later"
                ),
                "confidence": "medium",
            }
    return none


def _binding_ceiling(trial_dir: Path, result: dict[str, Any]) -> str | None:
    """Name the binding trial-budget ceiling, or None when caps are unknown.

    Compares the job's recorded caps (``lab-metadata.json`` first, then the
    top-level ``experiment-spec.json`` keys) against the trial's utilization
    the way probe-03 ``ceiling_which`` does, plus the cost ceiling. Unknown
    caps read as None, never a guess.
    """
    caps: dict[str, float | None] = {
        "max_input_tokens": None,
        "max_output_tokens": None,
        "max_requests": None,
        "max_total_tokens": None,
        "cost_limit_usd": None,
    }

    def _collect(node: Any) -> None:
        if isinstance(node, dict):
            for key in (
                "max_input_tokens",
                "max_output_tokens",
                "max_requests",
                "max_total_tokens",
            ):
                value = node.get(key)
                if (
                    caps[key] is None
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                ):
                    caps[key] = value
            if caps["cost_limit_usd"] is None:
                for key in ("cost_limit_usd", "max_cost_usd"):
                    value = node.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        caps["cost_limit_usd"] = float(value)
                        break
                micros = node.get("max_cost_micros")
                if (
                    caps["cost_limit_usd"] is None
                    and isinstance(micros, (int, float))
                    and not isinstance(micros, bool)
                ):
                    caps["cost_limit_usd"] = float(micros) / 1_000_000
            for value in node.values():
                _collect(value)
        elif isinstance(node, list):
            for value in node:
                _collect(value)

    job_dir = trial_dir.parent
    for name in ("lab-metadata.json", "experiment-spec.json"):
        try:
            document = json.loads((job_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        _collect(document)
        if all(value is not None for value in caps.values()):
            break

    agent_result = _dict(result.get("agent_result"))
    agent_metadata = _dict(agent_result.get("metadata"))
    n_input = _int(agent_result.get("n_input_tokens"))
    n_output = _int(agent_result.get("n_output_tokens"))
    n_episodes = _int(agent_metadata.get("n_episodes"))
    cost_usd = _float(agent_result.get("cost_usd"))
    if cost_usd is None:
        cost_usd = _float(result.get("cost_usd"))
    candidates: list[tuple[str, float | None, float | None]] = [
        ("input_tokens", n_input, caps["max_input_tokens"]),
        ("output_tokens", n_output, caps["max_output_tokens"]),
        ("requests", n_episodes, caps["max_requests"]),
        ("cost", cost_usd, caps["cost_limit_usd"]),
    ]
    total = None
    if n_input is not None and n_output is not None:
        total = n_input + n_output
    candidates.append(("total_tokens", total, caps["max_total_tokens"]))
    best: str | None = None
    best_share = -1.0
    for name, used, cap in candidates:
        if isinstance(used, (int, float)) and isinstance(cap, (int, float)) and cap:
            share = used / cap
            if share > best_share:
                best, best_share = name, share
    return best


_COMPLETION_TOOL = "mark_task_complete"
# A shell command that only echoes the completion token (the model retrying
# "task_complete" in prose after the harness demanded the real call).
_COMPLETION_ECHO_RE = re.compile(r"""['"]?echo\s+['"]?task_complete['"]?['"]?""")


_COMPLETION_STOPS = frozenset({"task_complete", "prose_completion"})


def _completion_claim_steps(
    positioned: Sequence[tuple[int, Any]],
    layers: Sequence[dict[str, Any] | None],
    actions: Sequence[_Action],
) -> list[int]:
    """Ordinals where the model claimed completion (layer flag or completion call)."""
    padded = list(layers) + [None] * max(0, len(positioned) - len(layers))
    well_formed = [
        (raw, layer)
        for (_, raw), layer in zip(positioned, padded, strict=False)
        if isinstance(raw, dict)
    ]
    claims: set[int] = set()
    for ordinal, (_raw, layer) in enumerate(well_formed, start=1):
        accepted = layer.get("accepted") if isinstance(layer, Mapping) else None
        if isinstance(accepted, Mapping) and accepted.get("task_complete") is True:
            claims.add(ordinal)
    claims |= {a.step for a in actions if a.tool == _COMPLETION_TOOL}
    return sorted(claims)


def _completion_verdict(
    *,
    claim_steps: Sequence[int],
    steps: Sequence[_Step],
    last_task_complete: bool | None,
    stop_reason: str | None,
) -> str | None:
    """One-line completion verdict: first claim, confirmation, and final turn.

    Terminus confirms a claim only when the very next turn claims again; any other turn in
    between resets it. A final-turn claim counts as confirmed only when the run actually
    stopped on it, and not on a budget, timeout or error that happened to follow it.
    """
    if not steps:
        return None
    last = steps[-1]
    if last.actions:
        targets = [
            a.target for a in last.actions if a.target and a.target.strip() not in {"{}", "[]"}
        ]
        if targets:
            detail = targets[0] if len(targets) == 1 else "; ".join(targets)
        else:
            # A bare mark_task_complete carries no arguments; name the tool, not "{}".
            detail = ", ".join(dict.fromkeys(a.tool for a in last.actions)) or "no tool call"
        detail = _clip(detail, 120) or "no tool call"
    elif last.message.strip():
        detail = _clip(last.message, 120) or "no tool call"
    else:
        detail = "no tool call"
    if not claim_steps:
        return f"Completion: never claimed; the run ended on step {last.step} ({detail})."
    first = claim_steps[0]
    claimed = set(claim_steps)
    order = [s.step for s in steps]
    confirmed = next(
        (
            cur
            for prev, cur in zip(order, order[1:], strict=False)
            if prev in claimed and cur in claimed
        ),
        None,
    )
    if confirmed is None and last_task_complete is True and stop_reason in _COMPLETION_STOPS:
        confirmed = claim_steps[-1]
    if confirmed is not None:
        if confirmed == first:
            return (
                f"Completion: claimed at step {first} on the final turn (confirmed); "
                f"the run ended on step {last.step} ({detail})."
            )
        return (
            f"Completion: claimed at step {first}, confirmed at step {confirmed}; "
            f"the run ended on step {last.step} ({detail})."
        )
    return (
        f"Completion: claimed at step {first}, never confirmed; "
        f"the run ended on step {last.step} ({detail})."
    )


def _outcome(
    result: dict[str, Any],
    steps: Sequence[_Step],
    trial_dir: Path,
    *,
    layer_summary: Mapping[str, Any] | None = None,
    stop: tuple[str | None, str] | None = None,
    problems: Mapping[str, Any] | None = None,
    completion: str | None = None,
) -> dict[str, Any]:
    verifier = _dict(result.get("verifier_result"))
    rewards = {
        str(k): float(v) for k, v in _dict(verifier.get("rewards")).items() if _float(v) is not None
    }
    from evallab.probe03 import read_reward

    reward, scored, _source = read_reward(trial_dir)
    judged = [reward] if scored and reward is not None else []
    exception = _dict(result.get("exception_info"))
    if not judged:
        verdict = "errored" if exception else "not_scored"
    elif all(value >= PASS_REWARD for value in judged):
        verdict = "passed"
    elif all(value <= 0 for value in judged):
        verdict = "failed"
    else:
        verdict = "partial"
    final_message = next(
        (s.message for s in reversed(steps) if s.source == "agent" and s.message.strip()), None
    )
    verifier_dir = trial_dir / "verifier"
    stop_reason, stop_detail = stop if stop is not None else (None, "unknown: no stop evidence")
    return {
        "verdict": verdict,
        "reward": reward,
        "rewards": rewards,
        # HAR-92: verifier outcome, stop reason, and execution problems are
        # three separate facts. A scored timeout still passes or fails on its
        # reward; the stop reason says why the agent phase ended.
        "verifier_outcome": verifier_outcome({"reward": reward} if scored else {}),
        "stop_reason": stop_reason,
        "stop_detail": stop_detail,
        # HAR-156: execution lens on how the run ended, separate from the
        # verifier verdict above. A scored timeout still passes or fails on
        # its reward; the category says whose limit (if any) stopped it.
        "stop_category": stop_category(stop_reason),
        "completion": completion,
        "execution_problems": dict(problems) if problems is not None else {},
        "final_agent_message": _clip(final_message, 600) if final_message else None,
        "verifier_files": sorted(
            str(p.relative_to(trial_dir)) for p in verifier_dir.rglob("*") if p.is_file()
        )[:20]
        if verifier_dir.is_dir()
        else [],
    }


def _step_flags(
    step: _Step,
    revisit_steps: set[int],
    error_steps: set[int],
    spawn_steps: set[int],
    event_steps: set[int],
    completion_steps: frozenset[int] | set[int] = frozenset(),
) -> list[str]:
    flags: list[str] = []
    if step.step in error_steps:
        flags.append("error")
    if step.step in revisit_steps:
        flags.append("revisit")
    if step.step in spawn_steps:
        flags.append("subagent")
    if step.step in event_steps:
        flags.append("context")
    if step.step in completion_steps:
        flags.append("completion")
    if step.is_sidechain:
        flags.append("sidechain")
    return flags


def _timeline(
    steps: Sequence[_Step],
    actions: Sequence[_Action],
    subagents: dict[str, Any],
    context: dict[str, Any],
    origin: datetime | None,
    limit: int | None,
    compactions: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    seen: set[str] = set()
    revisit_steps: set[int] = set()
    for action in actions:
        if action.is_poll:
            continue
        if action.signature in seen:
            revisit_steps.add(action.step)
        seen.add(action.signature)
    error_steps = {a.step for a in actions if a.status == "error"}
    spawn_steps = {i["spawned_at_step"] for i in subagents["items"] if i["spawned_at_step"]}
    spawn_steps |= {d["step"] for d in subagents["delegation_calls"]}
    event_steps = {e["step"] for e in context["events"]}
    completion_steps = {
        a.step
        for a in actions
        if a.tool == _COMPLETION_TOOL
        or (a.shell and _COMPLETION_ECHO_RE.fullmatch(a.target.strip()))
    }
    # The step after a completion claim carries the harness's answer to it
    # (for example the "are you sure" confirm prompt), so it stays visible too.
    notable = (
        revisit_steps
        | error_steps
        | spawn_steps
        | event_steps
        | completion_steps
        | {step + 1 for step in completion_steps}
    )

    def entry(step: _Step) -> dict[str, Any]:
        statuses = {a.status for a in step.actions}
        return {
            "step": step.step,
            "offset_seconds": _seconds(origin, step.timestamp),
            "source": step.source,
            "action": _action_preview(step.actions),
            "status": "error"
            if "error" in statuses
            else ("ok" if statuses == {"ok"} else ("unknown" if statuses else None)),
            "input_tokens": step.prompt_tokens,
            "output_tokens": step.completion_tokens,
            "cost_usd": step.cost_usd,
            "message": _clip(step.message, 140 if step.source == "agent" else 80)
            if step.message.strip()
            else None,
            "flags": _step_flags(
                step, revisit_steps, error_steps, spawn_steps, event_steps, completion_steps
            ),
        }

    if limit is None or len(steps) <= limit:
        chosen = list(steps)
    else:
        edge = max(1, limit // 6)
        keep = {s.step for s in steps[:edge]} | {s.step for s in steps[-edge:]}
        for step in steps:
            if len(keep) >= limit:
                break
            if step.step in notable:
                keep.add(step.step)
        chosen = [s for s in steps if s.step in keep]
    entries: list[dict[str, Any]] = []
    previous_step = 0
    for step in chosen:
        if step.step > previous_step + 1:
            entries.append({"omitted_steps": [previous_step + 1, step.step - 1]})
        entries.append(entry(step))
        previous_step = step.step
    if steps and previous_step < steps[-1].step:
        entries.append({"omitted_steps": [previous_step + 1, steps[-1].step]})
    compaction_steps = {drop["step"] for drop in compactions}
    windows = step_windows(steps, revisit_steps, compaction_steps)
    return {
        "total_steps": len(steps),
        "shown_steps": len(chosen),
        "entries": entries,
        "windows": windows,
        "time_windows": time_windows(steps, revisit_steps, compaction_steps),
    }


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def _identity(
    result: dict[str, Any],
    trial_dir: Path,
    root_doc: dict[str, Any],
) -> dict[str, Any]:
    config = _dict(result.get("config"))
    agent_info = _dict(result.get("agent_info"))
    agent_cfg = _dict(config.get("agent"))
    model_info = _dict(agent_info.get("model_info"))
    agent_doc = _dict(root_doc.get("agent"))
    model = model_info.get("name") or agent_cfg.get("model_name") or agent_doc.get("model_name")
    if model and model_info.get("provider") and "/" not in str(model):
        model = f"{model_info['provider']}/{model}"
    return {
        "trial_name": result.get("trial_name") or trial_dir.name,
        "trial_id": result.get("id"),
        "task": result.get("task_name") or None,
        "job": trial_dir.parent.name,
        "agent": agent_info.get("name") or agent_cfg.get("name") or agent_doc.get("name"),
        "agent_version": agent_info.get("version") or agent_doc.get("version"),
        "model": model,
        "atif_schema": root_doc.get("schema_version"),
        "session_id": root_doc.get("session_id"),
        "trial_dir": str(trial_dir),
    }


def _summary_line(report: dict[str, Any]) -> str:
    identity = report["identity"]
    outcome = report["outcome"]
    timing = report["timing"]
    tokens = report["tokens"]
    cost = report["cost"]
    tools = report["tools"]
    revisits = report["revisits"]
    subagents = report["subagents"]
    who = f"{identity['agent'] or 'unknown agent'} ({identity['model'] or 'unknown model'})"
    reward = outcome["reward"]
    verdict = outcome["verdict"].replace("_", " ")
    result = f"{verdict}" + (f" (reward {reward:g})" if reward is not None else "")
    wall = _fmt_seconds(timing["total_seconds"])
    agent = _fmt_seconds(timing["phases"]["agent_execution"]["seconds"])
    parts = [
        f"{who} {result} on {identity['task'] or 'an unknown task'}",
        f"in {wall} wall ({agent} agent)",
    ]
    if report["availability"]["trajectory"] == "present":
        spend = _fmt_tokens(tokens["total"])
        money = f"${cost['total_usd']:.4f}" if cost["total_usd"] is not None else "cost unavailable"
        parts.append(
            f"{_plural(report['timeline']['total_steps'], 'step')}, "
            f"{_plural(tools['total_calls'], 'tool call')}, "
            f"{_plural(report['errors']['tool_errors'], 'error')}, {spend} tokens, {money}"
        )
        parts.append(
            f"{_plural(revisits['repeated_actions'], 'repeated action')} "
            f"({_plural(revisits['exact_revisits'], 'exact revisit')})"
        )
        parts.append(
            "no subagents observed"
            if subagents["observability"] == "none_observed"
            else f"{subagents['count']} subagents, {len(subagents['delegation_calls'])} delegation calls"
        )
    else:
        parts.append(f"no trajectory ({report['availability']['reason']})")
    return "; ".join(parts) + "."


def _linked_capture(trial: Path) -> dict[str, Any] | None:
    """Linked independent-capture row for a trial, or None when absent."""
    try:
        from evallab.model_capture import find_trial_capture
    except ImportError:
        return None
    return find_trial_capture(trial)


def build_run_report(
    trial_dir: str | Path, *, timeline_limit: int | None = DEFAULT_TIMELINE_LIMIT
) -> dict[str, Any]:
    """Build the ``evallab.run_report/v1`` dictionary for one Harbor trial directory."""
    from evallab.process_job import _setup_deviations

    trial = Path(trial_dir).resolve()
    result_path = trial / "result.json"
    try:
        loaded = (
            json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
        )
    except (OSError, ValueError):
        loaded = {}
    result = _dict(loaded)
    quality: list[str] = []
    if not result:
        quality.append("result.json missing or unreadable: outcome and phase timing unavailable")

    # Loop suspicion is computed from the same single parse that builds the
    # steps below; the trajectory document is no longer read a second time.
    try:
        _, traj_path, _ = resolve_trial_target(trial, repo_root=trial, explicit_runs_root=trial)
    except (TrajectoryError, ValueError, OSError):
        traj_path = None

    chain_complete = False
    segments: tuple[tuple[Path, dict[str, Any], str], ...] = ()
    availability: dict[str, Any] = {"trajectory": "present", "reason": None}
    if traj_path is None or not traj_path.is_file():
        agent_name = str(
            _dict(result.get("agent_info")).get("name")
            or _dict(_dict(result.get("config")).get("agent")).get("name")
            or ""
        )
        reason = (
            _CONTROL_REASON
            if agent_name in CONTROL_AGENTS
            else "trajectory file missing (agent may have crashed before writing it)"
        )
        availability = {"trajectory": "absent", "reason": reason}
    else:
        try:
            data = json.loads(traj_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            data = None
            availability = {"trajectory": "unreadable", "reason": f"{type(exc).__name__}"}
        if isinstance(data, dict):
            chain = _resolve_chain_segments(traj_path, data, trial)
            segments = chain.segments
            chain_complete = chain.complete
            if not chain.complete:
                quality.append(
                    f"continuation chain incomplete: {chain.stopped_ref!r} is {chain.stop_cause}; "
                    "totals come from the last readable segment"
                )
        elif data is not None:
            availability = {"trajectory": "unreadable", "reason": "top-level JSON is not an object"}
    if availability["trajectory"] != "present":
        quality.append(f"trajectory {availability['trajectory']}: {availability['reason']}")

    if segments:
        positioned, stitch_stats = stitched_chain_action_steps(segments)
    else:
        positioned, stitch_stats = [], StitchStats()
    parse = _replay_parser()
    layers = _positioned_layers(positioned, parse)
    steps, loop_steps = _build_steps(positioned, layers)
    if len(steps) < len(positioned):
        quality.append(f"{len(positioned) - len(steps)} malformed (non-object) steps skipped")
    if parse is None and not any(isinstance(layer, Mapping) for layer in layers):
        quality.append(
            "step layers unavailable: no recorded layers and no parser to reconstruct them"
        )
    if stitch_stats.duplicated_steps:
        quality.append(
            f"{stitch_stats.duplicated_steps} steps shared across continuation parts counted once"
        )
    agent_dir = segments[0][0].parent if segments else trial / "agent"
    agent_metadata = _dict(_dict(result.get("agent_result")).get("metadata"))
    summarization_count = agent_metadata.get("summarization_count")
    coverage = coverage_record(
        discover_trajectory_parts(agent_dir),
        stitch_stats,
        summarization_count=summarization_count if isinstance(summarization_count, int) else None,
        step_lists={path.name: doc.get("steps") for path, doc, _ in segments} if segments else None,
    )
    layer_summary = summarize_layers(
        [raw for _, raw in positioned],
        dict(enumerate(layers)),
        layers_missing_why=(
            "no recorded step_layers and Harbor's Terminus parser is not importable "
            "for reconstruction (retry with `uv run --no-sync --with harbor==0.21.0 "
            "evallab report run --json <trial-dir>`)"
            if parse is None
            else None
        ),
    )
    lab_metadata = _lab_metadata(trial)
    last_task_complete, last_prose_completion = _final_turn_flags(positioned, layers)
    stop_reason, stop_detail = classify_stop_reason(
        agent_metadata=agent_metadata,
        exception_info=_dict(result.get("exception_info")),
        last_task_complete=last_task_complete,
        last_prose_completion=last_prose_completion,
    )
    if stop_reason == "trial_budget_exhausted":
        ceiling = _binding_ceiling(trial, result)
        stop_detail = (
            f"{stop_detail}; binding ceiling: {ceiling}"
            if ceiling is not None
            else f"{stop_detail}; binding ceiling unknown (no trial caps recorded)"
        )
    problems = execution_problems(layer_summary=layer_summary, lab_metadata=lab_metadata)
    actions = [a for s in steps for a in s.actions]
    completion = _completion_verdict(
        claim_steps=_completion_claim_steps(positioned, layers, actions),
        steps=steps,
        last_task_complete=last_task_complete,
        stop_reason=stop_reason,
    )
    root_doc = segments[0][1] if segments else {}
    terminal_doc = segments[-1][1] if segments else {}

    timing, origin = _timing(result, steps)
    if timing["phases_overlap"]:
        quality.append("Harbor phase timestamps overlap or exceed the trial wall time")
    tokens, cost, token_warnings = _tokens_and_cost(result, terminal_doc, steps, lab_metadata)
    if availability["reason"] != _CONTROL_REASON:
        quality.extend(token_warnings)
    if steps and not any(s.timestamp for s in steps):
        quality.append("steps carry no timestamps: per-step timing unavailable")
    compactions = infer_compactions(
        steps, min_prompt=_COMPACTION_MIN_PROMPT, drop_ratio=_COMPACTION_DROP_RATIO
    )
    subagents, subagent_events = _subagents(steps, segments, trial, origin)
    context = _context(steps, segments, subagent_events, trial, compactions)
    errors = _errors(actions, result, origin)
    if actions and all(a.evidence in (None, "output_text", "no_result") for a in actions):
        quality.append(
            "harness reports no per-call status (no exit codes or error flags): tool errors "
            "are only inferred from output text"
        )
    domain = domain_section(trial, result)
    # Outline-parity gate: loop suspicion only for a complete, well-formed chain
    # (the same conditions under which an outline would have been "featured").
    loop_view = (
        loop_steps
        if len(steps) == len(positioned)
        and all(isinstance(doc.get("steps"), list) and doc.get("steps") for _, doc, _ in segments)
        and chain_complete
        else None
    )
    outcome = _outcome(
        result,
        steps,
        trial,
        layer_summary=layer_summary,
        stop=(stop_reason, stop_detail),
        problems=problems,
        completion=completion,
    )
    outside_fetches, copied_pass = outside_fetch_section(actions, outcome["reward"])
    revisits = _revisits(actions, loop_view, len(steps))
    outcome["first_failure"] = _first_failure(
        steps,
        actions,
        outside_fetches["items"],
        revisits.get("longest_cycle"),
        outcome["reward"],
        len(steps),
    )
    report: dict[str, Any] = {
        "schema": RUN_REPORT_SCHEMA,
        "identity": _identity(result, trial, root_doc),
        "availability": availability,
        "outcome": outcome,
        "timing": timing,
        "tokens": tokens,
        "cost": cost,
        "tools": _tools(actions),
        "revisits": revisits,
        "subagents": subagents,
        "context": context,
        "errors": errors,
        "domain": domain,
        "outside_fetches": outside_fetches,
        "pass_may_be_copied": copied_pass,
        "timeline": _timeline(
            steps, actions, subagents, context, origin, timeline_limit, compactions
        ),
        "data_quality": quality,
        "sources": [
            {
                "path": str(path.relative_to(trial)) if trial in path.parents else str(path),
                "sha256": _sha256_file(path),
            }
            for path in ([result_path] if result_path.is_file() else [])
            + [p for p, _, _ in segments]
            + (
                [trial / _DAYTONA_USAGE_FILENAME]
                if (trial / _DAYTONA_USAGE_FILENAME).is_file()
                else []
            )
        ],
        # Independent capture is additive and optional: None means no linked
        # capture exists, never a lookup failure worth failing the report over.
        "capture": _linked_capture(trial),
        # Environment monitor evidence is additive and optional: None means
        # the trial predates the monitor (or ran off-Daytona), never zero use.
        "daytona_usage": _daytona_usage(trial),
        # HAR-156: declared setup deviations, same file the process-job run
        # page reads; [] when the job carries no fingerprint.
        "setup_deviations": _setup_deviations(trial.parent),
        # HAR-92: per-step model/harness/execution/observation layers,
        # continuation coverage, and the outcome/execution split.
        "step_layers": layer_summary,
        "trajectory_coverage": coverage,
    }
    report["summary"] = _summary_line(report)
    return report


def is_trial_dir(path: Path) -> bool:
    """A Harbor trial directory holds a TrialResult (``result.json`` with ``trial_name``)."""
    try:
        data = json.loads((path / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "trial_name" in data


def trial_dirs(job_dir: str | Path) -> list[Path]:
    """Trial directories directly under a Harbor job directory, sorted by name."""
    job = Path(job_dir)
    return sorted(p for p in job.iterdir() if p.is_dir() and is_trial_dir(p))


def build_job_report(
    job_dir: str | Path, *, timeline_limit: int | None = DEFAULT_TIMELINE_LIMIT
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build every trial report under ``job_dir`` plus an ``evallab.job_report/v1`` rollup."""
    job = Path(job_dir).resolve()
    reports = [build_run_report(t, timeline_limit=timeline_limit) for t in trial_dirs(job)]
    rows = []
    for r in reports:
        rows.append(
            {
                "trial": r["identity"]["trial_name"],
                "task": r["identity"]["task"],
                "agent": r["identity"]["agent"],
                "model": r["identity"]["model"],
                "verdict": r["outcome"]["verdict"],
                "reward": r["outcome"]["reward"],
                "stop_reason": r["outcome"].get("stop_reason"),
                "stop_category": r["outcome"].get("stop_category"),
                "wall_seconds": r["timing"]["total_seconds"],
                "agent_seconds": r["timing"]["phases"]["agent_execution"]["seconds"],
                "steps": r["timeline"]["total_steps"],
                "tool_calls": r["tools"]["total_calls"],
                "tool_errors": r["errors"]["tool_errors"],
                "tokens": r["tokens"]["total"],
                "cost_usd": r["cost"]["total_usd"],
                "cost_source": r["cost"]["source"],
                "repeated_actions": r["revisits"]["repeated_actions"],
                "exact_revisits": r["revisits"]["exact_revisits"],
                "subagents": r["subagents"]["count"],
                "trajectory": r["availability"]["trajectory"],
            }
        )
    rewards = [row["reward"] for row in rows if row["reward"] is not None]
    # Price-table estimates are trial-level list-price figures, never metered
    # spend: they roll up separately and never enter the harness-cost sums.
    costs = [
        row["cost_usd"]
        for row in rows
        if row["cost_usd"] is not None and row["cost_source"] != "price_table_estimate"
    ]
    estimated = [
        row["cost_usd"]
        for row in rows
        if row["cost_usd"] is not None and row["cost_source"] == "price_table_estimate"
    ]
    walls = [row["wall_seconds"] for row in rows if row["wall_seconds"] is not None]
    agents = [row["agent_seconds"] for row in rows if row["agent_seconds"] is not None]
    passed = sum(1 for row in rows if row["verdict"] == "passed")
    scored = sum(1 for row in rows if row["verdict"] in {"passed", "failed", "partial"})
    job_report = {
        "schema": JOB_REPORT_SCHEMA,
        "job": job.name,
        "job_dir": str(job),
        "trials": len(rows),
        "scored": scored,
        "passed": passed,
        "pass_rate": round(passed / scored, 4) if scored else None,
        "mean_reward": round(sum(rewards) / len(rewards), 4) if rewards else None,
        "total_cost_usd": round(sum(costs), 6) if costs else None,
        "trials_without_cost": len(rows) - len(costs) - len(estimated),
        "trials_with_estimated_cost": len(estimated),
        "estimated_cost_usd": round(sum(estimated), 6) if estimated else None,
        "cost_per_pass_usd": round(sum(costs) / passed, 6) if costs and passed else None,
        "median_wall_seconds": round(median(walls), 3) if walls else None,
        "median_agent_seconds": round(median(agents), 3) if agents else None,
        "total_tokens": _sum(row["tokens"] for row in rows),
        "total_tool_calls": sum(row["tool_calls"] for row in rows),
        "total_repeated_actions": sum(row["repeated_actions"] for row in rows),
        "total_subagents": sum(row["subagents"] for row in rows),
        "verdicts": dict(Counter(row["verdict"] for row in rows)),
        # HAR-156: limit-hit share over all trials; setup_limited is strictly
        # above a 5% our_limit share. Counts/verdict semantics untouched.
        "limit_hit_summary": limit_hit_summary(row["stop_reason"] for row in rows),
        "rows": rows,
    }
    for report in reports:
        report["job_limit_hit_summary"] = dict(job_report["limit_hit_summary"])
    return job_report, reports


def _fmt_identical_run(run: dict[str, Any]) -> str:
    """Longest identical run with its bounds, so a cut step list stays locatable."""
    start, end = run.get("start_step"), run.get("end_step")
    if start is None or end is None:
        return f"{run['length']} (no steps)"
    return f"{run['length']} (steps {start}\u2013{end})"


def _fmt_cycle(cycle: dict[str, Any] | None) -> str:
    """Longest command cycle, or ``none`` when no block repeats 3 times."""
    if cycle is None:
        return "none"
    commands = ", ".join(f"`{command}`" for command in cycle["commands"])
    return (
        f"period {cycle['period']} \u00d7 {cycle['repeats']} repeats "
        f"(steps {cycle['start_step']}\u2013{cycle['end_step']}): {commands}"
    )


def _fmt_revisit_onset(onset: dict[str, Any] | None) -> str:
    """One-line rendering of the ``revisits_started`` block."""
    if not onset:
        return "n/a"
    if onset["status"] == "onset":
        return (
            f"window {onset['window'] + 1} (steps {onset['steps'][0]}–{onset['steps'][1]}): "
            f"repeat rate {_fmt_pct(onset['repeat_rate'])} vs run median "
            f"{_fmt_pct(onset['median_repeat_rate'])}"
        )
    labels = {
        "no_repeats": "no repeated actions in the run",
        "flat": "no window exceeds the run-median repeat rate",
        "unavailable": onset.get("reason") or "no window carries actions",
    }
    return labels.get(str(onset["status"]), str(onset["status"]))


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


def _fmt_seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value < 60:
        return f"{value:.1f}s"
    minutes, seconds = divmod(int(round(value)), 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _fmt_tokens(value: int | float | None) -> str:
    if value is None:
        return "n/a"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 10_000:
        return f"{value / 1000:.1f}k"
    return f"{int(value):,}"


def _fmt_usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _cell(text: Any) -> str:
    return _fmt(text).replace("|", "\\|").replace("\n", " ")


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines.extend("| " + " | ".join(_cell(c) for c in row) + " |" for row in rows)
    return lines


def render_run_report_markdown(report: dict[str, Any]) -> str:
    """Render an ``evallab.run_report/v1`` dictionary as Markdown."""
    from evallab.process_job import _job_limit_hit_lines, _setup_deviation_lines

    identity = report["identity"]
    outcome = report["outcome"]
    timing = report["timing"]
    tokens = report["tokens"]
    cost = report["cost"]
    tools = report["tools"]
    revisits = report["revisits"]
    subagents = report["subagents"]
    context = report["context"]
    errors = report["errors"]
    timeline = report["timeline"]
    lines = [
        f"# Run report: {identity['trial_name']}",
        "",
        f"> {report['summary']}",
        "",
        "## Identity",
        *_table(
            ["Field", "Value"],
            [
                ["Task", identity["task"]],
                ["Agent", f"{identity['agent']} {identity['agent_version'] or ''}".strip()],
                ["Model", identity["model"]],
                ["Job", identity["job"]],
                [
                    "Trajectory",
                    f"{report['availability']['trajectory']} ({identity['atif_schema'] or 'no schema'})",
                ],
                ["Trial dir", f"`{identity['trial_dir']}`"],
            ],
        ),
        "",
        "## Outcome",
        f"- Verdict: **{outcome['verdict'].replace('_', ' ')}**"
        + (f" — reward {outcome['reward']:g}" if outcome["reward"] is not None else ""),
    ]
    if len(outcome["rewards"]) > 1:
        lines.append(
            "- Rewards: " + ", ".join(f"{k}={v:g}" for k, v in sorted(outcome["rewards"].items()))
        )
    if errors["exception"]:
        exc = errors["exception"]
        lines.append(f"- Exception: `{exc['type']}` — {exc['message'] or ''}".rstrip(" —"))
    lines.append(
        f"- Verifier: {outcome.get('verifier_outcome', 'unknown')}"
        f"; stop reason: {outcome.get('stop_reason') or 'unknown'}"
        + (f" ({outcome['stop_detail']})" if outcome.get("stop_detail") else "")
    )
    # HAR-156: execution lens, separate from the verdict line above. A scored
    # timeout still passes or fails on its reward; the category says whose
    # limit (if any) stopped the run.
    lines.append(
        f"- Stop category: `{outcome.get('stop_category') or 'unknown'}`"
        " (how the run ended; the verdict above is the verifier reading)."
    )
    lines += _job_limit_hit_lines(report)
    if outcome.get("completion"):
        lines.append(f"- {outcome['completion']}")
    problems = outcome.get("execution_problems") or {}
    problem_bits = [
        f"{problems[key]} {label}"
        for key, label in (
            ("parse_errors", "parse errors"),
            ("prose_completions", "prose completions"),
            ("http_400_no_usage", "HTTP 400s without usage"),
            ("proxy_unresolved_requests", "unresolved proxy requests"),
        )
        if isinstance(problems.get(key), int)
    ]
    if problems.get("proxy_usage_unreconciled") is True:
        problem_bits.append("proxy usage failed reconciliation")
    if problem_bits:
        problems_text = ", ".join(problem_bits)
    elif problems.get("coverage") != "complete":
        problems_text = "unknown"
    else:
        problems_text = "none recorded"
    lines.append(
        "- Execution problems: "
        + problems_text
        + (f" ({problems['coverage']})" if problems.get("coverage") != "complete" else "")
        + "."
    )
    if outcome["final_agent_message"]:
        lines.append(f"- Final agent message: {outcome['final_agent_message']}")
    lines.append(render_outside_fetch_line(report.get("outside_fetches") or {"items": []}))
    first_failure = outcome.get("first_failure") or {}
    if first_failure.get("step") is not None:
        lines.append(
            f"- First failure: step {first_failure['step']} ({first_failure['kind']}, "
            f"{first_failure['confidence']} confidence): {first_failure['evidence']}"
        )
    elif first_failure.get("evidence"):
        lines.append(f"- First failure: none found ({first_failure['evidence']}).")
    else:
        lines.append("- First failure: none found.")
    copied = report.get("pass_may_be_copied")
    if copied:
        evidence = ", ".join(
            f"fetched at step {item['fetch_step']}, read back at step {item['read_back_step']}"
            for item in copied.get("evidence_steps") or []
        )
        lines.append(f"- Flag: pass_may_be_copied ({evidence or 'no step detail'})")
    lines += ["", "## Time"]
    phases = timing["phases"]
    lines += _table(
        ["Phase", "Duration", "Starts at"],
        [
            [
                name.replace("_", " "),
                _fmt_seconds(phases[name]["seconds"]),
                _fmt_seconds(phases[name]["starts_at_offset_seconds"]),
            ]
            for name in _HARBOR_PHASES
        ]
        + [["**total wall**", _fmt_seconds(timing["total_seconds"]), "0.0s"]],
    )
    gaps = timing["step_gap_seconds"]
    if timing["first_agent_step_offset_seconds"] is not None:
        lines += [
            "",
            f"First agent step {_fmt_seconds(timing['first_agent_step_offset_seconds'])} after "
            f"`{timing['offset_origin']}` (offsets below use the same origin).",
        ]
    if gaps["count"]:
        lines += [
            "",
            f"Time between consecutive agent steps (model turn + previous tool time): median "
            f"{_fmt_seconds(gaps['median'])}, p90 {_fmt_seconds(gaps['p90'])}, max "
            f"{_fmt_seconds(gaps['max'])} over {gaps['count']} gaps.",
        ]
        if timing["slowest_steps"]:
            lines += ["", "Slowest steps:"]
            lines += [
                f"- step {s['step']}: {_fmt_seconds(s['seconds'])} — {s['action'] or 'no tool call'}"
                for s in timing["slowest_steps"]
            ]
    lines += ["", "## Tokens and cost"]
    src = tokens["sources"]
    lines += _table(
        ["Measure", "Value", "Source"],
        [
            ["Input (incl. cached)", _fmt_tokens(tokens["input"]), src.get("input")],
            ["  cached", _fmt_tokens(tokens["cached_input"]), src.get("cached_input")],
            ["  uncached", _fmt_tokens(tokens["uncached_input"]), "input − cached"],
            ["Output", _fmt_tokens(tokens["output"]), src.get("output")],
            ["  reasoning", _fmt_tokens(tokens["reasoning"]), src.get("reasoning")],
            ["Cache writes", _fmt_tokens(tokens["cache_write"]), "step_sum"],
            ["**Total tokens**", _fmt_tokens(tokens["total"]), "input + output"],
            [
                "**Cost**",
                _fmt_usd(cost["total_usd"])
                + (" (lower bound)" if cost["is_lower_bound"] else "")
                + (" (estimate)" if cost["source"] == "price_table_estimate" else ""),
                (cost["source"] or "unavailable")
                + (f" / {cost['harness_cost_source']}" if cost["harness_cost_source"] else ""),
            ],
        ],
    )
    if cost["source"] == "price_table_estimate" and cost["price_table"]:
        table = cost["price_table"]
        rates = table["usd_per_mtok"]
        lines += [
            "",
            f"Cost is an estimate from pinned list prices for model {table['model']!r} "
            f"(matched {table['matched_key']!r}): ${rates['input']}/M input, "
            f"${rates['cached_input']}/M cached input, ${rates['output']}/M output. "
            f"Source: {table['source_url']} (retrieved {table['retrieved_on']}). "
            "Not a metered charge.",
        ]
    ctx = tokens["context"]
    lines += [
        "",
        f"Usage recorded on {tokens['llm_steps_with_usage']} of {tokens['llm_steps']} agent steps. "
        f"Context: first prompt {_fmt_tokens(ctx['first_prompt_tokens'])}, peak "
        f"{_fmt_tokens(ctx['peak_prompt_tokens'])} (step {_fmt(ctx['peak_step'])}), last "
        f"{_fmt_tokens(ctx['last_prompt_tokens'])}. {cost['note']}",
    ]
    if tokens["top_steps"]:
        lines += ["", "Most expensive steps:"]
        lines += _table(
            ["Step", "Input", "Output", "Cost", "Action"],
            [
                [
                    s["step"],
                    _fmt_tokens(s["input"]),
                    _fmt_tokens(s["output"]),
                    _fmt_usd(s["cost_usd"]),
                    s["action"],
                ]
                for s in tokens["top_steps"]
            ],
        )
    lines += ["", "## Tools"]
    if tools["total_calls"]:
        lines.append(
            f"{_plural(tools['total_calls'], 'call')} across {_plural(tools['distinct_tools'], 'tool')}"
            + (
                f" in {_plural(tools['actions'], 'action')} (calls that share one observation "
                "count as one action; revisits, errors, and polls count actions)"
                if tools["actions"] != tools["total_calls"]
                else ""
            )
            + (f"; {tools['polls']} polls" if tools["polls"] else "")
            + (
                "; wrapped: " + ", ".join(f"{k}×{v}" for k, v in tools["wrapped_calls"].items())
                if tools["wrapped_calls"]
                else ""
            )
            + "."
        )
        lines += _table(
            [
                "Tool",
                "Calls",
                "OK",
                "Errors",
                "Unknown",
                "Errors / judged",
                "Output chars",
                "Steps",
            ],
            [
                [
                    r["tool"],
                    r["calls"],
                    r["ok"],
                    r["errors"],
                    r["unknown"],
                    f"{_fmt_pct(r['error_rate'])} of {r['judged']}" if r["judged"] else "n/a",
                    f"{r['output_chars']:,}",
                    f"{r['first_step']}–{r['last_step']}",
                ]
                for r in tools["by_tool"]
            ],
        )
        if tools["top_programs"]:
            lines += [
                "",
                "Shell programs: "
                + ", ".join(f"`{p['program']}`×{p['calls']}" for p in tools["top_programs"]),
            ]
    else:
        lines.append("No tool calls recorded.")
    step_layers = report.get("step_layers") or {}
    provenance_bits = [
        f"{step_layers[key]} {label}"
        for key, label in (
            ("recorded", "recorded"),
            ("reconstructed", "reconstructed"),
            ("copied", "copied context"),
            ("missing", "missing"),
        )
        if isinstance(step_layers.get(key), int) and step_layers[key]
    ]
    if provenance_bits and (tools["total_calls"] or step_layers.get("agent_steps")):
        lines.append(
            "Call provenance: "
            + ", ".join(provenance_bits)
            + ". Missing coverage reads as unknown, never zero."
        )
    coverage = report.get("trajectory_coverage") or {}
    lines += ["", "## Capture (what was recorded?)"]
    lines.append(
        f"Head: {'present' if coverage.get('trajectory_head') else 'absent'}; "
        f"continuations: {coverage.get('continuation_indices', 'unknown')}"
        + (
            f" (missing: {coverage['continuations_missing']})"
            if coverage.get("continuations_missing")
            else ""
        )
        + "."
    )
    part_bits = "; ".join(
        f"{part['path']}: "
        f"{part['steps'] if part['readable'] else 'unreadable'} steps"
        + (f" ({part['copied_steps']} copied)" if part.get("copied_steps") else "")
        for part in coverage.get("parts", [])
    )
    lines.append(
        f"Trajectory parts: {part_bits or 'none'}. "
        f"Unique non-copied steps: {coverage.get('unique_steps', 'unknown')}"
        + (
            f" ({coverage['duplicated_steps']} shared across parts counted once)"
            if coverage.get("duplicated_steps")
            else ""
        )
        + "."
    )
    for name, target in sorted((coverage.get("duplicate_segments") or {}).items()):
        lines.append(f"- {name} repeats an earlier segment ({target})")
    if coverage.get("gaps"):
        lines += [f"- coverage gap: {gap}" for gap in coverage["gaps"]]
    if coverage.get("notes"):
        lines += [f"- {note}" for note in coverage["notes"]]
    lines += ["", "## Revisits (did it circle back?)"]
    if revisits["actions_considered"]:
        lines += _table(
            ["Measure", "Value"],
            [
                ["Actions considered (polls excluded)", revisits["actions_considered"]],
                ["Distinct actions", revisits["distinct_actions"]],
                [
                    "Repeated actions",
                    f"{revisits['repeated_actions']} ({_fmt_pct(revisits['repeat_rate'])} of actions)",
                ],
                ["  returned to an earlier action", revisits["returns_to_earlier_action"]],
                ["  immediate repeats", revisits["consecutive_repeats"]],
                ["**Exact revisits** (same action, same result)", revisits["exact_revisits"]],
                [
                    "Same result from a different action",
                    revisits["same_result_from_different_action"],
                ],
                ["Repeated identical errors", revisits["repeated_errors"]],
                ["Longest identical run", _fmt_identical_run(revisits["longest_identical_run"])],
                ["Longest command cycle", _fmt_cycle(revisits.get("longest_cycle"))],
            ]
            + (
                [["Revisit onset", _fmt_revisit_onset(revisits.get("revisits_started"))]]
                if revisits.get("revisits_started")
                else []
            )
            + (
                [
                    [
                        "Loop suspicion",
                        f"{'detected' if revisits['loop_suspicion']['detected'] else 'not detected'} "
                        f"(score {revisits['loop_suspicion']['score']:.2f}; {', '.join(revisits['loop_suspicion']['reasons']) or 'no reasons'})",
                    ]
                ]
                if revisits["loop_suspicion"]
                else []
            ),
        )
        if revisits["most_repeated"]:
            lines += ["", "Most repeated actions:"]
            lines += [
                f"- {g['count']}× `{g['tool']}` {g['target']} — steps {g['steps']}, "
                f"{g['identical_results']} with identical results"
                for g in revisits["most_repeated"]
            ]
        if any(revisits.get("repeats_by_window") or ()):
            lines += ["", f"Repeats by tenth of the run: {revisits['repeats_by_window']}"]
        onset = revisits.get("revisits_started") or {}
        if onset.get("repeat_rate_by_window") is not None:
            rates = ", ".join(
                _fmt_pct(rate) if rate is not None else "—"
                for rate in onset["repeat_rate_by_window"]
            )
            lines += [
                "",
                f"Repeat rate by tenth of the run (median {_fmt_pct(onset['median_repeat_rate'])}): {rates}",
            ]
    else:
        lines.append("No actions to compare.")
    lines += ["", "## Subagents"]
    observability_texts = {
        "captured": "Subagent activity captured in the trajectory.",
        "delegations_only": "Delegation calls observed, but the harness did not record the child trajectories.",
        "none_observed": "No subagents or delegation calls observed. (Harbor's codex converter drops subagent threads; claude-code records them as sidechain steps; terminus-2 records summarization subagents.)",
    }
    if subagents["observability"] == "unavailable":
        lines.append(
            f"Subagent activity unavailable: {subagents.get('reason') or 'native rollouts could not be read'}."
        )
    else:
        lines.append(observability_texts[subagents["observability"]])
    rollouts = subagents.get("codex_rollouts")
    if rollouts is not None:
        detail = f"Native codex rollouts: {rollouts['status']}"
        if rollouts.get("reason"):
            detail += f" ({rollouts['reason']})"
        if rollouts.get("parent"):
            detail += f"; parent {rollouts['parent']}"
        detail += f"; {len(rollouts.get('files') or [])} file(s) read."
        lines.append(detail)
        others = rollouts.get("other_threads") or []
        if others:
            summaries = ", ".join(
                f"{o.get('thread_id')} ({o.get('source') or 'unknown source'})" for o in others
            )
            lines.append(f"Other native threads (not delegated spend): {summaries}.")
    if subagents["items"]:
        lines += _table(
            [
                "Id",
                "Kind",
                "Spawned (step / offset)",
                "Duration",
                "Steps",
                "Tool calls",
                "Tokens in/out",
                "Cost",
                "Evidence",
            ],
            [
                [
                    i["id"],
                    i["kind"],
                    f"{_fmt(i['spawned_at_step'])} / {_fmt_seconds(i['spawned_at_offset_seconds'])}",
                    _fmt_seconds(i.get("duration_seconds")),
                    _fmt(i.get("steps")),
                    _fmt(i.get("tool_calls")),
                    f"{_fmt_tokens(i.get('input_tokens'))}/{_fmt_tokens(i.get('output_tokens'))}",
                    _fmt_usd(i.get("cost_usd")),
                    i["evidence"],
                ]
                for i in subagents["items"]
            ],
        )
    if subagents["delegation_calls"]:
        lines += ["", "Delegation calls:"]
        lines += [
            f"- step {d['step']} (+{_fmt_seconds(d['offset_seconds'])}): `{d['tool']}` {d['target']}"
            for d in subagents["delegation_calls"]
        ]
    lines += ["", "## Context management"]
    lines.append(
        f"Segments: {len(context['segments'])}; copied-context steps excluded: "
        f"{context['copied_context_steps_excluded']}."
    )
    shown_events = context["events"][:MAX_CONTEXT_EVENT_LINES]
    lines += [f"- step {e['step']}: {e['kind']} — {e['detail']}" for e in shown_events]
    hidden = len(context["events"]) - len(shown_events)
    if hidden > 0:
        lines.append(f"- … {hidden} more events (see the JSON report)")
    lines += ["", "## Errors"]
    unknown_detail = [
        f"{errors[key]} {label}"
        for key, label in (
            ("calls_without_result", "without a recorded result"),
            ("not_executed_calls", "intercepted by the harness (not executed)"),
        )
        if errors[key]
    ]
    lines.append(
        f"{errors['tool_errors']} tool errors ({errors['harness_signalled']} signalled by the harness, "
        f"{errors['inferred_from_output']} inferred from output text); "
        f"{errors['status_unknown_calls']} calls with no status signal"
        + (f" ({', '.join(unknown_detail)})" if unknown_detail else "")
        + "."
        + (
            f" {errors['expected_probe_misses']} expected probe misses (for example grep finding "
            "nothing) are counted as ok."
            if errors["expected_probe_misses"]
            else ""
        )
    )
    first_error = errors.get("first_error")
    if first_error is not None:
        if first_error.get("evidence") == "output_text":
            how = "inferred from output text"
        else:
            channel = first_error.get("evidence") or "no channel recorded"
            how = f"signalled by the harness ({channel})"
        lines.append(f"- First tool error: step {first_error['step']} ({how}).")
    if errors["by_category"]:
        lines.append(
            "By category: " + ", ".join(f"{k}×{v}" for k, v in errors["by_category"].items())
        )
    lines += [
        f"- step {e['step']} `{e['tool']}` {e['target']} [{e['category']}]: {e['excerpt'] or ''}"
        for e in errors["examples"]
    ]
    capture = report.get("capture")
    if capture is not None:
        lines += ["", "## Independent capture"]
        lines.append(
            f"Verdict: **{str(capture.get('verdict', 'unknown')).replace('_', ' ')}** — "
            f"{capture.get('captured_calls', 0)} captured calls "
            f"({capture.get('captured_assistant_turns', 0)} assistant turns) vs "
            f"{capture.get('atif_agent_steps', 0)} ATIF agent steps."
        )
        divergence = capture.get("first_divergence")
        if isinstance(divergence, dict) and divergence:
            if divergence.get("kind") == "missing_turn":
                lines.append(
                    f"First divergence: captured turn {divergence.get('turn_index')} absent "
                    f"from the ATIF: {divergence.get('excerpt') or ''}"
                )
            else:
                lines.append(f"First divergence: {divergence.get('kind')}.")
        elif capture.get("verdict") == "complete":
            lines.append("Captured turns agree with the harness trajectory.")
    domain_lines = render_domain_markdown(report.get("domain"))
    if domain_lines:
        lines += [""] + domain_lines
    lines += render_daytona_usage_lines(report.get("daytona_usage"))
    # HAR-156: declared setup deviations, same wording as the process-job run
    # page; absent evidence renders nothing.
    lines += _setup_deviation_lines(report.get("setup_deviations"))
    lines += ["", "## Timeline"]
    if timeline["windows"]:
        lines.append("By tenth of the run:")
        lines += _table(
            ["Steps", "Calls", "Err", "Revisits", "Out tok", "Peak in", "Comp", "Cost", "Span"],
            [
                [
                    f"{w['steps'][0]}–{w['steps'][1]}",
                    w["tool_calls"],
                    w["errors"],
                    w["revisits"],
                    _fmt_tokens(w["output_tokens"]),
                    _fmt_tokens(w.get("peak_prompt_tokens")),
                    w.get("compactions"),
                    _fmt_usd(w["cost_usd"]),
                    _fmt_seconds(w["seconds"]),
                ]
                for w in timeline["windows"]
            ],
        )
        lines.append("")
    time_section = timeline.get("time_windows") or {}
    if time_section.get("windows"):
        lines.append(
            f"By wall clock (equal-duration windows over {_fmt_seconds(time_section['span_seconds'])}"
            + (
                f"; {_plural(time_section['steps_without_timestamps'], 'step')} without timestamps excluded"
                if time_section.get("steps_without_timestamps")
                else ""
            )
            + "):"
        )
        lines += _table(
            ["At", "Steps", "Calls", "Err", "Revisits", "Out tok", "Peak in", "Comp"],
            [
                [
                    f"+{_fmt_seconds(w['starts_at_offset_seconds'])}–"
                    f"{_fmt_seconds(w['ends_at_offset_seconds'])}",
                    f"{w['steps'][0]}–{w['steps'][1]}",
                    w["tool_calls"],
                    w["errors"],
                    w["revisits"],
                    _fmt_tokens(w["output_tokens"]),
                    _fmt_tokens(w.get("peak_prompt_tokens")),
                    w.get("compactions"),
                ]
                for w in time_section["windows"]
            ],
        )
        lines.append("")
    elif time_section.get("status") == "unavailable":
        lines.append(f"Time windows unavailable: {time_section['reason']}.")
        lines.append("")
    lines.append(f"Showing {timeline['shown_steps']} of {timeline['total_steps']} steps.")
    rows = []
    for e in timeline["entries"]:
        if "omitted_steps" in e:
            rows.append(
                [f"… {e['omitted_steps'][0]}–{e['omitted_steps'][1]}", "", "", "", "", "", ""]
            )
            continue
        rows.append(
            [
                e["step"],
                _fmt_seconds(e["offset_seconds"]),
                e["source"],
                e["action"] or e["message"] or "",
                e["status"] or "",
                "—"
                if e["input_tokens"] is None and e["output_tokens"] is None
                else f"{_fmt_tokens(e['input_tokens'])}/{_fmt_tokens(e['output_tokens'])}",
                ", ".join(e["flags"]),
            ]
        )
    lines += _table(
        ["Step", "At", "Source", "Action / message", "Status", "Tokens in/out", "Flags"], rows
    )
    lines += ["", "## Data quality"]
    lines += [f"- {q}" for q in report["data_quality"]] or ["- No gaps detected."]
    lines += ["", "## Sources"]
    lines += [f"- `{s['path']}` sha256:{s['sha256'][:16]}…" for s in report["sources"]]
    return "\n".join(lines) + "\n"


def render_job_report_markdown(job_report: dict[str, Any]) -> str:
    """Render an ``evallab.job_report/v1`` dictionary as Markdown."""
    lines = [
        f"# Job report: {job_report['job']}",
        "",
        f"Trials: {job_report['trials']}; scored: {job_report['scored']}; passed: {job_report['passed']} "
        f"(pass rate {_fmt_pct(job_report['pass_rate'])}, mean reward {_fmt(job_report['mean_reward'])}). "
        f"Total cost {_fmt_usd(job_report['total_cost_usd'])}"
        + (
            f" ({job_report['trials_without_cost']} trials without cost)"
            if job_report["trials_without_cost"]
            else ""
        )
        + f"; cost per pass {_fmt_usd(job_report['cost_per_pass_usd'])}. "
        + (
            f"Price-table estimates {_fmt_usd(job_report['estimated_cost_usd'])} "
            f"across {job_report['trials_with_estimated_cost']} trials (not metered). "
            if job_report.get("trials_with_estimated_cost")
            else ""
        )
        + f"Median wall {_fmt_seconds(job_report['median_wall_seconds'])}, median agent time "
        f"{_fmt_seconds(job_report['median_agent_seconds'])}. Total tokens {_fmt_tokens(job_report['total_tokens'])}, "
        f"tool calls {job_report['total_tool_calls']}, repeated actions {job_report['total_repeated_actions']}, "
        f"subagents {job_report['total_subagents']}.",
        "",
    ]
    # HAR-156: limit-hit share over all trials with the >5% setup-limited flag.
    # Tolerant of older payloads without the rollup (renders n/a, never crashes).
    limit_hit = job_report.get("limit_hit_summary") or {}
    share = limit_hit.get("limit_hit_share")
    flagged = bool(limit_hit.get("setup_limited"))
    lines.append(
        f"Limit hits: {limit_hit.get('limit_hit_trials', 0)}/{limit_hit.get('trials', 0)} trials "
        f"(share {_fmt_pct(share)}); setup-limited: `{str(flagged).lower()}`"
        + (
            " — flagged: over 5% of trials hit our limits, so treat the job as setup-limited."
            if flagged
            else "."
        )
    )
    lines.append("")
    lines += _table(
        [
            "Trial",
            "Verdict",
            "Stop reason",
            "Stop category",
            "Reward",
            "Wall",
            "Agent",
            "Steps",
            "Tools",
            "Errors",
            "Tokens",
            "Cost",
            "Repeats (exact)",
            "Subagents",
        ],
        [
            [
                r["trial"],
                r["verdict"],
                r.get("stop_reason"),
                r.get("stop_category"),
                r["reward"],
                _fmt_seconds(r["wall_seconds"]),
                _fmt_seconds(r["agent_seconds"]),
                r["steps"],
                r["tool_calls"],
                r["tool_errors"],
                _fmt_tokens(r["tokens"]),
                _fmt_usd(r["cost_usd"])
                + (" (est)" if r.get("cost_source") == "price_table_estimate" else ""),
                f"{r['repeated_actions']} ({r['exact_revisits']})",
                r["subagents"],
            ]
            for r in job_report["rows"]
        ],
    )
    return "\n".join(lines) + "\n"


_UNSAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _file_stem(name: str) -> str:
    """A filename stem that cannot leave the output directory."""
    stem = _UNSAFE_STEM_RE.sub("_", name).lstrip(".")[:120]
    return stem or "trial"


def write_reports(
    reports: Sequence[dict[str, Any]],
    output_dir: Path,
    job_report: dict[str, Any] | None = None,
) -> list[Path]:
    """Write JSON and Markdown files for each report (and the job rollup)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for report in reports:
        stem = _file_stem(Path(str(report["identity"]["trial_dir"])).name)
        json_path = output_dir / f"{stem}.run_report.json"
        md_path = output_dir / f"{stem}.run_report.md"
        json_path.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        md_path.write_text(render_run_report_markdown(report), encoding="utf-8")
        written += [json_path, md_path]
    if job_report is not None:
        json_path = output_dir / "job.run_report.json"
        md_path = output_dir / "job.run_report.md"
        json_path.write_text(json.dumps(job_report, indent=2) + "\n", encoding="utf-8")
        md_path.write_text(render_job_report_markdown(job_report), encoding="utf-8")
        written += [json_path, md_path]
    return written
