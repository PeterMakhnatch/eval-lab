from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from evallab.results import JobRecord, duration_seconds
from evallab.runner import transient_provider_exception
from evallab.schemas import CanaryDriftObservation


def schema_path() -> Path:
    return Path(__file__).resolve().parents[2] / "sql" / "schema.sql"


def initialize(database_url: str) -> None:
    schema = schema_path().read_text()
    with psycopg.connect(database_url) as connection:
        connection.execute(schema)


def _relative_or_absolute(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _exception_type(result: dict[str, Any]) -> str | None:
    exception = result.get("exception_info") or {}
    if transient_provider_exception(result) is not None:
        return "transient_harness"
    value = exception.get("exception_type")
    return str(value) if value else None


def count_consecutive_harness_failures(exception_types: Iterable[str | None]) -> int:
    """Count recent failures while treating provider capacity as neutral noise."""
    count = 0
    for exception_type in exception_types:
        if exception_type == "transient_harness":
            continue
        if exception_type is None:
            break
        count += 1
    return count


def _executemany(
    connection: psycopg.Connection[Any], query: str, parameters: list[tuple[Any, ...]]
) -> None:
    with connection.cursor() as cursor:
        cursor.executemany(query, parameters)


def ingest_job(connection: psycopg.Connection[Any], job: JobRecord, *, root: Path) -> None:
    stats = job.result.get("stats") or {}
    evidence_path = _relative_or_absolute(job.path, root)
    # A named local evidence directory can be intentionally regenerated before
    # publication. The filesystem remains authoritative, so remove a stale row
    # that points at the same path but carries the superseded Harbor UUID.
    connection.execute(
        "DELETE FROM jobs WHERE evidence_path = %s AND id <> %s",
        (evidence_path, job.id),
    )
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
            "id": job.id,
            "job_name": job.name,
            "evidence_path": evidence_path,
            "harbor_version": job.harbor_version,
            "started_at": job.result.get("started_at"),
            "finished_at": job.result.get("finished_at"),
            "duration_seconds": duration_seconds(
                job.result.get("started_at"), job.result.get("finished_at")
            ),
            "n_total_trials": job.result.get("n_total_trials"),
            "n_completed_trials": stats.get("n_completed_trials"),
            "n_errored_trials": stats.get("n_errored_trials"),
            "raw_config": Jsonb(job.config),
            "raw_lock": Jsonb(job.lock),
            "raw_result": Jsonb(job.result),
            "lab_metadata": Jsonb(job.metadata),
        },
    )

    connection.execute("DELETE FROM run_files WHERE job_id = %s", (job.id,))
    if job.files:
        _executemany(
            connection,
            """
            INSERT INTO run_files (job_id, relative_path, kind, size_bytes, sha256)
            VALUES (%s, %s, %s, %s, %s)
            """,
            [
                (job.id, item.relative_path, item.kind, item.size_bytes, item.sha256)
                for item in job.files
            ],
        )

    for trial in job.trials:
        result = trial.result
        agent_info = result.get("agent_info") or {}
        model_info = agent_info.get("model_info") or {}
        agent_result = result.get("agent_result") or {}
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
                "id": trial.id,
                "job_id": job.id,
                "trial_name": trial.name,
                "evidence_path": _relative_or_absolute(trial.path, root),
                "task_name": result.get("task_name"),
                "task_checksum": result.get("task_checksum"),
                "agent_name": agent_info.get("name"),
                "agent_version": agent_info.get("version"),
                "model_name": model_info.get("name") or model_info.get("model_name"),
                "primary_reward": trial.primary_reward,
                "exception_type": _exception_type(result),
                "started_at": result.get("started_at"),
                "finished_at": result.get("finished_at"),
                "duration_seconds": duration_seconds(
                    result.get("started_at"), result.get("finished_at")
                ),
                "input_tokens": agent_result.get("n_input_tokens"),
                "cache_tokens": agent_result.get("n_cache_tokens"),
                "output_tokens": agent_result.get("n_output_tokens"),
                "cost_usd": agent_result.get("cost_usd"),
                "raw_config": Jsonb(trial.config),
                "raw_lock": Jsonb(trial.lock),
                "raw_result": Jsonb(result),
            },
        )

        connection.execute("DELETE FROM rewards WHERE trial_id = %s", (trial.id,))
        if trial.rewards:
            _executemany(
                connection,
                "INSERT INTO rewards (trial_id, name, value) VALUES (%s, %s, %s)",
                [(trial.id, name, value) for name, value in trial.rewards.items()],
            )

        connection.execute("DELETE FROM artifacts WHERE trial_id = %s", (trial.id,))
        if trial.artifacts:
            _executemany(
                connection,
                """
                INSERT INTO artifacts (
                    trial_id, source, destination, artifact_type, status, service,
                    host_relative_path, exists_on_disk, size_bytes, sha256
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        trial.id,
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


def ingest(database_url: str, jobs: Iterable[JobRecord], *, root: Path) -> int:
    count = 0
    with psycopg.connect(database_url) as connection:
        for job in jobs:
            ingest_job(connection, job, root=root)
            count += 1
    return count


def list_trials(database_url: str, *, limit: int = 25) -> list[tuple[Any, ...]]:
    with psycopg.connect(database_url) as connection:
        return list(
            connection.execute(
                """
                SELECT job_name, trial_name, task_name, agent_name, model_name,
                       primary_reward, exception_type, duration_seconds
                FROM trial_observations
                ORDER BY ingested_at DESC, job_name, trial_name
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        )


def ping(database_url: str) -> str:
    with psycopg.connect(database_url, connect_timeout=2) as connection:
        row = connection.execute("SELECT version()").fetchone()
    return str(row[0]) if row else "unknown"


def daily_cost_usd(database_url: str, day: date) -> float:
    with psycopg.connect(database_url, connect_timeout=2) as connection:
        row = connection.execute(
            """
            SELECT COALESCE(sum(cost_usd), 0)
            FROM trials
            WHERE finished_at IS NOT NULL
              AND (finished_at::timestamptz AT TIME ZONE current_setting('TIMEZONE'))::date = %s
            """,
            (day,),
        ).fetchone()
    return float(row[0]) if row else 0.0


def consecutive_harness_failures(database_url: str) -> int:
    """Count the most recent uninterrupted run of infrastructure exceptions."""
    with psycopg.connect(database_url, connect_timeout=2) as connection:
        rows = connection.execute(
            """
            SELECT exception_type
            FROM trials
            ORDER BY finished_at DESC NULLS LAST, id DESC
            LIMIT 100
            """
        ).fetchall()
    return count_consecutive_harness_failures(exception_type for (exception_type,) in rows)


def digest_trials(database_url: str, day: date) -> list[tuple[Any, ...]]:
    """Return deterministic trial facts for one catalog-local calendar day."""
    with psycopg.connect(database_url, connect_timeout=2) as connection:
        return list(
            connection.execute(
                """
                SELECT
                    j.job_name,
                    t.task_name,
                    t.agent_name,
                    t.model_name,
                    t.primary_reward,
                    t.exception_type,
                    COALESCE(t.cost_usd, 0),
                    t.finished_at
                FROM trials t
                JOIN jobs j ON j.id = t.job_id
                WHERE t.finished_at IS NOT NULL
                  AND (
                    t.finished_at::timestamptz
                    AT TIME ZONE current_setting('TIMEZONE')
                  )::date = %s
                ORDER BY j.job_name, t.trial_name
                """,
                (day,),
            ).fetchall()
        )


def canary_drift_observations(
    database_url: str, day: date
) -> list[CanaryDriftObservation]:
    with psycopg.connect(database_url, connect_timeout=2) as connection:
        rows = connection.execute(
            """
            SELECT
                task_name,
                task_version,
                agent_name,
                reward,
                attempt_count,
                exception_count,
                baseline_n,
                baseline_mean,
                baseline_stddev,
                previous_task_version,
                task_version_changed,
                is_harness_drift_suspect,
                drift_reason
            FROM canary_drift_observations
            WHERE observation_date = %s
            ORDER BY task_name, agent_name
            """,
            (day,),
        ).fetchall()
    return [
        CanaryDriftObservation(
            task_name=str(row[0]),
            task_version=str(row[1]),
            agent_name=str(row[2]),
            reward=float(row[3]) if row[3] is not None else None,
            attempt_count=int(row[4]),
            exception_count=int(row[5]),
            baseline_n=int(row[6]),
            baseline_mean=float(row[7]) if row[7] is not None else None,
            baseline_stddev=float(row[8]) if row[8] is not None else None,
            previous_task_version=str(row[9]) if row[9] is not None else None,
            task_version_changed=bool(row[10]),
            is_harness_drift_suspect=bool(row[11]),
            drift_reason=str(row[12]) if row[12] is not None else None,
        )
        for row in rows
    ]
