from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any, Protocol

Request = tuple[str, str, dict[str, Any] | None]
Reply = tuple[int, dict[str, Any]]
Play = Generator[Request, Reply, None]
Policy = Callable[[str], Play]

REMOTE = "10.0.0.2"
LOOPBACK = "127.0.0.1"
MAX_STEPS = 20_000
ENDED = ("all_done", "step_limit")
ADAPTER_REQUIRED = ("NAME", "conditions", "instances", "seats", "make_world", "score", "policies", "claims")
ADAPTER_OPTIONAL = ("hidden", "budget", "state", "replay", "sources", "CARD_FREE_KINDS")


class WorldLike(Protocol):
    tokens: dict[str, str]

    def handle(
        self, method: str, path: str, token: str | None, body: object, client: str
    ) -> Reply: ...


class Schedule(Protocol):
    def pick(self, ready: list[str], step: int) -> str: ...


@dataclass(frozen=True)
class Episode:
    family: str
    condition: str
    instance: str
    lineup: dict[str, str]
    order: tuple[str, ...]
    events: tuple[dict[str, Any], ...]
    rewards: dict[str, float]
    steps: dict[str, int]
    ended: str
    state: dict[str, Any] | None
    replayed: dict[str, Any] | None


Clock = Callable[[], float]
