"""Tests for Z.ai Report Generator."""

from __future__ import annotations

from evallab.zai_analysis import (
    TrialEvidence,
    build_seed_blocked_contrasts,
    run_t1_analysis,
)
from evallab.zai_report import (
    generate_calibrated_markdown_report,
    generate_source_manifest,
    generate_summary_json,
)


def test_generate_report_and_summary_smoke() -> None:
    trials = [
        TrialEvidence(
            job_name="job_wave1",
            trial_name="action-neutral16k__abc",
            trial_dir="/tmp/t1",
            task_name="action",
            benchmark_family="action_memory",
            model_name="glm-5.3-flash",
            agent_name="opencode",
            wave="wave1",
            seed=42,
            arm="neutral_padding",
            dose="16k",
            reward=1.0,
            passed=True,
            step_count=8,
            tool_call_count=4,
            prompt_tokens=5000,
            completion_tokens=200,
            cached_tokens=1000,
            result_digest="sha256:" + "1" * 64,
            trajectory_digest="sha256:" + "2" * 64,
        ),
        TrialEvidence(
            job_name="job_wave2",
            trial_name="action-64k-semantic_distractor-s42__def",
            trial_dir="/tmp/t2",
            task_name="action",
            benchmark_family="action_memory",
            model_name="glm-5.3-flash",
            agent_name="opencode",
            wave="wave2",
            seed=42,
            arm="semantic_distractor",
            dose="64k",
            reward=0.0,
            passed=False,
            step_count=10,
            tool_call_count=12,
            prompt_tokens=20000,
            completion_tokens=800,
            cached_tokens=5000,
            first_error_step=3,
            lock_step=10,
            lock_event_observed=True,
            lock_predicate_id="pred_1",
            lock_evidence_ref="cit:1",
            result_digest="sha256:" + "3" * 64,
            trajectory_digest="sha256:" + "4" * 64,
        ),
    ]

    contrasts = build_seed_blocked_contrasts(trials)
    t1_out = run_t1_analysis(trials)

    summary_json = generate_summary_json(trials, contrasts, t1_out)
    assert summary_json["total_trials"] == 2
    assert "glm-5.3-flash" in summary_json["models"]
    assert "t1_capabilities" in summary_json

    md_report = generate_calibrated_markdown_report(trials, contrasts, t1_out)
    assert "# Z.ai OpenCode MCP Experiment Program" in md_report
    assert "LINEAGE_VIOLATION" in md_report

    manifest = generate_source_manifest(trials)
    assert manifest["entry_count"] == 2
    assert len(manifest["entries"]) == 2
