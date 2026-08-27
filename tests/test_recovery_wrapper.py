from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb

from evallab.evidence.facts import project_recovery_facts
from evallab.recovery.bundle import (
    CommandOutcome,
    PackageInventory,
    ProcessInventory,
    build_recovery_bundle,
)
from evallab.recovery.certify import certify_state_restoration
from evallab.recovery.pilot import run_bounded_recovery_pilot
from evallab.recovery.wrapper import (
    RecoveryTrialConfig,
    build_recovery_initial_prompt,
    evaluate_paired_recovery_trial,
)


def test_recovery_prompt_modes():
    commands = [
        CommandOutcome(
            index=0, command="mkdir test", exit_code=0, stdout_sha256="1", stderr_sha256="2"
        ),
        CommandOutcome(
            index=1, command="compile_app", exit_code=127, stdout_sha256="3", stderr_sha256="4"
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

    base_instr = "Fix the syntax error in main.py."

    # None mode: untouched base instruction
    prompt_none = build_recovery_initial_prompt(base_instr, bundle, "none")
    assert prompt_none == base_instr

    # Summary mode: includes failure count and failing commands
    prompt_summary = build_recovery_initial_prompt(base_instr, bundle, "summary")
    assert "resuming an in-progress attempt" in prompt_summary
    assert "compile_app" in prompt_summary
    assert base_instr in prompt_summary

    # Full mode: contains entire indexed command ledger
    prompt_full = build_recovery_initial_prompt(base_instr, bundle, "full")
    assert "[0] $ mkdir test (exit 0)" in prompt_full
    assert "[1] $ compile_app (exit 127)" in prompt_full
    assert base_instr in prompt_full


def test_paired_recovery_trial_and_facts_projection():
    bundle, archive_bytes = build_recovery_bundle(
        task_id="task-recovery-01",
        task_digest="sha256:task",
        base_image="img:v1",
        base_image_digest="sha256:img",
        verifier_digest="sha256:ver",
        source_trial_id="initial-trial-99",
        source_atif_path="path",
        source_atif_digest="sha256:atif",
        step_cutoff=3,
        command_ledger=[],
        file_entries=[],
        archive_bytes=b"bytes",
        package_inventory=PackageInventory(),
        process_inventory=ProcessInventory(),
        raw_env={},
    )

    cert = certify_state_restoration(
        bundle, archive_bytes, lambda: ([], PackageInventory(), [], True)
    )
    assert cert.overall_status == "PASS"

    config = RecoveryTrialConfig(
        task_id="task-recovery-01",
        bundle=bundle,
        certificate=cert,
        message_mode="summary",
        agent_name="codex-luna",
        agent_model="gpt-5.6-luna",
    )

    initial_metrics = {
        "reward": 0.0,
        "cost_usd": 0.15,
        "input_tokens": 15000,
        "output_tokens": 500,
        "steps": 3,
    }

    outcome = evaluate_paired_recovery_trial(
        config=config,
        base_instruction="Complete task",
        initial_trial_metrics=initial_metrics,
    )

    assert outcome.initial_trial_id == "initial-trial-99"
    assert outcome.final_recovery_reward == 1.0
    assert outcome.recovery_success is True
    assert outcome.initial_cost_usd == 0.15
    assert outcome.recovery_cost_usd == 0.05
    assert outcome.total_cost_usd == 0.20
    assert outcome.certificate_status == "PASS"

    # Test Parquet facts projection and DuckDB querying
    with TemporaryDirectory() as tmpdir:
        parquet_path = Path(tmpdir) / "recovery_facts.parquet"
        project_recovery_facts([outcome], parquet_path)
        assert parquet_path.exists()

        con = duckdb.connect()
        rows = con.execute(
            f"SELECT task_id, recovery_success, total_cost_usd FROM '{parquet_path}'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0] == ("task-recovery-01", True, 0.20)


def test_bounded_pilot_execution():
    report = run_bounded_recovery_pilot()
    assert report.total_tasks == 3
    assert "file-only" in report.certified_lanes
    assert "package-config" in report.certified_lanes
    assert "service-process" in report.blocked_lanes

    # Service-process lane must have honest blocker
    service_result = next(r for r in report.results if r.task_class == "service-process")
    assert service_result.blocker_issued is True
    assert "CRIU" in (service_result.blocker_reason or "")
