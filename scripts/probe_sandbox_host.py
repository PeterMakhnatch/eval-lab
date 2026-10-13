#!/usr/bin/env python3
"""Credential-free host reachability evidence through Harbor's Docker lifecycle.

Run with the installed Harbor tool's Python, with the repository src/ on
PYTHONPATH. The three lanes use the same task network/resource baseline and an
already-present Python image; no agent, verifier, credentials, or model executes.
Only one probe container exists at a time. Example (from the repository root)::

    PYTHONPATH=src ~/.local/share/uv/tools/harbor/bin/python \
        scripts/probe_sandbox_host.py --phase before --output /tmp/before.json

Use --bridge-port for an already-running Claude bridge. The probe's empty,
unauthenticated messages request cannot make a model call. Use --ports-from
/tmp/before.json during the after probe to preserve every original listening
port and raw host IP, so changed listeners or DNS cannot hide an original route.
--host-ip adds a local IP explicitly. --check-public-egress additionally makes
one credential-free HTTPS HEAD request to the public npm registry per lane.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import http.client
import importlib.metadata
import inspect
import ipaddress
import json
import re
import shlex
import socket
import subprocess
import tempfile
import tomllib
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LANES = ("claude-code", "codex", "nop")
REQUIRED_PORTS = frozenset({8000, 8001, 8004, 54329, 55433})
HOST_ALIASES = ("host.docker.internal", "gateway.docker.internal")
DEFAULT_IMAGE = "python:3.13-slim-bookworm"
SOCKET_TIMEOUT_SEC = 0.5
PUBLIC_EGRESS_HOST = "registry.npmjs.org"


def run_command(arguments: list[str], *, timeout: int = 30) -> str:
    """Keep subprocess failures free of arbitrary stderr or environment output."""
    result = subprocess.run(
        arguments,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    # lsof uses 1 when no matching listeners exist.
    if result.returncode and not (arguments[0] == "lsof" and result.returncode == 1):
        raise RuntimeError(f"{arguments[0]} command failed (exit {result.returncode})")
    return result.stdout


def listening_ports(lsof_output: str, bridge_port: int | None = None) -> list[int]:
    """Parse only numeric ports from lsof's machine-readable name fields."""
    ports = set(REQUIRED_PORTS)
    for line in lsof_output.splitlines():
        if not line.startswith("n"):
            continue
        match = re.search(r":(\d+)(?:\s+\(LISTEN\))?$", line)
        if match and 1 <= int(match[1]) <= 65535:
            ports.add(int(match[1]))
    if bridge_port is not None:
        if not 1 <= bridge_port <= 65535:
            raise ValueError("bridge port must be in 1..65535")
        ports.add(bridge_port)
    return sorted(ports)


def local_host_ip(value: str) -> str:
    """Accept local/private IP literals only; the diagnostic has no remote targets."""
    address = ipaddress.ip_address(value)
    if (
        not (address.is_private or address.is_loopback or address.is_link_local)
        or address.is_multicast
        or address.is_unspecified
    ):
        raise argparse.ArgumentTypeError("--host-ip must be a local/private host IP literal")
    return str(address)


def prior_targets(report: dict[str, Any]) -> tuple[list[int], list[str]]:
    """Preserve prior host-only targets without allowing arbitrary remote scans."""
    ports = report.get("ports", [])
    if not isinstance(ports, list) or any(
        type(port) is not int or not 1 <= port <= 65535 for port in ports
    ):
        raise ValueError("prior probe report must contain valid numeric ports")
    host_ips = set()
    for lane in report.get("lanes", []):
        for target in lane.get("probe", {}).get("targets", []):
            if not isinstance(target, str):
                continue
            try:
                host_ips.add(local_host_ip(target))
            except (ValueError, argparse.ArgumentTypeError):
                # Host aliases remain intrinsic targets; global addresses never
                # become scanning targets just because a report contains them.
                continue
    return sorted(set(ports)), sorted(host_ips)


def task_digest(task: Path) -> str:
    """Pin the complete source task without recording source file contents."""
    digest = hashlib.sha256()
    for path in sorted(task.rglob("*")):
        if path.is_symlink():
            raise ValueError("probe task must not contain symlinks")
        if path.is_file():
            digest.update(path.relative_to(task).as_posix().encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def source_environment(task_toml: str, image: str) -> dict[str, Any]:
    """Copy the source network/resource baseline without task env or host secrets."""
    source = tomllib.loads(task_toml).get("environment", {})
    if source.get("network_mode", "public") != "public":
        raise ValueError("probe requires a public source baseline to keep one container")
    if source.get("os", "linux") != "linux":
        raise ValueError("probe requires a Linux task")
    if source.get("gpus") or source.get("tpu"):
        raise ValueError("probe does not allocate accelerators")
    result = {
        key: source[key]
        for key in (
            "network_mode",
            "allowed_hosts",
            "build_timeout_sec",
            "os",
            "cpus",
            "memory_mb",
            "storage_mb",
        )
        if key in source
    }
    result.update(docker_image=image, env={}, mcp_servers=[])
    return result


def default_gateways(route_text: str) -> list[str]:
    """Extract the Linux IPv4 default gateway without invoking shell commands."""
    result = set()
    for line in route_text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        try:
            raw = bytes.fromhex(fields[2])
            if len(raw) != 4 or not (int(fields[3], 16) & 2):
                continue
            result.add(".".join(str(byte) for byte in reversed(raw)))
        except ValueError:
            continue
    return sorted(result)


def probe_connection(
    request: tuple[str, int, str, str],
    *,
    connect: Callable[..., Any] = socket.create_connection,
) -> dict[str, Any]:
    """Record TCP reachability and numeric HTTP status; discard all response bytes."""
    target, port, method, path = request
    record: dict[str, Any] = {
        "target": target,
        "port": port,
        "method": method,
        "path": path,
        "tcp_reachable": False,
        "http_status": None,
        "error_type": None,
    }
    try:
        with connect((target, port), timeout=SOCKET_TIMEOUT_SEC) as connection:
            record["tcp_reachable"] = True
            connection.settimeout(SOCKET_TIMEOUT_SEC)
            body = b"{}" if method == "POST" else b""
            message = (
                f"{method} {path} HTTP/1.0\r\n"
                "Host: local-isolation-probe\r\n"
                "Connection: close\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode("ascii") + body
            connection.sendall(message)
            # Stop at the first line. Neither body nor headers enter the report.
            status_line = bytearray()
            while len(status_line) < 512 and b"\n" not in status_line:
                byte = connection.recv(1)
                if not byte:
                    break
                status_line.extend(byte)
            match = re.match(rb"HTTP/\d(?:\.\d)? ([1-5]\d\d)(?: |\r|\n)", status_line)
            if match:
                record["http_status"] = int(match[1])
            else:
                record["error_type"] = "NonHTTPResponse"
    except Exception as error:
        record["error_type"] = type(error).__name__
    return record


def public_egress_probe(
    *,
    connection_factory: Callable[..., Any] = http.client.HTTPSConnection,
) -> dict[str, Any]:
    """A fixed public installer endpoint; report status and never read its body."""
    record = {
        "target": PUBLIC_EGRESS_HOST,
        "scheme": "https",
        "port": 443,
        "method": "HEAD",
        "path": "/",
        "http_status": None,
        "error_type": None,
    }
    connection = None
    try:
        connection = connection_factory(PUBLIC_EGRESS_HOST, timeout=5)
        connection.request("HEAD", "/")
        record["http_status"] = connection.getresponse().status
    except Exception as error:
        record["error_type"] = type(error).__name__
    finally:
        if connection is not None:
            connection.close()
    return record


def inside_probe(
    ports: list[int],
    bridge_port: int | None,
    host_ips: list[str],
    *,
    resolve: Callable[..., Any] = socket.getaddrinfo,
    connect: Callable[..., Any] = socket.create_connection,
    route_text: str | None = None,
    map_requests: Callable[..., Iterable[Any]] = map,
    check_public_egress: bool = False,
    public_probe: Callable[..., dict[str, Any]] = public_egress_probe,
) -> dict[str, Any]:
    """I/O seams let tests exercise the exact in-container payload without sockets."""
    aliases = []
    raw_ips = set(host_ips)
    for alias in HOST_ALIASES:
        addresses: list[str] = []
        error_type = None
        try:
            addresses = sorted({entry[4][0] for entry in resolve(alias, None)})
            raw_ips.update(addresses)
        except Exception as error:
            error_type = type(error).__name__
        aliases.append({"target": alias, "addresses": addresses, "error_type": error_type})
    if route_text is None:
        try:
            route_text = Path("/proc/net/route").read_text()
        except OSError:
            route_text = ""
    gateways = default_gateways(route_text)
    raw_ips.update(gateways)
    targets = [*HOST_ALIASES, *sorted(raw_ips)]
    requests = [(target, port, "HEAD", "/") for target in targets for port in ports]
    if bridge_port is not None:
        requests.extend(
            (target, bridge_port, method, path)
            for target in targets
            for method, path in (("POST", "/v1/messages"), ("GET", "/v1/models"))
        )
    rows = list(map_requests(lambda request: probe_connection(request, connect=connect), requests))
    return {
        "aliases": aliases,
        "default_gateway_ips": gateways,
        "explicit_host_ips": host_ips,
        "targets": targets,
        "results": rows,
        "public_egress": public_probe() if check_public_egress else None,
    }


def payload_command(
    ports: list[int],
    bridge_port: int | None,
    host_ips: list[str],
    *,
    check_public_egress: bool = False,
) -> str:
    """Transfer only diagnostic code and local target metadata into the sandbox."""
    source = (
        "from __future__ import annotations\n"
        "import http.client, json, re, socket\n"
        "from pathlib import Path\n"
        "from typing import Any\n"
        "from collections.abc import Callable, Iterable\n"
        "from concurrent.futures import ThreadPoolExecutor\n"
        f"HOST_ALIASES = {HOST_ALIASES!r}\n"
        f"SOCKET_TIMEOUT_SEC = {SOCKET_TIMEOUT_SEC!r}\n"
        f"PUBLIC_EGRESS_HOST = {PUBLIC_EGRESS_HOST!r}\n"
        + "\n".join(inspect.getsource(function) for function in (default_gateways, probe_connection, public_egress_probe, inside_probe))
        + "\nwith ThreadPoolExecutor(max_workers=12) as executor:\n"
        + f"    result = inside_probe({ports!r}, {bridge_port!r}, {host_ips!r}, map_requests=executor.map, check_public_egress={check_public_egress!r})\n"
        + "print(json.dumps(result, sort_keys=True))\n"
    )
    encoded = base64.b64encode(source.encode()).decode("ascii")
    launcher = f"import base64; exec(base64.b64decode({encoded!r}))"
    return f"python3 -c {shlex.quote(launcher)}"


def sanitized_container(container: dict[str, Any]) -> dict[str, Any]:
    """Inspect evidence excludes Config.Env, logs, mounts, and arbitrary options."""
    host = container.get("HostConfig") or {}
    networks = container.get("NetworkSettings", {}).get("Networks") or {}
    return {
        "id": container.get("Id"),
        "network_mode": host.get("NetworkMode"),
        "extra_hosts": host.get("ExtraHosts"),
        "privileged": host.get("Privileged"),
        "cap_add": host.get("CapAdd"),
        "cap_drop": host.get("CapDrop"),
        "security_opt": host.get("SecurityOpt"),
        "networks": {
            name: {
                key: details.get(key)
                for key in ("NetworkID", "IPAddress", "Gateway", "GlobalIPv6Address", "IPv6Gateway")
            }
            for name, details in networks.items()
        },
    }


def sanitized_network(network: dict[str, Any]) -> dict[str, Any]:
    return {
        key: network.get(key)
        for key in ("Id", "Name", "Driver", "Internal", "EnableIPv6")
    } | {"IPAM": {"Config": (network.get("IPAM") or {}).get("Config")}}


def probe_environment_type(phase: str) -> type:
    # Optional Harbor imports must not prevent base-environment unit collection.
    if phase == "before":
        from harbor.environments.docker.docker import DockerEnvironment

        native_type = DockerEnvironment
        isolation_record_name = None
    else:
        from evallab.harbor_host_isolation import (
            ISOLATION_RECORD,
            HostIsolatedDockerEnvironment,
        )

        native_type = HostIsolatedDockerEnvironment
        isolation_record_name = ISOLATION_RECORD

    class ExistingImageProbeEnvironment(native_type):
        _probe_isolation_record = isolation_record_name

        async def _run_docker_compose_command(self, command, **kwargs):
            command = list(command)
            if command[0] == "up":
                command.extend(["--pull", "never"])
            # Native delete requests `--rmi local`; this shared image is not ours.
            if "--rmi" in command:
                index = command.index("--rmi")
                del command[index : index + 2]
            return await super()._run_docker_compose_command(command, **kwargs)

    return ExistingImageProbeEnvironment


async def probe_lane(
    *,
    phase: str,
    lane: str,
    environment_type: type,
    environment_config: Any,
    root: Path,
    ports: list[int],
    bridge_port: int | None,
    host_ips: list[str],
    check_public_egress: bool = False,
    run: Callable[..., str] = run_command,
    trial_paths_type: type | None = None,
) -> dict[str, Any]:
    if trial_paths_type is None:
        from harbor.models.trial.paths import TrialPaths

        trial_paths_type = TrialPaths
    paths = trial_paths_type(root / lane / "trial")
    paths.mkdir()
    environment_dir = root / lane / "environment"
    environment_dir.mkdir(parents=True)
    project = f"evallab-host-probe-{uuid.uuid4().hex}"
    arguments: dict[str, Any] = {
        "environment_dir": environment_dir,
        "environment_name": f"host-probe-{lane}",
        "session_id": project,
        "trial_paths": paths,
        "task_env_config": environment_config,
        "network_policy": environment_config.resolve_baseline(),
        "keep_containers": False,
        "enable_environment_dir_upload": False,
    }
    if phase == "after":
        arguments["isolation_lane"] = lane
        arguments["claude_bridge_port"] = bridge_port if lane == "claude-code" else None
    environment = environment_type(**arguments)
    try:
        await environment.start(force_build=False)
        ids = run(
            ["docker", "ps", "--all", "--quiet", "--filter", f"label=com.docker.compose.project={project}"]
        ).split()
        if len(ids) != 1:
            raise RuntimeError("probe requires exactly one own container")
        container = json.loads(run(["docker", "container", "inspect", ids[0]]))[0]
        network = sanitized_container(container)
        network_names = list(network["networks"])
        networks = (
            [sanitized_network(item) for item in json.loads(run(["docker", "network", "inspect", *network_names]))]
            if network_names
            else []
        )
        result = await environment.exec(
            payload_command(ports, bridge_port, host_ips, check_public_egress=check_public_egress),
            timeout_sec=max(90, len(ports) * 4),
        )
        if result.return_code != 0:
            raise RuntimeError("in-container diagnostic failed")
        record_name = getattr(environment_type, "_probe_isolation_record", None)
        record_path = root / lane / "trial" / record_name if record_name else None
        isolation_record = (
            json.loads(record_path.read_text())
            if record_path is not None and record_path.is_file()
            else None
        )
        return {
            "lane": lane,
            "environment_class": f"{environment_type.__mro__[1].__module__}:{environment_type.__mro__[1].__name__}",
            "container": network,
            "networks": networks,
            "probe": json.loads(result.stdout),
            "isolation_record": isolation_record,
        }
    finally:
        await environment.stop(delete=True)
        remaining_containers = run(
            ["docker", "ps", "--all", "--quiet", "--filter", f"label=com.docker.compose.project={project}"]
        ).split()
        remaining_networks = run(
            ["docker", "network", "ls", "--quiet", "--filter", f"label=com.docker.compose.project={project}"]
        ).split()
        if remaining_containers or remaining_networks:
            raise RuntimeError("probe cleanup incomplete; refusing to start another lane")


async def run_probe(arguments: argparse.Namespace) -> dict[str, Any]:
    from harbor.models.task.config import EnvironmentConfig

    repo = Path(__file__).resolve().parents[1]
    task = arguments.task.resolve()
    # This guard plus Compose --pull never prevents even an implicit pull.
    run_command(["docker", "image", "inspect", "--format", "{{.Id}}", arguments.image])
    if any(
        (task / "environment" / name).exists()
        for name in ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml")
    ):
        raise ValueError("probe refuses task Compose overrides; source network must be unambiguous")
    task_toml = (task / "task.toml").read_text()
    baseline = source_environment(task_toml, arguments.image)
    config = EnvironmentConfig.model_validate(baseline)
    ports = listening_ports(
        run_command(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "n"]),
        arguments.bridge_port,
    )
    host_ips = set(arguments.host_ip)
    prior_sha256 = None
    if getattr(arguments, "ports_from", None) is not None:
        prior_bytes = arguments.ports_from.read_bytes()
        previous_ports, previous_host_ips = prior_targets(json.loads(prior_bytes))
        ports = sorted(set(ports) | set(previous_ports))
        host_ips.update(previous_host_ips)
        prior_sha256 = hashlib.sha256(prior_bytes).hexdigest()
    host_ips = sorted(host_ips)
    check_public_egress = getattr(arguments, "check_public_egress", False)
    report = {
        "schema_version": 1,
        "phase": arguments.phase,
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "repo_sha": run_command(["git", "-C", str(repo), "rev-parse", "HEAD"]).strip(),
        "harbor_version": importlib.metadata.version("harbor"),
        "task": str(task.relative_to(repo)) if task.is_relative_to(repo) else task.name,
        "task_sha256": task_digest(task),
        "task_toml_sha256": hashlib.sha256(task_toml.encode()).hexdigest(),
        "image": arguments.image,
        "source_environment": baseline,
        "ports": ports,
        "bridge_port": arguments.bridge_port,
        "host_ips": host_ips,
        "prior_probe_sha256": prior_sha256,
        "public_egress_requested": check_public_egress,
        "credential_free": True,
        "model_calls": 0,
        "lanes": [],
    }
    environment_type = probe_environment_type(arguments.phase)
    with tempfile.TemporaryDirectory(prefix="evallab-host-probe-", dir="/tmp") as scratch:
        for lane in LANES:
            report["lanes"].append(
                await probe_lane(
                    phase=arguments.phase,
                    lane=lane,
                    environment_type=environment_type,
                    environment_config=config,
                    root=Path(scratch),
                    ports=ports,
                    bridge_port=arguments.bridge_port,
                    host_ips=host_ips,
                    check_public_egress=check_public_egress,
                )
            )
    return report


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("before", "after"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task", type=Path, default=repo / "library/tasks/event-summary")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--bridge-port", type=int)
    parser.add_argument("--host-ip", type=local_host_ip, action="append", default=[])
    parser.add_argument("--ports-from", type=Path, help="preserve ports/raw local IPs from a prior probe report")
    parser.add_argument("--check-public-egress", action="store_true", help="also HEAD the public npm registry over HTTPS without credentials")
    arguments = parser.parse_args()
    if arguments.bridge_port is not None and not 1 <= arguments.bridge_port <= 65535:
        parser.error("--bridge-port must be in 1..65535")
    report = asyncio.run(run_probe(arguments))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(str(arguments.output))


if __name__ == "__main__":
    main()
