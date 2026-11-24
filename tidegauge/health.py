from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tidegauge.model import Seat, Trial

STALL_SECONDS = 300.0
EARLY_SHARE = 0.5
KINDS = ("lost", "stalled", "zero-calls", "turn-cap", "early-end")
UNMEASURED = frozenset({"lost", "stalled", "zero-calls"})


@dataclass(frozen=True, slots=True)
class SeatFault:
    kind: str
    seat: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"seat": self.seat, **self.evidence}


def stamp(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat().replace("+00:00", "Z")


def seconds(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def silence(seat: Seat, trial: Trial) -> float | None:
    end = seat.finished_at or trial.agent_finished_at or trial.finished_at
    last = seat.last_step_at or seat.started_at or trial.agent_started_at
    if end is None or last is None:
        return None
    return max(0.0, (end - last).total_seconds())


def agent_phase(trial: Trial) -> float | None:
    starts = [s.started_at for s in trial.seat_runs if s.started_at]
    ends = [s.finished_at for s in trial.seat_runs if s.finished_at]
    start = trial.agent_started_at or (min(starts) if starts else None)
    end = trial.agent_finished_at or (max(ends) if ends else None)
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def _lost(seat: Seat, trial: Trial) -> SeatFault | None:
    if seat.exit_code in (None, 0) or seat.exception is not None or trial.reward is None:
        return None
    return SeatFault(
        "lost",
        seat.name,
        {"exit_code": seat.exit_code, "played_s": seconds(seat.seconds()), "phase_s": seconds(agent_phase(trial))},
    )


def _stalled(seat: Seat, trial: Trial, stall_seconds: float) -> SeatFault | None:
    if seat.exception != "AgentTimeoutError":
        return None
    silent = silence(seat, trial)
    if silent is None or silent < stall_seconds:
        return None
    return SeatFault(
        "stalled",
        seat.name,
        {
            "silent_s": seconds(silent),
            "agent_steps": seat.agent_steps,
            "last_step_at": stamp(seat.last_step_at),
            "ended_at": stamp(seat.finished_at or trial.agent_finished_at or trial.finished_at),
        },
    )


def median_play(trial: Trial) -> float | None:
    played = [s for seat in trial.seat_runs if (s := seat.seconds()) is not None]
    return statistics.median(played) if len(played) >= 2 else None


def _early(seat: Seat, trial: Trial) -> SeatFault | None:
    if not seat.model or seat.exception is not None or seat.exit_code not in (None, 0):
        return None
    played, median = seat.seconds(), median_play(trial)
    if played is None or median is None or median <= 0 or played >= EARLY_SHARE * median:
        return None
    return SeatFault("early-end", seat.name, {"played_s": seconds(played), "median_s": seconds(median)})


def classify(seat: Seat, trial: Trial, stall_seconds: float = STALL_SECONDS) -> SeatFault | None:
    assert stall_seconds > 0
    fault = _lost(seat, trial) or _stalled(seat, trial, stall_seconds)
    if fault is not None:
        return fault
    if seat.model and seat.calls == 0:
        return SeatFault("zero-calls", seat.name, {"turns": seat.turns, "exception": seat.exception})
    if seat.turns is not None and seat.max_turns and seat.turns >= seat.max_turns:
        return SeatFault("turn-cap", seat.name, {"turns": seat.turns, "max_turns": seat.max_turns})
    return _early(seat, trial)


def trial_faults(trial: Trial, stall_seconds: float = STALL_SECONDS) -> list[SeatFault]:
    faults = [f for seat in trial.seat_runs if (f := classify(seat, trial, stall_seconds)) is not None]
    assert len(faults) <= len(trial.seat_runs)
    assert all(f.kind in KINDS for f in faults)
    return faults


def unmeasured(trial: Trial, stall_seconds: float = STALL_SECONDS) -> bool:
    if not trial.valid or trial.not_started is not None:
        return True
    return any(f.kind in UNMEASURED for f in trial_faults(trial, stall_seconds))
