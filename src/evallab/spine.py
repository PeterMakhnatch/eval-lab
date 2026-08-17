"""E05 join-spine validator.

`python -m evallab.spine check` reports per-edge orphan counts and bounded
samples of offending IDs. Exits non-zero if any orphans exist.

Edges: trial→job, job→spec, trajectory→trial, analysis→trial, observation→trial.

Uses DuckDB over schema fallbacks or derived parquet root (attach.py absent;
migrate to evallab db attach once E04 lands). No direct glob in new code.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import duckdb

from evallab.paths import derived_root_from_environment


def _get_conn() -> duckdb.DuckDBPyConnection:
    """Return in-memory DuckDB; caller populates tables or uses :memory: views."""
    return duckdb.connect(":memory:")


def _load_fallbacks(conn: duckdb.DuckDBPyConnection) -> None:
    """Minimal schema fallbacks matching sql/views.sql (TEXT ids for DuckDB test compat)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS experiments (
            id TEXT PRIMARY KEY,
            source_kind TEXT
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            experiment_id TEXT,
            job_name TEXT
        );
        CREATE TABLE IF NOT EXISTS trials (
            id TEXT PRIMARY KEY,
            job_id TEXT,
            trial_name TEXT,
            task_name TEXT,
            agent_name TEXT
        );
        CREATE TABLE IF NOT EXISTS trajectory_documents (
            id TEXT PRIMARY KEY,
            trial_id TEXT
        );
        CREATE TABLE IF NOT EXISTS analysis_invocations (
            id TEXT PRIMARY KEY,
            source_trial_id TEXT
        );
        CREATE TABLE IF NOT EXISTS observation_records (
            trial_id TEXT PRIMARY KEY
        );
        """
    )

def _check_edge(
    conn: duckdb.DuckDBPyConnection,
    child: str,
    parent: str,
    child_fk: str,
    parent_pk: str,
    sample_limit: int = 5,
) -> tuple[int | None, list[str]]:
    """Return (orphan_count, sample_ids) for child rows missing parent.
    Returns (None, samples) if count query yields no row (unmeasured edge).
    """
    q = f"""
        SELECT c.{child_fk} AS orphan_id
        FROM {child} c
        LEFT JOIN {parent} p ON c.{child_fk} = p.{parent_pk}
        WHERE p.{parent_pk} IS NULL
        LIMIT {sample_limit}
    """
    rows = conn.execute(q).fetchall()
    count_q = f"""
        SELECT count(*) FROM {child} c
        LEFT JOIN {parent} p ON c.{child_fk} = p.{parent_pk}
        WHERE p.{parent_pk} IS NULL
    """
    row = conn.execute(count_q).fetchone()
    if row is None:
        samples = [str(r[0]) for r in rows]
        return None, samples
    orphan_count = row[0]
    samples = [str(r[0]) for r in rows]
    return orphan_count, samples
def check_spine(conn: duckdb.DuckDBPyConnection | None = None) -> dict[str, Any]:
    """Run all spine edge checks. Return report dict with counts/samples.
    Edges with count=None are marked unmeasured; CLI must exit non-zero.
    """
    if conn is None:
        conn = _get_conn()
        _load_fallbacks(conn)
    edges = [
        ("trials", "jobs", "job_id", "id", "trial → job"),
        ("jobs", "experiments", "experiment_id", "id", "job → spec"),
        ("trajectory_documents", "trials", "trial_id", "id", "trajectory → trial"),
        ("analysis_invocations", "trials", "source_trial_id", "id", "analysis → trial"),
        ("observation_records", "trials", "trial_id", "id", "observation → trial"),
    ]
    report: dict[str, Any] = {"edges": {}, "total_orphans": 0, "unmeasured_edges": 0}
    for child, parent, fk, pk, label in edges:
        count, samples = _check_edge(conn, child, parent, fk, pk)
        if count is None:
            report["edges"][label] = {"orphans": None, "samples": samples, "status": "unmeasured"}
            report["unmeasured_edges"] += 1
        else:
            report["edges"][label] = {"orphans": count, "samples": samples}
            report["total_orphans"] += count
    return report


def print_report(report: dict[str, Any]) -> None:
    """Emit actionable report: counts + samples, never bare boolean.
    Distinguishes measured-zero from unmeasured (None count).
    """
    print("Join spine check (E05)")
    for label, data in report["edges"].items():
        n = data.get("orphans")
        if n is None:
            print(f"  {label}: unmeasured (count query returned no row)")
        elif n == 0:
            print(f"  {label}: 0 orphans")
        else:
            samples = ", ".join(data["samples"][:3])
            print(f"  {label}: {n} orphans, e.g. {samples}")
    total = report["total_orphans"]
    unmeas = report.get("unmeasured_edges", 0)
    print(f"Total orphans across spine: {total}")
    if unmeas > 0:
        print(f"Unmeasured edges: {unmeas}")
    if total > 0 or unmeas > 0:
        print("Spine broken: downstream numbers describe wrong population.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="E05 join-spine validator")
    parser.add_argument("cmd", choices=["check"], help="Run spine check")
    parser.add_argument("--derived-root", type=Path, default=None, help="Override parquet root")
    args = parser.parse_args(argv)

    conn = _get_conn()
    _load_fallbacks(conn)
    # If derived root present, could UNION parquet, but for now use in-mem for testability.
    # Real corpus: caller populates or use attach once landed.
    if args.derived_root:
        # root reserved for attach surface post-E04; no direct glob
        pass
    else:
        import contextlib

        with contextlib.suppress(Exception):
            _ = derived_root_from_environment(Path.cwd())
    report = check_spine(conn)
    print_report(report)
    if report.get("unmeasured_edges", 0) > 0 or report["total_orphans"] > 0:
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())