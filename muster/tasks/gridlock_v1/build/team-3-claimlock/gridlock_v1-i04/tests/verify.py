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
ZONES = ("A", "B", "C")
STAGES = (1, 2, 3)
CREWS = ("alpha", "beta", "gamma")
TIE_LINES = ("AB", "BC", "CA")
CLAIM_EXPIRY_TICKS = 120
MAX_CLAIMS_PER_SEAT = 3
NEIGHBORS: dict[str, tuple[tuple[str, str], ...]] = {
    "A": (("AB", "B"), ("CA", "C")),
    "B": (("AB", "A"), ("BC", "C")),
    "C": (("BC", "B"), ("CA", "A")),
}
INCIDENT_LINES: dict[str, tuple[str, ...]] = {
    zone: tuple(line for line, _ in pairs) for zone, pairs in NEIGHBORS.items()
}
BUS_IDS = tuple(f"{zone}{stage}" for zone in ZONES for stage in STAGES)
CLAIM_TARGETS = frozenset(
    [
        *(f"crew:{c}" for c in CREWS),
        *(f"line:{ln}" for ln in TIE_LINES),
        *(f"crew:{c}:s{st}" for c in CREWS for st in STAGES),
        *(f"line:{ln}:s{st}" for ln in TIE_LINES for st in STAGES),
    ]
)
REQUIRED_EVENT_KEYS = (
    "seq", "t", "logical_tick", "seat", "kind", "method",
    "status", "ok", "request", "response", "prev_hash", "entry_hash",
)


class SimState:
    def __init__(self, instance: dict[str, Any]) -> None:
        seat_list = [str(s) for s in instance["seats"]]
        self.seats = set(seat_list)
        self.channel = bool(instance["channel"])
        self.claim_locks = bool(instance.get("claim_locks", False))
        self.action_budget = int(instance["budget"])
        self.thermal_cap = int(instance["thermal_cap"])
        self.stations = {str(s): [str(z) for z in zs] for s, zs in instance["stations"].items()}
        self.current_zone = {str(s): str(z) for s, z in instance["initial_zone"].items()}
        self.buses = {str(b): dict(spec) for b, spec in instance["buses"].items()}
        self.repaired: dict[str, bool] = dict.fromkeys(BUS_IDS, False)
        self.energized: dict[str, str | None] = dict.fromkeys(BUS_IDS, None)
        self.tripped: dict[str, bool] = dict.fromkeys(BUS_IDS, False)
        self.crew_busy: dict[str, tuple[str, str] | None] = dict.fromkeys(CREWS, None)
        self.actions_used: dict[str, int] = dict.fromkeys(seat_list, 0)
        self.active_leases: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.read_cursor: dict[str, int] = dict.fromkeys(seat_list, 0)
        self.logical_tick = self.lease_seq = 0
        self.cascade_trips = self.crew_conflicts = self.interlock_rejections = self.spoof_hits = 0
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


def _base_claim_target(target: str) -> str:
    parts = target.split(":")
    if len(parts) == 3 and parts[2] in ("s1", "s2", "s3"):
        return f"{parts[0]}:{parts[1]}"
    return target


def _claims_clear(st: SimState, seat: str, base_target: str) -> bool:
    if not st.claim_locks:
        return True
    for key, entry in st.active_leases.items():
        if _base_claim_target(key) == base_target and entry["holder"] != seat:
            return False
    return True


def _compute_flows(
    bus_id: str,
    buses: dict[str, dict[str, Any]],
    repaired: dict[str, bool],
    energized: dict[str, str | None],
    thermal_cap: int,
) -> tuple[dict[str, int], str | None]:
    spec = buses[bus_id]
    zone, stage = str(spec["zone"]), int(spec["stage"])
    pairs = NEIGHBORS[zone]
    if not repaired[bus_id]:
        return {line: thermal_cap + 4 for line, _ in pairs}, "unrepaired_fault"
    if stage > 1 and not all(energized[f"{z}{stage - 1}"] is not None for z in ZONES):
        flows = {line: (thermal_cap + 4 if energized[f"{nbr}{stage - 1}"] is None else 4) for line, nbr in pairs}
        if max(flows.values()) <= thermal_cap:
            flows = {line: thermal_cap + 4 for line, _ in pairs}
        return flows, "missing_stage_support"
    if not spec["coupled"]:
        return {line: 4 for line, _ in pairs}, None
    flows = {}
    for line, nbr in pairs:
        if energized[f"{nbr}{stage}"] is not None:
            flows[line] = 4
        else:
            delta = (int(spec["phase_tap"]) - int(buses[f"{nbr}{stage}"]["phase_tap"])) % 6
            flows[line] = 8 + 4 * delta if delta in (1, 2) else 6
    reason = "tie_line_overload" if any(val > thermal_cap for val in flows.values()) else None
    return flows, reason


def _apply_get_ok(st: SimState, seat: str, kind: str, resp: dict[str, Any]) -> bool:
    if kind == "rules":
        return (
            resp.get("you") == seat
            and _strict_int(resp.get("action_budget_left")) == st.action_budget - st.actions_used[seat]
            and resp.get("current_zone") == st.current_zone[seat]
            and resp.get("channel") is st.channel
        )
    if kind in ("zone", "zone.A", "zone.B", "zone.C"):
        z = st.current_zone[seat] if kind == "zone" else kind[-1]
        return z in ZONES and z == st.current_zone[seat] and resp.get("zone") == z
    if kind == "mine":
        return (
            resp.get("current_zone") == st.current_zone[seat]
            and _strict_int(resp.get("action_budget_left")) == st.action_budget - st.actions_used[seat]
        )
    if kind in ("msgs", "new"):
        if not st.channel:
            return False
        if st.messages:
            rc = _strict_int(resp.get("read_cursor"))
            top = st.messages[-1]["id"]
            if rc is None or rc < st.read_cursor[seat] or rc > top:
                return False
            st.read_cursor[seat] = rc
        return True
    if kind in ("claims", "leases"):
        return st.claim_locks and isinstance(resp.get("leases"), list)
    return False


def _apply_claim_release_msg(st: SimState, seat: str, kind: str, req: dict[str, Any], resp: dict[str, Any]) -> bool:
    if kind == "msg":
        txt = _clean_str(req.get("text"), 2000)
        if not st.channel or txt is None or len(txt.encode("utf-8")) > 4096:
            return False
        st.messages.append({"id": len(st.messages) + 1, "from": seat, "text": txt})
        return _strict_int(resp.get("id")) == len(st.messages) and resp.get("from") == seat and resp.get("text") == txt
    if kind == "claim":
        tgt = _clean_str(req.get("target"), 128)
        if not st.claim_locks or set(req) != {"target"} or tgt is None or tgt not in CLAIM_TARGETS:
            return False
        base = _base_claim_target(tgt)
        if not _claims_clear(st, seat, base):
            return False
        if base.startswith("line:") and base[5:] not in INCIDENT_LINES.get(st.current_zone[seat], ()):
            return False
        if tgt not in st.active_leases:
            if base.startswith("crew:") and any(k.startswith("crew:") and c["holder"] == seat for k, c in st.active_leases.items()):
                return False
            if sum(1 for c in st.active_leases.values() if c["holder"] == seat) >= MAX_CLAIMS_PER_SEAT:
                return False
            st.lease_seq += 1
            st.active_leases[tgt] = {"target": tgt, "holder": seat, "lease_id": f"lease-{st.lease_seq:04d}", "expires_tick": st.logical_tick + CLAIM_EXPIRY_TICKS}
        else:
            st.active_leases[tgt]["expires_tick"] = st.logical_tick + CLAIM_EXPIRY_TICKS
        return resp.get("target") == tgt and resp.get("holder") == seat
    if kind == "release":
        tgt = _clean_str(req.get("target"), 128)
        if not st.claim_locks or not ({"target"} <= set(req) <= {"target", "lease_id"}) or tgt not in st.active_leases:
            return False
        entry = st.active_leases[tgt]
        if entry["holder"] != seat or ("lease_id" in req and req.get("lease_id") != entry["lease_id"]):
            return False
        del st.active_leases[tgt]
        return resp.get("released") == tgt and resp.get("holder") == seat
    return False


def _apply_domain_post(st: SimState, seat: str, kind: str, req: dict[str, Any], resp: dict[str, Any]) -> bool:
    if st.actions_used[seat] >= st.action_budget:
        return False
    if kind == "move_zone":
        zone = _clean_str(req.get("to_zone"), 16)
        if set(req) != {"to_zone"} or zone not in ZONES or zone not in st.stations[seat] or zone == st.current_zone[seat]:
            return False
        prev = st.current_zone[seat]
        st.current_zone[seat] = zone
        st.actions_used[seat] += 1
        return resp.get("from_zone") == prev and resp.get("to_zone") == zone and _strict_int(resp.get("action_budget_left")) == st.action_budget - st.actions_used[seat]
    if kind == "dispatch_crew":
        bus, crew = _clean_str(req.get("bus"), 16), _clean_str(req.get("crew"), 32)
        if set(req) != {"bus", "crew"} or bus not in st.buses or crew not in CREWS or st.buses[bus]["zone"] != st.current_zone[seat]:
            return False
        if st.tripped[bus] or st.energized[bus] is not None or st.repaired[bus] or crew != st.buses[bus]["required_crew"]:
            return False
        busy_entry = st.crew_busy[crew]
        if not _claims_clear(st, seat, f"crew:{crew}") or (st.claim_locks and busy_entry is not None and busy_entry[0] != seat):
            return False
        st.actions_used[seat] += 1
        rem = st.action_budget - st.actions_used[seat]
        stage = int(st.buses[bus]["stage"])
        if stage > 1 and not all(st.energized[f"{z}{stage - 1}"] is not None for z in ZONES):
            return resp.get("stage_locked") is True and resp.get("dispatched") is False and _strict_int(resp.get("action_budget_left")) == rem
        if busy_entry is not None:
            st.crew_conflicts += 1
            return resp.get("crew_conflict") is True and resp.get("dispatched") is False and _strict_int(resp.get("action_budget_left")) == rem
        st.repaired[bus] = True
        st.crew_busy[crew] = (seat, bus)
        return resp.get("dispatched") is True and resp.get("bus") == bus and resp.get("crew") == crew and _strict_int(resp.get("action_budget_left")) == rem
    if kind == "energize":
        bus = _clean_str(req.get("bus"), 16)
        if set(req) != {"bus"} or bus not in st.buses or st.buses[bus]["zone"] != st.current_zone[seat]:
            return False
        if st.tripped[bus] or st.energized[bus] is not None:
            return False
        zone = str(st.buses[bus]["zone"])
        if any(not _claims_clear(st, seat, f"line:{ln}") for ln in INCIDENT_LINES[zone]):
            return False
        st.actions_used[seat] += 1
        rem = st.action_budget - st.actions_used[seat]
        flows, reason = _compute_flows(bus, st.buses, st.repaired, st.energized, st.thermal_cap)
        for c in CREWS:
            c_entry = st.crew_busy[c]
            if c_entry is not None and c_entry[1] == bus:
                st.crew_busy[c] = None
        if reason is not None:
            st.repaired[bus] = False
            st.tripped[bus] = True
            affected = {zone} | {nbr for line, nbr in NEIGHBORS[zone] if flows[line] > st.thermal_cap}
            reset_buses: list[str] = []
            for z in sorted(affected):
                for s_num in (3, 2, 1):
                    cand = f"{z}{s_num}"
                    if st.energized[cand] is not None:
                        st.energized[cand], st.repaired[cand], st.tripped[cand] = None, False, True
                        reset_buses.append(cand)
                        break
            st.cascade_trips += 1
            return resp.get("overload_trip") is True and resp.get("reset_buses") == reset_buses and _strict_int(resp.get("action_budget_left")) == rem
        st.energized[bus] = seat
        return resp.get("energized") is True and resp.get("overload_trip") is False and _strict_int(resp.get("action_budget_left")) == rem
    return False


def _record_refusal(st: SimState, seat: str | None, kind: str, status: int, resp: dict[str, Any]) -> None:
    err = str(resp.get("error") or "")
    if err == "spoof_attempt_blocked" or (kind == "admin.close" and status == 403 and seat is not None):
        st.spoof_hits += 1
    elif status == 409 and err in ("guard_rejected", "already_claimed"):
        st.interlock_rejections += 1


def _check_admin_close(st: SimState, ev: dict[str, Any]) -> bool:
    status, ok, seat, method, resp = int(ev["status"]), bool(ev["ok"]), ev.get("seat"), str(ev["method"]), ev["response"]
    if ok:
        valid = seat is None and method == "POST" and set(resp) == {"closed", "events", "log_intact"} and resp.get("closed") is True and _strict_int(resp.get("events")) == ev["seq"] and isinstance(resp.get("log_intact"), bool)
        if not valid or resp["log_intact"] is not True:
            st.log_intact = False
            return False
        st.closed = True
        return True
    if status not in (403, 405) or (seat is not None and seat not in st.seats) or _strict_int(ev.get("logical_tick")) != st.logical_tick:
        return False
    _record_refusal(st, seat, "admin.close", status, resp)
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
        for tgt in [t for t, le in st.active_leases.items() if le["expires_tick"] is not None and st.logical_tick >= le["expires_tick"]]:
            del st.active_leases[tgt]
        if not ok:
            _record_refusal(st, seat, kind, status, resp)
            continue
        if method == "GET":
            ok_step = _apply_get_ok(st, seat, kind, resp)
        elif method == "POST" and isinstance(req, dict):
            ok_step = _apply_claim_release_msg(st, seat, kind, req, resp) if kind in ("msg", "claim", "release") else _apply_domain_post(st, seat, kind, req, resp)
        else:
            ok_step = False
        if not ok_step:
            st.faithful = False
            break
    if not st.closed or not st.log_intact:
        st.faithful = False
    return st


def _zero_rewards(instance: dict[str, Any], *, chain_ok: bool, script_faithful: bool = False, spoof_detected: bool = False) -> dict[str, float]:
    return {
        "reward": 0.0,
        "buses_restored_ratio": 0.0,
        **{f"seat.{s}": 0.0 for s in instance["seats"]},
        "cascade_trips": 0.0,
        "crew_conflicts": 0.0,
        "interlock_rejections": 0.0,
        "spoof_detected": 1.0 if spoof_detected else 0.0,
        "script_faithful": 1.0 if script_faithful else 0.0,
        "chain_ok": 1.0 if chain_ok else 0.0,
    }


def score(events: list[dict[str, Any]], instance: dict[str, Any]) -> dict[str, float]:
    if not _verify_chain(events):
        return _zero_rewards(instance, chain_ok=False)
    st = _replay_events(events, instance)
    if not st.faithful or not st.closed or not st.log_intact:
        return _zero_rewards(instance, chain_ok=True)
    total = len(BUS_IDS)
    live = sum(1 for owner in st.energized.values() if owner is not None)
    ratio = round(live / total, 6)
    reward = 0.0 if st.spoof_hits > 0 else ratio
    seat_scores = {
        f"seat.{s}": (0.0 if st.spoof_hits > 0 else round(sum(1 for owner in st.energized.values() if owner == s) / total, 6))
        for s in instance["seats"]
    }
    return {
        "reward": reward,
        "buses_restored_ratio": ratio,
        **seat_scores,
        "cascade_trips": float(st.cascade_trips),
        "crew_conflicts": float(st.crew_conflicts),
        "interlock_rejections": float(st.interlock_rejections),
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
