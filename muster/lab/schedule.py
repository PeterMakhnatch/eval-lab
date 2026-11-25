from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from random import Random

from muster.lab.api import Schedule
from muster.lab.load import LabError


class RoundRobin:
    def __init__(self) -> None:
        self.last = ""

    def pick(self, ready: list[str], step: int) -> str:
        assert ready
        self.last = ready[bisect_right(ready, self.last) % len(ready)]
        return self.last


class Seeded:
    def __init__(self, seed: int) -> None:
        self.rng = Random(seed)

    def pick(self, ready: list[str], step: int) -> str:
        assert ready
        return self.rng.choice(ready)


class Bursty:
    def __init__(self, seed: int, run: int) -> None:
        assert run >= 1
        self.rng = Random(seed)
        self.run = run
        self.remaining = 0
        self.seat = ""

    def pick(self, ready: list[str], step: int) -> str:
        assert ready
        if self.remaining == 0 or self.seat not in ready:
            choices = [seat for seat in ready if seat != self.seat] if len(ready) > 1 else ready
            self.seat = self.rng.choice(choices)
            self.remaining = self.rng.randint(1, self.run)
        self.remaining -= 1
        return self.seat


class Scripted:
    def __init__(self, order: Sequence[str]) -> None:
        self.order = tuple(order)

    def pick(self, ready: list[str], step: int) -> str:
        assert ready and step >= 0
        if step < len(self.order) and self.order[step] in ready:
            return self.order[step]
        return ready[0]


def parse(text: str) -> Schedule:
    parts = text.split(":")
    try:
        match parts:
            case ["round"]:
                return RoundRobin()
            case ["seed", seed]:
                return Seeded(int(seed))
            case ["bursty", seed, run] if int(run) >= 1:
                return Bursty(int(seed), int(run))
            case ["order", order]:
                seats = order.split(",") if order else []
                if all(seat and seat == seat.strip() for seat in seats):
                    return Scripted(seats)
    except ValueError as error:
        raise LabError(f"invalid schedule: {text!r}") from error
    raise LabError(f"invalid schedule: {text!r}; use round, seed:N, bursty:N:R or order:p1,p2")
