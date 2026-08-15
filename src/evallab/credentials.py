"""Credential availability probes shared by the doctor and the executor.

The lab runs several agent CLIs with different credential stores. Unattended
operation must not stop entirely because one credential is absent: a
codex-only night can proceed without the Claude subscription token, and vice
versa. The doctor reports which credentials are available; the executor defers
individual specs whose agent's credential is missing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from evallab.runner import subscription_environment

KEYCHAIN_SERVICE = "harbor-practice-claude-oauth"

CLAUDE_OAUTH = "claude_oauth"
CODEX_AUTH = "codex_auth"

# Agents whose runs require a credential. Control agents (oracle, nop) are
# deliberately absent: they must run with no credential at all.
AGENT_CREDENTIAL_REQUIREMENTS: dict[str, str] = {
    "claude-code": CLAUDE_OAUTH,
    "codex": CODEX_AUTH,
}


def probe_claude_keychain() -> bool:
    service = os.environ.get("HARBOR_CLAUDE_KEYCHAIN_SERVICE", KEYCHAIN_SERVICE)
    account = os.environ.get("HARBOR_CLAUDE_KEYCHAIN_ACCOUNT", os.environ.get("USER", ""))
    if not account:
        return False
    try:
        completed = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            env=subscription_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def probe_codex_auth(home: Path | None = None) -> bool:
    return ((home or Path.home()) / ".codex/auth.json").is_file()


def available_credentials(home: Path | None = None) -> frozenset[str]:
    found: set[str] = set()
    if probe_claude_keychain():
        found.add(CLAUDE_OAUTH)
    if probe_codex_auth(home):
        found.add(CODEX_AUTH)
    return frozenset(found)


def missing_credential_for(agent: str, available: frozenset[str]) -> str | None:
    """Name the credential *agent* needs but which is not available, if any."""
    required = AGENT_CREDENTIAL_REQUIREMENTS.get(agent)
    if required is None or required in available:
        return None
    return required


# Default model per agent when a spec does not pin one. The codex value is
# proven: it is the model recorded in the successful harbor-practice codex run
# (2026-08-06, transaction-reconciliation). The claude-code value follows
# Harbor's model-string convention but is unverified until the Claude
# credential exists — pin models explicitly in specs for comparisons.
DEFAULT_AGENT_MODELS: dict[str, str] = {
    "codex": "gpt-5.6-terra",
    "claude-code": "anthropic/claude-fable-5",
}
