"""Tests for Z.ai OpenCode MCP Analysis Pipeline."""

from __future__ import annotations

from pathlib import Path

from evallab.analysis_capability import (
    AnalysisStatus,
    CascadeStatus,
    CIDisposition,
    Verdict,
)
from evallab.zai_analysis import (
    TrialEvidence,
    build_seed_blocked_contrasts,
    parse_trial_directory,
    run_t1_analysis,
)

DIGEST = "sha256:" + "f" * 64


def test_parse_trial_directory_nonexistent() -> None:
    res = parse_trial_directory(Path("/tmp/nonexistent_trial_dir"), "job_1", "wave1")
    assert res is None


def test_build_seed_blocked_contrasts_empty() -> None:
    contrasts = build_seed_blocked_contrasts([])
    assert isinstance(contrasts, list)


def test_build_seed_blocked_contrasts_action_memory_and_recovery() -> None:
    t1 = TrialEvidence(
        job_name="job_action",
        trial_name="action-64k-neutral_padding-s42__abc",
        trial_dir="/tmp/t1",
        task_name="action",
        benchmark_family="action_memory",
        model_name="glm-5.3-flash",
        agent_name="opencode",
        wave="wave2",
        seed=42,
        arm="neutral_padding",
        dose="64k",
        reward=1.0,
        passed=True,
    )
    t2 = TrialEvidence(
        job_name="job_action",
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
    )
    t3 = TrialEvidence(
        job_name="job_rec",
        trial_name="recovery-transient5xx-fault-s42__ghi",
        trial_dir="/tmp/t3",
        task_name="recovery",
        benchmark_family="recovery",
        model_name="glm-5.3-flash",
        agent_name="opencode",
        wave="wave2",
        seed=42,
        arm="fault",
        factor="transient_http_5xx",
        reward=1.0,
        passed=True,
    )
    t4 = TrialEvidence(
        job_name="job_rec",
        trial_name="recovery-transient5xx-clean-s42__jkl",
        trial_dir="/tmp/t4",
        task_name="recovery",
        benchmark_family="recovery",
        model_name="glm-5.3-flash",
        agent_name="opencode",
        wave="wave2",
        seed=42,
        arm="clean_twin",
        factor="transient_http_5xx",
        reward=1.0,
        passed=True,
    )

    contrasts = build_seed_blocked_contrasts([t1, t2, t3, t4])
    assert len(contrasts) >= 2
    action_contrast = next(
        c for c in contrasts if "Action Memory 64k Neutral vs Semantic" in c.contrast_name
    )
    assert action_contrast.mean_reward_a == 1.0
    assert action_contrast.mean_reward_b == 0.0
    assert action_contrast.reward_delta == -1.0


def test_run_t1_analysis_integrations() -> None:
    # Build synthetic trial evidence set
    trials = [
        TrialEvidence(
            job_name=f"job_{i}",
            trial_name=f"trial_{i}",
            trial_dir=f"/tmp/trial_{i}",
            task_name=f"task_{i % 3}",
            benchmark_family="recovery" if i % 2 == 0 else "funcdag",
            model_name="glm-5.3-flash",
            agent_name="opencode",
            wave="wave2",
            seed=42,
            arm="fault" if i % 2 == 0 else "neutral",
            factor="persistent_signature_error",
            reward=1.0 if i % 3 != 0 else 0.0,
            passed=(i % 3 != 0),
            step_count=10,
            tool_call_count=5,
            prompt_tokens=1000 * i,
            completion_tokens=50 * i,
            cached_tokens=200 * i,
            first_error_step=2 if i % 3 == 0 else 1,
            lock_step=10 if i % 3 == 0 else None,
            censor_step=10 if i % 3 != 0 else None,
            lock_event_observed=(i % 3 == 0),
            right_censored=(i % 3 != 0),
            lock_predicate_id="pred_v1" if i % 3 == 0 else None,
            lock_evidence_ref="cit:1" if i % 3 == 0 else None,
            result_digest=f"sha256:{str(i).zfill(64)}",
        )
        for i in range(25)
    ]

    out = run_t1_analysis(trials)
    assert "t11_report" in out
    assert "t12_result" in out
    assert "t13_report" in out

    t11 = out["t11_report"]
    # Check known-positive static lineage violations
    res_val = next(r for r in t11.results if r.feature_name == "value_propagation_accuracy")
    assert res_val.verdict == Verdict.LINEAGE_VIOLATION
    assert res_val.ci_disposition == CIDisposition.BLOCK

    t12 = out["t12_result"]
    assert t12.status in (AnalysisStatus.VALID, AnalysisStatus.REFUSAL)

    t13 = out["t13_report"]
    assert len(t13.results) == 25
    observed = [r for r in t13.results if r.status == CascadeStatus.OBSERVED]
    censored = [r for r in t13.results if r.status == CascadeStatus.CENSORED]
    assert len(observed) > 0
    assert len(censored) > 0
