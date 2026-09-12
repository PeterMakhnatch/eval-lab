"""Approval interruption and sealed-input boundaries, not benchmark evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.gepa_optimizer.proposer import JournaledReflectionLM, ProposalUnavailable
from evallab.gepa_optimizer.workflow import QualificationProposer, _EvaluationHalt, load_campaign


def test_completed_proposal_is_replayed_without_another_model_call(tmp_path, monkeypatch):
    pytest.importorskip("gepa", reason="Install the isolated harness-gepa requirements")
    import gepa.lm

    calls = []

    class ResponseModel:
        total_cost = 0.0

        def __init__(self, *args, **kwargs):
            pass

        def __call__(self, prompt):
            calls.append(prompt)
            return "retained proposal"

    monkeypatch.setattr(gepa.lm, "LM", ResponseModel)
    first = JournaledReflectionLM(model="test/model", directory=tmp_path, max_requests=1)
    assert first("development feedback") == "retained proposal"
    resumed = JournaledReflectionLM(model="test/model", directory=tmp_path, max_requests=1)
    assert resumed("development feedback") == "retained proposal"
    with pytest.raises(ProposalUnavailable):
        resumed("different feedback")
    assert calls == ["development feedback"]


def test_ambiguous_model_failure_is_not_automatically_retried(tmp_path, monkeypatch):
    pytest.importorskip("gepa", reason="Install the isolated harness-gepa requirements")
    import gepa.lm

    calls = []

    class FailedModel:
        total_cost = 0.0

        def __init__(self, *args, **kwargs):
            pass

        def __call__(self, prompt):
            calls.append(prompt)
            raise TimeoutError("response outcome unknown")

    monkeypatch.setattr(gepa.lm, "LM", FailedModel)
    first = JournaledReflectionLM(model="test/model", directory=tmp_path, max_requests=2)
    with pytest.raises(TimeoutError):
        first("development feedback")
    resumed = JournaledReflectionLM(model="test/model", directory=tmp_path, max_requests=2)
    with pytest.raises(ProposalUnavailable):
        resumed("development feedback")
    assert len(calls) == 1


def test_released_gepa_does_not_turn_lab_halt_into_zero_reward(tmp_path):
    pytest.importorskip("gepa", reason="Install the isolated harness-gepa requirements")
    from gepa.optimize_anything import OptimizeAnythingConfig, optimize_anything

    def pending(candidate, example):
        raise _EvaluationHalt(RuntimeError("Lab evaluation unavailable"))

    with pytest.raises(_EvaluationHalt):
        optimize_anything(
            seed_candidate="seed",
            evaluator=pending,
            dataset=[{"id": "dev"}],
            config=OptimizeAnythingConfig(
                max_evals=4,
                max_concurrency=1,
                output_dir=tmp_path / "out",
                run_dir=str(tmp_path / "state"),
                engine_config={
                    "reflection": {
                        "reflection_lm": None,
                        "custom_candidate_proposer": QualificationProposer(),
                    }
                },
            ),
        )


def test_campaign_rejects_final_tasks_before_evaluator_construction(tmp_path: Path):
    config = {
        "name": "sealed",
        "engine": "gepa",
        "agent": "oracle",
        "max_evals": 4,
        "seed_candidate_path": "seed.txt",
        "output_dir": "out",
        "examples": [
            {
                "task_id": "sealed",
                "task_path": "tasks/sealed",
                "task_package_digest": "sha256:" + "a" * 64,
                "split": "final",
            }
        ],
    }
    path = tmp_path / "campaign.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        load_campaign(path, tmp_path)
