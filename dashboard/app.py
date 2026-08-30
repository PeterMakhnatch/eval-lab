from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from dashboard.projection import load_operator_snapshot
from dashboard.queries import (
    AttachSource,
    action_memory_contrast_fidelity,
    atif_activity,
    calibration_history,
    canary_history,
    daily_ceiling,
    discoveries,
    experiment_operations,
    experiment_operations_summary,
    leaderboard,
    model_access_vs_capability,
    queue_funnel,
    spend_history,
)
from evallab.status import SECTION_KEYS, StatusSnapshot
from evallab.storage.attach import attach

REPO_ROOT = Path(
    os.environ.get("EVALLAB_DASHBOARD_ROOT", Path(__file__).resolve().parents[1])
).resolve()
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://evallab:local-development-only@localhost:54329/evallab",
)
EXPLICIT_DERIVED = os.environ.get("EVALLAB_DERIVED_ROOT")


@st.cache_data(ttl=30, show_spinner=False)
def load_operator_status(repo_root_value: str, database_url: str) -> dict[str, object]:
    try:
        snapshot = load_operator_snapshot(Path(repo_root_value), postgres_url=database_url)
        return {"snapshot": snapshot, "error": None}
    except Exception as exc:  # Cold/missing stores stay readable.
        return {"snapshot": None, "error": f"{type(exc).__name__}: {exc}"}


@st.cache_data(ttl=30, show_spinner=False)
def load_research_snapshot(
    repo_root_value: str, report_day: date, explicit_derived: str | None = None
) -> dict[str, Any]:
    """Preserve the established research panes beside the operator projection."""
    root = Path(repo_root_value)
    derived = Path(explicit_derived) if explicit_derived else None
    attach_res = attach(repo_root=root, explicit_derived=derived)
    source = AttachSource(attach_res)
    errors: dict[str, str] = {}

    def load(label: str, function: Callable[[], Any], fallback: Any) -> Any:
        try:
            return function()
        except Exception as exc:  # One unavailable source must not hide other panes.
            errors[label] = f"{type(exc).__name__}: {exc}"
            return fallback

    def load_ops() -> list[dict[str, Any]]:
        return experiment_operations(source)

    ops_trials = load("operations_trials", load_ops, [])

    return {
        "leaderboard": load("leaderboard", lambda: leaderboard(source), []),
        "canaries": load("canaries", lambda: canary_history(source), []),
        "spend": load(
            "spend", lambda: spend_history(source, through=report_day, days=7), []
        ),
        "ceiling": load(
            "ceiling", lambda: daily_ceiling(root / "policy/standing-approvals.yaml"), 0.0
        ),
        "queue": load("queue", lambda: queue_funnel(root / "queue"), []),
        "calibrations": load(
            "calibrations",
            lambda: calibration_history(
                source, records_root=root / "research/calibration/records"
            ),
            [],
        ),
        "atif": load("atif", lambda: atif_activity(source), {}),
        "discoveries": load(
            "discoveries", lambda: discoveries(root / "digests/DISCOVERIES.md"), []
        ),
        "operations_trials": ops_trials,
        "operations_summary": load(
            "operations_summary",
            lambda: experiment_operations_summary(ops_trials) if ops_trials else [],
            [],
        ),
        "model_access": load(
            "model_access",
            lambda: model_access_vs_capability(ops_trials) if ops_trials else [],
            [],
        ),
        "action_memory": load(
            "action_memory",
            lambda: action_memory_contrast_fidelity(ops_trials) if ops_trials else {},
            {},
        ),
        "errors": errors,
    }


def _render_status_section(name: str, snapshot: StatusSnapshot) -> None:
    section = getattr(snapshot, name)
    st.caption(f"availability: {section.availability}")
    if not section.items:
        st.info("No items in this section.")
        return
    rows = [
        {
            "availability": item.availability,
            "label": item.label,
            "detail": item.detail or "",
            "kind": item.kind or "",
            "experiment": item.experiment_id or "",
            "job": item.job_id or "",
            "trial": item.trial_id or "",
            "analysis": item.analysis_id or "",
            "exception": item.exception_class or "",
            "scored as model failure": item.scored_as_model_failure,
        }
        for item in section.items
    ]
    st.dataframe(rows, width="stretch", hide_index=True)


def _percent(value: Any) -> str:
    return "—" if value is None else f"{float(value):.1%}"


def _pass_at_1_basis(row: dict[str, Any]) -> str:
    """Say why pass@1 is `—` so it cannot be read as "no data yet".

    `—` on a row means the cohort ran trials and none of them produced a score.
    That is a different fact from an empty leaderboard, and the statistic itself
    is unchanged: an unscorable cohort has no pass@1 and no interval.
    """
    if row["scorable"]:
        return f"{row['n']} of {row['n_total']} trials scored"
    parts = []
    if row["exceptions"]:
        parts.append(f"{row['exceptions']} raised an exception")
    if row["unscored_no_reward"]:
        parts.append(f"{row['unscored_no_reward']} recorded no reward")
    return f"unscorable — {row['n_total']} trials, " + " and ".join(parts)


def _leaderboard_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "cohort": row["cohort"],
            "task": row["task"],
            "agent / model": f"{row['agent']} / {row['model']}",
            "n total": row["n_total"],
            "n scored": row["n"],
            "exceptions": row["exceptions"],
            "passes": row["passes"],
            "pass@1": _percent(row["pass_rate"]),
            "95% CI": (
                f"{_percent(row['ci_95_low'])} – {_percent(row['ci_95_high'])}"
                if row["scorable"]
                else "not defined"
            ),
            "pass@1 basis": _pass_at_1_basis(row),
        }
        for row in rows
    ]


def _calibration_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "date": row["date"],
            "family": row["family"],
            "judge": f"{row['backend']} / {row['model']}",
            "documents": row["documents"],
            "n decisions": row["n"],
            "agreement": _percent(row["agreement"]),
            "95% CI": f"{_percent(row['ci_95_low'])} – {_percent(row['ci_95_high'])}",
            "floor": _percent(row["floor"]),
            "reportable": row["reportable"],
            "meets floor": row["meets_floor"],
            "status": row["status"],
        }
        for row in rows
    ]


st.set_page_config(page_title="Eval Lab", page_icon="🔬", layout="wide")
started = time.perf_counter()
operator_payload = load_operator_status(str(REPO_ROOT), DATABASE_URL)
research = load_research_snapshot(str(REPO_ROOT), date.today(), EXPLICIT_DERIVED)

st.title("Eval Lab research overview")
st.caption(
    "Read-only operator status plus unified attach surface (Z2 catalog, "
    "Z3 Parquet, Z4 docs), queue, and research records. Approvals remain in the evallab CLI."
)

st.header("Operator status")
if operator_payload["error"]:
    st.warning(operator_payload["error"])
elif operator_payload["snapshot"] is None:
    st.warning("Status snapshot unavailable.")
else:
    operator = operator_payload["snapshot"]
    assert isinstance(operator, StatusSnapshot)
    st.caption(f"generated_at {operator.generated_at.isoformat()}")
    for tab, name in zip(st.tabs(SECTION_KEYS), SECTION_KEYS, strict=True):
        with tab:
            _render_status_section(name, operator)

if research["errors"]:
    with st.expander("Unavailable research sources", expanded=True):
        for pane, error in research["errors"].items():
            st.warning(f"{pane}: {error}")

st.header("Operational experiment view")
st.caption(
    "Fail-closed operational view separating scored, harness-failure, provider-access, "
    "calibration-only, and causal-admissible trials. Capability pass rates are computed "
    "strictly over scored trials (zero denominator contamination)."
)

ops_summary = research.get("operations_summary", [])
if ops_summary:
    st.subheader("Campaign progress & evidence eligibility")
    summary_table = [
        {
            "cohort": row["cohort"],
            "task": row["task"],
            "agent / model": f"{row['agent']} / {row['model']}",
            "total trials": row["n_total"],
            "scored": row["n_scored"],
            "passes": row["passes"],
            "pass@1 (scored only)": _percent(row["pass_rate"]),
            "95% CI": (
                f"{_percent(row['ci_95_low'])} – {_percent(row['ci_95_high'])}"
                if row["scorable"]
                else "not defined"
            ),
            "harness failures": row["harness_failures"],
            "provider access failures": row["provider_access_failures"],
            "refusals": row["refusals"],
            "causal admissible": row["causal_admissible"],
            "calibration only": row["calibration_only"],
        }
        for row in ops_summary
    ]
    st.dataframe(summary_table, width="stretch", hide_index=True)
elif "operations_summary" in research.get("errors", {}):
    st.warning(f"Operations summary unavailable: {research['errors']['operations_summary']}")
else:
    st.info("No operational experiment trials are indexed yet.")

model_access = research.get("model_access", [])
if model_access:
    st.subheader("Model access vs capability")
    st.caption(
        "Provider access failures (rate limits, 429/5xx) are separated from agent "
        "capability pass rates to prevent misattributing outages to model failure."
    )
    access_table = [
        {
            "agent / model": f"{row['agent']} / {row['model']}",
            "attempts": row["total_attempts"],
            "access status": row["access_status"],
            "access failures": row["access_failures"],
            "access rate": _percent(row["access_success_rate"]),
            "scored trials": row["n_scored"],
            "passes": row["passes"],
            "capability rate": _percent(row["capability_pass_rate"]),
            "95% CI": (
                f"{_percent(row['ci_95_low'])} – {_percent(row['ci_95_high'])}"
                if row["n_scored"] > 0
                else "not defined"
            ),
            "basis": row["access_vs_capability_basis"],
        }
        for row in model_access
    ]
    st.dataframe(access_table, width="stretch", hide_index=True)

am_fidelity = research.get("action_memory", {})
if am_fidelity and am_fidelity.get("total_trials", 0) > 0:
    st.subheader("Action Memory matched contrast fidelity")
    st.caption(
        "Matched contrast fidelity across dose ladder cells (paired by task_block_id, "
        "dose_bytes, seed). Exposes coverage, unknown, omitted, duplicate, and order fidelity."
    )
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Coverage fidelity", _percent(am_fidelity.get("coverage_fidelity")))
    c2.metric("Matched pairs", am_fidelity.get("matched_pairs", 0))
    c3.metric("Unknown keys", am_fidelity.get("unknown_count", 0))
    c4.metric("Omitted arms", am_fidelity.get("omitted_count", 0))
    c5.metric("Order fidelity rate", _percent(am_fidelity.get("order_fidelity_rate")))

    if am_fidelity.get("contrast_groups"):
        st.dataframe(am_fidelity["contrast_groups"], width="stretch", hide_index=True)

ops_trials = research.get("operations_trials", [])
refused_trials = [t for t in ops_trials if t.get("outcome_class") == "refused"]
if refused_trials:
    with st.expander(f"Refusal & unscored trials ({len(refused_trials)} observed)", expanded=False):
        st.caption("Refusal reasons displayed explicitly rather than coercing missing outcomes to zero.")
        refusal_table = [
            {
                "cohort": t.get("cohort"),
                "job": t.get("job_name"),
                "trial": t.get("trial_name"),
                "task": t.get("task_name"),
                "agent / model": f"{t.get('agent_name')} / {t.get('model_name')}",
                "refusal rationale": t.get("refusal_reason"),
            }
            for t in refused_trials
        ]
        st.dataframe(refusal_table, width="stretch", hide_index=True)

st.header("Leaderboard by cohort")
st.caption(
    "Pass@1 uses the cohort analyzer's Wilson 95% interval; exceptions are excluded from n. "
    "A `—` in pass@1 never means 'no data yet': it means this cohort ran trials and none of "
    "them produced a score. `pass@1 basis` says which. No trials at all shows as an empty "
    "table below instead."
)
if research["leaderboard"]:
    st.dataframe(
        _leaderboard_rows(research["leaderboard"]), width="stretch", hide_index=True
    )
elif "leaderboard" in research["errors"]:
    st.warning(f"Leaderboard unavailable: {research['errors']['leaderboard']}")
else:
    st.info("No catalog trials are indexed yet — no data, as distinct from unscorable data.")

st.header("Canary trend vs 7-day baseline")
canaries = research["canaries"]
if canaries:
    chart_rows = []
    for row in canaries:
        series = f"{row['task_name']} · {row['agent_name']}"
        chart_rows.append(
            {
                "date": row["observation_date"],
                "series": f"{series} current",
                "reward": row["reward"],
            }
        )
        chart_rows.append(
            {
                "date": row["observation_date"],
                "series": f"{series} 7-day baseline",
                "reward": row["baseline_mean"],
            }
        )
    st.line_chart(pd.DataFrame(chart_rows), x="date", y="reward", color="series")
    st.dataframe(canaries, width="stretch", hide_index=True)
elif "canaries" in research["errors"]:
    st.warning(f"Canary trend unavailable: {research['errors']['canaries']}")
else:
    st.info("No canary observations are indexed yet.")

st.header("Spend vs daily ceiling")
spend = research["spend"]
ceiling = float(research["ceiling"])
today_spend = float(spend[-1]["spend_usd"]) if spend else 0.0
left, middle, right = st.columns(3)
left.metric("Today", f"${today_spend:.2f}")
middle.metric("Daily ceiling", f"${ceiling:.2f}")
right.metric("Remaining", f"${max(ceiling - today_spend, 0):.2f}")
st.progress(min(today_spend / ceiling, 1.0) if ceiling else 0.0)
if spend:
    st.bar_chart(pd.DataFrame(spend), x="date", y="spend_usd")
elif "spend" in research["errors"]:
    st.warning(f"Spend history unavailable: {research['errors']['spend']}")

st.header("Queue funnel")
queue = research["queue"]
if queue:
    st.bar_chart(pd.DataFrame(queue), x="state", y="count")
    st.dataframe(queue, width="stretch", hide_index=True)
elif "queue" in research["errors"]:
    st.warning(f"Queue funnel unavailable: {research['errors']['queue']}")
else:
    st.info("No queue state is available.")

st.header("Calibration history")
if research["calibrations"]:
    st.dataframe(
        _calibration_rows(research["calibrations"]), width="stretch", hide_index=True
    )
elif "calibrations" in research["errors"]:
    st.warning(f"Calibration history unavailable: {research['errors']['calibrations']}")
else:
    st.info("No measured calibration records are available.")

st.header("ATIF-derived activity")
atif = research["atif"]
if atif.get("summary"):
    summary = atif["summary"]
    columns = st.columns(5)
    columns[0].metric("Trials", summary["trial_count"])
    columns[1].metric("Trajectories", summary["trajectory_count"])
    columns[2].metric("Steps", summary["step_count"])
    columns[3].metric("LLM calls", summary["llm_call_count"])
    columns[4].metric("Tool calls", summary["tool_call_count"])
    if summary["invalid_trial_count"]:
        st.warning(f"{summary['invalid_trial_count']} trial(s) contain invalid trajectories.")
    if atif["tools"]:
        st.dataframe(atif["tools"], width="stretch", hide_index=True)
elif "atif" in research["errors"]:
    st.warning(f"ATIF activity unavailable: {research['errors']['atif']}")
else:
    st.info("No ATIF-derived Parquet is available.")

st.header("DISCOVERIES")
if research["discoveries"]:
    for entry in research["discoveries"]:
        st.subheader(entry["discovery_id"])
        st.caption(f"Status: {entry['status']}")
        st.write(entry["claim"] or "No claim text found.")
elif "discoveries" in research["errors"]:
    st.warning(f"DISCOVERIES unavailable: {research['errors']['discoveries']}")
else:
    st.info("No discovery entries are recorded.")

st.caption(f"Snapshot loaded in {time.perf_counter() - started:.3f}s · cached for 30 seconds")

st.divider()
st.subheader("Run & analysis explorer")
st.caption(
    "Drill into any task, job, trial, trajectory, or analysis — read-only, "
    "with per-field provenance and copyable next-action commands:"
)
st.code("uv run --with streamlit==1.61.1 streamlit run dashboard/explorer.py", language="bash")
