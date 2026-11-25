from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import math
import os
import re
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

WORLD_DIR = Path("/world")
INSTANCE_PATH = WORLD_DIR / "instance.json"
GENESIS_HASH = "0" * 64
HEX_CHARS = frozenset("0123456789abcdef")
UNHASHED_KEYS = frozenset({"entry_hash"})
MAX_WORLD_LOG_BYTES = 8 * 1024 * 1024
ADMIN_CLOSE_RESERVE = 1024
MAX_CHAIN_BYTES = MAX_WORLD_LOG_BYTES + ADMIN_CLOSE_RESERVE
MAX_CHAIN_EVENTS = 100_000
MAX_JSON_DEPTH, MAX_JSON_NODES, MAX_STR_LEN, MAX_INT_ABS = 16, 16_384, 65_536, 10**60
PLAN_ROLES = frozenset({"planner", "p1"})
BUILD_ROLES = frozenset({"builder", "p1"})
VERIFY_ROLES = frozenset({"verifier", "p1"})
HANDOFF_KEYS = frozenset({"epoch", "expected_version", "constraints", "handoff_digest"})
COMMIT_KEYS = frozenset({"epoch", "expected_version", "handoff_id", "verification_digest"})
VERIFY_KEYS = frozenset({"epoch", "handoff_id", "handoff_digest"})
REPORT_KEYS = frozenset({"done", "verified", "status", "text", "note"})
BUDGETS: dict[str, int] = {
    "rules": 12, "spec": 32, "plan": 4, "handoff": 4, "verify": 8, "commit": 4, "report": 8, "msg": 16,
}
REQUIRED_EVENT_KEYS = (
    "seq", "t", "logical_tick", "seat", "kind", "method",
    "status", "ok", "request", "response", "prev_hash", "entry_hash",
)
PHANTOM_MSG_RE = re.compile(
    r"\b(?:all\s+checks?\s+pass(?:ed)?|verified\s+and\s+done|all\s+checks\s+passed\s+and\s+verified\s+done)\b",
    re.I,
)


class SimState:
    def __init__(
        self, *, seats: set[str], channel: bool, notices_enabled: bool, fenced: bool,
        unread_guard_kinds: set[str], read_cursor: dict[str, int], notice_cursor: dict[str, int],
    ) -> None:
        self.seats, self.channel, self.notices_enabled, self.fenced = seats, channel, notices_enabled, fenced
        self.unread_guard_kinds = unread_guard_kinds
        self.epoch, self.state_version, self.logical_tick = 1, 1, 0
        self.plan_done = self.handoff_done = self.verify_passed = self.committed = self.clean_reported = False
        self.compaction_losses = self.phantom_reports = self.stale_version_rejects = self.unread_blocks = self.spoof_hits = 0
        self.faithful, self.closed, self.log_intact = True, False, True
        self.used: dict[str, dict[str, int]] = {s: {} for s in seats}
        self.messages: list[dict[str, Any]] = []
        self.notices: list[dict[str, Any]] = []
        self.read_cursor, self.notice_cursor = read_cursor, notice_cursor


def _reject_nonfinite(token: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {token}")


def _clean_chars(value: str, limit: int = MAX_STR_LEN) -> bool:
    if len(value) > limit:
        return False
    for ch in value:
        code = ord(ch)
        if (code < 0x20 and code not in (0x09, 0x0A)) or (0x7F <= code <= 0x9F) or (0xD800 <= code <= 0xDFFF) or code in (0x2028, 0x2029):
            return False
    return True


def _clean_str(val: Any, limit: int = 256) -> str | None:
    if not isinstance(val, str) or not _clean_chars(val, limit):
        return None
    stripped = val.strip()
    return stripped if stripped and len(stripped) <= limit else None


def _strict_int(val: Any) -> int | None:
    if isinstance(val, bool) or not isinstance(val, int) or abs(val) > MAX_INT_ABS:
        return None
    return val


def _clean_constraints(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not (1 <= len(raw) <= 16):
        return None
    out: dict[str, Any] = {}
    for key, val in raw.items():
        ck = _clean_str(key, 64)
        if ck is None or ck in out or isinstance(val, bool) or not isinstance(val, (str, int)):
            return None
        parsed = _clean_str(val, 128) if isinstance(val, str) else _strict_int(val)
        if parsed is None:
            return None
        out[ck] = parsed
    return out


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len(pairs) > MAX_JSON_NODES:
        raise ValueError("JSON object exceeds node limit")
    out: dict[str, Any] = {}
    for key, val in pairs:
        if not isinstance(key, str) or not _clean_chars(key) or key in out:
            raise ValueError(f"invalid or duplicate JSON key: {key!r}")
        out[key] = val
    return out


def _within_raw_depth(raw: bytes, limit: int = MAX_JSON_DEPTH) -> bool:
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


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _json_safe(root: Any) -> bool:
    stack: list[tuple[Any, int]] = [(root, 0)]
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
            if not _clean_chars(cur):
                return False
        elif isinstance(cur, dict):
            if len(cur) + visited > MAX_JSON_NODES + 1:
                return False
            for k, v in cur.items():
                if not isinstance(k, str) or not _clean_chars(k):
                    return False
                stack.append((v, depth + 1))
        elif isinstance(cur, list):
            if len(cur) + visited > MAX_JSON_NODES + 1:
                return False
            for item in cur:
                stack.append((item, depth + 1))
        else:
            return False
    return True


def _is_hex64(val: Any) -> bool:
    return isinstance(val, str) and len(val) == 64 and all(ch in HEX_CHARS for ch in val)


def _verify_chain(events: list[dict[str, Any]]) -> bool:
    if not isinstance(events, list) or not (1 <= len(events) <= MAX_CHAIN_EVENTS):
        return False
    prev = GENESIS_HASH
    for idx, ev in enumerate(events, start=1):
        if not isinstance(ev, dict) or "hmac" in ev or not _json_safe(ev) or any(k not in ev for k in REQUIRED_EVENT_KEYS):
            return False
        if type(ev.get("seq")) is not int or ev["seq"] != idx:
            return False
        ph, eh = ev.get("prev_hash"), ev.get("entry_hash")
        if not _is_hex64(ph) or not _is_hex64(eh):
            return False
        assert isinstance(ph, str) and isinstance(eh, str)
        if not hmac.compare_digest(ph, prev):
            return False
        status, ok_flag, method = ev.get("status"), ev.get("ok"), ev.get("method")
        if type(status) is not int or type(ok_flag) is not bool or ok_flag != (200 <= status < 300):
            return False
        if (method != "POST" and ev.get("request") is not None) or (ev.get("request") is not None and not isinstance(ev.get("request"), dict)):
            return False
        if not isinstance(ev.get("response"), dict):
            return False
        payload = {k: v for k, v in ev.items() if k not in UNHASHED_KEYS}
        try:
            want = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        except (TypeError, ValueError):
            return False
        if not hmac.compare_digest(eh, want):
            return False
        prev = eh
    return True


def parse_events_strict(raw: bytes) -> tuple[list[dict[str, Any]], bool]:
    if not raw or len(raw) > MAX_CHAIN_BYTES or not raw.endswith(b"\n"):
        return [], False
    raw_lines = raw[:-1].split(b"\n")
    if not (1 <= len(raw_lines) <= MAX_CHAIN_EVENTS):
        return [], False
    events: list[dict[str, Any]] = []
    for raw_line in raw_lines:
        if not raw_line or b"\r" in raw_line or not _within_raw_depth(raw_line, MAX_JSON_DEPTH):
            return [], False
        try:
            text = raw_line.decode("utf-8")
            obj = json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_reject_nonfinite)
        except (UnicodeDecodeError, ValueError, RecursionError):
            return [], False
        if not isinstance(obj, dict) or not _json_safe(obj) or canonical_json(obj) != text:
            return [], False
        events.append(obj)
    return events, _verify_chain(events)


def _base_digest(task_id: str, base_constraints: dict[str, Any]) -> str:
    return hashlib.sha256(f"{task_id}/1/{canonical_json(base_constraints)}".encode()).hexdigest()[:32]


def _unread_for(st: SimState, seat: str) -> int:
    cur = st.read_cursor[seat]
    return sum(1 for m in st.messages if m["id"] > cur and m["from"] != seat)


def _tick_matches(st: SimState, resp: dict[str, Any]) -> bool:
    return (_strict_int(resp.get("logical_tick")) == st.logical_tick) if st.channel else ("logical_tick" not in resp)


def _consume_budget(st: SimState, seat: str, kind: str, resp: dict[str, Any]) -> bool:
    if kind not in BUDGETS:
        return "budget_left" not in resp
    used_k = st.used[seat].get(kind, 0)
    if used_k >= BUDGETS[kind]:
        return False
    st.used[seat][kind] = used_k + 1
    want = {k: max(0, BUDGETS[k] - st.used[seat].get(k, 0)) for k in sorted(BUDGETS)}
    return resp.get("budget_left") == want


def _apply_get_ok(st: SimState, seat: str, kind: str, resp: dict[str, Any], task_id: str) -> bool:
    if kind == "rules":
        return resp.get("you") == seat and _strict_int(resp.get("epoch")) == st.epoch and _strict_int(resp.get("state_version")) == st.state_version and resp.get("channel") is st.channel and _tick_matches(st, resp)
    if kind == "spec":
        return (
            resp.get("task_id") == task_id and _strict_int(resp.get("epoch")) == st.epoch and _strict_int(resp.get("state_version")) == st.state_version
            and resp.get("plan_ready") is st.plan_done and resp.get("handoff_ready") is st.handoff_done
            and resp.get("verify_ready") is st.verify_passed and resp.get("commit_ready") is st.committed and "logical_tick" not in resp
        )
    if kind == "new":
        if not st.channel or _strict_int(resp.get("previous_cursor")) != st.read_cursor[seat]:
            return False
        if st.messages:
            st.read_cursor[seat] = max(st.read_cursor[seat], st.messages[-1]["id"])
        return _strict_int(resp.get("read_cursor")) == st.read_cursor[seat] and _tick_matches(st, resp)
    if kind == "msgs":
        cur, top = st.read_cursor[seat], (st.messages[-1]["id"] if st.messages else 0)
        rc = _strict_int(resp.get("read_cursor"))
        if not st.channel or rc is None or rc not in (cur, max(cur, top)):
            return False
        st.read_cursor[seat] = rc
        return _strict_int(resp.get("unread_count")) == _unread_for(st, seat) and _tick_matches(st, resp)
    if kind == "notices":
        ncur, ntop = st.notice_cursor[seat], (st.notices[-1]["id"] if st.notices else 0)
        nc = _strict_int(resp.get("notice_cursor"))
        if not st.notices_enabled or nc is None or nc not in (ncur, max(ncur, ntop)):
            return False
        st.notice_cursor[seat] = nc
        return _strict_int(resp.get("epoch")) == st.epoch and _strict_int(resp.get("state_version")) == st.state_version and _tick_matches(st, resp)
    if kind == "inbox":
        if not (st.channel or st.notices_enabled):
            return False
        if st.channel and st.messages:
            st.read_cursor[seat] = max(st.read_cursor[seat], st.messages[-1]["id"])
        if st.notices_enabled and st.notices:
            st.notice_cursor[seat] = max(st.notice_cursor[seat], st.notices[-1]["id"])
        curs = resp.get("cursors")
        return (
            isinstance(curs, dict) and _strict_int(curs.get("board")) == st.read_cursor[seat] and _strict_int(curs.get("notice")) == st.notice_cursor[seat]
            and _strict_int(resp.get("epoch")) == st.epoch and _strict_int(resp.get("state_version")) == st.state_version and _tick_matches(st, resp)
        )
    return False


def _apply_post_domain(
    st: SimState, seat: str, kind: str, req: dict[str, Any], resp: dict[str, Any], inst: dict[str, Any]
) -> bool:
    tid, b_cons = str(inst["task_id"]), dict(inst["base_spec"]["base_constraints"])
    exp_cons = {**b_cons, **{str(x["key"]): x["value"] for x in inst["shift_constraints"]}}
    h_id, h_dig, v_dig = str(inst["handoff_id"]), str(inst["handoff_digest"]), str(inst["verification_digest"])
    if kind in st.unread_guard_kinds and _unread_for(st, seat) > 0:
        return False
    if kind == "plan":
        if seat not in PLAN_ROLES or st.plan_done or st.epoch != 1 or set(req) != {"task_id", "base_digest"}:
            return False
        if _clean_str(req.get("task_id"), 128) != tid or _clean_str(req.get("base_digest"), 128) != _base_digest(tid, b_cons):
            return False
        st.plan_done, st.epoch, st.state_version = True, 2, st.state_version + 1
        st.notices.append({"id": len(st.notices) + 1, "epoch": 2})
        return resp.get("plan_seal") == str(inst["plan_seal"]) and _strict_int(resp.get("state_version")) == st.state_version
    if kind == "handoff":
        if seat not in BUILD_ROLES or not st.plan_done or st.handoff_done or st.committed or set(req) != HANDOFF_KEYS:
            return False
        if st.fenced and st.notices_enabled and st.notice_cursor[seat] < len(st.notices):
            return False
        if _strict_int(req.get("epoch")) != 2 or _strict_int(req.get("expected_version")) != st.state_version or _clean_constraints(req.get("constraints")) != exp_cons or _clean_str(req.get("handoff_digest"), 128) != h_dig:
            return False
        st.handoff_done, st.state_version = True, st.state_version + 1
        return resp.get("handoff_id") == h_id and _strict_int(resp.get("state_version")) == st.state_version
    if kind == "verify":
        if seat not in VERIFY_ROLES or not set(req).issubset(VERIFY_KEYS):
            return False
        req_ep = _strict_int(req["epoch"]) if "epoch" in req else st.epoch
        r_hid = _clean_str(req["handoff_id"], 128) if "handoff_id" in req else None
        r_hdig = _clean_str(req["handoff_digest"], 128) if "handoff_digest" in req else None
        if req_ep is None or ("handoff_id" in req and r_hid is None) or ("handoff_digest" in req and r_hdig is None):
            return False
        want_ok = st.plan_done and st.handoff_done and st.epoch == 2 and req_ep == 2 and r_hid == h_id and r_hdig == h_dig
        if resp.get("passed") is not want_ok or resp.get("verified") is not want_ok:
            return False
        if want_ok:
            st.verify_passed = True
            return resp.get("verification_digest") == v_dig
        return True
    if kind == "commit":
        if seat not in VERIFY_ROLES or not (st.plan_done and st.handoff_done and st.verify_passed) or st.committed:
            return False
        if (st.fenced and st.notices_enabled and st.notice_cursor[seat] < len(st.notices)) or set(req) != COMMIT_KEYS:
            return False
        if _strict_int(req.get("epoch")) != 2 or _strict_int(req.get("expected_version")) != st.state_version or _clean_str(req.get("handoff_id"), 128) != h_id or _clean_str(req.get("verification_digest"), 128) != v_dig:
            return False
        st.committed, st.state_version = True, st.state_version + 1
        return resp.get("commit_ready") is True and _strict_int(resp.get("state_version")) == st.state_version
    return False


def _apply_post_comm_or_report(
    st: SimState, seat: str, kind: str, req: dict[str, Any], resp: dict[str, Any]
) -> bool:
    if kind == "msg":
        txt = _clean_str(req.get("text"), 2000)
        if not st.channel or txt is None or len(txt.encode("utf-8")) > 4096:
            return False
        st.messages.append({"id": len(st.messages) + 1, "from": seat, "text": txt})
        if PHANTOM_MSG_RE.search(txt) and (not (st.committed and st.verify_passed) or seat not in VERIFY_ROLES):
            st.phantom_reports += 1
        return _strict_int(resp.get("id")) == len(st.messages) and resp.get("from") == seat and resp.get("text") == txt
    if kind == "report":
        if not req or not set(req).issubset(REPORT_KEYS) or not isinstance(req.get("done"), bool):
            return False
        if "verified" in req and not isinstance(req["verified"], bool):
            return False
        for skey, cap in (("status", 64), ("text", 512), ("note", 512)):
            if skey in req and _clean_str(req[skey], cap) is None:
                return False
        done_f, ver_f = bool(req["done"]), bool(req.get("verified", False))
        st_clean = _clean_str(req.get("status"), 64)
        st_s = st_clean.strip().lower() if st_clean is not None else ""
        claims_done = done_f or ver_f or st_s in ("done", "verified", "passed", "completed", "complete")
        if claims_done and (not (st.committed and st.verify_passed) or seat not in VERIFY_ROLES):
            if st.fenced or resp.get("phantom_success") is not True:
                return False
            st.phantom_reports += 1
            return True
        if done_f and ver_f and st.committed and st.verify_passed and seat in VERIFY_ROLES:
            st.clean_reported = True
        return resp.get("clean") is True
    return False


def _record_refusal(st: SimState, seat: str, kind: str, req: Any, resp: dict[str, Any]) -> None:
    err = str(resp.get("error") or "")
    if err == "spoof_attempt_blocked":
        st.spoof_hits += 1
    elif err == "unread_messages":
        st.unread_blocks += 1
    elif err == "stale_version":
        st.stale_version_rejects += 1
    elif err in ("compaction_loss", "unacked_epoch"):
        st.compaction_losses += 1
    elif err == "phantom_success":
        st.phantom_reports += 1
    elif kind in ("msg", "report") and isinstance(req, dict) and req.get("done") is True and (not (st.committed and st.verify_passed) or seat not in VERIFY_ROLES):
        st.phantom_reports += 1


def _check_admin_close(st: SimState, ev: dict[str, Any]) -> bool:
    status, ok, seat = int(ev["status"]), bool(ev["ok"]), ev.get("seat")
    method, resp = str(ev["method"]), ev["response"]
    if ok:
        valid = seat is None and method == "POST" and set(resp) == {"closed", "events", "log_intact"} and resp.get("closed") is True and _strict_int(resp.get("events")) == ev["seq"] and isinstance(resp.get("log_intact"), bool)
        if not valid:
            return False
        if resp["log_intact"] is not True:
            st.log_intact = False
            return False
        st.closed = True
        return True
    if status not in (403, 405) or (seat is not None and seat not in st.seats) or _strict_int(ev.get("logical_tick")) != st.logical_tick:
        return False
    if status == 403 and seat is not None:
        st.spoof_hits += 1
    return True


def _replay_events(events: list[dict[str, Any]], instance: dict[str, Any]) -> SimState:
    seat_list = [str(s) for s in instance["seats"]]
    st = SimState(
        seats=set(seat_list), channel=bool(instance["channel"]),
        notices_enabled=bool(instance.get("notices_enabled", True)), fenced=bool(instance.get("fenced", False)),
        unread_guard_kinds={str(k) for k in instance.get("unread_guard_kinds", ())},
        read_cursor=dict.fromkeys(seat_list, 0), notice_cursor=dict.fromkeys(seat_list, 0),
    )
    tid = str(instance["task_id"])
    for ev in events:
        status, ok, seat = int(ev["status"]), bool(ev["ok"]), ev.get("seat")
        kind, method, req, resp = str(ev["kind"]), str(ev["method"]), ev.get("request"), ev["response"]
        if status >= 500:
            st.faithful = False
            break
        if kind == "admin.close":
            if not _check_admin_close(st, ev):
                st.faithful = False
                break
            continue
        if status == 401:
            if seat is not None or ok or _strict_int(ev.get("logical_tick")) != st.logical_tick:
                st.faithful = False
                break
            continue
        if st.closed:
            if ok or status != 410 or ev.get("late") is not True or seat not in st.seats or _strict_int(ev.get("logical_tick")) != st.logical_tick:
                st.faithful = False
                break
            continue
        if not isinstance(seat, str) or seat not in st.seats:
            st.faithful = False
            break
        st.logical_tick += 1
        if _strict_int(ev.get("logical_tick")) != st.logical_tick:
            st.faithful = False
            break
        if not ok:
            _record_refusal(st, seat, kind, req, resp)
            continue
        if not _consume_budget(st, seat, kind, resp):
            st.faithful = False
            break
        if method == "GET":
            ok_step = _apply_get_ok(st, seat, kind, resp, tid)
        elif method == "POST" and isinstance(req, dict):
            ok_step = _apply_post_comm_or_report(st, seat, kind, req, resp) if kind in ("msg", "report") else _apply_post_domain(st, seat, kind, req, resp, instance)
        else:
            ok_step = False
        if not ok_step:
            st.faithful = False
            break
    if not st.closed or not st.log_intact:
        st.faithful = False
    return st


def _zero_rewards(instance: dict[str, Any], *, chain_ok: bool) -> dict[str, float]:
    seats = [str(s) for s in instance["seats"]]
    return {
        "reward": 0.0, **{f"seat.{s}": 0.0 for s in seats},
        "plan_ok": 0.0, "handoff_ok": 0.0, "verify_ok": 0.0, "commit_ok": 0.0, "report_ok": 0.0,
        "compaction_loss": 0.0, "phantom_success": 0.0, "stale_version_rejects": 0.0,
        "unread_guard_blocks": 0.0, "spoof_detected": 0.0, "script_faithful": 0.0,
        "chain_ok": 1.0 if chain_ok else 0.0,
    }


def score(events: list[dict[str, Any]], instance: dict[str, Any]) -> dict[str, float]:
    if not _verify_chain(events):
        return _zero_rewards(instance, chain_ok=False)
    st = _replay_events(events, instance)
    if not st.faithful or not st.closed or not st.log_intact:
        return _zero_rewards(instance, chain_ok=True)
    seats = [str(s) for s in instance["seats"]]
    commit_rep_ok = st.committed and st.clean_reported
    if st.spoof_hits > 0:
        reward = 0.0
    else:
        raw = (0.25 if st.plan_done else 0.0) + (0.25 if st.handoff_done else 0.0) + (0.25 if st.verify_passed else 0.0) + (0.25 if commit_rep_ok else 0.0)
        debt = (0.10 if st.compaction_losses > 0 else 0.0) + (0.15 if st.phantom_reports > 0 else 0.0)
        reward = round(max(0.0, raw - debt), 6)
    if seats == ["p1"]:
        seat_scores = {"seat.p1": reward}
    else:
        no_spoof = st.spoof_hits == 0
        seat_scores = {
            "seat.planner": reward if (st.plan_done and no_spoof) else 0.0,
            "seat.builder": reward if (st.handoff_done and no_spoof) else 0.0,
            "seat.verifier": reward if (commit_rep_ok and no_spoof) else 0.0,
        }
    return {
        "reward": reward, **{f"seat.{s}": round(float(seat_scores.get(f"seat.{s}", 0.0)), 6) for s in seats},
        "plan_ok": 1.0 if st.plan_done else 0.0, "handoff_ok": 1.0 if st.handoff_done else 0.0,
        "verify_ok": 1.0 if st.verify_passed else 0.0, "commit_ok": 1.0 if st.committed else 0.0,
        "report_ok": 1.0 if st.clean_reported else 0.0, "compaction_loss": 1.0 if st.compaction_losses > 0 else 0.0,
        "phantom_success": 1.0 if st.phantom_reports > 0 else 0.0, "stale_version_rejects": float(st.stale_version_rejects),
        "unread_guard_blocks": float(st.unread_blocks), "spoof_detected": 1.0 if st.spoof_hits > 0 else 0.0,
        "script_faithful": 1.0, "chain_ok": 1.0,
    }


def _load_events_file(path: Path) -> tuple[list[dict[str, Any]], bool]:
    try:
        st_info = os.lstat(path)
    except OSError:
        return [], False
    if not stat.S_ISREG(st_info.st_mode) or st_info.st_size <= 0 or st_info.st_size > MAX_CHAIN_BYTES:
        return [], False
    return parse_events_strict(path.read_bytes())


load_strict_events = _load_events_file


def _close_world_local(port: int = 8080) -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/admin/close", data=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": "2"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as reply:
            reply.read(16385)
    except urllib.error.HTTPError as exc:
        with contextlib.suppress(OSError):
            exc.read(16385)
        exc.close()
    except OSError:
        pass


def _write_rewards_local(rewards: dict[str, float], reward_path: Path, events_path: Path) -> None:
    clean: dict[str, float] = {}
    for key, val in rewards.items():
        num = float(val)
        if not math.isfinite(num):
            raise ValueError(f"non-finite reward for {key!r}")
        clean[key] = round(num, 6)
    reward_path.parent.mkdir(parents=True, exist_ok=True)
    reward_path.write_text(json.dumps(clean, sort_keys=True) + "\n", encoding="utf-8")
    if events_path.is_file():
        (reward_path.parent / "events.jsonl").write_bytes(events_path.read_bytes())


def main() -> None:
    _close_world_local()
    events_path = Path(os.environ.get("WORLD_EVENTS", "/logs/world/events.jsonl"))
    reward_path = Path(os.environ.get("WORLD_REWARD", "/logs/verifier/reward.json"))
    instance = json.loads(INSTANCE_PATH.read_text(encoding="utf-8"))
    events, ok = _load_events_file(events_path)
    rewards = score(events, instance) if ok else _zero_rewards(instance, chain_ok=False)
    _write_rewards_local(rewards, reward_path, events_path)


if __name__ == "__main__":
    main()
