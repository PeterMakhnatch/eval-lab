"""Immutable execution contracts, DTOs, and validation for runner and queue subsystems.

Key invariants:
- Immutable dataclasses for run requests, dispatch capacities, process outcomes, and authorizations.
- Non-secret environment allowlisting plus narrowly scoped, redacted credential routing.
- Strict request parameter validation (job names, timeouts, concurrency, billable approval).
- Standard Harbor command construction and transient exception classification.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tomllib
import urllib.parse
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from evallab.schemas import (
    ExperimentSpec,
    RunProvenance,
    StandingApprovalsPolicy,
)

CONTROL_AGENTS = frozenset({"oracle", "nop"})
_TASK_COMPOSE_FILENAMES = ("docker-compose.yaml", "docker-compose.yml")
SAFE_JOB_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
# Lease generations are immutable, 32-lowercase-hex identifiers produced by
# secrets.token_hex(16). Every durable-record reader that later turns a stored
# generation into a filesystem path MUST reject anything outside this contract,
# so a tampered lease/cancel record cannot inject path separators or arbitrary
# suffixes into a cancel-marker filename.
LEASE_GENERATION_PATTERN = re.compile(r"^[0-9a-f]{32}$")
DEFAULT_TRIAL_TIMEOUT_SECONDS = 1_800
MAX_TRIAL_TIMEOUT_SECONDS = 28_800
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
        "DAYTONA_API_URL",
        "DAYTONA_TARGET",
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

DAYTONA_CREDENTIAL_ENVIRONMENT_KEYS = frozenset({"DAYTONA_API_KEY"})

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
DEEPSEEK_PROXY_UID_ENV = "EVALLAB_PROXY_UID"
DEEPSEEK_PROXY_GID_ENV = "EVALLAB_PROXY_GID"
DEEPSEEK_PROXY_CAPABILITY_ENV = "EVALLAB_DEEPSEEK_PROXY_CAPABILITY"
DEEPSEEK_PROXY_USAGE_DIR_ENV = "EVALLAB_DEEPSEEK_USAGE_DIR"
DEEPSEEK_PROXY_ATTEMPT_ID_ENV = "EVALLAB_DEEPSEEK_ATTEMPT_ID"
DEEPSEEK_PROXY_USAGE_FILE_ENV = "EVALLAB_DEEPSEEK_USAGE_FILE"
DEEPSEEK_ALLOWED_MODEL_ENV = "EVALLAB_DEEPSEEK_ALLOWED_MODEL"
DEEPSEEK_ALLOWED_MODEL = "deepseek-flash"
# DeepSeek V4.1 API tiers map to the trained integer effort control (1–100).
# Mirrored in containers/deepseek_secret_proxy.py for its stdlib-only image.
DEEPSEEK_REASONING_EFFORT_TIERS: Mapping[str, int] = MappingProxyType(
    {"low": 50, "high": 75, "max": 100}
)
DEEPSEEK_PROXY_BUDGET_KEYS: frozenset[str] = frozenset(
    {
        DEEPSEEK_PROXY_CAPABILITY_ENV,
        DEEPSEEK_ALLOWED_MODEL_ENV,
        DEEPSEEK_PROXY_ATTEMPT_ID_ENV,
        DEEPSEEK_PROXY_USAGE_DIR_ENV,
        DEEPSEEK_PROXY_USAGE_FILE_ENV,
        "EVALLAB_DEEPSEEK_MAX_REQUESTS",
        "EVALLAB_DEEPSEEK_MAX_INPUT_TOKENS",
        "EVALLAB_DEEPSEEK_MAX_OUTPUT_TOKENS",
        "EVALLAB_DEEPSEEK_MAX_COST_MICROS",
        "EVALLAB_DEEPSEEK_MAX_TOTAL_TOKENS",
        "EVALLAB_DEEPSEEK_CAPABILITY_EXPIRES_AT",
        "EVALLAB_DEEPSEEK_INPUT_COST_MICROS_PER_MILLION",
        "EVALLAB_DEEPSEEK_OUTPUT_COST_MICROS_PER_MILLION",
        DEEPSEEK_PROXY_UID_ENV,
        DEEPSEEK_PROXY_GID_ENV,
    }
)
RLM_AGENT = "rlm"
TERMINUS_AGENT = "terminus-2"
TERMINUS_AGENT_IMPORT_PATH = "evallab.harbor_terminus:SecretSafeTerminus2"
TERMINUS_PROXY_URL_ENV = "EVALLAB_TERMINUS_PROXY_URL"
TERMINUS_LOCAL_MODEL_SELECTOR = "ollama_chat/qwen2.5:7b"
TERMINUS_LOCAL_ENDPOINT_ENV = "EVALLAB_TERMINUS_OLLAMA_URL"
REEF_SCENARIO_ENV = "EVALLAB_REEF_SCENARIO"
BOUNDED_DAYTONA_ENVIRONMENT_IMPORT_PATH = "evallab.harbor_daytona:BoundedDaytonaEnvironment"
ZAI_OPENCODE_AGENT = "zai-opencode"
ZAI_OPENCODE_MODEL_SELECTORS: frozenset[str] = frozenset(
    {"zai-coding-plan/glm-5.3", "zai-coding-plan/glm-5.3-flash"}
)
ZAI_AUTH_PROVIDER = "zai-coding-plan"
OPENCODE_AUTH_RELATIVE_PATH = Path(".local/share/opencode/auth.json")
ZAI_CREDENTIAL_ENVIRONMENT_KEYS: frozenset[str] = frozenset(
    {
        "ZAI_CODING_PLAN_API_KEY",
        "ZAI_API_KEY",
        "ZAI_CODING_PLAN_KEY",
        "ZAI_KEY",
    }
)
ZAI_PROXY_HOST = "zai-secret-proxy"
ZAI_PROXY_URL = "http://zai-secret-proxy:8080/api/paas/v4"
ZAI_PROXY_TOKEN = "evallab-proxy-placeholder"
ZAI_SECRET_COMPOSE = Path("containers/zai-secret.compose.yaml")
ZAI_PROXY_SCRIPT = Path("containers/zai_secret_proxy.py")
ZAI_SECRET_FILE_ENV = "EVALLAB_ZAI_SECRET_FILE"
ZAI_SECRET_PATH_ENV = "EVALLAB_ZAI_SECRET_PATH"
ZAI_PROXY_SCRIPT_ENV = "EVALLAB_ZAI_PROXY_SCRIPT"
ZAI_PROXY_UID_ENV = "EVALLAB_PROXY_UID"
ZAI_PROXY_GID_ENV = "EVALLAB_PROXY_GID"
ZAI_PROXY_CAPABILITY_ENV = "EVALLAB_ZAI_PROXY_CAPABILITY"
ZAI_CAPABILITY_EXPIRES_AT_ENV = "EVALLAB_ZAI_CAPABILITY_EXPIRES_AT"
ZAI_UPSTREAM_ENV = "EVALLAB_ZAI_UPSTREAM"
ZAI_PROXY_USAGE_DIR_ENV = "EVALLAB_ZAI_USAGE_DIR"
ZAI_PROXY_ATTEMPT_ID_ENV = "EVALLAB_ZAI_ATTEMPT_ID"
ZAI_PROXY_USAGE_FILE_ENV = "EVALLAB_ZAI_USAGE_FILE"
ZAI_INPUT_COST_MICROS_PER_MILLION = 1_400_000
ZAI_OUTPUT_COST_MICROS_PER_MILLION = 4_400_000
ZAI_PROXY_BUDGET_KEYS: frozenset[str] = frozenset(
    {
        ZAI_PROXY_CAPABILITY_ENV,
        ZAI_PROXY_ATTEMPT_ID_ENV,
        ZAI_PROXY_USAGE_DIR_ENV,
        ZAI_PROXY_USAGE_FILE_ENV,
        "EVALLAB_ZAI_MAX_REQUESTS",
        "EVALLAB_ZAI_MAX_INPUT_TOKENS",
        "EVALLAB_ZAI_MAX_OUTPUT_TOKENS",
        "EVALLAB_ZAI_MAX_TOTAL_TOKENS",
        "EVALLAB_ZAI_MAX_COST_MICROS",
        "EVALLAB_ZAI_INPUT_COST_MICROS_PER_MILLION",
        "EVALLAB_ZAI_OUTPUT_COST_MICROS_PER_MILLION",
        ZAI_CAPABILITY_EXPIRES_AT_ENV,
        ZAI_PROXY_UID_ENV,
        ZAI_PROXY_GID_ENV,
    }
)
ZAI_OPENAPI_MODEL_SELECTOR = "zai/glm-5.3-flash"
ZAI_OPENAPI_ALLOWED_MODELS: frozenset[str] = frozenset({ZAI_OPENAPI_MODEL_SELECTOR})
ZAI_OPENAPI_ALLOWED_MODEL = "glm-5.3-flash"
ZAI_OPENAPI_PROXY_HOST = "zai-openapi-secret-proxy"
ZAI_OPENAPI_PROXY_URL = "http://zai-openapi-secret-proxy:8080"
ZAI_OPENAPI_PROXY_TOKEN = "evallab-proxy-placeholder"
ZAI_OPENAPI_SECRET_COMPOSE = Path("containers/zai-openapi-secret.compose.yaml")
ZAI_OPENAPI_PROXY_SCRIPT = Path("containers/zai_openapi_secret_proxy.py")
ZAI_OPENAPI_CREDENTIAL_ENVIRONMENT_KEYS: frozenset[str] = frozenset({"ZAI_OPENAPI_API_KEY"})
ZAI_OPENAPI_SECRET_FILE_ENV = "EVALLAB_ZAI_OPENAPI_SECRET_FILE"
ZAI_OPENAPI_SECRET_PATH_ENV = "EVALLAB_ZAI_OPENAPI_SECRET_PATH"
ZAI_OPENAPI_PROXY_SCRIPT_ENV = "EVALLAB_ZAI_OPENAPI_PROXY_SCRIPT"
ZAI_OPENAPI_PROXY_UID_ENV = "EVALLAB_PROXY_UID"
ZAI_OPENAPI_PROXY_GID_ENV = "EVALLAB_PROXY_GID"
ZAI_OPENAPI_PROXY_CAPABILITY_ENV = "EVALLAB_ZAI_OPENAPI_PROXY_CAPABILITY"
ZAI_OPENAPI_CAPABILITY_EXPIRES_AT_ENV = "EVALLAB_ZAI_OPENAPI_CAPABILITY_EXPIRES_AT"
ZAI_OPENAPI_UPSTREAM_ENV = "EVALLAB_ZAI_OPENAPI_UPSTREAM"
ZAI_OPENAPI_PROXY_USAGE_DIR_ENV = "EVALLAB_ZAI_OPENAPI_USAGE_DIR"
ZAI_OPENAPI_PROXY_ATTEMPT_ID_ENV = "EVALLAB_ZAI_OPENAPI_ATTEMPT_ID"
ZAI_OPENAPI_PROXY_USAGE_FILE_ENV = "EVALLAB_ZAI_OPENAPI_USAGE_FILE"
ZAI_OPENAPI_ALLOWED_MODEL_ENV = "EVALLAB_ZAI_OPENAPI_ALLOWED_MODEL"
ZAI_OPENAPI_INPUT_COST_MICROS_PER_MILLION = 150_000
ZAI_OPENAPI_OUTPUT_COST_MICROS_PER_MILLION = 500_000
ZAI_OPENAPI_PROXY_BUDGET_KEYS: frozenset[str] = frozenset(
    {
        ZAI_OPENAPI_PROXY_CAPABILITY_ENV,
        ZAI_OPENAPI_PROXY_ATTEMPT_ID_ENV,
        ZAI_OPENAPI_PROXY_USAGE_DIR_ENV,
        ZAI_OPENAPI_PROXY_USAGE_FILE_ENV,
        ZAI_OPENAPI_ALLOWED_MODEL_ENV,
        "EVALLAB_ZAI_OPENAPI_MAX_REQUESTS",
        "EVALLAB_ZAI_OPENAPI_MAX_INPUT_TOKENS",
        "EVALLAB_ZAI_OPENAPI_MAX_OUTPUT_TOKENS",
        "EVALLAB_ZAI_OPENAPI_MAX_TOTAL_TOKENS",
        "EVALLAB_ZAI_OPENAPI_MAX_COST_MICROS",
        "EVALLAB_ZAI_OPENAPI_INPUT_COST_MICROS_PER_MILLION",
        "EVALLAB_ZAI_OPENAPI_OUTPUT_COST_MICROS_PER_MILLION",
        ZAI_OPENAPI_CAPABILITY_EXPIRES_AT_ENV,
        ZAI_OPENAPI_PROXY_UID_ENV,
        ZAI_OPENAPI_PROXY_GID_ENV,
    }
)
GLM_SELFHOSTED_BASE_MODEL_SELECTOR = "glm-selfhosted/glm-5.3-flash"
GLM_SELFHOSTED_FT_MODEL_SELECTOR = "glm-ft/glm-5.3-flash-ft"
GLM_SELFHOSTED_ALLOWED_PROVIDERS: frozenset[str] = frozenset({"glm-selfhosted", "glm-ft"})
GLM_SELFHOSTED_CREDENTIAL_ENVIRONMENT_KEYS: frozenset[str] = frozenset({"GLM_SELFHOSTED_API_KEY"})
GLM_SELFHOSTED_BASE_URL_ENV = "GLM_SELFHOSTED_BASE_URL"
GLM_SELFHOSTED_PROXY_TOKEN = "evallab-proxy-placeholder"
GLM_SELFHOSTED_MINISWE_AGENT_IMPORT_PATH = (
    "evallab.harbor_glm_selfhosted:SecretSafeGlmSelfhostedMiniSweAgent"
)


class ProfileInferenceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    effort: str | int | None = None
    max_tokens: int | None = None


InferenceSettings = ProfileInferenceSettings
ZAI_MINISWE_AGENT_IMPORT_PATH = "evallab.harbor_zai_miniswe:SecretSafeZaiMiniSweAgent"
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
    "zai-opencode": "evallab.harbor_zai_opencode:SecretSafeZaiOpenCodeAgent",
    RLM_AGENT: "evallab.harbor_rlm:LabRlmAgent",
    TERMINUS_AGENT: TERMINUS_AGENT_IMPORT_PATH,
}

DEEPSEEK_MODEL_SELECTOR = "deepseek/deepseek-flash"
DEEPSEEK_SECRET_COMPOSE = Path("containers/deepseek-v4-flash-secret.compose.yaml")

HARBOR_STATE_JOURNAL_PLUGIN = "evallab.harbor_state_journal:StateJournalPlugin"

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class ExecutionFailure(RuntimeError):
    """Base error for trial execution failures."""

    reason_code = "execution_failed"

    def __init__(
        self,
        reason_code_or_message: str,
        message: str | None = None,
    ) -> None:
        if message is None:
            super().__init__(reason_code_or_message)
            return
        self.reason_code = reason_code_or_message
        super().__init__(message)


class TrialTimeoutFailure(ExecutionFailure):
    """Raised when trial exceeds allowed wall-clock timeout."""

    reason_code = "trial_wall_clock_timeout"

    #: Identity of the specific trial that exceeded its per-trial allowance,
    #: when the aggregate process outlived per-trial limits (else None).
    timed_out_trial: str | None = None


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
    experiment_spec: ExperimentSpec | None = None
    lease_path: Path | None = None
    lease_generation: str | None = None
    extra_instruction_path: Path | None = None
    toolbox_path: Path | None = None
    toolbox_sha256: str | None = None
    harness_tree_path: Path | None = None
    harness_tree_sha256: str | None = None
    skill: Path | str | Sequence[Path | str] | None = None
    skills: Sequence[Path | str] | None = None
    load_trajectory: Path | str | None = None
    export_traces: bool = False
    max_requests: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    cost_limit_usd: float | None = None
    harness_policy: str | None = None
    requested_selector: str | None = None
    effective_endpoint_base: str | None = None
    provider_returned_model_id: str | None = None
    inference_settings: ProfileInferenceSettings | None = None
    reef: ReefTrafficBinding | None = None

    @property
    def job_timeout_seconds(self) -> int:
        """Conservative process deadline: one wall-clock allowance per attempt."""
        return self.timeout_seconds * self.attempts

    @property
    def resolved_skills(self) -> tuple[str, ...]:
        """Deterministic sequence of skill paths forwarded to the execution command."""
        result: list[str] = []
        if self.skill is not None:
            if isinstance(self.skill, (str, Path)):
                result.append(str(self.skill))
            else:
                result.extend(str(s) for s in self.skill)
        if self.skills is not None:
            if isinstance(self.skills, (str, Path)):
                result.append(str(self.skills))
            else:
                result.extend(str(s) for s in self.skills)
        return tuple(result)

@dataclass(frozen=True)
class ReefTrafficBinding:
    """Pinned Reef capture/report binding for one terminus-2 execution.

    Mirrors ``ReefTrafficSpec``: the service URL, the scenario the records
    belong to, the environment variable holding the bearer token (never the
    token value), and the release/content ids pinned at prepare time.
    """

    url: str
    scenario: str
    token_env: str
    release_id: str
    content_id: str


@dataclass(frozen=True)
class HarborProcessResult:
    """Outcome of running a Harbor subprocess under watchdog supervision."""

    returncode: int
    timed_out: bool
    log_path: Path
    timed_out_trial: str | None = None
    proxy_usage: dict[str, Any] | None = None
    reef_turns: tuple[dict[str, Any], ...] | None = None


@dataclass(frozen=True)
class ProxyTrialLimits:
    """Explicit ceilings bound to one provider capability."""

    max_requests: int
    max_input_tokens: int
    max_output_tokens: int
    max_total_tokens: int
    max_cost_micros: int

    def __post_init__(self) -> None:
        if (
            min(
                self.max_requests,
                self.max_input_tokens,
                self.max_output_tokens,
                self.max_total_tokens,
                self.max_cost_micros,
            )
            < 1
        ):
            raise ValueError("proxy trial ceilings must be positive")
        if self.max_total_tokens > self.max_input_tokens + self.max_output_tokens:
            raise ValueError("proxy total-token ceiling exceeds component ceilings")


@dataclass(frozen=True)
class PaidRunAuthorization:
    """One recorded human decision to let a specific queued spec spend money."""

    spec_id: str
    actor: str
    authorized_at: datetime
    quota_override: bool = False
    approved_spec_digest: str | None = None
    campaign_manifest_digest: str | None = None
    campaign_spec_digest: str | None = None


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
    if info.st_uid not in {0, os.geteuid()}:
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


def opencode_auth_path(
    *,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Return OpenCode's host auth store without reading credential material."""
    source = os.environ if environment is None else environment
    if xdg_data_home := source.get("XDG_DATA_HOME"):
        return Path(xdg_data_home).expanduser() / "opencode/auth.json"
    return (home or Path.home()) / OPENCODE_AUTH_RELATIVE_PATH


def read_zai_opencode_key(path: Path) -> str:
    """Read the Z.ai key from an owner-only OpenCode auth store without exposing it."""
    try:
        payload = json.loads(read_owner_secret_file(path))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("OpenCode Z.ai credential is unavailable") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("OpenCode Z.ai credential is unavailable")
    entry = payload.get(ZAI_AUTH_PROVIDER)
    if not isinstance(entry, dict):
        raise RuntimeError("OpenCode Z.ai credential is unavailable")
    value = entry.get("key")
    if not isinstance(value, str) or not value:
        raise RuntimeError("OpenCode Z.ai credential is unavailable")
    return value


def materialize_zai_secret_file(
    destination: Path,
    *,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Write only the Z.ai provider key from OpenCode's auth store to a 0400 file."""
    source_path = opencode_auth_path(home=home, environment=environment)
    value = read_zai_opencode_key(source_path)
    persist_private_bytes(destination, f"{value}\n".encode(), secrets=(), mode=0o400)
    return destination


def materialize_zai_openapi_secret_file(
    destination: Path,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Write the Z.ai OpenAPI provider key to a 0400 file for Compose secret mounting."""
    source = os.environ if environment is None else environment
    value = source.get("ZAI_OPENAPI_API_KEY")
    if value == ZAI_OPENAPI_PROXY_TOKEN:
        value = None
    if not value:
        existing = source.get(ZAI_OPENAPI_SECRET_FILE_ENV)
        if existing:
            path = Path(existing)
            try:
                read_owner_secret_file(path)
            except OSError as exc:
                raise RuntimeError("Z.ai OpenAPI provider credential is missing") from exc
            return path
        raise RuntimeError("Z.ai OpenAPI provider credential is missing")
    persist_private_bytes(destination, f"{value}\n".encode(), secrets=(), mode=0o400)
    return destination


def proxy_runtime_identity(path: Path) -> tuple[int, int]:
    """Return the numeric uid/gid the proxy must run as to read *path*.

    The file must be a regular, owner-only secret owned by root or the
    invoking euid. Compose must use these numbers as ``user:``, not secret
    uid/gid remap metadata.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags)
    try:
        info = _fstat_owner_secret(fd, allowed_modes=frozenset({0o400, 0o600}))
        return info.st_uid, info.st_gid
    finally:
        os.close(fd)


def collected_secret_values(
    environment: Mapping[str, str] | None = None,
) -> frozenset[str]:
    """Return non-placeholder provider secret strings present in *environment*."""
    source = os.environ if environment is None else environment
    values: set[str] = set()
    for key, placeholder in (
        *((key, "") for key in DAYTONA_CREDENTIAL_ENVIRONMENT_KEYS),
        *((key, DEEPSEEK_PROXY_TOKEN) for key in DEEPSEEK_CREDENTIAL_ENVIRONMENT_KEYS),
        *((key, ZAI_PROXY_TOKEN) for key in ZAI_CREDENTIAL_ENVIRONMENT_KEYS),
        *((key, ZAI_OPENAPI_PROXY_TOKEN) for key in ZAI_OPENAPI_CREDENTIAL_ENVIRONMENT_KEYS),
        *((key, GLM_SELFHOSTED_PROXY_TOKEN) for key in GLM_SELFHOSTED_CREDENTIAL_ENVIRONMENT_KEYS),
    ):
        value = source.get(key)
        if value and value != placeholder:
            values.add(value)
    for secret_file_env, placeholder in (
        (DEEPSEEK_SECRET_FILE_ENV, DEEPSEEK_PROXY_TOKEN),
        (ZAI_SECRET_FILE_ENV, ZAI_PROXY_TOKEN),
        (ZAI_OPENAPI_SECRET_FILE_ENV, ZAI_OPENAPI_PROXY_TOKEN),
    ):
        secret_file = source.get(secret_file_env)
        if secret_file:
            try:
                file_value = read_owner_secret_file(Path(secret_file))
            except OSError:
                file_value = ""
            if file_value and file_value != placeholder:
                values.add(file_value)
    for capability_env, placeholder in (
        (DEEPSEEK_PROXY_CAPABILITY_ENV, DEEPSEEK_PROXY_TOKEN),
        (ZAI_PROXY_CAPABILITY_ENV, ZAI_PROXY_TOKEN),
        (ZAI_OPENAPI_PROXY_CAPABILITY_ENV, ZAI_OPENAPI_PROXY_TOKEN),
    ):
        capability = source.get(capability_env)
        if capability and capability != placeholder:
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
        self._holdback = min(_MAX_HOLDBACK, max(longest * 2, _BEARER_HOLDBACK))
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
    include_zai_credentials: bool = False,
    include_zai_openapi_credentials: bool = False,
    include_glm_selfhosted_credentials: bool = False,
    include_daytona_credentials: bool = False,
) -> dict[str, str]:
    """Build Harbor's environment from explicit non-secret allowlists.
    DeepSeek and Z.ai provider keys never enter this mapping. The metered agent
    lanes receive only the internal proxy script path and a file-mounted secret path.
    """
    source = os.environ if environment is None else environment
    sanitized = {key: source[key] for key in _SUBSCRIPTION_ENVIRONMENT_KEYS if key in source}
    if include_daytona_credentials:
        for key in DAYTONA_CREDENTIAL_ENVIRONMENT_KEYS:
            if source.get(key):
                sanitized[key] = source[key]
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
    if include_zai_credentials:
        for key in (
            ZAI_SECRET_FILE_ENV,
            ZAI_PROXY_SCRIPT_ENV,
            ZAI_UPSTREAM_ENV,
            *ZAI_PROXY_BUDGET_KEYS,
        ):
            if source.get(key):
                sanitized[key] = source[key]
        capability = source.get(ZAI_PROXY_CAPABILITY_ENV) or ZAI_PROXY_TOKEN
        sanitized[ZAI_PROXY_CAPABILITY_ENV] = capability
        sanitized["ZAI_CODING_PLAN_API_KEY"] = capability
        sanitized["ZAI_API_KEY"] = capability
        sanitized["ZAI_BASE_URL"] = ZAI_PROXY_URL
        sanitized["OPENAI_BASE_URL"] = ZAI_PROXY_URL
        sanitized["OPENAI_API_BASE"] = ZAI_PROXY_URL
    if include_zai_openapi_credentials:
        for key in (
            ZAI_OPENAPI_SECRET_FILE_ENV,
            ZAI_OPENAPI_PROXY_SCRIPT_ENV,
            ZAI_OPENAPI_UPSTREAM_ENV,
            *ZAI_OPENAPI_PROXY_BUDGET_KEYS,
        ):
            if source.get(key):
                sanitized[key] = source[key]
        capability = source.get(ZAI_OPENAPI_PROXY_CAPABILITY_ENV) or ZAI_OPENAPI_PROXY_TOKEN
        sanitized[ZAI_OPENAPI_PROXY_CAPABILITY_ENV] = capability
        sanitized["ZAI_OPENAPI_API_KEY"] = capability
        sanitized["MSWEA_API_KEY"] = capability
        sanitized["OPENAI_BASE_URL"] = ZAI_OPENAPI_PROXY_URL
        sanitized["OPENAI_API_BASE"] = ZAI_OPENAPI_PROXY_URL
    if include_glm_selfhosted_credentials:
        base_url = source.get(GLM_SELFHOSTED_BASE_URL_ENV)
        if not base_url:
            raise ValueError(f"{GLM_SELFHOSTED_BASE_URL_ENV} environment variable is not set")
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                f"{GLM_SELFHOSTED_BASE_URL_ENV} must be a valid http or https URL, got {base_url!r}"
            )
        sanitized[GLM_SELFHOSTED_BASE_URL_ENV] = base_url
        sanitized["MSWEA_API_KEY"] = GLM_SELFHOSTED_PROXY_TOKEN
        sanitized["OPENAI_BASE_URL"] = base_url
        sanitized["OPENAI_API_BASE"] = base_url
    sanitized["AGY_FORCE_AUTH_JSON"] = "1"
    sanitized["CODEX_FORCE_AUTH_JSON"] = "1"
    sanitized["CLAUDE_FORCE_OAUTH"] = "1"
    sanitized["REWARDKIT_FORCE_OAUTH"] = "1"
    return sanitized


def redact_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Return a log-safe copy with every admitted provider value replaced."""
    secrets = collected_secret_values(environment)
    credential_keys = (
        DEEPSEEK_CREDENTIAL_ENVIRONMENT_KEYS
        | ZAI_CREDENTIAL_ENVIRONMENT_KEYS
        | ZAI_OPENAPI_CREDENTIAL_ENVIRONMENT_KEYS
        | GLM_SELFHOSTED_CREDENTIAL_ENVIRONMENT_KEYS
        | DAYTONA_CREDENTIAL_ENVIRONMENT_KEYS
    )
    redacted: dict[str, str] = {}
    for key, value in environment.items():
        if (key in credential_keys and value) or value in secrets:
            redacted[key] = REDACTED_SECRET_VALUE
        else:
            redacted[key] = value
    return redacted


def _task_has_provided_compose(task: Path) -> bool:
    """Return True when the source task package ships its own Compose file."""
    environment_dir = task / "environment"
    return any((environment_dir / name).is_file() for name in _TASK_COMPOSE_FILENAMES)


def _task_gpu_request(task: Path) -> int | None:
    """Read GPU requirements before attempting local Docker execution."""
    environment = tomllib.loads((task / "task.toml").read_text()).get("environment", {})
    if not isinstance(environment, dict):
        raise ValueError("task.toml environment must be a table")
    gpus = environment.get("gpus")
    return gpus if isinstance(gpus, int) and not isinstance(gpus, bool) else None


def uses_provider_proxy(agent: str, model: str | None) -> bool:
    """Whether this model route can enforce the provider request/token/cost ceilings."""
    return agent in {"mini-swe-agent", ZAI_OPENCODE_AGENT} or (
        agent == TERMINUS_AGENT and model != TERMINUS_LOCAL_MODEL_SELECTOR
    )


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
            f"Concurrency and attempts must be positive; timeout must be 1-{MAX_TRIAL_TIMEOUT_SECONDS} seconds"
        )
    proxy_limits = (
        request.max_requests,
        request.max_input_tokens,
        request.max_output_tokens,
        request.max_total_tokens,
        request.cost_limit_usd,
    )
    # The rlm lane forwards cost_limit_usd as a harness agent-kwarg rather than
    # enforcing it through the secret proxy, so it is not a proxy ceiling here.
    if request.agent == RLM_AGENT:
        proxy_limits = proxy_limits[:4]
    metered = uses_provider_proxy(request.agent, request.model)
    if any(value is not None for value in proxy_limits):
        if not metered:
            raise ValueError("this agent cannot enforce provider request/cost/token ceilings")
        if any(value is None for value in proxy_limits):
            raise ValueError(f"{request.agent} requires every provider ceiling")
        if (
            request.max_requests is None
            or request.max_input_tokens is None
            or request.max_output_tokens is None
            or request.max_total_tokens is None
            or request.cost_limit_usd is None
        ):
            raise AssertionError("validated provider ceilings unexpectedly absent")
        if (
            min(
                request.max_requests,
                request.max_input_tokens,
                request.max_output_tokens,
                request.max_total_tokens,
            )
            < 1
        ):
            raise ValueError("provider request and token ceilings must be positive")
        if request.cost_limit_usd <= 0:
            raise ValueError("cost_limit_usd must be positive")
        if request.max_total_tokens > request.max_input_tokens + request.max_output_tokens:
            raise ValueError("total-token ceiling exceeds input plus output ceilings")
    if metered:
        if any(value is None for value in proxy_limits):
            raise ValueError(f"{request.agent} requires explicit provider ceilings")
        if request.attempts != 1 or request.concurrency != 1:
            raise ValueError(f"{request.agent} capabilities bind exactly one trial")
    if request.agent == TERMINUS_AGENT:
        if request.model not in {ZAI_OPENAPI_MODEL_SELECTOR, TERMINUS_LOCAL_MODEL_SELECTOR}:
            raise ValueError(
                f"terminus-2 requires standard-API {ZAI_OPENAPI_MODEL_SELECTOR!r} "
                f"or installed local {TERMINUS_LOCAL_MODEL_SELECTOR!r}; "
                "Coding Plan credentials are not admitted for this harness"
            )
        if request.attempts != 1 or request.concurrency != 1:
            raise ValueError("terminus-2 specs bind exactly one trial")
    if request.harness_policy is not None and request.agent != RLM_AGENT:
        raise ValueError("harness_policy is supported only by the rlm lane")
    if request.agent == RLM_AGENT:
        if request.attempts != 1 or request.concurrency != 1:
            raise ValueError(f"{request.agent} capabilities bind exactly one trial")
        if request.model not in ZAI_OPENCODE_MODEL_SELECTORS:
            raise ValueError(
                f"rlm requires one of the exact models {sorted(ZAI_OPENCODE_MODEL_SELECTORS)}"
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
    # The proxy's host mounts need a backend-specific transport, not --env passthrough.
    if request.environment == "docker":
        gpus = _task_gpu_request(request.task)
        if gpus is not None and gpus >= 1:
            raise ValueError(
                f"Task requires {gpus} GPU(s) but Harbor Docker does not support GPU allocation; "
                "select a compatible remote task/harness/backend combination"
            )
    elif request.agent in {"mini-swe-agent", ZAI_OPENCODE_AGENT, RLM_AGENT}:
        zai_mini = request.agent == "mini-swe-agent" and request.model == ZAI_OPENAPI_MODEL_SELECTOR
        if request.environment == "daytona" and zai_mini:
            if _task_has_provided_compose(request.task):
                raise ValueError(
                    "GLM Daytona proxy transport requires a single-container task; "
                    "task-provided Compose is not supported. Use environment='docker' "
                    "for multi-container tasks"
                )
        else:
            hint = " or environment='daytona' for single-container GLM mini-SWE" if zai_mini else ""
            raise ValueError(
                f"environment={request.environment!r} is not integrated for {request.agent} "
                f"with model {request.model!r}: the provider credential transport requires "
                f"environment='docker'{hint}. This is a Lab integration gap, not a "
                "credential or credit issue"
            )
    if request.toolbox_path is not None or request.toolbox_sha256 is not None:
        if request.toolbox_path is None or request.toolbox_sha256 is None:
            raise ValueError("toolbox_path and toolbox_sha256 must be provided together")
        if request.agent not in {"oracle", "nop", ZAI_OPENCODE_AGENT}:
            raise ValueError(
                f"Agent {request.agent!r} does not support toolbox skills; "
                f"supported agents are 'oracle', 'nop', and {ZAI_OPENCODE_AGENT!r}"
            )
        if request.agent == ZAI_OPENCODE_AGENT:
            model = request.model or "zai-coding-plan/glm-5.3-flash"
            if model not in ZAI_OPENCODE_MODEL_SELECTORS:
                raise ValueError(
                    f"zai-opencode requires one of the exact models {sorted(ZAI_OPENCODE_MODEL_SELECTORS)}"
                )
        if request.environment != "docker":
            raise ValueError("toolbox execution requires environment='docker'")
        environment = tomllib.loads((request.task / "task.toml").read_text()).get("environment", {})
        if environment.get("skills_dir") not in {None, "/harbor/skills"}:
            raise ValueError("toolbox descriptor requires the native /harbor/skills directory")
        if request.resolved_skills:
            raise ValueError("toolbox artifact cannot be combined with other skill sources")
        from evallab.toolbox import validate_toolbox_source

        validate_toolbox_source(request.toolbox_path, request.toolbox_sha256)
    if request.harness_tree_path is not None or request.harness_tree_sha256 is not None:
        if request.harness_tree_path is None or request.harness_tree_sha256 is None:
            raise ValueError("harness_tree_path and harness_tree_sha256 must be provided together")
        if request.agent != TERMINUS_AGENT:
            raise ValueError("harness trees are supported only by terminus-2")
        from evallab.terminus_harness import load_harness_tree

        load_harness_tree(request.harness_tree_path, request.harness_tree_sha256)

    if request.reef is not None:
        if request.agent != TERMINUS_AGENT:
            raise ValueError("reef capture/reporting is supported only by terminus-2")
        if request.model != TERMINUS_LOCAL_MODEL_SELECTOR:
            raise ValueError("reef traffic runs on the local terminus route")
        if request.harness_tree_path is None or request.harness_tree_sha256 is None:
            raise ValueError("reef execution requires the pinned harness tree")
        for label, value in (
            ("url", request.reef.url),
            ("scenario", request.reef.scenario),
            ("token_env", request.reef.token_env),
            ("release_id", request.reef.release_id),
            ("content_id", request.reef.content_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"reef binding {label} must be a nonempty string")


def resolve_harbor_agent(agent: str, model: str | None = None) -> str:
    """Use the lab-owned adapter where Harbor supports custom import paths."""
    if agent == "mini-swe-agent" and model is not None:
        if model.startswith("zai/"):
            return ZAI_MINISWE_AGENT_IMPORT_PATH
        if model.startswith(("glm-selfhosted/", "glm-ft/")):
            return GLM_SELFHOSTED_MINISWE_AGENT_IMPORT_PATH
    return HARBOR_AGENT_IMPORT_PATHS.get(agent, agent)


def resolve_harbor_model(agent: str, model: str | None) -> str | None:
    """Translate a local CLI model identifier to Harbor's expected model string."""
    if model is None:
        return None
    return LOCAL_TO_HARBOR_MODEL.get((agent, model), model)


def _agent_timeout_multiplier(request: RunRequest) -> str | None:
    """Translate the absolute request timeout to Harbor's task-relative agent timeout."""
    try:
        document = tomllib.loads((request.task / "task.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("cannot read task agent timeout from task.toml") from exc
    agent = document.get("agent")
    timeout = agent.get("timeout_sec") if isinstance(agent, dict) else None
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        return None
    return format(request.timeout_seconds / float(timeout), ".12g")


def terminus_agent_kwargs(request: RunRequest) -> dict[str, Any]:
    """Render native behavior settings without admitting transport overrides."""
    completion_limit = (
        request.inference_settings.max_tokens
        if request.inference_settings and request.inference_settings.max_tokens is not None
        else 8192
    )
    kwargs: dict[str, Any] = {
        "llm_call_kwargs": {
            "max_tokens": min(completion_limit, request.max_output_tokens or completion_limit)
        }
    }
    if request.inference_settings and request.inference_settings.effort is not None:
        kwargs["reasoning_effort"] = request.inference_settings.effort
    if request.harness_tree_path is not None:
        from evallab.terminus_harness import load_harness_tree

        tree = load_harness_tree(request.harness_tree_path, request.harness_tree_sha256)
        for key, value in tree.config.items():
            if key == "llm_call_kwargs":
                kwargs[key] = {**kwargs[key], **value}
            else:
                kwargs[key] = value
    max_tokens = kwargs["llm_call_kwargs"].get("max_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise ValueError("Terminus max_tokens must be a positive integer")
    if request.max_output_tokens is not None and max_tokens > request.max_output_tokens:
        raise ValueError("Terminus per-response max_tokens exceeds the trial output-token ceiling")
    return kwargs


def build_command(request: RunRequest) -> list[str]:
    """Build the exact Harbor CLI invocation command for a RunRequest."""
    environment = request.environment
    zai_daytona = (
        environment == "daytona"
        and request.agent == "mini-swe-agent"
        and request.model == ZAI_OPENAPI_MODEL_SELECTOR
    )
    terminus_daytona = environment == "daytona" and request.agent == TERMINUS_AGENT
    if zai_daytona:
        environment = "evallab.harbor_daytona:SecretSafeDaytonaEnvironment"
    elif terminus_daytona:
        environment = BOUNDED_DAYTONA_ENVIRONMENT_IMPORT_PATH
    command = [
        "harbor",
        "run",
        "--path",
        str(request.task),
        "--agent",
        resolve_harbor_agent(request.agent, request.model),
        "--env",
        environment,
        "--job-name",
        request.name,
        "--jobs-dir",
        str(request.jobs_dir),
        "--n-concurrent",
        str(request.concurrency),
        "--n-attempts",
        str(request.attempts),
    ]
    if zai_daytona or terminus_daytona:
        # Provider-side destruction still applies if the local controller dies.
        ttl_minutes = (request.timeout_seconds + 600 + 59) // 60
        command.extend(["--environment-kwarg", f"ttl_minutes={ttl_minutes}"])
    command.extend(["--plugin", HARBOR_STATE_JOURNAL_PLUGIN])
    harbor_model = resolve_harbor_model(request.agent, request.model)
    if harbor_model:
        command.extend(["--model", harbor_model])
    agent_timeout_multiplier = _agent_timeout_multiplier(request)
    if agent_timeout_multiplier is not None:
        command.extend(["--agent-timeout-multiplier", agent_timeout_multiplier])
    if request.agent == "mini-swe-agent":
        if harbor_model == DEEPSEEK_MODEL_SELECTOR:
            cost_limit = request.cost_limit_usd if request.cost_limit_usd is not None else 2.5
            max_tokens = (
                request.max_output_tokens if request.max_output_tokens is not None else 8192
            )
            command.extend(
                [
                    "--n-concurrent-agents",
                    "1",
                    "--n-tasks",
                    "1",
                    "--max-retries",
                    "0",
                    "--agent-kwarg",
                    f"cost_limit={cost_limit}",
                    "--agent-kwarg",
                    f"max_tokens={max_tokens}",
                ]
            )
        elif harbor_model == ZAI_OPENAPI_MODEL_SELECTOR:
            if request.cost_limit_usd is not None:
                cost_limit = request.cost_limit_usd
            elif request.max_input_tokens is not None and request.max_output_tokens is not None:
                cost_limit = round(
                    (
                        request.max_input_tokens * ZAI_OPENAPI_INPUT_COST_MICROS_PER_MILLION
                        + request.max_output_tokens * ZAI_OPENAPI_OUTPUT_COST_MICROS_PER_MILLION
                    )
                    / 1_000_000,
                    4,
                )
            else:
                cost_limit = 2.5
            # The proxy's output allowance is cumulative across the trial.
            # Do not request that entire allowance in each model completion.
            completion_limit = (
                request.inference_settings.max_tokens
                if request.inference_settings and request.inference_settings.max_tokens is not None
                else 8192
            )
            max_tokens = min(completion_limit, request.max_output_tokens or completion_limit)
            command.extend(
                [
                    "--n-concurrent-agents",
                    "1",
                    "--n-tasks",
                    "1",
                    "--max-retries",
                    "0",
                    "--agent-kwarg",
                    f"cost_limit={cost_limit}",
                    "--agent-kwarg",
                    f"max_tokens={max_tokens}",
                ]
            )
        elif harbor_model and (
            harbor_model.startswith("glm-selfhosted/") or harbor_model.startswith("glm-ft/")
        ):
            cost_limit = request.cost_limit_usd if request.cost_limit_usd is not None else 2.5
            max_tokens = (
                request.max_output_tokens
                if request.max_output_tokens is not None
                else (
                    request.inference_settings.max_tokens
                    if request.inference_settings
                    and request.inference_settings.max_tokens is not None
                    else 8192
                )
            )
            command.extend(
                [
                    "--n-concurrent-agents",
                    "1",
                    "--n-tasks",
                    "1",
                    "--max-retries",
                    "0",
                    "--agent-kwarg",
                    f"cost_limit={cost_limit}",
                    "--agent-kwarg",
                    f"max_tokens={max_tokens}",
                ]
            )
            if request.inference_settings and request.inference_settings.effort is not None:
                command.extend(
                    ["--agent-kwarg", f"reasoning_effort={request.inference_settings.effort}"]
                )
        else:
            raise ValueError(
                f"mini-swe-agent requires model {DEEPSEEK_MODEL_SELECTOR} or {ZAI_OPENAPI_MODEL_SELECTOR}"
            )
    if request.agent == ZAI_OPENCODE_AGENT:
        if harbor_model not in ZAI_OPENCODE_MODEL_SELECTORS:
            raise ValueError(
                "zai-opencode requires one of the exact models "
                f"{sorted(ZAI_OPENCODE_MODEL_SELECTORS)}"
            )
        command.extend(
            [
                "--n-concurrent-agents",
                "1",
                "--n-tasks",
                "1",
                "--max-retries",
                "0",
            ]
        )
    if request.agent == TERMINUS_AGENT:
        command.extend(
            ["--n-concurrent-agents", "1", "--n-tasks", "1", "--max-retries", "0"]
        )
        for key, value in sorted(terminus_agent_kwargs(request).items()):
            command.extend(
                ["--agent-kwarg", f"{key}={json.dumps(value, separators=(',', ':'), allow_nan=False)}"]
            )
    if request.agent == RLM_AGENT:
        if harbor_model not in ZAI_OPENCODE_MODEL_SELECTORS:
            raise ValueError(
                f"rlm requires one of the exact models {sorted(ZAI_OPENCODE_MODEL_SELECTORS)}"
            )
        command.extend(
            [
                "--n-concurrent-agents",
                "1",
                "--n-tasks",
                "1",
                "--max-retries",
                "0",
                "--agent-kwarg",
                f"policy={request.harness_policy or 'stock'}",
                "--agent-kwarg",
                f"cost_limit_usd={request.cost_limit_usd or 1.0}",
            ]
        )
    if request.extra_instruction_path is not None:
        command.extend(["--extra-instruction-path", str(request.extra_instruction_path)])
    for skill_path in request.resolved_skills:
        command.extend(["--skill", skill_path])
    if request.harness_tree_path is not None:
        from evallab.terminus_harness import load_harness_tree

        tree = load_harness_tree(request.harness_tree_path, request.harness_tree_sha256)
        if tree.rules_path is not None:
            command.extend(["--extra-instruction-path", str(tree.rules_path)])
        for skill_root in tree.skill_roots:
            command.extend(["--skill", str(skill_root)])
    if request.load_trajectory is not None:
        command.extend(["--load-trajectory", str(request.load_trajectory)])
    if request.export_traces:
        command.append("--export-traces")
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
        if request.model == DEEPSEEK_MODEL_SELECTOR:
            overlay = (repo_root / DEEPSEEK_SECRET_COMPOSE).resolve()
            proxy = (repo_root / DEEPSEEK_PROXY_SCRIPT).resolve()
            if not overlay.is_file():
                raise RuntimeError(f"DeepSeek secret overlay is missing: {overlay}")
            if not proxy.is_file():
                raise RuntimeError(f"DeepSeek secret proxy is missing: {proxy}")
            return [*harbor_command, "--extra-docker-compose", str(overlay)]
        if request.model == ZAI_OPENAPI_MODEL_SELECTOR:
            overlay = (repo_root / ZAI_OPENAPI_SECRET_COMPOSE).resolve()
            proxy = (repo_root / ZAI_OPENAPI_PROXY_SCRIPT).resolve()
            if not overlay.is_file():
                raise RuntimeError(f"Z.ai OpenAPI secret overlay is missing: {overlay}")
            if not proxy.is_file():
                raise RuntimeError(f"Z.ai OpenAPI secret proxy is missing: {proxy}")
            return [*harbor_command, "--extra-docker-compose", str(overlay)]
        if request.model and (
            request.model.startswith("glm-selfhosted/") or request.model.startswith("glm-ft/")
        ):
            base_url = os.environ.get(GLM_SELFHOSTED_BASE_URL_ENV)
            if not base_url:
                raise ValueError(f"{GLM_SELFHOSTED_BASE_URL_ENV} environment variable is not set")
            parsed = urllib.parse.urlparse(base_url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError(
                    f"{GLM_SELFHOSTED_BASE_URL_ENV} must be a valid http or https URL, got {base_url!r}"
                )
            return harbor_command
        raise RuntimeError(
            f"the mini-swe-agent execution lane requires model {DEEPSEEK_MODEL_SELECTOR} or {ZAI_OPENAPI_MODEL_SELECTOR}"
        )
    if request.agent == ZAI_OPENCODE_AGENT:
        if request.model not in ZAI_OPENCODE_MODEL_SELECTORS:
            raise RuntimeError(
                "the zai-opencode execution lane requires one of the exact models "
                f"{sorted(ZAI_OPENCODE_MODEL_SELECTORS)}"
            )
        overlay = (repo_root / ZAI_SECRET_COMPOSE).resolve()
        proxy = (repo_root / ZAI_PROXY_SCRIPT).resolve()
        if not overlay.is_file():
            raise RuntimeError(f"Z.ai secret overlay is missing: {overlay}")
        if not proxy.is_file():
            raise RuntimeError(f"Z.ai secret proxy is missing: {proxy}")
        return [*harbor_command, "--extra-docker-compose", str(overlay)]
    return harbor_command


def transient_provider_reason(text: str) -> str | None:
    """Classify transient 429 or 5xx HTTP provider reasons from unstructured text."""
    if _PROVIDER_429.search(text):
        return "transient_harness:provider_http_429"
    if _PROVIDER_5XX.search(text):
        return "transient_harness:provider_http_5xx"
    return None


def is_lease_generation(value: object) -> bool:
    """Return True only for a strict 32-lowercase-hex lease generation."""
    return isinstance(value, str) and LEASE_GENERATION_PATTERN.fullmatch(value) is not None


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
