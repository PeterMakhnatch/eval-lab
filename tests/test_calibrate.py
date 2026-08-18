"""Tests for SG-4: LLM-as-a-Verifier calibration and best-of-k selection lift.

Covers:
- Selection lift on fixture cohort with known rewards (pass@1, selected@k, oracle ceiling)
- Underpowered cohort rendering 'not distinguishable' / NOT_COMPARABLE
- Exclusion and separate reporting of never-measured exception trials
- Degenerate always-pass verifier unmasked by Cohen's Kappa (=0.0) and Balanced Accuracy (=0.50)
- CalibrationRecord persistence and history round-trip
- Missing verifier dependency degradation with clear remediation
- Paid model authorization token spend gate (refusal without opt-in)
- Hard boundary: no battery or registry path imports or consumes LLM verdicts
- Eval card generation with purpose='calibration' and stubbed provenance labeling
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from evallab.calibrate import (
    AlwaysPassStubVerifier,
    LlmVerifier,
    MissingVerifierDependencyError,
    PaidModelAuthorizationError,
    StubVerifier,
    TaskAttemptUnit,
    compute_agreement_metrics,
    draft_verifier_calibration_card,
    evaluate_selection_lift,
    evaluate_verifier_agreement,
    load_calibration_records,
    load_llm_verifier,
    save_calibration_record,
)
from evallab.cohort import NOT_COMPARABLE
from evallab.queue import new_ulid
from evallab.schemas import CalibrationRecord, CriterionAgreement


def test_selection_lift_known_fixture() -> None:
    """Assert pass@1, selected@k, and oracle ceiling match exact analytical values."""
    # Task 1: 3 trials [0.0, 1.0, 0.0], verifier picks index 1 (reward 1.0)
    # Task 2: 3 trials [1.0, 1.0, 1.0], verifier picks index 0 (reward 1.0)
    # Task 3: 3 trials [0.0, 0.0, 0.0], verifier picks index 2 (reward 0.0)
    # Task 4: 3 trials [1.0, 0.0, 0.0], verifier picks index 0 (reward 1.0)
    task1 = TaskAttemptUnit(
        task_name="task-1",
        task_digest=None,
        trial_ids=["t1", "t2", "t3"],
        rewards=[0.0, 1.0, 0.0],
        exception_classes=[None, None, None],
        candidate_texts=["cand-0", "cand-1", "cand-2"],
    )
    task2 = TaskAttemptUnit(
        task_name="task-2",
        task_digest=None,
        trial_ids=["t4", "t5", "t6"],
        rewards=[1.0, 1.0, 1.0],
        exception_classes=[None, None, None],
        candidate_texts=["cand-0", "cand-1", "cand-2"],
    )
    task3 = TaskAttemptUnit(
        task_name="task-3",
        task_digest=None,
        trial_ids=["t7", "t8", "t9"],
        rewards=[0.0, 0.0, 0.0],
        exception_classes=[None, None, None],
        candidate_texts=["cand-0", "cand-1", "cand-2"],
    )
    task4 = TaskAttemptUnit(
        task_name="task-4",
        task_digest=None,
        trial_ids=["t10", "t11", "t12"],
        rewards=[1.0, 0.0, 0.0],
        exception_classes=[None, None, None],
        candidate_texts=["cand-0", "cand-1", "cand-2"],
    )

    # Oracle-aligned selector: picks index 1 for task-1, 0 for task-2, 2 for task-3, 0 for task-4
    picks = {
        "task-1": 1,
        "task-2": 0,
        "task-3": 2,
        "task-4": 0,
    }
    verifier = StubVerifier(select_fn=lambda task, cands: picks[task])

    report = evaluate_selection_lift([task1, task2, task3, task4], verifier, k=3)

    assert report.n_tasks == 4
    assert report.k == 3
    assert not report.is_underpowered

    # Task 1: pass@1 = 1/3, selected@3 = 1.0, oracle = 1.0
    # Task 2: pass@1 = 3/3, selected@3 = 1.0, oracle = 1.0
    # Task 3: pass@1 = 0/3, selected@3 = 0.0, oracle = 0.0
    # Task 4: pass@1 = 1/3, selected@3 = 1.0, oracle = 1.0
    # Cohort means:
    # pass@1 = (1/3 + 1.0 + 0.0 + 1/3) / 4 = (5/3) / 4 = 5/12 = 0.416666...
    # selected@3 = (1.0 + 1.0 + 0.0 + 1.0) / 4 = 3/4 = 0.75
    # oracle_ceiling = (1.0 + 1.0 + 0.0 + 1.0) / 4 = 3/4 = 0.75
    # lift = 0.75 - 5/12 = 4/12 = 1/3 = 0.333333...

    assert report.pass_at_1 is not None
    assert report.selected_at_k is not None
    assert report.oracle_ceiling is not None
    assert report.selection_lift is not None

    assert pytest.approx(report.pass_at_1, rel=1e-5) == 5 / 12
    assert pytest.approx(report.selected_at_k, rel=1e-5) == 0.75
    assert pytest.approx(report.oracle_ceiling, rel=1e-5) == 0.75
    assert pytest.approx(report.selection_lift, rel=1e-5) == 1 / 3

    assert report.pass_at_1_interval is not None
    assert report.selected_at_k_interval is not None
    assert report.oracle_ceiling_interval is not None
    assert report.selection_lift_interval is not None


def test_selection_lift_underpowered_cohort_renders_not_distinguishable() -> None:
    """An underpowered cohort (n_tasks < 2) renders 'not distinguishable' / NOT_COMPARABLE."""
    single_task = TaskAttemptUnit(
        task_name="single-task",
        task_digest=None,
        trial_ids=["t1", "t2", "t3"],
        rewards=[0.0, 1.0, 1.0],
        exception_classes=[None, None, None],
        candidate_texts=["c0", "c1", "c2"],
    )
    verifier = AlwaysPassStubVerifier()
    report = evaluate_selection_lift([single_task], verifier, k=3)

    assert report.is_underpowered
    assert report.n_tasks == 1
    assert report.pass_at_1_text == NOT_COMPARABLE
    assert report.selected_at_k_text == NOT_COMPARABLE
    assert report.oracle_ceiling_text == NOT_COMPARABLE
    assert report.selection_lift_text == NOT_COMPARABLE
    assert any("Underpowered cohort" in threat for threat in report.threats)


def test_agreement_excludes_never_measured_trials() -> None:
    """Never-measured trials (non-null exception_class) are excluded from agreement."""
    trials: list[tuple[str, float | None, str | None, str]] = [
        # 4 measured passes
        ("t1", 1.0, None, "task-a"),
        ("t2", 1.0, None, "task-a"),
        ("t3", 1.0, None, "task-a"),
        ("t4", 1.0, None, "task-a"),
        # 2 measured failures
        ("t5", 0.0, None, "task-b"),
        ("t6", 0.0, None, "task-b"),
        # 3 never-measured exceptions
        ("t7", None, "ValueError", "task-c"),
        ("t8", 0.0, "NonZeroAgentExitCodeError", "task-c"),
        ("t9", 1.0, "DockerTimeoutError", "task-c"),
    ]

    # Stub verifier predicting 1.0 on all
    verifier = AlwaysPassStubVerifier()
    report = evaluate_verifier_agreement(trials, verifier)

    # Total 9 trials: 6 measured (4 pass, 2 fail), 3 never-measured
    assert report.metrics.class_balance.total == 9
    assert report.metrics.class_balance.never_measured == 3
    assert report.metrics.class_balance.passed == 4
    assert report.metrics.class_balance.failed == 2

    # Confusion matrix over the 6 measured trials only
    assert report.metrics.confusion.tp == 4
    assert report.metrics.confusion.fp == 2
    assert report.metrics.confusion.tn == 0
    assert report.metrics.confusion.fn == 0

    assert len(report.never_measured_trials) == 3
    exc_classes = {item["exception_class"] for item in report.never_measured_trials}
    assert exc_classes == {"ValueError", "NonZeroAgentExitCodeError", "DockerTimeoutError"}


def test_degenerate_always_pass_verifier_exposed_by_kappa_and_balanced_accuracy() -> None:
    """Always-pass verifier yields Kappa=0.0 and Balanced Accuracy=50% on unbalanced corpus."""
    ground_truths = [1] * 68 + [0] * 8
    always_pass_preds = [1] * 76

    metrics = compute_agreement_metrics(ground_truths, always_pass_preds, never_measured_count=16)

    # Raw agreement is misleadingly high (89.5%)
    assert pytest.approx(metrics.raw_agreement, rel=1e-3) == 68 / 76
    assert metrics.raw_agreement > 0.89

    # Cohen's Kappa completely unmasks the degenerate verifier: 0.0
    assert pytest.approx(metrics.cohens_kappa, abs=1e-6) == 0.0

    # Balanced accuracy reflects random guessing: exactly 50%
    assert pytest.approx(metrics.balanced_accuracy, abs=1e-6) == 0.50

    # Sensitivity is 100%, but Specificity is 0%
    assert metrics.pass_sensitivity == 1.0
    assert metrics.fail_specificity == 0.0

    # Matthews Correlation Coefficient is 0.0
    assert pytest.approx(metrics.mcc, abs=1e-6) == 0.0


def test_calibration_record_round_trip(tmp_path: Path) -> None:
    """CalibrationRecord validates, persists to disk, and round-trips through history loader."""
    calib_id = new_ulid()
    record = CalibrationRecord(
        calib_id=calib_id,
        judge_model="stub-verifier/deterministic",
        rubric_digest="sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        corpus_digest="sha256:fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
        per_criterion_agreement={
            "execution_ground_truth": CriterionAgreement(agreements=68, total=76, rate=68 / 76),
            "pass_class_sensitivity": CriterionAgreement(agreements=68, total=68, rate=1.0),
            "fail_class_specificity": CriterionAgreement(agreements=0, total=8, rate=0.0),
        },
        date=datetime.now(UTC),
    )

    dest = save_calibration_record(tmp_path, record)
    assert dest.is_file()
    assert dest.name == f"{calib_id}.json"

    # Reload from records root
    records = load_calibration_records(tmp_path)
    assert len(records) == 1
    loaded = records[0]

    assert loaded.calib_id == record.calib_id
    assert loaded.judge_model == record.judge_model
    assert loaded.rubric_digest == record.rubric_digest
    assert loaded.corpus_digest == record.corpus_digest
    assert loaded.per_criterion_agreement["execution_ground_truth"].agreements == 68
    assert loaded.per_criterion_agreement["execution_ground_truth"].total == 76


def test_missing_verifier_dependency_degrades_gracefully() -> None:
    """Missing llm-verifier raises MissingVerifierDependencyError with install advice."""
    with patch(
        "importlib.import_module", side_effect=ImportError("No module named 'llm_verifier'")
    ):
        with pytest.raises(MissingVerifierDependencyError) as exc_info:
            load_llm_verifier()
        assert "eval-lab[verifier]" in str(exc_info.value)
        assert "llm-verifier is not installed" in str(exc_info.value)


def test_paid_model_authorization_gate() -> None:
    """LlmVerifier refuses to run without explicit allow_paid_tokens=True opt-in."""
    verifier = LlmVerifier(allow_paid_tokens=False)

    with pytest.raises(PaidModelAuthorizationError) as exc_info:
        verifier.select("reverse string", ["s[::-1]", "s"])
    assert "allow_paid_tokens=True" in str(exc_info.value)

    with pytest.raises(PaidModelAuthorizationError) as exc_info:
        verifier.score("reverse string", "s[::-1]")
    assert "allow_paid_tokens=True" in str(exc_info.value)


def test_hard_boundary_no_battery_or_registry_imports_verifier() -> None:
    """Assert task_workbench.py and registry.py never import llm_verifier or calibrate."""
    repo_src = Path(__file__).resolve().parent.parent / "src/evallab"
    target_files = [repo_src / "task_workbench.py", repo_src / "registry.py"]

    forbidden_modules = {"llm_verifier", "evallab.calibrate"}

    for target in target_files:
        assert target.is_file(), f"missing source file: {target}"
        tree = ast.parse(target.read_text(encoding="utf-8"), filename=str(target))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for forbidden in forbidden_modules:
                        assert not alias.name.startswith(forbidden), (
                            f"Boundary violation: {target.name} imports {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for forbidden in forbidden_modules:
                    assert not mod.startswith(forbidden), (
                        f"Boundary violation: {target.name} imports from {mod}"
                    )
                if mod == "evallab":
                    for alias in node.names:
                        assert alias.name != "calibrate", (
                            f"Boundary violation: {target.name} imports calibrate from evallab"
                        )


def test_eval_card_generation_purpose_calibration(tmp_path: Path) -> None:
    """Eval card generation emits purpose='calibration' with both metrics and stub labeling."""
    # Create template fixture under tmp_path
    template_dir = tmp_path / "research/cards"
    template_dir.mkdir(parents=True, exist_ok=True)
    orig_template = Path(__file__).resolve().parent.parent / "research/cards/TEMPLATE.md"
    template_file = template_dir / "TEMPLATE.md"
    template_file.write_text(orig_template.read_text(encoding="utf-8"), encoding="utf-8")

    # Build lift and agreement reports
    task1 = TaskAttemptUnit(
        task_name="task-1",
        task_digest=None,
        trial_ids=["t1", "t2", "t3"],
        rewards=[0.0, 1.0, 0.0],
        exception_classes=[None, None, None],
        candidate_texts=["c0", "c1", "c2"],
    )
    task2 = TaskAttemptUnit(
        task_name="task-2",
        task_digest=None,
        trial_ids=["t4", "t5", "t6"],
        rewards=[1.0, 1.0, 1.0],
        exception_classes=[None, None, None],
        candidate_texts=["c0", "c1", "c2"],
    )
    lift_report = evaluate_selection_lift([task1, task2], AlwaysPassStubVerifier(), k=3)

    trials = [
        ("t1", 1.0, None, "task-1"),
        ("t2", 1.0, None, "task-1"),
        ("t3", 0.0, None, "task-1"),
        ("t4", None, "ValueError", "task-2"),
    ]
    agreement_report = evaluate_verifier_agreement(trials, AlwaysPassStubVerifier())

    dest, card_data = draft_verifier_calibration_card(
        lift_report,
        agreement_report,
        repo_root=tmp_path,
        title="test-verifier-calibration",
        is_stubbed=True,
    )

    assert dest.is_file()
    card_text = dest.read_text(encoding="utf-8")

    assert card_data["purpose"] == "calibration"
    assert "INJECTED STUB VERIFIER" in card_text
    assert "Zero tokens spent" in card_text or "zero tokens spent" in card_text
    assert "Cohen's Kappa" in card_text
    assert "Selected@k" in card_text or "selected@k" in card_text
