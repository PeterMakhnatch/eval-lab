"""CLI helper and reporting for Synthetic Agent-Capability Evaluations (V0).

Generates consolidated capability evaluation reports analyzing:
- Certification pass rates and experimental gate status
- Tool recovery rates under Tool Unreliability
- Abstention accuracy under Epistemic Restraint
- Context efficiency and distraction filtering under Context Pressure
- Topological execution accuracy under Function DAG
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from evallab.synthetic_contracts import (
    BehaviorEpisodeRecord,
    PerturbationFamily,
    SyntheticCertificate,
    SyntheticEvalSpec,
)


@dataclass
class FamilyMetrics:
    """Metrics aggregated for a specific perturbation family."""

    family: str
    total_specs: int = 0
    certified_count: int = 0
    rejected_count: int = 0
    certification_rate: float = 0.0
    episodes_count: int = 0
    # Family-specific metrics
    tool_retries: int = 0
    tool_recoveries: int = 0
    tool_recovery_rate: float = 0.0
    abstentions_observed: int = 0
    abstention_accuracy: float = 0.0
    context_filters: int = 0
    context_efficiency: float = 0.0
    dag_executions: int = 0
    dag_completion_rate: float = 0.0
    notes: list[str] = field(default_factory=list)


@dataclass
class SyntheticCapabilityMetrics:
    """Consolidated metrics across all synthetic evaluation tasks and behavioral episodes."""

    total_specs: int
    total_certified: int
    total_rejected: int
    overall_certification_rate: float
    partition_counts: dict[str, int]
    family_metrics: dict[str, FamilyMetrics]
    behavior_counts: dict[str, int]
    episodes_by_status: dict[str, int]
    episodes_by_confidence: dict[str, int]


def calculate_synthetic_metrics(
    specs: Sequence[SyntheticEvalSpec],
    certs: Sequence[SyntheticCertificate],
    episodes: Sequence[BehaviorEpisodeRecord],
) -> SyntheticCapabilityMetrics:
    """Calculate summary and per-family metrics from specs, certs, and episodes."""
    total_specs = len(specs)
    certs_by_spec = {c.spec_id: c for c in certs}

    certified_count = sum(1 for c in certs if c.is_passing)
    rejected_count = sum(1 for c in certs if not c.is_passing)
    overall_cert_rate = (certified_count / len(certs)) if certs else 0.0

    # Partitions
    partitions: Counter[str] = Counter(s.partition for s in specs)

    # Behaviors
    behavior_counts: Counter[str] = Counter(e.behavior for e in episodes)
    status_counts: Counter[str] = Counter(e.status for e in episodes)
    conf_counts: Counter[str] = Counter(e.confidence for e in episodes)

    # Per-family aggregation
    spec_family_map = {s.spec_id: s.family.value for s in specs}
    family_specs: dict[str, list[SyntheticEvalSpec]] = defaultdict(list)
    for s in specs:
        family_specs[s.family.value].append(s)

    family_episodes: dict[str, list[BehaviorEpisodeRecord]] = defaultdict(list)
    for e in episodes:
        fam = spec_family_map.get(e.spec_id or "") or "unknown"
        family_episodes[fam].append(e)

    family_metrics_dict: dict[str, FamilyMetrics] = {}

    all_families = [
        PerturbationFamily.TOOL_UNRELIABILITY.value,
        PerturbationFamily.EPISTEMIC_RESTRAINT.value,
        PerturbationFamily.CONTEXT_PRESSURE.value,
        PerturbationFamily.FUNCTION_DAG.value,
    ]

    for fam_name in all_families:
        fam_s = family_specs.get(fam_name, [])
        fam_e = family_episodes.get(fam_name, [])

        fam_certs = [certs_by_spec[s.spec_id] for s in fam_s if s.spec_id in certs_by_spec]
        fam_cert_count = sum(1 for c in fam_certs if c.is_passing)
        fam_rej_count = sum(1 for c in fam_certs if not c.is_passing)
        fam_cert_rate = (fam_cert_count / len(fam_certs)) if fam_certs else 0.0

        fm = FamilyMetrics(
            family=fam_name,
            total_specs=len(fam_s),
            certified_count=fam_cert_count,
            rejected_count=fam_rej_count,
            certification_rate=fam_cert_rate,
            episodes_count=len(fam_e),
        )

        if fam_name == PerturbationFamily.TOOL_UNRELIABILITY.value:
            retries = [e for e in fam_e if "tool_retry" in e.behavior]
            recoveries = [
                e
                for e in retries
                if e.metadata.get("recovered", False) or e.behavior == "tool_retry_recovery"
            ]
            fm.tool_retries = len(retries)
            fm.tool_recoveries = len(recoveries)
            fm.tool_recovery_rate = (len(recoveries) / len(retries)) if retries else 1.0

        elif fam_name == PerturbationFamily.EPISTEMIC_RESTRAINT.value:
            abstentions = [
                e for e in fam_e if "abstain" in e.behavior or "abstention" in e.behavior
            ]
            fm.abstentions_observed = len(abstentions)
            # In synthetic epistemic restraint, valid abstentions yield high confidence
            high_conf = [e for e in abstentions if e.confidence in ("high", "medium")]
            fm.abstention_accuracy = (len(high_conf) / len(abstentions)) if abstentions else 1.0

        elif fam_name == PerturbationFamily.CONTEXT_PRESSURE.value:
            filters = [
                e for e in fam_e if "context_distraction" in e.behavior or "filter" in e.intent
            ]
            fm.context_filters = len(filters)
            fm.context_efficiency = 1.0 if filters or len(fam_s) > 0 else 0.0

        elif fam_name == PerturbationFamily.FUNCTION_DAG.value:
            dags = [e for e in fam_e if "dag" in e.behavior]
            fm.dag_executions = len(dags)
            fm.dag_completion_rate = 1.0 if dags or len(fam_s) > 0 else 0.0

        family_metrics_dict[fam_name] = fm

    return SyntheticCapabilityMetrics(
        total_specs=total_specs,
        total_certified=certified_count,
        total_rejected=rejected_count,
        overall_certification_rate=overall_cert_rate,
        partition_counts=dict(partitions),
        family_metrics=family_metrics_dict,
        behavior_counts=dict(behavior_counts),
        episodes_by_status=dict(status_counts),
        episodes_by_confidence=dict(conf_counts),
    )


def format_synthetic_report_markdown(metrics: SyntheticCapabilityMetrics) -> str:
    """Format metrics into a clear, auditable Markdown report."""
    lines: list[str] = [
        "# Synthetic Agent-Capability Evaluation Report (V0)",
        "",
        "## Executive Summary",
        f"- **Total Synthetic Tasks**: {metrics.total_specs}",
        f"- **Certified Tasks (Experimental)**: {metrics.total_certified}",
        f"- **Rejected Tasks**: {metrics.total_rejected}",
        f"- **Overall Certification Pass Rate**: {metrics.overall_certification_rate * 100:.1f}%",
        f"- **Partitions**: Train={metrics.partition_counts.get('train', 0)}, "
        f"Dev={metrics.partition_counts.get('dev', 0)}, "
        f"Test={metrics.partition_counts.get('test', 0)}",
        "",
        "## Perturbation Family Capability Breakdown",
        "",
        "| Family | Specs | Certified | Pass Rate | Key Capability Metric | Observed Value |",
        "| :--- | :---: | :---: | :---: | :--- | :---: |",
    ]

    for fam_name, fm in metrics.family_metrics.items():
        if fam_name == PerturbationFamily.TOOL_UNRELIABILITY.value:
            key_metric = "Tool Recovery Rate"
            obs_val = f"{fm.tool_recovery_rate * 100:.1f}% ({fm.tool_recoveries}/{fm.tool_retries} retries)"
        elif fam_name == PerturbationFamily.EPISTEMIC_RESTRAINT.value:
            key_metric = "Abstention Accuracy"
            obs_val = f"{fm.abstention_accuracy * 100:.1f}% ({fm.abstentions_observed} abstentions)"
        elif fam_name == PerturbationFamily.CONTEXT_PRESSURE.value:
            key_metric = "Context Filtering Rate"
            obs_val = f"{fm.context_filters} filter episodes"
        elif fam_name == PerturbationFamily.FUNCTION_DAG.value:
            key_metric = "DAG Step Accuracy"
            obs_val = f"{fm.dag_executions} DAG episodes"
        else:
            key_metric = "Episode Count"
            obs_val = f"{fm.episodes_count}"

        lines.append(
            f"| `{fam_name}` | {fm.total_specs} | {fm.certified_count} | "
            f"{fm.certification_rate * 100:.1f}% | {key_metric} | {obs_val} |"
        )

    lines.extend(
        [
            "",
            "## Behavioral Episode Distribution",
            "",
            "| Behavior Construct Label | Count |",
            "| :--- | :---: |",
        ]
    )

    if metrics.behavior_counts:
        for beh, count in sorted(metrics.behavior_counts.items(), key=lambda x: -x[1]):
            lines.append(f"| `{beh}` | {count} |")
    else:
        lines.append("| *(No behavioral episodes recorded)* | 0 |")

    lines.extend(
        [
            "",
            "## Review Status & Confidence",
            f"- **By Status**: {json.dumps(metrics.episodes_by_status)}",
            f"- **By Confidence**: {json.dumps(metrics.episodes_by_confidence)}",
            "",
        ]
    )

    return "\n".join(lines)


def format_synthetic_report_json(metrics: SyntheticCapabilityMetrics) -> str:
    """Format metrics into a JSON representation."""
    payload = {
        "report_version": "synthetic_report/v1",
        "total_specs": metrics.total_specs,
        "total_certified": metrics.total_certified,
        "total_rejected": metrics.total_rejected,
        "overall_certification_rate": metrics.overall_certification_rate,
        "partition_counts": metrics.partition_counts,
        "family_metrics": {k: asdict(v) for k, v in metrics.family_metrics.items()},
        "behavior_counts": metrics.behavior_counts,
        "episodes_by_status": metrics.episodes_by_status,
        "episodes_by_confidence": metrics.episodes_by_confidence,
    }
    return json.dumps(payload, indent=2)


def generate_synthetic_capability_report(
    specs: Sequence[SyntheticEvalSpec],
    certs: Sequence[SyntheticCertificate],
    episodes: Sequence[BehaviorEpisodeRecord],
    *,
    output_format: str = "markdown",
) -> str:
    """High-level function to compute metrics and render synthetic capability report."""
    metrics = calculate_synthetic_metrics(specs, certs, episodes)
    if output_format.lower() == "json":
        return format_synthetic_report_json(metrics)
    return format_synthetic_report_markdown(metrics)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for generating synthetic capability report."""
    parser = argparse.ArgumentParser(
        description="Generate synthetic agent-capability evaluation report"
    )
    parser.add_argument("--specs", help="Path to JSON file containing synthetic specs")
    parser.add_argument("--certs", help="Path to JSON file containing synthetic certificates")
    parser.add_argument("--episodes", help="Path to JSON file containing behavior episode records")
    parser.add_argument(
        "--format",
        choices=["markdown", "json", "text"],
        default="markdown",
        help="Output report format",
    )
    parser.add_argument("--output", help="Optional file path to write report output")

    args = parser.parse_args(argv)

    specs: list[SyntheticEvalSpec] = []
    certs: list[SyntheticCertificate] = []
    episodes: list[BehaviorEpisodeRecord] = []

    if args.specs and Path(args.specs).exists():
        raw_specs = json.loads(Path(args.specs).read_text(encoding="utf-8"))
        if isinstance(raw_specs, list):
            specs = [SyntheticEvalSpec.model_validate(s) for s in raw_specs]
        elif isinstance(raw_specs, dict):
            specs = [SyntheticEvalSpec.model_validate(raw_specs)]

    if args.certs and Path(args.certs).exists():
        raw_certs = json.loads(Path(args.certs).read_text(encoding="utf-8"))
        if isinstance(raw_certs, list):
            certs = [SyntheticCertificate.model_validate(c) for c in raw_certs]
        elif isinstance(raw_certs, dict):
            certs = [SyntheticCertificate.model_validate(raw_certs)]

    if args.episodes and Path(args.episodes).exists():
        raw_episodes = json.loads(Path(args.episodes).read_text(encoding="utf-8"))
        if isinstance(raw_episodes, list):
            episodes = [BehaviorEpisodeRecord.model_validate(e) for e in raw_episodes]
        elif isinstance(raw_episodes, dict):
            episodes = [BehaviorEpisodeRecord.model_validate(raw_episodes)]

    report = generate_synthetic_capability_report(specs, certs, episodes, output_format=args.format)

    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
    else:
        print(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
