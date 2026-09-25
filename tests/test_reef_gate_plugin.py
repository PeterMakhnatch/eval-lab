"""Plugin tests use Reef's actual value types in its separate interpreter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("reef", reason="real Reef integration runs in the separate Reef interpreter")

from evallab_reef_gate.plugin import Factory  # noqa: E402
from evallab_reef_gate.rules import GateConfig  # noqa: E402
from reef.core.evaluation import (  # noqa: E402
    CandidateEvaluator,
    EvaluationResult,
    UpdateCandidate,
)
from reef.train.cordis_backend.backend import HarnessCandidate  # noqa: E402


class ScoredBackend(CandidateEvaluator):
    def __init__(self, candidate_scores: tuple, current_scores: tuple, repeats: int = 5) -> None:
        self.candidate_scores = candidate_scores
        self.current_scores = current_scores
        self.repeats = repeats

    def evaluate(self, candidate: UpdateCandidate) -> EvaluationResult:
        return EvaluationResult(
            evaluator="test_pairs", evaluator_version="1",
            metrics={
                "candidate_scores": self.candidate_scores,
                "current_scores": self.current_scores,
                "episode_repeats": self.repeats,
            },
        )


class BrokenBackend(CandidateEvaluator):
    def evaluate(self, candidate: UpdateCandidate) -> EvaluationResult:
        raise RuntimeError("private provider context must not become a gate record")


def candidate(identifier: str, tasks: int = 1) -> HarnessCandidate:
    return HarnessCandidate(
        candidate_id=identifier,
        candidate_files={}, current_files={}, candidate_entries=(), current_entries=(),
        mutations=(), evaluation_tasks=tuple(f"task {index}" for index in range(tasks)),
    )


def test_plugin_selects_only_a_recorded_significant_candidate(tmp_path: Path) -> None:
    factory = Factory(record_dir=tmp_path / "decisions")
    plugin = factory.build(ScoredBackend((1.0,) * 5, (0.0,) * 5))
    proposal = candidate("five-wins")
    decision = plugin.decide(proposal, plugin.evaluate(proposal))

    assert decision.selected
    assert decision.metrics["p_value"] == 0.03125
    record = json.loads(Path(decision.metrics["decision_record"]).read_text())
    assert record["candidate_id"] == proposal.candidate_id
    assert record["selected"] is True
    assert record["valid_pairs"] == 5
    assert record["reef_commit"] == factory.reef_commit
    assert record["config"]["alpha"] == 0.05
    # A second computation cannot replace the already recorded candidate decision.
    repeated = plugin.decide(proposal, plugin.evaluate(proposal))
    assert not repeated.selected
    assert repeated.metrics["reason_code"] == "decision_record_error"
    assert json.loads(Path(decision.metrics["decision_record"]).read_text()) == record


@pytest.mark.parametrize("scores", [
    ((0.0,) * 5, (None,) * 5),
    ((None,) * 5, (0.0,) * 5),
    ((None,) * 5, (None,) * 5),
])
def test_plugin_missing_scores_are_invalid_not_wins(tmp_path: Path, scores: tuple) -> None:
    plugin = Factory(record_dir=tmp_path / "decisions").build(ScoredBackend(*scores))
    proposal = candidate("missing-scores")
    decision = plugin.decide(proposal, plugin.evaluate(proposal))

    assert not decision.selected
    assert decision.metrics["reason_code"] == "insufficient_evidence"
    assert decision.metrics["invalid_pairs"] == 5
    assert decision.metrics["wins"] == decision.metrics["losses"] == 0
    record = json.loads(Path(decision.metrics["decision_record"]).read_text())
    assert record["invalid_pairs"] == 5


def test_regression_veto_overrides_significance_and_respects_threshold(tmp_path: Path) -> None:
    scores = (0.0, 1.0, 1.0, 1.0, 1.0) + (1.0,) * 10
    current = (1.0,) * 5 + (0.0,) * 10
    strict = Factory(record_dir=tmp_path / "strict").build(ScoredBackend(scores, current))
    proposal = candidate("regression", tasks=3)
    vetoed = strict.decide(proposal, strict.evaluate(proposal))
    assert not vetoed.selected
    assert vetoed.metrics["p_value"] < 0.05
    assert vetoed.metrics["reason_code"] == "regression_veto"

    configured = Factory(
        config=GateConfig(regression_failure_threshold=2), record_dir=tmp_path / "configured"
    ).build(ScoredBackend(scores, current))
    admitted = configured.decide(proposal, configured.evaluate(proposal))
    assert admitted.selected


def test_evaluator_exception_rejects_without_fabricated_side_observations(tmp_path: Path) -> None:
    plugin = Factory(record_dir=tmp_path / "decisions").build(BrokenBackend())
    proposal = candidate("evaluator-error")
    measured = plugin.evaluate(proposal)
    decision = plugin.decide(proposal, measured)

    assert not decision.selected
    assert decision.metrics["reason_code"] == "evaluator_error"
    assert measured.metrics["evaluation_sides"] == []
    record_text = Path(decision.metrics["decision_record"]).read_text()
    record = json.loads(record_text)
    assert record["error_type"] == "RuntimeError"
    assert record["p_value"] is None
    assert "private provider context" not in record_text


def test_configured_selector_rejects_unknown_rule_fields(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "gate.json"
    path.write_text(json.dumps({"alpha": 0.05, "auto_publish": True}))
    monkeypatch.setenv("EVALLAB_REEF_GATE_CONFIG", str(path))
    with pytest.raises(ValueError):
        Factory(record_dir=tmp_path / "decisions")
    assert not (tmp_path / "decisions").exists()
