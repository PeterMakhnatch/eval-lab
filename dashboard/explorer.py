"""Run & analysis explorer page (M005). Read-only, always.

Launch:  uv run streamlit run dashboard/explorer.py
Root:    EVALLAB_EXPLORER_ROOT (defaults to the repository root; a fixture
         root such as tests/fixtures/explorer works for a safe demo).

Every field renders with its provenance label (observed / derived / draft /
unavailable). Infrastructure exceptions render in their own section, never
alongside reward failures. Next Action shows copyable commands and executes
nothing. No control in this page mutates any state anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from evallab.explorer import (
    ExplorerIndex,
    TrajectoryView,
    build_index,
    next_actions_for_queue,
    next_actions_for_task,
    next_actions_for_trial,
)

_BADGE = {
    "observed": "🟢 observed",
    "derived": "🔵 derived",
    "draft": "🟡 draft (unreviewed model output)",
    "unavailable": "⚪ unavailable",
}


def _root() -> Path:
    configured = os.environ.get("EVALLAB_EXPLORER_ROOT")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1]


@st.cache_data(ttl=30, show_spinner=False)
def _index(root_value: str) -> ExplorerIndex:
    root = Path(root_value)
    if (root / "jobs").is_dir():  # fixture/scratch layout
        return build_index([root / "jobs"], root / "analyses")
    return build_index(
        [root / "runs", root / "research" / "evidence" / "runs"],
        root / "derived" / "analyses",
        root / "library" / "registry",
    )


def _labeled(container, name, labeled) -> None:
    badge = _BADGE.get(labeled.provenance, labeled.provenance)
    if labeled.provenance == "unavailable":
        container.markdown(f"**{name}** — {badge}: _{labeled.reason}_")
    else:
        note = f" · _{labeled.reason}_" if labeled.reason else ""
        container.markdown(f"**{name}**: `{labeled.value}` — {badge}{note}")


st.set_page_config(page_title="Eval Lab — Explorer", page_icon="🧭", layout="wide")
st.title("Run & analysis explorer")
st.caption(
    "Read-only. Every value is labeled observed / derived / draft / unavailable. "
    "Commands are copyable, never executed."
)

root = _root()
index = _index(str(root))

for note in index.notes:
    st.warning(note)

tasks_tab, trials_tab, analyses_tab = st.tabs(["Tasks", "Jobs & trials", "Analyses"])

with tasks_tab:
    if not index.tasks:
        st.info("No tasks with readable evidence yet. Cold start is a valid state.")
    for task in index.tasks:
        with st.expander(f"📋 {task.task_name}", expanded=False):
            _labeled(st, "Registration", task.registration)
            _labeled(st, "Controls with evidence here", task.control_state)
            st.markdown("**Trials:** " + ", ".join(task.trial_keys))
            st.markdown("**Next action** (copy, review, run yourself):")
            for action in next_actions_for_task(task.task_name):
                st.code(action.command, language="bash")

with trials_tab:
    if not index.trials:
        st.info("No trials found under the configured roots.")
    if index.jobs:
        st.subheader("Jobs")
        for job in index.jobs:
            with st.expander(f"📦 {job.job_name}"):
                _labeled(st, "Tasks", job.task_names)
                st.markdown("**Trials — observed:** " + ", ".join(job.trial_keys))
                for note in job.notes:
                    st.warning(note)
    infra = [t for t in index.trials.values()
             if t.outcome_class.value == "infra-exception"]
    scored = [t for t in index.trials.values()
              if t.outcome_class.value != "infra-exception"]
    if infra:
        st.subheader("⚠️ Infrastructure exceptions (not scores)")
        st.caption("These trials failed before a verdict. They are evidence about the "
                   "harness, never about the model.")
        for trial in infra:
            with st.expander(f"🔌 {trial.trial_key}"):
                _labeled(st, "Exception", trial.exception)
                _labeled(st, "Agent", trial.agent)
                for action in next_actions_for_trial(trial):
                    st.code(action.command, language="bash")
    st.subheader("Scored trials")
    for trial in sorted(scored, key=lambda t: t.trial_key):
        icon = {"pass": "✅", "reward-failure": "❌"}.get(str(trial.outcome_class.value), "▫️")
        with st.expander(f"{icon} {trial.trial_key}"):
            left, right = st.columns(2)
            for name, labeled in (("Task", trial.task_name), ("Agent", trial.agent),
                                  ("Model", trial.model), ("Reward", trial.reward),
                                  ("Outcome", trial.outcome_class)):
                _labeled(left, name, labeled)
            for name, labeled in (("Timing", trial.timing), ("Cost", trial.cost),
                                  ("Config", trial.config)):
                _labeled(right, name, labeled)

            st.markdown("**Trajectory**")
            trajectory = trial.trajectory
            if isinstance(trajectory, TrajectoryView):
                _labeled(st, "Steps", trajectory.step_count)
                _labeled(st, "Repeated call signatures", trajectory.repeated_signatures)
                _labeled(st, "Verification before finishing", trajectory.verify_before_done)
                if trajectory.tool_calls:
                    st.dataframe(
                        [{"step": c.step_id, "call": c.tool_call_id,
                          "function": c.function, "exit": c.exit_code}
                         for c in trajectory.tool_calls],
                        hide_index=True, width="stretch",
                    )
            else:
                _labeled(st, "Trajectory", trajectory)

            if trial.artifacts:
                st.markdown("**Artifacts** (trial-relative, read-only)")
                st.dataframe(
                    [{"name": a.name, "path": a.relative_path, "bytes": a.size_bytes}
                     for a in trial.artifacts],
                    hide_index=True, width="stretch",
                )
            st.markdown("**Next action**")
            for action in next_actions_for_trial(trial):
                st.caption(action.label)
                st.code(action.command, language="bash")

with analyses_tab:
    if not index.analyses:
        st.info("No analysis sidecars found.")
    for analysis in index.analyses:
        with st.expander(f"🔎 {analysis.analysis_id} → {analysis.trial_key or 'unlinked'}"):
            _labeled(st, "Validation status", analysis.status)
            _labeled(st, "Validity", analysis.validity)
            _labeled(st, "Category", analysis.category)
            _labeled(st, "Summary", analysis.summary)
            _labeled(st, "Confidence", analysis.confidence)
            _labeled(st, "Alternatives", analysis.alternatives)
            st.markdown("**Evidence citations** (resolved against the source trial)")
            for citation in analysis.citations:
                ok = citation.resolution.value == "resolved"
                line = (f"{'✅' if ok else '⛔'} `{citation.citation_path}`"
                        f" step={citation.step_id} call={citation.tool_call_id}"
                        f" — {citation.supports}")
                st.markdown(line)
                if not ok:
                    st.caption(f"unresolved: {citation.resolution.reason}")
            _labeled(st, "Provenance", analysis.provenance)
    st.markdown("**Queue next actions**")
    for action in next_actions_for_queue():
        st.caption(action.label)
        st.code(action.command, language="bash")
