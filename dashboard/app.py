from __future__ import annotations

import os
import time
from pathlib import Path

import streamlit as st

from dashboard.projection import load_operator_snapshot
from evallab.status import SECTION_KEYS, StatusSnapshot

REPO_ROOT = Path(
    os.environ.get("EVALLAB_DASHBOARD_ROOT", Path(__file__).resolve().parents[1])
).resolve()
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://evallab:local-development-only@localhost:54329/evallab",
)


@st.cache_data(ttl=30, show_spinner=False)
def load_snapshot(repo_root_value: str, database_url: str) -> dict[str, object]:
    try:
        snapshot = load_operator_snapshot(Path(repo_root_value), postgres_url=database_url)
        return {"snapshot": snapshot, "error": None}
    except Exception as exc:  # Cold/missing stores stay readable.
        return {"snapshot": None, "error": f"{type(exc).__name__}: {exc}"}


def _render_section(name: str, snapshot: StatusSnapshot) -> None:
    section = getattr(snapshot, name)
    st.header(name)
    st.caption(f"availability: {section.availability}")
    if not section.items:
        st.info("No items in this section.")
        return
    rows = []
    for item in section.items:
        rows.append(
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
        )
    st.dataframe(rows, width="stretch", hide_index=True)


st.set_page_config(page_title="Eval Lab", page_icon="🔬", layout="wide")
started = time.perf_counter()
payload = load_snapshot(str(REPO_ROOT), DATABASE_URL)

st.title("Eval Lab operator status")
st.caption(
    "Read-only projection shared with `evallab status`. "
    "Approvals remain in the evallab CLI."
)

if payload["error"]:
    st.warning(payload["error"])
elif payload["snapshot"] is None:
    st.warning("Status snapshot unavailable.")
else:
    snapshot = payload["snapshot"]
    assert isinstance(snapshot, StatusSnapshot)
    st.caption(f"generated_at {snapshot.generated_at.isoformat()}")
    for name in SECTION_KEYS:
        _render_section(name, snapshot)

st.caption(f"Snapshot loaded in {time.perf_counter() - started:.3f}s · cached for 30 seconds")
