from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

PHASES = ("environment_setup", "agent_setup", "agent_execution", "verifier")


class Counting(StrEnum):
    VALID = "valid"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class Seat:
    name: str
    model: bool
    exception: str | None
    started_at: datetime | None
    finished_at: datetime | None
    turns: int | None
    max_turns: int | None
    calls: int | None
    exit_code: int | None = None
    agent_steps: int | None = None
    last_step_at: datetime | None = None

    def seconds(self) -> float | None:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None


@dataclass(frozen=True, slots=True)
class Progress:
    seat: str
    state: str
    agent_steps: int = 0
    last_step_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Running:
    path: Path
    started_at: datetime | None
    seats: tuple[Progress, ...]


@dataclass(frozen=True, slots=True)
class Trial:
    path: Path
    trial_name: str
    task: str
    task_name: str
    task_checksum: str | None
    source: str | None
    agent: str | None
    agent_version: str | None
    model: str | None
    environment: str | None
    reward: float | None
    valid: bool
    rewards: Mapping[str, float]
    exception: str | None
    started_at: datetime | None
    finished_at: datetime | None
    phase_seconds: Mapping[str, float]
    tokens_in: int | None
    tokens_cache: int | None
    tokens_out: int | None
    cost_usd: float | None
    harbor_version: str | None = None
    conditions: Mapping[str, Any] = field(default_factory=dict)
    seats: tuple[str, ...] = ()
    model_seats: tuple[str, ...] = ()
    seat_exceptions: Mapping[str, str] = field(default_factory=dict)
    partial_usage: tuple[str, ...] = ()
    key_matched: bool | None = None
    seat_runs: tuple[Seat, ...] = ()
    agent_started_at: datetime | None = None
    agent_finished_at: datetime | None = None
    not_started: str | None = None

    def metric(self, name: str) -> float | None:
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
    path: Path
    harbor_version: str | None
    n_total_trials: int | None
    n_retries: int | None


@dataclass(frozen=True, slots=True)
class Problem:
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
    seen: dict[Any, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)


def tasks_of(arms: Sequence[Arm]) -> list[str]:
    if not arms:
        return []
    shared = set(arms[0].by_task())
    for arm in arms[1:]:
        shared &= set(arm.by_task())
    return sorted(shared)
