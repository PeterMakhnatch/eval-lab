from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from harbor_lab import __version__, database
from harbor_lab.queue import DirectoryQueue, Executor, read_spec
from harbor_lab.results import JobRecord, load_job, load_jobs
from harbor_lab.runner import (
    RunRequest,
    database_url_from_environment,
    expected_primary_reward,
    load_matrix,
    request_from_matrix,
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_local_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="harbor-lab",
        description="Run, inspect, and index Harbor evaluation experiments.",
    )
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)

    commands.add_parser("doctor", help="Check local Harbor, Docker, uv, and PostgreSQL")

    submit = commands.add_parser("submit", help="Validate and submit one experiment spec")
    submit.add_argument("path", type=Path)

    commands.add_parser("tick", help="Reconcile and drain the approved experiment queue")

    approve = commands.add_parser("approve", help="Approve one waiting experiment")
    approve.add_argument("spec_id")
    approve.add_argument("--actor", default="peter")

    reject = commands.add_parser("reject", help="Reject one queued experiment")
    reject.add_argument("spec_id")
    reject.add_argument("--actor", default="peter")
    reject.add_argument("--reason", required=True)

    commands.add_parser("stop", help="Stop dispatch after the current trial")
    commands.add_parser("resume", help="Remove the queue stop marker")

    run = commands.add_parser("run", help="Run one explicitly named Harbor job")
    run.add_argument("--task", type=Path, required=True)
    run.add_argument("--agent", required=True)
    run.add_argument("--model")
    run.add_argument("--name", required=True)
    run.add_argument("--jobs-dir", type=Path, default=Path("runs"))
    run.add_argument("--environment", default="docker")
    run.add_argument("--concurrency", type=int, default=1)
    run.add_argument("--attempts", type=int, default=1)
    run.add_argument(
        "--allow-billable",
        action="store_true",
        help="Acknowledge that the selected adapter/model may incur charges",
    )

    matrix = commands.add_parser("matrix", help="Run a checked-in JSON experiment matrix")
    matrix.add_argument("path", type=Path)
    matrix.add_argument(
        "--reuse-existing",
        action="store_true",
        help="Validate completed named jobs instead of refusing to reuse them",
    )

    summarize = commands.add_parser(
        "summarize", help="Print trial results directly from Harbor job directories"
    )
    summarize.add_argument("paths", type=Path, nargs="+", default=[Path("runs")])

    ingest = commands.add_parser("ingest", help="Upsert Harbor job metadata into PostgreSQL")
    ingest.add_argument("paths", type=Path, nargs="+", default=[Path("runs")])
    ingest.add_argument("--database-url")

    db = commands.add_parser("db", help="Manage the derived PostgreSQL index")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_init = db_commands.add_parser("init", help="Apply the idempotent schema")
    db_init.add_argument("--database-url")
    db_list = db_commands.add_parser("list", help="List recently ingested trials")
    db_list.add_argument("--database-url")
    db_list.add_argument("--limit", type=int, default=25)
    return root


def _resolve(root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _print_summary(jobs: Sequence[JobRecord]) -> None:
    print("| job | task | agent | model | reward | exception | seconds |")
    print("|---|---|---|---|---:|---|---:|")
    for job in jobs:
        for trial in job.trials:
            result = trial.result
            agent_info = result.get("agent_info") or {}
            model_info = agent_info.get("model_info") or {}
            exception = result.get("exception_info") or {}
            started = result.get("started_at")
            finished = result.get("finished_at")
            from harbor_lab.results import duration_seconds

            seconds = duration_seconds(started, finished)
            reward = "" if trial.primary_reward is None else f"{trial.primary_reward:g}"
            print(
                f"| {job.name} | {result.get('task_name', '')} | "
                f"{agent_info.get('name', '')} | "
                f"{model_info.get('name') or model_info.get('model_name') or 'adhoc'} | "
                f"{reward} | {exception.get('exception_type', '')} | "
                f"{'' if seconds is None else f'{seconds:.3f}'} |"
            )


def _doctor(root: Path) -> int:
    checks = Executor.from_repo(root).local_runtime_checks()

    database_url = database_url_from_environment()
    try:
        detail = database.ping(database_url)
        checks.append(("postgres", True, detail))
    except Exception as exc:  # Doctor should report all checks, not stop at the first.
        checks.append(("postgres", False, f"unavailable: {type(exc).__name__}"))

    checks.append(("task", (root / "tasks/event-summary/task.toml").is_file(), "event-summary"))
    for name, ok, detail in checks:
        print(f"{'ok' if ok else 'FAIL':4}  {name:14} {detail}")
    required = {"harbor", "docker", "docker-daemon", "uv", "task"}
    return 0 if all(ok for name, ok, _ in checks if name in required) else 1


def _run_command(args: argparse.Namespace, root: Path) -> int:
    request = RunRequest(
        task=_resolve(root, args.task),
        agent=args.agent,
        name=args.name,
        jobs_dir=_resolve(root, args.jobs_dir),
        environment=args.environment,
        model=args.model,
        concurrency=args.concurrency,
        attempts=args.attempts,
        allow_billable=args.allow_billable,
    )
    job_dir = Executor.from_repo(root).execute_direct(request)
    print(f"completed: {job_dir}")
    _print_summary([load_job(job_dir)])
    return 0


def _matrix_command(args: argparse.Namespace, root: Path) -> int:
    matrix_path = _resolve(root, args.path)
    matrix = load_matrix(matrix_path)
    completed: list[JobRecord] = []
    mismatch = False
    executor = Executor.from_repo(root)
    for run in matrix.runs:
        request = request_from_matrix(matrix, run, repo_root=root)
        job_dir = request.jobs_dir / request.name
        if args.reuse_existing and job_dir.is_dir():
            job = load_job(job_dir)
        else:
            job = load_job(executor.execute_direct(request))
        completed.append(job)
        expected = expected_primary_reward(run)
        if expected is not None:
            actual = job.trials[0].primary_reward if len(job.trials) == 1 else None
            if actual != expected:
                mismatch = True
                print(
                    f"expectation failed for {request.name}: expected {expected:g}, got {actual}",
                    file=sys.stderr,
                )
    _print_summary(completed)
    return 1 if mismatch else 0


def run_cli(argv: Sequence[str] | None = None) -> int:
    root = repo_root()
    load_local_env(root / ".env")
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return _doctor(root)
        if args.command == "submit":
            spec = read_spec(_resolve(root, args.path))
            path, decision = Executor.from_repo(root).submit(spec)
            print(f"{path.parent.name}: {path}")
            print(decision.message)
            return 0
        if args.command == "tick":
            count = Executor.from_repo(root).tick()
            print(f"dispatched {count} experiment(s)")
            return 0
        if args.command == "approve":
            path = DirectoryQueue(root / "queue").approve(args.spec_id, actor=args.actor)
            print(f"approved: {path}")
            return 0
        if args.command == "reject":
            path = DirectoryQueue(root / "queue").reject(
                args.spec_id, actor=args.actor, message=args.reason
            )
            print(f"rejected: {path}")
            return 0
        if args.command == "stop":
            DirectoryQueue(root / "queue").stop()
            print("queue stopped")
            return 0
        if args.command == "resume":
            DirectoryQueue(root / "queue").resume()
            print("queue resumed")
            return 0
        if args.command == "run":
            return _run_command(args, root)
        if args.command == "matrix":
            return _matrix_command(args, root)
        if args.command == "summarize":
            jobs = load_jobs([_resolve(root, path) for path in args.paths])
            if not jobs:
                print("No completed Harbor jobs found.", file=sys.stderr)
                return 1
            _print_summary(jobs)
            return 0
        if args.command == "ingest":
            jobs = load_jobs([_resolve(root, path) for path in args.paths])
            if not jobs:
                print("No completed Harbor jobs found.", file=sys.stderr)
                return 1
            url = database_url_from_environment(args.database_url)
            database.initialize(url)
            count = database.ingest(url, jobs, root=root)
            print(f"ingested {count} job(s)")
            return 0
        if args.command == "db" and args.db_command == "init":
            url = database_url_from_environment(args.database_url)
            database.initialize(url)
            print("database schema is current")
            return 0
        if args.command == "db" and args.db_command == "list":
            url = database_url_from_environment(args.database_url)
            rows = database.list_trials(url, limit=args.limit)
            print("| job | trial | task | agent | model | reward | exception | seconds |")
            print("|---|---|---|---|---|---:|---|---:|")
            for row in rows:
                print(
                    "| " + " | ".join("" if value is None else str(value) for value in row) + " |"
                )
            return 0
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
