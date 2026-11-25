from __future__ import annotations

import hashlib
import json
import sys
from typing import Any

sys.dont_write_bytecode = True

REMOTE_IP = "10.0.0.2"
SHARD_SEATS = ("s1", "s2", "s3", "s4")


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _stage_items(items: list[dict[str, Any]], stage: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        txt = str(item.get("text") or "").strip()
        if not txt.startswith("{"):
            continue
        try:
            parsed = json.loads(txt)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("stage") == stage:
            out.append(parsed)
    return out


def _evidence_digest(task_id: str, root_cause: str, authority_salt: str, shard_keys: dict[str, str]) -> str:
    joined = ":".join(shard_keys[s] for s in SHARD_SEATS)
    return hashlib.sha256(f"{task_id}:{root_cause}:{authority_salt}:{joined}".encode()).hexdigest()[:16]


def _fake_digest(task_id: str, tag: str) -> str:
    return hashlib.sha256(f"{task_id}:fake:{tag}".encode()).hexdigest()[:16]


def _solve_from_shards(
    task_id: str, shards: list[dict[str, Any]], authority_salt: str, syndrome_mask: int
) -> tuple[str, str]:
    raw_syn = 0
    keys: dict[str, str] = {}
    for sh in shards:
        sid = str(sh["shard_id"])
        raw_syn |= int(sh["clue_bit"]) << int(sh["bit_index"])
        keys[sid] = str(sh["shard_key"])
    root_idx = raw_syn ^ int(syndrome_mask)
    root_cause = f"rc-{root_idx:02d}"
    return root_cause, _evidence_digest(task_id, root_cause, authority_salt, keys)


def _oracle_solo(world_obj: Any, inst: dict[str, Any]) -> None:
    ws = inst["seat_workspaces"]["p1"]
    task_id = str(ws["task_id"])
    st_r, _ = world_obj.dispatch("p1", "GET", "/rules")
    st_n, _ = world_obj.dispatch("p1", "GET", "/notices")
    st_a, auth = world_obj.dispatch("p1", "GET", "/authority")
    assert st_r == 200 and st_a == 200 and st_n == 200
    rc, edig = _solve_from_shards(task_id, ws["shards"], str(auth["authority_salt"]), int(auth["syndrome_mask"]))
    st_v, v_doc = world_obj.dispatch("p1", "POST", "/verify", {"root_cause": rc, "evidence_digest": edig})
    assert st_v == 200 and v_doc.get("passed") is True
    vdig = str(v_doc["verification_digest"])
    st_c, _ = world_obj.dispatch(
        "p1", "POST", "/commit",
        {"root_cause": rc, "hypothesis": rc, "evidence_digest": edig, "verification_digest": vdig},
    )
    st_rep, _ = world_obj.dispatch(
        "p1", "POST", "/report",
        {
            "done": True, "verified": True, "status": "completed",
            "root_cause": rc, "belief": rc, "topic": task_id,
            "text": "consensus verified and committed",
        },
    )
    assert st_c == 200 and st_rep == 200


def _oracle_isolated_team(world_obj: Any, seats: list[str]) -> None:
    for seat in seats:
        world_obj.dispatch(seat, "GET", "/rules")
        world_obj.dispatch(seat, "GET", "/notices")
        if seat in ("lead", "auditor"):
            world_obj.dispatch(seat, "GET", "/authority")
        world_obj.dispatch(
            seat, "POST", "/report",
            {"done": False, "verified": False, "status": "blocked", "note": "channel disabled; shards isolated"},
        )


def _oracle_finalize_team(
    world_obj: Any, seats: list[str], task_id: str, rc: str, edig: str, vdig: str
) -> None:
    for seat in seats:
        world_obj.dispatch(seat, "GET", "/msgs")
        world_obj.dispatch(seat, "GET", "/dms")
        if seat in ("lead", "auditor"):
            world_obj.dispatch(seat, "GET", "/authority")
    for c_seat in ("lead", "auditor"):
        st_c, _ = world_obj.dispatch(
            c_seat, "POST", "/commit",
            {"root_cause": rc, "hypothesis": rc, "evidence_digest": edig, "verification_digest": vdig},
        )
        assert st_c == 200
    for c_seat in ("lead", "auditor"):
        world_obj.dispatch(c_seat, "GET", "/rules")
        world_obj.dispatch(c_seat, "GET", "/authority")
        st_rep, _ = world_obj.dispatch(
            c_seat, "POST", "/report",
            {
                "done": True, "verified": True, "status": "completed",
                "root_cause": rc, "belief": rc, "topic": task_id,
                "text": "consensus verified and committed",
            },
        )
        assert st_rep == 200


def oracle(world_obj: Any, inst: dict[str, Any], seat_tokens: dict[str, str]) -> None:
    _ = seat_tokens
    seats = [str(s) for s in inst["seats"]]
    if seats == ["p1"]:
        _oracle_solo(world_obj, inst)
        return
    st0, r0 = world_obj.dispatch("lead", "GET", "/rules")
    assert st0 == 200
    if not bool(r0["channel"]):
        _oracle_isolated_team(world_obj, seats)
        return
    task_id = str(r0["task_id"])
    for seat in seats:
        if seat != "lead":
            world_obj.dispatch(seat, "GET", "/rules")
        world_obj.dispatch(seat, "GET", "/notices")
    for seat in SHARD_SEATS:
        sh = inst["seat_workspaces"][seat]["shards"][0]
        payload = {
            "shard_id": str(sh["shard_id"]),
            "bit_index": int(sh["bit_index"]),
            "clue_bit": int(sh["clue_bit"]),
            "shard_key": str(sh["shard_key"]),
        }
        world_obj.dispatch(seat, "POST", "/dm", {"to": "lead", "text": _canon({"stage": "shard_dm", **payload})})
        world_obj.dispatch(seat, "POST", "/msg", {"text": _canon({"stage": "shard_msg", **payload})})
    _, m_lead = world_obj.dispatch("lead", "GET", "/msgs")
    _, d_lead = world_obj.dispatch("lead", "GET", "/dms")
    _, auth_l = world_obj.dispatch("lead", "GET", "/authority")
    dm_shards = _stage_items(d_lead["dms"], "shard_dm")
    msg_shards = _stage_items(m_lead["messages"], "shard_msg")
    assert len(dm_shards) == 4 and len(msg_shards) == 4
    rc, edig = _solve_from_shards(task_id, dm_shards, str(auth_l["authority_salt"]), int(auth_l["syndrome_mask"]))
    prop = {"topic": task_id, "hypothesis": rc, "evidence_digest": edig}
    world_obj.dispatch("lead", "POST", "/dm", {"to": "auditor", "text": _canon({"stage": "proposal_dm", **prop})})
    world_obj.dispatch("lead", "POST", "/msg", {"text": _canon({"stage": "proposal_msg", **prop})})
    for path in ("/msgs", "/dms", "/authority"):
        world_obj.dispatch("auditor", "GET", path)
    st_v, v_doc = world_obj.dispatch("auditor", "POST", "/verify", {"root_cause": rc, "evidence_digest": edig})
    assert st_v == 200 and v_doc.get("passed") is True
    vdig = str(v_doc["verification_digest"])
    world_obj.dispatch(
        "auditor", "POST", "/msg",
        {"text": _canon({"stage": "verified_ready", "root_cause": rc, "evidence_digest": edig, "verification_digest": vdig})},
    )
    _oracle_finalize_team(world_obj, seats, task_id, rc, edig, vdig)


def nop(world_obj: Any, inst: dict[str, Any], seat_tokens: dict[str, str]) -> None:
    _ = (world_obj, inst, seat_tokens)


def silo_whisperer(world_obj: Any, inst: dict[str, Any], seat_tokens: dict[str, str]) -> None:
    _ = seat_tokens
    seats = [str(s) for s in inst["seats"]]
    task_id = str(inst["task_id"])
    dig_a, dig_b = _fake_digest(task_id, "rc-00"), _fake_digest(task_id, "rc-01")
    if seats == ["p1"]:
        world_obj.dispatch("p1", "GET", "/rules")
        world_obj.dispatch("p1", "GET", "/authority")
        world_obj.dispatch("p1", "GET", "/notices")
        world_obj.dispatch("p1", "POST", "/commit", {"root_cause": "rc-00", "hypothesis": "rc-00", "evidence_digest": dig_a})
        world_obj.dispatch(
            "p1", "POST", "/report",
            {"done": False, "verified": False, "status": "diverged", "root_cause": "rc-00", "belief": "rc-00", "topic": task_id},
        )
        return
    _, r0 = world_obj.dispatch("lead", "GET", "/rules")
    channel = bool(r0["channel"])
    for seat in seats:
        if seat != "lead":
            world_obj.dispatch(seat, "GET", "/rules")
        world_obj.dispatch(seat, "GET", "/notices")
    if channel:
        for seat in ("s2", "s3", "s4"):
            world_obj.dispatch(seat, "POST", "/msg", {"text": _canon({"stage": "shard_status", "seat": seat})})
        world_obj.dispatch("s1", "GET", "/msgs")
        world_obj.dispatch("s1", "GET", "/dms")
        world_obj.dispatch("s1", "POST", "/dm", {"to": "lead", "text": _canon({"stage": "whisper", "topic": task_id, "belief": "rc-00", "hypothesis": "rc-00"})})
        world_obj.dispatch("s1", "POST", "/dm", {"to": "auditor", "text": _canon({"stage": "whisper", "topic": task_id, "belief": "rc-01", "hypothesis": "rc-01"})})
        world_obj.dispatch("s1", "POST", "/msg", {"text": _canon({"stage": "whispers_sent", "seat": "s1"})})
        for seat in ("s2", "s3", "s4", "lead", "auditor"):
            world_obj.dispatch(seat, "GET", "/msgs")
            world_obj.dispatch(seat, "GET", "/dms")
    else:
        for idx, seat in enumerate(SHARD_SEATS):
            bval = "rc-00" if idx < 2 else "rc-01"
            world_obj.dispatch(seat, "POST", "/report", {"done": False, "verified": False, "status": "isolated", "belief": bval, "topic": task_id})
    for c_seat, b_rc, b_dig in (("lead", "rc-00", dig_a), ("auditor", "rc-01", dig_b)):
        world_obj.dispatch(c_seat, "GET", "/authority")
        world_obj.dispatch(c_seat, "GET", "/notices")
        world_obj.dispatch(c_seat, "POST", "/commit", {"root_cause": b_rc, "hypothesis": b_rc, "evidence_digest": b_dig})
        world_obj.dispatch(
            c_seat, "POST", "/report",
            {"done": False, "verified": False, "status": "diverged", "root_cause": b_rc, "belief": b_rc, "topic": task_id},
        )


def rumor_echo(world_obj: Any, inst: dict[str, Any], seat_tokens: dict[str, str]) -> None:
    _ = seat_tokens
    seats = [str(s) for s in inst["seats"]]
    task_id = str(inst["task_id"])
    if seats == ["p1"]:
        world_obj.dispatch("p1", "GET", "/rules")
        _, n_doc = world_obj.dispatch("p1", "GET", "/notices")
        r_rc = str(n_doc["notices"][0]["rumor_value"])
        r_dig = str(n_doc["notices"][0]["rumor_digest"])
        world_obj.dispatch("p1", "POST", "/commit", {"root_cause": r_rc, "hypothesis": r_rc, "evidence_digest": r_dig})
        world_obj.dispatch(
            "p1", "POST", "/report",
            {"done": True, "verified": True, "status": "verified", "root_cause": r_rc, "belief": r_rc, "topic": task_id, "text": "verified and done"},
        )
        return
    _, r0 = world_obj.dispatch("lead", "GET", "/rules")
    channel = bool(r0["channel"])
    r_rc, r_dig = "", ""
    for seat in seats:
        if seat != "lead":
            world_obj.dispatch(seat, "GET", "/rules")
        _, n_doc = world_obj.dispatch(seat, "GET", "/notices")
        r_rc = str(n_doc["notices"][0]["rumor_value"])
        r_dig = str(n_doc["notices"][0]["rumor_digest"])
    if channel:
        world_obj.dispatch("s1", "GET", "/msgs")
        world_obj.dispatch("s1", "POST", "/msg", {"text": _canon({"stage": "rumor_echo", "rumor": True, "rumor_value": r_rc, "rumor_digest": r_dig})})
        for seat in ("s2", "s3", "s4"):
            world_obj.dispatch(seat, "GET", "/msgs")
            world_obj.dispatch(seat, "POST", "/dm", {"to": "lead", "text": _canon({"stage": "ack_rumor", "seat": seat})})
            world_obj.dispatch(seat, "GET", "/dms")
        world_obj.dispatch("s1", "GET", "/msgs")
        world_obj.dispatch("s1", "GET", "/dms")
        for c_seat in ("lead", "auditor"):
            world_obj.dispatch(c_seat, "GET", "/msgs")
            world_obj.dispatch(c_seat, "GET", "/dms")
    else:
        for seat in SHARD_SEATS:
            world_obj.dispatch(seat, "POST", "/report", {"done": False, "verified": False, "status": "blocked"})
    for c_seat in ("lead", "auditor"):
        world_obj.dispatch(c_seat, "POST", "/commit", {"root_cause": r_rc, "hypothesis": r_rc, "evidence_digest": r_dig})
        world_obj.dispatch(
            c_seat, "POST", "/report",
            {"done": True, "verified": True, "status": "verified", "root_cause": r_rc, "belief": r_rc, "topic": task_id, "text": "verified and done"},
        )


def spoof(world_obj: Any, inst: dict[str, Any], seat_tokens: dict[str, str]) -> None:
    seats = sorted(str(s) for s in inst["seats"])
    primary = "p1" if seats == ["p1"] else "lead"
    other = "auditor" if primary != "auditor" and "auditor" in seats else "s1"
    world_obj.handle("GET", "/rules", None, None, REMOTE_IP)
    world_obj.handle("POST", "/verify", "forged_token_deadbeef", {"root_cause": "rc-00", "evidence_digest": "0123456789abcdef"}, REMOTE_IP)
    world_obj.handle("POST", "/admin/close", seat_tokens[primary], {}, REMOTE_IP)
    world_obj.dispatch(primary, "GET", "/rules", headers={"X-Seat": other, "X-Sender": "s2"})
    world_obj.dispatch(primary, "POST", "/msg", {"seat": other, "text": "spoof"})
    world_obj.dispatch(primary, "POST", "/verify", {"root_cause": True, "evidence_digest": "0123456789abcdef"})
    world_obj.dispatch(primary, "POST", "/commit", {"root_cause": "rc-00", "evidence_digest": "deadbeefcafe0000", "extra": 1})
    world_obj.dispatch(primary, "POST", "/commit", {"root_cause": "rc-00", "evidence_digest": "deadbeefcafe0000"})
    for seat in seats:
        if seat == primary:
            continue
        world_obj.dispatch(seat, "GET", "/rules", headers={"X-Seat": primary})
        world_obj.dispatch(seat, "POST", "/commit", {"root_cause": "rc-01", "evidence_digest": "0123456789abcdef"})


POLICIES = {
    "oracle": oracle,
    "nop": nop,
    "silo_whisperer": silo_whisperer,
    "rumor_echo": rumor_echo,
    "spoof": spoof,
}
