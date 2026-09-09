"""Operator surface for paired harness comparison.

Buttons call evallab.harness_compare against the real Lab queue and policy.
They do not mock admission, tick Harbor, or bypass paid-run authorization.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from evallab.harness_compare import (
    DEFAULT_BASELINE_PROFILE,
    DEFAULT_CANDIDATE_PROFILE,
    HarnessCompareError,
    compile_pair,
    inspect_pair,
    load_pair_inputs,
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
    manifest_path: Path | None = None,
    cohort_path: Path | None = None,
    baseline_profile: str = DEFAULT_BASELINE_PROFILE,
    candidate_profile: str = DEFAULT_CANDIDATE_PROFILE,
    root_model: str | None = None,
    submitted_by: str = "harness-first-operator",
    canary_only: bool = True,
    queue_root: Path | None = None,
    analysis_report: Path | None = None,
) -> dict[str, Any]:
    """Execute one real backend action. Used by the UI and tests."""
    analysis_rel = None
    if analysis_report is not None:
        try:
            analysis_rel = analysis_report.resolve().relative_to(root).as_posix()
        except ValueError:
            analysis_rel = None
    manifest = load_pair_inputs(
        manifest_path=manifest_path,
        cohort_path=cohort_path,
        baseline_profile=baseline_profile,
        candidate_profile=candidate_profile,
        root_model=root_model,
        analysis_report=analysis_rel,
    )
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
        analysis = analysis_report
        if analysis is None and manifest.analysis_report:
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
        "Select a Factory cohort.json or a frozen paired manifest, prepare or submit "
        "through existing Lab policy, then inspect queue state, jobs, and any HAR-13 "
        "report. Model runs stay held until `uv run evallab approve <spec-id> --actor <you>`. "
        "This page does not tick Harbor or invent results."
    )
    root = repo_root()
    source = st.radio("Input", ("Factory cohort.json", "Paired manifest"), horizontal=True)
    cohort_value = st.text_input(
        "Cohort path",
        value=str(root / "research/experiments/harness-first/cohort.json"),
    )
    manifest_value = st.text_input("Manifest path", value=str(root / "pair.json"))
    baseline_profile = st.text_input("Baseline profile", value=DEFAULT_BASELINE_PROFILE)
    candidate_profile = st.text_input("Candidate profile", value=DEFAULT_CANDIDATE_PROFILE)
    root_model = st.text_input("Shared root model", value="deepseek/deepseek-v4-flash")
    analysis_value = st.text_input("HAR-13 analysis report or directory", value="")
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
            manifest_path=None if source.startswith("Factory") else Path(manifest_value),
            cohort_path=Path(cohort_value) if source.startswith("Factory") else None,
            baseline_profile=baseline_profile,
            candidate_profile=candidate_profile,
            root_model=root_model or None,
            submitted_by=submitted_by,
            canary_only=canary_only,
            analysis_report=Path(analysis_value) if analysis_value.strip() else None,
        )
    except (HarnessCompareError, OSError, ValueError) as exc:
        st.error(str(exc))
        return
    st.subheader(f"{action} result")
    st.code(render_pair_text(report), language="text")
    if report.get("comparison_spec"):
        st.subheader("CohortComparisonSpec for HAR-13")
        st.json(report["comparison_spec"])
    analysis = report.get("analysis") or {}
    if analysis.get("markdown"):
        st.subheader("HAR-13 report")
        st.caption(f"evidence_kind={analysis.get('evidence_kind')} sha256={analysis.get('sha256')}")
        st.markdown(analysis["markdown"])
    st.json(report)
    for arm in report.get("arms") or ():
        hold = arm.get("hold") or {}
        if hold.get("approval_command"):
            st.warning(hold.get("message") or hold.get("reason_code"))
            st.code(hold["approval_command"], language="bash")


if __name__ == "__main__":
    main()
