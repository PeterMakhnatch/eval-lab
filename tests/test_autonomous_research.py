from __future__ import annotations

import pytest

from evallab.autonomous_research import (
    ResearchIterationV1,
    ResearchRunTraceV1,
    ScoreScaleBindingV1,
    extract_autonomous_research_features,
    parse_jsonl_experiment_log,
)
from evallab.interpretation.feature_registry import (
    TRAJECTORY_FEATURE_REGISTRY,
    audit_denominator_policy,
    audit_verdict_coupling,
    compute_benchmark_feature_yield,
    feature_analysis_eligibility,
    verify_benchmark_feature_coverage,
)


def test_autonomous_research_features_capture_iteration_selection_and_transfer() -> None:
    scale_binding = ScoreScaleBindingV1.create(
        metric_name="score",
        direction="higher",
        visible_split_id="val",
        hidden_split_id="test",
        normalization_digest="sha256:" + "norm" * 16,
    )
    trace = ResearchRunTraceV1(
        run_id="rsi-run-1",
        benchmark_family="rsi-exam/game2048",
        source_kind="harbor",
        source_version="v2",
        source_record_id="rec-123",
        source_revision_id="rev-456",
        source_digest="sha256:" + "1" * 64,
        baseline_visible_score=10.0,
        score_direction="higher",
        score_scale_binding=scale_binding,
        hidden_score=12.0,
        selected_iteration_id="v5",
        budget_seconds=100.0,
        elapsed_seconds=80.0,
        final_artifact_digest="sha256:" + "2" * 64,
        artifact_replay_verified=True,
        total_cost_usd=1.50,
        total_tokens=15000,
        required_milestones=4,
        completed_milestones=3,
        total_rubric_subtasks=10,
        completed_rubric_subtasks=8,
        environment_setup_seconds=12.5,
        dependency_repair_attempts=2,
        dependency_repair_successes=2,
        leakage_flag=False,
        leakage_warning_count=0,
        train_val_split_intact=True,
        iterations=(
            ResearchIterationV1(
                iteration_id="v1",
                hypothesis="Prefer monotonic corners",
                visible_score=11.0,
                disposition="kept",
                changed_bytes=100,
                elapsed_seconds=10.0,
                artifact_digest="sha256:" + "a" * 64,
                is_reproducible=True,
            ),
            ResearchIterationV1(
                iteration_id="v2",
                hypothesis="Maximize immediate merges",
                visible_score=9.0,
                disposition="reverted",
                changed_bytes=100,
                elapsed_seconds=15.0,
                artifact_digest="sha256:" + "b" * 64,
                is_reproducible=True,
            ),
            ResearchIterationV1(
                iteration_id="v3",
                hypothesis="Search two moves",
                disposition="invalid",
                changed_bytes=100,
                elapsed_seconds=5.0,
                execution_success=False,
            ),
            ResearchIterationV1(
                iteration_id="v4",
                hypothesis="Search two moves",
                visible_score=14.0,
                disposition="kept",
                changed_bytes=100,
                elapsed_seconds=25.0,
                artifact_digest="sha256:" + "c" * 64,
                is_reproducible=True,
            ),
            ResearchIterationV1(
                iteration_id="v5",
                hypothesis="Search two moves",
                visible_score=13.0,
                disposition="observed",
                changed_bytes=100,
                elapsed_seconds=25.0,
                artifact_digest="sha256:" + "2" * 64,
                is_reproducible=True,
            ),
        ),
    )
    features = extract_autonomous_research_features(trace)

    # Identity & Source
    assert features.source_kind == "harbor"
    assert features.source_version == "v2"
    assert features.source_record_id == "rec-123"
    assert features.source_revision_id == "rev-456"
    assert features.score_direction == "higher"

    # 1. Experiment Throughput & Validity
    assert features.iteration_count == 5
    assert features.measured_iteration_count == 4
    assert features.valid_experiment_count == 4
    assert features.invalid_iteration_count == 1
    assert features.valid_experiment_rate == 0.8
    assert features.experiment_throughput_per_hour == pytest.approx(5 / (80.0 / 3600.0))

    # 2. Hypothesis Turnover & Exploration
    assert features.unique_hypothesis_count == 3
    assert features.repeated_hypothesis_count == 2
    assert features.hypothesis_turnover_rate == 0.6

    # 3. Regressions & Rollback Control
    assert features.kept_iteration_count == 2
    assert features.reverted_iteration_count == 1
    assert features.regression_count == 2
    assert features.max_consecutive_regressions == 1
    assert features.rollback_rate == 0.25
    assert features.regression_rate == pytest.approx(2 / 3)

    # 4. Score-Time Curves & Dynamics
    assert features.baseline_visible_score == 10.0
    assert features.best_visible_score == 14.0
    assert features.final_visible_score == 13.0
    assert features.first_improvement_iteration == 1
    assert features.best_improvement_iteration == 4
    assert features.time_to_first_improvement_seconds == 10.0
    assert features.time_to_best_score_seconds == 55.0
    assert features.stalled_iteration_count == 1
    assert features.plateau_streak_max == 1
    assert features.visible_improvement == 4.0
    assert features.improvement_per_experiment == 1.0
    assert features.late_improvement_share == 0.75
    assert features.stalled_iteration_rate == 0.2

    # 5. Milestone & Rubric Progression
    assert features.required_milestones == 4
    assert features.completed_milestones == 3
    assert features.milestone_completion_rate == 0.75
    assert features.total_rubric_subtasks == 10
    assert features.completed_rubric_subtasks == 8
    assert features.rubric_completion_rate == 0.8

    # 6. Final-Selection Regret
    assert features.selected_iteration_id == "v5"
    assert features.optimal_selection_flag is False
    assert features.final_selection_regret == 1.0

    # 7. Hidden-Transfer Gap & Generalization
    assert features.score_scale_compatible is True
    assert features.scale_binding_digest == scale_binding.binding_digest
    assert features.hidden_score == 12.0
    assert features.visible_hidden_transfer_gap == -1.0

    # 8. Artifact Replay & Reproducibility
    assert features.final_artifact_digest == "sha256:" + "2" * 64
    assert features.artifact_replay_verified is True
    assert features.reproducibility_evaluated_count == 4
    assert features.reproducible_iteration_count == 4
    assert features.reproducibility_rate == 1.0

    # 9. Environment Reconstruction & Dependency Repair
    assert features.environment_setup_seconds == 12.5
    assert features.dependency_repair_attempts == 2
    assert features.dependency_repair_successes == 2
    assert features.runtime_environment_repaired is True
    assert features.dependency_repair_success_rate == 1.0

    # 10. Budget & Cost Efficiency
    assert features.budget_seconds == 100.0
    assert features.elapsed_seconds == 80.0
    assert features.total_cost_usd == 1.50
    assert features.total_tokens == 15000
    assert features.total_changed_bytes == 500
    assert features.budget_utilization_rate == 0.8
    assert features.cost_per_improvement == pytest.approx(1.50 / 4.0)
    assert features.tokens_per_experiment == 3000.0
    assert features.changed_bytes_per_improvement == 125.0

    # 11. Data Integrity & Contamination Prevention
    assert features.leakage_detected_flag is False
    assert features.leakage_warning_count == 0
    assert features.train_val_split_intact is True

    # Lineage digests
    assert features.source_digest == "sha256:" + "1" * 64
    assert features.feature_digest.startswith("sha256:")

    # Dictionary representation
    record = features.to_dict()
    assert isinstance(record, dict)
    assert record["run_id"] == "rsi-run-1"
    assert record["visible_improvement"] == 4.0
    assert record["selected_iteration_id"] == "v5"
    assert record["scale_binding_digest"] == scale_binding.binding_digest


def test_selected_iteration_id_governs_final_visible_score_and_regret() -> None:
    # Test selecting the best iteration v4 vs sub-optimal iteration v5
    trace_optimal = ResearchRunTraceV1(
        run_id="opt-selection-run",
        benchmark_family="mle-bench/comp",
        source_digest="sha256:" + "a" * 64,
        baseline_visible_score=10.0,
        selected_iteration_id="v4",  # v4 is peak score (14.0)
        iterations=(
            ResearchIterationV1(iteration_id="v1", visible_score=11.0),
            ResearchIterationV1(iteration_id="v4", visible_score=14.0),
            ResearchIterationV1(iteration_id="v5", visible_score=12.0),
        ),
    )
    features_opt = extract_autonomous_research_features(trace_optimal)
    assert features_opt.selected_iteration_id == "v4"
    assert features_opt.final_visible_score == 14.0
    assert features_opt.final_selection_regret == 0.0
    assert features_opt.optimal_selection_flag is True

    # Test auto-resolution via final_artifact_digest
    trace_auto = ResearchRunTraceV1(
        run_id="auto-selection-run",
        benchmark_family="mle-bench/comp",
        source_digest="sha256:" + "b" * 64,
        baseline_visible_score=10.0,
        final_artifact_digest="sha256:" + "f" * 64,
        iterations=(
            ResearchIterationV1(
                iteration_id="v1",
                visible_score=11.0,
                artifact_digest="sha256:" + "e" * 64,
            ),
            ResearchIterationV1(
                iteration_id="v2",
                visible_score=14.0,
                artifact_digest="sha256:" + "f" * 64,
            ),
            ResearchIterationV1(
                iteration_id="v3",
                visible_score=12.0,
                artifact_digest="sha256:" + "g" * 64,
            ),
        ),
    )
    features_auto = extract_autonomous_research_features(trace_auto)
    assert features_auto.selected_iteration_id == "v2"
    assert features_auto.final_visible_score == 14.0
    assert features_auto.final_selection_regret == 0.0


def test_selected_iteration_validation_enforces_digest_and_existence() -> None:
    # Non-existent selected iteration
    with pytest.raises(ValueError, match="not found in trace iterations"):
        ResearchRunTraceV1(
            run_id="err-run",
            benchmark_family="mle-bench",
            source_digest="sha256:" + "1" * 64,
            selected_iteration_id="missing-id",
            iterations=(ResearchIterationV1(iteration_id="v1", visible_score=1.0),),
        )

    # Selected iteration artifact digest mismatch with final_artifact_digest
    with pytest.raises(ValueError, match="does not match final_artifact_digest"):
        ResearchRunTraceV1(
            run_id="err-run-digest",
            benchmark_family="mle-bench",
            source_digest="sha256:" + "1" * 64,
            selected_iteration_id="v1",
            final_artifact_digest="sha256:" + "expected" * 8,
            iterations=(
                ResearchIterationV1(
                    iteration_id="v1",
                    visible_score=1.0,
                    artifact_digest="sha256:" + "mismatch" * 8,
                ),
            ),
        )


def test_score_scale_binding_validation_and_transfer_gap_gate() -> None:
    binding = ScoreScaleBindingV1.create(
        metric_name="accuracy",
        direction="higher",
        visible_split_id="public_val",
        hidden_split_id="private_test",
        normalization_digest="sha256:" + "norm" * 16,
    )

    # Valid binding digest validation
    assert binding.binding_digest.startswith("sha256:")
    assert binding.normalization_digest == "sha256:" + "norm" * 16

    # Refuses missing or empty normalization_digest
    with pytest.raises(ValueError, match="normalization_digest"):
        ScoreScaleBindingV1.create(
            metric_name="accuracy",
            direction="higher",
            visible_split_id="public_val",
            hidden_split_id="private_test",
            normalization_digest="",
        )

    # Tampered binding digest raises ValueError
    with pytest.raises(ValueError, match="digest mismatch"):
        ScoreScaleBindingV1(
            metric_name="accuracy",
            direction="higher",
            visible_split_id="public_val",
            hidden_split_id="private_test",
            normalization_digest="sha256:" + "norm" * 16,
            binding_digest="sha256:bad_digest",
        )
    with pytest.raises(ValueError, match="does not match trace score_direction"):
        ResearchRunTraceV1(
            run_id="mismatch-run",
            benchmark_family="paperbench",
            source_digest="sha256:" + "3" * 64,
            score_direction="lower",
            score_scale_binding=binding,  # binding has direction="higher"
            iterations=(ResearchIterationV1(iteration_id="v1", visible_score=0.5),),
        )

    # Trace without binding returns None for transfer gap even when hidden_score is present
    unbound_trace = ResearchRunTraceV1(
        run_id="unbound-run",
        benchmark_family="paperbench",
        source_digest="sha256:" + "3" * 64,
        baseline_visible_score=0.1,
        hidden_score=0.4,
        score_scale_binding=None,
        iterations=(
            ResearchIterationV1(
                iteration_id="v1",
                visible_score=0.3,
                disposition="kept",
            ),
        ),
    )
    unbound_features = extract_autonomous_research_features(unbound_trace)
    assert unbound_features.visible_hidden_transfer_gap is None
    assert unbound_features.score_scale_compatible is False
    assert unbound_features.scale_binding_digest is None


def test_jsonl_experiment_log_parser_refuses_ambiguous_prose() -> None:
    text = "\n".join(
        [
            "# explicit records",
            '{"iteration_id":"v1","hypothesis":"try A","visible_score":1.0,"disposition":"kept"}',
            '{"iteration_id":"v2","hypothesis":"try B","visible_score":0.5,"disposition":"reverted"}',
        ]
    )
    iterations = parse_jsonl_experiment_log(text)
    assert [iteration.iteration_id for iteration in iterations] == ["v1", "v2"]
    with pytest.raises(ValueError, match="not explicit JSONL"):
        parse_jsonl_experiment_log("v1 improved the score")


def test_autonomous_research_edge_cases_and_null_conditions() -> None:
    # Empty run trace with zero iterations
    empty_trace = ResearchRunTraceV1(
        run_id="empty-run",
        benchmark_family="mle-bench/competition-1",
        source_digest="sha256:" + "0" * 64,
        iterations=(),
    )
    features = extract_autonomous_research_features(empty_trace)
    assert features.iteration_count == 0
    assert features.measured_iteration_count == 0
    assert features.valid_experiment_count == 0
    assert features.invalid_iteration_count == 0
    assert features.valid_experiment_rate is None
    assert features.experiment_throughput_per_hour is None
    assert features.unique_hypothesis_count == 0
    assert features.repeated_hypothesis_count == 0
    assert features.hypothesis_turnover_rate is None
    assert features.rollback_rate is None
    assert features.regression_rate is None
    assert features.visible_improvement is None
    assert features.improvement_per_experiment is None
    assert features.late_improvement_share is None
    assert features.stalled_iteration_rate is None
    assert features.milestone_completion_rate is None
    assert features.rubric_completion_rate is None
    assert features.selected_iteration_id is None
    assert features.final_selection_regret is None
    assert features.optimal_selection_flag is None
    assert features.visible_hidden_transfer_gap is None
    assert features.reproducibility_rate is None
    assert features.dependency_repair_success_rate is None
    assert features.budget_utilization_rate is None
    assert features.cost_per_improvement is None
    assert features.tokens_per_experiment is None
    assert features.changed_bytes_per_improvement is None


def test_consecutive_regressions_and_plateau_streaks() -> None:
    trace = ResearchRunTraceV1(
        run_id="streak-run",
        benchmark_family="re-bench/task-1",
        source_digest="sha256:" + "4" * 64,
        baseline_visible_score=10.0,
        selected_iteration_id="i6",
        iterations=(
            ResearchIterationV1(iteration_id="i1", visible_score=12.0),
            ResearchIterationV1(iteration_id="i2", visible_score=11.0),
            ResearchIterationV1(iteration_id="i3", visible_score=10.0),
            ResearchIterationV1(iteration_id="i4", visible_score=9.0),
            ResearchIterationV1(iteration_id="i5", visible_score=15.0),
            ResearchIterationV1(iteration_id="i6", visible_score=14.0),
        ),
    )
    features = extract_autonomous_research_features(trace)
    assert features.max_consecutive_regressions == 3  # i1->i2->i3->i4
    assert features.plateau_streak_max == 3  # i2, i3, i4 failed to exceed running max of 12.0
    assert features.best_visible_score == 15.0
    assert features.best_improvement_iteration == 5
    assert features.stalled_iteration_count == 1  # only i6 after i5
    assert features.selected_iteration_id == "i6"
    assert features.final_visible_score == 14.0
    assert features.final_selection_regret == 1.0
    assert features.optimal_selection_flag is False


def test_leakage_and_contamination_detection() -> None:
    trace = ResearchRunTraceV1(
        run_id="leak-run",
        benchmark_family="mle-bench/leakage-case",
        source_digest="sha256:" + "5" * 64,
        leakage_flag=False,
        train_val_split_intact=True,
        iterations=(
            ResearchIterationV1(
                iteration_id="i1",
                visible_score=10.0,
                leakage_detected=True,
            ),
        ),
    )
    features = extract_autonomous_research_features(trace)
    assert features.leakage_detected_flag is True
    assert features.train_val_split_intact is False


def test_lower_is_better_score_direction_semantics() -> None:
    scale_binding = ScoreScaleBindingV1.create(
        metric_name="loss",
        direction="lower",
        visible_split_id="val",
        hidden_split_id="test",
        normalization_digest="sha256:" + "norm" * 16,
    )
    trace = ResearchRunTraceV1(
        run_id="loss-opt-run",
        benchmark_family="mle-bench/loss-minimization",
        source_digest="sha256:" + "6" * 64,
        baseline_visible_score=2.5,
        score_direction="lower",
        score_scale_binding=scale_binding,
        hidden_score=1.7,
        selected_iteration_id="i4",
        iterations=(
            ResearchIterationV1(
                iteration_id="i1",
                hypothesis="reduce learning rate",
                visible_score=2.2,
                disposition="kept",
                elapsed_seconds=10.0,
            ),
            ResearchIterationV1(
                iteration_id="i2",
                hypothesis="add weight decay",
                visible_score=2.4,
                disposition="reverted",
                elapsed_seconds=10.0,
            ),
            ResearchIterationV1(
                iteration_id="i3",
                hypothesis="cosine schedule",
                visible_score=1.8,
                disposition="kept",
                elapsed_seconds=15.0,
            ),
            ResearchIterationV1(
                iteration_id="i4",
                hypothesis="increase batch size",
                visible_score=1.9,
                disposition="observed",
                elapsed_seconds=15.0,
            ),
        ),
    )
    features = extract_autonomous_research_features(trace)

    assert features.score_direction == "lower"
    assert features.baseline_visible_score == 2.5
    assert features.best_visible_score == 1.8  # minimum loss is best
    assert features.selected_iteration_id == "i4"
    assert features.final_visible_score == 1.9
    assert features.visible_improvement == pytest.approx(2.5 - 1.8)  # +0.7 positive improvement
    assert features.final_selection_regret == pytest.approx(1.9 - 1.8)  # +0.1 positive regret
    assert features.optimal_selection_flag is False
    assert features.regression_count == 2  # i1->i2 (2.2->2.4 is worse), i3->i4 (1.8->1.9 is worse)
    assert features.max_consecutive_regressions == 1
    assert features.first_improvement_iteration == 1  # i1 (2.2 < 2.5)
    assert features.best_improvement_iteration == 3  # i3 (1.8)
    assert features.stalled_iteration_count == 1  # i4 after i3
    assert features.plateau_streak_max == 1  # i2 did not beat running best of 2.2
    assert features.late_improvement_share == pytest.approx((2.2 - 1.8) / (2.5 - 1.8))  # 0.4 / 0.7
    # Raw visible-hidden transfer gap is preserved as hidden - final
    assert features.visible_hidden_transfer_gap == pytest.approx(1.7 - 1.9)
    assert features.scale_binding_digest == scale_binding.binding_digest


def test_reproducibility_distinguishes_unknown_from_false() -> None:
    # Mixed trace: 1 True, 1 False, 1 None (unknown)
    mixed_trace = ResearchRunTraceV1(
        run_id="repro-mixed",
        benchmark_family="core-bench/capsule",
        source_digest="sha256:" + "7" * 64,
        iterations=(
            ResearchIterationV1(iteration_id="i1", is_reproducible=True),
            ResearchIterationV1(iteration_id="i2", is_reproducible=False),
            ResearchIterationV1(iteration_id="i3", is_reproducible=None),
        ),
    )
    mixed_features = extract_autonomous_research_features(mixed_trace)
    assert mixed_features.iteration_count == 3
    assert mixed_features.reproducibility_evaluated_count == 2
    assert mixed_features.reproducible_iteration_count == 1
    assert mixed_features.reproducibility_rate == 0.5

    # Unmeasured trace: all is_reproducible=None
    unmeasured_trace = ResearchRunTraceV1(
        run_id="repro-unmeasured",
        benchmark_family="rsi-exam/pilot",
        source_digest="sha256:" + "8" * 64,
        iterations=(
            ResearchIterationV1(iteration_id="i1", is_reproducible=None),
            ResearchIterationV1(iteration_id="i2", is_reproducible=None),
        ),
    )
    unmeasured_features = extract_autonomous_research_features(unmeasured_trace)
    assert unmeasured_features.iteration_count == 2
    assert unmeasured_features.reproducibility_evaluated_count == 0
    assert unmeasured_features.reproducible_iteration_count == 0
    assert unmeasured_features.reproducibility_rate is None  # MUST be NULL, not 0.0!


def test_feature_registry_governance_for_autonomous_research_family() -> None:
    family_features = TRAJECTORY_FEATURE_REGISTRY.by_family("autonomous-research-v1")
    assert len(family_features) == 68, (
        f"Expected 68 registered features, got {len(family_features)}"
    )

    # Audit denominator policies and verdict coupling for all features in family
    for col_name, feat in family_features.items():
        assert feat.family == "autonomous-research-v1"
        assert feat.producer_module == "evallab.autonomous_research"
        assert feat.source_table == "autonomous_research_runs"
        assert feat.is_screening is False
        assert not col_name.endswith("_screening")

        denom_audit = audit_denominator_policy(feat)
        assert denom_audit is None, f"Feature {col_name} failed denominator audit: {denom_audit}"

        coupling_audit = audit_verdict_coupling(feat)
        assert coupling_audit is None, (
            f"Feature {col_name} failed verdict coupling audit: {coupling_audit}"
        )

        eligibility = feature_analysis_eligibility(feat)
        assert eligibility.outcome_allowed or feat.category == "identity"


def test_benchmark_feature_coverage_and_yield() -> None:
    scale_binding = ScoreScaleBindingV1.create(
        metric_name="score",
        direction="higher",
        visible_split_id="val",
        hidden_split_id="test",
        normalization_digest="sha256:" + "norm" * 16,
    )
    trace = ResearchRunTraceV1(
        run_id="eval-run-yield",
        benchmark_family="rsi-exam",
        source_kind="synthetic",
        source_version="v1",
        source_record_id="rec-1",
        source_revision_id="rev-1",
        source_digest="sha256:" + "9" * 64,
        baseline_visible_score=5.0,
        score_scale_binding=scale_binding,
        hidden_score=8.0,
        selected_iteration_id="it2",
        budget_seconds=3600.0,
        elapsed_seconds=1800.0,
        total_cost_usd=0.50,
        total_tokens=5000,
        required_milestones=2,
        completed_milestones=2,
        total_rubric_subtasks=5,
        completed_rubric_subtasks=4,
        environment_setup_seconds=10.0,
        dependency_repair_attempts=1,
        dependency_repair_successes=1,
        final_artifact_digest="sha256:" + "8" * 64,
        artifact_replay_verified=True,
        iterations=(
            ResearchIterationV1(
                iteration_id="it1",
                hypothesis="initial model baseline",
                visible_score=6.0,
                disposition="kept",
                changed_bytes=50,
                elapsed_seconds=10.0,
                artifact_digest="sha256:" + "7" * 64,
                is_reproducible=True,
            ),
            ResearchIterationV1(
                iteration_id="it2",
                hypothesis="hyperparameter sweep",
                visible_score=7.0,
                disposition="kept",
                changed_bytes=100,
                elapsed_seconds=20.0,
                artifact_digest="sha256:" + "8" * 64,
                is_reproducible=True,
            ),
        ),
    )
    features = extract_autonomous_research_features(trace)
    record = features.to_dict()

    coverage = verify_benchmark_feature_coverage([record], family="autonomous-research-v1")
    assert coverage["passed"] is True
    assert coverage["missing_features"] == []

    yield_diag = compute_benchmark_feature_yield([record], family="autonomous-research-v1")
    assert yield_diag["total_records"] == 1
    assert len(yield_diag["feature_stats"]) == 68


def test_trace_validation_rejects_duplicate_iteration_ids_and_non_finite_scores() -> None:
    with pytest.raises(ValueError, match="iteration ID 'same-id' is not unique"):
        ResearchRunTraceV1(
            run_id="dup-run",
            benchmark_family="core-bench",
            source_digest="sha256:" + "a" * 64,
            iterations=(
                ResearchIterationV1(iteration_id="same-id", visible_score=1.0),
                ResearchIterationV1(iteration_id="same-id", visible_score=2.0),
            ),
        )

    with pytest.raises(ValueError, match="finite"):
        ResearchIterationV1(
            iteration_id="nan-iter",
            visible_score=float("nan"),
        )
