from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path
from uuid import UUID

from evallab import __version__, database
from evallab.atif import check_projection_invariant, ingest_and_project
from evallab.automation import (
    GuardedTick,
    HeadlessDoctor,
    NightlyCycle,
    ScheduleInstaller,
    record_quarantine,
    record_researcher_deferral,
)
from evallab.backups import create_postgres_backup
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
from evallab.cohort import (
    index_comparison_associations,
    minimum_detectable_effect,
    pass_at_k_probability,
    power_requirements,
    write_comparison,
)
from evallab.digest import DigestRenderer
from evallab.facts import (
    AnalyzerCallResult,
    analysis_plan,
    ingest_analysis_sidecar,
    load_analysis_source,
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
from evallab.gc import (
    append_gc_plan_to_digest,
    doctor_disk_line,
    format_plan,
    nightly_gc_plan,
    run_gc,
)
from evallab.paths import DERIVED_ROOT_ENV, derived_root_from_environment
from evallab.queue import (
    DirectoryQueue,
    Executor,
    load_policy,
    new_ulid,
    read_spec,
    record_projection_failures,
)
from evallab.report import (
    build_eval_card,
    draft_eval_card,
    family_report,
    render_family_report,
    write_family_report,
)
from evallab.researchers import ResearcherLoop
from evallab.results import JobRecord, load_job, load_jobs
from evallab.runner import (
    RunRequest,
    database_url_from_environment,
    expected_primary_reward,
    load_matrix,
    request_from_matrix,
    subscription_environment,
)
from evallab.status import build_status_snapshot, render_status_text, snapshot_as_dict
from evallab.tracing import (
    TraceError,
    format_batch,
    instrument_openinference,
    trace_completed_jobs,
    trace_path,
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


LOCAL_ENV_KEYS = {
    "DATABASE_URL",
    DERIVED_ROOT_ENV,
    "HARBOR_CLAUDE_KEYCHAIN_ACCOUNT",
    "HARBOR_CLAUDE_KEYCHAIN_SERVICE",
    "LAB_REPORT_ISSUE",
    "POSTGRES_DB",
    "POSTGRES_PASSWORD",
    "POSTGRES_PORT",
    "POSTGRES_USER",
}


def load_local_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        if normalized_key in LOCAL_ENV_KEYS:
            os.environ.setdefault(normalized_key, value.strip().strip("'\""))


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

    status = commands.add_parser(
        "status",
        help="Read-only operator snapshot of recent work, health, and saved analysis",
    )
    status.add_argument(
        "--json",
        action="store_true",
        help="Emit the typed status snapshot as JSON",
    )
    status.add_argument(
        "--from",
        dest="status_from",
        type=Path,
        help="Repository root or smoke scratch (queue/ + jobs/) to read",
    )

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
        "--timeout-seconds",
        type=int,
        default=1_800,
        help="executor wall-clock allowance per attempted trial",
    )
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
    ingest.add_argument(
        "--derived-dir",
        type=Path,
        help="override the shared Parquet root for this invocation",
    )

    trajectories = commands.add_parser(
        "trajectories",
        help="Validate ATIF; optionally rebuild catalog and deterministic Parquet facts",
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
        help="Rebuild the catalog and write trajectory/trial facts to Parquet",
    )
    trajectories.add_argument(
        "--output-dir",
        type=Path,
        help="override the shared Parquet root for this invocation",
    )
    trajectories.add_argument("--database-url")

    compare = commands.add_parser("compare", help="Compare declared trial cohorts")
    compare.add_argument("path", type=Path)
    compare.add_argument("--output-dir", type=Path, default=Path("derived/comparisons"))
    compare.add_argument("--index", action="store_true")
    compare.add_argument("--database-url")

    power = commands.add_parser("power", help="Plan task-paired pass@k comparison power")
    power_mode = power.add_mutually_exclusive_group(required=True)
    power_mode.add_argument(
        "--n-tasks",
        type=int,
        help="Compute the minimum detectable per-attempt difference for this many paired tasks",
    )
    power_mode.add_argument(
        "--target",
        type=float,
        help="Compute required paired tasks across k for this per-attempt difference",
    )
    power.add_argument("--k", type=int, help="Attempts per task for --n-tasks mode")
    power.add_argument("--max-k", type=int, default=8, help="Largest k for --target mode")
    power.add_argument("--baseline", type=float, required=True)
    power.add_argument("--alpha", type=float, default=0.05)
    power.add_argument("--power", dest="target_power", type=float, default=0.8)
    power.add_argument("--pair-correlation", type=float, default=0.0)

    report = commands.add_parser("report", help="Render trajectory families and eval cards")
    report_commands = report.add_subparsers(dest="report_command", required=True)
    report_family = report_commands.add_parser(
        "family", help="Explain one task family from Parquet and canonical ATIF"
    )
    report_family.add_argument("task")
    report_family.add_argument(
        "--parquet-dir",
        type=Path,
        help="override the shared Parquet root for this invocation",
    )
    report_family.add_argument(
        "--raw-root",
        type=Path,
        action="append",
        help="Raw Harbor root; repeat as needed (defaults to runs and reviewed evidence)",
    )
    report_family.add_argument(
        "--output-dir",
        type=Path,
        help="write JSON and Markdown reports (default: render without writing)",
    )
    report_card = report_commands.add_parser(
        "card", help="Draft a provenance-bearing eval card from a completed spec"
    )
    report_card.add_argument("path", type=Path)
    report_card.add_argument(
        "--output",
        type=Path,
        help="write the eval card (default: render without writing)",
    )

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

    gc = commands.add_parser(
        "gc",
        help="Plan or apply compression/pruning of unpromoted ingested runs",
    )
    gc.add_argument(
        "--apply",
        action="store_true",
        help="Execute the plan (default is a dry-run that mutates nothing)",
    )
    gc.add_argument(
        "--runs-dir",
        type=Path,
        default=Path("runs"),
        help="Job directory root to scan (default: runs/)",
    )

    registry = commands.add_parser(
        "registry",
        help="Inspect and audit explicit registered tasks",
    )
    registry_commands = registry.add_subparsers(
        dest="registry_command",
        required=True,
    )
    registry_list = registry_commands.add_parser(
        "list",
        help="List explicit task registry records",
    )
    registry_list.add_argument(
        "--json",
        action="store_true",
        help="Emit records as JSON array",
    )
    registry_list.add_argument(
        "--state",
        choices=["candidate", "registered", "retired"],
        help="Filter records by admission state",
    )

    registry_audit = registry_commands.add_parser(
        "audit",
        help="Audit task registry records and queue claims",
    )
    registry_audit.add_argument(
        "--json",
        action="store_true",
        help="Emit audit report as JSON",
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
        checks.append(("catalog-parquet", False, "catalog unavailable"))
    else:
        try:
            invariant = check_projection_invariant(
                database_url,
                derived_root_from_environment(root),
                root / "queue/events.jsonl",
            )
        except Exception as exc:
            checks.append(("catalog-parquet", False, f"unavailable: {type(exc).__name__}"))
        else:
            checks.append(("catalog-parquet", invariant.ok, invariant.detail))

    task_toml = root / "library/tasks/event-summary/task.toml"
    checks.append(("task", task_toml.is_file(), "event-summary"))
    for name, ok, detail in checks:
        print(f"{'ok' if ok else 'FAIL':4}  {name:14} {detail}")
    print(doctor_disk_line(root))
    required = {"harbor", "docker", "docker-daemon", "uv", "task", "catalog-parquet"}
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
        timeout_seconds=args.timeout_seconds,
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


def _power_command(args: argparse.Namespace) -> int:
    if args.n_tasks is not None:
        if args.k is None:
            raise ValueError("--k is required with --n-tasks")
        effect = minimum_detectable_effect(
            n_tasks=args.n_tasks,
            k=args.k,
            baseline=args.baseline,
            alpha=args.alpha,
            target_power=args.target_power,
            pair_correlation=args.pair_correlation,
        )
        baseline_pass = pass_at_k_probability(args.baseline, args.k)
        print("Task-paired pass@k power plan")
        print(f"n_tasks: {args.n_tasks}")
        print(f"k: {args.k}")
        print(f"baseline per-attempt pass rate: {args.baseline:.3f}")
        print(f"baseline pass@{args.k}: {baseline_pass:.3f}")
        print(f"alpha / power: {args.alpha:.3f} / {args.target_power:.3f}")
        if effect is None:
            print("minimum detectable per-attempt difference: unavailable at this n and k")
        else:
            comparison_pass = pass_at_k_probability(args.baseline + effect, args.k)
            print(f"minimum detectable per-attempt difference: {effect:.4f}")
            print(
                f"implied pass@{args.k} difference: {comparison_pass - baseline_pass:.4f}"
            )
        print(
            "Assumptions: independent attempts for the pass@k transformation and a normal "
            "approximation to paired task outcomes; "
            f"pair correlation={args.pair_correlation:.3f}."
        )
        return 0

    rows = power_requirements(
        baseline=args.baseline,
        attempt_effect=args.target,
        max_k=args.max_k,
        alpha=args.alpha,
        target_power=args.target_power,
        pair_correlation=args.pair_correlation,
    )
    print("| k | baseline pass@k | comparison pass@k | task effect | n_tasks | attempts |")
    print("|---:|---:|---:|---:|---:|---:|")
    for row in rows:
        print(
            f"| {row['k']} | {row['baseline_pass_at_k']:.3f} | "
            f"{row['comparison_pass_at_k']:.3f} | {row['task_level_effect']:.3f} | "
            f"{row['required_n_tasks']} | {row['total_attempts_two_cohorts']} |"
        )
    print(
        "n_tasks is paired tasks per cohort; attempts counts both cohorts. "
        f"alpha={args.alpha:.3f}, power={args.target_power:.3f}, "
        f"pair correlation={args.pair_correlation:.3f}."
    )
    print("Assumption: attempts are independent for the pass@k transformation.")
    return 0


def _report_command(args: argparse.Namespace, root: Path) -> int:
    if args.report_command == "family":
        raw_roots = args.raw_root or [
            Path("runs"),
            Path("research/evidence/runs"),
            Path("evidence/runs"),
        ]
        parquet_root = (
            _resolve(root, args.parquet_dir)
            if args.parquet_dir is not None
            else derived_root_from_environment(root)
        )
        resolved_raw_roots = [_resolve(root, path) for path in raw_roots]
        if args.output_dir is None:
            report = family_report(
                args.task,
                parquet_root=parquet_root,
                raw_roots=resolved_raw_roots,
            )
        else:
            json_path, markdown_path, report = write_family_report(
                args.task,
                parquet_root=parquet_root,
                raw_roots=resolved_raw_roots,
                output_root=_resolve(root, args.output_dir),
            )
        print(render_family_report(report))
        if args.output_dir is not None:
            print(f"json: {json_path}")
            print(f"markdown: {markdown_path}")
        return 0
    spec_path = _resolve(root, args.path)
    if args.output is None:
        rendered, card = build_eval_card(spec_path, repo_root=root)
        print(rendered)
    else:
        path, card = draft_eval_card(
            spec_path,
            repo_root=root,
            output_path=_resolve(root, args.output),
        )
        print(f"eval card: {path}")
    print(f"config digest: {card['spec_digest']}")
    return 0


def _status_command(args: argparse.Namespace, root: Path) -> int:
    target = _resolve(root, args.status_from) if args.status_from is not None else root
    snapshot = build_status_snapshot(target)
    if args.json:
        print(json.dumps(snapshot_as_dict(snapshot), indent=2))
    else:
        print(render_status_text(snapshot), end="")
    return 0


def _dashboard_command(args: argparse.Namespace, root: Path) -> int:
    environment = subscription_environment()
    environment["DATABASE_URL"] = database_url_from_environment(args.database_url)
    environment[DERIVED_ROOT_ENV] = str(derived_root_from_environment(root))
    environment["PYTHONPATH"] = str(root)
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
        if args.command == "power":
            return _power_command(args)
        if args.command == "report":
            return _report_command(args, root)
        if args.command == "dashboard":
            return _dashboard_command(args, root)
        if args.command == "status":
            return _status_command(args, root)
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
            append_gc_plan_to_digest(path, nightly_gc_plan(root))
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
            database_url = database_url_from_environment()

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
                digest_enricher=_nightly_digest_enricher(root, get_researcher_loop),
                completed_job_ingester=lambda: ingest_and_project(
                    database_url,
                    load_jobs(
                        [
                            root / "runs",
                            root / "research/evidence/runs",
                            root / "evidence/runs",
                        ]
                    ),
                    root=root,
                    output_root=derived_root_from_environment(root),
                ),
                database_backup=lambda day: create_postgres_backup(root, day),
            ).run(report_date=args.report_date)
            print(f"digest: {result.digest_path}")
            print(
                "database backup: "
                f"{getattr(result, 'backup_path', None) or 'not created'}"
            )
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
            plan = nightly_gc_plan(root)
            print(format_plan(plan))
            return 1 if result.quarantined else 0
        if args.command == "gc":
            plan, applied = run_gc(
                root,
                apply=args.apply,
                runs_dir=_resolve(root, args.runs_dir),
            )
            print(format_plan(plan))
            if applied is not None:
                print(f"applied: {len(applied.tombstones)} tombstone(s)")
            return 0
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
            print(f"ingested {result.cataloged_jobs} job(s)")
            for table, rows in sorted(result.row_counts.items()):
                print(f"{table}: {rows} row(s)")
            for failure in result.failures:
                print(f"projection failed: {failure.job_name} ({failure.error_type})")
            return 1 if result.failures else 0
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
            if not args.export:
                return 0
            result = ingest_and_project(
                database_url_from_environment(args.database_url),
                jobs,
                root=root,
                output_root=derived_root_from_environment(
                    root,
                    explicit=args.output_dir,
                ),
            )
            record_projection_failures(
                DirectoryQueue(root / "queue"),
                result,
                actor="manual-trajectories",
                spec_id=f"system-{new_ulid()}",
            )
            for table in result.tables:
                print(f"{table.table}: {table.rows} row(s) -> {table.path}")
            print("totals:")
            for table, rows in sorted(result.row_counts.items()):
                print(f"{table}: {rows} row(s)")
            for failure in result.failures:
                print(f"projection failed: {failure.job_name} ({failure.error_type})")
            return 1 if result.failures else 0
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
            for paired in report["paired"]:
                print(paired["statement"])
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
        if args.command == "registry" and args.registry_command == "list":
            from evallab.registry import TaskRegistry

            reg = TaskRegistry.from_repo(root)
            records = reg.list_records(args.state)
            if args.json:
                payload = [record.model_dump(mode="json") for record in records]
                print(json.dumps(payload, indent=2))
                return 0
            if not records:
                filter_msg = f" with state {args.state!r}" if args.state else ""
                print(f"No task records found in library/registry/{filter_msg}.")
                return 0
            print(f"{'TASK ID':<32} {'VERSION':<10} {'STATE':<12} {'ZONE':<18} {'PATH'}")
            print("-" * 100)
            for record in records:
                print(
                    f"{record.task_id:<32} {record.version:<10} {record.state:<12} "
                    f"{record.provenance_zone:<18} {record.task_path}"
                )
            return 0
        if args.command == "registry" and args.registry_command == "audit":
            from evallab.registry import audit_registry

            report = audit_registry(root)
            if args.json:
                print(json.dumps(report.to_dict(), indent=2))
                return 0 if report.passed else 1

            print(f"Task Registry Audit (Total records: {report.total_records})")
            print(f"  Registered: {report.registered_count}")
            print(f"  Candidate:  {report.candidate_count}")
            print(f"  Retired:    {report.retired_count}")
            print()

            if not report.findings:
                print("PASS: zero audit findings. Registry and queue claims are valid.")
                return 0

            error_count = sum(1 for f in report.findings if f.severity == "error")
            warning_count = sum(1 for f in report.findings if f.severity == "warning")
            info_count = sum(1 for f in report.findings if f.severity == "info")

            print(f"Findings: {error_count} errors, {warning_count} warnings, {info_count} info")
            print("-" * 80)
            for finding in report.findings:
                icon = (
                    "FAIL"
                    if finding.severity == "error"
                    else ("WARN" if finding.severity == "warning" else "INFO")
                )
                print(f"[{icon}] {finding.category} -> {finding.target}")
                print(f"       {finding.message}")
            return 0 if report.passed else 1
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


def _nightly_digest_enricher(root: Path, get_loop: Callable[[], ResearcherLoop]):
    def enrich(path: Path, day: date) -> None:
        get_loop().enrich_digest(path, day)
        append_gc_plan_to_digest(path, nightly_gc_plan(root))

    return enrich


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
