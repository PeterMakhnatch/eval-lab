"""Digest-bound Docker egress probes independent of transport qualification."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal

from evallab.schemas import (
    NETWORK_ESCAPE_CLASSES,
    NetworkEscapeProbeResultV1,
    NetworkIsolationEvidenceV1,
    NetworkIsolationProbeIdentityV1,
    NetworkIsolationRuntimeIdentityV1,
    NetworkPolicyEvidenceV1,
    build_network_isolation_evidence,
)

PROBE_IMPLEMENTATION_VERSION = "1.0.0"
DEFAULT_PROBE_IMAGE = "python:3.12-alpine"

_CONTAINER_PROBE = r"""
import json
import socket
import sys
import urllib.error
import urllib.request

config = json.loads(sys.argv[1])

def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=config["timeout_seconds"]) as response:
            body = response.read().decode("utf-8")
        return ("escaped", "http-response:" + body) if body == "probe-ok" else ("error", "unexpected-body:" + body)
    except urllib.error.URLError as exc:
        return "blocked", "network-error:" + type(exc.reason).__name__
    except (TimeoutError, OSError) as exc:
        return "blocked", "network-error:" + type(exc).__name__
    except Exception as exc:
        return "error", "probe-error:" + type(exc).__name__

host = "host.docker.internal"
primary = config["primary_port"]
alternate = config["alternate_port"]
targets = [
    ("hostname", f"http://{host}:{primary}/hostname"),
    ("direct-ip", f"http://{socket.gethostbyname(host)}:{primary}/direct-ip"),
    ("alternate-port", f"http://{host}:{alternate}/alternate-port"),
    ("redirect", f"http://{host}:{primary}/redirect"),
    ("dns-rebinding", f"http://isolation-rebind.test:{primary}/dns-rebinding"),
]
rows = []
for kind, url in targets:
    outcome, detail = fetch(url)
    rows.append(
        {"escape_class": kind, "target": url, "outcome": outcome, "detail": detail}
    )
print(json.dumps(rows, sort_keys=True))
"""


class _ProbeHandler(BaseHTTPRequestHandler):
    alternate_port: int = 0

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://host.docker.internal:{self.alternate_port}/redirect-target",
            )
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"probe-ok")

    def log_message(self, format: str, *args: object) -> None:
        return


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _runtime_output(args: Sequence[str]) -> str:
    completed = subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


def _server() -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("0.0.0.0", 0), _ProbeHandler)


def run_docker_network_isolation_probe(
    *,
    requested_agent_policy: NetworkPolicyEvidenceV1,
    effective_agent_policy: NetworkPolicyEvidenceV1,
    requested_verifier_policy: NetworkPolicyEvidenceV1,
    effective_verifier_policy: NetworkPolicyEvidenceV1,
    requested_verifier_phase_policy: NetworkPolicyEvidenceV1 | None,
    effective_verifier_phase_policy: NetworkPolicyEvidenceV1 | None,
    adapter: str,
    adapter_version: str,
    adapter_digest: str,
    image: str = DEFAULT_PROBE_IMAGE,
    timeout_seconds: float = 3.0,
    valid_for: timedelta = timedelta(hours=24),
    observed_at: datetime | None = None,
) -> NetworkIsolationEvidenceV1:
    """Run all five egress probes in one fresh container, without a model call."""
    primary = _server()
    alternate = _server()
    _ProbeHandler.alternate_port = alternate.server_port
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (primary, alternate)
    ]
    for thread in threads:
        thread.start()
    try:
        image_digest = _runtime_output(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
        runtime_version = _runtime_output(["docker", "version", "--format", "{{.Server.Version}}"])
        config = {
            "primary_port": primary.server_port,
            "alternate_port": alternate.server_port,
            "timeout_seconds": timeout_seconds,
            "image_digest": image_digest,
            "network_mode": effective_agent_policy.mode,
        }
        docker_network: Literal["none", "bridge"] = (
            "none" if effective_agent_policy.mode == "no-network" else "bridge"
        )
        completed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--network",
                docker_network,
                "--add-host",
                "isolation-rebind.test:host-gateway",
                image,
                "python",
                "-c",
                _CONTAINER_PROBE,
                json.dumps(config, sort_keys=True, separators=(",", ":")),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=max(30.0, timeout_seconds * 10),
        )
        raw_results = json.loads(completed.stdout)
        results = tuple(NetworkEscapeProbeResultV1.model_validate(row) for row in raw_results)
        if tuple(result.escape_class for result in results) != NETWORK_ESCAPE_CLASSES:
            raise ValueError("network-isolation probe returned an unexpected escape-class order")
        observed = observed_at or datetime.now(UTC)
        module_bytes = Path(__file__).read_bytes()
        probe_identity = NetworkIsolationProbeIdentityV1(
            implementation="evallab.network_isolation.run_docker_network_isolation_probe",
            implementation_version=PROBE_IMPLEMENTATION_VERSION,
            implementation_digest=_sha256(
                PROBE_IMPLEMENTATION_VERSION.encode() + b"\n" + module_bytes
            ),
            config_digest=_sha256(
                json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
            ),
        )
        runtime_identity = NetworkIsolationRuntimeIdentityV1(
            platform_system=platform.system(),
            platform_release=platform.release(),
            platform_machine=platform.machine(),
            container_runtime="docker",
            container_runtime_version=runtime_version,
            container_image_digest=image_digest,
            adapter=adapter,
            adapter_version=adapter_version,
            adapter_digest=adapter_digest,
        )
        return build_network_isolation_evidence(
            requested_agent_policy=requested_agent_policy,
            effective_agent_policy=effective_agent_policy,
            requested_verifier_policy=requested_verifier_policy,
            effective_verifier_policy=effective_verifier_policy,
            requested_verifier_phase_policy=requested_verifier_phase_policy,
            effective_verifier_phase_policy=effective_verifier_phase_policy,
            runtime_identity=runtime_identity,
            probe_identity=probe_identity,
            probe_results=results,
            observed_at=observed,
            valid_until=observed + valid_for,
            evaluated_at=observed,
        )
    finally:
        for server in (primary, alternate):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


__all__ = [
    "DEFAULT_PROBE_IMAGE",
    "PROBE_IMPLEMENTATION_VERSION",
    "run_docker_network_isolation_probe",
]
