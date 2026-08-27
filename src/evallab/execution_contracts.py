"""Immutable execution contracts, DTOs, and validation for runner and queue subsystems.

Key invariants:
- Immutable dataclasses for run requests, dispatch capacities, process outcomes, and authorizations.
- Non-secret environment allowlisting plus narrowly scoped, redacted credential routing.
- Strict request parameter validation (job names, timeouts, concurrency, billable approval).
- Standard Harbor command construction and transient exception classification.
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from evallab.schemas import (
    RunProvenance,
    StandingApprovalsPolicy,
)

CONTROL_AGENTS = frozenset({"oracle", "nop"})
SAFE_JOB_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
DEFAULT_TRIAL_TIMEOUT_SECONDS = 1_800
MAX_TRIAL_TIMEOUT_SECONDS = 21_600
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30.0
SUPPORT_COMMAND_TIMEOUT_SECONDS = 10
WATCHDOG_POLL_SECONDS = 0.1

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
_KNOWN_TRANSIENT_PROVIDER_EXCEPTIONS: dict[str, str] = {
    "ApiRateLimitError": "transient_harness:provider_http_429",
    "ApiInternalServerError": "transient_harness:provider_http_5xx",
    "ApiOverloadedError": "transient_harness:provider_http_5xx",
}
_PROVIDER_WRAPPER_EXCEPTIONS: frozenset[str] = frozenset(
    {
        "AgentRunError",
        "NonZeroAgentExitCodeError",
        "UnknownApiError",
    }
)
_SUBSCRIPTION_ENVIRONMENT_KEYS: frozenset[str] = frozenset(
    {
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
)

DEEPSEEK_CREDENTIAL_ENVIRONMENT_KEYS: frozenset[str] = frozenset(
    {"DEEPSEEK_API_KEY", "MSWEA_API_KEY"}
)
REDACTED_SECRET_VALUE = "<redacted>"

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
    "mini-swe-agent": "evallab.harbor_deepseek:SecretSafeDeepSeekMiniSweAgent",
}

DEEPSEEK_MODEL_SELECTOR = "deepseek/deepseek-v4-flash"
DEEPSEEK_SECRET_COMPOSE = Path("containers/deepseek-v4-flash-secret.compose.yaml")

HARBOR_STATE_JOURNAL_PLUGIN = "evallab.harbor_state_journal:StateJournalPlugin"

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class ExecutionFailure(RuntimeError):
    """Base error for trial execution failures."""

    reason_code = "execution_failed"


class TrialTimeoutFailure(ExecutionFailure):
    """Raised when trial exceeds allowed wall-clock timeout."""

    reason_code = "trial_wall_clock_timeout"


class TransientHarnessFailure(ExecutionFailure):
    """Raised when execution encounters a transient provider/harness error eligible for retry."""

    def __init__(self, reason_code: str, *, message: str | None = None) -> None:
        self.reason_code = reason_code
        super().__init__(message or reason_code)


@dataclass(frozen=True)
class RunRequest:
    """Immutable specification for one trial execution invocation."""

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


@dataclass(frozen=True)
class HarborProcessResult:
    """Outcome of running a Harbor subprocess under watchdog supervision."""

    returncode: int
    timed_out: bool
    log_path: Path
    timed_out_trial: str | None = None


@dataclass(frozen=True)
class PaidRunAuthorization:
    """One recorded human decision to let a specific queued spec spend money."""

    spec_id: str
    actor: str
    authorized_at: datetime
    quota_override: bool = False


@dataclass(frozen=True)
class DispatchCapacity:
    """Explicit global limits for one concurrent dispatch batch."""

    max_specs_per_tick: int | None = None
    max_active_trials: int | None = None
    per_agent_active_trials: dict[str, int] | None = None

    def __post_init__(self) -> None:
        values = [
            self.max_specs_per_tick,
            self.max_active_trials,
            *(self.per_agent_active_trials or {}).values(),
        ]
        if any(value is not None and value < 1 for value in values):
            raise ValueError("dispatch capacity values must be positive")


def new_ulid(*, timestamp_ms: int | None = None, randomness: int | None = None) -> str:
    """Return a lexically sortable ULID without adding a runtime ID dependency."""
    millis = timestamp_ms if timestamp_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
    if not 0 <= millis < 2**48:
        raise ValueError("ULID timestamp is outside the 48-bit range")
    random_bits = randomness if randomness is not None else secrets.randbits(80)
    if not 0 <= random_bits < 2**80:
        raise ValueError("ULID randomness is outside the 80-bit range")
    value = (millis << 80) | random_bits
    chars: list[str] = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def load_policy(path: Path) -> StandingApprovalsPolicy:
    """Load and validate a standing-approvals policy from YAML."""
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Cannot load standing-approvals policy: {exc}") from exc
    try:
        return StandingApprovalsPolicy.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"Invalid standing-approvals policy: {exc}") from exc


def subscription_environment(
    environment: Mapping[str, str] | None = None,
    *,
    include_deepseek_credentials: bool = False,
) -> dict[str, str]:
    """Build Harbor's environment from explicit non-secret and credential allowlists.

    DeepSeek credentials cross the host boundary only for the repo-owned
    mini-swe-agent adapter. ``MSWEA_API_KEY`` is accepted as an alias and copied
    to the canonical ``DEEPSEEK_API_KEY`` name without exposing either value.
    """
    source = os.environ if environment is None else environment
    sanitized = {key: source[key] for key in _SUBSCRIPTION_ENVIRONMENT_KEYS if key in source}
    if include_deepseek_credentials:
        for key in DEEPSEEK_CREDENTIAL_ENVIRONMENT_KEYS:
            if source.get(key):
                sanitized[key] = source[key]
        if "DEEPSEEK_API_KEY" not in sanitized and sanitized.get("MSWEA_API_KEY"):
            sanitized["DEEPSEEK_API_KEY"] = sanitized["MSWEA_API_KEY"]
    sanitized["AGY_FORCE_AUTH_JSON"] = "1"
    sanitized["CODEX_FORCE_AUTH_JSON"] = "1"
    sanitized["CLAUDE_FORCE_OAUTH"] = "1"
    sanitized["REWARDKIT_FORCE_OAUTH"] = "1"
    return sanitized


def redact_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Return a log-safe copy with every admitted DeepSeek value replaced."""
    return {
        key: REDACTED_SECRET_VALUE
        if key in DEEPSEEK_CREDENTIAL_ENVIRONMENT_KEYS and value
        else value
        for key, value in environment.items()
    }


def validate_request(request: RunRequest) -> None:
    """Validate that a RunRequest adheres to directory, name, timeout, and billable invariants."""
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


def resolve_harbor_agent(agent: str) -> str:
    """Use the lab-owned adapter where Harbor supports custom import paths."""
    return HARBOR_AGENT_IMPORT_PATHS.get(agent, agent)


def resolve_harbor_model(agent: str, model: str | None) -> str | None:
    """Translate a local CLI model identifier to Harbor's expected model string."""
    if model is None:
        return None
    return LOCAL_TO_HARBOR_MODEL.get((agent, model), model)


def build_command(request: RunRequest) -> list[str]:
    """Build the exact Harbor CLI invocation command for a RunRequest."""
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
    if request.agent == "mini-swe-agent":
        if harbor_model != DEEPSEEK_MODEL_SELECTOR:
            raise ValueError(f"mini-swe-agent requires the exact model {DEEPSEEK_MODEL_SELECTOR}")
        command.extend(
            [
                "--n-concurrent-agents",
                "1",
                "--n-tasks",
                "1",
                "--max-retries",
                "0",
                "--agent-kwarg",
                "cost_limit=2.5",
                "--agent-kwarg",
                "max_tokens=8192",
            ]
        )
    if request.extra_instruction_path is not None:
        command.extend(["--extra-instruction-path", str(request.extra_instruction_path)])
    return command


def subscription_command(
    request: RunRequest,
    harbor_command: list[str],
    *,
    repo_root: Path,
) -> list[str]:
    """Add the credential transport required by subscription or API-key profiles."""
    if request.agent == "claude-code":
        wrapper = (repo_root / "scripts/with-claude-auth").resolve()
        if not wrapper.is_file():
            raise RuntimeError(f"Claude subscription wrapper is missing: {wrapper}")
        return [str(wrapper), *harbor_command]
    if request.agent == "mini-swe-agent":
        if request.model != DEEPSEEK_MODEL_SELECTOR:
            raise RuntimeError(
                f"the mini-swe-agent execution lane is pinned to {DEEPSEEK_MODEL_SELECTOR}"
            )
        overlay = (repo_root / DEEPSEEK_SECRET_COMPOSE).resolve()
        if not overlay.is_file():
            raise RuntimeError(f"DeepSeek secret overlay is missing: {overlay}")
        return [*harbor_command, "--extra-docker-compose", str(overlay)]
    return harbor_command


def transient_provider_reason(text: str) -> str | None:
    """Classify transient 429 or 5xx HTTP provider reasons from unstructured text."""
    if _PROVIDER_429.search(text):
        return "transient_harness:provider_http_429"
    if _PROVIDER_5XX.search(text):
        return "transient_harness:provider_http_5xx"
    return None


def transient_provider_exception(result: Mapping[str, Any]) -> str | None:
    """Classify structured provider-facing trial exceptions."""
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
    if message.startswith("Command failed"):
        _, separator, adapter_output = message.rpartition("\nstdout:")
        if not separator:
            return None
        message = adapter_output
    elif exception_type == "NonZeroAgentExitCodeError":
        return None
    return transient_provider_reason(message)
