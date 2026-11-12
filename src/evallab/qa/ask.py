"""Read-only evidence tools for a sealed QA Trace workspace."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

MAX_TOOL_CHARS = 16_000
MAX_READ_LINES = 200


class WorkspaceTools:
    """A capability limited to the sealed file inventory, with no executable tools."""

    def __init__(
        self, root: Path, files: set[str], *, max_read_lines: int = MAX_READ_LINES,
        default_read_lines: int = 100, max_chars: int = MAX_TOOL_CHARS, clamp_reads: bool = False,
    ) -> None:
        self.root = root.resolve(strict=True)
        self.files = files - {"bundle.json"}
        self.max_read_lines = max_read_lines
        self.default_read_lines = default_read_lines
        self.max_chars = max_chars
        #: Clip an over-long range to the window (with a continuation note) instead of refusing it.
        self.clamp_reads = clamp_reads

    def _relative(self, raw: str) -> str:
        if raw.startswith("/workspace/"):
            raw = raw.removeprefix("/workspace/")
        elif raw == "/workspace":
            raw = "."
        path = PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts or ":" in raw or "\\" in raw:
            raise ValueError("only workspace-relative evidence paths are allowed")
        return str(path)

    def _path(self, rel: str) -> Path:
        rel = self._relative(rel)
        if rel not in self.files:
            raise ValueError("path is not in the sealed evidence inventory")
        path = self.root / rel
        for part in (path, *path.parents):
            if part == self.root:
                break
            if part.is_symlink():
                raise ValueError("evidence symlinks are forbidden")
        if not path.resolve(strict=True).is_relative_to(self.root):
            raise ValueError("path leaves the workspace")
        return path

    def _read(self, rel: str) -> str:
        return self._path(rel).read_text(errors="replace")

    def run(self, tool: str, args: dict[str, Any]) -> str:
        if tool not in {"read", "grep", "diff"}:
            raise ValueError("only read, grep and diff are allowed")
        allowed = {"path", "pattern"} if tool == "grep" else {"path", "start_line", "end_line"}
        if set(args) - allowed:
            raise ValueError("unknown tool argument")
        raw = args.get("path", "." if tool == "grep" else "")
        if not isinstance(raw, str) or not raw:
            raise ValueError("a nonempty evidence path is required")
        rel = self._relative(raw)
        if tool == "grep":
            pattern = args.get("pattern")
            if not isinstance(pattern, str) or not pattern or len(pattern) > 500:
                raise ValueError("grep needs a literal pattern of 1..500 characters")
            candidates = self._files_below(rel)
            if not candidates:
                raise ValueError("path is not in the sealed evidence inventory")
            rows, size = [], 0
            for name in candidates:
                for number, line in enumerate(self._read(name).splitlines(), 1):
                    if pattern.casefold() in line.casefold():
                        row = f"{name}:{number}: {line}"
                        if size + len(row) + 1 > self.max_chars:
                            return "\n".join(rows) + "\n[more matches omitted; narrow the search]"
                        rows.append(row)
                        size += len(row) + 1
            return "\n".join(rows) or "[no matches in retained evidence]"
        if rel not in self.files:
            if tool == "diff":
                raise ValueError("diff needs a retained .diff or .patch file")
            names = self._files_below(rel)
            if not names:
                raise ValueError("path is not in the sealed evidence inventory")
            return ("\n".join(names))[: self.max_chars]
        if tool == "diff" and not rel.endswith((".diff", ".patch")):
            raise ValueError("diff needs a retained .diff or .patch file")
        start, end = args.get("start_line", 1), args.get("end_line")
        end = start + self.default_read_lines - 1 if end is None and type(start) is int else end
        if type(start) is not int or type(end) is not int or not 1 <= start <= end:
            raise ValueError("line bounds must be positive ordered integers")
        clamped = None
        if end - start + 1 > self.max_read_lines:
            if not self.clamp_reads:
                raise ValueError(f"read at most {self.max_read_lines} lines per call")
            clamped, end = end, start + self.max_read_lines - 1
        lines = self._read(rel).splitlines()
        rows, size = [], 0
        for number in range(start, min(end, len(lines)) + 1):
            row = f"{rel}:{number}: {lines[number - 1]}"
            remaining = self.max_chars - size
            if len(row) + 1 > remaining:
                rows.append(row[: max(0, remaining)] + "\n[line or range clipped; narrow the read]")
                break
            rows.append(row)
            size += len(row) + 1
        if clamped is not None and rows and end < min(clamped, len(lines)):
            rows.append(f"[read clipped to {self.max_read_lines} lines; continue at "
                        f"{rel}:{end + 1} (total lines: {len(lines)})]")
        return "\n".join(rows) or f"[no lines at {rel}:{start}; total lines: {len(lines)}]"

    def _files_below(self, rel: str) -> list[str]:
        return sorted(
            name for name in self.files if rel == "." or name == rel or name.startswith(rel + "/")
        )
