from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime
from importlib import import_module
from pathlib import Path
from typing import Any
from uuid import UUID

import evallab.interpretation.trajectory_runtime as trajectory_runtime
from evallab import __version__, database
from evallab import queue as queue_module
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
from evallab.evidence.atif import check_projection_invariant, ingest_and_project
from evallab.evidence.facts import (
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
from evallab.labels import (
    evaluate_heuristic_precision,
    label_trajectory,
    select_review_queue,
)
from evallab.lineage import lineage_to_dict, render_lineage_tree, resolve_lineage
from evallab.preflight import build_preflight_report, render_preflight
from evallab.queue import (
    DirectoryQueue,
    DispatchCapacity,
    Executor,
    lab_threshold_reached,
    load_policy,
    new_ulid,
    provider_reported_exhaustion,
    read_spec,
    record_projection_failures,
    render_headroom_notice,
)
from evallab.quota import (
    Headroom,
    default_roots,
    load_quota_report,
    provider_subscription_description,
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
from evallab.schemas import ANALYSIS_REVIEWS_DIRNAME, ANALYSIS_SIDECAR_FILENAME
from evallab.status import build_status_snapshot, render_status_text, snapshot_as_dict
from evallab.status_generator import generate_status_markdown, update_status_file
from evallab.storage.attach import attach, attach_and_query, build_sql_preamble, print_zones
from evallab.storage.paths import DERIVED_ROOT_ENV, derived_root_from_environment
from evallab.tracing import (
    TraceError,
    format_batch,
    instrument_openinference,
    trace_completed_jobs,
    trace_path,
)
from evallab.traj import outline_trajectory, project_trajectory_features, render_outline


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _headroom_for(root: Path, *, agent: str) -> Headroom:
    """Read only the allowance evidence belonging to ``agent``."""
    try:
        return load_quota_report(
            default_roots(root),
            now=datetime.now(UTC),
            paid_agents=frozenset({agent}),
        ).headroom
    except (OSError, ValueError) as exc:
        return Headroom(
            availability="unavailable",
            reason=f"the quota scan failed ({type(exc).__name__}: {exc})",
        )


def _configured_quota_ceiling(root: Path) -> float | None:
    """The lab's `used_percent` refusal ceiling, wherever it currently lives.

    `queue.REFUSE_BILLABLE_AT_USED_PERCENT` (queue.py:224-240) says its durable
    home is `policy/standing-approvals.yaml`, and WS-E item 1 moves it there as
    `StandingApprovalsPolicy.refuse_billable_at_used_percent`. Both are
    committed unset, so preferring the policy and falling back to the constant
    is correct in every state of that migration and never under-reports a
    ceiling that is actually set.

    FOLLOW-UP: delete the `queue_module` fallback once the policy field lands.
    Recorded in `agents/handoffs/preflight.md`.
    """
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
    for name, ok, detail in checks:
        print(f"{'ok' if ok else 'FAIL':4}  {name:14} {detail}")
    print(doctor_disk_line(root))
    required = {"harbor", "docker", "docker-daemon", "uv", "task", "catalog-parquet"}
    return 0 if all(ok for name, ok, _ in checks if name in required) else 1


def _nightly_digest_enricher(root: Path, get_loop: Callable[[], ResearcherLoop]):
    def enrich(path: Path, day: date) -> None:
        get_loop().enrich_digest(path, day)
        append_gc_plan_to_digest(path, nightly_gc_plan(root))

    return enrich


def _nightly_analysis_stager(root: Path) -> Callable[[], object]:
    """Stage-only completion hook (M006): freezes identity, never calls."""

    def stage() -> object:
        from evallab.analysis_worker import default_job_roots, default_worker

        return default_worker(root).stage(default_job_roots(root))

    return stage


def _digest_renderer(root: Path) -> DigestRenderer:
    return DigestRenderer(
        repo_root=root,
        queue=DirectoryQueue(root / "queue"),
        policy=load_policy(root / "policy/standing-approvals.yaml"),
    )


# ---------------------------------------------------------------------------
# Declarative Command Handlers
# ---------------------------------------------------------------------------


def _doctor_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    if args.headless:
        executor = Executor.from_repo(root)
        report = HeadlessDoctor(root, executor=executor).run()
        print(report.model_dump_json(indent=2))
        return 0 if report.healthy else 1
    return _doctor(root)


def _dashboard_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
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


def _status_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    target = _resolve(root, args.status_from) if args.status_from is not None else root
    if getattr(args, "generate", False):
        md = generate_status_markdown(
            target,
            target_date=args.target_date,
            database_url=getattr(args, "database_url", None),
        )
        if args.status_output is not None:
            out_path = _resolve(root, args.status_output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(md)
            print(f"wrote: {out_path}")
        else:
            print(md, end="")
        return 0
    if getattr(args, "update", False):
        dest = _resolve(root, args.status_output) if args.status_output is not None else None
        out_path = update_status_file(
            target,
            target_date=args.target_date,
            destination=dest,
            database_url=getattr(args, "database_url", None),
        )
        print(f"updated: {out_path}")
        return 0
    snapshot = build_status_snapshot(target)
    if args.json:
        print(json.dumps(snapshot_as_dict(snapshot), indent=2))
    else:
        print(render_status_text(snapshot), end="")
    return 0


def _preflight_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    """Print the preflight and exit non-zero when a provider refuses billable work.

    Costs nothing and blocks on nothing: the report is built from Harbor job
    directories and `queue/` alone. `provider_reported_exhaustion` is passed in
    rather than reimplemented so this surface and the dispatch gate can never
    disagree about what the provider said.
    """
    target = _resolve(root, args.preflight_from) if args.preflight_from else root
    report = build_preflight_report(
        target,
        now=datetime.now(UTC),
        refusal=provider_reported_exhaustion,
        refuse_at_used_percent=_configured_quota_ceiling(target),
        useful_effect=args.useful_effect,
    )
    print(render_preflight(report))
    return 1 if report.refusals() else 0


def _submit_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    spec = read_spec(_resolve(root, args.path))
    executor = Executor.from_repo(root)
    path, decision = executor.submit(spec)
    # `approve`, `reject`, and the catalog's `experiment_id` column all
    # want the bare ULID. Printing the queue state directory as if it
    # were a label made the operator copy a word that no command takes
    # (M009 F-09). Read the id back off the artifact that was written.
    submitted = executor.queue.load(path)
    print(f"spec_id: {submitted.spec_id}")
    print(f"state: {path.parent.name}")
    print(f"path: {path}")
    print(decision.message)
    # A paid-authorization refusal already spells out both commands with
    # this spec's real id; repeating one of them here reads as noise.
    if path.parent.name == "waiting" and "evallab approve" not in decision.message:
        print(f"next: uv run evallab approve {shlex.quote(str(submitted.spec_id))} --actor <you>")
    return 0


def _tick_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    # if args.command == "tick": the preflight is rendered exactly once here,
    # before the guarded executor performs any dispatch.
    print(
        render_preflight(
            build_preflight_report(
                root,
                now=datetime.now(UTC),
                refusal=provider_reported_exhaustion,
                refuse_at_used_percent=_configured_quota_ceiling(root),
            )
        )
    )
    agent_caps: dict[str, int] = {}
    for raw in args.agent_capacity:
        agent, separator, value = raw.partition("=")
        if not separator or not agent or not value.isdigit() or int(value) < 1:
            print(f"invalid --agent-capacity value: {raw!r}", file=sys.stderr)
            return 2
        agent_caps[agent] = int(value)
    capacity = None
    if args.max_specs is not None or args.max_active_trials is not None or agent_caps:
        capacity = DispatchCapacity(
            max_specs_per_tick=args.max_specs,
            max_active_trials=args.max_active_trials,
            per_agent_active_trials=agent_caps or None,
        )
    executor = Executor.from_repo(
        root,
        parallel=getattr(args, "parallel", 1),
        progress=print,
        capacity=capacity,
    )
    result = GuardedTick(
        doctor=HeadlessDoctor(root, executor=executor),
        executor=executor,
    ).run()
    print(f"dispatched {result.dispatched} experiment(s)")
    print(f"quarantined: {'no' if result.report.healthy else 'yes'}")
    return 0 if result.report.healthy else 1


def _approve_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    queue = DirectoryQueue(root / "queue")
    path = queue.approve(
        args.spec_id,
        actor=args.actor,
        quota_override=args.despite_quota,
    )
    authorized = queue.load(path)
    print(f"authorized: {authorized.spec_id}")
    print(f"actor: {args.actor}")
    print(f"path: {path}")
    if authorized.billable:
        print(
            f"spend: {authorized.agent} x {authorized.attempts} attempt(s), "
            f"estimated {authorized.est_cost_usd:.2f} USD per job, billed to "
            f"{provider_subscription_description(authorized.agent)}"
        )
        # The dollar figure is an API-list-price equivalent; the provider
        # allowance or policy state is the binding account-side signal.
        headroom = _headroom_for(root, agent=authorized.agent)
        print(render_headroom_notice(headroom, agent=authorized.agent))
        # The threshold is policy, not code: `refuse_billable_at_used_percent`
        # in `policy/standing-approvals.yaml`, committed unset. Loaded
        # inline here, as `_digest_renderer` does at cli.py:1526.
        ceiling = load_policy(
            root / "policy/standing-approvals.yaml"
        ).refuse_billable_at_used_percent
        blocked = provider_reported_exhaustion(headroom) or lab_threshold_reached(
            headroom, threshold=ceiling
        )
        if blocked and not args.despite_quota:
            print(
                f"WARNING: dispatch will refuse this spec — {blocked}. "
                "Re-approve with --despite-quota only if you have reason "
                "to believe the reading is wrong."
            )
        elif args.despite_quota and not blocked:
            print(
                "note: --despite-quota was recorded, but the reading "
                "reports no exhaustion, so it overrode nothing."
            )
    if authorized.campaign_ledger is not None:
        from evallab.campaigns import CAMPAIGN_STATE_ROOT

        manifest_path = CAMPAIGN_STATE_ROOT / authorized.campaign_ledger.ledger_id / "manifest.json"
        print(f"next: uv run evallab campaign resume {manifest_path.as_posix()}")
    else:
        print("next: uv run evallab tick")
    return 0


def _print_campaign_status(status: Any, *, as_json: bool) -> None:
    if as_json:
        print(status.model_dump_json(indent=2))
        return
    print(f"campaign: {status.campaign_id}")
    print(f"benchmark: {status.benchmark}")
    print(f"manifest_digest: {status.manifest_digest}")
    print(f"state: {status.state}")
    print(f"attempts: {status.completed_attempts}/{status.total_attempts} completed")
    print(
        "usage: "
        f"${status.cost_usd:.6f}, "
        f"{status.input_tokens} input tokens, "
        f"{status.output_tokens} output tokens, "
        f"{status.wall_clock_seconds:.3f}s"
    )
    if status.circuit_reason:
        print(f"circuit: {status.circuit_reason}")
    if status.block_reason:
        print(f"blocked: {status.block_reason}")
    for attempt in status.attempts:
        print(
            f"- {attempt.attempt_id} {attempt.cell_id}/{attempt.task_id}"
            f"#{attempt.attempt}: {attempt.queue_state}"
        )
        if attempt.approval_command:
            print(f"  approve: {attempt.approval_command}")


def _campaign_plan_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    del harbor
    from evallab.campaigns import plan_campaign

    manifest, path = plan_campaign(
        _resolve(root, args.definition),
        repo_root=root,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "campaign_id": manifest.campaign_id,
                    "benchmark": manifest.benchmark,
                    "manifest_digest": manifest.manifest_digest,
                    "manifest_path": path.relative_to(root).as_posix(),
                    "attempts": len(manifest.attempts),
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"campaign_id: {manifest.campaign_id}")
        print(f"benchmark: {manifest.benchmark}")
        print(f"manifest_digest: {manifest.manifest_digest}")
        print(f"manifest: {path}")
        print(f"attempts: {len(manifest.attempts)}")
    return 0


def _campaign_orchestrator(args: argparse.Namespace, root: Path) -> Any:
    from evallab.campaigns import CAMPAIGN_STATE_ROOT, CampaignOrchestrator

    return CampaignOrchestrator.from_path(
        _resolve(root, args.manifest),
        repo_root=root,
        state_root=root / CAMPAIGN_STATE_ROOT,
        requested_parallel=getattr(args, "parallel", None),
    )


def _campaign_status_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    del harbor
    status = _campaign_orchestrator(args, root).status()
    _print_campaign_status(status, as_json=args.json)
    return 0


def _campaign_run_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    del harbor
    status = _campaign_orchestrator(args, root).run(dry_run=args.dry_run)
    _print_campaign_status(status, as_json=args.json)
    return 0


def _campaign_resume_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    del harbor
    status = _campaign_orchestrator(args, root).resume(dry_run=args.dry_run)
    _print_campaign_status(status, as_json=args.json)
    return 0


def _reject_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    path = DirectoryQueue(root / "queue").reject(
        args.spec_id, actor=args.actor, message=args.reason
    )
    print(f"rejected: {path}")
    return 0


def _stop_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    DirectoryQueue(root / "queue").stop()
    print("queue stopped")
    return 0


def _resume_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    DirectoryQueue(root / "queue").resume()
    print("queue resumed")
    return 0


def _schedule_install_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    paths = ScheduleInstaller(root).install()
    for path in paths:
        print(f"installed: {path}")
    return 0


def _digest_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    report_date = args.report_date or date.today()
    path = _digest_renderer(root).write(report_date=report_date)
    ResearcherLoop.from_repo(root).enrich_digest(path, report_date)
    append_gc_plan_to_digest(path, nightly_gc_plan(root))
    print(f"digest: {path}")
    return 0


def _research_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
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


def _nightly_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
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
        researcher_pass=lambda day: get_researcher_loop().run(report_date=day).invocation_count,
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
        analysis_stager=_nightly_analysis_stager(root),
    ).run(report_date=args.report_date)
    print(f"digest: {result.digest_path}")
    print(f"database backup: {getattr(result, 'backup_path', None) or 'not created'}")
    print(f"enqueued: {result.enqueued}")
    print(f"dispatched: {result.dispatched}")
    print(f"researcher invocations: {getattr(result, 'researcher_invocations', 0)}")
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


def _canary_import_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
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


def _calibrate_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
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


def _run_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
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


def _matrix_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
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


def _summarize_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    jobs = load_jobs([_resolve(root, path) for path in args.paths])
    if not jobs:
        print("No completed Harbor jobs found.", file=sys.stderr)
        return 1
    _print_summary(jobs)
    return 0


def _ingest_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
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


def _trajectories_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.evidence.atif import project_trial

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


def _compare_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
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


def _curve_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.curve import build_curve, load_curve_report, load_curve_spec, write_curve

    try:
        if args.curve_command == "report":
            report = load_curve_report(_resolve(root, args.path))
            print(report.model_dump_json(indent=2))
            return 0

        spec_path = _resolve(root, args.path)
        spec = load_curve_spec(spec_path)
        if args.curve_command == "validate":
            report = build_curve(spec, repo_root=root, produced_by=args.produced_by)
            print(report.model_dump_json(indent=2))
            return 0

        output = (
            _resolve(root, args.output)
            if args.output is not None
            else root / "derived" / "curves" / f"{spec.curve_id}.json"
        )
        path, report = write_curve(
            spec_path,
            repo_root=root,
            output_path=output,
            produced_by=args.produced_by,
        )
        try:
            artifact = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            artifact = path.resolve().as_posix()
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "curve_id": report.curve_id,
                    "artifact": artifact,
                    "rankable": report.rankable,
                    "refuse_to_rank_reasons": report.refuse_to_rank_reasons,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "valid": False,
                    "error": str(exc),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2


def _power_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
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
            print(f"implied pass@{args.k} difference: {comparison_pass - baseline_pass:.4f}")
        print(
            "Assumptions: independent attempts for the pass@k transformation and a normal "
            "approximation to paired task outcomes; "
            f"pair correlation={args.pair_correlation:.3f}."
        )
        print(
            "pass_at_k_probability is a model-based independent-attempt planning "
            "transform; it is not realized first-k and not Chen/Yao unbiased pass@k."
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
    print(
        "pass_at_k_probability is a model-based independent-attempt planning "
        "transform; it is not realized first-k and not Chen/Yao unbiased pass@k."
    )
    return 0


def _report_family_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
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


def _report_card_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
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


def _analyze_plan_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    job, trial = load_analysis_source(_resolve(root, args.path))
    prompt_path = root / "research/analysis/stage5-prompt.md"
    rubric_path = root / "research/analysis/stage5-rubric.json"
    output_root = _resolve(root, args.output_dir)
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


def _analyze_stub_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    job, trial = load_analysis_source(_resolve(root, args.path))
    prompt_path = root / "research/analysis/stage5-prompt.md"
    rubric_path = root / "research/analysis/stage5-rubric.json"
    output_root = _resolve(root, args.output_dir)
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
    print(f"analysis: {sidecar_path}")
    print(f"validation: {sidecar.validation_status}")
    # `--index` used to be invisible: the output was byte-identical to
    # the un-indexed form and the only way to confirm the row existed
    # was to query `analysis_invocations` by hand (M009 F-12).
    if args.index:
        url = database_url_from_environment(args.database_url)
        database.initialize(url)
        ingest_analysis_sidecar(url, sidecar_path, root=root)
        print(f"indexed analysis: {sidecar.analysis_id}")
        print(f"catalog: {database.identity(url)}")
    else:
        print("indexed: no (the catalog is a derived index, written on request)")
        print(f"next: uv run evallab analyze ingest-sidecar {shlex.quote(str(sidecar_path))}")
    return 0 if sidecar.validation_status == "valid" else 1


def _analyze_worker_plan_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.analysis_worker import default_job_roots, default_worker

    worker = default_worker(root)
    print(json.dumps(worker.plan(default_job_roots(root)), indent=2))
    return 0


def _analyze_worker_status_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.analysis_worker import default_worker

    worker = default_worker(root)
    print(json.dumps(worker.status(), indent=2))
    return 0


def _analyze_worker_run_one_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.analysis_worker import default_worker
    from evallab.evidence.facts import CodexExecAnalyzer

    adapter_factory = None
    if args.adapter == "codex-exec":
        if args.authorization is None:
            print("error: --authorization is required for codex-exec", file=sys.stderr)
            return 2
        authorization_path = _resolve(root, args.authorization)
        scratch_root = _resolve(root, args.scratch_dir)

        def adapter_factory(job, trial, request):
            return CodexExecAnalyzer(
                repo_root=root,
                trial=trial,
                model=request.model,
                authorization_path=authorization_path,
                scratch_dir=scratch_root / request.request_id,
            )

    worker = default_worker(root, adapter_factory=adapter_factory)
    transition = worker.run_one(args.request_id)
    print(json.dumps({"state": transition.state, "reason": transition.reason}))
    return 0 if transition.state == "completed" else 1


def _analyze_worker_resolve_ambiguous_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.analysis_worker import default_worker

    worker = default_worker(root)
    transition = worker.resolve_ambiguous(
        args.request_id,
        action=args.action,
        actor=args.actor,
    )
    print(json.dumps({"state": transition.state, "reason": transition.reason}))
    return 0


def _analyze_ingest_sidecar_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    sidecar_path = _resolve(root, args.path)
    url = database_url_from_environment(args.database_url)
    database.initialize(url)
    sidecar = ingest_analysis_sidecar(url, sidecar_path, root=root)
    reviews = len(list((sidecar_path.parent / ANALYSIS_REVIEWS_DIRNAME).glob("*.json")))
    print(f"indexed analysis: {sidecar.analysis_id}")
    print(f"indexed reviews: {reviews}")
    print(f"catalog: {database.identity(url)}")
    return 0


def _analyze_review_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    sidecar_path = _resolve(root, args.path)
    if not sidecar_path.is_file():
        raise ValueError(
            f"no analysis sidecar at {sidecar_path}; pass the "
            f"{ANALYSIS_SIDECAR_FILENAME} path printed by "
            "`evallab analyze stub` "
            "(derived/analyses/<analysis_id>/analysis.json)"
        )
    review_path, review = write_analysis_review(
        sidecar_path,
        disposition=args.disposition,
        rationale=args.rationale,
        reviewer=args.reviewer,
        superseded_by=(UUID(args.superseded_by) if args.superseded_by else None),
    )
    print(f"review: {review_path}")
    print(f"disposition: {review.disposition}")
    # The catalog is a derived index, so indexing stays opt-in — but the
    # operator is told which state they are in, never left to discover
    # that `analysis_reviews` is empty (M009 F-02).
    if args.index:
        url = database_url_from_environment(args.database_url)
        database.initialize(url)
        ingest_analysis_sidecar(url, sidecar_path, root=root)
        print(f"indexed review: {review.review_id} -> analysis_reviews")
        print(f"catalog: {database.identity(url)}")
    else:
        print("indexed: no (the catalog is a derived index, written on request)")
        print(f"next: uv run evallab analyze ingest-sidecar {shlex.quote(str(sidecar_path))}")
    return 0


def _analyze_agreement_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
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


def _analyze_trial_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    del harbor
    store_root = _resolve(root, args.store)
    output_dir = _resolve(root, args.output_dir)
    derived_root = output_dir.parent
    calibration_report = (
        _resolve(root, args.calibration_report) if args.calibration_report else None
    )

    target: dict[str, Any] | str
    if args.cas_uri:
        target = {"cas_uri": args.cas_uri}
    elif args.inventory and args.trial_id:
        inv_path = _resolve(root, args.inventory)
        if inv_path.is_file():
            inv_data = json.loads(inv_path.read_text(encoding="utf-8"))
            if isinstance(inv_data, dict) and "analysis_cohort_5_trials" in inv_data:
                manifest = trajectory_runtime.load_campaign_analysis_manifest(inv_path)
                item = next((i for i in manifest.items if i.trial_id == args.trial_id), None)
                if item is None:
                    raise ValueError(f"trial_id {args.trial_id} not found in inventory")
                target = item.as_inventory_dict()
            else:
                records = inv_data if isinstance(inv_data, list) else []
                record = next((r for r in records if r.get("trial_id") == args.trial_id), None)
                if record is None:
                    raise ValueError(f"trial_id {args.trial_id} not found in inventory")
                target = record
        else:
            raise ValueError(f"inventory not found: {args.inventory}")
    else:
        raise ValueError("must pass --cas-uri or --inventory with --trial-id")

    result = trajectory_runtime.analyze_trial(
        target,
        repo_root=root,
        store_root=store_root,
        output_dir=output_dir,
        derived_root=derived_root,
        database_url=args.database_url,
        calibration_report=calibration_report,
    )
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


def _analyze_batch_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    del harbor
    inventory = _resolve(root, args.inventory)
    store_root = _resolve(root, args.store)
    output_dir = _resolve(root, args.output_dir)
    derived_root = output_dir.parent
    calibration_report = (
        _resolve(root, args.calibration_report) if args.calibration_report else None
    )
    report = trajectory_runtime.analyze_batch(
        inventory,
        repo_root=root,
        store_root=store_root,
        output_dir=output_dir,
        derived_root=derived_root,
        database_url=args.database_url,
        calibration_report=calibration_report,
    )
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


def _analyze_inspect_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    del harbor
    output_dir = _resolve(root, args.output_dir)
    store_root = _resolve(root, args.store)
    result = trajectory_runtime.analyze_inspect(
        args.target,
        output_dir=output_dir,
        store_root=store_root,
        repo_root=root,
    )
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


def _analyze_calibrate_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    del harbor
    report_path = _resolve(root, args.path)
    result = trajectory_runtime.analyze_calibrate(report_path)
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


def _select_json_fields(payload: dict[str, Any], fields: str) -> dict[str, Any]:
    """Select named object paths for concise operator output."""
    expression = fields.strip()
    if not (expression.startswith("{") and expression.endswith("}")):
        raise ValueError("--fields must use {name:.path,...} syntax")
    selected: dict[str, Any] = {}
    body = expression[1:-1].strip()
    if not body:
        return selected
    for raw_entry in body.split(","):
        entry = raw_entry.strip()
        if ":" not in entry:
            raise ValueError(f"invalid --fields entry: {entry!r}")
        name, raw_path = (part.strip() for part in entry.split(":", 1))
        if not name or not raw_path.startswith("."):
            raise ValueError(f"invalid --fields entry: {entry!r}")
        if name in selected:
            raise ValueError(f"duplicate --fields name: {name}")
        value: Any = payload
        for segment in raw_path.removeprefix(".").split("."):
            if not segment or not isinstance(value, dict) or segment not in value:
                raise ValueError(f"unknown --fields path: {raw_path}")
            value = value[segment]
        selected[name] = value
    return selected


def _analyze_quality_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    del harbor
    from evallab.interpretation.trajectory_data_quality import campaign_data_quality_report

    inventory = _resolve(root, args.inventory)
    store_root = _resolve(root, args.store)
    output_dir = _resolve(root, args.output_dir)
    derived_root = _resolve(root, args.derived_root) if args.derived_root else output_dir.parent
    result = campaign_data_quality_report(
        inventory,
        repo_root=root,
        store_root=store_root,
        output_dir=output_dir,
        derived_root=derived_root,
        database_url=args.database_url,
    )
    rendered = _select_json_fields(result, args.fields) if args.fields else result
    print(json.dumps(rendered, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


def _data_backfill_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    del harbor
    from evallab.storage.data_backfill import run_all_durable_backfill

    inventory = _resolve(root, args.inventory)
    manifest_dir = _resolve(root, args.manifest_dir)
    store_root = _resolve(root, args.store_root)
    output_dir = _resolve(root, args.output_dir)
    derived_root = _resolve(root, args.derived_root) if args.derived_root else output_dir.parent
    ledger = run_all_durable_backfill(
        inventory_path=inventory,
        manifest_dir=manifest_dir,
        repo_root=root,
        store_root=store_root,
        output_dir=output_dir,
        derived_root=derived_root,
        database_url=args.database_url,
    )
    print(
        f"data backfill: {ledger.disposition_count} dispositions "
        f"({ledger.ready_count} ANALYSIS_READY, {ledger.hold_count} HOLD) "
        f"exit={ledger.exit_code} digest={ledger.content_digest}"
    )
    return ledger.exit_code


def _db_init_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    url = database_url_from_environment(args.database_url)
    database.initialize(url)
    print("database schema is current")
    return 0


def _db_list_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    url = database_url_from_environment(args.database_url)
    rows = database.list_trials(url, limit=args.limit)
    print("| job | trial | task | agent | model | reward | exception | seconds |")
    print("|---|---|---|---|---|---:|---|---:|")
    for row in rows:
        print("| " + " | ".join("" if value is None else str(value) for value in row) + " |")
    return 0


_URI_PASSWORD_QUERY = re.compile(r"(?i)([?&](?:password|sslpassword)=)([^&#]*)")
_KEYWORD_PASSWORD = re.compile(
    r"(?i)(\b(?:password|sslpassword)\s*=\s*)"
    r"(?:'(?:\\.|''|[^'\\])*'|\"(?:\\.|\"\"|[^\"\\])*\"|(?:\\.|[^\s])+)"
)


def _redact_database_dsn(dsn: str) -> tuple[str, bool]:
    """Remove password values from URI and libpq keyword DSNs."""
    redacted = dsn
    had_credentials = False

    scheme_end = redacted.find("://")
    if scheme_end >= 0:
        authority_start = scheme_end + 3
        authority_end = len(redacted)
        for delimiter in "/?#":
            position = redacted.find(delimiter, authority_start)
            if position >= 0:
                authority_end = min(authority_end, position)
        authority = redacted[authority_start:authority_end]
        at = authority.rfind("@")
        if at >= 0:
            userinfo = authority[:at]
            separator = userinfo.find(":")
            if separator >= 0:
                authority = userinfo[:separator] + ":<REDACTED>@" + authority[at + 1 :]
                redacted = redacted[:authority_start] + authority + redacted[authority_end:]
                had_credentials = True

    redacted, query_count = _URI_PASSWORD_QUERY.subn(r"\1<REDACTED>", redacted)
    redacted, keyword_count = _KEYWORD_PASSWORD.subn(r"\1'<REDACTED>'", redacted)
    return redacted, had_credentials or query_count > 0 or keyword_count > 0


def _db_attach_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    # thin layer over attach/print_zones/attach_and_query/build_sql_preamble
    explicit = getattr(args, "derived_root", None)
    derived = derived_root_from_environment(root, explicit=explicit)
    result = attach(repo_root=root, explicit_derived=derived)
    if args.zones:
        print_zones(result.zones)
        attached = sum(1 for z in result.zones if z.attached)
        result.connection.close()
        return 0 if attached > 0 else 1
    if args.print_sql:
        dsn = database_url_from_environment()
        safe_dsn, had_credentials = _redact_database_dsn(dsn)
        if had_credentials:
            print("-- REDACTED / NON-EXECUTABLE: credentials were removed from this SQL preamble.")
        print(build_sql_preamble(safe_dsn, derived, root))
        result.connection.close()
        return 0
    if args.query:
        result.connection.close()
        rows = attach_and_query(args.query, repo_root=root, explicit_derived=derived)
        for row in rows:
            print(row)
        return 0
    result.connection.close()
    return 0


def _lineage_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    explicit = getattr(args, "derived_root", None)
    derived = derived_root_from_environment(root, explicit=explicit)
    result = resolve_lineage(args.target, repo_root=root, explicit_derived=derived)
    if args.json:
        print(json.dumps(lineage_to_dict(result), indent=2))
    else:
        print(render_lineage_tree(result))
    return 0 if result.resolved else 1


def _analyst_run_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.analyst import run_analysis

    explicit_derived = getattr(args, "derived_root", None)
    derived = derived_root_from_environment(root, explicit=explicit_derived)
    record, traj_data, conclusion_file, trajectory_file = run_analysis(
        args.trial_id,
        model=args.model,
        repo_root=root,
        derived_root=derived,
        runs_root=getattr(args, "runs_root", None),
    )
    print(f"analysis_id: {record.analysis_id}")
    print(f"trial_id: {record.trial_id}")
    print(f"model: {record.model}")
    print(f"category: {record.category}")
    print(f"confidence: {record.confidence.level}")
    print(f"evidence: {len(record.evidence)} citation(s)")
    print(f"conclusion: {conclusion_file}")
    print(f"trajectory: {trajectory_file}")
    return 0


def _analyst_list_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.analyst import list_analyses

    records = list_analyses(root, trial_id=args.trial_id)
    if not records:
        print("No analysis records found.")
        return 0
    print("| analysis_id | trial_id | model | category | confidence | evidence |")
    print("|---|---|---|---|---|---:|")
    for r in records:
        print(
            f"| {r['analysis_id']} | {r['trial_id']} | {r['model']} | "
            f"{r['category']} | {r['confidence']} | {r['evidence_count']} |"
        )
    return 0


def _analyst_show_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.analyst import show_analysis

    try:
        conclusion, trajectory = show_analysis(args.analysis_id, root)
    except FileNotFoundError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        payload = {"conclusion": conclusion, "trajectory": trajectory}
        print(json.dumps(payload, indent=2))
        return 0
    print(f"# Analysis {conclusion.get('analysis_id')}")
    print(f"Trial ID: {conclusion.get('trial_id')}")
    print(f"Model: {conclusion.get('model')}")
    print(f"Category: {conclusion.get('category')}")
    conf = conclusion.get("confidence") or {}
    conf_level = conf.get("level") if isinstance(conf, dict) else str(conf)
    print(f"Confidence: {conf_level}")
    print(f"Summary: {conclusion.get('summary', '')}")
    print("\n## Cited Evidence:")
    for ev in conclusion.get("evidence", []):
        step_info = f" (step {ev['step']})" if ev.get("step") is not None else ""
        print(f"- {ev.get('path')}{step_info}")
    print("\n## Lineage Inputs:")
    for inp in conclusion.get("inputs", []):
        print(f"- {inp.get('path')} ({inp.get('digest')})")
    print("\n## Analyst Trajectory Steps:")
    for step in trajectory.get("steps", []):
        sid = step.get("step_id")
        src = step.get("source")
        ts = step.get("timestamp")
        msg = step.get("message")
        print(f"[{sid}] {src} ({ts}): {msg}")
    return 0


def _card_generate_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.cards import generate_card

    explicit = getattr(args, "derived_root", None)
    derived = derived_root_from_environment(root, explicit=explicit)
    rendered, card_data = generate_card(
        args.target,
        repo_root=root,
        explicit_derived=derived,
        output_path=args.output,
    )
    if args.json:
        print(json.dumps(card_data, indent=2))
        return 0
    if args.output is None:
        print(rendered)
    else:
        print(f"eval card: {args.output}")
        print(f"config digest: {card_data.get('spec_digest')}")
    return 0


def _card_validate_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.cards import validate_card_file

    card_path = _resolve(root, args.path)
    result = validate_card_file(card_path)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0 if result.valid else 1
    if result.valid:
        print(f"VALID: {args.path} passed all schema and caveat checks.")
        return 0
    print(f"INVALID: {args.path} failed validation:", file=sys.stderr)
    for err in result.errors:
        print(f"  - {err}", file=sys.stderr)
    return 1


def _behavior_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.behavior import (
        generate_behavior_report,
        render_behavior_report,
        report_to_dict,
    )

    explicit = getattr(args, "derived_root", None)
    derived = derived_root_from_environment(root, explicit=explicit)
    report = generate_behavior_report(
        repo_root=root,
        explicit_derived=derived,
        task_filter=args.task,
        agent_filter=args.agent,
    )
    if args.json:
        print(json.dumps(report_to_dict(report), indent=2))
    else:
        print(render_behavior_report(report))
    return 0


def _semantic_facts_project_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.semantic_facts import load_fact_bundle, project_fact_bundle

    bundle = load_fact_bundle(_resolve(root, args.bundle))
    paths = project_fact_bundle(bundle, _resolve(root, args.output_dir))
    if args.json:
        print(json.dumps({name: str(path) for name, path in paths.items()}, sort_keys=True))
    else:
        print(f"Projected {len(paths)} semantic fact tables to {args.output_dir}")
    return 0


def _semantic_facts_query_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.semantic_facts import query_scorecard

    rows = query_scorecard(
        _resolve(root, args.output_dir),
        benchmark=args.benchmark,
        construct=args.construct,
    )
    print(json.dumps(rows, indent=2, sort_keys=True))
    return 0


def _semantics_bindings(values: Sequence[str]):
    from evallab.interpretation.trajectory_semantics import (
        TaskProfileBinding,
        get_profile,
    )

    bindings = []
    for value in values:
        task_id, separator, profile_id = value.partition("=")
        if not separator or not task_id.strip() or not profile_id.strip():
            raise ValueError("--bind must use TASK_ID=PROFILE_ID")
        profile = get_profile(profile_id.strip())
        bindings.append(TaskProfileBinding.from_profile(task_id.strip(), profile))
    return bindings


def _semantics_project_command(
    args: argparse.Namespace,
    root: Path,
    *,
    harbor: HarborBackend | None = None,
) -> int:
    del harbor
    from evallab.interpretation.trajectory_semantics import project_job_semantics

    jobs = load_jobs([_resolve(root, path) for path in args.paths])
    if not jobs:
        print("No completed Harbor jobs found.", file=sys.stderr)
        return 1
    derived = derived_root_from_environment(
        root,
        explicit=args.output_dir,
    )
    result = project_job_semantics(
        jobs,
        bindings=_semantics_bindings(args.bind),
        output_root=derived,
        query_threshold=args.coverage_threshold,
        strict=not args.permissive,
    )
    payload = {
        "files": [str(path) for path in result.files],
        "coverage": [row.model_dump(mode="json") for row in result.coverage],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"projected {len(result.files)} semantic file(s) to {derived}")
        for row in result.coverage:
            print(
                f"{row.trial_id}: {row.coverage_fraction:.3f} "
                f"at threshold {row.query_threshold:.3f} — {row.status}"
            )
    return 0


def _semantics_coverage_command(
    args: argparse.Namespace,
    root: Path,
    *,
    harbor: HarborBackend | None = None,
) -> int:
    del harbor
    from evallab.interpretation.trajectory_semantics import query_semantic_coverage

    derived = derived_root_from_environment(
        root,
        explicit=args.derived_dir,
    )
    rows = query_semantic_coverage(
        derived,
        query_threshold=args.threshold,
    )
    print(
        json.dumps(
            [row.model_dump(mode="json") for row in rows],
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _evidence_archive_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.evidence_store import archive_evidence

    source = _resolve(root, args.source)
    archive = archive_evidence(
        source,
        _resolve(root, args.store),
        record_id=args.record_id or source.name,
        kind=args.kind,
    )
    payload = {
        "record_id": archive.record_id,
        "kind": archive.kind,
        "uri": archive.uri,
        "content_digest": archive.content_digest,
        "archive_digest": archive.archive_digest,
        "blob_path": str(archive.blob_path),
        "manifest_path": str(archive.manifest_path),
        "file_count": archive.file_count,
        "uncompressed_bytes": archive.uncompressed_bytes,
    }
    print(json.dumps(payload, indent=2) if args.json else archive.uri)
    return 0


def _evidence_restore_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.evidence_store import restore_evidence

    destination = restore_evidence(
        _resolve(root, args.store),
        args.uri,
        _resolve(root, args.destination),
    )
    print(destination)
    return 0


def _tasks_import_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.task_import import import_task_batch

    report = import_task_batch(
        _resolve(root, args.source),
        _resolve(root, args.destination),
        _resolve(root, args.ledger),
        limit=args.limit,
    )
    payload = {
        "discovered": report.discovered,
        "imported": report.imported,
        "skipped": report.skipped,
        "failed": report.failed,
        "items": [
            {
                "source": str(item.source),
                "source_digest": item.source_digest,
                "destination": str(item.destination) if item.destination else None,
                "status": item.status,
                "reason": item.reason,
            }
            for item in report.items
        ],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"tasks: {report.discovered} discovered, {report.imported} imported, "
            f"{report.skipped} resumed, {report.failed} failed"
        )
    return 1 if report.failed else 0


def _ladder_validate_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.ladder import generate_grid, load_grid_spec

    try:
        grid_spec = load_grid_spec(_resolve(root, args.grid_spec))
        result = generate_grid(
            grid_spec,
            repo_root=root,
            dry_run=True,
            check_quota_headroom=False,
        )
    except Exception as exc:
        if args.json:
            print(json.dumps({"valid": False, "errors": [str(exc)]}, indent=2))
        else:
            print(f"invalid: {exc}")
        return 1

    remaining = result.total_specs
    deduped = len(result.deduped)
    skipped = len(result.skipped)
    declared = remaining + deduped + skipped
    payload = {
        "valid": True,
        "grid_id": result.grid_id,
        "errors": [],
        "declared": declared,
        "remaining": remaining,
        "deduped": deduped,
        "skipped": skipped,
        "declared_spec_count": declared,
        "remaining_spec_count": remaining,
        "deduped_spec_count": deduped,
        "skipped_spec_count": skipped,
        "spec_count": remaining,
        "trial_count": result.total_trials,
        "shard_count": len(result.shards),
        "normalized_spec": grid_spec.model_dump(mode="json"),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"valid: {result.grid_id} "
            f"({declared} declared, {remaining} remaining, {deduped} deduped, "
            f"{skipped} skipped; {result.total_trials} remaining trials, "
            f"{len(result.shards)} shards)"
        )
    return 0


def _ladder_generate_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.ladder import generate_grid, load_grid_spec

    grid_path = _resolve(root, args.grid_spec)
    grid_spec = load_grid_spec(grid_path)
    if args.no_quota_check:
        grid_spec = grid_spec.model_copy(update={"check_quota_headroom": False})

    dry_run = args.dry_run or (not args.submit and args.output_dir is None)
    output_dir = _resolve(root, args.output_dir) if args.output_dir else None

    result = generate_grid(
        grid_spec,
        output_dir=output_dir,
        repo_root=root,
        submit=args.submit,
        dry_run=dry_run,
    )

    if args.json:
        out_data = {
            "grid_id": result.grid_id,
            "total_specs": result.total_specs,
            "total_trials": result.total_trials,
            "total_estimated_cost_usd": result.total_estimated_cost_usd,
            "specs": [s.model_dump(mode="json") for s in result.specs],
            "shards": [
                {
                    "shard_id": shard.shard_id,
                    "index": shard.index,
                    "spec_names": list(shard.spec_names),
                    "trial_count": shard.trial_count,
                    "estimated_cost_usd": shard.estimated_cost_usd,
                    "sha256": shard.sha256,
                    "path": str(shard.path) if shard.path else None,
                }
                for shard in result.shards
            ],
            "skipped": [
                {
                    "name": sk.name,
                    "task": sk.task,
                    "agent": sk.agent,
                    "preamble": sk.preamble,
                    "attempts": sk.attempts,
                    "reason": sk.reason,
                    "arm_id": sk.arm_id,
                    "factor_values": sk.factor_values,
                }
                for sk in result.skipped
            ],
            "deduped": [
                {
                    "grid_id": d.grid_id,
                    "task": d.task,
                    "agent": d.agent,
                    "preamble": d.preamble,
                    "attempts": d.attempts,
                    "reason": d.reason,
                    "arm_id": d.arm_id,
                    "factor_values": d.factor_values,
                }
                for d in result.deduped
            ],
            "written_files": [str(p) for p in result.written_paths],
            "submitted_specs": result.submitted_specs,
        }
        print(json.dumps(out_data, indent=2))
    else:
        print(result.summary())
    return 0


def _ladder_screen_stage1_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.ladder import generate_stage1_screen, load_screen_spec

    spec_path = _resolve(root, args.spec)
    screen_spec = load_screen_spec(spec_path)
    dry_run = args.dry_run or (not args.submit and args.output_dir is None)
    output_dir = _resolve(root, args.output_dir) if args.output_dir else None

    result = generate_stage1_screen(
        screen_spec,
        repo_root=root,
        output_dir=output_dir,
        submit=args.submit,
        dry_run=dry_run,
    )

    if args.json:
        out = {
            "screen_id": result.grid_id,
            "stage": 1,
            "total_specs": result.total_specs,
            "total_trials": result.total_trials,
            "specs": [s.model_dump(mode="json") for s in result.specs],
            "written_files": [str(p) for p in result.written_paths],
        }
        print(json.dumps(out, indent=2))
    else:
        print(f"LADDER Screen Stage 1 Generation: {result.grid_id}")
        print(
            f"Generated {result.total_specs} specs "
            f"({result.total_trials} trials, k={screen_spec.initial_k})"
        )
        print(f"Tasks: {len(screen_spec.tasks)} | Model levels: {len(screen_spec.model_levels)}")
        if result.written_paths:
            print(
                f"Written to: {result.written_paths[0].parent} ({len(result.written_paths)} files)"
            )
        elif dry_run:
            print("Dry-run mode: no files written to disk.")
        print("Human approval preserved (pending review before dispatch).")
    return 0


def _ladder_screen_analyze_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.ladder import analyze_screen_results, load_screen_spec

    target = args.screen_id_or_spec
    target_path = _resolve(root, Path(target))
    screen_id = target
    spec_obj = None
    if target_path.is_file():
        spec_obj = load_screen_spec(target_path)
        screen_id = spec_obj.screen_id
    jobs_dir = _resolve(root, args.jobs_dir) if args.jobs_dir else None

    report = analyze_screen_results(
        screen_id,
        spec=spec_obj,
        repo_root=root,
        jobs_dir=jobs_dir,
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.summary())
    return 0


def _ladder_screen_stage2_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.ladder import (
        analyze_screen_results,
        generate_stage2_screen,
        load_screen_spec,
    )

    spec_path = _resolve(root, args.spec)
    screen_spec = load_screen_spec(spec_path)
    jobs_dir = _resolve(root, args.jobs_dir) if args.jobs_dir else None

    report = analyze_screen_results(
        screen_spec.screen_id,
        spec=screen_spec,
        repo_root=root,
        jobs_dir=jobs_dir,
    )

    dry_run = args.dry_run or (not args.submit and args.output_dir is None)
    output_dir = _resolve(root, args.output_dir) if args.output_dir else None

    result = generate_stage2_screen(
        report,
        screen_spec,
        repo_root=root,
        output_dir=output_dir,
        submit=args.submit,
        dry_run=dry_run,
    )

    if args.json:
        out = {
            "screen_id": result.grid_id,
            "stage": 2,
            "separating_tasks": report.separating_tasks,
            "stopped_tasks": report.stopped_tasks,
            "total_specs": result.total_specs,
            "total_trials": result.total_trials,
            "specs": [s.model_dump(mode="json") for s in result.specs],
            "written_files": [str(p) for p in result.written_paths],
        }
        print(json.dumps(out, indent=2))
    else:
        print(f"LADDER Screen Stage 2 Follow-Up Generation: {result.grid_id}")
        sep_str = ", ".join(report.separating_tasks) or "none"
        stop_str = ", ".join(report.stopped_tasks) or "none"
        print(
            f"Separating tasks selected for follow-up ({len(report.separating_tasks)}): {sep_str}"
        )
        print(f"Stopped tasks ({len(report.stopped_tasks)}): {stop_str}")
        print("Task decisions:")
        for task_result in report.tasks:
            action = "SELECTED for Stage 2" if task_result.selected_for_followup else "STOPPED"
            print(f"  - {task_result.task_id}: {action} — {task_result.followup_reason}")
        print(
            f"Generated {result.total_specs} follow-up specs "
            f"({result.total_trials} trials, k={screen_spec.followup_k})"
        )
        if result.written_paths:
            print(
                f"Written to: {result.written_paths[0].parent} ({len(result.written_paths)} files)"
            )
        elif dry_run:
            print("Dry-run mode: no files written to disk.")
        print("Human approval preserved (no automatic paid dispatch).")
    return 0


def _trace_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
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


def _fetch_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
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


def _gc_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    plan, applied = run_gc(
        root,
        apply=args.apply,
        runs_dir=_resolve(root, args.runs_dir),
    )
    print(format_plan(plan))
    if applied is not None:
        print(f"applied: {len(applied.tombstones)} tombstone(s)")
    return 0


def _registry_list_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
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


def _tidy_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.tidy import run_tidy

    apply = args.apply and not args.dry_run
    return run_tidy(root, apply=apply)


def _verdict_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.verdicts import (
        format_verdict_history_table,
        format_verdicts_table,
        get_verdict_history_from_catalog,
        list_current_verdicts_from_catalog,
        record_verdict,
    )

    action = args.action_or_id
    if action is None:
        for act in parser()._actions:
            if isinstance(act, argparse._SubParsersAction) and "verdict" in act.choices:
                print(act.choices["verdict"].format_help())
                return 0
        return 0
    db_url = getattr(args, "database_url", None)

    if action == "list":
        status_filter = args.status or args.status_or_id
        try:
            verdicts = list_current_verdicts_from_catalog(database_url=db_url, status=status_filter)
        except Exception:
            if db_url is not None:
                raise
            verdicts = []
        if args.json:
            print(json.dumps([v.model_dump(mode="json") for v in verdicts], indent=2))
        else:
            print(format_verdicts_table(verdicts))
        return 0

    if action == "history":
        target_id = args.status_or_id
        if not target_id:
            print(
                "error: discovery_id is required for 'verdict history <discovery_id>'",
                file=sys.stderr,
            )
            return 2
        try:
            history = get_verdict_history_from_catalog(target_id, database_url=db_url)
        except Exception:
            if db_url is not None:
                raise
            history = []
        if args.json:
            print(json.dumps([v.model_dump(mode="json") for v in history], indent=2))
        else:
            print(format_verdict_history_table(target_id, history))
        return 0

    discovery_id = action
    status = args.status_or_id
    if not status:
        print(
            "error: status is required: evallab verdict <discovery_id> "
            "<accepted|rejected|needs_evidence|pending> --by <who>",
            file=sys.stderr,
        )
        return 2
    if not args.by:
        print("error: --by <who> is required for recording a verdict", file=sys.stderr)
        return 2

    verdict = record_verdict(
        discovery_id,
        status,
        by=args.by,
        note=args.note,
        repo_root=root,
        database_url=db_url,
    )
    if args.json:
        print(json.dumps(verdict.model_dump(mode="json"), indent=2))
    else:
        note_suffix = f" (note: {verdict.note})" if verdict.note else ""
        print(
            f"Recorded verdict for {verdict.discovery_id}: {verdict.status} "
            f"by {verdict.by}{note_suffix}"
        )
    return 0


def _traj_outline_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    try:
        outline = outline_trajectory(args.target, repo_root=root)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(outline.to_dict(), indent=2))
    else:
        print(render_outline(outline, verbose=args.verbose))
    return 0 if outline.status == "featured" else 1


def _traj_queue_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    runs_roots = [_resolve(root, args.runs_dir)] if args.runs_dir else None
    queue_items = select_review_queue(
        limit=args.limit,
        runs_roots=runs_roots,
        repo_root=root,
    )
    if args.json:
        print(json.dumps([asdict(item) for item in queue_items], indent=2))
    else:
        if not queue_items:
            print("No unlabeled trajectories in review queue.")
            return 0
        print(f"TRAJECTORY REVIEW QUEUE ({len(queue_items)} items):")
        print("=" * 80)
        for idx, item in enumerate(queue_items, start=1):
            print(
                f"{idx}. [{item.task_name}] {item.trial_name} ({item.agent_name}/{item.model_name})"
            )
            print(f"   Signals:    {item.outline_preview}")
            print(f"   Suggested:  {item.suggested_taxonomy} ({item.suggestion_reason})")
            print(f"   Action:     {item.next_command}")
            print("")
    return 0


def _traj_label_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    try:
        label = label_trajectory(
            args.trial,
            label=args.label,
            note=args.note,
            provenance=args.provenance,
            author=args.author,
            repo_root=root,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(label.model_dump_json(indent=2))
    else:
        note_str = f" (note: {label.rationale})" if label.rationale else ""
        print(
            f"Recorded label: {label.trial_name} -> {label.label} "
            f"by {label.author} [{label.provenance}]{note_str}"
        )
    return 0


def _traj_project_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    runs_roots = [_resolve(root, r) for r in args.runs_dir] if args.runs_dir else None
    out_dir = _resolve(root, args.output_dir) if args.output_dir else None
    res = project_trajectory_features(runs_roots=runs_roots, output_root=out_dir, repo_root=root)
    if args.json:
        print(json.dumps(asdict(res), indent=2, default=str))
    else:
        print(f"Projected {res.table_rows} trajectory feature rows to {res.output_path}")
        print(f"  Featured:    {res.featured_count}")
        print(f"  Unavailable: {res.unavailable_count}")
        print(f"  Digest:      {res.sha256[:16]}...")
    return 0


def _traj_report_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    report = evaluate_heuristic_precision(repo_root=root)
    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        print("HEURISTIC PRECISION REPORT:")
        print(f"  Human Labels:        {report.human_label_count}")
        print(f"  Heuristic Proposals: {report.heuristic_proposal_count}")
        print(f"  Matched Trials:      {report.matched_trials_count}")
        print(f"  Exact Matches:       {report.exact_taxonomy_matches}")
        pct = f"{report.precision * 100:.1f}%" if report.matched_trials_count > 0 else "N/A"
        print(f"  Precision:           {pct}")
        if report.disagreements:
            print(f"\nDISAGREEMENTS ({len(report.disagreements)}):")
            for d in report.disagreements:
                t_name = d["task_name"]
                t_id = d["trial_id"]
                h_tax = d["human_label"]
                he_tax = d["heuristic_label"]
                print(f"  - {t_name} ({t_id}): human={h_tax!r} vs heuristic={he_tax!r}")
    return 0


def _traj_card_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.interpretation.benchmark_events import ingest_benchmark_trial
    from evallab.interpretation.benchmark_projection import (
        agent_readable_projection_provenance,
        build_projection_dimensions,
        load_compliance_report,
    )
    from evallab.interpretation.traj_card import generate_traj_card
    from evallab.interpretation.trajectory_hydration import RedactionPolicy
    from evallab.traj import resolve_trial_target

    runs_roots = [_resolve(root, args.runs_dir)] if args.runs_dir else None
    output_path = _resolve(root, args.output) if args.output else None
    fmt = "json" if args.json else "markdown"
    policy = RedactionPolicy(redact_secrets=not args.no_redact)

    try:
        projection_provenance = None
        projection_dimensions = None
        if getattr(args, "compliance_report", None):
            report = load_compliance_report(_resolve(root, args.compliance_report))
            metadata = (
                json.loads(_resolve(root, args.projection_metadata).read_text(encoding="utf-8"))
                if getattr(args, "projection_metadata", None)
                else {}
            )
            if not isinstance(metadata, dict):
                raise ValueError("projection metadata must be a JSON object")
            trial_dir, _, _ = resolve_trial_target(
                args.trial, repo_root=root, explicit_runs_root=runs_roots[0] if runs_roots else None
            )
            projection_dimensions = build_projection_dimensions(
                ingest_benchmark_trial(trial_dir), report, metadata=metadata
            )
            projection_provenance = agent_readable_projection_provenance(
                report, projection_dimensions
            )

        rendered, _card = generate_traj_card(
            target=args.trial,
            repo_root=root,
            runs_roots=runs_roots,
            output_path=output_path,
            output_format=fmt,
            policy=policy,
            projection_provenance=projection_provenance,
            projection_dimensions=projection_dimensions,
        )
        print(rendered)
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _traj_ir_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.interpretation.trajectory_ir import build_trajectory_ir

    explicit_root = _resolve(root, args.runs_dir) if getattr(args, "runs_dir", None) else None
    if explicit_root is None:
        try:
            target_path = Path(args.trial)
            target_resolved = target_path.resolve()
            repo_resolved = root.resolve()
            if target_resolved != repo_resolved and repo_resolved not in target_resolved.parents:
                explicit_root = (
                    target_resolved.parent if target_resolved.is_file() else target_resolved
                )
        except Exception:
            pass

    store_root = explicit_root if (explicit_root and (explicit_root / "blobs").exists()) else None
    try:
        ir = build_trajectory_ir(
            args.trial, repo_root=root, explicit_runs_root=explicit_root, store_root=store_root
        )
        output_str = json.dumps(ir.to_dict(), indent=2)
        if getattr(args, "output", None):
            out_path = _resolve(root, args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(output_str, encoding="utf-8")
            print(f"Wrote TrajectoryIR (digest: {ir.ir_digest}) -> {out_path}")
        else:
            print(output_str)
        return 0 if ir.status == "featured" else 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _traj_pack_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.interpretation.evidence_pack import build_evidence_pack
    from evallab.interpretation.trajectory_hydration import RedactionPolicy
    from evallab.interpretation.trajectory_ir import build_trajectory_ir

    explicit_root = _resolve(root, args.runs_dir) if getattr(args, "runs_dir", None) else None
    if explicit_root is None:
        try:
            target_path = Path(args.trial)
            target_resolved = target_path.resolve()
            repo_resolved = root.resolve()
            if target_resolved != repo_resolved and repo_resolved not in target_resolved.parents:
                explicit_root = (
                    target_resolved.parent if target_resolved.is_file() else target_resolved
                )
        except Exception:
            pass

    budget = getattr(args, "budget", 16000) or 16000
    policy = RedactionPolicy(redact_secrets=not getattr(args, "no_redact", False))

    store_root = explicit_root if (explicit_root and (explicit_root / "blobs").exists()) else None
    try:
        ir = build_trajectory_ir(
            args.trial, repo_root=root, explicit_runs_root=explicit_root, store_root=store_root
        )
        trial_str = str(args.trial)
        trial_dir: Path | None = None
        if not trial_str.startswith("cas://"):
            try:
                tp = Path(args.trial)
                if tp.exists():
                    trial_dir = tp if tp.is_dir() else tp.parent
            except Exception:
                pass
        pack = build_evidence_pack(
            ir,
            trial_dir=trial_dir,
            repo_root=root,
            store_root=store_root,
            budget_tokens=budget,
            policy=policy,
        )
        fmt = getattr(args, "format", "markdown")
        if getattr(args, "json", False) or fmt == "json":
            rendered = json.dumps(pack.to_dict(), indent=2)
        else:
            rendered = pack.render_markdown()

        if getattr(args, "output", None):
            out_path = _resolve(root, args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(rendered, encoding="utf-8")
            print(f"Wrote EvidencePack (digest: {pack.pack_digest}) -> {out_path}")
        else:
            print(rendered)
        return 0 if ir.status == "featured" else 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _traj_align_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.interpretation.trajectory_alignment import align_trajectory_pair
    from evallab.interpretation.trajectory_ir import build_trajectory_ir

    explicit_root = _resolve(root, args.runs_dir) if getattr(args, "runs_dir", None) else None

    def _resolve_candidate(cand_str: str) -> Path | None:
        if explicit_root:
            return explicit_root
        try:
            tp = Path(cand_str).resolve()
            rp = root.resolve()
            if tp != rp and rp not in tp.parents:
                return tp.parent if tp.is_file() else tp
        except Exception:
            pass
        return None

    try:
        ir_a = build_trajectory_ir(
            args.trial_a, repo_root=root, explicit_runs_root=_resolve_candidate(args.trial_a)
        )
        ir_b = build_trajectory_ir(
            args.trial_b, repo_root=root, explicit_runs_root=_resolve_candidate(args.trial_b)
        )
        result = align_trajectory_pair(ir_a, ir_b)

        output_str = json.dumps(result.to_dict(), indent=2)
        if getattr(args, "output", None):
            out_path = _resolve(root, args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(output_str, encoding="utf-8")
            print(f"Wrote PairedAlignment (id: {result.alignment_id}) -> {out_path}")
        else:
            print(output_str)
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _traj_benchmark_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.interpretation.benchmark_events import ingest_benchmark_trial
    from evallab.interpretation.benchmark_projection import (
        agent_readable_projection_provenance,
        build_projection_dimensions,
        load_compliance_report,
    )
    from evallab.interpretation.producers.action_memory import extract_action_memory_features
    from evallab.interpretation.producers.mcp_funcdag import extract_mcp_funcdag_features
    from evallab.interpretation.producers.mcp_recovery import extract_mcp_recovery_features
    from evallab.traj import outline_trajectory, resolve_trial_target

    explicit_root = _resolve(root, args.runs_dir) if getattr(args, "runs_dir", None) else None
    if explicit_root is None:
        try:
            target_path = Path(args.trial).resolve()
            repo_resolved = root.resolve()
            if target_path != repo_resolved and repo_resolved not in target_path.parents:
                explicit_root = target_path.parent if target_path.is_file() else target_path
        except Exception:
            pass

    try:
        trial_dir, traj_path, result_path = resolve_trial_target(
            args.trial, repo_root=root, explicit_runs_root=explicit_root
        )
        bundle = ingest_benchmark_trial(trial_dir)
        outline = outline_trajectory(args.trial, repo_root=root, explicit_runs_root=explicit_root)
        step_tokens = [s.prompt_tokens for s in outline.steps if s.prompt_tokens is not None]
        report = (
            load_compliance_report(_resolve(root, args.compliance_report))
            if getattr(args, "compliance_report", None)
            else None
        )
        metadata = (
            json.loads(_resolve(root, args.projection_metadata).read_text(encoding="utf-8"))
            if getattr(args, "projection_metadata", None)
            else {}
        )
        if not isinstance(metadata, dict):
            raise ValueError("projection metadata must be a JSON object")
        dimensions = build_projection_dimensions(bundle, report, metadata=metadata)
        if bundle.contract.family == "action-memory-v1":
            feat_obj = extract_action_memory_features(
                bundle, step_tokens=step_tokens, dimensions=dimensions
            )
        elif bundle.contract.family == "mcp-funcdag-v1":
            feat_obj = extract_mcp_funcdag_features(
                bundle, step_tokens=step_tokens, dimensions=dimensions
            )
        elif bundle.contract.family == "mcp-recovery-v1":
            feat_obj = extract_mcp_recovery_features(
                bundle, step_tokens=step_tokens, dimensions=dimensions
            )
        else:
            raise ValueError(f"Unknown benchmark family: {bundle.contract.family}")

        data = asdict(feat_obj)
        data["projection_provenance"] = agent_readable_projection_provenance(report, dimensions)
        if getattr(args, "json", False):
            print(json.dumps(data, indent=2, default=str))
        else:
            print(f"BENCHMARK OBSERVABLES ({bundle.contract.family}):")
            print(f"  Trial ID:       {data.get('trial_id')}")
            print(f"  Task ID:        {data.get('task_id') or data.get('task_name')}")
            print(
                f"  Agent:          {data.get('agent') or data.get('agent_name') or outline.agent_name}"
            )
            print(f"  Construct:      {data.get('construct')}")
            print(f"  Causal Grade:   {data.get('causal_grade')}")
            print(f"  Truth Digest:   {data.get('verifier_truth_digest')}")
            print("\n  Metrics:")
            for k, v in data.items():
                if k not in (
                    "trial_id",
                    "job_id",
                    "task_name",
                    "agent_name",
                    "construct",
                    "causal_grade",
                    "verifier_truth_digest",
                    "created_at",
                    "contract_family",
                ):
                    print(f"    {k}: {v}")
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Declarative CLI Parser Construction
# ---------------------------------------------------------------------------


def _load_claims_tokenizer(selector: str) -> Callable[[str], int] | object:
    module_name, separator, attribute = selector.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("tokenizer must be MODULE:ATTRIBUTE")
    target: object = import_module(module_name)
    for component in attribute.split("."):
        target = getattr(target, component)
    if callable(target) or callable(getattr(target, "encode", None)):
        return target
    raise TypeError("tokenizer target must be callable or provide encode()")


def _claims_pack_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.interpretation.trajectory_context import build_durable_trajectory_context

    output_format = "json" if args.json else "markdown"
    tokenizer = _load_claims_tokenizer(args.tokenizer) if args.tokenizer is not None else None
    pack = build_durable_trajectory_context(
        trial_id=args.trial,
        repo_root=root,
        derived_root=args.derived_root,
        database_url=args.database_url,
        sidecar_roots=tuple(args.analysis_root or ()),
        semantic_root=args.semantic_root,
        max_bytes=args.max_bytes,
        max_entries=args.max_entries,
        max_tokens=args.max_tokens,
        tokenizer=tokenizer,
        output_format=output_format,
    )
    print(pack.render(output_format), end="")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="evallab",
        description="Run, inspect, and analyze agent evaluations through Harbor.",
    )
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    claims = commands.add_parser("claims", help="Compile durable, provenance-backed claims context")
    claims_commands = claims.add_subparsers(dest="claims_command", required=True)
    claims_pack = claims_commands.add_parser(
        "pack", help="Compile one trial's accepted/current claims"
    )
    claims_pack.add_argument("--trial", required=True, help="Trial identifier")
    claims_pack.add_argument("--max-bytes", type=int, help="Exact UTF-8 output byte bound")
    claims_pack.add_argument("--max-entries", type=int, help="Maximum complete claims")
    claims_pack.add_argument(
        "--max-tokens",
        type=int,
        help="Exact token bound; requires --tokenizer",
    )
    claims_pack.add_argument(
        "--tokenizer",
        help="Explicit tokenizer as MODULE:ATTRIBUTE callable or encode()-provider",
    )
    claims_pack.add_argument("--database-url", help="PostgreSQL catalog URL override")
    claims_pack.add_argument("--derived-root", type=Path, help="Shared Parquet root override")
    claims_pack.add_argument(
        "--analysis-root",
        type=Path,
        action="append",
        help="Analysis sidecar root (repeatable)",
    )
    claims_pack.add_argument("--semantic-root", type=Path, help="Semantic Parquet root")
    claims_pack.add_argument("--json", action="store_true", help="Emit the typed pack as JSON")
    claims_pack.set_defaults(func=_claims_pack_command)

    doctor = commands.add_parser("doctor", help="Check local Harbor, Docker, uv, and PostgreSQL")
    doctor.add_argument(
        "--headless",
        action="store_true",
        help="Fail closed and print only boolean prerequisite status as JSON",
    )
    doctor.set_defaults(func=_doctor_command)

    dashboard = commands.add_parser("dashboard", help="Open the read-only research overview")
    dashboard.add_argument("--address", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8501)
    dashboard.add_argument("--database-url")
    dashboard.set_defaults(func=_dashboard_command)

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
    status.add_argument(
        "--generate",
        action="store_true",
        help="Generate docs/STATUS.md markdown projection to stdout",
    )
    status.add_argument(
        "--update",
        action="store_true",
        help="Generate and update docs/STATUS.md on disk",
    )
    status.add_argument(
        "--target-date",
        type=date.fromisoformat,
        default=None,
        help="Target date for status generation (YYYY-MM-DD)",
    )
    status.add_argument(
        "--output",
        "-o",
        dest="status_output",
        type=Path,
        default=None,
        help="Destination path for status output",
    )
    status.set_defaults(func=_status_command)

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
    preflight.set_defaults(func=_preflight_command)

    submit = commands.add_parser("submit", help="Validate and submit one experiment spec")
    submit.add_argument("path", type=Path)
    submit.set_defaults(func=_submit_command)

    tick = commands.add_parser("tick", help="Reconcile and drain the approved experiment queue")
    tick.add_argument(
        "--parallel",
        type=int,
        default=1,
        metavar="N",
        help="Bounded parallel dispatch worker count (default: 1)",
    )
    tick.add_argument(
        "--max-specs",
        type=int,
        help="Maximum specs admitted to this dispatch batch",
    )
    tick.add_argument(
        "--max-active-trials",
        type=int,
        help="Maximum sum of Harbor-internal concurrent trial slots",
    )
    tick.add_argument(
        "--agent-capacity",
        action="append",
        default=[],
        metavar="AGENT=N",
        help="Per-agent concurrent trial slots (repeatable)",
    )
    tick.set_defaults(func=_tick_command)

    approve = commands.add_parser(
        "approve",
        help="Authorize one queued experiment; required before any billable agent runs",
    )
    approve.add_argument("spec_id")
    approve.add_argument(
        "--actor",
        required=True,
        help="who is authorizing; recorded in queue/events.jsonl and never defaulted",
    )
    approve.add_argument(
        "--despite-quota",
        action="store_true",
        help=(
            "authorize even though the provider reports the subscription "
            "exhausted; recorded on the authorisation event and overrides "
            "nothing else"
        ),
    )
    approve.set_defaults(func=_approve_command)

    reject = commands.add_parser("reject", help="Reject one queued experiment")
    reject.add_argument("spec_id")
    reject.add_argument("--actor", default="peter")
    reject.add_argument("--reason", required=True)
    reject.set_defaults(func=_reject_command)

    stop = commands.add_parser("stop", help="Stop dispatch after the current trial")
    stop.set_defaults(func=_stop_command)

    resume = commands.add_parser("resume", help="Remove the queue stop marker")
    resume.set_defaults(func=_resume_command)

    campaign = commands.add_parser(
        "campaign",
        help="Plan and run immutable policy-gated billable campaigns",
    )
    campaign_commands = campaign.add_subparsers(
        dest="campaign_command",
        required=True,
    )
    campaign_plan = campaign_commands.add_parser(
        "plan",
        help="Validate a campaign definition and freeze its immutable manifest",
    )
    campaign_plan.add_argument("definition", type=Path)
    campaign_plan.add_argument("--json", action="store_true")
    campaign_plan.set_defaults(func=_campaign_plan_command)

    campaign_status = campaign_commands.add_parser(
        "status",
        help="Report queue, budget, archive, and circuit state without mutation",
    )
    campaign_status.add_argument("manifest", type=Path)
    campaign_status.add_argument("--json", action="store_true")
    campaign_status.set_defaults(func=_campaign_status_command)

    campaign_run = campaign_commands.add_parser(
        "run",
        help="Submit a new campaign through PolicyGate and dispatch only admitted specs",
    )
    campaign_resume = campaign_commands.add_parser(
        "resume",
        help="Resume exact-digest attempts without repeating completed work",
    )
    for campaign_execute in (campaign_run, campaign_resume):
        campaign_execute.add_argument("manifest", type=Path)
        campaign_execute.add_argument(
            "--parallel",
            type=int,
            help="Dispatch workers; cannot exceed the frozen campaign ceiling",
        )
        campaign_execute.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and report without queue, journal, or Harbor mutations",
        )
        campaign_execute.add_argument("--json", action="store_true")
    campaign_run.set_defaults(func=_campaign_run_command)
    campaign_resume.set_defaults(func=_campaign_resume_command)

    schedule = commands.add_parser("schedule", help="Manage unattended launchd schedules")
    schedule_commands = schedule.add_subparsers(dest="schedule_command", required=True)
    schedule_install = schedule_commands.add_parser(
        "install", help="Install and load tick/nightly LaunchAgents"
    )
    schedule_install.set_defaults(func=_schedule_install_command)

    digest = commands.add_parser("digest", help="Render one daily digest from catalog and events")
    digest.add_argument("--date", dest="report_date", type=date.fromisoformat)
    digest.set_defaults(func=_digest_command)

    nightly = commands.add_parser("nightly", help="Run the fail-closed unattended nightly cycle")
    nightly.add_argument("--date", dest="report_date", type=date.fromisoformat)
    nightly.set_defaults(func=_nightly_command)

    research = commands.add_parser(
        "research",
        help="Run one guarded analyst/synthesizer/proposer pass",
    )
    research.add_argument("--date", dest="report_date", type=date.fromisoformat)
    research.set_defaults(func=_research_command)

    canary = commands.add_parser("canary", help="Manage version-pinned nightly canaries")
    canary_commands = canary.add_subparsers(dest="canary_command", required=True)
    import_task = canary_commands.add_parser(
        "import-terminal-bench",
        help="Import one task through an immutable Harbor dataset download",
    )
    import_task.add_argument("--dataset-ref", required=True)
    import_task.add_argument("--task-name", required=True)
    import_task.add_argument("--destination", type=Path, required=True)
    import_task.set_defaults(func=_canary_import_command)

    calibrate = commands.add_parser(
        "calibrate", help="Measure a judge against one sealed calibration family"
    )
    calibrate.add_argument("family", choices=("checkout-pool-exhaustion", "retry-storm-backlog"))
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
    calibrate.set_defaults(func=_calibrate_command)

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
    trajectories.set_defaults(func=_trajectories_command)

    compare = commands.add_parser("compare", help="Compare declared trial cohorts")
    compare.add_argument("path", type=Path)
    compare.add_argument("--output-dir", type=Path, default=Path("derived/comparisons"))
    compare.add_argument("--index", action="store_true")
    compare.add_argument("--database-url")
    compare.set_defaults(func=_compare_command)

    curve = commands.add_parser(
        "curve", help="Validate, build, or read an empirical paired capability curve"
    )
    curve_commands = curve.add_subparsers(dest="curve_command", required=True)
    curve_validate = curve_commands.add_parser(
        "validate", help="Validate a curve spec and its paired cohort inputs as JSON"
    )
    curve_validate.add_argument("path", type=Path)
    curve_validate.add_argument("--produced-by", default="evallab")
    curve_validate.set_defaults(func=_curve_command)
    curve_build = curve_commands.add_parser(
        "build", help="Build a provenance-backed empirical curve artifact"
    )
    curve_build.add_argument("path", type=Path)
    curve_build.add_argument("--output", type=Path)
    curve_build.add_argument("--produced-by", default="evallab")
    curve_build.set_defaults(func=_curve_command)
    curve_report = curve_commands.add_parser(
        "report", help="Validate and emit a frozen curve artifact as JSON"
    )
    curve_report.add_argument("path", type=Path)
    curve_report.set_defaults(func=_curve_command)

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
    power.set_defaults(func=_power_command)

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
    report_family.set_defaults(func=_report_family_command)

    report_card = report_commands.add_parser(
        "card", help="Draft a provenance-bearing eval card from a completed spec"
    )
    report_card.add_argument("path", type=Path)
    report_card.add_argument(
        "--output",
        type=Path,
        help="write the eval card (default: render without writing)",
    )
    report_card.set_defaults(func=_report_card_command)

    analyze = commands.add_parser("analyze", help="Plan or index bounded trial analyses")
    analyze_commands = analyze.add_subparsers(dest="analyze_command", required=True)
    analyze_plan_parser = analyze_commands.add_parser(
        "plan", help="Show a no-call stage-5 analysis plan"
    )
    analyze_plan_parser.add_argument("path", type=Path)
    analyze_plan_parser.add_argument("--agent", default="codex")
    analyze_plan_parser.add_argument("--agent-version", default="local")
    analyze_plan_parser.add_argument("--model", default="configured-by-queue")
    analyze_plan_parser.add_argument("--output-dir", type=Path, default=Path("derived/analyses"))
    analyze_plan_parser.set_defaults(func=_analyze_plan_command)

    analyze_worker_plan = analyze_commands.add_parser(
        "worker-plan", help="Read-only: what an analysis-worker cycle would do"
    )
    analyze_worker_plan.set_defaults(func=_analyze_worker_plan_command)

    analyze_worker_status = analyze_commands.add_parser(
        "worker-status", help="Read-only: analysis request counts and states"
    )
    analyze_worker_status.set_defaults(func=_analyze_worker_status_command)

    analyze_worker_run = analyze_commands.add_parser(
        "worker-run-one", help="Run ONE request through normal admission (never self-approves)"
    )
    analyze_worker_run.add_argument("request_id")
    analyze_worker_run.add_argument(
        "--adapter",
        choices=("none", "codex-exec"),
        default="none",
        help="Explicit guarded model adapter; default performs no model call",
    )
    analyze_worker_run.add_argument(
        "--authorization",
        type=Path,
        help="Queue authorization JSON required by codex-exec",
    )
    analyze_worker_run.add_argument(
        "--scratch-dir",
        type=Path,
        default=Path("derived/analyses/scratch"),
    )
    analyze_worker_run.set_defaults(func=_analyze_worker_run_one_command)

    analyze_worker_resolve = analyze_commands.add_parser(
        "worker-resolve-ambiguous",
        help="Explicitly retry or quarantine one possibly-paid ambiguous invocation",
    )
    analyze_worker_resolve.add_argument("request_id")
    analyze_worker_resolve.add_argument("--action", choices=("retry", "quarantine"), required=True)
    analyze_worker_resolve.add_argument("--actor", required=True)
    analyze_worker_resolve.set_defaults(func=_analyze_worker_resolve_ambiguous_command)

    analyze_stub = analyze_commands.add_parser(
        "stub", help="Validate a saved response and write an immutable sidecar"
    )
    analyze_stub.add_argument("path", type=Path)
    analyze_stub.add_argument("--response", type=Path, required=True)
    analyze_stub.add_argument("--output-dir", type=Path, default=Path("derived/analyses"))
    analyze_stub.add_argument("--index", action="store_true")
    analyze_stub.add_argument("--database-url")
    analyze_stub.set_defaults(func=_analyze_stub_command)

    analyze_ingest = analyze_commands.add_parser(
        "ingest-sidecar", help="Index one durable analysis sidecar"
    )
    analyze_ingest.add_argument("path", type=Path)
    analyze_ingest.add_argument("--database-url")
    analyze_ingest.set_defaults(func=_analyze_ingest_sidecar_command)

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
    analyze_review.add_argument(
        "--index",
        action="store_true",
        help="Also index the review into the catalog (analysis_reviews)",
    )
    analyze_review.add_argument("--database-url")
    analyze_review.set_defaults(func=_analyze_review_command)

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
    analyze_agreement.set_defaults(func=_analyze_agreement_command)

    analyze_trial = analyze_commands.add_parser(
        "trial", help="Analyze one cohort-style input from CAS (pack-only, no model)"
    )
    analyze_trial.add_argument("--cas-uri")
    analyze_trial.add_argument("--store", type=Path, default=Path("derived/evidence-cas"))
    analyze_trial.add_argument("--inventory", type=Path)
    analyze_trial.add_argument("--trial-id")
    analyze_trial.add_argument("--output-dir", type=Path, default=Path("derived/interpretation"))
    analyze_trial.add_argument("--database-url")
    analyze_trial.add_argument("--calibration-report", type=Path)
    analyze_trial.set_defaults(func=_analyze_trial_command)

    analyze_batch = analyze_commands.add_parser(
        "batch", help="Consume the merged five-TB3 machine-analysis inventory"
    )
    analyze_batch.add_argument("inventory", type=Path)
    analyze_batch.add_argument("--store", type=Path, default=Path("derived/evidence-cas"))
    analyze_batch.add_argument("--output-dir", type=Path, default=Path("derived/interpretation"))
    analyze_batch.add_argument("--database-url")
    analyze_batch.add_argument("--calibration-report", type=Path)
    analyze_batch.set_defaults(func=_analyze_batch_command)

    analyze_inspect = analyze_commands.add_parser(
        "inspect", help="Reopen artifact lineage and exact citations for one decision"
    )
    analyze_inspect.add_argument("target")
    analyze_inspect.add_argument("--store", type=Path, default=Path("derived/evidence-cas"))
    analyze_inspect.add_argument("--output-dir", type=Path, default=Path("derived/interpretation"))
    analyze_inspect.set_defaults(func=_analyze_inspect_command)

    analyze_calibrate = analyze_commands.add_parser(
        "calibrate", help="Parse a committed CalibrationReport and report hold-only status"
    )
    analyze_calibrate.add_argument("path", type=Path)
    analyze_calibrate.set_defaults(func=_analyze_calibrate_command)

    analyze_quality = analyze_commands.add_parser(
        "quality",
        help="Report per-campaign data-quality HOLD, coverage, CAS identity, and projections (no judge)",
    )
    analyze_quality.add_argument("inventory", type=Path)
    analyze_quality.add_argument("--store", type=Path, default=Path("derived/evidence-cas"))
    analyze_quality.add_argument("--output-dir", type=Path, default=Path("derived/interpretation"))
    analyze_quality.add_argument("--derived-root", type=Path)
    analyze_quality.add_argument("--database-url")
    analyze_quality.add_argument(
        "--fields",
        help="Select named report paths using {name:.path,...} syntax",
    )
    analyze_quality.set_defaults(func=_analyze_quality_command)

    data = commands.add_parser(
        "data",
        help="Completed-trial data layer: reconcile durable trials to ANALYSIS_READY or HOLD",
    )
    data_commands = data.add_subparsers(dest="data_command", required=True)
    data_backfill = data_commands.add_parser(
        "backfill",
        help="Reconcile every durable completed trial to a reason-coded disposition",
    )
    data_backfill.add_argument(
        "--inventory",
        type=Path,
        default=Path("research/experiments/manifests/cross-campaign-analysis-inventory.json"),
    )
    data_backfill.add_argument(
        "--manifest-dir",
        type=Path,
        default=Path("research/experiments/manifests"),
    )
    data_backfill.add_argument(
        "--store-root",
        type=Path,
        default=Path("derived/evidence-cas"),
        help="CAS store root (records/job live under this directory)",
    )
    data_backfill.add_argument("--derived-root", type=Path)
    data_backfill.add_argument(
        "--output-dir",
        type=Path,
        default=Path("derived/analyses/all-durable-backfill"),
    )
    data_backfill.add_argument("--database-url")
    data_backfill.set_defaults(func=_data_backfill_command)

    db = commands.add_parser("db", help="Manage the derived PostgreSQL index")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_init = db_commands.add_parser("init", help="Apply the idempotent schema")
    db_init.add_argument("--database-url")
    db_init.set_defaults(func=_db_init_command)

    db_list = db_commands.add_parser("list", help="List recently ingested trials")
    db_list.add_argument("--database-url")
    db_list.add_argument("--limit", type=int, default=25)
    db_list.set_defaults(func=_db_list_command)

    db_attach = db_commands.add_parser("attach", help="Attach unified DuckDB surface (Z2+Z3+Z4)")
    db_attach.add_argument(
        "--zones", action="store_true", help="report zone status (exit non-zero if none attached)"
    )  # noqa: E501
    db_attach.add_argument(
        "--print-sql", action="store_true", help="emit the attach + view DDL preamble to stdout"
    )  # noqa: E501
    db_attach.add_argument(
        "--query", metavar="SQL", help="run query against the surface and print rows"
    )  # noqa: E501
    db_attach.add_argument(
        "--derived-root",
        type=Path,
        help="override the shared Parquet root (same resolution as library)",
    )  # noqa: E501
    db_attach.set_defaults(func=_db_attach_command)

    lineage = commands.add_parser(
        "lineage", help="Trace recursive lineage of generated artifacts back to Z1"
    )
    lineage.add_argument("target", help="Artifact path or identifier to trace")
    lineage.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON lineage graph"
    )
    lineage.add_argument(
        "--derived-root",
        type=Path,
        help="override the shared Parquet root (same resolution as library)",
    )
    lineage.set_defaults(func=_lineage_command)

    analyst = commands.add_parser(
        "analyst", help="Run, list, and inspect durable trial analyses with reasoning trajectories"
    )
    analyst_commands = analyst.add_subparsers(dest="analyst_command", required=True)
    analyst_run = analyst_commands.add_parser(
        "run",
        help="Run analysis on one trial (deterministic stub by default; --model spends tokens)",
    )
    analyst_run.add_argument("trial_id", help="Trial identifier, trial name, or path")
    analyst_run.add_argument(
        "--model",
        default=None,
        help="Explicit model selector (opt-in spend: default is deterministic stub)",
    )
    analyst_run.add_argument(
        "--derived-root",
        type=Path,
        help="override the shared Parquet root",
    )
    analyst_run.add_argument(
        "--runs-root",
        type=Path,
        help="override candidate runs root for raw trajectory discovery",
    )
    analyst_run.set_defaults(func=_analyst_run_command)

    analyst_list = analyst_commands.add_parser("list", help="List stored analysis conclusions")
    analyst_list.add_argument(
        "--trial",
        dest="trial_id",
        default=None,
        help="filter conclusions by source trial_id",
    )
    analyst_list.set_defaults(func=_analyst_list_command)

    analyst_show = analyst_commands.add_parser(
        "show", help="Show an analysis conclusion and its recorded trajectory"
    )
    analyst_show.add_argument("analysis_id", help="ULID of the analysis record to show")
    analyst_show.add_argument("--json", action="store_true", help="emit raw structured JSON")
    analyst_show.set_defaults(func=_analyst_show_command)

    card = commands.add_parser(
        "card", help="Generate and validate purpose-bound eval cards from completed evidence"
    )
    card_commands = card.add_subparsers(dest="card_command", required=True)
    card_generate = card_commands.add_parser(
        "generate", help="Generate an eval card from a spec_id or job_id"
    )
    card_generate.add_argument("target", help="Spec ID, spec path, job ID, or job path")
    card_generate.add_argument(
        "-o",
        "--output",
        type=Path,
        help="write the eval card (default: render without writing)",
    )
    card_generate.add_argument(
        "--json",
        action="store_true",
        help="print structured card JSON summary",
    )
    card_generate.add_argument(
        "--derived-root",
        type=Path,
        help="override the shared Parquet root",
    )
    card_generate.set_defaults(func=_card_generate_command)

    card_validate = card_commands.add_parser(
        "validate", help="Validate an eval card against schema and mandatory caveats"
    )
    card_validate.add_argument("path", type=Path, help="Path to eval card markdown file")
    card_validate.add_argument(
        "--json",
        action="store_true",
        help="print structured validation result JSON",
    )
    card_validate.set_defaults(func=_card_validate_command)

    behavior = commands.add_parser(
        "behavior", help="Analyze agent execution behavior, effort, and efficiency"
    )
    behavior.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON behavior report"
    )
    behavior.add_argument("--task", help="filter analysis to one task name")
    behavior.add_argument("--agent", help="filter analysis to one agent name")
    behavior.add_argument(
        "--derived-root",
        type=Path,
        help="override the shared Parquet root (same resolution as library)",
    )
    behavior.set_defaults(func=_behavior_command)

    semantic_facts = commands.add_parser(
        "semantic-facts", help="Project and query typed benchmark analysis facts"
    )
    semantic_facts_commands = semantic_facts.add_subparsers(
        dest="semantic_facts_command", required=True
    )
    semantic_facts_project = semantic_facts_commands.add_parser(
        "project", help="Project a normalized JSON fact bundle to typed Parquet"
    )
    semantic_facts_project.add_argument("bundle", type=Path)
    semantic_facts_project.add_argument("--output-dir", type=Path, required=True)
    semantic_facts_project.add_argument("--json", action="store_true")
    semantic_facts_project.set_defaults(func=_semantic_facts_project_command)
    semantic_facts_query = semantic_facts_commands.add_parser(
        "query", help="Query benchmark×construct analysis-readiness scorecards"
    )
    semantic_facts_query.add_argument("output_dir", type=Path)
    semantic_facts_query.add_argument("--benchmark")
    semantic_facts_query.add_argument("--construct")
    semantic_facts_query.set_defaults(func=_semantic_facts_query_command)

    semantics = commands.add_parser(
        "semantics",
        help="Project and query profile-derived semantic action facts",
    )
    semantics_commands = semantics.add_subparsers(
        dest="semantics_command",
        required=True,
    )
    semantics_project = semantics_commands.add_parser(
        "project",
        help="Project normalized ATIF with explicit task-to-profile bindings",
    )
    semantics_project.add_argument("paths", nargs="+", type=Path)
    semantics_project.add_argument(
        "--bind",
        action="append",
        required=True,
        metavar="TASK_ID=PROFILE_ID",
    )
    semantics_project.add_argument("--output-dir", type=Path)
    semantics_project.add_argument(
        "--coverage-threshold",
        type=float,
        required=True,
    )
    semantics_project.add_argument("--permissive", action="store_true")
    semantics_project.add_argument("--json", action="store_true")
    semantics_project.set_defaults(func=_semantics_project_command)
    semantics_coverage = semantics_commands.add_parser(
        "coverage",
        help="Query per-trial semantic coverage at an explicit threshold",
    )
    semantics_coverage.add_argument("--derived-dir", type=Path)
    semantics_coverage.add_argument("--threshold", type=float, required=True)
    semantics_coverage.set_defaults(func=_semantics_coverage_command)

    evidence_parser = commands.add_parser(
        "evidence", help="Archive and restore content-addressed raw evidence"
    )
    evidence_commands = evidence_parser.add_subparsers(dest="evidence_command", required=True)
    evidence_archive = evidence_commands.add_parser("archive")
    evidence_archive.add_argument("source", type=Path)
    evidence_archive.add_argument("--store", type=Path, required=True)
    evidence_archive.add_argument("--record-id")
    evidence_archive.add_argument("--kind", default="job")
    evidence_archive.add_argument("--json", action="store_true")
    evidence_archive.set_defaults(func=_evidence_archive_command)
    evidence_restore = evidence_commands.add_parser("restore")
    evidence_restore.add_argument("uri")
    evidence_restore.add_argument("--store", type=Path, required=True)
    evidence_restore.add_argument("--destination", type=Path, required=True)
    evidence_restore.set_defaults(func=_evidence_restore_command)

    tasks_parser = commands.add_parser("tasks", help="Import and manage task corpora")
    tasks_commands = tasks_parser.add_subparsers(dest="tasks_command", required=True)
    tasks_import = tasks_commands.add_parser(
        "import", help="Restartable batch import of local Harbor task packages"
    )
    tasks_import.add_argument("source", type=Path)
    tasks_import.add_argument(
        "--destination",
        type=Path,
        default=Path("library/tasks/imported"),
    )
    tasks_import.add_argument(
        "--ledger",
        type=Path,
        default=Path("derived/imports/tasks.sqlite3"),
    )
    tasks_import.add_argument("--limit", type=int)
    tasks_import.add_argument("--json", action="store_true")
    tasks_import.set_defaults(func=_tasks_import_command)

    ladder = commands.add_parser(
        "ladder", help="Expand Cartesian evaluation grids into ExperimentSpecs"
    )
    ladder_commands = ladder.add_subparsers(dest="ladder_command", required=True)
    ladder_validate = ladder_commands.add_parser(
        "validate", help="Validate and cardinality-check a plan without writing or submitting"
    )
    ladder_validate.add_argument("grid_spec", type=Path)
    ladder_validate.add_argument("--json", action="store_true")
    ladder_validate.set_defaults(func=_ladder_validate_command)

    ladder_generate = ladder_commands.add_parser(
        "generate", help="Expand a grid specification into ExperimentSpec files"
    )
    ladder_generate.add_argument("grid_spec", type=Path, help="Path to grid YAML/JSON file")
    ladder_generate.add_argument(
        "-o",
        "--output",
        dest="output_dir",
        type=Path,
        default=None,
        help="Directory to write generated ExperimentSpec JSON files",
    )
    ladder_generate.add_argument(
        "--submit",
        action="store_true",
        help="Submit generated ExperimentSpecs directly to the queue",
    )
    ladder_generate.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print expansion and decisions without writing to disk (default)",
    )
    ladder_generate.add_argument(
        "--no-quota-check",
        action="store_true",
        help="Disable automatic headroom/quota checking against existing runs",
    )
    ladder_generate.add_argument(
        "--json",
        action="store_true",
        help="Print generation summary and details in JSON format",
    )
    ladder_generate.set_defaults(func=_ladder_generate_command)

    ladder_screen = ladder_commands.add_parser(
        "screen", help="Staged difficulty screening and follow-up generation"
    )
    screen_commands = ladder_screen.add_subparsers(dest="screen_command", required=True)

    s1_parser = screen_commands.add_parser(
        "stage1", help="Emit Stage 1 screening specs (k=1) across tasks and model levels"
    )
    s1_parser.add_argument("spec", type=Path, help="Path to ScreenSpec YAML/JSON file")
    s1_parser.add_argument(
        "-o",
        "--output",
        dest="output_dir",
        type=Path,
        default=None,
        help="Directory to write generated ExperimentSpec JSON files",
    )
    s1_parser.add_argument(
        "--submit",
        action="store_true",
        help="Submit generated ExperimentSpecs directly to the queue",
    )
    s1_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print expansion without writing to disk (default)",
    )
    s1_parser.add_argument(
        "--json",
        action="store_true",
        help="Print generation summary in JSON format",
    )
    s1_parser.set_defaults(func=_ladder_screen_stage1_command)

    sa_parser = screen_commands.add_parser(
        "analyze", help="Analyze completed Stage 1 results and classify task separation"
    )
    sa_parser.add_argument("screen_id_or_spec", help="Screen ID or path to ScreenSpec file")
    sa_parser.add_argument(
        "--jobs-dir", type=Path, default=None, help="Jobs directory to search for results"
    )
    sa_parser.add_argument(
        "--json",
        action="store_true",
        help="Print analysis report in JSON format",
    )
    sa_parser.set_defaults(func=_ladder_screen_analyze_command)

    s2_parser = screen_commands.add_parser(
        "stage2", help="Emit Stage 2 follow-up specs (k=3) for separating tasks only"
    )
    s2_parser.add_argument("spec", type=Path, help="Path to ScreenSpec YAML/JSON file")
    s2_parser.add_argument(
        "-o",
        "--output",
        dest="output_dir",
        type=Path,
        default=None,
        help="Directory to write generated ExperimentSpec JSON files",
    )
    s2_parser.add_argument(
        "--submit",
        action="store_true",
        help="Submit generated ExperimentSpecs directly to the queue",
    )
    s2_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print expansion without writing to disk (default)",
    )
    s2_parser.add_argument(
        "--jobs-dir", type=Path, default=None, help="Jobs directory to search for results"
    )
    s2_parser.add_argument(
        "--json",
        action="store_true",
        help="Print generation summary in JSON format",
    )
    s2_parser.set_defaults(func=_ladder_screen_stage2_command)

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
    trace.set_defaults(func=_trace_command)

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
    fetch.set_defaults(func=_fetch_command)

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
    gc.set_defaults(func=_gc_command)

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
    registry_list.set_defaults(func=_registry_list_command)

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
        help="Durable task_workbench certification.json to bind",
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
        help="Durable task_workbench certification.json required for new registration",
    )
    registry_register.add_argument(
        "--json",
        action="store_true",
        help="Emit registered record as JSON",
    )
    registry_register.set_defaults(func=_registry_register_command)
    tidy = commands.add_parser(
        "tidy",
        help="Sweep working tree strays, stale worktrees, and retention violations",
    )
    tidy.add_argument(
        "--apply",
        action="store_true",
        help="Execute safe deletions (default is dry-run report)",
    )
    tidy.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report findings without making changes (default behavior)",
    )
    tidy.set_defaults(func=_tidy_command)

    verdict = commands.add_parser(
        "verdict",
        help="Record, list, or inspect append-only human verdicts on discoveries",
    )
    verdict.add_argument(
        "action_or_id",
        nargs="?",
        help="Discovery ID to verdict/inspect, or 'list'/'history'",
    )
    verdict.add_argument(
        "status_or_id",
        nargs="?",
        help="Status ('accepted'|'rejected'|'needs_evidence'|'pending') or ID for history",
    )
    verdict.add_argument(
        "--by",
        help="Human actor issuing the verdict (mandatory for recording)",
    )
    verdict.add_argument(
        "--note",
        help="Free-text rationale or evidence pointer",
    )
    verdict.add_argument(
        "--status",
        help="Filter status for 'verdict list'",
    )
    verdict.add_argument(
        "--json",
        action="store_true",
        help="Emit results as JSON",
    )
    verdict.add_argument(
        "--database-url",
        help="PostgreSQL catalog URL override",
    )
    verdict.add_argument(
        "--derived-root",
        type=Path,
        help="Override the shared Parquet root",
    )
    verdict.set_defaults(func=_verdict_command)
    traj = commands.add_parser(
        "traj", help="Analyze, feature-extract, outline, and label trajectories"
    )
    traj_commands = traj.add_subparsers(dest="traj_command", required=True)

    traj_outline = traj_commands.add_parser(
        "outline", help="Deterministic step outline of an ATIF trajectory"
    )
    traj_outline.add_argument(
        "target", help="Trial ID, directory, result.json, or trajectory.json path"
    )
    traj_outline.add_argument(
        "--verbose", "-v", action="store_true", help="Include detailed step snippets"
    )
    traj_outline.add_argument("--json", action="store_true", help="Emit outline as JSON")
    traj_outline.set_defaults(func=_traj_outline_command)

    traj_queue = traj_commands.add_parser("queue", help="Deterministic daily reading/review queue")
    traj_queue.add_argument(
        "--limit", type=int, default=3, help="Number of trajectories to select (default: 3)"
    )
    traj_queue.add_argument("--runs-dir", type=Path, help="Override candidate runs root")
    traj_queue.add_argument("--json", action="store_true", help="Emit queue as JSON")
    traj_queue.set_defaults(func=_traj_queue_command)

    traj_label = traj_commands.add_parser(
        "label", help="Persist a human or heuristic trajectory behavior label"
    )
    traj_label.add_argument("trial", help="Trial identifier or directory")
    traj_label.add_argument("label", help="Behavior label (e.g. tool_use_loop)")
    traj_label.add_argument("--note", help="Optional label explanation or observation")
    traj_label.add_argument("--provenance", default="human", choices=["human", "heuristic"])
    traj_label.add_argument("--author", default="peter", help="Author of the label")
    traj_label.add_argument("--json", action="store_true", help="Emit label as JSON")
    traj_label.set_defaults(func=_traj_label_command)

    traj_project = traj_commands.add_parser(
        "project", help="Extract mechanical features to Parquet"
    )
    traj_project.add_argument("--output-dir", type=Path, help="Override output Parquet root")
    traj_project.add_argument(
        "--runs-dir", type=Path, action="append", help="Runs directory (repeatable)"
    )
    traj_project.add_argument("--json", action="store_true", help="Emit projection summary as JSON")
    traj_project.set_defaults(func=_traj_project_command)

    traj_report = traj_commands.add_parser(
        "report", help="Precision report of heuristic labels vs human ground truth"
    )
    traj_report.add_argument("--json", action="store_true", help="Emit report as JSON")
    traj_report.set_defaults(func=_traj_report_command)

    traj_card = traj_commands.add_parser(
        "card", help="Render a Trajectory Interpretation Card for a trial"
    )
    traj_card.add_argument("trial", help="Trial identifier, directory, or result.json")
    traj_card.add_argument("--runs-dir", type=Path, help="Override candidate runs root")
    traj_card.add_argument("--output", "-o", type=Path, help="Write card markdown to file")
    traj_card.add_argument("--json", action="store_true", help="Emit card data as JSON")
    traj_card.add_argument(
        "--no-redact", action="store_true", help="Disable on-read secret redaction"
    )
    traj_card.add_argument(
        "--compliance-report",
        type=Path,
        help="Materialized Data ComplianceIngestReport JSON; never invokes the compliance hook",
    )
    traj_card.add_argument(
        "--projection-metadata",
        type=Path,
        help="Agent-Data dimension JSON: harness/scaffold/repeat/dose/alphabet/source digest",
    )
    traj_card.set_defaults(func=_traj_card_command)

    traj_ir = traj_commands.add_parser(
        "ir", help="Build a deterministic TrajectoryIR intermediate representation"
    )
    traj_ir.add_argument("trial", help="Trial identifier, directory, or result.json")
    traj_ir.add_argument("--runs-dir", type=Path, help="Override candidate runs root")
    traj_ir.add_argument("--output", "-o", type=Path, help="Write IR JSON to file")
    traj_ir.set_defaults(func=_traj_ir_command)

    traj_pack = traj_commands.add_parser(
        "pack", help="Build a bounded, citation-preserving EvidencePack for model interpretation"
    )
    traj_pack.add_argument("trial", help="Trial identifier, directory, or result.json")
    traj_pack.add_argument(
        "--budget", type=int, default=16000, help="Token budget (default: 16000)"
    )
    traj_pack.add_argument("--runs-dir", type=Path, help="Override candidate runs root")
    traj_pack.add_argument("--output", "-o", type=Path, help="Write EvidencePack to file")
    traj_pack.add_argument(
        "--format", choices=["markdown", "json"], default="markdown", help="Output format"
    )
    traj_pack.add_argument("--json", action="store_true", help="Emit EvidencePack as JSON")
    traj_pack.add_argument(
        "--no-redact", action="store_true", help="Disable on-read secret redaction"
    )
    traj_pack.set_defaults(func=_traj_pack_command)

    traj_align = traj_commands.add_parser(
        "align", help="Align two counterfactual trajectory branches and detect divergence k*"
    )
    traj_align.add_argument("trial_a", help="First trial identifier, directory, or result.json")
    traj_align.add_argument("trial_b", help="Second trial identifier, directory, or result.json")
    traj_align.add_argument("--runs-dir", type=Path, help="Override candidate runs root")
    traj_align.add_argument("--output", "-o", type=Path, help="Write alignment JSON to file")
    traj_align.set_defaults(func=_traj_align_command)
    traj_bm = traj_commands.add_parser(
        "benchmark", help="Extract and inspect benchmark trajectory observables for a trial"
    )
    traj_bm.add_argument("trial", help="Trial identifier, directory, or result.json")
    traj_bm.add_argument("--runs-dir", type=Path, help="Override candidate runs root")
    traj_bm.add_argument("--json", action="store_true", help="Emit observables as JSON")
    traj_bm.add_argument(
        "--compliance-report",
        type=Path,
        help="Materialized Data ComplianceIngestReport JSON; never invokes the compliance hook",
    )
    traj_bm.add_argument(
        "--projection-metadata",
        type=Path,
        help="Agent-Data dimension JSON: harness/scaffold/repeat/dose/alphabet/source digest",
    )
    traj_bm.set_defaults(func=_traj_benchmark_command)
    return root


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
        handler: Callable[..., int] | None = getattr(args, "func", None)
        if handler is None:
            return 2
        return handler(args, root, harbor=harbor)
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(run_cli(argv))


def legacy_main() -> None:
    print("warning: harbor-lab is deprecated; use evallab", file=sys.stderr)
    main()


if __name__ == "__main__":
    main()
