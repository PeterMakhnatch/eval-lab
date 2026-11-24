from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

MAX_SPANS = 1_000_000
MAX_WINDOWS = 10_000
LANE_GAP_SECONDS = 1800.0
RERUN = re.compile(r"^(?P<arm>.+)\.r(?P<round>\d{1,4})(?:-.*)?$")


@dataclass(frozen=True, slots=True)
class Span:
    label: str
    start: datetime
    end: datetime
    own: bool = True


@dataclass(frozen=True, slots=True)
class Peak:
    count: int = 0
    at: datetime | None = None
    running: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"count": self.count, "at": stamp(self.at), "running": self.running}


@dataclass(frozen=True, slots=True)
class Window:
    name: str
    start: datetime
    end: datetime


def stamp(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def rerun_of(name: str) -> tuple[str, int] | None:
    found = RERUN.match(name)
    return (found["arm"], int(found["round"])) if found else None


def running_at(spans: Sequence[Span], counted: Callable[[Span], bool], moment: datetime) -> dict[str, int]:
    hits = Counter(s.label for s in spans if counted(s) and s.start <= moment < s.end)
    return dict(sorted(hits.items(), key=lambda item: (-item[1], item[0])))


def peak(spans: Sequence[Span], counted: Callable[[Span], bool], gate: Callable[[Span], bool]) -> Peak:
    assert len(spans) <= MAX_SPANS
    assert all(s.end > s.start for s in spans)
    events = sorted(
        [(s.start, 1, i) for i, s in enumerate(spans)] + [(s.end, 0, i) for i, s in enumerate(spans)]
    )
    inside = gated = best = 0
    at: datetime | None = None
    for moment, rising, index in events:
        step = 1 if rising else -1
        span = spans[index]
        inside += step if counted(span) else 0
        gated += step if gate(span) else 0
        assert inside >= 0 and gated >= 0
        if rising and gated > 0 and inside > best:
            best, at = inside, moment
    assert inside == 0 and gated == 0
    if at is None:
        return Peak()
    return Peak(best, at, running_at(spans, counted, at))


def lanes(windows: Sequence[Window], gap_seconds: float = LANE_GAP_SECONDS) -> list[list[Window]]:
    assert len(windows) <= MAX_WINDOWS and gap_seconds >= 0
    out: list[list[Window]] = []
    for window in sorted(windows, key=lambda w: (w.start, w.end, w.name)):
        assert window.end >= window.start
        free = [
            lane
            for lane in out
            if lane[-1].end <= window.start and (window.start - lane[-1].end).total_seconds() <= gap_seconds
        ]
        if free:
            max(free, key=lambda lane: lane[-1].end).append(window)
        else:
            out.append([window])
    assert sum(len(lane) for lane in out) == len(windows)
    return out
