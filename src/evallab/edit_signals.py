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

import re

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
