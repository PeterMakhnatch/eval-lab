"""Daytona transport for the existing GLM proxy, without changing the agent loop.

Harbor 0.21 stages extra Compose files but not their host bind mounts. Keep the
provider key on the sandbox VM, mounted only into the trusted proxy container,
and recover its accounting before Harbor deletes the VM. The task container
has only the internal proxy network, never the VM filesystem or Docker socket.

``BoundedDaytonaEnvironment`` also offers an opt-in post-setup egress lock
(HAR-122): Daytona's runner-side firewall, which a root agent cannot undo.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from daytona import Resources
from harbor.environments.daytona.environment import (  # ty: ignore[unresolved-import]
    DaytonaEnvironment,
    _DaytonaDinD,
)
from harbor.models.task.config import NetworkMode  # ty: ignore[unresolved-import]

from evallab.daytona_guard import AdmissionRefused, DaytonaGuard, GuardUnavailable
from evallab.execution_contracts import (
    ZAI_OPENAPI_PROXY_HOST,
    ZAI_OPENAPI_PROXY_SCRIPT_ENV,
    ZAI_OPENAPI_PROXY_USAGE_FILE_ENV,
    ZAI_OPENAPI_SECRET_FILE_ENV,
)

_REMOTE_ROOT = "/run/evallab-zai-openapi"
_PROXY_UID = 65532
#: Written into the trial directory when the egress lock is applied.
EGRESS_LOCK_RECORD = "egress-lock.json"
DAYTONA_USAGE_RECORD = "daytona-usage.json"
DAYTONA_MONITOR_SECONDS = 15


def render_proxy_overlay(source: Path) -> dict[str, Any]:
    """Use Compose's own interpolation without starting a local container."""
    rendered = subprocess.run(
        [
            "docker", "compose", "-f", str(source), "config",
            "--no-consistency", "--no-path-resolution", "--format", "json",
        ],
        check=True, capture_output=True, text=True, timeout=15,
    )
    config = json.loads(rendered.stdout)
    config.pop("name", None)
    source_services = yaml.safe_load(source.read_text(encoding="utf-8"))["services"]
    # Compose normalization emits null command/entrypoint even when omitted.
    # Re-applying those nulls would erase Harbor's keep-alive command.
    for name, service in config["services"].items():
        for field in ("command", "entrypoint"):
            if field not in source_services[name]:
                service.pop(field, None)
    if set(config["services"]) != {"main", ZAI_OPENAPI_PROXY_HOST}:
        raise ValueError("GLM Daytona transport requires the dedicated two-service proxy overlay")
    proxy = config["services"][ZAI_OPENAPI_PROXY_HOST]
    proxy["user"] = f"{_PROXY_UID}:{_PROXY_UID}"
    targets = {
        "/opt/evallab/zai_openapi_secret_proxy.py": f"{_REMOTE_ROOT}/proxy.py",
        "/run/secrets/evallab_zai_openapi_api_key": f"{_REMOTE_ROOT}/key",
        "/run/evallab": f"{_REMOTE_ROOT}/usage",
    }
    if {mount["target"] for mount in proxy["volumes"]} != set(targets):
        raise ValueError("Unexpected proxy mount contract")
    for mount in proxy["volumes"]:
        mount["source"] = targets[mount["target"]]
    # Trusted harness installation initially needs package-index access. Harbor's
    # agent-phase transition disconnects this public network before model actions.
    config["services"]["main"]["networks"] = {
        "default": None, "workbench-internal": None,
    }
    config["networks"]["workbench-internal"]["internal"] = True
    return config


class _ProxyDaytonaDinD(_DaytonaDinD):
    async def _stage_extra_compose_files(self) -> None:
        env = self._env
        if len(env.extra_docker_compose_paths) != 1:
            raise ValueError("GLM Daytona transport requires exactly one proxy overlay")
        source = env.extra_docker_compose_paths[0]
        config = render_proxy_overlay(source)
        env._proxy_networks = {
            name: config["networks"][name]["name"]
            for name in ("default", "workbench-internal")
        }
        result = await self._vm_exec(
            f"mkdir -p {_REMOTE_ROOT}/usage && chmod 700 {_REMOTE_ROOT} && "
            f"chown {_PROXY_UID}:{_PROXY_UID} {_REMOTE_ROOT}/usage && "
            f"chmod 700 {_REMOTE_ROOT}/usage", timeout_sec=15,
        )
        if result.return_code:
            raise RuntimeError("Could not initialize private remote proxy directories")
        await env._sdk_upload_file(os.environ[ZAI_OPENAPI_PROXY_SCRIPT_ENV], f"{_REMOTE_ROOT}/proxy.py")
        await env._sdk_upload_file(os.environ[ZAI_OPENAPI_SECRET_FILE_ENV], f"{_REMOTE_ROOT}/key")
        result = await self._vm_exec(
            f"chown {_PROXY_UID}:{_PROXY_UID} {_REMOTE_ROOT}/key && "
            f"chmod 400 {_REMOTE_ROOT}/key && chmod 444 {_REMOTE_ROOT}/proxy.py",
            timeout_sec=15,
        )
        if result.return_code:
            raise RuntimeError("Could not protect the remote provider credential")
        with tempfile.TemporaryDirectory(prefix="evallab-daytona-compose.") as directory:
            resolved = Path(directory) / "proxy.json"
            resolved.write_text(json.dumps(config), encoding="utf-8")
            resolved.chmod(0o600)
            await env._sdk_upload_file(resolved, self._extra_compose_target_paths()[0])
        env._proxy_staged = True


class BoundedDaytonaEnvironment(DaytonaEnvironment):
    """Native Daytona task handling with a provider-side destruction deadline.

    ``egress_lock=true`` (off by default) blocks all outbound traffic from the
    sandbox once agent setup has finished, through Daytona's runner-side
    firewall (``update_network_settings(network_block_all=True)``,
    https://www.daytona.io/docs/en/network-limits/). Task setup (the
    healthcheck) and the agent's own install keep the network. The rule lives
    outside the sandbox, so a root agent cannot lift it by editing
    ``/etc/hosts``, changing the index URL or dialing an IP; the verifier runs
    under it too. The lock is taken before the first command after agent
    setup, whichever agent runs; if Daytona refuses it, that command fails and
    the agent never runs unlocked.
    """

    def __init__(
        self, *args: Any, ttl_minutes: int, egress_lock: bool = False, **kwargs: Any
    ) -> None:
        if (
            isinstance(ttl_minutes, bool)
            or not isinstance(ttl_minutes, int)
            or not 1 <= ttl_minutes <= 1440
        ):
            raise ValueError("An explicit positive Daytona lifetime is required")
        if not isinstance(egress_lock, bool):
            raise ValueError("egress_lock must be true or false")
        self._trial_ttl_minutes = ttl_minutes
        self._egress_lock = egress_lock
        self._egress_scope_depth = 0
        self._egress_lock_due = False
        self._egress_locked = False
        self._egress_lock_guard = asyncio.Lock()
        self._daytona_guard = DaytonaGuard()
        self._daytona_usage: dict[str, Any] = {"schema_version": 1}
        self._daytona_monitor: asyncio.Task[None] | None = None
        kwargs["auto_delete_interval_mins"] = 0
        kwargs["auto_stop_interval_mins"] = 5
        super().__init__(*args, **kwargs)
        identity = f"{self.session_id}:{self.environment_name}"
        self._daytona_sandbox_name = "evallab-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        self._daytona_reservation_seconds = (
            self._trial_ttl_minutes * 60 + int(self.task_env_config.build_timeout_sec) + 60
        )
        if egress_lock:
            if self._compose_mode:
                raise ValueError("egress_lock supports single-container tasks only")
            if any(policy != self._network_policy for policy in self._phase_network_policies):
                # Harbor would restore the task's baseline after that phase,
                # lifting the lock.
                raise ValueError("egress_lock cannot be combined with task phase network policies")

    def _sandbox_resources(self) -> Resources | None:
        resources = super()._sandbox_resources()
        if resources is not None:
            # Provider allocations are whole GiB; never floor a positive task
            # declaration to zero or silently provision less than requested.
            if (memory_mb := self._effective_memory_mb) is not None:
                resources.memory = (memory_mb + 1023) // 1024
            if (storage_mb := self._effective_storage_mb) is not None:
                resources.disk = (storage_mb + 1023) // 1024
        return resources

    async def _usage_resources(self, resources: Any) -> dict[str, float]:
        allocation = {
            "cpu": resources.cpu,
            "memory_gib": resources.memory,
            "disk_gib": resources.disk,
            "gpu": resources.gpu if resources.gpu is not None else 0,
        }
        if any(allocation[key] is None for key in ("cpu", "memory_gib", "disk_gib")):
            snapshot = await asyncio.to_thread(self._daytona_guard.observe)
            caps = snapshot["per_sandbox_limits"]
            # Omitted image resources are server defaults, not SDK defaults.
            # Reserve the verified maximum instead of guessing that allocation.
            for key in ("cpu", "memory_gib", "disk_gib"):
                if allocation[key] is None:
                    allocation[key] = caps[key]
        return allocation

    def _write_daytona_usage(self) -> None:
        path = self.trial_paths.trial_dir / DAYTONA_USAGE_RECORD
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self._daytona_usage, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)

    async def start(self, force_build: bool) -> None:
        # Hold cross-lane capacity before Harbor can build a paid snapshot.
        # The final check uses the resolved snapshot's actual allocation.
        try:
            resources = await self._usage_resources(self._sandbox_resources() or Resources())
            preview = await asyncio.to_thread(
                self._daytona_guard.reserve,
                self._daytona_sandbox_name,
                resources,
                ttl_seconds=self._daytona_reservation_seconds,
            )
        except AdmissionRefused as error:
            self._daytona_usage["admission"] = error.snapshot
            self._write_daytona_usage()
            raise
        except GuardUnavailable as error:
            self._daytona_usage["monitor_error"] = str(error)
            self._write_daytona_usage()
            raise
        self._daytona_usage["admission"] = preview
        self._write_daytona_usage()
        try:
            await super().start(force_build)
        except Exception as error:
            if isinstance(error, GuardUnavailable):
                self._daytona_usage["monitor_error"] = str(error)
                self._write_daytona_usage()
            await self._capture_daytona_disappearance(error)
            raise

    async def _create_sandbox(self, params: Any, daytona: Any = None) -> None:
        params.ttl_minutes = self._trial_ttl_minutes
        # A retry cannot create multiple unnamed resources for the same trial.
        params.name = self._daytona_sandbox_name
        snapshot_name = getattr(params, "snapshot", None)
        if hasattr(params, "snapshot"):
            if snapshot_name is None:
                snapshot_name = await asyncio.to_thread(self._daytona_guard.default_snapshot)
            resources, sandbox_class = await asyncio.to_thread(
                self._daytona_guard.snapshot_resources, snapshot_name
            )
        else:
            resources = await self._usage_resources(
                getattr(params, "resources", None) or Resources()
            )
            sandbox_class = "container"
        try:
            admission = await asyncio.to_thread(
                self._daytona_guard.reserve,
                params.name,
                resources,
                ttl_seconds=self._daytona_reservation_seconds,
                sandbox_class=sandbox_class,
            )
        except AdmissionRefused as error:
            self._daytona_usage["admission"] = error.snapshot
            self._write_daytona_usage()
            raise
        self._daytona_usage["admission"] = admission
        self._write_daytona_usage()
        # Keep the reservation on an ambiguous creation failure. A process
        # losing its response does not establish that the provider created nothing.
        await super()._create_sandbox(params=params, daytona=daytona)
        self._daytona_monitor = asyncio.create_task(self._monitor_daytona_usage())

    async def _capture_daytona_disappearance(
        self, error: Exception | None = None, *, current_usage: dict[str, Any] | None = None
    ) -> None:
        sandbox = self._sandbox
        if sandbox is None or self._daytona_usage.get("disappearance"):
            return
        try:
            missing = await asyncio.to_thread(self._daytona_guard.sandbox_missing, sandbox.id)
        except GuardUnavailable as unavailable:
            self._daytona_usage["monitor_error"] = str(unavailable)
            self._write_daytona_usage()
            return
        if not missing:
            return
        previous = self._daytona_usage.get("last_observation")
        current = current_usage
        read_error = None
        if current is None:
            try:
                current = await asyncio.to_thread(self._daytona_guard.observe)
            except GuardUnavailable as unavailable:
                read_error = str(unavailable)
        now = datetime.now(UTC)
        recent_pressure = False
        if isinstance(previous, dict) and previous.get("at_or_near_limit"):
            observed_at = datetime.fromisoformat(previous["observed_at"])
            age = (now - observed_at).total_seconds()
            recent_pressure = 0 <= age <= DAYTONA_MONITOR_SECONDS * 2
        current_pressure = bool(current and current.get("at_or_near_limit"))
        pressure = current_pressure or recent_pressure
        self._daytona_usage["disappearance"] = {
            "observed_at": now.isoformat(),
            "sandbox_id": sandbox.id,
            "sandbox_name": self._daytona_sandbox_name,
            "cause": "usage_limit_pressure" if pressure else "unknown",
            "evidence_strength": (
                "observed_capacity_pressure_not_provider_event"
                if pressure
                else "no_capacity_evidence"
            ),
            "pressure_source": (
                "current_usage" if current_pressure else "last_observation" if recent_pressure else None
            ),
            "current_usage": current,
            "last_observation": previous,
            "error_type": type(error).__name__ if error is not None else None,
            "read_error": read_error,
        }
        self._write_daytona_usage()

    async def _monitor_daytona_usage(self) -> None:
        while True:
            try:
                snapshot = await asyncio.to_thread(self._daytona_guard.observe)
                sandbox = self._sandbox
                visible = {
                    row["id"] for row in snapshot["inventory"]
                }
                if sandbox is not None and sandbox.id not in visible:
                    await self._capture_daytona_disappearance(current_usage=snapshot)
                    if self._daytona_usage.get("disappearance"):
                        return
                self._daytona_usage["last_observation"] = snapshot
                self._daytona_usage.pop("monitor_error", None)
                self._write_daytona_usage()
            except GuardUnavailable as unavailable:
                self._daytona_usage["monitor_error"] = str(unavailable)
                self._write_daytona_usage()
            await asyncio.sleep(DAYTONA_MONITOR_SECONDS)

    async def _sandbox_exec(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return await super()._sandbox_exec(*args, **kwargs)
        except Exception as error:
            await self._capture_daytona_disappearance(error)
            raise

    @contextlib.contextmanager
    def scoped_exec_env(self, env: dict[str, str]) -> Iterator[None]:
        # Harbor 0.21 runs agent.setup() inside the first top-level scope
        # (trial.py _setup_agent); leaving it marks setup as finished.
        self._egress_scope_depth += 1
        try:
            with super().scoped_exec_env(env):
                yield
        finally:
            self._egress_scope_depth -= 1
            if self._egress_lock and self._egress_scope_depth == 0:
                self._egress_lock_due = True

    async def exec(self, *args: Any, **kwargs: Any) -> Any:
        try:
            if self._egress_lock_due and not self._egress_locked:
                await self._lock_egress()
            return await super().exec(*args, **kwargs)
        except Exception as error:
            await self._capture_daytona_disappearance(error)
            raise

    async def _lock_egress(self) -> None:
        async with self._egress_lock_guard:
            if self._egress_locked:
                return
            sandbox = self._sandbox
            if sandbox is None:
                raise RuntimeError("egress_lock: no sandbox to lock")
            await sandbox.update_network_settings(network_block_all=True)
            if sandbox.network_block_all is not True:
                raise RuntimeError("egress_lock: Daytona did not confirm network_block_all")
            self._egress_locked = True
            record = {
                "schema_version": 1,
                "locked_at": datetime.now(UTC).isoformat(),
                "sandbox_id": sandbox.id,
                "network_block_all": sandbox.network_block_all,
                "mechanism": "daytona update_network_settings(network_block_all=True)",
            }
            (self.trial_paths.trial_dir / EGRESS_LOCK_RECORD).write_text(
                json.dumps(record, indent=2) + "\n"
            )
            self.logger.info("egress locked after agent setup: sandbox %s", sandbox.id)

    async def _apply_network_policy(self, network_policy: Any) -> None:
        if self._egress_locked:
            raise RuntimeError("egress_lock: refusing to change the network after the lock")
        await super()._apply_network_policy(network_policy)

    async def stop(self, delete: bool) -> None:
        monitor = self._daytona_monitor
        if monitor is not None:
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor
            self._daytona_monitor = None
        sandbox_id = self._sandbox.id if self._sandbox is not None else None
        await super().stop(delete=True)
        if sandbox_id is not None and self._daytona_sandbox_name is not None:
            try:
                gone = await asyncio.to_thread(self._daytona_guard.sandbox_missing, sandbox_id)
                if gone:
                    await asyncio.to_thread(self._daytona_guard.release, self._daytona_sandbox_name)
            except GuardUnavailable as unavailable:
                self._daytona_usage["monitor_error"] = str(unavailable)
                self._write_daytona_usage()


class SecretSafeDaytonaEnvironment(BoundedDaytonaEnvironment):
    """Bounded Daytona VM with the same metered proxy used by local GLM trials."""

    def __init__(self, *args: Any, ttl_minutes: int, **kwargs: Any) -> None:
        if kwargs.get("egress_lock"):
            raise ValueError("egress_lock would cut the in-sandbox model proxy off its provider")
        self._proxy_staged = False
        self._proxy_networks: dict[str, str] = {}
        super().__init__(*args, ttl_minutes=ttl_minutes, **kwargs)
        if self._compose_mode:
            if self._environment_docker_compose_path.exists():
                raise ValueError("GLM Daytona proxy transport currently requires a single-container task")
            self._strategy = _ProxyDaytonaDinD(self)

    @property
    def capabilities(self) -> Any:
        capabilities = super().capabilities
        if self._compose_mode:
            return capabilities.model_copy(update={
                "network_allowlist": True,
                "network_allowlist_hostnames": True,
                "dynamic_network_policy": True,
            })
        return capabilities

    def validate_network_policy_support(self, network_policy: Any = None) -> None:
        super().validate_network_policy_support(network_policy)
        if self._compose_mode:
            policy = network_policy or self.network_policy
            if policy.network_mode == NetworkMode.ALLOWLIST and set(policy.allowed_hosts) != {ZAI_OPENAPI_PROXY_HOST}:
                raise ValueError("GLM Daytona allowlists may contain only the internal model proxy")

    async def _apply_network_policy(self, network_policy: Any) -> None:
        if not self._compose_mode:
            return await super()._apply_network_policy(network_policy)
        self.validate_network_policy_support(network_policy)
        strategy = self._strategy
        if not isinstance(strategy, _ProxyDaytonaDinD):
            raise RuntimeError("The GLM proxy requires the scoped Daytona compose transport")
        result = await strategy._compose_exec(["ps", "-q", "main"], timeout_sec=15)
        container_id = (result.stdout or "").strip()
        if result.return_code or not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            raise RuntimeError("Could not identify the task container for network isolation")
        inspected = await strategy._vm_exec(
            "docker inspect --format '{{json .NetworkSettings.Networks}}' " + container_id,
            timeout_sec=15,
        )
        if inspected.return_code or not inspected.stdout:
            raise RuntimeError("Could not inspect task container networks")
        attached = set(json.loads(inspected.stdout))
        public = self._proxy_networks["default"]
        internal = self._proxy_networks["workbench-internal"]
        mode = network_policy.network_mode
        desired = (
            {public, internal} if mode == NetworkMode.PUBLIC
            else {internal} if mode == NetworkMode.ALLOWLIST
            else set()
        )
        if attached - {public, internal}:
            raise RuntimeError("Task container has an unexpected network attachment")
        for network in sorted(attached - desired):
            changed = await strategy._vm_exec(
                f"docker network disconnect {shlex.quote(network)} {container_id}", timeout_sec=15,
            )
            if changed.return_code:
                raise RuntimeError("Failed to disconnect task network")
        for network in sorted(desired - attached):
            changed = await strategy._vm_exec(
                f"docker network connect {shlex.quote(network)} {container_id}", timeout_sec=15,
            )
            if changed.return_code:
                raise RuntimeError("Failed to connect task network")

    async def stop(self, delete: bool) -> None:
        try:
            if self._proxy_staged and self._sandbox is not None:
                # Quiesce both callers and proxy before taking the final ledger.
                strategy = self._strategy
                if not isinstance(strategy, _ProxyDaytonaDinD):
                    raise RuntimeError("The GLM proxy requires the scoped Daytona compose transport")
                stopped = await strategy._compose_exec(
                    ["stop", "-t", "5", "main", ZAI_OPENAPI_PROXY_HOST], timeout_sec=30,
                )
                if stopped.return_code:
                    raise RuntimeError("Could not quiesce the remote model proxy")
                target = Path(os.environ[ZAI_OPENAPI_PROXY_USAGE_FILE_ENV])
                try:
                    await self._sandbox.fs.download_file(
                        f"{_REMOTE_ROOT}/usage/zai-openapi-proxy-usage.json", str(target),
                    )
                    target.chmod(0o600)
                except Exception as error:
                    # The existing runner rejects missing accounting. Never invent
                    # zero usage, and never let capture failure prevent deletion.
                    self.logger.error("Could not recover GLM proxy accounting: %s", type(error).__name__)
        finally:
            await super().stop(delete=True)
