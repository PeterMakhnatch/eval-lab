"""Shell lexing shared by the task lint rules; nothing here executes task content."""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from pathlib import PurePosixPath

SHELL_PUNCTUATION = ";&|()"
COMMAND_SEPARATORS = frozenset({"&&", "||", ";", "|", "|&", "&", ";;", "(", ")"})
SHELL_KEYWORDS = frozenset(
    {
        "if", "then", "else", "elif", "fi", "do", "done", "while", "until",
        "for", "in", "case", "esac", "!", "{", "}", "time", "builtin", "eval",
    }
)
EXEC_WRAPPERS = frozenset({"sudo", "env", "command", "exec", "nohup"})
WRAPPER_VALUE_OPTIONS = frozenset(
    {"-u", "--user", "-g", "--group", "-h", "--host", "--unset", "-C", "-S"}
)


def logical_lines(text: str) -> Iterator[tuple[int, str]]:
    """Join backslash continuations and retain the first physical line number."""
    pending = ""
    start = 1
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not pending:
            start = number
        continued = line.endswith("\\")
        pending += line[:-1] if continued else line
        if continued:
            pending += " "
            continue
        yield start, pending
        pending = ""
    if pending:
        yield start, pending


def split_shell(line: str) -> list[str]:
    """Split a shell line, separating operators attached without spaces."""
    lexer = shlex.shlex(line, posix=True, punctuation_chars=SHELL_PUNCTUATION)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        return [token for token in lexer if token]
    except ValueError:
        return line.split()


def command_head(tokens: list[str]) -> list[str]:
    """Remove assignments, shell keywords, and wrappers before the command."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            index += 1
            continue
        if token in SHELL_KEYWORDS:
            index += 1
            continue
        wrapper = PurePosixPath(token).name
        if wrapper not in EXEC_WRAPPERS:
            break
        index += 1
        while index < len(tokens) and tokens[index].startswith("-"):
            option = tokens[index]
            index += 1
            if option == "--":
                break
            if option in WRAPPER_VALUE_OPTIONS:
                index += 1
    return tokens[index:]


def shell_commands(tokens: list[str]) -> Iterator[tuple[list[str], str]]:
    start = 0
    for index, token in enumerate(tokens):
        if token in COMMAND_SEPARATORS:
            if index > start:
                yield command_head(tokens[start:index]), token
            start = index + 1
    if start < len(tokens):
        yield command_head(tokens[start:]), ""
