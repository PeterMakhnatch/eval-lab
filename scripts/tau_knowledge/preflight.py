#!/usr/bin/env python3
"""Fail-closed credential preflight for tau-Knowledge.

Tau's simulated-user runtime needs a real host ``OPENAI_API_KEY``; PinnedCodex
needs ``~/.codex/auth.json`` with ``CODEX_FORCE_AUTH_JSON=1``.  This module only
probes presence and routing; it never reads, logs, or forwards token values.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from evallab.credentials import probe_codex_auth_result

DEFAULT_LUNA_AGENT = "evallab.harbor_codex:PinnedCodex"
_PINNED_CODEX_SELECTORS = frozenset(
    {
        DEFAULT_LUNA_AGENT,
        "PinnedCodex",
        "codex",
    }
)

_ENV_VAR_PATTERN = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)\b"
)


def _default_credentials() -> dict[str, dict[str, Any]]:
    return {
        "simulated_user": {
            "consumer": "tau3-runtime",
            "required_env": ["OPENAI_API_KEY"],
            "required_by_phases": ["reference", "luna"],
            "oauth_equivalent": False,
        },
        "pinned_codex": {
            "consumer": DEFAULT_LUNA_AGENT,
            "routing_env": {"CODEX_FORCE_AUTH_JSON": "1"},
            "auth_file": "~/.codex/auth.json",
            "required_by_phases": ["luna"],
        },
    }


@dataclass(frozen=True)
class TauCredentialDecision:
    """Fail-closed credential decision, never a reward or trial outcome."""

    phase: str
    proceed: bool
    reason_code: str | None = None
    detail: str = ""
    consumers: list[dict[str, Any]] = field(default_factory=list)
    created_trial: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = "proceed" if self.proceed else "blocked"
        return payload


def _is_pinned_codex(agent: str | None) -> bool:
    """True when the agent is one of the PinnedCodex selectors."""
    if agent is None:
        return True
    return agent.strip() in _PINNED_CODEX_SELECTORS


def _simulated_user_key_present(env: Mapping[str, str]) -> bool:
    """Presence-only check for the host OpenAI key used by the simulated user."""
    value = env.get("OPENAI_API_KEY") or ""
    return value.strip() != ""


def _relevant_consumers(
    credentials: Mapping[str, Mapping[str, Any]], phase: str
) -> list[dict[str, Any]]:
    """Return the credential consumers that apply to this phase."""
    result: list[dict[str, Any]] = []
    for name, spec in credentials.items():
        if phase in spec.get("required_by_phases", []):
            entry: dict[str, Any] = {"name": name, "consumer": spec["consumer"]}
            if "required_env" in spec:
                entry["required_env"] = list(spec["required_env"])
            if "routing_env" in spec:
                entry["routing_env"] = dict(spec["routing_env"])
            if "auth_file" in spec:
                entry["auth_file"] = spec["auth_file"]
            result.append(entry)
    return result


def _configured_credentials(
    config: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if config is None:
        return _default_credentials()
    value = config.get("credentials")
    if not isinstance(value, dict):
        return _default_credentials()
    # Accept either the consumer name as key or an inner "name"; normalize to
    # the expected two consumers.
    credentials: dict[str, dict[str, Any]] = {}
    for key, spec in value.items():
        if isinstance(spec, dict):
            credentials[key] = dict(spec)
    if not credentials:
        return _default_credentials()
    return credentials


def preflight_tau_phase(
    phase: str,
    *,
    env: Mapping[str, str],
    home: Path,
    config: Mapping[str, Any] | None = None,
    agent: str | None = None,
) -> TauCredentialDecision:
    """Decide whether this tau-Knowledge phase may start.

    The decision is fail-closed: a missing simulated-user ``OPENAI_API_KEY`` or
    missing Codex ``auth.json`` blocks the trial before Harbor is invoked.
    """
    credentials = _configured_credentials(config)
    consumers = _relevant_consumers(credentials, phase)
    prefix = "Harness credential block (not a model or verifier failure): "

    simulated = credentials.get("simulated_user") or {}
    if phase in simulated.get("required_by_phases", []):
        if not _simulated_user_key_present(env):
            return TauCredentialDecision(
                phase=phase,
                proceed=False,
                reason_code="blocked:missing_openai_api_key_for_simulated_user",
                detail=(
                    prefix
                    + "The simulated-user runtime (tau3-runtime) requires a host "
                    "OPENAI_API_KEY. Codex OAuth via ~/.codex/auth.json cannot be "
                    "converted into the simulated-user OPENAI_API_KEY."
                ),
                consumers=consumers,
                created_trial=False,
            )

    pinned = credentials.get("pinned_codex") or {}
    if phase in pinned.get("required_by_phases", []) and _is_pinned_codex(agent):
        probe = probe_codex_auth_result(home=home)
        if not probe.ok:
            return TauCredentialDecision(
                phase=phase,
                proceed=False,
                reason_code="blocked:missing_codex_auth_json",
                detail=(
                    prefix
                    + f"Luna with PinnedCodex requires ~/.codex/auth.json and "
                    f"CODEX_FORCE_AUTH_JSON=1. {probe.reason or 'auth file unavailable'}. "
                    f"This is a harness credential block, not a model failure."
                ),
                consumers=consumers,
                created_trial=False,
            )

    return TauCredentialDecision(
        phase=phase,
        proceed=True,
        detail="Credential preflight passed.",
        consumers=consumers,
        created_trial=False,
    )


def build_child_env(
    phase: str,
    *,
    env: Mapping[str, str],
    home: Path,
    repo_root: Path,
    adapter_pythonpath: str | None,
    luna_agent: str | None = None,
) -> dict[str, str]:
    """Build the child environment for a tau-Knowledge phase.

    The caller's environment is forwarded intact so a real ``OPENAI_API_KEY``
    reaches Harbor for task/env interpolation.  For Luna the Codex routing flag
    is forced, and ``PYTHONPATH`` is set so the local ``evallab.harbor_codex``
    adapter can be loaded.
    """
    child = dict(env)

    if phase == "luna":
        child["CODEX_FORCE_AUTH_JSON"] = "1"
        if not child.get("LUNA_AGENT"):
            child["LUNA_AGENT"] = luna_agent or DEFAULT_LUNA_AGENT

    pythonpath_parts: list[str] = [str(Path(repo_root) / "src")]
    if adapter_pythonpath:
        pythonpath_parts.append(str(adapter_pythonpath))
    existing = child.get("PYTHONPATH", "")
    if existing:
        pythonpath_parts.append(existing)
    child["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)

    return child


def render_command(
    command: list[str],
    *,
    task_path: Path,
    env: Mapping[str, str],
) -> list[str]:
    """Render a command, replacing ``{task_path}`` and ``$VAR`` / ``${VAR}``."""
    result: list[str] = []
    for item in command:
        rendered = item.replace("{task_path}", str(task_path))

        def _replace_var(match: re.Match[str]) -> str:
            name = match.group(1) or match.group(2)
            return env.get(name, "")

        rendered = _ENV_VAR_PATTERN.sub(_replace_var, rendered)
        result.append(rendered)
    return result
