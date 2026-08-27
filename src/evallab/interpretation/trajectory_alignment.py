"""Counterfactual pair matching, sequence alignment, and divergence k* detection.

Key invariants:
- The unit of capability insight is the counterfactual pair, not the solitary trace.
- Strict confound gate: refuses matching when task definitions, verifiers, or environments mismatch.
- Action-level sequence alignment over (action_family, status_owning_program, argument_skeleton).
- Multi-call awareness: aligns individual tool calls rather than collapsing multiple calls per step.
- Distinguishes local temporary mismatches that reconverge from permanent divergence k*.
- Identifies first non-reconvergent divergence k* with both-branch source citations and unmatched ranges.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from evallab.interpretation.trajectory_hydration import CitationHandle
from evallab.interpretation.trajectory_ir import IREvent, TrajectoryIR
from evallab.interpretation.trajectory_sequence import normalized_edit_distance


class ConfoundedPairError(ValueError):
    """Raised when two trials cannot be validly aligned due to task/environment confounds."""


@dataclass(frozen=True)
class AlignedStepPair:
    """One aligned step coordinate across two trajectory branches."""

    step_a: int | None
    call_index_a: int | None
    step_b: int | None
    call_index_b: int | None
    action_a: str | None
    action_b: str | None
    match_quality: str  # "exact" | "partial" | "mismatch" | "gap_a" | "gap_b"
    score: float


@dataclass(frozen=True)
class LocalDivergenceRecord:
    """A temporary divergence sequence that subsequently reconverged."""

    divergence_step_a: int
    divergence_step_b: int
    reconvergence_step_a: int
    reconvergence_step_b: int
    span_a: int
    span_b: int


@dataclass(frozen=True)
class PairedAlignmentResult:
    """Deterministic result of aligning two counterfactual trajectory branches."""

    alignment_id: str
    alignment_version: str
    trial_id_a: str
    trial_id_b: str
    ir_digest_a: str
    ir_digest_b: str
    trial_name_a: str
    trial_name_b: str
    task_name: str
    config_delta: str
    outcome_delta: str
    divergence_step_a: int | None  # First non-reconvergent divergence k*
    divergence_step_b: int | None
    citation_a: CitationHandle | None
    citation_b: CitationHandle | None
    has_local_divergences: bool
    local_divergences: tuple[LocalDivergenceRecord, ...]
    unmatched_ranges_a: tuple[tuple[int, int], ...]
    unmatched_ranges_b: tuple[tuple[int, int], ...]
    alignment_score: float
    normalized_edit_distance: float
    total_aligned_steps: int
    aligned_pairs: tuple[AlignedStepPair, ...]
    summary: str
    created_at: str
    def to_dict(self) -> dict[str, Any]:
        return {
            "alignment_id": self.alignment_id,
            "alignment_version": self.alignment_version,
            "trial_id_a": self.trial_id_a,
            "trial_id_b": self.trial_id_b,
            "ir_digest_a": self.ir_digest_a,
            "ir_digest_b": self.ir_digest_b,
            "trial_name_a": self.trial_name_a,
            "trial_name_b": self.trial_name_b,
            "task_name": self.task_name,
            "config_delta": self.config_delta,
            "outcome_delta": self.outcome_delta,
            "divergence_step_a": self.divergence_step_a,
            "divergence_step_b": self.divergence_step_b,
            "citation_a": self.citation_a.to_dict() if self.citation_a else None,
            "citation_b": self.citation_b.to_dict() if self.citation_b else None,
            "has_local_divergences": self.has_local_divergences,
            "local_divergences": [asdict(d) for d in self.local_divergences],
            "unmatched_ranges_a": list(self.unmatched_ranges_a),
            "unmatched_ranges_b": list(self.unmatched_ranges_b),
            "alignment_score": self.alignment_score,
            "normalized_edit_distance": self.normalized_edit_distance,
            "total_aligned_steps": self.total_aligned_steps,
            "aligned_pairs": [asdict(p) for p in self.aligned_pairs],
            "summary": self.summary,
            "created_at": self.created_at,
        }

    def to_projection_dict(self) -> dict[str, Any]:
        """Flat projection row matching DuckDB paired_alignments table and v_paired_alignments view."""
        return {
            "alignment_id": self.alignment_id,
            "alignment_version": self.alignment_version,
            "trial_id_a": self.trial_id_a,
            "trial_id_b": self.trial_id_b,
            "ir_digest_a": self.ir_digest_a,
            "ir_digest_b": self.ir_digest_b,
            "trial_name_a": self.trial_name_a,
            "trial_name_b": self.trial_name_b,
            "task_name": self.task_name,
            "config_delta": self.config_delta,
            "outcome_delta": self.outcome_delta,
            "divergence_step_a": self.divergence_step_a,
            "divergence_step_b": self.divergence_step_b,
            "citation_a_json": json.dumps(self.citation_a.to_dict()) if self.citation_a else "",
            "citation_b_json": json.dumps(self.citation_b.to_dict()) if self.citation_b else "",
            "has_local_divergences": self.has_local_divergences,
            "local_divergences_json": json.dumps([asdict(d) for d in self.local_divergences]),
            "unmatched_ranges_a_json": json.dumps(list(self.unmatched_ranges_a)),
            "unmatched_ranges_b_json": json.dumps(list(self.unmatched_ranges_b)),
            "alignment_score": self.alignment_score,
            "normalized_edit_distance": self.normalized_edit_distance,
            "total_aligned_steps": self.total_aligned_steps,
            "aligned_pairs_count": len(self.aligned_pairs),
            "summary": self.summary,
            "created_at": self.created_at,
        }

def _action_token(ev: IREvent) -> tuple[str, str, str]:
    """Tuple signature for sequence alignment: (family, program, skeleton)."""
    return (
        ev.action_family or "other",
        (ev.status_owning_program or "").lower(),
        (ev.argument_skeleton or "").lower(),
    )


def _step_actions(ir: TrajectoryIR) -> list[tuple[int, int | None, tuple[str, str, str], IREvent]]:
    """Extract fine-grained action tokens including individual tool calls from an IR instance."""
    actions: list[tuple[int, int | None, tuple[str, str, str], IREvent]] = []
    seen_events: set[str] = set()

    for ev in ir.events:
        # Align only agent tool calls and verifier checks; keep filesystem state events isolated from action tokens
        if (
            ev.event_type in ("tool_call", "verifier_check")
            or ev.action_family in ("file_edit", "file_write", "verification")
        ) and ev.event_type != "state_change" and ev.step_index is not None and ev.event_id not in seen_events:
            actions.append((ev.step_index, ev.call_index, _action_token(ev), ev))
            seen_events.add(ev.event_id)
    return actions

def align_action_sequences(
    seq_a: Sequence[tuple[int, int | None, tuple[str, str, str], IREvent]],
    seq_b: Sequence[tuple[int, int | None, tuple[str, str, str], IREvent]],
) -> tuple[list[AlignedStepPair], float]:
    """Needleman-Wunsch global sequence alignment over normalized action tuples."""
    n = len(seq_a)
    m = len(seq_b)

    if n == 0 and m == 0:
        return [], 0.0

    GAP_PENALTY = -1.5
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]

    for i in range(n + 1):
        dp[i][0] = i * GAP_PENALTY
    for j in range(m + 1):
        dp[0][j] = j * GAP_PENALTY

    def _score(t_a: tuple[str, str, str], t_b: tuple[str, str, str]) -> float:
        fam_a, prog_a, skel_a = t_a
        fam_b, prog_b, skel_b = t_b
        if fam_a == fam_b and prog_a == prog_b and skel_a == skel_b and prog_a != "":
            return 2.0
        if fam_a == fam_b and prog_a == prog_b and prog_a != "":
            return 1.0
        if fam_a == fam_b:
            return 0.5
        return -1.0

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            match_score = _score(seq_a[i - 1][2], seq_b[j - 1][2])
            dp[i][j] = max(
                dp[i - 1][j - 1] + match_score,
                dp[i - 1][j] + GAP_PENALTY,
                dp[i][j - 1] + GAP_PENALTY,
            )

    aligned: list[AlignedStepPair] = []
    i = n
    j = m

    while i > 0 or j > 0:
        if i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + _score(seq_a[i - 1][2], seq_b[j - 1][2]):
            s_a, c_a, tok_a, _ = seq_a[i - 1]
            s_b, c_b, tok_b, _ = seq_b[j - 1]
            sc = _score(tok_a, tok_b)
            qual = "exact" if sc == 2.0 else ("partial" if sc >= 0.5 else "mismatch")
            aligned.append(
                AlignedStepPair(
                    step_a=s_a,
                    call_index_a=c_a,
                    step_b=s_b,
                    call_index_b=c_b,
                    action_a=f"{tok_a[1]}({tok_a[0]})",
                    action_b=f"{tok_b[1]}({tok_b[0]})",
                    match_quality=qual,
                    score=sc,
                )
            )
            i -= 1
            j -= 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + GAP_PENALTY:
            s_a, c_a, tok_a, _ = seq_a[i - 1]
            aligned.append(
                AlignedStepPair(
                    step_a=s_a,
                    call_index_a=c_a,
                    step_b=None,
                    call_index_b=None,
                    action_a=f"{tok_a[1]}({tok_a[0]})",
                    action_b=None,
                    match_quality="gap_b",
                    score=GAP_PENALTY,
                )
            )
            i -= 1
        else:
            s_b, c_b, tok_b, _ = seq_b[j - 1]
            aligned.append(
                AlignedStepPair(
                    step_a=None,
                    call_index_a=None,
                    step_b=s_b,
                    call_index_b=c_b,
                    action_a=None,
                    action_b=f"{tok_b[1]}({tok_b[0]})",
                    match_quality="gap_a",
                    score=GAP_PENALTY,
                )
            )
            j -= 1

    aligned.reverse()
    total_score = dp[n][m]
    return aligned, round(total_score, 2)


def align_trajectory_pair(
    ir_a: TrajectoryIR,
    ir_b: TrajectoryIR,
) -> PairedAlignmentResult:
    """Align two counterfactual trajectory branches, enforcing confound gates and finding non-reconvergent k*."""
    # 1. Strict Confound Gate
    if ir_a.task_name != ir_b.task_name:
        raise ConfoundedPairError(
            f"Cannot align trials with mismatched task names: {ir_a.task_name!r} vs {ir_b.task_name!r}"
        )

    if (ir_a.task_digest or ir_b.task_digest) and ir_a.task_digest != ir_b.task_digest:
        raise ConfoundedPairError(
            f"Task digest mismatch ({ir_a.task_digest} vs {ir_b.task_digest}); trials evaluated different task versions."
        )

    if (ir_a.verifier_digest or ir_b.verifier_digest) and ir_a.verifier_digest != ir_b.verifier_digest:
        raise ConfoundedPairError(
            f"Verifier digest mismatch ({ir_a.verifier_digest} vs {ir_b.verifier_digest}); evaluation criteria differ."
        )
    # 2. Extract fine-grained step actions
    seq_a = _step_actions(ir_a)
    seq_b = _step_actions(ir_b)

    aligned_pairs, score = align_action_sequences(seq_a, seq_b)
    tokens_a = [_action_token(ev) for _, _, _, ev in seq_a]
    tokens_b = [_action_token(ev) for _, _, _, ev in seq_b]
    norm_edit_dist = round(normalized_edit_distance(tokens_a, tokens_b), 4)
    # 3. Analyze local divergences vs permanent non-reconvergent divergence k*
    local_divergences: list[LocalDivergenceRecord] = []
    unmatched_a: list[tuple[int, int]] = []
    unmatched_b: list[tuple[int, int]] = []

    match_indices = [idx for idx, p in enumerate(aligned_pairs) if p.match_quality in ("exact", "partial")]

    k_star_idx: int | None = None
    if match_indices:
        last_match_idx = match_indices[-1]
        if last_match_idx + 1 < len(aligned_pairs):
            k_star_idx = last_match_idx + 1
    elif aligned_pairs:
        k_star_idx = 0

    if match_indices and len(match_indices) >= 2:
        for m_i in range(len(match_indices) - 1):
            cur_m = match_indices[m_i]
            next_m = match_indices[m_i + 1]
            if next_m > cur_m + 1:
                first_div = aligned_pairs[cur_m + 1]
                reconv_p = aligned_pairs[next_m]
                s_a_div = first_div.step_a or aligned_pairs[cur_m].step_a or 0
                s_b_div = first_div.step_b or aligned_pairs[cur_m].step_b or 0
                s_a_rec = reconv_p.step_a or 0
                s_b_rec = reconv_p.step_b or 0
                local_divergences.append(
                    LocalDivergenceRecord(
                        divergence_step_a=s_a_div,
                        divergence_step_b=s_b_div,
                        reconvergence_step_a=s_a_rec,
                        reconvergence_step_b=s_b_rec,
                        span_a=max(0, s_a_rec - s_a_div),
                        span_b=max(0, s_b_rec - s_b_div),
                    )
                )

    k_star_a: int | None = None
    k_star_call_a: int | None = None
    k_star_b: int | None = None
    k_star_call_b: int | None = None
    if k_star_idx is not None and k_star_idx < len(aligned_pairs):
        p_div = aligned_pairs[k_star_idx]
        k_star_a = p_div.step_a
        k_star_call_a = p_div.call_index_a
        k_star_b = p_div.step_b
        k_star_call_b = p_div.call_index_b

    curr_unmatched_a_start = None
    curr_unmatched_a_end = None
    curr_unmatched_b_start = None
    curr_unmatched_b_end = None

    for p in aligned_pairs:
        if p.match_quality == "gap_b" and p.step_a is not None:
            if curr_unmatched_a_start is None:
                curr_unmatched_a_start = p.step_a
            curr_unmatched_a_end = p.step_a
        elif curr_unmatched_a_start is not None:
            unmatched_a.append((curr_unmatched_a_start, curr_unmatched_a_end or curr_unmatched_a_start))
            curr_unmatched_a_start = None

        if p.match_quality == "gap_a" and p.step_b is not None:
            if curr_unmatched_b_start is None:
                curr_unmatched_b_start = p.step_b
            curr_unmatched_b_end = p.step_b
        elif curr_unmatched_b_start is not None:
            unmatched_b.append((curr_unmatched_b_start, curr_unmatched_b_end or curr_unmatched_b_start))
            curr_unmatched_b_start = None

    if curr_unmatched_a_start is not None:
        unmatched_a.append((curr_unmatched_a_start, curr_unmatched_a_end or curr_unmatched_a_start))
    if curr_unmatched_b_start is not None:
        unmatched_b.append((curr_unmatched_b_start, curr_unmatched_b_end or curr_unmatched_b_start))
    cit_a: CitationHandle | None = None
    cit_b: CitationHandle | None = None

    if k_star_a is not None:
        for s_idx, c_idx, _, ev in seq_a:
            if s_idx == k_star_a and (k_star_call_a is None or c_idx == k_star_call_a):
                cit_a = ev.source_citation
                break

    if k_star_b is not None:
        for s_idx, c_idx, _, ev in seq_b:
            if s_idx == k_star_b and (k_star_call_b is None or c_idx == k_star_call_b):
                cit_b = ev.source_citation
                break
    cfg_delta = (
        f"model: {ir_a.model_name} vs {ir_b.model_name}"
        if ir_a.model_name != ir_b.model_name
        else f"agent: {ir_a.agent_scaffold} vs {ir_b.agent_scaffold}"
    )
    outcome_delta = f"{ir_a.final_verdict} (reward {ir_a.primary_reward}) vs {ir_b.final_verdict} (reward {ir_b.primary_reward})"

    div_summary = (
        f"Divergence k* at step ({k_star_a}, {k_star_b})"
        if k_star_idx is not None
        else "Branches strictly aligned"
    )
    if local_divergences:
        div_summary += f" ({len(local_divergences)} local reconvergent divergence(s))"

    summary = f"Aligned {len(aligned_pairs)} action step(s) | Score: {score} | {div_summary}"

    align_id = hashlib.sha256(
        f"v1:{ir_a.ir_digest}:{ir_b.ir_digest}:{score}:{k_star_a}:{k_star_b}".encode()
    ).hexdigest()

    return PairedAlignmentResult(
        alignment_id=f"align_{align_id[:16]}",
        alignment_version="1.0.0",
        trial_id_a=ir_a.trial_id,
        trial_id_b=ir_b.trial_id,
        ir_digest_a=ir_a.ir_digest,
        ir_digest_b=ir_b.ir_digest,
        trial_name_a=ir_a.trial_name,
        trial_name_b=ir_b.trial_name,
        task_name=ir_a.task_name,
        config_delta=cfg_delta,
        outcome_delta=outcome_delta,
        divergence_step_a=k_star_a,
        divergence_step_b=k_star_b,
        citation_a=cit_a,
        citation_b=cit_b,
        has_local_divergences=len(local_divergences) > 0,
        local_divergences=tuple(local_divergences),
        unmatched_ranges_a=tuple(unmatched_a),
        unmatched_ranges_b=tuple(unmatched_b),
        alignment_score=score,
        normalized_edit_distance=norm_edit_dist,
        total_aligned_steps=len(aligned_pairs),
        aligned_pairs=tuple(aligned_pairs),
        summary=summary,
        created_at=ir_a.created_at or ir_b.created_at,
    )
