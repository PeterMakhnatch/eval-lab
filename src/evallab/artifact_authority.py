"""Shared artifact authority boundary and verification primitives.

Grounding: Track F second-wave specification rev2 (research/inbox/artifact-authority-boundary-20260903.md)
Provides a fail-closed boundary distinguishing structural self-consistency from repo-jailed, bytes-verified authority.
Integrates natively with evallab.evidence_store primitives (load_blob, read_archive, reopen_evidence_archive)
and TrialAdmissibilityV1 authority records.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from evallab.benchmark_program_contracts import canonical_json, compute_prefixed_sha256
from evallab.evidence_store import (
    EvidenceLocator,
    _absolute,
    load_blob,
    read_archive,
    read_record,
    reopen_evidence_archive,
)
from evallab.schemas import ContractModel, Digest, TrialAdmissibilityV1

AuthorityLevel = Literal["structural-self-consistent", "bytes-verified"]

MODULE_NAME: str = "evallab.artifact_authority"
MODULE_VERSION: str = "1.0.0"
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
    """A typed reference to an artifact by canonical path, CAS URI, or archive anchor."""

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
    """Authenticated coordinate in the immutable evidence store."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    archive_uri: str
    record_kind: str
    record_id: str
    expected_record_digest: Digest | None = None


def compute_authority_digest(
    artifact: ArtifactRef,
    level: AuthorityLevel,
    verifier_implementation_digest: Digest,
    anchor: ArchiveAnchor | None = None,
) -> Digest:
    """Derive deterministic content-addressed semantic digest of the authority statement."""
    payload = {
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
    level: AuthorityLevel
    verifier_implementation_digest: Digest
    authority_digest: Digest

    @model_validator(mode="after")
    def authority_digest_matches(self) -> ArtifactAuthority:
        expected = compute_authority_digest(
            self.artifact, self.level, self.verifier_implementation_digest, self.anchor
        )
        if self.authority_digest != expected:
            raise ValueError(
                f"authority_digest mismatch: expected {expected!r}, got {self.authority_digest!r}"
            )
        return self

    def reanchor(self, store_root: Path) -> bytes | None:
        """Re-verify bytes on demand through the authenticated evidence archive."""
        if self.level != "bytes-verified":
            return None
        if self.anchor is not None:
            if self.anchor.expected_record_digest:
                locator = EvidenceLocator(
                    store_root=store_root,
                    kind=self.anchor.record_kind,
                    record_id=self.anchor.record_id,
                    expected_record_digest=self.anchor.expected_record_digest,
                    expected_content_digest=self.artifact.digest,
                )
                _, record_bytes = reopen_evidence_archive(locator)
                return record_bytes
            return read_record(
                store_root, kind=self.anchor.record_kind, record_id=self.anchor.record_id
            )
        if self.artifact.ref.startswith("cas://sha256/"):
            return load_blob(store_root, self.artifact.ref)
        return None


class AuthorityRefusal(ContractModel):
    """A typed, fail-closed refusal indicating why an artifact cannot be certified at the requested level."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: Literal[
        "authority_level_insufficient",
        "ref_digest_parity_failed",
        "ref_not_canonical",
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


def _read_cas_payload(
    store_root: Path, uri: str, anchor: ArchiveAnchor | None = None
) -> tuple[bytes | None, str | None]:
    """Load CAS payload using evidence_store primitives with no-follow descriptors."""
    try:
        canonical_store = _absolute(store_root)
    except ValueError as exc:
        return None, f"source_unreadable: invalid CAS root: {exc}"

    # 1. Direct blob lookup
    try:
        return load_blob(canonical_store, uri), None
    except FileNotFoundError:
        pass
    except ValueError:
        pass
    except Exception as exc:
        return None, f"source_unreadable: failed reading CAS payload for {uri}: {exc}"

    # 2. Archive lookup via reopen_evidence_archive if anchor is provided
    if anchor is not None:
        try:
            if anchor.expected_record_digest:
                locator = EvidenceLocator(
                    store_root=canonical_store,
                    kind=anchor.record_kind,
                    record_id=anchor.record_id,
                    expected_record_digest=anchor.expected_record_digest,
                    expected_content_digest=Digest(f"sha256:{uri.removeprefix('cas://sha256/')}"),
                )
                archive, record_bytes = reopen_evidence_archive(locator)
                if archive.content_digest == f"sha256:{uri.removeprefix('cas://sha256/')}":
                    return canonical_json(archive).encode("utf-8"), None
            else:
                record_bytes = read_record(
                    canonical_store, kind=anchor.record_kind, record_id=anchor.record_id
                )
                return record_bytes, None
        except Exception:
            pass

    # 3. Direct archive read
    try:
        return read_archive(canonical_store, uri), None
    except FileNotFoundError:
        return None, f"source_unreadable: CAS artifact missing at {uri}"
    except Exception as exc:
        return None, f"source_unreadable: cannot open CAS archive {uri}: {exc}"


def verify_artifact(
    artifact: ArtifactRef,
    *,
    minimum_level: AuthorityLevel,
    verifier_implementation_digest: Digest,
    anchor: ArchiveAnchor | None = None,
    admissibility: TrialAdmissibilityV1 | None = None,
    repo_root: Path | str | None = None,
    cas_root: Path | str | None = None,
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

    if minimum_level == "structural-self-consistent":
        authority_digest = compute_authority_digest(
            artifact, "structural-self-consistent", verifier_implementation_digest, anchor
        )
        return ArtifactAuthority(
            artifact=artifact,
            anchor=anchor,
            level="structural-self-consistent",
            verifier_implementation_digest=verifier_implementation_digest,
            authority_digest=authority_digest,
        )

    if minimum_level == "bytes-verified":
        effective_repo_root = Path(repo_root or Path.cwd())
        effective_cas_root = Path(cas_root or effective_repo_root / "derived" / "evidence-cas")

        if artifact.ref.startswith("cas://sha256/"):
            raw_bytes, error_detail = _read_cas_payload(
                effective_cas_root, artifact.ref, anchor=anchor
            )
            if raw_bytes is None:
                return AuthorityRefusal(
                    reason="source_unreadable",
                    detail=error_detail or f"cannot read CAS ref {artifact.ref!r}",
                )
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

        if artifact.digest != actual_digest and not artifact.ref.startswith("cas://sha256/"):
            return AuthorityRefusal(
                reason="ref_digest_parity_failed",
                detail=(
                    f"ref/digest parity failed for {artifact.ref!r}: "
                    f"declared {artifact.digest!r}, actual bytes {actual_digest!r}"
                ),
            )

        if admissibility is not None:
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
            bound_digests = [v for v in digests_dict.values() if v is not None]
            if artifact.digest not in bound_digests and actual_digest not in bound_digests:
                return AuthorityRefusal(
                    reason="receipt_digest_mismatch",
                    detail=(
                        f"artifact digest {artifact.digest!r} for {artifact.ref!r} is not "
                        f"present in trial {admissibility.trial_id!r} source digests: {bound_digests}"
                    ),
                )

        authority_digest = compute_authority_digest(
            artifact, "bytes-verified", verifier_implementation_digest, anchor
        )
        return ArtifactAuthority(
            artifact=artifact,
            anchor=anchor,
            level="bytes-verified",
            verifier_implementation_digest=verifier_implementation_digest,
            authority_digest=authority_digest,
        )

    return AuthorityRefusal(
        reason="authority_level_insufficient",
        detail=f"unrecognized authority level: {minimum_level!r}",
    )
