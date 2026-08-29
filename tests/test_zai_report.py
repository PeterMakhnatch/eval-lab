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
    assert "glm-5.3-highspeed" not in summary_json["models"]
    assert "t1_capabilities" in summary_json

    md_report = generate_calibrated_markdown_report(trials, contrasts, t1_out)
    assert "# Z.ai OpenCode MCP Experiment Program" in md_report
    assert "LINEAGE_VIOLATION" in md_report
    assert "GLM-5.3 Full vs. Flash" in md_report
    assert "14,137,819 prompt tokens" in md_report
    assert "~16–18x prompt expansion" in md_report
    assert "23 minutes 50 seconds" in md_report
    assert "No general effectiveness claim is made." in md_report

    # Assert no contrast has n=0
    for c in contrasts:
        assert len(c.trials_arm_a) > 0
        assert len(c.trials_arm_b) > 0

    manifest = generate_source_manifest(trials)
    assert manifest["entry_count"] == 2
    assert len(manifest["entries"]) == 2


def test_full_vs_flash_paired_contrasts() -> None:
    paired_trials = [
        # Task 1: funcdag_depth5_s42
        TrialEvidence(
            job_name="zai-wave2-funcdag-depth5-s42",
            trial_name="mcp-funcdag-depth_5-seed42__PBZKQYS",
            trial_dir="/tmp/t1",
            task_name="funcdag",
            benchmark_family="funcdag",
            model_name="glm-5.3-flash",
            agent_name="opencode",
            wave="wave2",
            seed=42,
            dose="depth_5",
            reward=1.0,
            passed=True,
        ),
        TrialEvidence(
            job_name="zai-wave2-glm53-funcdag-canary",
            trial_name="mcp-funcdag-depth_5-seed42__Ca9ToPk",
            trial_dir="/tmp/t2",
            task_name="funcdag",
            benchmark_family="funcdag",
            model_name="glm-5.3-full",
            agent_name="opencode",
            wave="wave2",
            seed=42,
            dose="depth_5",
            reward=1.0,
            passed=True,
        ),
        # Task 2: action_64k_semantic_s42
        TrialEvidence(
            job_name="zai-wave2-flash-matrix",
            trial_name="action-64k-semantic_distractor-s__CrxsJ57",
            trial_dir="/tmp/t3",
            task_name="action",
            benchmark_family="action_memory",
            model_name="glm-5.3-flash",
            agent_name="opencode",
            wave="wave2",
            seed=42,
            arm="semantic_distractor",
            dose="64k",
            reward=1.0,
            passed=True,
        ),
        TrialEvidence(
            job_name="zai-wave2-glm53-paired-mini",
            trial_name="action-64k-semantic_distractor-s__ZppWXWx",
            trial_dir="/tmp/t4",
            task_name="action",
            benchmark_family="action_memory",
            model_name="glm-5.3-full",
            agent_name="opencode",
            wave="wave2",
            seed=42,
            arm="semantic_distractor",
            dose="64k",
            reward=0.0,
            passed=False,
        ),
        # Task 3: recovery_persistent_s42_fault
        TrialEvidence(
            job_name="zai-wave2-flash-matrix",
            trial_name="recovery-persistent-signature-er__Lm9ohzb",
            trial_dir="/tmp/t5",
            task_name="recovery",
            benchmark_family="recovery",
            model_name="glm-5.3-flash",
            agent_name="opencode",
            wave="wave2",
            seed=42,
            arm="fault",
            factor="persistent_signature_error",
            reward=1.0,
            passed=True,
        ),
        TrialEvidence(
            job_name="zai-wave2-glm53-paired-mini",
            trial_name="recovery-persistent-signature-er__pgAA5NR",
            trial_dir="/tmp/t6",
            task_name="recovery",
            benchmark_family="recovery",
            model_name="glm-5.3-full",
            agent_name="opencode",
            wave="wave2",
            seed=42,
            arm="fault",
            factor="persistent_signature_error",
            reward=1.0,
            passed=True,
        ),
    ]

    contrasts = build_seed_blocked_contrasts(paired_trials)
    paired_contrasts = [c for c in contrasts if "Paired Model Contrast" in c.contrast_name]
    assert len(paired_contrasts) == 3
    for c in paired_contrasts:
        assert len(c.trials_arm_a) == 1
        assert len(c.trials_arm_b) == 1
        assert c.arm_a_label == "GLM-5.3-Flash"
        assert c.arm_b_label == "GLM-5.3 Full"

    t1_c = next(c for c in paired_contrasts if "funcdag_depth5_s42" in c.contrast_name)
    assert t1_c.mean_reward_a == 1.0
    assert t1_c.mean_reward_b == 1.0
    assert t1_c.reward_delta == 0.0

    t2_c = next(c for c in paired_contrasts if "action_64k_semantic_s42" in c.contrast_name)
    assert t2_c.mean_reward_a == 1.0
    assert t2_c.mean_reward_b == 0.0
    assert t2_c.reward_delta == -1.0

    t3_c = next(c for c in paired_contrasts if "recovery_persistent_s42_fault" in c.contrast_name)
    assert t3_c.mean_reward_a == 1.0
    assert t3_c.mean_reward_b == 1.0
    assert t3_c.reward_delta == 0.0
