"""Run & analysis explorer page (M005). Read-only, always.

Launch:  uv run streamlit run dashboard/explorer.py
Root:    EVALLAB_EXPLORER_ROOT (defaults to the repository root; a fixture
         root such as tests/fixtures/explorer works for a safe demo).

Every field renders with its provenance label (observed / derived / draft /
withheld / unavailable). Infrastructure exceptions render in their own
section, never alongside reward failures. Steps and citations render the
availability of what they point at, so a citation into a prompt that
promotion redacted can never look like a citation into real agent behaviour.
Next Action shows copyable commands and executes nothing. No control in this
page mutates any state anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from evallab.explorer import (
    CitationResolution,
    ExplorerIndex,
    Labeled,
    TrajectoryView,
    build_index,
    citation_state,
    content_summary,
    next_actions_for_queue,
    next_actions_for_task,
    next_actions_for_trial,
)

_BADGE = {
    "observed": "🟢 observed",
    "derived": "🔵 derived",
    "draft": "🟡 draft (unreviewed model output)",
    "withheld": "🔒 withheld (redacted before promotion)",
    "unavailable": "⚪ unavailable",
}

# One glyph per state, so a reader never has to read prose to tell readable
# evidence from evidence that was deliberately removed. The wording itself comes
# from evallab.explorer, which the test suite can import; this page cannot be
# imported in CI because Streamlit is not a project dependency.
_CONTENT_ICON = {
    "observed": "🟢",
    "derived": "🔵",
    "withheld": "🔒",
    "unavailable": "⚪",
}
_CITATION_ICON = {
    "readable": "✅",
    "withheld": "🔒",
    "absent": "⚪",
    "unresolved": "⛔",
}


def _content_cell(labeled: Labeled) -> str:
    """One table cell stating which content state applies."""
    return f"{_CONTENT_ICON.get(labeled.provenance, '▫️')} {content_summary(labeled)}"


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


def _citation_line(citation: CitationResolution) -> tuple[str, str | None]:
    """A citation renders its resolution *and* what it lets a reader see.

    ⛔ unresolved · 🔒 resolved but the cited text was withheld before promotion
    · ⚪ resolved but genuinely absent · ✅ resolved and readable. Rendering the
    middle two like the last one is the defect this page was fixed for; the
    states themselves come from ``evallab.explorer.citation_state``.
    """
    state = citation_state(citation)
    detail = (
        f"unresolved: {citation.resolution.reason}"
        if state == "unresolved"
        else None if state == "readable" else citation.content.reason
    )
    line = (
        f"{_CITATION_ICON[state]} `{citation.citation_path}`"
        f" step={citation.step_id} call={citation.tool_call_id}"
        f" — {citation.supports}"
        f" · {content_summary(citation.content)}"
    )
    return line, detail


st.set_page_config(page_title="Eval Lab — Explorer", page_icon="🧭", layout="wide")
st.title("Run & analysis explorer")
st.caption(
    "Read-only. Every value is labeled observed / derived / draft / withheld / "
    "unavailable. 🔒 marks evidence that promotion removed on purpose — the byte "
    "count and sha256 of the original are kept so the claim stays auditable. "
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
                st.markdown(f"**Discovered under** `{job.jobs_root}`")
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
                if trajectory.redaction.provenance == "withheld":
                    # Stated before the tables, so nobody reads the trajectory
                    # believing they are looking at all of it.
                    st.warning(f"🔒 {trajectory.redaction.reason}")
                else:
                    st.caption(f"🔵 {trajectory.redaction.reason}")
                _labeled(st, "Repeated call signatures", trajectory.repeated_signatures)
                _labeled(st, "Verification before finishing", trajectory.verify_before_done)
                if trajectory.steps:
                    st.caption(
                        "Step content — 🟢 readable · 🔒 withheld before promotion "
                        "(bytes and sha256 of the original shown) · ⚪ absent."
                    )
                    st.dataframe(
                        [{"step": s.step_id, "source": s.source,
                          "message": _content_cell(s.message),
                          "tool calls": s.n_tool_calls,
                          "observations": s.n_observations}
                         for s in trajectory.steps],
                        hide_index=True, width="stretch",
                    )
                if trajectory.tool_calls:
                    st.dataframe(
                        [{"step": c.step_id, "call": c.tool_call_id,
                          "function": c.function, "exit": c.exit_code,
                          "observation": _content_cell(c.observation)}
                         for c in trajectory.tool_calls],
                        hide_index=True, width="stretch",
                    )
                    st.caption(
                        "`exit` is blank unless the observation records "
                        "`command_exit_code`; no promoted Codex trajectory does."
                    )
            else:
                _labeled(st, "Trajectory", trajectory)

            if trial.artifacts:
                st.markdown("**Artifacts** (trial-relative, read-only)")
                st.dataframe(
                    [{"name": a.name, "path": a.relative_path, "bytes": a.size_bytes,
                      "content": _content_cell(a.content)}
                     for a in trial.artifacts],
                    hide_index=True, width="stretch",
                )
            if trial.omitted_files.provenance == "withheld":
                # These files are in no artifact list because they are not in the
                # bundle. Saying nothing would understate what was removed.
                st.warning(f"🔒 {trial.omitted_files.reason}")
                st.dataframe(
                    list(trial.omitted_files.value["markers"]),
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
        header = analysis.trial_key or "SOURCE TRIAL NOT FOUND"
        with st.expander(f"🔎 {analysis.analysis_id} → {header}"):
            _labeled(st, "Source trial", analysis.link)
            _labeled(st, "Validation status", analysis.status)
            _labeled(st, "Validity", analysis.validity)
            _labeled(st, "Category", analysis.category)
            _labeled(st, "Summary", analysis.summary)
            _labeled(st, "Confidence", analysis.confidence)
            _labeled(st, "Alternatives", analysis.alternatives)
            st.markdown("**Evidence citations** (resolved against the source trial)")
            st.caption(
                "✅ readable · 🔒 resolved but the cited text was withheld before "
                "promotion · ⚪ resolved but absent · ⛔ does not resolve."
            )
            for citation in analysis.citations:
                line, detail = _citation_line(citation)
                st.markdown(line)
                if detail:
                    st.caption(detail)
            _labeled(st, "Provenance", analysis.provenance)
    st.markdown("**Queue next actions**")
    for action in next_actions_for_queue():
        st.caption(action.label)
        st.code(action.command, language="bash")
