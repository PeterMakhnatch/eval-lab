from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from dashboard.queries import (
    ReadOnlyPostgres,
    atif_activity,
    calibration_history,
    canary_history,
    daily_ceiling,
    discoveries,
    leaderboard,
    queue_funnel,
    spend_history,
)

REPO_ROOT = Path(
    os.environ.get("EVALLAB_DASHBOARD_ROOT", Path(__file__).resolve().parents[1])
).resolve()
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://evallab:local-development-only@localhost:54329/evallab",
)


@st.cache_data(ttl=30, show_spinner=False)
def load_snapshot(repo_root_value: str, database_url: str, report_day: date) -> dict[str, Any]:
    root = Path(repo_root_value)
    source = ReadOnlyPostgres(database_url)
    errors: dict[str, str] = {}

    def load(label: str, function: Callable[[], Any], fallback: Any) -> Any:
        try:
            return function()
        except Exception as exc:  # Each pane remains visible when one source is unavailable.
            errors[label] = f"{type(exc).__name__}: {exc}"
            return fallback

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
        "atif": load("atif", lambda: atif_activity(root / "derived/parquet"), {}),
        "discoveries": load(
            "discoveries", lambda: discoveries(root / "digests/DISCOVERIES.md"), []
        ),
        "errors": errors,
    }


def _percent(value: Any) -> str:
    return "—" if value is None else f"{float(value):.1%}"


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
            "95% CI": f"{_percent(row['ci_95_low'])} – {_percent(row['ci_95_high'])}",
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
snapshot = load_snapshot(str(REPO_ROOT), DATABASE_URL, date.today())

st.title("Eval Lab research overview")
st.caption(
    "Read-only view of the catalog, ATIF-derived Parquet, queue state, and research records. "
    "Approvals remain in the evallab CLI."
)

if snapshot["errors"]:
    with st.expander("Unavailable sources", expanded=True):
        for pane, error in snapshot["errors"].items():
            st.warning(f"{pane}: {error}")

st.header("Leaderboard by cohort")
st.caption("Pass@1 uses the cohort analyzer's Wilson 95% interval; exceptions are excluded from n.")
if snapshot["leaderboard"]:
    st.dataframe(_leaderboard_rows(snapshot["leaderboard"]), width="stretch", hide_index=True)
else:
    st.info("No catalog trials are available.")

st.header("Canary trend vs 7-day baseline")
canaries = snapshot["canaries"]
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
else:
    st.info("No canary observations are indexed yet.")

st.header("Spend vs daily ceiling")
spend = snapshot["spend"]
ceiling = float(snapshot["ceiling"])
today_spend = float(spend[-1]["spend_usd"]) if spend else 0.0
left, middle, right = st.columns(3)
left.metric("Today", f"${today_spend:.2f}")
middle.metric("Daily ceiling", f"${ceiling:.2f}")
right.metric("Remaining", f"${max(ceiling - today_spend, 0):.2f}")
st.progress(min(today_spend / ceiling, 1.0) if ceiling else 0.0)
if spend:
    st.bar_chart(pd.DataFrame(spend), x="date", y="spend_usd")

st.header("Queue funnel")
queue = snapshot["queue"]
if queue:
    st.bar_chart(pd.DataFrame(queue), x="state", y="count")
    st.dataframe(queue, width="stretch", hide_index=True)
else:
    st.info("No queue state is available.")

st.header("Calibration history")
if snapshot["calibrations"]:
    st.dataframe(_calibration_rows(snapshot["calibrations"]), width="stretch", hide_index=True)
else:
    st.info("No measured calibration records are available.")

st.header("ATIF-derived activity")
atif = snapshot["atif"]
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
else:
    st.info("No ATIF-derived Parquet is available.")

st.header("DISCOVERIES")
if snapshot["discoveries"]:
    for entry in snapshot["discoveries"]:
        st.subheader(entry["discovery_id"])
        st.caption(f"Status: {entry['status']}")
        st.write(entry["claim"] or "No claim text found.")
else:
    st.info("No discovery entries are recorded.")

st.caption(f"Snapshot loaded in {time.perf_counter() - started:.3f}s · cached for 30 seconds")
