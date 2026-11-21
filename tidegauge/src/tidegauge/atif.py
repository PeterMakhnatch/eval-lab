"""Read Harbor's ATIF trajectory file: ``<trial dir>/agent/trajectory.json``.

ATIF (Agent Trajectory Interchange Format, ATIF-v1.0 to ATIF-v1.8) lists the
steps of one agent run. This reader keeps the usage numbers of each model call
and the totals that the agent's adapter wrote. Every field is optional. The
numbers are kept as reported: adapters count tokens in different ways. The
reader never raises on bad data: a file that it cannot use gets a ``problem``.

Only this 1 file is read. A continuation file and subagent trajectories are
not followed; ``continued`` and ``subagents`` record that they exist.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LFS_HEAD = "version https://git-lfs"  # first words of a Git LFS pointer: the file itself is not there


@dataclass(frozen=True, slots=True)
class Call:
    """One model call: an agent step that reports its prompt tokens."""

    step_id: int | None
    timestamp: datetime | None
    prompt_tokens: int  # every input token of the call, cached or not
    completion_tokens: int | None
    cached_tokens: int | None  # the part of ``prompt_tokens`` that the provider read from its cache
    cost_usd: float | None
    llm_calls: int | None = None  # ``llm_call_count``: above 1, the step adds up several model calls


@dataclass(frozen=True, slots=True)
class Trajectory:
    """What 1 trajectory file gives: its model calls and the totals that the adapter wrote."""

    path: Path
    problem: str | None = None  # why the file cannot be used; the other fields are then empty
    schema_version: str | None = None  # kept as read
    agent: str | None = None
    agent_version: str | None = None
    model: str | None = None
    n_steps: int = 0  # every step: system, user and agent
    calls: tuple[Call, ...] = ()
    total_prompt_tokens: int | None = None  # the 4 totals are the adapter's ``final_metrics``
    total_completion_tokens: int | None = None
    total_cached_tokens: int | None = None
    total_cost_usd: float | None = None
    continued: bool = False  # ``continued_trajectory_ref`` names a file with the rest of the run
    subagents: int = 0  # trajectories in ``subagent_trajectories``; their calls are not read


def _amount(value: Any) -> float | None:
    """A finite number that is not negative, or ``None``. Booleans are not numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _count(value: Any) -> int | None:
    amount = _amount(value)
    return None if amount is None else int(amount)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _time(value: Any) -> datetime | None:
    """An ISO 8601 time. A time without a zone is read as UTC, so every gap between 2 times exists."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _call(step: Mapping[str, Any]) -> Call | None:
    """The model call of a step, or ``None`` when the step is not a call of this run.

    No prompt is empty: 0 prompt tokens means that the adapter did not report the usage.
    A step marked ``is_copied_context`` repeats a call of an earlier session. Adapters
    leave it out of ``final_metrics``, and so does this reader.
    """
    metrics = _mapping(step.get("metrics"))
    prompt = _count(metrics.get("prompt_tokens"))
    if step.get("source") != "agent" or not prompt or step.get("is_copied_context") is True:
        return None
    return Call(
        step_id=_count(step.get("step_id")),
        timestamp=_time(step.get("timestamp")),
        prompt_tokens=prompt,
        completion_tokens=_count(metrics.get("completion_tokens")),
        cached_tokens=_count(metrics.get("cached_tokens")),
        cost_usd=_amount(metrics.get("cost_usd")),
        llm_calls=_count(step.get("llm_call_count")),
    )


def parse_trajectory(path: Path, data: Mapping[str, Any]) -> Trajectory:
    """Build a ``Trajectory`` from a parsed ATIF file."""
    steps = data.get("steps")
    steps = [step for step in steps if isinstance(step, Mapping)] if isinstance(steps, list) else []
    agent, totals = _mapping(data.get("agent")), _mapping(data.get("final_metrics"))
    subagents = data.get("subagent_trajectories")
    return Trajectory(
        path=path,
        schema_version=_text(data.get("schema_version")),
        agent=_text(agent.get("name")),
        agent_version=_text(agent.get("version")),
        model=_text(agent.get("model_name")),
        n_steps=len(steps),
        calls=tuple(call for step in steps if (call := _call(step)) is not None),
        total_prompt_tokens=_count(totals.get("total_prompt_tokens")),
        total_completion_tokens=_count(totals.get("total_completion_tokens")),
        total_cached_tokens=_count(totals.get("total_cached_tokens")),
        total_cost_usd=_amount(totals.get("total_cost_usd")),
        continued=bool(data.get("continued_trajectory_ref")),
        subagents=len(subagents) if isinstance(subagents, list) else 0,
    )


def read_trajectory(trial_dir: Path) -> Trajectory | None:
    """The trajectory of one trial, or ``None`` when the trial has no trajectory file."""
    path = trial_dir / "agent" / "trajectory.json"
    try:
        with open(path, encoding="utf-8") as handle:
            if handle.read(len(LFS_HEAD)) == LFS_HEAD:
                return Trajectory(path, problem="Git LFS pointer")
            handle.seek(0)
            data = json.load(handle)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, ValueError, RecursionError) as error:
        return Trajectory(path, problem=f"unreadable: {type(error).__name__}")
    if not isinstance(data, Mapping) or not isinstance(data.get("schema_version"), str):
        return Trajectory(path, problem="not ATIF: no schema_version")
    try:
        return parse_trajectory(path, data)
    except (TypeError, ValueError, AttributeError, OverflowError) as error:
        return Trajectory(path, problem=f"unexpected shape: {error}")
