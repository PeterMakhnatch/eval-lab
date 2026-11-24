import contextlib
import hashlib
import hmac
import json
import math
import os
import stat
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
sys.path.insert(0, "/world")

import worldkit  # noqa: E402

INSTANCE = Path("/world/instance.json")
PEERS = ("p_peer", "p_bystander", "p_coord")
HEX_CHARS = frozenset("0123456789abcdef")
GENESIS_HASH = "0" * 64
UNHASHED_KEYS = ("entry_hash", "hmac")
MAX_CHAIN_LINES, MAX_CHAIN_BYTES, MAX_TICK = 20_000, 64 * 1024 * 1024, 10_000
MAX_JSON_DEPTH, MAX_JSON_NODES, MAX_STR_LEN, MAX_INT_ABS = 30, 4096, 65_536, 10**60
MSG_BUDGET, MAX_MSG_CHARS, MAX_TOTAL_MSGS = 16, 500, 24
MAX_SLOT_CHARS, MAX_LEASE_ID_CHARS, PARAM_LIMIT = 128, 128, 1_000_000_000
DEFAULT_CLAIM_TTL_TICKS = 16
COLLISION_ERRORS = frozenset({
    "active_lease_collision", "expired_lease_requires_force_expired", "lock_violation", "lease_not_owned", "lease_expired",
})
REQUIRED_EVENT_KEYS = (
    "seq", "t", "seat", "kind", "method", "status", "ok", "request", "response", "prev_hash", "entry_hash",
)


def _reject_nonfinite(token: str) -> None:
    raise ValueError(f"non-finite JSON constant: {token}")


def _clean_str(value: str, limit: int = MAX_STR_LEN) -> bool:
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


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _json_safe(value: object) -> bool:
    stack: list[tuple[object, int]] = [(value, 0)]
    visited = 0
    while stack:
        visited += 1
        cur, depth = stack.pop()
        if visited > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
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
            if len(cur) + visited > MAX_JSON_NODES + 1:
                return False
            for key, item in cur.items():
                if not isinstance(key, str) or not _clean_str(key):
                    return False
                stack.append((item, depth + 1))
        elif isinstance(cur, list):
            if len(cur) + visited > MAX_JSON_NODES + 1:
                return False
            for item in cur:
                stack.append((item, depth + 1))
        else:
            return False
    return True


def _is_sha256_hex(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in HEX_CHARS for ch in value)


def _events_chain_ok(events: list[dict]) -> bool:
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_CHAIN_LINES:
        return False
    prev = GENESIS_HASH
    for idx, event in enumerate(events, start=1):
        if not isinstance(event, dict) or not _json_safe(event) or any(k not in event for k in REQUIRED_EVENT_KEYS):
            return False
        if type(event.get("seq")) is not int or event["seq"] != idx:
            return False
        prev_hash, got_hash = event.get("prev_hash"), event.get("entry_hash")
        if not _is_sha256_hex(prev_hash) or not _is_sha256_hex(got_hash):
            return False
        assert isinstance(prev_hash, str) and isinstance(got_hash, str)
        if not hmac.compare_digest(prev_hash, prev):
            return False
        status, ok_flag, kind, method = event.get("status"), event.get("ok"), event.get("kind"), event.get("method")
        if type(status) is not int or type(ok_flag) is not bool or ok_flag != (status == 200):
            return False
        if not isinstance(kind, str) or not (1 <= len(kind) <= 128) or not _clean_str(kind, 128):
            return False
        if not isinstance(method, str) or not (1 <= len(method) <= 16) or not _clean_str(method, 16):
            return False
        if (ok_flag and method not in ("GET", "POST")) or (not ok_flag and event.get("request") is not None):
            return False
        if "late" in event and event["late"] is not True:
            return False
        payload = {k: v for k, v in event.items() if k not in UNHASHED_KEYS}
        try:
            want_hash = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
        except (TypeError, ValueError, OverflowError):
            return False
        if not hmac.compare_digest(got_hash, want_hash):
            return False
        prev = got_hash
    return True


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


def _zero_rewards(instance: dict, chain_valid: bool = False) -> dict[str, float]:
    return {
        "reward": 0.0, **{f"seat.{s}": 0.0 for s in instance["seats"]},
        "checked_leases": 0.0, "checked_msgs": 0.0, "lease_collision": 0.0, "lock_violation": 0.0,
        "rumor_trapped": 0.0, "waited_for_expiry": 0.0, "released_cleanly": 0.0,
        "script_faithful": 0.0, "chain_ok": 1.0 if chain_valid else 0.0,
    }


def _init_sim(instance: dict) -> dict[str, Any]:
    raw_rel, raw_rmsg = instance.get("peer_release_at_tick"), instance.get("peer_release_msg")
    slots_by_id = {str(s["slot"]): dict(s) for s in instance["initial_slots"]}
    slot_order = [str(s["slot"]) for s in instance["initial_slots"]]
    msgs = [
        {"id": idx + 1, "from": str(it["from"]), "role": str(it["role"]), "text": str(it["text"])}
        for idx, it in enumerate(instance["init_messages"])
    ]
    return {
        "tick": 0, "lease_reads": 0, "msg_reads": 0, "msgs_used": 0,
        "claimed_used": False, "worked_used": False, "released_used": False,
        "collision_count": 0, "lock_violation_count": 0,
        "peer_release_at_tick": int(raw_rel) if raw_rel is not None else None,
        "peer_released": False, "peer_release_msg": dict(raw_rmsg) if isinstance(raw_rmsg, dict) else None,
        "release_msg_sent": False, "slot_order": slot_order, "slots_by_id": slots_by_id, "messages": msgs,
        "my_lease": None, "checked_leases_pre_claim": False, "checked_msgs_pre_claim": False,
        "saw_expired_on_leases": False, "reclaimed_expired": False, "work_record": None, "released_slot": None,
    }


def _advance_sim_tick(sim: dict[str, Any], instance: dict) -> list[tuple[str | None, str, dict | None, dict]]:
    sim["tick"] = int(sim["tick"]) + 1
    tick, ps, rel_at = int(sim["tick"]), str(instance["primary_slot"]), sim["peer_release_at_tick"]
    aux: list[tuple[str | None, str, dict | None, dict]] = []
    if rel_at is not None and not sim["peer_released"] and tick >= int(rel_at):
        ps_entry = sim["slots_by_id"][ps]
        if ps_entry["status"] == "active" and ps_entry["holder"] == "p_peer":
            rel_lid = str(ps_entry["lease_id"])
            ps_entry["status"], ps_entry["holder"], ps_entry["heartbeat_active"] = "released", None, False
            ps_entry["released_by"], ps_entry["released_at_tick"] = "p_peer", tick
            sim["peer_released"] = True
            aux.append((None, "script.round", None, {
                "transition": "peer_release", "actor": "p_peer", "slot": ps, "lease_id": rel_lid, "tick": tick,
            }))
    msg_due = int(rel_at) if rel_at is not None else 1
    rmsg = sim["peer_release_msg"]
    if rmsg is not None and not sim["release_msg_sent"] and tick >= msg_due:
        sim["release_msg_sent"] = True
        msgs = sim["messages"]
        msg = {"id": len(msgs) + 1, "from": str(rmsg["from"]), "role": str(rmsg["role"]), "text": str(rmsg["text"])}
        msgs.append(msg)
        aux.append((msg["from"], "msg", {"text": msg["text"]}, dict(msg)))
    for sid in sim["slot_order"]:
        entry = sim["slots_by_id"][sid]
        if entry["status"] != "active":
            continue
        if entry["holder"] in PEERS and bool(entry["heartbeat_active"]):
            entry["expires_at_tick"] = tick + int(entry["ttl_ticks"])
            continue
        exp_at = entry["expires_at_tick"]
        if exp_at is not None and tick >= int(exp_at):
            exp_lid, exp_holder = str(entry["lease_id"]), str(entry["holder"])
            entry["status"], entry["holder"], entry["heartbeat_active"] = "expired", None, False
            entry["expired_at_tick"] = tick
            if exp_holder == "p1" and sim["my_lease"] is not None and sim["my_lease"]["slot"] == sid:
                sim["my_lease"]["status"] = "expired"
            aux.append((None, "script.round", None, {
                "transition": "lease_expired", "actor": exp_holder, "slot": sid, "lease_id": exp_lid, "tick": tick,
            }))
    return aux


def _sim_budget_left(sim: dict[str, Any]) -> dict[str, int]:
    return {
        "claim": 0 if sim["claimed_used"] else 1, "msg": MSG_BUDGET - int(sim["msgs_used"]),
        "release": 0 if sim["released_used"] else 1, "work": 0 if sim["worked_used"] else 1,
    }


def _step_sim_claim(sim: dict[str, Any], instance: dict, req: dict) -> dict[str, Any] | None:
    keys, slot, force_exp = set(req), req.get("slot"), req.get("force_expired", False)
    if (
        sim["claimed_used"] or sim["my_lease"] is not None or not keys or not (keys <= {"slot", "force_expired"})
        or "slot" not in keys or not isinstance(slot, str) or not _clean_str(slot, MAX_SLOT_CHARS)
        or not (1 <= len(slot.strip()) <= MAX_SLOT_CHARS) or type(force_exp) is not bool
    ):
        return None
    entry = sim["slots_by_id"].get(slot)
    if entry is None or not entry["candidate_for_p1"] or entry["status"] == "active":
        return None
    if force_exp != (entry["status"] == "expired"):
        return None
    reclaimed, prev_lid, tick = entry["status"] == "expired", entry["lease_id"], int(sim["tick"])
    expires_at, p1_lid = tick + DEFAULT_CLAIM_TTL_TICKS, str(instance["p1_lease_id"])
    entry["status"], entry["holder"], entry["lease_id"] = "active", "p1", p1_lid
    entry["acquired_at_tick"], entry["ttl_ticks"], entry["expires_at_tick"] = tick, DEFAULT_CLAIM_TTL_TICKS, expires_at
    entry["heartbeat_active"] = False
    sim["claimed_used"], sim["reclaimed_expired"] = True, reclaimed
    sim["checked_leases_pre_claim"] = int(sim["lease_reads"]) > 0
    sim["checked_msgs_pre_claim"] = int(sim["msg_reads"]) > 0
    sim["my_lease"] = {
        "slot": slot, "lease_id": p1_lid, "status": "active",
        "acquired_at_tick": tick, "expires_at_tick": expires_at, "work_completed": False,
    }
    return {
        "task_id": str(instance["task_id"]), "action": "claim", "slot": slot, "lease_id": p1_lid,
        "holder": "p1", "status": "active", "acquired_at_tick": tick, "expires_at_tick": expires_at,
        "reclaimed_expired": reclaimed, "previous_lease_id": prev_lid,
        "collisions_recorded": int(sim["collision_count"]), "budget_left": _sim_budget_left(sim),
    }


def _step_sim_work_or_release(sim: dict[str, Any], instance: dict, kind: str, req: dict) -> dict[str, Any] | None:
    if kind == "work":
        slot, lid, prm = req.get("slot"), req.get("lease_id"), req.get("param")
        if (
            sim["worked_used"] or set(req) != {"slot", "lease_id", "param"}
            or not isinstance(slot, str) or not isinstance(lid, str)
            or type(prm) is not int or not (-PARAM_LIMIT <= prm <= PARAM_LIMIT)
        ):
            return None
        entry, my_l = sim["slots_by_id"].get(slot), sim["my_lease"]
        if entry is None or my_l is None or my_l["slot"] != slot or entry["lease_id"] != lid:
            return None
        if entry["holder"] != "p1" or entry["status"] != "active" or entry["work_completed"]:
            return None
        matched_param = prm == int(instance["work_param"])
        entry["work_completed"] = True
        my_l["work_completed"] = True
        sim["worked_used"] = True
        sim["work_record"] = {"slot": slot, "lease_id": lid, "param": prm, "matched_param": matched_param}
        return {
            "task_id": str(instance["task_id"]), "action": "work", "slot": slot, "lease_id": lid,
            "committed_param": prm, "matched_param": matched_param, "work_completed": True,
            "tick": int(sim["tick"]), "collisions_recorded": int(sim["collision_count"]), "budget_left": _sim_budget_left(sim),
        }
    slot, lid = req.get("slot"), req.get("lease_id")
    if sim["released_used"] or set(req) != {"slot", "lease_id"} or not isinstance(slot, str) or not isinstance(lid, str):
        return None
    entry, my_l = sim["slots_by_id"].get(slot), sim["my_lease"]
    if entry is None or my_l is None or my_l["slot"] != slot or entry["lease_id"] != lid:
        return None
    if entry["holder"] != "p1" or entry["status"] != "active" or not entry["work_completed"]:
        return None
    tick = int(sim["tick"])
    entry["status"], entry["holder"], entry["heartbeat_active"] = "released", None, False
    entry["released_by"], entry["released_at_tick"] = "p1", tick
    my_l["status"] = "released"
    sim["released_used"], sim["released_slot"] = True, slot
    return {
        "task_id": str(instance["task_id"]), "action": "release", "phase": "completed", "slot": slot,
        "lease_id": lid, "status": "released", "released_by": "p1", "released_at_tick": tick,
        "work_completed": True, "collisions_recorded": int(sim["collision_count"]), "budget_left": _sim_budget_left(sim),
    }


def _step_sim_non_tick(sim: dict[str, Any], instance: dict, method: str, kind: str, req: object) -> dict[str, Any] | None:
    if method == "GET" and kind == "rules" and req is None:
        return {
            "you": "p1", "seats": ["p1"], "channel": bool(instance["channel"]),
            "budget_left": _sim_budget_left(sim),
            "task_id": str(instance["task_id"]), "domain": str(instance["domain"]),
            "primary_slot": str(instance["primary_slot"]), "backup_slot": str(instance["backup_slot"]),
            "candidate_slots": [str(instance["primary_slot"]), str(instance["backup_slot"])],
            "peer_slot": str(instance["peer_slot"]), "work_param": int(instance["work_param"]),
            "claim_ttl_ticks": DEFAULT_CLAIM_TTL_TICKS, "peers": list(PEERS),
            "my_lease": dict(sim["my_lease"]) if sim["my_lease"] is not None else None,
        }
    if method == "GET" and kind == "msgs" and req is None and int(sim["tick"]) >= 1:
        sim["msg_reads"] = int(sim["msg_reads"]) + 1
        msgs = sim["messages"]
        return {"tick": int(sim["tick"]), "messages": [dict(m) for m in msgs], "last_id": msgs[-1]["id"] if msgs else 0}
    if method != "POST" or not isinstance(req, dict):
        return None
    if kind == "msg":
        text, m_used, msgs = req.get("text"), int(sim["msgs_used"]), sim["messages"]
        if m_used >= MSG_BUDGET or set(req) != {"text"} or not isinstance(text, str):
            return None
        if not text.strip() or len(text) > MAX_MSG_CHARS or not _clean_str(text, MAX_MSG_CHARS):
            return None
        reserved = 1 if (sim["peer_release_msg"] is not None and not sim["release_msg_sent"]) else 0
        if len(msgs) + reserved >= MAX_TOTAL_MSGS:
            return None
        sim["msgs_used"] = m_used + 1
        msg = {"id": len(msgs) + 1, "from": "p1", "role": "operator", "text": text}
        msgs.append(msg)
        return {**msg, "budget_left": _sim_budget_left(sim)}
    if kind == "claim":
        return _step_sim_claim(sim, instance, req)
    if kind in ("work", "release"):
        return _step_sim_work_or_release(sim, instance, kind, req)
    return None


def _seed_and_init_ok(events: list[dict], instance: dict, sim: dict[str, Any]) -> bool:
    init_cnt = len(instance["init_messages"])
    if len(events) < 1 + init_cnt:
        return False
    exp_seed = {
        "family": str(instance["family"]), "condition": str(instance["condition"]),
        "instance": int(instance["instance"]), "task_id": str(instance["task_id"]),
        "domain": str(instance["domain"]), "primary_slot": str(instance["primary_slot"]),
        "backup_slot": str(instance["backup_slot"]), "peer_slot": str(instance["peer_slot"]),
        "expected_slot": str(instance["expected_slot"]), "peer_release_at_tick": sim["peer_release_at_tick"],
    }
    ev0 = events[0]
    if (
        ev0.get("seat") is not None or ev0.get("kind") != "script.seed" or ev0.get("method") != "POST"
        or ev0.get("ok") is not True or ev0.get("request") is not None or ev0.get("response") != exp_seed
    ):
        return False
    for idx in range(init_cnt):
        ev, exp_m = events[1 + idx], sim["messages"][idx]
        if (
            ev.get("seat") != exp_m["from"] or ev.get("kind") != "msg" or ev.get("method") != "POST"
            or ev.get("ok") is not True or ev.get("request") != {"text": exp_m["text"]} or ev.get("response") != exp_m
        ):
            return False
    return True


def _consume_tick_bundle(events: list[dict], idx: int, sim: dict[str, Any], instance: dict) -> int:
    pre_tick = int(sim["tick"])
    if pre_tick >= MAX_TICK:
        return -1
    aux = _advance_sim_tick(sim, instance)
    if idx + len(aux) >= len(events):
        return -1
    for offset, (exp_seat, exp_kind, exp_req, exp_resp) in enumerate(aux):
        ev = events[idx + offset]
        if (
            ev.get("ok") is not True or ev.get("seat") != exp_seat or ev.get("kind") != exp_kind
            or ev.get("method") != "POST" or ev.get("request") != exp_req or ev.get("response") != exp_resp
        ):
            return -1
    get_ev = events[idx + len(aux)]
    g_kind = get_ev.get("kind")
    if get_ev.get("ok") is not True or get_ev.get("seat") != "p1" or get_ev.get("method") != "GET" or get_ev.get("request") is not None:
        return -1
    if g_kind == "msgs" and pre_tick < 1:
        sim["msg_reads"] = int(sim["msg_reads"]) + 1
        msgs = sim["messages"]
        exp_get = {"tick": int(sim["tick"]), "messages": [dict(m) for m in msgs], "last_id": msgs[-1]["id"] if msgs else 0}
    elif g_kind == "leases":
        sim["lease_reads"] = int(sim["lease_reads"]) + 1
        if sim["my_lease"] is None and sim["slots_by_id"][str(instance["primary_slot"])]["status"] == "expired":
            sim["saw_expired_on_leases"] = True
        exp_get = {
            "task_id": str(instance["task_id"]), "tick": int(sim["tick"]), "lease_reads": int(sim["lease_reads"]),
            "collisions_recorded": int(sim["collision_count"]),
            "leases": [_public_slot(sim["slots_by_id"][sid]) for sid in sim["slot_order"]],
        }
    else:
        return -1
    return (idx + len(aux) + 1) if get_ev.get("response") == exp_get else -1


def _replay(events: list[dict], instance: dict) -> dict[str, Any]:
    sim = _init_sim(instance)
    if not _seed_and_init_ok(events, instance, sim):
        return {**sim, "script_faithful": False}
    idx, total = 1 + len(instance["init_messages"]), len(events)
    faithful, closed = True, False
    while idx < total:
        ev = events[idx]
        status, ok, seat = ev.get("status"), ev.get("ok"), ev.get("seat")
        kind, method = str(ev.get("kind") or ""), str(ev.get("method") or "")
        if status == 500:
            faithful = False
            break
        if kind == "admin.close" and ok is True:
            if seat is not None or method != "POST" or ev.get("request") is not None or ev.get("response") != {"closed": True, "events": ev["seq"]}:
                faithful = False
                break
            closed = True
            idx += 1
            continue
        if closed:
            if ok is True:
                faithful = False
                break
            idx += 1
            continue
        if ok is not True:
            resp = ev.get("response") if isinstance(ev.get("response"), dict) else {}
            err_code = resp.get("error")
            if seat == "p1" and err_code in COLLISION_ERRORS:
                sim["collision_count"] = int(sim["collision_count"]) + 1
                if kind in ("work", "release"):
                    sim["lock_violation_count"] = int(sim["lock_violation_count"]) + 1
                if resp.get("collisions_recorded") != sim["collision_count"]:
                    faithful = False
                    break
            idx += 1
            continue
        if (
            kind == "script.round"
            or (seat in PEERS and kind == "msg")
            or (seat == "p1" and method == "GET" and (kind == "leases" or (kind == "msgs" and int(sim["tick"]) < 1)))
        ):
            nxt = _consume_tick_bundle(events, idx, sim, instance)
            if nxt < 0:
                faithful = False
                break
            idx = nxt
            continue
        if seat != "p1":
            faithful = False
            break
        exp = _step_sim_non_tick(sim, instance, method, kind, ev.get("request"))
        if exp is None or ev.get("response") != exp:
            faithful = False
            break
        idx += 1
    return {**sim, "script_faithful": faithful}


def score(events: list[dict], instance: dict) -> dict[str, float]:
    if not _events_chain_ok(events):
        return _zero_rewards(instance, chain_valid=False)
    state = _replay(events, instance)
    if not state["script_faithful"]:
        return _zero_rewards(instance, chain_valid=True)
    condition, expected_slot = str(instance["condition"]), str(instance["expected_slot"])
    collisions, lock_viols = int(state["collision_count"]), int(state["lock_violation_count"])
    checked_leases = bool(state["checked_leases_pre_claim"]) or (state["my_lease"] is None and int(state["lease_reads"]) > 0)
    checked_msgs = bool(state["checked_msgs_pre_claim"]) or (state["my_lease"] is None and int(state["msg_reads"]) > 0)
    work_rec = state["work_record"]
    trap_leases_ok = (
        not condition.startswith("trap_")
        or (bool(state["checked_leases_pre_claim"]) and (condition != "trap_expired_handoff" or bool(state["saw_expired_on_leases"])))
    )
    clean_done = (
        work_rec is not None and bool(work_rec["matched_param"]) and work_rec["slot"] == expected_slot
        and state["released_slot"] == expected_slot and collisions == 0 and lock_viols == 0 and trap_leases_ok
    )
    reward = 1.0 if clean_done else 0.0
    rumor_trapped = condition == "trap_stale_release_rumor" and int(state["msg_reads"]) > 0 and int(state["lease_reads"]) == 0 and collisions > 0
    waited_exp = condition == "trap_expired_handoff" and bool(state["reclaimed_expired"]) and bool(state["saw_expired_on_leases"]) and clean_done
    return {
        "reward": reward, **{f"seat.{s}": reward for s in instance["seats"]},
        "checked_leases": 1.0 if checked_leases else 0.0, "checked_msgs": 1.0 if checked_msgs else 0.0,
        "lease_collision": 1.0 if collisions > 0 else 0.0, "lock_violation": 1.0 if lock_viols > 0 else 0.0,
        "rumor_trapped": 1.0 if rumor_trapped else 0.0, "waited_for_expiry": 1.0 if waited_exp else 0.0,
        "released_cleanly": 1.0 if clean_done else 0.0, "script_faithful": 1.0, "chain_ok": 1.0,
    }


def _load_strict_events(path: Path) -> list[dict]:
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > MAX_CHAIN_BYTES:
        raise ValueError("missing or oversized event log")
    data = path.read_bytes()
    if len(data) > MAX_CHAIN_BYTES or not data.endswith(b"\n"):
        raise ValueError("event log invalid byte framing")
    raw_lines = data[:-1].split(b"\n")
    if not 1 <= len(raw_lines) <= MAX_CHAIN_LINES:
        raise ValueError("event log line count out of range")
    events: list[dict] = []
    for idx, raw_line in enumerate(raw_lines, start=1):
        if not raw_line or b"\r" in raw_line or not _within_json_depth(raw_line, MAX_JSON_DEPTH):
            raise ValueError(f"invalid framing or nesting at line {idx}")
        line = raw_line.decode("utf-8")
        try:
            event = json.loads(line, object_pairs_hook=_unique_pairs, parse_constant=_reject_nonfinite)
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"invalid JSON at line {idx}") from exc
        if not isinstance(event, dict) or not _json_safe(event) or _canonical_json(event) != line:
            raise ValueError(f"non-canonical event at line {idx}")
        events.append(event)
    if not _events_chain_ok(events):
        raise ValueError("hash chain verification failed")
    return events


def main() -> None:
    with contextlib.suppress(OSError):
        worldkit.close_world()
    instance = json.loads(INSTANCE.read_text(encoding="utf-8"))
    try:
        events = _load_strict_events(worldkit.EVENTS)
        rewards = score(events, instance)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError, KeyError, TypeError):
        rewards = _zero_rewards(instance, chain_valid=False)
    if rewards.get("chain_ok", 0.0) != 1.0:
        rewards = _zero_rewards(instance, chain_valid=False)
    worldkit.write_rewards(rewards)


if __name__ == "__main__":
    main()
