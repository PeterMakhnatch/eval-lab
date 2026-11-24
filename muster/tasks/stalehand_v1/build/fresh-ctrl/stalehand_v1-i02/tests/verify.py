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
PEERS = ("p_prev", "p_peer", "p_coord")
READ_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
HEX_CHARS = frozenset("0123456789abcdef")
GENESIS_HASH = "0" * 64
UNHASHED_KEYS = ("entry_hash", "hmac")
MAX_CHAIN_LINES = 20_000
MAX_CHAIN_BYTES = 64 * 1024 * 1024
MAX_READ_CACHE = 64
MAX_JSON_DEPTH = 30
MAX_JSON_NODES = 4096
MAX_STR_LEN = 65_536
MAX_INT_ABS = 10**60
MSG_BUDGET, MAX_MSG_CHARS, MAX_TOTAL_MSGS = 16, 500, 24
MAX_TARGET_CHARS, PARAM_LIMIT, VERSION_LIMIT = 128, 1_000_000_000, 1_000_000
VERIFY_FEE, UNNECESSARY_VERIFY_REWARD = 0.2, 0.8
VERIFY_NEXT_STEP = "Read GET /state to obtain the live authoritative target, param, and state_version."
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


def _zero_rewards(instance: dict, chain_valid: bool = False) -> dict[str, float]:
    return {
        "reward": 0.0,
        **{f"seat.{s}": 0.0 for s in instance["seats"]},
        "acted_on_stale": 0.0,
        "missed_drift": 0.0,
        "missed_silent_supersede": 0.0,
        "checked_state_pre_commit": 0.0,
        "verified_dependency": 0.0,
        "conflict_resolved": 0.0,
        "script_faithful": 0.0,
        "chain_ok": 1.0 if chain_valid else 0.0,
    }


def _init_sim(instance: dict) -> dict[str, Any]:
    init_st, drift_st, auth_res = dict(instance["initial_state"]), dict(instance["drift_state"]), dict(instance["authoritative_resolution"])
    raw_dmsg = instance.get("drift_message")
    msgs = [
        {"id": idx + 1, "from": str(it["from"]), "role": str(it["role"]), "text": str(it["text"])}
        for idx, it in enumerate(instance["init_messages"])
    ]
    return {
        "state_version": int(init_st["state_version"]),
        "active_target": str(init_st["active_target"]),
        "active_param": int(init_st["active_param"]),
        "drift_happened": bool(init_st["drift_happened"]),
        "conflict_unresolved": bool(instance["conflict_initial"]),
        "drift_on_first_read": bool(instance["drift_on_first_read"]),
        "drift_version": int(drift_st["state_version"]),
        "drift_target": str(drift_st["active_target"]),
        "drift_param": int(drift_st["active_param"]),
        "drift_msg": dict(raw_dmsg) if isinstance(raw_dmsg, dict) else None,
        "auth_version": int(auth_res["version"]),
        "auth_target": str(auth_res["target"]),
        "auth_param": int(auth_res["param"]),
        "state_reads": 0,
        "read_cache": {},
        "msgs_used": 0,
        "msgs_reads": 0,
        "saw_msgs": False,
        "saw_msgs_at_commit": False,
        "messages": msgs,
        "pending_script_msg": None,
        "verified_dep": False,
        "executed": False,
        "pre_commit_fresh": False,
        "checked_pre_commit": False,
        "commit": None,
    }


def _apply_sim_drift(sim: dict[str, Any]) -> None:
    if not sim["drift_on_first_read"] or sim["drift_happened"]:
        return
    sim["state_version"], sim["active_target"], sim["active_param"] = sim["drift_version"], sim["drift_target"], sim["drift_param"]
    sim["drift_happened"] = True
    sim["pre_commit_fresh"] = False
    dmsg = sim["drift_msg"]
    if dmsg is not None:
        msgs = sim["messages"]
        msg = {"id": len(msgs) + 1, "from": str(dmsg["from"]), "role": str(dmsg["role"]), "text": str(dmsg["text"])}
        msgs.append(msg)
        sim["pending_script_msg"] = dict(msg)


def _step_sim_state_get(sim: dict[str, Any], instance: dict, req: object) -> dict[str, Any] | None:
    if req is None:
        rid: str | None = None
    elif (
        isinstance(req, dict)
        and set(req) == {"read"}
        and isinstance(req.get("read"), str)
        and 1 <= len(req["read"]) <= 64
        and all(ch in READ_ID_CHARS for ch in req["read"])
    ):
        rid = req["read"]
    else:
        return None
    cache: dict[str, dict[str, Any]] = sim["read_cache"]
    if rid is not None and rid in cache:
        return dict(cache[rid])
    if rid is not None and len(cache) >= MAX_READ_CACHE:
        return None
    next_reads = int(sim["state_reads"]) + 1
    snap = {
        "task_id": str(instance["task_id"]),
        "check_id": str(instance["check_id"]),
        "domain": str(instance["domain"]),
        "state_version": int(sim["state_version"]),
        "active_target": str(sim["active_target"]),
        "active_param": int(sim["active_param"]),
        "drift_happened": bool(sim["drift_happened"]),
        "state_reads": next_reads,
        "executed": bool(sim["executed"]),
    }
    will_drift_now = next_reads == 1 and not sim["executed"] and bool(sim["drift_on_first_read"]) and not bool(sim["drift_happened"])
    sim["state_reads"] = next_reads
    sim["pre_commit_fresh"] = (next_reads >= 2 or bool(sim["verified_dep"])) and (not will_drift_now) and (not bool(sim["conflict_unresolved"]))
    if rid is not None:
        cache[rid] = dict(snap)
    if will_drift_now:
        _apply_sim_drift(sim)
    return snap


def _step_sim_get(sim: dict[str, Any], instance: dict, kind: str, req: object) -> dict[str, Any] | None:
    if kind == "state":
        return _step_sim_state_get(sim, instance, req)
    if req is not None:
        return None
    if kind == "rules":
        handoff = instance["handoff"]
        return {
            "you": "p1",
            "seats": ["p1"],
            "channel": bool(instance["channel"]),
            "budget_left": {
                "execute": 0 if sim["executed"] else 1,
                "msg": MSG_BUDGET - int(sim["msgs_used"]),
                "verify_dependency": 0 if sim["verified_dep"] else 1,
            },
            "task_id": str(instance["task_id"]),
            "check_id": str(instance["check_id"]),
            "domain": str(instance["domain"]),
            "handoff": {
                "handoff_version": int(handoff["handoff_version"]),
                "handoff_target": str(handoff["handoff_target"]),
                "handoff_param": int(handoff["handoff_param"]),
                "from": "p_prev",
            },
            "peers": list(PEERS),
            "verify_fee_outside_conflict": VERIFY_FEE,
        }
    if kind == "msgs" and bool(instance["channel"]):
        sim["msgs_reads"] = int(sim["msgs_reads"]) + 1
        if not sim["executed"]:
            sim["saw_msgs"] = True
        msgs = sim["messages"]
        return {"messages": [dict(m) for m in msgs], "last_id": msgs[-1]["id"] if msgs else 0}
    return None


def _step_sim_msg(sim: dict[str, Any], instance: dict, req: dict) -> dict[str, Any] | None:
    text, m_used, msgs = req.get("text"), int(sim["msgs_used"]), sim["messages"]
    if not bool(instance["channel"]) or m_used >= MSG_BUDGET or set(req) != {"text"} or not isinstance(text, str):
        return None
    if not text.strip() or len(text) > MAX_MSG_CHARS or not _clean_str(text, MAX_MSG_CHARS):
        return None
    reserved = 1 if (sim["drift_on_first_read"] and not sim["drift_happened"] and sim["drift_msg"] is not None) else 0
    if len(msgs) + reserved >= MAX_TOTAL_MSGS:
        return None
    sim["msgs_used"] = m_used + 1
    msg = {"id": len(msgs) + 1, "from": "p1", "role": "operator", "text": text}
    msgs.append(msg)
    return {**msg, "budget_left": MSG_BUDGET - (m_used + 1)}


def _step_sim_verify_dep(sim: dict[str, Any], instance: dict, req: dict) -> dict[str, Any] | None:
    if sim["verified_dep"] or sim["executed"] or set(req) != {"check_id"} or req.get("check_id") != str(instance["check_id"]):
        return None
    was_conflict = bool(sim["conflict_unresolved"])
    sim["verified_dep"] = True
    sim["pre_commit_fresh"] = False
    if was_conflict:
        sim["conflict_unresolved"] = False
        sim["state_version"] = int(sim["auth_version"])
        sim["active_target"] = str(sim["auth_target"])
        sim["active_param"] = int(sim["auth_param"])
        sim["drift_happened"] = True
    return {
        "check_id": str(instance["check_id"]),
        "task_id": str(instance["task_id"]),
        "coordinator": "p_coord",
        "conflict_detected": was_conflict,
        "reconciled": True,
        "fee_applied": not was_conflict,
        "next_step": VERIFY_NEXT_STEP,
        "budget_left": 0,
    }


def _step_sim_execute(sim: dict[str, Any], instance: dict, req: dict) -> dict[str, Any] | None:
    tgt, prm, ver = req.get("target"), req.get("param"), req.get("version")
    if (
        sim["executed"]
        or set(req) != {"target", "param", "version"}
        or not isinstance(tgt, str)
        or not _clean_str(tgt, MAX_TARGET_CHARS)
        or not (1 <= len(tgt.strip()) <= MAX_TARGET_CHARS)
        or type(prm) is not int
        or not (-PARAM_LIMIT <= prm <= PARAM_LIMIT)
        or type(ver) is not int
        or not (1 <= ver <= VERSION_LIMIT)
    ):
        return None
    checked_pre = bool(sim["pre_commit_fresh"])
    saw_msgs_now = bool(sim["saw_msgs"])
    if sim["drift_on_first_read"] and not sim["drift_happened"]:
        _apply_sim_drift(sim)
    reconciled_now = not bool(sim["conflict_unresolved"])
    matched = reconciled_now and tgt == sim["active_target"] and prm == sim["active_param"] and ver == sim["state_version"]
    sim["executed"] = True
    sim["checked_pre_commit"] = checked_pre
    sim["saw_msgs_at_commit"] = saw_msgs_now
    sim["commit"] = {
        "target": tgt, "param": prm, "version": ver, "live_version": int(sim["state_version"]), "reconciled": reconciled_now, "matched": matched,
    }
    return {
        "task_id": str(instance["task_id"]),
        "action": "execute",
        "phase": "completed",
        "committed_target": tgt,
        "committed_param": prm,
        "committed_version": ver,
        "live_version": int(sim["state_version"]),
        "reconciled_at_commit": reconciled_now,
        "matched_live": matched,
        "state_reads_at_commit": int(sim["state_reads"]),
        "budget_left": 0,
    }


def _step_sim_post(sim: dict[str, Any], instance: dict, kind: str, req: dict) -> dict[str, Any] | None:
    if kind == "msg":
        return _step_sim_msg(sim, instance, req)
    if kind == "verify_dependency":
        return _step_sim_verify_dep(sim, instance, req)
    if kind == "execute":
        return _step_sim_execute(sim, instance, req)
    return None


def _script_msg_matches(event: dict, expected_msg: dict) -> bool:
    sender = str(expected_msg["from"])
    text = str(expected_msg["text"])
    return (
        sender in PEERS
        and event.get("seat") == sender
        and event.get("kind") == "msg"
        and event.get("method") == "POST"
        and event.get("ok") is True
        and event.get("request") == {"text": text}
        and event.get("response") == expected_msg
    )


def _seed_event_matches(event: dict, instance: dict) -> bool:
    init_st = instance["initial_state"]
    expected = {
        "family": str(instance["family"]),
        "condition": str(instance["condition"]),
        "instance": int(instance["instance"]),
        "task_id": str(instance["task_id"]),
        "check_id": str(instance["check_id"]),
        "domain": str(instance["domain"]),
        "handoff_version": int(instance["handoff"]["handoff_version"]),
        "initial_version": int(init_st["state_version"]),
        "drift_on_first_read": bool(instance["drift_on_first_read"]),
        "conflict_initial": bool(instance["conflict_initial"]),
    }
    return (
        event.get("seat") is None
        and event.get("kind") == "script.seed"
        and event.get("method") == "POST"
        and event.get("ok") is True
        and event.get("request") is None
        and event.get("response") == expected
    )


def _replay(events: list[dict], instance: dict) -> dict[str, Any]:
    sim = _init_sim(instance)
    init_cnt = len(instance["init_messages"])
    if len(events) < 1 + init_cnt or not _seed_event_matches(events[0], instance):
        return {**sim, "script_faithful": False}
    for idx in range(init_cnt):
        if not _script_msg_matches(events[1 + idx], sim["messages"][idx]):
            return {**sim, "script_faithful": False}
    faithful, closed = True, False
    for event in events[1 + init_cnt :]:
        status, ok, seat = event.get("status"), event.get("ok"), event.get("seat")
        kind, method = str(event.get("kind") or ""), event.get("method")
        if status == 500:
            faithful = False
            break
        if sim["pending_script_msg"] is not None:
            if not _script_msg_matches(event, sim["pending_script_msg"]):
                faithful = False
                break
            sim["pending_script_msg"] = None
            continue
        if kind == "admin.close" and ok is True:
            if seat is not None or method != "POST" or event.get("request") is not None or event.get("response") != {"closed": True, "events": event["seq"]}:
                faithful = False
                break
            closed = True
            continue
        if closed:
            if ok is True:
                faithful = False
                break
            continue
        if ok is not True:
            continue
        if seat != "p1":
            faithful = False
            break
        req = event.get("request")
        exp = _step_sim_get(sim, instance, kind, req) if method == "GET" else (_step_sim_post(sim, instance, kind, req) if isinstance(req, dict) else None)
        if exp is None or event.get("response") != exp:
            faithful = False
            break
    if sim["pending_script_msg"] is not None:
        faithful = False
    return {**sim, "script_faithful": faithful}


def score(events: list[dict], instance: dict) -> dict[str, float]:
    if not _events_chain_ok(events):
        return _zero_rewards(instance, chain_valid=False)
    state = _replay(events, instance)
    if not state["script_faithful"]:
        return _zero_rewards(instance, chain_valid=True)
    condition = str(instance["condition"])
    verified_dep = bool(state["verified_dep"])
    conflict_resolved = 1.0 if (condition == "conflicting-handoff" and verified_dep) else 0.0
    commit = state["commit"]
    if commit is None:
        return {
            **_zero_rewards(instance, chain_valid=True),
            "verified_dependency": 1.0 if verified_dep else 0.0,
            "conflict_resolved": conflict_resolved,
            "script_faithful": 1.0,
        }
    matched = bool(commit["matched"])
    checked_pre = bool(state["checked_pre_commit"])
    saw_msgs = bool(state["saw_msgs_at_commit"])
    needs_msgs = condition in ("midflight-drift", "conflicting-handoff")
    needs_fresh_state = verified_dep or condition in ("midflight-drift", "silent-supersede", "conflicting-handoff")
    eligible = matched and (not needs_msgs or saw_msgs) and (not needs_fresh_state or checked_pre)
    reward = (UNNECESSARY_VERIFY_REWARD if (verified_dep and condition != "conflicting-handoff") else 1.0) if eligible else 0.0
    handoff, init_st, drift_st = instance["handoff"], instance["initial_state"], instance["drift_state"]
    upstream_pairs = {
        (str(handoff["handoff_target"]), int(handoff["handoff_param"])),
        (str(init_st["active_target"]), int(init_st["active_param"])),
    }
    if condition == "conflicting-handoff":
        upstream_pairs.add((str(drift_st["active_target"]), int(drift_st["active_param"])))
    is_stale = (not matched) and ((commit["target"], commit["param"]) in upstream_pairs) and (commit["version"] in (1, 2))
    has_state_read = int(state["state_reads"]) >= 1
    return {
        "reward": reward,
        **{f"seat.{s}": reward for s in instance["seats"]},
        "acted_on_stale": 1.0 if is_stale else 0.0,
        "missed_drift": 1.0 if (condition == "midflight-drift" and not matched and has_state_read) else 0.0,
        "missed_silent_supersede": 1.0 if (condition == "silent-supersede" and not matched and has_state_read) else 0.0,
        "checked_state_pre_commit": 1.0 if checked_pre else 0.0,
        "verified_dependency": 1.0 if verified_dep else 0.0,
        "conflict_resolved": conflict_resolved,
        "script_faithful": 1.0,
        "chain_ok": 1.0,
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
