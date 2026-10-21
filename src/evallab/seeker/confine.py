"""Confinement: a macOS sandbox profile for OMP lanes, and in-process tools for the API lane.

Both lanes expose the same surface: read/grep/glob under ``task/``, write under
``submission/``. No shell, no execution, no reads of the user's work trees.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

DENIED_READ_HOME = (
    "Developer",
    "Documents",
    "Desktop",
    ".ssh",
    ".aws",
    ".gnupg",
    ".config/gh",
    "Library/Keychains",
)
MAX_READ_CHARS = 30_000
MAX_WRITE_CHARS = 200_000
MAX_GREP_MATCHES = 100
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".parquet", ".npy", ".pyc"}


def sandbox_profile(workspace: Path, home: Path | None = None) -> str:
    """SBPL: writes only inside the workspace (plus OMP's own state), no work-tree reads."""
    home = home or Path.home()
    ws = str(workspace.resolve())
    denied = " ".join(f'(subpath "{home / sub}")' for sub in DENIED_READ_HOME)
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny file-write*)\n"
        f'(allow file-write* (subpath "{ws}") (subpath "{home / ".omp"}") '
        f'(subpath "{home / ".cache"}") (subpath "/private/var/folders") (subpath "/dev"))\n'
        f"(deny file-read* {denied})\n"
    )


class ConfinedTools:
    """The API lane's tool implementations, rooted at one run workspace."""

    def __init__(self, workspace: Path) -> None:
        self.root = workspace.resolve()
        self.task = self.root / "task"
        self.submission = self.root / "submission"

    def _read_path(self, raw: str) -> Path:
        path = (self.root / raw).resolve()
        if not path.is_relative_to(self.task.resolve()):
            raise ValueError(f"reads are confined to task/: {raw}")
        return path

    def _write_path(self, raw: str) -> Path:
        path = (self.root / raw).resolve()
        if not path.is_relative_to(self.submission.resolve()):
            raise ValueError(f"writes are confined to submission/: {raw}")
        return path

    def list_files(self, path: str = "task") -> str:
        base = self._read_path(path)
        if not base.is_dir():
            return f"not a directory: {path}"
        rows = [
            p.relative_to(self.root).as_posix() + ("/" if p.is_dir() else f" ({p.stat().st_size} B)")
            for p in sorted(base.rglob("*"))
        ][:500]
        return "\n".join(rows) or "(empty)"

    def read_file(self, path: str, start_line: int = 1, end_line: int = 400) -> str:
        target = self._read_path(path)
        if target.suffix.lower() in SKIP_SUFFIXES or not target.is_file():
            return f"cannot read: {path}"
        lines = target.read_text(errors="replace").splitlines()
        start = max(1, start_line)
        chunk = lines[start - 1 : max(start, end_line)]
        text = "\n".join(f"{start + i}: {line}" for i, line in enumerate(chunk))
        return text[:MAX_READ_CHARS] + ("\n[truncated]" if len(text) > MAX_READ_CHARS else "")

    def grep(self, pattern: str, path: str = "task") -> str:
        base = self._read_path(path)
        regex = re.compile(pattern)
        hits: list[str] = []
        files = [base] if base.is_file() else [p for p in sorted(base.rglob("*")) if p.is_file()]
        for file in files:
            if file.suffix.lower() in SKIP_SUFFIXES or file.stat().st_size > 1_000_000:
                continue
            for n, line in enumerate(file.read_text(errors="replace").splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{file.relative_to(self.root).as_posix()}:{n}: {line[:300]}")
                    if len(hits) >= MAX_GREP_MATCHES:
                        return "\n".join(hits)
        return "\n".join(hits) or "(no matches)"

    def write_file(self, path: str, content: str) -> str:
        if len(content) > MAX_WRITE_CHARS:
            raise ValueError("file too large")
        target = self._write_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.parent.resolve().is_relative_to(self.submission.resolve()):
            raise ValueError(f"writes are confined to submission/: {path}")
        target.write_text(content)
        return f"wrote {len(content)} chars to {path}"

    def call(self, name: str, args: dict[str, Any]) -> str:
        handlers = {
            "list_files": lambda: self.list_files(str(args.get("path", "task"))),
            "read_file": lambda: self.read_file(
                str(args["path"]), int(args.get("start_line", 1)), int(args.get("end_line", 400))
            ),
            "grep": lambda: self.grep(str(args["pattern"]), str(args.get("path", "task"))),
            "write_file": lambda: self.write_file(str(args["path"]), str(args["content"])),
            "finish": lambda: "finished",
        }
        if name not in handlers:
            return f"unknown tool: {name}"
        try:
            return handlers[name]()
        except (ValueError, KeyError, OSError, re.error) as exc:
            return f"error: {exc}"


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }
    for name, description, properties, required in (
        (
            "list_files",
            "List files under task/ (paths relative to the workspace).",
            {"path": {"type": "string"}},
            [],
        ),
        (
            "read_file",
            "Read a text file under task/ with line numbers.",
            {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            ["path"],
        ),
        (
            "grep",
            "Regex search over text files under task/.",
            {"pattern": {"type": "string"}, "path": {"type": "string"}},
            ["pattern"],
        ),
        (
            "write_file",
            "Write a file under submission/ (your deliverables).",
            {"path": {"type": "string"}, "content": {"type": "string"}},
            ["path", "content"],
        ),
        ("finish", "Call when solve.sh and notes.json are written.", {"summary": {"type": "string"}}, []),
    )
]
