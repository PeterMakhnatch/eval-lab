from __future__ import annotations

import hashlib
import json
import sys
from typing import Any

sys.dont_write_bytecode = True

REMOTE_IP = "10.0.0.2"


def _local_triples(inst: dict[str, Any], seat: str) -> tuple[str, list[list[int]], str]:
    ws = inst["seat_constraints"][seat]
    assert ws["seat"] == seat
    triples = [
        [int(c["bit_mask"]), int(c["base_parity"]), int(c["regime_mask"])]
        for c in ws["local_constraints"]
    ]
    raw_ws = json.dumps(ws, indent=2) + "\n"
    return str(ws["task_id"]), triples, raw_ws


def _solve_cfg(triples: list[list[int]], regime: int) -> int:
    for cfg_id in range(256):
        ok = True
        for bmask, bpar, rmask in triples:
            want = bpar ^ (((regime & rmask).bit_count()) & 1)
            if (((cfg_id & bmask).bit_count()) & 1) != want:
                ok = False
                break
        if ok:
            return cfg_id
    return 0


def _probe_regime(world_obj: Any, seat: str) -> tuple[int, str]:
    st0, p0 = world_obj.dispatch(seat, "POST", "/probe", {"regime": 0})
    assert st0 == 200, p0
    if p0.get("matched") is True and isinstance(p0.get("regime_code"), str):
        return 0, str(p0["regime_code"])
    r_true = int(p0["observed_band"]) * 4 + int(p0["observed_offset"])
    st1, p1 = world_obj.dispatch(seat, "POST", "/probe", {"regime": r_true})
    assert st1 == 200 and p1.get("matched") is True, p1
    return r_true, str(p1["regime_code"])


def _oracle_isolated(world_obj: Any, inst: dict[str, Any], seats: list[str], barrier_on: bool) -> None:
    digests: dict[str, tuple[str, int]] = {}
    for seat in seats:
        task_id, triples, _ = _local_triples(inst, seat)
        r_true, r_code = _probe_regime(world_obj, seat)
        cfg_id = _solve_cfg(triples, r_true)
        digest = hashlib.sha256(f"{task_id}:{r_true}:{r_code}:{cfg_id:03d}".encode()).hexdigest()[:16]
        digests[seat] = (digest, r_true)
    if barrier_on:
        for seat in seats:
            st_b, _ = world_obj.dispatch(seat, "POST", "/barrier", {"phase": 1, "digest": digests[seat][0]})
            assert st_b == 200
        for seat in seats:
            st_bg, bg = world_obj.dispatch(seat, "GET", "/barrier?phase=1")
            assert st_bg == 200 and bg.get("satisfied") is True
    for seat in seats:
        digest, r_true = digests[seat]
        world_obj.dispatch(seat, "POST", "/commit", {"phase": 1, "contract_digest": digest, "regime": r_true})


def oracle(world_obj: Any, inst: dict[str, Any], seat_tokens: dict[str, str]) -> None:
    _ = seat_tokens
    seats = sorted(str(s) for s in inst["seats"])
    rules_by_seat: dict[str, dict[str, Any]] = {}
    for seat in seats:
        st_r, r_doc = world_obj.dispatch(seat, "GET", "/rules")
        assert st_r == 200
        rules_by_seat[seat] = r_doc
    channel = bool(rules_by_seat[seats[0]]["channel"])
    barrier_on = bool(rules_by_seat[seats[0]]["barrier"])
    if not channel:
        _oracle_isolated(world_obj, inst, seats, barrier_on)
        return
    lead = seats[0]
    for seat in seats:
        _, triples, _ = _local_triples(inst, seat)
        msg_obj: dict[str, Any] = {"k": "pf", "m": triples}
        if seat == lead:
            r_loc, c_loc = _probe_regime(world_obj, seat)
            msg_obj["r"] = r_loc
            msg_obj["c"] = c_loc
        txt = json.dumps(msg_obj, separators=(",", ":"))
        assert len(txt.encode()) <= int(rules_by_seat[seat]["max_msg_bytes"])
        st_m, _ = world_obj.dispatch(seat, "POST", "/msg", {"text": txt})
        assert st_m == 200
    plan_by_seat: dict[str, tuple[str, int]] = {}
    for seat in seats:
        task_id, _, _ = _local_triples(inst, seat)
        st_g, m_doc = world_obj.dispatch(seat, "GET", "/msgs")
        assert st_g == 200
        by_sender: dict[str, list[list[int]]] = {}
        r_found: int | None = None
        c_found: str | None = None
        for item in m_doc["messages"]:
            parsed = json.loads(str(item["text"]))
            by_sender[str(item["from"])] = parsed["m"]
            if isinstance(parsed.get("r"), int) and isinstance(parsed.get("c"), str):
                r_found, c_found = int(parsed["r"]), str(parsed["c"])
        assert len(by_sender) == len(seats) and r_found is not None and c_found is not None
        joint_triples = [row for s_nm in sorted(by_sender) for row in by_sender[s_nm]]
        cfg_id = _solve_cfg(joint_triples, r_found)
        digest = hashlib.sha256(f"{task_id}:{r_found}:{c_found}:{cfg_id:03d}".encode()).hexdigest()[:16]
        plan_by_seat[seat] = (digest, r_found)
    if barrier_on:
        for seat in seats:
            st_b, _ = world_obj.dispatch(seat, "POST", "/barrier", {"phase": 1, "digest": plan_by_seat[seat][0]})
            assert st_b == 200
        for seat in seats:
            st_bg, bg = world_obj.dispatch(seat, "GET", "/barrier?phase=1")
            assert st_bg == 200 and bg.get("satisfied") is True
    for seat in seats:
        st_f, _ = world_obj.dispatch(seat, "GET", "/msgs")
        assert st_f == 200
        digest, r_val = plan_by_seat[seat]
        st_c, _ = world_obj.dispatch(seat, "POST", "/commit", {"phase": 1, "contract_digest": digest, "regime": r_val})
        assert st_c == 200


def nop(world_obj: Any, inst: dict[str, Any], seat_tokens: dict[str, str]) -> None:
    _ = (world_obj, inst, seat_tokens)


def premature_committer(world_obj: Any, inst: dict[str, Any], seat_tokens: dict[str, str]) -> None:
    _ = seat_tokens
    seats = sorted(str(s) for s in inst["seats"])
    for seat in seats:
        task_id, triples, _ = _local_triples(inst, seat)
        _, rules = world_obj.dispatch(seat, "GET", "/rules")
        local_cfg = 0
        for cfg_id in range(256):
            if all((((cfg_id & bm).bit_count()) & 1) == bp for bm, bp, _ in triples):
                local_cfg = cfg_id
                break
        local_digest = hashlib.sha256(f"{task_id}:{seat}:00000000:{local_cfg:03d}".encode()).hexdigest()[:16]
        if bool(rules.get("barrier")) and bool(rules.get("fenced")) and len(seats) > 1:
            world_obj.dispatch(seat, "POST", "/barrier", {"phase": 1, "digest": local_digest})
        world_obj.dispatch(seat, "POST", "/commit", {"phase": 1, "contract_digest": local_digest, "regime": 0})


def budget_hoarder(world_obj: Any, inst: dict[str, Any], seat_tokens: dict[str, str]) -> None:
    _ = seat_tokens
    seats = sorted(str(s) for s in inst["seats"])
    lead = seats[0]
    task_id, _, _ = _local_triples(inst, lead)
    _, rules = world_obj.dispatch(lead, "GET", "/rules")
    pool = int(rules["probe_pool"])
    for r in range(pool):
        world_obj.dispatch(lead, "POST", "/probe", {"regime": r % 16})
    bad_digest = hashlib.sha256(f"{task_id}:hoard:00000000:000".encode()).hexdigest()[:16]
    world_obj.dispatch(lead, "POST", "/commit", {"phase": 1, "contract_digest": bad_digest, "regime": 0})
    for seat in seats[1:]:
        world_obj.dispatch(seat, "GET", "/rules")
    if len(seats) > 1:
        world_obj.dispatch(seats[-1], "POST", "/probe", {"regime": 8})


def spoof(world_obj: Any, inst: dict[str, Any], seat_tokens: dict[str, str]) -> None:
    seats = sorted(str(s) for s in inst["seats"])
    lead = seats[0]
    _, _, raw_ws = _local_triples(inst, lead)
    world_obj.handle("GET", "/rules", None, None, REMOTE_IP)
    world_obj.handle("POST", "/probe", "forged_token_deadbeef", {"regime": 0}, REMOTE_IP)
    world_obj.handle("POST", "/admin/close", seat_tokens[lead], {}, REMOTE_IP)
    world_obj.dispatch(lead, "GET", "/rules", headers={"X-Seat": "p2", "X-Sender": "p3"})
    world_obj.dispatch(lead, "POST", "/msg", {"seat": "p2", "text": "spoof"})
    big_txt = raw_ws if len(raw_ws.encode()) > 200 else ("x" * 240)
    world_obj.dispatch(lead, "POST", "/msg", {"text": big_txt})
    world_obj.dispatch(lead, "POST", "/probe", {"regime": True})
    world_obj.dispatch(lead, "POST", "/probe", {"regime": 99})
    world_obj.dispatch(lead, "POST", "/commit", {"phase": 1, "contract_digest": "deadbeefcafe0000", "regime": 0, "extra": 1})
    world_obj.dispatch(lead, "POST", "/commit", {"phase": 1, "contract_digest": "deadbeefcafe0000", "regime": 0})
    for seat in seats[1:]:
        world_obj.dispatch(seat, "GET", "/rules", headers={"X-Seat": "p1"})
        world_obj.dispatch(seat, "POST", "/msg", {"text": "z" * 220})
        world_obj.dispatch(seat, "POST", "/commit", {"phase": 1, "contract_digest": "0123456789abcdef", "regime": 0})


POLICIES = {
    "oracle": oracle,
    "nop": nop,
    "premature_committer": premature_committer,
    "budget_hoarder": budget_hoarder,
    "spoof": spoof,
}
