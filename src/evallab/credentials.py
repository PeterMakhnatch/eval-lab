"""Credential availability probes shared by the doctor and the executor.

M003: this module is now a thin compatibility layer over
:mod:`evallab.profiles`, which owns agent identity, auth modes, and probe
seams. The public names here (``available_credentials``,
``missing_credential_for``, ``DEFAULT_AGENT_MODELS``, ``CLAUDE_OAUTH``,
``CODEX_AUTH``) keep their exact signatures because queue/automation/doctor
depend on them; new code should consume profiles directly.

Subscriptions only: nothing in this module reads, logs, or forwards an
API-key environment variable. Probes return availability and reason, never
secret material.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from evallab.profiles import (
    AuthFileProbe,
    CliSessionProbe,
    KeychainProbe,
    ProbeResult,
    builtin_profiles,
)
from evallab.runner import subscription_environment

KEYCHAIN_SERVICE = "harbor-practice-claude-oauth"

CLAUDE_OAUTH = "claude_oauth"
CODEX_AUTH = "codex_auth"
CURSOR_SESSION = "cursor_session"

# Agents whose runs require a credential. Control agents (oracle, nop) are
# deliberately absent: they must run with no credential at all.
AGENT_CREDENTIAL_REQUIREMENTS: dict[str, str] = {
    "claude-code": CLAUDE_OAUTH,
    "codex": CODEX_AUTH,
    "cursor-cli": CURSOR_SESSION,
}

_PROFILES = builtin_profiles()
_CLAUDE_PROFILE = _PROFILES["claude-code-fable-5"]
_CODEX_PROFILE = _PROFILES["codex-gpt-5.6-terra"]
_CURSOR_PROFILE = _PROFILES["cursor-grok-4.6-high"]


def _security_exit_status(args: list[str]) -> int:
    """Run /usr/bin/security for existence only; output is discarded unread.

    The ``-w`` flag (print the secret) is deliberately not used anywhere.
    """
    completed = subprocess.run(
        ["/usr/bin/security", *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        env=subscription_environment(),
    )
    return completed.returncode


def probe_claude_keychain() -> bool:
    return probe_claude_keychain_result().ok


def probe_claude_keychain_result() -> ProbeResult:
    service = os.environ.get("HARBOR_CLAUDE_KEYCHAIN_SERVICE", KEYCHAIN_SERVICE)
    account = os.environ.get("HARBOR_CLAUDE_KEYCHAIN_ACCOUNT", os.environ.get("USER", ""))
    probe = KeychainProbe(
        security_runner=_security_exit_status, service=service, account=account
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


def available_credentials(home: Path | None = None) -> frozenset[str]:
    found: set[str] = set()
    if probe_claude_keychain():
        found.add(CLAUDE_OAUTH)
    if probe_codex_auth(home):
        found.add(CODEX_AUTH)
    if probe_cursor_session():
        found.add(CURSOR_SESSION)
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
}

DEFAULT_AGENT_MODELS: dict[str, str] = {
    adapter: model
    for adapter, profile_id in DEFAULT_PROFILE_FOR_ADAPTER.items()
    if (model := builtin_profiles()[profile_id].model) is not None
}
