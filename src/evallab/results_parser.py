"""Summary and ingestion command registration."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from evallab.cli_support import _print_summary, _resolve

if TYPE_CHECKING:
    from evallab.fetch import HarborBackend


def _summarize_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.results import load_jobs

    jobs = load_jobs([_resolve(root, path) for path in args.paths])
    if not jobs:
        print("No completed Harbor jobs found.", file=sys.stderr)
        return 1
    _print_summary(jobs)
    return 0


def _ingest_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.evidence.atif import ingest_and_project
    from evallab.queue import DirectoryQueue, new_ulid, record_projection_failures
    from evallab.results import load_jobs
    from evallab.runner import database_url_from_environment
    from evallab.storage.paths import derived_root_from_environment

    jobs = load_jobs([_resolve(root, path) for path in args.paths])
    if not jobs:
        print("No completed Harbor jobs found.", file=sys.stderr)
        return 1
    url = database_url_from_environment(args.database_url)
    derived_root = derived_root_from_environment(
        root,
        explicit=args.derived_dir,
    )
    result = ingest_and_project(
        url,
        jobs,
        root=root,
        output_root=derived_root,
    )
    record_projection_failures(
        DirectoryQueue(root / "queue"),
        result,
        actor="manual-ingest",
        spec_id=f"system-{new_ulid()}",
    )
    from evallab.interpretation.trajectory_quality import (
        evaluate_trial_quality,
        persist_quality_ledger,
    )

    all_reports = []
    all_findings = []
    for job in jobs:
        for trial in job.trials:
            rep, findings = evaluate_trial_quality(
                trial.path,
                job.path,
                job_id_override=str(job.id),
                trial_id_override=str(trial.id),
            )
            all_reports.append(rep)
            all_findings.extend(findings)
    if all_reports:
        persist_quality_ledger(all_reports, all_findings, derived_root)

    print(f"ingested {result.cataloged_jobs} job(s)")
    for table, rows in sorted(result.row_counts.items()):
        print(f"{table}: {rows} row(s)")
    for failure in result.failures:
        print(f"projection failed: {failure.job_name} ({failure.error_type})")
    return 1 if result.failures else 0


def register_parser(commands: argparse._SubParsersAction) -> None:
    summarize = commands.add_parser(
        "summarize", help="Print trial results directly from Harbor job directories"
    )
    summarize.add_argument("paths", type=Path, nargs="+", default=[Path("runs")])
    summarize.set_defaults(func=_summarize_command)

    ingest = commands.add_parser("ingest", help="Upsert Harbor job metadata into PostgreSQL")
    ingest.add_argument("paths", type=Path, nargs="+", default=[Path("runs")])
    ingest.add_argument("--database-url")
    ingest.add_argument(
        "--derived-dir",
        type=Path,
        help="override the shared Parquet root for this invocation",
    )
    ingest.set_defaults(func=_ingest_command)
