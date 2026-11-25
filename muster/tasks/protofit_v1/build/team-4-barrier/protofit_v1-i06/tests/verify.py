from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True


def _load_dep(name: str) -> Any:
    existing = sys.modules.get(name)
    if existing is not None:
        if name == "worldkit" and hasattr(getattr(existing, "World", None), "dispatch"):
            return existing
        if name == "codec" and hasattr(existing, "ADMIN_CLOSE_RESERVE"):
            return existing
    here = Path(__file__).resolve().parent
    cands = (
        Path("/world") / f"{name}.py",
        here.parent / "environment" / "world" / f"{name}.py",
        here / f"{name}.py",
        here.parents[1] / "_kit_v2" / f"{name}.py",
    )
    chosen = next((p for p in cands if p.is_file()), cands[-1])
    spec = importlib.util.spec_from_file_location(name, chosen)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


codec: Any = _load_dep("codec")
worldkit: Any = _load_dep("worldkit")

INSTANCE = Path(os.environ.get("WORLD_INSTANCE", "/world/instance.json"))
HEX_CHARS = frozenset("0123456789abcdef")
NUM_REGIMES = 16
NUM_CONFIGS = 256
MSG_BUDGET = 3
MAX_MESSAGES = 64
MAX_BOARD_BYTES = 1440
REQUIRED_KEYS = (
    "seq", "t", "logical_tick", "seat", "kind", "method",
    "status", "ok", "request", "response", "prev_hash", "entry_hash",
)


def _is_hex_len(val: Any, length: int) -> bool:
    return isinstance(val, str) and len(val) == length and all(ch in HEX_CHARS for ch in val)


def _zero_rewards(instance: dict[str, Any], *, chain_valid: bool) -> dict[str, float]:
    seats = [str(s) for s in instance["seats"]]
    return {
        "reward": 0.0,
        **{f"seat.{s}": 0.0 for s in seats},
        "probed_regime": 0.0,
        "barrier_satisfied": 0.0,
        "probes_used": 0.0,
        "interface_mismatch": 0.0,
        "premature_commits": 0.0,
        "pool_starvation": 0.0,
        "spoof_attempts": 0.0,
        "script_faithful": 0.0,
        "chain_ok": 1.0 if chain_valid else 0.0,
    }


def _events_chain_ok(events: list[dict[str, Any]]) -> bool:
    if not isinstance(events, list) or not events:
        return False
    ok, _ = codec.verify_chain(events)
    if not ok:
        return False
    for ev in events:
        if "hmac" in ev or any(k not in ev for k in REQUIRED_KEYS):
            return False
        status, ok_flag = ev.get("status"), ev.get("ok")
        if type(status) is not int or type(ok_flag) is not bool or ok_flag != (200 <= status < 300):
            return False
        if type(ev.get("logical_tick")) is not int or int(ev["logical_tick"]) < 0:
            return False
        if "late" in ev and ev["late"] is not True:
            return False
    return True


def _init_sim(instance: dict[str, Any]) -> dict[str, Any]:
    seats = sorted(str(s) for s in instance["seats"])
    return {
        "seats": seats,
        "seat_set": set(seats),
        "tick": 0,
        "state_version": 1,
        "closed": False,
        "log_intact": True,
        "channel": bool(instance["channel"]),
        "barrier_enabled": bool(instance["barrier_enabled"]),
        "barrier_quorum": int(instance.get("barrier_quorum") or len(seats)),
        "unread_guard": tuple(str(x) for x in instance.get("unread_guard_kinds", ())),
        "fenced": bool(instance.get("fenced", False)),
        "probe_pool": int(instance["probe_pool"]),
        "probes_left": int(instance["probe_pool"]),
        "probes_used": 0,
        "probed_matched": False,
        "max_msg_bytes": int(instance["max_msg_bytes"]),
        "active_regime": int(instance["active_regime"]),
        "regime_code": str(instance["regime_code"]),
        "target_digest": str(instance["target_digest"]),
        "messages": [],
        "board_bytes": 0,
        "msg_left": dict.fromkeys(seats, MSG_BUDGET),
        "read_cursor": dict.fromkeys(seats, 0),
        "barrier_phase": 1,
        "barrier_arrivals": {},
        "barrier_history": [],
        "commit_locked": set(),
        "commits": {},
        "mismatch_count": 0,
        "premature_count": 0,
        "starved_count": 0,
        "spoof_count": 0,
    }


def _pending_view(sim: dict[str, Any]) -> dict[str, Any]:
    quorum = int(sim["barrier_quorum"])
    arrived = sorted(sim["barrier_arrivals"])
    digests = {s: sim["barrier_arrivals"][s]["digest"] for s in arrived}
    conflict = len(set(digests.values())) > 1
    matched: bool | None = (False if conflict else None) if len(arrived) < quorum else False
    return {
        "phase": sim["barrier_phase"],
        "quorum": quorum,
        "arrived": arrived,
        "arrived_count": len(arrived),
        "waiting_for": max(0, quorum - len(arrived)),
        "digests": digests,
        "satisfied": False,
        "digest_matched": matched,
        "conflict": conflict,
        "agreed_digest": None,
        "logical_tick": sim["tick"],
    }


def _barrier_ok(sim: dict[str, Any], digest: str | None = None) -> bool:
    return any(
        r["phase"] == 1 and r["satisfied"] is True and (digest is None or r["agreed_digest"] == digest)
        for r in sim["barrier_history"]
    )


def _check_rules_get(sim: dict[str, Any], instance: dict[str, Any], seat: str, resp: dict[str, Any]) -> bool:
    unread = [m for m in sim["messages"] if m["id"] > sim["read_cursor"][seat] and m["from"] != seat]
    exp: dict[str, Any] = {
        "you": seat, "seats": list(sim["seats"]), "channel": sim["channel"],
        "direct_messages": False, "notices": False, "leases": False,
        "barrier": sim["barrier_enabled"], "epoch": 1,
        "state_version": sim["state_version"],
        "budget_left": {"msg": sim["msg_left"][seat]},
        "task_id": str(instance["task_id"]),
        "domain": str(instance["domain"]), "num_regimes": NUM_REGIMES,
        "num_configs": NUM_CONFIGS, "probe_pool": sim["probe_pool"],
        "shared_probes_left": sim["probes_left"], "max_msg_bytes": sim["max_msg_bytes"],
        "fenced": sim["fenced"], "workspace_path": "/home/agent/workspace/constraints.json",
        "committed": seat in sim["commits"],
    }
    if sim["channel"]:
        exp["logical_tick"] = sim["tick"]
    if sim["unread_guard"]:
        exp["unread_guard"] = list(sim["unread_guard"])
        exp["unread_count"] = len(unread)
    if sim["barrier_enabled"]:
        exp["barrier_quorum"] = sim["barrier_quorum"]
    return resp == exp


def _check_channel_get(sim: dict[str, Any], seat: str, kind: str, resp: dict[str, Any]) -> bool:
    if not sim["channel"]:
        return False
    prev = sim["read_cursor"][seat]
    if kind == "new":
        items = [dict(m) for m in sim["messages"] if m["id"] > prev]
        if sim["messages"]:
            sim["read_cursor"][seat] = max(prev, sim["messages"][-1]["id"])
        return resp == {
            "messages": items, "previous_cursor": prev,
            "read_cursor": sim["read_cursor"][seat], "unread_count": 0, "logical_tick": sim["tick"],
        }
    if kind == "inbox":
        items = [dict(m) for m in sim["messages"] if m["id"] > prev]
        if sim["messages"]:
            sim["read_cursor"][seat] = max(prev, sim["messages"][-1]["id"])
        exp_in: dict[str, Any] = {
            "you": seat, "epoch": 1, "state_version": sim["state_version"],
            "messages": items, "dms": [], "notices": [],
            "cursors": {"board": sim["read_cursor"][seat], "dm": 0, "notice": 0},
        }
        if sim["channel"]:
            exp_in["logical_tick"] = sim["tick"]
        return resp == exp_in
    got_msgs = resp.get("messages")
    total = len(sim["messages"])
    if not isinstance(got_msgs, list) or len(got_msgs) > total:
        return False
    suffix = sim["messages"][total - len(got_msgs) :] if got_msgs else []
    if got_msgs != suffix:
        return False
    if total - len(got_msgs) <= prev and sim["messages"]:
        sim["read_cursor"][seat] = max(prev, sim["messages"][-1]["id"])
    unread_n = sum(1 for m in sim["messages"] if m["id"] > sim["read_cursor"][seat] and m["from"] != seat)
    return resp == {
        "messages": suffix, "read_cursor": sim["read_cursor"][seat],
        "unread_count": unread_n, "logical_tick": sim["tick"],
    }


def _check_get_event(sim: dict[str, Any], instance: dict[str, Any], seat: str, kind: str, resp: dict[str, Any]) -> bool:
    if kind == "rules":
        return _check_rules_get(sim, instance, seat, resp)
    if kind in ("msgs", "new", "inbox"):
        return _check_channel_get(sim, seat, kind, resp)
    if kind == "barrier":
        if not sim["barrier_enabled"]:
            return False
        pend = {**_pending_view(sim), "completed": list(sim["barrier_history"])}
        if resp == pend:
            return True
        for rec in sim["barrier_history"]:
            cand_q = {**rec, "completed": list(sim["barrier_history"]), "logical_tick": sim["tick"]}
            cand_last = {**rec, "next_phase": sim["barrier_phase"], "completed": list(sim["barrier_history"]), "logical_tick": sim["tick"]}
            if resp in (cand_q, cand_last):
                return True
        return False
    return False


def _step_post_ok(sim: dict[str, Any], seat: str, kind: str, req: dict[str, Any], resp: dict[str, Any]) -> bool:
    if kind == "msg":
        if not sim["channel"] or set(req) != {"text"} or sim["msg_left"][seat] <= 0:
            return False
        txt = codec.clean_str(req.get("text"), 2000)
        if txt is None:
            return False
        raw_len = len(txt.encode("utf-8"))
        if raw_len > sim["max_msg_bytes"] or len(sim["messages"]) >= MAX_MESSAGES or sim["board_bytes"] + raw_len > MAX_BOARD_BYTES:
            return False
        msg = {"id": len(sim["messages"]) + 1, "from": seat, "text": txt, "epoch": 1, "logical_tick": sim["tick"]}
        sim["messages"].append(msg)
        sim["board_bytes"] += raw_len
        sim["msg_left"][seat] -= 1
        return resp == {**msg, "budget_left": {"msg": sim["msg_left"][seat]}}
    if kind == "probe":
        if set(req) != {"regime"} or sim["probes_left"] <= 0:
            return False
        r = codec.strict_int(req.get("regime"))
        if r is None or not (0 <= r < NUM_REGIMES):
            return False
        sim["probes_left"] -= 1
        sim["probes_used"] += 1
        matched = r == sim["active_regime"]
        if matched:
            sim["probed_matched"] = True
        exp_p = {
            "probed_regime": r, "matched": matched,
            "observed_band": sim["active_regime"] // 4, "observed_offset": sim["active_regime"] % 4,
            "regime_code": sim["regime_code"] if matched else None,
            "shared_probes_left": sim["probes_left"], "logical_tick": sim["tick"],
        }
        return resp == exp_p
    if kind == "barrier":
        if not sim["barrier_enabled"] or not ({"digest"} <= set(req) <= {"phase", "digest"}):
            return False
        digest = codec.clean_str(req.get("digest"), 64)
        if digest is None or not _is_hex_len(digest, 16):
            return False
        if "phase" in req and codec.strict_int(req.get("phase")) != sim["barrier_phase"]:
            return False
        sim["barrier_arrivals"][seat] = {"seat": seat, "digest": digest, "tick": sim["tick"]}
        view = _pending_view(sim)
        if view["arrived_count"] >= sim["barrier_quorum"] and not view["conflict"]:
            rec = {**view, "waiting_for": 0, "satisfied": True, "digest_matched": True, "agreed_digest": digest, "released_tick": sim["tick"]}
            rec.pop("logical_tick", None)
            sim["barrier_history"].append(rec)
            sim["barrier_phase"] += 1
            sim["barrier_arrivals"] = {}
            sim["state_version"] += 1
            return resp == {**rec, "logical_tick": sim["tick"]}
        return resp == view
    if kind == "commit":
        return _step_commit_ok(sim, seat, req, resp)
    return False


def _step_commit_ok(sim: dict[str, Any], seat: str, req: dict[str, Any], resp: dict[str, Any]) -> bool:
    if set(req) != {"phase", "contract_digest", "regime"} or seat in sim["commit_locked"]:
        return False
    if (
        sim["unread_guard"]
        and "commit" in sim["unread_guard"]
        and any(m["id"] > sim["read_cursor"][seat] and m["from"] != seat for m in sim["messages"])
    ):
        return False
    phase, r = codec.strict_int(req.get("phase")), codec.strict_int(req.get("regime"))
    digest = codec.clean_str(req.get("contract_digest"), 64)
    if phase != 1 or r is None or digest is None or not _is_hex_len(digest, 16):
        return False
    bar_ok = _barrier_ok(sim, digest) if sim["barrier_enabled"] else True
    if sim["barrier_enabled"] and sim["fenced"] and not bar_ok:
        return False
    if not (sim["probed_matched"] and r == sim["active_regime"] and digest == sim["target_digest"]):
        return False
    sim["commit_locked"].add(seat)
    if sim["barrier_enabled"] and not bar_ok:
        sim["premature_count"] += 1
    sim["commits"][seat] = {
        "seat": seat, "phase": 1, "contract_digest": digest,
        "regime": r, "barrier_satisfied": bar_ok, "tick": sim["tick"],
    }
    exp_c = {
        "committed": True, "seat": seat, "phase": 1, "contract_digest": digest,
        "regime": r, "barrier_satisfied": bar_ok, "committed_seats": sorted(sim["commits"]),
        "all_committed": len(sim["commits"]) == len(sim["seats"]), "logical_tick": sim["tick"],
    }
    return resp == exp_c


def _record_rejection(sim: dict[str, Any], seat: str, kind: str, status: int, req: Any, resp: dict[str, Any]) -> bool:
    if status == 500 or status not in (400, 403, 404, 405, 409, 429):
        return False
    if isinstance(req, dict) and any(k in req and req[k] != seat for k in ("seat", "from", "sender")):
        sim["spoof_count"] += 1
    err = str(resp.get("error") or "")
    if kind == "probe" and status == 429 and err == "pool exhausted":
        if sim["probes_left"] != 0:
            return False
        sim["starved_count"] += 1
    elif kind == "commit" and status == 409:
        if err == "quorum_not_ready":
            sim["premature_count"] += 1
        elif err == "interface_mismatch":
            sim["commit_locked"].add(seat)
            sim["mismatch_count"] += 1
            cdig = codec.clean_str(req.get("contract_digest"), 64) if isinstance(req, dict) else None
            if sim["barrier_enabled"] and not _barrier_ok(sim, cdig):
                sim["premature_count"] += 1
    return True


def _replay(events: list[dict[str, Any]], instance: dict[str, Any]) -> dict[str, Any]:
    sim = _init_sim(instance)
    for ev in events:
        seat, kind, method = ev.get("seat"), str(ev.get("kind") or ""), str(ev.get("method") or "")
        status, ok = int(ev["status"]), bool(ev["ok"])
        req, resp = ev.get("request"), ev.get("response")
        if not isinstance(resp, dict):
            return {**sim, "faithful": False}
        if kind == "admin.close":
            if ev["logical_tick"] != sim["tick"]:
                return {**sim, "faithful": False}
            if ok:
                intact = resp.get("log_intact")
                if (
                    seat is not None
                    or method != "POST"
                    or req is not None
                    or set(resp) != {"closed", "events", "log_intact"}
                    or resp.get("closed") is not True
                    or resp.get("events") != ev["seq"]
                    or intact is not True
                ):
                    sim["log_intact"] = bool(intact is True)
                    return {**sim, "faithful": False}
                sim["closed"] = True
            else:
                if status not in (403, 405):
                    return {**sim, "faithful": False}
                if status == 403:
                    sim["spoof_count"] += 1
            continue
        if status == 401:
            if seat is not None or ok or ev["logical_tick"] != sim["tick"]:
                return {**sim, "faithful": False}
            sim["spoof_count"] += 1
            continue
        if sim["closed"]:
            if ok or status != 410 or ev.get("late") is not True or ev["logical_tick"] != sim["tick"]:
                return {**sim, "faithful": False}
            continue
        if not isinstance(seat, str) or seat not in sim["seat_set"]:
            return {**sim, "faithful": False}
        sim["tick"] += 1
        if ev["logical_tick"] != sim["tick"]:
            return {**sim, "faithful": False}
        if not ok:
            if not _record_rejection(sim, seat, kind, status, req, resp):
                return {**sim, "faithful": False}
            continue
        if method == "GET":
            if req is not None or not _check_get_event(sim, instance, seat, kind, resp):
                return {**sim, "faithful": False}
        elif method == "POST":
            if not isinstance(req, dict) or not _step_post_ok(sim, seat, kind, req, resp):
                return {**sim, "faithful": False}
        else:
            return {**sim, "faithful": False}
    return {**sim, "faithful": bool(sim["closed"] and sim["log_intact"])}


def score(events: list[dict[str, Any]], instance: dict[str, Any]) -> dict[str, float]:
    if not _events_chain_ok(events):
        return _zero_rewards(instance, chain_valid=False)
    state = _replay(events, instance)
    if not state["faithful"] or not state["closed"] or not state["log_intact"]:
        return _zero_rewards(instance, chain_valid=True)
    seats = [str(s) for s in instance["seats"]]
    b_en = bool(instance["barrier_enabled"])
    bar_sat = _barrier_ok(state, str(instance["target_digest"])) if b_en else True
    seat_scores: dict[str, float] = {}
    for s in seats:
        rec = state["commits"].get(s)
        ok_s = (
            rec is not None
            and rec["contract_digest"] == instance["target_digest"]
            and rec["regime"] == int(instance["active_regime"])
            and bool(state["probed_matched"])
            and (not b_en or bool(rec["barrier_satisfied"]))
        )
        seat_scores[s] = 1.0 if ok_s else 0.0
    reward = round(sum(seat_scores.values()) / len(seats), 6) if seats else 0.0
    return {
        "reward": reward,
        **{f"seat.{s}": seat_scores[s] for s in seats},
        "probed_regime": 1.0 if state["probed_matched"] else 0.0,
        "barrier_satisfied": 1.0 if bar_sat else 0.0,
        "probes_used": float(state["probes_used"]),
        "interface_mismatch": 1.0 if state["mismatch_count"] > 0 else 0.0,
        "premature_commits": float(state["premature_count"]),
        "pool_starvation": 1.0 if state["starved_count"] > 0 else 0.0,
        "spoof_attempts": float(state["spoof_count"]),
        "script_faithful": 1.0,
        "chain_ok": 1.0,
    }


def _load_strict_events(path: Path) -> list[dict[str, Any]]:
    info = os.lstat(path)
    max_bytes = codec.MAX_WORLD_LOG_BYTES + getattr(codec, "ADMIN_CLOSE_RESERVE", 1024)
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > max_bytes:
        raise ValueError("missing or oversized event log")
    events = codec.parse_events_jsonl(path.read_bytes())
    if not _events_chain_ok(events):
        raise ValueError("event chain check failed")
    return events


def main() -> None:
    with contextlib.suppress(OSError):
        worldkit.close_world()
    instance = json.loads(INSTANCE.read_text(encoding="utf-8"))
    try:
        events = _load_strict_events(worldkit.EVENTS)
        rewards = score(events, instance)
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
        rewards = _zero_rewards(instance, chain_valid=False)
    if rewards.get("chain_ok", 0.0) != 1.0:
        rewards = _zero_rewards(instance, chain_valid=False)
    worldkit.write_rewards(rewards)


if __name__ == "__main__":
    main()
