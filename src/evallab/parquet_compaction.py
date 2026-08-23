"""Deterministic Parquet compaction engine (WS-E item 4).

Consolidates uncompacted granular partitions from `derived/parquet/job_id=*/` into
closed-day partition layout `derived/parquet/compact/dt=YYYY-MM-DD/*.parquet` (one
file per table per day).

Retains per-trial granular partitions for recent days (default 7 days) for fine-grained
debugging, while pruning older granular partitions after compaction and validation.
Enforces zero row loss and exact schema integrity before and after writes.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from evallab.atif import PARQUET_SCHEMAS
from evallab.event_mart import EVENT_MART_SCHEMAS
from evallab.facts import FACT_SCHEMAS
from evallab.paths import derived_root_from_environment

DEFAULT_RETENTION_DAYS = 7
COMPACT_DIRNAME = "compact"

# --------------------------------------------------------------------------- #
# Schemas & Table Metadata
# --------------------------------------------------------------------------- #

TABLE_SCHEMAS: dict[str, pa.Schema] = {
    **PARQUET_SCHEMAS,
    **FACT_SCHEMAS,
    **EVENT_MART_SCHEMAS,
}

PROJECTED_TABLE_NAMES: tuple[str, ...] = (
    "jobs",
    "trajectories",
    "steps",
    "tool_calls",
    "observations",
    "trial_facts",
    "reward_facts",
    "artifact_facts",
    "tool_usage",
    "state_changes",
    "trajectory_events",
    "agent_actions",
    "llm_calls",
    "trajectory_phases",
    "action_effects",
)

TRIAL_TABLE_NAMES: tuple[str, ...] = (
    "trajectories",
    "steps",
    "tool_calls",
    "observations",
    "trial_facts",
    "reward_facts",
    "artifact_facts",
    "tool_usage",
    "state_changes",
    "trajectory_events",
    "agent_actions",
    "llm_calls",
    "trajectory_phases",
    "action_effects",
)

PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "jobs": ("job_id",),
    "trajectories": ("job_id", "trial_id", "document_id"),
    "steps": ("job_id", "trial_id", "document_id", "step_id"),
    "tool_calls": ("job_id", "trial_id", "document_id", "step_id", "tool_call_id"),
    "observations": ("job_id", "trial_id", "document_id", "step_id", "observation_index"),
    "trial_facts": ("job_id", "trial_id"),
    "reward_facts": ("job_id", "trial_id", "reward_name"),
    "artifact_facts": ("job_id", "trial_id", "source"),
    "tool_usage": ("job_id", "trial_id", "function_name"),
    "state_changes": ("job_id", "trial_id", "path"),
    "trajectory_events": ("job_id", "trial_id", "document_id", "step_id", "event_id"),
    "agent_actions": ("job_id", "trial_id", "document_id", "step_id", "action_id"),
    "llm_calls": ("job_id", "trial_id", "document_id", "step_id", "call_id"),
    "trajectory_phases": ("job_id", "trial_id", "phase_id"),
    "action_effects": ("job_id", "trial_id", "effect_id"),
}


class CompactionValidationError(Exception):
    """Raised when compaction row count or schema validation fails."""


# --------------------------------------------------------------------------- #
# Date Resolution & Discovery
# --------------------------------------------------------------------------- #


def parse_iso_date(value: str | None) -> date | None:
    """Parse an ISO8601 timestamp or date string into a UTC date."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).date()
    except (ValueError, TypeError):
        try:
            return date.fromisoformat(value)
        except (ValueError, TypeError):
            return None


def resolve_job_date(
    job_dir: Path,
    *,
    runs_dir: Path | None = None,
    database_url: str | None = None,
) -> date:
    """Determine the date (dt=YYYY-MM-DD in UTC) for a job partition.

    Hierarchy:
    1. Read timestamp column from trial_id=*/steps.parquet.
    2. Check result.json in runs_dir if available.
    3. Query Postgres if database_url provided and reachable.
    4. Fallback to file mtime of jobs.parquet or job_dir in UTC.
    """
    # 1. Inspect steps.parquet across trial directories
    for steps_file in job_dir.glob("trial_id=*/steps.parquet"):
        if steps_file.is_file():
            try:
                table = pq.read_table(steps_file, columns=["timestamp"])
                if table.num_rows > 0:
                    col = table.column("timestamp")
                    for val in col.to_pylist():
                        if val:
                            parsed_date = parse_iso_date(str(val))
                            if parsed_date is not None:
                                return parsed_date
            except Exception:
                pass

    job_id = job_dir.name.removeprefix("job_id=")

    # 2. Inspect result.json under runs_dir if provided
    if runs_dir is not None and runs_dir.is_dir():
        for candidate in runs_dir.rglob("result.json"):
            try:
                data = json.loads(candidate.read_text())
                if str(data.get("id")) == job_id:
                    finished = data.get("finished_at") or data.get("started_at")
                    if finished:
                        parsed_date = parse_iso_date(str(finished))
                        if parsed_date is not None:
                            return parsed_date
            except Exception:
                pass

    # 3. Query PostgreSQL if database_url is provided
    if database_url:
        try:
            import psycopg

            with psycopg.connect(database_url) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT finished_at, started_at FROM jobs WHERE id = %s",
                    (job_id,),
                )
                row = cur.fetchone()
                if row:
                    ts = row[0] or row[1]
                    if ts:
                        parsed_date = parse_iso_date(str(ts))
                        if parsed_date is not None:
                            return parsed_date
        except Exception:
            pass

    # 4. Fallback: file modification time
    jobs_file = job_dir / "jobs.parquet"
    target = jobs_file if jobs_file.is_file() else job_dir
    mtime = target.stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=UTC).date()


# --------------------------------------------------------------------------- #
# Data Structures
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class JobPartition:
    job_id: str
    path: Path
    dt: str
    table_counts: dict[str, int]


@dataclass(frozen=True)
class DayPlan:
    dt: str
    jobs: tuple[JobPartition, ...]
    is_closed: bool
    is_prunable: bool
    uncompacted_row_counts: dict[str, int]
    existing_compact_row_counts: dict[str, int]


@dataclass(frozen=True)
class CompactionPlan:
    derived_root: Path
    today: str
    retention_days: int
    cutoff_date: str
    days: tuple[DayPlan, ...]
    total_uncompacted_jobs: int


@dataclass(frozen=True)
class DayCompactionResult:
    dt: str
    table_row_counts: dict[str, int]
    pruned_job_ids: tuple[str, ...]
    retained_job_ids: tuple[str, ...]


@dataclass(frozen=True)
class CompactionResult:
    derived_root: Path
    compacted_days: tuple[DayCompactionResult, ...]
    total_compacted_rows: dict[str, int]
    pruned_jobs: tuple[str, ...]
    retained_jobs: tuple[str, ...]
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "derived_root": str(self.derived_root),
            "ok": self.ok,
            "compacted_days": [
                {
                    "dt": day.dt,
                    "table_row_counts": day.table_row_counts,
                    "pruned_jobs": list(day.pruned_job_ids),
                    "retained_jobs": list(day.retained_job_ids),
                }
                for day in self.compacted_days
            ],
            "total_compacted_rows": self.total_compacted_rows,
            "pruned_jobs": list(self.pruned_jobs),
            "retained_jobs": list(self.retained_jobs),
            "errors": list(self.errors),
        }


# --------------------------------------------------------------------------- #
# Discovery & Planning
# --------------------------------------------------------------------------- #


def count_table_rows(path: Path) -> int:
    """Read row count from a parquet file metadata without reading data."""
    if not path.is_file():
        return 0
    try:
        parquet_file = pq.ParquetFile(path)
        return parquet_file.metadata.num_rows
    except Exception:
        return 0


def discover_uncompacted_jobs(
    derived_root: Path,
    *,
    runs_dir: Path | None = None,
    database_url: str | None = None,
) -> list[JobPartition]:
    """Scan derived_root for all uncompacted job_id=* partition directories."""
    partitions: list[JobPartition] = []
    derived_root = derived_root.resolve()
    if not derived_root.is_dir():
        return partitions

    for job_dir in sorted(derived_root.glob("job_id=*")):
        if not job_dir.is_dir():
            continue
        job_id = job_dir.name.removeprefix("job_id=")
        dt = resolve_job_date(
            job_dir,
            runs_dir=runs_dir,
            database_url=database_url,
        ).isoformat()

        counts: dict[str, int] = {}
        jobs_file = job_dir / "jobs.parquet"
        counts["jobs"] = count_table_rows(jobs_file)

        for table_name in TRIAL_TABLE_NAMES:
            table_total = 0
            for trial_file in job_dir.glob(f"trial_id=*/{table_name}.parquet"):
                table_total += count_table_rows(trial_file)
            counts[table_name] = table_total

        partitions.append(
            JobPartition(
                job_id=job_id,
                path=job_dir,
                dt=dt,
                table_counts=counts,
            )
        )
    return partitions


def discover_compacted_row_counts(derived_root: Path, dt: str) -> dict[str, int]:
    """Inspect existing compact/dt=YYYY-MM-DD directory for row counts."""
    counts: dict[str, int] = {}
    day_dir = derived_root / COMPACT_DIRNAME / f"dt={dt}"
    for table_name in PROJECTED_TABLE_NAMES:
        table_path = day_dir / f"{table_name}.parquet"
        counts[table_name] = count_table_rows(table_path)
    return counts


def plan_compaction(
    derived_root: Path,
    *,
    target_date: str | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    clock_today: date | None = None,
    runs_dir: Path | None = None,
    database_url: str | None = None,
) -> CompactionPlan:
    """Plan parquet compaction across uncompacted partitions."""
    derived_root = derived_root.resolve()
    today = clock_today or datetime.now(UTC).date()
    cutoff = today - timedelta(days=retention_days)

    uncompacted_jobs = discover_uncompacted_jobs(
        derived_root,
        runs_dir=runs_dir,
        database_url=database_url,
    )

    jobs_by_date: dict[str, list[JobPartition]] = {}
    for job in uncompacted_jobs:
        jobs_by_date.setdefault(job.dt, []).append(job)

    # If target_date is given, ensure it is considered even if no uncompacted jobs exist
    if target_date and target_date not in jobs_by_date:
        jobs_by_date[target_date] = []

    day_plans: list[DayPlan] = []
    for dt_str in sorted(jobs_by_date):
        if target_date and dt_str != target_date:
            continue
        dt_val = date.fromisoformat(dt_str)
        is_closed = dt_val < today or (target_date is not None and dt_str == target_date)
        is_prunable = dt_val < cutoff

        jobs_for_day = tuple(jobs_by_date[dt_str])
        uncompacted_counts: dict[str, int] = {tbl: 0 for tbl in PROJECTED_TABLE_NAMES}
        for job in jobs_for_day:
            for tbl, count in job.table_counts.items():
                uncompacted_counts[tbl] += count

        existing_counts = discover_compacted_row_counts(derived_root, dt_str)

        day_plans.append(
            DayPlan(
                dt=dt_str,
                jobs=jobs_for_day,
                is_closed=is_closed,
                is_prunable=is_prunable,
                uncompacted_row_counts=uncompacted_counts,
                existing_compact_row_counts=existing_counts,
            )
        )

    return CompactionPlan(
        derived_root=derived_root,
        today=today.isoformat(),
        retention_days=retention_days,
        cutoff_date=cutoff.isoformat(),
        days=tuple(day_plans),
        total_uncompacted_jobs=len(uncompacted_jobs),
    )


# --------------------------------------------------------------------------- #
# Table Reading, Deduplication & Writing
# --------------------------------------------------------------------------- #


def _read_table_or_empty(path: Path, table_name: str) -> pa.Table:
    schema = TABLE_SCHEMAS[table_name]
    if not path.is_file():
        return pa.Table.from_batches([], schema=schema)
    try:
        return pq.read_table(path, schema=schema)
    except Exception:
        # Fallback reading and casting
        raw = pq.read_table(path)
        return raw.cast(schema)


def _collect_table_batches(
    derived_root: Path,
    jobs: Sequence[JobPartition],
    dt: str,
    table_name: str,
) -> list[pa.Table]:
    """Collect all tables for a given table_name across jobs and existing compact."""
    schema = TABLE_SCHEMAS[table_name]
    collected: list[pa.Table] = []

    # 1. Existing compacted table if present
    existing_compact = derived_root / COMPACT_DIRNAME / f"dt={dt}" / f"{table_name}.parquet"
    if existing_compact.is_file():
        t = _read_table_or_empty(existing_compact, table_name)
        if t.num_rows > 0:
            collected.append(t)

    # 2. Uncompacted job partitions
    if table_name == "jobs":
        for job in jobs:
            job_file = job.path / "jobs.parquet"
            if job_file.is_file():
                t = _read_table_or_empty(job_file, table_name)
                if t.num_rows > 0:
                    collected.append(t)
    else:
        for job in jobs:
            for trial_file in job.path.glob(f"trial_id=*/{table_name}.parquet"):
                if trial_file.is_file():
                    t = _read_table_or_empty(trial_file, table_name)
                    if t.num_rows > 0:
                        collected.append(t)

    if not collected:
        return [pa.Table.from_batches([], schema=schema)]
    return collected


def deduplicate_and_sort(table: pa.Table, table_name: str) -> pa.Table:
    """Deduplicate and sort deterministically by primary keys using DuckDB."""
    schema = TABLE_SCHEMAS[table_name]
    if table.num_rows == 0:
        return pa.Table.from_batches([], schema=schema)

    primary_keys = PRIMARY_KEYS[table_name]
    pk_cols = ", ".join(primary_keys)
    # Conflicting rows with the same key can arrive from an existing compact file
    # and a regenerated source partition. Select a canonical row rather than the
    # first input row so retention is independent of batch collection order.
    canonical_cols = ", ".join(f'"{name}"' for name in schema.names)

    con = duckdb.connect(database=":memory:")
    con.register("tbl", table)
    query = f"""
    SELECT *
    FROM tbl
    QUALIFY row_number() OVER (
        PARTITION BY {pk_cols}
        ORDER BY {canonical_cols}
    ) = 1
    ORDER BY {pk_cols}
    """
    res = con.execute(query).to_arrow_table()
    return res.cast(schema)


def write_compact_table(
    table: pa.Table,
    target_path: Path,
    table_name: str,
) -> int:
    """Write table to target_path atomically and validate row count & schema."""
    schema = TABLE_SCHEMAS[table_name]
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_suffix(".parquet.tmp")

    pq.write_table(
        table,
        temp_path,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
    )

    # Post-write validation
    try:
        written = pq.read_table(temp_path)
        if written.num_rows != table.num_rows:
            temp_path.unlink(missing_ok=True)
            raise CompactionValidationError(
                f"Row count mismatch for {table_name}: "
                f"expected {table.num_rows}, got {written.num_rows}"
            )
        if not written.schema.equals(schema):
            temp_path.unlink(missing_ok=True)
            raise CompactionValidationError(
                f"Schema integrity mismatch for {table_name}: {written.schema} != {schema}"
            )
        temp_path.replace(target_path)
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        if isinstance(exc, CompactionValidationError):
            raise
        raise CompactionValidationError(
            f"Failed to validate {table_name} post-write: {exc}"
        ) from exc

    return table.num_rows


# --------------------------------------------------------------------------- #
# Execution / Compaction Application
# --------------------------------------------------------------------------- #


def compact_day(
    derived_root: Path,
    day_plan: DayPlan,
    *,
    prune: bool = True,
) -> DayCompactionResult:
    """Compact all tables for a single day and optionally prune uncompacted jobs."""
    derived_root = derived_root.resolve()
    dt = day_plan.dt
    dest_dir = derived_root / COMPACT_DIRNAME / f"dt={dt}"

    table_row_counts: dict[str, int] = {}
    for table_name in PROJECTED_TABLE_NAMES:
        collected = _collect_table_batches(derived_root, day_plan.jobs, dt, table_name)
        merged = pa.concat_tables(collected, promote_options="default")
        deduped = deduplicate_and_sort(merged, table_name)
        target_file = dest_dir / f"{table_name}.parquet"
        rows = write_compact_table(deduped, target_file, table_name)
        table_row_counts[table_name] = rows

    # Pruning decision
    pruned_ids: list[str] = []
    retained_ids: list[str] = []

    if prune and day_plan.is_prunable:
        for job in day_plan.jobs:
            if job.path.is_dir():
                shutil.rmtree(job.path)
            pruned_ids.append(job.job_id)
    else:
        for job in day_plan.jobs:
            retained_ids.append(job.job_id)

    return DayCompactionResult(
        dt=dt,
        table_row_counts=table_row_counts,
        pruned_job_ids=tuple(pruned_ids),
        retained_job_ids=tuple(retained_ids),
    )


def compact(
    derived_root: Path | None = None,
    *,
    target_date: str | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    prune: bool = True,
    dry_run: bool = False,
    clock_today: date | None = None,
    runs_dir: Path | None = None,
    database_url: str | None = None,
) -> CompactionResult:
    """Main programmatic interface to execute deterministic Parquet compaction."""
    repo = runs_dir.parent if runs_dir else Path.cwd()
    root = derived_root or derived_root_from_environment(repo)
    plan = plan_compaction(
        root,
        target_date=target_date,
        retention_days=retention_days,
        clock_today=clock_today,
        runs_dir=runs_dir,
        database_url=database_url,
    )

    if dry_run:
        day_results: list[DayCompactionResult] = []
        total_rows: dict[str, int] = {tbl: 0 for tbl in PROJECTED_TABLE_NAMES}
        all_pruned: list[str] = []
        all_retained: list[str] = []
        for day in plan.days:
            if not day.is_closed:
                continue
            day_counts = {
                tbl: max(day.uncompacted_row_counts[tbl], day.existing_compact_row_counts[tbl])
                for tbl in PROJECTED_TABLE_NAMES
            }
            for tbl, cnt in day_counts.items():
                total_rows[tbl] += cnt
            if prune and day.is_prunable:
                all_pruned.extend(j.job_id for j in day.jobs)
                day_results.append(
                    DayCompactionResult(
                        dt=day.dt,
                        table_row_counts=day_counts,
                        pruned_job_ids=tuple(j.job_id for j in day.jobs),
                        retained_job_ids=(),
                    )
                )
            else:
                all_retained.extend(j.job_id for j in day.jobs)
                day_results.append(
                    DayCompactionResult(
                        dt=day.dt,
                        table_row_counts=day_counts,
                        pruned_job_ids=(),
                        retained_job_ids=tuple(j.job_id for j in day.jobs),
                    )
                )

        return CompactionResult(
            derived_root=root,
            compacted_days=tuple(day_results),
            total_compacted_rows=total_rows,
            pruned_jobs=tuple(all_pruned),
            retained_jobs=tuple(all_retained),
        )

    day_results = []
    total_rows = {tbl: 0 for tbl in PROJECTED_TABLE_NAMES}
    all_pruned = []
    all_retained = []
    errors: list[str] = []

    for day in plan.days:
        if not day.is_closed:
            continue
        try:
            day_res = compact_day(root, day, prune=prune)
            day_results.append(day_res)
            for tbl, rows in day_res.table_row_counts.items():
                total_rows[tbl] += rows
            all_pruned.extend(day_res.pruned_job_ids)
            all_retained.extend(day_res.retained_job_ids)
        except Exception as exc:
            errors.append(f"Failed to compact {day.dt}: {exc}")

    return CompactionResult(
        derived_root=root,
        compacted_days=tuple(day_results),
        total_compacted_rows=total_rows,
        pruned_jobs=tuple(all_pruned),
        retained_jobs=tuple(all_retained),
        errors=tuple(errors),
    )


# --------------------------------------------------------------------------- #
# CLI & Formatting
# --------------------------------------------------------------------------- #


def render_compaction_summary(result: CompactionResult) -> str:
    lines = ["parquet compaction"]
    lines.append(f"  derived_root: {result.derived_root}")
    lines.append(f"  compacted days: {len(result.compacted_days)}")
    for day in result.compacted_days:
        counts_str = " ".join(f"{k}={v}" for k, v in sorted(day.table_row_counts.items()))
        lines.append(f"    dt={day.dt}: {counts_str}")
        if day.pruned_job_ids:
            lines.append(f"      pruned granular partitions: {len(day.pruned_job_ids)} jobs")
        if day.retained_job_ids:
            lines.append(f"      retained granular partitions: {len(day.retained_job_ids)} jobs")

    total_str = " ".join(f"{k}={v}" for k, v in sorted(result.total_compacted_rows.items()))
    lines.append(f"  total compacted rows: {total_str}")
    lines.append(f"  total pruned jobs: {len(result.pruned_jobs)}")
    lines.append(f"  total retained jobs: {len(result.retained_jobs)}")
    for err in result.errors:
        lines.append(f"  ERROR: {err}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evallab.parquet_compaction",
        description="Deterministic Parquet compaction engine (WS-E item 4).",
    )
    subparsers = parser.add_subparsers(dest="command", required=False)

    compact_parser = subparsers.add_parser(
        "compact",
        help="compact closed-day data into derived/parquet/compact/dt=YYYY-MM-DD/",
    )
    for p in (parser, compact_parser):
        p.add_argument(
            "--target-date",
            type=str,
            default=None,
            help="compact only a specific date (YYYY-MM-DD)",
        )
        p.add_argument(
            "--derived-dir",
            "--out",
            "--parquet-dir",
            type=Path,
            default=None,
            help="override derived Parquet root (default: derived/parquet)",
        )
        p.add_argument(
            "--retention-days",
            type=int,
            default=DEFAULT_RETENTION_DAYS,
            help=f"days to retain granular partitions (default: {DEFAULT_RETENTION_DAYS})",
        )
        p.add_argument(
            "--no-prune",
            action="store_true",
            help="keep all granular partitions even for days older than retention window",
        )
        p.add_argument(
            "--dry-run",
            action="store_true",
            help="plan compaction without modifying filesystem",
        )
        p.add_argument(
            "--json",
            action="store_true",
            help="emit summary as JSON",
        )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        derived_root = (
            args.derived_dir.resolve()
            if args.derived_dir is not None
            else derived_root_from_environment(Path.cwd())
        )
    except Exception as exc:
        print(f"Error resolving derived root: {exc}", file=sys.stderr)
        return 1

    result = compact(
        derived_root=derived_root,
        target_date=args.target_date,
        retention_days=args.retention_days,
        prune=not args.no_prune,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(render_compaction_summary(result))

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
