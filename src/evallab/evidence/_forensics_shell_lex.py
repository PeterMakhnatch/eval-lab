from __future__ import annotations

import re

FILE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,10}$")
WORD_BREAK_CHARS = frozenset(" \t\n;|&<>()")
_ANSIC_SIMPLE = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_OCT_DIGITS = frozenset("01234567")


def _skip_dquote_span(text: str, i: int) -> int:
    n, i = len(text), i + 1
    stack: list[str] = ['"']
    while i < n and stack:
        top, ch, two = stack[-1], text[i], text[i : i + 2]
        if top == "'":
            if ch == "'":
                stack.pop()
            i += 1
        elif ch == "\\" and i + 1 < n:
            i += 2
        elif top == "$'":
            if ch == "'":
                stack.pop()
            i += 1
        elif top in ('"', '"${'):
            if two == "${":
                stack.append('"${')
                i += 2
            elif two == "$(":
                stack.append("$(")
                i += 2
            else:
                if top == '"' and ch == '"':
                    stack.pop()
                elif top == '"${' and ch in ('"', "}"):
                    stack.append('"') if ch == '"' else stack.pop()
                i += 1
        elif two == "$'":
            stack.append("$'")
            i += 2
        else:
            if ch in ("'", '"', "("):
                stack.append(ch)
            elif ch == ")":
                stack.pop()
            i += 1
    return i


def skip_quoted_span(text: str, i: int) -> int:
    n = len(text)
    if text[i : i + 2] == "$'":
        i += 2
        while i < n:
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == "'":
                return i + 1
            i += 1
        return n
    if text[i : i + 2] == '$"':
        return _skip_dquote_span(text, i + 1)
    if text[i] == '"':
        return _skip_dquote_span(text, i)
    i += 1
    while i < n:
        if text[i] == "'":
            return i + 1
        i += 1
    return n


def strip_line_continuations(text: str) -> str:
    if "\\\n" not in text:
        return text
    out: list[str] = []
    i, n = 0, len(text)
    stack: list[str] = []
    in_comment = False
    while i < n:
        ch = text[i]
        if in_comment:
            out.append(ch)
            if ch == "\n":
                in_comment = False
            i += 1
            continue
        top = stack[-1] if stack else ""
        if top in ("'", "$'"):
            if top == "$'" and ch == "\\" and i + 1 < n:
                out.append(text[i : i + 2])
                i += 2
                continue
            out.append(ch)
            if ch == "'":
                stack.pop()
            i += 1
            continue
        if ch == "\\":
            if i + 1 < n and text[i + 1] == "\n":
                i += 2
                continue
            if i + 1 < n:
                out.append(text[i : i + 2])
                i += 2
                continue
        if top in ('"', '"${'):
            if text[i : i + 2] == "${":
                stack.append('"${')
                out.append("${")
                i += 2
                continue
            if top == '"' and ch == '"':
                stack.pop()
            elif top == '"${' and ch in ('"', "}"):
                stack.append('"') if ch == '"' else stack.pop()
            out.append(ch)
            i += 1
            continue
        if not top and ch == "#" and (i == 0 or text[i - 1] in " \t\n;|&("):
            in_comment = True
            out.append(ch)
            i += 1
            continue
        if text[i : i + 2] in ("$'", '$"', "${"):
            tok = text[i : i + 2]
            stack.append('"' if tok == '$"' else tok)
            out.append(tok)
            i += 2
            continue
        if ch in ("'", '"'):
            stack.append(ch)
        elif top == "${" and ch == "}":
            stack.pop()
        out.append(ch)
        i += 1
    return "".join(out)


def precompute_paren_ends(
    text: str, *, prefix: str, skip_dquotes: bool
) -> dict[int, tuple[int, bool]]:
    if prefix not in text:
        return {}
    plen, n, i = len(prefix), len(text), 0
    ends: dict[int, tuple[int, bool]] = {}
    stack: list[list[int]] = []
    in_dquote = False
    while i < n:
        ch, two = text[i], text[i : i + 2]
        if not stack:
            if not skip_dquotes and (ch == '"' or (not in_dquote and two == '$"')):
                in_dquote = True if two == '$"' else not in_dquote
                i += 2 if two == '$"' else 1
            elif not in_dquote and (
                two in ("$'", '$"') or ch == "'" or (skip_dquotes and ch == '"')
            ):
                i = skip_quoted_span(text, i)
            elif ch == "\\" and i + 1 < n:
                i += 2
            elif text[i : i + plen] == prefix:
                stack.append([i, 0, 0])
                i += plen
            else:
                i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
        elif in_dquote and ch == '"':
            stack.clear()
            in_dquote = False
            i += 1
        elif not in_dquote and (two in ("$'", '$"') or ch in ("'", '"')):
            end = skip_quoted_span(text, i)
            if "$(" in text[i:end] or "`" in text[i:end]:
                stack[-1][2] = 1
            i = end
        elif ch == "`" or (two == "$(" and text[i : i + 3] != "$((") or two in ("<(", ">("):
            stack[-1][2] = 1
            i += 2 if two in ("<(", ">(") else 1
        elif text[i : i + plen] == prefix:
            stack.append([i, 0, 0])
            i += plen
        elif ch == "(":
            stack[-1][1] += 1
            i += 1
        elif ch == ")":
            if stack[-1][1] > 0:
                stack[-1][1] -= 1
                i += 1
            elif i + 1 < n and text[i + 1] == ")":
                open_pos, _, sub = stack.pop()
                if stack and sub:
                    stack[-1][2] = 1
                ends[open_pos] = (i + 2, bool(sub))
                i += 2
            else:
                stack.pop()
                if stack:
                    stack[-1][2] = 1
                i += 1
        else:
            i += 1
    return ends


def precompute_arith_ends(text: str) -> dict[int, tuple[int, bool]]:
    return precompute_paren_ends(text, prefix="$((", skip_dquotes=False)


def _decode_ansic_escape(text: str, i: int) -> tuple[str, int]:
    n = len(text)
    if i + 1 >= n:
        return "\\", n
    nxt = text[i + 1]
    if nxt in _ANSIC_SIMPLE:
        return _ANSIC_SIMPLE[nxt], i + 2
    if nxt == "x":
        j = i + 2
        while j < n and j < i + 4 and text[j] in _HEX_DIGITS:
            j += 1
        return (chr(int(text[i + 2 : j], 16)), j) if j > i + 2 else ("\\x", i + 2)
    if nxt in ("u", "U"):
        lim = 4 if nxt == "u" else 8
        j = i + 2
        while j < n and j < i + 2 + lim and text[j] in _HEX_DIGITS:
            j += 1
        if j > i + 2:
            cp = int(text[i + 2 : j], 16)
            return (chr(cp) if 0 <= cp <= 0x10FFFF else ""), j
        return f"\\{nxt}", i + 2
    if nxt in _OCT_DIGITS:
        j = i + 1
        while j < n and j < i + 4 and text[j] in _OCT_DIGITS:
            j += 1
        return chr(int(text[i + 1 : j], 8) & 0xFF), j
    if nxt == "c" and i + 2 < n:
        c = text[i + 2]
        if c == "\\" and i + 3 < n and text[i + 3] == "\\":
            return "\x1c", i + 4
        cp = ord(c)
        return chr(((ord(c.upper()) ^ 0x40) & 0x7F) if cp < 128 else (cp & 0x1F)), i + 3
    return f"\\{nxt}", i + 2


def read_shell_word(
    text: str,
    i: int,
    *,
    break_chars: frozenset[str],
    arith_ends: dict[int, tuple[int, bool]] | None = None,
) -> tuple[str, int, bool]:
    n = len(text)
    chars: list[str] = []
    saw_token = False
    while i < n and text[i] not in break_chars:
        saw_token = True
        if text[i : i + 3] == "$((":
            pair = arith_ends.get(i) if arith_ends is not None else None
            if pair is not None:
                chars.append(text[i : pair[0]])
                i = pair[0]
                continue
        if text[i : i + 2] == "$'":
            i += 2
            seg: list[str] = []
            while i < n:
                if text[i] == "\\":
                    decoded, i = _decode_ansic_escape(text, i)
                    seg.append(decoded)
                    continue
                if text[i] == "'":
                    i += 1
                    break
                seg.append(text[i])
                i += 1
            chars.append("".join(seg).split("\x00", 1)[0])
            continue
        if text[i : i + 2] == '$"':
            i += 1
        ch = text[i]
        if ch == "'":
            i += 1
            while i < n and text[i] != "'":
                chars.append(text[i])
                i += 1
            i += int(i < n)
            continue
        if ch == '"':
            end = _skip_dquote_span(text, i)
            j = i + 1
            lim = end - 1 if end > i + 1 and text[end - 1] == '"' else end
            while j < lim:
                if text[j] == "\\" and j + 1 < lim:
                    nxt = text[j + 1]
                    chars.append(nxt if nxt in ('"', "\\", "$", "`") else f"\\{nxt}")
                    j += 2
                    continue
                chars.append(text[j])
                j += 1
            i = end
            continue
        if ch == "\\" and i + 1 < n:
            chars.append(text[i + 1])
            i += 2
            continue
        chars.append(ch)
        i += 1
    return "".join(chars), i, saw_token


def split_shell_tokens(text: str) -> list[str]:
    text = strip_line_continuations(text)
    arith_ends = precompute_arith_ends(text)
    tokens: list[str] = []
    i = 0
    n = len(text)
    ws = frozenset(" \t\n")
    while i < n:
        while i < n and text[i] in ws:
            i += 1
        if i >= n:
            break
        word, next_i, saw = read_shell_word(text, i, break_chars=ws, arith_ends=arith_ends)
        if saw:
            tokens.append(word)
        i = next_i if next_i > i else i + 1
    return tokens


def strip_quotes_linear(text: str) -> str:
    text = strip_line_continuations(text)
    arith_ends = precompute_arith_ends(text)
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if text[i : i + 2] in ("$'", '$"') or ch in ("'", '"'):
            out.append("_")
            i = skip_quoted_span(text, i)
            continue
        if ch == "\\" and i + 1 < n:
            out.append("_")
            i += 2
            continue
        if text[i : i + 3] == "$((":
            pair = arith_ends.get(i)
            if pair is not None:
                out.append("_")
                i = pair[0]
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def has_unquoted_command_sub(text: str) -> bool:
    text = strip_line_continuations(text)
    arith_ends = precompute_arith_ends(text)
    i, n = 0, len(text)
    stack: list[str] = []
    while i < n:
        ch, two, top = text[i], text[i : i + 2], (stack[-1] if stack else "")
        if top in ('"', '"${'):
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if two == "${":
                stack.append('"${')
                i += 2
                continue
            if top == '"' and ch == '"':
                stack.pop()
                i += 1
                continue
            if top == '"${' and ch in ('"', "}"):
                stack.append('"') if ch == '"' else stack.pop()
                i += 1
                continue
        else:
            if two == "$'" or ch == "'":
                i = skip_quoted_span(text, i)
                continue
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if two == '$"':
                stack.append('"')
                i += 2
                continue
            if ch == '"':
                stack.append('"')
                i += 1
                continue
            if two == "${":
                stack.append("${")
                i += 2
                continue
            if two in ("<(", ">("):
                return True
            if top == "${" and ch == "}":
                stack.pop()
                i += 1
                continue
        if ch == "`":
            return True
        if two == "$(":
            if text[i : i + 3] == "$((":
                pair = arith_ends.get(i)
                if pair is None or pair[1]:
                    return True
                i = pair[0]
                continue
            return True
        i += 1
    return "${" in stack or '"${' in stack
