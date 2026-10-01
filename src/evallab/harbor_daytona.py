"""Daytona transport for the existing GLM proxy, without changing the agent loop.

Harbor 0.21 stages extra Compose files but not their host bind mounts. Keep the
provider key on the sandbox VM, mounted only into the trusted proxy container,
and recover its accounting before Harbor deletes the VM. The task container
has only the internal proxy network, never the VM filesystem or Docker socket.

``BoundedDaytonaEnvironment`` also offers a post-setup egress lock (HAR-122,
mandatory for MiMo runs since HAR-140): Daytona's runner-side firewall, which
a root agent cannot undo.
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
from harbor.environments.daytona.environment import (  # ty: ignore[unresolved-import]
    DaytonaEnvironment,
    _DaytonaDinD,
)
from harbor.models.task.config import NetworkMode  # ty: ignore[unresolved-import]

from evallab.execution_contracts import (
    ZAI_OPENAPI_PROXY_HOST,
    ZAI_OPENAPI_PROXY_SCRIPT_ENV,
    ZAI_OPENAPI_PROXY_USAGE_FILE_ENV,
    ZAI_OPENAPI_SECRET_FILE_ENV,
)

_REMOTE_ROOT = "/run/evallab-zai-openapi"
_PROXY_UID = 65532
#: Written into the trial directory for every Daytona trial (HAR-140): whether
#: the egress lock was requested, whether it was applied, when, and the
#: method or error.
EGRESS_LOCK_RECORD = "egress-lock.json"


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

    ``egress_lock=true`` blocks all outbound traffic from the sandbox once
    agent setup has finished, through Daytona's runner-side firewall
    (``update_network_settings(network_block_all=True)``,
    https://www.daytona.io/docs/en/network-limits/). Since HAR-140 the lab
    passes it by default for every MiMo run on Daytona (a MiMo-family model or
    a MiMo-dataset task, including the model-free ``nop``/``oracle`` census
    runs, which lock before the verifier's first command even though the agent
    itself issues no exec). Task setup (the healthcheck) and the agent's own
    install keep the network. The rule lives outside the sandbox, so a root
    agent cannot lift it by editing ``/etc/hosts``, changing the index URL or
    dialing an IP; the verifier runs under it too. The lock is taken before
    the first command after agent setup, whichever agent runs; if Daytona
    refuses it, that command fails and the agent never runs unlocked.
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
        self._egress_record_written = False
        self._egress_lock_guard = asyncio.Lock()
        kwargs["auto_delete_interval_mins"] = 0
        kwargs["auto_stop_interval_mins"] = 5
        super().__init__(*args, **kwargs)
        if egress_lock:
            if self._compose_mode:
                raise ValueError("egress_lock supports single-container tasks only")
            if any(policy != self._network_policy for policy in self._phase_network_policies):
                # Harbor would restore the task's baseline after that phase,
                # lifting the lock.
                raise ValueError("egress_lock cannot be combined with task phase network policies")

    def _write_egress_record(
        self,
        *,
        requested: bool,
        applied: bool,
        sandbox_id: str | None = None,
        network_block_all: bool | None = None,
        mechanism: str | None = None,
        locked_at: str | None = None,
        error: str | None = None,
    ) -> None:
        """Write ``egress-lock.json`` for this trial (HAR-140).

        Every Daytona trial leaves one: whether the lock was requested,
        whether it was applied, when, and the method or error. A lock failure
        still records (``applied: false`` with the error) before the trial
        raises, so the trial ends as ``infra`` with evidence on disk.
        """
        record = {
            "schema_version": 2,
            "requested": requested,
            "applied": applied,
            "locked_at": locked_at,
            "recorded_at": datetime.now(UTC).isoformat(),
            "sandbox_id": sandbox_id,
            "network_block_all": network_block_all,
            "mechanism": mechanism,
            "error": error,
        }
        (self.trial_paths.trial_dir / EGRESS_LOCK_RECORD).write_text(
            json.dumps(record, indent=2) + "\n"
        )
        self._egress_record_written = True

    async def _create_sandbox(self, params: Any, daytona: Any = None) -> None:
        params.ttl_minutes = self._trial_ttl_minutes
        # A retry cannot create multiple unnamed resources for the same trial.
        identity = f"{self.session_id}:{self.environment_name}"
        params.name = "evallab-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        await super()._create_sandbox(params=params, daytona=daytona)
        if not self._egress_lock:
            try:
                sandbox = self._sandbox
                self._write_egress_record(
                    requested=False,
                    applied=False,
                    sandbox_id=getattr(sandbox, "id", None),
                    network_block_all=getattr(sandbox, "network_block_all", None),
                )
            except Exception as error:
                self.logger.warning("could not write egress-lock.json: %s", error)

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
        if self._egress_lock_due and not self._egress_locked:
            # The verifier's first command takes the lock for agents (such as
            # nop) that issue no exec of their own, so a locked nop trial is
            # locked before grading and still writes egress-lock.json.
            await self._lock_egress()
        if not self._egress_lock and not self._egress_record_written:
            try:
                sandbox = self._sandbox
                self._write_egress_record(
                    requested=False,
                    applied=False,
                    sandbox_id=getattr(sandbox, "id", None),
                    network_block_all=getattr(sandbox, "network_block_all", None),
                )
            except Exception as error:
                self.logger.warning("could not write egress-lock.json: %s", error)
        return await super().exec(*args, **kwargs)

    async def _lock_egress(self) -> None:
        async with self._egress_lock_guard:
            if self._egress_locked:
                return
            sandbox = self._sandbox
            try:
                if sandbox is None:
                    raise RuntimeError("egress_lock: no sandbox to lock")
                await sandbox.update_network_settings(network_block_all=True)
                if sandbox.network_block_all is not True:
                    raise RuntimeError("egress_lock: Daytona did not confirm network_block_all")
            except Exception as error:
                with contextlib.suppress(Exception):
                    self._write_egress_record(
                        requested=True,
                        applied=False,
                        sandbox_id=getattr(sandbox, "id", None),
                        network_block_all=getattr(sandbox, "network_block_all", None),
                        error=f"{type(error).__name__}: {error}",
                    )
                raise
            self._egress_locked = True
            self._write_egress_record(
                requested=True,
                applied=True,
                locked_at=datetime.now(UTC).isoformat(),
                sandbox_id=sandbox.id,
                network_block_all=sandbox.network_block_all,
                mechanism="daytona update_network_settings(network_block_all=True)",
            )
            self.logger.info("egress locked after agent setup: sandbox %s", sandbox.id)

    async def _apply_network_policy(self, network_policy: Any) -> None:
        if self._egress_locked:
            raise RuntimeError("egress_lock: refusing to change the network after the lock")
        await super()._apply_network_policy(network_policy)

    async def stop(self, delete: bool) -> None:
        if not self._egress_record_written:
            try:
                sandbox = self._sandbox
                if self._egress_lock and not self._egress_locked:
                    self._write_egress_record(
                        requested=True,
                        applied=False,
                        sandbox_id=getattr(sandbox, "id", None),
                        network_block_all=getattr(sandbox, "network_block_all", None),
                        error="trial ended before the egress lock was applied",
                    )
                elif not self._egress_lock:
                    self._write_egress_record(
                        requested=False,
                        applied=False,
                        sandbox_id=getattr(sandbox, "id", None),
                        network_block_all=getattr(sandbox, "network_block_all", None),
                    )
            except Exception as error:
                self.logger.warning("could not write egress-lock.json: %s", error)
        await super().stop(delete=True)


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
