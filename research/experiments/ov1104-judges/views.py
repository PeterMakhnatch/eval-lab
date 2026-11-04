"""Normalized run views for the ov1104 judges (DetBench view format) and their text renderings.

A view is the DetBench contract's JSON: ``{"run_key", "task_text", "steps": [{"i", "role",
"text", "tool_calls": [{"name", "args"}], "observation"}], "final": {"reward", "diff"}}``.
Labels never enter a view. Reasoning content is never kept (agents' hidden reasoning is not an
action and leaks the agent's narration into every reader).

Two renderings feed the judges:

* ``full``: every step with its prose, tool calls and observations (what AutoReview-style readers
  see when they read the whole run);
* ``actions_only``: tool calls, commands, file writes and observations only, no agent prose.

Long runs are windowed by character budget; a judge scores each window and the run takes the
maximum window score.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

STEP_OBS_CHARS = 2500  # per-step observation budget (head + tail kept)
STEP_ARGS_CHARS = 6000  # per-tool-call argument budget (file writes keep this much content)
TASK_CHARS = 6000
DIFF_CHARS = 16000
WINDOW_CHARS = 60000

SHELL_ARG_KEYS = ("command", "cmd", "keystrokes", "code", "script")
_REASONING_KEYS = ("reasoning_content", "reasoning", "thinking")


def elide(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n[... {len(text) - limit} chars elided ...]\n{text[-tail:]}"


def _results_text(step: Mapping[str, Any]) -> str | None:
    observation = step.get("observation")
    if not isinstance(observation, Mapping):
        return None
    parts: list[str] = []
    for result in observation.get("results") or []:
        if not isinstance(result, Mapping):
            continue
        content = result.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif content is not None:
            parts.append(json.dumps(content, ensure_ascii=False))
        if result.get("is_error") is True:
            parts.append("[tool reported an error]")
    return "\n".join(parts) if parts else None


def _args_string(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    if isinstance(arguments, Mapping):
        for key in SHELL_ARG_KEYS:
            value = arguments.get(key)
            if isinstance(value, str) and len(arguments) <= 3:
                extra = {k: v for k, v in arguments.items() if k != key and k != "i"}
                return value if not extra else f"{value}\n{json.dumps(extra, ensure_ascii=False)}"
        return json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    return json.dumps(arguments, ensure_ascii=False)


def _text_of(step: Mapping[str, Any]) -> str | None:
    message = step.get("message")
    if isinstance(message, str):
        return message
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                part.get("text", "") for part in content if isinstance(part, Mapping) and part.get("type") == "text"
            )
    return None


def atif_to_view(run_key: str, steps: Iterable[Mapping[str, Any]], *, diff: str | None, reward: float | None,
                 task_text: str | None = None) -> dict[str, Any]:
    """An ATIF step list (``source``/``message``/``tool_calls``/``observation``) to a DetBench view."""
    out_steps: list[dict[str, Any]] = []
    preamble: list[str] = []
    for i, step in enumerate(steps):
        source = str(step.get("source") or "agent")
        role = {"agent": "agent", "assistant": "agent", "user": "user", "system": "system", "tool": "tool"}.get(
            source, "agent"
        )
        text = _text_of(step)
        calls = []
        for call in step.get("tool_calls") or []:
            if not isinstance(call, Mapping):
                continue
            name = str(call.get("function_name") or call.get("name") or "tool")
            calls.append({"name": name, "args": _args_string(call.get("arguments"))})
        if role in ("user", "system") and text and all(s["role"] != "agent" for s in out_steps):
            preamble.append(text)
        out_steps.append({
            "i": i,
            "role": role,
            "text": text,
            "tool_calls": calls,
            "observation": _results_text(step),
        })
    if task_text is None and preamble:
        task_text = "\n\n".join(preamble)
    return {
        "run_key": run_key,
        "task_text": task_text,
        "steps": out_steps,
        "final": {"reward": reward, "diff": diff},
    }


def load_view(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# renderings
# --------------------------------------------------------------------------- #


def render_step(step: Mapping[str, Any], *, actions_only: bool) -> str | None:
    """One step as text, or None when the rendering has nothing to show for it."""
    role = step.get("role")
    lines: list[str] = []
    if role in ("user", "system"):
        if actions_only:
            return None  # the task text is given separately when a judge needs it
        text = step.get("text") or ""
        lines.append(f"[{role}] {elide(text, STEP_OBS_CHARS)}")
    else:
        text = step.get("text") or ""
        if text.strip() and not actions_only:
            lines.append(f"[agent says] {elide(text, STEP_OBS_CHARS)}")
    for call in step.get("tool_calls") or []:
        lines.append(f"[tool call: {call.get('name')}]\n{elide(call.get('args') or '', STEP_ARGS_CHARS)}")
    observation = step.get("observation")
    if observation:
        lines.append(f"[tool output]\n{elide(observation, STEP_OBS_CHARS)}")
    if not lines:
        return None
    return "\n".join(lines)


def render_windows(view: Mapping[str, Any], *, actions_only: bool, window_chars: int = WINDOW_CHARS) -> list[dict[str, Any]]:
    """Consecutive step windows under the character budget: ``[{"steps": [i...], "text": str}]``."""
    windows: list[dict[str, Any]] = []
    current: list[tuple[int, str]] = []
    size = 0
    for step in view.get("steps") or []:
        rendered = render_step(step, actions_only=actions_only)
        if rendered is None:
            continue
        block = f"=== step {step['i']} ===\n{rendered}\n"
        if current and size + len(block) > window_chars:
            windows.append({"steps": [i for i, _ in current], "text": "".join(b for _, b in current)})
            current, size = [], 0
        current.append((step["i"], block))
        size += len(block)
    if current:
        windows.append({"steps": [i for i, _ in current], "text": "".join(b for _, b in current)})
    if not windows:
        windows.append({"steps": [], "text": "(no steps could be rendered for this run)"})
    return windows


def task_block(view: Mapping[str, Any]) -> str:
    text = view.get("task_text") or "(no task statement recovered)"
    return elide(text, TASK_CHARS)


def diff_block(view: Mapping[str, Any]) -> str:
    diff = (view.get("final") or {}).get("diff")
    if not diff:
        return "(no final diff available)"
    return elide(diff, DIFF_CHARS)


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def first_json_object(text: str) -> dict[str, Any] | None:
    """The first JSON object in a reply (tolerates code fences and prose around it)."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?|```$", "", stripped, flags=re.MULTILINE).strip()
    for candidate in (stripped, *(m.group(0) for m in _JSON_OBJECT.finditer(stripped))):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    # last resort: shrink from the right until it parses
    start = stripped.find("{")
    if start < 0:
        return None
    for end in range(len(stripped), start, -1):
        if stripped[end - 1] != "}":
            continue
        try:
            value = json.loads(stripped[start:end])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None
