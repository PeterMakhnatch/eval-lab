from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

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
from evallab.registry import compute_task_digests


def _sample_scale_binding(
    *,
    metric_name: str = "score",
    direction: Literal["higher", "lower"] = "higher",
    authority_kind: Literal["benchmark_contract", "deterministic_verifier"] = "benchmark_contract",
    visible_split_id: str = "val",
    hidden_split_id: str = "test",
    task_digest: str = "sha256:" + "1" * 64,
    verifier_digest: str = "sha256:" + "2" * 64,
    metric_config_digest: str = "sha256:" + "3" * 64,
    visible_outcome_binding_digest: str = "sha256:" + "4" * 64,
    hidden_outcome_binding_digest: str = "sha256:" + "5" * 64,
) -> ScoreScaleBindingV1:
    return ScoreScaleBindingV1.create(
        authority_kind=authority_kind,
        metric_name=metric_name,
        direction=direction,
        task_digest=task_digest,
        verifier_digest=verifier_digest,
        metric_config_digest=metric_config_digest,
        visible_split_id=visible_split_id,
        hidden_split_id=hidden_split_id,
        visible_outcome_binding_digest=visible_outcome_binding_digest,
        hidden_outcome_binding_digest=hidden_outcome_binding_digest,
    )


def test_autonomous_research_features_capture_iteration_selection_and_transfer(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("name = 'test'\n", encoding="utf-8")
    (task_dir / "instruction.md").write_text("# Test\n", encoding="utf-8")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_eval.py").write_text("def test_it(): pass\n", encoding="utf-8")

    task_digests = compute_task_digests(task_dir)
    metric_cfg = {"metric": "score"}
    vis_outcome = {"score": 10.0}
    hid_outcome = {"score": 12.0}

    def _d(v: dict[str, Any]) -> str:
        return (
            "sha256:"
            + hashlib.sha256(
                json.dumps(v, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )

    scale_binding = _sample_scale_binding(
        metric_name="score",
        direction="higher",
        task_digest=task_digests.package,
        verifier_digest=task_digests.verifier,
        metric_config_digest=_d(metric_cfg),
        visible_outcome_binding_digest=_d(vis_outcome),
        hidden_outcome_binding_digest=_d(hid_outcome),
    )
    trace = ResearchRunTraceV1(
        run_id="rsi-run-1",
        benchmark_family="rsi-exam/game2048",
        source_kind="harbor",
        source_version="v2",
        source_record_id="rec-123",
        source_revision_id="rev-456",
        source_digest="sha256:" + "1" * 64,
        task_digest=scale_binding.task_digest,
        verifier_digest=scale_binding.verifier_digest,
        metric_config_digest=scale_binding.metric_config_digest,
        visible_outcome_binding_digest=scale_binding.visible_outcome_binding_digest,
        hidden_outcome_binding_digest=scale_binding.hidden_outcome_binding_digest,
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
    features = extract_autonomous_research_features(
        trace,
        task_dir=task_dir,
        metric_config=metric_cfg,
        visible_outcome=vis_outcome,
        hidden_outcome=hid_outcome,
    )

    # Identity & Source
    assert features.source_kind == "harbor"
    assert features.source_version == "v2"
    assert features.source_record_id == "rec-123"
    assert features.source_revision_id == "rev-456"
    assert features.score_direction == "higher"

    # Provenance digests
    assert features.task_digest == scale_binding.task_digest
    assert features.verifier_digest == scale_binding.verifier_digest
    assert features.metric_config_digest == scale_binding.metric_config_digest
    assert features.visible_outcome_binding_digest == scale_binding.visible_outcome_binding_digest
    assert features.hidden_outcome_binding_digest == scale_binding.hidden_outcome_binding_digest

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
    assert features.selection_decision_count == 3
    assert features.kept_iteration_count == 2
    assert features.reverted_iteration_count == 1
    assert features.regression_count == 2
    assert features.max_consecutive_regressions == 1
    assert features.rollback_rate == pytest.approx(1 / 3)
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
    assert record["task_digest"] == scale_binding.task_digest


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
            ResearchIterationV1(iteration_id="v4", visible_score=14.0, disposition="kept"),
            ResearchIterationV1(iteration_id="v5", visible_score=12.0, disposition="reverted"),
        ),
    )
    features_opt = extract_autonomous_research_features(trace_optimal)
    assert features_opt.selected_iteration_id == "v4"
    assert features_opt.final_visible_score == 14.0
    assert features_opt.final_selection_regret == 0.0
    assert features_opt.optimal_selection_flag is True


def test_selection_metrics_require_two_decisions() -> None:
    trace = ResearchRunTraceV1(
        run_id="single-selection-run",
        benchmark_family="rsi-exam/single",
        source_digest="sha256:" + "0" * 64,
        baseline_visible_score=10.0,
        selected_iteration_id="v1",
        iterations=(
            ResearchIterationV1(
                iteration_id="v1",
                visible_score=14.0,
                disposition="kept",
            ),
        ),
    )

    features = extract_autonomous_research_features(trace)
    assert features.selection_decision_count == 1
    assert features.final_visible_score == 14.0
    assert features.optimal_selection_flag is None
    assert features.final_selection_regret is None


def test_fail_closed_candidate_selection_invariants() -> None:
    # 1. final_artifact_digest supplied without selected_iteration_id -> raises ValueError
    with pytest.raises(
        ValueError, match="selected_iteration_id is required when final_artifact_digest is supplied"
    ):
        ResearchRunTraceV1(
            run_id="err-no-sel-art",
            benchmark_family="mle-bench",
            source_digest="sha256:" + "1" * 64,
            final_artifact_digest="sha256:" + "a" * 64,
            iterations=(
                ResearchIterationV1(
                    iteration_id="v1", visible_score=1.0, artifact_digest="sha256:" + "a" * 64
                ),
            ),
        )

    # 2. hidden_score supplied without selected_iteration_id -> raises ValueError
    with pytest.raises(
        ValueError, match="selected_iteration_id is required when hidden_score is supplied"
    ):
        ResearchRunTraceV1(
            run_id="err-no-sel-hidden",
            benchmark_family="mle-bench",
            source_digest="sha256:" + "1" * 64,
            hidden_score=1.5,
            iterations=(ResearchIterationV1(iteration_id="v1", visible_score=1.0),),
        )

    # 3. selected_iteration_id not found in trace iterations -> raises ValueError
    with pytest.raises(ValueError, match="not found in trace iterations"):
        ResearchRunTraceV1(
            run_id="err-missing-id",
            benchmark_family="mle-bench",
            source_digest="sha256:" + "1" * 64,
            selected_iteration_id="missing-id",
            iterations=(ResearchIterationV1(iteration_id="v1", visible_score=1.0),),
        )

    # 4. artifactless selected iteration when final_artifact_digest is supplied -> raises ValueError
    with pytest.raises(ValueError, match="must have a non-null artifact_digest"):
        ResearchRunTraceV1(
            run_id="err-artifactless",
            benchmark_family="mle-bench",
            source_digest="sha256:" + "1" * 64,
            selected_iteration_id="v1",
            final_artifact_digest="sha256:" + "a" * 64,
            iterations=(
                ResearchIterationV1(iteration_id="v1", visible_score=1.0, artifact_digest=None),
            ),
        )

    # 5. selected iteration artifact digest mismatch with final_artifact_digest -> raises ValueError
    with pytest.raises(ValueError, match="does not match final_artifact_digest"):
        ResearchRunTraceV1(
            run_id="err-mismatch-digest",
            benchmark_family="mle-bench",
            source_digest="sha256:" + "1" * 64,
            selected_iteration_id="v1",
            final_artifact_digest="sha256:" + "a" * 64,
            iterations=(
                ResearchIterationV1(
                    iteration_id="v1", visible_score=1.0, artifact_digest="sha256:" + "b" * 64
                ),
            ),
        )

    # 6. Trace without selected_iteration_id produces NULL for all selection/transfer metrics and never infers final artifact
    unselected_trace = ResearchRunTraceV1(
        run_id="unselected-run",
        benchmark_family="mle-bench",
        source_digest="sha256:" + "1" * 64,
        baseline_visible_score=10.0,
        iterations=(
            ResearchIterationV1(
                iteration_id="v1",
                visible_score=11.0,
                disposition="kept",
                artifact_digest="sha256:" + "a" * 64,
            ),
            ResearchIterationV1(
                iteration_id="v2",
                visible_score=13.0,
                disposition="kept",
                artifact_digest="sha256:" + "b" * 64,
            ),
        ),
    )
    features_unselected = extract_autonomous_research_features(unselected_trace)
    assert features_unselected.selected_iteration_id is None
    assert features_unselected.final_visible_score is None
    assert features_unselected.optimal_selection_flag is None
    assert features_unselected.final_selection_regret is None
    assert features_unselected.visible_hidden_transfer_gap is None
    assert features_unselected.final_artifact_digest is None  # MUST NOT infer from v2


def test_score_scale_binding_validation_and_transfer_gap_gate() -> None:
    binding = _sample_scale_binding(
        metric_name="accuracy",
        direction="higher",
        authority_kind="benchmark_contract",
        visible_split_id="public_val",
        hidden_split_id="private_test",
    )

    # Valid binding digest validation
    assert binding.binding_digest.startswith("sha256:")
    assert binding.authority_kind == "benchmark_contract"

    # Deterministic verifier authority kind
    verifier_binding = _sample_scale_binding(
        metric_name="reward",
        direction="higher",
        authority_kind="deterministic_verifier",
    )
    assert verifier_binding.authority_kind == "deterministic_verifier"

    # Refuses invalid SHA-256 digest syntax
    with pytest.raises(ValueError, match="task_digest must match sha256"):
        ScoreScaleBindingV1.create(
            authority_kind="benchmark_contract",
            metric_name="accuracy",
            direction="higher",
            task_digest="bad_digest",
            verifier_digest="sha256:" + "2" * 64,
            metric_config_digest="sha256:" + "3" * 64,
            visible_split_id="val",
            hidden_split_id="test",
            visible_outcome_binding_digest="sha256:" + "4" * 64,
            hidden_outcome_binding_digest="sha256:" + "5" * 64,
        )

    # Tampered binding digest raises ValueError
    with pytest.raises(ValueError, match="digest mismatch"):
        ScoreScaleBindingV1(
            authority_kind="benchmark_contract",
            metric_name="accuracy",
            direction="higher",
            task_digest="sha256:" + "1" * 64,
            verifier_digest="sha256:" + "2" * 64,
            metric_config_digest="sha256:" + "3" * 64,
            visible_split_id="public_val",
            hidden_split_id="private_test",
            visible_outcome_binding_digest="sha256:" + "4" * 64,
            hidden_outcome_binding_digest="sha256:" + "5" * 64,
            binding_digest="sha256:" + "f" * 64,
        )

    # Direction mismatch between binding and trace raises ValueError
    with pytest.raises(ValueError, match="does not match trace score_direction"):
        ResearchRunTraceV1(
            run_id="mismatch-run",
            benchmark_family="paperbench",
            source_digest="sha256:" + "3" * 64,
            task_digest=binding.task_digest,
            verifier_digest=binding.verifier_digest,
            metric_config_digest=binding.metric_config_digest,
            visible_outcome_binding_digest=binding.visible_outcome_binding_digest,
            hidden_outcome_binding_digest=binding.hidden_outcome_binding_digest,
            score_direction="lower",
            score_scale_binding=binding,  # binding has direction="higher"
            selected_iteration_id="v1",
            iterations=(ResearchIterationV1(iteration_id="v1", visible_score=0.5),),
        )

    # Binding digest sensitivity: different verifier_digest produces different binding_digest
    binding_alt = ScoreScaleBindingV1.create(
        authority_kind="benchmark_contract",
        metric_name="accuracy",
        direction="higher",
        task_digest="sha256:" + "1" * 64,
        verifier_digest="sha256:" + "9" * 64,  # altered
        metric_config_digest="sha256:" + "3" * 64,
        visible_split_id="public_val",
        hidden_split_id="private_test",
        visible_outcome_binding_digest="sha256:" + "4" * 64,
        hidden_outcome_binding_digest="sha256:" + "5" * 64,
    )
    assert binding.binding_digest != binding_alt.binding_digest


def test_cross_binding_digest_parity_and_unbound_hidden_score() -> None:
    binding = _sample_scale_binding(metric_name="loss", direction="lower")

    # 1. Missing task_digest on trace when binding is supplied -> ValueError
    with pytest.raises(
        ValueError, match="task_digest is required on trace when score_scale_binding is supplied"
    ):
        ResearchRunTraceV1(
            run_id="missing-task-dig",
            benchmark_family="mle-bench",
            source_digest="sha256:" + "0" * 64,
            score_direction="lower",
            score_scale_binding=binding,
            selected_iteration_id="i1",
            iterations=(ResearchIterationV1(iteration_id="i1", visible_score=1.0),),
        )

    # 2. task_digest mismatch between trace and binding -> ValueError
    with pytest.raises(ValueError, match="trace task_digest .* does not match score_scale_binding"):
        ResearchRunTraceV1(
            run_id="mismatch-task-dig",
            benchmark_family="mle-bench",
            source_digest="sha256:" + "0" * 64,
            task_digest="sha256:" + "9" * 64,  # mismatch with binding.task_digest
            verifier_digest=binding.verifier_digest,
            metric_config_digest=binding.metric_config_digest,
            visible_outcome_binding_digest=binding.visible_outcome_binding_digest,
            hidden_outcome_binding_digest=binding.hidden_outcome_binding_digest,
            score_direction="lower",
            score_scale_binding=binding,
            selected_iteration_id="i1",
            iterations=(ResearchIterationV1(iteration_id="i1", visible_score=1.0),),
        )

    # 3. verifier_digest mismatch -> ValueError
    with pytest.raises(
        ValueError, match="trace verifier_digest .* does not match score_scale_binding"
    ):
        ResearchRunTraceV1(
            run_id="mismatch-ver-dig",
            benchmark_family="mle-bench",
            source_digest="sha256:" + "0" * 64,
            task_digest=binding.task_digest,
            verifier_digest="sha256:" + "9" * 64,  # mismatch
            metric_config_digest=binding.metric_config_digest,
            visible_outcome_binding_digest=binding.visible_outcome_binding_digest,
            hidden_outcome_binding_digest=binding.hidden_outcome_binding_digest,
            score_direction="lower",
            score_scale_binding=binding,
            selected_iteration_id="i1",
            iterations=(ResearchIterationV1(iteration_id="i1", visible_score=1.0),),
        )

    # 4. metric_config_digest mismatch -> ValueError
    with pytest.raises(
        ValueError, match="trace metric_config_digest .* does not match score_scale_binding"
    ):
        ResearchRunTraceV1(
            run_id="mismatch-met-dig",
            benchmark_family="mle-bench",
            source_digest="sha256:" + "0" * 64,
            task_digest=binding.task_digest,
            verifier_digest=binding.verifier_digest,
            metric_config_digest="sha256:" + "9" * 64,  # mismatch
            visible_outcome_binding_digest=binding.visible_outcome_binding_digest,
            hidden_outcome_binding_digest=binding.hidden_outcome_binding_digest,
            score_direction="lower",
            score_scale_binding=binding,
            selected_iteration_id="i1",
            iterations=(ResearchIterationV1(iteration_id="i1", visible_score=1.0),),
        )

    # 5. visible_outcome_binding_digest mismatch -> ValueError
    with pytest.raises(
        ValueError,
        match="trace visible_outcome_binding_digest .* does not match score_scale_binding",
    ):
        ResearchRunTraceV1(
            run_id="mismatch-vis-dig",
            benchmark_family="mle-bench",
            source_digest="sha256:" + "0" * 64,
            task_digest=binding.task_digest,
            verifier_digest=binding.verifier_digest,
            metric_config_digest=binding.metric_config_digest,
            visible_outcome_binding_digest="sha256:" + "9" * 64,  # mismatch
            hidden_outcome_binding_digest=binding.hidden_outcome_binding_digest,
            score_direction="lower",
            score_scale_binding=binding,
            selected_iteration_id="i1",
            iterations=(ResearchIterationV1(iteration_id="i1", visible_score=1.0),),
        )

    # 6. hidden_outcome_binding_digest mismatch -> ValueError
    with pytest.raises(
        ValueError,
        match="trace hidden_outcome_binding_digest .* does not match score_scale_binding",
    ):
        ResearchRunTraceV1(
            run_id="mismatch-hid-dig",
            benchmark_family="mle-bench",
            source_digest="sha256:" + "0" * 64,
            task_digest=binding.task_digest,
            verifier_digest=binding.verifier_digest,
            metric_config_digest=binding.metric_config_digest,
            visible_outcome_binding_digest=binding.visible_outcome_binding_digest,
            hidden_outcome_binding_digest="sha256:" + "9" * 64,  # mismatch
            score_direction="lower",
            score_scale_binding=binding,
            selected_iteration_id="i1",
            iterations=(ResearchIterationV1(iteration_id="i1", visible_score=1.0),),
        )

    # 7. Game2048 scenario: hidden_score exists WITHOUT score_scale_binding (non-comparable scales)
    game2048_trace = ResearchRunTraceV1(
        run_id="game2048-unbound-hidden",
        benchmark_family="rsi-exam/game2048",
        source_digest="sha256:" + "e" * 64,
        baseline_visible_score=10.0,
        score_direction="higher",
        score_scale_binding=None,  # NO binding
        hidden_score=1500.0,  # hidden score in raw game score scale vs visible normalized 10.0
        selected_iteration_id="v1",
        iterations=(ResearchIterationV1(iteration_id="v1", visible_score=12.0),),
    )
    features_game2048 = extract_autonomous_research_features(game2048_trace)
    assert features_game2048.hidden_score == 1500.0
    assert features_game2048.final_visible_score == 12.0
    assert features_game2048.score_scale_compatible is False
    assert features_game2048.scale_binding_digest is None
    assert (
        features_game2048.visible_hidden_transfer_gap is None
    )  # MUST be NULL, refuses incomparable transfer!


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
    assert features.selection_decision_count == 0
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
            ResearchIterationV1(iteration_id="i5", visible_score=15.0, disposition="kept"),
            ResearchIterationV1(iteration_id="i6", visible_score=14.0, disposition="kept"),
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


def test_lower_is_better_score_direction_semantics(tmp_path: Path) -> None:
    task_dir = tmp_path / "task_lower"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("name = 'task_lower'\n", encoding="utf-8")
    (task_dir / "instruction.md").write_text("# Task Lower\n", encoding="utf-8")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_eval.py").write_text("def test_it(): pass\n", encoding="utf-8")

    from evallab.registry import compute_task_digests

    task_digests = compute_task_digests(task_dir)
    metric_cfg = {"metric": "loss"}
    vis_outcome = {"score": 2.5}
    hid_outcome = {"score": 1.7}

    import hashlib
    import json

    def _d(v: dict[str, Any]) -> str:
        return (
            "sha256:"
            + hashlib.sha256(
                json.dumps(v, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )

    scale_binding = _sample_scale_binding(
        metric_name="loss",
        direction="lower",
        task_digest=task_digests.package,
        verifier_digest=task_digests.verifier,
        metric_config_digest=_d(metric_cfg),
        visible_outcome_binding_digest=_d(vis_outcome),
        hidden_outcome_binding_digest=_d(hid_outcome),
        visible_split_id="val",
        hidden_split_id="test",
    )
    trace = ResearchRunTraceV1(
        run_id="loss-opt-run",
        benchmark_family="mle-bench/loss-minimization",
        source_digest="sha256:" + "6" * 64,
        task_digest=scale_binding.task_digest,
        verifier_digest=scale_binding.verifier_digest,
        metric_config_digest=scale_binding.metric_config_digest,
        visible_outcome_binding_digest=scale_binding.visible_outcome_binding_digest,
        hidden_outcome_binding_digest=scale_binding.hidden_outcome_binding_digest,
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
    features = extract_autonomous_research_features(
        trace,
        task_dir=task_dir,
        metric_config=metric_cfg,
        visible_outcome=vis_outcome,
        hidden_outcome=hid_outcome,
    )

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


def test_unmeasured_revert_calculates_rollback_rate_correctly() -> None:
    # Real Game2048 scenario: v1 and v2 kept with scores, v3 reverted on unscored diagnostic
    trace = ResearchRunTraceV1(
        run_id="game2048-pilot",
        benchmark_family="rsi-exam/game2048",
        source_digest="sha256:" + "d" * 64,
        baseline_visible_score=10.0,
        iterations=(
            ResearchIterationV1(iteration_id="v1", visible_score=12.0, disposition="kept"),
            ResearchIterationV1(iteration_id="v2", visible_score=14.0, disposition="kept"),
            ResearchIterationV1(iteration_id="v3", visible_score=None, disposition="reverted"),
        ),
    )
    features = extract_autonomous_research_features(trace)
    assert features.measured_iteration_count == 2
    assert features.kept_iteration_count == 2
    assert features.reverted_iteration_count == 1
    assert features.selection_decision_count == 3
    assert features.rollback_rate == pytest.approx(1 / 3)  # NOT 1.0 or broken by measured_count!

    # Zero decisions trace (all observed)
    zero_decision_trace = ResearchRunTraceV1(
        run_id="zero-decisions",
        benchmark_family="rsi-exam/game2048",
        source_digest="sha256:" + "e" * 64,
        iterations=(
            ResearchIterationV1(iteration_id="v1", visible_score=12.0, disposition="observed"),
        ),
    )
    features_zero = extract_autonomous_research_features(zero_decision_trace)
    assert features_zero.selection_decision_count == 0
    assert features_zero.rollback_rate is None  # MUST be NULL when selection_decision_count == 0


def test_feature_registry_governance_for_autonomous_research_family() -> None:
    family_features = TRAJECTORY_FEATURE_REGISTRY.by_family("autonomous-research-v1")
    assert len(family_features) == 74, (
        f"Expected 74 registered features, got {len(family_features)}"
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
    scale_binding = _sample_scale_binding(
        metric_name="score",
        direction="higher",
        visible_split_id="val",
        hidden_split_id="test",
    )
    trace = ResearchRunTraceV1(
        run_id="eval-run-yield",
        benchmark_family="rsi-exam",
        source_kind="synthetic",
        source_version="v1",
        source_record_id="rec-1",
        source_revision_id="rev-1",
        source_digest="sha256:" + "9" * 64,
        task_digest=scale_binding.task_digest,
        verifier_digest=scale_binding.verifier_digest,
        metric_config_digest=scale_binding.metric_config_digest,
        visible_outcome_binding_digest=scale_binding.visible_outcome_binding_digest,
        hidden_outcome_binding_digest=scale_binding.hidden_outcome_binding_digest,
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
    assert len(yield_diag["feature_stats"]) == 74


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
