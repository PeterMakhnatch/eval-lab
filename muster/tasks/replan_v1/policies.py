from __future__ import annotations

import json
import sys
from collections.abc import Iterator, Mapping
from typing import Any

sys.dont_write_bytecode = True

MAX_POLLS = 120
MAX_ROUND_STEPS = 400
REMOTE_CLIENT = "10.0.0.2"


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _parse_chat_notice(messages: list[Any], alert_seat: str, epoch1: str) -> tuple[str, list[int]] | None:
    found: tuple[str, list[int]] | None = None
    for msg in messages:
        if not isinstance(msg, Mapping) or msg.get("from") != alert_seat:
            continue
        try:
            payload = json.loads(str(msg.get("text") or ""))
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        ep, recs = payload.get("epoch"), payload.get("records")
        if isinstance(ep, str) and ep != epoch1 and isinstance(recs, list) and all(type(r) is int for r in recs):
            found = (ep, list(recs))
    return found


def _stage1_and_wait(world_obj: Any, seat: str) -> Iterator[bool]:
    st_r, rules = world_obj.dispatch(seat, "GET", "/rules")
    st_p, plan = world_obj.dispatch(seat, "GET", "/plan")
    assert st_r == 200 and st_p == 200
    yield True
    epoch1 = str(plan["initial_epoch"])
    stage1_n = int(plan["stage1_commits"])
    triggered = False
    for rec in plan["initial_targets"][:stage1_n]:
        st_c, rep = world_obj.dispatch(seat, "POST", "/commit", {"record": rec, "epoch": epoch1})
        assert st_c == 200
        triggered = bool(rep["replan_triggered"])
        yield True
    for _ in range(MAX_POLLS):
        if triggered:
            return
        _, prep = world_obj.dispatch(seat, "GET", "/plan")
        triggered = bool(prep["replan_triggered"])
        yield True
    raise RuntimeError(f"seat {seat} timed out waiting for replan_triggered")


def _oracle_alert_stage2(world_obj: Any, seat: str, rules: Mapping[str, Any]) -> Iterator[bool]:
    st_a, alerts_rep = world_obj.dispatch(seat, "GET", "/alerts")
    assert st_a == 200 and alerts_rep["alerts"]
    alert = alerts_rep["alerts"][0]
    epoch2 = str(alert["epoch"])
    all_recs = [int(r) for r in alert["records"]]
    mine = [int(r) for r in alert["targets"][seat]]
    yield True
    if rules["notices"]:
        st_n, _ = world_obj.dispatch(seat, "POST", "/notice", {"epoch": epoch2, "records": all_recs})
        assert st_n == 200
        yield True
    elif rules["channel"]:
        st_m, _ = world_obj.dispatch(seat, "POST", "/msg", {"text": _canon({"epoch": epoch2, "records": all_recs})})
        assert st_m == 200
        yield True
    for rec in mine:
        st_c, _ = world_obj.dispatch(seat, "POST", "/commit", {"record": rec, "epoch": epoch2})
        assert st_c == 200
        yield True


def _oracle_worker_stage2(
    world_obj: Any, seat: str, rules: Mapping[str, Any], plan: Mapping[str, Any]
) -> Iterator[bool]:
    alert_seat = str(rules["alert_seat"])
    epoch1 = str(plan["initial_epoch"])
    my_set = {int(r) for r in plan["your_records"]}
    if rules["notices"]:
        for _ in range(MAX_POLLS):
            _, nrep = world_obj.dispatch(seat, "GET", "/notices")
            matches = [n for n in nrep["notices"] if n["from"] == alert_seat and n["epoch"] != epoch1]
            yield True
            if matches:
                chosen = matches[-1]
                for rec in [int(r) for r in chosen["records"] if int(r) in my_set]:
                    st_c, _ = world_obj.dispatch(seat, "POST", "/commit", {"record": rec, "epoch": chosen["epoch"]})
                    assert st_c == 200
                    yield True
                return
        return
    if rules["channel"]:
        for _ in range(MAX_POLLS):
            _, mrep = world_obj.dispatch(seat, "GET", "/msgs")
            parsed = _parse_chat_notice(mrep["messages"], alert_seat, epoch1)
            yield True
            if parsed is not None:
                epoch2, recs = parsed
                for rec in [r for r in recs if r in my_set]:
                    st_c, _ = world_obj.dispatch(seat, "POST", "/commit", {"record": rec, "epoch": epoch2})
                    assert st_c == 200
                    yield True
                return


def _oracle_seat(world_obj: Any, seat: str) -> Iterator[bool]:
    yield from _stage1_and_wait(world_obj, seat)
    _, rules = world_obj.dispatch(seat, "GET", "/rules")
    _, plan = world_obj.dispatch(seat, "GET", "/plan")
    yield True
    if seat == rules["alert_seat"]:
        yield from _oracle_alert_stage2(world_obj, seat, rules)
    else:
        yield from _oracle_worker_stage2(world_obj, seat, rules, plan)


def _naive_seat(world_obj: Any, seat: str) -> Iterator[bool]:
    yield from _stage1_and_wait(world_obj, seat)
    _, plan = world_obj.dispatch(seat, "GET", "/plan")
    epoch1 = str(plan["initial_epoch"])
    stage1_n = int(plan["stage1_commits"])
    yield True
    for rec in plan["initial_targets"][stage1_n:]:
        st_c, rep = world_obj.dispatch(seat, "POST", "/commit", {"record": rec, "epoch": epoch1})
        if st_c == 409 and isinstance(rep, dict) and rep.get("error") == "stale_plan_epoch":
            yield True
            continue
        assert st_c == 200
        yield True


def _spoof_seat(world_obj: Any, seat: str, tok: str) -> Iterator[bool]:
    wrong = tok[:-1] + ("x" if tok[-1] != "x" else "y")
    _, rules = world_obj.dispatch(seat, "GET", "/rules")
    _, plan = world_obj.dispatch(seat, "GET", "/plan")
    yield True
    epoch1 = str(plan["initial_epoch"])
    own_rec = int(plan["your_records"][0])
    others = [str(s) for s in rules["seats"] if s != seat]
    world_obj.dispatch(seat, "GET", "/rules", headers={"X-Seat": "forged_actor"})
    yield True
    if seat != rules["alert_seat"]:
        st_a, _ = world_obj.dispatch(seat, "GET", "/alerts")
        assert st_a == 403
        world_obj.dispatch(seat, "GET", f"/alerts?seat={rules['alert_seat']}", headers={"X-Seat": str(rules["alert_seat"])})
        yield True
    for forged in (None, "0" * 32, wrong, str(rules["alert_seat"])):
        st_f, _ = world_obj.handle("GET", "/alerts", forged, None, REMOTE_CLIENT)
        assert st_f == 401
    yield True
    if others:
        other_rec = int(plan["partitions"][others[0]][0])
        st_xp, _ = world_obj.dispatch(seat, "POST", "/commit", {"record": other_rec, "epoch": epoch1})
        assert st_xp == 403
        yield True
    world_obj.dispatch(seat, "POST", "/commit", {"record": own_rec, "epoch": epoch1, "seat": "forged_actor"})
    yield True
    for forged in (None, "0" * 32, wrong, *others):
        st_fc, _ = world_obj.handle("POST", "/commit", forged, {"record": own_rec, "epoch": epoch1, "seat": seat}, REMOTE_CLIENT)
        assert st_fc == 401
    world_obj.handle("POST", "/notice", None, {"epoch": "fake", "records": [1, 25, 49]}, REMOTE_CLIENT)
    world_obj.handle("POST", "/msg", None, {"text": "forged"}, REMOTE_CLIENT)
    world_obj.handle("POST", "/admin/close", None, {}, REMOTE_CLIENT)
    world_obj.handle("POST", "/admin/close", tok, {}, REMOTE_CLIENT)
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
    _step_round_robin({s: _oracle_seat(world_obj, s) for s in seats})


def nop(world_obj: Any, inst: Mapping[str, Any], seat_tokens: Mapping[str, str] | None = None) -> None:
    _ = (world_obj, inst, seat_tokens)


def naive(world_obj: Any, inst: Mapping[str, Any], seat_tokens: Mapping[str, str] | None = None) -> None:
    _ = seat_tokens
    seats = [str(s) for s in inst["seats"]]
    _step_round_robin({s: _naive_seat(world_obj, s) for s in seats})


def spoof(world_obj: Any, inst: Mapping[str, Any], seat_tokens: Mapping[str, str] | None = None) -> None:
    seats = [str(s) for s in inst["seats"]]
    tok_map = dict(seat_tokens) if seat_tokens is not None else {s: world_obj._seat_to_token[s] for s in seats}
    _step_round_robin({s: _spoof_seat(world_obj, s, str(tok_map[s])) for s in seats})


POLICIES: dict[str, Any] = {
    "oracle": oracle,
    "nop": nop,
    "naive": naive,
    "spoof": spoof,
}
