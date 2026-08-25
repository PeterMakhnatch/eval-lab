from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class RecoveryContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CommandOutcome(RecoveryContractModel):
    index: int
    command: str
    exit_code: int
    stdout_sha256: str
    stderr_sha256: str
    duration_ms: int = 0
    stdout_bytes: int = 0
    stderr_bytes: int = 0


class FileEntry(RecoveryContractModel):
    path: str
    mode: int
    size_bytes: int
    sha256: str
    is_dir: bool = False
    is_symlink: bool = False
    symlink_target: str | None = None


class FilesystemManifest(RecoveryContractModel):
    root: str
    entries: list[FileEntry]
    manifest_digest: str


class PackageInventory(RecoveryContractModel):
    python_packages: dict[str, str] = Field(default_factory=dict)
    os_packages: dict[str, str] = Field(default_factory=dict)
    npm_packages: dict[str, str] = Field(default_factory=dict)


class ProcessEntry(RecoveryContractModel):
    name: str
    cmdline: str
    status: Literal["restorable", "observational", "system"]
    restart_command: str | None = None
    pid: int | None = None


class ProcessInventory(RecoveryContractModel):
    processes: list[ProcessEntry] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    restorable_services: list[str] = Field(default_factory=list)
    has_unrestorable_processes: bool = False


class EnvConfig(RecoveryContractModel):
    allowlist: list[str] = Field(default_factory=list)
    redacted_keys: list[str] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)


class RecoveryStateBundle(RecoveryContractModel):
    schema_version: int = 1
    bundle_id: str
    task_id: str
    task_digest: str
    base_image: str
    base_image_digest: str
    verifier_digest: str
    source_trial_id: str
    source_atif_path: str
    source_atif_digest: str
    step_cutoff: int
    command_ledger: list[CommandOutcome]
    filesystem_manifest: FilesystemManifest
    filesystem_archive_sha256: str
    package_inventory: PackageInventory
    process_inventory: ProcessInventory
    env_config: EnvConfig
    created_at: str
    bundle_digest: str


def compute_bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_canonical_manifest_digest(entries: list[FileEntry]) -> str:
    sorted_entries = sorted(entries, key=lambda e: e.path)
    lines = []
    for e in sorted_entries:
        target = e.symlink_target or ""
        lines.append(
            f"{e.path}:{e.mode}:{e.size_bytes}:{e.sha256}:{e.is_dir}:{e.is_symlink}:{target}"
        )
    canonical_repr = "\n".join(lines)
    return hashlib.sha256(canonical_repr.encode("utf-8")).hexdigest()


def sanitize_and_redact_env(
    raw_env: dict[str, str],
    allowlist: list[str] | None = None,
    secret_patterns: list[str] | None = None,
) -> EnvConfig:
    default_allowlist = [
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "LANG",
        "LC_ALL",
        "TERM",
        "DEBIAN_FRONTEND",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "NODE_PATH",
    ]
    effective_allowlist = set(allowlist or default_allowlist)
    patterns = secret_patterns or [
        "KEY",
        "SECRET",
        "TOKEN",
        "AUTH",
        "PASS",
        "CREDENTIAL",
    ]

    filtered_env: dict[str, str] = {}
    redacted_keys: list[str] = []

    for k, v in raw_env.items():
        is_secret = any(p in k.upper() for p in patterns)
        if is_secret:
            redacted_keys.append(k)
            continue
        if k in effective_allowlist:
            filtered_env[k] = v

    return EnvConfig(
        allowlist=sorted(list(effective_allowlist)),
        redacted_keys=sorted(redacted_keys),
        environment=filtered_env,
    )


def compute_bundle_digest(payload: dict[str, Any]) -> str:
    copy_payload = {k: v for k, v in payload.items() if k != "bundle_digest"}
    serialized = json.dumps(copy_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_recovery_bundle(
    task_id: str,
    task_digest: str,
    base_image: str,
    base_image_digest: str,
    verifier_digest: str,
    source_trial_id: str,
    source_atif_path: str,
    source_atif_digest: str,
    step_cutoff: int,
    command_ledger: list[CommandOutcome],
    file_entries: list[FileEntry],
    archive_bytes: bytes,
    package_inventory: PackageInventory,
    process_inventory: ProcessInventory,
    raw_env: dict[str, str],
    bundle_id: str | None = None,
) -> tuple[RecoveryStateBundle, bytes]:
    archive_sha256 = compute_bytes_sha256(archive_bytes)
    manifest_digest = compute_canonical_manifest_digest(file_entries)
    fs_manifest = FilesystemManifest(
        root="/",
        entries=sorted(file_entries, key=lambda e: e.path),
        manifest_digest=manifest_digest,
    )
    env_config = sanitize_and_redact_env(raw_env)
    b_id = bundle_id or str(uuid4())
    now = datetime.now(UTC).isoformat()

    bundle_dict: dict[str, Any] = {
        "schema_version": 1,
        "bundle_id": b_id,
        "task_id": task_id,
        "task_digest": task_digest,
        "base_image": base_image,
        "base_image_digest": base_image_digest,
        "verifier_digest": verifier_digest,
        "source_trial_id": source_trial_id,
        "source_atif_path": source_atif_path,
        "source_atif_digest": source_atif_digest,
        "step_cutoff": step_cutoff,
        "command_ledger": [c.model_dump() for c in command_ledger],
        "filesystem_manifest": fs_manifest.model_dump(),
        "filesystem_archive_sha256": archive_sha256,
        "package_inventory": package_inventory.model_dump(),
        "process_inventory": process_inventory.model_dump(),
        "env_config": env_config.model_dump(),
        "created_at": now,
    }

    digest = compute_bundle_digest(bundle_dict)
    bundle_dict["bundle_digest"] = digest
    bundle = RecoveryStateBundle.model_validate(bundle_dict)
    return bundle, archive_bytes
