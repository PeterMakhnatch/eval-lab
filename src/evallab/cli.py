from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path
from uuid import UUID

from evallab import __version__, database
from evallab.automation import (
    GuardedTick,
    HeadlessDoctor,
    NightlyCycle,
    ScheduleInstaller,
    record_quarantine,
    record_researcher_deferral,
)
from evallab.calibrate import (
    dispatch_approved_codex_calibration,
    dspy_split_summary,
    evaluate_predictions,
    load_prediction_bundle,
    make_stub_bundle,
    stage_queue_bundle,
    write_calibration_record,
    write_catalog_record,
)
from evallab.canary import CanaryEnqueuer, TerminalBenchCanaryImporter
from evallab.cohort import index_comparison_associations, write_comparison
from evallab.digest import DigestRenderer
from evallab.facts import (
    AnalyzerCallResult,
    analysis_plan,
    ingest_analysis_sidecar,
    ingest_catalog,
    load_analysis_source,
    rebuild_from_raw,
    run_trial_analysis,
    write_analysis_review,
    write_failure_taxonomy_agreement,
)
from evallab.fetch import (
    FetchError,
    FetchService,
    HarborBackend,
    SubprocessHarbor,
    format_audit,
)
from evallab.queue import DirectoryQueue, Executor, load_policy, read_spec
from evallab.researchers import ResearcherLoop
from evallab.results import JobRecord, load_job, load_jobs
from evallab.runner import (
    RunRequest,
    database_url_from_environment,
    expected_primary_reward,
    load_matrix,
    request_from_matrix,
)
from evallab.tracing import (
    TraceError,
    format_batch,
    instrument_openinference,
    trace_completed_jobs,
    trace_path,
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
        prog="evallab",
        description="Run, inspect, and analyze agent evaluations through Harbor.",
    )
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="Check local Harbor, Docker, uv, and PostgreSQL")
    doctor.add_argument(
        "--headless",
        action="store_true",
        help="Fail closed and print only boolean prerequisite status as JSON",
    )

    dashboard = commands.add_parser("dashboard", help="Open the read-only research overview")
    dashboard.add_argument("--address", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8501)
    dashboard.add_argument("--database-url")

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

    schedule = commands.add_parser("schedule", help="Manage unattended launchd schedules")
    schedule_commands = schedule.add_subparsers(dest="schedule_command", required=True)
    schedule_commands.add_parser("install", help="Install and load tick/nightly LaunchAgents")

    digest = commands.add_parser("digest", help="Render one daily digest from catalog and events")
    digest.add_argument("--date", dest="report_date", type=date.fromisoformat)

    nightly = commands.add_parser("nightly", help="Run the fail-closed unattended nightly cycle")
    nightly.add_argument("--date", dest="report_date", type=date.fromisoformat)

    research = commands.add_parser(
        "research",
        help="Run one guarded analyst/synthesizer/proposer pass",
    )
    research.add_argument("--date", dest="report_date", type=date.fromisoformat)

    canary = commands.add_parser("canary", help="Manage version-pinned nightly canaries")
    canary_commands = canary.add_subparsers(dest="canary_command", required=True)
    import_task = canary_commands.add_parser(
        "import-terminal-bench",
        help="Import one task through an immutable Harbor dataset download",
    )
    import_task.add_argument("--dataset-ref", required=True)
    import_task.add_argument("--task-name", required=True)
    import_task.add_argument("--destination", type=Path, required=True)

    calibrate = commands.add_parser(
        "calibrate", help="Measure a judge against one sealed calibration family"
    )
    calibrate.add_argument(
        "family", choices=("checkout-pool-exhaustion", "retry-storm-backlog")
    )
    calibration_mode = calibrate.add_mutually_exclusive_group()
    calibration_mode.add_argument("--predictions", type=Path)
    calibration_mode.add_argument("--stub", action="store_true")
    calibration_mode.add_argument("--stage", choices=("codex", "anthropic"))
    calibration_mode.add_argument("--dispatch-approved", metavar="SPEC_ID")
    calibration_mode.add_argument("--dspy-dry-run", action="store_true")
    calibrate.add_argument("--judge-model")
    calibrate.add_argument("--est-cost-usd", type=float, default=2.75)
    calibrate.add_argument("--date", dest="calibration_date", type=date.fromisoformat)
    calibrate.add_argument("--records-dir", type=Path)
    calibrate.add_argument("--prediction-artifact")
    calibrate.add_argument("--pending-backend", action="append", default=[])
    calibrate.add_argument("--database-url")
    calibrate.add_argument("--skip-catalog", action="store_true")

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
    ingest.add_argument("--derived-dir", type=Path, default=Path("derived/parquet"))

    trajectories = commands.add_parser(
        "trajectories",
        help="Validate ATIF and optionally rebuild deterministic Parquet facts",
    )
    trajectories.add_argument(
        "paths",
        type=Path,
        nargs="*",
        default=[Path("runs"), Path("research/evidence/runs"), Path("evidence/runs")],
    )
    trajectories.add_argument(
        "--export",
        action="store_true",
        help="Write trajectory and trial facts to partitioned Parquet",
    )
    trajectories.add_argument("--output-dir", type=Path, default=Path("derived/parquet"))

    compare = commands.add_parser("compare", help="Compare declared trial cohorts")
    compare.add_argument("path", type=Path)
    compare.add_argument("--output-dir", type=Path, default=Path("derived/comparisons"))
    compare.add_argument("--index", action="store_true")
    compare.add_argument("--database-url")

    analyze = commands.add_parser("analyze", help="Plan or index bounded trial analyses")
    analyze_commands = analyze.add_subparsers(dest="analyze_command", required=True)
    analyze_plan_parser = analyze_commands.add_parser(
        "plan", help="Show a no-call stage-5 analysis plan"
    )
    analyze_plan_parser.add_argument("path", type=Path)
    analyze_plan_parser.add_argument("--agent", default="codex")
    analyze_plan_parser.add_argument("--agent-version", default="local")
    analyze_plan_parser.add_argument("--model", default="configured-by-queue")
    analyze_plan_parser.add_argument(
        "--output-dir", type=Path, default=Path("derived/analyses")
    )
    analyze_stub = analyze_commands.add_parser(
        "stub", help="Validate a saved response and write an immutable sidecar"
    )
    analyze_stub.add_argument("path", type=Path)
    analyze_stub.add_argument("--response", type=Path, required=True)
    analyze_stub.add_argument("--output-dir", type=Path, default=Path("derived/analyses"))
    analyze_stub.add_argument("--index", action="store_true")
    analyze_stub.add_argument("--database-url")
    analyze_ingest = analyze_commands.add_parser(
        "ingest-sidecar", help="Index one durable analysis sidecar"
    )
    analyze_ingest.add_argument("path", type=Path)
    analyze_ingest.add_argument("--database-url")
    analyze_review = analyze_commands.add_parser(
        "review", help="Append a human review without editing the analysis"
    )
    analyze_review.add_argument("path", type=Path)
    analyze_review.add_argument(
        "--disposition",
        required=True,
        choices=("accepted", "needs_revision", "rejected", "superseded"),
    )
    analyze_review.add_argument("--rationale", required=True)
    analyze_review.add_argument("--reviewer", required=True)
    analyze_review.add_argument("--superseded-by")
    analyze_agreement = analyze_commands.add_parser(
        "agreement", help="Compare valid analysis categories with fixed labels"
    )
    analyze_agreement.add_argument("paths", type=Path, nargs="+")
    analyze_agreement.add_argument(
        "--labels",
        type=Path,
        default=Path("research/calibration/trajectory-labels"),
    )
    analyze_agreement.add_argument(
        "--output",
        type=Path,
        default=Path("derived/analysis/failure-taxonomy-agreement.json"),
    )

    db = commands.add_parser("db", help="Manage the derived PostgreSQL index")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_init = db_commands.add_parser("init", help="Apply the idempotent schema")
    db_init.add_argument("--database-url")
    db_list = db_commands.add_parser("list", help="List recently ingested trials")
    db_list.add_argument("--database-url")
    db_list.add_argument("--limit", type=int, default=25)

    trace = commands.add_parser(
        "trace",
        help="Convert ATIF trajectories to OTel and ship them to Phoenix",
    )
    trace.add_argument("path", type=Path, help="Trial directory, job directory, or trajectory.json")
    trace.add_argument(
        "--endpoint",
        default=None,
        help="Phoenix collector base URL (default: PHOENIX_COLLECTOR_ENDPOINT or http://127.0.0.1:6006)",
    )
    trace.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and convert only; do not POST OTLP",
    )
    trace.add_argument(
        "--include-controls",
        action="store_true",
        help="Also trace oracle/nop control trials",
    )

    fetch = commands.add_parser(
        "fetch",
        help="Acquire a pinned Harbor Hub dataset into library/benchmarks/",
    )
    fetch.add_argument(
        "ref",
        nargs="?",
        help="Pinned name@version (never @latest or other unpinned refs)",
    )
    fetch.add_argument(
        "--list",
        dest="fetch_list",
        action="store_true",
        help="Show fetchable Hub pins and named adapter lanes",
    )
    fetch.add_argument(
        "--audit",
        dest="fetch_audit",
        action="store_true",
        help="Re-verify digests of every library/benchmarks ingest",
    )
    fetch.add_argument(
        "--verify-sample",
        type=int,
        default=0,
        metavar="N",
        help="Run free oracle/nop on N tasks (Harbor -n <= 2) and record rewards",
    )
    return root


def _fetch_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None
) -> int:
    service = FetchService(
        root=root,
        harbor=harbor if harbor is not None else SubprocessHarbor(),
    )
    if args.fetch_list:
        print("\n".join(service.list_lines()))
        return 0
    if args.fetch_audit:
        rows = service.audit()
        print(format_audit(rows), end="")
        return 0 if all(row.status == "pass" for row in rows) else 1
    if not args.ref:
        raise FetchError("provide name@version, or --list / --audit")
    result = service.fetch(args.ref, verify_sample=args.verify_sample)
    print(f"{result.status}: {result.message}")
    if result.manifest_path is not None:
        print(f"manifest: {result.manifest_path}")
    return 0


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
            from evallab.results import duration_seconds

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

    task_toml = root / "library/tasks/event-summary/task.toml"
    checks.append(("task", task_toml.is_file(), "event-summary"))
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


def _dashboard_command(args: argparse.Namespace, root: Path) -> int:
    environment = os.environ.copy()
    if args.database_url:
        environment["DATABASE_URL"] = args.database_url
    python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(root) + (os.pathsep + python_path if python_path else "")
    try:
        completed = subprocess.run(
            [
                "uv",
                "run",
                "--with",
                "streamlit==1.61.1",
                "streamlit",
                "run",
                str(root / "dashboard/app.py"),
                f"--server.address={args.address}",
                f"--server.port={args.port}",
                "--server.headless=true",
                "--browser.gatherUsageStats=false",
            ],
            cwd=root,
            env=environment,
            check=False,
        )
    except KeyboardInterrupt:
        return 130
    return completed.returncode


def _calibrate_command(args: argparse.Namespace, root: Path) -> int:
    if args.dispatch_approved:
        readiness, dispatched = dispatch_approved_codex_calibration(
            root, args.family, args.dispatch_approved
        )
        print(json.dumps({"healthy": readiness.healthy, **readiness.__dict__}, indent=2))
        print(f"dispatched: {dispatched}")
        return 0
    if args.dspy_dry_run:
        print(json.dumps(dspy_split_summary(root, args.family), indent=2))
        return 0
    if args.predictions is None and not args.stub:
        staged = stage_queue_bundle(
            root,
            args.family,
            backend=args.stage or "codex",
            judge_model=args.judge_model,
            est_cost_usd=args.est_cost_usd,
            run_date=args.calibration_date,
        )
        print(f"task: {staged.task_path}")
        print(f"queue spec: {staged.spec_path}")
        print(f"submit with: uv run evallab submit {staged.spec_path.relative_to(root)}")
        return 0

    if args.stub:
        bundle = make_stub_bundle(root, args.family)
        prediction_artifact = args.prediction_artifact or "stub://deterministic-all-no"
        status = "stub"
    else:
        prediction_path = _resolve(root, args.predictions)
        bundle = load_prediction_bundle(prediction_path)
        if bundle.family != args.family:
            raise ValueError(
                f"prediction family {bundle.family!r} does not match CLI family {args.family!r}"
            )
        if args.judge_model:
            bundle = bundle.model_copy(update={"judge_model": args.judge_model})
        prediction_artifact = (
            args.prediction_artifact or prediction_path.relative_to(root).as_posix()
        )
        status = "measured"
    record = evaluate_predictions(
        root,
        bundle,
        prediction_artifact=prediction_artifact,
        evaluated_on=args.calibration_date,
        status=status,
        pending_backends=args.pending_backend,
    )
    if args.records_dir is not None:
        records_root = _resolve(root, args.records_dir)
    elif status == "stub":
        records_root = root / "queue/stub-calibration-records"
    else:
        records_root = None
    record_path = write_calibration_record(root, record, records_root=records_root)
    if status == "measured" and not args.skip_catalog:
        write_catalog_record(record, record_path, database_url=args.database_url)
    print(f"record: {record_path}")
    print(f"mean agreement: {record.mean_agreement:.4f}")
    print(f"reportable: {'yes' if record.reportable else 'no (stub)'}")
    print(f"meets {record.agreement_floor:.2f} floor: {'yes' if record.meets_floor else 'no'}")
    return 0


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    workspace: Path | None = None,
    harbor: HarborBackend | None = None,
) -> int:
    root = workspace if workspace is not None else repo_root()
    load_local_env(root / ".env")
    args = parser().parse_args(argv)
    instrument_openinference()
    try:
        if args.command == "fetch":
            return _fetch_command(args, root, harbor=harbor)
        if args.command == "doctor" and args.headless:
            executor = Executor.from_repo(root)
            report = HeadlessDoctor(root, executor=executor).run()
            print(report.model_dump_json(indent=2))
            return 0 if report.healthy else 1
        if args.command == "doctor":
            return _doctor(root)
        if args.command == "dashboard":
            return _dashboard_command(args, root)
        if args.command == "submit":
            spec = read_spec(_resolve(root, args.path))
            path, decision = Executor.from_repo(root).submit(spec)
            print(f"{path.parent.name}: {path}")
            print(decision.message)
            return 0
        if args.command == "tick":
            executor = Executor.from_repo(root)
            result = GuardedTick(
                doctor=HeadlessDoctor(root, executor=executor),
                executor=executor,
            ).run()
            print(f"dispatched {result.dispatched} experiment(s)")
            print(f"quarantined: {'no' if result.report.healthy else 'yes'}")
            return 0 if result.report.healthy else 1
        if args.command == "canary" and args.canary_command == "import-terminal-bench":
            executor = Executor.from_repo(root)
            imported = TerminalBenchCanaryImporter(
                executor=executor,
                repo_root=root,
            ).import_task(
                dataset_ref=args.dataset_ref,
                task_name=args.task_name,
                destination=_resolve(root, args.destination),
            )
            print(f"imported: {imported}")
            return 0
        if args.command == "calibrate":
            return _calibrate_command(args, root)
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
        if args.command == "schedule" and args.schedule_command == "install":
            paths = ScheduleInstaller(root).install()
            for path in paths:
                print(f"installed: {path}")
            return 0
        if args.command == "digest":
            report_date = args.report_date or date.today()
            path = _digest_renderer(root).write(report_date=report_date)
            ResearcherLoop.from_repo(root).enrich_digest(path, report_date)
            print(f"digest: {path}")
            return 0
        if args.command == "research":
            report_date = args.report_date or date.today()
            executor = Executor.from_repo(root)
            report = HeadlessDoctor(root, executor=executor).run()
            if not report.healthy:
                record_quarantine(
                    executor.queue,
                    event="researcher_quarantined",
                    report=report,
                    actor="manual-researcher",
                )
                print("researcher pass quarantined by headless doctor")
                return 1
            if not report.checks.codex_auth_present:
                record_researcher_deferral(
                    executor.queue,
                    report_date=report_date,
                    actor="manual-researcher",
                    reason="missing_credential:codex",
                )
                print("researcher pass deferred: missing Codex credential")
                return 0
            result = ResearcherLoop.from_repo(root).run(report_date=report_date)
            print(f"pass: {result.pass_id}")
            print(f"invocations: {result.invocation_count}")
            print(f"attributed cost: ${result.attributed_cost_usd:.2f}")
            if result.proposal_path is not None:
                print(f"proposal: {result.proposal_path}")
            if result.deferred_reason:
                print(f"deferred: {result.deferred_reason}")
            if result.failed_reason:
                print(f"failed: {result.failed_reason}")
                return 1
            return 0
        if args.command == "nightly":
            executor = Executor.from_repo(root)
            researcher_loop: ResearcherLoop | None = None

            def get_researcher_loop() -> ResearcherLoop:
                nonlocal researcher_loop
                if researcher_loop is None:
                    researcher_loop = ResearcherLoop.from_repo(root)
                return researcher_loop

            result = NightlyCycle(
                doctor=HeadlessDoctor(root, executor=executor),
                executor=executor,
                renderer=_digest_renderer(root),
                canary_enqueuer=CanaryEnqueuer.from_repo(root, executor).enqueue,
                researcher_pass=lambda day: get_researcher_loop().run(
                    report_date=day
                ).invocation_count,
                digest_enricher=lambda path, day: get_researcher_loop().enrich_digest(
                    path, day
                ),
            ).run(report_date=args.report_date)
            print(f"digest: {result.digest_path}")
            print(f"enqueued: {result.enqueued}")
            print(f"dispatched: {result.dispatched}")
            print(
                "researcher invocations: "
                f"{getattr(result, 'researcher_invocations', 0)}"
            )
            print(f"quarantined: {'yes' if result.quarantined else 'no'}")
            try:
                print(
                    format_batch(
                        trace_completed_jobs(
                            root / "runs",
                            include_controls=False,
                            dry_run=False,
                        )
                    )
                )
            except TraceError as exc:
                print(f"trace skipped: {exc}")
            return 1 if result.quarantined else 0
        if args.command == "trace":
            batch = trace_path(
                _resolve(root, args.path),
                endpoint=args.endpoint,
                dry_run=args.dry_run,
                include_controls=args.include_controls,
            )
            print(format_batch(batch))
            if batch.failed:
                return 1
            if not batch.shipped and not args.dry_run:
                return 1
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
            derived_root = _resolve(root, args.derived_dir)
            rebuild_from_raw(jobs, derived_root)
            ingest_catalog(url, jobs, root=root, derived_root=derived_root)
            print(f"ingested {count} job(s)")
            return 0
        if args.command == "trajectories":
            from evallab.atif import project_trial

            jobs = load_jobs([_resolve(root, path) for path in args.paths])
            if not jobs:
                print("No completed Harbor jobs found.", file=sys.stderr)
                return 1
            print("| job | trial | status | documents | steps | tools |")
            print("|---|---|---|---:|---:|---:|")
            for job in jobs:
                for trial in job.trials:
                    projection = project_trial(job, trial)
                    statuses = {item.validation_status for item in projection.trajectories}
                    status = (
                        "none"
                        if not statuses
                        else "invalid"
                        if "invalid" in statuses
                        else "unsupported"
                        if "unsupported" in statuses
                        else "valid"
                    )
                    print(
                        f"| {job.name} | {trial.name} | {status} | "
                        f"{len(projection.trajectories)} | {len(projection.steps)} | "
                        f"{len(projection.tool_calls)} |"
                    )
            if args.export:
                rebuilt = rebuild_from_raw(jobs, _resolve(root, args.output_dir))
                for table in rebuilt.tables:
                    print(f"{table.table}: {table.rows} row(s) -> {table.path}")
            return 0
        if args.command == "compare":
            spec_path = _resolve(root, args.path)
            json_path, markdown_path, report = write_comparison(
                spec_path,
                repo_root=root,
                output_root=_resolve(root, args.output_dir),
            )
            if args.index:
                url = database_url_from_environment(args.database_url)
                database.initialize(url)
                index_comparison_associations(
                    url,
                    spec_path=spec_path,
                    report=report,
                    repo_root=root,
                )
            print(f"json: {json_path}")
            print(f"markdown: {markdown_path}")
            return 0
        if args.command == "analyze" and args.analyze_command in {"plan", "stub"}:
            job, trial = load_analysis_source(_resolve(root, args.path))
            prompt_path = root / "research/analysis/stage5-prompt.md"
            rubric_path = root / "research/analysis/stage5-rubric.json"
            output_root = _resolve(root, args.output_dir)
            if args.analyze_command == "plan":
                plan = analysis_plan(
                    job,
                    trial,
                    repo_root=root,
                    destination_root=output_root,
                    prompt_path=prompt_path,
                    rubric_path=rubric_path,
                    agent=args.agent,
                    agent_version=args.agent_version,
                    model=args.model,
                )
                print(json.dumps(asdict(plan), indent=2, sort_keys=True))
                return 0
            response = _resolve(root, args.response).read_text()

            def saved_response(_prompt: str, _schema: dict[str, object]) -> AnalyzerCallResult:
                return AnalyzerCallResult(
                    raw_output=response,
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=0.0,
                )

            sidecar_path, sidecar = run_trial_analysis(
                job,
                trial,
                analyzer=saved_response,
                repo_root=root,
                destination_root=output_root,
                prompt_path=prompt_path,
                rubric_path=rubric_path,
                agent="stub",
                agent_version="1",
                model="saved-response",
            )
            if args.index:
                url = database_url_from_environment(args.database_url)
                database.initialize(url)
                ingest_analysis_sidecar(url, sidecar_path, root=root)
            print(f"analysis: {sidecar_path}")
            print(f"validation: {sidecar.validation_status}")
            return 0 if sidecar.validation_status == "valid" else 1
        if args.command == "analyze" and args.analyze_command == "ingest-sidecar":
            sidecar_path = _resolve(root, args.path)
            url = database_url_from_environment(args.database_url)
            database.initialize(url)
            sidecar = ingest_analysis_sidecar(url, sidecar_path, root=root)
            print(f"indexed analysis: {sidecar.analysis_id}")
            return 0
        if args.command == "analyze" and args.analyze_command == "review":
            review_path, review = write_analysis_review(
                _resolve(root, args.path),
                disposition=args.disposition,
                rationale=args.rationale,
                reviewer=args.reviewer,
                superseded_by=(UUID(args.superseded_by) if args.superseded_by else None),
            )
            print(f"review: {review_path}")
            print(f"disposition: {review.disposition}")
            return 0
        if args.command == "analyze" and args.analyze_command == "agreement":
            report_path, report = write_failure_taxonomy_agreement(
                [_resolve(root, path) for path in args.paths],
                labels_root=_resolve(root, args.labels),
                output_path=_resolve(root, args.output),
                reference_root=root,
            )
            agreement = report["exact_agreement"]
            print(f"report: {report_path}")
            print(
                "agreement: "
                f"{report['exact_matches']}/{report['n_matched_valid']} "
                f"({'n/a' if agreement is None else f'{agreement:.3f}'})"
            )
            coverage = report["label_coverage"]
            print(f"label coverage: {'n/a' if coverage is None else f'{coverage:.3f}'}")
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


def _digest_renderer(root: Path) -> DigestRenderer:
    return DigestRenderer(
        repo_root=root,
        queue=DirectoryQueue(root / "queue"),
        policy=load_policy(root / "policy/standing-approvals.yaml"),
    )


def main() -> None:
    raise SystemExit(run_cli())


def legacy_main() -> None:
    print("warning: harbor-lab is deprecated; use evallab", file=sys.stderr)
    main()


if __name__ == "__main__":
    main()
