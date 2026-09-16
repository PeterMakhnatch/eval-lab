"""Real upstream composition with deterministic external engine/evaluator seams."""

import json
from pathlib import Path

import pytest

try:
    from gepa.oa.config import OptimizeAnythingConfig
except ImportError:
    OptimizeAnythingConfig = None

# Keep each test collected when the optional engine is absent, as in
# test_gepa_meta_engine.py; module-level importorskip hides the whole module.
pytestmark = pytest.mark.skipif(
    OptimizeAnythingConfig is None, reason="Install the isolated harness-gepa requirements"
)
if OptimizeAnythingConfig is not None:
    from gepa.oa.engine import Result

    from evallab.gepa_optimizer import composition


class Pause(BaseException):
    pass


@pytest.fixture
def composed(tmp_path, monkeypatch):
    started = []
    outputs = {
        "explore-gepa": "better",
        "explore-autoresearch": "worse",
        "explore-meta_harness": "tied",
        "continue-gepa": "final",
    }
    scores = {"seed": 0.5, "better": 0.8, "worse": 0.1, "tied": 0.5, "final": 0.9}
    monkeypatch.setattr(composition, "engine_availability", lambda model: {"all_available": True})

    class ExternalEngine:
        def __init__(self, config):
            self.stage = Path(config.run_dir).name
            self.name = config.engine

        def run(self, task, server):
            started.append((self.stage, task.seed_candidate))
            server.evaluate(task.seed_candidate, task.train_set[0])
            candidate = outputs[self.stage]
            if isinstance(candidate, BaseException):
                raise candidate
            return Result(best_candidate=candidate, best_score=999, total_evals=1)

        def process_result(self, result, output_dir):
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "backend-result.txt").write_text(result.best_candidate)

    monkeypatch.setattr(composition, "_build_engine", ExternalEngine)
    monkeypatch.setattr(composition, "make_meta_harness_engine", ExternalEngine)
    monkeypatch.setattr(composition, "ReplaySafeGepaEngine", ExternalEngine)

    def evaluator(candidate, example):
        value = scores[candidate]
        if isinstance(value, BaseException):
            raise value
        return value, {"receipt_paths": {"job_result": f"retained/{candidate}.json"}}

    def config(stage, engine):
        return OptimizeAnythingConfig(
            engine=engine,
            run_dir=str(tmp_path / "search" / stage),
            output_dir=tmp_path / "upstream" / stage,
            max_concurrency=1,
            max_evals=10,
            sandbox=True,
        )

    def run(**changes):
        args = {
            "seed_candidate": "seed",
            "evaluator": evaluator,
            "dataset": [{"task_id": "dev"}],
            "valset": None,
            "config_factory": config,
            "output_dir": tmp_path / "omni",
            "objective": "Improve success",
            "background": "Instructions only",
            "proposer_model": "zai/glm-5.3-flash",
        }
        return composition.run_omni(**{**args, **changes})

    return run, started, outputs, scores, tmp_path / "omni/stage_receipts"


def test_common_scores_override_engine_claims_and_continue_from_winner(composed):
    run, started, _, _, receipts = composed
    result = run()
    assert result.best_candidate == "final"
    assert result.best_score == 0.9
    assert started == [
        ("explore-gepa", "seed"),
        ("explore-autoresearch", "seed"),
        ("explore-meta_harness", "seed"),
        ("continue-gepa", "better"),
    ]
    assert result.total_evals == 4
    assert (
        json.loads((receipts / "explore-autoresearch.json").read_text())["selected_candidate"]
        == "seed"
    )
    assert (
        json.loads((receipts / "explore-meta_harness.json").read_text())["selected_candidate"]
        == "seed"
    )
    assert json.loads(json.dumps(result.metadata))["exploration_winner_stage"] == "explore-gepa"


def test_continuation_regression_cannot_replace_exploration_winner(composed):
    run, _, outputs, _, _ = composed
    outputs["continue-gepa"] = "worse"
    assert run().best_candidate == "better"


def test_pending_evaluation_stops_later_engines_and_resume_does_not_repropose(composed):
    run, started, _, scores, _ = composed
    scores["better"] = Pause("native trial pending")
    with pytest.raises(Pause):
        run()
    assert started == [("explore-gepa", "seed")]
    scores["better"] = 0.8
    assert run().best_candidate == "final"
    assert [name for name, seed in started].count("explore-gepa") == 1
    before = list(started)
    assert run().best_candidate == "final"
    assert started == before


def test_cached_missing_evidence_does_not_trigger_new_search(composed):
    run, started, _, scores, _ = composed
    run()
    before = list(started)
    scores["better"] = Pause("retained native job missing")
    with pytest.raises(Pause):
        run()
    assert started == before


def test_changed_stage_seed_and_corrupt_receipt_fail_closed(composed):
    run, started, _, _, receipts = composed
    run()
    before = list(started)
    with pytest.raises(ValueError):
        run(seed_candidate="different")
    (receipts / "explore-gepa.json").write_text("{broken")
    with pytest.raises(ValueError):
        run()
    assert started == before


def test_unavailable_engines_halt_before_factory_or_evaluator(tmp_path, monkeypatch):
    monkeypatch.setattr(
        composition,
        "check_prerequisite_facts",
        lambda **kwargs: {
            "live_launch_blocker": "unqualified fail-open sandbox",
            "claude_cli_found": True,
            "is_macos": True,
            "bwrap_found": False,
        },
    )

    def forbidden(*args):
        raise AssertionError("Unavailable engines must not initiate work")

    with pytest.raises(composition.EngineUnavailable) as error:
        composition.run_omni(
            seed_candidate="seed",
            evaluator=forbidden,
            dataset=[{"task_id": "dev"}],
            valset=None,
            config_factory=forbidden,
            output_dir=tmp_path,
            objective="Improve",
            background="Instructions",
            proposer_model="zai/glm-5.3-flash",
        )
    assert set(error.value.report["blocked_engines"]) == {"autoresearch", "meta_harness"}
