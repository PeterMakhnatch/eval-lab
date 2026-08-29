from __future__ import annotations

import json
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from evallab.execution_contracts import (
    _SUBSCRIPTION_ENVIRONMENT_KEYS,
    CONTROL_AGENTS,
    DEEPSEEK_ALLOWED_MODEL,
    DEEPSEEK_ALLOWED_MODEL_ENV,
    DEEPSEEK_CREDENTIAL_ENVIRONMENT_KEYS,
    DEEPSEEK_MODEL_SELECTOR,
    DEEPSEEK_PROXY_CAPABILITY_ENV,
    DEEPSEEK_PROXY_HOST,
    DEEPSEEK_PROXY_SCRIPT,
    DEEPSEEK_PROXY_SCRIPT_ENV,
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
    ExecutionFailure,
    HarborProcessResult,
    RedactingBinaryWriter,
    RunRequest,
    TransientHarnessFailure,
    TrialTimeoutFailure,
    build_command,
    collected_secret_values,
    materialize_deepseek_secret_file,
    persist_private_bytes,
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
    "RunRequest",
    "TransientHarnessFailure",
    "TrialTimeoutFailure",
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
    "run_experiment",
    "run_harbor_process",
    "subscription_command",
    "subscription_environment",
    "tool_version",
    "transient_provider_exception",
    "transient_provider_reason",
    "validate_request",
]
HARBOR_COMPOSE_CONFIG_LABEL = "com.docker.compose.project.config_files"
HARBOR_COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
HARBOR_COMPOSE_WORKDIR_LABEL = "com.docker.compose.project.working_dir"


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


def run_harbor_process(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    log_path: Path,
    job_dir: Path | None = None,
    trial_timeout_seconds: float | None = None,
    lease_path: Path | None = None,
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
) -> HarborProcessResult:
    """Run Harbor under an aggregate fail-safe and a per-trial watchdog."""
    repo_imports = (*HARBOR_AGENT_IMPORT_PATHS.values(), HARBOR_STATE_JOURNAL_PLUGIN)
    deepseek_adapter = HARBOR_AGENT_IMPORT_PATHS["mini-swe-agent"]
    deepseek_lane = deepseek_adapter in command
    runtime_environment = subscription_environment(include_deepseek_credentials=deepseek_lane)
    secret_values = collected_secret_values()
    owned_secret_dir: Path | None = None
    owned_secret_path: Path | None = None
    if deepseek_lane:
        capability = secrets.token_urlsafe(32)
        runtime_environment[DEEPSEEK_PROXY_CAPABILITY_ENV] = capability
        runtime_environment["DEEPSEEK_API_KEY"] = capability
        runtime_environment["MSWEA_API_KEY"] = capability
        runtime_environment[DEEPSEEK_ALLOWED_MODEL_ENV] = os.environ.get(
            DEEPSEEK_ALLOWED_MODEL_ENV, DEEPSEEK_ALLOWED_MODEL
        )
        runtime_environment.setdefault(
            "EVALLAB_DEEPSEEK_MAX_REQUESTS",
            os.environ.get("EVALLAB_DEEPSEEK_MAX_REQUESTS", "8"),
        )
        runtime_environment.setdefault(
            "EVALLAB_DEEPSEEK_MAX_INPUT_TOKENS",
            os.environ.get("EVALLAB_DEEPSEEK_MAX_INPUT_TOKENS", "32768"),
        )
        runtime_environment.setdefault(
            "EVALLAB_DEEPSEEK_MAX_OUTPUT_TOKENS",
            os.environ.get("EVALLAB_DEEPSEEK_MAX_OUTPUT_TOKENS", "4096"),
        )
        runtime_environment.setdefault(
            "EVALLAB_DEEPSEEK_MAX_COST_MICROS",
            os.environ.get("EVALLAB_DEEPSEEK_MAX_COST_MICROS", "500000"),
        )
        runtime_environment.setdefault(
            "EVALLAB_DEEPSEEK_INPUT_COST_MICROS_PER_MILLION",
            os.environ.get("EVALLAB_DEEPSEEK_INPUT_COST_MICROS_PER_MILLION", "280000"),
        )
        runtime_environment.setdefault(
            "EVALLAB_DEEPSEEK_OUTPUT_COST_MICROS_PER_MILLION",
            os.environ.get("EVALLAB_DEEPSEEK_OUTPUT_COST_MICROS_PER_MILLION", "420000"),
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
        runtime_environment[DEEPSEEK_PROXY_SCRIPT_ENV] = str((cwd / DEEPSEEK_PROXY_SCRIPT).resolve())
        secret_values = collected_secret_values({**os.environ, **runtime_environment})
    if any(import_path in command for import_path in repo_imports):
        source_root = cwd / "src"
        if source_root.is_dir():
            inherited_pythonpath = os.environ.get("PYTHONPATH")
            runtime_environment["PYTHONPATH"] = os.pathsep.join(
                str(path) for path in (source_root, inherited_pythonpath) if path
            )
    secret_bytes = tuple(value.encode() for value in secret_values)
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
    except Exception:
        os.close(write_fd)
        os.close(read_fd)
        writer.close()
        _unlink_secret_dir(owned_secret_dir, owned_secret_path)
        raise
    os.close(write_fd)
    pump.start()
    started = time.monotonic()
    if lease_path is not None:
        try:
            if lease_path.is_file():
                lease_path.touch()
        except OSError:
            pass
    last_heartbeat = started
    first_seen: dict[Path, float] = {}
    while True:
        returncode = process.poll()
        if returncode is not None:
            break
        now = time.monotonic()
        if lease_path is not None and now - last_heartbeat >= heartbeat_interval_seconds:
            try:
                if lease_path.is_file():
                    lease_path.touch()
            except OSError:
                pass
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
            _unlink_secret_dir(owned_secret_dir, owned_secret_path)
            return HarborProcessResult(
                returncode=(process.returncode if process.returncode is not None else -1),
                timed_out=True,
                log_path=log_path,
                timed_out_trial=timed_out_trial,
            )
        try:
            process.wait(timeout=WATCHDOG_POLL_SECONDS)
        except subprocess.TimeoutExpired:
            continue
    pump.join(timeout=5)
    _unlink_secret_dir(owned_secret_dir, owned_secret_path)
    return HarborProcessResult(
        returncode=returncode,
        timed_out=False,
        log_path=log_path,
    )


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
        "repository": git_state(repo_root),
    }
    if request.provenance is not None:
        metadata["experiment"] = request.provenance.model_dump(mode="json")
    if network_adaptation is not None:
        metadata["network_adaptation"] = asdict(network_adaptation)
    (job_dir / "lab-metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
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
) -> tuple[Path | None, NetworkAdaptation | None]:
    """Create a host-compatible, agent-policy execution copy of a task package.

    The source package is never modified. ``task.toml`` is rewritten only for
    host network compatibility and an explicit agent allowlist. Returns
    ``(None, None)`` when no execution-only rewrite is required.
    """
    task_toml_path = source / "task.toml"
    if not task_toml_path.is_file():
        raise ValueError(f"task.toml missing in {source}")
    original_text = task_toml_path.read_text(encoding="utf-8")
    adapted_text, adaptation = adapt_task_toml_for_host(original_text)
    staged_text = with_agent_network_allowlist(adapted_text, agent_allowed_hosts)
    if staged_text == original_text:
        return None, None

    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, staging_dir)
    (staging_dir / "task.toml").write_text(staged_text, encoding="utf-8")

    manifest: dict[str, Any] = {
        "schema_version": "1.0",
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


def run_experiment(request: RunRequest, *, repo_root: Path) -> Path:
    validate_request(request)
    if request.agent == "mini-swe-agent":
        decision = preflight_request(request)
        if not decision.proceed:
            raise RuntimeError(f"DeepSeek credential preflight stopped: {decision.reason}")
    if not shutil.which("harbor"):
        raise RuntimeError("harbor is not installed or not on PATH")

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
                (DEEPSEEK_PROXY_HOST,) if request.agent == "mini-swe-agent" else ()
            ),
        )
        if staged_task is not None:
            staged_request: RunRequest = replace(request, task=staged_task)
        else:
            staged_request = request

        _write_network_adaptation(request, adaptation)

        harbor_command = build_command(staged_request)
        command = subscription_command(staged_request, harbor_command, repo_root=repo_root)
        containers_before = harbor_container_ids(staged_request.task)
        _write_executor_state(
            request,
            started_at=started,
            status="running",
            log_path=executor_log,
        )
        try:
            process = run_harbor_process(
                command,
                cwd=repo_root,
                timeout_seconds=request.job_timeout_seconds,
                log_path=executor_log,
                job_dir=job_dir,
                trial_timeout_seconds=request.timeout_seconds,
                lease_path=request.lease_path,
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
        _write_executor_state(
            request,
            started_at=started,
            status="failed" if process.timed_out or process.returncode != 0 else "running",
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
            network_adaptation=adaptation,
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
        _sanitize_persisted_job_tree(job_dir, tuple(value.encode() for value in collected_secret_values()))
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
        job = load_job(job_dir)
        _write_executor_state(
            request,
            started_at=started,
            status="failed" if transient_reason is not None else "completed",
            log_path=executor_log,
            finished_at=finished,
            process=process,
        )
        if transient_reason is not None:
            cleanup_failure = _cleanup_failure(staged_request, containers_before, job_dir)
            cleanup_detail = f"; {cleanup_failure}" if cleanup_failure else ""
            raise TransientHarnessFailure(
                transient_reason,
                message=transient_reason + cleanup_detail,
            )
        evidence_root = os.environ.get("EVALLAB_EVIDENCE_STORE_ROOT")
        if evidence_root:
            try:
                from evallab.evidence_store import archive_evidence

                archive_evidence(
                    job_dir,
                    Path(evidence_root),
                    record_id=str(job.id),
                    kind="job",
                )
            except Exception as exc:
                with suppress(Exception):
                    (job_dir / "evidence-archive-error.txt").write_text(
                        f"{type(exc).__name__}: {exc}\n"
                    )
        return job_dir
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
