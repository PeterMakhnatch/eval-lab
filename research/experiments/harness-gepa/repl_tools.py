#!/usr/bin/env python3
"""Standard-library Python seed toolbox for agent environments.

Provides bounded inspection, regex search, and output checking.
Can be imported directly or executed via a minimal CLI.

Adversarial regex caveat:
Python's standard library `re` uses a backtracking engine that can experience
polynomial or exponential runtime on pathological patterns (ReDoS). While input
line lengths are strictly bounded to MAX_LINE_CHARS to mitigate impact, stdlib
`re` cannot guarantee bounded execution time for arbitrary adversarial regular
expressions.
"""
from __future__ import annotations

import argparse, collections, json, os, re, sys

MAX_WINDOW_LINES, DEFAULT_MAX_CHARS, MAX_CHARS_CAP = 200, 16384, 65536
DEFAULT_MAX_MATCHES, MAX_MATCHES_CAP, DEFAULT_CONTEXT, MAX_CONTEXT_CAP = 20, 100, 2, 5
MAX_FILE_BYTES, MAX_TOTAL_BYTES, MAX_VISITED_FILES = 5 * 1024 * 1024, 20 * 1024 * 1024, 250
MAX_TREE_DEPTH, MAX_LINE_CHARS = 8, 4096


def _read_line(f):
    line = f.readline(MAX_LINE_CHARS + 1)
    if len(line) > MAX_LINE_CHARS and not line.endswith("\n"):
        line = line[:MAX_LINE_CHARS] + "\n"
        while True:
            chunk = f.readline(MAX_LINE_CHARS + 1)
            if not chunk or chunk.endswith("\n"): break
    return line


def read_window(file, start=1, end=None, max_chars=DEFAULT_MAX_CHARS):
    """Read a bounded line window [start, end] (1-indexed) from a regular text file."""
    p = str(file)
    res = {"file": p, "start_line": start, "end_line": end, "content": "", "truncated": False, "error": None}
    if start < 1: return {**res, "error": f"Invalid start line {start}: must be >= 1"}
    if end is not None and end < start: return {**res, "error": f"Invalid line range: end ({end}) < start ({start})"}
    if os.path.islink(p): return {**res, "error": f"Symlinks are not supported: {p}"}
    if not os.path.exists(p): return {**res, "error": f"File not found: {p}"}
    if not os.path.isfile(p): return {**res, "error": f"Not a regular file: {p}"}
    try:
        with open(p, "rb") as bf:
            if b"\x00" in bf.read(1024): return {**res, "error": f"Binary file not supported: {p}"}
        eff_end = min(end, start + MAX_WINDOW_LINES - 1) if end is not None else start + MAX_WINDOW_LINES - 1
        max_c, trunc = min(max(1, int(max_chars)), MAX_CHARS_CAP), end is not None and end > eff_end
        buf, cur_c, cur_ln, bytes_read = [], 0, 0, 0
        with open(p, "r", encoding="utf-8") as f:
            while cur_ln < eff_end:
                line = _read_line(f)
                if not line: break
                bytes_read += len(line.encode("utf-8", errors="replace"))
                if bytes_read > MAX_FILE_BYTES: trunc = True; break
                cur_ln += 1
                if cur_ln < start: continue
                entry = f"{cur_ln}:{line}"
                if cur_c + len(entry) > max_c: trunc = True; break
                buf.append(entry); cur_c += len(entry)
        return {**res, "end_line": cur_ln, "content": "".join(buf), "truncated": trunc}
    except (UnicodeDecodeError, OSError) as exc:
        return {**res, "error": f"Read failed: {exc}"}


def _iter_files(root_dir):
    base_depth = root_dir.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(root_dir):
        if root.rstrip(os.sep).count(os.sep) - base_depth >= MAX_TREE_DEPTH:
            dirs.clear(); continue
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "node_modules", "venv", ".venv") and not os.path.islink(os.path.join(root, d))]
        for fn in sorted(files):
            if not fn.startswith("."):
                fp = os.path.join(root, fn)
                if not os.path.islink(fp) and os.path.isfile(fp): yield fp


def smart_grep(pattern, path=".", max_matches=DEFAULT_MAX_MATCHES, context=DEFAULT_CONTEXT):
    """Search for regex pattern in bounded file or tree; rejects symlinks/binaries."""
    p_str = str(path)
    res = {"pattern": pattern, "path": p_str, "matches": [], "total_matches": 0, "truncated": False, "error": None}
    try: regex = re.compile(pattern)
    except re.error as exc: return {**res, "error": f"Invalid regex pattern: {exc}"}
    if os.path.islink(p_str): return {**res, "error": f"Symlinks are not supported: {p_str}"}
    if not os.path.exists(p_str): return {**res, "error": f"Path not found: {p_str}"}
    if not (os.path.isfile(p_str) or os.path.isdir(p_str)): return {**res, "error": f"Unsupported path type: {p_str}"}

    max_m, ctx_n = min(max(1, int(max_matches)), MAX_MATCHES_CAP), min(max(0, int(context)), MAX_CONTEXT_CAP)
    files_iter = [p_str] if os.path.isfile(p_str) else _iter_files(p_str)
    visited, total_bytes, out_chars, matches, total_matches, trunc = 0, 0, 0, [], 0, False

    for fpath in files_iter:
        visited += 1
        if visited > MAX_VISITED_FILES: trunc = True; break
        try:
            fsize = os.path.getsize(fpath)
            if fsize > MAX_FILE_BYTES: continue
            if total_bytes + fsize > MAX_TOTAL_BYTES: trunc = True; break
            with open(fpath, "rb") as bf:
                if b"\x00" in bf.read(1024): continue
            pre_ctx, pending, ln, file_bytes = collections.deque(maxlen=ctx_n), [], 0, 0
            add_m = lambda pm: (matches.append({"file": fpath, "line": pm["line"], "match": pm["match"], "context": "".join(pm["ctx"])}), len(matches[-1]["context"]))
            with open(fpath, "r", encoding="utf-8") as tf:
                while True:
                    line = _read_line(tf)
                    if not line: break
                    line_bytes = len(line.encode("utf-8", errors="replace"))
                    file_bytes += line_bytes; total_bytes += line_bytes; ln += 1
                    for pm in pending:
                        if ln <= pm["end"]: pm["ctx"].append(f"{ln}- {line}")
                    for pm in [p for p in pending if ln >= p["end"]]:
                        _, clen = add_m(pm); out_chars += clen
                    pending = [p for p in pending if ln < p["end"]]

                    if len(matches) + len(pending) < max_m and regex.search(line):
                        total_matches += 1
                        m_text = line.rstrip("\r\n")
                        ctx_lines = [f"{pln}- {pline}" for pln, pline in pre_ctx] + [f"{ln}:{line}"]
                        if ctx_n == 0:
                            matches.append({"file": fpath, "line": ln, "match": m_text, "context": "".join(ctx_lines)})
                            out_chars += len(ctx_lines[0])
                        else: pending.append({"line": ln, "match": m_text, "ctx": ctx_lines, "end": ln + ctx_n})
                    elif regex.search(line): total_matches += 1; trunc = True

                    pre_ctx.append((ln, line))
                    if len(matches) >= max_m or out_chars >= MAX_CHARS_CAP or total_bytes >= MAX_TOTAL_BYTES or file_bytes >= MAX_FILE_BYTES:
                        if len(matches) >= max_m or out_chars >= MAX_CHARS_CAP: trunc = True
                        break

            for pm in pending:
                _, clen = add_m(pm); out_chars += clen
            if len(matches) >= max_m or out_chars >= MAX_CHARS_CAP or total_bytes >= MAX_TOTAL_BYTES: trunc = True; break
        except (UnicodeDecodeError, OSError): continue

    return {**res, "matches": matches, "total_matches": total_matches, "truncated": trunc}


def check_output(file, expected_format="auto"):
    """Check existence, true UTF-8 parseability, and schema shape. Never scores or writes."""
    p = str(file)
    res = {"file": p, "exists": False, "is_regular_file": False, "size_bytes": 0, "format": expected_format, "valid": False, "parse_error": None, "summary": None}
    if os.path.islink(p): return {**res, "parse_error": f"Symlinks are not supported: {p}"}
    if not os.path.exists(p): return {**res, "parse_error": f"File does not exist: {p}"}
    res["exists"] = True
    if not os.path.isfile(p): return {**res, "parse_error": f"Path exists but is not a regular file: {p}"}
    res["is_regular_file"] = True
    try:
        res["size_bytes"] = os.path.getsize(p)
        if res["size_bytes"] > MAX_FILE_BYTES: return {**res, "parse_error": f"File exceeds maximum size ({MAX_FILE_BYTES} bytes)"}
        with open(p, "rb") as raw_f: raw_bytes = raw_f.read(MAX_FILE_BYTES + 1)
        if len(raw_bytes) > MAX_FILE_BYTES: return {**res, "parse_error": f"File exceeds maximum size ({MAX_FILE_BYTES} bytes)"}
        text = raw_bytes.decode("utf-8")
        if expected_format == "json" or (expected_format == "auto" and (p.endswith(".json") or text.strip().startswith(("{", "[")))):
            data = json.loads(text)
            res.update({"format": "json", "valid": True})
            if isinstance(data, dict): res["summary"] = {"type": "object", "keys": sorted(data.keys())[:20], "num_keys": len(data)}
            elif isinstance(data, list): res["summary"] = {"type": "array", "length": len(data)}
            else: res["summary"] = {"type": type(data).__name__, "value": repr(data)[:100]}
        else:
            res.update({"format": "text", "valid": True, "summary": {"lines": len(text.splitlines()), "chars": len(text)}})
        return res
    except UnicodeDecodeError as ude: return {**res, "parse_error": f"UTF-8 decode failure: {ude}"}
    except json.JSONDecodeError as jde: return {**res, "valid": False, "parse_error": f"JSON parse error: {jde}"}
    except OSError as exc: return {**res, "parse_error": f"Error reading file: {exc}"}


def _build_cli():
    p = argparse.ArgumentParser(prog="repl_tools", description="Bounded inspection & output checking.")
    sub = p.add_subparsers(dest="subcommand")
    r = sub.add_parser("read")
    r.add_argument("file"); r.add_argument("--start", type=int, default=1); r.add_argument("--end", type=int, default=None); r.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    g = sub.add_parser("grep")
    g.add_argument("pattern"); g.add_argument("path", nargs="?", default="."); g.add_argument("--max-matches", type=int, default=DEFAULT_MAX_MATCHES); g.add_argument("--context", "-C", type=int, default=DEFAULT_CONTEXT)
    c = sub.add_parser("check")
    c.add_argument("file"); c.add_argument("--format", choices=["auto", "json", "text"], default="auto")
    return p


def main(argv=None):
    parser = _build_cli()
    args = parser.parse_args(argv)
    if not args.subcommand:
        parser.print_help(sys.stderr); return 1
    if args.subcommand == "read":
        res = read_window(args.file, start=args.start, end=args.end, max_chars=args.max_chars)
        if res["error"]: sys.stderr.write(f"Error: {res['error']}\n"); return 2
        sys.stdout.write(res["content"])
        if res["truncated"]: sys.stderr.write("\n[Note: truncated by max_chars]\n")
        return 0
    if args.subcommand == "grep":
        res = smart_grep(args.pattern, path=args.path, max_matches=args.max_matches, context=args.context)
        if res["error"]: sys.stderr.write(f"Error: {res['error']}\n"); return 2
        for m in res["matches"]:
            sep = "" if m["context"].endswith("\n") else "\n"
            sys.stdout.write(f"--- {m['file']}:{m['line']} ---\n{m['context']}{sep}")
        if res["truncated"]:
            sys.stderr.write(f"\n[Note: truncated at {len(res['matches'])} matches; total: {res['total_matches']}]\n")
        return 0 if res["total_matches"] > 0 else 1
    if args.subcommand == "check":
        res = check_output(args.file, expected_format=args.format)
        sys.stdout.write(json.dumps(res, indent=2) + "\n")
        return 0 if res["valid"] else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
