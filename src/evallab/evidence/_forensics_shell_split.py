from __future__ import annotations

import re
from collections import deque

from ._forensics_shell_lex import (
    FILE_TOKEN_RE,
    WORD_BREAK_CHARS,
    precompute_arith_ends,
    precompute_paren_ends,
    read_shell_word,
    skip_quoted_span,
    strip_line_continuations,
)

_HEREDOC_PARAM_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*:?=")


def _ends_with_unescaped_backslash(line: str) -> bool:
    bs = 0
    idx = len(line) - 1
    while idx >= 0 and line[idx] == "\\":
        bs += 1
        idx -= 1
    return bs % 2 == 1


def _step_heredoc_quote_stack(line: str, i: int, stack: list[str]) -> int:
    n = len(line)
    ch = line[i]
    top = stack[-1]
    if top == "'":
        if ch == "'":
            stack.pop()
        return i + 1
    if top == "$'":
        if ch == "\\" and i + 1 < n:
            return i + 2
        if ch == "'":
            stack.pop()
        return i + 1
    if ch == "\\" and i + 1 < n:
        return i + 2
    if line[i : i + 2] == "${":
        stack.append('"${')
        return i + 2
    if line[i : i + 2] == "$(":
        stack.append("$(")
        return i + 2
    if top == '"':
        if ch == '"':
            stack.pop()
        return i + 1
    if ch == '"':
        stack.append('"')
    elif ch == "}":
        stack.pop()
    return i + 1


def _read_heredoc_delim(
    line: str,
    j: int,
    arith_ends: dict[int, tuple[int, bool]],
) -> tuple[str, int, bool, bool] | None:
    n = len(line)
    k = j
    while k < n and line[k] not in WORD_BREAK_CHARS:
        if line[k : k + 3] == "$((":
            pair = arith_ends.get(k)
            if pair is not None:
                k = pair[0]
                continue
        if line[k : k + 2] in ("$'", '$"') or line[k] in ("'", '"'):
            end = skip_quoted_span(line + "\n", k)
            if end > n:
                return None
            k = end
            continue
        if line[k] == "\\":
            if k + 1 >= n:
                return None
            k += 2
            continue
        k += 1
    word, next_j, saw = read_shell_word(
        line, j, break_chars=WORD_BREAK_CHARS, arith_ends=arith_ends
    )
    raw = line[j:next_j]
    is_quoted = any(c in raw for c in ("'", '"', "\\"))
    return word, next_j, saw, is_quoted


def _scan_heredoc_line(
    line: str,
    stack: list[str],
    pending: deque[tuple[str, bool, bool]],
) -> str:
    arith_ends = precompute_arith_ends(line)
    dparen_ends = precompute_paren_ends(line, prefix="((", skip_dquotes=True)
    i = 0
    n = len(line)
    while i < n:
        top = stack[-1] if stack else ""
        if top in ("'", "$'", '"', '"${'):
            i = _step_heredoc_quote_stack(line, i, stack)
            continue
        ch = line[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if line[i : i + 2] == "$'":
            stack.append("$'")
            i += 2
            continue
        if ch in ("'", '"'):
            stack.append(ch)
            i += 1
            continue
        if ch == "#" and (i == 0 or line[i - 1] in " \t;|&("):
            return line[:i]
        if line[i : i + 3] == "$((":
            pair = arith_ends.get(i)
            i = pair[0] if pair is not None else i + 3
            continue
        if line[i : i + 2] == "((":
            pair = dparen_ends.get(i)
            i = pair[0] if pair is not None else i + 2
            continue
        if line[i : i + 2] == "<<":
            if i + 2 < n and line[i + 2] == "<":
                i += 3
                continue
            j = i + 2
            strip_tabs = j < n and line[j] == "-"
            j += int(strip_tabs)
            while j < n and line[j] in (" ", "\t"):
                j += 1
            parsed = _read_heredoc_delim(line, j, arith_ends)
            if parsed is None:
                i = j
                continue
            word, next_j, saw, is_quoted = parsed
            if saw:
                pending.append((word, strip_tabs, is_quoted))
            i = next_j if next_j > j else j
            continue
        if line[i : i + 2] == "$(":
            stack.append("$(")
            i += 2
            continue
        if top in ("$(", "(") and ch in ("(", ")"):
            stack.append("(") if ch == "(" else stack.pop()
        i += 1
    return line


def _unquoted_heredoc_mutates(body: str) -> bool:
    if "$" not in body and "`" not in body:
        return False
    buf: list[str] = []
    idx, raw_n = 0, len(body)
    while idx < raw_n:
        if body[idx] == "\\" and idx + 1 < raw_n:
            if body[idx + 1] != "\n":
                buf.append(body[idx : idx + 2])
            idx += 2
            continue
        buf.append(body[idx])
        idx += 1
    joined = "".join(buf)
    arith_ends = precompute_arith_ends(joined)
    i, n, brace_depth = 0, len(joined), 0
    while i < n:
        ch = joined[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "`":
            return True
        if joined[i : i + 3] == "$((":
            pair = arith_ends.get(i)
            if pair is None or pair[1]:
                return True
            i = pair[0]
            continue
        if joined[i : i + 2] == "$(":
            return True
        if joined[i : i + 2] == "${":
            if _HEREDOC_PARAM_ASSIGN_RE.match(joined[i + 2 :]):
                return True
            brace_depth += 1
            i += 2
            continue
        if ch == "}" and brace_depth > 0:
            brace_depth -= 1
        i += 1
    return brace_depth > 0


def strip_heredocs_checked(cmd: str) -> tuple[str, bool]:
    kept: list[str] = []
    unquoted_body: list[str] = []
    pending: deque[tuple[str, bool, bool]] = deque()
    stack: list[str] = []
    continued = False
    mutates = False
    for line in cmd.split("\n"):
        if pending and not continued and (not stack or stack[-1] in ("$(", "(")):
            delim, strip_tabs, is_quoted = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == delim:
                pending.popleft()
                if not is_quoted and unquoted_body:
                    mutates = mutates or _unquoted_heredoc_mutates("\n".join(unquoted_body))
                    unquoted_body.clear()
            elif not is_quoted:
                unquoted_body.append(line)
            continue
        code_line = _scan_heredoc_line(line, stack, pending)
        kept.append(code_line)
        continued = (not stack or stack[-1] not in ("'", "$'")) and _ends_with_unescaped_backslash(
            code_line
        )
    if unquoted_body:
        mutates = mutates or _unquoted_heredoc_mutates("\n".join(unquoted_body))
    return "\n".join(kept), mutates


def strip_heredocs(cmd: str) -> str:
    return strip_heredocs_checked(cmd)[0]


def split_shell_segments(cmd: str, *, split_pipes: bool = False) -> list[str]:
    cmd = strip_line_continuations(cmd)
    arith_ends = precompute_arith_ends(cmd)
    segs: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if cmd[i : i + 2] == "$'" or ch in ("'", '"'):
            j = skip_quoted_span(cmd, i)
            buf.append(cmd[i:j])
            i = j
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(cmd[i : i + 2])
            i += 2
            continue
        if ch == "#" and (not buf or buf[-1] in " \t\n;|&("):
            while i < n and cmd[i] != "\n":
                i += 1
            continue
        if cmd[i : i + 3] == "$((":
            pair = arith_ends.get(i)
            if pair is not None:
                buf.append(cmd[i : pair[0]])
                i = pair[0]
                continue
        if ch in (";", "\n"):
            segs.append("".join(buf))
            buf.clear()
            i += 1
            continue
        if ch == "&":
            if i + 1 < n and cmd[i + 1] == "&":
                segs.append("".join(buf))
                buf.clear()
                i += 2
                continue
            prev_redir = bool(buf) and buf[-1] in (">", "<", "|")
            next_redir = i + 1 < n and cmd[i + 1] == ">"
            if not prev_redir and not next_redir:
                segs.append("".join(buf))
                buf.clear()
                i += 1
                continue
        if ch == "|":
            if i + 1 < n and cmd[i + 1] == "|":
                segs.append("".join(buf))
                buf.clear()
                i += 2
                continue
            if split_pipes and not (bool(buf) and buf[-1] == ">"):
                segs.append("".join(buf))
                buf.clear()
                i += 2 if i + 1 < n and cmd[i + 1] == "&" else 1
                continue
        buf.append(ch)
        i += 1
    if buf:
        segs.append("".join(buf))
    return segs


def _consume_out_redirect(
    seg: str,
    i: int,
    *,
    has_prefix: bool,
    arith_ends: dict[int, tuple[int, bool]],
    out_redirected: set[str],
) -> int:
    n = len(seg)
    j = i + 2 if has_prefix else i + 1
    if j < n and seg[j] in (">", "&", "|"):
        j += 1
    while j < n and seg[j] in (" ", "\t"):
        j += 1
    tgt, next_j, saw = read_shell_word(seg, j, break_chars=WORD_BREAK_CHARS, arith_ends=arith_ends)
    if saw and FILE_TOKEN_RE.match(tgt):
        out_redirected.add(tgt)
    return next_j if next_j > j else j


def _consume_in_redirect(
    seg: str,
    i: int,
    *,
    has_prefix: bool,
    arith_ends: dict[int, tuple[int, bool]],
    in_redirected: list[str],
) -> tuple[int, str]:
    n = len(seg)
    op_pos = i + 1 if has_prefix else i
    if seg[op_pos : op_pos + 2] == "<(":
        return i + 1, seg[i]
    j = op_pos + 1
    is_heredoc = seg[op_pos : op_pos + 2] == "<<"
    is_dup = seg[op_pos : op_pos + 2] == "<&"
    if is_heredoc:
        j += 1
        if j < n and seg[j] in ("<", "-"):
            j += 1
    elif j < n and seg[j] in ("&", ">"):
        j += 1
    while j < n and seg[j] in (" ", "\t"):
        j += 1
    tgt, next_j, saw = read_shell_word(seg, j, break_chars=WORD_BREAK_CHARS, arith_ends=arith_ends)
    if saw and not is_heredoc and not is_dup and FILE_TOKEN_RE.match(tgt):
        in_redirected.append(tgt)
    return (next_j if next_j > j else j), " "


def strip_unquoted_redirects(seg: str) -> tuple[str, set[str], list[str]]:
    seg = strip_line_continuations(seg)
    arith_ends = precompute_arith_ends(seg)
    out: list[str] = []
    out_redirected: set[str] = set()
    in_redirected: list[str] = []
    i = 0
    n = len(seg)
    while i < n:
        ch = seg[i]
        if seg[i : i + 2] == "$'" or ch in ("'", '"'):
            j = skip_quoted_span(seg, i)
            out.append(seg[i:j])
            i = j
            continue
        if ch == "\\" and i + 1 < n:
            out.append(seg[i : i + 2])
            i += 2
            continue
        if seg[i : i + 3] == "$((":
            pair = arith_ends.get(i)
            if pair is not None:
                out.append(seg[i : pair[0]])
                i = pair[0]
                continue
        has_out = (
            ch in "0123456789&"
            and i + 1 < n
            and seg[i + 1] == ">"
            and (i == 0 or seg[i - 1] in " \t;|&(")
        )
        if ch == ">" or has_out:
            i = _consume_out_redirect(
                seg, i, has_prefix=has_out, arith_ends=arith_ends, out_redirected=out_redirected
            )
            out.append(" ")
            continue
        has_in = (
            ch in "0123456789"
            and i + 1 < n
            and seg[i + 1] == "<"
            and (i == 0 or seg[i - 1] in " \t;|&(")
        )
        if ch == "<" or has_in:
            i, piece = _consume_in_redirect(
                seg, i, has_prefix=has_in, arith_ends=arith_ends, in_redirected=in_redirected
            )
            out.append(piece)
            continue
        out.append(ch)
        i += 1
    return "".join(out), out_redirected, in_redirected
