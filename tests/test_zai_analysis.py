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
        reward=1.0,
        passed=True,
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
        c for c in contrasts if "Action Memory 64k: Flash unscaffolded seed 42" in c.contrast_name
    )
    assert action_contrast.mean_reward_a == 1.0
    assert action_contrast.mean_reward_b == 1.0
    assert action_contrast.reward_delta == 0.0
    assert len(action_contrast.trials_arm_a) == 1
    assert len(action_contrast.trials_arm_b) == 1


def test_build_seed_blocked_contrasts_exact_action64_rows_and_denominators() -> None:
    # Create mock trials for all 3 Action 64k strata plus infra timeout exclusions
    trials = [
        # s42 Flash unscaffolded
        TrialEvidence(
            job_name="zai-wave2-flash-matrix",
            trial_name="action-64k-neutral_padding-s42__5Udx2Ac",
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
        ),
        TrialEvidence(
            job_name="zai-wave2-flash-matrix",
            trial_name="action-64k-semantic_distractor-s__CrxsJ57",
            trial_dir="/tmp/t2",
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
        # s1337 Flash unscaffolded matrix + repeats (3 neutral, 3 semantic)
        TrialEvidence(
            job_name="zai-wave2-flash-matrix",
            trial_name="action-64k-neutral_padding-s1337__xTVP9AZ",
            trial_dir="/tmp/t3",
            task_name="action",
            benchmark_family="action_memory",
            model_name="glm-5.3-flash",
            agent_name="opencode",
            wave="wave2",
            seed=1337,
            arm="neutral_padding",
            dose="64k",
            reward=0.0,
            passed=False,
        ),
        TrialEvidence(
            job_name="zai-wave2-action64k-s1337-repeats",
            trial_name="action-64k-neutral_padding-s1337__JvdEs9Y",
            trial_dir="/tmp/t4",
            task_name="action",
            benchmark_family="action_memory",
            model_name="glm-5.3-flash",
            agent_name="opencode",
            wave="wave2",
            seed=1337,
            arm="neutral_padding",
            dose="64k",
            reward=0.0,
            passed=False,
        ),
        TrialEvidence(
            job_name="zai-wave2-action64k-s1337-repeats",
            trial_name="action-64k-neutral_padding-s1337__wCHLZ4M",
            trial_dir="/tmp/t5",
            task_name="action",
            benchmark_family="action_memory",
            model_name="glm-5.3-flash",
            agent_name="opencode",
            wave="wave2",
            seed=1337,
            arm="neutral_padding",
            dose="64k",
            reward=0.0,
            passed=False,
        ),
        TrialEvidence(
            job_name="zai-wave2-flash-matrix",
            trial_name="action-64k-semantic_distractor-s__Qoz3nbU",
            trial_dir="/tmp/t6",
            task_name="action",
            benchmark_family="action_memory",
            model_name="glm-5.3-flash",
            agent_name="opencode",
            wave="wave2",
            seed=1337,
            arm="semantic_distractor",
            dose="64k",
            reward=0.0,
            passed=False,
        ),
        TrialEvidence(
            job_name="zai-wave2-action64k-s1337-repeats",
            trial_name="action-64k-semantic_distractor-s__FLiG7jy",
            trial_dir="/tmp/t7",
            task_name="action",
            benchmark_family="action_memory",
            model_name="glm-5.3-flash",
            agent_name="opencode",
            wave="wave2",
            seed=1337,
            arm="semantic_distractor",
            dose="64k",
            reward=0.0,
            passed=False,
        ),
        TrialEvidence(
            job_name="zai-wave2-action64k-s1337-repeats",
            trial_name="action-64k-semantic_distractor-s__Pgukjp8",
            trial_dir="/tmp/t8",
            task_name="action",
            benchmark_family="action_memory",
            model_name="glm-5.3-flash",
            agent_name="opencode",
            wave="wave2",
            seed=1337,
            arm="semantic_distractor",
            dose="64k",
            reward=0.0,
            passed=False,
        ),
        # s1337 sequential scaffold t3 (1 neutral, 1 semantic)
        TrialEvidence(
            job_name="zai-wave2-action64k-s1337-sequential-scaffold-t3",
            trial_name="action-64k-neutral_padding-s1337__u4CZxsA",
            trial_dir="/tmp/t9",
            task_name="action",
            benchmark_family="action_memory",
            model_name="glm-5.3-flash",
            agent_name="opencode",
            wave="wave2",
            seed=1337,
            arm="neutral_padding",
            dose="64k",
            reward=1.0,
            passed=True,
        ),
        TrialEvidence(
            job_name="zai-wave2-action64k-s1337-sequential-scaffold-t3",
            trial_name="action-64k-semantic_distractor-s__A67eDZ2",
            trial_dir="/tmp/t10",
            task_name="action",
            benchmark_family="action_memory",
            model_name="glm-5.3-flash",
            agent_name="opencode",
            wave="wave2",
            seed=1337,
            arm="semantic_distractor",
            dose="64k",
            reward=0.0,
            passed=False,
        ),
        # s1337 default timeout infrastructure exclusions (reward=None)
        TrialEvidence(
            job_name="zai-wave2-action64k-s1337-sequential-scaffold",
            trial_name="action-64k-neutral_padding-s1337__VxJbtpZ",
            trial_dir="/tmp/t11",
            task_name="action",
            benchmark_family="action_memory",
            model_name="glm-5.3-flash",
            agent_name="opencode",
            wave="wave2",
            seed=1337,
            arm="neutral_padding",
            dose="64k",
            reward=None,
            passed=False,
            is_infra_exception=True,
        ),
        TrialEvidence(
            job_name="zai-wave2-action64k-s1337-sequential-scaffold",
            trial_name="action-64k-semantic_distractor-s__TyoghGd",
            trial_dir="/tmp/t12",
            task_name="action",
            benchmark_family="action_memory",
            model_name="glm-5.3-flash",
            agent_name="opencode",
            wave="wave2",
            seed=1337,
            arm="semantic_distractor",
            dose="64k",
            reward=None,
            passed=False,
            is_infra_exception=True,
        ),
    ]

    contrasts = build_seed_blocked_contrasts(trials)
    assert len(contrasts) == 3

    # Row 1: Flash unscaffolded s42
    c_s42 = next(c for c in contrasts if "Flash unscaffolded seed 42" in c.contrast_name)
    assert len(c_s42.trials_arm_a) == 1
    assert len(c_s42.trials_arm_b) == 1
    assert c_s42.mean_reward_a == 1.0
    assert c_s42.mean_reward_b == 1.0
    assert c_s42.reward_delta == 0.0

    # Row 2: Flash unscaffolded s1337
    c_s1337 = next(c for c in contrasts if "Flash unscaffolded seed 1337" in c.contrast_name)
    assert len(c_s1337.trials_arm_a) == 3
    assert len(c_s1337.trials_arm_b) == 3
    assert c_s1337.mean_reward_a == 0.0
    assert c_s1337.mean_reward_b == 0.0
    assert c_s1337.reward_delta == 0.0

    # Row 3: Sequential scaffold t3 s1337
    c_scaffold = next(c for c in contrasts if "Sequential scaffold t3 seed 1337" in c.contrast_name)
    assert len(c_scaffold.trials_arm_a) == 1
    assert len(c_scaffold.trials_arm_b) == 1
    assert c_scaffold.mean_reward_a == 1.0
    assert c_scaffold.mean_reward_b == 0.0
    assert c_scaffold.reward_delta == -1.0

    # Assert pairing fingerprints and denominator invariants across all contrast rows
    for c in contrasts:
        # 1. Denominator equals scored count
        assert len(c.trials_arm_a) == len(
            [t for t in c.trials_arm_a if t.reward is not None and not t.is_infra_exception]
        )
        assert len(c.trials_arm_b) == len(
            [t for t in c.trials_arm_b if t.reward is not None and not t.is_infra_exception]
        )
        # 2. No timeouts included
        assert all(t.reward is not None for t in c.trials_arm_a + c.trials_arm_b)
        assert all(not t.is_infra_exception for t in c.trials_arm_a + c.trials_arm_b)
        # 3. Model consistency in perturbation contrast
        assert all(t.model_name == "glm-5.3-flash" for t in c.trials_arm_a + c.trials_arm_b)
        # 4. Stratum consistency: scaffold trials never mixed with unscaffolded
        is_scaffold_contrast = (
            "sequential scaffold" in c.contrast_name.lower()
            or "scaffold t3" in c.contrast_name.lower()
        )
        for t in c.trials_arm_a + c.trials_arm_b:
            assert ("scaffold" in t.job_name.lower()) == is_scaffold_contrast
        # 5. Seed consistency
        if "seed 42" in c.contrast_name:
            assert all(t.seed == 42 for t in c.trials_arm_a + c.trials_arm_b)
        elif "seed 1337" in c.contrast_name:
            assert all(t.seed == 1337 for t in c.trials_arm_a + c.trials_arm_b)


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
