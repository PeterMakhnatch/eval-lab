"""Normalise raw agent tool calls into one small action vocabulary.

``normalize_tool_call`` is pure: it maps one call (name plus arguments) to a
:class:`ToolAction`. ``name`` and ``arguments`` of the action are the canonical
call the existing shell/editor analysis (``trace_anatomy``, ``hack_drop`` L0,
``tripwires``) already understands, so consumers reuse their own rules instead
of adding a second engine. ``kind``, ``text`` and ``path`` serve display and
simple consumers such as Judge Station cards.

Principle: understand narrowly, and answer ``unknown`` when in doubt. Every
consumer treats ``unknown`` as "could not read this" (L0 routes it to review),
so a wrong ``shell`` answer silently turns a fail-safe into an evasion, while a
wrong ``unknown`` only costs a review. An ``unknown`` action carries the
original call unchanged.

Recognised transports:

- Terminus-2 ``bash_command {keystrokes, duration}``: tmux keystrokes typed
  into a persistent terminal. Keystrokes are shell text only when the terminal
  is known to be at a shell prompt, the input is submitted, every physical line
  lexes on its own (no multi-line quotes, comments, continuations, control or
  key-name tokens) and every heredoc is opened and closed exactly as a POSIX
  shell would. Lines before the last must run only programs known to return to
  the prompt. Anything else is ``unknown``.
- ``mark_task_complete`` / ``task_complete`` with no payload, and a short list
  of planning, waiting and tool-discovery calls carrying only descriptive
  arguments: ``noop``.
- ``text_editor {command, path, ...}`` (Anthropic str_replace editor): ``view``
  is a read, ``create`` a write of ``file_text``, ``str_replace``, ``insert``
  and ``undo_edit`` edits of ``path``. Malformed payloads are ``unknown``.
- Bundle exports: ``shell {command, keystrokes}`` with equal fields,
  ``glob {path, pattern}`` and notebook ``edit_file`` / ``edit_notebook``
  ``file_edit_payload.source``.

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
_BARE_KEYS = frozenset({"C-c", "C-u", "M-x", "Escape", "BSpace", "Up", "Down", "Left", "Right", "Tab", "Enter"})
_KEY_NAME = re.compile(
    r"(?:[CMS]-.|C-M-.|Enter|Escape|Tab|BTab|BSpace|Space|Up|Down|Left|Right|Home|End|PageUp|PageDown|PgUp|PgDn"
    r"|DC|IC|F\d{1,2})"
)
# Everything except tab and newline: C0 controls (including CR), DEL and C1 controls.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_NOOP_TOOLS = frozenset({
    "sleep", "wait", "close_file", "submit_final_answer", "exit", "todowrite", "toolsearch",
})
_NOOP_ARGUMENTS = frozenset({"explanation", "file_path", "filepath", "input", "i"})
_GLOB_ARGUMENTS = frozenset({"path", "pattern", "i", "hidden", "gitignore", "limit"})


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
    source = function if isinstance(function, Mapping) else call
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
    if confirmation >= 0:
        text = text[:confirmation]
    lines = text.rstrip().rsplit("\n", 1)
    return bool(_SHELL_PROMPT.fullmatch(lines[-1].strip()))


def _clean_path(raw: Any, *, directory: bool = False) -> str | None:
    """A normalised path (no ``.``/``..``/``//`` detours, no whitespace padding), or ``None`` when unusable."""
    if not isinstance(raw, str) or _CONTROL.search(raw) or "\n" in raw:
        return None
    path = raw.strip()
    if not path:
        return None
    normal = posixpath.normpath(path)
    if normal == ".":
        return "." if directory else None
    return re.sub(r"^/+", "/", normal)


# --------------------------------------------------------------------------- #
# Terminus-2 keystrokes
# --------------------------------------------------------------------------- #

_SEPARATORS = frozenset({";", "&&", "||", "|", "&"})
_REDIRECTS = frozenset({">", ">>", "<", "<<", "<<-", "<<<", ">&", "<&", "&>", "&>>", ">|"})
_FEEDS = frozenset({"<", "<<", "<<-", "<<<"})
_GROUPING = frozenset({"(", ")", "{", "}", "$", "$(", "`"})
_INTERPRETER = re.compile(
    r"(?:python[\d.]*|pypy[\d]*|node(?:js)?|ruby|perl|php|lua|julia|Rscript|bash|sh|zsh|dash|ksh)"
)
# Programs that always return to the prompt on their own (given a complete command line).
_BATCH = frozenset({
    "ls", "pwd", "cd", "echo", "printf", "mkdir", "rmdir", "rm", "cp", "mv", "ln", "touch", "chmod", "chown",
    "chgrp", "stat", "file", "du", "df", "ps", "pgrep", "pkill", "kill", "killall", "which", "whereis", "type",
    "whoami", "id", "date", "uname", "hostname", "export", "unset", "alias", "true", "false", "test", "[", "sleep",
    "clear", "history", "free", "uptime", "lsof", "mount", "umount", "ip", "ifconfig", "netstat", "ss", "find",
    "diff", "cmp", "md5sum", "sha1sum", "sha256sum", "basename", "dirname", "realpath", "readlink", "tar", "zip",
    "unzip", "curl", "wget", "pytest", "make", "cargo", "go", "dmesg", "locale", "ldd", "nproc", "lscpu", "lsblk",
    "blkid", "getent", "groups", "stty", "tput", "umask", "ulimit", "mktemp", "install", "sync", "setfacl",
    "getfacl", "lsattr", "chattr", "service", "useradd", "usermod", "groupadd", "userdel", "modprobe", "lsmod",
    "sysctl", "iptables", "ufw", "nslookup", "dig", "host", "printenv", "source", ".",
})
_WRAPPERS = frozenset({"sudo", "time", "nohup", "command", "builtin"})
# Filters read typed input unless they have this many operands or are fed by a pipe or ``<``.
_FILTER_OPERANDS = {
    "cat": 1, "tee": 99, "head": 1, "tail": 1, "wc": 1, "sort": 1, "xargs": 99, "grep": 2, "egrep": 2, "fgrep": 2,
    "sed": 2, "awk": 2, "cut": 1, "tr": 99, "uniq": 99, "nl": 1, "tac": 1, "rev": 1, "xxd": 1, "od": 1,
    "hexdump": 1, "strings": 1, "gzip": 1, "gunzip": 1,
}
_FILTER_BLOCKERS = frozenset({"-e", "-f", "--expression", "--file", "-regexp", "--regexp"})
_GIT_BATCH = frozenset({
    "init", "status", "add", "rm", "mv", "checkout", "switch", "restore", "reset", "clone", "config", "rev-parse",
    "ls-files", "apply", "fetch", "push", "stash", "cat-file", "hash-object", "remote", "branch", "ls-tree",
})
_SYSTEMCTL_BATCH = frozenset({
    "start", "stop", "restart", "reload", "enable", "disable", "mask", "unmask", "is-active", "is-enabled",
    "is-failed", "daemon-reload", "reset-failed", "kill",
})
_PACKAGE_PROMPTING = frozenset({"install", "remove", "upgrade", "dist-upgrade", "purge", "add", "uninstall", "-S"})
_PACKAGE_ASSUME_YES = frozenset({"-y", "--yes", "-qq", "--assume-yes", "--noconfirm"})


def _lex(line: str) -> list[str] | None:
    """Shell tokens of one physical line (``#`` is an ordinary character); ``None`` when it does not lex alone."""
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _is_key_token(token: str) -> bool:
    return bool(_KEY_NAME.fullmatch(token) or re.match(r"[CM]-[A-Za-z]", token))


def _operands(args: list[str]) -> list[str]:
    """Positional operands: options, redirections and their targets, ``-`` and fd digits dropped."""
    operands: list[str] = []
    skip = False
    for index, arg in enumerate(args):
        if skip:
            skip = False
        elif arg in _REDIRECTS:
            skip = True
        elif arg.isdigit() and index + 1 < len(args) and args[index + 1] in _REDIRECTS:
            continue
        elif not arg.startswith("-"):
            operands.append(arg)
    return operands


def _returns_to_prompt(tokens: list[str], *, piped_in: bool) -> bool:
    """Whether one simple command is known to return to the shell prompt without further input."""
    tokens = list(tokens)
    while tokens and re.fullmatch(r"[A-Za-z_]\w*=\S*", tokens[0]):
        tokens = tokens[1:]
    while tokens and tokens[0] in _WRAPPERS:
        tokens = tokens[1:]
        if tokens and tokens[0].startswith("-"):
            return False  # wrapper flags may take operands; do not guess where the program starts
    if not tokens:
        return True
    if any(token in _GROUPING or "`" in token or "$(" in token for token in tokens):
        return False
    program = posixpath.basename(tokens[0])
    args = tokens[1:]
    operands = _operands(args)
    fed = piped_in or any(arg in _FEEDS for arg in args)
    if _INTERPRETER.fullmatch(program):
        if "-i" in args or "--interactive" in args or "-" in args:
            return False
        return fed or bool({"-c", "-e", "-m"}.intersection(args)) or bool(operands)
    if program in _FILTER_OPERANDS:
        if _FILTER_BLOCKERS.intersection(args) or (program == "tail" and {"-f", "-F", "--follow"}.intersection(args)):
            return False
        return fed or len(operands) >= _FILTER_OPERANDS[program]
    if program == "git":
        subcommand = operands[0] if operands else ""
        return subcommand in _GIT_BATCH or (
            subcommand == "commit" and any(arg in ("-m", "--message", "-F", "--no-edit") or arg.startswith("-m") for arg in args)
        )
    if program == "systemctl":
        return bool(_SYSTEMCTL_BATCH.intersection(operands)) or "--no-pager" in args
    if program == "journalctl":
        return "--no-pager" in args and not {"-f", "-F", "--follow"}.intersection(args)
    if program == "docker":
        return not any(arg in ("-i", "-t", "-it", "-ti", "--interactive", "--tty") for arg in args)
    if program in ("apt", "apt-get", "yum", "dnf", "apk"):
        return bool(_PACKAGE_ASSUME_YES.intersection(args)) or not _PACKAGE_PROMPTING.intersection(operands)
    if program in ("pip", "pip3"):
        return "uninstall" not in operands or "-y" in args
    if program == "crontab":
        return ("-l" in args or "-r" in args) and "-e" not in args
    if program == "su":
        return "-c" in args
    if program == "env":
        return not args
    if program == "dd":
        return any(arg.startswith("if=") for arg in args)
    return program in _BATCH


def _parse_submission(text: str) -> list[list[tuple[list[str], bool]]] | None:
    """Per non-blank line, its simple commands as ``(tokens, piped_in)``; ``None`` when unsure.

    Every line must lex on its own, so multi-line quotes, comments, continuation
    lines, control characters and tmux key names are all unsure. A heredoc is
    recognised only from an operator token (never from text inside quotes) and
    must be closed by a line exactly equal to its delimiter (leading tabs
    ignored for ``<<-``); its body is opaque.
    """
    if _CONTROL.search(text):
        return None
    lines = text.split("\n")
    parsed: list[list[tuple[list[str], bool]]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip():
            continue
        if line.rstrip().endswith("\\"):
            return None
        tokens = _lex(line)
        if tokens is None or any(token.startswith("#") or _is_key_token(token) for token in tokens):
            return None
        heredocs: list[tuple[str, bool]] = []
        for position, token in enumerate(tokens):
            if token in ("<<", "<<-"):
                if position + 1 >= len(tokens):
                    return None
                delimiter = tokens[position + 1]
                indented = token == "<<-"
                if token == "<<" and delimiter.startswith("-") and "<<-" in line:
                    delimiter, indented = delimiter[1:], True  # the lexer split ``<<-EOF`` after the operator
                if not delimiter or delimiter.startswith("-") or delimiter in _REDIRECTS or delimiter in _SEPARATORS:
                    return None
                heredocs.append((delimiter, indented))
        for delimiter, indented in heredocs:
            while index < len(lines) and (lines[index].lstrip("\t") if indented else lines[index]) != delimiter:
                index += 1
            if index >= len(lines):
                return None
            index += 1
        commands: list[tuple[list[str], bool]] = []
        current: list[str] = []
        piped = False
        for token in [*tokens, ";"]:
            if token not in _SEPARATORS:
                current.append(token)
                continue
            if current:
                commands.append((current, piped))
            current = []
            piped = token == "|"
        parsed.append(commands)
    return parsed


def terminal_shell_input(keystrokes: str) -> str | None:
    """The shell command text submitted by ``keystrokes``, or ``None`` when unsure.

    A leading interrupt character (``\x03``/``\x15``) and the final newline are
    terminal controls, not command text. Key names such as ``C-c`` or ``Enter``
    are only keys when they are the whole keystrokes string; mixed with text
    they are typed literally, so such input is unsure. See the module docstring
    for what else is accepted.
    """
    text = re.sub(r"^[\x03\x15]+", "", keystrokes)
    submitted = text.endswith(("\n", "\r"))
    text = text.rstrip("\r\n")
    if not submitted or not text.strip() or text.strip() in _BARE_KEYS:
        return None
    lines = _parse_submission(text)
    if not lines:
        return None
    if any(not _returns_to_prompt(tokens, piped_in=piped) for line in lines[:-1] for tokens, piped in line):
        return None
    return text


def _shell_after(command: str) -> bool:
    """Whether the terminal is back at a shell prompt after ``command`` (its last line starts no program that reads keys)."""
    lines = _parse_submission(command)
    return bool(lines) and all(_returns_to_prompt(tokens, piped_in=piped) for tokens, piped in lines[-1])


def _shell(command: str, *, terminal: bool = False, shell_after: bool = True) -> ToolAction:
    return ToolAction("shell", "bash", {"command": command}, text=command, terminal=terminal, shell_after=shell_after)


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


# --------------------------------------------------------------------------- #
# Other transports
# --------------------------------------------------------------------------- #


def _glob(arguments: Mapping[str, Any]) -> ToolAction | None:
    pattern = arguments.get("pattern")
    if not _GLOB_ARGUMENTS.issuperset(arguments) or not (pattern is None or isinstance(pattern, str)):
        return None
    raw = arguments.get("path")
    if not isinstance(raw, str):
        return None
    paths = [_clean_path(part, directory=True) for part in raw.split(";")]
    if not paths or any(path is None for path in paths):
        return None
    if pattern:
        if _CONTROL.search(pattern) or "\n" in pattern:
            return None
        paths = [posixpath.join(str(path), pattern) for path in paths]
    command = "ls -- " + " ".join(shlex.quote(str(path)) for path in paths)
    return ToolAction(
        "read", "bash", {"command": command}, text=command, path=paths[0] if len(paths) == 1 else None,
    )


def _notebook_edit(arguments: Mapping[str, Any]) -> ToolAction | None:
    payload = arguments.get("file_edit_payload")
    candidates = {arguments[key] for key in ("file_path", "filepath") if key in arguments}
    if len(candidates) != 1 or not isinstance(payload, Mapping):
        return None
    path = _clean_path(next(iter(candidates)))
    if path is None:
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
    path = _clean_path(arguments.get("path"))
    if path is None:
        return None
    clean = {**arguments, "path": path}
    if command == "view":
        return ToolAction("read", "str_replace_editor", clean, path=path)
    if command == "create" and isinstance(arguments.get("file_text"), str):
        return ToolAction("write", "str_replace_editor", clean, text=arguments["file_text"], path=path)
    if command == "str_replace" and all(isinstance(arguments.get(key), str) for key in ("old_str", "new_str")):
        return ToolAction("edit", "str_replace_editor", clean, text=arguments["new_str"], path=path)
    if command == "insert" and isinstance(arguments.get("new_str"), str):
        return ToolAction("edit", "str_replace_editor", clean, text=arguments["new_str"], path=path)
    if command == "undo_edit":
        # Undo modifies the file even though it carries no replacement text.
        return ToolAction("edit", "edit", {**clean, "command": "edit"}, path=path)
    return None


def _is_noop(lowered: str, arguments: Mapping[str, Any]) -> bool:
    """A planning/waiting call whose arguments are only descriptive (never a command or code)."""
    return lowered in _NOOP_TOOLS and all(key in _NOOP_ARGUMENTS or key.endswith("_payload") for key in arguments)


def normalize_tool_call(name: str, arguments: Any, *, shell_ready: bool = True) -> ToolAction:
    """Map one raw tool call to a :class:`ToolAction`; unsupported shapes are ``unknown``."""
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
