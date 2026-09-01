from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, LiteralString, cast

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from evallab.outcome_authority import (
    AgentOutcomeStatus,
    ArtifactOutcomeStatus,
    OutcomeAuthorityResolution,
    OutcomeRecord,
    outcome_record_from_regrade,
    resolve_outcome_authority,
)
from evallab.results import JobRecord, TrialRecord, duration_seconds
from evallab.runner import transient_provider_exception
from evallab.schemas import CanaryDriftObservation

CanaryDriftReason = Literal[
    "task_version_changed",
    "reward_excursion",
    "canary_exception",
]


def _drift_reason(value: object) -> CanaryDriftReason | None:
    if value == "task_version_changed":
        return "task_version_changed"
    if value == "reward_excursion":
        return "reward_excursion"
    if value == "canary_exception":
        return "canary_exception"
    return None


def schema_path() -> Path:
    return Path(__file__).resolve().parents[2] / "sql" / "schema.sql"


def views_path() -> Path:
    return Path(__file__).resolve().parents[2] / "sql" / "views.sql"


def initialize(database_url: str) -> None:
    schema = cast(LiteralString, schema_path().read_text())
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
    connection: psycopg.Connection[Any], query: LiteralString, parameters: list[tuple[Any, ...]]
) -> None:
    with connection.cursor() as cursor:
        cursor.executemany(query, parameters)


def ingest_job(connection: psycopg.Connection[Any], job: JobRecord, *, root: Path) -> None:
    from evallab.evidence.facts import extract_outcome_records

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

        ingest_trial_outcomes(
            connection,
            extract_outcome_records(job, trial, repo_root=root),
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


def identity(database_url: str) -> str:
    """Return ``host:port/dbname`` for a connection string, never a credential.

    An operator reading a green catalog line has no way to tell which database
    produced it; the same check reported two different counts in one M009
    session because `DATABASE_URL` differed (F-11). Only host, port, and
    database name are read out of the parsed connection info, so a password
    embedded in the URL cannot reach an operator surface or a log.
    """
    try:
        info = conninfo_to_dict(database_url)
    except psycopg.Error:
        return "unparsable connection string"
    host = str(info.get("host") or "localhost")
    port = str(info.get("port") or 5432)
    dbname = str(info.get("dbname") or "")
    return f"{host}:{port}/{dbname}" if dbname else f"{host}:{port}"


def daily_cost_usd(database_url: str, day: date) -> float:
    """Return spend for the explicit UTC policy day, independent of DB settings."""
    with psycopg.connect(database_url, connect_timeout=2) as connection:
        row = connection.execute(
            """
            SELECT COALESCE(sum(cost_usd), 0)
            FROM trials
            WHERE finished_at IS NOT NULL
              AND (finished_at::timestamptz AT TIME ZONE 'UTC')::date = %s
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


def canary_drift_observations(database_url: str, day: date) -> list[CanaryDriftObservation]:
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
            drift_reason=_drift_reason(row[12]),
        )
        for row in rows
    ]


def _ingest_interpretation_artifacts(connection: Any, records: Iterable[Any]) -> int:
    """Insert interpretation records into identity/index tables.

    Accepts any iterable of objects exposing the ``ArtifactRecord`` shape.
    This helper is public so tests can call it with a fake connection.
    """
    count = 0
    for record in records:
        kind = getattr(record, "kind", None)
        artifact_digest = getattr(record, "artifact_digest", None)
        if not artifact_digest:
            continue

        connection.execute(
            cast(
                LiteralString,
                """
                INSERT INTO interpretation_artifacts (
                    artifact_digest, kind, trial_id, job_id, content_digest, artifact_path,
                    cas_uri, pack_digest, judgment_id, decision_id, ingested_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (artifact_digest) DO NOTHING
                """,
            ),
            (
                artifact_digest,
                kind,
                getattr(record, "trial_id", None),
                getattr(record, "job_id", None),
                getattr(record, "content_digest", None),
                str(getattr(record, "artifact_path", "")),
                getattr(record, "cas_uri", None) or None,
                getattr(record, "pack_digest", None) or None,
                getattr(record, "judgment_id", None) or None,
                getattr(record, "decision_id", None) or None,
            ),
        )

        if kind == "judgment":
            produced_at = getattr(record, "produced_at", None)
            produced_at = produced_at if isinstance(produced_at, datetime) else None
            connection.execute(
                cast(
                    LiteralString,
                    """
                    INSERT INTO machine_judgments (
                        judgment_id, judgment_digest, pack_digest, producer_kind, validity,
                        citation_ids, coverage_gaps, artifact_path, cas_uri, produced_at, ingested_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (judgment_id) DO NOTHING
                    """,
                ),
                (
                    artifact_digest,
                    getattr(record, "judgment_digest", None) or None,
                    getattr(record, "pack_digest", None) or None,
                    getattr(record, "producer_kind", None),
                    getattr(record, "validity", None),
                    Jsonb(list(getattr(record, "citation_ids", []) or [])),
                    Jsonb(list(getattr(record, "coverage_gaps", []) or [])),
                    str(getattr(record, "artifact_path", "")),
                    getattr(record, "cas_uri", None) or None,
                    produced_at,
                ),
            )

        if kind == "decision":
            produced_at = getattr(record, "produced_at", None)
            if not isinstance(produced_at, datetime):
                produced_at = None
            connection.execute(
                cast(
                    LiteralString,
                    """
                    INSERT INTO acceptance_decisions (
                        decision_id, decision_digest, decision, judgment_ids, pack_digest,
                        reason_codes, calibration_version, calibration_schema, status,
                        supersedes_decision_id, artifact_path, cas_uri, produced_at, ingested_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (decision_id) DO NOTHING
                    """,
                ),
                (
                    artifact_digest,
                    getattr(record, "decision_digest", None) or None,
                    getattr(record, "decision", None),
                    Jsonb(list(getattr(record, "judgment_ids", []) or [])),
                    getattr(record, "pack_digest", None) or None,
                    Jsonb(list(getattr(record, "reason_codes", []) or [])),
                    getattr(record, "calibration_version", None) or None,
                    getattr(record, "calibration_schema", None) or None,
                    getattr(record, "status", None),
                    getattr(record, "supersedes_decision_id", None) or None,
                    str(getattr(record, "artifact_path", "")),
                    getattr(record, "cas_uri", None) or None,
                    produced_at,
                ),
            )

        count += 1
    return count


def ingest_interpretation_artifacts(database_url: str, records: Iterable[Any]) -> int:
    """Open a real PostgreSQL connection and insert ``ArtifactRecord`` rows."""
    with psycopg.connect(database_url) as connection:
        return _ingest_interpretation_artifacts(connection, records)


def catalog_availability(database_url: str | None, *, connect_timeout: int = 2) -> dict[str, Any]:
    """Report catalog reachability without treating unavailability as zero rows.

    A missing or unreachable PostgreSQL catalog is ``unavailable`` with
    ``row_count=None``. This helper never returns ``0`` for an unread catalog.
    """
    if not database_url:
        return {
            "status": "unavailable",
            "reason": "database_url_not_provided",
            "row_count": None,
        }
    try:
        with psycopg.connect(database_url, connect_timeout=connect_timeout) as connection:
            connection.execute("SELECT 1")
    except Exception as exc:
        detail = str(exc)
        for candidate in (database_url, database_url.replace("'", "''")):
            detail = detail.replace(candidate, "<REDACTED DSN>")
        return {
            "status": "unavailable",
            "reason": f"{type(exc).__name__}: {detail}",
            "row_count": None,
        }
    return {"status": "attached", "reason": None, "row_count": None}


def quota_today(database_url: str) -> list[tuple[str, int, int]]:
    """Return today's UTC consumption (provider, runs, tokens) from v_quota_today."""
    with psycopg.connect(database_url, connect_timeout=2) as connection:
        return list(
            connection.execute(
                """
                SELECT provider, runs, tokens
                FROM v_quota_today
                ORDER BY provider
                """
            ).fetchall()
        )


_TRIAL_OUTCOME_COLUMNS = (
    "outcome_id",
    "trial_id",
    "source_trial_id",
    "outcome_kind",
    "outcome_namespace",
    "outcome_name",
    "reward_value",
    "is_valid_reward",
    "valid_fraction",
    "agent_status",
    "agent_exception",
    "verifier_status",
    "artifact_status",
    "artifact_digest",
    "source_digest",
    "verifier_digest",
    "evidence_digest",
    "authority_state",
    "superseded_by_outcome_id",
    "supersession_reason",
    "is_summable",
    "cas_uri",
    "evidence_path",
    "recorded_at",
)


def _outcome_record_to_row(record: OutcomeRecord) -> tuple[Any, ...]:
    data = record.model_dump(mode="json")
    return tuple(data[col] for col in _TRIAL_OUTCOME_COLUMNS)


def ingest_trial_outcomes(
    connection: psycopg.Connection[Any],
    outcomes: Sequence[OutcomeRecord],
) -> None:
    """Insert immutable outcome facts, ignoring exact re-ingestion duplicates."""
    if not outcomes:
        return
    columns = ", ".join(_TRIAL_OUTCOME_COLUMNS)
    placeholders = ", ".join(["%s"] * len(_TRIAL_OUTCOME_COLUMNS))
    query = cast(
        LiteralString,
        f"""
        INSERT INTO trial_outcomes ({columns})
        VALUES ({placeholders})
        ON CONFLICT (outcome_id) DO NOTHING
        """,
    )
    _executemany(connection, query, [_outcome_record_to_row(outcome) for outcome in outcomes])


def ingest_regrade(
    connection: psycopg.Connection[Any],
    regrade_trial: TrialRecord,
    source_trial_id: str,
    *,
    root: Path,
) -> OutcomeRecord:
    """Ingest a verifier-only regrade while preserving independently observed lineage.

    Source identity and source artifact preservation come from the original
    outcome. The regrade's verifier and evaluated-artifact digests come from
    the regrade evidence itself; neither is copied from the source.
    """
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            SELECT
                outcomes.trial_id,
                outcomes.source_digest,
                outcomes.artifact_digest,
                outcomes.artifact_status,
                outcomes.agent_status,
                outcomes.agent_exception
            FROM trial_outcomes AS outcomes
            LEFT JOIN trials ON trials.id::text = outcomes.trial_id
            WHERE (outcomes.trial_id = %s OR trials.trial_name = %s)
              AND outcomes.outcome_kind IN ('original_verifier', 'manual_audit')
            ORDER BY
                CASE outcomes.outcome_kind WHEN 'original_verifier' THEN 0 ELSE 1 END,
                outcomes.recorded_at NULLS LAST
            LIMIT 1
            """,
            (source_trial_id, source_trial_id),
        )
        source = cursor.fetchone()
    if source is None:
        raise ValueError(
            f"source trial {source_trial_id} has no outcome record for regrade linkage"
        )

    canonical_source_trial_id = str(source["trial_id"])
    record = outcome_record_from_regrade(
        regrade_trial,
        canonical_source_trial_id,
        source_digest=source["source_digest"],
        source_artifact_digest=source["artifact_digest"],
        source_artifact_status=ArtifactOutcomeStatus(source["artifact_status"]),
        source_agent_status=(
            AgentOutcomeStatus(source["agent_status"]) if source["agent_status"] else None
        ),
        source_agent_exception=source["agent_exception"],
        recorded_at=datetime.now(UTC).isoformat(),
    ).model_copy(
        update={
            "evidence_path": _relative_or_absolute(regrade_trial.path, root),
        }
    )

    ingest_trial_outcomes(connection, [record])
    return record


def ingest_regrades(
    database_url: str,
    regrade_trials: Iterable[TrialRecord],
    *,
    root: Path,
) -> int:
    """Append all linkable standalone regrades in one transaction."""
    count = 0
    with psycopg.connect(database_url) as connection:
        for regrade_trial in regrade_trials:
            if regrade_trial.source_trial_id is None:
                continue
            ingest_regrade(
                connection,
                regrade_trial,
                regrade_trial.source_trial_id,
                root=root,
            )
            count += 1
    return count


def resolve_trial_authority(
    connection: psycopg.Connection[Any],
    trial_id: str,
) -> OutcomeAuthorityResolution | None:
    """Resolve the source-neutral outcome authority for a trial from the catalog."""
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            SELECT {}
            FROM trial_outcomes
            WHERE trial_id = %s OR source_trial_id = %s
            """.format(", ".join(_TRIAL_OUTCOME_COLUMNS)),
            (trial_id, trial_id),
        )
        rows = cursor.fetchall()
    if not rows:
        return None
    outcomes = [OutcomeRecord.model_validate(row) for row in rows]
    return resolve_outcome_authority(outcomes, {"trial_id": trial_id})
