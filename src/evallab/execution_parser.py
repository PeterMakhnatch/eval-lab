"""Direct run and matrix command registration."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from evallab.cli_support import _print_summary, _resolve

if TYPE_CHECKING:
    from evallab.fetch import HarborBackend


def _run_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.queue import Executor
    from evallab.results import load_job
    from evallab.runner import RunRequest

    request = RunRequest(
        task=_resolve(root, args.task),
        agent=args.agent,
        name=args.name,
        jobs_dir=_resolve(root, args.jobs_dir),
        environment=args.environment,
        egress_lock=args.egress_lock,
        model=args.model,
        concurrency=args.concurrency,
        attempts=args.attempts,
        timeout_seconds=args.timeout_seconds,
        allow_billable=args.allow_billable,
    )
    job_dir = Executor.from_repo(root).execute_direct(request)
    print(f"completed: {job_dir}")
    _print_summary([load_job(job_dir)])
    return 0


def _matrix_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.runner import load_matrix, run_matrix

    matrix_path = _resolve(root, args.path)
    matrix = load_matrix(matrix_path)
    execution = run_matrix(matrix, root=root, reuse_existing=args.reuse_existing, harbor=harbor)
    _print_summary(execution.completed)
    print(f"matrix receipt: {execution.receipt_path}")
    print(f"matrix invocations: {execution.invocation_path}")
    return execution.exit_code


def register_parser(commands: argparse._SubParsersAction) -> None:
    run = commands.add_parser("run", help="Run one explicitly named Harbor job")
    run.add_argument("--task", type=Path, required=True)
    run.add_argument("--agent", required=True)
    run.add_argument("--model")
    run.add_argument("--name", required=True)
    run.add_argument("--jobs-dir", type=Path, default=Path("runs"))
    run.add_argument("--environment", default="docker")
    run.add_argument(
        "--egress-lock",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Explicit deny-all backend lock; Docker controls use creation-time network=none",
    )
    run.add_argument("--concurrency", type=int, default=1)
    run.add_argument("--attempts", type=int, default=1)
    run.add_argument(
        "--timeout-seconds",
        type=int,
        default=1_800,
        help="executor wall-clock allowance per attempted trial",
    )
    run.add_argument(
        "--allow-billable",
        action="store_true",
        help=(
            "Record billable acknowledgement; direct 'evallab run' remains control-only, "
            "and paid agents must use the standing-policy queue"
        ),
    )
    run.set_defaults(func=_run_command)

    matrix = commands.add_parser("matrix", help="Run a checked-in JSON experiment matrix")
    matrix.add_argument("path", type=Path)
    matrix.add_argument(
        "--reuse-existing",
        action="store_true",
        help="Validate completed named jobs instead of refusing to reuse them",
    )
    matrix.set_defaults(func=_matrix_command)
