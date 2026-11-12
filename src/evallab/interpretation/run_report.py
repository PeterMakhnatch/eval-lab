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

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, median
from typing import TYPE_CHECKING, Any, Literal, Protocol

from evallab.traj import (
    TrajectoryOutline,
    _compute_cbv_slope,
    _compute_exit_code_cascade,
    extract_features,
    resolve_trial_target,
)

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


# Hosts whose content is upstream source or release artifacts.

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


DEFAULT_TIMELINE_LIMIT = 60
TARGET_CHARS = 160

# Argument keys that carry the command text, compared case-insensitively.
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
_COMPACTION_MIN_PROMPT = 8_000


# --------------------------------------------------------------------------- #
# Normalized views
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


_WRAPPER_PROGRAMS = frozenset({"sudo", "doas", "env", "nice", "nohup", "time", "timeout", "stdbuf"})


_PROGRAM_NAME_RE = re.compile(r"^[A-Za-z_][\w.+-]*$")


# --------------------------------------------------------------------------- #
# Calls, outputs, and status
# --------------------------------------------------------------------------- #


_PAYLOAD_OK = frozenset({"ok", "success", "succeeded", "completed"})
_PAYLOAD_ERROR = frozenset({"error", "failed", "failure"})


_DAYTONA_DIMENSIONS = ("cpu", "memory_gib", "disk_gib", "gpu", "concurrent_sandboxes")
_DEFAULT_DAYTONA_SAFETY_FRACTION = 0.8


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


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


# A command cycle is a block of 2-3 distinct normalized commands repeated
# back-to-back at least 3 full times. Signatures reuse the identical-run
# normalization (tool plus whitespace-collapsed command). Only shell actions
# with substantive text count: completion claims such as
# ``mark_task_complete: {}`` interleave with real commands but are not
# commands cycling.
_MISSING_DEPENDENCY_RE = re.compile(r"ModuleNotFoundError|No module named [\w.]+")


# A shell command that only echoes the completion token (the model retrying
# "task_complete" in prose after the harness demanded the real call).
_COMPLETION_ECHO_RE = re.compile(r"""['"]?echo\s+['"]?task_complete['"]?['"]?""")


_COMPLETION_STOPS = frozenset({"task_complete", "prose_completion"})


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


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


_UNSAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")


