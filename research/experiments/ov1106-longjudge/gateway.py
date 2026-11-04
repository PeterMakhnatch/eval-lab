"""Lane-local gateway: configured launch and fail-closed, persistent route admission."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

BASE_PATH = Path(__file__).resolve().parent.parent / "ov1104-judges" / "gateway.py"
_spec = importlib.util.spec_from_file_location("_longjudge_base_gateway", BASE_PATH)
assert _spec is not None and _spec.loader is not None
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)
CONFIG = Path("/private/tmp/ov1106/shared/nofallback.yml")


class RouteFailure(RuntimeError):
    """The batch cannot admit any further requests."""


class Gateway(base.Gateway):
    _current = None
    _lock = None

    @classmethod
    async def get(cls):
        loop = asyncio.get_running_loop()
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        async with cls._lock:
            current = cls._current
            if current is not None and current.loop is loop and current.process.returncode is None:
                return current
            process = await asyncio.create_subprocess_exec(
                "omp", "auth-gateway", "stdio", "--config", str(CONFIG),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=1 << 26,
            )
            try:
                assert process.stdout is not None
                line = await asyncio.wait_for(process.stdout.readline(), timeout=120)
                ready = json.loads(line) if line.strip() else {}
                if not ready.get("ready"):
                    raise RuntimeError(f"omp auth-gateway stdio did not start: {line[:200]!r}")
            except BaseException:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
            cls._current = gateway = cls(process, loop)
            return gateway


class Caller(base.Caller):
    def __init__(self, log_path: Path, **kwargs: Any) -> None:
        super().__init__(log_path, **kwargs)
        self.stop_path = log_path.parent / "route-stop.json"
        self.stopped = json.loads(self.stop_path.read_bytes()) if self.stop_path.exists() else None
        self.active: set[asyncio.Task] = set()

    def stop_route(self, record: dict[str, Any]) -> None:
        if self.stopped is None:
            self.stopped = {"reason": "route_failure", "model_requested": record.get("model_requested"),
                            "model_served": record.get("model_served"), "status": record.get("status"),
                            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            # Synchronous atomic persistence precedes cancellation or semaphore release.
            fd, temporary = tempfile.mkstemp(prefix=".route-stop-", dir=self.stop_path.parent)
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump(self.stopped, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.stop_path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        for task in self.active:
            if task is not asyncio.current_task():
                task.cancel()
        if Gateway._current is not None:
            Gateway._current.close()

    async def call(self, model: str, prompt: str, *, tag: dict[str, Any], seed: int | None = None,
                   system: str | None = None) -> dict[str, Any]:
        task = asyncio.current_task()
        assert task is not None
        self.active.add(task)
        try:
            async with self.semaphore:
                if self.stopped or self.stop_path.exists():
                    raise RouteFailure("route admission is stopped; see route-stop.json")
                messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
                body = {"model": model, "messages": messages, "temperature": self.temperature, "max_tokens": self.max_tokens}
                if seed is not None:
                    body["seed"] = seed
                record = {**tag, "model_requested": model, "model_served": None,
                          "prompt_sha256": base.hashlib.sha256(prompt.encode()).hexdigest(),
                          "prompt_chars": len(prompt), "status": None, "error": None,
                          "usage": None, "latency_s": None, "attempts": 0, "ts": None, "text": ""}
                for attempt in range(1, base.MAX_ATTEMPTS + 1):
                    if self.stopped or self.stop_path.exists():
                        raise RouteFailure("route admission is stopped; see route-stop.json")
                    record.update(attempts=attempt, ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                    started = time.monotonic()
                    try:
                        gateway = await Gateway.get()
                        status, payload = await gateway.request("/v1/chat/completions", body)
                    except Exception as exc:
                        record.update(status=-1, error=f"{type(exc).__name__}: {exc}"[:500],
                                      latency_s=round(time.monotonic() - started, 2))
                        if attempt < base.MAX_ATTEMPTS:
                            await asyncio.sleep(2.0 * attempt)
                        continue
                    record.update(status=status, model_served=base._served_model(payload),
                                  latency_s=round(time.monotonic() - started, 2))
                    if record["model_served"] != model:
                        record["error"] = "missing or mismatched served route attestation"
                        self.stop_route(record)
                        await self._log(record | {"text_chars": 0})
                        raise RouteFailure(record["error"])
                    if status >= 400:
                        record["error"] = json.dumps(payload, ensure_ascii=False)[:500]
                        if status in (429, 500, 502, 503, 504) and attempt < base.MAX_ATTEMPTS:
                            await asyncio.sleep(3.0 * attempt)
                            continue
                        break
                    record.update(usage=payload.get("usage"), text=base._content(payload), error=None)
                    break
                await self._log({k: v for k, v in record.items() if k != "text"} | {"text_chars": len(record["text"])})
                return record
        finally:
            self.active.discard(task)
