"""Deterministic normalizer for MiMo's native tool calls on the Terminus-2 route.

Under Terminus-2, MiMo-V2.6-Distill-Qwen-9B keeps the tool-call wrapper of its
training harnesses: ``<tool_call><function=NAME>…</function></tool_call>``.
HAR-90's Daytona trials recorded six shapes (2,718 turns in all), and the
HAR-81 pilot a seventh:

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
  ``command`` parameter whose opener never came (0758-c);
- ``<function=task_complete><parameter=task_complete>true</parameter>``, the
  model's own completion call, alone or after a one-line summary: 0036-g
  solved its task, and after Terminus's "are you sure" sent this call 307
  times until the timeout;
- ``<function=command>ls -la /app</function>``: a call whose body is the
  command text itself, with no parameters, one or more per turn (HAR-81's
  candidate-2684: its first 6 turns, all parse errors, before it wrapped a
  Terminus object in the same call).

The keystrokes almost never end in a newline. In the model's own harnesses a
call executes its command. Terminus sends keystrokes verbatim, so HAR-90's
commands were typed but never run. MiMo also writes raw newlines inside JSON
strings, as it would inside an XML parameter; strict JSON rejects them. It
leaves quotes inside JSON strings unescaped too: 105 HAR-81 pilot turns were
rejected for that, 97 of them sending one of two messages over and over.

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
   - A turn whose only call is ``task_complete``, with the argument ``true``
     or no arguments, becomes ``task_complete: true`` with no commands. Text
     before the call becomes the analysis. Any other argument, or a
     completion call beside other calls, rejects the turn.
   - A ``command`` call whose body is plain text becomes one command with
     that text; one newline on each side belongs to the markup, as for XML
     values. Such calls mix with the command calls above, in order. A
     ``command`` call with a JSON body is left alone: the stock parser
     already finds the Terminus object inside it.
   - A Terminus object, bare or as the body of one of those calls, that does
     not decode because a string holds unescaped ``"`` is read every way its
     quotes allow: each may end the string or be text. Only readings with
     exactly Terminus's keys and field types count, since MiMo never writes
     others. The one that reads the fewest quotes as text is re-serialized;
     more would swallow real structure, such as the next command. A tie, no
     such reading, or a search past its budget rejects the turn. Text after
     the object, such as closing markup, is dropped, as the stock parser
     drops it. The keystrokes are the model's own, so a command whose stray
     quote leaves the shell's quoting unbalanced runs as written.
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

A native ``task_complete`` call passes through the same double confirmation;
it needs no ``finish_reason`` check, because its closing markup shows the
call is whole. Anything the rules above do not cover reaches Terminus
unchanged and gets its usual parse-error feedback.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from typing import Any, Protocol

__all__ = [
    "HARBOR_FALLBACK_RESPONSE",
    "MIMO_COMPLETE_FUNCTION",
    "MIMO_EXEC_FUNCTIONS",
    "MIMO_RAW_COMMAND_FUNCTION",
    "MimoToolCallParser",
    "executed_keystrokes",
    "normalize_mimo_tool_calls",
    "prose_completion",
]

#: Native function names whose calls map onto Terminus commands.
MIMO_EXEC_FUNCTIONS = frozenset({"exec", "exec_command", "bash"})
#: The native function name of MiMo's end-of-episode call.
MIMO_COMPLETE_FUNCTION = "task_complete"
#: The native function name whose call body is the command text itself.
MIMO_RAW_COMMAND_FUNCTION = "command"

_CALL_OPENER = re.compile(r"(?:<tool_call>\s*)?<function=([^>\s]+)>")
_CALL_CLOSER = re.compile(r"\s*(?:</function>\s*)?(?:</tool_call>\s*)?")
#: A call with no arguments must at least close its function tag.
_EMPTY_CALL_BODY = re.compile(r"\s*</function>\s*(?:</tool_call>\s*)?")
#: A raw ``command`` call body: text up to its one closing tag, holding no
#: other markup.
_RAW_CALL_BODY = re.compile(
    r"((?:(?!</function>|</tool_call>|<parameter=).)+)</function>\s*(?:</tool_call>\s*)?",
    re.DOTALL,
)
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
#: Calls whose JSON body may hold a whole Terminus object.
_OBJECT_FUNCTIONS = MIMO_EXEC_FUNCTIONS | {MIMO_RAW_COMMAND_FUNCTION}
#: Pieces of JSON for the inner-quote search: keys are read strictly, so only
#: string values have more than one reading.
_JSON_KEY = re.compile(r'"([^"\\\x00-\x1f]*)"')
_JSON_SCALAR = re.compile(r"true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")
#: An escape (skipped whole) or an unescaped quote inside a JSON string.
_STRING_STOP = re.compile(r'\\.|"', re.DOTALL)
#: Terminus's object keys, and the ones the stock parser requires.
_OBJECT_KEYS = frozenset({"analysis", "plan", "commands", "task_complete"})
_REQUIRED_KEYS = frozenset({"analysis", "plan", "commands"})
_COMMAND_FIELDS = frozenset({"keystrokes", "duration"})
#: Quotes the inner-quote search may try as string ends before giving up.
_REPAIR_BUDGET = 4096
#: A decoded JSON value, the offset after it, and how many quotes it reads
#: as string characters.
type _Readings = Iterator[tuple[Any, int, int]]
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


def _raw_command(body: str) -> dict[str, Any] | None:
    """Map a ``command`` call whose body is the command text itself."""
    if body.lstrip().startswith("{"):
        return None
    match = _RAW_CALL_BODY.fullmatch(body)
    if match is None:
        return None
    keystrokes = match.group(1).removeprefix("\n").removesuffix("\n")
    return {"keystrokes": keystrokes} if keystrokes.strip() else None


def _completion_call(body: str) -> bool:
    """Whether one call body completes the task: ``true`` or no arguments."""
    parsed = _call_arguments(body)
    if parsed is None:
        return _EMPTY_CALL_BODY.fullmatch(body) is not None
    arguments, _, stop = parsed
    if _CALL_CLOSER.fullmatch(body, stop) is None:
        return False
    if not arguments:
        return True
    value = arguments.get(MIMO_COMPLETE_FUNCTION)
    return set(arguments) == {MIMO_COMPLETE_FUNCTION} and (value is True or value == "true")


class _RepairBudgetExceeded(Exception):
    """The inner-quote search tried more string ends than it may."""


def _skip_ws(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t\n\r":
        pos += 1
    return pos


def _string_readings(text: str, pos: int, budget: list[int]) -> _Readings:
    """Every reading of the string opening at ``pos``, shortest first.

    Any unescaped ``"`` may end the string or be one of its characters. Each
    reading yields the decoded value, the offset after the closing quote,
    and how many quotes it reads as characters.
    """
    decoded = ""
    chunk = pos + 1
    quotes = 0
    for stop in _STRING_STOP.finditer(text, chunk):
        if stop.group() != '"':
            continue
        budget[0] -= 1
        if budget[0] < 0:
            raise _RepairBudgetExceeded
        # Escapes never span an unescaped quote, so each piece decodes alone.
        try:
            piece = _LENIENT_JSON.decode('"' + text[chunk : stop.start()] + '"')
        except json.JSONDecodeError:
            return  # an invalid escape stays in every longer reading
        yield decoded + piece, stop.end(), quotes
        decoded += piece + '"'
        chunk = stop.end()
        quotes += 1


def _value_readings(text: str, pos: int, budget: list[int]) -> _Readings:
    if text.startswith('"', pos):
        yield from _string_readings(text, pos, budget)
    elif text.startswith("{", pos):
        yield from _member_readings(text, _skip_ws(text, pos + 1), {}, 0, budget, first=True)
    elif text.startswith("[", pos):
        yield from _element_readings(text, _skip_ws(text, pos + 1), [], 0, budget, first=True)
    elif (match := _JSON_SCALAR.match(text, pos)) is not None:
        yield json.loads(match.group()), match.end(), 0


def _member_readings(
    text: str,
    pos: int,
    members: dict[str, Any],
    quotes: int,
    budget: list[int],
    *,
    first: bool = False,
) -> _Readings:
    if first and text.startswith("}", pos):
        yield members, pos + 1, quotes
        return
    key = _JSON_KEY.match(text, pos)
    if key is None or key.group(1) in members:
        return
    colon = _skip_ws(text, key.end())
    if not text.startswith(":", colon):
        return
    for value, end, inner in _value_readings(text, _skip_ws(text, colon + 1), budget):
        after = _skip_ws(text, end)
        grown = {**members, key.group(1): value}
        if text.startswith(",", after):
            yield from _member_readings(
                text, _skip_ws(text, after + 1), grown, quotes + inner, budget
            )
        elif text.startswith("}", after):
            yield grown, after + 1, quotes + inner


def _element_readings(
    text: str,
    pos: int,
    elements: list[Any],
    quotes: int,
    budget: list[int],
    *,
    first: bool = False,
) -> _Readings:
    if first and text.startswith("]", pos):
        yield elements, pos + 1, quotes
        return
    for value, end, inner in _value_readings(text, pos, budget):
        after = _skip_ws(text, end)
        grown = [*elements, value]
        if text.startswith(",", after):
            yield from _element_readings(
                text, _skip_ws(text, after + 1), grown, quotes + inner, budget
            )
        elif text.startswith("]", after):
            yield grown, after + 1, quotes + inner


def _terminus_shaped(value: Any) -> bool:
    """Whether a decoded object has exactly Terminus's shape.

    The stock parser also accepts other keys, with a warning; MiMo's objects
    never carry any, so a reading that needs one is not what the model wrote.
    """
    if not isinstance(value, dict) or not _REQUIRED_KEYS <= value.keys() <= _OBJECT_KEYS:
        return False
    if not isinstance(value["analysis"], str) or not isinstance(value["plan"], str):
        return False
    if not isinstance(value.get("task_complete", False), (bool, str)):
        return False
    commands = value["commands"]
    return isinstance(commands, list) and all(
        isinstance(command, dict)
        and "keystrokes" in command
        and command.keys() <= _COMMAND_FIELDS
        and isinstance(command["keystrokes"], str)
        and type(command.get("duration", 0.0)) in (int, float)
        for command in commands
    )


def _terminus_object_start(response: str) -> int | None:
    """Where the Terminus object of a bare or call-wrapped response opens."""
    start = _skip_ws(response, 0)
    if response.startswith("{", start):
        return start
    opener = _CALL_OPENER.search(response)
    if opener is None or opener.group(1) not in _OBJECT_FUNCTIONS:
        return None
    start = _skip_ws(response, opener.end())
    return start if response.startswith("{", start) else None


def _repair_inner_quotes(response: str) -> str | None:
    """Read a Terminus object that does not decode because of inner quotes.

    Applies to the object opening a bare response or the first
    ``exec``/``exec_command``/``bash``/``command`` call, and only when it
    does not decode as written. Every way of reading its unescaped quotes as
    string characters is tried. Among the readings with Terminus's shape,
    the one that reads the fewest quotes as characters wins and is
    re-serialized. Readings with more swallow real structure, e.g. one
    command's keystrokes running on through the next command. No such
    reading, a tie for the fewest, or a search over budget returns ``None``.
    """
    start = _terminus_object_start(response)
    if start is None:
        return None
    try:
        _LENIENT_JSON.raw_decode(response, start)
    except json.JSONDecodeError:
        pass
    else:
        return None
    best: list[Any] = []
    fewest = -1
    budget = [_REPAIR_BUDGET]
    try:
        for value, _, quotes in _value_readings(response, start, budget):
            if not _terminus_shaped(value) or 0 <= fewest < quotes:
                continue
            if quotes != fewest:
                best.clear()
                fewest = quotes
            best.append(value)
    except _RepairBudgetExceeded:
        return None
    return json.dumps(best[0]) if len(best) == 1 else None


def normalize_mimo_tool_calls(response: str) -> str | None:
    """Return one Terminus JSON object for a native MiMo response.

    ``None`` means the response needs no rewrite or is not a native shape.
    The caller then parses the raw text unchanged, so Terminus's own error
    feedback applies.
    """
    return _rewrite_native_calls(response) or _repair_inner_quotes(response)


def _rewrite_native_calls(response: str) -> str | None:
    """Rewrite native tool calls into one Terminus JSON object, or ``None``."""
    openers = list(_CALL_OPENER.finditer(response))
    if not openers:
        return _bare_terminus_object(response)
    outside = response[: openers[0].start()].replace("</tool_call>", "").strip()
    if openers[0].group(1) == MIMO_COMPLETE_FUNCTION:
        if len(openers) != 1 or not _completion_call(response[openers[0].end() :]):
            return None
        return json.dumps(
            {"analysis": outside, "plan": "", "commands": [], "task_complete": True}
        )
    commands: list[dict[str, Any]] = []
    passthrough: dict[str, Any] | None = None
    for index, opener in enumerate(openers):
        end = openers[index + 1].start() if index + 1 < len(openers) else len(response)
        body = response[opener.end() : end]
        if opener.group(1) == MIMO_RAW_COMMAND_FUNCTION:
            raw_command = _raw_command(body)
            if raw_command is None:
                return None
            commands.append(raw_command)
            continue
        if opener.group(1) not in MIMO_EXEC_FUNCTIONS:
            return None
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
