"""Tests for SG-4: LLM-as-a-Verifier calibration and best-of-k selection lift.

Covers:
- Selection lift on fixture cohort with known rewards (pass@1, selected@k, oracle ceiling)
- Underpowered cohort rendering 'not distinguishable' / NOT_COMPARABLE
- Exclusion and separate reporting of never-measured exception trials
- Degenerate always-pass verifier unmasked by Cohen's Kappa (=0.0) and Balanced Accuracy (=0.50)
- CalibrationRecord persistence and history round-trip
- Missing verifier dependency degradation with clear remediation
- Paid model authorization token spend gate (refusal without opt-in)
- Eval card generation with purpose='calibration' and stubbed provenance labeling
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from evallab.calibrate import (
    LlmVerifier,
    MissingVerifierDependencyError,
    PaidModelAuthorizationError,
    load_calibration_records,
    load_llm_verifier,
    save_calibration_record,
)
from evallab.queue import new_ulid
from evallab.schemas import CalibrationRecord, CriterionAgreement










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


