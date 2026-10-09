"""Host-only flight lifecycle, carried by the existing state-journal plugin.

Opt-in configuration lives beside the job directory, never in a target env,
mount, prompt or tool. Observer failures are recorded, never hidden.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from evallab.flight import observer


def configuration_path(job_dir: Path) -> Path:
    return job_dir.parent / ".flight" / f"{job_dir.name}.json"


@dataclass
class FlightRecorderPlugin:
    ready_timeout_seconds: float = 20.0
    image: str | None = None
    configuration: dict[str, Any] = field(default_factory=dict)
    active: dict[tuple[str, str], observer.Observer] = field(default_factory=dict)
    image_error: str | None = None
    original_setup_hooks: Any = None
    queue: Any = None
    job_dir: Path | None = None
    errors: list[dict[str, str]] = field(default_factory=list)
    errors_dropped: int = 0
    sessions: dict[str, str] = field(default_factory=dict)

    def _failed_job(self, phase: str, reason: str) -> None:
        reason = reason[:4096]
        if len(self.errors) < 30:
            self.errors.append({"phase": phase, "reason": reason})
        else:
            self.errors_dropped += 1
        try:
            if self.job_dir is not None:
                self.job_dir.mkdir(parents=True, exist_ok=True)
                (self.job_dir / "flight-unavailable.json").write_text(json.dumps({
                    "schema": "evallab.flight.status/v1", "status": "unavailable",
                    "errors": self.errors, "errors_dropped": self.errors_dropped,
                }) + "\n")
            logging.getLogger(__name__).warning("Flight observer %s unavailable: %s", phase, reason)
        except Exception:
            pass

    def _failed_trial(self, event: Any, phase: str, reason: str,
                      *, code: str = "observer_unavailable") -> None:
        try:
            output = Path(event.config.trials_dir) / event.trial_name / "flight"
            observer.write_status_unavailable(
                output, reason[:4096], self.image, phase=phase, code=code)
            trial_id = str(event.trial_id)
        except Exception:
            trial_id = "unknown"
        self._failed_job(f"{trial_id}:{phase}", reason)

    def _failed_instrumentation(self, trial: Any, phase: str, exc: Exception) -> None:
        reason = f"{type(exc).__name__}: {exc}"
        try:
            self._failed_trial(self._event(trial), phase, reason)
        except Exception:
            self._failed_job(phase, reason)

    async def on_job_start(self, job: Any) -> None:
        try:
            self.job_dir = Path(job.job_dir)
            path = configuration_path(self.job_dir)
            if not path.is_file():
                return
            configuration = json.loads(path.read_text())
            if not isinstance(configuration, dict):
                raise ValueError("flight configuration must be an object")
            if configuration.get("schema") != "evallab.flight.config/v1":
                raise ValueError("invalid flight configuration schema")
            if configuration.get("egress", "locked") not in ("locked", "open"):
                raise ValueError("flight egress must be locked or open")
            timeout = float(configuration.get("ready_timeout_seconds", self.ready_timeout_seconds))
            if not math.isfinite(timeout) or timeout <= 0 or timeout > 30:
                raise ValueError("flight ready timeout must be >0 and <=30 seconds")
            self.ready_timeout_seconds = timeout
            self.configuration = configuration
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed_job("configuration", f"{type(exc).__name__}: {exc}")
            return
        try:
            self.image = await asyncio.wait_for(observer.ensure_image(), self.ready_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.image_error = f"{type(exc).__name__}: {exc}"
            self._failed_job("image", self.image_error)
        try:
            (self.job_dir / "flight-recording.json").write_text(
                json.dumps(self.configuration, indent=2) + "\n")
        except Exception as exc:
            self._failed_job("configuration_persistence", f"{type(exc).__name__}: {exc}")
        self.queue = getattr(job, "_trial_queue", None)
        self.original_setup_hooks = getattr(self.queue, "_setup_hooks", None)
        if not callable(self.original_setup_hooks):
            self._failed_job("instrumentation", "trial queue has no callable _setup_hooks")
            return

        def setup_hooks(trial: Any) -> None:
            self.original_setup_hooks(trial)
            try:
                self._instrument_trial(trial)
            except Exception as exc:
                self._failed_instrumentation(trial, "agent", exc)

        try:
            queue: Any = self.queue
            queue._setup_hooks = setup_hooks
            for name, callback in (
                ("on_agent_ended", self._on_agent_ended),
                ("on_trial_ended", self._on_trial_ended),
                ("on_trial_cancelled", self._on_trial_ended),
                ("on_verification_started", self._on_verification_started),
            ):
                register = getattr(job, name, None)
                if callable(register):
                    register(callback)
                else:
                    self._failed_job("instrumentation", f"job has no callable {name}")
        except Exception as exc:
            self._failed_job("instrumentation", f"{type(exc).__name__}: {exc}")

    async def on_job_end(self, _result: Any) -> None:
        cancelled = False

        async def sweep() -> bool:
            interrupted = False
            for key in list(self.active):
                try:
                    await self._stop(*key)
                except asyncio.CancelledError:
                    interrupted = True
                    self._failed_job("stop", f"{key}: observer stop cancelled")
                except Exception as exc:
                    self._failed_job("stop", f"{key}: {type(exc).__name__}: {exc}")
            return interrupted

        task = asyncio.create_task(sweep())
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            cancelled = task.result() or cancelled
        finally:
            if self.queue is not None and callable(self.original_setup_hooks):
                try:
                    self.queue._setup_hooks = self.original_setup_hooks
                except Exception as exc:
                    self._failed_job("hook_restore", f"{type(exc).__name__}: {exc}")
        if cancelled:
            raise asyncio.CancelledError

    @staticmethod
    def _event(trial: Any) -> Any:
        return SimpleNamespace(config=trial.config, trial_name=trial.config.trial_name,
                               trial_id=trial.id, task_name=trial.task.name,
                               result=trial.result)

    def _instrument_trial(self, trial: Any) -> None:
        environment = trial.agent_environment
        original_start = environment.start

        async def start(*args: Any, **kwargs: Any) -> Any:
            result = await original_start(*args, **kwargs)
            try:
                event = self._event(trial)
                session = str(environment.session_id)
                self.sessions[str(trial.id)] = session
                await self._start(event, "agent", session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._failed_instrumentation(trial, "agent", exc)
            return result

        environment.start = start
        original_verifier = getattr(trial, "_separate_verifier_env", None)
        if not callable(original_verifier):
            return

        @contextlib.asynccontextmanager
        async def separate_verifier(*args: Any, **kwargs: Any):
            async with original_verifier(*args, **kwargs) as verifier:
                try:
                    await self._start(self._event(trial), "verifier", str(verifier.session_id))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._failed_instrumentation(trial, "verifier", exc)
                try:
                    yield verifier
                finally:
                    try:
                        await self._safe_stop(self._event(trial), "verifier")
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._failed_instrumentation(trial, "verifier", exc)

        trial._separate_verifier_env = separate_verifier

    async def _start(self, event: Any, phase: str, session_id: str) -> None:
        try:
            key = (str(event.trial_id), phase)
            if key in self.active:
                return
            output = Path(event.config.trials_dir) / event.trial_name / "flight"
            if self.image is None:
                self._failed_trial(event, phase, self.image_error or "image unavailable")
                return
            async with asyncio.timeout(self.ready_timeout_seconds):
                target = await observer.resolve_session_container(session_id)
                info = await observer.inspect_container(target)
                network_mode = info.get("HostConfig", {}).get("NetworkMode")
                tier = self.configuration.get("egress", "locked")
                if tier == "locked" and network_mode != "none":
                    raise RuntimeError(f"locked target is not network_mode none: {network_mode}")
                enforcement = "network_mode_none" if network_mode == "none" else "none"
                sniff = ""
                if tier == "open":
                    found = observer.container_ip(info)
                    if found is None:
                        raise RuntimeError("open target lacks bridge IPv4")
                    network, ip = found
                    interface = await observer.bridge_interface(network)
                    if interface is None:
                        raise RuntimeError("open target lacks native bridge interface")
                    sniff = f"{interface},{ip}"
                identity = {"trial_id": str(event.trial_id), "job_id": str(event.config.job_id),
                            "task": event.task_name, "task_id": str(event.result.task_id)}
                name = re.sub(r"[^a-zA-Z0-9_.-]", "-", str(event.trial_id))
                handle = await observer.start_observer(
                    image=self.image, observer_name=f"evallab-flight-{name}-{phase}",
                    container=target, output_dir=output, phase=phase, sniff=sniff,
                    identity=identity, enforcement=enforcement,
                    ready_timeout_seconds=self.ready_timeout_seconds)
                self.active[key] = handle
                (output / f"observer.{phase}.json").write_text(json.dumps({
                    "target_container": target, "target_pid": handle.target_pid, "phase": phase,
                    "egress_tier": tier, "enforcement": enforcement, "sniff": sniff,
                    "mode": "passive", "target_mutated": False, **identity}, indent=2) + "\n")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed_trial(event, phase, f"{type(exc).__name__}: {exc}")

    async def _stop(self, trial_id: str, phase: str) -> None:
        handle = self.active.pop((trial_id, phase), None)
        if handle is not None:
            await observer.stop_observer(handle.name)

    async def _safe_stop(self, event: Any, phase: str) -> None:
        try:
            await self._stop(str(event.trial_id), phase)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed_trial(event, phase, f"{type(exc).__name__}: {exc}")

    async def _on_agent_ended(self, event: Any) -> None:
        await self._safe_stop(event, "agent")

    async def _on_verification_started(self, event: Any) -> None:
        try:
            mode = getattr(event.result, "verifier_environment_mode", None)
            mode = getattr(mode, "value", mode)
            session = self.sessions.get(str(event.trial_id))
            if mode == "shared" and session is not None:
                await self._start(event, "verifier", session)
            elif mode is None or (mode == "shared" and session is None):
                self._failed_trial(event, "verifier", "shared verifier lifecycle not observed",
                                   code="verifier_unobserved")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed_trial(event, "verifier", f"{type(exc).__name__}: {exc}")

    async def _on_trial_ended(self, event: Any) -> None:
        for phase in ("agent", "verifier"):
            await self._safe_stop(event, phase)
