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
from typing import Any

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


#: Tools whose ``keystrokes`` (Terminus-2), ``command`` (MiMo), or Codex
#: ``exec`` script carry the shell commands an agent step runs.
SHELL_TOOL_NAMES = frozenset({"bash", "bash_command", "shell", "exec"})

_SHELL_WRAPPER_RE = re.compile(r"^(?:(?:/usr)?/bin/)?(?:ba|z|da)?sh\s+-(?:l?c|cl)\s+")

#: Codex runs shell commands through a scripted ``exec`` tool whose ``input`` is
#: JavaScript calling ``tools.exec_command({cmd: "<shell>", ...})`` (older
#: runs quote the key: ``{"cmd": "<shell>"}``). One script may call it several times.
_CODEX_EXEC_CMD_RE = re.compile(
    r'exec_command\s*\(\s*\{[^{}]*?(?<![\w"])(?:"cmd"|cmd)\s*:\s*("(?:[^"\\]|\\.)*")'
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


def codex_exec_commands(script: str) -> list[str]:
    """Shell command strings a Codex ``exec`` JS script runs (in call order)."""
    if not isinstance(script, str) or "exec_command" not in script:
        return []
    out: list[str] = []
    for match in _CODEX_EXEC_CMD_RE.finditer(script):
        try:
            command = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(command, str) and command.strip():
            out.append(command.strip())
    return out


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


def tool_bash_commands(call: dict[str, Any]) -> list[str]:
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
        return [keystrokes]
    name = str(call.get("function_name") or call.get("name") or "").lower()
    command = args.get("command")
    if name in SHELL_TOOL_NAMES and isinstance(command, str) and command.strip():
        return [command]
    script = args.get("input")
    return codex_exec_commands(script) if isinstance(script, str) else []


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
