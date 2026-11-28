from __future__ import annotations

import hashlib
import json
import math
import secrets
import sys
import urllib.parse
from collections.abc import Mapping
from typing import Any

sys.dont_write_bytecode = True

MAX_BODY_BYTES = 16384
MAX_JSON_DEPTH = 6
MAX_WORLD_LOG_BYTES = 8 * 1024 * 1024
ADMIN_CLOSE_RESERVE = 1024
GENESIS_HASH = "0" * 64

MAX_JSON_NODES = 16384
MAX_STR_CHARS = 65536
MAX_INT_ABS = 10**60
MAX_CHAIN_EVENTS = 100_000
HEX_CHARS = frozenset("0123456789abcdef")
UNHASHED_KEYS = frozenset({"entry_hash", "hmac"})
HTTP_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})
SPOOF_HEADERS = ("x-seat", "x-sender", "x-from", "x-actor")


def is_sha256_hex(val: Any) -> bool:
    return isinstance(val, str) and len(val) == 64 and all(ch in HEX_CHARS for ch in val)


def _is_clean_chars(text: str, max_len: int) -> bool:
    if len(text) > max_len:
        return False
    for ch in text:
        code = ord(ch)
        if (
            (code < 0x20 and code not in (0x09, 0x0A))
            or (0x7F <= code <= 0x9F)
            or (0xD800 <= code <= 0xDFFF)
            or code in (0x2028, 0x2029)
        ):
            return False
    return True


def clean_str(val: Any, max_len: int = 256, *, allow_empty: bool = False) -> str | None:
    if isinstance(max_len, bool) or not isinstance(max_len, int) or max_len < 0:
        raise ValueError("max_len must be a non-negative int")
    if not isinstance(val, str) or not _is_clean_chars(val, max_len):
        return None
    cleaned = val.strip()
    if not cleaned and not allow_empty:
        return None
    return cleaned if len(cleaned) <= max_len else None


def strict_int(val: Any) -> int | None:
    if isinstance(val, bool) or not isinstance(val, int) or abs(val) > MAX_INT_ABS:
        return None
    return val


def sanitize_method_and_path(method: Any, raw_path: Any) -> tuple[str, str, dict[str, str]]:
    clean_m = method if isinstance(method, str) and method in HTTP_METHODS else "INVALID"
    path_part, _, query_part = (raw_path if isinstance(raw_path, str) else "").partition("?")
    c_kind = clean_str(path_part.strip("/").replace("/", ".") or "root", 128)
    kind = c_kind if (c_kind is not None and all(0x20 < ord(c) < 0x7F for c in c_kind)) else "invalid_path"
    q_out: dict[str, str] = {}
    if query_part and len(query_part) <= 2048:
        for k, vals in list(urllib.parse.parse_qs(query_part, keep_blank_values=False).items())[:32]:
            ck, cv = clean_str(k, 64), (clean_str(vals[-1], 256) if vals else None)
            if ck is not None and cv is not None:
                q_out[ck] = cv
    return clean_m, kind, q_out


def extract_http_headers(hdrs: Any) -> tuple[str | None, str | None, dict[str, list[str]], str | None]:
    if hdrs.get_all("Transfer-Encoding"):
        return None, None, {}, "unsupported_transfer_encoding"
    auths = hdrs.get_all("Authorization") or []
    clens = hdrs.get_all("Content-Length") or []
    if len(auths) > 1 or len(clens) > 1:
        return None, None, {}, "duplicate_header"
    auth = str(auths[0]) if auths else ""
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else None
    raw_clen = str(clens[0]) if clens else None
    multi: dict[str, list[str]] = {}
    for raw_k, raw_v in hdrs.items():
        multi.setdefault(str(raw_k).lower(), []).append(str(raw_v))
    return token, raw_clen, multi, None


def has_spoof_signal(seat: str, body: dict[str, Any] | None, headers: Mapping[str, Any] | None) -> bool:
    if headers:
        low_map = {str(k).lower(): v for k, v in headers.items()}
        for h in SPOOF_HEADERS:
            if (raw_v := low_map.get(h)) is not None:
                vals = raw_v if isinstance(raw_v, (list, tuple)) else (raw_v,)
                if any(str(v).strip() != seat for v in vals):
                    return True
    return isinstance(body, dict) and any(k in body and body[k] != seat for k in ("seat", "from", "sender"))


def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len(pairs) > MAX_JSON_NODES:
        raise ValueError("JSON object exceeds node limit")
    out: dict[str, Any] = {}
    for key, val in pairs:
        if not isinstance(key, str) or not _is_clean_chars(key, MAX_BODY_BYTES):
            raise ValueError("invalid JSON key")
        if key in out:
            raise ValueError(f"duplicate JSON key: {key!r}")
        out[key] = val
    return out


def reject_nonfinite(token: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {token}")


def within_json_depth(obj: Any, max_depth: int = MAX_JSON_DEPTH) -> bool:
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
        return False
    stack: list[tuple[Any, int]] = [(obj, 0)]
    visited = 0
    while stack:
        visited += 1
        if visited > MAX_JSON_NODES:
            return False
        cur, depth = stack.pop()
        if depth > max_depth:
            return False
        if isinstance(cur, Mapping):
            if depth + 1 > max_depth or len(cur) + visited > MAX_JSON_NODES + 1:
                return False
            for val in cur.values():
                stack.append((val, depth + 1))
        elif isinstance(cur, (list, tuple)):
            if depth + 1 > max_depth or len(cur) + visited > MAX_JSON_NODES + 1:
                return False
            for item in cur:
                stack.append((item, depth + 1))
    return True


def _has_str_keys(root: Mapping[Any, Any]) -> bool:
    stack: list[Any] = [root]
    visited = 0
    while stack:
        visited += 1
        if visited > MAX_JSON_NODES:
            return False
        cur = stack.pop()
        if isinstance(cur, Mapping):
            if len(cur) + visited > MAX_JSON_NODES + 1:
                return False
            for k, v in cur.items():
                if not isinstance(k, str):
                    return False
                if isinstance(v, (Mapping, list, tuple)):
                    stack.append(v)
        elif isinstance(cur, (list, tuple)):
            if len(cur) + visited > MAX_JSON_NODES + 1:
                return False
            for item in cur:
                if isinstance(item, (Mapping, list, tuple)):
                    stack.append(item)
    return True


def _raw_exceeds_depth(raw: bytes, limit: int) -> bool:
    depth = 0
    in_str = esc = False
    for byte in raw:
        if in_str:
            if esc:
                esc = False
            elif byte == 0x5C:
                esc = True
            elif byte == 0x22:
                in_str = False
        elif byte == 0x22:
            in_str = True
        elif byte in (0x7B, 0x5B):
            depth += 1
            if depth > limit:
                return True
        elif byte in (0x7D, 0x5D) and depth > 0:
            depth -= 1
    return False


def _validate_parsed_values(root: Any, max_depth: int, max_str: int) -> bool:
    stack: list[tuple[Any, int]] = [(root, 0)]
    visited = 0
    while stack:
        visited += 1
        if visited > MAX_JSON_NODES:
            return False
        cur, depth = stack.pop()
        if depth > max_depth:
            return False
        if cur is None or isinstance(cur, bool):
            continue
        if type(cur) is int:
            if abs(cur) > MAX_INT_ABS:
                return False
        elif isinstance(cur, float):
            if not math.isfinite(cur):
                return False
        elif isinstance(cur, str):
            if not _is_clean_chars(cur, max_str):
                return False
        elif isinstance(cur, dict):
            if depth + 1 > max_depth or len(cur) + visited > MAX_JSON_NODES + 1:
                return False
            for k, v in cur.items():
                if not isinstance(k, str) or not _is_clean_chars(k, max_str):
                    return False
                stack.append((v, depth + 1))
        elif isinstance(cur, list):
            if depth + 1 > max_depth or len(cur) + visited > MAX_JSON_NODES + 1:
                return False
            for item in cur:
                stack.append((item, depth + 1))
        else:
            return False
    return True


def parse_json_object(raw: bytes) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(raw, (bytes, bytearray)):
        return None, "invalid_body_type"
    data = bytes(raw)
    if len(data) > MAX_BODY_BYTES:
        return None, "body_too_large"
    if not data or not data.strip():
        return None, "empty_body"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "invalid_utf8"
    if _raw_exceeds_depth(data, MAX_JSON_DEPTH):
        return None, "json_depth_exceeded"
    try:
        parsed = json.loads(text, object_pairs_hook=unique_pairs, parse_constant=reject_nonfinite)
    except RecursionError:
        return None, "json_depth_exceeded"
    except ValueError as exc:
        msg = str(exc)
        if "duplicate JSON key" in msg:
            return None, "duplicate_json_key"
        if "non-finite" in msg:
            return None, "non_finite_number"
        return None, "invalid_json"
    if not isinstance(parsed, dict):
        return None, "expected_json_object"
    if not within_json_depth(parsed, MAX_JSON_DEPTH):
        return None, "json_depth_exceeded"
    if not _validate_parsed_values(parsed, MAX_JSON_DEPTH, MAX_BODY_BYTES):
        return None, "unsafe_json_value"
    return parsed, None


def coerce_json_object_bytes(body: Any) -> tuple[bytes | None, str | None]:
    if body is None:
        return None, "empty_body"
    if isinstance(body, (bytes, bytearray)):
        return bytes(body), None
    if not isinstance(body, dict):
        return None, "expected_json_object"
    if not within_json_depth(body, MAX_JSON_DEPTH):
        return None, "json_depth_exceeded"
    if not _has_str_keys(body):
        return None, "invalid_json"
    try:
        return json.dumps(body, allow_nan=False).encode("utf-8"), None
    except RecursionError:
        return None, "json_depth_exceeded"
    except (TypeError, ValueError) as exc:
        return None, ("non_finite_number" if "Out of range float" in str(exc) else "invalid_json")


def read_framed_json_body(
    rfile: Any, raw_len: str | None, *, allow_empty_object: bool = False
) -> tuple[dict[str, Any] | None, str | None]:
    len_str = raw_len.strip() if isinstance(raw_len, str) else ""
    if not len_str:
        return ({}, None) if allow_empty_object else (None, "empty_body")
    if not (len_str.isascii() and len_str.isdigit() and len(len_str) <= 9):
        return None, "invalid_content_length"
    length = int(len_str)
    if length == 0:
        return ({}, None) if allow_empty_object else (None, "empty_body")
    if length > MAX_BODY_BYTES:
        _ = rfile.read(min(length, MAX_BODY_BYTES + 1))
        return None, "body_too_large"
    raw = rfile.read(length)
    return parse_json_object(raw) if len(raw) == length else (None, "truncated_body")


def _check_scalar_safe(cur: Any) -> Any:
    if cur is None or isinstance(cur, bool):
        return cur
    if type(cur) is int:
        if abs(cur) > MAX_INT_ABS:
            raise ValueError("integer out of range")
        return cur
    if isinstance(cur, float):
        if not math.isfinite(cur):
            raise ValueError("non-finite float")
        return cur
    if isinstance(cur, str):
        if not _is_clean_chars(cur, MAX_STR_CHARS):
            raise ValueError("unsafe string characters")
        return cur
    raise TypeError(f"unsupported JSON type: {type(cur).__name__}")


def json_safe(obj: Any, max_depth: int = 16, max_nodes: int = MAX_JSON_NODES) -> Any:
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 1:
        raise ValueError("max_depth must be a positive int")
    if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or max_nodes < 1:
        raise ValueError("max_nodes must be a positive int")
    frames: list[tuple[bool, Any, int]] = [(False, obj, 0)]
    results: list[Any] = []
    visited = 0
    while frames:
        done, cur, depth = frames.pop()
        if not done:
            visited += 1
            if visited > max_nodes or depth > max_depth:
                raise ValueError("JSON structure exceeds depth or node limit")
            if isinstance(cur, Mapping):
                if depth + 1 > max_depth or len(cur) + visited > max_nodes + 1:
                    raise ValueError("JSON mapping exceeds depth or node limit")
                items = list(cur.items())
                keys: list[str] = []
                for k, _ in items:
                    if not isinstance(k, str) or not _is_clean_chars(k, 1024):
                        raise ValueError("invalid JSON object key")
                    keys.append(k)
                frames.append((True, keys, len(items)))
                for _, v in reversed(items):
                    frames.append((False, v, depth + 1))
            elif isinstance(cur, (list, tuple)):
                if depth + 1 > max_depth or len(cur) + visited > max_nodes + 1:
                    raise ValueError("JSON list exceeds depth or node limit")
                frames.append((True, None, len(cur)))
                for item in reversed(cur):
                    frames.append((False, item, depth + 1))
            else:
                results.append(_check_scalar_safe(cur))
        elif cur is None:
            seq_out = results[-depth:] if depth > 0 else []
            if depth > 0:
                del results[-depth:]
            results.append(seq_out)
        else:
            vals = results[-depth:] if depth > 0 else []
            if depth > 0:
                del results[-depth:]
            results.append(dict(zip(cur, vals, strict=True)))
    assert len(results) == 1
    return results[0]


def canonical_json(obj: Any) -> str:
    safe = json_safe(obj)
    return json.dumps(safe, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def compute_entry_hash(prev_hash: str, body: dict[str, Any]) -> str:
    if not is_sha256_hex(prev_hash):
        raise ValueError("prev_hash must be 64 lowercase hex chars")
    if not isinstance(body, Mapping):
        raise ValueError("body must be a mapping")
    payload = {k: v for k, v in body.items() if k not in UNHASHED_KEYS}
    payload["prev_hash"] = prev_hash
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    assert len(digest) == 64
    return digest


def encode_event_record(
    next_seq: int, ts: float, logical_tick: int, seat: str | None, kind: str,
    method: str, req_obj: Any, status: int, res_obj: dict[str, Any], prev_hash: str, *, late: bool = False,
) -> tuple[dict[str, Any], str, bytes]:
    event: dict[str, Any] = {
        "seq": next_seq, "t": ts, "logical_tick": logical_tick,
        "seat": seat, "kind": kind, "method": method, "status": status, "ok": 200 <= status < 300,
        "request": req_obj, "response": res_obj, "prev_hash": prev_hash,
    }
    if late:
        event["late"] = True
    digest = compute_entry_hash(prev_hash, event)
    event["entry_hash"] = digest
    line = canonical_json(event)
    assert "\n" not in line and "\r" not in line
    return event, digest, (line + "\n").encode("utf-8")


def encode_logged_event(
    next_seq: int, ts: float, logical_tick: int, seat: str | None, kind: str,
    method: str, body: dict[str, Any] | None, status: int, response: dict[str, Any], prev_hash: str, *, late: bool = False,
) -> tuple[int, dict[str, Any], dict[str, Any], str, bytes]:
    safe_req: Any = None
    if isinstance(body, dict):
        try:
            safe_req = json_safe(body, MAX_JSON_DEPTH, 8000)
        except (TypeError, ValueError):
            safe_req = None
    try:
        safe_res = json_safe(response, 12, 8000)
        if not isinstance(safe_res, dict):
            raise ValueError("response must be a dict")
        event, digest, raw = encode_event_record(next_seq, ts, logical_tick, seat, kind, method, safe_req, status, safe_res, prev_hash, late=late)
        return status, safe_res, event, digest, raw
    except (TypeError, ValueError):
        err_res = {"error": "world error: unserializable_response"}
        event, digest, raw = encode_event_record(next_seq, ts, logical_tick, seat, kind, method, None, 500, err_res, prev_hash, late=late)
        return 500, err_res, event, digest, raw


def verify_chain(events: list[dict[str, Any]]) -> tuple[bool, str]:
    if not isinstance(events, list):
        return False, "events must be a list"
    if len(events) == 0:
        return False, "empty event log"
    if len(events) > MAX_CHAIN_EVENTS:
        return False, "event_count_exceeded"
    prev = GENESIS_HASH
    for idx, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            return False, f"event {idx}: not a dict"
        if "hmac" in event:
            return False, f"event {idx}: unexpected hmac field"
        if strict_int(event.get("seq")) != idx:
            return False, f"event {idx}: non-contiguous seq {event.get('seq')!r}"
        ph, eh = event.get("prev_hash"), event.get("entry_hash")
        if not is_sha256_hex(ph) or not is_sha256_hex(eh):
            return False, f"event {idx}: invalid prev_hash or entry_hash"
        assert isinstance(ph, str) and isinstance(eh, str)
        if not secrets.compare_digest(ph, prev):
            return False, f"event {idx}: prev_hash mismatch"
        try:
            expected = compute_entry_hash(prev, event)
        except (TypeError, ValueError) as exc:
            return False, f"event {idx}: {exc}"
        if not secrets.compare_digest(eh, expected):
            return False, f"event {idx}: entry_hash mismatch"
        prev = eh
    return True, "ok"


def parse_events_jsonl(data: bytes) -> list[dict[str, Any]]:
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("event log must be bytes")
    if not data:
        raise ValueError("empty event log")
    if len(data) > MAX_WORLD_LOG_BYTES + ADMIN_CLOSE_RESERVE:
        raise ValueError("event log exceeds MAX_WORLD_LOG_BYTES")
    if not data.endswith(b"\n"):
        raise ValueError("event log missing trailing newline")
    lines = bytes(data)[:-1].split(b"\n")
    if len(lines) > MAX_CHAIN_EVENTS:
        raise ValueError("event log exceeds MAX_CHAIN_EVENTS")
    events: list[dict[str, Any]] = []
    for idx, raw_line in enumerate(lines, start=1):
        if not raw_line or b"\r" in raw_line or _raw_exceeds_depth(raw_line, 16):
            raise ValueError(f"invalid framing or depth at line {idx}")
        text = raw_line.decode("utf-8")
        parsed = json.loads(text, object_pairs_hook=unique_pairs, parse_constant=reject_nonfinite)
        if not isinstance(parsed, dict) or canonical_json(parsed) != text:
            raise ValueError(f"non-canonical event at line {idx}")
        events.append(parsed)
    ok, reason = verify_chain(events)
    if not ok:
        raise ValueError(f"broken event chain: {reason}")
    return events
