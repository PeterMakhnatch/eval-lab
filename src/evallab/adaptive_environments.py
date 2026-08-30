"""Reviewed-finding to executable adaptive environment loop.

The loop is intentionally bounded: judge output must be reviewed, generated
candidates use a closed finite-state environment, oracle/nop checks are
mandatory, and promotion requires an explicit human review record.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field, model_validator

from evallab.analyst import JudgeDisagreementV1, TrajectoryJudgeRunV1
from evallab.schemas import ContractModel
from evallab.synthetic_contracts import (
    PerturbationFamily,
    SyntheticEvalSpec,
    create_synthetic_eval_spec,
)

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _digest(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _require_digest(value: str, field_name: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be sha256:<64-hex>")
    return value


class ReviewedFailureHypothesisV1(ContractModel):
    schema_version: Literal["reviewed-failure-hypothesis/v1"] = "reviewed-failure-hypothesis/v1"
    hypothesis_digest: str
    category: str
    construct_name: str
    summary: str
    source_run_digests: tuple[str, ...]
    evidence_window_digests: tuple[str, ...]
    occurrence_count: int = Field(gt=0)
    review_actor: str
    review_digest: str
    reviewed: Literal[True] = True
    decision_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _validate_hypothesis(self) -> ReviewedFailureHypothesisV1:
        _require_digest(self.review_digest, "review_digest")
        body = self.model_dump(mode="json", exclude={"hypothesis_digest"})
        if self.hypothesis_digest != _digest(body):
            raise ValueError("reviewed hypothesis digest mismatch")
        return self


class AdaptiveEnvironmentCandidateV1(ContractModel):
    schema_version: Literal["adaptive-environment-candidate/v1"] = (
        "adaptive-environment-candidate/v1"
    )
    candidate_digest: str
    hypothesis_digest: str
    construct_name: str
    family: PerturbationFamily
    difficulty: int = Field(ge=1)
    action_sequence: tuple[str, ...]
    max_steps: int = Field(gt=0)
    hint: str
    verifier_target_state: str
    status: Literal["candidate", "hard_feasible", "rejected"] = "candidate"
    requires_review: Literal[True] = True

    @model_validator(mode="after")
    def _validate_candidate(self) -> AdaptiveEnvironmentCandidateV1:
        if not self.action_sequence or self.max_steps < len(self.action_sequence):
            raise ValueError("candidate must provide an executable action sequence")
        body = self.model_dump(
            mode="json",
            exclude={"candidate_digest", "status"},
        )
        if self.candidate_digest != _digest(body):
            raise ValueError("adaptive candidate digest mismatch")
        return self


class OracleNopCheckV1(ContractModel):
    schema_version: Literal["oracle-nop-check/v1"] = "oracle-nop-check/v1"
    candidate_digest: str
    oracle_passed: bool
    nop_failed: bool
    verifier_passed: bool
    check_digest: str

    @model_validator(mode="after")
    def _validate_check(self) -> OracleNopCheckV1:
        body = self.model_dump(mode="json", exclude={"check_digest"})
        if self.check_digest != _digest(body):
            raise ValueError("oracle/nop check digest mismatch")
        return self


class HintedUnhintedOutcomeV1(ContractModel):
    schema_version: Literal["hinted-unhinted-outcome/v1"] = "hinted-unhinted-outcome/v1"
    candidate_digest: str
    run_id: str
    hint_available: bool
    success: bool


class CapabilityBoundaryV1(ContractModel):
    schema_version: Literal["capability-boundary/v1"] = "capability-boundary/v1"
    boundary_digest: str
    candidate_digest: str
    hinted_trials: int = Field(gt=0)
    unhinted_trials: int = Field(gt=0)
    hinted_success_rate: float = Field(ge=0.0, le=1.0)
    unhinted_success_rate: float = Field(ge=0.0, le=1.0)
    hint_regret: float = Field(ge=-1.0, le=1.0)
    at_capability_boundary: bool

    @model_validator(mode="after")
    def _validate_boundary(self) -> CapabilityBoundaryV1:
        body = self.model_dump(mode="json", exclude={"boundary_digest"})
        if self.boundary_digest != _digest(body):
            raise ValueError("capability boundary digest mismatch")
        return self


class CandidatePromotionReviewV1(ContractModel):
    schema_version: Literal["candidate-promotion-review/v1"] = "candidate-promotion-review/v1"
    review_digest: str
    candidate_digest: str
    reviewer_id: str
    disposition: Literal["approved", "rejected"]
    rationale: str
    human_reviewed: Literal[True]

    @model_validator(mode="after")
    def _validate_review(self) -> CandidatePromotionReviewV1:
        body = self.model_dump(mode="json", exclude={"review_digest"})
        if self.review_digest != _digest(body):
            raise ValueError("candidate promotion review digest mismatch")
        return self


@dataclass
class AdaptiveExecutableEnvironment:
    """Closed finite-state executable environment with reset()/step() semantics."""

    candidate: AdaptiveEnvironmentCandidateV1
    position: int = 0
    steps: int = 0
    terminal: bool = False

    def reset(self) -> dict[str, Any]:
        self.position = 0
        self.steps = 0
        self.terminal = False
        return self._observation()

    def _observation(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "remaining": len(self.candidate.action_sequence) - self.position,
            "hint": self.candidate.hint,
            "terminal": self.terminal,
        }

    def step(self, action: str) -> tuple[dict[str, Any], float, bool, dict[str, Any]]:
        if self.terminal:
            raise RuntimeError("environment is terminal; call reset before step")
        self.steps += 1
        expected = self.candidate.action_sequence[self.position]
        if action == expected:
            self.position += 1
        success = self.position == len(self.candidate.action_sequence)
        exhausted = self.steps >= self.candidate.max_steps
        self.terminal = success or exhausted
        reward = 1.0 if success else 0.0
        return (
            self._observation(),
            reward,
            self.terminal,
            {
                "expected_action": expected,
                "target_state": self.candidate.verifier_target_state,
                "success": success,
            },
        )


def collect_reviewed_failure_hypotheses(
    runs: Sequence[TrajectoryJudgeRunV1],
    disagreements: Sequence[JudgeDisagreementV1],
    *,
    reviewed_run_digests: Sequence[str],
    review_actor: str,
    review_digest: str,
) -> tuple[ReviewedFailureHypothesisV1, ...]:
    """Collect only reviewed runs with resolved repeated-judge consensus."""
    _require_digest(review_digest, "review_digest")
    reviewed = set(reviewed_run_digests)
    resolved_runs: dict[str, str] = {}
    for disagreement in disagreements:
        if disagreement.unresolved or disagreement.consensus_category is None:
            continue
        for run_digest in disagreement.run_digests:
            resolved_runs[run_digest] = disagreement.consensus_category
    grouped: dict[str, list[TrajectoryJudgeRunV1]] = {}
    for run in runs:
        if (
            run.run_digest not in reviewed
            or resolved_runs.get(run.run_digest) != run.final_category
        ):
            continue
        grouped.setdefault(run.final_category, []).append(run)
    hypotheses = []
    for category, category_runs in sorted(grouped.items()):
        final_stages = [run.stages[-1] for run in category_runs]
        evidence = sorted(
            {
                citation.window_digest
                for stage in final_stages
                for citation in stage.supporting_citations
            }
        )
        summaries = Counter(stage.summary for stage in final_stages)
        summary = summaries.most_common(1)[0][0]
        construct_name = {
            "memory_failure": "Context & Actionable Memory",
            "tool_composition_failure": "Tool Selection, Composition & Value Propagation",
            "recovery_failure": "Error Detection & Autonomous Recovery",
            "harness_capture_failure": "Trajectory Capture Integrity",
        }.get(category, "Agentic Trajectory Analysis")
        body = {
            "schema_version": "reviewed-failure-hypothesis/v1",
            "category": category,
            "construct_name": construct_name,
            "summary": summary,
            "source_run_digests": tuple(sorted(run.run_digest for run in category_runs)),
            "evidence_window_digests": tuple(evidence),
            "occurrence_count": len(category_runs),
            "review_actor": review_actor,
            "review_digest": review_digest,
            "reviewed": True,
            "decision_eligible": False,
        }
        hypotheses.append(
            ReviewedFailureHypothesisV1.model_validate({**body, "hypothesis_digest": _digest(body)})
        )
    return tuple(hypotheses)


def _family_for_category(category: str) -> PerturbationFamily:
    if category == "memory_failure":
        return PerturbationFamily.CONTEXT_PRESSURE
    if category == "tool_composition_failure":
        return PerturbationFamily.FUNCTION_DAG
    if category == "recovery_failure":
        return PerturbationFamily.TOOL_UNRELIABILITY
    return PerturbationFamily.EPISTEMIC_RESTRAINT


def generate_adaptive_candidates(
    hypotheses: Sequence[ReviewedFailureHypothesisV1],
    *,
    difficulty: int = 2,
) -> tuple[AdaptiveEnvironmentCandidateV1, ...]:
    """Generate executable finite-state candidates from reviewed hypotheses."""
    if difficulty <= 0:
        raise ValueError("adaptive difficulty must be positive")
    candidates = []
    for hypothesis in hypotheses:
        if hypothesis.reviewed is not True:
            raise ValueError("adaptive generation requires reviewed hypotheses")
        family = _family_for_category(hypothesis.category)
        sequences = {
            PerturbationFamily.CONTEXT_PRESSURE: (
                "retrieve_latest_update",
                "discard_obsolete_value",
                "apply_current_value",
                "verify_state",
            ),
            PerturbationFamily.FUNCTION_DAG: (
                "inspect_dependencies",
                "execute_prerequisite",
                "propagate_value",
                "verify_state",
            ),
            PerturbationFamily.TOOL_UNRELIABILITY: (
                "detect_fault",
                "change_strategy",
                "retry_operation",
                "verify_state",
            ),
            PerturbationFamily.EPISTEMIC_RESTRAINT: (
                "inspect_evidence",
                "request_clarification",
                "verify_state",
            ),
        }
        sequence = sequences[family]
        body = {
            "schema_version": "adaptive-environment-candidate/v1",
            "hypothesis_digest": hypothesis.hypothesis_digest,
            "construct_name": hypothesis.construct_name,
            "family": family,
            "difficulty": difficulty,
            "action_sequence": sequence,
            "max_steps": len(sequence) + difficulty,
            "hint": f"Begin with: {sequence[0]}",
            "verifier_target_state": "all_required_actions_completed_in_order",
            "status": "candidate",
            "requires_review": True,
        }
        candidates.append(
            AdaptiveEnvironmentCandidateV1.model_validate(
                {
                    **body,
                    "candidate_digest": _digest(
                        {key: value for key, value in body.items() if key != "status"}
                    ),
                }
            )
        )
    return tuple(candidates)


def run_oracle_nop_checks(candidate: AdaptiveEnvironmentCandidateV1) -> OracleNopCheckV1:
    """Execute the exact oracle sequence and a no-op agent against the candidate."""
    environment = AdaptiveExecutableEnvironment(candidate)
    environment.reset()
    oracle_reward = 0.0
    oracle_done = False
    oracle_info: Mapping[str, Any] = {}
    for action in candidate.action_sequence:
        _, oracle_reward, oracle_done, oracle_info = environment.step(action)
    oracle_passed = oracle_done and oracle_reward == 1.0 and bool(oracle_info.get("success"))

    environment.reset()
    nop_reward = 0.0
    nop_done = False
    nop_info: Mapping[str, Any] = {}
    for _ in range(candidate.max_steps):
        _, nop_reward, nop_done, nop_info = environment.step("noop")
        if nop_done:
            break
    nop_failed = nop_done and nop_reward == 0.0 and not bool(nop_info.get("success"))
    body = {
        "schema_version": "oracle-nop-check/v1",
        "candidate_digest": candidate.candidate_digest,
        "oracle_passed": oracle_passed,
        "nop_failed": nop_failed,
        "verifier_passed": oracle_passed and nop_failed,
    }
    return OracleNopCheckV1.model_validate({**body, "check_digest": _digest(body)})


def identify_capability_boundary(
    candidate_digest: str,
    outcomes: Sequence[HintedUnhintedOutcomeV1],
) -> CapabilityBoundaryV1:
    """Compute SPADE-style hint regret for one frozen candidate."""
    selected = [outcome for outcome in outcomes if outcome.candidate_digest == candidate_digest]
    hinted = [outcome for outcome in selected if outcome.hint_available]
    unhinted = [outcome for outcome in selected if not outcome.hint_available]
    if not hinted or not unhinted:
        raise ValueError("capability boundary requires hinted and unhinted trials")
    hinted_rate = sum(outcome.success for outcome in hinted) / len(hinted)
    unhinted_rate = sum(outcome.success for outcome in unhinted) / len(unhinted)
    regret = hinted_rate - unhinted_rate
    body = {
        "schema_version": "capability-boundary/v1",
        "candidate_digest": candidate_digest,
        "hinted_trials": len(hinted),
        "unhinted_trials": len(unhinted),
        "hinted_success_rate": hinted_rate,
        "unhinted_success_rate": unhinted_rate,
        "hint_regret": regret,
        "at_capability_boundary": hinted_rate > unhinted_rate and unhinted_rate < 1.0,
    }
    return CapabilityBoundaryV1.model_validate({**body, "boundary_digest": _digest(body)})


def retain_hard_feasible_candidates(
    candidates: Sequence[AdaptiveEnvironmentCandidateV1],
    checks: Sequence[OracleNopCheckV1],
    boundaries: Sequence[CapabilityBoundaryV1],
) -> tuple[AdaptiveEnvironmentCandidateV1, ...]:
    """Retain candidates that are executable, non-trivial, and at the capability boundary."""
    check_by_candidate = {check.candidate_digest: check for check in checks}
    boundary_by_candidate = {boundary.candidate_digest: boundary for boundary in boundaries}
    retained = []
    for candidate in candidates:
        check = check_by_candidate.get(candidate.candidate_digest)
        boundary = boundary_by_candidate.get(candidate.candidate_digest)
        if (
            check is None
            or boundary is None
            or check.verifier_passed is not True
            or boundary.at_capability_boundary is not True
        ):
            continue
        body = candidate.model_dump(mode="json", exclude={"candidate_digest"})
        body["status"] = "hard_feasible"
        retained.append(
            AdaptiveEnvironmentCandidateV1.model_validate(
                {**body, "candidate_digest": candidate.candidate_digest}
            )
        )
    return tuple(retained)


def create_candidate_promotion_review(
    candidate: AdaptiveEnvironmentCandidateV1,
    *,
    reviewer_id: str,
    disposition: Literal["approved", "rejected"],
    rationale: str,
    human_reviewed: Literal[True],
) -> CandidatePromotionReviewV1:
    """Create the required human review record for candidate promotion."""
    body = {
        "schema_version": "candidate-promotion-review/v1",
        "candidate_digest": candidate.candidate_digest,
        "reviewer_id": reviewer_id,
        "disposition": disposition,
        "rationale": rationale,
        "human_reviewed": human_reviewed,
    }
    return CandidatePromotionReviewV1.model_validate({**body, "review_digest": _digest(body)})


def promote_adaptive_candidate(
    candidate: AdaptiveEnvironmentCandidateV1,
    hypothesis: ReviewedFailureHypothesisV1,
    review: CandidatePromotionReviewV1,
    *,
    seed: int,
) -> SyntheticEvalSpec:
    """Promote an approved hard-feasible candidate into the existing synthetic contract."""
    if candidate.status != "hard_feasible":
        raise ValueError("only hard-feasible candidates may be promoted")
    if candidate.hypothesis_digest != hypothesis.hypothesis_digest:
        raise ValueError("adaptive candidate does not bind the reviewed hypothesis")
    if review.candidate_digest != candidate.candidate_digest:
        raise ValueError("promotion review does not bind the candidate")
    if review.human_reviewed is not True or review.disposition != "approved":
        raise ValueError("candidate promotion requires explicit human approval")
    return create_synthetic_eval_spec(
        construct_name=candidate.construct_name,
        family=candidate.family,
        perturbation_type="adaptive_executable_environment",
        seed=seed,
        source_task_ref=f"adaptive/{hypothesis.category}",
        source_failure_evidence=list(hypothesis.evidence_window_digests),
        base_task_digest=hypothesis.hypothesis_digest,
        generated_task_digest=candidate.candidate_digest,
        expected_behavior=" -> ".join(candidate.action_sequence),
        capability_opportunity=hypothesis.summary,
        required_evidence=["benchmark-events.jsonl", "final-state.json"],
        license_provenance="Locally generated from reviewed Eval Lab evidence",
        partition="dev",
        family_id=f"adaptive-{hypothesis.category}",
        lineage_id=review.review_digest,
        parameters={
            "max_steps": candidate.max_steps,
            "hint": candidate.hint,
            "verifier_target_state": candidate.verifier_target_state,
        },
    )
