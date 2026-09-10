"""Coverage and reconciliation report across durable evidence stores (Data Engineer mission).

Reconciles disk, catalog, parquet, and ATIF stores:
- Counts native jobs, catalogued jobs, projected jobs, excepted jobs, and failed jobs.
- Categorizes trajectory and usage availability by agent kind.
- Tracks exclusion and gap reasons surfaced by verify_ingest.
- Provides resumable repair paths or unresolvable checkout markers for failing/excepted jobs.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from evallab.evidence.atif import (
    JOB_PROJECTION_FILE,
    _recorded_projection_exceptions_map,
)
from evallab.ingest_verify import (
    IGNORED_DIR_NAMES,
    ExcludedJob,
    IngestGap,
    IngestVerificationResult,
    _default_catalog_loader,
    _owned_by_nested_checkout,
    scan_disk_trials,
    verify_ingest,
)
from evallab.runner import database_url_from_environment
from evallab.storage.attach import TABLES
from evallab.storage.paths import (
    derived_root_from_environment,
    discover_parquet_partitions,
)

LIST_CAP = 50


@dataclass(frozen=True)
class JobCategorySummary:
    """Summary of a job category: total count, sample job names (capped), and truncation flag."""

    count: int
    jobs: tuple[str, ...]
    truncated: bool = False

    @property
    def names(self) -> tuple[str, ...]:
        return self.jobs

    def __getitem__(self, key: str) -> Any:
        if key == "names":
            return self.jobs
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key) from None

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "jobs": list(self.jobs),
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class AgentTrajectoryAvailability:
    """Trajectory and token-usage availability for one agent kind."""

    trials: int
    with_trajectory: int | bool
    with_usage: int | bool
    reason: str | None = None

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key) from None

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def as_dict(self) -> dict[str, Any]:
        return {
            "trials": self.trials,
            "with_trajectory": self.with_trajectory,
            "with_usage": self.with_usage,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RepairPathEntry:
    """Actionable repair path or unresolvable indicator for a failing/excepted job."""

    job_name: str
    reason: str
    resumable_command: str
    origin: str = "catalog"

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key) from None

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_name": self.job_name,
            "reason": self.reason,
            "resumable_command": self.resumable_command,
            "origin": self.origin,
        }

@dataclass(frozen=True)
class CoverageReport:
    """Complete reconciliation and coverage report across evidence stores."""

    native_jobs_present: JobCategorySummary
    catalogued: JobCategorySummary
    projected: JobCategorySummary
    excepted: JobCategorySummary
    failed: JobCategorySummary
    trajectory_availability_by_agent: dict[str, AgentTrajectoryAvailability]
    reasons: dict[str, int]
    repair_path: tuple[RepairPathEntry, ...]

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key) from None

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def as_dict(self) -> dict[str, Any]:
        return {
            "native_jobs_present": self.native_jobs_present.as_dict(),
            "catalogued": self.catalogued.as_dict(),
            "projected": self.projected.as_dict(),
            "excepted": self.excepted.as_dict(),
            "failed": self.failed.as_dict(),
            "trajectory_availability_by_agent": {
                agent: entry.as_dict()
                for agent, entry in self.trajectory_availability_by_agent.items()
            },
            "reasons": dict(self.reasons),
            "repair_path": [entry.as_dict() for entry in self.repair_path],
        }


def _find_checkout_hint(repo_root: Path, path_str: str) -> str:
    """Return owning-checkout hint for an evidence path that lives elsewhere."""
    raw_path = Path(path_str)
    candidate = raw_path if raw_path.is_absolute() else (repo_root / raw_path)
    curr = candidate
    for parent in [curr] + list(curr.parents):
        if (parent / ".git").exists():
            return str(parent)
    return path_str


def _read_job_name_from_parquet(job_dir: Path) -> str | None:
    """Read the canonical job_name string from jobs.parquet in a job directory."""
    jp = job_dir / JOB_PROJECTION_FILE
    if not jp.is_file():
        return None
    try:
        tbl = pq.read_table(jp, columns=["job_name"])
        if tbl.num_rows > 0:
            return str(tbl["job_name"][0].as_py())
    except Exception:
        pass
    return None


def build_coverage_report(
    *,
    root: Path,
    database_url: str | None = None,
    derived_root: Path | None = None,
    catalog_loader: Any = None,
    selected_job_ids: frozenset[str] | set[str] | None = None,
) -> CoverageReport:
    """Build a complete coverage and reconciliation report across all durable stores.

    Reuses existing data machinery:
    - ``evallab.ingest_verify.verify_ingest`` for disk/catalog/parquet/ATIF reconciliation.
    - ``evallab.storage.paths.discover_parquet_partitions`` for physical partition inventory.
    - ``evallab.storage.attach.TABLES`` for table inventory inspection.

    When ``selected_job_ids`` is set (UUID strings), recorded projection
    exceptions and their repair entries are restricted to the selection, so a
    scope-bound product never borrows unrelated jobs' exception counts.
    """
    resolved_root = Path(root).resolve()
    db_url = database_url or database_url_from_environment()
    d_root = (
        Path(derived_root).resolve()
        if derived_root is not None
        else derived_root_from_environment(resolved_root)
    )

    # 1. Run ingest completeness verification
    verification: IngestVerificationResult = verify_ingest(
        resolved_root,
        database_url=db_url,
        derived_root=d_root,
        catalog_loader=catalog_loader,
    )

    # 2. Disk scan for native jobs (root-wide inventory by default)
    projectable_trials, unprojectable_runs = scan_disk_trials(resolved_root)
    selected_dirs: set[Path] | None = None
    if selected_job_ids is not None:
        # Closed-world product: restrict disk-derived populations to the
        # selected jobs' evidence dirs, so unrelated root jobs never leak
        # into a selected-job completeness claim. Dirs resolve lazily below
        # once the scoped catalog is loaded.
        selected_dirs = set()
    disk_job_names = sorted({pt.parent.name for pt in projectable_trials})
    native_jobs_summary = JobCategorySummary(
        count=verification.disk_jobs_count or len(disk_job_names),
        jobs=tuple(disk_job_names[:LIST_CAP]),
        truncated=len(disk_job_names) > LIST_CAP,
    )

    # 3. Query catalog (jobs, trials) if database is accessible
    all_catalog_jobs: dict[str, dict[str, Any]] = {}
    all_catalog_trials: dict[str, dict[str, Any]] = {}
    if catalog_loader is not None:
        try:
            all_catalog_jobs, all_catalog_trials = catalog_loader(db_url)
        except Exception:
            pass
    elif db_url:
        try:
            import psycopg

            with psycopg.connect(db_url, connect_timeout=3) as conn, conn.cursor() as cur:
                cur.execute("SELECT id, job_name, evidence_path FROM jobs")
                for r in cur.fetchall():
                    all_catalog_jobs[str(r[0])] = {
                        "id": str(r[0]),
                        "name": str(r[1]),
                        "path": str(r[2]),
                    }
                cur.execute(
                    "SELECT id, job_id, trial_name, evidence_path, agent_name, input_tokens, output_tokens FROM trials"
                )
                for r in cur.fetchall():
                    all_catalog_trials[str(r[0])] = {
                        "id": str(r[0]),
                        "job_id": str(r[1]),
                        "name": str(r[2]),
                        "path": str(r[3]),
                        "agent_name": str(r[4] or "unknown"),
                        "input_tokens": r[5] or 0,
                        "output_tokens": r[6] or 0,
                    }
        except Exception:
            try:
                all_catalog_jobs, all_catalog_trials = _default_catalog_loader(db_url)
            except Exception:
                pass

    # Closed-world scoping: restrict disk-derived populations to the selected
    # jobs' evidence dirs, and surface finished on-disk jobs that have no
    # catalog row at all (e.g. rows deleted after projection). Both stay off
    # on the default root-wide path.
    not_cataloged: list[ExcludedJob] = []
    selected_ids: frozenset[str] = (
        frozenset(str(jid) for jid in selected_job_ids)
        if selected_job_ids is not None
        else frozenset()
    )
    if selected_job_ids is not None and selected_dirs is not None:
        for info in all_catalog_jobs.values():
            raw = Path(str(info.get("path") or ""))
            candidate = raw if raw.is_absolute() else resolved_root / raw
            try:
                selected_dirs.add(candidate.resolve())
            except OSError:
                continue

        def _under_selected(path: Path) -> bool:
            try:
                resolved = path.resolve()
            except OSError:
                return False
            return any(
                resolved == selected or selected in resolved.parents
                for selected in selected_dirs
            )

        projectable_trials = [pt for pt in projectable_trials if _under_selected(pt)]
        unprojectable_runs = [u for u in unprojectable_runs if _under_selected(u.path)]
        selected_names = sorted(
            {pt.parent.name for pt in projectable_trials}
            | {
                u.path.parent.name
                if (u.path.parent / "result.json").is_file()
                else u.path.name
                for u in unprojectable_runs
            }
        )
        native_jobs_summary = JobCategorySummary(
            count=len(selected_names),
            jobs=tuple(selected_names[:LIST_CAP]),
            truncated=len(selected_names) > LIST_CAP,
        )
        catalog_ids = set(all_catalog_jobs)
        for base in (resolved_root / "runs",):
            if not base.is_dir():
                continue
            for child in sorted(base.iterdir()):
                if not child.is_dir():
                    continue
                res_file = child / "result.json"
                if not res_file.is_file():
                    continue
                try:
                    payload = json.loads(res_file.read_text())
                except (OSError, ValueError):
                    continue
                if (
                    isinstance(payload, dict)
                    and payload.get("finished_at")
                    and "n_total_trials" in payload
                    and str(payload.get("id") or "") not in catalog_ids
                    and str(payload.get("id") or "") in selected_ids
                ):
                    not_cataloged.append(
                        ExcludedJob(
                            job_id=str(payload.get("id") or child.name),
                            name=child.name,
                            path=child.relative_to(resolved_root).as_posix()
                            if child.is_relative_to(resolved_root)
                            else str(child),
                            reason="not_cataloged",
                        )
                    )

    excluded_jobs_list: list[ExcludedJob] = list(verification.excluded_jobs)
    if catalog_loader is not None:
        def scope_reason(info: dict[str, Any]) -> str | None:
            raw_path = Path(str(info.get("path") or ""))
            candidate = raw_path if raw_path.is_absolute() else resolved_root / raw_path
            try:
                relative = candidate.resolve().relative_to(resolved_root)
            except ValueError:
                return "outside_checkout"
            if _owned_by_nested_checkout(resolved_root, relative):
                return "nested_checkout"
            if not candidate.exists():
                return "evidence_absent"
            return None

        for job_id, info in all_catalog_jobs.items():
            s_reason = scope_reason(info)
            if s_reason is not None and job_id not in {e.job_id for e in excluded_jobs_list}:
                excluded_jobs_list.append(
                    ExcludedJob(
                        job_id=job_id,
                        name=str(info.get("name") or job_id),
                        path=str(info.get("path") or ""),
                        reason=s_reason,
                    )
                )

    unfinished_jobs_list: list[ExcludedJob] = list(verification.unfinished_jobs)
    # Partial-intake jobs are accounted separately: their result.json still
    # lacks finished_at, so without this guard the disk scan below would
    # double-report them as unfinished.
    partial_jobs_list: list[ExcludedJob] = list(verification.partial_jobs)
    partial_names = {p.name for p in partial_jobs_list}
    known_unfinished_names = {u.name for u in unfinished_jobs_list}
    for base in [resolved_root / "runs", resolved_root / "research/evidence/runs"]:
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir() or child.name in IGNORED_DIR_NAMES:
                continue
            if selected_dirs is not None and not _under_selected(child):
                continue
            res_file = child / "result.json"
            if child.name in partial_names:
                continue
            if res_file.is_file() and child.name not in known_unfinished_names:
                try:
                    payload = json.loads(res_file.read_text())
                    if isinstance(payload, dict) and not payload.get("finished_at"):
                        rel_path = (
                            child.relative_to(resolved_root).as_posix()
                            if child.is_relative_to(resolved_root)
                            else str(child)
                        )
                        unfinished_jobs_list.append(
                            ExcludedJob(
                                job_id=str(payload.get("id") or child.name),
                                name=child.name,
                                path=rel_path,
                                reason="job_unfinished",
                            )
                        )
                        known_unfinished_names.add(child.name)
                except Exception:
                    pass

    excluded_job_ids = {e.job_id for e in excluded_jobs_list}
    retained_catalog_jobs = {
        job_id: info
        for job_id, info in all_catalog_jobs.items()
        if job_id not in excluded_job_ids
    }
    retained_catalog_names = sorted(
        {str(info.get("name") or job_id) for job_id, info in retained_catalog_jobs.items()}
    )
    # Distinction, not omission: `catalogued` describes the retained in-scope
    # set, so count and names always agree; jobs excluded with reasons live
    # under `excepted`, never silently dropped. (Previously count came from the
    # unfiltered verification total while names were retained-only, reporting
    # e.g. count=78 with 20 names and truncated=false.)
    catalogued_summary = JobCategorySummary(
        count=len(retained_catalog_names),
        jobs=tuple(retained_catalog_names[:LIST_CAP]),
        truncated=len(retained_catalog_names) > LIST_CAP,
    )

    # 4. Discover parquet partitions and projected jobs
    discovery = discover_parquet_partitions(d_root)
    available_tables = frozenset(p.table for p in discovery.partitions if p.table in TABLES)

    projected_names_set: set[str] = set()
    if selected_job_ids is not None:
        # Closed world: project exactly the selected IDs, never the root-wide
        # discovery fallback (which previously listed all 175 partitions for a
        # singleton scope).
        for jid in frozenset(str(j) for j in selected_job_ids):
            jd = d_root / f"job_id={jid}"
            if (jd / JOB_PROJECTION_FILE).is_file():
                name = _read_job_name_from_parquet(jd) or all_catalog_jobs.get(jid, {}).get("name") or jid
                if name:
                    projected_names_set.add(str(name))
    elif retained_catalog_jobs:
        for job_id, info in retained_catalog_jobs.items():
            if (d_root / f"job_id={job_id}" / JOB_PROJECTION_FILE).is_file():
                projected_names_set.add(str(info.get("name") or job_id))
    else:
        for jd in discovery.job_directories:
            if (jd / JOB_PROJECTION_FILE).is_file():
                name = _read_job_name_from_parquet(jd) or jd.name.removeprefix("job_id=")
                if name:
                    projected_names_set.add(name)
    projected_names = sorted(projected_names_set)
    projected_count = len(projected_names)
    projected_summary = JobCategorySummary(
        count=projected_count,
        jobs=tuple(projected_names[:LIST_CAP]),
        truncated=len(projected_names) > LIST_CAP,
    )

    # 5. Load recorded exceptions from events.jsonl
    ev_path = resolved_root / "queue/events.jsonl"
    recorded_exceptions = (
        _recorded_projection_exceptions_map(ev_path) if ev_path.is_file() else {}
    )
    if selected_job_ids is not None:
        selected_ids = frozenset(str(jid) for jid in selected_job_ids)
        recorded_exceptions = {
            job_id: reason
            for job_id, reason in recorded_exceptions.items()
            if str(job_id) in selected_ids
        }

    job_name_lookup: dict[str, str] = {
        job_id: str(info.get("name") or job_id)
        for job_id, info in all_catalog_jobs.items()
    }
    job_path_lookup: dict[str, str] = {
        job_id: str(info.get("path") or f"runs/{job_name_lookup.get(job_id, job_id)}")
        for job_id, info in all_catalog_jobs.items()
    }

    # 6. Excepted jobs (excluded + unfinished + accounted exceptions)
    excepted_names_set: set[str] = set()
    for e in excluded_jobs_list:
        excepted_names_set.add(e.name)
    for u in unfinished_jobs_list:
        excepted_names_set.add(u.name)
    for p in partial_jobs_list:
        excepted_names_set.add(p.name)
    for job_id in recorded_exceptions:
        excepted_names_set.add(job_name_lookup.get(job_id, job_id))
    for n in not_cataloged:
        excepted_names_set.add(n.name)
    excepted_names = sorted(excepted_names_set)
    excepted_summary = JobCategorySummary(
        count=len(excepted_names),
        jobs=tuple(excepted_names[:LIST_CAP]),
        truncated=len(excepted_names) > LIST_CAP,
    )

    # Filter gaps so excluded jobs (owned by other checkouts) are not counted as gaps
    excluded_job_names = {e.name for e in excluded_jobs_list}
    excluded_job_ids_set = {e.job_id for e in excluded_jobs_list}
    real_gaps = [
        gap
        for gap in verification.gaps
        if gap.name not in excluded_job_names and gap.entity_id not in excluded_job_ids_set
    ]

    # 7. Failed jobs (active gaps + crashed executions)
    failed_names_set: set[str] = set()
    for u in unprojectable_runs:
        if u.reason == "crashed_execution":
            name = u.path.parent.name if (u.path.parent / "result.json").is_file() else u.path.name
            # Partial-intake jobs already account their crashed trials under
            # partial_intake with a dedicated repair entry; listing them again
            # as failed would misread accounted state as a new failure.
            if name in partial_names:
                continue
            failed_names_set.add(name)

    failed_names = sorted(failed_names_set)
    failed_summary = JobCategorySummary(
        count=len(failed_names),
        jobs=tuple(failed_names[:LIST_CAP]),
        truncated=len(failed_names) > LIST_CAP,
    )

    # 8. Reasons mapping (surfaced by verify_ingest)
    reasons: dict[str, int] = {}
    for e in excluded_jobs_list:
        reasons[e.reason] = reasons.get(e.reason, 0) + 1

    if unfinished_jobs_list:
        reasons["job_unfinished"] = len(unfinished_jobs_list)
    if partial_jobs_list:
        reasons["partial_intake"] = len(partial_jobs_list)
    if not_cataloged:
        reasons["not_cataloged"] = len(not_cataloged)

    # verification.accounted_exceptions_by_reason is global to the events file;
    # under a selection, count the filtered map instead so the product borrows
    # no unrelated job's exceptions.
    if selected_job_ids is not None:
        scoped_exception_counts: Counter[str] = Counter(recorded_exceptions.values())
        exception_items = scoped_exception_counts.items()
    else:
        exception_items = verification.accounted_exceptions_by_reason.items()
    for err_type, count in exception_items:
        key = (
            err_type
            if err_type.startswith("projection_failed:")
            else f"projection_failed:{err_type}"
        )
        reasons[key] = reasons.get(key, 0) + count

    gap_counts = Counter(gap.reason for gap in real_gaps)
    for gap_reason, count in gap_counts.items():
        reasons[gap_reason] = reasons.get(gap_reason, 0) + count
    # 9. Repair path (capped at 50)
    repair_entries: list[RepairPathEntry] = []
    seen_repair_jobs: set[str] = set()
    catalog_unfinished_names = {u.name for u in verification.unfinished_jobs}

    def add_repair_entry(
        name: str,
        reason_code: str,
        resumable_cmd: str,
        origin: str = "catalog",
    ) -> None:
        if name in seen_repair_jobs or len(repair_entries) >= LIST_CAP:
            return
        seen_repair_jobs.add(name)
        repair_entries.append(
            RepairPathEntry(
                job_name=name,
                reason=reason_code,
                resumable_command=resumable_cmd,
                origin=origin,
            )
        )

    # 1. Active gaps
    for gap in real_gaps:
        p = job_path_lookup.get(gap.entity_id, f"runs/{gap.name}")
        cmd = f"evallab ingest {p}"
        add_repair_entry(gap.name, gap.reason, cmd, origin="catalog")

    # 2. Unfinished jobs (both cataloged and disk-only)
    for u in unfinished_jobs_list:
        cmd = (
            f"unfinishable-by-ingest: re-run the job to completion, "
            f"then evallab ingest {u.path}; or abandon"
        )
        origin = "catalog" if u.name in catalog_unfinished_names else "disk-only"
        add_repair_entry(u.name, u.reason, cmd, origin=origin)
    # 2c. Finished on-disk jobs with no catalog row (closed-world only)
    for n in not_cataloged:
        cmd = f"evallab ingest {n.path}"
        add_repair_entry(n.name, "not_cataloged", cmd, origin="disk-only")
    # 2b. Partial-intake jobs (accounted, queryable subset only)
    for p in partial_jobs_list:
        cmd = (
            f"already-intaked-partial: {d_root}/job_id={p.job_id}; "
            f"re-run the job to completion, then evallab ingest {p.path} "
            f"for full projection"
        )
        add_repair_entry(p.name, "partial_intake", cmd, origin="catalog")

    # 3. Checkout routing (outside or nested checkouts)
    for e in excluded_jobs_list:
        if e.reason in ("outside_checkout", "nested_checkout"):
            hint = _find_checkout_hint(resolved_root, e.path)
            cmd = f"unresolvable-from-this-checkout ({hint})"
            add_repair_entry(e.name, e.reason, cmd, origin="catalog")

    # 4. Accounted projection exceptions
    for job_id, err_type in recorded_exceptions.items():
        name = job_name_lookup.get(job_id, job_id)
        p = job_path_lookup.get(job_id, f"runs/{name}")
        reason_str = (
            err_type
            if err_type.startswith("projection_failed:")
            else f"projection_failed:{err_type}"
        )
        cmd = f"evallab ingest {p}"
        add_repair_entry(name, reason_str, cmd, origin="catalog")

    # 5. Other excluded jobs (e.g. absent evidence)
    for e in excluded_jobs_list:
        if e.reason not in ("outside_checkout", "nested_checkout"):
            cmd = f"evallab ingest {e.path}"
            add_repair_entry(e.name, e.reason, cmd, origin="catalog")
    # Collect trials and agent metadata
    retained_trials: dict[str, dict[str, Any]] = {}
    if all_catalog_trials:
        for t_id, trial_info in all_catalog_trials.items():
            if trial_info.get("job_id") not in excluded_job_ids:
                retained_trials[t_id] = {
                    "id": t_id,
                    "job_id": trial_info.get("job_id"),
                    "agent_name": str(trial_info.get("agent_name") or "unknown"),
                    "input_tokens": trial_info.get("input_tokens") or 0,
                    "output_tokens": trial_info.get("output_tokens") or 0,
                }

    # If no catalog trials, discover from trial_facts.parquet partitions
    if not retained_trials:
        for tf_path in discovery.table_files("trial_facts"):
            try:
                tbl = pq.read_table(tf_path)
                for row in tbl.to_pylist():
                    t_id = str(row.get("trial_id") or "")
                    if t_id:
                        retained_trials[t_id] = {
                            "id": t_id,
                            "job_id": str(row.get("job_id") or ""),
                            "agent_name": str(row.get("agent_name") or "unknown"),
                            "trajectory_count": row.get("trajectory_count") or 0,
                            "has_usage": (
                                (row.get("input_tokens") or 0) > 0
                                or (row.get("output_tokens") or 0) > 0
                                or (row.get("tool_call_count") or 0) > 0
                                or (row.get("llm_call_count") or 0) > 0
                            ),
                        }
            except Exception:
                pass

    # If still no trials, discover from disk trial configs
    if not retained_trials:
        for pt in projectable_trials:
            cfg = pt / "config.json"
            if cfg.is_file():
                try:
                    cdata = json.loads(cfg.read_text())
                    agent = str(cdata.get("agent", {}).get("name") or "unknown")
                    retained_trials[pt.name] = {
                        "id": pt.name,
                        "job_id": pt.parent.name,
                        "agent_name": agent,
                    }
                except Exception:
                    pass

    # Inspect physical partitions for trajectory and usage presence
    traj_trial_ids: set[str] = set()
    for p in discovery.partitions:
        if p.table == "trajectories" and p.trial_id:
            try:
                tbl = pq.read_table(p.path)
                if tbl.num_rows > 0:
                    traj_trial_ids.add(p.trial_id)
            except Exception:
                pass

    usage_trial_ids: set[str] = set()
    for p in discovery.partitions:
        if p.table in ("tool_usage", "llm_calls") and p.trial_id:
            try:
                tbl = pq.read_table(p.path)
                if tbl.num_rows > 0:
                    usage_trial_ids.add(p.trial_id)
            except Exception:
                pass

    # Group by agent kind
    agent_trials_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trial in retained_trials.values():
        agent_kind = trial["agent_name"]
        agent_trials_map[agent_kind].append(trial)

    trajectory_availability_by_agent: dict[str, AgentTrajectoryAvailability] = {}
    for agent_kind, trial_list in sorted(agent_trials_map.items()):
        trial_count = len(trial_list)
        if agent_kind.lower() in ("oracle", "nop"):
            trajectory_availability_by_agent[agent_kind] = AgentTrajectoryAvailability(
                trials=trial_count,
                with_trajectory=False,
                with_usage=False,
                reason="non_atif_agent_expected",
            )
        else:
            with_t = 0
            with_u = 0
            for t in trial_list:
                t_id = t.get("id")
                has_traj = (
                    (t_id is not None and t_id in traj_trial_ids)
                    or (t.get("trajectory_count") or 0) > 0
                )
                has_use = (
                    (t_id is not None and t_id in usage_trial_ids)
                    or t.get("has_usage", False)
                    or (t.get("input_tokens") or 0) > 0
                    or (t.get("output_tokens") or 0) > 0
                )
                if has_traj:
                    with_t += 1
                if has_use:
                    with_u += 1
            trajectory_availability_by_agent[agent_kind] = AgentTrajectoryAvailability(
                trials=trial_count,
                with_trajectory=with_t,
                with_usage=with_u,
                reason=None,
            )

    return CoverageReport(
        native_jobs_present=native_jobs_summary,
        catalogued=catalogued_summary,
        projected=projected_summary,
        excepted=excepted_summary,
        failed=failed_summary,
        trajectory_availability_by_agent=trajectory_availability_by_agent,
        reasons=reasons,
        repair_path=tuple(repair_entries),
    )


SCOPE_PRODUCT_VERSION = 1


def scoped_catalog_loader(
    base_loader: Any,
    job_ids: set[str] | frozenset[str],
) -> Any:
    """Return a catalog_loader exposing exactly ``job_ids``.

    Shapes are preserved verbatim; only membership is restricted. Trials whose
    job is not selected are dropped with their job. Pass the result as
    ``catalog_loader`` to ``build_coverage_report`` (or ``verify_ingest``) to
    bind a report to an exact job set instead of the whole shared catalog.
    """
    selected = frozenset(str(jid) for jid in job_ids)

    def load(database_url: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        jobs, trials = base_loader(database_url)
        kept_jobs = {jid: info for jid, info in jobs.items() if str(jid) in selected}
        kept_trials = {
            tid: info
            for tid, info in trials.items()
            if str(info.get("job_id")) in selected
        }
        # The minimal default loader omits agent/usage columns, which would
        # collapse the trajectory-availability dimension to "unknown". Enrich
        # from the existing trials columns; failure keeps unenriched rows.
        try:
            import psycopg

            with psycopg.connect(database_url, connect_timeout=3) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT id, agent_name, input_tokens, output_tokens FROM trials "
                    "WHERE job_id = ANY(%s)",
                    (sorted(selected),),
                )
                for raw_id, agent, inp, out in cur.fetchall():
                    info = kept_trials.get(str(raw_id))
                    if info is not None:
                        info["agent_name"] = agent
                        info["input_tokens"] = inp
                        info["output_tokens"] = out
        except Exception:
            pass
        return kept_jobs, kept_trials

    return load


def resolve_job_ids_by_name(
    database_url: str,
    names: set[str] | frozenset[str] | list[str],
) -> dict[str, str]:
    """Map catalog job names to job-id strings (names are the stable handle)."""
    import psycopg

    wanted = set(names)
    resolved: dict[str, str] = {}
    with psycopg.connect(database_url, connect_timeout=3) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, job_name FROM jobs")
        for raw_id, name in cur.fetchall():
            if str(name) in wanted:
                resolved[str(name)] = str(raw_id)
    return resolved


def summarize_binding(report: CoverageReport, *, job_name: str) -> str:
    """Derive a job's binding status from a built report (never hand-written).

    External-link status strings must describe the same state the coverage
    counts show; a literal written before generation (e.g. claiming
    not-cataloged while counts say 1/1) is exactly the failure this prevents.
    """
    projected = job_name in set(report.projected.jobs)
    catalogued = job_name in set(report.catalogued.jobs)
    excepted_reason: str | None = None
    for entry in report.repair_path:
        if entry.job_name == job_name:
            excepted_reason = entry.reason
            break
    if projected and catalogued:
        return "bound-catalogued-projected"
    if catalogued:
        return "bound-catalogued-unprojected"
    if excepted_reason is not None:
        return f"bound-excepted-{excepted_reason}"
    return "unbound-unknown"


def write_scope_bound_product(
    *,
    root: Path,
    derived_root: Path,
    database_url: str | None = None,
    job_ids: set[str] | frozenset[str] | list[str],
    external_links: list[dict[str, Any]] | None = None,
    out_dir: Path | None = None,
    wrong_root: Path | None = None,
    catalog_loader: Any = None,
) -> Path:
    """Write an immutable scope-bound coverage product via existing data APIs.

    Builds the report through ``build_coverage_report`` with a scoped loader,
    then emits canonical JSON (sorted keys, no timestamps) whose sha256 names
    the file: ``coverage-scope-<sha12>.json``. Rebuilding with the same inputs
    yields byte-identical output. The product also carries its own binding
    proof: the same job set evaluated against ``wrong_root`` (default: a fresh
    empty directory), where every selected job must land excluded with its
    reason preserved — proving the product is bound to these roots and borrows
    no unrelated job/root truth.
    """
    resolved_root = Path(root).resolve()
    d_root = Path(derived_root).resolve()
    db_url = database_url or database_url_from_environment()
    selected = frozenset(str(jid) for jid in job_ids)
    links = list(external_links or [])
    base_loader = catalog_loader if catalog_loader is not None else _default_catalog_loader
    report = build_coverage_report(
        root=resolved_root,
        database_url=db_url,
        derived_root=d_root,
        catalog_loader=scoped_catalog_loader(base_loader, selected),
        selected_job_ids=selected,
    )
    payload: dict[str, Any] = {
        "schema_version": SCOPE_PRODUCT_VERSION,
        "producer": "evallab.coverage_report.write_scope_bound_product",
        "source_root": str(resolved_root),
        "derived_root": str(d_root),
        "job_ids": sorted(selected),
        "coverage": report.as_dict(),
        "external_links": links,
    }
    if wrong_root is None:
        scratch = tempfile.TemporaryDirectory(prefix="evallab-wrong-root-")
        try:
            negative = build_coverage_report(
                root=Path(scratch.name).resolve(),
                database_url=db_url,
                derived_root=d_root,
                catalog_loader=scoped_catalog_loader(base_loader, selected),
                selected_job_ids=selected,
            )
            # Label, not the temp path: the path differs every run, and the
            # product must rebuild byte-identical. The proof is the exclusion
            # outcome, not the scratch location.
            payload["binding_proof"] = {
                "wrong_root": "<ephemeral-empty-dir>",
                "excluded": negative.as_dict()["excepted"]["count"],
                "reasons": negative.as_dict()["reasons"],
            }
        finally:
            scratch.cleanup()
    else:
        negative = build_coverage_report(
            root=Path(wrong_root).resolve(),
            database_url=db_url,
            derived_root=d_root,
            catalog_loader=scoped_catalog_loader(base_loader, selected),
            selected_job_ids=selected,
        )
        payload["binding_proof"] = {
            "wrong_root": str(Path(wrong_root).resolve()),
            "excluded": negative.as_dict()["excepted"]["count"],
            "reasons": negative.as_dict()["reasons"],
        }
    canonical = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    target = Path(out_dir).resolve() if out_dir is not None else d_root
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"coverage-scope-{digest[:12]}.json"
    path.write_text(canonical)
    return path
