"""Immutable execution contracts, DTOs, and validation for runner and queue subsystems.

Key invariants:
- Immutable dataclasses for run requests, dispatch capacities, process outcomes, and authorizations.
- Non-secret environment allowlisting plus narrowly scoped, redacted credential routing.
- Strict request parameter validation (job names, timeouts, concurrency, billable approval).
- Standard Harbor command construction and transient exception classification.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import stat
import tomllib
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from evallab.harbor_repeat_verifier import VERIFIER_IMPORT_PATH
from evallab.schemas import (
    ExperimentSpec,
    RunProvenance,
    StandingApprovalsPolicy,
)

CONTROL_AGENTS = frozenset({"oracle", "nop"})
#: Fixed name of the deterministic, model-free cheat-audit agent (HAR-204).
#: It runs outside the sandbox, makes no provider calls, and derives its
#: verdict only from the benchmark's own verifier reward. Sibling lanes
#: reference this exact name; do not rename it.
CHEAT_AGENT = "cheat"
#: Env var carrying the comma-separated attack subset into the cheat trial.
#: Read by build_command (host) and evallab.harbor_cheat (agent); the default
#: empty value means the full ladder.
CHEAT_ATTACKS_ENV_VAR = "EVALLAB_CHEAT_ATTACKS"
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
#: Time a Harbor trial spends outside its agent timeout: environment start and
#: agent setup before, the verifier, artifact sync and teardown after. The
#: executor's per-trial fail-safe and the Daytona sandbox TTL both allow it on
#: top of the agent timeout. Otherwise a trial that runs its agent to the
#: timeout is killed before Harbor can verify it.
TRIAL_PHASE_ALLOWANCE_SECONDS = 600
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
    # LiteLLM maps an upstream HTTP 503 to this type with a body-free message
    # ("... ServiceUnavailableError: OpenAIException - upstream provider
    # error", G5 M1/M2 2026-10-01): no status code survives for the regexes
    # below, so the bare type must classify. A trial that still fails stays
    # infra-excluded downstream (never an agent stop, never a zero); this only
    # lets the queue retry path see it as harness-transient first.
    "ServiceUnavailableError": "transient_harness:provider_http_5xx",
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
        "EVALLAB_PROXY_LIVE_DIR",
        "EVALLAB_FILE_ACCESS",
        "EVALLAB_FILE_ACCESS_PATHS",
        "EVALLAB_STATE_JOURNAL",
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
        "XDG_STATE_HOME",
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
MIMO_AGENT = "mimoagent"
MIMO_AGENT_IMPORT_PATH = "evallab.harbor_mimoagent:NativeMimoAgent"
CHEAT_AGENT_IMPORT_PATH = "evallab.harbor_cheat:CheatAgent"
MIMO_SAMPLING_PROFILE_ENV = "EVALLAB_MIMO_SAMPLING_PROFILE"
#: Opt-in Xiaomi antihack guard for the mimoagent lane (`evallab.harbor_mimoagent`).
#: Set to 1/true to arm it. Default off: the task-level strip is the real fix
#: and the guard is bypassable (Vals' pack-parser run). Every trial records
#: the effective value in its agent metadata either way.
MIMO_ANTIHACK_ENV_VAR = "EVALLAB_MIMO_ANTIHACK"
#: Opt-in explicit-rules instruction addendum for the mimoagent lane, using
#: Vals' exact tested wording. Default off; recorded in trial metadata either way.
MIMO_EXPLICIT_RULES_ENV_VAR = "EVALLAB_MIMO_EXPLICIT_RULES"
TERMINUS_AGENT = "terminus-2"
TERMINUS_AGENT_IMPORT_PATH = "evallab.harbor_terminus:SecretSafeTerminus2"
TERMINUS_PROXY_URL_ENV = "EVALLAB_TERMINUS_PROXY_URL"
TERMINUS_LOCAL_MODEL_SELECTOR = "ollama_chat/qwen2.5:7b"
TERMINUS_LOCAL_ENDPOINT_ENV = "EVALLAB_TERMINUS_OLLAMA_URL"
BOUNDED_DAYTONA_ENVIRONMENT_IMPORT_PATH = "evallab.harbor_daytona:BoundedDaytonaEnvironment"
LOCKED_DOCKER_ENVIRONMENT_IMPORT_PATH = "evallab.harbor_docker:LockedDockerEnvironment"
ZAI_OPENCODE_AGENT = "zai-opencode"
ZAI_OPENCODE_MODEL_SELECTORS: frozenset[str] = frozenset(
    {"zai-coding-plan/glm-5.3", "zai-coding-plan/glm-5.3-flash"}
)
#: ``reasoning_effort`` values Harbor 0.24 admits for terminus-2
#: (``Terminus2Options`` in ``harbor/agents/terminus_2/terminus_2.py``).
#: Anything else is refused by Harbor preflight (``extra=forbid``), so the
#: terminus lane validates it here with a lab reason instead.
TERMINUS_REASONING_EFFORT_VALUES: frozenset[str] = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "default"}
)
#: Agents whose Harbor 0.24 ``capabilities`` admit skills
#: (``harbor/trial/trial.py`` ``_validate_agent_capabilities``). Every other
#: lane is refused at Harbor preflight when skills are configured, so
#: ``validate_request`` fails those early with a lab reason instead.
AGENTS_WITH_SKILLS_SUPPORT: frozenset[str] = frozenset({TERMINUS_AGENT, ZAI_OPENCODE_AGENT})
#: Agents whose Harbor 0.24 ``capabilities`` admit MCP servers. Same
#: preflight gate as skills.
AGENTS_WITH_MCP_SUPPORT: frozenset[str] = frozenset(
    {"mini-swe-agent", TERMINUS_AGENT, ZAI_OPENCODE_AGENT}
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
ZAI_OPENAPI_FULL_MODEL_SELECTOR = "zai/glm-5.3"
#: Exact model selectors Terminus 2 may pin on the Z.ai standard-API route.
ZAI_OPENAPI_TERMINUS_MODEL_SELECTORS: frozenset[str] = frozenset(
    {ZAI_OPENAPI_MODEL_SELECTOR, ZAI_OPENAPI_FULL_MODEL_SELECTOR}
)
ZAI_OPENAPI_ALLOWED_MODELS: frozenset[str] = frozenset(ZAI_OPENAPI_TERMINUS_MODEL_SELECTORS)
ZAI_OPENAPI_ALLOWED_MODEL = "glm-5.3-flash"
ZAI_OPENAPI_FULL_ALLOWED_MODEL = "glm-5.3"
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
PROXY_LIVE_DIR_ENV = "EVALLAB_PROXY_LIVE_DIR"
PROXY_LIVE_DIR_NAME = "proxy-live"
PROXY_LIVE_CALLS_FILE = "calls.jsonl"
PROXY_LIVE_LIMITS_FILE = "limits.json"
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
#: Z.ai standard-API list prices per native model id (USD per 1M tokens,
#: micros). glm-5.3-flash $0.15/$0.50; glm-5.3 $1.40/$4.40.
#: Source: docs.z.ai/guides/overview/pricing (verified 2026-09).
ZAI_OPENAPI_MODEL_PRICES_MICROS: Mapping[str, tuple[int, int]] = MappingProxyType(
    {
        ZAI_OPENAPI_ALLOWED_MODEL: (
            ZAI_OPENAPI_INPUT_COST_MICROS_PER_MILLION,
            ZAI_OPENAPI_OUTPUT_COST_MICROS_PER_MILLION,
        ),
        ZAI_OPENAPI_FULL_ALLOWED_MODEL: (1_400_000, 4_400_000),
    }
)


def zai_openapi_model_prices(
    environment: Mapping[str, str] | None = None,
) -> Mapping[str, tuple[int, int]]:
    """Resolve Z.ai per-model prices, honoring the flat per-run env overrides.

    The flat ``EVALLAB_ZAI_OPENAPI_{INPUT,OUTPUT}_COST_MICROS_PER_MILLION``
    overrides predate the per-model table; when set they replace the whole
    table so a pinned operator price can never be silently mixed.
    """
    source = os.environ if environment is None else environment
    raw_input = source.get("EVALLAB_ZAI_OPENAPI_INPUT_COST_MICROS_PER_MILLION")
    raw_output = source.get("EVALLAB_ZAI_OPENAPI_OUTPUT_COST_MICROS_PER_MILLION")
    if raw_input is not None and raw_output is not None:
        try:
            return MappingProxyType(
                {
                    model: (int(raw_input), int(raw_output))
                    for model in ZAI_OPENAPI_MODEL_PRICES_MICROS
                }
            )
        except ValueError as exc:
            raise ValueError(
                "EVALLAB_ZAI_OPENAPI_{INPUT,OUTPUT}_COST_MICROS_PER_MILLION must be integers"
            ) from exc
    return ZAI_OPENAPI_MODEL_PRICES_MICROS


def zai_openapi_price_for(model: str) -> tuple[int, int]:
    """Return (input, output) micros per 1M tokens for one Z.ai model id."""
    selector = model if model.startswith("zai/") else f"zai/{model}"
    native = selector.removeprefix("zai/")
    prices = zai_openapi_model_prices()
    if native not in prices:
        raise ValueError(f"no Z.ai OpenAPI price is pinned for model {model!r}")
    return prices[native]


TINKER_MODEL_PREFIX = "tinker/"
TINKER_UPSTREAM_HOST = "tinker.thinkingmachines.dev"
TINKER_UPSTREAM_PATH = "/services/tinker-prod/oai/api/v1/chat/completions"
TINKER_UPSTREAM_DEFAULT = f"https://{TINKER_UPSTREAM_HOST}"
TINKER_CREDENTIAL_ENVIRONMENT_KEYS: frozenset[str] = frozenset({"TINKER_API_KEY"})
TINKER_PROXY_TOKEN = "evallab-proxy-placeholder"
TINKER_SECRET_FILE_ENV = "EVALLAB_TINKER_SECRET_FILE"
TINKER_SECRET_PATH_ENV = "EVALLAB_TINKER_SECRET_PATH"
TINKER_PROXY_CAPABILITY_ENV = "EVALLAB_TINKER_PROXY_CAPABILITY"
TINKER_CAPABILITY_EXPIRES_AT_ENV = "EVALLAB_TINKER_CAPABILITY_EXPIRES_AT"
TINKER_UPSTREAM_ENV = "EVALLAB_TINKER_UPSTREAM"
TINKER_PROXY_ATTEMPT_ID_ENV = "EVALLAB_TINKER_ATTEMPT_ID"
TINKER_PROXY_USAGE_FILE_ENV = "EVALLAB_TINKER_USAGE_FILE"
TINKER_EXPECTED_BASE_ENV = "EVALLAB_TINKER_EXPECTED_BASE"
TINKER_PROXY_PROVIDER_ENV = "EVALLAB_PROXY_PROVIDER"
#: Thinking Machines Tinker list prices per base model (USD per 1M tokens,
#: micros; prefill/sample — cached prefill is deliberately not credited).
#: Qwen/Qwen3.6-35B-A3B 0.54/1.335; Qwen/Qwen3.8-27B 1.86/5.595;
#: Qwen/Qwen3.5-9B 0.66/1.995. Verified 2026-09-28.
TINKER_MODEL_PRICES_MICROS: Mapping[str, tuple[int, int]] = MappingProxyType(
    {
        "Qwen/Qwen3.6-35B-A3B": (540_000, 1_335_000),
        "Qwen/Qwen3.8-27B": (1_860_000, 5_595_000),
        "Qwen/Qwen3.5-9B": (660_000, 1_995_000),
    }
)
#: Context window shared by the Tinker-hosted Qwen students (input+output).
TINKER_CONTEXT_TOKENS = 65_536
#: ``tinker://<run-id>:train:<index>/sampler_weights/<step>`` — the checkpoint
#: form Tinker's OpenAI-compatible sampler accepts. Run ids are opaque
#: alphanumeric (including ``-``/``_``); train index and step are integers.
TINKER_CHECKPOINT_PATTERN = re.compile(
    r"^tinker://[A-Za-z0-9][A-Za-z0-9._-]*:train:\d+/sampler_weights/\d+$"
)


@dataclass(frozen=True)
class TinkerModelSpec:
    """One fully resolved Tinker Terminus route: selector, native id, pricing."""

    selector: str
    base_model: str
    native_model: str
    input_cost_micros_per_million: int
    output_cost_micros_per_million: int

    @property
    def is_checkpoint(self) -> bool:
        return self.native_model.startswith("tinker://")


def parse_tinker_model(model: str | None) -> TinkerModelSpec:
    """Strictly parse ``tinker/<base>[@tinker://<...>/sampler_weights/<n>]``.

    The selector's model string fully identifies the sampled weights: a bare
    base routes to base weights, the ``@`` form to a fine-tuned checkpoint.
    Anything else — unknown bases, malformed checkpoints, other providers'
    prefixes, or transport kwargs smuggled in the string — fails closed here
    before any execution or spec freeze.
    """
    if not isinstance(model, str) or not model.startswith(TINKER_MODEL_PREFIX):
        raise ValueError(
            f"Tinker Terminus model must start with {TINKER_MODEL_PREFIX!r}, got {model!r}"
        )
    remainder = model[len(TINKER_MODEL_PREFIX) :]
    if not remainder or remainder != remainder.strip() or "/" not in remainder:
        raise ValueError(f"malformed Tinker Terminus model selector: {model!r}")
    base, separator, checkpoint = remainder.partition("@")
    if base not in TINKER_MODEL_PRICES_MICROS:
        raise ValueError(
            "Tinker Terminus base model must be one of "
            f"{sorted(TINKER_MODEL_PRICES_MICROS)}, got {base!r}"
        )
    if separator:
        if not TINKER_CHECKPOINT_PATTERN.fullmatch(checkpoint):
            raise ValueError(
                "Tinker checkpoint must be "
                "tinker://<run-id>:train:<index>/sampler_weights/<step>, "
                f"got {checkpoint!r}"
            )
        native = checkpoint
    else:
        native = base
    input_cost, output_cost = TINKER_MODEL_PRICES_MICROS[base]
    return TinkerModelSpec(
        selector=model,
        base_model=base,
        native_model=native,
        input_cost_micros_per_million=input_cost,
        output_cost_micros_per_million=output_cost,
    )


def is_tinker_terminus_model(model: str | None) -> bool:
    return isinstance(model, str) and model.startswith(TINKER_MODEL_PREFIX)


#: Self-hosted MiMo route for Terminus-2: a single SGLang server on Modal
#: serving ``XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`` (run with
#: ``--served-model-name`` equal to the native id below). The Eval Lab
#: selector carries a ``selfhosted/`` prefix so it can never collide with a
#: hosted provider id; only the selectors below are admitted and anything
#: else under ``selfhosted/`` fails closed in :func:`parse_mimo_selfhosted_model`.
MIMO_SELFHOSTED_MODEL_PREFIX = "selfhosted/"
MIMO_SELFHOSTED_NATIVE_MODEL = "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
MIMO_SELFHOSTED_MODEL_SELECTOR = f"{MIMO_SELFHOSTED_MODEL_PREFIX}{MIMO_SELFHOSTED_NATIVE_MODEL}"
#: LoRA adapters served beside the base by the LoRA-enabled server
#: (``tools/modal-mimo-serve/serve_lora.py``), each chosen with SGLang's
#: ``<base>:<adapter>`` model name, which SGLang also echoes back. ``har129``
#: is the HAR-129 SFT adapter (tuned arm of the 2026-10-01 paired eval).
#: Mirrored in ``containers/zai_openapi_secret_proxy.py``.
MIMO_SELFHOSTED_ADAPTERS = ("har129",)
MIMO_SELFHOSTED_NATIVE_MODELS: frozenset[str] = frozenset(
    {MIMO_SELFHOSTED_NATIVE_MODEL}
    | {f"{MIMO_SELFHOSTED_NATIVE_MODEL}:{adapter}" for adapter in MIMO_SELFHOSTED_ADAPTERS}
)
MIMO_SELFHOSTED_MODEL_SELECTORS: frozenset[str] = frozenset(
    f"{MIMO_SELFHOSTED_MODEL_PREFIX}{native}" for native in MIMO_SELFHOSTED_NATIVE_MODELS
)
#: LiteLLM/OpenAI-compatible id sent upstream (served-model-name).
MIMO_SELFHOSTED_LITELLM_MODEL = f"openai/{MIMO_SELFHOSTED_NATIVE_MODEL}"
#: Context window as served (input+output).
MIMO_SELFHOSTED_CONTEXT_TOKENS = 262_144
#: Sampling the proxy enforces on every call (the model's generation_config):
#: SGLang's ``mimo`` reasoning parser only splits ``<think>`` when
#: ``enable_thinking=True``; without it reasoning lands in content and breaks
#: Terminus JSON. Mirrored in ``containers/zai_openapi_secret_proxy.py``,
#: which cannot import this module (standalone container script).
MIMO_SELFHOSTED_TEMPERATURE = 0.6
MIMO_SELFHOSTED_TOP_P = 0.95
MIMO_SELFHOSTED_TOP_K = 20
MIMO_SELFHOSTED_CREDENTIAL_ENVIRONMENT_KEYS: frozenset[str] = frozenset({"MIMO_SELFHOSTED_API_KEY"})
MIMO_SELFHOSTED_PROXY_TOKEN = "evallab-proxy-placeholder"
MIMO_SELFHOSTED_SECRET_FILE_ENV = "EVALLAB_MIMO_SELFHOSTED_SECRET_FILE"
MIMO_SELFHOSTED_SECRET_PATH_ENV = "EVALLAB_MIMO_SELFHOSTED_SECRET_PATH"
MIMO_SELFHOSTED_PROXY_CAPABILITY_ENV = "EVALLAB_MIMO_SELFHOSTED_PROXY_CAPABILITY"
MIMO_SELFHOSTED_CAPABILITY_EXPIRES_AT_ENV = "EVALLAB_MIMO_SELFHOSTED_CAPABILITY_EXPIRES_AT"
MIMO_SELFHOSTED_UPSTREAM_ENV = "EVALLAB_MIMO_SELFHOSTED_UPSTREAM"
MIMO_SELFHOSTED_PROXY_ATTEMPT_ID_ENV = "EVALLAB_MIMO_SELFHOSTED_ATTEMPT_ID"
MIMO_SELFHOSTED_PROXY_USAGE_FILE_ENV = "EVALLAB_MIMO_SELFHOSTED_USAGE_FILE"
MIMO_SELFHOSTED_PROXY_PROVIDER_ENV = "EVALLAB_PROXY_PROVIDER"
MIMO_SELFHOSTED_PROXY_PROVIDER = "mimo_selfhosted"
#: Operator knob for the secret proxy's bounded upstream-503 wait: total
#: seconds one model call waits through 503s (re-issuing the same
#: authenticated request with backoff) before surfacing the 503. ``0``
#: disables the wait; malformed values fall back to the default. Mirrored as
#: literals in ``containers/zai_openapi_secret_proxy.py`` (standalone
#: script); the runner passes the parent value through in
#: ``runner._terminus_proxy_env``. Default covers a full Modal cold start
#: with headroom (G5 2026-10-01 measured 3.5-4 min).
PROXY_UPSTREAM_503_WAIT_ENV = "EVALLAB_PROXY_UPSTREAM_503_WAIT_SECONDS"
PROXY_UPSTREAM_503_WAIT_DEFAULT_SECONDS = 360.0
PROXY_UPSTREAM_503_WAIT_MAX_SECONDS = 3600.0

#: Env var carrying the per-job capture route token into the metered secret
#: proxy. When set, the proxy forwards upstream to ``/t/<token>/<path>``;
#: ``evallab capture serve`` strips the prefix (recording it as
#: ``route_token``) before forwarding to the real upstream. Mirrored as a
#: literal in ``containers/zai_openapi_secret_proxy.py`` (standalone script).
CAPTURE_ROUTE_TOKEN_ENV = "EVALLAB_CAPTURE_ROUTE_TOKEN"
#: Env var carrying this job's capture directory (the ``evallab capture serve
#: --out`` directory whose ``capture.json`` holds the bound endpoint). The
#: round launcher sets it alongside ``EVALLAB_MODEL_CAPTURE=1`` and the
#: loopback upstream; the runner records it in ``lab-metadata.json`` and
#: auto-links the job to that file with ``link_capture``. Never set for
#: direct-to-vendor rounds.
CAPTURE_DIR_ENV = "EVALLAB_MODEL_CAPTURE_DIR"
#: Opt-in flag (``=1``) telling the runner a recording capture proxy sits in
#: the provider upstream path. The runner then hands each secret proxy the
#: job attempt id as ``EVALLAB_CAPTURE_ROUTE_TOKEN`` (``/t/<token>/``), so
#: ``capture link`` can attribute calls per job. Never set for
#: direct-to-vendor rounds.
CAPTURE_ENABLED_ENV = "EVALLAB_MODEL_CAPTURE"
#: Self-hosted tokens have no per-token price. The server container is billed
#: by Modal per second and accounted by the time-based estimate
#: (:func:`mimo_selfhosted_trial_cost_usd`), not the token ledger.
MIMO_SELFHOSTED_MODEL_PRICES_MICROS: Mapping[str, tuple[int, int]] = MappingProxyType(
    {native: (0, 0) for native in sorted(MIMO_SELFHOSTED_NATIVE_MODELS)}
)
#: Modal rate (USD per hour) for the whole server container, from
#: modal.com/pricing on 2026-09-28:
#:   A100-80GB $0.000694/s = $2.4984/h
#:   4 CPU cores × $0.0000131/s = $0.18864/h
#:   16 GiB × $0.00000222/s = $0.127872/h
#: Modal bills CPU and memory on top of the GPU.
MIMO_SELFHOSTED_SERVER_USD_PER_HOUR = 2.814912


def parse_mimo_selfhosted_model(model: str | None) -> str:
    """Strictly parse a self-hosted MiMo Terminus selector, returning the native id.

    Exactly ``selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`` and its
    admitted adapter names (``...:<adapter>``, :data:`MIMO_SELFHOSTED_ADAPTERS`)
    are accepted; any other ``selfhosted/...`` string — other models, other
    suffixes, or transport kwargs smuggled in the string — fails closed here
    before any execution or spec freeze.
    """
    if model not in MIMO_SELFHOSTED_MODEL_SELECTORS:
        raise ValueError(
            "self-hosted Terminus model must be exactly one of "
            f"{sorted(MIMO_SELFHOSTED_MODEL_SELECTORS)!r}, got {model!r}"
        )
    return model.removeprefix(MIMO_SELFHOSTED_MODEL_PREFIX)


def is_mimo_selfhosted_model(model: str | None) -> bool:
    return isinstance(model, str) and model.startswith(MIMO_SELFHOSTED_MODEL_PREFIX)


def is_mimo_family_model(model: str | None) -> bool:
    """Whether a model selector belongs to the MiMo family (HAR-140)."""
    if is_mimo_selfhosted_model(model):
        return True
    return isinstance(model, str) and "mimo" in model.lower()


def is_mimo_dataset_task(task: str | Path | None) -> bool:
    """Whether a task reference comes from the MiMo dataset (HAR-140).

    True when the task path, the spec ``task`` string, or the task's own
    ``task.toml`` identity (``[task] name``, ``[metadata] source_dataset``)
    names the ``mimo-v2.6`` program (for example the ``mimo-v2.6-rl__*``
    task-store slugs or the ``FineEnvs/MiMo-V2.6-RL-harbor-code`` source).
    """
    if task is None:
        return False
    text = str(task)
    if "mimo-v2.6" in text.lower():
        return True
    path = Path(text)
    if not path.is_absolute():
        return False
    try:
        document = tomllib.loads((path / "task.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return False
    if not isinstance(document, dict):
        return False
    task_table = document.get("task")
    if isinstance(task_table, dict):
        name = task_table.get("name")
        if isinstance(name, str) and "mimo-v2.6" in name.lower():
            return True
    metadata = document.get("metadata")
    if isinstance(metadata, dict):
        for key in ("source_dataset", "dataset", "source"):
            value = metadata.get(key)
            if isinstance(value, str) and "mimo" in value.lower():
                return True
        keywords = metadata.get("keywords")
        if isinstance(keywords, list) and any(
            isinstance(entry, str) and "mimo-v2.6" in entry.lower() for entry in keywords
        ):
            return True
    return False


def is_mimo_run(task: str | Path | None, model: str | None) -> bool:
    """Whether a run is a MiMo run for the Daytona egress lock (HAR-140).

    Either side counts: a MiMo-family model or a MiMo-dataset task. Control
    agents (``nop``/``oracle``) carry no model, so their census runs count by
    task alone.
    """
    return is_mimo_family_model(model) or is_mimo_dataset_task(task)


#: Docker is locked from creation, so in-container installation is not allowed.
#: Daytona preserves its existing trusted setup window before the provider lock.
EGRESS_LOCK_AGENTS = {
    "daytona": frozenset({TERMINUS_AGENT, MIMO_AGENT, *CONTROL_AGENTS}),
    "docker": frozenset({MIMO_AGENT, *CONTROL_AGENTS}),
}


def resolve_egress_lock(request: RunRequest) -> bool:
    """Resolve an explicit backend lock, preserving MiMo's Daytona default.

    Docker's creation-time lock is opt-in; an ordinary Docker control retains
    its existing network contract. Unsupported backends cannot claim a lock.
    """
    override = request.egress_lock
    if override is not None:
        if not isinstance(override, bool):
            raise ValueError("egress_lock must be true or false")
        if override and request.environment not in EGRESS_LOCK_AGENTS:
            raise ValueError("egress_lock=true requires a supported locked backend")
        return override
    if request.environment != "daytona":
        return False
    return is_mimo_run(request.task, request.model)


def _task_declares_phase_network_policy(task: Path) -> bool:
    """Whether ``task.toml`` declares a task phase network policy (HAR-140).

    Harbor would restore the task's baseline after that phase, lifting the
    egress lock, so a locked run refuses it at dispatch. The constructor of
    :class:`evallab.harbor_daytona.BoundedDaytonaEnvironment` remains the
    backstop for any phase policy Harbor derives beyond this spelling.
    """
    try:
        document = tomllib.loads((task / "task.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return False
    if not isinstance(document, dict):
        return False
    agent = document.get("agent")
    if isinstance(agent, dict) and "network_mode" in agent:
        return True
    verifier = document.get("verifier")
    return isinstance(verifier, dict) and "network_mode" in verifier


def _validate_egress_lock(request: RunRequest) -> None:
    """Refuse unsupported lock combinations without an unlocked fallback.

    There is no silent unlocked fallback: each refusal names its reason.
    """
    override = request.egress_lock
    if override is not None and not isinstance(override, bool):
        raise ValueError("egress_lock must be true or false")
    mimo = is_mimo_run(request.task, request.model)
    if request.environment not in EGRESS_LOCK_AGENTS:
        if override:
            raise ValueError("egress_lock=true requires a supported locked backend")
        return
    if request.environment == "daytona" and mimo and override is False:
        raise ValueError(
            "MiMo Daytona runs require egress_lock=true: explicit egress_lock=false "
            "is refused (no silent unlocked fallback)"
        )
    if not resolve_egress_lock(request):
        return
    if _task_has_provided_compose(request.task):
        raise ValueError(
            "egress_lock requires a single-container task: multi-container (compose) "
            "tasks cannot be locked"
        )
    if _task_declares_phase_network_policy(request.task):
        raise ValueError(
            "egress_lock cannot be combined with task phase network policies: "
            "Harbor would restore the baseline after the phase and lift the lock"
        )
    if request.agent == TERMINUS_AGENT and request.model == TERMINUS_LOCAL_MODEL_SELECTOR:
        raise ValueError(
            "egress_lock cannot be combined with the installed local model "
            f"{TERMINUS_LOCAL_MODEL_SELECTOR!r}: it needs network from inside the sandbox"
        )
    if request.agent not in EGRESS_LOCK_AGENTS[request.environment]:
        raise ValueError(
            f"egress_lock cannot be combined with agent {request.agent!r} on "
            f"{request.environment!r}: the backend cannot provide its sandbox "
            "network or installation requirements"
        )


def _validate_modal_app_name(request: RunRequest) -> None:
    """A billing-app override changes attribution, never backend or policy."""
    if request.modal_app_name is None:
        return
    if request.environment != "modal":
        raise ValueError("modal_app_name requires the Modal backend")
    if not isinstance(request.modal_app_name, str) or not request.modal_app_name.strip():
        raise ValueError("modal_app_name must be a nonempty string")


def _validate_modal_resource_policy(request: RunRequest) -> None:
    """Opt-in census billing optimization; preserve declared resource caps."""
    if request.modal_resource_policy is None:
        return
    if request.environment != "modal":
        raise ValueError("modal_resource_policy requires the Modal backend")
    if request.modal_app_name != "mimo-clean-census":
        raise ValueError("modal_resource_policy is restricted to the census billing app")
    if request.modal_resource_policy != "limit":
        raise ValueError("modal_resource_policy must be 'limit' or None")


def mimo_selfhosted_trial_cost_usd(
    trial_hours: float, concurrency: int, sandbox_usd: float
) -> float:
    """Estimate one trial's cost: server $/h x trial_hours / concurrency + sandbox_usd.

    This excludes the one-off cost of each warm period: a cold start plus
    the 300 s idle tail before scale-to-zero. With zero per-token rates the
    proxy's cost ceiling cannot trip; its request and token ceilings still
    bound the run.
    """
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError(f"concurrency must be a positive integer, got {concurrency!r}")
    for label, value in (("trial_hours", trial_hours), ("sandbox_usd", sandbox_usd)):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{label} must be a finite non-negative number, got {value!r}")
    return MIMO_SELFHOSTED_SERVER_USD_PER_HOUR * trial_hours / concurrency + sandbox_usd


#: OpenRouter metered route for Terminus-2 (HAR-104): a fixed table of models
#: behind OpenRouter's OpenAI-compatible chat-completions endpoint, each
#: pinned to one upstream serving endpoint. Selectors carry an
#: ``openrouter-metered/`` prefix — NOT ``openrouter/`` — because
#: litellm.get_llm_provider would route that prefix to its own OpenRouter
#: provider and bypass the openai-compatible path; with the metered prefix and
#: litellm_provider "openai" the selector resolves to provider "openai"
#: (verified 2026-09-29). Only selectors in :data:`OPENROUTER_ROUTES` are
#: admitted; anything else under the prefix fails closed in
#: :func:`parse_openrouter_model`.
OPENROUTER_MODEL_PREFIX = "openrouter-metered/"
OPENROUTER_UPSTREAM_HOST = "openrouter.ai"
OPENROUTER_UPSTREAM_PATH = "/api/v1/chat/completions"
OPENROUTER_UPSTREAM_DEFAULT = f"https://{OPENROUTER_UPSTREAM_HOST}"


@dataclass(frozen=True)
class OpenRouterRoute:
    """One admitted OpenRouter model and the serving pins the proxy forces.

    ``provider_pin`` names exactly one OpenRouter endpoint (``endpoint``) with
    fallbacks refused, so the upstream weights/quantization and the per-token
    list price are fixed. Prices are USD micros per 1M tokens; every pinned
    endpoint reports ``supports_implicit_caching=false``, so uncached input
    pricing is exact and cached-prefill discounts are never credited.
    Mirrored in ``containers/zai_openapi_secret_proxy.py``
    (``OPENROUTER_ROUTES``), which cannot import this module.
    """

    native_model: str
    endpoint: str
    provider_pin: Mapping[str, Any]
    reasoning_pin: Mapping[str, Any]
    input_cost_micros_per_million: int
    output_cost_micros_per_million: int
    context_input_tokens: int
    max_completion_tokens: int
    #: Model ids OpenRouter may echo for a call served by this route.
    returned_models: frozenset[str]

    @property
    def selector(self) -> str:
        return f"{OPENROUTER_MODEL_PREFIX}{self.native_model}"


OPENROUTER_ROUTES: Mapping[str, OpenRouterRoute] = MappingProxyType(
    {
        # MiMo-V2.6-Flash on Xiaomi's own endpoint (verified 2026-09-29):
        # $0.14 in / $0.28 out per 1M, 1,048,576-token context, 131,072-token
        # completions; MiMo thinking on, matching the self-hosted MiMo route.
        "xiaomi/mimo-v2.6-flash": OpenRouterRoute(
            native_model="xiaomi/mimo-v2.6-flash",
            endpoint="xiaomi/fp8",
            provider_pin=MappingProxyType({"order": ("xiaomi",), "allow_fallbacks": False}),
            reasoning_pin=MappingProxyType({"enabled": True}),
            input_cost_micros_per_million=140_000,
            output_cost_micros_per_million=280_000,
            context_input_tokens=1_048_576,
            max_completion_tokens=131_072,
            returned_models=frozenset({"xiaomi/mimo-v2.6-flash", "xiaomi/mimo-v2.6-flash:fp8"}),
        ),
        # gpt-oss-120b on DeepInfra's bf16 endpoint (verified 2026-09-30):
        # $0.037 in / $0.17 out per 1M, 131,072-token context, 117,964-token
        # completions, 99.9% 30-minute uptime. The full endpoint slug is
        # required: bare "deepinfra" also matches its turbo (16K output) and
        # fp8 endpoints at 4-5x the price. Reasoning effort is pinned to the
        # model's documented default, medium.
        "openai/gpt-oss-120b": OpenRouterRoute(
            native_model="openai/gpt-oss-120b",
            endpoint="deepinfra/bf16",
            provider_pin=MappingProxyType({"order": ("deepinfra/bf16",), "allow_fallbacks": False}),
            reasoning_pin=MappingProxyType({"effort": "medium"}),
            input_cost_micros_per_million=37_000,
            output_cost_micros_per_million=170_000,
            context_input_tokens=131_072,
            max_completion_tokens=117_964,
            returned_models=frozenset({"openai/gpt-oss-120b"}),
        ),
    }
)
OPENROUTER_MODEL_SELECTORS: tuple[str, ...] = tuple(
    route.selector for route in OPENROUTER_ROUTES.values()
)
OPENROUTER_CREDENTIAL_ENVIRONMENT_KEYS: frozenset[str] = frozenset({"OPENROUTER_API_KEY"})
#: The one native model a trial's proxy admits; the runner sets it from the
#: trial's selector so a sandbox cannot switch to another admitted route.
OPENROUTER_EXPECTED_MODEL_ENV = "EVALLAB_OPENROUTER_EXPECTED_MODEL"
OPENROUTER_PROXY_TOKEN = "evallab-proxy-placeholder"
OPENROUTER_SECRET_FILE_ENV = "EVALLAB_OPENROUTER_SECRET_FILE"
OPENROUTER_SECRET_PATH_ENV = "EVALLAB_OPENROUTER_SECRET_PATH"
OPENROUTER_PROXY_CAPABILITY_ENV = "EVALLAB_OPENROUTER_PROXY_CAPABILITY"
OPENROUTER_CAPABILITY_EXPIRES_AT_ENV = "EVALLAB_OPENROUTER_CAPABILITY_EXPIRES_AT"
OPENROUTER_UPSTREAM_ENV = "EVALLAB_OPENROUTER_UPSTREAM"
OPENROUTER_PROXY_ATTEMPT_ID_ENV = "EVALLAB_OPENROUTER_ATTEMPT_ID"
OPENROUTER_PROXY_USAGE_FILE_ENV = "EVALLAB_OPENROUTER_USAGE_FILE"
OPENROUTER_PROXY_PROVIDER_ENV = "EVALLAB_PROXY_PROVIDER"
OPENROUTER_PROXY_PROVIDER = "openrouter"


def openrouter_route(model: str | None) -> OpenRouterRoute:
    """Strictly resolve an OpenRouter Terminus selector to its pinned route.

    Exactly the selectors of :data:`OPENROUTER_ROUTES` are admitted; any other
    string under the prefix — other models, suffixes, other providers'
    prefixes, or transport kwargs smuggled in the string — fails closed here
    before any execution or spec freeze.
    """
    if isinstance(model, str) and model.startswith(OPENROUTER_MODEL_PREFIX):
        route = OPENROUTER_ROUTES.get(model.removeprefix(OPENROUTER_MODEL_PREFIX))
        if route is not None and route.selector == model:
            return route
    raise ValueError(
        f"OpenRouter Terminus model must be exactly one of {OPENROUTER_MODEL_SELECTORS!r}, "
        f"got {model!r}"
    )


def parse_openrouter_model(model: str | None) -> str:
    """The native OpenRouter model id of an admitted selector (see :func:`openrouter_route`)."""
    return openrouter_route(model).native_model


def is_openrouter_model(model: str | None) -> bool:
    return isinstance(model, str) and model.startswith(OPENROUTER_MODEL_PREFIX)


def materialize_openrouter_secret_file(
    destination: Path,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Write the OpenRouter API key to a 0400 file for the host loopback proxy."""
    source = os.environ if environment is None else environment
    value = source.get("OPENROUTER_API_KEY")
    if value == OPENROUTER_PROXY_TOKEN:
        value = None
    if not value:
        existing = source.get(OPENROUTER_SECRET_FILE_ENV)
        if existing:
            path = Path(existing)
            try:
                read_owner_secret_file(path)
            except OSError as exc:
                raise RuntimeError("OpenRouter provider credential is missing") from exc
            return path
        raise RuntimeError(
            "OpenRouter provider credential is missing: set OPENROUTER_API_KEY "
            "(or EVALLAB_OPENROUTER_SECRET_FILE) before launching Harbor"
        )
    persist_private_bytes(destination, f"{value}\n".encode(), secrets=(), mode=0o400)
    return destination


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
    rb'(?i)(authorization\s*[:=]\s*bearer\s+)[^"\s\\]+',
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
    MIMO_AGENT: MIMO_AGENT_IMPORT_PATH,
    CHEAT_AGENT: CHEAT_AGENT_IMPORT_PATH,
}

DEEPSEEK_MODEL_SELECTOR = "deepseek/deepseek-flash"
DEEPSEEK_SECRET_COMPOSE = Path("containers/deepseek-v4-flash-secret.compose.yaml")

HARBOR_STATE_JOURNAL_PLUGIN = "evallab.harbor_state_journal:StateJournalPlugin"
HARBOR_WATCH_HOOKS_PLUGIN = "evallab.harbor_watch_hooks:WatchHookPlugin"
HARBOR_FILE_ACCESS_PLUGIN = "evallab.harbor_file_access:FileAccessPlugin"

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
    verifier_repeat_n: int | None = None
    override_storage_mb: int | None = None
    requested_selector: str | None = None
    effective_endpoint_base: str | None = None
    provider_returned_model_id: str | None = None
    inference_settings: ProfileInferenceSettings | None = None
    egress_lock: bool | None = None
    modal_app_name: str | None = None
    modal_resource_policy: str | None = None
    diff_sources: tuple[Path, ...] = ()

    @property
    def trial_watchdog_seconds(self) -> int:
        """Per-trial fail-safe: the agent timeout plus Harbor's other phases."""
        return self.timeout_seconds + TRIAL_PHASE_ALLOWANCE_SECONDS

    @property
    def job_timeout_seconds(self) -> int:
        """Conservative process deadline: one trial fail-safe per attempt."""
        return self.trial_watchdog_seconds * self.attempts

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
class HarborProcessResult:
    """Outcome of running a Harbor subprocess under watchdog supervision."""

    returncode: int
    timed_out: bool
    log_path: Path
    timed_out_trial: str | None = None
    proxy_usage: dict[str, Any] | None = None


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


def materialize_tinker_secret_file(
    destination: Path,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Write the Tinker API key to a 0400 file for the host loopback proxy."""
    source = os.environ if environment is None else environment
    value = source.get("TINKER_API_KEY")
    if value == TINKER_PROXY_TOKEN:
        value = None
    if not value:
        existing = source.get(TINKER_SECRET_FILE_ENV)
        if existing:
            path = Path(existing)
            try:
                read_owner_secret_file(path)
            except OSError as exc:
                raise RuntimeError("Tinker provider credential is missing") from exc
            return path
        raise RuntimeError("Tinker provider credential is missing")
    persist_private_bytes(destination, f"{value}\n".encode(), secrets=(), mode=0o400)
    return destination


def materialize_mimo_selfhosted_secret_file(
    destination: Path,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Write the self-hosted MiMo SGLang API key to a 0400 file for the host loopback proxy."""
    source = os.environ if environment is None else environment
    value = source.get("MIMO_SELFHOSTED_API_KEY")
    if value == MIMO_SELFHOSTED_PROXY_TOKEN:
        value = None
    if not value:
        existing = source.get(MIMO_SELFHOSTED_SECRET_FILE_ENV)
        if existing:
            path = Path(existing)
            try:
                read_owner_secret_file(path)
            except OSError as exc:
                raise RuntimeError("Mimo self-hosted provider credential is missing") from exc
            return path
        raise RuntimeError("Mimo self-hosted provider credential is missing")
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
        *((key, TINKER_PROXY_TOKEN) for key in TINKER_CREDENTIAL_ENVIRONMENT_KEYS),
        *(
            (key, MIMO_SELFHOSTED_PROXY_TOKEN)
            for key in MIMO_SELFHOSTED_CREDENTIAL_ENVIRONMENT_KEYS
        ),
        *((key, OPENROUTER_PROXY_TOKEN) for key in OPENROUTER_CREDENTIAL_ENVIRONMENT_KEYS),
        *((key, GLM_SELFHOSTED_PROXY_TOKEN) for key in GLM_SELFHOSTED_CREDENTIAL_ENVIRONMENT_KEYS),
        ("LMNR_PROJECT_API_KEY", ""),
    ):
        value = source.get(key)
        if value and value != placeholder:
            values.add(value)
    for secret_file_env, placeholder in (
        (DEEPSEEK_SECRET_FILE_ENV, DEEPSEEK_PROXY_TOKEN),
        (ZAI_SECRET_FILE_ENV, ZAI_PROXY_TOKEN),
        (ZAI_OPENAPI_SECRET_FILE_ENV, ZAI_OPENAPI_PROXY_TOKEN),
        (TINKER_SECRET_FILE_ENV, TINKER_PROXY_TOKEN),
        (MIMO_SELFHOSTED_SECRET_FILE_ENV, MIMO_SELFHOSTED_PROXY_TOKEN),
        (OPENROUTER_SECRET_FILE_ENV, OPENROUTER_PROXY_TOKEN),
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
        (TINKER_PROXY_CAPABILITY_ENV, TINKER_PROXY_TOKEN),
        (MIMO_SELFHOSTED_PROXY_CAPABILITY_ENV, MIMO_SELFHOSTED_PROXY_TOKEN),
        (OPENROUTER_PROXY_CAPABILITY_ENV, OPENROUTER_PROXY_TOKEN),
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
    include_laminar_credentials: bool = False,
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
    if include_laminar_credentials and source.get("LMNR_PROJECT_API_KEY"):
        sanitized["LMNR_PROJECT_API_KEY"] = source["LMNR_PROJECT_API_KEY"]
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
        | TINKER_CREDENTIAL_ENVIRONMENT_KEYS
        | MIMO_SELFHOSTED_CREDENTIAL_ENVIRONMENT_KEYS
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
    """Whether this model route can enforce the provider request/token/cost ceilings.

    Every non-local Terminus model counts as metered here, so an unsupported
    selector reaches `validate_request`'s model grammar check and is refused
    with that reason rather than a misleading ceiling error.
    """
    return agent in {"mini-swe-agent", ZAI_OPENCODE_AGENT, MIMO_AGENT} or (
        agent == TERMINUS_AGENT and model != TERMINUS_LOCAL_MODEL_SELECTOR
    )


def validate_request(request: RunRequest, *, repo_root: Path | None = None) -> None:
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
    _validate_modal_app_name(request)
    _validate_modal_resource_policy(request)
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
    if request.agent == MIMO_AGENT:
        parse_mimo_selfhosted_model(request.model)
        if request.environment not in EGRESS_LOCK_AGENTS or not resolve_egress_lock(request):
            raise ValueError("mimoagent requires a supported locked task environment")
    if request.agent == TERMINUS_AGENT:
        model = request.model
        if is_tinker_terminus_model(model):
            # Strict fail-closed parse: unknown base or malformed checkpoint
            # refuses here, before any spec freeze or execution.
            parse_tinker_model(model)
        elif is_mimo_selfhosted_model(model):
            # Exactly one self-hosted selector is admitted; anything else
            # under selfhosted/ refuses here.
            parse_mimo_selfhosted_model(model)
        elif is_openrouter_model(model):
            # Exactly one OpenRouter selector is admitted; anything else
            # under openrouter-metered/ refuses here.
            parse_openrouter_model(model)
        elif model not in {
            *ZAI_OPENAPI_TERMINUS_MODEL_SELECTORS,
            TERMINUS_LOCAL_MODEL_SELECTOR,
        }:
            raise ValueError(
                "terminus-2 requires a Z.ai standard-API model "
                f"({sorted(ZAI_OPENAPI_TERMINUS_MODEL_SELECTORS)}), a Tinker "
                f"route ({TINKER_MODEL_PREFIX}<base>[@tinker://<run>:train:<i>"
                "/sampler_weights/<step>]), the self-hosted route "
                f"{MIMO_SELFHOSTED_MODEL_SELECTOR!r}, the OpenRouter routes "
                f"{OPENROUTER_MODEL_SELECTORS!r}, or installed local "
                f"{TERMINUS_LOCAL_MODEL_SELECTOR!r}; Coding Plan credentials "
                "are not admitted for this harness"
            )
        if request.attempts != 1 or request.concurrency != 1:
            raise ValueError("terminus-2 specs bind exactly one trial")
    if request.harness_policy is not None and request.agent != RLM_AGENT:
        raise ValueError("harness_policy is supported only by the rlm lane")
    if request.verifier_repeat_n is not None and not 2 <= request.verifier_repeat_n <= 10:
        raise ValueError("verifier_repeat_n must be between 2 and 10")
    if (
        request.override_storage_mb is not None
        and not 1024 <= request.override_storage_mb <= 1048576
    ):
        raise ValueError("override_storage_mb must be between 1024 and 1048576")
    if request.agent == RLM_AGENT:
        if request.attempts != 1 or request.concurrency != 1:
            raise ValueError(f"{request.agent} capabilities bind exactly one trial")
        if request.model not in ZAI_OPENCODE_MODEL_SELECTORS:
            raise ValueError(
                f"rlm requires one of the exact models {sorted(ZAI_OPENCODE_MODEL_SELECTORS)}"
            )
    # NEEDS PARENT APPROVAL (HAR-204 cheat lane): the model-free cheat audit
    # agent is admitted without --allow-billable. It makes no provider calls
    # ($0 by construction: no model, no proxy, local Docker only); every
    # billable agent still requires --allow-billable below.
    if (
        request.agent not in CONTROL_AGENTS
        and request.agent != CHEAT_AGENT
        and not request.allow_billable
    ):
        raise ValueError(
            f"Agent {request.agent!r} may invoke a model. Pass --allow-billable "
            "after reviewing credentials, model, and expected cost."
        )
    # NEEDS PARENT APPROVAL (HAR-204 cheat lane): like the oracle/nop controls,
    # the cheat agent is model-free and never accepts a model selector.
    if request.model and (request.agent in CONTROL_AGENTS or request.agent == CHEAT_AGENT):
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
        if request.agent not in AGENTS_WITH_SKILLS_SUPPORT:
            raise ValueError(
                f"Agent {request.agent!r} does not support toolbox skills under Harbor 0.24: "
                "trial.py _validate_agent_capabilities refuses skills for agents "
                "without capabilities.skills, so Harbor preflight would refuse the trial; "
                f"supported agents are {sorted(AGENTS_WITH_SKILLS_SUPPORT)}"
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

    _validate_agent_capabilities(request)
    _validate_egress_lock(request)
    _validate_setup_fingerprint(request, repo_root)


def _task_has_mcp_servers(task: Path) -> bool:
    """Whether the task declares environment MCP servers (Harbor 0.24 capability gate input)."""
    try:
        document = tomllib.loads((task / "task.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return False
    environment = document.get("environment")
    if not isinstance(environment, dict):
        return False
    servers = environment.get("mcp_servers")
    return isinstance(servers, list) and len(servers) > 0


def _validate_agent_capabilities(request: RunRequest) -> None:
    """Mirror Harbor 0.24's pre-sandbox capability gate in our preflight.

    Harbor 0.24 ``trial.py`` ``_validate_agent_capabilities`` refuses a trial
    whose task or agent config carries skills/MCP the agent's
    ``capabilities`` do not admit. Fail those specs here with a lab reason
    instead of burning a dispatch on Harbor's preflight refusal.
    """
    known_lanes = {
        *AGENTS_WITH_SKILLS_SUPPORT,
        *AGENTS_WITH_MCP_SUPPORT,
        RLM_AGENT,
        MIMO_AGENT,
        *CONTROL_AGENTS,
    }
    if request.agent not in known_lanes:
        # Passthrough agents (codex, antigravity, ...): Harbor's own preflight
        # validates their 0.24 capabilities; the lab pins no allowlist for them.
        return
    skill_roots: list[str] = []
    if request.harness_tree_path is not None:
        from evallab.terminus_harness import load_harness_tree

        tree = load_harness_tree(request.harness_tree_path, request.harness_tree_sha256)
        skill_roots.extend(str(root) for root in tree.skill_roots)
    if (request.resolved_skills or skill_roots) and request.agent not in AGENTS_WITH_SKILLS_SUPPORT:
        raise ValueError(
            f"Agent {request.agent!r} does not support skills under Harbor 0.24 "
            "(capabilities.skills is false); the trial would be refused at Harbor "
            "preflight. Remove the skill sources or use one of "
            f"{sorted(AGENTS_WITH_SKILLS_SUPPORT)}."
        )
    if _task_has_mcp_servers(request.task) and request.agent not in AGENTS_WITH_MCP_SUPPORT:
        raise ValueError(
            f"Agent {request.agent!r} does not support MCP servers under Harbor 0.24 "
            "(capabilities.mcp_servers is false) but the task declares "
            "environment.mcp_servers; the trial would be refused at Harbor "
            "preflight. Use one of "
            f"{sorted(AGENTS_WITH_MCP_SUPPORT)}."
        )


def _validate_setup_fingerprint(request: RunRequest, repo_root: Path | None) -> None:
    """Refuse a spec-driven MiMo run whose setup differs from its reference (HAR-149).

    Ad-hoc and prepared-task requests carry no experiment spec and keep the
    previous behaviour; every queue dispatch carries one. Non-MiMo runs are
    untouched.
    """
    spec = request.experiment_spec
    if spec is None or (
        not is_mimo_run(request.task, request.model) and not spec.reference_profile
    ):
        return
    from evallab.setup_fingerprint import resolve_repo_root, validate_mimo_setup

    root = resolve_repo_root(repo_root, request.task)
    validate_mimo_setup(request, root)


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
        effort = request.inference_settings.effort
        if isinstance(effort, bool) or str(effort) not in TERMINUS_REASONING_EFFORT_VALUES:
            raise ValueError(
                f"Terminus reasoning_effort {effort!r} is refused by Harbor 0.24 "
                f"(Terminus2Options admits {sorted(TERMINUS_REASONING_EFFORT_VALUES)})"
            )
        kwargs["reasoning_effort"] = effort
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


def _env_opt_in(name: str) -> bool:
    """Whether an opt-in env knob is armed (1/true/yes/on; default off)."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def format_task_chain(newest_first: Iterable[str]) -> str:
    """Join lineage transforms oldest-first (`a>b`), or `"original"` when bare."""
    ordered = list(newest_first)
    return ">".join(reversed(ordered)) if ordered else "original"


def mimoagent_agent_kwargs(task_dir: Path, *, repo_root: Path | None = None) -> dict[str, Any]:
    """Harbor agent kwargs for the mimoagent lane, all recorded in trial metadata.

    ``antihack``/``explicit_rules`` come from their env knobs (default off);
    ``task_chain_digest`` is the exact package digest and ``task_chain`` the
    lineage transform chain, oldest first. The chain resolves only when a
    lineage record matches the digest — original and ad-hoc tasks omit it,
    and the digest still identifies the bytes. The lookup globs the digest
    filename directly and never scans the records tree. The adapter
    normalizes and records every value, so unset options are recorded as
    off, never as missing.
    """
    from evallab.registry import task_directory_digest

    digest = task_directory_digest(task_dir)
    kwargs: dict[str, Any] = {
        "antihack": _env_opt_in(MIMO_ANTIHACK_ENV_VAR),
        "explicit_rules": _env_opt_in(MIMO_EXPLICIT_RULES_ENV_VAR),
        "task_chain_digest": digest,
    }
    chain = _resolve_task_chain(digest, repo_root=repo_root)
    if chain is not None:
        kwargs["task_chain"] = chain
    return kwargs


def _resolve_task_chain(digest: str, *, repo_root: Path | None) -> str | None:
    """Lineage chain for a package digest, or ``None`` when unresolvable.

    Fail-open and scan-free: the digest filename addresses the record
    directly (``<records>/<slug>/<digest12>.json``), so an original task
    costs one failed glob, not a tree walk.
    """
    try:
        from evallab.task_variants import RECORDS_DIRNAME, lineage_chain, resolve_record

        root = Path(repo_root).resolve() if repo_root is not None else Path.cwd().resolve()
        short = digest.split(":", 1)[1][:12]
        for candidate in sorted((root / RECORDS_DIRNAME).glob(f"*/{short}.json")):
            try:
                record = resolve_record(candidate, repo_root=root)
            except Exception:
                continue
            if record.variant_digest != digest:
                continue
            steps = lineage_chain(record, repo_root=root)
            return format_task_chain(step.record.transform for step in steps)
    except Exception:
        return None
    return None


def build_command(
    request: RunRequest,
    *,
    setup_fingerprint: str | None = None,
    repo_root: Path | None = None,
) -> list[str]:
    """Build the exact Harbor CLI invocation command for a RunRequest."""
    from evallab.setup_fingerprint import lock_setup_fingerprint

    environment = request.environment
    zai_daytona = (
        environment == "daytona"
        and request.agent == "mini-swe-agent"
        and request.model == ZAI_OPENAPI_MODEL_SELECTOR
    )
    if zai_daytona:
        environment = "evallab.harbor_daytona:SecretSafeDaytonaEnvironment"
    elif environment == "daytona":
        environment = BOUNDED_DAYTONA_ENVIRONMENT_IMPORT_PATH
    elif environment == "docker" and resolve_egress_lock(request):
        environment = LOCKED_DOCKER_ENVIRONMENT_IMPORT_PATH
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
    command.extend(
        [
            "--agent-env",
            "EVALLAB_SETUP_FINGERPRINT="
            + (
                setup_fingerprint
                if setup_fingerprint is not None
                else lock_setup_fingerprint(request)
            ),
        ]
    )
    for source in request.diff_sources:
        command.extend(["--diff", str(source)])
    if request.diff_sources:
        command.append("--yes")
    if request.environment == "daytona":
        # Provider-side destruction still applies if the local controller dies.
        ttl_minutes = (request.trial_watchdog_seconds + 59) // 60
        command.extend(["--environment-kwarg", f"ttl_minutes={ttl_minutes}"])
    _validate_modal_app_name(request)
    if request.modal_app_name is not None:
        command.extend(["--environment-kwarg", f"app_name={request.modal_app_name}"])
    _validate_modal_resource_policy(request)
    if request.modal_resource_policy is not None:
        for resource in ("cpu", "memory"):
            command.extend(
                [
                    "--environment-kwarg",
                    f"{resource}_enforcement_policy={request.modal_resource_policy}",
                ]
            )
    if resolve_egress_lock(request):
        _validate_egress_lock(request)
        command.extend(["--environment-kwarg", "egress_lock=true"])
    command.extend(["--plugin", HARBOR_STATE_JOURNAL_PLUGIN])
    command.extend(["--plugin", HARBOR_WATCH_HOOKS_PLUGIN])
    command.extend(["--plugin", HARBOR_FILE_ACCESS_PLUGIN])
    if request.verifier_repeat_n is not None:
        command.extend(
            [
                "--verifier",
                VERIFIER_IMPORT_PATH,
                "--verifier-kwarg",
                f"repeat_n={request.verifier_repeat_n}",
            ]
        )
    if request.override_storage_mb is not None:
        command.extend(["--override-storage-mb", str(request.override_storage_mb)])
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
    if request.agent == MIMO_AGENT:
        command.extend(["--n-concurrent-agents", "1", "--n-tasks", "1", "--max-retries", "0"])
        for key, value in sorted(mimoagent_agent_kwargs(request.task, repo_root=repo_root).items()):
            command.extend(
                [
                    "--agent-kwarg",
                    f"{key}={json.dumps(value, separators=(',', ':'), allow_nan=False)}",
                ]
            )
    if request.agent == TERMINUS_AGENT:
        command.extend(["--n-concurrent-agents", "1", "--n-tasks", "1", "--max-retries", "0"])
        for key, value in sorted(terminus_agent_kwargs(request).items()):
            command.extend(
                [
                    "--agent-kwarg",
                    f"{key}={json.dumps(value, separators=(',', ':'), allow_nan=False)}",
                ]
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
    if request.agent == CHEAT_AGENT:
        # NEEDS PARENT APPROVAL (HAR-204 cheat lane): forward the CLI-selected
        # attack subset to the model-free agent. Empty means the full ladder.
        attacks = os.environ.get(CHEAT_ATTACKS_ENV_VAR, "")
        command.extend(["--agent-env", f"{CHEAT_ATTACKS_ENV_VAR}={attacks}"])
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
    if request.agent == MIMO_AGENT and os.environ.get("LMNR_PROJECT_API_KEY"):
        executable = repo_root / ".venv/bin/harbor"
        if not executable.is_file():
            raise RuntimeError(
                "Laminar-enabled mimoagent requires the pinned host runtime: "
                "uv sync --locked --extra laminar"
            )
        return [str(executable), *harbor_command[1:]]
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
