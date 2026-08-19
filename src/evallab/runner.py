from __future__ import annotations

import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from evallab.results import load_job
from evallab.schemas import ExperimentMatrix, MatrixRun, RunProvenance

CONTROL_AGENTS = {"oracle", "nop"}
SAFE_JOB_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
DEFAULT_TRIAL_TIMEOUT_SECONDS = 1_800
MAX_TRIAL_TIMEOUT_SECONDS = 21_600
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30.0
SUPPORT_COMMAND_TIMEOUT_SECONDS = 10
WATCHDOG_POLL_SECONDS = 0.1
HARBOR_COMPOSE_CONFIG_LABEL = "com.docker.compose.project.config_files"
HARBOR_COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
HARBOR_COMPOSE_WORKDIR_LABEL = "com.docker.compose.project.working_dir"
_PROVIDER_429 = re.compile(
    r"(?:http(?:/\d(?:\.\d)?)?\s*429|status(?:\s+code)?\s*[:=]?\s*429|"
    r"429.{0,80}(?:rate.?limit|too many requests)|"
    r"(?:rate.?limit|too many requests).{0,80}429)",
    re.IGNORECASE | re.DOTALL,
)
_PROVIDER_5XX = re.compile(
    r"(?:http(?:/\d(?:\.\d)?)?\s*5\d\d|status(?:\s+code)?\s*[:=]?\s*5\d\d|"
    r"\b5\d\d\b.{0,80}(?:provider|upstream|api|server|gateway|service unavailable))",
    re.IGNORECASE | re.DOTALL,
)
_KNOWN_TRANSIENT_PROVIDER_EXCEPTIONS = {
    "ApiRateLimitError": "transient_harness:provider_http_429",
    "ApiInternalServerError": "transient_harness:provider_http_5xx",
    "ApiOverloadedError": "transient_harness:provider_http_5xx",
}
_PROVIDER_WRAPPER_EXCEPTIONS = {
    "AgentRunError",
    "NonZeroAgentExitCodeError",
    "UnknownApiError",
}
_SUBSCRIPTION_ENVIRONMENT_KEYS = {
    "CLAUDE_FORCE_OAUTH",
    "CODEX_HOME",
    "CODEX_FORCE_AUTH_JSON",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "HOME",
    "HARBOR_CLAUDE_KEYCHAIN_ACCOUNT",
    "HARBOR_CLAUDE_KEYCHAIN_SERVICE",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "REWARDKIT_FORCE_OAUTH",
    "SECURITYSESSIONID",
    "SHELL",
    "SSH_AUTH_SOCK",
    "TERM",
    "TMPDIR",
    "USER",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
}


def _run_text_command(
    command: list[str],
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, **kwargs)


class ExecutionFailure(RuntimeError):
    reason_code = "execution_failed"


class TrialTimeoutFailure(ExecutionFailure):
    reason_code = "trial_wall_clock_timeout"


class TransientHarnessFailure(ExecutionFailure):
    def __init__(self, reason_code: str, *, message: str | None = None) -> None:
        self.reason_code = reason_code
        super().__init__(message or reason_code)


@dataclass(frozen=True)
class RunRequest:
    task: Path
    agent: str
    name: str
    jobs_dir: Path
    environment: str = "docker"
    model: str | None = None
    concurrency: int = 1
    attempts: int = 1
    timeout_seconds: int = DEFAULT_TRIAL_TIMEOUT_SECONDS
    allow_billable: bool = False
    provenance: RunProvenance | None = None
    lease_path: Path | None = None
    extra_instruction_path: Path | None = None

    @property
    def job_timeout_seconds(self) -> int:
        """Conservative process deadline: one wall-clock allowance per attempt."""
        return self.timeout_seconds * self.attempts


def subscription_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build Harbor's environment from a non-secret allowlist.

    Values of API-key variables are never accessed or forwarded. Subscription
    agents authenticate through their auth file or Keychain integration.
    """
    source = os.environ if environment is None else environment
    sanitized = {
        key: source[key] for key in _SUBSCRIPTION_ENVIRONMENT_KEYS if key in source
    }
    # These switches are routing metadata, not credentials. Force Harbor's
    # installed agents onto subscription authentication even when a caller did
    # not source the optional interactive helper. API-key variables are never
    # looked up above and therefore cannot leak into a child process.
    sanitized["CODEX_FORCE_AUTH_JSON"] = "1"
    sanitized["CLAUDE_FORCE_OAUTH"] = "1"
    sanitized["REWARDKIT_FORCE_OAUTH"] = "1"
    return sanitized


def transient_provider_reason(text: str) -> str | None:
    if _PROVIDER_429.search(text):
        return "transient_harness:provider_http_429"
    if _PROVIDER_5XX.search(text):
        return "transient_harness:provider_http_5xx"
    return None


def transient_provider_exception(result: Mapping[str, Any]) -> str | None:
    """Classify only structured provider-facing trial exceptions."""
    exception = result.get("exception_info")
    if not isinstance(exception, Mapping):
        return None
    exception_type = str(exception.get("exception_type") or "")
    known_reason = _KNOWN_TRANSIENT_PROVIDER_EXCEPTIONS.get(exception_type)
    if known_reason is not None:
        return known_reason
    if exception_type not in _PROVIDER_WRAPPER_EXCEPTIONS:
        return None
    message = exception.get("exception_message") or exception.get("message")
    if not isinstance(message, str):
        return None
    # Installed-agent failures embed the full shell command (and therefore the
    # task prompt) before the adapter's output. Never let a task that mentions
    # an HTTP status manufacture a retry classification.
    if message.startswith("Command failed"):
        _, separator, adapter_output = message.rpartition("\nstdout:")
        if not separator:
            return None
        message = adapter_output
    elif exception_type == "NonZeroAgentExitCodeError":
        return None
    return transient_provider_reason(message)


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
        project == prefix or project.startswith(prefix + "__")
        for prefix in project_prefixes
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
            raise RuntimeError(
                "cannot inspect Docker for Harbor-labeled containers"
            ) from exc
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


def validate_request(request: RunRequest) -> None:
    if not request.task.is_dir():
        raise ValueError(f"Task directory does not exist: {request.task}")
    if not (request.task / "task.toml").is_file():
        raise ValueError(f"Task directory has no task.toml: {request.task}")
    if not SAFE_JOB_NAME.fullmatch(request.name):
        raise ValueError("Job names must be 3-80 lowercase letters, numbers, or hyphens")
    if (
        request.concurrency < 1
        or request.attempts < 1
        or not 1 <= request.timeout_seconds <= MAX_TRIAL_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "Concurrency and attempts must be positive; timeout must be 1-21600 seconds"
        )
    if request.agent not in CONTROL_AGENTS and not request.allow_billable:
        raise ValueError(
            f"Agent {request.agent!r} may invoke a model. Pass --allow-billable "
            "after reviewing credentials, model, and expected cost."
        )
    if request.model and request.agent in CONTROL_AGENTS:
        raise ValueError(f"The {request.agent} control does not accept a model")
    if request.model and not request.allow_billable:
        raise ValueError("A model requires --allow-billable")


def build_command(request: RunRequest) -> list[str]:
    command = [
        "harbor",
        "run",
        "--path",
        str(request.task),
        "--agent",
        request.agent,
        "--env",
        request.environment,
        "--job-name",
        request.name,
        "--jobs-dir",
        str(request.jobs_dir),
        "--n-concurrent",
        str(request.concurrency),
        "--n-attempts",
        str(request.attempts),
    ]
    if request.model:
        command.extend(["--model", request.model])
    if request.extra_instruction_path is not None:
        command.extend(["--extra-instruction-path", str(request.extra_instruction_path)])
    return command


def subscription_command(
    request: RunRequest,
    harbor_command: list[str],
    *,
    repo_root: Path,
) -> list[str]:
    """Route Claude through the Keychain-only OAuth wrapper.

    Codex consumes ``~/.codex/auth.json`` after ``subscription_environment``
    sets its non-secret routing flag. Claude's Harbor adapter needs the OAuth
    token in its immediate child environment, so the wrapper reads only the
    configured Keychain item and never aliases it to an API-key variable.
    """
    if request.agent != "claude-code":
        return harbor_command
    wrapper = (repo_root / "scripts/with-claude-auth").resolve()
    if not wrapper.is_file():
        raise RuntimeError(f"Claude subscription wrapper is missing: {wrapper}")
    return [str(wrapper), *harbor_command]


@dataclass(frozen=True)
class HarborProcessResult:
    returncode: int
    timed_out: bool
    log_path: Path
    timed_out_trial: str | None = None


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
        if candidate.is_dir()
        and "__" in candidate.name
        and not _trial_is_terminal(candidate)
    )


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
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=subscription_environment(),
            start_new_session=True,
        )
        started = time.monotonic()
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
                return HarborProcessResult(
                    returncode=(
                        process.returncode if process.returncode is not None else -1
                    ),
                    timed_out=True,
                    log_path=log_path,
                    timed_out_trial=timed_out_trial,
                )
            try:
                process.wait(timeout=WATCHDOG_POLL_SECONDS)
            except subprocess.TimeoutExpired:
                continue
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
    (job_dir / "lab-metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    )


def run_experiment(request: RunRequest, *, repo_root: Path) -> Path:
    validate_request(request)
    if not shutil.which("harbor"):
        raise RuntimeError("harbor is not installed or not on PATH")

    job_dir = request.jobs_dir / request.name
    if job_dir.exists():
        raise FileExistsError(
            f"Refusing to reuse existing job directory: {job_dir}. Choose a new explicit run name."
        )

    request.jobs_dir.mkdir(parents=True, exist_ok=True)
    harbor_command = build_command(request)
    command = subscription_command(request, harbor_command, repo_root=repo_root)
    executor_log = _executor_log_path(request)
    containers_before = harbor_container_ids(request.task)
    started = datetime.now(UTC)
    process = run_harbor_process(
        command,
        cwd=repo_root,
        timeout_seconds=request.job_timeout_seconds,
        log_path=executor_log,
        job_dir=job_dir,
        trial_timeout_seconds=request.timeout_seconds,
        lease_path=request.lease_path,
    )
    finished = datetime.now(UTC)
    _write_run_metadata(
        request,
        repo_root=repo_root,
        command=command,
        started=started,
        finished=finished,
        process=process,
    )

    if process.timed_out:
        cleanup_failure = _cleanup_failure(request, containers_before, job_dir)
        scope = (
            f"trial {process.timed_out_trial!r} exceeded {request.timeout_seconds}s"
            if process.timed_out_trial is not None
            else f"Harbor exceeded aggregate fail-safe {request.job_timeout_seconds}s"
        )
        cleanup_detail = f"; {cleanup_failure}" if cleanup_failure else ""
        raise TrialTimeoutFailure(
            f"{scope}; inspect {executor_log}{cleanup_detail}"
        )
    transient_reason = _transient_reason_from_job(job_dir)
    if process.returncode != 0:
        cleanup_failure = _cleanup_failure(request, containers_before, job_dir)
        cleanup_detail = f"; {cleanup_failure}" if cleanup_failure else ""
        if transient_reason is not None:
            raise TransientHarnessFailure(
                transient_reason,
                message=transient_reason + cleanup_detail,
            )
        raise ExecutionFailure(
            f"Harbor exited with {process.returncode}; inspect {executor_log}{cleanup_detail}"
        )
    load_job(job_dir)
    if transient_reason is not None:
        cleanup_failure = _cleanup_failure(request, containers_before, job_dir)
        cleanup_detail = f"; {cleanup_failure}" if cleanup_failure else ""
        raise TransientHarnessFailure(
            transient_reason,
            message=transient_reason + cleanup_detail,
        )
    return job_dir


def load_matrix(path: Path) -> ExperimentMatrix:
    try:
        return ExperimentMatrix.model_validate_json(path.read_text())
    except (OSError, ValidationError) as exc:
        raise ValueError(f"Invalid experiment matrix {path}: {exc}") from exc


def request_from_matrix(
    matrix: ExperimentMatrix, run: MatrixRun, *, repo_root: Path
) -> RunRequest:
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
            keychain_account=env.get(
                "HARBOR_CLAUDE_KEYCHAIN_ACCOUNT", env.get("USER", "")
            ),
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
