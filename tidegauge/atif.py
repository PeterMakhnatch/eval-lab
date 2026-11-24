from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LFS_HEAD = "version https://git-lfs"
COMPLETE_TOOLS = frozenset({"mark_task_complete", "task_complete"})


@dataclass(frozen=True, slots=True)
class Call:
    step_id: int | None
    timestamp: datetime | None
    prompt_tokens: int
    completion_tokens: int | None
    cached_tokens: int | None
    cost_usd: float | None
    llm_calls: int | None = None


@dataclass(frozen=True, slots=True)
class Trajectory:
    path: Path
    problem: str | None = None
    schema_version: str | None = None
    agent: str | None = None
    agent_version: str | None = None
    model: str | None = None
    n_steps: int = 0
    calls: tuple[Call, ...] = ()
    total_prompt_tokens: int | None = None
    total_completion_tokens: int | None = None
    total_cached_tokens: int | None = None
    total_cost_usd: float | None = None
    continued: bool = False
    subagents: int = 0
    agent_steps: int = 0
    last_agent_at: datetime | None = None
    last_step_at: datetime | None = None
    marked_complete: bool = False


def _amount(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _count(value: Any) -> int | None:
    amount = _amount(value)
    return None if amount is None else int(amount)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _call(step: Mapping[str, Any]) -> Call | None:
    metrics = _mapping(step.get("metrics"))
    prompt = _count(metrics.get("prompt_tokens"))
    if step.get("source") != "agent" or not prompt or step.get("is_copied_context") is True:
        return None
    assert prompt > 0
    return Call(
        step_id=_count(step.get("step_id")),
        timestamp=_time(step.get("timestamp")),
        prompt_tokens=prompt,
        completion_tokens=_count(metrics.get("completion_tokens")),
        cached_tokens=_count(metrics.get("cached_tokens")),
        cost_usd=_amount(metrics.get("cost_usd")),
        llm_calls=_count(step.get("llm_call_count")),
    )


def _latest(steps: list[Mapping[str, Any]]) -> datetime | None:
    stamps = [stamp for step in steps if (stamp := _time(step.get("timestamp"))) is not None]
    return max(stamps) if stamps else None


def _completes(step: Mapping[str, Any]) -> bool:
    calls = step.get("tool_calls")
    if not isinstance(calls, list):
        return False
    names = [c.get("function_name") for c in calls if isinstance(c, Mapping)]
    return any(isinstance(name, str) and name in COMPLETE_TOOLS for name in names)


def parse_trajectory(path: Path, data: Mapping[str, Any]) -> Trajectory:
    steps = data.get("steps")
    steps = [step for step in steps if isinstance(step, Mapping)] if isinstance(steps, list) else []
    agent, totals = _mapping(data.get("agent")), _mapping(data.get("final_metrics"))
    subagents = data.get("subagent_trajectories")
    acted = [step for step in steps if step.get("source") == "agent" and step.get("is_copied_context") is not True]
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
        agent_steps=len(acted),
        last_agent_at=_latest(acted),
        last_step_at=_latest(steps),
        marked_complete=bool(acted) and _completes(acted[-1]),
    )


def read_trajectory(trial_dir: Path) -> Trajectory | None:
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
