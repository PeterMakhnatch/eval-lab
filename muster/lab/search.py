from __future__ import annotations

import math
from collections.abc import Sequence
from random import Random
from statistics import fmean
from typing import Any

from muster.lab import api, episode
from muster.lab.schedule import Bursty, RoundRobin, Scripted, Seeded

_MAX_BURST = 16
_MAX_TRIES = 10_000
_MAX_MUTATIONS = 64


def _value(result: api.Episode, measure: str) -> float:
    value = result.rewards[measure]
    assert math.isfinite(value), f"non-finite measure: {measure}"
    return -math.inf if result.ended == "step_limit" else value


def _mutate(order: tuple[str, ...], rng: Random, swap: bool) -> list[str]:
    assert len(order) >= 2
    result = list(order)
    a, b = sorted(rng.sample(range(len(result)), 2))
    if swap:
        result[a], result[b] = result[b], result[a]
    else:
        block = result[a:b]
        del result[a:b]
        destination = rng.randrange(len(result) + 1)
        result[destination:destination] = block
    return result


def _search(
    adapter: Any,
    instance: dict,
    lineup: dict[str, str],
    *,
    measure: str,
    tries: int,
    seed: int,
    max_steps: int,
    zoo: dict[str, api.Policy] | None,
    minimize: bool,
) -> api.Episode:
    assert 0 <= tries <= _MAX_TRIES
    rng = Random(seed)

    def play(schedule: api.Schedule) -> api.Episode:
        return episode.run(adapter, instance, lineup, schedule, max_steps=max_steps, zoo=zoo)

    chosen = play(RoundRobin())
    chosen_value = _value(chosen, measure)
    for i in range(tries + min(tries, _MAX_MUTATIONS)):
        if i < tries:
            candidate_seed = rng.getrandbits(64)
            schedule: api.Schedule = (
                Seeded(candidate_seed)
                if i % 2 == 0
                else Bursty(candidate_seed, rng.randint(1, _MAX_BURST))
            )
        elif len(chosen.order) >= 2:
            schedule = Scripted(_mutate(chosen.order, rng, swap=i % 2 == 0))
        else:
            break
        candidate = play(schedule)
        value = _value(candidate, measure)
        improved = value < chosen_value if minimize else value > chosen_value
        if improved:
            chosen, chosen_value = candidate, value
    return chosen


def worst(
    adapter: Any,
    instance: dict,
    lineup: dict[str, str],
    *,
    measure: str = "reward",
    tries: int,
    seed: int,
    max_steps: int = api.MAX_STEPS,
    zoo: dict[str, api.Policy] | None = None,
) -> api.Episode:
    return _search(adapter, instance, lineup, measure=measure, tries=tries, seed=seed,
                   max_steps=max_steps, zoo=zoo, minimize=True)


def best(
    adapter: Any,
    instance: dict,
    lineup: dict[str, str],
    *,
    measure: str = "reward",
    tries: int,
    seed: int,
    max_steps: int = api.MAX_STEPS,
    zoo: dict[str, api.Policy] | None = None,
) -> api.Episode:
    return _search(adapter, instance, lineup, measure=measure, tries=tries, seed=seed,
                   max_steps=max_steps, zoo=zoo, minimize=False)


def spread(
    adapter: Any,
    instance: dict,
    lineup: dict[str, str],
    *,
    measure: str,
    seeds: Sequence[int],
    max_steps: int = api.MAX_STEPS,
    zoo: dict[str, api.Policy] | None = None,
) -> tuple[float, float, float]:
    assert seeds and len(seeds) <= _MAX_TRIES
    values = [
        _value(
            episode.run(adapter, instance, lineup, Seeded(seed), max_steps=max_steps, zoo=zoo),
            measure,
        )
        for seed in seeds
    ]
    return min(values), fmean(values), max(values)
