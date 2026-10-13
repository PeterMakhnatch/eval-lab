"""Single-container Docker host isolation, enforced before sandbox execution.

Reuse the creation/mount/privilege validation of the strict egress-lock backend,
then attach a fresh public bridge only after installing a kernel destination
filter. The sandbox's ordinary root lacks NET_ADMIN and NET_RAW. Only the trusted
controller briefly uses privileged exec to install/inspect the matched nft tool.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harbor.constants import MAIN_SERVICE_NAME  # ty: ignore[unresolved-import]
from harbor.environments.capabilities import (  # ty: ignore[unresolved-import]
    EnvironmentCapabilities,
)
from harbor.environments.docker.docker import DockerEnvironment  # ty: ignore[unresolved-import]
from harbor.models.task.config import NetworkMode, NetworkPolicy  # ty: ignore[unresolved-import]

from evallab.harbor_docker import LockedDockerEnvironment
from evallab.host_isolation import desktop_dns_addresses, nft_objects, nft_rules

ISOLATION_PORT_ENV = "EVALLAB_CLAUDE_GATEWAY_ISOLATION_PORT"
ISOLATION_RECORD = "host-isolation.json"
ISOLATION_RECORD_PREFIX = "host-isolation"
_TOOL_BASE = "alpine:3.23.4@sha256:5b10f432ef3da1b8d4c7eb6c487f2f5a8f096bc91145e68878dd4a5019afde11"
_TOOL_TARGET = "/opt/evallab-host-isolation"
_PHASE_SWITCH_MODES = frozenset({NetworkMode.PUBLIC, NetworkMode.NO_NETWORK})


class HostIsolatedDockerEnvironment(LockedDockerEnvironment):
    """Native Harbor Docker with a mandatory controller/private-network boundary.

    Public internet is retained when the source baseline is public. Non-public
    allowlists and task Compose are refused rather than silently weakened. The
    explicit egress_lock backend remains the stronger all-network deny option.

    The single permitted phase switch is agent ``no-network`` ↔ setup/verifier
    ``public`` (the mimo-clean-v5 contract): the nft destination boundary is
    installed before the first public attachment and stays in force across
    every disconnect/reconnect, and the no-network phase keeps no attachment
    at all. Any allowlist or other phase mode is refused at construction and
    at switch time.
    """

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

    def validate_network_policy_support(
        self, network_policy: NetworkPolicy | None = None
    ) -> None:
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

    @property
    def _docker_compose_paths(self) -> list[Path]:
        paths = super()._docker_compose_paths
        path = paths[-1].with_name("host-isolation.json")
        path.write_text(
            json.dumps(
                {
                    "services": {
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
                }
            )
        )
        return [*paths, path]

    async def _validate_resolved_compose(self) -> dict[str, Any]:
        main = await super()._validate_resolved_compose()
        if (
            main.get("entrypoint") != ["/bin/sh", "-c", "exec sleep infinity"]
            or main.get("command") != []
            or str(main.get("user")) != "0"
            or main.get("healthcheck", {}).get("disable") is not True
            or not {"NET_ADMIN", "NET_RAW"}.issubset(main.get("cap_drop", []))
        ):
            raise RuntimeError("host-isolation trusted creation settings are unconfirmed")
        return main

    async def _confirm_lock(self) -> None:
        await super()._confirm_lock()
        assert self._container_id is not None
        inspected = await self._daemon_json("container", "inspect", self._container_id)
        container = inspected[0]
        config = container.get("Config", {})
        drops = {
            cap.removeprefix("CAP_")
            for cap in (container.get("HostConfig", {}).get("CapDrop") or [])
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

    async def _nft(self, *arguments: str, rules: str | None = None) -> str:
        return await self._daemon_output(
            "exec",
            "--privileged",
            "--user",
            "0",
            "-i",
            self._container_id or "",
            *self._nft_command,
            *arguments,
            stdin=rules,
        )

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
                char if char.isalnum() or char in "-._" else "_"
                for char in self.session_id
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

    async def _install_and_verify_rules(self) -> str:
        """Install the destination boundary and return the confirmed kernel listing."""
        rule_count = 3 + len(self._host_addresses) + len(self._bridge_addresses)
        installed = nft_objects(
            json.loads(await self._nft("--echo", "--json", "--file", "-", rules=self._rules)),
            rule_count,
        )
        listing = await self._nft("--json", "list", "table", "inet", "evallab_host_isolation")
        if nft_objects(json.loads(listing), rule_count) != installed:
            raise RuntimeError("host-isolation kernel rules were not confirmed")
        return listing

    async def _container_networks(self) -> dict[str, Any]:
        assert self._container_id is not None
        inspected = await self._daemon_json("container", "inspect", self._container_id)
        networks = inspected[0].get("NetworkSettings", {}).get("Networks")
        if not isinstance(networks, dict):
            raise RuntimeError("host-isolation container network evidence is missing")
        return networks

    def _bridge_attached(self, networks: dict[str, Any]) -> bool:
        return self._network_id is not None and any(
            isinstance(endpoint, dict) and endpoint.get("NetworkID") == self._network_id
            for endpoint in networks.values()
        )

    async def _sandbox_interface_states(self) -> dict[str, str]:
        """Kernel operstate per sandbox interface, read through an unprivileged exec.

        A detached netns still lists kernel pseudo-devices (tunl0, sit0, ...);
        only operstate `up` marks a usable interface. The sandbox lacks
        NET_ADMIN, so it cannot bring an interface up; the daemon-side
        attachment evidence above is the primary proof and this census
        corroborates it from inside.
        """
        assert self._container_id is not None
        output = await self._daemon_output(
            "exec",
            self._container_id,
            "sh",
            "-c",
            "for i in /sys/class/net/*; do "
            "printf '%s %s\\n' \"${i##*/}\" "
            "\"$(cat \"$i\"/operstate 2>/dev/null || echo unknown)\"; done",
        )
        states: dict[str, str] = {}
        for line in output.splitlines():
            name, _, state = line.partition(" ")
            if name.strip():
                states[name.strip()] = state.strip()
        return states

    async def _confirm_no_network_state(self) -> dict[str, Any]:
        """Prove the sandbox has no attachment and no usable interface but lo."""
        networks = await self._container_networks()
        if self._bridge_attached(networks):
            raise RuntimeError("host-isolation no-network phase keeps a public attachment")
        await self._validate_network_attachments(networks)
        states = await self._sandbox_interface_states()
        up = sorted(name for name, state in states.items() if name != "lo" and state == "up")
        if up:
            raise RuntimeError(
                "host-isolation no-network phase keeps unexpected interfaces up: "
                + json.dumps(up)
            )
        return {
            "networks": sorted(networks),
            "interfaces": sorted(states),
            "interface_states": states,
        }

    async def _confirm_public_state(self) -> dict[str, Any]:
        """Prove the bridge is attached and the boundary survived the attach."""
        networks = await self._container_networks()
        if not self._bridge_attached(networks):
            raise RuntimeError("host-isolation public phase lacks its bridge attachment")
        listing = await self._nft("--json", "list", "table", "inet", "evallab_host_isolation")
        if listing != self._rules_listing:
            raise RuntimeError("host-isolation rules changed during network attachment")
        states = await self._sandbox_interface_states()
        return {
            "networks": sorted(networks),
            "interfaces": sorted(states),
            "interface_states": states,
            "rules_confirmed": True,
        }

    async def _ensure_rules_before_reconnect(self) -> None:
        """Reinstall-and-confirm the boundary when it did not survive a detach."""
        listing = await self._nft("--json", "list", "table", "inet", "evallab_host_isolation")
        if listing == self._rules_listing:
            return
        self._rules_listing = await self._install_and_verify_rules()

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
        if mode == NetworkMode.NO_NETWORK:
            networks = await self._container_networks()
            if self._bridge_attached(networks):
                assert self._network_id is not None
                await self._daemon_output(
                    "network", "disconnect", self._network_id, self._container_id
                )
            evidence = await self._confirm_no_network_state()
        else:
            if self._network_id is None:
                self._network_id = (
                    await self._daemon_output("network", "create", self._network_name)
                ).strip()
            # The filter lives in the container netns; reinstall-and-confirm
            # before the sandbox ever has a public attachment again.
            await self._ensure_rules_before_reconnect()
            if not self._bridge_attached(await self._container_networks()):
                assert self._network_id is not None
                await self._daemon_output(
                    "network", "connect", self._network_id, self._container_id
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
            await super().start(force_build=force_build)
            # The inherited inspect confirms creation-time none, safe mounts and
            # default capabilities before any copy/exec can take place.
            assert self._container_id is not None and self._tool_temp is not None
            await self._daemon_output(
                "cp", self._tool_temp.name, f"{self._container_id}:{_TOOL_TARGET}"
            )
            from evallab.host_isolation import controller_addresses

            self._host_addresses = controller_addresses()
            self._rules = nft_rules(self._host_addresses, self._bridge_addresses, self._bridge_port)
            self._rules_listing = await self._install_and_verify_rules()
            if self.network_policy.network_mode == NetworkMode.PUBLIC:
                if self._network_id is None:
                    self._network_id = (
                        await self._daemon_output("network", "create", self._network_name)
                    ).strip()
                await self._daemon_output("network", "disconnect", "none", self._container_id)
                await self._daemon_output(
                    "network", "connect", self._network_id, self._container_id
                )
                # Attachment must preserve the same kernel namespace/rules.
                if (
                    await self._nft("--json", "list", "table", "inet", "evallab_host_isolation")
                    != self._rules_listing
                ):
                    raise RuntimeError("host-isolation rules changed during network attachment")
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
