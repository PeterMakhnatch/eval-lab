"""Read-only report rendering and secret redaction for evidence consumers."""

from __future__ import annotations

import re
import shlex
from typing import Any

_SECRET_MARKERS = (
    "API_KEY",
    "API_TOKEN",
    "_SECRET",
    "ACCESS_KEY",
    "PASSWORD",
    "TOKEN",
    "OAUTH",
    "SESSION",
)
_SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(?:sk|rk)-[A-Za-z0-9._-]+"),
    re.compile(
        r"""(?i)["']?\b(?:api[_ -]?key|access[_ -]?token|oauth[_ -]?token|"""
        r"""session[_ -]?token|password|secret)["']?\s*[:=]\s*"""
        r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;]+)"""
    ),
)


def redact_text(text: str) -> str:
    """Redact common bearer/key-shaped values before a UI can render them."""
    redacted = text
    for pattern in _SECRET_TEXT_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


def redact_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    """Redact credential-shaped keys and nested string values recursively."""
    clean: dict[str, Any] = {}
    for key, value in mapping.items():
        if any(marker in str(key).upper() for marker in _SECRET_MARKERS):
            clean[key] = "[redacted]"
        else:
            clean[key] = _redact_value(value)
    return clean



_COVERAGE_JOB_NAME_LIMIT = 5


def render_experiment_text(report: dict[str, Any]) -> str:
    """Concise copyable text render of an experiment evidence view.

    Credential-shaped values are redacted with the explorer's existing
    helpers. Paths and commands are copyable text only; nothing executes.
    """
    lines: list[str] = []
    selection = report.get("selection") or {}
    lines.append(f"experiment evidence view ({report.get('kind')})")
    lines.append(
        f"selection: {selection.get('mode')} "
        f"{selection.get('experiment_id') or selection.get('job_dir') or 'inventory'}"
    )
    lines.append("")
    lines.append("experiments:")
    for experiment in report.get("experiments") or ():
        lines.append(
            f"  - {experiment.get('spec_id')} name={experiment.get('name')} "
            f"state={experiment.get('queue_state')} jobs={experiment.get('job_count')}"
        )
        for name in experiment.get("job_names") or ():
            lines.append(f"      job: {name}")
    if not (report.get("experiments") or ()):
        lines.append("  (none)")
    lines.append("")
    lines.append("queue:")
    for row in report.get("queue") or ():
        clean = redact_mapping(
            {"name": row.get("name"), "agent": row.get("agent"), "jobs_dir": row.get("jobs_dir")}
        )
        lines.append(
            f"  - [{row.get('state')}] {row.get('spec_id')} "
            f"{clean.get('name')} ({clean.get('agent')}) {row.get('path')}"
        )
        for receipt in row.get("reasons") or ():
            message = redact_text(str(receipt.get("message") or receipt.get("reason") or ""))
            lines.append(
                f"      reason {receipt.get('code') or receipt.get('status')}: "
                f"{message} [{receipt.get('path')}]"
            )
    if not (report.get("queue") or ()):
        lines.append("  (no queued specs)")
    lines.append("")
    lines.append("jobs:")
    for job in report.get("jobs") or ():
        lines.append(
            f"  - {job.get('name')} origin={job.get('origin')} "
            f"experiment={job.get('experiment_id')} status={job.get('execution_status')} "
            f"{job.get('path')}"
        )
        if job.get("load_issue"):
            lines.append(f"      unavailable: {redact_text(str(job['load_issue']))}")
        for trial in job.get("trials") or ():
            reward = trial.get("reward") or {}
            usage = trial.get("usage") or {}
            capture = trial.get("capture") or {}
            exception = trial.get("exception") or {}
            agent = trial.get("agent") or {}
            lines.append(
                f"      trial {trial.get('trial_name')} task={trial.get('task')} "
                f"agent={agent.get('name')} reward={reward.get('value')} "
                f"({reward.get('state')}) capture={capture.get('state')}"
            )
            if exception.get("class"):
                lines.append(
                    f"        exception: {exception.get('class')} "
                    f"(phase {exception.get('phase')}; execution signal, not a capture state)"
                )
            links = trial.get("links") or {}
            lines.append(
                f"        source: {links.get('source')} result: {links.get('result')} "
                f"trajectory: {links.get('trajectory')}"
            )
            lines.append(
                f"        usage: in={usage.get('input_tokens')} "
                f"out={usage.get('output_tokens')} usd={usage.get('cost_usd')} "
                f"({usage.get('state')}); capture note: {capture.get('reason')}"
            )
        if job.get("origin") == "harbor_native":
            lines.append(
                f"      next (copy, do not auto-run): evallab view {shlex.quote(str(job.get('path')))}"
            )
    if not (report.get("jobs") or ()):
        lines.append("  (none)")
    lines.append("")
    capture_report = report.get("capture_report") or {}
    lines.append(
        f"capture report: {capture_report.get('status')} "
        f"scope={capture_report.get('scope')} {capture_report.get('path') or ''}".rstrip()
    )
    if capture_report.get("source_root") or capture_report.get("derived_root"):
        lines.append(
            f"  roots: source={capture_report.get('source_root')} "
            f"derived={capture_report.get('derived_root')}"
        )
    job_ids = capture_report.get("job_ids")
    if isinstance(job_ids, list) and job_ids:
        shown_ids = ", ".join(str(jid) for jid in job_ids[:_COVERAGE_JOB_NAME_LIMIT])
        extra_ids = (
            f" +{len(job_ids) - _COVERAGE_JOB_NAME_LIMIT} more"
            if len(job_ids) > _COVERAGE_JOB_NAME_LIMIT
            else ""
        )
        lines.append(f"  closed-world job IDs ({len(job_ids)}): [{shown_ids}{extra_ids}]")
    membership = capture_report.get("membership")
    if isinstance(membership, dict) and membership.get("selected_job_ids"):
        sel_ids = membership["selected_job_ids"]
        shown_sel = ", ".join(str(jid) for jid in sel_ids[:_COVERAGE_JOB_NAME_LIMIT])
        extra_sel = (
            f" +{len(sel_ids) - _COVERAGE_JOB_NAME_LIMIT} more"
            if len(sel_ids) > _COVERAGE_JOB_NAME_LIMIT
            else ""
        )
        lines.append(f"  selected native UUIDs ({len(sel_ids)}): [{shown_sel}{extra_sel}]")
    summary = capture_report.get("summary") or {}
    for section, value in (summary.get("sections") or {}).items():
        names = value.get("jobs") or []
        shown = ", ".join(str(name) for name in names[:_COVERAGE_JOB_NAME_LIMIT])
        extra = (
            f" +{len(names) - _COVERAGE_JOB_NAME_LIMIT} more"
            if len(names) > _COVERAGE_JOB_NAME_LIMIT
            else ""
        )
        lines.append(
            f"  - {section}: count={value.get('count')} "
            f"truncated={value.get('truncated')} jobs=[{shown}{extra}]"
        )
    if summary.get("reasons"):
        lines.append(f"  capture reasons: {summary['reasons']}")
    if summary.get("trajectory_availability_by_agent") is not None:
        clean_availability = redact_mapping(summary["trajectory_availability_by_agent"])
        lines.append("  availability by agent:")
        for name, availability in clean_availability.items():
            lines.append(f"    {name}: {availability}")
    repairs = summary.get("repair_path")
    if isinstance(repairs, list) and repairs:
        lines.append("  supplied repair advice (not execution authorization):")
        for repair in repairs[:_COVERAGE_JOB_NAME_LIMIT]:
            if isinstance(repair, dict):
                lines.append(
                    f"    {repair.get('job_name')}: {repair.get('reason')} [{repair.get('origin')}]"
                )
                lines.append(
                    f"      {repair.get('resumable_command') or 'No resumable command supplied'}"
                )
        if len(repairs) > _COVERAGE_JOB_NAME_LIMIT:
            lines.append(
                f"    {len(repairs) - _COVERAGE_JOB_NAME_LIMIT} further entries in {capture_report.get('path')}"
            )
    elif repairs:
        lines.append(f"  supplied repair context: {repairs}")
    proof = capture_report.get("binding_proof")
    if isinstance(proof, dict):
        lines.append(
            f"  binding proof: wrong_root={proof.get('wrong_root')} "
            f"excluded={proof.get('excluded')} reasons={proof.get('reasons')}"
        )
    links = capture_report.get("external_links")
    if isinstance(links, list) and links:
        lines.append("  external links:")
        for link in links:
            if isinstance(link, dict):
                lines.append(
                    f"    - [{link.get('kind') or 'link'}] spec={link.get('spec_id')} "
                    f"job={link.get('job_name')} ({link.get('job_id')}) status={link.get('status')}"
                )
                if link.get("chain"):
                    lines.append(f"        chain: {link.get('chain')}")
                if link.get("done_spec"):
                    lines.append(f"        done spec: {link.get('done_spec')}")
                for key, value in link.items():
                    if key not in {
                        "kind",
                        "spec_id",
                        "job_name",
                        "job_id",
                        "status",
                        "chain",
                        "done_spec",
                    }:
                        lines.append(f"        {key}: {value}")
    lines.append("")
    if report.get("notices"):
        lines.append("notices:")
        for notice in report["notices"]:
            lines.append(f"  - {redact_text(notice)}")
        lines.append("")
    if report.get("issues"):
        lines.append("issues:")
        for issue in report["issues"]:
            lines.append(f"  - {redact_text(issue)}")
        lines.append("")
    return redact_text("\n".join(lines).rstrip() + "\n")
