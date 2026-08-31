from __future__ import annotations

import pytest

from evallab.autonomous_research import (
    AutonomousResearchFeatures,
    ResearchIterationV1,
    ResearchRunTraceV1,
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
    trace = ResearchRunTraceV1(
        run_id="rsi-run-1",
        benchmark_family="rsi-exam/game2048",
        source_digest="sha256:" + "1" * 64,
        baseline_visible_score=10.0,
        hidden_score=12.0,
        score_scale_compatible=True,
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
                is_reproducible=True,
            ),
            ResearchIterationV1(
                iteration_id="v2",
                hypothesis="Maximize immediate merges",
                visible_score=9.0,
                disposition="reverted",
                changed_bytes=100,
                elapsed_seconds=15.0,
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
                is_reproducible=True,
            ),
            ResearchIterationV1(
                iteration_id="v5",
                hypothesis="Search two moves",
                visible_score=13.0,
                disposition="observed",
                changed_bytes=100,
                elapsed_seconds=25.0,
                is_reproducible=True,
            ),
        ),
    )
    features = extract_autonomous_research_features(trace)

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
    assert features.optimal_selection_flag is False
    assert features.final_selection_regret == 1.0

    # 7. Hidden-Transfer Gap & Generalization
    assert features.score_scale_compatible is True
    assert features.hidden_score == 12.0
    assert features.visible_hidden_transfer_gap == -1.0

    # 8. Artifact Replay & Reproducibility
    assert features.final_artifact_digest == "sha256:" + "2" * 64
    assert features.artifact_replay_verified is True
    assert features.reproducible_iteration_count == 4
    assert features.reproducibility_rate == 0.8

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


def test_transfer_gap_requires_compatible_score_scale() -> None:
    trace = ResearchRunTraceV1(
        run_id="paperbench-run",
        benchmark_family="paperbench",
        source_digest="sha256:" + "3" * 64,
        baseline_visible_score=0.1,
        hidden_score=0.4,
        score_scale_compatible=False,
        iterations=(
            ResearchIterationV1(
                iteration_id="v1",
                visible_score=0.3,
                disposition="kept",
            ),
        ),
    )
    features = extract_autonomous_research_features(trace)
    assert features.visible_hidden_transfer_gap is None
    assert features.score_scale_compatible is False


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


def test_feature_registry_governance_for_autonomous_research_family() -> None:
    family_features = TRAJECTORY_FEATURE_REGISTRY.by_family("autonomous-research-v1")
    assert len(family_features) == 60, f"Expected 60 registered features, got {len(family_features)}"

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
        assert coupling_audit is None, f"Feature {col_name} failed verdict coupling audit: {coupling_audit}"

        eligibility = feature_analysis_eligibility(feat)
        assert eligibility.outcome_allowed or feat.category == "identity"


def test_benchmark_feature_coverage_and_yield() -> None:
    trace = ResearchRunTraceV1(
        run_id="eval-run-yield",
        benchmark_family="rsi-exam",
        source_digest="sha256:" + "9" * 64,
        baseline_visible_score=5.0,
        hidden_score=8.0,
        score_scale_compatible=True,
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
    assert len(yield_diag["feature_stats"]) == 60


def test_trace_validation_rejects_duplicate_iteration_ids_and_non_finite_scores() -> None:
    with pytest.raises(ValueError, match="iteration IDs must be unique"):
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
