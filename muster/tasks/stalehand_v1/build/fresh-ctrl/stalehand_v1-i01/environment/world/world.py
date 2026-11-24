import contextlib
import hashlib
import json
import math
import sys
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import ClassVar

sys.dont_write_bytecode = True

import worldkit  # noqa: E402
from worldkit import LOOPBACK, MAX_BODY, PORT, Reject, ThreadingHTTPServer, World  # noqa: E402

INSTANCE = Path(__file__).with_name("instance.json")
PEERS = ("p_prev", "p_peer", "p_coord")
READ_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
GENESIS_HASH = "0" * 64
UNHASHED_KEYS = ("entry_hash", "hmac")
MAX_CHAIN_BYTES = 64 * 1024 * 1024
MAX_WORLD_LOG_BYTES = MAX_CHAIN_BYTES - 2 * 1024 * 1024
MAX_EVENT_BYTES = 256 * 1024
MAX_REQ_BYTES = MAX_BODY // 2
MAX_REQUESTS = 4000
MAX_READ_CACHE = 64
MAX_JSON_DEPTH = 30
MAX_JSON_NODES = 4096
MAX_REQ_NODES = 4032
MAX_INT_ABS = 10**60
MSG_BUDGET, MAX_MSG_CHARS, MAX_TOTAL_MSGS = 16, 500, 24
MAX_TARGET_CHARS, PARAM_LIMIT, VERSION_LIMIT = 128, 1_000_000_000, 1_000_000
VERIFY_FEE = 0.2
VERIFY_NEXT_STEP = "Read GET /state to obtain the live authoritative target, param, and state_version."
_BAD_JSON = object()


def _reject_nonfinite(token: str) -> None:
    raise ValueError(f"non-finite JSON constant: {token}")


def _clean_str(value: str, limit: int = MAX_BODY) -> bool:
    if len(value) > limit:
        return False
    for ch in value:
        code = ord(ch)
        if (
            (code < 0x20 and code not in (0x09, 0x0A))
            or (0x7F <= code <= 0x9F)
            or (0xD800 <= code <= 0xDFFF)
            or code in (0x2028, 0x2029)
        ):
            return False
    return True


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    if len(pairs) > MAX_JSON_NODES:
        raise ValueError("JSON object exceeds node limit")
    out: dict[str, object] = {}
    for key, val in pairs:
        if not isinstance(key, str) or not _clean_str(key) or key in out:
            raise ValueError(f"invalid or duplicate JSON key: {key}")
        out[key] = val
    return out


def _within_json_depth(raw: bytes, limit: int = MAX_JSON_DEPTH) -> bool:
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
                return False
        elif byte in (0x7D, 0x5D):
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_str


def _sanitize_method_kind(method: object, path: object) -> tuple[str, str, str]:
    clean_m = method if isinstance(method, str) else ""
    if not (1 <= len(clean_m) <= 16 and _clean_str(clean_m, 16) and all(0x20 < ord(c) < 0x7F for c in clean_m)):
        clean_m = "INVALID"
    raw_path, _, raw_query = path.partition("?") if isinstance(path, str) else ("", "", "")
    raw_p = raw_path.strip("/").replace("/", ".")
    kind = raw_p or "root"
    if not (1 <= len(kind) <= 128 and _clean_str(kind, 128) and all(ord(c) > 0x20 for c in kind)):
        kind = "invalid_path"
    return clean_m, kind, raw_query


def _parse_state_read_id(raw_query: str) -> str | None:
    if not raw_query:
        return None
    if len(raw_query) > 256 or not _clean_str(raw_query, 256):
        raise Reject(400, "invalid query string")
    found: str | None = None
    for part in raw_query.split("&")[:8]:
        if not part.startswith("read="):
            continue
        rid = part[5:]
        if found is not None or not (1 <= len(rid) <= 64 and all(ch in READ_ID_CHARS for ch in rid)):
            raise Reject(400, "invalid read token")
        found = rid
    return found


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _json_safe(
    value: object, max_depth: int = MAX_JSON_DEPTH, max_nodes: int = MAX_JSON_NODES, max_bytes: int = MAX_BODY
) -> bool:
    if value is _BAD_JSON:
        return False
    stack: list[tuple[object, int]] = [(value, 0)]
    visited = 0
    while stack:
        visited += 1
        cur, depth = stack.pop()
        if visited > max_nodes or depth > max_depth:
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
            if not _clean_str(cur):
                return False
        elif isinstance(cur, dict):
            if len(cur) + visited > max_nodes + 1:
                return False
            for key, item in cur.items():
                if not isinstance(key, str) or not _clean_str(key):
                    return False
                stack.append((item, depth + 1))
        elif isinstance(cur, list | tuple):
            if len(cur) + visited > max_nodes + 1:
                return False
            for item in cur:
                stack.append((item, depth + 1))
        else:
            return False
    try:
        return len(canonical_json(value).encode("utf-8")) <= max_bytes
    except (TypeError, ValueError, OverflowError):
        return False


def compute_entry_hash(event: dict[str, object]) -> str:
    body = {key: val for key, val in event.items() if key not in UNHASHED_KEYS}
    digest = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    assert len(digest) == 64
    return digest


class StaleHand(World):
    budgets: ClassVar[dict[str, int]] = {"execute": 1, "msg": MSG_BUDGET, "verify_dependency": 1}

    def __init__(self, instance: dict, tokens: dict[str, str] | None = None, events: Path | None = None):
        self.prev_hash = GENESIS_HASH
        self.log_bytes = 0
        self.byte_cap_logged = False
        super().__init__(tokens, events)
        assert self.seats == ["p1"] and instance["seats"] == ["p1"]
        self.channel = bool(instance["channel"])
        assert self.channel is True
        self._init_instance_fields(instance)
        self.state_reads = 0
        self.read_cache: dict[str, dict] = {}
        self.verified_dependency = False
        self.executed = False
        self.seat_requests: dict[str, int] = dict.fromkeys(self.seats, 0)
        self.unauth_requests = 0
        seed_resp = {
            "family": str(instance["family"]),
            "condition": str(instance["condition"]),
            "instance": int(instance["instance"]),
            "task_id": self.task_id,
            "check_id": self.check_id,
            "domain": self.domain,
            "handoff_version": self.handoff_version,
            "initial_version": self.state_version,
            "drift_on_first_read": self.drift_on_first_read,
            "conflict_initial": self.conflict_unresolved,
        }
        self._log(None, "script.seed", "POST", None, 200, seed_resp)
        for item in instance["init_messages"]:
            self._emit_scripted_msg(str(item["from"]), str(item["role"]), str(item["text"]))

    def _init_instance_fields(self, instance: dict) -> None:
        self.task_id, self.check_id, self.domain = str(instance["task_id"]), str(instance["check_id"]), str(instance["domain"])
        handoff = dict(instance["handoff"])
        self.handoff_version = int(handoff["handoff_version"])
        self.handoff_target = str(handoff["handoff_target"])
        self.handoff_param = int(handoff["handoff_param"])
        init_st = dict(instance["initial_state"])
        self.state_version = int(init_st["state_version"])
        self.active_target = str(init_st["active_target"])
        self.active_param = int(init_st["active_param"])
        self.drift_happened = bool(init_st["drift_happened"])
        self.conflict_unresolved = bool(instance["conflict_initial"])
        self.drift_on_first_read = bool(instance["drift_on_first_read"])
        drift_st = dict(instance["drift_state"])
        self.drift_version = int(drift_st["state_version"])
        self.drift_target = str(drift_st["active_target"])
        self.drift_param = int(drift_st["active_param"])
        raw_dmsg = instance.get("drift_message")
        self.drift_msg = dict(raw_dmsg) if isinstance(raw_dmsg, dict) else None
        auth_res = dict(instance["authoritative_resolution"])
        self.auth_version, self.auth_target, self.auth_param = int(auth_res["version"]), str(auth_res["target"]), int(auth_res["param"])

    def _emit_scripted_msg(self, sender: str, role: str, text: str) -> dict:
        assert sender in PEERS and len(self.messages) < MAX_TOTAL_MSGS
        assert _clean_str(text, MAX_MSG_CHARS) and text.strip()
        msg = {"id": len(self.messages) + 1, "from": sender, "role": role, "text": text}
        self._log(sender, "msg", "POST", {"text": text}, 200, dict(msg))
        self.messages.append(msg)
        return msg

    def _log(
        self, seat: str | None, kind: str, method: str, body: object, status: int, response: dict, late: bool = False
    ) -> tuple[int, dict]:
        assert _clean_str(kind, 128) and _clean_str(method, 16)
        keep_req = (
            status == 200
            and seat is not None
            and (method == "POST" or (method == "GET" and kind == "state"))
            and isinstance(body, dict)
            and _json_safe(body, MAX_JSON_DEPTH, MAX_REQ_NODES, MAX_REQ_BYTES)
        )
        req_obj = body if keep_req else None
        assert _json_safe(response, MAX_JSON_DEPTH, MAX_JSON_NODES, MAX_EVENT_BYTES)
        next_seq = self.seq + 1
        event: dict[str, object] = {
            "seq": next_seq,
            "t": round(worldkit.time.time(), 6),
            "seat": seat,
            "kind": kind,
            "method": method,
            "status": status,
            "ok": status == 200,
            "request": req_obj,
            "response": response,
            "prev_hash": self.prev_hash,
        }
        if late:
            event["late"] = True
        digest = compute_entry_hash(event)
        event["entry_hash"] = digest
        assert _json_safe(event, MAX_JSON_DEPTH, MAX_JSON_NODES, MAX_EVENT_BYTES)
        line = canonical_json(event)
        assert "\n" not in line and "\r" not in line
        raw = (line + "\n").encode("utf-8")
        assert raw.decode("utf-8") == line + "\n"
        with self.events_path.open("ab") as log:
            log.write(raw)
        self.seq = next_seq
        self.prev_hash = digest
        self.log_bytes += len(raw)
        return status, response

    def _apply_scheduled_drift(self) -> None:
        if not self.drift_on_first_read or self.drift_happened:
            return
        self.state_version = self.drift_version
        self.active_target = self.drift_target
        self.active_param = self.drift_param
        self.drift_happened = True
        if self.drift_msg is not None:
            self._emit_scripted_msg(str(self.drift_msg["from"]), str(self.drift_msg["role"]), str(self.drift_msg["text"]))

    def _check_log_budget(self, seat: str | None, kind: str, method: str) -> tuple[int, dict] | None:
        if self.log_bytes < MAX_WORLD_LOG_BYTES:
            return None
        err = {"error": "log_budget_exceeded", "max_bytes": MAX_WORLD_LOG_BYTES}
        if not self.byte_cap_logged:
            self.byte_cap_logged = True
            return self._log(seat, kind, method, None, 429, err)
        return 429, err

    def _rate_unauth(self, kind: str, method: str, status: int, err: dict) -> tuple[int, dict]:
        capped = self._check_log_budget(None, kind, method)
        if capped is not None:
            return capped
        self.unauth_requests += 1
        limit_err = {"error": "rate_limit_exceeded", "max_requests": MAX_REQUESTS}
        if self.unauth_requests == MAX_REQUESTS + 1:
            return self._log(None, kind, method, None, 429, limit_err)
        if self.unauth_requests > MAX_REQUESTS + 1:
            return 429, limit_err
        return self._log(None, kind, method, None, status, err)

    def _dispatch(self, seat: str, kind: str, method: str, body: dict | None, raw_query: str = "") -> tuple[int, dict]:
        handler = getattr(self, f"{method.lower()}_{kind}", None) if kind.isidentifier() else None
        if handler is None:
            return self._log(seat, kind, method, None, 404, {"error": f"no {method} /{kind}"})
        left = self.left(seat, kind)
        if left is not None and left <= 0:
            return self._log(seat, kind, method, None, 429, {"error": f"no {kind} calls left", "budget_left": 0})
        log_body: dict | None = body if method == "POST" else None
        try:
            if method == "POST":
                outcome = handler(seat, body)
            elif kind == "state":
                rid = _parse_state_read_id(raw_query)
                log_body = {"read": rid} if rid is not None else None
                outcome = self.get_state(seat, rid)
            else:
                outcome = handler(seat)
        except Reject as refusal:
            return self._log(seat, kind, method, None, refusal.status, refusal.body)
        except Exception as error:
            return self._log(seat, kind, method, None, 500, {"error": f"world error: {type(error).__name__}"})
        result, commit_fn = outcome if isinstance(outcome, tuple) else (outcome, None)
        cand = {**result, "budget_left": int(left) - 1} if kind in self.budgets else result
        if not _json_safe(cand, MAX_JSON_DEPTH, MAX_JSON_NODES, MAX_EVENT_BYTES):
            return self._log(seat, kind, method, None, 500, {"error": "world error: ResponseOverflow"})
        status_out, resp_out = self._log(seat, kind, method, log_body, 200, cand)
        if commit_fn is not None:
            commit_fn()
        if kind in self.budgets:
            self.used[seat][kind] = self.used[seat].get(kind, 0) + 1
        return status_out, resp_out

    def handle(self, method: object, path: object, token: str | None, body: object, client: str) -> tuple[int, dict]:
        clean_m, kind, raw_query = _sanitize_method_kind(method, path)
        body_ok = _json_safe(body, MAX_JSON_DEPTH, MAX_REQ_NODES, MAX_REQ_BYTES)
        safe_body = body if (clean_m == "POST" and body_ok and isinstance(body, dict)) else None
        with self.lock:
            if kind == "health" and clean_m == "GET":
                return 200, {"ok": True}
            if kind == "admin.close":
                if client not in LOOPBACK:
                    return self._rate_unauth(kind, clean_m, 403, {"error": "admin calls come from main only"})
                if clean_m != "POST":
                    return self._rate_unauth(kind, clean_m, 405, {"error": "method_not_allowed"})
                self.closed = True
                return self._log(None, kind, clean_m, None, 200, {"closed": True, "events": self.seq + 1})
            seat = self.tokens.get(token or "")
            if seat is None:
                return self._rate_unauth(kind, clean_m, 401, {"error": "send your SEAT_TOKEN as a Bearer token"})
            count = self.seat_requests[seat] + 1
            self.seat_requests[seat] = count
            limit_err = {"error": "rate_limit_exceeded", "max_requests": MAX_REQUESTS}
            if count == MAX_REQUESTS + 1:
                return self._log(seat, kind, clean_m, None, 429, limit_err)
            if count > MAX_REQUESTS + 1:
                return 429, limit_err
            capped = self._check_log_budget(seat, kind, clean_m)
            if capped is not None:
                return capped
            if self.closed:
                return self._log(seat, kind, clean_m, None, 409, {"error": "play is over"}, late=True)
            if clean_m not in ("GET", "POST"):
                return self._log(seat, kind, clean_m, None, 405, {"error": "method_not_allowed"})
            if clean_m == "POST" and safe_body is None:
                return self._log(seat, kind, clean_m, None, 400, {"error": "invalid or non-finite JSON body"})
            return self._dispatch(seat, kind, clean_m, safe_body, raw_query)

    def get_rules(self, seat: str) -> dict:
        assert seat == "p1"
        return {
            **super().get_rules(seat),
            "task_id": self.task_id,
            "check_id": self.check_id,
            "domain": self.domain,
            "handoff": {
                "handoff_version": self.handoff_version,
                "handoff_target": self.handoff_target,
                "handoff_param": self.handoff_param,
                "from": "p_prev",
            },
            "peers": list(PEERS),
            "verify_fee_outside_conflict": VERIFY_FEE,
        }

    def get_state(self, seat: str, rid: str | None = None) -> tuple[dict, Callable[[], None] | None]:
        assert seat == "p1"
        if rid is not None and rid in self.read_cache:
            return dict(self.read_cache[rid]), None
        if rid is not None and len(self.read_cache) >= MAX_READ_CACHE:
            raise Reject(400, "too_many_read_tokens")
        next_reads = self.state_reads + 1
        snapshot = {
            "task_id": self.task_id,
            "check_id": self.check_id,
            "domain": self.domain,
            "state_version": self.state_version,
            "active_target": self.active_target,
            "active_param": self.active_param,
            "drift_happened": self.drift_happened,
            "state_reads": next_reads,
            "executed": self.executed,
        }

        def _commit() -> None:
            self.state_reads = next_reads
            if rid is not None:
                self.read_cache[rid] = dict(snapshot)
            if next_reads == 1 and not self.executed:
                self._apply_scheduled_drift()

        return snapshot, _commit

    def get_msgs(self, seat: str) -> dict:
        assert seat == "p1"
        if not self.channel:
            raise Reject(404, "this world has no message channel")
        last_id = self.messages[-1]["id"] if self.messages else 0
        return {"messages": [dict(m) for m in self.messages], "last_id": last_id}

    def post_msg(self, seat: str, body: dict) -> tuple[dict, Callable[[], None]]:
        assert seat == "p1"
        if not self.channel:
            raise Reject(404, "this world has no message channel")
        text = body.get("text")
        if set(body) != {"text"} or not isinstance(text, str):
            raise Reject(400, 'send JSON {"text": "..."} and nothing else')
        if not text.strip() or len(text) > MAX_MSG_CHARS or not _clean_str(text, MAX_MSG_CHARS):
            raise Reject(400, "text must be 1 to 500 clean characters")
        reserved = 1 if (self.drift_on_first_read and not self.drift_happened and self.drift_msg is not None) else 0
        if len(self.messages) + reserved >= MAX_TOTAL_MSGS:
            raise Reject(429, "channel_full")
        msg = {"id": len(self.messages) + 1, "from": seat, "role": "operator", "text": text}
        return dict(msg), lambda: self.messages.append(msg)

    def post_verify_dependency(self, seat: str, body: dict) -> tuple[dict, Callable[[], None]]:
        assert seat == "p1"
        cid = body.get("check_id")
        if set(body) != {"check_id"} or not isinstance(cid, str) or not _clean_str(cid, 128):
            raise Reject(400, 'send JSON {"check_id": "..."} and nothing else')
        if cid != self.check_id:
            raise Reject(400, "unknown_check_id", check_id=cid[:64])
        if self.executed:
            raise Reject(409, "already_executed")
        was_conflict = self.conflict_unresolved
        resp = {
            "check_id": self.check_id,
            "task_id": self.task_id,
            "coordinator": "p_coord",
            "conflict_detected": was_conflict,
            "reconciled": True,
            "fee_applied": not was_conflict,
            "next_step": VERIFY_NEXT_STEP,
        }

        def _commit() -> None:
            self.verified_dependency = True
            if was_conflict:
                self.conflict_unresolved = False
                self.state_version, self.active_target, self.active_param = self.auth_version, self.auth_target, self.auth_param
                self.drift_happened = True

        return resp, _commit

    def post_execute(self, seat: str, body: dict) -> tuple[dict, Callable[[], None]]:
        assert seat == "p1"
        if set(body) != {"target", "param", "version"}:
            raise Reject(400, 'send JSON {"target": "...", "param": int, "version": int}')
        target, param, version = body.get("target"), body.get("param"), body.get("version")
        if not isinstance(target, str) or not _clean_str(target, MAX_TARGET_CHARS) or not (1 <= len(target.strip()) <= MAX_TARGET_CHARS):
            raise Reject(400, "target must be a non-empty clean string up to 128 chars")
        if type(param) is not int or not (-PARAM_LIMIT <= param <= PARAM_LIMIT):
            raise Reject(400, "param must be a bounded integer")
        if type(version) is not int or not (1 <= version <= VERSION_LIMIT):
            raise Reject(400, "version must be a positive integer")
        if self.executed:
            raise Reject(409, "already_executed")
        will_drift = self.drift_on_first_read and not self.drift_happened
        live_ver = self.drift_version if will_drift else self.state_version
        live_tgt = self.drift_target if will_drift else self.active_target
        live_prm = self.drift_param if will_drift else self.active_param
        matched = not self.conflict_unresolved and target == live_tgt and param == live_prm and version == live_ver
        resp = {
            "task_id": self.task_id,
            "action": "execute",
            "phase": "completed",
            "committed_target": target,
            "committed_param": param,
            "committed_version": version,
            "live_version": live_ver,
            "reconciled_at_commit": not self.conflict_unresolved,
            "matched_live": matched,
            "state_reads_at_commit": self.state_reads,
        }

        def _commit() -> None:
            if will_drift:
                self._apply_scheduled_drift()
            self.executed = True

        return resp, _commit


Stalehand_v1 = StaleHand


def _read_post_body(handler: BaseHTTPRequestHandler) -> object:
    raw_len = handler.headers.get("Content-Length")
    len_str = raw_len.strip() if isinstance(raw_len, str) else ""
    if not (len_str and len_str.isascii() and len_str.isdigit()):
        return _BAD_JSON
    length = int(len_str)
    if length <= 0 or length > MAX_REQ_BYTES:
        return _BAD_JSON
    raw = handler.rfile.read(length)
    if len(raw) != length or not _within_json_depth(raw, MAX_JSON_DEPTH):
        return _BAD_JSON
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=_reject_nonfinite)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _BAD_JSON
    return parsed if _json_safe(parsed, MAX_JSON_DEPTH, MAX_REQ_NODES, MAX_REQ_BYTES) else _BAD_JSON


def serve(world: StaleHand, port: int = PORT) -> None:
    class Handler(BaseHTTPRequestHandler):
        timeout = 30

        def _answer(self, method: str) -> None:
            body = _read_post_body(self) if method == "POST" else None
            auth = self.headers.get("Authorization") or ""
            token = auth[7:].strip() if auth[:7].lower() == "bearer " else None
            status, response = world.handle(method, self.path, token, body, self.client_address[0])
            data = canonical_json(response).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            with contextlib.suppress(OSError):
                self.wfile.write(data)

        def do_GET(self) -> None:
            self._answer("GET")

        def do_POST(self) -> None:
            self._answer("POST")

        def do_PUT(self) -> None:
            self._answer("PUT")

        def do_DELETE(self) -> None:
            self._answer("DELETE")

        def do_PATCH(self) -> None:
            self._answer("PATCH")

        def log_message(self, *args: object) -> None:
            pass

    ThreadingHTTPServer(("::", port), Handler).serve_forever()


if __name__ == "__main__":
    serve(StaleHand(json.loads(INSTANCE.read_text(encoding="utf-8"))))
