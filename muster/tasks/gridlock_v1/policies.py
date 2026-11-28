from __future__ import annotations

import json
import sys
from collections.abc import Iterator, Mapping
from typing import Any

sys.dont_write_bytecode = True

ZONES = ("A", "B", "C")
PATIENCE = 80
MAX_ROUND_STEPS = 1000
REMOTE_CLIENT = "10.0.0.2"


def _safe_order(taps: dict[str, int]) -> list[str]:
    vals = set(taps.values())
    for base in range(6):
        if {(base + k) % 6 for k in (0, 1, 2)} == vals:
            by_tap = {tap: zone for zone, tap in taps.items()}
            return [by_tap[(base + k) % 6] for k in (0, 1, 2)]
    raise ValueError(f"invalid phase taps: {taps}")


def _parse_msgs(messages: list[Any]) -> tuple[dict[str, dict[int, dict[str, Any]]], set[str]]:
    specs: dict[str, dict[int, dict[str, Any]]] = {}
    done: set[str] = set()
    for msg in messages:
        if not isinstance(msg, Mapping):
            continue
        try:
            obj = json.loads(str(msg.get("text") or ""))
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("kind") == "spec" and obj.get("zone") in ZONES and isinstance(obj.get("buses"), list):
            zone = str(obj["zone"])
            if zone not in specs and len(obj["buses"]) == 3:
                specs[zone] = {int(b["stage"]): b for b in obj["buses"]}
        elif obj.get("kind") == "done" and isinstance(obj.get("bus"), str):
            done.add(obj["bus"])
    return specs, done


def _claim_bundle(world_obj: Any, seat: str, targets: list[str]) -> Iterator[bool]:
    held: list[str] = []
    for target in targets:
        status, _ = world_obj.dispatch(seat, "POST", "/claim", {"target": target})
        yield False
        if status == 200:
            held.append(target)
        else:
            for item in held:
                world_obj.dispatch(seat, "POST", "/release", {"target": item})
                yield False
            yield False
            return
    yield True


def _release_bundle(world_obj: Any, seat: str, targets: list[str]) -> Iterator[bool]:
    for target in targets:
        world_obj.dispatch(seat, "POST", "/release", {"target": target})
        yield True


def _restore_bus(
    world_obj: Any, seat: str, bus_spec: dict[str, Any], lines: list[str], claim_locks: bool
) -> Iterator[bool]:
    st = int(bus_spec["stage"])
    targets = [f"crew:{bus_spec['required_crew']}:s{st}", *[f"line:{ln}:s{st}" for ln in lines]]
    for _ in range(PATIENCE):
        if claim_locks:
            got = False
            for res in _claim_bundle(world_obj, seat, targets):
                got = res
                yield True
            if not got:
                continue
        st_d, rep = world_obj.dispatch(
            seat, "POST", "/dispatch_crew", {"bus": bus_spec["bus"], "crew": bus_spec["required_crew"]}
        )
        yield True
        if st_d == 200 and rep.get("dispatched") is True:
            st_e, _ = world_obj.dispatch(seat, "POST", "/energize", {"bus": bus_spec["bus"]})
            assert st_e == 200
            yield True
            if claim_locks:
                yield from _release_bundle(world_obj, seat, targets)
            return
        if claim_locks:
            yield from _release_bundle(world_obj, seat, targets)
        if st_d != 409:
            raise RuntimeError(f"dispatch failed on {bus_spec['bus']}: {st_d} {rep}")
    raise RuntimeError(f"patience exceeded on {bus_spec['bus']}")


def _oracle_solo(world_obj: Any, seat: str, cur: str) -> Iterator[bool]:
    specs: dict[str, dict[int, dict[str, Any]]] = {}
    for zone in ZONES:
        if cur != zone:
            st_m, _ = world_obj.dispatch(seat, "POST", "/move_zone", {"to_zone": zone})
            assert st_m == 200
            yield True
            cur = zone
        st_z, view = world_obj.dispatch(seat, "GET", "/zone")
        assert st_z == 200
        yield True
        specs[zone] = {int(b["stage"]): b for b in view["buses"]}
        st_d, _ = world_obj.dispatch(
            seat, "POST", "/dispatch_crew", {"bus": f"{zone}1", "crew": specs[zone][1]["required_crew"]}
        )
        assert st_d == 200
        yield True
        st_e, _ = world_obj.dispatch(seat, "POST", "/energize", {"bus": f"{zone}1"})
        assert st_e == 200
        yield True
    order2 = _safe_order({z: int(specs[z][2]["phase_tap"]) for z in ZONES})
    for zone in order2:
        if cur != zone:
            st_m, _ = world_obj.dispatch(seat, "POST", "/move_zone", {"to_zone": zone})
            assert st_m == 200
            yield True
            cur = zone
        st_d, _ = world_obj.dispatch(
            seat, "POST", "/dispatch_crew", {"bus": f"{zone}2", "crew": specs[zone][2]["required_crew"]}
        )
        assert st_d == 200
        yield True
        st_e, _ = world_obj.dispatch(seat, "POST", "/energize", {"bus": f"{zone}2"})
        assert st_e == 200
        yield True


def _oracle_team(
    world_obj: Any, seat: str, view: dict[str, Any], claim_locks: bool
) -> Iterator[bool]:
    my_zone = str(view["zone"])
    lines = list(view["incident_tie_lines"])
    world_obj.dispatch(seat, "POST", "/msg", {"text": json.dumps({"kind": "spec", "zone": my_zone, "buses": view["buses"]})})
    yield True
    specs: dict[str, dict[int, dict[str, Any]]] = {}
    done: set[str] = set()
    for _ in range(PATIENCE):
        _, m_res = world_obj.dispatch(seat, "GET", "/msgs")
        specs, done = _parse_msgs(m_res.get("messages", []))
        yield True
        if len(specs) == 3:
            break
    if len(specs) < 3:
        my_buses = {int(b["stage"]): b for b in view["buses"]}
        yield from _restore_bus(world_obj, seat, my_buses[1], lines, claim_locks)
        return
    orders = {
        1: list(ZONES),
        2: _safe_order({z: int(specs[z][2]["phase_tap"]) for z in ZONES}),
        3: _safe_order({z: int(specs[z][3]["phase_tap"]) for z in ZONES}),
    }
    for stage in (1, 2, 3):
        order = orders[stage]
        pos = order.index(my_zone)
        if pos > 0:
            prev_bus = f"{order[pos - 1]}{stage}"
            for _ in range(PATIENCE):
                _, m_res = world_obj.dispatch(seat, "GET", "/msgs")
                _, done = _parse_msgs(m_res.get("messages", []))
                yield True
                if prev_bus in done:
                    break
            if prev_bus not in done:
                return
        yield from _restore_bus(world_obj, seat, specs[my_zone][stage], lines, claim_locks)
        world_obj.dispatch(seat, "POST", "/msg", {"text": json.dumps({"kind": "done", "bus": f"{my_zone}{stage}"})})
        yield True
        needed = {f"{z}{stage}" for z in ZONES}
        for _ in range(PATIENCE):
            if needed.issubset(done):
                break
            _, m_res = world_obj.dispatch(seat, "GET", "/msgs")
            _, done = _parse_msgs(m_res.get("messages", []))
            yield True


def _oracle_seat(world_obj: Any, seat: str) -> Iterator[bool]:
    st_r, rules = world_obj.dispatch(seat, "GET", "/rules")
    assert st_r == 200
    yield True
    if len(rules["your_zones"]) == 3:
        yield from _oracle_solo(world_obj, seat, str(rules["current_zone"]))
        return
    st_z, view = world_obj.dispatch(seat, "GET", "/zone")
    assert st_z == 200
    yield True
    if not rules["channel"]:
        b1 = view["buses"][0]
        world_obj.dispatch(seat, "POST", "/dispatch_crew", {"bus": b1["bus"], "crew": b1["required_crew"]})
        yield True
        world_obj.dispatch(seat, "POST", "/energize", {"bus": b1["bus"]})
        yield True
        return
    yield from _oracle_team(world_obj, seat, view, bool(rules.get("claim_locks", False)))


def _naive_solo(world_obj: Any, seat: str, cur: str) -> Iterator[bool]:
    specs: dict[str, dict[int, dict[str, Any]]] = {}
    for stage in (1, 2):
        for zone in ZONES:
            if cur != zone:
                st_m, _ = world_obj.dispatch(seat, "POST", "/move_zone", {"to_zone": zone})
                yield True
                if st_m != 200:
                    return
                cur = zone
            if zone not in specs:
                _, view = world_obj.dispatch(seat, "GET", "/zone")
                yield True
                specs[zone] = {int(b["stage"]): b for b in view["buses"]}
            bus = specs[zone][stage]
            st_d, rep = world_obj.dispatch(
                seat, "POST", "/dispatch_crew", {"bus": bus["bus"], "crew": bus["required_crew"]}
            )
            yield True
            if st_d == 200 and rep.get("dispatched"):
                world_obj.dispatch(seat, "POST", "/energize", {"bus": bus["bus"]})
                yield True


def _naive_seat(world_obj: Any, seat: str) -> Iterator[bool]:
    _, rules = world_obj.dispatch(seat, "GET", "/rules")
    yield True
    if len(rules["your_zones"]) == 3:
        yield from _naive_solo(world_obj, seat, str(rules["current_zone"]))
        return
    claim_locks = bool(rules.get("claim_locks", False))
    _, view = world_obj.dispatch(seat, "GET", "/zone")
    yield True
    lines = list(view["incident_tie_lines"])
    buses = {int(b["stage"]): b for b in view["buses"]}
    yield from _restore_bus(world_obj, seat, buses[1], lines, claim_locks)
    b2 = buses[2]
    t2 = [f"crew:{b2['required_crew']}:s2", *[f"line:{ln}:s2" for ln in lines]]
    for _ in range(4):
        if claim_locks:
            got = False
            for res in _claim_bundle(world_obj, seat, t2):
                got = res
                yield True
            if not got:
                return
        st_d, rep = world_obj.dispatch(seat, "POST", "/dispatch_crew", {"bus": b2["bus"], "crew": b2["required_crew"]})
        yield True
        if st_d == 200 and (rep.get("stage_locked") or rep.get("crew_conflict")):
            if claim_locks:
                yield from _release_bundle(world_obj, seat, t2)
            continue
        if st_d == 200 and rep.get("dispatched"):
            world_obj.dispatch(seat, "POST", "/energize", {"bus": b2["bus"]})
            yield True
        if claim_locks:
            yield from _release_bundle(world_obj, seat, t2)
        return


def _spoof_seat(world_obj: Any, seat: str, tok: str) -> Iterator[bool]:
    _, rules = world_obj.dispatch(seat, "GET", "/rules")
    yield True
    me, reach = rules["you"], rules["your_zones"]
    others = [s for s in rules["seats"] if s != me] or ["admin"]
    forged = tok[:-1] + ("x" if tok[-1] != "x" else "y")
    outside = [z for z in ZONES if z not in reach]
    target_zone = outside[0] if outside else ZONES[0]
    for z in ZONES:
        if z != rules["current_zone"]:
            st_z, _ = world_obj.dispatch(seat, "GET", f"/zone/{z}")
            assert st_z == 403
            yield True
    if outside:
        for path, body in (
            ("/move_zone", {"to_zone": target_zone}),
            ("/dispatch_crew", {"bus": f"{target_zone}1", "crew": "alpha"}),
            ("/energize", {"bus": f"{target_zone}1"}),
        ):
            st_o, _ = world_obj.dispatch(seat, "POST", path, body)
            assert st_o == 403
            yield True
    for bad_tok in (None, "0" * 32, forged):
        for method, path, body in (
            ("GET", "/zone", None),
            ("POST", "/dispatch_crew", {"bus": f"{target_zone}1", "crew": "alpha"}),
        ):
            st_u, _ = world_obj.handle(method, path, bad_tok, body, REMOTE_CLIENT)
            assert st_u == 401
            yield True
    st_hs, _ = world_obj.dispatch(seat, "GET", "/rules", headers={"X-Seat": others[0]})
    assert st_hs == 403
    yield True
    cur_z = rules["current_zone"] if rules["current_zone"] in ZONES else ZONES[0]
    st_bs, _ = world_obj.dispatch(seat, "POST", "/dispatch_crew", {"bus": f"{cur_z}1", "crew": "alpha", "seat": others[0]})
    assert st_bs == 403
    yield True
    st_dup, _ = world_obj.dispatch(seat, "POST", "/dispatch_crew", b'{"bus":"A1","bus":"A2"}')
    assert st_dup == 400
    yield True
    st_ex, _ = world_obj.dispatch(seat, "POST", "/energize", {"bus": f"{cur_z}1", "extra": 1})
    assert st_ex == 400
    yield True
    for r_tok in (None, tok):
        st_adm, _ = world_obj.handle("POST", "/admin/close", r_tok, {}, REMOTE_CLIENT)
        assert st_adm == 403
        yield True
    if rules.get("claim_locks"):
        cur = str(rules["current_zone"])
        non_inc = [ln for ln in ("AB", "BC", "CA") if ln not in ("AB", "CA")][0]
        if cur == "B":
            non_inc = "CA"
        elif cur == "C":
            non_inc = "AB"
        else:
            non_inc = "BC"
        st_ni, _ = world_obj.dispatch(seat, "POST", "/claim", {"target": f"line:{non_inc}:s1"})
        assert st_ni == 403
        yield True
        st_un, _ = world_obj.handle("POST", "/claim", None, {"target": "crew:alpha:s1"}, REMOTE_CLIENT)
        assert st_un == 401
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
