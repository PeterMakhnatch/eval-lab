"""Provider-neutral agent profiles and credential preflight seams (M003).

An AgentProfile is the immutable identity of one runnable agent
configuration: Harbor adapter + exact model pin + auth mode + secret-source
*identifier* (never the secret) + requirements, capabilities, resources, and
limits, with a stable content digest.

Hard rules encoded here rather than remembered:

- Subscription credentials remain the default. The DeepSeek mini-swe-agent
  lane is the sole API-key exception and may name only its two admitted
  environment variables.
- Probes return availability, expiry, and reason only — no secret material —
  and run through the same injected seams execution uses.
- Qualification is a ladder of separate states (declared → installed →
  credential-ready → smoke-passed → canary-qualified); nothing is assumed.
- Auth failure is a *preflight stop*, never a trial that scores reward zero.
- Only independently observed facts are recorded as verified.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from evallab.schemas import AgentReadinessRecord, AgentSmokeRecord

# Substrings that identify API-key style environment variables. They remain
# forbidden everywhere except the exact DeepSeek environment source below.
_FORBIDDEN_KEY_MARKERS = ("API_KEY", "API_TOKEN", "_SECRET", "ACCESS_KEY")
DEEPSEEK_CREDENTIAL_NAMES = frozenset({"DEEPSEEK_API_KEY", "MSWEA_API_KEY"})

AuthMode = Literal[
    "none",
    "api-key-environment",
    "subscription-auth-file",
    "subscription-keychain",
    "subscription-cli-session",
]

CONTROL_ADAPTERS = frozenset({"oracle", "nop"})


def _rejects_api_key_names(values: tuple[str, ...] | list[str]) -> None:
    for value in values:
        upper = value.upper()
        if any(marker in upper for marker in _FORBIDDEN_KEY_MARKERS):
            raise ValueError(
                f"subscriptions-only: {value!r} looks like an API-key variable; "
                "profiles may never name one"
            )


class ProfileResources(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cpus: int = Field(default=2, ge=1)
    memory_mb: int = Field(default=4096, ge=256)


class ProfileLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_timeout_seconds: int = Field(default=21_600, ge=1)
    max_attempts: int = Field(default=5, ge=1)
    max_concurrency: int = Field(default=2, ge=1)


class AgentProfile(BaseModel):
    """Immutable identity of one runnable agent configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]+$", max_length=80)
    adapter: str = Field(min_length=1)
    adapter_version: str | None = None
    model: str | None = None
    auth_mode: AuthMode
    secret_source: str | None = Field(
        default=None,
        description=(
            "Identifier of where the credential lives (keychain service name or "
            "auth-file path pattern). An identifier, never secret material."
        ),
    )
    required_files: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    resources: ProfileResources = ProfileResources()
    limits: ProfileLimits = ProfileLimits()
    verified_facts: tuple[str, ...] = Field(
        default=(),
        description="Independently observed evidence, one dated sentence each.",
    )

    @field_validator("required_files", "capabilities", "verified_facts")
    @classmethod
    def no_api_key_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _rejects_api_key_names(value)
        return value

    @field_validator("secret_source")
    @classmethod
    def secret_source_is_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value.startswith("env:"):
            names = tuple(name.strip() for name in value.removeprefix("env:").split(","))
            if not names or any(not name for name in names):
                raise ValueError("environment secret_source must name at least one variable")
            if frozenset(names) != DEEPSEEK_CREDENTIAL_NAMES:
                raise ValueError(
                    "environment secret_source may name only the admitted DeepSeek variables"
                )
            return value
        _rejects_api_key_names((value,))
        if not value.startswith(("keychain:", "file:", "cli:")):
            raise ValueError(
                "secret_source must be 'keychain:<service>', 'file:<pattern>', "
                "'cli:<command>', or the admitted DeepSeek 'env:<names>' source"
            )
        return value

    @model_validator(mode="after")
    def coherent_identity(self) -> AgentProfile:
        if self.adapter in CONTROL_ADAPTERS:
            if self.auth_mode != "none" or self.model is not None:
                raise ValueError("control adapters take no credential and no model")
        else:
            if self.auth_mode == "none":
                raise ValueError(
                    f"adapter {self.adapter!r} invokes a model; auth_mode "
                    "'none' is reserved for controls"
                )
            if not self.model:
                raise ValueError("billable profiles require an exact model pin")
            if self.secret_source is None:
                raise ValueError("billable profiles must identify their secret source")
        if (
            self.auth_mode == "subscription-keychain"
            and self.secret_source is not None
            and not self.secret_source.startswith("keychain:")
        ):
            raise ValueError("keychain auth requires a keychain: secret source")
        if (
            self.auth_mode == "subscription-auth-file"
            and self.secret_source is not None
            and not self.secret_source.startswith("file:")
        ):
            raise ValueError("auth-file auth requires a file: secret source")
        if (
            self.auth_mode == "subscription-cli-session"
            and self.secret_source is not None
            and not self.secret_source.startswith("cli:")
        ):
            raise ValueError("cli-session auth requires a 'cli:<command>' secret source")
        if (
            self.auth_mode == "api-key-environment"
            and self.secret_source is not None
            and not self.secret_source.startswith("env:")
        ):
            raise ValueError("environment API-key auth requires an env: secret source")
        return self

    def canonical_json(self) -> str:
        """Deterministic serialization: sorted keys, no whitespace variance."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_json().encode()).hexdigest()


class ProfileState(StrEnum):
    """Qualification ladder. Each state is earned separately, never implied."""

    DECLARED = "declared"
    INSTALLED = "installed"
    CREDENTIAL_READY = "credential-ready"
    SMOKE_PASSED = "smoke-passed"
    CANARY_QUALIFIED = "canary-qualified"


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a credential probe: availability, expiry, reason. Nothing else.

    Deliberately not capable of carrying secret material: three typed fields,
    no free-form payload.
    """

    ok: bool
    expires_at: datetime | None = None
    reason: str | None = None


ProbeFn = Callable[[AgentProfile], ProbeResult]
SecurityRunner = Callable[[list[str]], int]
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def default_security_runner(args: list[str]) -> int:
    """Probe macOS Keychain metadata without reading or emitting secret values."""
    completed = subprocess.run(
        ["/usr/bin/security", *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    return completed.returncode


@dataclass(frozen=True)
class KeychainProbe:
    """Probe a macOS keychain service for existence — exit status only.

    The same injected runner/environment seams execution uses; the secret
    value is never requested with ``-w`` into captured output, never parsed,
    never returned.
    """

    security_runner: SecurityRunner
    service: str
    account: str

    def __call__(self, profile: AgentProfile) -> ProbeResult:
        if not self.account:
            return ProbeResult(ok=False, reason="keychain account unresolved")
        try:
            status = self.security_runner(
                ["find-generic-password", "-s", self.service, "-a", self.account]
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ProbeResult(ok=False, reason=f"keychain probe failed: {exc.__class__.__name__}")
        if status != 0:
            return ProbeResult(ok=False, reason=f"keychain item absent for {self.service}")
        return ProbeResult(ok=True)


_EXPIRY_KEYS = ("expires_at", "expire_at", "expiry", "expires")


@dataclass(frozen=True)
class AuthFileProbe:
    """Probe a subscription auth file. Reads structure for expiry only.

    Token values are never returned, logged, or attached to the result.
    """

    home: Path
    relative_path: str
    clock: Clock = _utc_now

    def __call__(self, profile: AgentProfile) -> ProbeResult:
        path = self.home / self.relative_path
        if not path.is_file():
            return ProbeResult(ok=False, reason=f"auth file missing: ~/{self.relative_path}")
        expires_at = self._expiry(path)
        if expires_at is not None and expires_at <= self.clock():
            return ProbeResult(ok=False, expires_at=expires_at, reason="auth file expired")
        return ProbeResult(ok=True, expires_at=expires_at)

    def _expiry(self, path: Path) -> datetime | None:
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        stack: list[object] = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in _EXPIRY_KEYS:
                        parsed = _parse_expiry(value)
                        if parsed is not None:
                            return parsed
                    else:
                        stack.append(value)
            elif isinstance(node, list):
                stack.extend(node)
        return None


@dataclass(frozen=True)
class EnvironmentPresenceProbe:
    """Check admitted credential names for non-empty values without returning them."""

    environment: Mapping[str, str]
    names: tuple[str, ...]

    def __call__(self, profile: AgentProfile) -> ProbeResult:
        del profile
        if any(bool(self.environment.get(name)) for name in self.names):
            return ProbeResult(ok=True)
        return ProbeResult(
            ok=False,
            reason="credential environment missing: " + " or ".join(self.names),
        )


def _parse_expiry(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


@dataclass(frozen=True)
class CliSessionProbe:
    """Ask a subscription CLI whether it currently holds a session.

    Some subscription CLIs keep their credential in an opaque internal store
    rather than a readable auth file or a keychain item — `cursor-agent` is the
    case that forced this seam: `~/.cursor/` holds only UI config, no keychain
    entry exists, and the only honest way to know whether the lane can run is to
    ask the CLI. Probing a config file instead would report "available" while the
    session was expired, which is worse than reporting nothing.

    Structure only: the probe reads the command's **exit status** and matches its
    stdout against an expected marker. It never captures, stores or forwards a
    token, and the command it runs must never be one that prints a secret.
    """

    argv: tuple[str, ...]
    expect: str
    runner: Callable[[Sequence[str]], tuple[int, str]] | None = None
    timeout_seconds: float = 20.0

    def __call__(self, profile: AgentProfile) -> ProbeResult:
        run = self.runner or self._default_runner
        try:
            status, stdout = run(self.argv)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ProbeResult(
                ok=False, reason=f"cli session probe failed: {exc.__class__.__name__}"
            )
        if status != 0:
            return ProbeResult(
                ok=False, reason=f"{self.argv[0]} reports no session (exit {status})"
            )
        if self.expect and self.expect.lower() not in stdout.lower():
            return ProbeResult(
                ok=False,
                reason=f"{self.argv[0]} session state did not match {self.expect!r}",
            )
        return ProbeResult(ok=True)

    def _default_runner(self, argv: Sequence[str]) -> tuple[int, str]:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        return completed.returncode, completed.stdout


@dataclass(frozen=True)
class DeclaredUnavailableProbe:
    """For providers with no independently observed working setup in this lab."""

    reason: str

    def __call__(self, profile: AgentProfile) -> ProbeResult:
        return ProbeResult(ok=False, reason=self.reason)


@dataclass(frozen=True)
class PreflightDecision:
    """Fail-closed dispatch decision, distinct from any trial outcome.

    ``stop`` means the trial must not start; a stopped preflight can never be
    recorded as reward zero because no trial exists to score.
    """

    proceed: bool
    profile_digest: str
    reason: str | None = None
    expires_at: datetime | None = None


def preflight(profile: AgentProfile, probe: ProbeFn | None) -> PreflightDecision:
    if profile.auth_mode == "none":
        return PreflightDecision(proceed=True, profile_digest=profile.digest)
    if probe is None:
        return PreflightDecision(
            proceed=False,
            profile_digest=profile.digest,
            reason="no credential probe wired for a billable profile (fail closed)",
        )
    result = probe(profile)
    if not result.ok:
        return PreflightDecision(
            proceed=False,
            profile_digest=profile.digest,
            reason=result.reason or "credential unavailable",
            expires_at=result.expires_at,
        )
    return PreflightDecision(
        proceed=True, profile_digest=profile.digest, expires_at=result.expires_at
    )


def scrub_environment(environment: Mapping[str, str], allowlist: frozenset[str]) -> dict[str, str]:
    """Allowlist filter that additionally refuses key-shaped names outright.

    Even a key-shaped variable added to the allowlist by mistake is dropped:
    belt and suspenders for the subscriptions-only rule.
    """
    clean: dict[str, str] = {}
    for key, value in environment.items():
        if key not in allowlist:
            continue
        upper = key.upper()
        if any(marker in upper for marker in _FORBIDDEN_KEY_MARKERS):
            continue
        clean[key] = value
    return clean


_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./_-]*$")


def validate_model_pin(profile: AgentProfile, model: str | None) -> None:
    """A run's model must match its profile's pin exactly (or inherit it)."""
    if profile.model is None:
        if model is not None:
            raise ValueError(f"control profile {profile.profile_id} takes no model")
        return
    if model is not None and model != profile.model:
        raise ValueError(
            f"model {model!r} does not match profile {profile.profile_id} "
            f"pin {profile.model!r}; change profiles, not pins"
        )
    if not _MODEL_RE.fullmatch(profile.model):
        raise ValueError(f"malformed model pin {profile.model!r}")


# ---------------------------------------------------------------------------
# Built-in profiles: only observed facts are marked verified.
# ---------------------------------------------------------------------------


def builtin_profiles() -> dict[str, AgentProfile]:
    return {
        p.profile_id: p
        for p in (
            AgentProfile(
                profile_id="oracle",
                adapter="oracle",
                auth_mode="none",
                verified_facts=("2026-08: oracle controls pass locally across the canary suite",),
            ),
            AgentProfile(
                profile_id="nop",
                adapter="nop",
                auth_mode="none",
                verified_facts=("2026-08: nop controls score 0.0 locally",),
            ),
            AgentProfile(
                profile_id="codex-gpt-5.6-terra",
                adapter="codex",
                model="gpt-5.6-terra",
                auth_mode="subscription-auth-file",
                secret_source="file:.codex/auth.json",
                required_files=(".codex/auth.json",),
                verified_facts=(
                    "2026-08-06: successful harbor-practice codex run "
                    "(transaction-reconciliation) recorded this exact model",
                ),
            ),
            AgentProfile(
                profile_id="claude-code-fable-5",
                adapter="claude-code",
                model="anthropic/claude-fable-5",
                auth_mode="subscription-keychain",
                secret_source="keychain:harbor-practice-claude-oauth",
                verified_facts=(),  # model string follows Harbor convention; unproven here
            ),
            AgentProfile(
                profile_id="gemini-cli-declared",
                adapter="gemini-cli",
                model="gemini/declared-unproven",
                auth_mode="subscription-auth-file",
                secret_source="file:.gemini/oauth_creds.json",
                verified_facts=(),  # declared only; no observed run in this lab
            ),
            AgentProfile(
                profile_id="grok-cli-declared",
                adapter="grok-cli",
                model="grok/declared-unproven",
                auth_mode="subscription-auth-file",
                secret_source="file:.grok/auth.json",
                verified_facts=(),  # declared only; no observed run in this lab
            ),
            AgentProfile(
                profile_id="mini-swe-agent-deepseek-v4-flash",
                adapter="mini-swe-agent",
                model="deepseek/deepseek-v4-flash",
                auth_mode="api-key-environment",
                secret_source="env:DEEPSEEK_API_KEY,MSWEA_API_KEY",
                limits=ProfileLimits(
                    max_timeout_seconds=600,
                    max_attempts=1,
                    max_concurrency=1,
                ),
                verified_facts=(
                    "2026-08: credential-free Harbor install smoke completed "
                    "with zero model trials",
                ),
            ),
            # Cursor lane. Verified in this lab on 2026-08-19: `cursor-agent status`
            # reported a live session and `cursor-agent -f -p …` returned the exact
            # requested string, so this lane is observed working rather than declared.
            # Peter's default is grok-4.6 **high**, explicitly not the -fast variant.
            AgentProfile(
                profile_id="cursor-grok-4.6-high",
                adapter="cursor-cli",
                model="cursor-grok-4.6-high",
                auth_mode="subscription-cli-session",
                secret_source="cli:cursor-agent status",
                verified_facts=(
                    "2026-08-19: `cursor-agent status` reported "
                    "'Logged in as p.makhnatch@gmail.com'",
                    "2026-08-19: `cursor-agent -f -p` returned the exact requested "
                    "marker, so the non-interactive lane works with the trust flag",
                    "2026-08-19: `cursor-agent models` listed 204 pinned models "
                    "including cursor-grok-4.6-high",
                ),
            ),
            AgentProfile(
                profile_id="cursor-grok-4.5-high",
                adapter="cursor-cli",
                model="cursor-grok-4.5-high",
                auth_mode="subscription-cli-session",
                secret_source="cli:cursor-agent status",
                verified_facts=(
                    "2026-08-19: listed by `cursor-agent models` on the same session "
                    "as cursor-grok-4.6-high",
                ),
            ),
            AgentProfile(
                profile_id="cursor-claude-opus-5-thinking-high",
                adapter="cursor-cli",
                model="claude-opus-5-thinking-high",
                auth_mode="subscription-cli-session",
                secret_source="cli:cursor-agent status",
                verified_facts=(
                    "2026-08-19: listed by `cursor-agent models`; reaches Claude "
                    "without the unprovisioned claude-code keychain item",
                ),
            ),
            AgentProfile(
                profile_id="cursor-gemini-3.7-flash-high",
                adapter="cursor-cli",
                model="gemini-3.7-flash-high",
                auth_mode="subscription-cli-session",
                secret_source="cli:cursor-agent status",
                verified_facts=(
                    "2026-08-19: listed by `cursor-agent models`; reaches Gemini "
                    "without gemini-cli, which is IneligibleTier for individuals",
                ),
            ),
            # Antigravity lane (AGY). Verified in this lab on 2026-08-19: `agy models`
            # reported 14 available models and `agy -p …` returned the exact
            # requested string, so this lane is observed working headlessly.
            # Peter's preferred model on this lane is Gemini 3.7 Flash.
            AgentProfile(
                profile_id="antigravity-gemini-3.7-flash-high",
                adapter="antigravity-cli",
                model="gemini-3.7-flash-high",
                auth_mode="subscription-cli-session",
                secret_source="cli:agy models",
                verified_facts=(
                    "2026-08-19: `agy -p 'Reply with exactly: AGY_LANE_OK'` returned "
                    "'AGY_LANE_OK' headlessly",
                    "2026-08-19: `agy models` listed 14 available models "
                    "including gemini-3.7-flash-high",
                    "2026-08-19: `agy --model gemini-3.7-flash-high -p` executed "
                    "successfully headlessly",
                ),
            ),
            AgentProfile(
                profile_id="antigravity-gemini-3.7-flash-medium",
                adapter="antigravity-cli",
                model="gemini-3.7-flash-medium",
                auth_mode="subscription-cli-session",
                secret_source="cli:agy models",
                verified_facts=(
                    "2026-08-19: `agy --model gemini-3.7-flash-medium -p` executed "
                    "successfully headlessly",
                ),
            ),
            AgentProfile(
                profile_id="antigravity-gemini-3.7-flash-low",
                adapter="antigravity-cli",
                model="gemini-3.7-flash-low",
                auth_mode="subscription-cli-session",
                secret_source="cli:agy models",
                verified_facts=(
                    "2026-08-19: listed by `agy models` on the same session "
                    "as gemini-3.7-flash-high",
                ),
            ),
            AgentProfile(
                profile_id="antigravity-gemini-3.1-pro-high",
                adapter="antigravity-cli",
                model="gemini-3.1-pro-high",
                auth_mode="subscription-cli-session",
                secret_source="cli:agy models",
                verified_facts=(
                    "2026-08-19: `agy --model gemini-3.1-pro-high -p` executed "
                    "successfully headlessly",
                ),
            ),
            AgentProfile(
                profile_id="antigravity-claude-sonnet-4-6",
                adapter="antigravity-cli",
                model="claude-sonnet-4-6",
                auth_mode="subscription-cli-session",
                secret_source="cli:agy models",
                verified_facts=(
                    "2026-08-19: listed by `agy models`; reaches Claude Sonnet 4.6 "
                    "via Google AI Pro Antigravity subscription",
                ),
            ),
        )
    }


#: Profiles that must not dispatch until a run in THIS lab proves them.
#: claude-code is NOT here: its keychain probe is real (observed integration);
#: what is unproven is its model string, which the qualification ladder
#: expresses as "credential-ready but never smoke-passed".
DECLARED_UNAVAILABLE: frozenset[str] = frozenset({"gemini-cli-declared", "grok-cli-declared"})


def default_probe_for(
    profile: AgentProfile,
    *,
    home: Path,
    security_runner: SecurityRunner,
    keychain_account: str,
    clock: Clock = _utc_now,
    environment: Mapping[str, str] | None = None,
    cli_runner: Callable[[Sequence[str]], tuple[int, str]] | None = None,
) -> ProbeFn | None:
    """Wire the standard probe for a profile through injected seams only."""
    if profile.auth_mode == "none":
        return None
    if profile.profile_id in DECLARED_UNAVAILABLE and not profile.verified_facts:
        return DeclaredUnavailableProbe(
            reason=f"{profile.profile_id} is declared but not independently proven in this lab"
        )
    if profile.auth_mode == "api-key-environment":
        source = (profile.secret_source or "env:").removeprefix("env:")
        names = tuple(name for name in source.split(",") if name)
        return EnvironmentPresenceProbe(
            environment=os.environ if environment is None else environment,
            names=names,
        )
    if profile.auth_mode == "subscription-keychain":
        service = (profile.secret_source or "keychain:")[len("keychain:") :]
        return KeychainProbe(
            security_runner=security_runner, service=service, account=keychain_account
        )
    if profile.auth_mode == "subscription-cli-session":
        command = (profile.secret_source or "cli:")[len("cli:") :].split()
        if not command:
            return DeclaredUnavailableProbe(
                reason=f"{profile.profile_id} declares cli-session auth with no command"
            )
        expect = "gemini" if command[0] == "agy" else "logged in"
        return CliSessionProbe(argv=tuple(command), expect=expect, runner=cli_runner)
    relative = (profile.secret_source or "file:")[len("file:") :]
    return AuthFileProbe(home=home, relative_path=relative, clock=clock)


# ---------------------------------------------------------------------------
# 8-Gate Qualification Ladder & Deterministic Blocker Control Plane
# ---------------------------------------------------------------------------

GATE_DECLARED = "declared"
GATE_INSTALLED = "installed"
GATE_HOST_CREDENTIAL = "host_credential"
GATE_HARBOR_TRANSPORT = "harbor_transport"
GATE_ENVIRONMENT_NETWORK = "environment_network"
GATE_STRUCTURED_TRAJECTORY = "structured_trajectory"
GATE_SMOKE = "smoke"
GATE_CANARY = "canary"

ALL_READINESS_GATES: tuple[str, ...] = (
    GATE_DECLARED,
    GATE_INSTALLED,
    GATE_HOST_CREDENTIAL,
    GATE_HARBOR_TRANSPORT,
    GATE_ENVIRONMENT_NETWORK,
    GATE_STRUCTURED_TRAJECTORY,
    GATE_SMOKE,
    GATE_CANARY,
)

ADAPTER_CLI_BINARIES: dict[str, str] = {
    "antigravity-cli": "agy",
    "cursor-cli": "cursor-agent",
    "codex": "codex",
    "claude-code": "claude",
    "gemini-cli": "gemini",
    "grok-cli": "grok",
}


def check_installed(
    profile: AgentProfile,
    *,
    is_installed_fn: Callable[[str], bool] | None = None,
) -> tuple[bool, str | None]:
    """Check whether adapter host binary or execution module is installed."""
    if profile.adapter in CONTROL_ADAPTERS:
        return True, None
    binary = ADAPTER_CLI_BINARIES.get(profile.adapter)
    if binary is not None:
        installed = (
            is_installed_fn(binary) if is_installed_fn is not None else bool(shutil.which(binary))
        )
        if not installed:
            return (
                False,
                f"CLI executable '{binary}' for adapter '{profile.adapter}' not found on host",
            )
        return True, None
    return True, None


def check_harbor_transport(
    profile: AgentProfile,
    *,
    host_credential_ok: bool,
) -> tuple[bool, str | None, str | None]:
    """Check whether host credentials can be securely transported into Harbor runner."""
    if profile.adapter in CONTROL_ADAPTERS:
        return True, None, None

    if profile.profile_id in DECLARED_UNAVAILABLE and not profile.verified_facts:
        return (
            False,
            f"{profile.profile_id} is declared but not independently proven in this lab",
            "Declared profiles are not runnable until proven in this lab",
        )

    if profile.adapter == "cursor-cli":
        # Host subscription session is active, but Harbor runner in Docker requires CURSOR_API_KEY
        # which is unavailable for subscription sessions.
        return (
            False,
            "Harbor runner requires CURSOR_API_KEY; host cursor-agent subscription session is not transported into Docker",
            "Cursor subscription cannot be mounted into Harbor Docker runner; use Antigravity or Codex lane for container execution",
        )

    if not host_credential_ok:
        return False, "Host credential unavailable or expired; transport cannot proceed", None

    if profile.adapter == "antigravity-cli":
        return True, None, None

    if profile.adapter == "codex":
        return True, None, None

    if profile.adapter == "mini-swe-agent":
        return True, None, None

    if profile.adapter == "claude-code":
        return True, None, None

    return True, None, None


def check_environment_network(
    *,
    docker_checker: Callable[[], tuple[bool, str]] | None = None,
) -> tuple[bool, str | None]:
    """Check container runtime and environment reachability."""
    if docker_checker is not None:
        ok, detail = docker_checker()
        return ok, (None if ok else detail)
    if not shutil.which("docker"):
        return False, "docker executable not found in PATH"
    try:
        completed = subprocess.run(
            ["docker", "version", "--format", "client={{.Client.Version}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if completed.returncode != 0:
            return False, "Docker daemon unreachable"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Docker check failed: {type(exc).__name__}"
    return True, None


def check_structured_trajectory(profile: AgentProfile) -> tuple[bool, str | None]:
    """Check whether adapter produces structured ATIF trajectory stream events."""
    if profile.adapter in CONTROL_ADAPTERS:
        return True, None
    structured_adapters = frozenset({"codex", "antigravity-cli", "mini-swe-agent", "oracle", "nop"})
    if profile.adapter in structured_adapters:
        return True, None
    return (
        False,
        f"Adapter '{profile.adapter}' lacks structured ATIF stream capture; only raw stdout recorded",
    )


def readiness_record_path(profile_id: str, *, root: Path) -> Path:
    return root / "research/evidence/readiness" / f"{profile_id}.json"


def load_readiness_record(profile_id: str, *, root: Path) -> AgentReadinessRecord | None:
    from evallab.schemas import AgentReadinessRecord

    path = readiness_record_path(profile_id, root=root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return AgentReadinessRecord.model_validate(data)
    except (OSError, ValueError):
        return None


def save_readiness_record(record: AgentReadinessRecord, *, root: Path) -> Path:
    path = readiness_record_path(record.profile_id, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    path.write_text(content, encoding="utf-8")
    return path


def compute_qualification_digest(
    smoke_records: Sequence[AgentSmokeRecord],
) -> str:
    canonical = json.dumps(
        [record.model_dump(mode="json") for record in smoke_records],
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def evaluate_profile_readiness(
    profile: AgentProfile,
    *,
    root: Path | None = None,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
    security_runner: SecurityRunner | None = None,
    keychain_account: str | None = None,
    cli_runner: Callable[[Sequence[str]], tuple[int, str]] | None = None,
    is_installed_fn: Callable[[str], bool] | None = None,
    docker_checker: Callable[[], tuple[bool, str]] | None = None,
    clock: Clock = _utc_now,
    persisted_record: AgentReadinessRecord | None = None,
) -> AgentReadinessRecord:
    """Evaluate the 8-gate qualification ladder and compute first deterministic blocker."""
    from evallab.schemas import (
        AgentBlocker,
        AgentGateEvaluations,
        AgentQualificationDigest,
        AgentReadinessRecord,
        GateStatus,
    )

    repo_root = root or Path.cwd()
    env = os.environ if environment is None else environment
    effective_home = home or Path.home()
    sec_runner = security_runner or default_security_runner
    kc_account = keychain_account if keychain_account is not None else env.get("USER", "")

    # Load persisted record if available
    saved_record = (
        persisted_record
        if persisted_record is not None
        else load_readiness_record(profile.profile_id, root=repo_root)
    )

    gates: dict[str, GateStatus] = {g: "untested" for g in ALL_READINESS_GATES}
    active_blocker: AgentBlocker | None = None

    # Gate 1: Declared
    declared_ok = True
    declared_reason: str | None = None
    if not profile.profile_id:
        declared_ok = False
        declared_reason = "Profile ID is empty"
    elif profile.adapter not in CONTROL_ADAPTERS and not profile.model:
        declared_ok = False
        declared_reason = "Billable profile requires an exact model pin"
    elif profile.adapter not in CONTROL_ADAPTERS and profile.secret_source is None:
        declared_ok = False
        declared_reason = "Billable profile must identify its secret source"

    if declared_ok:
        gates[GATE_DECLARED] = "pass"
    else:
        gates[GATE_DECLARED] = "fail"
        active_blocker = AgentBlocker(
            gate=GATE_DECLARED,
            reason=declared_reason or "Profile declaration invalid",
        )

    # Gate 2: Installed
    if active_blocker is None:
        installed_ok, installed_reason = check_installed(profile, is_installed_fn=is_installed_fn)
        if installed_ok:
            gates[GATE_INSTALLED] = "pass"
        else:
            gates[GATE_INSTALLED] = "fail"
            active_blocker = AgentBlocker(
                gate=GATE_INSTALLED,
                reason=installed_reason or f"Adapter '{profile.adapter}' executable not found",
                remediation=f"Install CLI for adapter '{profile.adapter}' or ensure it is in PATH",
            )
    else:
        gates[GATE_INSTALLED] = "blocked"

    # Gate 3: Host Credential
    if active_blocker is None:
        if profile.auth_mode == "none":
            gates[GATE_HOST_CREDENTIAL] = "pass"
        else:
            probe = default_probe_for(
                profile,
                home=effective_home,
                security_runner=sec_runner,
                keychain_account=kc_account,
                clock=clock,
                environment=env,
                cli_runner=cli_runner,
            )
            if probe is None:
                gates[GATE_HOST_CREDENTIAL] = "fail"
                active_blocker = AgentBlocker(
                    gate=GATE_HOST_CREDENTIAL,
                    reason="No credential probe wired for billable profile",
                )
            else:
                probe_res = probe(profile)
                if probe_res.ok:
                    gates[GATE_HOST_CREDENTIAL] = "pass"
                else:
                    gates[GATE_HOST_CREDENTIAL] = "fail"
                    active_blocker = AgentBlocker(
                        gate=GATE_HOST_CREDENTIAL,
                        reason=probe_res.reason or "Credential probe failed",
                        remediation=f"Configure credentials for {profile.secret_source}",
                    )
    else:
        gates[GATE_HOST_CREDENTIAL] = "blocked"

    # Gate 4: Harbor Transport
    if active_blocker is None:
        transport_ok, transport_reason, remediation = check_harbor_transport(
            profile,
            host_credential_ok=(gates[GATE_HOST_CREDENTIAL] == "pass"),
        )
        if transport_ok:
            gates[GATE_HARBOR_TRANSPORT] = "pass"
        else:
            gates[GATE_HARBOR_TRANSPORT] = "fail"
            active_blocker = AgentBlocker(
                gate=GATE_HARBOR_TRANSPORT,
                reason=transport_reason or "Credential transport into Harbor runner failed",
                remediation=remediation,
            )
    else:
        gates[GATE_HARBOR_TRANSPORT] = "blocked"

    # Gate 5: Environment / Network
    if active_blocker is None:
        env_ok, env_reason = check_environment_network(docker_checker=docker_checker)
        if env_ok:
            gates[GATE_ENVIRONMENT_NETWORK] = "pass"
        else:
            gates[GATE_ENVIRONMENT_NETWORK] = "fail"
            active_blocker = AgentBlocker(
                gate=GATE_ENVIRONMENT_NETWORK,
                reason=env_reason or "Docker daemon or container environment unreachable",
                remediation="Ensure Docker daemon is running and accessible",
            )
    else:
        gates[GATE_ENVIRONMENT_NETWORK] = "blocked"

    # Gate 6: Structured Trajectory
    if active_blocker is None:
        traj_ok, traj_reason = check_structured_trajectory(profile)
        if traj_ok:
            gates[GATE_STRUCTURED_TRAJECTORY] = "pass"
        else:
            gates[GATE_STRUCTURED_TRAJECTORY] = "fail"
            active_blocker = AgentBlocker(
                gate=GATE_STRUCTURED_TRAJECTORY,
                reason=traj_reason
                or f"Adapter '{profile.adapter}' lacks structured ATIF trajectory capture",
                remediation=f"Implement ATIF stream capture for adapter '{profile.adapter}'",
            )
    else:
        gates[GATE_STRUCTURED_TRAJECTORY] = "blocked"

    # Gate 7: Smoke
    last_smoke: AgentSmokeRecord | None = saved_record.last_smoke if saved_record else None
    if active_blocker is None:
        if (
            last_smoke is not None
            and last_smoke.reward >= 1.0
            and last_smoke.profile_digest == profile.digest
        ):
            gates[GATE_SMOKE] = "pass"
        else:
            gates[GATE_SMOKE] = "blocked"
            active_blocker = AgentBlocker(
                gate=GATE_SMOKE,
                reason=f"No verified smoke run on record; run 'evallab agents smoke {profile.profile_id}'",
                remediation=f"Run 'evallab agents smoke {profile.profile_id}'",
            )
    else:
        gates[GATE_SMOKE] = "blocked"

    # Gate 8: Canary
    qualification: AgentQualificationDigest | None = (
        saved_record.qualification if saved_record else None
    )
    if active_blocker is None:
        if (
            qualification is not None
            and qualification.success_count >= qualification.repeats
            and qualification.repeats >= 3
            and qualification.profile_digest == profile.digest
        ):
            gates[GATE_CANARY] = "pass"
        else:
            gates[GATE_CANARY] = "blocked"
            active_blocker = AgentBlocker(
                gate=GATE_CANARY,
                reason=f"Profile not qualified across repeated canary controls; run 'evallab agents qualify {profile.profile_id}'",
                remediation=f"Run 'evallab agents qualify {profile.profile_id}'",
            )
    else:
        gates[GATE_CANARY] = "blocked"

    # Compute Ladder State
    if gates[GATE_CANARY] == "pass":
        state = ProfileState.CANARY_QUALIFIED
    elif gates[GATE_SMOKE] == "pass":
        state = ProfileState.SMOKE_PASSED
    elif (
        gates[GATE_DECLARED] == "pass"
        and gates[GATE_INSTALLED] == "pass"
        and gates[GATE_HOST_CREDENTIAL] == "pass"
        and gates[GATE_HARBOR_TRANSPORT] == "pass"
        and gates[GATE_ENVIRONMENT_NETWORK] == "pass"
        and gates[GATE_STRUCTURED_TRAJECTORY] == "pass"
    ):
        state = ProfileState.CREDENTIAL_READY
    elif gates[GATE_DECLARED] == "pass" and gates[GATE_INSTALLED] == "pass":
        state = ProfileState.INSTALLED
    else:
        state = ProfileState.DECLARED

    return AgentReadinessRecord(
        schema_version=1,
        profile_id=profile.profile_id,
        adapter=profile.adapter,
        model=profile.model,
        profile_digest=profile.digest,
        state=state.value,
        gates=AgentGateEvaluations(**gates),
        blocker=active_blocker,
        last_smoke=last_smoke,
        qualification=qualification,
        updated_at=datetime.now(UTC),
    )
