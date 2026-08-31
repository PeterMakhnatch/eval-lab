from __future__ import annotations

import pytest

from evallab.autonomous_research import (
    ResearchIterationV1,
    ResearchRunTraceV1,
    extract_autonomous_research_features,
    parse_jsonl_experiment_log,
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
        iterations=(
            ResearchIterationV1(
                iteration_id="v1",
                hypothesis="Prefer monotonic corners",
                visible_score=11.0,
                disposition="kept",
                changed_bytes=100,
            ),
            ResearchIterationV1(
                iteration_id="v2",
                hypothesis="Maximize immediate merges",
                visible_score=9.0,
                disposition="reverted",
                changed_bytes=100,
            ),
            ResearchIterationV1(
                iteration_id="v3",
                hypothesis="Search two moves",
                disposition="invalid",
                changed_bytes=100,
            ),
            ResearchIterationV1(
                iteration_id="v4",
                hypothesis="Search two moves",
                visible_score=14.0,
                disposition="kept",
                changed_bytes=100,
            ),
            ResearchIterationV1(
                iteration_id="v5",
                hypothesis="Search two moves",
                visible_score=13.0,
                disposition="observed",
                changed_bytes=100,
            ),
        ),
    )
    features = extract_autonomous_research_features(trace)
    assert features.iteration_count == 5
    assert features.measured_iteration_count == 4
    assert features.valid_experiment_rate == 0.8
    assert features.unique_hypothesis_count == 3
    assert features.hypothesis_turnover_rate == 0.6
    assert features.rollback_rate == 0.25
    assert features.regression_count == 2
    assert features.best_visible_score == 14.0
    assert features.final_visible_score == 13.0
    assert features.visible_improvement == 4.0
    assert features.final_selection_regret == 1.0
    assert features.improvement_per_experiment == 1.0
    assert features.first_improvement_iteration == 1
    assert features.late_improvement_share == 0.75
    assert features.visible_hidden_transfer_gap == -1.0
    assert features.budget_utilization_rate == 0.8
    assert features.changed_bytes_per_improvement == 125.0
    assert features.artifact_replay_verified is True
    assert features.feature_digest.startswith("sha256:")


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
    assert extract_autonomous_research_features(trace).visible_hidden_transfer_gap is None


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
