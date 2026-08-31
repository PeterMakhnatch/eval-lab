"""Platform-aware Harbor Docker network policy for the execution staging adapter.

Harbor's Docker provider enforces ``no-network`` and ``allowlist`` only on Linux
Docker hosts. Docker Desktop on macOS (and other non-Linux providers) correctly
rejects those policies with:

    ValueError: network_mode='no-network' is not supported by
    EnvironmentType.DOCKER environment.

The execution staging adapter in ``runner.py`` uses this module to derive an
effective network mode for the current host, preserving the requested canonical
policy in a temporary execution copy and returning a ``NetworkAdaptation`` record
the runner can persist in that copy's ``run_manifest.json``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import platform
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

NetworkMode = Literal["public", "no-network", "allowlist"]

ADAPTER_VERSION = "1.0.0"


def adapter_digest() -> str:
    """SHA-256 digest of this adapter module and the version string."""
    source = Path(__file__).resolve().read_bytes()
    return f"sha256:{hashlib.sha256(ADAPTER_VERSION.encode() + b'\n' + source).hexdigest()}"


@dataclass(frozen=True)
class HarborNetworkPolicy:
    """Network policy the current host can actually run.

    ``network_isolation_enforced`` is ``True`` only when the host platform is
    known to support Harbor's no-network Docker enforcement (Linux). On other
    platforms the policy is ``public`` and no isolation is claimed.
    """

    network_mode: NetworkMode
    network_isolation_enforced: bool
    network_isolation_reason: str | None


def host_harbor_network_policy() -> HarborNetworkPolicy:
    """Return the network policy appropriate for the current host.

    Linux: ``no-network`` with isolation enforced. macOS and other unsupported
    platforms: ``public`` with isolation unenforced and a documented reason.
    """
    system = platform.system()
    if system == "Linux":
        return HarborNetworkPolicy(
            network_mode="no-network",
            network_isolation_enforced=True,
            network_isolation_reason=None,
        )
    if system == "Darwin":
        return HarborNetworkPolicy(
            network_mode="public",
            network_isolation_enforced=False,
            network_isolation_reason="darwin-docker-cannot-enforce-no-network",
        )
    return HarborNetworkPolicy(
        network_mode="public",
        network_isolation_enforced=False,
        network_isolation_reason=f"{system.lower()}-docker-cannot-enforce-no-network",
    )


@dataclass(frozen=True)
class NetworkAdaptation:
    """Record of a canonical network policy adapted to a host's capabilities."""

    requested_agent_network: str
    effective_agent_network: str
    requested_verifier_network: str
    effective_verifier_network: str
    requested_verifier_phase_network: str | None
    effective_verifier_phase_network: str | None
    network_isolation_enforced: bool
    network_isolation_reason: str | None
    adapter_version: str
    adapter_digest: str
    requested_agent_allowed_hosts: tuple[str, ...] = ()
    agent_allowlist_enforced: bool = False


def _canonical_networks(
    config: dict[str, Any],
) -> tuple[str, str, str | None]:
    """Return (agent, verifier_baseline, verifier_phase) declared in task.toml.

    The verifier baseline follows Harbor 0.21.0 resolution:
    ``[verifier.environment].network_mode`` if present, otherwise the agent's
    ``[environment].network_mode``. The phase override is ``[verifier].network_mode``
    if present, otherwise the verifier baseline.
    """
    environment = config.get("environment")
    agent: str = (
        environment.get("network_mode", "public") if isinstance(environment, dict) else "public"
    )

    verifier = config.get("verifier")
    verifier_table = verifier if isinstance(verifier, dict) else {}
    verifier_env = verifier_table.get("environment")
    verifier_env_table = verifier_env if isinstance(verifier_env, dict) else {}
    declared = verifier_env_table.get("network_mode")
    verifier_baseline = declared if isinstance(declared, str) else agent

    phase = verifier_table.get("network_mode")
    verifier_phase = phase if isinstance(phase, str) else None

    return agent, verifier_baseline, verifier_phase


def _effective_network(requested: str, host: HarborNetworkPolicy) -> str:
    """Downgrade no-network to the host's supported mode when necessary."""
    if requested == "no-network" and host.network_mode != "no-network":
        return host.network_mode
    return requested


def _section_range(text: str, header: str) -> tuple[int, int] | None:
    pattern = re.compile(rf"^\[{re.escape(header)}\]$", re.MULTILINE)
    match = pattern.search(text)
    if match is None:
        return None
    start = match.start()
    next_match = re.compile(r"^\[", re.MULTILINE).search(text, match.end())
    end = next_match.start() if next_match is not None else len(text)
    return start, end


def with_agent_network_allowlist(
    task_toml_text: str,
    allowed_hosts: tuple[str, ...],
) -> str:
    """Add an explicit agent-phase allowlist without changing the source package."""
    if not allowed_hosts:
        return task_toml_text
    if any(not re.fullmatch(r"[A-Za-z0-9.-]+", host) for host in allowed_hosts):
        raise ValueError("agent allowed_hosts contains an invalid hostname")
    config = tomllib.loads(task_toml_text)
    agent = config.get("agent")
    if not isinstance(agent, dict):
        raise ValueError("[agent] not found in task.toml")
    existing_mode = agent.get("network_mode")
    existing_hosts = agent.get("allowed_hosts")
    if existing_mode is not None or existing_hosts is not None:
        if existing_mode == "allowlist" and existing_hosts == list(allowed_hosts):
            return task_toml_text
        raise ValueError("task already declares a different agent network policy")
    found = _section_range(task_toml_text, "agent")
    if found is None:
        raise ValueError("[agent] not found in task.toml")
    start, end = found
    header_end = task_toml_text.find("\n", start, end)
    if header_end < 0:
        header_end = end
        separator = "\n"
    else:
        header_end += 1
        separator = ""
    hosts = ", ".join(json.dumps(host) for host in allowed_hosts)
    insertion = separator + 'network_mode = "allowlist"\n' + f"allowed_hosts = [{hosts}]\n"
    updated = task_toml_text[:header_end] + insertion + task_toml_text[header_end:]
    updated_agent = tomllib.loads(updated).get("agent")
    if not isinstance(updated_agent, dict):
        raise ValueError("agent network policy did not parse")
    if updated_agent.get("network_mode") != "allowlist":
        raise ValueError("agent network_mode was not set")
    if updated_agent.get("allowed_hosts") != list(allowed_hosts):
        raise ValueError("agent allowed_hosts was not set")
    return updated


def _replace_in_section(
    text: str,
    section: str,
    key: str,
    new_value: str,
) -> str:
    """Replace the value of exactly one ``key = ...`` line inside ``[section]``.

    Preserves leading whitespace, the quoting style, and any inline comment or
    trailing whitespace after the original value. Raises ``ValueError`` when the
    section or key is missing, or when the key appears more than once.
    """
    found = _section_range(text, section)
    if found is None:
        raise ValueError(f"[{section}] not found in task.toml")
    start, end = found
    section_text = text[start:end]

    key_pattern = re.compile(
        f"^(\\s*{re.escape(key)}\\s*=\\s*)([\"'])(.+?)\\2(.*)$",
        re.MULTILINE,
    )
    matches = list(key_pattern.finditer(section_text))
    if len(matches) > 1:
        raise ValueError(f"ambiguous {key!r} in [{section}]")
    if not matches:
        raise ValueError(f"{key!r} not found in [{section}]")

    match = matches[0]
    line_start = start + match.start()
    line_end = start + match.end()
    quote = match.group(2)
    new_line = f"{match.group(1)}{quote}{new_value}{quote}{match.group(4)}"
    return text[:line_start] + new_line + text[line_end:]


def _set_network_sentinel(config: dict[str, Any], sentinel: str) -> dict[str, Any]:
    """Set every network_mode value to a sentinel for comparison."""
    config = copy.deepcopy(config)
    if isinstance(config.get("environment"), dict):
        config["environment"]["network_mode"] = sentinel
    if isinstance(config.get("verifier"), dict):
        v = config["verifier"]
        v["network_mode"] = sentinel
        if isinstance(v.get("environment"), dict):
            v["environment"]["network_mode"] = sentinel
    return config


def adapt_task_toml_for_host(
    task_toml_text: str,
    *,
    host_policy: HarborNetworkPolicy | None = None,
    agent_allowed_hosts: tuple[str, ...] = (),
) -> tuple[str, NetworkAdaptation | None]:
    """Derive an executable network policy for the current Docker host.

    A requested agent allowlist is applied only where Harbor Docker can enforce
    it. Unsupported hosts record an explicit allowlist-to-public adaptation
    instead of passing an invalid policy to Harbor.
    """
    config = tomllib.loads(task_toml_text)
    host = host_policy if host_policy is not None else host_harbor_network_policy()
    requested_environment, requested_verifier, requested_phase = _canonical_networks(config)
    effective_environment = _effective_network(requested_environment, host)
    effective_verifier = _effective_network(requested_verifier, host)
    effective_phase = (
        _effective_network(requested_phase, host) if requested_phase is not None else None
    )
    requested_agent = "allowlist" if agent_allowed_hosts else requested_environment
    allowlist_enforced = bool(agent_allowed_hosts and host.network_isolation_enforced)
    effective_agent = "allowlist" if allowlist_enforced else effective_environment

    all_effective_no_network = (
        effective_agent == "no-network"
        and effective_verifier == "no-network"
        and (effective_phase is None or effective_phase == "no-network")
    )
    isolation_enforced = all_effective_no_network and host.network_isolation_enforced
    isolation_reason = None if isolation_enforced else host.network_isolation_reason

    if (
        not agent_allowed_hosts
        and effective_environment == requested_environment
        and effective_verifier == requested_verifier
        and effective_phase == requested_phase
    ):
        return task_toml_text, None

    new_text = task_toml_text
    new_text = _replace_in_section(
        new_text,
        "environment",
        "network_mode",
        effective_environment,
    )
    if _section_range(new_text, "verifier.environment") is not None:
        new_text = _replace_in_section(
            new_text,
            "verifier.environment",
            "network_mode",
            effective_verifier,
        )
    if requested_phase is not None and effective_phase is not None:
        new_text = _replace_in_section(
            new_text,
            "verifier",
            "network_mode",
            effective_phase,
        )
    new_config = tomllib.loads(new_text)

    if _set_network_sentinel(config, "<adapted>") != _set_network_sentinel(new_config, "<adapted>"):
        raise ValueError("adaptation changed non-network parsed fields")
    if new_config["environment"]["network_mode"] != effective_environment:
        raise ValueError("environment.network_mode not set to effective value")
    if (
        isinstance(new_config.get("verifier"), dict)
        and isinstance(new_config["verifier"].get("environment"), dict)
        and new_config["verifier"]["environment"]["network_mode"] != effective_verifier
    ):
        raise ValueError("verifier.environment.network_mode not set to effective value")
    if (
        requested_phase is not None
        and new_config.get("verifier", {}).get("network_mode") != effective_phase
    ):
        raise ValueError("verifier.network_mode not set to effective value")

    if allowlist_enforced:
        new_text = with_agent_network_allowlist(new_text, agent_allowed_hosts)

    adaptation = NetworkAdaptation(
        requested_agent_network=requested_agent,
        effective_agent_network=effective_agent,
        requested_verifier_network=requested_verifier,
        effective_verifier_network=effective_verifier,
        requested_verifier_phase_network=requested_phase,
        effective_verifier_phase_network=effective_phase,
        network_isolation_enforced=isolation_enforced,
        network_isolation_reason=isolation_reason,
        adapter_version=ADAPTER_VERSION,
        adapter_digest=adapter_digest(),
        requested_agent_allowed_hosts=agent_allowed_hosts,
        agent_allowlist_enforced=allowlist_enforced,
    )
    return new_text, adaptation
