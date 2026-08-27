"""Canonical event/action projections over Harbor trial evidence.

The mart is deterministic and mechanical: it projects normalized ATIF steps,
tool calls, observations, phase outlines, and filesystem journal changes without
claiming that an observed action caused a filesystem effect.  ``action_effects``
links only by temporal precedence and records the linkage method explicitly.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq

from evallab.evidence.atif import ExportedTable, ExportResult, project_trial
from evallab.evidence.facts import StateChangeFact, extract_job_facts, sha256_file
from evallab.results import JobRecord, TrialRecord
from evallab.traj import outline_trajectory

EVENT_MART_TABLES = (
    "trajectory_events",
    "agent_actions",
    "llm_calls",
    "trajectory_phases",
    "action_effects",
)


def _stable_id(*parts: object) -> str:
    return hashlib.sha256("\0".join(str(part) for part in parts).encode()).hexdigest()


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _action_family(name: str) -> str:
    lowered = name.lower()
    tokens = set(re.findall(r"[a-z0-9]+", lowered))
    groups = (
        ("inspect", {"view", "read", "list", "glob"}),
        ("search", {"search", "grep", "find"}),
        ("edit", {"edit", "patch", "write", "replace", "insert"}),
        ("test", {"test", "pytest", "verify", "check"}),
        ("execute", {"bash", "shell", "command", "exec", "run"}),
        ("browser", {"browser", "playwright", "navigate", "click"}),
        ("version_control", {"git", "commit", "checkout"}),
        ("network", {"fetch", "http", "request", "download"}),
    )
    for family, needles in groups:
        if tokens & needles:
            return family
    if lowered == "task" or tokens & {"agent", "subagent", "delegate"}:
        return "subagent"
    return "other"


@dataclass(frozen=True)
class EventMartProjection:
    trajectory_events: tuple[dict[str, Any], ...]
    agent_actions: tuple[dict[str, Any], ...]
    llm_calls: tuple[dict[str, Any], ...]
    trajectory_phases: tuple[dict[str, Any], ...]
    action_effects: tuple[dict[str, Any], ...]


EVENT_MART_SCHEMAS: dict[str, pa.Schema] = {
    "trajectory_events": pa.schema([
        pa.field("job_id", pa.string(), nullable=False),
        pa.field("trial_id", pa.string(), nullable=False),
        pa.field("document_id", pa.string(), nullable=False),
        pa.field("event_id", pa.string(), nullable=False),
        pa.field("parent_event_id", pa.string()),
        pa.field("sequence", pa.int64(), nullable=False),
        pa.field("step_id", pa.int64()),
        pa.field("event_type", pa.string(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("timestamp", pa.string()),
        pa.field("model_name", pa.string()),
        pa.field("tool_call_id", pa.string()),
        pa.field("content_sha256", pa.string()),
        pa.field("content_size_bytes", pa.int64()),
        pa.field("outcome", pa.string()),
        pa.field("exit_code", pa.int64()),
        pa.field("source_path", pa.string(), nullable=False),
    ]),
    "agent_actions": pa.schema([
        pa.field("job_id", pa.string(), nullable=False),
        pa.field("trial_id", pa.string(), nullable=False),
        pa.field("document_id", pa.string(), nullable=False),
        pa.field("action_id", pa.string(), nullable=False),
        pa.field("step_id", pa.int64(), nullable=False),
        pa.field("tool_call_id", pa.string(), nullable=False),
        pa.field("timestamp", pa.string()),
        pa.field("function_name", pa.string(), nullable=False),
        pa.field("action_family", pa.string(), nullable=False),
        pa.field("arguments_sha256", pa.string(), nullable=False),
        pa.field("observation_sha256", pa.string()),
        pa.field("observation_size_bytes", pa.int64()),
        pa.field("exit_code", pa.int64()),
        pa.field("outcome", pa.string(), nullable=False),
        pa.field("effect_count", pa.int64(), nullable=False),
        pa.field("source_path", pa.string(), nullable=False),
    ]),
    "llm_calls": pa.schema([
        pa.field("job_id", pa.string(), nullable=False),
        pa.field("trial_id", pa.string(), nullable=False),
        pa.field("document_id", pa.string(), nullable=False),
        pa.field("call_id", pa.string(), nullable=False),
        pa.field("step_id", pa.int64(), nullable=False),
        pa.field("timestamp", pa.string()),
        pa.field("model_name", pa.string()),
        pa.field("call_count", pa.int64(), nullable=False),
        pa.field("prompt_tokens", pa.int64()),
        pa.field("completion_tokens", pa.int64()),
        pa.field("cached_tokens", pa.int64()),
        pa.field("cost_usd", pa.float64()),
        pa.field("projection_status", pa.string(), nullable=False),
        pa.field("source_path", pa.string(), nullable=False),
    ]),
    "trajectory_phases": pa.schema([
        pa.field("job_id", pa.string(), nullable=False),
        pa.field("trial_id", pa.string(), nullable=False),
        pa.field("phase_id", pa.int64(), nullable=False),
        pa.field("phase_type", pa.string(), nullable=False),
        pa.field("name", pa.string(), nullable=False),
        pa.field("step_start", pa.int64(), nullable=False),
        pa.field("step_end", pa.int64(), nullable=False),
        pa.field("step_count", pa.int64(), nullable=False),
        pa.field("tool_calls", pa.int64(), nullable=False),
        pa.field("errors", pa.int64(), nullable=False),
        pa.field("prompt_tokens", pa.int64(), nullable=False),
        pa.field("completion_tokens", pa.int64(), nullable=False),
        pa.field("cached_tokens", pa.int64(), nullable=False),
        pa.field("cost_usd", pa.float64(), nullable=False),
        pa.field("algorithm_version", pa.string(), nullable=False),
        pa.field("source_path", pa.string(), nullable=False),
    ]),
    "action_effects": pa.schema([
        pa.field("job_id", pa.string(), nullable=False),
        pa.field("trial_id", pa.string(), nullable=False),
        pa.field("effect_id", pa.string(), nullable=False),
        pa.field("action_id", pa.string()),
        pa.field("path", pa.string(), nullable=False),
        pa.field("change_type", pa.string(), nullable=False),
        pa.field("before_sha256", pa.string()),
        pa.field("after_sha256", pa.string()),
        pa.field("before_size_bytes", pa.int64()),
        pa.field("after_size_bytes", pa.int64()),
        pa.field("first_event_at", pa.string()),
        pa.field("last_event_at", pa.string()),
        pa.field("link_status", pa.string(), nullable=False),
        pa.field("link_method", pa.string(), nullable=False),
    ]),
}


def project_event_mart(
    job: JobRecord,
    trial: TrialRecord,
    *,
    state_changes: tuple[StateChangeFact, ...] = (),
    repo_root: Path | None = None,
) -> EventMartProjection:
    projection = project_trial(job, trial)
    observations = {
        (row.document_id, row.step_id, row.source_call_id): row
        for row in projection.observations
        if row.source_call_id is not None
    }
    tool_calls_by_step: dict[tuple[str, int], list[Any]] = {}
    for tool in projection.tool_calls:
        tool_calls_by_step.setdefault((tool.document_id, tool.step_id), []).append(tool)
    observations_by_step: dict[tuple[str, int], list[Any]] = {}
    for observation in projection.observations:
        observations_by_step.setdefault(
            (observation.document_id, observation.step_id), []
        ).append(observation)

    events: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    sequence = 0
    action_timestamps: list[tuple[datetime, int, str]] = []

    for step in sorted(projection.steps, key=lambda row: (row.document_id, row.step_id)):
        sequence += 1
        step_event_id = _stable_id(job.id, trial.id, step.document_id, "step", step.step_id)
        events.append({
            "job_id": str(job.id), "trial_id": str(trial.id),
            "document_id": step.document_id, "event_id": step_event_id,
            "parent_event_id": None, "sequence": sequence, "step_id": step.step_id,
            "event_type": f"{step.source.lower()}_step", "source": step.source,
            "timestamp": step.timestamp, "model_name": step.model_name,
            "tool_call_id": None, "content_sha256": None, "content_size_bytes": None,
            "outcome": None, "exit_code": None, "source_path": step.source_path,
        })
        if step.llm_call_count > 0:
            calls.append({
                "job_id": str(job.id), "trial_id": str(trial.id),
                "document_id": step.document_id,
                "call_id": _stable_id(job.id, trial.id, step.document_id, "llm", step.step_id),
                "step_id": step.step_id, "timestamp": step.timestamp,
                "model_name": step.model_name, "call_count": step.llm_call_count,
                "prompt_tokens": step.prompt_tokens,
                "completion_tokens": step.completion_tokens,
                "cached_tokens": step.cached_tokens, "cost_usd": step.cost_usd,
                "projection_status": (
                    "one_call_step" if step.llm_call_count == 1 else "aggregated_step"
                ),
                "source_path": step.source_path,
            })

        for tool in tool_calls_by_step.get((step.document_id, step.step_id), ()):
            observation = observations.get(
                (tool.document_id, tool.step_id, tool.tool_call_id)
            )
            exit_code = observation.command_exit_code if observation else None
            outcome: Literal["success", "error", "unknown"] = (
                "unknown" if exit_code is None else "success" if exit_code == 0 else "error"
            )
            action_id = _stable_id(
                job.id, trial.id, tool.document_id, "tool_call",
                tool.step_id, tool.tool_call_id,
            )
            parsed_time = _timestamp(step.timestamp)
            if parsed_time is not None:
                action_timestamps.append((parsed_time, len(actions), action_id))
            sequence += 1
            events.append({
                "job_id": str(job.id), "trial_id": str(trial.id),
                "document_id": tool.document_id, "event_id": action_id,
                "parent_event_id": step_event_id,
                "sequence": sequence, "step_id": tool.step_id, "event_type": "tool_call",
                "source": "agent", "timestamp": step.timestamp,
                "model_name": step.model_name, "tool_call_id": tool.tool_call_id,
                "content_sha256": tool.arguments_sha256, "content_size_bytes": None,
                "outcome": outcome, "exit_code": exit_code, "source_path": tool.source_path,
            })
            actions.append({
                "job_id": str(job.id), "trial_id": str(trial.id),
                "document_id": tool.document_id, "action_id": action_id,
                "step_id": tool.step_id, "tool_call_id": tool.tool_call_id,
                "timestamp": step.timestamp, "function_name": tool.function_name,
                "action_family": _action_family(tool.function_name),
                "arguments_sha256": tool.arguments_sha256,
                "observation_sha256": observation.content_sha256 if observation else None,
                "observation_size_bytes": observation.content_size_bytes if observation else None,
                "exit_code": exit_code, "outcome": outcome, "effect_count": 0,
                "source_path": tool.source_path,
            })

        for observation in observations_by_step.get(
            (step.document_id, step.step_id), ()
        ):
            sequence += 1
            events.append({
                "job_id": str(job.id), "trial_id": str(trial.id),
                "document_id": observation.document_id,
                "event_id": _stable_id(
                    job.id, trial.id, observation.document_id, "observation",
                    observation.step_id, observation.observation_index,
                ),
                "parent_event_id": (
                    _stable_id(
                        job.id, trial.id, observation.document_id, "tool_call",
                        observation.step_id, observation.source_call_id,
                    )
                    if observation.source_call_id
                    else step_event_id
                ),
                "sequence": sequence, "step_id": observation.step_id,
                "event_type": "tool_result", "source": "environment",
                "timestamp": step.timestamp, "model_name": step.model_name,
                "tool_call_id": observation.source_call_id,
                "content_sha256": observation.content_sha256,
                "content_size_bytes": observation.content_size_bytes,
                "outcome": (
                    "unknown" if observation.command_exit_code is None
                    else "success" if observation.command_exit_code == 0 else "error"
                ),
                "exit_code": observation.command_exit_code,
                "source_path": observation.source_path,
            })

    action_timestamps.sort()
    effects: list[dict[str, Any]] = []
    effect_counts: dict[str, int] = {}
    for change in sorted(state_changes, key=lambda row: row.path):
        changed_at = _timestamp(change.first_event_at)
        preceding = [item for item in action_timestamps if changed_at and item[0] <= changed_at]
        action_id = preceding[-1][2] if preceding else None
        if action_id is not None:
            effect_counts[action_id] = effect_counts.get(action_id, 0) + 1
        effects.append({
            "job_id": change.job_id, "trial_id": change.trial_id,
            "effect_id": _stable_id(
                change.job_id, change.trial_id, change.path, change.change_type
            ),
            "action_id": action_id, "path": change.path, "change_type": change.change_type,
            "before_sha256": change.before_sha256, "after_sha256": change.after_sha256,
            "before_size_bytes": change.before_size_bytes,
            "after_size_bytes": change.after_size_bytes,
            "first_event_at": change.first_event_at, "last_event_at": change.last_event_at,
            "link_status": "temporally_preceded" if action_id else "unattributed",
            "link_method": "last_action_before_first_filesystem_event_v1",
        })
    actions = [{**row, "effect_count": effect_counts.get(row["action_id"], 0)} for row in actions]

    root = (repo_root or trial.path.parent.parent).resolve()
    outline = outline_trajectory(
        trial.path, repo_root=root, explicit_runs_root=trial.path.parent.parent
    )
    phase_source_path = (
        Path(outline.source_path).resolve().relative_to(trial.path.resolve()).as_posix()
    )
    phases = tuple({
        "job_id": str(job.id), "trial_id": str(trial.id),
        "phase_id": phase.phase_id, "phase_type": phase.phase_type,
        "name": phase.name, "step_start": phase.step_start, "step_end": phase.step_end,
        "step_count": phase.step_count, "tool_calls": phase.tool_calls,
        "errors": phase.errors, "prompt_tokens": phase.prompt_tokens,
        "completion_tokens": phase.completion_tokens, "cached_tokens": phase.cached_tokens,
        "cost_usd": phase.cost_usd, "algorithm_version": "mechanical-phase-v1",
        "source_path": phase_source_path,
    } for phase in outline.phases)

    return EventMartProjection(
        trajectory_events=tuple(events), agent_actions=tuple(actions),
        llm_calls=tuple(calls), trajectory_phases=phases, action_effects=tuple(effects),
    )


def _write_table(path: Path, table_name: str, rows: list[dict[str, Any]]) -> ExportedTable:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=EVENT_MART_SCHEMAS[table_name])
    temporary = path.with_suffix(".parquet.tmp")
    pq.write_table(
        table, temporary, compression="zstd", use_dictionary=False, write_statistics=True
    )
    temporary.replace(path)
    return ExportedTable(
        table=table_name, path=path, rows=len(rows), sha256=f"sha256:{sha256_file(path)}"
    )


def export_event_mart(jobs: list[JobRecord], output_root: Path) -> ExportResult:
    """Project event tables for all trials into normal job/trial partitions."""
    output_root = output_root.resolve()
    exported: list[ExportedTable] = []
    for job in sorted(jobs, key=lambda item: item.id):
        all_changes = extract_job_facts(job).state_changes
        for trial in sorted(job.trials, key=lambda item: item.id):
            projection = project_event_mart(
                job, trial,
                state_changes=tuple(row for row in all_changes if row.trial_id == str(trial.id)),
                repo_root=job.path.parent.parent,
            )
            partition = output_root / f"job_id={job.id}" / f"trial_id={trial.id}"
            for table_name in EVENT_MART_TABLES:
                rows = [
                    asdict(row) if not isinstance(row, dict) else row
                    for row in getattr(projection, table_name)
                ]
                exported.append(
                    _write_table(partition / f"{table_name}.parquet", table_name, rows)
                )
    return ExportResult(root=output_root, tables=tuple(exported))
