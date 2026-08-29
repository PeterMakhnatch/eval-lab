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
import stat
from collections.abc import Mapping
from contextlib import suppress
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
DEEPSEEK_PROXY_HOST = "deepseek-secret-proxy"
DEEPSEEK_PROXY_URL = "http://deepseek-secret-proxy:8080"
DEEPSEEK_PROXY_TOKEN = "evallab-proxy-placeholder"
DEEPSEEK_PROXY_SCRIPT = Path("containers/deepseek_secret_proxy.py")
DEEPSEEK_SECRET_FILE_ENV = "EVALLAB_DEEPSEEK_SECRET_FILE"
DEEPSEEK_PROXY_SCRIPT_ENV = "EVALLAB_DEEPSEEK_PROXY_SCRIPT"
DEEPSEEK_UPSTREAM_ENV = "EVALLAB_DEEPSEEK_UPSTREAM"
DEEPSEEK_PROXY_CAPABILITY_ENV = "EVALLAB_DEEPSEEK_PROXY_CAPABILITY"
DEEPSEEK_ALLOWED_MODEL_ENV = "EVALLAB_DEEPSEEK_ALLOWED_MODEL"
DEEPSEEK_ALLOWED_MODEL = "deepseek-v4-flash"
DEEPSEEK_PROXY_BUDGET_KEYS: frozenset[str] = frozenset(
    {
        DEEPSEEK_PROXY_CAPABILITY_ENV,
        DEEPSEEK_ALLOWED_MODEL_ENV,
        "EVALLAB_DEEPSEEK_MAX_REQUESTS",
        "EVALLAB_DEEPSEEK_MAX_INPUT_TOKENS",
        "EVALLAB_DEEPSEEK_MAX_OUTPUT_TOKENS",
        "EVALLAB_DEEPSEEK_MAX_COST_MICROS",
        "EVALLAB_DEEPSEEK_CAPABILITY_EXPIRES_AT",
        "EVALLAB_DEEPSEEK_INPUT_COST_MICROS_PER_MILLION",
        "EVALLAB_DEEPSEEK_OUTPUT_COST_MICROS_PER_MILLION",
    }
)
REDACTED_SECRET_VALUE = "<redacted>"
REDACTED_SECRET_BYTES = REDACTED_SECRET_VALUE.encode()
PRIVATE_PERSIST_MODE = 0o600
_BEARER_HEADER = re.compile(
    rb"(?i)(authorization\s*[:=]\s*bearer\s+)\S+",
)

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


def _fstat_owner_secret(fd: int, *, allowed_modes: frozenset[int]) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise OSError("secret path is not a regular file")
    if info.st_uid != os.geteuid():
        raise OSError("secret file owner mismatch")
    if (info.st_mode & 0o777) not in allowed_modes:
        raise OSError("secret file mode is not owner-only")
    return info


def read_owner_secret_file(path: Path) -> str:
    """Read a regular, owner-only secret file without following symlinks."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags)
    try:
        _fstat_owner_secret(fd, allowed_modes=frozenset({0o400, 0o600}))
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8").rstrip("\r\n")


def collected_secret_values(
    environment: Mapping[str, str] | None = None,
) -> frozenset[str]:
    """Return non-placeholder provider secret strings present in *environment*."""
    source = os.environ if environment is None else environment
    values: set[str] = set()
    for key in DEEPSEEK_CREDENTIAL_ENVIRONMENT_KEYS:
        value = source.get(key)
        if value and value != DEEPSEEK_PROXY_TOKEN:
            values.add(value)
    secret_file = source.get(DEEPSEEK_SECRET_FILE_ENV)
    if secret_file:
        try:
            file_value = read_owner_secret_file(Path(secret_file))
        except OSError:
            file_value = ""
        if file_value and file_value != DEEPSEEK_PROXY_TOKEN:
            values.add(file_value)
    capability = source.get(DEEPSEEK_PROXY_CAPABILITY_ENV)
    if capability and capability != DEEPSEEK_PROXY_TOKEN:
        values.add(capability)
    return frozenset(values)


def redact_secret_material(data: bytes, secrets: tuple[bytes, ...] = ()) -> bytes:
    """Redact known secrets and bearer tokens before any disk write."""
    redacted = data
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, REDACTED_SECRET_BYTES)
    return _BEARER_HEADER.sub(rb"\1" + REDACTED_SECRET_BYTES, redacted)


def persist_private_bytes(
    path: Path,
    data: bytes,
    *,
    secrets: tuple[bytes, ...] = (),
    mode: int = PRIVATE_PERSIST_MODE,
) -> None:
    """Write *data* only after redaction, then restrict the file mode."""
    sanitized = redact_secret_material(data, secrets)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    else:
        if stat.S_ISLNK(existing.st_mode):
            raise OSError("refusing to write through a symlink")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(temporary, flags, 0o600)
    try:
        os.fchmod(fd, mode)
        _fstat_owner_secret(fd, allowed_modes=frozenset({mode}))
        view = memoryview(sanitized)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except Exception:
        os.close(fd)
        with suppress(OSError):
            os.unlink(temporary)
        raise
    os.close(fd)
    os.replace(temporary, path)
    verify = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fchmod(verify, mode)
        _fstat_owner_secret(verify, allowed_modes=frozenset({mode}))
    finally:
        os.close(verify)


_BEARER_HOLDBACK = len(b"authorization: bearer ") + 64
_MAX_HOLDBACK = 8192


class RedactingBinaryWriter:
    """File-like stdout sink that redacts secrets across chunk boundaries."""

    def __init__(self, path: Path, secrets: tuple[bytes, ...]) -> None:
        self.path = path
        self._secrets = secrets
        longest = max((len(secret) for secret in secrets if secret), default=1)
        self._holdback = min(_MAX_HOLDBACK, max(longest, _BEARER_HOLDBACK))
        self._pending = b""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("wb")
        os.chmod(path, PRIVATE_PERSIST_MODE)

    def _flush_window(self, *, finalize: bool) -> None:
        sanitized = redact_secret_material(self._pending, self._secrets)
        if finalize or len(sanitized) <= self._holdback:
            if finalize and sanitized:
                self._handle.write(sanitized)
                self._handle.flush()
                sanitized = b""
            self._pending = sanitized
            return
        emit, self._pending = sanitized[: -self._holdback], sanitized[-self._holdback :]
        self._handle.write(emit)
        self._handle.flush()

    def write(self, data: bytes) -> int:
        self._pending += data
        self._flush_window(finalize=False)
        return len(data)

    def flush(self) -> None:
        self._handle.flush()

    def close(self) -> None:
        self._flush_window(finalize=True)
        self._handle.close()
        with suppress(OSError):
            os.chmod(self.path, PRIVATE_PERSIST_MODE)

    def __enter__(self) -> RedactingBinaryWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def materialize_deepseek_secret_file(
    destination: Path,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Write the provider key to a 0600 file for Compose secret mounting."""
    source = os.environ if environment is None else environment
    value = source.get("DEEPSEEK_API_KEY") or source.get("MSWEA_API_KEY")
    if value == DEEPSEEK_PROXY_TOKEN:
        value = None
    if not value:
        existing = source.get(DEEPSEEK_SECRET_FILE_ENV)
        if existing:
            path = Path(existing)
            try:
                read_owner_secret_file(path)
            except OSError as exc:
                raise RuntimeError("DeepSeek provider credential is missing") from exc
            return path
        raise RuntimeError("DeepSeek provider credential is missing")
    persist_private_bytes(destination, f"{value}\n".encode(), secrets=(), mode=0o400)
    return destination


def subscription_environment(
    environment: Mapping[str, str] | None = None,
    *,
    include_deepseek_credentials: bool = False,
) -> dict[str, str]:
    """Build Harbor's environment from explicit non-secret allowlists.

    DeepSeek provider keys never enter this mapping. The mini-swe-agent lane
    receives only the internal proxy script path and a file-mounted secret path.
    """
    source = os.environ if environment is None else environment
    sanitized = {key: source[key] for key in _SUBSCRIPTION_ENVIRONMENT_KEYS if key in source}
    if include_deepseek_credentials:
        for key in (
            DEEPSEEK_SECRET_FILE_ENV,
            DEEPSEEK_PROXY_SCRIPT_ENV,
            DEEPSEEK_UPSTREAM_ENV,
            *DEEPSEEK_PROXY_BUDGET_KEYS,
        ):
            if source.get(key):
                sanitized[key] = source[key]
        capability = source.get(DEEPSEEK_PROXY_CAPABILITY_ENV) or DEEPSEEK_PROXY_TOKEN
        sanitized[DEEPSEEK_PROXY_CAPABILITY_ENV] = capability
        sanitized["DEEPSEEK_API_KEY"] = capability
        sanitized["MSWEA_API_KEY"] = capability
        sanitized["DEEPSEEK_BASE_URL"] = DEEPSEEK_PROXY_URL
        sanitized["OPENAI_BASE_URL"] = DEEPSEEK_PROXY_URL
        sanitized["OPENAI_API_BASE"] = DEEPSEEK_PROXY_URL
    sanitized["AGY_FORCE_AUTH_JSON"] = "1"
    sanitized["CODEX_FORCE_AUTH_JSON"] = "1"
    sanitized["CLAUDE_FORCE_OAUTH"] = "1"
    sanitized["REWARDKIT_FORCE_OAUTH"] = "1"
    return sanitized


def redact_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Return a log-safe copy with every admitted DeepSeek value replaced."""
    secrets = collected_secret_values(environment)
    redacted: dict[str, str] = {}
    for key, value in environment.items():
        if (key in DEEPSEEK_CREDENTIAL_ENVIRONMENT_KEYS and value) or value in secrets:
            redacted[key] = REDACTED_SECRET_VALUE
        else:
            redacted[key] = value
    return redacted


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
        proxy = (repo_root / DEEPSEEK_PROXY_SCRIPT).resolve()
        if not overlay.is_file():
            raise RuntimeError(f"DeepSeek secret overlay is missing: {overlay}")
        if not proxy.is_file():
            raise RuntimeError(f"DeepSeek secret proxy is missing: {proxy}")
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
