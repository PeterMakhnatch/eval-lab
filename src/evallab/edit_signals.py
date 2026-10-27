"""Shared edit detectors: tool names and command patterns (HAR-116 fix).

Single source of truth for ``EDIT_TOOL_NAMES`` and
``EDIT_COMMAND_PATTERNS``, used by the traj outline, token-flow analysis,
and the live HAR-116 loop break.

This module is stdlib-only on purpose. Harbor's agent venv
(``~/.local/share/uv/tools/harbor``) has no ``duckdb`` (and no
``polars``), so anything the live agent imports — the loop break via
``token_flow._is_edit`` — must never pull ``evallab.traj`` and its heavy
parquet dependencies. Keep it that way: no third-party imports here.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any, NamedTuple

#: Tool calls that count as a file edit.
EDIT_TOOL_NAMES = frozenset(
    {
        "ast_edit",
        "apply_patch",
        "create_file",
        "edit",
        "edit_file",
        "file_change",
        "patch",
        "sed",
        "write",
        "write_file",
    }
)
#: Shell commands that count as a file edit.
EDIT_COMMAND_PATTERNS = re.compile(
    r"\b("
    r"apply_patch|git\s+(?:apply|checkout\s+--)|"
    r"sed\s+-i|echo\s+.*>|cat\s+.*>|tee\s+|touch\s+|truncate\s+|"
    r"python\s+.*(?:write|open\(|write_text)|"
    r"node\s+.*(?:writeFileSync|writeFile)|fs\.writeFileSync"
    r")\b"
)


_HEREDOC_START_RE = re.compile(r"<<-?\s*['\"]?([A-Za-z0-9_]+)['\"]?")
_QUOTED_SPAN_RE = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")


def blank_quoted_and_heredocs(command: str) -> str:
    """Blank out quoted spans and heredoc bodies before regex matching.

    Prevents comparison operators (e.g. ``awk 'NR>=125 && NR<=240'``,
    ``python3 -c "print(1>0)"``, ``grep -n 'a>b'``) from being mistaken
    for shell redirects. Real redirects on the command line (e.g.
    ``cat <<'EOF' > f``, ``echo x > f``, ``cmd >> f``) are preserved
    because the opening command line is kept and only the heredoc body
    and quoted spans are blanked.
    """
    if not command:
        return ""
    lines = command.split("\n")
    kept: list[str] = []
    terminator: str | None = None
    for line in lines:
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        match = _HEREDOC_START_RE.search(line)
        if match:
            terminator = match.group(1)
            kept.append(line)
            continue
        kept.append(line)
    text = "\n".join(kept)
    return _QUOTED_SPAN_RE.sub(" ", text)


#: Tools whose ``keystrokes`` (Terminus-2), ``command`` (MiMo, OpenHands /
#: SWE-agent ``execute_bash``), or Codex ``exec`` script carry the shell commands
#: an agent step runs. ``execute_bash`` joined in tripwire rules v2.
SHELL_TOOL_NAMES = frozenset({"bash", "bash_command", "shell", "exec", "execute_bash"})

_SHELL_WRAPPER_RE = re.compile(r"^(?:(?:/usr)?/bin/)?(?:ba|z|da)?sh\s+-(?:l?c|cl)\s+")

#: Codex runs shell commands through a scripted ``exec`` tool whose ``input`` is
#: JavaScript calling ``tools.exec_command({cmd: "<shell>", ...})`` (older
#: runs quote the key: ``{"cmd": "<shell>"}``). One script may call it several times.
#: Only complete JSON string values count, not literal prefixes of JS expressions.
#: Comment matching must stop at the first terminator, not swallow an expression.
_CODEX_EXEC_CMD_RE = re.compile(
    r"exec_command\s*\(\s*(?P<obj>\{)[^{}]*?(?<![\w\"])(?:\"cmd\"|cmd)\s*:\s*"
    r'(?P<cmd>"(?:[^"\\]|\\.)*")'
    r"(?=(?:\s|/\*(?:[^*]|\*(?!/))*\*/|"
    r"//[^\r\n\u2028\u2029]*(?:[\r\n\u2028\u2029]|\Z))*[,}])"
)

#: ``tools.apply_patch(patch)`` scripts bind the patch to a JSON-escaped string
#: literal starting with ``"*** Begin Patch``.
_CODEX_PATCH_STR_RE = re.compile(r'"\*\*\* Begin Patch(?:[^"\\]|\\.)*"')


def unwrap_shell_wrapper(command: str) -> str:
    """Unwrap the harness's ``/bin/bash -lc '<script>'`` to ``<script>`` when unambiguous."""
    stripped = command.strip()
    if _SHELL_WRAPPER_RE.match(stripped):
        try:
            parts = shlex.split(stripped)
        except ValueError:
            return command
        if len(parts) >= 3 and parts[1] in ("-c", "-lc", "-cl"):
            return parts[2]
    return command


_OPENERS = "([{"
_CLOSERS = ")]}"
_WORKDIR_FIELD_RE = re.compile(r"""^\s*(?:"workdir"|'workdir'|workdir)\s*(?::|$)""")
_WORKDIR_LITERAL_RE = re.compile(
    r"""^\s*(?:"workdir"|'workdir'|workdir)\s*:\s*("(?:[^"\\\n]|\\.)*")\s*$"""
)


def _object_fields(script: str, start: int) -> list[str]:
    """Top-level field texts of the object literal opening at ``script[start]``.

    Strings and nested brackets are skipped and comments dropped, so a quoted
    ``workdir: "..."`` or a nested object's field is never a top-level field.
    An unterminated object yields only its terminated fields.
    """
    fields: list[str] = []
    current: list[str] = []
    depth = 0
    index = start + 1
    end = len(script)
    while index < end:
        char = script[index]
        if char in "\"'`":
            close = index + 1
            while close < end and script[close] != char:
                close += 2 if script[close] == "\\" else 1
            current.append(script[index : close + 1])
            index = close + 1
            continue
        if script.startswith("//", index):
            stop = re.compile(r"[\r\n\u2028\u2029]").search(script, index)
            index = stop.start() if stop else end
            continue
        if script.startswith("/*", index):
            stop_at = script.find("*/", index + 2)
            if stop_at == -1:
                return fields
            index = stop_at + 2
            current.append(" ")
            continue
        if char in _OPENERS:
            depth += 1
        elif char in _CLOSERS:
            if depth == 0:
                if char == "}":
                    fields.append("".join(current))
                return fields if char == "}" else []
            depth -= 1
        elif char == "," and depth == 0:
            fields.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    return fields


def _object_workdir(script: str, start: int) -> tuple[bool, str | None]:
    """``(present, literal)`` for the ``workdir`` field of the call object at ``start``."""
    found: list[str | None] = []
    for field in _object_fields(script, start):
        if _WORKDIR_FIELD_RE.match(field) is None:
            continue
        literal = _WORKDIR_LITERAL_RE.match(field)
        value: Any = None
        if literal is not None:
            try:
                value = json.loads(literal.group(1))
            except ValueError:
                value = None
        found.append(value if isinstance(value, str) and value.strip() else None)
    if not found:
        return False, None
    return True, found[0] if len(found) == 1 else None


def codex_exec_invocations(script: str) -> list[BashInvocation]:
    """Complete literal ``cmd`` values in a Codex ``exec`` script, in call order.

    Each carries the literal ``workdir`` of the SAME ``exec_command`` argument
    object (before or after ``cmd``). A computed, duplicated or non-string
    ``workdir`` is present-but-unknown; quoted text, comments and nested objects
    never supply one.
    """
    if not isinstance(script, str) or "exec_command" not in script:
        return []
    out: list[BashInvocation] = []
    for match in _CODEX_EXEC_CMD_RE.finditer(script):
        try:
            command = json.loads(match.group("cmd"))
        except ValueError:
            continue
        if isinstance(command, str) and command.strip():
            present, workdir = _object_workdir(script, match.start("obj"))
            out.append(BashInvocation(command.strip(), present, workdir))
    return out


def codex_exec_commands(script: str) -> list[str]:
    """Complete literal ``cmd`` values in a Codex ``exec`` script, in call order."""
    return [invocation.command for invocation in codex_exec_invocations(script)]


def codex_apply_patches(script: str) -> list[str]:
    """``*** Begin Patch`` payloads a Codex ``exec`` JS script applies (decoded)."""
    if not isinstance(script, str) or "Begin Patch" not in script:
        return []
    out: list[str] = []
    for match in _CODEX_PATCH_STR_RE.finditer(script):
        try:
            decoded = json.loads(match.group(0))
        except ValueError:
            continue
        if isinstance(decoded, str) and decoded.strip():
            out.append(decoded)
    return out


class BashInvocation(NamedTuple):
    """One decoded shell command and the working directory its own call object names.

    ``has_workdir`` is False when the source carries no per-command directory.
    When True, ``workdir`` is the complete literal directory, or ``None`` when
    the field is present but computed, duplicated or otherwise not a plain
    literal (unknown: callers must not guess).
    """

    command: str
    has_workdir: bool = False
    workdir: str | None = None


def tool_bash_invocations(call: dict[str, Any]) -> list[BashInvocation]:
    """Decode shell text, preserving legacy keystrokes and Codex call attribution.

    Only known shell tools interpret ``command`` as executable text. Legacy
    ``keystrokes`` and scripted Codex input keep their existing extraction
    semantics; a populated ``keystrokes`` always takes precedence.
    """
    args = call.get("arguments")
    if not isinstance(args, dict):
        return []
    keystrokes = args.get("keystrokes")
    if isinstance(keystrokes, str) and keystrokes.strip():
        return [BashInvocation(keystrokes)]
    name = str(call.get("function_name") or call.get("name") or "").lower()
    command = args.get("command")
    if name in SHELL_TOOL_NAMES and isinstance(command, str) and command.strip():
        return [BashInvocation(command)]
    script = args.get("input")
    return codex_exec_invocations(script) if isinstance(script, str) else []


def tool_bash_commands(call: dict[str, Any]) -> list[str]:
    """Shell command texts of one tool call (see :func:`tool_bash_invocations`)."""
    return [invocation.command for invocation in tool_bash_invocations(call)]


def step_bash_commands(step: dict[str, Any]) -> list[str]:
    """Shell command texts one ATIF step runs, in order.

    ``keystrokes`` or ``command`` of the shell tools (verbatim), plus the
    ``tools.exec_command({cmd: ...})`` calls inside a Codex ``exec`` script
    (``arguments.input``). Other argument shapes are not read.
    """
    out: list[str] = []
    calls = step.get("tool_calls")
    if not isinstance(calls, list):
        return out
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = str(call.get("function_name") or call.get("name") or "").lower()
        if name in SHELL_TOOL_NAMES:
            out.extend(tool_bash_commands(call))
    return out
