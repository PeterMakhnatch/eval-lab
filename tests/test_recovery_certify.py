from __future__ import annotations

from evallab.recovery.bundle import (
    FileEntry,
    PackageInventory,
    ProcessEntry,
    ProcessInventory,
    build_recovery_bundle,
)
from evallab.recovery.certify import (
    certify_state_restoration,
)


def test_certify_state_restoration_clean_pass():
    file_entries = [
        FileEntry(path="/app/code.py", mode=0o644, size_bytes=100, sha256="hash_code"),
    ]
    pkg_inv = PackageInventory(python_packages={"flask": "3.0.0"})
    proc_inv = ProcessInventory(restorable_services=["web-service"])
    archive_bytes = b"VALID_ARCHIVE_DATA"

    bundle, _ = build_recovery_bundle(
        task_id="task-01",
        task_digest="sha256:1",
        base_image="image:v1",
        base_image_digest="sha256:2",
        verifier_digest="sha256:3",
        source_trial_id="trial-1",
        source_atif_path="path",
        source_atif_digest="sha256:4",
        step_cutoff=1,
        command_ledger=[],
        file_entries=file_entries,
        archive_bytes=archive_bytes,
        package_inventory=pkg_inv,
        process_inventory=proc_inv,
        raw_env={},
    )

    def probe_fn():
        return file_entries, pkg_inv, ["web-service"], True

    cert = certify_state_restoration(bundle, archive_bytes, probe_fn, test_idempotency=True)
    assert cert.overall_status == "PASS"
    assert cert.idempotent_pass is True
    assert cert.rejection_reason is None


def test_certify_state_restoration_archive_corruption():
    file_entries = [
        FileEntry(path="/app/code.py", mode=0o644, size_bytes=100, sha256="hash_code")
    ]
    bundle, _ = build_recovery_bundle(
        task_id="task-01",
        task_digest="sha256:1",
        base_image="image:v1",
        base_image_digest="sha256:2",
        verifier_digest="sha256:3",
        source_trial_id="trial-1",
        source_atif_path="path",
        source_atif_digest="sha256:4",
        step_cutoff=1,
        command_ledger=[],
        file_entries=file_entries,
        archive_bytes=b"ORIGINAL_BYTES",
        package_inventory=PackageInventory(),
        process_inventory=ProcessInventory(),
        raw_env={},
    )

    corrupted_bytes = b"CORRUPTED_BYTES"
    cert = certify_state_restoration(
        bundle, corrupted_bytes, lambda: (file_entries, PackageInventory(), [], True)
    )
    assert cert.overall_status == "FAIL"
    assert "Corrupted or altered filesystem archive" in (cert.rejection_reason or "")


def test_certify_state_restoration_manifest_divergence():
    expected_entries = [
        FileEntry(path="/app/code.py", mode=0o644, size_bytes=100, sha256="hash_original"),
    ]
    bundle, archive_bytes = build_recovery_bundle(
        task_id="task-01",
        task_digest="sha256:1",
        base_image="image:v1",
        base_image_digest="sha256:2",
        verifier_digest="sha256:3",
        source_trial_id="trial-1",
        source_atif_path="path",
        source_atif_digest="sha256:4",
        step_cutoff=1,
        command_ledger=[],
        file_entries=expected_entries,
        archive_bytes=b"BYTES",
        package_inventory=PackageInventory(),
        process_inventory=ProcessInventory(),
        raw_env={},
    )

    diverged_entries = [
        FileEntry(path="/app/code.py", mode=0o644, size_bytes=100, sha256="hash_DIFFERENT"),
    ]

    cert = certify_state_restoration(
        bundle, archive_bytes, lambda: (diverged_entries, PackageInventory(), [], True)
    )
    assert cert.overall_status == "FAIL"
    assert "Mismatched content" in (cert.rejection_reason or "")


def test_certify_state_restoration_unrestorable_process_unknown():
    file_entries = [
        FileEntry(path="/app/code.py", mode=0o644, size_bytes=100, sha256="hash_code")
    ]
    proc_inv = ProcessInventory(
        processes=[
            ProcessEntry(name="daemon", cmdline="./daemon", status="observational", pid=99)
        ],
        has_unrestorable_processes=True,
    )
    bundle, archive_bytes = build_recovery_bundle(
        task_id="task-01",
        task_digest="sha256:1",
        base_image="image:v1",
        base_image_digest="sha256:2",
        verifier_digest="sha256:3",
        source_trial_id="trial-1",
        source_atif_path="path",
        source_atif_digest="sha256:4",
        step_cutoff=1,
        command_ledger=[],
        file_entries=file_entries,
        archive_bytes=b"BYTES",
        package_inventory=PackageInventory(),
        process_inventory=proc_inv,
        raw_env={},
    )

    cert = certify_state_restoration(
        bundle, archive_bytes, lambda: (file_entries, PackageInventory(), [], True)
    )
    assert cert.overall_status == "UNKNOWN"
    assert "CRIU" in (cert.rejection_reason or "")
