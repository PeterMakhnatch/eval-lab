from __future__ import annotations

import contextlib
import json
import math
import os
import secrets
import socket
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer as _StockServer
from pathlib import Path
from typing import Any, ClassVar

import codec

sys.dont_write_bytecode = True

EVENTS = Path(os.environ.get("WORLD_EVENTS", "/logs/world/events.jsonl"))
REWARD = Path(os.environ.get("WORLD_REWARD", "/logs/verifier/reward.json"))
PORT, MAX_BODY = 8080, codec.MAX_BODY_BYTES
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
REMOTE_CLIENT = "10.0.0.2"


class ThreadingHTTPServer(_StockServer):
    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


class Reject(Exception):
    def __init__(self, status: int, error: str, **extra: Any) -> None:
        super().__init__(error)
        assert 400 <= status < 600
        self.status, self.body = status, {"error": error, **extra}


def _resolve_events_path(events: Path | None) -> Path | None:
    if events is not None or "WORLD_EVENTS" in os.environ:
        return Path(events) if events is not None else Path(os.environ["WORLD_EVENTS"])
    return EVENTS if (EVENTS.parent.exists() or os.access("/", os.W_OK)) else None


def _build_token_maps(
    tokens: dict[str, str] | None, seats: list[str] | tuple[str, ...] | None
) -> tuple[dict[str, str], list[str], dict[str, str]]:
    raw = tokens if tokens is not None else ({f"mem_token_{s}": str(s) for s in seats} if seats is not None else json.loads(os.environ["SEAT_TOKENS"]))
    tok_map = {str(k): str(v) for k, v in raw.items()}
    seat_list = sorted(set(tok_map.values()))
    assert seat_list and all(codec.clean_str(k, 256) and codec.clean_str(v, 64) for k, v in tok_map.items())
    seat_to_tok: dict[str, str] = {}
    for tok, s_name in sorted(tok_map.items()):
        seat_to_tok.setdefault(s_name, tok)
    return tok_map, seat_list, seat_to_tok


class World:
    channel = direct_messages = notices_enabled = inbox_enabled = leases_enabled = claim_locks = barrier_enabled = False
    budgets: ClassVar[dict[str, int]] = {}
    unread_guard_kinds: tuple[str, ...] = ()
    max_messages, max_msg_chars, max_msg_bytes, max_board_bytes = 256, 2000, 4096, 262_144
    max_dms, max_dm_chars, max_dm_bytes, max_notices = 256, 2000, 262_144, 256
    lease_expiry_ticks, max_total_leases, max_barrier_phases = 16, 1024, 256
    max_leases_per_seat: int | None = None
    barrier_quorum: int | None = None

    def __init__(
        self, tokens: dict[str, str] | None = None, events: Path | None = None, *,
        seats: list[str] | tuple[str, ...] | None = None, channel: bool | None = None,
        direct_messages: bool | None = None, notices_enabled: bool | None = None,
        inbox_enabled: bool | None = None, leases_enabled: bool | None = None,
        barrier_enabled: bool | None = None, unread_guard_kinds: tuple[str, ...] | list[str] | None = None,
        lease_expiry_ticks: int | None = None, barrier_quorum: int | None = None,
        max_log_bytes: int | None = None, clock: Callable[[], float] | None = None,
    ) -> None:
        self.tokens, self.seats, self._seat_to_token = _build_token_maps(tokens, seats)
        self._token_pairs = tuple((tok.encode("utf-8"), seat) for tok, seat in sorted(self.tokens.items()))
        for attr, val in (("channel", channel), ("direct_messages", direct_messages), ("notices_enabled", notices_enabled), ("inbox_enabled", inbox_enabled), ("leases_enabled", leases_enabled), ("barrier_enabled", barrier_enabled)):
            if val is not None:
                setattr(self, attr, bool(val))
        if unread_guard_kinds is not None:
            self.unread_guard_kinds = tuple(unread_guard_kinds)
        if lease_expiry_ticks is not None:
            assert lease_expiry_ticks >= 1
            self.lease_expiry_ticks = int(lease_expiry_ticks)
        if barrier_quorum is not None:
            assert barrier_quorum >= 1
            self.barrier_quorum = int(barrier_quorum)
        self.max_log_bytes = int(max_log_bytes) if max_log_bytes is not None else codec.MAX_WORLD_LOG_BYTES
        self._clock, self.lock = clock or time.time, threading.Lock()
        self.seq, self.logical_tick, self.state_version, self.epoch = 0, 0, 1, 1
        self.prev_hash, self.closed, self.log_intact, self.log_bytes = codec.GENESIS_HASH, False, True, 0
        self.spoof_attempts, self.board_bytes, self.dm_bytes, self.lease_seq, self.barrier_phase = 0, 0, 0, 0, 1
        self.events: list[dict[str, Any]] = []
        self.handlers: dict[tuple[str, str], Callable[..., dict[str, Any]]] = {}
        self._current_query: dict[str, str] = {}
        self.used: dict[str, dict[str, int]] = {s: {} for s in self.seats}
        self.messages, self.dms, self.notices, self.barrier_history = [], [], [], []
        self.read_cursor: dict[str, int] = dict.fromkeys(self.seats, 0)
        self.dm_cursor: dict[str, int] = dict.fromkeys(self.seats, 0)
        self.notice_cursor: dict[str, int] = dict.fromkeys(self.seats, 0)
        self.active_leases, self.barrier_arrivals = {}, {}
        self.events_path = _resolve_events_path(events)
        if self.events_path is not None:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            self.events_path.write_bytes(b"")

    def _require_on(self, enabled: bool, code: str) -> None:
        if not enabled:
            raise Reject(404, code)

    def route(self, method: str, path_or_kind: str, handler: Callable[..., dict[str, Any]]) -> None:
        m = method.upper()
        assert m in ("GET", "POST")
        self.handlers[(m, path_or_kind.strip("/").replace("/", ".") or "root")] = handler

    def query_param(self, key: str, default: str | None = None) -> str | None:
        return self._current_query.get(key, default)

    def query_int(self, key: str, default: int = 0) -> int:
        raw = self._current_query.get(key)
        return int(raw) if (raw is not None and raw.isascii() and raw.isdigit() and len(raw) <= 12) else default

    def _auth_seat(self, token: str | None) -> str | None:
        if not isinstance(token, str) or not token or len(token) > 256:
            return None
        cand, matched = token.encode("utf-8"), None
        for expected_bytes, seat_name in self._token_pairs:
            if secrets.compare_digest(cand, expected_bytes):
                matched = seat_name
        return matched

    def left(self, seat: str, kind: str) -> int | None:
        return (self.budgets[kind] - self.used[seat].get(kind, 0)) if kind in self.budgets else None

    def budget_snapshot(self, seat: str) -> dict[str, int]:
        return {k: max(0, self.budgets[k] - self.used[seat].get(k, 0)) for k in sorted(self.budgets)}

    def unread_messages(self, seat: str) -> list[dict[str, Any]]:
        cur = self.read_cursor[seat]
        return [dict(m) for m in self.messages if m["id"] > cur and m["from"] != seat]

    def bump_version(self) -> int:
        self.state_version += 1
        return self.state_version

    def check_version(self, expected_version: Any) -> None:
        ver = codec.strict_int(expected_version)
        if ver is None:
            raise Reject(400, "invalid_version")
        if ver != self.state_version:
            raise Reject(409, "stale_version", current_version=self.state_version)

    require_version = check_version

    def add_notice(
        self, text: str, *, kind: str = "system", from_actor: str = "system", extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        clean_t = codec.clean_str(text, 2000)
        assert clean_t is not None and len(self.notices) < self.max_notices
        notice: dict[str, Any] = {"id": len(self.notices) + 1, "from": from_actor, "kind": kind, "text": clean_t, "epoch": self.epoch, "state_version": self.state_version, "logical_tick": self.logical_tick}
        if extra:
            notice.update(codec.json_safe(extra))
        self.notices.append(notice)
        return dict(notice)

    def advance_epoch(self, notice_text: str | None = None, *, extra: dict[str, Any] | None = None) -> int:
        self.epoch += 1
        self.state_version += 1
        if notice_text is not None:
            self.add_notice(notice_text, kind="epoch", extra=extra)
        return self.epoch

    def get_rules(self, seat: str) -> dict[str, Any]:
        rules: dict[str, Any] = {
            "you": seat, "seats": list(self.seats), "channel": self.channel, "direct_messages": self.direct_messages,
            "notices": self.notices_enabled, "leases": bool(self.leases_enabled or self.claim_locks),
            "barrier": self.barrier_enabled, "epoch": self.epoch, "state_version": self.state_version,
            "logical_tick": self.logical_tick, "budget_left": self.budget_snapshot(seat),
        }
        if self.unread_guard_kinds:
            rules["unread_guard"], rules["unread_count"] = list(self.unread_guard_kinds), len(self.unread_messages(seat))
        if self.leases_enabled or self.claim_locks:
            rules["expiry_ticks"] = self.lease_expiry_ticks
        if self.barrier_enabled:
            rules["barrier_quorum"] = self.barrier_quorum or len(self.seats)
        return rules

    def _clean_text_payload(self, body: dict[str, Any], max_chars: int, max_bytes: int) -> tuple[str, int]:
        text = codec.clean_str(body.get("text"), max_chars)
        if text is None:
            raise Reject(400, "invalid_text")
        raw_len = len(text.encode("utf-8"))
        if raw_len > max_bytes:
            raise Reject(400, "message_too_large")
        return text, raw_len

    def post_msg(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_on(self.channel, "channel_disabled")
        text, raw_len = self._clean_text_payload(body, self.max_msg_chars, self.max_msg_bytes)
        if len(self.messages) >= self.max_messages or self.board_bytes + raw_len > self.max_board_bytes:
            raise Reject(429, "channel_full")
        msg = {"id": len(self.messages) + 1, "from": seat, "text": text, "epoch": self.epoch, "logical_tick": self.logical_tick}
        self.messages.append(msg)
        self.board_bytes += raw_len
        return dict(msg)

    def get_msgs(self, seat: str) -> dict[str, Any]:
        self._require_on(self.channel, "channel_disabled")
        since = self.query_int("since", 0)
        if self.messages:
            self.read_cursor[seat] = max(self.read_cursor[seat], self.messages[-1]["id"])
        return {"messages": [dict(m) for m in self.messages if m["id"] > since], "read_cursor": self.read_cursor[seat], "unread_count": 0, "logical_tick": self.logical_tick}

    def get_new(self, seat: str) -> dict[str, Any]:
        self._require_on(self.channel, "channel_disabled")
        prev = self.read_cursor[seat]
        items = [dict(m) for m in self.messages if m["id"] > prev]
        self.read_cursor[seat] = max(prev, self.messages[-1]["id"] if self.messages else prev)
        return {"messages": items, "previous_cursor": prev, "read_cursor": self.read_cursor[seat], "unread_count": 0, "logical_tick": self.logical_tick}

    def post_dm(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_on(self.direct_messages, "dms_disabled")
        to_seat = codec.clean_str(body.get("to"), 64)
        if to_seat is None or to_seat not in self.seats or to_seat == seat:
            raise Reject(400, "invalid_recipient")
        text, raw_len = self._clean_text_payload(body, self.max_dm_chars, self.max_dm_bytes)
        if len(self.dms) >= self.max_dms or self.dm_bytes + raw_len > self.max_dm_bytes:
            raise Reject(429, "dm_channel_full")
        dm = {"id": len(self.dms) + 1, "from": seat, "to": to_seat, "text": text, "epoch": self.epoch, "logical_tick": self.logical_tick}
        self.dms.append(dm)
        self.dm_bytes += raw_len
        return dict(dm)

    def get_dms(self, seat: str) -> dict[str, Any]:
        self._require_on(self.direct_messages, "dms_disabled")
        since = self.query_int("since", 0)
        incoming = [dict(d) for d in self.dms if d["to"] == seat and d["id"] > since]
        if all_to_me := [d["id"] for d in self.dms if d["to"] == seat]:
            self.dm_cursor[seat] = max(self.dm_cursor[seat], all_to_me[-1])
        return {"dms": incoming, "dm_cursor": self.dm_cursor[seat], "unread_dms": 0, "logical_tick": self.logical_tick}

    def get_notices(self, seat: str) -> dict[str, Any]:
        self._require_on(self.notices_enabled, "notices_disabled")
        since = self.query_int("since", 0)
        if self.notices:
            self.notice_cursor[seat] = max(self.notice_cursor[seat], self.notices[-1]["id"])
        return {"notices": [dict(n) for n in self.notices if n["id"] > since], "epoch": self.epoch, "state_version": self.state_version, "notice_cursor": self.notice_cursor[seat], "logical_tick": self.logical_tick}

    def get_inbox(self, seat: str) -> dict[str, Any]:
        self._require_on(bool(self.inbox_enabled or self.notices_enabled or self.channel or self.direct_messages), "inbox_disabled")
        board_new = [dict(m) for m in self.messages if m["id"] > self.read_cursor[seat]] if self.channel else []
        if self.channel and self.messages:
            self.read_cursor[seat] = max(self.read_cursor[seat], self.messages[-1]["id"])
        dm_new = [dict(d) for d in self.dms if d["to"] == seat and d["id"] > self.dm_cursor[seat]] if self.direct_messages else []
        if self.direct_messages and (my_dms := [d["id"] for d in self.dms if d["to"] == seat]):
            self.dm_cursor[seat] = max(self.dm_cursor[seat], my_dms[-1])
        not_new = [dict(n) for n in self.notices if n["id"] > self.notice_cursor[seat]] if self.notices_enabled else []
        if self.notices_enabled and self.notices:
            self.notice_cursor[seat] = max(self.notice_cursor[seat], self.notices[-1]["id"])
        return {"you": seat, "epoch": self.epoch, "state_version": self.state_version, "logical_tick": self.logical_tick, "messages": board_new, "dms": dm_new, "notices": not_new, "cursors": {"board": self.read_cursor[seat], "dm": self.dm_cursor[seat], "notice": self.notice_cursor[seat]}}

    def valid_lease_target(self, target: str) -> bool:
        return True

    def _parse_lease_target(self, body: dict[str, Any]) -> str:
        bx = codec.strict_int(body.get("box")) if "box" in body else None
        raw = body.get("target") if "target" in body else (body.get("slot") if "slot" in body else (str(bx) if bx is not None else None))
        target = codec.clean_str(raw, 128)
        if target is None or not self.valid_lease_target(target):
            raise Reject(400, "invalid_target")
        return target

    def _expire_leases(self) -> None:
        for tgt in [t for t, e in self.active_leases.items() if e["expires_tick"] is not None and self.logical_tick >= e["expires_tick"]]:
            del self.active_leases[tgt]

    def lease_holder(self, target: str) -> str | None:
        entry = self.active_leases.get(target)
        return str(entry["holder"]) if entry is not None else None

    def require_lease(self, seat: str, target: str, lease_id: str | None = None) -> dict[str, Any]:
        entry = self.active_leases.get(target)
        if entry is None or entry["holder"] != seat:
            raise Reject(409, "lease_required" if entry is None else "already_claimed", target=target, holder=entry["holder"] if entry else None)
        if lease_id is not None and entry["lease_id"] != lease_id:
            raise Reject(409, "lease_id_mismatch", target=target)
        return dict(entry)

    def post_claim(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_on(bool(self.leases_enabled or self.claim_locks), "leases_disabled")
        target, ttl = self._parse_lease_target(body), self.lease_expiry_ticks
        if "ttl_ticks" in body:
            req_ttl = codec.strict_int(body.get("ttl_ticks"))
            if req_ttl is None or not (1 <= req_ttl <= self.lease_expiry_ticks):
                raise Reject(400, "invalid_ttl_ticks")
            ttl = req_ttl
        if (existing := self.active_leases.get(target)) is not None:
            if existing["holder"] != seat:
                raise Reject(409, "already_claimed", target=target, holder=existing["holder"], expires_tick=existing["expires_tick"])
            existing["expires_tick"], existing["expiry_ticks"] = self.logical_tick + ttl, ttl
            return dict(existing)
        mine = sum(1 for item in self.active_leases.values() if item["holder"] == seat)
        if self.max_leases_per_seat is not None and mine >= self.max_leases_per_seat:
            raise Reject(409, "lease_limit", limit=self.max_leases_per_seat)
        if len(self.active_leases) >= self.max_total_leases:
            raise Reject(429, "lease_table_full")
        self.lease_seq += 1
        lease = {"target": target, "holder": seat, "lease_id": f"lease-{self.lease_seq:04d}", "acquired_tick": self.logical_tick, "expires_tick": self.logical_tick + ttl, "expiry_ticks": ttl}
        self.active_leases[target] = lease
        return dict(lease)

    def post_release(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_on(bool(self.leases_enabled or self.claim_locks), "leases_disabled")
        target = self._parse_lease_target(body)
        existing = self.active_leases.get(target)
        if existing is None:
            raise Reject(409, "not_claimed", target=target)
        if existing["holder"] != seat:
            raise Reject(403, "lease_not_owned", target=target, holder=existing["holder"])
        if "lease_id" in body and body.get("lease_id") != existing["lease_id"]:
            raise Reject(409, "lease_id_mismatch", target=target)
        del self.active_leases[target]
        return {"released": target, "lease_id": existing["lease_id"], "holder": seat, "logical_tick": self.logical_tick}

    def get_leases(self, seat: str) -> dict[str, Any]:
        self._require_on(bool(self.leases_enabled or self.claim_locks), "leases_disabled")
        return {"leases": [dict(self.active_leases[k]) for k in sorted(self.active_leases)], "expiry_ticks": self.lease_expiry_ticks, "logical_tick": self.logical_tick}

    get_claims = get_leases

    def barrier_satisfied(self, phase: int = 1, digest: str | None = None) -> bool:
        return any(r["phase"] == phase and r["satisfied"] is True and (digest is None or r["agreed_digest"] == digest) for r in self.barrier_history)

    def require_barrier(self, phase: int = 1, digest: str | None = None) -> dict[str, Any]:
        for rec in self.barrier_history:
            if rec["phase"] == phase and rec["satisfied"] is True:
                if digest is not None and rec["agreed_digest"] != digest:
                    raise Reject(409, "interface_mismatch", agreed_digest=rec["agreed_digest"])
                return dict(rec)
        raise Reject(409, "quorum_not_ready", phase=phase)

    def _pending_barrier_view(self, quorum: int) -> dict[str, Any]:
        arrived = sorted(self.barrier_arrivals)
        digests = {s: self.barrier_arrivals[s]["digest"] for s in arrived}
        conflict = len(set(digests.values())) > 1
        matched: bool | None = (False if conflict else None) if len(arrived) < quorum else False
        return {"phase": self.barrier_phase, "quorum": quorum, "arrived": arrived, "arrived_count": len(arrived), "waiting_for": max(0, quorum - len(arrived)), "digests": digests, "satisfied": False, "digest_matched": matched, "conflict": conflict, "agreed_digest": None, "logical_tick": self.logical_tick}

    def post_barrier(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_on(self.barrier_enabled, "barrier_disabled")
        digest = codec.clean_str(body.get("digest"), 256)
        if digest is None:
            raise Reject(400, "invalid_digest")
        if "phase" in body:
            req_p = codec.strict_int(body.get("phase"))
            if req_p is None:
                raise Reject(400, "invalid_phase")
            if req_p != self.barrier_phase:
                raise Reject(409, "stale_barrier_phase", current_phase=self.barrier_phase)
        if len(self.barrier_history) >= self.max_barrier_phases:
            raise Reject(429, "barrier_limit_reached")
        quorum = self.barrier_quorum or len(self.seats)
        self.barrier_arrivals[seat] = {"seat": seat, "digest": digest, "tick": self.logical_tick}
        view = self._pending_barrier_view(quorum)
        if view["arrived_count"] >= quorum and not view["conflict"]:
            rec = {**view, "waiting_for": 0, "satisfied": True, "digest_matched": True, "agreed_digest": digest, "released_tick": self.logical_tick}
            rec.pop("logical_tick", None)
            self.barrier_history.append(rec)
            self.barrier_phase += 1
            self.barrier_arrivals = {}
            self.state_version += 1
            return {**rec, "logical_tick": self.logical_tick}
        return view

    def get_barrier(self, seat: str) -> dict[str, Any]:
        self._require_on(self.barrier_enabled, "barrier_disabled")
        quorum, q_phase = self.barrier_quorum or len(self.seats), self.query_int("phase", 0)
        if q_phase > 0:
            for rec in self.barrier_history:
                if rec["phase"] == q_phase:
                    return {**rec, "completed": list(self.barrier_history), "logical_tick": self.logical_tick}
        if not self.barrier_arrivals and self.barrier_history and q_phase == 0:
            return {**self.barrier_history[-1], "next_phase": self.barrier_phase, "completed": list(self.barrier_history), "logical_tick": self.logical_tick}
        return {**self._pending_barrier_view(quorum), "completed": list(self.barrier_history)}

    def _resolve_handler(self, method: str, kind: str) -> Callable[..., dict[str, Any]] | None:
        if (direct := self.handlers.get((method, kind))) is not None:
            return direct
        attr_suffix = kind.replace(".", "_").replace("-", "_")
        return getattr(self, f"{method.lower()}_{attr_suffix}", None) if attr_suffix.isidentifier() else None

    def _execute_seat_request(self, seat: str, kind: str, method: str, body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        handler = self._resolve_handler(method, kind)
        if handler is None:
            return self._log(seat, kind, method, None, 404, {"error": f"no {method} /{kind}"})
        left = self.left(seat, kind)
        if left is not None and left <= 0:
            return self._log(seat, kind, method, None, 429, {"error": f"no {kind} calls left", "budget_left": 0})
        if method == "POST" and kind in self.unread_guard_kinds and (unread := self.unread_messages(seat)):
            return self._log(seat, kind, method, body, 409, {"error": "unread_messages", "unread_count": len(unread)})
        try:
            result = handler(seat, body) if method == "POST" else handler(seat)
        except Reject as refusal:
            return self._log(seat, kind, method, body if method == "POST" else None, refusal.status, refusal.body)
        except Exception as exc:
            return self._log(seat, kind, method, None, 500, {"error": f"world error: {type(exc).__name__}"})
        if kind in self.budgets:
            self.used[seat][kind] = self.used[seat].get(kind, 0) + 1
            result = {**result, "budget_left": self.budget_snapshot(seat)}
        return self._log(seat, kind, method, body if method == "POST" else None, 200, result)

    def _record_spoof_signals(self, seat: str, body: dict[str, Any] | None, headers: dict[str, str] | None) -> None:
        hdr_spoof = bool(headers) and any((v := headers.get(h)) is not None and v.strip() != seat for h in ("x-seat", "x-sender", "x-from", "x-actor"))
        body_spoof = isinstance(body, dict) and any(k in body and body[k] != seat for k in ("seat", "from", "sender"))
        if hdr_spoof or body_spoof:
            self.spoof_attempts += 1

    def handle(
        self, method: Any, path: Any, token: str | None, body: Any, client: str,
        *, headers: dict[str, str] | None = None, body_error: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        clean_m, kind, query_map = codec.sanitize_method_and_path(method, path)
        with self.lock:
            if kind == "health" and clean_m == "GET":
                return 200, {"ok": True}
            seat = self._auth_seat(token)
            if kind == "admin.close":
                if client not in LOOPBACK:
                    return self._log(seat, kind, clean_m, None, 403, {"error": "admin calls come from main only"})
                if clean_m != "POST":
                    return self._log(None, kind, clean_m, None, 405, {"error": "method_not_allowed"})
                self.closed = True
                return self._log(None, kind, clean_m, None, 200, {"closed": True, "events": self.seq + 1})
            if seat is None:
                return self._log(None, kind, clean_m, None, 401, {"error": "unauthorized"})
            if self.closed:
                return self._log(seat, kind, clean_m, None, 410, {"error": "world_closed"}, late=True)
            self.logical_tick += 1
            self._expire_leases()
            if clean_m not in ("GET", "POST"):
                return self._log(seat, kind, clean_m, None, 405, {"error": "method_not_allowed"})
            parsed_body: dict[str, Any] | None = None
            if clean_m == "POST":
                if body_error is not None or not isinstance(body, dict):
                    return self._log(seat, kind, clean_m, None, 400, {"error": body_error or "invalid_json"})
                parsed_body = body
            self._record_spoof_signals(seat, parsed_body, headers)
            self._current_query = query_map
            try:
                return self._execute_seat_request(seat, kind, clean_m, parsed_body)
            finally:
                self._current_query = {}

    def dispatch(
        self, seat: str | None, method: str, path: str, body: Any = None, *, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, Any]]:
        clean_m = method.upper() if isinstance(method, str) else "INVALID"
        if path.split("?", 1)[0].strip("/") == "admin/close" and seat in (None, "admin"):
            return self.handle(clean_m, path, None, {}, "127.0.0.1")
        tok = self._seat_to_token.get(seat) if isinstance(seat, str) else None
        hdrs = {k.lower(): str(v) for k, v in headers.items()} if headers else None
        if clean_m != "POST":
            return self.handle(method, path, tok, None, REMOTE_CLIENT, headers=hdrs)
        raw, err = codec.coerce_json_object_bytes(body)
        parsed, p_err = codec.parse_json_object(raw) if err is None and raw is not None else (None, err)
        return self.handle(method, path, tok, parsed, REMOTE_CLIENT, headers=hdrs, body_error=p_err)

    def close(self) -> tuple[int, dict[str, Any]]:
        return self.handle("POST", "/admin/close", None, {}, "127.0.0.1")

    def _log(
        self, seat: str | None, kind: str, method: str, body: dict[str, Any] | None,
        status: int, response: dict[str, Any], *, late: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        if not self.log_intact:
            return 507, {"error": "world_log_overflow", "log_intact": False}
        try:
            safe_req = codec.json_safe(body) if isinstance(body, dict) else None
            safe_res = codec.json_safe(response)
        except (TypeError, ValueError):
            safe_req, status, safe_res = None, 500, {"error": "world error: unserializable_response"}
        next_seq = self.seq + 1
        event: dict[str, Any] = {
            "seq": next_seq, "t": round(self._clock(), 6), "logical_tick": self.logical_tick,
            "seat": seat, "kind": kind, "method": method, "status": status, "ok": 200 <= status < 300,
            "request": safe_req, "response": safe_res, "prev_hash": self.prev_hash,
        }
        if late:
            event["late"] = True
        digest = codec.compute_entry_hash(self.prev_hash, event)
        event["entry_hash"] = digest
        line = codec.canonical_json(event)
        assert "\n" not in line and "\r" not in line
        raw = (line + "\n").encode("utf-8")
        if self.log_bytes + len(raw) > self.max_log_bytes:
            self.log_intact = False
            return 507, {"error": "world_log_overflow", "log_intact": False}
        if self.events_path is not None:
            with self.events_path.open("ab") as handle_out:
                handle_out.write(raw)
        self.seq, self.prev_hash = next_seq, digest
        self.log_bytes += len(raw)
        self.events.append(event)
        return status, safe_res


def make_server(world: World, port: int = PORT) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        timeout = 30

        def _answer(self, method: str) -> None:
            is_close = self.path.split("?", 1)[0].strip("/") == "admin/close"
            parsed_body, body_err = codec.read_framed_json_body(self.rfile, self.headers.get("Content-Length"), allow_empty_object=is_close) if method == "POST" else (None, None)
            auth = self.headers.get("Authorization") or ""
            token = auth[7:].strip() if auth[:7].lower() == "bearer " else None
            hdr_map = {k.lower(): v for k, v in self.headers.items()}
            status, response = world.handle(method, self.path, token, parsed_body, self.client_address[0], headers=hdr_map, body_error=body_err)
            data = codec.canonical_json(response).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            with contextlib.suppress(OSError):
                self.wfile.write(data)

        def do_GET(self) -> None:
            self._answer(self.command)

        do_POST = do_PUT = do_DELETE = do_PATCH = do_GET

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return ThreadingHTTPServer(("::", port), Handler)


def run_server(world: World, port: int = PORT) -> None:
    make_server(world, port).serve_forever()


serve = run_server


def close_world(port: int = PORT) -> None:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/admin/close", data=b"{}", method="POST")
    with urllib.request.urlopen(request, timeout=10) as reply:
        reply.read()


def load_events(path: Path | None = None) -> list[dict[str, Any]]:
    return codec.parse_events_jsonl((path or EVENTS).read_bytes())


def chain_ok(events: list[dict[str, Any]]) -> bool:
    return bool(events) and codec.verify_chain(events)[0]


def before_close(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    played: list[dict[str, Any]] = []
    for event in events:
        if event.get("kind") == "admin.close" and event.get("ok") is True:
            break
        if event.get("seat") is not None:
            played.append(event)
    return played


def in_play(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in before_close(events) if event.get("ok") is True]


def write_rewards(rewards: dict[str, float], path: Path | None = None, events: Path | None = None) -> None:
    clean: dict[str, float] = {}
    for key, value in rewards.items():
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"reward {key!r} is not a finite number")
        clean[key] = round(number, 6)
    target = path or REWARD
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(clean, sort_keys=True) + "\n", encoding="utf-8")
    log = events or EVENTS
    if log.is_file():
        (target.parent / "events.jsonl").write_bytes(log.read_bytes())
