"""Focused tests for the Track B capability-deficit miner.

Fixture-only: positive cases materialize temporary bytes and require public
authority re-verification; no network, model, or external backend.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from evallab.artifact_authority import (
    VERIFIER_IMPLEMENTATION_DIGEST,
    AdmissibilityReceiptBinding,
    ArchiveAnchor,
    ArtifactAuthority,
    ArtifactRef,
    compute_authority_digest,
    verify_artifact,
)
from evallab.evidence_store import archive_evidence
from evallab.interpretation.capability_deficits import (
    CapabilityDeficitArtifact,
    CapabilityDeficitInput,
    mine_capability_deficit,
    reverify_capability_deficit_artifact,
    trace_capability_measures,
    trace_priority_order,
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


def _binding(**over):
    b = {
        "job_id": "job-1",
        "trial_id": "t1",
        "benchmark_family": "tau3-bench",
        "task_id": "task-001",
        "task_digest": D_T,
        "verifier_result_digest": D_V,
        "events_digest": D_E,
        "test_stdout_digest": D_V,
        "cluster_key": "ck-1",
    }
    b.update(over)
    return b


def _digests():
    return {
        "verifier/result.json": D_V,
        "artifacts/app/output/benchmark-events.jsonl": D_E,
        "tau3_runtime_state.json": D_T,
        "result.json": D_V,
        "verifier/test-stdout.txt": D_V,
    }


def _capture(**over):
    c = {
        "capture_status": "captured",
        "environment_integrity": "declared",
        "trial_admissible": True,
        "llm_calls_recorded": 10,
        "tool_calls_recorded": 55,
    }
    c.update(over)
    return c


def _spec(**over):
    spec = {
        "source_binding": _binding(**over.pop("binding", {})),
        "artifact_digests": _digests(),
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
        "capture": _capture(),
    }
    spec.update(over)
    return spec


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


def _with_reverified_authorities(tmp_path, spec, *, admissibility_trial_id=None, source_digest=None):
    """Archive cited bytes and reopen every authority through public APIs."""
    unverified = mine_capability_deficit(spec)
    cited = sorted({
        item.source_path
        for item in (*unverified.evidence, *unverified.counterevidence)
    })
    authored = json.loads(json.dumps(spec))
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    source_digests = {}
    for path in cited:
        raw = f"fixture bytes for {path}\n".encode()
        target = source_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        source_digests[path] = digest
        authored["artifact_digests"][path] = digest
        if path == "verifier/result.json":
            authored["source_binding"]["verifier_result_digest"] = digest
        elif path == "artifacts/app/output/benchmark-events.jsonl":
            authored["source_binding"]["events_digest"] = digest
        elif path == "verifier/test-stdout.txt":
            authored["source_binding"]["test_stdout_digest"] = digest

    trial_id = admissibility_trial_id or authored["source_binding"]["trial_id"]
    runtime = TaskRuntimeIdentityV1(
        task_id="task-001",
        task_version="1",
        registry_record_digest=Digest(D_T),
        certified_runtime_package_digest=Digest(D_V),
        registry_admission_state="registered",
    )
    record = build_trial_admissibility(
        trial_id=trial_id,
        task_runtime_identity=runtime,
        source_digests=TrialSourceDigestsV1(
            contract=Digest(D_T),
            trajectory=Digest(D_T),
            final_state=Digest(D_T),
            verifier=Digest(source_digests.get("verifier/result.json", D_V)),
            outcome=Digest(D_T),
            interpretation=Digest(
                source_digest
                or source_digests.get(
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
    record_bytes = json.dumps(
        record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode() + b"\n"
    (source_dir / record_path).parent.mkdir(parents=True, exist_ok=True)
    (source_dir / record_path).write_bytes(record_bytes)
    store_root = tmp_path / "store"
    archive = archive_evidence(
        source_dir, store_root, record_id=f"trial-{trial_id}", kind="job"
    )
    base_anchor = {
        "record_kind": "job",
        "record_id": f"trial-{trial_id}",
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
        ref = ArtifactRef(ref=path, digest=source_digests[path])
        anchor = ArchiveAnchor(**base_anchor, inner_path=path)
        authority = verify_artifact(
            ref,
            minimum_level="bytes-verified",
            verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
            anchor=anchor,
            admissibility=record,
            artifact_kind=_KINDS[path],
            store_root=store_root,
        )
        if not isinstance(authority, ArtifactAuthority) and source_digest is None:
            pytest.fail(f"fixture authority verification refused: {authority}")
        if not isinstance(authority, ArtifactAuthority):
            receipt = AdmissibilityReceiptBinding(
                trial_id=record.trial_id,
                admissibility_digest=record.admissibility_digest,
                artifact_kind=_KINDS[path],
            )
            authority = ArtifactAuthority(
                artifact=ref,
                anchor=anchor,
                admissibility_binding=receipt,
                level="bytes-verified",
                verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
                authority_digest=compute_authority_digest(
                    ref,
                    "bytes-verified",
                    VERIFIER_IMPLEMENTATION_DIGEST,
                    anchor,
                    receipt,
                ),
            )
        authorities.append(authority.model_dump(mode="json"))
    authored["evidence_authorities"] = authorities
    authored["admissibility_record_authority"] = record_authority.model_dump(mode="json")
    return authored


def test_self_attested_capture_cannot_support_a_deficit():
    art = mine_capability_deficit(_spec())
    assert art.family == "unclassified"
    assert art.attribution_gate == "unattributable"
    assert "positive_authority_unverified" in art.hold_reasons


def test_reverified_fixture_can_support_a_deficit(tmp_path):
    spec = _with_reverified_authorities(tmp_path, _spec())
    art = mine_capability_deficit(spec, authority_store_root=tmp_path / "store")
    assert art.family == "complete-but-reordered"
    assert art.attribution_gate == "deficit_supported"
    assert art.proposed_intervention_dimensions == ("instruction_ordering_scaffold",)
    assert {authority.artifact.ref for authority in art.evidence_authorities} == {
        "artifacts/app/output/benchmark-events.jsonl",
        "verifier/result.json",
    }


def _assert_closed(spec, store_root):
    art = mine_capability_deficit(spec, authority_store_root=store_root)
    assert art.family == "unclassified"
    assert art.attribution_gate == "unattributable"
    assert "positive_authority_unverified" in art.hold_reasons


def test_record_authority_attacks_fail_closed(tmp_path):
    for mutation in ("omit", "fabricated_binding", "unanchored", "different_anchor"):
        case_root = tmp_path / mutation
        spec = _with_reverified_authorities(case_root, _spec())
        original = ArtifactAuthority.model_validate(spec["admissibility_record_authority"])
        if mutation == "omit":
            del spec["admissibility_record_authority"]
        elif mutation == "fabricated_binding":
            forged_binding = AdmissibilityReceiptBinding(
                trial_id="t1", admissibility_digest=D_X, artifact_kind="verifier"
            )
            spec["admissibility_record_authority"] = ArtifactAuthority(
                artifact=original.artifact,
                anchor=original.anchor,
                admissibility_binding=forged_binding,
                level=original.level,
                verifier_implementation_digest=original.verifier_implementation_digest,
                authority_digest=compute_authority_digest(
                    original.artifact,
                    original.level,
                    original.verifier_implementation_digest,
                    original.anchor,
                    forged_binding,
                ),
            ).model_dump(mode="json")
        elif mutation == "unanchored":
            spec["admissibility_record_authority"] = ArtifactAuthority(
                artifact=original.artifact,
                level=original.level,
                verifier_implementation_digest=original.verifier_implementation_digest,
                authority_digest=compute_authority_digest(
                    original.artifact,
                    original.level,
                    original.verifier_implementation_digest,
                ),
            ).model_dump(mode="json")
        else:
            source = ArtifactAuthority.model_validate(spec["evidence_authorities"][0])
            wrong_anchor = source.anchor.model_copy(update={"record_id": "other-record"})
            spec["evidence_authorities"][0] = ArtifactAuthority(
                artifact=source.artifact,
                anchor=wrong_anchor,
                admissibility_binding=source.admissibility_binding,
                level=source.level,
                verifier_implementation_digest=source.verifier_implementation_digest,
                authority_digest=compute_authority_digest(
                    source.artifact,
                    source.level,
                    source.verifier_implementation_digest,
                    wrong_anchor,
                    source.admissibility_binding,
                ),
            ).model_dump(mode="json")
        _assert_closed(spec, case_root / "store")


def test_mutated_admissibility_record_archive_fails_closed(tmp_path):
    spec = _with_reverified_authorities(tmp_path, _spec())
    record = ArtifactAuthority.model_validate(spec["admissibility_record_authority"])
    digest = record.anchor.expected_content_digest.removeprefix("sha256:")
    blob = tmp_path / "store" / "blobs" / "sha256" / digest[:2] / f"{digest}.tar.gz"
    blob.write_bytes(b"mutated after authority issuance")
    _assert_closed(spec, tmp_path / "store")

def test_wrong_record_trial_or_source_digest_fails_closed(tmp_path):
    wrong_trial = _with_reverified_authorities(
        tmp_path / "wrong-trial", _spec(), admissibility_trial_id="other-trial"
    )
    _assert_closed(wrong_trial, tmp_path / "wrong-trial" / "store")

    wrong_source = _with_reverified_authorities(
        tmp_path / "wrong-source", _spec(), source_digest=D_X
    )
    _assert_closed(wrong_source, tmp_path / "wrong-source" / "store")


def _rehash_artifact_body(body):
    content = {key: value for key, value in body.items() if key != "content_digest"}
    raw = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    body["content_digest"] = "sha256:" + hashlib.sha256(
        b"evallab.capability-deficit.v1\x00" + raw
    ).hexdigest()

def _reissue_authority(authority, *, artifact=None, anchor=None, receipt=None):
    artifact = authority.artifact if artifact is None else artifact
    anchor = authority.anchor if anchor is None else anchor
    receipt = authority.admissibility_binding if receipt is None else receipt
    return ArtifactAuthority(
        artifact=artifact,
        anchor=anchor,
        admissibility_binding=receipt,
        level=authority.level,
        verifier_implementation_digest=authority.verifier_implementation_digest,
        authority_digest=compute_authority_digest(
            artifact,
            authority.level,
            authority.verifier_implementation_digest,
            anchor,
            receipt,
        ),
    )


def _positive_artifact(tmp_path):
    spec = _with_reverified_authorities(tmp_path, _spec())
    return mine_capability_deficit(spec, authority_store_root=tmp_path / "store")


def test_live_verifier_accepts_mined_and_serialized_artifact(tmp_path):
    artifact = _positive_artifact(tmp_path)
    store_root = tmp_path / "store"

    assert reverify_capability_deficit_artifact(
        artifact, authority_store_root=store_root
    )
    assert reverify_capability_deficit_artifact(
        json.loads(artifact.model_dump_json()), authority_store_root=store_root
    )


def test_live_verifier_rejects_fully_rehashed_authority_mutations(tmp_path):
    def assert_structural_but_untrusted(body, store_root):
        _rehash_artifact_body(body)
        CapabilityDeficitArtifact.model_validate(body)
        assert not reverify_capability_deficit_artifact(
            body, authority_store_root=store_root
        )

    receipt_root = tmp_path / "receipt"
    receipt_artifact = _positive_artifact(receipt_root)
    receipt_body = receipt_artifact.model_dump(mode="json")
    receipt_index = 0
    receipt_authority = ArtifactAuthority.model_validate(
        receipt_body["evidence_authorities"][receipt_index]
    )
    bad_receipt = receipt_authority.admissibility_binding.model_copy(
        update={"admissibility_digest": D_X}
    )
    receipt_body["evidence_authorities"][receipt_index] = _reissue_authority(
        receipt_authority, receipt=bad_receipt
    ).model_dump(mode="json")
    assert_structural_but_untrusted(receipt_body, receipt_root / "store")

    record_root = tmp_path / "record"
    record_artifact = _positive_artifact(record_root)
    record_body = record_artifact.model_dump(mode="json")
    record_authority = ArtifactAuthority.model_validate(
        record_body["admissibility_record_authority"]
    )
    bad_record_ref = record_authority.artifact.model_copy(update={"digest": D_X})
    record_body["admissibility_record_authority"] = _reissue_authority(
        record_authority, artifact=bad_record_ref
    ).model_dump(mode="json")
    assert_structural_but_untrusted(record_body, record_root / "store")

    bytes_root = tmp_path / "bytes"
    bytes_artifact = _positive_artifact(bytes_root)
    bytes_body = bytes_artifact.model_dump(mode="json")
    bytes_record = ArtifactAuthority.model_validate(
        bytes_body["admissibility_record_authority"]
    )
    archive_digest = bytes_record.anchor.expected_content_digest.removeprefix("sha256:")
    archive_blob = (
        bytes_root
        / "store"
        / "blobs"
        / "sha256"
        / archive_digest[:2]
        / f"{archive_digest}.tar.gz"
    )
    archive_blob.write_bytes(b"mutated after artifact retention")
    assert_structural_but_untrusted(bytes_body, bytes_root / "store")

    anchor_root = tmp_path / "anchor"
    anchor_artifact = _positive_artifact(anchor_root)
    anchor_body = anchor_artifact.model_dump(mode="json")

    def with_bad_archive_coordinate(authority):
        anchor = authority.anchor.model_copy(update={"expected_content_digest": D_X})
        return _reissue_authority(authority, anchor=anchor).model_dump(mode="json")

    anchor_body["admissibility_record_authority"] = with_bad_archive_coordinate(
        ArtifactAuthority.model_validate(anchor_body["admissibility_record_authority"])
    )
    anchor_body["evidence_authorities"] = [
        with_bad_archive_coordinate(ArtifactAuthority.model_validate(authority))
        for authority in anchor_body["evidence_authorities"]
    ]
    assert_structural_but_untrusted(anchor_body, anchor_root / "store")

    ref_root = tmp_path / "ref"
    ref_artifact = _positive_artifact(ref_root)
    ref_body = ref_artifact.model_dump(mode="json")
    source_index = next(
        index
        for index, authority in enumerate(ref_body["evidence_authorities"])
        if authority["artifact"]["ref"] == "verifier/result.json"
    )
    source_authority = ArtifactAuthority.model_validate(
        ref_body["evidence_authorities"][source_index]
    )
    forged_ref = source_authority.artifact.model_copy(
        update={"ref": "verifier/test-stdout.txt"}
    )
    forged_anchor = source_authority.anchor.model_copy(
        update={"inner_path": forged_ref.ref}
    )
    ref_body["evidence_authorities"][source_index] = _reissue_authority(
        source_authority, artifact=forged_ref, anchor=forged_anchor
    ).model_dump(mode="json")
    evidence_index = next(
        index
        for index, evidence in enumerate(ref_body["evidence"])
        if evidence["source_path"] == "verifier/result.json"
    )
    ref_body["evidence"][evidence_index]["source_path"] = forged_ref.ref
    ref_body["source_binding"]["test_stdout_digest"] = forged_ref.digest
    assert_structural_but_untrusted(ref_body, ref_root / "store")



def test_positive_artifact_rehydration_requires_retained_record_authority(tmp_path):
    spec = _with_reverified_authorities(tmp_path, _spec())
    art = mine_capability_deficit(spec, authority_store_root=tmp_path / "store")

    missing = art.model_dump(mode="json")
    missing["admissibility_record_authority"] = None
    _rehash_artifact_body(missing)
    with pytest.raises(ValidationError):
        CapabilityDeficitArtifact.model_validate(missing)

    mismatched = art.model_dump(mode="json")
    record = ArtifactAuthority.model_validate(mismatched["admissibility_record_authority"])
    wrong_anchor = record.anchor.model_copy(update={"record_id": "other-record"})
    mismatched["admissibility_record_authority"] = ArtifactAuthority(
        artifact=record.artifact,
        anchor=wrong_anchor,
        level=record.level,
        verifier_implementation_digest=record.verifier_implementation_digest,
        authority_digest=compute_authority_digest(
            record.artifact,
            record.level,
            record.verifier_implementation_digest,
            wrong_anchor,
        ),
    ).model_dump(mode="json")
    _rehash_artifact_body(mismatched)
    with pytest.raises(ValidationError):
        CapabilityDeficitArtifact.model_validate(mismatched)

def test_wrong_admissibility_artifact_kind_cannot_support(tmp_path):
    spec = _with_reverified_authorities(tmp_path, _spec())
    original = ArtifactAuthority.model_validate(spec["evidence_authorities"][0])
    wrong_binding = original.admissibility_binding.model_copy(
        update={"artifact_kind": "outcome"}
    )
    spec["evidence_authorities"][0] = ArtifactAuthority(
        artifact=original.artifact,
        anchor=original.anchor,
        admissibility_binding=wrong_binding,
        level=original.level,
        verifier_implementation_digest=original.verifier_implementation_digest,
        authority_digest=compute_authority_digest(
            original.artifact,
            original.level,
            original.verifier_implementation_digest,
            original.anchor,
            wrong_binding,
        ),
    ).model_dump(mode="json")
    art = mine_capability_deficit(spec, authority_store_root=tmp_path / "store")
    assert art.family == "unclassified"
    assert art.attribution_gate == "unattributable"
    assert "positive_authority_unverified" in art.hold_reasons


def test_strict_input_rejects_unknown_and_missing():
    spec = _spec()
    spec["totally_unknown_key"] = 1
    with pytest.raises(ValidationError):
        mine_capability_deficit(spec)
    broken = _spec()
    del broken["source_binding"]
    with pytest.raises(ValidationError):
        mine_capability_deficit(broken)


def test_input_model_extra_forbid():
    payload = _spec()
    inp = CapabilityDeficitInput.model_validate(payload)
    assert inp.capture.trial_admissible is True
    with pytest.raises(ValidationError):
        CapabilityDeficitInput.model_validate({**payload, "nope": 1})


def test_explicit_non_evaluation_only():
    spec = _spec(
        verifier={
            "status": "mismatch",
            "reward": 0.0,
            "tau2_evaluation": False,
            "reward_basis": ["DB", "COMMUNICATE"],
            "reward_breakdown": None,
        },
    )
    art = mine_capability_deficit(spec)
    assert art.family == "unclassified"
    assert art.attribution_gate == "unattributable"
    assert "environment_non_evaluation" in art.hold_reasons


def test_scalar_zero_with_unknown_evaluator_is_not_non_evaluation():
    spec = _spec(
        verifier={
            "status": "mismatch",
            "reward": 0.0,
            "tau2_evaluation": None,
            "reward_basis": ["DB"],
            "reward_breakdown": None,
            "reason": "some_reason",
        },
        retrieval={},
    )
    art = mine_capability_deficit(spec)
    assert art.family == "unclassified"
    assert art.attribution_gate == "unattributable"
    assert "evaluator_status_unavailable" in art.hold_reasons
    assert "environment_non_evaluation" not in art.hold_reasons


def test_reward_only_is_unattributable_not_refuted():
    spec = _spec(
        verifier={
            "status": "passed",
            "reward": 1.0,
            "tau2_evaluation": None,
            "reward_breakdown": None,
        },
        retrieval={},
    )
    art = mine_capability_deficit(spec)
    assert art.family == "none"
    assert art.attribution_gate == "unattributable"
    assert "reward_only_without_semantic_evaluator" in art.hold_reasons
    assert art.attribution_gate != "deficit_refuted"


def test_agent_configured_environment_is_unattributable():
    spec = _spec(
        capture=_capture(environment_integrity="agent_configured"),
        verifier={
            "status": "passed",
            "reward": 1.0,
            "tau2_evaluation": True,
            "reward_basis": ["DB"],
            "reward_breakdown": {"DB": 1.0},
        },
    )
    art = mine_capability_deficit(spec)
    assert art.attribution_gate == "unattributable"
    assert "environment_agent_configured" in art.hold_reasons


def test_capture_loss_blocks_everything():
    art = mine_capability_deficit(
        _spec(capture=_capture(capture_status="capture_loss", trial_admissible=None))
    )
    assert art.family == "unclassified"
    assert "unattributable_capture_loss" in art.hold_reasons


def test_self_attested_pass_cannot_refute():
    spec = _spec(
        verifier={
            "status": "passed",
            "reward": 1.0,
            "tau2_evaluation": True,
            "reward_basis": ["DB"],
            "reward_breakdown": {"DB": 1.0},
        },
    )
    art = mine_capability_deficit(spec)
    assert art.family == "none"
    assert art.attribution_gate == "unattributable"
    assert "positive_authority_unverified" in art.hold_reasons


def test_reverified_pass_with_semantic_evidence_refutes(tmp_path):
    spec = _with_reverified_authorities(
        tmp_path,
        _spec(
            verifier={
                "status": "passed",
                "reward": 1.0,
                "tau2_evaluation": True,
                "reward_basis": ["DB"],
                "reward_breakdown": {"DB": 1.0},
            },
        ),
    )
    art = mine_capability_deficit(spec, authority_store_root=tmp_path / "store")
    assert art.family == "none"
    assert art.attribution_gate == "deficit_refuted"
    assert art.counterevidence


def test_varied_args_all_error_is_unclassified_not_blind():
    """Orchestrator ruling 2026-09-03: unsuccessful adaptation is not blind
    repetition. Varied-arg all-error runs stay unclassified with a typed hold,
    evidence fully retained for a future family definition."""
    calls_varied = [
        {"tool_name": "start_conversation", "arguments": {"q": "a"}, "is_error": True},
        {"tool_name": "start_conversation", "arguments": {"q": "b"}, "is_error": True},
        {"tool_name": "start_conversation", "arguments": {"q": "c"}, "is_error": True},
    ]
    art = mine_capability_deficit(_spec(retrieval={}, tool_call_sequence=calls_varied))
    assert art.family == "unclassified"
    assert art.attribution_gate == "unattributable"
    assert "varied_argument_retry_unclassified" in art.hold_reasons
    kinds = {e.kind for e in art.evidence}
    assert "varied_argument_failed_calls" in kinds  # evidence retained without blind label
    assert "repeated_identical_failed_call" not in kinds


def test_blind_retry_requires_every_call_in_run_errored():
    calls = [
        {"tool_name": "start_conversation", "arguments": {}, "is_error": False},
        {"tool_name": "start_conversation", "arguments": {}, "is_error": False},
        {"tool_name": "start_conversation", "arguments": {}, "is_error": True},
    ]
    art = mine_capability_deficit(_spec(retrieval={}, tool_call_sequence=calls))
    assert art.family != "blind-retry"
    assert art.attribution_gate == "unattributable"

    calls_ok = [
        {"tool_name": "start_conversation", "arguments": {}, "is_error": True},
        {"tool_name": "start_conversation", "arguments": {}, "is_error": True},
        {"tool_name": "start_conversation", "arguments": {}, "is_error": True},
    ]
    art_ok = mine_capability_deficit(_spec(retrieval={}, tool_call_sequence=calls_ok))
    assert art_ok.family == "unclassified"
    assert art_ok.attribution_gate == "unattributable"
    assert "positive_authority_unverified" in art_ok.hold_reasons


def test_mixed_blind_run_plus_adaptive_tail_records_both():
    calls = [
        {"tool_name": "start_conversation", "arguments": {}, "is_error": True},
        {"tool_name": "start_conversation", "arguments": {}, "is_error": True},
        {"tool_name": "start_conversation", "arguments": {}, "is_error": True},
        {"tool_name": "configure_run", "arguments": {"seed": 1}, "is_error": False},
    ]
    art = mine_capability_deficit(_spec(retrieval={}, tool_call_sequence=calls))
    assert art.family == "unclassified"
    kinds = {c.kind for c in art.counterevidence}
    assert "adaptive_interleave_present" in kinds
    assert "retry_pattern_adaptive" in art.hold_reasons


def test_integrity_unknown_blocks_supported_gate():
    spec = _spec(capture=_capture(environment_integrity="unknown"))
    art = mine_capability_deficit(spec)
    assert art.family == "unclassified"
    assert art.attribution_gate == "unattributable"
    assert "positive_verification_missing" in art.hold_reasons
    assert "mechanism_not_represented" in art.hold_reasons


def test_admissibility_unverified_blocks_supported_gate():
    spec = _spec(capture=_capture(trial_admissible=None))
    art = mine_capability_deficit(spec)
    assert art.attribution_gate == "unattributable"
    assert "positive_verification_missing" in art.hold_reasons


def test_adaptive_interleave_refutes_blind_retry():
    calls = [
        {"tool_name": "start_conversation", "arguments": {}, "is_error": True},
        {"tool_name": "configure_run", "arguments": {"seed": 1}, "is_error": False},
        {"tool_name": "start_conversation", "arguments": {}, "is_error": True},
        {"tool_name": "get_runtime_status", "arguments": {}, "is_error": False},
        {"tool_name": "start_conversation", "arguments": {}, "is_error": True},
    ]
    art = mine_capability_deficit(_spec(retrieval={}, tool_call_sequence=calls))
    assert art.family == "unclassified"
    assert any(c.kind == "adaptive_interleave_present" for c in art.counterevidence)


def test_self_attested_wrong_binding_cannot_support():
    spec = _spec(
        verifier={
            "status": "mismatch",
            "reward": 0.0,
            "tau2_evaluation": True,
            "reason": "Function call does not match ground truth",
            "output_contract_ok": True,
            "function_name_match": True,
            "argument_semantic_match": False,
            "argument_type_match": False,
        },
        retrieval={},
    )
    art = mine_capability_deficit(spec)
    assert art.family == "unclassified"
    assert art.attribution_gate == "unattributable"
    assert "positive_authority_unverified" in art.hold_reasons

def test_malformed_output_takes_precedence():
    spec = _spec(
        verifier={
            "status": "mismatch",
            "reward": 0.0,
            "tau2_evaluation": True,
            "reason": "no result file",
            "output_contract_ok": False,
            "function_name_match": False,
        },
        retrieval={},
    )
    art = mine_capability_deficit(spec)
    assert art.family == "unclassified"


def test_mining_is_idempotent_and_digest_binds():
    spec = _spec()
    a = mine_capability_deficit(spec)
    b = mine_capability_deficit(spec)
    assert a.model_dump_json() == b.model_dump_json()

    # substituting an UNBOUND *cited* path digest changes the artifact digest.
    # the wrong-binding specimen cites verifier/test-stdout.txt (unbound).
    spec_wb = _spec(
        verifier={
            "status": "mismatch",
            "reward": 0.0,
            "tau2_evaluation": True,
            "reason": "Function call does not match ground truth",
            "output_contract_ok": True,
            "function_name_match": True,
            "argument_semantic_match": False,
            "argument_type_match": False,
        },
        retrieval={},
    )
    art_wb = mine_capability_deficit(spec_wb)
    # consistent substitution of a bound path AND its binding digest -> new digest
    spec_wb2 = json.loads(json.dumps(spec_wb))
    spec_wb2["artifact_digests"]["verifier/test-stdout.txt"] = D_X
    spec_wb2["source_binding"]["test_stdout_digest"] = D_X
    art_wb2 = mine_capability_deficit(spec_wb2)
    assert art_wb2.content_digest != art_wb.content_digest
    # substituting an UNCITED digest must NOT change the artifact: only cited
    # sources are bound.
    spec_u = json.loads(json.dumps(spec))
    spec_u["artifact_digests"]["tau3_runtime_state.json"] = D_X
    assert mine_capability_deficit(spec_u).content_digest == a.content_digest
    # substituting a BOUND path digest is refused by the binding-match rule
    spec4 = json.loads(json.dumps(spec))
    spec4["artifact_digests"]["verifier/result.json"] = D_X
    with pytest.raises((ValidationError, ValueError)):
        mine_capability_deficit(spec4)

    spec3 = json.loads(json.dumps(spec))
    spec3["source_binding"]["trial_id"] = "other"
    art3 = mine_capability_deficit(spec3)
    assert art3.content_digest != a.content_digest

    body = json.loads(a.model_dump_json())
    body["content_digest"] = "sha256:" + "00" * 32
    with pytest.raises(ValidationError):
        CapabilityDeficitArtifact.model_validate(body)

    body2 = json.loads(a.model_dump_json())
    body2["family"] = "malformed-output"
    with pytest.raises(ValidationError):
        CapabilityDeficitArtifact.model_validate(body2)


def test_binding_digest_mismatch_is_typed_rejection():
    art = mine_capability_deficit(_spec())
    body = json.loads(art.model_dump_json())
    body["source_binding"]["verifier_result_digest"] = D_X
    with pytest.raises(ValidationError):
        CapabilityDeficitArtifact.model_validate(body)


def test_claim_structure_makes_general_claims_unrepresentable():
    art = mine_capability_deficit(_spec())
    d = art.model_dump()
    assert d["claim_scope"] == "descriptive_single_trial"
    assert not any(k in d for k in ("summary", "narrative", "analysis", "interpretation"))
    with pytest.raises(ValidationError):
        CapabilityDeficitArtifact.model_validate(
            {**d, "claim": "models generally fail at ordering"}
        )
    with pytest.raises(ValidationError):
        CapabilityDeficitArtifact.model_validate({**d, "extra_field": 1})
    for field, wrong in (
        ("schema_version", "capability-deficit-artifact/v0"),
        ("extractor_id", "not-the-miner"),
        ("extractor_version", "9"),
        ("algorithm_version", "deficit-classifier/v0"),
    ):
        broken = {**d, field: wrong}
        with pytest.raises(ValidationError):
            CapabilityDeficitArtifact.model_validate(broken)


def test_no_label_inference_from_paths_or_names():
    calls = [
        {"tool_name": "start_conversation", "arguments": {}, "is_error": True},
        {"tool_name": "start_conversation", "arguments": {}, "is_error": True},
        {"tool_name": "start_conversation", "arguments": {}, "is_error": True},
    ]
    spec_a = _spec(binding={"trial_id": "e0b-indexed-4k-semantic_distractor-s2026"}, retrieval={})
    spec_b = _spec(binding={"trial_id": "zzz-unrelated-name-999"}, retrieval={})
    spec_a["tool_call_sequence"] = calls
    spec_b["tool_call_sequence"] = calls
    art_a = mine_capability_deficit(spec_a)
    art_b = mine_capability_deficit(spec_b)
    assert art_a.family == art_b.family == "unclassified"
    assert art_a.attribution_gate == art_b.attribution_gate == "unattributable"
    spec_c = json.loads(json.dumps(spec_a))
    spec_c["verifier"]["reason"] = "failed: /Users/x/e0b-action-s2026/whatever"
    art_c = mine_capability_deficit(spec_c)
    assert art_c.family == "unclassified"


def test_trace_measures_na_lacking_and_delta(tmp_path):
    failed_root = tmp_path / "failed"
    failed_spec = _with_reverified_authorities(failed_root, _spec())
    arts = [
        mine_capability_deficit(failed_spec, authority_store_root=failed_root / "store")
    ]
    m = trace_capability_measures(arts, "complete-but-reordered")
    assert m.cov.status == "PRESENT" and m.cov.value == 1.0
    assert m.er_plus.status == "PRESENT" and m.er_plus.value == 1.0
    assert m.er_minus.status == "LACKING"
    assert m.delta.status == "NA"
    assert m.delta_interpretation == "priority_only_never_causal"

    passed_root = tmp_path / "passed"
    passed_spec = _with_reverified_authorities(
        passed_root,
        _spec(
            binding={"trial_id": "t2"},
            verifier={
                "status": "passed",
                "reward": 1.0,
                "tau2_evaluation": True,
                "reward_basis": ["DB"],
                "reward_breakdown": {"DB": 1.0},
            },
        ),
    )
    passed = mine_capability_deficit(passed_spec, authority_store_root=passed_root / "store")
    m2 = trace_capability_measures([arts[0], passed], "complete-but-reordered")
    assert m2.er_minus.status == "PRESENT" and m2.er_minus.value == 0.0
    assert m2.delta.status == "PRESENT" and m2.delta.value == 1.0
    assert trace_priority_order([m2]) == [("complete-but-reordered", 1.0)]


def test_trace_unknown_family_refused():
    arts = [mine_capability_deficit(_spec())]
    with pytest.raises(ValueError):
        trace_capability_measures(arts, "unclassified")
    with pytest.raises(ValueError):
        trace_capability_measures(arts, "none")


def test_attack_forged_args_digest_is_impossible():
    """arguments_digest no longer exists: identity is computed from arguments.
    Distinct real args, all errors -> unsuccessful adaptation (unclassified
    with typed hold), exactly as the orchestrator ruling requires; no forged
    digest can merge or split runs because no caller-supplied digest is read."""
    calls_distinct = [
        {"tool_name": "start_conversation", "arguments": {"q": "a"}, "is_error": True},
        {"tool_name": "start_conversation", "arguments": {"q": "b"}, "is_error": True},
        {"tool_name": "start_conversation", "arguments": {"q": "c"}, "is_error": True},
    ]
    art = mine_capability_deficit(_spec(retrieval={}, tool_call_sequence=calls_distinct))
    assert art.family == "unclassified"
    assert art.attribution_gate == "unattributable"
    assert "varied_argument_retry_unclassified" in art.hold_reasons
    with pytest.raises(ValidationError):
        CapabilityDeficitInput.model_validate(
            {**_spec(retrieval={}),
             "tool_call_sequence": [
                 {"tool_name": "x", "arguments": {}, "arguments_digest": D_V, "is_error": True}
             ]}
        )


def test_attack_wrong_verifier_digest_refused_by_miner():
    spec = _spec()
    spec["artifact_digests"]["verifier/result.json"] = D_X
    with pytest.raises(ValidationError):
        mine_capability_deficit(spec)


def test_attack_wrong_test_stdout_counter_digest_refused():
    spec = _spec(
        verifier={
            "status": "mismatch",
            "reward": 0.0,
            "tau2_evaluation": True,
            "reason": "Function call does not match ground truth",
            "output_contract_ok": True,
            "function_name_match": True,
            "argument_semantic_match": False,
            "argument_type_match": False,
        },
        retrieval={},
    )
    spec["artifact_digests"]["verifier/test-stdout.txt"] = D_X
    with pytest.raises(ValidationError):
        mine_capability_deficit(spec)


def test_missing_binding_digest_is_typed_rejection():
    b = _binding()
    del b["verifier_result_digest"]
    with pytest.raises(ValidationError):
        CapabilityDeficitInput.model_validate({**_spec(), "source_binding": b})


def test_missing_cited_digest_refuses():
    spec = _spec()
    del spec["artifact_digests"]["artifacts/app/output/benchmark-events.jsonl"]
    with pytest.raises(ValueError, match="artifact digest missing"):
        mine_capability_deficit(spec)


def test_malformed_digest_pattern_refused():
    b = _binding()
    b["task_digest"] = "not-a-digest"
    with pytest.raises(ValidationError):
        CapabilityDeficitInput.model_validate({**_spec(), "source_binding": b})


def test_evidence_id_overlap_refused():
    art = mine_capability_deficit(_spec())
    body = json.loads(art.model_dump_json())
    # craft the same content-derived id on both sides
    if body["evidence"] and body["counterevidence"]:
        body["counterevidence"][-1]["evidence_id"] = body["evidence"][0]["evidence_id"]
        with pytest.raises(ValidationError):
            CapabilityDeficitArtifact.model_validate(body)
    else:
        # deterministic fallback: duplicate id inside one list
        body2 = json.loads(art.model_dump_json())
        body2["evidence"].append(dict(body2["evidence"][0]))
        with pytest.raises(ValidationError):
            CapabilityDeficitArtifact.model_validate(body2)
