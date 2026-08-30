from __future__ import annotations

import pytest

from evallab.adaptive_environments import (
    HintedUnhintedOutcomeV1,
    collect_reviewed_failure_hypotheses,
    create_candidate_promotion_review,
    generate_adaptive_candidates,
    identify_capability_boundary,
    promote_adaptive_candidate,
    retain_hard_feasible_candidates,
    run_oracle_nop_checks,
)
from evallab.analyst import AnalystResult, run_trajectory_judge
from evallab.lance import build_trajectory_windows
from evallab.schemas import ConfidenceClaim, EvidenceCitation


def _windows():
    return build_trajectory_windows(
        [
            {"step_id": 1, "message": "required memory update was omitted"},
            {"step_id": 2, "message": "clean replay preserves the latest value"},
        ],
        snapshot_digest="sha256:" + "1" * 64,
        source_digest="sha256:" + "2" * 64,
        redaction_policy_digest="sha256:" + "3" * 64,
        source_is_redacted=True,
        job_id="adaptive-job",
        trial_id="adaptive-trial",
        window_steps=1,
        stride_steps=1,
    )


class ReviewedMemoryAnalyzer:
    model = "reviewed-memory-judge"

    def analyze(self, prompt: str, context: str) -> AnalystResult:
        windows = _windows()
        support = EvidenceCitation(path=windows[0].window_digest, step=1)
        counter = EvidenceCitation(path=windows[1].window_digest, step=2)
        final = "Stage: FINAL" in prompt
        return AnalystResult(
            category="memory_failure",
            summary="The agent omitted the latest update before binding stale state.",
            evidence=[support],
            contradicting_evidence=[counter] if final else [],
            alternative_explanations=["The trace may be capture-incomplete."] if final else [],
            confidence=ConfidenceClaim(
                level="high",
                n=1,
                provenance_digest="sha256:" + "4" * 64,
            ),
        )


def test_reviewed_findings_generate_check_compare_retain_and_promote() -> None:
    runs, disagreement = run_trajectory_judge(
        ReviewedMemoryAnalyzer(),
        _windows(),
        rubric="Diagnose the agentic failure with counterevidence.",
        repeats=3,
    )
    review_digest = "sha256:" + "5" * 64
    hypotheses = collect_reviewed_failure_hypotheses(
        runs,
        [disagreement],
        reviewed_run_digests=[run.run_digest for run in runs],
        review_actor="human-reviewer",
        review_digest=review_digest,
    )
    assert len(hypotheses) == 1
    assert hypotheses[0].reviewed is True
    assert hypotheses[0].occurrence_count == 3

    candidates = generate_adaptive_candidates(hypotheses, difficulty=2)
    assert len(candidates) == 1
    candidate = candidates[0]
    check = run_oracle_nop_checks(candidate)
    assert check.oracle_passed is True
    assert check.nop_failed is True
    assert check.verifier_passed is True

    outcomes = [
        HintedUnhintedOutcomeV1(
            candidate_digest=candidate.candidate_digest,
            run_id="hinted-1",
            hint_available=True,
            success=True,
        ),
        HintedUnhintedOutcomeV1(
            candidate_digest=candidate.candidate_digest,
            run_id="hinted-2",
            hint_available=True,
            success=True,
        ),
        HintedUnhintedOutcomeV1(
            candidate_digest=candidate.candidate_digest,
            run_id="unhinted-1",
            hint_available=False,
            success=False,
        ),
        HintedUnhintedOutcomeV1(
            candidate_digest=candidate.candidate_digest,
            run_id="unhinted-2",
            hint_available=False,
            success=True,
        ),
    ]
    boundary = identify_capability_boundary(candidate.candidate_digest, outcomes)
    assert boundary.hinted_success_rate == 1.0
    assert boundary.unhinted_success_rate == 0.5
    assert boundary.hint_regret == 0.5
    assert boundary.at_capability_boundary is True

    retained = retain_hard_feasible_candidates(candidates, [check], [boundary])
    assert len(retained) == 1
    assert retained[0].status == "hard_feasible"
    assert retained[0].candidate_digest == candidate.candidate_digest
    assert retained[0].requires_review is True

    review = create_candidate_promotion_review(
        retained[0],
        reviewer_id="human-reviewer",
        disposition="approved",
        rationale="Executable, non-trivial, and grounded in reviewed evidence.",
        human_reviewed=True,
    )
    spec = promote_adaptive_candidate(
        retained[0],
        hypotheses[0],
        review,
        seed=42,
    )
    assert spec.verify_spec_id()
    assert spec.parameters["verifier_target_state"] == ("all_required_actions_completed_in_order")


def test_candidate_promotion_refuses_rejected_review() -> None:
    runs, disagreement = run_trajectory_judge(
        ReviewedMemoryAnalyzer(),
        _windows(),
        rubric="Diagnose the agentic failure with counterevidence.",
        repeats=1,
    )
    hypotheses = collect_reviewed_failure_hypotheses(
        runs,
        [disagreement],
        reviewed_run_digests=[runs[0].run_digest],
        review_actor="human-reviewer",
        review_digest="sha256:" + "6" * 64,
    )
    candidate = generate_adaptive_candidates(hypotheses)[0]
    check = run_oracle_nop_checks(candidate)
    outcomes = [
        HintedUnhintedOutcomeV1(
            candidate_digest=candidate.candidate_digest,
            run_id="hinted",
            hint_available=True,
            success=True,
        ),
        HintedUnhintedOutcomeV1(
            candidate_digest=candidate.candidate_digest,
            run_id="unhinted",
            hint_available=False,
            success=False,
        ),
    ]
    boundary = identify_capability_boundary(candidate.candidate_digest, outcomes)
    retained = retain_hard_feasible_candidates([candidate], [check], [boundary])[0]
    review = create_candidate_promotion_review(
        retained,
        reviewer_id="human-reviewer",
        disposition="rejected",
        rationale="Needs another independent run.",
        human_reviewed=True,
    )
    with pytest.raises(ValueError, match="human approval"):
        promote_adaptive_candidate(retained, hypotheses[0], review, seed=1)
