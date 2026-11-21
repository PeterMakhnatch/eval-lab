"""Dataclasses shared by all modules (docs/DESIGN.md section 3)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SeatRun:
    """One seat's agent run in a trial."""

    seat: str  # service name
    under_test: bool
    agent: str  # harness name, e.g. "oracle", "terminus-2", "script"
    model: str | None  # "provider/name" or None
    policy: str | None  # ScriptAgent: the script's stem; otherwise None
    exception: str | None  # exception type on this seat
    started_at: str | None  # ISO, agent execution
    finished_at: str | None
    tokens_in: int | None
    tokens_cache: int | None
    tokens_out: int | None
    cost_usd: float | None
    trajectory: Path | None  # seats/<seat>/agent/trajectory.json when present
    logs: Path  # seats/<seat>/agent

    @property
    def label(self) -> str:
        """What sits in the seat: policy-or-agent[/model]."""
        name = self.policy or self.agent
        return f"{name}/{self.model}" if self.model else name


@dataclass(frozen=True)
class SeatTrial:
    """One trial of a task with seats, as Harbor recorded it."""

    path: Path
    trial_name: str
    task: str  # last part of task_name, lower case
    task_checksum: str | None
    rewards: dict[str, float]  # every key the verifier wrote; {} when there is none
    reward_valid: bool
    exception: str | None  # trial-level exception type
    seats: tuple[SeatRun, ...]  # task declaration order
    artifacts: Path  # <trial>/artifacts
    started_at: str | None
    finished_at: str | None
    task_path: str | None = None  # the task directory that Harbor ran, as recorded
    play_finished_at: str | None = None  # ISO, the end of agent execution for the whole trial

    def roster_key(self) -> str:
        """ "p1=oracle,p2=nop": seat=policy-or-agent[/model], declaration order."""
        return ",".join(f"{run.seat}={run.label}" for run in self.seats)


@dataclass(frozen=True)
class Problem:
    """A directory that is not a readable seat trial, and why."""

    path: Path
    reason: str


class InputError(ValueError):
    """Unusable input: the command line prints the message on one line and exits with status 3."""
