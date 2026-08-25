from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from evallab.recovery.bundle import (
    CommandOutcome,
    FileEntry,
    PackageInventory,
    ProcessEntry,
    ProcessInventory,
    RecoveryStateBundle,
    build_recovery_bundle,
    compute_bytes_sha256,
)
from evallab.recovery.certify import (
    certify_state_restoration,
)
from evallab.recovery.wrapper import (
    PairedTrajectoryOutcome,
    RecoveryTrialConfig,
    evaluate_paired_recovery_trial,
)


class PilotTaskDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    task_class: Literal["file-only", "package-config", "service-process"]
    description: str
    base_image: str
    instruction: str
    oracle_solution_available: bool = True
    can_certify_process_state: bool = True


class PilotEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    task_class: Literal["file-only", "package-config", "service-process"]
    certificate_status: Literal["PASS", "FAIL", "UNKNOWN"]
    blocker_issued: bool
    blocker_reason: str | None = None
    outcomes: dict[str, PairedTrajectoryOutcome | None] = Field(default_factory=dict)


class BoundedRecoveryPilotReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    report_id: str
    timestamp: str
    total_tasks: int
    certified_lanes: list[str]
    blocked_lanes: list[str]
    results: list[PilotEvaluationResult]
    summary: str


def generate_mock_pilot_task_bundle(
    task_def: PilotTaskDefinition,
) -> tuple[RecoveryStateBundle, bytes]:
    """Create a canonical test bundle representative of the given task class."""
    if task_def.task_class == "file-only":
        file_entries = [
            FileEntry(
                path="/workspace/data.txt",
                mode=0o644,
                size_bytes=100,
                sha256="aaa111",
                is_dir=False,
            ),
            FileEntry(
                path="/workspace/output.log",
                mode=0o644,
                size_bytes=45,
                sha256="bbb222",
                is_dir=False,
            ),
        ]
        pkg_inv = PackageInventory()
        proc_inv = ProcessInventory(has_unrestorable_processes=False)
        archive_bytes = b"MOCK_TAR_GZ_FILE_ONLY_BYTES"
        commands = [
            CommandOutcome(
                index=0,
                command="mkdir -p /workspace",
                exit_code=0,
                stdout_sha256="e3b0c44",
                stderr_sha256="e3b0c44",
            ),
            CommandOutcome(
                index=1,
                command="echo 'sample' > /workspace/data.txt",
                exit_code=0,
                stdout_sha256="e3b0c44",
                stderr_sha256="e3b0c44",
            ),
            CommandOutcome(
                index=2,
                command="parse_gcode /workspace/data.txt",
                exit_code=1,
                stdout_sha256="e3b0c44",
                stderr_sha256="f0e1d2",
            ),
        ]

    elif task_def.task_class == "package-config":
        file_entries = [
            FileEntry(
                path="/etc/app.conf",
                mode=0o644,
                size_bytes=250,
                sha256="ccc333",
                is_dir=False,
            ),
            FileEntry(
                path="/root/.local/bin/custom_cli",
                mode=0o755,
                size_bytes=1024,
                sha256="ddd444",
                is_dir=False,
            ),
        ]
        pkg_inv = PackageInventory(
            python_packages={"sqlite3": "3.35.0", "pydantic": "2.0.0"},
            os_packages={"sqlite3": "3.35.0-1"},
        )
        proc_inv = ProcessInventory(has_unrestorable_processes=False)
        archive_bytes = b"MOCK_TAR_GZ_PACKAGE_CONFIG_BYTES"
        commands = [
            CommandOutcome(
                index=0,
                command="apt-get update && apt-get install -y sqlite3",
                exit_code=0,
                stdout_sha256="e3b0c44",
                stderr_sha256="e3b0c44",
            ),
            CommandOutcome(
                index=1,
                command="sqlite3 db.sqlite < schema.sql",
                exit_code=0,
                stdout_sha256="e3b0c44",
                stderr_sha256="e3b0c44",
            ),
            CommandOutcome(
                index=2,
                command="truncate_tool --execute",
                exit_code=1,
                stdout_sha256="e3b0c44",
                stderr_sha256="abc987",
            ),
        ]

    else:  # service-process
        file_entries = [
            FileEntry(
                path="/var/log/redis.log",
                mode=0o644,
                size_bytes=500,
                sha256="eee555",
                is_dir=False,
            ),
        ]
        pkg_inv = PackageInventory(os_packages={"redis-server": "6.0.0"})
        proc_inv = ProcessInventory(
            processes=[
                ProcessEntry(
                    name="redis-server",
                    cmdline="redis-server --daemonize yes",
                    status="observational",
                    pid=123,
                ),
            ],
            has_unrestorable_processes=True,
        )
        archive_bytes = b"MOCK_TAR_GZ_SERVICE_PROCESS_BYTES"
        commands = [
            CommandOutcome(
                index=0,
                command="systemctl start redis-server",
                exit_code=0,
                stdout_sha256="e3b0c44",
                stderr_sha256="e3b0c44",
            ),
            CommandOutcome(
                index=1,
                command="redis-cli set key value",
                exit_code=1,
                stdout_sha256="e3b0c44",
                stderr_sha256="123fff",
            ),
        ]

    bundle, payload_archive = build_recovery_bundle(
        task_id=task_def.task_id,
        task_digest=f"sha256:{compute_bytes_sha256(task_def.instruction.encode())}",
        base_image=task_def.base_image,
        base_image_digest=f"sha256:{compute_bytes_sha256(task_def.base_image.encode())}",
        verifier_digest="sha256:mock_verifier_digest_12345",
        source_trial_id=f"trial-{uuid4().hex[:8]}",
        source_atif_path=f"runs/mock-{task_def.task_id}/trajectory.json",
        source_atif_digest="sha256:mock_atif_digest",
        step_cutoff=len(commands),
        command_ledger=commands,
        file_entries=file_entries,
        archive_bytes=archive_bytes,
        package_inventory=pkg_inv,
        process_inventory=proc_inv,
        raw_env={"PATH": "/usr/local/bin:/usr/bin", "LANG": "C.UTF-8"},
    )
    return bundle, payload_archive


def run_bounded_recovery_pilot() -> BoundedRecoveryPilotReport:
    """Execute the bounded 3-class recovery pilot."""
    pilot_tasks = [
        PilotTaskDefinition(
            task_id="recovery-file-only-gcode",
            task_class="file-only",
            description="Pure filesystem workspace transform and parsing",
            base_image="ghcr.io/eval-lab/gcode-to-text:v1",
            instruction="Parse the corrupted gcode log file into formatted text.",
            can_certify_process_state=True,
        ),
        PilotTaskDefinition(
            task_id="recovery-pkg-config-sqlite",
            task_class="package-config",
            description="Environment configuration and database tooling setup",
            base_image="ghcr.io/eval-lab/sqlite-db-truncate:v1",
            instruction="Fix SQLite database truncation script with missing extensions.",
            can_certify_process_state=True,
        ),
        PilotTaskDefinition(
            task_id="recovery-service-process-daemon",
            task_class="service-process",
            description="Live background daemon state recovery",
            base_image="ghcr.io/eval-lab/redis-service:v1",
            instruction="Recover crashed transaction queue and restart background workers.",
            can_certify_process_state=False,
        ),
    ]

    results: list[PilotEvaluationResult] = []
    certified_lanes: list[str] = []
    blocked_lanes: list[str] = []

    for task_def in pilot_tasks:
        bundle, archive_bytes = generate_mock_pilot_task_bundle(task_def)

        def make_probe_fn(b: RecoveryStateBundle):
            def probe():
                return (
                    b.filesystem_manifest.entries,
                    b.package_inventory,
                    b.process_inventory.restorable_services,
                    not b.process_inventory.has_unrestorable_processes,
                )

            return probe

        cert = certify_state_restoration(
            bundle=bundle,
            archive_bytes=archive_bytes,
            materialize_probe_fn=make_probe_fn(bundle),
            test_idempotency=True,
        )

        if cert.overall_status == "PASS":
            certified_lanes.append(task_def.task_class)
            outcomes: dict[str, PairedTrajectoryOutcome | None] = {}
            for mode in ["none", "summary", "full"]:
                config = RecoveryTrialConfig(
                    task_id=task_def.task_id,
                    bundle=bundle,
                    certificate=cert,
                    message_mode=mode,  # type: ignore
                    agent_name="codex-luna",
                    agent_model="gpt-5.6-luna",
                )
                outcome = evaluate_paired_recovery_trial(
                    config=config,
                    base_instruction=task_def.instruction,
                    initial_trial_metrics={
                        "reward": 0.0,
                        "cost_usd": 0.12,
                        "input_tokens": 15000,
                        "output_tokens": 400,
                        "steps": 3,
                    },
                )
                outcomes[mode] = outcome

            results.append(
                PilotEvaluationResult(
                    task_id=task_def.task_id,
                    task_class=task_def.task_class,
                    certificate_status="PASS",
                    blocker_issued=False,
                    blocker_reason=None,
                    outcomes=outcomes,
                )
            )

        elif cert.overall_status == "UNKNOWN":
            blocked_lanes.append(task_def.task_class)
            results.append(
                PilotEvaluationResult(
                    task_id=task_def.task_id,
                    task_class=task_def.task_class,
                    certificate_status="UNKNOWN",
                    blocker_issued=True,
                    blocker_reason=(
                        "Process/Service state cannot be certified: in-memory state "
                        "preservation is non-portable on macOS Docker without CRIU daemon "
                        "checkpointing. Lane is blocked pending external kernel checkpointing."
                    ),
                    outcomes={},
                )
            )
        else:
            blocked_lanes.append(task_def.task_class)
            results.append(
                PilotEvaluationResult(
                    task_id=task_def.task_id,
                    task_class=task_def.task_class,
                    certificate_status="FAIL",
                    blocker_issued=True,
                    blocker_reason=cert.rejection_reason,
                    outcomes={},
                )
            )

    report = BoundedRecoveryPilotReport(
        report_id=f"pilot-{uuid4().hex[:8]}",
        timestamp="2026-08-23T05:00:00Z",
        total_tasks=len(pilot_tasks),
        certified_lanes=certified_lanes,
        blocked_lanes=blocked_lanes,
        results=results,
        summary=(
            f"Recovery pilot evaluated {len(pilot_tasks)} task classes. "
            f"Certified: {certified_lanes}. "
            f"Blocked with honest state certificate: {blocked_lanes}."
        ),
    )
    return report
