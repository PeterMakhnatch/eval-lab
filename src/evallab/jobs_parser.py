from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from evallab.cli_support import _resolve

if TYPE_CHECKING:
    from evallab.fetch import HarborBackend


def _jobs_verify_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.job_verify import exit_code, render_text, verify_jobs
    from evallab.results import discover_job_dirs

    job_dirs = discover_job_dirs([_resolve(root, path) for path in args.paths])
    report = verify_jobs(job_dirs)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        if not job_dirs:
            print("No completed Harbor jobs found.", file=sys.stderr)
        print(render_text(report))
    return exit_code(report)


def register_parser(commands: argparse._SubParsersAction) -> None:
    jobs = commands.add_parser("jobs", help="Inspect finished Harbor job directories")
    job_commands = jobs.add_subparsers(dest="jobs_command", required=True)
    verify = job_commands.add_parser(
        "verify",
        help="Verify reward integrity of Harbor job dirs (exit 0 pass, 1 block, 2 error)",
    )
    verify.add_argument("paths", type=Path, nargs="+", default=[Path("runs")])
    verify.add_argument("--json", action="store_true", help="print the report as JSON")
    verify.set_defaults(func=_jobs_verify_command)
