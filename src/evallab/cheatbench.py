"""CheatBench as a calibration source for ``evallab detectors`` and the Scout viewer.

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

One converter serves both consumers. ``convert`` turns a raw trace into
detector ATIF (lossy by design: message/observation caps, dropped orphan
observations) and returns its ``blind_atif`` projection, so materialized
trials carry evidence only: no label, mechanism or span side-channel is in
``trajectory.json`` or ``result.json``; labels live in the corpus manifest.
The evaluator-only viewer pipeline (``convert_row``) decorates the same steps
with the builder's per-step source spans and runs the
cheat-localization layer (``locate_row`` / ``message_number`` /
``build_annotation``) to stage Scout transcripts with a clearly-marked
annotation message. The fetch path is a single sha256-verified pinned
download (``fetch_parquet``).
"""

from __future__ import annotations

import ast
import contextlib
import json
import re
import shlex
from collections.abc import Iterator, Mapping
from typing import Any

MSG_CAP = 8_000
OBS_CAP = 50_000


# ---------------------------------------------------------------------------
# small parsing helpers


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]"


def _bounded(text: str, limit: int, caps: bool = True) -> str:
    """``_cap`` for the default import; verbatim when the caps are switched off."""
    return _cap(text, limit) if caps else text


def text_of(content: Any) -> str:
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
                parts.append(_cap(json.dumps(block), 2000))
        return "\n".join(parts)
    return _cap(json.dumps(content), 4000) if content is not None else ""


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
            out.append("[thinking] " + _cap(block["thinking"], 2000))
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
                parts.append(_cap(json.dumps(block), 2000))
        return "\n".join(parts)
    return _cap(json.dumps(content), 4000) if content is not None else ""


def _pilot_steps(stdout: str, *, caps: bool = True) -> list[tuple[str, list[dict], list[dict]]]:
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
            args = {"input": _cap(json.dumps(raw), 4000)}
    else:
        for field in ("file_path", "path", "filename", "file", "command", "cmd", "input"):
            value = raw.get(field)
            if isinstance(value, str) and value:
                args[field] = _cap(value, 4000)
                if field in ("command", "cmd"):
                    args.setdefault("keystrokes", _cap(value, 4000))
        for field in ("old_str", "new_str", "old_string", "new_string"):
            value = raw.get(field)
            if isinstance(value, str) and value:
                args[field] = _cap(value, 4000)
        if not args:
            args = {"input": _cap(json.dumps(raw), 4000)}
        elif "keystrokes" not in args and lname in ("bash",):
            pass
    for key in ("description",):
        if isinstance(raw.get(key), str):
            args[key] = _cap(raw[key], 500)
    return args


# ---------------------------------------------------------------------------
# step builder


class _Steps:
    def __init__(self, model: str = "", *, caps: bool = True) -> None:
        self.steps: list[dict[str, Any]] = []
        self.model = model
        self.caps = caps
        self.n_tool_calls = 0
        self.n_dropped_obs = 0
        # Source spans, parallel to ``steps``: {"field": own field, "obs": [obs fields]}.
        # Never serialized into detector trial bytes; the viewer pipeline
        # decorates staged steps from these. ``_span`` scopes set the default
        # field; an explicit ``field`` overrides it for msg/obs precision.
        self.regions: list[dict[str, Any]] = []
        self._scope: list[str] = []

    def _field(self, explicit: str | None) -> str | None:
        if explicit is not None:
            return explicit
        return self._scope[-1] if self._scope else None

    def user(self, text: str, *, field: str | None = None) -> None:
        text = text.strip()
        if text:
            self.steps.append(
                {
                    "step_id": len(self.steps) + 1,
                    "source": "user",
                    "message": _bounded(text, MSG_CAP, self.caps),
                }
            )
            self.regions.append({"field": self._field(field), "obs": []})

    def agent(
        self,
        text: str,
        calls: list[dict] | None = None,
        results: list[dict] | None = None,
        *,
        field: str | None = None,
        obs_field: str | None = None,
    ) -> None:
        text = text.strip()
        if not text and not calls and not results:
            return
        step: dict[str, Any] = {
            "step_id": len(self.steps) + 1,
            "source": "agent",
            "message": _bounded(text, MSG_CAP, self.caps) if text else "(tool calls only)",
        }
        if self.model:
            step["model_name"] = self.model
        if calls:
            step["tool_calls"] = calls
            self.n_tool_calls += len(calls)
        own = self._field(field)
        if results:
            step["observation"] = {"results": results}
        self.steps.append(step)
        self.regions.append(
            {
                "field": own,
                "obs": [{"field": obs_field or own} for _ in (results or [])],
            }
        )

    def observe(self, text: str, call_id: str = "unknown", *, field: str | None = None) -> None:
        text = text.strip()
        if not text:
            return
        for idx in range(len(self.steps) - 1, -1, -1):
            if self.steps[idx].get("source") == "agent":
                obs = self.steps[idx].setdefault("observation", {"results": []})
                obs["results"].append(
                    {"source_call_id": call_id, "content": _bounded(text, OBS_CAP, self.caps)}
                )
                self.regions[idx]["obs"].append({"field": self._field(field)})
                return
        self.n_dropped_obs += 1


@contextlib.contextmanager
def _span(builder: _Steps, field: str) -> Iterator[None]:
    """Default source field for steps built inside the block (span side-channel)."""
    builder._scope.append(field)
    try:
        yield
    finally:
        builder._scope.pop()


# ---------------------------------------------------------------------------
# task statement extraction for the blind projection (only the ask; redacted)

#: Shorter payloads are harness noise (a path, a bare ``$NN`` ref), not an ask a
#: reader can judge; the DA-4 recovery uses the same floor.
MIN_STATEMENT_CHARS = 40

_REF_RE = re.compile(r"^\$\d+$")
_EPISODE_PROMPT_RE = re.compile(r"episode-\d+/prompt\.txt$")
#: User-role events that carry harness output or continuation boilerplate, not the ask.
_NON_STATEMENT_PREFIXES = ("OBSERVATION:", "Observation:", "<environment_context>")
_CONTINUATION_RE = re.compile(r"^Please continue on whatever approach you think is suitable\b")


def _redact_benchmarks(text: str) -> str:
    """Strip source/benchmark names with the atlas vocabulary (one redaction list)."""
    from evallab.production.run_atlas import redact_sources

    return redact_sources(text)


def _command_payload(content: str) -> str | None:
    """The ask inside a harness invocation: ``forge … -p '<ask>'`` or ``pilot task '<ask>'``.

    The shell has already been applied, so a quote inside the ask appears as
    ``'\\''``; :func:`shlex.split` undoes exactly that.
    """
    try:
        tokens = shlex.split(content, posix=True)
    except ValueError:
        return None
    for index, token in enumerate(tokens[:-1]):
        if token == "-p" or (token == "task" and index and tokens[index - 1] == "pilot"):
            return tokens[index + 1]
    return None


def _file_entries(raw: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """``raw["files"]`` as entries; the pinned shape is ``[{path, content, format}]``."""
    files = raw.get("files")
    if not isinstance(files, list):
        return []
    return [entry for entry in files if isinstance(entry, Mapping)]


def _entry_text(entry: Mapping[str, Any]) -> str:
    content = entry.get("content")
    return content if isinstance(content, str) else ""


def _ref_table(raw: Mapping[str, Any]) -> dict[str, str]:
    """A ``$NN`` value table the row itself carries (the dataset's elision artifact)."""
    table: dict[str, str] = {}
    for value in raw.values():
        if isinstance(value, Mapping):
            pairs: Iterator[tuple[Any, Any]] = iter(value.items())
        elif isinstance(value, list):
            pairs = ((f"${index}", item) for index, item in enumerate(value))
        else:
            continue
        for key, item in pairs:
            if isinstance(key, str) and _REF_RE.match(key) and isinstance(item, str):
                table[key] = item
    return table


def _user_ask(raw: Mapping[str, Any]) -> str | None:
    """The first natural-language user-role event (``$NN`` refs resolved inside the row)."""
    resolved = _ref_table(raw)
    for event in raw.get("events") or []:
        if not isinstance(event, Mapping):
            continue
        if (event.get("src") or event.get("role")) != "user":
            continue
        text = event.get("msg") if "msg" in event else event.get("content")
        if not isinstance(text, str):
            continue
        text = resolved.get(text.strip(), text.strip())
        if len(text) < MIN_STATEMENT_CHARS or _REF_RE.match(text):
            continue
        if text.startswith(_NON_STATEMENT_PREFIXES) or _CONTINUATION_RE.match(text):
            continue
        return text
    return None


def _extract_task_statement(raw: Mapping[str, Any]) -> str | None:
    """The ask the run was given, from the row's own files and then its user events.

    Order and filters match the DA-4 recovery (``ds-da4-…/recover_tasks.py``): the
    ``command.txt`` payload, else the first ``episode-N/prompt.txt``, else the
    first user-role statement. Never a label, split marker, placeholder or
    benchmark name (redaction happens at the call site).
    """
    if not isinstance(raw, Mapping):
        return None
    entries = _file_entries(raw)
    for entry in entries:
        if not str(entry.get("path") or "").endswith("command.txt"):
            continue
        payload = _command_payload(_entry_text(entry))
        if payload and len(payload.strip()) >= MIN_STATEMENT_CHARS:
            return payload.strip()
    prompts = sorted(
        (str(entry.get("path") or ""), _entry_text(entry))
        for entry in entries
        if _EPISODE_PROMPT_RE.search(str(entry.get("path") or ""))
    )
    for _path, content in prompts:
        if len(content.strip()) >= MIN_STATEMENT_CHARS:
            return content.strip()
    return _user_ask(raw)


# ---------------------------------------------------------------------------
# envelope A: TB2 raw_trace (typed events)


def _convert_agent_command(builder: _Steps, event: dict, index: int) -> bool:
    """An ``agent_command`` event; True when agent content was extracted."""
    stdout = event.get("stdout") or ""
    triples = _pilot_steps(stdout, caps=builder.caps)
    if triples:
        with _span(builder, f"events[{index}].stdout"):
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
                    "keystrokes": _bounded(command, MSG_CAP, builder.caps),
                    "command": _bounded(command, MSG_CAP, builder.caps),
                },
            }
        ]
        if command
        else None
    )
    with _span(builder, f"events[{index}]"):
        builder.agent(
            f"terminal command (exit {event.get('return_code')})" if command else "(output only)",
            calls,
            [{"source_call_id": call_id, "content": _bounded(obs, OBS_CAP, builder.caps)}]
            if obs
            else None,
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
                    else {"input": _cap(json.dumps(args), 4000)},
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
            args = {"input": _cap(str(cmd), 4000)} if cmd else {}
            maybe_path = str(cmd) if cmd and ("/" in str(cmd) or "." in str(cmd)) else ""
            if maybe_path:
                args.setdefault("path", _cap(maybe_path, 2000))
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
                args[field] = _cap(params[field], 4000)
        for field in ("old_str", "new_str", "old_text", "new_text"):
            if params.get(field):
                args[field] = _cap(params[field], 4000)
        command = params.get("command") or params.get("cmd") or params.get("code") or ""
        is_shell = name.lower() in ("bash", "exec", "shell") or (
            name == "str_replace_editor" and params.get("command") not in ("view",)
        )
        if command and is_shell:
            args.setdefault("keystrokes", _cap(command, 4000))
        if not args:
            args = {"input": _cap(json.dumps(params), 4000)}
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
                    _bounded(cmd, MSG_CAP, builder.caps),
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
                    pending_prose.append(_cap(str(summary).strip(), 2000))
                if rest:
                    pending_prose.append(_cap(rest, 2000))
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
            builder.user(f"[{role}] {_bounded(body, MSG_CAP, builder.caps)}")


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
                k: (_cap(str(v), 4000) if isinstance(v, str) else v)
                for k, v in obj.items()
                if k != "tool"
            }
            path = obj.get("filepath") or obj.get("file_path") or obj.get("path")
            if isinstance(path, str) and path:
                args.setdefault("file_path", _cap(path, 2000))
            builder.agent(
                _bounded(body, MSG_CAP, builder.caps),
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
    for index, event in enumerate(events):
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
        with _span(builder, f"events[{index}]"):
            if messages:
                for message in messages:
                    if isinstance(message, dict) and message.get("role") == "user":
                        builder.user(text_of(message.get("content")))
            try:
                content = output["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                content = ""
            builder.agent(text_of(content))
        ok = True
    return ok


# ---------------------------------------------------------------------------
# top-level conversion


def _convert_inner(
    trace_id: str,
    raw: dict,
    meta: dict,
    *,
    caps: bool = True,
) -> tuple[dict, dict, list[dict]]:
    """Convert one CheatBench trace to ATIF.

    Returns ``(doc, stats, regions)``; raises ``ValueError(reason)`` with a
    reason in ``EXCLUDED_REASONS`` when the row cannot be converted.
    ``regions`` are per-step source spans parallel to ``doc["steps"]``
    (plus one ``None``-field entry prepended for the context step by the
    viewer pipeline); they never enter the detector bytes.
    """
    model = str(meta.get("model") or "")
    builder = _Steps(model=model, caps=caps)
    envelope = "unknown"
    extra: dict[str, Any] = {}

    if (
        isinstance(raw.get("events"), list)
        and raw.get("events")
        and isinstance(raw["events"][0], dict)
        and "type" in raw["events"][0]
    ):
        envelope = "tb2_typed"
        for index, event in enumerate(raw["events"]):
            if not isinstance(event, dict):
                continue
            etype = event.get("type")
            if etype == "agent_command":
                _convert_agent_command(builder, event, index)
            elif etype == "agent_log":
                text = (event.get("text") or "").strip()
                if text:
                    with _span(builder, f"events[{index}].text"):
                        builder.agent(text)
            elif etype == "agent_episode":
                prompt = event.get("prompt")
                if isinstance(prompt, str) and prompt.strip():
                    with _span(builder, f"events[{index}].prompt"):
                        builder.observe(prompt)
                content, calls = _episode_calls(event.get("response"))
                if content or calls:
                    with _span(builder, f"events[{index}].response"):
                        builder.agent(content, calls or None)
            elif isinstance(event, dict) and any(k in event for k in ("msg", "tools", "obs")):
                _convert_msg_event(builder, event, index)
    elif (
        isinstance(raw.get("events"), list)
        and raw.get("events")
        and isinstance(raw["events"][0], dict)
        and {"step", "src", "msg"} <= set(raw["events"][0])
    ):
        envelope = "tb2_step"
        for index, event in enumerate(raw["events"]):
            if isinstance(event, dict):
                _convert_msg_event(builder, event, index)
    elif isinstance(raw.get("messages"), list):
        envelope = (
            "iquest"
            if raw.get("source") == "iquest" or _looks_iquest(raw["messages"])
            else "messages"
        )
        _convert_messages(builder, raw["messages"], envelope=envelope, base="messages")
    elif (
        isinstance(raw.get("events"), list)
        and raw.get("events")
        and all(isinstance(e, dict) and "role" in e for e in raw["events"][:3])
    ):
        envelope = _classify_message_events(raw["events"])
        _convert_messages(builder, raw["events"], envelope=envelope, base="events")
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
    statement = _extract_task_statement(raw)
    injected = False
    if statement:
        ask = _redact_benchmarks(statement)
        # Prefer the trace's own first user turn when it already shows the ask;
        # otherwise put the ask first. Never both, and never a placeholder.
        first_user = next((s for s in builder.steps if s.get("source") == "user"), None)
        shown = _redact_benchmarks(str(first_user.get("message") or "")) if first_user else ""
        if ask not in shown:
            builder.steps.insert(0, {"step_id": 0, "source": "user", "message": ask})
            injected = True
    # No ask recovered: no synthetic turn. The old fallback line named the
    # benchmark and the model and was not the task, so a reader could not tell
    # what the run had been asked to do from it.
    for idx, step in enumerate(builder.steps):
        step["step_id"] = idx
    # Labels, mechanisms and the source trace id stay in the corpus manifest;
    # the converted document carries evidence only.
    doc = {
        "schema_version": "ATIF-v1.6",
        "agent": {"name": "cheatbench/[source]", "version": "raw_trace.v1-import"},
        "steps": builder.steps,
    }
    stats = {
        "envelope": envelope,
        "has_statement": bool(statement),
        "statement_injected": injected,
        "n_steps": len(builder.steps),
        "n_tool_calls": builder.n_tool_calls,
        "has_tool_calls": builder.n_tool_calls > 0,
        "dropped_obs": builder.n_dropped_obs,
        **extra,
    }
    return doc, stats, builder.regions


def convert(trace_id: str, raw: dict, meta: dict, *, caps: bool = True) -> tuple[dict, dict]:
    """Convert one CheatBench trace to the actor-visible ATIF document.

    The document is the :func:`evallab.cheatbench_fidelity.blind_atif`
    projection (``item_id`` = ``trace_id``): no label, mechanism or span
    side-channel reaches a consumer. Returns ``(doc, stats)``; raises
    ``ValueError(reason)`` with a reason in ``EXCLUDED_REASONS`` when the row
    cannot be converted.
    """
    from evallab.cheatbench_fidelity import blind_atif

    doc, stats, _ = _convert_inner(trace_id, raw, meta, caps=caps)
    return blind_atif(doc, item_id=trace_id), stats


def _looks_iquest(messages: list[Any]) -> bool:
    for message in messages[:20]:
        if isinstance(message, dict) and "<function=" in text_of(message.get("content")):
            return True
    return False


def _classify_message_events(events: list[Any]) -> str:
    for event in events[:10]:
        if not isinstance(event, dict):
            continue
        text = text_of(event.get("content"))
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
            if any("[action]" in text_of(e.get("content")) for e in events if isinstance(e, dict))
            else "messages"
        )
    return "messages"


def _convert_msg_event(builder: _Steps, event: dict, index: int) -> None:
    src = str(event.get("src") or "agent").lower()
    msg = event.get("msg")
    msg_text = msg if isinstance(msg, str) else text_of(msg)
    calls = _msg_tool_calls(event.get("tools")) if event.get("tools") else []
    obs = event.get("obs")
    obs_text = obs if isinstance(obs, str) else text_of(obs)
    if src in ("user", "system"):
        if msg_text.strip():
            with _span(builder, f"events[{index}].msg"):
                builder.user(msg_text)
        return
    with _span(builder, f"events[{index}].msg"):
        builder.agent(msg_text, calls or None)
    if obs_text.strip():
        builder.observe(obs_text, field=f"events[{index}].obs")


def _convert_messages(
    builder: _Steps, messages: list[Any], *, envelope: str, base: str = "messages"
) -> None:
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        with _span(builder, f"{base}[{index}].content"):
            _convert_message(builder, message, envelope=envelope)


def _convert_message(builder: _Steps, message: dict, *, envelope: str) -> None:
    role = str(message.get("role") or "")
    text = text_of(message.get("content"))
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


# ---------------------------------------------------------------------------
# Viewer subset: the MiMo-relevant rows staged for the Trace Lab Scout viewer.
# Staging reuses the single converter above (same steps the detectors score)
# and runs the cheat-localization layer over its span side-channel, ending
# each transcript with a clearly-marked annotation message.
# ---------------------------------------------------------------------------


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


class CheatbenchError(Exception):
    """Pinned-fetch or conversion failure for the CheatBench corpus."""


_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")
RAW_SCHEMA = "cheatbench.raw_trace.v1"


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


