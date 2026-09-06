"""Compile queue-ready ExperimentSpecs from (task, agent, model) triples.

Thin compiler over the queue submit path. It validates the task package
exists, requires a model for billable agents, reads the task's own agent
timeout where declared, and fills honest defaults — then stops. Approval
stays human: nothing here approves, ticks, dispatches, or runs.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evallab.execution_contracts import CONTROL_AGENTS
from evallab.schemas import ExperimentPurpose, ExperimentSpec

DEFAULT_TIMEOUT_SECONDS = 1_800
MAX_TIMEOUT_SECONDS = 21_600
SET_ENTRY_KEYS = frozenset({"task", "agent", "model", "name", "purpose", "attempts"})


class DispatchError(ValueError):
    """Fail-closed refusal from spec compilation."""


def resolve_task(repo_root: Path, task_ref: str) -> Path | None:
    """Return the repo-relative task dir, or None for registry-resolved refs.

    Local refs must name an existing directory containing task.toml.
    ``registered/...`` refs pass through for the gate to resolve.
    """
    if task_ref.startswith("registered/"):
        return None
    if task_ref.startswith("/") or ".." in task_ref.split("/"):
        raise DispatchError(f"task ref must stay relative to the repository: {task_ref!r}")
    task_dir = repo_root / task_ref
    if not task_dir.is_dir() or not (task_dir / "task.toml").is_file():
        raise DispatchError(f"task package not found: {task_ref!r}")
    return task_dir


def default_timeout(task_dir: Path | None) -> int:
    """Read the task's declared agent timeout, else the schema default."""
    if task_dir is not None:
        try:
            config = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise DispatchError(f"task.toml unreadable in {task_dir}: {exc}") from exc
        agent = config.get("agent")
        if isinstance(agent, dict):
            timeout = agent.get("timeout_sec")
            if isinstance(timeout, (int, float)) and 1 <= timeout <= MAX_TIMEOUT_SECONDS:
                return int(timeout)
    return DEFAULT_TIMEOUT_SECONDS


def compile_spec(
    repo_root: Path,
    *,
    task: str,
    agent: str,
    model: str | None,
    name: str,
    submitted_by: str,
    purpose: ExperimentPurpose = "baseline",
    hypothesis: str | None = None,
    attempts: int = 1,
    timeout_seconds: int | None = None,
    est_cost_usd: float = 0.0,
) -> ExperimentSpec:
    """Build a validated ExperimentSpec without submitting it."""
    if agent not in CONTROL_AGENTS and not model:
        raise DispatchError(f"agent {agent!r} may invoke a model: pass --model explicitly")
    task_dir = resolve_task(repo_root, task)
    return ExperimentSpec(
        name=name,
        hypothesis=(hypothesis or f"measure {agent}/{model or 'control'} on {task} ({purpose})"),
        purpose=purpose,
        task=task,
        task_path=task_dir.relative_to(repo_root).as_posix() if task_dir else None,
        agent=agent,
        model=model,
        attempts=attempts,
        timeout_seconds=timeout_seconds or default_timeout(task_dir),
        submitted_by=submitted_by,
        est_cost_usd=est_cost_usd,
    )


def load_dispatch_set(path: Path) -> list[Mapping[str, Any]]:
    """Load a strict list of dispatch entries from JSON or YAML."""
    import json

    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yaml", ".yml"}:
        import yaml

        payload = yaml.safe_load(text)
    else:
        payload = json.loads(text)
    if not isinstance(payload, list) or not payload:
        raise DispatchError("dispatch set must be a non-empty list of entries")
    for entry in payload:
        if not isinstance(entry, dict):
            raise DispatchError("dispatch set entries must be mappings")
        unknown = set(entry) - SET_ENTRY_KEYS
        if unknown:
            raise DispatchError(f"dispatch set entry has unknown keys: {sorted(unknown)}")
        for key in ("task", "agent", "name"):
            if not entry.get(key):
                raise DispatchError(f"dispatch set entry missing required key: {key}")
    return payload


def compile_set(
    repo_root: Path,
    entries: list[Mapping[str, Any]],
    *,
    submitted_by: str,
) -> list[ExperimentSpec]:
    """Compile every entry of a dispatch set. Fails closed on the first bad one."""
    specs = [
        compile_spec(
            repo_root,
            task=str(entry["task"]),
            agent=str(entry["agent"]),
            model=str(entry["model"]) if entry.get("model") else None,
            name=str(entry["name"]),
            submitted_by=submitted_by,
            purpose=entry.get("purpose", "baseline"),  # type: ignore[arg-type]
            attempts=int(entry.get("attempts", 1)),
        )
        for entry in entries
    ]
    names = [spec.name for spec in specs]
    if len(set(names)) != len(names):
        raise DispatchError("dispatch set names must be unique")
    return specs


__all__ = [
    "DispatchError",
    "compile_set",
    "compile_spec",
    "default_timeout",
    "load_dispatch_set",
    "resolve_task",
]
