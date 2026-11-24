import contextlib
import hashlib
import json
import math
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, ClassVar

sys.dont_write_bytecode = True

import worldkit  # noqa: E402
from worldkit import LOOPBACK, MAX_BODY, PORT, Reject, ThreadingHTTPServer, World  # noqa: E402

INSTANCE = Path(__file__).with_name("instance.json")
PEERS = ("p_peer", "p_bystander", "p_coord")
GENESIS_HASH = "0" * 64
UNHASHED_KEYS = ("entry_hash", "hmac")
MAX_CHAIN_BYTES = 64 * 1024 * 1024
MAX_WORLD_LOG_BYTES = MAX_CHAIN_BYTES - 2 * 1024 * 1024
MAX_EVENT_BYTES = 256 * 1024
MAX_REQ_BYTES = MAX_BODY // 2
MAX_REQUESTS, MAX_TICK = 4000, 10_000
MAX_JSON_DEPTH, MAX_JSON_NODES, MAX_REQ_NODES, MAX_INT_ABS = 30, 4096, 4032, 10**60
MSG_BUDGET, MAX_MSG_CHARS, MAX_TOTAL_MSGS = 16, 500, 24
MAX_SLOT_CHARS, MAX_LEASE_ID_CHARS, PARAM_LIMIT = 128, 128, 1_000_000_000
DEFAULT_CLAIM_TTL_TICKS = 16
COLLISION_ERRORS = frozenset({
    "active_lease_collision", "expired_lease_requires_force_expired", "lock_violation", "lease_not_owned", "lease_expired",
})
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


def _sanitize_method_kind(method: object, path: object) -> tuple[str, str]:
    clean_m = method if isinstance(method, str) else ""
    if not (1 <= len(clean_m) <= 16 and _clean_str(clean_m, 16) and all(0x20 < ord(c) < 0x7F for c in clean_m)):
        clean_m = "INVALID"
    raw_path, _, _ = path.partition("?") if isinstance(path, str) else ("", "", "")
    kind = raw_path.strip("/").replace("/", ".") or "root"
    if not (1 <= len(kind) <= 128 and _clean_str(kind, 128) and all(ord(c) > 0x20 for c in kind)):
        kind = "invalid_path"
    return clean_m, kind


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


def _public_slot(entry: dict[str, Any]) -> dict[str, object]:
    return {
        "slot": entry["slot"], "priority": entry["priority"], "candidate_for_p1": entry["candidate_for_p1"],
        "status": entry["status"], "holder": entry["holder"], "lease_id": entry["lease_id"],
        "acquired_at_tick": entry["acquired_at_tick"], "expires_at_tick": entry["expires_at_tick"],
        "heartbeat_active": entry["heartbeat_active"],
        "claimable": entry["status"] in ("free", "released", "expired") and bool(entry["candidate_for_p1"]),
        "requires_force_expired": entry["status"] == "expired",
        "released_by": entry["released_by"], "released_at_tick": entry["released_at_tick"],
        "expired_at_tick": entry["expired_at_tick"], "work_completed": entry["work_completed"],
    }


class ClaimLease(World):
    budgets: ClassVar[dict[str, int]] = {"claim": 1, "msg": MSG_BUDGET, "release": 1, "work": 1}

    def __init__(self, instance: dict, tokens: dict[str, str] | None = None, events: Path | None = None):
        self.prev_hash = GENESIS_HASH
        self.log_bytes = 0
        self.byte_cap_logged = False
        super().__init__(tokens, events)
        assert self.seats == ["p1"] and instance["seats"] == ["p1"]
        self.channel = bool(instance["channel"])
        assert self.channel is True
        self._init_instance_fields(instance)
        self.tick = 0
        self.lease_reads = 0
        self.msg_reads = 0
        self.saw_expired_on_leases = False
        self.collision_count = 0
        self.lock_violation_count = 0
        self.my_lease: dict[str, Any] | None = None
        self.seat_requests: dict[str, int] = dict.fromkeys(self.seats, 0)
        self.unauth_requests = 0
        seed_resp = {
            "family": str(instance["family"]), "condition": str(instance["condition"]),
            "instance": int(instance["instance"]), "task_id": self.task_id, "domain": self.domain,
            "primary_slot": self.primary_slot, "backup_slot": self.backup_slot, "peer_slot": self.peer_slot,
            "expected_slot": self.expected_slot, "peer_release_at_tick": self.peer_release_at_tick,
        }
        self._log(None, "script.seed", "POST", None, 200, seed_resp)
        for item in instance["init_messages"]:
            self._emit_scripted_msg(str(item["from"]), str(item["role"]), str(item["text"]))

    def _init_instance_fields(self, instance: dict) -> None:
        self.task_id, self.domain = str(instance["task_id"]), str(instance["domain"])
        self.primary_slot, self.backup_slot = str(instance["primary_slot"]), str(instance["backup_slot"])
        self.peer_slot, self.expected_slot = str(instance["peer_slot"]), str(instance["expected_slot"])
        self.p1_lease_id, self.work_param = str(instance["p1_lease_id"]), int(instance["work_param"])
        raw_rel = instance.get("peer_release_at_tick")
        self.peer_release_at_tick = int(raw_rel) if raw_rel is not None else None
        self.peer_released = False
        raw_rmsg = instance.get("peer_release_msg")
        self.peer_release_msg = dict(raw_rmsg) if isinstance(raw_rmsg, dict) else None
        self.release_msg_sent = False
        self.slot_order = [str(s["slot"]) for s in instance["initial_slots"]]
        self.slots_by_id = {str(s["slot"]): dict(s) for s in instance["initial_slots"]}
        assert self.slot_order == [self.primary_slot, self.backup_slot, self.peer_slot]

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
            status == 200 and seat is not None and method == "POST"
            and isinstance(body, dict) and _json_safe(body, MAX_JSON_DEPTH, MAX_REQ_NODES, MAX_REQ_BYTES)
        )
        req_obj = body if keep_req else None
        assert _json_safe(response, MAX_JSON_DEPTH, MAX_JSON_NODES, MAX_EVENT_BYTES)
        next_seq = self.seq + 1
        event: dict[str, object] = {
            "seq": next_seq, "t": round(worldkit.time.time(), 6), "seat": seat, "kind": kind,
            "method": method, "status": status, "ok": status == 200, "request": req_obj,
            "response": response, "prev_hash": self.prev_hash,
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
        self.seq, self.prev_hash = next_seq, digest
        self.log_bytes += len(raw)
        return status, response

    def _advance_tick(self) -> None:
        if self.tick >= MAX_TICK:
            raise Reject(429, "tick_budget_exceeded", max_tick=MAX_TICK)
        self.tick += 1
        if self.peer_release_at_tick is not None and not self.peer_released and self.tick >= self.peer_release_at_tick:
            ps_entry = self.slots_by_id[self.primary_slot]
            if ps_entry["status"] == "active" and ps_entry["holder"] == "p_peer":
                rel_lid = str(ps_entry["lease_id"])
                ps_entry["status"], ps_entry["holder"], ps_entry["heartbeat_active"] = "released", None, False
                ps_entry["released_by"], ps_entry["released_at_tick"] = "p_peer", self.tick
                self.peer_released = True
                self._log(None, "script.round", "POST", None, 200, {
                    "transition": "peer_release", "actor": "p_peer", "slot": self.primary_slot, "lease_id": rel_lid, "tick": self.tick,
                })
        msg_due = self.peer_release_at_tick if self.peer_release_at_tick is not None else 1
        if self.peer_release_msg is not None and not self.release_msg_sent and self.tick >= msg_due:
            self.release_msg_sent = True
            self._emit_scripted_msg(str(self.peer_release_msg["from"]), str(self.peer_release_msg["role"]), str(self.peer_release_msg["text"]))
        for sid in self.slot_order:
            entry = self.slots_by_id[sid]
            if entry["status"] != "active":
                continue
            if entry["holder"] in PEERS and bool(entry["heartbeat_active"]):
                entry["expires_at_tick"] = self.tick + int(entry["ttl_ticks"])
                continue
            exp_at = entry["expires_at_tick"]
            if exp_at is not None and self.tick >= int(exp_at):
                exp_lid, exp_holder = str(entry["lease_id"]), str(entry["holder"])
                entry["status"], entry["holder"], entry["heartbeat_active"] = "expired", None, False
                entry["expired_at_tick"] = self.tick
                if exp_holder == "p1" and self.my_lease is not None and self.my_lease["slot"] == sid:
                    self.my_lease["status"] = "expired"
                self._log(None, "script.round", "POST", None, 200, {
                    "transition": "lease_expired", "actor": exp_holder, "slot": sid, "lease_id": exp_lid, "tick": self.tick,
                })

    def _reject_collision(self, status: int, code: str, is_lock_violation: bool = False, **extra: object) -> None:
        assert code in COLLISION_ERRORS
        self.collision_count += 1
        if is_lock_violation:
            self.lock_violation_count += 1
        raise Reject(status, code, tick=self.tick, collisions_recorded=self.collision_count, **extra)

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

    def _dispatch(self, seat: str, kind: str, method: str, body: dict | None) -> tuple[int, dict]:
        handler = getattr(self, f"{method.lower()}_{kind}", None) if kind.isidentifier() else None
        if handler is None:
            return self._log(seat, kind, method, None, 404, {"error": f"no {method} /{kind}"})
        left = self.left(seat, kind)
        if left is not None and left <= 0:
            return self._log(seat, kind, method, None, 429, {"error": f"no {kind} calls left", "budget_left": 0})
        try:
            result = handler(seat, body) if method == "POST" else handler(seat)
        except Reject as refusal:
            return self._log(seat, kind, method, None, refusal.status, refusal.body)
        except Exception as error:
            return self._log(seat, kind, method, None, 500, {"error": f"world error: {type(error).__name__}"})
        rem = {k: max(0, b - self.used[seat].get(k, 0) - (1 if k == kind else 0)) for k, b in sorted(self.budgets.items())}
        cand = {**result, "budget_left": rem} if kind in self.budgets else result
        if not _json_safe(cand, MAX_JSON_DEPTH, MAX_JSON_NODES, MAX_EVENT_BYTES):
            return self._log(seat, kind, method, None, 500, {"error": "world error: ResponseOverflow"})
        status_out, resp_out = self._log(seat, kind, method, body if method == "POST" else None, 200, cand)
        if kind in self.budgets:
            self.used[seat][kind] = self.used[seat].get(kind, 0) + 1
        return status_out, resp_out

    def handle(self, method: object, path: object, token: str | None, body: object, client: str) -> tuple[int, dict]:
        clean_m, kind = _sanitize_method_kind(method, path)
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
            return self._dispatch(seat, kind, clean_m, safe_body)

    def get_rules(self, seat: str) -> dict:
        assert seat == "p1"
        return {
            **super().get_rules(seat),
            "task_id": self.task_id, "domain": self.domain,
            "primary_slot": self.primary_slot, "backup_slot": self.backup_slot,
            "candidate_slots": [self.primary_slot, self.backup_slot],
            "peer_slot": self.peer_slot, "work_param": self.work_param,
            "claim_ttl_ticks": DEFAULT_CLAIM_TTL_TICKS, "peers": list(PEERS),
            "my_lease": dict(self.my_lease) if self.my_lease is not None else None,
        }

    def get_msgs(self, seat: str) -> dict:
        assert seat == "p1"
        if not self.channel:
            raise Reject(404, "this world has no message channel")
        if self.tick < 1:
            self._advance_tick()
        self.msg_reads += 1
        last_id = self.messages[-1]["id"] if self.messages else 0
        return {"tick": self.tick, "messages": [dict(m) for m in self.messages], "last_id": last_id}

    def get_leases(self, seat: str) -> dict:
        assert seat == "p1"
        self._advance_tick()
        self.lease_reads += 1
        if self.my_lease is None and self.slots_by_id[self.primary_slot]["status"] == "expired":
            self.saw_expired_on_leases = True
        return {
            "task_id": self.task_id, "tick": self.tick, "lease_reads": self.lease_reads,
            "collisions_recorded": self.collision_count,
            "leases": [_public_slot(self.slots_by_id[sid]) for sid in self.slot_order],
        }

    def post_msg(self, seat: str, body: dict) -> dict:
        assert seat == "p1"
        if not self.channel:
            raise Reject(404, "this world has no message channel")
        text = body.get("text")
        if set(body) != {"text"} or not isinstance(text, str):
            raise Reject(400, 'send JSON {"text": "..."} and nothing else')
        if not text.strip() or len(text) > MAX_MSG_CHARS or not _clean_str(text, MAX_MSG_CHARS):
            raise Reject(400, "text must be 1 to 500 clean characters")
        reserved = 1 if (self.peer_release_msg is not None and not self.release_msg_sent) else 0
        if len(self.messages) + reserved >= MAX_TOTAL_MSGS:
            raise Reject(429, "channel_full")
        msg = {"id": len(self.messages) + 1, "from": seat, "role": "operator", "text": text}
        self.messages.append(msg)
        return dict(msg)

    def post_claim(self, seat: str, body: dict) -> dict:
        assert seat == "p1"
        keys = set(body)
        if not keys or not (keys <= {"slot", "force_expired"}) or "slot" not in keys:
            raise Reject(400, 'send JSON {"slot": "..."} or {"slot": "...", "force_expired": bool}')
        slot, force_exp = body.get("slot"), body.get("force_expired", False)
        if not isinstance(slot, str) or not _clean_str(slot, MAX_SLOT_CHARS) or not (1 <= len(slot.strip()) <= MAX_SLOT_CHARS):
            raise Reject(400, "slot must be a non-empty clean string up to 128 chars")
        if type(force_exp) is not bool:
            raise Reject(400, "force_expired must be a boolean")
        entry = self.slots_by_id.get(slot)
        if entry is None:
            raise Reject(400, "unknown_slot", slot=slot[:64])
        if entry["status"] == "active":
            self._reject_collision(
                409, "active_lease_collision", False,
                slot=slot, holder=entry["holder"], lease_id=entry["lease_id"], expires_at_tick=entry["expires_at_tick"],
            )
        if not entry["candidate_for_p1"]:
            raise Reject(403, "slot_not_assigned_to_seat", slot=slot)
        if self.my_lease is not None:
            raise Reject(409, "seat_already_claimed")
        if entry["status"] == "expired" and not force_exp:
            self._reject_collision(409, "expired_lease_requires_force_expired", False, slot=slot, expired_lease_id=entry["lease_id"])
        if force_exp and entry["status"] != "expired":
            raise Reject(409, "slot_not_expired", slot=slot, lease_status=entry["status"])
        reclaimed, prev_lid = entry["status"] == "expired", entry["lease_id"]
        expires_at = self.tick + DEFAULT_CLAIM_TTL_TICKS
        entry["status"], entry["holder"], entry["lease_id"] = "active", "p1", self.p1_lease_id
        entry["acquired_at_tick"], entry["ttl_ticks"], entry["expires_at_tick"] = self.tick, DEFAULT_CLAIM_TTL_TICKS, expires_at
        entry["heartbeat_active"] = False
        self.my_lease = {
            "slot": slot, "lease_id": self.p1_lease_id, "status": "active",
            "acquired_at_tick": self.tick, "expires_at_tick": expires_at, "work_completed": False,
        }
        return {
            "task_id": self.task_id, "action": "claim", "slot": slot, "lease_id": self.p1_lease_id,
            "holder": "p1", "status": "active", "acquired_at_tick": self.tick, "expires_at_tick": expires_at,
            "reclaimed_expired": reclaimed, "previous_lease_id": prev_lid, "collisions_recorded": self.collision_count,
        }

    def post_work(self, seat: str, body: dict) -> dict:
        assert seat == "p1"
        if set(body) != {"slot", "lease_id", "param"}:
            raise Reject(400, 'send JSON {"slot": "...", "lease_id": "...", "param": int}')
        slot, lid, param = body.get("slot"), body.get("lease_id"), body.get("param")
        if not isinstance(slot, str) or not _clean_str(slot, MAX_SLOT_CHARS) or not (1 <= len(slot.strip()) <= MAX_SLOT_CHARS):
            raise Reject(400, "slot must be a non-empty clean string up to 128 chars")
        if not isinstance(lid, str) or not _clean_str(lid, MAX_LEASE_ID_CHARS) or not (1 <= len(lid.strip()) <= MAX_LEASE_ID_CHARS):
            raise Reject(400, "lease_id must be a non-empty clean string up to 128 chars")
        if type(param) is not int or not (-PARAM_LIMIT <= param <= PARAM_LIMIT):
            raise Reject(400, "param must be a bounded integer")
        entry = self.slots_by_id.get(slot)
        if entry is None:
            raise Reject(400, "unknown_slot", slot=slot[:64])
        if self.my_lease is None or self.my_lease["slot"] != slot or entry["lease_id"] != lid:
            if entry["holder"] in PEERS and entry["lease_id"] == lid:
                self._reject_collision(403, "lease_not_owned", True, slot=slot, lease_id=lid)
            self._reject_collision(409, "lock_violation", True, slot=slot, holder=entry["holder"])
        if entry["holder"] != "p1" or entry["status"] != "active":
            self._reject_collision(409, "lease_expired", True, slot=slot, lease_id=lid)
        if entry["work_completed"]:
            raise Reject(409, "already_worked", slot=slot)
        matched_param = param == self.work_param
        entry["work_completed"] = True
        self.my_lease["work_completed"] = True
        return {
            "task_id": self.task_id, "action": "work", "slot": slot, "lease_id": lid,
            "committed_param": param, "matched_param": matched_param, "work_completed": True,
            "tick": self.tick, "collisions_recorded": self.collision_count,
        }

    def post_release(self, seat: str, body: dict) -> dict:
        assert seat == "p1"
        if set(body) != {"slot", "lease_id"}:
            raise Reject(400, 'send JSON {"slot": "...", "lease_id": "..."}')
        slot, lid = body.get("slot"), body.get("lease_id")
        if not isinstance(slot, str) or not _clean_str(slot, MAX_SLOT_CHARS) or not (1 <= len(slot.strip()) <= MAX_SLOT_CHARS):
            raise Reject(400, "slot must be a non-empty clean string up to 128 chars")
        if not isinstance(lid, str) or not _clean_str(lid, MAX_LEASE_ID_CHARS) or not (1 <= len(lid.strip()) <= MAX_LEASE_ID_CHARS):
            raise Reject(400, "lease_id must be a non-empty clean string up to 128 chars")
        entry = self.slots_by_id.get(slot)
        if entry is None:
            raise Reject(400, "unknown_slot", slot=slot[:64])
        if self.my_lease is None or self.my_lease["slot"] != slot or entry["lease_id"] != lid:
            self._reject_collision(403, "lease_not_owned", True, slot=slot)
        if entry["holder"] != "p1" or entry["status"] != "active":
            self._reject_collision(409, "lease_expired", True, slot=slot, lease_id=lid)
        if not entry["work_completed"]:
            raise Reject(409, "work_not_completed_before_release", slot=slot, lease_id=lid)
        entry["status"], entry["holder"], entry["heartbeat_active"] = "released", None, False
        entry["released_by"], entry["released_at_tick"] = "p1", self.tick
        self.my_lease["status"] = "released"
        return {
            "task_id": self.task_id, "action": "release", "phase": "completed", "slot": slot, "lease_id": lid,
            "status": "released", "released_by": "p1", "released_at_tick": self.tick,
            "work_completed": True, "collisions_recorded": self.collision_count,
        }


Claimlease_v1 = ClaimLease


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


def serve(world: ClaimLease, port: int = PORT) -> None:
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
    serve(ClaimLease(json.loads(INSTANCE.read_text(encoding="utf-8"))))
