"""Rebind stored isolation evidence to the live Docker and adapter identity."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from evallab.harbor_network import ADAPTER_VERSION, adapter_digest
from evallab.network_isolation import PROBE_IMPLEMENTATION_VERSION
from evallab.schemas import (
    NetworkIsolationDispatchIdentityV1,
    NetworkIsolationEvidenceV1,
    NetworkIsolationProbeIdentityV1,
    NetworkIsolationRuntimeIdentityV1,
)

_DEFAULT_TIMEOUT_SECONDS = 3.0
_SUPPORTED_ADAPTER = "zai-opencode"
_PROBE_IMPLEMENTATION = "evallab.network_isolation.run_docker_network_isolation_probe"


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _runtime_output(args: list[str]) -> str:
    completed = subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


def _probe_port(evidence: NetworkIsolationEvidenceV1, escape_class: str) -> int:
    matches = [result for result in evidence.probe_results if result.escape_class == escape_class]
    if len(matches) != 1:
        raise ValueError(f"isolation evidence has no unique {escape_class} probe target")
    port = urlparse(matches[0].target).port
    if port is None:
        raise ValueError(f"isolation evidence {escape_class} target has no port")
    return port


def current_dispatch_isolation_identity(
    evidence: NetworkIsolationEvidenceV1,
) -> NetworkIsolationDispatchIdentityV1:
    """Compute the live identity corresponding to a stored isolation probe contract."""
    stored_runtime = evidence.runtime_identity
    stored_probe = evidence.probe_identity
    effective_policy = evidence.effective_agent_policy
    if stored_runtime is None or stored_probe is None or effective_policy is None:
        raise ValueError("isolation evidence lacks runtime, probe, or effective policy identity")
    if stored_runtime.adapter != _SUPPORTED_ADAPTER:
        raise ValueError(f"unsupported isolation-bound adapter: {stored_runtime.adapter}")
    if stored_probe.implementation != _PROBE_IMPLEMENTATION:
        raise ValueError(f"unsupported isolation probe: {stored_probe.implementation}")

    image_digest = _runtime_output(
        [
            "docker",
            "image",
            "inspect",
            stored_runtime.container_image_digest,
            "--format",
            "{{.Id}}",
        ]
    )
    runtime_version = _runtime_output(["docker", "version", "--format", "{{.Server.Version}}"])
    config = {
        "primary_port": _probe_port(evidence, "hostname"),
        "alternate_port": _probe_port(evidence, "alternate-port"),
        "timeout_seconds": _DEFAULT_TIMEOUT_SECONDS,
        "image_digest": image_digest,
        "network_mode": effective_policy.mode,
    }
    probe_module = Path(__file__).with_name("network_isolation.py")
    probe_identity = NetworkIsolationProbeIdentityV1(
        implementation=_PROBE_IMPLEMENTATION,
        implementation_version=PROBE_IMPLEMENTATION_VERSION,
        implementation_digest=_sha256(
            PROBE_IMPLEMENTATION_VERSION.encode() + b"\n" + probe_module.read_bytes()
        ),
        config_digest=_sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()),
    )
    runtime_identity = NetworkIsolationRuntimeIdentityV1(
        platform_system=platform.system(),
        platform_release=platform.release(),
        platform_machine=platform.machine(),
        container_runtime="docker",
        container_runtime_version=runtime_version,
        container_image_digest=image_digest,
        adapter=stored_runtime.adapter,
        adapter_version=ADAPTER_VERSION,
        adapter_digest=adapter_digest(),
    )
    return NetworkIsolationDispatchIdentityV1(
        runtime_identity=runtime_identity,
        probe_identity=probe_identity,
    )


__all__ = ["current_dispatch_isolation_identity"]
