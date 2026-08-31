"""Credential availability probes shared by the doctor and executor.

M003: this module is a compatibility layer over :mod:`evallab.profiles`,
which owns agent identity, auth modes, and probe seams. Public compatibility
names remain because queue/automation/doctor depend on them.

Subscription credentials remain the default. The DeepSeek mini-swe-agent lane
is the only API-key exception: probes report presence only, runner transport is
agent-scoped, and values never enter logs or configuration.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from evallab.profiles import (
    AuthFileProbe,
    CliSessionProbe,
    EnvironmentPresenceProbe,
    KeychainProbe,
    OpenCodeProviderAuthProbe,
    ProbeResult,
    builtin_profiles,
    default_security_runner,
)

KEYCHAIN_SERVICE = "harbor-practice-claude-oauth"

CLAUDE_OAUTH = "claude_oauth"
CODEX_AUTH = "codex_auth"
CURSOR_SESSION = "cursor_session"
ANTIGRAVITY_SESSION = "antigravity_session"
DEEPSEEK_API_CREDENTIAL = "deepseek_api_environment"
ZAI_OPENCODE_AUTH = "zai_opencode_auth_file"

# Agents whose runs require a credential. Control agents (oracle, nop) are
# deliberately absent: they must run with no credential at all.
AGENT_CREDENTIAL_REQUIREMENTS: dict[str, str] = {
    "claude-code": CLAUDE_OAUTH,
    "codex": CODEX_AUTH,
    "cursor-cli": CURSOR_SESSION,
    "antigravity-cli": ANTIGRAVITY_SESSION,
    "mini-swe-agent": DEEPSEEK_API_CREDENTIAL,
    "zai-opencode": ZAI_OPENCODE_AUTH,
}

_PROFILES = builtin_profiles()
_CLAUDE_PROFILE = _PROFILES["claude-code-fable-5"]
_CODEX_PROFILE = _PROFILES["codex-gpt-5.6-terra"]
_CURSOR_PROFILE = _PROFILES["cursor-grok-4.6-high"]
_ANTIGRAVITY_PROFILE = _PROFILES["antigravity-gemini-3.7-flash-high"]
_DEEPSEEK_PROFILE = _PROFILES["mini-swe-agent-deepseek-v4-flash"]
_ZAI_OPENCODE_PROFILE = _PROFILES["zai-opencode-glm-5.3"]


def probe_claude_keychain() -> bool:
    return probe_claude_keychain_result().ok


def probe_claude_keychain_result() -> ProbeResult:
    service = os.environ.get("HARBOR_CLAUDE_KEYCHAIN_SERVICE", KEYCHAIN_SERVICE)
    account = os.environ.get("HARBOR_CLAUDE_KEYCHAIN_ACCOUNT", os.environ.get("USER", ""))
    probe = KeychainProbe(
        security_runner=default_security_runner,
        service=service,
        account=account,
    )
    try:
        return probe(_CLAUDE_PROFILE)
    except subprocess.TimeoutExpired:
        return ProbeResult(ok=False, reason="keychain probe timed out")


def probe_codex_auth(home: Path | None = None) -> bool:
    return probe_codex_auth_result(home).ok


def probe_codex_auth_result(home: Path | None = None) -> ProbeResult:
    probe = AuthFileProbe(home=home or Path.home(), relative_path=".codex/auth.json")
    return probe(_CODEX_PROFILE)


def probe_zai_opencode_auth(home: Path | None = None) -> bool:
    return probe_zai_opencode_auth_result(home).ok


def probe_zai_opencode_auth_result(home: Path | None = None) -> ProbeResult:
    probe = OpenCodeProviderAuthProbe(
        home=home or Path.home(),
        relative_path=".local/share/opencode/auth.json",
        provider="zai-coding-plan",
    )
    return probe(_ZAI_OPENCODE_PROFILE)


def probe_cursor_session() -> bool:
    return probe_cursor_session_result().ok


def probe_cursor_session_result() -> ProbeResult:
    """Ask `cursor-agent` whether it holds a session.

    Cursor keeps its credential in an opaque internal store: `~/.cursor/` holds
    only UI config and no keychain item exists, so a file probe would report
    "available" while the session was actually expired. Asking the CLI is the
    only honest check. Exit status and a stdout marker only — never a token.
    """
    probe = CliSessionProbe(argv=("cursor-agent", "status"), expect="logged in")
    try:
        return probe(_CURSOR_PROFILE)
    except subprocess.TimeoutExpired:
        return ProbeResult(ok=False, reason="cursor session probe timed out")


def probe_antigravity_session() -> bool:
    return probe_antigravity_session_result().ok


def probe_antigravity_session_result() -> ProbeResult:
    """Ask `agy` whether it holds a session.

    Antigravity keeps its credential in the OS keyring or internal token store.
    Asking `agy models` is the honest check that an active session exists.
    Exit status and a stdout marker only — never a token.
    """
    probe = CliSessionProbe(argv=("agy", "models"), expect="gemini")
    try:
        return probe(_ANTIGRAVITY_PROFILE)
    except subprocess.TimeoutExpired:
        return ProbeResult(ok=False, reason="antigravity session probe timed out")


def probe_deepseek_api() -> bool:
    return probe_deepseek_api_result().ok


def probe_deepseek_api_result(
    environment: Mapping[str, str] | None = None,
) -> ProbeResult:
    probe = EnvironmentPresenceProbe(
        environment=os.environ if environment is None else environment,
        names=("DEEPSEEK_API_KEY", "MSWEA_API_KEY"),
    )
    return probe(_DEEPSEEK_PROFILE)


def available_credentials(home: Path | None = None) -> frozenset[str]:
    found: set[str] = set()
    if probe_claude_keychain():
        found.add(CLAUDE_OAUTH)
    if probe_codex_auth(home):
        found.add(CODEX_AUTH)
    if probe_zai_opencode_auth(home):
        found.add(ZAI_OPENCODE_AUTH)
    if probe_cursor_session():
        found.add(CURSOR_SESSION)
    if probe_antigravity_session():
        found.add(ANTIGRAVITY_SESSION)
    if probe_deepseek_api():
        found.add(DEEPSEEK_API_CREDENTIAL)
    return frozenset(found)


def missing_credential_for(agent: str, available: frozenset[str]) -> str | None:
    """Name the credential *agent* needs but which is not available, if any."""
    required = AGENT_CREDENTIAL_REQUIREMENTS.get(agent)
    if required is None or required in available:
        return None
    return required


# Default model per agent, derived from the profile registry (single source of
# truth). The codex pin is proven (2026-08-06 harbor-practice run); the
# claude-code pin follows Harbor's convention but is unverified until a smoke
# run passes — pin models explicitly in specs for comparisons.
#: The profile that supplies each adapter's default model. Explicit rather than
#: "last profile wins": several profiles share the `cursor-cli` adapter, so a
#: comprehension over the registry would let iteration order pick the default —
#: which silently chose Gemini over Peter's stated grok-4.6 default once already.
DEFAULT_PROFILE_FOR_ADAPTER: dict[str, str] = {
    "codex": "codex-gpt-5.6-terra",
    "claude-code": "claude-code-fable-5",
    "cursor-cli": "cursor-grok-4.6-high",
    "antigravity-cli": "antigravity-gemini-3.7-flash-high",
    "mini-swe-agent": "mini-swe-agent-deepseek-v4-flash",
    "zai-opencode": "zai-opencode-glm-5.3",
}

DEFAULT_AGENT_MODELS: dict[str, str] = {
    adapter: model
    for adapter, profile_id in DEFAULT_PROFILE_FOR_ADAPTER.items()
    if (model := builtin_profiles()[profile_id].model) is not None
}
