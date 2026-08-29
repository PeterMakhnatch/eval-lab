"""Reusable host task staging for Darwin (Docker Desktop) execution lanes.

Given a canonical task package and a *separate* destination directory, stage a
host-adapted execution copy of the package. Adaptations are the ones the
Z.ai/OpenCode pilot performed by hand and nothing more:

1. ``task.toml`` network adaptation via the existing ``harbor_network``
   adapter (``adapt_task_toml_for_host``), recorded as a
   ``NetworkAdaptation``.
2. Optional, explicitly requested platform pinning of compose services and
   the separate verifier/environment Dockerfiles to ``linux/amd64`` so the
   reviewed cp312/manylinux_2_17_x86_64 wheel manifest can be used under
   emulation on Apple Silicon.
3. Optional, explicitly requested public-egress network attachment for the
   ``main`` service only, preserving the internal-only MCP sidecar.

Every staging writes a typed ``run_manifest.json`` with adapter digest and
version, requested/effective networks, platform reason, source digest, and
staged digest. The helper refuses symlinked sources, path escapes
(destination inside source or vice versa), pre-existing destinations, unknown
compose shapes, and platform changes that were not explicitly requested.

TRUSTED-TASK-ONLY LANE: attaching public egress to ``main`` and running the
Z.ai credential mount means the agent container can reach the network and
read the mounted credential. This helper stages such runs deliberately and
records them; it does not provide — and must not be described as providing —
proxy-grade credential isolation or enforced network isolation on Darwin.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from evallab.harbor_network import (
    HarborNetworkPolicy,
    _canonical_networks,
    adapt_task_toml_for_host,
    host_harbor_network_policy,
)
from evallab.mcp_substrate import DEFAULT_SIDECAR_SERVICE

ADAPTER_VERSION = "1.0.0"

#: Platform of the reviewed trusted wheel manifest (cp312/manylinux_2_17_x86_64).
TRUSTED_WHEELHOUSE_PLATFORM = "linux/amd64"

#: Default recorded justification for a platform pin.
DEFAULT_PLATFORM_REASON = (
    "reviewed cp312/manylinux_2_17_x86_64 wheel manifest requires "
    f"{TRUSTED_WHEELHOUSE_PLATFORM} emulation on the Darwin Docker Desktop host"
)

#: Compose's implicit public network; attaching it grants egress.
PUBLIC_EGRESS_NETWORK = "default"

#: The only service allowed to receive the public egress attachment.
EGRESS_SERVICE = "main"

#: Allowed compose top-level keys (matches the substrate topology validator).
_ALLOWED_COMPOSE_TOP_KEYS = frozenset({"services", "volumes", "networks", "version"})

#: The only service names a staged compose document may declare.
_ALLOWED_SERVICES = frozenset({EGRESS_SERVICE, DEFAULT_SIDECAR_SERVICE})

#: Dockerfile lines eligible for a platform pin.
_FROM_LINE_RE = re.compile(r"^(FROM(?:\s+--platform=(?P<platform>[^\s]+))?\s+.+)$")

_MANIFEST_FILENAME = "run_manifest.json"

_COMPOSE_FILENAMES = ("docker-compose.yaml", "docker-compose.yml")


def adapter_digest() -> str:
    """SHA-256 digest of this adapter module and the version string."""
    source = Path(__file__).resolve().read_bytes()
    return f"sha256:{hashlib.sha256(ADAPTER_VERSION.encode() + b'\n' + source).hexdigest()}"


@dataclass(frozen=True)
class PlatformPin:
    """One explicit platform pin applied to a staged service or Dockerfile."""

    target: str
    platform: str


@dataclass(frozen=True)
class HostTaskRunManifest:
    """Typed record of every adaptation applied to one staged execution copy."""

    schema_version: str
    adapter_version: str
    adapter_digest: str
    source_package_digest: str
    staged_package_digest: str
    requested_agent_network: str
    effective_agent_network: str
    requested_verifier_network: str
    effective_verifier_network: str
    requested_verifier_phase_network: str | None
    effective_verifier_phase_network: str | None
    network_isolation_enforced: bool
    network_isolation_reason: str | None
    agent_public_egress: bool
    compose_present: bool
    main_networks: tuple[str, ...] | None
    sidecar_networks: tuple[str, ...] | None
    platform_pins: tuple[PlatformPin, ...]
    platform_reason: str | None
    task_toml_adapted: bool
    modified_paths: tuple[str, ...]


def _compose_path(staged: Path) -> Path | None:
    for name in _COMPOSE_FILENAMES:
        candidate = staged / "environment" / name
        if candidate.is_file():
            return candidate
    return None


def _load_compose(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"compose document is not a mapping: {path}")
    return data


def _validate_compose_shape(data: dict[str, Any], path: Path) -> None:
    """Reject unknown compose shapes before any staging mutation."""
    unknown_keys = sorted(set(data) - _ALLOWED_COMPOSE_TOP_KEYS)
    if unknown_keys:
        raise ValueError(f"unknown compose top-level keys {unknown_keys} in {path}")
    services = data.get("services")
    if not isinstance(services, dict) or not services:
        raise ValueError(f"compose 'services' must be a non-empty mapping in {path}")
    if EGRESS_SERVICE not in services:
        raise ValueError(f"compose topology must declare a '{EGRESS_SERVICE}' service in {path}")
    if len(services) > len(_ALLOWED_SERVICES):
        raise ValueError(
            f"compose topology admits at most {len(_ALLOWED_SERVICES)} services "
            f"({sorted(_ALLOWED_SERVICES)}), got {sorted(services)} in {path}"
        )
    unknown = sorted(set(services) - _ALLOWED_SERVICES)
    if unknown:
        raise ValueError(f"unknown compose services {unknown} in {path}")
    for name, cfg in services.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"compose service {name!r} configuration must be a mapping in {path}")


def _without_staging_changes(
    data: dict[str, Any],
    *,
    strip_pins: bool,
    strip_egress: bool,
) -> dict[str, Any]:
    """Copy of a compose document with the requested staging changes undone.

    ``strip_pins`` removes every service platform pin; ``strip_egress``
    removes the public egress network from the ``main`` service (dropping an
    emptied networks declaration so an absent key stays absent). Comparing
    the result to the pre-staging document proves no other field changed.
    """
    restored = copy.deepcopy(data)
    services = restored.get("services")
    if not isinstance(services, dict):
        return restored
    for name, cfg in services.items():
        if not isinstance(cfg, dict):
            continue
        if strip_pins:
            cfg.pop("platform", None)
        if strip_egress and name == EGRESS_SERVICE:
            networks = cfg.get("networks")
            if isinstance(networks, list) and PUBLIC_EGRESS_NETWORK in networks:
                networks = [n for n in networks if n != PUBLIC_EGRESS_NETWORK]
                if networks:
                    cfg["networks"] = networks
                else:
                    del cfg["networks"]
            elif isinstance(networks, dict) and PUBLIC_EGRESS_NETWORK in networks:
                del networks[PUBLIC_EGRESS_NETWORK]
                if not networks:
                    del cfg["networks"]
    return restored


def _pin_compose_services(
    data: dict[str, Any],
    *,
    platform: str,
    rel_path: str,
) -> list[PlatformPin]:
    """Pin every compose service to ``platform``; refuse conflicting pins."""
    pins: list[PlatformPin] = []
    for name, cfg in data["services"].items():
        existing = cfg.get("platform")
        if existing is not None and existing != platform:
            raise ValueError(
                f"compose service {name!r} in {rel_path} is already pinned to "
                f"{existing!r}; refusing to change it to {platform!r}"
            )
        if existing != platform:
            cfg["platform"] = platform
        pins.append(PlatformPin(target=f"service:{name}", platform=platform))
    return pins


def _attach_egress_to_main(
    data: dict[str, Any],
    *,
    rel_path: str,
) -> None:
    """Attach the public default network to ``main`` only."""
    main_cfg = data["services"][EGRESS_SERVICE]
    networks = main_cfg.get("networks")
    if networks is None:
        main_cfg["networks"] = [PUBLIC_EGRESS_NETWORK]
        return
    if not isinstance(networks, (list, dict)):
        raise ValueError(
            f"compose service 'main' networks must be a list or mapping in {rel_path}"
        )
    if isinstance(networks, list):
        if PUBLIC_EGRESS_NETWORK not in networks:
            networks.append(PUBLIC_EGRESS_NETWORK)
    elif PUBLIC_EGRESS_NETWORK not in networks:
        networks[PUBLIC_EGRESS_NETWORK] = None


def _service_networks(data: dict[str, Any], service: str) -> tuple[str, ...] | None:
    cfg = data.get("services", {}).get(service)
    if not isinstance(cfg, dict):
        return None
    networks = cfg.get("networks")
    if networks is None:
        return None
    if isinstance(networks, dict):
        return tuple(sorted(networks))
    return tuple(networks)


def _pin_dockerfile(
    text: str,
    *,
    platform: str,
    rel_path: str,
) -> tuple[str, list[PlatformPin]]:
    """Insert ``--platform`` on every ``FROM`` line; refuse conflicting pins."""
    lines: list[str] = []
    pins: list[PlatformPin] = []
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        match = _FROM_LINE_RE.match(stripped)
        if match is None:
            lines.append(line)
            continue
        existing = match.group("platform")
        if existing is not None and existing != platform:
            raise ValueError(
                f"{rel_path} FROM line already pins platform {existing!r}; "
                f"refusing to change it to {platform!r}"
            )
        if existing is None:
            pinned = re.sub(r"^FROM\s+", f"FROM --platform={platform} ", stripped)
            lines.append(pinned + ("\n" if line.endswith("\n") else ""))
            pins.append(PlatformPin(target=f"dockerfile:{rel_path}", platform=platform))
        else:
            lines.append(line)
    return "".join(lines), pins


def _verify_dockerfile_change(before: str, after: str, rel_path: str) -> None:
    """Prove a Dockerfile edit only touched ``FROM`` platform flags."""
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    if len(before_lines) != len(after_lines):
        raise ValueError(f"platform pin changed line count of {rel_path}")
    for index, (old, new) in enumerate(zip(before_lines, after_lines, strict=True), start=1):
        if old == new:
            continue
        old_image = re.sub(r"^FROM(\s+--platform=[^\s]+)?", "FROM", old)
        new_image = re.sub(r"^FROM(\s+--platform=[^\s]+)?", "FROM", new)
        if old_image != new_image or not new.startswith("FROM --platform="):
            raise ValueError(
                f"platform pin changed more than the FROM platform flag at "
                f"{rel_path}:{index}"
            )


def _assert_source_and_destination(source: Path, destination: Path) -> tuple[Path, Path]:
    """Refuse symlinked sources, path escapes, and pre-existing destinations."""
    source = source.resolve()
    destination = Path(destination).absolute()
    # A dangling symlink at the destination itself is refused; symlinked
    # *parents* are fine (macOS TMPDIR lives behind /var -> /private/var) —
    # containment below is checked on fully resolved paths.
    if destination.is_symlink():
        raise ValueError(f"destination path is a symlink: {destination}")
    destination = destination.resolve()
    if not source.is_dir():
        raise ValueError(f"source task directory not found: {source}")
    if source.is_symlink() or any(p.is_symlink() for p in source.rglob("*")):
        raise ValueError("task package staging rejects symlinks")
    if not (source / "task.toml").is_file():
        raise ValueError(f"task.toml missing in {source}")
    if source == destination:
        raise ValueError(
            "refusing to stage a task package onto itself: "
            f"source and destination are both {destination}"
        )
    if destination in source.parents or source in destination.parents:
        raise ValueError(
            "refusing path escape: destination must be outside the source "
            f"package (source={source}, destination={destination})"
        )
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"destination already exists: {destination}")
    return source, destination


def stage_task_for_host(
    source: Path,
    destination: Path,
    *,
    host_policy: HarborNetworkPolicy | None = None,
    pin_platform: bool = False,
    attach_agent_egress: bool = False,
    platform: str = TRUSTED_WHEELHOUSE_PLATFORM,
    platform_reason: str | None = None,
) -> HostTaskRunManifest:
    """Stage a host-adapted execution copy of ``source`` at ``destination``.

    Fail-closed wrapper: when staging raises after the copy was created, the
    partial destination is removed (unless it pre-existed the call, in which
    case it is left untouched) and the original error propagates. See
    ``_stage_task_for_host`` for the adaptation contract and the
    trusted-task-only security boundary.
    """
    preexisting = destination.exists() or destination.is_symlink()
    try:
        return _stage_task_for_host(
            source,
            destination,
            host_policy=host_policy,
            pin_platform=pin_platform,
            attach_agent_egress=attach_agent_egress,
            platform=platform,
            platform_reason=platform_reason,
        )
    except BaseException:
        if not preexisting:
            shutil.rmtree(destination, ignore_errors=True)
        raise


def _stage_task_for_host(
    source: Path,
    destination: Path,
    *,
    host_policy: HarborNetworkPolicy | None = None,
    pin_platform: bool = False,
    attach_agent_egress: bool = False,
    platform: str = TRUSTED_WHEELHOUSE_PLATFORM,
    platform_reason: str | None = None,
) -> HostTaskRunManifest:
    """Stage a host-adapted execution copy of ``source`` at ``destination``.

    See the module docstring for the full adaptation contract and the
    trusted-task-only security boundary. All mutations are explicit
    (``pin_platform``/``attach_agent_egress`` default to ``False``), proven
    minimal against the source bytes, and recorded in the typed manifest
    written to ``<destination>/run_manifest.json``.
    """
    from evallab.registry import compute_task_digests

    source, destination = _assert_source_and_destination(source, destination)
    source_digest = compute_task_digests(source).package

    modified: list[str] = []

    shutil.copytree(source, destination, symlinks=False)
    if any(p.is_symlink() for p in destination.rglob("*")):
        raise ValueError("staged task copy contains a symlink")

    # 1. task.toml host network adaptation (existing harbor_network adapter).
    task_toml_path = destination / "task.toml"
    original_text = task_toml_path.read_text(encoding="utf-8")
    adapted_text, adaptation = adapt_task_toml_for_host(original_text, host_policy=host_policy)
    task_toml_adapted = adapted_text != original_text
    if task_toml_adapted:
        task_toml_path.write_text(adapted_text, encoding="utf-8")
        modified.append("task.toml")

    compose_path = _compose_path(destination)
    platform_pins: list[PlatformPin] = []
    main_networks: tuple[str, ...] | None = None
    sidecar_networks: tuple[str, ...] | None = None

    if compose_path is not None:
        compose_rel = compose_path.relative_to(destination).as_posix()
        compose_data = _load_compose(compose_path)
        _validate_compose_shape(compose_data, compose_path)
        original_compose = copy.deepcopy(compose_data)

        if pin_platform:
            platform_pins.extend(_pin_compose_services(compose_data, platform=platform, rel_path=compose_rel))
        elif any("platform" in cfg for cfg in compose_data["services"].values() if isinstance(cfg, dict)):
            raise ValueError(
                "compose services carry platform pins but pin_platform was not "
                f"requested: {compose_rel}"
            )

        if attach_agent_egress:
            _attach_egress_to_main(compose_data, rel_path=compose_rel)

        if compose_data != original_compose:
            compose_path.write_text(
                yaml.safe_dump(compose_data, sort_keys=False), encoding="utf-8"
            )
            modified.append(compose_rel)
        rewritten = _load_compose(compose_path)
        # Prove the only compose changes are the requested platform pins and,
        # when requested, the main-service public egress attachment.
        proof = _without_staging_changes(
            rewritten, strip_pins=pin_platform, strip_egress=attach_agent_egress
        )
        if proof != original_compose:
            raise ValueError(
                "compose staging changed fields beyond the requested platform "
                f"pins and main-service egress attachment: {compose_rel}"
            )
        main_networks = _service_networks(rewritten, EGRESS_SERVICE)
        sidecar_networks = (
            _service_networks(rewritten, DEFAULT_SIDECAR_SERVICE)
            if DEFAULT_SIDECAR_SERVICE in rewritten.get("services", {})
            else None
        )

    # 2. Dockerfile platform pins (environment and separate verifier).
    for rel in ("environment/Dockerfile", "tests/Dockerfile"):
        dockerfile = destination / rel
        if not dockerfile.is_file():
            continue
        text = dockerfile.read_text(encoding="utf-8")
        has_pin = any(
            (match := _FROM_LINE_RE.match(line)) and match.group("platform") is not None
            for line in text.splitlines()
        )
        if pin_platform:
            pinned_text, pins = _pin_dockerfile(text, platform=platform, rel_path=rel)
            _verify_dockerfile_change(text, pinned_text, rel)
            if pinned_text != text:
                dockerfile.write_text(pinned_text, encoding="utf-8")
                modified.append(rel)
            platform_pins.extend(pins)
        elif has_pin:
            raise ValueError(
                f"{rel} carries a FROM platform pin but pin_platform was not requested"
            )

    if adaptation is not None:
        requested_agent = adaptation.requested_agent_network
        effective_agent = adaptation.effective_agent_network
        requested_verifier = adaptation.requested_verifier_network
        effective_verifier = adaptation.effective_verifier_network
        requested_phase = adaptation.requested_verifier_phase_network
        effective_phase = adaptation.effective_verifier_phase_network
        isolation_enforced = adaptation.network_isolation_enforced
        isolation_reason = adaptation.network_isolation_reason
    else:
        # No adaptation was needed: the canonical policy stands as declared.
        # Derive the isolation verdict from the declared modes and the host.
        host = host_policy if host_policy is not None else host_harbor_network_policy()
        declared_agent, declared_verifier, declared_phase = _canonical_networks(
            tomllib.loads(adapted_text)
        )
        requested_agent = effective_agent = declared_agent
        requested_verifier = effective_verifier = declared_verifier
        requested_phase = effective_phase = declared_phase
        all_no_network = (
            declared_agent == "no-network"
            and declared_verifier in (None, "no-network")
            and declared_phase in (None, "no-network")
        )
        isolation_enforced = all_no_network and host.network_isolation_enforced
        isolation_reason = None if isolation_enforced else host.network_isolation_reason

    if attach_agent_egress and compose_path is None and effective_agent != "public":
        raise ValueError(
            "agent egress was requested but the single-container task's "
            f"effective network is {effective_agent!r}, not public"
        )

    staged_digest = compute_task_digests(destination).package
    source_digest_after = compute_task_digests(source).package
    if source_digest_after != source_digest:
        raise ValueError("source task package mutated during staging")

    manifest = HostTaskRunManifest(
        schema_version="1.0",
        adapter_version=ADAPTER_VERSION,
        adapter_digest=adapter_digest(),
        source_package_digest=source_digest,
        staged_package_digest=staged_digest,
        requested_agent_network=requested_agent,
        effective_agent_network=effective_agent,
        requested_verifier_network=requested_verifier,
        effective_verifier_network=effective_verifier,
        requested_verifier_phase_network=requested_phase,
        effective_verifier_phase_network=effective_phase,
        network_isolation_enforced=isolation_enforced,
        network_isolation_reason=isolation_reason,
        agent_public_egress=attach_agent_egress,
        compose_present=compose_path is not None,
        main_networks=main_networks,
        sidecar_networks=sidecar_networks,
        platform_pins=tuple(platform_pins),
        platform_reason=(platform_reason or DEFAULT_PLATFORM_REASON) if pin_platform else None,
        task_toml_adapted=task_toml_adapted,
        modified_paths=tuple(sorted(modified)),
    )
    (destination / _MANIFEST_FILENAME).write_text(
        json.dumps(asdict(manifest), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return manifest


