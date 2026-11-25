from __future__ import annotations

import os
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from random import Random
from types import SimpleNamespace
from typing import Any

from muster.lab import api, load

MUTANTS = (
    "grader_at_least", "leak_secret", "oracle_gives_up", "nondeterministic", "crash",
    "budget_unequal", "oracle_spins", "replay_drift", "trap_no_bite", "credits_refused",
)
_SEATS = ("p1", "p2", "p3")
_CONDITIONS = ("calm", "busy")
_SET = 4
_CAS = 12
_KIT = Path(__file__).resolve().parents[1] / "tasks" / "_kit" / "worldkit.py"


def conditions() -> list[str]:
    return list(_CONDITIONS)


def instances(condition: str) -> list[dict]:
    assert condition in _CONDITIONS
    result = []
    for i in range(1, 5):
        name = f"i{i:02d}"
        rng = Random(name)
        values = [rng.randint(1, 20) for _ in range(9)]
        orders = {seat: values[j * 3:j * 3 + 3] for j, seat in enumerate(_SEATS)}
        if condition == "calm":
            orders = {"p1": values, "p2": [], "p3": []}
        result.append({"id": name, "condition": condition, "orders": orders,
                       "secret": f"{rng.getrandbits(64):016x}", "total": sum(values)})
    return result


def seats(instance: dict) -> list[str]:
    return list(_SEATS)


def hidden(instance: dict, seat: str) -> list[str]:
    assert seat in _SEATS
    return [instance["secret"]]


def _budgets(instance: dict, mutant: str | None) -> dict[str, int]:
    return {"set": 6 if mutant == "budget_unequal" and instance["condition"] == "busy" else _SET,
            "cas": _CAS}


def _tokens(instance: dict) -> dict[str, str]:
    return {
        sha256(f"toy/{instance['id']}/{seat}".encode()).hexdigest()[:32]: seat
        for seat in _SEATS
    }


def _world_factory(kit: Any, mutant: str | None) -> Callable:
    class Counter(kit.World):
        def __init__(self, instance: dict, events: Path):
            super().__init__(tokens=_tokens(instance), events=events)
            self.instance = instance
            self.budgets = _budgets(instance, mutant)
            self.value = 0
            self.version = 0
            self.reads = 0
            self.last_read = dict.fromkeys(_SEATS, 0)

        def get_rules(self, seat: str) -> dict:
            rules = super().get_rules(seat)
            if mutant == "leak_secret":
                rules["secret"] = self.instance["secret"]
            return rules

        def get_orders(self, seat: str) -> dict:
            return {"orders": list(self.instance["orders"][seat])}

        def get_counter(self, seat: str) -> dict:
            self.reads += 1
            if mutant == "replay_drift" and self.reads % 5 == 0:
                self.value += 1
            self.last_read[seat] = self.value
            response = self.snapshot()
            if mutant == "nondeterministic":
                response["nonce"] = os.urandom(16).hex()
            return response

        def snapshot(self) -> dict:
            return {"value": self.value, "version": self.version}

        def write(self, value: int) -> dict:
            self.value = value
            self.version += 1
            return self.snapshot()

        def post_set(self, seat: str, body: dict) -> dict:
            value = body.get("value")
            if type(value) is not int:
                if mutant == "crash":
                    raise TypeError("value is not an integer")
                raise kit.Reject(400, "value must be an integer")
            if set(body) != {"value"}:
                raise kit.Reject(400, "expected only value")
            if mutant == "trap_no_bite":
                value = self.value + value - self.last_read[seat]
            return self.write(value)

        def post_cas(self, seat: str, body: dict) -> dict:
            value, version = body.get("value"), body.get("version")
            if type(value) is not int or type(version) is not int:
                raise kit.Reject(400, "value and version must be integers")
            if version != self.version:
                raise kit.Reject(409, "stale version", **self.snapshot())
            return self.write(value)

    def make_world(instance: dict, events: Path, clock: api.Clock) -> api.WorldLike:
        kit.time = SimpleNamespace(time=clock)
        return Counter(instance, events)

    return make_world


def _reconstruct(kit: Any, events: list[dict]) -> dict:
    state = {"value": 0, "version": 0}
    for event in kit.in_play(events):
        if event["kind"] in ("set", "cas"):
            state = {key: event["response"][key] for key in state}
    return state


def _credit_refused(events: list[dict], final: int) -> int:
    value = final
    for event in events:
        if event["kind"] == "admin.close" and event["ok"]:
            break
        if event["seat"] is None or event["kind"] not in ("set", "cas"):
            continue
        if event["ok"]:
            value = event["response"]["value"]
        elif event["kind"] == "set" and type((event["request"] or {}).get("value")) is int:
            value = event["request"]["value"]
    return value


def _oracle(seat: str, mutant: str | None) -> api.Play:
    status, orders = yield "GET", "/orders", None
    assert status == 200
    amount = sum(orders["orders"])
    if not amount:
        return
    status, data = yield "GET", "/counter", None
    assert status == 200
    for _ in range(_CAS):
        body = {"value": data["value"] + amount, "version": data["version"]}
        status, data = yield "POST", "/cas", body
        if status != 409 or mutant == "oracle_gives_up":
            return
        if mutant == "oracle_spins":
            while True:
                yield "POST", "/cas", body


def _naive(seat: str) -> api.Play:
    status, data = yield "GET", "/orders", None
    assert status == 200
    amount = sum(data["orders"])
    if amount:
        status, data = yield "GET", "/counter", None
        assert status == 200
        yield "POST", "/set", {"value": data["value"] + amount}


def _idle(seat: str) -> api.Play:
    yield "GET", "/rules", None


def _overshoot(seat: str) -> api.Play:
    yield "POST", "/set", {"value": 10**9}


def _fuzz(seat: str) -> api.Play:
    yield "POST", "/set", {}
    yield "POST", "/set", {"value": "bad"}
    yield "POST", "/cas", {"value": "bad", "version": 0}
    yield "POST", "/cas", {"value": 1, "version": None}
    yield "POST", "/cas", {"value": True, "version": 0}
    yield "POST", "/set", {"value": 10**9, "version": "bad"}


def _refused_exact(seat: str) -> api.Play:
    status, data = yield "GET", "/orders", None
    assert status == 200
    amount = sum(data["orders"])
    if amount:
        yield "POST", "/set", {"value": amount, "extra": True}



def _greedy_reader(seat: str) -> api.Play:
    for _ in range(3):
        for path in ("/rules", "/orders", "/counter"):
            yield "GET", path, None


def claims() -> list[dict]:
    return [
        {"id": "oracle_perfect", "type": "equals", "policy": "oracle",
         "conditions": ["calm", "busy"], "measure": "reward", "value": 1.0},
        {"id": "idle_zero", "type": "at_most", "policy": "idle",
         "conditions": ["calm", "busy"], "measure": "reward", "value": 0.0},
        {"id": "overshoot_zero", "type": "at_most", "policy": "overshoot",
         "conditions": ["calm", "busy"], "measure": "reward", "value": 0.0},
        {"id": "counter_ladder", "type": "ladder", "policies": ["naive", "oracle"],
         "conditions": ["calm", "busy"], "measure": "reward"},
        {"id": "equal_budget", "type": "equal_budget", "conditions": ["calm", "busy"]},
    ]


def family(mutant: str | None = None) -> SimpleNamespace:
    if mutant is not None and mutant not in MUTANTS:
        raise load.LabError(f"unknown counter mutant: {mutant}")
    kit = load.module(_KIT, "toy_kit")

    def replay(events: list[dict], instance: dict) -> dict:
        return _reconstruct(kit, events)

    def score(events: list[dict], instance: dict) -> dict[str, float]:
        value = replay(events, instance)["value"]
        if mutant == "credits_refused":
            value = _credit_refused(events, value)
        total = instance["total"]
        versions = [event["response"]["version"] for event in kit.in_play(events)
                    if event["kind"] in ("set", "cas")]
        continuous = all(version == index for index, version in enumerate(versions, 1))
        won = value >= total if mutant == "grader_at_least" else value == total
        return {"reward": float(won and continuous), "lost": float(total - value)}

    def state(world: Any) -> dict:
        return world.snapshot()

    def policies() -> dict[str, api.Policy]:
        return {
            "oracle": lambda seat: _oracle(seat, mutant), "naive": _naive,
            "idle": _idle, "overshoot": _overshoot, "fuzz": _fuzz,
            "greedy_reader": _greedy_reader, "refused_exact": _refused_exact,
        }

    def budget(instance: dict) -> int:
        return len(_SEATS) * sum(_budgets(instance, mutant).values())

    return SimpleNamespace(NAME="counter", conditions=conditions, instances=instances, seats=seats,
                           make_world=_world_factory(kit, mutant), score=score, policies=policies,
                           claims=claims, hidden=hidden, budget=budget, state=state, replay=replay)
