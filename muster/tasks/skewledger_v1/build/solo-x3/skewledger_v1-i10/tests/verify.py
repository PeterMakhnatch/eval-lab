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
        if name == "codec" and hasattr(existing, "ADMIN_CLOSE_RESERVE") and hasattr(existing, "verify_chain"):
            return existing
    here = Path(__file__).resolve().parent
    cands = [
        here / f"{name}.py",
        here.parent / "environment" / "world" / f"{name}.py",
        Path("/world") / f"{name}.py",
    ]
    if len(here.parents) >= 2:
        cands.append(here.parents[1] / "_kit_v2" / f"{name}.py")
    for cand in cands:
        if cand.is_file():
            spec = importlib.util.spec_from_file_location(name, cand)
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError(f"missing dependency {name}")


codec = _load_dep("codec")
worldkit = _load_dep("worldkit")

INSTANCE = Path("/world/instance.json")
ACCOUNTS = ("acct_a", "acct_b")
LEASE_TARGETS = frozenset({"acct_a", "acct_b", "settlement"})
TRANSFER_KEYS = frozenset({"account", "amount", "expected_version", "lease_id"})
COMMIT_KEYS = frozenset({"total_reserve", "official_rate", "expected_version", "lease_id"})
OK_GET_KINDS = frozenset({"rules", "state", "rates", "msgs", "new", "notices", "inbox", "leases", "claims"})
OK_POST_KINDS = frozenset({"claim", "release", "transfer", "commit", "msg"})
MAX_AMOUNT = 1_000_000
MAX_RESERVE = 10_000_000


def _zero_rewards(instance: dict[str, Any], *, chain_ok: bool, faithful: bool = False) -> dict[str, float]:
    seats = [str(s) for s in instance.get("seats", ())]
    return {
        "reward": 0.0,
        **{f"seat.{s}": 0.0 for s in seats},
        "reserve_preserved": 0.0,
        "allocation_exact": 0.0,
        "rates_verified": 0.0,
        "settled_cleanly": 0.0,
        "write_skew_breach": 0.0,
        "stale_commits": 0.0,
        "rumor_adopted": 0.0,
        "spoof_attempts": 0.0,
        "script_faithful": 1.0 if faithful else 0.0,
        "chain_ok": 1.0 if chain_ok else 0.0,
    }


def _init_sim(instance: dict[str, Any]) -> dict[str, Any]:
    return {
        "seats": [str(s) for s in instance["seats"]],
        "channel": bool(instance.get("channel", False)),
        "leases_enabled": bool(instance.get("leases_enabled", False)),
        "bal_a": int(instance["init_bal_a"]),
        "bal_b": int(instance["init_bal_b"]),
        "reserve_floor": int(instance["reserve_floor"]),
        "fenced": bool(instance.get("fenced", False)),
        "state_version": 1,
        "rates_mutated": False,
        "current_rate": str(instance["initial_rate"]),
        "post_rate": str(instance["post_transfer_rate"]),
        "rumor_rate": str(instance["rumor_rate"]),
        "acct_a_done": False,
        "acct_b_done": False,
        "withdrawn_a": 0,
        "withdrawn_b": 0,
        "settled": False,
        "committed_reserve": None,
        "committed_rate": None,
        "stale_commits": 0,
        "rumor_hits": 0,
        "spoof_attempts": 0,
        "state_reads": {},
        "pending_cas": {},
        "post_mut_rate_readers": set(),
        "leases": {},
        "lease_seq": 0,
    }


def _allowed_sim_targets(seat: str) -> frozenset[str]:
    if seat == "p1":
        return LEASE_TARGETS
    if seat in ACCOUNTS:
        return frozenset({seat})
    if seat == "auditor":
        return frozenset({"settlement"})
    return frozenset()


def _clean_opt_lease_id(req: dict[str, Any]) -> tuple[str | None, bool]:
    if "lease_id" not in req or req["lease_id"] is None:
        return None, True
    lid = codec.clean_str(req.get("lease_id"), 128)
    return lid, lid is not None


def _parse_sim_target(req: dict[str, Any]) -> str | None:
    bx = codec.strict_int(req.get("box")) if "box" in req else None
    raw = req.get("target") if "target" in req else (req.get("slot") if "slot" in req else (str(bx) if bx is not None else None))
    return codec.clean_str(raw, 128)


def _expire_sim_leases(sim: dict[str, Any], tick: int) -> None:
    leases = sim["leases"]
    for tgt in [k for k, v in leases.items() if tick >= int(v["expires_tick"])]:
        del leases[tgt]


def _seed_ok(ev0: dict[str, Any], instance: dict[str, Any]) -> bool:
    exp = {
        "task_id": str(instance["task_id"]),
        "domain": str(instance["domain"]),
        "init_bal_a": int(instance["init_bal_a"]),
        "init_bal_b": int(instance["init_bal_b"]),
        "reserve_floor": int(instance["reserve_floor"]),
        "fenced": bool(instance.get("fenced", False)),
        "state_version": 1,
    }
    return (
        ev0.get("seat") is None
        and ev0.get("kind") == "script.seed"
        and ev0.get("method") == "POST"
        and ev0.get("ok") is True
        and ev0.get("request") is None
        and ev0.get("response") == exp
    )


def _apply_sim_lease(sim: dict[str, Any], seat: str, kind: str, req: dict[str, Any], resp: dict[str, Any], tick: int) -> bool:
    if not sim["leases_enabled"]:
        return False
    leases = sim["leases"]
    target = _parse_sim_target(req)
    if kind == "claim":
        if target not in _allowed_sim_targets(seat):
            return False
        ttl = codec.strict_int(req.get("ttl_ticks")) if "ttl_ticks" in req else 32
        if ttl is None or not (1 <= ttl <= 32):
            return False
        existing = leases.get(target)
        if existing is not None:
            if existing["holder"] != seat:
                return False
            existing["expires_tick"] = tick + ttl
            return resp.get("lease_id") == existing["lease_id"]
        max_per = 3 if sim["seats"] == ["p1"] else 1
        if sum(1 for item in leases.values() if item["holder"] == seat) >= max_per or len(leases) >= 16:
            return False
        sim["lease_seq"] = int(sim["lease_seq"]) + 1
        lid = f"lease-{int(sim['lease_seq']):04d}"
        leases[target] = {"holder": seat, "lease_id": lid, "expires_tick": tick + ttl}
        return resp.get("lease_id") == lid and resp.get("holder") == seat
    if target not in LEASE_TARGETS:
        return False
    existing = leases.get(target)
    if existing is None or existing["holder"] != seat:
        return False
    rel_lid, lid_ok = _clean_opt_lease_id(req)
    if not lid_ok or (rel_lid is not None and rel_lid != existing["lease_id"]):
        return False
    del leases[target]
    return resp.get("released") == target


def _apply_sim_transfer(sim: dict[str, Any], seat: str, req: dict[str, Any], resp: dict[str, Any]) -> bool:
    if not (set(req) <= TRANSFER_KEYS):
        return False
    account = codec.clean_str(req.get("account"), 32)
    amount = codec.strict_int(req.get("amount"))
    exp_ver = codec.strict_int(req.get("expected_version"))
    lid, lid_ok = _clean_opt_lease_id(req)
    if not lid_ok or account not in ACCOUNTS or amount is None or not (1 <= amount <= MAX_AMOUNT) or exp_ver is None or exp_ver < 1:
        return False
    if seat == "auditor" or (seat in ACCOUNTS and seat != account) or bool(sim["settled"]):
        return False
    if (account == "acct_a" and sim["acct_a_done"]) or (account == "acct_b" and sim["acct_b_done"]):
        return False
    if sim["fenced"]:
        held = sim["leases"].get(account)
        if held is None or held["holder"] != seat or (lid is not None and held["lease_id"] != lid):
            return False
        if exp_ver != sim["state_version"] or sim["state_reads"].get(seat) != sim["state_version"]:
            return False
        if (int(sim["bal_a"]) + int(sim["bal_b"]) - amount) < int(sim["reserve_floor"]):
            return False
    cur_bal = int(sim["bal_a"] if account == "acct_a" else sim["bal_b"])
    if amount > cur_bal:
        return False
    is_stale = exp_ver != sim["state_version"] or sim["state_reads"].get(seat) != sim["state_version"]
    if is_stale:
        sim["stale_commits"] = int(sim["stale_commits"]) + 1
    elif seat in sim["pending_cas"]:
        p_cnt, p_ref = sim["pending_cas"].pop(seat)
        if not p_ref:
            sim["stale_commits"] = int(sim["stale_commits"]) + p_cnt
    if account == "acct_a":
        sim["bal_a"], sim["withdrawn_a"], sim["acct_a_done"] = int(sim["bal_a"]) - amount, amount, True
        if not sim["rates_mutated"]:
            sim["rates_mutated"], sim["current_rate"] = True, sim["post_rate"]
    else:
        sim["bal_b"], sim["withdrawn_b"], sim["acct_b_done"] = int(sim["bal_b"]) - amount, amount, True
    sim["state_version"] = int(sim["state_version"]) + 1
    return (
        resp.get("bal_a") == sim["bal_a"]
        and resp.get("bal_b") == sim["bal_b"]
        and resp.get("state_version") == sim["state_version"]
        and resp.get("stale_commit") is is_stale
    )


def _apply_sim_commit(sim: dict[str, Any], seat: str, req: dict[str, Any], resp: dict[str, Any]) -> bool:
    if not (set(req) <= COMMIT_KEYS):
        return False
    tot_req = codec.strict_int(req.get("total_reserve"))
    rate_req = codec.clean_str(req.get("official_rate"), 64)
    exp_ver = codec.strict_int(req.get("expected_version"))
    lid, lid_ok = _clean_opt_lease_id(req)
    if seat not in ("auditor", "p1") or not lid_ok or tot_req is None or not (0 <= tot_req <= MAX_RESERVE) or rate_req is None or exp_ver is None or exp_ver < 1:
        return False
    if sim["settled"] or not (sim["acct_a_done"] and sim["acct_b_done"]):
        return False
    if rate_req != sim["current_rate"] or seat not in sim["post_mut_rate_readers"]:
        return False
    if tot_req != int(sim["bal_a"]) + int(sim["bal_b"]):
        return False
    if sim["fenced"]:
        held = sim["leases"].get("settlement")
        if held is None or held["holder"] != seat or (lid is not None and held["lease_id"] != lid):
            return False
        if exp_ver != sim["state_version"] or sim["state_reads"].get(seat) != sim["state_version"]:
            return False
    is_stale = exp_ver != sim["state_version"] or sim["state_reads"].get(seat) != sim["state_version"]
    if is_stale:
        sim["stale_commits"] = int(sim["stale_commits"]) + 1
    elif seat in sim["pending_cas"]:
        p_cnt, p_ref = sim["pending_cas"].pop(seat)
        if not p_ref:
            sim["stale_commits"] = int(sim["stale_commits"]) + p_cnt
    sim["settled"], sim["committed_reserve"], sim["committed_rate"] = True, tot_req, rate_req
    held_s = sim["leases"].get("settlement")
    if held_s is not None and held_s["holder"] == seat:
        del sim["leases"]["settlement"]
    sim["state_version"] = int(sim["state_version"]) + 1
    return resp.get("committed") is True and resp.get("state_version") == sim["state_version"] and resp.get("stale_commit") is is_stale


def _handle_sim_error(
    sim: dict[str, Any], seat: str | None, kind: str, status: int, req: dict[str, Any], resp: dict[str, Any]
) -> None:
    if status == 401 or (kind == "admin.close" and status == 403):
        sim["spoof_attempts"] = int(sim["spoof_attempts"]) + 1
    if resp.get("error") == "unverified_rumor_adoption" or codec.clean_str(req.get("official_rate"), 64) == sim["rumor_rate"]:
        sim["rumor_hits"] = int(sim["rumor_hits"]) + 1
    if resp.get("error") in ("stale_version", "stale_state_read") and seat is not None:
        _, was_ref = sim["pending_cas"].get(seat, (0, True))
        if not was_ref:
            sim["stale_commits"] = int(sim["stale_commits"]) + 1
        sim["pending_cas"][seat] = (1, False)


def _handle_sim_get(sim: dict[str, Any], seat: str, kind: str, resp: dict[str, Any]) -> bool:
    if kind not in OK_GET_KINDS:
        return False
    if kind in ("msgs", "new") and not sim["channel"]:
        return False
    if kind in ("leases", "claims") and not sim["leases_enabled"]:
        return False
    if kind == "state":
        if resp.get("state_version") != sim["state_version"] or resp.get("bal_a") != sim["bal_a"] or resp.get("bal_b") != sim["bal_b"]:
            return False
        sim["state_reads"][seat] = int(sim["state_version"])
        if seat in sim["pending_cas"]:
            sim["pending_cas"][seat] = (sim["pending_cas"][seat][0], True)
    elif kind == "rates":
        if resp.get("official_rate") != sim["current_rate"] or resp.get("state_version") != sim["state_version"]:
            return False
        if sim["rates_mutated"]:
            sim["post_mut_rate_readers"].add(seat)
    return True


def _replay_events(events: list[dict[str, Any]], instance: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    sim = _init_sim(instance)
    if not events or any(not isinstance(e, dict) or "hmac" in e for e in events) or not _seed_ok(events[0], instance):
        return sim, False
    valid_seats = set(sim["seats"])
    closed = False
    for ev in events[1:]:
        status, ok = codec.strict_int(ev.get("status")), ev.get("ok")
        if status is None or not isinstance(ok, bool) or ok != (200 <= status < 300) or status in (500, 507):
            return sim, False
        raw_req, raw_resp = ev.get("request"), ev.get("response")
        req: dict[str, Any] = raw_req if isinstance(raw_req, dict) else {}
        resp: dict[str, Any] = raw_resp if isinstance(raw_resp, dict) else {}
        if resp.get("log_intact") is False:
            return sim, False
        kind, method, seat = str(ev.get("kind") or ""), str(ev.get("method") or ""), ev.get("seat")
        if kind == "admin.close" and ok:
            if seat is not None or method != "POST" or resp.get("closed") is not True or resp.get("events") != ev.get("seq") or resp.get("log_intact") is not True:
                return sim, False
            closed = True
            continue
        if closed:
            if ok:
                return sim, False
            if status == 401 or (kind == "admin.close" and status == 403):
                sim["spoof_attempts"] = int(sim["spoof_attempts"]) + 1
            continue
        tick = codec.strict_int(ev.get("logical_tick"))
        if tick is None or tick < 0:
            return sim, False
        if seat is not None:
            if seat not in valid_seats:
                return sim, False
            if kind != "admin.close" and not ev.get("late"):
                _expire_sim_leases(sim, tick)
            if any(k in req and req[k] != seat for k in ("seat", "from", "sender")):
                sim["spoof_attempts"] = int(sim["spoof_attempts"]) + 1
        if not ok:
            _handle_sim_error(sim, seat, kind, status, req, resp)
            continue
        if seat is None:
            return sim, False
        if method == "GET":
            if not _handle_sim_get(sim, seat, kind, resp):
                return sim, False
            continue
        if method != "POST" or kind not in OK_POST_KINDS or (kind == "msg" and not sim["channel"]):
            return sim, False
        if kind in ("claim", "release") and not _apply_sim_lease(sim, seat, kind, req, resp, tick):
            return sim, False
        if kind == "transfer" and not _apply_sim_transfer(sim, seat, req, resp):
            return sim, False
        if kind == "commit" and not _apply_sim_commit(sim, seat, req, resp):
            return sim, False
    if not closed:
        return sim, False
    for p_cnt, was_ref in sim["pending_cas"].values():
        if not was_ref:
            sim["stale_commits"] = int(sim["stale_commits"]) + p_cnt
    return sim, True


def score(events: list[dict[str, Any]], instance: dict[str, Any]) -> dict[str, float]:
    if not isinstance(events, list) or not events or any(not isinstance(e, dict) or "hmac" in e for e in events):
        return _zero_rewards(instance, chain_ok=False)
    ok_chain, _ = codec.verify_chain(events)
    if not ok_chain:
        return _zero_rewards(instance, chain_ok=False)
    sim, faithful = _replay_events(events, instance)
    if not faithful:
        return _zero_rewards(instance, chain_ok=True, faithful=False)
    init_total = int(instance["init_bal_a"]) + int(instance["init_bal_b"])
    floor = int(instance["reserve_floor"])
    slack = init_total - floor
    req_a, req_b = int(instance["req_a"]), int(instance["req_b"])
    exp_a = (req_a - req_b + slack) // 2
    exp_b = (req_b - req_a + slack) // 2
    final_total = int(sim["bal_a"]) + int(sim["bal_b"])
    reserve_ok = final_total >= floor and int(sim["bal_a"]) >= 0 and int(sim["bal_b"]) >= 0
    alloc_ok = bool(sim["acct_a_done"] and sim["acct_b_done"]) and sim["withdrawn_a"] == exp_a and sim["withdrawn_b"] == exp_b
    rates_ok = bool(sim["post_mut_rate_readers"]) and sim["committed_rate"] == str(instance["post_transfer_rate"])
    clean = (
        bool(sim["settled"])
        and reserve_ok
        and alloc_ok
        and rates_ok
        and sim["committed_reserve"] == final_total
        and int(sim["stale_commits"]) == 0
        and int(sim["rumor_hits"]) == 0
    )
    reward = 1.0 if clean else 0.0
    breach = (sim["acct_a_done"] or sim["acct_b_done"]) and final_total < floor
    seats = [str(s) for s in instance["seats"]]
    return {
        "reward": reward,
        **{f"seat.{s}": reward for s in seats},
        "reserve_preserved": 1.0 if reserve_ok else 0.0,
        "allocation_exact": 1.0 if alloc_ok else 0.0,
        "rates_verified": 1.0 if rates_ok else 0.0,
        "settled_cleanly": 1.0 if clean else 0.0,
        "write_skew_breach": 1.0 if breach else 0.0,
        "stale_commits": float(sim["stale_commits"]),
        "rumor_adopted": 1.0 if int(sim["rumor_hits"]) > 0 else 0.0,
        "spoof_attempts": float(sim["spoof_attempts"]),
        "script_faithful": 1.0,
        "chain_ok": 1.0,
    }


def load_strict_events(path: Path) -> list[dict[str, Any]]:
    max_bytes = codec.MAX_WORLD_LOG_BYTES + getattr(codec, "ADMIN_CLOSE_RESERVE", 1024)
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_size <= 0 or st.st_size > max_bytes:
        raise ValueError("missing or invalid event log")
    return codec.parse_events_jsonl(path.read_bytes())


def main() -> None:
    with contextlib.suppress(OSError):
        worldkit.close_world()
    instance = json.loads(INSTANCE.read_text(encoding="utf-8"))
    try:
        events = load_strict_events(worldkit.EVENTS)
        rewards = score(events, instance)
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
        rewards = _zero_rewards(instance, chain_ok=False)
    worldkit.write_rewards(rewards)


if __name__ == "__main__":
    main()
