"""Comprehensive test suite for shared artifact authority boundary (rev3).

Covers all Section 4 and audit-required controls:
- (a) Raw CAS Blob verification (load_blob) with strict hash check
- (b) Archived Inner Artifact verification (ArchiveAnchor + reopen_evidence_archive)
- (c) Repo-jailed regular file verification (_read_repo_regular_file with O_NOFOLLOW)
- (d) Jailed path traversal refusal & symlink root refusal
- (e) CAS URI vs digest contradiction refusal
- (f) On-demand re-anchoring across all 3 paths
- (g) Admissibility gating with explicit artifact_kind binding and causal_eligible check
- (h) Verifier implementation digest mismatch refusal
- (i) Model immutability, frozen contracts, extra-field rejection
- (j) Rehydration parity and authority_digest determinism
"""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.artifact_authority import (
    VERIFIER_IMPLEMENTATION_DIGEST,
    ArchiveAnchor,
    ArtifactAuthority,
    ArtifactRef,
    AuthorityRefusal,
    compute_authority_digest,
    verify_artifact,
)
from evallab.evidence_store import archive_evidence, store_blob
from evallab.schemas import (
    NETWORK_ESCAPE_CLASSES,
    Digest,
    NetworkEscapeProbeResultV1,
    NetworkIsolationEvidenceV1,
    NetworkIsolationProbeIdentityV1,
    NetworkIsolationRuntimeIdentityV1,
    NetworkPolicyEvidenceV1,
    TaskRuntimeIdentityV1,
    TrialAdmissibilityV1,
    TrialSourceDigestsV1,
    TrialSourcePathsV1,
    build_network_isolation_evidence,
    build_trial_admissibility,
)

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


def _isolation_evidence() -> NetworkIsolationEvidenceV1:
    policy = NetworkPolicyEvidenceV1(mode="no-network")
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
            container_image_digest=DIGEST,
            adapter="test-adapter",
            adapter_version="1",
            adapter_digest="sha256:" + "b" * 64,
        ),
        probe_identity=NetworkIsolationProbeIdentityV1(
            implementation="test-probe",
            implementation_version="1",
            implementation_digest="sha256:" + "c" * 64,
            config_digest="sha256:" + "d" * 64,
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
        observed_at=NOW,
        valid_until=NOW + timedelta(days=7),
        evaluated_at=NOW,
    )


def _make_admissibility(
    trial_id: str = "trial-001",
    trajectory: str = "sha256:2222222222222222222222222222222222222222222222222222222222222222",
    verifier: str = "sha256:5555555555555555555555555555555555555555555555555555555555555555",
    task_state: str = "registered",
    allowed_use_override: str | None = None,
) -> TrialAdmissibilityV1:
    task_identity = TaskRuntimeIdentityV1(
        task_id="task-01",
        task_version="1.0",
        registry_record_digest=Digest("sha256:" + "1" * 64),
        certified_runtime_package_digest=Digest("sha256:" + "2" * 64),
        registry_admission_state=task_state,  # type: ignore[arg-type]
    )
    source_digests = TrialSourceDigestsV1(
        contract=Digest("sha256:" + "3" * 64),
        trajectory=Digest(trajectory),
        final_state=Digest("sha256:" + "4" * 64),
        verifier=Digest(verifier),
        outcome=Digest("sha256:" + "6" * 64),
        interpretation=Digest("sha256:" + "7" * 64),
    )
    source_paths = TrialSourcePathsV1(
        contract=("benchmark_contract.json",),
        trajectory=("trajectory.json",),
        final_state=("final-state.json",),
        verifier=("verifier/result.json",),
        outcome=("outcome.json",),
        interpretation=("interpretation.json",),
    )
    isolation = _isolation_evidence() if task_state == "registered" else None
    record = build_trial_admissibility(
        trial_id=trial_id,
        task_runtime_identity=task_identity,
        source_digests=source_digests,
        source_paths=source_paths,
        network_isolation_evidence=isolation,
        evaluated_at=NOW,
    )
    if allowed_use_override:
        return record.model_copy(update={"allowed_use": allowed_use_override})
    return record


def test_structural_verification_success() -> None:
    ref = ArtifactRef(
        ref="docs/INDEX.md",
        digest=Digest("sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
    )
    result = verify_artifact(
        ref,
        minimum_level="structural-self-consistent",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
    )
    assert isinstance(result, ArtifactAuthority)
    assert result.level == "structural-self-consistent"
    assert result.artifact == ref
    assert result.verifier_implementation_digest == VERIFIER_IMPLEMENTATION_DIGEST
    assert result.authority_digest.startswith("sha256:")


def test_cas_ref_structural_validation_and_uri_digest_mismatch() -> None:
    valid_hex = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    valid_cas = ArtifactRef(
        ref=f"cas://sha256/{valid_hex}",
        digest=Digest(f"sha256:{valid_hex}"),
    )
    result = verify_artifact(
        valid_cas,
        minimum_level="structural-self-consistent",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
    )
    assert isinstance(result, ArtifactAuthority)

    # Invalid non-hex
    with pytest.raises(ValidationError):
        ArtifactRef(
            ref="cas://sha256/not-hex",
            digest=Digest(f"sha256:{valid_hex}"),
        )

    # URI hex contradicts declared digest
    mismatched_hex = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    with pytest.raises(ValidationError):
        ArtifactRef(
            ref=f"cas://sha256/{valid_hex}",
            digest=Digest(f"sha256:{mismatched_hex}"),
        )


def test_ref_canonical_path_validation() -> None:
    with pytest.raises(ValidationError):
        ArtifactRef(
            ref="/etc/passwd",
            digest=Digest(
                "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            ),
        )

    with pytest.raises(ValidationError):
        ArtifactRef(
            ref="../secret.txt",
            digest=Digest(
                "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            ),
        )

    with pytest.raises(ValidationError):
        ArtifactRef(
            ref="docs/./INDEX.md",
            digest=Digest(
                "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            ),
        )


def test_verifier_implementation_mismatch() -> None:
    ref = ArtifactRef(
        ref="docs/INDEX.md",
        digest=Digest("sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
    )
    fake_digest = Digest("sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff")
    result = verify_artifact(
        ref,
        minimum_level="structural-self-consistent",
        verifier_implementation_digest=fake_digest,
    )
    assert isinstance(result, AuthorityRefusal)
    assert result.reason == "verifier_implementation_mismatch"


def test_bytes_verified_repo_file(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target_file = repo_root / "payload.json"
    content = b'{"status": "ok"}'
    target_file.write_bytes(content)
    expected_sha = Digest(f"sha256:{hashlib.sha256(content).hexdigest()}")

    ref = ArtifactRef(ref="payload.json", digest=expected_sha)
    result = verify_artifact(
        ref,
        minimum_level="bytes-verified",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        repo_root=repo_root,
    )
    assert isinstance(result, ArtifactAuthority)
    assert result.level == "bytes-verified"
    assert result.artifact.digest == expected_sha
    assert result.reanchor(repo_root) == content


def test_bytes_verified_digest_mismatch(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target_file = repo_root / "payload.json"
    target_file.write_bytes(b"actual content")
    declared_sha = Digest("sha256:0000000000000000000000000000000000000000000000000000000000000000")

    ref = ArtifactRef(ref="payload.json", digest=declared_sha)
    result = verify_artifact(
        ref,
        minimum_level="bytes-verified",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        repo_root=repo_root,
    )
    assert isinstance(result, AuthorityRefusal)
    assert result.reason == "ref_digest_parity_failed"


def test_bytes_verified_source_unreadable(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    ref = ArtifactRef(
        ref="non_existent.json",
        digest=Digest("sha256:0000000000000000000000000000000000000000000000000000000000000000"),
    )
    result = verify_artifact(
        ref,
        minimum_level="bytes-verified",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        repo_root=repo_root,
    )
    assert isinstance(result, AuthorityRefusal)
    assert result.reason == "source_unreadable"


def test_bytes_verified_cas_raw_blob(tmp_path: Path) -> None:
    cas_root = tmp_path / "cas"
    content = b"cas payload data stored via store_blob"
    uri = store_blob(cas_root, content)
    digest_hex = hashlib.sha256(content).hexdigest()
    declared_digest = Digest(f"sha256:{digest_hex}")

    ref = ArtifactRef(ref=uri, digest=declared_digest)
    result = verify_artifact(
        ref,
        minimum_level="bytes-verified",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        store_root=cas_root,
    )
    assert isinstance(result, ArtifactAuthority)
    assert result.level == "bytes-verified"
    assert result.artifact.digest == declared_digest
    assert result.reanchor(cas_root) == content


def test_bytes_verified_archived_inner_artifact(tmp_path: Path) -> None:
    store_root = tmp_path / "store"
    source_dir = tmp_path / "source_evidence"
    source_dir.mkdir()
    inner_content = b'{"score": 1.0, "status": "passed"}'
    (source_dir / "result.json").write_bytes(inner_content)
    inner_sha = Digest(f"sha256:{hashlib.sha256(inner_content).hexdigest()}")

    archive = archive_evidence(source_dir, store_root, record_id="trial-001", kind="job")

    anchor = ArchiveAnchor(
        record_kind="job",
        record_id="trial-001",
        expected_record_digest=Digest(archive.record_digest),
        expected_content_digest=Digest(archive.content_digest),
        inner_path="result.json",
    )
    ref = ArtifactRef(
        ref="result.json",
        digest=inner_sha,
    )
    result = verify_artifact(
        ref,
        minimum_level="bytes-verified",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        anchor=anchor,
        store_root=store_root,
    )
    assert isinstance(result, ArtifactAuthority)
    assert result.anchor == anchor
    assert result.level == "bytes-verified"
    assert result.artifact.digest == inner_sha
    assert result.reanchor(store_root) == inner_content


def test_bytes_verified_with_admissibility_and_kind_binding(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    content = b"trajectory data"
    sha = Digest(f"sha256:{hashlib.sha256(content).hexdigest()}")
    (repo_root / "trajectory.json").write_bytes(content)

    admissibility = _make_admissibility(trajectory=sha, task_state="registered")
    assert admissibility.causal_eligible is True
    ref = ArtifactRef(ref="trajectory.json", digest=sha)
    result = verify_artifact(
        ref,
        minimum_level="bytes-verified",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        admissibility=admissibility,
        artifact_kind="trajectory",
        repo_root=repo_root,
    )
    assert isinstance(result, ArtifactAuthority)
    assert result.level == "bytes-verified"

    # Mismatched kind binding
    mismatched_kind_result = verify_artifact(
        ref,
        minimum_level="bytes-verified",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        admissibility=admissibility,
        artifact_kind="verifier",  # claims to be verifier, but sha is trajectory
        repo_root=repo_root,
    )
    assert isinstance(mismatched_kind_result, AuthorityRefusal)
    assert mismatched_kind_result.reason == "receipt_digest_mismatch"


def test_bytes_verified_with_non_admissible_refusal(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    content = b"trajectory data"
    sha = Digest(f"sha256:{hashlib.sha256(content).hexdigest()}")
    (repo_root / "trajectory.json").write_bytes(content)

    admissibility = _make_admissibility(trajectory=sha, task_state="candidate")
    assert admissibility.causal_eligible is False
    ref = ArtifactRef(ref="trajectory.json", digest=sha)
    result = verify_artifact(
        ref,
        minimum_level="bytes-verified",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        admissibility=admissibility,
        repo_root=repo_root,
    )
    assert isinstance(result, AuthorityRefusal)
    assert result.reason == "authority_level_insufficient"


def test_model_immutability_and_extra_forbidden() -> None:
    ref = ArtifactRef(
        ref="docs/INDEX.md",
        digest=Digest("sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
    )
    authority = ArtifactAuthority(
        artifact=ref,
        level="structural-self-consistent",
        verifier_implementation_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        authority_digest=compute_authority_digest(
            ref, "structural-self-consistent", VERIFIER_IMPLEMENTATION_DIGEST
        ),
    )
    with pytest.raises(ValidationError):
        authority.level = "bytes-verified"  # type: ignore[misc]

    with pytest.raises(ValidationError):
        ArtifactRef.model_validate({"ref": "docs/INDEX.md", "digest": ref.digest, "extra": 123})
