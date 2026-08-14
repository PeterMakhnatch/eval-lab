from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg.types.json import Jsonb

from harbor_lab.atif import ExportedTable, ExportResult, export_trajectories, project_trial
from harbor_lab.results import JobRecord, TrialRecord, duration_seconds, sha256_file

JsonObject = dict[str, Any]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest_json(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


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
class JobFacts:
    trials: tuple[TrialFact, ...]
    rewards: tuple[RewardFact, ...]
    artifacts: tuple[ArtifactFact, ...]
    tool_usage: tuple[ToolUseFact, ...]


@dataclass(frozen=True)
class RebuildResult:
    trajectory_export: ExportResult
    fact_export: ExportResult

    @property
    def tables(self) -> tuple[ExportedTable, ...]:
        return self.trajectory_export.tables + self.fact_export.tables


def extract_trial_fact(job: JobRecord, trial: TrialRecord) -> TrialFact:
    projection = project_trial(job, trial)
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
    return TrialFact(
        experiment_id=experiment_id(job),
        job_id=job.id,
        trial_id=trial.id,
        job_name=job.name,
        trial_name=trial.name,
        task_name=_string(result.get("task_name")),
        task_digest=_task_digest(trial),
        verifier_digest=_verifier_digest(job, trial),
        environment_digest=digest_json(trial.lock.get("environment") or {}),
        agent_config_digest=digest_json(trial.lock.get("agent") or {}),
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
    )


def extract_job_facts(job: JobRecord) -> JobFacts:
    trial_facts: list[TrialFact] = []
    reward_facts: list[RewardFact] = []
    artifact_facts: list[ArtifactFact] = []
    tool_usage: list[ToolUseFact] = []
    association = experiment_id(job)
    for trial in sorted(job.trials, key=lambda item: item.id):
        projection = project_trial(job, trial)
        trial_facts.append(extract_trial_fact(job, trial))
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
    return JobFacts(
        trials=tuple(trial_facts),
        rewards=tuple(reward_facts),
        artifacts=tuple(artifact_facts),
        tool_usage=tuple(tool_usage),
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
            }
            for table_name, rows in rows_by_table.items():
                exported.append(
                    _write_fact_table(partition / f"{table_name}.parquet", table_name, rows)
                )
    return ExportResult(root=output_root, tables=tuple(exported))


def rebuild_from_raw(jobs: list[JobRecord], output_root: Path) -> RebuildResult:
    return RebuildResult(
        trajectory_export=export_trajectories(jobs, output_root),
        fact_export=export_facts(jobs, output_root),
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
                        agent_config_digest, exception_phase,
                        environment_setup_seconds, agent_setup_seconds,
                        agent_execution_seconds, verifier_seconds,
                        trajectory_count, invalid_trajectory_count, step_count,
                        llm_call_count, tool_call_count, command_failure_count,
                        repeated_failed_command_count, artifact_count,
                        missing_artifact_count, artifact_set_digest, raw_facts,
                        updated_at
                    ) VALUES (
                        %(trial_id)s, %(verifier_digest)s, %(environment_digest)s,
                        %(agent_config_digest)s, %(exception_phase)s,
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
