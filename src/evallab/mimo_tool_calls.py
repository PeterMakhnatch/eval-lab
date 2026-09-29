"""Deterministic normalizer for MiMo's native tool calls on the Terminus-2 route.

Under Terminus-2, MiMo-V2.6-Distill-Qwen-9B keeps the tool-call wrapper of its
training harnesses: ``<tool_call><function=NAME>…</function></tool_call>``.
HAR-90's Daytona trials recorded three shapes (1,610 turns in all):

- ``<function=exec>{"analysis": …, "plan": …, "commands": […]}``: a whole
  Terminus object behind the wrapper, with no closing tags (trial 1);
- ``<function=exec_command>{"keystrokes": "pwd", "duration": 0.1}``, one or
  more JSON-argument calls per turn (trial 2);
- ``<function=bash><parameter=command>ls -la /app</parameter>
  <parameter=duration>0.1</parameter>``, Qwen3-Coder XML arguments, one or
  more per turn (trials 3 and 4).

The keystrokes almost never end in a newline. In the model's own harnesses a
call executes its command. Terminus sends keystrokes verbatim, so HAR-90's
commands were typed but never run.

This module changes only what Terminus executes. The raw model text stays in
the chat history, the ATIF trajectory and the rollout details, because SFT and
RL train on the real tokens. :class:`MimoToolCallParser` wraps the stock
Terminus JSON parser:

1. :func:`normalize_mimo_tool_calls` rewrites a response built only from
   ``exec``/``exec_command``/``bash`` calls into one Terminus JSON object.
   - A wrapped Terminus object passes through verbatim.
   - A command call (``keystrokes`` or ``command``, optional ``duration``)
     becomes one of the ``commands``, in its original order.
   - Any other shape returns ``None``. The raw text then reaches the stock
     parser and gets its usual parse-error feedback.
2. :func:`executed_keystrokes` appends the Enter that the model's harnesses
   implied to every parsed command. It leaves empty keystrokes (pure waits)
   and lone tmux key names (``C-c``, ``Escape``, …) alone.

No native completion call occurs in HAR-90's trajectories, so none is mapped.
Completion stays Terminus's ``task_complete`` field, which passes through
inside a wrapped object.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

__all__ = [
    "MIMO_EXEC_FUNCTIONS",
    "MimoToolCallParser",
    "executed_keystrokes",
    "normalize_mimo_tool_calls",
]

#: Native function names whose calls map onto Terminus commands.
MIMO_EXEC_FUNCTIONS = frozenset({"exec", "exec_command", "bash"})

_CALL_OPENER = re.compile(r"(?:<tool_call>\s*)?<function=([^>\s]+)>")
_CALL_CLOSER = re.compile(r"\s*(?:</function>\s*)?(?:</tool_call>\s*)?")
_XML_PARAMETER = re.compile(r"\s*<parameter=([^>\s]+)>(.*?)</parameter>", re.DOTALL)
_COMMAND_KEYS = ("keystrokes", "command")

#: A lone tmux key name (optionally with C-/M-/S- modifiers or ``^X``). tmux
#: reads these as keys only when they are the whole argument, so appending a
#: newline would turn ``C-c`` into the literal text "C-c".
_TMUX_KEY_NAME = re.compile(
    r"(?:(?:[CMS]-)+\S|\^\S|(?:[CMS]-)*(?:Enter|Escape|Tab|BTab|BSpace|Space|Up|Down|Left|Right"
    r"|Home|End|PageUp|PgUp|PageDown|PgDn|NPage|PPage|Insert|IC|Delete|DC|KPEnter|F\d{1,2}))"
)


def _call_arguments(body: str) -> tuple[dict[str, Any], str | None, int] | None:
    """Parse one call body as JSON or XML parameters.

    Returns the arguments, the verbatim JSON text (``None`` for XML) and the
    offset where the arguments end.
    """
    start = len(body) - len(body.lstrip())
    if body.startswith("{", start):
        try:
            value, stop = json.JSONDecoder().raw_decode(body, start)
        except json.JSONDecodeError:
            return None
        return (value, body[start:stop], stop) if isinstance(value, dict) else None
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
    return (arguments, None, stop) if arguments else None


def _terminus_command(arguments: dict[str, Any]) -> dict[str, Any] | None:
    """Map one command call's arguments onto a Terminus command."""
    present = [key for key in _COMMAND_KEYS if key in arguments]
    if len(present) != 1 or not set(arguments) <= {present[0], "duration"}:
        return None
    keystrokes = arguments[present[0]]
    if not isinstance(keystrokes, str):
        return None
    command: dict[str, Any] = {"keystrokes": keystrokes}
    if "duration" in arguments:
        command["duration"] = arguments["duration"]
    return command


def normalize_mimo_tool_calls(response: str) -> str | None:
    """Return one Terminus JSON object for a native MiMo tool-call response.

    ``None`` means the response is not purely native calls. The caller then
    parses the raw text unchanged, so Terminus's own error feedback applies.
    """
    openers = list(_CALL_OPENER.finditer(response))
    if not openers:
        return None
    outside = response[: openers[0].start()].replace("</tool_call>", "").strip()
    commands: list[dict[str, Any]] = []
    passthrough: str | None = None
    for index, opener in enumerate(openers):
        if opener.group(1) not in MIMO_EXEC_FUNCTIONS:
            return None
        end = openers[index + 1].start() if index + 1 < len(openers) else len(response)
        body = response[opener.end() : end]
        parsed = _call_arguments(body)
        if parsed is None or _CALL_CLOSER.fullmatch(body, parsed[2]) is None:
            return None
        arguments, json_text, _ = parsed
        if "commands" in arguments and json_text is not None:
            if len(openers) != 1:
                return None
            passthrough = json_text
            continue
        command = _terminus_command(arguments)
        if command is None:
            return None
        commands.append(command)
    if passthrough is not None:
        return passthrough
    return json.dumps({"analysis": outside, "plan": "", "commands": commands})


def executed_keystrokes(keystrokes: str) -> str:
    """Return the keystrokes Terminus sends for one MiMo command."""
    if not keystrokes or keystrokes[-1] in "\r\n" or _TMUX_KEY_NAME.fullmatch(keystrokes):
        return keystrokes
    return keystrokes + "\n"


class _ParsedCommand(Protocol):
    keystrokes: str


class _ParseResult(Protocol):
    commands: list[_ParsedCommand]


class _TerminusParser(Protocol):
    def parse_response(self, response: str) -> Any: ...


class MimoToolCallParser:
    """Terminus JSON parser that executes MiMo's native tool calls.

    Terminus records ``llm_response.content``, never the parser input, so the
    raw model text is untouched everywhere it is stored.
    """

    def __init__(self, inner: _TerminusParser) -> None:
        self._inner = inner

    def parse_response(self, response: str) -> Any:
        result: _ParseResult = self._inner.parse_response(
            normalize_mimo_tool_calls(response) or response
        )
        for command in result.commands:
            command.keystrokes = executed_keystrokes(command.keystrokes)
        return result
