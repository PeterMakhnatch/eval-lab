"""Behavioral tests for the pure Reef gate decision rules."""

from __future__ import annotations

import json

import pytest

from evallab_reef_gate.rules import (
    REASON_INSUFFICIENT_EVIDENCE,
    REASON_NOT_SIGNIFICANT,
    REASON_PUBLISH,
    REASON_REGRESSION_VETO,
    REASON_WORSE,
    GateConfig,
    GateVeto,
    decide_pairs,
    sign_test_p,
)

NAN = float("nan")
INF = float("inf")


def _decide(
    candidate: list[object],
    current: list[object],
    *,
    tasks: tuple[str, ...],
    repeats: int,
    config: GateConfig | None = None,
):
    return decide_pairs(
        candidate,
        current,
        task_ids=tasks,
        episode_repeats=repeats,
        config=config if config is not None else GateConfig(),
    )


class TestSignTestP:
    def test_exact_upper_tail_values(self) -> None:
        assert sign_test_p(0, 0) == 1.0
        assert sign_test_p(0, 4) == 1.0
        assert sign_test_p(4, 0) == 1 / 16
        assert sign_test_p(5, 0) == 1 / 32
        assert sign_test_p(3, 1) == 5 / 16
        assert sign_test_p(2, 2) == 11 / 16
        assert sign_test_p(4, 1) == 6 / 32
        assert sign_test_p(5, 1) == 7 / 64
        assert sign_test_p(10, 1) == 12 / 2048
        assert sign_test_p(10, 2) == 79 / 4096

    def test_rejects_negative_or_non_integer_counts(self) -> None:
        for wins, losses in ((-1, 0), (0, -1), (1.5, 0), (True, 0), (0, "2")):
            with pytest.raises(ValueError):
                sign_test_p(wins, losses)


class TestSignBoundary:
    def test_alpha_equality_does_not_select(self) -> None:
        # Five decisive wins give p = 1/32 exactly; alpha == p must reject.
        config = GateConfig(alpha=1 / 32)
        result = _decide([1.0] * 5, [0.0] * 5, tasks=("t",), repeats=5, config=config)
        assert result.reason_code == REASON_NOT_SIGNIFICANT
        assert result.selected is False
        assert result.p_value == pytest.approx(1 / 32)

    def test_alpha_just_above_p_selects(self) -> None:
        config = GateConfig(alpha=1 / 32 + 1e-9)
        result = _decide([1.0] * 5, [0.0] * 5, tasks=("t",), repeats=5, config=config)
        assert result.reason_code == REASON_PUBLISH
        assert result.selected is True

    def test_default_alpha_publishes_five_all_wins(self) -> None:
        result = _decide([1.0] * 5, [0.0] * 5, tasks=("t",), repeats=5)
        assert result.reason_code == REASON_PUBLISH
        assert result.selected is True
        assert (result.wins, result.losses, result.ties) == (5, 0, 0)
        assert result.valid_pairs == 5
        assert result.p_value == pytest.approx(1 / 32)

    def test_sign_boundary_above_alpha_rejects(self) -> None:
        # Four decisive wins: p = 1/16 = 0.0625 > 0.05, with enough valid
        # evidence, so the decision falls through to not_significant.
        config = GateConfig(min_valid_pairs=4)
        result = _decide([1.0] * 4, [0.0] * 4, tasks=("t",), repeats=4, config=config)
        assert result.reason_code == REASON_NOT_SIGNIFICANT
        assert result.p_value == pytest.approx(1 / 16)


class TestTies:
    def test_ties_count_as_valid_pairs_only(self) -> None:
        # Five ties: all valid, sign sample empty, p = 1.
        result = _decide([0.5] * 5, [0.5] * 5, tasks=("t",), repeats=5)
        assert (result.wins, result.losses, result.ties) == (0, 0, 5)
        assert result.valid_pairs == 5
        assert result.p_value == 1.0
        assert result.reason_code == REASON_NOT_SIGNIFICANT

    def test_ties_are_dropped_from_the_sign_sample(self) -> None:
        # Five wins plus two ties: the sign test sees only the five wins.
        candidate = [1.0] * 5 + [0.5, 0.5]
        current = [0.5] * 5 + [0.5, 0.5]
        result = _decide(candidate, current, tasks=("t",), repeats=7)
        assert result.valid_pairs == 7
        assert (result.wins, result.losses, result.ties) == (5, 0, 2)
        assert result.p_value == pytest.approx(1 / 32)
        assert result.reason_code == REASON_PUBLISH


class TestMissingScores:
    def test_none_on_either_or_both_sides_is_one_invalid_pair(self) -> None:
        config = GateConfig(min_valid_pairs=1)
        candidate = [1.0, None, 1.0, None]
        current = [None, 1.0, 1.0, None]
        result = _decide(candidate, current, tasks=("t",), repeats=4, config=config)
        assert result.invalid_pairs == 3
        assert result.valid_pairs == 1
        assert [pair.result for pair in result.pairs] == ["invalid", "invalid", "tie", "invalid"]
        assert result.pairs[0].invalid_reason == "current_score_missing"
        assert result.pairs[1].invalid_reason == "candidate_score_missing"
        assert result.pairs[3].invalid_reason == "candidate_score_missing; current_score_missing"
        assert result.reason_code == REASON_NOT_SIGNIFICANT

    def test_missing_does_not_fabricate_a_loss_or_veto(self) -> None:
        # A None candidate episode on a perfect current task is missing
        # evidence: no loss is tallied and no veto failure is counted.
        candidate = [1.0, 1.0, None, 1.0, 1.0]
        result = _decide(candidate, [1.0] * 5, tasks=("t",), repeats=5)
        assert result.invalid_pairs == 1
        assert (result.wins, result.losses, result.ties) == (0, 0, 4)
        assert result.vetoes == ()
        assert result.reason_code == REASON_INSUFFICIENT_EVIDENCE


class TestValidZero:
    def test_zero_is_a_valid_score_not_missing(self) -> None:
        config = GateConfig(min_valid_pairs=2)
        candidate = [0.0, 0.0, None]
        current = [1.0, 0.0, 1.0]
        result = _decide(candidate, current, tasks=("t",), repeats=3, config=config)
        assert [pair.valid for pair in result.pairs] == [True, True, False]
        assert [pair.result for pair in result.pairs] == ["loss", "tie", "invalid"]
        assert result.valid_pairs == 2
        assert result.reason_code == REASON_WORSE

    def test_invalid_pairs_keep_the_readable_side_on_record(self) -> None:
        candidate = [0.0, None]
        result = _decide(candidate, [1.0, 1.0], tasks=("t",), repeats=2)
        assert (result.pairs[0].candidate_score, result.pairs[0].current_score) == (0.0, 1.0)
        assert (result.pairs[1].candidate_score, result.pairs[1].current_score) == (None, 1.0)
        assert result.pairs[1].invalid_reason == "candidate_score_missing"

    def test_current_side_missing_keeps_the_candidate_score(self) -> None:
        result = _decide([0.9], [None], tasks=("t",), repeats=1)
        assert (result.pairs[0].candidate_score, result.pairs[0].current_score) == (0.9, None)
        assert result.pairs[0].invalid_reason == "current_score_missing"


class TestNonFiniteScores:
    def test_bool_nonnumeric_and_nonfinite_are_invalid_never_zero(self) -> None:
        candidate = [True, "0.9", NAN, INF, 0.5]
        current = [0.5, 0.5, 0.5, 0.5, NAN]
        result = _decide(candidate, current, tasks=("t",), repeats=5)
        assert result.valid_pairs == 0
        assert result.invalid_pairs == 5
        assert result.reason_code == REASON_INSUFFICIENT_EVIDENCE
        assert [pair.result for pair in result.pairs] == ["invalid"] * 5
        reasons = [pair.invalid_reason for pair in result.pairs]
        assert reasons == [
            "candidate_score_not_numeric",
            "candidate_score_not_numeric",
            "candidate_score_not_finite",
            "candidate_score_not_finite",
            "current_score_not_finite",
        ]

    def test_nonfinite_sanitizes_only_the_offending_side(self) -> None:
        result = _decide([NAN, INF, 0.5], [1.0, 1.0, NAN], tasks=("t",), repeats=3)
        assert [(pair.candidate_score, pair.current_score) for pair in result.pairs] == [
            (None, 1.0),
            (None, 1.0),
            (0.5, None),
        ]


class TestMinimumEvidence:
    def test_four_all_wins_lack_required_evidence_and_never_select(self) -> None:
        result = _decide([1.0] * 4, [0.0] * 4, tasks=("t",), repeats=4)
        # Four valid pairs are below the default min evidence of five, and
        # p = 1/16 = 0.0625 also misses alpha = 0.05; both reasons reject.
        assert result.reason_code == REASON_INSUFFICIENT_EVIDENCE
        assert result.selected is False
        assert result.valid_pairs == 4

    def test_evidence_precedes_veto(self) -> None:
        # One valid below-threshold loss on a perfect current task would veto,
        # but a single valid pair is below the required evidence.
        result = _decide([0.0, None], [1.0, 1.0], tasks=("t",), repeats=2)
        assert result.vetoes == (GateVeto(task_id="t", failure_count=1, failed_repeat_indices=(0,), threshold=1),)
        assert result.reason_code == REASON_INSUFFICIENT_EVIDENCE
        assert result.selected is False

    def test_no_usable_scores_at_all(self) -> None:
        result = _decide([None] * 5, [None] * 5, tasks=("t",), repeats=5)
        assert result.reason_code == REASON_INSUFFICIENT_EVIDENCE
        assert result.valid_pairs == 0


class TestRegressionVeto:
    def test_below_threshold_on_a_perfect_current_task_vetoes(self) -> None:
        candidate = [1.0, 1.0, 1.0, 1.0, 0.9]
        result = _decide(candidate, [1.0] * 5, tasks=("t",), repeats=5)
        assert result.valid_pairs == 5
        assert (result.wins, result.losses, result.ties) == (0, 1, 4)
        assert result.vetoes == (
            GateVeto(task_id="t", failure_count=1, failed_repeat_indices=(4,), threshold=1),
        )
        assert result.reason_code == REASON_REGRESSION_VETO
        assert result.selected is False
        assert result.p_value == 1.0

    def test_veto_threshold_is_per_task_not_pooled(self) -> None:
        # Two protected tasks with one failure each never reach a threshold of
        # two, so otherwise-significant evidence still publishes.
        spread_candidate = [1.0, 1.0, 1.0, 1.0, 0.0] * 2 + [1.0] * 10
        spread_current = [1.0] * 10 + [0.0] * 10
        config = GateConfig(regression_failure_threshold=2)
        spread = _decide(spread_candidate, spread_current, tasks=("a", "b", "w1", "w2"), repeats=5, config=config)
        assert (spread.wins, spread.losses, spread.ties) == (10, 2, 8)
        assert spread.p_value == pytest.approx(79 / 4096)
        assert spread.vetoes == ()
        assert spread.reason_code == REASON_PUBLISH
        assert spread.selected is True

        # The same aggregate record with both failures on one task vetoes.
        concentrated_candidate = [1.0, 1.0, 1.0, 0.0, 0.0] + [1.0] * 5 + [1.0] * 10
        concentrated_current = [1.0] * 10 + [0.0] * 10
        concentrated = _decide(
            concentrated_candidate, concentrated_current, tasks=("a", "b", "w1", "w2"), repeats=5, config=config
        )
        assert (concentrated.wins, concentrated.losses, concentrated.ties) == (10, 2, 8)
        assert concentrated.p_value == pytest.approx(79 / 4096)
        assert concentrated.vetoes == (
            GateVeto(task_id="a", failure_count=2, failed_repeat_indices=(3, 4), threshold=2),
        )
        assert concentrated.reason_code == REASON_REGRESSION_VETO
        assert concentrated.selected is False

    def test_failure_threshold_is_configurable(self) -> None:
        candidate = [1.0, 1.0, 1.0, 1.0, 0.9]
        config = GateConfig(regression_failure_threshold=2)
        result = _decide(candidate, [1.0] * 5, tasks=("t",), repeats=5, config=config)
        assert result.vetoes == ()
        assert result.reason_code == REASON_WORSE

    def test_current_below_threshold_leaves_the_task_unprotected(self) -> None:
        # The current tree already fails this task, so a candidate failure
        # there is not a regression of a protected capability.
        result = _decide([0.0] * 5, [0.9] * 5, tasks=("t",), repeats=5)
        assert result.vetoes == ()
        assert result.reason_code == REASON_WORSE

    def test_current_missing_leaves_the_task_unprotected(self) -> None:
        # An invalid current episode means the perfect-current premise is
        # unproven; it neither protects the task nor fabricates failures.
        current = [1.0, None, 1.0, 1.0, 1.0]
        result = _decide([0.0] * 5, current, tasks=("t",), repeats=5)
        assert result.vetoes == ()
        assert result.reason_code == REASON_INSUFFICIENT_EVIDENCE

    def test_veto_counts_only_candidate_failures_on_protected_tasks(self) -> None:
        # Three tasks x two repeats. Task b is protected (current perfect);
        # its two zero scores count. Tasks a and c are unprotected, so their
        # candidate losses do not.
        candidate = [1.0, 1.0, 0.0, 0.0, 1.0, 1.0]
        current = [0.0, 0.0, 1.0, 1.0, 0.0, 0.0]
        result = _decide(candidate, current, tasks=("a", "b", "c"), repeats=2)
        assert result.vetoes == (
            GateVeto(task_id="b", failure_count=2, failed_repeat_indices=(0, 1), threshold=1),
        )
        assert (result.wins, result.losses) == (4, 2)
        assert result.reason_code == REASON_REGRESSION_VETO

    def test_veto_overrides_publishable_sign_evidence(self) -> None:
        # Ten wins against one loss gives p = 12/2048 ~ 0.0059, well under
        # alpha, yet the single protected-task regression must still reject.
        candidate = [1.0] * 5 + [1.0, 1.0, 1.0, 1.0, 0.0] + [1.0] * 5
        current = [0.0] * 5 + [1.0] * 5 + [0.0] * 5
        result = _decide(candidate, current, tasks=("a", "b", "c"), repeats=5)
        assert result.p_value == pytest.approx(12 / 2048)
        assert result.vetoes == (
            GateVeto(task_id="b", failure_count=1, failed_repeat_indices=(4,), threshold=1),
        )
        assert result.reason_code == REASON_REGRESSION_VETO
        assert result.selected is False

    def test_same_evidence_publishes_when_threshold_is_raised(self) -> None:
        candidate = [1.0] * 5 + [1.0, 1.0, 1.0, 1.0, 0.0] + [1.0] * 5
        current = [0.0] * 5 + [1.0] * 5 + [0.0] * 5
        config = GateConfig(regression_failure_threshold=2)
        result = _decide(candidate, current, tasks=("a", "b", "c"), repeats=5, config=config)
        assert result.vetoes == ()
        assert result.reason_code == REASON_PUBLISH
        assert result.selected is True


class TestTaskGrouping:
    def test_records_carry_task_and_repeat_identity(self) -> None:
        candidate = [1.0, 0.5, 1.0, 0.5, 1.0, 0.5]
        current = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        result = _decide(candidate, current, tasks=("a", "b", "c"), repeats=2)
        assert [(pair.task_id, pair.repeat_index) for pair in result.pairs] == [
            ("a", 0),
            ("a", 1),
            ("b", 0),
            ("b", 1),
            ("c", 0),
            ("c", 1),
        ]
        assert [pair.index for pair in result.pairs] == list(range(6))


class TestFallthroughReasons:
    def test_losses_exceeding_wins_is_worse(self) -> None:
        result = _decide([0.0] * 5, [0.5] * 5, tasks=("t",), repeats=5)
        assert (result.wins, result.losses) == (0, 5)
        assert result.reason_code == REASON_WORSE

    def test_equal_wins_and_losses_is_not_significant(self) -> None:
        candidate = [1.0, 1.0, 0.0, 0.0, 1.0]
        current = [0.0, 0.0, 1.0, 1.0, 1.0]
        result = _decide(candidate, current, tasks=("t",), repeats=5)
        assert (result.wins, result.losses, result.ties) == (2, 2, 1)
        assert result.p_value == pytest.approx(11 / 16)
        assert result.reason_code == REASON_NOT_SIGNIFICANT

    def test_more_wins_but_not_significant_is_not_worse(self) -> None:
        candidate = [1.0, 1.0, 1.0, 0.0, 1.0]
        current = [0.0, 0.0, 0.0, 1.0, 1.0]
        result = _decide(candidate, current, tasks=("t",), repeats=5)
        assert (result.wins, result.losses) == (3, 1)
        assert result.p_value == pytest.approx(5 / 16)
        assert result.reason_code == REASON_NOT_SIGNIFICANT


class TestMalformedShapes:
    def test_mismatched_score_vector_lengths(self) -> None:
        with pytest.raises(ValueError, match="equal length"):
            _decide([1.0, 2.0], [1.0], tasks=("t",), repeats=2)

    def test_scores_do_not_fill_whole_task_groups(self) -> None:
        with pytest.raises(ValueError, match="contiguous positions per task"):
            _decide([1.0] * 5, [1.0] * 5, tasks=("a", "b"), repeats=2)

    def test_invalid_episode_repeats(self) -> None:
        for repeats in (0, -1, 1.5, True):
            with pytest.raises(ValueError):
                _decide([1.0], [1.0], tasks=("t",), repeats=repeats)

    def test_task_ids_must_be_non_empty_strings(self) -> None:
        with pytest.raises(ValueError, match="non-empty strings"):
            _decide([1.0, 1.0], [1.0, 1.0], tasks=("a", 3), repeats=1)
        with pytest.raises(ValueError, match="non-empty strings"):
            _decide([1.0, 1.0], [1.0, 1.0], tasks=("a", ""), repeats=1)

    def test_score_and_task_arguments_must_be_real_sequences(self) -> None:
        with pytest.raises(ValueError, match="candidate_scores must be a sequence"):
            _decide("1.0", [1.0], tasks=("t",), repeats=1)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="candidate_scores must be a sequence"):
            _decide(None, [1.0], tasks=("t",), repeats=1)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="current_scores must be a sequence"):
            _decide([1.0], 7, tasks=("t",), repeats=1)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="task_ids must be a sequence"):
            _decide([1.0], [1.0], tasks="tt", repeats=1)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="task_ids must be a sequence"):
            _decide([1.0], [1.0], tasks=None, repeats=1)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="candidate_scores must be a sequence"):
            _decide((value for value in [1.0]), [1.0], tasks=("t",), repeats=1)  # type: ignore[arg-type]

    def test_config_must_be_a_gate_config(self) -> None:
        with pytest.raises(ValueError):
            decide_pairs(
                [1.0],
                [0.0],
                task_ids=("t",),
                episode_repeats=1,
                config="alpha=0.05",  # type: ignore[arg-type]
            )


class TestGateConfigValidation:
    def test_rejects_unusable_alpha(self) -> None:
        for alpha in (0.0, 1.0, -0.1, 1.5, NAN, INF, True, "0.05"):
            with pytest.raises(ValueError):
                GateConfig(alpha=alpha)

    def test_rejects_unusable_pass_threshold(self) -> None:
        for threshold in (NAN, INF, -INF, True, "1.0"):
            with pytest.raises(ValueError):
                GateConfig(pass_threshold=threshold)

    def test_pass_threshold_accepts_any_finite_number(self) -> None:
        assert GateConfig(pass_threshold=2.5).pass_threshold == 2.5
        assert GateConfig(pass_threshold=0.5).pass_threshold == 0.5
        assert GateConfig(pass_threshold=-1.0).pass_threshold == -1.0

    def test_rejects_non_positive_integer_counts(self) -> None:
        for min_valid_pairs in (0, -1, 2.5, True):
            with pytest.raises(ValueError):
                GateConfig(min_valid_pairs=min_valid_pairs)
        for threshold in (0, -2, 1.5, True):
            with pytest.raises(ValueError):
                GateConfig(regression_failure_threshold=threshold)

    def test_accepts_boundary_configuration(self) -> None:
        config = GateConfig(alpha=0.5, min_valid_pairs=1, pass_threshold=2.5, regression_failure_threshold=3)
        assert config.to_dict() == {
            "alpha": 0.5,
            "min_valid_pairs": 1,
            "pass_threshold": 2.5,
            "regression_failure_threshold": 3,
        }


class TestSerialization:
    def test_to_dict_is_json_ready_with_contract_fields(self) -> None:
        result = _decide([1.0] * 5, [0.0] * 5, tasks=("t",), repeats=5)
        record = result.to_dict()
        assert set(record) == {
            "selected",
            "reason_code",
            "pairs",
            "valid_pairs",
            "invalid_pairs",
            "wins",
            "losses",
            "ties",
            "p_value",
            "vetoes",
            "config",
        }
        assert set(record["pairs"][0]) == {
            "index",
            "task_id",
            "repeat_index",
            "candidate_score",
            "current_score",
            "valid",
            "result",
            "invalid_reason",
        }
        assert set(record["config"]) == {
            "alpha",
            "min_valid_pairs",
            "pass_threshold",
            "regression_failure_threshold",
        }
        assert record["vetoes"] == []
        assert json.dumps(record)  # strict: any NaN or inf would raise

    def test_vetoes_serialize_as_per_task_records(self) -> None:
        candidate = [1.0, 1.0, 1.0, 1.0, 0.9]
        result = _decide(candidate, [1.0] * 5, tasks=("t",), repeats=5)
        record = result.to_dict()
        assert record["vetoes"] == [
            {"task_id": "t", "failure_count": 1, "failed_repeat_indices": [4], "threshold": 1}
        ]
        assert json.dumps(record)

    def test_invalid_scores_serialize_without_nan(self) -> None:
        result = _decide([NAN, 1.0], [1.0, None], tasks=("t",), repeats=2)
        record = result.to_dict()
        assert json.dumps(record)
        assert record["pairs"][0]["invalid_reason"] == "candidate_score_not_finite"
        assert (record["pairs"][0]["candidate_score"], record["pairs"][0]["current_score"]) == (None, 1.0)
        assert record["pairs"][1]["invalid_reason"] == "current_score_missing"
        assert (record["pairs"][1]["candidate_score"], record["pairs"][1]["current_score"]) == (1.0, None)
