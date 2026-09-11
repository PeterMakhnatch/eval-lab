"""Operator surface for paired harness comparison.

Buttons call evallab.harness_compare against the real Lab queue and policy.
They do not mock admission, tick Harbor, or bypass paid-run authorization.
"""

from __future__ import annotations

import copy
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
    readiness_report,
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
    if action == "readiness":
        return readiness_report(root, manifest, submitted_by=submitted_by, queue_root=queue_root)
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


def sanitize_readiness_report(report: dict[str, Any]) -> dict[str, Any]:
    """Ensure readiness reports suppress approval commands when blocked, stale, unknown, or ambiguous."""
    if not isinstance(report, dict):
        return report
    if report.get("kind") != "harness_paired_readiness" and "gates" not in report:
        return report

    sanitized = copy.deepcopy(report)
    verdict = sanitized.get("verdict")
    current_pair = sanitized.get("current_pair")
    pair_status = current_pair.get("status") if isinstance(current_pair, dict) else None
    block_all = (verdict != "READY_FOR_APPROVAL") or (pair_status != "present")

    for arm in sanitized.get("arms") or []:
        if isinstance(arm, dict) and isinstance(arm.get("hold"), dict) and block_all:
            arm["hold"]["approval_command"] = None

    for row in sanitized.get("spec_ids") or []:
        if isinstance(row, dict):
            freshness = row.get("freshness")
            stale = row.get("stale")
            if block_all or freshness in {"stale", "unknown"} or stale is not False:
                row["approval_command"] = None

    return sanitized

def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="Eval Lab — Paired harness comparison", layout="wide")
    st.title("Paired harness comparison")
    st.caption(
        "Select a Factory cohort.json or a frozen paired manifest. Readiness is the "
        "no-spend launch view: it will not call READY_FOR_APPROVAL while HAR-10 is blocked. "
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
    queue_value = st.text_input(
        "Isolated queue root (optional; blank preserves no-production-scan default)",
        value="",
    )
    submitted_by = st.text_input("Submitted by", value="harness-first-operator")
    canary_only = st.checkbox("Canary only (first launch)", value=True)
    columns = st.columns(4)
    action = None
    if columns[0].button("Prepare (compile only)"):
        action = "prepare"
    if columns[1].button("Readiness (no spend)"):
        action = "readiness"
    if columns[2].button("Submit through policy"):
        action = "submit"
    if columns[3].button("Inspect results"):
        action = "inspect"
    if action is None:
        st.info("No action yet. Buttons call evallab.harness_compare, not a mock screen.")
        return
    queue_root = None
    if queue_value.strip():
        q_path = Path(queue_value.strip())
        queue_root = q_path if q_path.is_absolute() else (root / q_path)
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
            queue_root=queue_root,
            analysis_report=Path(analysis_value) if analysis_value.strip() else None,
        )
    except (HarnessCompareError, OSError, ValueError) as exc:
        st.error(str(exc))
        return
    if action == "readiness" or report.get("kind") == "harness_paired_readiness":
        report = sanitize_readiness_report(report)
    st.subheader(f"{action} result")
    st.code(render_pair_text(report), language="text")

    if report.get("verdict") == "BLOCKED":
        st.error(report.get("verdict_reason") or "Not READY_FOR_APPROVAL")
    elif report.get("verdict"):
        st.info(f"Verdict: {report.get('verdict')} — {report.get('verdict_reason') or ''}")

    current_pair = report.get("current_pair")
    if current_pair and isinstance(current_pair, dict):
        st.subheader("Current pair")
        status = current_pair.get("status") or "absent"
        reason = current_pair.get("reason") or "No reason recorded."
        b_id = current_pair.get("baseline_spec_id")
        c_id = current_pair.get("candidate_spec_id")
        if status == "present":
            st.success(
                f"**Status: present**\n\n"
                f"- Baseline spec: `{b_id}`\n"
                f"- Candidate spec: `{c_id}`\n\n"
                f"{reason}\n\n"
                "*Notice: 'present' indicates verified metadata freshness only; it does not grant execution authorization or prove runtime operability.*"
            )
        elif status == "ambiguous":
            st.warning(f"**Status: ambiguous**\n\n{reason}")
        elif status == "incomplete":
            st.warning(f"**Status: incomplete**\n\n{reason}")
        elif status == "unverifiable":
            st.warning(f"**Status: unverifiable**\n\n{reason}")
        elif status == "absent":
            st.info(f"**Status: absent**\n\n{reason}")
        else:
            st.info(f"**Status: {status}**\n\n{reason}")

    gates = report.get("gates")
    if gates and isinstance(gates, list):
        st.subheader("Readiness gates")
        gate_rows = [
            {
                "Gate": gate.get("name"),
                "Status": gate.get("status"),
                "Evidence kind": gate.get("evidence_kind", "unavailable"),
                "Detail": gate.get("detail", ""),
            }
            for gate in gates
            if isinstance(gate, dict)
        ]
        if gate_rows:
            st.dataframe(gate_rows, width="stretch", hide_index=True)

    estimates = report.get("estimates")
    if estimates and isinstance(estimates, dict):
        st.subheader("Cost estimates")
        cov = estimates.get("coverage", "unknown")
        b_usd = estimates.get("baseline_usd")
        c_usd = estimates.get("candidate_usd")
        src = estimates.get("source")
        reason_desc = estimates.get("reason") or ""
        b_str = f"${b_usd:.2f}" if isinstance(b_usd, (int, float)) else "null (unknown)"
        c_str = f"${c_usd:.2f}" if isinstance(c_usd, (int, float)) else "null (unknown)"
        st.write(f"**Coverage:** `{cov}` | **Baseline:** {b_str} | **Candidate:** {c_str}")
        if src:
            st.caption(f"**Source:** {src}")
        if reason_desc:
            st.caption(f"**Applicability:** {reason_desc}")

    spec_ids = report.get("spec_ids")
    if spec_ids and isinstance(spec_ids, list):
        st.subheader("Inspected queue specs")
        spec_rows = [
            {
                "Arm": row.get("arm"),
                "Spec ID": row.get("spec_id"),
                "Freshness": row.get("freshness", "unknown"),
                "Stale": str(row.get("stale")),
                "Queue state": row.get("queue_state"),
                "Status reason": row.get("status_reason", ""),
            }
            for row in spec_ids
            if isinstance(row, dict)
        ]
        if spec_rows:
            st.dataframe(spec_rows, width="stretch", hide_index=True)

    if report.get("comparison_spec"):
        st.subheader("CohortComparisonSpec for HAR-13")
        st.json(report["comparison_spec"])
    analysis = report.get("analysis") or {}
    if analysis.get("markdown"):
        st.subheader("HAR-13 report")
        st.caption(f"evidence_kind={analysis.get('evidence_kind')} sha256={analysis.get('sha256')}")
        st.markdown(analysis["markdown"])
    st.subheader("Raw report")
    st.json(report)
    for arm in report.get("arms") or ():
        hold = arm.get("hold") or {}
        if hold.get("approval_command") and report.get("verdict") != "BLOCKED":
            st.warning(hold.get("message") or hold.get("reason_code"))
            st.code(hold["approval_command"], language="bash")

if __name__ == "__main__":
    main()
