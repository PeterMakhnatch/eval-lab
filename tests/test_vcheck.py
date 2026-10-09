"""VerifierCheck core: contracts, controls, hypothesis lifecycle, waves.

Offline: every grade goes through an injected fake ``grade_fn``. No Docker,
no model, no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evallab.vcheck import (
    MODEL_DEFAULTS,
    RED_BUDGETS,
    CampaignManifest,
    ControlReport,
    GradeResult,
    Hypothesis,
    RequirementItem,
    RequirementMap,
    Submission,
    WaveResult,
    blank_submission,
    manifest_digest,
    oracle_submission,
    raise_hypothesis,
    run_controls,
    run_wave,
    synthesize_broadcast,
    transition_hypothesis,
)


def _grade(reward: float | None, status: str = "ok", script: str = "fake.sh") -> GradeResult:
    return GradeResult(
        reward=reward,
        status=status,
        verifier_outputs={},
        job_dir=None,
        script=script,  # type: ignore[arg-type]
    )


def _fake_grade(
    oracle_reward: float | None = 1.0,
    blank_reward: float | None = 0.0,
    honest_reward: float | None = None,
    oracle_status: str = "ok",
    blank_status: str = "ok",
) -> Any:
    """Fake grade_fn keyed on submission shape: oracle+no-op, env+empty, else honest."""

    def grade(package: str | Path, submission: Submission) -> GradeResult:
        if submission.base == "oracle" and not submission.content:
            return _grade(oracle_reward, oracle_status, "oracle.sh")
        if submission.base == "environment" and not submission.content:
            return _grade(blank_reward, blank_status, "blank.sh")
        return _grade(
            honest_reward if honest_reward is not None else oracle_reward, script="honest.sh"
        )

    return grade


def _manifest(maps: list[RequirementMap]) -> CampaignManifest:
    return CampaignManifest(
        campaign_id="camp-1",
        benchmark="bench",
        pin="pin-1",
        tasks=["t1", "t2"],
        requirement_maps=maps,
        models=dict(MODEL_DEFAULTS),
        budgets=dict(RED_BUDGETS),
        prompt_shas={"red": "sha256:abc"},
    )


def _map(task: str = "t1") -> RequirementMap:
    return RequirementMap.freeze(
        task,
        [
            RequirementItem(req_id="r1", text="reject empty output", covered_by=["t1"]),
            RequirementItem(req_id="r2", text="check the watermark", covered_by=[]),
        ],
    )


def _promoted(id: str, task: str, family: str, cls: str, status: str = "promoted") -> Hypothesis:
    hypothesis = raise_hypothesis(id=id, task=task, statement=f"{cls} on {task}")
    hypothesis.evidence.append({"family": family, "class": cls})
    if status != "candidate":
        transition_hypothesis(hypothesis, status)  # type: ignore[arg-type]
    return hypothesis


# --------------------------------------------------------------------------- #
# Hypothesis lifecycle
# --------------------------------------------------------------------------- #


def test_raise_starts_candidate_with_genesis_history() -> None:
    hypothesis = raise_hypothesis(id="h1", task="t1", statement="s", note="first")
    assert hypothesis.status == "candidate"
    assert hypothesis.history == [{"from": "genesis", "to": "candidate", "note": "first"}]


@pytest.mark.parametrize("terminal", ["promoted", "attached", "merged", "rejected", "inconclusive"])
def test_candidate_reaches_every_terminal_state(terminal: str) -> None:
    hypothesis = raise_hypothesis(id="h1", task="t1", statement="s")
    transition_hypothesis(hypothesis, terminal, note="done")  # type: ignore[arg-type]
    assert hypothesis.status == terminal
    assert hypothesis.history[-1] == {"from": "candidate", "to": terminal, "note": "done"}


def test_terminal_states_have_no_outgoing_transitions() -> None:
    hypothesis = raise_hypothesis(id="h1", task="t1", statement="s")
    transition_hypothesis(hypothesis, "promoted")
    with pytest.raises(ValueError, match="cannot transition promoted"):
        transition_hypothesis(hypothesis, "rejected")
    assert hypothesis.status == "promoted"
    assert len(hypothesis.history) == 2


def test_unknown_target_status_rejected() -> None:
    hypothesis = raise_hypothesis(id="h1", task="t1", statement="s")
    with pytest.raises(ValueError, match="cannot transition candidate"):
        transition_hypothesis(hypothesis, "candidate")


# --------------------------------------------------------------------------- #
# Harness controls
# --------------------------------------------------------------------------- #


def test_controls_ok_when_oracle_scores_1_and_empty_scores_0() -> None:
    report = run_controls("pkg", _fake_grade(), task="t1")
    assert isinstance(report, ControlReport)
    assert report.verdict == "ok"
    assert report.task == "t1"
    assert report.baseline.reward == 1.0
    assert report.negative.reward == 0.0
    assert report.legit is report.baseline  # no honest solve: legit reuses oracle


def test_legit_grades_honest_solve_when_available() -> None:
    honest = Submission(kind="file", path="/app/out.txt", content=b"honest", base="oracle")
    report = run_controls("pkg", _fake_grade(honest_reward=1.0), honest=honest)
    assert report.verdict == "ok"
    assert report.legit.script == "honest.sh"


@pytest.mark.parametrize(
    ("oracle_reward", "blank_reward", "oracle_status", "blank_status", "match"),
    [
        (0.0, 0.0, "ok", "ok", "oracle earned 0"),
        (0.5, 0.0, "ok", "ok", "oracle earned 0.5"),
        (1.0, 1.0, "ok", "ok", "empty submission earned 1"),
        (1.0, 0.5, "ok", "ok", "empty submission earned 0.5"),
        (None, 0.0, "infra", "ok", "baseline unscored"),
        (None, 0.0, "ok", "ok", "baseline unscored"),
        (1.0, None, "ok", "infra", "negative control unscored"),
        # task_broken takes precedence: both controls bad still reports task_broken.
        (0.0, 1.0, "ok", "ok", "oracle earned 0"),
        (None, None, "infra", "infra", "baseline unscored"),
    ],
)
def test_task_broken_precedence(
    oracle_reward: float | None,
    blank_reward: float | None,
    oracle_status: str,
    blank_status: str,
    match: str,
) -> None:
    report = run_controls(
        "pkg",
        _fake_grade(
            oracle_reward=oracle_reward,
            blank_reward=blank_reward,
            oracle_status=oracle_status,
            blank_status=blank_status,
        ),
    )
    assert report.verdict == "task_broken"
    assert match in report.reason


def test_control_submission_shapes() -> None:
    assert oracle_submission().base == "oracle" and oracle_submission().content == b""
    assert blank_submission().base == "environment" and blank_submission().content == b""


# --------------------------------------------------------------------------- #
# Broadcast synthesis
# --------------------------------------------------------------------------- #


def test_broadcast_needs_two_same_class_confirmations() -> None:
    solo = synthesize_broadcast([_promoted("h1", "t1", "insufficient-checking", "narrow-x")])
    assert solo == []
    pair = synthesize_broadcast(
        [
            _promoted("h1", "t1", "insufficient-checking", "narrow-x"),
            _promoted("h2", "t2", "insufficient-checking", "narrow-x"),
        ]
    )
    assert pair == [
        "in this universe, verifiers of family insufficient-checking fail to check narrow-x "
        "-- test whether this task's verifier also fails"
    ]


def test_broadcast_ignores_candidates_and_splits_classes() -> None:
    hypotheses = [
        _promoted("h1", "t1", "insufficient-checking", "narrow-x"),
        _promoted("h2", "t2", "insufficient-checking", "narrow-x"),
        _promoted("h3", "t1", "isolation", "state-writable", status="candidate"),
        _promoted("h4", "t2", "isolation", "state-writable"),
    ]
    assert len(synthesize_broadcast(hypotheses)) == 1


def test_broadcast_reads_nested_defect_mapping() -> None:
    hypothesis = raise_hypothesis(id="h1", task="t1", statement="s")
    hypothesis.evidence.append({"defect": {"family": "checker-logic", "class": "parser-bug"}})
    transition_hypothesis(hypothesis, "promoted")
    other = raise_hypothesis(id="h2", task="t2", statement="s")
    other.evidence.append({"family": "checker-logic", "class": "parser-bug"})
    transition_hypothesis(other, "promoted")
    (hint,) = synthesize_broadcast([hypothesis, other])
    assert "family checker-logic fail to check parser-bug" in hint


# --------------------------------------------------------------------------- #
# Manifest pinning
# --------------------------------------------------------------------------- #


def test_manifest_digest_stable_and_sensitive_to_requirement_changes() -> None:
    first, second = _map(), _map()
    assert first.sha256 == second.sha256
    assert manifest_digest(_manifest([first])) == manifest_digest(_manifest([second]))
    changed = RequirementMap.freeze(
        "t1",
        [
            RequirementItem(req_id="r1", text="reject empty output!!", covered_by=["t1"]),
            RequirementItem(req_id="r2", text="check the watermark", covered_by=[]),
        ],
    )
    assert manifest_digest(_manifest([changed])) != manifest_digest(_manifest([first]))


# --------------------------------------------------------------------------- #
# Waves
# --------------------------------------------------------------------------- #


def test_wave_collects_hypotheses_and_synthesizes_broadcast() -> None:
    manifest = _manifest([_map("t1"), _map("t2")])

    def factory(task_id: str, hints: list[str]) -> list[Hypothesis]:
        assert hints == ["prior hint"]
        return [_promoted(f"h-{task_id}", task_id, "insufficient-checking", "narrow-x")]

    result = run_wave(
        manifest,
        ["t1", "t2"],
        factory,
        ["prior hint"],
        grade_fn=_fake_grade(),
        packages={"t1": "pkg-t1", "t2": "pkg-t2"},
    )
    assert isinstance(result, WaveResult)
    assert result.campaign_id == "camp-1"
    assert [h.task for h in result.hypotheses] == ["t1", "t2"]
    assert result.skipped_task_broken == []
    assert len(result.broadcast) == 1


def test_wave_skips_task_broken_before_red() -> None:
    manifest = _manifest([_map("t1"), _map("t2")])
    seen: list[str] = []

    def factory(task_id: str, hints: list[str]) -> list[Hypothesis]:
        seen.append(task_id)
        return []

    def grade(package: str | Path, submission: Submission) -> GradeResult:
        if Path(str(package)).name == "bad-pkg":
            return _grade(1.0 if submission.base == "oracle" else 1.0)
        return _fake_grade()(package, submission)

    result = run_wave(
        manifest,
        ["t1", "t2"],
        factory,
        [],
        grade_fn=grade,
        packages={"t1": "good-pkg", "t2": "bad-pkg"},
    )
    assert result.skipped_task_broken == ["t2"]
    assert seen == ["t1"]


def test_wave_without_grade_fn_audits_every_task() -> None:
    manifest = _manifest([_map("t1")])
    result = run_wave(manifest, ["t1"], lambda task_id, hints: [], [])
    assert result.hypotheses == [] and result.skipped_task_broken == []


def test_wave_requires_package_mapping_when_grading() -> None:
    manifest = _manifest([_map("t1")])
    with pytest.raises(ValueError, match="no package mapping"):
        run_wave(manifest, ["t1"], lambda task_id, hints: [], [], grade_fn=_fake_grade())
