from __future__ import annotations

from evallab.recovery.bundle import (
    CommandOutcome,
    PackageInventory,
    ProcessInventory,
    build_recovery_bundle,
)
from evallab.recovery.wrapper import build_recovery_initial_prompt


def test_recovery_prompt_modes() -> None:
    commands = [
        CommandOutcome(
            index=0,
            command="mkdir test",
            exit_code=0,
            stdout_sha256="1",
            stderr_sha256="2",
        ),
        CommandOutcome(
            index=1,
            command="compile_app",
            exit_code=127,
            stdout_sha256="3",
            stderr_sha256="4",
        ),
    ]
    bundle, _ = build_recovery_bundle(
        task_id="task-01",
        task_digest="sha256:1",
        base_image="img",
        base_image_digest="sha256:2",
        verifier_digest="sha256:3",
        source_trial_id="trial-1",
        source_atif_path="p",
        source_atif_digest="sha256:4",
        step_cutoff=2,
        command_ledger=commands,
        file_entries=[],
        archive_bytes=b"bytes",
        package_inventory=PackageInventory(),
        process_inventory=ProcessInventory(),
        raw_env={},
    )

    base_instruction = "Fix the syntax error in main.py."
    assert build_recovery_initial_prompt(base_instruction, bundle, "none") == base_instruction

    summary = build_recovery_initial_prompt(base_instruction, bundle, "summary")
    assert "resuming an in-progress attempt" in summary
    assert "compile_app" in summary
    assert base_instruction in summary

    full = build_recovery_initial_prompt(base_instruction, bundle, "full")
    assert "[0] $ mkdir test (exit 0)" in full
    assert "[1] $ compile_app (exit 127)" in full
    assert base_instruction in full
