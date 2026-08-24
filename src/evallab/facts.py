from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args
from uuid import UUID, uuid4

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from evallab.atif import ExportedTable, ExportResult, export_trajectories, project_trial
from evallab.results import JobRecord, TrialRecord, duration_seconds, load_job, sha256_file
from evallab.runner import subscription_environment
from evallab.schemas import (
    ANALYSIS_REVIEWS_DIRNAME,
    ANALYSIS_SIDECAR_FILENAME,
    AnalysisProvenance,
    AnalysisReview,
    AnalysisSourceDigests,
    FailureCategory,
    TrialAnalysisOutput,
    TrialAnalysisSidecar,
)
from evallab.state_events import (
    StateEventFact,
    StateEventValidationError,
    invalid_state_event_fact,
    load_state_event_facts,
)

JsonObject = dict[str, Any]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def digest_json(value: Any) -> str:
    return _digest_bytes(_canonical_bytes(value))


def _string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _phase_duration(result: JsonObject, phase: str) -> float | None:
    timing = result.get(phase)
    if not isinstance(timing, dict):
        return None
    return duration_seconds(_string(timing.get("started_at")), _string(timing.get("finished_at")))


def _exception_phase(exception_class: str | None) -> str | None:
    if exception_class is None:
        return None
    lowered = exception_class.lower()
    if lowered.startswith("agent") or "model" in lowered or "api" in lowered:
        return "agent"
    if lowered.startswith("verifier") or lowered.startswith("reward"):
        return "verifier"
    if "environment" in lowered or "docker" in lowered or "sandbox" in lowered:
        return "environment"
    return "unknown"


def experiment_id(job: JobRecord) -> str | None:
    experiment = job.metadata.get("experiment")
    if not isinstance(experiment, dict):
        return None
    value = experiment.get("spec_id")
    return str(value) if value else None

def _experiment_provenance(job: JobRecord) -> JsonObject:
    value = job.metadata.get("experiment")
    return value if isinstance(value, dict) else {}


def _coordinate_json(value: Any) -> str | None:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if isinstance(value, dict)
        else None
    )


def _task_identity(
    job: JobRecord,
    trial: TrialRecord,
    *,
    task_digest: str | None,
    verifier_digest: str,
    environment_digest: str,
) -> tuple[
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
]:
    provenance = _experiment_provenance(job)
    task_lock = trial.lock.get("task")
    observed_task = task_lock if isinstance(task_lock, dict) else {}
    task_id = _string(provenance.get("task_id") or observed_task.get("name"))
    task_family = _string(provenance.get("task_family") or observed_task.get("family"))
    instance_id = _string(
        provenance.get("task_instance_id") or observed_task.get("instance_id")
    )
    generator_seed = (
        provenance["generator_seed"]
        if provenance.get("generator_seed") is not None
        else observed_task.get("generator_seed")
    )
    generator_seed_json = (
        json.dumps(
            generator_seed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        if generator_seed is not None
        else None
    )
    package_identity = task_digest or _string(provenance.get("package_digest"))
    if package_identity is None:
        return (
            task_family,
            task_id,
            instance_id,
            generator_seed_json,
            None,
            None,
        )
    inputs = {
        "task_package_digest": package_identity,
        "instance_id": instance_id,
        "generator_seed": generator_seed,
        "verifier_base_digest": verifier_digest,
        "environment_base_digest": environment_digest,
    }
    inputs_json = json.dumps(
        inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return (
        task_family,
        task_id,
        instance_id,
        generator_seed_json,
        inputs_json,
        digest_json(inputs),
    )


def _task_digest(trial: TrialRecord) -> str | None:
    task = trial.lock.get("task")
    if isinstance(task, dict) and task.get("digest"):
        return str(task["digest"])
    return _string(trial.result.get("task_checksum"))


def _verifier_digest(job: JobRecord, trial: TrialRecord) -> str:
    experiment = job.metadata.get("experiment")
    if isinstance(experiment, dict) and experiment.get("verifier_digest"):
        return str(experiment["verifier_digest"])
    return digest_json(
        {
            "task_digest": _task_digest(trial),
            "verifier": trial.lock.get("verifier") or {},
        }
    )


def _agent_result(result: JsonObject) -> JsonObject:
    value = result.get("agent_result")
    return value if isinstance(value, dict) else {}


def _exception_class(result: JsonObject) -> str | None:
    exception = result.get("exception_info")
    if not isinstance(exception, dict):
        return None
    return _string(exception.get("exception_type"))


@dataclass(frozen=True)
class TrialFact:
    experiment_id: str | None
    job_id: str
    trial_id: str
    job_name: str
    trial_name: str
    task_name: str | None
    task_digest: str | None
    verifier_digest: str
    environment_digest: str
    grid_id: str | None
    point_id: str | None
    arm_id: str | None
    factor_values_json: str | None
    factor_values_digest: str | None
    factor_bindings_json: str | None
    factor_bindings_digest: str | None
    bound_execution_values_json: str | None
    bound_execution_values_digest: str | None
    preamble_path: str | None
    preamble_content_sha256: str | None
    task_family: str | None
    task_id: str | None
    task_instance_id: str | None
    generator_seed_json: str | None
    task_block_inputs_json: str | None
    task_block_id: str | None
    agent_config_digest: str
    agent_name: str | None
    agent_version: str | None
    model_name: str | None
    primary_reward: float | None
    exception_class: str | None
    exception_phase: str | None
    duration_seconds: float | None
    environment_setup_seconds: float | None
    agent_setup_seconds: float | None
    agent_execution_seconds: float | None
    verifier_seconds: float | None
    input_tokens: int | None
    cache_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    trajectory_count: int
    invalid_trajectory_count: int
    step_count: int
    llm_call_count: int
    tool_call_count: int
    command_failure_count: int
    repeated_failed_command_count: int
    artifact_count: int
    missing_artifact_count: int
    artifact_set_digest: str
    state_journal_status: str
    state_journal_reason: str | None
    state_change_count: int


@dataclass(frozen=True)
class RewardFact:
    experiment_id: str | None
    job_id: str
    trial_id: str
    reward_name: str
    reward_value: float


@dataclass(frozen=True)
class ArtifactFact:
    experiment_id: str | None
    job_id: str
    trial_id: str
    source: str
    destination: str | None
    status: str | None
    exists_on_disk: bool
    size_bytes: int | None
    sha256: str | None


@dataclass(frozen=True)
class ToolUseFact:
    experiment_id: str | None
    job_id: str
    trial_id: str
    function_name: str
    call_count: int


@dataclass(frozen=True)
class StateJournalRecord:
    status: str
    reason: str | None
    changes: tuple[JsonObject, ...]


@dataclass(frozen=True)
class StateChangeFact:
    experiment_id: str | None
    job_id: str
    trial_id: str
    path: str
    change_type: str
    before_sha256: str | None
    after_sha256: str | None
    before_size_bytes: int | None
    after_size_bytes: int | None
    event_count: int
    first_event_at: str | None
    last_event_at: str | None
    journal_status: str


@dataclass(frozen=True)
class JobFacts:
    trials: tuple[TrialFact, ...]
    rewards: tuple[RewardFact, ...]
    artifacts: tuple[ArtifactFact, ...]
    tool_usage: tuple[ToolUseFact, ...]
    state_changes: tuple[StateChangeFact, ...]
    state_events: tuple[StateEventFact, ...]


@dataclass(frozen=True)
class RebuildResult:
    trajectory_export: ExportResult
    fact_export: ExportResult
    event_mart_export: ExportResult

    @property
    def tables(self) -> tuple[ExportedTable, ...]:
        return (
            self.trajectory_export.tables
            + self.fact_export.tables
            + self.event_mart_export.tables
        )


def load_state_journal(trial: TrialRecord) -> StateJournalRecord:
    status_path = trial.path / "state-journal" / "status.json"
    diff_path = trial.path / "state-journal" / "state-diff.json"
    if not status_path.is_file():
        return StateJournalRecord("absent", "not_recorded", ())
    try:
        status_payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return StateJournalRecord("invalid", f"status_unreadable:{type(exc).__name__}", ())
    if not isinstance(status_payload, dict):
        return StateJournalRecord("invalid", "status_invalid", ())
    status_value = status_payload.get("status")
    status = status_value if isinstance(status_value, str) and status_value else "invalid"
    reason_value = status_payload.get("reason")
    reason = reason_value if isinstance(reason_value, str) else None
    if not diff_path.is_file():
        return StateJournalRecord(status, reason or "state_diff_missing", ())
    try:
        diff_payload = json.loads(diff_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return StateJournalRecord(
            status, reason or f"diff_unreadable:{type(exc).__name__}", ()
        )
    if not isinstance(diff_payload, dict):
        return StateJournalRecord(status, reason or "state_diff_invalid", ())
    changes = diff_payload.get("changes")
    if not isinstance(changes, list) or not all(isinstance(item, dict) for item in changes):
        return StateJournalRecord(status, reason or "changes_invalid", ())
    return StateJournalRecord(status, reason, tuple(changes))


def _state_change_fact(
    *,
    association: str | None,
    job: JobRecord,
    trial: TrialRecord,
    change: JsonObject,
    journal_status: str,
) -> StateChangeFact | None:
    path = _string(change.get("path"))
    change_type = _string(change.get("change_type"))
    if not path or not change_type:
        return None
    before_value = change.get("before")
    after_value = change.get("after")
    before: JsonObject = before_value if isinstance(before_value, dict) else {}
    after: JsonObject = after_value if isinstance(after_value, dict) else {}
    return StateChangeFact(
        experiment_id=association,
        job_id=job.id,
        trial_id=trial.id,
        path=path,
        change_type=change_type,
        before_sha256=_string(before.get("sha256")),
        after_sha256=_string(after.get("sha256")),
        before_size_bytes=_integer(before.get("size_bytes")),
        after_size_bytes=_integer(after.get("size_bytes")),
        event_count=_integer(change.get("event_count")) or 0,
        first_event_at=_string(change.get("first_event_at")),
        last_event_at=_string(change.get("last_event_at")),
        journal_status=journal_status,
    )


def extract_trial_fact(
    job: JobRecord,
    trial: TrialRecord,
    state_journal: StateJournalRecord | None = None,
) -> TrialFact:
    projection = project_trial(job, trial)
    journal = state_journal or load_state_journal(trial)
    result = trial.result
    agent_info = result.get("agent_info") if isinstance(result.get("agent_info"), dict) else {}
    model_info = (
        agent_info.get("model_info") if isinstance(agent_info.get("model_info"), dict) else {}
    )
    raw_agent_result = _agent_result(result)
    root_trajectories = [item for item in projection.trajectories if item.embedded_path is None]
    root_metrics = root_trajectories[0] if root_trajectories else None
    input_tokens = _integer(raw_agent_result.get("n_input_tokens"))
    cache_tokens = _integer(raw_agent_result.get("n_cache_tokens"))
    output_tokens = _integer(raw_agent_result.get("n_output_tokens"))
    cost_usd = _number(raw_agent_result.get("cost_usd"))
    if root_metrics is not None:
        input_tokens = input_tokens if input_tokens is not None else root_metrics.prompt_tokens
        cache_tokens = cache_tokens if cache_tokens is not None else root_metrics.cached_tokens
        output_tokens = (
            output_tokens if output_tokens is not None else root_metrics.completion_tokens
        )
        cost_usd = cost_usd if cost_usd is not None else root_metrics.cost_usd

    failed_call_ids = {
        (item.document_id, item.step_id, item.source_call_id)
        for item in projection.observations
        if item.command_exit_code not in (None, 0) and item.source_call_id is not None
    }
    failed_argument_digests = [
        item.arguments_sha256
        for item in projection.tool_calls
        if (item.document_id, item.step_id, item.tool_call_id) in failed_call_ids
    ]
    failed_digest_counts = Counter(failed_argument_digests)
    exception_class = _exception_class(result)
    artifact_inventory = [
        {
            "source": artifact.source,
            "destination": artifact.destination,
            "status": artifact.status,
            "exists": artifact.exists,
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
        }
        for artifact in sorted(
            trial.artifacts,
            key=lambda item: (item.source, item.destination or ""),
        )
    ]
    provenance = _experiment_provenance(job)
    factor_values_json = _coordinate_json(provenance.get("factor_values"))
    bound_values_json = _coordinate_json(provenance.get("bound_execution_values"))
    factor_bindings_json = _coordinate_json(provenance.get("factor_bindings"))
    verifier_digest = _verifier_digest(job, trial)
    environment_digest = digest_json(trial.lock.get("environment") or {})
    task_digest = _task_digest(trial)
    (
        task_family,
        task_id,
        task_instance_id,
        generator_seed_json,
        task_block_inputs_json,
        task_block_id,
    ) = _task_identity(
        job,
        trial,
        task_digest=task_digest,
        verifier_digest=verifier_digest,
        environment_digest=environment_digest,
    )
    return TrialFact(
        experiment_id=experiment_id(job),
        job_id=job.id,
        trial_id=trial.id,
        job_name=job.name,
        trial_name=trial.name,
        task_name=_string(result.get("task_name")),
        task_digest=task_digest,
        verifier_digest=verifier_digest,
        environment_digest=environment_digest,
        agent_config_digest=digest_json(trial.lock.get("agent") or {}),
        grid_id=_string(provenance.get("grid_id")),
        point_id=_string(provenance.get("point_id")),
        arm_id=_string(provenance.get("arm_id")),
        factor_values_json=factor_values_json,
        factor_values_digest=(
            digest_json(provenance["factor_values"])
            if factor_values_json is not None
            else None
        ),
        factor_bindings_json=factor_bindings_json,
        factor_bindings_digest=(
            digest_json(provenance["factor_bindings"])
            if factor_bindings_json is not None
            else None
        ),
        bound_execution_values_json=bound_values_json,
        bound_execution_values_digest=(
            digest_json(provenance["bound_execution_values"])
            if bound_values_json is not None
            else None
        ),
        preamble_path=_string(provenance.get("preamble_path")),
        preamble_content_sha256=_string(provenance.get("preamble_sha256")),
        task_family=task_family,
        task_id=task_id,
        task_instance_id=task_instance_id,
        generator_seed_json=generator_seed_json,
        task_block_inputs_json=task_block_inputs_json,
        task_block_id=task_block_id,
        agent_name=_string(agent_info.get("name")),
        agent_version=_string(agent_info.get("version")),
        model_name=_string(model_info.get("name") or model_info.get("model_name")),
        primary_reward=trial.primary_reward,
        exception_class=exception_class,
        exception_phase=_exception_phase(exception_class),
        duration_seconds=duration_seconds(
            _string(result.get("started_at")), _string(result.get("finished_at"))
        ),
        environment_setup_seconds=_phase_duration(result, "environment_setup"),
        agent_setup_seconds=_phase_duration(result, "agent_setup"),
        agent_execution_seconds=_phase_duration(result, "agent_execution"),
        verifier_seconds=_phase_duration(result, "verifier"),
        input_tokens=input_tokens,
        cache_tokens=cache_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        trajectory_count=len(projection.trajectories),
        invalid_trajectory_count=sum(
            item.validation_status != "valid" for item in projection.trajectories
        ),
        step_count=len(projection.steps),
        llm_call_count=sum(item.llm_call_count for item in projection.steps),
        tool_call_count=len(projection.tool_calls),
        command_failure_count=len(failed_argument_digests),
        repeated_failed_command_count=sum(
            count - 1 for count in failed_digest_counts.values() if count > 1
        ),
        artifact_count=len(trial.artifacts),
        missing_artifact_count=sum(not item.exists for item in trial.artifacts),
        artifact_set_digest=digest_json(artifact_inventory),
        state_journal_status=journal.status,
        state_journal_reason=journal.reason,
        state_change_count=len(journal.changes),
    )


def extract_job_facts(job: JobRecord) -> JobFacts:
    trial_facts: list[TrialFact] = []
    reward_facts: list[RewardFact] = []
    artifact_facts: list[ArtifactFact] = []
    tool_usage: list[ToolUseFact] = []
    state_change_facts: list[StateChangeFact] = []
    state_event_facts: list[StateEventFact] = []
    association = experiment_id(job)
    for trial in sorted(job.trials, key=lambda item: item.id):
        projection = project_trial(job, trial)
        state_journal = load_state_journal(trial)
        trial_facts.append(extract_trial_fact(job, trial, state_journal))
        try:
            state_event_facts.extend(
                load_state_event_facts(
                    trial,
                    job_id=str(job.id),
                    experiment_id=association,
                )
            )
        except StateEventValidationError as exc:
            state_event_facts.append(
                invalid_state_event_fact(
                    trial,
                    job_id=str(job.id),
                    experiment_id=association,
                    error=exc,
                )
            )
        reward_facts.extend(
            RewardFact(
                experiment_id=association,
                job_id=job.id,
                trial_id=trial.id,
                reward_name=name,
                reward_value=value,
            )
            for name, value in sorted(trial.rewards.items())
        )
        artifact_facts.extend(
            ArtifactFact(
                experiment_id=association,
                job_id=job.id,
                trial_id=trial.id,
                source=item.source,
                destination=item.destination,
                status=item.status,
                exists_on_disk=item.exists,
                size_bytes=item.size_bytes,
                sha256=f"sha256:{item.sha256}" if item.sha256 else None,
            )
            for item in sorted(
                trial.artifacts,
                key=lambda value: (value.source, value.destination or ""),
            )
        )
        counts = Counter(item.function_name for item in projection.tool_calls)
        tool_usage.extend(
            ToolUseFact(
                experiment_id=association,
                job_id=job.id,
                trial_id=trial.id,
                function_name=name,
                call_count=count,
            )
            for name, count in sorted(counts.items())
        )
        for change in state_journal.changes:
            fact = _state_change_fact(
                association=association,
                job=job,
                trial=trial,
                change=change,
                journal_status=state_journal.status,
            )
            if fact is not None:
                state_change_facts.append(fact)
    return JobFacts(
        trials=tuple(trial_facts),
        rewards=tuple(reward_facts),
        artifacts=tuple(artifact_facts),
        tool_usage=tuple(tool_usage),
        state_changes=tuple(state_change_facts),
        state_events=tuple(state_event_facts),
    )


TRIAL_FACT_SCHEMA = pa.schema(
    [
        pa.field("experiment_id", pa.string()),
        pa.field("job_id", pa.string(), nullable=False),
        pa.field("trial_id", pa.string(), nullable=False),
        pa.field("job_name", pa.string(), nullable=False),
        pa.field("trial_name", pa.string(), nullable=False),
        pa.field("task_name", pa.string()),
        pa.field("task_digest", pa.string()),
        pa.field("verifier_digest", pa.string(), nullable=False),
        pa.field("environment_digest", pa.string(), nullable=False),
        pa.field("grid_id", pa.string()),
        pa.field("point_id", pa.string()),
        pa.field("arm_id", pa.string()),
        pa.field("factor_values_json", pa.string()),
        pa.field("factor_values_digest", pa.string()),
        pa.field("factor_bindings_json", pa.string()),
        pa.field("factor_bindings_digest", pa.string()),
        pa.field("bound_execution_values_json", pa.string()),
        pa.field("bound_execution_values_digest", pa.string()),
        pa.field("preamble_path", pa.string()),
        pa.field("preamble_content_sha256", pa.string()),
        pa.field("task_family", pa.string()),
        pa.field("task_id", pa.string()),
        pa.field("task_instance_id", pa.string()),
        pa.field("generator_seed_json", pa.string()),
        pa.field("task_block_inputs_json", pa.string()),
        pa.field("task_block_id", pa.string()),
        pa.field("agent_config_digest", pa.string(), nullable=False),
        pa.field("agent_name", pa.string()),
        pa.field("agent_version", pa.string()),
        pa.field("model_name", pa.string()),
        pa.field("primary_reward", pa.float64()),
        pa.field("exception_class", pa.string()),
        pa.field("exception_phase", pa.string()),
        pa.field("duration_seconds", pa.float64()),
        pa.field("environment_setup_seconds", pa.float64()),
        pa.field("agent_setup_seconds", pa.float64()),
        pa.field("agent_execution_seconds", pa.float64()),
        pa.field("verifier_seconds", pa.float64()),
        pa.field("input_tokens", pa.int64()),
        pa.field("cache_tokens", pa.int64()),
        pa.field("output_tokens", pa.int64()),
        pa.field("cost_usd", pa.float64()),
        pa.field("trajectory_count", pa.int64(), nullable=False),
        pa.field("invalid_trajectory_count", pa.int64(), nullable=False),
        pa.field("step_count", pa.int64(), nullable=False),
        pa.field("llm_call_count", pa.int64(), nullable=False),
        pa.field("tool_call_count", pa.int64(), nullable=False),
        pa.field("command_failure_count", pa.int64(), nullable=False),
        pa.field("repeated_failed_command_count", pa.int64(), nullable=False),
        pa.field("artifact_count", pa.int64(), nullable=False),
        pa.field("missing_artifact_count", pa.int64(), nullable=False),
        pa.field("artifact_set_digest", pa.string(), nullable=False),
        pa.field("state_journal_status", pa.string(), nullable=False),
        pa.field("state_journal_reason", pa.string()),
        pa.field("state_change_count", pa.int64(), nullable=False),
    ]
)


FACT_SCHEMAS = {
    "trial_facts": TRIAL_FACT_SCHEMA,
    "reward_facts": pa.schema(
        [
            pa.field("experiment_id", pa.string()),
            pa.field("job_id", pa.string(), nullable=False),
            pa.field("trial_id", pa.string(), nullable=False),
            pa.field("reward_name", pa.string(), nullable=False),
            pa.field("reward_value", pa.float64(), nullable=False),
        ]
    ),
    "artifact_facts": pa.schema(
        [
            pa.field("experiment_id", pa.string()),
            pa.field("job_id", pa.string(), nullable=False),
            pa.field("trial_id", pa.string(), nullable=False),
            pa.field("source", pa.string(), nullable=False),
            pa.field("destination", pa.string()),
            pa.field("status", pa.string()),
            pa.field("exists_on_disk", pa.bool_(), nullable=False),
            pa.field("size_bytes", pa.int64()),
            pa.field("sha256", pa.string()),
        ]
    ),
    "tool_usage": pa.schema(
        [
            pa.field("experiment_id", pa.string()),
            pa.field("job_id", pa.string(), nullable=False),
            pa.field("trial_id", pa.string(), nullable=False),
            pa.field("function_name", pa.string(), nullable=False),
            pa.field("call_count", pa.int64(), nullable=False),
        ]
    ),
    "state_changes": pa.schema(
        [
            pa.field("experiment_id", pa.string()),
            pa.field("job_id", pa.string(), nullable=False),
            pa.field("trial_id", pa.string(), nullable=False),
            pa.field("path", pa.string(), nullable=False),
            pa.field("change_type", pa.string(), nullable=False),
            pa.field("before_sha256", pa.string()),
            pa.field("after_sha256", pa.string()),
            pa.field("before_size_bytes", pa.int64()),
            pa.field("after_size_bytes", pa.int64()),
            pa.field("event_count", pa.int64(), nullable=False),
            pa.field("first_event_at", pa.string()),
            pa.field("last_event_at", pa.string()),
            pa.field("journal_status", pa.string(), nullable=False),
        ]
    ),
    "state_events": pa.schema(
        [
            pa.field("experiment_id", pa.string()),
            pa.field("job_id", pa.string(), nullable=False),
            pa.field("trial_id", pa.string(), nullable=False),
            pa.field("sequence", pa.int64(), nullable=False),
            pa.field("precedence", pa.int64(), nullable=False),
            pa.field("predecessor_sequence", pa.int64()),
            pa.field("event_at", pa.string()),
            pa.field("operations", pa.list_(pa.string()), nullable=False),
            pa.field("path", pa.string()),
            pa.field("is_directory", pa.bool_()),
            pa.field("cookie", pa.int64()),
            pa.field("before_state_digest", pa.string()),
            pa.field("after_state_digest", pa.string()),
            pa.field("before_content_sha256", pa.string()),
            pa.field("after_content_sha256", pa.string()),
            pa.field("before_size_bytes", pa.int64()),
            pa.field("after_size_bytes", pa.int64()),
            pa.field("producer", pa.string(), nullable=False),
            pa.field("producer_schema_version", pa.int64()),
            pa.field("fact_schema_version", pa.string(), nullable=False),
            pa.field("source_digest", pa.string(), nullable=False),
            pa.field("source_record_digest", pa.string()),
            pa.field("temporal_semantics", pa.string(), nullable=False),
            pa.field("evidence_status", pa.string(), nullable=False),
            pa.field("invalid_reason", pa.string()),
            pa.field("invalid_error_digest", pa.string()),
        ]
    ),
}


def _write_fact_table(path: Path, table_name: str, rows: list[dict[str, Any]]) -> ExportedTable:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=FACT_SCHEMAS[table_name])
    temporary = path.with_suffix(".parquet.tmp")
    pq.write_table(
        table,
        temporary,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
    )
    temporary.replace(path)
    return ExportedTable(
        table=table_name,
        path=path,
        rows=len(rows),
        sha256=f"sha256:{sha256_file(path)}",
    )


def export_facts(jobs: list[JobRecord], output_root: Path) -> ExportResult:
    output_root = output_root.resolve()
    exported: list[ExportedTable] = []
    for job in sorted(jobs, key=lambda item: item.id):
        facts = extract_job_facts(job)
        trial_by_id = {item.trial_id: item for item in facts.trials}
        for trial_id in sorted(trial_by_id):
            partition = output_root / f"job_id={job.id}" / f"trial_id={trial_id}"
            rows_by_table = {
                "trial_facts": [asdict(trial_by_id[trial_id])],
                "reward_facts": [
                    asdict(item) for item in facts.rewards if item.trial_id == trial_id
                ],
                "artifact_facts": [
                    asdict(item) for item in facts.artifacts if item.trial_id == trial_id
                ],
                "tool_usage": [
                    asdict(item) for item in facts.tool_usage if item.trial_id == trial_id
                ],
                "state_changes": [
                    asdict(item) for item in facts.state_changes if item.trial_id == trial_id
                ],
                "state_events": [
                    asdict(item) for item in facts.state_events if item.trial_id == trial_id
                ],
            }
            for table_name, rows in rows_by_table.items():
                exported.append(
                    _write_fact_table(partition / f"{table_name}.parquet", table_name, rows)
                )
    return ExportResult(root=output_root, tables=tuple(exported))


def rebuild_from_raw(jobs: list[JobRecord], output_root: Path) -> RebuildResult:
    from evallab.event_mart import export_event_mart

    return RebuildResult(
        trajectory_export=export_trajectories(jobs, output_root),
        fact_export=export_facts(jobs, output_root),
        event_mart_export=export_event_mart(jobs, output_root),
    )


def _relative_or_absolute(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def ingest_catalog(
    database_url: str,
    jobs: list[JobRecord],
    *,
    root: Path,
    derived_root: Path | None = None,
) -> None:
    """Upsert deterministic document/fact records after the base job ingest."""
    with psycopg.connect(database_url) as connection:
        for job in jobs:
            association = experiment_id(job)
            if association is not None:
                experiment = job.metadata.get("experiment") or {}
                connection.execute(
                    """
                    INSERT INTO experiments (id, source_kind, raw_provenance)
                    VALUES (%s, 'lab-metadata', %s)
                    ON CONFLICT (id) DO UPDATE SET raw_provenance = EXCLUDED.raw_provenance
                    """,
                    (association, Jsonb(experiment)),
                )
                existing = connection.execute(
                    "SELECT experiment_id FROM jobs WHERE id = %s", (job.id,)
                ).fetchone()
                if existing is not None and existing[0] not in (None, association):
                    raise ValueError(
                        f"job {job.id} is already associated with experiment {existing[0]!r}"
                    )
                connection.execute(
                    "UPDATE jobs SET experiment_id = %s WHERE id = %s",
                    (association, job.id),
                )
            facts = extract_job_facts(job)
            for trial, trial_fact in zip(
                sorted(job.trials, key=lambda item: item.id), facts.trials, strict=True
            ):
                projection = project_trial(job, trial)
                connection.execute(
                    "DELETE FROM trajectory_documents WHERE trial_id = %s", (trial.id,)
                )
                for document in projection.trajectories:
                    parquet_path = None
                    if derived_root is not None:
                        parquet_path = _relative_or_absolute(
                            derived_root
                            / f"job_id={job.id}"
                            / f"trial_id={trial.id}"
                            / "trajectories.parquet",
                            root,
                        )
                    connection.execute(
                        """
                        INSERT INTO trajectory_documents (
                            id, trial_id, source_path, source_sha256, embedded_path,
                            schema_version, session_id, trajectory_id,
                            validation_status, validator, validation_error,
                            step_count, llm_call_count, parquet_path
                        ) VALUES (
                            %(id)s, %(trial_id)s, %(source_path)s, %(source_sha256)s,
                            %(embedded_path)s, %(schema_version)s, %(session_id)s,
                            %(trajectory_id)s, %(validation_status)s, %(validator)s,
                            %(validation_error)s, %(step_count)s, %(llm_call_count)s,
                            %(parquet_path)s
                        )
                        """,
                        {
                            "id": document.document_id,
                            "trial_id": trial.id,
                            "source_path": document.source_path,
                            "source_sha256": document.source_sha256,
                            "embedded_path": document.embedded_path,
                            "schema_version": document.schema_version,
                            "session_id": document.session_id,
                            "trajectory_id": document.trajectory_id,
                            "validation_status": document.validation_status,
                            "validator": document.validator,
                            "validation_error": document.validation_error,
                            "step_count": document.step_count,
                            "llm_call_count": document.llm_call_count,
                            "parquet_path": parquet_path,
                        },
                    )
                connection.execute(
                    """
                    INSERT INTO deterministic_trial_facts (
                        trial_id, verifier_digest, environment_digest,
                        agent_config_digest, grid_id, point_id, arm_id,
                        factor_values_json, factor_values_digest,
                        factor_bindings_json, factor_bindings_digest,
                        bound_execution_values_json, bound_execution_values_digest,
                        preamble_path, preamble_content_sha256, task_family, task_id,
                        task_instance_id, generator_seed_json,
                        task_block_inputs_json, task_block_id, exception_phase,
                        environment_setup_seconds, agent_setup_seconds,
                        agent_execution_seconds, verifier_seconds,
                        trajectory_count, invalid_trajectory_count, step_count,
                        llm_call_count, tool_call_count, command_failure_count,
                        repeated_failed_command_count, artifact_count,
                        missing_artifact_count, artifact_set_digest, raw_facts,
                        updated_at
                    ) VALUES (
                        %(trial_id)s, %(verifier_digest)s, %(environment_digest)s,
                        %(agent_config_digest)s, %(grid_id)s, %(point_id)s, %(arm_id)s,
                        %(factor_values_json)s, %(factor_values_digest)s,
                        %(factor_bindings_json)s, %(factor_bindings_digest)s,
                        %(bound_execution_values_json)s,
                        %(bound_execution_values_digest)s,
                        %(preamble_path)s, %(preamble_content_sha256)s, %(task_family)s,
                        %(task_id)s, %(task_instance_id)s, %(generator_seed_json)s,
                        %(task_block_inputs_json)s, %(task_block_id)s, %(exception_phase)s,
                        %(environment_setup_seconds)s, %(agent_setup_seconds)s,
                        %(agent_execution_seconds)s, %(verifier_seconds)s,
                        %(trajectory_count)s, %(invalid_trajectory_count)s,
                        %(step_count)s, %(llm_call_count)s, %(tool_call_count)s,
                        %(command_failure_count)s, %(repeated_failed_command_count)s,
                        %(artifact_count)s, %(missing_artifact_count)s,
                        %(artifact_set_digest)s, %(raw_facts)s, now()
                    )
                    ON CONFLICT (trial_id) DO UPDATE SET
                        verifier_digest = EXCLUDED.verifier_digest,
                        environment_digest = EXCLUDED.environment_digest,
                        agent_config_digest = EXCLUDED.agent_config_digest,
                        grid_id = EXCLUDED.grid_id,
                        point_id = EXCLUDED.point_id,
                        arm_id = EXCLUDED.arm_id,
                        factor_values_json = EXCLUDED.factor_values_json,
                        factor_values_digest = EXCLUDED.factor_values_digest,
                        factor_bindings_json = EXCLUDED.factor_bindings_json,
                        factor_bindings_digest = EXCLUDED.factor_bindings_digest,
                        bound_execution_values_json = EXCLUDED.bound_execution_values_json,
                        bound_execution_values_digest = EXCLUDED.bound_execution_values_digest,
                        preamble_path = EXCLUDED.preamble_path,
                        preamble_content_sha256 = EXCLUDED.preamble_content_sha256,
                        task_family = EXCLUDED.task_family,
                        task_id = EXCLUDED.task_id,
                        task_instance_id = EXCLUDED.task_instance_id,
                        generator_seed_json = EXCLUDED.generator_seed_json,
                        task_block_inputs_json = EXCLUDED.task_block_inputs_json,
                        task_block_id = EXCLUDED.task_block_id,
                        exception_phase = EXCLUDED.exception_phase,
                        environment_setup_seconds = EXCLUDED.environment_setup_seconds,
                        agent_setup_seconds = EXCLUDED.agent_setup_seconds,
                        agent_execution_seconds = EXCLUDED.agent_execution_seconds,
                        verifier_seconds = EXCLUDED.verifier_seconds,
                        trajectory_count = EXCLUDED.trajectory_count,
                        invalid_trajectory_count = EXCLUDED.invalid_trajectory_count,
                        step_count = EXCLUDED.step_count,
                        llm_call_count = EXCLUDED.llm_call_count,
                        tool_call_count = EXCLUDED.tool_call_count,
                        command_failure_count = EXCLUDED.command_failure_count,
                        repeated_failed_command_count = EXCLUDED.repeated_failed_command_count,
                        artifact_count = EXCLUDED.artifact_count,
                        missing_artifact_count = EXCLUDED.missing_artifact_count,
                        artifact_set_digest = EXCLUDED.artifact_set_digest,
                        raw_facts = EXCLUDED.raw_facts,
                        updated_at = now()
                    """,
                    {**asdict(trial_fact), "raw_facts": Jsonb(asdict(trial_fact))},
                )


@dataclass(frozen=True)
class AnalyzerCallResult:
    raw_output: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


AnalyzerCallable = Callable[[str, dict[str, Any]], AnalyzerCallResult]


@dataclass(frozen=True)
class AnalysisPlan:
    experiment_id: str | None
    job_id: str
    source_trial_id: str
    source_trial_path: str
    agent: str
    agent_version: str
    model: str
    estimated_model_calls: int
    maximum_model_calls: int
    queue_policy_rule: str
    destination_root: str
    prompt_digest: str
    rubric_digest: str
    output_schema_digest: str


def load_analysis_source(path: Path) -> tuple[JobRecord, TrialRecord]:
    result_path = path / "result.json"
    if not result_path.is_file():
        raise ValueError(f"analysis source has no result.json: {path}")
    result = json.loads(result_path.read_text())
    if not isinstance(result, dict):
        raise ValueError(f"analysis source result is not an object: {path}")
    if "trial_name" in result and "task_name" in result:
        job = load_job(path.parent)
        trial = next((item for item in job.trials if item.path.resolve() == path.resolve()), None)
        if trial is None:
            raise ValueError(f"trial is not a member of parent job: {path}")
        return job, trial
    job = load_job(path)
    if len(job.trials) != 1:
        raise ValueError("analysis plan requires a trial path or a single-trial job")
    return job, job.trials[0]


def _analysis_file_digest(path: Path) -> str:
    return f"sha256:{sha256_file(path)}"


def _trial_tree_digests(trial_dir: Path) -> dict[str, str]:
    return {
        path.relative_to(trial_dir).as_posix(): _analysis_file_digest(path)
        for path in sorted(trial_dir.rglob("*"))
        if path.is_file()
    }


def analysis_plan(
    job: JobRecord,
    trial: TrialRecord,
    *,
    repo_root: Path,
    destination_root: Path,
    prompt_path: Path,
    rubric_path: Path,
    agent: str,
    agent_version: str,
    model: str,
) -> AnalysisPlan:
    schema = TrialAnalysisOutput.model_json_schema()
    rubric = json.loads(rubric_path.read_text())
    prompt = _render_analysis_prompt(
        trial,
        prompt_template=prompt_path.read_text(),
        rubric=rubric,
        schema=schema,
    )
    try:
        source_path = trial.path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        source_path = trial.path.resolve().as_posix()
    return AnalysisPlan(
        experiment_id=experiment_id(job),
        job_id=job.id,
        source_trial_id=trial.id,
        source_trial_path=source_path,
        agent=agent,
        agent_version=agent_version,
        model=model,
        estimated_model_calls=1,
        maximum_model_calls=2,
        queue_policy_rule="researcher-followups",
        destination_root=_relative_or_absolute(destination_root, repo_root),
        prompt_digest=_digest_bytes(prompt.encode()),
        rubric_digest=digest_json(rubric),
        output_schema_digest=digest_json(schema),
    )


def _render_analysis_prompt(
    trial: TrialRecord,
    *,
    prompt_template: str,
    rubric: JsonObject,
    schema: dict[str, Any],
) -> str:
    return prompt_template.format(
        source_trial_path=trial.path.resolve().as_posix(),
        rubric=json.dumps(rubric, indent=2, sort_keys=True),
        output_schema=json.dumps(schema, indent=2, sort_keys=True),
    )


def _task_source_digest(trial: TrialRecord) -> str:
    digest = _task_digest(trial)
    if isinstance(digest, str) and len(digest) == 71 and digest.startswith("sha256:"):
        return digest
    lock_path = trial.path / "lock.json"
    return _analysis_file_digest(lock_path)


def _source_digests(trial: TrialRecord, cited_paths: set[str]) -> AnalysisSourceDigests:
    result_path = trial.path / "result.json"
    trajectory_path = trial.path / "agent/trajectory.json"
    required_paths = {"result.json", "lock.json"} | cited_paths
    files = {
        relative: _analysis_file_digest(trial.path / relative)
        for relative in sorted(required_paths)
        if (trial.path / relative).is_file()
    }
    return AnalysisSourceDigests(
        result=_analysis_file_digest(result_path),
        task=_task_source_digest(trial),
        trajectory=(_analysis_file_digest(trajectory_path) if trajectory_path.is_file() else None),
        files=files,
    )


def _load_trajectory_steps(path: Path) -> list[JsonObject] | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not str(payload.get("schema_version", "")).startswith(
        "ATIF-"
    ):
        return None
    steps = payload.get("steps")
    return [item for item in steps if isinstance(item, dict)] if isinstance(steps, list) else []


def validate_analysis_evidence(
    trial: TrialRecord,
    output: TrialAnalysisOutput,
) -> list[str]:
    errors: list[str] = []
    trial_root = trial.path.resolve()
    for index, citation in enumerate(output.evidence):
        path = (trial.path / citation.path).resolve()
        if path != trial_root and trial_root not in path.parents:
            errors.append(f"evidence[{index}] path escapes source trial")
            continue
        if not path.is_file():
            errors.append(f"evidence[{index}] missing file: {citation.path}")
            continue
        steps = _load_trajectory_steps(path)
        if steps is None:
            if citation.step_id is not None or citation.tool_call_id is not None:
                errors.append(
                    f"evidence[{index}] cites a step/tool on non-ATIF file: {citation.path}"
                )
            continue
        if citation.step_id is None:
            errors.append(f"evidence[{index}] ATIF citation requires step_id")
            continue
        step = next(
            (item for item in steps if item.get("step_id") == citation.step_id),
            None,
        )
        if step is None:
            errors.append(f"evidence[{index}] missing step {citation.step_id} in {citation.path}")
            continue
        if citation.tool_call_id is not None:
            call_ids = {
                item.get("tool_call_id")
                for item in step.get("tool_calls") or []
                if isinstance(item, dict)
            }
            if citation.tool_call_id not in call_ids:
                errors.append(
                    f"evidence[{index}] missing tool call {citation.tool_call_id} "
                    f"at step {citation.step_id}"
                )
    exception_class = _exception_class(trial.result)
    agent_failure_categories = {
        "planning",
        "evidence_use",
        "tool_use",
        "implementation",
        "verification_behavior",
        "context_management",
        "policy_or_refusal",
    }
    if exception_class is not None and output.validity == "valid_agent_attempt":
        errors.append(
            f"source has harness exception {exception_class}; cannot label valid_agent_attempt"
        )
    if exception_class is not None and output.primary_category in agent_failure_categories:
        errors.append(
            f"source has harness exception {exception_class}; agent failure category is unsupported"
        )
    return errors


def _parse_analysis_with_retry(
    analyzer: AnalyzerCallable,
    *,
    prompt: str,
    schema: dict[str, Any],
) -> tuple[TrialAnalysisOutput, AnalyzerCallResult]:
    validation_error = ""
    last_result: AnalyzerCallResult | None = None
    for attempt in range(2):
        current_prompt = prompt
        if attempt:
            current_prompt += (
                "\n\nYour prior response failed schema validation. Return only corrected JSON. "
                f"Validation error: {validation_error}"
            )
        last_result = analyzer(current_prompt, schema)
        try:
            payload = json.loads(last_result.raw_output)
            return TrialAnalysisOutput.model_validate(payload), last_result
        except (json.JSONDecodeError, ValidationError) as exc:
            validation_error = str(exc)
    raise ValueError(f"analysis output failed validation after one retry: {validation_error}")


def run_trial_analysis(
    job: JobRecord,
    trial: TrialRecord,
    *,
    analyzer: AnalyzerCallable,
    repo_root: Path,
    destination_root: Path,
    prompt_path: Path,
    rubric_path: Path,
    agent: str,
    agent_version: str,
    model: str,
    created_at: datetime | None = None,
) -> tuple[Path, TrialAnalysisSidecar]:
    before = _trial_tree_digests(trial.path)
    prompt_template = prompt_path.read_text()
    rubric = json.loads(rubric_path.read_text())
    schema = TrialAnalysisOutput.model_json_schema()
    prompt = _render_analysis_prompt(
        trial,
        prompt_template=prompt_template,
        rubric=rubric,
        schema=schema,
    )
    output, call_result = _parse_analysis_with_retry(
        analyzer,
        prompt=prompt,
        schema=schema,
    )
    validation_errors = validate_analysis_evidence(trial, output)
    analysis_id = uuid4()
    try:
        source_path = trial.path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        source_path = trial.path.resolve().as_posix()
    sidecar = TrialAnalysisSidecar(
        analysis_id=analysis_id,
        experiment_id=experiment_id(job),
        job_id=UUID(job.id),
        source_trial_id=UUID(trial.id),
        source_trial_path=source_path,
        source_digests=_source_digests(
            trial,
            {citation.path for citation in output.evidence},
        ),
        analysis_provenance=AnalysisProvenance(
            agent=agent,
            agent_version=agent_version,
            model=model,
            prompt_digest=_digest_bytes(prompt.encode()),
            rubric_digest=digest_json(rubric),
            output_schema_digest=digest_json(schema),
            created_at=created_at or datetime.now(UTC),
            input_tokens=call_result.input_tokens,
            output_tokens=call_result.output_tokens,
            cost_usd=call_result.cost_usd,
        ),
        output=output,
        validation_status="invalid" if validation_errors else "valid",
        validation_errors=validation_errors,
        raw_response_digest=_digest_bytes(call_result.raw_output.encode()),
    )
    if _trial_tree_digests(trial.path) != before:
        raise RuntimeError("analysis modified the immutable source trial")
    sidecar_dir = destination_root.resolve() / str(analysis_id)
    sidecar_dir.mkdir(parents=True, exist_ok=False)
    sidecar_path = sidecar_dir / ANALYSIS_SIDECAR_FILENAME
    sidecar_path.write_text(sidecar.model_dump_json(indent=2) + "\n")
    return sidecar_path, sidecar


def write_analysis_review(
    sidecar_path: Path,
    *,
    disposition: str,
    rationale: str,
    reviewer: str,
    superseded_by: UUID | None = None,
    reviewed_at: datetime | None = None,
) -> tuple[Path, AnalysisReview]:
    sidecar = TrialAnalysisSidecar.model_validate_json(sidecar_path.read_text())
    review = AnalysisReview(
        review_id=uuid4(),
        analysis_id=sidecar.analysis_id,
        disposition=disposition,
        rationale=rationale,
        reviewer=reviewer,
        reviewed_at=reviewed_at or datetime.now(UTC),
        superseded_by=superseded_by,
    )
    reviews_dir = sidecar_path.parent / ANALYSIS_REVIEWS_DIRNAME
    reviews_dir.mkdir(exist_ok=True)
    review_path = reviews_dir / f"{review.review_id}.json"
    with review_path.open("x") as handle:
        handle.write(review.model_dump_json(indent=2) + "\n")
    return review_path, review


def validate_queue_authorization(
    authorization_path: Path,
    *,
    repo_root: Path,
    source_trial_id: str,
) -> JsonObject:
    resolved = authorization_path.resolve()
    running = (repo_root / "queue/running").resolve()
    if resolved.parent != running:
        raise ValueError("live analysis requires an authorization in queue/running")
    payload = json.loads(resolved.read_text())
    if not isinstance(payload, dict):
        raise ValueError("queue authorization must be an object")
    required = {
        "kind": "researcher-followup",
        "policy_rule": "researcher-followups",
        "source_trial_id": source_trial_id,
    }
    for field, expected in required.items():
        if payload.get(field) != expected:
            raise ValueError(f"queue authorization {field} must be {expected!r}")
    if payload.get("max_model_calls") != 2:
        raise ValueError("queue authorization must cap max_model_calls at 2")
    return payload


class CodexExecAnalyzer:
    """Queue-gated headless Codex adapter; tests should inject a stub instead."""

    def __init__(
        self,
        *,
        repo_root: Path,
        trial: TrialRecord,
        model: str,
        authorization_path: Path,
        scratch_dir: Path,
    ) -> None:
        validate_queue_authorization(
            authorization_path,
            repo_root=repo_root,
            source_trial_id=trial.id,
        )
        self.repo_root = repo_root
        self.trial = trial
        self.model = model
        self.authorization_path = authorization_path
        self.scratch_dir = scratch_dir.resolve()
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        self._calls = 0

    def __call__(self, prompt: str, schema: dict[str, Any]) -> AnalyzerCallResult:
        validate_queue_authorization(
            self.authorization_path,
            repo_root=self.repo_root,
            source_trial_id=self.trial.id,
        )
        if self._calls >= 2:
            raise RuntimeError("queue authorization caps analysis at two model calls")
        self._calls += 1
        call_id = uuid4()
        schema_path = self.scratch_dir / f"{call_id}.schema.json"
        output_path = self.scratch_dir / f"{call_id}.output.json"
        schema_path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
        completed = subprocess.run(
            [
                "codex",
                "exec",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--model",
                self.model,
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "--cd",
                str(self.trial.path),
                prompt,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=subscription_environment(),
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"codex exec failed with {completed.returncode}: {completed.stderr.strip()[:500]}"
            )
        return AnalyzerCallResult(raw_output=output_path.read_text())


def ingest_analysis_sidecar(
    database_url: str,
    sidecar_path: Path,
    *,
    root: Path,
) -> TrialAnalysisSidecar:
    sidecar = TrialAnalysisSidecar.model_validate_json(sidecar_path.read_text())
    sidecar_sha256 = _analysis_file_digest(sidecar_path)
    with psycopg.connect(database_url) as connection:
        existing = connection.execute(
            "SELECT sidecar_sha256 FROM analysis_invocations WHERE id = %s",
            (str(sidecar.analysis_id),),
        ).fetchone()
        if existing is not None and existing[0] not in (None, sidecar_sha256):
            raise ValueError(
                f"analysis {sidecar.analysis_id} is already indexed with different bytes"
            )
        connection.execute(
            """
            INSERT INTO analysis_invocations (
                id, source_trial_id, sidecar_path, sidecar_sha256, validation_status,
                agent_name, agent_version, model_name, prompt_digest,
                rubric_digest, output_schema_digest, source_digests,
                created_at, input_tokens, output_tokens, cost_usd, raw_sidecar
            ) VALUES (
                %(id)s, %(source_trial_id)s, %(sidecar_path)s, %(sidecar_sha256)s,
                %(validation_status)s, %(agent_name)s, %(agent_version)s,
                %(model_name)s, %(prompt_digest)s, %(rubric_digest)s,
                %(output_schema_digest)s, %(source_digests)s, %(created_at)s,
                %(input_tokens)s, %(output_tokens)s, %(cost_usd)s, %(raw_sidecar)s
            )
            ON CONFLICT (id) DO UPDATE SET
                sidecar_path = EXCLUDED.sidecar_path,
                sidecar_sha256 = EXCLUDED.sidecar_sha256,
                validation_status = EXCLUDED.validation_status,
                raw_sidecar = EXCLUDED.raw_sidecar
            WHERE analysis_invocations.sidecar_sha256 IS NULL
            """,
            {
                "id": str(sidecar.analysis_id),
                "source_trial_id": str(sidecar.source_trial_id),
                "sidecar_path": _relative_or_absolute(sidecar_path, root),
                "sidecar_sha256": sidecar_sha256,
                "validation_status": sidecar.validation_status,
                "agent_name": sidecar.analysis_provenance.agent,
                "agent_version": sidecar.analysis_provenance.agent_version,
                "model_name": sidecar.analysis_provenance.model,
                "prompt_digest": sidecar.analysis_provenance.prompt_digest,
                "rubric_digest": sidecar.analysis_provenance.rubric_digest,
                "output_schema_digest": sidecar.analysis_provenance.output_schema_digest,
                "source_digests": Jsonb(sidecar.source_digests.model_dump(mode="json")),
                "created_at": sidecar.analysis_provenance.created_at,
                "input_tokens": sidecar.analysis_provenance.input_tokens,
                "output_tokens": sidecar.analysis_provenance.output_tokens,
                "cost_usd": sidecar.analysis_provenance.cost_usd,
                "raw_sidecar": Jsonb(sidecar.model_dump(mode="json")),
            },
        )
        connection.execute(
            """
            INSERT INTO analysis_findings (
                analysis_id, validity, primary_category, summary,
                earliest_failure_step_id, confidence, proposed_discriminator,
                alternative_explanations
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (analysis_id) DO UPDATE SET
                validity = EXCLUDED.validity,
                primary_category = EXCLUDED.primary_category,
                summary = EXCLUDED.summary,
                earliest_failure_step_id = EXCLUDED.earliest_failure_step_id,
                confidence = EXCLUDED.confidence,
                proposed_discriminator = EXCLUDED.proposed_discriminator,
                alternative_explanations = EXCLUDED.alternative_explanations
            """,
            (
                str(sidecar.analysis_id),
                sidecar.output.validity,
                sidecar.output.primary_category,
                sidecar.output.summary,
                sidecar.output.earliest_failure_step_id,
                sidecar.output.confidence,
                sidecar.output.proposed_discriminator,
                Jsonb(sidecar.output.alternative_explanations),
            ),
        )
        connection.execute(
            "DELETE FROM analysis_evidence_citations WHERE analysis_id = %s",
            (str(sidecar.analysis_id),),
        )
        for index, citation in enumerate(sidecar.output.evidence):
            connection.execute(
                """
                INSERT INTO analysis_evidence_citations (
                    analysis_id, citation_index, source_path, step_id,
                    tool_call_id, supports
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    str(sidecar.analysis_id),
                    index,
                    citation.path,
                    citation.step_id,
                    citation.tool_call_id,
                    citation.supports,
                ),
            )
        reviews_dir = sidecar_path.parent / ANALYSIS_REVIEWS_DIRNAME
        for review_path in sorted(reviews_dir.glob("*.json")):
            review = AnalysisReview.model_validate_json(review_path.read_text())
            connection.execute(
                """
                INSERT INTO analysis_reviews (
                    id, analysis_id, disposition, rationale, reviewer,
                    reviewed_at, superseded_by, review_path
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    str(review.review_id),
                    str(review.analysis_id),
                    review.disposition,
                    review.rationale,
                    review.reviewer,
                    review.reviewed_at,
                    str(review.superseded_by) if review.superseded_by else None,
                    _relative_or_absolute(review_path, root),
                ),
            )
    return sidecar


def failure_taxonomy_agreement(
    sidecar_roots: list[Path],
    *,
    labels_root: Path,
    reference_root: Path | None = None,
) -> dict[str, Any]:
    def report_path(path: Path) -> str:
        if reference_root is None:
            return path.as_posix()
        return _relative_or_absolute(path, reference_root)

    label_paths = sorted(labels_root.glob("*.json"))
    labels: dict[str, tuple[str, Path]] = {}
    allowed_categories = set(get_args(FailureCategory))
    for path in label_paths:
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict):
            raise ValueError(f"trajectory label is not an object: {path}")
        trial_name = payload.get("trial_name")
        category = payload.get("primary_category")
        if not isinstance(trial_name, str) or not isinstance(category, str):
            raise ValueError(f"trajectory label lacks trial_name/category: {path}")
        if category not in allowed_categories:
            raise ValueError(f"trajectory label has unknown category {category!r}: {path}")
        if trial_name in labels:
            raise ValueError(f"duplicate trajectory label for {trial_name}")
        labels[trial_name] = (category, path)

    discovered: dict[Path, None] = {}
    for root in sidecar_roots:
        resolved = root.resolve()
        if resolved.is_file() and resolved.name == ANALYSIS_SIDECAR_FILENAME:
            discovered[resolved] = None
        elif resolved.is_dir():
            for path in resolved.rglob(ANALYSIS_SIDECAR_FILENAME):
                discovered[path.resolve()] = None

    rows: list[dict[str, Any]] = []
    valid_predicted_trials: set[str] = set()
    unmatched_predictions: list[str] = []
    invalid_analyses = 0
    for path in sorted(discovered):
        sidecar = TrialAnalysisSidecar.model_validate_json(path.read_text())
        trial_name = Path(sidecar.source_trial_path).name
        if sidecar.validation_status == "valid":
            valid_predicted_trials.add(trial_name)
        else:
            invalid_analyses += 1
        label = labels.get(trial_name)
        if label is None:
            unmatched_predictions.append(str(sidecar.analysis_id))
            continue
        expected, label_path = label
        rows.append(
            {
                "analysis_id": str(sidecar.analysis_id),
                "trial_name": trial_name,
                "analysis_validation_status": sidecar.validation_status,
                "predicted_category": sidecar.output.primary_category,
                "expected_category": expected,
                "exact_match": sidecar.output.primary_category == expected,
                "sidecar_path": report_path(path),
                "sidecar_sha256": _analysis_file_digest(path),
                "label_path": report_path(label_path),
                "label_sha256": _analysis_file_digest(label_path),
            }
        )
    valid_rows = [row for row in rows if row["analysis_validation_status"] == "valid"]
    matches = sum(bool(row["exact_match"]) for row in valid_rows)
    return {
        "schema_version": 1,
        "labels_digest": digest_json(
            [
                {
                    "path": report_path(path),
                    "sha256": _analysis_file_digest(path),
                }
                for path in label_paths
            ]
        ),
        "sidecars_digest": digest_json(
            [
                {"path": report_path(path), "sha256": _analysis_file_digest(path)}
                for path in sorted(discovered)
            ]
        ),
        "n_labels": len(labels),
        "n_sidecars": len(discovered),
        "n_matched_valid": len(valid_rows),
        "n_invalid_analyses": invalid_analyses,
        "exact_matches": matches,
        "exact_agreement": matches / len(valid_rows) if valid_rows else None,
        "label_coverage": (
            len(set(labels) & valid_predicted_trials) / len(labels) if labels else None
        ),
        "unmatched_analysis_ids": sorted(unmatched_predictions),
        "labels_without_valid_analysis": sorted(set(labels) - valid_predicted_trials),
        "comparisons": sorted(rows, key=lambda row: (row["trial_name"], row["analysis_id"])),
    }


def write_failure_taxonomy_agreement(
    sidecar_roots: list[Path],
    *,
    labels_root: Path,
    output_path: Path,
    reference_root: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    report = failure_taxonomy_agreement(
        sidecar_roots,
        labels_root=labels_root,
        reference_root=reference_root,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    temporary.replace(output_path)
    return output_path, report
