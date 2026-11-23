from __future__ import annotations

import re
import shlex
from collections import deque
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
EXEC_WRAPPERS = frozenset(
    {
        "sudo", "env", "command", "exec", "nohup",
        "timeout", "nice", "stdbuf",
    }
)
WRAPPER_VALUE_OPTIONS = {
    "sudo": frozenset({"-u", "--user", "-g", "--group", "-h", "--host"}),
    "env": frozenset({"-u", "--unset", "-C", "-S"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
}
WRAPPER_OPERANDS = {"timeout": 1}
INLINE_SHELL_HEADS = frozenset({"bash", "sh", "dash", "zsh", "ksh"})
INLINE_COMMAND_LIMIT = 64
_BACKTICK = re.compile(r"`([^`\n]+)`")
_INLINE_FLAGS = frozenset({"-c", "--command"})


def logical_lines(text: str) -> Iterator[tuple[int, str]]:
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
    lexer = shlex.shlex(line, posix=True, punctuation_chars=SHELL_PUNCTUATION)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        return [token for token in lexer if token]
    except ValueError:
        return line.split()


def command_head(tokens: list[str]) -> list[str]:
    assert isinstance(tokens, list)
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
        values = WRAPPER_VALUE_OPTIONS.get(wrapper, frozenset())
        while index < len(tokens) and tokens[index].startswith("-"):
            option = tokens[index]
            index += 1
            if option == "--":
                break
            if option in values and index < len(tokens):
                index += 1
        operands = WRAPPER_OPERANDS.get(wrapper, 0)
        assert isinstance(operands, int) and operands >= 0
        index = min(index + operands, len(tokens))
    assert 0 <= index <= len(tokens)
    return tokens[index:]


def _direct_commands(tokens: list[str]) -> Iterator[tuple[list[str], str]]:
    start = 0
    for index, token in enumerate(tokens):
        if token in COMMAND_SEPARATORS:
            if index > start:
                yield command_head(tokens[start:index]), token
            start = index + 1
    if start < len(tokens):
        yield command_head(tokens[start:]), ""


def _flag_value(command: list[str]) -> str | None:
    for index, token in enumerate(command[1:], 1):
        if token in _INLINE_FLAGS and index + 1 < len(command):
            return command[index + 1]
    return None


def _backtick_bodies(text: str) -> Iterator[str]:
    count = 0
    for match in _BACKTICK.finditer(text):
        if count >= INLINE_COMMAND_LIMIT:
            break
        count += 1
        yield match.group(1)
    assert count <= INLINE_COMMAND_LIMIT


def _inline_bodies(command: list[str]) -> Iterator[str]:
    assert isinstance(command, list)
    seen: set[str] = set()
    yielded = 0
    if command and PurePosixPath(command[0]).name in INLINE_SHELL_HEADS:
        body = _flag_value(command)
        if body is not None and body not in seen:
            seen.add(body)
            yielded += 1
            yield body
    for body in _backtick_bodies(" ".join(command)):
        if yielded >= INLINE_COMMAND_LIMIT:
            break
        if body in seen:
            continue
        seen.add(body)
        yielded += 1
        yield body
    assert yielded <= INLINE_COMMAND_LIMIT


def _enqueue(
    queue: deque[list[str]], seen: set[tuple[str, ...]], tokens: list[str]
) -> None:
    assert len(queue) <= INLINE_COMMAND_LIMIT
    key = tuple(tokens)
    if key in seen or len(queue) >= INLINE_COMMAND_LIMIT:
        return
    seen.add(key)
    queue.append(tokens)
    assert len(queue) <= INLINE_COMMAND_LIMIT


def _enqueue_bodies(
    queue: deque[list[str]], seen: set[tuple[str, ...]], bodies: Iterator[str]
) -> None:
    added = 0
    for body in bodies:
        if added >= INLINE_COMMAND_LIMIT:
            break
        _enqueue(queue, seen, split_shell(body))
        added += 1
    assert added <= INLINE_COMMAND_LIMIT


def shell_commands(tokens: list[str]) -> Iterator[tuple[list[str], str]]:
    assert isinstance(tokens, list)
    queue: deque[list[str]] = deque()
    seen: set[tuple[str, ...]] = set()
    _enqueue(queue, seen, tokens)
    emitted = 0
    steps = 0
    while queue and emitted < INLINE_COMMAND_LIMIT and steps < INLINE_COMMAND_LIMIT:
        steps += 1
        current = queue.popleft()
        for command, separator in _direct_commands(current):
            if emitted >= INLINE_COMMAND_LIMIT:
                break
            yield command, separator
            emitted += 1
            _enqueue_bodies(queue, seen, _inline_bodies(command))
        if emitted >= INLINE_COMMAND_LIMIT:
            break
        _enqueue_bodies(queue, seen, _backtick_bodies(" ".join(current)))
    assert emitted <= INLINE_COMMAND_LIMIT
    assert steps <= INLINE_COMMAND_LIMIT
    assert len(queue) <= INLINE_COMMAND_LIMIT
