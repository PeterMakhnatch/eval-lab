from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from evallab.task_workbench import (
    WorkbenchError,
    compare_quality_audits,
    render_quality_audit_comparison_text,
    run_cli,
)

REAL_REVIEW_RESULT = Path(
    "/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/quality/pr75-runtime-review-20260908/review-result.json"
)
REAL_REPAIR_CONTRACT = Path(
    "/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/quality/pr75-runtime-review-20260908/repair-contract.json"
)


def test_real_pr75_native_comparison_exposes_expected_populations() -> None:
    """Verify 4 paired contrasts, valid no-effect arm, 0.8 partial credit, and 2 infra failures."""
    if not REAL_REVIEW_RESULT.is_file() or not REAL_REPAIR_CONTRACT.is_file():
        pytest.skip("Real PR75 review artifacts not present on disk")

    comparison = compare_quality_audits(
        review_result_path=REAL_REVIEW_RESULT,
        repair_contract_path=REAL_REPAIR_CONTRACT,
    )

    # 1. Verify populations
    pops = comparison["populations"]
    assert pops["paired_arms_count"] == 4
    assert pops["control_trials_count"] == 8
    assert pops["infrastructure_failures_count"] == 2
    assert pops["native_ingested_jobs_count"] == 10
    assert pops["native_reward_facts_count"] == 8

    # 2. Verify the 4 paired arms
    arms = comparison["arms"]
    assert set(arms.keys()) == {
        "unfixed_error_branch",
        "valid_scalar_representation",
        "zero_tests_exit_zero",
        "valid_path_construction",
    }

    # Arm 1: 1 -> 0.8 unresolved partial progress
    unfixed = arms["unfixed_error_branch"]
    assert unfixed["original"]["observed_reward"] == 1.0
    assert unfixed["original"]["observed_resolved"] is True
    assert unfixed["repaired"]["observed_reward"] == 0.8
    assert unfixed["repaired"]["observed_resolved"] is False
    assert unfixed["observed_reward_delta"] == -0.2
    assert unfixed["partial_credit"] is True

    # Arm 2: 0.466667 -> 1 credit restoration
    scalar = arms["valid_scalar_representation"]
    assert scalar["original"]["observed_reward"] == 0.466667
    assert scalar["original"]["observed_resolved"] is False
    assert scalar["repaired"]["observed_reward"] == 1.0
    assert scalar["repaired"]["observed_resolved"] is True
    assert scalar["observed_reward_delta"] == 0.533333

    # Arm 3: 1 -> 0 no-test exit-zero bypass closure
    zero = arms["zero_tests_exit_zero"]
    assert zero["original"]["observed_reward"] == 1.0
    assert zero["original"]["observed_resolved"] is False
    assert zero["repaired"]["observed_reward"] == 0.0
    assert zero["repaired"]["observed_resolved"] is False
    assert zero["observed_reward_delta"] == -1.0

    # Arm 4: valid_path_construction 1 -> 1 valid no-effect arm
    path_arm = arms["valid_path_construction"]
    assert path_arm["original"]["observed_reward"] == 1.0
    assert path_arm["original"]["observed_resolved"] is True
    assert path_arm["repaired"]["observed_reward"] == 1.0
    assert path_arm["repaired"]["observed_resolved"] is True
    assert path_arm["observed_reward_delta"] == 0.0
    assert path_arm["delta_type"] == "neutral"

    # 3. Verify the 8 native control trials
    controls = comparison["control_trials"]
    assert len(controls) == 8
    assert all(c["observed_reward"] is not None for c in controls)
    assert all(c["execution_status"] == "completed" for c in controls)

    # 4. Verify the 2 preserved infrastructure failures
    failures = comparison["infrastructure_failures"]
    assert len(failures) == 2
    for fail in failures:
        assert fail["reward"] is None  # Never averaged into controls or assigned 0.0
        assert fail["reward_facts"] == 0
        assert fail["classification"] == "infrastructure/instrumentation; no semantic reward"
        assert fail["exception_type"] == "RewardFileNotFoundError"

    # 5. Verify text rendering output
    text = render_quality_audit_comparison_text(comparison)
    assert "PR75 runtime review and repair contract" in text
    assert "Paired arms (4 contrasts):" in text
    assert "delta=-0.2 (partial credit 0.8 preserved; unresolved error branch)" in text
    assert "delta=0.0 (neutral delta; valid no-effect arm preserved)" in text
    assert "Native control trials (8):" in text
    assert "Preserved infrastructure failures (2):" in text
    assert "RewardFileNotFoundError" in text


def test_missing_raw_result_never_uses_cached_review_expectations(tmp_path: Path) -> None:
    """Ensure missing trial result.json reports None and does not copy review expectations."""
    # Create empty trial directories without result.json
    orig_dir = tmp_path / "orig_trial"
    orig_dir.mkdir()
    rep_dir = tmp_path / "rep_trial"
    rep_dir.mkdir()

    review_file = tmp_path / "review-result.json"
    review_file.write_text(
        json.dumps(
            {
                "comparison": [
                    {
                        "arm": "probe_arm",
                        "original_reward": 1.0,  # Cached expectations in review
                        "repaired_reward": 0.8,
                        "original_trial": str(orig_dir),
                        "repaired_trial": str(rep_dir),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    repair_file = tmp_path / "repair-contract.json"
    repair_file.write_text(
        json.dumps(
            {
                "expected_rewards": {"probe_arm": 0.8},
            }
        ),
        encoding="utf-8",
    )

    comparison = compare_quality_audits(
        review_result_path=review_file,
        repair_contract_path=repair_file,
    )
    probe = comparison["arms"]["probe_arm"]
    assert probe["original"]["observed_reward"] is None
    assert probe["original"]["execution_status"] == "missing"
    assert probe["repaired"]["observed_reward"] is None
    assert probe["repaired"]["execution_status"] == "missing"
    assert probe["observed_reward_delta"] is None


def test_infrastructure_failures_distinct_population_not_averaged(tmp_path: Path) -> None:
    """Verify infrastructure failure records are distinct and never averaged into controls."""
    ev_root = tmp_path / "evidence_root"
    ev_root.mkdir()
    fail_file = ev_root / "instrumentation-failures.json"
    fail_file.write_text(
        json.dumps(
            {
                "failures": [
                    {
                        "job": str(tmp_path / "fake-job-001"),
                        "reason": "Test non-executable bit failure",
                    }
                ],
                "classification": "infrastructure/instrumentation; no semantic reward",
            }
        ),
        encoding="utf-8",
    )

    # Create real trial with result.json
    trial_dir = tmp_path / "trial_ok"
    trial_dir.mkdir()
    (trial_dir / "result.json").write_text(
        json.dumps({"id": "t1", "verifier_result": {"rewards": {"reward": 1.0}}}),
        encoding="utf-8",
    )

    review_file = tmp_path / "review-result.json"
    review_file.write_text(
        json.dumps(
            {
                "evidence_root": str(ev_root),
                "comparison": [
                    {
                        "arm": "ok_arm",
                        "original_trial": str(trial_dir),
                        "repaired_trial": str(trial_dir),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    repair_file = tmp_path / "repair-contract.json"
    repair_file.write_text(
        json.dumps({"expected_rewards": {"ok_arm": 1.0}}),
        encoding="utf-8",
    )

    comparison = compare_quality_audits(
        review_result_path=review_file,
        repair_contract_path=repair_file,
    )
    assert len(comparison["infrastructure_failures"]) == 1
    fail = comparison["infrastructure_failures"][0]
    assert fail["reward"] is None
    assert fail["classification"] == "infrastructure/instrumentation; no semantic reward"

    # Verify control trials only have the real trial, not the failure
    assert len(comparison["control_trials"]) == 2
    for c in comparison["control_trials"]:
        assert c["observed_reward"] == 1.0


def test_cli_invocation_and_invalid_modes() -> None:
    """Test CLI exit codes for valid review-contract mode, missing arguments, and mixed modes."""
    if not REAL_REVIEW_RESULT.is_file() or not REAL_REPAIR_CONTRACT.is_file():
        pytest.skip("Real PR75 review artifacts not present on disk")

    # 1. Valid review-contract invocation
    assert (
        run_cli(
            [
                "audit-compare",
                "--review-result",
                str(REAL_REVIEW_RESULT),
                "--repair-contract",
                str(REAL_REPAIR_CONTRACT),
                "--format",
                "json",
            ]
        )
        == 0
    )

    # 2. Missing --repair-contract
    assert (
        run_cli(
            [
                "audit-compare",
                "--review-result",
                str(REAL_REVIEW_RESULT),
            ]
        )
        == 2
    )

    # 3. Invalid mixed modes: positional directory + --review-result
    assert (
        run_cli(
            [
                "audit-compare",
                "/tmp",
                "/tmp",
                "--review-result",
                str(REAL_REVIEW_RESULT),
                "--repair-contract",
                str(REAL_REPAIR_CONTRACT),
            ]
        )
        == 2
    )

    # 4. Bare invocation without any arguments
    assert run_cli(["audit-compare"]) == 2
