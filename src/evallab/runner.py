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
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from evallab.harbor_network import NetworkAdaptation, adapt_task_toml_for_host
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
    # Antigravity (agy) reads a plaintext OAuth token file, not an API key, so
    # forwarding these keeps the lab subscriptions-only. Harbor's
    # antigravity_cli adapter resolves AGY_AUTH_JSON_PATH first, then falls
    # back to ~/.gemini/antigravity-cli/antigravity-oauth-token when
    # AGY_FORCE_AUTH_JSON is truthy. Mint the token with:
    #   ~/.local/share/uv/tools/harbor/bin/python \
    #     -m harbor.agents.installed.antigravity_login
    # CURSOR_API_KEY is deliberately absent: Harbor's cursor_cli adapter
    # requires an API key, which this lab's profiles forbid by name. Adding it
    # is a policy decision for a human, not a plumbing fix.
    "AGY_AUTH_JSON_PATH",
    "AGY_FORCE_AUTH_JSON",
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
    sanitized = {key: source[key] for key in _SUBSCRIPTION_ENVIRONMENT_KEYS if key in source}
    # These switches are routing metadata, not credentials. Force Harbor's
    # installed agents onto subscription authentication even when a caller did
    # not source the optional interactive helper. API-key variables are never
    # looked up above and therefore cannot leak into a child process.
    sanitized["AGY_FORCE_AUTH_JSON"] = "1"
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


# Mapping from local CLI model identifiers to Harbor-compatible model identifiers.
#
# The only difference between the two namespaces is the provider prefix. Harbor
# requires one; `agy` does not accept one:
#  - Harbor rejects a bare id (`antigravity_cli.py:776-777`,
#    `ValueError: Model name must be in the format provider/model_name`). That
#    check lives in the run path, so a bare id fails mid-trial, after the
#    container is built.
#  - Harbor then strips the prefix (`antigravity_cli.py:779`) and passes the
#    remainder verbatim as `agy --model <remainder>` (`:810-819`).
#
# The thinking level belongs *in* the model id, because that is how `agy` names
# its models: `agy models` lists `gemini-3.7-flash-high`, `-medium`, `-low` as
# distinct ids. Proven by a live containerised trial on 2026-08-19:
#
#   --model google/gemini-3.7-flash      -> agy exits non-zero:
#       "invalid model selection (--model "gemini-3.7-flash" --effort ""):
#        --model gemini-3.7-flash requires --effort (available: low, medium, high)"
#   --model google/gemini-3.7-flash-high -> trial completes, primary reward 1.0
#
# Do not try to send the level separately as `--agent-kwarg reasoning_effort=`.
# Harbor's adapter declares no `--effort` flag at all (`CLI_FLAGS` holds only
# `sandbox`, `:47-53`); the kwarg only writes a `~/.agy` settings file that
# Harbor's own comment says the *legacy* CLI reads (`:806-809`). It never reaches
# the Go CLI, which is why the run above failed with an empty `--effort`.
#
# Cost note: `litellm.model_cost` has no entry for the suffixed id
# (`antigravity_cli.py:625-629`), so Harbor cannot price these trials. That is
# correct here - this lane bills against a subscription, not per token, and this
# lab records provider dollar figures only as list-price equivalents.
LOCAL_TO_HARBOR_MODEL: dict[tuple[str, str], str] = {
    ("antigravity-cli", "gemini-3.7-flash-high"): "google/gemini-3.7-flash-high",
    ("antigravity-cli", "gemini-3.7-flash-medium"): "google/gemini-3.7-flash-medium",
    ("antigravity-cli", "gemini-3.7-flash-low"): "google/gemini-3.7-flash-low",
    ("antigravity-cli", "gemini-3.1-pro-high"): "google/gemini-3.1-pro-high",
    ("antigravity-cli", "claude-sonnet-4-6"): "google/claude-sonnet-4-6",
}

HARBOR_AGENT_IMPORT_PATHS: dict[str, str] = {
    "codex": "evallab.harbor_codex:PinnedCodex",
    "antigravity-cli": "evallab.harbor_antigravity:AntigravityCliCapture",
}

HARBOR_STATE_JOURNAL_PLUGIN = "evallab.harbor_state_journal:StateJournalPlugin"


def resolve_harbor_agent(agent: str) -> str:
    """Use the lab-owned adapter where Harbor supports custom import paths."""
    return HARBOR_AGENT_IMPORT_PATHS.get(agent, agent)


def resolve_harbor_model(agent: str, model: str | None) -> str | None:
    """Translate a local CLI model identifier to Harbor's expected model string.

    Returns the mapped Harbor model if one is registered for (agent, model),
    or the original model identifier if not mapped.
    """
    if model is None:
        return None
    return LOCAL_TO_HARBOR_MODEL.get((agent, model), model)


def build_command(request: RunRequest) -> list[str]:
    command = [
        "harbor",
        "run",
        "--path",
        str(request.task),
        "--agent",
        resolve_harbor_agent(request.agent),
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
    command.extend(["--plugin", HARBOR_STATE_JOURNAL_PLUGIN])
    harbor_model = resolve_harbor_model(request.agent, request.model)
    if harbor_model:
        command.extend(["--model", harbor_model])
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
        if candidate.is_dir() and "__" in candidate.name and not _trial_is_terminal(candidate)
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
    runtime_environment = subscription_environment()
    repo_imports = (*HARBOR_AGENT_IMPORT_PATHS.values(), HARBOR_STATE_JOURNAL_PLUGIN)
    if any(import_path in command for import_path in repo_imports):
        source_root = cwd / "src"
        if source_root.is_dir():
            inherited_pythonpath = os.environ.get("PYTHONPATH")
            runtime_environment["PYTHONPATH"] = os.pathsep.join(
                str(path) for path in (source_root, inherited_pythonpath) if path
            )
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=runtime_environment,
            start_new_session=True,
        )
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
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


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
) -> tuple[Path | None, NetworkAdaptation | None]:
    """Create a host-compatible execution copy of the task package.

    The source package is never modified. Only ``task.toml`` is rewritten, and
    a ``run_manifest.json`` is added to the copy when a network adaptation is
    required. Returns ``(None, None)`` when the host can execute the canonical
    policy directly.
    """
    task_toml_path = source / "task.toml"
    if not task_toml_path.is_file():
        raise ValueError(f"task.toml missing in {source}")
    original_text = task_toml_path.read_text(encoding="utf-8")
    adapted_text, adaptation = adapt_task_toml_for_host(original_text)
    if adaptation is None:
        return None, None

    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, staging_dir)
    (staging_dir / "task.toml").write_text(adapted_text, encoding="utf-8")

    manifest = {
        "schema_version": "1.0",
        "network_adaptation": asdict(adaptation),
    }
    (staging_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return staging_dir, adaptation

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
    staging_dir = request.jobs_dir / ".exec-stage" / request.name
    executor_log = _executor_log_path(request)
    started = datetime.now(UTC)
    try:
        staged_task, adaptation = _stage_task_for_host(request.task, staging_dir)
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
