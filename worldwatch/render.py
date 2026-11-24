from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from worldwatch.model import MAX_REPORT_JSON_NODES, sanitize_surrogates, validate_json_tree


def pct(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{100.0 * value:.{digits}f}%"


def num(value: float | None, digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def shorten(text: str, width: int = 44) -> str:
    assert width >= 4
    clean = sanitize_surrogates(text)
    return clean if len(clean) <= width else clean[: width - 1] + "…"


def to_json(report: Mapping[str, Any]) -> str:
    validate_json_tree(report, max_nodes=MAX_REPORT_JSON_NODES)
    raw = json.dumps(report, indent=2, sort_keys=False, ensure_ascii=False, allow_nan=False)
    return sanitize_surrogates(raw)


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]], right: Sequence[int] = ()) -> str:
    assert len(headers) >= 1
    right_set = set(right)
    cells = [
        [sanitize_surrogates(str(h)) for h in headers],
        *[[sanitize_surrogates(str(c)) for c in r] for r in rows],
    ]
    widths = [max(len(r[idx]) for r in cells) for idx in range(len(headers))]
    lines: list[str] = []
    for row_idx, row in enumerate(cells):
        parts = [
            cell.rjust(w) if col_idx in right_set else cell.ljust(w)
            for col_idx, (cell, w) in enumerate(zip(row, widths, strict=True))
        ]
        lines.append("  ".join(parts).rstrip())
        if row_idx == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def render_tail(report: Mapping[str, Any]) -> str:
    counts = report["counts"]
    cost = report.get("total_cost_usd")
    cost_str = "n/a" if cost is None else f"${float(cost):.4f}"
    lines = [
        f"target {sanitize_surrogates(str(report['target']))} at {report['now']} (stall={report['stall_seconds']:g}s)",
        f"trials: total={counts['total']} running={counts['running']} finished={counts['finished']} "
        f"failed={counts['failed']} unreadable={counts['unreadable']}",
        f"events={report['total_events']} partial_bytes={report['total_partial_bytes']} "
        f"resets={report['total_resets']} cost={cost_str} "
        f"critical={report['critical_alerts']} warn={report['warn_alerts']} stopped={report['stopped']}",
    ]
    trials = report.get("trials") or []
    if trials:
        t_rows = [
            [
                shorten(str(t["trial"]), 36),
                t["ecosystem"],
                t["state"],
                t["events_count"],
                pct(float(t["reject_ratio"])),
                "broken" if t["chain_ok"] is False else ("ok" if t["chain_ok"] is True else "-"),
                "-" if t["reward"] is None else f"{float(t['reward']):.4f}",
                "-" if t["cost_usd"] is None else f"${float(t['cost_usd']):.4f}",
            ]
            for t in trials[:100]
        ]
        lines.extend([
            "",
            table(
                ["trial", "eco", "state", "events", "reject", "chain", "reward", "cost"],
                t_rows,
                right=(3, 4, 6, 7),
            ),
        ])
    alerts = report.get("alerts") or []
    if alerts:
        a_rows = [[a["severity"], a["code"], a.get("trial") or "-", a.get("seat") or "-", a["message"]] for a in alerts[:50]]
        lines.extend(["", table(["severity", "code", "trial", "seat", "message"], a_rows)])
    return sanitize_surrogates("\n".join(lines))


def render_detectors(report: Mapping[str, Any]) -> str:
    lines = [
        f"detectors on {sanitize_surrogates(str(report['path']))}: {report['trials_scanned']} trial(s) ({report['ecosystems']})",
        "",
    ]
    summary = report["summary"]
    s_rows = [
        [name, summary[name]["trials_flagged"], summary[name]["total_hits"], pct(summary[name]["rate"])]
        for name in report["detector_names"]
    ]
    lines.append(table(["detector", "trials_flagged", "total_hits", "rate"], s_rows, right=(1, 2, 3)))
    flagged_trials = [t for t in report["trials"] if t["triggered"]]
    if flagged_trials:
        t_rows = [
            [shorten(str(t["trial"]), 38), t["ecosystem"], t["events_count"], ", ".join(t["triggered"])]
            for t in flagged_trials[:100]
        ]
        lines.extend(["", table(["trial", "eco", "events", "triggered"], t_rows, right=(2,))])
    return sanitize_surrogates("\n".join(lines))


def render_validity(report: Mapping[str, Any]) -> str:
    verdict = "PASS" if report["passed"] else "FAIL"
    min_p = "n/a" if report["min_fisher_p"] is None else f"{float(report['min_fisher_p']):.6f}"
    lines = [
        f"validity: {verdict} (max_unmeasured_rate={pct(report['max_unmeasured_rate'])}, "
        f"alpha={report['alpha']:g}, stall={report['stall_seconds']:g}s, min_fisher_p={min_p})",
        "",
    ]
    a_rows = [
        [
            a["arm"],
            a["total_trials"],
            a["measured"],
            a["unmeasured"],
            pct(a["unmeasured_rate"]),
            num(a["mean_measured_reward"]),
            ", ".join(f"{k}:{v}" for k, v in a["attrition_by_reason"].items()) or "-",
        ]
        for a in report["arms"]
    ]
    lines.append(
        table(
            ["arm", "trials", "measured", "unmeasured", "unmeasured_rate", "mean_reward", "reasons"],
            a_rows,
            right=(1, 2, 3, 4, 5),
        )
    )
    if report["pairs"]:
        p_rows = [
            [
                p["arm_a"],
                p["arm_b"],
                f"{p['rate_diff']:+.4f}",
                f"{p['fisher_p']:.6f}",
                "YES" if p["differential_attrition"] else "no",
            ]
            for p in report["pairs"]
        ]
        lines.extend(["", table(["arm_a", "arm_b", "rate_diff", "fisher_p", "flagged"], p_rows, right=(2, 3))])
    return sanitize_surrogates("\n".join(lines))


def render_certify_baselines(report: Mapping[str, Any]) -> str:
    if "families" in report:
        verdict = "PASSED" if report["passed"] else "FAILED"
        lines = [
            f"certify-baselines suite ({report['root_dir']}): {verdict} "
            f"({report['families_passed']}/{report['families_checked']} passed, "
            f"skipped={len(report.get('skipped_families', ()))})",
            "",
        ]
        f_rows = [
            [
                f["family"],
                str(f["status"]).upper(),
                f.get("conditions_checked", 0),
                len(f.get("control_arms", ())),
                len(f.get("trap_arms", ())),
                len(f.get("findings", ())),
            ]
            for f in report["families"]
        ]
        if f_rows:
            lines.append(table(["family", "status", "conds", "ctrls", "traps", "findings"], f_rows, right=(2, 3, 4, 5)))
        return sanitize_surrogates("\n".join(lines))
    lines = [
        f"certify-baselines {report['family']} ({report['family_dir']}): {str(report['status']).upper()}",
    ]
    if report.get("error"):
        lines.append(f"error: {report['error']}")
        return sanitize_surrogates("\n".join(lines))
    margins = report.get("margins") or {}
    if margins:
        m_rows = [
            [
                pol,
                info["conditions"],
                num(info["oracle_mean"]),
                num(info["policy_mean"]),
                f"{info['mean_margin']:+.4f}",
                f"{info['min_margin']:+.4f}",
            ]
            for pol, info in sorted(margins.items())
        ]
        lines.extend([
            "",
            table(
                ["policy", "conds", "oracle_mean", "policy_mean", "mean_margin", "min_margin"],
                m_rows,
                right=(1, 2, 3, 4, 5),
            ),
        ])
    lines.append(
        f"control_arms={report.get('control_arms', [])} trap_arms={report.get('trap_arms', [])}"
    )
    findings = report.get("findings") or []
    if findings:
        f_rows = [[f["severity"], f["code"], f["message"]] for f in findings]
        lines.extend(["", table(["severity", "code", "message"], f_rows)])
    return sanitize_surrogates("\n".join(lines))
