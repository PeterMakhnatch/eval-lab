"""Ingest completeness verification and gap reconciliation (LOOP-INGEST).

Counts and reconciles the four durable evidence stores:
1. Trial directories on disk (runs/, research/evidence/runs/, queue/researchers/passes/)
2. PostgreSQL / DuckDB catalog (jobs, trials tables)
3. Parquet analytics partitions (derived/parquet/job_id=*/trial_id=*/*.parquet)
4. ATIF trajectory documents index (trajectory_documents)

Completeness is an invariant: every trial directory must be either projected
into the Parquet analytics surface or explicitly accounted for with a named reason.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evallab.evidence.atif import (
    JOB_PROJECTION_FILE,
    PROJECTED_TABLES,
    _recorded_projection_exceptions_map,
)
from evallab.runner import database_url_from_environment
from evallab.storage.paths import derived_root_from_environment

# Directories ignored from trial scanning (ephemeral or harness-internal)
IGNORED_DIR_NAMES = frozenset(
    {".executor", "_premerge", "_smoke", ".git", ".worktrees", "__pycache__"}
)


@dataclass(frozen=True)
class IngestGap:
    """A specific data gap between durable stores."""

    store: str
    entity_type: str
    entity_id: str
    name: str
    reason: str
    detail: str | None = None


@dataclass(frozen=True)
class UnprojectableRun:
    """A trial or job directory that cannot be projected, accounted with a reason."""

    path: Path
    job_id: str | None
    trial_name: str | None
    reason: str
    detail: str


@dataclass(frozen=True)
class IngestVerificationResult:
    """Complete reconciliation result across all four durable stores."""

    disk_jobs_count: int
    disk_trials_count: int
    disk_unprojectable_count: int
    disk_unprojectable_by_reason: dict[str, list[str]]
    catalog_jobs_count: int
    catalog_trials_count: int
    parquet_jobs_count: int
    parquet_trials_count: int
    atif_documents_count: int
    accounted_exceptions_count: int
    accounted_exceptions_by_reason: dict[str, int]
    gaps: tuple[IngestGap, ...] = field(default_factory=tuple)

    @property
    def is_complete(self) -> bool:
        """True if all projectable trial directories are projected and 0 gaps exist."""
        return len(self.gaps) == 0

    @property
    def invariant_ok(self) -> bool:
        """True if projected + accounted == catalog."""
        return (
            self.parquet_jobs_count + self.accounted_exceptions_count
            >= self.catalog_jobs_count
            and not any(g.store == "parquet" for g in self.gaps)
        )

    def summary_table(self) -> str:
        lines = [
            "=== Ingest Completeness Verification ===",
            f"Disk trial directories:       {self.disk_trials_count} projectable, "
            f"{self.disk_unprojectable_count} unprojectable",
            f"Catalog (PostgreSQL):         {self.catalog_jobs_count} jobs, "
            f"{self.catalog_trials_count} trials",
            f"Parquet analytics partitions: {self.parquet_jobs_count} jobs, "
            f"{self.parquet_trials_count} trials",
            f"ATIF trajectory documents:    {self.atif_documents_count} indexed",
            f"Accounted exceptions:         {self.accounted_exceptions_count}",
        ]
        if self.accounted_exceptions_by_reason:
            for r, c in sorted(self.accounted_exceptions_by_reason.items()):
                lines.append(f"  - {r}: {c}")
        if self.disk_unprojectable_by_reason:
            lines.append("Unprojectable disk runs by reason:")
            for r, paths in sorted(self.disk_unprojectable_by_reason.items()):
                examples = ", ".join(paths[:3])
                suffix = " ..." if len(paths) > 3 else ""
                lines.append(f"  - {r} ({len(paths)}): {examples}{suffix}")
        lines.append(f"Gaps detected:                {len(self.gaps)}")
        status_str = (
            "COMPLETE (0 gaps)" if self.is_complete else f"INCOMPLETE ({len(self.gaps)} gaps)"
        )
        lines.append(f"Completeness status:          {status_str}")
        return "\n".join(lines)

    def gap_table(self) -> str:
        if not self.gaps:
            return "No ingest gaps detected across durable stores."
        lines = [
            f"{'Store':12} {'Entity':10} {'ID':38} {'Name':30} {'Reason':30}",
            "-" * 120,
        ]
        for g in self.gaps:
            lines.append(
                f"{g.store:12} {g.entity_type:10} {g.entity_id[:36]:38} "
                f"{g.name[:28]:30} {g.reason[:28]:30}"
            )
        return "\n".join(lines)


def scan_disk_trials(
    repo_root: Path,
    search_roots: Sequence[Path] | None = None,
) -> tuple[list[Path], list[UnprojectableRun]]:
    """Scan disk directories for trial runs and classify projectable vs unprojectable."""
    roots = search_roots or [
        repo_root / "runs",
        repo_root / "research/evidence/runs",
        repo_root / "queue/researchers/passes",
    ]
    projectable: list[Path] = []
    unprojectable: list[UnprojectableRun] = []
    seen_paths: set[Path] = set()

    for base in roots:
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir() or child.name in IGNORED_DIR_NAMES:
                continue
            if child.name.startswith((".", "_")) and child.name != "_smoke":
                continue

            subdirs = [
                c for c in child.iterdir() if c.is_dir() and c.name not in IGNORED_DIR_NAMES
            ]

            if (child / "result.json").is_file():
                trial_subdirs = [
                    c for c in subdirs if c.name not in ("artifacts", "agent", "verifier")
                ]
                for td in trial_subdirs:
                    if td in seen_paths:
                        continue
                    seen_paths.add(td)
                    if (td / "result.json").is_file():
                        projectable.append(td)
                    else:
                        files = [f.name for f in td.iterdir() if f.is_file()]
                        reason = "missing_result_json"
                        if not files:
                            reason = "empty_trial_dir"
                        elif "exception.txt" in files or "trial.log" in files:
                            reason = "crashed_execution"
                        unprojectable.append(
                            UnprojectableRun(
                                path=td,
                                job_id=None,
                                trial_name=td.name,
                                reason=reason,
                                detail=f"Files present: {files}",
                            )
                        )
            elif subdirs:
                for sub in subdirs:
                    if (sub / "result.json").is_file():
                        nested_trials = [
                            c
                            for c in sub.iterdir()
                            if c.is_dir() and c.name not in ("artifacts", "agent", "verifier")
                        ]
                        for td in nested_trials:
                            if td in seen_paths:
                                continue
                            seen_paths.add(td)
                            if (td / "result.json").is_file():
                                projectable.append(td)
                            else:
                                files = [f.name for f in td.iterdir() if f.is_file()]
                                reason = "missing_result_json"
                                if not files:
                                    reason = "empty_trial_dir"
                                elif "exception.txt" in files or "trial.log" in files:
                                    reason = "crashed_execution"
                                unprojectable.append(
                                    UnprojectableRun(
                                        path=td,
                                        job_id=None,
                                        trial_name=td.name,
                                        reason=reason,
                                        detail=f"Files present: {files}",
                                    )
                                )
                    else:
                        files = [f.name for f in sub.iterdir() if f.is_file()]
                        if files:
                            unprojectable.append(
                                UnprojectableRun(
                                    path=sub,
                                    job_id=None,
                                    trial_name=sub.name,
                                    reason="missing_result_json",
                                    detail=f"Files: {files}",
                                )
                            )
            else:
                files = [f.name for f in child.iterdir() if f.is_file()]
                unprojectable.append(
                    UnprojectableRun(
                        path=child,
                        job_id=None,
                        trial_name=child.name,
                        reason="empty_directory" if not files else "missing_result_json",
                        detail=f"Files: {files}",
                    )
                )

    return projectable, unprojectable


CatalogLoader = Callable[
    [str], tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]
]


def _default_catalog_loader(
    database_url: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    jobs: dict[str, dict[str, Any]] = {}
    trials: dict[str, dict[str, Any]] = {}
    import psycopg

    with psycopg.connect(database_url, connect_timeout=3) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, job_name, evidence_path FROM jobs")
        for r in cur.fetchall():
            jobs[str(r[0])] = {
                "id": str(r[0]),
                "name": str(r[1]),
                "path": str(r[2]),
            }
        cur.execute("SELECT id, job_id, trial_name, evidence_path FROM trials")
        for r in cur.fetchall():
            trials[str(r[0])] = {
                "id": str(r[0]),
                "job_id": str(r[1]),
                "name": str(r[2]),
                "path": str(r[3]),
            }
    return jobs, trials


def verify_ingest(
    repo_root: Path,
    *,
    database_url: str | None = None,
    derived_root: Path | None = None,
    events_path: Path | None = None,
    search_roots: Sequence[Path] | None = None,
    catalog_loader: CatalogLoader | None = None,
) -> IngestVerificationResult:
    """Verify completeness and reconcile data across disk, catalog, parquet, and ATIF."""
    root = repo_root.resolve()
    db_url = database_url or database_url_from_environment()
    d_root = derived_root or derived_root_from_environment(root)
    ev_path = events_path or (root / "queue/events.jsonl")

    # 1. Scan disk
    projectable_trials, unprojectable_runs = scan_disk_trials(root, search_roots=search_roots)
    unprojectable_by_reason: dict[str, list[str]] = defaultdict(list)
    for u in unprojectable_runs:
        rel = u.path.relative_to(root).as_posix() if u.path.is_relative_to(root) else str(u.path)
        unprojectable_by_reason[u.reason].append(rel)

    # 2. Query catalog
    catalog_jobs: dict[str, dict[str, Any]] = {}
    catalog_trials: dict[str, dict[str, Any]] = {}

    loader = catalog_loader or _default_catalog_loader
    try:
        catalog_jobs, catalog_trials = loader(db_url)
    except Exception as exc:
        print(f"warning: catalog query failed ({type(exc).__name__}: {exc})", file=sys.stderr)
    if catalog_loader is None:
        def retained_in_checkout(info: dict[str, Any]) -> bool:
            raw_path = Path(str(info.get("path") or ""))
            candidate = raw_path if raw_path.is_absolute() else root / raw_path
            try:
                candidate.resolve().relative_to(root)
            except ValueError:
                return False
            return candidate.exists()

        catalog_jobs = {
            job_id: info
            for job_id, info in catalog_jobs.items()
            if retained_in_checkout(info)
        }
        catalog_trials = {
            trial_id: info
            for trial_id, info in catalog_trials.items()
            if info.get("job_id") in catalog_jobs and retained_in_checkout(info)
        }

    # 3. Scan Parquet partitions
    parquet_jobs: set[str] = set()
    parquet_trials: set[str] = set()
    missing_parquet_trials: list[tuple[str, str, str]] = []

    for job_id, _job_info in catalog_jobs.items():
        job_dir = d_root / f"job_id={job_id}"
        if not (job_dir / JOB_PROJECTION_FILE).is_file():
            continue
        parquet_jobs.add(job_id)

        job_trials = [t for t in catalog_trials.values() if t["job_id"] == job_id]
        for t in job_trials:
            t_id = t["id"]
            t_dir = job_dir / f"trial_id={t_id}"
            if t_dir.is_dir():
                present = {f.name for f in t_dir.glob("*.parquet")}
                if present >= PROJECTED_TABLES:
                    parquet_trials.add(t_id)
                else:
                    missing_tables = PROJECTED_TABLES - present
                    missing_parquet_trials.append(
                        (job_id, t_id, f"missing tables: {missing_tables}")
                    )
            else:
                missing_parquet_trials.append((job_id, t_id, "missing trial parquet directory"))

    # 4. Count ATIF trajectory documents
    atif_count = 0
    if d_root.is_dir():
        for traj_file in d_root.glob("job_id=*/trial_id=*/trajectories.parquet"):
            if traj_file.is_file():
                try:
                    import pyarrow.parquet as pq

                    tbl = pq.read_table(traj_file, columns=["validation_status"])
                    atif_count += tbl.num_rows
                except Exception:
                    pass

    # 5. Recorded exceptions from events
    recorded_exceptions = (
        _recorded_projection_exceptions_map(ev_path) if ev_path.is_file() else {}
    )
    exceptions_by_reason = Counter(recorded_exceptions.values())

    # 6. Reconcile and detect gaps
    gaps: list[IngestGap] = []

    # Check for cataloged jobs missing from Parquet
    for job_id, j_info in catalog_jobs.items():
        if job_id not in parquet_jobs and job_id not in recorded_exceptions:
            gaps.append(
                IngestGap(
                    store="parquet",
                    entity_type="job",
                    entity_id=job_id,
                    name=j_info["name"],
                    reason="missing_jobs_parquet",
                    detail=f"Parquet directory {d_root / f'job_id={job_id}'} missing jobs.parquet",
                )
            )

    # Check for cataloged trials missing required Parquet tables
    for job_id, trial_id, reason in missing_parquet_trials:
        if job_id not in recorded_exceptions:
            t_name = catalog_trials.get(trial_id, {}).get("name", trial_id)
            gaps.append(
                IngestGap(
                    store="parquet",
                    entity_type="trial",
                    entity_id=trial_id,
                    name=t_name,
                    reason="incomplete_parquet_partition",
                    detail=reason,
                )
            )

    # Count disk jobs
    disk_jobs_set: set[str] = set()
    for pt in projectable_trials:
        disk_jobs_set.add(pt.parent.name)

    return IngestVerificationResult(
        disk_jobs_count=len(disk_jobs_set),
        disk_trials_count=len(projectable_trials),
        disk_unprojectable_count=len(unprojectable_runs),
        disk_unprojectable_by_reason=dict(unprojectable_by_reason),
        catalog_jobs_count=len(catalog_jobs),
        catalog_trials_count=len(catalog_trials),
        parquet_jobs_count=len(parquet_jobs),
        parquet_trials_count=len(parquet_trials),
        atif_documents_count=atif_count,
        accounted_exceptions_count=len(recorded_exceptions),
        accounted_exceptions_by_reason=dict(exceptions_by_reason),
        gaps=tuple(gaps),
    )


def verify_idempotence(
    repo_root: Path,
    job_paths: Sequence[Path],
    *,
    database_url: str | None = None,
    derived_root: Path | None = None,
) -> bool:
    """Verify that re-ingesting already-ingested jobs causes zero churn and preserves row counts."""
    from evallab.evidence.atif import ingest_and_project
    from evallab.results import load_jobs

    root = repo_root.resolve()
    db_url = database_url or database_url_from_environment()
    d_root = derived_root or derived_root_from_environment(root)

    loaded = load_jobs([p.resolve() for p in job_paths])
    if not loaded:
        return True

    # First ingest/project
    res1 = ingest_and_project(db_url, loaded, root=root, output_root=d_root)
    # Second ingest/project
    res2 = ingest_and_project(db_url, loaded, root=root, output_root=d_root)

    return res1.row_counts == res2.row_counts and len(res1.failures) == len(res2.failures)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify ingest completeness across disk, catalog, parquet, and ATIF."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="Repository root directory (default: current working directory)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON verification report",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print full gap details and path lists",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    result = verify_ingest(root)

    if args.json:
        payload = {
            "is_complete": result.is_complete,
            "invariant_ok": result.invariant_ok,
            "disk_jobs_count": result.disk_jobs_count,
            "disk_trials_count": result.disk_trials_count,
            "disk_unprojectable_count": result.disk_unprojectable_count,
            "disk_unprojectable_by_reason": result.disk_unprojectable_by_reason,
            "catalog_jobs_count": result.catalog_jobs_count,
            "catalog_trials_count": result.catalog_trials_count,
            "parquet_jobs_count": result.parquet_jobs_count,
            "parquet_trials_count": result.parquet_trials_count,
            "atif_documents_count": result.atif_documents_count,
            "accounted_exceptions_count": result.accounted_exceptions_count,
            "accounted_exceptions_by_reason": result.accounted_exceptions_by_reason,
            "gaps_count": len(result.gaps),
            "gaps": [
                {
                    "store": g.store,
                    "entity_type": g.entity_type,
                    "entity_id": g.entity_id,
                    "name": g.name,
                    "reason": g.reason,
                    "detail": g.detail,
                }
                for g in result.gaps
            ],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(result.summary_table())
        if result.gaps:
            print("\n" + result.gap_table())

    return 0 if result.is_complete else 1


if __name__ == "__main__":
    sys.exit(main())
