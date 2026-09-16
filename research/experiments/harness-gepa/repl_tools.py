#!/usr/bin/env python3
"""Standard-library Python seed toolbox for agent environments.

Provides bounded inspection, regex search, and output checking.
Can be imported directly or executed via a minimal CLI.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Union

DEFAULT_MAX_LINES = 200
DEFAULT_MAX_CHARS = 16384
DEFAULT_MAX_MATCHES = 20
DEFAULT_CONTEXT = 2
DEFAULT_MAX_BYTES = 10 * 1024 * 1024


def read_window(
    file: Union[str, os.PathLike],
    start: int = 1,
    end: Optional[int] = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> Dict[str, Any]:
    """Read a bounded line window [start, end] (1-indexed) from a regular text file."""
    path_str = str(file)
    res: Dict[str, Any] = {
        "file": path_str,
        "start_line": start,
        "end_line": end,
        "total_lines": 0,
        "content": "",
        "truncated": False,
        "error": None,
    }
    if start < 1:
        res["error"] = f"Invalid start line {start}: must be >= 1"
        return res
    if end is not None and end < start:
        res["error"] = f"Invalid line range: end ({end}) < start ({start})"
        return res
    if not os.path.exists(path_str):
        res["error"] = f"File not found: {path_str}"
        return res
    if not os.path.isfile(path_str):
        res["error"] = f"Not a regular file: {path_str}"
        return res
    try:
        if os.path.getsize(path_str) > DEFAULT_MAX_BYTES:
            res["error"] = f"File exceeds maximum size ({DEFAULT_MAX_BYTES} bytes)"
            return res
        with open(path_str, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        res["total_lines"] = total
        eff_end = max(start, end if end is not None else min(start + DEFAULT_MAX_LINES - 1, total))
        res["end_line"] = eff_end
        if start > total:
            return res
        buf, cur_chars, truncated = [], 0, False
        for idx, line in enumerate(lines[start - 1 : eff_end], start=start):
            entry = f"{idx}:{line}"
            if cur_chars + len(entry) > max_chars:
                truncated = True
                break
            buf.append(entry)
            cur_chars += len(entry)
        res["content"] = "".join(buf)
        res["truncated"] = truncated
        return res
    except Exception as exc:
        res["error"] = f"Read failed: {exc}"
        return res


def smart_grep(
    pattern: str,
    path: Union[str, os.PathLike] = ".",
    max_matches: int = DEFAULT_MAX_MATCHES,
    context: int = DEFAULT_CONTEXT,
) -> Dict[str, Any]:
    """Search for regex pattern in a file or bounded tree; excludes binary files."""
    path_str = str(path)
    res: Dict[str, Any] = {
        "pattern": pattern,
        "path": path_str,
        "matches": [],
        "total_matches": 0,
        "truncated": False,
        "error": None,
    }
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        res["error"] = f"Invalid regex pattern: {exc}"
        return res
    if not os.path.exists(path_str):
        res["error"] = f"Path not found: {path_str}"
        return res

    files_to_scan: List[str] = []
    if os.path.isfile(path_str):
        files_to_scan.append(path_str)
    elif os.path.isdir(path_str):
        for root, dirs, filenames in os.walk(path_str):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "node_modules")]
            for fn in sorted(filenames):
                if not fn.startswith("."):
                    p = os.path.join(root, fn)
                    if os.path.isfile(p):
                        files_to_scan.append(p)
    else:
        res["error"] = f"Unsupported path type: {path_str}"
        return res

    matches_list, total_matches, truncated = [], 0, False
    for fpath in files_to_scan:
        try:
            if os.path.getsize(fpath) > DEFAULT_MAX_BYTES:
                continue
            with open(fpath, "rb") as raw_f:
                if b"\x00" in raw_f.read(1024):
                    continue
            with open(fpath, "r", encoding="utf-8", errors="replace") as txt_f:
                lines = txt_f.readlines()
            for i, line in enumerate(lines):
                if regex.search(line):
                    total_matches += 1
                    if len(matches_list) < max_matches:
                        ctx = [
                            f"{ln + 1}{':' if ln == i else '- '}{lines[ln]}"
                            for ln in range(max(0, i - context), min(len(lines), i + context + 1))
                        ]
                        matches_list.append({"file": fpath, "line": i + 1, "match": line.rstrip("\r\n"), "context": "".join(ctx)})
                    else:
                        truncated = True
        except Exception:
            continue
    res.update({"matches": matches_list, "total_matches": total_matches, "truncated": truncated})
    return res


def check_output(
    file: Union[str, os.PathLike],
    expected_format: str = "auto",
) -> Dict[str, Any]:
    """Check existence, UTF-8 validity, and JSON/text shape. Never evaluates task score."""
    path_str = str(file)
    res: Dict[str, Any] = {
        "file": path_str,
        "exists": False,
        "is_regular_file": False,
        "size_bytes": 0,
        "format": expected_format,
        "valid": False,
        "parse_error": None,
        "summary": None,
    }
    if not os.path.exists(path_str):
        res["parse_error"] = f"File does not exist: {path_str}"
        return res
    res["exists"] = True
    if not os.path.isfile(path_str):
        res["parse_error"] = f"Path exists but is not a regular file: {path_str}"
        return res
    res["is_regular_file"] = True
    try:
        res["size_bytes"] = os.path.getsize(path_str)
        with open(path_str, "rb") as raw_f:
            raw_bytes = raw_f.read()
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as ude:
            res["parse_error"] = f"UTF-8 decode failure: {ude}"
            return res
        is_json = expected_format == "json" or (
            expected_format == "auto" and (path_str.endswith(".json") or text.strip().startswith(("{", "[")))
        )
        if is_json:
            res["format"] = "json"
            try:
                data = json.loads(text)
                res["valid"] = True
                if isinstance(data, dict):
                    res["summary"] = {"type": "object", "keys": sorted(list(data.keys()))[:20], "num_keys": len(data)}
                elif isinstance(data, list):
                    res["summary"] = {"type": "array", "length": len(data)}
                else:
                    res["summary"] = {"type": type(data).__name__, "value": repr(data)[:100]}
            except json.JSONDecodeError as jde:
                res["valid"] = False
                res["parse_error"] = f"JSON parse error: {jde}"
        else:
            res["format"] = "text"
            res["valid"] = True
            res["summary"] = {"lines": len(text.splitlines()), "chars": len(text)}
        return res
    except Exception as exc:
        res["parse_error"] = f"Error checking output file: {exc}"
        return res


def _build_cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repl_tools", description="Standard library toolbox for agent inspection.")
    sub = p.add_subparsers(dest="subcommand", help="Subcommand")
    p_read = sub.add_parser("read", help="Read bounded line window")
    p_read.add_argument("file", help="Path to text file")
    p_read.add_argument("--start", type=int, default=1, help="Starting line (1-indexed, default: 1)")
    p_read.add_argument("--end", type=int, default=None, help="Ending line (inclusive)")
    p_read.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Max characters")
    p_grep = sub.add_parser("grep", help="Regex search in file or tree")
    p_grep.add_argument("pattern", help="Regex pattern")
    p_grep.add_argument("path", nargs="?", default=".", help="Search path")
    p_grep.add_argument("--max-matches", type=int, default=DEFAULT_MAX_MATCHES, help="Max matches")
    p_grep.add_argument("--context", "-C", type=int, default=DEFAULT_CONTEXT, help="Context lines")
    p_check = sub.add_parser("check", help="Check output file validity")
    p_check.add_argument("file", help="Output file path")
    p_check.add_argument("--format", choices=["auto", "json", "text"], default="auto", help="Expected format")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_cli()
    args = parser.parse_args(argv)
    if not args.subcommand:
        parser.print_help(sys.stderr)
        return 1
    if args.subcommand == "read":
        res = read_window(file=args.file, start=args.start, end=args.end, max_chars=args.max_chars)
        if res["error"]:
            sys.stderr.write(f"Error: {res['error']}\n")
            return 2
        sys.stdout.write(res["content"])
        if res["truncated"]:
            sys.stderr.write("\n[Note: truncated by max_chars]\n")
        return 0
    elif args.subcommand == "grep":
        res = smart_grep(pattern=args.pattern, path=args.path, max_matches=args.max_matches, context=args.context)
        if res["error"]:
            sys.stderr.write(f"Error: {res['error']}\n")
            return 2
        for m in res["matches"]:
            sys.stdout.write(f"--- {m['file']}:{m['line']} ---\n{m['context']}")
            if not m["context"].endswith("\n"):
                sys.stdout.write("\n")
        if res["truncated"]:
            sys.stderr.write(f"\n[Note: truncated at {len(res['matches'])} matches; total: {res['total_matches']}]\n")
        return 0 if res["total_matches"] > 0 else 1
    elif args.subcommand == "check":
        res = check_output(file=args.file, expected_format=args.format)
        sys.stdout.write(json.dumps(res, indent=2) + "\n")
        return 0 if res["valid"] else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
