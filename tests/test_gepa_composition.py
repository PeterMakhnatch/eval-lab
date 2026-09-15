"""Focused behavioral boundary tests for Omni composition in Eval Lab."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure src is in sys.path when running tests directly without editable install
_SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))
try:
    import gepa.oa.config as gepa_config
except ImportError:  # the isolated harness-gepa requirements are optional
    gepa_config = None

pytestmark = pytest.mark.skipif(
    gepa_config is None, reason="Install the isolated harness-gepa requirements"
)

if gepa_config is not None:
    from evallab.gepa_optimizer.composition import (
        DEFAULT_CONTINUATION_ENGINE,
        EXPLORATION_ENGINES,
        OMNI_RECIPE_SOURCE,
        PINNED_GEPA_COMMIT,
        EngineUnavailable,
        StageEngineWrapper,
        _AbortSignal,
        compute_candidate_digest,
        engine_availability,
        run_omni,
    )
    from gepa.oa.config import OptimizeAnythingConfig
    from gepa.oa.engine import Result


class DummyDeterministicEngine:
    """External deterministic engine double for CPU tests without model calls."""

    def __init__(
        self,
        name: str,
        output_candidate: str,
        score: float = 0.8,
        total_evals: int = 2,
        should_fail: bool = False,
        failure_exception: BaseException | None = None,
    ) -> None:
        self.name = name
        self.output_candidate = output_candidate
        self.score = score
        self.total_evals = total_evals
        self.should_fail = should_fail
        self.failure_exception = failure_exception or RuntimeError("Engine failure")
        self.run_calls = 0

    def run(self, task: Any, server: Any) -> Result:
        self.run_calls += 1
        if self.should_fail:
            raise self.failure_exception
        return Result(
            best_candidate=self.output_candidate,
            best_score=self.score,
            total_evals=self.total_evals,
            metadata={"engine": self.name},
        )

    def process_result(self, result: Result, output_dir: Path | None) -> None:
        pass


def test_engine_availability_macos_flash_blocks_subprocess_engines() -> None:
    """Verify engine_availability reports explicit blockers on macOS for subprocess engines."""
    report = engine_availability("gemini-2.5-flash")

    assert report["schema_version"] == 1
    assert report["proposer_model"] == "gemini-2.5-flash"
    assert "gepa" in report["engines"]
    assert "autoresearch" in report["engines"]
    assert "meta_harness" in report["engines"]

    # In-process gepa is available
    assert report["engines"]["gepa"]["available"] is True
    assert report["engines"]["gepa"]["in_process"] is True
    assert report["engines"]["gepa"]["physical_request_metering"] is True

    # Subprocess autoresearch is blocked
    auto_rep = report["engines"]["autoresearch"]
    assert auto_rep["available"] is False
    assert auto_rep["in_process"] is False
    assert auto_rep["physical_request_metering"] is False
    # Must report macOS fail-open blocker, physical metering blocker, and provider constraint
    blockers_text = " ".join(auto_rep["blockers"])
    assert "physical-request metering" in blockers_text
    assert "provider substitution" in blockers_text

    # Subprocess meta_harness is blocked
    meta_rep = report["engines"]["meta_harness"]
    assert meta_rep["available"] is False
    assert meta_rep["in_process"] is False
    assert meta_rep["physical_request_metering"] is False
    meta_blockers = " ".join(meta_rep["blockers"])
    assert "physical-request metering" in meta_blockers

    assert report["all_available"] is False
    assert "autoresearch" in report["blocked_engines"]
    assert "meta_harness" in report["blocked_engines"]

    # Test EngineUnavailable carries report
    exc = EngineUnavailable("Engine unavailable", report=report)
    assert exc.report == report
    assert str(exc) == "Engine unavailable"


def test_run_omni_fails_closed_on_blocked_named_engines(tmp_path: Path) -> None:
    """Verify run_omni fails closed immediately before any evaluator call if named engines cannot run."""
    evaluator_calls = 0

    def mock_evaluator(candidate: str, example: Any) -> tuple[float, dict[str, Any]]:
        nonlocal evaluator_calls
        evaluator_calls += 1
        return 0.5, {}

    def config_factory(stage_id: str, engine: str) -> OptimizeAnythingConfig:
        return OptimizeAnythingConfig(
            engine=engine,
            run_dir=str(tmp_path / stage_id / "run"),
            output_dir=tmp_path / stage_id / "output",
            engine_config={"model": "gemini-2.5-flash"},
        )

    with pytest.raises(EngineUnavailable) as exc_info:
        run_omni(
            seed_candidate="Baseline seed",
            evaluator=mock_evaluator,
            dataset=[{"task_id": "task_1"}],
            valset=None,
            config_factory=config_factory,
            output_dir=tmp_path / "omni_out",
            objective="Improve prompt",
            background="Evaluation task",
            proposer_model="gemini-2.5-flash",
        )

    # Must have failed closed before ANY evaluator call
    assert evaluator_calls == 0
    assert "autoresearch" in exc_info.value.report["blocked_engines"] or "meta_harness" in exc_info.value.report["blocked_engines"]


def test_observable_selection_and_tie_monotonicity(tmp_path: Path) -> None:
    """Verify normalization against seed candidate, tie monotonicity, and cross-engine selection."""
    engines: dict[str, DummyDeterministicEngine] = {
        "explore-gepa": DummyDeterministicEngine("gepa", "better_candidate", score=0.85),
        "explore-autoresearch": DummyDeterministicEngine("autoresearch", "worse_candidate", score=0.2),
        "explore-meta_harness": DummyDeterministicEngine("meta_harness", "tied_candidate", score=0.5),
        "continue-gepa": DummyDeterministicEngine("continue_gepa", "continued_candidate", score=0.9),
    }

    def mock_evaluator(candidate: str, example: Any) -> tuple[float, dict[str, Any]]:
        # Evaluator scores based on candidate content deterministically
        if candidate == "better_candidate":
            return 0.85, {"job_path": "/jobs/better", "receipt_paths": {"job_result": "/jobs/better/res.json"}}
        if candidate == "worse_candidate":
            return 0.2, {"job_path": "/jobs/worse", "receipt_paths": {"job_result": "/jobs/worse/res.json"}}
        if candidate == "tied_candidate":
            return 0.5, {"job_path": "/jobs/tied", "receipt_paths": {"job_result": "/jobs/tied/res.json"}}
        if candidate == "continued_candidate":
            return 0.9, {"job_path": "/jobs/continued", "receipt_paths": {"job_result": "/jobs/continued/res.json"}}
        if candidate == "seed_candidate":
            return 0.5, {"job_path": "/jobs/seed", "receipt_paths": {"job_result": "/jobs/seed/res.json"}}
        return 0.1, {}

    def config_factory(stage_id: str, engine: str) -> OptimizeAnythingConfig:
        engine_instance = engines[stage_id]
        return OptimizeAnythingConfig(
            engine=engine_instance,
            run_dir=str(tmp_path / stage_id / "run"),
            output_dir=tmp_path / stage_id / "output",
        )

    output_dir = tmp_path / "omni_test_out"
    result = run_omni(
        seed_candidate="seed_candidate",
        evaluator=mock_evaluator,
        dataset=[{"task_id": "task_dev_1"}, {"task_id": "task_dev_2"}],
        valset=None,
        config_factory=config_factory,
        output_dir=output_dir,
        objective="Maximize benchmark success",
        background="Context instructions",
    )

    # Continuation won with continued_candidate at score 0.9
    assert result.best_candidate == "continued_candidate"
    assert result.best_score == 0.9
    assert result.metadata["recipe"] == "omni"
    assert result.metadata["exploration_winner_score"] == 0.85

    # Check stage receipts on disk
    receipts_dir = output_dir / "stage_receipts"
    assert receipts_dir.exists()

    gepa_receipt = json.loads((receipts_dir / "explore-gepa.json").read_text())
    assert gepa_receipt["status"] == "completed"
    assert gepa_receipt["retained_candidate_text"] == "better_candidate"
    assert gepa_receipt["common_pool_scores"]["retained_seed"] is False
    assert gepa_receipt["common_pool_scores"]["normalized_score"] == 0.85

    auto_receipt = json.loads((receipts_dir / "explore-autoresearch.json").read_text())
    assert auto_receipt["status"] == "completed"
    assert auto_receipt["candidate_text"] == "worse_candidate"
    # Regression: retained input seed!
    assert auto_receipt["retained_candidate_text"] == "seed_candidate"
    assert auto_receipt["common_pool_scores"]["retained_seed"] is True
    assert auto_receipt["common_pool_scores"]["normalized_score"] == 0.5

    meta_receipt = json.loads((receipts_dir / "explore-meta_harness.json").read_text())
    assert meta_receipt["status"] == "completed"
    assert meta_receipt["candidate_text"] == "tied_candidate"
    # Tie: retained input seed!
    assert meta_receipt["retained_candidate_text"] == "seed_candidate"
    assert meta_receipt["common_pool_scores"]["retained_seed"] is True
    assert meta_receipt["common_pool_scores"]["normalized_score"] == 0.5


def test_same_seed_versus_fresh_continuation(tmp_path: Path) -> None:
    """Verify exploration arms receive the same seed, while continuation gets winner and fresh run_dir."""
    observed_seeds: dict[str, str] = {}
    observed_run_dirs: dict[str, str] = {}

    class TrackingEngine:
        def __init__(self, name: str, output: str) -> None:
            self.name = name
            self.output = output

        def run(self, task: Any, server: Any) -> Result:
            observed_seeds[self.name] = task.seed_candidate
            return Result(best_candidate=self.output, best_score=0.8, total_evals=1)

    engines = {
        "explore-gepa": TrackingEngine("explore-gepa", "exp_gepa_cand"),
        "explore-autoresearch": TrackingEngine("explore-autoresearch", "exp_auto_cand"),
        "explore-meta_harness": TrackingEngine("explore-meta_harness", "exp_meta_cand"),
        "continue-gepa": TrackingEngine("continue-gepa", "final_cont_cand"),
    }

    def evaluator(candidate: str, example: Any) -> float:
        if candidate == "exp_gepa_cand":
            return 0.8
        if candidate == "final_cont_cand":
            return 0.95
        return 0.5

    def config_factory(stage_id: str, engine: str) -> OptimizeAnythingConfig:
        run_d = str(tmp_path / "runs" / stage_id)
        observed_run_dirs[stage_id] = run_d
        return OptimizeAnythingConfig(
            engine=engines[stage_id],
            run_dir=run_d,
            output_dir=tmp_path / "outputs" / stage_id,
        )

    initial_seed = "initial_untouched_seed"
    res = run_omni(
        seed_candidate=initial_seed,
        evaluator=evaluator,
        dataset=[{"task_id": "task_1"}],
        valset=None,
        config_factory=config_factory,
        output_dir=tmp_path / "omni_seed_test",
        objective="Test seeds",
        background="Background",
    )

    # Exploration arms received identical seed
    assert observed_seeds["explore-gepa"] == initial_seed
    assert observed_seeds["explore-autoresearch"] == initial_seed
    assert observed_seeds["explore-meta_harness"] == initial_seed

    # Continuation received exploration winner
    assert observed_seeds["continue-gepa"] == "exp_gepa_cand"

    # Continuation had its own fresh run_dir
    assert observed_run_dirs["continue-gepa"] != observed_run_dirs["explore-gepa"]
    assert res.best_candidate == "final_cont_cand"


def test_interruption_prevents_later_stage_side_effects(tmp_path: Path) -> None:
    """Verify BaseException / CampaignStopped in early stage sets abort signal and stops queued stages."""
    class HaltException(BaseException):
        pass

    engine_gepa = DummyDeterministicEngine("gepa", "gepa_cand", should_fail=True, failure_exception=HaltException("Halt"))
    engine_auto = DummyDeterministicEngine("autoresearch", "auto_cand")
    engine_meta = DummyDeterministicEngine("meta_harness", "meta_cand")
    engine_cont = DummyDeterministicEngine("continue_gepa", "cont_cand")

    engines = {
        "explore-gepa": engine_gepa,
        "explore-autoresearch": engine_auto,
        "explore-meta_harness": engine_meta,
        "continue-gepa": engine_cont,
    }

    def config_factory(stage_id: str, engine: str) -> OptimizeAnythingConfig:
        return OptimizeAnythingConfig(
            engine=engines[stage_id],
            run_dir=str(tmp_path / stage_id / "run"),
            output_dir=tmp_path / stage_id / "output",
        )

    output_dir = tmp_path / "interrupted_out"
    with pytest.raises(HaltException):
        run_omni(
            seed_candidate="seed",
            evaluator=lambda c, ex: 0.5,
            dataset=[{"task_id": "task_1"}],
            valset=None,
            config_factory=config_factory,
            output_dir=output_dir,
            objective="Obj",
            background="Bg",
        )

    # Gepa engine attempted run and failed
    assert engine_gepa.run_calls == 1
    # Subsequent queued engines in ThreadPool were blocked by abort signal before run() side effects
    assert engine_auto.run_calls == 0
    assert engine_meta.run_calls == 0
    assert engine_cont.run_calls == 0

    # No completed receipts written for stages 2, 3, or continuation
    receipts_dir = output_dir / "stage_receipts"
    assert not (receipts_dir / "explore-autoresearch.json").exists()
    assert not (receipts_dir / "explore-meta_harness.json").exists()
    assert not (receipts_dir / "continue-gepa.json").exists()


def test_resumed_complete_stage_does_not_repropose(tmp_path: Path) -> None:
    """Verify already completed stage revalidates via evaluator without invoking engine proposals."""
    engine_gepa = DummyDeterministicEngine("gepa", "gepa_cand", score=0.8)
    engine_auto = DummyDeterministicEngine("autoresearch", "auto_cand", score=0.7)
    engine_meta = DummyDeterministicEngine("meta_harness", "meta_cand", score=0.6)
    engine_cont = DummyDeterministicEngine("continue_gepa", "cont_cand", score=0.9)

    engines = {
        "explore-gepa": engine_gepa,
        "explore-autoresearch": engine_auto,
        "explore-meta_harness": engine_meta,
        "continue-gepa": engine_cont,
    }

    def config_factory(stage_id: str, engine: str) -> OptimizeAnythingConfig:
        return OptimizeAnythingConfig(
            engine=engines[stage_id],
            run_dir=str(tmp_path / stage_id / "run"),
            output_dir=tmp_path / stage_id / "output",
        )

    output_dir = tmp_path / "resume_test_out"

    # First run: all stages execute
    res1 = run_omni(
        seed_candidate="seed",
        evaluator=lambda c, ex: (0.9 if c == "cont_cand" else (0.8 if c == "gepa_cand" else 0.5)),
        dataset=[{"task_id": "task_1"}],
        valset=None,
        config_factory=config_factory,
        output_dir=output_dir,
        objective="Obj",
        background="Bg",
    )
    assert res1.best_candidate == "cont_cand"
    assert engine_gepa.run_calls == 1
    assert engine_auto.run_calls == 1
    assert engine_meta.run_calls == 1
    assert engine_cont.run_calls == 1

    # Second run: all stages are already completed with receipts on disk
    # Engines should NOT be called again
    res2 = run_omni(
        seed_candidate="seed",
        evaluator=lambda c, ex: (0.9 if c == "cont_cand" else (0.8 if c == "gepa_cand" else 0.5)),
        dataset=[{"task_id": "task_1"}],
        valset=None,
        config_factory=config_factory,
        output_dir=output_dir,
        objective="Obj",
        background="Bg",
    )
    assert res2.best_candidate == "cont_cand"
    # Proposal count did not increase!
    assert engine_gepa.run_calls == 1
    assert engine_auto.run_calls == 1
    assert engine_meta.run_calls == 1
    assert engine_cont.run_calls == 1


def test_cached_missing_evidence_halts(tmp_path: Path) -> None:
    """Verify resumption with missing native evidence halts via evaluator error rather than trusting cached score."""
    output_dir = tmp_path / "missing_evidence_out"
    receipts_dir = output_dir / "stage_receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)

    # Pre-seed a receipt that claims success
    fake_receipt = {
        "stage_id": "explore-gepa",
        "engine": "gepa",
        "status": "completed",
        "seed_digest": compute_candidate_digest("seed"),
        "seed_text": "seed",
        "candidate_digest": compute_candidate_digest("winner"),
        "candidate_text": "winner",
        "retained_candidate_digest": compute_candidate_digest("winner"),
        "retained_candidate_text": "winner",
        "common_pool_scores": {"seed_score": 0.5, "normalized_score": 0.9},
        "output_lineage": {"stage_id": "explore-gepa"},
        "native_evaluator_info_references": {},
        "total_evals": 5,
        "created_at": "2026-09-15T00:00:00Z",
    }
    (receipts_dir / "explore-gepa.json").write_text(json.dumps(fake_receipt))

    # Evaluator raises because native evidence is missing on disk
    def failing_evaluator(candidate: str, example: Any) -> tuple[float, dict[str, Any]]:
        raise RuntimeError("Missing evaluation receipt in evaluations/ directory")

    def config_factory(stage_id: str, engine: str) -> OptimizeAnythingConfig:
        return OptimizeAnythingConfig(
            engine=DummyDeterministicEngine(engine, "dummy"),
            run_dir=str(tmp_path / stage_id / "run"),
            output_dir=tmp_path / stage_id / "output",
        )

    with pytest.raises(RuntimeError, match="Missing evaluation receipt"):
        run_omni(
            seed_candidate="seed",
            evaluator=failing_evaluator,
            dataset=[{"task_id": "task_1"}],
            valset=None,
            config_factory=config_factory,
            output_dir=output_dir,
            objective="Obj",
            background="Bg",
        )


def test_bounded_metadata_no_recursive_all_results(tmp_path: Path) -> None:
    """Verify result metadata is clean, bounded, and JSON-serializable without recursive all_results."""
    def config_factory(stage_id: str, engine: str) -> OptimizeAnythingConfig:
        return OptimizeAnythingConfig(
            engine=DummyDeterministicEngine(engine, f"cand_{stage_id}", score=0.7),
            run_dir=str(tmp_path / stage_id / "run"),
            output_dir=tmp_path / stage_id / "output",
        )

    res = run_omni(
        seed_candidate="seed",
        evaluator=lambda c, ex: 0.7,
        dataset=[{"task_id": "task_1"}],
        valset=None,
        config_factory=config_factory,
        output_dir=tmp_path / "meta_test",
        objective="Obj",
        background="Bg",
    )

    # Must serialize cleanly to JSON
    serialized = json.dumps(res.metadata)
    data = json.loads(serialized)
    assert data["recipe"] == "omni"
    assert data["omni_source"] == OMNI_RECIPE_SOURCE
    assert data["pinned_gepa_commit"] == PINNED_GEPA_COMMIT
    assert isinstance(data["exploration_results"], list)
    for entry in data["exploration_results"]:
        assert "stage_id" in entry
        assert "engine" in entry
        assert "best_score" in entry
        assert "total_evals" in entry
        # No recursive full result objects
        assert not isinstance(entry.get("all_results"), list)
