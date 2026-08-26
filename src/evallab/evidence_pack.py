"""EvidencePack v1: Hierarchical, bounded, citation-preserving input for interpreting models.

Key invariants:
- Models consume bounded JSON EvidencePacks (never raw Parquet or entire uncompressed directories).
- Hierarchical compression: global outline -> episode index -> selected raw evidence windows.
- Mandatory-window budget overflow or quarantine produces uncallable pack (abstain/tier required).
- Every included fact and omitted range carries an exact canonical CitationHandle reopening coordinate.
- Deterministic token budgeting and pack content hashing (pack_digest).
- Redaction changes mint a new pack digest; raw source digest remains separate.
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
class EvidenceWindow:
    """A prioritized sequence of raw hydrated events around critical problem-solving moments."""

    window_id: int
    reason: str  # "error_observation" | "verifier_evaluation" | "state_mutation" | "loop_trigger" | "counterfactual_divergence"
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
    """Explicit accounting of steps omitted from the detailed evidence window."""

    range_id: int
    step_start: int
    step_end: int
    event_count: int
    action_families: tuple[str, ...]
    summary: str
    reopening_citation: CitationHandle

    def to_dict(self) -> dict[str, Any]:
        return {
            "range_id": self.range_id,
            "step_start": self.step_start,
            "step_end": self.step_end,
            "event_count": self.event_count,
            "action_families": list(self.action_families),
            "summary": self.summary,
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

    def render_markdown(self) -> str:
        """Render a concise, model-friendly text prompt from the evidence pack."""
        lines: list[str] = []
        lines.append(f"# Evidence Pack: {self.trial_name} ({self.task_name})")
        lines.append(f"**Outcome:** {self.final_verdict} (Reward: {self.primary_reward} | Quality: {self.quality_status})")
        lines.append(f"**Agent / Model:** {self.agent_name} | {self.model_name}")
        callable_str = "YES" if self.is_model_callable else f"NO ({self.overflow_reason or 'uncallable'})"
        lines.append(f"**Model Callable:** {callable_str} | **Pack Digest:** `{self.pack_digest}` (Budget: {self.budget_tokens} tok, Est Consumed: {self.consumed_tokens_est} tok)")
        lines.append("")

        lines.append("## Global Outline & Telemetry")
        for k, v in self.global_outline.items():
            lines.append(f"- **{k}:** {v}")
        lines.append("")

        lines.append("## Execution Episodes")
        for ep in self.episodes:
            lines.append(f"- **Episode {ep.get('episode_id')} ({ep.get('episode_type')}):** steps {ep.get('start_ordinal')}..{ep.get('end_ordinal')} — {ep.get('summary')}")
        lines.append("")

        if self.selected_windows:
            lines.append("## Selected Detailed Evidence Windows")
            for w in self.selected_windows:
                lines.append(f"### Window #{w.window_id} ({w.reason}) — Steps {w.step_start}..{w.step_end}")
                lines.append(f"- **Citation:** `{w.reopening_citation.format_citation()}`")
                for ev in w.events:
                    exit_info = f" [exit {ev.get('exit_code')}]" if ev.get("exit_code") is not None else ""
                    lines.append(f"  - **Event {ev.get('event_ordinal')} ({ev.get('actor')}/{ev.get('event_type')}):** `{ev.get('summary')}`{exit_info}")
                    if ev.get("hydrated_content"):
                        lines.append("    ```")
                        lines.append(f"    {ev['hydrated_content'].strip()}")
                        lines.append("    ```")
            lines.append("")

        if self.omitted_ranges:
            lines.append("## Omitted Routine Ranges (Preserved with Reopening Handles)")
            for om in self.omitted_ranges:
                lines.append(f"- **Steps {om.step_start}..{om.step_end} ({om.event_count} events):** {om.summary} | Citation: `{om.reopening_citation.format_citation()}`")
            lines.append("")

        return "\n".join(lines)


def build_evidence_pack(
    ir: TrajectoryIR,
    *,
    trial_dir: Path | None = None,
    budget_tokens: int = DEFAULT_TOKEN_BUDGET,
    policy: RedactionPolicy | None = None,
) -> EvidencePack:
    """Build a hierarchical, bounded EvidencePack from a TrajectoryIR."""
    if policy is None:
        policy = RedactionPolicy()

    policy_digest = policy.compute_digest()

    # 1. Global Outline
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

    # 3. Identify Mandatory & Optional Steps for Windows
    critical_step_indices: set[int] = set()
    critical_reasons: dict[int, str] = {}

    for ev in ir.events:
        # Reason 1: Error or failing tool execution
        if ev.is_error or (ev.exit_code is not None and ev.exit_code != 0):
            critical_step_indices.add(ev.step_index)
            critical_reasons[ev.step_index] = "error_observation"
        # Reason 2: Verifier check
        elif ev.event_type == "verifier_check" or ev.phase == "verifier":
            critical_step_indices.add(ev.step_index)
            critical_reasons[ev.step_index] = "verifier_evaluation"
        # Reason 3: State mutation
        elif ev.action_family in ("file_edit", "file_write") and len([s for s in critical_step_indices if critical_reasons.get(s) == "state_mutation"]) < 2:
            critical_step_indices.add(ev.step_index)
            critical_reasons[ev.step_index] = "state_mutation"

    # Expand critical steps to include 1 step of context
    window_step_ranges: list[tuple[int, int, str]] = []
    sorted_critical = sorted(critical_step_indices)

    if sorted_critical:
        curr_start = max(1, sorted_critical[0] - 1)
        curr_end = sorted_critical[0] + 1
        curr_reason = critical_reasons.get(sorted_critical[0], "critical_event")

        for s in sorted_critical[1:]:
            s_start = max(1, s - 1)
            s_end = s + 1
            if s_start <= curr_end + 1:
                curr_end = max(curr_end, s_end)
            else:
                window_step_ranges.append((curr_start, curr_end, curr_reason))
                curr_start = s_start
                curr_end = s_end
                curr_reason = critical_reasons.get(s, "critical_event")
        window_step_ranges.append((curr_start, curr_end, curr_reason))

    # If no critical steps, include initial prompt & final verifier
    if not window_step_ranges and ir.events:
        first_step = ir.events[0].step_index
        last_step = ir.events[-1].step_index
        window_step_ranges.append((first_step, min(first_step + 2, last_step), "execution_sample"))

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

            reopening_cit = create_citation_handle(
                source_path=ir.source_digests.get("source_sha256", "agent/trajectory.json"),
                source_sha256=ir.source_digests.get("source_sha256", ""),
                raw_cas_uri=ir.source_digests.get("cas_uri"),
                step_id=w_start,
                target_type="step",
                redaction_profile_digest=policy_digest,
            )

            while step_ptr < len(all_steps) and all_steps[step_ptr] <= w_end:
                cur_step = all_steps[step_ptr]
                for ev in events_by_step.get(cur_step, []):
                    ev_dict = ev.to_dict()
                    if trial_dir is not None:
                        hydrated = hydrate_citation(ev.source_citation, trial_dir=trial_dir, policy=policy)
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
            om_cit = create_citation_handle(
                source_path=ir.source_digests.get("source_sha256", "agent/trajectory.json"),
                source_sha256=ir.source_digests.get("source_sha256", ""),
                raw_cas_uri=ir.source_digests.get("cas_uri"),
                step_id=om_start,
                target_type="step",
                redaction_profile_digest=policy_digest,
            )
            summary_str = f"Omitted {len(om_events)} routine event(s) across action families: {', '.join(fams) if fams else 'inspection'}"
            omitted_ranges.append(
                OmittedRange(
                    range_id=omitted_id,
                    step_start=om_start,
                    step_end=om_end,
                    event_count=len(om_events),
                    action_families=fams,
                    summary=summary_str,
                    reopening_citation=om_cit,
                )
            )
            omitted_id += 1

    # 5. Token budget enforcement and callability gating
    est_raw_json = json.dumps(
        {
            "global_outline": global_outline,
            "episodes": episodes_summary,
            "selected_windows": [w.to_dict() for w in selected_windows],
            "omitted_ranges": [o.to_dict() for o in omitted_ranges],
        }
    )
    consumed_est = _est_tokens(est_raw_json)

    # Callability & overflow checks
    is_model_callable = True
    tiered_pack_required = False
    abstain_required = False
    overflow_reason: str | None = None

    if consumed_est > budget_tokens:
        is_model_callable = False
        tiered_pack_required = True
        overflow_reason = f"mandatory_window_budget_overflow ({consumed_est} > {budget_tokens})"
    elif ir.quality_status in ("quarantine", "fail", "quarantined"):
        is_model_callable = False
        abstain_required = True
        overflow_reason = f"quality_{ir.quality_status}_blocked"

    # 6. Build Pack Data and Compute Deterministic Digest
    evidence_coverage = {
        "ir_digest": ir.ir_digest,
        "total_events": len(ir.events),
        "selected_events_count": sum(w.event_count for w in selected_windows),
        "omitted_events_count": sum(o.event_count for o in omitted_ranges),
        "budget_tokens": budget_tokens,
        "consumed_tokens_est": consumed_est,
        "is_bounded": consumed_est <= budget_tokens,
        "is_model_callable": is_model_callable,
        "unpaired_tool_calls_count": ir.unpaired_tool_calls_count,
        "linkage_coverage": ir.linkage_coverage,
    }

    raw_pack_dict = {
        "pack_version": "1.0",
        "trial_id": ir.trial_id,
        "trial_name": ir.trial_name,
        "job_id": ir.job_id,
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
        "consumed_tokens_est": consumed_est,
        "is_model_callable": is_model_callable,
        "tiered_pack_required": tiered_pack_required,
        "abstain_required": abstain_required,
        "overflow_reason": overflow_reason,
        "redaction_profile_digest": policy_digest,
        "global_outline": global_outline,
        "episodes": episodes_summary,
        "selected_windows": [w.to_dict() for w in selected_windows],
        "omitted_ranges": [o.to_dict() for o in omitted_ranges],
        "evidence_coverage": evidence_coverage,
        "source_digests": ir.source_digests,
        "created_at": ir.created_at,
    }

    pack_digest = _sha256_canonical_json(raw_pack_dict)

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
        consumed_tokens_est=consumed_est,
        is_model_callable=is_model_callable,
        tiered_pack_required=tiered_pack_required,
        abstain_required=abstain_required,
        overflow_reason=overflow_reason,
        redaction_profile_digest=policy_digest,
        global_outline=global_outline,
        episodes=tuple(episodes_summary),
        selected_windows=tuple(selected_windows),
        omitted_ranges=tuple(omitted_ranges),
        evidence_coverage=evidence_coverage,
        source_digests=ir.source_digests,
        created_at=ir.created_at,
    )
