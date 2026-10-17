"""Fresh-state reference-subset grades on the lab's pinned Harbor Modal adapter.

Only this module touches native environments. Import it after local planning and
approval checks. A prepared filesystem snapshot is immutable: reference reads use
the preparation sandbox, and every grade allocates a different sandbox. Native
image preparation and every sandbox creation reserve from the same spend gate.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shlex
import tarfile
import tempfile
import time
import tomllib
import uuid
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from harbor.environments.modal import ModalEnvironment
from harbor.models.task.config import NetworkPolicy, TaskConfig
from harbor.models.task.paths import TaskPaths
from harbor.models.task.verifier_mode import resolve_verifier_environment_definition
from harbor.models.trial.paths import TrialPaths

from evallab.execution_contracts import MODAL_PATH_PIN
from evallab.harbor_modal import LockedModalEnvironment, RegradeModalEnvironment
from evallab.ref_subset import Grade, Package, _tar
from evallab.regrade import CloudSpendGate, SpendCapReached

_SNAPSHOT_TTL_SECONDS = 86_400
_MAX_LIFETIME_SECONDS = 7_200
_TRANSFER_TIMEOUT_SECONDS = 600


class _ReservedCreation(ModalEnvironment):
    """Reserve both potential native creation attempts before the provider call.

    Harbor keeps its normal retry and lock-evidence behavior. Unused attempt
    reservations remain charged estimates, not claims of provider executions.
    """

    def __init__(
        self,
        *args: Any,
        spend_gate: CloudSpendGate,
        events: list[dict[str, Any]],
        operation: str,
        snapshot: tuple[Any, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self._ref_gate = spend_gate
        self._ref_events = events
        self._ref_operation = operation
        self._ref_snapshot = snapshot
        self._ref_prepaid_creation: dict[str, Any] | None = None
        super().__init__(*args, **kwargs)

    def _reserve(self, kind: str) -> dict[str, Any]:
        event: dict[str, Any] = {"operation": self._ref_operation, "kind": kind}
        try:
            self._ref_gate.charge()
        except SpendCapReached as exc:
            event.update(status="budget_exhausted", error=str(exc))
            self._ref_events.append(event)
            raise
        event.update(status="reserved", reservation_usd=self._ref_gate.usd_per_execution)
        self._ref_events.append(event)
        return event

    async def start(self, force_build: bool = False) -> None:
        if self._ref_snapshot is None:
            # Image.from_dockerfile/from_registry may execute a paid image build
            # before the sandbox exists. Reserve before entering native start.
            self._reserve("image_preparation")
            self._reserve("sandbox_creation_attempt_1")
            self._ref_prepaid_creation = self._reserve("sandbox_creation_attempt_2")
            await super().start(force_build=force_build)
        else:
            self._image, self._app = self._ref_snapshot
            self._sandbox = await self._create_sandbox(entrypoint=["sh", "-c", "sleep infinity"])

    async def _create_sandbox(self, **kwargs: Any) -> Any:
        if self._ref_prepaid_creation is None:
            self._reserve("sandbox_creation_attempt_1")
            event = self._reserve("sandbox_creation_attempt_2")
        else:
            event = self._ref_prepaid_creation
            self._ref_prepaid_creation = None
        try:
            sandbox = await super()._create_sandbox(**kwargs)
            self._sandbox = sandbox
            event.update(status="created", sandbox_id=sandbox.object_id)
            return sandbox
        except Exception as exc:
            event.update(status="error", error=str(exc)[-2_000:])
            raise


class _AgentEnvironment(_ReservedCreation, LockedModalEnvironment):
    pass


class _VerifierEnvironment(_ReservedCreation, RegradeModalEnvironment):
    pass


class ModalSandbox:
    """Raw MiMo snapshots and reference-derived separate-verifier artifacts."""

    def __init__(
        self,
        package: Package,
        *,
        spend_gate: CloudSpendGate,
        out_dir: Path,
        memory: str = "4g",
        oracle_timeout: float = 3_600,
        grade_timeout: float = 1_800,
        build_timeout: float = 1_800,
    ) -> None:
        self.package = package
        self.gate = spend_gate
        self.nonce = uuid.uuid4().hex[:12]
        self.out_dir = out_dir / self.nonce
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.config = TaskConfig.model_validate(
            tomllib.loads((package.path / "task.toml").read_text())
        )
        self.memory_mb = self.config.environment._parse_size_to_mb(memory)
        if self.memory_mb <= 0:
            raise ValueError("Modal memory must be positive")
        self.oracle_timeout = oracle_timeout
        self.grade_timeout = grade_timeout
        self.build_timeout = build_timeout
        for name, value in (
            ("oracle_timeout", oracle_timeout),
            ("grade_timeout", grade_timeout),
            ("build_timeout", build_timeout),
        ):
            if not math.isfinite(value) or value <= 0 or value > _MAX_LIFETIME_SECONDS - 600:
                raise ValueError(f"{name} must be positive and at most 6600 seconds on Modal")
        self.runner = asyncio.Runner()
        self.events: list[dict[str, Any]] = []
        self.modes: dict[str, int] = {}
        self.dirs: set[str] = set()
        self.unchanged: dict[str, bytes] = {}
        self.preparation: Any | None = None
        self.snapshot: tuple[Any, Any] | None = None
        self.environments: list[Any] = []
        self.counter = 0

    def _environment(
        self, operation: str, *, verifier: bool, snapshot: tuple[Any, Any] | None = None
    ) -> Any:
        self.counter += 1
        paths = TrialPaths(self.out_dir / f"{self.counter:03d}-{operation.replace(':', '-')[:60]}")
        paths.mkdir()
        config = self.config.environment.model_copy(deep=True)
        directory = self.package.path / "environment"
        if verifier and self.package.separate:
            definition = resolve_verifier_environment_definition(
                self.config, TaskPaths(self.package.path)
            )
            if definition is None:
                raise ValueError("the native Harbor verifier definition is not separate")
            config, directory = definition.config, definition.directory
        if not self.package.separate:
            config = config.model_copy(update={"workdir": self.package.workdir})
            policy = NetworkPolicy(network_mode="no-network")
        else:
            role = self.config.verifier if verifier else self.config.agent
            policy = role.explicit_phase_policy() or config.resolve_baseline()
        if config.gpus or config.tpu:
            raise ValueError(
                "GPU/TPU Modal reference-subset resources have no approved price estimate"
            )
        cpus = min(config.cpus or 2, 2)
        cls = _VerifierEnvironment if verifier else _AgentEnvironment
        timeout = self.grade_timeout if verifier else max(self.oracle_timeout, self.build_timeout)
        kwargs: dict[str, Any] = {}
        if not verifier:
            kwargs["evallab_egress_lock"] = policy.network_mode.value != "public"
        environment = cls(
            environment_dir=directory,
            environment_name=f"evallab-ref-subset-{self.nonce}-{self.counter}",
            session_id=f"ref-subset-{self.nonce}-{self.counter}",
            trial_paths=paths,
            task_env_config=config,
            override_cpus=cpus,
            override_memory_mb=self.memory_mb,
            cpu_enforcement_policy="limit",
            memory_enforcement_policy="limit",
            network_policy=policy,
            persistent_env={"PATH": MODAL_PATH_PIN, "PIP_NO_CACHE_DIR": "1"},
            sandbox_timeout_secs=min(_MAX_LIFETIME_SECONDS, math.ceil(timeout + 600)),
            sandbox_idle_timeout_secs=600,
            enable_environment_dir_upload=False,
            spend_gate=self.gate,
            events=self.events,
            operation=operation,
            snapshot=snapshot,
            **kwargs,
        )
        self.environments.append(environment)
        self.events.append(
            {
                "kind": "native_environment",
                "session_id": environment.session_id,
                "adapter": "RegradeModalEnvironment" if verifier else "LockedModalEnvironment",
                "definition": str(directory),
                "network_policy_requested": policy.model_dump(mode="json"),
                "cpus_limit": cpus,
                "memory_limit_mb": self.memory_mb,
                "lifetime_limit_s": environment._sandbox_timeout,
            }
        )
        return environment

    async def _start(self, environment: Any) -> None:
        await asyncio.wait_for(environment.start(force_build=False), timeout=self.build_timeout)

    async def _exec(
        self,
        environment: Any,
        command: str,
        *,
        timeout: float = _TRANSFER_TIMEOUT_SECONDS,
        check: bool = True,
        **kwargs: Any,
    ) -> Any:
        event: dict[str, Any] = {
            "kind": "exec",
            "session_id": environment.session_id,
            "command": command,
            "cwd": kwargs.get("cwd"),
            "user": kwargs.get("user"),
            "timeout_s": timeout,
        }
        self.events.append(event)
        try:
            result = await asyncio.wait_for(
                environment.exec(command, timeout_sec=math.ceil(timeout), **kwargs),
                timeout=timeout + 5,
            )
            event["return_code"] = result.return_code
            output_dir = environment.trial_paths.trial_dir / "commands"
            output_dir.mkdir(exist_ok=True)
            for stream in ("stdout", "stderr"):
                path = output_dir / f"{len(self.events):04d}.{stream}.txt"
                path.write_text(getattr(result, stream) or "")
                event[f"{stream}_path"] = str(path)
            if check and result.return_code != 0:
                raise RuntimeError(
                    f"{command[:120]} failed ({result.return_code}): "
                    f"{(result.stderr or result.stdout or '')[-2_000:]}"
                )
            return result
        except Exception as exc:
            event["error"] = str(exc)[-2_000:]
            raise

    async def _stop(self, environment: Any) -> None:
        sandbox = getattr(environment, "_sandbox", None)
        if sandbox is None:
            return
        event: dict[str, Any] = {
            "kind": "stop",
            "session_id": environment.session_id,
            "sandbox_id": sandbox.object_id,
        }
        self.events.append(event)
        try:
            await asyncio.wait_for(environment.stop(delete=True), timeout=120)
            event["status"] = "native_stop_completed"
        except Exception as exc:
            event.update(status="error", error=str(exc)[-2_000:])
            raise

    async def _snapshot(self, environment: Any) -> tuple[Any, Any]:
        image = await asyncio.wait_for(
            environment._sandbox.snapshot_filesystem.aio(timeout=120, ttl=_SNAPSHOT_TTL_SECONDS),
            timeout=125,
        )
        self.events.append(
            {
                "kind": "filesystem_snapshot",
                "sandbox_id": environment._sandbox.object_id,
                "image_id": image.object_id,
                "ttl_s": _SNAPSHOT_TTL_SECONDS,
            }
        )
        return image, environment._app

    def _run(self, operation: Any) -> Any:
        try:
            return self.runner.run(operation)
        except SpendCapReached:
            raise
        except Exception as exc:
            raise RuntimeError(f"Modal ref-subset: {exc}") from exc

    def prepare(self) -> None:
        self._run(self._prepare())

    async def _prepare(self) -> None:
        environment = self._environment("prepare", verifier=self.package.separate)
        self.preparation = environment
        await self._start(environment)
        if not self.package.separate:
            await self._exec(
                environment, "mkdir -p /var/lib/mimo /tests /logs/verifier /logs/artifacts"
            )
            await environment.upload_dir(
                self.package.path / "environment" / "setup", "/var/lib/mimo"
            )
            await self._exec(environment, "bash /var/lib/mimo/setup.sh", timeout=self.build_timeout)
        await environment.upload_dir(self.package.path / "tests", "/tests")
        await self._exec(
            environment,
            "mkdir -p /logs/verifier /logs/artifacts && chmod 777 /logs/verifier /logs/artifacts "
            "&& rm -f /logs/verifier/reward.txt /logs/verifier/reward.json",
        )
        self.snapshot = await self._snapshot(environment)
        if self.package.separate:
            await self._stop(environment)
            self.preparation = None

    async def _read(
        self, environment: Any, paths: Iterable[str], *, root: str, recursive: bool
    ) -> dict[str, bytes | None]:
        wanted = list(paths)
        out: dict[str, bytes | None] = {} if recursive else dict.fromkeys(wanted)
        if not wanted:
            return out
        quoted = " ".join(shlex.quote(p) for p in wanted)
        script = (
            'for p in "$@"; do if [ -d "$p" ]; then printf "D%s\\0" "$p"; '
            'elif [ -f "$p" ]; then printf "F%s\\0" "$p"; fi; done'
        )
        listing = await self._exec(
            environment,
            f"sh -c {shlex.quote(script)} sh {quoted}",
            cwd=root,
        )
        present: list[str] = []
        for entry in (listing.stdout or "").split("\0"):
            if not entry:
                continue
            kind, path = entry[0], entry[1:]
            if kind == "D":
                if not recursive:
                    continue
                self.dirs.add(path)
            present.append(path)
        if not present:
            return out
        remote = f"/tmp/ref-subset-{uuid.uuid4().hex}.tar"
        try:
            await self._exec(
                environment,
                f"tar -C {shlex.quote(root)} -cf {remote} -- "
                + " ".join(shlex.quote(p) for p in present),
            )
            with tempfile.TemporaryDirectory(prefix="ref-subset-modal-read-") as temporary:
                local = Path(temporary) / "files.tar"
                await environment.download_file(remote, local)
                with tarfile.open(local) as archive:
                    for member in archive.getmembers():
                        if not member.isfile():
                            continue
                        handle = archive.extractfile(member)
                        if handle is not None:
                            out[member.name] = handle.read()
                            self.modes[member.name] = member.mode
        finally:
            await self._exec(environment, f"rm -f {remote}", check=False)
        return out

    def read(self, paths: Iterable[str]) -> dict[str, bytes | None]:
        return self._run(self._read_preparation(paths))

    async def _read_preparation(self, paths: Iterable[str]) -> dict[str, bytes | None]:
        if self.preparation is None:
            raise RuntimeError("raw MiMo preparation is not running")
        try:
            return await self._read(
                self.preparation, paths, root=self.package.workdir, recursive=False
            )
        finally:
            await self._stop(self.preparation)
            self.preparation = None

    def reference(self) -> tuple[dict[str, bytes | None], dict[str, bytes | None]]:
        return self._run(self._reference())

    async def _reference(self) -> tuple[dict[str, bytes | None], dict[str, bytes | None]]:
        environment = self._environment("oracle", verifier=False)
        try:
            await self._start(environment)
            roots = [path.lstrip("/") or "." for path in self.package.artifacts]
            before = await self._read(environment, roots, root="/", recursive=True)
            await self._exec(environment, "mkdir -p /solution")
            await environment.upload_dir(self.package.path / "solution", "/solution")
            await self._exec(environment, "bash /solution/solve.sh", timeout=self.oracle_timeout)
            after = await self._read(environment, roots, root="/", recursive=True)
        finally:
            await self._stop(environment)
        paths = sorted(set(before) | set(after))
        self.unchanged = {
            p: before[p] for p in paths if before.get(p) is not None and before[p] == after.get(p)
        }
        return {p: before.get(p) for p in paths}, {p: after.get(p) for p in paths}

    async def _write(self, environment: Any, files: dict[str, bytes | None]) -> None:
        root = "/" if self.package.separate else self.package.workdir
        submitted = self.unchanged | files if self.package.separate else files
        present = {
            p: (data, self.modes.get(p, 0o644)) for p, data in submitted.items() if data is not None
        }
        removed = [str(PurePosixPath(root, p)) for p, data in submitted.items() if data is None]
        if self.package.separate:
            removed += [f"/{d}" for d in sorted(self.dirs)]
        if removed:
            await self._exec(environment, "rm -rf -- " + " ".join(shlex.quote(p) for p in removed))
        if present:
            parents = sorted({str(PurePosixPath(root, p).parent) for p in present})
            await self._exec(environment, "mkdir -p " + " ".join(shlex.quote(p) for p in parents))
            remote = f"/tmp/ref-subset-{uuid.uuid4().hex}.tar"
            with tempfile.TemporaryDirectory(prefix="ref-subset-modal-write-") as temporary:
                local = Path(temporary) / "files.tar"
                raw = _tar(present)
                local.write_bytes(raw)
                self.events.append(
                    {
                        "kind": "artifact_upload",
                        "session_id": environment.session_id,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "files": sorted(present),
                        "file_evidence": [
                            {"path": p, "sha256": hashlib.sha256(data).hexdigest(), "mode": mode}
                            for p, (data, mode) in sorted(present.items())
                        ],
                    }
                )
                try:
                    await environment.upload_file(local, remote)
                    await self._exec(environment, f"tar -C {shlex.quote(root)} -xf {remote}")
                finally:
                    await self._exec(environment, f"rm -f {remote}", check=False)
        stale = [p for p in files if p.endswith(".py")]
        if stale:
            patterns = [
                shlex.quote(
                    str(PurePosixPath(root, p).parent / "__pycache__" / PurePosixPath(p).stem)
                )
                + ".*.pyc"
                for p in stale
            ]
            patterns += [shlex.quote(str(PurePosixPath(root, p)) + "c") for p in stale]
            await self._exec(environment, "rm -f -- " + " ".join(patterns))

    def grade(
        self,
        label: str,
        kept: list[str],
        files: dict[str, bytes | None],
        timeout: float,
        tail_chars: int = 2_000,
    ) -> Grade:
        return self._run(self._grade(label, kept, files, timeout, tail_chars))

    async def _grade(
        self,
        label: str,
        kept: list[str],
        files: dict[str, bytes | None],
        timeout: float,
        tail_chars: int,
    ) -> Grade:
        started = time.monotonic()
        if self.snapshot is None:
            raise RuntimeError("the Modal filesystem snapshot has not been prepared")
        environment = self._environment(label, verifier=True, snapshot=self.snapshot)
        try:
            await self._start(environment)
            await self._write(environment, files)
            await self._exec(
                environment, "rm -f /logs/verifier/reward.txt /logs/verifier/reward.json"
            )
            result = await self._exec(
                environment,
                "bash /tests/test.sh",
                timeout=timeout,
                check=False,
                env=dict(self.package.verifier_env),
                user=self.package.verifier_user,
            )
            raw = await self._exec(environment, "cat /logs/verifier/reward.txt", check=False)
            tail = ((result.stdout or "") + (result.stderr or ""))[-tail_chars:]
            try:
                reward = float((raw.stdout or "").strip())
            except ValueError:
                reward = None
            if (
                raw.return_code != 0
                or reward is None
                or not math.isfinite(reward)
                or not 0 <= reward <= 1
            ):
                return Grade(
                    label,
                    kept,
                    None,
                    result.return_code,
                    time.monotonic() - started,
                    "unscored",
                    tail,
                )
            if result.return_code < 0 or result.return_code in {124, 137, 143}:
                return Grade(
                    label, kept, None, result.return_code, time.monotonic() - started, "error", tail
                )
            return Grade(
                label, kept, reward, result.return_code, time.monotonic() - started, "scored", tail
            )
        except SpendCapReached:
            raise
        except TimeoutError as exc:
            return Grade(
                label,
                kept,
                None,
                None,
                time.monotonic() - started,
                "timeout",
                str(exc)[-tail_chars:],
            )
        except Exception as exc:
            return Grade(
                label, kept, None, None, time.monotonic() - started, "error", str(exc)[-tail_chars:]
            )
        finally:
            await self._stop(environment)

    def evidence(self) -> dict[str, Any]:
        locks = []
        for environment in self.environments:
            path = environment.trial_paths.trial_dir / "egress-lock.json"
            if path.is_file():
                locks.append({"path": str(path), "record": json.loads(path.read_text())})
        return {
            "adapter": "evallab.ref_subset_modal.ModalSandbox",
            "adapter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "snapshot_image_id": self.snapshot[0].object_id if self.snapshot else None,
            "events": self.events,
            "egress_evidence": locks,
        }

    def close(self) -> None:
        failure: Exception | None = None
        try:
            for environment in self.environments:
                try:
                    self.runner.run(self._stop(environment))
                except Exception as exc:
                    failure = failure or exc
        finally:
            self.runner.close()
        if failure is not None:
            raise RuntimeError(f"Modal sandbox teardown failed: {failure}") from failure
