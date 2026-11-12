"""Scale helpers for run reports: windows, revisit onset, context growth.

Long-horizon trials (2k–10k steps) must cost roughly the same per step as short
ones, so everything here is a single pass over views the report already built:
no second parse of the trajectory document and no repeated full-chain scans.
The step-window math (``window_of``) matches the historical tenth-of-the-run
bucketing so window indices stay comparable across report versions.

Loop suspicion is ``evallab.traj._analyze_loop_suspicion`` itself, called on
per-step facts from ``evallab.traj.extract_loop_step``: one implementation and
one fact source shared with the trajectory outline.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

WINDOW_COUNT = 10
MIN_STEPS_FOR_WINDOWS = 20


class WindowAction(Protocol):
    """Action view used by window aggregation (status only)."""

    @property
    def status(self) -> str: ...


class WindowStep(Protocol):
    """Step view used by window aggregation."""

    @property
    def step(self) -> int: ...

    @property
    def timestamp(self) -> datetime | None: ...

    @property
    def source(self) -> str: ...

    @property
    def prompt_tokens(self) -> int | None: ...

    @property
    def completion_tokens(self) -> int | None: ...

    @property
    def cost_usd(self) -> float | None: ...

    @property
    def actions(self) -> Sequence[WindowAction]: ...


