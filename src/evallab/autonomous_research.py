"""Deterministic process features for long-horizon autonomous research tasks.

The producer consumes structured experiment iterations rather than asking an LLM
to infer research quality from prose. Visible/hidden transfer is emitted only
when the task declares the score scales comparable.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field, model_validator

from evallab.schemas import ContractModel


def _digest(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class ResearchIterationV1(ContractModel):
    schema_version: Literal["research-iteration/v1"] = "research-iteration/v1"
    iteration_id: str = Field(min_length=1)
    hypothesis: str | None = None
    visible_score: float | None = None
    disposition: Literal["kept", "reverted", "invalid", "observed"] = "observed"
    artifact_digest: str | None = None
    elapsed_seconds: float | None = Field(default=None, ge=0)
    changed_file_count: int = Field(default=0, ge=0)
    changed_bytes: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_iteration(self) -> ResearchIterationV1:
        if self.visible_score is not None and not math.isfinite(self.visible_score):
            raise ValueError("visible experiment score must be finite")
        return self


class ResearchRunTraceV1(ContractModel):
    schema_version: Literal["research-run-trace/v1"] = "research-run-trace/v1"
    run_id: str = Field(min_length=1)
    benchmark_family: str = Field(min_length=1)
    source_digest: str
    baseline_visible_score: float | None = None
    hidden_score: float | None = None
    score_scale_compatible: bool = False
    budget_seconds: float | None = Field(default=None, gt=0)
    elapsed_seconds: float | None = Field(default=None, ge=0)
    final_artifact_digest: str | None = None
    artifact_replay_verified: bool | None = None
    iterations: tuple[ResearchIterationV1, ...]

    @model_validator(mode="after")
    def _validate_trace(self) -> ResearchRunTraceV1:
        for value in (self.baseline_visible_score, self.hidden_score):
            if value is not None and not math.isfinite(value):
                raise ValueError("research run scores must be finite")
        iteration_ids = [iteration.iteration_id for iteration in self.iterations]
        if len(iteration_ids) != len(set(iteration_ids)):
            raise ValueError("research iteration IDs must be unique")
        return self


@dataclass(frozen=True)
class AutonomousResearchFeatures:
    run_id: str
    benchmark_family: str
    iteration_count: int
    measured_iteration_count: int
    valid_experiment_rate: float | None
    unique_hypothesis_count: int
    hypothesis_turnover_rate: float | None
    kept_iteration_count: int
    reverted_iteration_count: int
    invalid_iteration_count: int
    rollback_rate: float | None
    regression_count: int
    baseline_visible_score: float | None
    best_visible_score: float | None
    final_visible_score: float | None
    visible_improvement: float | None
    final_selection_regret: float | None
    improvement_per_experiment: float | None
    first_improvement_iteration: int | None
    late_improvement_share: float | None
    visible_hidden_transfer_gap: float | None
    budget_utilization_rate: float | None
    changed_bytes_per_improvement: float | None
    artifact_replay_verified: bool | None
    source_digest: str
    feature_digest: str


def _hypothesis_key(value: str) -> str:
    normalized = " ".join(value.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def extract_autonomous_research_features(
    trace: ResearchRunTraceV1,
) -> AutonomousResearchFeatures:
    """Compute research-loop efficiency, selection, and generalization features."""
    iterations = list(trace.iterations)
    measured = [iteration for iteration in iterations if iteration.visible_score is not None]
    valid = [iteration for iteration in measured if iteration.disposition not in {"invalid"}]
    hypotheses = {
        _hypothesis_key(iteration.hypothesis)
        for iteration in iterations
        if iteration.hypothesis and iteration.hypothesis.strip()
    }
    kept = sum(iteration.disposition == "kept" for iteration in iterations)
    reverted = sum(iteration.disposition == "reverted" for iteration in iterations)
    invalid = sum(iteration.disposition == "invalid" for iteration in iterations)
    iteration_count = len(iterations)
    measured_count = len(measured)
    valid_experiment_rate = len(valid) / iteration_count if iteration_count else None
    hypothesis_turnover_rate = len(hypotheses) / iteration_count if iteration_count else None
    rollback_rate = reverted / measured_count if measured_count else None

    measured_scores = [
        iteration.visible_score for iteration in iterations if iteration.visible_score is not None
    ]
    regression_count = sum(
        current < previous
        for previous, current in zip(measured_scores, measured_scores[1:], strict=False)
    )
    best_visible = max(measured_scores) if measured_scores else None
    final_visible = measured_scores[-1] if measured_scores else None
    baseline = trace.baseline_visible_score
    visible_improvement = (
        best_visible - baseline if best_visible is not None and baseline is not None else None
    )
    final_selection_regret = (
        best_visible - final_visible
        if best_visible is not None and final_visible is not None
        else None
    )
    improvement_per_experiment = (
        visible_improvement / measured_count
        if visible_improvement is not None and measured_count > 0
        else None
    )

    first_improvement_iteration = None
    if baseline is not None:
        for index, iteration in enumerate(iterations, start=1):
            if iteration.visible_score is not None and iteration.visible_score > baseline:
                first_improvement_iteration = index
                break

    late_improvement_share = None
    if baseline is not None and measured_scores and best_visible is not None:
        total_gain = max(0.0, best_visible - baseline)
        if total_gain > 0:
            midpoint = max(1, math.ceil(iteration_count / 2))
            early_scores = [
                float(iteration.visible_score)
                for iteration in iterations[:midpoint]
                if iteration.visible_score is not None
            ]
            early_best = max([baseline, *early_scores])
            late_improvement_share = max(0.0, best_visible - early_best) / total_gain

    transfer_gap = None
    if (
        trace.score_scale_compatible
        and trace.hidden_score is not None
        and final_visible is not None
    ):
        transfer_gap = trace.hidden_score - final_visible

    budget_utilization_rate = None
    if trace.budget_seconds is not None and trace.elapsed_seconds is not None:
        budget_utilization_rate = trace.elapsed_seconds / trace.budget_seconds

    total_changed_bytes = sum(iteration.changed_bytes for iteration in iterations)
    changed_bytes_per_improvement = None
    if visible_improvement is not None and visible_improvement > 0:
        changed_bytes_per_improvement = total_changed_bytes / visible_improvement

    body = {
        "run_id": trace.run_id,
        "benchmark_family": trace.benchmark_family,
        "iteration_count": iteration_count,
        "measured_iteration_count": measured_count,
        "valid_experiment_rate": valid_experiment_rate,
        "unique_hypothesis_count": len(hypotheses),
        "hypothesis_turnover_rate": hypothesis_turnover_rate,
        "kept_iteration_count": kept,
        "reverted_iteration_count": reverted,
        "invalid_iteration_count": invalid,
        "rollback_rate": rollback_rate,
        "regression_count": regression_count,
        "baseline_visible_score": baseline,
        "best_visible_score": best_visible,
        "final_visible_score": final_visible,
        "visible_improvement": visible_improvement,
        "final_selection_regret": final_selection_regret,
        "improvement_per_experiment": improvement_per_experiment,
        "first_improvement_iteration": first_improvement_iteration,
        "late_improvement_share": late_improvement_share,
        "visible_hidden_transfer_gap": transfer_gap,
        "budget_utilization_rate": budget_utilization_rate,
        "changed_bytes_per_improvement": changed_bytes_per_improvement,
        "artifact_replay_verified": trace.artifact_replay_verified,
        "source_digest": trace.source_digest,
    }
    return AutonomousResearchFeatures(**body, feature_digest=_digest(body))


def parse_jsonl_experiment_log(text: str) -> tuple[ResearchIterationV1, ...]:
    """Parse explicit JSONL experiment records; refuse ambiguous prose rather than guessing."""
    iterations = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"experiment log line {line_number} is not explicit JSONL") from exc
        if not isinstance(value, dict):
            raise ValueError(f"experiment log line {line_number} must be an object")
        iterations.append(ResearchIterationV1.model_validate(value))
    return tuple(iterations)
