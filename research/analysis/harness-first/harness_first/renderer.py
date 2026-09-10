from __future__ import annotations

import html
from typing import Any


def cell(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_markdown_report(report: dict[str, Any]) -> str:
    meta, summary = report["metadata"], report["summary"]
    kind = meta["evidence_kind"]
    lines = [
        f"# Harness comparison: {cell(meta['spec_id'])}",
        "",
        f"> Evidence: **{kind}** — {cell(meta['specimen_origin'])}.",
        "> Descriptive whole-agent configuration comparison. No significance, causal benefit, or matched-compute claim.",
        "> CPU fixtures are software checks, never measured model improvement. Historical data remain retrospective.",
        "",
        f"Analysis repository revision: `{meta['analysis_identity']['repository_revision']}` (not a model revision).",
        f"Input spec SHA256: `{meta['spec_sha256']}`. Pairing: `{meta['pairing_key']}`.",
        f"Success threshold: {cell(meta['pass_threshold'])}. Raw rewards remain visible when exceptions suppress effective outcomes.",
        "",
        "## Coverage and outcome denominators",
        "",
        "| Measure | Observed value |",
        "|---|---|",
    ]
    for field, value in {**summary["counts_and_denominators"], **summary["outcomes"]}.items():
        lines.append(f"| {cell(field)} | {cell(value)} |")
    lines += [
        "",
        "## Paired outcomes",
        "",
        "| Task/key | Pairing | Baseline effective/raw | Candidate effective/raw | Effective delta | Qualification |",
        "|---|---|---|---|---|---|",
    ]
    for pair in report["per_task_pairs"]:
        before, after = pair.get("baseline") or {}, pair.get("candidate") or {}
        delta = pair.get("delta") or {}
        qualification = pair["qualification"]
        reason = (
            ", ".join(qualification["disqualification_reasons"])
            or qualification["model_qualification"]
        )
        lines.append(
            f"| {cell(pair['pairing_key_value'])} | {cell(pair['pairing_status'])} | "
            f"{cell(before.get('effective_reward'))}/{cell(before.get('raw_reward'))} | "
            f"{cell(after.get('effective_reward'))}/{cell(after.get('raw_reward'))} | "
            f"{cell(delta.get('effective_reward_delta'))} | {cell(reason)} |"
        )
    lines += [
        "",
        "## Per-task outcomes, compute and failure evidence",
        "",
        "Role amounts below sum retained, disjoint ATIF generation-step metrics only. They do not establish full physical request coverage or actual cash billing. Inclusive trajectory final_metrics are not added to worker totals. Missing worker traces are not zero worker usage. Native aggregate values remain separately reported with unknown role scope.",
    ]
    for pair in report["per_task_pairs"]:
        lines += [
            "",
            f"### {cell(pair['pairing_key_value'])}",
            "",
            "| Arm / trial | Root identity | Harness | Effective/raw reward | Wall/agent seconds | Failure category / class |",
            "|---|---|---|---|---|---|",
        ]
        observations = []
        for side in ("baseline", "candidate"):
            duplicates = pair.get(f"{side}_duplicate_attempts") or []
            selected = duplicates or ([pair[side]] if pair.get(side) else [])
            for trial in selected:
                observations.append((side, trial))
                lines.append(
                    f"| {side} / {cell(trial['trial_id'])} | "
                    f"{cell(trial['model_name'])}@{cell(trial.get('model_revision'))} | "
                    f"{cell(trial['agent_name'])}@{cell(trial['agent_version'])} | "
                    f"{cell(trial['effective_reward'])}/{cell(trial['raw_reward'])} | "
                    f"{cell(trial['wall_time_seconds'])}/{cell(trial['agent_execution_seconds'])} | "
                    f"{cell(trial['exception_category'])} / {cell(trial['exception_class'])} |"
                )
        lines += [
            "",
            "| Arm / scope | Input / output tokens | Cached input (subset) | Reported USD | Coverage |",
            "|---|---|---|---|---|",
        ]
        for side, trial in observations:
            for scope in ("root_usage", "worker_usage", "total_usage", "native_aggregate_usage"):
                usage = trial[scope]
                lines.append(
                    f"| {side} / {scope} | {cell(usage['input_tokens'])}/{cell(usage['output_tokens'])} | "
                    f"{cell(usage['cache_tokens'])} | {cell(usage['cost_usd'])} | {cell(usage['coverage_reason'])} |"
                )
            lines.append(
                f"\n{side} source: `{cell(trial['source_path'])}`; verifier strength: {cell(trial['verifier_strength'])}; "
                f"record status: {cell(trial.get('record_status'))}; issues: {cell(trial.get('issues', []))}."
            )
            if trial.get("source_native_accounting"):
                lines.append(
                    f"\n{side} source-native accounting: {cell(trial['source_native_accounting'])}."
                )
    lines += [
        "",
        "## Cohort compute, including failed attempts",
        "",
        "A complete total is null unless every selected record has that metric. Observed subtotals and covered-record counts are separate; they are not extrapolated totals.",
        "",
        "| Arm / scope / metric | Complete total | Observed subtotal | Covered / selected |",
        "|---|---|---|---|",
    ]
    for side in ("baseline", "candidate"):
        cohort = summary[f"{side}_cohort_summary"]
        for scope in ("native_aggregate_usage", "root_usage", "worker_usage", "total_usage"):
            for metric, coverage in cohort[scope].items():
                lines.append(
                    f"| {side} / {scope} / {metric} | {cell(coverage['total'])} | "
                    f"{cell(coverage['observed_subtotal'])} | {coverage['covered_count']}/{coverage['n_total']} |"
                )
        lines += [
            f"\n{side} elapsed metrics: {cell(cohort['compute_and_timing'])}.",
            f"{side} failures: {cell(cohort['exceptions'])}.",
        ]
    lines += ["", "## Qualifications and pending inputs", ""]
    lines += [f"- {cell(warning)}" for warning in summary["warnings"]]
    lines += [
        "",
        "Exact source filenames/hashes and per-arm identities are retained in report.json.",
    ]
    return "\n".join(lines)


def render_svg_plot(report: dict[str, Any]) -> str:
    rows = report["per_task_pairs"]
    height = 180 + max(1, len(rows)) * 36

    def escape(value):
        return html.escape(str(value), quote=True)

    reward_values = [
        abs((row.get("delta") or {}).get("effective_reward_delta") or 0) for row in rows
    ]
    token_values = [
        abs((row.get("delta") or {}).get("total_input_tokens_delta") or 0) for row in rows
    ]
    reward_scale, token_scale = max([1.0, *reward_values]), max([1, *token_values])
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="{height}" viewBox="0 0 1200 {height}">',
        f"<title>{escape(report['metadata']['spec_id'])} descriptive paired diagnostics</title>",
        '<rect width="100%" height="100%" fill="#f8fafc"/>',
        '<g font-family="monospace" font-size="12" fill="#172033">',
        f'<text x="20" y="28" font-size="18">[{escape(report["metadata"]["evidence_kind"].upper())}] Descriptive paired outcomes and retained compute</text>',
        '<text x="20" y="52">No significance/causal claim. Gray rows are incomplete or unqualified, not zero effects.</text>',
        '<text x="490" y="82">Reward delta (candidate - baseline)</text>',
        '<text x="850" y="82">Retained input-token delta</text>',
    ]
    for index, row in enumerate(rows):
        y = 112 + index * 36
        label = row["pairing_key_value"]
        svg.append(
            f'<text x="20" y="{y}"><title>{escape(label)}</title>{escape(label[:52])}</text>'
        )
        for metric, center, scale in (
            ("effective_reward_delta", 640, reward_scale),
            ("total_input_tokens_delta", 1010, token_scale),
        ):
            value = (row.get("delta") or {}).get(metric)
            svg.append(
                f'<line x1="{center}" x2="{center}" y1="{y - 16}" y2="{y + 6}" stroke="#94a3b8"/>'
            )
            if value is None:
                svg.append(f'<text x="{center - 72}" y="{y}" fill="#64748b">unavailable</text>')
                continue
            length = abs(value) / scale * 115
            color = "#15803d" if value > 0 else "#c2410c" if value < 0 else "#64748b"
            if metric != "effective_reward_delta":
                color = "#2563eb"
            start = center if value >= 0 else center - length
            svg.append(
                f'<rect x="{start:.2f}" y="{y - 13}" width="{max(2, length):.2f}" height="17" fill="{color}"/>'
            )
            svg.append(f'<text x="{center + 125}" y="{y}">{value:+.4g}</text>')
    svg += [
        f'<text x="20" y="{height - 32}">Compute is retained trace evidence; full request/worker coverage and matched total budgets are not presumed.</text>',
        "</g></svg>",
    ]
    return "\n".join(svg)
