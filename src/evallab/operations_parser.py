"""Doctor, preflight, and nightly command registration."""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from evallab.cli_support import _resolve

if TYPE_CHECKING:
    from evallab.fetch import HarborBackend


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


def _doctor(
    root: Path, *, home: Path | None = None, clock: Callable[[], datetime] | None = None
) -> int:
    from evallab import database
    from evallab.automation import audit_launch_agents, sync_primary_status
    from evallab.evidence.atif import check_projection_invariant
    from evallab.gc import doctor_disk_line
    from evallab.queue import Executor
    from evallab.runner import database_url_from_environment
    from evallab.storage.paths import derived_root_from_environment

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


def register_parser(commands: argparse._SubParsersAction) -> None:
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
