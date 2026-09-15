"""Genuine Omni composition for GEPA optimizer in Eval Lab.

Official Omni Recipe Specification:
-----------------------------------
1. Exploration Phase:
   - Three engine arms initialized from the identical untouched ``seed_candidate``:
     - ``explore-gepa``: GEPA reflective evolutionary optimization (in-process).
     - ``explore-autoresearch``: AutoResearch Claude Code subprocess optimization.
     - ``explore-meta_harness``: MetaHarness isolated Claude Code subprocess optimization.
   - Executed serially (max_workers=1) via pinned upstream
     ``gepa.oa.ensemble.optimize_best_of``, ensuring fit into a single worker slot.
   - Real engines are constructed through pinned upstream ``_build_engine``, preserving
     the safe ``make_meta_harness_engine`` wrapper for ``meta_harness``.
   - Each completed stage winner is normalized against the untouched input seed over
     the common evaluation pool (``valset`` if provided, else ``dataset``) using the
     SAME evaluator. Ties and regressions retain the input seed, enforcing monotonic
     safety and cross-engine comparability.
   - Every stage records a durable stage receipt JSON under ``output_dir/stage_receipts/``
     containing seed/engine/candidate digests, execution status, common-pool scores,
     lineage, and native evaluator info references.
   - Complete stages can resume without making new proposals, but must revalidate cached
     evidence through the evaluator so missing native evidence halts rather than being
     silently trusted.
   - A shared abort signal is set on any stage error (including BaseException or
     CampaignStopped) to ensure subsequent queued stages halt immediately without
     triggering side effects.
2. Selection:
   - The arm achieving the highest normalized validation score is selected as the
     exploration winner.
3. Continuation Phase:
   - Stage ``continue-<continuation_engine>`` (default ``gepa``) is initialized with the
     exploration winner candidate text as its seed.
   - Supplied with a fresh durable ``stage/run_dir`` from ``config_factory`` (never
     inheriting an exploration GEPA state checkpoint).
   - Executed via pinned upstream ``gepa.optimize_anything.optimize_anything``.
   - Normalized against the continuation input seed and recorded in stage receipts.
   - Returns a released Result / GEPAResult compatible object with bounded,
     non-recursive metadata.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gepa.oa.config import OptimizeAnythingConfig  # ty: ignore[unresolved-import]
from gepa.oa.engine import Result  # ty: ignore[unresolved-import]
from gepa.oa.ensemble import optimize_best_of  # ty: ignore[unresolved-import]
from gepa.optimize_anything import _build_engine, optimize_anything  # ty: ignore[unresolved-import]

from .meta_engine import check_prerequisite_facts, make_meta_harness_engine

PINNED_GEPA_COMMIT = "0632cdb5dcc052e690eab439e1b4a7e3e9cfe407"
OMNI_RECIPE_SOURCE = (
    "Pinned GEPA OA ensemble exploration via gepa.oa.ensemble.optimize_best_of "
    "(gepa, autoresearch, meta_harness from identical seed with max_workers=1) "
    "followed by continuation via gepa.optimize_anything.optimize_anything (gepa default)"
)
EXPLORATION_ENGINES = ("gepa", "autoresearch", "meta_harness")
DEFAULT_CONTINUATION_ENGINE = "gepa"


def compute_candidate_digest(candidate: str) -> str:
    """Return canonical sha256 hex digest for candidate instruction text."""
    return f"sha256:{hashlib.sha256(candidate.encode('utf-8')).hexdigest()}"


class EngineUnavailable(Exception):
    """Raised when one or more named engines cannot meet provider, OS, or metering requirements."""

    def __init__(self, message: str, report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = report or {}


class _AbortSignal:
    """Shared abort coordinator across serial ThreadPool execution."""

    def __init__(self) -> None:
        self.error: BaseException | None = None

    def set(self, exc: BaseException) -> None:
        if self.error is None:
            self.error = exc

    def check(self) -> None:
        if self.error is not None:
            raise self.error


def engine_availability(proposer_model: str) -> dict[str, Any]:
    """Inspect real prerequisites for Omni composition engines.

    Examines platform facts, fail-closed OS sandbox support on macOS,
    Claude CLI availability, provider constraints, and physical-request metering.
    """
    facts = check_prerequisite_facts(sandbox=True)
    engines_report: dict[str, dict[str, Any]] = {}

    # 1. gepa: in-process Python engine with JournaledReflectionLM support
    gepa_reasons: list[str] = []
    gepa_blockers: list[str] = []
    if not proposer_model:
        gepa_blockers.append("No proposer_model specified")
    engines_report["gepa"] = {
        "available": len(gepa_blockers) == 0,
        "reasons": gepa_reasons,
        "blockers": gepa_blockers,
        "in_process": True,
        "physical_request_metering": True,
        "os_sandbox_fail_closed": True,
    }

    # Model provider check for subprocess engines:
    model_lower = (proposer_model or "").lower()
    is_claude_model = ("claude" in model_lower) or model_lower.startswith("anthropic/")

    # 2. autoresearch: Claude CLI subprocess
    auto_reasons: list[str] = []
    auto_blockers: list[str] = []
    if facts.get("live_launch_blocker"):
        auto_blockers.append(facts["live_launch_blocker"])
    if not facts.get("fail_closed_os_launch_supported", False):
        auto_blockers.append("Fail-closed OS sandbox launch not supported on macOS")
    if not facts.get("claude_cli_found", False):
        auto_blockers.append("Claude Code CLI (`claude`) not found on PATH")
    if not is_claude_model:
        auto_blockers.append(
            f"Claude Code subprocess engine requires Anthropic Claude model; "
            f"proposer_model {proposer_model!r} cannot be used without prohibited provider substitution"
        )
    auto_blockers.append(
        "Subprocess engine does not support in-process physical-request metering "
        "or accounting callbacks (user <=12 physical proposer requests limit cannot be enforced)"
    )
    engines_report["autoresearch"] = {
        "available": len(auto_blockers) == 0,
        "reasons": auto_reasons,
        "blockers": auto_blockers,
        "in_process": False,
        "physical_request_metering": False,
        "os_sandbox_fail_closed": facts.get("fail_closed_os_launch_supported", False),
    }

    # 3. meta_harness: Claude CLI subprocess inside meta harness
    meta_reasons: list[str] = []
    meta_blockers: list[str] = []
    if facts.get("live_launch_blocker"):
        meta_blockers.append(facts["live_launch_blocker"])
    if not facts.get("fail_closed_os_launch_supported", False):
        meta_blockers.append("Fail-closed OS sandbox launch not supported on macOS")
    if not facts.get("claude_cli_found", False):
        meta_blockers.append("Claude Code CLI (`claude`) not found on PATH")
    if not is_claude_model:
        meta_blockers.append(
            f"MetaHarness Claude Code engine requires Anthropic Claude model; "
            f"proposer_model {proposer_model!r} cannot be used without prohibited provider substitution"
        )
    meta_blockers.append(
        "Subprocess engine does not support in-process physical-request metering "
        "or accounting callbacks (user <=12 physical proposer requests limit cannot be enforced)"
    )
    engines_report["meta_harness"] = {
        "available": len(meta_blockers) == 0,
        "reasons": meta_reasons,
        "blockers": meta_blockers,
        "in_process": False,
        "physical_request_metering": False,
        "os_sandbox_fail_closed": facts.get("fail_closed_os_launch_supported", False),
    }

    available_engines = [name for name, rep in engines_report.items() if rep["available"]]
    blocked_engines = [name for name, rep in engines_report.items() if not rep["available"]]
    all_available = len(blocked_engines) == 0

    return {
        "schema_version": 1,
        "proposer_model": proposer_model,
        "prerequisites": facts,
        "engines": engines_report,
        "available_engines": available_engines,
        "blocked_engines": blocked_engines,
        "all_available": all_available,
    }


def _evaluate_on_pool(
    evaluator: Callable[[Any, Any], Any],
    candidate: str,
    examples: list[Any],
    abort_signal: _AbortSignal,
) -> tuple[float, list[dict[str, Any]]]:
    """Score candidate against common evaluation pool using the evaluator."""
    total_score = 0.0
    records: list[dict[str, Any]] = []
    for ex in examples:
        abort_signal.check()
        res = evaluator(candidate, ex)
        if isinstance(res, tuple):
            score = float(res[0])
            info = res[1] if len(res) > 1 and isinstance(res[1], dict) else {}
        else:
            score = float(res)
            info = {}
        total_score += score
        records.append({"example": ex, "score": score, "info": info})
    avg_score = total_score / len(examples) if examples else 0.0
    return avg_score, records


def _extract_text_candidate(cand: Any) -> str:
    """Extract string candidate from dict or string representation."""
    if isinstance(cand, dict):
        if "current_candidate" in cand:
            return str(cand["current_candidate"])
        if len(cand) == 1:
            return str(next(iter(cand.values())))
        return str(cand)
    return str(cand)


class StageEngineWrapper:
    """Engine wrapper enforcing stage receipts, revalidation resumption, and abort checks."""

    def __init__(
        self,
        *,
        stage_id: str,
        engine_name: str,
        underlying_engine: Any,
        stage_config: OptimizeAnythingConfig,
        seed_candidate: str,
        eval_pool: list[Any],
        evaluator: Callable[[Any, Any], Any],
        receipts_dir: Path,
        abort_signal: _AbortSignal,
        before_stage: Callable[[], None] | None,
        parent_stage_id: str | None = None,
    ) -> None:
        self.stage_id = stage_id
        self.name = getattr(underlying_engine, "name", engine_name)
        self.engine_name = engine_name
        self.underlying_engine = underlying_engine
        self.stage_config = stage_config
        self.seed_candidate = seed_candidate
        self.eval_pool = eval_pool
        self.evaluator = evaluator
        self.receipts_dir = receipts_dir
        self.abort_signal = abort_signal
        self.before_stage = before_stage
        self.parent_stage_id = parent_stage_id

    def run(self, task: Any, server: Any) -> Result:
        self.abort_signal.check()
        receipt_file = self.receipts_dir / f"{self.stage_id}.json"

        # Check resumption for completed stage
        if receipt_file.exists():
            receipt_data: dict[str, Any] | None = None
            try:
                receipt_data = json.loads(receipt_file.read_text(encoding="utf-8"))
            except Exception:
                receipt_data = None
            if receipt_data and receipt_data.get("status") == "completed":
                try:
                    cached_winner = receipt_data.get("candidate_text")
                    if cached_winner is None:
                        cached_winner = receipt_data.get("retained_candidate_text", self.seed_candidate)
                    cached_seed = receipt_data.get("seed_text", self.seed_candidate)

                    # Revalidate cached winner and seed through evaluator
                    reval_winner_sc, _ = _evaluate_on_pool(
                        self.evaluator, cached_winner, self.eval_pool, self.abort_signal
                    )
                    reval_seed_sc, _ = _evaluate_on_pool(
                        self.evaluator, cached_seed, self.eval_pool, self.abort_signal
                    )

                    if reval_winner_sc <= reval_seed_sc:
                        ret_cand = cached_seed
                        ret_score = reval_seed_sc
                    else:
                        ret_cand = cached_winner
                        ret_score = reval_winner_sc

                    return Result(
                        best_candidate=ret_cand,
                        best_score=ret_score,
                        total_evals=receipt_data.get("total_evals", 0),
                        metadata={
                            "stage_id": self.stage_id,
                            "engine": self.engine_name,
                            "resumed": True,
                            "receipt_path": str(receipt_file),
                        },
                    )
                except BaseException as exc:
                    self.abort_signal.set(exc)
                    raise exc

        self.abort_signal.check()
        if self.before_stage is not None:
            try:
                self.before_stage()
            except BaseException as exc:
                self.abort_signal.set(exc)
                raise exc

        try:
            raw_result = self.underlying_engine.run(task, server)
        except BaseException as exc:
            self.abort_signal.set(exc)
            raise exc

        raw_candidate = _extract_text_candidate(raw_result.best_candidate)

        try:
            seed_sc, seed_recs = _evaluate_on_pool(
                self.evaluator, self.seed_candidate, self.eval_pool, self.abort_signal
            )
            if raw_candidate == self.seed_candidate:
                winner_sc, winner_recs = seed_sc, seed_recs
            else:
                winner_sc, winner_recs = _evaluate_on_pool(
                    self.evaluator, raw_candidate, self.eval_pool, self.abort_signal
                )

            if winner_sc <= seed_sc:
                retained_cand = self.seed_candidate
                retained_score = seed_sc
                retained_seed = True
                retained_recs = seed_recs
            else:
                retained_cand = raw_candidate
                retained_score = winner_sc
                retained_seed = False
                retained_recs = winner_recs

            stage_eval_count = getattr(raw_result, "total_evals", server.budget.used)

            receipt = {
                "stage_id": self.stage_id,
                "engine": self.engine_name,
                "status": "completed",
                "seed_digest": compute_candidate_digest(self.seed_candidate),
                "seed_text": self.seed_candidate,
                "candidate_digest": compute_candidate_digest(raw_candidate),
                "candidate_text": raw_candidate,
                "retained_candidate_digest": compute_candidate_digest(retained_cand),
                "retained_candidate_text": retained_cand,
                "common_pool_scores": {
                    "seed_score": seed_sc,
                    "raw_winner_score": winner_sc,
                    "normalized_score": retained_score,
                    "retained_seed": retained_seed,
                    "num_examples": len(self.eval_pool),
                },
                "output_lineage": {
                    "stage_id": self.stage_id,
                    "parent_stage_id": self.parent_stage_id,
                    "run_dir": str(self.stage_config.run_dir) if self.stage_config.run_dir else None,
                    "output_dir": str(self.stage_config.output_dir) if self.stage_config.output_dir else None,
                },
                "native_evaluator_info_references": {
                    "seed_records": [
                        {
                            "task_id": (
                                getattr(r["example"], "task_id", None)
                                or (r["example"].get("task_id") if isinstance(r["example"], dict) else str(r["example"]))
                            ),
                            "score": r["score"],
                            "receipt_paths": r["info"].get("receipt_paths", {}),
                            "job_path": r["info"].get("job_path"),
                        }
                        for r in seed_recs
                    ],
                    "winner_records": [
                        {
                            "task_id": (
                                getattr(r["example"], "task_id", None)
                                or (r["example"].get("task_id") if isinstance(r["example"], dict) else str(r["example"]))
                            ),
                            "score": r["score"],
                            "receipt_paths": r["info"].get("receipt_paths", {}),
                            "job_path": r["info"].get("job_path"),
                        }
                        for r in winner_recs
                    ],
                },
                "total_evals": stage_eval_count,
                "created_at": datetime.now(UTC).isoformat(),
            }
            receipt_file.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

            return Result(
                best_candidate=retained_cand,
                best_score=retained_score,
                total_evals=stage_eval_count,
                eval_log=getattr(raw_result, "eval_log", []),
                metadata={
                    "stage_id": self.stage_id,
                    "engine": self.engine_name,
                    "raw_candidate": raw_candidate,
                    "retained_candidate": retained_cand,
                    "retained_seed": retained_seed,
                    "receipt_path": str(receipt_file),
                },
            )
        except BaseException as exc:
            self.abort_signal.set(exc)
            raise exc

    def process_result(self, result: Result, output_dir: Path | None) -> None:
        if hasattr(self.underlying_engine, "process_result"):
            try:
                self.underlying_engine.process_result(result, output_dir)
            except Exception:
                pass


def _construct_engine(config: OptimizeAnythingConfig, engine_name: str) -> Any:
    """Build or preserve engine instance without mutating upstream code."""
    if isinstance(config.engine, str):
        if config.engine == "meta_harness" or engine_name == "meta_harness":
            return make_meta_harness_engine(config)
        return _build_engine(config)
    return config.engine


def run_omni(
    *,
    seed_candidate: str,
    evaluator: Callable[[Any, Any], Any],
    dataset: list[Any],
    valset: list[Any] | None,
    config_factory: Callable[[str, str], OptimizeAnythingConfig],
    output_dir: Path,
    objective: str,
    background: str,
    before_stage: Callable[[], None] | None = None,
    continuation_engine: str = DEFAULT_CONTINUATION_ENGINE,
    proposer_model: str | None = None,
) -> Result:
    """Run genuine Omni composition: serial Best-of-3 exploration followed by continuation."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    receipts_dir = output_path / "stage_receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)

    eval_pool = valset if (valset is not None and len(valset) > 0) else dataset
    if not eval_pool:
        raise ValueError("At least one example in dataset or valset is required for evaluation")

    # Check engine availability before any execution / model calls
    stages_to_plan = [
        ("explore-gepa", "gepa"),
        ("explore-autoresearch", "autoresearch"),
        ("explore-meta_harness", "meta_harness"),
        (f"continue-{continuation_engine}", continuation_engine),
    ]

    # Inspect configs to detect model and engine types
    planned_configs: dict[str, OptimizeAnythingConfig] = {}
    detected_model = proposer_model
    for stage_id, engine_name in stages_to_plan:
        cfg = config_factory(stage_id, engine_name)
        planned_configs[stage_id] = cfg
        if detected_model is None and cfg.engine_config and isinstance(cfg.engine_config, dict):
            detected_model = cfg.engine_config.get("model") or cfg.engine_config.get("proposer_model")

    avail_report = engine_availability(detected_model or "unknown")

    # Live run MUST fail closed before any model/target work when named engines cannot meet prerequisites
    for stage_id, engine_name in stages_to_plan:
        cfg = planned_configs[stage_id]
        engine_obj = cfg.engine
        is_named_str = isinstance(engine_obj, str)
        is_real_subprocess = type(engine_obj).__name__ in ("MetaHarnessEngine", "AutoResearchEngine", "SafeMetaHarnessEngine")
        if is_named_str or is_real_subprocess:
            eng_key = engine_name
            rep = avail_report["engines"].get(eng_key, {})
            if not rep.get("available", False):
                blockers = rep.get("blockers", ["Engine unavailable under current environment"])
                raise EngineUnavailable(
                    f"Engine {eng_key!r} is unavailable for live Omni composition: {'; '.join(blockers)}",
                    report=avail_report,
                )

    abort_signal = _AbortSignal()

    # 1. Exploration Phase: Best-of-3 via optimize_best_of
    wrapped_exploration_configs: list[OptimizeAnythingConfig] = []
    for stage_id, engine_name in [
        ("explore-gepa", "gepa"),
        ("explore-autoresearch", "autoresearch"),
        ("explore-meta_harness", "meta_harness"),
    ]:
        cfg = planned_configs[stage_id]
        real_engine = _construct_engine(cfg, engine_name)
        wrapper = StageEngineWrapper(
            stage_id=stage_id,
            engine_name=engine_name,
            underlying_engine=real_engine,
            stage_config=cfg,
            seed_candidate=seed_candidate,
            eval_pool=eval_pool,
            evaluator=evaluator,
            receipts_dir=receipts_dir,
            abort_signal=abort_signal,
            before_stage=before_stage,
            parent_stage_id=None,
        )
        cfg.engine = wrapper
        wrapped_exploration_configs.append(cfg)

    best_of_result = optimize_best_of(
        seed_candidate=seed_candidate,
        evaluator=evaluator,
        configs=wrapped_exploration_configs,
        dataset=dataset,
        valset=valset,
        objective=objective,
        background=background,
        max_workers=1,
    )

    # Sanitize all_results to avoid recursive metadata serialization
    bounded_exploration_results: list[dict[str, Any]] = []
    for i, r in enumerate(best_of_result.metadata.get("all_results", [])):
        if isinstance(r, Result):
            bounded_exploration_results.append({
                "stage_id": r.metadata.get("stage_id", f"explore_stage_{i}"),
                "engine": r.metadata.get("engine", "unknown"),
                "best_candidate_digest": compute_candidate_digest(r.best_candidate) if isinstance(r.best_candidate, str) else None,
                "best_score": float(r.best_score) if r.best_score is not None else float("-inf"),
                "total_evals": int(r.total_evals),
            })
        elif isinstance(r, dict):
            bounded_exploration_results.append({
                "stage_id": r.get("stage_id", f"explore_stage_{i}"),
                "engine": r.get("engine", "unknown"),
                "best_candidate_digest": r.get("best_candidate_digest"),
                "best_score": float(r.get("best_score", float("-inf"))),
                "total_evals": int(r.get("total_evals", 0)),
            })
    best_of_result.metadata["all_results"] = bounded_exploration_results

    exploration_winner_candidate = best_of_result.best_candidate
    exploration_winner_score = best_of_result.best_score
    exploration_winner_stage = best_of_result.metadata.get("stage_id", "unknown")

    # 2. Continuation Phase: fresh optimize_anything continuation
    continue_stage_id = f"continue-{continuation_engine}"
    continue_cfg = planned_configs[continue_stage_id]
    continue_engine = _construct_engine(continue_cfg, continuation_engine)

    continue_wrapper = StageEngineWrapper(
        stage_id=continue_stage_id,
        engine_name=continuation_engine,
        underlying_engine=continue_engine,
        stage_config=continue_cfg,
        seed_candidate=exploration_winner_candidate,
        eval_pool=eval_pool,
        evaluator=evaluator,
        receipts_dir=receipts_dir,
        abort_signal=abort_signal,
        before_stage=before_stage,
        parent_stage_id=exploration_winner_stage,
    )
    continue_cfg.engine = continue_wrapper

    continuation_result = optimize_anything(
        seed_candidate=exploration_winner_candidate,
        evaluator=evaluator,
        dataset=dataset,
        valset=valset,
        test_set=None,
        objective=objective,
        background=background,
        config=continue_cfg,
    )

    final_best_candidate = continuation_result.best_candidate
    final_best_score = continuation_result.best_score
    continuation_evals = getattr(continuation_result, "total_evals", 0)
    total_evals_combined = best_of_result.total_evals + continuation_evals

    final_metadata = {
        "recipe": "omni",
        "omni_source": OMNI_RECIPE_SOURCE,
        "pinned_gepa_commit": PINNED_GEPA_COMMIT,
        "exploration_winner_stage": exploration_winner_stage,
        "exploration_winner_candidate_digest": compute_candidate_digest(exploration_winner_candidate),
        "exploration_winner_score": exploration_winner_score,
        "continuation_stage_id": continue_stage_id,
        "continuation_engine": continuation_engine,
        "seed_candidate_digest": compute_candidate_digest(seed_candidate),
        "best_candidate_digest": compute_candidate_digest(final_best_candidate),
        "exploration_results": bounded_exploration_results,
        "receipt_paths": {
            "explore-gepa": str(receipts_dir / "explore-gepa.json"),
            "explore-autoresearch": str(receipts_dir / "explore-autoresearch.json"),
            "explore-meta_harness": str(receipts_dir / "explore-meta_harness.json"),
            continue_stage_id: str(receipts_dir / f"{continue_stage_id}.json"),
        },
    }

    return Result(
        best_candidate=final_best_candidate,
        best_score=final_best_score,
        total_evals=total_evals_combined,
        eval_log=getattr(continuation_result, "eval_log", []),
        metadata=final_metadata,
    )
