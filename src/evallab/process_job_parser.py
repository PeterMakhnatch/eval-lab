"""Landed-job processing command registration."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evallab.fetch import HarborBackend


def _process_job_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.process_job import process_job

    try:
        report = process_job(
            args.job_dir,
            output_dir=args.output_dir,
            root=root,
            database_url=args.database_url,
            ingest=not args.no_ingest,
            publish=not args.no_publish,
            nop_runs_dir=args.nop_runs_dir,
            session_spend=args.session_spend,
            publication_card=args.publication_card,
            parquet_root=args.parquet_root,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        summary = report.get("summary") or {}
        print(f"job: {report['job_name']} ({summary.get('n_trials')} trials)")
        for row in report.get("trials") or []:
            flags = ", ".join(row.get("flags") or [])
            print(
                f"  {row['trial_name']}: reward={row.get('reward')} "
                f"stop={row.get('stop_reason')} "
                f"tokens={row.get('tokens_used')}/{row.get('tokens_attempted')} "
                f"cost={row.get('cost')} flags=[{flags}]"
            )
        print(f"ingest: {summary.get('ingest')}")
    return 0


def register_parser(commands: argparse._SubParsersAction) -> None:
    process_job_parser = commands.add_parser(
        "process-job",
        help="Process a landed Harbor job: ingest, detectors, run/job reports",
    )
    process_job_parser.add_argument("job_dir", type=Path, help="Landed Harbor job directory")
    process_job_parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Report output directory (default: <job>/processed/)",
    )
    process_job_parser.add_argument(
        "--parquet-root",
        type=Path,
        help="Rebuild existing ATIF/facts and trial features in this Parquet root",
    )
    process_job_parser.add_argument(
        "--no-ingest", action="store_true", help="Skip the catalog ingest"
    )
    process_job_parser.add_argument(
        "--database-url", default=None, help="PostgreSQL catalog URL override"
    )
    process_job_parser.add_argument(
        "--nop-runs-dir",
        default=None,
        help="Runs root holding same-task nop/qual trials for the grader cross-check",
    )
    process_job_parser.add_argument("--json", action="store_true", help="Emit report as JSON")
    process_job_parser.add_argument(
        "--session-spend",
        type=Path,
        default=None,
        help=(
            "Billed-session receipt JSON (evallab.session_spend/v1) for the "
            "allocated GPU share. Reprocessing after the bills land requires "
            "passing the receipt again."
        ),
    )
    process_job_parser.add_argument(
        "--publication-card",
        default=None,
        help="Explicit HAR issue attribution when the frozen job/spec has none; native inputs stay unchanged",
    )
    process_job_parser.set_defaults(func=_process_job_command)
    process_job_parser.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip publishing the job to the results home",
    )
