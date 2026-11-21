from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SeatRun:
    seat: str
    under_test: bool
    agent: str
    model: str | None
    policy: str | None
    exception: str | None
    started_at: str | None
    finished_at: str | None
    tokens_in: int | None
    tokens_cache: int | None
    tokens_out: int | None
    cost_usd: float | None
    trajectory: Path | None
    logs: Path

    @property
    def label(self) -> str:
        name = self.policy or self.agent
        return f"{name}/{self.model}" if self.model else name


@dataclass(frozen=True)
class SeatTrial:
    path: Path
    trial_name: str
    task: str
    task_checksum: str | None
    rewards: dict[str, float]
    reward_valid: bool
    exception: str | None
    seats: tuple[SeatRun, ...]
    artifacts: Path
    started_at: str | None
    finished_at: str | None
    task_path: str | None = None
    play_finished_at: str | None = None

    def roster_key(self) -> str:
        return ",".join(f"{run.seat}={run.label}" for run in self.seats)


@dataclass(frozen=True)
class Problem:
    path: Path
    reason: str


class InputError(ValueError):
    pass
