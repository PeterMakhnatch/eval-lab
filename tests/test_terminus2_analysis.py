"""Terminus 2 analysis acceptance (HAR-70 analysis slice).

Compact deterministic fixtures under ``tests/fixtures/terminus2`` mirror the actual
upstream Terminus 2 native ATIF output (``harbor/agents/terminus_2/terminus_2.py``):

- root agent identity ``terminus-2`` / ``2.0.0`` with the configured model, while
  per-step ``model_name`` carries the returned model;
- text-command tool calls (``bash_command`` / ``mark_task_complete``) with
  single-command observations linked via ``source_call_id`` and multi-command
  observations left unlinked;
- a parse-error agent step that still carries metrics and an observation;
- a system summarization step whose observation references three file-ref child
  trajectories; each child carries copied parent steps with metrics omitted
  (``is_copied_context``) plus one response step with that call's usage;
- the parent ``final_metrics`` already folds summarizer usage into the native
  totals (``AgentContext`` = main chat + ``SubagentMetrics``), so consumers treat
  the declared totals as authoritative for the NATIVE ledger and never sum
  ``final_metrics`` across parent/child/continuation documents;
- a ``continued_trajectory_ref`` chain for the split-history mode, stitched so
  earlier non-copied actions survive alongside the terminal aggregates;
- a failure dump (``_dump_trajectory`` in ``run()``'s ``finally``) that stays a
  parseable, exception-bearing trace and never a verified pass.

Native-ledger limit (stated, not solved): upstream raises on length-truncated
calls before usage is recorded, so such physical calls can be absent from both
step metrics and native totals. The physical invoice lives in the separate proxy
ledger; nothing here infers one ledger from the other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.evidence.atif import project_trial
from evallab.evidence.facts import extract_job_facts, extract_trial_fact
from evallab.explorer import _trajectory_view
from evallab.interpretation.trace_readiness import check_trace_readiness
from evallab.interpretation.trajectory_ir import build_trajectory_ir
from evallab.results import load_job
from evallab.traj import outline_trajectory, render_outline

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "terminus2"
FIXTURE_JOB = FIXTURE_ROOT / "job-terminus2"

CONFIGURED_MODEL = "zai/glm-5.3-flash"
RETURNED_MODEL = "glm-5.3-flash-20260901"


def _trial(name: str) -> Path:
    return FIXTURE_JOB / name


def _step_token_sums(outline) -> tuple[int, int]:
    prompt = sum(step.prompt_tokens or 0 for step in outline.steps)
    completion = sum(step.completion_tokens or 0 for step in outline.steps)
    return prompt, completion


def test_outline_prefers_declared_native_totals() -> None:
    """Summarizer usage folded into final_metrics must not be understated."""
    outline = outline_trajectory(_trial("trial-summarized"), repo_root=FIXTURE_ROOT)

    assert outline.status == "featured"
    assert outline.agent_name == "terminus-2"
    assert outline.agent_version == "2.0.0"
    assert outline.model_name == CONFIGURED_MODEL
    # Per-step model records the returned model, distinct from the configured one.
    assert outline.steps[1].model_name == RETURNED_MODEL

    # Authoritative native totals (main chat + summarizer subagents).
    assert outline.total_prompt_tokens == 18150
    assert outline.total_completion_tokens == 1720
    assert outline.total_cached_tokens == 4500
    assert outline.total_cost_usd == pytest.approx(0.0146)
    assert outline.final_metrics_present is True
    # Attribution provenance stays computable from the step array itself.
    assert _step_token_sums(outline) == (7350, 650)
    # No redundant aggregate fields beyond the existing authority convention.
    assert not hasattr(outline, "step_attributed_prompt_tokens")
    assert not hasattr(outline, "continued_trajectory_ref")

    assert outline.total_steps == 8
    assert outline.agent_steps == 5
    assert outline.system_steps == 1
    assert outline.user_steps == 2
    assert outline.total_tool_calls == 5
    assert outline.tool_mix == {"bash_command": 4, "mark_task_complete": 1}
    assert all(step.is_copied_context is False for step in outline.steps)

    # The parse-error step keeps its metrics and observation without tool calls.
    parse_step = outline.steps[2]
    assert parse_step.tool_name is None
    assert parse_step.prompt_tokens == 1350

    rendered = render_outline(outline)
    assert "METRICS SUMMARY:" in rendered
    assert "Unattributed (native ledger):" in rendered


def test_children_cover_copied_context_without_double_count() -> None:
    """Parent final = step-attributed main calls + one usage record per child."""
    job = load_job(FIXTURE_JOB)
    trial = next(t for t in job.trials if t.path.name == "trial-summarized")
    projection = project_trial(job, trial)

    assert {t.validation_status for t in projection.trajectories} == {"valid"}
    assert len(projection.trajectories) == 4  # parent + 3 summarizer children

    by_source = {t.source_path: t for t in projection.trajectories}
    parent = by_source["agent/trajectory.json"]
    assert parent.agent_name == "terminus-2"
    assert parent.agent_version == "2.0.0"
    assert parent.model_name == CONFIGURED_MODEL
    assert parent.continued_trajectory_ref is None
    assert parent.prompt_tokens == 18150
    assert parent.completion_tokens == 1720
    assert parent.cached_tokens == 4500
    assert parent.cost_usd == pytest.approx(0.0146)

    children = [t for path, t in by_source.items() if path != "agent/trajectory.json"]
    assert len(children) == 3
    child_prompt = sorted(t.prompt_tokens for t in children)
    assert child_prompt == [1500, 4200, 5100]

    parent_step_prompt = sum(
        s.prompt_tokens
        for s in projection.steps
        if s.source_path == "agent/trajectory.json" and s.prompt_tokens is not None
    )
    assert parent_step_prompt == 7350
    # Each physical call is recorded exactly once across documents; the parent
    # final already folds them in, so summing finals across documents overcounts.
    assert parent_step_prompt + sum(child_prompt) == parent.prompt_tokens
    assert sum(t.prompt_tokens or 0 for t in projection.trajectories) == 28950
    assert sum(t.prompt_tokens or 0 for t in projection.trajectories) != parent.prompt_tokens

    # Copied context carries no metrics, so it can never double-count usage.
    copied = [s for s in projection.steps if s.is_copied_context]
    assert copied, "expected copied-context steps in summarizer children"
    assert all(s.prompt_tokens is None for s in copied)
    assert all(s.source_path != "agent/trajectory.json" for s in copied)

    # The summarization delegation step references all three children.
    delegation = [
        o
        for o in projection.observations
        if o.source_path == "agent/trajectory.json" and o.subagent_ref_count == 3
    ]
    assert len(delegation) == 1

    # Native tool/observation shapes survive projection.
    functions = {
        c.function_name
        for c in projection.tool_calls
        if c.source_path == "agent/trajectory.json"
    }
    assert functions == {"bash_command", "mark_task_complete"}
    parent_observations = [
        o for o in projection.observations if o.source_path == "agent/trajectory.json"
    ]
    assert any(o.source_call_id is None for o in parent_observations)  # multi-command
    assert any(o.source_call_id is not None for o in parent_observations)  # linked
    parse_step = next(
        s
        for s in projection.steps
        if s.source_path == "agent/trajectory.json" and s.step_id == 3
    )
    assert parse_step.tool_call_count == 0
    assert parse_step.observation_count == 1


def test_trial_fact_counts_exclude_copied_duplicates() -> None:
    """Copied steps are not new actions; every document stays addressable."""
    job = load_job(FIXTURE_JOB)
    summarized = next(t for t in job.trials if t.path.name == "trial-summarized")
    fact = extract_trial_fact(job, summarized)

    # 8 parent actions + 2 live steps per child (prompt + response); the 12
    # copied steps are excluded, and the 4 projected documents are all retained.
    assert fact.step_count == 14
    assert fact.tool_call_count == 5
    assert fact.trajectory_count == 4
    assert fact.invalid_trajectory_count == 0
    assert fact.input_tokens == 18150
    assert fact.output_tokens == 1720
    assert fact.cache_tokens == 4500
    assert fact.cost_usd == pytest.approx(0.0146)

    usage = {
        row.function_name: row.call_count
        for row in extract_job_facts(job).tool_usage
        if row.trial_id == fact.trial_id
    }
    assert usage == {"bash_command": 4, "mark_task_complete": 1}


def test_continuation_stitch_preserves_earlier_actions() -> None:
    """Stitched outline keeps pre-split actions with terminal aggregates."""
    outline = outline_trajectory(_trial("trial-continued"), repo_root=FIXTURE_ROOT)

    assert outline.status == "featured"
    assert outline.source_path.endswith("agent/trajectory.cont-1.json")
    # Earlier non-copied segment-0 actions survive alongside terminal work.
    assert outline.total_steps == 6
    assert [step.step_id for step in outline.steps] == [1, 2, 3, 4, 5, 6]
    assert all(step.is_copied_context is False for step in outline.steps)
    assert outline.tool_mix == {"bash_command": 3, "mark_task_complete": 1}
    assert outline.total_tool_calls == 4
    # Terminal cumulative totals, not segment finals summed (7500).
    assert outline.total_prompt_tokens == 4500
    assert outline.total_completion_tokens == 490
    assert outline.total_cached_tokens == 600
    assert outline.total_cost_usd == pytest.approx(0.0029)
    assert _step_token_sums(outline) == (4500, 490)
    cited = [c.path for c in outline.citations if c.kind == "trajectory"]
    assert len(cited) == 2
    assert cited[0].endswith("agent/trajectory.json")
    assert cited[1].endswith("agent/trajectory.cont-1.json")


def test_continuation_fact_and_readiness_agree_with_outline() -> None:
    """Facts, readiness, and explorer resolve the same chain, not stale roots."""
    job = load_job(FIXTURE_JOB)
    continued = next(t for t in job.trials if t.path.name == "trial-continued")

    fact = extract_trial_fact(job, continued)
    assert fact.step_count == 6
    assert fact.tool_call_count == 4
    assert fact.input_tokens == 4500
    assert fact.output_tokens == 490
    assert fact.cache_tokens == 600

    readiness = check_trace_readiness(_trial("trial-continued"), repo_root=FIXTURE_ROOT)
    assert readiness["steps"] == 6
    assert readiness["prompt_tokens"] == 4500
    assert readiness["verdict"] in {"interpretable", "degraded"}

    view = _trajectory_view(_trial("trial-continued"))
    assert [s.step_id for s in view.steps] == [1, 2, 3, 4, 5, 6]
    assert len(view.tool_calls) == 4

    report = check_trace_readiness(_trial("trial-summarized"), repo_root=FIXTURE_ROOT)
    assert report["steps"] == 8
    assert report["prompt_tokens"] == 18150


def test_continuation_events_span_both_segments() -> None:
    """Interpretation events pair raw calls from each segment, not just the root."""
    trial_dir = _trial("trial-continued")
    ir = build_trajectory_ir(trial_dir, repo_root=FIXTURE_ROOT)

    programs = {event.status_owning_program for event in ir.events}
    assert "bash_command" in programs
    assert "mark_task_complete" in programs
    bash_steps = {
        event.step_id for event in ir.events if event.status_owning_program == "bash_command"
    }
    assert {2, 3, 5} <= bash_steps
    assert {
        event.step_id
        for event in ir.events
        if event.status_owning_program == "mark_task_complete"
    } == {6}


def test_failure_dump_is_parseable_but_never_a_pass() -> None:
    """A failure-dump trajectory keeps its exception and earns no reward."""
    outline = outline_trajectory(_trial("trial-failed"), repo_root=FIXTURE_ROOT)

    assert outline.status == "featured"
    assert outline.exception_class == "TimeoutError"
    assert outline.primary_reward is None
    assert outline.total_prompt_tokens == 2400
    assert outline.total_completion_tokens == 200
    assert outline.total_cached_tokens == 100
    assert outline.total_cost_usd == pytest.approx(0.0016)
