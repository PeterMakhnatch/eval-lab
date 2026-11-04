"""$0 model calls through ``omp auth-gateway stdio`` (Peter's subscriptions), with attestation.

The gateway speaks JSON lines: ``{"id", "path", "body"}`` in, ``{"id", "status", "body"}`` out,
where ``path`` is an OpenAI Chat Completions path and ``body`` the usual request. The response
body carries the served ``model``; a call whose served model differs from the requested one is
a failure, never a result (a fallback is not the model an experiment names).

Every call is appended to ``calls.jsonl`` (requested/served model, prompt sha256, usage, latency,
status) so RESULT.md can attest served models and call counts per route from the log alone.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import itertools
import json
import time
from pathlib import Path
from typing import Any

GATEWAY_TIMEOUT_S = 600.0
MAX_ATTEMPTS = 3


class Gateway:
    """One ``omp auth-gateway stdio`` child per event loop; concurrent requests share it by id."""

    _current: Gateway | None = None

    def __init__(self, process: asyncio.subprocess.Process, loop: asyncio.AbstractEventLoop) -> None:
        self.process = process
        self.loop = loop
        self.pending: dict[str, asyncio.Future[tuple[int, Any]]] = {}
        self.ids = itertools.count(1)
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        self.reader = loop.create_task(self._read_stdout())
        self.stderr_reader = loop.create_task(self._read_stderr())

    _lock: asyncio.Lock | None = None

    @classmethod
    async def get(cls) -> Gateway:
        loop = asyncio.get_running_loop()
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        async with cls._lock:  # one child at a time: concurrent spawns race on omp's auth store
            current = cls._current
            if current is not None and current.loop is loop and current.process.returncode is None:
                return current
            process = await asyncio.create_subprocess_exec(
                "omp", "auth-gateway", "stdio",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=1 << 26,
            )
            assert process.stdout is not None
            line = await asyncio.wait_for(process.stdout.readline(), timeout=120)
            ready = json.loads(line) if line.strip() else {}
            if not ready.get("ready"):
                process.kill()
                raise RuntimeError(f"omp auth-gateway stdio did not start: {line[:200]!r}")
            cls._current = gateway = cls(process, loop)
            return gateway

    async def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        while line := await self.process.stdout.readline():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            future = self.pending.pop(str(message.get("id")), None)
            if future is not None and not future.done():
                future.set_result((int(message.get("status", 0)), message.get("body")))
        error = RuntimeError(f"omp auth-gateway stdio exited: {' | '.join(self.stderr_tail)}")
        for future in self.pending.values():
            if not future.done():
                future.set_exception(error)
        self.pending.clear()

    async def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        while line := await self.process.stderr.readline():
            self.stderr_tail.append(line.decode(errors="replace").strip())

    async def request(self, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        assert self.process.stdin is not None
        request_id = str(next(self.ids))
        future: asyncio.Future[tuple[int, Any]] = self.loop.create_future()
        self.pending[request_id] = future
        self.process.stdin.write((json.dumps({"id": request_id, "path": path, "body": body}) + "\n").encode())
        await self.process.stdin.drain()
        try:
            return await asyncio.wait_for(future, timeout=GATEWAY_TIMEOUT_S)
        finally:
            self.pending.pop(request_id, None)

    def close(self) -> None:
        if self.process.returncode is None:
            self.process.kill()


def _served_model(payload: Any) -> str | None:
    if isinstance(payload, dict):
        model = payload.get("model")
        return model if isinstance(model, str) else None
    return None


def _content(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


class Caller:
    """Bounded-concurrency chat calls with a JSONL log; ``call`` returns the reply text + record."""

    def __init__(self, log_path: Path, *, concurrency: int = 6, temperature: float = 0.0, max_tokens: int = 1500) -> None:
        self.log_path = log_path
        self.semaphore = asyncio.Semaphore(concurrency)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def _log(self, record: dict[str, Any]) -> None:
        async with self._lock:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def call(self, model: str, prompt: str, *, tag: dict[str, Any], seed: int | None = None,
                   system: str | None = None) -> dict[str, Any]:
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        body: dict[str, Any] = {
            "model": model, "messages": messages, "temperature": self.temperature, "max_tokens": self.max_tokens,
        }
        if seed is not None:
            body["seed"] = seed
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        record: dict[str, Any] = {
            **tag, "model_requested": model, "model_served": None, "prompt_sha256": prompt_sha,
            "prompt_chars": len(prompt), "status": None, "error": None, "usage": None, "latency_s": None,
            "attempts": 0, "ts": None, "text": "",
        }
        async with self.semaphore:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                record["attempts"] = attempt
                record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                started = time.monotonic()
                try:
                    gateway = await Gateway.get()
                    status, payload = await gateway.request("/v1/chat/completions", body)
                except Exception as exc:  # gateway died or timed out: retry once the child restarts
                    record["status"], record["error"] = -1, f"{type(exc).__name__}: {exc}"[:500]
                    record["latency_s"] = round(time.monotonic() - started, 2)
                    await asyncio.sleep(2.0 * attempt)
                    continue
                record["latency_s"] = round(time.monotonic() - started, 2)
                record["status"] = status
                served = _served_model(payload)
                record["model_served"] = served
                if status >= 400:
                    record["error"] = json.dumps(payload, ensure_ascii=False)[:500] if payload is not None else f"HTTP {status}"
                    if status in (429, 500, 502, 503, 504) and attempt < MAX_ATTEMPTS:
                        await asyncio.sleep(3.0 * attempt)
                        continue
                    break
                if served is not None and served != model:
                    record["error"] = f"served {served}, requested {model}"
                    break
                record["usage"] = payload.get("usage") if isinstance(payload, dict) else None
                record["text"] = _content(payload)
                record["error"] = None
                break
        await self._log({k: v for k, v in record.items() if k != "text"} | {"text_chars": len(record["text"])})
        return record


def summarize_log(log_path: Path) -> dict[str, Any]:
    """Call counts per requested route, served models, failures: the attestation block."""
    per_route: dict[str, dict[str, Any]] = {}
    for line in log_path.read_text(encoding="utf-8").splitlines() if log_path.is_file() else []:
        if not line.strip():
            continue
        row = json.loads(line)
        bucket = per_route.setdefault(row["model_requested"], {"calls": 0, "ok": 0, "failed": 0, "served": {}, "tokens_in": 0, "tokens_out": 0})
        bucket["calls"] += 1
        if row.get("error"):
            bucket["failed"] += 1
        else:
            bucket["ok"] += 1
        served = row.get("model_served") or "unattested"
        bucket["served"][served] = bucket["served"].get(served, 0) + 1
        usage = row.get("usage") or {}
        bucket["tokens_in"] += int(usage.get("prompt_tokens") or 0)
        bucket["tokens_out"] += int(usage.get("completion_tokens") or 0)
    return per_route
