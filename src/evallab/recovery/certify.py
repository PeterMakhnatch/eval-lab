from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from evallab.recovery.bundle import (
    FileEntry,
    FilesystemManifest,
    PackageInventory,
    ProcessInventory,
    RecoveryStateBundle,
    compute_bytes_sha256,
    compute_canonical_manifest_digest,
)


class CertificationCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    status: Literal["PASS", "FAIL", "UNKNOWN"]
    expected_digest: str | None = None
    actual_digest: str | None = None
    diff_summary: str | None = None
    reason: str = ""


class StateCertificate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    certificate_id: str
    bundle_id: str
    bundle_digest: str
    timestamp: str
    overall_status: Literal["PASS", "FAIL", "UNKNOWN"]
    criteria: list[CertificationCriterion]
    idempotent_pass: bool = False
    rejection_reason: str | None = None


def evaluate_archive_hash(
    bundle: RecoveryStateBundle,
    archive_bytes: bytes,
) -> CertificationCriterion:
    actual_hash = compute_bytes_sha256(archive_bytes)
    if actual_hash == bundle.filesystem_archive_sha256:
        return CertificationCriterion(
            name="archive_integrity",
            status="PASS",
            expected_digest=bundle.filesystem_archive_sha256,
            actual_digest=actual_hash,
            reason="Archive byte digest matches bundle specification.",
        )
    return CertificationCriterion(
        name="archive_integrity",
        status="FAIL",
        expected_digest=bundle.filesystem_archive_sha256,
        actual_digest=actual_hash,
        diff_summary=(
            f"Archive hash mismatch: expected {bundle.filesystem_archive_sha256}, got {actual_hash}"
        ),
        reason="Corrupted or altered filesystem archive.",
    )


def evaluate_manifest_equivalence(
    expected_manifest: FilesystemManifest,
    actual_entries: list[FileEntry],
) -> CertificationCriterion:
    actual_digest = compute_canonical_manifest_digest(actual_entries)
    if actual_digest == expected_manifest.manifest_digest:
        return CertificationCriterion(
            name="filesystem_equivalence",
            status="PASS",
            expected_digest=expected_manifest.manifest_digest,
            actual_digest=actual_digest,
            reason="Materialized filesystem is byte-identical to certified manifest.",
        )

    expected_map = {e.path: e for e in expected_manifest.entries}
    actual_map = {e.path: e for e in actual_entries}

    missing = set(expected_map.keys()) - set(actual_map.keys())
    extra = set(actual_map.keys()) - set(expected_map.keys())
    mismatched = [
        p
        for p in set(expected_map.keys()) & set(actual_map.keys())
        if expected_map[p].sha256 != actual_map[p].sha256
    ]

    diff_parts = []
    if missing:
        diff_parts.append(f"Missing {len(missing)} paths: {sorted(list(missing))[:5]}")
    if extra:
        diff_parts.append(f"Extra {len(extra)} paths: {sorted(list(extra))[:5]}")
    if mismatched:
        diff_parts.append(f"Mismatched content in {len(mismatched)} paths: {mismatched[:5]}")

    diff_summary = "; ".join(diff_parts) or "Manifest digest mismatch"

    return CertificationCriterion(
        name="filesystem_equivalence",
        status="FAIL",
        expected_digest=expected_manifest.manifest_digest,
        actual_digest=actual_digest,
        diff_summary=diff_summary,
        reason="Materialized container filesystem diverged from expected manifest.",
    )


def evaluate_package_equivalence(
    expected: PackageInventory,
    actual: PackageInventory,
) -> CertificationCriterion:
    missing_py = set(expected.python_packages.items()) - set(actual.python_packages.items())
    missing_os = set(expected.os_packages.items()) - set(actual.os_packages.items())
    missing_npm = set(expected.npm_packages.items()) - set(actual.npm_packages.items())

    if not missing_py and not missing_os and not missing_npm:
        return CertificationCriterion(
            name="package_environment",
            status="PASS",
            reason="All declared python, OS, and npm packages match exactly.",
        )

    diff = f"Missing python: {len(missing_py)}, os: {len(missing_os)}, npm: {len(missing_npm)}"
    return CertificationCriterion(
        name="package_environment",
        status="FAIL",
        diff_summary=diff,
        reason="Environment package inventory does not satisfy declared bundle dependencies.",
    )


def evaluate_process_and_service_state(
    inventory: ProcessInventory,
    live_running_services: list[str],
    has_live_probe: bool = True,
) -> CertificationCriterion:
    if inventory.has_unrestorable_processes or not has_live_probe:
        return CertificationCriterion(
            name="process_and_service_state",
            status="UNKNOWN",
            reason=(
                "Task contains in-memory process or service state that cannot be guaranteed "
                "restorable across platforms without CRIU checkpointing."
            ),
        )

    missing_services = set(inventory.restorable_services) - set(live_running_services)
    if missing_services:
        diff = f"Declared restorable services failed to restart: {sorted(list(missing_services))}"
        return CertificationCriterion(
            name="process_and_service_state",
            status="FAIL",
            diff_summary=diff,
            reason="Service rehydration failed to start declared background services.",
        )

    return CertificationCriterion(
        name="process_and_service_state",
        status="PASS",
        reason="All declared restorable services verified running.",
    )


def certify_state_restoration(
    bundle: RecoveryStateBundle,
    archive_bytes: bytes,
    materialize_probe_fn: Callable[[], tuple[list[FileEntry], PackageInventory, list[str], bool]],
    test_idempotency: bool = True,
) -> StateCertificate:
    now = datetime.now(UTC).isoformat()
    cert_id = str(uuid4())
    criteria: list[CertificationCriterion] = []

    # 1. Check archive byte integrity
    crit_archive = evaluate_archive_hash(bundle, archive_bytes)
    criteria.append(crit_archive)
    if crit_archive.status == "FAIL":
        return StateCertificate(
            certificate_id=cert_id,
            bundle_id=bundle.bundle_id,
            bundle_digest=bundle.bundle_digest,
            timestamp=now,
            overall_status="FAIL",
            criteria=criteria,
            rejection_reason=crit_archive.reason,
        )

    # 2. First materialization probe
    actual_entries_1, actual_pkg_1, actual_svc_1, live_probe_1 = materialize_probe_fn()

    crit_fs = evaluate_manifest_equivalence(bundle.filesystem_manifest, actual_entries_1)
    criteria.append(crit_fs)

    crit_pkg = evaluate_package_equivalence(bundle.package_inventory, actual_pkg_1)
    criteria.append(crit_pkg)

    crit_proc = evaluate_process_and_service_state(
        bundle.process_inventory, actual_svc_1, live_probe_1
    )
    criteria.append(crit_proc)

    # 3. Idempotency test (second materialization from same bundle)
    idempotent_pass = True
    if test_idempotency and crit_fs.status == "PASS":
        actual_entries_2, _, _, _ = materialize_probe_fn()
        digest_1 = compute_canonical_manifest_digest(actual_entries_1)
        digest_2 = compute_canonical_manifest_digest(actual_entries_2)
        if digest_1 != digest_2:
            idempotent_pass = False
            criteria.append(
                CertificationCriterion(
                    name="idempotency_check",
                    status="FAIL",
                    expected_digest=digest_1,
                    actual_digest=digest_2,
                    reason="Subsequent restoration produced non-deterministic filesystem state.",
                )
            )
        else:
            criteria.append(
                CertificationCriterion(
                    name="idempotency_check",
                    status="PASS",
                    expected_digest=digest_1,
                    actual_digest=digest_2,
                    reason="Subsequent restoration produced identical filesystem state.",
                )
            )

    # Determine overall status
    if any(c.status == "FAIL" for c in criteria) or not idempotent_pass:
        overall = "FAIL"
        rejection_reason = "; ".join(
            c.diff_summary or c.reason for c in criteria if c.status == "FAIL"
        )
    elif any(c.status == "UNKNOWN" for c in criteria):
        overall = "UNKNOWN"
        rejection_reason = "; ".join(c.reason for c in criteria if c.status == "UNKNOWN")
    else:
        overall = "PASS"
        rejection_reason = None

    return StateCertificate(
        certificate_id=cert_id,
        bundle_id=bundle.bundle_id,
        bundle_digest=bundle.bundle_digest,
        timestamp=now,
        overall_status=overall,
        criteria=criteria,
        idempotent_pass=idempotent_pass,
        rejection_reason=rejection_reason,
    )
