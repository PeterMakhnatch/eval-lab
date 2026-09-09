"""Operator surface for paired harness comparison.

Buttons call evallab.harness_compare against the real Lab queue and policy.
They do not mock admission, tick Harbor, or bypass paid-run authorization.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from evallab.harness_compare import (
    HarnessCompareError,
    compile_pair,
    inspect_pair,
    load_manifest,
    render_pair_text,
    submit_pair,
)


def repo_root() -> Path:
    configured = os.environ.get("EVALLAB_DASHBOARD_ROOT")
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[1]


def run_operator_action(
    root: Path,
    *,
    action: str,
    manifest_path: Path,
    submitted_by: str = "harness-first-operator",
    canary_only: bool = True,
    queue_root: Path | None = None,
) -> dict[str, Any]:
    """Execute one real backend action. Used by the UI and tests."""
    manifest = load_manifest(manifest_path)
    if action == "prepare":
        return compile_pair(root, manifest, submitted_by=submitted_by, canary_only=canary_only)
    if action == "submit":
        return submit_pair(
            root,
            manifest,
            submitted_by=submitted_by,
            canary_only=canary_only,
            queue_root=queue_root,
        )
    if action == "inspect":
        analysis = None
        if manifest.analysis_report:
            analysis = root / manifest.analysis_report
        return inspect_pair(
            root,
            comparison_id=manifest.comparison_id,
            analysis_report=analysis,
            queue_root=queue_root,
        )
    raise HarnessCompareError(f"unknown operator action {action!r}")


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="Eval Lab — Paired harness comparison", layout="wide")
    st.title("Paired harness comparison")
    st.caption(
        "Select a frozen cohort manifest, prepare or submit paired specs through "
        "existing Lab policy, then inspect queue state and jobs. Model runs stay "
        "held until `uv run evallab approve <spec-id> --actor <you>`. This page "
        "does not tick Harbor or invent results."
    )
    root = repo_root()
    manifest_value = st.text_input("Manifest path", value=str(root / "pair.json"))
    submitted_by = st.text_input("Submitted by", value="harness-first-operator")
    canary_only = st.checkbox("Canary only (first launch)", value=True)
    columns = st.columns(3)
    action = None
    if columns[0].button("Prepare (compile only)"):
        action = "prepare"
    if columns[1].button("Submit through policy"):
        action = "submit"
    if columns[2].button("Inspect results"):
        action = "inspect"
    if action is None:
        st.info("No action yet. Buttons call evallab.harness_compare, not a mock screen.")
        return
    try:
        report = run_operator_action(
            root,
            action=action,
            manifest_path=Path(manifest_value),
            submitted_by=submitted_by,
            canary_only=canary_only,
        )
    except (HarnessCompareError, OSError, ValueError) as exc:
        st.error(str(exc))
        return
    st.subheader(f"{action} result")
    st.code(render_pair_text(report), language="text")
    st.json(report)
    for arm in report.get("arms") or ():
        hold = arm.get("hold") or {}
        if hold.get("approval_command"):
            st.warning(hold.get("message") or hold.get("reason_code"))
            st.code(hold["approval_command"], language="bash")


if __name__ == "__main__":
    main()
