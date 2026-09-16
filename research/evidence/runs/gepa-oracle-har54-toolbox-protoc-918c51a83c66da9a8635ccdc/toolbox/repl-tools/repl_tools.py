#!/usr/bin/env python3
"""Bounded, read-only file helpers. JSON validity is not task correctness.

Regex input is bounded, but Python's backtracking regex engine has no execution
 time guarantee; the containing Harbor trial supplies the execution deadline.
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import re
from pathlib import Path

MAX_BYTES = 262144
MAX_TOTAL_BYTES = 1048576
MAX_ENTRIES = 128
MAX_DEPTH = 8
MAX_CHARS = 16384
MAX_LINES = 200
MAX_LINE_CHARS = 4096


def _path(value):
    path = Path(value).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("symlinks are not supported")
    return path


def _text(path, limit=MAX_BYTES):
    if not path.is_file():
        raise ValueError("not a regular file")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    truncated = len(data) > limit
    prefix = data[:limit]
    if b"\x00" in prefix:
        raise ValueError("binary content is not supported")
    decoder = codecs.getincrementaldecoder("utf-8")()
    return decoder.decode(prefix, final=not truncated), truncated, len(data)


def read_window(file, start=1, end=None, max_chars=MAX_CHARS):
    """Read a numbered inclusive line window from a bounded UTF-8 prefix."""
    try:
        if (
            type(start) is not int
            or start < 1
            or (end is not None and (type(end) is not int or end < start))
        ):
            raise ValueError("require 1 <= start <= end")
        if type(max_chars) is not int or not 1 <= max_chars <= MAX_CHARS:
            raise ValueError(f"max_chars must be in 1..{MAX_CHARS}")
        text, truncated, _ = _text(_path(file))
        lines = text.splitlines()
        stop = min(end if end is not None else len(lines), start + MAX_LINES - 1, len(lines))
        requested_stop = min(end if end is not None else len(lines), len(lines))
        truncated |= stop < requested_stop
        content = ""
        for number in range(start, stop + 1):
            line = lines[number - 1]
            entry = f"{number}:{line[:MAX_LINE_CHARS]}\n"
            truncated |= len(line) > MAX_LINE_CHARS
            remaining = max_chars - len(content)
            if len(entry) > remaining:
                content += entry[:remaining]
                truncated = True
                break
            content += entry
        return {"content": content, "truncated": truncated, "error": None}
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        return {"content": "", "truncated": False, "error": str(exc)}


def _files(root):
    if root.is_file():
        return [root], False
    if not root.is_dir():
        raise ValueError("not a regular file or directory")
    files, stack, visited, truncated = [], [(root, 0)], 0, False
    while stack:
        directory, depth = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                visited += 1
                if visited > MAX_ENTRIES:
                    return sorted(files), True
                if entry.is_symlink() or entry.name.startswith("."):
                    continue
                if entry.is_file(follow_symlinks=False):
                    files.append(Path(entry.path))
                elif entry.is_dir(follow_symlinks=False):
                    if depth < MAX_DEPTH:
                        stack.append((Path(entry.path), depth + 1))
                    else:
                        truncated = True
    return sorted(files), truncated


def smart_grep(pattern, path=".", max_matches=20, context=2):
    """Search bounded files; reported matches are not a full-tree match count."""
    matches, used_bytes, used_chars, truncated, skipped = [], 0, 0, False, 0
    try:
        if type(max_matches) is not int or not 1 <= max_matches <= 100:
            raise ValueError("max_matches must be in 1..100")
        if type(context) is not int or not 0 <= context <= 5:
            raise ValueError("context must be in 0..5")
        if not isinstance(pattern, str) or len(pattern) > 1024:
            raise ValueError("pattern must be at most 1024 characters")
        expression = re.compile(pattern)
        files, truncated = _files(_path(path))
        for file in files:
            remaining = MAX_TOTAL_BYTES - used_bytes
            if remaining <= 0:
                truncated = True
                break
            limit = min(MAX_BYTES, remaining)
            try:
                text, partial, count = _text(file, limit)
            except (OSError, UnicodeError, ValueError):
                used_bytes += limit + 1
                skipped += 1
                truncated = True
                continue
            used_bytes += count
            truncated |= partial
            lines = text.splitlines()
            for index, line in enumerate(lines):
                truncated |= len(line) > MAX_LINE_CHARS
                if not expression.search(line[:MAX_LINE_CHARS]):
                    continue
                excerpt = "".join(
                    f"{i + 1}:{lines[i][:MAX_LINE_CHARS]}\n"
                    for i in range(max(0, index - context), min(len(lines), index + context + 1))
                )
                remaining_chars = MAX_CHARS - used_chars
                if len(excerpt) > remaining_chars:
                    excerpt = excerpt[:remaining_chars]
                    truncated = True
                matches.append({"file": str(file), "line": index + 1, "context": excerpt})
                used_chars += len(excerpt)
                if len(matches) >= max_matches or used_chars >= MAX_CHARS:
                    return {
                        "matches": matches,
                        "truncated": True,
                        "skipped_files": skipped,
                        "error": None,
                    }
        return {"matches": matches, "truncated": truncated, "skipped_files": skipped, "error": None}
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        return {"matches": matches, "truncated": True, "skipped_files": skipped, "error": str(exc)}


def check_output(file, expected_format="auto"):
    """Check complete UTF-8/JSON parseability only; never score task correctness."""
    try:
        if expected_format not in {"auto", "json", "text"}:
            raise ValueError("format must be auto, json, or text")
        path = _path(file)
        text, truncated, _ = _text(path)
        if truncated:
            raise ValueError("file exceeds complete validation byte limit")
        kind = (
            "json"
            if expected_format == "json" or (expected_format == "auto" and path.suffix == ".json")
            else "text"
        )

        def reject_constant(value):
            raise ValueError(f"non-JSON numeric constant: {value}")

        if kind == "json":
            value = json.loads(text, parse_constant=reject_constant)
            shape = type(value).__name__
        else:
            shape = "text"
        return {
            "valid": True,
            "format": kind,
            "shape": shape,
            "task_correctness": "not_checked",
            "error": None,
        }
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        return {"valid": False, "task_correctness": "not_checked", "error": str(exc)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("read")
    read.add_argument("file")
    read.add_argument("--start", type=int, default=1)
    read.add_argument("--end", type=int)
    read.add_argument("--max-chars", type=int, default=MAX_CHARS)
    grep = commands.add_parser("grep")
    grep.add_argument("pattern")
    grep.add_argument("path", nargs="?", default=".")
    grep.add_argument("--max-matches", type=int, default=20)
    grep.add_argument("--context", type=int, default=2)
    check = commands.add_parser("check")
    check.add_argument("file")
    check.add_argument("--format", choices=["auto", "json", "text"], default="auto")
    args = vars(parser.parse_args())
    command = args.pop("command")
    if command == "check":
        args["expected_format"] = args.pop("format")
    result = {"read": read_window, "grep": smart_grep, "check": check_output}[command](**args)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
