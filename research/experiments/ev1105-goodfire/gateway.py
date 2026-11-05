"""Route-locked google-antigravity/* gateway runner for ev1105-goodfire readings.

Enforces ``--config /private/tmp/ev1105/shared/nofallback.yml`` and
``PI_CONFIG_FILES=/private/tmp/ev1105/shared/nofallback.yml`` on every
``omp auth-gateway stdio`` process, verifies that only ``google-antigravity/*``
routes are requested, and rejects any response whose ``model_served`` differs
from the requested route.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

NOFALLBACK_CONFIG = Path("/private/tmp/ev1105/shared/nofallback.yml")
ALLOWED_PREFIX = "google-antigravity/"


async def start_gateway_with_retry(
    config_path: Path = NOFALLBACK_CONFIG,
    max_tries: int = 12,
) -> asyncio.subprocess.Process:
    """Spawn ``omp auth-gateway stdio`` with no-fallback config and SQLite busy retries."""
    env = os.environ.copy()
    env["PI_CONFIG_FILES"] = str(config_path)
    for attempt in range(1, max_tries + 1):
        proc = await asyncio.create_subprocess_exec(
            "omp",
            "--config",
            str(config_path),
            "auth-gateway",
            "stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=1 << 25,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
        if line.strip():
            ready = json.loads(line.decode("utf-8", errors="replace"))
            if ready.get("ready"):
                return proc
        err = (await proc.stderr.read()).decode("utf-8", errors="replace")
        proc.kill()
        await proc.wait()
        if attempt == max_tries:
            raise RuntimeError(f"Failed to start omp auth-gateway stdio: {err[:240]}")
        await asyncio.sleep(1.5 * attempt)
    raise RuntimeError("Could not start omp auth-gateway stdio")


def verify_route_allowed(model: str) -> None:
    """Raise ValueError if ``model`` is not a ``google-antigravity/*`` route."""
    if not model.startswith(ALLOWED_PREFIX):
        raise ValueError(
            f"Route {model!r} is forbidden for lane goodfire; must start with {ALLOWED_PREFIX!r}"
        )


def parse_first_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from model output text."""
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(text[idx:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        idx = text.find("{", idx + 1)
    return None


async def run_reading_batch(
    *,
    batch_name: str,
    out_dir: Path,
    calls_log: Path,
    model: str,
    sys_prompt: str,
    packets: list[dict[str, Any]],
    temperature: float = 0.0,
    seed: int | None = None,
    concurrency: int = 12,
    config_path: Path = NOFALLBACK_CONFIG,
) -> int:
    """Execute a resumable reading batch over ``packets`` on ``model``."""
    verify_route_allowed(model)
    out_dir.mkdir(parents=True, exist_ok=True)
    todo_packets: list[dict[str, Any]] = []
    for pkt in packets:
        rk_hash = hashlib.sha1(str(pkt["run_key"]).encode("utf-8")).hexdigest()[:16]
        out_path = out_dir / f"{rk_hash}.json"
        if out_path.exists():
            try:
                existing = json.loads(out_path.read_text(encoding="utf-8"))
                if existing.get("parsed") and existing.get("model_served") == model:
                    continue
            except Exception:
                pass
        todo_packets.append(pkt)

    if not todo_packets:
        return 0

    proc = await start_gateway_with_retry(config_path=config_path)
    stdin = proc.stdin
    stdout = proc.stdout
    assert stdin is not None
    assert stdout is not None
    write_lock = asyncio.Lock()
    log_lock = asyncio.Lock()
    pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def reader_loop() -> None:
        while True:
            raw = await stdout.readline()
            if not raw:
                for fut in pending.values():
                    if not fut.done():
                        fut.set_exception(RuntimeError("Gateway EOF"))
                break
            msg = json.loads(raw.decode("utf-8", errors="replace"))
            req_id = str(msg.get("id") or "")
            fut = pending.pop(req_id, None)
            if fut and not fut.done():
                fut.set_result(msg)

    reader_task = asyncio.create_task(reader_loop())
    sem = asyncio.Semaphore(concurrency)
    completed = 0

    async def call_one(idx: int, pkt: dict[str, Any]) -> None:
        nonlocal completed
        rk = str(pkt["run_key"])
        rk_hash = hashlib.sha1(rk.encode("utf-8")).hexdigest()[:16]
        out_path = out_dir / f"{rk_hash}.json"
        async with sem:
            for attempt in range(3):
                req_id = f"{batch_name}-{idx}-{attempt}"
                body: dict[str, Any] = {
                    "model": model,
                    "temperature": temperature,
                    "max_tokens": 1024,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": pkt["text"]},
                    ],
                }
                if seed is not None:
                    body["seed"] = seed
                req = {"id": req_id, "path": "/v1/chat/completions", "body": body}
                loop = asyncio.get_running_loop()
                fut: asyncio.Future[dict[str, Any]] = loop.create_future()
                pending[req_id] = fut
                t0 = time.monotonic()
                async with write_lock:
                    stdin.write((json.dumps(req) + "\n").encode("utf-8"))
                    await stdin.drain()
                resp = await asyncio.wait_for(fut, timeout=180)
                dt = round(time.monotonic() - t0, 3)
                status = resp.get("status")
                rbody = resp.get("body") or {}
                served = rbody.get("model") if isinstance(rbody, dict) else None
                usage = rbody.get("usage") if isinstance(rbody, dict) else None

                async with log_lock:
                    with calls_log.open("a", encoding="utf-8") as handle:
                        handle.write(
                            json.dumps(
                                {
                                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                    "batch": batch_name,
                                    "run_key": rk,
                                    "model_requested": model,
                                    "model_served": served,
                                    "status": status,
                                    "latency_s": dt,
                                    "usage": usage,
                                }
                            )
                            + "\n"
                        )

                if status != 200 or served != model:
                    raise RuntimeError(
                        f"Route violation or error on {rk}: status={status}, "
                        f"requested={model}, served={served}"
                    )

                content = rbody["choices"][0]["message"]["content"]
                if isinstance(content, list):
                    content = "".join(
                        part.get("text", "") if isinstance(part, dict) else str(part)
                        for part in content
                    )
                parsed = parse_first_json_object(str(content))
                if parsed and parsed.get("classification") in (
                    "executed-shortcut",
                    "intent-or-narration-only",
                    "no-evidence",
                ):
                    out_obj = {
                        "batch": batch_name,
                        "run_key": rk,
                        "model_requested": model,
                        "model_served": served,
                        "latency_s": dt,
                        "usage": usage,
                        "parsed": parsed,
                        "raw": content,
                    }
                    out_path.write_text(
                        json.dumps(out_obj, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    completed += 1
                    return
            raise RuntimeError(f"Failed to parse valid JSON classification for {rk}")

    try:
        await asyncio.gather(*(call_one(i, p) for i, p in enumerate(todo_packets)))
    finally:
        reader_task.cancel()
        proc.kill()
        await proc.wait()
    return completed
