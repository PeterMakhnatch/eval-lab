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
    if (existing := sys.modules.get(name)) is not None:
        return existing
    here = Path(__file__).resolve().parent
    cands = (
        Path("/world") / f"{name}.py",
        here / f"{name}.py",
        here.parent / "environment" / "world" / f"{name}.py",
        here.parents[1] / "_kit_v2" / f"{name}.py",
    )
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
        "script_faithful": 1.0 if faithful else 0.0,
        "chain_ok": 1.0 if chain_ok else 0.0,
    }


def _init_sim(instance: dict[str, Any]) -> dict[str, Any]:
    return {
        "bal_a": int(instance["init_bal_a"]),
        "bal_b": int(instance["init_bal_b"]),
        "reserve_floor": int(instance["reserve_floor"]),
        "fenced": bool(instance.get("fenced", False)),
        "state_version": 1,
        "logical_tick": 0,
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
        "state_readers": set(),
        "pre_state_ok": True,
        "post_mut_rate_readers": set(),
        "leases": {},
        "lease_seq": 0,
    }


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
    leases = sim["leases"]
    if kind == "claim":
        target = codec.clean_str(req.get("target", req.get("slot")), 128)
        if target not in LEASE_TARGETS:
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
        sim["lease_seq"] = int(sim["lease_seq"]) + 1
        lid = f"lease-{int(sim['lease_seq']):04d}"
        leases[target] = {"holder": seat, "lease_id": lid, "expires_tick": tick + ttl}
        return resp.get("lease_id") == lid and resp.get("holder") == seat
    target = codec.clean_str(req.get("target", req.get("slot")), 128)
    existing = leases.get(target) if target is not None else None
    if existing is None or existing["holder"] != seat:
        return False
    if "lease_id" in req and req.get("lease_id") != existing["lease_id"]:
        return False
    del leases[target]
    return resp.get("released") == target


def _apply_sim_transfer(sim: dict[str, Any], seat: str, req: dict[str, Any], resp: dict[str, Any]) -> bool:
    account = req.get("account")
    amount = codec.strict_int(req.get("amount"))
    exp_ver = codec.strict_int(req.get("expected_version"))
    if account not in ACCOUNTS or amount is None or amount < 1 or exp_ver is None or exp_ver < 1:
        return False
    if seat == "auditor" or (seat in ACCOUNTS and seat != account) or bool(sim["settled"]):
        return False
    if (account == "acct_a" and sim["acct_a_done"]) or (account == "acct_b" and sim["acct_b_done"]):
        return False
    if sim["fenced"]:
        held = sim["leases"].get(account)
        lid = req.get("lease_id")
        if held is None or held["holder"] != seat or (lid is not None and held["lease_id"] != lid):
            return False
        if exp_ver != sim["state_version"] or (sim["bal_a"] + sim["bal_b"] - amount) < sim["reserve_floor"]:
            return False
    cur_bal = int(sim["bal_a"] if account == "acct_a" else sim["bal_b"])
    if amount > cur_bal:
        return False
    if seat not in sim["state_readers"]:
        sim["pre_state_ok"] = False
    if exp_ver != sim["state_version"]:
        sim["stale_commits"] = int(sim["stale_commits"]) + 1
    if account == "acct_a":
        sim["bal_a"] = int(sim["bal_a"]) - amount
        sim["withdrawn_a"] = amount
        sim["acct_a_done"] = True
        if not sim["rates_mutated"]:
            sim["rates_mutated"] = True
            sim["current_rate"] = sim["post_rate"]
    else:
        sim["bal_b"] = int(sim["bal_b"]) - amount
        sim["withdrawn_b"] = amount
        sim["acct_b_done"] = True
    sim["state_version"] = int(sim["state_version"]) + 1
    return (
        resp.get("bal_a") == sim["bal_a"]
        and resp.get("bal_b") == sim["bal_b"]
        and resp.get("state_version") == sim["state_version"]
    )


def _apply_sim_commit(sim: dict[str, Any], seat: str, req: dict[str, Any], resp: dict[str, Any]) -> bool:
    tot_req = codec.strict_int(req.get("total_reserve"))
    rate_req = req.get("official_rate")
    exp_ver = codec.strict_int(req.get("expected_version"))
    if seat not in ("auditor", "p1") or tot_req is None or not isinstance(rate_req, str) or exp_ver is None:
        return False
    if sim["settled"] or not (sim["acct_a_done"] and sim["acct_b_done"]):
        return False
    if rate_req != sim["current_rate"] or seat not in sim["post_mut_rate_readers"]:
        return False
    if tot_req != int(sim["bal_a"]) + int(sim["bal_b"]):
        return False
    if sim["fenced"]:
        held = sim["leases"].get("settlement")
        lid = req.get("lease_id")
        if held is None or held["holder"] != seat or (lid is not None and held["lease_id"] != lid):
            return False
        if exp_ver != sim["state_version"]:
            return False
    if seat not in sim["state_readers"]:
        sim["pre_state_ok"] = False
    if exp_ver != sim["state_version"]:
        sim["stale_commits"] = int(sim["stale_commits"]) + 1
    sim["settled"] = True
    sim["committed_reserve"] = tot_req
    sim["committed_rate"] = rate_req
    held_s = sim["leases"].get("settlement")
    if held_s is not None and held_s["holder"] == seat:
        del sim["leases"]["settlement"]
    sim["state_version"] = int(sim["state_version"]) + 1
    return resp.get("committed") is True and resp.get("state_version") == sim["state_version"]


def _replay_events(events: list[dict[str, Any]], instance: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    sim = _init_sim(instance)
    if not events or not _seed_ok(events[0], instance):
        return sim, False
    valid_seats = set(str(s) for s in instance["seats"])
    closed = False
    for ev in events[1:]:
        status, ok = codec.strict_int(ev.get("status")), ev.get("ok")
        if status is None or not isinstance(ok, bool) or ok != (200 <= status < 300) or status == 500:
            return sim, False
        kind, method, seat = str(ev.get("kind") or ""), str(ev.get("method") or ""), ev.get("seat")
        if kind == "admin.close" and ok:
            if seat is not None or method != "POST":
                return sim, False
            closed = True
            continue
        if closed:
            if ok:
                return sim, False
            continue
        tick = codec.strict_int(ev.get("logical_tick")) or 0
        if seat is not None:
            if seat not in valid_seats:
                return sim, False
            _expire_sim_leases(sim, tick)
        raw_req, raw_resp = ev.get("request"), ev.get("response")
        req: dict[str, Any] = raw_req if isinstance(raw_req, dict) else {}
        resp: dict[str, Any] = raw_resp if isinstance(raw_resp, dict) else {}
        if not ok:
            if resp.get("error") == "unverified_rumor_adoption" or req.get("official_rate") == sim["rumor_rate"]:
                sim["rumor_hits"] = int(sim["rumor_hits"]) + 1
            if resp.get("error") == "stale_version":
                sim["stale_commits"] = int(sim["stale_commits"]) + 1
            continue
        if seat is None:
            return sim, False
        if method == "GET":
            if kind == "state":
                if resp.get("state_version") != sim["state_version"] or resp.get("bal_a") != sim["bal_a"] or resp.get("bal_b") != sim["bal_b"]:
                    return sim, False
                sim["state_readers"].add(seat)
            elif kind == "rates":
                if resp.get("official_rate") != sim["current_rate"] or resp.get("state_version") != sim["state_version"]:
                    return sim, False
                if sim["rates_mutated"]:
                    sim["post_mut_rate_readers"].add(seat)
            continue
        if kind in ("claim", "release") and not _apply_sim_lease(sim, seat, kind, req, resp, tick):
            return sim, False
        if kind == "transfer" and not _apply_sim_transfer(sim, seat, req, resp):
            return sim, False
        if kind == "commit" and not _apply_sim_commit(sim, seat, req, resp):
            return sim, False
    return sim, True


def score(events: list[dict[str, Any]], instance: dict[str, Any]) -> dict[str, float]:
    if not isinstance(events, list) or not events:
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
        and bool(sim["pre_state_ok"])
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
        "script_faithful": 1.0,
        "chain_ok": 1.0,
    }


def load_strict_events(path: Path) -> list[dict[str, Any]]:
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_size <= 0 or st.st_size > codec.MAX_WORLD_LOG_BYTES:
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
