"""Faithful port of CheatBench's core/render.py.

Source: /private/tmp/cheatbench-src/core/render.py at pinned commit
4d1a82543f93edd0e4e5a2651192688ba609c1e7 (copy ``rubric-judge.md`` next to
this file is the verbatim prompts/judge.md the prompt template is read from).

What changed in the port, in full:
  * Operates on plain ``dict`` objects parsed from Harbor's
    ``agent/trajectory.json`` (ATIF) instead of CheatBench's pydantic
    ``Trajectory`` / ``ContentPart`` classes. Every field access is
    ``dict.get`` with the same key names, so the rendered shape is identical.
  * Adds ``load_atif`` (read ``trajectory.json`` plus ``trajectory.cont-N.json``
    continuations and concatenate their steps) and ``render_trial`` (load +
    render one Harbor trial dir). The line format itself is untouched.

What did NOT change: the ``[init]`` / ``THINKING`` / ``ASSISTANT`` /
``[N] TOOL_CALL`` / ``-> RESULT`` / ``FINAL MESSAGE`` shape, the per-tool
priority key order, the no-truncation rule (only base64-looking runs of
200+ chars are elided and blank runs collapsed), and image-payload elision.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Renderer identity recorded on every judge row so a re-render after a
# renderer change can never be confused with an old one.
RENDERER_VERSION = "cheatbench-port/render-v1"

# Anything that looks like a base64 payload (a long unbroken run of the
# alphabet) is worthless to a reader and expensive to send.
_B64_RUN = re.compile(r"[A-Za-z0-9+/=]{200,}")

# The argument that decides a verdict goes first, so a reader (and the judge)
# sees `command=` before a wall of options. Keyed by tool name across every
# adapter. This is a presentation hint, not adapter dispatch -- a name that is
# not in the table simply renders in its natural key order.
# Harbor/Terminus-2 additions: `bash_command` (keystrokes) and the
# `mark_task_complete` / `view_images` tools. Original CheatBench entries kept.
_PRIORITY = {
    # claude-sdk
    "WebFetch": ["url", "prompt"],
    "WebSearch": ["query"],
    "Bash": ["command", "description"],
    "Read": ["file_path"],
    "Write": ["file_path", "content"],
    "Grep": ["pattern", "path"],
    "Glob": ["pattern", "path"],
    # codex
    "shell": ["command", "workdir"],
    "exec": ["cmd", "command", "input", "workdir"],
    "unified_exec": ["cmd", "command", "input", "workdir"],
    "local_shell": ["command", "workdir"],
    "apply_patch": ["input", "patch"],
    "web_search": ["query", "queries", "url"],
    "web_search_call": ["query", "queries", "url"],
    # harbor terminus-2
    "bash_command": ["keystrokes", "duration"],
    "mark_task_complete": ["summary", "result"],
    "view_images": ["paths", "path"],
}


def scrub(text: Any) -> str:
    """Elide base64 blobs and collapse blank runs. Text is never truncated."""
    if not isinstance(text, str):
        text = str(text)
    text = _B64_RUN.sub(
        lambda m: f"<base64 blob, {len(m.group(0))} chars, elided>", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def tool_input(name: str, inp: Any) -> str:
    """Tool arguments, with the fields that decide the verdict shown first."""
    if not isinstance(inp, dict):
        return scrub(inp)
    priority = _PRIORITY.get(name, [])
    keys = [k for k in priority if k in inp] + [k for k in inp if k not in priority]
    parts = []
    for k in keys:
        v = inp[k]
        v = v if isinstance(v, str) else json.dumps(v, default=str)
        parts.append(f"{k}={scrub(v)}")
    return "  ".join(parts) if parts else "{}"


def _content_text(item: Any) -> str:
    # ATIF multimodal parts, which is what a converted trajectory holds.
    if isinstance(item, dict):
        if item.get("type") == "image":
            return "<image returned to the model, elided>"
        if "text" in item:
            return str(item["text"])
        return json.dumps(item, default=str)
    text = getattr(item, "text", None)
    if text is not None or hasattr(item, "type"):
        return text or "" if getattr(item, "type", None) == "text" \
            else f"<{getattr(item, 'type', '?')} returned to the model, elided>"
    return str(item)


def tool_result(content: Any) -> str:
    """What came back, flattened. Image payloads are named, never sent."""
    if isinstance(content, list):
        content = "\n".join(_content_text(item) for item in content)
    return scrub(content)


def message_text(message: Any) -> str:
    """An ATIF `Step.message`, which is a string or a list of ContentParts."""
    if message is None:
        return ""
    if isinstance(message, str):
        return message
    chunks = []
    for part in message:
        if isinstance(part, dict):
            chunks.append(part.get("text")
                          or f"<{part.get('type')} content, elided>")
        else:
            text = getattr(part, "text", None)
            if getattr(part, "type", None) == "text":
                chunks.append(text or "")
            elif hasattr(part, "type"):
                src = getattr(part, "source", None)
                path = getattr(src, "path", "?") if src else "?"
                chunks.append(f"<{part.type}: {path}>")
            else:
                chunks.append(str(part))
    return "\n".join(c for c in chunks if c)


def render_steps(traj: dict, *, start_seq: int = 0) -> list[str]:
    """The step lines of a trajectory, without the `[init]`/`FINAL` envelope."""
    lines: list[str] = []
    seq = start_seq
    names: dict[str, str] = {}
    prev_ctx: tuple | None = None

    for step in traj.get("steps") or []:
        # Model / effort, announced only when it CHANGES.
        if step.get("source") == "agent":
            ctx = (step.get("model_name"), step.get("reasoning_effort"))
            if any(c is not None for c in ctx):
                if prev_ctx is not None and ctx != prev_ctx:
                    lines.append(f"[context] model={ctx[0]} "
                                 f"reasoning_effort={ctx[1]}")
                prev_ctx = ctx

        # Extended thinking is its OWN line and is never folded into the
        # message: the judge reads what the model said and what it privately
        # reasoned as different evidence.
        reasoning = step.get("reasoning_content")
        if reasoning and str(reasoning).strip():
            lines.append(f"THINKING: {scrub(str(reasoning).strip())}")

        text = message_text(step.get("message")).strip()
        if text:
            if step.get("source") == "agent":
                lines.append(f"ASSISTANT: {scrub(text)}")
            else:
                lines.append(f"    -> USER/SYSTEM[{step.get('source')}]: {scrub(text)}")

        for call in step.get("tool_calls") or []:
            call_id = call.get("tool_call_id") or ""
            func = call.get("function_name") or call.get("name") or "tool"
            names[call_id] = func
            lines.append(f"[{seq}] TOOL_CALL {func}: "
                         f"{tool_input(func, call.get('arguments'))}")
            seq += 1

        observation = step.get("observation")
        if observation:
            for result in observation.get("results") or []:
                name = names.get(result.get("source_call_id") or "", "tool")
                err = " (ERROR)" if (result.get("extra") or {}).get("is_error") else ""
                lines.append(f"    -> RESULT[{name}]{err}: "
                             f"{tool_result(result.get('content'))}")

    return lines


def render_trajectory(traj: dict) -> str:
    """A whole ATIF trajectory as the judge's ordered log. The ONLY renderer."""
    agent = traj.get("agent") or {}
    init = [f"agent={agent.get('name', '?')}", f"version={agent.get('version', '?')}"]
    if agent.get("model_name"):
        init.append(f"model={agent.get('model_name')}")
    for key, value in (agent.get("extra") or {}).items():
        init.append(f"{key}={value}")
    lines = ["[init] " + " ".join(init)]

    lines += render_steps(traj)

    # The agent's closing answer, when its runtime reports one separately from
    # the last assistant step. ATIF has no dedicated field for it, so it lives
    # in the root `extra`.
    final = (traj.get("extra") or {}).get("final_text")
    if final:
        lines.append(f"FINAL MESSAGE: {scrub(str(final))}")

    return "\n".join(lines)


def load_atif(agent_dir: Path) -> dict:
    """Load ``trajectory.json`` plus any ``trajectory.cont-N.json`` parts.

    Continuation files repeat the trajectory envelope; only their ``steps``
    are appended, in numeric suffix order. Top-level fields come from the
    base file.
    """
    base = Path(agent_dir) / "trajectory.json"
    if not base.exists():
        raise FileNotFoundError(f"{base} not found -- is this a Harbor trial dir?")
    traj = json.loads(base.read_text())
    conts = sorted(
        Path(agent_dir).glob("trajectory.cont-*.json"),
        key=lambda p: p.name,
    )
    steps = list(traj.get("steps") or [])
    for cont in conts:
        part = json.loads(cont.read_text())
        if isinstance(part, list):
            steps.extend(part)
        else:
            steps.extend(part.get("steps") or [])
    traj["steps"] = steps
    return traj


def render_trial(trial_dir: Path) -> tuple[str, dict]:
    """Render one Harbor trial dir. Returns (log, atif dict)."""
    trial_dir = Path(trial_dir)
    direct = trial_dir if (trial_dir / "trajectory.json").exists() \
        else trial_dir / "agent"
    traj = load_atif(direct)
    return render_trajectory(traj), traj
