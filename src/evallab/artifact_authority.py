"""Shared artifact authority boundary and verification primitives.

Grounding: Track F second-wave specification rev4 (research/inbox/artifact-authority-boundary-20260903.md)
Provides a fail-closed boundary distinguishing structural self-consistency from repo-jailed, bytes-verified authority.

Three distinct, non-fallback bytes-verification paths:
1. Raw CAS Blob (`cas://sha256/<hex>`): load_blob via evidence_store with strict content hash verification.
2. Archived Inner Artifact (ArchiveAnchor): reopen_evidence_archive with EvidenceLocator + exact inner_path extraction.
3. Repo-Jailed Regular File: _read_repo_regular_file with strict O_NOFOLLOW descriptor traversal.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tarfile
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from evallab.benchmark_program_contracts import canonical_json, compute_prefixed_sha256
from evallab.evidence_store import (
    _absolute,
    load_blob,
    open_archive,
    reopen_evidence_archive,
)
from evallab.schemas import ContractModel, Digest, TrialAdmissibilityV1

AuthorityLevel = Literal["structural-self-consistent", "bytes-verified"]
ArtifactKind = Literal[
    "contract", "trajectory", "final_state", "verifier", "outcome", "interpretation"
]

MODULE_NAME: str = "evallab.artifact_authority"
MODULE_VERSION: str = "2.0.0"
VERIFY_FUNCTION_NAME: str = "verify_artifact"


def compute_verifier_implementation_digest(
    module_name: str = MODULE_NAME,
    version: str = MODULE_VERSION,
    function_name: str = VERIFY_FUNCTION_NAME,
) -> Digest:
    """Compute the immutable identifier binding the verifying code identity."""
    payload = {
        "function": function_name,
        "module": module_name,
        "version": version,
    }
    return Digest(compute_prefixed_sha256(canonical_json(payload)))


VERIFIER_IMPLEMENTATION_DIGEST: Digest = compute_verifier_implementation_digest()


class ArtifactRef(ContractModel):
    """A typed reference to an artifact by canonical path or CAS URI and its declared digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str = Field(min_length=1)
    digest: Digest

    @field_validator("ref")
    @classmethod
    def validate_ref_is_canonical(cls, value: str) -> str:
        if not value or not isinstance(value, str):
            raise ValueError("ref must be a non-empty string")

        if value.startswith("cas://sha256/"):
            hex_part = value.removeprefix("cas://sha256/")
            if len(hex_part) != 64 or not all(c in "0123456789abcdef" for c in hex_part):
                raise ValueError(
                    f"cas ref must have a 64-character lowercase hex digest: {value!r}"
                )
            return value

        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError(
                f"ref must be a canonical repo-relative POSIX path or cas://sha256/<hex>, got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def cas_uri_matches_declared_digest(self) -> ArtifactRef:
        if self.ref.startswith("cas://sha256/"):
            hex_part = self.ref.removeprefix("cas://sha256/")
            expected_digest = f"sha256:{hex_part}"
            if self.digest != expected_digest:
                raise ValueError(
                    f"cas ref URI digest {hex_part!r} does not match declared digest {self.digest!r}"
                )
        return self


class ArchiveAnchor(ContractModel):
    """Authenticated coordinate binding an artifact inside an immutable evidence archive."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_kind: str = Field(min_length=1)
    record_id: str = Field(min_length=1)
    expected_record_digest: Digest
    expected_content_digest: Digest
    inner_path: str = Field(min_length=1)

    @field_validator("record_kind", "record_id")
    @classmethod
    def validate_component_names(cls, value: str) -> str:
        if not value or value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
            raise ValueError(f"invalid record component: {value!r}")
        return value

    @field_validator("inner_path")
    @classmethod
    def validate_inner_path_canonical(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError(f"inner_path must be a canonical relative POSIX path: {value!r}")
        return value


class AdmissibilityReceiptBinding(ContractModel):
    """Immutable binding of verified trial admissibility decision and artifact kind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trial_id: str = Field(min_length=1)
    admissibility_digest: Digest
    artifact_kind: ArtifactKind


def compute_authority_digest(
    artifact: ArtifactRef,
    level: AuthorityLevel,
    verifier_implementation_digest: Digest,
    anchor: ArchiveAnchor | None = None,
    admissibility_binding: AdmissibilityReceiptBinding | None = None,
) -> Digest:
    """Derive deterministic content-addressed semantic digest of the authority statement."""
    payload = {
        "admissibility_binding": (
            admissibility_binding.model_dump(mode="json")
            if admissibility_binding is not None
            else None
        ),
        "anchor": anchor.model_dump(mode="json") if anchor is not None else None,
        "artifact": artifact.model_dump(mode="json"),
        "level": level,
        "verifier_implementation_digest": verifier_implementation_digest,
    }
    return Digest(compute_prefixed_sha256(canonical_json(payload)))


class ArtifactAuthority(ContractModel):
    """An affirmative, verifiable authority statement binding an artifact to its verified level."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact: ArtifactRef
    anchor: ArchiveAnchor | None = None
    admissibility_binding: AdmissibilityReceiptBinding | None = None
    level: AuthorityLevel
    verifier_implementation_digest: Digest
    authority_digest: Digest

    @model_validator(mode="after")
    def authority_digest_matches(self) -> ArtifactAuthority:
        expected = compute_authority_digest(
            self.artifact,
            self.level,
            self.verifier_implementation_digest,
            self.anchor,
            self.admissibility_binding,
        )
        if self.authority_digest != expected:
            raise ValueError(
                f"authority_digest mismatch: expected {expected!r}, got {self.authority_digest!r}"
            )
        return self


class AuthorityRefusal(ContractModel):
    """A typed, fail-closed refusal indicating why an artifact cannot be certified at the requested level."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: Literal[
        "authority_level_insufficient",
        "ref_digest_parity_failed",
        "ref_not_canonical",
        "anchor_ref_mismatch",
        "admissibility_parameter_mismatch",
        "source_unreadable",
        "verifier_implementation_mismatch",
        "receipt_digest_mismatch",
        "receipt_contradiction",
    ]
    detail: str


AuthorityResult = ArtifactAuthority | AuthorityRefusal


def _read_repo_regular_file(repo_root: Path, relative_path: str) -> tuple[bytes | None, str | None]:
    """Read repo-confined bytes without following any component symlink."""
    path = PurePosixPath(relative_path)
    components = path.parts
    if (
        path.is_absolute()
        or path.as_posix() != relative_path
        or not components
        or any(component in {"", ".", ".."} for component in components)
    ):
        return None, "ref_not_canonical: path must be canonical and repo-relative"

    try:
        canonical_root = _absolute(repo_root)
    except ValueError as exc:
        return None, f"source_unreadable: invalid repo root: {exc}"

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        directory = os.open(canonical_root, flags)
    except OSError as exc:
        return None, f"source_unreadable: cannot open repository root: {exc}"

    try:
        for component in components[:-1]:
            try:
                next_directory = os.open(component, flags, dir_fd=directory)
            except OSError as exc:
                return None, f"source_unreadable: cannot traverse path {relative_path!r}: {exc}"
            os.close(directory)
            directory = next_directory

        file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(components[-1], file_flags, dir_fd=directory)
        except OSError as exc:
            return None, f"source_unreadable: cannot open file {relative_path!r}: {exc}"

        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                return None, f"source_unreadable: path {relative_path!r} must be a regular file"
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 64 * 1024):
                chunks.append(chunk)
            return b"".join(chunks), None
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _read_anchored_archive_member(
    store_root: Path, anchor: ArchiveAnchor
) -> tuple[bytes | None, str | None]:
    """Open an authenticated archive and extract the exact inner artifact bytes."""
    try:
        canonical_store = _absolute(store_root)
    except ValueError as exc:
        return None, f"source_unreadable: invalid evidence store root: {exc}"

    try:
        archive, _record_bytes = reopen_evidence_archive(
            canonical_store,
            kind=anchor.record_kind,
            record_id=anchor.record_id,
            expected_record_digest=anchor.expected_record_digest,
            expected_content_digest=anchor.expected_content_digest,
        )
    except Exception as exc:
        return (
            None,
            f"source_unreadable: failed reopening evidence archive {anchor.record_id}: {exc}",
        )

    try:
        with (
            open_archive(canonical_store, archive.uri) as source,
            tarfile.open(fileobj=source, mode="r:gz") as tar,
        ):
            try:
                member = tar.getmember(anchor.inner_path)
            except KeyError:
                return (
                    None,
                    f"source_unreadable: inner artifact {anchor.inner_path!r} not in archive",
                )
            if not member.isfile():
                return (
                    None,
                    f"source_unreadable: inner artifact {anchor.inner_path!r} is not a regular file",
                )
            extracted = tar.extractfile(member)
            if extracted is None:
                return None, f"source_unreadable: failed extracting {anchor.inner_path!r}"
            return extracted.read(), None
    except Exception as exc:
        return (
            None,
            f"source_unreadable: failed reading archive member {anchor.inner_path!r}: {exc}",
        )


def verify_artifact(
    artifact: ArtifactRef,
    *,
    minimum_level: AuthorityLevel,
    verifier_implementation_digest: Digest,
    anchor: ArchiveAnchor | None = None,
    admissibility: TrialAdmissibilityV1 | None = None,
    artifact_kind: ArtifactKind | None = None,
    repo_root: Path | str | None = None,
    store_root: Path | str | None = None,
) -> AuthorityResult:
    """Verify an artifact reference at the requested authority level.

    Fail-closed: Returns an AuthorityRefusal on any structural, parity, provenance, or accessibility error.
    """
    if verifier_implementation_digest != VERIFIER_IMPLEMENTATION_DIGEST:
        return AuthorityRefusal(
            reason="verifier_implementation_mismatch",
            detail=(
                f"verifier_implementation_digest mismatch: expected "
                f"{VERIFIER_IMPLEMENTATION_DIGEST!r}, got {verifier_implementation_digest!r}"
            ),
        )

    # Invariant: anchor.inner_path must equal artifact.ref (prevents ref spoofing)
    if anchor is not None and artifact.ref != anchor.inner_path:
        return AuthorityRefusal(
            reason="anchor_ref_mismatch",
            detail=(
                f"artifact.ref {artifact.ref!r} does not match anchor.inner_path {anchor.inner_path!r}"
            ),
        )

    # Invariant: admissibility and artifact_kind must be provided together (iff rule)
    if (admissibility is None) != (artifact_kind is None):
        return AuthorityRefusal(
            reason="admissibility_parameter_mismatch",
            detail=(
                "admissibility and artifact_kind must be provided together: "
                f"admissibility={'present' if admissibility else 'absent'}, "
                f"artifact_kind={artifact_kind!r}"
            ),
        )

    admissibility_binding: AdmissibilityReceiptBinding | None = None
    if admissibility is not None and artifact_kind is not None:
        if not admissibility.causal_eligible:
            return AuthorityRefusal(
                reason="authority_level_insufficient",
                detail=(
                    f"trial admissibility for trial {admissibility.trial_id!r} "
                    f"is not causal-eligible (decision={admissibility.decision!r}, "
                    f"allowed_use={admissibility.allowed_use!r})"
                ),
            )

        digests_dict = admissibility.source_digests.model_dump(mode="json")
        expected_kind_digest = digests_dict.get(artifact_kind)
        if expected_kind_digest != artifact.digest:
            return AuthorityRefusal(
                reason="receipt_digest_mismatch",
                detail=(
                    f"artifact digest {artifact.digest!r} for kind {artifact_kind!r} "
                    f"does not match admissibility record digest: {expected_kind_digest!r}"
                ),
            )

        admissibility_binding = AdmissibilityReceiptBinding(
            trial_id=admissibility.trial_id,
            admissibility_digest=admissibility.admissibility_digest,
            artifact_kind=artifact_kind,
        )

    if minimum_level == "structural-self-consistent":
        authority_digest = compute_authority_digest(
            artifact,
            "structural-self-consistent",
            verifier_implementation_digest,
            anchor,
            admissibility_binding,
        )
        return ArtifactAuthority(
            artifact=artifact,
            anchor=anchor,
            admissibility_binding=admissibility_binding,
            level="structural-self-consistent",
            verifier_implementation_digest=verifier_implementation_digest,
            authority_digest=authority_digest,
        )

    if minimum_level == "bytes-verified":
        effective_repo_root = Path(repo_root or Path.cwd())
        effective_store_root = Path(store_root or effective_repo_root / "derived" / "evidence-cas")

        # Path 1: Archived Inner Artifact via ArchiveAnchor
        if anchor is not None:
            raw_bytes, error_detail = _read_anchored_archive_member(effective_store_root, anchor)
            if raw_bytes is None:
                return AuthorityRefusal(
                    reason="source_unreadable",
                    detail=error_detail or f"cannot read anchored artifact {anchor.inner_path!r}",
                )

        # Path 2: Raw CAS Blob
        elif artifact.ref.startswith("cas://sha256/"):
            try:
                canonical_store = _absolute(effective_store_root)
                raw_bytes = load_blob(canonical_store, artifact.ref)
            except Exception as exc:
                return AuthorityRefusal(
                    reason="source_unreadable",
                    detail=f"cannot read CAS blob {artifact.ref!r}: {exc}",
                )

        # Path 3: Repo-Jailed Regular File
        else:
            raw_bytes, error_detail = _read_repo_regular_file(effective_repo_root, artifact.ref)
            if raw_bytes is None:
                if error_detail and error_detail.startswith("ref_not_canonical"):
                    return AuthorityRefusal(
                        reason="ref_not_canonical",
                        detail=error_detail,
                    )
                return AuthorityRefusal(
                    reason="source_unreadable",
                    detail=error_detail or f"cannot read repo file {artifact.ref!r}",
                )

        computed_sha = hashlib.sha256(raw_bytes).hexdigest()
        actual_digest = Digest(f"sha256:{computed_sha}")

        # Strict ref/digest parity for EVERY path (never skipped)
        if artifact.digest != actual_digest:
            return AuthorityRefusal(
                reason="ref_digest_parity_failed",
                detail=(
                    f"ref/digest parity failed for {artifact.ref!r}: "
                    f"declared {artifact.digest!r}, actual bytes {actual_digest!r}"
                ),
            )

        authority_digest = compute_authority_digest(
            artifact,
            "bytes-verified",
            verifier_implementation_digest,
            anchor,
            admissibility_binding,
        )
        return ArtifactAuthority(
            artifact=artifact,
            anchor=anchor,
            admissibility_binding=admissibility_binding,
            level="bytes-verified",
            verifier_implementation_digest=verifier_implementation_digest,
            authority_digest=authority_digest,
        )

    return AuthorityRefusal(
        reason="authority_level_insufficient",
        detail=f"unrecognized authority level: {minimum_level!r}",
    )


def reverify_authority(
    authority: ArtifactAuthority,
    *,
    expected_verifier_digest: Digest,
    repo_root: Path | str | None = None,
    store_root: Path | str | None = None,
) -> tuple[bytes, ArtifactAuthority] | AuthorityRefusal:
    """Consumers call this to re-read exact bytes and prove authority statement authenticity.

    Guarantees:
    1. Re-verifies verifier_implementation_digest against expected_verifier_digest.
    2. Re-derives authority_digest against model fields.
    3. Re-reads exact bytes through the authenticated path and re-checks sha256(bytes) == artifact.digest.
    4. Returns exact verified bytes plus authentic authority statement, or AuthorityRefusal.
    """
    if authority.level != "bytes-verified":
        return AuthorityRefusal(
            reason="authority_level_insufficient",
            detail=f"cannot re-verify bytes for structural authority level: {authority.level!r}",
        )

    if authority.verifier_implementation_digest != expected_verifier_digest:
        return AuthorityRefusal(
            reason="verifier_implementation_mismatch",
            detail=(
                f"verifier digest mismatch: expected {expected_verifier_digest!r}, "
                f"got {authority.verifier_implementation_digest!r}"
            ),
        )

    expected_authority_digest = compute_authority_digest(
        authority.artifact,
        authority.level,
        authority.verifier_implementation_digest,
        authority.anchor,
        authority.admissibility_binding,
    )
    if authority.authority_digest != expected_authority_digest:
        return AuthorityRefusal(
            reason="receipt_digest_mismatch",
            detail="authority_digest does not match recomputed semantic digest",
        )

    effective_repo_root = Path(repo_root or Path.cwd())
    effective_store_root = Path(store_root or effective_repo_root / "derived" / "evidence-cas")

    if authority.anchor is not None:
        raw_bytes, error_detail = _read_anchored_archive_member(
            effective_store_root, authority.anchor
        )
    elif authority.artifact.ref.startswith("cas://sha256/"):
        try:
            canonical_store = _absolute(effective_store_root)
            raw_bytes = load_blob(canonical_store, authority.artifact.ref)
            error_detail = None
        except Exception as exc:
            raw_bytes, error_detail = None, str(exc)
    else:
        raw_bytes, error_detail = _read_repo_regular_file(
            effective_repo_root, authority.artifact.ref
        )

    if raw_bytes is None:
        return AuthorityRefusal(
            reason="source_unreadable",
            detail=error_detail or f"cannot re-read bytes for {authority.artifact.ref!r}",
        )

    computed_sha = hashlib.sha256(raw_bytes).hexdigest()
    actual_digest = Digest(f"sha256:{computed_sha}")
    if authority.artifact.digest != actual_digest:
        return AuthorityRefusal(
            reason="ref_digest_parity_failed",
            detail=(
                f"re-verification parity failed for {authority.artifact.ref!r}: "
                f"expected {authority.artifact.digest!r}, got bytes digest {actual_digest!r}"
            ),
        )

    return raw_bytes, authority
