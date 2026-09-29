"""Deterministic normalizer for MiMo's native tool calls on the Terminus-2 route.

Under Terminus-2, MiMo-V2.6-Distill-Qwen-9B keeps the tool-call wrapper of its
training harnesses: ``<tool_call><function=NAME>…</function></tool_call>``.
HAR-90's Daytona trials recorded five shapes (2,310 turns in all):

- ``<function=exec>{"analysis": …, "plan": …, "commands": […]}``: a whole
  Terminus object behind the wrapper, with no closing tags (trial 1);
- ``<function=exec_command>{"keystrokes": "pwd", "duration": 0.1}``, one or
  more JSON-argument calls per turn (trial 2);
- ``<function=bash><parameter=command>ls -la /app</parameter>
  <parameter=duration>0.1</parameter>``, Qwen3-Coder XML arguments, one or
  more per turn (0036-d, 0758-b, 0036-e);
- the same bash call with a ``description`` parameter instead of
  ``duration``, as in Claude Code's Bash tool: after 0758-d's summarization
  every one of its 177 turns used it, two calls per turn;
- a bare Terminus object followed by ``</parameter><parameter=duration>0.5
  </parameter></function></tool_call>``: the object filled a bash call's
  ``command`` parameter whose opener never came (0758-c).

The keystrokes almost never end in a newline. In the model's own harnesses a
call executes its command. Terminus sends keystrokes verbatim, so HAR-90's
commands were typed but never run. MiMo also writes raw newlines inside JSON
strings, as it would inside an XML parameter; strict JSON rejects them.

This module changes only what Terminus executes. The raw model text stays in
the chat history, the ATIF trajectory and the rollout details, because SFT and
RL train on the real tokens. :class:`MimoToolCallParser` wraps the stock
Terminus JSON parser:

1. :func:`normalize_mimo_tool_calls` rewrites a native response into one
   Terminus JSON object. JSON is decoded with raw control characters allowed.
   - A wrapped Terminus object passes through, re-serialized.
   - A response built only from ``exec``/``exec_command``/``bash`` command
     calls (``keystrokes`` or ``command``, optional ``duration``) becomes one
     ``commands`` entry per call, in order. A ``description`` parameter only
     labels the call, so it is dropped; any other parameter rejects the turn.
   - A bare Terminus object followed by the native closing markup, or valid
     only with raw control characters, is re-serialized without the markup.
     The markup's ``duration`` is dropped; the object's commands keep theirs.
   - Anything else returns ``None``: valid Terminus JSON, prose, and unknown
     shapes reach the stock parser untouched and get its usual feedback.
2. :func:`executed_keystrokes` appends the Enter that the model's harnesses
   implied to every parsed command. It leaves empty keystrokes (pure waits)
   and lone tmux key names (``C-c``, ``Escape``, …) alone.
3. :func:`prose_completion` recognizes MiMo's native end of episode: a reply
   with no tool call. After HAR-90's 0036-e solved its task, the model
   answered with the same prose summary 50 times, each a parse error, until
   the timeout. The parser maps such a turn to ``task_complete: true`` with no
   commands, and only when all of these hold:

   - the completion's ``finish_reason`` is ``stop``, so a reply cut off at
     ``max_tokens`` never ends an episode;
   - the text left after the reasoning split is non-empty;
   - it carries no tool-call markup and no JSON object;
   - it is not Harbor's own fallback reply.

   Terminus's double confirmation still applies: the first mapped turn gets
   Terminus's "are you sure" prompt, and only a second completion ends the
   episode. The adapter flags every mapped step in the trajectory.

No native completion call occurs in HAR-90's trajectories, so none is mapped.
Anything the rules above do not cover reaches Terminus unchanged and gets its
usual parse-error feedback.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Protocol

__all__ = [
    "HARBOR_FALLBACK_RESPONSE",
    "MIMO_EXEC_FUNCTIONS",
    "MimoToolCallParser",
    "executed_keystrokes",
    "normalize_mimo_tool_calls",
    "prose_completion",
]

#: Native function names whose calls map onto Terminus commands.
MIMO_EXEC_FUNCTIONS = frozenset({"exec", "exec_command", "bash"})

_CALL_OPENER = re.compile(r"(?:<tool_call>\s*)?<function=([^>\s]+)>")
_CALL_CLOSER = re.compile(r"\s*(?:</function>\s*)?(?:</tool_call>\s*)?")
#: The native markup after a bare Terminus object: the rest of an XML call
#: whose ``command`` parameter the object filled, without its opener.
_WRAPPER_TAIL = re.compile(
    r"\s*</parameter>\s*(?:<parameter=[^>\s]+>.*?</parameter>\s*)*(?:</function>\s*)?"
    r"(?:</tool_call>\s*)?",
    re.DOTALL,
)
_XML_PARAMETER = re.compile(r"\s*<parameter=([^>\s]+)>(.*?)</parameter>", re.DOTALL)
_COMMAND_KEYS = ("keystrokes", "command")
#: Parameters that label a call without changing what it executes.
_LABEL_KEYS = frozenset({"description"})
#: MiMo writes raw newlines inside JSON strings, as it would inside an XML
#: parameter; strict JSON rejects them as control characters.
_LENIENT_JSON = json.JSONDecoder(strict=False)
#: Harbor's stand-in reply when even its summarization fallback call fails
#: (``Terminus2._query_llm``). It is not model output, so it never ends an
#: episode.
HARBOR_FALLBACK_RESPONSE = "Technical difficulties. Please continue with the task."
#: Any piece of native tool-call markup.
_NATIVE_MARKUP = re.compile(r"</?tool_call>|<function=|</function>|<parameter=|</parameter>")

#: A lone tmux key name (optionally with C-/M-/S- modifiers or ``^X``). tmux
#: reads these as keys only when they are the whole argument, so appending a
#: newline would turn ``C-c`` into the literal text "C-c".
_TMUX_KEY_NAME = re.compile(
    r"(?:(?:[CMS]-)+\S|\^\S|(?:[CMS]-)*(?:Enter|Escape|Tab|BTab|BSpace|Space|Up|Down|Left|Right"
    r"|Home|End|PageUp|PgUp|PageDown|PgDn|NPage|PPage|Insert|IC|Delete|DC|KPEnter|F\d{1,2}))"
)


def _call_arguments(body: str) -> tuple[dict[str, Any], bool, int] | None:
    """Parse one call body as JSON or XML parameters.

    Returns the arguments, whether they were JSON, and the offset where the
    arguments end.
    """
    start = len(body) - len(body.lstrip())
    if body.startswith("{", start):
        try:
            value, stop = _LENIENT_JSON.raw_decode(body, start)
        except json.JSONDecodeError:
            return None
        return (value, True, stop) if isinstance(value, dict) else None
    arguments: dict[str, Any] = {}
    stop = 0
    while (match := _XML_PARAMETER.match(body, stop)) is not None:
        name, value = match.group(1), match.group(2)
        if name in arguments:
            return None
        # Qwen3-Coder XML values may sit on their own lines; one newline on
        # each side belongs to the markup, as in SGLang's qwen3_coder parser.
        value = value.removeprefix("\n").removesuffix("\n")
        if name == "duration":
            try:
                arguments[name] = float(value)
            except ValueError:
                return None
        else:
            arguments[name] = value
        stop = match.end()
    return (arguments, False, stop) if arguments else None


def _bare_terminus_object(response: str) -> str | None:
    """Canonicalize a Terminus object that strict JSON or its tail breaks.

    Valid Terminus JSON with nothing after it returns ``None`` and reaches the
    stock parser untouched, as does an object followed by anything other
    than native closing markup.
    """
    start = len(response) - len(response.lstrip())
    if not response.startswith("{", start):
        return None
    try:
        value, stop = _LENIENT_JSON.raw_decode(response, start)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or "commands" not in value:
        return None
    tail = response[stop:]
    if tail.strip():
        if _WRAPPER_TAIL.fullmatch(tail) is None:
            return None
    else:
        try:
            json.loads(response[start:stop])
        except json.JSONDecodeError:
            pass
        else:
            return None
    return json.dumps(value)


def _terminus_command(arguments: dict[str, Any]) -> dict[str, Any] | None:
    """Map one command call's arguments onto a Terminus command."""
    present = [key for key in _COMMAND_KEYS if key in arguments]
    if len(present) != 1 or not set(arguments) <= {present[0], "duration", *_LABEL_KEYS}:
        return None
    keystrokes = arguments[present[0]]
    if not isinstance(keystrokes, str):
        return None
    command: dict[str, Any] = {"keystrokes": keystrokes}
    if "duration" in arguments:
        command["duration"] = arguments["duration"]
    return command


def normalize_mimo_tool_calls(response: str) -> str | None:
    """Return one Terminus JSON object for a native MiMo response.

    ``None`` means the response needs no rewrite or is not a native shape.
    The caller then parses the raw text unchanged, so Terminus's own error
    feedback applies.
    """
    openers = list(_CALL_OPENER.finditer(response))
    if not openers:
        return _bare_terminus_object(response)
    outside = response[: openers[0].start()].replace("</tool_call>", "").strip()
    commands: list[dict[str, Any]] = []
    passthrough: dict[str, Any] | None = None
    for index, opener in enumerate(openers):
        if opener.group(1) not in MIMO_EXEC_FUNCTIONS:
            return None
        end = openers[index + 1].start() if index + 1 < len(openers) else len(response)
        body = response[opener.end() : end]
        parsed = _call_arguments(body)
        if parsed is None or _CALL_CLOSER.fullmatch(body, parsed[2]) is None:
            return None
        arguments, from_json, _ = parsed
        if "commands" in arguments and from_json:
            if len(openers) != 1:
                return None
            passthrough = arguments
            continue
        command = _terminus_command(arguments)
        if command is None:
            return None
        commands.append(command)
    if passthrough is not None:
        return json.dumps(passthrough)
    return json.dumps({"analysis": outside, "plan": "", "commands": commands})


def executed_keystrokes(keystrokes: str) -> str:
    """Return the keystrokes Terminus sends for one MiMo command."""
    if not keystrokes or keystrokes[-1] in "\r\n" or _TMUX_KEY_NAME.fullmatch(keystrokes):
        return keystrokes
    return keystrokes + "\n"


def _has_json_object(text: str) -> bool:
    start = text.find("{")
    while start != -1:
        try:
            value, _ = _LENIENT_JSON.raw_decode(text, start)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(value, dict):
                return True
        start = text.find("{", start + 1)
    return False


def prose_completion(response: str) -> str | None:
    """Return the answer of a prose-only turn, or ``None`` for anything else.

    Reasoning inside ``<think>`` tags is split off first, as a server-side
    reasoning parser would. What remains must be non-empty, free of tool-call
    markup and JSON objects, and not Harbor's fallback reply. The caller
    checks ``finish_reason`` separately.
    """
    text = response
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    text = text.split("<think>", 1)[0].strip()
    if not text or text == HARBOR_FALLBACK_RESPONSE or _NATIVE_MARKUP.search(text):
        return None
    if _has_json_object(text):
        return None
    return text


class _ParsedCommand(Protocol):
    keystrokes: str


class _ParseResult(Protocol):
    commands: list[_ParsedCommand]


class _TerminusParser(Protocol):
    def parse_response(self, response: str) -> Any: ...


class MimoToolCallParser:
    """Terminus JSON parser that executes MiMo's native tool calls.

    Terminus records ``llm_response.content``, never the parser input, so the
    raw model text is untouched everywhere it is stored. ``finish_reason``
    returns the finish reason of the completion being parsed; only ``stop``
    lets a prose-only turn end the episode. :attr:`last_prose_completion`
    tells the caller whether the latest parsed turn was mapped that way.
    """

    def __init__(
        self, inner: _TerminusParser, *, finish_reason: Callable[[], str | None]
    ) -> None:
        self._inner = inner
        self._finish_reason = finish_reason
        self.last_prose_completion = False
        #: Recording-only HAR-92 state: the normalized text the latest parse
        #: acted on (``None`` when the raw response reached the inner parser
        #: untouched), plus that parse's error. Parser decisions are unchanged.
        self.last_normalized: str | None = None
        self.last_error: str | None = None

    def parse_response(self, response: str) -> Any:
        normalized = normalize_mimo_tool_calls(response)
        answer = (
            prose_completion(response)
            if normalized is None and self._finish_reason() == "stop"
            else None
        )
        self.last_prose_completion = answer is not None
        if answer is not None:
            normalized = json.dumps(
                {"analysis": answer, "plan": "", "commands": [], "task_complete": True}
            )
        self.last_normalized = normalized
        result: _ParseResult = self._inner.parse_response(normalized or response)
        for command in result.commands:
            command.keystrokes = executed_keystrokes(command.keystrokes)
        self.last_error = getattr(result, "error", None)
        return result
