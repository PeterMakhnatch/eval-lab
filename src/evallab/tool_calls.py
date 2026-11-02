"""Normalise raw agent tool calls into one small action vocabulary.

``normalize_tool_call`` is pure: it maps one call (name plus arguments) to a
:class:`ToolAction`. ``name`` and ``arguments`` of the action are the canonical
call the existing shell/editor analysis (``trace_anatomy``, ``hack_drop`` L0,
``tripwires``) already understands, so consumers reuse their own rules instead
of adding a second engine. ``kind``, ``text`` and ``path`` serve display and
simple consumers such as Judge Station cards.

Recognised transports:

- Terminus-2 ``bash_command {keystrokes, duration}``: tmux keystrokes typed
  into a persistent terminal. Only a single submitted line, typed while the
  terminal is known to show a shell prompt, is shell text. Heredocs, multi-line
  input, bare control keys and anything typed into a REPL or editor stay
  ``unknown`` (the caller decides what unknown means; L0 routes to review).
- ``mark_task_complete`` / ``task_complete`` with no payload, and planning,
  waiting, tool-discovery and close calls: ``noop``.
- ``text_editor {command, path, ...}`` (Anthropic str_replace editor):
  ``view`` is a read, ``create`` a write of ``file_text``, ``str_replace``,
  ``insert`` and ``undo_edit`` edits of ``path``.
- Bundle exports: ``shell {command, keystrokes}`` with equal fields,
  ``glob {path, pattern}``, and notebook ``edit_file`` / ``edit_notebook``
  ``file_edit_payload.source``.

Terminal context is the caller's state: start with ``shell_ready=True`` (a
Terminus session begins at a shell), pass it in, and after each terminal call
recompute it with :func:`terminal_shell_ready` from the call's observation.
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
_SHELL_PROMPT = re.compile(r"(?:^|\n)(?:\([^\n()]+\)\s*)?[\w.-]+@[\w.-]+:[^\n]*[#$]\s*$")
_COMPLETE_CONFIRMATION = re.compile(r"\n*Are you sure you want to mark the task as complete\?[^\n]*\s*$")
_BARE_KEYS = frozenset({"C-c", "C-u", "M-x", "Escape", "BSpace", "Up", "Down", "Left", "Right", "Tab", "Enter"})
_NOOP_TOOLS = frozenset({
    "sleep", "wait", "close_file", "submit_final_answer", "exit", "todowrite", "toolsearch",
})


@dataclass(frozen=True)
class ToolAction:
    """One normalised tool call.

    ``name``/``arguments`` are the canonical call for existing analysis engines
    (the original call, unchanged, when ``kind`` is ``unknown`` and the shape is
    not a recognised transport). ``text`` is the shell command, written content
    or, for ambiguous terminal input, the raw keystrokes (display only).
    ``terminal`` marks Terminus keystrokes. For those, ``shell_after`` says
    whether the terminal is still at a shell prompt afterwards; use it between
    several calls of one step, where no observation separates them.
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
    source = function if isinstance(function, Mapping) else call
    name = str(source.get("name") or source.get("function_name") or "")
    arguments = source.get("arguments")
    if isinstance(arguments, str):
        with contextlib.suppress(ValueError):
            arguments = json.loads(arguments)
    return name, arguments


_HEREDOC = re.compile(r"(?<!<)<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1")
_NEWLINE = "\x01"
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", _NEWLINE})
_REDIRECTS = frozenset({">", ">>", "<", "<<", "<<<", ">&", "<&", "&>", "&>>"})
_WRAPPERS = frozenset({"sudo", "env", "time", "nohup", "nice", "command", "exec"})
# Programs that take over the terminal; keys typed after them are not shell input.
_INTERACTIVE = frozenset({
    "vi", "vim", "nvim", "nano", "emacs", "ed", "less", "more", "most", "man", "top", "htop", "watch",
    "gdb", "lldb", "pdb", "mysql", "psql", "sqlite3", "redis-cli", "mongo", "mongosh", "irb", "ipython",
    "bc", "dc", "tmux", "screen", "ssh", "telnet", "ftp", "sftp", "nc", "ncat", "passwd", "read",
    "crontab", "visudo", "vipw", "fdisk", "parted", "cfdisk", "gdisk",
})
# Interpreters wait for typed input unless given a script, ``-c`` or ``-e``.
_INTERPRETERS = frozenset({
    "python", "python2", "python3", "node", "nodejs", "ruby", "perl", "php", "lua", "julia", "sh", "bash",
    "zsh", "dash", "ksh",
})
# Filters read typed input when they have too few operands and no pipe or ``<`` feeding them.
_FILTER_OPERANDS = {
    "cat": 1, "tee": 99, "head": 1, "tail": 1, "wc": 1, "sort": 1, "xargs": 99, "grep": 2, "sed": 2,
    "awk": 2, "cut": 1, "tr": 99,
}
_PACKAGE_MANAGERS = frozenset({"apt", "apt-get", "yum", "dnf", "zypper", "apk", "pacman"})
_PACKAGE_PROMPTING = frozenset({"install", "remove", "upgrade", "dist-upgrade", "purge", "add", "-S"})
_PACKAGE_ASSUME_YES = frozenset({"-y", "--yes", "-qq", "--assume-yes", "--noconfirm"})


def terminal_shell_ready(observation: str) -> bool:
    """Whether a terminal observation ends at a shell prompt (not a REPL, editor or heredoc)."""
    text = _TERMINAL_ESCAPE.sub("", observation).replace("\r", "")
    text = _COMPLETE_CONFIRMATION.sub("", text)
    return bool(_SHELL_PROMPT.search(text))


def _strip_heredoc_bodies(text: str) -> tuple[str, bool]:
    """Command text without heredoc bodies, and whether every heredoc is terminated."""
    lines = text.split("\n")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for match in _HEREDOC.finditer(line):
            while index < len(lines) and lines[index].strip() != match.group(2):
                index += 1
            if index >= len(lines):
                return "\n".join(kept), False
            index += 1
    return "\n".join(kept), True


def _mark_newlines(text: str) -> str:
    """Replace unquoted newlines with a separator word; escaped newlines are continuations."""
    out: list[str] = []
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote == "'":
            quote = "" if char == "'" else quote
        elif char == "\\" and index + 1 < len(text):
            index += 1
            if text[index] != "\n":
                out.append(char + text[index])
            index += 1
            continue
        elif quote == '"':
            quote = "" if char == '"' else quote
        elif char in "'\"":
            quote = char
        elif char == "\n":
            out.append(f" {_NEWLINE} ")
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _commands(text: str) -> list[tuple[list[str], bool, int]] | None:
    """``(tokens, piped_in, line_index)`` per simple command; ``None`` when quoting is unbalanced."""
    lexer = shlex.shlex(_mark_newlines(text), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    commands: list[tuple[list[str], bool, int]] = []
    current: list[str] = []
    piped = False
    line = 0
    for token in [*tokens, ";"]:
        if token not in _SEPARATORS:
            current.append(token)
            continue
        if current:
            commands.append((current, piped, line))
        current = []
        piped = token == "|"
        line += token == _NEWLINE
    return commands


def _takes_over_terminal(tokens: list[str], *, piped_in: bool) -> bool:
    """Whether one simple command starts a program that will read typed keys."""
    tokens = list(tokens)
    while tokens and (tokens[0] in _WRAPPERS or re.fullmatch(r"[A-Za-z_]\w*=\S*", tokens[0])):
        tokens = tokens[1:]
        while tokens and tokens[0].startswith("-") and tokens[0] != "-":
            tokens = tokens[1:]
    if not tokens:
        return False
    program = posixpath.basename(tokens[0])
    rest = tokens[1:]
    operands: list[str] = []
    skip = False
    for arg in rest:
        if skip:
            skip = False
        elif arg in _REDIRECTS:
            skip = True
        elif not arg.startswith("-") or arg == "-":
            operands.append(arg)
    fed = piped_in or any(arg in ("<", "<<", "<<<") for arg in rest)
    if program in _INTERACTIVE:
        return True
    if program == "su":
        return "-c" not in rest
    if program in _PACKAGE_MANAGERS:
        return not _PACKAGE_ASSUME_YES.intersection(rest) and bool(_PACKAGE_PROMPTING.intersection(rest))
    if program in ("tail", "journalctl") and {"-f", "-F", "--follow"}.intersection(rest):
        return True
    if program in _INTERPRETERS:
        if "-i" in rest:
            return True
        return not fed and not ({"-c", "-e", "-m"}.intersection(rest) or [o for o in operands if o != "-"])
    if program in _FILTER_OPERANDS:
        return not fed and len(operands) < _FILTER_OPERANDS[program]
    return False


def terminal_shell_input(keystrokes: str) -> str | None:
    """The shell command text submitted by ``keystrokes``, or ``None`` when unsure.

    A leading ``C-c``/``C-u`` and a trailing ``Enter`` or newline are terminal
    controls, not command text. Several submitted lines and complete heredocs
    are returned whole (the shell analysis parses them like any bash command);
    anything that leaves a heredoc open, ends in a continuation, has unbalanced
    quoting, or starts an interactive program before the last line is unsure.
    """
    text = re.sub(r"^(?:(?:C-c|C-u)\s*|\x03|\x15)+", "", keystrokes)
    submitted = text.endswith(("\n", "\r")) or bool(re.search(r"(?:^|\s)Enter\s*$", text))
    text = re.sub(r"(?:^|\s)Enter\s*$", "", text).rstrip("\r\n")
    if not submitted or not text.strip() or text.strip() in _BARE_KEYS:
        return None
    if any(ord(char) < 32 and char not in "\n\t" for char in text):
        return None
    if text.rstrip().endswith("\\"):
        return None
    stripped, complete = _strip_heredoc_bodies(text)
    commands = _commands(stripped) if complete else None
    if commands is None:
        return None
    last_line = max((line for _, _, line in commands), default=0)
    if any(line < last_line and _takes_over_terminal(tokens, piped_in=piped) for tokens, piped, line in commands):
        return None
    return text


def _shell(command: str, *, terminal: bool = False, shell_after: bool = True) -> ToolAction:
    return ToolAction("shell", "bash", {"command": command}, text=command, terminal=terminal, shell_after=shell_after)


def _shell_after(command: str) -> bool:
    """Whether the terminal is back at a shell prompt after ``command`` (its last line starts no program that reads keys)."""
    commands = _commands(_strip_heredoc_bodies(command)[0]) or []
    last_line = max((line for _, _, line in commands), default=0)
    return not any(line == last_line and _takes_over_terminal(tokens, piped_in=piped) for tokens, piped, line in commands)


def _terminal(arguments: Mapping[str, Any], shell_ready: bool) -> ToolAction:
    keystrokes = arguments.get("keystrokes")
    if not isinstance(keystrokes, str):
        return ToolAction("unknown", "bash_command", arguments, terminal=True)
    if not keystrokes:
        return ToolAction("noop", "think", terminal=True, shell_after=shell_ready)
    command = terminal_shell_input(keystrokes) if shell_ready else None
    if command is not None:
        return _shell(command, terminal=True, shell_after=_shell_after(command))
    # Left as the original call: existing scanners keep reading it exactly as
    # before, and L0 still routes it to review as an unrecognised shape.
    return ToolAction("unknown", "bash_command", arguments, text=keystrokes, terminal=True)


def _glob(arguments: Mapping[str, Any]) -> ToolAction | None:
    path = arguments.get("path", ".")
    pattern = arguments.get("pattern")
    if not isinstance(path, str) or not (pattern is None or isinstance(pattern, str)):
        return None
    paths = [part for part in path.split(";") if part] or ["."]
    if pattern:
        paths = [posixpath.join(part, pattern) for part in paths]
    command = "ls " + " ".join(shlex.quote(part) for part in paths)
    return ToolAction("read", "bash", {"command": command}, text=command, path=paths[0])


def _notebook_edit(arguments: Mapping[str, Any]) -> ToolAction | None:
    payload = arguments.get("file_edit_payload")
    path = arguments.get("file_path") or arguments.get("filepath")
    if not isinstance(payload, Mapping) or not isinstance(path, str) or not path:
        return None
    source = payload.get("source")
    if isinstance(source, list) and all(isinstance(line, str) for line in source):
        # Jupyter source lines carry their own newline; some exports strip them.
        source = ("" if any(line.endswith("\n") for line in source) else "\n").join(source)
    if not isinstance(source, str):
        return None
    return ToolAction("edit", "edit", {"path": path, "command": "edit", "content": source}, text=source, path=path)


def _text_editor(arguments: Mapping[str, Any]) -> ToolAction | None:
    command = arguments.get("command")
    path = arguments.get("path")
    if not isinstance(path, str) or not path:
        return None
    if command == "view":
        return ToolAction("read", "str_replace_editor", arguments, path=path)
    if command == "create" and isinstance(arguments.get("file_text"), str):
        return ToolAction("write", "str_replace_editor", arguments, text=arguments["file_text"], path=path)
    if command == "str_replace" and all(isinstance(arguments.get(key), str) for key in ("old_str", "new_str")):
        return ToolAction("edit", "str_replace_editor", arguments, text=arguments["new_str"], path=path)
    if command == "insert" and isinstance(arguments.get("new_str"), str):
        return ToolAction("edit", "str_replace_editor", arguments, text=arguments["new_str"], path=path)
    if command == "undo_edit":
        # Undo modifies the file even though it carries no replacement text.
        return ToolAction("edit", "edit", {**arguments, "command": "edit"}, path=path)
    return None


def normalize_tool_call(name: str, arguments: Any, *, shell_ready: bool = True) -> ToolAction:
    """Map one raw tool call to a :class:`ToolAction`; unsupported shapes are ``unknown``."""
    lowered = name.lower()
    action: ToolAction | None = None
    if lowered in ("mark_task_complete", "task_complete") and (
        arguments in (None, {}) or arguments == {"input": "{}"}
    ):
        action = ToolAction("noop", "finish")
    elif isinstance(arguments, Mapping):
        if lowered in _NOOP_TOOLS:
            action = ToolAction("noop", "think")
        elif lowered == "glob":
            action = _glob(arguments)
        elif lowered == "shell":
            # Bundle exports duplicate the direct command into keystrokes; unlike
            # Terminus this is not input into a persistent terminal.
            command = arguments.get("command")
            if isinstance(command, str) and command and arguments.get("keystrokes", command) == command:
                action = _shell(command)
        elif lowered in ("edit_file", "edit_notebook"):
            action = _notebook_edit(arguments)
        elif lowered == "text_editor":
            action = _text_editor(arguments)
        elif lowered == "bash_command":
            return _terminal(arguments, shell_ready)
    return action or ToolAction("unknown", name, arguments if isinstance(arguments, Mapping) else {})
