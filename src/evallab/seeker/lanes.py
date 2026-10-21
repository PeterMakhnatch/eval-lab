"""The three seeker drivers: Sonnet and Codex through OMP, GPT-OSS-120b through OpenRouter.

All drivers get the same prompt, the same confined tool surface and one fresh
workspace per task. OMP lanes additionally run under the macOS sandbox profile.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.seeker.confine import TOOL_SCHEMAS, ConfinedTools, sandbox_profile
from evallab.seeker.prompt import SYSTEM_PROMPT, user_prompt

DRIVERS: dict[str, dict[str, str]] = {
    "sonnet": {"kind": "omp", "model": "anthropic/claude-sonnet-5-5"},
    "codex": {"kind": "omp", "model": "openai-codex/gpt-6-luna"},
    "gptoss": {"kind": "openrouter", "model": "openai/gpt-oss-120b"},
}
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# OpenRouter list prices observed 2026-10-21, USD per token.
GPT_OSS_PRICES = {"prompt": 0.15e-6, "completion": 0.60e-6}
CODEX_QUOTA_CEILING = 0.73
MAX_TOOL_CALLS = 48


def _now() -> str:
    return datetime.now(UTC).isoformat()


def codex_weekly_used() -> float | None:
    """Account-wide 7-day Codex used fraction from `omp usage` (None if unreadable)."""
    try:
        out = subprocess.run(
            ["omp", "usage", "--json", "--redact", "-p", "openai-codex"],
            capture_output=True, text=True, timeout=120, check=False,
        ).stdout
        doc = json.loads(out)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    used = [
        float(limit["amount"]["usedFraction"])
        for report in doc.get("reports", [])
        if report.get("provider") == "openai-codex"
        for limit in report.get("limits", [])
        if (limit.get("window") or {}).get("id") == "7d"
    ]
    return max(used) if used else None


def check_codex_quota() -> float:
    used = codex_weekly_used()
    if used is None:
        raise RuntimeError("Codex weekly quota reading unavailable; refusing Codex lane")
    if used >= CODEX_QUOTA_CEILING:
        raise RuntimeError(f"Codex weekly quota {used:.0%} >= {CODEX_QUOTA_CEILING:.0%} ceiling")
    return used


def _omp_result(stdout: str) -> tuple[str, dict[str, int], float, list[str]]:
    """Final text, summed usage, API-equivalent cost and tool calls of an OMP run."""
    text, cost = "", 0.0
    usage: dict[str, int] = defaultdict(int)
    commands: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "tool_execution_start":
            args = {k: v for k, v in (event.get("args") or {}).items() if k not in ("i", "intent")}
            commands.append(f"{event.get('toolName')}: {json.dumps(args, sort_keys=True)[:300]}")
            continue
        if event.get("type") != "agent_end":
            continue
        for message in event.get("messages") or []:
            if message.get("role") != "assistant":
                continue
            raw = message.get("usage") or {}
            for key in ("input", "output", "cacheRead", "cacheWrite"):
                if isinstance(raw.get(key), int):
                    usage[key] += raw[key]
            cost += float(((raw.get("cost") or {}).get("total")) or 0.0)
            parts = [p.get("text", "") for p in message.get("content") or [] if p.get("type") == "text"]
            if any(parts):
                text = "".join(parts)
    return text, dict(usage), round(cost, 6), commands


def run_omp_lane(driver: str, workspace: Path, task: str, *, timeout: float = 900.0) -> dict[str, Any]:
    model = DRIVERS[driver]["model"]
    profile = workspace.parent / f"{workspace.name}.sb"
    profile.write_text(sandbox_profile(workspace))
    argv = [
        "sandbox-exec", "-f", str(profile),
        "omp", "-p", "--no-session", "--no-lsp",
        "--model", model, "--thinking", "medium",
        "--mode", "json", "--cwd", str(workspace),
        "--tools", "read,grep,glob,write",
        "--system-prompt", SYSTEM_PROMPT,
        user_prompt(task),
    ]
    started = time.monotonic()
    record: dict[str, Any] = {
        "driver": driver, "model": model, "task": task, "started_at": _now(),
        "workspace": str(workspace),
    }
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        text, usage, cost, commands = _omp_result(proc.stdout)
        record.update(
            text=text[:2000], usage=usage, api_equivalent_usd=cost, paid_usd=0.0,
            tool_calls=commands, error=None if proc.returncode == 0 else f"omp exit {proc.returncode}",
        )
        (workspace / "transcript.jsonl").write_text(proc.stdout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        record.update(text="", usage={}, api_equivalent_usd=0.0, paid_usd=0.0,
                      tool_calls=[], error=f"{type(exc).__name__}: {exc}")
    record["seconds"] = round(time.monotonic() - started, 1)
    record["finished_at"] = _now()
    return record


def _openrouter_post(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"OpenRouter HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}") from exc


def run_openrouter_lane(
    workspace: Path,
    task: str,
    *,
    timeout: float = 900.0,
    post: Callable[[dict[str, Any], str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key and post is None:
        raise RuntimeError("OPENROUTER_API_KEY missing; run under `keys run --`")
    post = post or _openrouter_post
    tools = ConfinedTools(workspace)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt(task)},
    ]
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    calls: list[str] = []
    started = time.monotonic()
    record: dict[str, Any] = {
        "driver": "gptoss", "model": DRIVERS["gptoss"]["model"], "task": task,
        "started_at": _now(), "workspace": str(workspace),
    }
    error: str | None = None
    finished = False
    try:
        for _ in range(MAX_TOOL_CALLS):
            if time.monotonic() - started > timeout:
                error = "wall-clock timeout"
                break
            reply = post(
                {
                    "model": DRIVERS["gptoss"]["model"],
                    "messages": messages,
                    "tools": TOOL_SCHEMAS,
                    "tool_choice": "auto",
                    "temperature": 0.2,
                    "max_tokens": 4096,
                },
                api_key,
            )
            raw_usage = reply.get("usage") or {}
            usage["prompt_tokens"] += int(raw_usage.get("prompt_tokens") or 0)
            usage["completion_tokens"] += int(raw_usage.get("completion_tokens") or 0)
            message = reply["choices"][0]["message"]
            messages.append(message)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                # Cheap models often stop after reading; nudge them to write the deliverables.
                nudges = record.get("nudges", 0)
                if nudges < 2 and not (workspace / "submission/solve.sh").is_file():
                    record["nudges"] = nudges + 1
                    messages.append(
                        {
                            "role": "user",
                            "content": "You have not written submission/solve.sh and submission/notes.json yet. "
                            "Write both now with write_file, then call finish.",
                        }
                    )
                    continue
                break
            for call in tool_calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                calls.append(f"{name}: {json.dumps(args, sort_keys=True)[:300]}")
                result = tools.call(name, args)
                messages.append(
                    {"role": "tool", "tool_call_id": call.get("id", ""), "content": result[:30_000]}
                )
                if name == "finish":
                    finished = True
            if finished:
                break
    except (RuntimeError, KeyError, IndexError, TypeError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    cost = usage["prompt_tokens"] * GPT_OSS_PRICES["prompt"] + usage["completion_tokens"] * GPT_OSS_PRICES["completion"]
    record.update(
        text="", usage=usage, api_equivalent_usd=round(cost, 6), paid_usd=round(cost, 6),
        tool_calls=calls, error=error, finished=finished,
    )
    record["seconds"] = round(time.monotonic() - started, 1)
    record["finished_at"] = _now()
    return record
