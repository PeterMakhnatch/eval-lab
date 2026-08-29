"""Deterministic, non-admitting quality workbench for Harbor task candidates.

The workbench has deliberately narrow powers:

* read a task package and pinned source metadata;
* plan and run only local ``oracle``/``nop`` Harbor controls;
* replace the Oracle solution in isolated copies with declared invalid probes;
* emit candidate-only review records under ``research/registration/candidates``.

It cannot submit queue work, create registry records, approve policy, freeze a
task, or publish anything. Human-created ``library/registry`` records remain the
only admission boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
import unicodedata
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, cast

import yaml

from evallab.results import load_job
from evallab.runner import subscription_environment

SCHEMA_VERSION = 1
WORKBENCH_VERSION = "m049-v1"
ORACLE_REPETITIONS = 3
NOP_REPETITIONS = 2
MIN_ADVERSARIAL_CASES = 3
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
FLOATING_REFS = {"head", "latest", "main", "master", "trunk", "tip"}
FORBIDDEN_AGENT_IMAGE_PARTS = {"solution", "tests", "verifier", "workbench"}
LEAKAGE_DIAGNOSTIC_CODES = frozenset(
    {"agent_image_hidden_leak", "golden_data_leak", "hidden_artifact_exposure"}
)
ISOLATION_DIAGNOSTIC_CODES = frozenset(
    {
        "agent_env_unsupported",
        "build_context_unreadable",
        "build_network_use",
        "build_proof_invalid",
        "build_proof_lockfile_mismatch",
        "build_proof_lockfile_missing",
        "build_proof_unpinned_dependency",
        "collect_hooks_invalid",
        "collect_service_invalid",
        "compose_build_path_escape",
        "compose_feature_unsupported",
        "compose_host_ports_unsupported",
        "compose_image_unpinned",
        "compose_main_service_missing",
        "compose_network_mode_unsupported",
        "compose_main_env_credential_unauthorized",
        "compose_main_env_literal_secret",
        "compose_networks_unsupported",
        "compose_privileged_unsupported",
        "compose_sidecar_env_invalid",
        "compose_sidecar_env_unauthorized",
        "compose_structure_invalid",
        "compose_syntax_error",
        "compose_topology_invalid",
        "compose_volume_escape",
        "compose_volume_invalid",
        "compose_volume_mount_invalid",
        "compose_volume_unauthorized",
        "custom_compose_unsupported",
        "mcp_server_host_invalid",
        "mcp_server_unbound",
        "mcp_servers_invalid",
        "mcp_transport_unsupported",
        "mcp_url_auth_invalid",
        "mcp_url_invalid",
        "mcp_url_path_missing",
        "mcp_url_port_missing",
        "mcp_url_scheme_invalid",
        "path_escape",
        "prebuilt_image_unsupported",
        "solution_env_unsupported",
        "symlink_unsupported",
        "verifier_collect_unsupported",
        "verifier_credential_unauthorized",
        "verifier_env_invalid",
        "verifier_env_literal_secret",
        "verifier_not_isolated",
        "verifier_network_not_isolated",
        "verifier_phase_network_not_isolated",
        "control_network_binding_mismatch",
        "control_network_isolation_missing",
        "control_stage_tampered",
        "control_task_digest_mismatch",
        "control_task_identity_mismatch",
        "control_verifier_not_isolated",
    }
)

# Harbor 0.21.0 package identity pattern (harbor.constants.ORG_NAME_PATTERN).
# Reproduced here so the workbench can fail-closed on invalid package names
# without importing harbor at runtime.
HARBOR_PACKAGE_NAME_PATTERN = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9._-]*/[a-zA-Z0-9][a-zA-Z0-9._-]*$"
)


def _is_valid_harbor_package_name(name: str) -> bool:
    return bool(HARBOR_PACKAGE_NAME_PATTERN.fullmatch(name)) and ".." not in name


# --- The task.toml surface this workbench version claims to understand -------
# The workbench proves network isolation by reproducing Harbor's configuration
# resolution. Every table or key it fails to reproduce is a silent hole, and two
# review rounds each found a fresh one that let a task Harbor runs with egress
# earn `isolation: true`. Enumerating the holes cannot terminate, so the mirror
# is closed instead of open: this is the exhaustive set of constructs v1 models,
# and `_validate_supported_configuration` refuses everything else outright.
# Adding a key here is a deliberate act and must arrive with the check that
# models it. A value of `None` marks a table Harbor types as free-form and never
# interprets.
#
# M009 F-06: the accepted surface was originally drawn from what the test
# fixtures declare, so it refused three constructs `library/tasks/event-summary`
# declares and could not certify a single one of the four in-repo tasks. The
# keys admitted for that reason are the ones every real occurrence leaves inert
# — `mcp_servers = []`, `collect = []`, `os = "linux"`, `gpus = 0`, and empty
# `env` tables — and each is admitted *only* for the value
# `_MODELLED_CONSTRUCT_VALUES` proves inert. Admitting the key without that
# value model would be the silent hole again: an inert declaration and a loaded
# one are the same key.
_SUPPORTED_ENVIRONMENT_KEYS = frozenset(
    {
        "network_mode",
        "docker_image",
        "build_timeout_sec",
        "cpus",
        "memory_mb",
        "storage_mb",
        # Admitted for one value each; see `_MODELLED_CONSTRUCT_VALUES`.
        "os",
        "gpus",
        "mcp_servers",
        "env",
    }
)
SUPPORTED_TASK_CONFIG: dict[str, frozenset[str] | None] = {
    "": frozenset(
        {
            "schema_version",
            "artifacts",
            "task",
            "metadata",
            "agent",
            "verifier",
            "environment",
            "solution",
        }
    ),
    "task": frozenset({"name", "version", "description", "keywords", "authors"}),
    "task.authors": frozenset({"name", "email"}),
    "metadata": None,
    "agent": frozenset({"timeout_sec"}),
    "verifier": frozenset(
        {"timeout_sec", "environment_mode", "network_mode", "environment", "collect", "env"}
    ),
    # docker_image is deliberately absent here: a prebuilt verifier image would
    # bypass the tests/ build-context scan, which is the only boundary the
    # verifier image has.
    "verifier.environment": _SUPPORTED_ENVIRONMENT_KEYS - {"docker_image"},
    "environment": _SUPPORTED_ENVIRONMENT_KEYS,
    "environment.mcp_servers": frozenset({"name", "transport", "url"}),
    "verifier.collect": frozenset({"command", "service"}),
    "verifier.env": None,
    # SolutionConfig carries exactly one field, `env` (Harbor 0.21.0
    # models/task/config.py:335-336), so naming it closes the table.
    "solution": frozenset({"env"}),
}

# Notes for the constructs most likely to be reached, so the refusal explains
# itself instead of only naming a path.
_UNSUPPORTED_CONFIG_NOTES: dict[str, str] = {
    "steps": (
        "Harbor resolves a multi-step task's verifier step-first — "
        "resolve_effective_verifier_env_config returns steps[i].verifier.environment "
        "before [verifier.environment], and the phase override falls back the same way — "
        "so compliant task-level tables do not constrain a step's verifier. v1 models a "
        "single verify pass and refuses [[steps]] rather than certify one it cannot resolve"
    ),
    "multi_step_reward_strategy": "v1 models a single verify pass",
    "environment.allow_internet": (
        "the deprecated allow_internet alias is folded into network_mode by a Harbor "
        "model validator, but only when network_mode is absent from model_fields_set and "
        "allowed_hosts is None (models/task/config.py:885-892); mirroring that three-way "
        "interaction would put a second, weaker network resolver beside "
        "_effective_verifier_network. Declare network_mode explicitly instead"
    ),
    "verifier.environment.docker_image": (
        "a prebuilt verifier image is never built from tests/, so the build-context scan "
        "that is the verifier image's only network boundary would never see it"
    ),
}


@dataclass(frozen=True)
class _ModelledValue:
    """The single value shape v1 reproduces for an admitted key, and why.

    The module's rule is that a key arrives with the check that models it. For
    the F-06 keys the modelled shape is the inert one: Harbor reads each of them
    behind an emptiness or default test, so the accepted value is provably
    equivalent to omitting the key, while any other value reaches a Harbor code
    path v1 does not reproduce. `accepts` is that equivalence, and `note` records
    the Harbor behaviour that makes anything else a refusal.
    """

    accepts: Callable[[Any], bool]
    note: str


def _is_empty_array(value: Any) -> bool:
    return isinstance(value, list) and not value


def _is_empty_table(value: Any) -> bool:
    return isinstance(value, Mapping) and not value


def _is_default_task_os(value: Any) -> bool:
    # Harbor lowercases but does not strip (EnvironmentConfig.normalize_os), so
    # the accepted spelling is matched the same way.
    return isinstance(value, str) and value.lower() == "linux"


def _is_zero_gpus(value: Any) -> bool:
    # `True` is an int in Python and `gpus = true` is not a resource request.
    return isinstance(value, int) and not isinstance(value, bool) and value == 0


_EMPTY_ENV_NOTE = (
    "v1 models only an empty env table. Harbor resolves every entry against the *host* "
    "environment at runtime — resolve_env_vars substitutes ${VAR} from os.environ and "
    "raises when it is unset (utils/env.py:94-130) — so a populated table makes the "
    "container's environment a function of the workstation, which defeats determinism, "
    "and is the documented channel for API keys (verifier/verifier.py:166-171 warns about "
    "exactly that, and trial/trial.py:778-813 scrubs their resolved values out of the job "
    "directory afterwards). An empty table is provably inert: every consumer reads it "
    "behind a truthiness test"
)
_MODELLED_ENVIRONMENT_VALUES: dict[str, _ModelledValue] = {
    "os": _ModelledValue(
        accepts=_is_default_task_os,
        note=(
            "v1/v2 models only os = 'linux', which is the TaskOS default and therefore "
            "identical to omitting the key. os = 'windows' is refused because Harbor "
            "cannot enforce this workbench's isolation claim there: DockerEnvironment "
            "raises 'Docker network allowlist and dynamic network policy are only "
            "supported for Linux containers' whenever egress control is required "
            "(environments/docker/docker.py:218-222), and egress control is required by "
            "every network_mode other than public "
            "(environments/docker/docker.py:265-275). A Windows task also switches the "
            "file-transfer and exec platform and the artifact convention source, none of "
            "which v1/v2 reproduces"
        ),
    ),
    "gpus": _ModelledValue(
        accepts=_is_zero_gpus,
        note=(
            "v1/v2 models only gpus = 0, which Harbor folds to the same 0 as omitting the "
            "key (environments/base.py:367-369). A nonzero request cannot run under the "
            "local controls the workbench executes at all — DockerEnvironment leaves "
            "EnvironmentCapabilities.gpus at False, so Harbor raises 'Task requires N "
            "GPU(s) but docker environment does not support GPU allocation' "
            "(environments/base.py:745-750) — and the GPU-capable environments it steers "
            "to are cloud providers whose isolation v1/v2 does not model"
        ),
    ),
    "env": _ModelledValue(accepts=_is_empty_table, note=_EMPTY_ENV_NOTE),
}
# Keyed by the dotted spec `_scan_supported_table` computes, so one entry covers
# a key wherever it is reachable. `[verifier.environment]` reuses
# `EnvironmentConfig` verbatim, so it inherits the same value models.
_MODELLED_CONSTRUCT_VALUES: dict[str, _ModelledValue] = {
    **{f"environment.{key}": model for key, model in _MODELLED_ENVIRONMENT_VALUES.items()},
    **{
        f"verifier.environment.{key}": model
        for key, model in _MODELLED_ENVIRONMENT_VALUES.items()
    },
    "verifier.environment.mcp_servers": _ModelledValue(
        accepts=_is_empty_array,
        note=(
            "verifier environment may not declare MCP servers; verifier executes in isolation "
            "(harbor/trial/trial.py:648-650)"
        ),
    ),
    "solution.env": _ModelledValue(accepts=_is_empty_table, note=_EMPTY_ENV_NOTE),
}

_COLLECT_GUARD_CP_PATTERN = re.compile(
    r"^if\s+\[\s*!\s*-f\s+(?P<dst_guard>/[^\s\]]+)\s*\]\s*&&\s*\[\s*-f\s+(?P<src_guard>/[^\s\]]+)\s*\]\s*;\s*then\s+cp\s+(?:-f\s+)?(?P<src>/[^\s;]+)\s+(?P<dst>/[^\s;]+)\s*;\s*fi$",
    re.IGNORECASE,
)
_COLLECT_SRC_GUARD_CP_PATTERN = re.compile(
    r"^if\s+\[\s*-f\s+(?P<src_guard>/[^\s\]]+)\s*\]\s*;\s*then\s+cp\s+(?:-f\s+)?(?P<src>/[^\s;]+)\s+(?P<dst>/[^\s;]+)\s*;\s*fi$",
    re.IGNORECASE,
)
_COLLECT_TEST_CP_PATTERN = re.compile(
    r"^test\s+-f\s+(?P<src_guard>/[^\s&]+)\s*&&\s*cp\s+(?:-f\s+)?(?P<src>/[^\s]+)\s+(?P<dst>/[^\s]+)$",
    re.IGNORECASE,
)
_COLLECT_PLAIN_CP_PATTERN = re.compile(
    r"^cp\s+(?:-f\s+)?(?P<src>/[^\s]+)\s+(?P<dst>/[^\s]+)$",
    re.IGNORECASE,
)

NETWORK_SCRIPT_PATTERN = re.compile(
    r"(?:https?://|\bcurl\b|\bwget\b|\bapt(?:-get)?\b|\bpip(?:3)?\s+install\b|"
    r"\buvx\b|\bnpm\s+(?:install|ci)\b|\byarn\s+install\b|\bgit\s+clone\b|"
    r"\b(?:ssh|scp|nc|ncat|netcat|telnet)\b|\bsocket\.(?:socket|create_connection)\b|"
    r"\burllib\.|\brequests\.|\bhttpx\.|\baiohttp\.)",
    re.IGNORECASE,
)
# The separate verifier image has no container-level boundary at all: Harbor
# rebuilds the verifier runtime config with `extra_docker_compose: []`
# (harbor/trial/trial.py:648-650), so the workbench's `build.network=none`
# overlay never reaches that build. This pattern is therefore the whole boundary,
# and every spelling it misses is a task that fetches during the verifier build
# and still certifies. The spellings below are the plain, documented ones for
# each ecosystem's package manager; obfuscation defeats any text scan, so the
# claim is only that an unobfuscated fetch is named. `uv sync` leads because it
# is the idiom this repository itself uses. Maven and Gradle are matched on the
# tool name alone because every default goal resolves from a remote repository.
BUILD_NETWORK_PATTERN = re.compile(
    r"(?:https?://|ftp://|git://|ssh://|git@|\bcurl\b|\bwget\b|"
    r"\b(?:git|hg|svn)\s+(?:clone|fetch|pull|checkout)\b|"
    r"\bapt(?:-get)?\s+(?:update|install|upgrade|dist-upgrade)\b|"
    r"\b(?:apk|dnf|yum|microdnf|zypper)\s+(?:add|install|update|upgrade)\b|"
    r"\bpacman\s+(?:-[A-Za-z]*S|--sync)|"
    r"\b(?:brew|conda|mamba|micromamba)\s+(?:install|create|add|update|env)\b|"
    r"\bpip(?:3)?\s+(?:install|download|wheel)\b|"
    r"\buv\s+(?:pip\s+)?(?:install|sync|add|lock)\b|\buvx\b|"
    r"\b(?:poetry|pipenv|bundle|composer)\s+(?:install|add|update|require|lock|sync)\b|"
    r"\b(?:npm|pnpm)\s+(?:install|i|ci|add|update|up)\b|\bnpx\b|"
    r"\byarn\s+(?:install|add|ci|dlx|up|upgrade)\b|"
    r"\bgem\s+(?:install|update)\b|\bcargo\s+(?:install|add|fetch|update)\b|"
    r"\bgo\s+(?:get|install|mod\s+(?:download|tidy))\b|"
    r"\bdotnet\s+(?:restore|add|tool|build|publish|test)\b|"
    r"\b(?:mvn|mvnw|gradlew?)\b|"
    r"\bInvoke-(?:WebRequest|RestMethod)\b)",
    re.IGNORECASE,
)
NONDETERMINISM_PATTERN = re.compile(
    r"(?:\brandom\.|\bsecrets\.|\buuid\.uuid4\b|\btime\.time\b|"
    r"\bdatetime\.now\b|/dev/(?:u?random)|\bdate\s+\+)",
    re.IGNORECASE,
)
NETWORK_OVERLAY_RELATIVE = "environment/.workbench-network-none.yaml"
# Harbor 0.21.0 layers every extra compose file into the `docker compose ... build`
# invocation as well as `up`, so this overlay must deny the build network and the
# runtime network separately: `build.network` is the Compose *build* key, while
# `network_mode` only governs the started container. Harbor's own
# `docker-compose-build.yaml` declares `build.context` and no `build.network`, and
# Compose merges the two mappings, so `none` reaches the builder.
NETWORK_OVERLAY_CONTENT = (
    b"services:\n"
    b"  main:\n"
    b"    build:\n"
    b"      network: none\n"
    b"    network_mode: none\n"
)

def _network_overlay_content(
    sidecar_name: str | None = None,
    volume: Mapping[str, Any] | None = None,
    network_name: str | None = None,
) -> bytes:
    if sidecar_name is None:
        if volume is not None:
            raise WorkbenchError("volume declared without sidecar service")
        if network_name is not None:
            raise WorkbenchError("network declared without sidecar service")
        return NETWORK_OVERLAY_CONTENT
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", sidecar_name):
        raise WorkbenchError(f"unsafe sidecar service name in frozen candidate: {sidecar_name!r}")
    net_name = network_name if network_name is not None else "workbench-internal"
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", net_name):
        raise WorkbenchError(f"unsafe network name in frozen candidate: {net_name!r}")
    volume_name = str(volume.get("name")) if isinstance(volume, Mapping) else None
    mount_path = str(volume.get("mount_path")) if isinstance(volume, Mapping) else None
    has_volume = bool(volume_name and mount_path)
    lines = [
        "services:",
        "  main:",
        "    build:",
        "      network: none",
        "    networks:",
        f"      - {net_name}",
    ]
    if has_volume:
        lines.append("    volumes:")
        lines.append(f"      - {volume_name}:{mount_path}:ro")
    lines.extend([
        f"  {sidecar_name}:",
        "    build:",
        "      network: none",
        "    networks:",
        f"      - {net_name}",
    ])
    if has_volume:
        lines.append("    volumes:")
        lines.append(f"      - {volume_name}:{mount_path}:rw")
    if has_volume:
        lines.extend([
            "volumes:",
            f"  {volume_name}:",
        ])
    lines.extend([
        "networks:",
        f"  {net_name}:",
        "    internal: true",
    ])
    return "\n".join(lines).encode()


def _candidate_network_overlay(candidate: Mapping[str, Any]) -> bytes:
    topology = candidate.get("compose_topology")
    sidecar = topology.get("sidecar_service") if isinstance(topology, Mapping) else None
    volume = topology.get("volume") if isinstance(topology, Mapping) else None
    network = topology.get("network") if isinstance(topology, Mapping) else None
    network_name = network.get("name") if isinstance(network, Mapping) else None
    return _network_overlay_content(
        str(sidecar) if sidecar is not None else None,
        volume=volume,
        network_name=str(network_name) if sidecar is not None and network_name is not None else None,
    )

Severity = Literal["error", "warning", "info"]
Classification = Literal["task_defect", "harness_defect", "agent_failure", "expected"]
ControlStatus = Literal["completed", "harness_error", "interrupted"]
ProvenanceZone = Literal["01-external", "02-local-evidence", "03-synthetic", "04-curated"]
Disposition = Literal[
    "needs_changes",
    "controls_pending",
    "harness_blocked",
    "certified_for_review",
]


class WorkbenchError(RuntimeError):
    """Base error for safe workbench refusals."""


class UnsafePathError(WorkbenchError):
    """Raised when an input or output would escape its allowed root."""


class PacketConflictError(WorkbenchError):
    """Raised rather than replacing a non-identical review record."""


class ControlsNotAdmittedError(WorkbenchError):
    """Raised when static acceptance has not admitted local controls."""


class ControlInterrupted(WorkbenchError):
    """An injected control backend may use this to preserve an interrupted result."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _tree_entries(root: Path) -> list[tuple[str, str, int, str]]:
    """Return stable path/type/size/digest tuples without following symlinks."""
    if not root.exists():
        return []
    if root.is_file() and not root.is_symlink():
        return [(root.name, "file", root.stat().st_size, _sha256_file(root))]
    entries: list[tuple[str, str, int, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            target = os.readlink(path)
            entries.append(
                (relative, "symlink", len(target.encode()), _sha256_bytes(target.encode()))
            )
        elif path.is_file():
            entries.append((relative, "file", path.stat().st_size, _sha256_file(path)))
    return entries


def _tree_digest_from_entries(entries: Sequence[tuple[str, str, int, str]]) -> str:
    payload = [
        {"path": path, "type": entry_type, "size_bytes": size, "digest": digest}
        for path, entry_type, size, digest in entries
    ]
    return _sha256_bytes(_canonical_bytes(payload))


def _tree_digest(root: Path) -> str:
    return _tree_digest_from_entries(_tree_entries(root))


def _registry_package_digest_from_entries(
    entries: Sequence[tuple[str, str, int, str]],
) -> str:
    """Match registry.task_directory_digest from an already-hashed manifest."""
    aggregate = hashlib.sha256()
    ignored_names = {".DS_Store", ".git", "__pycache__", ".pytest_cache"}
    ignored_extensions = {".pyc", ".pyo", ".tmp"}
    for relative, entry_type, _size, digest in entries:
        pure = PurePosixPath(relative)
        if (
            entry_type != "file"
            or pure.name in ignored_names
            or pure.suffix in ignored_extensions
        ):
            continue
        aggregate.update(f"{digest.removeprefix('sha256:')}  ./{relative}\n".encode())
    return f"sha256:{aggregate.hexdigest()}"


def _registry_package_digest(root: Path) -> str:
    return _registry_package_digest_from_entries(_tree_entries(root))


def _verifier_output_digest(trial_dir: Path) -> str | None:
    """Digest the actual retained verifier files, not a synthesized reward value."""
    verifier_dir = trial_dir / "verifier"
    if not verifier_dir.is_dir() or not _tree_entries(verifier_dir):
        return None
    return _tree_digest(verifier_dir)


def _empty_digest() -> str:
    return _sha256_bytes(b"")


def _subpath_digest(path: Path) -> str:
    if path.is_file() and not path.is_symlink():
        return _sha256_file(path)
    if path.is_dir() or path.is_symlink():
        return _tree_digest(path)
    return _empty_digest()


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _repo_relative(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise UnsafePathError(f"path escapes repository: {path}") from exc


def _role_for_path(relative: str) -> str:
    first = PurePosixPath(relative).parts[0] if PurePosixPath(relative).parts else ""
    return {
        "task.toml": "config",
        "instruction.md": "instruction",
        "instructions.md": "instruction",
        "environment": "image",
        "solution": "oracle",
        "tests": "verifier",
        "verifier": "verifier",
        "workbench": "adversarial-control",
    }.get(first, "source")


@dataclass(frozen=True)
class Diagnostic:
    severity: Severity
    code: str
    classification: Classification
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "classification": self.classification,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True)
class CandidateSource:
    source_uri: str
    source_ref: str
    license: str
    provenance_zone: ProvenanceZone = "03-synthetic"
    credentials: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source_uri": self.source_uri,
            "source_ref": self.source_ref,
            "license": self.license,
            "provenance_zone": self.provenance_zone,
        }
        if self.credentials:
            data["credentials"] = list(self.credentials)
        return data

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CandidateSource:
        raw_creds = value.get("credentials")
        creds: tuple[str, ...] = (
            tuple(str(item) for item in raw_creds if isinstance(item, str))
            if isinstance(raw_creds, (list, tuple))
            else ()
        )
        return cls(
            source_uri=_required_string(value, "source_uri"),
            source_ref=_required_string(value, "source_ref"),
            license=_required_string(value, "license"),
            provenance_zone=cast(
                ProvenanceZone, value.get("provenance_zone", "03-synthetic")
            ),
            credentials=creds,
        )


@dataclass(frozen=True)
class ControlPlanEntry:
    control_id: str
    kind: Literal["oracle", "nop", "adversarial", "fair_alternative", "please_hack"]
    agent: Literal["oracle", "nop"]
    expected_reward: float
    mutation_path: str | None
    command: tuple[str, ...]
    command_digest: str
    concurrency: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id,
            "kind": self.kind,
            "agent": self.agent,
            "expected_reward": self.expected_reward,
            "mutation_path": self.mutation_path,
            "command": list(self.command),
            "command_digest": self.command_digest,
            "concurrency": self.concurrency,
        }


@dataclass(frozen=True)
class Inspection:
    candidate: dict[str, Any]
    diagnostics: tuple[Diagnostic, ...]
    control_plan: tuple[ControlPlanEntry, ...]

    @property
    def static_passed(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "task_workbench_plan",
            "candidate": self.candidate,
            "static_passed": self.static_passed,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "control_plan": [item.to_dict() for item in self.control_plan],
        }


@dataclass(frozen=True)
class ControlObservation:
    control_id: str
    status: ControlStatus
    reward: float | None
    reward_vector: dict[str, float]
    verifier_output_digest: str | None
    evidence_digest: str | None
    image_digest: str
    verifier_digest: str
    source_package_digest: str
    staged_package_digest: str
    command: tuple[str, ...]
    command_digest: str
    job_path: str | None = None
    exception_type: str | None = None
    diagnostic: str | None = None
    failure_classification: Classification | None = None

    def to_dict(self) -> dict[str, Any]:
        value = {
            "control_id": self.control_id,
            "status": self.status,
            "reward": self.reward,
            "reward_vector": dict(sorted(self.reward_vector.items())),
            "verifier_output_digest": self.verifier_output_digest,
            "evidence_digest": self.evidence_digest,
            "image_digest": self.image_digest,
            "verifier_digest": self.verifier_digest,
            "source_package_digest": self.source_package_digest,
            "staged_package_digest": self.staged_package_digest,
            "command": list(self.command),
            "command_digest": self.command_digest,
            "job_path": self.job_path,
            "exception_type": self.exception_type,
            "diagnostic": self.diagnostic,
        }
        if self.failure_classification is not None:
            value["failure_classification"] = self.failure_classification
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ControlObservation:
        allowed = {
            "control_id",
            "status",
            "reward",
            "reward_vector",
            "verifier_output_digest",
            "evidence_digest",
            "image_digest",
            "verifier_digest",
            "source_package_digest",
            "staged_package_digest",
            "command",
            "command_digest",
            "job_path",
            "exception_type",
            "diagnostic",
            "failure_classification",
        }
        unknown = set(value) - allowed
        if unknown:
            raise WorkbenchError(f"control observation has unknown fields: {sorted(unknown)}")
        status = value.get("status")
        if status not in {"completed", "harness_error", "interrupted"}:
            raise WorkbenchError(f"invalid control status: {status!r}")
        reward = value.get("reward")
        if reward is not None and not isinstance(reward, int | float):
            raise WorkbenchError("control reward must be numeric or null")
        vector = value.get("reward_vector")
        if not isinstance(vector, dict) or any(
            not isinstance(key, str) or not isinstance(item, int | float)
            for key, item in vector.items()
        ):
            raise WorkbenchError("control reward_vector must map strings to numbers")
        command = value.get("command")
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(item, str) for item in command)
        ):
            raise WorkbenchError("control command must be a non-empty string list")
        failure_classification = value.get("failure_classification")
        if failure_classification not in {
            None,
            "task_defect",
            "harness_defect",
            "agent_failure",
            "expected",
        }:
            raise WorkbenchError("control failure_classification is invalid")
        return cls(
            control_id=_required_string(value, "control_id"),
            status=cast(ControlStatus, status),
            reward=float(reward) if reward is not None else None,
            reward_vector={str(key): float(item) for key, item in vector.items()},
            verifier_output_digest=_optional_string(value, "verifier_output_digest"),
            evidence_digest=_optional_string(value, "evidence_digest"),
            image_digest=_required_digest(value, "image_digest"),
            verifier_digest=_required_digest(value, "verifier_digest"),
            source_package_digest=_required_digest(value, "source_package_digest"),
            staged_package_digest=_required_digest(value, "staged_package_digest"),
            command=tuple(command),
            command_digest=_required_digest(value, "command_digest"),
            job_path=_optional_string(value, "job_path"),
            exception_type=_optional_string(value, "exception_type"),
            diagnostic=_optional_string(value, "diagnostic"),
            failure_classification=cast(Classification | None, failure_classification),
        )


@dataclass(frozen=True)
class ControlBundle:
    candidate_id: str
    source_package_digest: str
    observations: tuple[ControlObservation, ...]
    bundle_digest: str

    @classmethod
    def build(
        cls,
        *,
        candidate_id: str,
        source_package_digest: str,
        observations: Sequence[ControlObservation],
    ) -> ControlBundle:
        body = {
            "schema_version": SCHEMA_VERSION,
            "kind": "task_workbench_controls",
            "candidate_id": candidate_id,
            "source_package_digest": source_package_digest,
            "observations": [item.to_dict() for item in observations],
        }
        return cls(
            candidate_id=candidate_id,
            source_package_digest=source_package_digest,
            observations=tuple(observations),
            bundle_digest=_sha256_bytes(_canonical_bytes(body)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "task_workbench_controls",
            "candidate_id": self.candidate_id,
            "source_package_digest": self.source_package_digest,
            "observations": [item.to_dict() for item in self.observations],
            "bundle_digest": self.bundle_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ControlBundle:
        allowed = {
            "schema_version",
            "kind",
            "candidate_id",
            "source_package_digest",
            "observations",
            "bundle_digest",
        }
        unknown = set(value) - allowed
        if unknown:
            raise WorkbenchError(f"control bundle has unknown fields: {sorted(unknown)}")
        if value.get("schema_version") != SCHEMA_VERSION:
            raise WorkbenchError("unsupported control bundle schema_version")
        if value.get("kind") != "task_workbench_controls":
            raise WorkbenchError("control bundle kind is invalid")
        raw_observations = value.get("observations")
        if not isinstance(raw_observations, list):
            raise WorkbenchError("control observations must be a list")
        observations = tuple(
            ControlObservation.from_dict(_required_mapping(item, "observation"))
            for item in raw_observations
        )
        rebuilt = cls.build(
            candidate_id=_required_string(value, "candidate_id"),
            source_package_digest=_required_digest(value, "source_package_digest"),
            observations=observations,
        )
        if value.get("bundle_digest") != rebuilt.bundle_digest:
            raise WorkbenchError("control bundle digest mismatch")
        return rebuilt


@dataclass(frozen=True)
class CheckReport:
    inspection: Inspection
    controls: ControlBundle | None
    diagnostics: tuple[Diagnostic, ...]
    disposition: Disposition

    @property
    def passed(self) -> bool:
        return self.disposition == "certified_for_review"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "task_workbench_check",
            "candidate_id": self.inspection.candidate["candidate_id"],
            "static_passed": self.inspection.static_passed,
            "controls_present": self.controls is not None,
            "passed": self.passed,
            "disposition": self.disposition,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "control_bundle_digest": self.controls.bundle_digest if self.controls else None,
        }


class ControlBackend(Protocol):
    def run(
        self,
        *,
        repo_root: Path,
        task_dir: Path,
        candidate: Mapping[str, Any],
        plan: ControlPlanEntry,
        run_root: Path,
    ) -> ControlObservation: ...


def _required_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkbenchError(f"{label} must be an object")
    return value


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise WorkbenchError(f"{key} must be a non-empty string")
    return item


def _optional_string(value: Mapping[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise WorkbenchError(f"{key} must be a non-empty string or null")
    return item


def _required_digest(value: Mapping[str, Any], key: str) -> str:
    item = _required_string(value, key)
    if not SHA256_PATTERN.fullmatch(item):
        raise WorkbenchError(f"{key} must be a sha256 digest")
    return item


def _diag(
    code: str,
    path: str,
    message: str,
    *,
    severity: Severity = "error",
    classification: Classification = "task_defect",
) -> Diagnostic:
    return Diagnostic(
        severity=severity,
        code=code,
        classification=classification,
        path=path,
        message=message,
    )


def _sort_diagnostics(values: Sequence[Diagnostic]) -> tuple[Diagnostic, ...]:
    return tuple(
        sorted(
            values,
            key=lambda item: (
                {"error": 0, "warning": 1, "info": 2}[item.severity],
                item.code,
                item.path,
                item.message,
            ),
        )
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ""


def _parse_task_toml(path: Path, diagnostics: list[Diagnostic]) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        diagnostics.append(_diag("task_toml_invalid", "task.toml", type(exc).__name__))
        return {}
    if not isinstance(parsed, dict):
        diagnostics.append(_diag("task_toml_invalid", "task.toml", "top level is not a table"))
        return {}
    return parsed


_MISSING = object()


def _unsupported_configuration(location: str, note: str | None = None) -> Diagnostic:
    note = note or _UNSUPPORTED_CONFIG_NOTES.get(location)
    detail = f"; {note}" if note else ""
    return _diag(
        "unsupported_task_configuration",
        "task.toml",
        f"{location} is outside the task.toml surface workbench {WORKBENCH_VERSION} models, "
        f"so isolation cannot be proven for this task{detail}. This is a limitation of the "
        "workbench, not necessarily a defect in the task: a construct v1 does not reproduce "
        "is refused rather than silently certified",
        classification="harness_defect",
    )


def _validate_supported_configuration(
    config: Mapping[str, Any], diagnostics: list[Diagnostic]
) -> None:
    """Refuse every task.toml construct this workbench version does not model.

    This is the fail-closed counterpart to `_effective_verifier_network`. That
    function reproduces Harbor's resolution for the constructs in
    `SUPPORTED_TASK_CONFIG`; anything else reaches Harbor unexamined, and an
    unexamined construct that changes the effective network policy turns a
    packet into a false green. Refusing an honest task the workbench cannot
    reason about is the cheaper error.
    """
    _scan_supported_table(config, "", "", diagnostics)


def _scan_supported_table(
    table: Mapping[str, Any], spec: str, location: str, diagnostics: list[Diagnostic]
) -> None:
    allowed = SUPPORTED_TASK_CONFIG[spec]
    if allowed is None:
        return
    for key in sorted(table):
        child_spec = f"{spec}.{key}" if spec else key
        child_location = f"{location}.{key}" if location else key
        if key not in allowed:
            diagnostics.append(_unsupported_configuration(child_location))
            continue
        if key in {"mcp_servers", "collect"} and isinstance(table[key], Mapping):
            diagnostics.append(_unsupported_configuration(child_location))
            continue
        model = _MODELLED_CONSTRUCT_VALUES.get(child_spec)
        if model is not None:
            # A key admitted for one value is decided by that value and never
            # descended into: the accepted shape is empty or scalar by
            # construction, so there is nothing below it, and descending would
            # let an unmodelled child pass under an allowlisted parent.
            if not model.accepts(table[key]):
                diagnostics.append(_unsupported_configuration(child_location, model.note))
            continue
        _scan_supported_value(table[key], child_spec, child_location, diagnostics)


def _scan_supported_value(
    value: Any, spec: str, location: str, diagnostics: list[Diagnostic]
) -> None:
    """Descend into tables and arrays of tables, naming the exact offending path."""
    if isinstance(value, Mapping):
        if SUPPORTED_TASK_CONFIG.get(spec, _MISSING) is _MISSING:
            # An allowlisted scalar key that arrived as a table is a shape the
            # workbench never modelled either.
            diagnostics.append(_unsupported_configuration(location))
            return
        _scan_supported_table(value, spec, location, diagnostics)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, Mapping | list):
                _scan_supported_value(item, spec, f"{location}[{index}]", diagnostics)


def _is_pinned_ref(value: str) -> bool:
    normalized = value.strip()
    if not normalized or normalized.lower() in FLOATING_REFS:
        return False
    lowered = normalized.lower()
    if any(f"/{item}" in lowered or f"@{item}" in lowered for item in FLOATING_REFS):
        return False
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", normalized, re.IGNORECASE):
        return True
    if re.fullmatch(r"v?\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.-]+)?", normalized):
        return True
    if re.fullmatch(r"local/[a-z0-9][a-z0-9._/-]+@\d+\.\d+(?:\.\d+)?", normalized):
        return True
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9._/-]+@v?\d+(?:\.\d+){0,2}", normalized))

def _is_pinned_image_ref(value: str) -> bool:
    normalized = value.strip()
    return bool(
        re.fullmatch(
            r"[A-Za-z0-9._/:-]+@sha256:[0-9a-f]{64}",
            normalized,
            re.IGNORECASE,
        )
        or re.fullmatch(
            r"[A-Za-z0-9._/-]+:v?\d+(?:\.\d+){1,2}(?:[-+][A-Za-z0-9.-]+)?",
            normalized,
        )
    )


def _validate_source(source: CandidateSource, diagnostics: list[Diagnostic]) -> None:
    if not source.source_uri.strip():
        diagnostics.append(_diag("source_uri_missing", "$source", "source_uri is required"))
    if not _is_pinned_ref(source.source_ref):
        diagnostics.append(
            _diag(
                "source_ref_unpinned",
                "$source",
                "source_ref must be an immutable commit, release, or local version pin",
            )
        )
    if not source.license.strip() or source.license.strip().lower() in {"unknown", "none", "n/a"}:
        diagnostics.append(
            _diag("license_missing", "$source", "a concrete redistribution license is required")
        )


def _validate_layout(task_dir: Path, diagnostics: list[Diagnostic]) -> None:
    required = (
        "task.toml",
        "instruction.md",
        "environment/Dockerfile",
        "solution/solve.sh",
        "tests/Dockerfile",
        "tests/test.sh",
    )
    for relative in required:
        if not (task_dir / relative).is_file():
            diagnostics.append(_diag("required_file_missing", relative, "required file is missing"))

    for path in sorted(task_dir.rglob("*")):
        if not path.is_symlink():
            continue
        relative = path.relative_to(task_dir).as_posix()
        diagnostics.append(
            _diag(
                "symlink_unsupported",
                relative,
                "candidate packages may not contain symlinks; Harbor build context bytes "
                "must be regular files",
            )
        )
        if not _is_under(path, task_dir):
            diagnostics.append(
                _diag("path_escape", relative, "symlink resolves outside the candidate package")
            )

    for relative in ("solution/solve.sh", "tests/test.sh"):
        path = task_dir / relative
        if path.is_file() and not os.access(path, os.X_OK):
            diagnostics.append(
                _diag("script_not_executable", relative, "script must be executable")
            )
    if (task_dir / ".gitignore").exists():
        diagnostics.append(
            _diag(
                "custom_package_ignore_unsupported",
                ".gitignore",
                "v1 cannot bind Harbor task digests with custom package ignore rules",
            )
        )


def _validate_task_metadata(
    config: Mapping[str, Any], task_dir: Path, diagnostics: list[Diagnostic]
) -> tuple[str, str | None, list[str]]:
    schema = config.get("schema_version")
    if not isinstance(schema, str) or not re.fullmatch(r"1\.\d+", schema):
        diagnostics.append(
            _diag("schema_version_invalid", "task.toml", "schema_version must be a 1.x string")
        )
    task = config.get("task")
    task_table = task if isinstance(task, dict) else {}
    raw_name = task_table.get("name")
    name = raw_name if isinstance(raw_name, str) and raw_name.strip() else task_dir.name
    if not isinstance(raw_name, str) or not raw_name.strip():
        diagnostics.append(_diag("task_name_missing", "task.toml", "[task].name is required"))
    elif not _is_valid_harbor_package_name(name):
        diagnostics.append(
            _diag(
                "task_name_invalid",
                "task.toml",
                f"Package name must be in 'org/name' format with alphanumeric characters, "
                f"hyphens, underscores, and dots. Cannot start with a dot or contain '..'. "
                f"Got: {name}",
            )
        )
    raw_version = task_table.get("version")
    version = raw_version if isinstance(raw_version, str) and raw_version.strip() else None
    if version is None:
        diagnostics.append(_diag("task_version_missing", "task.toml", "[task].version is required"))
    description = task_table.get("description")
    if not isinstance(description, str) or not description.strip():
        diagnostics.append(
            _diag("task_description_missing", "task.toml", "[task].description is required")
        )
    keywords = task_table.get("keywords")
    normalized_keywords = (
        [item for item in keywords if isinstance(item, str) and item.strip()]
        if isinstance(keywords, list)
        else []
    )
    if not 3 <= len(normalized_keywords) <= 8 or len(normalized_keywords) != len(
        set(normalized_keywords)
    ):
        diagnostics.append(
            _diag(
                "task_keywords_invalid",
                "task.toml",
                "[task].keywords must contain 3-8 unique non-empty strings",
            )
        )
    authors = task_table.get("authors")
    if not isinstance(authors, list) or not authors:
        diagnostics.append(
            _diag("task_authors_missing", "task.toml", "[task].authors must name an author")
        )
    metadata = config.get("metadata")
    metadata_table = metadata if isinstance(metadata, dict) else {}
    for key in ("difficulty", "category", "tags"):
        item = metadata_table.get(key)
        if item is None or item == "" or item == []:
            diagnostics.append(
                _diag("metadata_incomplete", "task.toml", f"[metadata].{key} is required")
            )
    return name, version, normalized_keywords


def _validate_timeouts_and_artifacts(
    config: Mapping[str, Any], diagnostics: list[Diagnostic]
) -> list[str]:
    for section in ("agent", "verifier"):
        raw = config.get(section)
        table = raw if isinstance(raw, dict) else {}
        timeout = table.get("timeout_sec")
        if (
            not isinstance(timeout, int | float)
            or isinstance(timeout, bool)
            or not 1 <= timeout <= 21_600
        ):
            diagnostics.append(
                _diag(
                    "timeout_invalid",
                    "task.toml",
                    f"[{section}].timeout_sec must be between 1 and 21600 seconds",
                )
            )
    raw_artifacts = config.get("artifacts")
    if not isinstance(raw_artifacts, list):
        diagnostics.append(
            _diag("artifacts_invalid", "task.toml", "artifacts must be an explicit list")
        )
        return []
    artifacts: list[str] = []
    for item in raw_artifacts:
        if not isinstance(item, str):
            diagnostics.append(
                _diag("artifact_path_invalid", "task.toml", "artifact paths must be strings")
            )
            continue
        pure = PurePosixPath(item)
        if not pure.is_absolute() or ".." in pure.parts or not item.startswith("/app/"):
            diagnostics.append(
                _diag(
                    "artifact_path_escape",
                    "task.toml",
                    f"artifact path {item!r} must be absolute under /app",
                )
            )
        if any(part in FORBIDDEN_AGENT_IMAGE_PARTS for part in pure.parts):
            diagnostics.append(
                _diag(
                    "hidden_artifact_exposure",
                    "task.toml",
                    f"artifact path {item!r} exposes a hidden task component",
                )
            )
        artifacts.append(item)
    if len(artifacts) != len(set(artifacts)):
        diagnostics.append(
            _diag("artifact_duplicate", "task.toml", "artifact paths must be unique")
        )
    return artifacts


def _docker_logical_lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\\\n", " ").splitlines() if line.strip()]


def _docker_copy_sources(arguments: str) -> tuple[list[str], bool]:
    """Parse COPY/ADD sources and fail closed on unsupported dynamic syntax."""
    payload = arguments.strip()
    while payload.startswith("--"):
        match = re.match(r"--[^\s]+\s+", payload)
        if match is None:
            return [], True
        payload = payload[match.end() :].lstrip()
    if payload.startswith("["):
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            return [], True
        if (
            not isinstance(value, list)
            or len(value) < 2
            or any(not isinstance(item, str) for item in value)
        ):
            return [], True
        return list(value[:-1]), False
    try:
        tokens = shlex.split(payload)
    except ValueError:
        return [], True
    if len(tokens) < 2:
        return [], True
    return tokens[:-1], False


def _is_proven_offline_install(line: str) -> bool:
    """Accept only exact package-manager commands that cannot contact a registry."""
    normalized = line.strip()
    if re.fullmatch(r"(?:RUN\s+)?uv\s+sync(?:\s+--[A-Za-z0-9=._/-]+)+", normalized):
        tokens = normalized.split()
        return "--offline" in tokens and "--frozen" in tokens
    if not re.fullmatch(
        r"(?:RUN\s+)?(?:python(?:3)?\s+-m\s+)?pip(?:3)?\s+install"
        r"(?:\s+--[A-Za-z0-9=._/-]+|\s+-r\s+\S+)+",
        normalized,
    ):
        return False
    tokens = shlex.split(normalized)
    if tokens and tokens[0] == "RUN":
        tokens = tokens[1:]
    try:
        requirement = tokens[tokens.index("-r") + 1]
    except (ValueError, IndexError):
        return False
    requirement_path = PurePosixPath(requirement)
    local_requirement = bool(
        re.fullmatch(r"/?[A-Za-z0-9._/-]+", requirement)
        and ".." not in requirement_path.parts
        and "//" not in requirement
        and not urllib.parse.urlsplit(requirement).scheme
    )
    return (
        "--no-index" in tokens
        and "--require-hashes" in tokens
        and local_requirement
    )


def _validate_build_network(
    text: str, relative: str, diagnostics: list[Diagnostic], has_proof: bool = False
) -> None:
    for line in _docker_logical_lines(text):
        if line.startswith("#") or re.match(r"(?i)^FROM\s+", line):
            continue
        if BUILD_NETWORK_PATTERN.search(line):
            if has_proof and _is_proven_offline_install(line):
                continue
            diagnostics.append(
                _diag(
                    "build_network_use",
                    relative,
                    "Docker builds may not fetch from a network or invoke an online package "
                    "manager; reviewed proof permits only exact offline frozen installs",
                )
            )
            return


# Both images Harbor builds from the candidate, and the human-readable name each
# refusal should use. `environment/` builds the agent image; `tests/` is what
# Harbor's `_verifier_env_build_context` hands the separate verifier image.
_BUILD_CONTEXTS: tuple[tuple[str, str], ...] = (
    ("environment", "the agent image"),
    ("tests", "the separate verifier image"),
)


# --- The build-context filenames this workbench version claims to understand --
_COMPOSE_CONFIG_NAME = re.compile(r"^(?:docker-)?compose\b.*\.(?:ya?ml|json)$", re.IGNORECASE)
_COMPOSE_ENV_CONFIG_NAME = re.compile(r"^\.env(?:\..+)?$", re.IGNORECASE)
_BUILDER_CONFIG_NAMES = frozenset({".dockerignore"})


def _unmodelled_build_config(name: str, context: str = "environment") -> tuple[str, str] | None:
    """Classify a build-context filename read as configuration but not modelled."""
    if _COMPOSE_CONFIG_NAME.match(name):
        if context == "environment" and name in {"docker-compose.yaml", "docker-compose.yml"}:
            return None
        return (
            "custom_compose_unsupported",
            "Harbor layers a Compose file from an environment directory into both "
            "`docker compose build` and `docker compose up`, and excludes any service "
            "that declares its own network_mode or networks from the egress control "
            "that implements no-network; arbitrary task-authored Compose files are "
            "refused",
        )
    if _COMPOSE_ENV_CONFIG_NAME.match(name):
        return (
            "compose_env_file_unsupported",
            "Harbor runs `docker compose --project-directory` on this directory, so "
            "Compose reads this file and interpolates it into every Compose document "
            "including its own; v1/v2 does not model task-supplied Compose variables",
        )
    if name in _BUILDER_CONFIG_NAMES:
        return (
            "build_context_ignore_unsupported",
            "Docker's builder reads this file to drop paths from the build context, so "
            "the files the workbench scanned would not be the files in the image; v1/v2 "
            "certifies a context exactly as it stands",
        )
    if name in {"build-proof.json", "offline-build-proof.json"}:
        return None
    return None


def _is_exact_dependency_version(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    if not normalized or normalized.lower() in FLOATING_REFS:
        return False
    return bool(
        re.fullmatch(
            r"(?:v?[0-9][A-Za-z0-9._+-]*|[0-9a-f]{40,64}|sha256:[0-9a-f]{64}|"
            r"[A-Za-z0-9._+-]+@sha256:[0-9a-f]{64})",
            normalized,
        )
    )


def _is_sha256_hex(val: Any) -> bool:
    if not isinstance(val, str):
        return False
    raw = val.lower().removeprefix("sha256:")
    return bool(re.fullmatch(r"[0-9a-f]{64}", raw))


def _derive_all_build_contexts(
    task_dir: Path, compose_topology: Mapping[str, Any] | None, diagnostics: list[Diagnostic]
) -> tuple[tuple[str, str], ...]:
    contexts = list(_BUILD_CONTEXTS)
    seen_paths: dict[str, str] = {"environment": "the agent image", "tests": "the separate verifier image"}
    if compose_topology and isinstance(compose_topology.get("services"), Mapping):
        for s_name, s_info in compose_topology["services"].items():
            if not isinstance(s_info, Mapping):
                continue
            ctx_rel = s_info.get("build_context")
            if not ctx_rel or not isinstance(ctx_rel, str) or ctx_rel in ("environment", "."):
                continue
            clean_rel = ctx_rel.strip("/")
            resolved = (task_dir / clean_rel).resolve()
            if not _is_under(resolved, task_dir):
                diagnostics.append(
                    _diag("compose_build_path_escape", "environment/docker-compose.yaml", f"service {s_name!r} build context escapes task directory: {clean_rel}")
                )
                continue
            if resolved.is_symlink():
                diagnostics.append(
                    _diag("compose_build_path_symlink", "environment/docker-compose.yaml", f"service {s_name!r} build context is a symlink: {clean_rel}")
                )
                continue
            if clean_rel not in seen_paths:
                seen_paths[clean_rel] = f"the sidecar service {s_name}"
                contexts.append((clean_rel, f"the sidecar service {s_name}"))
    return tuple(contexts)


def _validate_offline_build_proofs(
    task_dir: Path, diagnostics: list[Diagnostic], compose_topology: Mapping[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    proofs: dict[str, dict[str, Any]] = {}
    all_contexts = _derive_all_build_contexts(task_dir, compose_topology, diagnostics)
    for context, _image in all_contexts:
        root = task_dir / context
        if not root.is_dir() or root.is_symlink():
            continue
        for proof_name in ("build-proof.json", "offline-build-proof.json"):
            proof_path = root / proof_name
            if not proof_path.is_file() or proof_path.is_symlink():
                continue
            rel_proof = proof_path.relative_to(task_dir).as_posix()
            try:
                data = json.loads(proof_path.read_text(encoding="utf-8"))
            except Exception as exc:
                diagnostics.append(
                    _diag("build_proof_invalid", rel_proof, f"build proof is not valid JSON: {exc}")
                )
                continue
            if not isinstance(data, Mapping):
                diagnostics.append(
                    _diag("build_proof_invalid", rel_proof, "build proof must be a JSON object")
                )
                continue

            # Check if canonical MCP substrate proof
            if "substrate_version" in data or data.get("mode") in ("complete_offline_package", "plan_only"):
                mode = data.get("mode")
                if mode == "plan_only":
                    if (root / "Dockerfile").exists():
                        diagnostics.append(
                            _diag("build_proof_invalid", rel_proof, "plan-only proof cannot accompany an active Dockerfile build context")
                        )
                        continue
                    proofs[context] = {
                        "context": context,
                        "proof_path": rel_proof,
                        "mode": "plan_only",
                        "substrate_version": data.get("substrate_version"),
                        "proof_digest": _sha256_file(proof_path),
                    }
                    continue
                if mode != "complete_offline_package":
                    diagnostics.append(
                        _diag("build_proof_invalid", rel_proof, f"unknown substrate proof mode: {mode!r}")
                    )
                    continue

                # 1. Mandatory requirements.txt matching requirements_sha256
                req_path = root / "requirements.txt"
                if not req_path.is_file() or req_path.is_symlink():
                    diagnostics.append(
                        _diag("build_proof_lockfile_missing", rel_proof, "requirements.txt missing or symlink for substrate build proof")
                    )
                    continue
                declared_req_digest = data.get("requirements_sha256")
                if not _is_sha256_hex(declared_req_digest):
                    diagnostics.append(
                        _diag("build_proof_invalid", rel_proof, "substrate proof requires valid sha256 'requirements_sha256'")
                    )
                    continue
                actual_req_digest = hashlib.sha256(req_path.read_bytes()).hexdigest()
                if actual_req_digest != declared_req_digest.lower().removeprefix("sha256:"):
                    diagnostics.append(
                        _diag("build_proof_lockfile_mismatch", rel_proof, f"requirements.txt digest {actual_req_digest} does not match proof {declared_req_digest}")
                    )
                    continue

                # 2. Mandatory Dockerfile matching dockerfile_sha256
                df_path = root / "Dockerfile"
                if not df_path.is_file() or df_path.is_symlink():
                    diagnostics.append(
                        _diag("build_proof_invalid", rel_proof, "Dockerfile missing or symlink for substrate build proof")
                    )
                    continue
                declared_df_digest = data.get("dockerfile_sha256")
                if not _is_sha256_hex(declared_df_digest):
                    diagnostics.append(
                        _diag("build_proof_invalid", rel_proof, "substrate proof requires valid sha256 'dockerfile_sha256'")
                    )
                    continue
                df_bytes = df_path.read_bytes()
                actual_df_digest = hashlib.sha256(df_bytes).hexdigest()
                if actual_df_digest != declared_df_digest.lower().removeprefix("sha256:"):
                    diagnostics.append(
                        _diag("build_proof_invalid", rel_proof, f"Dockerfile digest {actual_df_digest} does not match proof {declared_df_digest}")
                    )
                    continue

                # Parse all Dockerfile instructions; reject ADD, flags, and multi-source COPY
                df_text = df_bytes.decode("utf-8", errors="replace")
                dockerfile_copies: list[tuple[str, str]] = []
                has_df_err = False
                for line in _docker_logical_lines(df_text):
                    clean = line.strip()
                    if clean.startswith("#"):
                        continue
                    if re.match(r"(?i)^ADD\b", clean):
                        diagnostics.append(
                            _diag("build_proof_invalid", rel_proof, f"ADD instructions are forbidden in canonical sidecar Dockerfile: {clean!r}")
                        )
                        has_df_err = True
                        break
                    if re.match(r"(?i)^COPY\b", clean):
                        # Reject flags (--from, --chown, etc)
                        if re.search(r"--[a-z0-9_-]+=", clean):
                            diagnostics.append(
                                _diag("build_proof_invalid", rel_proof, f"COPY flags are forbidden in canonical sidecar Dockerfile: {clean!r}")
                            )
                            has_df_err = True
                            break
                        # Handle JSON array form COPY ["src", "dst"] vs shell form COPY src dst
                        if clean[4:].strip().startswith("["):
                            try:
                                json_arr = json.loads(clean[4:].strip())
                                if not isinstance(json_arr, list) or len(json_arr) != 2 or not all(isinstance(x, str) for x in json_arr):
                                    diagnostics.append(
                                        _diag("build_proof_invalid", rel_proof, f"JSON COPY must contain exactly 2 string arguments: {clean!r}")
                                    )
                                    has_df_err = True
                                    break
                                src_token, dst_token = json_arr[0], json_arr[1]
                            except Exception:
                                diagnostics.append(
                                    _diag("build_proof_invalid", rel_proof, f"malformed JSON COPY instruction: {clean!r}")
                                )
                                has_df_err = True
                                break
                        else:
                            tokens = clean.split()[1:]
                            if len(tokens) != 2:
                                diagnostics.append(
                                    _diag("build_proof_invalid", rel_proof, f"only exact 2-token COPY instructions are supported in canonical sidecar Dockerfile: {clean!r}")
                                )
                                has_df_err = True
                                break
                            src_token, dst_token = tokens[0], tokens[1]
                        dockerfile_copies.append((src_token, dst_token))

                if has_df_err:
                    continue

                # Verify canonical base copies
                canonical_base_copies = [
                    ("wheelhouse", "/wheelhouse"),
                    ("requirements.txt", "/app/requirements.txt"),
                    ("server.py", "/app/server.py"),
                ]
                if dockerfile_copies[:3] != canonical_base_copies:
                    diagnostics.append(
                        _diag("build_proof_invalid", rel_proof, f"Dockerfile must begin with canonical base COPY instructions, got {dockerfile_copies[:3]}")
                    )
                    continue

                asset_copies = dockerfile_copies[3:]
                copy_sources = [src for src, _dst in asset_copies]
                if len(copy_sources) != len(set(copy_sources)):
                    diagnostics.append(
                        _diag("build_proof_invalid", rel_proof, "Dockerfile contains duplicate asset COPY sources")
                    )
                    continue

                copy_dests = [dst for _src, dst in asset_copies]
                if len(copy_dests) != len(set(copy_dests)):
                    diagnostics.append(
                        _diag("build_proof_invalid", rel_proof, "Dockerfile contains duplicate asset COPY destinations")
                    )
                    continue

                # 3. Mandatory runtime_assets verification and 1:1 mapping with Dockerfile COPY
                has_asset_err = False
                declared_asset_paths = set()
                declared_asset_fold = set()
                if "runtime_assets" not in data:
                    diagnostics.append(
                        _diag("build_proof_invalid", rel_proof, "substrate build proof requires mandatory 'runtime_assets' list")
                    )
                    continue
                raw_assets = data["runtime_assets"]
                if not isinstance(raw_assets, Sequence) or isinstance(raw_assets, (str, bytes)):
                    diagnostics.append(
                        _diag("build_proof_invalid", rel_proof, "runtime_assets in substrate proof must be a list")
                    )
                    continue

                for a in raw_assets:
                    if (
                        not isinstance(a, Mapping)
                        or not isinstance(a.get("path"), str)
                        or not isinstance(a.get("sha256"), str)
                        or not isinstance(a.get("size_bytes"), int)
                        or not _is_sha256_hex(a["sha256"])
                    ):
                        diagnostics.append(
                            _diag("build_proof_invalid", rel_proof, "each runtime_asset entry must declare valid path (str), sha256 (hex), and size_bytes (int)")
                        )
                        has_asset_err = True
                        break
                    a_rel = a["path"]
                    # NFC normalization check
                    if unicodedata.normalize("NFC", a_rel) != a_rel:
                        diagnostics.append(
                            _diag("build_proof_invalid", rel_proof, f"runtime asset path {a_rel!r} must be Unicode NFC normalized")
                        )
                        has_asset_err = True
                        break
                    if (
                        not a_rel
                        or "\\" in a_rel
                        or a_rel.startswith("/")
                        or any(part in (".", "..") for part in Path(a_rel).parts)
                        or any(ord(c) < 32 or ord(c) == 127 for c in a_rel)
                    ):
                        diagnostics.append(
                            _diag("build_proof_invalid", rel_proof, f"runtime asset path {a_rel!r} is not a normalized confined POSIX relative path")
                        )
                        has_asset_err = True
                        break
                    a_fold = a_rel.casefold()
                    a_first = a_fold.split("/", 1)[0]
                    if (
                        a_fold in {".dockerignore", "compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml", "dockerfile", "dockerfile.dockerignore", "offline-build-proof.json", "requirements.txt", "server.py", "wheelhouse"}
                        or a_first in {".dockerignore", "compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml", "dockerfile", "dockerfile.dockerignore", "offline-build-proof.json", "requirements.txt", "server.py", "wheelhouse"}
                        or a_fold.startswith("dockerfile.")
                        or a_first.startswith("dockerfile.")
                    ):
                        diagnostics.append(
                            _diag("build_proof_invalid", rel_proof, f"runtime asset path {a_rel!r} is reserved")
                        )
                        has_asset_err = True
                        break
                    if a_fold in declared_asset_fold:
                        diagnostics.append(
                            _diag("build_proof_invalid", rel_proof, f"duplicate runtime asset path in proof: {a_rel!r}")
                        )
                        has_asset_err = True
                        break
                    declared_asset_fold.add(a_fold)
                    declared_asset_paths.add(a_rel)
                    a_file = root / a_rel
                    if not a_file.is_file() or a_file.is_symlink() or not _is_under(a_file.resolve(), root.resolve()):
                        diagnostics.append(
                            _diag("build_proof_invalid", rel_proof, f"runtime asset {a_rel!r} missing, symlink, or escapes root")
                        )
                        has_asset_err = True
                        break
                    a_bytes = a_file.read_bytes()
                    if len(a_bytes) != a["size_bytes"]:
                        diagnostics.append(
                            _diag("build_proof_invalid", rel_proof, f"runtime asset {a_rel!r} size {len(a_bytes)} does not match proof {a['size_bytes']}")
                        )
                        has_asset_err = True
                        break
                    a_hash = hashlib.sha256(a_bytes).hexdigest()
                    if a_hash != a["sha256"].lower().removeprefix("sha256:"):
                        diagnostics.append(
                            _diag("build_proof_invalid", rel_proof, f"runtime asset {a_rel!r} digest {a_hash} does not match proof {a['sha256']}")
                        )
                        has_asset_err = True
                        break

                if has_asset_err:
                    continue

                # Check for prefix / ancestor collisions among runtime assets
                folded_sorted = sorted(declared_asset_fold)
                has_prefix_err = False
                for idx_f, left in enumerate(folded_sorted):
                    prefix_check = f"{left}/"
                    for right in folded_sorted[idx_f + 1:]:
                        if right.startswith(prefix_check):
                            diagnostics.append(
                                _diag("build_proof_invalid", rel_proof, f"runtime asset path {right!r} conflicts with ancestor prefix {left!r}")
                            )
                            has_prefix_err = True
                            break
                    if has_prefix_err:
                        break
                if has_prefix_err:
                    continue

                expected_asset_copies = [
                    (a["path"], f"/app/{a['path']}")
                    for a in sorted(raw_assets, key=lambda x: x["path"])
                ]
                if asset_copies != expected_asset_copies:
                    diagnostics.append(
                        _diag("build_proof_invalid", rel_proof, "Dockerfile asset COPY lines do not match sorted proof runtime_assets exactly")
                    )
                    continue

                # 4. Mandatory wheels verification strictly under root/wheelhouse/<basename>
                if "wheels" not in data or not isinstance(data["wheels"], Sequence) or not data["wheels"]:
                    diagnostics.append(
                        _diag("build_proof_invalid", rel_proof, "substrate build proof requires non-empty 'wheels' list")
                    )
                    continue
                wheels = data["wheels"]

                if "wheel_count" not in data or not isinstance(data["wheel_count"], int) or data["wheel_count"] != len(wheels):
                    diagnostics.append(
                        _diag("build_proof_invalid", rel_proof, f"mandatory integer wheel_count missing or does not match wheels list length {len(wheels)}")
                    )
                    continue

                wheelhouse_dir = root / "wheelhouse"
                if not wheelhouse_dir.is_dir() or wheelhouse_dir.is_symlink():
                    diagnostics.append(
                        _diag("build_proof_invalid", rel_proof, "wheelhouse directory missing or symlink")
                    )
                    continue

                # Strict directory inspection: reject symlinks, directories, non-.whl files, and hidden files
                actual_wheel_files = []
                has_dir_err = False
                for p in wheelhouse_dir.iterdir():
                    if p.is_symlink():
                        diagnostics.append(
                            _diag("build_proof_invalid", rel_proof, f"wheelhouse contains symlink entry {p.name!r}")
                        )
                        has_dir_err = True
                        break
                    if not p.is_file():
                        diagnostics.append(
                            _diag("build_proof_invalid", rel_proof, f"wheelhouse contains non-file directory {p.name!r}")
                        )
                        has_dir_err = True
                        break
                    if not p.name.endswith(".whl") or p.name.startswith("."):
                        diagnostics.append(
                            _diag("build_proof_invalid", rel_proof, f"wheelhouse contains non-wheel or hidden file {p.name!r}")
                        )
                        has_dir_err = True
                        break
                    actual_wheel_files.append(p.name)

                if has_dir_err:
                    continue

                if len(actual_wheel_files) != data["wheel_count"]:
                    diagnostics.append(
                        _diag("build_proof_invalid", rel_proof, f"actual regular wheel count {len(actual_wheel_files)} does not match proof wheel_count {data['wheel_count']}")
                    )
                    continue

                pinned_deps = []
                has_wheel_err = False
                declared_filenames = set()
                seen_fold = set()
                for w in wheels:
                    if (
                        not isinstance(w, Mapping)
                        or not isinstance(w.get("filename"), str)
                        or not isinstance(w.get("sha256"), str)
                        or not isinstance(w.get("size_bytes"), int)
                        or not _is_sha256_hex(w["sha256"])
                    ):
                        diagnostics.append(
                            _diag("build_proof_invalid", rel_proof, "each wheel entry must declare valid filename (str), sha256 (hex), and size_bytes (int)")
                        )
                        has_wheel_err = True
                        break
                    w_name = w["filename"]
                    if (
                        Path(w_name).name != w_name
                        or not w_name.endswith(".whl")
                        or "/" in w_name
                        or "\\" in w_name
                        or any(ord(c) < 32 or ord(c) == 127 for c in w_name)
                    ):
                        diagnostics.append(
                            _diag("build_proof_invalid", rel_proof, f"wheel filename {w_name!r} must be a normalized plain .whl basename")
                        )
                        has_wheel_err = True
                        break
                    w_fold = w_name.casefold()
                    if w_fold in seen_fold:
                        diagnostics.append(
                            _diag("build_proof_invalid", rel_proof, f"duplicate wheel filename in proof: {w_name!r}")
                        )
                        has_wheel_err = True
                        break
                    seen_fold.add(w_fold)
                    declared_filenames.add(w_name)
                    w_path = wheelhouse_dir / w_name
                    if not w_path.is_file() or w_path.is_symlink() or not _is_under(w_path.resolve(), wheelhouse_dir.resolve()):
                        diagnostics.append(
                            _diag("build_proof_invalid", rel_proof, f"wheel {w_name!r} missing, symlink, or escapes wheelhouse")
                        )
                        has_wheel_err = True
                        break
                    w_bytes = w_path.read_bytes()
                    if len(w_bytes) != w["size_bytes"]:
                        diagnostics.append(
                            _diag("build_proof_invalid", rel_proof, f"wheel {w_name!r} size {len(w_bytes)} does not match proof {w['size_bytes']}")
                        )
                        has_wheel_err = True
                        break
                    w_hash = hashlib.sha256(w_bytes).hexdigest()
                    if w_hash != w["sha256"].lower().removeprefix("sha256:"):
                        diagnostics.append(
                            _diag("build_proof_invalid", rel_proof, f"wheel {w_name!r} digest {w_hash} does not match proof {w['sha256']}")
                        )
                        has_wheel_err = True
                        break
                    pinned_deps.append({
                        "name": w.get("name") or w_name.split("-")[0],
                        "version": w.get("version") or "pinned",
                        "sha256": w["sha256"],
                        "wheel": w_name,
                    })
                if has_wheel_err:
                    continue

                # Exact inventory check
                extra_wheels = set(actual_wheel_files) - declared_filenames
                if extra_wheels:
                    diagnostics.append(
                        _diag("build_proof_unpinned_dependency", rel_proof, f"extra unapproved wheels in wheelhouse not in proof: {sorted(extra_wheels)}")
                    )
                    continue

                proofs[context] = {
                    "context": context,
                    "proof_path": rel_proof,
                    "lockfile": "requirements.txt",
                    "lockfile_digest": actual_req_digest,
                    "ecosystem": "pypi",
                    "reviewed_by": "eval-lab-substrate",
                    "pinned_dependencies": pinned_deps,
                    "pinned_dependencies_count": len(pinned_deps),
                    "proof_digest": _sha256_file(proof_path),
                }
                continue

            # Legacy / Tau2 offline build proof
            if data.get("kind") != "offline_build_proof":
                diagnostics.append(
                    _diag(
                        "build_proof_invalid",
                        rel_proof,
                        "build proof kind must be 'offline_build_proof'",
                    )
                )
                continue
            ecosystem = data.get("ecosystem")
            if not isinstance(ecosystem, str) or not ecosystem.strip():
                diagnostics.append(
                    _diag("build_proof_invalid", rel_proof, "build proof requires 'ecosystem'")
                )
                continue
            lockfile_rel = data.get("lockfile")
            if not isinstance(lockfile_rel, str) or not lockfile_rel.strip():
                diagnostics.append(
                    _diag("build_proof_invalid", rel_proof, "build proof requires 'lockfile'")
                )
                continue
            lockfile_path = root / lockfile_rel
            if not lockfile_path.is_file() or not _is_under(lockfile_path, root):
                diagnostics.append(
                    _diag(
                        "build_proof_lockfile_missing",
                        rel_proof,
                        f"declared lockfile {lockfile_rel!r} does not exist in build context",
                    )
                )
                continue
            declared_lock_digest = data.get("lockfile_digest")
            if not isinstance(declared_lock_digest, str) or not SHA256_PATTERN.match(declared_lock_digest):
                diagnostics.append(
                    _diag(
                        "build_proof_invalid",
                        rel_proof,
                        "build proof requires valid sha256 'lockfile_digest'",
                    )
                )
                continue
            actual_lock_digest = _sha256_file(lockfile_path)
            if actual_lock_digest != declared_lock_digest:
                diagnostics.append(
                    _diag(
                        "build_proof_lockfile_mismatch",
                        rel_proof,
                        f"lockfile digest {actual_lock_digest} does not match declared {declared_lock_digest}",
                    )
                )
                continue
            pinned_deps = data.get("pinned_dependencies")
            if not isinstance(pinned_deps, (Mapping, Sequence)) or not pinned_deps:
                diagnostics.append(
                    _diag(
                        "build_proof_invalid",
                        rel_proof,
                        "build proof requires non-empty 'pinned_dependencies'",
                    )
                )
                continue
            has_unpinned = False
            if isinstance(pinned_deps, Mapping):
                for dep_name, dep_ver in pinned_deps.items():
                    if (
                        not isinstance(dep_name, str)
                        or not dep_name.strip()
                        or not _is_exact_dependency_version(dep_ver)
                    ):
                        diagnostics.append(
                            _diag(
                                "build_proof_unpinned_dependency",
                                rel_proof,
                                f"dependency {dep_name!r} has floating/unpinned reference {dep_ver!r}",
                            )
                        )
                        has_unpinned = True
                        break
            elif isinstance(pinned_deps, Sequence):
                for item in pinned_deps:
                    if not isinstance(item, Mapping) or not item.get("name") or not item.get("version"):
                        diagnostics.append(
                            _diag(
                                "build_proof_invalid",
                                rel_proof,
                                "each pinned dependency item must declare name and version",
                            )
                        )
                        has_unpinned = True
                        break
                    ver_str = item.get("version")
                    if not _is_exact_dependency_version(ver_str):
                        diagnostics.append(
                            _diag(
                                "build_proof_unpinned_dependency",
                                rel_proof,
                                f"dependency {item.get('name')!r} has floating/unpinned reference {ver_str!r}",
                            )
                        )
                        has_unpinned = True
                        break
            if has_unpinned:
                continue
            reviewed_by = data.get("reviewed_by")
            if not isinstance(reviewed_by, str) or not reviewed_by.strip():
                diagnostics.append(
                    _diag("build_proof_invalid", rel_proof, "build proof requires 'reviewed_by'")
                )
                continue
            proofs[context] = {
                "context": context,
                "proof_path": rel_proof,
                "lockfile": lockfile_rel,
                "lockfile_digest": actual_lock_digest,
                "ecosystem": ecosystem,
                "reviewed_by": reviewed_by,
                "pinned_dependencies": pinned_deps,
                "pinned_dependencies_count": len(pinned_deps),
                "proof_digest": _sha256_file(proof_path),
            }
    return proofs


def _validate_sidecar_environment(
    service_name: str,
    service_env: Any,
    rel_path: str,
    credentials: tuple[str, ...],
    diagnostics: list[Diagnostic],
) -> None:
    """Validate sidecar environment values are simple, declared credential placeholders."""
    entries: list[tuple[str, str]]
    if isinstance(service_env, Mapping):
        entries = [(str(k), str(v)) for k, v in service_env.items()]
    elif isinstance(service_env, Sequence) and not isinstance(service_env, (str, bytes)):
        entries = []
        for item in service_env:
            if not isinstance(item, str):
                diagnostics.append(
                    _diag(
                        "compose_sidecar_env_invalid",
                        rel_path,
                        f"sidecar {service_name!r} environment entry {item!r} is not a string",
                    )
                )
                continue
            if "=" in item:
                key, value = item.split("=", 1)
                entries.append((key, value))
            else:
                entries.append((item, ""))
    else:
        diagnostics.append(
            _diag(
                "compose_sidecar_env_invalid",
                rel_path,
                f"sidecar {service_name!r} environment must be a mapping or list",
            )
        )
        return

    for key, value in entries:
        if not value:
            diagnostics.append(
                _diag(
                    "compose_sidecar_env_invalid",
                    rel_path,
                    f"sidecar {service_name!r} environment entry {key!r} has no value",
                )
            )
            continue
        match = re.fullmatch(r"^\$\{([A-Z_][A-Z0-9_]*)\}$|^\$([A-Z_][A-Z0-9_]*)$", value)
        if not match:
            diagnostics.append(
                _diag(
                    "compose_sidecar_env_invalid",
                    rel_path,
                    f"sidecar {service_name!r} environment value {key}={value!r} is not a simple ${{VAR}} or $VAR placeholder",
                )
            )
            continue
        var = match.group(1) or match.group(2)
        if var not in credentials:
            diagnostics.append(
                _diag(
                    "compose_sidecar_env_unauthorized",
                    rel_path,
                    f"sidecar {service_name!r} environment references undeclared credential {var!r}",
                )
            )


def _validate_service_volume_mounts(
    service_name: str,
    service_mounts: Any,
    volume_name: str | None,
    rel_path: str,
    diagnostics: list[Diagnostic],
) -> list[dict[str, Any]]:
    """Validate a service volume list uses only the task-local named volume."""
    valid_mounts: list[dict[str, Any]] = []
    if not isinstance(service_mounts, Sequence) or isinstance(service_mounts, (str, bytes)):
        diagnostics.append(
            _diag(
                "compose_volume_mount_invalid",
                rel_path,
                f"service {service_name!r} volumes must be a list",
            )
        )
        return valid_mounts
    if volume_name is None and service_mounts:
        diagnostics.append(
            _diag(
                "compose_volume_mount_invalid",
                rel_path,
                f"service {service_name!r} mounts a volume but no top-level 'volumes' is declared",
            )
        )
        return valid_mounts
    for mount in service_mounts:
        source, target, mode = _parse_compose_volume_mount(mount)
        if source is None:
            diagnostics.append(
                _diag(
                    "compose_volume_mount_invalid",
                    rel_path,
                    f"service {service_name!r} volume mount {mount!r} is malformed",
                )
            )
            continue
        if target is None:
            diagnostics.append(
                _diag(
                    "compose_volume_mount_invalid",
                    rel_path,
                    f"service {service_name!r} named volume mount {mount!r} is missing a target path",
                )
            )
            continue
        if source.startswith("/") or re.match(r"^[A-Za-z]:/", source) or ":" in source or "\\" in source:
            diagnostics.append(
                _diag(
                    "compose_volume_escape",
                    rel_path,
                    f"service {service_name!r} volume mount {mount!r} is a host bind or external path",
                )
            )
            continue
        if source != volume_name:
            diagnostics.append(
                _diag(
                    "compose_volume_unauthorized",
                    rel_path,
                    f"service {service_name!r} may only mount task-local volume {volume_name!r}, got {source!r}",
                )
            )
            continue
        if not _is_under_path_only(Path(target), Path("/")):
            diagnostics.append(
                _diag(
                    "compose_volume_escape",
                    rel_path,
                    f"service {service_name!r} volume target {target!r} escapes the container root",
                )
            )
            continue
        expected_mode = "ro" if service_name == "main" else "rw"
        if mode != expected_mode:
            diagnostics.append(
                _diag(
                    "compose_volume_mount_invalid",
                    rel_path,
                    f"service {service_name!r} must mount {volume_name!r} as {expected_mode}, got {mode!r}",
                )
            )
            continue
        valid_mounts.append({"source": source, "target": target, "mode": mode})
    return valid_mounts


def _parse_compose_volume_mount(mount: Any) -> tuple[str | None, str | None, str]:
    """Parse a Compose volume mount string or mapping.

    Returns (source, target, mode).  Mode defaults to 'rw'.  For malformed
    entries, returns (None, None, '') and the caller emits a diagnostic.
    """
    if isinstance(mount, str):
        parts = mount.split(":")
        if len(parts) == 1:
            # short form: target path creates anonymous volume (rejected later)
            return parts[0], parts[0], "rw"
        source = parts[0]
        target = parts[1]
        mode = parts[2] if len(parts) > 2 else "rw"
        return source, target, mode
    if isinstance(mount, Mapping):
        source = mount.get("source")
        target = mount.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            return None, None, ""
        read_only = mount.get("read_only", False)
        if not isinstance(read_only, bool):
            read_only = str(read_only).lower() in {"true", "1", "yes", "ro"}
        return source, target, "ro" if read_only else "rw"
    return None, None, ""


def _is_under_path_only(path: Path, root: Path) -> bool:
    """Check that a path is absolute and stays under root (used for volume targets)."""
    try:
        return path.is_absolute() and _is_under(path, root)
    except (ValueError, OSError):
        return False


def _extract_sidecar_env(service_env: Any) -> dict[str, str]:
    """Return the sidecar environment as a {key: value} mapping."""
    result: dict[str, str] = {}
    if isinstance(service_env, Mapping):
        for k, v in service_env.items():
            result[str(k)] = str(v)
    elif isinstance(service_env, Sequence) and not isinstance(service_env, (str, bytes)):
        for item in service_env:
            if isinstance(item, str) and "=" in item:
                key, value = item.split("=", 1)
                result[key] = value
    return result


def _validate_compose_networks(
    data: Mapping[str, Any],
    rel_path: str,
    diagnostics: list[Diagnostic],
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Validate the top-level networks section, if any.

    Returns `(network_record, network_name, is_valid)`.
    A valid record contains exactly one network whose definition is only
    ``internal: true``, using a safe task-local name.
    """
    top_networks = data.get("networks")
    if top_networks is None:
        return None, None, True
    if not isinstance(top_networks, Mapping):
        diagnostics.append(
            _diag(
                "compose_networks_unsupported",
                rel_path,
                "top-level 'networks' must be a mapping",
            )
        )
        return None, None, False
    if not top_networks:
        diagnostics.append(
            _diag(
                "compose_networks_unsupported",
                rel_path,
                "top-level 'networks' must declare exactly one network",
            )
        )
        return None, None, False
    if len(top_networks) > 1:
        diagnostics.append(
            _diag(
                "compose_networks_unsupported",
                rel_path,
                f"top-level 'networks' may contain at most 1 network, got {len(top_networks)}: {list(top_networks)}",
            )
        )
        return None, None, False
    net_name, net_def = next(iter(top_networks.items()))
    if not isinstance(net_name, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]*", net_name
    ):
        diagnostics.append(
            _diag(
                "compose_networks_unsupported",
                rel_path,
                f"network name {net_name!r} is not a safe task-local name",
            )
        )
        return None, None, False
    if not isinstance(net_def, Mapping):
        diagnostics.append(
            _diag(
                "compose_networks_unsupported",
                rel_path,
                f"network {net_name!r} definition must be a mapping",
            )
        )
        return None, None, False
    for key in sorted(net_def, key=str):
        if not isinstance(key, str) or key != "internal":
            diagnostics.append(
                _diag(
                    "compose_networks_unsupported",
                    rel_path,
                    f"network {net_name!r} declares unsupported key {key!r}",
                )
            )
            return None, None, False
    if net_def.get("internal") is not True:
        diagnostics.append(
            _diag(
                "compose_networks_unsupported",
                rel_path,
                f"network {net_name!r} must set internal: true",
            )
        )
        return None, None, False
    return {"name": net_name, "internal": True}, net_name, True


def _validate_service_networks(
    service_name: str,
    networks: Any,
    network_name: str,
    rel_path: str,
    diagnostics: list[Diagnostic],
) -> None:
    """Validate a service attaches only to the one declared internal network."""
    if isinstance(networks, list):
        if (
            len(networks) != 1
            or not isinstance(networks[0], str)
            or networks[0] != network_name
        ):
            diagnostics.append(
                _diag(
                    "compose_networks_unsupported",
                    rel_path,
                    f"service {service_name!r} must declare exactly one network attachment to {network_name!r}",
                )
            )
        return
    diagnostics.append(
        _diag(
            "compose_networks_unsupported",
            rel_path,
            f"service {service_name!r} 'networks' must be a single-item list containing {network_name!r}",
        )
    )


def _validate_compose_topology(
    task_dir: Path,
    diagnostics: list[Diagnostic],
    credentials: tuple[str, ...] = (),
) -> tuple[dict[str, Any] | None, str | None]:
    compose_path = task_dir / "environment/docker-compose.yaml"
    if not compose_path.is_file():
        compose_path = task_dir / "environment/docker-compose.yml"
        if not compose_path.is_file():
            return None, None
    rel_path = compose_path.relative_to(task_dir).as_posix()
    try:
        raw_text = compose_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text)
    except Exception as exc:
        diagnostics.append(
            _diag("compose_syntax_error", rel_path, f"docker-compose.yaml syntax error: {exc}")
        )
        return None, None
    if not isinstance(data, Mapping):
        diagnostics.append(
            _diag("compose_structure_invalid", rel_path, "docker-compose.yaml must be a mapping")
        )
        return None, None
    volume_definition: dict[str, Any] | None = None
    for top_key in data:
        if top_key not in {"services", "version", "volumes", "networks"}:
            diagnostics.append(
                _diag(
                    "custom_compose_unsupported",
                    rel_path,
                    f"top-level Compose key {top_key!r} is unsupported; only 'services', 'volumes', and 'networks' are modelled",
                )
            )
    services = data.get("services")
    if not isinstance(services, Mapping) or not services:
        diagnostics.append(
            _diag("compose_structure_invalid", rel_path, "docker-compose.yaml must declare 'services'")
        )
        return None, None
    if "main" not in services:
        diagnostics.append(
            _diag("compose_main_service_missing", rel_path, "Compose topology must declare a 'main' service")
        )
        return None, None
    top_volumes = data.get("volumes")
    volume_name: str | None = None
    if top_volumes is not None:
        if not isinstance(top_volumes, Mapping):
            diagnostics.append(
                _diag("compose_volume_invalid", rel_path, "top-level 'volumes' must be a mapping")
            )
            top_volumes = None
        elif top_volumes:
            if len(top_volumes) > 1:
                diagnostics.append(
                    _diag(
                        "compose_volume_invalid",
                        rel_path,
                        f"top-level 'volumes' may contain at most 1 volume, got {len(top_volumes)}: {list(top_volumes)}",
                    )
                )
            volume_name, volume_def = next(iter(top_volumes.items()))
            if not isinstance(volume_name, str) or not re.fullmatch(
                r"[a-z0-9][a-z0-9_-]*", volume_name
            ):
                diagnostics.append(
                    _diag(
                        "compose_volume_invalid",
                        rel_path,
                        f"volume name {volume_name!r} is not a safe task-local name",
                    )
                )
                volume_name = None
            else:
                if volume_def is not None and volume_def != {}:
                    diagnostics.append(
                        _diag(
                            "compose_volume_unauthorized",
                            rel_path,
                            f"volume {volume_name!r} may only be declared with an empty or null definition",
                        )
                    )
                if volume_name:
                    volume_definition = {volume_name: None}
    network_record, network_name, networks_valid = _validate_compose_networks(
        data, rel_path, diagnostics
    )
    service_names = list(services.keys())
    if len(service_names) > 2:
        diagnostics.append(
            _diag(
                "compose_topology_invalid",
                rel_path,
                f"Compose topology admits at most 2 services ('main' + 1 MCP sidecar), got {len(service_names)}: {service_names}",
            )
        )
    sidecar_name: str | None = None
    for name in service_names:
        if not isinstance(name, str):
            diagnostics.append(
                _diag(
                    "compose_topology_invalid",
                    rel_path,
                    f"service name {name!r} must be a string",
                )
            )
            continue
        if name != "main":
            sidecar_name = name
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name, re.IGNORECASE):
                diagnostics.append(
                    _diag("compose_topology_invalid", rel_path, f"sidecar service name {name!r} is invalid")
                )
    service_summaries: dict[str, Any] = {}
    for name, s_config in services.items():
        if not isinstance(name, str):
            diagnostics.append(
                _diag(
                    "compose_structure_invalid",
                    rel_path,
                    f"service name {name!r} must be a string",
                )
            )
            continue
        if not isinstance(s_config, Mapping):
            diagnostics.append(
                _diag("compose_structure_invalid", rel_path, f"service {name!r} configuration must be a mapping")
            )
            continue
        allowed_service_keys = {"build", "image"}
        dedicated_service_keys = {
            "depends_on",
            "environment",
            "expose",
            "healthcheck",
            "ipc",
            "network_mode",
            "networks",
            "pid",
            "ports",
            "privileged",
            "volumes",
        }
        unsupported_service_keys = sorted(
            set(s_config) - allowed_service_keys - dedicated_service_keys,
            key=str,
        )
        if unsupported_service_keys:
            diagnostics.append(
                _diag(
                    "compose_service_key_unsupported",
                    rel_path,
                    f"service {name!r} uses unsupported keys {unsupported_service_keys}; "
                    "v2 models only local build or pinned image",
                )
            )
        if "network_mode" in s_config:
            diagnostics.append(
                _diag(
                    "custom_compose_unsupported",
                    rel_path,
                    f"service {name!r} declares custom network_mode {s_config['network_mode']!r}",
                )
            )
            continue
        if "networks" in s_config:
            if not networks_valid:
                pass
            elif network_name is None:
                diagnostics.append(
                    _diag(
                        "compose_networks_unsupported",
                        rel_path,
                        f"service {name!r} declares networks but no top-level network is defined",
                    )
                )
            else:
                _validate_service_networks(
                    name, s_config["networks"], network_name, rel_path, diagnostics
                )
        elif networks_valid and network_name is not None:
            diagnostics.append(
                _diag(
                    "compose_networks_unsupported",
                    rel_path,
                    f"service {name!r} must attach to declared network {network_name!r}",
                )
            )
        if s_config.get("depends_on"):
            diagnostics.append(
                _diag(
                    "compose_topology_invalid",
                    rel_path,
                    f"service {name!r} may not declare depends_on",
                )
            )
        if s_config.get("expose"):
            diagnostics.append(
                _diag(
                    "compose_host_ports_unsupported",
                    rel_path,
                    f"service {name!r} may not expose ports",
                )
            )
        if s_config.get("healthcheck"):
            diagnostics.append(
                _diag(
                    "compose_service_key_unsupported",
                    rel_path,
                    f"service {name!r} may not declare a healthcheck",
                )
            )
        if "ports" in s_config and s_config["ports"]:
            diagnostics.append(
                _diag(
                    "compose_host_ports_unsupported",
                    rel_path,
                    f"service {name!r} may not publish host ports",
                )
            )
        if s_config.get("privileged") is True:
            diagnostics.append(
                _diag(
                    "compose_privileged_unsupported",
                    rel_path,
                    f"service {name!r} may not request privileged mode",
                )
            )
        if s_config.get("pid") == "host" or s_config.get("ipc") == "host":
            diagnostics.append(
                _diag(
                    "compose_privileged_unsupported",
                    rel_path,
                    f"service {name!r} may not share host PID/IPC namespace",
                )
            )

        service_env = s_config.get("environment")
        sidecar_env: dict[str, str] | None = None
        if service_env:
            if name == "main":
                diagnostics.append(
                    _diag(
                        "compose_main_env_unauthorized",
                        rel_path,
                        "main service may not declare an environment",
                    )
                )
            else:
                _validate_sidecar_environment(
                    name, service_env, rel_path, credentials, diagnostics
                )
                sidecar_env = _extract_sidecar_env(service_env)

        service_mounts = s_config.get("volumes")
        valid_mounts: list[dict[str, Any]] = []
        if service_mounts is not None:
            valid_mounts = _validate_service_volume_mounts(
                name,
                service_mounts,
                volume_name,
                rel_path,
                diagnostics,
            )

        build_cfg = s_config.get("build")
        image_cfg = s_config.get("image")
        build_context_rel: str | None = None
        if build_cfg is not None:
            if isinstance(build_cfg, str):
                ctx_str = build_cfg
            elif isinstance(build_cfg, Mapping):
                unsupported_build_keys = sorted(set(build_cfg) - {"context"}, key=str)
                if unsupported_build_keys:
                    diagnostics.append(
                        _diag(
                            "compose_build_key_unsupported",
                            rel_path,
                            f"service {name!r} build uses unsupported keys {unsupported_build_keys}",
                        )
                    )
                ctx_str = str(build_cfg.get("context", "."))
            else:
                ctx_str = "."
            resolved_ctx = (task_dir / "environment" / ctx_str).resolve()
            if not _is_under(resolved_ctx, task_dir / "environment"):
                diagnostics.append(
                    _diag(
                        "compose_build_path_escape",
                        rel_path,
                        f"service {name!r} build context {ctx_str!r} escapes environment directory",
                    )
                )
            else:
                build_context_rel = resolved_ctx.relative_to(task_dir.resolve()).as_posix()
                nested_dockerfile = resolved_ctx / "Dockerfile"
                if not nested_dockerfile.is_file():
                    diagnostics.append(
                        _diag(
                            "compose_build_dockerfile_missing",
                            rel_path,
                            f"service {name!r} local build context has no Dockerfile",
                        )
                    )
                else:
                    nested_relative = nested_dockerfile.relative_to(task_dir).as_posix()
                    nested_text = _read_text(nested_dockerfile)
                    sidecar_proof = any(
                        (resolved_ctx / pn).is_file()
                        for pn in ("build-proof.json", "offline-build-proof.json")
                    )
                    _validate_build_network(
                        nested_text, nested_relative, diagnostics, has_proof=sidecar_proof
                    )
                    from_lines = [
                        line
                        for line in _docker_logical_lines(nested_text)
                        if re.match(r"(?i)^FROM\s+", line)
                    ]
                    if not from_lines:
                        diagnostics.append(
                            _diag(
                                "compose_build_from_missing",
                                nested_relative,
                                f"service {name!r} Dockerfile has no FROM instruction",
                            )
                        )
                    for from_line in from_lines:
                        ref = from_line.split()[1] if len(from_line.split()) >= 2 else ""
                        if not _is_pinned_image_ref(ref):
                            diagnostics.append(
                                _diag(
                                    "compose_image_unpinned",
                                    nested_relative,
                                    f"service {name!r} base image {ref!r} is not immutable",
                                )
                            )
        elif image_cfg is not None:
            if not isinstance(image_cfg, str) or not _is_pinned_image_ref(image_cfg):
                diagnostics.append(
                    _diag(
                        "compose_image_unpinned",
                        rel_path,
                        f"service {name!r} image {image_cfg!r} must be pinned by @sha256 digest or immutable version",
                    )
                )
        else:
            diagnostics.append(
                _diag(
                    "compose_structure_invalid",
                    rel_path,
                    f"service {name!r} must declare either 'build' context or pinned 'image'",
                )
            )
        service_summaries[name] = {
            "name": name,
            "build_context": build_context_rel,
            "image": image_cfg if isinstance(image_cfg, str) else None,
            "environment": sidecar_env if name != "main" else None,
            "volumes": valid_mounts,
        }
    volume_record: dict[str, Any] | None = None
    if volume_name:
        main_targets = [
            m["target"]
            for m in service_summaries.get("main", {}).get("volumes", [])
        ]
        sidecar_targets = [
            m["target"]
            for m in service_summaries.get(sidecar_name or "", {}).get("volumes", [])
            if sidecar_name
        ]
        mount_path: str | None = None
        if main_targets:
            mount_path = main_targets[0]
        elif sidecar_targets:
            mount_path = sidecar_targets[0]
        if main_targets and sidecar_targets and main_targets[0] != sidecar_targets[0]:
            diagnostics.append(
                _diag(
                    "compose_volume_mount_invalid",
                    rel_path,
                    f"main and sidecar must mount {volume_name!r} to the same target, "
                    f"got {main_targets[0]!r} and {sidecar_targets[0]!r}",
                )
            )
        volume_record = {
            "name": volume_name,
            "mount_path": mount_path,
            "definition": volume_definition,
        }
    if networks_valid and network_name is not None and sidecar_name is None:
        diagnostics.append(
            _diag(
                "compose_networks_unsupported",
                rel_path,
                f"internal network {network_name!r} requires a sidecar service",
            )
        )
    topology_record = {
        "compose_file": rel_path,
        "services": service_summaries,
        "sidecar_service": sidecar_name,
        "network": network_record,
        "volume": volume_record,
        "digest": _sha256_bytes(_canonical_bytes(data)),
    }
    return topology_record, sidecar_name


def _validate_mcp_servers(
    config: Mapping[str, Any],
    sidecar_name: str | None,
    diagnostics: list[Diagnostic],
) -> list[dict[str, Any]]:
    environment = config.get("environment")
    if not isinstance(environment, Mapping):
        return []
    raw_mcp = environment.get("mcp_servers")
    if raw_mcp is None or raw_mcp == []:
        return []
    if not isinstance(raw_mcp, Sequence) or isinstance(raw_mcp, (str, bytes)):
        diagnostics.append(
            _diag(
                "mcp_servers_invalid",
                "task.toml",
                "[environment].mcp_servers must be a list of server tables",
            )
        )
        return []
    servers: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_mcp):
        location = f"environment.mcp_servers[{idx}]"
        if not isinstance(item, Mapping):
            diagnostics.append(
                _diag("mcp_servers_invalid", "task.toml", f"{location} must be a table")
            )
            continue
        name = item.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name, re.IGNORECASE):
            diagnostics.append(
                _diag("mcp_servers_invalid", "task.toml", f"{location}.name must be a safe identifier")
            )
        transport = item.get("transport")
        if transport != "streamable-http":
            diagnostics.append(
                _diag(
                    "mcp_transport_unsupported",
                    "task.toml",
                    f"{location}.transport is {transport!r}; only 'streamable-http' is supported in this v2 slice (stdio/SSE rejected)",
                )
            )
        url_str = item.get("url")
        if not isinstance(url_str, str) or not url_str.strip():
            diagnostics.append(
                _diag("mcp_url_invalid", "task.toml", f"{location}.url must be a non-empty URL string")
            )
            continue
        try:
            parsed = urllib.parse.urlparse(url_str)
        except Exception:
            diagnostics.append(
                _diag("mcp_url_invalid", "task.toml", f"{location}.url {url_str!r} cannot be parsed")
            )
            continue
        if parsed.scheme.lower() != "http":
            diagnostics.append(
                _diag(
                    "mcp_url_scheme_invalid",
                    "task.toml",
                    f"{location}.url scheme is {parsed.scheme!r}; only 'http' is supported for local MCP sidecars",
                )
            )
        if parsed.username or parsed.password:
            diagnostics.append(
                _diag(
                    "mcp_url_auth_invalid",
                    "task.toml",
                    f"{location}.url may not contain embedded credentials",
                )
            )
        host = parsed.hostname
        if not host or host.lower() in {"localhost", "127.0.0.1", "::1"} or re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
            diagnostics.append(
                _diag(
                    "mcp_server_host_invalid",
                    "task.toml",
                    f"{location}.url host {host!r} is invalid; must name a declared local Compose service, not localhost or IP",
                )
            )
        elif sidecar_name is None or host != sidecar_name:
            diagnostics.append(
                _diag(
                    "mcp_server_unbound",
                    "task.toml",
                    f"{location}.url host {host!r} does not match declared task Compose sidecar service {sidecar_name!r}",
                )
            )
        if parsed.port is None or not (1 <= parsed.port <= 65535):
            diagnostics.append(
                _diag(
                    "mcp_url_port_missing",
                    "task.toml",
                    f"{location}.url must specify an explicit port (1-65535)",
                )
            )
        if not parsed.path or parsed.path in {"", "/"}:
            diagnostics.append(
                _diag(
                    "mcp_url_path_missing",
                    "task.toml",
                    f"{location}.url must specify an explicit non-empty endpoint path",
                )
            )
        if parsed.query or parsed.fragment:
            diagnostics.append(
                _diag(
                    "mcp_url_invalid",
                    "task.toml",
                    f"{location}.url may not contain query parameters or fragments",
                )
            )
        servers.append({
            "name": str(name),
            "transport": str(transport),
            "url": str(url_str),
            "host": host,
            "port": parsed.port,
            "path": parsed.path,
        })
    return servers


def _validate_verifier_collect(
    config: Mapping[str, Any],
    artifacts: list[str],
    diagnostics: list[Diagnostic],
) -> list[dict[str, str]]:
    verifier = config.get("verifier")
    if not isinstance(verifier, Mapping):
        return []
    raw_collect = verifier.get("collect")
    if raw_collect is None or raw_collect == []:
        return []
    if not isinstance(raw_collect, Sequence) or isinstance(raw_collect, (str, bytes)):
        diagnostics.append(
            _diag("collect_hooks_invalid", "task.toml", "[verifier.collect] must be a list of hook tables")
        )
        return []
    hooks: list[dict[str, str]] = []
    artifacts_set = set(artifacts)
    for idx, item in enumerate(raw_collect):
        location = f"verifier.collect[{idx}]"
        if not isinstance(item, Mapping):
            diagnostics.append(
                _diag("collect_hooks_invalid", "task.toml", f"{location} must be a table")
            )
            continue
        service = item.get("service")
        if service != "main":
            diagnostics.append(
                _diag(
                    "collect_service_invalid",
                    "task.toml",
                    f"{location}.service is {service!r}; only fixed service 'main' is permitted",
                )
            )
        cmd_raw = item.get("command")
        if not isinstance(cmd_raw, str) or not cmd_raw.strip():
            diagnostics.append(
                _diag("verifier_collect_unsupported", "task.toml", f"{location}.command must be a non-empty string")
            )
            continue
        command = cmd_raw.strip()
        m = (
            _COLLECT_GUARD_CP_PATTERN.match(command)
            or _COLLECT_SRC_GUARD_CP_PATTERN.match(command)
            or _COLLECT_TEST_CP_PATTERN.match(command)
            or _COLLECT_PLAIN_CP_PATTERN.match(command)
        )
        if not m:
            diagnostics.append(
                _diag(
                    "verifier_collect_unsupported",
                    "task.toml",
                    f"{location}.command {command!r} does not match deterministic copy-hook grammar",
                )
            )
            continue
        groups = m.groupdict()
        src = groups.get("src", "")
        dst = groups.get("dst", "")
        dst_guard = groups.get("dst_guard")
        src_guard = groups.get("src_guard")
        if dst_guard is not None and dst_guard != dst:
            diagnostics.append(
                _diag(
                    "verifier_collect_unsupported",
                    "task.toml",
                    f"{location}.command destination guard {dst_guard!r} does not match destination {dst!r}",
                )
            )
            continue
        if src_guard is not None and src_guard != src:
            diagnostics.append(
                _diag(
                    "verifier_collect_unsupported",
                    "task.toml",
                    f"{location}.command source guard {src_guard!r} does not match source {src!r}",
                )
            )
            continue
        pure_src = PurePosixPath(src)
        pure_dst = PurePosixPath(dst)
        safe_path = re.compile(r"^/[A-Za-z0-9._/-]+$")
        if (
            not safe_path.fullmatch(src)
            or not pure_src.is_absolute()
            or ".." in pure_src.parts
            or "//" in src
        ):
            diagnostics.append(
                _diag(
                    "verifier_collect_unsupported",
                    "task.toml",
                    f"{location}.command source path {src!r} must be an absolute normalized literal path",
                )
            )
            continue
        if (
            not safe_path.fullmatch(dst)
            or not pure_dst.is_absolute()
            or ".." in pure_dst.parts
            or "//" in dst
        ):
            diagnostics.append(
                _diag(
                    "verifier_collect_unsupported",
                    "task.toml",
                    f"{location}.command destination path {dst!r} must be an absolute normalized literal path",
                )
            )
            continue
        if any(part in FORBIDDEN_AGENT_IMAGE_PARTS for part in pure_src.parts):
            diagnostics.append(
                _diag(
                    "verifier_collect_unsupported",
                    "task.toml",
                    f"{location}.command source {src!r} exposes hidden task component",
                )
            )
            continue
        if dst not in artifacts_set:
            diagnostics.append(
                _diag(
                    "verifier_collect_unsupported",
                    "task.toml",
                    f"{location}.command destination {dst!r} is not declared in artifacts {sorted(artifacts_set)}",
                )
            )
            continue
        hooks.append({"service": "main", "command": command, "src": src, "dst": dst})
    return hooks


def _validate_verifier_env(
    config: Mapping[str, Any],
    source: CandidateSource,
    diagnostics: list[Diagnostic],
) -> list[str]:
    verifier = config.get("verifier")
    if not isinstance(verifier, Mapping):
        return []
    v_env = verifier.get("env")
    if v_env is None or v_env == {}:
        return []
    if not isinstance(v_env, Mapping):
        diagnostics.append(
            _diag("verifier_env_invalid", "task.toml", "[verifier.env] must be a table of environment variables")
        )
        return []
    allowed_credentials = set(source.credentials)
    validated_vars: list[str] = []
    for key, value in sorted(v_env.items()):
        location = f"verifier.env.{key}"
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_]+", key):
            diagnostics.append(
                _diag("verifier_env_invalid", "task.toml", f"{location} variable name is invalid")
            )
            continue
        if not isinstance(value, str):
            diagnostics.append(
                _diag("verifier_env_literal_secret", "task.toml", f"{location} value must be a placeholder string")
            )
            continue
        match = re.fullmatch(r"^\$\{([A-Za-z0-9_]+)\}$|^\$([A-Za-z0-9_]+)$", value.strip())
        if not match:
            diagnostics.append(
                _diag(
                    "verifier_env_literal_secret",
                    "task.toml",
                    f"{location} contains a literal value or complex interpolation {value!r}; only simple placeholders '${{VAR}}' are allowed",
                )
            )
            continue
        var_ref = match.group(1) or match.group(2)
        if var_ref not in allowed_credentials:
            diagnostics.append(
                _diag(
                    "verifier_credential_unauthorized",
                    "task.toml",
                    f"{location} references credential {var_ref!r} which is not declared in source metadata credentials {sorted(allowed_credentials)}",
                )
            )
            continue
        if key != var_ref:
            diagnostics.append(
                _diag(
                    "verifier_credential_alias_unsupported",
                    "task.toml",
                    f"{location} must use the same declared credential name on both sides of the placeholder",
                )
            )
            continue
        validated_vars.append(key)
    env_cfg = config.get("environment")
    if isinstance(env_cfg, Mapping):
        agent_env = env_cfg.get("env")
        if isinstance(agent_env, Mapping) and agent_env:
            diagnostics.append(
                _diag(
                    "agent_env_unsupported",
                    "task.toml",
                    "[environment].env must remain empty; credentials may not propagate to agent",
                )
            )
    sol_cfg = config.get("solution")
    if isinstance(sol_cfg, Mapping):
        sol_env = sol_cfg.get("env")
        if isinstance(sol_env, Mapping) and sol_env:
            diagnostics.append(
                _diag(
                    "solution_env_unsupported",
                    "task.toml",
                    "[solution].env must remain empty; credentials may not propagate to solution",
                )
            )
    return validated_vars


def _validate_build_context_contents(
    task_dir: Path,
    diagnostics: list[Diagnostic],
    build_proofs: Mapping[str, Any] | None = None,
    compose_topology: Mapping[str, Any] | None = None,
) -> None:
    """Refuse unmodelled configuration files, then content-scan the payload."""
    all_contexts = _derive_all_build_contexts(task_dir, compose_topology, diagnostics)
    nested_roots = [
        (task_dir / ctx).resolve()
        for ctx, _img in all_contexts
        if ctx not in ("environment", "tests")
    ]
    for context, image in all_contexts:
        root = task_dir / context
        if not root.is_dir() or root.is_symlink():
            continue
        dockerfile = f"{context}/Dockerfile"
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            resolved_path = path.resolve()
            if context == "environment" and any(_is_under(resolved_path, n_root) for n_root in nested_roots):
                continue
            relative = path.relative_to(task_dir).as_posix()
            if relative == dockerfile:
                continue
            if context == "environment" and path.name in {"docker-compose.yaml", "docker-compose.yml"}:
                continue
            if path.name in {"build-proof.json", "offline-build-proof.json"}:
                continue
            if path.suffix == ".whl":
                if build_proofs and context in build_proofs:
                    _verify_wheel_in_build_proof(path, root, build_proofs[context], diagnostics)
                    continue
                diagnostics.append(
                    _diag(
                        "build_context_unreadable",
                        relative,
                        f"binary wheel {path.name!r} is only permitted with a reviewed build proof",
                    )
                )
                continue
            unmodelled = _unmodelled_build_config(path.name, context)
            if unmodelled is not None:
                code, detail = unmodelled
                diagnostics.append(
                    _diag(code, relative, f"{detail}; refused in the build context for {image}")
                )
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                diagnostics.append(
                    _diag(
                        "build_context_unreadable",
                        relative,
                        f"every file in the build context for {image} must be readable UTF-8 "
                        "text so it can be scanned for build-time network use; ship "
                        "reviewable inputs instead",
                    )
                )
                continue
            if BUILD_NETWORK_PATTERN.search(text):
                unapproved = any(
                    BUILD_NETWORK_PATTERN.search(line)
                    and not (
                        build_proofs
                        and context in build_proofs
                        and _is_proven_offline_install(line)
                    )
                    for line in _docker_logical_lines(text)
                )
                if unapproved:
                    diagnostics.append(
                        _diag(
                            "build_network_use",
                            relative,
                            f"files in the build context for {image} may not fetch from a network "
                            "or invoke an online package manager; proof permits only exact "
                            "offline frozen installs",
                        )
                    )


def _verify_wheel_in_build_proof(
    path: Path,
    root: Path,
    proof: Mapping[str, Any],
    diagnostics: list[Diagnostic],
) -> None:
    """Verify a .whl file is covered by the build proof and has a matching hash."""
    rel = path.relative_to(root.parent).as_posix()
    pinned = proof.get("pinned_dependencies")
    wheel_hash = _sha256_file(path)
    if not isinstance(pinned, (Mapping, Sequence)):
        diagnostics.append(
            _diag("build_proof_invalid", rel, "build proof has no pinned dependencies for wheel verification")
        )
        return
    matched: dict[str, Any] | None = None
    if isinstance(pinned, Mapping):
        for name, value in pinned.items():
            if isinstance(value, Mapping):
                item = value
            else:
                item = {"name": name, "version": value, "hash": None, "wheel": None}
            if _wheel_matches_entry(path.name, item):
                matched = item
                break
    elif isinstance(pinned, Sequence):
        for item in pinned:
            if _wheel_matches_entry(path.name, item):
                matched = item
                break
    if matched is None:
        diagnostics.append(
            _diag(
                "build_proof_unpinned_dependency",
                rel,
                f"wheel {path.name!r} is not listed in the build proof",
            )
        )
        return
    declared_hash = matched.get("hash") or matched.get("sha256")
    if declared_hash and str(declared_hash).lower().removeprefix("sha256:") != wheel_hash.removeprefix("sha256:"):
        diagnostics.append(
            _diag(
                "build_proof_invalid",
                rel,
                f"wheel {path.name!r} digest {wheel_hash} does not match proof {declared_hash}",
            )
        )


def _wheel_matches_entry(wheel_name: str, item: Mapping[str, Any]) -> bool:
    """Check whether a wheel filename matches a pinned dependency entry."""
    if not isinstance(item, Mapping):
        return False
    declared_wheel = item.get("wheel")
    if isinstance(declared_wheel, str):
        return wheel_name == declared_wheel or wheel_name == declared_wheel.rsplit("/", 1)[-1]
    name = str(item.get("name", "")).replace("-", "_")
    version = str(item.get("version", ""))
    if not name or not version:
        return False
    normalized = name.replace("-", "_").lower()
    wheel_stem = wheel_name.lower().rsplit(".whl", 1)[0]
    # wheel naming: name-version[-build?]-python-abi-platform.whl
    return wheel_stem.startswith(f"{normalized}-{version}")


def _is_remote_docker_source(source: str) -> bool:
    normalized = source.strip().lower()
    return bool(
        re.match(r"^(?:https?|ftp|git|ssh)://", normalized)
        or normalized.startswith("git@")
    )


def _validate_dockerfile(
    task_dir: Path, diagnostics: list[Diagnostic], has_proof: bool = False
) -> str | None:
    path = task_dir / "environment/Dockerfile"
    if not path.is_file():
        return None
    text = _read_text(path)
    _validate_build_network(text, "environment/Dockerfile", diagnostics, has_proof=has_proof)
    base_ref: str | None = None
    for line in _docker_logical_lines(text):
        if line.startswith("#"):
            continue
        if re.match(r"(?i)^FROM\s+", line):
            parts = shlex.split(line)
            references = [
                part for part in parts[1:] if not part.startswith("--") and part.upper() != "AS"
            ]
            if references:
                base_ref = references[0]
                if base_ref != "scratch" and not re.search(r"@sha256:[0-9a-f]{64}$", base_ref):
                    diagnostics.append(
                        _diag(
                            "base_image_unpinned",
                            "environment/Dockerfile",
                            "every FROM image must be pinned by @sha256 digest",
                        )
                    )
        copy_match = re.match(r"(?i)^(COPY|ADD)\s+(.+)$", line)
        if copy_match:
            instruction = copy_match.group(1).upper()
            sources, unsupported = _docker_copy_sources(copy_match.group(2))
            if unsupported:
                diagnostics.append(
                    _diag(
                        "agent_image_copy_unsupported",
                        "environment/Dockerfile",
                        "COPY/ADD syntax must be statically resolvable",
                    )
                )
            for source in sources:
                if instruction == "ADD" and _is_remote_docker_source(source):
                    diagnostics.append(
                        _diag(
                            "remote_docker_add",
                            "environment/Dockerfile",
                            "remote Docker ADD sources are forbidden even when checksummed",
                        )
                    )
                pure = PurePosixPath(source)
                if (
                    source in {".", "./"}
                    or pure.is_absolute()
                    or ".." in pure.parts
                    or bool(re.search(r"[$*?\[\]{}]", source))
                    or any(part in FORBIDDEN_AGENT_IMAGE_PARTS for part in pure.parts)
                ):
                    diagnostics.append(
                        _diag(
                            "agent_image_hidden_leak",
                            "environment/Dockerfile",
                            "COPY/ADD may not include hidden components, '.', or escaping paths",
                        )
                    )

    normalized = text.replace("\\\n", " ")
    if not has_proof:
        for match in re.finditer(r"\bpip(?:3)?\s+install\s+([^;&\n]+)", normalized, re.IGNORECASE):
            try:
                dependencies = [
                    item for item in shlex.split(match.group(1)) if not item.startswith("-")
                ]
            except ValueError:
                dependencies = []
            for dependency in dependencies:
                if "==" not in dependency and "@" not in dependency:
                    diagnostics.append(
                        _diag(
                            "dependency_unpinned",
                            "environment/Dockerfile",
                            "pip dependencies must use exact == or immutable @ pins",
                        )
                    )
                    break
        for match in re.finditer(r"\bapt(?:-get)?\s+install\s+([^;&\n]+)", normalized, re.IGNORECASE):
            try:
                dependencies = [
                    item for item in shlex.split(match.group(1)) if not item.startswith("-")
                ]
            except ValueError:
                dependencies = []
            for dependency in dependencies:
                if dependency in {"install"}:
                    continue
                if "=" not in dependency:
                    diagnostics.append(
                        _diag(
                            "dependency_unpinned",
                            "environment/Dockerfile",
                            "apt dependencies must use exact package=version pins",
                        )
                    )
                    break
    return base_ref


def _validate_verifier_image(
    task_dir: Path, diagnostics: list[Diagnostic], has_proof: bool = False
) -> None:
    path = task_dir / "tests/Dockerfile"
    if not path.is_file():
        return
    text = _read_text(path)
    _validate_build_network(text, "tests/Dockerfile", diagnostics, has_proof=has_proof)
    found_from = False
    for line in _docker_logical_lines(text):
        if line.startswith("#") or not re.match(r"(?i)^FROM\s+", line):
            continue
        found_from = True
        parts = shlex.split(line)
        references = [
            part for part in parts[1:] if not part.startswith("--") and part.upper() != "AS"
        ]
        if (
            references
            and references[0] != "scratch"
            and not re.search(r"@sha256:[0-9a-f]{64}$", references[0])
        ):
            diagnostics.append(
                _diag(
                    "verifier_image_unpinned",
                    "tests/Dockerfile",
                    "the separate verifier FROM image must be pinned by @sha256 digest",
                )
            )
    if not found_from:
        diagnostics.append(
            _diag(
                "verifier_image_invalid",
                "tests/Dockerfile",
                "separate verifier Dockerfile must declare a FROM image",
            )
        )

def _effective_verifier_network(config: Mapping[str, Any]) -> tuple[str, str, str]:
    """Resolve the verifier's effective network modes the way Harbor 0.21.0 does.

    Harbor discards `extra_docker_compose` when it builds the separate verifier
    runtime config, so the workbench's no-network overlay never reaches the
    verifier container. Its exposure is therefore whatever `task.toml` declares:

    * baseline (applies from container start) — `[verifier.environment]` when that
      table exists, otherwise a deep copy of `[environment]`. `EnvironmentConfig`
      defaults `network_mode` to `public`, so a table that omits the key does not
      inherit the other table's value.
    * phase (applies during `verify()`) — the explicit `[verifier].network_mode`
      override when set, otherwise the baseline.

    This is a mirror, not a call: Harbor ships as a standalone CLI tool rather
    than a library this package imports, so the resolver cannot be reused in
    process. The mirrored symbols are
    `harbor.models.task.verifier_mode.resolve_effective_verifier_env_config`,
    `harbor.models.task.config.BaselineNetworkPolicyConfig.resolve_baseline`, and
    `harbor.trial.network_policy.resolve_verifier_phase_policy` (Harbor 0.21.0).
    `test_verifier_network_resolution_matches_harbor` executes those three
    against the installed Harbor and fails if this mirror drifts from them.

    Both this function and Harbor's resolver take a step argument that this one
    omits, because `_validate_supported_configuration` refuses `[[steps]]`
    outright: Harbor resolves step-first, and v1 does not model a multi-step
    trial. The mirror is only valid for the step-less case it is given.

    Returns `(baseline, phase, baseline_origin)`.
    """
    environment = config.get("environment")
    environment_table = environment if isinstance(environment, Mapping) else {}
    verifier = config.get("verifier")
    verifier_table = verifier if isinstance(verifier, Mapping) else {}
    verifier_environment = verifier_table.get("environment")
    if isinstance(verifier_environment, Mapping):
        declared = verifier_environment.get("network_mode")
        origin = "[verifier.environment]"
    else:
        declared = environment_table.get("network_mode")
        origin = "[environment] (inherited)"
    baseline = declared if isinstance(declared, str) else "public"
    override = verifier_table.get("network_mode")
    phase = override if isinstance(override, str) else baseline
    return baseline, phase, origin


def _validate_network_and_isolation(
    config: Mapping[str, Any], task_dir: Path, diagnostics: list[Diagnostic]
) -> None:
    environment = config.get("environment")
    environment_table = environment if isinstance(environment, dict) else {}
    network_mode = environment_table.get("network_mode")
    if network_mode not in {"no-network", "public", "allowlist"}:
        diagnostics.append(
            _diag(
                "network_policy_invalid",
                "task.toml",
                "[environment].network_mode must be explicit and supported",
            )
        )
    # A declared [environment].docker_image makes Harbor skip the build entirely
    # (should_use_prebuilt_docker_image returns True whenever it is set), so the
    # reviewed environment/Dockerfile is never built and the overlay's
    # build.network=none never applies. Refuse rather than certify an image the
    # workbench did not see built.
    if environment_table.get("docker_image") is not None:
        diagnostics.append(
            _diag(
                "prebuilt_image_unsupported",
                "task.toml",
                "[environment].docker_image bypasses the reviewed environment/Dockerfile "
                "build and the workbench's build-time network denial",
            )
        )
    verifier = config.get("verifier")
    verifier_table = verifier if isinstance(verifier, dict) else {}
    if verifier_table.get("environment_mode") != "separate":
        diagnostics.append(
            _diag(
                "verifier_not_isolated",
                "task.toml",
                "[verifier].environment_mode must be 'separate'",
            )
        )
    declared_modes = [
        value
        for value in (
            verifier_table.get("network_mode"),
            verifier_table.get("environment", {}).get("network_mode")
            if isinstance(verifier_table.get("environment"), Mapping)
            else None,
        )
        if value is not None
    ]
    if any(value not in {"no-network", "public", "allowlist"} for value in declared_modes):
        diagnostics.append(
            _diag(
                "verifier_network_invalid",
                "task.toml",
                "verifier network_mode is invalid",
            )
        )
    baseline, _phase, origin = _effective_verifier_network(config)
    if baseline != "no-network":
        diagnostics.append(
            _diag(
                "verifier_network_not_isolated",
                "task.toml",
                "the separate verifier environment must declare network_mode='no-network'; "
                f"the effective baseline is '{baseline}' from {origin}. Harbor drops the "
                "workbench no-network overlay for the verifier container, so a networked "
                "verifier can exfiltrate hidden inputs and can make verifier_deterministic "
                "an artifact of a stable remote response",
            )
        )
    phase_override = verifier_table.get("network_mode")
    if isinstance(phase_override, str) and phase_override != "no-network":
        diagnostics.append(
            _diag(
                "verifier_phase_network_not_isolated",
                "task.toml",
                "[verifier].network_mode reopens the network for the verification phase; "
                f"the effective phase policy is '{phase_override}' and must be 'no-network'",
            )
        )
    # Task-authored Compose files are refused by `_validate_build_context_contents`
    # for every build context, not by exact path here: the same file under
    # `tests/` reaches the separate verifier's build and `up`, and refusing only
    # `environment/docker-compose.yaml` left that open.

    for root_name in ("tests", "verifier", "solution"):
        root = task_dir / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            text = _read_text(path)
            relative = path.relative_to(task_dir).as_posix()
            if any(
                NETWORK_SCRIPT_PATTERN.search(line)
                and not _is_proven_offline_install(line)
                for line in _docker_logical_lines(text)
            ):
                diagnostics.append(
                    _diag(
                        "runtime_network_use",
                        relative,
                        "control/verifier scripts may not fetch or install over "
                        "the network at runtime",
                    )
                )
            if root_name in {"tests", "verifier"} and NONDETERMINISM_PATTERN.search(text):
                diagnostics.append(
                    _diag(
                        "verifier_nondeterminism_static",
                        relative,
                        "verifier references a nondeterministic clock/random source",
                    )
                )


def _sensitive_lines(task_dir: Path) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    roots = (task_dir / "solution", task_dir / "tests", task_dir / "verifier")
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if path.name in {"Dockerfile", "test.sh", "evaluate.py", "build-proof.json", "offline-build-proof.json", "requirements.txt"}:
                # Verifier-image plumbing and build proofs are not golden task content.
                continue
            if path.suffix in {".whl", ".tar.gz", ".zip"}:
                continue
            relative = path.relative_to(task_dir).as_posix()
            text = _read_text(path)
            if "fixtures" not in path.relative_to(root).parts:
                for line in text.splitlines():
                    normalized = " ".join(line.strip().split())
                    if (
                        len(normalized) >= 32
                        and not normalized.startswith(("#", "//", "/*", "*"))
                        and re.search(r"[A-Za-z]", normalized)
                    ):
                        candidates.append((relative, normalized))
            if any(token in path.name.lower() for token in ("golden", "expected", "answer")):
                compact = " ".join(text.split())
                if len(compact) >= 12:
                    candidates.append((relative, compact))
    return candidates


def _validate_golden_leak(task_dir: Path, diagnostics: list[Diagnostic]) -> None:
    visible_paths: list[Path] = []
    for relative in ("instruction.md", "instructions.md"):
        path = task_dir / relative
        if path.is_file():
            visible_paths.append(path)
    environment = task_dir / "environment"
    if environment.exists():
        visible_paths.extend(
            path
            for path in sorted(environment.rglob("*"))
            if path.is_file() and not path.is_symlink()
        )
    visible = "\n".join(" ".join(_read_text(path).split()) for path in visible_paths)
    for source_path, span in _sensitive_lines(task_dir):
        if span and span in visible:
            diagnostics.append(
                _diag(
                    "golden_data_leak",
                    source_path,
                    "a hidden solution/verifier span is present in agent-visible task bytes",
                )
            )
            break


def _validate_test_contract(task_dir: Path, diagnostics: list[Diagnostic]) -> None:
    test_script = task_dir / "tests/test.sh"
    if test_script.is_file():
        text = _read_text(test_script)
        verifier_text = "\n".join(
            _read_text(path)
            for path in sorted((task_dir / "tests").rglob("*"))
            if path.is_file() and not path.is_symlink()
        )
        writes_reward = "/logs/verifier/reward" in verifier_text or (
            "/logs/verifier" in verifier_text
            and ("reward.json" in verifier_text or "reward.txt" in verifier_text)
        )
        if not writes_reward:
            diagnostics.append(
                _diag(
                    "verifier_reward_missing",
                    "tests/test.sh",
                    "verifier must write an absolute /logs/verifier/reward output",
                )
            )
        if "/tests/" not in text:
            diagnostics.append(
                _diag(
                    "verifier_path_relative",
                    "tests/test.sh",
                    "verifier entrypoint must use an absolute /tests path",
                )
            )
    instruction = task_dir / "instruction.md"
    if instruction.is_file() and not instruction.read_text(encoding="utf-8").strip():
        diagnostics.append(_diag("instruction_empty", "instruction.md", "instruction is empty"))


def _adversarial_scripts(task_dir: Path, diagnostics: list[Diagnostic]) -> list[Path]:
    root = task_dir / "workbench/adversarial"
    scripts = sorted(root.glob("*.sh")) if root.is_dir() else []
    if len(scripts) < MIN_ADVERSARIAL_CASES:
        diagnostics.append(
            _diag(
                "adversarial_cases_insufficient",
                "workbench/adversarial",
                f"at least {MIN_ADVERSARIAL_CASES} invalid-solution .sh probes are required",
            )
        )
    for path in scripts:
        if not SAFE_SLUG.fullmatch(path.stem):
            diagnostics.append(
                _diag(
                    "adversarial_name_invalid",
                    path.relative_to(task_dir).as_posix(),
                    "adversarial case names must be safe lowercase slugs",
                )
            )
        if not os.access(path, os.X_OK):
            diagnostics.append(
                _diag(
                    "script_not_executable",
                    path.relative_to(task_dir).as_posix(),
                    "adversarial solution must be executable",
                )
            )
    return scripts


def _special_control_scripts(
    task_dir: Path, diagnostics: list[Diagnostic]
) -> tuple[Path, Path]:
    oracle = task_dir / "solution/solve.sh"
    fair = task_dir / "workbench/fair-alternative.sh"
    please_hack = task_dir / "workbench/please-hack.sh"
    for label, path in (("fair alternative", fair), ("please-hack replay", please_hack)):
        relative = path.relative_to(task_dir).as_posix()
        if not path.is_file():
            diagnostics.append(
                _diag(
                    "required_control_missing",
                    relative,
                    f"executable {label} solver is required",
                )
            )
        elif not os.access(path, os.X_OK):
            diagnostics.append(
                _diag("script_not_executable", relative, f"{label} solver must be executable")
            )
    if fair.is_file() and oracle.is_file():
        if fair.read_bytes() == oracle.read_bytes():
            diagnostics.append(
                _diag(
                    "fair_alternative_not_distinct",
                    "workbench/fair-alternative.sh",
                    "fair alternative must be byte-distinct from the oracle",
                )
            )
        forbidden = re.compile(r"(?:^|[/ ])(?:tests|solution|workbench)(?:[/ ]|$)|golden", re.I)
        if forbidden.search(_read_text(fair)):
            diagnostics.append(
                _diag(
                    "fair_alternative_hidden_dependency",
                    "workbench/fair-alternative.sh",
                    "fair alternative may use only the instruction and "
                    "environment-visible contract",
                )
            )
    if (
        please_hack.is_file()
        and oracle.is_file()
        and please_hack.read_bytes() == oracle.read_bytes()
    ):
        diagnostics.append(
            _diag(
                "please_hack_not_invalid",
                "workbench/please-hack.sh",
                "please-hack replay must be byte-distinct from the valid oracle",
            )
        )
    return fair, please_hack


def _detect_forged_registration(
    repo_root: Path,
    task_dir: Path,
    task_id: str,
    config: Mapping[str, Any],
    diagnostics: list[Diagnostic],
) -> dict[str, Any]:
    task_relative = _repo_relative(task_dir, repo_root)
    if task_relative.startswith("library/registry/") or task_relative.startswith("registered/"):
        diagnostics.append(
            _diag(
                "forged_registration",
                task_relative,
                "candidate packages cannot occupy registry or registered namespaces",
            )
        )
    suspicious_names = {"registration.json", "registry.json", ".registered"}
    for path in sorted(task_dir.rglob("*")):
        if path.is_file() and path.name in suspicious_names:
            diagnostics.append(
                _diag(
                    "forged_registration",
                    path.relative_to(task_dir).as_posix(),
                    "candidate-local files cannot assert registration",
                )
            )
    metadata = config.get("metadata")
    if isinstance(metadata, dict) and (
        metadata.get("state") == "registered" or metadata.get("registered") is True
    ):
        diagnostics.append(
            _diag(
                "forged_registration",
                "task.toml",
                "task metadata cannot self-assert registered state",
            )
        )

    record_path = repo_root / "library/registry" / f"{task_id}.json"
    observation: dict[str, Any] = {
        "record_present": record_path.is_file(),
        "state": "unregistered",
        "record_digest": _sha256_file(record_path) if record_path.is_file() else None,
        "path_matches": False,
    }
    if record_path.is_file():
        try:
            value = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            observation["state"] = "malformed"
        else:
            if isinstance(value, dict):
                state = value.get("state")
                observation["state"] = state if isinstance(state, str) else "malformed"
                observation["path_matches"] = value.get("task_path") == task_relative
    return observation


def _control_command(candidate_id: str, task_id: str, entry_id: str, agent: str) -> tuple[str, ...]:
    safe_task = re.sub(r"[^a-z0-9-]+", "-", task_id.lower()).strip("-") or "task"
    safe_task = safe_task[-24:]
    job_name = f"m007-{safe_task}-{candidate_id[-8:]}-{entry_id}"
    staging_root = f"$REPO/runs/task-workbench/{candidate_id}/staging"
    staged_task = f"{staging_root}/{entry_id}"
    jobs = f"$REPO/runs/task-workbench/{candidate_id}/jobs"
    network_overlay = f"{staged_task}/{NETWORK_OVERLAY_RELATIVE}"
    return (
        "harbor",
        "run",
        "--path",
        staging_root,
        "--include-task-name",
        entry_id,
        "--agent",
        agent,
        "--env",
        "docker",
        "--extra-docker-compose",
        network_overlay,
        "--job-name",
        job_name,
        "--jobs-dir",
        jobs,
        "--n-concurrent",
        "1",
        "--n-attempts",
        "1",
        "-y",
    )


def _build_control_plan(
    candidate_id: str,
    task_id: str,
    task_dir: Path,
    adversarial: Sequence[Path],
    fair_alternative: Path,
    please_hack: Path,
) -> tuple[ControlPlanEntry, ...]:
    specs: list[
        tuple[
            str,
            Literal["oracle", "nop", "adversarial", "fair_alternative", "please_hack"],
            Literal["oracle", "nop"],
            float,
            str | None,
        ]
    ] = []
    for index in range(1, ORACLE_REPETITIONS + 1):
        specs.append((f"oracle-{index}", "oracle", "oracle", 1.0, None))
    specs.append(
        (
            "fair-alternative",
            "fair_alternative",
            "oracle",
            1.0,
            fair_alternative.relative_to(task_dir).as_posix(),
        )
    )
    for index in range(1, NOP_REPETITIONS + 1):
        specs.append((f"nop-{index}", "nop", "nop", 0.0, None))
    for path in adversarial:
        relative = path.relative_to(task_dir).as_posix()
        specs.append((f"adversarial-{path.stem}", "adversarial", "oracle", 0.0, relative))
    specs.append(
        (
            "please-hack",
            "please_hack",
            "oracle",
            0.0,
            please_hack.relative_to(task_dir).as_posix(),
        )
    )
    entries: list[ControlPlanEntry] = []
    for control_id, kind, agent, expected, mutation_path in specs:
        command = _control_command(candidate_id, task_id, control_id, agent)
        entries.append(
            ControlPlanEntry(
                control_id=control_id,
                kind=kind,
                agent=agent,
                expected_reward=expected,
                mutation_path=mutation_path,
                command=command,
                command_digest=_sha256_bytes(_canonical_bytes(list(command))),
            )
        )
    return tuple(entries)


def inspect_candidate(*, repo_root: Path, task_path: Path, source: CandidateSource) -> Inspection:
    repo_root = repo_root.resolve()
    task_dir = task_path if task_path.is_absolute() else repo_root / task_path
    if not _is_under(task_dir, repo_root):
        raise UnsafePathError(f"candidate path escapes repository: {task_path}")
    task_dir = task_dir.resolve()
    if not task_dir.is_dir():
        raise WorkbenchError(f"candidate directory is missing: {task_path}")

    diagnostics: list[Diagnostic] = []
    _validate_source(source, diagnostics)
    _validate_layout(task_dir, diagnostics)
    config = _parse_task_toml(task_dir / "task.toml", diagnostics)
    _validate_supported_configuration(config, diagnostics)
    task_name, task_version, keywords = _validate_task_metadata(config, task_dir, diagnostics)
    artifacts = _validate_timeouts_and_artifacts(config, diagnostics)
    compose_topology, sidecar_name = _validate_compose_topology(
        task_dir, diagnostics, credentials=source.credentials
    )
    build_proofs = _validate_offline_build_proofs(
        task_dir, diagnostics, compose_topology=compose_topology
    )
    mcp_servers = _validate_mcp_servers(config, sidecar_name, diagnostics)
    collect_hooks = _validate_verifier_collect(config, artifacts, diagnostics)
    _validate_verifier_env(config, source, diagnostics)
    base_image = _validate_dockerfile(
        task_dir, diagnostics, has_proof=("environment" in build_proofs)
    )
    _validate_build_context_contents(
        task_dir, diagnostics, build_proofs, compose_topology=compose_topology
    )
    _validate_verifier_image(
        task_dir, diagnostics, has_proof=("tests" in build_proofs)
    )
    _validate_network_and_isolation(config, task_dir, diagnostics)
    verifier_baseline, verifier_phase, _verifier_origin = _effective_verifier_network(config)
    _validate_golden_leak(task_dir, diagnostics)
    _validate_test_contract(task_dir, diagnostics)
    adversarial = _adversarial_scripts(task_dir, diagnostics)
    fair_alternative, please_hack = _special_control_scripts(task_dir, diagnostics)

    task_id = task_name.rsplit("/", 1)[-1]
    if not _is_valid_harbor_package_name(task_name):
        # The declared package name is not a valid Harbor identity. Keep a
        # deterministic, safe local task_id so error records and commands still
        # have a stable suffix.
        task_id = re.sub(r"[^a-z0-9-]+", "-", task_dir.name.lower()).strip("-") or "task"
    registration = _detect_forged_registration(repo_root, task_dir, task_id, config, diagnostics)

    package_entries = _tree_entries(task_dir)
    files = [
        {
            "path": path,
            "role": _role_for_path(path),
            "type": entry_type,
            "size_bytes": size,
            "digest": digest,
        }
        for path, entry_type, size, digest in package_entries
    ]
    leakage_scan = [
        {"path": path, "line_digest": _sha256_bytes(line.encode())}
        for path, line in _sensitive_lines(task_dir)
    ]
    digests = {
        "package": _tree_digest_from_entries(package_entries),
        "registry_package": _registry_package_digest_from_entries(package_entries),
        "task_toml": _subpath_digest(task_dir / "task.toml"),
        "instruction": _subpath_digest(task_dir / "instruction.md"),
        "image_definition": _subpath_digest(task_dir / "environment"),
        "solution": _subpath_digest(task_dir / "solution"),
        "verifier": _subpath_digest(task_dir / "tests"),
        "adversarial_controls": _subpath_digest(task_dir / "workbench/adversarial"),
        "fair_alternative": _subpath_digest(fair_alternative),
        "please_hack": _subpath_digest(please_hack),
        "leakage_scan": _sha256_bytes(_canonical_bytes(leakage_scan)),
        "artifact_config": _sha256_bytes(_canonical_bytes(artifacts)),
        "source_metadata": _sha256_bytes(_canonical_bytes(source.to_dict())),
    }
    identity = {
        "workbench_version": WORKBENCH_VERSION,
        "task_id": task_id,
        "task_version": task_version,
        "task_path": _repo_relative(task_dir, repo_root),
        "source": source.to_dict(),
        "package_digest": digests["package"],
    }
    candidate_id = "candidate-" + hashlib.sha256(_canonical_bytes(identity)).hexdigest()[:24]
    plan = _build_control_plan(
        candidate_id,
        task_id,
        task_dir,
        adversarial,
        fair_alternative,
        please_hack,
    )
    network_record = (
        compose_topology.get("network")
        if isinstance(compose_topology, Mapping)
        else None
    )
    network_name = (
        network_record.get("name") if isinstance(network_record, Mapping) else None
    )
    overlay_network_name = network_name if sidecar_name is not None else None
    control_overlay = _network_overlay_content(
        sidecar_name,
        volume=compose_topology.get("volume") if isinstance(compose_topology, Mapping) else None,
        network_name=overlay_network_name,
    )
    control_network = (
        network_name
        if sidecar_name is not None and network_name is not None
        else "workbench-internal"
    )
    candidate: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "task_workbench_candidate",
        "candidate_id": candidate_id,
        "workbench_version": WORKBENCH_VERSION,
        "task_id": task_id,
        "task_name": task_name,
        "task_version": task_version,
        "task_path": identity["task_path"],
        "source": source.to_dict(),
        "declared_base_image": base_image,
        "network_policy": {
            "environment": (
                config.get("environment", {}).get("network_mode")
                if isinstance(config.get("environment"), dict)
                else None
            ),
            "verifier": (
                config.get("verifier", {}).get("network_mode")
                if isinstance(config.get("verifier"), dict)
                else None
            ),
            "verifier_effective_baseline": verifier_baseline,
            "verifier_effective_phase": verifier_phase,
            "agent_build_network": "denied by overlay build.network=none",
            "agent_runtime_network": (
                f"isolated on {control_network} (internal: true)"
                if sidecar_name is not None
                else "denied by overlay network_mode=none"
            ),
            "verifier_build_network": "static scan of tests/ only; overlay not applied",
            "verifier_runtime_network": "declared in task.toml; overlay not applied",
            "control_enforcement": (
                f"docker-compose main + sidecar on {control_network} with build.network=none"
                if sidecar_name is not None
                else "docker-compose main build.network=none and network_mode=none"
            ),
            "control_overlay_digest": _sha256_bytes(control_overlay),
        },
        "keywords": keywords,
        "artifacts": artifacts,
        "digests": digests,
        "files": files,
        "generator_identity": {
            "code_digest": digests["solution"],
            "execution": "local",
            "model": None,
            "prompt_digest": None,
        },
        "validator_identity": {
            "code_digest": digests["verifier"],
            "execution": "local",
            "model": None,
            "prompt_digest": None,
        },
        "leakage_scan": {
            "digest": digests["leakage_scan"],
            "scanned_span_count": len(leakage_scan),
        },
        "registration_observation": registration,
        "admission_boundary": {
            "candidate_only": True,
            "can_queue": False,
            "can_register": False,
            "can_freeze": False,
            "can_publish": False,
            "can_edit_policy": False,
            "required_next_actor": "human-created library/registry record",
        },
    }
    if compose_topology is not None:
        candidate["compose_topology"] = compose_topology
    if mcp_servers:
        candidate["mcp_servers"] = mcp_servers
    if build_proofs:
        candidate["offline_build_proofs"] = build_proofs
    if collect_hooks:
        candidate["collect_hooks"] = collect_hooks
    if source.credentials:
        candidate["credentials"] = list(source.credentials)
    candidate["candidate_record_digest"] = _sha256_bytes(_canonical_bytes(candidate))
    return Inspection(
        candidate=candidate,
        diagnostics=_sort_diagnostics(diagnostics),
        control_plan=plan,
    )


def _materialize_command(command: Sequence[str], repo_root: Path) -> tuple[str, ...]:
    prefix = "$REPO/"
    return tuple(
        str(repo_root / item.removeprefix(prefix)) if item.startswith(prefix) else item
        for item in command
    )


def _validate_candidate_record(candidate: Mapping[str, Any]) -> None:
    recorded_digest = _required_digest(candidate, "candidate_record_digest")
    body = dict(candidate)
    body.pop("candidate_record_digest")
    if recorded_digest != _sha256_bytes(_canonical_bytes(body)):
        raise WorkbenchError("frozen candidate record digest is invalid")

    identity = {
        "workbench_version": _required_string(candidate, "workbench_version"),
        "task_id": _required_string(candidate, "task_id"),
        "task_version": _required_string(candidate, "task_version"),
        "task_path": _required_string(candidate, "task_path"),
        "source": dict(_required_mapping(candidate.get("source"), "source")),
        "package_digest": _required_digest(
            _required_mapping(candidate.get("digests"), "digests"), "package"
        ),
    }
    expected_id = "candidate-" + hashlib.sha256(_canonical_bytes(identity)).hexdigest()[:24]
    if _required_string(candidate, "candidate_id") != expected_id:
        raise WorkbenchError("frozen candidate identity digest is invalid")


def _candidate_task_dir(repo_root: Path, candidate: Mapping[str, Any]) -> Path:
    relative = PurePosixPath(_required_string(candidate, "task_path"))
    if relative.is_absolute() or ".." in relative.parts:
        raise UnsafePathError("frozen candidate task_path is unsafe")
    task_dir = (repo_root / Path(*relative.parts)).resolve()
    if not _is_under(task_dir, repo_root) or not task_dir.is_dir():
        raise UnsafePathError("frozen candidate task_path is missing or escapes the repository")
    return task_dir


def _actual_candidate_files(task_dir: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path,
            "role": _role_for_path(path),
            "type": entry_type,
            "size_bytes": size,
            "digest": digest,
        }
        for path, entry_type, size, digest in _tree_entries(task_dir)
    ]


def _validate_candidate_bytes(
    *, repo_root: Path, task_dir: Path, candidate: Mapping[str, Any]
) -> None:
    _validate_candidate_record(candidate)
    expected_task_dir = _candidate_task_dir(repo_root, candidate)
    if task_dir.resolve() != expected_task_dir:
        raise WorkbenchError("control task_path does not match the frozen Inspection path")
    symlinks = [
        path.relative_to(task_dir).as_posix()
        for path in sorted(task_dir.rglob("*"))
        if path.is_symlink()
    ]
    if symlinks:
        raise WorkbenchError(f"candidate bytes contain forbidden symlink: {symlinks[0]}")
    digests = _required_mapping(candidate.get("digests"), "digests")
    if _tree_digest(task_dir) != _required_digest(digests, "package"):
        raise WorkbenchError("candidate package bytes drifted after Inspection")
    raw_files = candidate.get("files")
    if not isinstance(raw_files, list) or raw_files != _actual_candidate_files(task_dir):
        raise WorkbenchError("candidate file manifest drifted after Inspection")


def _source_from_candidate(candidate: Mapping[str, Any]) -> CandidateSource:
    raw = _required_mapping(candidate.get("source"), "source")
    zone = _required_string(raw, "provenance_zone")
    if zone not in {"01-external", "02-local-evidence", "03-synthetic", "04-curated"}:
        raise WorkbenchError("frozen source provenance_zone is invalid")
    return CandidateSource.from_dict(raw)


def _reinspect_frozen_candidate(
    *, inspection: Inspection, repo_root: Path, task_dir: Path
) -> None:
    _validate_candidate_bytes(
        repo_root=repo_root,
        task_dir=task_dir,
        candidate=inspection.candidate,
    )
    current = inspect_candidate(
        repo_root=repo_root,
        task_path=task_dir,
        source=_source_from_candidate(inspection.candidate),
    )
    if current != inspection:
        raise WorkbenchError("candidate Inspection or frozen control plan drifted before execution")


def _validate_backend_plan(candidate: Mapping[str, Any], plan: ControlPlanEntry) -> None:
    candidate_id = _required_string(candidate, "candidate_id")
    task_id = _required_string(candidate, "task_id")
    expected_kind: str
    expected_agent: str
    expected_reward: float
    expected_mutation: str | None
    if plan.control_id in {f"oracle-{index}" for index in range(1, ORACLE_REPETITIONS + 1)}:
        expected_kind, expected_agent, expected_reward, expected_mutation = (
            "oracle",
            "oracle",
            1.0,
            None,
        )
    elif plan.control_id == "fair-alternative":
        expected_kind, expected_agent, expected_reward, expected_mutation = (
            "fair_alternative",
            "oracle",
            1.0,
            "workbench/fair-alternative.sh",
        )
    elif plan.control_id in {f"nop-{index}" for index in range(1, NOP_REPETITIONS + 1)}:
        expected_kind, expected_agent, expected_reward, expected_mutation = (
            "nop",
            "nop",
            0.0,
            None,
        )
    elif plan.control_id.startswith("adversarial-"):
        stem = plan.control_id.removeprefix("adversarial-")
        expected_kind, expected_agent, expected_reward, expected_mutation = (
            "adversarial",
            "oracle",
            0.0,
            f"workbench/adversarial/{stem}.sh",
        )
    elif plan.control_id == "please-hack":
        expected_kind, expected_agent, expected_reward, expected_mutation = (
            "please_hack",
            "oracle",
            0.0,
            "workbench/please-hack.sh",
        )
    else:
        raise WorkbenchError("control plan id is outside the fixed control set")
    if expected_mutation is not None:
        raw_files = candidate.get("files")
        if not isinstance(raw_files, list) or not any(
            isinstance(item, Mapping)
            and item.get("path") == expected_mutation
            and item.get("type") == "file"
            for item in raw_files
        ):
            raise WorkbenchError("control mutation does not name a frozen candidate file")

    if (
        plan.kind != expected_kind
        or plan.agent != expected_agent
        or plan.expected_reward != expected_reward
        or plan.mutation_path != expected_mutation
        or plan.concurrency != 1
    ):
        raise WorkbenchError("control plan semantics drifted from the fixed control set")
    expected_command = _control_command(candidate_id, task_id, plan.control_id, plan.agent)
    expected_digest = _sha256_bytes(_canonical_bytes(list(expected_command)))
    if plan.command != expected_command or plan.command_digest != expected_digest:
        raise WorkbenchError("control command drifted from the backend's fixed command")


def _scrub_diagnostic(value: str, repo_root: Path, *, limit: int = 2_000) -> str:
    text = value.replace(str(repo_root), "$REPO")
    return text[-limit:]


def _reward_vector_from_trial(result: Mapping[str, Any]) -> dict[str, float]:
    verifier = result.get("verifier_result")
    rewards = verifier.get("rewards") if isinstance(verifier, Mapping) else None
    if not isinstance(rewards, Mapping):
        return {}
    return {
        str(key): float(value)
        for key, value in rewards.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }


def _runner_failure_classification(message: str) -> Classification:
    normalized = message.lower()
    task_markers = (
        "dockerfile",
        "environmentbuilderror",
        "failed to build",
        "failed to solve",
        "imagepullerror",
        "invalid task",
        "taskconfigerror",
        "taskvalidationerror",
    )
    infrastructure_markers = (
        "cannot connect to the docker daemon",
        "credential",
        "docker daemon is not running",
        "operation timed out",
        "permission denied",
        "service unavailable",
    )
    if any(marker in normalized for marker in task_markers):
        return "task_defect"
    if any(marker in normalized for marker in infrastructure_markers):
        return "harness_defect"
    return "harness_defect"


class HarborControlBackend:
    """Fixed-command local backend for oracle, nop, and script-replay controls."""

    def __init__(
        self,
        *,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        environment_provider: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        self._command_runner = command_runner or subprocess.run
        self._environment_provider = environment_provider or subscription_environment

    def run(
        self,
        *,
        repo_root: Path,
        task_dir: Path,
        candidate: Mapping[str, Any],
        plan: ControlPlanEntry,
        run_root: Path,
    ) -> ControlObservation:
        repo_root = repo_root.resolve()
        task_dir = task_dir.resolve()
        _validate_candidate_bytes(
            repo_root=repo_root,
            task_dir=task_dir,
            candidate=candidate,
        )
        _validate_backend_plan(candidate, plan)
        candidate_id = _required_string(candidate, "candidate_id")
        source_digest = _required_digest(
            _required_mapping(candidate.get("digests"), "digests"), "package"
        )
        image_digest = _required_digest(
            _required_mapping(candidate.get("digests"), "digests"), "image_definition"
        )
        verifier_digest = _required_digest(
            _required_mapping(candidate.get("digests"), "digests"), "verifier"
        )
        expected_run_root = repo_root / "runs/task-workbench" / candidate_id
        if run_root.resolve() != expected_run_root.resolve() or not _is_under(
            run_root, repo_root / "runs"
        ):
            raise UnsafePathError(
                "control run root must be the candidate's runs/task-workbench path"
            )
        stage = run_root / "staging" / plan.control_id
        jobs = run_root / "jobs"
        job_name = plan.command[plan.command.index("--job-name") + 1]
        job_dir = jobs / job_name
        if stage.exists():
            raise WorkbenchError(f"refusing to replace existing control staging path: {stage}")
        stage.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(task_dir, stage, symlinks=True)
        if plan.mutation_path is not None:
            mutation = stage / plan.mutation_path
            if not mutation.is_file() or not _is_under(mutation, stage):
                raise WorkbenchError(
                    f"adversarial mutation is missing or unsafe: {plan.mutation_path}"
                )
            solution = stage / "solution/solve.sh"
            shutil.copyfile(mutation, solution)
            solution.chmod(mutation.stat().st_mode)
        overlay = stage / NETWORK_OVERLAY_RELATIVE
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_bytes(_candidate_network_overlay(candidate))
        staged_digest = _tree_digest(stage)
        if staged_digest != _expected_stage_digest(candidate, plan):
            raise WorkbenchError("isolated staged task bytes do not match the frozen control plan")
        canonical_command = tuple(plan.command)
        materialized = _materialize_command(canonical_command, repo_root)
        dataset_path = Path(materialized[materialized.index("--path") + 1]).resolve()
        included_task = materialized[materialized.index("--include-task-name") + 1]
        if dataset_path != stage.parent.resolve() or included_task != plan.control_id:
            raise WorkbenchError("materialized control command does not select its isolated stage")
        jobs.mkdir(parents=True, exist_ok=True)
        if job_dir.exists():
            return self._observation_from_existing(
                repo_root=repo_root,
                plan=plan,
                job_dir=job_dir,
                source_digest=source_digest,
                staged_digest=staged_digest,
                image_digest=image_digest,
                verifier_digest=verifier_digest,
            )
        try:
            completed = self._command_runner(
                list(materialized),
                cwd=repo_root,
                check=False,
                capture_output=True,
                text=True,
                timeout=21_600,
                env=dict(self._environment_provider()),
            )
        except KeyboardInterrupt as exc:
            raise ControlInterrupted("operator interrupted Harbor control") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ControlObservation(
                control_id=plan.control_id,
                status="harness_error",
                reward=None,
                reward_vector={},
                verifier_output_digest=None,
                evidence_digest=None,
                image_digest=image_digest,
                verifier_digest=verifier_digest,
                source_package_digest=source_digest,
                staged_package_digest=staged_digest,
                command=canonical_command,
                command_digest=plan.command_digest,
                job_path=_repo_relative(job_dir, repo_root) if job_dir.exists() else None,
                exception_type=type(exc).__name__,
                diagnostic=_scrub_diagnostic(str(exc), repo_root),
                failure_classification="harness_defect",
            )
        if completed.returncode != 0 and not (job_dir / "result.json").is_file():
            diagnostic = completed.stderr or completed.stdout or "Harbor returned nonzero"
            failure_classification = _runner_failure_classification(diagnostic)
            return ControlObservation(
                control_id=plan.control_id,
                status="harness_error",
                reward=None,
                reward_vector={},
                verifier_output_digest=None,
                evidence_digest=None,
                image_digest=image_digest,
                verifier_digest=verifier_digest,
                source_package_digest=source_digest,
                staged_package_digest=staged_digest,
                command=canonical_command,
                command_digest=plan.command_digest,
                job_path=_repo_relative(job_dir, repo_root) if job_dir.exists() else None,
                exception_type="HarborNonZeroExit",
                diagnostic=_scrub_diagnostic(diagnostic, repo_root),
                failure_classification=failure_classification,
            )
        runner_diagnostic = None
        if completed.returncode != 0:
            runner_diagnostic = _scrub_diagnostic(
                completed.stderr or completed.stdout or "Harbor returned nonzero",
                repo_root,
            )
        return self._observation_from_existing(
            repo_root=repo_root,
            plan=plan,
            job_dir=job_dir,
            source_digest=source_digest,
            staged_digest=staged_digest,
            image_digest=image_digest,
            verifier_digest=verifier_digest,
            runner_diagnostic=runner_diagnostic,
        )

    def _observation_from_existing(
        self,
        *,
        repo_root: Path,
        plan: ControlPlanEntry,
        job_dir: Path,
        source_digest: str,
        staged_digest: str,
        image_digest: str,
        verifier_digest: str,
        runner_diagnostic: str | None = None,
    ) -> ControlObservation:
        canonical_command = tuple(plan.command)
        try:
            job = load_job(job_dir)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return ControlObservation(
                control_id=plan.control_id,
                status="interrupted",
                reward=None,
                reward_vector={},
                verifier_output_digest=None,
                evidence_digest=_tree_digest(job_dir) if job_dir.exists() else None,
                image_digest=image_digest,
                verifier_digest=verifier_digest,
                source_package_digest=source_digest,
                staged_package_digest=staged_digest,
                command=canonical_command,
                command_digest=plan.command_digest,
                job_path=_repo_relative(job_dir, repo_root),
                exception_type=("HarborNonZeroExit" if runner_diagnostic else type(exc).__name__),
                diagnostic=runner_diagnostic or _scrub_diagnostic(str(exc), repo_root),
                failure_classification="harness_defect",
            )
        if len(job.trials) != 1:
            return ControlObservation(
                control_id=plan.control_id,
                status="harness_error",
                reward=None,
                reward_vector={},
                verifier_output_digest=None,
                evidence_digest=_tree_digest(job_dir),
                image_digest=image_digest,
                verifier_digest=verifier_digest,
                source_package_digest=source_digest,
                staged_package_digest=staged_digest,
                command=canonical_command,
                command_digest=plan.command_digest,
                job_path=_repo_relative(job_dir, repo_root),
                exception_type="UnexpectedTrialCount",
                diagnostic=f"expected one trial, found {len(job.trials)}",
                failure_classification="harness_defect",
            )
        trial = job.trials[0]
        vector = _reward_vector_from_trial(trial.result)
        exception = trial.result.get("exception_info")
        exception_type = None
        if isinstance(exception, Mapping):
            raw_type = exception.get("exception_type")
            exception_type = str(raw_type) if raw_type else "HarborTrialException"
        status: ControlStatus = "harness_error" if exception_type else "completed"
        failure_classification = (
            classify_trial_outcome(
                agent=plan.agent,
                reward=trial.primary_reward,
                exception_type=exception_type,
                expected_reward=plan.expected_reward,
            )
            if exception_type
            else None
        )
        return ControlObservation(
            control_id=plan.control_id,
            status=status,
            reward=trial.primary_reward,
            reward_vector=vector,
            verifier_output_digest=_verifier_output_digest(trial.path),
            evidence_digest=_tree_digest(job_dir),
            image_digest=image_digest,
            verifier_digest=verifier_digest,
            source_package_digest=source_digest,
            staged_package_digest=staged_digest,
            command=canonical_command,
            command_digest=plan.command_digest,
            job_path=_repo_relative(job_dir, repo_root),
            exception_type=exception_type,
            diagnostic=("trial contains a Harbor exception" if exception_type else None),
            failure_classification=failure_classification,
        )


def _interrupted_observation(
    inspection: Inspection, plan: ControlPlanEntry, message: str
) -> ControlObservation:
    digests = _required_mapping(inspection.candidate.get("digests"), "digests")
    return ControlObservation(
        control_id=plan.control_id,
        status="interrupted",
        reward=None,
        reward_vector={},
        verifier_output_digest=None,
        evidence_digest=None,
        image_digest=_required_digest(digests, "image_definition"),
        verifier_digest=_required_digest(digests, "verifier"),
        source_package_digest=_required_digest(digests, "package"),
        staged_package_digest=_required_digest(digests, "package"),
        command=plan.command,
        command_digest=plan.command_digest,
        exception_type="ControlInterrupted",
        diagnostic=message,
        failure_classification="harness_defect",
    )


def _atomic_create_or_verify(path: Path, content: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != content:
            raise PacketConflictError(f"refusing to replace non-identical record: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    if temporary.exists():
        raise PacketConflictError(f"temporary record already exists: {temporary}")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_controls(
    *,
    inspection: Inspection,
    repo_root: Path,
    task_path: Path,
    backend: ControlBackend,
    run_root: Path | None = None,
) -> ControlBundle:
    if not inspection.static_passed:
        raise ControlsNotAdmittedError("static checks failed; zero controls were called")
    repo_root = repo_root.resolve()
    task_dir = task_path if task_path.is_absolute() else repo_root / task_path
    task_dir = task_dir.resolve()
    _reinspect_frozen_candidate(
        inspection=inspection,
        repo_root=repo_root,
        task_dir=task_dir,
    )
    candidate_id = _required_string(inspection.candidate, "candidate_id")
    source_digest = _required_digest(
        _required_mapping(inspection.candidate.get("digests"), "digests"), "package"
    )
    target = run_root or repo_root / "runs/task-workbench" / candidate_id
    if target.resolve() != (repo_root / "runs/task-workbench" / candidate_id).resolve():
        raise UnsafePathError("controls may write only to the candidate's runs/task-workbench root")
    observations: list[ControlObservation] = []
    target.mkdir(parents=True, exist_ok=True)
    bundle_path = target / "controls.json"
    if bundle_path.is_file():
        existing = load_control_bundle(bundle_path)
        if (
            existing.candidate_id == candidate_id
            and existing.source_package_digest == source_digest
            and len(existing.observations) == len(inspection.control_plan)
        ):
            return existing
        raise PacketConflictError("existing controls.json is partial or belongs to another source")
    for plan in inspection.control_plan:
        try:
            observation = backend.run(
                repo_root=repo_root,
                task_dir=task_dir,
                candidate=inspection.candidate,
                plan=plan,
                run_root=target,
            )
        except ControlInterrupted as exc:
            observation = _interrupted_observation(inspection, plan, str(exc))
            observations.append(observation)
            partial = ControlBundle.build(
                candidate_id=candidate_id,
                source_package_digest=source_digest,
                observations=observations,
            )
            _atomic_create_or_verify(bundle_path, _canonical_bytes(partial.to_dict()))
            return partial
        observations.append(observation)
    bundle = ControlBundle.build(
        candidate_id=candidate_id,
        source_package_digest=source_digest,
        observations=observations,
    )
    _atomic_create_or_verify(bundle_path, _canonical_bytes(bundle.to_dict()))
    return bundle


def load_control_bundle(path: Path) -> ControlBundle:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkbenchError(f"invalid control bundle {path}: {type(exc).__name__}") from exc
    return ControlBundle.from_dict(_required_mapping(value, "control bundle"))


def classify_trial_outcome(
    *,
    agent: str,
    reward: float | None,
    exception_type: str | None,
    expected_reward: float | None,
) -> Classification:
    """Keep infrastructure, task, and ordinary-agent outcomes separate."""
    if exception_type:
        if exception_type in {
            "DockerfileBuildError",
            "EnvironmentBuildError",
            "ImagePullError",
            "RewardFileNotFoundError",
            "RewardFileEmptyError",
            "TaskConfigError",
            "TaskValidationError",
            "VerifierOutputParseError",
        }:
            return "task_defect"
        agent_execution_errors = {
            "AgentRunError",
            "NonZeroAgentExitCodeError",
            "AgentTimeoutError",
        }
        if agent == "oracle" and exception_type in agent_execution_errors:
            return "task_defect"
        if agent not in {"oracle", "nop"} and exception_type in {
            *agent_execution_errors,
            "AgentSafetyRefusalError",
        }:
            return "agent_failure"
        return "harness_defect"
    if agent == "oracle" and expected_reward == 1.0 and reward != 1.0:
        return "task_defect"
    if agent == "nop" and expected_reward == 0.0 and reward != 0.0:
        return "task_defect"
    if agent not in {"oracle", "nop"} and expected_reward is not None and reward != expected_reward:
        return "agent_failure"
    return "expected"


def _trial_exception_type(result: Mapping[str, Any]) -> str | None:
    exception = result.get("exception_info")
    if not isinstance(exception, Mapping):
        return None
    raw_type = exception.get("exception_type")
    return str(raw_type) if raw_type else "HarborTrialException"


def _expected_stage_digest(
    candidate: Mapping[str, Any], plan: ControlPlanEntry
) -> str:
    raw_files = candidate.get("files")
    if not isinstance(raw_files, list):
        raise WorkbenchError("candidate files manifest is invalid")
    entries = [dict(_required_mapping(item, "candidate file")) for item in raw_files]
    if plan.mutation_path is not None:
        mutation = next(
            (item for item in entries if item.get("path") == plan.mutation_path),
            None,
        )
        solution = next(
            (item for item in entries if item.get("path") == "solution/solve.sh"),
            None,
        )
        if mutation is None or solution is None:
            raise WorkbenchError("adversarial plan cannot be reconstructed from manifest")
        solution["size_bytes"] = mutation["size_bytes"]
        solution["digest"] = mutation["digest"]
    entries.append(
        {
            "path": NETWORK_OVERLAY_RELATIVE,
            "role": "image",
            "type": "file",
            "size_bytes": len(_candidate_network_overlay(candidate)),
            "digest": _sha256_bytes(_candidate_network_overlay(candidate)),
        }
    )
    tree_payload = [
        {
            "path": item["path"],
            "type": item["type"],
            "size_bytes": item["size_bytes"],
            "digest": item["digest"],
        }
        for item in sorted(entries, key=lambda item: str(item["path"]))
    ]
    return _sha256_bytes(_canonical_bytes(tree_payload))


def _harbor_task_digest(task_dir: Path) -> str:
    """Reproduce Harbor Packager's default local-task content digest."""
    files: list[Path] = []
    for relative in ("task.toml", "instruction.md", "README.md"):
        path = task_dir / relative
        if path.is_file():
            files.append(path)
    for relative in ("environment", "tests", "solution", "steps"):
        root = task_dir / relative
        if not root.exists():
            continue
        files.extend(path for path in root.rglob("*") if path.is_file())

    def ignored(path: Path) -> bool:
        relative = path.relative_to(task_dir)
        return bool(
            "__pycache__" in relative.parts
            or path.name == ".DS_Store"
            or path.suffix == ".pyc"
            or path.suffix in {".swp", ".swo"}
            or path.name.endswith("~")
        )

    outer = hashlib.sha256()
    for path in sorted(
        (path for path in files if not ignored(path)),
        key=lambda item: item.relative_to(task_dir).as_posix(),
    ):
        relative = path.relative_to(task_dir).as_posix()
        file_digest = _sha256_file(path).removeprefix("sha256:")
        outer.update(f"{relative}\0{file_digest}\n".encode())
    return f"sha256:{outer.hexdigest()}"


def _validate_control_evidence(
    *,
    inspection: Inspection,
    plan: ControlPlanEntry,
    observation: ControlObservation,
    repo_root: Path | None,
) -> tuple[Diagnostic, ...]:
    if observation.status != "completed":
        return ()
    if repo_root is None:
        return (
            _diag(
                "control_evidence_root_missing",
                observation.control_id,
                "completed controls require a repository root for evidence verification",
                classification="harness_defect",
            ),
        )
    candidate_id = _required_string(inspection.candidate, "candidate_id")
    run_root = repo_root.resolve() / "runs/task-workbench" / candidate_id
    job_name = plan.command[plan.command.index("--job-name") + 1]
    expected_job = run_root / "jobs" / job_name
    expected_job_relative = _repo_relative(expected_job, repo_root)
    stage = run_root / "staging" / plan.control_id
    diagnostics: list[Diagnostic] = []
    if observation.job_path != expected_job_relative:
        diagnostics.append(
            _diag(
                "control_job_path_invalid",
                observation.control_id,
                "job_path does not name the frozen control job",
            )
        )
        return tuple(diagnostics)
    if not expected_job.is_dir():
        diagnostics.append(
            _diag(
                "control_evidence_missing",
                observation.control_id,
                "the cited Harbor job directory is not retained",
                classification="harness_defect",
            )
        )
        return tuple(diagnostics)
    actual_evidence_digest = _tree_digest(expected_job)
    if observation.evidence_digest != actual_evidence_digest:
        diagnostics.append(
            _diag(
                "control_evidence_tampered",
                observation.control_id,
                "retained Harbor job bytes do not match evidence_digest",
            )
        )
    if not stage.is_dir():
        diagnostics.append(
            _diag(
                "control_stage_missing",
                observation.control_id,
                "the isolated staged task is not retained",
                classification="harness_defect",
            )
        )
    else:
        overlay = stage / NETWORK_OVERLAY_RELATIVE
        if not overlay.is_file() or overlay.read_bytes() != _candidate_network_overlay(inspection.candidate):
            diagnostics.append(
                _diag(
                    "control_network_isolation_missing",
                    observation.control_id,
                    "the deterministic Docker no-network overlay is absent or changed",
                )
            )
        actual_stage_digest = _tree_digest(stage)
        expected_stage_digest = _expected_stage_digest(inspection.candidate, plan)
        if (
            observation.staged_package_digest != actual_stage_digest
            or actual_stage_digest != expected_stage_digest
        ):
            diagnostics.append(
                _diag(
                    "control_stage_tampered",
                    observation.control_id,
                    "staged task bytes do not reconstruct from candidate and control plan",
                )
            )
    try:
        job = load_job(expected_job)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        diagnostics.append(
            _diag(
                "control_evidence_invalid",
                observation.control_id,
                f"retained Harbor job cannot be loaded: {type(exc).__name__}",
            )
        )
        return tuple(diagnostics)
    if len(job.trials) != 1:
        diagnostics.append(
            _diag(
                "control_trial_count_invalid",
                observation.control_id,
                f"retained Harbor job has {len(job.trials)} trials instead of one",
            )
        )
        return tuple(diagnostics)
    trial = job.trials[0]
    reward_vector = _reward_vector_from_trial(trial.result)
    verifier_output_digest = _verifier_output_digest(trial.path)
    actual_agent = trial.result.get("agent_info")
    actual_agent_name = actual_agent.get("name") if isinstance(actual_agent, Mapping) else None
    expected_stage_path = str(stage.resolve())
    expected_overlay_path = str((stage / NETWORK_OVERLAY_RELATIVE).resolve())
    candidate_name = _required_string(inspection.candidate, "task_name")
    candidate_version = _required_string(inspection.candidate, "task_version")
    result_task_id = trial.result.get("task_id")
    result_config = trial.result.get("config")
    result_task_config = result_config.get("task") if isinstance(result_config, Mapping) else None
    result_environment = (
        result_config.get("environment") if isinstance(result_config, Mapping) else None
    )
    task_checksum = trial.result.get("task_checksum")
    lock_task = trial.lock.get("task")
    lock_agent = trial.lock.get("agent")
    lock_environment = trial.lock.get("environment")
    lock_verifier = trial.lock.get("verifier")
    lock_compose = trial.lock.get("extra_docker_compose")
    task_identity_matches = bool(
        trial.result.get("task_name") == candidate_name
        and isinstance(result_task_id, Mapping)
        and result_task_id.get("path") == expected_stage_path
        and isinstance(result_task_config, Mapping)
        and result_task_config.get("path") == expected_stage_path
        and isinstance(task_checksum, str)
        and re.fullmatch(r"[0-9a-f]{64}", task_checksum)
        and isinstance(lock_task, Mapping)
        and lock_task.get("name") == plan.control_id
        and lock_task.get("version") == candidate_version
        and lock_task.get("type") == "local"
        and lock_task.get("path") == expected_stage_path
    )
    if not task_identity_matches:
        diagnostics.append(
            _diag(
                "control_task_identity_mismatch",
                observation.control_id,
                "retained trial task identity does not name the frozen candidate stage",
            )
        )
    expected_harbor_digest = _harbor_task_digest(stage)
    if not isinstance(lock_task, Mapping) or lock_task.get("digest") != expected_harbor_digest:
        diagnostics.append(
            _diag(
                "control_task_digest_mismatch",
                observation.control_id,
                "Harbor task lock digest does not match the frozen staged task",
            )
        )
    network_binding_matches = bool(
        isinstance(result_environment, Mapping)
        and result_environment.get("type") == "docker"
        and result_environment.get("extra_docker_compose") == [expected_overlay_path]
        and isinstance(lock_environment, Mapping)
        and lock_environment.get("type") == "docker"
        and lock_environment.get("extra_docker_compose") == [expected_overlay_path]
        and isinstance(lock_compose, list)
        and lock_compose
        == [
            {
                "path": expected_overlay_path,
                "digest": _sha256_bytes(_candidate_network_overlay(inspection.candidate)),
            }
        ]
        and isinstance(lock_verifier, Mapping)
        and lock_verifier.get("disable") is False
        and lock_verifier.get("environment_mode") == "separate"
    )
    if not network_binding_matches:
        diagnostics.append(
            _diag(
                "control_network_binding_mismatch",
                observation.control_id,
                "retained trial is not bound to the frozen Docker no-network overlay",
            )
        )
    if actual_agent_name != plan.agent:
        diagnostics.append(
            _diag(
                "control_agent_mismatch",
                observation.control_id,
                "retained trial did not use the planned free control agent",
            )
        )
    if not isinstance(lock_agent, Mapping) or lock_agent.get("name") != plan.agent:
        diagnostics.append(
            _diag(
                "control_agent_lock_mismatch",
                observation.control_id,
                "Harbor trial lock does not name the planned free control agent",
            )
        )
    if trial.result.get("verifier_environment_mode") != "separate":
        diagnostics.append(
            _diag(
                "control_verifier_not_isolated",
                observation.control_id,
                "retained trial did not use a separate verifier environment",
            )
        )
    if (
        observation.reward != trial.primary_reward
        or observation.reward_vector != reward_vector
        or observation.verifier_output_digest != verifier_output_digest
        or observation.exception_type != _trial_exception_type(trial.result)
    ):
        diagnostics.append(
            _diag(
                "control_result_tampered",
                observation.control_id,
                "control claims do not match the retained Harbor trial result",
            )
        )
    return _sort_diagnostics(diagnostics)


def _assess_controls(
    inspection: Inspection, bundle: ControlBundle, *, repo_root: Path | None
) -> tuple[Diagnostic, ...]:
    diagnostics: list[Diagnostic] = []
    candidate_id = _required_string(inspection.candidate, "candidate_id")
    digests = _required_mapping(inspection.candidate.get("digests"), "digests")
    package_digest = _required_digest(digests, "package")
    image_digest = _required_digest(digests, "image_definition")
    verifier_digest = _required_digest(digests, "verifier")
    if bundle.candidate_id != candidate_id or bundle.source_package_digest != package_digest:
        return (
            _diag(
                "control_source_stale",
                "$controls",
                "control bundle identity does not match the inspected candidate",
            ),
        )
    plan_by_id = {item.control_id: item for item in inspection.control_plan}
    seen: set[str] = set()
    oracle_output_digests: list[str] = []
    for observation in bundle.observations:
        if observation.control_id in seen:
            diagnostics.append(
                _diag("control_duplicate", observation.control_id, "control appears more than once")
            )
            continue
        seen.add(observation.control_id)
        plan = plan_by_id.get(observation.control_id)
        if plan is None:
            diagnostics.append(
                _diag(
                    "control_unknown", observation.control_id, "control is not in the frozen plan"
                )
            )
            continue
        if observation.command != plan.command or observation.command_digest != plan.command_digest:
            diagnostics.append(
                _diag(
                    "control_command_drift",
                    observation.control_id,
                    "executed command differs from plan",
                )
            )
        if observation.command_digest != _sha256_bytes(_canonical_bytes(list(observation.command))):
            diagnostics.append(
                _diag(
                    "control_command_digest_invalid",
                    observation.control_id,
                    "command digest is invalid",
                )
            )
        diagnostics.extend(
            _validate_control_evidence(
                inspection=inspection,
                plan=plan,
                observation=observation,
                repo_root=repo_root,
            )
        )
        if observation.source_package_digest != package_digest:
            diagnostics.append(
                _diag(
                    "control_source_stale", observation.control_id, "source package digest changed"
                )
            )
        if observation.image_digest != image_digest:
            diagnostics.append(
                _diag(
                    "control_image_drift", observation.control_id, "image definition digest changed"
                )
            )
        if observation.verifier_digest != verifier_digest:
            diagnostics.append(
                _diag("control_verifier_drift", observation.control_id, "verifier digest changed")
            )
        if observation.status in {"harness_error", "interrupted"}:
            classification = observation.failure_classification or "harness_defect"
            code = {
                "task_defect": "control_task_error",
                "agent_failure": "control_agent_failure",
                "harness_defect": (
                    "control_interrupted"
                    if observation.status == "interrupted"
                    else "control_harness_error"
                ),
                "expected": "control_harness_error",
            }[classification]
            diagnostics.append(
                _diag(
                    code,
                    observation.control_id,
                    observation.diagnostic or "control did not complete",
                    classification=classification,
                )
            )
            continue
        if observation.exception_type:
            diagnostics.append(
                _diag(
                    "control_harness_exception",
                    observation.control_id,
                    f"completed record contains {observation.exception_type}",
                    classification="harness_defect",
                )
            )
            continue
        if observation.reward != plan.expected_reward:
            code = "oracle_false_negative" if plan.expected_reward == 1.0 else "verifier_permissive"
            diagnostics.append(
                _diag(
                    code,
                    observation.control_id,
                    f"expected exact reward {plan.expected_reward}, observed {observation.reward}",
                )
            )
        if observation.verifier_output_digest is None:
            diagnostics.append(
                _diag(
                    "verifier_output_missing",
                    observation.control_id,
                    "completed control has no verifier output digest",
                )
            )
        elif plan.kind == "oracle":
            oracle_output_digests.append(observation.verifier_output_digest)
        if observation.evidence_digest is None or not SHA256_PATTERN.fullmatch(
            observation.evidence_digest
        ):
            diagnostics.append(
                _diag(
                    "control_evidence_digest_missing",
                    observation.control_id,
                    "completed control must retain a valid evidence digest",
                )
            )
    missing = sorted(set(plan_by_id) - seen)
    for control_id in missing:
        diagnostics.append(
            _diag(
                "control_missing",
                control_id,
                "planned control has no observation",
                classification="harness_defect",
            )
        )
    if len(oracle_output_digests) == ORACLE_REPETITIONS and len(set(oracle_output_digests)) != 1:
        diagnostics.append(
            _diag(
                "verifier_nondeterministic",
                "$controls",
                "consecutive Oracle retained verifier output trees are not byte-identical",
            )
        )
    return _sort_diagnostics(diagnostics)


def check_candidate(
    inspection: Inspection,
    controls: ControlBundle | None = None,
    *,
    repo_root: Path | None = None,
) -> CheckReport:
    diagnostics = list(inspection.diagnostics)
    control_diagnostics: tuple[Diagnostic, ...] = ()
    if controls is not None:
        control_diagnostics = _assess_controls(
            inspection,
            controls,
            repo_root=repo_root.resolve() if repo_root is not None else None,
        )
        diagnostics.extend(control_diagnostics)
    sorted_diagnostics = _sort_diagnostics(diagnostics)
    if any(item.severity == "error" for item in inspection.diagnostics):
        disposition: Disposition = "needs_changes"
    elif controls is None:
        disposition = "controls_pending"
    elif any(
        item.severity == "error" and item.classification == "harness_defect"
        for item in control_diagnostics
    ):
        disposition = "harness_blocked"
    elif any(item.severity == "error" for item in control_diagnostics):
        disposition = "needs_changes"
    else:
        disposition = "certified_for_review"
    return CheckReport(
        inspection=inspection,
        controls=controls,
        diagnostics=sorted_diagnostics,
        disposition=disposition,
    )


def _certification_record(
    report: CheckReport,
    *,
    retained_evidence: Sequence[Mapping[str, str]] = (),
    retained_replays: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    candidate = report.inspection.candidate
    digests = _required_mapping(candidate.get("digests"), "digests")
    observations = (
        {item.control_id: item for item in report.controls.observations}
        if report.controls is not None
        else {}
    )
    error_paths = {
        item.path for item in report.diagnostics if item.severity == "error"
    }
    bundle_valid = "$controls" not in error_paths

    def control_matches(plan: ControlPlanEntry) -> bool:
        observation = observations.get(plan.control_id)
        return bool(
            bundle_valid
            and report.inspection.static_passed
            and plan.control_id not in error_paths
            and observation is not None
            and observation.status == "completed"
            and observation.exception_type is None
            and observation.reward == plan.expected_reward
            and observation.verifier_output_digest is not None
            and observation.evidence_digest is not None
        )

    plans_by_kind = {
        kind: [item for item in report.inspection.control_plan if item.kind == kind]
        for kind in ("oracle", "nop", "adversarial", "fair_alternative", "please_hack")
    }
    oracle_plan = plans_by_kind["oracle"]
    nop_plan = plans_by_kind["nop"]
    adversarial_plan = plans_by_kind["adversarial"]
    fair_plan = plans_by_kind["fair_alternative"]
    hack_plan = plans_by_kind["please_hack"]
    oracle_exact = len(oracle_plan) == ORACLE_REPETITIONS and all(
        control_matches(item) for item in oracle_plan
    )
    oracle_outputs = [
        observations[item.control_id].verifier_output_digest
        for item in oracle_plan
        if item.control_id in observations
    ]
    oracle_stable = (
        oracle_exact
        and len(oracle_outputs) == ORACLE_REPETITIONS
        and len(set(oracle_outputs)) == 1
    )
    nop_exact = len(nop_plan) == NOP_REPETITIONS and all(
        control_matches(item) for item in nop_plan
    )
    invalid_rejected = (
        len(adversarial_plan) >= MIN_ADVERSARIAL_CASES
        and all(control_matches(item) for item in adversarial_plan)
    )
    fair_exact = len(fair_plan) == 1 and all(control_matches(item) for item in fair_plan)
    please_hack_executed = (
        bundle_valid
        and len(hack_plan) == 1
        and hack_plan[0].control_id in observations
        and observations[hack_plan[0].control_id].status == "completed"
        and observations[hack_plan[0].control_id].exception_type is None
    )
    retained_ids = {item.get("control_id") for item in retained_evidence}
    replay_ids = {item.get("control_id") for item in retained_replays}
    hack_detected = bool(
        please_hack_executed
        and hack_plan[0].mutation_path == "workbench/please-hack.sh"
        and hack_plan[0].control_id in retained_ids
        and hack_plan[0].control_id in replay_ids
        and observations[hack_plan[0].control_id].reward == 1.0
    )
    all_completed = bundle_valid and bool(observations) and all(
        item.status == "completed" and item.exception_type is None
        for item in observations.values()
    )
    leakage_clean = bundle_valid and report.inspection.static_passed and not any(
        item.code in LEAKAGE_DIAGNOSTIC_CODES for item in report.diagnostics
    )
    isolation = report.inspection.static_passed and all_completed and not any(
        item.code in ISOLATION_DIAGNOSTIC_CODES for item in report.diagnostics
    )
    check_vector = {
        "all_controls_completed": all_completed,
        "static": bundle_valid and report.inspection.static_passed,
        "oracle_exact_1_x3": oracle_exact,
        "oracle_stable_output": oracle_stable,
        "nop_exact_0_x2": nop_exact,
        "invalid_outputs_rejected": invalid_rejected,
        "fair_alternative_exact_1": fair_exact,
        "please_hack_executed": please_hack_executed,
        "hack_detected": hack_detected,
        "leakage_scan_clean": leakage_clean,
        "isolation": isolation,
    }
    task_correct = (
        check_vector["all_controls_completed"]
        and check_vector["static"]
        and check_vector["oracle_exact_1_x3"]
        and check_vector["oracle_stable_output"]
    )
    sound = (
        check_vector["nop_exact_0_x2"]
        and check_vector["invalid_outputs_rejected"]
        and check_vector["please_hack_executed"]
        and not check_vector["hack_detected"]
    )
    axes = {
        "task_correctness": {
            "status": "passed" if task_correct else "failed",
            "reason": (
                "all control jobs completed after backend environment setup; "
                "this is not an independent image-build attestation"
            )
            if task_correct
            else "static/control/oracle evidence is incomplete or contradictory",
            "evidence": [item.control_id for item in oracle_plan] if bundle_valid else [],
        },
        "verifier_soundness": {
            "status": "passed" if sound else "failed",
            "reason": (
                "executed nop, invalid, and please-hack probes were rejected; "
                "this bounded probe set does not prove security"
                if sound
                else "one or more executed invalid controls were not cleanly rejected"
            ),
            "evidence": (
                [item.control_id for item in nop_plan + adversarial_plan + hack_plan]
                if bundle_valid
                else []
            ),
        },
        "verifier_completeness": {
            "status": "passed" if fair_exact else "failed",
            "reason": "byte-distinct fair alternative scored 1"
            if fair_exact
            else "executed fair alternative did not score 1",
            "evidence": [item.control_id for item in fair_plan] if bundle_valid else [],
        },
        "solvability": {
            "status": "passed" if oracle_exact and fair_exact else "failed",
            "reason": "oracle and independent fair alternative both succeeded"
            if oracle_exact and fair_exact
            else "valid-solver evidence is incomplete",
            "evidence": (
                [item.control_id for item in oracle_plan + fair_plan]
                if bundle_valid
                else []
            ),
        },
        "difficulty_calibration": {
            "status": "not_applicable",
            "reason": "local certificate controls do not measure task difficulty",
            "evidence": [],
        },
        "realism_review": {
            "status": "not_assessed",
            "reason": "realism requires separate human or domain review",
            "evidence": [],
        },
    }
    result_digests = [
        _sha256_bytes(_canonical_bytes(observations[plan.control_id].to_dict()))
        for plan in report.inspection.control_plan
        if plan.control_id in observations
    ]
    control_summary = {
        "oracle_runs": sum(item.control_id in observations for item in oracle_plan),
        "nop_runs": sum(item.control_id in observations for item in nop_plan),
        "invalid_probe_runs": sum(item.control_id in observations for item in adversarial_plan),
        "fair_alternative_runs": sum(item.control_id in observations for item in fair_plan),
        "please_hack_runs": sum(item.control_id in observations for item in hack_plan),
        "result_digests": result_digests,
    }
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "task_workbench_certification",
        "workbench_version": WORKBENCH_VERSION,
        "candidate_id": candidate["candidate_id"],
        "candidate_record_digest": candidate["candidate_record_digest"],
        "task_binding": {
            "task_id": candidate["task_id"],
            "task_version": candidate["task_version"],
            "task_path": candidate["task_path"],
            "candidate_package_digest": digests["package"],
            "package_digest": digests["registry_package"],
        },
        "digest_lineage": {
            name: digests[name]
            for name in (
                "package",
                "registry_package",
                "task_toml",
                "instruction",
                "image_definition",
                "solution",
                "verifier",
                "adversarial_controls",
                "fair_alternative",
                "please_hack",
                "leakage_scan",
            )
        },
        "generator_identity": candidate["generator_identity"],
        "validator_identity": candidate["validator_identity"],
        "status": report.disposition,
        "certified": report.passed,
        "admission_granted": False,
        "diagnostics": [item.to_dict() for item in report.diagnostics],
        "check_vector": check_vector,
        "control_summary": control_summary,
        "axes": axes,
        "control_plan": [item.to_dict() for item in report.inspection.control_plan],
        "control_bundle": report.controls.to_dict() if report.controls else None,
        "retained_evidence": [dict(item) for item in retained_evidence],
        "retained_replays": [dict(item) for item in retained_replays],
        "human_action_required": (
            "Review this candidate packet; admission policy may require a preregistered "
            "subset of axes, and realism/difficulty remain separate."
        ),
    }
    body["certification_id"] = "cert-" + hashlib.sha256(_canonical_bytes(body)).hexdigest()[:24]
    return body


def _scrub_repo_paths(value: Any, repo_root: Path) -> Any:
    if isinstance(value, str):
        return value.replace(str(repo_root), "$REPO")
    if isinstance(value, list):
        return [_scrub_repo_paths(item, repo_root) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _scrub_repo_paths(item, repo_root)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    return value


def _manifest(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path,
            "type": entry_type,
            "size_bytes": size,
            "digest": digest,
        }
        for path, entry_type, size, digest in _tree_entries(root)
    ]


def _retained_evidence_record(
    *,
    repo_root: Path,
    report: CheckReport,
    plan: ControlPlanEntry,
    observation: ControlObservation,
) -> dict[str, Any]:
    if observation.status != "completed" or observation.job_path is None:
        raise WorkbenchError("only completed, cited controls can be retained")
    candidate_id = _required_string(report.inspection.candidate, "candidate_id")
    expected_job_name = plan.command[plan.command.index("--job-name") + 1]
    expected_job = repo_root / "runs/task-workbench" / candidate_id / "jobs" / expected_job_name
    if observation.job_path != _repo_relative(expected_job, repo_root):
        raise WorkbenchError("control job path changed before packet retention")
    stage = repo_root / "runs/task-workbench" / candidate_id / "staging" / plan.control_id
    if not expected_job.is_dir() or not stage.is_dir():
        raise WorkbenchError("control evidence disappeared before packet retention")
    if _tree_digest(expected_job) != observation.evidence_digest:
        raise WorkbenchError("control evidence changed before packet retention")
    if _tree_digest(stage) != observation.staged_package_digest:
        raise WorkbenchError("control stage changed before packet retention")
    job = load_job(expected_job)
    if len(job.trials) != 1:
        raise WorkbenchError("control evidence must retain exactly one Harbor trial")
    trial = job.trials[0]
    job_result_path = expected_job / "result.json"
    trial_result_path = trial.path / "result.json"
    trial_lock_path = trial.path / "lock.json"
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "task_workbench_retained_control_evidence",
        "candidate_id": candidate_id,
        "control_id": plan.control_id,
        "command": list(plan.command),
        "command_digest": plan.command_digest,
        "source_package_digest": observation.source_package_digest,
        "staged_package_digest": observation.staged_package_digest,
        "image_digest": observation.image_digest,
        "verifier_digest": observation.verifier_digest,
        "job_tree_digest": observation.evidence_digest,
        "job_manifest": _manifest(expected_job),
        "stage_manifest": _manifest(stage),
        "raw_job_result_digest": _sha256_file(job_result_path),
        "raw_trial_result_digest": _sha256_file(trial_result_path),
        "raw_trial_lock_digest": _sha256_file(trial_lock_path),
        "job_result": _scrub_repo_paths(job.result, repo_root),
        "trial_result": _scrub_repo_paths(trial.result, repo_root),
        "trial_lock": _scrub_repo_paths(trial.lock, repo_root),
        "claim_extract": {
            "agent": plan.agent,
            "exception_type": _trial_exception_type(trial.result),
            "reward": trial.primary_reward,
            "reward_vector": _reward_vector_from_trial(trial.result),
            "verifier_output_digest": _verifier_output_digest(trial.path),
            "verifier_environment_mode": trial.result.get("verifier_environment_mode"),
        },
        "omitted_content": [
            "agent logs",
            "artifacts",
            "verifier stdout/stderr",
        ],
        "omission_reason": "avoid retaining candidate outputs or hidden verifier content",
    }
    body["evidence_record_digest"] = _sha256_bytes(_canonical_bytes(body))
    return body


def write_packet(
    *, repo_root: Path, report: CheckReport, output_root: Path | None = None
) -> tuple[Path, Path]:
    repo_root = repo_root.resolve()
    allowed_root = (repo_root / "research/registration/candidates").resolve()
    target_root = (output_root or allowed_root).resolve()
    if not _is_under(target_root, allowed_root):
        raise UnsafePathError("packets may be written only under research/registration/candidates")
    candidate_id = _required_string(report.inspection.candidate, "candidate_id")
    packet_dir = target_root / candidate_id
    if not _is_under(packet_dir, allowed_root):
        raise UnsafePathError("candidate packet path escapes its review root")
    candidate_path = packet_dir / "candidate.json"
    certification_path = packet_dir / "certification.json"
    _atomic_create_or_verify(candidate_path, _canonical_bytes(report.inspection.candidate))
    retained: list[dict[str, str]] = []
    if report.controls is not None:
        plan_by_id = {item.control_id: item for item in report.inspection.control_plan}
        for observation in report.controls.observations:
            if observation.status != "completed":
                continue
            plan = plan_by_id.get(observation.control_id)
            if plan is None:
                raise WorkbenchError("cannot retain evidence for an unknown control")
            record = _retained_evidence_record(
                repo_root=repo_root,
                report=report,
                plan=plan,
                observation=observation,
            )
            evidence_path = packet_dir / "evidence" / f"{observation.control_id}.json"
            content = _canonical_bytes(record)
            _atomic_create_or_verify(evidence_path, content)
            retained.append(
                {
                    "control_id": observation.control_id,
                    "path": _repo_relative(evidence_path, repo_root),
                    "digest": _sha256_bytes(content),
                }
            )
    retained_replays: list[dict[str, str]] = []
    task_dir = _candidate_task_dir(repo_root, report.inspection.candidate)
    for plan in report.inspection.control_plan:
        if plan.kind not in {"fair_alternative", "please_hack"}:
            continue
        if plan.mutation_path is None:
            raise WorkbenchError(f"{plan.control_id} has no replayable solver path")
        source = (task_dir / plan.mutation_path).resolve()
        if not source.is_file() or not _is_under(source, task_dir):
            raise WorkbenchError(f"{plan.control_id} replay script is missing or unsafe")
        replay_path = packet_dir / "replays" / f"{plan.control_id}.sh"
        content = source.read_bytes()
        _atomic_create_or_verify(replay_path, content)
        retained_replays.append(
            {
                "control_id": plan.control_id,
                "kind": plan.kind,
                "path": _repo_relative(replay_path, repo_root),
                "digest": _sha256_bytes(content),
            }
        )
    _atomic_create_or_verify(
        certification_path,
        _canonical_bytes(
            _certification_record(
                report,
                retained_evidence=retained,
                retained_replays=retained_replays,
            )
        ),
    )
    return candidate_path, certification_path


def _source_from_args(args: argparse.Namespace) -> CandidateSource:
    raw_creds = getattr(args, "credentials", None) or ()
    creds = tuple(str(item) for item in raw_creds) if isinstance(raw_creds, (list, tuple)) else ()
    return CandidateSource(
        source_uri=args.source_uri,
        source_ref=args.source_ref,
        license=args.license,
        provenance_zone=args.zone,
        credentials=creds,
    )


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("task", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--source-uri", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--license", required=True)
    parser.add_argument(
        "--zone",
        choices=("01-external", "02-local-evidence", "03-synthetic", "04-curated"),
        default="03-synthetic",
    )
    parser.add_argument(
        "--credential",
        "--credentials",
        dest="credentials",
        action="append",
        default=[],
        help="Declared host credential names authorized for verifier environment placeholders",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evallab.task_workbench",
        description="Inspect and certify Harbor task candidates without admitting them.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan", help="inspect and print frozen local control plan")
    _add_common_arguments(plan)
    check = subparsers.add_parser("check", help="run static checks and assess/run controls")
    _add_common_arguments(check)
    controls = check.add_mutually_exclusive_group()
    controls.add_argument("--controls", type=Path)
    controls.add_argument("--run-controls", action="store_true")
    packet = subparsers.add_parser("packet", help="write deterministic candidate review records")
    _add_common_arguments(packet)
    packet.add_argument("--controls", type=Path)
    packet.add_argument("--output-root", type=Path)
    return parser


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    control_backend: ControlBackend | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    repo_root = args.repo_root.resolve()
    source = _source_from_args(args)
    try:
        inspection = inspect_candidate(repo_root=repo_root, task_path=args.task, source=source)
        if args.command == "plan":
            sys.stdout.buffer.write(_canonical_bytes(inspection.to_dict()))
            return 0 if inspection.static_passed else 1
        controls: ControlBundle | None = None
        controls_path = getattr(args, "controls", None)
        if controls_path is not None:
            controls = load_control_bundle(controls_path)
        elif getattr(args, "run_controls", False):
            controls = run_controls(
                inspection=inspection,
                repo_root=repo_root,
                task_path=args.task,
                backend=control_backend or HarborControlBackend(),
            )
        report = check_candidate(inspection, controls, repo_root=repo_root)
        if args.command == "check":
            sys.stdout.buffer.write(_canonical_bytes(report.to_dict()))
            return 0 if report.passed else 1
        output_root = args.output_root.resolve() if args.output_root else None
        candidate_path, certification_path = write_packet(
            repo_root=repo_root,
            report=report,
            output_root=output_root,
        )
        payload = {
            "candidate": _repo_relative(candidate_path, repo_root),
            "certification": _repo_relative(certification_path, repo_root),
            "disposition": report.disposition,
        }
        sys.stdout.buffer.write(_canonical_bytes(payload))
        return 0 if report.passed else 1
    except WorkbenchError as exc:
        sys.stderr.write(f"task-workbench: {exc}\n")
        return 2


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
