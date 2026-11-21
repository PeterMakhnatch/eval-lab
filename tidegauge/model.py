"""The kernel contract: what every command may rely on.

A ``Trial`` is one agent attempt at one task, read from a Harbor trial directory.
A ``Job`` is what Harbor recorded about one ``harbor run``.
An ``Arm`` is a named set of trials that the user treats as one configuration.
Commands read these objects. They never open Harbor files themselves.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

PHASES = ("environment_setup", "agent_setup", "agent_execution", "verifier")


class Counting(StrEnum):
    """Which trials count toward a score.

    Harbor and the Terminal-Bench leaderboard use different rules, and the
    choice can move a score by tens of points.
    """

    VALID = "valid"  # Harbor's job mean: only trials with a valid reward count
    ALL = "all"  # Terminal-Bench leaderboard: every trial counts, no reward means 0


@dataclass(frozen=True, slots=True)
class Trial:
    path: Path
    trial_name: str
    task: str  # canonical task key, used to align arms. ``--task-key`` can take it from a part of the name
    task_name: str  # the name exactly as Harbor wrote it
    task_checksum: str | None
    source: str | None  # dataset the task came from
    agent: str | None  # a seat trial: its roster, as in ``p1=terminus-2,p2=oracle``
    agent_version: str | None
    model: str | None  # a seat trial: the models of the roster, as in ``p1=openai/gpt-5.5,p2=-``
    environment: str | None  # docker, daytona, modal, ...
    reward: float | None  # the reward Harbor captured, valid or not
    valid: bool  # Harbor's ``reward_valid``: a reward exists and no exception spoils it
    rewards: Mapping[str, float]
    exception: str | None  # Harbor exception type, if the trial raised
    started_at: datetime | None
    finished_at: datetime | None
    phase_seconds: Mapping[str, float]
    tokens_in: int | None  # a seat trial: the sum over its seats, or None when a seat does not report
    tokens_cache: int | None
    tokens_out: int | None
    cost_usd: float | None
    harbor_version: str | None = None
    conditions: Mapping[str, Any] = field(default_factory=dict)  # settings that change the result
    # A seat trial runs several agents, 1 per seat (the olamlabs/harbor-multiagent fork).
    seats: tuple[str, ...] = ()  # seat names in task order. Empty: the trial has 1 agent
    model_seats: tuple[str, ...] = ()  # the seats that used a model
    seat_exceptions: Mapping[str, str] = field(default_factory=dict)  # seat -> exception type
    partial_usage: tuple[str, ...] = ()  # usage fields that only a part of the model seats report
    key_matched: bool | None = None  # ``--task-key`` gave the task key. None: no ``--task-key``

    def metric(self, name: str) -> float | None:
        """Value of a named per-trial metric, or ``None`` when it is not recorded."""
        if name == "reward":
            return self.reward
        if name in self.rewards:
            return self.rewards[name]
        if name == "cost_usd":
            return self.cost_usd
        if name in ("tokens_in", "tokens_out", "tokens_cache"):
            value = getattr(self, name)
            return float(value) if value is not None else None
        if name == "seconds":
            if self.started_at and self.finished_at:
                return (self.finished_at - self.started_at).total_seconds()
            return None
        if name.endswith("_seconds") and name[: -len("_seconds")] in PHASES:
            return self.phase_seconds.get(name[: -len("_seconds")])
        raise KeyError(f"unknown metric: {name}")


METRICS = (
    "reward",
    "cost_usd",
    "tokens_in",
    "tokens_out",
    "tokens_cache",
    "seconds",
    *(f"{p}_seconds" for p in PHASES),
)


@dataclass(frozen=True, slots=True)
class Job:
    """One ``harbor run``, from the job-level ``result.json`` and ``lock.json``."""

    path: Path
    harbor_version: str | None
    n_total_trials: int | None  # how many trials Harbor planned
    n_retries: int | None  # attempts that Harbor ran again; it deletes the earlier ones


@dataclass(frozen=True, slots=True)
class Problem:
    """A file that could not be read as a trial."""

    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class Arm:
    name: str
    trials: tuple[Trial, ...]
    problems: tuple[Problem, ...] = ()
    jobs: tuple[Job, ...] = ()

    def by_task(self) -> dict[str, list[Trial]]:
        out: dict[str, list[Trial]] = {}
        for trial in self.trials:
            out.setdefault(trial.task, []).append(trial)
        return out

    def outcomes(self, metric: str = "reward", counting: Counting = Counting.VALID) -> dict[str, list[float]]:
        """``task -> values`` for the statistics functions.

        For ``reward``, ``counting`` decides: ``valid`` keeps the trials with a
        valid reward, ``all`` keeps every trial and reads a missing reward as 0.
        For every other metric, a trial without the value is left out. A name
        that is not in ``METRICS`` is a reward name, such as ``seat.p1``.
        """
        out: dict[str, list[float]] = {}
        for trial in self.trials:
            value = trial.metric(metric) if metric in METRICS else trial.rewards.get(metric)
            if metric == "reward":
                if counting is Counting.VALID and not (trial.valid and value is not None):
                    continue
                if value is None:
                    value = 0.0
            elif value is None:
                continue
            out.setdefault(trial.task, []).append(value)
        return out


def distinct(values: Iterable[Any]) -> list[Any]:
    """Distinct values in first-seen order."""
    seen: dict[Any, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)


def tasks_of(arms: Sequence[Arm]) -> list[str]:
    """Tasks that every arm has, sorted."""
    if not arms:
        return []
    shared = set(arms[0].by_task())
    for arm in arms[1:]:
        shared &= set(arm.by_task())
    return sorted(shared)
