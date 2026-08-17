"""Contract freeze tests for E00.

Golden schemas committed so that any field add/rename/retype/reorder fails CI.
Regeneration script documented in docs/contracts.md.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab.schemas import (
    AnalysisRecord,
    CalibrationRecord,
    ConfidenceClaim,
    CriterionAgreement,
    EvidenceCitation,
    ObservationRecord,
    Suite,
    Verdict,
)

FIXTURES = Path(__file__).parent / "fixtures" / "contracts"


def _load_golden(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_golden_schemas_match_live():
    """Byte-for-byte golden freeze: changing any contract field fails this."""
    for Model, golden_name in [
        (Suite, "Suite"),
        (AnalysisRecord, "AnalysisRecord"),
        (ObservationRecord, "ObservationRecord"),
        (CalibrationRecord, "CalibrationRecord"),
        (Verdict, "Verdict"),
    ]:
        live = Model.model_json_schema()
        committed = _load_golden(golden_name)
        assert live == committed, f"{golden_name} schema drift"


def test_suite_frozen_immutable():
    """frozen_at set => mutation rejected (enforced in model)."""
    now = datetime.now(UTC)
    s = Suite(name="baseline", version="v1", frozen_at=now)
    assert s.frozen_at == now
    with pytest.raises(ValueError, match="frozen Suite is immutable"):
        s.name = "other"

def test_roundtrip_all_models():
    """Valid instances survive model_dump -> model_validate unchanged."""
    now = datetime.now(UTC)

    suite = Suite(name="test", version="1", members=["task@1"], frozen_at=now)
    assert Suite.model_validate(suite.model_dump()) == suite

    analysis = AnalysisRecord(
        analysis_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        trial_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        rubric_digest="sha256:" + "a" * 64,
        model="gpt-4o",
        category="failure",
        evidence=[EvidenceCitation(path="result.json", step=3)],
        confidence=ConfidenceClaim(level="high", n=10, interval=(0.8, 0.95)),
    )
    assert AnalysisRecord.model_validate(analysis.model_dump()) == analysis

    obs = ObservationRecord(
        trial_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        trial_name="t1",
        job="j1",
        agent="oracle",
        model="gpt",
        task="event-summary@1",
        reward=1.0,
        steps_taken=5,
        first_failure_step="none",
        loop_detected="no",
        loop_step="none",
        verified_before_done="no",
        tool_errors=0,
        summary="ok",
        evidence_files="result.json",
    )
    assert ObservationRecord.model_validate(obs.model_dump()) == obs

    calib = CalibrationRecord(
        calib_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        judge_model="gpt-4o",
        rubric_digest="sha256:" + "b" * 64,
        corpus_digest="sha256:" + "c" * 64,
        per_criterion_agreement={
            "correctness": CriterionAgreement(agreements=8, total=10, rate=0.8)
        },
        date=now,
    )
    assert CalibrationRecord.model_validate(calib.model_dump()) == calib

    verdict = Verdict(
        discovery_id="D-20260815-KTXJSHGZ",
        status="accepted",
        by="peter",
        at=now,
        note="solid",
    )
    assert Verdict.model_validate(verdict.model_dump()) == verdict


@pytest.mark.parametrize(
    "bad_id",
    [
        "not-a-ulid",
        "01ARZ3NDEKTSV4RRFFQ69G5FA",  # too short
        "81ARZ3NDEKTSV4RRFFQ69G5FAV",  # invalid first char
        "01ARZ3NDEKTSV4RRFFQ69G5FAV-extra",
    ],
)
def test_ulid_rejection(bad_id):
    """Non-ULID ids are rejected on construction for every id field."""
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="identifier must be ULID"):
        AnalysisRecord(
            analysis_id=bad_id,
            trial_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            rubric_digest="sha256:" + "a" * 64,
            model="m",
            category="c",
            confidence=ConfidenceClaim(level="low"),
        )
    with pytest.raises(ValueError, match="identifier must be ULID"):
        ObservationRecord(
            trial_id=bad_id,
            trial_name="t",
            job="j",
            agent="a",
            task="task@1",
            reward=0,
            steps_taken=0,
            summary="",
        )
    with pytest.raises(ValueError, match="identifier must be ULID"):
        CalibrationRecord(
            calib_id=bad_id,
            judge_model="m",
            rubric_digest="sha256:" + "a" * 64,
            corpus_digest="sha256:" + "b" * 64,
            per_criterion_agreement={},
            date=now,
        )

@pytest.mark.parametrize(
    "bad_id",
    [
        "not-a-discovery-id",
        "01ARZ3NDEKTSV4RRFFQ69G5FAV",  # bare ULID
        "D-2026081-KTXJSHGZ",  # date too short (7 digits)
        "D-202608151-KTXJSHGZ",  # date too long (9 digits)
        "D-20260815-",  # empty suffix
        "20260815-KTXJSHGZ",  # missing D- prefix
        "D-20260815",  # missing suffix
        "D-20260815-foo!",  # invalid char in suffix
    ],
)
def test_discovery_id_rejection(bad_id: str) -> None:
    """Malformed discovery IDs rejected on Verdict construction."""
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        Verdict(
            discovery_id=bad_id,
            status="pending",
            by="peter",
            at=now,
        )



@pytest.mark.parametrize(
    "bad_digest",
    [
        "sha256:deadbeef",
        "notsha:" + "a" * 64,
        "sha256:" + "A" * 64,  # upper
        "sha256:" + "g" * 64,  # invalid hex
    ],
)
def test_digest_rejection(bad_digest):
    """Unprefixed or malformed digests rejected."""
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="digest must be sha256"):
        AnalysisRecord(
            analysis_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            trial_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            rubric_digest=bad_digest,
            model="m",
            category="c",
            confidence=ConfidenceClaim(level="low"),
        )
    with pytest.raises(ValueError, match="digest must be sha256"):
        CalibrationRecord(
            calib_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            judge_model="m",
            rubric_digest=bad_digest,
            corpus_digest="sha256:" + "a" * 64,
            per_criterion_agreement={},
            date=now,
        )


def test_status_rejection():
    """status outside the literal set rejected."""
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        Verdict(
            discovery_id="D-20260815-KTXJSHGZ",
            status="maybe",  # type: ignore[arg-type]
            by="x",
            at=now,
        )


def test_frozen_suite_rejects_mutation_after_construction():
    """Explicit test that frozen instance cannot be mutated."""
    now = datetime.now(UTC)
    s = Suite(name="s", version="1", frozen_at=now)
    with pytest.raises(ValueError, match="frozen Suite is immutable"):
        s.version = "2"


def test_observation_factual_fields_roundtrip():
    """ObservationRecord accepts the exact factual field list from TEMPLATE.md."""
    obs = ObservationRecord(
        trial_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        trial_name="event-summary__foo",
        job="job1",
        agent="nop",
        model=None,
        task="event-summary@1",
        reward=0.0,
        steps_taken=0,
        first_failure_step="none",
        loop_detected="no",
        loop_step="none",
        verified_before_done="no",
        tool_errors=0,
        summary="Nop finished",
        evidence_files="result.json,verifier/reward.json",
    )
    dumped = obs.model_dump()
    assert dumped["template_version"] == "observatory-1"
    assert dumped["reward"] == 0.0
    assert ObservationRecord.model_validate(dumped) == obs
