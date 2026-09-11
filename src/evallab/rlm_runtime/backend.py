"""Portable managed-REPL backend binding for Eval Lab.

One persistent staged worker behind a caller-owned trial environment. The
trial environment arrives as a duck-typed handle (``upload_file``/``exec``
only); this backend NEVER creates or destroys environments, NEVER stops
the provided environment, and NEVER fabricates trial metadata.

Topology per episode (adapted from the post-training managed REPL):

- ONE persistent ``worker.py`` process (supplied by the caller via
  ``worker_src``), spawned once by the staged ``relay.py`` and kept alive
  across every request; REPL state persists in-process.
- The staged ``bridge.py`` on loopback gives the worker's ``llm_query``
  an endpoint; the bridge is a verbatim forwarder to ``subreq/sN.json``
  files.
- This backend, while awaiting a worker response, polls ``subreq/`` and
  forwards each envelope to the ACTUAL ``proxy_url`` passed to ``start``
  (sub-LLM transport only; no local hook, no local accounting), then
  writes ``subresp/sN.json``.

Lifecycle contract:

- ``start`` runs setup in an inner task; cancellation JOINS the in-flight
  setup (shielded, bounded join loop) and only then tears down.
- Teardown is a shielded, repeated-cancellation-resistant barrier with a
  single enforced bound; it distinguishes ``teardown_failed`` from clean
  termination, kills ONLY the exact owned PIDs captured at launch via
  ``echo $!`` (plus the relay-reported worker pid), removes ONLY the
  backend's own ``work_root`` staging, and invalidates the session so
  later requests fail loudly. It never stops the provided environment,
  never ``pkill``-by-name, and never sweeps shared prefixes.
- Identity is the caller-given session/project/image (reported honestly
  as such, never as a container ID).

Stdlib only. Sub-LLM transport uses ``urllib`` inside threads, never
``aiohttp``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

_HERE = Path(__file__).resolve().parent
RELAY_SRC = _HERE / "relay.py"
BRIDGE_SRC = _HERE / "bridge.py"

_PROXY_POST_TIMEOUT_SEC = 120.0


@dataclass
class ExecResult:
    """Local result shape for both env exec and REPL execute.

    The trial-environment ``exec`` surface yields ``stdout``/``stderr``/
    ``return_code``; the REPL ``execute`` path additionally populates the
    worker-reported ``final_answer``/``execution_time``/``locals_keys``.
    One superset dataclass keeps the package free of Harbor imports while
    satisfying both shapes.
    """

    stdout: str = ""
    stderr: str = ""
    return_code: int = 0
    final_answer: str | None = None
    execution_time: float = 0.0
    locals_keys: list[str] = field(default_factory=list)


@runtime_checkable
class EnvironmentHandle(Protocol):
    """Duck-typed trial environment supplied by the caller (Harness)."""

    async def upload_file(self, source_path: str | Path, target_path: str) -> None: ...

    async def exec(self, command: str, timeout_sec: float | None = None) -> ExecResult: ...


class ManagedReplError(RuntimeError):
    pass


class ManagedReplBackend:
    def __init__(
        self,
        environment: EnvironmentHandle,
        *,
        worker_src: Path,
        work_root: str = "/opt/rlm-managed",
        bridge_port: int = 8765,
        request_timeout: float = 120.0,
        startup_timeout: float = 60.0,
        teardown_timeout: float = 30.0,
        session_id: str = "rlm-managed",
        session: str | None = None,
        project: str | None = None,
        image: str | None = None,
    ) -> None:
        self._environment = environment
        self._worker_src = Path(worker_src)
        self._work_root = work_root
        self._bridge_port = bridge_port
        self._request_timeout = request_timeout
        self._startup_timeout = startup_timeout
        self._teardown_timeout = teardown_timeout
        self._session_id = session if session is not None else session_id
        self._project = project if project is not None else self._session_id
        self._image = image
        self._seq = 0
        self._lock = asyncio.Lock()
        self._started = False
        self._staged = False
        self._teardown_failed: str | None = None
        self._forwarded_subcalls = 0
        self._proxy_url: str | None = None
        self._bridge_pid: int | None = None
        self._relay_pid: int | None = None
        self._worker_pid: int | None = None
        self._teardown_task: asyncio.Task | None = None

    @property
    def identity(self) -> dict[str, Any]:
        return {
            "kind": "managed_environment_handle",
            "session_id": self._session_id,
            "session": self._session_id,
            "project": self._project,
            "compose_project": self._project,
            "image": self._image,
            "note": "caller-provided trial environment handle; not a container ID",
        }

    @property
    def forwarded_subcalls(self) -> int:
        return self._forwarded_subcalls

    @property
    def teardown_failed(self) -> str | None:
        return self._teardown_failed

    # -- lifecycle ------------------------------------------------------
    async def start(self, proxy_url: str, rollout_id: str, depth: int = 1) -> None:
        if self._started:
            raise ManagedReplError("backend already started")
        self._proxy_url = proxy_url.rstrip("/")
        inner = asyncio.create_task(self._start_inner(proxy_url, rollout_id, depth))
        try:
            await asyncio.wait_for(asyncio.shield(inner), timeout=self._startup_timeout)
        except TimeoutError:
            inner.cancel()
            await self._join(inner)
            await self._teardown_barrier()
            raise ManagedReplError(f"managed startup exceeded {self._startup_timeout:g}s") from None
        except asyncio.CancelledError:
            # JOIN the in-flight setup under repeated cancellation: the
            # shield keeps ``inner`` running; we do not return until it has
            # actually finished, so no owned process can appear after
            # cancellation returned. Then tear down whatever exists.
            await self._join(inner)
            await self._teardown_barrier()
            raise
        except Exception:
            await self._join(inner)
            await self._teardown_barrier()
            raise
        self._started = True

    async def _join(self, task: asyncio.Task) -> None:
        """Await ``task`` to completion, resisting repeated cancellation."""
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                return

    async def _start_inner(self, proxy_url: str, rollout_id: str, depth: int) -> None:
        env = self._environment
        root = self._work_root
        q_root = shlex.quote(root)
        await env.exec(
            f"mkdir -p {q_root}/reqs {q_root}/resps "
            f"{q_root}/subreq {q_root}/subresp",
            timeout_sec=30,
        )
        self._staged = True

        async def put(src: Path, dst: str) -> None:
            await env.upload_file(src, dst)

        await put(self._worker_src, f"{root}/worker.py")
        await put(RELAY_SRC, f"{root}/relay.py")
        await put(BRIDGE_SRC, f"{root}/bridge.py")
        env_prefix = (
            f"RLM_TRAIN_PROXY_URL=http://127.0.0.1:{self._bridge_port} "
            f"RLM_TRAIN_ROLLOUT_ID={shlex.quote(rollout_id)} "
            f"RLM_TRAIN_DEPTH={shlex.quote(str(depth))} "
        )
        bridge_cmd = (
            f"nohup env {env_prefix}"
            f"python3 {q_root}/bridge.py {q_root} {self._bridge_port} "
            f"> {q_root}/bridge.log 2>&1 & echo $!"
        )
        bridge_res = await env.exec(bridge_cmd, timeout_sec=30)
        self._bridge_pid = self._parse_pid(bridge_res.stdout, "bridge")
        relay_cmd = (
            f"nohup env {env_prefix}"
            f"python3 {q_root}/relay.py {q_root} "
            f"> {q_root}/relay.log 2>&1 & echo $!"
        )
        relay_res = await env.exec(relay_cmd, timeout_sec=30)
        self._relay_pid = self._parse_pid(relay_res.stdout, "relay")
        await self._await_ready()

    @staticmethod
    def _parse_pid(stdout: str | None, name: str) -> int:
        tokens = (stdout or "").strip().split()
        if not tokens:
            raise ManagedReplError(f"{name} launch returned no pid")
        try:
            return int(tokens[-1])
        except ValueError:
            raise ManagedReplError(
                f"{name} launch returned unparsable pid: {(stdout or '')[-100:]}"
            ) from None

    async def _await_ready(self, tries: int = 120) -> None:
        env = self._environment
        root = self._work_root
        q_root = shlex.quote(root)
        for _ in range(tries):
            probe = await env.exec(
                f'python3 -c "import socket;'
                f"socket.create_connection(('127.0.0.1',{self._bridge_port}),timeout=1)."
                f'close()" && cat {q_root}/resps/_init.json',
                timeout_sec=10,
            )
            if probe.return_code == 0 and "ok" in (probe.stdout or ""):
                await self._read_worker_pid()
                return
            await asyncio.sleep(0.5)
        raise ManagedReplError("managed relay/bridge never became ready")

    async def _read_worker_pid(self) -> None:
        try:
            probe = await self._environment.exec(
                f"cat {shlex.quote(self._work_root)}/worker.pid",
                timeout_sec=10,
            )
        except Exception:  # noqa: BLE001
            return
        if probe.return_code == 0:
            try:
                self._worker_pid = int((probe.stdout or "").strip().split()[-1])
            except (ValueError, IndexError):
                self._worker_pid = None

    async def stop(self) -> None:
        await self._teardown_barrier()

    async def cancel(self) -> None:
        await self._teardown_barrier()

    async def _teardown_barrier(self) -> None:
        """Shielded, bounded, repeated-cancellation-resistant teardown.

        Kills ONLY the exact owned PIDs, removes ONLY the backend's own
        ``work_root`` staging, and invalidates the session. The provided
        environment itself is NEVER stopped.
        """
        current = self._teardown_task
        if current is not None and not current.done():
            try:
                await asyncio.shield(current)
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(current)
                except asyncio.CancelledError:
                    raise
            return

        async def run() -> None:
            try:
                await asyncio.wait_for(
                    self._teardown_owned(),
                    timeout=self._teardown_timeout,
                )
            except TimeoutError:
                self._teardown_failed = f"managed teardown exceeded {self._teardown_timeout:g}s"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._teardown_failed = f"{type(exc).__name__}: {exc}"
            finally:
                self._started = False
                self._proxy_url = None
                self._bridge_pid = None
                self._relay_pid = None
                self._worker_pid = None
                self._staged = False

        task = asyncio.create_task(run())
        self._teardown_task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # First cancellation waits for the barrier; a second one also
            # cannot interrupt the shielded inner work.
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    self._teardown_failed = "teardown barrier cancelled before completion"
                raise
        finally:
            if self._teardown_task is task and task.done():
                self._teardown_task = None

    async def _teardown_owned(self) -> None:
        env = self._environment
        for pid in (self._worker_pid, self._relay_pid, self._bridge_pid):
            if pid is None:
                continue
            try:
                await env.exec(f"kill {int(pid)} 2>/dev/null || true", timeout_sec=10)
            except Exception:  # noqa: BLE001
                continue
        # Reap stubborn owned processes by exact PID only (never pkill).
        await asyncio.sleep(0.5)
        for pid in (self._worker_pid, self._relay_pid, self._bridge_pid):
            if pid is None:
                continue
            try:
                await env.exec(f"kill -9 {int(pid)} 2>/dev/null || true", timeout_sec=10)
            except Exception:  # noqa: BLE001
                continue
        if self._staged:
            with contextlib.suppress(Exception):
                await env.exec(f"rm -rf -- {shlex.quote(self._work_root)}", timeout_sec=30)

    # -- ReplBackend API -------------------------------------------------
    async def load_context(self, payload: Any, index: int | None = None) -> int:
        result = await self._request({"type": "load_context", "payload": payload, "index": index})
        return int(result.get("index", 0))

    async def bootstrap(self, code: str) -> None:
        if not code:
            return
        await self._request({"type": "bootstrap", "code": code})

    async def execute(self, code: str) -> ExecResult:
        result = await self._request({"type": "exec", "code": code})
        return ExecResult(
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            return_code=0 if result.get("ok", True) else 1,
            final_answer=result.get("final_answer"),
            execution_time=float(result.get("execution_time") or 0.0),
            locals_keys=list(result.get("locals_keys") or []),
        )

    # -- request/forward plumbing ----------------------------------------
    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            if not self._started:
                raise ManagedReplError(
                    "backend session invalidated (timeout/cancel/stop); "
                    "restart the backend for a fresh session"
                )
            self._seq += 1
            name = f"r{self._seq:06d}"
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
                tmp.write(json.dumps({"id": name, **payload}))
                tmp_path = Path(tmp.name)
            try:
                await self._environment.upload_file(tmp_path, f"{self._work_root}/reqs/{name}.json")
            finally:
                tmp_path.unlink(missing_ok=True)
            try:
                return await self._await_response(name)
            except TimeoutError as e:
                await self._teardown_barrier()
                raise ManagedReplError(
                    f"request {payload.get('type')} exceeded "
                    f"{self._request_timeout:g}s watchdog; session terminated"
                ) from e
            except asyncio.CancelledError:
                await self._teardown_barrier()
                raise

    async def _await_response(self, name: str) -> dict[str, Any]:
        deadline = time.monotonic() + self._request_timeout
        target = f"{self._work_root}/resps/{name}.json"
        while time.monotonic() < deadline:
            await self._forward_pending_subcalls()
            probe = await self._environment.exec(f"cat {shlex.quote(target)}", timeout_sec=15)
            out = (probe.stdout or "").strip()
            if probe.return_code == 0 and out:
                # The relay uses atomic rename so a complete JSON object
                # is final; a decode failure means cat raced the write.
                try:
                    return json.loads(out)
                except json.JSONDecodeError:
                    pass
            await asyncio.sleep(0.1)
        raise TimeoutError(f"no worker response for {name}")

    async def _forward_pending_subcalls(self) -> None:
        env = self._environment
        root = self._work_root
        listing = await env.exec(f"ls {shlex.quote(root)}/subreq", timeout_sec=10)
        if listing.return_code != 0:
            return
        names = [
            n
            for n in (listing.stdout or "").split()
            if n.endswith(".json") and await self._subresp_missing(n[:-5])
        ]
        for n in names:
            envelope_raw = await env.exec(
                f"cat {shlex.quote(root)}/subreq/{shlex.quote(n)}", timeout_sec=10
            )
            if envelope_raw.return_code != 0:
                continue
            try:
                envelope = json.loads((envelope_raw.stdout or "").strip())
            except json.JSONDecodeError:
                continue
            response = await self._post_to_env_proxy(envelope)
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
                json.dump(response, tmp)
                tmp_path = Path(tmp.name)
            try:
                await env.upload_file(tmp_path, f"{root}/subresp/{n[:-5]}.json")
            finally:
                tmp_path.unlink(missing_ok=True)
            self._forwarded_subcalls += 1

    async def _subresp_missing(self, stem: str) -> bool:
        probe = await self._environment.exec(
            f"test -f {shlex.quote(self._work_root)}/subresp/{shlex.quote(stem)}.json",
            timeout_sec=10,
        )
        return probe.return_code != 0

    async def _post_to_env_proxy(self, envelope: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._proxy_url}/rollout/{envelope['rollout_id']}/{envelope['route']}"
        body = json.dumps(envelope["body"]).encode("utf-8")

        def _do() -> str:
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=_PROXY_POST_TIMEOUT_SEC) as resp:
                    return resp.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                try:
                    return exc.read().decode("utf-8")
                except Exception:  # noqa: BLE001
                    return json.dumps({"error": f"env proxy HTTP {exc.code}"})
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"error": f"env proxy unreachable: {exc}"})

        text = await asyncio.to_thread(_do)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"error": f"env proxy returned non-JSON: {text[:200]}"}
        return parsed if isinstance(parsed, dict) else {"result": parsed}
