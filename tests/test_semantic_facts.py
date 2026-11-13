from __future__ import annotations

import hashlib

import pytest

from evallab.semantic_facts import (
    CapabilityOpportunity,
    EvidenceCoverage,
    RetrievalFact,
)


def digest(value: str = "source") -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def opportunity(**overrides: object) -> CapabilityOpportunity:
    values: dict[str, object] = {
        "opportunity_id": "opp-1",
        "trial_id": "trial-1",
        "benchmark": "bench",
        "construct": "retrieval",
        "eligible": True,
        "required_evidence": ("source_id",),
        "missing_evidence": (),
        "source_ref": "runs/trial-1/trajectory.json",
        "source_digest": digest(),
        "provenance_kind": "mechanical",
    }
    values.update(overrides)
    return CapabilityOpportunity.model_validate(values)


def test_rows_require_valid_source_digest_and_retrieval_source_id() -> None:
    with pytest.raises(ValueError, match="source_digest"):
        opportunity(source_digest="not-a-digest")
    with pytest.raises(ValueError, match="exposed"):
        RetrievalFact(
            trial_id="trial-1",
            utilized_status=True,
            cited_evidence_ref="step:1",
            source_ref="runs/trial-1/trajectory.json",
            source_digest=digest(),
            provenance_kind="mechanical",
        )




def test_coverage_rejects_inconsistent_readiness() -> None:
    with pytest.raises(ValueError, match="analysis_ready"):
        EvidenceCoverage(
            trial_id="trial-1",
            benchmark="bench",
            construct="x",
            exposed=True,
            eligible=True,
            required_evidence=("a",),
            observed_evidence=(),
            missing_evidence=("a",),
            analysis_ready=True,
            source_ref="derived:coverage",
            source_digest=digest(),
            provenance_kind="derived",
        )
