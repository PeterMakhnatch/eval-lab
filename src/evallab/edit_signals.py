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
