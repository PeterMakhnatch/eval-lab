from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ._multiagent_io import sanitize_json_value

_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_KEY_UESC_RE = re.compile(r"\\+u([0-9a-fA-F]{4})|\\+U([0-9a-fA-F]{8})|\\+x([0-9a-fA-F]{2})")
_KEY_WS_ESC_RE = re.compile(r"\\+[nrt]")
_SENSITIVE_WORD = (
    r"(?:rewards?|verifier(?:[_\-\s]results?)?|ground[_\-\s]truth|scores?|pass[_\-\s]rates?"
    r"|(?:(?:seat|auth|access|refresh|session|bearer|api|world|admin)[_\-\s]?)?token"
    r"|(?:seat|auth|access|refresh|session|bearer|api|world|admin)[_\-\s]?tokens"
    r"|api[_\-\s]?keys?|secrets?"
    r"|hmac(?:[_\-\s]?(?:keys?|secrets?|sig(?:nature)?s?|sha\d+))?"
    r"|authorization|passwords?|private[_\-\s]?keys?)"
)
_SENSITIVE_KEY_RE = re.compile(
    rf"(?:^|[^A-Za-z0-9]){_SENSITIVE_WORD}(?:[^A-Za-z0-9]|$)", re.IGNORECASE
)
_INLINE_SECRET_RE = re.compile(
    r"(?:"
    r"\bsk-(?:proj-|ant-|or-v1-|live-|test-)?[A-Za-z0-9_\-]{12,}"
    r"|\bBearer\s+[A-Za-z0-9_\-\.~+/=]{8,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]{0,8192}?-----END [A-Z ]+ PRIVATE KEY-----"
    r")",
    re.IGNORECASE,
)
_KEY_ASSIGN_RE = re.compile(
    r"""(?:"""
    r"""(?:(?<!\\)\\*+")((?:\\[^\r\n"]|[^\r\n"\\]){1,1024}?)(?:\\*+")"""
    r"""|(?:(?<!\\)\\*+')((?:\\[^\r\n']|[^\r\n'\\]){1,1024}?)(?:\\*+')"""
    r"""|(?:(?<!\\)\\*+`)((?:\\[^\r\n`]|[^\r\n`\\]){1,1024}?)(?:\\*+`)"""
    r"""|(?<![A-Za-z0-9_\-.])([A-Za-z0-9_\-.]{0,40}?(?i:verifier[ \t]+results?|ground[ \t]+truth|pass[ \t]+rates?|seat[ \t]+tokens?|api[ \t]+keys?|hmac[ \t]+(?:keys?|secrets?|sig(?:nature)?s?|sha\d+)|access[ \t]+tokens?|private[ \t]+keys?)(?:[A-Za-z0-9_\-.]++|\[[^\]\n]{1,64}\]){0,16}+)"""
    r"""|(?<![A-Za-z0-9_\-.])([A-Za-z0-9_\-.]++(?:\[[^\]\n]{1,64}\][A-Za-z0-9_\-.]*+){0,16}+)"""
    r""")[ \t]*+(?P<op>===|[:=]=|=>|[:=])[ \t]*+"""
)
_SEAT_CSV_NEXT_RE = re.compile(r",[ \t]*[A-Za-z0-9_-]{1,32}:[^\s,;}\])\"']")
_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9._:-]+")
_SAFE_ROLE_RE = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_KEY_LEN = 160
_MAX_ROLE_LEN = 32
_MAX_KEY_COLLISIONS = 1_000_000
_MAX_JSON_SCRUB_NODES = 100_000
_MAX_JSONL_SCRUB_LINES = 100_000
MAX_LINE_BYTES = 4 * 1024 * 1024


def _decode_key_escapes(key: str) -> str:
    if "\\" not in key:
        return key

    def _sub_hex(match: re.Match[str]) -> str:
        raw = match.group(1) or match.group(2) or match.group(3)
        assert raw is not None
        cp = int(raw, 16)
        if cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
            return " "
        return chr(cp)

    return _KEY_WS_ESC_RE.sub(" ", _KEY_UESC_RE.sub(_sub_hex, key))


def _is_sensitive_key(key: str) -> bool:
    decoded = _decode_key_escapes(key)
    return _SENSITIVE_KEY_RE.search(_CAMEL_SPLIT_RE.sub("_", decoded)) is not None


def sanitize_key(raw: str) -> str:
    cleaned = _SAFE_KEY_RE.sub("_", raw).lstrip("_.:-").rstrip("_")
    if not cleaned:
        return "decision"
    if not cleaned[0].isalnum():
        cleaned = f"d_{cleaned}"
    if len(cleaned) <= _MAX_KEY_LEN:
        return cleaned
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:12]
    tail = cleaned[-72:]
    sep = "" if tail.startswith(":") else "-"
    head_len = _MAX_KEY_LEN - 10 - len(digest) - 1 - len(sep) - len(tail)
    head = cleaned[: max(1, head_len)].rstrip("_.:-") or "d"
    return f"{head}-{digest}{sep}{tail}"


def _candidate_key(base_key: str, suffix: int) -> str:
    tag = f"-{suffix}"
    if len(base_key) + len(tag) <= _MAX_KEY_LEN:
        return f"{base_key}{tag}"
    prefix = base_key[: _MAX_KEY_LEN - len(tag)].rstrip("_")
    return f"{prefix or 'decision'}{tag}"


def unique_key(base_key: str, seen_keys: set[str]) -> str:
    if base_key not in seen_keys:
        seen_keys.add(base_key)
        return base_key
    lo = 1
    hi = 2
    for _ in range(21):
        if _candidate_key(base_key, hi) not in seen_keys:
            break
        lo = hi
        assert lo < _MAX_KEY_COLLISIONS
        hi = min(hi * 2, _MAX_KEY_COLLISIONS)
    for _ in range(21):
        if hi - lo <= 1:
            break
        mid = (lo + hi) // 2
        if _candidate_key(base_key, mid) in seen_keys:
            lo = mid
        else:
            hi = mid
    key = _candidate_key(base_key, hi)
    assert key not in seen_keys
    seen_keys.add(key)
    return key


def sanitize_role(raw: str) -> str:
    cleaned = _SAFE_ROLE_RE.sub("_", raw).strip("_")
    if not cleaned:
        return "seat"
    if not cleaned[0].isalpha():
        cleaned = f"s_{cleaned}"
    return cleaned[:_MAX_ROLE_LEN]


def _step_escaped_indent(text: str, k: int) -> int:
    n = len(text)
    while k < n:
        if text[k] in (" ", "\t"):
            k += 1
            continue
        if text[k] == "\\":
            m = k
            while m < n and text[m] == "\\":
                m += 1
            if m < n and text[m] == "t":
                k = m + 1
                continue
        break
    return k


def _skip_value_lead_ws(text: str, start: int, *, colon_sep: bool = False) -> int:
    n = len(text)
    while start < n:
        if text[start] in (" ", "\t"):
            start += 1
            continue
        if colon_sep and text[start] in ("\r", "\n"):
            k = start
            while k < n and text[k] in ("\r", "\n"):
                k += 1
            indented = _step_escaped_indent(text, k)
            if indented > k and indented < n and _KEY_ASSIGN_RE.match(text, indented) is None:
                start = indented
                continue
            break
        if text[start] == "\\":
            j = start
            while j < n and text[j] == "\\":
                j += 1
            if j < n and text[j] == "t":
                start = j + 1
                continue
            if colon_sep and j < n and text[j] in ("n", "r"):
                k = j + 1
                while k < n and text[k] == "\\":
                    m = k
                    while m < n and text[m] == "\\":
                        m += 1
                    if m < n and text[m] in ("n", "r"):
                        k = m + 1
                        continue
                    break
                indented = _step_escaped_indent(text, k)
                if indented > k and indented < n and _KEY_ASSIGN_RE.match(text, indented) is None:
                    start = indented
                    continue
        break
    return start


def _scan_escaped_quote_value(text: str, j: int, bs0: int) -> int:
    n = len(text)
    esc_q = text[j]
    step = (bs0 + 1) & -(bs0 + 1)
    eff_bs0 = step - 1
    mod = 2 * step
    i = j + 1
    bs = 0
    while i < n:
        c = text[i]
        if c == "\\":
            bs += 1
        elif c == esc_q:
            rem = bs % step
            if rem != eff_bs0:
                return i - rem if rem % 2 == 1 else i
            if bs % mod == eff_bs0:
                q_start = i - bs
                return q_start if _KEY_ASSIGN_RE.match(text, q_start) is not None else i + 1
            bs = 0
        else:
            bs = 0
        i += 1
    return n


def _scan_bracket_value(text: str, start: int) -> int:
    n = len(text)
    close_for = {"{": "}", "[": "]", "(": ")"}
    stack: list[str] = []
    counts = {"}": 0, "]": 0, ")": 0}
    quote: str | None = None
    escaped_quote: str | None = None
    esc_bs0 = 1
    esc_bs = 0
    i = start
    while i < n:
        c = text[i]
        if escaped_quote is not None:
            if c == "\\":
                esc_bs += 1
            elif c == escaped_quote:
                step = esc_bs0 + 1
                rem = esc_bs % step
                if rem != esc_bs0:
                    return i - rem if rem % 2 == 1 else i
                if esc_bs % (2 * step) == esc_bs0:
                    escaped_quote = None
                esc_bs = 0
            else:
                esc_bs = 0
        elif quote is not None:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c == "\\":
            j = i
            while j < n and text[j] == "\\":
                j += 1
            bs0 = j - i
            if bs0 % 2 == 1 and j < n and text[j] in ('"', "'"):
                escaped_quote = text[j]
                esc_bs0 = ((bs0 + 1) & -(bs0 + 1)) - 1
                esc_bs = 0
                i = j + 1
                continue
            i = j
            continue
        elif c in ('"', "'", "`"):
            quote = c
        elif c in close_for:
            closer = close_for[c]
            stack.append(closer)
            counts[closer] += 1
        elif c in counts and counts[c] > 0:
            while stack:
                popped = stack.pop()
                counts[popped] -= 1
                if popped == c:
                    break
            if not stack:
                return i + 1
        i += 1
    return n


def _scan_single_quote_seg(text: str, start: int) -> tuple[int, bool]:
    n = len(text)
    ch0 = text[start]
    i = start + 1
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == ch0:
            if _KEY_ASSIGN_RE.match(text, i) is not None:
                return i, True
            return i + 1, False
        i += 1
    return n, True


def _scan_shell_concat_tail(text: str, i: int) -> int:
    n = len(text)
    while i < n and not text[i].isspace() and text[i] not in ",;}])":
        if text[i] in ('"', "'", "`"):
            if _KEY_ASSIGN_RE.match(text, i) is not None:
                return i
            i, stopped = _scan_single_quote_seg(text, i)
            if stopped:
                return i
            continue
        if text[i] == "\\":
            if i + 1 >= n or text[i + 1] in ("n", "r", "t"):
                break
            i += 2
            continue
        i += 1
    return i


def _scan_value_end(
    text: str, start: int, *, colon_sep: bool = False, plural_csv: bool = False
) -> int:
    n = len(text)
    lead = _skip_value_lead_ws(text, start, colon_sep=colon_sep)
    if lead >= n:
        return start
    cur = lead
    if text[cur] == "\\":
        j = cur
        while j < n and text[j] == "\\":
            j += 1
        bs0 = j - cur
        if bs0 % 2 == 1 and j < n and text[j] in ('"', "'"):
            return _scan_escaped_quote_value(text, j, bs0)
        if bs0 % 2 == 0 and j < n and text[j] in ('"', "'", "`"):
            cur = j
    ch0 = text[cur]
    if ch0 in ('"', "'", "`"):
        if _KEY_ASSIGN_RE.match(text, cur) is not None:
            return start
        end_q, stopped = _scan_single_quote_seg(text, cur)
        if stopped or colon_sep:
            return end_q
        return _scan_shell_concat_tail(text, end_q)
    if ch0 in ("{", "[", "("):
        return _scan_bracket_value(text, cur)
    i = cur
    stop_chars = "\r\n,;}])\"'" if colon_sep else ";})]"
    while i < n and (colon_sep or not text[i].isspace()):
        if text[i] == ",":
            if colon_sep:
                if plural_csv and _SEAT_CSV_NEXT_RE.match(text, i) is not None:
                    i += 1
                    continue
                break
            if i + 1 < n and text[i + 1].isspace():
                break
            i += 1
            continue
        if text[i] in stop_chars:
            break
        if not colon_sep and text[i] in ('"', "'"):
            end_q, stopped = _scan_single_quote_seg(text, i)
            i = end_q
            if stopped:
                break
            continue
        if text[i] == "\\":
            k = i + 1
            while k < n and text[k] == "\\":
                k += 1
            if k >= n or text[k] in ("n", "r", "t") or (colon_sep and text[k] in ('"', "'")):
                break
            i = k + 1 if not colon_sep and text[k] in ('"', "'") else k
            continue
        i += 1
    return i if i > cur else start


def scrub_regex_passes(text: str) -> str:
    if not text:
        return text
    out: list[str] = []
    pos = 0
    n = len(text)
    redactions = 0
    for _ in range(n + 1):
        if pos >= n:
            break
        match = _KEY_ASSIGN_RE.search(text, pos)
        if match is None:
            out.append(text[pos:])
            break
        key_tok = next((g for g in match.groups()[:-1] if g is not None), "")
        if not _is_sensitive_key(key_tok):
            out.append(text[pos : match.end()])
            pos = match.end()
            continue
        redactions += 1
        if redactions > 4096:
            out.append(text[pos : match.start()])
            out.append("[redacted]")
            break
        out.append(text[pos : match.start()])
        out.append("[redacted]")
        low_key = _decode_key_escapes(key_tok).lower()
        is_plural = low_key.endswith(("tokens", "keys", "secrets"))
        next_pos = _scan_value_end(
            text, match.end(), colon_sep=match.group("op") == ":", plural_csv=is_plural
        )
        pos = next_pos if next_pos > match.start() else match.end()
    return _INLINE_SECRET_RE.sub("[redacted]", "".join(out))


def scrub_json_obj(root: Any) -> Any:
    if isinstance(root, dict):
        result: Any = {}
    elif isinstance(root, list):
        result = []
    elif isinstance(root, str):
        return scrub_regex_passes(root)
    else:
        return root
    stack: list[tuple[Any, Any]] = [(root, result)]
    nodes = 0
    while stack:
        src, dst = stack.pop()
        if isinstance(src, dict):
            for k, v in src.items():
                nodes += 1
                if nodes > _MAX_JSON_SCRUB_NODES:
                    return "[redacted]"
                ks = str(k)
                if _is_sensitive_key(ks):
                    dst[ks] = "[redacted]"
                elif isinstance(v, dict):
                    child_d: dict[str, Any] = {}
                    dst[ks] = child_d
                    stack.append((v, child_d))
                elif isinstance(v, list):
                    child_l: list[Any] = []
                    dst[ks] = child_l
                    stack.append((v, child_l))
                elif isinstance(v, str):
                    dst[ks] = scrub_regex_passes(v)
                else:
                    dst[ks] = v
        elif isinstance(src, list):
            for item in src:
                nodes += 1
                if nodes > _MAX_JSON_SCRUB_NODES:
                    return "[redacted]"
                if isinstance(item, dict):
                    child_d = {}
                    dst.append(child_d)
                    stack.append((item, child_d))
                elif isinstance(item, list):
                    child_l = []
                    dst.append(child_l)
                    stack.append((item, child_l))
                elif isinstance(item, str):
                    dst.append(scrub_regex_passes(item))
                else:
                    dst.append(item)
    return result


def _scrub_json_doc(raw: str) -> str | None:
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(parsed, (dict, list)):
        return None
    cleaned = sanitize_json_value(scrub_json_obj(parsed))
    try:
        serialized = json.dumps(
            cleaned,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (ValueError, RecursionError):
        return "[redacted]"
    return scrub_regex_passes(serialized)


def scrub_text(text: str, *, max_chars: int = 1200) -> str:
    stripped = text.strip()
    if stripped.startswith(("{", "[")) and (cleaned := _scrub_json_doc(stripped)) is not None:
        return cleaned[:max_chars]
    if "\n" in text and ("{" in text or "[" in text):
        rebuilt: list[str] = []
        any_json = False
        acc_len = 0
        for idx, ln in enumerate(text.split("\n")):
            if idx >= _MAX_JSONL_SCRUB_LINES:
                rebuilt.append("[redacted]")
                break
            ls = ln.strip()
            if ls.startswith(("{", "[")) and (c := _scrub_json_doc(ls)) is not None:
                piece = c
                any_json = True
            else:
                piece = scrub_regex_passes(ln)
            rebuilt.append(piece)
            acc_len += len(piece) + 1
            if acc_len > max_chars and max_chars >= 0:
                break
        if any_json:
            return "\n".join(rebuilt)[:max_chars]
    return scrub_regex_passes(text)[:max_chars]


def canonical_sha256(obj: dict[str, Any]) -> str:
    payload = json.dumps(
        sanitize_json_value(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def encode_bounded_record(rec: dict[str, Any]) -> bytes:
    clean = sanitize_json_value(rec)
    for trim in (None, 100, 32, 8, 0):
        if isinstance(clean.get("view"), dict):
            view = clean["view"]
            if trim is not None:
                for field_name in (
                    "received_messages",
                    "prior_self_reads",
                    "prior_self_actions",
                    "prior_atif_observations",
                ):
                    if isinstance(view.get(field_name), list):
                        view[field_name] = view[field_name][-trim:] if trim > 0 else []
            clean["view_sha256"] = canonical_sha256(view)
        serialized = (
            json.dumps(
                clean,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029")
        )
        line = (serialized + "\n").encode("utf-8")
        if len(line) - 1 <= MAX_LINE_BYTES:
            if isinstance(clean.get("view"), dict):
                rec["view"] = clean["view"]
                rec["view_sha256"] = clean["view_sha256"]
            elif "view_sha256" in clean:
                rec["view_sha256"] = clean["view_sha256"]
            return line
    raise ValueError("seatlab decision record exceeds 4 MiB line limit")
