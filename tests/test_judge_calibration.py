"""Focused unit tests for E1 Judge Calibration over 44 Keyed Items."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from evallab.execution_contracts import PaidRunAuthorization
from evallab.judge_calibration import (
    CLASSES,
    EXPECTED_CLASS_COUNTS,
    FAMILIES,
    TOTAL_KEYED_ITEMS,
    TOTAL_MODEL_CALLS_PER_ARM,
    AuthorizedModelGrader,
    E1AuthorizationError,
    E1CalibrationError,
    E1IncompleteCorpusError,
    E1IncompleteRepetitionsError,
    E1MissingLaneIdentityError,
    E1TrajectoryLabelAccessError,
    ItemEvaluation,
    LexicalControlGrader,
    assert_no_trajectory_label_access,
    clopper_pearson_interval,
    compute_confusion_matrix,
    compute_diagnostics,
    load_keyed_corpus,
    main,
    run_lexical_control_arm,
    run_model_grader_arm,
)


@pytest.fixture
def repo_root() -> Path:
    """Resolve repository root."""
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def sample_authorization() -> PaidRunAuthorization:
    """Create a sample valid human PaidRunAuthorization."""
    return PaidRunAuthorization(
        spec_id="01KCALIBRATIONE1SPEC0000000",
        actor="peter",
        authorized_at=datetime.now(UTC),
        quota_override=False,
    )


# =========================================================================== #
# 1. Keyed Corpus Loading & Invariant Tests
# =========================================================================== #


def test_load_keyed_corpus_loads_all_44_items_with_exact_class_denominators(
    repo_root: Path,
) -> None:
    """Assert all 44 items across the two incident families load with expected class counts."""
    items = load_keyed_corpus(repo_root)
    assert len(items) == TOTAL_KEYED_ITEMS == 44

    by_family = {fam: [item for item in items if item.family == fam] for fam in FAMILIES}
    assert len(by_family["checkout-pool-exhaustion"]) == 22
    assert len(by_family["retry-storm-backlog"]) == 22

    class_counts: dict[str, int] = {}
    for item in items:
        class_counts[item.variant] = class_counts.get(item.variant, 0) + 1
        assert item.variant in CLASSES
        assert item.answer_key["variant"] == item.variant
        assert "criteria" in item.answer_key

    assert class_counts == EXPECTED_CLASS_COUNTS


def test_corpus_loader_fails_closed_on_missing_dir(tmp_path: Path) -> None:
    """Assert E1IncompleteCorpusError is raised if calibration root does not exist."""
    with pytest.raises(E1IncompleteCorpusError, match="Calibration directory not found"):
        load_keyed_corpus(tmp_path)


def test_trajectory_label_access_is_forbidden() -> None:
    """Assert security guard raises E1TrajectoryLabelAccessError when trajectory labels are accessed."""
    forbidden_path = Path("research/calibration/trajectory-labels/sample_trial.json")
    with pytest.raises(E1TrajectoryLabelAccessError, match="strictly forbids reading trajectory labels"):
        assert_no_trajectory_label_access(forbidden_path)


# =========================================================================== #
# 2. Clopper-Pearson Exact Confidence Interval Tests
# =========================================================================== #


def test_clopper_pearson_exact_intervals() -> None:
    """Assert exact binomial Clopper-Pearson intervals satisfy mathematical bounds."""
    # k = 0 boundary case
    low_0, high_0 = clopper_pearson_interval(0, 10)
    assert low_0 == 0.0
    assert 0.0 < high_0 < 1.0
    # For n=10, k=0, upper bound is 1 - 0.025^(1/10) ~ 0.308497
    assert pytest.approx(high_0, abs=1e-4) == 1.0 - 0.025 ** 0.1

    # k = n boundary case
    low_n, high_n = clopper_pearson_interval(10, 10)
    assert 0.0 < low_n < 1.0
    assert high_n == 1.0
    # For n=10, k=10, lower bound is 0.025^(1/10) ~ 0.691503
    assert pytest.approx(low_n, abs=1e-4) == 0.025 ** 0.1

    # Symmetric case k = 5, n = 10
    low_mid, high_mid = clopper_pearson_interval(5, 10)
    assert 0.0 < low_mid < 0.5 < high_mid < 1.0
    assert pytest.approx(0.5 - low_mid, abs=1e-6) == high_mid - 0.5

    # Invalid input guards
    with pytest.raises(ValueError, match="denominator n must be positive"):
        clopper_pearson_interval(0, 0)

    with pytest.raises(ValueError, match="successes k must be between 0 and n"):
        clopper_pearson_interval(11, 10)


# =========================================================================== #
# 3. Lexical Control Arm Tests
# =========================================================================== #


def test_free_deterministic_lexical_control_arm_executes_offline(repo_root: Path) -> None:
    """Assert the free deterministic lexical-control arm executes offline with zero model calls."""
    report = run_lexical_control_arm(repo_root, lane_id="deterministic-lexical-control-v1")

    assert report.arm_type == "lexical-control"
    assert report.lane_id == "deterministic-lexical-control-v1"
    assert report.total_items == 44
    assert report.total_calls == 44
    assert report.self_consistency_rate == 1.0
    assert report.deterministic_classes_consistency == 1.0
    assert not report.refused

    # 7x7 matrix validation
    matrix = report.confusion_matrix
    assert len(matrix.classes) == 7
    assert matrix.total_evaluations == 44
    assert set(matrix.matrix.keys()) == set(CLASSES)
    for row in matrix.matrix.values():
        assert set(row.keys()) == set(CLASSES)

    for cls_name in CLASSES:
        assert cls_name in matrix.per_class_intervals
        interval = matrix.per_class_intervals[cls_name]
        assert interval.n == EXPECTED_CLASS_COUNTS[cls_name]
        assert 0.0 <= interval.lower_95 <= interval.rate <= interval.upper_95 <= 1.0

    # Diagnostics presence
    assert 0.0 <= report.diagnostics.correct_vs_style_fluent_separation <= 1.0
    assert 0.0 <= report.diagnostics.style_only_false_positive_rate <= 1.0


# =========================================================================== #
# 4. Model Grader Arm Contract & Authorization Tests
# =========================================================================== #


def test_model_arm_fails_closed_without_paid_run_authorization(repo_root: Path) -> None:
    """Assert model arm strictly refuses execution when PaidRunAuthorization is missing."""
    mock_grader = MagicMock(return_value="correct")

    # 1. Constructor gate
    with pytest.raises(E1AuthorizationError, match="requires a valid PaidRunAuthorization"):
        AuthorizedModelGrader(
            grader_fn=mock_grader,
            authorization=None,  # type: ignore[arg-type]
            lane_id="gemini-3.7-flash-high",
        )

    # 2. Runner gate
    with pytest.raises(E1AuthorizationError, match="requires a valid PaidRunAuthorization"):
        run_model_grader_arm(
            repo_root=repo_root,
            grader=mock_grader,
            authorization=None,  # type: ignore[arg-type]
            lane_id="gemini-3.7-flash-high",
        )


def test_model_arm_fails_closed_without_lane_identity(
    repo_root: Path,
    sample_authorization: PaidRunAuthorization,
) -> None:
    """Assert model arm requires explicit lane/model identity."""
    mock_grader = MagicMock(return_value="correct")

    with pytest.raises(E1MissingLaneIdentityError, match="lane identity must be specified"):
        run_model_grader_arm(
            repo_root=repo_root,
            grader=mock_grader,
            authorization=sample_authorization,
            lane_id="",
        )


def test_model_arm_contract_executes_132_calls_across_3_repetitions(
    repo_root: Path,
    sample_authorization: PaidRunAuthorization,
) -> None:
    """Assert authorized model arm executes exact 3 repetitions per item (132 calls)."""
    calls: list[tuple[str, str]] = []

    def mock_classify(text: str, family: str) -> str:
        calls.append((text, family))
        return "correct"

    grader = AuthorizedModelGrader(
        grader_fn=mock_classify,
        authorization=sample_authorization,
        lane_id="gemini-3.7-flash-high",
    )

    report = run_model_grader_arm(
        repo_root=repo_root,
        grader=grader,
        authorization=sample_authorization,
        lane_id="gemini-3.7-flash-high",
        repetitions=3,
    )

    assert report.arm_type == "model-grader"
    assert report.lane_id == "gemini-3.7-flash-high"
    assert report.total_items == 44
    assert report.repetitions_per_item == 3
    assert report.total_calls == TOTAL_MODEL_CALLS_PER_ARM == 132
    assert len(calls) == 132
    assert report.self_consistency_rate == 1.0


def test_model_arm_fails_on_wrong_repetitions_count(
    repo_root: Path,
    sample_authorization: PaidRunAuthorization,
) -> None:
    """Assert model arm refuses any repetitions count other than exactly 3."""
    mock_grader = MagicMock(return_value="correct")
    with pytest.raises(E1IncompleteRepetitionsError, match="requires exactly 3 repetitions"):
        run_model_grader_arm(
            repo_root=repo_root,
            grader=mock_grader,
            authorization=sample_authorization,
            lane_id="gemini-3.7-flash-high",
            repetitions=2,
        )


def test_model_arm_refuses_matrix_on_deterministic_class_instability(
    repo_root: Path,
    sample_authorization: PaidRunAuthorization,
) -> None:
    """Assert model arm marks report refused when stability on empty/copied-evidence items is < 100%."""
    # Flaking grader that varies prediction on empty/copied-evidence items across reps
    call_counts: dict[str, int] = {}

    def flaking_classify(text: str, family: str) -> str:
        # Detect short text (empty document)
        if len(text.strip()) == 0:
            call_counts["empty"] = call_counts.get("empty", 0) + 1
            return "empty" if call_counts["empty"] % 2 == 1 else "style-only-fluent"
        return "correct"

    grader = AuthorizedModelGrader(
        grader_fn=flaking_classify,
        authorization=sample_authorization,
        lane_id="flaking-grader",
    )

    report = run_model_grader_arm(
        repo_root=repo_root,
        grader=grader,
        authorization=sample_authorization,
        lane_id="flaking-grader",
    )

    assert report.refused is True
    assert report.refusal_reason is not None
    assert "deterministic classes" in report.refusal_reason.lower()
    assert report.deterministic_classes_consistency < 1.0


# =========================================================================== #
# 5. Confusion Matrix & Diagnostics Computation Tests
# =========================================================================== #


def test_confusion_matrix_and_diagnostics_calculation() -> None:
    """Assert confusion matrix and style vs cause separation diagnostics compute accurately."""
    evaluations: list[ItemEvaluation] = []

    # 10 correct items: 8 predicted correct, 2 predicted subtly-wrong-cause
    for i in range(10):
        pred = "correct" if i < 8 else "subtly-wrong-cause"
        evaluations.append(
            ItemEvaluation(
                document_id=f"correct-{i}",
                family="checkout-pool-exhaustion",
                true_variant="correct",
                predictions=(pred, pred, pred),
                consensus_prediction=pred,
                is_consistent=True,
            )
        )

    # 6 style-only items: 5 predicted style-only, 1 misclassified as correct
    for i in range(6):
        pred = "correct" if i == 0 else "style-only-fluent"
        evaluations.append(
            ItemEvaluation(
                document_id=f"style-{i}",
                family="checkout-pool-exhaustion",
                true_variant="style-only-fluent",
                predictions=(pred, pred, pred),
                consensus_prediction=pred,
                is_consistent=True,
            )
        )

    # 10 subtly-wrong items: 9 predicted subtly-wrong, 1 predicted correct
    for i in range(10):
        pred = "correct" if i == 0 else "subtly-wrong-cause"
        evaluations.append(
            ItemEvaluation(
                document_id=f"subtly-{i}",
                family="retry-storm-backlog",
                true_variant="subtly-wrong-cause",
                predictions=(pred, pred, pred),
                consensus_prediction=pred,
                is_consistent=True,
            )
        )

    # Fill remaining classes
    for variant, count in [
        ("right-cause-useless-actions", 8),
        ("fabricated-evidence", 6),
        ("copied-evidence", 2),
        ("empty", 2),
    ]:
        for i in range(count):
            evaluations.append(
                ItemEvaluation(
                    document_id=f"{variant}-{i}",
                    family="retry-storm-backlog",
                    true_variant=variant,
                    predictions=(variant, variant, variant),
                    consensus_prediction=variant,
                    is_consistent=True,
                )
            )

    assert len(evaluations) == 44
    matrix = compute_confusion_matrix(evaluations)
    assert matrix.matrix["correct"]["correct"] == 8
    assert matrix.matrix["correct"]["subtly-wrong-cause"] == 2
    assert matrix.matrix["style-only-fluent"]["correct"] == 1
    assert matrix.matrix["style-only-fluent"]["style-only-fluent"] == 5

    diagnostics = compute_diagnostics(matrix)
    # style_fp = 1 / 6
    assert pytest.approx(diagnostics.style_only_false_positive_rate, abs=1e-4) == 1.0 / 6.0
    assert pytest.approx(diagnostics.correct_vs_style_fluent_separation, abs=1e-4) == 5.0 / 6.0
    assert diagnostics.style_discrimination_passed is True


# =========================================================================== #
# 6. CLI Integration Tests
# =========================================================================== #


def test_cli_module_main_entrypoint(repo_root: Path) -> None:
    """Assert module CLI main executes lexical-control arm and emits valid JSON when requested."""
    # Test JSON mode
    exit_code_json = main(["--repo-root", str(repo_root), "--arm", "lexical-control", "--json"])
    assert exit_code_json == 0

    # Test summary mode
    exit_code_summary = main(["--repo-root", str(repo_root), "--arm", "lexical-control"])
    assert exit_code_summary == 0
