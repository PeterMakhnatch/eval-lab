from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from evallab import __version__

if TYPE_CHECKING:
    from evallab.fetch import HarborBackend
    from evallab.results import JobRecord
from evallab.storage.paths import DERIVED_ROOT_ENV, derived_root_from_environment


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _configured_quota_ceiling(root: Path) -> float | None:
    """Read the lab's policy-controlled `used_percent` refusal ceiling.

    `policy/standing-approvals.yaml` governs the ceiling through
    `StandingApprovalsPolicy.refuse_billable_at_used_percent`. It is currently
    committed at 80%, raised from the original 60% baseline introduced for the
    2026-10-13 overnight subscription-agent work. The legacy module constant
    is consulted only if the policy has no configured ceiling.
    """
    from evallab import queue as queue_module
    from evallab.queue import load_policy

    configured = getattr(
        load_policy(root / "policy/standing-approvals.yaml"),
        "refuse_billable_at_used_percent",
        None,
    )
    if configured is not None:
        return float(configured)
    return getattr(queue_module, "REFUSE_BILLABLE_AT_USED_PERCENT", None)


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
            reward = "ungraded" if trial.primary_reward is None else f"{trial.primary_reward:g}"
            print(
                f"| {job.name} | {result.get('task_name', '')} | "
                f"{agent_info.get('name', '')} | "
                f"{model_info.get('name') or model_info.get('model_name') or 'adhoc'} | "
                f"{reward} | {exception.get('exception_type', '')} | "
                f"{'' if seconds is None else f'{seconds:.3f}'} |"
            )


def _doctor(
    root: Path, *, home: Path | None = None, clock: Callable[[], datetime] | None = None
) -> int:
    from evallab import database
    from evallab.automation import audit_launch_agents, sync_primary_status
    from evallab.evidence.atif import check_projection_invariant
    from evallab.gc import doctor_disk_line
    from evallab.queue import Executor
    from evallab.runner import database_url_from_environment

    checks = Executor.from_repo(root).local_runtime_checks()

    database_url = database_url_from_environment()
    # The same green `catalog-parquet` line reported catalog=69 and catalog=4
    # in one M009 session because DATABASE_URL differed between shells. Name
    # the database on the line itself; `identity` never returns a credential
    # even when the connection string carries a password (F-11).
    catalog = f"db={database.identity(database_url)}"
    try:
        detail = database.ping(database_url)
        checks.append(("postgres", True, detail))
    except Exception as exc:  # Doctor should report all checks, not stop at the first.
        checks.append(("postgres", False, f"unavailable: {type(exc).__name__}"))
        checks.append(("catalog-parquet", False, f"catalog unavailable {catalog}"))
    else:
        try:
            invariant = check_projection_invariant(
                database_url,
                derived_root_from_environment(root),
                root / "queue/events.jsonl",
            )
        except Exception as exc:
            checks.append(
                ("catalog-parquet", False, f"unavailable: {type(exc).__name__} {catalog}")
            )
        else:
            checks.append(("catalog-parquet", invariant.ok, f"{invariant.detail} {catalog}"))

    task_toml = root / "library/tasks/event-summary/task.toml"
    checks.append(("task", task_toml.is_file(), "event-summary"))
    home_dir = home or Path.home()
    launch_agents = audit_launch_agents(home_dir)
    if launch_agents.broken:
        checks.extend(("launchagent", False, issue.describe()) for issue in launch_agents.broken)
    else:
        checks.append(("launchagent", True, f"{launch_agents.checked} evallab LaunchAgents intact"))
    # Advisory: the primary checkout's sync timer. It is deliberately absent
    # from `required` so a stalled sync is visible without changing doctor's
    # exit code (a 2026-10-12 dirty tree left the primary 848 commits behind).
    sync = sync_primary_status(home_dir, now=clock() if clock else None)
    checks.append(("sync-primary", sync.ok, sync.detail))
    for name, ok, detail in checks:
        print(f"{'ok' if ok else 'FAIL':4}  {name:14} {detail}")
    print(doctor_disk_line(root))
    required = {
        "harbor",
        "harbor-pin",
        "docker",
        "docker-daemon",
        "uv",
        "task",
        "catalog-parquet",
        "launchagent",
    }
    return 0 if all(ok for name, ok, _ in checks if name in required) else 1


# ---------------------------------------------------------------------------
# Declarative Command Handlers
# ---------------------------------------------------------------------------


def _doctor_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.automation import HeadlessDoctor
    from evallab.queue import Executor

    if args.headless:
        executor = Executor.from_repo(root)
        report = HeadlessDoctor(root, executor=executor).run()
        print(report.model_dump_json(indent=2))
        return 0 if report.ready else 1
    from evallab.automation import core_runtime_checks

    status = _doctor(root)
    checks = core_runtime_checks(root)
    for name, ok, detail in checks:
        print(f"{'ok' if ok else 'FAIL':4}  {name:14} {detail}")
    return int(status != 0 or any(not ok for _, ok, _ in checks))


def _preflight_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    """Print the preflight and exit non-zero when a provider refuses billable work.

    Costs nothing and blocks on nothing: the report is built from Harbor job
    directories and `queue/` alone. `provider_reported_exhaustion` is passed in
    rather than reimplemented so this surface and the dispatch gate can never
    disagree about what the provider said.

    With `--spec`, dry-runs each spec file against its named setup reference
    instead (HAR-149): same comparison dispatch enforces, before anything is
    approved. Needs no Daytona or Modal; `--root` points the task paths,
    ledger, and serve config at another checkout (read-only).
    """
    from evallab.preflight import build_preflight_report, render_preflight
    from evallab.queue import provider_reported_exhaustion


    if getattr(args, "spec", None):
        from evallab.setup_fingerprint import render_spec_preflight
        checkout = Path(args.preflight_root) if args.preflight_root else root
        ok = True
        for spec_path in args.spec:
            text, spec_ok = render_spec_preflight(_resolve(root, spec_path), checkout)
            print(text, end="" if text.endswith("\n") else "\n")
            if spec_ok:
                from evallab.job_diff import render_diff
                from evallab.preflight import preview_spec_diff
                from evallab.schemas import ExperimentSpec

                try:
                    spec = ExperimentSpec.model_validate_json(_resolve(root, spec_path).read_text())
                    sources = getattr(args, "diff_sources", None)
                    if sources:
                        spec = spec.model_copy(
                            update={
                                "diff_sources": [str(_resolve(checkout, path)) for path in sources]
                            }
                        )
                    print(render_diff(preview_spec_diff(spec, checkout)), end="")
                except (OSError, ValueError, RuntimeError, ImportError) as exc:
                    print(f"DIFF REFUSED: {exc}")
                    spec_ok = False
            ok = ok and spec_ok
        return 0 if ok else 1
    if getattr(args, "diff_sources", None):
        raise ValueError("--diff requires --spec; persist diff_sources in the spec before approval")
    target = _resolve(root, args.preflight_from) if args.preflight_from else root
    report = build_preflight_report(
        target,
        now=datetime.now(UTC),
        refusal=provider_reported_exhaustion,
        refuse_at_used_percent=_configured_quota_ceiling(target),
        useful_effect=args.useful_effect,
    )
    print(render_preflight(report))
    return 1 if report.refusals() or report.diff_errors else 0


def _nightly_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.nightly_refresh import RefreshConfig, refresh_exit_code, run_refresh

    result = run_refresh(
        RefreshConfig(
            repo_root=root,
            state_dir=args.state_dir.expanduser().resolve(),
            queue_roots=tuple(args.queue_root),
            facts_root=args.facts_root,
            readers_store=args.readers_store,
            data_root=args.data_root,
            verdict_root=getattr(args, "verdict_root", None),
            backup=getattr(args, "backup", False),
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return refresh_exit_code(result)


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


def _db_init_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab import database
    from evallab.runner import database_url_from_environment

    url = database_url_from_environment(args.database_url)
    database.initialize(url)
    print("database schema is current")
    return 0


def _harden_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.full_hardening import HardeningRefused, harden

    try:
        manifest = harden(
            args.package,
            args.out,
            audit_only=args.audit_only,
            expected_tests=args.expected_tests,
            expected_skipped=args.expected_skipped,
            calibrate=args.calibrate,
            helpers=tuple(args.helper),
            verifier_dockerfile=args.verifier_dockerfile,
        )
    except HardeningRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(manifest.to_json(), indent=2, sort_keys=True))
        return 0
    print(
        f"{manifest.transform} {manifest.shape} package, workdir {manifest.workdir}, runner {manifest.runner}"
    )
    for layer in manifest.layers:
        print(f"  {layer.layer} {layer.status}: {layer.mechanism}")
    verifier = manifest.verifier
    print(
        f"  verifier gate_runtime {verifier.gate_runtime}"
        + (f" ({verifier.dockerfile} {verifier.origin}, sha256 {verifier.dockerfile_sha256})" if verifier.dockerfile else "")
    )
    for finding in manifest.findings:
        print(f"  finding {finding.kind} {finding.path}: {finding.detail} [{finding.action}]")
    print(f"  output: {manifest.output or '(audit only)'}")
    return 0


def _tasks_lint_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from dataclasses import asdict

    from evallab.task_lint import discover_tasks, lint_task

    tasks = discover_tasks([_resolve(root, path) for path in args.paths])
    findings = [finding for task in tasks for finding in lint_task(task)]
    if args.json:
        print(json.dumps([asdict(finding) for finding in findings], indent=2))
    else:
        for finding in findings:
            print(f"{finding.severity} {finding.rule} {finding.path}: {finding.message}")
    return 1 if any(finding.severity == "error" for finding in findings) else 0


def _tasks_anatomy_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.package_anatomy import analyze_package, discover_packages, dumps_card, render_text

    try:
        packages = discover_packages([_resolve(root, path) for path in args.paths])
        if args.reference_patch is not None and len(packages) != 1:
            raise ValueError("--reference-patch applies to exactly one task package")
        patch = None if args.reference_patch is None else _resolve(root, args.reference_patch)
        cards = [analyze_package(package, reference_patch=patch) for package in packages]
    except (FileNotFoundError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if args.out is not None:
        out = _resolve(root, args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(dumps_card(card) + "\n" for card in cards), encoding="utf-8")
    if args.json:
        print(json.dumps(cards, indent=2, ensure_ascii=False))
    else:
        for card in cards:
            print(render_text(card))
    return 0


def _registry_promote_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.registry import RegistryError, promote_task

    registry_dir = Path(args.registry_dir) if args.registry_dir else None
    jobs_roots = [Path(args.jobs_dir)] if args.jobs_dir else None
    allowed_uses = (
        [u.strip() for u in args.allowed_uses.split(",")]  # type: ignore[arg-type]
        if args.allowed_uses
        else None
    )

    state = (
        "registered"
        if getattr(args, "register", False) or args.state == "registered"
        else "candidate"
    )
    if state == "registered" and not args.actor:
        print("error: registering a task record requires --actor", file=sys.stderr)
        return 1

    try:
        record = promote_task(
            task_path=args.task_path,
            repo_root=root,
            registry_dir=registry_dir,
            task_id=args.task_id,
            task_family=getattr(args, "task_family", None),
            version=args.version,
            source_uri=args.source_uri,
            source_ref=args.source_ref,
            license_str=args.license,
            provenance_zone=args.provenance_zone,
            is_synthetic=args.synthetic,
            timeout_seconds=args.timeout_seconds,
            max_memory_mb=args.max_memory_mb,
            max_cpus=args.max_cpus,
            allowed_uses=allowed_uses,
            human_minutes=args.human_minutes,
            state=state,
            actor=args.actor,
            jobs_roots=jobs_roots,
            certification_path=getattr(args, "certification_packet", None),
        )
    except (RegistryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(record.model_dump(mode="json"), indent=2))
    else:
        print(f"promoted: {record.task_id}@{record.version} (state: {record.state})")
        print(f"  path:     {record.task_path}")
        print(f"  package:  {record.digests.package}")
        evidence = record.control_evidence
        if evidence is not None:
            print(f"  oracle:   {evidence.oracle.job_name} ({evidence.oracle.reward})")
            print(f"  nop:      {evidence.nop.job_name} ({evidence.nop.reward})")
        if record.approved_by:
            print(f"  approved: {record.approved_by} at {record.approved_at}")
    return 0

def _registry_register_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.registry import RegistryError, register_task

    if not args.actor or not args.actor.strip():
        print("error: registering a task requires --actor", file=sys.stderr)
        return 1

    registry_dir = Path(args.registry_dir) if args.registry_dir else None

    try:
        record = register_task(
            task_id=args.task_id,
            actor=args.actor,
            repo_root=root,
            registry_dir=registry_dir,
            certification_path=getattr(args, "certification_packet", None),
        )
    except (RegistryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(record.model_dump(mode="json"), indent=2))
    else:
        print(f"registered: {record.task_id}@{record.version} (state: {record.state})")
        print(f"  approved by: {record.approved_by}")
        print(f"  approved at: {record.approved_at}")
    return 0


def _registry_audit_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
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


def _registry_devloop_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    del harbor
    from evallab.devloop import run_devloop

    return run_devloop(
        root, since=args.since, run=args.run, as_json=args.json, checkpoint=args.checkpoint
    )


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


# ---------------------------------------------------------------------------
# Declarative CLI Parser Construction
# ---------------------------------------------------------------------------



def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="evallab",
        description="Run, inspect, and analyze agent evaluations through Harbor.",
    )
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="Check CORE tool pins, stores, credentials, and installed jobs")
    doctor.add_argument(
        "--headless",
        action="store_true",
        help="Fail closed and print only boolean prerequisite status as JSON",
    )
    doctor.set_defaults(func=_doctor_command)


    preflight = commands.add_parser(
        "preflight",
        help="Read-only: remaining quota per provider, the queue by purpose, power warnings",
    )
    preflight.add_argument(
        "--useful-effect",
        type=float,
        help=(
            "Per-attempt difference you would call useful. Unset by default because "
            "'useful' is a spend judgement, not a lab constant; supply it and a queued "
            "comparison whose smallest detectable difference exceeds it becomes a warning."
        ),
    )
    preflight.add_argument(
        "--from",
        dest="preflight_from",
        type=Path,
        help="Repository root to read (default: this checkout)",
    )
    preflight.add_argument(
        "--spec",
        type=Path,
        nargs="+",
        default=None,
        help=(
            "Dry-run spec file(s) against their named setup reference (HAR-149): "
            "the same fingerprint comparison dispatch enforces, before anything "
            "is approved. Needs no Daytona or Modal."
        ),
    )
    preflight.add_argument(
        "--root",
        dest="preflight_root",
        type=Path,
        default=None,
        help=(
            "Checkout the spec task paths, ledger, and serve config resolve "
            "against (default: this checkout). Read-only."
        ),
    )
    preflight.add_argument(
        "--diff",
        dest="diff_sources",
        action="append",
        type=Path,
        help="Preview against a local prior job (repeatable); requires --spec. Dispatch uses the spec's approval-bound diff_sources.",
    )
    preflight.set_defaults(func=_preflight_command)


    nightly = commands.add_parser("nightly", help="Refresh local nightly reports")
    nightly.add_argument(
        "--refresh",
        action="store_true",
        required=True,
        help="HAR-199: local-only $0 incremental refresh; never dispatch or call models",
    )
    nightly.add_argument(
        "--queue-root",
        action="append",
        type=Path,
        default=[],
        help="Checkout containing queue and runs/jobs (repeatable); default primary plus worktrees with queues",
    )
    nightly.add_argument(
        "--state-dir", type=Path, default=Path.home() / ".local/state/evallab-nightly"
    )
    nightly.add_argument(
        "--facts-root",
        type=Path,
        help="Daily-report state root (default ~/.local/state/daily-report)",
    )
    nightly.add_argument(
        "--readers-store", type=Path, help="Read-only HAR-176 reader verdict store"
    )
    nightly.add_argument(
        "--data-root",
        type=Path,
        help="Primary evidence checkout when running an installed source snapshot",
    )
    nightly.add_argument(
        "--verdict-root",
        type=Path,
        help="Read-only checkout with real HAR-177 census/variant/oracle inputs; snapshotted under state",
    )
    nightly.add_argument(
        "--backup",
        action="store_true",
        help="With --refresh: also take the dated pg_dump under the 120-second backup timeout",
    )
    nightly.set_defaults(func=_nightly_command)


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


    db = commands.add_parser("db", help="Manage the derived PostgreSQL index")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_init = db_commands.add_parser("init", help="Apply the idempotent schema")
    db_init.add_argument("--database-url")
    db_init.set_defaults(func=_db_init_command)


    tasks_parser = commands.add_parser("tasks", help="Import and manage task corpora")
    tasks_commands = tasks_parser.add_subparsers(dest="tasks_command", required=True)


    tasks_lint = tasks_commands.add_parser(
        "lint", help="Read-only static checks for task verifier trust boundaries"
    )
    tasks_lint.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Task directories or task collections; nested task packages are discovered",
    )
    tasks_lint.add_argument("--json", action="store_true")
    tasks_lint.set_defaults(func=_tasks_lint_command)

    tasks_anatomy = tasks_commands.add_parser(
        "anatomy",
        help="Deterministic grading/container attack-surface card for task packages ($0, static)",
    )
    tasks_anatomy.add_argument(
        "paths", nargs="+", type=Path, help="Task directories or collections of task directories"
    )
    tasks_anatomy.add_argument(
        "--reference-patch",
        type=Path,
        help="DEV reference patch for the one package given; enables symbol/branch exercise checks",
    )
    tasks_anatomy.add_argument("--out", type=Path, help="Write one compact JSON card per line")
    tasks_anatomy.add_argument("--json", action="store_true", help="Print full cards as JSON")
    tasks_anatomy.set_defaults(func=_tasks_anatomy_command)


    trace_parser = commands.add_parser(
        "trace",
        help="Trace anatomy and AutoReview",
    )
    trace_actions = trace_parser.add_subparsers(dest="trace_command", required=True)
    from evallab.trace_parser import register_anatomy as register_trace_anatomy

    register_trace_anatomy(trace_actions)
    from evallab.trace_parser import register_autoreview

    register_autoreview(trace_actions)


    registry = commands.add_parser(
        "registry",
        help="Inspect and audit explicit registered tasks",
    )
    registry_commands = registry.add_subparsers(
        dest="registry_command",
        required=True,
    )

    registry_promote = registry_commands.add_parser(
        "promote",
        help="Promote a task package into the explicit task registry",
    )
    registry_promote.add_argument(
        "task_path",
        help="Path to task package directory",
    )
    registry_promote.add_argument(
        "--task-id",
        help="Explicit task identifier (defaults to task.toml name or directory name)",
    )
    registry_promote.add_argument(
        "--task-family",
        help="Immutable benchmark family (or declare task.family in task.toml)",
    )
    registry_promote.add_argument(
        "--version",
        help="Task version string (defaults to task.toml version or 1.0.0)",
    )
    registry_promote.add_argument(
        "--source-uri",
        help="Source URI for task provenance",
    )
    registry_promote.add_argument(
        "--source-ref",
        help="Source revision/commit reference",
    )
    registry_promote.add_argument(
        "--license",
        help="Declared license",
    )
    registry_promote.add_argument(
        "--provenance-zone",
        choices=["01-external", "02-local-evidence", "03-synthetic", "04-curated"],
        help="Provenance zone",
    )
    registry_promote.add_argument(
        "--synthetic",
        action="store_true",
        help="Mark task as synthetic",
    )
    registry_promote.add_argument(
        "--timeout-seconds",
        type=int,
        help="Execution timeout limit in seconds",
    )
    registry_promote.add_argument(
        "--max-memory-mb",
        type=int,
        help="Memory limit in megabytes",
    )
    registry_promote.add_argument(
        "--max-cpus",
        type=float,
        help="CPU limit",
    )
    registry_promote.add_argument(
        "--allowed-uses",
        help="Comma-separated allowed uses (default: measurement,training)",
    )
    registry_promote.add_argument(
        "--human-minutes",
        type=int,
        help="Expert human completion time estimate in minutes",
    )
    registry_promote.add_argument(
        "--state",
        choices=["candidate", "registered"],
        default="candidate",
        help="Admission state (default: candidate)",
    )
    registry_promote.add_argument(
        "--actor",
        help="Approving human actor (required when state is registered)",
    )
    registry_promote.add_argument(
        "--register",
        action="store_true",
        help="Register task immediately (requires --actor)",
    )
    registry_promote.add_argument(
        "--jobs-dir",
        help="Directory containing completed Harbor control runs",
    )
    registry_promote.add_argument(
        "--registry-dir",
        help="Override destination registry directory",
    )
    registry_promote.add_argument(
        "--certification-packet",
        help="Durable native package-check certification.json to bind (not a Gold certificate)",
    )
    registry_promote.add_argument(
        "--json",
        action="store_true",
        help="Emit promoted record as JSON",
    )
    registry_promote.set_defaults(func=_registry_promote_command)

    registry_register = registry_commands.add_parser(
        "register",
        help="Register a candidate task in the explicit task registry with human approval",
    )
    registry_register.add_argument(
        "task_id",
        help="Task ID of candidate record in registry",
    )
    registry_register.add_argument(
        "--actor",
        required=True,
        help="Approving human actor name",
    )
    registry_register.add_argument(
        "--registry-dir",
        help="Override registry directory",
    )
    registry_register.add_argument(
        "--certification-packet",
        help="Durable native package-check certification.json required for new registration",
    )
    registry_register.add_argument(
        "--json",
        action="store_true",
        help="Emit registered record as JSON",
    )
    registry_register.set_defaults(func=_registry_register_command)


    registry_audit = registry_commands.add_parser(
        "audit",
        help="Audit task registry records and queue claims",
    )
    registry_audit.add_argument(
        "--json",
        action="store_true",
        help="Emit audit report as JSON",
    )
    registry_audit.set_defaults(func=_registry_audit_command)


    registry_devloop = registry_commands.add_parser(
        "devloop",
        help="Map working tree changes to affected test modules and run targeted pytest",
    )
    registry_devloop.add_argument(
        "--since",
        default="working",
        help="Revision or baseline to diff against (default: working)",
    )
    registry_devloop.add_argument(
        "--run",
        action="store_true",
        help="Execute the computed pytest command",
    )
    registry_devloop.add_argument(
        "--json",
        action="store_true",
        help="Emit results as JSON",
    )
    registry_devloop.add_argument(
        "--checkpoint",
        action="store_true",
        help="Run repository contracts and direct consumers; bounded local check, not CI green",
    )
    registry_devloop.set_defaults(func=_registry_devloop_command)


    harden_parser = commands.add_parser(
        "harden",
        help="full-hardening@1: L1 auto-load purge, L2 pristine tests/helpers, L3 report gate, L4 separation",
    )
    harden_parser.add_argument(
        "package", type=Path, help="MiMo test.sh or general Harbor task package"
    )
    harden_parser.add_argument("--out", type=Path, help="New directory for the hardened package")
    harden_parser.add_argument(
        "--audit-only", action="store_true", help="Report the plan; write nothing"
    )
    harden_parser.add_argument("--expected-tests", type=int, help="Reference test count (L3 gate)")
    harden_parser.add_argument("--expected-skipped", type=int, default=0)
    harden_parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Record report counts without gating (reference runs only)",
    )
    harden_parser.add_argument(
        "--helper",
        action="append",
        default=[],
        help="Workspace path the grader trusts (L2); repeatable",
    )
    harden_parser.add_argument(
        "--verifier-dockerfile",
        type=Path,
        help=(
            "Verifier image for the separate verifier (harbor packages): copied byte-identical to "
            "tests/Dockerfile; without it the gate runtime is recorded as unverified"
        ),
    )
    harden_parser.add_argument("--json", action="store_true")
    harden_parser.set_defaults(func=_harden_command)
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


    from evallab.runtime_parser import register_runtime_commands

    register_runtime_commands(commands)


    return root


def _normalize_review_argv(argv: Sequence[str] | None) -> Sequence[str] | None:
    if argv is None:
        argv = sys.argv[1:]
    normalized: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--arm-regex" and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            normalized.append(f"{argv[i]}={argv[i + 1]}")
            i += 2
        else:
            normalized.append(argv[i])
            i += 1
    return normalized


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    workspace: Path | None = None,
    harbor: HarborBackend | None = None,
) -> int:
    root = workspace if workspace is not None else repo_root()
    load_local_env(root / ".env")
    args = parser().parse_args(_normalize_review_argv(argv))
    try:
        handler: Callable[..., int] | None = getattr(args, "func", None)
        if handler is None:
            return 2
        return handler(args, root, harbor=harbor)
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(run_cli(argv))


if __name__ == "__main__":
    main()
