"""Trajectory Readiness and HOLD Audit Report Generator.

Evaluates every durable trajectory across research/evidence/runs/ and derived/canary-runs/,
building TrajectoryIR and EvidencePack to report:
- Analysis-ready vs HOLD counts and ratios
- Exact category coverage metrics (raw source, events, episodes, errors, state, verifier, linkage)
- Distinct hold reasons (missing_atif, degraded_linkage, quarantine_status, empty_events)
- Human-readable markdown audit summary and structured JSON artifact
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.interpretation.evidence_pack import (
    build_evidence_pack,
)
from evallab.interpretation.trajectory_ir import (
    build_trajectory_ir,
)


@dataclass(frozen=True)
class TrialReadinessRecord:
    """Readiness status and coverage breakdown for a single evaluated trajectory."""

    trial_id: str
    trial_name: str
    job_name: str
    task_name: str
    agent_scaffold: str
    model_name: str
    final_verdict: str
    primary_reward: float | None
    is_analysis_ready: bool
    hold_reasons: tuple[str, ...]
    ir_digest: str
    pack_digest: str
    is_model_callable: bool
    consumed_tokens_est: int
    coverage_metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "trial_name": self.trial_name,
            "job_name": self.job_name,
            "task_name": self.task_name,
            "agent_scaffold": self.agent_scaffold,
            "model_name": self.model_name,
            "final_verdict": self.final_verdict,
            "primary_reward": self.primary_reward,
            "is_analysis_ready": self.is_analysis_ready,
            "hold_reasons": list(self.hold_reasons),
            "ir_digest": self.ir_digest,
            "pack_digest": self.pack_digest,
            "is_model_callable": self.is_model_callable,
            "consumed_tokens_est": self.consumed_tokens_est,
            "coverage_metrics": self.coverage_metrics,
        }


@dataclass(frozen=True)
class BatchReadinessReport:
    """Comprehensive batch readiness report across durable evaluated trajectories."""

    report_id: str
    created_at: str
    total_trials_scanned: int
    analysis_ready_count: int
    hold_count: int
    analysis_ready_ratio: float
    hold_reasons_distribution: dict[str, int]
    task_families_distribution: dict[str, int]
    verdicts_distribution: dict[str, int]
    trial_records: tuple[TrialReadinessRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "created_at": self.created_at,
            "total_trials_scanned": self.total_trials_scanned,
            "analysis_ready_count": self.analysis_ready_count,
            "hold_count": self.hold_count,
            "analysis_ready_ratio": self.analysis_ready_ratio,
            "hold_reasons_distribution": self.hold_reasons_distribution,
            "task_families_distribution": self.task_families_distribution,
            "verdicts_distribution": self.verdicts_distribution,
            "trial_records": [r.to_dict() for r in self.trial_records],
        }

    def render_markdown(self) -> str:
        """Render human-readable Markdown summary table and HOLD audit details."""
        lines: list[str] = []
        lines.append(f"# Batch Trajectory Readiness & HOLD Report ({self.report_id})")
        lines.append(f"**Generated:** {self.created_at}")
        lines.append(
            f"**Headline Summary:** {self.total_trials_scanned} total trials scanned | "
            f"**Analysis Ready:** {self.analysis_ready_count} ({self.analysis_ready_ratio * 100:.1f}%) | "
            f"**HOLD:** {self.hold_count} ({(1.0 - self.analysis_ready_ratio) * 100:.1f}%)"
        )
        lines.append("")

        lines.append("## Hold Reasons Distribution")
        if self.hold_reasons_distribution:
            for reason, count in sorted(self.hold_reasons_distribution.items(), key=lambda x: -x[1]):
                lines.append(f"- **`{reason}`:** {count} trial(s)")
        else:
            lines.append("- *(None: all scanned trials are analysis ready)*")
        lines.append("")

        lines.append("## Task Families & Verdicts")
        lines.append(f"- **Task Families:** {', '.join(f'{k} ({v})' for k, v in self.task_families_distribution.items())}")
        lines.append(f"- **Verdicts:** {', '.join(f'{k} ({v})' for k, v in self.verdicts_distribution.items())}")
        lines.append("")

        lines.append("## Detailed Trial Inventory")
        lines.append("| Trial Name | Task Family | Outcome | Ready? | Tokens | HOLD Reasons | IR Digest |")
        lines.append("|---|---|---|---|---|---|---|")
        for rec in self.trial_records:
            ready_str = "✅ READY" if rec.is_analysis_ready else "⚠️ HOLD"
            reasons_str = ", ".join(rec.hold_reasons) if rec.hold_reasons else "-"
            ir_short = rec.ir_digest[:16] + "..." if rec.ir_digest else "-"
            reward_str = f" ({rec.primary_reward})" if rec.primary_reward is not None else ""
            lines.append(
                f"| `{rec.trial_name}` | `{rec.task_name}` | {rec.final_verdict}{reward_str} | {ready_str} | "
                f"{rec.consumed_tokens_est:,} | {reasons_str} | `{ir_short}` |"
            )
        lines.append("")

        return "\n".join(lines)


def audit_durable_trajectories(
    runs_roots: Sequence[Path] | None = None,
    *,
    repo_root: Path | None = None,
    store_root: Path | None = None,
) -> BatchReadinessReport:
    """Scan all durable runs, build IR and EvidencePack, and generate the batch readiness report."""
    root = (repo_root or Path.cwd()).resolve()

    candidate_roots: list[Path] = []
    if runs_roots:
        candidate_roots.extend(runs_roots)
    else:
        # Default durable run discovery paths
        candidates = [
            root / "research" / "evidence" / "runs",
            root / "derived" / "canary-runs",
            root / "runs",
        ]
        for c in candidates:
            if c.is_dir():
                candidate_roots.append(c)

    discovered_trial_dirs: set[Path] = set()
    for c_root in candidate_roots:
        for job_dir in c_root.iterdir():
            if not job_dir.is_dir() or job_dir.name.startswith("."):
                continue
            for trial_dir in job_dir.iterdir():
                if trial_dir.is_dir() and not trial_dir.name.startswith("."):
                    discovered_trial_dirs.add(trial_dir)

    trial_records: list[TrialReadinessRecord] = []
    hold_reasons_counter: Counter[str] = Counter()
    task_counter: Counter[str] = Counter()
    verdict_counter: Counter[str] = Counter()

    for t_dir in sorted(discovered_trial_dirs, key=lambda p: str(p)):
        try:
            ir = build_trajectory_ir(t_dir, repo_root=root, store_root=store_root)
            pack = build_evidence_pack(ir, trial_dir=t_dir, repo_root=root, store_root=store_root)
            coverage = pack.evidence_coverage

            is_ready = bool(coverage.get("analysis_ready", True))
            hold_reasons = tuple(coverage.get("hold_reasons", []))

            for hr in hold_reasons:
                hold_reasons_counter[hr] += 1
            task_counter[ir.task_name] += 1
            verdict_counter[ir.final_verdict] += 1

            trial_records.append(
                TrialReadinessRecord(
                    trial_id=ir.trial_id,
                    trial_name=ir.trial_name,
                    job_name=ir.job_name,
                    task_name=ir.task_name,
                    agent_scaffold=ir.agent_scaffold,
                    model_name=ir.model_name,
                    final_verdict=ir.final_verdict,
                    primary_reward=ir.primary_reward,
                    is_analysis_ready=is_ready,
                    hold_reasons=hold_reasons,
                    ir_digest=ir.ir_digest,
                    pack_digest=pack.pack_digest,
                    is_model_callable=pack.is_model_callable,
                    consumed_tokens_est=pack.consumed_tokens_est,
                    coverage_metrics=coverage,
                )
            )
        except Exception as exc:
            hold_reasons_counter[f"build_exception_{type(exc).__name__}"] += 1
            trial_records.append(
                TrialReadinessRecord(
                    trial_id=t_dir.name,
                    trial_name=t_dir.name,
                    job_name=t_dir.parent.name,
                    task_name="unknown",
                    agent_scaffold="unknown",
                    model_name="unknown",
                    final_verdict=f"EXCEPTION ({type(exc).__name__})",
                    primary_reward=None,
                    is_analysis_ready=False,
                    hold_reasons=(f"build_exception_{type(exc).__name__}: {exc}",),
                    ir_digest="",
                    pack_digest="",
                    is_model_callable=False,
                    consumed_tokens_est=0,
                    coverage_metrics={"analysis_ready": False, "hold_reasons": [f"build_exception: {exc}"]},
                )
            )

    total_scanned = len(trial_records)
    ready_count = sum(1 for r in trial_records if r.is_analysis_ready)
    hold_count = total_scanned - ready_count
    ratio = ready_count / total_scanned if total_scanned > 0 else 0.0

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report_id = f"readiness-audit-{int(time.time())}"

    return BatchReadinessReport(
        report_id=report_id,
        created_at=now_iso,
        total_trials_scanned=total_scanned,
        analysis_ready_count=ready_count,
        hold_count=hold_count,
        analysis_ready_ratio=round(ratio, 4),
        hold_reasons_distribution=dict(hold_reasons_counter),
        task_families_distribution=dict(task_counter),
        verdicts_distribution=dict(verdict_counter),
        trial_records=tuple(trial_records),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit durable trajectories for batch analysis readiness and HOLD status.")
    parser.add_argument("--runs-root", type=Path, action="append", help="Specific runs root directory to scan")
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of Markdown")
    parser.add_argument("--output", type=Path, help="Write output to file path")
    args = parser.parse_args()

    report = audit_durable_trajectories(args.runs_root)

    rendered = json.dumps(report.to_dict(), indent=2) if args.json else report.render_markdown()

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Wrote batch readiness report to {args.output}")
    else:
        print(rendered)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
