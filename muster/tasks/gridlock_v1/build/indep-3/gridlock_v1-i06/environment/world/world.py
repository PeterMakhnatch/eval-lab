from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

sys.dont_write_bytecode = True


def _load_dep(name: str) -> Any:
    existing = sys.modules.get(name)
    if (
        existing is not None
        and (name != "worldkit" or (hasattr(existing, "World") and hasattr(existing.World, "dispatch")))
        and (name != "codec" or hasattr(existing, "ADMIN_CLOSE_RESERVE"))
    ):
        return existing
    here = Path(__file__).resolve().parent
    cands = [here / f"{name}.py", Path("/world") / f"{name}.py"]
    if len(here.parents) >= 2:
        cands.append(here.parents[1] / "_kit_v2" / f"{name}.py")
    cand = next((p for p in cands if p.is_file()), cands[-1])
    spec = importlib.util.spec_from_file_location(name, cand)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


codec: Any = _load_dep("codec")
worldkit: Any = _load_dep("worldkit")
WorldBase: Any = worldkit.World
Reject: Any = worldkit.Reject

INSTANCE = Path(os.environ.get("WORLD_INSTANCE", str(Path(__file__).with_name("instance.json"))))
ZONES = ("A", "B", "C")
STAGES = (1, 2, 3)
CREWS = ("alpha", "beta", "gamma")
TIE_LINES = ("AB", "BC", "CA")
MAX_SEAT_REQUESTS = 512
MAX_CLAIMS_PER_SEAT = 3
CLAIM_EXPIRY_TICKS = 120
NEIGHBORS: dict[str, tuple[tuple[str, str], ...]] = {
    "A": (("AB", "B"), ("CA", "C")),
    "B": (("AB", "A"), ("BC", "C")),
    "C": (("BC", "B"), ("CA", "A")),
}
INCIDENT_LINES: dict[str, tuple[str, ...]] = {
    zone: tuple(line for line, _ in pairs) for zone, pairs in NEIGHBORS.items()
}
BUS_IDS = tuple(f"{zone}{stage}" for zone in ZONES for stage in STAGES)
CLAIM_TARGETS = frozenset(
    [
        *(f"crew:{c}" for c in CREWS),
        *(f"line:{ln}" for ln in TIE_LINES),
        *(f"crew:{c}:s{st}" for c in CREWS for st in STAGES),
        *(f"line:{ln}:s{st}" for ln in TIE_LINES for st in STAGES),
    ]
)


def _base_claim_target(target: str) -> str:
    parts = target.split(":")
    if len(parts) == 3 and parts[2] in ("s1", "s2", "s3"):
        return f"{parts[0]}:{parts[1]}"
    return target


def _normalize_tokens(instance: dict[str, Any], tokens: dict[str, str] | None) -> dict[str, str] | None:
    if tokens is None:
        return None
    seat_set = {str(s) for s in instance["seats"]}
    keys = {str(k) for k in tokens}
    vals = {str(v) for v in tokens.values()}
    if vals == seat_set:
        return {str(k): str(v) for k, v in tokens.items()}
    if keys == seat_set:
        return {str(v): str(k) for k, v in tokens.items()}
    return {str(k): str(v) for k, v in tokens.items()}


def compute_flows(
    bus_id: str,
    buses: dict[str, dict[str, Any]],
    repaired: dict[str, bool],
    energized: dict[str, str | None],
    thermal_cap: int,
) -> tuple[dict[str, int], str | None]:
    spec = buses[bus_id]
    zone, stage = str(spec["zone"]), int(spec["stage"])
    pairs = NEIGHBORS[zone]
    if not repaired[bus_id]:
        return {line: thermal_cap + 4 for line, _ in pairs}, "unrepaired_fault"
    if stage > 1 and not all(energized[f"{z}{stage - 1}"] is not None for z in ZONES):
        flows = {
            line: (thermal_cap + 4 if energized[f"{nbr}{stage - 1}"] is None else 4)
            for line, nbr in pairs
        }
        if max(flows.values()) <= thermal_cap:
            flows = {line: thermal_cap + 4 for line, _ in pairs}
        return flows, "missing_stage_support"
    if not spec["coupled"]:
        return {line: 4 for line, _ in pairs}, None
    flows = {}
    for line, nbr in pairs:
        if energized[f"{nbr}{stage}"] is not None:
            flows[line] = 4
        else:
            delta = (int(spec["phase_tap"]) - int(buses[f"{nbr}{stage}"]["phase_tap"])) % 6
            flows[line] = 8 + 4 * delta if delta in (1, 2) else 6
    reason = "tie_line_overload" if any(val > thermal_cap for val in flows.values()) else None
    return flows, reason


class Gridlock(WorldBase):
    budgets: ClassVar[dict[str, int]] = {}

    def __init__(
        self,
        instance: dict[str, Any],
        tokens: dict[str, str] | None = None,
        events: Path | None = None,
    ) -> None:
        seats = [str(s) for s in instance["seats"]]
        norm_toks = _normalize_tokens(instance, tokens)
        claim_locks = bool(instance.get("claim_locks", False))
        super().__init__(
            tokens=norm_toks,
            events=events,
            seats=seats if norm_toks is None else None,
            channel=bool(instance["channel"]),
            notices_enabled=False,
            leases_enabled=claim_locks,
            lease_expiry_ticks=CLAIM_EXPIRY_TICKS,
        )
        budget = codec.strict_int(instance["budget"])
        tcap = codec.strict_int(instance["thermal_cap"])
        assert budget is not None and budget > 0
        assert tcap is not None and tcap > 0
        assert sorted(instance["stations"]) == self.seats
        assert sorted(instance["initial_zone"]) == self.seats
        assert sorted(instance["buses"]) == sorted(BUS_IDS)
        self.action_budget = budget
        self.claim_locks = claim_locks
        self.max_leases_per_seat = MAX_CLAIMS_PER_SEAT if self.claim_locks else None
        self.thermal_cap = tcap
        self.stations = {str(s): [str(z) for z in zs] for s, zs in instance["stations"].items()}
        self.current_zone = {str(s): str(z) for s, z in instance["initial_zone"].items()}
        self.buses = {str(b): dict(spec) for b, spec in instance["buses"].items()}
        self.repaired: dict[str, bool] = dict.fromkeys(BUS_IDS, False)
        self.energized: dict[str, str | None] = dict.fromkeys(BUS_IDS, None)
        self.tripped: dict[str, bool] = dict.fromkeys(BUS_IDS, False)
        self.crew_busy: dict[str, tuple[str, str] | None] = dict.fromkeys(CREWS, None)
        self.actions_used: dict[str, int] = dict.fromkeys(self.seats, 0)
        self._logged_spoofs = 0
        for z in ZONES:
            self.route("GET", f"/zone/{z}", self._make_zone_handler(z))
        self._assert_invariants()

    def _make_zone_handler(self, zone: str) -> Callable[[str], dict[str, Any]]:
        def _handler(seat: str) -> dict[str, Any]:
            return self._get_specific_zone(seat, zone)

        return _handler

    def _assert_invariants(self) -> None:
        for seat in self.seats:
            assert 0 <= self.actions_used[seat] <= self.action_budget
        busy_buses = {pair[1]: crew for crew, pair in self.crew_busy.items() if pair is not None}
        for bus in BUS_IDS:
            if self.tripped[bus]:
                assert self.energized[bus] is None and not self.repaired[bus] and bus not in busy_buses
            elif self.energized[bus] is not None:
                assert self.repaired[bus] and bus not in busy_buses
            elif self.repaired[bus]:
                assert bus in busy_buses
            else:
                assert bus not in busy_buses

    def _execute_seat_request(
        self, seat: str, kind: str, method: str, body: dict[str, Any] | None
    ) -> tuple[int, dict[str, Any]]:
        if self.spoof_attempts > self._logged_spoofs:
            self._logged_spoofs = self.spoof_attempts
            req_log = body if method == "POST" else None
            return self._log(seat, kind, method, req_log, 403, {"error": "spoof_attempt_blocked"})
        if self.seat_requests.get(seat, 0) > MAX_SEAT_REQUESTS:
            return self._log(seat, kind, method, None, 429, {"error": "seat_request_limit_exceeded"})
        st_code, resp_out = super()._execute_seat_request(seat, kind, method, body)
        self._assert_invariants()
        return st_code, resp_out

    def _consume_action_budget(self, seat: str) -> int:
        used = self.actions_used[seat]
        if used >= self.action_budget:
            raise Reject(429, "action_budget_exhausted", action_budget_left=0)
        self.actions_used[seat] = used + 1
        return self.action_budget - (used + 1)

    def _check_action_budget(self, seat: str) -> None:
        if self.actions_used[seat] >= self.action_budget:
            raise Reject(429, "action_budget_exhausted", action_budget_left=0)

    def valid_lease_target(self, target: str) -> bool:
        return target in CLAIM_TARGETS

    def require_claim_free_or_owned(self, seat: str, base_target: str) -> None:
        if not self.claim_locks:
            return
        for key, entry in sorted(self.active_leases.items()):
            if _base_claim_target(key) == base_target and entry["holder"] != seat:
                raise Reject(409, "guard_rejected", owner=entry["holder"], target=key)

    def post_claim(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_on(self.claim_locks, "leases_disabled")
        if set(body) != {"target"}:
            raise Reject(400, "invalid_claim_schema")
        target = codec.clean_str(body.get("target"), 128)
        if target is None or target not in CLAIM_TARGETS:
            raise Reject(400, "invalid_target")
        base = _base_claim_target(target)
        for key, entry in sorted(self.active_leases.items()):
            if _base_claim_target(key) == base and entry["holder"] != seat:
                raise Reject(
                    409,
                    "already_claimed",
                    target=target,
                    holder=entry["holder"],
                    expires_tick=entry["expires_tick"],
                )
        if base.startswith("line:"):
            line = base[5:]
            cur = self.current_zone[seat]
            if cur not in ZONES or line not in INCIDENT_LINES[cur]:
                raise Reject(
                    403,
                    "line_not_incident",
                    target=target,
                    current_zone=cur,
                    incident_tie_lines=list(INCIDENT_LINES.get(cur, ())),
                )
        elif base.startswith("crew:") and target not in self.active_leases:
            held_crews = sum(
                1 for k, c in self.active_leases.items() if k.startswith("crew:") and c["holder"] == seat
            )
            if held_crews >= 1:
                raise Reject(409, "claim_limit", limit=1, kind="crew")
        return super().post_claim(seat, {"target": target})

    def post_release(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_on(self.claim_locks, "leases_disabled")
        if not body or not ({"target"} <= set(body) <= {"target", "lease_id"}):
            raise Reject(400, "invalid_release_schema")
        return super().post_release(seat, body)

    def post_msg(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_on(self.channel, "channel_disabled")
        if set(body) != {"text"}:
            raise Reject(400, "invalid_msg_schema")
        return super().post_msg(seat, body)

    def _free_bus_crew(self, bus_id: str) -> None:
        for crew in CREWS:
            pair = self.crew_busy[crew]
            if pair is not None and pair[1] == bus_id:
                self.crew_busy[crew] = None

    def _zone_view(self, zone: str) -> dict[str, Any]:
        assert zone in ZONES
        return {
            "zone": zone,
            "thermal_cap": self.thermal_cap,
            "incident_tie_lines": list(INCIDENT_LINES[zone]),
            "buses": [
                {
                    "bus": f"{zone}{stage}",
                    "stage": stage,
                    "required_crew": self.buses[f"{zone}{stage}"]["required_crew"],
                    "coupled": self.buses[f"{zone}{stage}"]["coupled"],
                    "phase_tap": self.buses[f"{zone}{stage}"]["phase_tap"],
                    "repaired": self.repaired[f"{zone}{stage}"],
                    "energized": self.energized[f"{zone}{stage}"] is not None,
                    "tripped": self.tripped[f"{zone}{stage}"],
                }
                for stage in STAGES
            ],
        }

    def get_rules(self, seat: str) -> dict[str, Any]:
        rem = self.action_budget - self.actions_used[seat]
        return {
            **super().get_rules(seat),
            "claim_locks": self.claim_locks,
            "claim_ttl_ticks": self.lease_expiry_ticks,
            "stations": {other: list(zones) for other, zones in sorted(self.stations.items())},
            "your_zones": list(self.stations[seat]),
            "current_zone": self.current_zone[seat],
            "crews": list(CREWS),
            "tie_lines": list(TIE_LINES),
            "thermal_cap": self.thermal_cap,
            "action_budget_left": rem,
        }

    def get_zone(self, seat: str) -> dict[str, Any]:
        cur = self.current_zone[seat]
        if cur not in ZONES:
            raise Reject(409, "at_depot", current_zone=cur, your_zones=list(self.stations[seat]))
        return self._zone_view(cur)

    def _get_specific_zone(self, seat: str, zone: str) -> dict[str, Any]:
        if zone != self.current_zone[seat]:
            raise Reject(403, "zone_forbidden", zone=zone, current_zone=self.current_zone[seat])
        return self._zone_view(zone)

    def get_mine(self, seat: str) -> dict[str, Any]:
        return {
            "current_zone": self.current_zone[seat],
            "action_budget_left": self.action_budget - self.actions_used[seat],
            "energized_by_you": [bus for bus in BUS_IDS if self.energized[bus] == seat],
        }

    def _bus_in_current_zone(self, seat: str, body: dict[str, Any]) -> str:
        bus = codec.clean_str(body.get("bus"), 16)
        if bus is None or bus not in self.buses:
            raise Reject(400, "invalid_bus")
        zone = str(self.buses[bus]["zone"])
        if zone != self.current_zone[seat]:
            raise Reject(403, "zone_forbidden", zone=zone, current_zone=self.current_zone[seat])
        return bus

    def post_move_zone(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        if set(body) != {"to_zone"}:
            raise Reject(400, "invalid_move_zone_schema")
        zone = codec.clean_str(body.get("to_zone"), 16)
        if zone is None or zone not in ZONES:
            raise Reject(400, "invalid_zone")
        if zone not in self.stations[seat]:
            raise Reject(403, "zone_forbidden", zone=zone, your_zones=list(self.stations[seat]))
        prev = self.current_zone[seat]
        if zone == prev:
            raise Reject(409, "already_in_zone", to_zone=zone)
        rem = self._consume_action_budget(seat)
        self.current_zone[seat] = zone
        return {"from_zone": prev, "to_zone": zone, "action_budget_left": rem}

    def post_dispatch_crew(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        if set(body) != {"bus", "crew"}:
            raise Reject(400, "invalid_dispatch_schema")
        bus = self._bus_in_current_zone(seat, body)
        crew = codec.clean_str(body.get("crew"), 32)
        if crew is None or crew not in CREWS:
            raise Reject(400, "invalid_crew")
        if self.tripped[bus]:
            raise Reject(409, "breaker_tripped", bus=bus)
        if self.energized[bus] is not None:
            raise Reject(409, "already_energized", bus=bus)
        if self.repaired[bus]:
            raise Reject(409, "already_repaired", bus=bus)
        spec = self.buses[bus]
        if crew != spec["required_crew"]:
            raise Reject(409, "wrong_crew", bus=bus, crew=crew, required_crew=spec["required_crew"])
        if self.claim_locks:
            self.require_claim_free_or_owned(seat, f"crew:{crew}")
            busy_guard = self.crew_busy[crew]
            if busy_guard is not None and busy_guard[0] != seat:
                raise Reject(409, "guard_rejected", owner=busy_guard[0], target=f"crew:{crew}")
        rem = self._consume_action_budget(seat)
        stage = int(spec["stage"])
        if stage > 1 and not all(self.energized[f"{z}{stage - 1}"] is not None for z in ZONES):
            return {
                "bus": bus,
                "crew": crew,
                "dispatched": False,
                "repaired": False,
                "crew_conflict": False,
                "stage_locked": True,
                "stage": stage,
                "required_stage": stage - 1,
                "action_budget_left": rem,
            }
        busy = self.crew_busy[crew]
        if busy is not None:
            return {
                "bus": bus,
                "crew": crew,
                "dispatched": False,
                "repaired": False,
                "crew_conflict": True,
                "stage_locked": False,
                "busy_seat": busy[0],
                "busy_bus": busy[1],
                "action_budget_left": rem,
            }
        self.repaired[bus] = True
        self.crew_busy[crew] = (seat, bus)
        return {
            "bus": bus,
            "crew": crew,
            "dispatched": True,
            "repaired": True,
            "crew_conflict": False,
            "stage_locked": False,
            "action_budget_left": rem,
        }

    def _apply_overload_trip(
        self, bus: str, zone: str, flows: dict[str, int], reason: str, rem: int
    ) -> dict[str, Any]:
        self._free_bus_crew(bus)
        self.repaired[bus] = False
        self.tripped[bus] = True
        tripped_lines = sorted(line for line, val in flows.items() if val > self.thermal_cap)
        affected = {zone} | {nbr for line, nbr in NEIGHBORS[zone] if flows[line] > self.thermal_cap}
        reset_buses: list[str] = []
        for z in sorted(affected):
            for st in (3, 2, 1):
                cand = f"{z}{st}"
                if self.energized[cand] is not None:
                    self.energized[cand] = None
                    self.repaired[cand] = False
                    self.tripped[cand] = True
                    reset_buses.append(cand)
                    break
        return {
            "bus": bus,
            "zone": zone,
            "energized": False,
            "overload_trip": True,
            "trip_reason": reason,
            "tie_line_flows": {"overloaded": True, "lines": tripped_lines},
            "thermal_cap": self.thermal_cap,
            "reset_buses": reset_buses,
            "action_budget_left": rem,
        }

    def post_energize(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        if set(body) != {"bus"}:
            raise Reject(400, "invalid_energize_schema")
        bus = self._bus_in_current_zone(seat, body)
        if self.tripped[bus]:
            raise Reject(409, "breaker_tripped", bus=bus)
        if self.energized[bus] is not None:
            raise Reject(409, "already_energized", bus=bus)
        zone = str(self.buses[bus]["zone"])
        if self.claim_locks:
            for line in INCIDENT_LINES[zone]:
                self.require_claim_free_or_owned(seat, f"line:{line}")
        rem = self._consume_action_budget(seat)
        flows, reason = compute_flows(bus, self.buses, self.repaired, self.energized, self.thermal_cap)
        if reason is not None:
            return self._apply_overload_trip(bus, zone, flows, reason, rem)
        self._free_bus_crew(bus)
        self.energized[bus] = seat
        return {
            "bus": bus,
            "zone": zone,
            "energized": True,
            "overload_trip": False,
            "tie_line_flows": {"overloaded": False, "lines": []},
            "thermal_cap": self.thermal_cap,
            "reset_buses": [],
            "action_budget_left": rem,
        }


def make_world(
    instance: dict[str, Any],
    tokens: dict[str, str] | None = None,
    events: Path | None = None,
) -> Gridlock:
    return Gridlock(instance, tokens=tokens, events=events)


def main() -> None:
    instance = json.loads(INSTANCE.read_text(encoding="utf-8"))
    worldkit.run_server(Gridlock(instance))


if __name__ == "__main__":
    main()
