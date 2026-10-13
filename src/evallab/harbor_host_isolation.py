"""Multi-service Docker host isolation, enforced before sandbox execution.

Reuse the creation/mount/privilege validation of the strict egress-lock backend,
then attach a fresh isolated bridge only after installing a kernel destination
filter in every project container. The sandbox's ordinary root lacks NET_ADMIN
and NET_RAW. Only the trusted controller briefly uses privileged exec to
install/inspect the matched nft tool.

Tasks that ship their own ``environment/docker-compose.yaml`` (e.g. CheatBench
checker sidecars) run each declared service under the same creation-time lock
as ``main``: ``network_mode: none``, immutable capability drops and
``no-new-privileges`` on every container, no host bind mounts on sidecars, and
a per-container nft destination boundary installed before any network is
attached. Sidecars keep their own entrypoint so their servers start normally.
The shared project network afterwards uses a subnet outside the filter's
blocked ranges (Docker's default pools are all blocked on purpose), allocated
per sandbox from ``_ISOLATION_SUBNET_CANDIDATES``; on an ``--internal`` network
that subnet provably cannot route anywhere but the project containers.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import tempfile
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from harbor.constants import MAIN_SERVICE_NAME  # ty: ignore[unresolved-import]
from harbor.environments.capabilities import (  # ty: ignore[unresolved-import]
    EnvironmentCapabilities,
)
from harbor.environments.docker.docker import (  # ty: ignore[unresolved-import]
    DockerEnvironment,
    _sanitize_docker_compose_project_name,
)
from harbor.models.task.config import NetworkMode, NetworkPolicy  # ty: ignore[unresolved-import]

from evallab.harbor_docker import _SECURITY_OPTIONS, LockedDockerEnvironment
from evallab.host_isolation import desktop_dns_addresses, nft_objects, nft_rules

ISOLATION_PORT_ENV = "EVALLAB_CLAUDE_GATEWAY_ISOLATION_PORT"
ISOLATION_RECORD = "host-isolation.json"
ISOLATION_RECORD_PREFIX = "host-isolation"
_TOOL_BASE = "alpine:3.23.4@sha256:5b10f432ef3da1b8d4c7eb6c487f2f5a8f096bc91145e68878dd4a5019afde11"
_TOOL_TARGET = "/opt/evallab-host-isolation"
_PHASE_SWITCH_MODES = frozenset({NetworkMode.PUBLIC, NetworkMode.NO_NETWORK})
#: Per-sandbox project-network subnets, tried in order until `docker network
#: create --subnet` succeeds (a stale network may hold an earlier slice).
#: Docker's default pools (rfc1918/100.64/10, Docker Desktop host space) are
#: ALL denied by the nft destination boundary on purpose, so a default-pool
#: subnet would leave `main` unable to reach its own sidecars (verified:
#: connection refused under the installed filter). These /28 slices sit in
#: 192.31.196.0/24 (AS112-v4 sink, RFC 7535): no legitimate sandbox
#: destination lives there, and on an `--internal` network the subnet cannot
#: route anywhere but the project containers. The public-mode shadow is 16
#: anycast-sink addresses, never registries, APIs, or the Claude bridge.
_ISOLATION_SUBNET_CANDIDATES = tuple(f"192.31.196.{base}/28" for base in range(0, 256, 16))


class HostIsolatedDockerEnvironment(LockedDockerEnvironment):
    """Native Harbor Docker with a mandatory controller/private-network boundary.

    Public internet is retained when the source baseline is public. Non-public
    allowlists are refused rather than silently weakened. Task-provided Compose
    sidecars are accepted and locked per service (see below). The explicit
    egress_lock backend remains the stronger all-network deny option and stays
    strict single-container.

    The single permitted phase switch is agent ``no-network`` ↔ setup/verifier
    ``public`` (the mimo-clean-v5 contract): the nft destination boundary is
    installed before the first public attachment and stays in force across
    every disconnect/reconnect, and the no-network phase keeps no attachment
    at all. Any allowlist or other phase mode is refused at construction and
    at switch time.

    Multi-service tasks (a task ``environment/docker-compose.yaml`` declaring
    sidecars besides ``main``, e.g. CheatBench checkers): the overlay applies
    creation-time lock settings to every service, every container is confirmed
    against the daemon evidence, each container gets its own nft boundary
    (sidecars always strict, never the Claude bridge exception), and the
    shared project network uses an isolated subnet (see
    ``_ISOLATION_SUBNET_CANDIDATES``) with per-service ``--alias`` names so
    ``main`` reaches sidecars without weakening the filter.
    """

    # Task sidecars are locked per service (overlay + daemon confirmation +
    # per-container nft), so task-provided Compose is accepted here while the
    # explicit egress-lock backend stays strict single-container.
    _allow_task_compose = True

    def __init__(
        self,
        *args: Any,
        isolation_lane: str = "nop",
        claude_bridge_port: int | None = None,
        **kwargs: Any,
    ) -> None:
        self._isolation_confirmed = False
        self._isolation_lane = isolation_lane
        raw_port = claude_bridge_port
        if raw_port is None and isolation_lane == "claude-code":
            value = os.environ.get(ISOLATION_PORT_ENV)
            raw_port = int(value) if value is not None else None
        if raw_port is not None and isolation_lane != "claude-code":
            raise ValueError("only the Claude lane may admit a host bridge")
        if raw_port is not None:
            # Validate the complete tuple before Docker work, not after creation.
            nft_rules((), ("192.0.2.1",), raw_port)
        self._bridge_port = raw_port
        self._bridge_addresses: tuple[str, ...] = ()
        self._host_addresses: tuple[str, ...] = ()
        self._rules = ""
        self._rules_listing = ""
        self._declared_phase_policies: tuple[NetworkPolicy, ...] = ()
        self._phase_transitions: list[dict[str, Any]] = []
        self._tool_temp: tempfile.TemporaryDirectory[str] | None = None
        self._tool_image_digest: str | None = None
        self._network_id: str | None = None
        self._network_name = f"evallab-isolation-{uuid.uuid4().hex}"
        self._nft_command: list[str] = []
        self._record_path: Path | None = None
        self._service_container_ids: dict[str, str] = {}
        self._service_rules: dict[str, str] = {}
        self._service_listings: dict[str, str] = {}
        self._isolation_subnet: str | None = None
        if "egress_lock" in kwargs:
            raise ValueError("use LockedDockerEnvironment for explicit egress_lock")
        declared, args, kwargs = self._extract_phase_policies(args, kwargs)
        baseline = self._extract_baseline(args, kwargs)
        for policy in declared:
            if policy == baseline:
                continue
            if (
                policy.network_mode not in _PHASE_SWITCH_MODES
                or baseline.network_mode not in _PHASE_SWITCH_MODES
            ):
                raise ValueError(
                    "host-isolation refuses phase network policies outside "
                    "the no-network/public pair"
                )
        self._declared_phase_policies = tuple(declared)
        # The strict base refuses every switch; forward only baseline repeats
        # and keep the declared none/public pair for the phase-switch path.
        forward = [policy for policy in declared if policy == baseline]
        args, kwargs = self._replace_phase_policies(args, kwargs, forward)
        super().__init__(*args, egress_lock=True, **kwargs)

    @staticmethod
    def _extract_phase_policies(
        args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> tuple[list[NetworkPolicy], tuple[Any, ...], dict[str, Any]]:
        if "phase_network_policies" in kwargs:
            return list(kwargs["phase_network_policies"]), args, kwargs
        if len(args) >= 8:
            return list(args[7]), args, kwargs
        return [], args, kwargs

    @staticmethod
    def _replace_phase_policies(
        args: tuple[Any, ...], kwargs: dict[str, Any], forward: list[NetworkPolicy]
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        if "phase_network_policies" in kwargs:
            return args, {**kwargs, "phase_network_policies": forward}
        if len(args) >= 8:
            return (*args[:7], forward, *args[8:]), kwargs
        return args, {**kwargs, "phase_network_policies": forward}

    @staticmethod
    def _extract_baseline(args: tuple[Any, ...], kwargs: dict[str, Any]) -> NetworkPolicy:
        network_policy = kwargs.get("network_policy")
        if network_policy is None and len(args) >= 7:
            network_policy = args[6]
        if network_policy is not None:
            return network_policy
        task_env_config = kwargs.get("task_env_config")
        if task_env_config is None and len(args) >= 5:
            task_env_config = args[4]
        if task_env_config is None:
            raise ValueError("host-isolation requires a task environment config")
        return task_env_config.resolve_baseline()

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(
            disable_internet=True, mounted=True, dynamic_network_policy=True
        )

    def validate_network_policy_support(self, network_policy: NetworkPolicy | None = None) -> None:
        policy = network_policy or self.network_policy
        if policy.network_mode not in _PHASE_SWITCH_MODES:
            raise ValueError("host-isolation refuses network allowlists")
        # Both directions of the no-network/public pair are applied through
        # disconnect/reconnect with the nft boundary preserved; the strict
        # base-class switch refusal does not apply to this pair.

    @classmethod
    def _engine_cmd(cls, *command: str) -> list[str]:
        native = getattr(DockerEnvironment, "_engine_cmd", None)
        return native(*command) if native is not None else ["docker", *command]

    def _task_compose_service_names(self) -> list[str]:
        """Service names declared by the task's own Compose file, if any.

        Merged after Harbor's base/build fragments and before this wrapper's
        lock overlay, so the overlay (and later the resolved-config and daemon
        confirmation) must cover exactly these plus ``main``. Anything
        unparseable fails closed: an unreadable task network boundary is never
        guessed.
        """
        candidates = [
            self.environment_dir / name
            for name in (
                "docker-compose.yaml",
                "docker-compose.yml",
                "compose.yaml",
                "compose.yml",
            )
        ]
        candidates.extend(getattr(self, "extra_docker_compose_paths", None) or [])
        names: list[str] = []
        for raw in candidates:
            candidate = raw if isinstance(raw, Path) else Path(raw)
            if not candidate.is_file():
                continue
            try:
                document = yaml.safe_load(candidate.read_text())
            except Exception as error:
                raise ValueError(
                    f"host-isolation cannot parse task Compose {candidate}: {error}"
                ) from error
            if document is None:
                continue
            if not isinstance(document, dict):
                raise ValueError(f"host-isolation refuses a non-mapping task Compose {candidate}")
            services = document.get("services", {})
            if not isinstance(services, dict):
                raise ValueError(
                    f"host-isolation refuses a task Compose {candidate} without services"
                )
            for name in services:
                if not isinstance(name, str) or not name:
                    raise ValueError(f"host-isolation refuses a non-string service in {candidate}")
                if name not in names:
                    names.append(name)
        return sorted(names)

    @property
    def _docker_compose_paths(self) -> list[Path]:
        paths = super()._docker_compose_paths
        services: dict[str, Any] = {
            MAIN_SERVICE_NAME: {
                "network_mode": "none",
                "entrypoint": ["/bin/sh", "-c", "exec sleep infinity"],
                "command": [],
                "healthcheck": {"disable": True},
                "cap_drop": ["NET_ADMIN", "NET_RAW"],
                "security_opt": ["no-new-privileges:true"],
                "user": "0",
            }
        }
        for name in self._task_compose_service_names():
            if name == MAIN_SERVICE_NAME:
                continue
            # Sidecars keep their own entrypoint/command/user/healthcheck so
            # their servers start normally; every network/capability boundary
            # is still forced at creation.
            services[name] = {
                "network_mode": "none",
                "cap_drop": ["NET_ADMIN", "NET_RAW"],
                "security_opt": ["no-new-privileges:true"],
            }
        path = paths[-1].with_name("host-isolation.json")
        path.write_text(json.dumps({"services": services}))
        return [*paths, path]

    def _validate_sidecar_service(self, name: str, service: Mapping[str, Any]) -> None:
        if service.get("network_mode") != "none" or service.get("networks"):
            raise RuntimeError(f"host-isolation sidecar {name!r} Compose network_mode is not none")
        unsafe = (
            "privileged",
            "ports",
            "devices",
            "device_cgroup_rules",
            "volumes_from",
            "links",
            "pid",
            "ipc",
            "uts",
            "userns_mode",
            "cgroup",
            "cgroup_parent",
            "gpus",
            "cap_add",
            "secrets",
            "configs",
            "use_api_socket",
            "post_start",
            "pre_stop",
            "develop",
            "provider",
        )
        # `expose` is harmless metadata (no published ports without a
        # PortBinding, which the daemon confirmation separately refuses), so
        # unlike the strict single-container backend it is not refused here.
        if any(service.get(key) for key in unsafe) or service.get("runtime") not in (
            None,
            "runc",
        ):
            raise RuntimeError(f"host-isolation refuses unsafe sidecar {name!r} host access")
        if set(service.get("security_opt") or []) - _SECURITY_OPTIONS or not service.get(
            "security_opt"
        ):
            raise RuntimeError(
                f"host-isolation sidecar {name!r} requires no-new-privileges "
                "without security overrides"
            )
        if not {"NET_ADMIN", "NET_RAW"}.issubset(service.get("cap_drop", [])):
            raise RuntimeError(f"host-isolation sidecar {name!r} must drop NET_ADMIN and NET_RAW")
        if service.get("volumes"):
            raise RuntimeError(f"host-isolation sidecar {name!r} must not mount host paths")

    async def _validate_resolved_compose(self) -> dict[str, Any]:
        services = await self._resolved_compose_services()
        expected = {MAIN_SERVICE_NAME} | set(self._task_compose_service_names())
        if set(services) != expected:
            raise RuntimeError(
                "host-isolation Compose services do not match the task definition: "
                + json.dumps({"expected": sorted(expected), "found": sorted(services)})
            )
        main = services[MAIN_SERVICE_NAME]
        self._validate_locked_main_service(main)
        if (
            main.get("entrypoint") != ["/bin/sh", "-c", "exec sleep infinity"]
            or main.get("command") != []
            or str(main.get("user")) != "0"
            or main.get("healthcheck", {}).get("disable") is not True
            or not {"NET_ADMIN", "NET_RAW"}.issubset(main.get("cap_drop", []))
        ):
            raise RuntimeError("host-isolation trusted creation settings are unconfirmed")
        for name in sorted(expected):
            if name != MAIN_SERVICE_NAME:
                service = services[name]
                if not isinstance(service, dict):
                    raise RuntimeError(f"host-isolation sidecar {name!r} resolved to a non-mapping")
                self._validate_sidecar_service(name, service)
        return main

    async def _confirm_lock(self) -> None:
        expected = {MAIN_SERVICE_NAME} | set(self._task_compose_service_names())
        result = await super()._run_docker_compose_command(
            ["ps", "--all", "--quiet"], timeout_sec=10
        )
        ids: list[str] = str(result.stdout or "").split()
        if len(ids) != len(expected):
            raise RuntimeError(
                "host-isolation cannot confirm every Compose service: "
                + json.dumps({"expected": sorted(expected), "containers": len(ids)})
            )
        inspected = await self._daemon_json("container", "inspect", *ids)
        if (
            not isinstance(inspected, list)
            or len(inspected) != len(ids)
            or not all(isinstance(container, dict) for container in inspected)
        ):
            raise RuntimeError("host-isolation received invalid Docker inspect evidence")
        by_service: dict[str, dict[str, Any]] = {}
        for container in inspected:
            service = (container.get("Config", {}) or {}).get("Labels", {}) or {}
            name = service.get("com.docker.compose.service")
            if not isinstance(name, str) or not name:
                raise RuntimeError(
                    "host-isolation Compose service identity is unconfirmed: non-string service"
                )
            if name in expected and name not in by_service:
                by_service[name] = container
        if set(by_service) != expected:
            raise RuntimeError(
                "host-isolation Compose service identity is unconfirmed: "
                + json.dumps({"expected": sorted(expected), "found": sorted(by_service)})
            )
        project = _sanitize_docker_compose_project_name(self.session_id)
        evidence: dict[str, Any] = {}
        for name in sorted(by_service):
            container = by_service[name]
            await self._verify_service_container(container, service=name, project=project, ids=ids)
            config = container.get("Config", {})
            evidence[name] = {
                key: config.get(key) for key in ("Entrypoint", "Cmd", "User", "Healthcheck")
            }
        main = by_service[MAIN_SERVICE_NAME]
        host = main.get("HostConfig")
        self._container_id = main.get("Id")
        service_ids: dict[str, str] = {}
        for name in sorted(by_service):
            cid = by_service[name].get("Id")
            if not isinstance(cid, str):
                self._lock_confirmed = False
                raise RuntimeError(f"host-isolation container identity is unconfirmed: {name}")
            service_ids[name] = cid
        self._service_container_ids = service_ids
        self._native_evidence = {
            "Id": self._container_id,
            "Platform": main.get("Platform"),
            "State": main.get("State"),
            "HostConfig": {
                key: host.get(key)
                for key in (
                    "NetworkMode",
                    "Privileged",
                    "CapAdd",
                    "CapDrop",
                    "SecurityOpt",
                    "PidMode",
                    "IpcMode",
                    "UTSMode",
                    "UsernsMode",
                    "CgroupnsMode",
                    "Runtime",
                    "Devices",
                    "DeviceRequests",
                    "DeviceCgroupRules",
                    "VolumesFrom",
                    "PortBindings",
                    "PublishAllPorts",
                )
            }
            if isinstance(host, dict)
            else host,
            "NetworkSettings": main.get("NetworkSettings"),
            "Mounts": main.get("Mounts"),
            "services": evidence,
        }
        config = main.get("Config", {})
        drops = {
            cap.removeprefix("CAP_")
            for cap in ((main.get("HostConfig", {}) or {}).get("CapDrop") or [])
        }
        if (
            config.get("Entrypoint") != ["/bin/sh", "-c", "exec sleep infinity"]
            or config.get("Cmd") not in (None, [])
            or config.get("User") != "0"
            or config.get("Healthcheck", {}).get("Test") != ["NONE"]
            or not {"NET_ADMIN", "NET_RAW"}.issubset(drops)
        ):
            self._lock_confirmed = False
            raise RuntimeError(
                "host-isolation daemon creation settings are unconfirmed: "
                + json.dumps(
                    {key: config.get(key) for key in ("Entrypoint", "Cmd", "User", "Healthcheck")}
                    | {"CapDrop": sorted(drops)}
                )
            )
        self._native_evidence["creation"] = {
            key: config.get(key) for key in ("Entrypoint", "Cmd", "User", "Healthcheck")
        }
        if not isinstance(self._container_id, str):
            self._lock_confirmed = False
            raise RuntimeError("host-isolation main container identity is unconfirmed")
        self._write_lock_record(applied=True)
        self._lock_confirmed = True

    async def _verify_service_container(
        self, container: dict[str, Any], *, service: str, project: str, ids: list[str]
    ) -> None:
        """Confirm one project container's creation-time lock before any exec.

        Mirrors the strict backend's daemon evidence checks on every service,
        with the strictly stronger capability requirement (both ``NET_ADMIN``
        and ``NET_RAW`` dropped). ``main`` additionally matches the Harbor
        trial mounts; sidecars must carry no bind mounts at all.
        """
        container_id = container.get("Id")
        host = container.get("HostConfig")
        networks = container.get("NetworkSettings")
        mounts = container.get("Mounts")
        labels = (container.get("Config", {}) or {}).get("Labels") or {}
        if (
            not isinstance(container_id, str)
            or not any(container_id.startswith(prefix) for prefix in ids)
            or container.get("Platform") != "linux"
            or container.get("State", {}).get("Running") is not True
            or labels.get("com.docker.compose.project") != project
            or labels.get("com.docker.compose.service") != service
        ):
            raise RuntimeError(
                f"host-isolation container identity/OS/running state is unconfirmed: {service}"
            )
        if (
            not isinstance(host, dict)
            or not isinstance(networks, dict)
            or not isinstance(mounts, list)
        ):
            raise RuntimeError(
                f"host-isolation lacks Docker host/network/mount evidence: {service}"
            )
        if host.get("NetworkMode") != "none" or networks.get("Ports"):
            raise RuntimeError(
                f"host-isolation Docker container has network attachments: {service}"
            )
        await self._validate_network_attachments(networks.get("Networks"))
        required = {
            "CapAdd",
            "Devices",
            "DeviceRequests",
            "DeviceCgroupRules",
            "VolumesFrom",
            "PortBindings",
            "Links",
            "ExtraHosts",
            "CgroupParent",
        }
        if not required.issubset(host):
            raise RuntimeError(f"host-isolation lacks Docker host-access evidence: {service}")
        if host.get("Privileged") is not False or host.get("PublishAllPorts") is not False:
            raise RuntimeError(
                f"host-isolation Docker privilege/port flags are unsafe or unknown: {service}"
            )
        if any(
            host.get(key)
            for key in (
                "CapAdd",
                "Devices",
                "DeviceRequests",
                "DeviceCgroupRules",
                "VolumesFrom",
                "PortBindings",
                "Links",
                "ExtraHosts",
                "CgroupParent",
            )
        ):
            raise RuntimeError(f"host-isolation Docker container has unsafe host access: {service}")
        for key, allowed in {
            "PidMode": ("",),
            "IpcMode": ("private", ""),
            "UTSMode": ("",),
            "UsernsMode": ("",),
            "CgroupnsMode": ("private", ""),
            "Runtime": ("runc",),
        }.items():
            if host.get(key) not in allowed:
                raise RuntimeError(
                    f"host-isolation refuses unsafe or unknown Docker {key}: {service}"
                )
        drops = {cap.removeprefix("CAP_") for cap in (host.get("CapDrop") or [])}
        if not {"NET_ADMIN", "NET_RAW"}.issubset(drops):
            raise RuntimeError(f"host-isolation Docker capability drops are unconfirmed: {service}")
        options = host.get("SecurityOpt")
        if not isinstance(options, list) or not options or set(options) - _SECURITY_OPTIONS:
            raise RuntimeError(
                f"host-isolation Docker security options are unsafe or unknown: {service}"
            )
        if service == MAIN_SERVICE_NAME:
            self._check_locked_mounts(mounts)
        elif any(mount.get("Type") == "bind" for mount in mounts):
            raise RuntimeError(f"host-isolation sidecar carries host bind mounts: {service}")

    async def _prepare_tool(self) -> None:
        """Extract a matched tool bundle; remove the helper before the sandbox exists."""
        # No implicit image pulls. Preloading this pinned host-side build base is
        # an operator preparation step, distinct from sandbox/model traffic.
        endpoint = os.environ.get("DOCKER_HOST") if not os.environ.get("DOCKER_CONTEXT") else None
        if endpoint is None:
            endpoint = json.loads(
                await self._daemon_output(
                    "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"
                )
            )
        if not isinstance(endpoint, str) or not endpoint.startswith("unix://"):
            raise RuntimeError("host-isolation requires a local Unix Docker daemon endpoint")
        await self._daemon_output("image", "inspect", _TOOL_BASE)
        tag = f"evallab-isolation-tools:{uuid.uuid4().hex}"
        helper: str | None = None
        self._tool_temp = tempfile.TemporaryDirectory(prefix="evallab-isolation-tools-")
        target = Path(self._tool_temp.name)
        context = Path(__file__).resolve().parents[2] / "containers" / "host-isolation"
        try:
            await self._daemon_output("build", "--pull=false", "--tag", tag, str(context))
            info = await self._daemon_json("image", "inspect", tag)
            self._tool_image_digest = info[0]["Id"]
            helper = (
                await self._daemon_output(
                    "create",
                    "--name",
                    f"evallab-isolation-helper-{uuid.uuid4().hex}",
                    "--network",
                    "bridge",
                    "--entrypoint",
                    "/bin/sh",
                    tag,
                    "-c",
                    "ldd /usr/sbin/nft; nslookup -type=A host.docker.internal; nslookup -type=AAAA host.docker.internal"
                    if self._bridge_port is not None
                    else "ldd /usr/sbin/nft",
                )
            ).strip()
            output = await self._daemon_output("start", "--attach", helper)
            if self._bridge_port is not None:
                # The supervised bridge binds 127.0.0.1, so only Desktop's
                # IPv4 alias can name that frontend. An AAAA answer must not
                # admit a distinct IPv6-only host listener on the same port.
                self._bridge_addresses = tuple(
                    address
                    for address in desktop_dns_addresses(output)
                    if ipaddress.ip_address(address).version == 4
                )
                if not self._bridge_addresses:
                    raise ValueError("Claude bridge requires an IPv4 Docker host address")
            libraries = set(re.findall(r"(?:=>\s+)?(/\S+)\s+\(", output))
            loaders = [path for path in libraries if Path(path).name.startswith("ld-musl-")]
            if len(loaders) != 1 or not libraries:
                raise RuntimeError("host-isolation nft dependency bundle is unconfirmed")
            for source in sorted(libraries | {"/usr/sbin/nft"}):
                if not re.fullmatch(r"/(?:usr/lib|lib|usr/sbin)/[A-Za-z0-9._+-]+", source):
                    raise RuntimeError("host-isolation refuses an unexpected tool dependency")
                await self._daemon_output(
                    "cp", "-L", f"{helper}:{source}", str(target / Path(source).name)
                )
            self._nft_command = [
                f"{_TOOL_TARGET}/{Path(loaders[0]).name}",
                "--library-path",
                _TOOL_TARGET,
                f"{_TOOL_TARGET}/nft",
            ]
        finally:
            if helper is not None:
                await self._daemon_output("rm", "--force", helper)
            # This unique tag/image was created here, never a pre-existing image.
            with contextlib.suppress(Exception):
                await self._daemon_output("image", "rm", tag)

    async def _nft_in(self, container_id: str, *arguments: str, rules: str | None = None) -> str:
        return await self._daemon_output(
            "exec",
            "--privileged",
            "--user",
            "0",
            "-i",
            container_id,
            *self._nft_command,
            *arguments,
            stdin=rules,
        )

    async def _nft(self, *arguments: str, rules: str | None = None) -> str:
        return await self._nft_in(self._container_id or "", *arguments, rules=rules)

    async def _daemon_output(self, *command: str, stdin: str | None = None) -> str:
        # The base helper has a deliberately short daemon timeout. Host-side
        # tool builds need a bounded but longer timeout and nft needs stdin.
        import asyncio

        process = await asyncio.create_subprocess_exec(
            *type(self)._engine_cmd(*command),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(
                process.communicate(stdin.encode() if stdin is not None else None),
                timeout=180 if command[0] == "build" else 30,
            )
        except BaseException:
            await self._terminate_process(process)
            raise
        if process.returncode != 0:
            raise RuntimeError(
                f"host-isolation Docker {command[0]} failed (exit {process.returncode})"
            )
        return stdout.decode("utf-8")

    def _resolve_record_path(self) -> Path:
        """One record file per environment instance sharing a trial directory.

        The agent environment and each separate verifier environment share
        ``trial_dir`` but own distinct containers. The first writer keeps
        ``host-isolation.json``; a later environment whose container differs
        takes ``host-isolation-<session>.json`` so the agent phase-switch
        evidence is never overwritten by the verifier environment.
        """
        if self._record_path is not None:
            return self._record_path
        primary = self.trial_paths.trial_dir / ISOLATION_RECORD
        if self._record_owned_by_other(primary):
            safe = "".join(
                char if char.isalnum() or char in "-._" else "_" for char in self.session_id
            )
            self._record_path = (
                self.trial_paths.trial_dir / f"{ISOLATION_RECORD_PREFIX}-{safe}.json"
            )
        else:
            self._record_path = primary
        return self._record_path

    def _record_owned_by_other(self, path: Path) -> bool:
        if not path.is_file():
            return False
        try:
            existing = json.loads(path.read_text())
        except (OSError, ValueError):
            return False
        container_id = existing.get("container_id")
        return container_id is not None and container_id != self._container_id

    def _write_lock_record(self, *, applied: bool, error: BaseException | None = None) -> None:
        record = {
            "schema_version": 3,
            "recorded_at": datetime.now(UTC).isoformat(),
            "applied": applied and self._isolation_confirmed,
            "lane": self._isolation_lane,
            "session_id": self.session_id,
            "container_id": self._container_id,
            "service_container_ids": dict(self._service_container_ids),
            "isolation_subnet": self._isolation_subnet,
            "mechanism": "creation-time none; immutable NET_ADMIN/NET_RAW drops; nft inet output",
            "bridge_port": self._bridge_port,
            "bridge_addresses": self._bridge_addresses,
            "blocked_host_addresses": self._host_addresses,
            "rules": self._rules,
            "tool_image_digest": self._tool_image_digest,
            "startup_evidence": self._native_evidence,
            "network_id": self._network_id,
            "declared_phase_policies": [
                {
                    "network_mode": policy.network_mode.value,
                    "allowed_hosts": list(policy.allowed_hosts),
                }
                for policy in self._declared_phase_policies
            ],
            "phase_transitions": self._phase_transitions,
            "error_type": type(error).__name__ if error else None,
        }
        self.trial_paths.trial_dir.mkdir(parents=True, exist_ok=True)
        self._resolve_record_path().write_text(json.dumps(record, indent=2) + "\n")

    def _ordered_services(self) -> list[str]:
        """Deterministic service order: `main` first, then sidecars sorted."""
        services = self._service_container_ids or ({MAIN_SERVICE_NAME: self._container_id or ""})
        return [
            MAIN_SERVICE_NAME,
            *(name for name in sorted(services) if name != MAIN_SERVICE_NAME),
        ]

    async def _install_and_verify_rules(
        self, rules: str, rule_count: int, container_id: str
    ) -> str:
        """Install the destination boundary and return the confirmed kernel listing."""
        installed = nft_objects(
            json.loads(
                await self._nft_in(container_id, "--echo", "--json", "--file", "-", rules=rules)
            ),
            rule_count,
        )
        listing = await self._nft_in(
            container_id, "--json", "list", "table", "inet", "evallab_host_isolation"
        )
        if nft_objects(json.loads(listing), rule_count) != installed:
            raise RuntimeError("host-isolation kernel rules were not confirmed")
        return listing

    async def _container_networks(self, container_id: str | None = None) -> dict[str, Any]:
        cid = container_id or self._container_id
        assert cid is not None
        inspected = await self._daemon_json("container", "inspect", cid)
        networks = inspected[0].get("NetworkSettings", {}).get("Networks")
        if not isinstance(networks, dict):
            raise RuntimeError("host-isolation container network evidence is missing")
        return networks

    def _bridge_attached(self, networks: dict[str, Any]) -> bool:
        return self._network_id is not None and any(
            isinstance(endpoint, dict) and endpoint.get("NetworkID") == self._network_id
            for endpoint in networks.values()
        )

    async def _sandbox_interface_states(self, container_id: str | None = None) -> dict[str, str]:
        """Kernel operstate per sandbox interface, read through an unprivileged exec.

        A detached netns still lists kernel pseudo-devices (tunl0, sit0, ...);
        only operstate `up` marks a usable interface. The sandbox lacks
        NET_ADMIN, so it cannot bring an interface up; the daemon-side
        attachment evidence above is the primary proof and this census
        corroborates it from inside.
        """
        cid = container_id or self._container_id
        assert cid is not None
        output = await self._daemon_output(
            "exec",
            cid,
            "sh",
            "-c",
            "for i in /sys/class/net/*; do "
            "printf '%s %s\\n' \"${i##*/}\" "
            '"$(cat "$i"/operstate 2>/dev/null || echo unknown)"; done',
        )
        states: dict[str, str] = {}
        for line in output.splitlines():
            name, _, state = line.partition(" ")
            if name.strip():
                states[name.strip()] = state.strip()
        return states

    async def _confirm_no_network_state(self) -> dict[str, Any]:
        """Prove the sandbox has no attachment and no usable interface but lo."""
        per_service: dict[str, Any] = {}
        for service in self._ordered_services():
            cid = self._service_container_ids.get(service) or self._container_id
            networks = await self._container_networks(cid)
            if self._bridge_attached(networks):
                raise RuntimeError(
                    "host-isolation no-network phase keeps a public attachment: " + service
                )
            await self._validate_network_attachments(networks)
            states = await self._sandbox_interface_states(cid)
            up = sorted(name for name, state in states.items() if name != "lo" and state == "up")
            if up:
                raise RuntimeError(
                    "host-isolation no-network phase keeps unexpected interfaces up: "
                    + json.dumps({service: up})
                )
            per_service[service] = {
                "networks": sorted(networks),
                "interfaces": sorted(states),
                "interface_states": states,
            }
        evidence = dict(per_service[MAIN_SERVICE_NAME])
        if len(per_service) > 1:
            evidence["services"] = per_service
        return evidence

    async def _confirm_public_state(self) -> dict[str, Any]:
        """Prove the bridge is attached and the boundary survived the attach."""
        per_service: dict[str, Any] = {}
        for service in self._ordered_services():
            cid = self._service_container_ids.get(service) or self._container_id
            assert cid is not None
            networks = await self._container_networks(cid)
            if not self._bridge_attached(networks):
                raise RuntimeError(
                    "host-isolation public phase lacks its bridge attachment: " + service
                )
            listing = await self._nft_in(
                cid, "--json", "list", "table", "inet", "evallab_host_isolation"
            )
            if listing != self._service_listings.get(service, self._rules_listing):
                raise RuntimeError(
                    "host-isolation rules changed during network attachment: " + service
                )
            states = await self._sandbox_interface_states(cid)
            per_service[service] = {
                "networks": sorted(networks),
                "interfaces": sorted(states),
                "interface_states": states,
                "rules_confirmed": True,
            }
        evidence = dict(per_service[MAIN_SERVICE_NAME])
        if len(per_service) > 1:
            evidence["services"] = per_service
        return evidence

    async def _ensure_rules_before_reconnect(self) -> None:
        """Reinstall-and-confirm the boundary when it did not survive a detach."""
        for service in self._ordered_services():
            cid = self._service_container_ids.get(service) or self._container_id
            assert cid is not None
            expected = self._service_listings.get(service, self._rules_listing)
            listing = await self._nft_in(
                cid, "--json", "list", "table", "inet", "evallab_host_isolation"
            )
            if listing == expected:
                continue
            rules = self._service_rules.get(service, self._rules)
            count = 3 + len(self._host_addresses)
            if service == MAIN_SERVICE_NAME:
                count += len(self._bridge_addresses)
            listing = await self._install_and_verify_rules(rules, count, cid)
            self._service_listings[service] = listing
            if service == MAIN_SERVICE_NAME:
                self._rules_listing = listing

    async def _create_isolation_network(self, *, internal: bool) -> str:
        """Create the shared project network on an isolated, filter-safe subnet.

        Docker's default pools are all denied by the nft destination boundary,
        so the project network carries an explicit subnet outside the blocked
        ranges (see ``_ISOLATION_SUBNET_CANDIDATES``). Candidates are tried in
        order; a stale sandbox network may hold an earlier slice.
        """
        errors: list[str] = []
        for subnet in _ISOLATION_SUBNET_CANDIDATES:
            command = ["network", "create"]
            if internal:
                command.append("--internal")
            command.extend(["--subnet", subnet, self._network_name])
            try:
                network_id = (await self._daemon_output(*command)).strip()
            except RuntimeError as error:
                errors.append(f"{subnet}: {error}")
                continue
            self._isolation_subnet = subnet
            return network_id
        raise RuntimeError(
            "host-isolation could not allocate an isolated project subnet: " + "; ".join(errors)
        )

    def _connect_command(self, service: str, network_id: str, container_id: str) -> list[str]:
        command = ["network", "connect"]
        if len(self._service_container_ids) > 1:
            # Per-service DNS aliases so `main` reaches sidecars by name.
            command.extend(["--alias", service])
        command.extend([network_id, container_id])
        return command

    async def _apply_network_policy(self, network_policy: NetworkPolicy) -> None:
        # Harbor calls this only for real switches (set_network_policy returns
        # early on repeats); the support check above already refused allowlists.
        mode = network_policy.network_mode
        if mode not in _PHASE_SWITCH_MODES:
            raise ValueError("host-isolation refuses network allowlists")
        if network_policy == self.network_policy:
            return
        if not self._isolation_confirmed:
            raise RuntimeError("host-isolation phase switch refused before confirmation")
        assert self._container_id is not None
        services = self._ordered_services()
        cids = {
            service: self._service_container_ids.get(service) or self._container_id
            for service in services
        }
        if mode == NetworkMode.NO_NETWORK:
            for service in services:
                networks = await self._container_networks(cids[service])
                if self._bridge_attached(networks):
                    assert self._network_id is not None
                    await self._daemon_output(
                        "network", "disconnect", self._network_id, cids[service]
                    )
            evidence = await self._confirm_no_network_state()
        else:
            if self._network_id is None:
                self._network_id = await self._create_isolation_network(internal=False)
            # The filter lives in the container netns; reinstall-and-confirm
            # before the sandbox ever has a public attachment again.
            await self._ensure_rules_before_reconnect()
            for service in services:
                if not self._bridge_attached(await self._container_networks(cids[service])):
                    assert self._network_id is not None
                    await self._daemon_output(
                        *self._connect_command(service, self._network_id, cids[service])
                    )
            evidence = await self._confirm_public_state()
        self._phase_transitions.append(
            {
                "recorded_at": datetime.now(UTC).isoformat(),
                "policy": mode.value,
                **evidence,
            }
        )
        self._write_lock_record(applied=True)

    async def start(self, force_build: bool) -> None:
        try:
            await self._prepare_tool()
            self._service_container_ids = {}
            self._service_rules = {}
            self._service_listings = {}
            self._isolation_subnet = None
            await super().start(force_build=force_build)
            # The inherited inspect confirms creation-time none, safe mounts and
            # default capabilities before any copy/exec can take place.
            assert self._container_id is not None and self._tool_temp is not None
            assert self._service_container_ids, "host-isolation confirmed no services"
            from evallab.host_isolation import controller_addresses

            self._host_addresses = controller_addresses()
            self._rules = nft_rules(self._host_addresses, self._bridge_addresses, self._bridge_port)
            for service in self._ordered_services():
                cid = self._service_container_ids[service]
                await self._daemon_output("cp", self._tool_temp.name, f"{cid}:{_TOOL_TARGET}")
                if service == MAIN_SERVICE_NAME:
                    rules = self._rules
                    count = 3 + len(self._host_addresses) + len(self._bridge_addresses)
                else:
                    # Sidecars never admit the Claude bridge exception, even on
                    # the Claude lane: only main may reach the gateway.
                    rules = nft_rules(self._host_addresses, (), None)
                    count = 3 + len(self._host_addresses)
                self._service_rules[service] = rules
                listing = await self._install_and_verify_rules(rules, count, cid)
                self._service_listings[service] = listing
                if service == MAIN_SERVICE_NAME:
                    self._rules_listing = listing
            attach = self.network_policy.network_mode == NetworkMode.PUBLIC or (
                self.network_policy.network_mode == NetworkMode.NO_NETWORK
                and len(self._service_container_ids) > 1
            )
            if attach:
                internal = self.network_policy.network_mode == NetworkMode.NO_NETWORK
                if self._network_id is None:
                    self._network_id = await self._create_isolation_network(internal=internal)
                for service in self._ordered_services():
                    cid = self._service_container_ids[service]
                    await self._daemon_output("network", "disconnect", "none", cid)
                    assert self._network_id is not None
                    await self._daemon_output(
                        *self._connect_command(service, self._network_id, cid)
                    )
                # Attachment must preserve the same kernel namespace/rules.
                for service in self._ordered_services():
                    cid = self._service_container_ids[service]
                    if (
                        await self._nft_in(
                            cid, "--json", "list", "table", "inet", "evallab_host_isolation"
                        )
                        != self._service_listings[service]
                    ):
                        raise RuntimeError(
                            "host-isolation rules changed during network attachment: " + service
                        )
            self._isolation_confirmed = True
            self._write_lock_record(applied=True)
        except BaseException as error:
            self._isolation_confirmed = False
            self._write_lock_record(applied=False, error=error)
            with contextlib.suppress(Exception):
                await self.stop(delete=True)
            raise
        finally:
            if self._tool_temp is not None:
                self._tool_temp.cleanup()
                self._tool_temp = None

    async def stop(self, delete: bool) -> None:
        try:
            await super().stop(delete=delete)
        finally:
            self._isolation_confirmed = False
            if delete and self._network_id is not None:
                await self._daemon_output("network", "rm", self._network_id)
                self._network_id = None
