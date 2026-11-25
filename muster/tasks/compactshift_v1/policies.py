from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterator, Mapping
from typing import Any

sys.dont_write_bytecode = True

MAX_POLLS = 80
MAX_SPEC_POLLS = 20
MAX_ROUND_STEPS = 400
REMOTE_CLIENT = "10.0.0.2"
PHANTOM_TEXT = "all checks passed and verified done"


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _base_digest(task_id: str, base_constraints: Mapping[str, Any]) -> str:
    return hashlib.sha256(f"{task_id}/1/{_canon(dict(base_constraints))}".encode()).hexdigest()[:32]


def _handoff_digest(task_id: str, plan_seal: str, constraints: Mapping[str, Any]) -> str:
    return hashlib.sha256(f"{task_id}/2/{plan_seal}/{_canon(dict(constraints))}".encode()).hexdigest()[:32]


def _find_msg_stage(messages: list[Any], stage: str) -> dict[str, Any] | None:
    for item in messages:
        if not isinstance(item, Mapping):
            continue
        txt = str(item.get("text") or "").strip()
        if not txt.startswith("{"):
            continue
        try:
            parsed = json.loads(txt)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("stage") == stage:
            return parsed
    return None


def _has_msg_text(messages: list[Any], needle: str) -> bool:
    return any(isinstance(m, Mapping) and needle in str(m.get("text") or "") for m in messages)


def _oracle_solo(world_obj: Any, seat: str) -> Iterator[bool]:
    st_r, _ = world_obj.dispatch(seat, "GET", "/rules")
    assert st_r == 200
    yield True
    st_s, spec = world_obj.dispatch(seat, "GET", "/spec")
    assert st_s == 200
    tid = str(spec["task_id"])
    b_cons = dict(spec["base_spec"]["base_constraints"])
    yield True
    st_p, plan_res = world_obj.dispatch(seat, "POST", "/plan", {"task_id": tid, "base_digest": _base_digest(tid, b_cons)})
    assert st_p == 200
    plan_seal = str(plan_res["plan_seal"])
    yield True
    st_n, n_res = world_obj.dispatch(seat, "GET", "/notices")
    assert st_n == 200
    s_cons = dict(n_res["notices"][-1]["shift_constraints"])
    merged = {**b_cons, **s_cons}
    h_dig = _handoff_digest(tid, plan_seal, merged)
    yield True
    st_h, h_res = world_obj.dispatch(
        seat,
        "POST",
        "/handoff",
        {"epoch": 2, "expected_version": int(n_res["state_version"]), "constraints": merged, "handoff_digest": h_dig},
    )
    assert st_h == 200
    hid = str(h_res["handoff_id"])
    yield True
    st_v, v_res = world_obj.dispatch(seat, "POST", "/verify", {"epoch": 2, "handoff_id": hid, "handoff_digest": h_dig})
    assert st_v == 200 and v_res["passed"] is True
    yield True
    st_c, _ = world_obj.dispatch(
        seat,
        "POST",
        "/commit",
        {
            "epoch": 2,
            "expected_version": int(v_res["state_version"]),
            "handoff_id": hid,
            "verification_digest": str(v_res["verification_digest"]),
        },
    )
    assert st_c == 200
    yield True
    st_rep, _ = world_obj.dispatch(
        seat,
        "POST",
        "/report",
        {"done": True, "verified": True, "status": "completed", "text": "epoch 2 handoff verified and committed"},
    )
    assert st_rep == 200
    yield True


def _oracle_planner(world_obj: Any, seat: str) -> Iterator[bool]:
    _, rules = world_obj.dispatch(seat, "GET", "/rules")
    channel = bool(rules["channel"])
    yield True
    _, spec = world_obj.dispatch(seat, "GET", "/spec")
    tid, b_cons = str(spec["task_id"]), dict(spec["base_spec"]["base_constraints"])
    yield True
    _, p_res = world_obj.dispatch(seat, "POST", "/plan", {"task_id": tid, "base_digest": _base_digest(tid, b_cons)})
    plan_seal = str(p_res["plan_seal"])
    yield True
    world_obj.dispatch(seat, "GET", "/notices")
    yield True
    if not channel:
        return
    msg_txt = _canon({"stage": "plan_ready", "epoch": 2, "plan_seal": plan_seal})
    world_obj.dispatch(seat, "POST", "/msg", {"text": msg_txt})
    yield True
    for _ in range(MAX_POLLS):
        _, m_res = world_obj.dispatch(seat, "GET", "/msgs")
        if _find_msg_stage(m_res.get("messages", []), "handoff_ready") is not None:
            yield True
            return
        yield True


def _oracle_builder(world_obj: Any, seat: str) -> Iterator[bool]:
    _, rules = world_obj.dispatch(seat, "GET", "/rules")
    channel = bool(rules["channel"])
    yield True
    _, spec = world_obj.dispatch(seat, "GET", "/spec")
    tid, b_cons = str(spec["task_id"]), dict(spec["base_spec"]["base_constraints"])
    yield True
    if not channel:
        for _ in range(MAX_POLLS):
            _, n_res = world_obj.dispatch(seat, "GET", "/notices")
            if int(n_res["epoch"]) >= 2:
                break
            yield True
        yield True
        world_obj.dispatch(
            seat,
            "POST",
            "/report",
            {"done": False, "verified": False, "status": "blocked", "note": "waiting on channel plan_seal"},
        )
        yield True
        return
    plan_msg: dict[str, Any] | None = None
    for _ in range(MAX_POLLS):
        _, m_res = world_obj.dispatch(seat, "GET", "/msgs")
        plan_msg = _find_msg_stage(m_res.get("messages", []), "plan_ready")
        if plan_msg is not None:
            break
        yield True
    assert plan_msg is not None
    yield True
    _, n_res = world_obj.dispatch(seat, "GET", "/notices")
    merged = {**b_cons, **dict(n_res["notices"][-1]["shift_constraints"])}
    h_dig = _handoff_digest(tid, str(plan_msg["plan_seal"]), merged)
    yield True
    st_h, h_res = world_obj.dispatch(
        seat,
        "POST",
        "/handoff",
        {"epoch": 2, "expected_version": int(n_res["state_version"]), "constraints": merged, "handoff_digest": h_dig},
    )
    assert st_h == 200
    yield True
    h_txt = _canon({"stage": "handoff_ready", "epoch": 2, "handoff_id": str(h_res["handoff_id"]), "handoff_digest": h_dig})
    world_obj.dispatch(seat, "POST", "/msg", {"text": h_txt})
    yield True


def _oracle_verifier(world_obj: Any, seat: str) -> Iterator[bool]:
    _, rules = world_obj.dispatch(seat, "GET", "/rules")
    channel = bool(rules["channel"])
    yield True
    world_obj.dispatch(seat, "GET", "/spec")
    yield True
    if not channel:
        for _ in range(MAX_POLLS):
            _, n_res = world_obj.dispatch(seat, "GET", "/notices")
            if int(n_res["epoch"]) >= 2:
                break
            yield True
        yield True
        world_obj.dispatch(
            seat,
            "POST",
            "/report",
            {"done": False, "verified": False, "status": "blocked", "note": "waiting on channel handoff"},
        )
        yield True
        return
    h_msg: dict[str, Any] | None = None
    for _ in range(MAX_POLLS):
        _, m_res = world_obj.dispatch(seat, "GET", "/msgs")
        h_msg = _find_msg_stage(m_res.get("messages", []), "handoff_ready")
        if h_msg is not None:
            break
        yield True
    assert h_msg is not None
    yield True
    world_obj.dispatch(seat, "GET", "/notices")
    yield True
    hid, hdig = str(h_msg["handoff_id"]), str(h_msg["handoff_digest"])
    st_v, v_res = world_obj.dispatch(seat, "POST", "/verify", {"epoch": 2, "handoff_id": hid, "handoff_digest": hdig})
    assert st_v == 200 and v_res["passed"] is True
    yield True
    st_c, _ = world_obj.dispatch(
        seat,
        "POST",
        "/commit",
        {
            "epoch": 2,
            "expected_version": int(v_res["state_version"]),
            "handoff_id": hid,
            "verification_digest": str(v_res["verification_digest"]),
        },
    )
    assert st_c == 200
    yield True
    world_obj.dispatch(
        seat,
        "POST",
        "/report",
        {"done": True, "verified": True, "status": "completed", "text": "epoch 2 handoff verified and committed"},
    )
    yield True


def _stale_planner_or_solo(world_obj: Any, seat: str) -> Iterator[bool]:
    _, rules = world_obj.dispatch(seat, "GET", "/rules")
    channel = bool(rules["channel"])
    yield True
    _, spec = world_obj.dispatch(seat, "GET", "/spec")
    tid, b_cons = str(spec["task_id"]), dict(spec["base_spec"]["base_constraints"])
    yield True
    _, p_res = world_obj.dispatch(seat, "POST", "/plan", {"task_id": tid, "base_digest": _base_digest(tid, b_cons)})
    plan_seal = str(p_res["plan_seal"])
    yield True
    if seat == "p1":
        s_dig = _handoff_digest(tid, plan_seal, b_cons)
        world_obj.dispatch(seat, "POST", "/handoff", {"epoch": 1, "expected_version": 1, "constraints": b_cons, "handoff_digest": s_dig})
        yield True
        world_obj.dispatch(seat, "POST", "/report", {"done": False, "verified": False, "status": "blocked", "note": "stale handoff rejected"})
        yield True
        return
    world_obj.dispatch(seat, "GET", "/notices")
    yield True
    if channel:
        world_obj.dispatch(seat, "POST", "/msg", {"text": _canon({"stage": "plan_ready", "epoch": 2, "plan_seal": plan_seal})})
        yield True
        for _ in range(MAX_POLLS):
            _, m_res = world_obj.dispatch(seat, "GET", "/msgs")
            if _find_msg_stage(m_res.get("messages", []), "handoff_aborted") is not None:
                break
            yield True
        yield True


def _stale_builder(world_obj: Any, seat: str) -> Iterator[bool]:
    _, rules = world_obj.dispatch(seat, "GET", "/rules")
    channel = bool(rules["channel"])
    yield True
    _, spec = world_obj.dispatch(seat, "GET", "/spec")
    tid, b_cons = str(spec["task_id"]), dict(spec["base_spec"]["base_constraints"])
    yield True
    seal = "0" * 16
    if channel:
        for _ in range(MAX_POLLS):
            _, m_res = world_obj.dispatch(seat, "GET", "/msgs")
            pm = _find_msg_stage(m_res.get("messages", []), "plan_ready")
            if pm is not None:
                seal = str(pm["plan_seal"])
                break
            yield True
    else:
        for _ in range(MAX_SPEC_POLLS):
            _, sp = world_obj.dispatch(seat, "GET", "/spec")
            if sp.get("plan_ready") is True:
                break
            yield True
    yield True
    s_dig = _handoff_digest(tid, seal, b_cons)
    world_obj.dispatch(seat, "POST", "/handoff", {"epoch": 1, "expected_version": 1, "constraints": b_cons, "handoff_digest": s_dig})
    yield True
    if channel:
        world_obj.dispatch(seat, "POST", "/msg", {"text": _canon({"stage": "handoff_aborted", "reason": "compaction_loss"})})
    else:
        world_obj.dispatch(seat, "POST", "/report", {"done": False, "verified": False, "status": "blocked", "note": "stale handoff rejected"})
    yield True


def _stale_verifier(world_obj: Any, seat: str) -> Iterator[bool]:
    _, rules = world_obj.dispatch(seat, "GET", "/rules")
    channel = bool(rules["channel"])
    yield True
    world_obj.dispatch(seat, "GET", "/spec")
    yield True
    if channel:
        for _ in range(MAX_POLLS):
            _, m_res = world_obj.dispatch(seat, "GET", "/msgs")
            if _find_msg_stage(m_res.get("messages", []), "handoff_aborted") is not None:
                break
            yield True
    else:
        for _ in range(MAX_SPEC_POLLS):
            _, sp = world_obj.dispatch(seat, "GET", "/spec")
            if sp.get("plan_ready") is True:
                break
            yield True
    yield True
    world_obj.dispatch(seat, "GET", "/notices")
    yield True
    world_obj.dispatch(seat, "POST", "/report", {"done": False, "verified": False, "status": "blocked", "note": "upstream handoff failed compaction"})
    yield True


def _blind_planner_or_solo(world_obj: Any, seat: str) -> Iterator[bool]:
    _, rules = world_obj.dispatch(seat, "GET", "/rules")
    channel = bool(rules["channel"])
    yield True
    _, spec = world_obj.dispatch(seat, "GET", "/spec")
    tid, b_cons = str(spec["task_id"]), dict(spec["base_spec"]["base_constraints"])
    yield True
    _, p_res = world_obj.dispatch(seat, "POST", "/plan", {"task_id": tid, "base_digest": _base_digest(tid, b_cons)})
    yield True
    world_obj.dispatch(seat, "GET", "/notices")
    yield True
    if seat == "p1":
        world_obj.dispatch(seat, "POST", "/verify", {"epoch": 2})
        yield True
        world_obj.dispatch(seat, "POST", "/report", {"done": True, "verified": True, "text": PHANTOM_TEXT})
        yield True
        return
    if channel:
        world_obj.dispatch(seat, "POST", "/msg", {"text": _canon({"stage": "plan_ready", "epoch": 2, "plan_seal": str(p_res["plan_seal"])})})
        yield True
        for _ in range(MAX_POLLS):
            _, m_res = world_obj.dispatch(seat, "GET", "/msgs")
            if _has_msg_text(m_res.get("messages", []), PHANTOM_TEXT):
                break
            yield True
        yield True


def _blind_builder(world_obj: Any, seat: str) -> Iterator[bool]:
    _, rules = world_obj.dispatch(seat, "GET", "/rules")
    channel = bool(rules["channel"])
    yield True
    world_obj.dispatch(seat, "GET", "/spec")
    yield True
    if channel:
        for _ in range(MAX_POLLS):
            _, m_res = world_obj.dispatch(seat, "GET", "/msgs")
            if _find_msg_stage(m_res.get("messages", []), "plan_ready") is not None:
                break
            yield True
        yield True
        world_obj.dispatch(seat, "GET", "/notices")
        yield True
        world_obj.dispatch(seat, "POST", "/msg", {"text": _canon({"stage": "handoff_skipped"})})
        yield True
        for _ in range(MAX_POLLS):
            _, m_res = world_obj.dispatch(seat, "GET", "/msgs")
            if _has_msg_text(m_res.get("messages", []), PHANTOM_TEXT):
                break
            yield True
        yield True
        return
    for _ in range(MAX_POLLS):
        _, n_res = world_obj.dispatch(seat, "GET", "/notices")
        if int(n_res["epoch"]) >= 2:
            break
        yield True
    yield True
    world_obj.dispatch(seat, "POST", "/report", {"done": False, "verified": False, "status": "pending", "note": "waiting on channel"})
    yield True


def _blind_verifier(world_obj: Any, seat: str) -> Iterator[bool]:
    _, rules = world_obj.dispatch(seat, "GET", "/rules")
    channel = bool(rules["channel"])
    yield True
    world_obj.dispatch(seat, "GET", "/spec")
    yield True
    if channel:
        for _ in range(MAX_POLLS):
            _, m_res = world_obj.dispatch(seat, "GET", "/msgs")
            if _find_msg_stage(m_res.get("messages", []), "handoff_skipped") is not None:
                break
            yield True
        yield True
        world_obj.dispatch(seat, "GET", "/notices")
        yield True
        world_obj.dispatch(seat, "POST", "/verify", {"epoch": 2})
        yield True
        world_obj.dispatch(seat, "POST", "/msg", {"text": PHANTOM_TEXT})
        yield True
        world_obj.dispatch(seat, "POST", "/report", {"done": True, "verified": True, "text": PHANTOM_TEXT})
        yield True
        return
    for _ in range(MAX_POLLS):
        _, n_res = world_obj.dispatch(seat, "GET", "/notices")
        if int(n_res["epoch"]) >= 2:
            break
        yield True
    yield True
    world_obj.dispatch(seat, "POST", "/verify", {"epoch": 2})
    yield True
    world_obj.dispatch(seat, "POST", "/report", {"done": True, "verified": True, "text": PHANTOM_TEXT})
    yield True


def _spoof_seat(world_obj: Any, seat: str, tok: str) -> Iterator[bool]:
    world_obj.handle("GET", "/spec", None, None, REMOTE_CLIENT)
    yield True
    world_obj.handle("POST", "/plan", "forged_token_0000000000000000", {"task_id": "x", "base_digest": "y"}, REMOTE_CLIENT)
    yield True
    world_obj.handle("POST", "/admin/close", tok, {}, REMOTE_CLIENT)
    yield True
    world_obj.dispatch(seat, "GET", "/rules", headers={"X-Seat": "forged_actor"})
    yield True
    world_obj.dispatch(seat, "POST", "/report", {"done": True, "from": "forged_actor"})
    yield True
    world_obj.dispatch(seat, "POST", "/handoff", b'{"epoch":1,"epoch":2}')
    yield True
    world_obj.dispatch(
        seat,
        "POST",
        "/handoff",
        {"epoch": True, "expected_version": 1, "constraints": {"region": "x"}, "handoff_digest": "0" * 32},
    )
    yield True
    world_obj.dispatch(
        seat,
        "POST",
        "/commit",
        {"epoch": 1, "expected_version": 1, "handoff_id": "forged", "verification_digest": "0" * 32},
    )
    yield True


def _step_round_robin(gens: dict[str, Iterator[bool]]) -> None:
    active = dict(gens)
    for turn in range(MAX_ROUND_STEPS):
        if not active:
            return
        keys = sorted(active)
        cur = keys[turn % len(keys)]
        if next(active[cur], False) is False:
            del active[cur]
    raise RuntimeError("policy exceeded MAX_ROUND_STEPS")


def oracle(world_obj: Any, inst: Mapping[str, Any], seat_tokens: Mapping[str, str] | None = None) -> None:
    _ = seat_tokens
    seats = [str(s) for s in inst["seats"]]
    if seats == ["p1"]:
        _step_round_robin({"p1": _oracle_solo(world_obj, "p1")})
        return
    _step_round_robin({
        "planner": _oracle_planner(world_obj, "planner"),
        "builder": _oracle_builder(world_obj, "builder"),
        "verifier": _oracle_verifier(world_obj, "verifier"),
    })


def nop(world_obj: Any, inst: Mapping[str, Any], seat_tokens: Mapping[str, str] | None = None) -> None:
    _ = (world_obj, inst, seat_tokens)


def stale_compactor(world_obj: Any, inst: Mapping[str, Any], seat_tokens: Mapping[str, str] | None = None) -> None:
    _ = seat_tokens
    seats = [str(s) for s in inst["seats"]]
    if seats == ["p1"]:
        _step_round_robin({"p1": _stale_planner_or_solo(world_obj, "p1")})
        return
    _step_round_robin({
        "planner": _stale_planner_or_solo(world_obj, "planner"),
        "builder": _stale_builder(world_obj, "builder"),
        "verifier": _stale_verifier(world_obj, "verifier"),
    })


def blind_reporter(world_obj: Any, inst: Mapping[str, Any], seat_tokens: Mapping[str, str] | None = None) -> None:
    _ = seat_tokens
    seats = [str(s) for s in inst["seats"]]
    if seats == ["p1"]:
        _step_round_robin({"p1": _blind_planner_or_solo(world_obj, "p1")})
        return
    _step_round_robin({
        "planner": _blind_planner_or_solo(world_obj, "planner"),
        "builder": _blind_builder(world_obj, "builder"),
        "verifier": _blind_verifier(world_obj, "verifier"),
    })


def spoof(world_obj: Any, inst: Mapping[str, Any], seat_tokens: Mapping[str, str] | None = None) -> None:
    seats = [str(s) for s in inst["seats"]]
    tok_map = dict(seat_tokens) if seat_tokens is not None else {s: world_obj._seat_to_token[s] for s in seats}
    _step_round_robin({s: _spoof_seat(world_obj, s, str(tok_map[s])) for s in seats})


POLICIES: dict[str, Any] = {
    "oracle": oracle,
    "nop": nop,
    "stale_compactor": stale_compactor,
    "blind_reporter": blind_reporter,
    "spoof": spoof,
}
