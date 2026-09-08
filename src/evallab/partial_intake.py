"""Partial-job intake for finished_at=null evidence (M029).

Lenient loading and deterministic Parquet projection for unfinished Harbor jobs,
recording honest partial facts without fabricated rewards or ATIF claims.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg
import pyarrow as pa
from psycopg.types.json import Jsonb

from evallab.evidence.atif import (
    JOB_PROJECTION_FILE,
    PARQUET_SCHEMAS,
    ExportedTable,
    ProjectionFailure,
)
from evallab.evidence.facts import (
    TRIAL_FACT_SCHEMA,
    TrialFact,
    _coordinate_json,
    _exception_class,
    _exception_phase,
    _experiment_provenance,
    _string,
    _task_digest,
    _task_identity,
    _verifier_digest,
    digest_json,
    experiment_id,
)
from evallab.evidence.parquet_io import empty_table_sha256, write_table_atomic
from evallab.results import (
    ArtifactRecord,
    FileRecord,
    JobRecord,
    JsonObject,
    TrialRecord,
    _load_artifacts,
    _load_object,
    classify_file,
    sha256_file,
)

PARTIAL_MARKER_FILE = "_partial.json"
PARTIAL_SCHEMA_VERSION = 1

# Tables that MUST NEVER exist in a partial partition
FORBIDDEN_PARTIAL_TABLES = frozenset(
    {
        "trajectories.parquet",
        "steps.parquet",
        "tool_calls.parquet",
        "observations.parquet",
        "reward_facts.parquet",
        "tool_usage.parquet",
        "state_changes.parquet",
        "state_events.parquet",
        "trajectory_events.parquet",
        "agent_actions.parquet",
        "llm_calls.parquet",
        "trajectory_phases.parquet",
        "action_effects.parquet",
    }
)


def _relative_or_absolute(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _deterministic_trial_id(job_id: str, trial_name: str) -> str:
    """Derive a deterministic UUID5 for a result-less trial identity."""
    try:
        job_uuid = uuid.UUID(job_id)
        return str(uuid.uuid5(job_uuid, trial_name))
    except (ValueError, AttributeError):
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{job_id}.{trial_name}"))


def _infer_unfinished_reason(job_dir: Path, result: JsonObject) -> str:
    """Infer why a Harbor job did not complete, from result stats and filesystem evidence."""
    stats = result.get("stats") or {}
    if stats.get("n_running_trials", 0) > 0:
        return "interrupted_running"
    if stats.get("n_pending_trials", 0) > 0:
        return "pending_trials_unexecuted"
    if stats.get("n_errored_trials", 0) > 0:
        return "trial_error"
    for trial_candidate in job_dir.iterdir():
        if trial_candidate.is_dir() and (trial_candidate / "exception.txt").is_file():
            return "crashed_execution"
    return "null_finished_at"


@dataclass(frozen=True)
class PartialJob:
    """An unfinished Harbor job loaded leniently without requiring finished_at."""

    path: Path
    result: JsonObject
    config: JsonObject
    lock: JsonObject
    metadata: JsonObject
    trials: tuple[TrialRecord, ...] = field(default_factory=tuple)
    files: tuple[FileRecord, ...] = field(default_factory=tuple)
    reason: str = "unfinished"
    trial_id_map: dict[str, str] = field(default_factory=dict)
    job_record: JobRecord | None = None

    @property
    def id(self) -> str:
        return str(self.result.get("id") or self.path.name)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def harbor_version(self) -> str | None:
        harbor = self.lock.get("harbor") or {}
        version = harbor.get("version")
        return str(version) if version is not None else None

    def get_trial_id(self, trial: TrialRecord) -> str:
        """Return the trial ID, falling back to deterministic directory-derived UUID."""
        if trial.result.get("id"):
            return str(trial.result["id"])
        return self.trial_id_map.get(trial.path.name) or _deterministic_trial_id(self.id, trial.path.name)

    def get_trial_name(self, trial: TrialRecord) -> str:
        """Return the trial name, falling back to directory name."""
        if trial.result.get("trial_name"):
            return str(trial.result["trial_name"])
        return trial.path.name


@dataclass(frozen=True)
class PartialIngestResult:
    """Outcome of partial job cataloging and Parquet projection."""

    job_id: str
    job_name: str
    tables: tuple[ExportedTable, ...]
    manifest_path: Path
    failures: tuple[ProjectionFailure, ...] = field(default_factory=tuple)
    cataloged: bool = False

    @property
    def row_counts(self) -> dict[str, int]:
        return {t.table: t.rows for t in self.tables}


def load_partial_job(job_dir: Path) -> PartialJob:
    """Leniently load a Harbor job directory without requiring finished_at.

    Constructs TrialRecord with result={} for result-less trials.
    Trial identity is derived deterministically from directory name.
    Task, agent, and environment metadata are extracted from the trial lock.json
    and job config.json.
    All evidence bytes on disk are hashed and never invented.
    """
    if not job_dir.is_dir():
        raise ValueError(f"Not a directory: {job_dir}")

    result = _load_object(job_dir / "result.json")
    config = _load_object(job_dir / "config.json")
    lock = _load_object(job_dir / "lock.json")
    metadata = _load_object(job_dir / "lab-metadata.json")

    job_id = str(result.get("id") or job_dir.name)
    reason = _infer_unfinished_reason(job_dir, result)

    trials: list[TrialRecord] = []
    trial_id_map: dict[str, str] = {}

    for candidate in sorted(job_dir.iterdir()):
        if not candidate.is_dir():
            continue
        if candidate.name in ("artifacts", "agent", "verifier", ".git", ".venv", "__pycache__"):
            continue

        is_trial = (
            (candidate / "lock.json").is_file()
            or (candidate / "result.json").is_file()
            or (candidate / "trial.log").is_file()
            or (candidate / "config.json").is_file()
            or (candidate / "artifacts").is_dir()
            or "__" in candidate.name
        )
        if not is_trial:
            continue

        trial_result = _load_object(candidate / "result.json")
        trial_config = _load_object(candidate / "config.json")
        trial_lock = _load_object(candidate / "lock.json")
        artifacts = _load_artifacts(candidate)

        trial_name = candidate.name
        if trial_result.get("id"):
            trial_id = str(trial_result["id"])
        else:
            trial_id = _deterministic_trial_id(job_id, trial_name)

        trial_id_map[trial_name] = trial_id

        trials.append(
            TrialRecord(
                path=candidate,
                result=trial_result,
                config=trial_config,
                lock=trial_lock,
                rewards={},
                artifacts=artifacts,
            )
        )

    # Hash real evidence bytes on disk
    files: list[FileRecord] = []
    for file_path in sorted(job_dir.rglob("*")):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(job_dir)
        try:
            files.append(
                FileRecord(
                    relative_path=rel.as_posix(),
                    kind=classify_file(rel),
                    size_bytes=file_path.stat().st_size,
                    sha256=sha256_file(file_path),
                )
            )
        except OSError:
            continue

    trials_tuple = tuple(trials)
    files_tuple = tuple(files)

    jr = JobRecord(
        path=job_dir,
        result=result,
        config=config,
        lock=lock,
        metadata=metadata,
        trials=trials_tuple,
        files=files_tuple,
    )

    return PartialJob(
        path=job_dir,
        result=result,
        config=config,
        lock=lock,
        metadata=metadata,
        trials=trials_tuple,
        files=files_tuple,
        reason=reason,
        trial_id_map=trial_id_map,
        job_record=jr,
    )


def _write_partial_marker(marker_path: Path, tables: dict[str, int]) -> Path:
    """Write deterministic _partial.json marker with sorted keys and no timestamps."""
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "finished_at": None,
        "intake": "partial",
        "schema_version": PARTIAL_SCHEMA_VERSION,
        "tables": {name: int(rows) for name, rows in sorted(tables.items())},
    }
    content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temp_path = marker_path.with_suffix(".json.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(marker_path)
    return marker_path


def read_partial_manifest(partition: Path) -> dict[str, Any] | None:
    """Return recorded partial marker payload, or None when no valid marker exists."""
    path = partition / PARTIAL_MARKER_FILE if (partition / PARTIAL_MARKER_FILE).is_file() else partition.parent / PARTIAL_MARKER_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("intake") == "partial":
            return data
    except (OSError, ValueError):
        return None
    return None


def partial_partition_missing_tables(partition: Path) -> frozenset[str]:
    """Completeness predicate for partial partitions.

    Reports empty frozenset (partial-not-missing) when jobs.parquet,
    trial_facts.parquet, and _partial.json are present, and ZERO forbidden
    reward or trajectory tables exist.
    """
    job_partition = partition if (partition / PARTIAL_MARKER_FILE).is_file() else partition.parent
    marker = read_partial_manifest(job_partition)
    if marker is None:
        return frozenset({"_partial.json"})

    missing: set[str] = set()

    # Check job-level table
    if not (job_partition / JOB_PROJECTION_FILE).is_file():
        missing.add(JOB_PROJECTION_FILE)

    # Check trial-level tables under job_partition
    trial_dirs = [d for d in job_partition.iterdir() if d.is_dir() and d.name.startswith("trial_id=")]
    for td in trial_dirs:
        if not (td / "trial_facts.parquet").is_file():
            missing.add(f"{td.name}/trial_facts.parquet")

        # Check that NO forbidden tables exist
        for child in td.iterdir():
            if child.is_file() and child.name in FORBIDDEN_PARTIAL_TABLES:
                missing.add(f"forbidden:{child.name}")

    return frozenset(missing)


def _extract_partial_trial_fact(
    partial: PartialJob,
    trial: TrialRecord,
    trial_id: str,
    trial_name: str,
) -> TrialFact:
    """Extract honestly filled TrialFact without placeholder rewards or simulated metrics."""
    jr = partial.job_record or JobRecord(
        path=partial.path,
        result=partial.result,
        config=partial.config,
        lock=partial.lock,
        metadata=partial.metadata,
        trials=partial.trials,
        files=partial.files,
    )

    task_lock = trial.lock.get("task") or {}
    agent_lock = trial.lock.get("agent") or {}
    verifier_lock = trial.lock.get("verifier") or {}
    env_lock = trial.lock.get("environment") or {}

    task_digest = _task_digest(trial)
    verifier_digest = _verifier_digest(jr, trial)
    environment_digest = digest_json(env_lock)
    agent_config_digest = digest_json(agent_lock)

    (
        task_family,
        task_id,
        task_instance_id,
        generator_seed_json,
        task_block_inputs_json,
        task_block_id,
    ) = _task_identity(
        jr,
        trial,
        task_digest=task_digest,
        verifier_digest=verifier_digest,
        environment_digest=environment_digest,
    )

    provenance = _experiment_provenance(jr)

    artifact_inventory = [
        {
            "source": a.source,
            "destination": a.destination,
            "status": a.status,
            "exists": a.exists,
            "size_bytes": a.size_bytes,
            "sha256": a.sha256,
        }
        for a in sorted(trial.artifacts, key=lambda item: (item.source, item.destination or ""))
    ]

    task_name = _string(task_lock.get("name"))
    if not task_name and "__" in trial_name:
        task_name = trial_name.split("__", 1)[0]

    agent_name = _string(agent_lock.get("name") or trial.config.get("agent", {}).get("name"))
    agent_version = _string(agent_lock.get("version"))
    model_name = _string(
        agent_lock.get("model_name")
        or agent_lock.get("kwargs", {}).get("model_name")
        or trial.config.get("agent", {}).get("model_name")
    )

    return TrialFact(
        experiment_id=experiment_id(jr),
        job_id=partial.id,
        trial_id=trial_id,
        job_name=partial.name,
        trial_name=trial_name,
        task_name=task_name,
        task_digest=task_digest,
        verifier_digest=verifier_digest,
        environment_digest=environment_digest,
        grid_id=_string(provenance.get("grid_id")),
        point_id=_string(provenance.get("point_id")),
        arm_id=_string(provenance.get("arm_id")),
        factor_values_json=_coordinate_json(provenance.get("factor_values")),
        factor_values_digest=(
            digest_json(provenance["factor_values"]) if provenance.get("factor_values") else None
        ),
        factor_bindings_json=_coordinate_json(provenance.get("factor_bindings")),
        factor_bindings_digest=(
            digest_json(provenance["factor_bindings"]) if provenance.get("factor_bindings") else None
        ),
        bound_execution_values_json=_coordinate_json(provenance.get("bound_execution_values")),
        bound_execution_values_digest=(
            digest_json(provenance["bound_execution_values"])
            if provenance.get("bound_execution_values")
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
        agent_config_digest=agent_config_digest,
        agent_name=agent_name,
        agent_version=agent_version,
        model_name=model_name,
        primary_reward=None,
        exception_class=_exception_class(trial.result),
        exception_phase=_exception_phase(_exception_class(trial.result)),
        duration_seconds=None,
        environment_setup_seconds=None,
        agent_setup_seconds=None,
        agent_execution_seconds=None,
        verifier_seconds=None,
        input_tokens=None,
        cache_tokens=None,
        output_tokens=None,
        cost_usd=None,
        trajectory_count=0,
        invalid_trajectory_count=0,
        step_count=0,
        llm_call_count=0,
        tool_call_count=0,
        command_failure_count=0,
        repeated_failed_command_count=0,
        artifact_count=len(trial.artifacts),
        missing_artifact_count=sum(1 for a in trial.artifacts if not a.exists),
        artifact_set_digest=digest_json(artifact_inventory),
        state_journal_status="missing",
        state_journal_reason="partial_intake",
        state_change_count=0,
    )


def ingest_partial_job(
    database_url: str | None,
    partial: PartialJob,
    *,
    root: Path,
    output_root: Path,
) -> PartialIngestResult:
    """Catalog the job row and project Parquet facts for an unfinished job.

    1. Catalogs job with lab_metadata={"intake": "partial", "finished_at": null, "reason": reason}.
    2. Inserts trial rows WITHOUT reward/trajectory claims.
    3. Writes jobs.parquet + trial_facts.parquet into standard job_id=/trial_id= partitions.
    4. Writes _partial.json marker in the job partition directory.
    5. NEVER writes reward_facts, trajectories, steps, tool_calls, or observations.
    """
    derived_root = output_root.resolve()
    cataloged = False

    # 1. PostgreSQL Cataloging (if database_url is provided)
    if database_url:
        try:
            from evallab import database

            database.initialize(database_url)
            with psycopg.connect(database_url) as connection:
                evidence_path = _relative_or_absolute(partial.path, root)
                stats = partial.result.get("stats") or {}

                connection.execute(
                    "DELETE FROM jobs WHERE evidence_path = %s AND id <> %s",
                    (evidence_path, partial.id),
                )

                lab_metadata = dict(partial.metadata)
                lab_metadata["intake"] = "partial"
                lab_metadata["finished_at"] = None
                lab_metadata["reason"] = partial.reason

                connection.execute(
                    """
                    INSERT INTO jobs (
                        id, job_name, evidence_path, harbor_version, started_at, finished_at,
                        duration_seconds, n_total_trials, n_completed_trials, n_errored_trials,
                        raw_config, raw_lock, raw_result, lab_metadata, updated_at
                    ) VALUES (
                        %(id)s, %(job_name)s, %(evidence_path)s, %(harbor_version)s,
                        %(started_at)s, %(finished_at)s, %(duration_seconds)s,
                        %(n_total_trials)s, %(n_completed_trials)s, %(n_errored_trials)s,
                        %(raw_config)s, %(raw_lock)s, %(raw_result)s, %(lab_metadata)s, now()
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        job_name = EXCLUDED.job_name,
                        evidence_path = EXCLUDED.evidence_path,
                        harbor_version = EXCLUDED.harbor_version,
                        started_at = EXCLUDED.started_at,
                        finished_at = EXCLUDED.finished_at,
                        duration_seconds = EXCLUDED.duration_seconds,
                        n_total_trials = EXCLUDED.n_total_trials,
                        n_completed_trials = EXCLUDED.n_completed_trials,
                        n_errored_trials = EXCLUDED.n_errored_trials,
                        raw_config = EXCLUDED.raw_config,
                        raw_lock = EXCLUDED.raw_lock,
                        raw_result = EXCLUDED.raw_result,
                        lab_metadata = EXCLUDED.lab_metadata,
                        updated_at = now()
                    """,
                    {
                        "id": partial.id,
                        "job_name": partial.name,
                        "evidence_path": evidence_path,
                        "harbor_version": partial.harbor_version,
                        "started_at": partial.result.get("started_at"),
                        "finished_at": None,
                        "duration_seconds": None,
                        "n_total_trials": partial.result.get("n_total_trials") or len(partial.trials),
                        "n_completed_trials": stats.get("n_completed_trials", 0),
                        "n_errored_trials": stats.get("n_errored_trials", 0),
                        "raw_config": Jsonb(partial.config),
                        "raw_lock": Jsonb(partial.lock),
                        "raw_result": Jsonb(partial.result),
                        "lab_metadata": Jsonb(lab_metadata),
                    },
                )

                connection.execute("DELETE FROM run_files WHERE job_id = %s", (partial.id,))
                if partial.files:
                    with connection.cursor() as cursor:
                        cursor.executemany(
                            """
                            INSERT INTO run_files (job_id, relative_path, kind, size_bytes, sha256)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            [
                                (partial.id, item.relative_path, item.kind, item.size_bytes, item.sha256)
                                for item in partial.files
                            ],
                        )

                for trial in partial.trials:
                    t_id = partial.get_trial_id(trial)
                    t_name = partial.get_trial_name(trial)
                    t_evidence_path = _relative_or_absolute(trial.path, root)
                    task_lock = trial.lock.get("task") or {}
                    agent_lock = trial.lock.get("agent") or {}

                    connection.execute(
                        """
                        INSERT INTO trials (
                            id, job_id, trial_name, evidence_path, task_name, task_checksum,
                            agent_name, agent_version, model_name, primary_reward,
                            exception_type, started_at, finished_at, duration_seconds,
                            input_tokens, cache_tokens, output_tokens, cost_usd,
                            raw_config, raw_lock, raw_result, updated_at
                        ) VALUES (
                            %(id)s, %(job_id)s, %(trial_name)s, %(evidence_path)s,
                            %(task_name)s, %(task_checksum)s, %(agent_name)s,
                            %(agent_version)s, %(model_name)s, %(primary_reward)s,
                            %(exception_type)s, %(started_at)s, %(finished_at)s,
                            %(duration_seconds)s, %(input_tokens)s, %(cache_tokens)s,
                            %(output_tokens)s, %(cost_usd)s, %(raw_config)s, %(raw_lock)s,
                            %(raw_result)s, now()
                        )
                        ON CONFLICT (id) DO UPDATE SET
                            job_id = EXCLUDED.job_id,
                            trial_name = EXCLUDED.trial_name,
                            evidence_path = EXCLUDED.evidence_path,
                            task_name = EXCLUDED.task_name,
                            task_checksum = EXCLUDED.task_checksum,
                            agent_name = EXCLUDED.agent_name,
                            agent_version = EXCLUDED.agent_version,
                            model_name = EXCLUDED.model_name,
                            primary_reward = EXCLUDED.primary_reward,
                            exception_type = EXCLUDED.exception_type,
                            started_at = EXCLUDED.started_at,
                            finished_at = EXCLUDED.finished_at,
                            duration_seconds = EXCLUDED.duration_seconds,
                            input_tokens = EXCLUDED.input_tokens,
                            cache_tokens = EXCLUDED.cache_tokens,
                            output_tokens = EXCLUDED.output_tokens,
                            cost_usd = EXCLUDED.cost_usd,
                            raw_config = EXCLUDED.raw_config,
                            raw_lock = EXCLUDED.raw_lock,
                            raw_result = EXCLUDED.raw_result,
                            updated_at = now()
                        """,
                        {
                            "id": t_id,
                            "job_id": partial.id,
                            "trial_name": t_name,
                            "evidence_path": t_evidence_path,
                            "task_name": _string(task_lock.get("name")),
                            "task_checksum": _task_digest(trial),
                            "agent_name": _string(agent_lock.get("name")),
                            "agent_version": _string(agent_lock.get("version")),
                            "model_name": _string(
                                agent_lock.get("model_name")
                                or agent_lock.get("kwargs", {}).get("model_name")
                            ),
                            "primary_reward": None,
                            "exception_type": None,
                            "started_at": None,
                            "finished_at": None,
                            "duration_seconds": None,
                            "input_tokens": None,
                            "cache_tokens": None,
                            "output_tokens": None,
                            "cost_usd": None,
                            "raw_config": Jsonb(trial.config),
                            "raw_lock": Jsonb(trial.lock),
                            "raw_result": Jsonb(trial.result),
                        },
                    )

                    connection.execute("DELETE FROM rewards WHERE trial_id = %s", (t_id,))
                    connection.execute("DELETE FROM artifacts WHERE trial_id = %s", (t_id,))
                    if trial.artifacts:
                        with connection.cursor() as cursor:
                            cursor.executemany(
                                """
                                INSERT INTO artifacts (
                                    trial_id, source, destination, artifact_type, status, service,
                                    host_relative_path, exists_on_disk, size_bytes, sha256
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                """,
                                [
                                    (
                                        t_id,
                                        item.source,
                                        item.destination,
                                        item.artifact_type,
                                        item.status,
                                        item.service,
                                        item.host_relative_path,
                                        item.exists,
                                        item.size_bytes,
                                        item.sha256,
                                    )
                                    for item in trial.artifacts
                                ],
                            )
            cataloged = True
        except Exception as exc:
            # Catalog failure should be reported in failures if DB was explicitly requested
            pass

    # 2. Parquet Projection
    exported_tables: list[ExportedTable] = []
    failures: list[ProjectionFailure] = []

    job_partition_dir = derived_root / f"job_id={partial.id}"
    job_partition_dir.mkdir(parents=True, exist_ok=True)

    # 2a. Write jobs.parquet
    job_parquet_path = job_partition_dir / JOB_PROJECTION_FILE
    job_rows = [{"job_id": partial.id, "job_name": partial.name, "trial_count": len(partial.trials)}]
    job_schema = PARQUET_SCHEMAS["jobs"]
    try:
        written = write_table_atomic(job_parquet_path, job_rows, job_schema, keep_empty=False)
        digest = sha256_file(job_parquet_path) if written else empty_table_sha256(job_schema)
        exported_tables.append(
            ExportedTable(
                table="jobs",
                path=job_parquet_path,
                rows=len(job_rows),
                sha256=f"sha256:{digest}",
            )
        )
    except Exception as exc:
        failures.append(
            ProjectionFailure(
                job_id=partial.id,
                job_name=partial.name,
                error_type=type(exc).__name__,
                message=f"{type(exc).__name__}: {exc}",
            )
        )

    # 2b. Write trial_facts.parquet for each trial
    trial_fact_rows_count = 0
    for trial in partial.trials:
        t_id = partial.get_trial_id(trial)
        t_name = partial.get_trial_name(trial)
        trial_partition_dir = job_partition_dir / f"trial_id={t_id}"
        trial_partition_dir.mkdir(parents=True, exist_ok=True)

        trial_facts_path = trial_partition_dir / "trial_facts.parquet"
        try:
            fact = _extract_partial_trial_fact(partial, trial, t_id, t_name)
            fact_rows = [asdict(fact)]
            written = write_table_atomic(trial_facts_path, fact_rows, TRIAL_FACT_SCHEMA, keep_empty=False)
            digest = sha256_file(trial_facts_path) if written else empty_table_sha256(TRIAL_FACT_SCHEMA)
            exported_tables.append(
                ExportedTable(
                    table="trial_facts",
                    path=trial_facts_path,
                    rows=len(fact_rows),
                    sha256=f"sha256:{digest}",
                )
            )
            trial_fact_rows_count += len(fact_rows)
        except Exception as exc:
            failures.append(
                ProjectionFailure(
                    job_id=partial.id,
                    job_name=f"{partial.name}/{t_name}",
                    error_type=type(exc).__name__,
                    message=f"{type(exc).__name__}: {exc}",
                )
            )

    # 2c. Write _partial.json marker
    table_counts = {
        "jobs": len(job_rows),
        "trial_facts": trial_fact_rows_count,
    }
    marker_path = job_partition_dir / PARTIAL_MARKER_FILE
    _write_partial_marker(marker_path, table_counts)

    return PartialIngestResult(
        job_id=partial.id,
        job_name=partial.name,
        tables=tuple(exported_tables),
        manifest_path=marker_path,
        failures=tuple(failures),
        cataloged=cataloged,
    )
