"""Smoke-test the deployed MiMo SGLang server (HAR-90). Stdlib only.

Reads ``EVALLAB_MIMO_SELFHOSTED_UPSTREAM`` (the Modal server URL) and
``MIMO_SELFHOSTED_API_KEY`` from the environment; never prints the key.
It measures:

- cold start: seconds from the first request until ``/health`` answers 200
  (a scaled-to-zero Modal Server answers 503 while its container boots);
- reasoning split: with ``enable_thinking`` the reply carries
  ``reasoning_content`` and a ``content`` free of ``<think>``;
- time to first token and decode tokens/s at 1 and 8 concurrent streams.

Usage::

    uv run --project tools/modal-mimo-serve --locked \\
        python tools/modal-mimo-serve/smoke.py --out runs/har90-modal-smoke/smoke.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

MODEL_ID = "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
SAMPLING = {"temperature": 0.6, "top_p": 0.95, "top_k": 20}
THINKING = {"chat_template_kwargs": {"enable_thinking": True}}


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _post(base: str, key: str, body: dict[str, Any], timeout: float) -> Any:
    request = urllib.request.Request(
        f"{base}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers=_headers(key),
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=timeout)


def wait_cold_start(base: str, key: str, budget_s: float) -> dict[str, Any]:
    started = time.monotonic()
    statuses: dict[str, int] = {}
    while time.monotonic() - started < budget_s:
        request = urllib.request.Request(f"{base}/health", headers=_headers(key))
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status == 200:
                    return {
                        "ready_after_s": round(time.monotonic() - started, 1),
                        "non_200_responses": statuses,
                    }
        except urllib.error.HTTPError as exc:
            statuses[str(exc.code)] = statuses.get(str(exc.code), 0) + 1
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            statuses["transport"] = statuses.get("transport", 0) + 1
        time.sleep(5)
    raise TimeoutError(f"server not healthy within {budget_s}s: {statuses}")


def reasoning_split(base: str, key: str) -> dict[str, Any]:
    body = {
        "model": MODEL_ID,
        "messages": [{"role": "user", "content": "What is 15% of 240? Reply with the number."}],
        "max_tokens": 2048,
        **SAMPLING,
        **THINKING,
    }
    started = time.monotonic()
    with _post(base, key, body, timeout=300) as response:
        payload = json.load(response)
    message = payload["choices"][0]["message"]
    content = message.get("content") or ""
    reasoning = message.get("reasoning_content") or ""
    return {
        "wall_s": round(time.monotonic() - started, 2),
        "returned_model": payload.get("model"),
        "reasoning_chars": len(reasoning),
        "content": content[:200],
        "think_tag_in_content": "<think>" in content or "</think>" in content,
        "split_ok": bool(reasoning) and bool(content) and "<think>" not in content,
        "usage": payload.get("usage"),
    }


def terminus_json_turn(base: str, key: str) -> dict[str, Any]:
    """One Terminus-2-shaped turn: the reply content must be a JSON object."""
    system = (
        "You are an AI assistant solving command-line tasks in a Linux shell. "
        "Respond ONLY with a JSON object with keys: analysis (string), plan (string), "
        'commands (array of {"keystrokes": string, "duration": number}), '
        "task_complete (boolean)."
    )
    body = {
        "model": MODEL_ID,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": "Task: create /app/hello.txt containing 'hi'.\n"
                "Current terminal state:\nroot@box:/app# ",
            },
        ],
        "max_tokens": 4096,
        **SAMPLING,
        **THINKING,
    }
    with _post(base, key, body, timeout=300) as response:
        payload = json.load(response)
    message = payload["choices"][0]["message"]
    content = (message.get("content") or "").strip()
    text = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.loads(text)
        keys = sorted(parsed) if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        keys = None
    return {
        "json_object": keys is not None,
        "keys": keys,
        "reasoning_chars": len(message.get("reasoning_content") or ""),
        "usage": payload.get("usage"),
    }


def stream_once(base: str, key: str, prompt: str, max_tokens: int) -> dict[str, Any]:
    body = {
        "model": MODEL_ID,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "ignore_eos": True,
        "stream": True,
        "stream_options": {"include_usage": True},
        **SAMPLING,
        **THINKING,
    }
    started = time.monotonic()
    first = last = None
    usage = None
    with _post(base, key, body, timeout=600) as response:
        for raw in response:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            event = json.loads(line[5:])
            if event.get("usage"):
                usage = event["usage"]
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content") or delta.get("reasoning_content"):
                    now = time.monotonic()
                    first = first or now
                    last = now
    if first is None or last is None or not usage:
        raise RuntimeError("stream produced no tokens or no usage")
    completion = usage["completion_tokens"]
    return {
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": completion,
        "ttft_s": round(first - started, 3),
        "decode_tok_s": round((completion - 1) / (last - first), 1) if last > first else None,
        "wall_s": round(last - started, 2),
    }


def concurrent_streams(
    base: str, key: str, prompt: str, max_tokens: int, n: int
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            result = stream_once(base, key, prompt, max_tokens)
        except Exception as exc:  # recorded, not raised: one failure must not hide the rest
            with lock:
                errors.append(type(exc).__name__)
            return
        with lock:
            results.append(result)

    started = time.monotonic()
    threads = [threading.Thread(target=worker) for _ in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall = time.monotonic() - started
    total = sum(r["completion_tokens"] for r in results)
    return {
        "concurrency": n,
        "ok": len(results),
        "errors": errors,
        "ttft_s_median": statistics.median(r["ttft_s"] for r in results) if results else None,
        "decode_tok_s_per_stream_median": (
            statistics.median(r["decode_tok_s"] for r in results if r["decode_tok_s"])
            if results
            else None
        ),
        "aggregate_tok_s": round(total / wall, 1) if results else None,
        "wall_s": round(wall, 2),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--cold-start-budget-s", type=float, default=1200)
    args = parser.parse_args(argv)
    base = os.environ.get("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", "").rstrip("/")
    key = os.environ.get("MIMO_SELFHOSTED_API_KEY", "")
    if not base or not key:
        print("EVALLAB_MIMO_SELFHOSTED_UPSTREAM and MIMO_SELFHOSTED_API_KEY must be set")
        return 2

    report: dict[str, Any] = {"model": MODEL_ID, "server": base}
    report["cold_start"] = wait_cold_start(base, key, args.cold_start_budget_s)
    report["reasoning_split"] = reasoning_split(base, key)
    report["terminus_json_turn"] = terminus_json_turn(base, key)
    short = "Write a detailed essay about the history of the Unix shell."
    long_prompt = "Summarize the following log.\n" + "\n".join(
        f"line {i}: service=api status=200 latency_ms={i % 97}" for i in range(700)
    )
    report["c1_short"] = [stream_once(base, key, short, args.max_tokens) for _ in range(3)]
    report["c1_long_prefill"] = stream_once(base, key, long_prompt, args.max_tokens)
    report["c8_short"] = concurrent_streams(base, key, short, args.max_tokens, 8)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    json.dump(report, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
