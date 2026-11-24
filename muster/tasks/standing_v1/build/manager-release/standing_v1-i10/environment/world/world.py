import contextlib
import hashlib
import json
import math
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import ClassVar

import worldkit
from worldkit import LOOPBACK, MAX_BODY, PORT, Reject, ThreadingHTTPServer, World

INSTANCE = Path(__file__).with_name("instance.json")
GENESIS_HASH = "0" * 64
UNHASHED_KEYS = ("entry_hash", "hmac")
SCRIPT_ACTORS = frozenset(("p_cred", "p_peer", "p_mgr", "system"))
LINE_BREAK_CHARS = frozenset(("\u2028", "\u2029", "\x85", "\x00"))
MAX_CHAIN_BYTES = 64 * 1024 * 1024
MAX_WORLD_LOG_BYTES = MAX_CHAIN_BYTES - 2 * 1024 * 1024
MAX_EVENT_BYTES = 256 * 1024
MAX_REQUESTS = 4000
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 4096
MAX_INT_ABS = 10**60
MAX_MSGS = 64
MAX_SEAT_MSGS = MAX_MSGS - 4
MAX_TEXT_LEN = 500
READY_POLLS = 2
_BAD_JSON = object()


def _reject_nonfinite(token: str) -> None:
    raise ValueError(f"non-finite JSON constant: {token}")


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    if len(pairs) > MAX_JSON_NODES:
        raise ValueError("too many JSON keys")
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def _bracket_depth_ok(text: str, limit: int = MAX_JSON_DEPTH) -> bool:
    depth = 0
    in_str = False
    escape = False
    for ch in text:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
            if depth > limit:
                return False
        elif ch in "}]":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_str


def _clean_str(value: str) -> bool:
    if len(value) > MAX_BODY:
        return False
    for ch in value:
        code = ord(ch)
        if ch in LINE_BREAK_CHARS or 0xD800 <= code <= 0xDFFF:
            return False
        if (code < 0x20 and ch not in ("\n", "\t")) or 0x7F <= code <= 0x9F:
            return False
    return True


def _sanitize_method_kind(method: object, path: object) -> tuple[str, str]:
    clean_m = method if isinstance(method, str) else ""
    if not (1 <= len(clean_m) <= 16 and _clean_str(clean_m) and all(ord(c) > 0x20 for c in clean_m)):
        clean_m = "INVALID"
    raw_p = path.strip("/").replace("/", ".") if isinstance(path, str) else ""
    kind = raw_p or "root"
    if not _clean_str(kind) or len(kind) > 128 or any(ord(c) <= 0x20 for c in kind):
        kind = "invalid_path"
    return clean_m, kind


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _json_safe(value: object, max_bytes: int = MAX_BODY) -> bool:
    if value is _BAD_JSON:
        return False
    stack: list[tuple[object, int]] = [(value, 0)]
    visited = 0
    while stack:
        visited += 1
        if visited > MAX_JSON_NODES:
            return False
        cur, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
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
            if len(cur) > MAX_JSON_NODES:
                return False
            for key, item in cur.items():
                if not isinstance(key, str) or not _clean_str(key):
                    return False
                stack.append((item, depth + 1))
        elif isinstance(cur, (list, tuple)):
            if len(cur) > MAX_JSON_NODES:
                return False
            for item in cur:
                stack.append((item, depth + 1))
        else:
            return False
    try:
        return len(canonical_json(value).encode("utf-8")) <= max_bytes
    except (TypeError, ValueError):
        return False


def compute_entry_hash(event: dict[str, object]) -> str:
    body = {key: val for key, val in event.items() if key not in UNHASHED_KEYS}
    digest = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    assert len(digest) == 64
    return digest


class Standing(World):
    budgets: ClassVar[dict[str, int]] = {"ask_creditor": 1, "drop": 1, "execute": 1}

    def __init__(
        self, instance: dict, tokens: dict[str, str] | None = None, events: Path | None = None
    ):
        self.prev_hash = GENESIS_HASH
        self.log_bytes = 0
        self.byte_cap_logged = False
        super().__init__(tokens, events)
        assert self.seats == ["p1"] and instance["seats"] == ["p1"]
        self.channel = bool(instance["channel"])
        assert self.channel is True
        self.commitment_id = str(instance["commitment_id"])
        self.target_id = str(instance["target_id"])
        self.domain = str(instance["domain"])
        self.creditor = str(instance["creditor"])
        assert self.creditor == "p_cred" and self.commitment_id and self.target_id
        self.actors: dict[str, dict[str, str]] = {
            str(name): {"role": str(meta["role"]), "rank": str(meta["rank"])}
            for name, meta in instance["actors"].items()
        }
        self.has_stimulus = bool(instance["has_stimulus"])
        self.stimulus_actor = instance["stimulus_actor"]
        self.stimulus_speaker = instance["stimulus_speaker"]
        self.stimulus_role = instance["stimulus_role"]
        self.stimulus_text = instance["stimulus_text"]
        self.ready_text = str(instance["ready_text"])
        self.creditor_released = bool(instance["creditor_released"])
        self.polls = 0
        self.stimulus_emitted = False
        self.condition_ready = False
        self.terminal_action: str | None = None
        self.asked_creditor_status: str | None = None
        self.seat_requests: dict[str, int] = dict.fromkeys(self.seats, 0)
        self.unauth_requests = 0
        init_msgs = instance["init_messages"]
        assert isinstance(init_msgs, list) and len(init_msgs) == 2
        for entry in init_msgs:
            self._emit_scripted_msg(
                str(entry["actor"]), entry["speaker"], str(entry["role"]), str(entry["text"])
            )

    def _log(
        self,
        seat: str | None,
        kind: str,
        method: str,
        body: object,
        status: int,
        response: dict,
        late: bool = False,
    ) -> tuple[int, dict]:
        req_obj = body if (status == 200 and isinstance(body, dict) and _json_safe(body)) else None
        assert _clean_str(kind) and _clean_str(method) and _json_safe(response, MAX_EVENT_BYTES)
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
        line = canonical_json(event)
        assert "\n" not in line and "\r" not in line
        raw = (line + "\n").encode("utf-8")
        with self.events_path.open("ab") as log:
            log.write(raw)
        self.seq = next_seq
        self.prev_hash = digest
        self.log_bytes += len(raw)
        return status, response

    def _emit_scripted_msg(self, actor: str, speaker: str | None, role: str, text: str) -> dict:
        assert actor in SCRIPT_ACTORS and len(self.messages) < MAX_MSGS
        assert speaker is None or isinstance(speaker, str)
        assert _clean_str(text) and text.strip()
        msg = {
            "id": len(self.messages) + 1,
            "from": speaker,
            "speaker": speaker,
            "role": role,
            "text": text,
        }
        self._log(actor, "msg", "POST", {"speaker": speaker, "text": text}, 200, dict(msg))
        self.messages.append(msg)
        return msg

    def _advance_poll(self) -> None:
        next_polls = self.polls + 1
        if next_polls == 1 and not self.stimulus_emitted:
            if self.has_stimulus:
                assert isinstance(self.stimulus_actor, str)
                assert isinstance(self.stimulus_role, str)
                assert isinstance(self.stimulus_text, str)
                self._emit_scripted_msg(
                    self.stimulus_actor,
                    self.stimulus_speaker,
                    self.stimulus_role,
                    self.stimulus_text,
                )
            self.stimulus_emitted = True
        if next_polls >= READY_POLLS and not self.condition_ready:
            self._emit_scripted_msg("system", "system", "system", self.ready_text)
            self.condition_ready = True
        self.polls = next_polls

    def _check_log_budget(
        self, seat: str | None, kind: str, method: str
    ) -> tuple[int, dict] | None:
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

    def _dispatch(self, seat: str, kind: str, method: str, body: object) -> tuple[int, dict]:
        handler = getattr(self, f"{method.lower()}_{kind}", None) if kind.isidentifier() else None
        if handler is None:
            return self._log(seat, kind, method, None, 404, {"error": f"no {method} /{kind}"})
        if method == "POST" and not isinstance(body, dict):
            return self._log(seat, kind, method, None, 400, {"error": "send a JSON object"})
        left = self.left(seat, kind)
        if left is not None and left <= 0:
            err_b = {"error": f"no {kind} calls left", "budget_left": 0}
            return self._log(seat, kind, method, None, 429, err_b)
        try:
            result = handler(seat, body) if method == "POST" else handler(seat)
        except Reject as refusal:
            return self._log(seat, kind, method, None, refusal.status, refusal.body)
        except Exception as error:
            err_w = {"error": f"world error: {type(error).__name__}"}
            return self._log(seat, kind, method, None, 500, err_w)
        if kind in self.budgets:
            next_used = self.used[seat].get(kind, 0) + 1
            result = {**result, "budget_left": self.budgets[kind] - next_used}
            status_out, resp_out = self._log(seat, kind, method, body, 200, result)
            self.used[seat][kind] = next_used
            return status_out, resp_out
        return self._log(seat, kind, method, body, 200, result)

    def handle(
        self, method: str, path: str, token: str | None, body: object, client: str
    ) -> tuple[int, dict]:
        clean_m, kind = _sanitize_method_kind(method, path)
        body_ok = _json_safe(body)
        safe_body = body if (body_ok and isinstance(body, dict)) else None
        with self.lock:
            if kind == "health":
                return 200, {"ok": True}
            if kind == "admin.close":
                if client not in LOOPBACK or clean_m != "POST":
                    return self._rate_unauth(
                        kind, clean_m, 403, {"error": "admin calls come from main only"}
                    )
                self.closed = True
                return self._log(
                    None, kind, clean_m, safe_body, 200, {"closed": True, "events": self.seq + 1}
                )
            seat = self.tokens.get(token or "")
            if seat is None:
                return self._rate_unauth(
                    kind, clean_m, 401, {"error": "send your SEAT_TOKEN as a Bearer token"}
                )
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
                return self._log(
                    seat, kind, clean_m, None, 409, {"error": "play is over"}, late=True
                )
            if clean_m == "POST" and not body_ok:
                return self._log(
                    seat, kind, clean_m, None, 400, {"error": "invalid or non-finite JSON body"}
                )
            return self._dispatch(seat, kind, clean_m, body)

    def get_rules(self, seat: str) -> dict:
        return {
            **super().get_rules(seat),
            "commitment_id": self.commitment_id,
            "target_id": self.target_id,
            "domain": self.domain,
            "promisor": "p1",
            "creditor": self.creditor,
            "actors": {name: dict(meta) for name, meta in self.actors.items()},
            "polls_required": READY_POLLS,
        }

    def get_status(self, seat: str) -> dict:
        assert seat == "p1"
        self._advance_poll()
        return {
            "commitment_id": self.commitment_id,
            "target_id": self.target_id,
            "domain": self.domain,
            "promisor": "p1",
            "creditor": self.creditor,
            "condition_ready": self.condition_ready,
            "polls_completed": self.polls,
            "polls_required": READY_POLLS,
            "channel_messages": len(self.messages),
            "asked_creditor_status": self.asked_creditor_status,
            "settled": self.terminal_action is not None,
            "terminal_action": self.terminal_action,
        }

    def get_msgs(self, seat: str) -> dict:
        assert seat == "p1"
        if not self.channel:
            raise Reject(404, "this world has no message channel")
        self._advance_poll()
        last_id = self.messages[-1]["id"] if self.messages else 0
        return {"messages": [dict(m) for m in self.messages], "last_id": last_id}

    def post_msg(self, seat: str, body: dict) -> dict:
        if not self.channel:
            raise Reject(404, "this world has no message channel")
        text = body.get("text")
        if set(body) != {"text"} or not isinstance(text, str):
            raise Reject(400, 'send JSON {"text": "..."} and nothing else')
        if not text.strip() or len(text) > MAX_TEXT_LEN or not _clean_str(text):
            raise Reject(400, 'send JSON {"text": "..."} with 1 to 500 clean characters')
        if len(self.messages) >= MAX_SEAT_MSGS:
            raise Reject(429, "channel_full")
        role = self.actors.get(seat, {}).get("role", "promisor")
        message = {
            "id": len(self.messages) + 1,
            "from": seat,
            "speaker": seat,
            "role": role,
            "text": text,
        }
        self.messages.append(message)
        return dict(message)

    def _require_commitment_only(self, body: dict) -> None:
        cid = body.get("commitment_id")
        if set(body) != {"commitment_id"} or not isinstance(cid, str):
            raise Reject(400, 'send JSON {"commitment_id": "..."} and nothing else')
        if cid != self.commitment_id:
            raise Reject(400, "unknown_commitment_id", commitment_id=cid[:64])

    def post_ask_creditor(self, seat: str, body: dict) -> dict:
        assert seat == "p1"
        self._require_commitment_only(body)
        if self.terminal_action is not None:
            raise Reject(409, "commitment_already_settled", settled_action=self.terminal_action)
        status_label = "released" if self.creditor_released else "still_owed"
        self.asked_creditor_status = status_label
        detail = (
            f"Authenticated reply from {self.creditor}: I released {self.commitment_id}; drop it."
            if self.creditor_released
            else f"Authenticated reply from {self.creditor}: {self.commitment_id} is still owed; I did NOT release it."
        )
        return {
            "commitment_id": self.commitment_id,
            "creditor": self.creditor,
            "status": status_label,
            "reply": detail,
        }

    def post_execute(self, seat: str, body: dict) -> dict:
        assert seat == "p1"
        self._require_commitment_only(body)
        if self.terminal_action is not None:
            raise Reject(409, "commitment_already_settled", settled_action=self.terminal_action)
        self.terminal_action = "execute"
        return {
            "commitment_id": self.commitment_id,
            "target_id": self.target_id,
            "creditor": self.creditor,
            "action": "execute",
            "ready_at_action": self.condition_ready,
            "polls_at_action": self.polls,
        }

    def post_drop(self, seat: str, body: dict) -> dict:
        assert seat == "p1"
        cid = body.get("commitment_id")
        reason = body.get("reason")
        if set(body) != {"commitment_id", "reason"} or not isinstance(cid, str) or not isinstance(reason, str):
            raise Reject(400, 'send JSON {"commitment_id": "...", "reason": "..."} and nothing else')
        if cid != self.commitment_id:
            raise Reject(400, "unknown_commitment_id", commitment_id=cid[:64])
        trimmed = reason.strip()
        if not 1 <= len(trimmed) <= MAX_TEXT_LEN or not _clean_str(reason):
            raise Reject(400, "reason must be 1 to 500 clean characters")
        if self.terminal_action is not None:
            raise Reject(409, "commitment_already_settled", settled_action=self.terminal_action)
        self.terminal_action = "drop"
        return {
            "commitment_id": self.commitment_id,
            "target_id": self.target_id,
            "creditor": self.creditor,
            "action": "drop",
            "reason": trimmed,
            "ready_at_action": self.condition_ready,
            "polls_at_action": self.polls,
        }


def _read_post_body(handler: BaseHTTPRequestHandler) -> object:
    raw_len = handler.headers.get("Content-Length")
    if raw_len is None or not raw_len.strip().isascii() or not raw_len.strip().isdigit():
        return _BAD_JSON
    length = int(raw_len.strip())
    if length <= 0 or length > MAX_BODY:
        return _BAD_JSON
    raw = handler.rfile.read(length)
    if len(raw) != length:
        return _BAD_JSON
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _BAD_JSON
    if not _bracket_depth_ok(text, MAX_JSON_DEPTH):
        return _BAD_JSON
    try:
        parsed = json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_reject_nonfinite)
    except (ValueError, RecursionError):
        return _BAD_JSON
    return parsed if _json_safe(parsed) else _BAD_JSON


def serve(world: Standing, port: int = PORT) -> None:
    class Handler(BaseHTTPRequestHandler):
        timeout = 30

        def _answer(self, method: str) -> None:
            body = _read_post_body(self) if method == "POST" else None
            auth = self.headers.get("Authorization") or ""
            token = auth[7:].strip() if auth[:7].lower() == "bearer " else None
            status, response = world.handle(
                method, self.path.split("?")[0], token, body, self.client_address[0]
            )
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

        def log_message(self, *args: object) -> None:
            pass

    ThreadingHTTPServer(("::", port), Handler).serve_forever()


if __name__ == "__main__":
    serve(Standing(json.loads(INSTANCE.read_text(encoding="utf-8"))))
