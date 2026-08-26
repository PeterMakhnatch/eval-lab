"""EvidencePack v1: Hierarchical, bounded, citation-preserving input for interpreting models.

Key invariants:
- Models consume bounded JSON EvidencePacks (never raw Parquet or entire uncompressed directories).
- Hierarchical compression: global outline -> episode index -> selected raw evidence windows.
- Mandatory-window budget overflow or quarantine produces uncallable pack (abstain/tier required).
- Every included fact and omitted range carries an exact canonical CitationHandle reopening coordinate.
- Deterministic token budgeting and pack content hashing (pack_digest).
- Redaction changes mint a new pack digest; raw source digest remains separate.
- Fine-grained category coverage metrics (raw_source, events, episodes, errors, state, verifier, linkage).
- Lossless on-demand reopening of omitted ranges via reopen_omitted_range().
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.trajectory_hydration import (
    CitationHandle,
    RedactionPolicy,
    create_citation_handle,
    hydrate_citation,
)
from evallab.trajectory_ir import (
    IREvent,
    TrajectoryIR,
)

DEFAULT_TOKEN_BUDGET = 16000  # ~64,000 characters


def _est_tokens(text: str) -> int:
    """Rough character-to-token estimator (4 chars/token)."""
    return max(1, len(text) // 4)


def _sha256_canonical_json(data: Any) -> str:
    serialized = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


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
    recovery_episodes_count: int
    terminal_episodes_count: int

    # 4. Error Coverage
    total_errors: int
    unhandled_exceptions_count: int
    tool_errors_count: int
    exit_code_cascades_max: int
    recovered_errors_count: int
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
            "recovery_episodes_count": self.recovery_episodes_count,
            "terminal_episodes_count": self.terminal_episodes_count,
            "total_errors": self.total_errors,
            "unhandled_exceptions_count": self.unhandled_exceptions_count,
            "tool_errors_count": self.tool_errors_count,
            "exit_code_cascades_max": self.exit_code_cascades_max,
            "recovered_errors_count": self.recovered_errors_count,
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
    ir: TrajectoryIR,
    trial_dir: Path | None = None,
) -> EvidenceCoverageMetrics:
    """Compute deterministic, category-wise evidence coverage metrics."""
    t_dir = trial_dir if (trial_dir and trial_dir.is_dir()) else None
    has_atif = ir.status == "featured"
    if t_dir:
        has_result = (t_dir / "result.json").is_file()
        has_state_journal = (t_dir / "state-journal" / "state-diff.json").is_file() or (t_dir / "state-diff.json").is_file()
        has_ctrf = (t_dir / "verifier" / "ctrf.json").is_file() or (t_dir / "verifier" / "reward.json").is_file()
    else:
        has_result = bool(ir.source_digests.get("result_sha256") or ir.primary_reward is not None)
        has_state_journal = any(e.state_before_digest or e.state_after_digest for e in ir.events)
        has_ctrf = any(e.event_type == "verifier_check" for e in ir.events)

    has_cas = bool(ir.is_production_cas or ir.source_digests.get("cas_uri"))

    user_msgs = sum(1 for e in ir.events if e.event_type == "user_message" or e.actor == "user")
    agent_msgs = sum(1 for e in ir.events if e.event_type == "agent_message" or (e.actor == "agent" and not e.status_owning_program))
    tool_calls = sum(1 for e in ir.events if e.event_type == "tool_call" or e.call_index is not None)
    observations = sum(1 for e in ir.events if e.event_type == "observation")
    state_changes = sum(1 for e in ir.events if e.event_type == "state_change")
    verifier_checks = sum(1 for e in ir.events if e.event_type == "verifier_check" or e.phase == "verifier")
    context_mgmt = sum(1 for e in ir.events if e.event_type == "context_management" or e.action_family == "context_control")

    setup_eps = sum(1 for ep in ir.episodes if ep.episode_type == "setup")
    inst_eps = sum(1 for ep in ir.episodes if ep.episode_type == "instruction")
    insp_eps = sum(1 for ep in ir.episodes if ep.episode_type == "inspection")
    mut_eps = sum(1 for ep in ir.episodes if ep.episode_type == "mutation")
    ver_eps = sum(1 for ep in ir.episodes if ep.episode_type == "verification")
    rec_eps = sum(1 for ep in ir.episodes if ep.episode_type == "screening_recovery")
    term_eps = sum(1 for ep in ir.episodes if ep.episode_type == "terminal")

    total_errs = sum(1 for e in ir.events if e.is_error)
    tool_errs = sum(1 for e in ir.events if e.is_error and e.event_type == "tool_call")
    unhandled_exc = 1 if ir.exception_class else 0
    rec_errs = ir.baseline_metrics.recovery_count
    unrec_errs = max(0, total_errs - rec_errs)
    max_cascade = ir.baseline_metrics.max_exit_code_cascade_screening

    state_mutations = sum(1 for e in ir.events if e.action_family in ("file_edit", "file_write"))
    state_diff_obs = bool(has_state_journal)
    certified_pass = ir.primary_reward is not None and ir.primary_reward >= 1.0
    state_linked = any(e.state_before_digest or e.state_after_digest for e in ir.events) or has_state_journal

    verifier_exec = verifier_checks > 0 or has_ctrf
    verifier_reward_obs = ir.primary_reward is not None
    verifier_tests = 1 if verifier_exec else 0
    verifier_passed = 1 if (ir.primary_reward and ir.primary_reward >= 1.0) else 0
    unsupported_claims = 1 if (ir.final_verdict == "PASS" and not verifier_exec and not verifier_reward_obs) else 0

    hold_reasons: list[str] = []
    if not has_atif and not ir.is_production_cas:
        hold_reasons.append("missing_atif_evidence")
    if ir.unpaired_tool_calls_count > 0:
        hold_reasons.append("degraded_tool_linkage")
    if ir.quality_status in ("fail", "quarantined"):
        hold_reasons.append(f"quarantine_quality_status_{ir.quality_status}")
    if len(ir.events) == 0 and ir.status != "accounted_unavailable":
        hold_reasons.append("empty_event_sequence")
    if unsupported_claims > 0:
        hold_reasons.append("unsupported_terminal_claim")

    analysis_ready = len(hold_reasons) == 0

    return EvidenceCoverageMetrics(
        has_atif=has_atif,
        has_result=has_result,
        has_state_journal=has_state_journal,
        has_ctrf_verifier=has_ctrf,
        has_cas_archive=has_cas,
        is_production_cas=ir.is_production_cas,
        total_events=len(ir.events),
        user_messages_count=user_msgs,
        agent_messages_count=agent_msgs,
        tool_calls_count=tool_calls,
        observations_count=observations,
        state_changes_count=state_changes,
        verifier_checks_count=verifier_checks,
        context_management_count=context_mgmt,
        total_episodes=len(ir.episodes),
        setup_episodes_count=setup_eps,
        instruction_episodes_count=inst_eps,
        inspection_episodes_count=insp_eps,
        mutation_episodes_count=mut_eps,
        verification_episodes_count=ver_eps,
        recovery_episodes_count=rec_eps,
        terminal_episodes_count=term_eps,
        total_errors=total_errs,
        unhandled_exceptions_count=unhandled_exc,
        tool_errors_count=tool_errs,
        exit_code_cascades_max=max_cascade,
        recovered_errors_count=rec_errs,
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
        unpaired_tool_calls_count=ir.unpaired_tool_calls_count,
        linkage_coverage=ir.linkage_coverage,
        analysis_ready=analysis_ready,
        hold_reasons=tuple(hold_reasons),
    )


@dataclass(frozen=True)
class EvidenceWindow:
    """A prioritized sequence of raw hydrated events around critical problem-solving moments."""

    window_id: int
    reason: str  # "critical_error" | "terminal_evaluation" | "state_mutation" | "screening_recovery" | "verification" | "context_compaction" | "instruction_boundary" | "terminal_boundary" | "execution_sample"
    step_start: int
    step_end: int
    event_count: int
    events: tuple[dict[str, Any], ...]
    reopening_citation: CitationHandle

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "reason": self.reason,
            "step_start": self.step_start,
            "step_end": self.step_end,
            "event_count": self.event_count,
            "events": list(self.events),
            "reopening_citation": self.reopening_citation.to_dict(),
        }


@dataclass(frozen=True)
class OmittedRange:
    """Explicit accounting of steps omitted from the detailed evidence window with content digests."""

    range_id: int
    step_start: int
    step_end: int
    event_count: int
    event_ids: tuple[str, ...]
    action_families: tuple[str, ...]
    summary: str
    omitted_content_digest: str  # SHA-256 over canonical JSON of omitted event payloads
    reopening_citation: CitationHandle

    def to_dict(self) -> dict[str, Any]:
        return {
            "range_id": self.range_id,
            "step_start": self.step_start,
            "step_end": self.step_end,
            "event_count": self.event_count,
            "event_ids": list(self.event_ids),
            "action_families": list(self.action_families),
            "summary": self.summary,
            "omitted_content_digest": self.omitted_content_digest,
            "reopening_citation": self.reopening_citation.to_dict(),
        }


@dataclass(frozen=True)
class EvidencePack:
    """Bounded, citation-preserving evidence pack ready for model interpretation."""

    pack_version: str
    pack_digest: str
    trial_id: str
    trial_name: str
    job_id: str
    job_name: str
    task_name: str
    agent_name: str
    model_name: str
    final_verdict: str
    primary_reward: float | None
    exception_class: str | None
    quality_status: str
    quality_findings: tuple[str, ...]
    budget_tokens: int
    consumed_tokens_est: int
    is_model_callable: bool
    tiered_pack_required: bool
    abstain_required: bool
    overflow_reason: str | None
    redaction_profile_digest: str
    global_outline: dict[str, Any]
    episodes: tuple[dict[str, Any], ...]
    selected_windows: tuple[EvidenceWindow, ...]
    omitted_ranges: tuple[OmittedRange, ...]
    evidence_coverage: dict[str, Any]
    source_digests: dict[str, str]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_version": self.pack_version,
            "pack_digest": self.pack_digest,
            "trial_id": self.trial_id,
            "trial_name": self.trial_name,
            "job_id": self.job_id,
            "job_name": self.job_name,
            "task_name": self.task_name,
            "agent_name": self.agent_name,
            "model_name": self.model_name,
            "final_verdict": self.final_verdict,
            "primary_reward": self.primary_reward,
            "exception_class": self.exception_class,
            "quality_status": self.quality_status,
            "quality_findings": list(self.quality_findings),
            "budget_tokens": self.budget_tokens,
            "consumed_tokens_est": self.consumed_tokens_est,
            "is_model_callable": self.is_model_callable,
            "tiered_pack_required": self.tiered_pack_required,
            "abstain_required": self.abstain_required,
            "overflow_reason": self.overflow_reason,
            "redaction_profile_digest": self.redaction_profile_digest,
            "global_outline": self.global_outline,
            "episodes": list(self.episodes),
            "selected_windows": [w.to_dict() for w in self.selected_windows],
            "omitted_ranges": [o.to_dict() for o in self.omitted_ranges],
            "evidence_coverage": self.evidence_coverage,
            "source_digests": self.source_digests,
            "created_at": self.created_at,
        }

    def to_projection_dict(self) -> dict[str, Any]:
        """Flat projection row matching DuckDB evidence_packs table and v_evidence_packs view."""
        return {
            "pack_digest": self.pack_digest,
            "ir_digest": self.source_digests.get("ir_digest", ""),
            "trial_id": self.trial_id,
            "job_id": self.job_id,
            "trial_name": self.trial_name,
            "job_name": self.job_name,
            "task_name": self.task_name,
            "agent_name": self.agent_name,
            "model_name": self.model_name,
            "final_verdict": self.final_verdict,
            "primary_reward": self.primary_reward,
            "quality_status": self.quality_status,
            "quality_findings_json": json.dumps(list(self.quality_findings)),
            "budget_tokens": self.budget_tokens,
            "consumed_tokens_est": self.consumed_tokens_est,
            "is_model_callable": self.is_model_callable,
            "tiered_pack_required": self.tiered_pack_required,
            "abstain_required": self.abstain_required,
            "overflow_reason": self.overflow_reason or "",
            "redaction_profile_digest": self.redaction_profile_digest,
            "selected_windows_count": len(self.selected_windows),
            "omitted_ranges_count": len(self.omitted_ranges),
            "is_bounded": self.evidence_coverage.get("is_bounded", True),
            "created_at": self.created_at,
        }

    def render_markdown(self) -> str:
        """Render a concise, model-friendly text prompt from the evidence pack."""
        lines: list[str] = []
        lines.append(f"# Evidence Pack: {self.trial_name} ({self.task_name})")
        lines.append(f"**Agent / Model:** {self.agent_name} | {self.model_name}")
        lines.append(f"**Execution Quality:** {self.quality_status}")
        callable_str = "YES" if self.is_model_callable else f"NO ({self.overflow_reason or 'uncallable'})"
        lines.append(f"**Model Callable:** {callable_str} | **Pack Digest:** `{self.pack_digest}` (Budget: {self.budget_tokens} tok, Est Consumed: {self.consumed_tokens_est} tok)")
        lines.append("")

        lines.append("## Global Outline & Telemetry")
        for k, v in self.global_outline.items():
            lines.append(f"- **{k}:** {v}")
        lines.append("")

        lines.append("## Execution Episodes")
        for ep in self.episodes:
            lines.append(f"- **Episode {ep.get('episode_id', 1)} [{ep.get('name', 'ep')}]:** {ep.get('summary', '')}")
        lines.append("")

        lines.append("## Detailed Evidence Windows")
        for win in self.selected_windows:
            lines.append(f"### Window {win.window_id}: Steps {win.step_start}–{win.step_end} (Reason: {win.reason})")
            for ev in win.events:
                actor = ev.get("actor", "agent")
                s_idx = ev.get("step_index", 0)
                cmd = ev.get("status_owning_program") or ev.get("action_family", "action")
                exit_c = ev.get("exit_code")
                exit_str = f" [exit {exit_c}]" if exit_c is not None else ""
                lines.append(f"  - **Step {s_idx} ({actor})** `{cmd}`{exit_str}: {ev.get('summary', '')}")
                content = ev.get("hydrated_content")
                if content:
                    lines.append(f"    ```\n    {content[:300]}\n    ```")
            lines.append("")

        if self.omitted_ranges:
            lines.append("## Omitted Routine Ranges")
            for om in self.omitted_ranges:
                lines.append(f"- **Steps {om.step_start}–{om.step_end}** ({om.event_count} events): {om.summary} [Digest: `{om.omitted_content_digest[:16]}...`]")
            lines.append("")

        return "\n".join(lines)


def reopen_omitted_range(
    pack: EvidencePack,
    range_id: int,
    *,
    trial_dir: Path | None = None,
    store_root: Path | None = None,
    repo_root: Path | None = None,
    policy: RedactionPolicy | None = None,
) -> EvidenceWindow:
    """Losslessly reopen and hydrate an omitted range into a detailed EvidenceWindow.

    Verifies that the reopened content matches omitted_content_digest.
    """
    target_range = next((r for r in pack.omitted_ranges if r.range_id == range_id), None)
    if not target_range:
        raise ValueError(f"Omitted range id {range_id} not found in pack {pack.pack_digest}")

    if policy is None:
        policy = RedactionPolicy()

    # Hydrate all steps across the omitted range and verify digest
    reopened_events: list[dict[str, Any]] = []
    root = (repo_root or Path.cwd()).resolve()

    for step_id in range(target_range.step_start, target_range.step_end + 1):
        step_cit = create_citation_handle(
            source_path=target_range.reopening_citation.source_path,
            source_sha256=target_range.reopening_citation.source_sha256,
            raw_cas_uri=target_range.reopening_citation.raw_cas_uri,
            step_id=step_id,
            target_type="step",
            redaction_profile_digest=policy.compute_digest(),
        )
        hydrated = hydrate_citation(
            step_cit,
            trial_dir=trial_dir,
            repo_root=store_root or root,
            policy=policy,
        )
        reopened_events.append({
            "step_index": step_id,
            "action_family": target_range.action_families[0] if target_range.action_families else "inspection",
            "hydrated_content": hydrated.redacted_content,
            "summary": f"Step {step_id} in {target_range.summary}",
            "reopening_citation": step_cit.to_dict(),
        })

    return EvidenceWindow(
        window_id=target_range.range_id + 1000,
        reason="reopened_omitted_range",
        step_start=target_range.step_start,
        step_end=target_range.step_end,
        event_count=len(reopened_events),
        events=tuple(reopened_events),
        reopening_citation=target_range.reopening_citation,
    )


def build_evidence_pack(
    ir: TrajectoryIR,
    *,
    trial_dir: Path | None = None,
    budget_tokens: int = DEFAULT_TOKEN_BUDGET,
    policy: RedactionPolicy | None = None,
    store_root: Path | None = None,
    repo_root: Path | None = None,
) -> EvidencePack:
    """Construct a bounded, hierarchical, citation-preserving EvidencePack from TrajectoryIR."""
    if policy is None:
        policy = RedactionPolicy()

    policy_digest = policy.compute_digest()
    bm = ir.baseline_metrics
    global_outline = {
        "step_count": bm.step_count,
        "tool_call_count": bm.tool_call_count,
        "unique_tools_count": bm.unique_tools_count,
        "error_count": bm.error_count,
        "recovery_count": bm.recovery_count,
        "linear_innocence_screening": bm.linear_innocence_screening,
        "tool_error_rate_screening": bm.tool_error_rate_screening,
        "context_burn_velocity_screening": bm.context_burn_velocity_screening,
        "max_exit_code_cascade_screening": bm.max_exit_code_cascade_screening,
        "cache_hit_rate_screening": bm.cache_hit_rate_screening,
        "total_tokens": bm.total_tokens,
        "duration_seconds": bm.duration_seconds,
        "cost_usd": bm.cost_usd,
        "unpaired_tool_calls_count": ir.unpaired_tool_calls_count,
        "linkage_coverage": ir.linkage_coverage,
    }

    # 2. Episode Summaries
    episodes_summary = [ep.to_dict() for ep in ir.episodes]

    all_steps_list = sorted({ev.step_index for ev in ir.events}) if ir.events else [1]
    min_step = all_steps_list[0]
    max_step = all_steps_list[-1]

    critical_step_indices: set[int] = set()
    critical_reasons: dict[int, str] = {}

    # 1. Mandatory Instruction Boundary (first 1-3 steps)
    instruction_steps = [s for s in all_steps_list if s <= min_step + 2]
    for s in instruction_steps:
        critical_step_indices.add(s)
        critical_reasons[s] = "instruction_boundary"

    # 2. Mandatory Terminal Boundary (last 3 steps)
    terminal_steps = [s for s in all_steps_list if s >= max_step - 2]
    for s in terminal_steps:
        critical_step_indices.add(s)
        if s not in critical_reasons:
            critical_reasons[s] = "terminal_boundary"

    # 3. Critical Errors (profile-aware ev.is_error ONLY), Error-Adjacent Context, and Semantic Actions
    prior_error_step: int | None = None
    for ev in ir.events:
        if ev.is_error:
            critical_step_indices.add(ev.step_index)
            critical_reasons[ev.step_index] = "critical_error"
            # Anchor context step before
            if (ev.step_index - 1) in all_steps_list and (ev.step_index - 1) not in critical_reasons:
                critical_step_indices.add(ev.step_index - 1)
                critical_reasons[ev.step_index - 1] = "error_adjacent_context"
            # Anchor context step after
            if (ev.step_index + 1) in all_steps_list and (ev.step_index + 1) not in critical_reasons:
                critical_step_indices.add(ev.step_index + 1)
                critical_reasons[ev.step_index + 1] = "error_adjacent_context"
            prior_error_step = ev.step_index
        elif prior_error_step is not None and ev.exit_semantics == "success":
            critical_step_indices.add(ev.step_index)
            critical_reasons[ev.step_index] = "screening_recovery"
            prior_error_step = None
        elif ev.event_type == "verifier_check" or ev.phase == "verifier":
            critical_step_indices.add(ev.step_index)
            critical_reasons[ev.step_index] = "terminal_evaluation"
        elif ev.action_family == "verification":
            critical_step_indices.add(ev.step_index)
            if ev.step_index not in critical_reasons:
                critical_reasons[ev.step_index] = "verification"
        elif ev.action_family in ("file_edit", "file_write"):
            critical_step_indices.add(ev.step_index)
            if ev.step_index not in critical_reasons:
                critical_reasons[ev.step_index] = "state_mutation"
        elif ev.event_type == "context_management" or ev.action_family == "context_control":
            critical_step_indices.add(ev.step_index)
            if ev.step_index not in critical_reasons:
                critical_reasons[ev.step_index] = "context_compaction"

    # Group contiguous critical steps into cohesive windows
    window_step_ranges: list[tuple[int, int, str]] = []
    sorted_critical = sorted(critical_step_indices)

    if sorted_critical:
        curr_start = sorted_critical[0]
        curr_end = sorted_critical[0]
        curr_reason = critical_reasons.get(sorted_critical[0], "instruction_boundary")

        for s in sorted_critical[1:]:
            if s <= curr_end + 1:
                curr_end = s
            else:
                window_step_ranges.append((curr_start, curr_end, curr_reason))
                curr_start = s
                curr_end = s
                curr_reason = critical_reasons.get(s, "critical_event")
        window_step_ranges.append((curr_start, curr_end, curr_reason))

    # 4. Build EvidenceWindows and OmittedRanges (Whole-window selection, no byte-truncation)
    selected_windows: list[EvidenceWindow] = []
    omitted_ranges: list[OmittedRange] = []
    window_id = 1
    omitted_id = 1

    events_by_step: dict[int, list[IREvent]] = {}
    for ev in ir.events:
        events_by_step.setdefault(ev.step_index, []).append(ev)

    all_steps = sorted(events_by_step.keys())
    step_ptr = 0

    while step_ptr < len(all_steps):
        s_idx = all_steps[step_ptr]

        in_range: tuple[int, int, str] | None = None
        for w_start, w_end, w_reason in window_step_ranges:
            if w_start <= s_idx <= w_end:
                in_range = (w_start, w_end, w_reason)
                break

        if in_range:
            w_start, w_end, w_reason = in_range
            window_events_list: list[dict[str, Any]] = []

            source_citation = events_by_step[s_idx][0].source_citation
            reopening_cit = create_citation_handle(
                source_path=source_citation.source_path,
                source_sha256=source_citation.source_sha256,
                raw_cas_uri=source_citation.raw_cas_uri,
                step_id=s_idx,
                target_type="step",
                redaction_profile_digest=policy_digest,
            )

            while step_ptr < len(all_steps) and all_steps[step_ptr] <= w_end:
                cur_step = all_steps[step_ptr]
                for ev in events_by_step.get(cur_step, []):
                    ev_dict = ev.to_dict()
                    hydrated = hydrate_citation(ev.source_citation, trial_dir=trial_dir, repo_root=store_root or repo_root, policy=policy)
                    ev_dict["hydrated_content"] = hydrated.redacted_content
                    window_events_list.append(ev_dict)
                step_ptr += 1

            selected_windows.append(
                EvidenceWindow(
                    window_id=window_id,
                    reason=w_reason,
                    step_start=w_start,
                    step_end=min(w_end, all_steps[-1]),
                    event_count=len(window_events_list),
                    events=tuple(window_events_list),
                    reopening_citation=reopening_cit,
                )
            )
            window_id += 1
        else:
            om_start = s_idx
            om_events: list[IREvent] = []
            while step_ptr < len(all_steps):
                candidate_s = all_steps[step_ptr]
                if any(w[0] <= candidate_s <= w[1] for w in window_step_ranges):
                    break
                om_events.extend(events_by_step.get(candidate_s, []))
                step_ptr += 1

            om_end = all_steps[step_ptr - 1] if step_ptr > 0 else om_start
            fams = tuple(sorted({e.action_family for e in om_events}))
            source_citation = om_events[0].source_citation
            om_cit = create_citation_handle(
                source_path=source_citation.source_path,
                source_sha256=source_citation.source_sha256,
                raw_cas_uri=source_citation.raw_cas_uri,
                step_id=om_start,
                target_type="step",
                redaction_profile_digest=policy_digest,
            )
            om_event_ids = tuple(e.event_id for e in om_events)
            om_payloads = [e.to_dict() for e in om_events]
            om_digest = _sha256_canonical_json(om_payloads)
            summary_str = f"Omitted {len(om_events)} routine event(s) across action families: {', '.join(fams) if fams else 'inspection'}"
            omitted_ranges.append(
                OmittedRange(
                    range_id=omitted_id,
                    step_start=om_start,
                    step_end=om_end,
                    event_count=len(om_events),
                    event_ids=om_event_ids,
                    action_families=fams,
                    summary=summary_str,
                    omitted_content_digest=om_digest,
                    reopening_citation=om_cit,
                )
            )
            omitted_id += 1

    # 5. Token budget enforcement and callability gating
    est_raw_json = json.dumps(
        {
            "outline": global_outline,
            "episodes": episodes_summary,
            "windows": [w.to_dict() for w in selected_windows],
            "omitted": [o.to_dict() for o in omitted_ranges],
        }
    )
    est_tokens = _est_tokens(est_raw_json)

    is_callable = True
    tiered_required = False
    abstain_required = False
    overflow_reason: str | None = None

    if ir.status == "accounted_unavailable" or ir.quality_status in ("fail", "quarantined", "no_atif"):
        is_callable = False
        abstain_required = True
        if ir.status == "accounted_unavailable":
            overflow_reason = f"source_missing ({ir.unavailable_reason or 'missing_trajectory_file'})"
        else:
            overflow_reason = f"quarantined_quality_status_{ir.quality_status}"
    elif est_tokens > budget_tokens:
        is_callable = False
        tiered_required = True
        overflow_reason = f"mandatory_window_budget_overflow ({est_tokens} > {budget_tokens} tokens)"

    # 6. Fine-Grained Category Evidence Coverage
    coverage_metrics = compute_evidence_coverage_metrics(ir, trial_dir=trial_dir)
    coverage_dict = coverage_metrics.to_dict()
    coverage_dict["is_bounded"] = bool(est_tokens <= budget_tokens)

    source_digests = dict(ir.source_digests)
    source_digests["ir_digest"] = ir.ir_digest
    source_digests["redaction_profile_digest"] = policy_digest

    pack_raw = {
        "pack_version": "1.0",
        "trial_id": ir.trial_id,
        "job_id": ir.job_id,
        "trial_name": ir.trial_name,
        "job_name": ir.job_name,
        "task_name": ir.task_name,
        "agent_name": ir.agent_scaffold,
        "model_name": ir.model_name,
        "final_verdict": ir.final_verdict,
        "primary_reward": ir.primary_reward,
        "exception_class": ir.exception_class,
        "quality_status": ir.quality_status,
        "quality_findings": list(ir.quality_findings),
        "budget_tokens": budget_tokens,
        "consumed_tokens_est": est_tokens,
        "is_model_callable": is_callable,
        "tiered_pack_required": tiered_required,
        "abstain_required": abstain_required,
        "overflow_reason": overflow_reason,
        "redaction_profile_digest": policy_digest,
        "global_outline": global_outline,
        "episodes": episodes_summary,
        "selected_windows": [w.to_dict() for w in selected_windows],
        "omitted_ranges": [o.to_dict() for o in omitted_ranges],
        "evidence_coverage": coverage_dict,
        "source_digests": source_digests,
        "created_at": ir.created_at,
    }

    pack_digest = _sha256_canonical_json(pack_raw)

    return EvidencePack(
        pack_version="1.0",
        pack_digest=pack_digest,
        trial_id=ir.trial_id,
        trial_name=ir.trial_name,
        job_id=ir.job_id,
        job_name=ir.job_name,
        task_name=ir.task_name,
        agent_name=ir.agent_scaffold,
        model_name=ir.model_name,
        final_verdict=ir.final_verdict,
        primary_reward=ir.primary_reward,
        exception_class=ir.exception_class,
        quality_status=ir.quality_status,
        quality_findings=ir.quality_findings,
        budget_tokens=budget_tokens,
        consumed_tokens_est=est_tokens,
        is_model_callable=is_callable,
        tiered_pack_required=tiered_required,
        abstain_required=abstain_required,
        overflow_reason=overflow_reason,
        redaction_profile_digest=policy_digest,
        global_outline=global_outline,
        episodes=tuple(episodes_summary),
        selected_windows=tuple(selected_windows),
        omitted_ranges=tuple(omitted_ranges),
        evidence_coverage=coverage_dict,
        source_digests=source_digests,
        created_at=ir.created_at,
    )
