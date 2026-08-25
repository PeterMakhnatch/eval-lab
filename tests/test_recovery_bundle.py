from __future__ import annotations

from evallab.recovery.bundle import (
    CommandOutcome,
    FileEntry,
    PackageInventory,
    ProcessInventory,
    build_recovery_bundle,
    compute_bundle_digest,
    compute_bytes_sha256,
    compute_canonical_manifest_digest,
)


def test_build_recovery_bundle_deterministic_digest():
    file_entries = [
        FileEntry(path="/app/main.py", mode=0o644, size_bytes=120, sha256="abc1234"),
        FileEntry(path="/app/config.json", mode=0o644, size_bytes=50, sha256="def5678"),
    ]
    commands = [
        CommandOutcome(
            index=0,
            command="echo start",
            exit_code=0,
            stdout_sha256="111",
            stderr_sha256="222",
        ),
        CommandOutcome(
            index=1,
            command="python main.py",
            exit_code=1,
            stdout_sha256="333",
            stderr_sha256="444",
        ),
    ]
    raw_env = {
        "PATH": "/usr/bin:/bin",
        "SECRET_API_KEY": "supersecrettoken",
        "DATABASE_PASSWORD": "password123",
        "LANG": "en_US.UTF-8",
    }
    archive_bytes = b"MOCK_ARCHIVE_DATA_12345"

    bundle, _ = build_recovery_bundle(
        task_id="task-test-01",
        task_digest="sha256:taskdigest123",
        base_image="ghcr.io/eval-lab/test-image:v1",
        base_image_digest="sha256:imagedigest123",
        verifier_digest="sha256:verifierdigest123",
        source_trial_id="trial-orig-01",
        source_atif_path="runs/trial-orig-01/trajectory.json",
        source_atif_digest="sha256:atifdigest123",
        step_cutoff=2,
        command_ledger=commands,
        file_entries=file_entries,
        archive_bytes=archive_bytes,
        package_inventory=PackageInventory(python_packages={"pytest": "8.0.0"}),
        process_inventory=ProcessInventory(),
        raw_env=raw_env,
        bundle_id="bnd-fixed-test-uuid",
    )

    assert bundle.bundle_id == "bnd-fixed-test-uuid"
    assert bundle.filesystem_archive_sha256 == compute_bytes_sha256(archive_bytes)
    assert len(bundle.command_ledger) == 2
    assert bundle.step_cutoff == 2

    # Verify secret redaction
    assert "SECRET_API_KEY" not in bundle.env_config.environment
    assert "DATABASE_PASSWORD" not in bundle.env_config.environment
    assert "SECRET_API_KEY" in bundle.env_config.redacted_keys
    assert "DATABASE_PASSWORD" in bundle.env_config.redacted_keys
    assert bundle.env_config.environment["PATH"] == "/usr/bin:/bin"

    # Verify deterministic manifest digest regardless of entry input order
    shuffled_entries = list(reversed(file_entries))
    expected_digest = bundle.filesystem_manifest.manifest_digest
    assert compute_canonical_manifest_digest(shuffled_entries) == expected_digest


def test_bundle_digest_tamper_detection():
    file_entries = [FileEntry(path="/app/file.txt", mode=0o644, size_bytes=10, sha256="hash1")]
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
        archive_bytes=b"data",
        package_inventory=PackageInventory(),
        process_inventory=ProcessInventory(),
        raw_env={},
        bundle_id="bnd-01",
    )

    bundle_dict = bundle.model_dump()
    assert compute_bundle_digest(bundle_dict) == bundle.bundle_digest

    # Tamper with a field
    bundle_dict["step_cutoff"] = 999
    assert compute_bundle_digest(bundle_dict) != bundle.bundle_digest
