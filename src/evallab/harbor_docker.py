"""Fail-closed, single-container Harbor 0.24 Docker execution.

The final Compose overlay puts ``main`` in ``network_mode: none`` at creation;
there is no network-enabled setup interval, sidecar, or delayed disconnect.
Host-side Docker exec/copy still work. Image builds/pulls are host operations,
not sandbox traffic, and are not covered by this runtime egress lock. Agents
needing sandbox network during setup are deliberately incompatible.

This is network isolation, not a VM/container-escape guarantee. The Docker daemon
and host are trusted; an external daemon administrator can change containers.
Only Harbor's own trial log/artifact binds are accepted. Their host directories
must not contain secrets, sockets, or links to other host data. Linux Docker
Desktop can exercise containment, but says nothing about Hetzner performance.
CPU/memory/storage handling and stop/delete semantics remain Harbor-native.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import stat
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Harbor is optional in the base Lab install; native CI installs the pinned SDK.
from harbor.constants import MAIN_SERVICE_NAME  # ty: ignore[unresolved-import]
from harbor.environments.base import ExecResult  # ty: ignore[unresolved-import]
from harbor.environments.capabilities import (  # ty: ignore[unresolved-import]
    EnvironmentCapabilities,
)
from harbor.environments.docker.docker import (  # ty: ignore[unresolved-import]
    DockerEnvironment,
    _sanitize_docker_compose_project_name,
)
from harbor.models.task.config import (  # ty: ignore[unresolved-import]
    EnvironmentConfig,
    NetworkMode,
    NetworkPolicy,
    TaskOS,
)
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths  # ty: ignore[unresolved-import]

EGRESS_LOCK_RECORD = "egress-lock.json"
_MECHANISM = "docker-compose main network_mode=none from creation"
_SECURITY_OPTIONS = {"no-new-privileges", "no-new-privileges:true"}


def verifier_context_must_build(environment_dir: Path, docker_image: str | None) -> bool:
    """True when this build context is a separate-verifier ``tests/`` tree.

    Harbor copies ``[environment].docker_image`` onto the verifier when
    ``[verifier.environment]`` is absent. With ``force_build`` false it then
    runs that agent image and never builds ``tests/Dockerfile``, and it does
    not upload tests into a separate verifier. ``/tests/test.sh`` is missing.
    The agent context (``environment/``) keeps the prebuilt image.
    """
    if not docker_image:
        return False
    context = Path(environment_dir)
    return (
        context.name == "tests"
        and (context / "Dockerfile").is_file()
        and (context / "test.sh").is_file()
    )


class LockedDockerEnvironment(DockerEnvironment):
    """Native Docker lifecycle with an explicit, immutable egress lock.

    Import through ``evallab.harbor_docker:LockedDockerEnvironment`` and pass
    ``egress_lock=True``. No-network phases and baseline restores are logical
    assertions: the container stays network=none throughout. Other policy
    switches and network allowlists are refused, not adapted.
    GPU/device passthrough is outside this CPU sandbox's qualified boundary.
    """

    #: Only subclasses that extend every creation-time lock setting to each
    #: additional Compose service (e.g. host isolation with task sidecars) may
    #: accept a task-provided Compose file. The explicit egress-lock backend
    #: itself stays strict single-container.
    _allow_task_compose = False

    def __init__(
        self,
        environment_dir: Path,
        environment_name: str,
        session_id: str,
        trial_paths: TrialPaths,
        task_env_config: EnvironmentConfig,
        keep_containers: bool = False,
        network_policy: NetworkPolicy | None = None,
        phase_network_policies: Sequence[NetworkPolicy] = (),
        *,
        egress_lock: bool | None = None,
        **kwargs: Any,
    ) -> None:
        self.trial_paths = trial_paths
        self._lock_requested = egress_lock is True
        self._lock_confirmed = False
        self._lock_temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._daemon_info: dict[str, Any] = {}
        self._native_evidence: dict[str, Any] = {}
        self._container_id: str | None = None
        baseline = network_policy or task_env_config.resolve_baseline()
        self._lock_baseline = baseline
        try:
            if egress_lock is not True:
                raise ValueError("LockedDockerEnvironment requires explicit egress_lock=true")
            if task_env_config.os != TaskOS.LINUX:
                raise ValueError("egress_lock requires a Linux container")
            if not self._allow_task_compose and any(
                (environment_dir / name).exists()
                for name in (
                    "docker-compose.yaml",
                    "docker-compose.yml",
                    "compose.yaml",
                    "compose.yml",
                )
            ):
                raise ValueError("egress_lock refuses task-provided Compose")
            if kwargs.get("extra_docker_compose") or kwargs.get("extra_docker_compose_paths"):
                raise ValueError("egress_lock refuses extra Compose overlays")
            accepted = {
                "logger",
                "override_cpus",
                "override_memory_mb",
                "override_storage_mb",
                "override_gpus",
                "override_tpu",
                "cpu_enforcement_policy",
                "memory_enforcement_policy",
                "persistent_env",
                "mounts",
                "extra_docker_compose",
                "stream",
                "enable_environment_dir_upload",
            }
            if set(kwargs) - accepted:
                raise ValueError("egress_lock refuses unknown environment options")
            if any(
                policy != baseline and policy.network_mode != NetworkMode.NO_NETWORK
                for policy in phase_network_policies
            ):
                raise ValueError("egress_lock refuses phase network policy switches")
            if (task_env_config.gpus or 0) > 0 or (kwargs.get("override_gpus") or 0) > 0:
                raise ValueError("egress_lock CPU sandbox does not support GPU passthrough")
            super().__init__(
                environment_dir=environment_dir,
                environment_name=environment_name,
                session_id=session_id,
                trial_paths=trial_paths,
                task_env_config=task_env_config,
                keep_containers=keep_containers,
                network_policy=baseline,
                phase_network_policies=phase_network_policies,
                **kwargs,
            )
            self._validate_mount_specs(self._mounts)
        except Exception as error:
            self._write_lock_record(applied=False, error=error)
            raise

    @staticmethod
    def _requires_egress_control(**kwargs: Any) -> bool:
        # Native's sidecar is neither used nor probed (including on Darwin).
        return False

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        # Harbor requires this capability for a phase that differs logically
        # from the task baseline, even though our Docker network never changes.
        return EnvironmentCapabilities(
            disable_internet=True, mounted=True, dynamic_network_policy=True
        )

    def validate_network_policy_support(self, network_policy: NetworkPolicy | None = None) -> None:
        policy = network_policy or self.network_policy
        if policy.network_mode not in (NetworkMode.PUBLIC, NetworkMode.NO_NETWORK):
            raise ValueError("egress_lock refuses network allowlists")
        if policy != self._lock_baseline and policy.network_mode != NetworkMode.NO_NETWORK:
            raise ValueError("egress_lock refuses network policy switches")

    async def _apply_network_policy(self, network_policy: NetworkPolicy) -> None:
        """Assert a logical phase without changing the creation-time lock."""
        self.validate_network_policy_support(network_policy)
        if not self._lock_confirmed:
            raise RuntimeError("egress_lock phase assertions require a confirmed lock")
        # Harbor updates its logical policy after this assertion. Baseline
        # restoration never reconnects this network=none container.

    def _allowed_mounts(self) -> dict[str, Path]:
        paths = EnvironmentPaths()
        allowed = {
            str(paths.agent_dir): self.trial_paths.agent_dir,
            str(paths.verifier_dir): self.trial_paths.verifier_dir,
            str(paths.artifacts_dir): self.trial_paths.host_artifact_path(
                MAIN_SERVICE_NAME, str(paths.artifacts_dir)
            ),
        }
        # Harbor 0.21 has only the agent log mount; 0.24 adds user_agent.
        if hasattr(paths, "user_agent_dir"):
            allowed[str(paths.user_agent_dir)] = self.trial_paths.user_agent_dir
        return allowed

    def _validate_mount_specs(self, mounts: Sequence[Mapping[str, Any]]) -> None:
        allowed = self._allowed_mounts()
        trial_root = self.trial_paths.trial_dir.resolve()
        seen: set[str] = set()
        for mount in mounts:
            target = mount.get("target")
            source = mount.get("source")
            if mount.get("type") != "bind" or target not in allowed or not isinstance(source, str):
                raise ValueError("egress_lock allows only Harbor trial log/artifact bind mounts")
            expected = allowed[target].absolute()
            resolved = expected.resolve()
            if not resolved.is_relative_to(trial_root) or resolved == trial_root:
                raise ValueError("egress_lock refuses a Harbor mount escaping the trial directory")
            if (
                not Path(source).is_absolute()
                or Path(source).resolve() != resolved
                or target in seen
            ):
                raise ValueError("egress_lock refuses unexpected or duplicate host mount sources")
            seen.add(target)
            if set(mount) - {"type", "source", "target", "read_only", "bind"}:
                raise ValueError("egress_lock refuses unsupported mount options")
            bind = mount.get("bind") or {}
            if not isinstance(bind, dict) or set(bind) - {"selinux", "create_host_path"}:
                raise ValueError("egress_lock refuses bind propagation or unknown bind options")
            if bind.get("selinux") not in (None, "z", "Z"):
                raise ValueError("egress_lock refuses unknown bind labeling")
            if resolved.exists():
                if not resolved.is_dir():
                    raise ValueError("egress_lock log/artifact mounts must be directories")
                for entry in resolved.rglob("*"):
                    mode = entry.lstat().st_mode
                    if stat.S_ISLNK(mode):
                        if not entry.resolve().is_relative_to(resolved):
                            raise ValueError(
                                "egress_lock refuses host-data symlinks in trial mounts"
                            )
                    elif not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                        raise ValueError(
                            "egress_lock refuses sockets/devices/FIFOs in trial mounts"
                        )

    @property
    def _docker_compose_paths(self) -> list[Path]:
        if self._lock_temp_dir is None:
            self._lock_temp_dir = tempfile.TemporaryDirectory(prefix="evallab-docker-lock-")
            (Path(self._lock_temp_dir.name) / "lock.json").write_text(
                json.dumps(
                    {
                        "services": {
                            MAIN_SERVICE_NAME: {
                                "network_mode": "none",
                                "cap_drop": ["NET_RAW"],
                                "security_opt": ["no-new-privileges:true"],
                            }
                        }
                    }
                )
            )
        return [*super()._docker_compose_paths, Path(self._lock_temp_dir.name) / "lock.json"]

    def _validate_daemon_mode(self) -> None:
        if self._daemon_info.get("OSType") != "linux":
            raise RuntimeError("egress_lock requires a confirmed Linux Docker daemon")

    async def _daemon_output(self, *command: str) -> str:
        process = await asyncio.create_subprocess_exec(
            *type(self)._engine_cmd(*command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        except BaseException:
            await self._terminate_process(process)
            raise
        if process.returncode != 0:
            raise RuntimeError(f"Docker {command[0]} failed: {stderr.decode(errors='replace')}")
        return stdout.decode("utf-8")

    async def _daemon_json(self, *command: str) -> Any:
        return json.loads(await self._daemon_output(*command))

    async def _resolved_compose_services(self) -> dict[str, Any]:
        # Resolve actual native overlays before creation. Output-to-file avoids
        # mixing Compose warnings with JSON and is deleted immediately (env).
        path = self._docker_compose_paths[-1].with_name("resolved.json")
        try:
            await super()._run_docker_compose_command(
                ["config", "--format", "json", "--output", str(path)], timeout_sec=10
            )
            document = json.loads(path.read_text())
        finally:
            path.unlink(missing_ok=True)
        services = document.get("services")
        if not isinstance(services, dict):
            raise RuntimeError("egress_lock resolved Compose has no services")
        return services

    def _validate_locked_main_service(self, main: Mapping[str, Any]) -> None:
        if main.get("network_mode") != "none" or main.get("networks"):
            raise RuntimeError("egress_lock Compose network_mode is not none")
        unsafe = (
            "privileged",
            "ports",
            "expose",
            "devices",
            "device_cgroup_rules",
            "volumes_from",
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
        if any(main.get(key) for key in unsafe) or main.get("runtime") not in (None, "runc"):
            raise RuntimeError("egress_lock refuses unsafe Compose host access")
        if set(main.get("security_opt") or []) - _SECURITY_OPTIONS or not main.get("security_opt"):
            raise RuntimeError("egress_lock requires no-new-privileges without security overrides")
        if "NET_RAW" not in main.get("cap_drop", []):
            raise RuntimeError("egress_lock requires dropping NET_RAW")
        self._validate_mount_specs(main.get("volumes") or [])

    async def _validate_resolved_compose(self) -> dict[str, Any]:
        services = await self._resolved_compose_services()
        if set(services) != {MAIN_SERVICE_NAME}:
            raise RuntimeError("egress_lock requires exactly one main Compose service")
        main = services[MAIN_SERVICE_NAME]
        self._validate_locked_main_service(main)
        return main

    async def _sandbox_interface_states(self, container_id: str | None = None) -> dict[str, str]:
        """Corroborate daemon attachment evidence with a kernel interface census.

        Detached namespaces can retain down pseudo-devices (tunl0, sit0, ...).
        The sandbox lacks NET_ADMIN; daemon-side attachment evidence remains
        authoritative, rather than trusting this in-container census alone.
        """
        cid = container_id or self._container_id
        assert cid is not None
        output = await self._daemon_output(
            "exec",
            cid,
            "sh",
            "-c",
            "for i in /sys/class/net/*; do "
            '[ -d "$i" ] || continue; '
            "printf '%s %s\\n' \"${i##*/}\" "
            '"$(cat "$i"/operstate 2>/dev/null || echo unknown)"; done',
        )
        states: dict[str, str] = {}
        for line in output.splitlines():
            fields = line.split()
            if len(fields) != 2 or fields[0] in states:
                raise RuntimeError("Docker interface census is malformed")
            name, state = fields
            states[name] = state
        if "lo" not in states:
            raise RuntimeError("Docker interface census is missing loopback evidence")
        return states

    async def _confirm_lock(self) -> None:
        result = await super()._run_docker_compose_command(
            ["ps", "--all", "--quiet"], timeout_sec=10
        )
        ids = (result.stdout or "").split()
        if len(ids) != 1:
            raise RuntimeError("egress_lock cannot confirm a single Docker container")
        inspected = await self._daemon_json("container", "inspect", ids[0])
        if (
            not isinstance(inspected, list)
            or len(inspected) != 1
            or not isinstance(inspected[0], dict)
        ):
            raise RuntimeError("egress_lock received invalid Docker inspect evidence")
        container = inspected[0]
        self._container_id = container.get("Id")
        host = container.get("HostConfig")
        networks = container.get("NetworkSettings")
        mounts = container.get("Mounts")
        self._native_evidence = {
            "Id": self._container_id,
            "Platform": container.get("Platform"),
            "State": container.get("State"),
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
            "NetworkSettings": networks,
            "Mounts": mounts,
        }
        labels = container.get("Config", {}).get("Labels") or {}
        if (
            not isinstance(self._container_id, str)
            or not self._container_id.startswith(ids[0])
            or container.get("Platform") != "linux"
            or container.get("State", {}).get("Running") is not True
            or labels.get("com.docker.compose.project")
            != _sanitize_docker_compose_project_name(self.session_id)
            or labels.get("com.docker.compose.service") != MAIN_SERVICE_NAME
        ):
            raise RuntimeError("egress_lock container identity/OS/running state is unconfirmed")
        if (
            not isinstance(host, dict)
            or not isinstance(networks, dict)
            or not isinstance(mounts, list)
        ):
            raise RuntimeError("egress_lock lacks Docker host/network/mount evidence")
        if host.get("NetworkMode") != "none" or networks.get("Ports"):
            raise RuntimeError("egress_lock Docker container has network attachments")
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
            raise RuntimeError("egress_lock lacks Docker host-access evidence")
        if host.get("Privileged") is not False or host.get("PublishAllPorts") is not False:
            raise RuntimeError("egress_lock Docker privilege/port flags are unsafe or unknown")
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
            raise RuntimeError("egress_lock Docker container has unsafe host access")
        for key, allowed in {
            "PidMode": ("",),
            "IpcMode": ("private", ""),
            "UTSMode": ("",),
            "UsernsMode": ("",),
            "CgroupnsMode": ("private", ""),
            "Runtime": ("runc",),
        }.items():
            if host.get(key) not in allowed:
                raise RuntimeError(f"egress_lock refuses unsafe or unknown Docker {key}")
        if not any(cap in ("NET_RAW", "CAP_NET_RAW") for cap in (host.get("CapDrop") or [])):
            raise RuntimeError("egress_lock Docker NET_RAW drop is unconfirmed")
        options = host.get("SecurityOpt")
        if not isinstance(options, list) or not options or set(options) - _SECURITY_OPTIONS:
            raise RuntimeError("egress_lock Docker security options are unsafe or unknown")
        self._check_locked_mounts(mounts)
        states = await self._sandbox_interface_states()
        self._native_evidence["interface_states"] = states
        unexpected = {
            name: state
            for name, state in states.items()
            if name != "lo" and state != "down"
        }
        if unexpected:
            raise RuntimeError(
                "egress_lock Docker has non-loopback interfaces not confirmed down: "
                + json.dumps(unexpected, sort_keys=True)
            )
        self._write_lock_record(applied=True)
        self._lock_confirmed = True

    def _check_locked_mounts(self, mounts: list[Any]) -> None:
        expected = {(str(Path(m["source"]).resolve()), m["target"]): m for m in self._mounts}
        if len(mounts) != len(expected):
            raise RuntimeError("egress_lock Docker has unexpected mounts (including image volumes)")
        seen: set[tuple[str, str]] = set()
        for mount in mounts:
            key = (mount.get("Source"), mount.get("Destination"))
            spec = expected.get(key)
            if (
                mount.get("Type") != "bind"
                or spec is None
                or key in seen
                or mount.get("Propagation") not in ("rprivate", "private")
                or mount.get("RW") is not (not bool(spec.get("read_only")))
            ):
                raise RuntimeError("egress_lock Docker mount source/access is unsafe or unknown")
            seen.add(key)

    async def _validate_network_attachments(self, attachments: Any) -> None:
        if attachments == {}:
            return
        # Docker may report the built-in `none` pseudo-network, which creates
        # only loopback, rather than an empty Networks map. Never trust its name
        # alone: confirm the driver's native daemon identity and empty addresses.
        if not isinstance(attachments, dict) or set(attachments) != {"none"}:
            raise RuntimeError("egress_lock Docker container has network attachments")
        endpoint = attachments["none"]
        if not isinstance(endpoint, dict) or not endpoint.get("NetworkID"):
            raise RuntimeError("egress_lock none-driver endpoint is unconfirmed")
        for key in ("IPAddress", "Gateway", "GlobalIPv6Address", "IPv6Gateway", "MacAddress"):
            if endpoint.get(key) != "":
                raise RuntimeError(
                    "egress_lock none-driver endpoint has an address or missing evidence"
                )
        if endpoint.get("IPPrefixLen") != 0 or endpoint.get("GlobalIPv6PrefixLen") != 0:
            raise RuntimeError("egress_lock none-driver endpoint has an address prefix")
        inspected = await self._daemon_json("network", "inspect", endpoint["NetworkID"])
        if not isinstance(inspected, list) or len(inspected) != 1:
            raise RuntimeError("egress_lock none-driver network inspection is unconfirmed")
        network = inspected[0]
        if (
            not isinstance(network, dict)
            or network.get("Id") != endpoint["NetworkID"]
            or network.get("Name") != "none"
            or network.get("Driver") != "null"
            or network.get("Scope") != "local"
        ):
            raise RuntimeError("egress_lock refuses a non-none Docker network driver")
        self._native_evidence["none_network"] = {
            key: network.get(key) for key in ("Id", "Name", "Driver", "Scope")
        }

    async def _run_docker_compose_command(self, command: list[str], **kwargs: Any) -> ExecResult:
        operation = command[0]
        if operation in ("exec", "cp", "run") and not self._lock_confirmed:
            error = RuntimeError("egress_lock is unconfirmed; container execution/copy is refused")
            self._write_lock_record(applied=False, error=error)
            raise error
        if operation == "run":
            raise RuntimeError("egress_lock refuses additional containers")
        if operation == "up":
            await self._validate_resolved_compose()
        result = await super()._run_docker_compose_command(command, **kwargs)
        if operation == "up":
            await self._confirm_lock()
        return result

    def _write_lock_record(self, *, applied: bool, error: BaseException | None = None) -> None:
        now = datetime.now(UTC).isoformat()
        record = {
            "schema_version": 2,
            "requested": self._lock_requested,
            "applied": applied,
            "locked_at": now if applied else None,
            "recorded_at": now,
            "backend": "docker",
            "container_id": self._container_id,
            "network_block_all": True if applied else None,
            "mechanism": _MECHANISM,
            "native_evidence": self._native_evidence,
            "daemon": {
                key: self._daemon_info.get(key)
                for key in ("OSType", "OperatingSystem", "ServerVersion")
            },
            "error": f"{type(error).__name__}: {error}" if error is not None else None,
        }
        self.trial_paths.trial_dir.mkdir(parents=True, exist_ok=True)
        (self.trial_paths.trial_dir / EGRESS_LOCK_RECORD).write_text(
            json.dumps(record, indent=2) + "\n"
        )

    def _build_verifier_image_instead_of_agent_image(self) -> None:
        image = getattr(self.task_env_config, "docker_image", None)
        if not verifier_context_must_build(self.environment_dir, image):
            return
        self.task_env_config = self.task_env_config.model_copy(update={"docker_image": None})
        env_vars = getattr(self, "_env_vars", None)
        if env_vars is not None:
            env_vars.prebuilt_image_name = None

    async def start(self, force_build: bool) -> None:
        self._build_verifier_image_instead_of_agent_image()
        native_entered = False
        self._lock_confirmed = False
        self._daemon_info = {}
        self._native_evidence = {}
        self._container_id = None
        try:
            info = await self._daemon_json("info", "--format", "{{json .}}")
            if not isinstance(info, dict):
                raise RuntimeError("egress_lock Docker daemon information is unconfirmed")
            self._daemon_info = info
            self._validate_daemon_mode()
            self._validate_mount_specs(self._mounts)
            native_entered = True
            await super().start(force_build=force_build)
            if not self._lock_confirmed:
                raise RuntimeError("egress_lock Docker startup completed without confirmation")
        except BaseException as error:
            self._lock_confirmed = False
            try:
                self._write_lock_record(applied=False, error=error)
            finally:
                if native_entered:
                    with contextlib.suppress(Exception):
                        await self.stop(delete=True)
            raise

    async def prepare_logs_for_host(self) -> None:
        # Do not exec even cleanup commands in an unconfirmed container.
        if self._lock_confirmed:
            await super().prepare_logs_for_host()

    async def stop(self, delete: bool) -> None:
        try:
            await super().stop(delete=delete)
        finally:
            self._lock_confirmed = False
            if self._lock_temp_dir is not None:
                self._lock_temp_dir.cleanup()
                self._lock_temp_dir = None
