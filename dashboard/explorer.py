"""Read-only M035 GYM-UI trajectory, truth, and analyst explorer.

Launch: ``uv run --with streamlit==1.61.1 streamlit run dashboard/explorer.py``.
The page is a thin Streamlit presentation over ``evallab.explorer``; it never
executes commands, mutates evidence, or reads task tests/solutions.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

import streamlit as st

from evallab.explorer import (
    CitationResolution,
    ExplorerIndex,
    Labeled,
    StoredAnalysisView,
    build_index,
    citation_state,
    content_summary,
    next_actions_for_queue,
    next_actions_for_trial,
    redact_mapping,
    redact_text,
)
from evallab.traj import TrajectoryOutline

_BADGE = {
    "observed": "observed",
    "derived": "derived",
    "draft": "draft (analysis/inference; not truth)",
    "withheld": "withheld (redacted before promotion)",
    "unavailable": "unavailable",
}
_CONTENT_ICON = {
    "observed": "readable",
    "derived": "derived",
    "withheld": "withheld",
    "unavailable": "unavailable",
}
_CITATION_ICON = {
    "readable": "readable",
    "withheld": "withheld",
    "absent": "absent",
    "unresolved": "unresolved",
}


def _root() -> Path:
    configured = os.environ.get("EVALLAB_EXPLORER_ROOT")
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[1]


@st.cache_data(ttl=30, show_spinner=False)
def _index(root_value: str) -> ExplorerIndex:
    root = Path(root_value).resolve()
    if (root / "jobs").is_dir():
        return build_index(
            [root / "jobs"],
            root / "analyses",
            repo_root=root,
            review_queue_limit=3,
        )
    return build_index(
        [root / "runs", root / "research" / "evidence" / "runs"],
        root / "derived" / "analyses",
        root / "library" / "registry",
        repo_root=root,
        analyst_dir=root / "research" / "analysis",
        review_queue_limit=3,
    )


def _safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_safe_value(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def _labeled(container: Any, name: str, labeled: Labeled | None) -> None:
    if labeled is None:
        container.markdown(f"**{name}**: unavailable — not recorded")
        return
    badge = _BADGE.get(labeled.provenance, labeled.provenance)
    if labeled.provenance == "unavailable":
        container.markdown(f"**{name}**: {badge} — _{labeled.reason}_")
    else:
        note = f" · _{redact_text(labeled.reason)}_" if labeled.reason else ""
        container.markdown(f"**{name}**: `{_safe_value(labeled.value)}` — {badge}{note}")


def _content_cell(labeled: Labeled) -> str:
    return f"{_CONTENT_ICON.get(labeled.provenance, 'unavailable')} · {content_summary(labeled)}"


def _citation_line(citation: CitationResolution) -> tuple[str, str | None]:
    state = citation_state(citation)
    detail = (
        f"unresolved: {citation.resolution.reason}"
        if state == "unresolved"
        else None
        if state == "readable"
        else citation.content.reason
    )
    line = (
        f"{_CITATION_ICON[state]} `{redact_text(citation.citation_path)}`"
        f" step={citation.step_id} call={citation.tool_call_id}"
        f" — {redact_text(citation.supports)}"
        f" · {content_summary(citation.content)}"
    )
    return line, detail


def _outline_metrics(outline: TrajectoryOutline) -> list[dict[str, Any]]:
    return [
        {"metric": "status", "value": outline.status},
        {"metric": "steps", "value": str(outline.total_steps)},
        {"metric": "tool calls", "value": str(outline.total_tool_calls)},
        {
            "metric": "errors / recoveries",
            "value": f"{outline.total_errors} / {outline.recovery_count}",
        },
        {
            "metric": "duration",
            "value": "" if outline.duration_seconds is None else f"{outline.duration_seconds:.3f}s",
        },
        {
            "metric": "prompt / completion tokens",
            "value": f"{outline.total_prompt_tokens:,} / {outline.total_completion_tokens:,}",
        },
        {"metric": "cached tokens", "value": str(outline.total_cached_tokens)},
        {"metric": "cost USD", "value": f"{outline.total_cost_usd:.6f}"},
        {
            "metric": "first tool",
            "value": "" if outline.step_to_first_tool is None else str(outline.step_to_first_tool),
        },
        {
            "metric": "first edit",
            "value": "" if outline.step_to_first_edit is None else str(outline.step_to_first_edit),
        },
    ]


def _render_outline(outline: TrajectoryOutline) -> None:
    if outline.status != "featured":
        st.warning(
            "Trajectory unavailable. This is an accounted absence, not an empty successful trace: "
            f"{outline.unavailable_reason or 'reason not recorded'}."
        )
        return

    st.dataframe(_outline_metrics(outline), hide_index=True, width="stretch")
    st.subheader("Ordered phases")
    st.dataframe(
        [
            {
                "phase": phase.name,
                "type": phase.phase_type,
                "steps": f"{phase.step_start}–{phase.step_end}",
                "step count": phase.step_count,
                "tools": phase.tool_calls,
                "errors": phase.errors,
                "tokens": phase.prompt_tokens + phase.completion_tokens,
                "cost USD": phase.cost_usd,
                "summary": phase.summary,
            }
            for phase in outline.phases
        ],
        hide_index=True,
        width="stretch",
    )

    st.subheader("Step highlights and tool calls")
    st.dataframe(
        [
            {
                "step": step.step_id,
                "source": step.source,
                "timestamp": step.timestamp or "",
                "tool": redact_text(step.tool_name or ""),
                "command": redact_text((step.tool_command or "")[:160]),
                "exit": "" if step.exit_code is None else str(step.exit_code),
                "error": redact_text(step.error_message or "") if step.is_error else "",
                "highlight": "redacted prompt text withheld"
                if step.is_redacted
                else redact_text(step.thought_snippet or ""),
                "tokens": f"{step.prompt_tokens or 0} / {step.completion_tokens or 0}",
                "cost USD": "" if step.cost_usd is None else f"{step.cost_usd:.6f}",
            }
            for step in outline.steps
        ],
        hide_index=True,
        width="stretch",
    )
    loop = outline.loop_suspicion
    st.markdown(
        f"**Loop suspicion (derived heuristic):** score `{loop.score:.2f}`, "
        f"detected `{loop.detected}`, repeated commands `{loop.repeated_command_count}`, "
        f"repeated errors `{loop.repeated_error_count}`, "
        f"cyclic patterns `{loop.cyclic_patterns_count}`."
    )
    if loop.reasons:
        st.caption("; ".join(redact_text(reason) for reason in loop.reasons))

    st.subheader("Source citations")
    st.dataframe(
        [
            {"kind": citation.kind, "path": redact_text(citation.path), "sha256": citation.sha256}
            for citation in outline.citations
        ],
        hide_index=True,
        width="stretch",
    )


def _render_truth(trial: Any) -> None:
    st.info(
        "Truth panel: observed verifier/result evidence. Derived outcome labels are "
        "marked; analyst conclusions do not appear here."
    )
    left, right = st.columns(2)
    for name, labeled in (
        ("Task", trial.task_name),
        ("Agent", trial.agent),
        ("Model", trial.model),
        ("Primary reward", trial.reward),
        ("Outcome class", trial.outcome_class),
    ):
        _labeled(left, name, labeled)
    for name, labeled in (
        ("Exit code", trial.exit_code),
        ("Exception", trial.exception),
        ("Timing", trial.timing),
        ("Cost", trial.cost),
    ):
        _labeled(right, name, labeled)

    st.subheader("Verifier outputs")
    _labeled(st, "Verifier result", trial.verifier_output)
    _labeled(st, "Reward dimensions", trial.reward_dimensions)
    if trial.artifacts:
        st.subheader("Artifact links")
        st.dataframe(
            [
                {
                    "name": a.name,
                    "path": a.relative_path,
                    "bytes": a.size_bytes,
                    "content": _content_cell(a.content),
                }
                for a in trial.artifacts
            ],
            hide_index=True,
            width="stretch",
        )
    if trial.omitted_files.provenance == "withheld":
        st.warning(f"Withheld artifacts: {trial.omitted_files.reason}")


def _render_stored_analysis(analysis: StoredAnalysisView) -> None:
    header = analysis.trial_key or "source trial unavailable"
    with st.expander(f"{analysis.analysis_id} → {header}"):
        _labeled(st, "Source trial", analysis.link)
        _labeled(st, "Category", analysis.category)
        _labeled(st, "Conclusion", analysis.summary)
        _labeled(st, "Confidence", analysis.confidence)
        _labeled(st, "Provenance", analysis.provenance)
        _labeled(st, "Reasoning transcript artifact", analysis.transcript)
        st.caption(
            "The transcript field is a recorded artifact reference only; the "
            "explorer never synthesizes hidden chain-of-thought."
        )
        st.markdown("**Evidence citations**")
        for citation in analysis.citations:
            line, detail = _citation_line(citation)
            st.markdown(line)
            if detail:
                st.caption(detail)


def _render_legacy_analysis(analysis: Any) -> None:
    header = analysis.trial_key or "source trial unavailable"
    with st.expander(f"Sidecar {analysis.analysis_id} → {header}"):
        _labeled(st, "Source trial", analysis.link)
        _labeled(st, "Validation status", analysis.status)
        _labeled(st, "Validity", analysis.validity)
        _labeled(st, "Category", analysis.category)
        _labeled(st, "Summary", analysis.summary)
        _labeled(st, "Confidence", analysis.confidence)
        _labeled(st, "Alternatives", analysis.alternatives)
        _labeled(st, "Provenance", analysis.provenance)
        for citation in analysis.citations:
            line, detail = _citation_line(citation)
            st.markdown(line)
            if detail:
                st.caption(detail)


st.set_page_config(page_title="Eval Lab — Explorer", page_icon="E", layout="wide")
st.title("Run & analysis explorer")
st.caption(
    "Read-only GYM-UI. Trajectory mechanics are deterministic M030 facts; truth "
    "is kept separate from analyst inference. Missing evidence is unavailable "
    "with a reason, not an empty success."
)

root = _root()
index = _index(str(root))
for note in index.notes:
    st.warning(note)

trajectory_tab, truth_tab, analyst_tab = st.tabs(
    ["Trajectory browser", "Truth panel", "Analyst panel"]
)
trial_keys = sorted(index.trials)
queue_keys = [item.job_name + "/" + item.trial_name for item in index.review_queue]
options = queue_keys + [key for key in trial_keys if key not in queue_keys]

with trajectory_tab:
    st.header("Trajectory browser")
    st.caption(
        "Review queue is deterministic, excludes oracle/nop controls, and does not record a label."
    )
    if index.review_queue:
        st.dataframe(
            [asdict(item) for item in index.review_queue], hide_index=True, width="stretch"
        )
    else:
        st.info("No unread real-agent trajectories are available for review.")
    if not options:
        st.info("No trials found under the configured roots.")
    else:
        selected_key = st.selectbox("Select trial", options, index=0)
        trial = index.trials[selected_key]
        st.subheader(selected_key)
        outline = trial.trajectory_outline
        if outline is None:
            st.warning("M030 trajectory outline unavailable; no outline was synthesized.")
        else:
            _render_outline(outline)
        if trial.trajectory_fallback is not None:
            _labeled(st, "AGY fallback", trial.trajectory_fallback)
        for action in next_actions_for_trial(trial):
            st.caption(action.label)
            st.code(action.command, language="bash")

with truth_tab:
    st.header("Truth panel")
    if not options:
        st.info("No trial truth records are available.")
    else:
        selected_truth = st.selectbox("Select trial for truth", options, key="truth_trial")
        _render_truth(index.trials[selected_truth])

with analyst_tab:
    st.header("Analyst panel")
    st.caption(
        "Stored conclusions and provenance are analysis/inference, never verifier "
        "truth. No hidden chain-of-thought is inferred."
    )
    if index.analyst_analyses:
        st.subheader("Stored analyst conclusions")
        for analysis in index.analyst_analyses:
            _render_stored_analysis(analysis)
    else:
        st.info("No stored analyst conclusions are available.")
    if index.analyses:
        st.subheader("Validated analysis sidecars")
        for analysis in index.analyses:
            _render_legacy_analysis(analysis)
    for action in next_actions_for_queue():
        st.caption(action.label)
        st.code(action.command, language="bash")
