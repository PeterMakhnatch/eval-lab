from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from evallab.recovery.bundle import (
    CommandOutcome,
    FileEntry,
    PackageInventory,
    ProcessInventory,
    build_recovery_bundle,
    compute_bytes_sha256,
)
from evallab.recovery.certify import certify_state_restoration
from evallab.recovery.wrapper import build_recovery_initial_prompt

AUDIT_ROOT = Path("/tmp/recovery-replay-audit")


def test_real_gcode_to_text_audit_bundle_and_certification():
    """Exercise bundle creation and certification on real gcode-to-text audit trace."""
    run_dir = AUDIT_ROOT / "runs" / "ee524a8f-gcode-to-text__Rb675EN" / "run-03"
    if not run_dir.exists():
        pytest.skip(f"Real audit artifacts not found at {run_dir}")

    # 1. Parse real command outcomes
    outcomes_path = run_dir / "command-outcomes.json"
    with open(outcomes_path) as f:
        raw_outcomes = json.load(f)

    command_ledger: list[CommandOutcome] = []
    for item in raw_outcomes:
        command_ledger.append(
            CommandOutcome(
                index=item["index"],
                command=item["command"],
                exit_code=item["exit_code"],
                stdout_sha256=item["stdout_artifact"]["sha256"],
                stderr_sha256=item["stderr_artifact"]["sha256"],
                duration_ms=int(item.get("duration_ms", 0)),
                stdout_bytes=item["stdout_artifact"].get("size_bytes", 0),
                stderr_bytes=item["stderr_artifact"].get("size_bytes", 0),
            )
        )

    assert len(command_ledger) == 17
    assert all(c.exit_code == 0 for c in command_ledger)

    # 2. Parse real filesystem TSV manifest
    fs_tsv_path = run_dir / "manifest" / "post-raw" / "filesystem.tsv"
    file_entries: list[FileEntry] = []
    if fs_tsv_path.exists():
        with open(fs_tsv_path) as f:
            lines = [line.strip() for line in f if line.strip()]
        i = 0
        while i < len(lines):
            line = lines[i]
            parts = line.split("\t")
            entry_type = parts[0]
            if entry_type in ("file", "dir", "symlink") and len(parts) >= 2:
                path = parts[1]
                mode = int(parts[2], 8) if len(parts) > 2 and parts[2].isdigit() else 0o644
                size = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
                target = parts[6] if len(parts) > 6 and entry_type == "symlink" else None
                sha = "e3b0c44"
                # Check if next line has hash
                if i + 1 < len(lines):
                    next_parts = lines[i + 1].split("\t")
                    if len(next_parts) >= 2 and len(next_parts[1]) == 64:
                        sha = next_parts[1]
                        i += 1
                file_entries.append(
                    FileEntry(
                        path=path,
                        mode=mode,
                        size_bytes=size,
                        sha256=sha,
                        is_dir=(entry_type == "dir"),
                        is_symlink=(entry_type == "symlink"),
                        symlink_target=target,
                    )
                )
            i += 1

    assert len(file_entries) > 0

    # 3. Parse real python packages
    pkg_json_path = run_dir / "manifest" / "post-raw" / "python-packages.json"
    py_pkgs = {}
    if pkg_json_path.exists():
        with open(pkg_json_path) as f:
            try:
                py_data = json.load(f)
                if isinstance(py_data, list):
                    for p in py_data:
                        if isinstance(p, dict) and "name" in p:
                            py_pkgs[p["name"]] = p.get("version", "1.0")
            except json.JSONDecodeError:
                pass

    pkg_inventory = PackageInventory(python_packages=py_pkgs)
    proc_inventory = ProcessInventory(has_unrestorable_processes=False)

    # 4. Create real tar.gz archive in memory
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz") as tar:
        # Add app/text.gcode simulation file
        info = tarfile.TarInfo(name="app/text.gcode")
        data = b"G0 X0 Y0\nG1 X10 Y10 E5\n"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    archive_bytes = tar_buf.getvalue()

    # 5. Build bundle
    bundle, _ = build_recovery_bundle(
        task_id="gcode-to-text",
        task_digest="sha256:5531de29b9b445e4cd67b66231d34cb9e7bddcf15f4c2574b7376a19f0e4c339",
        base_image="alexgshaw/gcode-to-text",
        base_image_digest="sha256:0979ef40c6a3e8c4e7ab5b6c2524c74625a84799e15cd45dd98dcf83b11efd4d",
        verifier_digest="sha256:verifier_gcode_to_text",
        source_trial_id="ee524a8f-gcode-to-text__Rb675EN",
        source_atif_path="traces/ee524a8f-gcode-to-text__Rb675EN/trajectory.json",
        source_atif_digest="sha256:f149abf5784b574ec6eea385d0f547a8d456b43eae841530060741ff63b5c240",
        step_cutoff=17,
        command_ledger=command_ledger,
        file_entries=file_entries,
        archive_bytes=archive_bytes,
        package_inventory=pkg_inventory,
        process_inventory=proc_inventory,
        raw_env={"PATH": "/usr/local/bin:/usr/bin", "LANG": "C.UTF-8"},
    )

    assert bundle.task_id == "gcode-to-text"
    assert bundle.step_cutoff == 17
    assert bundle.filesystem_archive_sha256 == compute_bytes_sha256(archive_bytes)

    # 6. Verify certification passes with matching probe
    def real_probe_fn():
        return file_entries, pkg_inventory, [], True

    cert = certify_state_restoration(bundle, archive_bytes, real_probe_fn, test_idempotency=True)
    assert cert.overall_status == "PASS"
    assert cert.idempotent_pass is True

    # 7. Verify prompt generation
    prompt = build_recovery_initial_prompt(
        base_instruction="Convert G-code to text representation.",
        bundle=bundle,
        message_mode="summary",
    )
    assert "resuming an in-progress attempt on this task at step 17" in prompt
    assert "Convert G-code to text representation." in prompt
