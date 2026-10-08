"""Eval Lab dashboard entry point: Integrity (default) + Operations pages."""

from __future__ import annotations

import streamlit as st

from dashboard import integrity_app, operations

st.set_page_config(page_title="Eval Lab — Integrity", page_icon="🔬", layout="wide")

integrity_page = st.Page(
    integrity_app.render_integrity_page, title="Integrity", icon="🛡️", default=True
)
operations_page = st.Page(
    operations.render_operations_page, title="Operations", icon="📊"
)

pg = st.navigation([integrity_page, operations_page])
pg.run()
