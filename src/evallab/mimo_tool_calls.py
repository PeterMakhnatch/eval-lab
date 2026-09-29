"""Deterministic normalizer for MiMo's native tool calls on the Terminus-2 route.

MiMo-V2.6-Distill-Qwen-9B was tuned in a harness whose tool is
``exec_command(keystrokes, duration)``. Under Terminus-2 it keeps that
format. HAR-90 recorded 320 turns of it in two shapes:

- ``<tool_call><function=exec>{"analysis": …, "plan": …, "commands": […]}``:
  a whole Terminus object behind the wrapper, with no closing tags;
- ``<tool_call><function=exec_command>{"keystrokes": "pwd", "duration": 0.1}
  </function></tool_call>``, one or more calls per turn.

In both shapes the keystrokes almost never end in a newline. In the native
harness a call executes its command; in Terminus, keystrokes are sent verbatim,
so HAR-90's commands were typed but never run.

This module changes only what Terminus executes. The raw model text stays in
the chat history, the ATIF trajectory and the rollout details, because SFT and
RL train on the real tokens. :class:`MimoToolCallParser` wraps the stock
Terminus JSON parser:

1. :func:`normalize_mimo_tool_calls` rewrites a response built only from
   ``exec``/``exec_command`` calls into one Terminus JSON object. A wrapped
   Terminus object passes through verbatim. Exec calls become ``commands`` in
   their original order. Any other shape returns ``None``, so the raw text
   reaches the stock parser and gets its usual parse-error feedback.
2. :func:`executed_keystrokes` appends the Enter the model's harness implied
   to every parsed command. Empty keystrokes (pure waits) and lone tmux key
   names (``C-c``, ``Escape``, …) are left alone.

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
MIMO_EXEC_FUNCTIONS = frozenset({"exec", "exec_command"})

_CALL_OPENER = re.compile(r"(?:<tool_call>\s*)?<function=([^>\s]+)>")
_CALL_CLOSER = re.compile(r"\s*(?:</function>\s*)?(?:</tool_call>\s*)?")
_EXEC_ARGUMENT_KEYS = frozenset({"keystrokes", "duration"})

#: A lone tmux key name (optionally with C-/M-/S- modifiers or ``^X``). tmux
#: reads these as keys only when they are the whole argument, so appending a
#: newline would turn ``C-c`` into the literal text "C-c".
_TMUX_KEY_NAME = re.compile(
    r"(?:(?:[CMS]-)+\S|\^\S|(?:[CMS]-)*(?:Enter|Escape|Tab|BTab|BSpace|Space|Up|Down|Left|Right"
    r"|Home|End|PageUp|PgUp|PageDown|PgDn|NPage|PPage|Insert|IC|Delete|DC|KPEnter|F\d{1,2}))"
)


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
    decoder = json.JSONDecoder()
    for index, opener in enumerate(openers):
        if opener.group(1) not in MIMO_EXEC_FUNCTIONS:
            return None
        end = openers[index + 1].start() if index + 1 < len(openers) else len(response)
        body = response[opener.end() : end]
        start = len(body) - len(body.lstrip())
        try:
            value, stop = decoder.raw_decode(body, start)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict) or _CALL_CLOSER.fullmatch(body, stop) is None:
            return None
        if "commands" in value:
            if len(openers) != 1:
                return None
            passthrough = body[start:stop]
        elif set(value) <= _EXEC_ARGUMENT_KEYS and isinstance(value.get("keystrokes"), str):
            commands.append(value)
        else:
            return None
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
