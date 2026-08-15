from __future__ import annotations

import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evallab.results import load_job
from evallab.schemas import ExperimentMatrix, MatrixRun, RunProvenance

CONTROL_AGENTS = {"oracle", "nop"}
SAFE_JOB_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
DEFAULT_TRIAL_TIMEOUT_SECONDS = 1_800
MAX_TRIAL_TIMEOUT_SECONDS = 21_600
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
_SUBSCRIPTION_ENVIRONMENT_KEYS = {
    "CODEX_HOME",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
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
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


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
    return {key: source[key] for key in _SUBSCRIPTION_ENVIRONMENT_KEYS if key in source}


def transient_provider_reason(text: str) -> str | None:
    if _PROVIDER_429.search(text):
        return "transient_harness:provider_http_429"
    if _PROVIDER_5XX.search(text):
        return "transient_harness:provider_http_5xx"
    return None


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
        env=environment,
    )
    if listed.returncode != 0:
        raise RuntimeError("cannot inspect Docker for Harbor-labeled containers")
    matches: set[str] = set()
    for container_id in listed.stdout.split():
        inspected = runner(
            ["docker", "inspect", "--format", "{{json .Config.Labels}}", container_id],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
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
    completed = runner(
        ["docker", "rm", "-f", "--", *orphaned],
        check=False,
        capture_output=True,
        text=True,
        env=subscription_environment(),
    )
    if completed.returncode != 0:
        raise RuntimeError("failed to remove Harbor-labeled orphan containers")
    return orphaned


def tool_version(command: str) -> str | None:
    executable = shutil.which(command)
    if not executable:
        return None
    completed = subprocess.run(
        [executable, "--version"],
        check=False,
        capture_output=True,
        text=True,
        env=subscription_environment(),
    )
    output = (completed.stdout or completed.stderr).strip()
    return output.splitlines()[0] if output else None


def git_state(root: Path) -> dict[str, Any]:
    if not (root / ".git").exists():
        return {"commit": None, "dirty": None}
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        env=subscription_environment(),
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        env=subscription_environment(),
    )
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
    return command


@dataclass(frozen=True)
class HarborProcessResult:
    returncode: int
    timed_out: bool
    log_path: Path


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
        process.wait()


def run_harbor_process(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    log_path: Path,
) -> HarborProcessResult:
    """Run Harbor in its own process group under an executor-owned deadline."""
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
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _terminate_process_group(process)
            return HarborProcessResult(
                returncode=process.returncode if process.returncode is not None else -1,
                timed_out=True,
                log_path=log_path,
            )
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


def _transient_reason_from_job(job_dir: Path, process_log: Path) -> str | None:
    texts = [_tail_text(process_log)]
    if job_dir.is_dir():
        for path in sorted(job_dir.rglob("result.json")):
            try:
                payload = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or not payload.get("exception_info"):
                continue
            texts.append(json.dumps(payload["exception_info"], sort_keys=True))
        for pattern in ("job.log", "trial.log"):
            for path in sorted(job_dir.rglob(pattern)):
                texts.append(_tail_text(path))
    return transient_provider_reason("\n".join(texts))


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
    command = build_command(request)
    executor_log = _executor_log_path(request)
    containers_before = harbor_container_ids(request.task)
    started = datetime.now(UTC)
    process = run_harbor_process(
        command,
        cwd=repo_root,
        timeout_seconds=request.job_timeout_seconds,
        log_path=executor_log,
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
        cleanup_new_harbor_containers(
            request.task,
            containers_before,
            project_prefixes=_harbor_project_prefixes(job_dir),
        )
        raise TrialTimeoutFailure(
            f"Harbor exceeded the executor deadline of {request.job_timeout_seconds}s; "
            f"inspect {executor_log}"
        )
    transient_reason = _transient_reason_from_job(job_dir, executor_log)
    if process.returncode != 0:
        cleanup_new_harbor_containers(
            request.task,
            containers_before,
            project_prefixes=_harbor_project_prefixes(job_dir),
        )
        if transient_reason is not None:
            raise TransientHarnessFailure(transient_reason)
        raise ExecutionFailure(
            f"Harbor exited with {process.returncode}; inspect {executor_log}"
        )
    load_job(job_dir)
    if transient_reason is not None:
        cleanup_new_harbor_containers(
            request.task,
            containers_before,
            project_prefixes=_harbor_project_prefixes(job_dir),
        )
        raise TransientHarnessFailure(transient_reason)
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
