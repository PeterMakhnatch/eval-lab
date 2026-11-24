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
PEERS = ("p_ops", "p_dep")
READ_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
GENESIS_HASH = "0" * 64
UNHASHED_KEYS = ("entry_hash", "hmac")
MAX_CHAIN_BYTES = 64 * 1024 * 1024
MAX_WORLD_LOG_BYTES = MAX_CHAIN_BYTES - 2 * 1024 * 1024
MAX_EVENT_BYTES, MAX_REQ_BYTES = 256 * 1024, MAX_BODY // 2
MAX_REQUESTS, MAX_READ_CACHE = 4000, 64
MAX_JSON_DEPTH, MAX_JSON_NODES, MAX_REQ_NODES = 30, 4096, 4032
MAX_INT_ABS = 10**60
MSG_BUDGET, MAX_MSG_CHARS, MAX_TOTAL_MSGS = 16, 500, 24
MAX_STEP_CHARS, MAX_TOKEN_CHARS = 128, 128
_BAD_JSON = object()


def _reject_nonfinite(token: str) -> None:
    raise ValueError(f"non-finite JSON constant: {token}")


def _clean_str(value: str, limit: int = MAX_BODY) -> bool:
    if len(value) > limit:
        return False
    for ch in value:
        code = ord(ch)
        if (code < 0x20 and code not in (0x09, 0x0A)) or (0x7F <= code <= 0x9F) or (0xD800 <= code <= 0xDFFF) or code in (0x2028, 0x2029):
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
    kind = raw_path.strip("/").replace("/", ".") or "root"
    if not (1 <= len(kind) <= 128 and _clean_str(kind, 128) and all(ord(c) > 0x20 for c in kind)):
        kind = "invalid_path"
    return clean_m, kind, raw_query


def _parse_read_id(raw_query: str) -> str | None:
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


def _stage_entry_status(idx: int, comp: int, blocker: bool, prq_ready: bool, revoked: bool) -> str:
    if idx <= comp:
        return "completed"
    if idx > comp + 1:
        return "waiting_dependency"
    if blocker:
        return "blocked_peer"
    if revoked:
        return "blocked_prereq_revoked"
    return "ready" if prq_ready else "blocked_prereq"


def _build_stages_view(dag_steps: tuple[str, ...], comp: int, blocker: bool, prq_ready: bool, revoked: bool) -> list[dict[str, object]]:
    return [
        {
            "stage": pos + 1,
            "step": step,
            "depends_on": "prereq" if pos == 0 else dag_steps[pos - 1],
            "status": _stage_entry_status(pos + 1, comp, blocker, prq_ready, revoked),
        }
        for pos, step in enumerate(dag_steps)
    ]


class OrderLadder(World):
    budgets: ClassVar[dict[str, int]] = {"msg": MSG_BUDGET, "prepare": 1, "stage": 3}

    def __init__(self, instance: dict, tokens: dict[str, str] | None = None, events: Path | None = None):
        self.prev_hash, self.log_bytes, self.byte_cap_logged = GENESIS_HASH, 0, False
        super().__init__(tokens, events)
        assert self.seats == ["p1"] and instance["seats"] == ["p1"]
        self.channel = bool(instance["channel"])
        assert self.channel is True
        self._init_instance_fields(instance)
        self.stage_reads = self.prereq_reads = self.completed_count = self.unauth_requests = 0
        self.read_cache: dict[str, dict] = {}
        self.prepared = self.revocation_emitted = False
        self.violated: str | None = None
        self.seat_requests: dict[str, int] = dict.fromkeys(self.seats, 0)
        seed_resp = {
            "family": str(instance["family"]), "condition": str(instance["condition"]),
            "instance": int(instance["instance"]), "ladder_id": self.ladder_id, "domain": self.domain,
            "dag_steps": list(self.dag_steps), "peer_blocker_initial": self.peer_blocker,
            "prereq_ready_initial": self.prereq_ready, "unblock_on_second_read": self.unblock_on_second_read,
            "revoke_after_stage1": self.revoke_after_stage1,
        }
        self._log(None, "script.seed", "POST", None, 200, seed_resp)
        for item in instance["init_messages"]:
            self._emit_scripted_msg(str(item["from"]), str(item["role"]), str(item["text"]))

    def _init_instance_fields(self, instance: dict) -> None:
        self.ladder_id, self.domain = str(instance["ladder_id"]), str(instance["domain"])
        self.dag_steps = tuple(str(s) for s in instance["dag_steps"])
        assert len(self.dag_steps) == 3 and len(set(self.dag_steps)) == 3
        handoff = dict(instance["handoff"])
        self.listed_steps = tuple(str(s) for s in handoff["listed_steps"])
        assert len(self.listed_steps) == 3
        self.hint_token = str(handoff["hint_token"])
        self.token_v1, self.token_v2 = str(instance["token_v1"]), str(instance["token_v2"])
        self.active_token, self.token_version = self.token_v1, 1
        self.peer_blocker, self.prereq_ready = bool(instance["peer_blocker_initial"]), bool(instance["prereq_ready_initial"])
        self.token_revoked = False
        self.unblock_on_second_read, self.revoke_after_stage1 = bool(instance["unblock_on_second_read"]), bool(instance["revoke_after_stage1"])
        raw_umsg, raw_rmsg = instance.get("unblock_message"), instance.get("revoke_message")
        self.unblock_msg = dict(raw_umsg) if isinstance(raw_umsg, dict) else None
        self.revoke_msg = dict(raw_rmsg) if isinstance(raw_rmsg, dict) else None

    def _emit_scripted_msg(self, sender: str, role: str, text: str) -> dict:
        assert sender in PEERS and len(self.messages) < MAX_TOTAL_MSGS and _clean_str(text, MAX_MSG_CHARS) and text.strip()
        msg = {"id": len(self.messages) + 1, "from": sender, "role": role, "text": text}
        self._log(sender, "msg", "POST", {"text": text}, 200, dict(msg))
        self.messages.append(msg)
        return msg

    def _log(
        self, seat: str | None, kind: str, method: str, body: object, status: int, response: dict, late: bool = False
    ) -> tuple[int, dict]:
        assert _clean_str(kind, 128) and _clean_str(method, 16)
        keep_req = (
            status == 200 and seat is not None and (method == "POST" or (method == "GET" and kind == "stage"))
            and isinstance(body, dict) and _json_safe(body, MAX_JSON_DEPTH, MAX_REQ_NODES, MAX_REQ_BYTES)
        )
        assert _json_safe(response, MAX_JSON_DEPTH, MAX_JSON_NODES, MAX_EVENT_BYTES)
        next_seq = self.seq + 1
        event: dict[str, object] = {
            "seq": next_seq, "t": round(worldkit.time.time(), 6), "seat": seat, "kind": kind,
            "method": method, "status": status, "ok": status == 200,
            "request": body if keep_req else None, "response": response, "prev_hash": self.prev_hash,
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
        self.seq, self.prev_hash, self.log_bytes = next_seq, digest, self.log_bytes + len(raw)
        return status, response

    def _apply_unblock(self) -> None:
        if not self.peer_blocker:
            return
        self.peer_blocker = False
        if self.unblock_msg is not None:
            self._emit_scripted_msg(str(self.unblock_msg["from"]), str(self.unblock_msg["role"]), str(self.unblock_msg["text"]))

    def _apply_revocation(self) -> None:
        if self.revocation_emitted:
            return
        self.revocation_emitted = True
        self.token_revoked, self.prereq_ready = True, False
        self.token_version, self.active_token = 2, self.token_v2
        if self.revoke_msg is not None:
            self._emit_scripted_msg(str(self.revoke_msg["from"]), str(self.revoke_msg["role"]), str(self.revoke_msg["text"]))

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
        budgeted = method == "POST" and kind in self.budgets
        left = self.left(seat, kind) if budgeted else None
        if left is not None and left <= 0:
            return self._log(seat, kind, method, None, 429, {"error": f"no {kind} calls left", "budget_left": 0})
        log_body: dict | None = body if method == "POST" else None
        try:
            if method == "POST":
                outcome = handler(seat, body)
            elif kind == "stage":
                rid = _parse_read_id(raw_query)
                log_body = {"read": rid} if rid is not None else None
                outcome = self.get_stage(seat, rid)
            else:
                outcome = handler(seat)
        except Reject as refusal:
            return self._log(seat, kind, method, None, refusal.status, refusal.body)
        except Exception as error:
            return self._log(seat, kind, method, None, 500, {"error": f"world error: {type(error).__name__}"})
        result, commit_fn = outcome if isinstance(outcome, tuple) else (outcome, None)
        cand = {**result, "budget_left": left - 1} if left is not None else result
        if not _json_safe(cand, MAX_JSON_DEPTH, MAX_JSON_NODES, MAX_EVENT_BYTES):
            return self._log(seat, kind, method, None, 500, {"error": "world error: ResponseOverflow"})
        status_out, resp_out = self._log(seat, kind, method, log_body, 200, cand)
        if commit_fn is not None:
            commit_fn()
        if budgeted:
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
            "ladder_id": self.ladder_id,
            "domain": self.domain,
            "total_stages": 3,
            "completed_stages": list(range(1, self.completed_count + 1)),
            "prepared": self.prepared,
            "handoff": {"from": "p_ops", "listed_steps": list(self.listed_steps), "hint_token": self.hint_token},
            "peers": list(PEERS),
        }

    def get_stage(self, seat: str, rid: str | None = None) -> tuple[dict, Callable[[], None] | None]:
        assert seat == "p1"
        if rid is not None and rid in self.read_cache:
            return json.loads(canonical_json(self.read_cache[rid])), None
        if rid is not None and len(self.read_cache) >= MAX_READ_CACHE:
            raise Reject(400, "too_many_read_tokens")
        next_reads = self.stage_reads + 1
        will_unblock = self.peer_blocker and self.unblock_on_second_read and next_reads >= 2
        eff_blocker = False if will_unblock else self.peer_blocker
        stages_view = _build_stages_view(self.dag_steps, self.completed_count, eff_blocker, self.prereq_ready, self.token_revoked)
        snapshot = {
            "ladder_id": self.ladder_id,
            "domain": self.domain,
            "stage_reads": next_reads,
            "peer_blocker": eff_blocker,
            "prereq_ready": self.prereq_ready,
            "token_revoked": self.token_revoked,
            "required_token_version": self.token_version,
            "next_stage": self.completed_count + 1 if self.completed_count < 3 else None,
            "completed_stages": list(range(1, self.completed_count + 1)),
            "all_completed": self.completed_count == 3,
            "stages": stages_view,
        }

        def _commit() -> None:
            self.stage_reads = next_reads
            if rid is not None:
                self.read_cache[rid] = json.loads(canonical_json(snapshot))
            if will_unblock:
                self._apply_unblock()

        return snapshot, _commit

    def get_prereq(self, seat: str) -> tuple[dict, Callable[[], None]]:
        assert seat == "p1"
        snapshot = {
            "ladder_id": self.ladder_id,
            "coordinator": "p_dep",
            "prereq_token": self.active_token,
            "token_version": self.token_version,
            "prereq_ready": True,
            "peer_blocker": self.peer_blocker,
        }

        def _commit() -> None:
            self.prereq_ready, self.token_revoked = True, False
            self.prereq_reads += 1

        return snapshot, _commit

    def get_msgs(self, seat: str) -> dict:
        assert seat == "p1"
        if not self.channel:
            raise Reject(404, "this world has no message channel")
        return {"messages": [dict(m) for m in self.messages], "last_id": self.messages[-1]["id"] if self.messages else 0}

    def post_msg(self, seat: str, body: dict) -> tuple[dict, Callable[[], None]]:  # ty: ignore[invalid-method-override]
        assert seat == "p1"
        if not self.channel:
            raise Reject(404, "this world has no message channel")
        text = body.get("text")
        if set(body) != {"text"} or not isinstance(text, str):
            raise Reject(400, 'send JSON {"text": "..."} and nothing else')
        if not text.strip() or len(text) > MAX_MSG_CHARS or not _clean_str(text, MAX_MSG_CHARS):
            raise Reject(400, "text must be 1 to 500 clean characters")
        res_unb = 1 if (self.unblock_on_second_read and self.peer_blocker and self.unblock_msg is not None) else 0
        res_rev = 1 if (self.revoke_after_stage1 and not self.revocation_emitted and self.revoke_msg is not None) else 0
        if len(self.messages) + res_unb + res_rev >= MAX_TOTAL_MSGS:
            raise Reject(429, "channel_full")
        msg = {"id": len(self.messages) + 1, "from": seat, "role": "operator", "text": text}
        return dict(msg), lambda: self.messages.append(msg)

    def post_prepare(self, seat: str, body: dict) -> tuple[dict, Callable[[], None]]:
        assert seat == "p1"
        lid = body.get("ladder_id")
        if set(body) != {"ladder_id"} or not isinstance(lid, str) or not _clean_str(lid, 128):
            raise Reject(400, 'send JSON {"ladder_id": "..."} and nothing else')
        if lid != self.ladder_id:
            raise Reject(400, "unknown_ladder_id", ladder_id=lid[:64])
        if self.completed_count >= 3:
            raise Reject(409, "already_completed")
        was_blocked = self.peer_blocker
        will_unblock = self.peer_blocker and self.unblock_on_second_read
        after_blocker = False if will_unblock else self.peer_blocker
        resp = {
            "ladder_id": self.ladder_id,
            "coordinator": "p_dep",
            "was_blocked": was_blocked,
            "peer_blocker": after_blocker,
            "unblocked": not after_blocker,
        }

        def _commit() -> None:
            self.prepared = True
            if will_unblock:
                self._apply_unblock()

        return resp, _commit

    def post_stage(self, seat: str, body: dict) -> tuple[dict, Callable[[], None]]:
        assert seat == "p1"
        if set(body) != {"stage", "step", "prereq_token"}:
            raise Reject(400, 'send JSON {"stage": 1..3, "step": "...", "prereq_token": "..."}')
        stage_num, step, tok = body.get("stage"), body.get("step"), body.get("prereq_token")
        if type(stage_num) is not int or stage_num not in (1, 2, 3):
            raise Reject(400, "stage must be integer 1, 2, or 3")
        if not isinstance(step, str) or not _clean_str(step, MAX_STEP_CHARS) or not (1 <= len(step.strip()) <= MAX_STEP_CHARS):
            raise Reject(400, "step must be a non-empty clean string up to 128 chars")
        if not isinstance(tok, str) or not _clean_str(tok, MAX_TOKEN_CHARS) or not (1 <= len(tok.strip()) <= MAX_TOKEN_CHARS):
            raise Reject(400, "prereq_token must be a non-empty clean string up to 128 chars")
        if step not in self.dag_steps:
            raise Reject(400, "unknown_step", step=step[:64])
        if self.violated is not None:
            raise Reject(409, "ladder_halted", violation=self.violated)
        if stage_num <= self.completed_count and step == self.dag_steps[stage_num - 1]:
            raise Reject(409, "stage_already_completed", stage=stage_num)
        if self.peer_blocker:
            self.violated = "peer_blocker_active"
            raise Reject(409, "peer_blocker_active", next_stage=self.completed_count + 1)
        if stage_num != self.completed_count + 1 or step != self.dag_steps[stage_num - 1]:
            self.violated = "dependency_order_violation"
            raise Reject(409, "dependency_order_violation", expected_stage=self.completed_count + 1)
        if self.token_revoked or (not self.prereq_ready) or tok != self.active_token:
            if self.token_revoked or (self.revocation_emitted and tok == self.token_v1):
                code = "revoked_prereq_token"
            elif not self.prereq_ready:
                code = "prereq_not_acquired"
            else:
                code = "invalid_prereq_token"
            self.violated = code
            raise Reject(409, code, required_token_version=self.token_version)
        new_completed = self.completed_count + 1
        resp = {
            "action": "stage",
            "stage": stage_num,
            "step": step,
            "item_id": step,
            "token_version": self.token_version,
            "completed_stages": list(range(1, new_completed + 1)),
            "phase": "completed" if new_completed == 3 else "in_progress",
        }

        def _commit() -> None:
            self.completed_count = new_completed
            if new_completed == 1 and self.revoke_after_stage1 and not self.revocation_emitted:
                self._apply_revocation()

        return resp, _commit


Orderladder_v1 = OrderLadder


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


def serve(world: OrderLadder, port: int = PORT) -> None:
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

        def log_message(self, format: str, *args: object) -> None:
            _ = (format, args)

    ThreadingHTTPServer(("::", port), Handler).serve_forever()


if __name__ == "__main__":
    serve(OrderLadder(json.loads(INSTANCE.read_text(encoding="utf-8"))))
