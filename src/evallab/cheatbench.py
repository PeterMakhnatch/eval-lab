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
