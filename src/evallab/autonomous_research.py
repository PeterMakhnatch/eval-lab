"""Deterministic process features for long-horizon autonomous research tasks.

The producer consumes structured experiment iterations rather than asking an LLM
to infer research quality from prose. Visible/hidden transfer is emitted only
when the task declares the score scales comparable. Grounded in research-loop
methodology across RSI-Exam, RE-Bench, PaperBench, MLE-bench, CORE-Bench, and AgentBoard.
Supports both higher-is-better (reward, accuracy) and lower-is-better (loss, error)
score directions.
"""

from __future__ import annotations

import dataclasses
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
    milestone_progress: float | None = Field(default=None, ge=0)
    rubric_subtasks_passed: int = Field(default=0, ge=0)
    dependency_repair_attempted: bool = False
    dependency_repair_succeeded: bool = False
    leakage_detected: bool = False
    tokens_used: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    is_reproducible: bool | None = None
    execution_success: bool = True

    @model_validator(mode="after")
    def _validate_iteration(self) -> ResearchIterationV1:
        if self.visible_score is not None and not math.isfinite(self.visible_score):
            raise ValueError("visible experiment score must be finite")
        if self.elapsed_seconds is not None and not math.isfinite(self.elapsed_seconds):
            raise ValueError("elapsed seconds must be finite")
        if self.cost_usd is not None and not math.isfinite(self.cost_usd):
            raise ValueError("cost_usd must be finite")
        return self


class ResearchRunTraceV1(ContractModel):
    schema_version: Literal["research-run-trace/v1"] = "research-run-trace/v1"
    run_id: str = Field(min_length=1)
    benchmark_family: str = Field(min_length=1)
    source_digest: str
    baseline_visible_score: float | None = None
    score_direction: Literal["higher", "lower"] = "higher"
    hidden_score: float | None = None
    score_scale_compatible: bool = False
    budget_seconds: float | None = Field(default=None, gt=0)
    elapsed_seconds: float | None = Field(default=None, ge=0)
    total_cost_usd: float | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    required_milestones: int = Field(default=0, ge=0)
    completed_milestones: int = Field(default=0, ge=0)
    total_rubric_subtasks: int = Field(default=0, ge=0)
    completed_rubric_subtasks: int = Field(default=0, ge=0)
    environment_setup_seconds: float | None = Field(default=None, ge=0)
    dependency_repair_attempts: int = Field(default=0, ge=0)
    dependency_repair_successes: int = Field(default=0, ge=0)
    leakage_flag: bool = False
    leakage_warning_count: int = Field(default=0, ge=0)
    train_val_split_intact: bool = True
    final_artifact_digest: str | None = None
    artifact_replay_verified: bool | None = None
    iterations: tuple[ResearchIterationV1, ...]

    @model_validator(mode="after")
    def _validate_trace(self) -> ResearchRunTraceV1:
        for value in (
            self.baseline_visible_score,
            self.hidden_score,
            self.budget_seconds,
            self.elapsed_seconds,
            self.total_cost_usd,
            self.environment_setup_seconds,
        ):
            if value is not None and not math.isfinite(value):
                raise ValueError("research run numeric fields must be finite")
        iteration_ids = [iteration.iteration_id for iteration in self.iterations]
        if len(iteration_ids) != len(set(iteration_ids)):
            raise ValueError("research iteration IDs must be unique")
        return self


@dataclass(frozen=True)
class AutonomousResearchFeatures:
    # Identity & metadata
    run_id: str
    benchmark_family: str
    score_direction: str

    # 1. Experiment Throughput & Validity (RSI-Exam, MLE-bench, RE-Bench)
    iteration_count: int
    measured_iteration_count: int
    valid_experiment_count: int
    invalid_iteration_count: int
    valid_experiment_rate: float | None
    experiment_throughput_per_hour: float | None

    # 2. Hypothesis Turnover & Exploration (RSI-Exam, MLE-bench)
    unique_hypothesis_count: int
    repeated_hypothesis_count: int
    hypothesis_turnover_rate: float | None

    # 3. Regressions & Rollback Control (RSI-Exam, RE-Bench)
    kept_iteration_count: int
    reverted_iteration_count: int
    regression_count: int
    max_consecutive_regressions: int
    rollback_rate: float | None
    regression_rate: float | None

    # 4. Score-Time Curves & Dynamics (RE-Bench, RSI-Exam, AgentBoard)
    baseline_visible_score: float | None
    best_visible_score: float | None
    final_visible_score: float | None
    first_improvement_iteration: int | None
    best_improvement_iteration: int | None
    time_to_first_improvement_seconds: float | None
    time_to_best_score_seconds: float | None
    stalled_iteration_count: int
    plateau_streak_max: int
    visible_improvement: float | None
    improvement_per_experiment: float | None
    late_improvement_share: float | None
    stalled_iteration_rate: float | None

    # 5. Milestone & Rubric Progression (PaperBench, AgentBoard, CORE-Bench)
    required_milestones: int
    completed_milestones: int
    milestone_completion_rate: float | None
    total_rubric_subtasks: int
    completed_rubric_subtasks: int
    rubric_completion_rate: float | None

    # 6. Final-Selection Regret (RSI-Exam, MLE-bench)
    optimal_selection_flag: bool | None
    final_selection_regret: float | None

    # 7. Hidden-Transfer Gap & Generalization (RSI-Exam, MLE-bench)
    score_scale_compatible: bool
    hidden_score: float | None
    visible_hidden_transfer_gap: float | None

    # 8. Artifact Replay & Reproducibility (RSI-Exam, CORE-Bench, PaperBench)
    final_artifact_digest: str | None
    artifact_replay_verified: bool | None
    reproducible_iteration_count: int
    reproducibility_rate: float | None

    # 9. Environment Reconstruction & Dependency Repair (CORE-Bench)
    environment_setup_seconds: float | None
    dependency_repair_attempts: int
    dependency_repair_successes: int
    runtime_environment_repaired: bool
    dependency_repair_success_rate: float | None

    # 10. Budget & Cost Efficiency (RE-Bench, RSI-Exam, MLE-bench)
    budget_seconds: float | None
    elapsed_seconds: float | None
    total_cost_usd: float | None
    total_tokens: int | None
    total_changed_bytes: int
    budget_utilization_rate: float | None
    cost_per_improvement: float | None
    tokens_per_experiment: float | None
    changed_bytes_per_improvement: float | None

    # 11. Data Integrity & Contamination Prevention (MLE-bench, RSI-Exam)
    leakage_detected_flag: bool
    leakage_warning_count: int
    train_val_split_intact: bool

    # Lineage digests
    source_digest: str
    feature_digest: str

    def to_dict(self) -> dict[str, Any]:
        """Convert features dataclass to dictionary."""
        return dataclasses.asdict(self)


def _hypothesis_key(value: str) -> str:
    normalized = " ".join(value.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def extract_autonomous_research_features(
    trace: ResearchRunTraceV1,
) -> AutonomousResearchFeatures:
    """Compute research-loop efficiency, selection, and generalization features."""
    iterations = list(trace.iterations)
    iteration_count = len(iterations)
    is_lower = trace.score_direction == "lower"

    # 1. Experiment Throughput & Validity
    measured = [iteration for iteration in iterations if iteration.visible_score is not None]
    measured_count = len(measured)
    valid = [
        iteration
        for iteration in measured
        if iteration.disposition != "invalid" and iteration.execution_success
    ]
    valid_count = len(valid)
    invalid_count = sum(
        1
        for iteration in iterations
        if iteration.disposition == "invalid" or not iteration.execution_success
    )
    valid_experiment_rate = valid_count / iteration_count if iteration_count > 0 else None

    elapsed_time = trace.elapsed_seconds
    if elapsed_time is None and any(it.elapsed_seconds is not None for it in iterations):
        elapsed_time = sum(
            it.elapsed_seconds for it in iterations if it.elapsed_seconds is not None
        )

    experiment_throughput_per_hour = (
        (iteration_count / (elapsed_time / 3600.0))
        if elapsed_time is not None and elapsed_time > 0 and iteration_count > 0
        else None
    )

    # 2. Hypothesis Turnover & Exploration
    hypotheses = {
        _hypothesis_key(iteration.hypothesis)
        for iteration in iterations
        if iteration.hypothesis and iteration.hypothesis.strip()
    }
    unique_hypothesis_count = len(hypotheses)
    has_hypotheses = any(it.hypothesis and it.hypothesis.strip() for it in iterations)
    repeated_hypothesis_count = (
        max(0, iteration_count - unique_hypothesis_count) if has_hypotheses else 0
    )
    hypothesis_turnover_rate = (
        unique_hypothesis_count / iteration_count if iteration_count > 0 else None
    )

    # 3. Regressions & Rollback Control
    kept_count = sum(iteration.disposition == "kept" for iteration in iterations)
    reverted_count = sum(iteration.disposition == "reverted" for iteration in iterations)
    rollback_rate = reverted_count / measured_count if measured_count > 0 else None

    measured_scores = [
        iteration.visible_score for iteration in iterations if iteration.visible_score is not None
    ]

    # Direction-aware regression count and max consecutive streak
    regression_count = sum(
        (current > previous) if is_lower else (current < previous)
        for previous, current in zip(measured_scores, measured_scores[1:], strict=False)
    )
    regression_rate = regression_count / (measured_count - 1) if measured_count > 1 else None

    max_consecutive_regressions = 0
    current_reg_streak = 0
    for previous, current in zip(measured_scores, measured_scores[1:], strict=False):
        is_worse = (current > previous) if is_lower else (current < previous)
        if is_worse:
            current_reg_streak += 1
            if current_reg_streak > max_consecutive_regressions:
                max_consecutive_regressions = current_reg_streak
        else:
            current_reg_streak = 0

    # 4. Score-Time Curves & Dynamics
    baseline = trace.baseline_visible_score
    if measured_scores:
        best_visible = min(measured_scores) if is_lower else max(measured_scores)
        final_visible = measured_scores[-1]
    else:
        best_visible = None
        final_visible = None

    if best_visible is not None and baseline is not None:
        visible_improvement = (baseline - best_visible) if is_lower else (best_visible - baseline)
    else:
        visible_improvement = None

    improvement_per_experiment = (
        visible_improvement / measured_count
        if visible_improvement is not None and measured_count > 0
        else None
    )

    # First improvement iteration & timing (strictly better than baseline)
    first_improvement_iteration: int | None = None
    time_to_first_improvement_seconds: float | None = None
    if baseline is not None:
        cum_time = 0.0
        for index, iteration in enumerate(iterations, start=1):
            if iteration.elapsed_seconds is not None:
                cum_time += iteration.elapsed_seconds
            if iteration.visible_score is not None:
                is_improved = (
                    (iteration.visible_score < baseline)
                    if is_lower
                    else (iteration.visible_score > baseline)
                )
                if is_improved:
                    first_improvement_iteration = index
                    if iteration.elapsed_seconds is not None:
                        time_to_first_improvement_seconds = cum_time
                    break

    # Best improvement iteration & timing
    best_improvement_iteration: int | None = None
    time_to_best_score_seconds: float | None = None
    if best_visible is not None:
        cum_time = 0.0
        for index, iteration in enumerate(iterations, start=1):
            if iteration.elapsed_seconds is not None:
                cum_time += iteration.elapsed_seconds
            if iteration.visible_score is not None and iteration.visible_score == best_visible:
                best_improvement_iteration = index
                if iteration.elapsed_seconds is not None:
                    time_to_best_score_seconds = cum_time
                break

    # Stalled iteration count and rate
    if best_improvement_iteration is not None:
        stalled_iteration_count = max(0, iteration_count - best_improvement_iteration)
    else:
        stalled_iteration_count = iteration_count
    stalled_iteration_rate = (
        stalled_iteration_count / iteration_count if iteration_count > 0 else None
    )

    # Plateau streak max (longest streak of measured iterations without setting a new high score)
    plateau_streak_max = 0
    current_plateau = 0
    running_best = float("inf") if is_lower else -float("inf")
    for score in measured_scores:
        is_strictly_better = (score < running_best) if is_lower else (score > running_best)
        if is_strictly_better:
            running_best = score
            current_plateau = 0
        else:
            current_plateau += 1
            if current_plateau > plateau_streak_max:
                plateau_streak_max = current_plateau

    # Late improvement share
    late_improvement_share = None
    if baseline is not None and measured_scores and best_visible is not None:
        total_gain = (
            max(0.0, baseline - best_visible) if is_lower else max(0.0, best_visible - baseline)
        )
        if total_gain > 0:
            midpoint = max(1, math.ceil(iteration_count / 2))
            early_scores = [
                float(iteration.visible_score)
                for iteration in iterations[:midpoint]
                if iteration.visible_score is not None
            ]
            if is_lower:
                early_best = min([baseline, *early_scores])
                late_improvement_share = max(0.0, early_best - best_visible) / total_gain
            else:
                early_best = max([baseline, *early_scores])
                late_improvement_share = max(0.0, best_visible - early_best) / total_gain

    # 5. Milestone & Rubric Progression
    required_milestones = trace.required_milestones
    completed_milestones = trace.completed_milestones
    milestone_completion_rate = (
        completed_milestones / required_milestones if required_milestones > 0 else None
    )

    total_rubric_subtasks = trace.total_rubric_subtasks
    completed_rubric_subtasks = trace.completed_rubric_subtasks
    if completed_rubric_subtasks == 0 and any(it.rubric_subtasks_passed > 0 for it in iterations):
        completed_rubric_subtasks = max(it.rubric_subtasks_passed for it in iterations)
    rubric_completion_rate = (
        completed_rubric_subtasks / total_rubric_subtasks if total_rubric_subtasks > 0 else None
    )

    # 6. Final-Selection Regret (non-negative loss from choosing final instead of best)
    optimal_selection_flag = (
        (final_visible == best_visible)
        if final_visible is not None and best_visible is not None
        else None
    )
    if best_visible is not None and final_visible is not None:
        final_selection_regret = (
            (final_visible - best_visible) if is_lower else (best_visible - final_visible)
        )
    else:
        final_selection_regret = None

    # 7. Hidden-Transfer Gap & Generalization (raw delta: hidden_score - final_visible)
    transfer_gap = None
    if (
        trace.score_scale_compatible
        and trace.hidden_score is not None
        and final_visible is not None
    ):
        transfer_gap = trace.hidden_score - final_visible

    # 8. Artifact Replay & Reproducibility
    reproducible_iteration_count = sum(
        1 for iteration in iterations if iteration.is_reproducible is True
    )
    reproducibility_rate = (
        reproducible_iteration_count / iteration_count if iteration_count > 0 else None
    )
    final_artifact_digest = trace.final_artifact_digest
    if final_artifact_digest is None and iterations:
        last_kept = [it for it in iterations if it.disposition == "kept" and it.artifact_digest]
        if last_kept:
            final_artifact_digest = last_kept[-1].artifact_digest

    # 9. Environment Reconstruction & Dependency Repair
    dep_attempts = trace.dependency_repair_attempts + sum(
        1 for it in iterations if it.dependency_repair_attempted
    )
    dep_successes = trace.dependency_repair_successes + sum(
        1 for it in iterations if it.dependency_repair_succeeded
    )
    runtime_env_repaired = dep_successes >= dep_attempts if dep_attempts > 0 else True
    dependency_repair_success_rate = dep_successes / dep_attempts if dep_attempts > 0 else None

    # 10. Budget & Cost Efficiency
    budget_utilization_rate = None
    if trace.budget_seconds is not None and trace.budget_seconds > 0 and elapsed_time is not None:
        budget_utilization_rate = elapsed_time / trace.budget_seconds

    total_cost = trace.total_cost_usd
    if total_cost is None and any(it.cost_usd is not None for it in iterations):
        total_cost = sum(it.cost_usd for it in iterations if it.cost_usd is not None)

    cost_per_improvement = None
    if total_cost is not None and visible_improvement is not None and visible_improvement > 0:
        cost_per_improvement = total_cost / visible_improvement

    total_tokens = trace.total_tokens
    if total_tokens is None and any(it.tokens_used is not None for it in iterations):
        total_tokens = sum(it.tokens_used for it in iterations if it.tokens_used is not None)

    tokens_per_experiment = (
        total_tokens / iteration_count if total_tokens is not None and iteration_count > 0 else None
    )

    total_changed_bytes = sum(iteration.changed_bytes for iteration in iterations)
    changed_bytes_per_improvement = None
    if visible_improvement is not None and visible_improvement > 0:
        changed_bytes_per_improvement = total_changed_bytes / visible_improvement

    # 11. Data Integrity & Contamination Prevention
    leakage_detected = trace.leakage_flag or any(it.leakage_detected for it in iterations)
    leakage_warning_count = trace.leakage_warning_count
    train_val_split_intact = trace.train_val_split_intact and not leakage_detected

    body = {
        "run_id": trace.run_id,
        "benchmark_family": trace.benchmark_family,
        "score_direction": trace.score_direction,
        # 1. Experiment Throughput & Validity
        "iteration_count": iteration_count,
        "measured_iteration_count": measured_count,
        "valid_experiment_count": valid_count,
        "invalid_iteration_count": invalid_count,
        "valid_experiment_rate": valid_experiment_rate,
        "experiment_throughput_per_hour": experiment_throughput_per_hour,
        # 2. Hypothesis Turnover & Exploration
        "unique_hypothesis_count": unique_hypothesis_count,
        "repeated_hypothesis_count": repeated_hypothesis_count,
        "hypothesis_turnover_rate": hypothesis_turnover_rate,
        # 3. Regressions & Rollback Control
        "kept_iteration_count": kept_count,
        "reverted_iteration_count": reverted_count,
        "regression_count": regression_count,
        "max_consecutive_regressions": max_consecutive_regressions,
        "rollback_rate": rollback_rate,
        "regression_rate": regression_rate,
        # 4. Score-Time Curves & Dynamics
        "baseline_visible_score": baseline,
        "best_visible_score": best_visible,
        "final_visible_score": final_visible,
        "first_improvement_iteration": first_improvement_iteration,
        "best_improvement_iteration": best_improvement_iteration,
        "time_to_first_improvement_seconds": time_to_first_improvement_seconds,
        "time_to_best_score_seconds": time_to_best_score_seconds,
        "stalled_iteration_count": stalled_iteration_count,
        "plateau_streak_max": plateau_streak_max,
        "visible_improvement": visible_improvement,
        "improvement_per_experiment": improvement_per_experiment,
        "late_improvement_share": late_improvement_share,
        "stalled_iteration_rate": stalled_iteration_rate,
        # 5. Milestone & Rubric Progression
        "required_milestones": required_milestones,
        "completed_milestones": completed_milestones,
        "milestone_completion_rate": milestone_completion_rate,
        "total_rubric_subtasks": total_rubric_subtasks,
        "completed_rubric_subtasks": completed_rubric_subtasks,
        "rubric_completion_rate": rubric_completion_rate,
        # 6. Final-Selection Regret
        "optimal_selection_flag": optimal_selection_flag,
        "final_selection_regret": final_selection_regret,
        # 7. Hidden-Transfer Gap & Generalization
        "score_scale_compatible": trace.score_scale_compatible,
        "hidden_score": trace.hidden_score,
        "visible_hidden_transfer_gap": transfer_gap,
        # 8. Artifact Replay & Reproducibility
        "final_artifact_digest": final_artifact_digest,
        "artifact_replay_verified": trace.artifact_replay_verified,
        "reproducible_iteration_count": reproducible_iteration_count,
        "reproducibility_rate": reproducibility_rate,
        # 9. Environment Reconstruction & Dependency Repair
        "environment_setup_seconds": trace.environment_setup_seconds,
        "dependency_repair_attempts": dep_attempts,
        "dependency_repair_successes": dep_successes,
        "runtime_environment_repaired": runtime_env_repaired,
        "dependency_repair_success_rate": dependency_repair_success_rate,
        # 10. Budget & Cost Efficiency
        "budget_seconds": trace.budget_seconds,
        "elapsed_seconds": elapsed_time,
        "total_cost_usd": total_cost,
        "total_tokens": total_tokens,
        "total_changed_bytes": total_changed_bytes,
        "budget_utilization_rate": budget_utilization_rate,
        "cost_per_improvement": cost_per_improvement,
        "tokens_per_experiment": tokens_per_experiment,
        "changed_bytes_per_improvement": changed_bytes_per_improvement,
        # 11. Data Integrity & Contamination Prevention
        "leakage_detected_flag": leakage_detected,
        "leakage_warning_count": leakage_warning_count,
        "train_val_split_intact": train_val_split_intact,
        # Lineage
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
