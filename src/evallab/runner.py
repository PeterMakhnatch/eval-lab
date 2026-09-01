from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
from contextlib import ExitStack, suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from evallab.evidence_store import (
    EvidenceArchive,
    EvidenceLocator,
    archive_evidence,
    evidence_locator,
    reopen_evidence_archive,
)
from evallab.execution_contracts import (
    _SUBSCRIPTION_ENVIRONMENT_KEYS,
    CONTROL_AGENTS,
    DEEPSEEK_ALLOWED_MODEL,
    DEEPSEEK_ALLOWED_MODEL_ENV,
    DEEPSEEK_CREDENTIAL_ENVIRONMENT_KEYS,
    DEEPSEEK_MODEL_SELECTOR,
    DEEPSEEK_PROXY_ATTEMPT_ID_ENV,
    DEEPSEEK_PROXY_CAPABILITY_ENV,
    DEEPSEEK_PROXY_GID_ENV,
    DEEPSEEK_PROXY_HOST,
    DEEPSEEK_PROXY_SCRIPT,
    DEEPSEEK_PROXY_SCRIPT_ENV,
    DEEPSEEK_PROXY_UID_ENV,
    DEEPSEEK_PROXY_USAGE_DIR_ENV,
    DEEPSEEK_PROXY_USAGE_FILE_ENV,
    DEEPSEEK_SECRET_FILE_ENV,
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_TRIAL_TIMEOUT_SECONDS,
    HARBOR_AGENT_IMPORT_PATHS,
    HARBOR_STATE_JOURNAL_PLUGIN,
    LOCAL_TO_HARBOR_MODEL,
    MAX_TRIAL_TIMEOUT_SECONDS,
    REDACTED_SECRET_VALUE,
    SUPPORT_COMMAND_TIMEOUT_SECONDS,
    WATCHDOG_POLL_SECONDS,
    ZAI_CAPABILITY_EXPIRES_AT_ENV,
    ZAI_INPUT_COST_MICROS_PER_MILLION,
    ZAI_OPENCODE_AGENT,
    ZAI_OUTPUT_COST_MICROS_PER_MILLION,
    ZAI_PROXY_ATTEMPT_ID_ENV,
    ZAI_PROXY_CAPABILITY_ENV,
    ZAI_PROXY_GID_ENV,
    ZAI_PROXY_HOST,
    ZAI_PROXY_SCRIPT,
    ZAI_PROXY_SCRIPT_ENV,
    ZAI_PROXY_UID_ENV,
    ZAI_PROXY_USAGE_DIR_ENV,
    ZAI_PROXY_USAGE_FILE_ENV,
    ZAI_SECRET_FILE_ENV,
    ExecutionFailure,
    HarborProcessResult,
    ProxyTrialLimits,
    RedactingBinaryWriter,
    RunRequest,
    TransientHarnessFailure,
    TrialTimeoutFailure,
    build_command,
    collected_secret_values,
    is_lease_generation,
    materialize_deepseek_secret_file,
    materialize_zai_secret_file,
    persist_private_bytes,
    proxy_runtime_identity,
    read_owner_secret_file,
    redact_environment,
    resolve_harbor_agent,
    resolve_harbor_model,
    subscription_command,
    subscription_environment,
    transient_provider_exception,
    transient_provider_reason,
    validate_request,
)
from evallab.harbor_network import (
    NetworkAdaptation,
    adapt_task_toml_for_host,
    with_agent_network_allowlist,
)
from evallab.results import load_job
from evallab.schemas import ExperimentMatrix, MatrixRun

__all__ = [
    "_SUBSCRIPTION_ENVIRONMENT_KEYS",
    "CONTROL_AGENTS",
    "DEEPSEEK_CREDENTIAL_ENVIRONMENT_KEYS",
    "DEEPSEEK_MODEL_SELECTOR",
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_TRIAL_TIMEOUT_SECONDS",
    "HARBOR_AGENT_IMPORT_PATHS",
    "HARBOR_STATE_JOURNAL_PLUGIN",
    "LOCAL_TO_HARBOR_MODEL",
    "MAX_TRIAL_TIMEOUT_SECONDS",
    "REDACTED_SECRET_VALUE",
    "SUPPORT_COMMAND_TIMEOUT_SECONDS",
    "WATCHDOG_POLL_SECONDS",
    "HarborProcessResult",
    "HarborRuntimeIdentity",
    "SettledRun",
    "RunRequest",
    "build_command",
    "cleanup_new_harbor_containers",
    "database_url_from_environment",
    "expected_primary_reward",
    "executor_state_path",
    "git_state",
    "harbor_container_ids",
    "load_matrix",
    "preflight_request",
    "profile_for_request",
    "redact_environment",
    "request_from_matrix",
    "resolve_harbor_agent",
    "resolve_harbor_model",
    "resolve_harbor_runtime_identity",
    "run_experiment",
    "run_harbor_process",
    "subscription_command",
    "subscription_environment",
    "transient_provider_exception",
    "transient_provider_reason",
]
HARBOR_COMPOSE_CONFIG_LABEL = "com.docker.compose.project.config_files"
HARBOR_COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
HARBOR_COMPOSE_WORKDIR_LABEL = "com.docker.compose.project.working_dir"

_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


@dataclass(frozen=True)
class HarborRuntimeIdentity:
    """The declared lock identity and executable selected for one run."""

    declared_version: str
    actual_version: str
    executable_path: Path
    executable_digest: str
    executable_device: int
    executable_inode: int
    executable_size: int
    executable_mtime_ns: int


@dataclass(frozen=True)
class SettledRun:
    """A completed run whose only downstream authority is a CAS locator."""

    cas_locator: EvidenceLocator
    cas_record: EvidenceArchive


def _run_text_command(
    command: list[str],
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, **kwargs)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _is_harbor_container(
    labels: dict[str, str],
    task_root: Path,
    project_prefixes: frozenset[str] | None,
) -> bool:
    config_files = labels.get(HARBOR_COMPOSE_CONFIG_LABEL, "")
    working_dir = labels.get(HARBOR_COMPOSE_WORKDIR_LABEL, "")
    project = labels.get(HARBOR_COMPOSE_PROJECT_LABEL, "")
    project_matches = project_prefixes is None or any(
        project == prefix or project.startswith(prefix + "__") for prefix in project_prefixes
    )
    return bool(
        project
        and project_matches
        and "/harbor/environments/docker/" in config_files.replace("\\", "/")
        and working_dir
        and _is_under(Path(working_dir), task_root)
    )


def harbor_container_ids(
    task_root: Path,
    *,
    project_prefixes: frozenset[str] | None = None,
    command_runner: Any = None,
) -> frozenset[str]:
    """Return only containers proven by labels to belong to Harbor for this task."""
    runner = command_runner or _run_text_command
    environment = subscription_environment()
    try:
        listed = runner(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label={HARBOR_COMPOSE_PROJECT_LABEL}",
                "--filter",
                f"label={HARBOR_COMPOSE_CONFIG_LABEL}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=SUPPORT_COMMAND_TIMEOUT_SECONDS,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("cannot inspect Docker for Harbor-labeled containers") from exc
    if listed.returncode != 0:
        raise RuntimeError("cannot inspect Docker for Harbor-labeled containers")
    matches: set[str] = set()
    for container_id in listed.stdout.split():
        try:
            inspected = runner(
                ["docker", "inspect", "--format", "{{json .Config.Labels}}", container_id],
                check=False,
                capture_output=True,
                text=True,
                timeout=SUPPORT_COMMAND_TIMEOUT_SECONDS,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("cannot inspect Docker for Harbor-labeled containers") from exc
        if inspected.returncode != 0:
            continue
        try:
            labels = json.loads(inspected.stdout)
        except json.JSONDecodeError:
            continue
        if isinstance(labels, dict) and _is_harbor_container(
            labels,
            task_root,
            project_prefixes,
        ):
            matches.add(container_id)
    return frozenset(matches)


def cleanup_new_harbor_containers(
    task_root: Path,
    containers_before: frozenset[str],
    *,
    project_prefixes: frozenset[str],
    command_runner: Any = None,
) -> tuple[str, ...]:
    """Force-remove new Harbor-labeled task containers; never prune globally."""
    if not project_prefixes:
        return ()
    current = harbor_container_ids(
        task_root,
        project_prefixes=project_prefixes,
        command_runner=command_runner,
    )
    orphaned = tuple(sorted(current - containers_before))
    if not orphaned:
        return ()
    runner = command_runner or _run_text_command
    try:
        completed = runner(
            ["docker", "rm", "-f", "--", *orphaned],
            check=False,
            capture_output=True,
            text=True,
            timeout=SUPPORT_COMMAND_TIMEOUT_SECONDS,
            env=subscription_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("failed to remove Harbor-labeled orphan containers") from exc
    if completed.returncode != 0:
        raise RuntimeError("failed to remove Harbor-labeled orphan containers")
    return orphaned


def _canonical_semver(value: str, *, label: str) -> str:
    matched = _SEMVER.fullmatch(value)
    if matched is None:
        raise ExecutionFailure(
            "harbor_identity_unavailable",
            f"{label} is not a strict semantic version: {value!r}",
        )
    return ".".join(str(int(component)) for component in matched.groups())


def _locked_harbor_version(repo_root: Path) -> str:
    lock_path = repo_root / "uv.lock"
    try:
        payload = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ExecutionFailure(
            "harbor_identity_unavailable",
            f"cannot read Harbor lock authority: {lock_path}",
        ) from exc
    packages = payload.get("package")
    if not isinstance(packages, list):
        raise ExecutionFailure(
            "harbor_identity_unavailable",
            f"Harbor lock authority has no package list: {lock_path}",
        )
    versions = {
        item.get("version")
        for item in packages
        if isinstance(item, dict) and item.get("name") == "harbor"
    }
    if len(versions) != 1 or not isinstance(next(iter(versions), None), str):
        raise ExecutionFailure(
            "harbor_identity_unavailable",
            f"Harbor lock authority is missing or ambiguous: {lock_path}",
        )
    return _canonical_semver(next(iter(versions)), label="locked Harbor version")


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def resolve_harbor_runtime_identity(repo_root: Path) -> HarborRuntimeIdentity:
    """Resolve and require the exact lock-pinned Harbor executable before launch."""

    declared_version = _locked_harbor_version(repo_root)
    candidate = shutil.which("harbor")
    if candidate is None:
        raise ExecutionFailure("harbor_identity_unavailable", "Harbor executable is unavailable")
    try:
        executable_path = Path(candidate).resolve(strict=True)
        completed = subprocess.run(
            [str(executable_path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=SUPPORT_COMMAND_TIMEOUT_SECONDS,
            env=subscription_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExecutionFailure(
            "harbor_identity_unavailable", "cannot resolve Harbor executable identity"
        ) from exc
    if completed.returncode != 0:
        raise ExecutionFailure(
            "harbor_identity_unavailable", "Harbor executable does not report a semantic version"
        )
    actual_version = _canonical_semver(
        (completed.stdout or completed.stderr).strip(),
        label="Harbor executable version",
    )
    if actual_version != declared_version:
        raise ExecutionFailure(
            "harbor_version_mismatch",
            f"Harbor executable {actual_version} does not match locked {declared_version}",
        )
    try:
        (
            executable_device,
            executable_inode,
            executable_size,
            executable_mtime_ns,
            executable_digest,
        ) = _executable_snapshot(executable_path)
    except OSError as exc:
        raise ExecutionFailure(
            "harbor_identity_unavailable", "cannot digest Harbor executable identity"
        ) from exc
    return HarborRuntimeIdentity(
        declared_version=declared_version,
        actual_version=actual_version,
        executable_path=executable_path,
        executable_digest=executable_digest,
        executable_device=executable_device,
        executable_inode=executable_inode,
        executable_size=executable_size,
        executable_mtime_ns=executable_mtime_ns,
    )


def _executable_snapshot(path: Path) -> tuple[int, int, int, int, str]:
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise OSError("Harbor executable is not a regular file")
    digest = _file_digest(path)
    after = path.stat()
    snapshot = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if snapshot != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
        raise OSError("Harbor executable changed while its identity was captured")
    return (*snapshot, digest)


def _verify_harbor_runtime_identity(identity: HarborRuntimeIdentity) -> None:
    try:
        snapshot = _executable_snapshot(identity.executable_path)
    except OSError as exc:
        raise ExecutionFailure(
            "harbor_identity_drift",
            "Harbor executable changed before launch",
        ) from exc
    if snapshot != (
        identity.executable_device,
        identity.executable_inode,
        identity.executable_size,
        identity.executable_mtime_ns,
        identity.executable_digest,
    ):
        raise ExecutionFailure(
            "harbor_identity_drift",
            "Harbor executable changed before launch",
        )


def _stage_verified_harbor_executable(identity: HarborRuntimeIdentity, staging_dir: Path) -> Path:
    """Copy locked Harbor bytes to an executor-owned launch artifact."""

    launch_path = staging_dir / ".harbor-launch"
    try:
        shutil.copyfile(identity.executable_path, launch_path)
        launch_path.chmod(0o700)
        if _executable_snapshot(launch_path)[4] != identity.executable_digest:
            raise OSError("staged Harbor bytes do not match the locked executable")
    except OSError as exc:
        raise ExecutionFailure(
            "harbor_identity_drift",
            "Harbor executable changed before launch",
        ) from exc
    return launch_path


def tool_version(command: str) -> str | None:
    executable = shutil.which(command)
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=SUPPORT_COMMAND_TIMEOUT_SECONDS,
            env=subscription_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (completed.stdout or completed.stderr).strip()
    return output.splitlines()[0] if output else None


def git_state(root: Path) -> dict[str, Any]:
    if not (root / ".git").exists():
        return {"commit": None, "dirty": None}
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=SUPPORT_COMMAND_TIMEOUT_SECONDS,
            env=subscription_environment(),
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=SUPPORT_COMMAND_TIMEOUT_SECONDS,
            env=subscription_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"commit": None, "dirty": None}
    return {
        "commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
    }


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return


def _trial_is_terminal(trial_dir: Path) -> bool:
    try:
        payload = json.loads((trial_dir / "result.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return bool(isinstance(payload, dict) and payload.get("finished_at"))


def _active_trial_directories(job_dir: Path) -> tuple[Path, ...]:
    if not job_dir.is_dir():
        return ()
    return tuple(
        candidate
        for candidate in job_dir.iterdir()
        if candidate.is_dir() and "__" in candidate.name and not _trial_is_terminal(candidate)
    )


def _unlink_secret_dir(directory: Path | None, secret_file: Path | None) -> None:
    if secret_file is not None:
        with suppress(OSError):
            secret_file.unlink()
    if directory is not None:
        with suppress(OSError):
            shutil.rmtree(directory)


class _StreamingRedactor:
    """Redact exact secret bytes before any child output reaches disk."""

    def __init__(self, secrets: frozenset[str]) -> None:
        self.secrets = tuple(
            sorted(
                (secret.encode() for secret in secrets if secret),
                key=len,
                reverse=True,
            )
        )
        self.pending = b""
        self.max_secret_length = max((len(secret) for secret in self.secrets), default=0)

    def feed(self, chunk: bytes) -> bytes:
        if not self.secrets:
            return chunk
        combined = self.pending + chunk
        cut = max(0, len(combined) - self.max_secret_length + 1)
        for secret in self.secrets:
            search_from = max(0, cut - len(secret) + 1)
            start = combined.find(secret, search_from)
            while start >= 0:
                if start < cut < start + len(secret):
                    cut = start
                start = combined.find(secret, start + 1)
        safe = combined[:cut]
        self.pending = combined[cut:]
        for secret in self.secrets:
            safe = safe.replace(secret, b"<redacted>")
        return safe

    def finish(self) -> bytes:
        safe = self.pending
        self.pending = b""
        for secret in self.secrets:
            safe = safe.replace(secret, b"<redacted>")
        return safe


def _drain_redacted_output(
    source: Any,
    destination: Any,
    secrets: frozenset[str],
) -> None:
    redactor = _StreamingRedactor(secrets)
    while chunk := source.read(64 * 1024):
        destination.write(redactor.feed(chunk))
        destination.flush()
    destination.write(redactor.finish())
    destination.flush()


def assert_no_secret_material(
    paths: tuple[Path, ...],
    *,
    secrets: frozenset[str],
) -> None:
    """Fail closed if provider credential bytes reached a persistent artifact."""
    encoded = tuple(secret.encode() for secret in secrets if secret)
    if not encoded:
        return
    leaks: list[Path] = []
    for root in paths:
        candidates = (root,) if root.is_file() or root.is_symlink() else tuple(root.rglob("*"))
        for path in candidates:
            if path.is_symlink():
                raise ExecutionFailure(
                    "unsafe_artifact_symlink",
                    f"persistent artifact contains a symlink: {path}",
                )
            if not path.is_file():
                continue
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise ExecutionFailure(
                    "artifact_scan_failed",
                    f"persistent artifact cannot be scanned: {path}",
                ) from exc
            if any(secret in content for secret in encoded):
                leaks.append(path)
    if leaks:
        labels = ", ".join(str(path) for path in leaks)
        removal_failures: list[str] = []
        for path in leaks:
            try:
                path.unlink()
            except OSError:
                removal_failures.append(str(path))
        disposition = (
            "removal failed for: " + ", ".join(removal_failures)
            if removal_failures
            else "contaminated files were removed"
        )
        raise ExecutionFailure(
            "credential_material_detected",
            f"credential material reached persistent artifacts ({disposition}): {labels}",
        )


def _read_generation(path: Path) -> str | None:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            payload = json.load(source)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    finally:
        os.close(descriptor)
    if not isinstance(payload, dict):
        return None
    generation = payload.get("lease_generation")
    if not is_lease_generation(generation):
        return None
    return generation


def _lease_owned(lease_path: Path | None, lease_generation: str | None) -> bool:
    if lease_path is None:
        return True
    return lease_generation is not None and _read_generation(lease_path) == lease_generation


def _cancel_requested(
    lease_path: Path | None,
    lease_generation: str | None,
) -> bool:
    if lease_path is None or lease_generation is None:
        return False
    marker = lease_path.with_name(f"{lease_path.name}.cancel.{lease_generation}")
    return _read_generation(marker) == lease_generation


def _heartbeat_lease(lease_path: Path, lease_generation: str) -> bool:
    try:
        descriptor = os.open(
            lease_path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return False
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            payload = json.load(source)
        if not isinstance(payload, dict) or payload.get("lease_generation") != lease_generation:
            return False
        os.utime(descriptor)
        return True
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    finally:
        os.close(descriptor)


def _lease_cancelled_or_lost(
    lease_path: Path | None,
    lease_generation: str | None,
) -> bool:
    return _cancel_requested(lease_path, lease_generation) or not _lease_owned(
        lease_path,
        lease_generation,
    )


def _read_proxy_usage(
    path: Path,
    *,
    capability_id: str,
    attempt_id: str,
    limits: ProxyTrialLimits,
    provider_label: str = "DeepSeek",
    expected_pricing: dict[str, int] | None = None,
) -> dict[str, Any]:
    try:
        payload = json.loads(read_owner_secret_file(path))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionFailure(
            "proxy_usage_invalid",
            f"{provider_label} proxy usage report is unreadable",
        ) from exc
    if not isinstance(payload, dict):
        raise ExecutionFailure(
            "proxy_usage_invalid",
            f"{provider_label} proxy usage report is invalid",
        )

    def integer(value: object, label: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 2**63 - 1:
            raise ExecutionFailure(
                "proxy_usage_invalid",
                f"{provider_label} proxy usage field {label} is invalid",
            )
        return value

    expected_limits = asdict(limits)
    binding_invalid = (
        payload.get("schema_version") != 1
        or payload.get("capability_id") != capability_id
        or payload.get("attempt_id") != attempt_id
        or payload.get("limits") != expected_limits
        or (expected_pricing is not None and payload.get("pricing") != expected_pricing)
    )
    if binding_invalid:
        raise ExecutionFailure(
            "proxy_usage_invalid",
            f"{provider_label} proxy usage binding does not match this trial",
        )
    calls = payload.get("calls")
    totals = payload.get("totals")
    if not isinstance(calls, list) or not isinstance(totals, dict):
        raise ExecutionFailure(
            "proxy_usage_invalid",
            f"{provider_label} proxy usage report is invalid",
        )
    computed = {
        "requests": len(calls),
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_micros": 0,
    }
    unresolved = 0
    expected_sequence = 0
    for index, raw_call in enumerate(calls, start=1):
        if not isinstance(raw_call, dict) or integer(raw_call.get("call_id"), "call_id") != index:
            raise ExecutionFailure(
                "proxy_usage_invalid",
                f"{provider_label} proxy call sequence is invalid",
            )
        state = raw_call.get("state")
        if state == "reconciled":
            source_fields = ("input_tokens", "output_tokens", "cost_micros")
            expected_sequence += 2
        elif state == "exceeded":
            source_fields = ("input_tokens", "output_tokens", "cost_micros")
            unresolved += 1
            expected_sequence += 2
        elif state in {"reserved", "unresolved"}:
            source_fields = (
                "reserved_input_tokens",
                "reserved_output_tokens",
                "reserved_cost_micros",
            )
            unresolved += 1
            expected_sequence += 1 if state == "reserved" else 2
        else:
            raise ExecutionFailure(
                "proxy_usage_invalid",
                f"{provider_label} proxy call state is invalid",
            )
        computed["input_tokens"] += integer(raw_call.get(source_fields[0]), source_fields[0])
        computed["output_tokens"] += integer(raw_call.get(source_fields[1]), source_fields[1])
        computed["cost_micros"] += integer(raw_call.get(source_fields[2]), source_fields[2])
    expected_totals = {
        **computed,
        "total_tokens": computed["input_tokens"] + computed["output_tokens"],
    }
    if (
        {name: integer(totals.get(name), name) for name in expected_totals} != expected_totals
        or integer(payload.get("unresolved_requests"), "unresolved_requests") != unresolved
        or integer(payload.get("sequence"), "sequence") != expected_sequence
    ):
        raise ExecutionFailure(
            "proxy_usage_invalid",
            f"{provider_label} proxy totals do not reconcile with provider calls",
        )
    return payload


def run_harbor_process(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    log_path: Path,
    job_dir: Path | None = None,
    trial_timeout_seconds: float | None = None,
    lease_path: Path | None = None,
    lease_generation: str | None = None,
    proxy_attempt_id: str | None = None,
    proxy_limits: ProxyTrialLimits | None = None,
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
) -> HarborProcessResult:
    """Run Harbor while redacting provider credentials before log persistence."""
    if lease_path is not None and lease_generation is None:
        raise ValueError("lease_generation is required when lease_path is set")
    if _lease_cancelled_or_lost(lease_path, lease_generation):
        raise ExecutionFailure(
            "execution_cancelled",
            "campaign owner cancelled the active queue lease before Harbor launch",
        )
    repo_imports = (*HARBOR_AGENT_IMPORT_PATHS.values(), HARBOR_STATE_JOURNAL_PLUGIN)
    deepseek_adapter = HARBOR_AGENT_IMPORT_PATHS["mini-swe-agent"]
    zai_adapter = HARBOR_AGENT_IMPORT_PATHS[ZAI_OPENCODE_AGENT]
    deepseek_lane = deepseek_adapter in command
    zai_lane = zai_adapter in command
    runtime_environment = subscription_environment(
        include_deepseek_credentials=deepseek_lane,
        include_zai_credentials=zai_lane,
    )
    secret_values = collected_secret_values()
    owned_secret_dir: Path | None = None
    owned_secret_path: Path | None = None
    owned_usage_dir: Path | None = None
    owned_usage_path: Path | None = None
    capability_id: str | None = None
    proxy_pricing: dict[str, int] | None = None
    try:
        if deepseek_lane:
            if proxy_attempt_id is None or proxy_limits is None:
                raise ValueError("DeepSeek execution requires a bound trial capability")
            capability = secrets.token_urlsafe(32)
            capability_id = "sha256:" + hashlib.sha256(capability.encode()).hexdigest()
            owned_usage_dir = Path(
                tempfile.mkdtemp(
                    prefix="evallab-deepseek-usage.",
                    dir=os.environ.get("TMPDIR") or None,
                )
            )
            os.chmod(owned_usage_dir, 0o700)
            owned_usage_path = owned_usage_dir / "deepseek-proxy-usage.json"
            runtime_environment[DEEPSEEK_PROXY_CAPABILITY_ENV] = capability
            runtime_environment[DEEPSEEK_PROXY_ATTEMPT_ID_ENV] = proxy_attempt_id
            runtime_environment[DEEPSEEK_PROXY_USAGE_DIR_ENV] = str(owned_usage_dir)
            runtime_environment[DEEPSEEK_PROXY_USAGE_FILE_ENV] = str(owned_usage_path)
            runtime_environment["DEEPSEEK_API_KEY"] = capability
            runtime_environment["MSWEA_API_KEY"] = capability
            runtime_environment[DEEPSEEK_ALLOWED_MODEL_ENV] = os.environ.get(
                DEEPSEEK_ALLOWED_MODEL_ENV, DEEPSEEK_ALLOWED_MODEL
            )
            runtime_environment["EVALLAB_DEEPSEEK_MAX_REQUESTS"] = str(proxy_limits.max_requests)
            runtime_environment["EVALLAB_DEEPSEEK_MAX_INPUT_TOKENS"] = str(
                proxy_limits.max_input_tokens
            )
            runtime_environment["EVALLAB_DEEPSEEK_MAX_OUTPUT_TOKENS"] = str(
                proxy_limits.max_output_tokens
            )
            runtime_environment["EVALLAB_DEEPSEEK_MAX_TOTAL_TOKENS"] = str(
                proxy_limits.max_total_tokens
            )
            runtime_environment["EVALLAB_DEEPSEEK_MAX_COST_MICROS"] = str(
                proxy_limits.max_cost_micros
            )
            runtime_environment["EVALLAB_DEEPSEEK_INPUT_COST_MICROS_PER_MILLION"] = os.environ.get(
                "EVALLAB_DEEPSEEK_INPUT_COST_MICROS_PER_MILLION",
                "280000",
            )
            runtime_environment["EVALLAB_DEEPSEEK_OUTPUT_COST_MICROS_PER_MILLION"] = os.environ.get(
                "EVALLAB_DEEPSEEK_OUTPUT_COST_MICROS_PER_MILLION",
                "420000",
            )
            runtime_environment["EVALLAB_DEEPSEEK_CAPABILITY_EXPIRES_AT"] = str(
                time.time() + float(timeout_seconds) + 60.0
            )
            existing_secret = runtime_environment.get(DEEPSEEK_SECRET_FILE_ENV) or os.environ.get(
                DEEPSEEK_SECRET_FILE_ENV
            )
            log_root = log_path.resolve()
            if existing_secret:
                try:
                    read_owner_secret_file(Path(existing_secret))
                    existing_path = Path(existing_secret).resolve()
                except OSError:
                    existing_secret = None
                else:
                    if log_root.parent in existing_path.parents or (
                        job_dir is not None and job_dir.resolve() in existing_path.parents
                    ):
                        existing_secret = None
            if existing_secret:
                runtime_environment[DEEPSEEK_SECRET_FILE_ENV] = existing_secret
            else:
                owned_secret_dir = Path(
                    tempfile.mkdtemp(
                        prefix="evallab-deepseek-secret.",
                        dir=os.environ.get("TMPDIR") or None,
                    )
                )
                os.chmod(owned_secret_dir, 0o700)
                owned_secret_path = owned_secret_dir / "key"
                materialize_deepseek_secret_file(owned_secret_path)
                runtime_environment[DEEPSEEK_SECRET_FILE_ENV] = str(owned_secret_path)
            runtime_environment[DEEPSEEK_PROXY_SCRIPT_ENV] = str(
                (cwd / DEEPSEEK_PROXY_SCRIPT).resolve()
            )
            proxy_uid, proxy_gid = proxy_runtime_identity(
                Path(runtime_environment[DEEPSEEK_SECRET_FILE_ENV])
            )
            runtime_environment[DEEPSEEK_PROXY_UID_ENV] = str(proxy_uid)
            runtime_environment[DEEPSEEK_PROXY_GID_ENV] = str(proxy_gid)
            secret_values = collected_secret_values({**os.environ, **runtime_environment})
        if zai_lane:
            if proxy_attempt_id is None or proxy_limits is None:
                raise ValueError("Z.ai execution requires a bound trial capability")
            capability = secrets.token_urlsafe(32)
            capability_id = "sha256:" + hashlib.sha256(capability.encode()).hexdigest()
            owned_usage_dir = Path(
                tempfile.mkdtemp(
                    prefix="evallab-zai-usage.",
                    dir=os.environ.get("TMPDIR") or None,
                )
            )
            os.chmod(owned_usage_dir, 0o700)
            owned_usage_path = owned_usage_dir / "zai-proxy-usage.json"
            runtime_environment[ZAI_PROXY_ATTEMPT_ID_ENV] = proxy_attempt_id
            runtime_environment[ZAI_PROXY_USAGE_DIR_ENV] = str(owned_usage_dir)
            runtime_environment[ZAI_PROXY_USAGE_FILE_ENV] = str(owned_usage_path)
            runtime_environment["EVALLAB_ZAI_MAX_REQUESTS"] = str(proxy_limits.max_requests)
            runtime_environment["EVALLAB_ZAI_MAX_INPUT_TOKENS"] = str(proxy_limits.max_input_tokens)
            runtime_environment["EVALLAB_ZAI_MAX_OUTPUT_TOKENS"] = str(
                proxy_limits.max_output_tokens
            )
            runtime_environment["EVALLAB_ZAI_MAX_TOTAL_TOKENS"] = str(proxy_limits.max_total_tokens)
            runtime_environment["EVALLAB_ZAI_MAX_COST_MICROS"] = str(proxy_limits.max_cost_micros)
            proxy_pricing = {
                "input_cost_micros_per_million": ZAI_INPUT_COST_MICROS_PER_MILLION,
                "output_cost_micros_per_million": ZAI_OUTPUT_COST_MICROS_PER_MILLION,
            }
            runtime_environment["EVALLAB_ZAI_INPUT_COST_MICROS_PER_MILLION"] = str(
                ZAI_INPUT_COST_MICROS_PER_MILLION
            )
            runtime_environment["EVALLAB_ZAI_OUTPUT_COST_MICROS_PER_MILLION"] = str(
                ZAI_OUTPUT_COST_MICROS_PER_MILLION
            )
            owned_secret_dir = Path(
                tempfile.mkdtemp(
                    prefix="evallab-zai-secret.",
                    dir=os.environ.get("TMPDIR") or None,
                )
            )
            os.chmod(owned_secret_dir, 0o700)
            owned_secret_path = owned_secret_dir / "key"
            materialize_zai_secret_file(owned_secret_path)
            runtime_environment[ZAI_SECRET_FILE_ENV] = str(owned_secret_path)
            runtime_environment[ZAI_PROXY_SCRIPT_ENV] = str((cwd / ZAI_PROXY_SCRIPT).resolve())
            proxy_uid, proxy_gid = proxy_runtime_identity(owned_secret_path)
            runtime_environment[ZAI_PROXY_UID_ENV] = str(proxy_uid)
            runtime_environment[ZAI_PROXY_GID_ENV] = str(proxy_gid)
            runtime_environment[ZAI_PROXY_CAPABILITY_ENV] = capability
            runtime_environment[ZAI_CAPABILITY_EXPIRES_AT_ENV] = str(
                time.time() + float(timeout_seconds) + 60.0
            )
            runtime_environment["ZAI_CODING_PLAN_API_KEY"] = capability
            runtime_environment["ZAI_API_KEY"] = capability
            secret_values = collected_secret_values({**os.environ, **runtime_environment})
        if any(import_path in command for import_path in repo_imports):
            source_root = cwd / "src"
            if source_root.is_dir():
                inherited_pythonpath = os.environ.get("PYTHONPATH")
                runtime_environment["PYTHONPATH"] = os.pathsep.join(
                    str(path) for path in (source_root, inherited_pythonpath) if path
                )
        secret_bytes = tuple(value.encode() for value in secret_values)

        def _result(
            *,
            returncode: int,
            timed_out: bool,
            timed_out_trial: str | None = None,
        ) -> HarborProcessResult:
            proxy_usage = None
            if (
                (deepseek_lane or zai_lane)
                and owned_usage_path is not None
                and owned_usage_path.is_file()
                and capability_id is not None
                and proxy_attempt_id is not None
                and proxy_limits is not None
            ):
                proxy_usage = _read_proxy_usage(
                    owned_usage_path,
                    capability_id=capability_id,
                    attempt_id=proxy_attempt_id,
                    limits=proxy_limits,
                    provider_label="Z.ai" if zai_lane else "DeepSeek",
                    expected_pricing=proxy_pricing,
                )
            return HarborProcessResult(
                returncode=returncode,
                timed_out=timed_out,
                log_path=log_path,
                timed_out_trial=timed_out_trial,
                proxy_usage=proxy_usage,
            )

        read_fd, write_fd = os.pipe()
        writer = RedactingBinaryWriter(log_path, secret_bytes)

        def _pump_redacted_output() -> None:
            try:
                with os.fdopen(read_fd, "rb", closefd=True) as source:
                    while True:
                        chunk = source.read(8192)
                        if not chunk:
                            break
                        writer.write(chunk)
            finally:
                writer.close()

        pump = threading.Thread(target=_pump_redacted_output, name="harbor-log-redact", daemon=True)
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=write_fd,
                stderr=subprocess.STDOUT,
                env=runtime_environment,
                start_new_session=True,
            )
        except BaseException:
            os.close(write_fd)
            os.close(read_fd)
            writer.close()
            raise
        os.close(write_fd)
        pump.start()
        try:
            started = time.monotonic()
            if (
                lease_path is not None
                and lease_generation is not None
                and not _heartbeat_lease(lease_path, lease_generation)
            ):
                _terminate_process_group(process)
                pump.join(timeout=5)
                return _result(returncode=-1, timed_out=False)
            last_heartbeat = started
            first_seen: dict[Path, float] = {}
            while True:
                if _lease_cancelled_or_lost(lease_path, lease_generation):
                    _terminate_process_group(process)
                    pump.join(timeout=5)
                    return _result(
                        returncode=(process.returncode if process.returncode is not None else -1),
                        timed_out=False,
                    )
                returncode = process.poll()
                if returncode is not None:
                    break
                now = time.monotonic()
                if (
                    lease_path is not None
                    and lease_generation is not None
                    and now - last_heartbeat >= heartbeat_interval_seconds
                ):
                    if not _heartbeat_lease(lease_path, lease_generation):
                        _terminate_process_group(process)
                        pump.join(timeout=5)
                        return _result(
                            returncode=(
                                process.returncode if process.returncode is not None else -1
                            ),
                            timed_out=False,
                        )
                    last_heartbeat = now
                timed_out_trial: str | None = None
                if job_dir is not None and trial_timeout_seconds is not None:
                    active = set(_active_trial_directories(job_dir))
                    first_seen = {
                        path: observed for path, observed in first_seen.items() if path in active
                    }
                    for path in active:
                        first_seen.setdefault(path, now)
                        if now - first_seen[path] >= trial_timeout_seconds:
                            timed_out_trial = path.name
                            break
                if timed_out_trial is not None or now - started >= timeout_seconds:
                    _terminate_process_group(process)
                    pump.join(timeout=5)
                    return _result(
                        returncode=(process.returncode if process.returncode is not None else -1),
                        timed_out=True,
                        timed_out_trial=timed_out_trial,
                    )
                try:
                    process.wait(timeout=WATCHDOG_POLL_SECONDS)
                except subprocess.TimeoutExpired:
                    continue
            pump.join(timeout=5)
            return _result(
                returncode=returncode,
                timed_out=False,
            )

        finally:
            _unlink_secret_dir(owned_secret_dir, owned_secret_path)
            _unlink_secret_dir(owned_usage_dir, owned_usage_path)
    finally:
        _unlink_secret_dir(owned_secret_dir, owned_secret_path)
        _unlink_secret_dir(owned_usage_dir, owned_usage_path)


def _tail_text(path: Path, *, limit_bytes: int = 1_000_000) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as source:
        source.seek(0, os.SEEK_END)
        size = source.tell()
        source.seek(max(0, size - limit_bytes))
        return source.read().decode(errors="replace")


def _executor_log_path(request: RunRequest) -> Path:
    root = request.jobs_dir / ".executor"
    initial = root / f"{request.name}.log"
    if not initial.exists():
        return initial
    attempt = 2
    while True:
        candidate = root / f"{request.name}.attempt-{attempt}.log"
        if not candidate.exists():
            return candidate
        attempt += 1


def executor_state_path(request: RunRequest) -> Path:
    return request.jobs_dir / ".executor" / f"{request.name}.state.json"


def _write_executor_state(
    request: RunRequest,
    *,
    started_at: datetime,
    status: str,
    log_path: Path,
    finished_at: datetime | None = None,
    process: HarborProcessResult | None = None,
) -> None:
    path = executor_state_path(request)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": status,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat() if finished_at is not None else None,
        "trial_timeout_seconds": request.timeout_seconds,
        "job_timeout_seconds": request.job_timeout_seconds,
        "log_path": str(log_path.relative_to(request.jobs_dir)),
    }
    if process is not None:
        payload.update(
            {
                "exit_code": process.returncode,
                "timed_out": process.timed_out,
                "timed_out_trial": process.timed_out_trial,
            }
        )
    persist_private_bytes(
        path,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(),
        secrets=tuple(value.encode() for value in collected_secret_values()),
    )


def _harbor_project_prefixes(job_dir: Path) -> frozenset[str]:
    if not job_dir.is_dir():
        return frozenset()
    return frozenset(
        re.sub(r"[^a-z0-9_-]", "-", child.name.lower())
        for child in job_dir.iterdir()
        if child.is_dir() and not child.name.startswith(".")
    )


def _transient_reason_from_job(job_dir: Path) -> str | None:
    if job_dir.is_dir():
        for path in sorted(job_dir.rglob("result.json")):
            try:
                payload = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                reason = transient_provider_exception(payload)
                if reason is not None:
                    return reason
    return None


def _cleanup_failure(
    request: RunRequest,
    containers_before: frozenset[str],
    job_dir: Path,
) -> str | None:
    try:
        cleanup_new_harbor_containers(
            request.task,
            containers_before,
            project_prefixes=_harbor_project_prefixes(job_dir),
        )
    except Exception as exc:
        return f"cleanup_failed:{type(exc).__name__}"
    return None


def _write_run_metadata(
    request: RunRequest,
    *,
    repo_root: Path,
    command: list[str],
    started: datetime,
    finished: datetime,
    process: HarborProcessResult,
    harbor_identity: HarborRuntimeIdentity,
    network_adaptation: NetworkAdaptation | None = None,
) -> None:
    job_dir = request.jobs_dir / request.name
    if not job_dir.exists():
        return
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "command": command,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "exit_code": process.returncode,
        "timed_out": process.timed_out,
        "timed_out_trial": process.timed_out_trial,
        "trial_timeout_seconds": request.timeout_seconds,
        "job_timeout_seconds": request.job_timeout_seconds,
        "executor_log": process.log_path.relative_to(request.jobs_dir).as_posix(),
        "host": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
        },
        "tools": {
            "harbor": tool_version("harbor"),
            "docker": tool_version("docker"),
            "uv": tool_version("uv"),
        },
        "harbor_runtime": {
            "declared_version": harbor_identity.declared_version,
            "actual_version": harbor_identity.actual_version,
            "executable_path": str(harbor_identity.executable_path),
            "executable_digest": harbor_identity.executable_digest,
        },
        "repository": git_state(repo_root),
    }
    if request.provenance is not None:
        metadata["experiment"] = request.provenance.model_dump(mode="json")
    if process.proxy_usage is not None:
        metadata["provider_usage"] = process.proxy_usage
    if network_adaptation is not None:
        metadata["network_adaptation"] = asdict(network_adaptation)
    persist_private_bytes(
        job_dir / "lab-metadata.json",
        (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode(),
        secrets=tuple(value.encode() for value in collected_secret_values()),
    )


def _network_adaptation_path(request: RunRequest) -> Path:
    return request.jobs_dir / ".executor" / f"{request.name}.network-adaptation.json"


def _write_network_adaptation(
    request: RunRequest,
    adaptation: NetworkAdaptation | None,
) -> None:
    """Persist compact network adaptation metadata outside the staging copy.

    This is written as soon as staging is created so that early build/runtime
    failures still retain the requested and effective modes, adapter version,
    adapter digest, and isolation reason even if the temporary staging copy is
    later deleted.
    """
    if adaptation is None:
        return
    path = _network_adaptation_path(request)
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "network_adaptation": asdict(adaptation),
    }
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
    except Exception:
        with suppress(OSError):
            temporary.unlink()
        raise
    temporary.replace(path)


def _cleanup_stage(staging_dir: Path | None) -> None:
    """Remove the temporary execution copy, including any copied task secrets.

    The compact adaptation metadata has already been persisted to
    ``.executor/<name>.network-adaptation.json`` and, when the run reached the
    metadata step, ``<job-dir>/lab-metadata.json``.
    """
    if staging_dir is not None and staging_dir.is_dir():
        shutil.rmtree(staging_dir)


def _stage_task_for_host(
    source: Path,
    staging_dir: Path,
    *,
    agent_allowed_hosts: tuple[str, ...] = (),
    expected_package_digest: str | None = None,
) -> tuple[Path, NetworkAdaptation | None]:
    """Create and verify a private immutable-input snapshot before adaptation."""
    from evallab.registry import compute_task_digests

    task_toml_path = source / "task.toml"
    if not task_toml_path.is_file():
        raise ValueError(f"task.toml missing in {source}")
    if any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("task package snapshots reject symlinks")
    source_digest_before = compute_task_digests(source).package
    if expected_package_digest is not None and source_digest_before != expected_package_digest:
        raise ValueError("task package differs from its frozen digest before staging")

    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, staging_dir, symlinks=True)
    if any(path.is_symlink() for path in staging_dir.rglob("*")):
        raise ValueError("staged task snapshot contains a symlink")
    staged_digest = compute_task_digests(staging_dir).package
    source_digest_after = compute_task_digests(source).package
    if staged_digest != source_digest_before or source_digest_after != source_digest_before:
        raise ValueError("task package changed while its execution snapshot was created")

    original_text = (staging_dir / "task.toml").read_text(encoding="utf-8")
    adapted_text, adaptation = adapt_task_toml_for_host(original_text)
    staged_text = with_agent_network_allowlist(adapted_text, agent_allowed_hosts)
    (staging_dir / "task.toml").write_text(staged_text, encoding="utf-8")
    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "source_package_digest": source_digest_before,
        "agent_allowed_hosts": list(agent_allowed_hosts),
    }
    if adaptation is not None:
        manifest["network_adaptation"] = asdict(adaptation)
    (staging_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return staging_dir, adaptation


def _sanitize_persisted_job_tree(root: Path, secrets: tuple[bytes, ...]) -> None:
    """Redact then chmod lab-owned persisted streams under a Harbor job directory."""
    if not root.is_dir() or not secrets:
        return
    suffixes = {".log", ".json", ".jsonl", ".txt", ".yaml", ".yml"}
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in suffixes:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        persist_private_bytes(path, data, secrets=secrets)


def _proxy_trial_limits(request: RunRequest) -> ProxyTrialLimits | None:
    if request.agent not in {"mini-swe-agent", ZAI_OPENCODE_AGENT}:
        return None
    if (
        request.max_requests is None
        or request.max_input_tokens is None
        or request.max_output_tokens is None
        or request.max_total_tokens is None
        or request.cost_limit_usd is None
    ):
        raise ValueError("DeepSeek execution requires explicit provider ceilings")
    return ProxyTrialLimits(
        max_requests=request.max_requests,
        max_input_tokens=request.max_input_tokens,
        max_output_tokens=request.max_output_tokens,
        max_total_tokens=request.max_total_tokens,
        max_cost_micros=math.ceil(request.cost_limit_usd * 1_000_000),
    )


def _proxy_attempt_id(request: RunRequest) -> str | None:
    if request.agent not in {"mini-swe-agent", ZAI_OPENCODE_AGENT}:
        return None
    if request.provenance is not None:
        return request.provenance.campaign_attempt_id or request.provenance.spec_id or request.name
    return request.name


def _evidence_store_root() -> Path:
    configured = os.environ.get("EVALLAB_EVIDENCE_STORE_ROOT")
    if not configured:
        raise ExecutionFailure(
            "evidence_cas_unconfigured",
            "EVALLAB_EVIDENCE_STORE_ROOT is required before Harbor execution",
        )
    return Path(configured).absolute()


def _freeze_completed_job(job_dir: Path) -> Path:
    """Atomically remove completed output from the mutable producer namespace."""

    job_dir = job_dir.absolute()
    executor_root = job_dir.parent / ".executor"
    if executor_root.is_symlink():
        raise ValueError("executor state root cannot be a symlink")
    executor_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    settlement_root = executor_root / "settled"
    if settlement_root.is_symlink():
        raise ValueError("settlement root cannot be a symlink")
    settlement_root.mkdir(mode=0o700, exist_ok=True)

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as descriptors:
        source_parent_descriptor = os.open(job_dir.parent, flags)
        descriptors.callback(os.close, source_parent_descriptor)
        settlement_descriptor = os.open(settlement_root, flags)
        descriptors.callback(os.close, settlement_descriptor)
        source_info = os.stat(
            job_dir.name,
            dir_fd=source_parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(source_info.st_mode):
            raise ValueError("completed Harbor job is not a regular directory")
        if source_info.st_dev != os.fstat(settlement_descriptor).st_dev:
            raise ValueError("completed job and settlement root must share a filesystem")
        os.fchmod(settlement_descriptor, 0o700)
        frozen_name = f"source-{secrets.token_hex(16)}"
        os.rename(
            job_dir.name,
            frozen_name,
            src_dir_fd=source_parent_descriptor,
            dst_dir_fd=settlement_descriptor,
        )
        frozen_info = os.stat(
            frozen_name,
            dir_fd=settlement_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(frozen_info.st_mode)
            or frozen_info.st_dev != source_info.st_dev
            or frozen_info.st_ino != source_info.st_ino
        ):
            raise ValueError("frozen Harbor job identity changed during settlement")
        os.fsync(source_parent_descriptor)
        os.fsync(settlement_descriptor)
    return settlement_root / frozen_name


def _settle_completed_job(
    job_dir: Path,
    *,
    store_root: Path,
    record_id: str,
) -> tuple[EvidenceLocator, EvidenceArchive]:
    """Freeze a completed job and bind its exact content to canonical CAS identity."""

    try:
        frozen_source = _freeze_completed_job(job_dir)
        produced = archive_evidence(
            frozen_source,
            store_root,
            record_id=record_id,
            kind="job",
        )
        archive, _record_bytes = reopen_evidence_archive(
            store_root,
            kind="job",
            record_id=record_id,
            expected_record_digest=produced.record_digest,
            expected_content_digest=produced.content_digest,
        )
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError, tarfile.TarError) as exc:
        raise ExecutionFailure(
            "evidence_cas_unsettled",
            "completed Harbor job could not be archived and reopened from its frozen source",
        ) from exc
    return evidence_locator(store_root, archive), archive


def run_experiment(request: RunRequest, *, repo_root: Path) -> SettledRun:
    validate_request(request)
    repo_root = repo_root.resolve()
    harbor_identity = resolve_harbor_runtime_identity(repo_root)
    evidence_store = _evidence_store_root()
    if request.agent in {"mini-swe-agent", ZAI_OPENCODE_AGENT}:
        decision = preflight_request(request)
        if not decision.proceed:
            raise RuntimeError(f"{request.agent} credential preflight stopped: {decision.reason}")

    job_dir = request.jobs_dir / request.name
    if job_dir.exists():
        raise FileExistsError(
            f"Refusing to reuse existing job directory: {job_dir}. Choose a new explicit run name."
        )

    request.jobs_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = request.jobs_dir / ".exec-stage" / request.name
    executor_log = _executor_log_path(request)
    started = datetime.now(UTC)
    try:
        staged_task, adaptation = _stage_task_for_host(
            request.task,
            staging_dir,
            agent_allowed_hosts=(
                (DEEPSEEK_PROXY_HOST,)
                if request.agent == "mini-swe-agent"
                else ((ZAI_PROXY_HOST,) if request.agent == ZAI_OPENCODE_AGENT else ())
            ),
            expected_package_digest=(
                request.provenance.package_digest if request.provenance is not None else None
            ),
        )
        staged_request: RunRequest = replace(request, task=staged_task)

        _write_network_adaptation(request, adaptation)

        harbor_command = build_command(staged_request)
        containers_before = harbor_container_ids(staged_request.task)
        _write_executor_state(
            request,
            started_at=started,
            status="running",
            log_path=executor_log,
        )
        try:
            _verify_harbor_runtime_identity(harbor_identity)
            harbor_command[0] = str(_stage_verified_harbor_executable(harbor_identity, staging_dir))
            command = subscription_command(staged_request, harbor_command, repo_root=repo_root)
            process = run_harbor_process(
                command,
                cwd=repo_root,
                timeout_seconds=request.job_timeout_seconds,
                log_path=executor_log,
                job_dir=job_dir,
                trial_timeout_seconds=request.timeout_seconds,
                lease_path=request.lease_path,
                lease_generation=request.lease_generation,
                proxy_attempt_id=_proxy_attempt_id(request),
                proxy_limits=_proxy_trial_limits(request),
            )
        except BaseException:
            _write_executor_state(
                request,
                started_at=started,
                status="failed",
                log_path=executor_log,
                finished_at=datetime.now(UTC),
            )
            raise
        finished = datetime.now(UTC)
        cancelled = _lease_cancelled_or_lost(
            request.lease_path,
            request.lease_generation,
        )
        _write_executor_state(
            request,
            started_at=started,
            status=(
                "failed" if cancelled or process.timed_out or process.returncode != 0 else "running"
            ),
            log_path=executor_log,
            finished_at=finished,
            process=process,
        )
        _write_run_metadata(
            request,
            repo_root=repo_root,
            command=command,
            started=started,
            finished=finished,
            process=process,
            harbor_identity=harbor_identity,
            network_adaptation=adaptation,
        )
        if cancelled:
            cleanup_failure = _cleanup_failure(staged_request, containers_before, job_dir)
            cleanup_detail = f"; {cleanup_failure}" if cleanup_failure else ""
            raise ExecutionFailure(
                "execution_cancelled",
                f"campaign owner cancelled the active queue lease{cleanup_detail}",
            )

        if process.timed_out:
            cleanup_failure = _cleanup_failure(staged_request, containers_before, job_dir)
            scope = (
                f"trial {process.timed_out_trial!r} exceeded {request.timeout_seconds}s"
                if process.timed_out_trial is not None
                else f"Harbor exceeded aggregate fail-safe {request.job_timeout_seconds}s"
            )
            cleanup_detail = f"; {cleanup_failure}" if cleanup_failure else ""
            raise TrialTimeoutFailure(f"{scope}; inspect {executor_log}{cleanup_detail}")
        secret_values = collected_secret_values()
        assert_no_secret_material((job_dir,), secrets=secret_values)
        _sanitize_persisted_job_tree(
            job_dir,
            tuple(value.encode() for value in secret_values),
        )
        transient_reason = _transient_reason_from_job(job_dir)
        if process.returncode != 0:
            cleanup_failure = _cleanup_failure(staged_request, containers_before, job_dir)
            cleanup_detail = f"; {cleanup_failure}" if cleanup_failure else ""
            if transient_reason is not None:
                raise TransientHarnessFailure(
                    transient_reason,
                    message=transient_reason + cleanup_detail,
                )
            raise ExecutionFailure(
                f"Harbor exited with {process.returncode}; inspect {executor_log}{cleanup_detail}"
            )
        if request.agent in {"mini-swe-agent", ZAI_OPENCODE_AGENT}:
            provider_label = "Z.ai" if request.agent == ZAI_OPENCODE_AGENT else "DeepSeek"
            if process.proxy_usage is None:
                cleanup_failure = _cleanup_failure(staged_request, containers_before, job_dir)
                cleanup_detail = f"; {cleanup_failure}" if cleanup_failure else ""
                raise ExecutionFailure(
                    "proxy_usage_missing",
                    f"{provider_label} proxy usage report is missing{cleanup_detail}",
                )
            if process.proxy_usage.get("unresolved_requests") != 0:
                cleanup_failure = _cleanup_failure(staged_request, containers_before, job_dir)
                cleanup_detail = f"; {cleanup_failure}" if cleanup_failure else ""
                raise ExecutionFailure(
                    "proxy_usage_unreconciled",
                    f"{provider_label} proxy has unreconciled provider calls{cleanup_detail}",
                )
        job = load_job(job_dir)
        if transient_reason is not None:
            cleanup_failure = _cleanup_failure(staged_request, containers_before, job_dir)
            cleanup_detail = f"; {cleanup_failure}" if cleanup_failure else ""
            _write_executor_state(
                request,
                started_at=started,
                status="failed",
                log_path=executor_log,
                finished_at=finished,
                process=process,
            )
            raise TransientHarnessFailure(
                transient_reason,
                message=transient_reason + cleanup_detail,
            )
        try:
            locator, archive = _settle_completed_job(
                job_dir,
                store_root=evidence_store,
                record_id=str(job.id),
            )
        except BaseException:
            with suppress(Exception):
                _write_executor_state(
                    request,
                    started_at=started,
                    status="failed",
                    log_path=executor_log,
                    finished_at=finished,
                    process=process,
                )
            raise
        _write_executor_state(
            request,
            started_at=started,
            status="completed",
            log_path=executor_log,
            finished_at=finished,
            process=process,
        )
        return SettledRun(
            cas_locator=locator,
            cas_record=archive,
        )
    finally:
        _cleanup_stage(staging_dir)


def load_matrix(path: Path) -> ExperimentMatrix:
    try:
        return ExperimentMatrix.model_validate_json(path.read_text())
    except (OSError, ValidationError) as exc:
        raise ValueError(f"Invalid experiment matrix {path}: {exc}") from exc


def request_from_matrix(matrix: ExperimentMatrix, run: MatrixRun, *, repo_root: Path) -> RunRequest:
    return RunRequest(
        task=(repo_root / matrix.task).resolve(),
        agent=run.agent,
        name=run.name,
        jobs_dir=(repo_root / matrix.jobs_dir).resolve(),
        environment=matrix.environment,
        model=run.model,
        concurrency=matrix.concurrency,
        attempts=run.attempts,
        timeout_seconds=matrix.timeout_seconds,
        allow_billable=run.allow_billable,
    )


def expected_primary_reward(run: MatrixRun) -> float | None:
    return run.expect_reward


def database_url_from_environment(explicit: str | None = None) -> str:
    return explicit or os.environ.get(
        "DATABASE_URL",
        "postgresql://evallab:local-development-only@localhost:54329/evallab",
    )


# ---------------------------------------------------------------------------
# M003: profile-aware preflight (additive; wiring into the queue is a later
# integrator change). An auth failure here stops before any trial exists —
# it can never be recorded as a reward.
# ---------------------------------------------------------------------------

if TYPE_CHECKING:
    from evallab.profiles import AgentProfile, PreflightDecision, ProbeFn


def profile_for_request(request: RunRequest) -> AgentProfile:
    """Resolve the immutable profile for a request's agent (+ optional model).

    Raises ValueError when no profile matches or the requested model differs
    from the profile's pin — change profiles, not pins.
    """
    from evallab import profiles as profiles_module

    registry = profiles_module.builtin_profiles()
    candidates = [p for p in registry.values() if p.adapter == request.agent]
    if not candidates:
        raise ValueError(f"no profile declared for agent {request.agent!r}")
    if request.model is not None:
        for profile in candidates:
            if profile.model == request.model:
                return profile
        raise ValueError(
            f"no profile pins model {request.model!r} for agent {request.agent!r}; "
            "add a profile instead of overriding a pin"
        )
    return candidates[0]


def preflight_request(
    request: RunRequest,
    *,
    probe: ProbeFn | None = None,
    home: Path | None = None,
) -> PreflightDecision:
    """Fail-closed credential preflight for one request.

    Control agents pass with no credential. Billable profiles require a
    passing probe; a missing or failing probe blocks dispatch with a reason
    and no trial is started (auth failure never becomes reward zero).
    """
    from evallab import profiles as profiles_module

    profile = profile_for_request(request)
    profiles_module.validate_model_pin(profile, request.model)
    if probe is None and profile.auth_mode != "none":
        env = subscription_environment()
        probe = profiles_module.default_probe_for(
            profile,
            home=home or Path.home(),
            security_runner=_security_status,
            keychain_account=env.get("HARBOR_CLAUDE_KEYCHAIN_ACCOUNT", env.get("USER", "")),
        )
    return profiles_module.preflight(profile, probe)


def _security_status(args: list[str]) -> int:
    """Existence check via /usr/bin/security; output discarded unread."""
    completed = subprocess.run(
        ["/usr/bin/security", *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        env=subscription_environment(),
    )
    return completed.returncode
