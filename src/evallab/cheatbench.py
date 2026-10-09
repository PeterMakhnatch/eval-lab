"""CheatBench as a calibration source for ``evallab detectors``.

Source: steinad/CheatBench on Hugging Face (MIT license), pinned to
``REVISION``. Only opaque row metadata (ids, labels, mechanisms, benchmarks,
localization spans) is committed to the repo (``cb_subset.json`` plus the
``detectors-v1`` manifest rows); trace bodies are materialized outside git
with ``materialize`` into ``<cb-root>/cb/<trace_id>/``.

Trace envelopes (all observed in the pinned revision):

* TB2 ``raw_trace`` (``task_name``/``benchmark``/``config``/``events``/
  ``files``/``result``): typed ``agent_command`` (Pilot JSONL session logs
  with Anthropic ``tool_use``/``tool_result`` blocks, or plain stdout),
  ``agent_log``, ``agent_episode`` (``Content:`` + ``Tool Calls:`` JSON), or
  ``{step, src, msg[, tools, obs]}`` dicts (``tools`` is JSON
  ``[{fn, cmd}]``; ``obs``/``msg`` may be unresolvable ``$NN`` refs, kept
  verbatim).
* ``cheatbench.raw_trace.v1`` messages form and IQuest ``{id, messages}``:
  ``{role, content}`` lists. Tool calls are recovered from harness-specific
  encodings: IQuest ``<function=..>`` XML, CyBench ``shell: {json}`` blocks,
  Codex ``[action] {json}`` blocks, OpenHands ``[NNNN] role=`` segments with
  ``TOOL_CALL[i]`` lines, MLE-Bench ``LLM_OUTPUT:`` tool JSON. SWE-agent and
  KernelBench exports carry no machine-readable tool calls and convert as
  message-only steps (recorded in stats).
* HAL-USACO weave ``litellm.acompletion`` spans: python-repr ``inputs`` /
  ``output`` dicts parsed with ``ast.literal_eval``.

Rows that cannot be converted are excluded with a recorded reason
(``no_agent_content``, ``weave_unparseable``, ``unknown_envelope``) and
counted; nothing is silently dropped.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

DATASET = "steinad/CheatBench"
REVISION = "461360b34e93644955563dcc9243c6cc1a53bd0f"
LICENSE = "MIT"
USER_AGENT = "eval-lab-cheatbench"

MSG_CAP = 8_000
OBS_CAP = 50_000

EXCLUDED_REASONS = ("no_agent_content", "weave_unparseable", "unknown_envelope")


# ---------------------------------------------------------------------------
# small parsing helpers


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]"


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
            elif isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block.get("content"), str):
                parts.append(block["content"])
            else:
                parts.append(json.dumps(block)[:2000])
        return "\n".join(parts)
    return json.dumps(content)[:4000] if content is not None else ""


def _balanced_json(text: str, start: int) -> tuple[Any, int] | None:
    """Parse one JSON value starting at ``start`` (object or array)."""
    decoder = json.JSONDecoder()
    try:
        obj, end = decoder.raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return obj, start + end


def _pilot_text_of(blocks: Any) -> str:
    out = []
    if not isinstance(blocks, list):
        return str(blocks) if blocks else ""
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            out.append(block["text"])
        elif block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
            out.append("[thinking] " + block["thinking"][:2000])
    return "\n".join(out)


def _pilot_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
            elif isinstance(block.get("content"), str):
                parts.append(block["content"])
            else:
                parts.append(json.dumps(block)[:2000])
        return "\n".join(parts)
    return json.dumps(content)[:4000] if content is not None else ""


def _pilot_steps(stdout: str) -> list[tuple[str, list[dict], list[dict]]]:
    """Parse a Pilot JSONL session log into (message, calls, results) triples."""
    triples: list[tuple[str, list[dict], list[dict]]] = []
    pending: tuple[str, list[dict], list[dict]] | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        otype = obj.get("type")
        msg = obj.get("message") or {}
        if otype == "assistant" and isinstance(msg, dict):
            if pending is not None:
                triples.append(pending)
            content = msg.get("content") or []
            text = _pilot_text_of(content) if isinstance(content, list) else str(content)
            calls: list[dict] = []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = str(block.get("name") or "bash")
                        raw = block.get("input") if isinstance(block.get("input"), dict) else {}
                        call_id = str(block.get("id") or f"call-{len(calls)}")
                        args = _pilot_tool_args(name, raw)
                        calls.append(
                            {
                                "tool_call_id": call_id,
                                "function_name": name,
                                "arguments": args,
                            }
                        )
            pending = (text, calls, [])
        elif otype == "user" and isinstance(msg, dict) and pending is not None:
            content = msg.get("content") or []
            blocks = content if isinstance(content, list) else [content]
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    pending[2].append(
                        {
                            "source_call_id": str(block.get("tool_use_id") or "unknown"),
                            "content": _cap(_pilot_result_text(block.get("content")), OBS_CAP),
                        }
                    )
    if pending is not None:
        triples.append(pending)
    return triples


def _pilot_tool_args(name: str, raw: dict) -> dict:
    """Tool-call arguments preserving both detectors text and path fields."""
    args: dict[str, Any] = {}
    lname = name.lower()
    if lname in ("bash", "bash_command", "shell", "exec"):
        cmd = raw.get("command")
        if isinstance(cmd, str) and cmd:
            args = {"keystrokes": cmd, "command": cmd}
        else:
            args = {"input": json.dumps(raw)[:4000]}
    else:
        for field in ("file_path", "path", "filename", "file", "command", "cmd", "input"):
            value = raw.get(field)
            if isinstance(value, str) and value:
                args[field] = value[:4000]
                if field in ("command", "cmd"):
                    args.setdefault("keystrokes", value[:4000])
        for field in ("old_str", "new_str", "old_string", "new_string"):
            value = raw.get(field)
            if isinstance(value, str) and value:
                args[field] = value[:4000]
        if not args:
            args = {"input": json.dumps(raw)[:4000]}
        elif "keystrokes" not in args and lname in ("bash",):
            pass
    for key in ("description",):
        if isinstance(raw.get(key), str):
            args[key] = raw[key][:500]
    return args


# ---------------------------------------------------------------------------
# step builder


class _Steps:
    def __init__(self, model: str = "") -> None:
        self.steps: list[dict[str, Any]] = []
        self.model = model
        self.n_tool_calls = 0
        self.n_dropped_obs = 0

    def user(self, text: str) -> None:
        text = text.strip()
        if text:
            self.steps.append(
                {"step_id": len(self.steps) + 1, "source": "user", "message": _cap(text, MSG_CAP)}
            )

    def agent(
        self,
        text: str,
        calls: list[dict] | None = None,
        results: list[dict] | None = None,
    ) -> None:
        text = text.strip()
        if not text and not calls and not results:
            return
        step: dict[str, Any] = {
            "step_id": len(self.steps) + 1,
            "source": "agent",
            "message": _cap(text, MSG_CAP) if text else "(tool calls only)",
        }
        if self.model:
            step["model_name"] = self.model
        if calls:
            step["tool_calls"] = calls
            self.n_tool_calls += len(calls)
        if results:
            step["observation"] = {"results": results}
        self.steps.append(step)

    def observe(self, text: str, call_id: str = "unknown") -> None:
        text = text.strip()
        if not text:
            return
        for step in reversed(self.steps):
            if step.get("source") == "agent":
                obs = step.setdefault("observation", {"results": []})
                obs["results"].append({"source_call_id": call_id, "content": _cap(text, OBS_CAP)})
                return
        self.n_dropped_obs += 1


# ---------------------------------------------------------------------------
# envelope A: TB2 raw_trace (typed events)


def _convert_agent_command(builder: _Steps, event: dict) -> bool:
    """An ``agent_command`` event; True when agent content was extracted."""
    stdout = event.get("stdout") or ""
    triples = _pilot_steps(stdout)
    if triples:
        for text, calls, results in triples:
            known = {c["tool_call_id"] for c in calls}
            fixed_calls = list(calls)
            for result in results:
                if result["source_call_id"] not in known:
                    known.add(result["source_call_id"])
                    fixed_calls.append(
                        {
                            "tool_call_id": result["source_call_id"],
                            "function_name": "unknown_tool",
                            "arguments": {},
                        }
                    )
            builder.agent(text, fixed_calls, results or None)
        return True
    command = event.get("command") or ""
    obs = ((event.get("stdout") or "") + "\n" + (event.get("stderr") or "")).strip()
    if not command and not obs:
        return False
    call_id = str(event.get("id") or f"cmd-{len(builder.steps)}")
    calls = (
        [
            {
                "tool_call_id": call_id,
                "function_name": "bash",
                "arguments": {
                    "keystrokes": command[:MSG_CAP],
                    "command": command[:MSG_CAP],
                },
            }
        ]
        if command
        else None
    )
    builder.agent(
        f"terminal command (exit {event.get('return_code')})" if command else "(output only)",
        calls,
        [{"source_call_id": call_id, "content": _cap(obs, OBS_CAP)}] if obs else None,
    )
    return True


def _episode_calls(response: Any) -> tuple[str, list[dict]]:
    """Split a Meta-Harness episode response into (content, tool calls)."""
    text = response if isinstance(response, str) else json.dumps(response)
    head, _, tail = text.partition("Tool Calls:")
    calls: list[dict] = []
    parsed = _balanced_json(tail.strip(), 0) if tail.strip() else None
    items = parsed[0] if parsed and isinstance(parsed[0], list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") or {}
        name = str(fn.get("name") or item.get("name") or "bash")
        raw_args = fn.get("arguments") or item.get("arguments") or ""
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            args = {}
        if isinstance(args, dict) and isinstance(args.get("commands"), list):
            for cmd in args["commands"]:
                if isinstance(cmd, dict) and isinstance(cmd.get("keystrokes"), str):
                    calls.append(
                        {
                            "tool_call_id": str(item.get("id") or f"ep-{len(calls)}"),
                            "function_name": "bash",
                            "arguments": {
                                "keystrokes": cmd["keystrokes"],
                                "command": cmd["keystrokes"],
                            },
                        }
                    )
        elif isinstance(args, dict):
            keys = args.get("command") or args.get("cmd") or args.get("keystrokes")
            calls.append(
                {
                    "tool_call_id": str(item.get("id") or f"ep-{len(calls)}"),
                    "function_name": name,
                    "arguments": {"keystrokes": str(keys), "command": str(keys)}
                    if isinstance(keys, str)
                    else {"input": json.dumps(args)[:4000]},
                }
            )
    return head.strip(), calls


def _msg_tool_calls(tools_raw: Any) -> list[dict]:
    """Parse ``{step,src,msg,tools,obs}`` tools JSON (``[{fn, cmd}]``)."""
    calls: list[dict] = []
    if isinstance(tools_raw, str):
        try:
            items = json.loads(tools_raw)
        except json.JSONDecodeError:
            return calls
    elif isinstance(tools_raw, list):
        items = tools_raw
    else:
        return calls
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("fn") or item.get("name") or "bash")
        cmd = item.get("cmd") or item.get("command") or item.get("input") or ""
        lname = name.lower()
        if lname in ("bash", "bash_command", "shell", "exec", "execute_bash"):
            args = {"keystrokes": str(cmd), "command": str(cmd)}
        else:
            args = {"input": str(cmd)[:4000]} if cmd else {}
            maybe_path = str(cmd) if cmd and ("/" in str(cmd) or "." in str(cmd)) else ""
            if maybe_path:
                args.setdefault("path", maybe_path[:2000])
        calls.append({"tool_call_id": f"t-{len(calls)}", "function_name": name, "arguments": args})
    return calls


# ---------------------------------------------------------------------------
# envelope B/C: messages form ({role, content} lists)


_IQUEST_FN_RE = re.compile(
    r"<function=([^>]+)>(.*?)</function>",
    re.DOTALL,
)
_IQUEST_PARAM_RE = re.compile(r"<parameter=([^>]+)>(.*?)</parameter>", re.DOTALL)


def _iquest_calls(text: str) -> tuple[str, list[dict]]:
    """Split IQuest assistant text into (prose, tool calls)."""
    calls: list[dict] = []

    def _one(match: re.Match) -> str:
        name = match.group(1).strip()
        params = {
            m.group(1).strip(): m.group(2).strip()
            for m in _IQUEST_PARAM_RE.finditer(match.group(2))
        }
        args: dict[str, Any] = {}
        for field in ("path", "file_path", "filename", "file", "command", "cmd", "code"):
            if params.get(field):
                args[field] = params[field][:4000]
        for field in ("old_str", "new_str", "old_text", "new_text"):
            if params.get(field):
                args[field] = params[field][:4000]
        command = params.get("command") or params.get("cmd") or params.get("code") or ""
        is_shell = name.lower() in ("bash", "exec", "shell") or (
            name == "str_replace_editor" and params.get("command") not in ("view",)
        )
        if command and is_shell:
            args.setdefault("keystrokes", command[:4000])
        if not args:
            args = {"input": json.dumps(params)[:4000]}
        calls.append(
            {
                "tool_call_id": f"iq-{len(calls)}",
                "function_name": name,
                "arguments": args,
            }
        )
        return ""

    prose = _IQUEST_FN_RE.sub(_one, text).strip()
    return prose, calls


_SHELL_JSON_RE = re.compile(r"\bshell\s*:")


def _cybench_calls(text: str) -> tuple[str, list[dict]]:
    """Split CyBench assistant text into (prose, shell tool calls)."""
    calls: list[dict] = []
    parts: list[str] = []
    pos = 0
    for match in _SHELL_JSON_RE.finditer(text):
        start = match.end()
        while start < len(text) and text[start] in " \t\n`":
            start += 1
        if start < len(text) and text[start] == "{":
            parsed = _balanced_json(text, start)
            if parsed and isinstance(parsed[0], dict):
                obj = parsed[0]
                cmd = obj.get("command", "")
                calls.append(
                    {
                        "tool_call_id": f"cy-{len(calls)}",
                        "function_name": "bash",
                        "arguments": {"keystrokes": str(cmd), "command": str(cmd)},
                    }
                )
                parts.append(text[pos : match.start()])
                pos = parsed[1]
    parts.append(text[pos:])
    return "".join(parts).strip().strip("`").strip(), calls


_CODEX_ACTION_RE = re.compile(r"^\[action\]", re.MULTILINE)


def _convert_codex(builder: _Steps, text: str) -> None:
    """Split a Codex ``[action] {json}`` transcript into steps."""
    matches = list(_CODEX_ACTION_RE.finditer(text))
    if not matches:
        builder.agent(text)
        return
    pending_prose: list[str] = []
    if matches[0].start() > 0:
        pending_prose.append(text[: matches[0].start()].strip())
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        chunk = text[match.end() : end]
        stripped = chunk.strip().lstrip("`").strip()
        parsed = _balanced_json(stripped, 0) if stripped[:1] == "{" else None
        rest = ""
        if parsed and isinstance(parsed[0], dict):
            obj = parsed[0]
            rest = stripped[parsed[1] :].strip().strip("`").strip()
            if "command" in obj:
                argv = obj["command"]
                if isinstance(argv, list):
                    argv = [str(a) for a in argv]
                    cmd = (
                        argv[2] if argv[:2] == ["bash", "-lc"] and len(argv) > 2 else " ".join(argv)
                    )
                else:
                    cmd = str(argv)
                if pending_prose:
                    builder.agent("\n".join(pending_prose))
                    pending_prose = []
                builder.agent(
                    cmd[:MSG_CAP],
                    [
                        {
                            "tool_call_id": f"cx-{len(builder.steps)}",
                            "function_name": "bash",
                            "arguments": {"keystrokes": cmd, "command": cmd},
                        }
                    ],
                )
            else:
                summary = obj.get("summary") or ""
                if isinstance(summary, list):
                    summary = "\n".join(str(s) for s in summary)
                if str(summary).strip():
                    pending_prose.append(str(summary).strip()[:2000])
                if rest:
                    pending_prose.append(rest[:2000])
        elif stripped:
            builder.observe(stripped)
        if rest and parsed and "command" in (parsed[0] or {}):
            builder.observe(rest)
    if pending_prose:
        builder.agent("\n".join(pending_prose))


_OH_SEG_RE = re.compile(r"^\[(\d+)\]\s+role=(\S+)(?:\s+name=(\S+))?", re.MULTILINE)
_OH_TOOLCALL_RE = re.compile(r"^TOOL_CALL\[(\d+)\]\s+(\S+?):\s*(.*)$", re.MULTILINE)


def _convert_openhands_flat(builder: _Steps, text: str) -> None:
    """Split an OpenHands ``[NNNN] role=`` flattened log into steps."""
    matches = list(_OH_SEG_RE.finditer(text))
    if not matches:
        builder.agent(text)
        return
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        role = match.group(2)
        name = match.group(3) or ""
        body = text[match.end() : end].strip()
        if role == "assistant":
            prose_parts: list[str] = []
            calls: list[dict] = []
            pos = 0
            for call in _OH_TOOLCALL_RE.finditer(body):
                prose_parts.append(body[pos : call.start()])
                tool, args = call.group(2), call.group(3).strip()
                calls.append(
                    {
                        "tool_call_id": f"oh-{match.group(1)}-{call.group(1)}",
                        "function_name": tool,
                        "arguments": {"keystrokes": args, "command": args},
                    }
                )
                pos = call.end()
            prose_parts.append(body[pos:])
            builder.agent("\n".join(p.strip() for p in prose_parts).strip(), calls or None)
        elif role == "tool":
            builder.observe(body, call_id=name or "unknown")
        elif body:
            builder.user(f"[{role}] {body[:MSG_CAP]}")


def _convert_mle(builder: _Steps, text: str) -> None:
    """Split MLE-Bench ``LLM_OUTPUT:`` / ``TOOL_OUTCOME:`` blocks into steps."""
    pattern = re.compile(r"^(LLM_OUTPUT|TOOL_OUTCOME):\s*$", re.MULTILINE)
    marks = list(pattern.finditer(text))
    if not marks:
        builder.agent(text)
        return
    if marks[0].start() > 0:
        builder.agent(text[: marks[0].start()])
    for idx, mark in enumerate(marks):
        end = marks[idx + 1].start() if idx + 1 < len(marks) else len(text)
        body = text[mark.end() : end].strip()
        if mark.group(1) == "TOOL_OUTCOME":
            builder.observe(body)
            continue
        parsed = _balanced_json(body, 0) if body[:1] == "{" else None
        if parsed and isinstance(parsed[0], dict):
            obj = parsed[0]
            name = str(obj.get("tool") or "unknown_tool")
            args = {
                k: (str(v)[:4000] if isinstance(v, str) else v)
                for k, v in obj.items()
                if k != "tool"
            }
            path = obj.get("filepath") or obj.get("file_path") or obj.get("path")
            if isinstance(path, str) and path:
                args.setdefault("file_path", path[:2000])
            builder.agent(
                body[:MSG_CAP],
                [
                    {
                        "tool_call_id": f"mle-{len(builder.steps)}",
                        "function_name": name,
                        "arguments": args,
                    }
                ],
            )
        else:
            builder.agent(body)


# ---------------------------------------------------------------------------
# envelope D: HAL-USACO weave spans


def _convert_weave(builder: _Steps, events: list[Any]) -> bool:
    """Convert weave ``litellm.acompletion`` spans; False when unparseable."""
    ok = False
    for event in events:
        if not isinstance(event, dict):
            continue
        try:
            inputs = ast.literal_eval(str(event.get("inputs") or ""))
            output = ast.literal_eval(str(event.get("output") or ""))
        except (SyntaxError, ValueError, MemoryError):
            return False
        if not isinstance(inputs, dict) or not isinstance(output, dict):
            return False
        messages = None
        if isinstance(inputs.get("messages"), list):
            messages = inputs["messages"]
        elif isinstance(inputs.get("kwargs"), dict) and isinstance(
            inputs["kwargs"].get("messages"), list
        ):
            messages = inputs["kwargs"]["messages"]
        if messages:
            for message in messages:
                if isinstance(message, dict) and message.get("role") == "user":
                    builder.user(_text_of(message.get("content")))
        try:
            content = output["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            content = ""
        builder.agent(_text_of(content))
        ok = True
    return ok


# ---------------------------------------------------------------------------
# top-level conversion


def convert(trace_id: str, raw: dict, meta: dict) -> tuple[dict, dict]:
    """Convert one CheatBench trace to ATIF.

    Returns ``(doc, stats)``; raises ``ValueError(reason)`` with a reason in
    ``EXCLUDED_REASONS`` when the row cannot be converted.
    """
    model = str(meta.get("model") or "")
    builder = _Steps(model=model)
    envelope = "unknown"
    extra: dict[str, Any] = {}

    if (
        isinstance(raw.get("events"), list)
        and raw.get("events")
        and isinstance(raw["events"][0], dict)
        and "type" in raw["events"][0]
    ):
        envelope = "tb2_typed"
        for event in raw["events"]:
            if not isinstance(event, dict):
                continue
            etype = event.get("type")
            if etype == "agent_command":
                _convert_agent_command(builder, event)
            elif etype == "agent_log":
                text = (event.get("text") or "").strip()
                if text:
                    builder.agent(text)
            elif etype == "agent_episode":
                prompt = event.get("prompt")
                if isinstance(prompt, str) and prompt.strip():
                    builder.observe(prompt)
                content, calls = _episode_calls(event.get("response"))
                if content or calls:
                    builder.agent(content, calls or None)
            elif isinstance(event, dict) and any(k in event for k in ("msg", "tools", "obs")):
                _convert_msg_event(builder, event)
    elif (
        isinstance(raw.get("events"), list)
        and raw.get("events")
        and isinstance(raw["events"][0], dict)
        and {"step", "src", "msg"} <= set(raw["events"][0])
    ):
        envelope = "tb2_step"
        for event in raw["events"]:
            if isinstance(event, dict):
                _convert_msg_event(builder, event)
    elif isinstance(raw.get("messages"), list):
        envelope = (
            "iquest"
            if raw.get("source") == "iquest" or _looks_iquest(raw["messages"])
            else "messages"
        )
        _convert_messages(builder, raw["messages"], envelope=envelope)
    elif (
        isinstance(raw.get("events"), list)
        and raw.get("events")
        and all(isinstance(e, dict) and "role" in e for e in raw["events"][:3])
    ):
        envelope = _classify_message_events(raw["events"])
        _convert_messages(builder, raw["events"], envelope=envelope)
    elif (
        isinstance(raw.get("events"), list)
        and raw.get("events")
        and isinstance(raw["events"][0], dict)
        and ("inputs" in raw["events"][0] or "outputs" in raw["events"][0])
    ):
        envelope = "weave"
        if not _convert_weave(builder, raw["events"]):
            raise ValueError("weave_unparseable")
    elif not raw.get("events") and not raw.get("messages"):
        raise ValueError("no_agent_content")
    else:
        raise ValueError("unknown_envelope")

    agent_steps = [s for s in builder.steps if s.get("source") == "agent"]
    if not agent_steps:
        raise ValueError("no_agent_content")
    bench = str(raw.get("benchmark") or meta.get("benchmark") or "")
    task = str(raw.get("task_name") or (raw.get("task") or "") or trace_id)
    if isinstance(raw.get("task"), dict):
        task = str(raw["task"].get("id") or raw["task"].get("name") or trace_id)
    builder.steps.insert(
        0,
        {"step_id": 0, "source": "user", "message": f"[{bench}] {task} (model: {model})"},
    )
    for idx, step in enumerate(builder.steps):
        step["step_id"] = idx
    doc = {
        "schema_version": "ATIF-v1.6",
        "agent": {"name": f"cheatbench/{bench}", "version": "raw_trace.v1-import"},
        "steps": builder.steps,
        "metadata": {
            "cheatbench_trace_id": trace_id,
            "cheatbench_label": meta.get("label"),
            "cheatbench_mechanism": meta.get("cheating_mechanism"),
        },
    }
    stats = {
        "envelope": envelope,
        "n_steps": len(builder.steps),
        "n_tool_calls": builder.n_tool_calls,
        "has_tool_calls": builder.n_tool_calls > 0,
        "dropped_obs": builder.n_dropped_obs,
        **extra,
    }
    return doc, stats


def _looks_iquest(messages: list[Any]) -> bool:
    for message in messages[:20]:
        if isinstance(message, dict) and "<function=" in _text_of(message.get("content")):
            return True
    return False


def _classify_message_events(events: list[Any]) -> str:
    for event in events[:10]:
        if not isinstance(event, dict):
            continue
        text = _text_of(event.get("content"))
        if event.get("role") == "trace":
            if "[action]" in text:
                return "codex"
            if re.search(r"^\[\d+\]\s+role=", text, re.M):
                return "openhands_flat"
            if "LLM_OUTPUT:" in text:
                return "mle"
            return "trace_text"
        if "<function=" in text:
            return "iquest"
        if _SHELL_JSON_RE.search(text):
            return "cybench"
    roles = {e.get("role") for e in events[:10] if isinstance(e, dict)}
    if roles <= {"system", "assistant"}:
        return (
            "codex"
            if any("[action]" in _text_of(e.get("content")) for e in events if isinstance(e, dict))
            else "messages"
        )
    return "messages"


def _convert_msg_event(builder: _Steps, event: dict) -> None:
    src = str(event.get("src") or "agent").lower()
    msg = event.get("msg")
    msg_text = msg if isinstance(msg, str) else _text_of(msg)
    calls = _msg_tool_calls(event.get("tools")) if event.get("tools") else []
    obs = event.get("obs")
    obs_text = obs if isinstance(obs, str) else _text_of(obs)
    if src in ("user", "system"):
        if msg_text.strip():
            builder.user(msg_text)
        return
    builder.agent(msg_text, calls or None)
    if obs_text.strip():
        builder.observe(obs_text)


def _convert_messages(builder: _Steps, messages: list[Any], *, envelope: str) -> None:
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        text = _text_of(message.get("content"))
        if role in ("system", "user"):
            if role == "user" and text.lstrip().startswith("OBSERVATION:"):
                builder.observe(text)
            else:
                builder.user(text)
        elif role == "assistant":
            if envelope == "iquest" or "<function=" in text:
                prose, calls = _iquest_calls(text)
                builder.agent(prose, calls or None)
            elif envelope == "codex":
                _convert_codex(builder, text)
            elif envelope == "openhands_flat":
                _convert_openhands_flat(builder, text)
            elif envelope == "mle":
                _convert_mle(builder, text)
            elif envelope == "cybench" or _SHELL_JSON_RE.search(text):
                prose, calls = _cybench_calls(text)
                builder.agent(prose, calls or None)
            else:
                builder.agent(text)
        elif role == "tool":
            builder.observe(text)
        elif role == "trace":
            if envelope == "codex":
                _convert_codex(builder, text)
            elif envelope == "openhands_flat":
                _convert_openhands_flat(builder, text)
            elif envelope == "mle":
                _convert_mle(builder, text)
            else:
                builder.agent(text)
        elif text.strip():
            builder.user(f"[{role or 'unknown'}] {text}")


# ---------------------------------------------------------------------------
# fetch / subset / materialize


def fetch(split: str, dest: str | Path) -> Path:
    """Download the pinned parquet split (and manifests) into ``dest``."""
    from huggingface_hub import hf_hub_download

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    names = [
        f"data/processed/parquet/{split}.parquet",
        f"data/processed/subsets/cheatbench-{split}.manifest.jsonl",
        f"data/processed/subsets/cheatbench-{split}.summary.json",
    ]
    for name in names:
        hf_hub_download(DATASET, name, repo_type="dataset", revision=REVISION, local_dir=dest)
    return dest / "data" / "processed" / "parquet" / f"{split}.parquet"


def _iter_parquet(parquet_path: str | Path) -> Any:
    import pyarrow.parquet as pq

    table = pq.read_table(str(parquet_path))
    yield from table.to_pylist()


def row_meta(row: dict) -> dict:
    """Committed metadata for one parquet row (no trace body)."""
    loc = {
        "kind": row.get("loc_kind"),
        "field_path": row.get("loc_field_path"),
        "line_start": row.get("loc_line_start"),
        "line_end": row.get("loc_line_end"),
        "char_start": row.get("loc_char_start"),
        "char_end": row.get("loc_char_end"),
    }
    return {
        "trace_id": row.get("trace_id"),
        "revision": REVISION,
        "label": row.get("label"),
        "cheating_type": row.get("cheating_type"),
        "cheating_mechanism": row.get("cheating_mechanism"),
        "benchmark": row.get("benchmark"),
        "harness": row.get("harness"),
        "model": row.get("model"),
        "split": row.get("split"),
        "loc": loc,
    }


def _index_entry(meta: dict) -> dict:
    """Slim committed index entry: drop per-row constants and empty spans."""
    loc = {k: v for k, v in (meta.get("loc") or {}).items() if v not in (None, "")}
    entry = {
        "trace_id": meta.get("trace_id"),
        "label": meta.get("label"),
        "cheating_type": meta.get("cheating_type"),
        "cheating_mechanism": meta.get("cheating_mechanism"),
        "benchmark": meta.get("benchmark"),
        "harness": meta.get("harness"),
        "model": meta.get("model"),
    }
    if loc:
        entry["loc"] = loc
    return entry


def trajectory_id(row_id: str) -> str:
    return row_id


def detector_row(trace_id: str, meta: dict, trajectory_sha256: str | None) -> dict:
    label = meta.get("label")
    mechanism = meta.get("cheating_mechanism") or "none"
    benchmark = meta.get("benchmark") or "unknown"
    families: list[str] = []
    if label in ("cheating", "attempt"):
        families = [
            mechanism,
            str(meta.get("cheating_type") or "unknown"),
            f"benchmark:{benchmark}",
        ]
    return {
        "id": trace_id,
        "source": "cb",
        "families": families,
        "labels": {
            "reward_hacking": {"cheating": True, "benign": False}.get(label),
            "cheating_behavior": {"cheating": True, "attempt": True, "benign": False}.get(label),
        },
        "trial": {"kind": "cheatbench", "cb_id": trace_id},
        "trajectory_sha256": trajectory_sha256,
        "cheatbench": {
            "mechanism": mechanism,
            "benchmark": benchmark,
            "harness": meta.get("harness"),
            "loc_field_path": (meta.get("loc") or {}).get("field_path"),
        },
    }


def materialize(parquet_path: str | Path, cb_root: str | Path) -> dict:
    """Convert every row and write trial dirs under ``<cb-root>/cb/<trace_id>/``.

    Returns conversion stats; per-row exclusions go to ``exclusions.jsonl``
    next to the trials (outside git).
    """
    cb_root = Path(cb_root)
    out_dir = cb_root / "cb"
    out_dir.mkdir(parents=True, exist_ok=True)
    stats: dict[str, Any] = {
        "rows": 0,
        "converted": 0,
        "excluded": {},
        "envelopes": {},
        "with_tool_calls": 0,
    }
    exclusions: list[dict] = []
    for row in _iter_parquet(parquet_path):
        trace_id = str(row.get("trace_id"))
        stats["rows"] += 1
        meta = row_meta(row)
        try:
            raw = json.loads(row["trace"])
        except (json.JSONDecodeError, TypeError, KeyError):
            stats["excluded"]["trace_not_json"] = stats["excluded"].get("trace_not_json", 0) + 1
            exclusions.append({"trace_id": trace_id, "reason": "trace_not_json"})
            continue
        try:
            doc, conv = convert(trace_id, raw, meta)
        except ValueError as exc:
            reason = str(exc) if str(exc) in EXCLUDED_REASONS else "unknown_envelope"
            stats["excluded"][reason] = stats["excluded"].get(reason, 0) + 1
            exclusions.append({"trace_id": trace_id, "reason": reason})
            continue
        trial_dir = out_dir / trace_id
        agent_dir = trial_dir / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        body = json.dumps(doc, sort_keys=True)
        (agent_dir / "trajectory.json").write_text(body + "\n", encoding="utf-8")
        (trial_dir / "result.json").write_text(
            json.dumps(
                {
                    "reward": 1 if meta.get("label") == "cheating" else 0,
                    "cheatbench_label": meta.get("label"),
                    "cheatbench_mechanism": meta.get("cheating_mechanism"),
                },
                indent=1,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (trial_dir / "config.json").write_text(
            json.dumps(
                {
                    "task": {
                        "path": f"cheatbench/{meta.get('benchmark')}/{trace_id}",
                    }
                },
                indent=1,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        stats["converted"] += 1
        stats["envelopes"][conv["envelope"]] = stats["envelopes"].get(conv["envelope"], 0) + 1
        stats["with_tool_calls"] += int(conv["has_tool_calls"])
    (cb_root / "exclusions.jsonl").write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in exclusions), encoding="utf-8"
    )
    (cb_root / "materialize-stats.json").write_text(
        json.dumps(stats, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return stats


def subset(parquet_path: str | Path) -> tuple[list[dict], list[dict], dict]:
    """Build committed metadata: (index_rows, manifest_rows, stats).

    ``index_rows`` go to ``cb_subset.json`` (one entry per parquet row,
    exclusions recorded with a reason); ``manifest_rows`` are the convertible
    rows as ``detectors-v1`` manifest entries.
    """
    index_rows: list[dict] = []
    manifest_rows: list[dict] = []
    stats: dict[str, Any] = {"rows": 0, "converted": 0, "excluded": {}, "envelopes": {}}
    for row in _iter_parquet(parquet_path):
        trace_id = str(row.get("trace_id"))
        stats["rows"] += 1
        meta = row_meta(row)
        entry = _index_entry(meta)
        try:
            raw = json.loads(row["trace"])
            doc, conv = convert(trace_id, raw, meta)
        except (json.JSONDecodeError, TypeError, KeyError):
            entry["excluded"] = "trace_not_json"
            stats["excluded"]["trace_not_json"] = stats["excluded"].get("trace_not_json", 0) + 1
        except ValueError as exc:
            reason = str(exc) if str(exc) in EXCLUDED_REASONS else "unknown_envelope"
            entry["excluded"] = reason
            stats["excluded"][reason] = stats["excluded"].get(reason, 0) + 1
        else:
            digest = hashlib.sha256(json.dumps(doc, sort_keys=True).encode("utf-8")).hexdigest()
            entry["envelope"] = conv["envelope"]
            entry["n_tool_calls"] = conv["n_tool_calls"]
            manifest_rows.append(detector_row(trace_id, meta, digest))
            stats["converted"] += 1
            stats["envelopes"][conv["envelope"]] = stats["envelopes"].get(conv["envelope"], 0) + 1
        index_rows.append(entry)
    return index_rows, manifest_rows, stats


def _cmd_fetch(args: argparse.Namespace) -> int:
    path = fetch(args.split, args.dest)
    print(f"fetched {args.split} -> {path}")
    return 0


def _cmd_subset(args: argparse.Namespace) -> int:
    index_rows, manifest_rows, stats = subset(args.parquet)
    out = Path(args.out)
    out.write_text(
        json.dumps(
            {
                "dataset": DATASET,
                "revision": REVISION,
                "license": LICENSE,
                "split": args.split,
                "rows": index_rows,
            },
            indent=1,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    Path(args.manifest_out).write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in manifest_rows),
        encoding="utf-8",
    )
    print(json.dumps(stats, indent=1, sort_keys=True))
    return 0


def _cmd_materialize(args: argparse.Namespace) -> int:
    stats = materialize(args.parquet, args.cb_root)
    print(json.dumps(stats, indent=1, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cheatbench_cmd", required=True)
    fetch_p = sub.add_parser("fetch", help="Download the pinned parquet split")
    fetch_p.add_argument("--split", default="full")
    fetch_p.add_argument("--dest", default="cache/cheatbench")
    fetch_p.set_defaults(func=_cmd_fetch)
    subset_p = sub.add_parser("subset", help="Emit cb_subset.json + manifest rows")
    subset_p.add_argument("--parquet", required=True)
    subset_p.add_argument("--split", default="full")
    subset_p.add_argument("--out", required=True)
    subset_p.add_argument("--manifest-out", required=True)
    subset_p.set_defaults(func=_cmd_subset)
    mat_p = sub.add_parser("materialize", help="Convert rows to trial dirs under a cb-root")
    mat_p.add_argument("--parquet", required=True)
    mat_p.add_argument("--cb-root", required=True)
    mat_p.set_defaults(func=_cmd_materialize)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
# Viewer layer: faithful staged-ATIF conversion for the Trace Lab Scout viewer
# (HAR-203 second wave). The calibration layer above is lossy by design
# (caps, dropped observations, exclusion reasons) and its step ids are not
# Scout-validated; this layer keeps every message/tool call/output verbatim,
# tracks raw line spans per step, and maps each row's localization to the
# owning ATIF step plus a clearly-marked annotation message. Pinned constants
# (DATASET/REVISION/LICENSE/USER_AGENT) are shared above.
# ---------------------------------------------------------------------------

FILENAME = "data/processed/parquet/full.parquet"

SHA256 = "b4d2a1496e5f8de7b4c0e4791bd9f8fcbd0a3567f95d2e759b5da20ebb160e63"
CARD_URL = "https://huggingface.co/datasets/steinad/CheatBench"
RAW_SCHEMA = "cheatbench.raw_trace.v1"

SUBSET_LABELS = frozenset({"cheating", "attempt"})
#: Mechanisms matching our MiMo cheats (future git history, upstream fetch,
#: grader exploitation).
SUBSET_MECHANISMS = frozenset(
    {
        "secret_discovery_in_environment",
        "secret_discovery_via_web_search",
        "verifier_exploitation",
    }
)

ATIF_SOURCES = ("system", "user", "agent")
_STAGED_BY = "src/evallab/cheatbench.py"


class CheatbenchError(Exception):
    """Pinned-fetch or conversion failure for the CheatBench corpus."""


def source_url() -> str:
    """Pinned HTTPS URL of the full parquet (never a moving ref)."""
    return f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/{FILENAME}"


def fetch_parquet(dest: str | Path, *, downloader: Callable[[str], bytes] | None = None) -> Path:
    """Download the pinned parquet to ``dest`` and verify its sha256.

    ``downloader`` is injectable so tests never reach the network.
    """
    dest = Path(dest)
    if dest.is_file() and hashlib.sha256(dest.read_bytes()).hexdigest() == SHA256:
        return dest
    payload = (downloader or _anonymous_download)(source_url())
    actual = hashlib.sha256(payload).hexdigest()
    if actual != SHA256:
        raise CheatbenchError(
            f"digest mismatch for {DATASET}@{REVISION}: expected {SHA256}, got {actual}"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(payload)
    return dest


def _anonymous_download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
        return response.read()


def load_rows(parquet_path: str | Path, *, columns: list[str] | None = None) -> list[dict]:
    """Read parquet rows as plain dicts (pyarrow is a root dependency)."""
    import pyarrow.parquet as pq

    return pq.read_table(parquet_path, columns=columns).to_pylist()


def is_subset_row(row: Mapping[str, Any]) -> bool:
    """Whether a row belongs to the MiMo-relevant viewer subset."""
    return row.get("label") in SUBSET_LABELS and row.get("cheating_mechanism") in SUBSET_MECHANISMS


def select_subset(rows: list[dict]) -> list[dict]:
    """Every ``cheating``/``attempt`` row with a MiMo-relevant mechanism."""
    return [row for row in rows if is_subset_row(row)]


def counts_table(rows: list[dict]) -> list[tuple[Any, Any, Any, int]]:
    """Sorted (benchmark, mechanism, label, n) over the subset rows."""
    from collections import Counter

    counts: Counter[tuple[Any, Any, Any]] = Counter(
        (row.get("benchmark"), row.get("cheating_mechanism"), row.get("label"))
        for row in rows
        if is_subset_row(row)
    )
    return [(b, m, label, counts[(b, m, label)]) for (b, m, label) in sorted(counts)]


_FUNCTION_MARKUP = re.compile(r"<function=([^>]+)>")
_PARAMETER_MARKUP = re.compile(r"<parameter=([^>]+)>(.*?)</parameter>", re.DOTALL)
_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def resolve_field_path(obj: Any, path: str) -> Any:
    """Resolve ``messages[8].content``-style paths; raise LookupError."""
    if not path:
        raise LookupError("empty field path")
    current = obj
    for match in _PATH_TOKEN.finditer(path):
        name, index = match.group(1), match.group(2)
        try:
            current = current[name] if name is not None else current[int(index)]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LookupError(f"unresolvable field path {path!r}") from exc
    return current


def parse_raw_trace(payload: str) -> dict:
    """Parse a serialized trace; reject a mismatched declared schema."""
    try:
        trace = json.loads(payload)
    except ValueError as exc:
        raise CheatbenchError(f"trace is not JSON: {exc}") from exc
    if not isinstance(trace, dict):
        raise CheatbenchError("trace payload must be a JSON object")
    declared = trace.get("schema_version", trace.get("schema"))
    if declared is not None and declared != RAW_SCHEMA:
        raise CheatbenchError(f"unsupported trace schema {declared!r}")
    return trace


def text_of(content: Any) -> str:
    """Best-effort plain text of heterogeneous message content."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                for key in ("text", "content", "output"):
                    value = part.get(key)
                    if isinstance(value, str):
                        parts.append(value)
                        break
        return "".join(parts)
    if isinstance(content, dict):
        return json.dumps(content)
    return str(content)


def parse_function_markup(message: str) -> list[dict]:
    """SWE-bench-style ``<function=f><parameter=k>v</parameter>`` calls."""
    calls = []
    for index, fn_match in enumerate(_FUNCTION_MARKUP.finditer(message), start=1):
        params = {key: value for key, value in _PARAMETER_MARKUP.findall(message[fn_match.end() :])}
        calls.append(
            {
                "tool_call_id": f"markup_{index}",
                "function_name": fn_match.group(1),
                "arguments": params,
            }
        )
    return calls


def parse_openai_tool_calls(tool_calls: Any) -> list[dict]:
    """OpenAI-style ``{id, function: {name, arguments}}`` to staged shape."""
    staged = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {"command": arguments}
        staged.append(
            {
                "tool_call_id": str(call.get("id") or f"call_{len(staged) + 1}"),
                "function_name": str(function.get("name") or "unknown"),
                "arguments": arguments if isinstance(arguments, dict) else {},
            }
        )
    return staged


def _new_step(
    step_id: int,
    source: str,
    message: str,
    *,
    field: str,
    line_start: int | None = None,
    line_end: int | None = None,
    note: str | None = None,
) -> dict:
    cheatbench: dict[str, Any] = {
        "source_ref": {"field": field, "line_start": line_start, "line_end": line_end}
    }
    if note is not None:
        cheatbench["note"] = note
    step: dict[str, Any] = {
        "step_id": step_id,
        "source": source,
        "message": message,
        "extra": {"cheatbench": cheatbench},
    }
    return step


def _attach_observation(
    steps: list[dict],
    content: str,
    *,
    field: str,
    line_start: int | None = None,
    line_end: int | None = None,
    source_call_id: str | None = None,
    warnings: list[str],
) -> int | None:
    """Attach tool output to the latest agent step; return its step_id."""
    text = content if isinstance(content, str) else text_of(content)
    result: dict[str, Any] = {"content": text}
    if source_call_id:
        result["source_call_id"] = source_call_id
    for step in reversed(steps):
        if step["source"] == "agent":
            observation = step.setdefault("observation", {"results": []})
            observation["results"].append(result)
            ref = step["extra"]["cheatbench"].setdefault("obs_refs", [])
            ref.append({"field": field, "line_start": line_start, "line_end": line_end})
            return step["step_id"]
    warnings.append(f"tool output at {field} has no preceding agent step; kept as user step")
    steps.append(_new_step(len(steps) + 1, "user", text, field=field, note="remapped-tool-output"))
    return steps[-1]["step_id"]


#: MLE-bench turn events use ``role: trace`` for agent narrations
#: (``LLM_OUTPUT: ...``); the swe-rebench single-log kind event never reaches
#: role mapping (its content is parsed into turns instead).
_ROLE_MAP = {"system": "system", "user": "user", "assistant": "agent", "trace": "agent"}


def _map_role(role: Any, *, field: str, warnings: list[str]) -> str:
    mapped = _ROLE_MAP.get(role)
    if mapped is None:
        warnings.append(f"unknown role {role!r} at {field}; kept as user step")
        return "user"
    return mapped


def convert_trace(trace: dict) -> tuple[list[dict], list[str]]:
    """Convert one parsed raw trace to ATIF steps (1-based step_id)."""
    warnings: list[str] = []
    if "messages" in trace:
        return _convert_viewer_messages(trace, warnings), warnings
    return _convert_events(trace, warnings), warnings


def _convert_viewer_messages(trace: dict, warnings: list[str]) -> list[dict]:
    steps = []
    for index, message in enumerate(trace.get("messages") or []):
        field = f"messages[{index}].content"
        role = message.get("role") if isinstance(message, dict) else None
        content = text_of(message.get("content") if isinstance(message, dict) else message)
        source = _map_role(role, field=field, warnings=warnings)
        calls = parse_function_markup(content) if source == "agent" else []
        step = _new_step(len(steps) + 1, source, content, field=field)
        if calls:
            step["tool_calls"] = calls
        steps.append(step)
    if not steps:
        warnings.append("trace has an empty messages[] list")
    return steps


def _convert_events(trace: dict, warnings: list[str]) -> list[dict]:
    events = trace.get("events") or []
    if isinstance(events, dict):
        events = [events]
    kinds = {tuple(sorted(e.keys())) for e in events if isinstance(e, dict)}
    if kinds == {("content", "kind", "role")} and len(events) == 1:
        content = events[0].get("content")
        if isinstance(content, str) and re.search(r"^\[\d+\]\s+role=", content, re.MULTILINE):
            return _convert_prefixed_log(content, "events[0].content", warnings)
    stdout_corpus = "\n".join(
        event.get("stdout")
        for event in events
        if isinstance(event, dict) and isinstance(event.get("stdout"), str)
    )
    steps: list[dict] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            warnings.append(f"events[{index}] is not an object; skipped")
            continue
        keys = set(event.keys())
        if {"msg", "step"} <= keys:
            _convert_step_event(event, index, steps, warnings)
        elif {"prompt", "response"} <= keys:
            _convert_episode_event(event, index, steps, warnings)
        elif "stdout" in keys or ("text" in keys and "command" in keys):
            _convert_forgecode_event(event, index, steps, warnings)
        elif "text" in keys and "content" not in keys:
            _convert_text_event(event, index, steps, warnings, stdout_corpus=stdout_corpus)
        elif "content" in keys:
            _convert_content_event(event, index, steps, warnings)
        else:
            warnings.append(f"events[{index}] has unhandled keys {sorted(keys)}; skipped")
    return steps


def _convert_content_event(event: dict, index: int, steps: list[dict], warnings: list[str]) -> None:
    field = f"events[{index}].content"
    role = event.get("role")
    content = text_of(event.get("content"))
    if role == "tool":
        ids = event.get("tool_call_ids") or []
        call_id = str(ids[0]) if ids else None
        lines = content.split("\n")
        _attach_observation(
            steps,
            content,
            field=field,
            line_start=1,
            line_end=len(lines) or 1,
            source_call_id=call_id,
            warnings=warnings,
        )
        return
    source = _map_role(role, field=field, warnings=warnings)
    message = content
    action = event.get("action") if isinstance(event.get("action"), str) else None
    if source == "agent" and action and action not in content:
        # The executed command lives in ``action`` (and in tool_calls
        # arguments); inline it so the cheat step stays text-searchable.
        message = f"{content}\n\n[action]\n{action}" if content else f"[action]\n{action}"
        step = _new_step(len(steps) + 1, source, message, field=field, note="action-inlined")
    else:
        step = _new_step(len(steps) + 1, source, content, field=field)
    if source == "agent":
        message_type = event.get("message_type")
        if message_type == "action" or action:
            calls = parse_openai_tool_calls(event.get("tool_calls"))
            if action and not calls:
                calls = [
                    {
                        "tool_call_id": f"action_{len(steps)}",
                        "function_name": "bash",
                        "arguments": {"command": action},
                    }
                ]
            if calls:
                step["tool_calls"] = calls
        elif event.get("tool_calls"):
            step["tool_calls"] = parse_openai_tool_calls(event.get("tool_calls"))
    steps.append(step)


def _convert_step_event(event: dict, index: int, steps: list[dict], warnings: list[str]) -> None:
    """Codex/mini-swe-agent style ``{msg, obs, src, step, tools?}`` events."""
    src = event.get("src", "agent")
    source = _map_role(
        {"agent": "assistant", "user": "user"}.get(src, src),
        field=f"events[{index}].src",
        warnings=warnings,
    )
    message = event.get("msg") or ""
    step = _new_step(len(steps) + 1, source, message, field=f"events[{index}].msg")
    tools = event.get("tools") or []
    calls = []
    for position, tool in enumerate(tools):
        if not isinstance(tool, dict):
            continue
        calls.append(
            {
                "tool_call_id": f"call_{event.get('step', index)}_{position}",
                "function_name": str(tool.get("fn") or "shell"),
                "arguments": {"cmd": tool.get("cmd")},
            }
        )
    if calls:
        step["tool_calls"] = calls
    obs = event.get("obs")
    if isinstance(obs, str) and obs:
        lines = obs.split("\n")
        step["observation"] = {"results": [{"content": obs}]}
        step["extra"]["cheatbench"]["obs_refs"] = [
            {"field": f"events[{index}].obs", "line_start": 1, "line_end": len(lines) or 1}
        ]
    steps.append(step)


def _convert_episode_event(event: dict, index: int, steps: list[dict], warnings: list[str]) -> None:
    """Meta-Harness style ``{prompt, response}`` episode events."""
    prompt = event.get("prompt") or ""
    response = event.get("response") or ""
    response_text = response if isinstance(response, str) else text_of(response)
    steps.append(_new_step(len(steps) + 1, "user", prompt, field=f"events[{index}].prompt"))
    step = _new_step(len(steps) + 1, "agent", response_text, field=f"events[{index}].response")
    calls = _parse_embedded_tool_calls(response_text)
    if calls:
        step["tool_calls"] = calls
    elif "Tool Calls" in response_text:
        warnings.append(f"events[{index}].response mentions tool calls that did not parse")
    steps.append(step)


_TOOL_CALLS_JSON = re.compile(r"Tool Calls:\s*(\[.*\])", re.DOTALL)


def _parse_embedded_tool_calls(response_text: str) -> list[dict]:
    match = _TOOL_CALLS_JSON.search(response_text)
    if not match:
        return []
    try:
        return parse_openai_tool_calls(json.loads(match.group(1)))
    except ValueError:
        return []


def _convert_forgecode_event(
    event: dict, index: int, steps: list[dict], warnings: list[str]
) -> None:
    """ForgeCode ``{command, stdout}`` + ``{text}`` events (Claude renderings).

    ``text`` is the same rendered transcript as ``stdout`` minus the shell
    banner, so when it is fully contained it is not emitted twice.
    """
    stdout = event.get("stdout") or ""
    command = event.get("command") or ""
    return_code = event.get("return_code")
    if "stdout" in event:
        for segment, start, end in _split_rendered_log(stdout):
            step = _new_step(
                len(steps) + 1,
                "agent",
                segment,
                field=f"events[{index}].stdout",
                line_start=start,
                line_end=end,
            )
            if start == 1 and command:
                step["extra"]["cheatbench"]["shell_command"] = command
                step["extra"]["cheatbench"]["return_code"] = return_code
            steps.append(step)
    if "text" in event:
        _convert_text_event(event, index, steps, warnings, stdout_corpus=stdout)


def _convert_text_event(
    event: dict, index: int, steps: list[dict], warnings: list[str], *, stdout_corpus: str
) -> None:
    """ForgeCode ``{id, text, type}`` events: skip when stdout already holds them.

    The skipped text is recorded as a line-offset alias on the first stdout
    step so localizations against ``events[i].text`` still map (cb-001975).
    """
    text = event.get("text")
    field = f"events[{index}].text"
    if not isinstance(text, str) or not text:
        warnings.append(f"{field} is empty; skipped")
        return
    if text in stdout_corpus:
        line_offset = stdout_corpus[: stdout_corpus.find(text)].count("\n")
        for step in steps:
            ref = step["extra"]["cheatbench"]["source_ref"]
            if ref["field"].endswith(".stdout"):
                step["extra"]["cheatbench"].setdefault("text_aliases", []).append(
                    {"field": field, "line_offset": line_offset}
                )
                warnings.append(f"{field} is contained in {ref['field']}; emitted once")
                return
        warnings.append(f"{field} is contained in stdout but no stdout step exists; emitting")
    for segment, start, end in _split_rendered_log(text):
        steps.append(
            _new_step(len(steps) + 1, "agent", segment, field=field, line_start=start, line_end=end)
        )


def _split_rendered_log(stdout: str) -> list[tuple[str, int, int]]:
    """Split a Claude rendered transcript on ``⏺`` markers (1-based lines)."""
    lines = stdout.split("\n")
    starts = [n for n, line in enumerate(lines) if line.startswith("⏺ ")]
    if not starts:
        return [(stdout, 1, len(lines) or 1)] if stdout else []
    boundaries = [0] + starts[1:] + [len(lines)]
    segments = []
    for start, end in zip(boundaries[:-1], boundaries[1:], strict=True):
        body = "\n".join(lines[start:end]).strip("\n")
        if body:
            segments.append((body, start + 1, end))
    return segments


_LOG_TURN = re.compile(r"^\[(\d+)\]\s+role=(system|assistant|tool)\b")
_LOG_CALL = re.compile(r"^TOOL_CALL\[(\d+)\]\s+(\w+):\s*(.*)$", re.DOTALL)
_LOG_PREFIX = re.compile(r"^\[\d+\]\s+")


def _convert_prefixed_log(content: str, field: str, warnings: list[str]) -> list[dict]:
    """SWE-rebench single-log ``[NNNN] role=X`` transcripts."""
    steps: list[dict] = []
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        role = current["role"]
        text = "\n".join(current["lines"])
        if role == "tool":
            _attach_observation(
                steps,
                text,
                field=field,
                line_start=current["start"],
                line_end=current["end"],
                warnings=warnings,
            )
        else:
            step = _new_step(
                len(steps) + 1,
                _map_role(role, field=field, warnings=warnings),
                text,
                field=field,
                line_start=current["start"],
                line_end=current["end"],
            )
            if current["calls"]:
                step["tool_calls"] = current["calls"]
            steps.append(step)
        current = None

    for lineno, raw in enumerate(content.split("\n"), start=1):
        body = _LOG_PREFIX.sub("", raw)
        turn = _LOG_TURN.match(raw)
        if turn:
            flush()
            current = {
                "role": turn.group(2),
                "lines": [raw],
                "calls": [],
                "start": lineno,
                "end": lineno,
            }
            continue
        call = _LOG_CALL.match(body)
        if call and current is not None and current["role"] == "assistant":
            current["calls"].append(
                {
                    "tool_call_id": f"log_{current['start']}_{call.group(1)}",
                    "function_name": call.group(2),
                    "arguments": {"command": call.group(3)},
                }
            )
        if current is None:
            current = {
                "role": "assistant",
                "lines": [],
                "calls": [],
                "start": lineno,
                "end": lineno,
            }
        assert current is not None
        current["lines"].append(raw)
        current["end"] = lineno
    flush()
    if not steps:
        warnings.append(f"{field}: prefixed log produced no steps")
    return steps


def _step_line_span(step: dict) -> list[tuple[str, int, int]]:
    """(field, start, end) spans a step covers: own ref plus obs refs."""
    spans = []
    ref = step["extra"]["cheatbench"]["source_ref"]
    spans.append((ref["field"], ref.get("line_start"), ref.get("line_end")))
    for obs in step["extra"]["cheatbench"].get("obs_refs", []):
        spans.append((obs["field"], obs.get("line_start"), obs.get("line_end")))
    return spans


def locate_cheat(
    steps: list[dict], loc_field_path: str, loc_line_start: Any
) -> tuple[int | None, int | None, str | None]:
    """Map a CheatBench localization to (step_id, obs result idx|None, correction).

    ``correction`` is None on an exact match, else one of
    ``single-event-fallback`` (swe-rebench ``events[1]`` on 1-event traces),
    ``text-alias`` (skipped-duplicate ``events[i].text``), or
    ``event-index-fallback`` (subfields with no own span: ``tools[j].cmd``,
    ``action``).
    """
    if not loc_field_path:
        return None, None, None
    try:
        line = int(loc_line_start) if loc_line_start is not None else None
    except (TypeError, ValueError):
        line = None
    match = re.fullmatch(r"(messages|events)\[(\d+)\](\..+)?", loc_field_path)
    if match is None:
        return None, None, None
    kind, wanted, _rest = match.group(1), int(match.group(2)), match.group(3)
    correction: str | None = None
    fields = {span[0] for step in steps for span in _step_line_span(step)}
    if loc_field_path not in fields:
        aliased = _resolve_text_alias(steps, loc_field_path, line)
        if aliased is not None:
            loc_field_path, line = aliased
            correction = "text-alias"
        elif kind == "events" and wanted == 1 and f"events[0]{loc_field_path[9:]}" in fields:
            loc_field_path = f"events[0]{loc_field_path[9:]}"
            correction = "single-event-fallback"
        else:
            event_steps = [
                step["step_id"]
                for step in steps
                for field, _s, _e in _step_line_span(step)
                if field.startswith(f"events[{wanted}].")
            ]
            if kind == "events" and event_steps:
                return event_steps[0], None, "event-index-fallback"
            return None, None, None
    for step in steps:
        for position, (field, start, end) in enumerate(_step_line_span(step)):
            if field != loc_field_path:
                continue
            if line is None or start is None or end is None or start <= line <= end:
                return step["step_id"], (position - 1 if position > 0 else None), correction
    first = next(
        step["step_id"]
        for step in steps
        for field, _s, _e in _step_line_span(step)
        if field == loc_field_path
    )
    return first, None, correction


def _resolve_text_alias(
    steps: list[dict], loc_field_path: str, line: int | None
) -> tuple[str, int | None] | None:
    """Map a skipped-duplicate ``events[i].text`` line into its stdout step."""
    for step in steps:
        for alias in step["extra"]["cheatbench"].get("text_aliases", []):
            if alias["field"] != loc_field_path:
                continue
            for field, start, end in _step_line_span(step):
                if not field.endswith(".stdout"):
                    continue
                shifted = None if line is None else line + alias["line_offset"]
                if shifted is None or start is None or end is None or start <= shifted <= end:
                    return field, shifted
            for field, _s, _e in _step_line_span(step):
                if field.endswith(".stdout"):
                    return field, None
    return None


def _resolve_loc_content(trace: dict, loc_field_path: str) -> tuple[str | None, str | None]:
    """Resolve loc content, applying the single-event fallback (swe-rebench)."""
    try:
        content = resolve_field_path(trace, loc_field_path)
    except LookupError:
        content = None
    if isinstance(content, str):
        return content, loc_field_path
    match = re.fullmatch(r"events\[1\](\..+)", loc_field_path)
    if match is not None:
        fallback = f"events[0]{match.group(1)}"
        try:
            content = resolve_field_path(trace, fallback)
        except LookupError:
            content = None
        if isinstance(content, str):
            return content, fallback
    return None, None


def _anchor_line(content: str, loc_line_start: Any, loc_snippet: str | None) -> tuple[Any, bool]:
    """Calibrate the loc line by searching the snippet in the field content.

    Upstream line numbers drift when a field mixes ``\\r``/``\\n`` newlines,
    and some snippets normalize newlines to ``|`` or unescape JSON quotes;
    the snippet itself is ground truth when found verbatim under one of
    these exact-substring variants.
    """
    needle = (loc_snippet or "")[:200]
    for variant in _snippet_variants(needle):
        if variant and variant in content:
            line = content[: content.find(variant)].count("\n") + 1
            try:
                adjusted = loc_line_start is not None and int(loc_line_start) != line
            except (TypeError, ValueError):
                adjusted = True
            return line, adjusted
    return loc_line_start, False


def _snippet_variants(needle: str) -> list[str]:
    """Exact-substring search variants for upstream snippet normalizations."""
    variants = [needle]
    piped = needle.replace("|", "\n")
    if piped != needle:
        variants.append(piped)
    escaped = needle.replace('"', '\\"')
    if escaped != needle:
        variants.append(escaped)
    return variants


def message_number(steps: list[dict], step_id: int | None, result_index: int | None) -> int | None:
    """1-based transcript message number as Scout badges it.

    Scout numbers each step's own message plus one entry per structured
    ``tool_calls`` item; ``observation.results`` tool outputs render without
    a badge number, so a cheat inside one has no message number (step only).
    """
    if step_id is None or result_index is not None:
        return None
    count = 0
    for step in steps:
        if step["step_id"] == step_id:
            return count + 1
        count += 1 + len(step.get("tool_calls") or [])
    return None


_CORRECTION_NOTES = {
    "single-event-fallback": (
        "note: raw loc path events[1].content did not resolve (single-event trace); "
        "mapped to events[0].content"
    ),
    "text-alias": (
        "note: raw loc events[i].text is a duplicate of the shell stdout; "
        "mapped into the stdout steps"
    ),
    "event-index-fallback": (
        "note: raw loc subfield (tools[j].cmd/action) has no own text span; "
        "mapped to the owning event's step"
    ),
}


def build_annotation(
    row: Mapping[str, Any],
    *,
    cheat_step_id: int | None,
    cheat_message_no: int | None,
    correction: str | None,
    line_adjusted: bool,
    warnings: list[str],
    n_steps: int,
) -> str:
    """Clearly-marked locator card appended after the original trace."""
    snippet = (row.get("loc_snippet") or "")[:1000]
    lines = [
        "[CheatBench annotation — not part of the original trace]",
        f"trace {row.get('trace_id')} · benchmark {row.get('benchmark')} · "
        f"harness {row.get('harness')} · model {row.get('model')}",
        f"label: {row.get('label')} (type {row.get('cheating_type')}) · "
        f"mechanism: {row.get('cheating_mechanism')}",
    ]
    if cheat_step_id is None:
        lines.append(
            "cheat location: unlocalized — the curators record no in-trace span "
            f"(loc_kind={row.get('loc_kind')})"
        )
    else:
        lines.append(
            f"cheat location: ATIF step {cheat_step_id} of {n_steps}"
            + (f", transcript message M{cheat_message_no}" if cheat_message_no else "")
            + f" · raw loc: {row.get('loc_field_path')} "
            f"L{row.get('loc_line_start')}-L{row.get('loc_line_end')}"
        )
    if correction in _CORRECTION_NOTES:
        lines.append(_CORRECTION_NOTES[correction])
    if line_adjusted:
        lines.append(
            "note: raw loc line did not match the snippet position (mixed newlines); "
            "mapped by snippet search"
        )
    if row.get("rationale"):
        lines.append(f"rationale: {row.get('rationale')}")
    if row.get("note"):
        lines.append(f"note: {row.get('note')}")
    if snippet:
        lines.append(f"loc snippet: {snippet}")
    for warning in warnings:
        lines.append(f"converter warning: {warning}")
    return "\n".join(lines)


def locate_row(
    trace: dict, steps: list[dict], row: Mapping[str, Any]
) -> tuple[int | None, int | None, str | None, bool]:
    """Locate a row's cheat: (step_id, obs idx|None, correction|None, line_adjusted)."""
    raw_path = row.get("loc_field_path") or ""
    content, resolved_path = _resolve_loc_content(trace, raw_path)
    if content is not None:
        loc_line, line_adjusted = _anchor_line(
            content, row.get("loc_line_start"), row.get("loc_snippet")
        )
    else:
        resolved_path, loc_line, line_adjusted = raw_path, row.get("loc_line_start"), False
    cheat_step_id, result_index, correction = locate_cheat(steps, resolved_path or "", loc_line)
    if resolved_path != raw_path and correction is None and cheat_step_id is not None:
        correction = "single-event-fallback"
    return cheat_step_id, result_index, correction, line_adjusted


def convert_row(row: Mapping[str, Any]) -> tuple[dict, dict, list[str]]:
    """Convert one parquet row to (staged trajectory, scout metadata, warnings)."""
    trace = parse_raw_trace(row["trace"])
    steps, warnings = convert_trace(trace)
    cheat_step_id, result_index, correction, line_adjusted = locate_row(trace, steps, row)
    cheat_message_no = message_number(steps, cheat_step_id, result_index)
    annotation = build_annotation(
        row,
        cheat_step_id=cheat_step_id,
        cheat_message_no=cheat_message_no,
        correction=correction,
        line_adjusted=line_adjusted,
        warnings=list(warnings),
        n_steps=len(steps),
    )
    steps.append(_new_step(len(steps) + 1, "user", annotation, field="cheatbench.annotation"))
    trajectory = {
        "schema_version": "ATIF-v1.7",
        "session_id": f"cheatbench-{row.get('trace_id')}",
        "agent": {
            "name": str(row.get("harness") or "cheatbench"),
            "version": "cheatbench-import/1",
            "model_name": row.get("model"),
        },
        "steps": steps,
        "extra": {
            "cheatbench": {
                "staged_by": _STAGED_BY,
                "trace_id": row.get("trace_id"),
                "benchmark": row.get("benchmark"),
                "label": row.get("label"),
                "cheating_type": row.get("cheating_type"),
                "cheating_mechanism": row.get("cheating_mechanism"),
                "source_uri": CARD_URL,
                "revision": REVISION,
            }
        },
    }
    label = row.get("label")
    reward = 1.0 if label == "cheating" else 0.0
    meta = {
        "task": row.get("benchmark"),
        "trial_name": row.get("trace_id"),
        "trial_id": row.get("trace_id"),
        "job": "cheatbench",
        "agent": row.get("harness"),
        "model": row.get("model"),
        "verdict": "passed" if label == "cheating" else "failed",
        "reward": reward,
        "rewards": {"reward": reward},
        "stop_reason": None,
        "source": "cheatbench:import_cheatbench.py",
        "cheatbench_label": label,
        "cheating_type": row.get("cheating_type"),
        "cheating_mechanism": row.get("cheating_mechanism"),
        "rationale": row.get("rationale"),
        "note": row.get("note"),
        "cheat_step_id": cheat_step_id,
        "cheat_message_no": cheat_message_no,
        "loc_corrected": correction is not None,
        "loc_correction": correction,
        "loc_line_adjusted": line_adjusted,
        "loc_kind": row.get("loc_kind"),
        "loc_pattern": row.get("loc_pattern"),
        "loc_blatancy": row.get("loc_blatancy"),
        "loc_field_path": row.get("loc_field_path"),
        "loc_line_start": row.get("loc_line_start"),
        "loc_line_end": row.get("loc_line_end"),
        "loc_snippet_context": row.get("loc_snippet_context"),
        "loc_evidence_source": row.get("loc_evidence_source"),
        "loc_confidence": row.get("loc_confidence"),
    }
    return trajectory, meta, warnings


def validate_staged(trajectory: Mapping[str, Any]) -> list[str]:
    """Structural check of a staged trajectory (Harbor validates for real)."""
    issues = []
    if not isinstance(trajectory, dict):
        return ["trajectory must be an object"]
    for key in ("schema_version", "session_id", "agent", "steps"):
        if key not in trajectory:
            issues.append(f"missing required key {key!r}")
    agent = trajectory.get("agent")
    if not isinstance(agent, dict) or not agent.get("name"):
        issues.append("agent.name is required")
    steps = trajectory.get("steps")
    if not isinstance(steps, list) or not steps:
        issues.append("steps must be a non-empty list")
        return issues
    for position, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            issues.append(f"step {position}: must be an object")
            continue
        if step.get("step_id") != position:
            issues.append(f"step {position}: step_id must be {position}")
        if step.get("source") not in ATIF_SOURCES:
            issues.append(f"step {position}: invalid source {step.get('source')!r}")
        if not isinstance(step.get("message"), str):
            issues.append(f"step {position}: message must be a string")
        for call in step.get("tool_calls") or []:
            for key in ("tool_call_id", "function_name", "arguments"):
                if key not in call:
                    issues.append(f"step {position}: tool_call missing {key!r}")
        results = (step.get("observation") or {}).get("results", [])
        if not isinstance(results, list):
            issues.append(f"step {position}: observation.results must be a list")
    return issues
