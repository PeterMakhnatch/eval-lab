"""Focused Track C descriptor-contract tests; all inputs are local fixtures."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

import evallab.curriculum_candidates as curriculum
from evallab.artifact_authority import (
    VERIFIER_IMPLEMENTATION_DIGEST,
    ArchiveAnchor,
    ArtifactAuthority,
    ArtifactRef,
    compute_authority_digest,
    verify_artifact,
)
from evallab.evidence_store import archive_evidence
from evallab.interpretation.capability_deficits import (
    CapabilityDeficitArtifact,
    CapabilityDeficitArtifactReceipt,
    CapabilityDeficitOutputExpectation,
    TraceCapabilityMeasures,
    mine_capability_deficit,
)
from evallab.schemas import (
    NETWORK_ESCAPE_CLASSES,
    Digest,
    NetworkEscapeProbeResultV1,
    NetworkIsolationProbeIdentityV1,
    NetworkIsolationRuntimeIdentityV1,
    NetworkPolicyEvidenceV1,
    TaskRuntimeIdentityV1,
    TrialSourceDigestsV1,
    TrialSourcePathsV1,
    build_network_isolation_evidence,
    build_trial_admissibility,
)

D_V = "sha256:" + "a1" * 32
D_E = "sha256:" + "b2" * 32
D_T = "sha256:" + "c3" * 32
D_X = "sha256:" + "ff" * 32
_KINDS = {
    "verifier/result.json": "verifier",
    "artifacts/app/output/benchmark-events.jsonl": "interpretation",
    "verifier/test-stdout.txt": "verifier",
    "tau3_runtime_state.json": "final_state",
    "result.json": "outcome",
}


def _spec() -> dict[str, Any]:
    return {
        "source_binding": {
            "job_id": "curriculum-fixture-job",
            "trial_id": "curriculum-fixture-trial",
            "benchmark_family": "tau3-bench",
            "task_id": "curriculum-fixture-task",
            "task_digest": D_T,
            "verifier_result_digest": D_V,
            "events_digest": D_E,
            "test_stdout_digest": D_V,
            "cluster_key": "curriculum-fixture-cluster",
            "split": "train",
        },
        "artifact_digests": {
            "verifier/result.json": D_V,
            "artifacts/app/output/benchmark-events.jsonl": D_E,
            "tau3_runtime_state.json": D_T,
            "result.json": D_V,
            "verifier/test-stdout.txt": D_V,
        },
        "verifier": {
            "status": "mismatch",
            "reward": 0.0,
            "tau2_evaluation": True,
            "reward_basis": ["DB"],
            "reward_breakdown": {"DB": 0.0},
            "reason": "incomplete_or_reordered_context_retrieval",
        },
        "retrieval": {
            "required_reads": 7,
            "observed_reads": 7,
            "actual_read_order_matches_required": False,
        },
        "tool_call_sequence": [],
        "capture": {
            "capture_status": "captured",
            "environment_integrity": "declared",
            "trial_admissible": True,
            "llm_calls_recorded": 10,
            "tool_calls_recorded": 55,
        },
    }


def _isolation_evidence():
    policy = NetworkPolicyEvidenceV1(mode="no-network")
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    return build_network_isolation_evidence(
        requested_agent_policy=policy,
        effective_agent_policy=policy,
        requested_verifier_policy=policy,
        effective_verifier_policy=policy,
        requested_verifier_phase_policy=policy,
        effective_verifier_phase_policy=policy,
        runtime_identity=NetworkIsolationRuntimeIdentityV1(
            platform_system="Linux",
            platform_release="test",
            platform_machine="arm64",
            container_runtime="docker",
            container_runtime_version="29.4.1",
            container_image_digest=Digest(D_T),
            adapter="test-adapter",
            adapter_version="1",
            adapter_digest=Digest(D_V),
        ),
        probe_identity=NetworkIsolationProbeIdentityV1(
            implementation="test-probe",
            implementation_version="1",
            implementation_digest=Digest(D_E),
            config_digest=Digest(D_X),
        ),
        probe_results=tuple(
            NetworkEscapeProbeResultV1(
                escape_class=escape_class,
                target=f"http://target.invalid/{escape_class}",
                outcome="blocked",
                detail="blocked",
            )
            for escape_class in NETWORK_ESCAPE_CLASSES
        ),
        observed_at=now,
        valid_until=now + timedelta(days=7),
        evaluated_at=now,
    )


def real_track_b_receipt(
    tmp_path,
) -> tuple[CapabilityDeficitArtifactReceipt, CapabilityDeficitOutputExpectation]:
    """Mine and externally archive a Track B artifact from fixture bytes."""
    spec = _spec()
    unverified = mine_capability_deficit(spec)
    cited = sorted(
        {
            item.source_path
            for item in (*unverified.evidence, *unverified.counterevidence)
        }
    )
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_digests: dict[str, str] = {}
    for path in cited:
        raw = f"fixture bytes for {path}\n".encode()
        destination = source_dir / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        source_digests[path] = digest
        spec["artifact_digests"][path] = digest
        if path == "verifier/result.json":
            spec["source_binding"]["verifier_result_digest"] = digest
        elif path == "artifacts/app/output/benchmark-events.jsonl":
            spec["source_binding"]["events_digest"] = digest
        elif path == "verifier/test-stdout.txt":
            spec["source_binding"]["test_stdout_digest"] = digest

    record = build_trial_admissibility(
        trial_id=spec["source_binding"]["trial_id"],
        task_runtime_identity=TaskRuntimeIdentityV1(
            task_id=spec["source_binding"]["task_id"],
            task_version="1",
            registry_record_digest=Digest(D_T),
            certified_runtime_package_digest=Digest(D_V),
            registry_admission_state="registered",
        ),
        source_digests=TrialSourceDigestsV1(
            contract=Digest(D_T),
            trajectory=Digest(D_T),
            final_state=Digest(D_T),
            verifier=Digest(source_digests.get("verifier/result.json", D_V)),
            outcome=Digest(D_T),
            interpretation=Digest(
                source_digests.get(
                    "artifacts/app/output/benchmark-events.jsonl", D_E
                )
            ),
        ),
        source_paths=TrialSourcePathsV1(
            contract=("benchmark_contract.json",),
            trajectory=("trajectory.json",),
            final_state=("final-state.json",),
            verifier=tuple(path for path in cited if _KINDS[path] == "verifier")
            or ("verifier/result.json",),
            outcome=("outcome.json",),
            interpretation=tuple(
                path for path in cited if _KINDS[path] == "interpretation"
            )
            or ("interpretation.json",),
        ),
        network_isolation_evidence=_isolation_evidence(),
        evaluated_at=datetime(2026, 9, 3, 12, tzinfo=UTC),
    )
    record_path = "admissibility/trial-admissibility.json"
    record_bytes = (
        json.dumps(
            record.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        + b"\n"
    )
    destination = source_dir / record_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(record_bytes)
    store_root = tmp_path / "store"
    archive = archive_evidence(
        source_dir,
        store_root,
        record_id=f"trial-{record.trial_id}",
        kind="job",
    )
    base_anchor = {
        "record_kind": "job",
        "record_id": f"trial-{record.trial_id}",
        "expected_record_digest": archive.record_digest,
        "expected_content_digest": archive.content_digest,
    }
    record_ref = ArtifactRef(
        ref=record_path,
        digest="sha256:" + hashlib.sha256(record_bytes).hexdigest(),
    )
    record_authority = verify_artifact(
        record_ref,
        minimum_level="bytes-verified",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        anchor=ArchiveAnchor(**base_anchor, inner_path=record_path),
        store_root=store_root,
    )
    assert isinstance(record_authority, ArtifactAuthority)
    authorities = []
    for path in cited:
        authority = verify_artifact(
            ArtifactRef(ref=path, digest=source_digests[path]),
            minimum_level="bytes-verified",
            verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
            anchor=ArchiveAnchor(**base_anchor, inner_path=path),
            admissibility=record,
            artifact_kind=_KINDS[path],
            store_root=store_root,
        )
        assert isinstance(authority, ArtifactAuthority)
        authorities.append(authority.model_dump(mode="json"))
    spec["evidence_authorities"] = authorities
    spec["admissibility_record_authority"] = record_authority.model_dump(mode="json")
    artifact = mine_capability_deficit(spec, authority_store_root=store_root)
    output_path = "generated/capability-deficit-artifact.json"
    output_bytes = (
        json.dumps(
            artifact.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        + b"\n"
    )
    output_dir = tmp_path / "generated"
    destination = output_dir / output_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(output_bytes)
    output_archive = archive_evidence(
        output_dir,
        store_root,
        record_id="generated-capability-deficit",
        kind="generated",
    )
    artifact_authority = verify_artifact(
        ArtifactRef(
            ref=output_path,
            digest="sha256:" + hashlib.sha256(output_bytes).hexdigest(),
        ),
        minimum_level="bytes-verified",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        anchor=ArchiveAnchor(
            record_kind="generated",
            record_id="generated-capability-deficit",
            expected_record_digest=output_archive.record_digest,
            expected_content_digest=output_archive.content_digest,
            inner_path=output_path,
        ),
        store_root=store_root,
    )
    assert isinstance(artifact_authority, ArtifactAuthority)
    receipt = CapabilityDeficitArtifactReceipt(
        artifact=artifact,
        artifact_authority=artifact_authority,
    )
    return (
        receipt,
        CapabilityDeficitOutputExpectation(
            artifact_content_digest=receipt.artifact.content_digest,
            artifact_bytes_digest=receipt.artifact_authority.artifact.digest,
            anchor=receipt.artifact_authority.anchor,
        ),
    )


def _trusted_parent_outputs(
    receipt: CapabilityDeficitArtifactReceipt,
    expectation: CapabilityDeficitOutputExpectation,
) -> dict[Digest, CapabilityDeficitOutputExpectation]:
    return {receipt.artifact.content_digest: expectation}


def _synthesize_real(tmp_path) -> curriculum.SynthesisResult:
    receipt, expectation = real_track_b_receipt(tmp_path)
    return curriculum.synthesize_curriculum_candidates(
        [receipt],
        trusted_parent_outputs=_trusted_parent_outputs(receipt, expectation),
        authority_store_root=tmp_path / "store",
    )

def _trace(family: str, **measure: Any) -> TraceCapabilityMeasures:
    present = {"status": "PRESENT", "value": 0.5, "numerator": 1, "denominator": 2}
    present.update(measure)
    return TraceCapabilityMeasures.model_validate(
        {"family": family, "cov": present, "er_minus": present, "er_plus": present, "delta": present}
    )


def _rehash_outer(raw: dict[str, Any]) -> dict[str, Any]:
    raw["content_digest"] = curriculum._domain_digest(
        "artifact", {key: value for key, value in raw.items() if key != "content_digest"}
    )
    return raw


def _rehash_track_b_parent(raw: dict[str, Any]) -> dict[str, Any]:
    content = {key: value for key, value in raw.items() if key != "content_digest"}
    raw["content_digest"] = "sha256:" + hashlib.sha256(
        b"evallab.capability-deficit.v1\x00"
        + json.dumps(
            content,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return raw




def _rehash_candidate(candidate: dict[str, Any]) -> None:
    candidate["candidate_id"] = curriculum._domain_digest(
        "candidate", {key: value for key, value in candidate.items() if key != "candidate_id"}
    )


def _rehash_pair(pair: dict[str, Any]) -> None:
    pair["contrast_pair_id"] = curriculum._domain_digest(
        "contrast-pair",
        {
            "twin_pair_id": pair["twin_pair_id"],
            "candidate_ids": pair["candidate_ids"],
            "one_variable_delta": pair["one_variable_delta"],
        },
    )


def _rehash_candidate_and_pair(raw: dict[str, Any], candidate_index: int = 0) -> dict[str, Any]:
    candidate = raw["candidates"][candidate_index]
    prior_id = candidate["candidate_id"]
    _rehash_candidate(candidate)
    for pair in raw["contrast_pairs"]:
        if prior_id in pair["candidate_ids"]:
            pair["candidate_ids"] = [
                candidate["candidate_id"] if identifier == prior_id else identifier for identifier in pair["candidate_ids"]
            ]
            _rehash_pair(pair)
            break
    return _rehash_outer(raw)


def test_real_track_b_receipt_to_track_c_round_trip(tmp_path) -> None:
    receipt, expectation = real_track_b_receipt(tmp_path)
    result = curriculum.synthesize_curriculum_candidates(
        [receipt],
        trusted_parent_outputs=_trusted_parent_outputs(receipt, expectation),
        seed=7,
        authority_store_root=tmp_path / "store",
    )
    assert len(result.candidates) == len(result.contrast_pairs) * 2 == 2
    rehydrated = curriculum.rehydrate_curriculum_artifact(json.loads(result.model_dump_json()))
    assert rehydrated.content_digest == result.content_digest
    for candidate in rehydrated.candidates:
        assert candidate.provenance.parent_artifact == receipt.artifact
        assert candidate.provenance.parent_artifact_authority == receipt.artifact_authority
        assert candidate.provenance.parent_output_expectation == expectation
        assert candidate.provenance.parent_evidence == receipt.artifact.evidence
        assert candidate.provenance.parent_counterevidence == receipt.artifact.counterevidence
        assert candidate.provenance.parent_authority_not_transferred is True
        assert candidate.descriptor_only and candidate.fixture_only
        assert candidate.status == "quarantined"
        assert candidate.training_eligible is False
        assert candidate.authority_scope == "priority_only_never_general"


def test_missing_trusted_parent_output_fails_closed(tmp_path) -> None:
    receipt, _ = real_track_b_receipt(tmp_path)
    result = curriculum.synthesize_curriculum_candidates(
        [receipt],
        trusted_parent_outputs={},
        authority_store_root=tmp_path / "store",
    )

    assert result.candidates == ()
    assert [refusal.reason_code for refusal in result.refusals] == [
        "parent_authority_unverified"
    ]


def test_malformed_trusted_parent_output_fails_closed(tmp_path) -> None:
    receipt, _ = real_track_b_receipt(tmp_path)
    result = curriculum.synthesize_curriculum_candidates(
        [receipt],
        trusted_parent_outputs={receipt.artifact.content_digest: {"anchor": {}}},
        authority_store_root=tmp_path / "store",
    )

    assert result.candidates == ()
    assert [refusal.reason_code for refusal in result.refusals] == [
        "parent_authority_unverified"
    ]


def test_mismatched_trusted_parent_output_fails_closed(tmp_path) -> None:
    receipt, expectation = real_track_b_receipt(tmp_path)
    mismatched_expectation = expectation.model_copy(
        update={"artifact_content_digest": D_X}
    )
    result = curriculum.synthesize_curriculum_candidates(
        [receipt],
        trusted_parent_outputs={
            receipt.artifact.content_digest: mismatched_expectation
        },
        authority_store_root=tmp_path / "store",
    )

    assert result.candidates == ()
    assert [refusal.reason_code for refusal in result.refusals] == [
        "parent_authority_unverified"
    ]


def test_bare_or_malformed_parent_input_is_a_typed_refusal(tmp_path) -> None:
    receipt, expectation = real_track_b_receipt(tmp_path)
    invalid_digest = receipt.model_dump(mode="json")
    invalid_digest["artifact"]["content_digest"] = "sha256:" + "0" * 64
    lossy = receipt.model_dump(mode="json")
    lossy["artifact"]["evidence"] = []
    result = curriculum.synthesize_curriculum_candidates(
        [
            invalid_digest,
            lossy,
            receipt.artifact,
            receipt.artifact.model_dump(mode="json"),
            receipt.artifact_authority.model_dump(mode="json"),
            {"artifact": receipt.artifact.model_dump(mode="json"), "verify": True},
            True,
            lambda: receipt,
        ],  # type: ignore[list-item]
        trusted_parent_outputs=_trusted_parent_outputs(receipt, expectation),
    )
    assert [refusal.reason_code for refusal in result.refusals] == [
        "invalid_parent_artifact",
        "invalid_parent_artifact",
        "invalid_parent_artifact",
        "invalid_parent_artifact",
        "invalid_parent_artifact",
        "invalid_parent_artifact",
        "invalid_parent_artifact",
        "invalid_parent_artifact",
    ]


@pytest.mark.parametrize("mutation", ["family_and_dimensions", "evidence_detail"])
def test_live_verification_rejects_rehashed_inner_artifact_mutation(
    tmp_path, mutation: str
) -> None:
    receipt, expectation = real_track_b_receipt(tmp_path)
    forged = receipt.model_dump(mode="json")
    if mutation == "family_and_dimensions":
        forged["artifact"]["family"] = "blind-retry"
        forged["artifact"]["proposed_intervention_dimensions"] = ["retry_policy"]
    else:
        forged["artifact"]["evidence"][0]["detail"] = "forged evidence detail"
    forged["artifact"] = _rehash_track_b_parent(forged["artifact"])

    structurally_valid_artifact = CapabilityDeficitArtifact.model_validate(
        forged["artifact"]
    )
    structurally_valid_receipt = CapabilityDeficitArtifactReceipt.model_validate(forged)
    assert structurally_valid_receipt.artifact == structurally_valid_artifact
    assert structurally_valid_receipt.artifact_authority == receipt.artifact_authority
    result = curriculum.synthesize_curriculum_candidates(
        [structurally_valid_receipt],
        trusted_parent_outputs=_trusted_parent_outputs(receipt, expectation),
        authority_store_root=tmp_path / "store",
    )

    assert result.candidates == ()
    assert [refusal.reason_code for refusal in result.refusals] == [
        "parent_authority_unverified"
    ]


def test_rearchived_reissued_parent_cannot_replace_trusted_output(tmp_path) -> None:
    publisher_receipt, expectation = real_track_b_receipt(tmp_path)
    forged_body = publisher_receipt.artifact.model_dump(mode="json")
    forged_body["family"] = "wrong-binding-or-addressing"
    forged_body["proposed_intervention_dimensions"] = [
        "retrieval_addressing",
        "output_format_contract",
    ]
    forged_artifact = CapabilityDeficitArtifact.model_validate(
        _rehash_track_b_parent(forged_body)
    )

    artifact_path = publisher_receipt.artifact_authority.artifact.ref
    forged_source = tmp_path / "attacker-output"
    forged_file = forged_source / artifact_path
    forged_file.parent.mkdir(parents=True)
    forged_bytes = (
        json.dumps(
            forged_artifact.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        + b"\n"
    )
    forged_file.write_bytes(forged_bytes)
    forged_archive = archive_evidence(
        forged_source,
        tmp_path / "store",
        record_id="attacker-capability-deficit-reissue",
        kind="generated",
    )
    forged_authority = verify_artifact(
        ArtifactRef(
            ref=artifact_path,
            digest="sha256:" + hashlib.sha256(forged_bytes).hexdigest(),
        ),
        minimum_level="bytes-verified",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        anchor=ArchiveAnchor(
            record_kind="generated",
            record_id="attacker-capability-deficit-reissue",
            expected_record_digest=forged_archive.record_digest,
            expected_content_digest=forged_archive.content_digest,
            inner_path=artifact_path,
        ),
        store_root=tmp_path / "store",
    )
    assert isinstance(forged_authority, ArtifactAuthority)
    forged_receipt = CapabilityDeficitArtifactReceipt(
        artifact=forged_artifact,
        artifact_authority=forged_authority,
    )

    result = curriculum.synthesize_curriculum_candidates(
        [forged_receipt],
        trusted_parent_outputs=_trusted_parent_outputs(
            publisher_receipt, expectation
        ),
        authority_store_root=tmp_path / "store",
    )

    assert result.candidates == ()
    assert [refusal.reason_code for refusal in result.refusals] == [
        "parent_authority_unverified"
    ]


def test_live_verification_rejects_mutated_output_authority(tmp_path) -> None:
    receipt, expectation = real_track_b_receipt(tmp_path)
    authority = receipt.artifact_authority
    forged_ref = authority.artifact.model_copy(update={"digest": D_X})
    forged_authority = ArtifactAuthority(
        artifact=forged_ref,
        anchor=authority.anchor,
        admissibility_binding=authority.admissibility_binding,
        level=authority.level,
        verifier_implementation_digest=authority.verifier_implementation_digest,
        authority_digest=compute_authority_digest(
            forged_ref,
            authority.level,
            authority.verifier_implementation_digest,
            authority.anchor,
            authority.admissibility_binding,
        ),
    )
    forged = CapabilityDeficitArtifactReceipt(
        artifact=receipt.artifact,
        artifact_authority=forged_authority,
    )
    assert CapabilityDeficitArtifactReceipt.model_validate(
        forged.model_dump(mode="json")
    ) == forged
    result = curriculum.synthesize_curriculum_candidates(
        [forged],
        trusted_parent_outputs=_trusted_parent_outputs(receipt, expectation),
        authority_store_root=tmp_path / "store",
    )

    assert result.candidates == ()
    assert [refusal.reason_code for refusal in result.refusals] == [
        "parent_authority_unverified"
    ]

def test_recomputed_outer_digest_cannot_mask_candidate_id_mutation(tmp_path) -> None:
    result = _synthesize_real(tmp_path)
    raw = result.model_dump(mode="json")
    raw["candidates"][0]["candidate_id"] = "sha256:" + "f" * 64
    with pytest.raises(ValidationError, match="candidate identifier"):
        curriculum.rehydrate_curriculum_artifact(_rehash_outer(raw))


def test_pair_one_variable_violation_is_rejected_after_rehash(tmp_path) -> None:
    result = _synthesize_real(tmp_path)
    raw = result.model_dump(mode="json")
    first = raw["candidates"][0]
    first["spec"]["distractor_density"] = 0.99 if first["spec"]["distractor_density"] != 0.99 else 0.98
    first["twin"]["twin_id"] = curriculum._domain_digest(
        "twin",
        {
            "twin_pair_id": first["twin"]["twin_pair_id"],
            "arm": first["twin"]["arm"],
            "one_variable_delta": first["twin"]["one_variable_delta"],
            "spec": first["spec"],
        },
    )
    first["candidate_id"] = curriculum._domain_digest(
        "candidate", {key: value for key, value in first.items() if key != "candidate_id"}
    )
    pair = raw["contrast_pairs"][0]
    pair["candidate_ids"][0] = first["candidate_id"]
    pair["contrast_pair_id"] = curriculum._domain_digest(
        "contrast-pair",
        {"twin_pair_id": pair["twin_pair_id"], "candidate_ids": pair["candidate_ids"], "one_variable_delta": pair["one_variable_delta"]},
    )
    with pytest.raises(ValidationError, match="exactly its declared variable"):
        curriculum.rehydrate_curriculum_artifact(_rehash_outer(raw))




def test_rehashed_candidate_deficit_family_must_match_its_parent(tmp_path) -> None:
    result = _synthesize_real(tmp_path)
    raw = result.model_dump(mode="json")
    candidate = raw["candidates"][0]
    parent_family = candidate["provenance"]["parent_artifact"]["family"]
    candidate["deficit_family"] = next(
        family
        for families, _ in curriculum.TRANSFORM_ELIGIBILITY.values()
        for family in families
        if family != parent_family
    )
    candidate["trace_priority"]["family"] = candidate["deficit_family"]
    with pytest.raises(ValidationError, match="deficit family must match"):
        curriculum.rehydrate_curriculum_artifact(_rehash_candidate_and_pair(raw))


def test_rehashed_expected_capability_must_be_proposed_by_its_parent(tmp_path) -> None:
    result = _synthesize_real(tmp_path)
    raw = result.model_dump(mode="json")
    candidate = raw["candidates"][0]
    eligible_capabilities = curriculum.TRANSFORM_ELIGIBILITY[candidate["transform_id"]][1]
    candidate["expected_capability"] = next(
        capability for capability in eligible_capabilities if capability != candidate["expected_capability"]
    )
    with pytest.raises(ValidationError, match="expected capability must be proposed"):
        curriculum.rehydrate_curriculum_artifact(_rehash_candidate_and_pair(raw))


def test_rehashed_transform_must_remain_eligible_for_its_parent(tmp_path) -> None:
    result = _synthesize_real(tmp_path)
    raw = result.model_dump(mode="json")
    candidate = raw["candidates"][0]
    transform_id = next(
        transform_id for transform_id in curriculum.TRANSFORM_ELIGIBILITY if transform_id != candidate["transform_id"]
    )
    candidate["transform_id"] = transform_id
    candidate["twin"]["twin_pair_id"] = curriculum._domain_digest(
        "twin-pair",
        {
            "parent_deficit_digest": candidate["provenance"]["parent_deficit_digest"],
            "transform_id": transform_id,
            "seed": candidate["seed"],
        },
    )
    candidate["twin"]["one_variable_delta"] = (
        "authoritative_source_index"
        if transform_id == "funcdag_cross_source_conflict"
        else "permutation"
    )
    candidate["twin"]["twin_id"] = curriculum._domain_digest(
        "twin",
        {
            "twin_pair_id": candidate["twin"]["twin_pair_id"],
            "arm": candidate["twin"]["arm"],
            "one_variable_delta": candidate["twin"]["one_variable_delta"],
            "spec": candidate["spec"],
        },
    )
    prior_id = candidate["candidate_id"]
    _rehash_candidate(candidate)
    pair = raw["contrast_pairs"][0]
    pair["twin_pair_id"] = candidate["twin"]["twin_pair_id"]
    pair["one_variable_delta"] = candidate["twin"]["one_variable_delta"]
    pair["candidate_ids"] = [
        candidate["candidate_id"] if identifier == prior_id else identifier for identifier in pair["candidate_ids"]
    ]
    _rehash_pair(pair)
    with pytest.raises(ValidationError, match="not eligible for this transform"):
        curriculum.rehydrate_curriculum_artifact(_rehash_outer(raw))


def test_rehashed_spec_type_must_match_its_transform(tmp_path) -> None:
    result = _synthesize_real(tmp_path)
    raw = result.model_dump(mode="json")
    candidate = raw["candidates"][0]
    if candidate["transform_id"] == "funcdag_cross_source_conflict":
        candidate["spec"] = {
            "address_axes": ["primary_key", "secondary_key"],
            "permutation": [0, 1],
            "distractor_density": 0.2,
        }
    else:
        candidate["spec"] = {
            "entity_count": 2,
            "source_count": 2,
            "authoritative_source_index": 0,
            "conflict_axis": "value",
            "distractor_fields": [],
        }
    candidate["twin"]["twin_id"] = curriculum._domain_digest(
        "twin",
        {
            "twin_pair_id": candidate["twin"]["twin_pair_id"],
            "arm": candidate["twin"]["arm"],
            "one_variable_delta": candidate["twin"]["one_variable_delta"],
            "spec": candidate["spec"],
        },
    )
    with pytest.raises(ValidationError, match="requires an? .*Spec"):
        curriculum.rehydrate_curriculum_artifact(_rehash_candidate_and_pair(raw))


def test_rehashed_validation_plan_must_match_checked_in_plan(tmp_path) -> None:
    result = _synthesize_real(tmp_path)
    raw = result.model_dump(mode="json")
    candidate = raw["candidates"][0]
    candidate["validation_plan"]["hidden_verifier_plan"] = ("substituted_control",)
    with pytest.raises(ValidationError, match="validation plan must match"):
        curriculum.rehydrate_curriculum_artifact(_rehash_candidate_and_pair(raw))


def test_rehashed_outer_generator_digest_must_match_candidates_and_source(tmp_path) -> None:
    result = _synthesize_real(tmp_path)
    raw = result.model_dump(mode="json")
    raw["generator_implementation_digest"] = "sha256:" + "f" * 64
    with pytest.raises(ValidationError, match="curriculum artifact is not bound"):
        curriculum.rehydrate_curriculum_artifact(_rehash_outer(raw))


def test_rehashed_pair_members_must_remain_base_then_variant(tmp_path) -> None:
    result = _synthesize_real(tmp_path)
    raw = result.model_dump(mode="json")
    pair = raw["contrast_pairs"][0]
    pair["candidate_ids"] = list(reversed(pair["candidate_ids"]))
    _rehash_pair(pair)
    with pytest.raises(ValidationError, match="ordered base then variant"):
        curriculum.rehydrate_curriculum_artifact(_rehash_outer(raw))
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1])
def test_trace_rejects_nonfinite_or_out_of_range_values(value: float) -> None:
    with pytest.raises(ValueError):
        curriculum._validated_trace(_trace("complete-but-reordered", value=value))


def test_trace_rejects_na_with_numbers() -> None:
    invalid = TraceCapabilityMeasures.model_validate(
        {"family": "complete-but-reordered", "cov": {"status": "NA", "value": 0.5}, "er_minus": {"status": "NA"}, "er_plus": {"status": "NA"}, "delta": {"status": "NA"}}
    )
    with pytest.raises(ValueError, match="must not carry numeric"):
        curriculum._validated_trace(invalid)


def test_supported_empty_evidence_and_duplicate_parent_are_deterministic(tmp_path) -> None:
    receipt, expectation = real_track_b_receipt(tmp_path)
    empty = receipt.model_dump(mode="json")
    empty["artifact"]["evidence"] = []
    result = curriculum.synthesize_curriculum_candidates(
        [empty, receipt, receipt],
        trusted_parent_outputs=_trusted_parent_outputs(receipt, expectation),
        authority_store_root=tmp_path / "store",
    )
    assert [refusal.reason_code for refusal in result.refusals] == [
        "invalid_parent_artifact",
        "duplicate_parent_artifact",
    ]


def test_any_claim_of_training_authority_is_forbidden(tmp_path) -> None:
    result = _synthesize_real(tmp_path)
    raw = result.model_dump(mode="json")
    raw["training_eligible"] = True
    with pytest.raises(ValidationError):
        curriculum.rehydrate_curriculum_artifact(raw)
    candidate = result.candidates[0].model_dump(mode="json")
    candidate["training_eligible"] = True
    with pytest.raises(ValidationError):
        curriculum.SyntheticTaskCandidate.model_validate(candidate)
