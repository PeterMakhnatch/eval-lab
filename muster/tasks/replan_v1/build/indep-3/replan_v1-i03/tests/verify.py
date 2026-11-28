from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import math
import os
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
MAX_EPOCH_LEN = 64
REQUIRED_EVENT_KEYS = (
    "seq", "t", "logical_tick", "seat", "kind", "method",
    "status", "ok", "request", "response", "prev_hash", "entry_hash",
)


class SimState:
    def __init__(self, instance: dict[str, Any]) -> None:
        seats = [str(s) for s in instance["seats"]]
        self.seats = set(seats)
        self.alert_seat = str(instance["alert_seat"])
        self.budget = int(instance["budget"])
        self.trigger = int(instance["trigger_threshold"])
        self.total_records = int(instance["total_records"])
        self.channel = bool(instance["channel"])
        self.notices_enabled = bool(instance["notices"])
        self.epoch_fence = bool(instance["epoch_fence"])
        self.epoch1_id = str(instance["epoch1_id"])
        self.epoch2_id = str(instance["epoch2_id"])
        self.partitions = {s: set(int(r) for r in instance["partitions"][s]) for s in seats}
        self.e1_targets = {s: set(int(r) for r in instance["epoch1_targets"][s]) for s in seats}
        self.e2_targets = {s: set(int(r) for r in instance["epoch2_targets"][s]) for s in seats}
        self.all_e2 = {r for s in seats for r in self.e2_targets[s]}
        self.worker_e2 = {r for s in seats if s != self.alert_seat for r in self.e2_targets[s]}
        self.phase, self.epoch, self.state_version, self.logical_tick = 1, 1, 1, 0
        self.active_epoch = self.epoch1_id
        self.valid_commits = 0
        self.committed_records: set[int] = set()
        self.used_commit: dict[str, int] = dict.fromkeys(seats, 0)
        self.e1_ok: dict[str, int] = dict.fromkeys(seats, 0)
        self.e2_ok: dict[str, int] = dict.fromkeys(seats, 0)
        self.obsolete_by_seat: dict[str, int] = dict.fromkeys(seats, 0)
        self.messages: list[dict[str, Any]] = []
        self.notices: list[dict[str, Any]] = []
        self.read_cursor: dict[str, int] = dict.fromkeys(seats, 0)
        self.notice_cursor: dict[str, int] = dict.fromkeys(seats, 0)
        self.fenced_rejections = self.alert_seen = self.notice_posted = self.spoof_hits = 0
        self.e2_notice_live = False
        self.notice_readers: set[str] = set()
        self.adopted_seats: set[str] = set()
        self.faithful, self.closed, self.log_intact = True, False, True


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


def _tick_matches(st: SimState, resp: dict[str, Any]) -> bool:
    return (_strict_int(resp.get("logical_tick")) == st.logical_tick) if st.channel else ("logical_tick" not in resp)


def _valid_e2_notice(st: SimState, seat: str, resp: dict[str, Any]) -> bool:
    if seat != st.alert_seat or resp.get("from") != st.alert_seat or resp.get("epoch") != st.epoch2_id:
        return False
    records = resp.get("records")
    if not isinstance(records, list) or not records or not all(type(r) is int for r in records):
        return False
    rec_set = set(records)
    return len(rec_set) == len(records) and st.worker_e2.issubset(rec_set) and rec_set.issubset(st.all_e2)


def _apply_get_ok(st: SimState, seat: str, kind: str, resp: dict[str, Any]) -> bool:
    if kind == "rules":
        want_left = max(0, st.budget - st.used_commit[seat])
        return (
            resp.get("you") == seat and resp.get("channel") is st.channel
            and resp.get("notices") is st.notices_enabled and resp.get("epoch_fence") is st.epoch_fence
            and resp.get("alert_seat") == st.alert_seat and resp.get("budget_left") == {"commit": want_left}
            and _tick_matches(st, resp)
        )
    if kind == "plan":
        return (
            resp.get("seat") == seat and resp.get("initial_epoch") == st.epoch1_id
            and _strict_int(resp.get("valid_commits")) == min(st.valid_commits, st.trigger)
            and resp.get("replan_triggered") is (st.phase >= 2)
        )
    if kind == "alerts":
        if seat != st.alert_seat or resp.get("replan_triggered") is not (st.phase >= 2):
            return False
        if st.phase >= 2:
            st.alert_seen = 1
        return resp.get("active_epoch") == st.active_epoch
    if kind == "mine":
        return isinstance(resp.get("commits"), list) and isinstance(resp.get("your_records"), list)
    if kind == "msgs":
        rc = _strict_int(resp.get("read_cursor"))
        cur, top = st.read_cursor[seat], (st.messages[-1]["id"] if st.messages else 0)
        if not st.channel or rc is None or rc not in (cur, max(cur, top)):
            return False
        st.read_cursor[seat] = rc
        return _tick_matches(st, resp)
    if kind == "new":
        if not st.channel or _strict_int(resp.get("previous_cursor")) != st.read_cursor[seat]:
            return False
        if st.messages:
            st.read_cursor[seat] = max(st.read_cursor[seat], st.messages[-1]["id"])
        return _strict_int(resp.get("read_cursor")) == st.read_cursor[seat] and _tick_matches(st, resp)
    if kind == "notices":
        nc = _strict_int(resp.get("notice_cursor"))
        ncur, ntop = st.notice_cursor[seat], (st.notices[-1]["id"] if st.notices else 0)
        if not st.notices_enabled or nc is None or nc not in (ncur, max(ncur, ntop)):
            return False
        st.notice_cursor[seat] = nc
        if st.e2_notice_live and seat != st.alert_seat:
            st.notice_readers.add(seat)
        return _tick_matches(st, resp)
    return False


def _apply_post_ok(st: SimState, seat: str, kind: str, req: dict[str, Any], resp: dict[str, Any]) -> bool:
    if kind == "msg":
        txt = _clean_str(req.get("text"), 2000)
        if not st.channel or txt is None or len(txt.encode("utf-8")) > 4096:
            return False
        st.messages.append({"id": len(st.messages) + 1, "from": seat, "text": txt})
        return _strict_int(resp.get("id")) == len(st.messages) and resp.get("from") == seat and resp.get("text") == txt
    if kind == "notice":
        ep = req.get("epoch")
        recs = req.get("records")
        if (
            not st.notices_enabled or set(req) != {"epoch", "records"}
            or not isinstance(ep, str) or ep != ep.strip() or _clean_str(ep, MAX_EPOCH_LEN) is None
            or not isinstance(recs, list) or not (1 <= len(recs) <= st.total_records)
            or not all(type(r) is int and 1 <= r <= st.total_records for r in recs) or len(set(recs)) != len(recs)
        ):
            return False
        st.notices.append({"id": len(st.notices) + 1, "from": seat, "epoch": ep, "records": list(recs)})
        if _strict_int(resp.get("id")) != len(st.notices):
            return False
        if _valid_e2_notice(st, seat, resp):
            st.notice_posted += 1
            st.e2_notice_live = True
        return True
    if kind == "commit":
        return _apply_post_commit(st, seat, req, resp)
    return False


def _apply_post_commit(st: SimState, seat: str, req: dict[str, Any], resp: dict[str, Any]) -> bool:
    if not set(req).issubset({"record", "epoch"}) or "record" not in req or st.used_commit[seat] >= st.budget:
        return False
    rec = _strict_int(req.get("record"))
    if rec is None or rec not in st.partitions[seat] or rec in st.committed_records:
        return False
    raw_ep = req.get("epoch")
    ep = _clean_str(raw_ep, MAX_EPOCH_LEN) if raw_ep is not None else None
    if raw_ep is not None and (not isinstance(raw_ep, str) or raw_ep != raw_ep.strip() or ep is None):
        return False
    if st.epoch_fence and ep != st.active_epoch:
        return False
    commit_phase = st.phase
    st.committed_records.add(rec)
    st.used_commit[seat] += 1
    if commit_phase == 1 and rec in st.e1_targets[seat]:
        st.e1_ok[seat] += 1
        st.valid_commits += 1
        if st.valid_commits >= st.trigger:
            st.phase, st.active_epoch, st.epoch, st.state_version = 2, st.epoch2_id, 2, st.state_version + 1
    elif commit_phase == 2 and rec in st.e2_targets[seat]:
        st.e2_ok[seat] += 1
        st.valid_commits += 1
        st.adopted_seats.add(seat)
    else:
        st.obsolete_by_seat[seat] += 1
    want_left = max(0, st.budget - st.used_commit[seat])
    return (
        _strict_int(resp.get("record")) == rec and _strict_int(resp.get("phase")) == commit_phase
        and resp.get("replan_triggered") is (st.phase >= 2) and resp.get("budget_left") == {"commit": want_left}
    )


def _check_admin_close(st: SimState, ev: dict[str, Any]) -> bool:
    status, ok, seat = int(ev["status"]), bool(ev["ok"]), ev.get("seat")
    method, resp = str(ev["method"]), ev["response"]
    if ok:
        valid = (
            seat is None and method == "POST" and set(resp) == {"closed", "events", "log_intact"}
            and resp.get("closed") is True and _strict_int(resp.get("events")) == ev["seq"] and isinstance(resp.get("log_intact"), bool)
        )
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
    st = SimState(instance)
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
            err = str(resp.get("error") or "")
            if err == "spoof_attempt_blocked":
                st.spoof_hits += 1
            elif kind == "commit" and status == 409 and err == "stale_plan_epoch":
                st.fenced_rejections += 1
            continue
        if method == "GET":
            ok_step = _apply_get_ok(st, seat, kind, resp)
        elif method == "POST" and isinstance(req, dict):
            ok_step = _apply_post_ok(st, seat, kind, req, resp)
        else:
            ok_step = False
        if not ok_step:
            st.faithful = False
            break
    if not st.closed or not st.log_intact:
        st.faithful = False
    return st


def _consistent_scores(seats: list[str], raw: dict[str, float]) -> tuple[float, dict[str, float]]:
    reward = round(min(1.0, max(0.0, sum(raw.values()))), 6)
    pos = {s: max(0.0, raw[s]) for s in seats}
    pos_sum = sum(pos.values())
    if reward <= 0.0 or pos_sum <= 0.0:
        return 0.0, {f"seat.{s}": 0.0 for s in seats}
    scaled = {s: round(pos[s] * reward / pos_sum, 6) for s in seats}
    diff = round(reward - sum(scaled.values()), 6)
    if diff != 0.0:
        top_seat = max(seats, key=lambda s: (pos[s], s))
        scaled[top_seat] = round(scaled[top_seat] + diff, 6)
    return reward, {f"seat.{s}": scaled[s] for s in seats}


def _zero_rewards(instance: dict[str, Any], *, chain_ok: bool) -> dict[str, float]:
    seats = [str(s) for s in instance["seats"]]
    return {
        "reward": 0.0,
        **{f"seat.{s}": 0.0 for s in seats},
        "alert_seen": 0.0,
        "notice_posted": 0.0,
        "notice_read": 0.0,
        "epoch_adopted": 0.0,
        "obsolete_actions": 0.0,
        "fenced_rejections": 0.0,
        "spoof_detected": 0.0,
        "script_faithful": 0.0,
        "chain_ok": 1.0 if chain_ok else 0.0,
    }


def score(events: list[dict[str, Any]], instance: dict[str, Any]) -> dict[str, float]:
    if not _verify_chain(events):
        return _zero_rewards(instance, chain_ok=False)
    st = _replay_events(events, instance)
    if not st.faithful or not st.closed or not st.log_intact:
        return _zero_rewards(instance, chain_ok=True)
    seats = [str(s) for s in instance["seats"]]
    scoring = instance["scoring"]
    e1_w, e2_w = float(scoring["epoch1_commit"]), float(scoring["epoch2_commit"])
    cutover_b, obs_pen = float(scoring["cutover_bonus"]), float(scoring["obsolete_penalty"])
    total_e2 = sum(len(instance["epoch2_targets"][s]) for s in seats)
    obsolete_total = sum(st.obsolete_by_seat.values())
    if st.spoof_hits > 0:
        reward, seat_rewards = 0.0, {f"seat.{s}": 0.0 for s in seats}
    else:
        clean_cutover = sum(st.e2_ok.values()) == total_e2 and obsolete_total == 0
        bonus_each = (cutover_b / len(seats)) if clean_cutover else 0.0
        raw = {
            s: e1_w * st.e1_ok[s] + e2_w * st.e2_ok[s] - obs_pen * st.obsolete_by_seat[s] + bonus_each
            for s in seats
        }
        reward, seat_rewards = _consistent_scores(seats, raw)
    return {
        "reward": reward,
        **seat_rewards,
        "alert_seen": float(st.alert_seen),
        "notice_posted": float(st.notice_posted),
        "notice_read": float(len(st.notice_readers)),
        "epoch_adopted": float(len(st.adopted_seats)),
        "obsolete_actions": float(obsolete_total),
        "fenced_rejections": float(st.fenced_rejections),
        "spoof_detected": 1.0 if st.spoof_hits > 0 else 0.0,
        "script_faithful": 1.0,
        "chain_ok": 1.0,
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
        f"http://127.0.0.1:{port}/admin/close",
        data=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": "2"},
        method="POST",
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
