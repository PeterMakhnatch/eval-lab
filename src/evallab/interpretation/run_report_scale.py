"""Scale helpers for run reports: windows, revisit onset, context growth.

Long-horizon trials (2k–10k steps) must cost roughly the same per step as short
ones, so everything here is a single pass over views the report already built:
no second parse of the trajectory document and no repeated full-chain scans.
The step-window math (``window_of``) matches the historical tenth-of-the-run
bucketing so window indices stay comparable across report versions.

Loop suspicion is a verbatim port of ``evallab.traj._analyze_loop_suspicion``
operating on :class:`LoopStep` (the four fields that algorithm reads) so the
report computes the same score without building a second trajectory outline.
A parity test pins the two implementations together.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any, Protocol

from evallab.traj import LoopSuspicion

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


@dataclass(frozen=True, slots=True)
class LoopStep:
    """The four per-step facts the loop-suspicion heuristic reads."""

    tool_name: str | None
    tool_command: str | None
    exit_code: int | None
    is_error: bool


def window_bounds(
    index: int, total_steps: int, window_count: int = WINDOW_COUNT
) -> tuple[int, int]:
    """Inclusive step range of window ``index``; exact inverse of ``window_of``."""
    if total_steps <= 0:
        return (0, 0)
    first = -(-index * total_steps // window_count) + 1
    last = (
        total_steps
        if index >= window_count - 1
        else min(total_steps, -(-(index + 1) * total_steps // window_count))
    )
    return (first, max(first, last))


def analyze_loop_suspicion(steps: Sequence[LoopStep]) -> LoopSuspicion:
    """Loop suspicion over report steps; parity: ``traj._analyze_loop_suspicion``."""
    repeated_commands = 0
    repeated_errors = 0
    cyclic_patterns = 0
    reasons: list[str] = []

    # 1. Consecutive identical tool commands
    consecutive_cmd_count = 1
    last_cmd: str | None = None
    for step in steps:
        cmd = step.tool_command
        if cmd and len(cmd) > 5:
            if cmd == last_cmd:
                consecutive_cmd_count += 1
                if consecutive_cmd_count == 3:
                    repeated_commands += 1
                    reasons.append(f"repeated_consecutive_command: {cmd[:40]!r} (3+ times)")
            else:
                consecutive_cmd_count = 1
                last_cmd = cmd
        else:
            consecutive_cmd_count = 1
            last_cmd = None

    # 2. Repeated failing commands with identical error/exit code
    failed_cmds: Counter[str] = Counter()
    for step in steps:
        if step.is_error and step.tool_command:
            norm = f"{step.tool_name}:{step.tool_command[:60]}:{step.exit_code}"
            failed_cmds[norm] += 1
    for failed_cmd, count in failed_cmds.items():
        if count >= 3:
            repeated_errors += 1
            reasons.append(f"repeated_failing_command: {failed_cmd} ({count} failures)")

    # 3. Alternating tool cycles (e.g. A -> B -> A -> B -> A -> B)
    tool_sequence = [s.tool_name for s in steps if s.tool_name]
    if len(tool_sequence) >= 6:
        for period in (2, 3):
            matches = 0
            for i in range(len(tool_sequence) - period * 2 + 1):
                chunk1 = tool_sequence[i : i + period]
                chunk2 = tool_sequence[i + period : i + period * 2]
                if chunk1 == chunk2 and len(set(chunk1)) > 1:
                    matches += 1
            if matches >= 2:
                cyclic_patterns += 1
                reasons.append(f"cyclic_tool_pattern: period={period} repeated {matches} times")
                break

    # Calculate bounded score [0.0, 1.0]
    score = 0.0
    if repeated_commands > 0:
        score += 0.35 + min(0.35, repeated_commands * 0.15)
    if repeated_errors > 0:
        score += 0.30 + min(0.30, repeated_errors * 0.15)
    if cyclic_patterns > 0:
        score += 0.40

    score = min(1.0, round(score, 4))
    detected = score >= 0.50

    return LoopSuspicion(
        score=score,
        detected=detected,
        reasons=tuple(reasons),
        repeated_command_count=repeated_commands,
        repeated_error_count=repeated_errors,
        cyclic_patterns_count=cyclic_patterns,
    )


def window_of(step: int, total_steps: int, window_count: int = WINDOW_COUNT) -> int:
    """Index of the tenth-of-the-run window holding ``step`` (1-based ordinals)."""
    return min(window_count - 1, (step - 1) * window_count // max(total_steps, 1))


def infer_compactions(
    steps: Sequence[WindowStep], *, min_prompt: int, drop_ratio: float
) -> list[dict[str, Any]]:
    """Prompt-token drops that look like context compaction, in step order.

    Same heuristic as the report's ``inferred_context_drop`` events: an agent
    step whose input tokens fall below ``drop_ratio`` of the previous agent
    step's once the context had grown past ``min_prompt``.
    """
    events: list[dict[str, Any]] = []
    previous: WindowStep | None = None
    for step in steps:
        if (
            previous is not None
            and previous.prompt_tokens is not None
            and step.prompt_tokens is not None
            and previous.prompt_tokens >= min_prompt
            and step.prompt_tokens < previous.prompt_tokens * drop_ratio
        ):
            events.append(
                {
                    "step": step.step,
                    "from_tokens": previous.prompt_tokens,
                    "to_tokens": step.prompt_tokens,
                }
            )
        if step.source == "agent":
            previous = step
    return events


def _window_row(members: Sequence[WindowStep], revisit_steps: set[int], compaction_steps: set[int]) -> dict[str, Any]:
    member_actions = [a for s in members for a in s.actions]
    stamps = [s.timestamp for s in members if s.timestamp is not None]
    return {
        "steps": [members[0].step, members[-1].step],
        "tool_calls": len(member_actions),
        "errors": sum(1 for a in member_actions if a.status == "error"),
        "revisits": sum(1 for s in members if s.step in revisit_steps),
        "output_tokens": _sum(s.completion_tokens for s in members),
        "cost_usd": _sum(s.cost_usd for s in members),
        "seconds": _seconds(min(stamps), max(stamps)) if len(stamps) > 1 else None,
        "peak_prompt_tokens": _max(s.prompt_tokens for s in members),
        "compactions": sum(1 for s in members if s.step in compaction_steps),
    }


def _sum(values: Any) -> int | float | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _max(values: Any) -> int | None:
    return max((v for v in values if v is not None), default=None)


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds(), 3)


def step_windows(
    steps: Sequence[WindowStep],
    revisit_steps: set[int],
    compaction_steps: set[int],
    *,
    window_count: int = WINDOW_COUNT,
    min_steps: int = MIN_STEPS_FOR_WINDOWS,
) -> list[dict[str, Any]]:
    """Per-tenth-of-the-run rows in one bucketing pass (no window-by-window scans)."""
    if len(steps) < min_steps:
        return []
    buckets: list[list[WindowStep]] = [[] for _ in range(window_count)]
    for step in steps:
        buckets[window_of(step.step, len(steps), window_count)].append(step)
    return [_window_row(members, revisit_steps, compaction_steps) for members in buckets if members]


def time_windows(
    steps: Sequence[WindowStep],
    revisit_steps: set[int],
    compaction_steps: set[int],
    *,
    window_count: int = WINDOW_COUNT,
    min_steps: int = MIN_STEPS_FOR_WINDOWS,
) -> dict[str, Any]:
    """Equal-duration wall-clock windows alongside the step-count windows.

    Steps without timestamps cannot be placed and are reported as such; when
    timestamps are missing or degenerate the section says why instead of
    inventing windows.
    """
    if len(steps) < min_steps:
        return {
            "status": "unavailable",
            "reason": f"fewer than {min_steps} steps",
            "windows": [],
        }
    stamped = [(s, s.timestamp) for s in steps if s.timestamp is not None]
    if not stamped:
        return {
            "status": "unavailable",
            "reason": "steps carry no timestamps",
            "windows": [],
        }
    start = min(ts for _, ts in stamped)
    end = max(ts for _, ts in stamped)
    span = (end - start).total_seconds()
    if span <= 0:
        return {
            "status": "unavailable",
            "reason": "all timestamps are identical",
            "windows": [],
        }
    buckets: list[list[WindowStep]] = [[] for _ in range(window_count)]
    for step, stamp in stamped:
        index = min(window_count - 1, int((stamp - start).total_seconds() * window_count / span))
        buckets[index].append(step)
    rows: list[dict[str, Any]] = []
    for index, members in enumerate(buckets):
        if not members:
            continue
        row = _window_row(members, revisit_steps, compaction_steps)
        row.pop("seconds")
        row["starts_at_offset_seconds"] = round(span * index / window_count, 3)
        row["ends_at_offset_seconds"] = round(span * (index + 1) / window_count, 3)
        rows.append(row)
    return {
        "status": "available",
        "reason": None,
        "origin": start.isoformat(),
        "span_seconds": round(span, 3),
        "steps_without_timestamps": len(steps) - len(stamped),
        "windows": rows,
    }


def repeat_onset(
    counted_per_window: Sequence[int], repeats_per_window: Sequence[int]
) -> dict[str, Any]:
    """When did revisits start: first window above the run-median repeat rate.

    The repeat rate of a window is repeated actions divided by counted
    (non-poll) actions in that window, rounded to four decimals. The run median
    is the median over windows that carry at least one counted action; windows
    without actions have no rate and can never be the onset. The onset is the
    first window whose rate is strictly greater than that median. Runs without
    any repeated action report ``no_repeats``; runs where no window exceeds the
    median (all rates equal) report ``flat``.
    """
    rates = [
        round(repeats / counted, 4) if counted else None
        for counted, repeats in zip(counted_per_window, repeats_per_window, strict=True)
    ]
    defined = [rate for rate in rates if rate is not None]
    block: dict[str, Any] = {
        "repeat_rate_by_window": rates,
        "median_repeat_rate": None,
        "status": "unavailable",
        "reason": "no window carries actions",
        "window": None,
        "steps": None,
        "repeat_rate": None,
    }
    if not defined:
        return block
    middle = round(median(defined), 4)
    block["median_repeat_rate"] = middle
    if sum(repeats_per_window) == 0:
        block.update(status="no_repeats", reason="no repeated actions in the run")
        return block
    for index, rate in enumerate(rates):
        if rate is not None and rate > middle:
            block.update(
                status="onset",
                reason=None,
                window=index,
                repeat_rate=rate,
            )
            return block
    block.update(status="flat", reason="no window exceeds the run-median repeat rate")
    return block
