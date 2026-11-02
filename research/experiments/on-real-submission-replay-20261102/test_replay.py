"""Unit tests for A8B Real Submission Replay and Control Synthesis."""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXP_DIR = Path(__file__).resolve().parent
RECEIPT_PATH = EXP_DIR / "replay_receipt.json"


def test_replay_receipt_integrity() -> None:
    assert RECEIPT_PATH.exists(), f"Missing receipt at {RECEIPT_PATH}"
    data = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))

    summary = data["summary"]
    assert summary["schema_version"] == "a8b.real_submission_replay/v1"
    assert summary["controls"]["all_references_passed"] is True
    assert summary["controls"]["reference_control_pass_rate"] == "8/8 = 100.0%"

    # Check cohort counts
    cohort = summary["cohort"]
    assert cohort["certified_tasks_count"] == 8
    assert cohort["real_submissions_count"] == 9
    assert cohort["known_equivalent_controls_count"] == 24
    assert cohort["total_cohort_count"] == 33

    # Check metrics
    metrics = summary["metrics"]
    real_flip = metrics["real_flip_rate"]
    assert real_flip["k"] == 5
    assert real_flip["n"] == 9
    assert 0.25 <= real_flip["ci"][0] <= 0.30
    assert 0.80 <= real_flip["ci"][1] <= 0.85

    ctrl_flip = metrics["control_false_reject_rate"]
    assert ctrl_flip["k"] == 0
    assert ctrl_flip["n"] == 24
    assert ctrl_flip["rate"] == 0.0

    combined = metrics["combined_reject_rate"]
    assert combined["k"] == 5
    assert combined["n"] == 33

    # Check adjudication
    adj = metrics["adjudication"]
    assert adj["total_adjudicated"] == 5
    assert adj["paired_sample_size"] == 5
    assert adj["raw_agreement_rate"] == 0.8
    assert adj["verdicts_gemini"]["wrong"] == 2
    assert adj["verdicts_gemini"]["correct_too_strict"] == 3
    assert adj["verdicts_gemini"]["unclear"] == 0


def test_receipt_lists_completeness() -> None:
    data = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
    assert len(data["real_submissions"]) == 9
    assert len(data["controls"]) == 24
    assert len(data["adjudications"]) == 5

    # Check that all 9 real submissions have original_reward == 1.0
    for s in data["real_submissions"]:
        assert s["original_reward"] == 1.0
        assert s["task_id"] in (
            "format-code-task-000838",
            "format-code-task-002391",
            "format-code-task-002552",
        )

    # Check that all 24 controls have original_reward == 1.0
    for c in data["controls"]:
        assert c["original_reward"] == 1.0
        assert c["gold_reward"] == 1.0
        assert c["flipped"] is False
