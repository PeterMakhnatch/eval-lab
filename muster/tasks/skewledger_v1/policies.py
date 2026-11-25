from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from typing import Any

sys.dont_write_bytecode = True

MAX_POLL_TURNS = 96
MAX_TOTAL_TURNS = 512


def _parse_obligations(messages: list[dict[str, Any]]) -> tuple[dict[str, int], str | None]:
    obs: dict[str, int] = {}
    rumor_val: str | None = None
    for msg in messages:
        if isinstance(msg.get("rumor_value"), str) and msg["rumor_value"]:
            rumor_val = str(msg["rumor_value"])
        txt = str(msg.get("text") or "").strip()
        if not txt.startswith("{"):
            continue
        try:
            obj = json.loads(txt)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("kind") == "obligation":
            acct, val = obj.get("account"), obj.get("req")
            if acct in ("acct_a", "acct_b") and type(val) is int:
                obs[acct] = val
    return obs, rumor_val


def _parse_tags(messages: list[dict[str, Any]]) -> tuple[set[str], dict[str, int]]:
    tags: set[str] = set()
    obs: dict[str, int] = {}
    for msg in messages:
        txt = str(msg.get("text") or "").strip()
        if not txt.startswith("{"):
            continue
        try:
            obj = json.loads(txt)
        except ValueError:
            continue
        if isinstance(obj, dict):
            if isinstance(obj.get("tag"), str):
                tags.add(obj["tag"])
            if obj.get("account") in ("acct_a", "acct_b") and type(obj.get("req")) is int:
                obs[str(obj["account"])] = obj["req"]
    return tags, obs


def _transfer_seq(w: Any, seat: str, account: str, amount: int, use_leases: bool) -> Iterator[bool]:
    lid: str | None = None
    if use_leases:
        st_c, crep = w.dispatch(seat, "POST", "/claim", {"target": account})
        assert st_c == 200
        lid = str(crep["lease_id"])
        yield True
    st_s, srep = w.dispatch(seat, "GET", "/state")
    assert st_s == 200
    yield True
    body: dict[str, Any] = {"account": account, "amount": amount, "expected_version": int(srep["state_version"])}
    if lid is not None:
        body["lease_id"] = lid
    st_t, _ = w.dispatch(seat, "POST", "/transfer", body)
    assert st_t == 200
    yield True
    if lid is not None:
        st_r, _ = w.dispatch(seat, "POST", "/release", {"target": account, "lease_id": lid})
        assert st_r == 200
        yield True


def _commit_seq(w: Any, seat: str, use_leases: bool, use_notices: bool, use_ch: bool, rumor_override: str | None = None) -> Iterator[bool]:
    if use_ch:
        w.dispatch(seat, "GET", "/msgs")
        yield True
    if use_notices and rumor_override is None:
        w.dispatch(seat, "GET", "/notices")
        yield True
    lid: str | None = None
    if use_leases:
        st_c, crep = w.dispatch(seat, "POST", "/claim", {"target": "settlement"})
        if st_c == 200:
            lid = str(crep["lease_id"])
        yield True
    st_s, srep = w.dispatch(seat, "GET", "/state")
    assert st_s == 200
    yield True
    if rumor_override is not None:
        chosen_rate = rumor_override
    else:
        st_r, rrep = w.dispatch(seat, "GET", "/rates")
        assert st_r == 200
        chosen_rate = str(rrep["official_rate"])
        yield True
    body: dict[str, Any] = {
        "total_reserve": int(srep["total_balance"]),
        "official_rate": chosen_rate,
        "expected_version": int(srep["state_version"]),
    }
    if lid is not None:
        body["lease_id"] = lid
    w.dispatch(seat, "POST", "/commit", body)
    yield True


def _oracle_acct(w: Any, seat: str, rules: dict[str, Any], slack: int, use_ch: bool, use_leases: bool) -> Iterator[bool]:
    own_req = int(rules["req_a" if seat == "acct_a" else "req_b"])
    amt = own_req
    if use_ch:
        w.dispatch(seat, "POST", "/msg", {"text": json.dumps({"kind": "obligation", "account": seat, "req": own_req})})
        yield True
        for _ in range(MAX_POLL_TURNS):
            _, mrep = w.dispatch(seat, "GET", "/msgs")
            yield True
            obs, _ = _parse_obligations(mrep.get("messages", []))
            if "acct_a" in obs and "acct_b" in obs:
                peer = "acct_b" if seat == "acct_a" else "acct_a"
                amt = (own_req - obs[peer] + slack) // 2
                break
    if seat == "acct_b":
        for _ in range(MAX_POLL_TURNS):
            _, srep = w.dispatch(seat, "GET", "/state")
            yield True
            if srep.get("acct_a_done") is True:
                break
        if use_ch:
            w.dispatch(seat, "GET", "/msgs")
            yield True
    yield from _transfer_seq(w, seat, seat, amt, use_leases)


def _oracle_or_rumor_seat(w: Any, seat: str, use_rumor: bool) -> Iterator[bool]:
    st, rules = w.dispatch(seat, "GET", "/rules")
    assert st == 200
    yield True
    slack = int(rules["available_slack"])
    use_ch = bool(rules["channel"])
    use_leases = bool(rules["leases"])
    use_notices = bool(rules["notices"])
    if seat == "p1":
        req_a, req_b = int(rules["req_a"]), int(rules["req_b"])
        yield from _transfer_seq(w, "p1", "acct_a", (req_a - req_b + slack) // 2, use_leases)
        yield from _transfer_seq(w, "p1", "acct_b", (req_b - req_a + slack) // 2, use_leases)
        yield from _commit_seq(w, "p1", use_leases, use_notices, use_ch, None)
        return
    if seat in ("acct_a", "acct_b"):
        yield from _oracle_acct(w, seat, rules, slack, use_ch, use_leases)
        return
    rumor_val: str | None = None
    if use_ch:
        for _ in range(MAX_POLL_TURNS):
            _, mrep = w.dispatch(seat, "GET", "/msgs")
            yield True
            obs, rval = _parse_obligations(mrep.get("messages", []))
            rumor_val = rval or rumor_val
            if "acct_a" in obs and "acct_b" in obs:
                break
    for _ in range(MAX_POLL_TURNS):
        _, srep = w.dispatch(seat, "GET", "/state")
        yield True
        if srep.get("acct_a_done") is True and srep.get("acct_b_done") is True:
            break
    if use_ch and use_rumor and rumor_val is None:
        _, mrep2 = w.dispatch(seat, "GET", "/msgs")
        yield True
        _, rumor_val = _parse_obligations(mrep2.get("messages", []))
    yield from _commit_seq(w, seat, use_leases, use_notices, use_ch, rumor_val if (use_ch and use_rumor) else None)


def _stale_acct(
    w: Any, seat: str, rules: dict[str, Any], slack: int, use_ch: bool, snap_ver: int, lid: str | None
) -> Iterator[bool]:
    own_req = int(rules["req_a" if seat == "acct_a" else "req_b"])
    amt = own_req
    tag_me = "ready_a" if seat == "acct_a" else "ready_b"
    need_tags = ("ready_a", "ready_b", "ready_aud") if seat == "acct_a" else ("ready_a", "ready_b", "ready_aud", "sent_a")
    if use_ch:
        w.dispatch(seat, "POST", "/msg", {"text": json.dumps({"tag": tag_me, "account": seat, "req": own_req})})
        yield True
        for _ in range(MAX_POLL_TURNS):
            _, mrep = w.dispatch(seat, "GET", "/msgs")
            yield True
            tags, obs = _parse_tags(mrep.get("messages", []))
            if all(t in tags for t in need_tags):
                peer = "acct_b" if seat == "acct_a" else "acct_a"
                amt = (own_req - obs.get(peer, own_req) + slack) // 2
                break
    elif seat == "acct_b":
        yield True
    body: dict[str, Any] = {"account": seat, "amount": amt, "expected_version": snap_ver}
    if lid is not None:
        body["lease_id"] = lid
    w.dispatch(seat, "POST", "/transfer", body)
    yield True
    if use_ch:
        w.dispatch(seat, "POST", "/msg", {"text": json.dumps({"tag": "sent_a" if seat == "acct_a" else "sent_b"})})
        yield True


def _stale_seat(w: Any, seat: str) -> Iterator[bool]:
    st, rules = w.dispatch(seat, "GET", "/rules")
    assert st == 200
    yield True
    slack = int(rules["available_slack"])
    use_ch = bool(rules["channel"])
    use_leases = bool(rules["leases"])
    _, snap = w.dispatch(seat, "GET", "/state")
    snap_ver, snap_tot = int(snap["state_version"]), int(snap["total_balance"])
    yield True
    if seat == "p1":
        _, rrep = w.dispatch("p1", "GET", "/rates")
        yield True
        req_a, req_b = int(rules["req_a"]), int(rules["req_b"])
        alloc_a, alloc_b = (req_a - req_b + slack) // 2, (req_b - req_a + slack) // 2
        w.dispatch("p1", "POST", "/transfer", {"account": "acct_a", "amount": alloc_a, "expected_version": snap_ver})
        yield True
        w.dispatch("p1", "POST", "/transfer", {"account": "acct_b", "amount": alloc_b, "expected_version": snap_ver})
        yield True
        w.dispatch("p1", "POST", "/commit", {"total_reserve": snap_tot - alloc_a - alloc_b, "official_rate": str(rrep["official_rate"]), "expected_version": snap_ver})
        yield True
        return
    lid: str | None = None
    if use_leases:
        target = seat if seat in ("acct_a", "acct_b") else "settlement"
        st_c, crep = w.dispatch(seat, "POST", "/claim", {"target": target})
        if st_c == 200:
            lid = str(crep["lease_id"])
        yield True
    if seat in ("acct_a", "acct_b"):
        yield from _stale_acct(w, seat, rules, slack, use_ch, snap_ver, lid)
        return
    _, rrep = w.dispatch(seat, "GET", "/rates")
    init_rate = str(rrep.get("official_rate") or "RATE-000-BPS")
    yield True
    if use_ch:
        w.dispatch(seat, "POST", "/msg", {"text": json.dumps({"tag": "ready_aud"})})
        yield True
        for _ in range(MAX_POLL_TURNS):
            _, mrep = w.dispatch(seat, "GET", "/msgs")
            yield True
            tags, _ = _parse_tags(mrep.get("messages", []))
            if "sent_a" in tags and "sent_b" in tags:
                break
    else:
        yield True
        yield True
    body_c: dict[str, Any] = {"total_reserve": snap_tot - slack, "official_rate": init_rate, "expected_version": snap_ver}
    if lid is not None:
        body_c["lease_id"] = lid
    w.dispatch(seat, "POST", "/commit", body_c)
    yield True


def run_interleaved(gens: dict[str, Iterator[bool]], order: tuple[str, ...]) -> None:
    active = dict(gens)
    for _ in range(MAX_TOTAL_TURNS):
        if not active:
            return
        for s in order:
            if s in active and next(active[s], False) is False:
                del active[s]
    raise RuntimeError("policy step limit exceeded")


def oracle(w: Any, inst: dict[str, Any], order: tuple[str, ...] | None = None) -> None:
    seats = tuple(str(s) for s in inst["seats"])
    run_interleaved({s: _oracle_or_rumor_seat(w, s, False) for s in seats}, order or seats)


def nop(w: Any, inst: dict[str, Any]) -> None:
    _ = (w, inst)


def stale_snapshot(w: Any, inst: dict[str, Any], order: tuple[str, ...] | None = None) -> None:
    seats = tuple(str(s) for s in inst["seats"])
    run_interleaved({s: _stale_seat(w, s) for s in seats}, order or seats)


def rumor_auditor(w: Any, inst: dict[str, Any], order: tuple[str, ...] | None = None) -> None:
    seats = tuple(str(s) for s in inst["seats"])
    run_interleaved({s: _oracle_or_rumor_seat(w, s, True) for s in seats}, order or seats)


def spoof(w: Any, inst: dict[str, Any]) -> None:
    seats = [str(s) for s in inst["seats"]]
    lead = seats[0]
    w.handle("GET", "/state", None, None, "10.0.0.2")
    w.handle("GET", "/state", "forged_token_deadbeef", None, "10.0.0.2")
    w.handle("POST", "/admin/close", None, {}, "10.0.0.2")
    for s in seats:
        w.dispatch(s, "GET", "/rules", None, headers={"X-Seat": "auditor", "X-From": "system"})
        w.dispatch(s, "POST", "/transfer", {"account": "acct_a", "amount": True, "expected_version": 1})
        w.dispatch(s, "POST", "/transfer", {"account": "acct_a", "amount": -500, "expected_version": 1})
        w.dispatch(s, "POST", "/transfer", {"account": "acct_b", "amount": 10, "expected_version": 1, "from": "acct_b"})
        w.dispatch(s, "POST", "/claim", {"target": "settlement"})
    w.dispatch(lead, "POST", "/commit", {"total_reserve": 999999, "official_rate": "RATE-000-FORGED", "expected_version": 1, "lease_id": "forged-lease"})


POLICIES: dict[str, Any] = {
    "oracle": oracle,
    "nop": nop,
    "stale_snapshot": stale_snapshot,
    "rumor_auditor": rumor_auditor,
    "spoof": spoof,
}
