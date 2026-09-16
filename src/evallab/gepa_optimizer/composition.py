"""Pinned Omni recipe: three genuine engines, common scoring, fresh continuation.

Source: https://gepa-ai.github.io/gepa/blog/2026/07/22/optimize-anything-omni/
Exploration is serial to respect the single native target slot. Live subprocess
engines remain gated until their OS/provider/request-metering route is qualified.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from gepa.oa.engine import Result  # ty: ignore[unresolved-import]
from gepa.oa.ensemble import optimize_best_of  # ty: ignore[unresolved-import]
from gepa.optimize_anything import _build_engine, optimize_anything  # ty: ignore[unresolved-import]

from .meta_engine import (
    check_prerequisite_facts,
    compute_candidate_digest,
    make_meta_harness_engine,
)
from .proposer import ReplaySafeGepaEngine, direct_proposer_blocker
from .release import COMMIT, verify_release

OMNI_RECIPE_SOURCE = "https://gepa-ai.github.io/gepa/blog/2026/07/22/optimize-anything-omni/"
EXPLORATION_ENGINES = ("gepa", "autoresearch", "meta_harness")


class EngineUnavailable(Exception):
    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        super().__init__("Omni engines unavailable: " + ", ".join(report["blocked_engines"]))


def engine_availability(proposer_model: str) -> dict[str, Any]:
    facts = check_prerequisite_facts(sandbox=True)
    gepa_blocker = (
        direct_proposer_blocker(proposer_model) if proposer_model else "Missing proposer model"
    )
    engines = {
        "gepa": {
            "available": gepa_blocker is None,
            "blockers": [gepa_blocker] if gepa_blocker else [],
        }
    }
    for name in EXPLORATION_ENGINES[1:]:
        blockers = []
        if facts["live_launch_blocker"]:
            blockers.append(facts["live_launch_blocker"])
        if not facts["claude_cli_found"]:
            blockers.append("Released engine requires Claude Code CLI")
        if not facts["is_macos"] and not facts["bwrap_found"]:
            blockers.append("Released engine requires a qualified fail-closed bwrap route")
        if proposer_model.startswith("zai/"):
            blockers.append(
                "Existing Flash Coding Plan transport is not wired or qualified for this Claude Code engine; no provider substitution"
            )
        blockers.append(
            "Released subprocess engine has no qualified physical-request meter enforcing the shared proposer-request ceiling"
        )
        engines[name] = {"available": not blockers, "blockers": blockers}
    return {
        "release_commit": COMMIT,
        "recipe": OMNI_RECIPE_SOURCE,
        "prerequisites": facts,
        "proposer_model": proposer_model,
        "engines": engines,
        "blocked_engines": [name for name, row in engines.items() if not row["available"]],
        "all_available": all(row["available"] for row in engines.values()),
    }


def _write(path: Path, payload: dict[str, Any]) -> None:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("Stage receipt paths must not traverse symlinks")
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _text(candidate: Any) -> str:
    if isinstance(candidate, dict) and set(candidate) == {"current_candidate"}:
        candidate = candidate["current_candidate"]
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError("Released engine must return nonempty instruction text")
    return candidate


class _Stage:
    def __init__(
        self, stage_id, engine_name, config, seed, pool, evaluator, directory, abort, check, parent
    ):
        self.stage_id, self.name, self.config = stage_id, engine_name, config
        self.seed, self.pool, self.evaluator = seed, pool, evaluator
        self.directory, self.abort, self.check = directory, abort, check
        self.path = directory / f"{stage_id}.json"
        self.binding = {
            "stage_id": stage_id,
            "engine": engine_name,
            "seed_sha256": compute_candidate_digest(seed),
            "pool_sha256": hashlib.sha256(json.dumps(pool, sort_keys=True).encode()).hexdigest(),
            "run_dir": str(config.run_dir),
            "output_dir": str(config.output_dir),
            "max_evals": config.max_evals,
            "max_token_cost": config.max_token_cost,
            "parent_stage_id": parent,
            "release_commit": COMMIT,
        }

    def _check(self):
        if self.abort:
            raise self.abort[0]
        if self.check:
            self.check()

    def _score(self, candidate):
        rows = []
        for example in self.pool:
            self._check()
            score, info = self.evaluator(candidate, example)
            if (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not math.isfinite(score)
            ):
                raise ValueError("Common evaluator must return a finite measured score")
            rows.append(
                {
                    "task_id": example.get("task_id"),
                    "score": score,
                    "job_path": info.get("job_path"),
                    "receipt_paths": info.get("receipt_paths", {}),
                }
            )
        return sum(row["score"] for row in rows) / len(rows), rows

    def run(self, task, server):
        try:
            self._check()
            if _text(task.seed_candidate) != self.seed:
                raise ValueError("Stage seed differs from frozen lineage")
            if self.path.is_symlink():
                raise ValueError("Stage receipt must not be a symlink")
            cached = self.path.exists()
            receipt: dict[str, Any]
            if cached:
                receipt = json.loads(self.path.read_text())
                if receipt.get("binding") != self.binding or receipt.get("status") not in {
                    "optimized",
                    "completed",
                }:
                    raise ValueError("Retained stage differs from frozen binding or is corrupt")
                if not receipt.get("artifacts_retained"):
                    raise ValueError(
                        "Retained stage artifacts are incomplete; restore them, not another proposal"
                    )
                candidate = _text(receipt["raw_candidate"])
                if compute_candidate_digest(candidate) != receipt["raw_candidate_sha256"]:
                    raise ValueError("Retained stage candidate digest mismatch")
                if type(receipt["upstream_evals"]) is not int or receipt["upstream_evals"] < 0:
                    raise ValueError("Retained stage evaluation count is invalid")
            else:
                engine = (
                    make_meta_harness_engine(self.config)
                    if self.name == "meta_harness"
                    else ReplaySafeGepaEngine(self.config)
                    if self.name == "gepa"
                    else _build_engine(self.config)
                )
                try:
                    raw = engine.run(task, server)
                    candidate = _text(raw.best_candidate)
                    receipt = {
                        "binding": self.binding,
                        "status": "optimized",
                        "raw_candidate": candidate,
                        "raw_candidate_sha256": compute_candidate_digest(candidate),
                        "upstream_evals": raw.total_evals,
                        "artifacts_retained": False,
                    }
                    # Preserve an already-generated result before pending common-pool
                    # evaluation can interrupt us; resumption must not re-propose it.
                    _write(self.path, receipt)
                    engine.process_result(raw, self.config.output_dir)
                    receipt["artifacts_retained"] = True
                    _write(self.path, receipt)
                finally:
                    retain = getattr(engine, "retain", None)
                    if callable(retain):
                        retain(self.directory / (self.stage_id + "-retained"))
            seed_score, seed_rows = self._score(self.seed)
            score, rows = (
                (seed_score, seed_rows) if candidate == self.seed else self._score(candidate)
            )
            selected, selected_score = (
                (candidate, score) if score > seed_score else (self.seed, seed_score)
            )
            scored = {
                "seed_score": seed_score,
                "candidate_score": score,
                "selected_score": selected_score,
                "selected_sha256": compute_candidate_digest(selected),
                "selected_candidate": selected,
                "seed_evidence": seed_rows,
                "candidate_evidence": rows,
            }
            if receipt["status"] == "completed" and any(
                receipt.get(key) != value for key, value in scored.items()
            ):
                raise ValueError(
                    "Retained stage evaluation changed; do not reuse unverified results"
                )
            receipt.update(scored, status="completed")
            _write(self.path, receipt)
            return Result(
                best_candidate=selected,
                best_score=selected_score,
                total_evals=receipt["upstream_evals"],
                metadata={
                    "stage_id": self.stage_id,
                    "engine": self.name,
                    "receipt_path": str(self.path),
                    "resumed": cached,
                },
            )
        except BaseException as exc:
            if not self.abort:
                self.abort.append(exc)
            _write(
                self.directory / f"{self.stage_id}-halt-{uuid.uuid4().hex}.json",
                {"binding": self.binding, "status": "halted", "error_type": type(exc).__name__},
            )
            raise

    def process_result(self, result, output_dir):
        """Original backend artifacts were retained before common-pool scoring."""


def run_omni(
    *,
    seed_candidate: str,
    evaluator: Callable,
    dataset: list,
    valset: list | None,
    config_factory: Callable,
    output_dir: Path,
    objective: str,
    background: str,
    before_stage: Callable[[], None] | None = None,
    continuation_engine: str = "gepa",
    proposer_model: str,
) -> Result:
    verify_release()
    if continuation_engine not in EXPLORATION_ENGINES:
        raise ValueError("Continuation must use a released Omni engine")
    availability = engine_availability(proposer_model)
    if not availability["all_available"]:
        raise EngineUnavailable(availability)
    pool = valset or dataset
    if not pool:
        raise ValueError("A nonempty common development pool is required")
    output_dir = Path(output_dir)
    if any(path.is_symlink() for path in (output_dir, *output_dir.parents)):
        raise ValueError("Omni output must not traverse symlinks")
    directory = output_dir / "stage_receipts"
    directory.mkdir(parents=True, exist_ok=True)
    abort: list[BaseException] = []
    run_dirs: set[str] = set()

    def stage(stage_id, engine_name, seed, parent=None):
        config = config_factory(stage_id, engine_name)
        if (
            config.engine != engine_name
            or config.sandbox is not True
            or config.max_concurrency != 1
        ):
            raise ValueError(
                "Omni requires the named released engine, sandboxing and serial evaluation"
            )
        run_dir = str(config.run_dir)
        if config.run_dir is None or run_dir in run_dirs:
            raise ValueError("Every stage needs a distinct durable search directory")
        run_dirs.add(run_dir)
        wrapper = _Stage(
            stage_id,
            engine_name,
            config,
            seed,
            pool,
            evaluator,
            directory,
            abort,
            before_stage,
            parent,
        )
        return replace(config, engine=wrapper, engine_config={})

    configs = [stage("explore-" + name, name, seed_candidate) for name in EXPLORATION_ENGINES]
    explored = optimize_best_of(
        seed_candidate,
        evaluator=evaluator,
        configs=configs,
        dataset=dataset,
        valset=valset,
        test_set=None,
        objective=objective,
        background=background,
        max_workers=1,
    )
    exploration: list[dict[str, Any]] = [
        {
            "stage_id": result.metadata["stage_id"],
            "engine": result.metadata["engine"],
            "candidate_sha256": compute_candidate_digest(result.best_candidate),
            "score": result.best_score,
            "upstream_evals": result.total_evals,
            "receipt_path": result.metadata["receipt_path"],
        }
        for result in explored.metadata["all_results"]
    ]
    winner = explored.metadata["stage_id"]
    continuation_id = "continue-" + continuation_engine
    config = stage(continuation_id, continuation_engine, explored.best_candidate, winner)
    continued = optimize_anything(
        explored.best_candidate,
        evaluator=evaluator,
        dataset=dataset,
        valset=valset,
        test_set=None,
        objective=objective,
        background=background,
        config=config,
    )
    return Result(
        best_candidate=_text(continued.best_candidate),
        best_score=continued.best_score,
        total_evals=sum(row["upstream_evals"] for row in exploration) + continued.total_evals,
        metadata={
            "recipe": OMNI_RECIPE_SOURCE,
            "release_commit": COMMIT,
            "exploration": exploration,
            "exploration_winner_stage": winner,
            "continuation_stage": continuation_id,
            "continuation_seed_sha256": compute_candidate_digest(explored.best_candidate),
            "continuation_receipt": str(directory / f"{continuation_id}.json"),
            "counter_scope": "Upstream stage evaluation calls; physical attempts are counted by the shared Lab budget",
        },
    )
