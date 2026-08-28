"""Trajectory Interpretation Card Renderer (evallab traj card <trial>).

Renders a comprehensive, provenance-grounded diagnostic card for an ATIF trajectory trial:
1. Identity & Final Outcome (verdict, reward, exception, cost/tokens, execution duration)
2. Quality Status (when quality ledger available; cleanly reports unknown when absent)
3. Phase Outline (ordered semantic phases: setup, prompt, work, verifier)
4. Mechanical Baseline Metrics (LI, TER, CBV, cache hit rate, subagent overhead)
5. Cited Error Observations & Stderr (untruncated stderr/observations seen by agent with exact source citations)
6. Loop & Cascade Reason Codes (deterministic reason codes and step citations)
7. Intervention Provenance (autonomous vs user-assisted vs system-assisted)
8. Semantic Coverage Status (analysis_ready vs screening_only vs unprojected)
9. Exact Source Citations (trial dir, trajectory sha256, result sha256, step coordinates)
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from evallab.interpretation.traj_baseline import TraceBaselineRecord, compute_trace_baseline
from evallab.interpretation.trajectory_hydration import (
    HydratedEvidence,
    RedactionPolicy,
    hydrate_error_observations,
)
from evallab.results import sha256_file
from evallab.traj import (
    PhaseOutline,
    TrajectoryOutline,
    outline_trajectory,
    resolve_trial_target,
)


@dataclass(frozen=True)
class QualityInspection:
    """Quality status inspected from trial artifacts or quality ledger."""

    status: str  # "pass" | "warn" | "fail" | "quarantined" | "unknown"
    source: str
    reasons: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class SemanticCoverageInspection:
    """Semantic facts and coverage inspection for a trial."""

    status: str  # "analysis_ready" | "screening_only" | "unprojected"
    source: str
    fact_tables: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class InterventionProvenance:
    """Analysis of intervening user/system turns vs autonomous execution."""

    category: str  # "autonomous" | "user_assisted" | "system_assisted" | "interrupted"
    user_steps_count: int
    system_steps_count: int
    agent_steps_count: int
    has_intermediate_user_turns: bool
    summary: str


@dataclass(frozen=True)
class TrajectoryCardData:
    """Structured data payload backing a Trajectory Interpretation Card."""

    trial_id: str
    trial_name: str
    job_id: str
    job_name: str
    task_name: str
    agent_name: str
    agent_version: str
    model_name: str
    status: str
    final_verdict: str
    evidence_limitation: str | None
    primary_reward: float | None
    exception_class: str | None
    exception_message: str | None
    duration_seconds: float | None
    cost_usd: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    total_tokens: int | None
    quality: QualityInspection
    semantic_coverage: SemanticCoverageInspection
    intervention: InterventionProvenance
    baseline_metrics: TraceBaselineRecord
    phases: tuple[PhaseOutline, ...]
    error_evidences: tuple[HydratedEvidence, ...]
    loop_score: float
    loop_detected: bool
    loop_reasons: tuple[str, ...]
    trial_dir: str
    trajectory_path: str
    trajectory_sha256: str
    result_path: str | None
    result_sha256: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "trial_name": self.trial_name,
            "job_id": self.job_id,
            "job_name": self.job_name,
            "task_name": self.task_name,
            "agent_name": self.agent_name,
            "agent_version": self.agent_version,
            "model_name": self.model_name,
            "status": self.status,
            "final_verdict": self.final_verdict,
            "evidence_limitation": self.evidence_limitation,
            "primary_reward": self.primary_reward,
            "exception_class": self.exception_class,
            "exception_message": self.exception_message,
            "duration_seconds": self.duration_seconds,
            "cost_usd": self.cost_usd,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "quality": asdict(self.quality),
            "semantic_coverage": asdict(self.semantic_coverage),
            "intervention": asdict(self.intervention),
            "baseline_metrics": self.baseline_metrics.to_dict(),
            "phases": [asdict(p) for p in self.phases],
            "error_evidences": [e.to_dict() for e in self.error_evidences],
            "loop_score": self.loop_score,
            "loop_detected": self.loop_detected,
            "loop_reasons": list(self.loop_reasons),
            "trial_dir": self.trial_dir,
            "trajectory_path": self.trajectory_path,
            "trajectory_sha256": self.trajectory_sha256,
            "result_path": self.result_path,
            "result_sha256": self.result_sha256,
        }


def _inspect_quality_status(trial_dir: Path) -> QualityInspection:
    """Inspect quality ledger output when available, otherwise returning unknown."""
    # Check for direct quality files in trial directory
    candidate_files = [
        trial_dir / "quality.json",
        trial_dir / "status.json",
        trial_dir / "trial_quality.json",
        trial_dir / "quality-findings.json",
    ]
    for cand in candidate_files:
        if cand.is_file():
            try:
                data = json.loads(cand.read_text(encoding="utf-8", errors="replace"))
                if isinstance(data, dict):
                    status_val = data.get("quality_status") or data.get("status")
                    if status_val and str(status_val).lower() in (
                        "pass",
                        "warn",
                        "fail",
                        "quarantined",
                    ):
                        reasons = tuple(data.get("reasons") or data.get("findings") or [])
                        return QualityInspection(
                            status=str(status_val).lower(),
                            source=cand.name,
                            reasons=reasons,
                            detail=f"Inspected from {cand.name}",
                        )
            except Exception:
                pass

    # Clean fallback: quality ledger is uncomputed / unavailable
    return QualityInspection(
        status="unknown",
        source="none",
        reasons=(),
        detail="Quality Ledger uncomputed for this trial",
    )


def _inspect_semantic_coverage(trial_dir: Path) -> SemanticCoverageInspection:
    """Inspect semantic fact tables and evidence coverage for this trial."""
    facts_dir = trial_dir / "facts"
    if not facts_dir.is_dir():
        # Also check parent job facts directory if partitioned
        facts_dir = trial_dir.parent / "facts"

    if facts_dir.is_dir():
        parquet_files = sorted(f.name for f in facts_dir.glob("*.parquet"))
        if parquet_files:
            has_coverage = "evidence_coverage.parquet" in parquet_files
            status = "analysis_ready" if has_coverage else "screening_only"
            return SemanticCoverageInspection(
                status=status,
                source=str(facts_dir.name),
                fact_tables=tuple(parquet_files),
                detail=f"{len(parquet_files)} semantic fact tables found in {facts_dir.name}/",
            )

    return SemanticCoverageInspection(
        status="unprojected",
        source="none",
        fact_tables=(),
        detail="No semantic fact projections found; relying on mechanical screening",
    )


def _analyze_intervention_provenance(outline: TrajectoryOutline) -> InterventionProvenance:
    """Analyze whether the agent executed autonomously or with intermediate human/supervisor steering."""
    user_steps = 0
    intermediate_user_turns = False
    system_steps = 0
    agent_steps = 0

    for i, step in enumerate(outline.steps):
        if step.source == "user":
            user_steps += 1
            # If user turn occurs after step index 1 (0-based) or after agent has acted, it is an intervention
            if i > 1 and agent_steps > 0:
                intermediate_user_turns = True
        elif step.source in ("system", "verifier", "setup"):
            system_steps += 1
        elif step.source in ("agent", "model"):
            agent_steps += 1
    if intermediate_user_turns:
        category = "user_assisted"
        summary = (
            f"User-assisted execution ({user_steps} user turns; intermediate steering detected)"
        )
    elif user_steps > 0:
        category = "autonomous"
        summary = (
            "Autonomous execution (initial task instruction only; no intermediate user steering)"
        )
    elif agent_steps > 0:
        category = "autonomous"
        summary = "Autonomous execution (zero human turns in trajectory)"
    else:
        category = "unknown"
        summary = "No agent execution steps recorded"

    return InterventionProvenance(
        category=category,
        user_steps_count=user_steps,
        system_steps_count=system_steps,
        agent_steps_count=agent_steps,
        has_intermediate_user_turns=intermediate_user_turns,
        summary=summary,
    )


def build_traj_card_data(
    target: str | Path,
    repo_root: Path,
    runs_roots: Sequence[Path] | None = None,
    policy: RedactionPolicy | None = None,
) -> TrajectoryCardData:
    """Build the complete structured data payload for a Trajectory Interpretation Card."""
    if policy is None:
        policy = RedactionPolicy()

    explicit_root = runs_roots[0] if runs_roots else None
    if explicit_root is None:
        try:
            target_path = Path(target)
            target_resolved = target_path.resolve()
            repo_resolved = repo_root.resolve()
            if target_resolved != repo_resolved and repo_resolved not in target_resolved.parents:
                explicit_root = (
                    target_resolved.parent if target_resolved.is_file() else target_resolved
                )
        except Exception:
            pass

    trial_dir, traj_path, result_path = resolve_trial_target(
        target, repo_root=repo_root, explicit_runs_root=explicit_root
    )

    # Outline trajectory
    outline = outline_trajectory(target, repo_root=repo_root, explicit_runs_root=explicit_root)

    # Compute baseline metrics
    baseline = compute_trace_baseline(outline)

    # Read result.json if present
    result_data: dict[str, Any] = {}
    result_sha: str | None = None
    if result_path and result_path.is_file():
        result_sha = sha256_file(result_path)
        try:
            result_data = json.loads(result_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            result_data = {}

    # Extract outcome and classify evidence limitations vs normal execution
    primary_reward = baseline.primary_reward
    exception_class = baseline.exception_class
    exception_message: str | None = None
    evidence_limitation: str | None = None

    if isinstance(result_data.get("exception_info"), dict):
        exception_message = result_data["exception_info"].get("exception_message")
    elif isinstance(result_data.get("exception"), dict):
        exception_message = result_data["exception"].get("message")

    if outline.status == "accounted_unavailable":
        unavail_reason = outline.unavailable_reason or "missing_trajectory_file"
        final_verdict = f"EVIDENCE_UNAVAILABLE ({unavail_reason})"
        evidence_limitation = f"Trajectory evidence is unavailable ({unavail_reason}). This represents a harness/evidence limitation, not a verified agent task execution."
    elif exception_class:
        if "Reward" in exception_class or "Verifier" in exception_class:
            final_verdict = f"VERIFIER_ERROR ({exception_class})"
            evidence_limitation = f"Verifier reward output is missing or crashed ({exception_class}). The evaluation harness could not establish a verified outcome."
        else:
            final_verdict = f"EXCEPTION ({exception_class})"
            evidence_limitation = f"Harness or runtime exception occurred ({exception_class})."
    elif primary_reward is not None:
        final_verdict = (
            "PASS"
            if primary_reward >= 1.0
            else ("FAIL" if primary_reward == 0.0 else f"PARTIAL ({primary_reward:.2f})")
        )
    else:
        final_verdict = "UNKNOWN"
        evidence_limitation = "No verifier reward or runtime exception was emitted."

    # Quality inspection
    quality = _inspect_quality_status(trial_dir)

    # Semantic coverage inspection
    semantic_coverage = _inspect_semantic_coverage(trial_dir)

    # Intervention provenance
    intervention = _analyze_intervention_provenance(outline)

    # Hydrate error observations
    error_evidences = tuple(hydrate_error_observations(trial_dir, outline, policy=policy))

    # Loop reasons list
    try:
        loop_reasons_list = tuple(json.loads(baseline.loop_reasons_json))
    except Exception:
        loop_reasons_list = ()

    return TrajectoryCardData(
        trial_id=baseline.trial_id,
        trial_name=baseline.trial_name,
        job_id=baseline.job_id,
        job_name=baseline.job_name,
        task_name=baseline.task_name,
        agent_name=baseline.agent_name,
        agent_version=baseline.agent_version,
        model_name=baseline.model_name,
        status=baseline.status,
        final_verdict=final_verdict,
        evidence_limitation=evidence_limitation,
        primary_reward=primary_reward,
        exception_class=exception_class,
        exception_message=exception_message,
        duration_seconds=baseline.duration_seconds,
        cost_usd=baseline.cost_usd,
        prompt_tokens=baseline.prompt_tokens,
        completion_tokens=baseline.completion_tokens,
        cached_tokens=baseline.cached_tokens,
        total_tokens=baseline.total_tokens,
        quality=quality,
        semantic_coverage=semantic_coverage,
        intervention=intervention,
        baseline_metrics=baseline,
        phases=outline.phases,
        error_evidences=error_evidences,
        loop_score=baseline.loop_suspicion_score,
        loop_detected=baseline.loop_suspicion_detected,
        loop_reasons=loop_reasons_list,
        trial_dir=str(trial_dir),
        trajectory_path=baseline.source_path,
        trajectory_sha256=baseline.source_sha256,
        result_path=result_path.name if result_path else None,
        result_sha256=result_sha,
    )


def render_traj_card_markdown(card: TrajectoryCardData) -> str:
    """Render a Trajectory Interpretation Card into deterministic Markdown."""
    lines: list[str] = []

    # Title & Header
    lines.append(f"# Trajectory Interpretation Card: {card.trial_name}")
    lines.append("")

    # Section 1: Identity & Final Outcome
    lines.append("## 1. Identity & Final Outcome")
    lines.append(f"- **Trial Name:** `{card.trial_name}`")
    lines.append(f"- **Task Name:** `{card.task_name}`")
    lines.append(f"- **Job ID / Name:** `{card.job_id}` ({card.job_name})")
    lines.append(
        f"- **Agent / Model:** `{card.agent_name}` (v: {card.agent_version}) | `{card.model_name}`"
    )

    reward_str = f"{card.primary_reward:.2f}" if card.primary_reward is not None else "none"
    lines.append(
        f"- **Final Verdict:** **{card.final_verdict}** (Primary Reward: `{reward_str}` | Trajectory Status: `{card.status}`)"
    )
    if card.evidence_limitation:
        lines.append(f"- **Evidence Limitation:** *{card.evidence_limitation}*")
    if card.exception_class:
        msg_str = f" — {card.exception_message}" if card.exception_message else ""
        lines.append(f"- **Exception Details:** `{card.exception_class}`{msg_str}")
    else:
        lines.append("- **Exception Details:** none")
    # Quality status
    q_reasons_str = f" ({', '.join(card.quality.reasons)})" if card.quality.reasons else ""
    lines.append(
        f"- **Quality Status:** `{card.quality.status}`{q_reasons_str} — *{card.quality.detail}*"
    )

    # Duration & Tokens & Cost
    dur_str = f"{card.duration_seconds:.1f}s" if card.duration_seconds is not None else "n/a"
    cost_str = f"${card.cost_usd:.4f}" if card.cost_usd is not None else "n/a"
    tok_str = f"{card.total_tokens:,}" if card.total_tokens is not None else "n/a"
    lines.append(
        f"- **Execution Telemetry:** Duration: `{dur_str}` | Cost: `{cost_str}` | Tokens: `{tok_str}`"
    )
    lines.append("")

    # Section 2: Execution Phases Outline
    lines.append("## 2. Execution Phases")
    if card.phases:
        lines.append("| Phase | Steps (Span) | Tool Calls | Errors | Summary |")
        lines.append("|---|---|---|---|---|")
        for p in card.phases:
            step_span = (
                f"{p.step_start}..{p.step_end} ({p.step_count})"
                if p.step_start != p.step_end
                else f"{p.step_start} (1)"
            )
            lines.append(
                f"| `{p.phase_type}` | {step_span} | {p.tool_calls} | {p.errors} | {p.summary} |"
            )
    else:
        lines.append("*No ordered semantic phases identified.*")
    lines.append("")

    # Section 3: Mechanical Baseline Metrics
    b = card.baseline_metrics
    lines.append("## 3. Mechanical Baseline Metrics")
    lines.append("| Metric | Value | Provenance Category / Screening Semantics |")
    lines.append("|---|---|---|")
    lines.append(
        f"| **Steps (total / agent / sys / user)** | {b.step_count} ({b.agent_step_count} / {b.system_step_count} / {b.user_step_count}) | `mechanical_fact` (exact step counts) |"
    )
    lines.append(
        f"| **Tool Calls (total / unique)** | {b.tool_call_count} ({b.unique_tools_count}) | `mechanical_fact` (exact tool call counts) |"
    )
    lines.append(
        f"| **Errors / Recoveries** | {b.error_count} / {b.recovery_count} | `mechanical_fact` (exact execution error counts) |"
    )
    neg_label = "YES (Control)" if b.is_expected_negative else "NO"
    lines.append(
        f"| **Expected Negative / Probes** | {neg_label} ({b.expected_probe_count} probes) | `mechanical_fact` (intentional negative control screening) |"
    )
    first_err_s = f"Step {b.step_to_first_error}" if b.step_to_first_error is not None else "none"
    first_err_t = (
        f"{b.time_to_first_error_seconds:.1f}s"
        if b.time_to_first_error_seconds is not None
        else "none"
    )
    lines.append(
        f"| **Step / Time to First Error** | {first_err_s} ({first_err_t}) | `mechanical_fact` (first error latency) |"
    )
    rec_lat_s = (
        f"{b.recovery_latency_steps} steps" if b.recovery_latency_steps is not None else "none"
    )
    rec_lat_t = (
        f"{b.recovery_latency_seconds:.1f}s" if b.recovery_latency_seconds is not None else "none"
    )
    lines.append(
        f"| **Recovery Latency** | {rec_lat_s} ({rec_lat_t}) | `mechanical_fact` (step/time delay to recovery) |"
    )
    term_err_lbl = "UNRECOVERED ERROR" if b.unrecovered_at_terminal else "Recovered / Clean"
    lines.append(
        f"| **Terminal Error State** | {term_err_lbl} | `mechanical_fact` (active error at final step) |"
    )
    li_val = (
        f"{b.linear_innocence_screening:.4f}"
        if b.linear_innocence_screening is not None
        else "NULL (0 tool calls)"
    )
    lines.append(
        f"| **Linearity Index (`LI_screening`)** | {li_val} | `screening_heuristic` (unique_tools / tool_calls) |"
    )
    ter_val = (
        f"{b.tool_error_rate_screening:.4f}"
        if b.tool_error_rate_screening is not None
        else "NULL (0 tool calls)"
    )
    lines.append(
        f"| **Tool Error Rate (`TER_screening`)** | {ter_val} | `screening_heuristic` (errors / tool_calls) |"
    )
    rec_rate_val = (
        f"{b.recovery_rate_screening:.4f}"
        if b.recovery_rate_screening is not None
        else "NULL (0 errors)"
    )
    lines.append(
        f"| **Recovery Rate (`recovery_rate_screening`)** | {rec_rate_val} | `screening_heuristic` (recoveries / errors) |"
    )
    cbv_val = (
        f"{b.context_burn_velocity_screening:+.2f} tok/step"
        if b.context_burn_velocity_screening is not None
        else "NULL (<2 observations)"
    )
    lines.append(
        f"| **Context Burn Velocity (`CBV_screening`)** | {cbv_val} | `screening_heuristic` (regr_slope prompt_tokens over steps) |"
    )
    cache_val = (
        f"{b.cache_hit_rate_screening * 100:.1f}%"
        if b.cache_hit_rate_screening is not None
        else "NULL (no cached tokens)"
    )
    lines.append(
        f"| **Cache Hit Rate (`cache_hit_rate_screening`)** | {cache_val} | `screening_heuristic` (cached / prompt) |"
    )
    sub_val = (
        f"{b.subagent_overhead_ratio_screening * 100:.1f}%"
        if b.subagent_overhead_ratio_screening is not None
        else "NULL (0 steps)"
    )
    lines.append(
        f"| **Subagent Overhead (`subagent_overhead_screening`)** | {sub_val} | `screening_heuristic` (subagent_steps / total_steps) |"
    )
    auto_val = (
        f"{b.autonomous_step_ratio_screening * 100:.1f}%"
        if b.autonomous_step_ratio_screening is not None
        else "NULL (0 steps)"
    )
    lines.append(
        f"| **Autonomous Ratio (`autonomous_step_ratio_screening`)** | {auto_val} | `screening_heuristic` (autonomous / total) |"
    )
    asst_val = (
        f"{b.assisted_step_ratio_screening * 100:.1f}%"
        if b.assisted_step_ratio_screening is not None
        else "NULL (0 steps)"
    )
    lines.append(
        f"| **Assisted Ratio (`assisted_step_ratio_screening`)** | {asst_val} | `screening_heuristic` (assisted / total) |"
    )
    lines.append("")
    # Section 4: Cited Error Observations & Stderr
    lines.append("## 4. Cited Error Observations & Stderr")
    if card.error_evidences:
        for idx, ev in enumerate(card.error_evidences, 1):
            cit_str = ev.citation.format_citation()
            lines.append(f"### Error #{idx}: Step {ev.citation.step_index}")
            lines.append(f"- **Citation:** `{cit_str}`")
            lines.append(f"- **Content Digest:** `{ev.content_sha256}` ({ev.content_bytes} bytes)")
            if ev.is_redacted:
                lines.append(f"- **Redaction:** Applied on-read ({ev.redaction_metadata})")
            lines.append("```")
            lines.append(ev.redacted_content.strip())
            lines.append("```")
            lines.append("")
    else:
        lines.append("No tool or command execution errors recorded in trajectory.")
        lines.append("")

    # Section 5: Loop & Cascade Detection
    lines.append("## 5. Loop & Cascade Reason Codes")
    loop_det_str = "DETECTED" if card.loop_detected else "not detected"
    lines.append(f"- **Loop Suspicion:** **{loop_det_str}** (Score: `{card.loop_score:.2f}`)")
    if card.loop_reasons:
        lines.append("- **Triggered Reason Codes:**")
        for r in card.loop_reasons:
            lines.append(f"  - `{r}`")
    else:
        lines.append("- **Triggered Reason Codes:** none")
    lines.append(
        f"- **Max Exit-Code Cascade:** `{b.max_exit_code_cascade_screening}` consecutive error steps"
    )
    lines.append("")

    # Section 6: Intervention Provenance
    lines.append("## 6. Intervention Provenance")
    lines.append(f"- **Intervention Category:** `{card.intervention.category}`")
    lines.append(f"- **Summary:** {card.intervention.summary}")
    lines.append(
        f"- **Turn Breakdown:** Autonomous agent steps: `{b.autonomous_step_count}` | Assisted steps: `{b.assisted_step_count}` | Post-error interventions: `{b.intervention_count}`"
    )
    # Section 7: Semantic Coverage
    lines.append("## 7. Semantic Coverage")
    lines.append(f"- **Coverage Status:** `{card.semantic_coverage.status}`")
    lines.append(f"- **Details:** {card.semantic_coverage.detail}")
    if card.semantic_coverage.fact_tables:
        lines.append(
            "- **Projected Fact Tables:** "
            + ", ".join(f"`{t}`" for t in card.semantic_coverage.fact_tables)
        )
    lines.append("")

    # Section 8: Source Citations & Provenance
    lines.append("## 8. Source Citations & Exact Provenance")
    lines.append(f"- **Trial Directory:** `{card.trial_dir}`")
    lines.append(
        f"- **Trajectory File:** `{card.trajectory_path}` (SHA-256: `{card.trajectory_sha256}`)"
    )
    if card.result_path:
        lines.append(
            f"- **Result File:** `{card.result_path}` (SHA-256: `{card.result_sha256 or 'n/a'}`)"
        )
    lines.append("")

    return "\n".join(lines)


def generate_traj_card(
    target: str | Path,
    repo_root: Path,
    runs_roots: Sequence[Path] | None = None,
    output_path: Path | None = None,
    output_format: str = "markdown",
    policy: RedactionPolicy | None = None,
) -> tuple[str, TrajectoryCardData]:
    """Generate a Trajectory Interpretation Card, optionally write it to disk, and return formatted content."""
    card_data = build_traj_card_data(
        target, repo_root=repo_root, runs_roots=runs_roots, policy=policy
    )

    if output_format == "json":
        rendered = json.dumps(card_data.to_dict(), indent=2)
    else:
        rendered = render_traj_card_markdown(card_data)

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")

    return rendered, card_data
