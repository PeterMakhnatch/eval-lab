"""Analyst and analysis-worker refusal checks independent of CLI transports."""

from __future__ import annotations

from pathlib import Path

from evallab.analysis_worker import (
    AnalysisWorker,
    _no_adapter,
    default_worker,
)


def test_analysis_worker_defers_with_no_adapter(tmp_path: Path) -> None:
    # Verify _no_adapter raises RuntimeError
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "standing-approvals.yaml").write_text(
        """version: 1
daily_cost_ceiling_usd: 20
per_job_cost_ceiling_usd: 3
quiet_failure_rule: 3
refuse_billable_at_used_percent: null
auto_run:
  - name: local-controls
    agents: [oracle, nop]
escalate_to_human:
  - any_billable_agent
"""
    )
    (tmp_path / "derived" / "analyses" / "worker").mkdir(parents=True, exist_ok=True)

    worker = default_worker(tmp_path)
    assert isinstance(worker, AnalysisWorker)
    assert worker.adapter is _no_adapter

