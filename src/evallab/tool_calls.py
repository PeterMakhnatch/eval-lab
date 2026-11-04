"""Normalise a few common, confidently harmless tool calls into one small action vocabulary.

``normalize_tool_call`` is pure: it maps one call (name plus arguments) to a
:class:`ToolAction`. ``name`` and ``arguments`` of the action are the canonical
call the existing shell/editor analysis (``trace_anatomy``, ``hack_drop`` L0,
``tripwires``) already understands, so consumers reuse their own rules instead
of adding a second engine. ``kind``, ``text`` and ``path`` serve display.

Design: safety must not depend on parsing. This module is an allow-list, not a
shell parser. It recognises only inputs that it can check completely and that
are harmless to read as plain text; everything else is ``unknown`` and carries
the original call unchanged. Consumers treat ``unknown`` as "could not read
this" (L0 routes it to review), so a wrong ``shell`` answer would turn a
fail-safe into an evasion while a wrong ``unknown`` only costs a review.
Consumers must therefore also scan the raw call (see ``hack_drop`` L0) for
protected targets in calls the normaliser did not turn into a plain shell,
read, write or edit action.

Recognised:

- Terminus-2 ``bash_command {keystrokes, duration}``: exactly one line, newline
  terminated, typed while the terminal is known to be at an empty shell
  prompt. The line must be an allow-listed command (``ls``, ``cat``/``head``/
  ``grep`` with operands, ``pytest``, ``python3 script.py``, read-only ``git``,
  ``pip install``, ...), optionally chained with ``&&`` or piped into a
  filter. No quotes-with-expansion, ``$``, backticks, backslashes, ``;``,
  redirections (other than ``2>&1`` and ``2>/dev/null``), comments, subshells,
  heredocs, multiple lines, control characters or tmux key names.
- ``shell {command, keystrokes}`` bundle exports with equal fields: the same
  allow-list.
- ``mark_task_complete`` / ``task_complete`` with no payload, and a short list
  of planning, waiting and tool-discovery calls carrying only descriptive
  arguments: ``noop``.
- ``text_editor``: exactly the documented argument sets of ``view``,
  ``create``, ``str_replace``, ``insert`` and ``undo_edit``, with an already
  clean path (no ``.``/``..``/``//`` segments, no padding).
- ``glob {path, pattern}`` with one clean path and a plain pattern, and
  notebook ``edit_file`` / ``edit_notebook`` with exactly the known keys.

Terminal context is the caller's state: start with ``shell_ready=True`` (a
Terminus session begins at a shell), pass it in, set it to
``action.shell_after`` after each terminal call within one step, and to
:func:`terminal_shell_ready` of the step's observation once the step ends.
"""

from __future__ import annotations

import contextlib
import json
import posixpath
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

ToolActionKind = Literal["shell", "read", "write", "edit", "noop", "unknown"]

_TERMINAL_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
# A prompt line is user@host:path followed by # or $ and nothing else: typed or echoed text after it fails the match.
_SHELL_PROMPT = re.compile(r"(?:\([^()\n]{1,40}\) ?)?[\w.-]+@[\w.-]+:[~/][^\s#$]*[#$]")
_COMPLETE_CONFIRMATION = "Are you sure you want to mark the task as complete?"
# Everything except tab and newline: C0 controls (including CR), DEL and C1 controls.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_KEY_NAME = re.compile(
    r"(?:[CMS]-.*|Enter|Escape|Tab|BTab|BSpace|Space|Up|Down|Left|Right|Home|End|PageUp|PageDown|PgUp|PgDn|DC|IC|F\d{1,2})"
)
_NOOP_TOOLS = frozenset({
    "sleep", "wait", "close_file", "submit_final_answer", "exit", "todowrite", "toolsearch",
})
# Every tool name this module can turn into a recognised action; callers apply their raw-text backstop to these.
HANDLED_TOOL_NAMES = _NOOP_TOOLS | frozenset({
    "bash_command", "shell", "text_editor", "glob", "edit_file", "edit_notebook", "mark_task_complete", "task_complete",
})
_NOOP_ARGUMENTS = frozenset({"explanation", "file_path", "filepath", "input", "i"})
_GLOB_ARGUMENTS = frozenset({"path", "pattern", "i", "hidden", "gitignore", "limit"})
_GLOB_PATTERN = re.compile(r"[\w.*?\[\]/-]+")
_NOTEBOOK_ARGUMENTS = frozenset({"file_path", "filepath", "explanation", "file_edit_payload", "cell_type"})
_NOTEBOOK_PAYLOAD = frozenset({
    "cell_type", "notebook_cell_index", "cell_index", "notebook_edit_type", "action", "source",
})
_EDITOR_ARGUMENTS = {
    "view": (frozenset({"command", "path"}), frozenset({"view_range"})),
    "create": (frozenset({"command", "path", "file_text"}), frozenset()),
    "str_replace": (frozenset({"command", "path", "old_str", "new_str"}), frozenset()),
    "insert": (frozenset({"command", "path", "insert_line", "new_str"}), frozenset()),
    "undo_edit": (frozenset({"command", "path"}), frozenset()),
}


@dataclass(frozen=True)
class ToolAction:
    """One normalised tool call.

    ``name``/``arguments`` are the canonical call for existing analysis engines
    (the original call, unchanged, when ``kind`` is ``unknown``). ``text`` is the
    shell command, written content or, for ambiguous terminal input, the raw
    keystrokes (display only). ``terminal`` marks Terminus keystrokes. For
    those, ``shell_after`` says whether the terminal is still at a shell prompt
    afterwards; use it between several calls of one step, where no observation
    separates them.
    """

    kind: ToolActionKind
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    text: str | None = None
    path: str | None = None
    terminal: bool = False
    shell_after: bool = False


def call_name_and_arguments(call: Mapping[str, Any]) -> tuple[str, Any]:
    """Tool name (as written) and decoded arguments of an OpenAI- or ATIF-shaped call."""
    function = call.get("function")
    source = function if isinstance(function, Mapping) and (function.get("name") or function.get("function_name")) else call
    name = str(source.get("name") or source.get("function_name") or "")
    arguments = source.get("arguments")
    if isinstance(arguments, str):
        with contextlib.suppress(ValueError):
            arguments = json.loads(arguments)
    return name, arguments


def terminal_shell_ready(observation: str) -> bool:
    """Whether a terminal observation ends at an empty shell prompt (not a REPL, editor or heredoc)."""
    text = _TERMINAL_ESCAPE.sub("", observation).replace("\r", "")
    confirmation = text.rfind(_COMPLETE_CONFIRMATION)
    if confirmation >= 0 and text[confirmation:].count("\n") <= 2:
        text = text[:confirmation]
    lines = text.rstrip().rsplit("\n", 1)
    return bool(_SHELL_PROMPT.fullmatch(lines[-1].strip()))


def _clean_path(raw: Any, *, directory: bool = False) -> str | None:
    """``raw`` when it is already a clean path (no ``.``/``..``/``//`` detours, no padding), else ``None``."""
    if not isinstance(raw, str) or not raw or raw != raw.strip() or _CONTROL.search(raw) or "\n" in raw:
        return None
    if raw == "." and directory:
        return raw
    if posixpath.normpath(raw) != raw or ".." in raw.split("/") or "." in raw.split("/") or raw.startswith("//"):
        return None
    return raw


# --------------------------------------------------------------------------- #
# Shell command allow-list
# --------------------------------------------------------------------------- #

_SILENT_STDERR = re.compile(r"(?<=\s)2>(?:&1|/dev/null)(?=\s|$)")
_FORBIDDEN_ANYWHERE = frozenset("\\$`!")
_FORBIDDEN_UNQUOTED = frozenset(";&|<>(){}#^\n")
_VALUE_FLAGS = frozenset({"-n", "-c", "-A", "-B", "-C", "-m", "-d", "-k", "-t", "-s", "-w", "-name", "-type", "-maxdepth"})
_FIND_FORBIDDEN = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls"})
_READERS = {"cat": 1, "head": 1, "tail": 1, "wc": 1, "sort": 1, "uniq": 1, "cut": 1, "grep": 2, "egrep": 2, "fgrep": 2}
_PLAIN = frozenset({
    "ls", "pwd", "cd", "echo", "which", "whoami", "id", "date", "uname", "file", "stat", "du", "df", "ps",
    "mkdir", "printenv", "hostname", "nproc", "free", "uptime", "env",
})
_GIT_READ = frozenset({"status", "diff", "log", "show", "branch", "rev-parse", "ls-files", "remote"})
_GIT_PAGED = frozenset({"diff", "log", "show"})
_PYTHON = re.compile(r"python[\d.]*")


def _split_commands(line: str) -> list[tuple[list[str], bool]] | None:
    """Split one line on unquoted ``&&`` / ``|`` into ``(tokens, piped_in)``; ``None`` unless plain allow-listable text."""
    if not line or _CONTROL.search(line) or any(char in line for char in _FORBIDDEN_ANYWHERE):
        return None
    line = _SILENT_STDERR.sub("", line)
    segments: list[tuple[str, bool]] = []
    chunk: list[str] = []
    piped = False
    quote = ""
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif line.startswith("&&", index):
            segments.append(("".join(chunk), piped))
            chunk, piped, index = [], False, index + 2
            continue
        elif char == "|" and not line.startswith("||", index):
            segments.append(("".join(chunk), piped))
            chunk, piped, index = [], True, index + 1
            continue
        elif char in _FORBIDDEN_UNQUOTED:
            return None
        chunk.append(char)
        index += 1
    if quote:
        return None
    segments.append(("".join(chunk), piped))
    commands: list[tuple[list[str], bool]] = []
    for text, piped_in in segments:
        try:
            tokens = shlex.split(text)
        except ValueError:
            return None
        if not tokens or any(_KEY_NAME.fullmatch(token) for token in tokens):
            return None
        commands.append((tokens, piped_in))
    return commands


def _operands(args: list[str]) -> list[str]:
    operands: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in _VALUE_FLAGS:
            skip = True
        elif not arg.startswith("-"):
            operands.append(arg)
    return operands


def _allowed(tokens: list[str], *, piped_in: bool) -> bool | None:
    """Whether one simple command is allow-listed (``None``: not allowed). ``True``: returns to the prompt."""
    program, args = posixpath.basename(tokens[0]), tokens[1:]
    if "-" in args:
        return None
    operands = _operands(args)
    if program in _READERS:
        if program == "tail" and {"-f", "-F", "--follow"}.intersection(args):
            return None
        if {"-e", "-f", "--expression", "--file", "--regexp"}.intersection(args):
            return None
        return True if piped_in or len(operands) >= _READERS[program] else None
    if program in _PLAIN:
        return True if program != "env" or not args else None
    if program == "find":
        return True if not _FIND_FORBIDDEN.intersection(args) else None
    if program in ("pytest", "py.test"):
        return True
    if _PYTHON.fullmatch(program):
        if "-c" in args or "-i" in args or "-" in args:
            return None
        if args[:2] == ["-m", "pytest"] or args[:1] == ["--version"]:
            return True
        # A script may read stdin, so the terminal may not be at a prompt afterwards.
        return False if operands and not args[0].startswith("-") else None
    if program == "git":
        subcommand = operands[0] if operands else ""
        if subcommand not in _GIT_READ:
            return None
        return not (subcommand in _GIT_PAGED and "--no-pager" not in args)
    if program in ("pip", "pip3"):
        return True if operands[:1] in (["install"], ["list"], ["show"], ["freeze"]) or args == ["--version"] else None
    if program == "make":
        return True
    if program in ("cargo", "go"):
        return True if operands[:1] in (["test"], ["build"], ["check"], ["vet"]) else None
    return None


def _safe_command(command: str) -> tuple[str, bool] | None:
    """``(command, shell_after)`` for one allow-listed line, else ``None``."""
    commands = _split_commands(command)
    if commands is None:
        return None
    after = True
    for tokens, piped in commands:
        verdict = _allowed(tokens, piped_in=piped)
        if verdict is None:
            return None
        after = after and verdict
    return command, after


def terminal_shell_input(keystrokes: str) -> str | None:
    """The one allow-listed shell line typed by ``keystrokes``, or ``None`` when unsure."""
    if not keystrokes.endswith("\n") or _CONTROL.search(keystrokes.removesuffix("\n").removesuffix("\r")):
        return None
    line = keystrokes.removesuffix("\n").removesuffix("\r")
    return line if "\n" not in line and _safe_command(line) is not None else None


def _shell(command: str, *, terminal: bool = False, shell_after: bool = True) -> ToolAction:
    return ToolAction("shell", "bash", {"command": command}, text=command, terminal=terminal, shell_after=shell_after)


def _terminal(arguments: Mapping[str, Any], shell_ready: bool) -> ToolAction:
    keystrokes = arguments.get("keystrokes")
    if not isinstance(keystrokes, str):
        return ToolAction("unknown", "bash_command", arguments, terminal=True)
    if not keystrokes:
        return ToolAction("noop", "think", terminal=True, shell_after=shell_ready)
    command = terminal_shell_input(keystrokes) if shell_ready else None
    safe = _safe_command(command) if command is not None else None
    if safe is not None:
        return _shell(safe[0], terminal=True, shell_after=safe[1])
    # Left as the original call: existing scanners keep reading it exactly as
    # before, and L0 still routes it to review as an unrecognised shape.
    return ToolAction("unknown", "bash_command", arguments, text=keystrokes, terminal=True)


# --------------------------------------------------------------------------- #
# Other transports
# --------------------------------------------------------------------------- #


def _glob(arguments: Mapping[str, Any]) -> ToolAction | None:
    pattern = arguments.get("pattern")
    if not _GLOB_ARGUMENTS.issuperset(arguments):
        return None
    path = _clean_path(arguments.get("path", "." if pattern is not None else None), directory=True)
    if path is None or ";" in path:
        return None
    target = path
    if pattern is not None:
        if not isinstance(pattern, str) or not _GLOB_PATTERN.fullmatch(pattern) or ".." in pattern.split("/"):
            return None
        target = posixpath.join(path, pattern)
    command = f"ls -- {shlex.quote(target)}"
    return ToolAction("read", "bash", {"command": command}, text=command, path=target)


def _notebook_edit(arguments: Mapping[str, Any]) -> ToolAction | None:
    payload = arguments.get("file_edit_payload")
    if not _NOTEBOOK_ARGUMENTS.issuperset(arguments) or not isinstance(payload, Mapping):
        return None
    if not _NOTEBOOK_PAYLOAD.issuperset(payload):
        return None
    paths = {key: arguments[key] for key in ("file_path", "filepath") if key in arguments}
    path = _clean_path(next(iter(paths.values()), None))
    if path is None or any(value != path for value in paths.values()):
        return None
    source = payload.get("source")
    if isinstance(source, list) and all(isinstance(line, str) for line in source):
        if all(line.endswith("\n") for line in source[:-1]):
            source = "".join(source)  # Jupyter convention: every line but the last carries its newline
        elif not any(line.endswith("\n") for line in source):
            source = "\n".join(source)  # newline-stripped export
        else:
            return None
    if not isinstance(source, str):
        return None
    return ToolAction("edit", "edit", {"path": path, "command": "edit", "content": source}, text=source, path=path)


def _text_editor(arguments: Mapping[str, Any]) -> ToolAction | None:
    command = arguments.get("command")
    if not isinstance(command, str) or command not in _EDITOR_ARGUMENTS:
        return None
    required, optional = _EDITOR_ARGUMENTS[command]
    keys = set(arguments)
    if not required <= keys or not keys <= required | optional:
        return None
    path = _clean_path(arguments.get("path"))
    if path is None:
        return None
    texts = [arguments.get(key) for key in ("file_text", "old_str", "new_str") if key in arguments]
    if not all(isinstance(text, str) for text in texts):
        return None
    if command == "view":
        return ToolAction("read", "str_replace_editor", dict(arguments), path=path)
    if command == "create":
        return ToolAction("write", "str_replace_editor", dict(arguments), text=arguments["file_text"], path=path)
    if command in ("str_replace", "insert"):
        return ToolAction("edit", "str_replace_editor", dict(arguments), text=arguments["new_str"], path=path)
    # Undo modifies the file even though it carries no replacement text.
    return ToolAction("edit", "edit", {**arguments, "command": "edit"}, path=path)


def _is_noop(lowered: str, arguments: Mapping[str, Any]) -> bool:
    """A planning/waiting call whose arguments are only descriptive (never a command or code)."""
    return lowered in _NOOP_TOOLS and all(key in _NOOP_ARGUMENTS or key.endswith("_payload") for key in arguments)


def normalize_tool_call(name: str, arguments: Any, *, shell_ready: bool = True) -> ToolAction:
    """Map one raw tool call to a :class:`ToolAction`; anything not allow-listed is ``unknown``."""
    lowered = name.lower()
    action: ToolAction | None = None
    if lowered in ("mark_task_complete", "task_complete") and (
        arguments in (None, {}) or arguments == {"input": "{}"}
    ):
        action = ToolAction("noop", "finish")
    elif isinstance(arguments, Mapping):
        if _is_noop(lowered, arguments):
            action = ToolAction("noop", "think")
        elif lowered == "glob":
            action = _glob(arguments)
        elif lowered == "shell":
            # Bundle exports duplicate the direct command into keystrokes.
            command = arguments.get("command")
            safe = _safe_command(command) if isinstance(command, str) else None
            if safe is not None and arguments.get("keystrokes", command) == command and set(arguments) <= {"command", "keystrokes"}:
                action = _shell(safe[0], shell_after=safe[1])
        elif lowered in ("edit_file", "edit_notebook"):
            action = _notebook_edit(arguments)
        elif lowered == "text_editor":
            action = _text_editor(arguments)
        elif lowered == "bash_command":
            return _terminal(arguments, shell_ready)
    return action or ToolAction(
        "unknown", name, arguments if isinstance(arguments, Mapping) else {},
        terminal=lowered == "bash_command",
    )
