"""Track C repair-graft tests: leak-scan results, trace acknowledgment, and
forged/reissued-parent regressions (p7 item 4 + wK:p6 F-8/F-10).

Reuses the acceptance-suite helpers so every parent is a real mined,
archived, authority-verified Track B artifact.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from test_curriculum_candidates import _trusted_parent_outputs, real_track_b_receipt

import evallab.curriculum_candidates as curriculum
from evallab.interpretation.capability_deficits import TraceCapabilityMeasures


def test_leak_scan_results_recorded_and_passing(tmp_path) -> None:
    receipt, expectation = real_track_b_receipt(tmp_path)
    result = curriculum.synthesize_curriculum_candidates(
        [receipt], trusted_parent_outputs=_trusted_parent_outputs(receipt, expectation),
        authority_store_root=tmp_path / "store",
    )
    assert result.candidates
    for candidate in result.candidates:
        outcomes = {item.check: item.result for item in candidate.leak_scan_results}
        assert set(outcomes.values()) == {"pass"}
        assert "no_hex_secrets" in outcomes and "no_digest_markers" in outcomes


def test_leak_scan_function_catches_secrets() -> None:
    scan = curriculum._leak_scan_results({"note": "sha256:" + "ab" * 32})
    assert any(item.result == "fail" for item in scan)


def test_forced_leak_failure_refuses_pair(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        curriculum,
        "_FORBIDDEN_SECRET_PATTERNS",
        (("always_fail", r"primary_key|entity_count"),),
    )
    receipt, expectation = real_track_b_receipt(tmp_path)
    result = curriculum.synthesize_curriculum_candidates(
        [receipt],
        trusted_parent_outputs=_trusted_parent_outputs(receipt, expectation),
        authority_store_root=tmp_path / "store",
    )
    assert result.candidates == ()
    assert [refusal.reason_code for refusal in result.refusals] == ["leak_scan_failed"]
    assert result.refusals[0].rank == 1


def test_missing_trace_is_explicit_neutral_default(tmp_path) -> None:
    receipt, expectation = real_track_b_receipt(tmp_path)
    result = curriculum.synthesize_curriculum_candidates(
        [receipt], trusted_parent_outputs=_trusted_parent_outputs(receipt, expectation),
        authority_store_root=tmp_path / "store",
    )
    assert result.candidates
    assert all(
        candidate.trace_acknowledgment == "neutral_default"
        for candidate in result.candidates
    )


def test_provided_trace_is_acknowledged(tmp_path) -> None:
    receipt, expectation = real_track_b_receipt(tmp_path)
    trace = TraceCapabilityMeasures.model_validate(
        {
            "family": receipt.artifact.family,
            "cov": {"status": "NA"},
            "er_minus": {"status": "NA"},
            "er_plus": {"status": "NA"},
            "delta": {"status": "NA"},
        }
    )
    result = curriculum.synthesize_curriculum_candidates(
        [receipt],
        {receipt.artifact.content_digest: trace},
        trusted_parent_outputs=_trusted_parent_outputs(receipt, expectation),
        authority_store_root=tmp_path / "store",
    )
    assert result.candidates
    assert all(
        candidate.trace_acknowledgment == "provided" for candidate in result.candidates
    )


def test_forged_parent_artifact_refuses(tmp_path) -> None:
    receipt, expectation = real_track_b_receipt(tmp_path)
    forged = receipt.artifact.model_dump(mode="json")
    forged["hold_reasons"] = ["attacker-injected"]
    forged_receipt = receipt.model_copy(update={"artifact": forged})
    result = curriculum.synthesize_curriculum_candidates(
        [forged_receipt], trusted_parent_outputs=_trusted_parent_outputs(receipt, expectation),
        authority_store_root=tmp_path / "store",
    )
    assert result.candidates == ()
    assert result.refusals[0].reason_code == "invalid_parent_artifact"


def test_reissued_parent_same_content_new_authority_refuses(tmp_path) -> None:
    """p7 item 4 regression: re-archived identical content under a different
    authority must fail live re-verification against the trusted expectation."""
    import hashlib

    from evallab.artifact_authority import (
        VERIFIER_IMPLEMENTATION_DIGEST,
        ArchiveAnchor,
        ArtifactRef,
        verify_artifact,
    )
    from evallab.evidence_store import archive_evidence

    receipt_a, expectation_a = real_track_b_receipt(tmp_path / "original")
    output_bytes = (
        json.dumps(
            receipt_a.artifact.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        + b"\n"
    )
    output_dir = tmp_path / "reissued" / "generated"
    output_dir.mkdir(parents=True)
    output_path = "capability-deficit-artifact.json"
    (output_dir / output_path).write_bytes(output_bytes)
    reissue_archive = archive_evidence(
        output_dir,
        tmp_path / "reissued" / "store",
        record_id="generated-capability-deficit-reissue",
        kind="generated",
    )
    authority_b = verify_artifact(
        ArtifactRef(
            ref=output_path,
            digest="sha256:" + hashlib.sha256(output_bytes).hexdigest(),
        ),
        minimum_level="bytes-verified",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        anchor=ArchiveAnchor(
            record_kind="generated",
            record_id="generated-capability-deficit-reissue",
            expected_record_digest=reissue_archive.record_digest,
            expected_content_digest=reissue_archive.content_digest,
            inner_path=output_path,
        ),
        store_root=tmp_path / "reissued" / "store",
    )
    receipt_b = type(receipt_a)(artifact=receipt_a.artifact, artifact_authority=authority_b)
    assert receipt_b.artifact.content_digest == receipt_a.artifact.content_digest
    assert receipt_b.artifact_authority.anchor != receipt_a.artifact_authority.anchor
    result = curriculum.synthesize_curriculum_candidates(
        [receipt_b],
        trusted_parent_outputs=_trusted_parent_outputs(receipt_a, expectation_a),
        authority_store_root=tmp_path / "original" / "store",
    )
    assert result.candidates == ()
    assert [refusal.reason_code for refusal in result.refusals] == [
        "parent_authority_unverified"
    ]


def test_missing_trusted_expectation_refuses(tmp_path) -> None:
    receipt, _expectation = real_track_b_receipt(tmp_path)
    result = curriculum.synthesize_curriculum_candidates(
        [receipt], trusted_parent_outputs={}
    )
    assert result.candidates == ()
    assert [refusal.reason_code for refusal in result.refusals] == [
        "parent_authority_unverified"
    ]


def test_candidate_mutation_refuses_rehydration(tmp_path) -> None:
    receipt, expectation = real_track_b_receipt(tmp_path)
    result = curriculum.synthesize_curriculum_candidates(
        [receipt], trusted_parent_outputs=_trusted_parent_outputs(receipt, expectation),
        authority_store_root=tmp_path / "store",
    )
    original = result.candidates[0].model_dump(mode="json")
    original["rank"] = 99
    with pytest.raises(ValidationError):
        curriculum.SynthesisResult.model_validate(
            {**result.model_dump(mode="json"), "candidates": [original]}
        )
