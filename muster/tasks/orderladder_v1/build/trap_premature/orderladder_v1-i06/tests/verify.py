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
PEERS = ("p_ops", "p_dep")
READ_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
HEX_CHARS = frozenset("0123456789abcdef")
GENESIS_HASH = "0" * 64
UNHASHED_KEYS = ("entry_hash", "hmac")
MAX_CHAIN_LINES, MAX_CHAIN_BYTES = 20_000, 64 * 1024 * 1024
MAX_READ_CACHE, MAX_JSON_DEPTH, MAX_JSON_NODES = 64, 30, 4096
MAX_STR_LEN, MAX_INT_ABS = 65_536, 10**60
MSG_BUDGET, MAX_MSG_CHARS, MAX_TOTAL_MSGS = 16, 500, 24
MAX_STEP_CHARS, MAX_TOKEN_CHARS = 128, 128
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


def _zero_rewards(instance: dict, chain_valid: bool = False) -> dict[str, float]:
    return {
        "reward": 0.0, **{f"seat.{s}": 0.0 for s in instance["seats"]},
        "completed_all_stages": 0.0, "premature_violation": 0.0, "ordering_violation": 0.0,
        "revoked_prereq_violation": 0.0, "interstage_checked": 0.0, "script_faithful": 0.0,
        "chain_ok": 1.0 if chain_valid else 0.0,
    }


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


def _init_sim(instance: dict) -> dict[str, Any]:
    raw_umsg, raw_rmsg = instance.get("unblock_message"), instance.get("revoke_message")
    msgs = [
        {"id": idx + 1, "from": str(it["from"]), "role": str(it["role"]), "text": str(it["text"])}
        for idx, it in enumerate(instance["init_messages"])
    ]
    return {
        "dag_steps": tuple(str(s) for s in instance["dag_steps"]),
        "token_v1": str(instance["token_v1"]), "token_v2": str(instance["token_v2"]),
        "active_token": str(instance["token_v1"]), "token_version": 1,
        "peer_blocker": bool(instance["peer_blocker_initial"]),
        "prereq_ready": bool(instance["prereq_ready_initial"]),
        "token_revoked": False,
        "unblock_on_second_read": bool(instance["unblock_on_second_read"]),
        "revoke_after_stage1": bool(instance["revoke_after_stage1"]),
        "unblock_msg": dict(raw_umsg) if isinstance(raw_umsg, dict) else None,
        "revoke_msg": dict(raw_rmsg) if isinstance(raw_rmsg, dict) else None,
        "stage_reads": 0, "prereq_reads": 0, "read_cache": {}, "msgs_used": 0,
        "prepared": False, "completed_count": 0, "revocation_emitted": False,
        "violated": None, "messages": msgs, "pending_script_msg": None,
        "saw_stage_before_s1": False,
        "stage_after_s1": False, "msgs_after_s1": False,
        "stage_after_s2": False, "msgs_after_s2": False,
    }


def _emit_sim_peer_msg(sim: dict[str, Any], raw_msg: dict | None) -> None:
    if raw_msg is None:
        return
    msgs = sim["messages"]
    msg = {"id": len(msgs) + 1, "from": str(raw_msg["from"]), "role": str(raw_msg["role"]), "text": str(raw_msg["text"])}
    msgs.append(msg)
    sim["pending_script_msg"] = dict(msg)


def _step_sim_stage_get(sim: dict[str, Any], instance: dict, req: object) -> dict[str, Any] | None:
    if req is None:
        rid: str | None = None
    elif isinstance(req, dict) and set(req) == {"read"} and isinstance(req.get("read"), str) and 1 <= len(req["read"]) <= 64 and all(ch in READ_ID_CHARS for ch in req["read"]):
        rid = req["read"]
    else:
        return None
    cache: dict[str, dict[str, Any]] = sim["read_cache"]
    if rid is not None and rid in cache:
        return json.loads(_canonical_json(cache[rid]))
    if rid is not None and len(cache) >= MAX_READ_CACHE:
        return None
    next_reads = int(sim["stage_reads"]) + 1
    will_unblock = bool(sim["peer_blocker"]) and bool(sim["unblock_on_second_read"]) and next_reads >= 2
    eff_blocker = False if will_unblock else bool(sim["peer_blocker"])
    comp = int(sim["completed_count"])
    snap = {
        "ladder_id": str(instance["ladder_id"]),
        "domain": str(instance["domain"]),
        "stage_reads": next_reads,
        "peer_blocker": eff_blocker,
        "prereq_ready": bool(sim["prereq_ready"]),
        "token_revoked": bool(sim["token_revoked"]),
        "required_token_version": int(sim["token_version"]),
        "next_stage": comp + 1 if comp < 3 else None,
        "completed_stages": list(range(1, comp + 1)),
        "all_completed": comp == 3,
        "stages": _build_stages_view(sim["dag_steps"], comp, eff_blocker, bool(sim["prereq_ready"]), bool(sim["token_revoked"])),
    }
    sim["stage_reads"] = next_reads
    if comp == 0:
        sim["saw_stage_before_s1"] = True
    elif comp == 1:
        sim["stage_after_s1"] = True
    elif comp == 2:
        sim["stage_after_s2"] = True
    if rid is not None:
        cache[rid] = json.loads(_canonical_json(snap))
    if will_unblock:
        sim["peer_blocker"] = False
        _emit_sim_peer_msg(sim, sim["unblock_msg"])
    return snap


def _step_sim_get(sim: dict[str, Any], instance: dict, kind: str, req: object) -> dict[str, Any] | None:
    if kind == "stage":
        return _step_sim_stage_get(sim, instance, req)
    if req is not None:
        return None
    if kind == "rules":
        handoff, comp = instance["handoff"], int(sim["completed_count"])
        return {
            "you": "p1",
            "seats": ["p1"],
            "channel": bool(instance["channel"]),
            "budget_left": {"msg": MSG_BUDGET - int(sim["msgs_used"]), "prepare": 0 if sim["prepared"] else 1, "stage": 3 - comp},
            "ladder_id": str(instance["ladder_id"]),
            "domain": str(instance["domain"]),
            "total_stages": 3,
            "completed_stages": list(range(1, comp + 1)),
            "prepared": bool(sim["prepared"]),
            "handoff": {"from": "p_ops", "listed_steps": [str(s) for s in handoff["listed_steps"]], "hint_token": str(handoff["hint_token"])},
            "peers": list(PEERS),
        }
    if kind == "prereq":
        snap = {
            "ladder_id": str(instance["ladder_id"]),
            "coordinator": "p_dep",
            "prereq_token": str(sim["active_token"]),
            "token_version": int(sim["token_version"]),
            "prereq_ready": True,
            "peer_blocker": bool(sim["peer_blocker"]),
        }
        sim["prereq_ready"], sim["token_revoked"] = True, False
        sim["prereq_reads"] = int(sim["prereq_reads"]) + 1
        return snap
    if kind == "msgs" and bool(instance["channel"]):
        comp = int(sim["completed_count"])
        if comp == 1:
            sim["msgs_after_s1"] = True
        elif comp == 2:
            sim["msgs_after_s2"] = True
        msgs = sim["messages"]
        return {"messages": [dict(m) for m in msgs], "last_id": msgs[-1]["id"] if msgs else 0}
    return None


def _step_sim_msg(sim: dict[str, Any], instance: dict, req: dict) -> dict[str, Any] | None:
    text, m_used, msgs = req.get("text"), int(sim["msgs_used"]), sim["messages"]
    if not bool(instance["channel"]) or m_used >= MSG_BUDGET or set(req) != {"text"} or not isinstance(text, str):
        return None
    if not text.strip() or len(text) > MAX_MSG_CHARS or not _clean_str(text, MAX_MSG_CHARS):
        return None
    res_unb = 1 if (sim["unblock_on_second_read"] and sim["peer_blocker"] and sim["unblock_msg"] is not None) else 0
    res_rev = 1 if (sim["revoke_after_stage1"] and not sim["revocation_emitted"] and sim["revoke_msg"] is not None) else 0
    if len(msgs) + res_unb + res_rev >= MAX_TOTAL_MSGS:
        return None
    sim["msgs_used"] = m_used + 1
    msg = {"id": len(msgs) + 1, "from": "p1", "role": "operator", "text": text}
    msgs.append(msg)
    return {**msg, "budget_left": MSG_BUDGET - (m_used + 1)}


def _step_sim_prepare(sim: dict[str, Any], instance: dict, req: dict) -> dict[str, Any] | None:
    if sim["prepared"] or int(sim["completed_count"]) >= 3 or set(req) != {"ladder_id"} or req.get("ladder_id") != str(instance["ladder_id"]):
        return None
    was_blocked = bool(sim["peer_blocker"])
    will_unblock = was_blocked and bool(sim["unblock_on_second_read"])
    after_blocker = False if will_unblock else was_blocked
    sim["prepared"] = True
    if will_unblock:
        sim["peer_blocker"] = False
        _emit_sim_peer_msg(sim, sim["unblock_msg"])
    return {
        "ladder_id": str(instance["ladder_id"]),
        "coordinator": "p_dep",
        "was_blocked": was_blocked,
        "peer_blocker": after_blocker,
        "unblocked": not after_blocker,
        "budget_left": 0,
    }


def _step_sim_stage(sim: dict[str, Any], req: dict) -> dict[str, Any] | None:
    stage_num, step, tok = req.get("stage"), req.get("step"), req.get("prereq_token")
    comp, dag = int(sim["completed_count"]), sim["dag_steps"]
    if (
        comp >= 3
        or sim["violated"] is not None
        or set(req) != {"stage", "step", "prereq_token"}
        or type(stage_num) is not int
        or stage_num != comp + 1
        or not isinstance(step, str)
        or not _clean_str(step, MAX_STEP_CHARS)
        or step != dag[stage_num - 1]
        or not isinstance(tok, str)
        or not _clean_str(tok, MAX_TOKEN_CHARS)
        or bool(sim["peer_blocker"])
        or bool(sim["token_revoked"])
        or not bool(sim["prereq_ready"])
        or tok != sim["active_token"]
    ):
        return None
    new_comp, ver_now = comp + 1, int(sim["token_version"])
    sim["completed_count"] = new_comp
    if new_comp == 1 and bool(sim["revoke_after_stage1"]) and not bool(sim["revocation_emitted"]):
        sim["revocation_emitted"], sim["token_revoked"], sim["prereq_ready"] = True, True, False
        sim["token_version"], sim["active_token"] = 2, sim["token_v2"]
        _emit_sim_peer_msg(sim, sim["revoke_msg"])
    return {
        "action": "stage",
        "stage": stage_num,
        "step": step,
        "item_id": step,
        "token_version": ver_now,
        "completed_stages": list(range(1, new_comp + 1)),
        "phase": "completed" if new_comp == 3 else "in_progress",
        "budget_left": 3 - new_comp,
    }


def _step_sim_post(sim: dict[str, Any], instance: dict, kind: str, req: dict) -> dict[str, Any] | None:
    if kind == "msg":
        return _step_sim_msg(sim, instance, req)
    if kind == "prepare":
        return _step_sim_prepare(sim, instance, req)
    return _step_sim_stage(sim, req) if kind == "stage" else None


def _step_sim_conflict_409(sim: dict[str, Any], kind: str, resp: object) -> bool:
    if not isinstance(resp, dict):
        return False
    comp = int(sim["completed_count"])
    if kind == "prepare":
        return comp >= 3 and not bool(sim["prepared"]) and resp == {"error": "already_completed"}
    if kind != "stage" or comp >= 3:
        return False
    err, ver = resp.get("error"), int(sim["token_version"])
    if sim["violated"] is not None:
        return resp == {"error": "ladder_halted", "violation": sim["violated"]}
    if err == "stage_already_completed":
        st_n = resp.get("stage")
        return type(st_n) is int and 1 <= st_n <= comp and resp == {"error": "stage_already_completed", "stage": st_n}
    blocker = bool(sim["peer_blocker"])
    if err == "peer_blocker_active":
        ok_b = blocker and resp == {"error": "peer_blocker_active", "next_stage": comp + 1}
        if ok_b:
            sim["violated"] = err
        return ok_b
    if blocker:
        return False
    if err == "dependency_order_violation":
        ok_d = resp == {"error": "dependency_order_violation", "expected_stage": comp + 1}
        if ok_d:
            sim["violated"] = err
        return ok_d
    token_checks = {
        "revoked_prereq_token": bool(sim["token_revoked"]) or bool(sim["revocation_emitted"]),
        "prereq_not_acquired": not bool(sim["token_revoked"]) and not bool(sim["prereq_ready"]),
        "invalid_prereq_token": not bool(sim["token_revoked"]) and bool(sim["prereq_ready"]),
    }
    if err in token_checks and token_checks[err] and resp == {"error": err, "required_token_version": ver}:
        sim["violated"] = err
        return True
    return False


def _script_msg_matches(event: dict, expected_msg: dict) -> bool:
    sender, text = str(expected_msg["from"]), str(expected_msg["text"])
    return (
        sender in PEERS and event.get("seat") == sender and event.get("kind") == "msg"
        and event.get("method") == "POST" and event.get("ok") is True
        and event.get("request") == {"text": text} and event.get("response") == expected_msg
    )


def _seed_event_matches(event: dict, instance: dict) -> bool:
    expected = {
        "family": str(instance["family"]), "condition": str(instance["condition"]),
        "instance": int(instance["instance"]), "ladder_id": str(instance["ladder_id"]),
        "domain": str(instance["domain"]), "dag_steps": [str(s) for s in instance["dag_steps"]],
        "peer_blocker_initial": bool(instance["peer_blocker_initial"]),
        "prereq_ready_initial": bool(instance["prereq_ready_initial"]),
        "unblock_on_second_read": bool(instance["unblock_on_second_read"]),
        "revoke_after_stage1": bool(instance["revoke_after_stage1"]),
    }
    return (
        event.get("seat") is None and event.get("kind") == "script.seed" and event.get("method") == "POST"
        and event.get("ok") is True and event.get("request") is None and event.get("response") == expected
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
            if (
                seat == "p1"
                and method == "POST"
                and status == 409
                and kind in ("stage", "prepare")
                and not _step_sim_conflict_409(sim, kind, event.get("response"))
            ):
                faithful = False
                break
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
    condition, violated = str(instance["condition"]), state["violated"]
    completed_all = int(state["completed_count"]) == 3 and violated is None
    interstage_ok = (
        bool(state["stage_after_s1"])
        and bool(state["msgs_after_s1"])
        and bool(state["stage_after_s2"])
        and bool(state["msgs_after_s2"])
    )
    needs_init_stage = condition != "ctrl_ready"
    eligible = completed_all and (not needs_init_stage or bool(state["saw_stage_before_s1"])) and (condition != "trap_revoked_prereq" or interstage_ok)
    reward = 1.0 if eligible else 0.0
    return {
        "reward": reward,
        **{f"seat.{s}": reward for s in instance["seats"]},
        "completed_all_stages": 1.0 if completed_all else 0.0,
        "premature_violation": 1.0 if violated in ("peer_blocker_active", "prereq_not_acquired") else 0.0,
        "ordering_violation": 1.0 if violated == "dependency_order_violation" else 0.0,
        "revoked_prereq_violation": 1.0 if violated in ("revoked_prereq_token", "invalid_prereq_token") else 0.0,
        "interstage_checked": 1.0 if interstage_ok else 0.0,
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
