"""Single-container Docker host isolation, enforced before sandbox execution.

Reuse the creation/mount/privilege validation of the strict egress-lock backend,
then attach a fresh public bridge only after installing a kernel destination
filter. The sandbox's ordinary root lacks NET_ADMIN and NET_RAW. Only the trusted
controller briefly uses privileged exec to install/inspect the matched nft tool.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harbor.constants import MAIN_SERVICE_NAME  # ty: ignore[unresolved-import]
from harbor.environments.docker.docker import DockerEnvironment  # ty: ignore[unresolved-import]
from harbor.models.task.config import NetworkMode  # ty: ignore[unresolved-import]

from evallab.harbor_docker import LockedDockerEnvironment
from evallab.host_isolation import desktop_dns_addresses, nft_objects, nft_rules

ISOLATION_PORT_ENV = "EVALLAB_CLAUDE_GATEWAY_ISOLATION_PORT"
ISOLATION_RECORD = "host-isolation.json"
_TOOL_BASE = "alpine:3.23.4@sha256:5b10f432ef3da1b8d4c7eb6c487f2f5a8f096bc91145e68878dd4a5019afde11"
_TOOL_TARGET = "/opt/evallab-host-isolation"


class HostIsolatedDockerEnvironment(LockedDockerEnvironment):
    """Native Harbor Docker with a mandatory controller/private-network boundary.

    Public internet is retained when the source baseline is public. Non-public
    allowlists and task Compose are refused rather than silently weakened. The
    explicit egress_lock backend remains the stronger all-network deny option.
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
        self._tool_temp: tempfile.TemporaryDirectory[str] | None = None
        self._tool_image_digest: str | None = None
        self._network_id: str | None = None
        self._network_name = f"evallab-isolation-{uuid.uuid4().hex}"
        self._nft_command: list[str] = []
        if "egress_lock" in kwargs:
            raise ValueError("use LockedDockerEnvironment for explicit egress_lock")
        super().__init__(*args, egress_lock=True, **kwargs)

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
                self._bridge_addresses = desktop_dns_addresses(output)
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

    def _write_lock_record(self, *, applied: bool, error: BaseException | None = None) -> None:
        record = {
            "schema_version": 1,
            "recorded_at": datetime.now(UTC).isoformat(),
            "applied": applied and self._isolation_confirmed,
            "lane": self._isolation_lane,
            "container_id": self._container_id,
            "mechanism": "creation-time none; immutable NET_ADMIN/NET_RAW drops; nft inet output",
            "bridge_port": self._bridge_port,
            "bridge_addresses": self._bridge_addresses,
            "blocked_host_addresses": self._host_addresses,
            "rules": self._rules,
            "tool_image_digest": self._tool_image_digest,
            "startup_evidence": self._native_evidence,
            "network_id": self._network_id,
            "error_type": type(error).__name__ if error else None,
        }
        self.trial_paths.trial_dir.mkdir(parents=True, exist_ok=True)
        (self.trial_paths.trial_dir / ISOLATION_RECORD).write_text(
            json.dumps(record, indent=2) + "\n"
        )

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
            rule_count = 3 + len(self._host_addresses) + len(self._bridge_addresses)
            installed = nft_objects(
                json.loads(await self._nft("--echo", "--json", "--file", "-", rules=self._rules)),
                rule_count,
            )
            listing = await self._nft("--json", "list", "table", "inet", "evallab_host_isolation")
            if nft_objects(json.loads(listing), rule_count) != installed:
                raise RuntimeError("host-isolation kernel rules were not confirmed")
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
                    != listing
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
