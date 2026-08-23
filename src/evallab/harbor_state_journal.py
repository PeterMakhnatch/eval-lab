"""Passive filesystem state journal for Harbor Docker trials.

The observer runs in a separate container in the Docker host PID namespace. It
reads the task root through ``/proc/<target-pid>/root/app`` and writes only to a
host directory mounted into the observer. The task container receives no mount,
file, environment variable, executable, or prompt change.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLUGIN_IMPORT_PATH = "evallab.harbor_state_journal:StateJournalPlugin"


@dataclass(frozen=True)
class Monitor:
    name: str
    output_dir: Path
    target_container: str
    target_pid: int


def compose_project_name(trial_name: str) -> str:
    """Reproduce Harbor's Docker Compose project naming for an agent environment."""
    name = f"{trial_name}__env".lower()
    if not re.match(r"^[a-z0-9]", name):
        name = "0" + name
    return re.sub(r"[^a-z0-9_-]", "-", name)


def monitor_command(
    *,
    image: str,
    monitor_name: str,
    target_pid: int,
    output_dir: Path,
    watch_root: str,
    max_hash_bytes: int,
) -> list[str]:
    """Build a sidecar command that does not mutate or mount into the target."""
    return [
        "docker",
        "run",
        "--detach",
        "--rm",
        "--name",
        monitor_name,
        "--pid=host",
        "--cap-add=SYS_PTRACE",
        "--security-opt",
        "no-new-privileges=true",
        "--volume",
        f"{output_dir.resolve()}:/journal",
        "--env",
        f"TARGET_PID={target_pid}",
        "--env",
        f"WATCH_ROOT={watch_root}",
        "--env",
        f"MAX_HASH_BYTES={max_hash_bytes}",
        image,
    ]


async def _command(*args: str, timeout: float = 120.0) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 124, "", f"command timed out after {timeout:.0f}s"
    return (
        process.returncode or 0,
        stdout.decode(errors="replace").strip(),
        stderr.decode(errors="replace").strip(),
    )


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


class StateJournalPlugin:
    """Attach a fail-open, model-invisible state observer to Docker trials."""

    def __init__(
        self,
        *,
        context_dir: str | Path | None = None,
        watch_root: str = "/app",
        max_hash_bytes: int = 8 * 1024 * 1024,
        ready_timeout_seconds: float = 20.0,
    ) -> None:
        self.context_dir = Path(context_dir or Path.cwd() / "containers" / "state-journal")
        self.watch_root = watch_root
        self.max_hash_bytes = max_hash_bytes
        self.ready_timeout_seconds = ready_timeout_seconds
        self.image: str | None = None
        self.image_error: str | None = None
        self.monitors: dict[str, Monitor] = {}

    async def on_job_start(self, job: Any) -> None:
        try:
            self.image = await self._ensure_image()
        except Exception as exc:  # fail-open observability must never fail a trial
            self.image_error = f"{type(exc).__name__}: {exc}"
        job.on_agent_started(self._on_agent_started)
        job.on_agent_ended(self._on_agent_ended)
        job.on_trial_ended(self._on_trial_ended)
        job.on_trial_cancelled(self._on_trial_ended)

    async def on_job_end(self, _job_result: Any) -> None:
        for trial_id in list(self.monitors):
            await self._stop(trial_id)

    def _output_dir(self, event: Any) -> Path:
        return Path(event.config.trials_dir) / event.trial_name / "state-journal"

    async def _ensure_image(self) -> str:
        dockerfile = self.context_dir / "Dockerfile"
        watcher = self.context_dir / "watch.py"
        if not dockerfile.is_file() or not watcher.is_file():
            raise FileNotFoundError(
                f"state journal image context is incomplete: {self.context_dir}"
            )
        content_digest = hashlib.sha256(dockerfile.read_bytes() + watcher.read_bytes()).hexdigest()
        image = f"evallab-state-journal:{content_digest[:16]}"
        code, _, _ = await _command("docker", "image", "inspect", image, timeout=15)
        if code == 0:
            return image
        code, stdout, stderr = await _command(
            "docker",
            "build",
            "--quiet",
            "--tag",
            image,
            str(self.context_dir),
            timeout=600,
        )
        if code != 0:
            raise RuntimeError(stderr or stdout or "state journal image build failed")
        return image

    async def _target_container(self, trial_name: str) -> str:
        project = compose_project_name(trial_name)
        code, stdout, stderr = await _command(
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "label=com.docker.compose.service=main",
            "--format",
            "{{.ID}}",
            timeout=15,
        )
        containers = [line for line in stdout.splitlines() if line]
        if code != 0 or len(containers) != 1:
            detail = stderr or f"found {len(containers)} matching main containers"
            raise RuntimeError(f"cannot resolve target container for {trial_name}: {detail}")
        return containers[0]

    async def _target_pid(self, container: str) -> int:
        code, stdout, stderr = await _command(
            "docker",
            "inspect",
            "--format",
            "{{.State.Pid}}",
            container,
            timeout=15,
        )
        if code != 0 or not stdout.isdigit() or int(stdout) <= 0:
            raise RuntimeError(stderr or f"invalid target pid: {stdout!r}")
        return int(stdout)

    def _unavailable(self, output: Path, reason: str) -> None:
        _atomic_json(
            output / "status.json",
            {
                "schema_version": 1,
                "status": "unavailable",
                "reason": reason,
                "observer": {
                    "mode": "external-sidecar",
                    "target_mutated": False,
                    "model_visible_output": False,
                    "image": self.image,
                },
            },
        )

    async def _on_agent_started(self, event: Any) -> None:
        trial_id = str(event.trial_id)
        output = self._output_dir(event)
        output.mkdir(parents=True, exist_ok=True)
        output.chmod(0o700)
        if self.image is None:
            self._unavailable(output, self.image_error or "observer image unavailable")
            return
        try:
            target = await self._target_container(event.trial_name)
            target_pid = await self._target_pid(target)
            monitor_name = f"evallab-state-{trial_id.replace('-', '')[:24]}"
            await _command("docker", "rm", "--force", monitor_name, timeout=15)
            command = monitor_command(
                image=self.image,
                monitor_name=monitor_name,
                target_pid=target_pid,
                output_dir=output,
                watch_root=self.watch_root,
                max_hash_bytes=self.max_hash_bytes,
            )
            code, stdout, stderr = await _command(*command, timeout=60)
            if code != 0:
                raise RuntimeError(stderr or stdout or "observer container failed to start")
            monitor = Monitor(monitor_name, output, target, target_pid)
            self.monitors[trial_id] = monitor
            deadline = asyncio.get_running_loop().time() + self.ready_timeout_seconds
            while asyncio.get_running_loop().time() < deadline:
                if (output / "READY").is_file():
                    return
                await asyncio.sleep(0.05)
            await self._stop(trial_id)
            raise RuntimeError("observer did not become ready before agent execution")
        except Exception as exc:  # fail-open: absence is explicit, task continues untouched
            self._unavailable(output, f"{type(exc).__name__}: {exc}")

    async def _on_agent_ended(self, event: Any) -> None:
        await self._stop(str(event.trial_id))

    async def _on_trial_ended(self, event: Any) -> None:
        await self._stop(str(event.trial_id))

    async def _stop(self, trial_id: str) -> None:
        monitor = self.monitors.pop(trial_id, None)
        if monitor is None:
            return
        code, stdout, stderr = await _command(
            "docker",
            "stop",
            "--time",
            "10",
            monitor.name,
            timeout=20,
        )
        status_path = monitor.output_dir / "status.json"
        if code != 0 or not (monitor.output_dir / "state-diff.json").is_file():
            self._unavailable(
                monitor.output_dir,
                stderr or stdout or "observer stopped without a state diff",
            )
            return
        status: dict[str, Any]
        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
            status = payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            status = {}
        if not status:
            status = {
                "schema_version": 1,
                "status": "unavailable",
                "reason": "status unreadable",
            }
        status["observer"] = {
            "mode": "external-sidecar",
            "target_mutated": False,
            "model_visible_output": False,
            "image": self.image,
            "target_container": monitor.target_container,
            "target_pid": monitor.target_pid,
        }
        _atomic_json(status_path, status)
