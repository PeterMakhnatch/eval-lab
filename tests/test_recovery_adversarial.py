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
    evaluate_package_equivalence,
    evaluate_process_and_service_state,
)


def test_adversarial_missing_dependency_fails():
    expected_pkg = PackageInventory(
        python_packages={"numpy": "1.26.0", "scipy": "1.12.0"},
        os_packages={"libgomp1": "12.2.0"},
    )
    actual_pkg = PackageInventory(
        python_packages={"numpy": "1.26.0"},  # Missing scipy
        os_packages={},  # Missing libgomp1
    )

    crit = evaluate_package_equivalence(expected_pkg, actual_pkg)
    assert crit.status == "FAIL"
    assert "Missing python: 1" in (crit.diff_summary or "")
    assert "os: 1" in (crit.diff_summary or "")


def test_adversarial_undeclared_or_unrestorable_process_is_unknown():
    proc_inv = ProcessInventory(
        processes=[
            ProcessEntry(name="untracked_worker", cmdline="./worker", status="observational")
        ],
        has_unrestorable_processes=True,
    )

    crit = evaluate_process_and_service_state(
        proc_inv, live_running_services=[], has_live_probe=False
    )
    assert crit.status == "UNKNOWN"
    assert "CRIU" in crit.reason


def test_adversarial_failed_service_restart():
    proc_inv = ProcessInventory(
        restorable_services=["postgresql", "nginx"],
        has_unrestorable_processes=False,
    )
    live_services = ["postgresql"]

    crit = evaluate_process_and_service_state(
        proc_inv, live_running_services=live_services, has_live_probe=True
    )
    assert crit.status == "FAIL"
    assert "nginx" in (crit.diff_summary or "")


def test_adversarial_idempotency_divergence_fails_certification():
    bundle, archive_bytes = build_recovery_bundle(
        task_id="task-idem",
        task_digest="sha256:1",
        base_image="img",
        base_image_digest="sha256:2",
        verifier_digest="sha256:3",
        source_trial_id="t-1",
        source_atif_path="p",
        source_atif_digest="sha256:4",
        step_cutoff=1,
        command_ledger=[],
        file_entries=[FileEntry(path="/app/f.txt", mode=0o644, size_bytes=10, sha256="hash1")],
        archive_bytes=b"ARCHIVE",
        package_inventory=PackageInventory(),
        process_inventory=ProcessInventory(),
        raw_env={},
    )

    call_count = 0

    def flaky_materialize_probe():
        nonlocal call_count
        call_count += 1
        file_hash = "hash1" if call_count == 1 else "hash2_diverged"
        entries = [FileEntry(path="/app/f.txt", mode=0o644, size_bytes=10, sha256=file_hash)]
        return entries, PackageInventory(), [], True

    cert = certify_state_restoration(
        bundle, archive_bytes, flaky_materialize_probe, test_idempotency=True
    )
    assert cert.overall_status == "FAIL"
    assert cert.idempotent_pass is False
    assert any(c.name == "idempotency_check" and c.status == "FAIL" for c in cert.criteria)
