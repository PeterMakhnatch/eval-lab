from __future__ import annotations

import json
import math
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from muster.lab import api
from muster.lab.load import LabError, check_adapter


class PolicyError(LabError):
    def __init__(self, seat: str, policy: str, step: int, original: Exception):
        super().__init__(f"policy {policy!r} for seat {seat!r} at step {step}: {original}")
        self.seat = seat
        self.policy = policy
        self.step = step
        self.original = original


class WorldError(LabError):
    def __init__(self, seat: str | None, step: int, path: str, original: Exception):
        super().__init__(f"world request {path!r} from {seat!r} at step {step}: {original}")
        self.seat = seat
        self.step = step
        self.path = path
        self.original = original


class LogicalClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self) -> None:
        self.now += 1.0


def _handle(
    world: api.WorldLike,
    clock: LogicalClock,
    request: api.Request,
    token: str | None,
    seat: str | None,
    step: int,
) -> api.Reply:
    method, path, body = request
    clock.advance()
    try:
        reply = world.handle(
            method, path, token, deepcopy(body), api.LOOPBACK if seat is None else api.REMOTE
        )
    except Exception as error:
        raise WorldError(seat, step, path, error) from error
    return deepcopy(reply)


def _start(zoo: dict[str, api.Policy], lineup: dict[str, str]) -> dict[str, api.Play]:
    players = {}
    for seat, name in sorted(lineup.items()):
        try:
            players[seat] = zoo[name](seat)
        except Exception as error:
            raise PolicyError(seat, name, 0, error) from error
    return players


def _request(value: object, seat: str, policy: str) -> api.Request:
    message = f"invalid request from seat {seat!r}, policy {policy!r}: {value!r}"
    assert isinstance(value, tuple) and len(value) == 3, message
    method, path, body = value
    assert method in ("GET", "POST"), message
    assert isinstance(path, str) and path.startswith("/"), message
    assert body is None or isinstance(body, dict), message
    return method, path, body


def _play(
    world: api.WorldLike,
    clock: LogicalClock,
    lineup: dict[str, str],
    zoo: dict[str, api.Policy],
    schedule: api.Schedule,
    max_steps: int,
) -> tuple[tuple[str, ...], dict[str, int], str]:
    players = _start(zoo, lineup)
    tokens = {seat: token for token, seat in world.tokens.items() if seat in players}
    assert set(tokens) == set(players), "missing seat tokens"
    assert sum(seat in players for seat in world.tokens.values()) == len(players), (
        "duplicate seat tokens"
    )
    pending: dict[str, api.Reply | None] = dict.fromkeys(players)
    steps = dict.fromkeys(players, 0)
    order: list[str] = []
    for _ in range(max_steps + len(players)):
        if not players or len(order) == max_steps:
            break
        ready = sorted(players)
        step = len(order)
        seat = schedule.pick(ready, step)
        assert seat in players, f"schedule selected {seat!r} outside ready seats {ready}"
        try:
            reply = pending[seat]
            value = next(players[seat]) if reply is None else players[seat].send(reply)
        except StopIteration:
            del players[seat]
            continue
        except Exception as error:
            raise PolicyError(seat, lineup[seat], step, error) from error
        request = _request(value, seat, lineup[seat])
        pending[seat] = _handle(world, clock, request, tokens[seat], seat, step)
        order.append(seat)
        steps[seat] += 1
    ended = "step_limit" if len(order) == max_steps else "all_done"
    assert ended == "step_limit" or not players
    return tuple(order), steps, ended


def run(
    adapter: Any,
    instance: dict,
    lineup: dict[str, str],
    schedule: api.Schedule,
    *,
    max_steps: int = api.MAX_STEPS,
    zoo: dict[str, api.Policy] | None = None,
) -> api.Episode:
    check_adapter(adapter)
    assert type(max_steps) is int and max_steps >= 1, "max_steps must be positive"
    seats = adapter.seats(instance)
    assert len(seats) == len(set(seats)), "adapter returned duplicate seats"
    assert set(seats) == set(lineup), "lineup must cover every seat exactly"
    policies = adapter.policies() if zoo is None else zoo
    assert set(lineup.values()) <= set(policies), "lineup contains an unknown policy"
    clock = LogicalClock()
    with TemporaryDirectory(prefix="muster-lab-") as directory:
        path = Path(directory) / "events.jsonl"
        world = adapter.make_world(instance, path, clock)
        order, steps, ended = _play(world, clock, lineup, policies, schedule, max_steps)
        status, _ = _handle(world, clock, ("POST", "/admin/close", {}), None, None, len(order))
        assert status == 200, f"world close rejected: {status}"
        events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        rewards = adapter.score(events, instance)
        reward = rewards.get("reward")
        assert type(reward) is float and math.isfinite(reward), "reward must be a finite float"
        state = replayed = None
        if hasattr(adapter, "state") and hasattr(adapter, "replay"):
            state = adapter.state(world)
            replayed = adapter.replay(events, instance)
        return api.Episode(
            adapter.NAME, instance["condition"], instance["id"], dict(lineup), order,
            tuple(events), rewards, steps, ended, state, replayed,
        )


def strip_time(events: Any) -> tuple[dict, ...]:
    return tuple({key: value for key, value in event.items() if key != "t"} for event in events)
